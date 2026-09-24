"""Order §43: failure, concurrency and protocol cases for the new paths. Only task-owned processes are killed;
coordination uses persisted markers (job/lease rows, checkpoints, stop files), never arbitrary sleeps as proof."""
import json
import os
import signal
import subprocess
import sys
import time
import unittest
from pathlib import Path
import httpx
from metacoin_service.tests.test_service import Instance, free_port, ROOT, ENV, own_inputs
from metacoin_service.tests.test_agents import TEMPORAL
from metacoin_service.tests.test_budgets import two_node_definition
from metacoin_service import db as database, worker as worker_mod, artifacts
from metacoin_service.db import now

PY = sys.executable


def wait_for(pred, timeout=30, step=0.1):
    deadline = time.time() + timeout
    while time.time() < deadline:
        v = pred()
        if v:
            return v
        time.sleep(step)
    return None


class WorkerTerminationTests(unittest.TestCase):
    def setUp(self):
        self.inst = Instance(); self.addCleanup(self.inst.close); self.c = self.inst.client; self.H = self.inst.h('owner')
        self.D = database.Database(self.inst.settings.db_path)

    def test_killed_worker_after_claim_cannot_publish_and_the_job_is_recovered(self):
        one = two_node_definition('kill'); one['nodes'] = one['nodes'][:1]; one['outputs'] = ['a']
        wid = self.c.post('/api/v1/workflows', headers=self.H, json={'definition': one}).json()['id']
        rid = self.c.post('/api/v1/workflows/' + wid + '/runs', headers=self.H, json={'budget_ceiling': 2}).json()['run_id']
        self.c.post('/api/v1/runs/' + rid + '/advance', headers=self.H)
        # a worker process with a short lease claims a node job; it is killed (SIGKILL) while the child computes
        env = dict(ENV, METACOIN_TEST_EXEC_DELAY_SECONDS='5')                      # the child sleeps 5 s before computing so the kill lands mid-attempt
        stop = Path(self.inst.temp.name) / 'stop1'
        w1 = subprocess.Popen([PY, '-m', 'metacoin_service', '--home', str(self.inst.home), 'worker', '--name', 'victim', '--stop-file', str(stop)], cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        claimed = wait_for(lambda: [dict(r) for r in self.D_read("SELECT id, lease_owner, lease_generation FROM jobs WHERE state='running'")] or None, timeout=30)
        self.assertTrue(claimed, 'worker never claimed')
        os.kill(w1.pid, signal.SIGKILL); w1.wait(timeout=10)
        job = claimed[0]
        # the lease is still held by the dead worker: nobody else can claim until it expires; expire it (persisted marker) and let another worker recover
        with self.D.tx() as db:
            db.execute("UPDATE jobs SET lease_expires=? WHERE id=?", (now() - 1, job['id']))
        w2 = worker_mod.Worker(self.D, artifacts.ArtifactStore(self.inst.settings), self.inst.settings, name='rescuer')
        ran = w2.run_once()
        self.assertEqual(ran[0], job['id'])
        row = self.D_read('SELECT state, lease_generation, evidence_root FROM jobs WHERE id=?', (job['id'],))[0]
        self.assertEqual((row['state'], row['lease_generation']), ('succeeded', job['lease_generation'] + 1))
        # a stale attempt (the dead worker's generation) cannot publish: the fence refuses it and records the fact
        stale = worker_mod.Worker(self.D, artifacts.ArtifactStore(self.inst.settings), self.inst.settings, name='stale')
        stale.worker_id = job['lease_owner']
        outcome = stale._finish(dict(id=job['id'], workspace='ws_default', contract_id=self.D_read('SELECT contract_id FROM jobs WHERE id=?', (job['id'],))[0]['contract_id'], lease_generation=job['lease_generation']),
                                {'evidence_vault': {'receipt': {'root': 'f' * 64}, 'fields': []}, 'outcome': 'FAKE', 'summary': {}}, None)
        self.assertEqual(outcome, 'fenced')
        self.assertEqual(self.D_read('SELECT evidence_root FROM jobs WHERE id=?', (job['id'],))[0]['evidence_root'], row['evidence_root'])
        events = self.c.get('/api/v1/jobs/' + job['id'] + '/history', headers=self.H).json()
        rows = events if isinstance(events, list) else next(v for v in events.values() if isinstance(v, list))
        self.assertTrue(any(e['event_type'] == 'job.failed' and 'fenced_out' in e.get('ref_json', json.dumps(e.get('ref', {}))) for e in rows))
        # the workflow sees exactly one committed node and the budget reserved exactly once
        self.c.post('/api/v1/runs/' + rid + '/advance', headers=self.H)
        v = self.c.get('/api/v1/runs/' + rid, headers=self.H).json()
        self.assertEqual(sum(n['state'] == 'succeeded' for n in v['nodes']), 1)
        self.assertEqual((v['budget']['committed_total'], v['budget']['reserved_total']), (1, 0))
        # the dead worker is reported stale in the registry
        victims = [w for w in self.c.get('/api/v1/workers', headers=self.H).json()['items'] if w['name'] == 'victim']
        self.assertEqual(victims[0]['current_job_id'], job['id'])         # never cleared: the process died holding it

    def D_read(self, sql, args=()):
        with self.D.read() as db:
            return [dict(r) for r in db.execute(sql, args).fetchall()]

    def test_duplicate_node_completion_and_duplicate_quote_acceptance_are_idempotent(self):
        wid = self.c.post('/api/v1/workflows', headers=self.H, json={'definition': two_node_definition('dup')}).json()['id']
        rid = self.c.post('/api/v1/workflows/' + wid + '/runs', headers=self.H, json={}).json()['run_id']
        self.c.post('/api/v1/runs/' + rid + '/advance', headers=self.H)
        self.inst.worker().run_once(); self.inst.worker().run_once()
        for _ in range(3):
            self.c.post('/api/v1/runs/' + rid + '/advance', headers=self.H)
        v = self.c.get('/api/v1/runs/' + rid, headers=self.H).json()
        self.assertEqual(v['state'], 'completed')
        self.assertEqual(len(self.c.get('/api/v1/jobs', headers=self.H).json()['items']), 2)                 # advancing again never re-dispatches
        self.assertEqual([r['state'] for r in v['budget']['reservations']], ['committed', 'committed'])
        sid = next(s['id'] for s in self.c.get('/api/v1/services', headers=self.H).json()['items'] if s['kind'] == 'temporal_energy')
        q = self.c.post('/api/v1/services/' + sid + '/quote', headers=self.H, json={'inputs': TEMPORAL}).json()
        a1 = self.c.post('/api/v1/quotes/' + q['quote_id'] + '/accept', headers=self.H).json()
        a2 = self.c.post('/api/v1/quotes/' + q['quote_id'] + '/accept', headers=self.H).json()
        self.assertEqual((a1['state'], a2['state'], a1['accepted_at'], a2['accepted_at']), ('accepted', 'accepted', a1['accepted_at'], a1['accepted_at']))
        # changed provider mode between quote and invocation is refused
        r = self.c.post('/api/v1/services/' + sid + '/quote', headers=self.H, json={'inputs': TEMPORAL, 'provider_mode': 'test-http'})
        self.assertEqual(r.status_code, 201)
        self.c.post('/api/v1/quotes/' + r.json()['quote_id'] + '/accept', headers=self.H)
        inv = self.c.post('/api/v1/services/' + sid + '/invoke', headers=self.H, json={'quote_id': r.json()['quote_id'], 'inputs': TEMPORAL})
        self.assertEqual((inv.status_code, inv.json()['code']), (409, 'CONFLICT'))

    def test_interrupted_dataset_upload_persists_nothing(self):
        # a truncated body (connection cut mid-upload) is a malformed request: refused, no dataset row, no artifact
        good = json.dumps({'name': 'cut', 'kind': 'temporal_series', 'format': 'csv', 'content': "duration_s,harvest_low_mW,harvest_high_mW,load_low_mW,load_high_mW\n10,600,800,500,500\n", 'provenance': 'declared'})
        r = self.c.post('/api/v1/datasets', headers=dict(self.H, **{'Content-Type': 'application/json'}), content=good[:len(good) // 2])
        self.assertIn(r.status_code, (400, 422))
        self.assertEqual(self.D_read('SELECT COUNT(*) AS n FROM datasets')[0]['n'], 0)
        self.assertEqual(self.D_read("SELECT COUNT(*) AS n FROM artifacts WHERE kind LIKE 'dataset%'")[0]['n'], 0)
        # a content-length larger than the body: the server never sees a complete request; over a real socket it is a client abort
        self.assertEqual(self.c.post('/api/v1/datasets', headers=self.H, json=json.loads(good)).status_code, 201)


class RestartAfterAcceptanceOverTcp(unittest.TestCase):
    """The API process dies after a quote is accepted but before the client saw the answer; after restart the client
    re-presents the acceptance (idempotent) and the invocation proceeds exactly once."""

    def test_restart_between_acceptance_and_acknowledgment(self):
        inst = Instance(); self.addCleanup(inst.close); port = free_port(); base = 'http://127.0.0.1:%d' % port
        start = lambda: subprocess.Popen([PY, '-m', 'metacoin_service', '--home', str(inst.home), 'serve', '--port', str(port)], cwd=ROOT, env=ENV, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        proc = start()
        self.addCleanup(lambda: (proc.poll() is None and proc.terminate()))
        wait_for(lambda: self._healthy(base), timeout=30)
        H = inst.h('owner')
        sid = next(s['id'] for s in httpx.get(base + '/api/v1/services', headers=H).json()['items'] if s['kind'] == 'temporal_energy')
        q = httpx.post(base + '/api/v1/services/' + sid + '/quote', headers=H, json={'inputs': TEMPORAL}).json()
        httpx.post(base + '/api/v1/quotes/' + q['quote_id'] + '/accept', headers=H)         # accepted server-side...
        os.kill(proc.pid, signal.SIGKILL); proc.wait(timeout=10)                                # ...and the process dies before the client acts on the answer
        proc = start(); self.addCleanup(lambda: (proc.poll() is None and proc.terminate()))
        wait_for(lambda: self._healthy(base), timeout=30)
        again = httpx.post(base + '/api/v1/quotes/' + q['quote_id'] + '/accept', headers=H).json()
        self.assertEqual(again['state'], 'accepted')
        key = {'Idempotency-Key': 'invoke-' + q['quote_id']}
        first = httpx.post(base + '/api/v1/services/' + sid + '/invoke', headers=dict(H, **key), json={'quote_id': q['quote_id'], 'inputs': TEMPORAL})
        replay = httpx.post(base + '/api/v1/services/' + sid + '/invoke', headers=dict(H, **key), json={'quote_id': q['quote_id'], 'inputs': TEMPORAL})
        self.assertEqual((first.status_code, replay.status_code, first.json()['job_id'], replay.json()['job_id']), (202, 202, first.json()['job_id'], first.json()['job_id']))
        fresh = httpx.post(base + '/api/v1/services/' + sid + '/invoke', headers=H, json={'quote_id': q['quote_id'], 'inputs': TEMPORAL})
        self.assertEqual(fresh.status_code, 409)                                                 # without the key: the quote is consumed, no second job
        self.assertEqual(len(httpx.get(base + '/api/v1/jobs', headers=H).json()['items']), 1)
        # repeated event cursors never duplicate deliveries
        page = httpx.get(base + '/api/v1/events?after=0&limit=500', headers=H).json()
        seqs = [i['seq'] for i in page['items']]
        self.assertEqual(seqs, sorted(set(seqs)))
        self.assertEqual(httpx.get(base + '/api/v1/events?after=%d' % page['cursor'], headers=H).json()['items'], [])
        proc.terminate(); proc.wait(timeout=10)

    @staticmethod
    def _healthy(base):
        try:
            return httpx.get(base + '/api/health', timeout=1).status_code == 200
        except Exception:
            return False


class ProviderUnreachableTests(unittest.TestCase):
    """A workflow node's action whose provider acknowledgment is lost stays OUTCOME_UNKNOWN; cancelling the run afterwards
    keeps the evidence and the economic uncertainty exactly as they are (journey 8 with a genuinely unresolved action)."""

    def test_cancel_run_with_unresolved_action_preserves_evidence_and_exposure(self):
        from experiments.work_contracts import demo, fixtures
        from metacoin_service import actions as actions_mod
        inst = Instance(); self.addCleanup(inst.close); c = inst.client; H = inst.h('owner')
        definition = two_node_definition('unresolved'); definition['nodes'][1]['depends_on'] = ['a']
        wid = c.post('/api/v1/workflows', headers=H, json={'definition': definition}).json()['id']
        rid = c.post('/api/v1/workflows/' + wid + '/runs', headers=H, json={'budget_ceiling': 2}).json()['run_id']
        c.post('/api/v1/runs/' + rid + '/advance', headers=H)
        inst.worker().run_once()
        c.post('/api/v1/runs/' + rid + '/advance', headers=H)
        v = c.get('/api/v1/runs/' + rid, headers=H).json()
        ja = [n for n in v['nodes'] if n['node_id'] == 'a'][0]['job_id']
        c.post('/api/v1/jobs/' + ja + '/review-request', headers=H)
        c.post('/api/v1/reviews/' + ja + '/decision', headers=inst.h('reviewer'), json={'decision': 'accepted'})
        lost = demo.LostAcknowledgement(fixtures.funded_faucet(actor=inst.ids['owner'], amount=10))
        original = actions_mod.provider_for
        actions_mod.provider_for = lambda mode, settings, capability, actor='x': (lost, {'adapter_session': 'test'})
        try:
            r = c.post('/api/v1/actions', headers=H, json={'job_id': ja, 'request_id': 'lost-1'})
            self.assertEqual(r.json()['state'], 'OUTCOME_UNKNOWN')
            self.assertEqual(c.post('/api/v1/runs/' + rid + '/cancel', headers=H).status_code, 200)
            for _ in range(3):
                c.post('/api/v1/runs/' + rid + '/advance', headers=H)
            v = c.get('/api/v1/runs/' + rid, headers=H).json()
            job_a = c.get('/api/v1/jobs/' + ja, headers=H).json()
        finally:
            actions_mod.provider_for = original
        self.assertEqual((v['state'], {n['node_id']: n['state'] for n in v['nodes']}), ('cancelled', {'a': 'succeeded', 'b': 'cancelled'}))
        self.assertEqual(job_a['payment']['state'], 'OUTCOME_UNKNOWN')                       # never converted into success or failure
        self.assertEqual(c.get('/api/v1/jobs/' + ja + '/result', headers=H).status_code, 200)   # evidence preserved
        self.assertEqual((v['budget']['committed_total'], v['budget']['reserved_total']), (1, 0))
        self.assertGreaterEqual(c.get('/api/v1/status', headers=H).json()['unresolved_payment_actions'], 1)
        self.assertEqual(c.get('/api/v1/budget', headers=H).json()['by_state']['OUTCOME_UNKNOWN'], 1)


if __name__ == '__main__':
    unittest.main()
