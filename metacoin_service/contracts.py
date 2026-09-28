"""Drafts change; a frozen contract never does. An amendment is a new version with lineage."""
import hashlib
import json
import secrets
from experiments.private_receipts import receipt as merkle
from experiments.work_contracts import contract as terms, energy_analysis as energy
from . import history, science, temporal
from .compute import inputs as compute_inputs, manifests as compute_manifests
from .models import service as model_svc, engine as model_engine
from .db import now
from .errors import ServiceError

COMPUTE_KINDS = compute_manifests.KINDS
MODEL_KINDS = model_engine.KINDS
KINDS = ('energy_audit', 'safe_runtime', 'plan_comparison', 'task_selection', 'temporal_energy') + COMPUTE_KINDS + MODEL_KINDS
DEFAULT_EXPIRY_SECONDS = 7 * 86400
SERVICE_CONTRACT_SCHEMA = 'metacoin-service-contract/v1'
VALIDATORS = {'energy_audit': energy.validate, 'safe_runtime': science.validate_safe_runtime,
              'plan_comparison': science.validate_comparison, 'task_selection': science.validate_selection,
              'temporal_energy': temporal.validate, **compute_inputs.VALIDATORS, **model_svc.VALIDATORS}
MODEL_IDS = {'safe_runtime': science.SAFE_RUNTIME_MODEL, 'plan_comparison': science.COMPARISON_MODEL,
             'task_selection': science.SELECTION_MODEL, 'temporal_energy': temporal.MODEL_ID,
             **{k: m['model_id'] for k, m in compute_manifests.MANIFESTS.items()}, **model_engine.MODEL_IDS}
VERIFIER_OF = {'temporal_energy': ('temporal-energy-verifier/v1', temporal.bundle_digest),
               **{k: (m['manifest_id'] + '-verifier', compute_manifests.implementation_digest) for k, m in compute_manifests.MANIFESTS.items()},
               **{k: ('model-runtime/v1', model_engine.implementation_digest) for k in MODEL_KINDS}}


DEFAULT_CAPABILITY = {'simulation': 'legacy_simulation', 'test-http': 'x402_loopback_test', 'production': 'x402_http_buyer'}


def validate_policy(kind, policy, default_capability='legacy_simulation'):
    merkle.canonical(policy)
    allowed = {'accepted_outcomes', 'disclose_outcome', 'disclose_explanation', 'amount', 'capability',
               'expires_in_seconds', 'retention_seconds', 'reviewer_id'}
    if type(policy) is not dict or not set(policy) <= allowed:
        raise ServiceError('VALIDATION', 'policy fields')
    out = {'accepted_outcomes': list(energy.OUTCOMES), 'disclose_outcome': True, 'disclose_explanation': False,
           'amount': 1, 'capability': default_capability, 'expires_in_seconds': DEFAULT_EXPIRY_SECONDS,
           'retention_seconds': 30 * 86400, 'reviewer_id': None}
    out.update(policy)
    if type(out['accepted_outcomes']) is not list or not out['accepted_outcomes'] \
            or not set(out['accepted_outcomes']) <= set(energy.OUTCOMES) or len(set(out['accepted_outcomes'])) != len(out['accepted_outcomes']):
        raise ServiceError('VALIDATION', 'accepted_outcomes')
    for flag in ('disclose_outcome', 'disclose_explanation'):
        if type(out[flag]) is not bool:
            raise ServiceError('VALIDATION', flag)
    energy.integer(out['amount'], 1, 10**9)
    energy.integer(out['expires_in_seconds'], 60, 365 * 86400)
    energy.integer(out['retention_seconds'], 3600, 10 * 365 * 86400)
    if out['capability'] not in terms.CAPABILITIES:
        raise ServiceError('VALIDATION', 'capability')
    if out['reviewer_id'] is not None and (type(out['reviewer_id']) is not str or len(out['reviewer_id']) > 64):
        raise ServiceError('VALIDATION', 'reviewer_id')
    return out


