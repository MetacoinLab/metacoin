"""Exact kernels of the decision-evidence order: observability, witnesses, independent checker, acquisition planning against a
brute-force oracle, hypergeometric sampling plans against exhaustive enumeration (fixtures: N=1000, alpha=1/100 -> k=368 for
m=10, k=990 for m=1), reliability statistics. Standard library only."""
import itertools
import json
import math
import random
import unittest
from fractions import Fraction as F

from metacoin_service.decision import checker, kernels as K


def model(hyps, views, observations=None, **kw):
    d = {'schema': K.ANALYSIS_SCHEMA, 'model': {'id': K.MODEL_ID, 'version': 1}, 'candidate_set_version': 'cs-1', 'measurement_menu_version': 'mm-1',
         'coordinates': [{'name': 'cell_1', 'unit': 'mJ'}, {'name': 'cell_2', 'unit': 'mJ'}], 'hypotheses': hyps, 'views': views}
    if observations is not None:
        d['observations'] = observations
    d.update(kw)
    return d


HOT = [{'id': 'hot_cell_1', 'label': 'DO_NOT_START', 'state': [100, 20]}, {'id': 'hot_cell_2', 'label': 'START_UNDER_DECLARED_MODEL', 'state': [20, 100]}]
TOTAL = {'id': 'total', 'rows': [[1, 1]], 'noise': [2], 'output_units': ['mJ'], 'cost': 0}
MENU = [{'id': 'total_again', 'rows': [[1, 1]], 'noise': [2], 'output_units': ['mJ'], 'cost': 1},
        {'id': 'near_parallel', 'rows': [[1, '1001/1000']], 'noise': [1], 'output_units': ['mJ'], 'cost': 1},
        {'id': 'cell_1_sensor', 'rows': [[1, 0]], 'noise': [2], 'output_units': ['mJ'], 'cost': 3}]


