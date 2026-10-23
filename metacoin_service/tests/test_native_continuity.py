"""Mission continuity and fenced takeover (order 2026-10-23 group H; journeys 36–38, 40 at unit level)."""
import unittest

from metacoin_service.tests.test_native_missions import MissionBase


class ContinuityTests(MissionBase):
    def test_takeover_fences_stale_controllers_and_continuity_reconstructs(self):
        m = self.mission(policy={'compute_cpu_ms': 20}); mid = m['id']
        s = self.step(mid); self.assertEqual(s['performed']['action'], 'open_request')                                   # unfenced mission: compatible behaviour
        c = self.c.get('/api/v1/missions/%s/continuity' % mid, headers=self.R).json(); self.assertIsNone(c['active_generation']); self.assertEqual(c['pending_operations'][0]['milestone'], 'acquire'); self.assertTrue(c['recovery_evidence']['recovery_state'])
        # controller A obtains generation 1 from the authority
        t1 = self.c.post('/api/v1/missions/%s/takeover' % mid, headers=self.R, json={'holder': 'runner-A'}); self.assertEqual(t1.status_code, 200, t1.text); self.assertEqual((t1.json()['generation'], t1.json()['superseded']), (1, None))
        self.assertEqual(self.c.post('/api/v1/missions/%s/takeover' % mid, headers=self.actors['supplier']['h'], json={'holder': 'intruder'}).status_code, 403)   # not a party
        bare = self.c.post('/api/v1/missions/%s/step' % mid, headers=self.R, json={}); self.assertEqual((bare.status_code, bare.json()['detail']['code']), (409, 'stale_generation'))   # no generation presented
        self.offer_as_machine(self.ms(mid, 'acquire')['binding']['request_id'], 4)
        s = self.c.post('/api/v1/missions/%s/step' % mid, headers=self.R, json={'generation': 1}).json(); self.assertEqual(s['performed']['action'], 'award')
        # journey 36: controller A dies; controller B reconstructs from the continuity record and obtains generation 2
        c = self.c.get('/api/v1/missions/%s/continuity' % mid, headers=self.R).json(); self.assertEqual(c['active_generation']['holder'], 'runner-A'); self.assertEqual(c['pending_operations'][0]['state'], 'executing')
        t2 = self.c.post('/api/v1/missions/%s/takeover' % mid, headers=self.R, json={'holder': 'runner-B', 'reason': 'A unresponsive'}).json(); self.assertEqual((t2['generation'], t2['superseded']), (2, 1))
        # journey 37: A's stale mutations are refused and recorded; journey 38: a self-issued/forged generation is refused
        for _ in range(3):
            self.w.run_once()
        st = self.c.post('/api/v1/missions/%s/step' % mid, headers=self.R, json={'generation': 1}); self.assertEqual((st.status_code, st.json()['detail']['code'], st.json()['detail']['active']), (409, 'stale_generation', 2))
        hd = self.c.post('/api/v1/missions/%s/step' % mid, headers=dict(self.R, **{'X-Controller-Generation': '1'}), json={}); self.assertEqual(hd.json()['detail']['code'], 'stale_generation')
        forged = self.c.post('/api/v1/missions/%s/step' % mid, headers=self.R, json={'generation': 99}); self.assertEqual(forged.json()['detail']['code'], 'stale_generation')
        self.assertEqual(self.c.post('/api/v1/missions/%s/run' % mid, headers=self.R, json={'generation': 1}).json()['detail']['code'], 'stale_generation')
        self.assertEqual(self.c.post('/api/v1/missions/%s/propose' % mid, headers=self.R, json={'generation': 1, 'action': 'verify', 'milestone': 'acquire'}).json()['detail']['code'], 'stale_generation')
        ev = [e for e in self.view(mid)['events'] if e['kind'] == 'stale_generation_refused']; self.assertGreaterEqual(len(ev), 4)
        s = self.c.post('/api/v1/missions/%s/step' % mid, headers=self.R, json={'generation': 2}).json(); self.assertEqual(s['performed']['action'], 'verify')
        for _ in range(3):
            self.w.run_once()
        s = self.c.post('/api/v1/missions/%s/step' % mid, headers=self.R, json={'generation': 2}).json(); self.assertEqual((s['performed']['action'], s['result']['decision']), ('decide', 'accepted'))
        c = self.c.get('/api/v1/missions/%s/continuity' % mid, headers=self.R).json(); self.assertEqual([g['state'] for g in c['generations']], ['superseded', 'active']); self.assertTrue(any(a['action'] == 'pay' for a in c['safe_actions']))
        # quarantine blocks new generations (journey 39 relation): simulate by setting the domain recovery state
        with self.inst.app.state.services.db.tx() as db:
            db.execute("UPDATE native_domains SET recovery_state='quarantine'")
        q = self.c.post('/api/v1/missions/%s/takeover' % mid, headers=self.R, json={'holder': 'runner-C'}); self.assertEqual((q.status_code, q.json()['detail']['code']), (409, 'domain_quarantined'))
        with self.inst.app.state.services.db.tx() as db:
            db.execute("UPDATE native_domains SET recovery_state='normal'")
        # a manual pause keeps obligations and earned funds; new work stops
        p = self.c.post('/api/v1/missions/%s/pause' % mid, headers=self.R, json={'reason': 'operator pause'}); self.assertEqual(p.status_code, 200, p.text)
        c = self.c.get('/api/v1/missions/%s/continuity' % mid, headers=self.R).json(); self.assertTrue(c['stop_reason'] is None or c['stop_reason'].startswith('paused')); led = self.c.get('/api/v1/missions/%s/ledger' % mid, headers=self.R).json(); self.assertEqual(led['totals']['reserved'], 4)


if __name__ == '__main__':
    unittest.main()
