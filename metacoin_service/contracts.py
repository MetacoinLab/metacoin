"""Drafts change; a frozen contract never does. An amendment is a new version with lineage."""
import json
import secrets
from experiments.private_receipts import receipt as merkle
from experiments.work_contracts import contract as terms, energy_analysis as energy
from . import history, science
from .db import now
from .errors import ServiceError

KINDS = ('energy_audit', 'safe_runtime', 'plan_comparison', 'task_selection')
DEFAULT_EXPIRY_SECONDS = 7 * 86400
SERVICE_CONTRACT_SCHEMA = 'metacoin-service-contract/v1'
VALIDATORS = {'energy_audit': energy.validate, 'safe_runtime': science.validate_safe_runtime,
              'plan_comparison': science.validate_comparison, 'task_selection': science.validate_selection}
MODEL_IDS = {'safe_runtime': science.SAFE_RUNTIME_MODEL, 'plan_comparison': science.COMPARISON_MODEL,
             'task_selection': science.SELECTION_MODEL}


def validate_policy(kind, policy):
    merkle.canonical(policy)
    allowed = {'accepted_outcomes', 'disclose_outcome', 'disclose_explanation', 'amount', 'capability',
               'expires_in_seconds', 'retention_seconds', 'reviewer_id'}
    if type(policy) is not dict or not set(policy) <= allowed:
        raise ServiceError('VALIDATION', 'policy fields')
    out = {'accepted_outcomes': list(energy.OUTCOMES), 'disclose_outcome': True, 'disclose_explanation': False,
           'amount': 1, 'capability': 'legacy_simulation', 'expires_in_seconds': DEFAULT_EXPIRY_SECONDS,
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
    def __init__(self, store):
        self.store = store

    def create_draft(self, db, principal, *, kind, title, inputs, policy):
        principal.require('contract:create')
        if type(title) is not str or not 1 <= len(title) <= 128:
            raise ServiceError('VALIDATION', 'title')
        validate_inputs(kind, inputs)
        pol = validate_policy(kind, policy)
        if pol['reviewer_id'] is not None:
            self._reviewer(db, principal.workspace, pol['reviewer_id'])
        cid = 'ct_' + secrets.token_hex(8)
        aid = self.store.store(db, workspace=principal.workspace, kind='draft_input', owner_id=principal.id,
                               plaintext=merkle.canonical(inputs), recipients=[], intended_use='draft-input;owner-and-worker',
                               contract_id=None)
        db.execute('INSERT INTO contracts (id, workspace, owner_id, kind, state, version, lineage_id, title, policy_json, params_json, '
                   'input_artifact_id, reviewer_id, created_at) VALUES (?,?,?,?,?,1,?,?,?,?,?,?,?)',
                   (cid, principal.workspace, principal.id, kind, 'draft', cid, title, json.dumps(pol), json.dumps({}),
                    aid, pol['reviewer_id'], now()))
        db.execute('UPDATE artifacts SET contract_id=? WHERE id=?', (cid, aid))
        history.record(db, principal.workspace, principal.id, 'contract.created', 'contract', cid, {'kind': kind, 'version': 1})
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
            pol = validate_policy(row['kind'], policy)
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
                   'verifier_id': 'service-science/v1', 'verifier_digest': science.bundle_digest(),
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
                'frozen_at': row['frozen_at'], 'terms': merkle.parse(row['contract_json']) if row['contract_json'] else None}