class ObservabilityTests(unittest.TestCase):
    def test_repeated_identical_totals_stay_ambiguous_and_witness_is_shared(self):
        views = [dict(TOTAL, id='total_%d' % i) for i in range(64)]
        out = K.analyze(model(HOT, views))
        self.assertEqual(out['conclusion'], 'AMBIGUOUS'); self.assertEqual(out['decision_relevant_pairs'], 1)
        w = out['ambiguity_witnesses'][0]
        self.assertTrue(all(v == ['120'] for v in w['shared_observation'].values()))
        self.assertTrue(checker.check_witness(HOT, views, model(HOT, views)['coordinates'], w)['ok'])

    def test_complementary_measurement_resolves(self):
        out = K.analyze(model(HOT, [TOTAL, dict(MENU[2])]))
        self.assertEqual(out['conclusion'], 'SEPARATED_ON_DECLARED_SET'); self.assertEqual(out['separated_pairs'][0]['separating_views'], ['cell_1_sensor'])
        self.assertTrue(checker.check_separated(HOT, [TOTAL, MENU[2]], model(HOT, [])['coordinates'], out['separated_pairs'][0])['ok'])

    def test_near_parallel_fails_under_noise(self):
        # projections 100 + 20.02 = 120.02 and 20 + 100.1 = 120.1: gap 0.08 <= 2*1
        out = K.analyze(model(HOT, [TOTAL, MENU[1]]))
        self.assertEqual(out['conclusion'], 'AMBIGUOUS')
        v = dict(MENU[1], noise=['1/100'])            # tighter noise: gap 2/25 > 2/100 -> separated
        self.assertEqual(K.analyze(model(HOT, [TOTAL, v]))['conclusion'], 'SEPARATED_ON_DECLARED_SET')

    def test_touching_closed_intervals_are_not_separated(self):
        hs = [{'id': 'a', 'label': 'A', 'state': [0, 0]}, {'id': 'b', 'label': 'B', 'state': [0, 10]}]
        v = {'id': 'y', 'rows': [[0, 1]], 'noise': [5], 'output_units': ['mJ'], 'cost': 1}
        out = K.analyze(model(hs, [v]))
        self.assertEqual(out['conclusion'], 'AMBIGUOUS'); self.assertEqual(out['ambiguity_witnesses'][0]['shared_observation']['y'], ['5'])
        self.assertEqual(K.analyze(model(hs, [dict(v, noise=['499/100'])]))['conclusion'], 'SEPARATED_ON_DECLARED_SET')

    def test_witness_passes_checker_and_fabricated_witness_fails(self):
        views = [{'id': 'x', 'rows': [[1, 0]], 'noise': [1], 'output_units': ['mJ'], 'cost': 0}, {'id': 'y', 'rows': [[0, 1], [1, 1]], 'noise': [6, 7], 'output_units': ['mJ', 'mJ'], 'cost': 1}]
        hs = [{'id': 'a', 'label': 'A', 'state': [0, 0]}, {'id': 'b', 'label': 'B', 'state': [0, 10]}]
        coords = model(hs, views)['coordinates']
        out = K.analyze(model(hs, views))
        w = out['ambiguity_witnesses'][0]
        self.assertTrue(checker.check_witness(hs, views, coords, w)['ok'])
        fake = json.loads(json.dumps(w)); fake['shared_observation']['y'] = ['11', '5']        # 11 is outside a's box [-6, 6]
        r = checker.check_witness(hs, views, coords, fake); self.assertFalse(r['ok'])
        fake2 = json.loads(json.dumps(w)); fake2['pair'] = ['a', 'zzz']
        self.assertFalse(checker.check_witness(hs, views, coords, fake2)['ok'])
        same = [dict(hs[0]), dict(hs[1], label='A')]
        self.assertFalse(checker.check_witness(same, views, coords, w)['ok'])
        self.assertTrue(checker.check_analysis(model(hs, views), out)['ok'])
        tampered = json.loads(json.dumps(out)); tampered['conclusion'] = 'SEPARATED_ON_DECLARED_SET'
        self.assertFalse(checker.check_analysis(model(hs, views), tampered)['ok'])

    def test_scaling_view_and_noise_consistently_preserves_decisions(self):
        rng = random.Random(7)
        for _ in range(40):
            hs = [{'id': 'h%d' % i, 'label': rng.choice('AB'), 'state': [rng.randint(-20, 20), rng.randint(-20, 20)]} for i in range(4)]
            views = [{'id': 'v%d' % j, 'rows': [[rng.randint(-3, 3), rng.randint(-3, 3)]], 'noise': [rng.randint(0, 6)], 'output_units': ['mJ'], 'cost': 1} for j in range(3)]
            base = K.analyze(model(hs, views))
            for f in ('3', '1/7', '1000'):
                scaled = K.analyze(K.check_scaled_consistency(model(hs, views), 'v1', f))
                self.assertEqual((base['conclusion'], [s['pair'] for s in base['separated_pairs']]), (scaled['conclusion'], [s['pair'] for s in scaled['separated_pairs']]))

    def test_observations_outside_all_hypotheses_is_model_inconsistency(self):
        out = K.analyze(model(HOT, [TOTAL], observations={'total': ['500']}))
        self.assertEqual(out['conclusion'], 'MODEL_INCONSISTENT_WITH_OBSERVATIONS'); self.assertEqual(out['conditioning']['surviving'], []); self.assertIsNone(out['conditioning']['determined_label'])
        res = K.analyze(model(HOT, [TOTAL, MENU[2]], observations={'cell_1_sensor': ['99']}))
        self.assertEqual((res['conclusion'], res['conditioning']['determined_label']), ('DETERMINED_WITHIN_DECLARED_MODEL', 'DO_NOT_START'))
        amb = K.analyze(model(HOT, [TOTAL], observations={'total': ['121']}))
        self.assertEqual((amb['conclusion'], len(amb['conditioning']['remaining_witnesses'])), ('AMBIGUOUS_AFTER_OBSERVATION', 1))
        self.assertTrue(checker.check_analysis(model(HOT, [TOTAL], observations={'total': ['121']}), amb)['ok'])

    def test_single_label_candidate_set_is_vacuous(self):
        hs = [dict(HOT[0]), dict(HOT[1], label='DO_NOT_START')]
        out = K.analyze(model(hs, [TOTAL]))
        self.assertEqual((out['conclusion'], out['vacuous'], out['decision_relevant_pairs']), ('NO_DECISION_RELEVANT_ALTERNATIVES', True, 0))

    def test_units_conversion_must_be_validated(self):
        v = {'id': 'joule', 'rows': [[1, 1]], 'noise': ['1/500'], 'output_units': ['J'], 'cost': 0}
        with self.assertRaises(K.KernelInvalid) as cm:
            K.analyze(model(HOT, [v]))
        self.assertEqual(cm.exception.code, 'view_unit_mismatch')
        ok = dict(v, conversion={'from': 'mJ', 'to': 'J', 'factor': '1/1000'})
        out = K.analyze(model(HOT, [ok]))
        self.assertEqual(out['ambiguity_witnesses'][0]['shared_observation']['joule'], ['3/25'])      # 0.12 J
        with self.assertRaises(K.KernelInvalid) as cm:
            K.analyze(model(HOT, [dict(v, conversion={'from': 'mJ', 'to': 'J', 'factor': '1/10'})]))
        self.assertEqual(cm.exception.code, 'view_conversion_not_validated')
        mixed = {'id': 'm', 'rows': [[1, 1]], 'noise': [1], 'output_units': ['mJ'], 'cost': 0}
        d = model(HOT, [mixed]); d['coordinates'][1]['unit'] = 'K'
        with self.assertRaises(K.KernelInvalid) as cm:
            K.analyze(d)
        self.assertEqual(cm.exception.code, 'view_mixed_units_in_row')

    def test_rejects_floats_bools_nan_duplicates_and_limits(self):
        for bad in ([{'id': 'a', 'label': 'A', 'state': [0.5, 0]}, HOT[1]], [{'id': 'a', 'label': 'A', 'state': [True, 0]}, HOT[1]], [{'id': 'a', 'label': 'A', 'state': ['nan', 0]}, HOT[1]],
                    [{'id': 'a', 'label': 'A', 'state': ['inf', 0]}, HOT[1]], [HOT[0], dict(HOT[1], id='hot_cell_1')], [{'id': 'a', 'label': 'A', 'state': [0]}, HOT[1]], [{'id': 'a', 'label': 'A', 'state': ['1' * 60, 0]}, HOT[1]]):
            with self.assertRaises(K.KernelInvalid):
                K.analyze(model(bad, [TOTAL]))
        with self.assertRaises(K.KernelInvalid):
            K.analyze(model(HOT, [TOTAL, dict(TOTAL)]))
        with self.assertRaises(K.KernelInvalid):
            K.analyze(model(HOT, [dict(TOTAL, noise=[-1])]))
        with self.assertRaises(K.KernelInvalid):
            K.analyze(model([{'id': 'h%d' % i, 'label': 'A', 'state': [i, 0]} for i in range(33)], [TOTAL]))
        with self.assertRaises(K.KernelInvalid):
            K.analyze(model(HOT, [dict(TOTAL, id='v%d' % i) for i in range(65)]))
        with self.assertRaises(K.KernelInvalid):
            K.analyze(dict(model(HOT, [TOTAL]), model={'id': K.MODEL_ID, 'version': 2}))
        with self.assertRaises(K.KernelInvalid):
            K.analyze(dict(model(HOT, [TOTAL]), extra=1))


