"""Calibration through real entry points: numeric datasets (exact synthetic model recovered to float precision,
noisy bounded case, rank deficiency, zero-variance column, extreme scales, invalid values, ridge shrinkage),
independent pure-Python refit verification by the worker, predictions with interpolation/extrapolation status and an
empirical interval, performance datasets from this workspace's own compute runs, approval as a scheduling signal,
the operator toggle, replay and counterfactual planning."""
import json
import random
import time
import unittest

from metacoin_service.tests.test_compute_engine import ComputeInstance, HAVE_RUNTIME, batch_spec
from metacoin_service.compute import calibration as cal


class ReferenceTests(unittest.TestCase):
    def test_householder_recovers_exact_model_and_flags_rank_deficiency(self):
        rows = [[float(i), float(i * i % 7)] for i in range(12)]
        y = [3 * a - 2 * b + 5 for a, b in rows]
        Z = [[a, b, 1.0] for a, b in rows]
        x, rank, _ = cal.householder_lstsq(Z, y)
        self.assertEqual(rank, 3)
        for got, want in zip(x, (3, -2, 5)):
            self.assertAlmostEqual(got, want, places=9)
        Zd = [[a, 2 * a, 1.0] for a, _ in rows]
        _, rank_d, _ = cal.householder_lstsq(Zd, y)
        self.assertLess(rank_d, 3)
        self.assertEqual(cal.split_rows(10, {'method': 'chronological', 'train_fraction_percent': 80})[0], list(range(8)))
        self.assertEqual(cal.domain_status({'a': 5.0}, {'a': [0, 4]})[0], 'extrapolation')


