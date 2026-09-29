"""Robust resource planning: `robust-resource-plan/v1` (Order 07 Group D).

EXECUTABLE MODEL SPECIFICATION (integers only: mJ, mW, s; slots)

Horizon: T slots of `slot_seconds` seconds each, t = 0..T-1; boundaries b = 0..T.
Storage: usable energy E_b in [0, capacity] (mJ). E_0 = initial_low (the conservative start). Reserve r (mJ) must hold at
EVERY boundary: E_b >= r for b = 0..T. Supply and consumption both accrue over a slot and are evaluated at its end
boundary; an intra-slot dip below r is bounded by the slot's consumption and is reported as a caveat, not modelled.
Per slot t: supply interval [supply_low_t, supply_high_t] (mW), baseline load interval [base_low_t, base_high_t] (mW).
Conservative recurrence (minimum declared supply, maximum declared consumption; interval inputs treated as independent
bounds, correlations ignored conservatively):
    net_t   = supply_low_t - base_high_t - sum_i power_high_i * a_{i,t}          (mW)
    E_{t+1} = min(capacity, E_t + net_t * slot_seconds)                          (mJ; spill = the amount refused at capacity)
Tasks i: id, mandatory, utility (integer >= 0), duration d_i (slots >= 1), allowed starts s in [earliest_i, latest_i],
power_high_i (mW), resource demand q_{i,k} (integer per resource k with per-slot capacity c_k), dependencies (j must end
before i starts: s_j + d_j <= s_i, and j must be selected when i is), exclusive_with (never active in the same slot).
Decision: x_{i,s} in {0,1} (task i starts at slot s); optional tasks sum_s x_{i,s} <= 1, mandatory = 1.
Active: a_{i,t} = sum_{s : s <= t < s + d_i} x_{i,s}.
Constraints: resources sum_i q_{i,k} a_{i,t} <= c_k; exclusivity a_{i,t} + a_{j,t} <= 1; dependencies as above; energy
recurrence linearized with a spill variable sp_t >= 0: E_{t+1} = E_t + net_t*slot_seconds - sp_t, 0 <= E_{t+1} <= capacity,
E_{t+1} >= r (a solver may spill more than saturation requires; that only lowers E, so any solver-feasible schedule is
feasible under the exact min() semantics, which the independent simulator re-checks).
Objective: maximize sum_i utility_i * sum_s x_{i,s}; secondary lexicographic tie-break: earlier total start (recorded),
weighted so it can never change the integer primary objective.
Solver: scipy.optimize.milp (HiGHS) with a time limit; statuses are mapped explicitly (see STATUSES). Rounding of binaries
uses tolerance 1e-6 and the rounded candidate is re-checked by the exact simulator; a candidate failing the checker is
rejected. Small instances (bounded enumeration) are additionally solved by an exhaustive oracle structurally independent
of the constraint construction; infeasibility is then established, not merely reported.

What this certifies: feasibility of a schedule UNDER THE DECLARED MODEL AND BOUNDS, and optimality to solver tolerance or
by enumeration. It does not certify physical hardware, the accuracy of imported measurements, or utility as real-world value."""
import itertools
import json
import math
import time
from fractions import Fraction

SCHEMA = 'robust-resource-plan-input/v1'
RESULT_SCHEMA = 'robust-resource-plan-result/v1'
MODEL_ID = 'robust-resource-plan/v1'
LIMITS = {'max_slots': 96, 'max_tasks': 24, 'max_resources': 4, 'max_start_variables': 2000, 'max_magnitude': 10 ** 12, 'max_sweep': 8, 'max_sensitivity': 8, 'oracle_max_tasks': 6, 'oracle_max_combinations': 30000, 'default_time_limit_s': 20, 'max_time_limit_s': 120}
STATUSES = ('optimal_within_tolerance', 'feasible_incumbent_no_optimality_claim', 'infeasible_by_solver', 'infeasible_established_by_enumeration', 'limit_no_candidate', 'numerical_failure', 'candidate_rejected_by_checker', 'invalid_input')


