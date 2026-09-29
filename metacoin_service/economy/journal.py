"""Replayable double-entry accounting journal (Order 08 §49–§51).

Every journal entry binds one unique business event (event_key), balances within one asset/network scope, and has an
immutable identity. Integer base units only; assets and environments are never mixed into one number. Application
budget reservations are memorandum accounts (they are not asset movements). Derived balances are rebuilt from the
postings by `replay()` and compared with the live operational views; nothing patches a cached balance.

Account names (scope = asset@network, environment kept separately):
  memo:budget_reserved / memo:budget_available        application reservations (memorandum, not money)
  expense:work_accepted                                accepted obligations to providers
  expense:platform_fee / expense:verifier              fee and verifier obligations of the requester
  liability:payable                                    accepted, not yet submitted obligations (per entitlement in ref)
  exposure:pending                                     submitted, not yet observed transfers
  asset:payer_cash                                     the payer's asset holdings (credit = paid out, debit = refund received)
  treasury:cash / treasury:revenue                     fees observed as settled to the treasury address
  treasury:committed                                   treasury allocations reserved for awards (memorandum against revenue)
  liability:refund_claim                               repayment obligations until a reverse transfer is observed
"""
import json
import secrets

from .. import history
from ..db import now
from ..errors import ServiceError


def scope(asset, network):
    return '%s@%s' % (asset, network)


def post(db, workspace, event_key, kind, asset, network, environment, postings, ref_type=None, ref_id=None, memo=None):
    """Idempotent by event_key: a duplicate observation, webhook replay or repeated reconciliation posts nothing twice."""
    existing = db.execute('SELECT id FROM journal_entries WHERE event_key=?', (event_key,)).fetchone()
    if existing is not None:
        return existing['id'], False
    if not postings or any(type(d) is not int or type(c) is not int or d < 0 or c < 0 or (d and c) for _, d, c in postings):
        raise ServiceError('INTERNAL_DEFECT', 'journal postings must be non-negative integers, debit xor credit')
    if sum(d for _, d, _ in postings) != sum(c for _, _, c in postings):
        raise ServiceError('INTERNAL_DEFECT', {'code': 'journal_unbalanced', 'event_key': event_key})
    eid = 'je_' + secrets.token_hex(8)
    db.execute('INSERT INTO journal_entries VALUES (?,?,?,?,?,?,?,?,?,?,?)', (eid, workspace, event_key, kind, asset, network, environment, ref_type, ref_id, memo, now()))
    sc = scope(asset, network)
    for account, debit, credit in postings:
        db.execute('INSERT INTO journal_postings VALUES (?,?,?,?,?)', ('jp_' + secrets.token_hex(6), eid, sc + '/' + account, debit, credit))
    history.record(db, workspace, 'journal', 'work.journal', 'journal_entry', eid, {'event_key': event_key, 'kind': kind, 'asset': asset, 'network': network, 'postings': len(postings)})
    return eid, True


def balances(db, workspace, sc=None):
    """account -> (debits, credits, net = debits - credits) rebuilt from the postings."""
    rows = db.execute('SELECT p.account, SUM(p.debit) AS d, SUM(p.credit) AS c FROM journal_postings p JOIN journal_entries e ON e.id=p.entry_id WHERE e.workspace=? GROUP BY p.account', (workspace,)).fetchall()
    out = {}
    for r in rows:
        if sc is None or r['account'].startswith(sc + '/'):
            out[r['account']] = {'debits': r['d'] or 0, 'credits': r['c'] or 0, 'net': (r['d'] or 0) - (r['c'] or 0)}
    return out


def entries(db, workspace, limit=500):
    out = []
    for e in db.execute('SELECT * FROM journal_entries WHERE workspace=? ORDER BY created_at, rowid LIMIT ?', (workspace, limit)).fetchall():
        posts = [{'account': p['account'], 'debit': p['debit'], 'credit': p['credit']} for p in db.execute('SELECT * FROM journal_postings WHERE entry_id=? ORDER BY rowid', (e['id'],)).fetchall()]
        out.append({'id': e['id'], 'event_key': e['event_key'], 'kind': e['kind'], 'asset': e['asset'], 'network': e['network'], 'environment': e['environment'], 'ref_type': e['ref_type'], 'ref_id': e['ref_id'], 'memo': e['memo'], 'created_at': e['created_at'], 'postings': posts})
    return out


def scope_balances(db, workspace):
    """Per asset scope: the five figures the treasury and requester views are made of (all rebuilt from postings)."""
    b = balances(db, workspace)
    scopes = sorted({a.split('/')[0] for a in b})
    out = {}
    for sc in scopes:
        g = lambda name: b.get(sc + '/' + name, {'net': 0, 'debits': 0, 'credits': 0})
        out[sc] = {'obligations_payable': g('liability:payable')['credits'] - g('liability:payable')['debits'], 'exposure_pending': g('exposure:pending')['credits'] - g('exposure:pending')['debits'],
                   'paid_out': g('asset:payer_cash')['credits'] - g('asset:payer_cash')['debits'], 'refund_claims_open': g('liability:refund_claim')['credits'] - g('liability:refund_claim')['debits'],
                   'treasury_revenue': g('treasury:revenue')['credits'], 'treasury_cash': g('treasury:cash')['debits'] - g('treasury:cash')['credits'], 'treasury_committed': g('treasury:committed')['debits'] - g('treasury:committed')['credits'],
                   'treasury_spent': g('treasury:spent')['debits'], 'budget_reserved_memo': g('memo:budget_reserved')['debits'] - g('memo:budget_reserved')['credits']}
        out[sc]['treasury_available'] = out[sc]['treasury_revenue'] - out[sc]['treasury_committed'] - out[sc]['treasury_spent']
    return out


