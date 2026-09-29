"""Fee-backed MetaStar treasury work budgets at the service layer (Order 08 §50–§51).

Available treasury money comes only from fee revenue observed as SETTLED to the treasury address (journal account
treasury:revenue), never from accepting a report, recording GPU time or labelling work useful. A budget proposes,
allocates and reserves amounts under that confirmed revenue; a treasury-funded award reuses ordinary WorkTerms and
milestones (funding='treasury'), so verification and acceptance policy apply unchanged. Base issuance is untouched:
this is an application journal over synthetic local-chain fees (development) — never protocol emission."""
import json
import secrets

from .. import history
from ..db import now
from ..errors import ServiceError
from . import journal, terms as terms_mod
from .money import ENV


class Treasury:
    def __init__(self, settings, services, money):
        self.settings, self.svc, self.money = settings, services, money

    def _scope(self, asset):
        return journal.scope(asset, self.money._network(asset))

    def view(self, db, principal, asset='local-chain-token'):
        principal.require('work:read')
        sc = self._scope(asset)
        b = journal.scope_balances(db, principal.workspace).get(sc, {'treasury_revenue': 0, 'treasury_committed': 0, 'treasury_spent': 0, 'treasury_cash': 0, 'treasury_available': 0})
        allocs = [dict(a) for a in db.execute("SELECT * FROM treasury_allocations WHERE workspace=? ORDER BY created_at", (principal.workspace,)).fetchall()]
        exposure = sum(i['max_amount'] for i in db.execute("SELECT max_amount FROM payment_intents WHERE workspace=? AND payer_authority='treasury' AND state IN ('submitted','unknown','expired')", (principal.workspace,)).fetchall())
        out = {'asset': asset, 'scope': sc, 'confirmed_revenue': b['treasury_revenue'], 'reserved_commitments': b['treasury_committed'], 'settled_spending': b['treasury_spent'], 'unresolved_exposure': exposure,
               'available': b['treasury_available'], 'unallocated_confirmed_revenue': b['treasury_revenue'] - b['treasury_committed'] - b['treasury_spent'], 'cash_at_treasury_address': b['treasury_cash'],
               'allocations': allocs, 'provenance': 'confirmed revenue = platform fees observed as settled to the treasury address; synthetic local chain in development; never base issuance',
               'principle': 'MetaStar treasury is funded only by fees and pays judged work as grants; it can never mint base supply'}
        chain = self.money.chain()
        if chain is not None and asset == 'local-chain-token':
            out['on_chain_balance'] = chain.token_c.functions.balanceOf(chain.w3.eth.accounts[6]).call()
            out['note'] = 'on_chain_balance may exceed confirmed revenue only through explicitly labelled test setup; the journal, not the balance, defines availability'
        out['rebuilt_from_journal'] = True
        return out

    def allocate(self, db, principal, body):
        """Propose + allocate + reserve a treasury amount for frozen WorkTerms with funding='treasury' (state machine in one record)."""
        principal.require('work:treasury')
        t = db.execute('SELECT * FROM work_terms WHERE id=? AND workspace=?', (body.get('terms_id'), principal.workspace)).fetchone()
        if t is None or t['state'] != 'frozen':
            raise ServiceError('NOT_FOUND', 'frozen work terms')
        terms = json.loads(t['terms_json'])
        if terms['payment'].get('funding') != 'treasury':
            raise ServiceError('VALIDATION', {'code': 'funding_not_treasury', 'note': "set payment.funding='treasury' in the terms before freezing"})
        amount = terms['payment']['ceiling']
        v = self.view(db, principal, terms['payment']['asset'])
        if amount > v['available']:
            raise ServiceError('BUDGET_EXHAUSTED', {'code': 'treasury_available_insufficient', 'available': v['available'], 'requested': amount, 'note': 'availability = confirmed fee revenue minus commitments and spending; nothing was allocated'})
        if db.execute("SELECT id FROM treasury_allocations WHERE terms_id=? AND state IN ('reserved','awarded')", (t['id'],)).fetchone():
            raise ServiceError('CONFLICT', 'already allocated')
        bid = self._budget(db, principal, terms['payment']['asset'])
        aid = 'ta_' + secrets.token_hex(6)
        db.execute('INSERT INTO treasury_allocations VALUES (?,?,?,?,NULL,?,?,?,?,?,?)', (aid, principal.workspace, bid, t['id'], amount, 'reserved', str(body.get('note', ''))[:256], principal.id, now(), now()))
        journal.post(db, principal.workspace, 'treasury-reserve:%s' % aid, 'treasury_reservation', terms['payment']['asset'], self.money._network(terms['payment']['asset']), ENV[self.settings.provider_mode], [('treasury:committed', amount, 0), ('treasury:available_memo', 0, amount)], 'treasury_allocation', aid, 'treasury commitment reserved under confirmed revenue')
        history.record(db, principal.workspace, principal.id, 'work.treasury', 'treasury_allocation', aid, {'terms_id': t['id'], 'amount': amount, 'state': 'reserved'})
        return dict(self.view(db, principal, terms['payment']['asset']), allocation_id=aid)

    def _budget(self, db, principal, asset):
        b = db.execute("SELECT id FROM treasury_budgets WHERE workspace=? AND asset=?", (principal.workspace, asset)).fetchone()
        if b:
            return b['id']
        bid = 'tb_' + secrets.token_hex(6)
        db.execute('INSERT INTO treasury_budgets VALUES (?,?,?,?,?,?,?,?,?,?,?)', (bid, principal.workspace, 'MetaStar work budget (' + asset + ')', asset, self.money._network(asset), self.money._treasury_address(db, principal.workspace, asset), 'open', json.dumps({'source': 'settled platform fees only', 'bypass': 'none: awards go through WorkTerms, verification and acceptance'}), principal.id, now(), now()))
        return bid

    def on_award(self, db, award, terms):
        a = db.execute("SELECT * FROM treasury_allocations WHERE terms_id=? AND state='reserved'", (award['terms_id'],)).fetchone()
        if a is None:
            raise ServiceError('CONFLICT', {'code': 'treasury_allocation_required', 'note': 'allocate the treasury budget for these terms before awarding'})
        db.execute("UPDATE treasury_allocations SET state='awarded', award_id=?, updated_at=? WHERE id=?", (award['id'], now(), a['id']))
        return a['id']

    def release(self, db, workspace, award_id, spent):
        a = db.execute("SELECT * FROM treasury_allocations WHERE award_id=? AND state='awarded'", (award_id,)).fetchone()
        if a is None:
            return
        release = a['amount'] - spent
        asset = db.execute('SELECT asset FROM treasury_budgets WHERE id=?', (a['budget_id'],)).fetchone()['asset']
        journal.post(db, workspace, 'treasury-release:%s' % a['id'], 'treasury_release', asset, self.money._network(asset), ENV[self.settings.provider_mode], [('treasury:available_memo', a['amount'], 0), ('treasury:committed', 0, a['amount'])], 'treasury_allocation', a['id'], 'commitment closed: spent %d, released %d' % (spent, release))
        db.execute("UPDATE treasury_allocations SET state='closed', updated_at=? WHERE id=?", (now(), a['id']))
