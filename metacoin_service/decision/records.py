"""Database-facing helpers that turn stored verification records into assurance statements (the pure logic is in
`assurance`; this module only reads rows)."""
import json

from . import assurance


def _table(db, name):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def sampling_clause(db, vrow):
    """The sampling plan bound to a sampled_reference verification (None when no plan was bound before the challenge)."""
    if not _table(db, 'sampling_plans'):
        return None
    params = json.loads(vrow['params_json'] or '{}')
    pid = params.get('sampling_plan_id')
    if not pid:
        return None
    plan = db.execute('SELECT * FROM sampling_plans WHERE id=?', (pid,)).fetchone()
    if plan is None:
        return None
    attempts = db.execute("SELECT id, state FROM verification_jobs WHERE target_job_id=? AND class='sampled_reference' AND params_json LIKE ? ORDER BY created_at", (vrow['target_job_id'], '%"sampling_plan_id": "' + pid + '"%')).fetchall()
    return {'plan_id': pid, 'plan_digest': plan['plan_digest'], 'population': plan['population'], 'minimum_defects': plan['minimum_defects'], 'alpha': plan['alpha'], 'required_samples': plan['required_samples'], 'feasible': bool(plan['feasible']),
            'achieved_bound_exact': plan['achieved_bound_exact'], 'frame_digest': plan['frame_digest'], 'checker_digest': plan['checker_digest'], 'attempt_policy': plan['attempt_policy'],
            'attempts': [{'verification_id': a['id'], 'state': a['state']} for a in attempts], 'failed_attempts': [a['id'] for a in attempts if a['state'] in ('failed', 'disputed', 'resolved')], 'bound_at': plan['created_at']}


def statements_for_job(db, job, contract, provider_execution='local_worker'):
    """Assurance statements of every verification of a job's committed evidence root (all states; the evaluator sorts them)."""
    out = []
    for v in db.execute('SELECT * FROM verification_jobs WHERE target_job_id=? ORDER BY created_at', (job['id'],)).fetchall():
        vd = dict(v)
        backends = None
        if v['class'] == 'replica':
            tr = db.execute('SELECT selected_backend FROM compute_runs WHERE job_id=?', (job['id'],)).fetchone()
            rr = db.execute('SELECT selected_backend FROM compute_runs WHERE job_id=?', (v['replica_job_id'],)).fetchone() if v['replica_job_id'] else None
            backends = [tr['selected_backend'] if tr else None, rr['selected_backend'] if rr else None]
        out.append(assurance.statement_from_verification(vd, dict(job), dict(contract), provider_execution, sampling=sampling_clause(db, v), replica_backends=backends))
    return out


def binding_for(job, contract):
    doc = json.loads(contract['contract_json']) if contract['contract_json'] else {}
    params = json.loads(contract['params_json'] or '{}') if 'params_json' in contract.keys() else {}
    return {'kind': job['kind'], 'model_id': doc.get('model_id'), 'model_version': params.get('manifest_version'), 'input_root': contract['input_root'], 'evidence_root': job['evidence_root']}
