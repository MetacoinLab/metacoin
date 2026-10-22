"""Resource- and communication-aware procurement (order §12). A mission task declares a resource envelope (energy with a
protected reserve, memory, runtime, thermal permission, storage, contact windows, deadline semantics, monetary exposure);
candidate plans (local compute, remote compute via provider offers, deferred work, a reusable artifact, explicit refusal)
declare their claims in the same separate units. Ineligible plans are filtered first by hard constraints (no weighted
score can override them); only feasible alternatives are compared under a declared policy. FIFO and cheapest-eligible
baselines are provided for benchmarking. Energy claims are labelled measured or estimated; a resource receipt never
proves useful work and never reaches issuance."""
import json

from ..errors import ServiceError

ENVELOPE_FIELDS = {'energy_budget_mwh', 'energy_reserve_mwh', 'memory_bytes', 'runtime_seconds', 'thermal_permission', 'storage_bytes', 'contact_windows', 'deadline', 'max_exposure', 'asset'}
PLAN_FIELDS = {'id', 'kind', 'energy_mwh', 'energy_basis', 'memory_bytes', 'runtime_seconds', 'thermal_class', 'storage_bytes', 'delivery_contact', 'price', 'verification_cost', 'ready_at', 'fidelity', 'offer_id', 'artifact_id', 'provider_id'}
PLAN_KINDS = ('local_compute', 'remote_compute', 'deferred', 'reusable_artifact', 'refusal')
ENERGY_BASIS = ('measured', 'estimated_tdp_runtime', 'estimated_model')
THERMAL = ('nominal', 'elevated', 'peak')
DEADLINE_AT = ('execution', 'delivery', 'acceptance')
POLICIES = ('cheapest_eligible', 'earliest_delivery', 'lowest_energy', 'fifo')


def _int(v, lo, hi, name):
    if type(v) is not int or isinstance(v, bool) or not lo <= v <= hi:
        raise ServiceError('VALIDATION', {'code': name, 'allowed': 'integer %d..%d' % (lo, hi)})
    return v


def validate_envelope(env):
    if type(env) is not dict or set(env) - ENVELOPE_FIELDS:
        raise ServiceError('VALIDATION', {'code': 'resource_envelope_fields', 'allowed': sorted(ENVELOPE_FIELDS)})
    for k in ('energy_budget_mwh', 'energy_reserve_mwh', 'memory_bytes', 'runtime_seconds', 'storage_bytes', 'max_exposure'):
        _int(env.get(k, 0), 0, 10 ** 15, k)
    if env.get('energy_reserve_mwh', 0) > env.get('energy_budget_mwh', 0):
        raise ServiceError('VALIDATION', {'code': 'reserve_exceeds_budget'})
    if env.get('thermal_permission', 'nominal') not in THERMAL:
        raise ServiceError('VALIDATION', {'code': 'thermal_permission', 'allowed': list(THERMAL)})
    cw = env.get('contact_windows', [])
    if type(cw) is not list or len(cw) > 64:
        raise ServiceError('VALIDATION', {'code': 'contact_windows', 'allowed': 'list of {id, start, end, capacity_bytes} (explicit supplied scenario, never invented ephemerides)'})
    for w in cw:
        if type(w) is not dict or set(w) != {'id', 'start', 'end', 'capacity_bytes'} or type(w['id']) is not str:
            raise ServiceError('VALIDATION', {'code': 'contact_window'})
        _int(w['start'], 0, 10 ** 12, 'start'); _int(w['end'], 0, 10 ** 12, 'end'); _int(w['capacity_bytes'], 0, 10 ** 15, 'capacity_bytes')
        if w['end'] < w['start']:
            raise ServiceError('VALIDATION', {'code': 'contact_window_order'})
    dl = env.get('deadline')
    if dl is not None:
        if type(dl) is not dict or set(dl) != {'at', 'time'} or dl['at'] not in DEADLINE_AT:
            raise ServiceError('VALIDATION', {'code': 'deadline', 'allowed': '{at: execution|delivery|acceptance, time: int}'})
        _int(dl['time'], 0, 10 ** 12, 'deadline_time')
    return env


