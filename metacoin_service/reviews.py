"""Authorized private review with a signed, versioned decision envelope.

Custody model (stated, not hidden): the service holds each reviewer's Ed25519
key and signs only after the authenticated reviewer records a decision. This is
server-managed custody, not non-custodial review. Verification trusts only the
reviewer_keys table (active or rotated = historical), never a key carried in an
envelope. A decision certifies scientific acceptance; it confers no payment
authority by itself (that requires the explicit action path in actions.py).
"""
import hashlib
import json
import secrets
from experiments.private_receipts import receipt as merkle
from experiments.work_contracts import acceptance, contract as terms
from . import crypto, history, science, temporal
from .db import now
from .errors import ServiceError

ENVELOPE_SCHEMA = 'metacoin-review-envelope/v1'


def envelope_bytes(envelope):
    """Deterministic bytes: the experiments' canonical JSON (sorted keys, no floats,
    duplicate keys rejected on parse) under a domain prefix applied by crypto.sign."""
    merkle.canonical(envelope)
    required = ('schema', 'workspace', 'job_id', 'contract_id', 'contract_digest', 'input_root', 'evidence_root',
                'verifier_id', 'verifier_digest', 'model_id', 'kind', 'decision', 'reviewer_id', 'key_id',
                'accepted_outcomes', 'scientific_outcome', 'recomputation', 'decided_at', 'expires_at', 'nonce')
    if type(envelope) is not dict or set(envelope) != set(required) or envelope['schema'] != ENVELOPE_SCHEMA:
        raise ServiceError('VALIDATION', 'envelope fields')
    if envelope['decision'] not in ('accepted', 'rejected'):
        raise ServiceError('VALIDATION', 'decision')
    return merkle.canonical(envelope)


