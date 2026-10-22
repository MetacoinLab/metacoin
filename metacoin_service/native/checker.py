"""Independent conformance checker (order §18): inspects an exported native record (messages, receipts, intents, obligations,
allocations, journal entries) and an explicit trust-root configuration without the live database or any network. It
verifies the supported claims and names missing evidence, keeping five properties apart:

  authorized_bookkeeping      journal entries balance; account categories are non-negative; conservation of the exercised sequence
  cryptographic_authenticity  envelope / certificate / receipt signatures under the SUPPLIED roots only; bindings (domain, environment, epoch)
  scientific_reproducibility  not decided here (products have their own reference checker); reported as out of scope
  custody_assumptions         who holds which keys (development keys are service-custodied; the operator is the same for both domains)
  economic_backing            exported backing vs imported claims; unresolved allocations; what is pending

No single badge: the report is per property. Only the standard library and the protocol module (canonical bytes) are used;
signature verification uses the same Ed25519 primitive as the service (cryptography) through metacoin_service.crypto."""
import json

from .. import crypto
from . import protocol as P

CHECKER_ID = 'metacoin-native-conformance-checker/v0'


def _ok(name, ok, detail=None):
    return {'check': name, 'ok': bool(ok), 'detail': detail}


def verify_item(item, roots, environment):
    """One transport item: envelope + payload, each signed by the sender's authority key as pinned in roots {domain: [{key_id, public_hex, epoch, retired_at?, grace_until?}]}."""
    checks = []
    try:
        env = item['envelope']; P.validate_envelope(env)
    except (KeyError, TypeError, P.ProtocolError) as exc:
        return [_ok('envelope_shape', False, getattr(exc, 'code', str(exc)))]
    checks.append(_ok('envelope_environment', env['environment'] == environment, {'got': env['environment'], 'expected': environment}))
    keys = {k['key_id']: k for k in roots.get(env['sender'], [])}
    key = keys.get(item.get('key_id'))
    checks.append(_ok('sender_root_pinned', key is not None, {'sender': env['sender'], 'key_id': item.get('key_id'), 'note': None if key else 'no pinned root for this sender/key: authenticity cannot be established from the bundle itself'}))
    if key is None:
        return checks
    if key.get('retired_at') and (key.get('grace_until') is None or env['created_at'] > key['grace_until']):
        checks.append(_ok('key_validity_interval', False, {'retired_at': key.get('retired_at'), 'grace_until': key.get('grace_until'), 'created_at': env['created_at']}))
    checks.append(_ok('envelope_signature', crypto.verify(key['public_hex'], P.signing_bytes('envelope', env), item.get('envelope_signature', ''))))
    checks.append(_ok('payload_digest', env['payload_digest'] == P.digest(item.get('payload'))))
    checks.append(_ok('payload_signature', crypto.verify(key['public_hex'], P.signing_bytes(env['message_type'], item.get('payload')), item.get('payload_signature', ''))))
    try:
        P.validate_message(env['message_type'], item['payload'], environment); checks.append(_ok('payload_fields', True))
    except P.ProtocolError as exc:
        checks.append(_ok('payload_fields', False, {'code': exc.code, 'detail': exc.detail}))
        return checks
    pl = item['payload']
    if env['message_type'] == 'allocation_certificate':
        checks.append(_ok('certificate_sender_binding', pl['source_authority'] == env['sender'] and pl['source_epoch'] == key.get('epoch'), {'source_epoch': pl['source_epoch'], 'key_epoch': key.get('epoch')}))
        checks.append(_ok('certificate_destination_binding', pl['destination_authority'] == env['receiver']))
        checks.append(_ok('certificate_asset_origin', P.asset_parts(pl['asset'])['authority'] == env['sender']))
    return checks


