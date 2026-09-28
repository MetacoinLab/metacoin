"""Typed multi-step workflow engine: immutable definitions, runs, node states, review gates.

Definition (`metacoin-workflow/v1`, digested and immutable once stored):
  {"schema": "metacoin-workflow/v1", "name": str,
   "nodes": [{"id": str, "type": <allowlisted>, "depends_on": [str | {"node": str, "require": ...}], ...params}],
   "outputs": [node ids that must succeed for the run to be complete]}

Node types (allowlisted; no expressions, imports, shell or uploaded code anywhere):
  dataset          {"dataset_version_id" | slot "bind": "<slot>"}          -> immutable dataset reference
  temporal_energy  {"input": <dataset node>, "parameters": {...}}           -> contract + job (temporal-energy/v1)
  energy_audit     {"input": <dataset node> | "inputs": {...}, "parameters"} -> contract + job (interval model)
  safe_runtime / task_selection / plan_comparison {"inputs": {...}}         -> contract + job
  review_gate      {"input": <service node>}                                -> waits for the designated reviewer's signed decision
  export           {"input": <service node>, "fields": [...]}                -> permitted public projection artifact

Dependency conditions: a plain id means "succeeded"; {"node": id, "require": "accepted_review"} needs an
accepted signed review of that node's job; {"node": id, "require": {"outcome_in": [...]}} needs the
upstream scientific outcome to be in the set (a valid INFEASIBLE is a result, not a failure).
Parameters may take integers from an upstream summary through {"from": {"node": id, "field": <allowlisted>}}.

Bindings record, per node, the exact upstream job id, evidence root, artifact id and field used.
Runs advance through `advance()` ticks (called by the worker loop and on request); each tick is one
transaction per node transition. Node execution reuses contracts/jobs/reviews unchanged.
"""
import hashlib
import json
import secrets
from experiments.private_receipts import receipt as merkle
from . import history, budgets
from .auth import Principal
from .datasets import add_edge
from .db import now
from .errors import ServiceError

SCHEMA = 'metacoin-workflow/v1'
SERVICE_TYPES = ('temporal_energy', 'energy_audit', 'safe_runtime', 'task_selection', 'plan_comparison', 'temporal_batch', 'monte_carlo_reliability', 'heat_diffusion', 'resource_plan')
NODE_TYPES = ('dataset',) + SERVICE_TYPES + ('review_gate', 'export')
LIMITS = {'max_nodes': 32, 'max_edges': 64, 'max_outputs': 8, 'max_export_fields': 16}
FROM_FIELDS = {'safe_duration', 'additional_usable_energy', 'required_high', 'required_low', 'worst_margin', 'best_margin',
               'total_value', 'min_reserve_margin_pessimistic', 'horizon_seconds'}
EXPORT_FIELDS = ('outcome', 'model_id', 'verifier_id', 'verifier_digest', 'contract_digest', 'evidence_root', 'review_decision',
                 'review_key_id', 'envelope_digest', 'horizon_seconds', 'safe_duration', 'selected_id', 'selected_ids', 'status')
NODE_STATES = ('pending', 'ready', 'waiting_dependency', 'waiting_review', 'blocked', 'queued', 'running', 'succeeded', 'failed', 'cancelled')
RUN_STATES = ('created', 'running', 'waiting_review', 'blocked', 'partially_failed', 'cancelled', 'completed', 'failed')