class Invalid(ValueError):
    pass


def _int(v, name, lo=0, hi=None):
    if type(v) is not int or isinstance(v, bool) or v < lo or (hi is not None and v > hi) or abs(v) > LIMITS['max_magnitude']:
        raise Invalid('%s: integer in [%s, %s]' % (name, lo, hi if hi is not None else LIMITS['max_magnitude']))
    return v


def validate(data):
    if type(data) is not dict or data.get('schema') != SCHEMA:
        raise Invalid('schema must be ' + SCHEMA)
    allowed = {'schema', 'slot_seconds', 'slots', 'capacity', 'initial_low', 'reserve', 'supply_low', 'supply_high', 'base_low', 'base_high', 'resources', 'tasks', 'objectives', 'sensitivity', 'time_limit_s', 'units', 'assumptions', 'private_label', 'uncertainty_interpretation', 'device_policy', 'provenance'}
    unknown = set(data) - allowed
    if unknown:
        raise Invalid('unknown fields are refused: ' + ','.join(sorted(unknown)))
    T = _int(data.get('slots'), 'slots', 1, LIMITS['max_slots']); _int(data.get('slot_seconds'), 'slot_seconds', 1, 86400)
    cap = _int(data.get('capacity'), 'capacity', 1); _int(data.get('initial_low'), 'initial_low', 0, cap); _int(data.get('reserve'), 'reserve', 0, cap)
    for name in ('supply_low', 'supply_high', 'base_low', 'base_high'):
        v = data.get(name)
        if type(v) is not list or len(v) != T:
            raise Invalid('%s: list of %d integers (mW)' % (name, T))
        for t, x in enumerate(v):
            _int(x, '%s[%d]' % (name, t), 0)
    if any(data['supply_low'][t] > data['supply_high'][t] or data['base_low'][t] > data['base_high'][t] for t in range(T)):
        raise Invalid('reversed interval bounds')
    if data.get('uncertainty_interpretation') not in ('specification_bound', 'observed_min_max'):
        raise Invalid('uncertainty_interpretation must be declared as specification_bound or observed_min_max (a confidence interval is not a hard bound for robust feasibility)')
    res = data.get('resources') or {}
    if type(res) is not dict or len(res) > LIMITS['max_resources'] or not all(type(k) is str and 1 <= len(k) <= 32 for k in res):
        raise Invalid('resources: {name: capacity} up to %d' % LIMITS['max_resources'])
    for k, v in res.items():
        _int(v, 'resources.' + k, 0)
    tasks = data.get('tasks')
    if type(tasks) is not list or not 0 <= len(tasks) <= LIMITS['max_tasks']:
        raise Invalid('tasks: 0..%d' % LIMITS['max_tasks'])
    ids, nvars = set(), 0
    for tk in tasks:
        if type(tk) is not dict or set(tk) - {'id', 'mandatory', 'utility', 'duration', 'earliest_start', 'latest_start', 'power_high', 'resources', 'dependencies', 'exclusive_with', 'cost'}:
            raise Invalid('task fields: id, mandatory, utility, duration, earliest_start, latest_start, power_high, resources, dependencies, exclusive_with, cost')
        if type(tk.get('id')) is not str or not 1 <= len(tk['id']) <= 32 or tk['id'] in ids:
            raise Invalid('task id: unique 1..32 chars')
        ids.add(tk['id'])
        if type(tk.get('mandatory', False)) is not bool:
            raise Invalid('mandatory: bool')
        _int(tk.get('utility', 0), 'utility', 0); d = _int(tk.get('duration'), 'duration', 1, T); _int(tk.get('power_high', 0), 'power_high', 0); _int(tk.get('cost', 0), 'cost', 0)
        es = _int(tk.get('earliest_start', 0), 'earliest_start', 0, T - 1); ls = _int(tk.get('latest_start', T - d), 'latest_start', 0, T - 1)
        if ls < es:
            raise Invalid('latest_start < earliest_start for ' + tk['id'])
        nvars += max(0, min(ls, T - d) - es + 1)
        for k, v in (tk.get('resources') or {}).items():
            if k not in res:
                raise Invalid('task %s uses unknown resource %s' % (tk['id'], k))
            _int(v, 'resource demand', 0)
    for tk in tasks:
        for dep in tk.get('dependencies') or []:
            if dep not in ids or dep == tk['id']:
                raise Invalid('dependency of %s not a task: %s' % (tk['id'], dep))
        for ex in tk.get('exclusive_with') or []:
            if ex not in ids or ex == tk['id']:
                raise Invalid('exclusive_with of %s not a task: %s' % (tk['id'], ex))
    if nvars > LIMITS['max_start_variables']:
        raise Invalid('start variables %d exceed %d' % (nvars, LIMITS['max_start_variables']))
    obj = data.get('objectives') or {'mode': 'utility'}
    if type(obj) is not dict or obj.get('mode') not in ('utility', 'cost_sweep'):
        raise Invalid("objectives.mode: utility | cost_sweep")
    if obj['mode'] == 'cost_sweep':
        ceilings = obj.get('cost_ceilings')
        if type(ceilings) is not list or not 1 <= len(ceilings) <= LIMITS['max_sweep'] or not all(type(c) is int and c >= 0 for c in ceilings):
            raise Invalid('objectives.cost_ceilings: 1..%d integers' % LIMITS['max_sweep'])
    sens = data.get('sensitivity') or []
    if type(sens) is not list or len(sens) > LIMITS['max_sensitivity']:
        raise Invalid('sensitivity: up to %d changes' % LIMITS['max_sensitivity'])
    for ch in sens:
        if type(ch) is not dict or ch.get('parameter') not in ('reserve', 'initial_low', 'capacity', 'supply_scale_percent', 'base_scale_percent') or type(ch.get('value')) is not int:
            raise Invalid('sensitivity change: {parameter: reserve|initial_low|capacity|supply_scale_percent|base_scale_percent, value: int}')
    tl = data.get('time_limit_s', LIMITS['default_time_limit_s'])
    _int(tl, 'time_limit_s', 1, LIMITS['max_time_limit_s'])
    if type(data.get('private_label', 'x')) is not str:
        raise Invalid('private_label')
    return data


