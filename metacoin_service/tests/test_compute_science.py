"""Scientific core of the compute services, checked against structurally independent references on the CPU backend
and, when a CUDA device is present, on the GPU backend. Runs under an interpreter that has numpy (and torch for CUDA)."""
import math
import random
import unittest
from fractions import Fraction
from metacoin_service.compute import inputs, kernels, reference, npy
from metacoin_service.compute.kernels import Backend
from metacoin_service import temporal

try:
    import numpy  # noqa
    HAVE_NUMPY = True
except ImportError:
    HAVE_NUMPY = False
try:
    import torch
    HAVE_CUDA = torch.cuda.is_available()
except Exception:
    HAVE_CUDA = False


def base_temporal(**over):
    d = {'schema': temporal.INPUT_SCHEMA, 'capacity': 10_000, 'initial_low': 6_000, 'initial_high': 6_000, 'reserve': 2_000,
         'segments': [{'duration': 10, 'harvest_low': 600, 'harvest_high': 800, 'load_low': 500, 'load_high': 500, 'leakage_low': 0, 'leakage_high': 0},
                      {'duration': 30, 'harvest_low': 0, 'harvest_high': 0, 'load_low': 100, 'load_high': 200, 'leakage_low': 0, 'leakage_high': 5},
                      {'duration': 20, 'harvest_low': 900, 'harvest_high': 1200, 'load_low': 0, 'load_high': 10, 'leakage_low': 0, 'leakage_high': 0}],
         'units': dict(temporal.UNITS), 'assumptions': list(temporal.ASSUMPTIONS), 'provenance': 'synthetic', 'private_label': 'COMPUTE_SYNTHETIC'}
    d.update(over)
    return d


DURATIONS = (3, 7, 1, 12, 5, 9)


def random_scenario(rng, segments=6):
    cap = rng.randint(1, 5000)
    segs = []
    for d in DURATIONS[:segments]:                      # one duration schedule per batch (the batch contract)
        h = sorted(rng.randint(0, 400) for _ in range(2)); l = sorted(rng.randint(0, 400) for _ in range(2)); k = sorted(rng.randint(0, 20) for _ in range(2))
        segs.append({'duration': d, 'harvest_low': h[0], 'harvest_high': h[1], 'load_low': l[0], 'load_high': l[1], 'leakage_low': k[0], 'leakage_high': k[1]})
    init = sorted(rng.randint(0, cap) for _ in range(2))
    return base_temporal(capacity=cap, initial_low=init[0], initial_high=init[1], reserve=rng.randint(0, cap), segments=segs)