def validate_definition(definition):
    """Structural validation with node-identifier-precise errors. Returns (digest, topological order)."""
    merkle.canonical(definition)
    errors = []
    if type(definition) is not dict or definition.get('schema') != SCHEMA:
        raise ServiceError('VALIDATION', {'node': None, 'code': 'schema', 'expected': SCHEMA})
    allowed_top = {'schema', 'name', 'nodes', 'outputs', 'slots'}
    if not set(definition) <= allowed_top or not {'schema', 'name', 'nodes', 'outputs'} <= set(definition):
        raise ServiceError('VALIDATION', {'node': None, 'code': 'top_level_fields', 'allowed': sorted(allowed_top)})
    if type(definition['name']) is not str or not 1 <= len(definition['name']) <= 128:
        errors.append({'node': None, 'code': 'name'})
    slots = definition.get('slots', {})
    if type(slots) is not dict or len(slots) > 16 or not all(
            type(k) is str and 1 <= len(k) <= 32 and k.replace('_', '').isalnum() and type(v) is dict and set(v) <= {'type', 'min', 'max', 'description'}
            and v.get('type') == 'integer' and all(type(v[b]) is int and type(v[b]) is not bool for b in ('min', 'max') if b in v)
            and (type(v.get('description', '')) is str and len(v.get('description', '')) <= 256) for k, v in slots.items()):
        raise ServiceError('VALIDATION', {'node': None, 'code': 'slots', 'expected': "{name: {type: 'integer', min?: int, max?: int, description?: str}}", 'max_slots': 16})
    nodes = definition['nodes']
    if type(nodes) is not list or not 1 <= len(nodes) <= LIMITS['max_nodes']:
        raise ServiceError('VALIDATION', {'node': None, 'code': 'node_count', 'max': LIMITS['max_nodes']})
    ids, by_id, edges = [], {}, 0
    for n in nodes:
        if type(n) is not dict or type(n.get('id')) is not str or not 1 <= len(n['id']) <= 32 or not n['id'].replace('_', '').replace('-', '').isalnum():
            errors.append({'node': n.get('id') if type(n) is dict else None, 'code': 'node_id'}); continue
        if n['id'] in by_id:
            errors.append({'node': n['id'], 'code': 'duplicate_node'}); continue
        ids.append(n['id']); by_id[n['id']] = n
    for n in [by_id[i] for i in ids]:
        nid = n['id']
        if n.get('type') not in NODE_TYPES:
            errors.append({'node': nid, 'code': 'unsupported_type', 'allowed': list(NODE_TYPES)}); continue
        deps = n.get('depends_on', [])
        if type(deps) is not list or len(deps) > 8:
            errors.append({'node': nid, 'code': 'depends_on'}); continue
        seen = set()
        for d in deps:
            key = d if type(d) is str else (d.get('node') if type(d) is dict else None)
            if key not in by_id:
                errors.append({'node': nid, 'code': 'unknown_dependency', 'dependency': key})
            elif key in seen:
                errors.append({'node': nid, 'code': 'duplicate_edge', 'dependency': key})
            elif key == nid:
                errors.append({'node': nid, 'code': 'self_dependency'})
            seen.add(key); edges += 1
            if type(d) is dict:
                req = d.get('require')
                if set(d) != {'node', 'require'} or not (req in ('succeeded', 'accepted_review') or (type(req) is dict and set(req) == {'outcome_in'}
                        and type(req['outcome_in']) is list and req['outcome_in'] and all(type(o) is str for o in req['outcome_in']))):
                    errors.append({'node': nid, 'code': 'condition'})
        extra = set(n) - {'id', 'type', 'depends_on'}
        t = n['type']
        if t == 'dataset':
            if not (extra == {'dataset_version_id'} and type(n['dataset_version_id']) is str) and not (extra == {'bind'} and type(n['bind']) is str):
                errors.append({'node': nid, 'code': 'dataset_params', 'expected': 'dataset_version_id | bind'})
        elif t in SERVICE_TYPES:
            if not extra <= {'input', 'inputs', 'parameters', 'policy', 'budget'}:
                errors.append({'node': nid, 'code': 'unexpected_params', 'allowed': ['input', 'inputs', 'parameters', 'policy', 'budget']})
            if 'budget' in n and (type(n['budget']) is not int or type(n['budget']) is bool or n['budget'] < 0):
                errors.append({'node': nid, 'code': 'budget_type', 'expected': 'non-negative integer ceiling for this node'})
            if ('input' in n) == ('inputs' in n):
                errors.append({'node': nid, 'code': 'input_xor_inputs'})
            if 'input' in n and (n['input'] not in by_id or by_id[n['input']].get('type') != 'dataset' or n['input'] not in seen):
                errors.append({'node': nid, 'code': 'input_must_be_dataset_dependency'})
            if 'inputs' in n and type(n['inputs']) is not dict:
                errors.append({'node': nid, 'code': 'inputs_object'})
            for k, v in (n.get('parameters') or {}).items():
                if type(v) is dict and set(v) == {'slot'}:
                    if type(v['slot']) is not str or v['slot'] not in slots:
                        errors.append({'node': nid, 'code': 'undeclared_slot', 'parameter': k, 'slot': v.get('slot'), 'declared': sorted(slots)})
                    continue
                if type(v) is dict:
                    src = v.get('from') if set(v) == {'from'} else None
                    if not (type(src) is dict and set(src) == {'node', 'field'} and src['node'] in seen and src['field'] in FROM_FIELDS):
                        errors.append({'node': nid, 'code': 'parameter_mapping', 'parameter': k, 'allowed_fields': sorted(FROM_FIELDS)})
                elif type(v) not in (int, str) or type(v) is bool:
                    errors.append({'node': nid, 'code': 'parameter_type', 'parameter': k})
            if 'policy' in n and (type(n['policy']) is not dict or not set(n['policy']) <= {'accepted_outcomes', 'disclose_outcome', 'disclose_explanation', 'amount', 'expires_in_seconds'}):
                errors.append({'node': nid, 'code': 'policy_fields'})
        elif t == 'review_gate':
            if extra != {'input'} or n['input'] not in seen or by_id.get(n['input'], {}).get('type') not in SERVICE_TYPES:
                errors.append({'node': nid, 'code': 'review_gate_input_must_be_service_dependency'})
        elif t == 'export':
            fields = n.get('fields')
            if extra != {'input', 'fields'} or n['input'] not in seen or by_id.get(n['input'], {}).get('type') not in SERVICE_TYPES \
                    or type(fields) is not list or not 1 <= len(fields) <= LIMITS['max_export_fields'] or not set(fields) <= set(EXPORT_FIELDS):
                errors.append({'node': nid, 'code': 'export_params', 'allowed_fields': list(EXPORT_FIELDS)})
    if edges > LIMITS['max_edges']:
        errors.append({'node': None, 'code': 'edge_count', 'max': LIMITS['max_edges']})
    outputs = definition['outputs']
    if type(outputs) is not list or not 1 <= len(outputs) <= LIMITS['max_outputs'] or len(set(outputs)) != len(outputs) \
            or not all(o in by_id for o in outputs):
        errors.append({'node': None, 'code': 'outputs'})
    if errors:
        raise ServiceError('VALIDATION', {'code': 'workflow', 'errors': errors[:20]})
    # cycle detection + topological order (Kahn), deterministic by declaration order
    indeg = {i: 0 for i in ids}
    succ = {i: [] for i in ids}
    for i in ids:
        for d in by_id[i].get('depends_on', []):
            key = d if type(d) is str else d['node']
            indeg[i] += 1; succ[key].append(i)
    order, ready = [], [i for i in ids if indeg[i] == 0]
    while ready:
        cur = ready.pop(0); order.append(cur)
        for nxt in succ[cur]:
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                ready.append(nxt)
    if len(order) != len(ids):
        raise ServiceError('VALIDATION', {'code': 'workflow', 'errors': [{'node': i, 'code': 'cycle'} for i in ids if indeg[i] > 0]})
    # every required output must be reachable from some dataset/inline root (it always is in a DAG); check outputs are not orphaned pending
    digest = hashlib.sha256(b'metacoin/workflow-definition/v1\0' + merkle.canonical(definition)).hexdigest()
    return digest, order


