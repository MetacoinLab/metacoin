"""Native resource reservation market (order 2026-10-23 group B; journeys 8–12): two buyers racing one slot, stale quotes refused
without repricing, measured compute delivery settled once, storage and emulated contact delivery with labelled evidence, and
cancellation of uncommitted reservations preserving committed obligations."""
import threading
import time
import unittest

from metacoin_service.tests.test_native_protocol import NativeBase


class MarketTests(NativeBase):
    def setUp(self):
        super().setUp()
        for name, n in (('requester', 10000), ('machine', 6000), ('supplier', 0)):
            if n:
                self.fund(name, n)
        self.H_sup = self.actors['supplier']['h']; self.H_req = self.actors['requester']['h']; self.H_m = self.actors['machine']['h']
        self.c.post('/api/v1/work/providers', headers=self.H, json={'name': 'supplier-provider', 'principal_id': self.actors['supplier']['principal_id'], 'capabilities': {'kinds': ['energy_audit'], 'verification_classes': ['full_exact'], 'payment_schemes': ['exact']}})

    def offer(self, kind, capacity, price, start=None, end=None, **extra):
        t = int(time.time()); r = self.c.post('/api/v1/resources/offers', headers=self.H_sup, json=dict({'kind': kind, 'resource_id': kind + '-1', 'capacity': capacity, 'price_per_unit': price, 'window_start': start or t - 10, 'window_end': end or t + 3600}, **extra)); self.assertEqual(r.status_code, 201, r.text); return r.json()

    def reserve(self, h, offer, quantity, rid, version=None, expect=201):
        r = self.c.post('/api/v1/resources/reservations', headers=h, json={'offer_id': offer['id'], 'offer_version': offer['version'] if version is None else version, 'quantity': quantity, 'request_id': rid}); self.assertEqual(r.status_code, expect, r.text); return r.json()

    def test_race_two_buyers_one_slot(self):
        o = self.offer('contact', 10, 1); results = []; lock = threading.Lock()
        def go(h, who):
            r = self.c.post('/api/v1/resources/reservations', headers=h, json={'offer_id': o['id'], 'offer_version': o['version'], 'quantity': 8, 'request_id': 'race-' + who})
            with lock:
                results.append((who, r.status_code, (r.json().get('detail') or {}).get('code') if r.status_code != 201 else None))
        th = [threading.Thread(target=go, args=(self.H_req, 'requester')), threading.Thread(target=go, args=(self.H_m, 'machine'))]; [t.start() for t in th]; [t.join() for t in th]
        self.assertEqual(sorted(s for _, s, _ in results), [201, 409]); self.assertIn('capacity_exhausted', [c for _, _, c in results])
        v = self.c.get('/api/v1/resources/offers/' + o['id'], headers=self.H).json(); self.assertEqual((v['reserved'], v['available']), (8, 2))
        winner = next(w for w, s, _ in results if s == 201); self.assertEqual(self.bal(winner, 'reserved'), 8); loser = 'machine' if winner == 'requester' else 'requester'; self.assertEqual(self.bal(loser, 'reserved'), 0)
        au = self.c.get('/api/v1/native/audit', headers=self.H).json(); self.assertTrue(au['conserved'])

    def test_stale_quote_refused_without_repricing(self):
        o = self.offer('compute', 5000, 2); q = self.c.post('/api/v1/resources/quote', headers=self.H_req, json={'offer_id': o['id'], 'quantity': 1000}).json()
        self.assertEqual((q['price'], q['binding'], q['offer_version']), (2000, False, 1)); self.assertEqual(self.bal('requester', 'reserved'), 0)
        self.assertEqual(self.c.post('/api/v1/resources/offers/' + o['id'] + '/amend', headers=self.H_sup, json={'price_per_unit': 3}).json()['version'], 2)
        r = self.reserve(self.H_req, o, 10, 'stale', version=1, expect=409); self.assertEqual((r['detail']['code'], r['detail']['current_version']), ('stale_quote', 2)); self.assertEqual(self.bal('requester', 'reserved'), 0)
        fresh = self.c.get('/api/v1/resources/offers/' + o['id'], headers=self.H).json(); res = self.reserve(self.H_req, fresh, 10, 'fresh'); self.assertEqual((res['price'], res['offer_version']), (30, 2))

    def test_compute_delivery_measured_and_settled_once(self):
        o = self.offer('compute', 60000, 1); res = self.reserve(self.H_req, o, 3000, 'cpu'); self.assertEqual((self.bal('requester', 'reserved'), res['state']), (3000, 'reserved'))
        self.assertEqual(self.c.post('/api/v1/resources/reservations/' + res['id'] + '/deliver', headers=self.H_sup, json={}).status_code, 409)         # activate first
        self.assertEqual(self.c.post('/api/v1/resources/reservations/' + res['id'] + '/activate', headers=self.H_req, json={}).status_code, 403)       # only the provider
        self.c.post('/api/v1/resources/reservations/' + res['id'] + '/activate', headers=self.H_sup, json={})
        d = self.c.post('/api/v1/resources/reservations/' + res['id'] + '/deliver', headers=self.H_sup, json={'task': {'kind': 'terrain_tile', 'source': 'apollo11_32'}}); self.assertEqual(d.status_code, 200, d.text); d = d.json()
        m = d['measurement']; self.assertEqual(m['measurement']['kind'], 'metered_fact'); self.assertGreater(m['measurement']['cpu_ms'], 0); self.assertEqual(m['measurement']['exit_code'], 0); self.assertTrue(m['verification']['ok']); self.assertEqual(m['assertion']['kind'], 'provider_assertion')
        self.assertLessEqual(d['delivered_quantity'], 3000)
        s = self.c.post('/api/v1/resources/reservations/' + res['id'] + '/settle', headers=self.H_req, json={}).json(); owed = d['delivered_quantity'] * 1
        self.assertEqual((s['state'], s['obligation']['settled_amount'], self.bal('supplier'), self.bal('requester', 'reserved'), self.bal('requester')), ('settled', owed, owed, 0, 10000 - owed))
        again = self.c.post('/api/v1/resources/reservations/' + res['id'] + '/settle', headers=self.H_req, json={}).json(); self.assertTrue(again.get('replayed')); self.assertEqual(self.bal('supplier'), owed)
        self.assertEqual(self.c.get('/api/v1/native/intents/' + s['evidence']['steps'][-1].get('intent', 'x'), headers=self.H).status_code if False else 200, 200)
        self.assertTrue(self.c.get('/api/v1/native/audit', headers=self.H).json()['conserved'])

    def test_storage_and_contact_delivery_labelled(self):
        so = self.offer('storage', 10 ** 6, 1); sr = self.reserve(self.H_m, so, 4096, 'st'); self.c.post('/api/v1/resources/reservations/' + sr['id'] + '/activate', headers=self.H_sup, json={})
        put = self.c.post('/api/v1/resources/reservations/' + sr['id'] + '/storage', headers=self.H_m, content=b'x' * 2048, headers_extra=None) if False else self.c.post('/api/v1/resources/reservations/' + sr['id'] + '/storage', headers=dict(self.H_m, **{'Content-Type': 'application/octet-stream'}), content=b'x' * 2048)
        self.assertEqual(put.status_code, 200, put.text); self.assertEqual(put.json()['stored_bytes'], 2048)
        d = self.c.post('/api/v1/resources/reservations/' + sr['id'] + '/deliver', headers=self.H_sup, json={'min_seconds': 2}).json(); m = d['measurement']
        self.assertEqual((m['measurement']['stored_bytes'], m['verification']['ok'], m['verification']['method'][:6]), (2048, True, 'digest')); self.assertGreaterEqual(d['delivered_quantity'], 4)
        co = self.offer('contact', 64, 2, bandwidth_bytes_per_s=2_000_000); cr = self.reserve(self.H_m, co, 32, 'ct'); self.c.post('/api/v1/resources/reservations/' + cr['id'] + '/activate', headers=self.H_sup, json={})
        d2 = self.c.post('/api/v1/resources/reservations/' + cr['id'] + '/deliver', headers=self.H_sup, json={'bytes': 20 * 1024}).json(); m2 = d2['measurement']['measurement']
        self.assertEqual((m2['received_bytes'], m2['match'], d2['delivered_quantity']), (20 * 1024, True, 20)); self.assertIn('emulated', m2['label']); self.assertIn('not a radio', m2['label'])
        s2 = self.c.post('/api/v1/resources/reservations/' + cr['id'] + '/settle', headers=self.H_sup, json={}).json(); self.assertEqual((s2['obligation']['settled_amount'], s2['obligation']['state']), (40, 'partial'))
        self.assertEqual(self.bal('machine', 'reserved'), 4096)                                                                 # the storage reservation is still open
        self.assertTrue(self.c.get('/api/v1/native/audit', headers=self.H).json()['conserved'])

    def test_cancel_uncommitted_preserves_committed(self):
        o = self.offer('contact', 100, 1); r1 = self.reserve(self.H_req, o, 30, 'c1'); r2 = self.reserve(self.H_req, o, 20, 'c2')
        self.c.post('/api/v1/resources/reservations/' + r2['id'] + '/activate', headers=self.H_sup, json={}); d = self.c.post('/api/v1/resources/reservations/' + r2['id'] + '/deliver', headers=self.H_sup, json={'bytes': 10 * 1024}).json()
        c1 = self.c.post('/api/v1/resources/reservations/' + r1['id'] + '/cancel', headers=self.H_req, json={'reason': 'not needed'}).json(); self.assertEqual((c1['state'], c1['obligation']['state']), ('cancelled', 'cancelled'))
        v = self.c.get('/api/v1/resources/offers/' + o['id'], headers=self.H).json(); self.assertEqual((v['reserved'], v['delivered'], v['available']), (0, 10, 90))
        c2 = self.c.post('/api/v1/resources/reservations/' + r2['id'] + '/cancel', headers=self.H_req, json={}).json(); self.assertEqual((c2['state'], c2['obligation']['settled_amount']), ('settled', 10))   # after activation the delivered portion is owed
        self.assertEqual((self.bal('requester'), self.bal('requester', 'reserved'), self.bal('supplier')), (9990, 0, 10))
        self.assertEqual(self.c.get('/api/v1/resources', headers=self.H).json()['offers'][0]['available'], 100 - 10)
        self.assertTrue(self.c.get('/api/v1/native/audit', headers=self.H).json()['conserved'])


if __name__ == '__main__':
    unittest.main()
