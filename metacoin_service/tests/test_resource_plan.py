"""Group D: robust resource planning. Model tests need scipy (the compute interpreter); the simulator/oracle tests are pure
Python; the service tests drive the real worker child, the verification service, campaigns and the console."""
import json
import random
import unittest
from unittest import mock
from metacoin_service.compute import resource_plan as rp
from metacoin_service.documents import units

try:
    import scipy.optimize  # noqa
    HAVE_SCIPY = True
except ImportError:
    HAVE_SCIPY = False


def instance(**over):
    d = dict(schema=rp.SCHEMA, slot_seconds=60, slots=6, capacity=100000, initial_low=50000, reserve=20000, supply_low=[500] * 6, supply_high=[600] * 6, base_low=[100] * 6, base_high=[200] * 6,
             uncertainty_interpretation='specification_bound', resources={'cpu': 2}, tasks=[], private_label='RP_TEST')
    d.update(over)
    return d


def task(id, **kw):
    t = dict(id=id, utility=1, duration=1, power_high=100)
    t.update(kw)
    return t


class SimulatorAndOracleTests(unittest.TestCase):
    def test_simulator_rejects_invalid_duplicate_overlapping_and_out_of_window_assignments(self):
        d = instance(tasks=[task('a', duration=2, latest_start=2), task('b', exclusive_with=['a']), task('c', dependencies=['a']), task('m', mandatory=True)])
        self.assertEqual(rp.simulate(d, {'zz': 0})['violations'][0]['code'], 'unknown_task')
        self.assertEqual(rp.simulate(d, {'a': 4, 'm': 0})['violations'][0]['code'], 'start_outside_window')
        self.assertEqual(rp.simulate(d, {'a': 0})['violations'][0]['code'], 'mandatory_task_missing')
        self.assertEqual(rp.simulate(d, {'a': 0, 'b': 1, 'm': 0})['violations'][0]['code'], 'exclusive_overlap')
        self.assertEqual(rp.simulate(d, {'a': 0, 'c': 1, 'm': 0})['violations'][0]['code'], 'dependency_not_finished')
        self.assertEqual(rp.simulate(d, {'c': 3, 'm': 0})['violations'][0]['code'], 'dependency_not_selected')
        ok = rp.simulate(d, {'a': 0, 'c': 2, 'm': 5})
        self.assertTrue(ok['feasible']); self.assertEqual(ok['utility'], 3); self.assertEqual(len(ok['trajectory']), 7)

    def test_reserve_at_every_boundary_capacity_spill_and_resource_limits(self):
        # reserve violated at boundary 2 although the horizon ends above the reserve (later recharge)
        d = instance(initial_low=30000, base_low=[50] * 6, base_high=[50] * 6, supply_low=[0, 0, 900, 900, 900, 900], supply_high=[0, 0, 900, 900, 900, 900], tasks=[task('a', power_high=300, duration=2)])
        sim = rp.simulate(d, {'a': 0})
        self.assertFalse(sim['feasible']); self.assertEqual((sim['violations'][0]['code'], sim['violations'][0]['boundary']), ('reserve_violated', 1))
        self.assertTrue(rp.simulate(d, {'a': 4})['feasible'])                # same task after the recharge
        # spill: the store saturates at capacity and the refused energy is reported
        d2 = instance(initial_low=99000, tasks=[])
        sim2 = rp.simulate(d2, {})
        self.assertEqual(sim2['trajectory'][1]['energy'], 100000); self.assertEqual(sim2['spill'], (99000 + 300 * 60) - 100000 + 300 * 60 * 5)
        # resource capacity per slot
        d3 = instance(tasks=[task('a', resources={'cpu': 2}), task('b', resources={'cpu': 1})])
        self.assertEqual(rp.simulate(d3, {'a': 0, 'b': 0})['violations'][0]['code'], 'resource_capacity_exceeded')
        self.assertTrue(rp.simulate(d3, {'a': 0, 'b': 1})['feasible'])

    def test_oracle_enumerates_bounded_instances_only(self):
        d = instance(tasks=[task('a'), task('b', mandatory=True, earliest_start=5, latest_start=5)])
        o = rp.oracle(d)
        self.assertEqual((o['combinations'], o['best']['utility']), (7 * 1, 2))
        self.assertIsNone(rp.oracle(instance(tasks=[task('t%d' % i) for i in range(7)])))       # more than 6 tasks: no oracle claim
        self.assertIsNone(rp.oracle(instance(slots=96, supply_low=[500] * 96, supply_high=[600] * 96, base_low=[100] * 96, base_high=[200] * 96, tasks=[task('t%d' % i) for i in range(4)])))

    def test_input_boundary_magnitudes_and_directed_unit_conversion(self):
        with self.assertRaises(rp.Invalid):
            rp.validate(instance(capacity=10 ** 13))
        with self.assertRaises(rp.Invalid):
            rp.validate(instance(uncertainty_interpretation='confidence_interval'))
        with self.assertRaises(rp.Invalid):
            rp.validate(instance(tasks=[task('a', dependencies=['a'])]))
        with self.assertRaises(rp.Invalid):
            rp.validate(dict(instance(), extra_field=1))
        # interval bounds convert with directed rounding at the boundary: a low bound never rounds up, a high bound never rounds down
        low = units.to_base_integer('0.4166', 'W', role='low', rounding='outward'); high = units.to_base_integer('0.4166', 'W', role='high', rounding='outward')
        self.assertEqual((low['value'], low['directed'], high['value'], high['directed']), (416, 'floor', 417, 'ceil'))
        exact = units.to_base_integer('1.5', 'J', role='point')
        self.assertEqual((exact['value'], exact['unit'], exact['exact']), (1500, 'mJ', True))
        with self.assertRaises(units.UnitError):
            units.to_base_integer('0.4166', 'W', role='point', rounding='reject')