@unittest.skipUnless(HAVE_NUMPY, 'numpy not installed in this interpreter')
class TemporalBatchKernelTests(unittest.TestCase):
    def backends(self):
        return [Backend('cpu')] + ([Backend('cuda')] if HAVE_CUDA else [])

    def test_kernel_matches_reference_exactly_on_random_and_edge_scenarios(self):
        rng = random.Random(20260924)
        scenarios = [random_scenario(rng) for _ in range(400)]
        # deliberate edge cases in one batch: early deficit then surplus, capacity spill, exact reserve contact, zero-width, initial violation
        cap_cases = base_temporal(segments=[{'duration': 3, 'harvest_low': 0, 'harvest_high': 0, 'load_low': 3000, 'load_high': 3000, 'leakage_low': 0, 'leakage_high': 0},
                                            {'duration': 100, 'harvest_low': 500, 'harvest_high': 500, 'load_low': 0, 'load_high': 0, 'leakage_low': 0, 'leakage_high': 0},
                                            {'duration': 10, 'harvest_low': 100, 'harvest_high': 100, 'load_low': 0, 'load_high': 0, 'leakage_low': 0, 'leakage_high': 0}])
        contact = base_temporal(initial_low=2000, initial_high=2000, segments=[{'duration': 4, 'harvest_low': 0, 'harvest_high': 0, 'load_low': 0, 'load_high': 0, 'leakage_low': 0, 'leakage_high': 0}]*3)
        bad_start = base_temporal(initial_low=1000, initial_high=1500, segments=[{'duration': 4, 'harvest_low': 10, 'harvest_high': 10, 'load_low': 0, 'load_high': 0, 'leakage_low': 0, 'leakage_high': 0}]*3)
        # groups must share a duration schedule; the batch kernel is applied per group
        groups = [scenarios, [cap_cases], [contact], [bad_start]]
        for be in self.backends():
            for group in groups:
                rows = kernels.temporal_batch(be, kernels.temporal_chunk_from_scenarios(group))
                for sc, row in zip(group, rows):
                    self.assertEqual(row, reference.temporal_reference_row(sc), (be.name, sc['private_label'], reference.row_view(row)))
            outcomes = {row[0] for group in groups for row in kernels.temporal_batch(be, kernels.temporal_chunk_from_scenarios(group))}
            self.assertEqual(outcomes, {0, 1, 2}, 'the batches together should contain every outcome')

    def test_named_edge_semantics(self):
        be = Backend('cpu')
        early = base_temporal(segments=[{'duration': 3, 'harvest_low': 0, 'harvest_high': 0, 'load_low': 3000, 'load_high': 3000, 'leakage_low': 0, 'leakage_high': 0},
                                        {'duration': 100, 'harvest_low': 500, 'harvest_high': 500, 'load_low': 0, 'load_high': 0, 'leakage_low': 0, 'leakage_high': 0}])
        row = reference.row_view(kernels.temporal_batch(be, kernels.temporal_chunk_from_scenarios([early]))[0])
        self.assertEqual(row['outcome'], kernels.OUTCOME_CODES['INFEASIBLE'])       # early deficit despite later surplus
        self.assertEqual((row['viol_seg_high'], row['viol_num_high'], row['viol_den_high']), (0, 4, 3))   # (6000-2000)/3000 = 4/3 s
        self.assertGreater(row['spill_high'], 0)                                       # later surplus spills at capacity
        self.assertEqual(row['final_high'], 10_000)
        # zero-width uncertainty: low == high rows agree
        self.assertEqual((row['min_margin_low'], row['spill_low']), (row['min_margin_high'], row['spill_high']))

    def test_overflow_bound_refuses_unsafe_batches_and_accepts_wide_safe_ones(self):
        big = base_temporal(capacity=2 ** 40, initial_low=2 ** 39, initial_high=2 ** 39, reserve=1,
                            segments=[{'duration': 2 ** 20, 'harvest_low': 0, 'harvest_high': 2 ** 32, 'load_low': 0, 'load_high': 0, 'leakage_low': 0, 'leakage_high': 0}] * 12)
        temporal.validate(big)                                                            # the single-scenario verifier accepts it; the 100x scaled batch swing would exceed 2^62
        with self.assertRaises(Exception):
            inputs.validate_temporal_batch({'schema': inputs.TEMPORAL_BATCH_SCHEMA, 'base': big, 'scenarios': [{'harvest_scale_percent': 10_000}], 'grid': None, 'device_policy': 'cpu', 'verification': 'auto', 'private_label': 'x'})
        ok = {'schema': inputs.TEMPORAL_BATCH_SCHEMA, 'base': base_temporal(), 'scenarios': None, 'grid': [{'path': 'reserve', 'start': 0, 'stop': 9000, 'step': 1000}, {'path': 'load_scale_percent', 'values': [50, 100, 150]}],
              'device_policy': 'auto', 'verification': 'auto', 'private_label': 'x'}
        inputs.validate_temporal_batch(ok)
        self.assertEqual(inputs.batch_total(ok), 30)
        sid, sc, var = inputs.scenario(ok, 29)
        self.assertEqual((sid, var), ('grid-29', {'reserve': 9000, 'load_scale_percent': 150}))
        self.assertEqual(sc['segments'][0]['load_high'], 750)


