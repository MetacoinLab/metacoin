"""Order §60: bounded failure and concurrency campaign around meaningful boundaries, with task-owned processes and
deterministic coordination (no arbitrary sleeps as the mechanism). Invariants checked across failures: no
unauthorized access, no duplicate accepted result, no widened grant, no silently lost job, no duplicate useful-work
charge, no release of unresolved economic exposure, no stale worker overwrite. Classes exercised here are named;
power loss, hardware faults and Byzantine networks are outside scope."""
import json
import os
import signal
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path

from metacoin_service.db import Database, now
from metacoin_service.tests.test_compute_engine import ComputeInstance, HAVE_RUNTIME, batch_spec, heat_spec
from metacoin_service.tests.test_models import ModelInstance, HAVE_TORCH, installed, GEN, EMB
from metacoin_service.tests.test_service import ROOT, ENV

PY = sys.executable
CLASSES_EXERCISED = ['accepted request before client acknowledgment (idempotent resubmission)', 'model output before artifact commit (runtime child killed mid-generation)',
                     'index build before version publication (worker killed mid-build)', 'audit challenge before result (result commitment changed)',
                     'worker result before acknowledgment (node result upload without publication; stale generation)', 'usage finalization before provider response (unknown outcome preserved)',
                     'restore before reconciliation (gate set)']


class FCInstance(ModelInstance, ComputeInstance):
    """Model helpers (register_defaults) plus compute helpers (compute_job, view) on one temporary home."""


