"""Operational status/metrics, bounded search without cross-workspace exposure, safe CSV export, schema compatibility, prior-database migration."""
import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from metacoin_service.tests.test_service import Instance, own_inputs
from metacoin_service.tests.test_budgets import two_node_definition
from metacoin_service import db as database, worker as worker_mod, artifacts, config, api
from metacoin_service.db import now


class OpsAndSearchTests(unittest.TestCase):
    def setUp(self):
        self.inst = Instance(); self.addCleanup(self.inst.close); self.c = self.inst.client; self.H = self.inst.h('owner')

    def test_status_and_metrics_have_bounded_labels(self):
        jid = self.inst.job()
        wid = self.c.post('/api/v1/workflows', headers=self.H, json={'definition': two_node_definition('ops')}).json()['id']
        rid = self.c.post('/api/v1/workflows/' + wid + '/runs', headers=self.H, json={}).json()['run_id']
        s = self.c.get('/api/v1/status', headers=self.H).json()
        self.assertEqual((s['jobs_by_state']['queued'], s['queue_backlog']['energy_audit'], s['runs_by_state']['running']), (3, 3, 1))   # 1 direct + 2 workflow nodes dispatched at start
        self.assertEqual(s['workers']['registered'], 0)
        m = self.c.get('/api/metrics', headers=self.H)
        self.assertEqual(m.status_code, 200)
        self.assertIn('metacoin_jobs{state="queued"} 3', m.text)
        self.assertNotIn(jid, m.text); self.assertNotIn(rid, m.text); self.assertNotIn('ops', m.text.replace('metacoin_', ''))   # no ids or names as labels
        self.assertEqual(self.c.get('/api/metrics').status_code, 401)

    def test_search_is_workspace_scoped_paginated_and_filterable(self):
        csv = "duration_s,harvest_low_mW,harvest_high_mW,load_low_mW,load_high_mW\n10,600,800,500,500\n"
        self.c.post('/api/v1/datasets', headers=self.H, json={'name': 'series-a', 'kind': 'temporal_series', 'format': 'csv', 'content': csv, 'provenance': 'declared'})
        wid = self.c.post('/api/v1/workflows', headers=self.H, json={'definition': two_node_definition('search')}).json()['id']
        rid = self.c.post('/api/v1/workflows/' + wid + '/runs', headers=self.H, json={}).json()['run_id']
        # plant objects in another workspace directly: they must never be counted, listed or suggested
        with database.Database(self.inst.settings.db_path).tx() as db:
            db.execute("INSERT INTO principals (id, workspace, name, role, created_at) VALUES ('p_foreign','ws_other','foreign','owner',?)", (now(),))
            db.execute("INSERT INTO datasets (id, workspace, owner_id, name, kind, tags, created_at) VALUES ('ds_foreign','ws_other','p_foreign','SECRET-FOREIGN-DATASET','temporal_series','[\"secret\"]',?)", (now(),))
        r = self.c.get('/api/v1/search', headers=self.H).json()
        self.assertEqual(r['ordering'], 'created_at desc, id desc')
        types = sorted({i['type'] for i in r['items']})
        self.assertEqual(types, ['dataset', 'run', 'service', 'workflow_definition'])
        self.assertNotIn('SECRET-FOREIGN', str(r)); self.assertEqual(r['total_matching'], len(r['items']))
        self.assertEqual([i['id'] for i in self.c.get('/api/v1/search?type=run&status=running', headers=self.H).json()['items']], [rid])
        self.assertEqual(self.c.get('/api/v1/search?type=run&status=completed', headers=self.H).json()['items'], [])
        self.assertEqual(self.c.get('/api/v1/search?type=dataset&creator=p_foreign', headers=self.H).json()['total_matching'], 0)
        self.assertEqual(self.c.get('/api/v1/search?type=dataset&tag=secret', headers=self.H).json()['total_matching'], 0)
        self.assertEqual(self.c.get('/api/v1/search?type=service&model=temporal_energy', headers=self.H).json()['items'][0]['name'], 'temporal-energy')
        self.assertEqual(self.c.get('/api/v1/search?type=workflow_definition&model=energy_audit', headers=self.H).json()['items'][0]['id'], wid)
        self.assertEqual(self.c.get('/api/v1/search?type=bogus', headers=self.H).status_code, 422)
        page = self.c.get('/api/v1/search?limit=2', headers=self.H).json()
        self.assertEqual((len(page['items']), page['more']), (2, True))
        nxt = self.c.get('/api/v1/search?limit=2&before=%d' % page['next_before'], headers=self.H).json()
        self.assertTrue(all(i['created_at'] < page['next_before'] for i in nxt['items']))
        self.assertEqual(self.c.get('/api/v1/search?since=%d' % (now() + 100), headers=self.H).json()['total_matching'], 0)
        # a viewer may search (read-only objects) and still sees nothing foreign
        v = self.c.get('/api/v1/search?type=dataset', headers=self.inst.h('viewer')).json()
        self.assertEqual([i['name'] for i in v['items']], ['series-a'])

    def test_results_csv_is_safe_and_distinguishes_missing_from_withheld(self):
        cid = self.inst.contract(title='=HYPERLINK("x")')
        jid = self.inst.job(cid=cid)
        self.inst.worker().run_once()
        j2 = self.inst.job(cid=self.inst.contract(inputs=own_inputs('OTHER')))                                # stays queued
        text = self.c.get('/api/v1/results.csv', headers=self.H).text
        lines = text.strip().split('\n')
        self.assertEqual(lines[0].split(','), ['job_id', 'kind', 'model_id', 'verifier_digest', 'state', 'review_state', 'outcome', 'evidence_root', 'reused_from', 'created_at', 'finished_at'])
        rows = {l.split(',')[0]: l.split(',') for l in lines[1:]}
        self.assertEqual(rows[jid][6], rows[jid][6].strip()); self.assertTrue(rows[jid][6])                   # a real outcome
        self.assertEqual(rows[j2][6], '')                                                                     # queued: missing, not zero, not withheld
        viewer_text = self.c.get('/api/v1/results.csv', headers=self.inst.h('viewer')).text
        vrows = {l.split(',')[0]: l.split(',') for l in viewer_text.strip().split('\n')[1:]}
        self.assertEqual(vrows[jid][6], 'withheld-by-policy-or-not-yet-reviewed')
        self.assertNotIn('USER_PRIVATE_LABEL', text + viewer_text)
        # formula-looking cells are neutralised
        with database.Database(self.inst.settings.db_path).tx() as db:
            db.execute("UPDATE jobs SET outcome='=cmd|calc' WHERE id=?", (jid,))
        self.assertIn("'=cmd|calc", self.c.get('/api/v1/results.csv', headers=self.H).text)

    def test_worker_refuses_a_database_with_another_schema(self):
        with database.Database(self.inst.settings.db_path).tx() as db:
            db.execute("DELETE FROM schema_migrations WHERE name='010_reuse_and_sharing'")
        with self.assertRaises(Exception) as ctx:
            worker_mod.Worker(database.Database(self.inst.settings.db_path), artifacts.ArtifactStore(self.inst.settings), self.inst.settings)
        self.assertIn('schema_mismatch', str(getattr(ctx.exception, 'detail', ctx.exception)))
        with database.Database(self.inst.settings.db_path).tx() as db:
            db.execute("INSERT INTO schema_migrations VALUES ('010_reuse_and_sharing', ?)", (now(),))
        worker_mod.Worker(database.Database(self.inst.settings.db_path), artifacts.ArtifactStore(self.inst.settings), self.inst.settings)


