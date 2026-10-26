"""Shared communication-route planning across several relay offers (order 2026-10-23 §21 extension 3): bounded enumeration of hop
sequences over candidate contact offers (distinct relay accounts, hop count ≤ max_hops ≤ 4), fees recomputed per hop exactly as
the relay service bills them (whole KiB × price per unit), ranked deterministically (total fee, hops, latest window end). The
exposure of a chosen route is checked by an independent function that re-derives every hop fee from the offers and refuses a
plan above the ceiling before any reservation exists."""
import itertools

MAX_OFFERS = 12


def kib_of(nbytes):
    return max(1, -(-nbytes // 1024))


def hop_fee(offer, nbytes):
    return kib_of(nbytes) * offer['price_per_unit']


def _win(offer):
    w = offer.get('window')
    if isinstance(w, dict):
        return w['start'], w['end']
    return offer['window_start'], offer['window_end']


def feasible(offer, now, nbytes):
    bw = offer.get('bandwidth_bytes_per_s') or (offer.get('meta') or {}).get('bandwidth_bytes_per_s') or 0; need = -(-nbytes // bw) if bw else 1; ws, we = _win(offer)
    return offer['kind'] == 'contact' and offer.get('state', 'open') == 'open' and ws <= now and we >= now + need and offer.get('available', offer.get('capacity', 0)) >= kib_of(nbytes)


def plan(offers, nbytes, max_hops, fee_ceiling, now, recipient_account=None, exclude_accounts=(), sender_account=None, limit=5):
    """Ranked candidate routes. Every route is a list of hops {offer_id, offer_version, relay_account, fee}; a relay account appears
    at most once; the recipient and the sender never relay their own message."""
    if type(max_hops) is not int or not 1 <= max_hops <= 4:
        raise ValueError('max_hops 1..4')
    cands = [o for o in offers if feasible(o, now, nbytes) and o['provider_account'] not in set(exclude_accounts) | {recipient_account, sender_account}][:MAX_OFFERS]
    by_acct = {}
    for o in cands:
        by_acct.setdefault(o['provider_account'], []).append(o)
    best_per_acct = {a: min(os_, key=lambda o: (hop_fee(o, nbytes), -_win(o)[1], o['id'])) for a, os_ in by_acct.items()}     # one offer per relay account: the cheapest, then the longest window
    routes = []
    for n in range(1, max_hops + 1):
        for seq in itertools.permutations(sorted(best_per_acct), n):
            hops = [best_per_acct[a] for a in seq]; fee = sum(hop_fee(o, nbytes) for o in hops)
            if fee <= fee_ceiling:
                routes.append({'hops': [{'offer_id': o['id'], 'offer_version': o['version'], 'relay_account': o['provider_account'], 'fee': hop_fee(o, nbytes), 'window_end': _win(o)[1]} for o in hops], 'total_fee': fee, 'hop_count': n, 'window_closes_at': min(_win(o)[1] for o in hops)})
    routes.sort(key=lambda r: (r['total_fee'], r['hop_count'], -r['window_closes_at'], [h['offer_id'] for h in r['hops']]))
    return {'schema': 'metacoin-route-plan/v1', 'bytes': nbytes, 'kib': kib_of(nbytes), 'fee_ceiling': fee_ceiling, 'candidates_considered': len(cands), 'relay_accounts': len(best_per_acct), 'enumerated': sum(1 for _ in ()) or len(routes), 'routes': routes[:limit], 'bound': 'exhaustive over the cheapest offer of each relay account, hop count ≤ %d; not a guarantee of delivery' % max_hops,
            'note': 'a plan reserves nothing: create the route with POST /relay/routes using the hops of one candidate; the exposure check below re-derives every fee from the offers'}


def check_exposure(route_hops, offers, nbytes, fee_ceiling):
    """Independent re-derivation of the total exposure of a route from the offers themselves (never from the planner's numbers)."""
    by_id = {o['id']: o for o in offers}; total = 0; problems = []; seen = set()
    for i, h in enumerate(route_hops):
        o = by_id.get(h['offer_id'])
        if o is None:
            problems.append({'hop': i, 'code': 'unknown_offer'}); continue
        if o['version'] != h.get('offer_version', o['version']):
            problems.append({'hop': i, 'code': 'stale_offer_version'})
        if o['provider_account'] != h.get('relay_account'):
            problems.append({'hop': i, 'code': 'relay_account_mismatch'})
        if o['provider_account'] in seen:
            problems.append({'hop': i, 'code': 'relay_account_repeated'})
        seen.add(o['provider_account']); total += hop_fee(o, nbytes)
    if total > fee_ceiling:
        problems.append({'code': 'fee_ceiling_exceeded', 'total_fee': total, 'fee_ceiling': fee_ceiling})
    if len(route_hops) > 4 or not route_hops:
        problems.append({'code': 'hops', 'allowed': '1..4'})
    return {'ok': not problems, 'total_exposure': total, 'fee_ceiling': fee_ceiling, 'problems': problems, 'per_hop': [hop_fee(by_id[h['offer_id']], nbytes) for h in route_hops if h['offer_id'] in by_id]}
