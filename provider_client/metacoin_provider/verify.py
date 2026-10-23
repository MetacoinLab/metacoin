"""Offline verification of native receipts, intents, journal accounting and portable mission bundles under explicitly pinned
roots. Nothing here reads a database or the network; keys found inside a record are data, never roots. Results are reported
per check so a reader sees what was and was not established."""
from . import wire


def _ok(name, ok, detail=None):
    return {'check': name, 'ok': bool(ok), 'detail': detail}


def _keys(roots, domain):
    return {k['key_id']: k for k in (roots or {}).get(domain, [])}


def verify_receipts(receipts, roots, domain):
    """Each receipt statement must carry a valid authority signature under a pinned root of `domain`."""
    out = []; keys = _keys(roots, domain)
    for r in receipts or []:
        key = keys.get(r.get('key_id')); st = r.get('statement')
        if key is None:
            out.append(_ok('receipt:' + str(r.get('id')), False, {'reason': 'key_not_pinned', 'key_id': r.get('key_id')})); continue
        ok = isinstance(st, dict) and wire.verify(key['public_hex'], wire.signing_bytes('receipt', st), r.get('signature_hex', ''))
        out.append(_ok('receipt:' + str(r.get('id')), ok, {'kind': r.get('kind'), 'ref': [r.get('ref_type'), r.get('ref_id')], 'claims': (st or {}).get('claims') if ok else None}))
    return out


def verify_intents(intents, accounts=None, account_keys=None):
    """An intent's signature must verify under a public key recorded for the actor account (the current key, or a key of its
    history: rotated primaries and delegates) and its digest must match the message. The keys are data from the record: this
    establishes consistency of the record, not the identity of the signer."""
    out = []; pub = {a['id']: a.get('public_hex') for a in (accounts or [])}; hist = {}
    for k in account_keys or []:
        hist.setdefault(k.get('account_id'), []).append(k)
    for i in intents or []:
        if i.get('signature_hex') and i.get('message'):
            acct = i.get('actor_account'); keys = hist.get(acct, []); named = [k['public_hex'] for k in keys if k.get('key_id') == i.get('signer_key_id')]
            cands = [p for p in (named + [i.get('signer_public_hex'), pub.get(acct)] + [k['public_hex'] for k in keys]) if p]
            sb = wire.signing_bytes('intent', i['message']); used = next((p for p in cands if wire.verify(p, sb, i['signature_hex'])), None)
            ok = used is not None and wire.digest(i['message']) == i.get('message_digest')
            out.append(_ok('intent:' + str(i.get('id')), ok, {'actor_account': acct, 'amount_final': i.get('amount_final'), 'key': 'named key' if used and named and used == named[0] else ('current key' if used == pub.get(acct) else ('key history' if used else None)), 'bound_to': {k: i['message'].get(k) for k in ('authority', 'recipient', 'amount_ceiling', 'obligation_id')}}))
        elif i.get('type') == 'settlement':
            out.append(_ok('intent:' + str(i.get('id')), True, {'note': 'authority-ordered settlement without an actor signature (treasury or reservation settlement); the authority receipt applies'}))
    return out


def bookkeeping(entries, complete=True):
    """Every journal entry balances; over a COMPLETE sequence no account category goes negative (a mission bundle carries only the
    entries that reference the mission, so that second check is reported as not decidable there)."""
    unbalanced = [e.get('event_key') for e in entries or [] if sum(p['debit'] for p in e['postings']) != sum(p['credit'] for p in e['postings'])]
    bal = {}
    for e in entries or []:
        for p in e['postings']:
            key = (e['asset'], p['account'].split('/', 1)[-1]); bal[key] = bal.get(key, 0) + p['credit'] - p['debit']
    out = [_ok('entries_balanced', not unbalanced, {'unbalanced': unbalanced[:10], 'entries': len(entries or [])})]
    if complete:
        neg = [list(k) for k, v in bal.items() if k[1].startswith('acct:') and v < 0]
        out.append(_ok('no_negative_account_category', not neg, {'negative': neg[:10]}))
    else:
        out.append(_ok('no_negative_account_category', True, {'scope': 'not decidable from a mission subset of the journal; verify the full export for this property'}))
    return out, bal


