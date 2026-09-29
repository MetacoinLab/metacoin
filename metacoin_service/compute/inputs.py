"""Bounded typed inputs for the three compute services: strict validation, exact normalization,
deterministic work-unit counts, and int64 overflow bounds computed on the host before any device
conversion. Floating-point parameters (heat) enter as bounded decimal strings and are converted once
to float64 with the conversion recorded; the stability test is done exactly on the rationals."""
import decimal
import itertools
import math
import re
from fractions import Fraction
from experiments.private_receipts.receipt import Invalid, canonical
from experiments.work_contracts import energy_analysis as energy
from .. import temporal
from ..errors import ServiceError


class ComputeInvalid(ServiceError):
    """A validation refusal with a compute-specific message (HTTP 422); str() is the message."""

    def __init__(self, message):
        super().__init__('VALIDATION', {'code': 'compute_input', 'reason': message})
        self.message = message

    def __str__(self):
        return self.message

INT64_SAFE = 2 ** 62                       # every intermediate must stay strictly below this magnitude
DEVICE_POLICIES = ('cpu', 'gpu', 'auto')
SCALE_FIELDS = ('harvest_scale_percent', 'load_scale_percent', 'leakage_scale_percent')
SCALAR_FIELDS = ('reserve', 'capacity', 'initial_low', 'initial_high')

TEMPORAL_BATCH_SCHEMA = 'temporal-batch-input/v1'
MONTE_CARLO_SCHEMA = 'monte-carlo-reliability-input/v1'
HEAT_SCHEMA = 'heat-diffusion-input/v1'

BATCH_LIMITS = {'max_scenarios_listed': 4096, 'max_scenarios_grid': 200_000, 'max_axes': 3, 'max_values_per_axis': 512, 'chunk_scenarios': 8192}
MC_LIMITS = {'max_samples': 2_000_000, 'min_samples': 1, 'max_distribution_values': 64, 'max_parameters': 6, 'chunk_samples': 65_536}
HEAT_LIMITS = {'min_n': 3, 'max_n': 1024, 'max_cells': 1024 * 1024, 'max_steps': 1_000_000, 'max_cell_steps': 20_000_000_000, 'max_snapshots': 8,
               'max_explicit_cells': 4096, 'chunk_cell_steps': 400_000_000}
DECIMAL_RE = re.compile(r'^-?[0-9]{1,12}(\.[0-9]{1,12})?$')


# ---- shared helpers ------------------------------------------------------------------------------
def scale_low(value, percent):
    return (value * percent) // 100            # floor, exact integers