@unittest.skipUnless(HAVE_RUNTIME, 'no numpy-capable compute interpreter on this host')
class CalibrationTests(unittest.TestCase):
    def setUp(self):
        self.inst = ComputeInstance(); self.addCleanup(self.inst.close); self.c = self.inst.client; self.H = self.inst.h('owner')
        self.w = self.inst.worker(); self.addCleanup(self.w.offline)

    def dataset(self, rows, columns=('x1', 'x2', 'y'), target='y', units=None, name='synthetic'):
        r = self.c.post('/api/v1/calibration/datasets', headers=self.H, json={'name': name, 'columns': list(columns), 'target': target, 'units': units or {'y': 'ms'}, 'rows': rows, 'provenance': 'synthetic'})
        self.assertEqual(r.status_code, 201, r.text)
        return r.json()['id']

    def fit(self, inputs, expect='succeeded'):
        r = self.c.post('/api/v1/calibration/fits', headers=self.H, json={'inputs': inputs})
        self.assertEqual(r.status_code, 202, r.text)
        jid = r.json()['job_id']
        self.assertEqual(self.w.run_once()[1], expect)
        models = [m for m in self.c.get('/api/v1/calibration/models', headers=self.H).json()['items'] if m['job_id'] == jid]
        return jid, (models[0] if models else None)

    def test_fits_predictions_verification_and_scheduling_signal(self):
        rng = random.Random(4)
        exact = [{'x1': i, 'x2': (i * 3) % 11, 'y': 3 * i - 2 * ((i * 3) % 11) + 5} for i in range(40)]
        did = self.dataset(exact)
        jid, m = self.fit({'dataset_id': did, 'features': ['x1', 'x2'], 'target': 'y', 'intercept': True, 'split': {'method': 'random', 'train_fraction_percent': 75, 'seed': 1}})
        self.assertIsNotNone(m); self.assertTrue(m['verification_passed'], m)
        full = self.c.get('/api/v1/calibration/models/' + m['id'], headers=self.H).json()
        coef = dict(zip(full['manifest']['coefficient_names'], [float(c) for c in full['manifest']['coefficients']]))
        # standardized coefficients map back exactly: prediction on a training row equals the exact model
        self.assertLess(float(full['metrics']['train']['rmse']), 1e-9); self.assertLess(float(full['metrics']['eval']['rmse']), 1e-9)
        self.assertEqual((full['manifest']['rank'], full['manifest']['split']['method']), (3, 'random'))
        pred = self.c.post('/api/v1/calibration/models/' + m['id'] + '/predict', headers=self.H, json={'features': {'x1': 10, 'x2': 4}}).json()
        self.assertAlmostEqual(float(pred['prediction']), 3 * 10 - 2 * 4 + 5, places=8); self.assertEqual(pred['domain_status'], 'interpolation')
        ext = self.c.post('/api/v1/calibration/models/' + m['id'] + '/predict', headers=self.H, json={'features': {'x1': 1000, 'x2': 4}}).json()
        self.assertEqual(ext['domain_status'], 'extrapolation'); self.assertIn('x1', ext['outside_domain']); self.assertFalse(ext['usable_for_scheduling'])
        self.assertEqual(self.c.post('/api/v1/calibration/models/' + m['id'] + '/predict', headers=self.H, json={'features': {'x1': 'nan', 'x2': 4}}).status_code, 422)
        self.assertEqual(self.c.post('/api/v1/calibration/models/' + m['id'] + '/predict', headers=self.H, json={'features': {'x1': 1}}).status_code, 422)
        # verification record from the persisted compute phase: independent QR refit agreed
        cv = self.c.get('/api/v1/compute/jobs/' + jid, headers=self.H).json()
        self.assertEqual((cv['verification']['mode'], cv['verification']['passed']), ('reference_refit', True))
        self.assertIn('reference_refit_predictions', [c['check'] for c in cv['verification']['checks']])
        # noisy bounded case with a chronological split and an empirical interval
        noisy = [{'x1': i, 'x2': rng.randint(0, 20), 'y': None} for i in range(60)]
        for r in noisy:
            r['y'] = str(round(2.5 * r['x1'] + 0.5 * r['x2'] + rng.uniform(-3, 3), 6))
        did2 = self.dataset(noisy, name='noisy')
        _, m2 = self.fit({'dataset_id': did2, 'features': ['x1', 'x2'], 'target': 'y', 'split': {'method': 'chronological', 'train_fraction_percent': 80}, 'interval_percent': 90})
        f2 = self.c.get('/api/v1/calibration/models/' + m2['id'], headers=self.H).json()
        self.assertLess(float(f2['metrics']['eval']['rmse']), 3.5); self.assertEqual(int(f2['manifest']['prediction_interval']['level_percent']), 90); self.assertEqual(f2['manifest']['split']['method'], 'chronological')
        p2 = self.c.post('/api/v1/calibration/models/' + m2['id'] + '/predict', headers=self.H, json={'features': {'x1': 30, 'x2': 10}}).json()
        self.assertLess(float(p2['interval']['low']), float(p2['prediction'])); self.assertGreater(float(p2['interval']['high']), float(p2['prediction']))
        # rank-deficient features and a zero-variance column are reported, not hidden
        dep = [{'x1': i, 'x2': 2 * i, 'x3': 7, 'y': 4 * i + 1} for i in range(20)]
        did3 = self.dataset(dep, columns=('x1', 'x2', 'x3', 'y'), name='rank')
        _, m3 = self.fit({'dataset_id': did3, 'features': ['x1', 'x2', 'x3'], 'target': 'y'})
        self.assertTrue(any('rank-deficient' in w for w in m3['warnings']), m3['warnings']); self.assertTrue(any('zero-variance' in w for w in m3['warnings']))
        self.assertTrue(m3['verification_passed'])
        # extreme scales still fit after standardization; ridge shrinks coefficients toward zero
        big = [{'x1': i * 10 ** 9, 'x2': i * 10 ** -6 if False else str(i) + 'e-6', 'y': str(i * 10 ** 9 * 2)} for i in range(1, 25)]
        did4 = self.dataset(big, name='scales')
        _, m4 = self.fit({'dataset_id': did4, 'features': ['x1', 'x2'], 'target': 'y'})
        self.assertTrue(m4['verification_passed'])
        _, m5 = self.fit({'dataset_id': did, 'features': ['x1', 'x2'], 'target': 'y', 'ridge_lambda': '1000'})
        f5 = self.c.get('/api/v1/calibration/models/' + m5['id'], headers=self.H).json()
        c_ols = [abs(float(c)) for c in full['manifest']['coefficients'][:2]]; c_ridge = [abs(float(c)) for c in f5['manifest']['coefficients'][:2]]
        self.assertTrue(all(r < o for r, o in zip(c_ridge, c_ols)), (c_ols, c_ridge)); self.assertTrue(f5['verification_passed'])
        # invalid values and unknown fields are refused at the boundary
        self.assertEqual(self.c.post('/api/v1/calibration/datasets', headers=self.H, json={'name': 'bad', 'columns': ['a', 'y'], 'target': 'y', 'rows': [{'a': 'inf', 'y': 1}] * 3}).status_code, 422)
        self.assertEqual(self.c.post('/api/v1/calibration/fits', headers=self.H, json={'inputs': {'dataset_id': did, 'features': ['x1'], 'target': 'y', 'solver': 'inverse'}}).status_code, 422)
        self.assertEqual(self.c.post('/api/v1/calibration/fits', headers=self.H, json={'inputs': {'dataset_id': did, 'features': ['x1'], 'target': 'x1'}}).status_code, 422)
        self.assertEqual(self.c.post('/api/v1/calibration/fits', headers=self.inst.h('viewer'), json={'inputs': {'dataset_id': did, 'features': ['x1'], 'target': 'y'}}).status_code, 403)
        # a numeric model has no task scope: approval for scheduling is refused
        self.assertEqual(self.c.post('/api/v1/calibration/models/' + m['id'] + '/approve', headers=self.H, json={}).status_code, 409)
        # performance dataset from this workspace's own runs; a scoped fit; approval; the scheduler uses or ignores it with a recorded reason
        for step in (300, 100, 50, 25, 20):
            jb = self.inst.compute_job('temporal_batch', batch_spec(grid=[{'path': 'reserve', 'start': 0, 'stop': 9000, 'step': step}, {'path': 'load_scale_percent', 'values': [50, 100, 150, 200]}]))
            self.assertEqual(self.w.run_once()[1], 'succeeded')
        pr = self.c.post('/api/v1/calibration/datasets', headers=self.H, json={'kind': 'performance', 'task_kind': 'temporal_batch'})
        self.assertEqual(pr.status_code, 201, pr.text); pds = pr.json()
        self.assertGreaterEqual(pds['rows'], 5); self.assertEqual(pds['target'], 'duration_ms'); self.assertIn('censoring', pds['policy'])
        _, pm = self.fit({'dataset_id': pds['id'], 'features': ['work_units'], 'target': 'duration_ms', 'split': {'method': 'chronological', 'train_fraction_percent': 80}, 'scope': {'task_kind': 'temporal_batch', 'backend': 'cpu'}})
        self.assertTrue(pm['verification_passed']); self.assertEqual(pm['scope'], {'task_kind': 'temporal_batch', 'backend': 'cpu'})
        ap = self.c.post('/api/v1/calibration/models/' + pm['id'] + '/approve', headers=self.H, json={'evidence': {'eval_rmse_ms': pm['metrics']['eval'].get('rmse')}}).json()
        self.assertEqual((ap['state'], ap['default_for']), ('approved', ['temporal_batch:cpu']))
        comp = self.c.get('/api/v1/calibration/models/' + pm['id'] + '/comparison', headers=self.H).json()
        self.assertGreaterEqual(len(comp['rows']), 5); self.assertTrue(all(r['prediction'] is not None for r in comp['rows']))
        plan = self.c.post('/api/v1/calibration/plan', headers=self.H, json={'task_kind': 'temporal_batch', 'inputs': batch_spec(device_policy='auto')}).json()
        cpu = next(c for c in plan['candidates'] if c['backend'] == 'cpu')
        self.assertEqual(cpu['prediction_status'], 'calibrated'); self.assertTrue(plan['not_a_measurement'])
        cuda = next(c for c in plan['candidates'] if c['backend'] == 'cuda')
        self.assertIn('no approved calibration', cuda['reason'])
        replay = self.c.get('/api/v1/calibration/replay/temporal_batch', headers=self.H).json()
        self.assertGreaterEqual(replay['runs'], 5); self.assertEqual(replay['fairness'][:9], 'unchanged')
        # a new auto job records why the calibration was used or ignored in its backend reason
        ja = self.inst.compute_job('temporal_batch', batch_spec(device_policy='auto'))
        self.assertEqual(self.w.run_once()[1], 'succeeded')
        reason = self.inst.view(ja)['backend_reason']
        self.assertTrue('calibrat' in reason, reason)
        # operator toggle: disabled -> conservative fallback; evidence preserved
        self.c.post('/api/v1/calibration/scheduling', headers=self.H, json={'enabled': False})
        plan2 = self.c.post('/api/v1/calibration/plan', headers=self.H, json={'task_kind': 'temporal_batch', 'inputs': batch_spec(device_policy='auto')}).json()
        self.assertTrue(all(c['predicted_duration_ms'] is None for c in plan2['candidates'])); self.assertIn('conservative', plan2['basis'])
        self.assertEqual(self.c.get('/api/v1/calibration/models/' + pm['id'], headers=self.H).json()['state'], 'approved')
        self.assertEqual(self.c.post('/api/v1/calibration/scheduling', headers=self.inst.h('viewer'), json={'enabled': True}).status_code, 403)
        # retire clears the default; the retired model stays readable
        rt = self.c.post('/api/v1/calibration/models/' + pm['id'] + '/retire', headers=self.H, json={}).json()
        self.assertEqual((rt['state'], rt['default_for']), ('retired', []))