def starts_of(task, T):
    d = task['duration']; es = task.get('earliest_start', 0); ls = min(task.get('latest_start', T - d), T - d)
    return list(range(es, ls + 1))


def work_units(data):
    """Decision variables (start variables) plus slots: the quote's work bound."""
    T = data['slots']
    return sum(len(starts_of(t, T)) for t in data['tasks']) + T


# ---- exact simulator (independent of the MILP encoding) ------------------------------------------------------------
def simulate(data, assignments):
    """assignments: {task_id: start_slot} for selected tasks. Exact integer replay under the declared model.
    Returns {'feasible', 'violations': [first-found structured violations], 'trajectory', 'resource_use', 'utility', 'cost', 'min_margin', 'spill'}."""
    T, ss, cap, r = data['slots'], data['slot_seconds'], data['capacity'], data['reserve']
    tasks = {t['id']: t for t in data['tasks']}
    viol = []
    for tid, s in assignments.items():
        if tid not in tasks:
            viol.append({'code': 'unknown_task', 'task': tid}); continue
        t = tasks[tid]
        if type(s) is not int or s not in starts_of(t, T):
            viol.append({'code': 'start_outside_window', 'task': tid, 'start': s, 'window': [starts_of(t, T)[0], starts_of(t, T)[-1]] if starts_of(t, T) else None})
    for t in data['tasks']:
        if t.get('mandatory') and t['id'] not in assignments:
            viol.append({'code': 'mandatory_task_missing', 'task': t['id']})
    if viol:
        return {'feasible': False, 'violations': viol}
    active = [[] for _ in range(T)]
    for tid, s in assignments.items():
        for t in range(s, s + tasks[tid]['duration']):
            active[t].append(tid)
    for tid, s in assignments.items():
        t = tasks[tid]
        for dep in t.get('dependencies') or []:
            if dep not in assignments:
                viol.append({'code': 'dependency_not_selected', 'task': tid, 'dependency': dep})
            elif assignments[dep] + tasks[dep]['duration'] > s:
                viol.append({'code': 'dependency_not_finished', 'task': tid, 'dependency': dep, 'slot': s})
        for ex in t.get('exclusive_with') or []:
            if ex in assignments and any(tid in a and ex in a for a in active):
                viol.append({'code': 'exclusive_overlap', 'task': tid, 'other': ex})
    if viol:
        return {'feasible': False, 'violations': viol}
    resource_use = {k: [0] * T for k in data.get('resources') or {}}
    for t in range(T):
        for tid in active[t]:
            for k, q in (tasks[tid].get('resources') or {}).items():
                resource_use[k][t] += q
    for k, capk in (data.get('resources') or {}).items():
        for t in range(T):
            if resource_use[k][t] > capk:
                viol.append({'code': 'resource_capacity_exceeded', 'resource': k, 'slot': t, 'demand': resource_use[k][t], 'capacity': capk}); break
    if viol:
        return {'feasible': False, 'violations': viol, 'resource_use': resource_use}
    E = data['initial_low']; traj = [{'boundary': 0, 'energy': E, 'margin': E - r}]; spill = 0; min_margin = E - r; consumption = []
    if E < r:
        viol.append({'code': 'reserve_violated', 'boundary': 0, 'energy': E, 'reserve': r})
    for t in range(T):
        cons = data['base_high'][t] + sum(tasks[tid]['power_high'] for tid in active[t])
        net = (data['supply_low'][t] - cons) * ss
        raw = E + net
        E2 = min(cap, raw); spill += max(0, raw - cap); E = E2
        consumption.append(cons)
        traj.append({'boundary': t + 1, 'energy': E, 'margin': E - r, 'net_mJ': net, 'active': sorted(active[t])})
        min_margin = min(min_margin, E - r)
        if E < r and not viol:
            viol.append({'code': 'reserve_violated', 'boundary': t + 1, 'energy': E, 'reserve': r, 'slot': t, 'active': sorted(active[t])})
    utility = sum(tasks[tid].get('utility', 0) for tid in assignments); cost = sum(tasks[tid].get('cost', 0) for tid in assignments)
    return {'feasible': not viol, 'violations': viol, 'trajectory': traj, 'resource_use': resource_use, 'utility': utility, 'cost': cost, 'min_margin': min_margin, 'spill': spill, 'consumption_mW': consumption,
            'intra_slot_caveat': 'reserve is checked at slot boundaries; within a slot the store can dip by at most consumption*slot_seconds below the boundary value'}


