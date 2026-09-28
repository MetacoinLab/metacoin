"""Group F: reusable scientific workflow packages.

A package is a versioned, content-addressed manifest (`metacoin-workflow-package/v1`) around a workflow definition whose
service nodes are trusted registered operations only. It declares accepted input schemas and units, operation versions
(manifest ids and implementation digests), required model identities, the verification policy that gates delivery,
resource bounds, output fields, disclosure defaults and a synthetic public example. Descriptions and examples are data.
Installing a package stores the manifest; nothing runs, nothing is fetched, no executable is registered.

Compatibility negotiation compares the manifest with this workspace (installed kinds and implementation digests, live
worker devices, model registry, schema versions, the caller's grant) and reports per requirement: supported_as_requested,
supported_via_declared_equivalent, missing_optional_enhancement or blocked_required_dependency. It reserves nothing.

A composite quote binds package digest, instantiated definition digest, per-node input digests, pricing revision, scheme and
expiry, and totals per-node service quotes (fixed = exact, metered = upto). A package run executes the instantiated
definition through the existing workflow engine under a run budget ceiling equal to the quote total, or, for a
single-operation package paid through the existing x402 upto route, as that metered job. Delivery is gated: the run's
results and (for metered jobs) the final charge are withheld until the required verification class passes; on failure the
result and evidence stay private, the run is `unaccepted`, and the disclosed failure charge policy applies. Signed result
bundles carry the selected projection with a manifest, digests and the service signature, checkable offline by
`metacoin_service/verify_bundle.py`."""
import base64
import hashlib
import io
import json
import re
import secrets
import zipfile

from experiments.private_receipts import receipt as merkle
from . import crypto, history
from .datasets import add_edge
from .db import now
from .errors import ServiceError

SCHEMA = 'metacoin-workflow-package/v1'
QUOTE_SCHEMA = 'metacoin-package-quote/v1'
BUNDLE_SCHEMA = 'metacoin-result-bundle/v1'
COMPAT = ('supported_as_requested', 'supported_via_declared_equivalent', 'missing_optional_enhancement', 'blocked_required_dependency')
LIMITS = {'max_name': 64, 'max_description': 2000, 'max_example_bytes': 64_000, 'max_manifest_bytes': 256_000, 'quote_ttl_seconds': 3600, 'bundle_max_bytes': 32 * 1024 * 1024, 'bundle_max_members': 256}
DELIVERY_GATES = ('required_verification', 'none')
FAILURE_CHARGE = ('none', 'measured')
SAFE_NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')
# cross-backend equivalence under the operation's own reproducibility contract (compute manifests): exact-integer kernels are
# backend independent; heat is equivalent within its declared tolerance; models and units are never automatically equivalent
BACKEND_EQUIVALENT = {'temporal_batch': 'exact (bit-identical integer results on any backend)', 'monte_carlo_reliability': 'exact (identical event counts on any backend)', 'heat_diffusion': 'within the manifest tolerance (float64 FTCS; replica verification available)',
                      'resource_plan': 'cpu only (HiGHS); no device choice', 'calibration_fit': 'cpu only'}


def _digest(obj):
    return hashlib.sha256(merkle.canonical(obj)).hexdigest()


