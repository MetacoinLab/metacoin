"""Order 08 §76 extensions, second set: repeated procurement programs (1), verifier challenge packages (3) and versioned
pricing experiments (7). Each has a working user operation and evidence; none awards, pays or trusts anything implicitly."""
import io
import json
import unittest
import zipfile

from metacoin_service.tests.test_work_evidence import EvidenceBase
from metacoin_service.tests.test_work_terms import energy_inputs


class ProcurementProgramTests(EvidenceBase):
    def test_program_runs_need_fresh_inputs_and_offers_and_respect_the_aggregate(self):
        cls = self.c.post('/api/v1/work/terms', headers=self.H, json={'template': 'determination', 'ceiling': 5}).json()['terms']
        bad = self.c.post('/api/v1/work/programs', headers=self.H, json={'name': 'weekly energy checks', 'class_terms': cls, 'per_run_ceiling': 5, 'aggregate_ceiling': 4}); self.assertEqual(bad.status_code, 422)
        pg = self.c.post('/api/v1/work/programs', headers=self.H, json={'name': 'weekly energy checks', 'class_terms': cls, 'per_run_ceiling': 5, 'aggregate_ceiling': 12, 'max_runs': 4}); self.assertEqual(pg.status_code, 201, pg.text); pg = pg.json()
        self.assertEqual((pg['runs_used'], pg['aggregate_available']), (0, 12))
        r1 = self.c.post('/api/v1/work/programs/%s/runs' % pg['id'], headers=self.H, json={'inputs': dict(energy_inputs('FEASIBLE'), private_label='RUN1')}); self.assertEqual(r1.status_code, 201, r1.text); r1 = r1.json()
        self.assertEqual(r1['request']['state'], 'open'); self.assertEqual(r1['terms']['terms']['payment']['ceiling'], 5)
        same = self.c.post('/api/v1/work/programs/%s/runs' % pg['id'], headers=self.H, json={'inputs': dict(energy_inputs('FEASIBLE'), private_label='RUN1')}); self.assertEqual(same.status_code, 409); self.assertEqual(same.json()['detail']['code'], 'fresh_inputs_required')
        self.assertEqual(self.c.post('/api/v1/work/programs/%s/runs' % pg['id'], headers=self.H, json={'inputs': energy_inputs('FEASIBLE'), 'ceiling': 9}).status_code, 422)          # above the per-run ceiling
        # fresh offers per run: the provider offers on run 1's request; award reserves against the aggregate
        o1 = self.c.post('/api/v1/work/requests/' + r1['request']['id'] + '/offers', headers=self.pv['alpha']['h'], json={'price_amount': 5, 'asset': 'action-units', 'scheme': 'exact', 'window_seconds': 3600, 'verification': {'class': 'full_exact'}}).json()
        a1 = self.c.post('/api/v1/work/requests/' + r1['request']['id'] + '/award', headers=self.H, json={'offer_id': o1['id']}); self.assertEqual(a1.status_code, 201, a1.text)
        r2 = self.c.post('/api/v1/work/programs/%s/runs' % pg['id'], headers=self.H, json={'inputs': dict(energy_inputs('INFEASIBLE'), private_label='RUN2')}).json()
        o2 = self.c.post('/api/v1/work/requests/' + r2['request']['id'] + '/offers', headers=self.pv['beta']['h'], json={'price_amount': 5, 'asset': 'action-units', 'scheme': 'exact', 'window_seconds': 3600, 'verification': {'class': 'full_exact'}}).json()
        self.assertEqual(self.c.post('/api/v1/work/requests/' + r2['request']['id'] + '/award', headers=self.H, json={'offer_id': o2['id']}).status_code, 201)
        view = self.c.get('/api/v1/work/programs/' + pg['id'], headers=self.H).json(); self.assertEqual((view['runs_used'], view['aggregate_exposure'], view['aggregate_available']), (2, 10, 2))
        # a third run may only bind a ceiling that fits the remaining aggregate; the award itself re-checks inside its transaction
        self.assertEqual(self.c.post('/api/v1/work/programs/%s/runs' % pg['id'], headers=self.H, json={'inputs': dict(energy_inputs('INDETERMINATE'), private_label='RUN3'), 'ceiling': 5}).json()['detail']['code'], 'aggregate_ceiling')
        r3 = self.c.post('/api/v1/work/programs/%s/runs' % pg['id'], headers=self.H, json={'inputs': dict(energy_inputs('INDETERMINATE'), private_label='RUN3'), 'ceiling': 2}); self.assertEqual(r3.status_code, 201, r3.text); r3 = r3.json()
        o3 = self.c.post('/api/v1/work/requests/' + r3['request']['id'] + '/offers', headers=self.pv['alpha']['h'], json={'price_amount': 2, 'asset': 'action-units', 'scheme': 'exact', 'window_seconds': 3600, 'verification': {'class': 'full_exact'}}).json()
        self.c.post('/api/v1/work/programs/%s/close' % pg['id'], headers=self.H, json={})
        closed = self.c.post('/api/v1/work/requests/' + r3['request']['id'] + '/award', headers=self.H, json={'offer_id': o3['id']}); self.assertEqual(closed.status_code, 409); self.assertEqual(closed.json()['detail']['code'], 'program_state')
        self.assertEqual(self.c.post('/api/v1/work/programs/%s/runs' % pg['id'], headers=self.H, json={'inputs': energy_inputs('FEASIBLE')}).status_code, 409)
        self.assertEqual(self.c.post('/api/v1/work/programs', headers=self.pv['alpha']['h'], json={'name': 'x', 'class_terms': cls, 'per_run_ceiling': 1, 'aggregate_ceiling': 1}).status_code, 403)


