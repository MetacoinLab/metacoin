"""Group D (Order 08 §45–§55): payment intents bound to accepted entitlements, settlement on the private local chain
(exact and capped), lost responses and reconciliation by nonce, authorization expiry, fee credit once and a treasury-funded
award with an accepted negative, verifier compensation independent of the verdict, partial refunds and credits, the
replayable journal with an independent expected-balance model, and adversarial refusals before signing."""
import json
import unittest

from integrations.x402.local_chain import harness
from metacoin_service.tests.test_service import Instance
from metacoin_service.tests.test_work_terms import energy_inputs

HAVE_CHAIN = harness.ARTIFACTS.exists()


@unittest.skipUnless(HAVE_CHAIN, 'needs the built local-chain artifacts')
class WorkMoneyTests(unittest.TestCase):
    def setUp(self):
        self.inst = Instance(provider_mode='test-http'); self.addCleanup(self.inst.close); self.c = self.inst.client; self.H = self.inst.h('owner'); self.R = self.inst.h('reviewer')
        self.inst.settings.limits['test_hooks'] = 1
        self.w = self.inst.worker(); self.addCleanup(self.w.offline)
        self.assertEqual(self.c.put('/api/v1/budgets/workspace', headers=self.H, json={'ceiling': 10000}).status_code, 200)
        self.rails = self.c.get('/api/v1/work/rails', headers=self.H).json(); self.acct = self.rails['local-chain-token']['synthetic_accounts']
        self.chain = self.inst.app.state.services.sales.local_chain()
        self.supply0 = self.chain.token_c.functions.totalSupply().call()
        self.pv = {}
        for name, key in (('alpha', 'provider_a'), ('beta', 'provider_b')):
            p = self.c.post('/api/v1/work/providers', headers=self.H, json={'name': name, 'capabilities': {'kinds': ['energy_audit'], 'verification_classes': ['full_exact'], 'payment_schemes': ['exact', 'upto']}, 'pay_to': self.acct[key]}).json()
            self.pv[name] = {'id': p['id'], 'h': {'Authorization': 'Bearer ' + p['credential']['token']}, 'addr': self.acct[key]}

    def bal(self, who):
        return self.chain.token_c.functions.balanceOf(self.acct[who]).call()

    def terms(self, template='determination', ceiling=10, scheme='exact', bps=0, verifier=0, funding='requester', **kw):
        t = self.c.post('/api/v1/work/terms', headers=self.H, json=dict({'template': template, 'ceiling': ceiling, 'asset': 'local-chain-token'}, **kw)).json()
        pay = dict(t['terms']['payment'], scheme=scheme, fee_policy={'schema': 'metacoin-fee-policy/v1', 'treasury_bps': bps, 'rounding': 'floor_fee_remainder_to_provider'}, funding=funding, verifier_compensation=verifier)
        if verifier:
            pay['verifier_pay_to'] = self.acct['verifier']
        el = dict(t['terms']['eligibility'], payment_schemes=[scheme])
        r = self.c.post('/api/v1/work/terms/' + t['id'], headers=self.H, json={'terms': {'payment': pay, 'eligibility': el}}); self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def awarded(self, outcome='FEASIBLE', price=10, provider='alpha', scheme='exact', **kw):
        t = self.terms(scheme=scheme, amount=price, **kw)
        f = self.c.post('/api/v1/work/terms/' + t['id'] + '/freeze', headers=self.H, json={'inputs': energy_inputs(outcome)}); self.assertEqual(f.status_code, 200, f.text)
        if kw.get('funding') == 'treasury':
            al = self.c.post('/api/v1/work/treasury/allocate', headers=self.H, json={'terms_id': t['id']}); self.assertEqual(al.status_code, 201, al.text)
        r = self.c.post('/api/v1/work/requests', headers=self.H, json={'terms_id': t['id']}).json(); self.c.post('/api/v1/work/requests/' + r['id'] + '/open', headers=self.H, json={})
        o = self.c.post('/api/v1/work/requests/' + r['id'] + '/offers', headers=self.pv[provider]['h'], json={'price_amount': price, 'asset': 'local-chain-token', 'scheme': scheme, 'window_seconds': 3600, 'verification': {'class': 'full_exact'}}); self.assertEqual(o.status_code, 201, o.text)
        a = self.c.post('/api/v1/work/requests/' + r['id'] + '/award', headers=self.H, json={'offer_id': o.json()['id']}); self.assertEqual(a.status_code, 201, a.text)
        return t, f.json(), r, a.json()

    def deliver_accept(self, aid, key='m1'):
        for _ in range(3):
            self.w.run_once()
        v = self.c.post('/api/v1/work/awards/%s/milestones/%s/verify' % (aid, key), headers=self.H, json={}); self.assertEqual(v.status_code, 202, v.text)
        for _ in range(3):
            self.w.run_once()
        d = self.c.post('/api/v1/work/awards/%s/milestones/%s/decide' % (aid, key), headers=self.H, json={'decision': 'accept'}); self.assertEqual(d.status_code, 200, d.text); return d.json()

    def pay(self, eid, body=None, expect_state='settled'):
        i = self.c.post('/api/v1/work/entitlements/' + eid + '/prepare', headers=self.H, json={}); self.assertEqual(i.status_code, 201, i.text); i = i.json()
        a = self.c.post('/api/v1/work/intents/' + i['id'] + '/authorize', headers=self.H, json={}); self.assertEqual(a.status_code, 200, a.text); self.assertEqual(a.json()['state'], 'authorized')
        s = self.c.post('/api/v1/work/intents/' + i['id'] + '/submit', headers=self.H, json=body or {}); self.assertEqual(s.status_code, 200, s.text); s = s.json()
        self.assertEqual(s['state'], expect_state, s); return s

    # J31 + J37 + J38 ------------------------------------------------------------------------------------------------------
    def test_exact_payment_matches_entitlement_refund_and_journal_replay(self):
        t, f, r, a = self.awarded('FEASIBLE', price=10)
        d = self.deliver_accept(a['id']); eid = d['entitlement']['id']
        p0, a0 = self.bal('requester_payer'), self.bal('provider_a')
        s = self.pay(eid)
        self.assertEqual((s['scheme'], s['rail'], s['final_amount'], s['max_amount']), ('exact', 'local-chain', 10, 10)); self.assertTrue(s['transaction_ref'].startswith('0x')); self.assertEqual(s['observations'][-1]['chain_receipt']['status'], 1)
        self.assertEqual((self.bal('requester_payer'), self.bal('provider_a')), (p0 - 10, a0 + 10))
        e = self.c.get('/api/v1/work/entitlements/' + eid, headers=self.H).json(); self.assertEqual((e['state'], e['payment_intent_id']), ('paid', s['id']))
        recs = self.c.get('/api/v1/work/awards/' + a['id'] + '/receipts', headers=self.H).json()['items']
        st = next(x for x in recs if x['kind'] == 'settlement')['statement']['claims']; self.assertEqual((st['final_amount'], st['transaction']), (10, s['transaction_ref']))
        # submitting again replays; the award closes with the paid amount committed and the rest released
        self.assertTrue(self.c.post('/api/v1/work/intents/' + s['id'] + '/submit', headers=self.H, json={}).json().get('replayed'))
        aw = self.c.get('/api/v1/work/awards/' + a['id'], headers=self.H).json(); self.assertEqual((aw['state'], aw['reserved']), ('closed', 10))
        self.assertEqual(aw['milestones'][0]['dimensions'], {'execution': 'completed', 'science': 'FEASIBLE', 'acceptance': 'accepted', 'payment': 'paid'})
        # partial refund under the preauthorized local recovery arrangement; duplicates and over-refunds refused; credit is not currency
        self.assertEqual(self.c.post('/api/v1/work/entitlements/' + eid + '/refund', headers=self.H, json={'amount': 4}).status_code, 403)
        rf = self.c.post('/api/v1/work/entitlements/' + eid + '/refund', headers=self.H, json={'amount': 4, 'provider_preauthorized': True, 'request_key': 'rf-1'}); self.assertEqual(rf.status_code, 201, rf.text); rf = rf.json()
        self.assertEqual((rf['kind'], rf['state'], rf['final_amount']), ('refund', 'settled', 4)); self.assertEqual((self.bal('requester_payer'), self.bal('provider_a')), (p0 - 6, a0 + 6))
        again = self.c.post('/api/v1/work/entitlements/' + eid + '/refund', headers=self.H, json={'amount': 4, 'provider_preauthorized': True, 'request_key': 'rf-1'}).json(); self.assertTrue(again.get('replayed')); self.assertEqual(self.bal('provider_a'), a0 + 6)
        over = self.c.post('/api/v1/work/entitlements/' + eid + '/refund', headers=self.H, json={'amount': 7, 'provider_preauthorized': True}); self.assertEqual(over.json()['detail']['code'], 'refund_exceeds_refundable')
        lost = self.c.post('/api/v1/work/entitlements/' + eid + '/refund', headers=self.H, json={'amount': 2, 'provider_preauthorized': True, '_simulate_lost_response': True}).json(); self.assertEqual(lost['state'], 'unknown')
        rec = self.c.post('/api/v1/work/intents/' + lost['id'] + '/reconcile', headers=self.H, json={}).json(); self.assertEqual(rec['state'], 'settled'); self.assertEqual(self.bal('provider_a'), a0 + 4)
        cr = self.c.post('/api/v1/work/entitlements/' + eid + '/credit', headers=self.H, json={'amount': 3}).json(); self.assertIn('not returned currency', cr['label'])
        # journal replay: balances rebuilt from postings agree with the live views and every entry balances; independent model
        rep = self.c.post('/api/v1/work/journal/replay', headers=self.H, json={}).json()
        self.assertTrue(rep['consistent'], rep['differences']); self.assertTrue(all(c['ok'] for c in rep['invariants']), rep['invariants'])
        sc = next(k for k in rep['scopes'] if k.startswith('local-chain-token'))
        expected = {'obligations_payable': 0, 'exposure_pending': 0, 'paid_out': 10 - 4 - 2, 'refund_claims_open': 0, 'treasury_revenue': 0}   # hand-derived: 10 paid, 4 + 2 returned
        self.assertEqual({k: rep['scopes'][sc][k] for k in expected}, expected)
        self.assertEqual(self.chain.token_c.functions.totalSupply().call(), self.supply0)                                   # no supply mutation anywhere
        j = self.c.get('/api/v1/work/journal', headers=self.H).json(); keys = [e['event_key'] for e in j['entries']]
        self.assertEqual(len(keys), len(set(keys))); self.assertIn('settle:' + s['id'], keys)

    # J32 + J34 + J33 ------------------------------------------------------------------------------------------------------
    def test_capped_metered_below_maximum_expiry_and_lost_response(self):
        # diagnostic template: INDETERMINATE earns 5 of the 10 ceiling; upto authorizes the milestone ceiling, settles 5
        t, f, r, a = self.awarded('INDETERMINATE', price=10, scheme='upto', template='diagnostic_delivery')
        d = self.deliver_accept(a['id']); self.assertEqual((d['payment_class'], d['payable_amount']), ('diagnostic', 5))
        p0 = self.bal('requester_payer')
        s = self.pay(d['entitlement']['id']); self.assertEqual((s['scheme'], s['max_amount'], s['final_amount']), ('upto', 10, 5)); self.assertEqual(self.bal('requester_payer'), p0 - 5)
        aw = self.c.get('/api/v1/work/awards/' + a['id'], headers=self.H).json(); self.assertEqual((aw['state'], aw['reserved'], aw['ceiling']), ('closed', 5, 10))
        tree = self.c.get('/api/v1/budgets/tree', headers=self.H).json()['tree']; node = next(n for n in tree['children'] if n['ref_id'] == 'award:' + a['id'])
        self.assertEqual((node['reserved'], node['committed']), (0, 5))                                                      # unused reservation released, paid amount committed
        # a provider cannot inflate the settled amount: final follows the decision, not a usage statement
        self.assertEqual(self.c.post('/api/v1/work/intents/' + s['id'] + '/submit', headers=self.H, json={'final_amount': 9}).json().get('replayed'), True)
        # lost response: submitted -> unknown -> reconcile by nonce -> settled exactly once
        t2, f2, r2, a2 = self.awarded('FEASIBLE', price=7)
        d2 = self.deliver_accept(a2['id']); p1 = self.bal('requester_payer')
        u = self.pay(d2['entitlement']['id'], body={'_simulate_lost_response': True}, expect_state='unknown')
        self.assertEqual(self.bal('requester_payer'), p1 - 7)                                                                  # executed on the rail, response lost
        self.assertEqual(self.c.post('/api/v1/work/intents/' + u['id'] + '/submit', headers=self.H, json={}).status_code, 409)      # retry refused before reconciliation
        ex = self.c.get('/api/v1/work/exposure', headers=self.H).json(); self.assertIn(u['id'], [x['id'] for x in ex['unresolved']])
        rc = self.c.post('/api/v1/work/intents/' + u['id'] + '/reconcile', headers=self.H, json={}).json()
        self.assertEqual((rc['state'], rc['observations'][-1]['method'], rc['observations'][-1]['used']), ('settled', 'permit2_nonce_bitmap', True)); self.assertEqual(self.bal('requester_payer'), p1 - 7)
        # authorization expiry: work done, authorization expired before settlement -> refused, reconciled as unused, renewal is a NEW intent
        t3, f3, r3, a3 = self.awarded('FEASIBLE', price=3)
        d3 = self.deliver_accept(a3['id']); eid3 = d3['entitlement']['id']
        i3 = self.c.post('/api/v1/work/entitlements/' + eid3 + '/prepare', headers=self.H, json={}).json(); self.c.post('/api/v1/work/intents/' + i3['id'] + '/authorize', headers=self.H, json={})
        with self.inst.app.state.services.db.tx() as db:
            db.execute('UPDATE payment_intents SET valid_until=1 WHERE id=?', (i3['id'],))
        exp = self.c.post('/api/v1/work/intents/' + i3['id'] + '/submit', headers=self.H, json={}); self.assertEqual(exp.json()['code'], 'EXPIRED')
        rc3 = self.c.post('/api/v1/work/intents/' + i3['id'] + '/reconcile', headers=self.H, json={}).json(); self.assertEqual(rc3['state'], 'expired'); self.assertIn('renewal', rc3['reconciliation'])
        e3 = self.c.get('/api/v1/work/entitlements/' + eid3, headers=self.H).json(); self.assertEqual(e3['state'], 'payable')
        s3 = self.pay(eid3); self.assertEqual(s3['final_amount'], 3); self.assertNotEqual(s3['id'], i3['id'])
        self.assertEqual(len([i for i in self.c.get('/api/v1/work/intents', headers=self.H).json()['items'] if i['entitlement_id'] == eid3]), 2)
        rep = self.c.post('/api/v1/work/journal/replay', headers=self.H, json={}).json(); self.assertTrue(rep['consistent'], rep['differences'])

    # J35 + J36 ----------------------------------------------------------------------------------------------------------------
    def test_fee_credited_once_funds_a_treasury_award_with_accepted_negative_and_verifier_paid_on_rejection(self):
        tr0 = self.bal('treasury'); v0 = self.bal('verifier')
        # 10% fee on an 11-ceiling contract: provider 10, treasury fee 1; verifier compensation 2 bound to completion of the audit
        t, f, r, a = self.awarded('FEASIBLE', price=10, ceiling=13, bps=1000, verifier=2)
        d = self.deliver_accept(a['id'])
        ents = [self.c.get('/api/v1/work/entitlements/' + e['id'], headers=self.H).json() for e in self.inst.app.state.services.db and []]
        with self.inst.app.state.services.db.tx() as db:
            rows = db.execute('SELECT id, kind, amount, recipient FROM work_entitlements WHERE award_id=? ORDER BY kind', (a['id'],)).fetchall()
        kinds = {r['kind']: dict(r) for r in rows}
        self.assertEqual({k: v['amount'] for k, v in kinds.items()}, {'fee': 1, 'provider': 10, 'verifier': 2}); self.assertEqual(kinds['fee']['recipient'], self.acct['treasury'])
        for k in ('provider', 'fee', 'verifier'):
            self.pay(kinds[k]['id'])
        self.assertEqual((self.bal('treasury') - tr0, self.bal('verifier') - v0), (1, 2))
        tv = self.c.get('/api/v1/work/treasury', headers=self.H).json()
        self.assertEqual((tv['confirmed_revenue'], tv['available'], tv['reserved_commitments'], tv['settled_spending']), (1, 1, 0, 0))
        # duplicate observation / replayed reconciliation cannot credit the fee twice
        fee_intent = self.c.get('/api/v1/work/entitlements/' + kinds['fee']['id'], headers=self.H).json()['payment_intent_id']
        self.c.post('/api/v1/work/intents/' + fee_intent + '/reconcile', headers=self.H, json={}); self.c.post('/api/v1/work/intents/' + fee_intent + '/submit', headers=self.H, json={})
        self.assertEqual(self.c.get('/api/v1/work/treasury', headers=self.H).json()['confirmed_revenue'], 1)
        # treasury-funded determination contract (ceiling 1) with an accepted NEGATIVE; the treasury pays; no base-supply change
        self.assertEqual(self.c.post('/api/v1/work/treasury/allocate', headers=self.H, json={'terms_id': t['id']}).json()['code'], 'VALIDATION')
        t2, f2, r2, a2 = self.awarded('INFEASIBLE', price=1, ceiling=1, funding='treasury', provider='beta')
        self.assertEqual(self.c.get('/api/v1/work/treasury', headers=self.H).json()['reserved_commitments'], 1)
        d2 = self.deliver_accept(a2['id']); self.assertEqual((d2['evaluation']['science'], d2['decision'], d2['payable_amount']), ('INFEASIBLE', 'accepted', 1))
        b0 = self.bal('provider_b')
        s2 = self.pay(d2['entitlement']['id']); self.assertEqual(s2['payer_authority'], 'treasury'); self.assertEqual(s2['payer'], self.acct['treasury'])
        self.assertEqual((self.bal('provider_b') - b0, self.bal('treasury') - tr0), (1, 0))
        tv2 = self.c.get('/api/v1/work/treasury', headers=self.H).json()
        self.assertEqual((tv2['confirmed_revenue'], tv2['settled_spending'], tv2['available'], tv2['reserved_commitments']), (1, 1, 0, 0))
        self.assertEqual(self.chain.token_c.functions.totalSupply().call(), self.supply0)
        # a second treasury award beyond confirmed revenue is refused before anything is reserved
        t3 = self.terms(ceiling=1, funding='treasury'); self.c.post('/api/v1/work/terms/' + t3['id'] + '/freeze', headers=self.H, json={'inputs': energy_inputs('FEASIBLE')})
        ref = self.c.post('/api/v1/work/treasury/allocate', headers=self.H, json={'terms_id': t3['id']}); self.assertEqual(ref.json()['code'], 'BUDGET_EXHAUSTED')
        # verifier compensation is paid for a completed verification that REJECTS fabricated evidence (verdict-independent)
        t4, f4, r4, a4 = self.awarded('FEASIBLE', price=5, ceiling=7, verifier=2)
        for _ in range(3):
            self.w.run_once()
        jid = self.c.get('/api/v1/work/awards/' + a4['id'], headers=self.H).json()['milestones'][0]['job_id']
        with self.inst.app.state.services.db.tx() as db:
            db.execute("INSERT INTO meta VALUES (?, '1')", ('fault:verification_fail:' + jid,))
        self.c.post('/api/v1/work/awards/%s/milestones/m1/verify' % a4['id'], headers=self.H, json={})
        for _ in range(3):
            self.w.run_once()
        self.c.get('/api/v1/work/awards/' + a4['id'], headers=self.H)
        with self.inst.app.state.services.db.tx() as db:
            rows = {r['kind']: dict(r) for r in db.execute('SELECT id, kind, amount, state FROM work_entitlements WHERE award_id=?', (a4['id'],)).fetchall()}
        self.assertEqual(sorted(rows), ['verifier']); self.assertEqual(rows['verifier']['amount'], 2)
        v1 = self.bal('verifier'); self.pay(rows['verifier']['id']); self.assertEqual(self.bal('verifier') - v1, 2)
        rej = self.c.post('/api/v1/work/awards/%s/milestones/m1/decide' % a4['id'], headers=self.H, json={'decision': 'reject', 'reason': 'audit failed'}).json(); self.assertEqual(rej['decision'], 'rejected')
        rep = self.c.post('/api/v1/work/journal/replay', headers=self.H, json={}).json(); self.assertTrue(rep['consistent'], rep['differences']); self.assertTrue(all(c['ok'] for c in rep['invariants']), [c for c in rep['invariants'] if not c['ok']])

    # §45 + §55 adversarial refusals before signing --------------------------------------------------------------------------------
    def test_refusals_before_signing_and_stale_revision(self):
        t, f, r, a = self.awarded('FEASIBLE', price=6)
        d = self.deliver_accept(a['id']); eid = d['entitlement']['id']
        for body, code in (({'recipient': self.acct['provider_b']}, 'recipient_substitution'), ({'network': 'eip155:1'}, 'wrong_network'), ({'asset': 'action-units'}, 'asset_mismatch'), ({'amount': 7}, 'amount_above_entitlement')):
            resp = self.c.post('/api/v1/work/entitlements/' + eid + '/prepare', headers=self.H, json=body); self.assertIn(resp.status_code, (403, 409), resp.text); self.assertEqual((resp.json().get('detail') or {}).get('code'), code, resp.text)
        self.assertEqual(self.c.get('/api/v1/work/intents', headers=self.H).json()['items'], [])
        # missing acceptance: a milestone without a decision has no entitlement to pay
        t2, f2, r2, a2 = self.awarded('FEASIBLE', price=6)
        self.assertIsNone(self.c.get('/api/v1/work/awards/' + a2['id'], headers=self.H).json()['milestones'][0]['entitlement_id'])
        # stale contract revision: the terms superseded after the award -> prepare refused
        am = self.c.post('/api/v1/work/terms/' + t['id'] + '/amend', headers=self.H, json={'terms': {'title': 'renamed'}}).json(); self.c.post('/api/v1/work/terms/' + am['id'] + '/freeze', headers=self.H, json={})
        st = self.c.post('/api/v1/work/entitlements/' + eid + '/prepare', headers=self.H, json={}); self.assertEqual(st.json()['detail']['code'], 'stale_contract_revision')
        # a viewer / provider cannot pay; production guard message is explicit in the rails view
        self.assertEqual(self.c.post('/api/v1/work/entitlements/' + eid + '/prepare', headers=self.pv['alpha']['h'], json={}).status_code, 403)
        self.assertIn('refused by the payer guard', self.rails['local-chain-token']['production'])


if __name__ == '__main__':
    unittest.main()