def tie_key(data, assignments, sim):
    """Deterministic ranking: higher utility, then lower total energy consumed, then earlier total start, then lexicographic ids."""
    tasks = {t['id']: t for t in data['tasks']}
    energy = sum(tasks[i]['power_high'] * tasks[i]['duration'] for i in assignments)
    return (-sim['utility'], energy, sum(assignments.values()), sorted(assignments.items()))


# ---- exhaustive oracle (bounded; independent) ----------------------------------------------------------------------
def oracle(data, cost_ceiling=None):
    T = data['slots']; tasks = data['tasks']
    if len(tasks) > LIMITS['oracle_max_tasks']:
        return None
    choices = [([] if t.get('mandatory') else [None]) + starts_of(t, T) for t in tasks]
    combos = 1
    for ch in choices:
        combos *= len(ch)
    if combos > LIMITS['oracle_max_combinations']:
        return None
    best, best_key, feasible_count = None, None, 0
    for combo in itertools.product(*choices):
        assign = {t['id']: s for t, s in zip(tasks, combo) if s is not None}
        sim = simulate(data, assign)
        if not sim['feasible'] or (cost_ceiling is not None and sim['cost'] > cost_ceiling):
            continue
        feasible_count += 1
        key = tie_key(data, assign, sim)
        if best_key is None or key < best_key:
            best, best_key = (assign, sim), key
    return {'combinations': combos, 'feasible_assignments': feasible_count, 'best': None if best is None else {'assignments': best[0], 'utility': best[1]['utility'], 'cost': best[1]['cost'], 'min_margin': best[1]['min_margin']}}


