"""Bounded resource and contact planning (order 2026-10-23 group E).

Problem: a finite horizon [start, end) discretised into `slot_seconds` slots (explicit), tasks (kind, quantity, duration in slots,
dependencies, release and deadline slots, mission tag) and offers (kind, window as a slot range, available capacity in units,
declared price and energy per unit). Constraints: an assigned offer matches the kind and covers the task's slots; dependencies finish
before a task starts; summed quantities per offer never exceed its available capacity (shared across missions); declared cost per
mission stays within that mission's budget; declared energy stays within budget minus reserve; deadlines hold.

Planners: `baseline` (deterministic greedy list scheduling, no backtracking), `improved` (depth-first branch and bound over the
offer choice per task with a lower bound and a timeout; exposes incumbent, bound and status), `oracle` (exhaustive enumeration on
fixtures below a product bound; the reference the other two are checked against). Start slots are left-shifted (earliest feasible)
because capacity counts units over an offer window, so starting earlier never hurts either objective.

Objectives: `earliest_delivery` (minimise the makespan subject to budget; cost breaks ties) and `lowest_cost` (minimise declared cost
subject to the delivery deadline and reserves; makespan breaks ties). A result is a feasible schedule or an explicit unsatisfied
constraint witness. Energy and thermal figures are planning assumptions: nothing here meters rover electricity. A plan never locks
capacity or spends money: reservation acceptance revalidates the selected offers.
"""
import itertools
import time

from ..errors import ServiceError

OBJECTIVES = ('earliest_delivery', 'lowest_cost')
PLANNERS = ('baseline', 'improved', 'oracle')
MAX_SLOTS = 4096
ORACLE_MAX_COMBINATIONS = 50000
LABELS = {'energy': 'planning_assumption: declared mWh per unit from offers; no instrument meters actual rover electricity',
          'thermal': 'not modelled: no thermal constraint is represented; a thermal model would be a planning assumption unless calibrated',
          'binding': 'a plan never locks capacity or spends; reservation acceptance revalidates offer version, capacity and funds'}


def _int(v, lo, hi, name):
    if type(v) is bool or type(v) is not int or not lo <= v <= hi:
        raise ServiceError('VALIDATION', {'code': 'bounded_integer', 'field': name, 'min': lo, 'max': hi})
    return v


