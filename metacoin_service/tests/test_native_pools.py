"""Shared capacity across overlapping offers (order 2026-10-23 group C; journeys 11–15 at unit level): one pool behind two
overlapping offers cannot oversell their shared interval; reservations racing across two offers conserve capacity and funds;
boundary-touching intervals and unit conversions are exact; capacity reductions below commitments are refused; draining keeps
delivered work and accepted obligations. The interval sweep is checked against an independent oracle on exact fixtures."""
import random
import threading
import time
import unittest

from metacoin_service.native import pools as PL
from metacoin_service.tests.test_native_protocol import NativeBase


def oracle_max_usage(commitments, t0, t1):
    """Independent point-wise oracle: evaluate usage at every integer second (small fixtures only)."""
    return max((sum(c['slots'] for c in commitments if c['start'] <= t < c['end']) for t in range(t0, t1)), default=0)


class SweepKernelTests(unittest.TestCase):
    def test_sweep_matches_pointwise_oracle_on_random_fixtures(self):
        r = random.Random(3)
        for _ in range(300):
            sched = [{'start': 0, 'end': 100, 'capacity': r.randint(1, 4)}]
            commits = [{'start': (a := r.randint(0, 90)), 'end': a + r.randint(1, 10), 'slots': r.randint(1, 2)} for _ in range(r.randint(1, 6))]
            segs = PL.sweep(sched, commits, 0, 100); peak = max(s['used'] for s in segs)
            self.assertEqual(peak, oracle_max_usage(commits, 0, 100)); self.assertEqual(all(s['ok'] for s in segs), peak <= sched[0]['capacity'])
            for s in segs:                                                     # every segment's usage equals the point-wise usage at its start
                self.assertEqual(s['used'], sum(c['slots'] for c in commits if c['start'] <= s['start'] < c['end']))

    def test_touching_intervals_do_not_conflict_and_alignment_is_outward(self):
        sched = [{'start': 0, 'end': 60, 'capacity': 1}]
        ok, _ = PL.admissible(sched, [{'start': 0, 'end': 30, 'slots': 1}], 30, 60, 1); self.assertTrue(ok)
        ok, seg = PL.admissible(sched, [{'start': 0, 'end': 30, 'slots': 1}], 29, 31, 1); self.assertFalse(ok); self.assertEqual((seg['start'], seg['end']), (29, 30))
        self.assertEqual(PL.align(7, 13, 5), (5, 15)); self.assertEqual(PL.align(10, 20, 10), (10, 20))
        self.assertEqual(PL.slots_for(30000, 1000, 0, 30), 1); self.assertEqual(PL.slots_for(30001, 1000, 0, 30), 2); self.assertEqual(PL.slots_for(300, 1, 0, 100), 3)   # kib_seconds over 100 s → 3 KiB slots