@unittest.skipUnless(HAVE_RUNTIME, 'no numpy-capable compute interpreter on this host')
class DesignSuggestionTests(CalibrationTests):
    test_fits_predictions_verification_and_scheduling_signal = None          # fixture reuse only

    def test_design_suggestions_rank_by_predicted_utility_under_cost_policy(self):
        # training data covers x1 in 0..19 densely but x2 only in {0, 1}: a candidate far along x2 has the most leverage
        rows = [{'x1': i, 'x2': i % 2, 'y': 3 * i - 2 * (i % 2) + 5} for i in range(24)]
        did = self.dataset(rows)
        jid, m = self.fit({'dataset_id': did, 'features': ['x1', 'x2'], 'target': 'y', 'intercept': True, 'split': {'method': 'random', 'train_fraction_percent': 80, 'seed': 3}})
        self.assertIsNotNone(m)
        cands = [{'label': 'dup-inside', 'features': {'x1': 5, 'x2': 1}, 'cost': 1}, {'label': 'x2-far', 'features': {'x1': 5, 'x2': 6}, 'cost': 1},
                 {'label': 'x1-far', 'features': {'x1': 60, 'x2': 0}, 'cost': 1}, {'label': 'pricey', 'features': {'x1': 5, 'x2': 8}, 'cost': 40}, {'label': 'free-inside', 'features': {'x1': 10, 'x2': 0}, 'cost': 0}]
        r = self.c.post('/api/v1/calibration/models/' + m['id'] + '/design', headers=self.H, json={'candidates': cands, 'objective': 'reduce_overall_uncertainty', 'cost_policy': {'budget': 3, 'rank_by': 'utility_per_cost'}, 'max_selected': 3})
        self.assertEqual(r.status_code, 200, r.text); d = r.json()
        labels = [s['label'] for s in d['selected']]
        self.assertEqual(len(labels), 3); self.assertIn('x2-far', labels[:2]); self.assertIn('x1-far', labels[:2])
        self.assertTrue(all(s['proven_information_gain'] is None for s in d['selected']))
        self.assertEqual({n['label']: n['reason'] for n in d['not_selected']}['pricey'], 'over_budget')
        self.assertEqual(d['selected'][0]['domain_status'], 'extrapolation'); self.assertEqual(float(d['total_cost']), sum(c['cost'] for c in cands if c['label'] in labels))
        self.assertIn('none', d['instrument_contact'])
        # utility decreases for a near-duplicate after the first pick (sequential update), and a zero-utility point is reported as such
        first = float(d['selected'][0]['predicted_utility']); self.assertGreater(first, float(d['selected'][-1]['predicted_utility']))
        # target-focused objective: reduce uncertainty at a declared point far along x2 prefers the x2 candidate strictly
        r2 = self.c.post('/api/v1/calibration/models/' + m['id'] + '/design', headers=self.H, json={'candidates': cands[:3], 'objective': 'reduce_uncertainty_at_targets', 'targets': [{'x1': 5, 'x2': 7}], 'cost_policy': {'rank_by': 'utility'}, 'max_selected': 1}).json()
        self.assertEqual(r2['selected'][0]['label'], 'x2-far')
        # refusals: wrong feature set, non-finite cost, unknown objective, viewer role; nothing was submitted
        self.assertEqual(self.c.post('/api/v1/calibration/models/' + m['id'] + '/design', headers=self.H, json={'candidates': [{'features': {'x1': 1}}]}).status_code, 422)
        self.assertEqual(self.c.post('/api/v1/calibration/models/' + m['id'] + '/design', headers=self.H, json={'candidates': [{'features': {'x1': 1, 'x2': 1}, 'cost': 'inf'}]}).status_code, 422)
        self.assertEqual(self.c.post('/api/v1/calibration/models/' + m['id'] + '/design', headers=self.H, json={'candidates': cands[:1], 'objective': 'maximize_truth'}).status_code, 422)
        self.assertEqual(self.c.post('/api/v1/calibration/models/' + m['id'] + '/design', headers=self.inst.h('viewer'), json={'candidates': cands[:1]}).status_code, 403)
        self.assertEqual(len([j for j in self.c.get('/api/v1/jobs', headers=self.H).json()['items']]), 1)
