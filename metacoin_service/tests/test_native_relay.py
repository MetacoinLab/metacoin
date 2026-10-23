"""Scheduled relay service with native payment (order 2026-10-23 group F; journeys 26–30 at unit level)."""
import hashlib
import time
import unittest

from metacoin_service.tests.test_native_missions import MissionBase


class RelayTests(MissionBase):
    def setUp(self):
        super().setUp(); self.fund('requester', 5000)
        a = self.c.post('/api/v1/native/actors', headers=self.H, json={'name': 'relay2'}).json(); self.actors['relay2'] = {'account': a['account_id'], 'principal_id': a['principal_id'], 'h': {'Authorization': 'Bearer ' + a['credential']['token']}}
        t = int(time.time()); self.o1 = self.offer_contact(self.S, 'r1', t - 1, t + 3600, 1); self.o2 = self.offer_contact(self.actors['relay2']['h'], 'r2', t - 1, t + 3600, 2)

    def offer_contact(self, h, rid, ws, we, price, cap=10000):
        r = self.c.post('/api/v1/resources/offers', headers=h, json={'kind': 'contact', 'resource_id': rid, 'capacity': cap, 'price_per_unit': price, 'window_start': ws, 'window_end': we, 'bandwidth_bytes_per_s': 5_000_000}); self.assertEqual(r.status_code, 201, r.text); return r.json()

    def route(self, mid='msg-1', nbytes=3000, ceiling=100, policy='receiver_ack_required', hops=None, expect=201):
        hops = hops or [{'offer_id': self.o1['id'], 'offer_version': self.o1['version'], 'relay_account': self.actors['supplier']['account']}, {'offer_id': self.o2['id'], 'offer_version': self.o2['version'], 'relay_account': self.actors['relay2']['account']}]
        r = self.c.post('/api/v1/relay/routes', headers=self.R, json={'message_id': mid, 'recipient_account': self.actors['machine']['account'], 'bytes': nbytes, 'hops': hops, 'fee_ceiling': ceiling, 'dispute_policy': policy, 'ack_timeout_seconds': 60}); self.assertEqual(r.status_code, expect, r.text); return r.json()

    def test_paid_two_hop_relay_with_separate_evidence_and_bounded_fees(self):
        r = self.route(); rid = r['id']; req0 = self.bal('requester')
        self.assertEqual((r['state'], r['fees_bound'], [h['reservation_state'] for h in r['hops']]), ('funded', 3 * 1 + 3 * 2, ['reserved', 'reserved'])); self.assertEqual(self.bal('requester', 'reserved'), 9)
        payload = b'terrain-product-bytes' * 100; self.assertLessEqual(len(payload), 3000)
        s = self.c.post('/api/v1/relay/routes/%s/submit' % rid, headers=dict(self.R, **{'Content-Type': 'application/octet-stream'}), content=payload); self.assertEqual(s.status_code, 200, s.text); s = s.json()
        self.assertEqual((s['state'], s['queues'][0]['state'], s['queues'][0]['evidence']['custody']['sha256']), ('in_transit', 'stored', hashlib.sha256(payload).hexdigest()))
        # journey 27: hop 0 forwarding evidence is not final delivery; the recipient cannot read yet
        f0 = self.c.post('/api/v1/relay/routes/%s/forward' % rid, headers=self.S, json={'hop': 0}); self.assertEqual(f0.status_code, 200, f0.text); f0 = f0.json()
        self.assertEqual((f0['state'], f0['queues'][0]['state'], f0['queues'][1]['state']), ('in_transit', 'forwarded', 'stored')); self.assertTrue(f0['queues'][0]['evidence']['forwarding']['match']); self.assertIn('emulated', f0['queues'][0]['evidence']['forwarding']['label'])
        self.assertEqual(self.c.get('/api/v1/relay/routes/%s/message' % rid, headers=self.M).status_code, 409)
        self.assertEqual(self.c.post('/api/v1/relay/routes/%s/forward' % rid, headers=self.R, json={'hop': 1}).status_code, 403)                     # only the relay of the hop
        again = self.c.post('/api/v1/relay/routes/%s/forward' % rid, headers=self.S, json={'hop': 0}).json(); self.assertTrue(again.get('replayed'))     # journey 28: a retry charges nothing again
        f1 = self.c.post('/api/v1/relay/routes/%s/forward' % rid, headers=self.actors['relay2']['h'], json={'hop': 1}).json(); self.assertEqual(f1['state'], 'delivered'); self.assertIn('not mission success', f1['evidence']['steps'][-1]['note'])
        got = self.c.get('/api/v1/relay/routes/%s/message' % rid, headers=self.M); self.assertEqual(got.content, payload)
        st = self.c.post('/api/v1/relay/routes/%s/settle' % rid, headers=self.R, json={}).json(); self.assertEqual([h['state'] for h in st['hops']], ['settled', 'awaiting_acknowledgment']); self.assertIn('withhold', st['hops'][1]['note'])
        bad = self.c.post('/api/v1/relay/routes/%s/acknowledge' % rid, headers=self.M, json={'message_id': 'msg-1', 'content_hash': 'ff' * 32}); self.assertEqual(bad.status_code, 422)
        ack = self.c.post('/api/v1/relay/routes/%s/acknowledge' % rid, headers=self.M, json={'message_id': 'msg-1', 'content_hash': hashlib.sha256(payload).hexdigest()}).json(); self.assertEqual(ack['state'], 'acknowledged')
        st = self.c.post('/api/v1/relay/routes/%s/settle' % rid, headers=self.R, json={}).json(); self.assertEqual([h['state'] for h in st['hops']], ['settled', 'settled']); st2 = self.c.post('/api/v1/relay/routes/%s/settle' % rid, headers=self.R, json={}).json(); self.assertTrue(all(h.get('already') for h in st2['hops']))
        self.assertEqual((self.bal('supplier'), self.bal('relay2'), self.bal('requester'), self.bal('requester', 'reserved')), (2, 4, req0 + 3, 0));   # 2 KiB delivered per hop (2100 bytes): the undelivered KiB of each 3 KiB reservation is released, never charged
        self.assertTrue(self.c.get('/api/v1/native/audit', headers=self.H).json()['conserved'])

    def test_admission_limits_missed_window_and_lost_receipt(self):
        # journey 30: payload, retention, recipient and fee limits at admission
        self.route(mid='big', nbytes=5 * 1024 * 1024, expect=422); self.assertEqual(self.c.post('/api/v1/relay/routes', headers=self.R, json={'message_id': 'x', 'recipient_account': self.actors['requester']['account'], 'bytes': 10, 'hops': [{'offer_id': self.o1['id'], 'offer_version': 1, 'relay_account': self.actors['supplier']['account']}], 'fee_ceiling': 1}).json()['detail']['code'], 'recipient_is_sender')
        r = self.c.post('/api/v1/relay/routes', headers=self.R, json={'message_id': 'x2', 'recipient_account': self.actors['machine']['account'], 'bytes': 3000, 'hops': [{'offer_id': self.o1['id'], 'offer_version': 1, 'relay_account': self.actors['supplier']['account']}], 'fee_ceiling': 100, 'retention_seconds': 5}); self.assertEqual(r.json()['detail']['code'], 'retention_limit')
        r = self.route(mid='cheap', ceiling=2, expect=400); self.assertEqual(r['detail']['code'], 'fee_ceiling_exceeded'); self.assertEqual(self.bal('requester', 'reserved'), 0)
        # journey 29: a missed window (hop never activated) is released in full and replanned; a delivered hop is never released
        t = int(time.time()); short = self.offer_contact(self.S, 'short', t - 1, t + 2, 1); r = self.route(mid='missed', hops=[{'offer_id': short['id'], 'offer_version': 1, 'relay_account': self.actors['supplier']['account']}, {'offer_id': self.o2['id'], 'offer_version': 1, 'relay_account': self.actors['relay2']['account']}])
        time.sleep(2.2); c = self.c.post('/api/v1/relay/routes/%s/cancel-unused' % r['id'], headers=self.R, json={}).json(); self.assertEqual([h['state'] for h in c['hops']], ['cancelled', 'cancelled']); self.assertEqual(self.bal('requester', 'reserved'), 0)
        r2 = self.route(mid='missed-replan'); payload = b'x' * 2000; self.c.post('/api/v1/relay/routes/%s/submit' % r2['id'], headers=dict(self.R, **{'Content-Type': 'application/octet-stream'}), content=payload)
        self.c.post('/api/v1/relay/routes/%s/forward' % r2['id'], headers=self.S, json={'hop': 0}); c2 = self.c.post('/api/v1/relay/routes/%s/cancel-unused' % r2['id'], headers=self.R, json={}).json()
        self.assertEqual(c2['hops'][0]['state'], 'delivered'); self.assertIn('never released', c2['hops'][0]['note'])                                                # the forwarded hop's charge stands
        # journey 28: lost receipt (crash at the acknowledgment) and a relay restart: no duplicate charge
        r3 = self.route(mid='lost'); self.c.post('/api/v1/relay/routes/%s/submit' % r3['id'], headers=dict(self.R, **{'Content-Type': 'application/octet-stream'}), content=payload)
        self.c.post('/api/v1/relay/routes/%s/forward' % r3['id'], headers=self.S, json={'hop': 0}); self.c.post('/api/v1/relay/routes/%s/forward' % r3['id'], headers=self.actors['relay2']['h'], json={'hop': 1})
        self.c.post('/api/v1/ops/faults', headers=self.H, json={'fault': 'relay_receipt'}); a = self.c.post('/api/v1/relay/routes/%s/acknowledge' % r3['id'], headers=self.M, json={'message_id': 'lost', 'content_hash': hashlib.sha256(payload).hexdigest()}); self.assertEqual(a.status_code, 409); self.c.post('/api/v1/ops/faults', headers=self.H, json={'fault': 'relay_receipt', 'disarm': True})
        self.assertEqual(self.c.get('/api/v1/relay/routes/' + r3['id'], headers=self.R).json()['state'], 'delivered')
        self.inst.reopen(); self.c = self.inst.client                                                                                               # relay/service restart: durable queues
        v = self.c.get('/api/v1/relay/routes/' + r3['id'], headers=self.R).json(); self.assertEqual([q['state'] for q in v['queues']], ['forwarded', 'forwarded'])
        a = self.c.post('/api/v1/relay/routes/%s/acknowledge' % r3['id'], headers=self.M, json={'message_id': 'lost', 'content_hash': hashlib.sha256(payload).hexdigest()}); self.assertEqual(a.json()['state'], 'acknowledged')
        sup0 = self.bal('supplier'); st = self.c.post('/api/v1/relay/routes/%s/settle' % r3['id'], headers=self.R, json={}).json(); self.c.post('/api/v1/relay/routes/%s/settle' % r3['id'], headers=self.R, json={})
        self.assertEqual([h['state'] for h in st['hops']], ['settled', 'settled']); self.assertEqual(self.bal('supplier'), sup0 + 1)                   # 2000 bytes = 1 whole KiB delivered × 1, charged once
        # relay_evidence_after_timeout: the last hop pays on forwarding evidence after the timeout even without the acknowledgment
        r4 = self.route(mid='timeout', policy='relay_evidence_after_timeout'); self.c.post('/api/v1/relay/routes/%s/submit' % r4['id'], headers=dict(self.R, **{'Content-Type': 'application/octet-stream'}), content=payload)
        self.c.post('/api/v1/relay/routes/%s/forward' % r4['id'], headers=self.S, json={'hop': 0}); self.c.post('/api/v1/relay/routes/%s/forward' % r4['id'], headers=self.actors['relay2']['h'], json={'hop': 1})
        st = self.c.post('/api/v1/relay/routes/%s/settle' % r4['id'], headers=self.R, json={}).json(); self.assertEqual(st['hops'][1]['state'], 'awaiting_acknowledgment')                 # timeout (60 s) not elapsed
        self.assertTrue(self.c.get('/api/v1/native/audit', headers=self.H).json()['conserved'])


if __name__ == '__main__':
    unittest.main()