class PricingExperimentTests(EvidenceBase):
    def test_fixed_vs_metered_on_a_synthetic_workload_changes_nothing_else(self):
        j0 = self.c.get('/api/v1/work/journal', headers=self.H).json(); svc0 = self.c.get('/api/v1/services', headers=self.H).json()
        wl = ['FEASIBLE'] * 4 + ['INFEASIBLE'] * 3 + ['INDETERMINATE'] * 2 + ['execution_failure'] + ['rejected_evidence']
        e = self.c.post('/api/v1/work/pricing-experiments', headers=self.H, json={'name': 'fixed-vs-metered', 'payment_rule': {'complete': 10, 'partial': 0, 'diagnostic': 5}, 'workload': wl, 'metered_bps': 6000}); self.assertEqual(e.status_code, 201, e.text); e = e.json()
        by = {r['policy']: r for r in e['results']}
        self.assertEqual(by['fixed']['per_outcome']['FEASIBLE'], by['fixed']['per_outcome']['INFEASIBLE']); self.assertEqual(by['fixed']['paid_for_failures'], 0)
        self.assertEqual(by['fixed']['total'], 4 * 10 + 3 * 10 + 2 * 5); self.assertEqual(by['metered']['per_outcome']['FEASIBLE'], 6); self.assertEqual(by['metered']['total'], 7 * 6 + 2 * 5)
        self.assertEqual(e['version'], 1); self.assertIn('assumptions', e['assumptions']['note'])
        e2 = self.c.post('/api/v1/work/pricing-experiments', headers=self.H, json={'name': 'fixed-vs-metered', 'workload': wl, 'metered_bps': 10000}).json(); self.assertEqual(e2['version'], 2)
        self.assertEqual(self.c.get('/api/v1/work/pricing-experiments/' + e['id'], headers=self.H).json()['results'], e['results'])
        self.assertEqual([x['version'] for x in self.c.get('/api/v1/work/pricing-experiments', headers=self.H).json()['items']], [1, 2])
        self.assertEqual(self.c.get('/api/v1/work/journal', headers=self.H).json(), j0); self.assertEqual(self.c.get('/api/v1/services', headers=self.H).json(), svc0)       # nothing awarded, no quote changed
        self.assertEqual(self.c.post('/api/v1/work/pricing-experiments', headers=self.H, json={'workload': ['MAYBE']}).status_code, 422)


