"""Margin-width decomposition and counterfactual: algebra, boundaries, ordering, privacy."""
from copy import deepcopy
from fractions import Fraction
import json
import random
import unittest
from experiments.private_receipts import receipt as merkle
from experiments.work_contracts import acceptance, contract, energy_analysis as energy, explanation, fixtures


def reference_widths_wh(data):
    """Independent hand-style derivation in exact watt-hours (not the mJ code path).

    Width of the usable-energy bound, then per segment (P_high - P_low) * t, with
    mW*s -> Wh via /1000 * /3600. Returned in Wh so the comparison converts back."""
    battery = Fraction(data['available_high'] - data['available_low'], 3_600_000)
    segments = [Fraction(row['power_high'] - row['power_low'], 1000) * Fraction(row['duration'], 3600)
                for row in data['segments']]
    return battery, segments


def to_mj(value_wh):
    out = value_wh * 3_600_000
    assert out.denominator == 1
    return int(out)


class DecompositionTests(unittest.TestCase):
    def test_hand_derived_fixture(self):
        # available 650_000..680_000 (width 30_000), one segment 800..1000 mW for 600 s
        # -> segment width 200 mW * 600 s = 120_000 mJ; total 150_000 mJ.
        out = explanation.explain(fixtures.inputs('INDETERMINATE'))
        self.assertEqual((out['margin_low'], out['margin_high'], out['margin_width']), (-50_000, 100_000, 150_000))
        self.assertEqual([part['width'] for part in out['contributions']], [30_000, 120_000])
        self.assertEqual(out['ranked_sources'], ['segment:0', 'available_energy'])
        self.assertEqual(out['dominant_uncertainty_source'], 'segment:0')
        self.assertEqual(out['reserve_width'], 0)
        self.assertEqual(out['outcome'], 'INDETERMINATE')

    def test_algebra_against_independent_reference_on_seeded_inputs(self):
        rng = random.Random(41_079)
        for _ in range(200):
            data = fixtures.inputs()
            data['segments'] = [{'power_low': (low := rng.randint(0, 5000)), 'power_high': low + rng.randint(0, 5000),
                                 'duration': rng.randint(1, 10_000)} for __ in range(rng.randint(1, 12))]
            data['reserve'] = rng.randint(0, 100_000)
            data['available_low'] = rng.randint(0, 100_000_000)
            data['available_high'] = data['available_low'] + rng.randint(0, 100_000_000)
            out = explanation.explain(data)
            battery, segments = reference_widths_wh(data)
            self.assertEqual(out['contributions'][0]['width'], to_mj(battery))
            self.assertEqual([part['width'] for part in out['contributions'][1:]], [to_mj(x) for x in segments])
            self.assertEqual(out['margin_width'], to_mj(battery + sum(segments, Fraction(0))))
            # margin bounds agree with the analysis result exactly
            result = energy.analyze(data)
            self.assertEqual((out['margin_low'], out['margin_high']), (result['worst_margin'], result['best_margin']))
            # ranking is by width descending; ties keep declaration order
            widths = [next(p['width'] for p in out['contributions'] if explanation.label(p) == name)
                      for name in out['ranked_sources']]
            self.assertEqual(widths, sorted(widths, reverse=True))
            # changing the reserve never changes any width (it is fixed in this model)
            shifted = dict(data, reserve=data['reserve'] + 12_345)
            self.assertEqual(explanation.explain(shifted)['contributions'], out['contributions'])

    def test_zero_width_and_exact_boundary(self):
        data = fixtures.inputs()
        data.update(available_low=700_000, available_high=700_000,
                    segments=[{'duration': 600, 'power_low': 1000, 'power_high': 1000}])
        out = explanation.explain(data)
        self.assertEqual(out['margin_width'], 0)
        self.assertEqual(out['dominant_uncertainty_source'], 'none')
        self.assertEqual((out['margin_low'], out['margin_high'], out['outcome']), (0, 0, 'FEASIBLE'))
        data['available_low'] = data['available_high'] = 699_999
        self.assertEqual(explanation.explain(data)['outcome'], 'INFEASIBLE')

    def test_splitting_a_segment_preserves_total_width_and_margins(self):
        data = fixtures.inputs('INDETERMINATE')
        whole = explanation.explain(data)
        split = deepcopy(data)
        first = split['segments'][0]
        split['segments'] = [dict(first, duration=1), dict(first, duration=first['duration'] - 1)]
        parts = explanation.explain(split)
        self.assertEqual((parts['margin_low'], parts['margin_high'], parts['margin_width']),
                         (whole['margin_low'], whole['margin_high'], whole['margin_width']))
        self.assertEqual(sum(p['width'] for p in parts['contributions'][1:]), whole['contributions'][1]['width'])

    def test_tie_ordering_is_deterministic_by_declaration(self):
        data = fixtures.inputs()
        data.update(available_low=100, available_high=100 + 6000,
                    segments=[{'duration': 6, 'power_low': 0, 'power_high': 1000},
                              {'duration': 3, 'power_low': 0, 'power_high': 2000}])
        out = explanation.explain(data)
        self.assertEqual([p['width'] for p in out['contributions']], [6000, 6000, 6000])
        self.assertEqual(out['ranked_sources'], ['available_energy', 'segment:0', 'segment:1'])

    def test_domain_refusals_are_the_model_refusals(self):
        for bad in (dict(available_low=2, available_high=1), dict(segments=[]), dict(reserve=1.5)):
            with self.subTest(bad=list(bad)), self.assertRaises(merkle.Invalid):
                explanation.explain(dict(fixtures.inputs(), **bad))