class AcquisitionTests(unittest.TestCase):
    def test_demonstration_budget_three_buys_sensor_budget_two_buys_nothing(self):
        p3 = K.plan_acquisition(model(HOT, [TOTAL]), MENU, 3)
        self.assertEqual((p3['selected_views'], p3['cost'], p3['resolved_before'], p3['resolved_after'], p3['optimization_status']), (['cell_1_sensor'], 3, 0, 1, 'exact'))
        p2 = K.plan_acquisition(model(HOT, [TOTAL]), MENU, 2)
        self.assertEqual((p2['selected_views'], p2['cost'], p2['resolved_after'], p2['nothing_to_acquire']), ([], 0, 0, True))
        self.assertEqual(p2['remaining_ambiguities'][0]['pair'], ['hot_cell_1', 'hot_cell_2'])

    def test_exact_search_agrees_with_brute_force_oracle_on_random_small_menus(self):
        rng = random.Random(11)
        for trial in range(60):
            n_h = rng.randint(2, 5); dim = 2
            hs = [{'id': 'h%d' % i, 'label': rng.choice('ABC'), 'state': [rng.randint(-10, 10) for _ in range(dim)]} for i in range(n_h)]
            existing = [{'id': 'e0', 'rows': [[rng.randint(-2, 2), rng.randint(-2, 2)]], 'noise': [rng.randint(0, 5)], 'output_units': ['mJ'], 'cost': 0}] if rng.random() < 0.5 else []
            menu = [{'id': 'c%d' % j, 'rows': [[rng.randint(-3, 3), rng.randint(-3, 3)]], 'noise': [rng.randint(0, 4)], 'output_units': ['mJ'], 'cost': rng.randint(0, 5)} for j in range(rng.randint(1, 6))]
            budget = rng.randint(0, 9)
            got = K.plan_acquisition(model(hs, existing), menu, budget); want = K.plan_oracle(model(hs, existing), menu, budget)
            self.assertEqual((got['resolved_after'], got['cost'], got['selected_views']), (want['resolved_after'], want['cost'], want['selected_views']), trial)

    def test_weighted_objective_needs_frozen_weights_and_changes_choice(self):
        hs = [{'id': 'a', 'label': 'A', 'state': [0, 0]}, {'id': 'b', 'label': 'B', 'state': [10, 0]}, {'id': 'c', 'label': 'C', 'state': [0, 10]}]
        menu = [{'id': 'vx', 'rows': [[1, 0]], 'noise': [1], 'output_units': ['mJ'], 'cost': 1}, {'id': 'vy', 'rows': [[0, 1]], 'noise': [1], 'output_units': ['mJ'], 'cost': 1}]
        with self.assertRaises(K.KernelInvalid):
            K.plan_acquisition(model(hs, []), menu, 1, objective='weighted_pairs/v1')
        w = {'A|B': 1, 'A|C': 5, 'B|C': 1}
        p = K.plan_acquisition(model(hs, []), menu, 1, objective='weighted_pairs/v1', weights=w)
        self.assertEqual(p['selected_views'], ['vy'])        # vy separates A|C (5) and B|C (1); vx separates A|B and B|C only
        self.assertEqual(K.plan_acquisition(model(hs, []), menu, 1)['selected_views'], ['vx'])   # unweighted: 2 pairs each -> lexicographic tie break

    def test_menu_too_large_is_refused_explicitly(self):
        menu = [{'id': 'c%d' % j, 'rows': [[1, 0]], 'noise': [1], 'output_units': ['mJ'], 'cost': 1} for j in range(13)]
        with self.assertRaises(K.KernelInvalid) as cm:
            K.plan_acquisition(model(HOT, [TOTAL]), menu, 5)
        self.assertEqual(cm.exception.code, 'menu_size_unsupported')