def verify_receipts(receipts, roots, environment, domain):
    checks = []; keys = {k['key_id']: k for k in roots.get(domain, [])}
    for r in receipts:
        key = keys.get(r.get('key_id')); st = r.get('statement')
        ok = key is not None and isinstance(st, dict) and crypto.verify(key['public_hex'], P.signing_bytes('receipt', st), r.get('signature_hex', ''))
        checks.append(_ok('receipt:' + str(r.get('id')), ok, {'kind': (st or {}).get('kind'), 'key_pinned': key is not None}))
    return checks


def bookkeeping(entries, audits):
    """Per asset: entries balance, no negative account category, conservation recomputed from the postings and compared with the exported audit."""
    checks = []
    unbalanced = [e['event_key'] for e in entries if sum(p['debit'] for p in e['postings']) != sum(p['credit'] for p in e['postings'])]
    checks.append(_ok('entries_balanced', not unbalanced, {'unbalanced': unbalanced[:10], 'entries': len(entries)}))
    bal = {}
    for e in entries:
        for p in e['postings']:
            key = (e['asset'], p['account'].split('/', 1)[-1]); bal[key] = bal.get(key, 0) + p['credit'] - p['debit']      # journal accounts are scope-prefixed (asset@network/account)
    neg = [k for k, v in bal.items() if k[1].startswith('acct:') and v < 0]
    checks.append(_ok('no_negative_account_category', not neg, {'negative': [list(k) for k in neg[:10]]}))
    totals = {}
    for asset in sorted({k[0] for k in bal}):
        held = sum(v for k, v in bal.items() if k[0] == asset and k[1].startswith('acct:'))
        fixture = -sum(v for k, v in bal.items() if k[0] == asset and k[1] == 'equity:fixture_funding')
        exported = sum(v for k, v in bal.items() if k[0] == asset and k[1].startswith('backing:exported:'))     # credited when value leaves the accounts
        claims = -sum(v for k, v in bal.items() if k[0] == asset and k[1].startswith('backing:claim:'))        # debited when a claim is credited to an account
        totals[asset] = {'held': held, 'fixture_funding': fixture, 'backing_exported': exported, 'claims': claims}
        checks.append(_ok('conservation_recomputed:' + asset, held == fixture - exported + claims, dict(totals[asset], expected_held=fixture - exported + claims)))
        a = (audits or {}).get(asset)
        if a:
            checks.append(_ok('audit_matches_recomputation:' + asset, a.get('total_held') == held and a.get('fixture_funding') == fixture and a.get('backing_locked_for_exports') == exported and a.get('claims_backed_elsewhere') == claims, {'reported': {k: a.get(k) for k in ('total_held', 'fixture_funding', 'backing_locked_for_exports', 'claims_backed_elsewhere')}}))
    return checks, totals


def economic_backing(allocations, export_domain):
    checks = []; pending = []; resolved = 0
    for a in allocations:
        st = a['state']
        msgs = a.get('messages') or []
        if st in ('acknowledged', 'returned') or (a['direction'] == 'import' and st == 'imported' and msgs and all(m['state'] == 'delivered' for m in msgs)):
            resolved += 1                                                             # at the destination an import is reconciled once its acknowledgment was delivered
        elif st in ('reserved', 'export_committed', 'certified', 'imported', 'return_requested', 'quarantined'):
            pending.append({'allocation_id': a['id'], 'direction': a['direction'], 'state': st, 'amount': a['amount'], 'finality': a.get('finality')})
    checks.append(_ok('allocations_classified', True, {'resolved': resolved, 'pending': len(pending)}))
    both = [a['id'] for a in allocations if a['direction'] == 'export' and a['state'] in ('certified', 'acknowledged') and a.get('source_backing_spendable')]
    checks.append(_ok('no_backing_spendable_while_claim_outstanding', not both, {'violations': both}))
    return checks, pending


