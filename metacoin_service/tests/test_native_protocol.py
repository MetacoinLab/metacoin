"""Native space-work and settlement protocol (order 2026-10-22): the 80-unit native loop with x402 disabled (§14), the
signed-intent state machine, conservation, and the treasury's monetary separation (§15). Runs against a disposable
instance in simulation mode; no public chain, no x402 facilitator, no external funds."""
import json
import os
import unittest

from metacoin_service.tests.test_work_terms import TermsInstance, energy_inputs

NATIVE_DISABLED_X402 = {'METACOIN_X402_DISABLED': '1'}


class NativeBase(unittest.TestCase):
    def setUp(self):
        for k, v in NATIVE_DISABLED_X402.items():
            os.environ[k] = v
        self.addCleanup(lambda: [os.environ.pop(k, None) for k in NATIVE_DISABLED_X402])
        self.inst = TermsInstance(); self.addCleanup(self.inst.close); self.inst.settings.x402_disabled = True; self.inst.reopen(); self.c = self.inst.client; self.H = self.inst.h('owner')
        self.w = self.inst.worker(); self.addCleanup(self.w.offline)
        self.assertEqual(self.c.put('/api/v1/budgets/workspace', headers=self.H, json={'ceiling': 100000}).status_code, 200)
        d = self.c.post('/api/v1/native/domain', headers=self.H, json={'domain_id': 'dom-a', 'operator': 'test operator', 'host': 'this host'}); self.assertEqual(d.status_code, 201, d.text)
        self.dom = d.json(); self.asset = self.dom['default_asset']
        self.actors = {}
        for name in ('requester', 'machine', 'verifier', 'supplier'):
            a = self.c.post('/api/v1/native/actors', headers=self.H, json={'name': name}); self.assertEqual(a.status_code, 201, a.text); a = a.json()
            self.actors[name] = {'account': a['account_id'], 'principal_id': a['principal_id'], 'h': {'Authorization': 'Bearer ' + a['credential']['token']}}

    def defects(self):
        p = self.inst.home / 'logs' / 'defects.log'
        return p.read_text()[-3000:] if p.exists() else ''

    def fund(self, who, amount):
        r = self.c.post('/api/v1/native/fixtures/fund', headers=self.H, json={'account_id': self.actors[who]['account'], 'amount': amount}); self.assertEqual(r.status_code, 201, r.text); return r.json()

    def provider(self, who, kinds=('energy_audit',)):
        p = self.c.post('/api/v1/work/providers', headers=self.H, json={'name': who + '-provider', 'principal_id': self.actors[who]['principal_id'], 'capabilities': {'kinds': list(kinds), 'verification_classes': ['full_exact'], 'payment_schemes': ['exact', 'upto']}})
        self.assertEqual(p.status_code, 201, p.text); p = p.json(); self.assertIsNone(p.get('credential')); self.assertEqual(p['pay_to'], self.actors[who]['account']); return p

    def bal(self, who, cat='available'):
        acct = self.actors[who]['account'] if who in self.actors else who
        r = self.c.get('/api/v1/native/accounts/' + acct, headers=self.H); self.assertEqual(r.status_code, 200, r.text); return r.json()[cat]

    def terms(self, requester_h, price, bps=0, verifier=0, verifier_pay_to=None, funding='requester', kind='energy_audit', scheme='exact'):
        ceiling = price + price * bps // 10000 + verifier                   # the frozen ceiling covers every leg: provider price, treasury fee, verifier compensation
        t = self.c.post('/api/v1/work/terms', headers=requester_h, json={'template': 'determination', 'ceiling': ceiling, 'amount': price, 'asset': self.asset, 'kind': kind}); self.assertEqual(t.status_code, 201, t.text); t = t.json()
        pay = dict(t['terms']['payment'], scheme=scheme, fee_policy={'schema': 'metacoin-fee-policy/v1', 'treasury_bps': bps, 'rounding': 'floor_fee_remainder_to_provider'}, funding=funding, verifier_compensation=verifier)
        if verifier:
            pay['verifier_pay_to'] = verifier_pay_to
        el = dict(t['terms']['eligibility'], payment_schemes=[scheme])
        r = self.c.post('/api/v1/work/terms/' + t['id'], headers=requester_h, json={'terms': {'payment': pay, 'eligibility': el}}); self.assertEqual(r.status_code, 200, r.text); return r.json()

    def award(self, requester_h, provider_h, price, outcome='FEASIBLE', expect=201, **kw):
        t = self.terms(requester_h, price, **kw)
        f = self.c.post('/api/v1/work/terms/' + t['id'] + '/freeze', headers=requester_h, json={'inputs': energy_inputs(outcome)}); self.assertEqual(f.status_code, 200, f.text)
        if kw.get('funding') == 'treasury':
            al = self.c.post('/api/v1/work/treasury/allocate', headers=self.H, json={'terms_id': t['id']}); self.assertEqual(al.status_code, 201, al.text)
        r = self.c.post('/api/v1/work/requests', headers=requester_h, json={'terms_id': t['id']}); self.assertEqual(r.status_code, 201, r.text); r = r.json()
        self.assertEqual(self.c.post('/api/v1/work/requests/' + r['id'] + '/open', headers=requester_h, json={}).status_code, 200)
        o = self.c.post('/api/v1/work/requests/' + r['id'] + '/offers', headers=provider_h, json={'price_amount': price, 'asset': self.asset, 'scheme': kw.get('scheme', 'exact'), 'window_seconds': 3600, 'verification': {'class': 'full_exact'}}); self.assertEqual(o.status_code, 201, o.text + self.defects())
        a = self.c.post('/api/v1/work/requests/' + r['id'] + '/award', headers=requester_h, json={'offer_id': o.json()['id']}); self.assertEqual(a.status_code, expect, a.text)
        return t, r, a.json()

    def deliver_accept(self, aid, requester_h, key='m1'):
        for _ in range(3):
            self.w.run_once()
        v = self.c.post('/api/v1/work/awards/%s/milestones/%s/verify' % (aid, key), headers=requester_h, json={}); self.assertEqual(v.status_code, 202, v.text)
        for _ in range(3):
            self.w.run_once()
        d = self.c.post('/api/v1/work/awards/%s/milestones/%s/decide' % (aid, key), headers=requester_h, json={'decision': 'accept'}); self.assertEqual(d.status_code, 200, d.text); return d.json()

    def pay_all(self, aid, payer_h):
        aw = self.c.get('/api/v1/work/awards/' + aid, headers=self.H).json()
        out = {}
        for e in aw['entitlements']:
            if e['state'] != 'payable':
                continue
            i = self.c.post('/api/v1/work/entitlements/' + e['id'] + '/prepare', headers=payer_h, json={}); self.assertEqual(i.status_code, 201, i.text); i = i.json()
            self.assertEqual(i['rail'], 'native')
            a = self.c.post('/api/v1/work/intents/' + i['id'] + '/authorize', headers=payer_h, json={}); self.assertEqual(a.status_code, 200, a.text)
            s = self.c.post('/api/v1/work/intents/' + i['id'] + '/submit', headers=payer_h, json={}); self.assertEqual(s.status_code, 200, s.text); s = s.json()
            self.assertEqual(s['state'], 'settled', s); self.assertTrue(s['transaction_ref'].startswith('native:'))
            out[e['kind']] = s
        return out