# ---- MILP -----------------------------------------------------------------------------------------------------------
def build_and_solve(data, cost_ceiling=None, time_limit=None):
    import numpy as np
    from scipy.optimize import milp, LinearConstraint, Bounds
    import scipy
    T, ss, cap, r = data['slots'], data['slot_seconds'], data['capacity'], data['reserve']
    tasks = data['tasks']; tid_index = {t['id']: i for i, t in enumerate(tasks)}
    xvars = []                                       # (task index, start)
    for i, t in enumerate(tasks):
        for s in starts_of(t, T):
            xvars.append((i, s))
    nx = len(xvars); nE = T + 1; nS = T
    n = nx + nE + nS
    def xi(i, s):
        return xvars.index((i, s))
    Ecol = lambda b: nx + b
    Scol = lambda t: nx + nE + t
    rows, lo, hi = [], [], []
    def add(coeffs, low, high):
        row = np.zeros(n)
        for c, v in coeffs:
            row[c] += v
        rows.append(row); lo.append(low); hi.append(high)
    # selection
    for i, t in enumerate(tasks):
        cols = [(xi(i, s), 1.0) for s in starts_of(t, T)]
        if t.get('mandatory'):
            if not cols:
                return {'status': 'infeasible_by_solver', 'reason': 'mandatory task %s has no admissible start' % t['id'], 'solver': None}
            add(cols, 1, 1)
        elif cols:
            add(cols, 0, 1)
    # active indicators are linear expressions of x; resources, exclusivity, energy
    def active_cols(i, t):
        return [(xi(i, s), 1.0) for s in starts_of(tasks[i], T) if s <= t < s + tasks[i]['duration']]
    for k, capk in (data.get('resources') or {}).items():
        for t in range(T):
            cols = []
            for i, tk in enumerate(tasks):
                q = (tk.get('resources') or {}).get(k, 0)
                if q:
                    cols += [(c, v * q) for c, v in active_cols(i, t)]
            if cols:
                add(cols, -np.inf, capk)
    pairs = set()
    for i, tk in enumerate(tasks):
        for ex in tk.get('exclusive_with') or []:
            j = tid_index[ex]
            if (min(i, j), max(i, j)) in pairs:
                continue
            pairs.add((min(i, j), max(i, j)))
            for t in range(T):
                cols = active_cols(i, t) + active_cols(j, t)
                if cols:
                    add(cols, -np.inf, 1)
        for dep in tk.get('dependencies') or []:
            j = tid_index[dep]
            for s in starts_of(tk, T):
                ok = [(xi(j, sj), 1.0) for sj in starts_of(tasks[j], T) if sj + tasks[j]['duration'] <= s]
                add([(xi(i, s), 1.0)] + [(c, -v) for c, v in ok], -np.inf, 0)             # x_{i,s} <= sum of enabling starts of j
    # energy recurrence: E_{t+1} - E_t + sum_i p_i*ss*a_{i,t} + sp_t = (supply_low_t - base_high_t)*ss
    for t in range(T):
        cols = [(Ecol(t + 1), 1.0), (Ecol(t), -1.0), (Scol(t), 1.0)]
        for i, tk in enumerate(tasks):
            if tk['power_high']:
                cols += [(c, v * tk['power_high'] * ss) for c, v in active_cols(i, t)]
        rhs = (data['supply_low'][t] - data['base_high'][t]) * ss
        add(cols, rhs, rhs)
    if cost_ceiling is not None:
        cols = [(xi(i, s), float(tk.get('cost', 0))) for i, tk in enumerate(tasks) for s in starts_of(tk, T) if tk.get('cost', 0)]
        if cols:
            add(cols, -np.inf, cost_ceiling)
    A = np.array(rows) if rows else np.zeros((0, n))
    lb = np.full(n, 0.0); ub = np.full(n, 1.0)
    lb[nx:nx + nE] = r; ub[nx:nx + nE] = cap; lb[Ecol(0)] = ub[Ecol(0)] = data['initial_low']
    ub[nx + nE:] = np.inf
    if data['initial_low'] < r:
        return {'status': 'infeasible_by_solver', 'reason': 'initial energy below the reserve at boundary 0', 'solver': None}
    eps = 0.5 / (T * max(1, nx) + 1)
    c = np.zeros(n)
    for (i, s) in xvars:
        c[xi(i, s)] = -float(tasks[i].get('utility', 0)) + eps * s
    integrality = np.zeros(n); integrality[:nx] = 1
    tl = float(time_limit or data.get('time_limit_s') or LIMITS['default_time_limit_s'])
    t0 = time.time()
    try:
        res = milp(c, constraints=[LinearConstraint(A, lo, hi)] if rows else [], integrality=integrality, bounds=Bounds(lb, ub), options={'time_limit': tl, 'disp': False})
    except Exception as exc:
        return {'status': 'numerical_failure', 'reason': type(exc).__name__ + ': ' + str(exc)[:120], 'solver': {'name': 'scipy.optimize.milp (HiGHS)', 'scipy': scipy.__version__}}
    runtime = time.time() - t0
    solver = {'name': 'scipy.optimize.milp (HiGHS)', 'scipy': scipy.__version__, 'status_code': int(res.status), 'message': str(res.message)[:160], 'runtime_s': round(runtime, 4), 'time_limit_s': tl, 'variables': n, 'binary_variables': nx, 'constraints': len(rows),
              'mip_gap': (float(res.mip_gap) if getattr(res, 'mip_gap', None) is not None else None), 'dual_bound': (float(res.mip_dual_bound) if getattr(res, 'mip_dual_bound', None) is not None else None), 'primal_objective': (float(res.fun) if res.x is not None else None),
              'tie_break': 'secondary objective: earlier total start, weight %.3g per slot (cannot change the integer utility)' % eps}
    if res.x is None:
        if res.status == 2:
            return {'status': 'infeasible_by_solver', 'reason': 'solver reports infeasible', 'solver': solver}
        if res.status == 1:
            return {'status': 'limit_no_candidate', 'reason': 'time limit reached before any feasible candidate; not evidence of infeasibility', 'solver': solver}
        return {'status': 'numerical_failure', 'reason': str(res.message)[:120], 'solver': solver}
    assign, rounding_max, multi = {}, 0.0, []
    for (i, s) in xvars:
        v = float(res.x[xi(i, s)]); rounding_max = max(rounding_max, abs(v - round(v)))
        if v > 0.5:
            if tasks[i]['id'] in assign:
                multi.append(tasks[i]['id'])
            assign[tasks[i]['id']] = s
    solver['max_binary_rounding'] = rounding_max
    if rounding_max > 1e-6:
        return {'status': 'candidate_rejected_by_checker', 'reason': 'binary variables not integral within tolerance 1e-6 (%.3g)' % rounding_max, 'solver': solver}
    if multi:
        return {'status': 'candidate_rejected_by_checker', 'reason': 'more than one start selected for: ' + ','.join(sorted(set(multi))), 'solver': solver}
    status = 'optimal_within_tolerance' if res.status == 0 else 'feasible_incumbent_no_optimality_claim'
    return {'status': status, 'assignments': assign, 'solver': solver}


