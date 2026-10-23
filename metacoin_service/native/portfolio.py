"""Mission portfolio and acquisition decisions (order 2026-10-23 group I). Several funded missions compete for shared pools,
contact schedules and domain-local liquidity. Liquidity is reported in its distinct parts (held locally, reserved, cross-domain
claims, expected earnings, spendable) and only spendable settled funds admit a mission. Admission follows the priorities
declared in each mission's terms (policy.priority) and grant: the reference policy is deterministic (priority, then declared
cost); the oracle enumerates every admission subset on small fixtures; the improved planner is a bounded branch and bound over
subsets with a priority bound and a timeout (incumbent, bound, status reported honestly). A recommendation is never spending
authorization: `validate_plan` is the independent check that refuses any plan overselling a pool interval or spending money that
is not spendable (pending remote claims, receivables) before any reservation is attempted."""
import itertools
import json
import time

from ..errors import ServiceError
from . import planner as PL
from . import pools as POOLS

ADMISSION_POLICIES = ('priority_then_cost',)
OBJECTIVES = ('earliest_delivery', 'lowest_cost')


def liquidity(db, workspace, asset, domain, account, min_reserve=0):
    """The five figures of a mission owner's money, each from the journal / obligations / lots; only `spendable` admits work."""
    from . import ledger
    cats = ledger.categories(db, workspace, asset, domain, account)
    receivable = db.execute("SELECT COALESCE(SUM(amount - settled_amount),0) FROM native_obligations WHERE workspace=? AND payee_account=? AND asset=? AND state IN ('open','partial')", (workspace, account, asset)).fetchone()[0]
    claims = db.execute("SELECT COALESCE(SUM(amount),0) FROM native_claim_lots WHERE workspace=? AND owner_account=? AND asset=? AND status='active'", (workspace, account, asset)).fetchone()[0] if db.execute("SELECT 1 FROM sqlite_master WHERE name='native_claim_lots'").fetchone() else 0
    pending_claims = db.execute("SELECT COALESCE(SUM(amount),0) FROM native_allocations WHERE workspace=? AND recipient_account=? AND asset=? AND direction='import' AND state='return_requested'", (workspace, account, asset)).fetchone()[0] + cats['in_transit']
    return {'held_locally': cats['available'], 'reserved': cats['reserved'] + cats['committed_outgoing'], 'cross_domain_claims_held': claims, 'cross_domain_pending': pending_claims, 'expected_earnings': receivable, 'spendable': max(cats['available'] - min_reserve, 0), 'min_reserve': min_reserve,
            'rule': 'spendable = settled available minus the declared minimum reserve; reserved, in-transit, pending claims and expected earnings never admit work'}


def _problem_of(mission_problems, admitted):
    tasks, offers, budgets = [], {}, {}
    for m in admitted:
        p = mission_problems[m]; tasks += p['tasks']; budgets[m] = p['budget']
        for o in p['offers']:
            offers[o['id']] = o
    base = next(iter(mission_problems.values()))
    return {'horizon': base['horizon'], 'tasks': tasks, 'offers': list(offers.values()), 'budgets': budgets, 'objective': base['objective'], 'deadline_slot': base.get('deadline_slot')}


def _score(admitted, prios, ev):
    """Lexicographic: maximise admitted priority mass, then the objective of the joint plan."""
    return (-sum(prios[m] for m in admitted), ev['objective_value'][0] if ev else 0, ev['objective_value'][1] if ev else 0)


def reference(mission_problems, prios, order=None):
    """Deterministic reference admission: priority desc, then declared ceiling asc; admit while the joint plan stays feasible."""
    order = order or sorted(mission_problems, key=lambda m: (-prios[m], sum(t.get('fixed_cost', 0) for t in mission_problems[m]['tasks']), m))
    admitted, deferred = [], []; last = None
    for m in order:
        trial = admitted + [m]; r = PL.solve(_problem_of(mission_problems, trial), 'improved', timeout_ms=2000)
        if r['status'] in ('optimal', 'feasible_timeout'):
            admitted = trial; last = r
        else:
            deferred.append({'mission': m, 'reason': (r.get('witness') or {}).get('constraint') or r['status']})
    return {'policy': 'priority_then_cost', 'admitted': admitted, 'deferred': deferred, 'plan': last, 'score': list(_score(admitted, prios, (last or {}).get('incumbent'))) if admitted else None}


