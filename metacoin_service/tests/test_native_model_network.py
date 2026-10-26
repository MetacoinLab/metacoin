"""Bounded network model vs implementation (order 2026-10-23 §17): every example sequence of native/model_network.py is replayed
through real instances (pools across overlapping offers, a funded subcontract, claim-lot splitting and partial retirement, fenced
controller generations) and the projected final state must agree with the model; the exploration itself reports no violation."""
import time
import unittest

from metacoin_service.native import courier, model_network as M
from metacoin_service.tests.test_native_lots import LotTests
from metacoin_service.tests.test_native_pools import PoolTests
from metacoin_service.tests.test_native_subcontracts import SubcontractTests
from metacoin_service.tests.test_native_missions import MissionBase


class ExplorationTests(unittest.TestCase):
    def test_bounded_exploration_has_no_violations(self):
        r = M.explore(6); self.assertEqual(r['violation_count'], 0, r['violations'][:3]); self.assertGreater(r['reachable_states'], 200); self.assertGreater(r['outcomes']['fenced_refusals'], 0); self.assertGreater(r['outcomes']['partially_returned'], 0)


class PoolReplay(PoolTests):
    def test_pool_sequences_agree(self):
        t = self.t0; S, R, Mh = self.S, self.R, self.M
        for seq in M.example_sequences()[:2]:
            with self.subTest(sequence=seq):
                p = self.pool(cap=1, u=1, kind='contact', unit='channel'); a = self.offer(p['id'], t, t + 7200, 7200, kind='contact'); b = self.offer(p['id'], t + 3600, t + 10800, 7200, kind='contact'); req0, mach0, sup0 = self.bal('requester'), self.bal('machine'), self.bal('supplier'); ids = {}
                for ev in seq:
                    if ev in ('reserve_a', 'reserve_b', 'reserve_b_retry'):
                        who, o, (s0, s1) = (R, a, (t, t + 7200)) if ev == 'reserve_a' else (Mh, b, (t + 3600, t + 10800)); r = self.reserve(who, o, 4, 'm-' + ev[:9] + p['id'], s0, s1)
                        if r.status_code == 201:
                            ids[ev[8]] = r.json()['id']
                    elif ev == 'crash_after_pool_commit':
                        self.c.post('/api/v1/ops/faults', headers=self.H, json={'fault': 'pool_commit'}); r = self.reserve(Mh, b, 4, 'm-crash' + p['id'], t + 3600, t + 10800); self.assertEqual(r.status_code, 409); self.c.post('/api/v1/ops/faults', headers=self.H, json={'fault': 'pool_commit', 'disarm': True})
                    elif ev == 'cancel_b' and 'b' in ids:
                        self.c.post('/api/v1/resources/reservations/%s/cancel' % ids['b'], headers=Mh, json={})
                    elif ev == 'deliver_a' and 'a' in ids:
                        self.c.post('/api/v1/resources/reservations/%s/activate' % ids['a'], headers=S, json={}); self.c.post('/api/v1/resources/reservations/%s/deliver' % ids['a'], headers=S, json={'bytes': 4 * 1024}); self.c.post('/api/v1/resources/reservations/%s/settle' % ids['a'], headers=R, json={})
                want = M.expected_final(seq); v = self.c.get('/api/v1/resources/pools/' + p['id'], headers=R).json()
                got = {'avail': {'req': 40 + self.bal('requester') - req0, 'mach': self.bal('machine') - mach0, 'sup': self.bal('supplier') - sup0}, 'reserved': {'req': self.bal('requester', 'reserved'), 'mach': self.bal('machine', 'reserved')}, 'pool': sorted(('a' if c['reservation_id'] == ids.get('a') else 'b', c['state']) for c in v['commitments'] if c['state'] != 'released')}
                self.assertEqual(got, {'avail': {k: want['avail'][k] for k in ('req', 'mach', 'sup')}, 'reserved': want['reserved'], 'pool': want['pool']}, seq)
                self.assertTrue(v['all_within_capacity']); self.assertTrue(self.c.get('/api/v1/native/audit', headers=self.H).json()['conserved'])


class SubcontractReplay(SubcontractTests):
    def test_subcontract_sequence_agrees(self):
        seq = M.example_sequences()[2]; mid = self.parent_awarded(); Mh = self.M; req0, mach0 = self.bal('requester'), self.bal('machine'); sid = None
        for ev in seq:
            if ev == 'sub_propose':
                sid = self.c.post('/api/v1/subcontracts', headers=Mh, json=self.sub_body(mid)).json()['id']
            elif ev == 'sub_fund_from_future':
                self.assertEqual(self.c.post('/api/v1/subcontracts', headers=Mh, json=self.sub_body(mid, mode='parent_future_earnings', label='FUT')).json()['detail']['code'], 'circular_funding')
            elif ev in ('sub_fund', 'sub_fund_retry'):
                r = self.c.post('/api/v1/subcontracts/%s/prefund' % sid, headers=self.R, json={}); self.assertIn(r.status_code, (200, 409))
            elif ev == 'sub_pay':
                s = self.drive_child(sid, Mh); self.assertEqual(s['step']['result']['decision'], 'accepted'); self.c.post('/api/v1/subcontracts/%s/step' % sid, headers=Mh, json={})
            elif ev == 'sub_pay_again':
                self.c.post('/api/v1/subcontracts/%s/step' % sid, headers=Mh, json={})
            elif ev == 'sub_return':
                self.c.post('/api/v1/subcontracts/%s/return-unused' % sid, headers=Mh, json={})
        want = M.expected_final(seq); got = {'req': 40 + self.bal('requester') - req0, 'mach': self.bal('machine') - mach0, 'sub': self.bal('subco'), 'state': self.c.get('/api/v1/subcontracts/' + sid, headers=Mh).json()['state']}
        self.assertEqual(got, {'req': want['avail']['req'], 'mach': want['avail']['mach'], 'sub': want['avail']['sub'], 'state': want['sub']})

    def test_funded_subcontract_bound_to_parent_executes_from_prefund_and_returns_unused(self):
        pass

    def test_failed_subcontract_preserves_accepted_work_and_parent_duty(self):
        pass


