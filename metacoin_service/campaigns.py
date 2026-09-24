"""Scientific campaigns: bounded parameter grids, adaptive bisection, Pareto sets, refinements.

A campaign evaluates a bounded set of parameter choices against one immutable model kind and one
immutable base input (a dataset version + parameters, or inline inputs). Candidates are enumerated
deterministically (Cartesian product in axis order, values ascending as declared) and persisted
with their index BEFORE any job exists, so a restart resumes exactly the missing evaluations.

Parameter axes (exact integer transforms only):
  temporal_energy : capacity, reserve, initial_low, initial_high, load_scale_percent, harvest_scale_percent
  energy_audit    : available_low, available_high, reserve, load_scale_percent
  safe_runtime    : available_low, reserve, variable_power_high, duration_cap
Scaling by s percent widens conservatively: low bounds use floor(x*s/100), high bounds use ceil(x*s/100).

Adaptive bisection (min/max boundary of robust FEASIBLE) is allowed only along axes whose effect on
the FEASIBLE verdict is monotone for the implemented model with fixed absolute initial energy:
  capacity (more never hurts: saturation only rises), reserve (more never helps), load_scale_percent
  (more never helps), harvest_scale_percent (more never hurts), available_low/high (more never hurts).
duration_cap and initial_* are refused for bisection (feasibility/validity is not monotone in them).
INDETERMINATE counts as "not robustly feasible" for the search objective. Budget exhaustion yields an
unresolved bracket, never a fabricated optimum.
"""
import hashlib
import itertools
import json
import math
import secrets
from experiments.private_receipts import receipt as merkle
from . import history
from .auth import Principal
from .datasets import add_edge
from .db import now
from .errors import ServiceError

KINDS = ('temporal_energy', 'energy_audit', 'safe_runtime')
AXES = {'temporal_energy': ('capacity', 'reserve', 'initial_low', 'initial_high', 'load_scale_percent', 'harvest_scale_percent'),
        'energy_audit': ('available_low', 'available_high', 'reserve', 'load_scale_percent'),
        'safe_runtime': ('available_low', 'reserve', 'variable_power_high', 'duration_cap')}
MONOTONE = {  # axis -> 'up_better' (larger values never make FEASIBLE harder) | 'up_worse'
    'capacity': 'up_better', 'reserve': 'up_worse', 'load_scale_percent': 'up_worse', 'harvest_scale_percent': 'up_better',
    'available_low': 'up_better', 'available_high': 'up_better'}
LIMITS = {'max_axes': 3, 'max_values_per_axis': 64, 'max_evaluations': 256, 'max_inflight': 8, 'max_total_segments': 65_536, 'max_adaptive_evaluations': 24}
STATES = ('created', 'running', 'paused', 'cancelled', 'completed', 'budget_exhausted')
CAND_STATES = ('unevaluated', 'invalid', 'queued', 'running', 'succeeded', 'failed', 'cancelled')
RESULT_FIELDS = {'temporal_energy': ('outcome', 'min_reserve_margin_pessimistic', 'min_reserve_margin_optimistic', 'spill_bounds', 'horizon_seconds'),
                 'energy_audit': ('outcome', 'worst_margin', 'best_margin', 'additional_usable_energy', 'required_high'),
                 'safe_runtime': ('status', 'safe_duration', 'margin_at_duration', 'residual_energy_worst_case')}


def _scale(value, percent, up):
    return math.ceil(value * percent / 100) if up else value * percent // 100


def apply_params(kind, base, params):
    """Deterministic, exact application of axis values to a base input. Returns a new input dict."""
    out = json.loads(merkle.canonical(base))
    for path, value in params.items():
        if path not in AXES[kind]:
            raise ServiceError('VALIDATION', {'code': 'axis', 'path': path})
        if path == 'load_scale_percent':
            key_lo, key_hi = ('load_low', 'load_high') if kind == 'temporal_energy' else ('power_low', 'power_high')
            for seg in out['segments']:
                seg[key_lo], seg[key_hi] = _scale(seg[key_lo], value, False), _scale(seg[key_hi], value, True)
        elif path == 'harvest_scale_percent':
            for seg in out['segments']:
                seg['harvest_low'], seg['harvest_high'] = _scale(seg['harvest_low'], value, False), _scale(seg['harvest_high'], value, True)
        else:
            out[path] = value
    return out


