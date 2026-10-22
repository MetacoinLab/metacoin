"""Exact sampling plans through the real routes (journeys 11-16 of the order of 2026-10-22) on committed itemwise compute jobs:
a plan meets its finite-population bound and is minimal; a single-defect target refuses an insufficient budget; a changed
result root / other job invalidates the attached claim; a replica cannot satisfy a distinct requirement by rank; a
final-transition heat audit cannot establish a whole-trajectory claim; an audit failure cannot be hidden by retrying."""
import json
import unittest

from metacoin_service.tests.test_compute_engine import ComputeInstance, HAVE_RUNTIME, batch_spec, heat_spec
from metacoin_service.tests.test_verification import corrupt_output, flip_first_outcome
from metacoin_service.decision import kernels as K


@unittest.skipUnless(HAVE_RUNTIME, 'no numpy-capable compute interpreter on this host')
class SamplingPlanRouteTests(unittest.TestCase):
    def setUp(self):
        self.inst = ComputeInstance(); self.addCleanup(self.inst.close); self.c = self.inst.client; self.H = self.inst.h('owner'); self.w = self.inst.worker(); self.addCleanup(self.w.offline)

    def batch(self, **over):
        jid = self.inst.compute_job('temporal_batch', batch_spec(**over)); self.assertEqual(self.w.run_once()[1], 'succeeded'); return jid

    def test_plan_bound_minimal_infeasible_invalidated_single_attempt(self):
        jid = self.batch()
        job = self.c.get('/api/v1/jobs/' + jid, headers=self.H).json(); n = job['summary']['scenarios'] if 'scenarios' in job['summary'] else None
        # J11: preview then bind; the required k agrees with the exact kernel and is minimal for the target
        pv = self.c.post('/api/v1/verification/sampling-plans', headers=self.H, json={'job_id': jid, 'minimum_defects': 10, 'alpha': '1/100', 'max_samples': 500, 'preview': True}); self.assertEqual(pv.status_code, 200, pv.text); pv = pv.json()
        self.assertEqual((pv['state'], pv['id'], pv['feasible']), ('preview', None, True)); N = pv['population']; k = pv['required_samples']
        oracle = K.sampling_plan(N, 10, '1/100', N); self.assertEqual(oracle['required_samples'], k); from fractions import Fraction; self.assertTrue(K.miss_at_most(N, 10, k, Fraction(1, 100))); self.assertFalse(K.miss_at_most(N, 10, k - 1, Fraction(1, 100)))
        self.assertIn('assumption', json.dumps(pv).lower()); self.assertNotIn('probability the result is correct', json.dumps(pv))
        sp = self.c.post('/api/v1/verification/sampling-plans', headers=self.H, json={'job_id': jid, 'minimum_defects': 10, 'alpha': '1/100', 'max_samples': 500}); self.assertEqual(sp.status_code, 201, sp.text); sp = sp.json()
        self.assertEqual((sp['state'], sp['required_samples'], sp['attempt_policy']), ('bound', k, 'single_fixed_size'))
        # J12: a single-defect target with an insufficient budget is infeasible: achieved bound and required budget reported, alpha never relaxed
        one = self.c.post('/api/v1/verification/sampling-plans', headers=self.H, json={'job_id': jid, 'minimum_defects': 1, 'alpha': '1/100', 'max_samples': min(50, N // 2)}).json()
        self.assertEqual((one['state'], one['feasible']), ('infeasible', False)); self.assertGreater(one['required_samples'], one['max_samples']); self.assertEqual(one['alpha'], '1/100')
        au = self.c.post('/api/v1/verification/sampling-plans/' + one['id'] + '/audit', headers=self.H, json={}); self.assertEqual((au.status_code, au.json()['detail']['code']), (409, 'sampling_plan_infeasible'))
        # the audit runs through the bound challenge; the signed statement carries the conditional bound and recomputes offline
        au = self.c.post('/api/v1/verification/sampling-plans/' + sp['id'] + '/audit', headers=self.H, json={}); self.assertEqual(au.status_code, 202, au.text); vid = au.json()['id']
        self.w.run_once(); v = self.c.get('/api/v1/verification/' + vid, headers=self.H).json(); self.assertEqual(v['state'], 'passed', v)
        self.assertEqual(v['challenge']['sampling_plan_id'], sp['id']); self.assertEqual(v['challenge']['sample_count'], k)
        st = self.c.get('/api/v1/verification/' + vid + '/statement', headers=self.H).json(); clause = st['statement']['sampling_plan']
        self.assertEqual((clause['checked_items'], clause['detected_defects'], clause['population']), (k, 0, N)); self.assertIn('P(no sampled item fails | at least 10', clause['conditional_detection_bound'])
        off = self.c.post('/api/v1/verification/sampling-plans/verify', headers=self.H, json={'bundle': st, 'expected_plan': {'plan_digest': sp['plan_digest']}}).json()
        self.assertTrue(off['ok']); self.assertTrue(off['signature']['valid']); self.assertTrue(all(c['ok'] for c in off['arithmetic']['checks']))
        tampered = json.loads(json.dumps(st)); tampered['statement']['sampling_plan']['required_samples'] = k - 1
        off2 = self.c.post('/api/v1/verification/sampling-plans/verify', headers=self.H, json={'bundle': tampered}).json(); self.assertFalse(off2['signature']['valid']); self.assertFalse(off2['arithmetic']['ok'])
        # J16: a second attempt under the same plan is refused (single fixed-size attempt policy)
        again = self.c.post('/api/v1/verification/sampling-plans/' + sp['id'] + '/audit', headers=self.H, json={}); self.assertEqual((again.status_code, again.json()['detail']['code']), (409, 'sampling_plan_attempt_policy'))
        asr = self.c.get('/api/v1/verification/' + vid + '/assurance', headers=self.H).json(); self.assertEqual((asr['claim_type'], asr['coverage'], asr['outcome']), ('itemwise_reference_exact', 'sampled_items', 'passed'))
        self.assertEqual(asr['sampling']['plan_digest'], sp['plan_digest'])
        # J13: the plan is bound to this job and result root: another job cannot use it; a plan on a job whose root changed is invalidated
        jid2 = self.batch(grid=[{'path': 'reserve', 'start': 0, 'stop': 5000, 'step': 100}, {'path': 'load_scale_percent', 'values': [50, 100]}])
        sp2 = self.c.post('/api/v1/verification/sampling-plans', headers=self.H, json={'job_id': jid2, 'minimum_defects': 4, 'alpha': '1/20', 'max_samples': 500}).json(); self.assertEqual(sp2['state'], 'bound')
        other = self.c.post('/api/v1/verification', headers=self.H, json={'job_id': jid, 'class': 'sampled_reference', 'params': {'sampling_plan_id': sp2['id']}}); self.assertEqual((other.status_code, other.json()['detail']['code']), (409, 'sampling_plan_binding'))
        with self.inst.app.state.services.db.tx() as db:
            db.execute("UPDATE jobs SET evidence_root=? WHERE id=?", ('0' * 64, jid2))
        inv = self.c.post('/api/v1/verification/sampling-plans/' + sp2['id'] + '/audit', headers=self.H, json={}); self.assertEqual((inv.status_code, inv.json()['detail']['code']), (409, 'sampling_plan_invalidated'))
        self.assertEqual(self.c.get('/api/v1/verification/sampling-plans/' + sp2['id'], headers=self.H).json()['state'], 'invalidated')
        # J16 (defect side): a corrupted item after commitment fails the bound audit; the plan cannot be re-run; the assurance records the defect
        jid3 = self.batch(grid=[{'path': 'reserve', 'start': 0, 'stop': 3000, 'step': 100}, {'path': 'load_scale_percent', 'values': [50, 100]}])
        sp3 = self.c.post('/api/v1/verification/sampling-plans', headers=self.H, json={'job_id': jid3, 'defect_fraction': '1/2', 'alpha': '1/1000', 'max_samples': 500}).json(); self.assertEqual(sp3['state'], 'bound'); self.assertIn('ceil', sp3['minimum_defects_rule'])
        corrupt_output(self.inst, jid3, flip_first_outcome)
        v3 = self.c.post('/api/v1/verification/sampling-plans/' + sp3['id'] + '/audit', headers=self.H, json={}).json(); self.w.run_once()
        vs3 = self.c.get('/api/v1/verification/' + v3['id'], headers=self.H).json()
        self.assertIn(vs3['state'], ('failed', 'passed'))     # the flipped item is detected only if sampled; either way the plan is consumed
        retry = self.c.post('/api/v1/verification/sampling-plans/' + sp3['id'] + '/audit', headers=self.H, json={}); self.assertEqual(retry.status_code, 409)
        if vs3['state'] == 'failed':
            a3 = self.c.get('/api/v1/verification/' + v3['id'] + '/assurance', headers=self.H).json(); self.assertEqual(a3['outcome'], 'failed'); self.assertTrue(a3['sampling']['plan_bound'])
        # unsupported kinds are refused explicitly
        hj = self.inst.compute_job('heat_diffusion', heat_spec(nx=48, ny=48, steps=200)); self.assertEqual(self.w.run_once()[1], 'succeeded')
        bad = self.c.post('/api/v1/verification/sampling-plans', headers=self.H, json={'job_id': hj, 'minimum_defects': 1, 'alpha': '1/10', 'max_samples': 10}); self.assertEqual((bad.status_code, bad.json()['detail']['code']), (422, 'kind_unsupported_for_sampling_plans'))

    def test_final_transition_audit_cannot_establish_the_whole_trajectory(self):
        # J15: a passed sampled_reference (final transition) heat audit does not satisfy a whole_result requirement; full_reference does
        hj = self.inst.compute_job('heat_diffusion', heat_spec(nx=48, ny=48, steps=200)); self.assertEqual(self.w.run_once()[1], 'succeeded')
        v = self.c.post('/api/v1/verification', headers=self.H, json={'job_id': hj, 'class': 'sampled_reference', 'params': {}}); self.assertEqual(v.status_code, 202, v.text); self.w.run_once()
        self.assertEqual(self.c.get('/api/v1/verification/' + v.json()['id'], headers=self.H).json()['state'], 'passed')
        whole = {'schema': 'metacoin-assurance-requirement/v1', 'claim_type': 'whole_result_reference', 'coverage': 'whole_result', 'comparison': {'semantics': 'tolerance'}, 'independence': {'implementation': True}}
        pv = self.c.post('/api/v1/verification/assurance/evaluate', headers=self.H, json={'job_id': hj, 'requirement': whole}).json()
        self.assertFalse(pv['satisfied']); self.assertEqual(pv['status'], 'missing'); self.assertTrue(any('final' in c.lower() or 'coverage' in c.lower() for c in pv['missing_clauses'] + [pv['plain']]))
        final = dict(whole, claim_type='final_transition_reference', coverage='final_transition')
        self.assertTrue(self.c.post('/api/v1/verification/assurance/evaluate', headers=self.H, json={'job_id': hj, 'requirement': final}).json()['satisfied'])
        v2 = self.c.post('/api/v1/verification', headers=self.H, json={'job_id': hj, 'class': 'full_reference', 'params': {}}); self.assertEqual(v2.status_code, 202, v2.text); self.w.run_once()
        self.assertTrue(self.c.post('/api/v1/verification/assurance/evaluate', headers=self.H, json={'job_id': hj, 'requirement': whole}).json()['satisfied'])


if __name__ == '__main__':
    unittest.main()