def validate_plan(p):
    if type(p) is not dict or set(p) - PLAN_FIELDS or type(p.get('id')) is not str or p.get('kind') not in PLAN_KINDS:
        raise ServiceError('VALIDATION', {'code': 'plan', 'allowed': sorted(PLAN_FIELDS), 'kinds': list(PLAN_KINDS)})
    for k in ('energy_mwh', 'memory_bytes', 'runtime_seconds', 'storage_bytes', 'price', 'verification_cost', 'ready_at'):
        _int(p.get(k, 0), 0, 10 ** 15, k)
    if p.get('energy_basis', 'estimated_model') not in ENERGY_BASIS:
        raise ServiceError('VALIDATION', {'code': 'energy_basis', 'allowed': list(ENERGY_BASIS), 'note': 'TDP x runtime is an estimate, not a measurement'})
    if p.get('thermal_class', 'nominal') not in THERMAL:
        raise ServiceError('VALIDATION', {'code': 'thermal_class', 'allowed': list(THERMAL)})
    if p.get('fidelity', 'full') not in ('full', 'reduced'):
        raise ServiceError('VALIDATION', {'code': 'fidelity', 'allowed': ['full', 'reduced'], 'note': 'a reduced-fidelity result is a distinct product with its own terms and price'})
    return p


def eligibility(envelope, plan, now_ts, allow_reduced=False):
    """Hard constraints, each in its own unit. Returns a list of structured violations (empty = eligible)."""
    v = []
    if plan['kind'] == 'refusal':
        return v
    usable = envelope.get('energy_budget_mwh', 0) - envelope.get('energy_reserve_mwh', 0)
    if plan.get('energy_mwh', 0) > usable:
        v.append({'code': 'energy_reserve_violation', 'claimed_mwh': plan.get('energy_mwh', 0), 'usable_above_reserve_mwh': usable, 'basis': plan.get('energy_basis', 'estimated_model')})
    if envelope.get('memory_bytes') and plan.get('memory_bytes', 0) > envelope['memory_bytes']:
        v.append({'code': 'memory_exceeded', 'claimed': plan.get('memory_bytes', 0), 'limit': envelope['memory_bytes']})
    if envelope.get('runtime_seconds') and plan.get('runtime_seconds', 0) > envelope['runtime_seconds']:
        v.append({'code': 'runtime_exceeded', 'claimed': plan.get('runtime_seconds', 0), 'limit': envelope['runtime_seconds']})
    if THERMAL.index(plan.get('thermal_class', 'nominal')) > THERMAL.index(envelope.get('thermal_permission', 'nominal')):
        v.append({'code': 'thermal_permission_missing', 'needed': plan.get('thermal_class'), 'permitted': envelope.get('thermal_permission', 'nominal')})
    if envelope.get('storage_bytes') and plan.get('storage_bytes', 0) > envelope['storage_bytes']:
        v.append({'code': 'storage_exceeded', 'claimed': plan.get('storage_bytes', 0), 'limit': envelope['storage_bytes']})
    exposure = plan.get('price', 0) + plan.get('verification_cost', 0)
    if envelope.get('max_exposure') is not None and exposure > envelope['max_exposure']:
        v.append({'code': 'exposure_exceeded', 'exposure': exposure, 'max_exposure': envelope['max_exposure']})
    if plan.get('fidelity', 'full') == 'reduced' and not allow_reduced:
        v.append({'code': 'reduced_fidelity_not_allowed'})
    finish = max(plan.get('ready_at', 0), now_ts) + plan.get('runtime_seconds', 0)
    delivery_time = finish
    if plan['kind'] == 'remote_compute' or plan.get('delivery_contact'):
        wins = envelope.get('contact_windows', [])
        if plan.get('delivery_contact'):
            w = next((x for x in wins if x['id'] == plan['delivery_contact']), None)
            if w is None:
                v.append({'code': 'contact_window_unknown', 'contact': plan['delivery_contact']})
            elif w['end'] < finish:
                v.append({'code': 'contact_window_missed', 'contact': w['id'], 'window_end': w['end'], 'ready_at': finish, 'note': 'missed windows defer or replan by policy; nothing retries spending'})
            elif w['capacity_bytes'] < plan.get('storage_bytes', 0):
                v.append({'code': 'contact_capacity_exceeded', 'contact': w['id'], 'capacity_bytes': w['capacity_bytes'], 'needed': plan.get('storage_bytes', 0)})
            else:
                delivery_time = max(finish, w['start'])
        elif wins:
            nxt = [w for w in wins if w['end'] >= finish]
            if not nxt:
                v.append({'code': 'no_contact_window_after_completion', 'ready_at': finish})
            else:
                delivery_time = max(finish, min(w['start'] for w in nxt))
    dl = envelope.get('deadline')
    if dl:
        t = finish if dl['at'] == 'execution' else delivery_time
        if t > dl['time']:
            v.append({'code': 'deadline_missed', 'at': dl['at'], 'deadline': dl['time'], 'projected': t})
    return v