def solve(data):
    """Full operation: main solve + checker (+ oracle when small) + optional cost sweep + sensitivity. Deterministic for pinned inputs."""
    validate(data)
    out = {'result_schema': RESULT_SCHEMA, 'model_id': MODEL_ID, 'units': {'energy': 'mJ', 'power': 'mW', 'time': 's', 'utility': 'declared integer'}, 'uncertainty_set': 'independent interval bounds per slot (%s); minimum supply and maximum consumption used; correlations ignored conservatively' % data['uncertainty_interpretation'],
           'assumptions': ['conservative recurrence with saturation at capacity', 'reserve checked at every slot boundary', 'supply and consumption accrue over the slot', 'tasks consume their declared maximum power for their whole duration', 'no partial tasks', 'declared bounds are not a hardware certification'],
           'limits': dict(LIMITS), 'conditional_on': 'declared-interval-model;not-a-measurement;not-a-hardware-guarantee'}
    main = _solve_case(data, None)
    out.update(main)
    if (data.get('objectives') or {}).get('mode') == 'cost_sweep':
        out['alternatives'] = sweep(data)
    if data.get('sensitivity'):
        out['sensitivity'] = sensitivity(data, main)
    return out


def _solve_case(data, cost_ceiling):
    sol = build_and_solve(data, cost_ceiling)
    case = {'status': sol['status'], 'solver': sol.get('solver'), 'reason': sol.get('reason')}
    if sol.get('assignments') is not None:
        sim = simulate(data, sol['assignments'])
        if cost_ceiling is not None and sim.get('cost', 0) > cost_ceiling:
            sim['feasible'] = False; sim['violations'] = [{'code': 'cost_ceiling_exceeded', 'cost': sim['cost'], 'ceiling': cost_ceiling}]
        if not sim['feasible']:
            case.update(status='candidate_rejected_by_checker', reason='solver candidate failed the exact simulator', checker=sim)
        else:
            case.update(assignments=sol['assignments'], selected=sorted(sol['assignments']), objective=sim['utility'], cost=sim['cost'], min_margin=sim['min_margin'], spill=sim['spill'], trajectory=sim['trajectory'], resource_use=sim['resource_use'], checker={'feasible': True, 'intra_slot_caveat': sim['intra_slot_caveat']})
            case['excluded'] = explain_excluded(data, sol['assignments'], sim)
    orc = oracle(data, cost_ceiling)
    if orc is not None:
        case['oracle'] = orc
        if orc['best'] is None:
            if case['status'] in ('infeasible_by_solver', 'limit_no_candidate'):
                case['status'] = 'infeasible_established_by_enumeration'
            elif case.get('assignments'):
                case['status'] = 'candidate_rejected_by_checker'; case['reason'] = 'the exhaustive oracle finds no feasible assignment but the solver returned one'
        elif case.get('objective') is not None:
            case['oracle_agreement'] = {'solver_objective': case['objective'], 'oracle_objective': orc['best']['utility'], 'agree': case['objective'] == orc['best']['utility']}
            if case['status'] == 'optimal_within_tolerance' and case['objective'] != orc['best']['utility']:
                case['status'] = 'numerical_failure'; case['reason'] = 'solver optimum disagrees with the exhaustive oracle'
            elif case['status'] == 'optimal_within_tolerance':
                case['optimality'] = 'established by enumeration and solver'
        elif case['status'] in ('infeasible_by_solver', 'limit_no_candidate'):
            case['status'] = 'numerical_failure'; case['reason'] = 'oracle found a feasible assignment the solver did not'
    if case.get('objective') is not None and 'optimality' not in case:
        case['optimality'] = 'to solver tolerance (mip gap %s)' % case['solver'].get('mip_gap') if case['status'] == 'optimal_within_tolerance' else 'not claimed (feasible incumbent)'
    if cost_ceiling is not None:
        case['cost_ceiling'] = cost_ceiling
    return case


