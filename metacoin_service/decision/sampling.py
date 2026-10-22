"""Exact sampling plans bound BEFORE the challenge (Deliverable B).

A plan freezes the committed population (N = the target job's committed item count, frame digest = sha256 over the
result commitment and N with the index ordering of the committed array), the defect assumption m (or an exact rational
fraction with the documented ceiling rule), the tolerated miss probability alpha, the check budget, the checker identity
and the fault model, and computes the smallest k with C(N-m,k)/C(N,k) <= alpha by exact integer comparison. The audit is
then issued through the existing commitment-first challenge path with the plan bound into the challenge record; the
attempt policy is single fixed-size (one challenge per plan). The signed verification statement carries the conditional
detection bound; `verify_statement_offline` recomputes the arithmetic from the statement alone and reports signature
verification separately."""
import hashlib
import json
import secrets
from fractions import Fraction

from experiments.private_receipts import receipt as merkle
from .. import history
from ..db import now
from ..errors import ServiceError
from . import assurance, kernels as K

SUPPORTED_KINDS = {'temporal_batch': 'one committed scenario row of results.npy (index i in 0..N-1), recomputed exactly by temporal-energy/v1'}


class Sampling:
    def __init__(self, settings, services):
        self.settings, self.svc = settings, services

    def _target(self, db, principal, job_id):
        job = db.execute('SELECT * FROM jobs WHERE id=? AND workspace=?', (job_id, principal.workspace)).fetchone()
        if job is None:
            raise ServiceError('NOT_FOUND', 'job')
        if job['state'] != 'succeeded':
            raise ServiceError('CONFLICT', 'target job has no committed result')
        if job['kind'] not in SUPPORTED_KINDS:
            raise ServiceError('VALIDATION', {'code': 'kind_unsupported_for_sampling_plans', 'kind': job['kind'], 'supported': sorted(SUPPORTED_KINDS), 'note': 'itemwise reference checks with explicit item boundaries only'})
        run = db.execute('SELECT work_total FROM compute_runs WHERE job_id=?', (job['id'],)).fetchone()
        return job, (run['work_total'] if run else None)

    def create(self, db, principal, body, preview=False):
        principal.require('verification:submit')
        if type(body) is not dict or set(body) - {'job_id', 'minimum_defects', 'defect_fraction', 'alpha', 'max_samples', 'preview'}:
            raise ServiceError('VALIDATION', {'code': 'fields', 'allowed': ['job_id', 'minimum_defects | defect_fraction', 'alpha', 'max_samples']})
        job, n = self._target(db, principal, body.get('job_id'))
        if not n:
            raise ServiceError('CONFLICT', 'population size unknown for this job')
        cap = body.get('max_samples', self.svc.verification.__class__.__module__ and 4096)
        from .. import verification as verification_mod
        cap = min(cap if type(cap) is int else 4096, verification_mod.MAX_WORK['sampled_reference'], n)
        frame = assurance.frame_digest(job['evidence_root'], n)
        checker = verification_mod.implementation_digest()
        try:
            plan = K.sampling_plan(n, body.get('minimum_defects'), body.get('alpha'), cap, frame_digest=frame, checker_digest=checker, sample_unit=SUPPORTED_KINDS[job['kind']], defect_fraction=body.get('defect_fraction'))
        except K.KernelInvalid as exc:
            raise ServiceError('VALIDATION', {'code': exc.code, **exc.detail})
        plan.update({'target_job_id': job['id'], 'kind': job['kind'], 'result_commitment': job['evidence_root'], 'attempt_policy': 'single_fixed_size', 'challenge_model': verification_mod.CHALLENGE_POLICY,
                     'randomness': 'server-side CSPRNG seed drawn after the commitment; ranks by sha256(seed || commitment || index): uniform without replacement under a computational-pseudorandomness / random-oracle assumption; not independent of a malicious server',
                     'budget_note': 'max_samples is capped by the verification work bound (%d) and by N' % verification_mod.MAX_WORK['sampled_reference']})
        plan['plan_digest'] = hashlib.sha256(b'metacoin/sampling-plan/v1\0' + merkle.canonical({k: v for k, v in plan.items() if k not in ('plan_digest',)})).hexdigest()
        if preview or body.get('preview'):
            return dict(plan, id=None, state='preview', note='not stored; create the plan to bind it before issuing the challenge')
        pid = 'sp_' + secrets.token_hex(6)
        db.execute('INSERT INTO sampling_plans VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   (pid, principal.workspace, job['id'], job['kind'], job['evidence_root'], n, frame, plan['minimum_defects'], plan['minimum_defects_rule'], plan['alpha'], plan['max_samples'], plan['required_samples'], 1 if plan['feasible'] else 0,
                    plan['achieved_bound']['exact'], checker, plan['fault_model'], plan['sample_unit'], 'single_fixed_size', json.dumps(plan), plan['plan_digest'], 'bound' if plan['feasible'] else 'infeasible', None, principal.id, now(), now()))
        history.record(db, principal.workspace, principal.id, 'verification.requested', 'sampling_plan', pid, {'target_job_id': job['id'], 'population': n, 'minimum_defects': plan['minimum_defects'], 'alpha': plan['alpha'], 'required_samples': plan['required_samples'], 'feasible': plan['feasible']})
        from ..datasets import add_edge
        add_edge(db, principal.workspace, 'job', job['id'], 'sampling_plan', pid, 'sampling_plan_bound')
        return self.view(db, principal, pid)

    def row(self, db, principal, pid):
        r = db.execute('SELECT * FROM sampling_plans WHERE id=? AND workspace=?', (pid, principal.workspace)).fetchone()
        if r is None:
            raise ServiceError('NOT_FOUND', 'sampling plan')
        return r

    def view(self, db, principal, pid):
        principal.require('job:read')
        r = self.row(db, principal, pid)
        plan = json.loads(r['plan_json'])
        attempts = db.execute("SELECT id, state, created_at FROM verification_jobs WHERE target_job_id=? AND params_json LIKE ? ORDER BY created_at", (r['target_job_id'], '%"sampling_plan_id": "' + pid + '"%')).fetchall()
        return dict(plan, id=pid, state=r['state'], verification_id=r['verification_id'], created_by=r['created_by'], created_at=r['created_at'], attempts=[dict(a) for a in attempts],
                    invalidation='bound to job %s and result commitment %s; any other job, frame, model or result root cannot use it' % (r['target_job_id'], r['result_commitment'][:16]))

    def list(self, db, principal, job_id=None):
        principal.require('job:read')
        rows = db.execute('SELECT id FROM sampling_plans WHERE workspace=?' + (' AND target_job_id=?' if job_id else '') + ' ORDER BY created_at DESC LIMIT 100', (principal.workspace,) + ((job_id,) if job_id else ())).fetchall()
        return [self.view(db, principal, r['id']) for r in rows]

    def audit(self, db, principal, pid):
        """Issue the bound challenge through the verification service (single attempt per plan)."""
        r = self.row(db, principal, pid)
        job = db.execute('SELECT * FROM jobs WHERE id=?', (r['target_job_id'],)).fetchone()
        if job['evidence_root'] != r['result_commitment']:
            db.execute("UPDATE sampling_plans SET state='invalidated', updated_at=? WHERE id=?", (now(), pid))
            raise ServiceError('CONFLICT', {'code': 'sampling_plan_invalidated', 'note': 'the result root changed since the plan was bound'})
        v = self.svc.verification.request(db, principal, r['target_job_id'], 'sampled_reference', {'sampling_plan_id': pid})
        return dict(v, sampling_plan_id=pid, plan_digest=r['plan_digest'])

    @staticmethod
    def verify_statement_offline(bundle, expected_plan=None):
        """Recompute the sampling arithmetic of a signed verification statement; signature and science are reported separately."""
        from .. import crypto
        st = (bundle or {}).get('statement') or {}
        out = {'signature': None, 'bindings': None, 'arithmetic': None, 'ok': False}
        try:
            sig_ok = crypto.verify(bundle['public_key_hex'], merkle.canonical(st), bundle['signature_hex'])
        except Exception:
            sig_ok = False
        out['signature'] = {'valid': bool(sig_ok), 'key_id': bundle.get('key_id'), 'meaning': 'authenticates the issuer; says nothing about the science'}
        sp = st.get('sampling_plan')
        if not sp:
            out['arithmetic'] = {'performed': False, 'reason': 'no sampling plan bound to this statement'}
            return out
        checks = []
        try:
            n, m, a, k = int(sp['population']), int(sp['minimum_defects_assumed']), Fraction(sp['alpha']), int(sp['required_samples'])
            plan = K.sampling_plan(n, m, str(a), n)
            checks.append({'check': 'required_samples_recomputed', 'ok': plan['required_samples'] == k, 'detail': {'recomputed': plan['required_samples'], 'stated': k}})
            checks.append({'check': 'bound_met_at_k', 'ok': K.miss_at_most(n, m, k, a)})
            checks.append({'check': 'k_minimal', 'ok': k == 0 or not K.miss_at_most(n, m, k - 1, a)})
            if sp.get('achieved_bound_exact'):
                checks.append({'check': 'achieved_bound_exact_recomputed', 'ok': str(K.miss_probability(n, m, k)) == sp['achieved_bound_exact']})
            checks.append({'check': 'checked_items_equals_k', 'ok': sp.get('checked_items') == k and (st.get('scope') or {}).get('checked') == k})
            checks.append({'check': 'frame_digest_binds_commitment_and_population', 'ok': assurance.frame_digest(st.get('result_commitment'), n) == sp.get('frame_digest')})
            checks.append({'check': 'no_detected_defects_on_pass', 'ok': (st.get('outcome') != 'passed') or sp.get('detected_defects') == 0})
            if expected_plan:
                checks.append({'check': 'plan_digest_matches_expected', 'ok': expected_plan.get('plan_digest') == sp.get('plan_digest')})
        except (K.KernelInvalid, ValueError, TypeError, KeyError) as exc:
            checks.append({'check': 'arithmetic', 'ok': False, 'detail': type(exc).__name__})
        out['arithmetic'] = {'performed': True, 'ok': all(c['ok'] for c in checks), 'checks': checks, 'claim': sp.get('conditional_detection_bound')}
        out['bindings'] = {'target_job_id': st.get('target_job_id'), 'result_commitment': st.get('result_commitment'), 'class': st.get('class'), 'outcome': st.get('outcome')}
        out['ok'] = bool(sig_ok) and out['arithmetic']['ok']
        return out