def verify_bundle(bundle, roots):
    """A portable mission bundle (metacoin-mission-bundle/v1): the authority's receipt over the bundle content digest, receipts,
    intents, journal bookkeeping and the mission ledger recomputed from the postings. Tampering with any evidence or any
    accounting figure fails a named check."""
    report = {'schema': 'metacoin-mission-bundle-verification/v1', 'checks': []}; C = report['checks']
    if not isinstance(bundle, dict) or bundle.get('schema') != 'metacoin-mission-bundle/v1':
        C.append(_ok('bundle_schema', False, {'got': bundle.get('schema') if isinstance(bundle, dict) else type(bundle).__name__})); report['ok'] = False; return report
    content = bundle.get('content') or {}; seal = bundle.get('seal') or {}; domain = content.get('domain')
    keys = _keys(roots, domain); key = keys.get(seal.get('key_id'))
    digest = wire.digest(content)
    C.append(_ok('content_digest', digest == seal.get('content_digest'), {'recomputed': digest, 'sealed': seal.get('content_digest')}))
    if key is None:
        C.append(_ok('seal_root_pinned', False, {'domain': domain, 'key_id': seal.get('key_id'), 'note': 'no pinned root for the sealing authority: authenticity cannot be established from the bundle itself'}))
    else:
        st = seal.get('statement') or {}
        C.append(_ok('seal_signature', wire.verify(key['public_hex'], wire.signing_bytes('receipt', st), seal.get('signature_hex', '')) and st.get('claims', {}).get('content_digest') == seal.get('content_digest'), {'issued_at': st.get('issued_at')}))
    C += verify_receipts(content.get('receipts'), roots, domain)
    C += verify_intents(content.get('intents'), content.get('accounts'), content.get('account_keys'))
    bk, bal = bookkeeping(content.get('journal_entries'), complete=False); C += bk
    # ledger recomputation: settled amounts per intent -> spent per milestone; released and reserved from the obligation/reservation states
    settled_by_ref = {}
    for i in content.get('intents') or []:
        if i.get('state') == 'settled' and i.get('obligation_id'):
            settled_by_ref[i['obligation_id']] = settled_by_ref.get(i['obligation_id'], 0) + int(i.get('amount_final') or 0)
    mism = []
    for row in (content.get('ledger') or {}).get('milestones') or []:
        obls = row.get('obligations') or []
        recomputed = sum(settled_by_ref.get(o, 0) for o in obls)
        if obls and recomputed != row.get('spent'):
            mism.append({'milestone': row.get('key'), 'ledger_spent': row.get('spent'), 'recomputed_from_intents': recomputed})
    C.append(_ok('ledger_spent_matches_settled_intents', not mism, {'mismatches': mism[:10]}))
    tot = (content.get('ledger') or {}).get('totals') or {}
    rows = (content.get('ledger') or {}).get('milestones') or []
    C.append(_ok('ledger_totals_match_rows', all(tot.get(k) == sum(r.get(k, 0) for r in rows) for k in ('spent', 'reserved', 'released')), {'totals': tot}))
    # receipts named by the mission must exist in the bundle
    named = set((content.get('mission') or {}).get('receipt_ids') or []); present = {r.get('id') for r in content.get('receipts') or []}
    C.append(_ok('named_receipts_present', named <= present, {'missing': sorted(named - present)[:10]}))
    report['ok'] = all(c['ok'] for c in C); report['scope'] = 'signatures under supplied roots; bookkeeping of the supplied entries; ledger figures against settled intents; scientific validity is not decided here'
    return report


def verify_export(export, roots):
    """Native record export (GET /api/v1/native/export): receipts, intents, bookkeeping."""
    C = verify_receipts(export.get('receipts'), roots, export.get('domain')) + verify_intents(export.get('intents'), export.get('accounts'), export.get('account_keys'))
    bk, _ = bookkeeping(export.get('journal_entries')); C += bk
    return {'schema': 'metacoin-export-verification/v1', 'domain': export.get('domain'), 'checks': C, 'ok': all(c['ok'] for c in C)}