@unittest.skipUnless(HAVE_NUMPY, 'numpy not installed in this interpreter')
class MonteCarloKernelTests(unittest.TestCase):
    def spec(self, samples=3000, seed=7):
        return {'schema': inputs.MONTE_CARLO_SCHEMA, 'base': base_temporal(initial_low=6000, initial_high=6000, segments=[
                    {'duration': 10, 'harvest_low': 600, 'harvest_high': 600, 'load_low': 500, 'load_high': 500, 'leakage_low': 0, 'leakage_high': 0},
                    {'duration': 30, 'harvest_low': 0, 'harvest_high': 0, 'load_low': 150, 'load_high': 150, 'leakage_low': 0, 'leakage_high': 0}]),
                'distributions': {'initial_energy': {'type': 'finite', 'values': [4000, 6000, 8000], 'weights': [1, 2, 1]},
                                  'load_scale_percent': {'type': 'uniform_int', 'low': 50, 'high': 200}},
                'samples': samples, 'seed': seed, 'confidence_percent': 95, 'event': 'reserve_maintained', 'device_policy': 'auto', 'private_label': 'MC_SYNTHETIC'}

    def test_stream_is_chunk_invariant_and_matches_the_reference_per_sample(self):
        spec = self.spec(); normalized = inputs.validate_monte_carlo(spec)
        be = Backend('cpu')
        whole = kernels.mc_chunk(be, spec['base'], normalized, spec['seed'], 0, spec['samples'])
        pieces = []
        for first in range(0, spec['samples'], 700):
            pieces.append(kernels.mc_chunk(be, spec['base'], normalized, spec['seed'], first, min(700, spec['samples'] - first)))
        self.assertTrue(whole[1] == [x for p in pieces for x in p[1]])                   # chunking never changes logical samples
        self.assertEqual(whole[0], sum(p[0] for p in pieces))
        # every audited sample re-evaluated through the reference verifier with the same mapped parameters
        rng = random.Random(1)
        for i in rng.sample(range(spec['samples']), 200):
            var = {k: v[i] for k, v in whole[2].items()}
            sc = inputs.apply_variation(spec['base'], {'initial_low': var['initial_energy'], 'initial_high': var['initial_energy'], 'load_scale_percent': var['load_scale_percent']})
            ref = temporal.analyze(sc)
            self.assertEqual(whole[1][i], 1 if ref['outcome'] == 'FEASIBLE' else 0, i)
        # marginal frequencies of the finite distribution at a fixed seed (deterministic regression, not a flaky statistical test)
        counts = {v: whole[2]['initial_energy'].count(v) for v in (4000, 6000, 8000)}
        self.assertLess(abs(counts[6000] / spec['samples'] - 0.5), 0.03, counts)
        if HAVE_CUDA:
            gpu = kernels.mc_chunk(Backend('cuda'), spec['base'], normalized, spec['seed'], 0, spec['samples'])
            self.assertTrue(gpu[1] == whole[1])

    def test_wilson_interval_edge_cases(self):
        w = kernels.wilson_interval(0, 50, 95); self.assertEqual(w['low'], 0.0); self.assertLess(w['high'], 0.1)
        w = kernels.wilson_interval(50, 50, 95); self.assertEqual(w['high'], 1.0); self.assertGreater(w['low'], 0.9)
        w = kernels.wilson_interval(5, 10, 99); self.assertTrue(w['low'] < 0.5 < w['high'])
        self.assertIsNone(kernels.wilson_interval(0, 0, 95))
        try:
            from scipy.stats import binomtest
            ci = binomtest(30, 100).proportion_ci(confidence_level=0.95, method='wilson')
            w = kernels.wilson_interval(30, 100, 95)
            self.assertAlmostEqual(w['low'], ci.low, places=9); self.assertAlmostEqual(w['high'], ci.high, places=9)
        except ImportError:
            pass


