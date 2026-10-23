"""Scenario comparison: the cost of extra evidence versus delay under fixed, declared assumptions (order 2026-10-23 §21 extension 8).
Every number is a model estimate under the assumptions passed in; the result says so in every row and never presents an
estimate as a guarantee. Integer arithmetic in basis points; no randomness."""

FIELDS = {'p_feasible_bps', 'evidence_price', 'evidence_delay_slots', 'evidence_reliability_bps', 'followon_price', 'followon_delay_slots', 'delay_cost_per_slot', 'deadline_slots', 'value_if_feasible', 'loss_if_infeasible_proceeded'}


def validate(a):
    if type(a) is not dict or set(a) - FIELDS or not FIELDS <= set(a):
        raise ValueError('assumptions need exactly: ' + ', '.join(sorted(FIELDS)))
    for k, v in a.items():
        if type(v) is not int or isinstance(v, bool) or v < 0:
            raise ValueError('%s must be a non-negative integer' % k)
    if a['p_feasible_bps'] > 10000 or a['evidence_reliability_bps'] > 10000:
        raise ValueError('probabilities in basis points 0..10000')
    return a


def compare(a):
    """Three courses of action over one decision: proceed now, buy evidence first, or wait for the deadline; expected net value and
    delay per course under the declared assumptions (expected values are linear combinations; nothing is sampled)."""
    a = validate(a); p = a['p_feasible_bps']; q = a['evidence_reliability_bps']; D = a['delay_cost_per_slot']
    def ev(num_bps, value):
        return num_bps * value // 10000
    proceed = {'course': 'proceed_without_evidence', 'cost': a['followon_price'] + D * a['followon_delay_slots'], 'expected_value': ev(p, a['value_if_feasible']) - ev(10000 - p, a['loss_if_infeasible_proceeded']), 'delay_slots': a['followon_delay_slots'], 'meets_deadline': a['followon_delay_slots'] <= a['deadline_slots']}
    # evidence: with reliability q the evidence reveals the true state; with 1-q it is uninformative and the decision proceeds as without evidence
    informed_value = ev(p, a['value_if_feasible'])                                                   # when feasible: proceed and gain; when infeasible: stop and avoid the loss
    evidence = {'course': 'buy_evidence_then_decide', 'cost': a['evidence_price'] + D * (a['evidence_delay_slots'] + a['followon_delay_slots']) + ev(p, a['followon_price']), 'expected_value': ev(q, informed_value) + ev(10000 - q, proceed['expected_value']), 'delay_slots': a['evidence_delay_slots'] + a['followon_delay_slots'], 'meets_deadline': a['evidence_delay_slots'] + a['followon_delay_slots'] <= a['deadline_slots']}
    wait = {'course': 'wait_for_deadline', 'cost': D * a['deadline_slots'], 'expected_value': 0, 'delay_slots': a['deadline_slots'], 'meets_deadline': True}
    rows = []
    for c in (proceed, evidence, wait):
        rows.append(dict(c, net=c['expected_value'] - c['cost'], label='model estimate under the declared assumptions; not a guarantee'))
    best = max(rows, key=lambda r: (r['net'], r['meets_deadline'], -r['delay_slots']))
    # sensitivity: the feasibility probability at which proceeding and buying evidence break even (scan in 1 % steps)
    be = None
    for bps in range(0, 10001, 100):
        r = compare_at(dict(a, p_feasible_bps=bps)) if bps != p else None
        if r and r['proceed_net'] >= r['evidence_net']:
            be = bps; break
    return {'schema': 'metacoin-evidence-scenarios/v1', 'assumptions': a, 'rows': rows, 'recommended_under_assumptions': best['course'], 'breakeven_p_feasible_bps_for_proceeding': be, 'caveat': 'expected values under fixed declared assumptions (probabilities and prices supplied by the caller); synthetic, not measured; a plan built on them is not spending authorization'}


def compare_at(a):
    p = a['p_feasible_bps']; q = a['evidence_reliability_bps']; D = a['delay_cost_per_slot']; ev = lambda b, v: b * v // 10000
    proceed_net = ev(p, a['value_if_feasible']) - ev(10000 - p, a['loss_if_infeasible_proceeded']) - (a['followon_price'] + D * a['followon_delay_slots'])
    inf = ev(p, a['value_if_feasible']); proceed_ev = ev(p, a['value_if_feasible']) - ev(10000 - p, a['loss_if_infeasible_proceeded'])
    evidence_net = ev(q, inf) + ev(10000 - q, proceed_ev) - (a['evidence_price'] + D * (a['evidence_delay_slots'] + a['followon_delay_slots']) + ev(p, a['followon_price']))
    return {'proceed_net': proceed_net, 'evidence_net': evidence_net}
