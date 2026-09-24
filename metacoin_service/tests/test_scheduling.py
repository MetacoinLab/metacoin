"""Worker registry, capabilities, draining, deterministic fair scheduling, queue view, persisted quotas."""
import unittest
from metacoin_service.tests.test_service import Instance, own_inputs
from metacoin_service import scheduling, worker as worker_mod, artifacts, db as database
from metacoin_service.db import now


class SchedulingTests(unittest.TestCase):
    def setUp(self):
        self.inst = Instance(); self.addCleanup(self.inst.close); self.c = self.inst.client; self.H = self.inst.h('owner')

    def worker(self, name, caps=None):
        return worker_mod.Worker(database.Database(self.inst.settings.db_path), artifacts.ArtifactStore(self.inst.settings), self.inst.settings, name=name, capabilities=caps)

    def test_capabilities_draining_and_queue_reasons(self):
        # no worker yet: every queued job explains that
        j1 = self.inst.job()
        q = self.c.get('/api/v1/queue', headers=self.H).json()
        self.assertEqual(q['queued'][0]['waiting_reason'], 'no live worker registered')
        # a worker that cannot run energy audits leaves the job waiting with the capability named
        w_sr = self.worker('runtime-only', ['safe_runtime'])
        self.assertIsNone(w_sr.run_once())
        q = self.c.get('/api/v1/queue', headers=self.H).json()
        self.assertEqual(q['queued'][0]['waiting_reason'], 'no live worker declares capability energy_audit')
        self.assertEqual([w['name'] for w in q['workers']], ['runtime-only'])
        self.assertEqual(q['live_capabilities'], ['safe_runtime'])
        # unknown capabilities are refused at registration
        with self.assertRaises(Exception):
            self.worker('bad', ['shell'])
        # a capable worker is drained by the operator: it claims nothing while draining, resumes afterwards
        w_all = self.worker('general')
        wid = w_all.worker_id
        self.assertEqual(self.c.post('/api/v1/workers/' + wid + '/drain', headers=self.inst.h('viewer')).status_code, 403)
        self.assertEqual(self.c.post('/api/v1/workers/' + wid + '/drain', headers=self.H).json()['state'], 'draining')
        self.assertIsNone(w_all.run_once())
        q = self.c.get('/api/v1/queue', headers=self.H).json()
        self.assertEqual(q['queued'][0]['waiting_reason'], 'no live worker declares capability energy_audit')   # the only live active worker lacks it
        self.assertEqual(self.c.post('/api/v1/workers/' + wid + '/resume', headers=self.H).json()['state'], 'active')
        q = self.c.get('/api/v1/queue', headers=self.H).json()
        self.assertEqual(q['queued'][0]['waiting_reason'], 'next to run')
        ran = w_all.run_once()
        self.assertEqual(ran[0], j1)
        self.assertEqual(self.c.get('/api/v1/jobs/' + j1, headers=self.H).json()['state'], 'succeeded')
        # offline workers drop out of the live set; stale heartbeats are reported
        w_all.offline()
        self.assertFalse([w for w in self.c.get('/api/v1/workers', headers=self.H).json()['items'] if w['id'] == wid][0]['live'])
        with database.Database(self.inst.settings.db_path).tx() as db:
            db.execute('UPDATE workers SET last_heartbeat=? WHERE id=?', (now() - 1000, w_sr.worker_id))
        self.assertEqual(self.c.get('/api/v1/queue', headers=self.H).json()['live_capabilities'], [])

    def test_fair_order_is_deterministic_across_submitters(self):
        # a second submitter with one job already running gets served after the submitter with none
        a1 = self.inst.job(); a2 = self.inst.job(); a3 = self.inst.job()
        with database.Database(self.inst.settings.db_path).tx() as db:
            db.execute("INSERT INTO principals (id, workspace, name, role, created_at) VALUES ('p_other','ws_default','other','owner',?)", (now(),))
            db.execute("UPDATE jobs SET submitted_by='p_other' WHERE id IN (?, ?)", (a2, a3))
            db.execute("UPDATE jobs SET state='running', lease_owner='w_x', lease_expires=? WHERE id=?", (now() + 60, a2))
            order = [r['id'] for r in scheduling.fair_order(db, None)]
        self.assertEqual(order, [a1, a3])                      # a1 (owner, 0 running) before a3 (other, 1 running) despite creation order
        q = self.c.get('/api/v1/queue', headers=self.H).json()
        self.assertEqual([i['job_id'] for i in q['queued']], [a1, a3])
        self.assertEqual([i['predicted_position'] for i in q['queued']], [0, 1])
        self.assertEqual(q['running'][0]['id'], a2)
        w = self.worker('general')
        self.assertEqual(w.run_once()[0], a1)
        self.assertEqual(w.run_once()[0], a3)

    def test_persisted_quotas_refuse_at_admission_with_the_quota_named(self):
        oid = self.inst.ids['owner']
        self.assertEqual(self.c.put('/api/v1/quotas/' + oid, headers=self.inst.h('viewer'), json={'max_queued': 1, 'max_per_minute': 10}).status_code, 403)
        self.assertEqual(self.c.put('/api/v1/quotas/nobody', headers=self.H, json={'max_queued': 1, 'max_per_minute': 10}).status_code, 404)
        r = self.c.put('/api/v1/quotas/' + oid, headers=self.H, json={'max_queued': 1, 'max_per_minute': 10})
        self.assertEqual(r.json()['items'][0]['max_queued'], 1)
        self.inst.job()
        refused = self.c.post('/api/v1/jobs', headers=self.H, json={'contract_id': self.inst.contract(inputs=own_inputs('SECOND'))})
        self.assertEqual((refused.status_code, refused.json()['detail']['code'], refused.json()['detail']['in_flight']), (429, 'quota_max_queued', 1))
        self.worker('general').run_once()
        self.assertEqual(self.c.post('/api/v1/jobs', headers=self.H, json={'contract_id': self.inst.contract(inputs=own_inputs('THIRD'))}).status_code, 202)
        # per-minute quota, applied through the workspace default entry '*'
        self.c.put('/api/v1/quotas/*', headers=self.H, json={'max_queued': 50, 'max_per_minute': 2})
        self.c.put('/api/v1/quotas/' + oid, headers=self.H, json={'max_queued': 50, 'max_per_minute': 2})
        refused = self.c.post('/api/v1/jobs', headers=self.H, json={'contract_id': self.inst.contract(inputs=own_inputs('FOURTH'))})
        self.assertEqual(refused.json()['detail']['code'], 'quota_max_per_minute')
        # the quota survives a service restart (persisted, not in memory)
        self.inst.reopen()
        self.assertEqual(self.inst.client.get('/api/v1/quotas', headers=self.H).json()['items'][1]['max_per_minute'], 2)


if __name__ == '__main__':
    unittest.main()