def explain_excluded(data, assignments, sim):
    """Why an excluded task was not selected, only when a single-swap replay establishes it; otherwise 'not uniquely determined'."""
    out = []
    T = data['slots']
    for t in data['tasks']:
        if t['id'] in assignments:
            continue
        reasons = []
        starts = starts_of(t, T)
        if not starts:
            reasons.append('no admissible start')
        elif t.get('utility', 0) == 0:
            reasons.append('zero utility')
        else:
            adds = [simulate(data, dict(assignments, **{t['id']: s})) for s in starts]
            if all(not a['feasible'] for a in adds):
                codes = sorted({v['code'] for a in adds for v in a['violations'][:1]})
                reasons.append('adding it at any admissible start violates: ' + ', '.join(codes))
            else:
                reasons.append('not uniquely determined by this solution (it could be added only by dropping others; the optimizer preferred the returned set)')
        out.append({'task': t['id'], 'reason': reasons[0]})
    return out


def sweep(data):
    """Epsilon-constraint sweep over declared cost ceilings: bounded, deduplicated, with dominance among the evaluated set."""
    cands = []
    for ceiling in sorted(set(data['objectives']['cost_ceilings'])):
        case = _solve_case(data, ceiling)
        cands.append(case)
    seen, alts = {}, []
    for c in cands:
        key = json.dumps(c.get('assignments'), sort_keys=True) if c.get('assignments') is not None else None
        if key is not None and key in seen:
            seen[key]['also_optimal_for_ceilings'].append(c['cost_ceiling']); continue
        entry = {'cost_ceiling': c['cost_ceiling'], 'status': c['status'], 'assignments': c.get('assignments'), 'utility': c.get('objective'), 'cost': c.get('cost'), 'min_margin': c.get('min_margin'), 'trajectory': c.get('trajectory'), 'solver': c.get('solver'), 'also_optimal_for_ceilings': [], 'optimality': c.get('optimality')}
        if key is not None:
            seen[key] = entry
        alts.append(entry)
    # dominance within the evaluated set: maximize utility, minimize cost, maximize min_margin
    for a in alts:
        if a['utility'] is None:
            a['dominated_by'] = None; continue
        dom = [b['cost_ceiling'] for b in alts if b is not a and b['utility'] is not None and b['utility'] >= a['utility'] and b['cost'] <= a['cost'] and b['min_margin'] >= a['min_margin'] and (b['utility'], -b['cost'], b['min_margin']) != (a['utility'], -a['cost'], a['min_margin'])]
        a['dominated_by'] = dom
        a['pareto_within_sweep'] = not dom
    return {'method': 'epsilon-constraint on declared cost, one solve per ceiling; dominance evaluated only among these candidates (not a complete frontier)', 'ceilings': sorted(set(data['objectives']['cost_ceilings'])), 'candidates': alts}


