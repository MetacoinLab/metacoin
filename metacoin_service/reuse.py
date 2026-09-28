"""Result reuse: an explicit, deterministic cache keyed by (workspace, kind, canonical inputs digest, verifier digest).

A submission that opts in (`reuse: true`) and hits the cache is committed immediately as a job that
points at the original evidence (`reused_from`). Nothing is recomputed, nothing is re-signed: the
evidence vault still binds the original contract, which the view says plainly, and reviews live on
the original job. A cache entry is unusable once the original evidence payload was deleted, and a
different installed verifier digest is a different key, so an upgraded verifier never serves stale
results. Reused jobs meter at zero (no computation happened).
"""
import hashlib
import json
import secrets
from experiments.private_receipts import receipt as merkle
from experiments.work_contracts import contract as terms
from . import history, science, temporal
from .datasets import add_edge
from .db import now
from .errors import ServiceError


def inputs_digest(inputs):
    return hashlib.sha256(merkle.canonical(inputs)).hexdigest()


def verifier_digest_for(kind):
    from .compute import manifests as compute_manifests
    if kind in compute_manifests.KINDS:
        return compute_manifests.implementation_digest()
    from .models import engine as model_engine
    if kind in model_engine.KINDS:
        return model_engine.implementation_digest()
    return {'energy_audit': terms.verifier_digest, 'temporal_energy': temporal.bundle_digest}.get(kind, science.bundle_digest)()


def record(db, job_row, contract_row):
    """Called on success; first result for a key wins (INSERT OR IGNORE keeps the earliest evidence)."""
    if not contract_row['inputs_digest']:
        return None
    db.execute('INSERT OR IGNORE INTO result_cache (workspace, kind, inputs_digest, verifier_digest, job_id, evidence_root, outcome, created_at) VALUES (?,?,?,?,?,?,?,?)',
               (job_row['workspace'], job_row['kind'], contract_row['inputs_digest'], verifier_digest_for(job_row['kind']), job_row['id'], job_row['evidence_root'], job_row['outcome'], now()))
    return True


def lookup(db, principal, contract_row):
    """A usable hit for this contract, or a dict explaining why there is none. Never creates anything."""
    principal.require('job:read')
    if contract_row['workspace'] != principal.workspace:
        raise ServiceError('NOT_FOUND', 'contract')
    if not contract_row['inputs_digest']:
        return {'hit': None, 'reason': 'contract predates the reuse index'}
    vd = verifier_digest_for(contract_row['kind'])
    row = db.execute('SELECT * FROM result_cache WHERE workspace=? AND kind=? AND inputs_digest=? AND verifier_digest=?',
                     (principal.workspace, contract_row['kind'], contract_row['inputs_digest'], vd)).fetchone()
    if row is None:
        return {'hit': None, 'reason': 'no succeeded job with identical canonical inputs under the installed verifier', 'verifier_digest': vd}
    job = db.execute('SELECT j.*, a.deleted_at AS evidence_deleted FROM jobs j LEFT JOIN artifacts a ON a.id=j.evidence_artifact_id WHERE j.id=?', (row['job_id'],)).fetchone()
    if job is None or job['state'] != 'succeeded':
        return {'hit': None, 'reason': 'cached job no longer succeeded'}
    if job['evidence_deleted'] is not None:
        return {'hit': None, 'reason': 'original evidence payload deleted under retention; recompute required', 'original_job_id': job['id']}
    if job['contract_id'] == contract_row['id']:
        return {'hit': None, 'reason': 'the contract already has its own job'}
    return {'hit': {'job_id': job['id'], 'evidence_root': job['evidence_root'], 'outcome': job['outcome'], 'review_state': job['review_state'],
                    'contract_id': job['contract_id'], 'finished_at': job['finished_at']},
            'verifier_digest': vd, 'basis': 'identical canonical inputs digest and identical installed verifier digest'}


def submit_reused(db, principal, contract_row, hit, settings, batch_id=None):
    original = db.execute('SELECT * FROM jobs WHERE id=?', (hit['job_id'],)).fetchone()
    jid = 'j_' + secrets.token_hex(8)
    db.execute('INSERT INTO jobs (id, workspace, contract_id, kind, state, retries_left, submitted_by, created_at, updated_at, batch_id, '
               'evidence_artifact_id, evidence_root, outcome, summary_json, finished_at, reused_from) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
               (jid, principal.workspace, contract_row['id'], contract_row['kind'], 'succeeded', 0, principal.id, now(), now(), batch_id,
                original['evidence_artifact_id'], original['evidence_root'], original['outcome'], original['summary_json'], now(), original['id']))
    history.record(db, principal.workspace, principal.id, 'job.queued', 'job', jid, {'contract_id': contract_row['id'], 'contract_digest': contract_row['contract_digest'],
                                                                                    'kind': contract_row['kind'], 'batch_id': batch_id, 'reused_from': original['id']})
    history.record(db, principal.workspace, principal.id, 'job.result_committed', 'job', jid, {'reused_from': original['id'], 'evidence_root': original['evidence_root'], 'recomputed': False})
    add_edge(db, principal.workspace, 'contract', contract_row['id'], 'job', jid, 'used_input')
    add_edge(db, principal.workspace, 'job', original['id'], 'job', jid, 'reused_result')
    from . import metering
    metering.record_for_job(settings, db, db.execute('SELECT * FROM jobs WHERE id=?', (jid,)).fetchone())
    return jid
