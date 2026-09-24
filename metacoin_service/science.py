"""New exact decision functions on the declared energy model (integer mJ / mW / s).

Both are conditional on the declared interval model of energy_analysis: bounds
are assumptions, not calibrated observations. Neither is a battery-health
measurement or a hardware safety guarantee. Model ids and result schemas are
explicit; the bundle digest of this module is recorded on every job.
"""
import hashlib
from pathlib import Path
from experiments.private_receipts.receipt import Invalid, canonical
from experiments.work_contracts import energy_analysis as energy, explanation

SAFE_RUNTIME_MODEL = 'safe-runtime/v1'
SAFE_RUNTIME_SCHEMA = 'safe-runtime-result/v1'
COMPARISON_MODEL = 'plan-comparison/v1'
COMPARISON_SCHEMA = 'plan-comparison-result/v1'
SELECTION_MODEL = 'task-selection/v1'
SELECTION_SCHEMA = 'task-selection-result/v1'
MAX_OPTIONAL_TASKS = 12          # exhaustive search over 2^12 subsets is bounded and exact
MAX_CANDIDATES = 16
MAX_TOTAL_SEGMENTS = 512
RANK = {'INFEASIBLE': 0, 'INDETERMINATE': 1, 'FEASIBLE': 2}


def bundle_digest():
    here = Path(__file__).parent
    files = [('science.py', here / 'science.py'), ('energy_analysis.py', Path(energy.__file__)),
             ('explanation.py', Path(explanation.__file__))]
    values = [[name, hashlib.sha256(path.read_bytes()).hexdigest()] for name, path in files]
    return hashlib.sha256(b'metacoin/service-science-bundle/v1\0' + canonical(values)).hexdigest()


# ---- Feature H: exact safe-runtime calculator ---------------------------------
def validate_safe_runtime(data):
    canonical(data)
    energy.exact(data, ('available_low', 'available_high', 'reserve', 'fixed_segments', 'variable_power_low',
                        'variable_power_high', 'duration_cap', 'units', 'assumptions', 'provenance', 'private_label'))
    if data['units'] != energy.UNITS or data['assumptions'] != energy.ASSUMPTIONS:
        raise Invalid('unsupported units or assumptions')
    if data['provenance'] not in ('synthetic', 'declared_unverified'):
        raise Invalid('unsupported provenance')
    if type(data['private_label']) is not str or not 1 <= len(data['private_label']) <= 128:
        raise Invalid('invalid private label')
    for name in ('available_low', 'available_high', 'reserve', 'variable_power_low', 'variable_power_high'):
        energy.integer(data[name])
    energy.integer(data['duration_cap'], 1)
    if data['available_low'] > data['available_high']:
        raise Invalid('reversed available bounds')
    if data['variable_power_low'] > data['variable_power_high']:
        raise Invalid('reversed power bounds')
    if type(data['fixed_segments']) is not list or len(data['fixed_segments']) > 128:
        raise Invalid('segment limit exceeded')
    for row in data['fixed_segments']:
        energy.exact(row, ('duration', 'power_low', 'power_high'))
        energy.integer(row['duration'], 1)
        energy.integer(row['power_low'])
        energy.integer(row['power_high'])
        if row['power_low'] > row['power_high']:
            raise Invalid('reversed power bounds')


def safe_runtime(data):
    """Maximum additional integer duration of the variable segment that stays
    robustly FEASIBLE (worst case covered): B = available_low - reserve -
    sum(fixed_power_high * duration); D = floor(B / variable_power_high)."""
    validate_safe_runtime(data)
    fixed_high = 0
    for row in data['fixed_segments']:
        fixed_high = energy.integer(fixed_high + row['power_high'] * row['duration'])
    residual = data['available_low'] - data['reserve'] - fixed_high
    cap = data['duration_cap']
    p_high = data['variable_power_high']
    out = {'result_schema': SAFE_RUNTIME_SCHEMA, 'model_id': SAFE_RUNTIME_MODEL, 'base_model_id': energy.MODEL_ID,
           'units': {'energy': 'mJ', 'power': 'mW', 'duration': 's'}, 'residual_energy_worst_case': residual,
           'fixed_demand_high': fixed_high, 'duration_cap': cap, 'provenance': data['provenance'],
           'assumptions': list(energy.ASSUMPTIONS) + ['variable_segment_upper_power_bound_binding', 'fixed_reserve'],
           'conditional_on': 'declared-interval-model;not-a-measurement;not-a-hardware-guarantee'}
    if residual < 0:
        return dict(out, status='BASE_PLAN_INFEASIBLE', safe_duration=0, binding='base_plan_energy',
                    margin_at_duration=residual, maximality='not-applicable;base plan already violates worst case',
                    additional_usable_energy=-residual)
    if p_high == 0:
        return dict(out, status='CAPPED_MODEL_UNBOUNDED', safe_duration=cap, binding='application_cap',
                    margin_at_duration=residual,
                    maximality='cap-limited;the energy model does not constrain duration at zero upper power')
    uncapped = residual // p_high
    if uncapped >= cap:
        margin = residual - cap * p_high
        return dict(out, status='CAPPED', safe_duration=cap, binding='application_cap', margin_at_duration=margin,
                    uncapped_duration=uncapped, maximality='cap-limited;physical maximality not claimed')
    margin = residual - uncapped * p_high
    witness_margin = residual - (uncapped + 1) * p_high
    if not (0 <= margin < p_high and witness_margin < 0):
        raise Invalid('margin decomposition does not reconcile')
    return dict(out, status='ROBUSTLY_FEASIBLE', safe_duration=uncapped, binding='worst_case_energy',
                margin_at_duration=margin,
                maximality={'one_more_second_margin': witness_margin, 'violates_declared_bound': True})