def invariants(db, workspace):
    """Conservation checks appropriate to this model; each named, each with the numbers behind it."""
    checks = []
    for e in db.execute('SELECT id, event_key FROM journal_entries WHERE workspace=?', (workspace,)).fetchall():
        s = db.execute('SELECT SUM(debit) AS d, SUM(credit) AS c FROM journal_postings WHERE entry_id=?', (e['id'],)).fetchone()
        if (s['d'] or 0) != (s['c'] or 0):
            checks.append({'check': 'entry_balanced', 'ok': False, 'detail': {'entry': e['id'], 'event_key': e['event_key']}})
    checks.append({'check': 'every_entry_balanced', 'ok': not any(c['check'] == 'entry_balanced' for c in checks), 'detail': None})
    for sc, v in scope_balances(db, workspace).items():
        checks.append({'check': 'treasury_available_within_confirmed_revenue:' + sc, 'ok': v['treasury_available'] <= v['treasury_revenue'] and v['treasury_committed'] >= 0 and v['treasury_spent'] >= 0, 'detail': v})
        checks.append({'check': 'no_negative_exposure:' + sc, 'ok': v['exposure_pending'] >= 0 and v['obligations_payable'] >= 0, 'detail': {'exposure_pending': v['exposure_pending'], 'obligations_payable': v['obligations_payable']}})
        checks.append({'check': 'treasury_cash_covers_committed_and_spent:' + sc, 'ok': v['treasury_cash'] >= v['treasury_committed'] - 0, 'detail': {'cash': v['treasury_cash'], 'committed': v['treasury_committed'], 'spent': v['treasury_spent']}})
    dup = db.execute('SELECT event_key, COUNT(*) c FROM journal_entries WHERE workspace=? GROUP BY event_key HAVING c > 1', (workspace,)).fetchall()
    checks.append({'check': 'unique_business_events', 'ok': not dup, 'detail': [d['event_key'] for d in dup][:5]})
    return checks


def replay(db, workspace):
    """Rebuild balances from the journal and compare with the live operational views (entitlements, intents, treasury)."""
    b = scope_balances(db, workspace)
    live = {}
    for e in db.execute('SELECT * FROM work_entitlements WHERE workspace=?', (workspace,)).fetchall():
        i = db.execute('SELECT network FROM payment_intents WHERE id=?', (e['payment_intent_id'],)).fetchone() if e['payment_intent_id'] else None
        net = i['network'] if i else ('application' if e['asset'] == 'action-units' else 'local-chain')
        sc = scope(e['asset'], net)
        live.setdefault(sc, {'payable': 0, 'submitted': 0, 'paid': 0, 'refund_claims': 0})
        if e['state'] in ('payable', 'held', 'authorized'):
            live[sc]['payable'] += e['amount']
        elif e['state'] == 'submitted' or e['state'] == 'exposed':
            live[sc]['submitted'] += e['amount']
        elif e['state'] == 'paid':
            live[sc]['paid'] += e['amount']
        elif e['state'] == 'refund_pending':
            live[sc]['paid'] += e['amount']; live[sc]['refund_claims'] += e['amount']
        elif e['state'] == 'refunded':
            pass
    diffs = []
    for sc, v in live.items():
        j = b.get(sc, {})
        for jkey, lkey in (('obligations_payable', 'payable'), ('exposure_pending', 'submitted'), ('refund_claims_open', 'refund_claims')):
            if j.get(jkey, 0) != v[lkey]:
                diffs.append({'scope': sc, 'figure': jkey, 'journal': j.get(jkey, 0), 'live_view': v[lkey]})
    refunded = {}
    for r in db.execute("SELECT asset, network, COALESCE(SUM(final_amount),0) AS a FROM payment_intents WHERE workspace=? AND kind='refund' AND state='settled' GROUP BY asset, network", (workspace,)).fetchall():
        refunded[scope(r['asset'], r['network'])] = r['a']
    for sc, v in live.items():
        j = b.get(sc, {})
        expected_paid_out = v['paid'] - refunded.get(sc, 0)
        if j.get('paid_out', 0) != expected_paid_out:
            diffs.append({'scope': sc, 'figure': 'paid_out', 'journal': j.get('paid_out', 0), 'live_view': expected_paid_out, 'note': 'live = paid entitlement amounts minus settled refunds'})
    return {'scopes': b, 'live': live, 'differences': diffs, 'consistent': not diffs, 'invariants': invariants(db, workspace), 'note': 'balances rebuilt from postings; no cached balance is patched'}