class SamplingTests(unittest.TestCase):
    def test_exact_probability_against_all_subsets(self):
        for n in range(1, 9):
            for m in range(n + 1):
                for k in range(n + 1):
                    self.assertEqual(K.miss_probability(n, m, k), K.exhaustive_miss(n, m, k))
                    a = F(1, 3)
                    self.assertEqual(K.miss_at_most(n, m, k, a), K.exhaustive_miss(n, m, k) <= a)

    def test_fixtures_368_and_990(self):
        p10 = K.sampling_plan(1000, 10, '1/100', 500)
        self.assertEqual((p10['required_samples'], p10['feasible'], p10['evaluated_samples']), (368, True, 368))
        self.assertEqual(p10['achieved_bound']['exact'], '352449623749338115/35595886548914893626')
        self.assertLess(K.miss_probability(1000, 10, 368), F(1, 100)); self.assertGreater(K.miss_probability(1000, 10, 367), F(1, 100))
        p1 = K.sampling_plan(1000, 1, '1/100', 500)
        self.assertEqual((p1['required_samples'], p1['feasible'], p1['refusal']['code'], p1['achieved_bound']['exact']), (990, False, 'sample_budget_insufficient', '1/2'))
        for m in (1, 10, 100):
            k = K.sampling_plan(1000, m, '1/100', 1000)['required_samples']
            self.assertTrue(K.miss_probability(1000, m, k) <= F(1, 100) < K.miss_probability(1000, m, k - 1))

    def test_fraction_ceiling_rule_and_boundary_alpha(self):
        p = K.sampling_plan(1000, None, '1/100', 1000, defect_fraction='1/300')       # 1000/300 = 3.33 -> m = 4
        self.assertEqual(p['minimum_defects'], 4); self.assertIn('ceil', p['minimum_defects_rule'])
        # alpha exactly equal to the miss probability counts as satisfied (<=), decided by integer comparison
        exact = K.miss_probability(20, 3, 5)
        self.assertEqual(K.sampling_plan(20, 3, str(exact), 20)['required_samples'], 5)
        below = exact - F(1, exact.denominator * 1000)
        self.assertEqual(K.sampling_plan(20, 3, str(below), 20)['required_samples'], 6)
        for bad in (('0',), ('1',), ('-1/2',)):
            with self.assertRaises(K.KernelInvalid):
                K.sampling_plan(20, 3, bad[0], 20)
        with self.assertRaises(K.KernelInvalid):
            K.sampling_plan(20, 3, 0.01, 20)

    def test_log_bound_is_conservative_against_exact(self):
        for n, m, k in ((1000, 10, 368), (1000, 1, 500), (50, 5, 20), (8, 2, 3), (200000, 1, 100000), (5000, 7, 4999)):
            ex = K.miss_probability(n, m, k); lb = K.miss_log_bound(n, m, k)
            if lb['upper'] is not None:
                self.assertGreaterEqual(lb['upper'], float(ex) * (1 - 1e-12))
        deep = K.miss_log_bound(1000, 500, 999)        # exact 0 since k > n - m
        self.assertEqual(deep['upper'], 0.0)
        tiny = K.miss_log_bound(200000, 100000, 100000)     # astronomically small but k <= n - m: not zero
        self.assertTrue(tiny['upper'] is None or tiny['upper'] > 0.0)

    def test_large_population_decides_exactly(self):
        p = K.sampling_plan(200000, 1, '1/100', 200000)
        self.assertEqual(p['required_samples'], 198000)
        self.assertTrue(K.miss_at_most(200000, 1, 198000, F(1, 100))); self.assertFalse(K.miss_at_most(200000, 1, 197999, F(1, 100)))
        with self.assertRaises(K.KernelInvalid):
            K.sampling_plan(2_000_000, 1, '1/100', 10)

    def test_miss_decreases_with_more_defects_and_invalid_inputs(self):
        values = [K.miss_probability(20, m, 5) for m in range(21)]
        self.assertEqual(values, sorted(values, reverse=True))
        for args in ((0, 0, 0), (10, 11, 1), (10, 2, 11), (True, 0, 0)):
            with self.assertRaises(K.KernelInvalid):
                K.miss_probability(*args)