# ---- Feature I: comparison of user-defined plans -------------------------------
def validate_comparison(data):
    canonical(data)
    energy.exact(data, ('candidates', 'objective', 'private_label'))
    if data['objective'] not in ('max_utility', 'none'):
        raise Invalid('unsupported objective')
    if type(data['private_label']) is not str or not 1 <= len(data['private_label']) <= 128:
        raise Invalid('invalid private label')
    cands = data['candidates']
    if type(cands) is not list or not 1 <= len(cands) <= MAX_CANDIDATES:
        raise Invalid('candidate limit exceeded')
    ids, total = set(), 0
    for cand in cands:
        energy.exact(cand, ('id', 'inputs', 'utility'))
        if type(cand['id']) is not str or not 1 <= len(cand['id']) <= 64 or cand['id'] in ids:
            raise Invalid('invalid candidate identifier')
        ids.add(cand['id'])
        if cand['utility'] is not None:
            energy.integer(cand['utility'])
        energy.validate(cand['inputs'])
        total += len(cand['inputs']['segments'])
    if total > MAX_TOTAL_SEGMENTS:
        raise Invalid('segment limit exceeded')


def compare_plans(data):
    """Per-candidate verdict, margins, duration and declared utility; deterministic
    selection of the highest utility among robustly feasible candidates with
    tie-breaking: lower worst-case demand, shorter duration, identifier order."""
    validate_comparison(data)
    rows = []
    for cand in data['candidates']:
        result = energy.analyze(cand['inputs'])
        rows.append({'id': cand['id'], 'outcome': result['outcome'], 'reason': result['reason'],
                     'required_low': result['required_low'], 'required_high': result['required_high'],
                     'worst_margin': result['worst_margin'], 'best_margin': result['best_margin'],
                     'duration': sum(s['duration'] for s in cand['inputs']['segments']),
                     'additional_usable_energy': result['additional_usable_energy'],
                     'utility': cand['utility'],
                     'dominant_uncertainty_source': explanation.explain(cand['inputs'])['dominant_uncertainty_source'],
                     'input_digest': hashlib.sha256(canonical(cand['inputs'])).hexdigest()})
    feasible = [r for r in rows if r['outcome'] == 'FEASIBLE']
    indeterminate = [r['id'] for r in rows if r['outcome'] == 'INDETERMINATE']
    infeasible = [r['id'] for r in rows if r['outcome'] == 'INFEASIBLE']
    selected, rationale = None, 'objective none: no selection requested'
    if data['objective'] == 'max_utility':
        pool = [r for r in feasible if r['utility'] is not None]
        if not feasible:
            rationale = 'no robustly feasible candidate; indeterminate candidates are not selected'
        elif not pool:
            rationale = 'feasible candidates exist but none declares a utility'
        else:
            best = sorted(pool, key=lambda r: (-r['utility'], r['required_high'], r['duration'], r['id']))[0]
            selected = best['id']
            rationale = ('highest declared utility among robustly feasible candidates; ties broken by lower '
                         'worst-case demand, shorter duration, identifier order')
    return {'result_schema': COMPARISON_SCHEMA, 'model_id': COMPARISON_MODEL, 'base_model_id': energy.MODEL_ID,
            'units': dict(energy.UNITS), 'candidates': rows, 'feasible_ids': [r['id'] for r in feasible],
            'indeterminate_ids': indeterminate, 'infeasible_ids': infeasible, 'objective': data['objective'],
            'selected_id': selected, 'selection_rationale': rationale,
            'selected_input_digest': next((r['input_digest'] for r in rows if r['id'] == selected), None),
            'scope': 'selection among submitted candidates only; no global optimization claimed',
            'assumptions': list(energy.ASSUMPTIONS)}


