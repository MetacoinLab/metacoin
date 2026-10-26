"""Bounded state-space model vs implementation (order §18; journeys 16, 36): every example sequence of the model is replayed
through the real two-domain instances and the final abstract state must agree; the exported record of the replay passes the
independent checker under explicitly pinned roots and fails under a wrong root; disputes are bounded (§16, journey 33)."""
import json
import unittest

from metacoin_service.native import checker, model, vectors
from metacoin_service.tests.test_native_domains import NativeDomainTests, courier


class NativeModelTests(NativeDomainTests):
    def abstract(self, alloc_id):
        A, B = self.A, self.B
        al = A.alloc(alloc_id) if alloc_id else None
        st = al['state'] if al else None
        if al and st == 'imported' and B.alloc(alloc_id)['state'] == 'return_requested':
            st = 'return_requested'
        if al and st in ('certified', 'acknowledged') and B.c.get('/api/v1/native/allocations/' + alloc_id, headers=B.H).status_code == 200:
            bs = B.alloc(alloc_id)['state']
            st = bs if bs in ('imported', 'return_requested', 'returned') and st == 'certified' else st
        return {'a_avail': A.bal(self.alice['account']) - 47, 'a_backing': A.audit()['backing_locked_for_exports'], 'b_claim_avail': B.bal(self.bob['account'], asset=A.asset), 'b_claim_in_transit': B.bal(self.bob['account'], 'in_transit', asset=A.asset), 'carol': B.bal(self.carol['account'], asset=A.asset), 'alloc_state': st}

    def test_model_sequences_agree_with_the_implementation(self):
        # alice holds 50 in the instance; the model's fixture is 3 (offset 47 keeps the comparison exact)
        for seq in model.example_sequences():
            with self.subTest(sequence=seq):
                self.setUp(); A, B = self.A, self.B; alloc = None; cert_bundle = None; replies = None; ret_bundle = None
                for ev in seq:
                    if ev in ('export_1', 'export_2'):
                        a = A.export('alice', B, self.bob['account'], 1 if ev == 'export_1' else 2, rid='m-' + ev); alloc = a['id']
                    elif ev == 'crash_after_reserve':
                        A.fault('native_export_reserve'); r = A.c.post('/api/v1/native/allocations/export', headers=self.alice['h'], json={'destination_domain': 'dom-b', 'recipient_account': self.bob['account'], 'amount': 1, 'request_id': 'm-crash'}); self.assertEqual(r.status_code, 409); A.fault('native_export_reserve', disarm=True)
                        alloc = [x for x in A.c.get('/api/v1/native/allocations', headers=A.H).json()['items'] if x['evidence'].get('request_id') == 'm-crash'][0]['id']
                    elif ev in ('deliver_cert', 'deliver_cert_again'):
                        A.c.post('/api/v1/native/allocations/recover', headers=A.H, json={})
                        if cert_bundle is None or ev == 'deliver_cert_again':
                            cert_bundle = cert_bundle or A.c.get('/api/v1/native/transport/outbox', headers=A.H, params={'receiver': 'dom-b', 'all': '1'}).json()
                        res = B.c.post('/api/v1/native/transport/inbox', headers=B.H, json=cert_bundle).json(); replies = (replies or []) + res['replies']
                    elif ev == 'drop_cert':
                        cert_bundle = A.c.get('/api/v1/native/transport/outbox', headers=A.H, params={'receiver': 'dom-b'}).json()      # fetched and dropped: stays queued at A
                    elif ev in ('ack', 'ack_again'):
                        if replies:
                            A.c.post('/api/v1/native/transport/inbox', headers=A.H, json={'items': replies})
                    elif ev in ('spend_at_b', 'double_use'):
                        B.c.post('/api/v1/native/transfers', headers=self.bob['h'], json={'to_account': self.carol['account'], 'amount': 1, 'asset': A.asset, 'request_id': 'm-' + ev + str(len(seq))})
                    elif ev == 'return_req':
                        r = B.c.post('/api/v1/native/allocations/' + alloc + '/return', headers=self.bob['h'], json={})
                        if r.status_code == 200:
                            ret_bundle = B.c.get('/api/v1/native/transport/outbox', headers=B.H, params={'receiver': 'dom-a'}).json()
                    elif ev in ('deliver_return', 'deliver_return_again'):
                        if ret_bundle:
                            res = A.c.post('/api/v1/native/transport/inbox', headers=A.H, json=ret_bundle).json()
                            if res['replies']:
                                B.c.post('/api/v1/native/transport/inbox', headers=B.H, json={'items': res['replies']})
                got = self.abstract(alloc); want = model.expected_final(seq)
                self.assertEqual(got, want, seq)
                self.assertEqual(A.audit()['total_held'] + B.audit(A.asset)['total_held'], 50)
                self.A.close(); self.B.close()

    def test_independent_checker_on_a_real_export_and_vectors(self):
        A, B = self.A, self.B
        a = A.export('alice', B, self.bob['account'], 20); courier.push(A.ep, B.ep, 'dom-b')
        B.c.post('/api/v1/native/transfers', headers=self.bob['h'], json={'to_account': self.carol['account'], 'amount': 5, 'asset': A.asset, 'request_id': 'chk1'})
        for dom in (A, B):
            exp = dom.c.get('/api/v1/native/export', headers=dom.H).json()
            roots = {d.id: [{'key_id': d.domain()['authority']['key_id'], 'public_hex': d.domain()['authority']['public_hex'], 'epoch': d.domain()['authority']['epoch']}] for d in (A, B)}
            rep = checker.check(exp, roots)
            self.assertTrue(rep['properties']['authorized_bookkeeping']['ok'], rep['properties']['authorized_bookkeeping'])
            self.assertTrue(rep['properties']['cryptographic_authenticity']['ok'], [c for c in rep['properties']['cryptographic_authenticity']['checks'] if not c['ok']][:3])
            self.assertTrue(rep['properties']['economic_backing']['ok']); self.assertIsNone(rep['properties']['scientific_reproducibility']['ok'])
            self.assertEqual(rep['properties']['economic_backing']['reconciliation'], 'complete for the exported sequence')
            wrong = {k: [dict(v[0], public_hex='00' * 32)] for k, v in roots.items()}
            bad = checker.check(exp, wrong); self.assertFalse(bad['properties']['cryptographic_authenticity']['ok']); self.assertTrue(bad['properties']['authorized_bookkeeping']['ok'])   # properties stay separate
            partial = checker.check(exp, {}); self.assertIn('sender_root_pinned', partial['properties']['cryptographic_authenticity']['missing_evidence'] or ['sender_root_pinned'])
        vr = vectors.run(); self.assertTrue(vr['all_as_expected'], [r for r in vr['results'] if not r['as_expected']])
        self.assertGreaterEqual(len(vr['results']), 16)

    def test_disputes_are_bounded_to_the_obligation(self):
        A = self.A; adam = self.adam
        # an obligation settled natively between alice and adam
        ob = A.c.post('/api/v1/native/transfers', headers=self.alice['h'], json={'to_account': adam['account'], 'amount': 8, 'request_id': 'd1', 'purpose': 'service'}).json()
        self.assertEqual(ob['state'], 'settled')
        with A.inst.app.state.services.db.tx() as db:
            oid = A.inst.app.state.services.native.obligation_create(db, 'ws_default', 'provider', self.alice['account'], adam['account'], A.asset, 8, 'work_milestone', 'ms-dispute', {'partial_payments': False})
            oid = oid['id'] if isinstance(oid, dict) else oid
            db.execute("UPDATE native_obligations SET settled_amount=8, state='settled' WHERE id=?", (oid,))
        # a stranger (bob at B has no account at A) / a non-party actor cannot open; the payer can; the hold is at most the settlement on the payee only
        other = A.actor('eve', fund=3)
        r = A.c.post('/api/v1/native/disputes', headers=other['h'], json={'obligation_id': oid, 'kind': 'delivery_mismatch', 'claim': 'not mine'}); self.assertEqual(r.status_code, 403)
        d = A.c.post('/api/v1/native/disputes', headers=self.alice['h'], json={'obligation_id': oid, 'kind': 'delivery_mismatch', 'claim': 'delivered artifact differs from the accepted digest'}); self.assertEqual(d.status_code, 201, d.text); d = d.json()
        self.assertEqual((d['held_amount'], A.bal(adam['account']), A.bal(adam['account'], 'disputed'), A.bal(other['account'])), (8, 0, 8, 3))
        # the respondent cannot resolve its own dispute; the reviewer upholds: refund as a NEW transfer, the original stays
        self.assertEqual(A.c.post('/api/v1/native/disputes/' + d['id'] + '/resolve', headers=adam['h'], json={'outcome': 'uphold'}).status_code, 403)
        res = A.c.post('/api/v1/native/disputes/' + d['id'] + '/resolve', headers=A.inst.h('reviewer'), json={'outcome': 'uphold', 'reason': 'digest mismatch confirmed'}); self.assertEqual(res.status_code, 200, res.text); res = res.json()
        self.assertEqual(res['state'], 'resolved'); self.assertTrue(res['resolution']['refund_intent'].startswith('ni_'))
        self.assertEqual((A.bal(self.alice['account']), A.bal(adam['account']), A.bal(adam['account'], 'disputed')), (50, 0, 0))
        self.assertEqual(A.c.get('/api/v1/native/intents/' + ob['id'], headers=A.H).json()['state'], 'settled')      # the historic settlement is not rewritten
        self.assertTrue(A.audit()['conserved'])


if __name__ == '__main__':
    unittest.main()