@unittest.skipUnless(HAVE_NUMPY, 'numpy not installed in this interpreter')
class HeatKernelTests(unittest.TestCase):
    def spec(self, **over):
        d = {'schema': inputs.HEAT_SCHEMA, 'nx': 12, 'ny': 10, 'dx': '0.1', 'dy': '0.1', 'dt': '0.002', 'alpha': '1.0', 'steps': 50,
             'boundary': {'type': 'dirichlet', 'values': {'left': '0', 'right': '0', 'top': '0', 'bottom': '0'}},
             'initial': {'type': 'hot_rectangle', 'x0': 4, 'x1': 7, 'y0': 3, 'y1': 6, 'inside': '100', 'outside': '0'}, 'snapshots': 2,
             'units': {'field': 'K', 'length': 'm', 'time': 's'}, 'device_policy': 'auto', 'precision': 'float64', 'private_label': 'HEAT_SYNTHETIC'}
        d.update(over)
        return d

    def test_stability_is_checked_exactly_and_unstable_timesteps_refused(self):
        p = inputs.validate_heat(self.spec())
        self.assertEqual(p['rx'] + p['ry'], Fraction(2, 5))
        with self.assertRaises(Exception) as ctx:
            inputs.validate_heat(self.spec(dt='0.0025000001'))
        self.assertIn('unstable timestep', str(ctx.exception))
        inputs.validate_heat(self.spec(dt='0.0025'))                                       # exact boundary r_x + r_y = 1/2 is allowed
        with self.assertRaises(Exception):
            inputs.validate_heat(self.spec(dt='1e-3'))                                      # not a bounded decimal string

    def test_matches_scalar_reference_and_invariants_on_every_backend(self):
        spec = self.spec(); p = inputs.validate_heat(spec)
        rx, ry = float(p['rx']), float(p['ry'])
        f0 = kernels.heat_initial_field(spec, p)
        ref = reference.heat_reference(f0, rx, ry, spec['steps'])
        for name in ['cpu'] + (['cuda'] if HAVE_CUDA else []):
            be = Backend(name)
            field, prev = kernels.heat_steps(be, be.floats([v for r in f0 for v in r], (p['ny'], p['nx'])), rx, ry, spec['steps'])
            rows = be.tolist(field)
            self.assertLess(reference.heat_max_abs_diff(rows, ref), 1e-9, name)
            self.assertTrue(be.isfinite_all(field))
            # boundaries fixed, discrete maximum principle
            self.assertTrue(all(rows[j][0] == 0 and rows[j][-1] == 0 for j in range(p['ny'])))
            self.assertTrue(0 <= min(min(r) for r in rows) and max(max(r) for r in rows) <= 100)

    def test_zero_diffusivity_identity_constant_equilibrium_and_eigenmode(self):
        be = Backend('cpu')
        spec = self.spec(alpha='0'); p = inputs.validate_heat(spec)
        f0 = kernels.heat_initial_field(spec, p)
        field, _ = kernels.heat_steps(be, be.floats([v for r in f0 for v in r], (p['ny'], p['nx'])), 0.0, 0.0, 20)
        self.assertEqual(be.tolist(field), f0)
        spec = self.spec(initial={'type': 'uniform', 'value': '7'}, boundary={'type': 'dirichlet', 'values': {'left': '7', 'right': '7', 'top': '7', 'bottom': '7'}}); p = inputs.validate_heat(spec)
        f0 = kernels.heat_initial_field(spec, p)
        field, _ = kernels.heat_steps(be, be.floats([v for r in f0 for v in r], (p['ny'], p['nx'])), float(p['rx']), float(p['ry']), 30)
        self.assertTrue(all(abs(v - 7) < 1e-12 for r in be.tolist(field) for v in r))
        # discrete sine eigenmode: every step multiplies the field by the closed-form factor
        spec = self.spec(nx=17, ny=17, initial={'type': 'sine_mode', 'm': 1, 'n': 2, 'amplitude': '1'}, steps=25); p = inputs.validate_heat(spec)
        f0 = kernels.heat_initial_field(spec, p)
        field, _ = kernels.heat_steps(be, be.floats([v for r in f0 for v in r], (p['ny'], p['nx'])), float(p['rx']), float(p['ry']), spec['steps'])
        g = reference.heat_eigenmode_factor(1, 2, 17, 17, float(p['rx']), float(p['ry'])) ** spec['steps']
        rows = be.tolist(field)
        self.assertLess(max(abs(rows[j][i] - g * f0[j][i]) for j in range(17) for i in range(17)), 1e-12)
        # refinement: halving dx, dy and quartering dt (same r) reduces the error against the continuous eigenmode decay
        errs = []
        for n in (9, 17, 33):
            dx = 1.0 / (n - 1); dt = 0.2 * dx * dx                                       # r_x = r_y = 0.2
            s = self.spec(nx=n, ny=n, dx=repr(dx)[:14] if 'e' not in repr(dx) else '%.12f' % dx, dy=repr(dx)[:14] if 'e' not in repr(dx) else '%.12f' % dx, dt='%.12f' % dt,
                          initial={'type': 'sine_mode', 'm': 1, 'n': 1, 'amplitude': '1'}, steps=int(round(0.02 / dt)))
            pp = inputs.validate_heat(s)
            f0 = kernels.heat_initial_field(s, pp)
            field, _ = kernels.heat_steps(be, be.floats([v for r in f0 for v in r], (n, n)), float(pp['rx']), float(pp['ry']), s['steps'])
            t = float(pp['dt']) * s['steps']
            exact = math.exp(-2 * math.pi ** 2 * t)                                       # continuous decay of sin(pi x) sin(pi y) with alpha = 1
            rows = be.tolist(field)
            errs.append(max(abs(rows[j][i] - exact * f0[j][i]) for j in range(n) for i in range(n)))
        self.assertTrue(errs[0] > errs[1] > errs[2], errs)
        self.assertLess(errs[2], errs[0] / 4, errs)                                       # roughly second-order trend in this regime


class NpyCodecTests(unittest.TestCase):
    def test_roundtrip_and_refusals(self):
        data = npy.encode([1, -2, 3, 4, 5, 6], '<i8', (2, 3))
        vals, dtype, shape = npy.decode(data)
        self.assertEqual((vals, dtype, shape), ([1, -2, 3, 4, 5, 6], '<i8', [2, 3]))
        f = npy.encode([0.5, 1e300, -3.25], '<f8', (3,))
        self.assertEqual(npy.decode(f)[0], [0.5, 1e300, -3.25])
        with self.assertRaises(ValueError):
            npy.decode(data.replace(b"'<i8'", b"'|O8'"))
        with self.assertRaises(ValueError):
            npy.decode(data.replace(b'False', b'True '))
        with self.assertRaises(ValueError):
            npy.decode(data[:-3])
        if HAVE_NUMPY:
            import numpy as np, io
            arr = np.load(io.BytesIO(data), allow_pickle=False)
            self.assertEqual(arr.tolist(), [[1, -2, 3], [4, 5, 6]])


if __name__ == '__main__':
    unittest.main()