@unittest.skipUnless(HAVE_SCIPY, 'scipy (HiGHS) is only in the compute interpreter')
class SolverTests(unittest.TestCase):
    def solve(self, d):
        return rp.solve(d)

    def test_analytic_fixtures(self):
        # no tasks
        r = self.solve(instance()); self.assertEqual((r['status'], r['objective'], r['assignments']), ('optimal_within_tolerance', 0, {}))
        # one feasible task
        r = self.solve(instance(tasks=[task('a', utility=3)])); self.assertEqual((r['objective'], r['selected']), (3, ['a'])); self.assertEqual(r['optimality'], 'established by enumeration and solver')
        # impossible mandatory task: too much power for the reserve at any start
        r = self.solve(instance(tasks=[task('m', mandatory=True, power_high=100000)]))
        self.assertEqual(r['status'], 'infeasible_established_by_enumeration'); self.assertIsNone(r.get('assignments'))
        # mutually exclusive alternatives with resource conflict: the better one is chosen and the other is explained
        r = self.solve(instance(tasks=[task('x', utility=5, duration=6, resources={'cpu': 2}), task('y', utility=4, duration=6, resources={'cpu': 2}, exclusive_with=['x'])]))
        self.assertEqual(r['selected'], ['x']); self.assertIn('violates', r['excluded'][0]['reason'])
        # precedence chain: b after a, c after b
        r = self.solve(instance(tasks=[task('a', utility=1, duration=2), task('b', utility=1, duration=2, dependencies=['a']), task('c', utility=1, dependencies=['b'])]))
        a, b, c = r['assignments']['a'], r['assignments']['b'], r['assignments']['c']
        self.assertTrue(a + 2 <= b and b + 2 <= c); self.assertEqual(r['objective'], 3)
        # reserve violation before a later recharge: the task must wait for the recharge
        r = self.solve(instance(initial_low=30000, base_low=[50] * 6, base_high=[50] * 6, supply_low=[0, 0, 900, 900, 900, 900], supply_high=[0, 0, 900, 900, 900, 900], tasks=[task('a', utility=1, power_high=300, duration=2)]))
        self.assertEqual((r['status'], r['assignments']['a']), ('optimal_within_tolerance', 2)); self.assertFalse(rp.simulate(dict(r_in := instance(initial_low=30000, base_low=[50] * 6, base_high=[50] * 6, supply_low=[0, 0, 900, 900, 900, 900], supply_high=[0, 0, 900, 900, 900, 900], tasks=[task('a', utility=1, power_high=300, duration=2)])), {'a': 1})['feasible'])
        # capacity spill is reported, never counted as usable energy
        r = self.solve(instance(initial_low=99000)); self.assertGreater(r['spill'], 0); self.assertLessEqual(max(t['energy'] for t in r['trajectory']), 100000)
        # greedy utility ranking is suboptimal: the single big task blocks two medium tasks whose sum is larger
        r = self.solve(instance(tasks=[task('big', utility=5, duration=6, resources={'cpu': 2}), task('m1', utility=3, duration=3, resources={'cpu': 2}), task('m2', utility=3, duration=3, resources={'cpu': 2})]))
        self.assertEqual((r['objective'], sorted(r['selected'])), (6, ['m1', 'm2'])); self.assertTrue(r['oracle_agreement']['agree'])

    def test_solver_matches_oracle_on_random_small_instances(self):
        rng = random.Random(20260928)
        agree = 0
        for n in range(30):
            T = rng.randint(3, 6); ntask = rng.randint(1, 4)
            tasks = []
            for i in range(ntask):
                d = rng.randint(1, min(3, T)); t = task('t%d' % i, utility=rng.randint(0, 6), duration=d, power_high=rng.choice([0, 100, 300, 700]), resources={'cpu': rng.randint(0, 2)}, latest_start=rng.randint(0, T - d))
                if i and rng.random() < 0.3:
                    t['dependencies'] = ['t%d' % rng.randrange(i)]
                if i and rng.random() < 0.3:
                    t['exclusive_with'] = ['t%d' % rng.randrange(i)]
                if rng.random() < 0.15:
                    t['mandatory'] = True
                tasks.append(t)
            d = instance(slots=T, supply_low=[rng.choice([0, 300, 600])] * T, supply_high=[900] * T, base_low=[50] * T, base_high=[rng.choice([100, 400])] * T, initial_low=rng.choice([25000, 50000]), tasks=tasks)
            r = rp.solve(d)
            o = r['oracle']
            if o['best'] is None:
                self.assertEqual(r['status'], 'infeasible_established_by_enumeration', r)
            else:
                self.assertEqual(r['status'], 'optimal_within_tolerance', r); self.assertEqual(r['objective'], o['best']['utility'])
                self.assertTrue(rp.simulate(d, r['assignments'])['feasible'])
            agree += 1
        self.assertEqual(agree, 30)

    def test_status_mapping_never_turns_a_timeout_into_infeasible_and_rejects_bad_candidates(self):
        d = instance(tasks=[task('a', utility=2), task('b', utility=1)])
        import scipy.optimize
        real = scipy.optimize.milp
        def limited(*a, **k):
            res = real(*a, **k); res.x = None; res.status = 1; res.message = 'Time limit reached'
            return res
        with mock.patch('scipy.optimize.milp', limited):
            r = rp.build_and_solve(d)
        self.assertEqual(r['status'], 'limit_no_candidate'); self.assertIn('not evidence of infeasibility', r['reason'])
        # a non-integral relaxation is rejected before any checker
        def fractional(*a, **k):
            res = real(*a, **k); res.x = res.x.copy(); res.x[0] = 0.4
            return res
        with mock.patch('scipy.optimize.milp', fractional):
            r = rp.build_and_solve(d)
        self.assertEqual(r['status'], 'candidate_rejected_by_checker')
        # an integral but infeasible candidate (all starts set) is rejected by the exact simulator, and the oracle still finds the true optimum
        def wrong(*a, **k):
            res = real(*a, **k); res.x = res.x.copy(); res.x[:res.x.shape[0] - 13] = 1.0
            return res
        with mock.patch('scipy.optimize.milp', wrong):
            case = rp._solve_case(d, None)
        self.assertEqual(case['status'], 'candidate_rejected_by_checker'); self.assertIn('more than one start', case['reason']); self.assertEqual(case['oracle']['best']['utility'], 3)
        # a heuristic incumbent is never called optimal
        def incumbent(*a, **k):
            res = real(*a, **k); res.status = 1; res.message = 'Time limit reached'
            return res
        with mock.patch('scipy.optimize.milp', incumbent):
            r = rp.build_and_solve(d)
        self.assertEqual(r['status'], 'feasible_incumbent_no_optimality_claim')
        # solver facts are preserved
        r = rp.build_and_solve(d)
        self.assertEqual(set(r['solver']) >= {'name', 'scipy', 'status_code', 'runtime_s', 'time_limit_s', 'mip_gap', 'dual_bound', 'primal_objective', 'tie_break', 'max_binary_rounding'}, True)

    def test_sweep_dominance_dedup_and_sensitivity_including_negative_findings(self):
        d = instance(tasks=[task('a', utility=5, duration=2, power_high=400, cost=3), task('b', utility=4, cost=2, dependencies=['a']), task('c', utility=3, duration=3, cost=1, exclusive_with=['a'])],
                     objectives={'mode': 'cost_sweep', 'cost_ceilings': [0, 1, 3, 6, 10]},
                     sensitivity=[{'parameter': 'reserve', 'value': 21000}, {'parameter': 'capacity', 'value': 200000}, {'parameter': 'reserve', 'value': 80000}, {'parameter': 'supply_scale_percent', 'value': 30}])
        r = rp.solve(d)
        cands = r['alternatives']['candidates']
        by = {c['cost_ceiling']: c for c in cands}
        self.assertEqual([c['cost_ceiling'] for c in cands], [0, 1, 3, 6]); self.assertEqual(by[6]['also_optimal_for_ceilings'], [10])      # identical schedules deduplicated
        self.assertEqual((by[0]['utility'], by[1]['utility'], by[3]['utility'], by[6]['utility']), (0, 3, 5, 12))
        self.assertTrue(all(c['pareto_within_sweep'] for c in cands))                        # utility rises with cost: incomparable, none dominated
        # a tightened reserve inside the sweep changes candidates; the recorded sweep stays bound to its own inputs
        r2 = rp.solve(dict(d, reserve=80000, sensitivity=[]))
        self.assertNotEqual([c['utility'] for c in r2['alternatives']['candidates']], [c['utility'] for c in cands])
        self.assertEqual([c['utility'] for c in rp.solve(dict(d, sensitivity=[]))['alternatives']['candidates']], [c['utility'] for c in cands])
        rows = {(x['change']['parameter'], x['change']['value']): x for x in r['sensitivity']['rows']}
        self.assertFalse(rows[('reserve', 21000)]['decision_changed'])                     # small change: no effect (honest negative finding)
        self.assertFalse(rows[('capacity', 200000)]['decision_changed'])                   # apparently important parameter, irrelevant in the tested range
        self.assertTrue(rows[('reserve', 80000)]['decision_changed'])                      # the bottleneck: feasibility/selection changes
        self.assertEqual(r['sensitivity']['value_of_information_proxy']['count'], sum(1 for x in r['sensitivity']['rows'] if x['decision_changed']))
        self.assertIn('not a monetary valuation', r['sensitivity']['value_of_information_proxy']['note'])
        self.assertIn('not a derivative', r['sensitivity']['method'])

    def test_dominated_alternative_is_marked(self):
        # a cheaper ceiling that reaches the same utility with the same margin dominates a costlier duplicate-free candidate only through the recorded fields
        d = instance(tasks=[task('a', utility=2, cost=2, power_high=0), task('b', utility=2, cost=5, power_high=600)], objectives={'mode': 'cost_sweep', 'cost_ceilings': [2, 5, 7]})
        r = rp.solve(d)
        by = {c['cost_ceiling']: c for c in r['alternatives']['candidates']}
        self.assertEqual((by[2]['utility'], by[2]['also_optimal_for_ceilings'], by[7]['utility']), (2, [5], 4)); self.assertTrue(by[7]['pareto_within_sweep'] and by[2]['pareto_within_sweep'])
        d2 = instance(tasks=[task('a', utility=2, cost=2, power_high=0), task('b', utility=0, cost=5, power_high=600)], objectives={'mode': 'cost_sweep', 'cost_ceilings': [2, 7]})
        r2 = rp.solve(d2)
        c2 = {c['cost_ceiling']: c for c in r2['alternatives']['candidates']}
        self.assertEqual(c2[2]['also_optimal_for_ceilings'], [7])                           # the zero-utility task is never added: identical schedule, deduplicated


if __name__ == '__main__':
    unittest.main()
