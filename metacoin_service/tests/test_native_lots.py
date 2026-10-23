"""Partial return with explicit claim lineage (order 2026-10-23 group G; journeys 31–35 at unit level): export 20, spend 7 to a
supplier, retire exactly the holder's remaining 13, release 13 at the source once while the supplier's 7 stays backed; duplicate
parent consumption, another owner's claim, mixed assets and missing lineage refused; every crash boundary replays safely; a v1
peer refuses the v2 message instead of reinterpreting it."""
import json
import unittest

from metacoin_service.native import courier, lots as L, protocol as P
from metacoin_service.tests.test_native_domains import NativeDomainTests


class LotTests(NativeDomainTests):
    def lots(self, dom, acct):
        return dom.c.get('/api/v1/native/accounts/%s/lots' % acct, headers=dom.H).json()

    def test_split_spend_and_retire_only_the_holders_remainder(self):
        A, B = self.A, self.B; bob, carol = self.bob['account'], self.carol['account']; alice0 = A.bal(self.alice['account'])
        a = A.export('alice', B, bob, 20, rid='lot20'); courier.push(A.ep, B.ep, 'dom-b')
        lots = self.lots(B, bob); self.assertEqual([(l['amount'], l['status'], l['transferred']) for l in lots['lots']], [(20, 'active', 0)]); self.assertEqual(lots['retirable_total'], 20)
        # spend 7 to the supplier: the lot splits; carol's 7 stays backed but is not hers to retire
        t = B.c.post('/api/v1/native/transfers', headers=self.bob['h'], json={'to_account': carol, 'amount': 7, 'asset': A.asset, 'request_id': 's7'}); self.assertEqual(t.status_code, 201, t.text)
        lb, lc = self.lots(B, bob), self.lots(B, carol)
        self.assertEqual(sorted((l['amount'], l['status']) for l in lb['lots'] if l['status'] in ('active',)), [(13, 'active')]); self.assertEqual([(l['amount'], l['status'], l['transferred'], l['origin_allocation']) for l in lc['lots']], [(7, 'active', 1, a['id'])])
        self.assertEqual((lb['retirable_total'], lc['retirable_total']), (13, 0)); inv = B.alloc(a['id'])['lot_invariants']; self.assertTrue(inv['partition_ok']); self.assertEqual(inv['partition_sum'], 20)
        # journey 33: another owner's claim and over-retirement are refused; the holder retires only its 13
        r = B.c.post('/api/v1/native/allocations/%s/return' % a['id'], headers=self.carol['h'], json={'amount': 7}); self.assertEqual(r.status_code, 403)
        r = B.c.post('/api/v1/native/allocations/%s/return' % a['id'], headers=self.bob['h'], json={'amount': 14}); self.assertEqual((r.status_code, r.json()['detail']['code']), (409, 'retire_exceeds_owned'))
        r = B.c.post('/api/v1/native/allocations/%s/return' % a['id'], headers=self.bob['h'], json={'amount': 13}); self.assertEqual(r.status_code, 200, r.text); v = r.json(); self.assertEqual((v['state'], v['exposed_state']), ('return_requested', 'retirement_pending'))
        self.assertEqual((B.bal(bob, asset=A.asset), B.bal(bob, 'in_transit', asset=A.asset)), (0, 13)); self.assertEqual([l['status'] for l in self.lots(B, bob)['lots'] if l['amount'] == 13], ['retired'])
        req = v['return']; self.assertTrue(req['partial']); self.assertEqual((req['amount'], req['returned_before'], len(req['lots'])), (13, 0, 1))
        # journey 32: the source releases exactly 13 once; 7 stays locked as backing for carol's lot
        out = courier.push(B.ep, A.ep, 'dom-a'); self.assertEqual(out['results'][0]['state'], 'accepted'); sa = A.alloc(a['id'])
        self.assertEqual((A.bal(self.alice['account']), sa['state'], sa['returned_amount']), (alice0 - 20 + 13, 'partially_returned', 13)); self.assertEqual(A.audit()['backing_locked_for_exports'], 7)
        sb = B.alloc(a['id']); self.assertEqual((sb['state'], sb['returned_amount'], B.bal(bob, 'in_transit', asset=A.asset)), ('imported', 13, 0)); self.assertEqual(B.audit(A.asset)['claims_backed_elsewhere'], 7); self.assertEqual(B.bal(carol, asset=A.asset), 7)
        # replayed request and reordered/duplicate certificate release and burn nothing twice
        ob = B.c.get('/api/v1/native/transport/outbox', headers=B.H, params={'receiver': 'dom-a', 'all': '1'}).json(); dup = A.c.post('/api/v1/native/transport/inbox', headers=A.H, json=ob).json()
        self.assertTrue(all(x['state'] == 'duplicate' for x in dup['results'])); self.assertEqual(A.bal(self.alice['account']), alice0 - 7)
        oa = A.c.get('/api/v1/native/transport/outbox', headers=A.H, params={'receiver': 'dom-b', 'all': '1'}).json(); dup2 = B.c.post('/api/v1/native/transport/inbox', headers=B.H, json=oa).json()
        self.assertTrue(all(x['state'] == 'duplicate' for x in dup2['results'])); self.assertEqual(B.alloc(a['id'])['returned_amount'], 13)
        # journey 33 (duplicate parent consumption): nothing retirable remains for bob; a second retirement is refused
        r = B.c.post('/api/v1/native/allocations/%s/return' % a['id'], headers=self.bob['h'], json={'amount': 1}); self.assertEqual(r.json()['detail']['code'], 'missing_lineage')
        acc = B.c.get('/api/v1/native/accounting', headers=B.H, params={'asset': A.asset}).json(); self.assertTrue(acc['lots']['partition_ok']); self.assertEqual(acc['lots']['returned_total'], 13); self.assertTrue(acc['checks']['backing_matches_journal'])
        self.assertEqual(A.audit()['total_held'] + A.audit()['backing_locked_for_exports'], 50); self.assertTrue(A.audit()['conserved'] and B.audit(A.asset)['conserved'])
        # the invariant recomputed independently from the exported record
        ex = B.c.get('/api/v1/native/export', headers=B.H).json(); by = {}
        for l in ex['claim_lots']:
            if l['origin_allocation'] == a['id']:
                by[l['status']] = by.get(l['status'], 0) + l['amount']
        self.assertEqual(sum(by.values()), 20); self.assertEqual(by.get('active', 0) + by.get('spent', 0), 7)

    def test_mixed_asset_missing_lineage_and_crash_boundaries(self):
        A, B = self.A, self.B; bob = self.bob['account']; alice0 = A.bal(self.alice['account'])
        a = A.export('alice', B, bob, 10, rid='lot10'); courier.push(A.ep, B.ep, 'dom-b')
        # missing lineage (journey 35): carol holds nothing of this allocation; a fixture-funded local unit has no origin
        self.assertEqual(B.c.post('/api/v1/native/allocations/%s/return' % a['id'], headers=self.carol['h'], json={'amount': 1}).status_code, 403)
        # mixed asset: a request naming another asset cannot pass the protocol binding at the source
        B.c.post('/api/v1/native/allocations/%s/return' % a['id'], headers=self.bob['h'], json={'amount': 4}); ob = B.c.get('/api/v1/native/transport/outbox', headers=B.H, params={'receiver': 'dom-a'}).json()
        tampered = json.loads(json.dumps(ob)); tampered['items'][0]['payload']['asset'] = B.asset; r = A.c.post('/api/v1/native/transport/inbox', headers=A.H, json=tampered).json(); self.assertIn(r['results'][0]['reason'], ('payload_digest_mismatch', 'payload_signature', 'signature_invalid', 'return_binding'))
        self.assertEqual(A.bal(self.alice['account']), alice0 - 10)
        # journey 34: crash boundaries — retirement init, source release, destination burn, acknowledgment/redelivery
        a2 = A.export('alice', B, bob, 6, rid='lot6'); courier.push(A.ep, B.ep, 'dom-b')
        B.fault('native_return_init'); r = B.c.post('/api/v1/native/allocations/%s/return' % a2['id'], headers=self.bob['h'], json={'amount': 4}); self.assertEqual(r.status_code, 409); B.fault('native_return_init', disarm=True)
        v = B.alloc(a2['id']); self.assertEqual((v['state'], v['exposed_state'], B.bal(bob, 'in_transit', asset=A.asset)), ('return_requested', 'retirement_pending', 4 + 4))      # retirement durable, message not yet queued (plus the pending 4 of lot10)
        r = B.c.post('/api/v1/native/allocations/%s/return' % a2['id'], headers=self.bob['h'], json={'amount': 4}); self.assertTrue(r.json().get('replayed'))
        A.fault('native_return_complete'); courier.push(B.ep, A.ep, 'dom-a'); A.fault('native_return_complete', disarm=True)
        sa = A.alloc(a2['id']); self.assertEqual((sa['state'], sa['returned_amount']), ('acknowledged', 0)); before = A.bal(self.alice['account'])
        courier.push(B.ep, A.ep, 'dom-a'); courier.push(B.ep, A.ep, 'dom-a')
        sa = A.alloc(a2['id']); self.assertEqual((sa['state'], sa['returned_amount'], A.bal(self.alice['account'])), ('partially_returned', 4, before + 4 + 4))   # both pending returns (4 of lot10, 4 of lot6) completed once each
        sb = B.alloc(a2['id']); self.assertEqual((sb['state'], sb['returned_amount']), ('imported', 4)); self.assertEqual(B.bal(bob, 'in_transit', asset=A.asset), 0)
        self.assertEqual(A.audit()['total_held'] + A.audit()['backing_locked_for_exports'], 50); self.assertTrue(B.audit(A.asset)['conserved'])
        # the remaining 2 of lot6 retire later in a second partial return; after it the allocation closes
        r = B.c.post('/api/v1/native/allocations/%s/return' % a2['id'], headers=self.bob['h'], json={'amount': 2}); self.assertEqual(r.status_code, 200, r.text); courier.push(B.ep, A.ep, 'dom-a')
        self.assertEqual((A.alloc(a2['id'])['state'], B.alloc(a2['id'])['state'], A.alloc(a2['id'])['returned_amount']), ('returned', 'returned', 6))

    def test_v1_peer_refuses_v2_messages_instead_of_reinterpreting(self):
        msg = {'protocol': P.PROTOCOL, 'type': 'return_request', 'allocation_id': 'na_x', 'environment': 'dev', 'source_authority': 'dom-a', 'destination_authority': 'dom-b', 'asset': 'dev:dom-a:ledger-dom-a:TMETA:0', 'amount': 13, 'recipient_account': 'na_b', 'destination_sequence': 3, 'requested_at': 1, 'partial': True, 'lots': ['lot_1'], 'returned_before': 0}
        P.validate_message('return_request', msg, 'dev')                                                     # v2-aware peer accepts
        with self.assertRaises(P.ProtocolError) as cm:                                                        # a v1 peer (strict field set) refuses: unknown fields, no silent full-return reading
            fields = P.RETURN_REQUEST_FIELDS
            if set(msg) != fields:
                raise P.ProtocolError('message_fields', {'expected': sorted(fields), 'got': sorted(msg)})
        self.assertEqual(cm.exception.code, 'message_fields')
        bad = dict(msg, lots=[]); self.assertRaises(P.ProtocolError, P.validate_message, 'return_request', bad, 'dev')


if __name__ == '__main__':
    unittest.main()