class PoolTests(NativeBase):
    def setUp(self):
        super().setUp(); self.fund('requester', 20000000); self.fund('machine', 20000000); self.S = self.actors['supplier']['h']; self.R = self.actors['requester']['h']; self.M = self.actors['machine']['h']
        self.t0 = (int(time.time()) // 100) * 100 + 100            # a future aligned origin so intervals are deterministic

    def pool(self, cap=1, u=1000, kind='compute', unit='cpu_slot', res=1, sched=None, h=None):
        r = self.c.post('/api/v1/resources/pools', headers=h or self.S, json={'kind': kind, 'label': 'rig', 'slot_unit': unit, 'units_per_slot_second': u, 'resolution_seconds': res, 'schedule': sched or [{'start': self.t0, 'end': self.t0 + 7200, 'capacity': cap}]}); self.assertEqual(r.status_code, 201, r.text); return r.json()

    def offer(self, pid, ws, we, cap, price=1, kind='compute', h=None, expect=201):
        r = self.c.post('/api/v1/resources/offers', headers=h or self.S, json={'kind': kind, 'resource_id': 'r-%d' % ws, 'capacity': cap, 'price_per_unit': price, 'window_start': ws, 'window_end': we, 'pool_id': pid}); self.assertEqual(r.status_code, expect, r.text); return r.json()

    def reserve(self, h, o, q, rid, start=None, end=None):
        body = {'offer_id': o['id'], 'offer_version': o['version'], 'quantity': q, 'request_id': rid}
        if start is not None:
            body.update(interval_start=start, interval_end=end)
        return self.c.post('/api/v1/resources/reservations', headers=h, json=body)

    def test_overlapping_offers_cannot_oversell_their_shared_interval(self):
        p = self.pool(); t = self.t0; a = self.offer(p['id'], t, t + 3600, 3600 * 1000); b = self.offer(p['id'], t + 1800, t + 5400, 3600 * 1000)
        r1 = self.reserve(self.R, a, 3600 * 1000, 'a-full', t, t + 3600); self.assertEqual(r1.status_code, 201, r1.text); self.assertEqual((r1.json()['slots'], r1.json()['interval']), (1, [t, t + 3600]))
        r2 = self.reserve(self.M, b, 3600 * 1000, 'b-full', t + 1800, t + 5400); self.assertEqual(r2.status_code, 409, r2.text); d = r2.json()['detail']; self.assertEqual(d['code'], 'pool_capacity_exceeded'); self.assertEqual((d['segment']['start'], d['segment']['end']), (t + 1800, t + 3600))
        self.assertEqual(self.bal('machine', 'reserved'), 0)                                                         # nothing reserved on refusal
        r3 = self.reserve(self.M, b, 1800 * 1000, 'b-tail', t + 3600, t + 5400); self.assertEqual(r3.status_code, 201, r3.text)   # the non-overlapping tail of offer b is fine
        v = self.c.get('/api/v1/resources/pools/' + p['id'], headers=self.R).json(); self.assertTrue(v['all_within_capacity']); self.assertEqual(v['peak_slots_used'], 1); self.assertEqual(len(v['offers']), 2)
        self.assertEqual(self.c.get('/api/v1/resources/offers/' + a['id'], headers=self.R).json()['pool_id'], p['id'])

    def test_race_across_two_offers_conserves_capacity_and_funds(self):
        p = self.pool(cap=2); t = self.t0; a = self.offer(p['id'], t, t + 3600, 7200 * 1000); b = self.offer(p['id'], t, t + 3600, 7200 * 1000); results = []; lock = threading.Lock()
        def go(h, o, who):
            r = self.reserve(h, o, 3600 * 1000, 'race-' + who, t, t + 3600)
            with lock:
                results.append((who, r.status_code))
        th = [threading.Thread(target=go, args=(self.R, a, 'r1')), threading.Thread(target=go, args=(self.M, b, 'm1')), threading.Thread(target=go, args=(self.R, b, 'r2')), threading.Thread(target=go, args=(self.M, a, 'm2'))]
        [x.start() for x in th]; [x.join() for x in th]
        self.assertEqual(sorted(s for _, s in results), [201, 201, 409, 409])
        v = self.c.get('/api/v1/resources/pools/' + p['id'], headers=self.R).json(); self.assertEqual(v['peak_slots_used'], 2); self.assertTrue(v['all_within_capacity'])
        reserved = self.bal('requester', 'reserved') + self.bal('machine', 'reserved'); self.assertEqual(reserved, 2 * 3600 * 1000); self.assertTrue(self.c.get('/api/v1/native/audit', headers=self.H).json()['conserved'])

    def test_boundaries_and_unit_conversion_exact(self):
        p = self.pool(cap=1, u=1000, res=10); t = self.t0; o = self.offer(p['id'], t, t + 600, 600 * 1000)
        self.assertEqual(self.reserve(self.R, o, 30 * 1000, 'x1', t, t + 30).status_code, 201)
        self.assertEqual(self.reserve(self.M, o, 30 * 1000, 'x2', t + 30, t + 60).status_code, 201)                    # touching: [t, t+30) and [t+30, t+60)
        r = self.reserve(self.M, o, 1, 'x3', t + 29, t + 31); self.assertEqual(r.status_code, 409)                      # aligned outward to [t+20, t+40): overlaps both
        self.assertEqual(self.reserve(self.M, o, 10 * 1000, 'x4', t + 60, t + 70).status_code, 201)
        r = self.reserve(self.M, o, 10 * 1000 + 1, 'x5', t + 70, t + 80); self.assertEqual(r.status_code, 409)         # 10001 cpu_ms over 10 s needs 2 slots of a 1-slot pool
        sp = self.pool(cap=4, u=1, kind='storage', unit='kib', h=self.S); so = self.offer(sp['id'], t, t + 100, 400, kind='storage')
        r = self.reserve(self.R, so, 300, 's1', t, t + 100); self.assertEqual((r.status_code, r.json()['slots']), (201, 3))      # 300 kib_seconds over 100 s = 3 KiB slots
        r = self.reserve(self.M, so, 101, 's2', t, t + 100); self.assertEqual(r.status_code, 409)                        # 2 more KiB slots would exceed 4
        self.assertEqual(self.reserve(self.M, so, 100, 's3', t, t + 100).status_code, 201)

    def test_capacity_reduction_conflicting_with_commitments_refused(self):
        p = self.pool(cap=2); t = self.t0; o = self.offer(p['id'], t, t + 3600, 7200 * 1000)
        r = self.reserve(self.R, o, 3600 * 1000, 'c1', t, t + 3600); self.assertEqual(r.status_code, 201); rid = r.json()['id']
        am = self.c.post('/api/v1/resources/pools/%s/amend' % p['id'], headers=self.S, json={'schedule': [{'start': t, 'end': t + 7200, 'capacity': 0}]}); self.assertEqual(am.status_code, 409); self.assertEqual(am.json()['detail']['code'], 'capacity_below_commitments'); self.assertIn(rid, am.json()['detail']['affected_reservations'])
        am = self.c.post('/api/v1/resources/pools/%s/amend' % p['id'], headers=self.S, json={'schedule': [{'start': t, 'end': t + 7200, 'capacity': 1}]}); self.assertEqual(am.status_code, 200, am.text); self.assertEqual(am.json()['version'], 2)
        am = self.c.post('/api/v1/resources/pools/%s/amend' % p['id'], headers=self.S, json={'schedule': [{'start': t + 3600, 'end': t + 7200, 'capacity': 1}]}); self.assertEqual(am.status_code, 409)   # removing the committed hour
        self.assertEqual(self.c.post('/api/v1/resources/pools/%s/amend' % p['id'], headers=self.R, json={'schedule': []}).status_code, 403)
        # a delivered reservation keeps its capacity committed; the delivered interval cannot be reduced away either
        self.c.post('/api/v1/resources/reservations/%s/activate' % rid, headers=self.S, json={}); self.c.post('/api/v1/resources/reservations/%s/deliver' % rid, headers=self.S, json={'task': {'kind': 'terrain_tile', 'source': 'apollo11_32'}})
        self.c.post('/api/v1/resources/reservations/%s/settle' % rid, headers=self.R, json={})
        v = self.c.get('/api/v1/resources/pools/' + p['id'], headers=self.R).json(); self.assertEqual([c['state'] for c in v['commitments']], ['delivered'])
        self.assertEqual(self.c.post('/api/v1/resources/pools/%s/amend' % p['id'], headers=self.S, json={'schedule': [{'start': t, 'end': t + 7200, 'capacity': 0}]}).status_code, 409)

    def test_drain_keeps_delivered_work_and_obligations(self):
        p = self.pool(cap=2); t = self.t0; o = self.offer(p['id'], t, t + 3600, 7200 * 1000)
        r1 = self.reserve(self.R, o, 1000 * 1000, 'd1', t, t + 1000).json(); self.c.post('/api/v1/resources/reservations/%s/activate' % r1['id'], headers=self.S, json={})
        self.c.post('/api/v1/resources/reservations/%s/deliver' % r1['id'], headers=self.S, json={'task': {'kind': 'terrain_tile', 'source': 'apollo11_32'}})
        dr = self.c.post('/api/v1/resources/pools/%s/drain' % p['id'], headers=self.S, json={}); self.assertEqual(dr.json()['state'], 'draining')
        r2 = self.reserve(self.M, o, 1000 * 1000, 'd2', t + 1000, t + 2000); self.assertEqual((r2.status_code, r2.json()['detail']['code']), (409, 'provider_draining'))
        self.assertEqual(self.offer(p['id'], t + 3600, t + 7200, 3600 * 1000, expect=409)['detail']['code'], 'provider_draining')         # publishing on a draining pool is refused too
        self.assertEqual(self.c.post('/api/v1/resources/pools/%s/close' % p['id'], headers=self.S, json={}).json()['state'], 'closed')     # delivered-but-unsettled work does not block closing: it occupies no future capacity
        s = self.c.post('/api/v1/resources/reservations/%s/settle' % r1['id'], headers=self.R, json={}).json(); self.assertEqual(s['state'], 'settled'); self.assertGreater(s['obligation']['settled_amount'], 0)   # and still settles after closing
        v = self.c.get('/api/v1/resources/pools/' + p['id'], headers=self.R).json(); self.assertEqual([c['state'] for c in v['commitments']], ['delivered']); self.assertEqual(self.bal('supplier'), s['obligation']['settled_amount'])
        # a cancel after activation keeps the delivered part of the commitment and frees only the future tail
        n0 = int(time.time()) - 100; p2 = self.pool(cap=1, sched=[{'start': n0, 'end': n0 + 7200, 'capacity': 1}]); o2 = self.offer(p2['id'], n0, n0 + 7200, 7200 * 1000)
        r3 = self.reserve(self.R, o2, 7200 * 1000, 'd3', n0, n0 + 7200).json(); self.c.post('/api/v1/resources/reservations/%s/activate' % r3['id'], headers=self.S, json={})
        self.c.post('/api/v1/resources/reservations/%s/cancel' % r3['id'], headers=self.R, json={}); v2 = self.c.get('/api/v1/resources/pools/' + p2['id'], headers=self.R).json(); c = v2['commitments'][0]
        self.assertEqual(c['state'], 'delivered'); self.assertLess(c['end'], n0 + 7200); self.assertGreaterEqual(c['end'], int(time.time()) - 1)      # truncated at the release point: the elapsed part stays committed
        p3 = self.pool(cap=1); o3 = self.offer(p3['id'], t, t + 3600, 3600 * 1000); r4 = self.reserve(self.R, o3, 3600 * 1000, 'd4', t, t + 3600).json(); self.c.post('/api/v1/resources/reservations/%s/activate' % r4['id'], headers=self.S, json={})
        self.c.post('/api/v1/resources/reservations/%s/cancel' % r4['id'], headers=self.R, json={}); self.assertEqual(self.c.get('/api/v1/resources/pools/' + p3['id'], headers=self.R).json()['commitments'], [])   # a wholly future interval is released in full

    def test_implicit_pool_for_plain_offers_and_legacy_semantics(self):
        r = self.c.post('/api/v1/resources/offers', headers=self.S, json={'kind': 'contact', 'resource_id': 'c-plain', 'capacity': 64, 'price_per_unit': 1, 'window_start': self.t0, 'window_end': self.t0 + 64}); self.assertEqual(r.status_code, 201, r.text); o = r.json()
        self.assertTrue(o['pool_id']); v = self.c.get('/api/v1/resources/pools/' + o['pool_id'], headers=self.R).json(); self.assertEqual((v['label'], v['schedule'][0]['capacity'], v['units_per_slot_second']), ('implicit pool of one offer', 64, 1))
        self.assertEqual(self.reserve(self.R, o, 40, 'p1').status_code, 201); self.assertEqual(self.reserve(self.M, o, 24, 'p2').status_code, 201); self.assertEqual(self.reserve(self.M, o, 1, 'p3').status_code, 409)


if __name__ == '__main__':
    unittest.main()