def validate_inputs(kind, inputs):
    if kind not in KINDS:
        raise ServiceError('VALIDATION', 'kind')
    VALIDATORS[kind](inputs)     # the same validators the CLI uses; raises merkle.Invalid
    return inputs


class Contracts:
    def __init__(self, store, settings=None):
        self.store, self.settings = store, settings
        # Contracts default to the action capability that matches the configured provider mode,
        # so a job created in the console can actually be dispatched on this instance.
        self.default_capability = DEFAULT_CAPABILITY[settings.provider_mode] if settings else 'legacy_simulation'

    def create_draft(self, db, principal, *, kind, title, inputs, policy, datasets=None):
        principal.require('contract:create')
        if type(title) is not str or not 1 <= len(title) <= 128:
            raise ServiceError('VALIDATION', 'title')
        dataset_version_id = None
        if type(inputs) is dict and 'dataset_version_id' in inputs:
            # Materialize an immutable model input from a dataset version + explicit parameters.
            if datasets is None or set(inputs) != {'dataset_version_id', 'parameters'}:
                raise ServiceError('VALIDATION', 'dataset-backed inputs: {dataset_version_id, parameters}')
            rows, v = datasets.rows(db, principal, inputs['dataset_version_id'])
            ds = db.execute('SELECT retired_at FROM datasets WHERE id=?', (v['dataset_id'],)).fetchone()
            if ds['retired_at'] is not None:
                raise ServiceError('CONFLICT', 'dataset retired; new work refused')
            if kind == 'temporal_energy' and v['kind'] == 'temporal_series':
                inputs = datasets.temporal_input(rows, inputs['parameters'], v)
            elif kind == 'energy_audit' and v['kind'] == 'energy_intervals':
                inputs = datasets.interval_input(rows, inputs['parameters'], v)
            else:
                raise ServiceError('VALIDATION', 'dataset kind incompatible with contract kind')
            dataset_version_id = v['id']
        validate_inputs(kind, inputs)
        pol = validate_policy(kind, policy, self.default_capability)
        if pol['reviewer_id'] is not None:
            self._reviewer(db, principal.workspace, pol['reviewer_id'])
        cid = 'ct_' + secrets.token_hex(8)
        params = {}
        if kind in COMPUTE_KINDS:                   # the accepted job binds manifest version, implementation digest, device policy and work bound
            man = compute_manifests.manifest(kind)
            params = {'manifest_id': man['manifest_id'], 'manifest_version': man['version'], 'implementation_digest': man['implementation_digest'],
                      'device_policy': inputs.get('device_policy', 'auto'), 'precision': man['precision'][0], 'work_units': compute_inputs.work_units(kind, inputs),
                      'input_digest': hashlib.sha256(merkle.canonical(inputs)).hexdigest()}
        elif kind in MODEL_KINDS:                   # binds the exact model revision (explicit or promoted default) and the runtime implementation
            params = model_svc.bind_params(db, self.settings, kind, inputs)
        aid = self.store.store(db, workspace=principal.workspace, kind='draft_input', owner_id=principal.id,
                               plaintext=merkle.canonical(inputs), recipients=[], intended_use='draft-input;owner-and-worker',
                               contract_id=None)
        db.execute('INSERT INTO contracts (id, workspace, owner_id, kind, state, version, lineage_id, title, policy_json, params_json, '
                   'input_artifact_id, reviewer_id, created_at) VALUES (?,?,?,?,?,1,?,?,?,?,?,?,?)',
                   (cid, principal.workspace, principal.id, kind, 'draft', cid, title, json.dumps(pol), json.dumps(params),
                    aid, pol['reviewer_id'], now()))
        db.execute('UPDATE artifacts SET contract_id=? WHERE id=?', (cid, aid))
        db.execute('UPDATE contracts SET inputs_digest=? WHERE id=?', (hashlib.sha256(merkle.canonical(inputs)).hexdigest(), cid))
        if dataset_version_id:
            db.execute('UPDATE contracts SET dataset_version_id=? WHERE id=?', (dataset_version_id, cid))
            from .datasets import add_edge
            add_edge(db, principal.workspace, 'dataset_version', dataset_version_id, 'contract', cid, 'used_input')
        history.record(db, principal.workspace, principal.id, 'contract.created', 'contract', cid, {'kind': kind, 'version': 1, 'dataset_version_id': dataset_version_id})
        return cid

    def _reviewer(self, db, workspace, reviewer_id):
        row = db.execute("SELECT id FROM principals WHERE id=? AND workspace=? AND role='reviewer' AND revoked_at IS NULL",
                         (reviewer_id, workspace)).fetchone()
        if row is None:
            raise ServiceError('VALIDATION', 'reviewer_id')
        pub = db.execute("SELECT value FROM meta WHERE key=?", ('age_public:' + reviewer_id,)).fetchone()
        return pub['value'] if pub else None

    def get(self, db, principal, contract_id):
        principal.require('contract:read')
        row = db.execute('SELECT * FROM contracts WHERE id=? AND workspace=?', (contract_id, principal.workspace)).fetchone()
        if row is None:
            raise ServiceError('NOT_FOUND', 'contract')
        return row

    def update_draft(self, db, principal, contract_id, *, inputs=None, policy=None, title=None):
        row = self.get(db, principal, contract_id)
        if row['state'] != 'draft' or row['owner_id'] != principal.id:
            raise ServiceError('CONFLICT', 'only the owner may edit a draft; frozen contracts never change')
        if inputs is not None:
            validate_inputs(row['kind'], inputs)
            aid = self.store.store(db, workspace=principal.workspace, kind='draft_input', owner_id=principal.id,
                                   plaintext=merkle.canonical(inputs), recipients=[], intended_use='draft-input;owner-and-worker',
                                   contract_id=contract_id)
            db.execute('UPDATE contracts SET input_artifact_id=? WHERE id=?', (aid, contract_id))
        if policy is not None:
            pol = validate_policy(row['kind'], policy, self.default_capability)
            if pol['reviewer_id'] is not None:
                self._reviewer(db, principal.workspace, pol['reviewer_id'])
            db.execute('UPDATE contracts SET policy_json=?, reviewer_id=? WHERE id=?', (json.dumps(pol), pol['reviewer_id'], contract_id))
        if title is not None:
            if type(title) is not str or not 1 <= len(title) <= 128:
                raise ServiceError('VALIDATION', 'title')
            db.execute('UPDATE contracts SET title=? WHERE id=?', (title, contract_id))

    def freeze(self, db, principal, contract_id):
        """Commit the inputs (salted Merkle vault), build the immutable terms, pin them."""
        principal.require('contract:freeze')
        row = self.get(db, principal, contract_id)
        if row['state'] != 'draft' or row['owner_id'] != principal.id:
            raise ServiceError('CONFLICT', 'not a draft owned by the caller')
        pol = json.loads(row['policy_json'])
        if pol['reviewer_id'] is None:
            raise ServiceError('VALIDATION', 'reviewer_id required before freezing')
        reviewer_pub = self._reviewer(db, principal.workspace, pol['reviewer_id'])
        inputs = self.store.load_json(db, row['input_artifact_id'], principal.workspace)
        validate_inputs(row['kind'], inputs)
        receipt, vault = merkle.commit({'inputs': inputs})
        expires_at = now() + pol['expires_in_seconds']
        if row['kind'] == 'energy_audit':
            doc = terms.make(contract_id, receipt['root'], expires_at, actor=principal.id, amount=pol['amount'],
                             accepted_outcomes=tuple(pol['accepted_outcomes']), disclose_outcome=pol['disclose_outcome'],
                             disclose_explanation=pol['disclose_explanation'], capability=pol['capability'],
                             owner=principal.id, auditor=pol['reviewer_id'], retention_seconds=pol['retention_seconds'])
            digest = terms.digest(doc)
        else:
            doc = {'schema': SERVICE_CONTRACT_SCHEMA, 'kind': row['kind'], 'job_id': contract_id, 'workspace': principal.workspace,
                   'owner': principal.id, 'auditor': pol['reviewer_id'], 'input_root': receipt['root'],
                   'commitment_schema': merkle.SCHEMA, 'model_id': MODEL_IDS[row['kind']],
                   'verifier_id': VERIFIER_OF.get(row['kind'], ('service-science/v1', science.bundle_digest))[0],
                   'verifier_digest': VERIFIER_OF.get(row['kind'], ('service-science/v1', science.bundle_digest))[1](),
                   'accepted_outcomes': pol['accepted_outcomes'], 'disclose_outcome': pol['disclose_outcome'],
                   'expires_at': expires_at, 'retention_seconds': pol['retention_seconds'],
                   'lineage_id': row['lineage_id'], 'version': row['version']}
            digest = __import__('hashlib').sha256(b'metacoin/service-contract/v1\0' + merkle.canonical(doc)).hexdigest()
        aid = self.store.store(db, workspace=principal.workspace, kind='input_vault', owner_id=principal.id,
                               plaintext=merkle.canonical(vault), recipients=[reviewer_pub] if reviewer_pub else [],
                               intended_use='private-input-vault;worker-and-designated-reviewer', contract_id=contract_id,
                               retention_deadline=now() + pol['retention_seconds'])
        db.execute("UPDATE contracts SET state='frozen', contract_json=?, contract_digest=?, input_root=?, input_artifact_id=?, "
                   "expires_at=?, frozen_at=? WHERE id=? AND state='draft'",
                   (merkle.canonical(doc).decode(), digest, receipt['root'], aid, expires_at, now(), contract_id))
        history.record(db, principal.workspace, principal.id, 'contract.frozen', 'contract', contract_id,
                       {'contract_digest': digest, 'input_root': receipt['root'], 'kind': row['kind'], 'version': row['version']})
        return digest

    def amend(self, db, principal, contract_id, *, inputs=None, policy=None, title=None):
        """New draft version with explicit lineage; the frozen original is untouched."""
        principal.require('contract:amend')
        row = self.get(db, principal, contract_id)
        if row['owner_id'] != principal.id:
            raise ServiceError('FORBIDDEN', 'contract')
        base_inputs = inputs if inputs is not None else self.store.load_json(db, row['input_artifact_id'], principal.workspace)
        if row['state'] == 'frozen' and inputs is None:
            base_inputs = base_inputs['fields'] and None  # a frozen input vault is not a draft input
            raise ServiceError('VALIDATION', 'inputs required when amending a frozen contract')
        pol = json.loads(row['policy_json'])
        if policy is not None:
            pol.update(policy)
        new_id = self.create_draft(db, principal, kind=row['kind'], title=title or row['title'], inputs=base_inputs, policy=pol)
        db.execute('UPDATE contracts SET version=?, lineage_id=?, previous_id=? WHERE id=?',
                   (row['version'] + 1, row['lineage_id'], contract_id, new_id))
        history.record(db, principal.workspace, principal.id, 'contract.amended', 'contract', new_id,
                       {'previous': contract_id, 'version': row['version'] + 1})
        return new_id

    def public_view(self, row):
        pol = json.loads(row['policy_json'])
        return {'id': row['id'], 'kind': row['kind'], 'state': row['state'], 'version': row['version'],
                'lineage_id': row['lineage_id'], 'previous_id': row['previous_id'], 'title': row['title'],
                'policy': pol, 'contract_digest': row['contract_digest'], 'input_root': row['input_root'],
                'reviewer_id': row['reviewer_id'], 'expires_at': row['expires_at'], 'created_at': row['created_at'],
                'dataset_version_id': row['dataset_version_id'] if 'dataset_version_id' in row.keys() else None,
                'frozen_at': row['frozen_at'], 'terms': merkle.parse(row['contract_json']) if row['contract_json'] else None}
