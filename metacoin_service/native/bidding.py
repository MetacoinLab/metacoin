"""Multi-provider bidding under a visible deterministic selection policy (order 2026-10-23 §21 extension 1). A work milestone may
declare `selection: {policy, bidding_window_seconds, min_bids, preference}`; the controller awards nothing while the bidding window
is open and fewer than `min_bids` eligible offers exist, then selects deterministically. Offers are private by construction: a
provider sees only its own offer, the requester sees all (economy/board.request_view). No universal reputation score exists: the
only provider-specific input is the requester's own declared preference list, visible in the recorded selection."""

POLICIES = ('lowest_price', 'earliest_window', 'declared_preference')


def validate(sel):
    if sel is None:
        return None
    if type(sel) is not dict or set(sel) - {'policy', 'bidding_window_seconds', 'min_bids', 'preference'}:
        raise ValueError('selection fields: policy, bidding_window_seconds?, min_bids?, preference?')
    if sel.get('policy', 'lowest_price') not in POLICIES:
        raise ValueError('selection.policy must be one of %s' % (POLICIES,))
    w = sel.get('bidding_window_seconds', 0); k = sel.get('min_bids', 1)
    if type(w) is not int or isinstance(w, bool) or not 0 <= w <= 7 * 86400 or type(k) is not int or isinstance(k, bool) or not 1 <= k <= 64:
        raise ValueError('selection.bidding_window_seconds 0..604800 and min_bids 1..64 (integers)')
    pref = sel.get('preference', [])
    if type(pref) is not list or any(type(p) is not str for p in pref) or len(pref) > 64:
        raise ValueError('selection.preference: list of provider ids')
    if sel.get('policy') == 'declared_preference' and not pref:
        raise ValueError('declared_preference needs a non-empty preference list')
    return {'policy': sel.get('policy', 'lowest_price'), 'bidding_window_seconds': w, 'min_bids': k, 'preference': pref}


def rank(sel, offers):
    """Deterministic total order over eligible offers (dicts with provider_id, offer_id, price_amount, window_seconds, sequence)."""
    pol = sel['policy']; pref = {p: i for i, p in enumerate(sel.get('preference', []))}
    if pol == 'lowest_price':
        key = lambda o: (o['price_amount'], o.get('window_seconds', 0), o.get('sequence', 0), o['offer_id'])
    elif pol == 'earliest_window':
        key = lambda o: (o.get('window_seconds', 0), o['price_amount'], o.get('sequence', 0), o['offer_id'])
    else:
        key = lambda o: (pref.get(o['provider_id'], len(pref)), o['price_amount'], o.get('sequence', 0), o['offer_id'])
    return sorted(offers, key=key)


def select(sel, offers, opened_at, now, ceiling=None):
    """Returns {'ready', 'chosen', 'ranked', 'reason'}: nothing is chosen while the window is open and bids are short; an offer above
    the funded ceiling is never chosen (it is ranked and marked)."""
    ranked = [dict(o, within_ceiling=(ceiling is None or o['price_amount'] <= ceiling)) for o in rank(sel, offers)]
    window_end = (opened_at or 0) + sel['bidding_window_seconds']; enough = len(offers) >= sel['min_bids']; closed = now >= window_end
    if not offers:
        return {'ready': False, 'chosen': None, 'ranked': ranked, 'reason': 'no eligible offer yet', 'window_closes_at': window_end}
    if not closed and not enough:
        return {'ready': False, 'chosen': None, 'ranked': ranked, 'reason': 'bidding window open until %d: %d of %d minimum bids' % (window_end, len(offers), sel['min_bids']), 'window_closes_at': window_end}
    chosen = next((o for o in ranked if o['within_ceiling']), None)
    return {'ready': chosen is not None, 'chosen': chosen, 'ranked': ranked, 'reason': ('selected by %s' % sel['policy']) if chosen else 'every eligible offer exceeds the funded ceiling', 'window_closes_at': window_end,
            'record': {'policy': sel['policy'], 'min_bids': sel['min_bids'], 'bids': len(offers), 'window_closed': closed, 'ranking': [{'offer_id': o['offer_id'], 'provider_id': o['provider_id'], 'price_amount': o['price_amount'], 'within_ceiling': o['within_ceiling']} for o in ranked], 'note': 'deterministic policy declared in the frozen terms; no reputation score; competing offers are private to their providers'}}
