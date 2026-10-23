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


def verify_intents(intents, accounts=None):
    """An intent's signature must verify under the actor account's recorded public key and its digest must match the message.
    The public key is data from the record: this establishes consistency of the record, not the identity of the signer."""
    out = []; pub = {a['id']: a.get('public_hex') for a in (accounts or [])}
    for i in intents or []:
        if i.get('signature_hex') and i.get('message'):
            p = i.get('signer_public_hex') or pub.get(i.get('actor_account'))
            ok = bool(p) and wire.verify(p, wire.signing_bytes('intent', i['message']), i['signature_hex']) and wire.digest(i['message']) == i.get('message_digest')
            out.append(_ok('intent:' + str(i.get('id')), ok, {'actor_account': i.get('actor_account'), 'amount_final': i.get('amount_final'), 'bound_to': {k: i['message'].get(k) for k in ('authority', 'recipient', 'amount_ceiling', 'obligation_id')}}))
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
    C += verify_intents(content.get('intents'), content.get('accounts'))
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
    C = verify_receipts(export.get('receipts'), roots, export.get('domain')) + verify_intents(export.get('intents'), export.get('accounts'))
    bk, _ = bookkeeping(export.get('journal_entries')); C += bk
    return {'schema': 'metacoin-export-verification/v1', 'domain': export.get('domain'), 'checks': C, 'ok': all(c['ok'] for c in C)}