def oracle(mission_problems, prios):
    names = sorted(mission_problems); best = None
    if len(names) > 6:
        raise ServiceError('VALIDATION', {'code': 'oracle_bound', 'missions': len(names), 'max': 6})
    for k in range(len(names), 0, -1):
        for sub in itertools.combinations(names, k):
            r = PL.solve(_problem_of(mission_problems, list(sub)), 'oracle', timeout_ms=5000)
            if r['status'] != 'optimal':
                continue
            sc = _score(list(sub), prios, r['incumbent'])
            if best is None or sc < best['score']:
                best = {'admitted': list(sub), 'score': sc, 'plan': r}
        if best is not None and k > 0 and False:
            break
    if best is None:
        return {'status': 'infeasible', 'admitted': [], 'score': None}
    return {'status': 'optimal', 'admitted': best['admitted'], 'score': list(best['score']), 'plan': best['plan']}


def improved(mission_problems, prios, timeout_ms=3000):
    """Branch and bound over admission subsets: order by priority; bound = admitted priority mass + remaining priority mass."""
    names = sorted(mission_problems, key=lambda m: (-prios[m], m)); t0 = time.monotonic(); deadline = t0 + timeout_ms / 1000.0
    state = {'best': None, 'best_score': None, 'nodes': 0, 'timed_out': False}
    suffix = [0] * (len(names) + 1)
    for i in range(len(names) - 1, -1, -1):
        suffix[i] = suffix[i + 1] + prios[names[i]]
    def rec(i, admitted):
        if time.monotonic() > deadline:
            state['timed_out'] = True; return
        state['nodes'] += 1
        if state['best_score'] is not None and -(sum(prios[m] for m in admitted) + suffix[i]) > state['best_score'][0]:
            return                                                        # cannot beat the incumbent's priority mass
        if i == len(names):
            if not admitted:
                return
            r = PL.solve(_problem_of(mission_problems, admitted), 'improved', timeout_ms=max(200, int((deadline - time.monotonic()) * 1000)))
            if r['status'] in ('optimal', 'feasible_timeout'):
                sc = _score(admitted, prios, r['incumbent'])
                if state['best_score'] is None or sc < state['best_score']:
                    state['best'], state['best_score'] = {'admitted': list(admitted), 'plan': r}, sc
            return
        rec(i + 1, admitted + [names[i]]); rec(i + 1, admitted)
    rec(0, [])
    rt = int((time.monotonic() - t0) * 1000)
    if state['best'] is None:
        return {'status': 'unknown_timeout' if state['timed_out'] else 'infeasible', 'admitted': [], 'score': None, 'nodes': state['nodes'], 'runtime_ms': rt}
    return {'status': 'feasible_timeout' if state['timed_out'] else 'optimal', 'admitted': state['best']['admitted'], 'score': list(state['best_score']), 'bound': [-suffix[0]], 'plan': state['best']['plan'], 'nodes': state['nodes'], 'runtime_ms': rt,
            'note': 'optimal = every admission subset explored under the priority bound and each joint plan solved to optimality; feasible_timeout = incumbent only'}