class ReliabilityTests(unittest.TestCase):
    def test_wilson_and_zero_failure_bounds(self):
        lo, hi = K.wilson(50, 100)
        self.assertAlmostEqual(lo, 0.4038315303659956); self.assertAlmostEqual(hi, 0.5961684696340044)
        self.assertGreater(K.wilson(0, 250)[1], 0)
        up = K.zero_failure_upper(250)
        self.assertAlmostEqual((1 - up) ** 250, 0.05); self.assertTrue(0.0118 < up < 0.0120)
        self.assertAlmostEqual(K.binomial_upper(0, 250), up)
        self.assertGreater(K.binomial_upper(3, 250), up)
        self.assertAlmostEqual(math.exp(K._log_binom_cdf(3, 250, K.binomial_upper(3, 250))), 0.05, places=6)

    def test_paired_comparison_and_zero_denominator_ratio(self):
        a = [True, False, False, True, False]; b = [True, True, True, True, False]
        pc = K.paired_comparison(a, b)
        self.assertEqual((pc['only_b_correct'], pc['only_a_correct'], pc['both_wrong']), (2, 0, 1)); self.assertEqual(pc['exact_sign_test_p_two_sided'], 0.5)
        r = K.residual_ratio(3, 0, 40)
        self.assertIsNone(r['estimate']); self.assertIn('undefined', r['estimate_status']); self.assertGreater(r['lower_bound'], 0)
        r2 = K.residual_ratio(6, 2, 40); self.assertEqual(r2['estimate'], 3.0)
        with self.assertRaises(K.KernelInvalid):
            K.paired_comparison([True], [])


if __name__ == '__main__':
    unittest.main()