class LotReplay(LotTests):
    def test_lot_sequences_agree(self):
        for seq in M.example_sequences()[3:5]:
            with self.subTest(sequence=seq):
                self.setUp(); A, B = self.A, self.B; bob, carol = self.bob['account'], self.carol['account']; alice0 = A.bal(self.alice['account']); a = None
                for ev in seq:
                    if ev == 'export_20':
                        a = A.export('alice', B, bob, 20, rid='m-lot20')
                    elif ev == 'deliver_cert':
                        courier.push(A.ep, B.ep, 'dom-b'); ob = A.c.get('/api/v1/native/transport/outbox', headers=A.H, params={'receiver': 'dom-b', 'all': '1'}).json(); B.c.post('/api/v1/native/transport/inbox', headers=B.H, json=ob)
                    elif ev == 'spend_7':
                        B.c.post('/api/v1/native/transfers', headers=self.bob['h'], json={'to_account': carol, 'amount': 7, 'asset': A.asset, 'request_id': 'm-s7'})
                    elif ev == 'retire_other':
                        self.assertEqual(B.c.post('/api/v1/native/allocations/%s/return' % a['id'], headers=self.carol['h'], json={'amount': 7}).status_code, 403)
                    elif ev in ('retire_13', 'retire_13_again'):
                        r = B.c.post('/api/v1/native/allocations/%s/return' % a['id'], headers=self.bob['h'], json={'amount': 13}); self.assertIn(r.status_code, (200, 409))
                    elif ev == 'retire_7_by_holder':
                        r = B.c.post('/api/v1/native/allocations/%s/return' % a['id'], headers=self.carol['h'], json={'amount': 7}); self.assertIn(r.status_code, (403, 409))
                    elif ev in ('deliver_retire', 'deliver_retire_again'):
                        courier.push(B.ep, A.ep, 'dom-a'); ob = B.c.get('/api/v1/native/transport/outbox', headers=B.H, params={'receiver': 'dom-a', 'all': '1'}).json(); A.c.post('/api/v1/native/transport/inbox', headers=A.H, json=ob)
                want = M.expected_final(seq); sa = A.alloc(a['id'])
                got = {'req': 40 + A.bal(self.alice['account']) - alice0, 'backing': A.audit()['backing_locked_for_exports'], 'state': sa['state'], 'returned': sa['returned_amount'], 'lots': {'bob': B.bal(bob, asset=A.asset), 'carol': B.bal(carol, asset=A.asset)}}
                self.assertEqual(got, {'req': want['avail']['req'], 'backing': want['backing'], 'state': want['alloc']['state'], 'returned': want['alloc']['returned'], 'lots': want['alloc']['lots']}, seq)
                self.assertTrue(A.audit()['conserved'] and B.audit(A.asset)['conserved']); self.A.close(); self.B.close()

    def test_split_spend_and_retire_only_the_holders_remainder(self):
        pass

    def test_mixed_asset_missing_lineage_and_crash_boundaries(self):
        pass

    def test_v1_peer_refuses_v2_messages_instead_of_reinterpreting(self):
        pass


class GenerationReplay(MissionBase):
    def test_generation_sequence_agrees(self):
        seq = M.example_sequences()[5]; m = self.mission(policy={'compute_cpu_ms': 20}); mid = m['id']; gen = 0
        for ev in seq:
            if ev == 'takeover':
                gen = self.c.post('/api/v1/missions/%s/takeover' % mid, headers=self.R, json={'holder': 'runner-%d' % (gen + 1)}).json()['generation']
            elif ev == 'step_gen_old':
                self.assertEqual(self.c.post('/api/v1/missions/%s/step' % mid, headers=self.R, json={'generation': gen - 1}).json()['detail']['code'], 'stale_generation')
            elif ev == 'step_gen_forged':
                self.assertEqual(self.c.post('/api/v1/missions/%s/step' % mid, headers=self.R, json={'generation': 99}).json()['detail']['code'], 'stale_generation')
            elif ev == 'step_gen_active':
                self.assertEqual(self.c.post('/api/v1/missions/%s/step' % mid, headers=self.R, json={'generation': gen}).status_code, 200)
        want = M.expected_final(seq); c = self.c.get('/api/v1/missions/%s/continuity' % mid, headers=self.R).json(); refused = len([e for e in self.view(mid)['events'] if e['kind'] == 'stale_generation_refused'])
        self.assertEqual((c['active_generation']['generation'], refused), (want['gen'], want['refused']))


if __name__ == '__main__':
    unittest.main()