class ChallengePackageTests(EvidenceBase):
    def test_counterexample_executes_without_private_evidence_and_packages(self):
        t, f, r, o, a = self.awarded('INFEASIBLE'); self.run_worker(); self.verify_ms(a['id']); self.decide(a['id'])
        recs = self.c.get('/api/v1/work/awards/%s/receipts' % a['id'], headers=self.H).json()['items']; vrec = next(x for x in recs if x['kind'] == 'verification'); srec = [x for x in recs if x['kind'] == 'settlement']
        # the provider (a recipient of the acceptance receipt) challenges the verification with a bounded counterexample of its own inputs
        ch = self.c.post('/api/v1/work/receipts/%s/challenge' % vrec['id'], headers=self.pv['beta']['h'], json={'claim': 'the exact model also declares this related scenario infeasible', 'counterexample_inputs': dict(energy_inputs('INFEASIBLE'), private_label='CHALLENGER_OWN', reserve=120000), 'asserted_outcome': 'INFEASIBLE'})
        self.assertEqual(ch.status_code, 201, ch.text); ch = ch.json(); self.assertEqual((ch['state'], ch['conclusion'], ch['private_evidence_accessed']), ('executing', 'pending', False))
        self.assertNotEqual(ch['counterexample']['input_root'], f['terms']['operation']['input_root'])
        self.run_worker()
        v = self.c.get('/api/v1/work/challenges/' + ch['id'], headers=self.pv['beta']['h']).json(); self.assertEqual((v['state'], v['conclusion'], v['counterexample']['outcome']), ('concluded', 'counterexample_reproduced', 'INFEASIBLE'))
        self.assertIn('not a rewrite of the receipt', v['meaning'])
        z = self.c.get('/api/v1/work/challenges/%s/package' % ch['id'], headers=self.pv['beta']['h']); self.assertEqual(z.status_code, 200)
        names = sorted(zipfile.ZipFile(io.BytesIO(z.content)).namelist()); self.assertEqual(names, ['challenge.json', 'counterexample-inputs.json', 'manifest.json', 'receipt.json', 'result.json', 'statement.json'])
        self.assertNotIn(b'TERMS_TEST_', z.content)                                                                                     # the challenged contract's private inputs are not in the package
        # a non-reproduced counterexample is recorded honestly; the viewer role cannot open one; settlement receipts are not challengeable
        ch2 = self.c.post('/api/v1/work/receipts/%s/challenge' % vrec['id'], headers=self.H, json={'claim': 'this feasible scenario is infeasible', 'counterexample_inputs': dict(energy_inputs('FEASIBLE'), private_label='CH2'), 'asserted_outcome': 'INFEASIBLE'}).json(); self.run_worker()
        self.assertEqual(self.c.get('/api/v1/work/challenges/' + ch2['id'], headers=self.H).json()['conclusion'], 'counterexample_not_reproduced')
        self.assertEqual(self.c.post('/api/v1/work/receipts/%s/challenge' % vrec['id'], headers=self.inst.h('viewer'), json={'claim': 'x', 'counterexample_inputs': {}, 'asserted_outcome': 'INFEASIBLE'}).status_code, 403)
        # attached as dispute evidence by id
        dp = self.c.post('/api/v1/work/awards/%s/milestones/m1/dispute' % a['id'], headers=self.pv['alpha']['h'], json={'claim': 'method behaviour questioned'}).json()
        ev = self.c.post('/api/v1/work/disputes/' + dp['id'] + '/evidence', headers=self.pv['alpha']['h'], json={'challenge_id': ch['id']}); self.assertEqual(ev.status_code, 200, ev.text)
        self.assertTrue(any(e.get('body', {}).get('challenge_id') == ch['id'] for e in ev.json().get('entries', ev.json().get('timeline', []))))


if __name__ == '__main__':
    unittest.main()
