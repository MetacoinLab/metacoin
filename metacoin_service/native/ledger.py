"""Native accounts on the authoritative journal (no parallel wallet balance).

Every account category is a journal account in the scope `asset@domain`: `acct:<account>:<category>`. Value enters a
domain only through an explicitly installed development fixture (`equity:fixture_funding`, isolated to zero-value test
assets) or an imported allocation whose backing is locked at the source (`backing:claim:<allocation>`); it leaves through
an export (`backing:exported:<allocation>`). Views are rebuilt from postings; `audit()` counts unique economic ownership."""
import secrets

from ..db import now
from ..errors import ServiceError
from ..economy import journal
from . import protocol as P


def account_name(account_id, category):
    if category not in P.CATEGORIES:
        raise ServiceError('INTERNAL_DEFECT', 'unknown account category ' + category)
    return 'acct:%s:%s' % (account_id, category)


def balance(db, workspace, asset, domain, account_id, category):
    sc = journal.scope(asset, domain)
    b = journal.balances(db, workspace, sc).get(sc + '/' + account_name(account_id, category))
    return (b['credits'] - b['debits']) if b else 0                      # holdings grow with credits


def categories(db, workspace, asset, domain, account_id):
    sc = journal.scope(asset, domain)
    b = journal.balances(db, workspace, sc)
    out = {}
    for c in P.CATEGORIES:
        r = b.get(sc + '/' + account_name(account_id, c))
        out[c] = (r['credits'] - r['debits']) if r else 0
    return out


def post(db, workspace, event_key, kind, asset, domain, environment, postings, ref_type, ref_id, memo):
    """Postings are (account_name, debit, credit) with holdings growing on credit; the journal checks balance."""
    return journal.post(db, workspace, event_key, kind, asset, domain, environment, postings, ref_type, ref_id, memo)


def move(db, workspace, event_key, kind, asset, domain, environment, amount, src, dst, ref_type, ref_id, memo):
    """Move `amount` from account/category src to account/category dst (each 'acct:x:cat' or a system account name)."""
    P.amount(amount)
    return post(db, workspace, event_key, kind, asset, domain, environment, [(src, amount, 0), (dst, 0, amount)], ref_type, ref_id, memo)


def require_available(db, workspace, asset, domain, account_id, amount):
    avail = balance(db, workspace, asset, domain, account_id, 'available')
    if avail < amount:
        raise ServiceError('BUDGET_EXHAUSTED', {'code': 'insufficient_available', 'available': avail, 'requested': amount, 'note': 'only settled available funds spend; receivables, reserved and in-transit amounts do not'})
    return avail


def audit(db, workspace, asset, domain):
    """Conservation for one asset in this domain: unique economic ownership = fixture funding + imported claims − exported backing
    − returned claims; the sum over every account category must equal it; backing locked for exports is reported separately."""
    sc = journal.scope(asset, domain)
    b = journal.balances(db, workspace, sc)
    held = {c: 0 for c in P.CATEGORIES}
    accounts = {}
    for name, v in b.items():
        acct = name.split('/', 1)[1]
        if acct.startswith('acct:'):
            _, aid, cat = acct.split(':', 2)
            held[cat] += v['credits'] - v['debits']
            accounts.setdefault(aid, {c: 0 for c in P.CATEGORIES})[cat] = v['credits'] - v['debits']
    g = lambda n: b.get(sc + '/' + n, {'debits': 0, 'credits': 0})
    fixture = g('equity:fixture_funding')['debits'] - g('equity:fixture_funding')['credits']
    exported = sum(v['credits'] - v['debits'] for n, v in b.items() if n.split('/', 1)[1].startswith('backing:exported:'))
    claims = sum(v['debits'] - v['credits'] for n, v in b.items() if n.split('/', 1)[1].startswith('backing:claim:'))
    total_held = sum(held.values())
    expected = fixture - exported + claims
    return {'asset': asset, 'domain': domain, 'held_by_category': held, 'total_held': total_held, 'fixture_funding': fixture, 'backing_locked_for_exports': exported, 'claims_backed_elsewhere': claims,
            'expected_total_held': expected, 'conserved': total_held == expected, 'accounts': accounts, 'negative_balances': [(a, c) for a, cs in accounts.items() for c, v in cs.items() if v < 0],
            'reading': 'unique ownership counts a unit once: a locked export backing is not held here and an imported claim is held here only; fixture funding is development-only and zero-value'}
