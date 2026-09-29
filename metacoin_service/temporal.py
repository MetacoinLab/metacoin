"""Time-dependent energy feasibility: `temporal-energy/v1`.

EXECUTABLE MODEL SPECIFICATION (integer mJ, mW, s; exact arithmetic)

Storage holds usable energy E(t) in [0, capacity]. Powers are declared at the usable-energy
boundary (conversion losses, if any, are already inside the declared bounds; there is no
efficiency constant). A schedule is a list of segments with strictly positive integer
duration d_i and independent interval bounds on harvest h, load l and leakage k (mW):

    net_low_i  = h_low_i  - l_high_i - k_high_i
    net_high_i = h_high_i - l_low_i  - k_low_i

Within a segment every trajectory has constant net power n in [net_low_i, net_high_i]
(piecewise-constant inputs). The update is linear with saturation at capacity:

    E(t0 + s) = min(capacity, E(t0) + n * s)          for 0 <= s <= d_i        (mJ = mW * s)

Deficits are NOT clamped: E may fall below the reserve (and below zero) as *virtual* energy so
that the first violation is detected and later evidence still propagates. The physical store
never holds negative charge; a negative virtual value means the schedule failed earlier.

Reserve r is a constant (0 <= r <= capacity) in this version. The requirement is E(t) >= r at
every t of the horizon [0, sum d_i], including t = 0.

Envelopes. Because min(capacity, .) is monotone, E_low (initial_low, every net_low_i) is a
pointwise lower bound on every allowed trajectory and E_high (initial_high, every
net_high_i) a pointwise upper bound; both are themselves allowed trajectories under the
independent-interval assumption (each bound is attainable, jointly). Within a segment a
trajectory is monotone up to saturation, so its minimum over the segment is at an endpoint
and its first crossing of r, if any, is at t = t0 + (E(t0) - r) / (-n) with n < 0.

Outcome (whole-horizon, conservative):
    FEASIBLE       min_t E_low(t)  >= r   (every allowed trajectory keeps the reserve)
    INFEASIBLE     min_t E_high(t) <  r   (even the optimistic attainable trajectory fails,
                                           so no allowed trajectory keeps the reserve)
    INDETERMINATE  otherwise

Spill (energy refused at capacity) for a trajectory equals sum(n_i d_i) - (E_end - E_0) and is
monotone in the net-power sequence, so [spill(E_low), spill(E_high)] bounds spill over all
allowed trajectories.

NOT modeled: recharge physics, nonlinear state of charge, temperature, switching transients,
power-delivery limits, time-varying reserve, correlated inputs. Bounds are assumptions.
"""
from fractions import Fraction
import hashlib
from pathlib import Path
from experiments.private_receipts.receipt import Invalid, canonical
from experiments.work_contracts import energy_analysis as energy

MODEL_ID = 'temporal-energy/v1'
RESULT_SCHEMA = 'temporal-energy-result/v1'
INPUT_SCHEMA = 'temporal-energy-input/v1'
UNITS = {'energy': 'mJ', 'power': 'mW', 'duration': 's'}
ASSUMPTIONS = ['piecewise_constant_power_bounds', 'independent_interval_bounds', 'powers_at_usable_energy_boundary',
               'saturation_at_capacity', 'constant_reserve', 'virtual_energy_below_reserve_for_diagnostics',
               'no_unmodeled_loads', 'no_recharge_physics']
MAX_SEGMENTS = 512
MAX_HORIZON_SECONDS = 10 ** 9
OUTCOMES = ('FEASIBLE', 'INFEASIBLE', 'INDETERMINATE')


def bundle_digest():
    return hashlib.sha256(b'metacoin/temporal-bundle/v1\0' + Path(__file__).read_bytes()).hexdigest()


