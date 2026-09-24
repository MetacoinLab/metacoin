"""Temporal energy model: analytical cases, a rational-arithmetic reference with a different
structure (dense per-second simulation), and exhaustive enumeration of tiny integer inputs."""
from fractions import Fraction
import itertools
import random
import unittest
from experiments.private_receipts.receipt import Invalid
from metacoin_service import temporal


def base(**kw):
    d = {'schema': temporal.INPUT_SCHEMA, 'capacity': 10_000, 'initial_low': 6_000, 'initial_high': 6_000, 'reserve': 2_000,
         'segments': [], 'units': dict(temporal.UNITS), 'assumptions': list(temporal.ASSUMPTIONS), 'provenance': 'synthetic',
         'private_label': 'TEMPORAL_PRIVATE_CANARY_88'}
    d.update(kw)
    return d


def seg(duration, harvest=(0, 0), load=(0, 0), leakage=(0, 0)):
    return {'duration': duration, 'harvest_low': harvest[0], 'harvest_high': harvest[1], 'load_low': load[0], 'load_high': load[1],
            'leakage_low': leakage[0], 'leakage_high': leakage[1]}


def simulate_per_second(initial, choices, data):
    """Reference: integrate second by second with Fractions (no per-segment closed form)."""
    e = Fraction(initial)
    cap, r = Fraction(data['capacity']), Fraction(data['reserve'])
    ok = e >= r
    for (h, l, k), s in zip(choices, data['segments']):
        for _ in range(s['duration']):
            e = min(cap, e + Fraction(h - l - k))
            if e < r:
                ok = False
    return ok, e


def enumerate_classification(data, initial_values):
    """Every integer trajectory: each segment's harvest/load/leakage chosen anywhere in its bounds."""
    per_segment = [list(itertools.product(range(s['harvest_low'], s['harvest_high'] + 1), range(s['load_low'], s['load_high'] + 1),
                                          range(s['leakage_low'], s['leakage_high'] + 1))) for s in data['segments']]
    results = [simulate_per_second(e0, choices, data)[0] for e0 in initial_values for choices in itertools.product(*per_segment)]
    if all(results):
        return 'FEASIBLE'
    if not any(results):
        return 'INFEASIBLE'
    return 'INDETERMINATE'