class CounterfactualTests(unittest.TestCase):
    def test_threshold_is_minimal_on_fixtures_and_seeded_inputs(self):
        rng = random.Random(9_113)
        cases = [fixtures.inputs(o) for o in energy.OUTCOMES]
        for _ in range(100):
            data = fixtures.inputs()
            data['segments'] = [{'power_low': (low := rng.randint(0, 3000)), 'power_high': low + rng.randint(0, 3000),
                                 'duration': rng.randint(1, 5000)} for __ in range(rng.randint(1, 6))]
            data['available_low'] = rng.randint(0, 20_000_000)
            data['available_high'] = data['available_low'] + rng.randint(0, 5_000_000)
            cases.append(data)
        for data in cases:
            check = explanation.threshold_check(data)
            self.assertTrue(check['threshold_verified'], check)
            need = energy.analyze(data)['additional_usable_energy']
            self.assertEqual(need, max(0, energy.analyze(data)['required_high'] - data['available_low']))

    def test_counterfactual_is_labeled_and_preserves_the_original(self):
        data = fixtures.inputs('INFEASIBLE')
        before = deepcopy(data)
        out = explanation.conditional_outcome(data, 200_000)
        self.assertEqual(data, before)
        self.assertTrue(out['hypothetical'])
        self.assertEqual((out['original_outcome'], out['conditional_outcome']), ('INFEASIBLE', 'FEASIBLE'))
        self.assertIn('not-an-available-hardware-modification', out['note'])
        with self.assertRaises(merkle.Invalid):
            explanation.conditional_outcome(data, -1)
        with self.assertRaises(merkle.Invalid):
            explanation.conditional_outcome(data, energy.LIMIT)


class ExplanationPrivacyTests(unittest.TestCase):
    def test_private_by_default_public_only_under_explicit_policy(self):
        terms, inputs, evidence = fixtures.prepare('plain', 'INDETERMINATE')
        pin = contract.digest(terms)
        audited = acceptance.audit(terms, pin, inputs, evidence)
        names = {x['name'] for x in audited['bundle']['disclosures']}
        self.assertNotIn('margin_explanation', names)
        self.assertNotIn('dominant_uncertainty_source', names)
        public_text = json.dumps(audited['bundle'])
        for secret in ('margin_width', 'contributions', '150000', 'SYNTHETIC_PRIVATE_CANARY_73'):
            self.assertNotIn(secret, public_text)
        # the full private evidence does carry it, for the authorized auditor
        values = acceptance.full_values(evidence, evidence['receipt']['root'])
        self.assertEqual(values['margin_explanation']['margin_width'], 150_000)
        # an explicit policy may open it; verification then checks its envelope
        shown, inputs2, evidence2 = fixtures.prepare('shown', 'INDETERMINATE', disclose_explanation=True)
        pin2 = contract.digest(shown)
        audited2 = acceptance.audit(shown, pin2, inputs2, evidence2)
        public = acceptance.verify_public(shown, pin2, audited2['bundle'], audited2['evidence_root'])
        self.assertEqual(public['disclosed']['dominant_uncertainty_source'], 'segment:0')
        self.assertEqual(public['disclosed']['margin_explanation']['explanation_schema'], explanation.EXPLANATION_SCHEMA)
        # opening the explanation under a contract that did not permit it is refused
        extra = merkle.disclose(evidence, list(contract.BINDINGS) + ['outcome', 'margin_explanation'])
        with self.assertRaisesRegex(merkle.Invalid, 'prohibited public disclosure'):
            acceptance.verify_public(terms, pin, extra, evidence['receipt']['root'])
        # a disclosed explanation with a foreign schema is refused even if it is a true opening
        for key, value in (('explanation_schema', 'margin-decomposition/v9'), ('model_id', 'other')):
            _, forged_vault = merkle.commit(dict(acceptance.full_values(evidence2, evidence2['receipt']['root']),
                                                 margin_explanation=dict(values['margin_explanation'], **{key: value})))
            forged = merkle.disclose(forged_vault, shown['required_disclosures'])
            with self.subTest(key=key), self.assertRaisesRegex(merkle.Invalid, 'malformed disclosed explanation'):
                acceptance.verify_public(shown, pin2, forged, forged_vault['receipt']['root'])