def expand_axes(kind, axes):
    if type(axes) is not list or not 1 <= len(axes) <= LIMITS['max_axes']:
        raise ServiceError('VALIDATION', {'code': 'axes_count', 'max': LIMITS['max_axes']})
    expanded, seen = [], set()
    for ax in axes:
        if type(ax) is not dict or ax.get('path') not in AXES[kind] or ax['path'] in seen:
            raise ServiceError('VALIDATION', {'code': 'axis', 'allowed': list(AXES[kind])})
        seen.add(ax['path'])
        if 'values' in ax and set(ax) == {'path', 'values'}:
            values = ax['values']
            if type(values) is not list or not values or not all(type(v) is int and type(v) is not bool for v in values) or len(set(values)) != len(values):
                raise ServiceError('VALIDATION', {'code': 'axis_values', 'path': ax['path']})
            values = sorted(values)
        elif set(ax) == {'path', 'start', 'stop', 'step'} and all(type(ax[k]) is int for k in ('start', 'stop', 'step')) and ax['step'] > 0 and ax['start'] <= ax['stop']:
            values = list(range(ax['start'], ax['stop'] + 1, ax['step']))
        else:
            raise ServiceError('VALIDATION', {'code': 'axis_shape', 'path': ax.get('path')})
        if len(values) > LIMITS['max_values_per_axis'] or any(v < 0 or v > 2 ** 53 - 1 for v in values):
            raise ServiceError('VALIDATION', {'code': 'axis_values', 'path': ax['path'], 'max': LIMITS['max_values_per_axis']})
        expanded.append((ax['path'], values))
    return expanded


def enumerate_candidates(expanded):
    paths = [p for p, _ in expanded]
    for combo in itertools.product(*[v for _, v in expanded]):
        yield dict(zip(paths, combo))