class TemporalAnalyticalTests(unittest.TestCase):
    def test_early_deficit_despite_aggregate_surplus(self):
        # 3 s of load 3000 mW with no harvest from 6000 mJ above reserve 2000: energy hits 2000 at t=1.333 s... -> below reserve
        # then a long strong harvest: aggregate balance is positive but the schedule is unacceptable.
        d = base(segments=[seg(3, load=(3000, 3000)), seg(100, harvest=(500, 500))])
        out = temporal.analyze(d)
        self.assertEqual(out['outcome'], 'INFEASIBLE')
        self.assertTrue(out['aggregate_balance_optimistic_ok'])
        self.assertTrue(out['aggregate_analysis_misses_early_deficit'])
        # first violation: from 6000 to reserve 2000 at 3000 mW takes 4/3 s
        self.assertEqual(out['first_infeasible_boundary'], {'segment': 0, 't_start': 0, 'offset_seconds': [4, 3]})
        self.assertEqual(out['envelope_high'][1]['energy'], -3000)          # virtual energy propagates the deficit
        self.assertEqual(out['final_energy_bounds'], [10_000, 10_000])       # later surplus saturates at capacity
        self.assertEqual(out['spill_bounds'], [37_000, 37_000])              # -3000 + 50000 - 10000

    def test_pure_discharge_and_exact_reserve_contact(self):
        d = base(segments=[seg(4, load=(1000, 1000))])                       # 6000 - 4000 = 2000 == reserve
        self.assertEqual(temporal.analyze(d)['outcome'], 'FEASIBLE')
        self.assertEqual(temporal.analyze(d)['min_reserve_margin_pessimistic'], 0)
        d = base(segments=[seg(4, load=(1000, 1001))])                       # pessimistic 1996 < reserve, optimistic 2000
        self.assertEqual(temporal.analyze(d)['outcome'], 'INDETERMINATE')
        d = base(segments=[seg(5, load=(1000, 1000))])
        out = temporal.analyze(d)
        self.assertEqual((out['outcome'], out['first_infeasible_boundary']['offset_seconds']), ('INFEASIBLE', [4, 1]))

    def test_charging_saturates_and_spills(self):
        d = base(segments=[seg(10, harvest=(1000, 2000))])                   # +10000..+20000 from 6000 into cap 10000
        out = temporal.analyze(d)
        self.assertEqual((out['outcome'], out['final_energy_bounds'], out['spill_bounds']), ('FEASIBLE', [10_000, 10_000], [6_000, 16_000]))

    def test_spill_then_later_deficit(self):
        d = base(segments=[seg(10, harvest=(2000, 2000)), seg(9, load=(1000, 1000))])   # full at 10000, then 9000 drain -> 1000 < 2000
        out = temporal.analyze(d)
        self.assertEqual(out['outcome'], 'INFEASIBLE')
        self.assertEqual(out['first_infeasible_boundary']['segment'], 1)
        self.assertEqual(out['first_infeasible_boundary']['offset_seconds'], [8, 1])

    def test_harvest_equal_load_leakage_only_and_zero_width(self):
        d = base(segments=[seg(1000, harvest=(500, 500), load=(500, 500))])
        out = temporal.analyze(d)
        self.assertEqual((out['outcome'], out['min_reserve_margin_pessimistic'], out['dominant_uncertainty_source']), ('FEASIBLE', 4000, 'none'))
        d = base(segments=[seg(5000, leakage=(1, 1))])                        # 6000 - 5000 = 1000 < 2000
        self.assertEqual(temporal.analyze(d)['outcome'], 'INFEASIBLE')
        d = base(initial_low=6000, initial_high=6000, segments=[seg(1, harvest=(5, 5), load=(5, 5), leakage=(0, 0))])
        self.assertEqual(temporal.analyze(d)['uncertainty_contributions'], [{'source': 'initial_energy', 'index': None, 'width': 0}])

    def test_uncertainty_straddling_and_refinement(self):
        d = base(segments=[seg(4, load=(900, 1100))])                          # pessimistic 1600 < 2000 <= optimistic 2400
        out = temporal.analyze(d)
        self.assertEqual((out['outcome'], out['dominant_uncertainty_source']), ('INDETERMINATE', 'load:0'))
        self.assertEqual(out['first_uncertain_boundary']['segment'], 0)
        self.assertIsNone(out['first_infeasible_boundary'])
        r = temporal.refine(d, [{'segment': 0, 'field': 'load', 'low': 900, 'high': 1000}])
        self.assertEqual((r['original_outcome'], r['refined_outcome'], r['resolves_uncertainty'], r['declared_width_removed_mJ']), ('INDETERMINATE', 'FEASIBLE', True, 400))
        self.assertEqual(temporal.analyze(d)['outcome'], 'INDETERMINATE')     # original unchanged
        with self.assertRaisesRegex(Invalid, 'not a subset'):
            temporal.refine(d, [{'segment': 0, 'field': 'load', 'low': 800, 'high': 1000}])
        with self.assertRaises(Invalid):
            temporal.refine(d, [{'segment': 3, 'field': 'load', 'low': 900, 'high': 950}])

    def test_domain_refusals(self):
        for bad in (dict(capacity=0), dict(reserve=20_000), dict(initial_high=20_000), dict(initial_low=7000),
                    dict(segments=[seg(0)]), dict(segments=[seg(1, harvest=(2, 1))]), dict(segments=[]),
                    dict(segments=[seg(1)] * 513), dict(segments=[seg(10 ** 9 + 1)]), dict(provenance='guessed'),
                    dict(schema='temporal-energy-input/v0'), dict(units={'energy': 'J', 'power': 'W', 'duration': 's'})):
            with self.subTest(bad=list(bad)), self.assertRaises(Invalid):
                temporal.analyze(base(**bad))
        with self.assertRaises(Invalid):
            temporal.analyze(dict(base(segments=[seg(1)]), reserve=1.5))