def slot_references(definition):
    """Names of parameter slots referenced by service nodes ({'slot': name})."""
    return {v['slot'] for n in definition['nodes'] for v in (n.get('parameters') or {}).values() if type(v) is dict and set(v) == {'slot'}}


def estimate(definition, limits):
    """Admission estimate with stated basis; hard limits bound the work regardless."""
    service_nodes = [n for n in definition['nodes'] if n['type'] in SERVICE_TYPES]
    attempts = 1 + limits['job_max_retries']
    return {'basis': 'counts from the validated definition and hard application limits; not measured runtimes',
            'nodes': len(definition['nodes']), 'service_nodes': len(service_nodes), 'review_gates': sum(n['type'] == 'review_gate' for n in definition['nodes']),
            'max_job_attempts': len(service_nodes) * attempts,
            'machine_time_upper_bound_seconds': len(service_nodes) * attempts * limits['job_timeout_seconds'],
            'monetary_exposure_max': {'amount': sum((n.get('policy') or {}).get('amount', 1) for n in service_nodes if n['type'] == 'energy_audit'),
                                      'unit': 'campaign asset base units', 'basis': 'one bounded action per energy-audit node at its policy amount'},
            'modeled_scientific_energy': 'per-node result; not money, not measured electricity'}


class Workflows:
    def __init__(self, contracts, jobs, reviews, datasets, store, settings):
        self.contracts, self.jobs, self.reviews, self.datasets, self.store, self.settings = contracts, jobs, reviews, datasets, store, settings

    # ---- definitions -----------------------------------------------------------------
    def create(self, db, principal, definition):
        principal.require('contract:create')
        digest, order = validate_definition(definition)
        existing = db.execute('SELECT id FROM workflow_definitions WHERE workspace=? AND digest=?', (principal.workspace, digest)).fetchone()
        if existing:
            return existing['id'], digest, False
        wid = 'wf_' + secrets.token_hex(8)
        version = db.execute('SELECT COALESCE(MAX(version),0)+1 FROM workflow_definitions WHERE workspace=? AND name=?', (principal.workspace, definition['name'])).fetchone()[0]
        db.execute('INSERT INTO workflow_definitions VALUES (?,?,?,?,?,?,?,?,?)',
                   (wid, principal.workspace, principal.id, definition['name'], version, digest, merkle.canonical(definition).decode(),
                    json.dumps({'order': order, 'limits': LIMITS, 'estimate': estimate(definition, self.settings.limits)}), now()))
        history.record(db, principal.workspace, principal.id, 'contract.created', 'workflow_definition', wid, {'digest': digest, 'version': version})
        return wid, digest, True

    def get_definition(self, db, principal, wid):
        principal.require('contract:read')
        row = db.execute('SELECT * FROM workflow_definitions WHERE id=? AND workspace=?', (wid, principal.workspace)).fetchone()
        if row is None:
            raise ServiceError('NOT_FOUND', 'workflow definition')
        return row

    def definition_view(self, row):
        meta = json.loads(row['limits_json'])
        return {'id': row['id'], 'name': row['name'], 'version': row['version'], 'digest': row['digest'], 'owner_id': row['owner_id'],
                'definition': merkle.parse(row['definition_json']), 'order': meta['order'], 'limits': meta['limits'], 'estimate': meta['estimate'],
                'created_at': row['created_at']}

    # ---- runs --------------------------------------------------------------------------
    def instantiate(self, db, principal, wid, values, name=None):
        """Fill a template's integer parameter slots and create a new immutable definition. The instance carries
        the caller's values only; it inherits no bindings, runs, budgets or grants from the template's owner."""
        principal.require('contract:create')
        drow = self.get_definition(db, principal, wid)
        template = merkle.parse(drow['definition_json'])
        slots = template.get('slots', {})
        referenced = slot_references(template)
        if not referenced:
            raise ServiceError('VALIDATION', {'code': 'not_a_template', 'detail': 'the definition references no parameter slots'})
        if type(values) is not dict or set(values) != referenced or not all(type(v) is int and type(v) is not bool for v in values.values()):
            raise ServiceError('VALIDATION', {'code': 'slot_values', 'required': sorted(referenced), 'given': sorted(values) if type(values) is dict else None})
        for k, v in values.items():
            spec = slots[k]
            if ('min' in spec and v < spec['min']) or ('max' in spec and v > spec['max']):
                raise ServiceError('VALIDATION', {'code': 'slot_out_of_range', 'slot': k, 'value': v, 'min': spec.get('min'), 'max': spec.get('max')})
        instance = json.loads(json.dumps(template))
        for n in instance['nodes']:
            for k, v in list((n.get('parameters') or {}).items()):
                if type(v) is dict and set(v) == {'slot'}:
                    n['parameters'][k] = values[v['slot']]
        instance.pop('slots', None)
        if name is not None:
            if type(name) is not str or not 1 <= len(name) <= 128:
                raise ServiceError('VALIDATION', 'name')
            instance['name'] = name
        new_id, digest, created = self.create(db, principal, instance)
        if created:
            add_edge(db, principal.workspace, 'workflow_definition', wid, 'workflow_definition', new_id, 'derived_from')
            history.record(db, principal.workspace, principal.id, 'contract.created', 'workflow_definition', new_id, {'instantiated_from': wid, 'slots': sorted(values), 'digest': digest})
        return dict(self.definition_view(self.get_definition(db, principal, new_id)), template_id=wid, values=values, created=created)

    def start_run(self, db, principal, wid, bindings=None, budget_ceiling=None, preview=False, reuse_nodes=None):
        principal.require('job:submit')
        drow = self.get_definition(db, principal, wid)
        definition = merkle.parse(drow['definition_json'])
        unbound = slot_references(definition)
        if unbound:
            raise ServiceError('VALIDATION', {'code': 'template_has_parameter_slots', 'slots': sorted(unbound), 'action': 'instantiate the template with values, then run the instance'})
        bindings = bindings or {}
        if type(bindings) is not dict or not all(type(k) is str and type(v) is str for k, v in bindings.items()):
            raise ServiceError('VALIDATION', 'bindings must map slot names to dataset version ids')
        reuse_nodes = sorted(set(reuse_nodes or []))
        if not all(type(x) is str and any(n['id'] == x for n in definition['nodes']) for x in reuse_nodes):
            raise ServiceError('VALIDATION', {'code': 'reuse_nodes', 'detail': 'node ids of this definition'})
        for n in definition['nodes']:
            if n['type'] == 'dataset':
                vid = n.get('dataset_version_id') or bindings.get(n.get('bind'))
                if not vid:
                    raise ServiceError('VALIDATION', {'node': n['id'], 'code': 'unbound_slot', 'slot': n.get('bind')})
                self.datasets.version(db, principal, vid)      # workspace-scoped existence check
        if budget_ceiling is not None:
            if type(budget_ceiling) is not int or budget_ceiling < 0:
                raise ServiceError('VALIDATION', 'budget_ceiling')
        est = estimate(definition, self.settings.limits)
        active = db.execute("SELECT COUNT(*) FROM workflow_runs WHERE workspace=? AND state IN ('created','running','waiting_review')", (principal.workspace,)).fetchone()[0]
        if active >= self.settings.limits.get('max_active_workflows', 20):
            raise ServiceError('RATE_LIMITED', 'active workflow quota')
        if preview:
            return {'preview': True, 'definition_id': wid, 'digest': drow['digest'], 'estimate': est, 'bindings': bindings, 'budget_ceiling': budget_ceiling}
        from .agents import guard
        guard(db, principal, 'workflow:run', workflows=1, jobs=est['service_nodes'])
        rid = 'run_' + secrets.token_hex(8)
        # hierarchical budget: workspace -> run (if a ceiling was given) -> node (if the definition gives one)
        parent = budgets.root(db, principal.workspace)['id']
        if budget_ceiling is not None:
            parent = budgets.create_child(db, principal.workspace, parent, 'workflow_run', rid, budget_ceiling)
        for n in definition['nodes']:
            if 'budget' in n:
                budgets.create_child(db, principal.workspace, parent, 'workflow_node', rid + '/' + n['id'], n['budget'])
        db.execute('INSERT INTO workflow_runs VALUES (?,?,?,?,?,?,?,?,?,0,?,?,NULL)',
                   (rid, principal.workspace, wid, principal.id, 'created', budget_ceiling, json.dumps(bindings), json.dumps(dict(est, reuse_nodes=reuse_nodes)), None, now(), now()))
        for n in definition['nodes']:
            db.execute('INSERT INTO workflow_nodes (run_id, node_id, type, state, attempts, updated_at) VALUES (?,?,?,?,0,?)', (rid, n['id'], n['type'], 'pending', now()))
        history.record(db, principal.workspace, principal.id, 'job.queued', 'workflow_run', rid, {'definition_id': wid, 'digest': drow['digest']})
        add_edge(db, principal.workspace, 'workflow_definition', wid, 'workflow_run', rid, 'used_input')
        db.execute("UPDATE workflow_runs SET state='running' WHERE id=?", (rid,))
        return {'run_id': rid, 'definition_id': wid, 'digest': drow['digest'], 'estimate': est}

    def _run(self, db, run_id, workspace=None):
        row = db.execute('SELECT * FROM workflow_runs WHERE id=?' + (' AND workspace=?' if workspace else ''), (run_id,) + ((workspace,) if workspace else ())).fetchone()
        if row is None:
            raise ServiceError('NOT_FOUND', 'workflow run')
        return row

    def _principal(self, db, principal_id):
        row = db.execute('SELECT * FROM principals WHERE id=?', (principal_id,)).fetchone()
        if row is None or row['revoked_at'] is not None:
            raise ServiceError('FORBIDDEN', 'run owner revoked')
        return Principal(row)

    def view(self, db, principal, run_id):
        principal.require('job:read')
        run = self._run(db, run_id, principal.workspace)
        drow = db.execute('SELECT * FROM workflow_definitions WHERE id=?', (run['definition_id'],)).fetchone()
        order = json.loads(drow['limits_json'])['order']
        nodes = {r['node_id']: dict(r) for r in db.execute('SELECT * FROM workflow_nodes WHERE run_id=?', (run_id,))}
        out_nodes = []
        for nid in order:
            n = nodes[nid]
            item = {'node_id': nid, 'type': n['type'], 'state': n['state'], 'attempts': n['attempts'], 'blocked_reason': n['blocked_reason'],
                    'job_id': n['job_id'], 'contract_id': n['contract_id'], 'evidence_root': n['output_root'], 'artifact_id': n['output_artifact_id'],
                    'binding': json.loads(n['binding_json']) if n['binding_json'] else None, 'updated_at': n['updated_at']}
            if n['job_id'] and (principal.can('job:read_private')):
                job = db.execute('SELECT outcome, review_state FROM jobs WHERE id=?', (n['job_id'],)).fetchone()
                item['outcome'], item['review_state'] = job['outcome'], job['review_state']
            elif n['job_id']:
                job = db.execute('SELECT review_state FROM jobs WHERE id=?', (n['job_id'],)).fetchone()
                item['review_state'] = job['review_state']
            out_nodes.append(item)
        definition = merkle.parse(drow['definition_json'])
        return {'run_id': run_id, 'definition_id': run['definition_id'], 'digest': drow['digest'], 'name': drow['name'], 'state': run['state'],
                'budget_ceiling': run['budget_ceiling'], 'bindings': json.loads(run['bindings_json']), 'estimate': json.loads(run['estimate_json']),
                'cancel_requested': bool(run['cancel_requested']), 'created_at': run['created_at'], 'updated_at': run['updated_at'],
                'finished_at': run['finished_at'], 'nodes': out_nodes, 'outputs': definition['outputs'],
                'budget': self._budget_summary(db, run_id),
                'deliverables': {o: nodes[o]['state'] == 'succeeded' for o in definition['outputs']},
                'summary': json.loads(run['summary_json']) if run['summary_json'] else None}

    def _budget_summary(self, db, run_id):
        run_node = budgets.node_for(db, 'workflow_run', run_id)
        nodes = [dict(r) for r in db.execute("SELECT kind, ref_id, ceiling, reserved, committed FROM budget_nodes WHERE kind='workflow_node' AND ref_id LIKE ?", (run_id + '/%',))]
        reservations = [dict(r) for r in db.execute("SELECT ref_id, amount, state FROM budget_reservations WHERE ref_type='workflow_node' AND ref_id LIKE ?", (run_id + '/%',))]
        return {'run_ceiling': run_node['ceiling'] if run_node else None, 'run_reserved': run_node['reserved'] if run_node else None,
                'run_committed': run_node['committed'] if run_node else None, 'node_ceilings': nodes, 'reservations': reservations,
                'reserved_total': sum(r['amount'] for r in reservations if r['state'] == 'reserved'), 'committed_total': sum(r['amount'] for r in reservations if r['state'] == 'committed')}

    def list(self, db, principal, limit=50):
        principal.require('job:read')
        rows = db.execute('SELECT r.id, r.state, r.created_at, r.updated_at, d.name, d.digest FROM workflow_runs r JOIN workflow_definitions d ON d.id=r.definition_id '
                          'WHERE r.workspace=? ORDER BY r.created_at DESC, r.id LIMIT ?', (principal.workspace, min(int(limit), 100))).fetchall()
        return [dict(r) for r in rows]

    def cancel(self, db, principal, run_id):
        principal.require('job:cancel')
        run = self._run(db, run_id, principal.workspace)
        if run['state'] in ('completed', 'cancelled', 'failed'):
            raise ServiceError('CONFLICT', 'run is terminal')
        db.execute('UPDATE workflow_runs SET cancel_requested=1, updated_at=? WHERE id=?', (now(), run_id))
        history.record(db, principal.workspace, principal.id, 'job.cancelled', 'workflow_run', run_id, {'requested': True})
        return self.advance(db, run_id)

    # ---- execution ---------------------------------------------------------------------
    def _deps_satisfied(self, db, run_id, node_def, nodes):
        """Returns ('ok'|'wait'|'blocked', reason)."""
        for d in node_def.get('depends_on', []):
            key = d if type(d) is str else d['node']
            req = 'succeeded' if type(d) is str else d['require']
            up = nodes[key]
            if up['state'] in ('failed', 'cancelled', 'blocked'):
                return 'blocked', 'dependency ' + key + ' ' + up['state'] + (': ' + up['blocked_reason'] if up['blocked_reason'] else '')
            if up['state'] != 'succeeded':
                return 'wait', 'dependency ' + key + ' is ' + up['state']
            if req == 'accepted_review' or type(req) is dict:
                job = db.execute('SELECT outcome, review_state FROM jobs WHERE id=?', (up['job_id'],)).fetchone() if up['job_id'] else None
                if req == 'accepted_review':
                    if job is None or job['review_state'] == 'rejected':
                        return 'blocked', 'dependency ' + key + ' review rejected or absent'
                    if job['review_state'] != 'accepted':
                        return 'wait', 'dependency ' + key + ' awaiting accepted review'
                else:
                    if job is None or job['outcome'] not in req['outcome_in']:
                        return 'blocked', 'dependency ' + key + ' outcome not in ' + ','.join(req['outcome_in'])
        return 'ok', None

    def _resolve_param(self, db, value, nodes, binding):
        if type(value) is dict:
            src = value['from']
            up = nodes[src['node']]
            job = db.execute('SELECT summary_json, evidence_root FROM jobs WHERE id=?', (up['job_id'],)).fetchone()
            summary = json.loads(job['summary_json'] or '{}')
            if src['field'] not in summary or type(summary[src['field']]) is not int:
                raise ServiceError('CONFLICT', 'upstream field unavailable: ' + src['field'])
            binding.append({'node': src['node'], 'job_id': up['job_id'], 'evidence_root': job['evidence_root'], 'field': src['field'], 'value': summary[src['field']]})
            return summary[src['field']]
        return value

    def _start_service_node(self, db, run, owner, node_def, nodes, bindings, drow):
        nid = node_def['id']
        binding = []
        params = {k: self._resolve_param(db, v, nodes, binding) for k, v in (node_def.get('parameters') or {}).items()}
        if 'input' in node_def:
            up = nodes[node_def['input']]
            vid = json.loads(up['binding_json'])['dataset_version_id']
            binding.append({'node': node_def['input'], 'dataset_version_id': vid, 'commitment': json.loads(up['binding_json'])['commitment']})
            inputs = {'dataset_version_id': vid, 'parameters': params}
        else:
            inputs = dict(node_def['inputs'], **params) if params else node_def['inputs']
        policy = dict(node_def.get('policy') or {})
        policy.setdefault('reviewer_id', db.execute("SELECT id FROM principals WHERE workspace=? AND role='reviewer' AND revoked_at IS NULL ORDER BY created_at LIMIT 1",
                                                     (run['workspace'],)).fetchone()['id'])
        cid = self.contracts.create_draft(db, owner, kind=node_def['type'], title=drow['name'] + '/' + nid, inputs=inputs, policy=policy, datasets=self.datasets)
        self.contracts.freeze(db, owner, cid)
        amount = json.loads(db.execute('SELECT policy_json FROM contracts WHERE id=?', (cid,)).fetchone()['policy_json']).get('amount', 0)
        bnode = budgets.node_for(db, 'workflow_node', run['id'] + '/' + nid) or budgets.node_for(db, 'workflow_run', run['id']) or budgets.root(db, run['workspace'])
        budgets.reserve(db, run['workspace'], bnode['id'], amount, 'workflow_node', run['id'] + '/' + nid)     # refuses atomically before any job exists
        jid = self.jobs.submit(db, owner, cid, reuse=nid in (json.loads(run['estimate_json']).get('reuse_nodes') or []))
        db.execute("UPDATE workflow_nodes SET state='queued', job_id=?, contract_id=?, attempts=attempts+1, binding_json=?, updated_at=? WHERE run_id=? AND node_id=?",
                   (jid, cid, json.dumps({'upstream': binding, 'contract_digest': db.execute('SELECT contract_digest FROM contracts WHERE id=?', (cid,)).fetchone()[0]}),
                    now(), run['id'], nid))
        add_edge(db, run['workspace'], 'workflow_run', run['id'], 'job', jid, 'produced')

    def advance(self, db, run_id):
        """One scheduling tick for a run; safe to call repeatedly (idempotent transitions)."""
        run = self._run(db, run_id)
        if run['state'] in ('completed', 'cancelled', 'failed'):
            return self._state_only(db, run_id)
        drow = db.execute('SELECT * FROM workflow_definitions WHERE id=?', (run['definition_id'],)).fetchone()
        definition = merkle.parse(drow['definition_json'])
        order = json.loads(drow['limits_json'])['order']
        by_id = {n['id']: n for n in definition['nodes']}
        bindings = json.loads(run['bindings_json'])
        owner = self._principal(db, run['started_by'])
        nodes = {r['node_id']: dict(r) for r in db.execute('SELECT * FROM workflow_nodes WHERE run_id=?', (run_id,))}
        cancel = bool(run['cancel_requested'])

        def set_state(nid, state, reason=None, **cols):
            sets = ', '.join(['state=?', 'blocked_reason=?', 'updated_at=?'] + [k + '=?' for k in cols])
            db.execute('UPDATE workflow_nodes SET ' + sets + ' WHERE run_id=? AND node_id=?', (state, reason, now(), *cols.values(), run_id, nid))
            nodes[nid].update(state=state, blocked_reason=reason, **cols)

        for nid in order:
            n, d = nodes[nid], by_id[nid]
            if n['state'] in ('succeeded', 'failed', 'cancelled', 'blocked'):
                continue
            if cancel and n['state'] in ('pending', 'ready', 'waiting_dependency', 'waiting_review'):
                set_state(nid, 'cancelled', 'run cancelled'); continue
            if n['state'] in ('queued', 'running'):
                job = db.execute('SELECT state, evidence_root, evidence_artifact_id, cancel_requested FROM jobs WHERE id=?', (n['job_id'],)).fetchone()
                if cancel and job['state'] in ('queued', 'running') and not job['cancel_requested']:
                    self.jobs.cancel(db, owner, n['job_id'])
                if job['state'] == 'succeeded':
                    budgets.settle(db, 'workflow_node', run_id + '/' + nid, 'commit')
                    set_state(nid, 'succeeded', None, output_root=job['evidence_root'], output_artifact_id=job['evidence_artifact_id'])
                elif job['state'] in ('failed', 'cancelled'):
                    budgets.settle(db, 'workflow_node', run_id + '/' + nid, 'release')
                    set_state(nid, job['state'], 'job ' + job['state'])
                elif job['state'] == 'running' and n['state'] == 'queued':
                    set_state(nid, 'running')
                continue
            if n['state'] == 'waiting_review':
                job = db.execute('SELECT review_state FROM jobs WHERE id=?', (nodes[d['input']]['job_id'],)).fetchone()
                if job['review_state'] == 'accepted':
                    review = db.execute('SELECT id, envelope_digest FROM reviews WHERE job_id=?', (nodes[d['input']]['job_id'],)).fetchone()
                    set_state(nid, 'succeeded', None, binding_json=json.dumps({'review_id': review['id'], 'envelope_digest': review['envelope_digest'], 'job_id': nodes[d['input']]['job_id']}))
                elif job['review_state'] == 'rejected':
                    set_state(nid, 'blocked', 'review rejected')
                continue
            status, reason = self._deps_satisfied(db, run_id, d, nodes)
            if status == 'blocked':
                set_state(nid, 'blocked', reason); continue
            if status == 'wait':
                set_state(nid, 'waiting_dependency', reason); continue
            # ready: start the node
            try:
                if d['type'] == 'dataset':
                    vid = d.get('dataset_version_id') or bindings.get(d.get('bind'))
                    v = self.datasets.version(db, owner, vid)
                    if v['deleted_at'] is not None:
                        set_state(nid, 'blocked', 'dataset payload deleted'); continue
                    set_state(nid, 'succeeded', None, binding_json=json.dumps({'dataset_version_id': vid, 'commitment': v['normalized_commitment']}))
                    add_edge(db, run['workspace'], 'dataset_version', vid, 'workflow_run', run_id, 'used_input')
                elif d['type'] in SERVICE_TYPES:
                    self._start_service_node(db, run, owner, d, nodes, bindings, drow)
                elif d['type'] == 'review_gate':
                    up_job = nodes[d['input']]['job_id']
                    state = db.execute('SELECT review_state FROM jobs WHERE id=?', (up_job,)).fetchone()['review_state']
                    if state == 'none':
                        self.reviews.request(db, owner, up_job)
                    set_state(nid, 'waiting_review', 'awaiting signed decision by the designated reviewer')
                elif d['type'] == 'export':
                    aid = self._export(db, run, owner, d, nodes)
                    set_state(nid, 'succeeded', None, output_artifact_id=aid)
            except ServiceError as exc:
                if exc.code == 'BUDGET_EXHAUSTED' and isinstance(exc.detail, dict) and exc.detail.get('retryable'):
                    set_state(nid, 'waiting_dependency', 'budget ' + exc.detail['explanation'])      # re-evaluated next tick
                else:
                    set_state(nid, 'blocked', exc.code + ': ' + (exc.detail.get('explanation') if isinstance(exc.detail, dict) and exc.detail.get('explanation') else
                                                                  (json.dumps(exc.detail) if isinstance(exc.detail, dict) else str(exc.detail or ''))))
        # derive run state
        states = [nodes[i]['state'] for i in order]
        outputs = definition['outputs']
        if all(nodes[o]['state'] == 'succeeded' for o in outputs):
            new = 'completed'
        elif cancel and all(s in ('succeeded', 'failed', 'cancelled', 'blocked') for s in states):
            new = 'cancelled'
        elif any(s == 'waiting_review' for s in states) and not any(s in ('queued', 'running') for s in states):
            new = 'waiting_review'
        elif all(s in ('succeeded', 'failed', 'cancelled', 'blocked') for s in states):
            if any(s == 'failed' for s in states):
                new = 'partially_failed' if any(s == 'succeeded' for s in states) else 'failed'
            elif any(s == 'blocked' for s in states):
                new = 'blocked'          # a dependency condition or review decision stopped a required deliverable
            else:
                new = 'partially_failed'
        elif any(s == 'blocked' for s in states) and not any(s in ('queued', 'running', 'waiting_review', 'pending', 'ready', 'waiting_dependency') for s in states):
            new = 'blocked'
        else:
            new = 'running'
        summary = {'deliverables': {o: nodes[o]['state'] == 'succeeded' for o in outputs},
                   'missing': [o for o in outputs if nodes[o]['state'] != 'succeeded'],
                   'node_states': {i: nodes[i]['state'] for i in order}}
        finished = now() if new in ('completed', 'cancelled', 'failed', 'partially_failed') else None
        db.execute('UPDATE workflow_runs SET state=?, summary_json=?, updated_at=?, finished_at=COALESCE(finished_at, ?) WHERE id=?',
                   (new, json.dumps(summary), now(), finished, run_id))
        if new != run['state']:
            history.record(db, run['workspace'], 'scheduler', 'job.result_committed' if new == 'completed' else ('job.cancelled' if new == 'cancelled' else 'job.retry_scheduled'),
                           'workflow_run', run_id, {'state': new, 'missing': summary['missing']})
        return {'run_id': run_id, 'state': new, 'summary': summary}

    def _state_only(self, db, run_id):
        run = self._run(db, run_id)
        return {'run_id': run_id, 'state': run['state'], 'summary': json.loads(run['summary_json']) if run['summary_json'] else None}

    def _export(self, db, run, owner, node_def, nodes):
        up = nodes[node_def['input']]
        job = db.execute('SELECT * FROM jobs WHERE id=?', (up['job_id'],)).fetchone()
        contract = db.execute('SELECT * FROM contracts WHERE id=?', (job['contract_id'],)).fetchone()
        review = db.execute('SELECT * FROM reviews WHERE job_id=?', (job['id'],)).fetchone()
        doc = merkle.parse(contract['contract_json'])
        pol = json.loads(contract['policy_json'])
        summary = json.loads(job['summary_json'] or '{}')
        available = {'model_id': doc['model_id'], 'verifier_id': doc['verifier_id'], 'verifier_digest': doc['verifier_digest'],
                     'contract_digest': contract['contract_digest'], 'evidence_root': job['evidence_root']}
        if pol['disclose_outcome'] and job['review_state'] == 'accepted':
            available['outcome'] = job['outcome']
            for f in ('horizon_seconds', 'safe_duration', 'selected_id', 'selected_ids', 'status'):
                if f in summary:
                    available[f] = summary[f]
        if review is not None:
            available.update(review_decision=review['decision'], review_key_id=review['key_id'], envelope_digest=review['envelope_digest'])
        projection = {k: available[k] for k in node_def['fields'] if k in available}
        omitted = [k for k in node_def['fields'] if k not in available]
        payload = {'schema': 'metacoin-workflow-export/v1', 'run_id': run['id'], 'node': node_def['id'], 'source_job': job['id'],
                   'fields': projection, 'omitted_by_policy': omitted,
                   'note': 'server-enforced projection; verifies bindings, not hidden computation'}
        aid = self.store.store(db, workspace=run['workspace'], kind='export', owner_id=owner.id, plaintext=merkle.canonical(payload),
                               recipients=[], intended_use='workflow-export-projection', job_id=job['id'], contract_id=contract['id'], public=True)
        add_edge(db, run['workspace'], 'job', job['id'], 'artifact', aid, 'produced')
        return aid

    def advance_all(self, db, limit=50):
        rows = db.execute("SELECT id FROM workflow_runs WHERE state IN ('created','running','waiting_review') ORDER BY updated_at LIMIT ?", (limit,)).fetchall()
        return [self.advance(db, r['id'])['state'] for r in rows]