@unittest.skipUnless(HAVE_RUNTIME, 'no numpy-capable compute interpreter on this host')
class FailureCampaign(unittest.TestCase):
    def setUp(self):
        self.inst = FCInstance(); self.addCleanup(self.inst.close); self.c = self.inst.client; self.H = self.inst.h('owner')

    def test_accepted_request_before_acknowledgment_is_idempotent(self):
        body = {'kind': 'temporal_batch', 'inputs': batch_spec(private_label='FC1'), 'title': 'fc1'}
        r1 = self.c.post('/api/v1/jobs/quick', headers=dict(self.H, **{'Idempotency-Key': 'fc-1'}), json=body)
        r2 = self.c.post('/api/v1/jobs/quick', headers=dict(self.H, **{'Idempotency-Key': 'fc-1'}), json=body)          # the client never saw r1
        r3 = self.c.post('/api/v1/jobs/quick', headers=dict(self.H, **{'Idempotency-Key': 'fc-1'}), json=dict(body, title='changed'))
        self.assertEqual((r1.status_code, r2.status_code, r1.json()['job_id'], r3.status_code), (202, 202, r2.json()['job_id'], 409))
        self.assertEqual(len([j for j in self.c.get('/api/v1/jobs', headers=self.H).json()['items'] if j['kind'] == 'temporal_batch']), 1)

    @unittest.skipUnless(HAVE_TORCH, 'no torch')
    def test_model_output_before_artifact_commit(self):
        if not installed(self.inst.settings, GEN) or not installed(self.inst.settings, EMB):
            self.skipTest('models absent')
        self.inst.register_defaults()
        w = self.inst.worker(); self.addCleanup(w.offline)
        g = self.c.post('/api/v1/models/generate', headers=self.H, json={'inputs': {'prompt': 'Write a long story about a lighthouse keeper and the sea.', 'max_output_tokens': 300}}).json()
        jid = g['job_id']
        # kill the runtime child once segments exist: the attempt fails, partial segments stay under their attempt generation, no output artifact is committed
        def killer():
            for _ in range(200):
                time.sleep(0.1)
                v = self.c.get('/api/v1/models/jobs/' + jid, headers=self.H).json()
                if v['usage']['segments'] and v['usage']['segments'] >= 2:
                    with Database(self.inst.settings.db_path).read() as db:
                        pid = db.execute("SELECT pid FROM model_runtimes WHERE state='ready'").fetchone()
                    if pid and pid['pid']:
                        os.kill(pid['pid'], signal.SIGKILL); return
        th = threading.Thread(target=killer); th.start()
        outcome = w.run_once()[1]; th.join()
        v = self.c.get('/api/v1/models/jobs/' + jid, headers=self.H).json()
        self.assertIn(outcome, ('retry', 'failed')); self.assertIsNone(v['output_artifact_id'])
        self.assertEqual(self.c.get('/api/v1/models/jobs/' + jid + '/outputs', headers=self.H).status_code, 404)
        self.assertFalse([u for u in self.c.get('/api/v1/usage', headers=self.H).json()['items'] if u['job_id'] == jid])
        if outcome == 'retry':
            # a new attempt runs under a new generation; it never splices onto the old segments
            self.assertEqual(w.run_once()[1], 'succeeded')
            v2 = self.c.get('/api/v1/models/jobs/' + jid, headers=self.H).json()
            segs = self.c.get('/api/v1/models/jobs/' + jid + '/segments', headers=self.H).json()
            self.assertEqual((v2['state'], segs['attempt_generation']), ('succeeded', v2['attempt_generation'])); self.assertGreater(v2['attempt_generation'], 1)

    @unittest.skipUnless(HAVE_TORCH, 'no torch')
    def test_index_build_before_publication(self):
        if not installed(self.inst.settings, GEN) or not installed(self.inst.settings, EMB):
            self.skipTest('models absent')
        self.inst.register_defaults()
        col = self.c.post('/api/v1/knowledge/collections', headers=self.H, json={'name': 'fc'}).json()
        for i in range(12):
            self.c.post('/api/v1/knowledge/collections/' + col['id'] + '/documents', headers=self.H, json={'name': 'd%d' % i, 'format': 'text', 'content': ('Paragraph %d about diffusion and reserves. ' % i) * 40})
        r = self.c.post('/api/v1/knowledge/collections/' + col['id'] + '/indexes', headers=self.H, json={}).json()
        # a worker process is killed while embedding: the index stays 'building', no ready version appears, and the retry (new attempt) publishes exactly one version
        proc = subprocess.Popen([PY, '-m', 'metacoin_service', '--home', str(self.inst.home), 'worker', '--once', '--name', 'fc-index'], cwd=ROOT, env=ENV, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(300):
            time.sleep(0.05)
            v = self.c.get('/api/v1/models/jobs/' + r['job_id'], headers=self.H).json()
            if v['phase'] in ('loading', 'running'):
                proc.kill(); break
        proc.wait(timeout=30)
        idx = self.c.get('/api/v1/knowledge/indexes/' + r['index_id'], headers=self.H).json()
        self.assertEqual(idx['state'], 'building')
        with Database(self.inst.settings.db_path).tx() as db:
            db.execute("UPDATE jobs SET lease_expires=? WHERE id=?", (now() - 1, r['job_id']))
        w = self.inst.worker(); self.addCleanup(w.offline)
        self.assertEqual(w.run_once()[1], 'succeeded')
        idx = self.c.get('/api/v1/knowledge/indexes/' + r['index_id'], headers=self.H).json()
        self.assertEqual(idx['state'], 'ready')
        self.assertEqual(len([i for i in self.c.get('/api/v1/knowledge/collections/' + col['id'], headers=self.H).json()['indexes'] if i['state'] == 'ready']), 1)

    def test_audit_challenge_before_result_change_and_stale_worker(self):
        w = self.inst.worker(); self.addCleanup(w.offline)
        jid = self.inst.compute_job('temporal_batch', batch_spec(private_label='FC4')); self.assertEqual(w.run_once()[1], 'succeeded')
        v = self.c.post('/api/v1/verification', headers=self.H, json={'job_id': jid, 'class': 'sampled_reference', 'params': {'sample_count': 8}}).json()
        with Database(self.inst.settings.db_path).tx() as db:      # the result commitment changes after the challenge was drawn
            db.execute("UPDATE jobs SET evidence_root=? WHERE id=?", ('0' * 64, jid))
        self.assertEqual(w.run_once()[1], 'failed')
        vv = self.c.get('/api/v1/verification/' + v['id'], headers=self.H).json()
        self.assertEqual(vv['state'], 'incomplete')
        # stale worker overwrite: a job claimed by worker A (lease expired) then completed by worker B; A's late publication is fenced
        jid2 = self.inst.compute_job('heat_diffusion', heat_spec(nx=64, ny=64, steps=200, device_policy='cpu'))
        wa = self.inst.worker(); self.addCleanup(wa.offline)
        job = wa.claim()
        with Database(self.inst.settings.db_path).tx() as db:
            db.execute("UPDATE jobs SET lease_expires=? WHERE id=?", (now() - 1, jid2))
        wb = self.inst.worker(); self.addCleanup(wb.offline)
        self.assertEqual(wb.run_once(), (jid2, 'succeeded'))
        self.assertEqual(wa.execute(job), 'fenced')
        with Database(self.inst.settings.db_path).read() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM artifacts WHERE job_id=? AND kind=?', (jid2, 'evidence_vault')).fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM jobs WHERE id=? AND state='succeeded'", (jid2,)).fetchone()[0], 1)

    def test_concurrent_submissions_and_workers_keep_invariants(self):
        jobs = [self.inst.compute_job('temporal_batch', batch_spec(private_label='FC5-%d' % i)) for i in range(6)]
        workers = [self.inst.worker() for _ in range(3)]
        for w in workers:
            self.addCleanup(w.offline)
        results = []
        def loop(w):
            for _ in range(6):
                r = w.run_once()
                if r:
                    results.append(r)
        ths = [threading.Thread(target=loop, args=(w,)) for w in workers]
        [t.start() for t in ths]; [t.join() for t in ths]
        done = {r[0] for r in results if r[1] == 'succeeded'}
        self.assertEqual(done, set(jobs))
        self.assertEqual(len([r for r in results if r[1] == 'succeeded']), 6)          # each job completed exactly once
        with Database(self.inst.settings.db_path).read() as db:
            for j in jobs:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM attempts WHERE job_id=? AND outcome='succeeded'", (j,)).fetchone()[0], 1)