class TemporalReferenceTests(unittest.TestCase):
    def test_exhaustive_tiny_integer_cases(self):
        """Every integer trajectory of tiny inputs is simulated per second with Fractions; the
        model's envelope classification must agree. Covers: 1-3 segments, durations 1-3, powers 0-3,
        capacity 4-9, reserve 0-4, initial intervals of width 0-2."""
        rng = random.Random(2_409)
        checked = 0
        for _ in range(400):
            cap = rng.randint(4, 9)
            r = rng.randint(0, 4)
            il = rng.randint(r, cap); ih = min(cap, il + rng.randint(0, 2))
            segs = []
            for __ in range(rng.randint(1, 3)):
                h = rng.randint(0, 3); l = rng.randint(0, 3); k = rng.randint(0, 1)
                segs.append(seg(rng.randint(1, 3), harvest=(h, h + rng.randint(0, 1)), load=(l, l + rng.randint(0, 1)), leakage=(k, k)))
            d = base(capacity=cap, initial_low=il, initial_high=ih, reserve=r, segments=segs)
            out = temporal.analyze(d)
            expected = enumerate_classification(d, range(il, ih + 1))
            self.assertEqual(out['outcome'], expected, d)
            # envelope endpoints must equal the per-second reference for the extreme choices
            low_choice = [(s['harvest_low'], s['load_high'], s['leakage_high']) for s in segs]
            high_choice = [(s['harvest_high'], s['load_low'], s['leakage_low']) for s in segs]
            self.assertEqual(Fraction(out['final_energy_bounds'][0]), simulate_per_second(il, low_choice, d)[1])
            self.assertEqual(Fraction(out['final_energy_bounds'][1]), simulate_per_second(ih, high_choice, d)[1])
            checked += 1
        self.assertEqual(checked, 400)

    def test_seeded_larger_cases_against_per_second_reference(self):
        rng = random.Random(77)
        for _ in range(60):
            cap = rng.randint(5_000, 50_000)
            r = rng.randint(0, cap // 2)
            il = rng.randint(r, cap); ih = min(cap, il + rng.randint(0, 3_000))
            segs = [seg(rng.randint(1, 40), harvest=(a := rng.randint(0, 2_000), a + rng.randint(0, 500)),
                        load=(b := rng.randint(0, 2_000), b + rng.randint(0, 500)), leakage=(c := rng.randint(0, 20), c + rng.randint(0, 5)))
                    for __ in range(rng.randint(1, 8))]
            d = base(capacity=cap, initial_low=il, initial_high=ih, reserve=r, segments=segs)
            out = temporal.analyze(d)
            low_ok, low_end = simulate_per_second(il, [(s['harvest_low'], s['load_high'], s['leakage_high']) for s in segs], d)
            high_ok, high_end = simulate_per_second(ih, [(s['harvest_high'], s['load_low'], s['leakage_low']) for s in segs], d)
            expected = 'FEASIBLE' if low_ok else ('INFEASIBLE' if not high_ok else 'INDETERMINATE')
            self.assertEqual(out['outcome'], expected)
            self.assertEqual(out['final_energy_bounds'], [int(low_end), int(high_end)])
            self.assertLessEqual(out['spill_bounds'][0], out['spill_bounds'][1])

    def test_result_structure_and_privacy(self):
        out = temporal.analyze(base(segments=[seg(2, load=(100, 200))]))
        for key in ('result_schema', 'model_id', 'outcome', 'horizon_seconds', 'envelope_low', 'envelope_high', 'min_reserve_margin_pessimistic',
                    'first_uncertain_boundary', 'first_infeasible_boundary', 'spill_bounds', 'assumptions', 'units', 'verifier_digest'):
            self.assertIn(key, out)
        self.assertNotIn('TEMPORAL_PRIVATE_CANARY_88', str(out))
        big = base(segments=[seg(1, load=(1, 1))] * 100)
        self.assertTrue(temporal.analyze(big)['envelope_low']['summarized'])
