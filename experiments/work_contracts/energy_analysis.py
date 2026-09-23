"""Integer interval energy accounting; no measurement or flight-safety claim."""
from experiments.private_receipts.receipt import Invalid, canonical

MODEL_ID = 'outage-energy-bounds/v0'
RESULT_SCHEMA = 'energy-analysis/v0'
LIMIT = 2**53 - 1
UNITS = {'energy': 'mJ', 'power': 'mW', 'duration': 's'}
ASSUMPTIONS = ['no_recharge', 'usable_energy_at_load_boundary',
               'piecewise_constant_power_bounds', 'no_unmodeled_loads']
OUTCOMES = ('FEASIBLE', 'INFEASIBLE', 'INDETERMINATE')


def exact(obj, keys):
    if type(obj) is not dict or set(obj) != set(keys):
        raise Invalid('unexpected fields')


def integer(value, minimum=0, maximum=LIMIT):
    if type(value) is not int or not minimum <= value <= maximum:
        raise Invalid('integer outside declared domain')
    return value


def validate(data):
    canonical(data)
    exact(data, ('available_low', 'available_high', 'reserve', 'segments',
                 'units', 'assumptions', 'provenance', 'private_label'))
    if data['units'] != UNITS or data['assumptions'] != ASSUMPTIONS:
        raise Invalid('unsupported units or assumptions')
    if data['provenance'] not in ('synthetic', 'declared_unverified'):
        raise Invalid('unsupported provenance')
    if type(data['private_label']) is not str or not 1 <= len(data['private_label']) <= 128:
        raise Invalid('invalid private label')
    for name in ('available_low', 'available_high', 'reserve'):
        integer(data[name])
    if data['available_low'] > data['available_high']:
        raise Invalid('reversed available bounds')
    if type(data['segments']) is not list or not 1 <= len(data['segments']) <= 128:
        raise Invalid('segment limit exceeded')
    for row in data['segments']:
        exact(row, ('duration', 'power_low', 'power_high'))
        integer(row['duration'], 1)
        integer(row['power_low'])
        integer(row['power_high'])
        if row['power_low'] > row['power_high']:
            raise Invalid('reversed power bounds')


def analyze(data):
    validate(data)
    low = high = data['reserve']
    for row in data['segments']:
        low = integer(low + row['power_low'] * row['duration'])
        high = integer(high + row['power_high'] * row['duration'])
    if data['available_low'] >= high:
        outcome, reason = 'FEASIBLE', 'worst_case_covered'
    elif data['available_high'] < low:
        outcome, reason = 'INFEASIBLE', 'best_case_not_covered'
    else:
        outcome, reason = 'INDETERMINATE', 'bounds_overlap'
    return {'outcome': outcome, 'reason': reason, 'required_low': low,
            'required_high': high, 'worst_margin': data['available_low'] - high,
            'best_margin': data['available_high'] - low,
            'additional_usable_energy': max(0, high - data['available_low']),
            'provenance': data['provenance'], 'model_id': MODEL_ID,
            'result_schema': RESULT_SCHEMA, 'units': dict(UNITS),
            'assumptions': list(ASSUMPTIONS)}