def normalize(problem):
    """Validate and discretise a raw problem; returns the internal form with offers mapped onto slot ranges."""
    if type(problem) is not dict or set(problem) - {'horizon', 'tasks', 'offers', 'budgets', 'budget', 'energy_budget_mwh', 'energy_reserve_mwh', 'deadline_slot', 'objective', 'label'}:
        raise ServiceError('VALIDATION', {'code': 'fields', 'allowed': ['horizon{start,end,slot_seconds}', 'tasks[]', 'offers[]', 'budgets{mission: amount} | budget', 'energy_budget_mwh?', 'energy_reserve_mwh?', 'deadline_slot?', 'objective?', 'label?']})
    h = problem.get('horizon') or {}
    start, end, slot = _int(h.get('start'), 0, 10 ** 12, 'horizon.start'), _int(h.get('end'), 0, 10 ** 12, 'horizon.end'), _int(h.get('slot_seconds'), 1, 10 ** 9, 'horizon.slot_seconds')
    if end <= start:
        raise ServiceError('VALIDATION', {'code': 'horizon', 'note': 'end must follow start'})
    n = -(-(end - start) // slot)
    if n > MAX_SLOTS:
        raise ServiceError('VALIDATION', {'code': 'horizon_slots', 'slots': n, 'max': MAX_SLOTS, 'note': 'coarsen slot_seconds or shorten the horizon'})
    tasks, ids = [], set()
    for i, t in enumerate(problem.get('tasks') or []):
        if type(t) is not dict or type(t.get('id')) is not str or t['id'] in ids:
            raise ServiceError('VALIDATION', {'code': 'task', 'index': i})
        ids.add(t['id'])
        tasks.append({'id': t['id'], 'kind': str(t.get('kind', 'compute')), 'quantity': _int(t.get('quantity', 1), 0, 10 ** 12, 'task.quantity'), 'duration': _int(t.get('duration_slots', 1), 1, n, 'task.duration_slots'),
                      'depends': list(t.get('depends') or []), 'release': _int(t.get('release_slot', 0), 0, n, 'task.release_slot'), 'deadline': None if t.get('deadline_slot') is None else _int(t['deadline_slot'], 1, n, 'task.deadline_slot'),
                      'mission': str(t.get('mission', 'm')), 'fixed_cost': _int(t.get('fixed_cost', 0), 0, 10 ** 15, 'task.fixed_cost'), 'local': bool(t.get('local', False)), 'label': str(t.get('label', t['id']))})
    for t in tasks:
        for d in t['depends']:
            if d not in ids:
                raise ServiceError('VALIDATION', {'code': 'dependency', 'task': t['id'], 'missing': d})
    order = _topological(tasks)
    offers = []
    for i, o in enumerate(problem.get('offers') or []):
        if type(o) is not dict or type(o.get('id')) is not str:
            raise ServiceError('VALIDATION', {'code': 'offer', 'index': i})
        ws, we = _int(o.get('window_start', start), 0, 10 ** 12, 'offer.window_start'), _int(o.get('window_end', end), 0, 10 ** 12, 'offer.window_end')
        s0, s1 = max(0, (ws - start) // slot if ws >= start else 0), min(n, -(-(we - start) // slot)) if we > start else 0
        pool = o.get('pool')
        if pool is not None:
            if type(pool) is not dict or type(pool.get('id')) is not str:
                raise ServiceError('VALIDATION', {'code': 'offer_pool'})
            pool = {'id': pool['id'], 'capacity': _int(pool.get('capacity', 1), 0, 10 ** 9, 'pool.capacity'), 'units_per_slot_second': _int(pool.get('units_per_slot_second', 1), 1, 10 ** 9, 'pool.units_per_slot_second')}
        offers.append({'id': o['id'], 'kind': str(o.get('kind', 'compute')), 'available': _int(o.get('available', 0), 0, 10 ** 12, 'offer.available'), 'price': _int(o.get('price_per_unit', 0), 0, 10 ** 9, 'offer.price_per_unit'),
                       'energy': _int(o.get('energy_mwh_per_unit', 0), 0, 10 ** 9, 'offer.energy_mwh_per_unit'), 'slot_start': s0, 'slot_end': s1, 'window': [ws, we], 'provider': o.get('provider'), 'pool': pool})
    budgets = dict(problem.get('budgets') or {})
    if problem.get('budget') is not None:
        for t in tasks:
            budgets.setdefault(t['mission'], problem['budget'])
    for k, v in budgets.items():
        _int(v, 0, 10 ** 15, 'budget.' + str(k))
    eb, er = problem.get('energy_budget_mwh'), problem.get('energy_reserve_mwh', 0)
    if eb is not None:
        _int(eb, 0, 10 ** 15, 'energy_budget_mwh')
    _int(er, 0, 10 ** 15, 'energy_reserve_mwh')
    dl = problem.get('deadline_slot')
    if dl is not None:
        _int(dl, 1, n, 'deadline_slot')
    obj = problem.get('objective', 'earliest_delivery')
    if obj not in OBJECTIVES:
        raise ServiceError('VALIDATION', {'code': 'objective', 'allowed': list(OBJECTIVES)})
    return {'horizon': {'start': start, 'end': end, 'slot_seconds': slot, 'slots': n}, 'tasks': tasks, 'order': order, 'offers': offers, '_offers': {o['id']: o for o in offers}, 'budgets': budgets, 'energy_budget': eb, 'energy_reserve': er, 'deadline': dl, 'objective': obj, 'label': problem.get('label')}


def _topological(tasks):
    by = {t['id']: t for t in tasks}; seen, out, mark = set(), [], set()

    def visit(tid, stack):
        if tid in seen:
            return
        if tid in stack:
            raise ServiceError('VALIDATION', {'code': 'cycle', 'task': tid})
        for d in by[tid]['depends']:
            visit(d, stack | {tid})
        seen.add(tid); out.append(tid)
    for t in tasks:
        visit(t['id'], frozenset())
    return out


# ---- evaluation --------------------------------------------------------------------------------------------------------------------
def _options(P, t):
    """Feasible offers for a task ignoring shared state (kind, window length, deadline, release); local tasks use the implicit 'local' slot."""
    if t['local']:
        return [{'id': None, 'kind': t['kind'], 'available': 10 ** 15, 'price': 0, 'energy': 0, 'slot_start': 0, 'slot_end': P['horizon']['slots'], 'local': True}]
    out = []
    for o in P['offers']:
        if o['kind'] != t['kind'] or o['available'] < t['quantity'] or o['slot_end'] - o['slot_start'] < t['duration']:
            continue
        if o.get('pool') and _pool_slots(P, t, o) > o['pool']['capacity']:
            continue
        if t['deadline'] is not None and o['slot_start'] + t['duration'] > t['deadline']:
            continue
        out.append(o)
    return out


def _pool_slots(P, t, o):
    """Concurrent pool slots a task needs on a pool-backed offer: ceil(quantity / (units_per_slot_second × seconds of the task))."""
    pool = o.get('pool')
    if not pool:
        return 0
    seconds = t['duration'] * P['horizon']['slot_seconds']; per_slot = pool['units_per_slot_second'] * seconds
    return -(-t['quantity'] // per_slot) if per_slot else 0


def _pool_ok(P, by_task, choice, sched):
    """Interval sweep per pool over the planner slots: concurrent slot demand never exceeds the pool capacity."""
    pools = {}
    for tid, oid in choice.items():
        if oid is None or tid not in sched:
            continue
        o = P['_offers'].get(oid)
        if o and o.get('pool'):
            pools.setdefault(o['pool']['id'], []).append((sched[tid]['start_slot'], sched[tid]['end_slot'], _pool_slots(P, by_task[tid], o), o['pool']['capacity']))
    for pid, items in pools.items():
        cap = items[0][3]
        for a, b, n, _ in items:
            for t in range(a, b):
                used = sum(n2 for a2, b2, n2, _ in items if a2 <= t < b2)
                if used > cap:
                    return False, {'constraint': 'pool_capacity', 'pool': pid, 'slot': t, 'used': used, 'capacity': cap}
    return True, None


def _pool_start(P, by_task, choice, sched, t, o, s0):
    """Earliest start >= s0 at which the task fits its offer's pool given the tasks already scheduled (deterministic rule shared by
    the oracle, the baseline and the improved planner: 'left-shift, then delay only as far as the pool forces')."""
    if not o.get('pool'):
        return s0
    last = min(o['slot_end'], P['horizon']['slots']) - t['duration']
    s = s0
    while s <= last:
        partial = dict(sched); partial[t['id']] = {'start_slot': s, 'end_slot': s + t['duration']}
        ok, _ = _pool_ok(P, by_task, dict(choice, **{t['id']: o['id']}), partial)
        if ok:
            return s
        s += 1
    return None


def _start_for(P, t, o, ends):
    s = max(t['release'], o['slot_start'], max((ends[d] for d in t['depends']), default=0))
    if s + t['duration'] > o['slot_end'] or s + t['duration'] > P['horizon']['slots']:
        return None
    if t['deadline'] is not None and s + t['duration'] > t['deadline']:
        return None
    if P['deadline'] is not None and s + t['duration'] > P['deadline']:
        return None
    return s


def evaluate(P, choice):
    """choice: task id -> offer id (None for local). Returns (ok, detail) with the schedule, cost, energy, makespan or the violated constraint."""
    by_off = {o['id']: o for o in P['offers']}; by_task = {t['id']: t for t in P['tasks']}; ends, sched = {}, {}
    used, cost, energy = {}, {}, 0
    for tid in P['order']:
        t = by_task[tid]
        if tid not in choice:
            return False, {'constraint': 'unassigned', 'task': tid}
        o = {'id': None, 'kind': t['kind'], 'available': 10 ** 15, 'price': 0, 'energy': 0, 'slot_start': 0, 'slot_end': P['horizon']['slots']} if t['local'] else by_off.get(choice[tid])
        if o is None or o['kind'] != t['kind']:
            return False, {'constraint': 'offer_kind', 'task': tid, 'offer': choice[tid]}
        s = _start_for(P, t, o, ends)
        if s is None:
            return False, {'constraint': 'window_or_deadline', 'task': tid, 'offer': choice[tid]}
        s = _pool_start(P, by_task, {k: v for k, v in choice.items() if k in sched}, sched, t, o, s)
        if s is None or (t['deadline'] is not None and s + t['duration'] > t['deadline']) or (P['deadline'] is not None and s + t['duration'] > P['deadline']):
            return False, {'constraint': 'pool_capacity', 'task': tid, 'offer': choice[tid], 'note': 'no pool-feasible start inside the window/deadline'}
        ends[tid] = s + t['duration']; sched[tid] = {'offer_id': choice[tid], 'start_slot': s, 'end_slot': s + t['duration'], 'quantity': t['quantity'], 'cost': t['quantity'] * o['price'] + t['fixed_cost'], 'energy_mwh': t['quantity'] * o['energy'], 'mission': t['mission'], 'label': t['label']}
        if not t['local']:
            used[o['id']] = used.get(o['id'], 0) + t['quantity']
            if used[o['id']] > o['available']:
                return False, {'constraint': 'capacity', 'offer': o['id'], 'demand': used[o['id']], 'available': o['available']}
        cost[t['mission']] = cost.get(t['mission'], 0) + sched[tid]['cost']; energy += sched[tid]['energy_mwh']
    ok_pool, why = _pool_ok(P, by_task, choice, sched)
    if not ok_pool:
        return False, why
    for mname, c in cost.items():
        b = P['budgets'].get(mname)
        if b is not None and c > b:
            return False, {'constraint': 'budget', 'mission': mname, 'cost': c, 'budget': b}
    if P['energy_budget'] is not None and energy > P['energy_budget'] - P['energy_reserve']:
        return False, {'constraint': 'energy', 'energy_mwh': energy, 'budget_mwh': P['energy_budget'], 'reserve_mwh': P['energy_reserve']}
    mk = max(ends.values(), default=0)
    return True, {'schedule': sched, 'cost': sum(cost.values()), 'cost_by_mission': cost, 'energy_mwh': energy, 'makespan_slots': mk, 'delivery_at': P['horizon']['start'] + mk * P['horizon']['slot_seconds']}


def objective_key(P, ev):
    return (ev['makespan_slots'], ev['cost']) if P['objective'] == 'earliest_delivery' else (ev['cost'], ev['makespan_slots'])


# ---- witnesses -----------------------------------------------------------------------------------------------------------------------
def witness(P):
    """The first constraint shown unsatisfiable by relaxation (each task alone, then additive lower bounds). None when relaxations pass."""
    by_task = {t['id']: t for t in P['tasks']}
    for tid in P['order']:
        t = by_task[tid]
        if not _options(P, t):
            cands = [o for o in P['offers'] if o['kind'] == t['kind']]
            if not cands:
                return {'constraint': 'no_offer_of_kind', 'task': tid, 'kind': t['kind']}
            if all(o['available'] < t['quantity'] for o in cands):
                return {'constraint': 'capacity', 'task': tid, 'quantity': t['quantity'], 'max_available': max(o['available'] for o in cands)}
            if all(o.get('pool') and _pool_slots(P, t, o) > o['pool']['capacity'] for o in cands):
                return {'constraint': 'pool_capacity', 'task': tid, 'note': 'the task needs more concurrent pool slots over its duration than any pool offers'}
            if t['deadline'] is not None and all(o['slot_start'] + t['duration'] > t['deadline'] for o in cands):
                return {'constraint': 'deadline', 'task': tid, 'deadline_slot': t['deadline'], 'earliest_finish_slot': min(o['slot_start'] + t['duration'] for o in cands)}
            return {'constraint': 'window', 'task': tid, 'note': 'no offer window inside the horizon holds the task duration', 'windows': [o['window'] for o in cands]}
    # critical path with earliest offers against deadlines
    ends = {}
    for tid in P['order']:
        t = by_task[tid]; best = None
        for o in _options(P, t):
            s = _start_for(P, t, o, ends)
            if s is not None and (best is None or s + t['duration'] < best):
                best = s + t['duration']
        if best is None:
            dl = P['deadline'] if t['deadline'] is None else min(t['deadline'], P['deadline'] or t['deadline'])
            return {'constraint': 'deadline' if dl is not None else 'window', 'task': tid, 'note': 'even the earliest feasible chain finishes after the deadline or outside the horizon', 'deadline_slot': dl, 'dependencies_end_slot': max((ends[d] for d in t['depends']), default=0)}
        ends[tid] = best
    for mname in {t['mission'] for t in P['tasks']}:
        b = P['budgets'].get(mname)
        if b is not None:
            lb = sum(min(t['quantity'] * o['price'] for o in _options(P, t)) + t['fixed_cost'] for t in P['tasks'] if t['mission'] == mname)
            if lb > b:
                return {'constraint': 'budget', 'mission': mname, 'minimum_cost': lb, 'budget': b, 'note': 'cheapest feasible offer per task already exceeds the budget (reserve already deducted)'}
    if P['energy_budget'] is not None:
        lb = sum(min(t['quantity'] * o['energy'] for o in _options(P, t)) for t in P['tasks'])
        if lb > P['energy_budget'] - P['energy_reserve']:
            return {'constraint': 'energy', 'minimum_energy_mwh': lb, 'budget_mwh': P['energy_budget'], 'reserve_mwh': P['energy_reserve']}
    return None


# ---- planners ------------------------------------------------------------------------------------------------------------------------
def baseline(P, timeout_ms=None):
    """Greedy list scheduling in topological order: each task takes the offer that is best for the objective given what was already chosen."""
    t0 = time.monotonic(); by_task = {t['id']: t for t in P['tasks']}; choice, ends, used, spent = {}, {}, {}, {}
    for tid in P['order']:
        t = by_task[tid]; best, bk = None, None
        for o in _options(P, t):
            if not t['local'] and used.get(o['id'], 0) + t['quantity'] > o['available']:
                continue
            s = _start_for(P, t, o, ends)
            if s is None:
                continue
            c = t['quantity'] * o['price'] + t['fixed_cost']; b = P['budgets'].get(t['mission'])
            if b is not None and spent.get(t['mission'], 0) + c > b:
                continue
            if o.get('pool'):
                partial = {k: {'start_slot': ends[k] - by_task[k]['duration'], 'end_slot': ends[k]} for k in choice}
                s = _pool_start(P, by_task, choice, partial, t, o, s)
                if s is None or (t['deadline'] is not None and s + t['duration'] > t['deadline']) or (P['deadline'] is not None and s + t['duration'] > P['deadline']):
                    continue
            k = (s + t['duration'], c) if P['objective'] == 'earliest_delivery' else (c, s + t['duration'])
            if bk is None or k < bk:
                best, bk = o, k
        if best is None:
            w = witness(P) or {'constraint': 'greedy_dead_end', 'task': tid, 'note': 'the baseline planner does not backtrack; the improved planner may still find a schedule'}
            return {'planner': 'baseline', 'status': 'infeasible' if w.get('constraint') != 'greedy_dead_end' else 'unknown', 'witness': w, 'incumbent': None, 'bound': None, 'runtime_ms': int((time.monotonic() - t0) * 1000)}
        s_best = _start_for(P, t, best, ends)
        if best.get('pool'):
            s_best = _pool_start(P, by_task, choice, {k: {'start_slot': ends[k] - by_task[k]['duration'], 'end_slot': ends[k]} for k in choice}, t, best, s_best)
        choice[tid] = best['id']; ends[tid] = s_best + t['duration']
        if not t['local']:
            used[best['id']] = used.get(best['id'], 0) + t['quantity']
        spent[t['mission']] = spent.get(t['mission'], 0) + t['quantity'] * best['price'] + t['fixed_cost']
    ok, ev = evaluate(P, choice)
    if not ok:
        return {'planner': 'baseline', 'status': 'infeasible', 'witness': ev, 'incumbent': None, 'bound': None, 'runtime_ms': int((time.monotonic() - t0) * 1000)}
    return {'planner': 'baseline', 'status': 'feasible', 'note': 'greedy result: feasible, not claimed optimal', 'incumbent': _result(P, choice, ev), 'bound': None, 'runtime_ms': int((time.monotonic() - t0) * 1000)}


def _result(P, choice, ev):
    return dict(ev, choice=choice, objective=P['objective'], objective_value=list(objective_key(P, ev)))


def improved(P, timeout_ms=2000):
    """Depth-first branch and bound over the offer choice per task (topological order) with a lower bound and a wall-clock timeout.
    Status: optimal (search exhausted), feasible_timeout (incumbent + best open bound), infeasible (exhausted without incumbent, witness
    attached), unknown_timeout (no incumbent before the timeout)."""
    t0 = time.monotonic(); deadline = t0 + timeout_ms / 1000.0; by_task = {t['id']: t for t in P['tasks']}; order = P['order']
    opts = {t['id']: _options(P, t) for t in P['tasks']}
    for tid in order:
        if not opts[tid]:
            return {'planner': 'improved', 'status': 'infeasible', 'witness': witness(P), 'incumbent': None, 'bound': None, 'nodes': 0, 'runtime_ms': int((time.monotonic() - t0) * 1000)}
    # per-task lower bounds for the remaining-cost / remaining-time bound
    min_cost = {tid: min(by_task[tid]['quantity'] * o['price'] for o in opts[tid]) + by_task[tid]['fixed_cost'] for tid in order}
    suffix_cost = {}; acc = 0
    for tid in reversed(order):
        acc += min_cost[tid]; suffix_cost[tid] = acc
    state = {'best': None, 'best_key': None, 'nodes': 0, 'timed_out': False, 'open_bound': None}
    choice, ends, used, spent = {}, {}, {}, {}

    def bound(i, cur_cost, cur_mk):
        rest = suffix_cost[order[i]] if i < len(order) else 0
        if P['objective'] == 'lowest_cost':
            return (cur_cost + rest, 0)
        # earliest delivery: remaining chain cannot finish before the longest remaining dependency chain from current ends
        lb = cur_mk; tmp = dict(ends)
        for tid in order[i:]:
            t = by_task[tid]; s = max(t['release'], min(o['slot_start'] for o in opts[tid]), max((tmp[d] for d in t['depends']), default=0)); tmp[tid] = s + t['duration']; lb = max(lb, tmp[tid])
        return (lb, cur_cost + rest)

    def rec(i, cur_cost_by_m, cur_mk, energy):
        if time.monotonic() > deadline:
            state['timed_out'] = True; return
        state['nodes'] += 1
        if i == len(order):
            ok, ev = evaluate(P, choice)
            if ok:
                k = objective_key(P, ev)
                if state['best_key'] is None or k < state['best_key']:
                    state['best'], state['best_key'] = _result(P, dict(choice), ev), k
            return
        b = bound(i, sum(cur_cost_by_m.values()), cur_mk)
        if state['best_key'] is not None and b >= state['best_key']:
            return
        tid = order[i]; t = by_task[tid]
        cands = []
        for o in opts[tid]:
            if not t['local'] and used.get(o['id'], 0) + t['quantity'] > o['available']:
                continue
            s = _start_for(P, t, o, ends)
            if s is None:
                continue
            c = t['quantity'] * o['price'] + t['fixed_cost']; bud = P['budgets'].get(t['mission'])
            if bud is not None and cur_cost_by_m.get(t['mission'], 0) + c > bud:
                continue
            e = energy + t['quantity'] * o['energy']
            if P['energy_budget'] is not None and e > P['energy_budget'] - P['energy_reserve']:
                continue
            cands.append(((s + t['duration'], c) if P['objective'] == 'earliest_delivery' else (c, s + t['duration']), o, s, c, e))
        cands.sort(key=lambda x: x[0])
        for _, o, s, c, e in cands:
            if state['timed_out']:
                return
            if o.get('pool'):
                partial = {k: {'start_slot': ends[k] - by_task[k]['duration'], 'end_slot': ends[k]} for k in choice}
                s = _pool_start(P, by_task, choice, partial, t, o, s)
                if s is None or (t['deadline'] is not None and s + t['duration'] > t['deadline']) or (P['deadline'] is not None and s + t['duration'] > P['deadline']):
                    continue
            choice[tid] = o['id']; ends[tid] = s + t['duration']
            if not t['local']:
                used[o['id']] = used.get(o['id'], 0) + t['quantity']
            cur_cost_by_m[t['mission']] = cur_cost_by_m.get(t['mission'], 0) + c
            rec(i + 1, cur_cost_by_m, max(cur_mk, s + t['duration']), e)
            cur_cost_by_m[t['mission']] -= c
            if not t['local']:
                used[o['id']] -= t['quantity']
            del choice[tid]; del ends[tid]
        if state['timed_out'] and state['open_bound'] is None:
            state['open_bound'] = list(b)

    rec(0, {}, 0, 0)
    rt = int((time.monotonic() - t0) * 1000); root_bound = list(bound(0, 0, 0))
    if state['best'] is None:
        if state['timed_out']:
            return {'planner': 'improved', 'status': 'unknown_timeout', 'witness': None, 'incumbent': None, 'bound': root_bound, 'nodes': state['nodes'], 'runtime_ms': rt, 'timeout_ms': timeout_ms, 'note': 'no feasible schedule found before the timeout; infeasibility is not established'}
        return {'planner': 'improved', 'status': 'infeasible', 'witness': witness(P) or {'constraint': 'capacity_or_precedence_conflict', 'note': 'every relaxation passes individually; the exhaustive search found no joint assignment (shared capacity with precedence/windows)', 'offers': [{'id': o['id'], 'available': o['available'], 'demand_of_kind': sum(t['quantity'] for t in P['tasks'] if t['kind'] == o['kind'])} for o in P['offers']]}, 'incumbent': None, 'bound': root_bound, 'nodes': state['nodes'], 'runtime_ms': rt}
    if state['timed_out']:
        return {'planner': 'improved', 'status': 'feasible_timeout', 'incumbent': state['best'], 'bound': state['open_bound'] or root_bound, 'nodes': state['nodes'], 'runtime_ms': rt, 'timeout_ms': timeout_ms, 'note': 'incumbent is feasible; optimality not established (search stopped by the timeout)'}
    return {'planner': 'improved', 'status': 'optimal', 'incumbent': state['best'], 'bound': list(state['best_key']), 'nodes': state['nodes'], 'runtime_ms': rt, 'note': 'search exhausted over offer choices under the stated discretisation and the shared scheduling rule (left-shift, delayed only as far as a pool forces); the rule itself is a heuristic for pool-timed tasks'}


def oracle(P, timeout_ms=None):
    """Exhaustive enumeration of every offer choice (reference). Refuses fixtures beyond ORACLE_MAX_COMBINATIONS."""
    t0 = time.monotonic(); order = P['order']; by_task = {t['id']: t for t in P['tasks']}
    opts = [[o['id'] for o in _options(P, by_task[tid])] for tid in order]
    total = 1
    for l in opts:
        total *= max(len(l), 1)
    if total > ORACLE_MAX_COMBINATIONS:
        raise ServiceError('VALIDATION', {'code': 'oracle_bound', 'combinations': total, 'max': ORACLE_MAX_COMBINATIONS, 'note': 'the exhaustive oracle is for bounded fixtures; use the improved planner'})
    if any(not l for l in opts):
        return {'planner': 'oracle', 'status': 'infeasible', 'witness': witness(P), 'incumbent': None, 'bound': None, 'combinations': total, 'runtime_ms': int((time.monotonic() - t0) * 1000)}
    best, bk, feasible = None, None, 0
    for combo in itertools.product(*opts):
        choice = dict(zip(order, combo)); ok, ev = evaluate(P, choice)
        if ok:
            feasible += 1; k = objective_key(P, ev)
            if bk is None or k < bk:
                best, bk = _result(P, choice, ev), k
    if best is None:
        return {'planner': 'oracle', 'status': 'infeasible', 'witness': witness(P) or {'constraint': 'capacity_or_precedence_conflict', 'note': 'every single assignment violates a joint constraint'}, 'incumbent': None, 'bound': None, 'combinations': total, 'runtime_ms': int((time.monotonic() - t0) * 1000)}
    return {'planner': 'oracle', 'status': 'optimal', 'incumbent': best, 'bound': list(bk), 'combinations': total, 'feasible_combinations': feasible, 'runtime_ms': int((time.monotonic() - t0) * 1000)}


def solve(problem, planner='improved', timeout_ms=2000):
    if planner not in PLANNERS:
        raise ServiceError('VALIDATION', {'code': 'planner', 'allowed': list(PLANNERS)})
    P = normalize(problem); fn = {'baseline': baseline, 'improved': improved, 'oracle': oracle}[planner]
    out = fn(P, _int(timeout_ms, 1, 600000, 'timeout_ms'))
    out.update({'schema': 'metacoin-plan/v1', 'objective': P['objective'], 'horizon': P['horizon'], 'labels': LABELS, 'binding': False, 'tasks': len(P['tasks']), 'offers': len(P['offers'])})
    if out.get('incumbent'):
        out['incumbent']['schedule'] = {k: dict(v, start_at=P['horizon']['start'] + v['start_slot'] * P['horizon']['slot_seconds'], end_at=P['horizon']['start'] + v['end_slot'] * P['horizon']['slot_seconds']) for k, v in out['incumbent']['schedule'].items()}
    return out
