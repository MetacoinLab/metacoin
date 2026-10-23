"""Complete economic custody separation (order 2026-10-23 group A; journeys 4–8, 40 at unit level): requester funding, machine
spending and supplier settlement all pass through external signers with durable policy and context binding; the service
holds no key and has no fallback; altered terms are refused before signing; an unavailable signer leaves the action waiting;
development-custody creation can be disabled for a scenario."""
import json
import time
import unittest

from metacoin_service.tests.test_native_missions import MissionBase
from metacoin_service.tests.test_native_signer import SignerProcess


class CustodyTests(MissionBase):
    def setUp(self):
        super().setUp(); self.sg = SignerProcess(); self.addCleanup(lambda: self.sg.close() if self.sg.proc.poll() is None else None)

    def enroll(self, name, ceiling=1000, policy=None):
        st, e = self.sg.client.call('POST', '/enroll', {'name': name, 'ceiling': ceiling}); self.assertEqual(st, 200, e)
        r = self.c.post('/api/v1/native/actors/enroll', headers=self.H, json={'name': name, 'public_hex': e['public_hex'], 'key_id': e['key_id'], 'signer_id': e['signer_id']}); self.assertEqual(r.status_code, 201, r.text); a = r.json()
        self.sg.client.call('POST', '/bind', {'actor': name, 'account': a['account_id']})
        if policy:
            self.assertEqual(self.sg.client.set_policy(name, policy)[0], 200)
        self.actors[name] = {'account': a['account_id'], 'principal_id': a['principal_id'], 'h': {'Authorization': 'Bearer ' + a['credential']['token']}, 'key_id': e['key_id']}; return a

    def sign_all(self, who, mutate=None):
        """The actor's own agent: fetch pending requests, sign at the signer (with context), submit. Returns the processed list."""
        pend = self.c.get('/api/v1/native/signing/pending', headers=self.actors[who]['h']).json(); out = []
        for req in pend['items']:
            msg, ctx = req['message'], req['context']
            if mutate:
                msg, ctx = mutate(dict(msg), dict(ctx))
            st, sig = self.sg.client.sign(who, msg, context=ctx)
            if st != 200:
                out.append({'request': req['id'], 'refused': sig['code']}); continue
            r = self.c.post('/api/v1/native/signing/requests/%s/submit' % req['id'], headers=self.actors[who]['h'], json={'signature_hex': sig['signature_hex'], 'key_id': sig['key_id']}); out.append({'request': req['id'], 'status': r.status_code, 'body': r.json(), 'defects': self.defects()[-1500:] if r.status_code >= 400 else None})
        return out

    def test_signed_funding_spending_and_settlement_without_fallback(self):
        req = self.enroll('xreq', 1000); mach = self.enroll('xmach', 500, policy={'permitted_recipients': [self.actors['supplier']['account']], 'require_context': True}); sup = self.enroll('xsup', 500)
        self.actors['requester'] = self.actors['xreq']; self.R = self.actors['xreq']['h']; self.fund('xreq', 300)
        prov = self.c.post('/api/v1/work/providers', headers=self.H, json={'name': 'xmach-provider', 'principal_id': mach['principal_id'], 'capabilities': {'kinds': ['terrain_tile', 'route_feasibility'], 'verification_classes': ['analytical', 'full_exact'], 'payment_schemes': ['exact']}}).json()
        self.c.post('/api/v1/native/fixtures/fund', headers=self.H, json={'account_id': self.actors['supplier']['account'], 'amount': 0}) if False else None
        m = self.mission(policy={'compute_cpu_ms': 20, 'permitted_providers': [prov['id']], 'followon_provider_account': self.actors['supplier']['account']}); mid = m['id']; self.assertEqual(m['policy']['permitted_providers'], [prov['id']])
        # journey 4 (funding): the requester's mission payment needs its signer; the controller issues signing requests, nothing settles
        s = self.step(mid); self.assertEqual(s['performed']['action'], 'open_request'); self.offer_as_machine_h(mid, 'acquire', 4, self.actors['xmach']['h'])
        s = self.step(mid); self.assertEqual(s['performed']['action'], 'award')
        for _ in range(3):
            self.w.run_once()
        s = self.step(mid); self.assertEqual(s['performed']['action'], 'verify')
        for _ in range(3):
            self.w.run_once()
        s = self.step(mid); self.assertEqual((s['performed']['action'], s['result']['decision']), ('decide', 'accepted'))
        s = self.step(mid); self.assertEqual(s['performed']['action'], 'pay'); self.assertEqual(len(s['result']['awaiting_signature']), 1); self.assertEqual(self.bal('xmach'), 0)
        ex = self.c.get('/api/v1/missions/%s/explain' % mid, headers=self.R).json(); self.assertTrue(any(a['action'] == 'await_signature' and a['milestone'] == 'settle_acquire' for a in ex['actions'])); self.assertEqual(ex['context']['custody'][:15], 'external-signer')
        s = self.step(mid); self.assertEqual((s['performed']['action'], s['performed']['milestone']), ('open_request', 'compute')); self.assertEqual(self.ms(mid, 'settle_acquire')['state'], 'ready')   # other work continues; the settlement waits for the signer
        # journey 5: altered recipient / amount / context are refused before signing or at submission; balances unchanged
        bad = self.sign_all('xreq', mutate=lambda m_, c_: (dict(m_, recipient=self.actors['supplier']['account']), c_)); self.assertEqual(bad[0]['refused'], 'context_recipient_mismatch')
        bad = self.sign_all('xreq', mutate=lambda m_, c_: (dict(m_, amount_ceiling=m_['amount_ceiling'] + 1), c_)); self.assertEqual(bad[0]['refused'], 'context_amount_mismatch')
        bad = self.sign_all('xreq', mutate=lambda m_, c_: (m_, dict(c_, obligation_id='ob_other'))); self.assertEqual(bad[0]['refused'], 'policy_hash_mismatch')
        bad = self.sign_all('xreq', mutate=lambda m_, c_: (dict(m_, authority='dom-z'), c_)); self.assertIn(bad[0]['refused'], ('intent', 'domain_not_pinned'))
        self.assertEqual((self.bal('xmach'), self.bal('xreq', 'reserved')), (0, 4))
        ok = self.sign_all('xreq'); self.assertEqual(ok[0]['status'], 200, ok); self.assertTrue(ok[0]['body']['executed']); self.assertEqual(self.bal('xmach'), 4); self.assertEqual(self.ms(mid, 'settle_acquire')['state'], 'settled')
        # the compute milestone: 40 + fee 2 + verifier 3 -> three settlement signatures from the requester
        self.offer_as_machine_h(mid, 'compute', 40, self.actors['xmach']['h']); s = self.step(mid); self.assertEqual(s['performed']['action'], 'award')
        for _ in range(3):
            self.w.run_once()
        self.step(mid)
        for _ in range(3):
            self.w.run_once()
        s = self.step(mid); self.assertEqual(s['result']['decision'], 'accepted'); s = self.step(mid); self.assertEqual((s['performed']['action'], len(s['result']['awaiting_signature'])), ('pay', 3))
        # journey 40 (signer unavailable): the signer is stopped; the action stays pending; nothing moves; no fallback
        self.sg.proc.terminate(); self.sg.proc.wait(timeout=10); st, _ = self.sg.client.health(); self.assertEqual(st, 0)
        s = self.step(mid); self.assertEqual(s['performed']['action'], 'await_signature'); self.assertEqual(self.bal('xmach'), 4); self.assertEqual(self.bal('xreq', 'reserved'), 45)
        self.sg.start(); ok = self.sign_all('xreq'); self.assertEqual([o['status'] for o in ok], [200, 200, 200]); self.assertEqual((self.bal('xmach'), self.bal('verifier'), self.bal(self.dom['accounts']['treasury'])), (44, 3, 2))
        self.assertEqual(self.ms(mid, 'settle')['state'], 'settled')
        # journey 4 (machine spending): the machine buys follow-on compute from settled earnings with a reservation intent its signer must sign
        s = self.step(mid, self.actors['xmach']['h']); self.assertEqual(s['performed']['action'], 'reserve'); self.assertTrue(s['result'].get('awaiting_signature')); self.assertEqual(self.bal('xmach', 'reserved'), 0)
        pend = self.c.get('/api/v1/native/signing/pending', headers=self.actors['xmach']['h']).json(); self.assertEqual(pend['items'][0]['kind'], 'reservation'); self.assertEqual(pend['items'][0]['context']['kind'], 'reservation')
        ok = self.sign_all('xmach'); self.assertEqual(ok[0]['status'], 200, ok); self.assertEqual(self.bal('xmach', 'reserved'), 20); self.assertEqual(self.ms(mid, 'followon')['state'], 'executing'); rid = self.ms(mid, 'followon')['binding']['reservation_id']
        self.c.post('/api/v1/resources/reservations/%s/activate' % rid, headers=self.S, json={}); self.c.post('/api/v1/resources/reservations/%s/deliver' % rid, headers=self.S, json={'task': {'kind': 'terrain_tile', 'source': 'apollo11_32'}})
        s = self.step(mid, self.actors['xmach']['h']); self.assertEqual(s['performed']['action'], 'settle_reservation'); self.assertGreater(self.bal('supplier'), 0)
        # journey 8 at the policy level: the machine's durable policy only permits the supplier as recipient
        st, pol = self.sg.client.policy('xmach'); self.assertEqual(pol['policy']['permitted_recipients'], [self.actors['supplier']['account']])
        msg = self.c.post('/api/v1/native/transfers/prepare', headers=self.actors['xmach']['h'], json={'to_account': self.actors['verifier']['account'], 'amount': 1, 'request_id': 'off-policy'}).json()['message']
        st, r = self.sg.client.sign('xmach', msg); self.assertEqual(r['code'], 'context_required')
        st, r = self.sg.client.sign('xmach', dict(msg, policy_hash='0' * 64), context={'kind': 'transfer', 'recipient': self.actors['verifier']['account'], 'amount': 1}); self.assertEqual(r['code'], 'policy_hash_mismatch')
        from metacoin_service.native import protocol as P
        ctx = {'kind': 'transfer', 'recipient': self.actors['verifier']['account'], 'amount': 1}; st, r = self.sg.client.sign('xmach', dict(msg, policy_hash=P.digest(ctx)), context=ctx); self.assertEqual(r['code'], 'policy_recipient')
        # supplier settlement through its signer: the supplier (external) pays the verifier 1 with a signed transfer
        sup_h = self.actors['xsup']['h']; self.c.post('/api/v1/native/fixtures/fund', headers=self.H, json={'account_id': sup['account_id'], 'amount': 5})
        msg = self.c.post('/api/v1/native/transfers/prepare', headers=sup_h, json={'to_account': self.actors['verifier']['account'], 'amount': 1, 'request_id': 'sup-pay'}).json()['message']
        st, sig = self.sg.client.sign('xsup', msg); r = self.c.post('/api/v1/native/transfers/submit', headers=sup_h, json={'message': msg, 'signature_hex': sig['signature_hex'], 'key_id': sig['key_id']}); self.assertEqual(r.status_code, 201, r.text)
        self.assertEqual(self.c.post('/api/v1/native/transfers', headers=sup_h, json={'to_account': self.actors['verifier']['account'], 'amount': 1, 'request_id': 'sup-unsigned'}).json()['detail']['code'], 'signature_required')
        self.assertTrue(self.c.get('/api/v1/native/audit', headers=self.H).json()['conserved'])
        self.assertFalse(list((self.inst.settings.keys_dir / 'native').glob('actor-' + req['account_id'] + '*')))

    def offer_as_machine_h(self, mid, key, price, h):
        rid = self.ms(mid, key)['binding']['request_id']; r = self.c.post('/api/v1/work/requests/' + rid + '/offers', headers=h, json={'price_amount': price, 'asset': self.asset, 'scheme': 'exact', 'window_seconds': 3600, 'verification': {'class': 'analytical'}}); self.assertEqual(r.status_code, 201, r.text)

    def test_sequence_numbers_allocate_at_prepare_and_authorize_once(self):
        # a signed-but-never-submitted intent (a lost race, a voided request) must not block the account's next intent at the signer
        self.enroll('xseq', 1000); self.fund('xseq', 50); h = self.actors['xseq']['h']; to = self.actors['supplier']['account']
        m1 = self.c.post('/api/v1/native/transfers/prepare', headers=h, json={'to_account': to, 'amount': 5, 'request_id': 'q1'}).json()['message']; m2 = self.c.post('/api/v1/native/transfers/prepare', headers=h, json={'to_account': to, 'amount': 6, 'request_id': 'q2'}).json()['message']
        self.assertNotEqual(m1['nonce'], m2['nonce'])                                                 # distinct sequences allocated at preparation
        s1 = self.sg.client.sign('xseq', m1)[1]; st2, s2 = self.sg.client.sign('xseq', m2); self.assertEqual(st2, 200, s2)
        r2 = self.c.post('/api/v1/native/transfers/submit', headers=h, json={'message': m2, 'signature_hex': s2['signature_hex'], 'key_id': s2['key_id']}); self.assertEqual(r2.status_code, 201, r2.text)   # the later sequence settles first
        r1 = self.c.post('/api/v1/native/transfers/submit', headers=h, json={'message': m1, 'signature_hex': s1['signature_hex'], 'key_id': s1['key_id']}); self.assertEqual(r1.status_code, 201, r1.text)   # the earlier one still authorizes exactly once
        m3 = self.c.post('/api/v1/native/transfers/prepare', headers=h, json={'to_account': to, 'amount': 1, 'request_id': 'q3'}).json()['message']; st3, s3 = self.sg.client.sign('xseq', m3); self.assertEqual(st3, 200, s3)
        forged = dict(m3, nonce=m1['nonce'], request_id='q4'); st, sf = self.sg.client.sign('xseq', forged); self.assertEqual(sf.get('code'), 'sequence')          # the signer refuses a reused sequence
        from metacoin_service import crypto
        from metacoin_service.native import protocol as P
        kf = next(f for f in (self.sg.home / 'keys').iterdir() if 'xseq' in f.name and '-e1-' in f.name); sig = crypto.sign(crypto.load_signing_key(kf), P.signing_bytes('intent', forged))
        r = self.c.post('/api/v1/native/transfers/submit', headers=h, json={'message': forged, 'signature_hex': sig, 'key_id': s1['key_id']}); self.assertEqual((r.status_code, r.json()['detail']['code']), (409, 'nonce_replayed'))   # and so does the service
        ahead = dict(m3, nonce=m3['nonce'].rsplit('-', 1)[0] + '-999', request_id='q5'); sig = crypto.sign(crypto.load_signing_key(kf), P.signing_bytes('intent', ahead))
        r = self.c.post('/api/v1/native/transfers/submit', headers=h, json={'message': ahead, 'signature_hex': sig, 'key_id': s1['key_id']}); self.assertEqual(r.json()['detail']['code'], 'nonce_stale')                                 # never allocated
        self.assertEqual(self.bal('xseq'), 39); self.assertTrue(self.c.get('/api/v1/native/audit', headers=self.H).json()['conserved'])

    def test_development_custody_can_be_disabled_for_a_scenario(self):
        self.inst.settings.require_external_custody = True
        r = self.c.post('/api/v1/native/actors', headers=self.H, json={'name': 'dev-x'}); self.assertEqual(r.status_code, 501, r.text); self.assertEqual(r.json()['detail']['code'], 'development_custody_disabled')
        cap = self.c.get('/api/v1/capabilities', headers=self.H).json()['signer']; self.assertEqual(cap['custody']['development_custody_creation'], 'disabled'); self.assertIn('no path', cap['custody']['fallback'])
        self.inst.settings.require_external_custody = False
        h = self.sg.client.health()[1]; self.assertIn('process-separated', h['isolation_mode']); self.assertIn('not consensus', h['authority_note'])


if __name__ == '__main__':
    unittest.main()