class NativeLoopTests(NativeBase):
    def test_capabilities_report_native_core_without_x402(self):
        cap = self.c.get('/api/v1/capabilities', headers=self.H).json()
        self.assertEqual(cap['native']['domain'], 'dom-a'); self.assertFalse(cap['x402']['required_by_native_core']); self.assertTrue(cap['x402']['disabled'])
        spec = self.c.get('/api/v1/native/spec', headers=self.H).json(); self.assertEqual(spec['protocol'], 'metacoin-native/v0'); self.assertEqual(len(spec['finality']), 7)

    def test_eighty_unit_native_loop(self):
        """§14: requester 80 -> machine 40 + verifier 3 + treasury 2; machine buys for 10 -> supplier 9 + treasury 1; treasury award 2."""
        R, M, V, S = (self.actors[k]['h'] for k in ('requester', 'machine', 'verifier', 'supplier'))
        self.provider('machine'); self.provider('supplier')
        tre = self.dom['accounts']['treasury']
        # the machine cannot buy before it has earned: its account is empty
        t, r, a = self.award(M, S, 9, expect=400, bps=1112)
        self.assertEqual(a['detail']['code'], 'insufficient_available')
        self.fund('requester', 80)
        self.assertEqual(self.bal('requester'), 80)
        t, r, a = self.award(R, M, 40, bps=500, verifier=3, verifier_pay_to=self.actors['verifier']['account'])
        self.assertEqual((a['ceiling'], self.bal('requester'), self.bal('requester', 'reserved')), (45, 35, 45))
        d = self.deliver_accept(a['id'], R); self.assertEqual(d['decision'], 'accepted')
        paid = self.pay_all(a['id'], R)
        self.assertEqual(sorted(paid), ['fee', 'provider', 'verifier'])
        self.assertEqual((self.bal('requester'), self.bal('machine'), self.bal('verifier'), self.bal(tre)), (35, 40, 3, 2))
        self.assertEqual(self.bal('requester', 'reserved'), 0)
        aw = self.c.get('/api/v1/work/awards/' + a['id'], headers=self.H).json(); self.assertEqual(aw['state'], 'closed')
        # the machine buys from the supplier: price 9, fee 1 (1112 bps floors to 1)
        t2, r2, a2 = self.award(M, S, 9, bps=1112)
        self.assertEqual((a2['ceiling'], self.bal('machine'), self.bal('machine', 'reserved')), (10, 30, 10))
        d2 = self.deliver_accept(a2['id'], M); paid2 = self.pay_all(a2['id'], M)
        self.assertEqual(sorted(paid2), ['fee', 'provider'])
        self.assertEqual((self.bal('machine'), self.bal('supplier'), self.bal(tre)), (30, 9, 3))
        # treasury-funded award of 2 to the machine (from fee revenue 3; never issuance)
        tv = self.c.get('/api/v1/work/treasury', headers=self.H, params={'asset': self.asset}).json(); self.assertEqual(tv['available'], 3)
        t3, r3, a3 = self.award(self.H, M, 2, funding='treasury')
        self.assertEqual((self.bal(tre), self.bal(tre, 'reserved')), (1, 2))
        d3 = self.deliver_accept(a3['id'], self.H); paid3 = self.pay_all(a3['id'], self.H)
        self.assertEqual(sorted(paid3), ['provider'])
        final = {k: self.bal(k) for k in ('requester', 'machine', 'supplier', 'verifier')}; final['treasury'] = self.bal(tre)
        self.assertEqual(final, {'requester': 35, 'machine': 32, 'supplier': 9, 'verifier': 3, 'treasury': 1})
        self.assertEqual(sum(final.values()), 80)
        au = self.c.get('/api/v1/native/audit', headers=self.H).json()
        self.assertTrue(au['conserved']); self.assertEqual((au['total_held'], au['fixture_funding'], au['held_by_category']['reserved']), (80, 80, 0)); self.assertEqual(au['negative_balances'], [])
        # a treasury award beyond confirmed revenue is refused, not minted
        t4 = self.terms(self.H, 5, funding='treasury'); self.c.post('/api/v1/work/terms/' + t4['id'] + '/freeze', headers=self.H, json={'inputs': energy_inputs('FEASIBLE')})
        al = self.c.post('/api/v1/work/treasury/allocate', headers=self.H, json={'terms_id': t4['id']}); self.assertEqual(al.status_code, 400, al.text); self.assertIn('insufficient', al.json()['detail']['code'])
        # every settlement is a signed native intent with a receipt and a commit sequence
        ints = self.c.get('/api/v1/native/intents', headers=self.H).json()['items']
        settled = [i for i in ints if i['state'] == 'settled']
        self.assertEqual(len(settled), 6); self.assertEqual(sorted(i['commit_sequence'] for i in settled), list(range(1, 7)))
        self.assertTrue(all(i['receipt'] and i['finality'] == 'domain_local_settlement' for i in settled))
        rep = self.c.post('/api/v1/work/journal/replay', headers=self.H, json={}).json(); self.assertTrue(rep.get('consistent', rep.get('ok', False)), rep)


if __name__ == '__main__':
    unittest.main()