def check(export, roots, environment='dev'):
    """export: the record produced by GET /api/v1/native/export (or assembled from bundles); roots: {domain: [{key_id, public_hex, epoch, ...}]}."""
    report = {'schema': 'metacoin-native-conformance-report/v0', 'checker': CHECKER_ID, 'domain': export.get('domain'), 'properties': {}}
    # 1 bookkeeping
    bk, totals = bookkeeping(export.get('journal_entries', []), export.get('audits') or ({export.get('audit', {}).get('asset'): export['audit']} if export.get('audit') else {}))
    report['properties']['authorized_bookkeeping'] = {'ok': all(c['ok'] for c in bk), 'checks': bk, 'totals': totals, 'scope': 'journal entries supplied in the export; the live database was not read'}
    # 2 authenticity
    auth = []
    for it in export.get('envelopes_in', []) + export.get('envelopes_out', []):
        auth += verify_item(it, roots, environment)
    auth += verify_receipts(export.get('receipts', []), roots, environment, export.get('domain'))
    for i in export.get('intents', []):
        if i.get('signature_hex') and i.get('message') and i.get('signer_public_hex'):
            auth.append(_ok('intent:' + i['id'], crypto.verify(i['signer_public_hex'], P.signing_bytes('intent', i['message']), i['signature_hex']) and P.digest(i['message']) == i.get('message_digest'), {'account': i.get('actor_account')}))
        elif i.get('type') == 'settlement' and not i.get('signature_hex'):
            auth.append(_ok('intent:' + i['id'], True, {'note': 'treasury/authority-ordered settlement: no actor signature by design; authority receipt applies'}))
    missing = [c['check'] for c in auth if c['check'] == 'sender_root_pinned' and not c['ok']]
    report['properties']['cryptographic_authenticity'] = {'ok': all(c['ok'] for c in auth), 'checks': auth, 'roots_supplied': sorted(roots), 'missing_evidence': missing, 'scope': 'only the supplied roots are trusted; keys inside the export are data, not roots'}
    # 3 reproducibility
    report['properties']['scientific_reproducibility'] = {'ok': None, 'checks': [], 'scope': 'not decided by this checker: lunar products carry their own independent reference (metacoin_service.lunar.reference); energy determinations use the work-contract verifier', 'note': 'a valid signature says nothing about scientific validity'}
    # 4 custody
    cust = [_ok('custody_labels_present', all(a.get('custody') for a in export.get('accounts', [])), {'accounts': len(export.get('accounts', []))}),
            _ok('development_custody_declared', all('development' in (a.get('custody') or '') or a.get('kind') != 'actor' for a in export.get('accounts', [])), {'note': 'actor keys are held by the service on the actor\'s behalf (not operator custody, not independent signers)'}),
            _ok('operator_same_for_peers', True, {'operator': export.get('operator'), 'host': export.get('host'), 'note': 'the same operator runs every domain in this experiment: replicated processes are not independent validators'})]
    report['properties']['custody_assumptions'] = {'ok': all(c['ok'] for c in cust), 'checks': cust}
    # 5 backing
    eb, pending = economic_backing(export.get('allocations', []), export.get('domain'))
    report['properties']['economic_backing'] = {'ok': all(c['ok'] for c in eb), 'checks': eb, 'pending': pending, 'backing_locked': {a: t['backing_exported'] for a, t in totals.items()}, 'claims_held': {a: t['claims'] for a, t in totals.items()}, 'reconciliation': 'pending' if pending else 'complete for the exported sequence'}
    report['summary'] = {k: v['ok'] for k, v in report['properties'].items()}
    report['note'] = 'per-property results; the checker never collapses them into one score'
    return report


def main(argv=None):
    import argparse, sys
    ap = argparse.ArgumentParser(prog='metacoin-native-check', description='independent conformance check of an exported native record (no database, no network)')
    ap.add_argument('export'); ap.add_argument('--roots', required=True, help='JSON {domain: [{key_id, public_hex, epoch}]} pinned explicitly by the verifier'); ap.add_argument('--environment', default='dev')
    a = ap.parse_args(argv)
    rep = check(json.load(open(a.export)), json.load(open(a.roots)), a.environment)
    print(json.dumps(rep, indent=1)); return 0 if all(v in (True, None) for v in rep['summary'].values()) else 1


if __name__ == '__main__':
    import sys
    sys.exit(main())