def scale_high(value, percent):
    return -((-value * percent) // 100)        # ceil, exact integers


def _policy(data):
    if data.get('device_policy', 'auto') not in DEVICE_POLICIES:
        raise ComputeInvalid('unsupported device policy')
    return data.get('device_policy', 'auto')


def _label(data):
    if type(data.get('private_label')) is not str or not 1 <= len(data['private_label']) <= 128:
        raise Invalid('invalid private label')


# ---- temporal batch -------------------------------------------------------------------------------
def apply_variation(base, var):
    """Exact application of a variation (scalars replace; percent scales floor the low and ceil the high bound)."""
    out = {k: (dict(v) if type(v) is dict else v) for k, v in base.items()}
    out['segments'] = [dict(s) for s in base['segments']]
    for k in SCALAR_FIELDS:
        if k in var:
            out[k] = var[k]
    for field, (lo, hi) in (('harvest_scale_percent', ('harvest_low', 'harvest_high')), ('load_scale_percent', ('load_low', 'load_high')), ('leakage_scale_percent', ('leakage_low', 'leakage_high'))):
        if field in var:
            for s in out['segments']:
                s[lo], s[hi] = scale_low(s[lo], var[field]), scale_high(s[hi], var[field])
    return out


def _validate_variation(var, base):
    if type(var) is not dict or not set(var) <= set(SCALAR_FIELDS) | set(SCALE_FIELDS) | {'id'}:
        raise Invalid('unexpected fields')
    for k in SCALAR_FIELDS:
        if k in var:
            energy.integer(var[k], 0)
    for k in SCALE_FIELDS:
        if k in var:
            energy.integer(var[k], 0, 10_000)
    if 'id' in var and (type(var['id']) is not str or not 1 <= len(var['id']) <= 64):
        raise Invalid('invalid candidate identifier')
    cap = var.get('capacity', base['capacity'])
    for k in ('initial_low', 'initial_high', 'reserve'):
        if var.get(k, base[k]) > cap or var.get(k, base[k]) < 0:
            raise Invalid('integer outside declared domain')
    if var.get('initial_low', base['initial_low']) > var.get('initial_high', base['initial_high']):
        raise Invalid('reversed available bounds')


def _expand_grid(grid):
    if type(grid) is not list or not 1 <= len(grid) <= BATCH_LIMITS['max_axes']:
        raise ComputeInvalid('axis count outside declared domain')
    axes, seen = [], set()
    for ax in grid:
        if type(ax) is not dict or ax.get('path') not in SCALAR_FIELDS + SCALE_FIELDS or ax['path'] in seen:
            raise ComputeInvalid('invalid axis')
        seen.add(ax['path'])
        if set(ax) == {'path', 'values'}:
            values = ax['values']
            if type(values) is not list or not 1 <= len(values) <= BATCH_LIMITS['max_values_per_axis'] or not all(type(v) is int and type(v) is not bool for v in values) or len(set(values)) != len(values):
                raise ComputeInvalid('invalid axis values')
        elif set(ax) == {'path', 'start', 'stop', 'step'}:
            start, stop, step = ax['start'], ax['stop'], ax['step']
            if not all(type(v) is int and type(v) is not bool for v in (start, stop, step)) or step <= 0 or stop < start or (stop - start) // step + 1 > BATCH_LIMITS['max_values_per_axis']:
                raise ComputeInvalid('invalid axis range')
            values = list(range(start, stop + 1, step))
        else:
            raise ComputeInvalid('invalid axis')
        axes.append((ax['path'], values))
    total = 1
    for _, values in axes:
        total *= len(values)
    if total > BATCH_LIMITS['max_scenarios_grid']:
        raise ComputeInvalid('scenario count outside declared domain')
    return axes, total


def validate_temporal_batch(data):
    canonical(data)
    energy.exact(data, ('schema', 'base', 'scenarios', 'grid', 'device_policy', 'verification', 'private_label'))
    if data['schema'] != TEMPORAL_BATCH_SCHEMA:
        raise Invalid('unsupported contract semantics or installed verifier')
    _label(data); _policy(data)
    if data['verification'] not in ('exact_all', 'exact_sampled', 'auto'):
        raise ComputeInvalid('unsupported verification mode')
    base = temporal.validate(data['base'])
    if (data['scenarios'] is None) == (data['grid'] is None):
        raise ComputeInvalid('exactly one of scenarios or grid')
    if data['scenarios'] is not None:
        if type(data['scenarios']) is not list or not 1 <= len(data['scenarios']) <= BATCH_LIMITS['max_scenarios_listed']:
            raise ComputeInvalid('scenario count outside declared domain')
        ids = set()
        for var in data['scenarios']:
            _validate_variation(var, base)
            if 'id' in var:
                if var['id'] in ids:
                    raise ComputeInvalid('duplicate candidate identifier')
                ids.add(var['id'])
        total = len(data['scenarios'])
    else:
        axes, total = _expand_grid(data['grid'])
        # every grid point must be a valid variation
        for combo in itertools.product(*[values for _, values in axes]):
            var = dict(zip([p for p, _ in axes], combo))
            _validate_variation(var, base)
            if total > 4096:
                break                          # bounded check for very large grids: extremes are checked below
        if total > 4096:
            for extreme in itertools.product(*[(min(values), max(values)) for _, values in axes]):
                _validate_variation(dict(zip([p for p, _ in axes], extreme)), base)
    overflow_bound(data)
    return data


def batch_total(data):
    if data['scenarios'] is not None:
        return len(data['scenarios'])
    return _expand_grid(data['grid'])[1]


def scenario(data, index):
    """Deterministic scenario materialization by index (grid: itertools.product order, last axis fastest)."""
    if data['scenarios'] is not None:
        var = data['scenarios'][index]
        return var.get('id', 'scenario-%d' % index), apply_variation(data['base'], var), var
    axes, total = _expand_grid(data['grid'])
    if not 0 <= index < total:
        raise Invalid('invalid candidate identifier')
    var, rem = {}, index
    for path, values in reversed(axes):
        var[path] = values[rem % len(values)]
        rem //= len(values)
    return 'grid-%d' % index, apply_variation(data['base'], var), var


def overflow_bound(data):
    """Conservative host-side bound on every int64 intermediate for any scenario of the batch."""
    base = data['base']
    max_scale = {f: 100 for f in SCALE_FIELDS}
    max_cap = base['capacity']
    if data['scenarios'] is not None:
        for var in data['scenarios']:
            for f in SCALE_FIELDS:
                max_scale[f] = max(max_scale[f], var.get(f, 100))
            max_cap = max(max_cap, var.get('capacity', 0))
    else:
        for path, values in _expand_grid(data['grid'])[0]:
            if path in SCALE_FIELDS:
                max_scale[path] = max(max_scale[path], max(values))
            if path == 'capacity':
                max_cap = max(max_cap, max(values))
    swing = 0
    for s in base['segments']:
        h = scale_high(abs(s['harvest_high']) + abs(s['harvest_low']), max_scale['harvest_scale_percent'])
        l = scale_high(abs(s['load_high']) + abs(s['load_low']), max_scale['load_scale_percent'])
        k = scale_high(abs(s['leakage_high']) + abs(s['leakage_low']), max_scale['leakage_scale_percent'])
        per_second = h + l + k
        if per_second * s['duration'] >= INT64_SAFE:
            raise Invalid('integer outside declared domain')
        swing += per_second * s['duration']
    bound = max_cap + swing                    # |energy|, |spill|, |margin| never exceed this
    if bound >= INT64_SAFE or bound * 2 >= INT64_SAFE:
        raise Invalid('integer outside declared domain')
    return bound


# ---- Monte Carlo ---------------------------------------------------------------------------------
MC_PARAMS = ('initial_energy', 'reserve', 'harvest_scale_percent', 'load_scale_percent', 'leakage_scale_percent')


def validate_distribution(dist):
    if type(dist) is not dict:
        raise ComputeInvalid('invalid distribution')
    if dist.get('type') == 'finite':
        energy.exact(dist, ('type', 'values', 'weights'))
        v, w = dist['values'], dist['weights']
        if type(v) is not list or type(w) is not list or not 1 <= len(v) <= MC_LIMITS['max_distribution_values'] or len(v) != len(w):
            raise ComputeInvalid('invalid distribution')
        if not all(type(x) is int and type(x) is not bool for x in v) or len(set(v)) != len(v) or not all(type(x) is int and type(x) is not bool and 1 <= x <= 10 ** 6 for x in w):
            raise ComputeInvalid('invalid distribution')
        return {'type': 'finite', 'values': list(v), 'weights': list(w), 'total_weight': sum(w)}
    if dist.get('type') == 'uniform_int':
        energy.exact(dist, ('type', 'low', 'high'))
        energy.integer(dist['low'], 0); energy.integer(dist['high'], 0)
        if dist['low'] > dist['high']:
            raise Invalid('reversed power bounds')
        return {'type': 'uniform_int', 'low': dist['low'], 'high': dist['high'], 'count': dist['high'] - dist['low'] + 1}
    raise ComputeInvalid('unsupported distribution')


def validate_monte_carlo(data):
    canonical(data)
    energy.exact(data, ('schema', 'base', 'distributions', 'samples', 'seed', 'confidence_percent', 'event', 'device_policy', 'private_label'))
    if data['schema'] != MONTE_CARLO_SCHEMA:
        raise Invalid('unsupported contract semantics or installed verifier')
    _label(data); _policy(data)
    base = temporal.validate(data['base'])
    if base['initial_low'] != base['initial_high'] or any(s[lo] != s[hi] for s in base['segments'] for lo, hi in (('harvest_low', 'harvest_high'), ('load_low', 'load_high'), ('leakage_low', 'leakage_high'))):
        raise ComputeInvalid('Monte Carlo needs a point-valued base model; declare variability through distributions')
    energy.integer(data['samples'], MC_LIMITS['min_samples'], MC_LIMITS['max_samples'])
    energy.integer(data['seed'], 0, 2 ** 62)
    if data['confidence_percent'] not in (90, 95, 99) or data['event'] != 'reserve_maintained':
        raise ComputeInvalid('unsupported confidence level or event')
    d = data['distributions']
    if type(d) is not dict or not 1 <= len(d) <= MC_LIMITS['max_parameters'] or not set(d) <= set(MC_PARAMS):
        raise ComputeInvalid('unsupported distribution parameters')
    normalized = {}
    for p in MC_PARAMS:                        # fixed parameter order defines the draw order inside a sample
        if p in d:
            normalized[p] = validate_distribution(d[p])
            values = normalized[p]['values'] if normalized[p]['type'] == 'finite' else (normalized[p]['low'], normalized[p]['high'])
            if p in ('initial_energy', 'reserve') and (min(values) < 0 or max(values) > base['capacity']):
                raise Invalid('integer outside declared domain')
            if p in SCALE_FIELDS and max(values) > 10_000:
                raise Invalid('integer outside declared domain')
    # overflow bound as a batch of extremes
    extremes = {'schema': TEMPORAL_BATCH_SCHEMA, 'base': base, 'grid': None, 'device_policy': 'cpu', 'verification': 'auto', 'private_label': 'x',
                'scenarios': [{f: (max(normalized[f]['values']) if normalized[f]['type'] == 'finite' else normalized[f]['high']) for f in normalized if f in SCALE_FIELDS}]}
    overflow_bound(extremes)
    return normalized


def mc_draws_per_sample(normalized):
    return len(normalized)


# ---- heat diffusion --------------------------------------------------------------------------------
def decimal_value(text):
    if type(text) is not str or not DECIMAL_RE.match(text):
        raise ComputeInvalid('decimal parameter must be a bounded decimal string')
    return Fraction(decimal.Decimal(text))


INITIAL_TYPES = ('uniform', 'gaussian', 'sine_mode', 'hot_rectangle', 'field')


def validate_heat(data):
    canonical(data)
    energy.exact(data, ('schema', 'nx', 'ny', 'dx', 'dy', 'dt', 'alpha', 'steps', 'boundary', 'initial', 'snapshots', 'units', 'device_policy', 'precision', 'private_label'))
    if data['schema'] != HEAT_SCHEMA:
        raise Invalid('unsupported contract semantics or installed verifier')
    _label(data); _policy(data)
    nx, ny = energy.integer(data['nx'], HEAT_LIMITS['min_n'], HEAT_LIMITS['max_n']), energy.integer(data['ny'], HEAT_LIMITS['min_n'], HEAT_LIMITS['max_n'])
    if nx * ny > HEAT_LIMITS['max_cells']:
        raise ComputeInvalid('grid outside declared domain')
    steps = energy.integer(data['steps'], 1, HEAT_LIMITS['max_steps'])
    if nx * ny * steps > HEAT_LIMITS['max_cell_steps']:
        raise ComputeInvalid('cell-steps outside declared domain')
    dx, dy, dt, alpha = (decimal_value(data[k]) for k in ('dx', 'dy', 'dt', 'alpha'))
    if dx <= 0 or dy <= 0 or dt <= 0 or alpha < 0:
        raise ComputeInvalid('grid spacing and timestep must be positive; diffusivity nonnegative')
    if data['precision'] != 'float64':
        raise ComputeInvalid('unsupported precision')
    rx, ry = alpha * dt / (dx * dx), alpha * dt / (dy * dy)
    if rx + ry > Fraction(1, 2):
        max_dt = Fraction(1, 2) / (alpha * (1 / (dx * dx) + 1 / (dy * dy))) if alpha > 0 else None
        raise ComputeInvalid('unstable timestep for the explicit scheme: r_x + r_y = %s > 1/2 (maximum dt = %s)' % (rx + ry, max_dt))
    b = data['boundary']
    if type(b) is not dict or b.get('type') != 'dirichlet' or set(b) != {'type', 'values'} or type(b['values']) is not dict or set(b['values']) != {'left', 'right', 'top', 'bottom'}:
        raise ComputeInvalid('unsupported boundary')
    for side in ('left', 'right', 'top', 'bottom'):
        decimal_value(b['values'][side])
    init = data['initial']
    if type(init) is not dict or init.get('type') not in INITIAL_TYPES:
        raise ComputeInvalid('unsupported initial condition')
    t = init['type']
    if t == 'uniform':
        energy.exact(init, ('type', 'value')); decimal_value(init['value'])
    elif t == 'gaussian':
        energy.exact(init, ('type', 'center_x', 'center_y', 'sigma', 'amplitude', 'background'))
        for k in ('center_x', 'center_y', 'sigma', 'amplitude', 'background'):
            decimal_value(init[k])
        if decimal_value(init['sigma']) <= 0:
            raise ComputeInvalid('sigma must be positive')
    elif t == 'sine_mode':
        energy.exact(init, ('type', 'm', 'n', 'amplitude'))
        energy.integer(init['m'], 1, 64); energy.integer(init['n'], 1, 64); decimal_value(init['amplitude'])
    elif t == 'hot_rectangle':
        energy.exact(init, ('type', 'x0', 'x1', 'y0', 'y1', 'inside', 'outside'))
        for k in ('x0', 'x1', 'y0', 'y1'):
            energy.integer(init[k], 0, 1024)
        if not (init['x0'] <= init['x1'] < nx and init['y0'] <= init['y1'] < ny):
            raise ComputeInvalid('rectangle outside the grid')
        decimal_value(init['inside']); decimal_value(init['outside'])
    else:
        energy.exact(init, ('type', 'rows'))
        rows = init['rows']
        if nx * ny > HEAT_LIMITS['max_explicit_cells'] or type(rows) is not list or len(rows) != ny or not all(type(r) is list and len(r) == nx for r in rows):
            raise ComputeInvalid('explicit field shape or size outside declared domain')
        for r in rows:
            for v in r:
                decimal_value(v)
    energy.integer(data['snapshots'], 0, HEAT_LIMITS['max_snapshots'])
    u = data['units']
    if type(u) is not dict or set(u) != {'field', 'length', 'time'} or not all(type(v) is str and 1 <= len(v) <= 16 for v in u.values()):
        raise ComputeInvalid('units must declare field, length and time')
    return {'nx': nx, 'ny': ny, 'steps': steps, 'dx': dx, 'dy': dy, 'dt': dt, 'alpha': alpha, 'rx': rx, 'ry': ry}


def heat_work_units(data):
    """Billable unit: one million interior cell updates, rounded up (declared in the manifest)."""
    return -(-(data['nx'] * data['ny'] * data['steps']) // 1_000_000)


CALIBRATION_SCHEMA = 'calibration-fit-input/v1'
CALIBRATION_LIMITS = {'max_rows': 5000, 'max_features': 16, 'max_ridge': '1e6', 'min_rows': 3}
SPLIT_METHODS = ('chronological', 'random', 'index')


def validate_calibration(data):
    """Fit request: dataset binding by id (rows are attached by the engine), feature/target names, intercept, ridge
    lambda as a decimal string, split policy, scaling and the interval level. Numeric policy is fixed by the manifest."""
    if type(data) is not dict or data.get('schema') != CALIBRATION_SCHEMA:
        raise ComputeInvalid('schema must be ' + CALIBRATION_SCHEMA)
    allowed = {'schema', 'dataset_id', 'features', 'target', 'intercept', 'ridge_lambda', 'split', 'scaling', 'interval_percent', 'device_policy', 'private_label', 'scope'}
    unknown = set(data) - allowed
    if unknown:
        raise ComputeInvalid('unknown fields are refused: ' + ','.join(sorted(unknown)))
    if type(data.get('dataset_id')) is not str or not data['dataset_id'].startswith('cd_') or len(data['dataset_id']) > 32:
        raise ComputeInvalid('dataset_id')
    feats = data.get('features')
    if type(feats) is not list or not 1 <= len(feats) <= CALIBRATION_LIMITS['max_features'] or len(set(feats)) != len(feats) \
            or not all(type(f) is str and re.match(r'^[a-z][a-z0-9_]{0,31}$', f) for f in feats):
        raise ComputeInvalid('features: 1..%d distinct snake_case names' % CALIBRATION_LIMITS['max_features'])
    if type(data.get('target')) is not str or not re.match(r'^[a-z][a-z0-9_]{0,31}$', data['target']) or data['target'] in feats:
        raise ComputeInvalid('target: a snake_case name distinct from the features')
    if type(data.get('intercept', True)) is not bool:
        raise ComputeInvalid('intercept: boolean')
    lam = data.get('ridge_lambda', '0')
    if type(lam) is not str or not DECIMAL_RE.match(lam) or Fraction(lam) < 0 or Fraction(lam) > Fraction(CALIBRATION_LIMITS['max_ridge']):
        raise ComputeInvalid('ridge_lambda: non-negative decimal string up to %s' % CALIBRATION_LIMITS['max_ridge'])
    split = data.get('split', {'method': 'chronological', 'train_fraction_percent': 80})
    if type(split) is not dict or split.get('method') not in SPLIT_METHODS:
        raise ComputeInvalid('split.method: ' + '|'.join(SPLIT_METHODS))
    if split['method'] == 'index':
        for key in ('train', 'eval'):
            v = split.get(key)
            if type(v) is not list or not v or not all(type(i) is int and i >= 0 for i in v) or len(set(v)) != len(v):
                raise ComputeInvalid('split.%s: distinct non-negative row indexes' % key)
        if set(split['train']) & set(split['eval']):
            raise ComputeInvalid('split: train and eval overlap')
    else:
        pct = split.get('train_fraction_percent', 80)
        if type(pct) is not int or not 50 <= pct <= 95:
            raise ComputeInvalid('split.train_fraction_percent: 50..95')
        if split['method'] == 'random' and (type(split.get('seed', 0)) is not int or split.get('seed', 0) < 0):
            raise ComputeInvalid('split.seed')
    if data.get('scaling', 'standardize') not in ('standardize', 'none'):
        raise ComputeInvalid('scaling: standardize | none')
    lvl = data.get('interval_percent', 90)
    if type(lvl) is not int or not 50 <= lvl <= 99:
        raise ComputeInvalid('interval_percent: 50..99')
    if data.get('device_policy', 'cpu') != 'cpu':
        raise ComputeInvalid('calibration runs on cpu only')
    if data.get('scope') is not None and (type(data['scope']) is not dict or not set(data['scope']) <= {'task_kind', 'backend'} or not all(type(v) is str and len(v) <= 40 for v in data['scope'].values())):
        raise ComputeInvalid('scope: {task_kind, backend}')
    return data


RESOURCE_PLAN_SCHEMA = 'robust-resource-plan-input/v1'


def validate_resource_plan(data):
    from . import resource_plan as rp
    try:
        rp.validate(data)
    except rp.Invalid as exc:
        raise ComputeInvalid(str(exc))
    if data.get('device_policy', 'cpu') != 'cpu':
        raise ComputeInvalid('resource planning runs on cpu only (HiGHS)')
    return data


RESOURCE_PLAN_LIMITS = None   # filled from compute.resource_plan.LIMITS at import of manifests


VALIDATORS = {'temporal_batch': validate_temporal_batch, 'monte_carlo_reliability': validate_monte_carlo, 'heat_diffusion': validate_heat, 'calibration_fit': validate_calibration, 'resource_plan': validate_resource_plan}


def work_units(kind, data):
    if kind == 'temporal_batch':
        return batch_total(data)
    if kind == 'monte_carlo_reliability':
        return data['samples']
    if kind == 'calibration_fit':
        return 1
    if kind == 'resource_plan':
        from . import resource_plan as rp
        return rp.work_units(data)
    return heat_work_units(data)