class PriorDatabaseMigrationTest(unittest.TestCase):
    """Migrate a representative prior-version database (the operator's pre-migration backup) and start the service on it."""
    BACKUPS = Path(os.environ.get('METACOIN_BACKUPS', os.path.expanduser('~/.local/state/metacoin-service-backups')))

    def test_prior_backup_migrates_and_serves(self):
        candidates = sorted(self.BACKUPS.glob('pre-migration-*/service.sqlite')) if self.BACKUPS.exists() else []
        if not candidates:
            self.skipTest('no prior-version backup found under ' + str(self.BACKUPS) + ' (set METACOIN_BACKUPS)')
        src = candidates[0]
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        home = Path(temp.name) / 'home'; home.mkdir(mode=0o700)
        shutil.copy(src, home / 'service.sqlite'); os.chmod(home / 'service.sqlite', 0o600)
        (home / 'keys').mkdir(mode=0o700); (home / 'artifacts').mkdir(mode=0o700)
        before = [r[0] for r in sqlite3.connect(home / 'service.sqlite').execute('SELECT name FROM schema_migrations ORDER BY name')]
        self.assertLess(len(before), len(database.MIGRATIONS))
        jobs_before = sqlite3.connect(home / 'service.sqlite').execute('SELECT COUNT(*) FROM jobs').fetchone()[0]
        applied = database.migrate(home / 'service.sqlite')
        self.assertEqual([a for a in applied], [n for n, _ in database.MIGRATIONS if n not in before])
        after = [r[0] for r in sqlite3.connect(home / 'service.sqlite').execute('SELECT name FROM schema_migrations ORDER BY name')]
        self.assertEqual(after, [n for n, _ in database.MIGRATIONS])
        settings = config.Settings(home=home, provider_mode='simulation'); settings.validate()
        app = api.create_app(settings)                                        # catalog populate + service key on a migrated home
        con = sqlite3.connect(home / 'service.sqlite')
        self.assertEqual(con.execute('SELECT COUNT(*) FROM jobs').fetchone()[0], jobs_before)          # nothing lost or invented
        self.assertEqual(con.execute("SELECT COUNT(*) FROM services WHERE status='registered'").fetchone()[0], 15)
        self.assertEqual(con.execute('SELECT COUNT(*) FROM policy_grants').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