# ---- v2 (order 2026-10-23 §17): resource overlap, claim-lot lineage, outstanding obligations, stale epochs, revoked keys --------------------
def pool_sweep(schedule, commitments):
    """Independent interval sweep over half-open commitments: every point within capacity."""
    pts = sorted({e['start'] for e in schedule} | {e['end'] for e in schedule} | {c['start'] for c in commitments} | {c['end'] for c in commitments}); bad = []
    for a, b in zip(pts, pts[1:]):
        used = sum(c['slots'] for c in commitments if c['start'] < b and c['end'] > a and c.get('state', 'reserved') in ('reserved', 'active', 'delivered'))
        cap = next((e['capacity'] for e in schedule if e['start'] <= a < e['end']), 0)
        if used > cap:
            bad.append({'start': a, 'end': b, 'used': used, 'capacity': cap})
    return bad


def check_pool(pool, commitments):
    if type(pool.get('units_per_slot_second')) is not int or pool['units_per_slot_second'] <= 0 or type(pool.get('resolution_seconds')) is not int or pool['resolution_seconds'] <= 0:
        return [_ok('pool:' + str(pool.get('id')), False, {'error': 'units_per_slot_second', 'note': 'resource units must be positive bounded integers'})]
    bad = pool_sweep(pool['schedule'], commitments)
    return [_ok('pool:' + str(pool.get('id')), not bad, {'error': 'pool_overlap' if bad else None, 'segments': bad[:3]})]


def check_lots(allocation, lots):
    """Lots of one origin partition its amount exactly once: a split reduces the parent in place and issues the remainder as a
    child, so the sum over every non-merged lot (active, committed, retired, spent) equals the allocation amount. A child issued
    from an already spent parent breaks the partition (the spent parent still carries its amount)."""
    mine = [l for l in lots if l['origin_allocation'] == allocation['id'] and l['status'] != 'merged']
    if not mine:
        return [_ok('lots:' + allocation['id'], True, {'note': 'no lots recorded (legacy allocation without lineage)'})]
    parents = {l['lot_id']: l for l in mine}
    for l in mine:
        par = parents.get(l.get('parent_lot') or '')
        if par is not None and par['status'] == 'spent' and l['created_sequence'] > par['created_sequence'] and l.get('split_after_spent'):
            return [_ok('lots:' + allocation['id'], False, {'error': 'lot_partition', 'parent': par['lot_id'], 'note': 'a split consumed an already spent parent'})]
    total = sum(l['amount'] for l in mine); ok = total == allocation['amount']
    return [_ok('lots:' + allocation['id'], ok, {'error': None if ok else 'lot_partition', 'lot_sum': total, 'amount': allocation['amount'], 'returned': allocation.get('returned_amount', 0), 'note': None if ok else 'lots of this origin do not partition the allocation amount (a split consumed an already spent parent, or a lot was lost)'})]


def check_export_v2(export, roots):
    """Reconstruct resource overlap, allocation lineage and outstanding obligations from an export (no database, no network)."""
    C = []
    by_pool = {}
    for c in export.get('pool_commitments') or []:
        by_pool.setdefault(c['pool_id'], []).append(c)
    for p in export.get('resource_pools') or []:
        C += check_pool(p, by_pool.get(p['id'], []))
    lots = export.get('claim_lots') or []
    for a in export.get('allocations') or []:
        if a.get('direction') == 'import':
            C += check_lots({'id': a['id'], 'amount': a['amount'], 'returned_amount': a.get('returned_amount', 0)}, lots)
    outstanding = [o for o in export.get('obligations') or [] if o.get('state') in ('open', 'partial')]
    C.append(_ok('obligations_consistent', all(o['settled_amount'] <= o['amount'] and o['amount'] - o['settled_amount'] == o.get('outstanding', o['amount'] - o['settled_amount']) for o in outstanding), {'outstanding': len(outstanding), 'sum_outstanding': sum(o['amount'] - o['settled_amount'] for o in outstanding)}))
    keys = {k['key_id']: k for ks in (roots or {}).values() for k in ks}
    for r in export.get('receipts') or []:
        k = keys.get(r.get('key_id'))
        if k and (k.get('state') == 'revoked' or (k.get('retired_at') and (k.get('grace_until') is None or (r.get('statement') or {}).get('issued_at', 0) > k['grace_until']))):
            C.append(_ok('receipt_key_valid:' + str(r.get('id')), False, {'error': 'key_not_valid', 'key_id': r.get('key_id')}))
    return {'schema': 'metacoin-export-verification/v2', 'checks': C, 'ok': all(c['ok'] for c in C)}