# ---- backlog 3: bounded task-selection optimizer -------------------------------
def validate_selection(data):
    canonical(data)
    energy.exact(data, ('available_low', 'available_high', 'reserve', 'fixed_segments', 'optional_tasks', 'duration_cap',
                        'units', 'assumptions', 'provenance', 'private_label'))
    if data['units'] != energy.UNITS or data['assumptions'] != energy.ASSUMPTIONS:
        raise Invalid('unsupported units or assumptions')
    if data['provenance'] not in ('synthetic', 'declared_unverified'):
        raise Invalid('unsupported provenance')
    if type(data['private_label']) is not str or not 1 <= len(data['private_label']) <= 128:
        raise Invalid('invalid private label')
    for name in ('available_low', 'available_high', 'reserve'):
        energy.integer(data[name])
    energy.integer(data['duration_cap'], 1)
    if data['available_low'] > data['available_high']:
        raise Invalid('reversed available bounds')
    if type(data['fixed_segments']) is not list or len(data['fixed_segments']) > 128:
        raise Invalid('segment limit exceeded')
    for row in data['fixed_segments']:
        energy.exact(row, ('duration', 'power_low', 'power_high'))
        energy.integer(row['duration'], 1)
        energy.integer(row['power_low'])
        energy.integer(row['power_high'])
        if row['power_low'] > row['power_high']:
            raise Invalid('reversed power bounds')
    tasks = data['optional_tasks']
    if type(tasks) is not list or not 1 <= len(tasks) <= MAX_OPTIONAL_TASKS:
        raise Invalid('candidate limit exceeded')
    ids = set()
    for task in tasks:
        energy.exact(task, ('id', 'duration', 'power_high', 'value'))
        if type(task['id']) is not str or not 1 <= len(task['id']) <= 64 or task['id'] in ids:
            raise Invalid('invalid candidate identifier')
        ids.add(task['id'])
        energy.integer(task['duration'], 1)
        energy.integer(task['power_high'])
        energy.integer(task['value'])


def select_tasks(data):
    """Exact selection of optional tasks: maximize the integer sum of declared values
    subject to worst-case energy (sum power_high * duration) <= residual worst-case energy
    and total duration <= duration_cap. Exhaustive over all subsets (n <= 12), so the
    optimum is established for the submitted tasks and this objective only. Ties:
    lower worst-case energy, shorter duration, then lexicographic identifier set."""
    validate_selection(data)
    fixed = 0
    for row in data['fixed_segments']:
        fixed = energy.integer(fixed + row['power_high'] * row['duration'])
    residual = data['available_low'] - data['reserve'] - fixed
    tasks = data['optional_tasks']
    base = {'result_schema': SELECTION_SCHEMA, 'model_id': SELECTION_MODEL, 'base_model_id': energy.MODEL_ID,
            'units': dict(energy.UNITS), 'residual_energy_worst_case': residual, 'duration_cap': data['duration_cap'],
            'objective': 'maximize sum of declared integer values; constraints: worst-case energy and total duration',
            'search': 'exhaustive over ' + str(2 ** len(tasks)) + ' subsets; optimal for the submitted tasks and this objective only',
            'assumptions': list(energy.ASSUMPTIONS) + ['task_upper_power_bound_binding', 'tasks_independent_and_sequential', 'fixed_reserve'],
            'provenance': data['provenance'], 'conditional_on': 'declared-interval-model;not-a-measurement;not-a-hardware-guarantee'}
    if residual < 0:
        return dict(base, status='BASE_PLAN_INFEASIBLE', selected_ids=[], total_value=0, energy_used=0, duration_used=0,
                    energy_margin=residual, considered=0)
    cost = [(t['power_high'] * t['duration'], t['duration'], t['value'], t['id']) for t in tasks]
    best = None
    for mask in range(2 ** len(tasks)):
        e = d = v = 0
        chosen = []
        for i, (ce, cd, cv, cid) in enumerate(cost):
            if mask >> i & 1:
                e += ce; d += cd; v += cv; chosen.append(cid)
        if e > residual or d > data['duration_cap']:
            continue
        key = (-v, e, d, sorted(chosen))
        if best is None or key < best[0]:
            best = (key, chosen, e, d, v)
    _, chosen, e, d, v = best
    return dict(base, status='OPTIMAL_FOR_SUBMITTED_TASKS', selected_ids=sorted(chosen), total_value=v, energy_used=e,
                duration_used=d, energy_margin=residual - e, duration_margin=data['duration_cap'] - d, considered=2 ** len(tasks))