def plan(envelope, plans, policy='cheapest_eligible', now_ts=0, allow_reduced=False, arrival_order=None):
    """Filter, then compare under the declared policy. Returns the selection with every reason preserved."""
    validate_envelope(envelope)
    if policy not in POLICIES:
        raise ServiceError('VALIDATION', {'code': 'policy', 'allowed': list(POLICIES)})
    if type(plans) is not list or not 1 <= len(plans) <= 64:
        raise ServiceError('VALIDATION', {'code': 'plans', 'allowed': '1..64 candidate plans'})
    rows = []
    for p in plans:
        validate_plan(p)
        viol = eligibility(envelope, p, now_ts, allow_reduced)
        finish = max(p.get('ready_at', 0), now_ts) + p.get('runtime_seconds', 0)
        rows.append({'plan': p, 'eligible': not viol, 'violations': viol, 'exposure': p.get('price', 0) + p.get('verification_cost', 0), 'projected_completion': finish, 'energy_mwh': p.get('energy_mwh', 0)})
    feasible = [r for r in rows if r['eligible'] and r['plan']['kind'] != 'refusal']
    refusal = next((r for r in rows if r['plan']['kind'] == 'refusal'), None)
    if not feasible:
        return {'schema': 'metacoin-procurement-plan/v0', 'policy': policy, 'selected': None, 'outcome': 'NO_FEASIBLE_PLAN', 'spend': 0, 'candidates': rows, 'refusal': refusal['plan'] if refusal else {'id': 'implicit_refusal', 'kind': 'refusal'},
                'explanation': 'every candidate violates a hard constraint (energy reserve, memory, runtime, thermal permission, storage, contact window, deadline or exposure); the informative outcome is to decline or defer, not to spend', 'units': 'energy mWh; memory/storage bytes; runtime s; money in the envelope asset; no cross-unit score'}
    key = {'cheapest_eligible': lambda r: (r['exposure'], r['projected_completion'], r['plan']['id']), 'earliest_delivery': lambda r: (r['projected_completion'], r['exposure'], r['plan']['id']), 'lowest_energy': lambda r: (r['energy_mwh'], r['exposure'], r['plan']['id']),
           'fifo': lambda r: ((arrival_order or [p['id'] for p in plans]).index(r['plan']['id']), r['plan']['id'])}[policy]
    chosen = sorted(feasible, key=key)[0]
    return {'schema': 'metacoin-procurement-plan/v0', 'policy': policy, 'selected': chosen['plan'], 'outcome': 'SELECTED', 'spend': chosen['exposure'], 'projected_completion': chosen['projected_completion'], 'candidates': rows,
            'explanation': 'hard constraints filtered %d of %d candidates; the %s policy ordered the %d feasible ones' % (len(rows) - len(feasible), len(rows), policy, len(feasible)), 'units': 'energy mWh; memory/storage bytes; runtime s; money in the envelope asset; no cross-unit score'}