def validate(data):
    canonical(data)
    energy.exact(data, ('schema', 'capacity', 'initial_low', 'initial_high', 'reserve', 'segments', 'units',
                        'assumptions', 'provenance', 'private_label'))
    if data['schema'] != INPUT_SCHEMA:
        raise Invalid('unsupported contract semantics or installed verifier')
    if data['units'] != UNITS or data['assumptions'] != ASSUMPTIONS:
        raise Invalid('unsupported units or assumptions')
    if data['provenance'] not in ('synthetic', 'declared', 'imported', 'measured_by_named_source'):
        raise Invalid('unsupported provenance')
    if type(data['private_label']) is not str or not 1 <= len(data['private_label']) <= 128:
        raise Invalid('invalid private label')
    cap = energy.integer(data['capacity'], 1)
    for name in ('initial_low', 'initial_high', 'reserve'):
        energy.integer(data[name], 0, cap)
    if data['initial_low'] > data['initial_high']:
        raise Invalid('reversed available bounds')
    segs = data['segments']
    if type(segs) is not list or not 1 <= len(segs) <= MAX_SEGMENTS:
        raise Invalid('segment limit exceeded')
    horizon = 0
    for row in segs:
        energy.exact(row, ('duration', 'harvest_low', 'harvest_high', 'load_low', 'load_high', 'leakage_low', 'leakage_high'))
        horizon += energy.integer(row['duration'], 1)
        for lo, hi in (('harvest_low', 'harvest_high'), ('load_low', 'load_high'), ('leakage_low', 'leakage_high')):
            energy.integer(row[lo])
            energy.integer(row[hi])
            if row[lo] > row[hi]:
                raise Invalid('reversed power bounds')
        # per-segment energy change must stay inside the interoperable integer range
        energy.integer(row['harvest_high'] * row['duration'])
        energy.integer((row['load_high'] + row['leakage_high']) * row['duration'])
    if horizon > MAX_HORIZON_SECONDS:
        raise Invalid('segment limit exceeded')
    return data


def _trajectory(initial, nets, durations, capacity, reserve):
    """Exact envelope walk. Returns per-boundary energies, min margin with its time, first
    reserve crossing (segment index, exact offset seconds as a fraction) and spill."""
    e = initial
    t = 0
    points = [{'t': 0, 'energy': e}]
    min_margin, min_t = e - reserve, 0
    first_violation = None if e >= reserve else {'segment': 0, 'offset_seconds': [0, 1], 't_start': 0}
    spill = 0
    for i, (n, d) in enumerate(zip(nets, durations)):
        raw = e + n * d
        end = min(capacity, raw)
        if raw > capacity:
            spill += raw - capacity
        if first_violation is None and end < reserve:
            # linear descent from e (>= reserve) to end (< reserve): exact crossing offset
            off = Fraction(e - reserve, -n)           # n < 0 here
            first_violation = {'segment': i, 't_start': t, 'offset_seconds': [off.numerator, off.denominator]}
        t += d
        e = end
        points.append({'t': t, 'energy': e})
        if e - reserve < min_margin:
            min_margin, min_t = e - reserve, t
    return {'points': points, 'min_margin': min_margin, 'min_margin_t': min_t, 'first_violation': first_violation,
            'spill': spill, 'final_energy': e}