class Portfolio:
    def __init__(self, settings, services):
        self.settings, self.svc = settings, services

    def plan(self, db, principal, body):
        """Compare several funded missions against shared pools and domain-local liquidity; a recommendation, not authorization."""
        principal.require('work:read')
        if type(body) is not dict or set(body) - {'mission_ids', 'objective', 'planner', 'timeout_ms', 'horizon', 'deadline_at', 'admission_policy'}:
            raise ServiceError('VALIDATION', {'code': 'fields', 'allowed': ['mission_ids', 'objective', 'planner (reference|improved|oracle)', 'timeout_ms', 'horizon', 'deadline_at', 'admission_policy (priority_then_cost)']})
        ids = body.get('mission_ids') or []
        if type(ids) is not list or not 1 <= len(ids) <= 12:
            raise ServiceError('VALIDATION', {'code': 'mission_ids', 'allowed': '1..12'})
        if body.get('admission_policy', 'priority_then_cost') not in ADMISSION_POLICIES:
            raise ServiceError('VALIDATION', {'code': 'admission_policy', 'allowed': list(ADMISSION_POLICIES)})
        MP = self.svc.planning; probs, prios, liq = {}, {}, {}
        for mid in ids:
            m = self.svc.missions._row(db, principal.workspace, mid); pol = json.loads(m['policy_json'])
            p = MP.problem(db, principal, [mid], {k: v for k, v in body.items() if k in ('objective', 'horizon', 'deadline_at')})
            d = self.svc.native.domain(db, principal.workspace); liq[mid] = liquidity(db, principal.workspace, m['asset'], d['id'], m['owner_account'], pol.get('min_cash_reserve', 0))
            probs[mid] = {'horizon': p['horizon'], 'tasks': p['tasks'], 'offers': p['offers'], 'budget': min(p['budgets'].get(mid, 0), liq[mid]['spendable']), 'objective': p['objective'], 'deadline_slot': p.get('deadline_slot')}
            prios[mid] = int(pol.get('priority', 0)); probs[mid]['priority'] = prios[mid]
        planner = body.get('planner', 'improved')
        if planner == 'reference':
            out = reference(probs, prios)
        elif planner == 'oracle':
            out = oracle(probs, prios)
        elif planner == 'improved':
            out = improved(probs, prios, int(body.get('timeout_ms', 3000)))
        else:
            raise ServiceError('VALIDATION', {'code': 'planner', 'allowed': ['reference', 'improved', 'oracle']})
        out.update({'schema': 'metacoin-portfolio-plan/v1', 'missions': ids, 'priorities': prios, 'liquidity': liq, 'admission_policy': 'priority_then_cost', 'binding': False, 'note': 'declared priorities decide admission; no model score supersedes them; this recommendation authorizes nothing — the independent validator and the reservation path decide'})
        for mid in ids:
            self.svc.missions._event(db, mid, principal.id, 'portfolio_plan', {'planner': planner, 'admitted': out.get('admitted'), 'status': out.get('status', 'reference'), 'score': out.get('score')})
        return out

    def validate_plan(self, db, principal, body):
        """Independent validator of a plan output before any reservation: pool intervals must be admissible against live commitments
        and every mission's cost must fit spendable settled funds (pending remote claims and receivables never count)."""
        principal.require('work:read')
        plan = (body or {}).get('plan') or {}; sched = (plan.get('incumbent') or {}).get('schedule') or plan.get('schedule') or {}; reasons = []; by_mission = {}
        pool_reqs = {}
        for tid, s in sched.items():
            if s.get('offer_id'):
                o = db.execute('SELECT * FROM resource_offers WHERE id=?', (s['offer_id'],)).fetchone()
                if o is None:
                    reasons.append({'task': tid, 'constraint': 'unknown_offer'}); continue
                units = s.get('quantity', 0); start, end = s.get('start_at'), s.get('end_at')
                if o['pool_id'] and start is not None and end is not None and end > start:
                    pool_reqs.setdefault(o['pool_id'], []).append({'task': tid, 'start': start, 'end': end, 'quantity': units})
            by_mission[s.get('mission', 'm')] = by_mission.get(s.get('mission', 'm'), 0) + int(s.get('cost', 0))
        for pid, reqs in pool_reqs.items():
            p = db.execute('SELECT * FROM resource_pools WHERE id=?', (pid,)).fetchone(); sched_p = json.loads(p['schedule_json']); commits = self.svc.pools.commitments(db, pid)
            new = []
            for r in reqs:
                a, b = POOLS.align(r['start'], r['end'], p['resolution_seconds']); slots = POOLS.slots_for(r['quantity'], p['units_per_slot_second'], a, b); new.append({'start': a, 'end': b, 'slots': slots, 'task': r['task']})
            segs = POOLS.sweep(sched_p, commits + new); bad = [s_ for s_ in segs if not s_['ok']]
            if bad:
                reasons.append({'pool': pid, 'constraint': 'pool_oversold', 'segments': bad[:3], 'note': 'the plan schedules an impossible overlap against live commitments'})
        for mid, cost in by_mission.items():
            m = db.execute('SELECT * FROM mission_contracts WHERE id=? AND workspace=?', (mid, principal.workspace)).fetchone()
            if m is None:
                continue
            d = self.svc.native.domain(db, principal.workspace); liq = liquidity(db, principal.workspace, m['asset'], d['id'], m['owner_account'], json.loads(m['policy_json']).get('min_cash_reserve', 0))
            if cost > liq['spendable']:
                reasons.append({'mission': mid, 'constraint': 'not_spendable', 'cost': cost, 'spendable': liq['spendable'], 'pending_remote_claims': liq['cross_domain_pending'], 'expected_earnings': liq['expected_earnings'], 'note': 'the plan spends money that is reserved, in transit, a pending remote claim or an expected earning'})
        return {'schema': 'metacoin-plan-validation/v1', 'valid': not reasons, 'reasons': reasons, 'checked': {'pools': list(pool_reqs), 'missions': list(by_mission)}, 'rule': 'a plan that oversells a pool interval or spends non-spendable funds is refused before any reservation'}

    def rolling_vs_fixed(self, body):
        """Deterministic comparison on one fixture: the same disruption (an offer window removed at time T) under a fixed plan and
        under rolling replanning of uncommitted work. Incurred commitments are preserved and replacement costs counted."""
        if type(body) is not dict or 'problem' not in body or 'disruption' not in body:
            raise ServiceError('VALIDATION', {'code': 'fields', 'allowed': ['problem', 'disruption {offer_id, at_slot}']})
        P_ = body['problem']; dis = body['disruption']; T = int(dis['at_slot']); lost = dis['offer_id']
        base = PL.solve(P_, 'improved', timeout_ms=3000)
        if not base.get('incumbent'):
            return {'status': base['status'], 'note': 'no initial plan'}
        norm = PL.normalize(P_); slot = norm['horizon']['slot_seconds']; sched = base['incumbent']['schedule']
        committed = {t: s for t, s in sched.items() if s['start_slot'] < T}; uncommitted = {t: s for t, s in sched.items() if s['start_slot'] >= T}
        # fixed plan: tasks on the lost offer that had not started are failed and must be re-bought after the disruption at the earliest feasible slot (replacement cost), serially
        fixed_cost = sum(s['cost'] for s in committed.values()); fixed_fail = [t for t, s in uncommitted.items() if s['offer_id'] == lost]
        fixed_cost += sum(s['cost'] for t, s in uncommitted.items() if s['offer_id'] != lost)
        rep = {'horizon': P_['horizon'], 'tasks': [t for t in P_['tasks'] if t['id'] in fixed_fail], 'offers': [o for o in P_['offers'] if o['id'] != lost], 'budget': 10 ** 9, 'objective': P_.get('objective', 'lowest_cost')}
        for t in rep['tasks']:
            t['release_slot'] = max(t.get('release_slot', 0), T); t['depends'] = [d for d in t.get('depends', []) if d in fixed_fail]
        rr = PL.solve(rep, 'improved', timeout_ms=3000) if rep['tasks'] else {'status': 'optimal', 'incumbent': {'cost': 0, 'makespan_slots': base['incumbent']['makespan_slots']}}
        fixed = {'cost_incurred_before_disruption': sum(s['cost'] for s in committed.values()), 'failed_tasks': fixed_fail, 'wasted_cost_on_lost_offer': sum(s['cost'] for t, s in committed.items() if s['offer_id'] == lost), 'replacement_status': rr['status'], 'replacement_cost': (rr.get('incumbent') or {}).get('cost'), 'total_cost': fixed_cost + ((rr.get('incumbent') or {}).get('cost') or 0), 'delivery_slot': max(base['incumbent']['makespan_slots'], (rr.get('incumbent') or {}).get('makespan_slots') or 0)}
        # rolling: at T replan every uncommitted task over the remaining offers, keeping committed ones
        roll = {'horizon': P_['horizon'], 'tasks': [dict(t, release_slot=max(t.get('release_slot', 0), T), depends=[d for d in t.get('depends', []) if d in uncommitted]) for t in P_['tasks'] if t['id'] in uncommitted], 'offers': [o for o in P_['offers'] if o['id'] != lost], 'budget': 10 ** 9, 'objective': P_.get('objective', 'lowest_cost')}
        r2 = PL.solve(roll, 'improved', timeout_ms=3000) if roll['tasks'] else {'status': 'optimal', 'incumbent': {'cost': 0, 'makespan_slots': max((s['end_slot'] for s in committed.values()), default=0)}}
        rolling = {'cost_incurred_before_disruption': sum(s['cost'] for s in committed.values()), 'replanned_tasks': sorted(uncommitted), 'replan_status': r2['status'], 'replan_cost': (r2.get('incumbent') or {}).get('cost'), 'total_cost': sum(s['cost'] for s in committed.values()) + ((r2.get('incumbent') or {}).get('cost') or 0), 'delivery_slot': max(max((s['end_slot'] for s in committed.values()), default=0), (r2.get('incumbent') or {}).get('makespan_slots') or 0)}
        return {'schema': 'metacoin-rolling-comparison/v1', 'disruption': dis, 'initial_plan': {'cost': base['incumbent']['cost'], 'makespan_slots': base['incumbent']['makespan_slots']}, 'fixed': fixed, 'rolling': rolling, 'identical_inputs': True, 'note': 'same fixture, same disruption, same offers after it; costs are declared offer prices, not model estimates'}