class Campaigns:
    def __init__(self, contracts, jobs, datasets, store, settings):
        self.contracts, self.jobs, self.datasets, self.store, self.settings = contracts, jobs, datasets, store, settings

    # ---- definition and admission ----------------------------------------------------
    def _base_input(self, db, principal, kind, base):
        """Materialize the immutable base input (dataset version + parameters, or inline)."""
        if type(base) is dict and 'dataset_version_id' in base:
            rows, v = self.datasets.rows(db, principal, base['dataset_version_id'])
            if kind == 'temporal_energy' and v['kind'] == 'temporal_series':
                return self.datasets.temporal_input(rows, base.get('parameters', {}), v), v['id']
            if kind == 'energy_audit' and v['kind'] == 'energy_intervals':
                return self.datasets.interval_input(rows, base.get('parameters', {}), v), v['id']
            raise ServiceError('VALIDATION', 'dataset kind incompatible with campaign kind')
        from .contracts import validate_inputs
        validate_inputs(kind, base)
        return base, None

    def preview(self, db, principal, definition):
        principal.require('contract:create')
        merkle.canonical(definition)
        allowed = {'name', 'kind', 'base', 'axes', 'max_evaluations', 'policy', 'adaptive', 'tags'}
        if type(definition) is not dict or not {'name', 'kind', 'base'} <= set(definition) or not set(definition) <= allowed:
            raise ServiceError('VALIDATION', {'code': 'definition_fields', 'allowed': sorted(allowed)})
        kind = definition['kind']
        if kind not in KINDS or type(definition['name']) is not str or not 1 <= len(definition['name']) <= 128:
            raise ServiceError('VALIDATION', {'code': 'kind_or_name', 'kinds': list(KINDS)})
        base, dataset_version_id = self._base_input(db, principal, kind, definition['base'])
        adaptive = definition.get('adaptive')
        if adaptive is not None:
            self._validate_adaptive(kind, adaptive)
            total = min(adaptive['max_evaluations'], LIMITS['max_adaptive_evaluations'])
            expanded = []
        else:
            expanded = expand_axes(kind, definition.get('axes'))
            total = 1
            for _, values in expanded:
                total *= len(values)
        max_eval = definition.get('max_evaluations', LIMITS['max_evaluations'])
        if type(max_eval) is not int or not 1 <= max_eval <= LIMITS['max_evaluations']:
            raise ServiceError('VALIDATION', {'code': 'max_evaluations', 'max': LIMITS['max_evaluations']})
        segments = len(base.get('segments') or base.get('fixed_segments') or [])
        if total > max_eval:
            raise ServiceError('VALIDATION', {'code': 'too_many_evaluations', 'requested': total, 'max': max_eval,
                                              'suggestion': 'reduce axis values so the product is at most ' + str(max_eval)})
        if total * max(segments, 1) > LIMITS['max_total_segments']:
            raise ServiceError('VALIDATION', {'code': 'too_many_segments', 'total_segments': total * segments, 'max': LIMITS['max_total_segments']})
        amount = (definition.get('policy') or {}).get('amount', 1) if kind == 'energy_audit' else 0
        attempts = 1 + self.settings.limits['job_max_retries']
        estimate = {'basis': 'exact counts from the accepted definition and hard application limits; no measured runtimes',
                    'evaluations': total, 'segments_per_evaluation': segments, 'total_segments': total * segments,
                    'max_job_attempts': total * attempts, 'machine_time_upper_bound_seconds': total * attempts * self.settings.limits['job_timeout_seconds'],
                    'monetary_exposure_max': {'amount': amount * total, 'unit': 'campaign asset base units',
                                              'basis': 'energy-audit candidates carry one action entitlement each; campaigns never dispatch actions'},
                    'modeled_scientific_energy': 'per-candidate mJ results; not money, not measured electricity'}
        digest = hashlib.sha256(b'metacoin/campaign/v1\0' + merkle.canonical({'definition': definition, 'base_input': base})).hexdigest()
        return {'kind': kind, 'base': base, 'dataset_version_id': dataset_version_id, 'expanded': expanded, 'total': total,
                'estimate': estimate, 'digest': digest, 'definition': definition}

    @staticmethod
    def _validate_adaptive(kind, adaptive):
        allowed = {'objective', 'axis', 'lo', 'hi', 'max_evaluations'}
        if type(adaptive) is not dict or set(adaptive) != allowed or adaptive['objective'] not in ('min_feasible', 'max_feasible'):
            raise ServiceError('VALIDATION', {'code': 'adaptive_fields', 'allowed': sorted(allowed), 'objectives': ['min_feasible', 'max_feasible']})
        if adaptive['axis'] not in AXES[kind]:
            raise ServiceError('VALIDATION', {'code': 'axis', 'allowed': list(AXES[kind])})
        if adaptive['axis'] not in MONOTONE:
            raise ServiceError('VALIDATION', {'code': 'not_monotone', 'axis': adaptive['axis'],
                                              'reason': 'feasibility is not monotone in this axis for the implemented model; use a bounded grid'})
        want = 'up_better' if adaptive['objective'] == 'min_feasible' else 'up_worse'
        if MONOTONE[adaptive['axis']] != want:
            raise ServiceError('VALIDATION', {'code': 'objective_direction', 'axis': adaptive['axis'], 'monotone': MONOTONE[adaptive['axis']],
                                              'reason': 'min_feasible needs an axis where larger is never worse; max_feasible needs larger never better'})
        if not all(type(adaptive[k]) is int for k in ('lo', 'hi', 'max_evaluations')) or adaptive['lo'] > adaptive['hi'] or adaptive['lo'] < 0 \
                or not 3 <= adaptive['max_evaluations'] <= LIMITS['max_adaptive_evaluations']:
            raise ServiceError('VALIDATION', {'code': 'adaptive_bounds', 'max_evaluations': LIMITS['max_adaptive_evaluations']})

    def create(self, db, principal, definition):
        pv = self.preview(db, principal, definition)
        from .agents import guard
        adaptive_def = definition.get('adaptive')
        guard(db, principal, 'campaign:run', workflows=1, jobs=pv['total'] if adaptive_def is None else min(adaptive_def['max_evaluations'], LIMITS['max_adaptive_evaluations']))
        cid = 'cmp_' + secrets.token_hex(8)
        base_id = self.store.store(db, workspace=principal.workspace, kind='draft_input', owner_id=principal.id, plaintext=merkle.canonical(pv['base']),
                                   recipients=[], intended_use='campaign-base-input;owner-worker')
        adaptive = definition.get('adaptive')
        db.execute('INSERT INTO sci_campaigns VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)',
                   (cid, principal.workspace, principal.id, definition['name'], pv['kind'], pv['dataset_version_id'], base_id,
                    merkle.canonical(definition).decode(), pv['digest'], 'created', pv['total'],
                    json.dumps({'lo': adaptive['lo'], 'hi': adaptive['hi'], 'evaluated': [], 'stopping_reason': None, 'boundary': None,
                                'phase': 'check_first', 'budget_left': min(adaptive['max_evaluations'], LIMITS['max_adaptive_evaluations'])}) if adaptive else None,
                    json.dumps(pv['estimate']), now(), now()))
        if adaptive is None:
            for index, params in enumerate(enumerate_candidates(pv['expanded'])):
                db.execute('INSERT INTO sci_campaign_candidates (campaign_id, idx, params_json, state, updated_at) VALUES (?,?,?,?,?)',
                           (cid, index, json.dumps(params, sort_keys=True), 'unevaluated', now()))
        history.record(db, principal.workspace, principal.id, 'job.queued', 'campaign', cid, {'digest': pv['digest'], 'total': pv['total'], 'kind': pv['kind']})
        if pv['dataset_version_id']:
            add_edge(db, principal.workspace, 'dataset_version', pv['dataset_version_id'], 'campaign', cid, 'used_input')
        return {'campaign_id': cid, 'digest': pv['digest'], 'total_candidates': pv['total'], 'estimate': pv['estimate']}

    # ---- state and control -----------------------------------------------------------------
    def _campaign(self, db, campaign_id, workspace=None):
        row = db.execute('SELECT * FROM sci_campaigns WHERE id=?' + (' AND workspace=?' if workspace else ''), (campaign_id,) + ((workspace,) if workspace else ())).fetchone()
        if row is None:
            raise ServiceError('NOT_FOUND', 'campaign')
        return row

    def control(self, db, principal, campaign_id, action):
        principal.require('job:submit' if action in ('run', 'resume') else 'job:cancel')
        c = self._campaign(db, campaign_id, principal.workspace)
        if c['owner_id'] != principal.id:
            raise ServiceError('FORBIDDEN', 'campaign')
        transitions = {'run': ('created', 'running'), 'pause': ('running', 'paused'), 'resume': ('paused', 'running'), 'cancel': (None, 'cancelled')}
        frm, to = transitions[action]
        if c['state'] in ('completed', 'cancelled', 'budget_exhausted'):
            raise ServiceError('CONFLICT', 'campaign is terminal')
        if frm is not None and c['state'] != frm:
            raise ServiceError('CONFLICT', 'campaign is ' + c['state'] + ', cannot ' + action)
        db.execute('UPDATE sci_campaigns SET state=?, updated_at=? WHERE id=?', (to, now(), campaign_id))
        if to == 'cancelled':
            for cand in db.execute("SELECT idx, job_id, state FROM sci_campaign_candidates WHERE campaign_id=? AND state IN ('unevaluated','queued','running')", (campaign_id,)).fetchall():
                if cand['job_id']:
                    try:
                        self.jobs.cancel(db, principal, cand['job_id'])
                    except ServiceError:
                        pass
                db.execute("UPDATE sci_campaign_candidates SET state='cancelled', updated_at=? WHERE campaign_id=? AND idx=?", (now(), campaign_id, cand['idx']))
            db.execute('UPDATE sci_campaigns SET finished_at=? WHERE id=?', (now(), campaign_id))
        history.record(db, principal.workspace, principal.id, 'job.cancelled' if to == 'cancelled' else 'job.retry_scheduled', 'campaign', campaign_id, {'action': action, 'state': to})
        if to == 'running':
            self.tick(db, campaign_id)
        return self.view(db, principal, campaign_id)

    def view(self, db, principal, campaign_id):
        principal.require('job:read')
        c = self._campaign(db, campaign_id, principal.workspace)
        counts = {r['state']: r['n'] for r in db.execute('SELECT state, COUNT(*) AS n FROM sci_campaign_candidates WHERE campaign_id=? GROUP BY state', (campaign_id,))}
        outcomes = {r['outcome']: r['n'] for r in db.execute("SELECT outcome, COUNT(*) AS n FROM sci_campaign_candidates WHERE campaign_id=? AND state='succeeded' GROUP BY outcome", (campaign_id,))}
        done = sum(counts.get(s, 0) for s in ('succeeded', 'failed', 'invalid', 'cancelled'))
        out = {'campaign_id': campaign_id, 'name': c['name'], 'kind': c['kind'], 'state': c['state'], 'digest': c['digest'],
               'dataset_version_id': c['dataset_version_id'], 'total_candidates': c['total_candidates'], 'done': done,
               'progress': {'denominator': c['total_candidates'], 'numerator': done}, 'by_state': counts, 'by_outcome': outcomes,
               'estimate': json.loads(c['estimate_json']), 'created_at': c['created_at'], 'updated_at': c['updated_at'], 'finished_at': c['finished_at'],
               'definition': merkle.parse(c['definition_json']) if principal.can('job:read_private') else None}
        if c['adaptive_json']:
            out['adaptive'] = json.loads(c['adaptive_json'])
        return out

    def results(self, db, principal, campaign_id, limit=500):
        principal.require('job:read')
        c = self._campaign(db, campaign_id, principal.workspace)
        private = principal.can('job:read_private')
        rows = db.execute('SELECT * FROM sci_campaign_candidates WHERE campaign_id=? ORDER BY idx LIMIT ?', (campaign_id, min(int(limit), 1000))).fetchall()
        table = []
        for r in rows:
            item = {'index': r['idx'], 'params': json.loads(r['params_json']), 'state': r['state'], 'job_id': r['job_id']}
            if private and r['summary_json']:
                summary = json.loads(r['summary_json'])
                item['result'] = {k: summary.get(k) for k in RESULT_FIELDS[c['kind']]}
                item['outcome'] = r['outcome']
            elif r['state'] == 'succeeded':
                item['outcome'] = 'withheld' if not private else r['outcome']
            if r['state'] == 'invalid':
                item['reason'] = r['outcome']            # refusal code, never a value
            table.append(item)
        return {'campaign_id': campaign_id, 'kind': c['kind'], 'model_version': {'temporal_energy': 'temporal-energy/v1', 'energy_audit': 'outage-energy-bounds/v0', 'safe_runtime': 'safe-runtime/v1'}[c['kind']],
                'rows': table, 'legend': {'unevaluated': 'not yet run', 'invalid': 'outside the model domain', 'failed': 'execution failed', 'cancelled': 'cancelled',
                                          'succeeded': 'result available; see outcome'}, 'note': 'missing values are absent, never zero'}

    def results_csv(self, db, principal, campaign_id):
        """Safe CSV: text cells beginning with = + - @ are prefixed with an apostrophe (export policy); numbers unchanged."""
        data = self.results(db, principal, campaign_id)
        axes = sorted({k for r in data['rows'] for k in r['params']})
        fields = list(RESULT_FIELDS[data['kind']])
        def safe(v):
            s = '' if v is None else str(v)
            return ("'" + s) if s[:1] in ('=', '+', '-', '@') and not s.lstrip('-').isdigit() else s
        lines = [','.join(['index'] + axes + ['state', 'outcome'] + fields + ['model_version'])]
        for r in data['rows']:
            res = r.get('result') or {}
            lines.append(','.join([str(r['index'])] + [str(r['params'].get(a, '')) for a in axes] + [r['state'], safe(r.get('outcome'))]
                                  + [safe(json.dumps(res[f]) if isinstance(res.get(f), list) else res.get(f)) for f in fields] + [data['model_version']]))
        return '\n'.join(lines) + '\n'

    # ---- execution tick ----------------------------------------------------------------------
    def _owner(self, db, c):
        row = db.execute('SELECT * FROM principals WHERE id=?', (c['owner_id'],)).fetchone()
        if row is None or row['revoked_at'] is not None:
            raise ServiceError('FORBIDDEN', 'campaign owner revoked')
        return Principal(row)

    def _dispatch(self, db, c, owner, base, params, index):
        definition = merkle.parse(c['definition_json'])
        try:
            inputs = apply_params(c['kind'], base, params)
            if c['kind'] == 'temporal_energy' and (inputs['initial_high'] > inputs['capacity'] or inputs['reserve'] > inputs['capacity']):
                raise ServiceError('MODEL_DOMAIN', 'initial energy or reserve exceeds capacity')
            from .contracts import validate_inputs
            validate_inputs(c['kind'], inputs)
        except Exception as exc:
            code = getattr(exc, 'code', 'MODEL_DOMAIN')
            db.execute("UPDATE sci_campaign_candidates SET state='invalid', outcome=?, updated_at=? WHERE campaign_id=? AND idx=?", (code, now(), c['id'], index))
            return 'invalid'
        policy = dict(definition.get('policy') or {})
        policy.setdefault('reviewer_id', db.execute("SELECT id FROM principals WHERE workspace=? AND role='reviewer' AND revoked_at IS NULL ORDER BY created_at LIMIT 1", (c['workspace'],)).fetchone()['id'])
        contract_id = self.contracts.create_draft(db, owner, kind=c['kind'], title=c['name'] + '#' + str(index), inputs=inputs, policy=policy)
        self.contracts.freeze(db, owner, contract_id)
        job_id = self.jobs.submit(db, owner, contract_id)
        db.execute("UPDATE sci_campaign_candidates SET state='queued', contract_id=?, job_id=?, updated_at=? WHERE campaign_id=? AND idx=?", (contract_id, job_id, now(), c['id'], index))
        add_edge(db, c['workspace'], 'campaign', c['id'], 'job', job_id, 'produced')
        return 'queued'

    def tick(self, db, campaign_id):
        """Poll in-flight candidates, dispatch new ones up to the concurrency limit, advance adaptive search."""
        c = self._campaign(db, campaign_id)
        if c['state'] not in ('running',):
            return c['state']
        owner = self._owner(db, c)
        base = self.store.load_json(db, c['base_artifact_id'], c['workspace'])
        # 1. poll
        for cand in db.execute("SELECT idx, job_id FROM sci_campaign_candidates WHERE campaign_id=? AND state IN ('queued','running')", (campaign_id,)).fetchall():
            job = db.execute('SELECT state, outcome, summary_json FROM jobs WHERE id=?', (cand['job_id'],)).fetchone()
            if job['state'] == 'succeeded':
                db.execute("UPDATE sci_campaign_candidates SET state='succeeded', outcome=?, summary_json=?, updated_at=? WHERE campaign_id=? AND idx=?",
                           (job['outcome'], job['summary_json'], now(), campaign_id, cand['idx']))
            elif job['state'] in ('failed', 'cancelled'):
                db.execute("UPDATE sci_campaign_candidates SET state=?, updated_at=? WHERE campaign_id=? AND idx=?", (job['state'], now(), campaign_id, cand['idx']))
            elif job['state'] == 'running':
                db.execute("UPDATE sci_campaign_candidates SET state='running', updated_at=? WHERE campaign_id=? AND idx=?", (now(), campaign_id, cand['idx']))
        # 2. adaptive step or grid dispatch
        if c['adaptive_json']:
            self._adaptive_step(db, c, owner, base)
        else:
            inflight = db.execute("SELECT COUNT(*) FROM sci_campaign_candidates WHERE campaign_id=? AND state IN ('queued','running')", (campaign_id,)).fetchone()[0]
            room = LIMITS['max_inflight'] - inflight
            if room > 0:
                for cand in db.execute("SELECT idx, params_json FROM sci_campaign_candidates WHERE campaign_id=? AND state='unevaluated' ORDER BY idx LIMIT ?", (campaign_id, room)).fetchall():
                    self._dispatch(db, c, owner, base, json.loads(cand['params_json']), cand['idx'])
        # 3. completion
        remaining = db.execute("SELECT COUNT(*) FROM sci_campaign_candidates WHERE campaign_id=? AND state IN ('unevaluated','queued','running')", (campaign_id,)).fetchone()[0]
        c = self._campaign(db, campaign_id)
        adaptive = json.loads(c['adaptive_json']) if c['adaptive_json'] else None
        if remaining == 0 and (adaptive is None or adaptive['stopping_reason']):
            final = 'budget_exhausted' if adaptive and adaptive['stopping_reason'] == 'evaluation_budget_exhausted' else 'completed'
            db.execute('UPDATE sci_campaigns SET state=?, updated_at=?, finished_at=? WHERE id=?', (final, now(), now(), campaign_id))
            history.record(db, c['workspace'], 'scheduler', 'job.result_committed', 'campaign', campaign_id, {'state': final})
            return final
        db.execute('UPDATE sci_campaigns SET updated_at=? WHERE id=?', (now(), campaign_id))
        return 'running'

    def _adaptive_step(self, db, c, owner, base):
        """Bisection with an explicit bracket. For max_feasible (axis up_worse) the feasible set is a prefix
        [domain_lo, boundary]; for min_feasible (axis up_better) it is a suffix [boundary, domain_hi].
        State: lo/hi = current bracket, phase check_first -> check_second -> bisect, evaluated = decisions."""
        state = json.loads(c['adaptive_json'])
        definition = merkle.parse(c['definition_json'])
        axis, objective = definition['adaptive']['axis'], definition['adaptive']['objective']
        if state['stopping_reason']:
            return
        if db.execute("SELECT 1 FROM sci_campaign_candidates WHERE campaign_id=? AND state IN ('unevaluated','queued','running')", (c['id'],)).fetchone():
            return                                            # exactly one evaluation in flight
        maxf = objective == 'max_feasible'
        last = db.execute("SELECT idx, state, outcome, params_json FROM sci_campaign_candidates WHERE campaign_id=? ORDER BY idx DESC LIMIT 1", (c['id'],)).fetchone()
        if last is not None and len(state['evaluated']) < last['idx'] + 1:
            value = json.loads(last['params_json'])[axis]
            ok = last['state'] == 'succeeded' and last['outcome'] == 'FEASIBLE'
            state['evaluated'].append({'index': last['idx'], 'value': value, 'outcome': last['outcome'] if last['state'] == 'succeeded' else last['state'], 'robust_feasible': ok})
            if last['state'] != 'succeeded':
                state['stopping_reason'] = 'evaluation_failed_or_invalid'
            elif state['phase'] == 'check_first':          # max: lo must be feasible; min: hi must be feasible
                if ok:
                    state['phase'] = 'check_second'
                else:
                    state['stopping_reason'] = 'no_feasible_point_in_domain'
            elif state['phase'] == 'check_second':         # max: hi; min: lo
                if ok:
                    state['boundary'] = state['hi'] if maxf else state['lo']
                    state['stopping_reason'] = 'boundary_at_domain_end' if maxf else 'boundary_at_domain_start'
                else:
                    state['phase'] = 'bisect'              # invariant: max -> lo feasible, hi infeasible; min -> lo infeasible, hi feasible
            else:
                if maxf:
                    if ok: state['lo'] = value
                    else: state['hi'] = value
                else:
                    if ok: state['hi'] = value
                    else: state['lo'] = value
        if state['stopping_reason'] is None:
            if state['phase'] == 'bisect' and state['hi'] - state['lo'] <= 1:
                state['boundary'] = state['lo'] if maxf else state['hi']
                state['stopping_reason'] = 'bracket_closed'
            elif state['budget_left'] <= 0:
                state['stopping_reason'] = 'evaluation_budget_exhausted'
            else:
                if state['phase'] == 'check_first':
                    value = state['lo'] if maxf else state['hi']
                elif state['phase'] == 'check_second':
                    value = state['hi'] if maxf else state['lo']
                else:
                    value = (state['lo'] + state['hi']) // 2
                index = db.execute('SELECT COALESCE(MAX(idx),-1)+1 FROM sci_campaign_candidates WHERE campaign_id=?', (c['id'],)).fetchone()[0]
                db.execute('INSERT INTO sci_campaign_candidates (campaign_id, idx, params_json, state, updated_at) VALUES (?,?,?,?,?)',
                           (c['id'], index, json.dumps({axis: value}), 'unevaluated', now()))
                self._dispatch(db, c, owner, base, {axis: value}, index)
                state['budget_left'] -= 1
        state['remaining_bracket'] = [state['lo'], state['hi']]
        db.execute('UPDATE sci_campaigns SET adaptive_json=?, total_candidates=(SELECT COUNT(*) FROM sci_campaign_candidates WHERE campaign_id=?), updated_at=? WHERE id=?',
                   (json.dumps(state), c['id'], now(), c['id']))

    def tick_all(self, db, limit=20):
        return [self.tick(db, r['id']) for r in db.execute("SELECT id FROM sci_campaigns WHERE state='running' ORDER BY updated_at LIMIT ?", (limit,)).fetchall()]

    # ---- Pareto ---------------------------------------------------------------------------------
    def pareto(self, db, principal, campaign_id, objectives, require_outcomes=('FEASIBLE',)):
        """Conservative interval dominance over succeeded candidates. objectives: [{field, direction}]
        where field is an axis value or a result field; interval fields ([lo, hi]) compare by worst/best ends."""
        principal.require('job:read_private')
        c = self._campaign(db, campaign_id, principal.workspace)
        if type(objectives) is not list or not 1 <= len(objectives) <= 4:
            raise ServiceError('VALIDATION', 'objectives (1-4)')
        for o in objectives:
            if type(o) is not dict or set(o) != {'field', 'direction'} or o['direction'] not in ('min', 'max') or type(o['field']) is not str:
                raise ServiceError('VALIDATION', 'objective shape {field, direction}')
        rows = db.execute("SELECT * FROM sci_campaign_candidates WHERE campaign_id=? AND state='succeeded' ORDER BY idx", (campaign_id,)).fetchall()
        cands, excluded = [], []
        for r in rows:
            if r['outcome'] not in require_outcomes:
                excluded.append({'index': r['idx'], 'reason': 'outcome ' + str(r['outcome']) + ' not in required ' + ','.join(require_outcomes)}); continue
            params, summary = json.loads(r['params_json']), json.loads(r['summary_json'] or '{}')
            vals, missing = [], []
            for o in objectives:
                v = params.get(o['field'], summary.get(o['field']))
                if v is None:
                    missing.append(o['field']); continue
                if type(v) is list and len(v) == 2 and all(type(x) is int for x in v):
                    lo, hi = v
                elif type(v) is int:
                    lo = hi = v
                else:
                    missing.append(o['field']); continue
                vals.append((lo, hi))
            if missing:
                excluded.append({'index': r['idx'], 'reason': 'missing objective(s): ' + ','.join(missing)}); continue
            cands.append({'index': r['idx'], 'params': params, 'values': vals})
        def dominates(a, b):
            better_somewhere = False
            for (alo, ahi), (blo, bhi), o in zip(a['values'], b['values'], objectives):
                if o['direction'] == 'min':
                    if ahi > blo: return False           # a's worst must be <= b's best
                    if ahi < blo: better_somewhere = True
                else:
                    if alo < bhi: return False
                    if alo > bhi: better_somewhere = True
            return better_somewhere
        front, dominated = [], []
        for a in cands:
            witness = next((b for b in cands if b is not a and dominates(b, a)), None)
            if witness is None:
                front.append(a)
            else:
                dominated.append({'index': a['index'], 'reason': 'dominated', 'dominated_by': witness['index']})
        return {'campaign_id': campaign_id, 'objectives': objectives, 'required_outcomes': list(require_outcomes),
                'non_dominated': [{'index': a['index'], 'params': a['params'], 'values': a['values']} for a in front],
                'excluded': excluded + dominated, 'rule': 'conservative interval dominance: for min, dominator worst <= other best on every objective, strictly better on one; '
                'for max, dominator best-worst >= other best; scalars are zero-width intervals',
                'note': 'a Pareto set does not identify a uniquely best choice; no cross-asset valuation is performed'}

    def plot_svg(self, db, principal, campaign_id):
        """Outcome grid over the first two axes from persisted results (accessible table is the primary view)."""
        data = self.results(db, principal, campaign_id)
        axes = sorted({k for r in data['rows'] for k in r['params']})[:2]
        if len(axes) < 1:
            return '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"></svg>'
        xs = sorted({r['params'][axes[0]] for r in data['rows']})
        ys = sorted({r['params'][axes[1]] for r in data['rows']}) if len(axes) > 1 else [0]
        cell, pad = 22, 60
        w, h = pad + cell * len(xs) + 10, pad + cell * len(ys) + 10
        colors = {'FEASIBLE': '#1c6b3a', 'INDETERMINATE': '#8a5a00', 'INFEASIBLE': '#9b1c1c', 'ROBUSTLY_FEASIBLE': '#1c6b3a', 'CAPPED': '#8a5a00',
                  'CAPPED_MODEL_UNBOUNDED': '#8a5a00', 'BASE_PLAN_INFEASIBLE': '#9b1c1c'}
        parts = ['<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" role="img" aria-label="campaign outcome grid over %s">' % (w, h, ' and '.join(axes))]
        for r in data['rows']:
            xi = xs.index(r['params'][axes[0]]); yi = ys.index(r['params'][axes[1]]) if len(axes) > 1 else 0
            state = r['state']; outcome = r.get('outcome')
            fill = colors.get(outcome, '#ffffff') if state == 'succeeded' else {'invalid': '#cccccc', 'failed': '#333333', 'cancelled': '#999999'}.get(state, '#f4f4f4')
            label = outcome if state == 'succeeded' else state
            parts.append('<rect x="%d" y="%d" width="%d" height="%d" fill="%s" stroke="#d8dcd6"><title>%s=%s %s: %s</title></rect>' %
                         (pad + xi * cell, pad + yi * cell, cell - 1, cell - 1, fill, axes[0], r['params'][axes[0]], (axes[1] + '=' + str(r['params'][axes[1]])) if len(axes) > 1 else '', label))
        parts.append('<text x="%d" y="%d" font-size="11">%s (%d values)</text>' % (pad, 20, axes[0], len(xs)))
        if len(axes) > 1:
            parts.append('<text x="4" y="%d" font-size="11">%s (%d values)</text>' % (40, axes[1], len(ys)))
        parts.append('<text x="%d" y="%d" font-size="10">green feasible, amber indeterminate/capped, red infeasible, grey invalid/unevaluated, black failed</text>' % (4, h - 2))
        parts.append('</svg>')
        return ''.join(parts)

    # ---- refinement what-if as an immutable derived result ----------------------------------------
    def refine_job(self, db, principal, job_id, refinements):
        principal.require('job:read_private')
        job = self.jobs.get(db, principal, job_id)
        if job['kind'] != 'temporal_energy' or job['state'] != 'succeeded':
            raise ServiceError('CONFLICT', 'refinement applies to a succeeded temporal_energy job')
        contract = db.execute('SELECT * FROM contracts WHERE id=?', (job['contract_id'],)).fetchone()
        vault = self.store.load_json(db, contract['input_artifact_id'], principal.workspace)
        inputs = {f['name']: f['value'] for f in vault['fields']}['inputs']
        from . import temporal
        result = temporal.refine(inputs, refinements)
        payload = {'schema': 'temporal-refinement/v1', 'source_job': job_id, 'source_evidence_root': job['evidence_root'], 'refinements': refinements, 'result': result}
        aid = self.store.store(db, workspace=principal.workspace, kind='evidence_vault', owner_id=principal.id, plaintext=merkle.canonical(payload),
                               recipients=[], intended_use='refinement-what-if;private', job_id=job_id, contract_id=contract['id'])
        add_edge(db, principal.workspace, 'job', job_id, 'artifact', aid, 'derived_from')
        history.record(db, principal.workspace, principal.id, 'job.result_committed', 'artifact', aid, {'refinement_of': job_id, 'hypothetical': True})
        return dict(result, artifact_id=aid, source_job=job_id)