class Reviews:
    def __init__(self, store, settings, jobs):
        self.store, self.settings, self.jobs = store, settings, jobs

    def request(self, db, principal, job_id):
        principal.require('review:request')
        job = self.jobs.get(db, principal, job_id)
        if job['state'] != 'succeeded':
            raise ServiceError('CONFLICT', 'job has no committed result')
        if job['review_state'] != 'none':
            raise ServiceError('CONFLICT', 'review already requested or decided')
        db.execute("UPDATE jobs SET review_state='requested', updated_at=? WHERE id=?", (now(), job_id))
        contract = db.execute('SELECT reviewer_id FROM contracts WHERE id=?', (job['contract_id'],)).fetchone()
        history.record(db, principal.workspace, principal.id, 'review.requested', 'job', job_id, {'reviewer_id': contract['reviewer_id']})
        return contract['reviewer_id']

    def _assigned(self, db, principal, job_id):
        job = db.execute('SELECT * FROM jobs WHERE id=? AND workspace=?', (job_id, principal.workspace)).fetchone()
        if job is None:
            raise ServiceError('NOT_FOUND', 'job')
        contract = db.execute('SELECT * FROM contracts WHERE id=?', (job['contract_id'],)).fetchone()
        if principal.role != 'reviewer' or contract['reviewer_id'] != principal.id:
            raise ServiceError('FORBIDDEN', 'not the designated reviewer for this contract')
        if job['review_state'] not in ('requested', 'accepted', 'rejected'):
            raise ServiceError('CONFLICT', 'review not requested')
        return job, contract

    def evidence(self, db, principal, job_id):
        """Decrypt both vaults for the designated reviewer and RECOMPUTE. The
        decision page is built from this, never from the worker's summary."""
        principal.require('review:read_evidence')
        job, contract = self._assigned(db, principal, job_id)
        doc = merkle.parse(contract['contract_json'])
        input_vault = self.store.load_json(db, contract['input_artifact_id'], principal.workspace)
        evidence_vault = self.store.load_json(db, job['evidence_artifact_id'], principal.workspace)
        out = {'job_id': job_id, 'contract_id': contract['id'], 'contract_digest': contract['contract_digest'],
               'input_root': contract['input_root'], 'evidence_root': job['evidence_root'], 'kind': job['kind'],
               'verifier_id': doc['verifier_id'], 'verifier_digest': doc['verifier_digest'], 'model_id': doc['model_id'],
               'accepted_outcomes': doc['accepted_outcomes'], 'contract_terms': doc, 'review_state': job['review_state']}
        try:
            if job['kind'] == 'energy_audit':
                audited = acceptance.audit(doc, contract['contract_digest'], input_vault, evidence_vault)
                values = acceptance.full_values(evidence_vault, evidence_vault['receipt']['root'])
                out.update(recomputation='matches', scientific_outcome=audited['scientific_outcome'],
                           policy_satisfied=audited['work_completed'], private_details=values['audit_details'],
                           margin_explanation=values['margin_explanation'], verifier_status=terms.verifier_status(doc))
            else:
                values = acceptance.full_values(evidence_vault, evidence_vault['receipt']['root'])
                inputs = acceptance.full_values(input_vault, contract['input_root'])['inputs']
                fresh = {'safe_runtime': science.safe_runtime, 'plan_comparison': science.compare_plans, 'task_selection': science.select_tasks,
                         'temporal_energy': temporal.analyze}[job['kind']](inputs)
                expected_digest = temporal.bundle_digest() if job['kind'] == 'temporal_energy' else science.bundle_digest()
                matches = (merkle.canonical(fresh) == merkle.canonical(values['result'])
                           and values['contract_digest'] == contract['contract_digest']
                           and values['verifier_digest'] == expected_digest)
                out.update(recomputation='matches' if matches else 'mismatch', scientific_outcome=job['outcome'],
                           policy_satisfied=matches, private_details=values['result'], verifier_status='current')
        except merkle.Invalid as exc:
            out.update(recomputation='mismatch', scientific_outcome=None, policy_satisfied=False, private_details=None,
                       refusal=str(exc))
        return out

    def decide(self, db, principal, job_id, decision):
        principal.require('review:decide')
        if decision not in ('accepted', 'rejected'):
            raise ServiceError('VALIDATION', 'decision')
        job, contract = self._assigned(db, principal, job_id)
        existing = db.execute('SELECT * FROM reviews WHERE job_id=?', (job_id,)).fetchone()
        if existing is not None:
            if existing['decision'] == decision:
                return self.view(existing)          # idempotent identical decision
            raise ServiceError('CONFLICT', 'a different decision is already recorded; amend to a new contract version')
        evidence = self.evidence(db, principal, job_id)
        if decision == 'accepted' and (evidence['recomputation'] != 'matches' or not evidence['policy_satisfied']):
            # A button cannot manufacture acceptance when recomputation failed or policy is unmet.
            raise ServiceError('EVIDENCE_MISMATCH', 'recomputation did not match or policy not satisfied')
        key = db.execute("SELECT * FROM reviewer_keys WHERE principal_id=? AND status='active'", (principal.id,)).fetchone()
        if key is None:
            raise ServiceError('CAPABILITY_UNAVAILABLE', 'reviewer has no active signing key')
        doc = evidence['contract_terms']
        envelope = {'schema': ENVELOPE_SCHEMA, 'workspace': principal.workspace, 'job_id': job_id,
                    'contract_id': contract['id'], 'contract_digest': contract['contract_digest'],
                    'input_root': contract['input_root'], 'evidence_root': job['evidence_root'],
                    'verifier_id': doc['verifier_id'], 'verifier_digest': doc['verifier_digest'], 'model_id': doc['model_id'],
                    'kind': job['kind'], 'decision': decision, 'reviewer_id': principal.id, 'key_id': key['key_id'],
                    'accepted_outcomes': doc['accepted_outcomes'], 'scientific_outcome': evidence['scientific_outcome'],
                    'recomputation': evidence['recomputation'], 'decided_at': now(), 'expires_at': doc['expires_at'],
                    'nonce': secrets.token_hex(16)}
        message = envelope_bytes(envelope)
        signer = crypto.load_signing_key(self.settings.keys_dir / ('reviewer-' + principal.id + '.ed25519'))
        signature = crypto.sign(signer, message)
        digest = hashlib.sha256(message).hexdigest()
        bundle_id = None
        if decision == 'accepted' and job['kind'] == 'energy_audit':
            input_vault = self.store.load_json(db, contract['input_artifact_id'], principal.workspace)
            evidence_vault = self.store.load_json(db, job['evidence_artifact_id'], principal.workspace)
            # Records acceptance in the economic journal (the only path that can later authorize an action).
            audited = self.jobs.journal(db, principal.workspace).audit(contract['id'], input_vault, evidence_vault, principal.id, now())
            bundle_id = self.store.store(db, workspace=principal.workspace, kind='public_bundle', owner_id=contract['owner_id'],
                                         plaintext=merkle.canonical(audited['bundle']), recipients=[], intended_use='public-openings',
                                         job_id=job_id, contract_id=contract['id'], public=True)
        rid = 'rv_' + secrets.token_hex(8)
        db.execute('INSERT INTO reviews VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                   (rid, principal.workspace, job_id, principal.id, key['key_id'], decision, message.decode(), digest,
                    signature, bundle_id, now()))
        db.execute('UPDATE jobs SET review_state=?, updated_at=? WHERE id=?', (decision, now(), job_id))
        history.record(db, principal.workspace, principal.id, 'review.' + decision, 'job', job_id,
                       {'review_id': rid, 'envelope_digest': digest, 'key_id': key['key_id'], 'evidence_root': job['evidence_root']})
        from .datasets import add_edge
        add_edge(db, principal.workspace, 'job', job_id, 'review', rid, 'reviewed')
        return self.view(db.execute('SELECT * FROM reviews WHERE id=?', (rid,)).fetchone())

    @staticmethod
    def view(row):
        return {'review_id': row['id'], 'job_id': row['job_id'], 'decision': row['decision'], 'key_id': row['key_id'],
                'envelope': merkle.parse(row['envelope_json']), 'envelope_digest': row['envelope_digest'],
                'signature_hex': row['signature_hex'], 'public_bundle_artifact_id': row['public_bundle_artifact_id'],
                'created_at': row['created_at'], 'custody': 'server-managed reviewer key; signed after authenticated reviewer decision'}

    @staticmethod
    def verify(db, envelope, signature_hex, expected=None):
        """Verify against the independently stored trust table. `expected` may pin
        job/contract/root so a valid envelope replayed elsewhere is refused."""
        try:
            message = envelope_bytes(envelope)
        except ServiceError:
            return {'valid': False, 'reason': 'malformed envelope'}
        if len(message) > 64 * 1024 or type(signature_hex) is not str or len(signature_hex) != 128:
            return {'valid': False, 'reason': 'oversized or malformed signature'}
        key = db.execute('SELECT * FROM reviewer_keys WHERE key_id=?', (envelope['key_id'],)).fetchone()
        if key is None or key['principal_id'] != envelope['reviewer_id']:
            return {'valid': False, 'reason': 'unknown reviewer key (trust table)'}
        if not crypto.verify(key['public_key_hex'], message, signature_hex):
            return {'valid': False, 'reason': 'signature does not verify'}
        for field, value in (expected or {}).items():
            if envelope.get(field) != value:
                return {'valid': False, 'reason': 'envelope bound to a different ' + field}
        status = {'active': 'current', 'rotated': 'historical', 'revoked': 'revoked'}[key['status']]
        return {'valid': status != 'revoked', 'key_status': status, 'reason': None if status != 'revoked' else 'key revoked',
                'digest': hashlib.sha256(message).hexdigest(), 'authenticates': 'issuer and bound statement, not scientific truth'}