class Packages:
    def __init__(self, settings, services):
        self.settings, self.svc = settings, services

    # ---- manifests ------------------------------------------------------------------------------------------------------
    def _operation_identities(self, kinds):
        from .compute import manifests as compute_manifests
        from .models import engine as model_engine
        from .catalog import INSTALLED, verifier_digest
        out = {}
        for k in sorted(kinds):
            if k not in INSTALLED:
                raise ServiceError('VALIDATION', {'code': 'unknown_operation', 'kind': k})
            entry = {'model_id': INSTALLED[k]['model_id'], 'result_schema': INSTALLED[k]['result_schema'], 'verifier_digest': verifier_digest(k)}
            if k in compute_manifests.KINDS:
                m = compute_manifests.MANIFESTS[k]
                entry.update(manifest_id=m['manifest_id'], manifest_version=m['version'], input_schema=m['input_schema'], devices=m['devices'], precision=m['precision'], backend_equivalence=BACKEND_EQUIVALENT.get(k))
            elif k in model_engine.KINDS or k in model_engine.KNOWLEDGE_KINDS:
                entry.update(input_schema=(INSTALLED[k].get('input_schema') or {}).get('schema'))
            else:
                entry.update(input_schema=(INSTALLED[k].get('input_schema') or {}).get('schema'))
            out[k] = entry
        return out

    def build_manifest(self, db, principal, body):
        """A manifest from a workflow definition in this workspace plus declarations. Inline node inputs are NOT copied into
        the package (a package is reusable structure): service nodes keep parameter slots and a synthetic example instead."""
        from . import workflows as wf_mod
        if type(body) is not dict:
            raise ServiceError('VALIDATION', 'body')
        name = body.get('name')
        if type(name) is not str or not SAFE_NAME.match(name):
            raise ServiceError('VALIDATION', {'code': 'name', 'pattern': SAFE_NAME.pattern})
        wid = body.get('workflow_id')
        drow = self.svc.workflows.get_definition(db, principal, wid)
        definition = merkle.parse(drow['definition_json'])
        include_inputs = bool(body.get('include_inline_inputs', False))
        structure = json.loads(json.dumps(definition))
        stripped = []
        for n in structure['nodes']:
            if n['type'] in wf_mod.SERVICE_TYPES and 'inputs' in n and not include_inputs:
                n['inputs_digest'] = _digest(n['inputs']); n.pop('inputs'); stripped.append(n['id'])
        kinds = {n['type'] for n in definition['nodes'] if n['type'] in wf_mod.SERVICE_TYPES}
        ops = self._operation_identities(kinds)
        models = []
        for r in db.execute('SELECT model_id, revision, weight_digest, tokenizer_digest, license FROM model_revisions WHERE id IN (%s)' % ','.join('?' * len(body.get('required_models') or [])), tuple(body.get('required_models') or [])).fetchall() if body.get('required_models') else []:
            models.append({'model_id': r['model_id'], 'revision': r['revision'], 'weight_digest': r['weight_digest'], 'tokenizer_digest': r['tokenizer_digest'], 'license': r['license']})
        if body.get('required_models') and len(models) != len(set(body['required_models'])):
            raise ServiceError('NOT_FOUND', 'required model revision')
        gate = body.get('delivery_policy') or {}
        if type(gate) is not dict or set(gate) - {'gate', 'required_class', 'metered_failure_charge'} or gate.get('gate', 'required_verification') not in DELIVERY_GATES or gate.get('metered_failure_charge', 'none') not in FAILURE_CHARGE:
            raise ServiceError('VALIDATION', {'code': 'delivery_policy', 'gate': list(DELIVERY_GATES), 'metered_failure_charge': list(FAILURE_CHARGE)})
        from .verification import CLASSES, SUPPORT
        req_cls = gate.get('required_class', 'full_reference')
        if gate.get('gate', 'required_verification') == 'required_verification':
            if req_cls not in CLASSES:
                raise ServiceError('VALIDATION', {'code': 'required_class', 'allowed': list(CLASSES)})
            unsupported = [k for k in kinds if req_cls not in SUPPORT.get(k, {})]
            if unsupported:
                raise ServiceError('VALIDATION', {'code': 'verification_class_unsupported_for_operations', 'class': req_cls, 'kinds': sorted(unsupported), 'hint': 'choose a class every operation supports or gate=none'})
        example = body.get('example') or {}
        if type(example) is not dict or len(json.dumps(example)) > LIMITS['max_example_bytes']:
            raise ServiceError('VALIDATION', {'code': 'example', 'max_bytes': LIMITS['max_example_bytes']})
        def strip_private(o):
            if isinstance(o, dict):
                return {k: strip_private(v) for k, v in o.items() if k != 'private_label'}
            if isinstance(o, list):
                return [strip_private(v) for v in o]
            return o
        example = strip_private(example)                   # examples are public, synthetic data: private labels never travel
        desc = body.get('description', '')
        if type(desc) is not str or len(desc) > LIMITS['max_description']:
            raise ServiceError('VALIDATION', 'description')
        units = body.get('units') or {'energy': 'mJ', 'power': 'mW', 'time': 's'}
        disclosure = body.get('disclosure_defaults') or {'disclose_inputs': False, 'disclose_witness': True, 'summary_fields': None}
        if type(disclosure) is not dict or set(disclosure) - {'disclose_inputs', 'disclose_witness', 'summary_fields'}:
            raise ServiceError('VALIDATION', 'disclosure_defaults: {disclose_inputs, disclose_witness, summary_fields}')
        est = wf_mod.estimate(definition, self.settings.limits)
        manifest = {'schema': SCHEMA, 'name': name, 'description': desc, 'workflow': structure, 'workflow_source_digest': drow['digest'], 'stripped_inline_inputs': stripped,
                    'input_schemas': {n['id']: (ops[n['type']]['input_schema'] if n['type'] in ops else ('dataset:' + n.get('kind', 'temporal_series') if n['type'] == 'dataset' else None)) for n in definition['nodes']},
                    'slots': definition.get('slots', {}), 'units': units, 'operations': ops, 'required_models': models,
                    'verification_policy': {'gate': gate.get('gate', 'required_verification'), 'required_class': req_cls if gate.get('gate', 'required_verification') == 'required_verification' else None, 'metered_failure_charge': gate.get('metered_failure_charge', 'none'),
                                            'fixed_price_failure': 'an exact (fixed-price) payment settles before computation: on verification failure the result is marked unaccepted and kept private; the application does not refund (no escrow)',
                                            'metered_failure': {'none': 'an upto authorization is left unused (no transfer) when verification fails', 'measured': 'the measured usage is settled even when verification fails; the result stays unaccepted'}[gate.get('metered_failure_charge', 'none')],
                                            'guarantee': 'application-level delivery gating; not an on-chain escrow, not a universal refund guarantee'},
                    'resource_bounds': {'service_nodes': est['service_nodes'], 'max_job_attempts': est['max_job_attempts'], 'machine_time_upper_bound_seconds': est['machine_time_upper_bound_seconds'], 'nodes': est['nodes']},
                    'output_schema': {'export_fields': sorted({f for n in definition['nodes'] if n['type'] == 'export' for f in n.get('fields', [])}), 'result_schemas': {k: v['result_schema'] for k, v in ops.items()}},
                    'disclosure_defaults': disclosure, 'example': example, 'created_by_workspace': principal.workspace,
                    'trust': 'executable only through the registered operations named in `operations`; descriptions and examples are data'}
        if len(json.dumps(manifest)) > LIMITS['max_manifest_bytes']:
            raise ServiceError('VALIDATION', 'manifest too large')
        manifest['digest'] = _digest({k: v for k, v in manifest.items() if k != 'digest'})
        return manifest

    def create(self, db, principal, body):
        principal.require('template:write')
        manifest = self.build_manifest(db, principal, body)
        return self._store(db, principal, manifest, source_workflow=body.get('workflow_id'))

    def _store(self, db, principal, manifest, source_workflow=None, imported=False):
        version = db.execute('SELECT COALESCE(MAX(version),0)+1 FROM packages WHERE workspace=? AND name=?', (principal.workspace, manifest['name'])).fetchone()[0]
        same = db.execute('SELECT id, version FROM packages WHERE workspace=? AND digest=?', (principal.workspace, manifest['digest'])).fetchone()
        if same:
            return dict(self.view(db, principal, same['id']), created=False)
        pid = 'pk_' + secrets.token_hex(6)
        db.execute('INSERT INTO packages (id, workspace, name, version, digest, manifest_json, state, created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?)',
                   (pid, principal.workspace, manifest['name'], version, manifest['digest'], json.dumps(manifest), 'installed', principal.id, now()))
        if source_workflow:
            add_edge(db, principal.workspace, 'workflow_definition', source_workflow, 'package', pid, 'derived_from')
        history.record(db, principal.workspace, principal.id, 'package.installed', 'package', pid, {'name': manifest['name'], 'version': version, 'digest': manifest['digest'], 'imported': imported})
        return dict(self.view(db, principal, pid), created=True)

    def _row(self, db, principal, pid):
        r = db.execute('SELECT * FROM packages WHERE id=? AND workspace=?', (pid, principal.workspace)).fetchone()
        if r is None:
            raise ServiceError('NOT_FOUND', 'package')
        return r

    def view(self, db, principal, pid):
        principal.require('contract:read')
        r = self._row(db, principal, pid)
        m = json.loads(r['manifest_json'])
        runs = [dict(x) for x in db.execute('SELECT id, kind, run_id, job_id, state, attempt, created_at, updated_at FROM package_runs WHERE package_id=? ORDER BY created_at DESC LIMIT 50', (pid,)).fetchall()]
        return {'id': pid, 'name': r['name'], 'version': r['version'], 'digest': r['digest'], 'state': r['state'], 'retired_at': r['retired_at'], 'created_by': r['created_by'], 'created_at': r['created_at'], 'manifest': m, 'runs': runs,
                'versions': [dict(v) for v in db.execute('SELECT id, version, digest, state FROM packages WHERE workspace=? AND name=? ORDER BY version', (principal.workspace, r['name'])).fetchall()]}

    def list(self, db, principal):
        principal.require('contract:read')
        return [{'id': r['id'], 'name': r['name'], 'version': r['version'], 'digest': r['digest'], 'state': r['state'], 'created_at': r['created_at']} for r in db.execute('SELECT * FROM packages WHERE workspace=? ORDER BY name, version', (principal.workspace,)).fetchall()]

    def retire(self, db, principal, pid):
        principal.require('template:write')
        self._row(db, principal, pid)
        db.execute("UPDATE packages SET state='retired', retired_at=COALESCE(retired_at, ?) WHERE id=?", (now(), pid))
        history.record(db, principal.workspace, principal.id, 'package.retired', 'package', pid, {})
        return self.view(db, principal, pid)

    def export(self, db, principal, pid, include_example=True):
        """The manifest as a portable document: structure, identities, policy and the synthetic example; never inline inputs
        beyond what the author chose at creation, never credentials, datasets, prompts, results or keys."""
        principal.require('artifact:export')
        v = self.view(db, principal, pid)
        m = dict(v['manifest'])
        if not include_example:
            m['example'] = {}
            m['digest'] = _digest({k: x for k, x in m.items() if k != 'digest'})
        history.record(db, principal.workspace, principal.id, 'artifact.exported', 'package', pid, {'digest': m['digest']})
        return {'schema': SCHEMA, 'package': m, 'exported_from_version': v['version'], 'contents': 'manifest only: workflow structure, operation identities, required model identities, verification/delivery policy, disclosure defaults, synthetic example'}

    def check_manifest(self, manifest):
        """Structural validation of an imported manifest: it is data. Refuses unknown top-level keys, non-registered operation
        kinds, executable-looking node types, and digest mismatch."""
        from . import workflows as wf_mod
        allowed = {'schema', 'name', 'description', 'workflow', 'workflow_source_digest', 'stripped_inline_inputs', 'input_schemas', 'slots', 'units', 'operations', 'required_models', 'verification_policy', 'resource_bounds', 'output_schema', 'disclosure_defaults', 'example', 'created_by_workspace', 'trust', 'digest'}
        if type(manifest) is not dict or manifest.get('schema') != SCHEMA:
            raise ServiceError('VALIDATION', {'code': 'schema', 'expected': SCHEMA, 'got': (manifest or {}).get('schema') if type(manifest) is dict else None})
        if set(manifest) - allowed:
            raise ServiceError('VALIDATION', {'code': 'unknown_manifest_keys', 'keys': sorted(set(manifest) - allowed)})
        if type(manifest.get('name')) is not str or not SAFE_NAME.match(manifest['name']):
            raise ServiceError('VALIDATION', 'name')
        if _digest({k: v for k, v in manifest.items() if k != 'digest'}) != manifest.get('digest'):
            raise ServiceError('VALIDATION', {'code': 'digest_mismatch'})
        wf = manifest.get('workflow')
        if type(wf) is not dict or type(wf.get('nodes')) is not list:
            raise ServiceError('VALIDATION', 'workflow')
        for n in wf['nodes']:
            if type(n) is not dict or n.get('type') not in wf_mod.NODE_TYPES:
                raise ServiceError('VALIDATION', {'code': 'node_type', 'allowed': list(wf_mod.NODE_TYPES), 'node': (n or {}).get('id') if type(n) is dict else None})
            for k in n:
                if k in ('script', 'command', 'code', 'exec', 'shell', 'url'):
                    raise ServiceError('VALIDATION', {'code': 'executable_field_refused', 'node': n.get('id'), 'field': k})
        if type(manifest.get('operations')) is not dict:
            raise ServiceError('VALIDATION', 'operations')
        return manifest

    def compatibility(self, db, principal, manifest, device_policy=None):
        """Structured compatibility report; reserves nothing, starts nothing, downloads nothing."""
        principal.require('contract:read')
        self.check_manifest(manifest)
        from .catalog import INSTALLED, verifier_digest
        from .compute import manifests as compute_manifests, service as compute_svc
        from .verification import CLASSES, SUPPORT, implementation_digest as vdigest
        items = []
        def add(req, status, detail, action=None):
            items.append({'requirement': req, 'status': status, 'detail': detail, 'action': action})
        add('schema', 'supported_as_requested' if manifest['schema'] == SCHEMA else 'blocked_required_dependency', {'schema': manifest['schema'], 'supported': [SCHEMA]})
        caps = compute_svc.capabilities(db, self.settings)
        live = set(caps['facts']['currently_available']['live_worker_devices'])
        for kind, op in manifest['operations'].items():
            if kind not in INSTALLED:
                add('operation.' + kind, 'blocked_required_dependency', {'reason': 'operation kind not installed here'}, 'install a release that provides ' + kind); continue
            here = verifier_digest(kind)
            if here != op.get('verifier_digest'):
                add('operation.' + kind + '.implementation', 'blocked_required_dependency', {'package': (op.get('verifier_digest') or '')[:16], 'installed': here[:16], 'reason': 'a different implementation is not automatically equivalent'}, 'instantiate a new package version against this implementation after review')
            else:
                add('operation.' + kind + '.implementation', 'supported_as_requested', {'implementation_digest': here[:16]})
            if kind in compute_manifests.KINDS:
                want = device_policy or (manifest.get('example') or {}).get('device_policy') or 'auto'
                devices = set(compute_manifests.MANIFESTS[kind]['devices'])
                if devices == {'cpu'}:
                    add('operation.' + kind + '.device', 'supported_as_requested' if 'cpu' in live else 'blocked_required_dependency', {'note': 'cpu-only operation; a device policy does not apply', 'available': sorted(live)}, None if 'cpu' in live else 'start a worker')
                    continue
                if want == 'gpu' and 'cuda' not in live:
                    if 'cpu' in live and 'cpu' in devices and BACKEND_EQUIVALENT.get(kind, '').startswith('exact'):
                        add('operation.' + kind + '.device', 'supported_via_declared_equivalent', {'requested': 'gpu', 'available': sorted(live), 'equivalence': BACKEND_EQUIVALENT[kind]})
                    elif 'cpu' in live and 'cpu' in devices:
                        add('operation.' + kind + '.device', 'supported_via_declared_equivalent', {'requested': 'gpu', 'available': sorted(live), 'equivalence': BACKEND_EQUIVALENT.get(kind), 'note': 'tolerance-level equivalence; request replica verification if bit-identity matters'})
                    else:
                        add('operation.' + kind + '.device', 'blocked_required_dependency', {'requested': 'gpu', 'available': sorted(live)})
                elif want == 'auto' and 'cuda' not in live and 'cuda' in devices:
                    add('operation.' + kind + '.device', 'missing_optional_enhancement', {'enhancement': 'cuda backend', 'available': sorted(live) or ['none live'], 'effect': 'runs on cpu under the same contract'})
                elif not live:
                    add('operation.' + kind + '.device', 'blocked_required_dependency', {'reason': 'no live worker devices', 'available': []}, 'start a worker')
                else:
                    add('operation.' + kind + '.device', 'supported_as_requested', {'available': sorted(live)})
        for m in manifest.get('required_models') or []:
            row = db.execute('SELECT id, installed, status FROM model_revisions WHERE model_id=? AND revision=?', (m['model_id'], m['revision'])).fetchone()
            if row is None:
                add('model.' + m['model_id'] + '@' + m['revision'][:8], 'blocked_required_dependency', {'reason': 'model revision not registered here; a different model is not equivalent'}, 'register and install the pinned revision (no automatic download)')
            elif not row['installed']:
                add('model.' + m['model_id'] + '@' + m['revision'][:8], 'blocked_required_dependency', {'registered': row['id'], 'installed': False}, 'install the pinned artifact under the model store')
            else:
                add('model.' + m['model_id'] + '@' + m['revision'][:8], 'supported_as_requested', {'registered': row['id'], 'installed': True})
        vp = manifest.get('verification_policy') or {}
        if vp.get('gate') == 'required_verification':
            cls = vp.get('required_class')
            bad = [k for k in manifest['operations'] if cls not in CLASSES or cls not in SUPPORT.get(k, {})]
            add('verification.' + str(cls), 'blocked_required_dependency' if bad else 'supported_as_requested', {'class': cls, 'unsupported_for': bad, 'verifier_implementation': vdigest()[:16]}, 'choose a supported class in a new package version' if bad else None)
        for perm in ('contract:create', 'job:submit'):
            add('grant.' + perm, 'supported_as_requested' if principal.can(perm) else 'blocked_required_dependency', {'principal': principal.id, 'role': principal.role}, None if principal.can(perm) else 'use a principal whose role holds ' + perm)
        from .agents import grant_of
        g = grant_of(principal)
        if g is not None:
            add('grant.agent', 'supported_as_requested', {'grant_id': g.get('id') if isinstance(g, dict) else str(g)[:16], 'note': 'execution counts against the agent grant ceilings'})
        from .bundles import runtime_identity
        ident = runtime_identity(self.settings)
        add('runtime.torch', 'supported_as_requested' if ident.get('torch') else 'missing_optional_enhancement', {'installed': ident.get('torch'), 'effect': 'no cuda backend and no local models without torch'})
        blocking = [i['requirement'] for i in items if i['status'] == 'blocked_required_dependency']
        return {'compatible': not blocking, 'blocking': blocking, 'items': items, 'package_digest': manifest['digest'], 'nothing_reserved': True, 'nothing_started': True, 'nothing_downloaded': True,
                'statuses': list(COMPAT), 'note': 'deterministic comparison of declared requirements with this workspace; a model explanation cannot override it'}

    def import_manifest(self, db, principal, manifest, apply=False):
        principal.require('template:write')
        report = self.compatibility(db, principal, manifest)
        if not apply:
            return dict(report, installed=False)
        if not report['compatible']:
            raise ServiceError('CONFLICT', {'code': 'package_incompatible', 'blocking': report['blocking']})
        return dict(self._store(db, principal, dict(manifest), imported=True), compatibility=report)

    # ---- instantiation and composite quotes --------------------------------------------------------------------------
    def instantiate(self, db, principal, pid, values=None, inputs=None, name=None):
        """A concrete workflow definition from the package: slot values and per-node inline inputs for the nodes whose inputs
        were stripped (validated against the operation's input schema at contract creation). The definition records the package
        identity; runs keep it even when a newer package version is installed later."""
        principal.require('contract:create')
        r = self._row(db, principal, pid)
        if r['state'] != 'installed':
            raise ServiceError('CONFLICT', {'code': 'package_retired', 'package': pid})
        m = json.loads(r['manifest_json'])
        definition = json.loads(json.dumps(m['workflow']))
        inputs = inputs or {}
        if type(inputs) is not dict:
            raise ServiceError('VALIDATION', 'inputs: {node_id: inputs}')
        from .contracts import validate_inputs
        for n in definition['nodes']:
            if n['id'] in m.get('stripped_inline_inputs', []):
                if n['id'] not in inputs:
                    raise ServiceError('VALIDATION', {'code': 'node_inputs_required', 'node': n['id'], 'schema': m['input_schemas'].get(n['id'])})
                validate_inputs(n['type'], inputs[n['id']])
                n['inputs'] = inputs[n['id']]; n.pop('inputs_digest', None)
        unknown = set(inputs) - {n['id'] for n in definition['nodes']}
        if unknown:
            raise ServiceError('VALIDATION', {'code': 'unknown_node', 'nodes': sorted(unknown)})
        definition['name'] = (name or (m['name'] + ' v%d' % r['version']))[:128]
        from . import workflows as wf_mod
        if wf_mod.slot_references(definition):
            if type(values) is not dict:
                raise ServiceError('VALIDATION', {'code': 'slot_values_required', 'slots': sorted(wf_mod.slot_references(definition))})
            wid, digest, created = self.svc.workflows.create(db, principal, definition)
            inst = self.svc.workflows.instantiate(db, principal, wid, values, name=definition['name'])
            wid, digest = inst['id'], inst['digest']
        else:
            wid, digest, created = self.svc.workflows.create(db, principal, definition)
        add_edge(db, principal.workspace, 'package', pid, 'workflow_definition', wid, 'derived_from')
        history.record(db, principal.workspace, principal.id, 'package.instantiated', 'package', pid, {'workflow_id': wid, 'digest': digest})
        return {'package_id': pid, 'package_version': r['version'], 'package_digest': r['digest'], 'workflow_id': wid, 'definition_digest': digest}

    def quote(self, db, principal, pid, workflow_id, scheme='exact'):
        """Composite quote over the instantiated definition: one bound service quote per service node (fixed for exact, metered
        for upto), totals, expiry = earliest node expiry, assumptions stated."""
        principal.require('contract:create')
        r = self._row(db, principal, pid)
        if r['state'] != 'installed':
            raise ServiceError('CONFLICT', {'code': 'package_retired'})
        if scheme not in ('exact', 'upto'):
            raise ServiceError('VALIDATION', {'code': 'scheme', 'allowed': ['exact', 'upto']})
        drow = self.svc.workflows.get_definition(db, principal, workflow_id)
        definition = merkle.parse(drow['definition_json'])
        from . import workflows as wf_mod
        cat = {s['kind']: s for s in self.svc.catalog.list(db, principal)}
        components, total, fixed, metered, units = [], 0, 0, 0, {}
        input_digests = {}
        for n in definition['nodes']:
            if n['type'] not in wf_mod.SERVICE_TYPES:
                components.append({'node': n['id'], 'type': n['type'], 'charge': 0, 'basis': 'no declared charge (structural node)'}); continue
            if 'inputs' not in n:
                components.append({'node': n['id'], 'type': n['type'], 'charge': None, 'basis': 'dataset-bound node: quoted at run start from the bound version'}); continue
            svc_row = cat.get(n['type'])
            if svc_row is None:
                raise ServiceError('CONFLICT', {'code': 'service_not_registered', 'kind': n['type']})
            q = self.svc.catalog.quote(db, principal, svc_row['id'], n['inputs'], quantity_max=None, provider_mode=self.settings.provider_mode, scheme=scheme)
            input_digests[n['id']] = _digest(n['inputs'])
            comp = {'node': n['id'], 'type': n['type'], 'quote_id': q['quote_id'], 'service_id': svc_row['id'], 'scheme': scheme, 'quantity_max': q['quantity_max'], 'unit': q['unit'], 'amount_per_unit': q['amount_per_unit'], 'amount_max': q['amount_max'], 'asset': q['asset'], 'expires_at': q.get('expires_at'),
                    'component': 'metered (upto: authorized up to amount_max, settled for measured units)' if scheme == 'upto' else 'fixed (exact: amount_max settles before computation)'}
            components.append(comp); total += q['amount_max']; units[n['id']] = {'quantity_max': q['quantity_max'], 'unit': q['unit']}
            if scheme == 'upto':
                metered += q['amount_max']
            else:
                fixed += q['amount_max']
        expires = min([c['expires_at'] for c in components if c.get('expires_at')] + [now() + LIMITS['quote_ttl_seconds']])
        vp = json.loads(r['manifest_json'])['verification_policy']
        qid = 'pq_' + secrets.token_hex(6)
        binding = {'schema': QUOTE_SCHEMA, 'package_id': pid, 'package_digest': r['digest'], 'workflow_id': workflow_id, 'definition_digest': drow['digest'], 'input_digests': input_digests, 'scheme': scheme,
                   'pricing_revision': sorted({str(c.get('amount_per_unit')) for c in components if c.get('quote_id')}), 'provider_mode': self.settings.provider_mode}
        digest = _digest(binding)
        db.execute('INSERT INTO package_quotes (id, workspace, package_id, workflow_id, principal_id, scheme, digest, binding_json, components_json, amount_max, fixed_amount, metered_amount, expires_at, state, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   (qid, principal.workspace, pid, workflow_id, principal.id, scheme, digest, json.dumps(binding), json.dumps(components), total, fixed, metered, expires, 'open', now()))
        history.record(db, principal.workspace, principal.id, 'package.quoted', 'package', pid, {'quote_id': qid, 'amount_max': total, 'scheme': scheme})
        return self.quote_view(db, principal, qid)

    def quote_view(self, db, principal, qid):
        r = db.execute('SELECT * FROM package_quotes WHERE id=? AND workspace=?', (qid, principal.workspace)).fetchone()
        if r is None:
            raise ServiceError('NOT_FOUND', 'package quote')
        pol = json.loads(self._row(db, principal, r['package_id'])['manifest_json'])['verification_policy']
        state = r['state'] if not (r['state'] == 'open' and r['expires_at'] < now()) else 'expired'
        return {'quote_id': qid, 'state': state, 'scheme': r['scheme'], 'package_id': r['package_id'], 'workflow_id': r['workflow_id'], 'binding': json.loads(r['binding_json']), 'digest': r['digest'], 'components': json.loads(r['components_json']),
                'amount_max': r['amount_max'], 'fixed_amount': r['fixed_amount'], 'metered_amount': r['metered_amount'], 'expires_at': r['expires_at'], 'created_at': r['created_at'],
                'assumptions': ['per-node ceilings from the service catalog at quote time; work bounded by validated inputs', 'verification, report assembly and reuse carry no declared charge in this catalog', 'reused results meter at zero',
                                'dataset-bound nodes are quoted at run start from the bound version'],
                'settlement_rule': {'exact': 'fixed components settle before computation', 'upto': 'metered components are authorized up to their ceiling and settled for measured units after delivery gating'}[r['scheme']],
                'delivery_policy': pol, 'economic_vs_physical': 'amounts are catalog charges; work units are the physical accounting; a reused result computes nothing and charges nothing'}

    # ---- package runs and delivery gating ------------------------------------------------------------------------------
    def start_run(self, db, principal, pid, quote_id, budget_ceiling=None):
        """Admit and start the package run under the composite quote: the quote must be open and unexpired and bind the same
        package/definition/input digests; the run's budget ceiling is the quote total (or a lower explicit ceiling); nodes reserve
        against that ceiling atomically inside the workflow engine."""
        principal.require('job:submit')
        q = self.quote_view(db, principal, quote_id)
        if q['package_id'] != pid:
            raise ServiceError('VALIDATION', 'quote belongs to another package')
        if q['state'] != 'open':
            raise ServiceError('CONFLICT', {'code': 'quote_' + q['state'], 'quote_id': quote_id})
        r = self._row(db, principal, pid)
        if r['state'] != 'installed':
            raise ServiceError('CONFLICT', {'code': 'package_retired'})
        drow = self.svc.workflows.get_definition(db, principal, q['workflow_id'])
        if drow['digest'] != q['binding']['definition_digest'] or r['digest'] != q['binding']['package_digest']:
            raise ServiceError('CONFLICT', {'code': 'quote_input_mismatch', 'detail': 'the definition or package changed since the quote; request a new quote'})
        definition = merkle.parse(drow['definition_json'])
        for n in definition['nodes']:
            if n['id'] in q['binding']['input_digests'] and _digest(n.get('inputs')) != q['binding']['input_digests'][n['id']]:
                raise ServiceError('CONFLICT', {'code': 'quote_input_mismatch', 'node': n['id']})
        ceiling = q['amount_max'] if budget_ceiling is None else min(int(budget_ceiling), q['amount_max'])
        from . import budgets
        root = budgets.root(db, principal.workspace)
        chk = budgets.check(db, root['id'], ceiling)
        if chk is not None:
            raise ServiceError('CONFLICT', {'code': 'budget_refused', 'detail': chk, 'quote_total': q['amount_max']})
        started = self.svc.workflows.start_run(db, principal, q['workflow_id'], bindings=None, budget_ceiling=ceiling)
        rid = 'pr_' + secrets.token_hex(6)
        db.execute('INSERT INTO package_runs (id, workspace, package_id, kind, run_id, job_id, quote_id, state, attempt, delivery_json, verification_json, created_by, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   (rid, principal.workspace, pid, 'workflow', started['run_id'], None, quote_id, 'running', 1, '{}', '{}', principal.id, now(), now()))
        db.execute("UPDATE package_quotes SET state='consumed', consumed_at=? WHERE id=?", (now(), quote_id))
        add_edge(db, principal.workspace, 'package', pid, 'workflow_run', started['run_id'], 'used_input')
        add_edge(db, principal.workspace, 'package_quote', quote_id, 'workflow_run', started['run_id'], 'used_input')
        history.record(db, principal.workspace, principal.id, 'package.run_started', 'package', pid, {'package_run_id': rid, 'run_id': started['run_id'], 'quote_id': quote_id, 'ceiling': ceiling})
        return self.run_view(db, principal, rid)

    def bind_metered_job(self, db, principal, pid, job_id):
        """A single-operation package executed through the existing x402 metered route: the job becomes a gated package run.
        Delivery and the final charge wait for the package's required verification."""
        principal.require('job:submit')
        r = self._row(db, principal, pid)
        m = json.loads(r['manifest_json'])
        job = db.execute('SELECT * FROM jobs WHERE id=? AND workspace=?', (job_id, principal.workspace)).fetchone()
        if job is None:
            raise ServiceError('NOT_FOUND', 'job')
        kinds = [n['type'] for n in m['workflow']['nodes'] if n['type'] in m['operations']]
        if len(kinds) != 1 or job['kind'] != kinds[0]:
            raise ServiceError('VALIDATION', {'code': 'not_a_single_operation_package', 'package_operations': kinds, 'job_kind': job['kind']})
        if db.execute('SELECT 1 FROM package_runs WHERE job_id=?', (job_id,)).fetchone():
            raise ServiceError('CONFLICT', 'job already bound to a package run')
        rid = 'pr_' + secrets.token_hex(6)
        db.execute('INSERT INTO package_runs (id, workspace, package_id, kind, run_id, job_id, quote_id, state, attempt, delivery_json, verification_json, created_by, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   (rid, principal.workspace, pid, 'metered_job', None, job_id, job['quote_id'], 'running', 1, '{}', '{}', principal.id, now(), now()))
        add_edge(db, principal.workspace, 'package', pid, 'job', job_id, 'used_input')
        history.record(db, principal.workspace, principal.id, 'package.run_started', 'package', pid, {'package_run_id': rid, 'job_id': job_id, 'metered': True})
        return self.run_view(db, principal, rid)

    def _run_row(self, db, rid, workspace=None):
        r = db.execute('SELECT * FROM package_runs WHERE id=?' + (' AND workspace=?' if workspace else ''), (rid,) + ((workspace,) if workspace else ())).fetchone()
        if r is None:
            raise ServiceError('NOT_FOUND', 'package run')
        return r

    def _jobs_of(self, db, pr):
        if pr['kind'] == 'metered_job':
            return {'job': pr['job_id']}
        return {n['node_id']: n['job_id'] for n in db.execute('SELECT node_id, job_id FROM workflow_nodes WHERE run_id=? AND job_id IS NOT NULL', (pr['run_id'],)).fetchall()}

    def gate_for_job(self, db, job_id):
        """Consulted by settlement: None when the job is not under a package delivery gate, otherwise the gate state and policy."""
        pr = db.execute('SELECT pr.*, p.manifest_json FROM package_runs pr JOIN packages p ON p.id=pr.package_id WHERE pr.job_id=? OR pr.run_id IN (SELECT run_id FROM workflow_nodes WHERE job_id=?)', (job_id, job_id)).fetchone()
        if pr is None:
            return None
        pol = json.loads(pr['manifest_json'])['verification_policy']
        if pol['gate'] != 'required_verification':
            return None
        state = self.tick(db, pr['id'])                     # the gate reports the current facts, not a stale row
        return {'package_run_id': pr['id'], 'state': state, 'policy': pol}

    def tick(self, db, rid):
        """Advance a package run's delivery state from the facts: underlying jobs → required verification requests → delivered /
        unaccepted / failed. Idempotent; called by the worker's maintenance tick and by views."""
        pr = self._run_row(db, rid)
        if pr['state'] in ('delivered', 'unaccepted', 'failed', 'cancelled'):
            return pr['state']
        pol = json.loads(self._row_any(db, pr['package_id'])['manifest_json'])['verification_policy']
        jobs = self._jobs_of(db, pr)
        if pr['kind'] == 'workflow':
            run = db.execute('SELECT state FROM workflow_runs WHERE id=?', (pr['run_id'],)).fetchone()
            if run['state'] in ('failed', 'cancelled', 'blocked', 'partially_failed'):
                self._set(db, pr, 'failed' if run['state'] != 'cancelled' else 'cancelled', {'run_state': run['state']}); return pr['state']
            if run['state'] != 'completed':
                return 'running'
        states = {n: db.execute('SELECT state, kind FROM jobs WHERE id=?', (j,)).fetchone() for n, j in jobs.items()}
        if any(s['state'] in ('failed', 'cancelled') for s in states.values()):
            self._set(db, pr, 'failed', {'jobs': {n: s['state'] for n, s in states.items()}}); return 'failed'
        if not jobs or any(s['state'] != 'succeeded' for s in states.values()):
            return 'running'
        if pol['gate'] != 'required_verification':
            self._set(db, pr, 'delivered', {'jobs': jobs, 'gate': 'none'}); return 'delivered'
        ver = json.loads(pr['verification_json'] or '{}')
        owner = self.svc.workflows._principal(db, pr['created_by'])
        changed = False
        for node, jid in jobs.items():
            if node not in ver:
                job = db.execute('SELECT kind FROM jobs WHERE id=?', (jid,)).fetchone()
                from .verification import SUPPORT
                if pol['required_class'] not in SUPPORT.get(job['kind'], {}):
                    ver[node] = {'verification_id': None, 'state': 'unsupported'}; changed = True; continue
                existing = db.execute('SELECT id, state FROM verification_jobs WHERE target_job_id=? AND class=? ORDER BY created_at DESC LIMIT 1', (jid, pol['required_class'])).fetchone()
                if existing is None:
                    from .auth import Principal
                    v = self.svc.verification.request(db, owner, jid, pol['required_class'], {})
                    ver[node] = {'verification_id': v['id'], 'state': v['state']}
                else:
                    ver[node] = {'verification_id': existing['id'], 'state': existing['state']}
                changed = True
            else:
                if ver[node].get('verification_id'):
                    cur = db.execute('SELECT state FROM verification_jobs WHERE id=?', (ver[node]['verification_id'],)).fetchone()
                    if cur and cur['state'] != ver[node]['state']:
                        ver[node]['state'] = cur['state']; changed = True
        if changed:
            db.execute('UPDATE package_runs SET verification_json=?, state=?, updated_at=? WHERE id=?', (json.dumps(ver), 'awaiting_verification', now(), rid))
        if any(v['state'] in ('failed', 'unsupported') for v in ver.values()):
            failed = {n: v for n, v in ver.items() if v['state'] in ('failed', 'unsupported')}
            self._set(db, pr, 'unaccepted', {'jobs': jobs, 'failed_verifications': failed, 'evidence': 'result and verification failure kept privately; not delivered', 'charge_policy': pol['metered_failure_charge']})
            return 'unaccepted'
        if ver and all(v['state'] == 'passed' for v in ver.values()):
            self._set(db, pr, 'delivered', {'jobs': jobs, 'verifications': ver, 'required_class': pol['required_class']})
            return 'delivered'
        return 'awaiting_verification'

    def _row_any(self, db, pid):
        return db.execute('SELECT * FROM packages WHERE id=?', (pid,)).fetchone()

    def _set(self, db, pr, state, delivery):
        db.execute('UPDATE package_runs SET state=?, delivery_json=?, updated_at=? WHERE id=?', (state, json.dumps(delivery), now(), pr['id']))
        history.record(db, pr['workspace'], 'scheduler', 'package.delivery', 'package', pr['package_id'], {'package_run_id': pr['id'], 'state': state})

    def tick_all(self, db, limit=50):
        out = []
        for r in db.execute("SELECT id FROM package_runs WHERE state IN ('running','awaiting_verification') ORDER BY updated_at LIMIT ?", (limit,)).fetchall():
            try:
                out.append({'package_run': r['id'], 'state': self.tick(db, r['id'])})
            except ServiceError as exc:
                out.append({'package_run': r['id'], 'error': exc.code})
        return out

    def run_view(self, db, principal, rid):
        principal.require('job:read')
        pr = self._run_row(db, rid, principal.workspace)
        state = self.tick(db, rid)
        pr = self._run_row(db, rid, principal.workspace)
        jobs = self._jobs_of(db, pr)
        pol = json.loads(self._row_any(db, pr['package_id'])['manifest_json'])['verification_policy']
        out = {'id': rid, 'package_id': pr['package_id'], 'kind': pr['kind'], 'run_id': pr['run_id'], 'job_id': pr['job_id'], 'quote_id': pr['quote_id'], 'state': state, 'attempt': pr['attempt'], 'jobs': jobs,
               'verification': json.loads(pr['verification_json'] or '{}'), 'delivery': json.loads(pr['delivery_json'] or '{}') if (principal.can('job:read_private') or state == 'delivered') else {'withheld': True},
               'delivery_policy': pol, 'created_at': pr['created_at'], 'updated_at': pr['updated_at'],
               'meaning': {'running': 'execution in progress', 'awaiting_verification': 'results computed; delivery and final charge withheld until the required verification passes', 'delivered': 'verification passed; results deliverable and metered settlement allowed',
                           'unaccepted': 'verification failed; result and evidence kept privately; failure charge policy applied', 'failed': 'execution failed', 'cancelled': 'cancelled'}.get(state)}
        if pr['kind'] == 'metered_job' and pr['job_id']:
            ms = db.execute('SELECT * FROM metered_settlements WHERE job_id=?', (pr['job_id'],)).fetchone()
            out['settlement'] = self.svc.sales.settlement_view(ms) if ms else None
        return out

    def retry(self, db, principal, rid):
        """A new execution attempt for an unaccepted or failed metered package run under the SAME payment authorization: the
        authorization identity, ceiling, expiry and replay protection are unchanged; the previous job stays as failure evidence."""
        principal.require('job:submit')
        pr = self._run_row(db, rid, principal.workspace)
        if pr['kind'] != 'metered_job' or pr['state'] not in ('unaccepted', 'failed'):
            raise ServiceError('CONFLICT', {'code': 'retry_not_applicable', 'state': pr['state'], 'kind': pr['kind']})
        old = db.execute('SELECT * FROM jobs WHERE id=?', (pr['job_id'],)).fetchone()
        ms = db.execute('SELECT * FROM metered_settlements WHERE job_id=?', (old['id'],)).fetchone()
        if ms is not None and ms['state'] not in ('AUTHORIZED',):
            raise ServiceError('CONFLICT', {'code': 'authorization_not_reusable', 'settlement_state': ms['state']})
        quote = db.execute('SELECT * FROM quotes WHERE id=?', (old['quote_id'],)).fetchone() if old['quote_id'] else None
        if quote is not None and quote['expires_at'] < now():
            raise ServiceError('CONFLICT', {'code': 'quote_expired', 'detail': 'the payment authorization bound to an expired quote cannot fund a retry'})
        contract = db.execute('SELECT * FROM contracts WHERE id=?', (old['contract_id'],)).fetchone()
        vault = self.svc.store.load_json(db, contract['input_artifact_id'], principal.workspace)
        inputs = {f['name']: f['value'] for f in vault['fields']}['inputs']
        cid = self.svc.contracts.create_draft(db, principal, kind=old['kind'], title=(contract['title'] or old['kind']) + ' (retry)', inputs=inputs, policy={'reviewer_id': contract['reviewer_id']}, datasets=self.svc.datasets)
        self.svc.contracts.freeze(db, principal, cid)
        jid = self.svc.jobs.submit(db, principal, cid)
        if old['quote_id']:
            db.execute('UPDATE jobs SET quote_id=? WHERE id=?', (old['quote_id'], jid)); db.execute('UPDATE contracts SET quote_id=? WHERE id=?', (old['quote_id'], cid))
        if ms is not None:
            db.execute('UPDATE metered_settlements SET job_id=?, updated_at=? WHERE payment_id=?', (jid, now(), ms['payment_id']))
        db.execute("UPDATE package_runs SET job_id=?, state='running', attempt=attempt+1, verification_json='{}', delivery_json=?, updated_at=? WHERE id=?", (jid, json.dumps({'previous_attempt_job': old['id'], 'previous_state': pr['state']}), now(), rid))
        add_edge(db, principal.workspace, 'job', old['id'], 'job', jid, 'derived_from')
        history.record(db, principal.workspace, principal.id, 'package.retry', 'package', pr['package_id'], {'package_run_id': rid, 'previous_job': old['id'], 'job_id': jid, 'payment_id': ms['payment_id'] if ms else None})
        return self.run_view(db, principal, rid)

    def list_runs(self, db, principal, pid=None):
        principal.require('job:read')
        rows = db.execute('SELECT id FROM package_runs WHERE workspace=? ' + ('AND package_id=? ' if pid else '') + 'ORDER BY created_at DESC LIMIT 100', (principal.workspace,) + ((pid,) if pid else ())).fetchall()
        return [self.run_view(db, principal, r['id']) for r in rows]

    # ---- signed result bundles -------------------------------------------------------------------------------------------
    def result_bundle(self, db, principal, rid, scope=None):
        """Zip bytes: manifest.json (canonical), statement.json + signature, per-node summary.json (projected fields), inputs.json
        (only when disclosed) and the numerical witness (plan.json for resource plans, when disclosed). Names are bounded and
        sanitized; no paths, keys or private runtime state."""
        principal.require('artifact:export'); principal.require('job:read_private')
        pr = self._run_row(db, rid, principal.workspace)
        state = self.tick(db, rid)
        if state != 'delivered':
            raise ServiceError('CONFLICT', {'code': 'not_delivered', 'state': state, 'detail': 'result bundles are issued for delivered package runs only'})
        pkg = self._row_any(db, pr['package_id']); m = json.loads(pkg['manifest_json'])
        scope = dict(m['disclosure_defaults'], **(scope or {}))
        if set(scope) - {'disclose_inputs', 'disclose_witness', 'summary_fields'}:
            raise ServiceError('VALIDATION', 'scope: {disclose_inputs, disclose_witness, summary_fields}')
        jobs = self._jobs_of(db, pr)
        from .compute import container
        files, nodes = {}, []
        for node, jid in sorted(jobs.items()):
            safe = re.sub(r'[^A-Za-z0-9_-]', '_', node)[:32]
            job = db.execute('SELECT * FROM jobs WHERE id=?', (jid,)).fetchone()
            contract = db.execute('SELECT * FROM contracts WHERE id=?', (job['contract_id'],)).fetchone()
            doc = json.loads(contract['contract_json'] or '{}')
            summary = json.loads(job['summary_json'] or '{}')
            fields = scope.get('summary_fields')
            projected = {k: v for k, v in summary.items() if fields is None or k in fields}
            projected.pop('private_label', None)
            files['files/%s/summary.json' % safe] = merkle.canonical(projected)
            entry = {'node': node, 'job_id': jid, 'kind': job['kind'], 'outcome': job['outcome'], 'evidence_root': job['evidence_root'], 'contract_digest': contract['contract_digest'], 'input_commitment': contract['inputs_digest'] or contract['input_root'],
                     'operation': {'model_id': doc.get('model_id'), 'verifier_id': doc.get('verifier_id'), 'verifier_digest': doc.get('verifier_digest')}, 'summary_file': 'files/%s/summary.json' % safe, 'reused_from': job['reused_from'] if 'reused_from' in job.keys() else None}
            if scope.get('disclose_inputs'):
                vault = self.svc.store.load_json(db, contract['input_artifact_id'], principal.workspace)
                inputs = {f['name']: f['value'] for f in vault['fields']}['inputs']
                inputs = {k: v for k, v in inputs.items() if k != 'private_label'}
                files['files/%s/inputs.json' % safe] = merkle.canonical(inputs); entry['inputs_file'] = 'files/%s/inputs.json' % safe
            run = db.execute('SELECT output_artifact_id FROM compute_runs WHERE job_id=?', (jid,)).fetchone()
            if scope.get('disclose_witness') and run and run['output_artifact_id'] and job['kind'] == 'resource_plan':
                out = container.unpack(self.svc.store.load(db, run['output_artifact_id'], principal.workspace))
                plan = json.loads(out['plan.json'])
                witness = {'schema': 'metacoin-resource-plan-witness/v1', 'assignments': plan.get('assignments'), 'status': plan.get('status'), 'objective': plan.get('objective'), 'min_margin': plan.get('min_margin'), 'spill': plan.get('spill'), 'trajectory_energy': [t['energy'] for t in plan.get('trajectory') or []],
                           'input_commitment': entry['input_commitment'], 'recomputable_offline': bool(scope.get('disclose_inputs'))}
                files['files/%s/witness.json' % safe] = merkle.canonical(witness); entry['witness_file'] = 'files/%s/witness.json' % safe
            vrows = db.execute('SELECT id, class, state, result_commitment, statement_json, signature_hex, key_id FROM verification_jobs WHERE target_job_id=? ORDER BY created_at', (jid,)).fetchall()
            entry['verifications'] = [{'id': v['id'], 'class': v['class'], 'state': v['state'], 'result_commitment': v['result_commitment'], 'statement': json.loads(v['statement_json']) if v['statement_json'] else None, 'signature': v['signature_hex'], 'key_id': v['key_id']} for v in vrows]
            nodes.append(entry)
        from . import metering
        pub = metering.ensure_service_key(self.settings, db)
        manifest = {'schema': BUNDLE_SCHEMA, 'package': {'id': pkg['id'], 'name': pkg['name'], 'version': pkg['version'], 'digest': pkg['digest']}, 'package_run_id': rid, 'run_id': pr['run_id'], 'workspace': principal.workspace, 'delivery_state': state,
                    'delivery_policy': m['verification_policy'], 'operations': m['operations'], 'nodes': nodes, 'scope': scope, 'file_sha256': {k: hashlib.sha256(v).hexdigest() for k, v in files.items()}, 'issued_at': now(), 'issued_by': principal.id,
                    'issuer_key_id': crypto.key_id_for(pub), 'signing': 'ed25519 by the issuing service key over the canonical manifest; a valid signature identifies the issuer, not an independent review',
                    'undisclosed': [] if scope.get('disclose_inputs') else ['inputs (commitments only): the science cannot be recomputed by a recipient from this bundle']}
        canon = merkle.canonical(manifest)
        signature = crypto.sign(crypto.load_signing_key(self.settings.keys_dir / 'service.ed25519'), canon)
        files['manifest.json'] = canon
        files['statement.json'] = merkle.canonical({'schema': BUNDLE_SCHEMA + '-statement', 'manifest_sha256': hashlib.sha256(canon).hexdigest(), 'signature': signature, 'public_key': pub, 'key_id': manifest['issuer_key_id']})
        if len(files) > LIMITS['bundle_max_members'] or sum(len(v) for v in files.values()) > LIMITS['bundle_max_bytes']:
            raise ServiceError('VALIDATION', 'bundle too large')
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w', compression=zipfile.ZIP_DEFLATED) as z:
            for name in sorted(files):
                zi = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0)); zi.compress_type = zipfile.ZIP_DEFLATED; zi.external_attr = 0o644 << 16
                z.writestr(zi, files[name])
        history.record(db, principal.workspace, principal.id, 'artifact.exported', 'package', pkg['id'], {'package_run_id': rid, 'bundle_sha256': hashlib.sha256(buf.getvalue()).hexdigest(), 'scope': scope})
        return buf.getvalue(), manifest

    def result_bundle_json(self, db, principal, rid, scope=None):
        data, manifest = self.result_bundle(db, principal, rid, scope)
        return {'schema': BUNDLE_SCHEMA, 'package_run_id': rid, 'zip_base64': base64.b64encode(data).decode(), 'zip_sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data), 'manifest': manifest,
                'verify': 'python -m metacoin_service.verify_bundle <bundle.zip> --trusted-key <hex> [--recompute]'}
