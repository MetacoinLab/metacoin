"""Audit-only explanation of where the energy-margin uncertainty comes from.

Exact integer decomposition of the represented interval width for the
rectangular bounds model in energy_analysis. It is NOT an expected-information
gain, a probability of success, a proof that all endpoints can jointly occur,
or evidence that narrowing a bound is affordable. Correlated uncertainties and
model error can change how the interval envelope should be read.

  margin_low   = available_low  - required_high
  margin_high  = available_high - required_low
  margin_width = margin_high - margin_low
               = (available_high - available_low)
                 + sum((power_high[i] - power_low[i]) * duration[i])

The reserve is fixed in this model version and contributes zero width. If a
later model makes it uncertain, its term must be derived and versioned here.
"""
from experiments.private_receipts.receipt import Invalid, canonical
from . import energy_analysis as energy

EXPLANATION_SCHEMA = 'margin-decomposition/v1'
COUNTERFACTUAL_SCHEMA = 'energy-counterfactual/v1'
INTERPRETATION = ('exact-interval-width-decomposition;not-probability;'
                  'not-joint-attainability;not-measurement-cost;reserve-fixed')


def explain(data):
    """Decompose the margin interval width by declared uncertainty source."""
    result = energy.analyze(data)
    margin_low, margin_high = result['worst_margin'], result['best_margin']
    width = margin_high - margin_low
    contributions = [{'source': 'available_energy', 'index': None,
                      'width': data['available_high'] - data['available_low']}]
    for index, row in enumerate(data['segments']):
        contributions.append({'source': 'segment', 'index': index,
                              'width': energy.integer((row['power_high'] - row['power_low']) * row['duration'])})
    # Internal consistency: the decomposition must reconcile exactly. A
    # mismatch is a defect in this module, never a scientific finding.
    if sum(part['width'] for part in contributions) != width or width < 0:
        raise Invalid('margin decomposition does not reconcile')
    # Deterministic ranking: larger width first; ties keep declaration order
    # (available energy, then segments by index), so equal inputs rank equally.
    ranked = sorted(contributions, key=lambda part: -part['width'])
    labels = [label(part) for part in ranked]
    dominant = 'none' if width == 0 else labels[0]
    out = {'explanation_schema': EXPLANATION_SCHEMA, 'model_id': energy.MODEL_ID,
           'units': {'energy': energy.UNITS['energy']}, 'outcome': result['outcome'],
           'margin_low': margin_low, 'margin_high': margin_high, 'margin_width': width,
           'reserve_width': 0, 'contributions': contributions, 'ranked_sources': labels,
           'dominant_uncertainty_source': dominant, 'interpretation': INTERPRETATION}
    canonical(out)
    return out


def label(part):
    return part['source'] if part['index'] is None else part['source'] + ':' + str(part['index'])


def conditional_outcome(data, added_usable_energy):
    """Hypothetical: shift both usable-energy bounds up by a proposed amount.

    The original input and outcome are preserved and returned beside the
    conditional one. This is a mathematical intervention on the declared
    bounds, not a statement that such energy is physically available.
    """
    energy.validate(data)
    energy.integer(added_usable_energy, 0)
    original = energy.analyze(data)
    shifted = dict(data, available_low=energy.integer(data['available_low'] + added_usable_energy),
                   available_high=energy.integer(data['available_high'] + added_usable_energy))
    conditional = energy.analyze(shifted)
    return {'counterfactual_schema': COUNTERFACTUAL_SCHEMA, 'hypothetical': True,
            'model_id': energy.MODEL_ID, 'units': {'energy': energy.UNITS['energy']},
            'added_usable_energy': added_usable_energy,
            'original_outcome': original['outcome'], 'conditional_outcome': conditional['outcome'],
            'note': 'mathematical-intervention-on-declared-bounds;not-an-available-hardware-modification;'
                    'no-action-or-spend-follows'}


def threshold_check(data):
    """Confirm additional_usable_energy is the least shift that yields FEASIBLE."""
    need = energy.analyze(data)['additional_usable_energy']
    at_threshold = conditional_outcome(data, need)['conditional_outcome'] == 'FEASIBLE'
    below = need == 0 or conditional_outcome(data, need - 1)['conditional_outcome'] != 'FEASIBLE'
    return {'additional_usable_energy': need, 'feasible_at_threshold': at_threshold,
            'not_feasible_one_below': below, 'threshold_verified': at_threshold and below}