def sensitivity(data, main):
    """Finite one-at-a-time changes; each re-solved and re-checked; labelled as a finite study of a discrete decision."""
    base_sel = main.get('selected')
    rows = []
    for ch in data['sensitivity']:
        d2 = json.loads(json.dumps({k: v for k, v in data.items() if k not in ('sensitivity', 'objectives')}))
        p, v = ch['parameter'], ch['value']
        if p in ('reserve', 'initial_low', 'capacity'):
            d2[p] = v
        elif p == 'supply_scale_percent':
            d2['supply_low'] = [x * v // 100 for x in d2['supply_low']]; d2['supply_high'] = [x * v // 100 for x in d2['supply_high']]
        elif p == 'base_scale_percent':
            d2['base_high'] = [-(-x * v // 100) for x in d2['base_high']]; d2['base_low'] = [x * v // 100 for x in d2['base_low']]
        try:
            validate(d2); case = _solve_case(d2, None)
        except Invalid as exc:
            rows.append({'change': ch, 'status': 'invalid_input', 'reason': str(exc), 'objective': None, 'selected': None, 'min_margin': None, 'decision_changed': None, 'first_violation': None}); continue
        rows.append({'change': ch, 'status': case['status'], 'objective': case.get('objective'), 'selected': case.get('selected'), 'min_margin': case.get('min_margin'), 'decision_changed': (case.get('selected') != base_sel),
                     'first_violation': (case.get('checker') or {}).get('violations', [None])[0] if case.get('status') == 'candidate_rejected_by_checker' else None})
    changed = [r for r in rows if r.get('decision_changed')]
    return {'method': 'finite one-at-a-time changes, each re-solved and re-checked; not a derivative (the decision is discrete)', 'rows': rows,
            'value_of_information_proxy': {'definition': 'number of tested changes that alter the selected set or feasibility', 'count': len(changed), 'of': len(rows), 'note': 'a ranking of which inputs matter within the tested range; not a monetary valuation'}}