def offer_claims_eligibility(envelope, claims, now_ts):
    """Board hook: an offer's declared resource claims against the terms' envelope (structured reasons for the eligibility list)."""
    if envelope is None or claims is None:
        return []
    p = dict({'id': 'offer', 'kind': 'remote_compute'}, **{k: v for k, v in claims.items() if k in PLAN_FIELDS})
    validate_plan(p)
    return eligibility(envelope, p, now_ts)


def benchmark(seed=1, missions=200):
    """Held-out synthetic workload: identical missions and candidate sets under the procurement policy and the FIFO / cheapest
    baselines that ignore the envelope. Reports accepted products, spending, verification cost, resource violations and pending
    exposure. A policy that 'wins' by ignoring refused missions is counted as violating, not as winning."""
    from ..lunar.scenario import splitmix
    rng = splitmix(seed)
    def r(n):
        return next(rng) % n
    out = {}
    workload = []
    for i in range(missions):
        env = {'energy_budget_mwh': 1000 + r(2000), 'energy_reserve_mwh': 300, 'memory_bytes': (2 + r(6)) << 30, 'runtime_seconds': 300 + r(3000), 'thermal_permission': THERMAL[r(3)], 'storage_bytes': (1 + r(8)) << 20, 'max_exposure': 5 + r(20), 'asset': 'dev:dom-a:ledger-dom-a:TMETA:0',
               'contact_windows': [{'id': 'w1', 'start': 1000 + r(2000), 'end': 4000 + r(3000), 'capacity_bytes': (2 + r(6)) << 20}], 'deadline': {'at': DEADLINE_AT[r(3)], 'time': 3000 + r(5000)}}
        plans = []
        for j in range(3 + r(4)):
            kind = PLAN_KINDS[r(4)]
            plans.append({'id': 'p%d' % j, 'kind': kind, 'energy_mwh': r(1500) if kind != 'reusable_artifact' else 0, 'energy_basis': ENERGY_BASIS[r(3)], 'memory_bytes': (1 + r(8)) << 30, 'runtime_seconds': r(2500) if kind != 'reusable_artifact' else 10, 'thermal_class': THERMAL[r(3)],
                          'storage_bytes': (1 + r(8)) << 20, 'delivery_contact': 'w1' if kind == 'remote_compute' else None, 'price': 1 + r(25) if kind != 'reusable_artifact' else r(3), 'verification_cost': r(4), 'ready_at': r(2000) if kind == 'deferred' else 0, 'fidelity': 'full'})
        plans.append({'id': 'decline', 'kind': 'refusal'})
        workload.append((env, plans))
    for name in ('cheapest_eligible', 'earliest_delivery', 'fifo_ignoring_envelope', 'cheapest_ignoring_envelope'):
        acc = spend = ver = viol = pending = declined = 0
        for env, plans in workload:
            if name in ('cheapest_eligible', 'earliest_delivery'):
                res = plan(env, plans, name, now_ts=0)
                if res['selected'] is None:
                    declined += 1; continue
                chosen = res['selected']
            else:
                cands = [p for p in plans if p['kind'] != 'refusal']
                chosen = cands[0] if name.startswith('fifo') else sorted(cands, key=lambda p: (p.get('price', 0) + p.get('verification_cost', 0), p['id']))[0]
            v = eligibility(env, chosen, 0)
            spend += chosen.get('price', 0); ver += chosen.get('verification_cost', 0)
            if v:
                viol += 1; pending += chosen.get('price', 0)                    # a violating plan cannot be accepted: its spend stays exposed
            else:
                acc += 1
        out[name] = {'missions': missions, 'accepted_products': acc, 'declined_no_feasible_plan': declined, 'total_spend': spend, 'verification_cost': ver, 'resource_violations': viol, 'pending_exposure': pending, 'spend_per_accepted': round(spend / acc, 2) if acc else None}
    return {'schema': 'metacoin-procurement-benchmark/v0', 'seed': seed, 'workload': 'synthetic held-out missions (identical across policies); not a mission ephemeris, not external demand', 'results': out,
            'reading': 'baselines that ignore the envelope spend on plans that violate hard constraints; those missions are counted as violations with pending exposure, never as successes'}