def analyze(data):
    validate(data)
    cap, r = data['capacity'], data['reserve']
    durations = [s['duration'] for s in data['segments']]
    net_low = [s['harvest_low'] - s['load_high'] - s['leakage_high'] for s in data['segments']]
    net_high = [s['harvest_high'] - s['load_low'] - s['leakage_low'] for s in data['segments']]
    low = _trajectory(data['initial_low'], net_low, durations, cap, r)
    high = _trajectory(data['initial_high'], net_high, durations, cap, r)
    if low['min_margin'] >= 0:
        outcome, reason = 'FEASIBLE', 'pessimistic_envelope_keeps_reserve_throughout'
    elif high['min_margin'] < 0:
        outcome, reason = 'INFEASIBLE', 'optimistic_envelope_violates_reserve'
    else:
        outcome, reason = 'INDETERMINATE', 'envelopes_straddle_reserve'
    horizon = sum(durations)
    # Aggregate inequality for comparison: what a single-interval energy balance would say.
    total_harvest_high = sum(s['harvest_high'] * s['duration'] for s in data['segments'])
    total_load_low = sum((s['load_low'] + s['leakage_low']) * s['duration'] for s in data['segments'])
    aggregate_optimistic_ok = data['initial_high'] + total_harvest_high - total_load_low >= r
    # Which declared widths contribute to uncertainty up to the first pessimistic violation (pre-saturation widths).
    cutoff = low['first_violation']['segment'] if low['first_violation'] else len(durations) - 1
    contributions = [{'source': 'initial_energy', 'index': None, 'width': data['initial_high'] - data['initial_low']}]
    for i, s in enumerate(data['segments'][:cutoff + 1]):
        for name, lo, hi in (('harvest', 'harvest_low', 'harvest_high'), ('load', 'load_low', 'load_high'), ('leakage', 'leakage_low', 'leakage_high')):
            w = (s[hi] - s[lo]) * s['duration']
            if w:
                contributions.append({'source': name, 'index': i, 'width': w})
    ranked = sorted(contributions, key=lambda c: -c['width'])
    dominant = 'none' if not ranked or ranked[0]['width'] == 0 else (ranked[0]['source'] + ('' if ranked[0]['index'] is None else ':' + str(ranked[0]['index'])))
    return {'result_schema': RESULT_SCHEMA, 'model_id': MODEL_ID, 'outcome': outcome, 'reason': reason,
            'horizon_seconds': horizon, 'segments': len(durations), 'units': dict(UNITS), 'assumptions': list(ASSUMPTIONS),
            'reserve': r, 'capacity': cap,
            'envelope_low': low['points'] if len(durations) <= 64 else {'summarized': True, 'boundaries': len(low['points']), 'min': min(p['energy'] for p in low['points']), 'max': max(p['energy'] for p in low['points'])},
            'envelope_high': high['points'] if len(durations) <= 64 else {'summarized': True, 'boundaries': len(high['points']), 'min': min(p['energy'] for p in high['points']), 'max': max(p['energy'] for p in high['points'])},
            'min_reserve_margin_pessimistic': low['min_margin'], 'min_reserve_margin_pessimistic_t': low['min_margin_t'],
            'min_reserve_margin_optimistic': high['min_margin'], 'min_reserve_margin_optimistic_t': high['min_margin_t'],
            'first_uncertain_boundary': low['first_violation'], 'first_infeasible_boundary': high['first_violation'],
            'spill_bounds': [low['spill'], high['spill']], 'final_energy_bounds': [low['final_energy'], high['final_energy']],
            'aggregate_balance_optimistic_ok': aggregate_optimistic_ok,
            'aggregate_analysis_misses_early_deficit': bool(aggregate_optimistic_ok and outcome == 'INFEASIBLE'),
            'uncertainty_contributions': contributions, 'dominant_uncertainty_source': dominant,
            'provenance': data['provenance'], 'verifier_digest': bundle_digest(),
            'interpretation': 'conservative whole-horizon classification under declared independent interval bounds; '
                              'envelopes are attainable trajectories; not a measurement or a hardware guarantee'}


def refine(data, refinements):
    """What-if: narrower intervals (subsets of the declared ones) re-analyzed; the original result
    is returned beside the refined one. refinements: [{'segment': i|None, 'field': 'harvest'|'load'|'leakage'|'initial', 'low': int, 'high': int}]"""
    validate(data)
    if type(refinements) is not list or not 1 <= len(refinements) <= 64:
        raise Invalid('candidate limit exceeded')
    revised = canonical(data) and __import__('json').loads(canonical(data))
    removed_width = 0
    for ref in refinements:
        energy.exact(ref, ('segment', 'field', 'low', 'high'))
        energy.integer(ref['low']); energy.integer(ref['high'])
        if ref['low'] > ref['high']:
            raise Invalid('reversed power bounds')
        if ref['field'] == 'initial':
            if ref['segment'] is not None:
                raise Invalid('invalid candidate identifier')
            lo_key, hi_key, target, scale = 'initial_low', 'initial_high', revised, 1
        else:
            if ref['field'] not in ('harvest', 'load', 'leakage') or type(ref['segment']) is not int or not 0 <= ref['segment'] < len(data['segments']):
                raise Invalid('invalid candidate identifier')
            lo_key, hi_key = ref['field'] + '_low', ref['field'] + '_high'
            target, scale = revised['segments'][ref['segment']], data['segments'][ref['segment']]['duration']
        if not (target[lo_key] <= ref['low'] and ref['high'] <= target[hi_key]):
            raise Invalid('refinement is not a subset of the declared interval')
        removed_width += ((target[hi_key] - target[lo_key]) - (ref['high'] - ref['low'])) * scale
        target[lo_key], target[hi_key] = ref['low'], ref['high']
    original, refined = analyze(data), analyze(revised)
    return {'schema': 'temporal-refinement/v1', 'hypothetical': True, 'original_outcome': original['outcome'],
            'refined_outcome': refined['outcome'], 'resolves_uncertainty': original['outcome'] == 'INDETERMINATE' and refined['outcome'] != 'INDETERMINATE',
            'declared_width_removed_mJ': removed_width, 'refined_min_margin_pessimistic': refined['min_reserve_margin_pessimistic'],
            'original_min_margin_pessimistic': original['min_reserve_margin_pessimistic'],
            'note': 'a narrower assumed range is not evidence that the real range is narrower; not expected information gain'}
