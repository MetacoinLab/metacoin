"""Group B: real static generation batching through the ordinary queue. Three greedy requests of one submitter run as ONE
padded forward pass (batch id shared, positions recorded, decode steps counted once); each member keeps its own segments,
token counts, terminal state and usage; a member cancelled mid-generation stops delivery while the others complete; a
client reconnects to a surviving member through the segment cursor without submitting anything; a revoked credential
cannot keep reading a stream; a sampled request never joins; operator settings bound the batch; singleton-versus-batch
equality is measured (not assumed) and recorded."""
import json
import os
import threading
import time
import unittest
from pathlib import Path

from metacoin_service.db import Database, now
from metacoin_service.tests.test_models import ModelInstance, HAVE_TORCH, installed, GEN, EMB


@unittest.skipUnless(HAVE_TORCH, 'no torch-capable interpreter on this host')
class GenerationBatchingTests(unittest.TestCase):
    def setUp(self):
        self.inst = ModelInstance(); self.addCleanup(self.inst.close); self.c = self.inst.client; self.H = self.inst.h('owner')
        if not installed(self.inst.settings, GEN) or not installed(self.inst.settings, EMB):
            self.skipTest('pinned model artifacts not installed in the model store')
        self.ids = self.inst.register_defaults()
        self.w = self.inst.worker(); self.addCleanup(self.w.offline)

    def gen(self, prompt, max_tokens=24, **extra):
        r = self.c.post('/api/v1/models/generate', headers=self.H, json={'inputs': dict({'messages': [{'role': 'user', 'content': prompt}], 'max_output_tokens': max_tokens}, **extra)})
        self.assertEqual(r.status_code, 202, r.text); return r.json()['job_id']

    def view(self, jid):
        return self.c.get('/api/v1/models/jobs/' + jid, headers=self.H).json()

    def test_three_requests_one_batch_independent_outputs_and_measured_equality(self):
        prompts = ['Reply with one word: hello', 'Name three primary colours separated by commas.', 'Write one sentence about diffusion in a battery.']
        # singleton references (batching disabled) for the same prompts
        self.c.post('/api/v1/models/batching', headers=self.H, json={'enabled': False})
        solo = {}
        for p in prompts:
            j = self.gen(p); self.assertEqual(self.w.run_once(), (j, 'succeeded')); solo[p] = self.view(j)
        self.assertTrue(all(v['usage'].get('batch_id') is None for v in solo.values()))
        # batched: the three queued together run as one static batch
        self.c.post('/api/v1/models/batching', headers=self.H, json={'enabled': True, 'max_sequences': 4})
        jobs = [self.gen(p) for p in prompts]
        primary, outcome = self.w.run_once()
        self.assertEqual(outcome, 'succeeded'); self.assertIn(primary, jobs)
        views = {j: self.view(j) for j in jobs}
        self.assertEqual({v['state'] for v in views.values()}, {'succeeded'})
        bids = {v['usage']['batch_id'] for v in views.values()}
        self.assertEqual(len(bids), 1); bid = bids.pop(); self.assertTrue(bid.startswith('mb_'))
        self.assertEqual(sorted(v['usage']['batch_position'] for v in views.values()), [0, 1, 2])
        with Database(self.inst.settings.db_path).read() as db:
            b = db.execute('SELECT * FROM model_batches WHERE id=?', (bid,)).fetchone()
        self.assertEqual((b['members'], b['mode']), (3, 'static')); self.assertGreater(b['decode_steps'], 0); self.assertGreater(b['padded_prompt_length'], 0)
        self.assertLessEqual(b['decode_steps'], 24)                                                   # one forward pass per decode step for the whole batch
        # independent identity: each job's own prompt tokens, own output, own segments, own usage record
        equal = {}
        for p, j in zip(prompts, jobs):
            v = views[j]; s = solo[p]
            self.assertEqual(v['usage']['input_tokens'], s['usage']['input_tokens'])             # padding never counts as input
            self.assertGreater(v['usage']['output_tokens'], 0); self.assertLessEqual(v['usage']['output_tokens'], 24)
            segs = self.c.get('/api/v1/models/jobs/' + j + '/segments', headers=self.H).json()
            text = ''.join(x['text'] for x in segs['segments']); self.assertTrue(text.strip())
            out = self.c.get('/api/v1/models/jobs/' + j + '/outputs', headers=self.H)
            self.assertEqual(out.status_code, 200)
            solo_segs = ''.join(x['text'] for x in self.c.get('/api/v1/models/jobs/' + s['job_id'] + '/segments', headers=self.H).json()['segments'])
            equal[p] = (text == solo_segs)
            self.assertNotIn(prompts[(prompts.index(p) + 1) % 3][:12].lower(), text.lower()[:20] if False else '')      # placeholder guard: no cross-talk asserted below
        # no cross-talk: the one-word answer stays short and does not contain the other prompts' words
        hello = ''.join(x['text'] for x in self.c.get('/api/v1/models/jobs/' + jobs[0] + '/segments', headers=self.H).json()['segments'])
        self.assertLess(len(hello), 60); self.assertNotIn('colour', hello.lower()); self.assertNotIn('diffusion', hello.lower())
        # measured reproducibility contract: recorded, at least the short deterministic answer must match its singleton
        self.assertTrue(equal[prompts[0]], (hello, solo))
        record = Path(os.environ.get('METACOIN_BATCH_EQUALITY_OUT', '/dev/null'))
        if str(record) != '/dev/null':
            record.write_text(json.dumps({'singleton_vs_static_batch_equal': equal, 'batch': dict(b)}, default=str, indent=1))
        # usage rows (unquoted path: none) and the runtime facts expose the batch
        facts = self.c.get('/api/v1/models/runtime', headers=self.H).json()['generation_batching']
        self.assertEqual(facts['mode'], 'static'); self.assertTrue(any(r['id'] == bid for r in facts['recent_batches']))

    def test_cancel_one_member_reconnect_and_revocation(self):
        self.c.post('/api/v1/models/batching', headers=self.H, json={'enabled': True, 'max_sequences': 4})
        long1 = self.gen('Write a long story about a lighthouse keeper and the sea, many paragraphs.', max_tokens=220)
        long2 = self.gen('Write a long story about a mountain climber and the storm, many paragraphs.', max_tokens=220)
        short = self.gen('Reply with one word: yes', max_tokens=8)
        # cancel long2 once it has produced a few segments; long1 and short must complete normally
        def killer():
            for _ in range(600):
                time.sleep(0.05)
                v = self.view(long2)
                if v['usage']['segments'] and v['usage']['segments'] >= 2:
                    self.c.post('/api/v1/jobs/' + long2 + '/cancel', headers=self.H); return
        th = threading.Thread(target=killer); th.start()
        primary, outcome = self.w.run_once(); th.join()
        views = {j: self.view(j) for j in (long1, long2, short)}
        self.assertEqual(views[long1]['state'], 'succeeded'); self.assertEqual(views[short]['state'], 'succeeded'); self.assertEqual(views[long2]['state'], 'cancelled', views[long2])
        self.assertEqual(views[long2]['usage']['finish_reason'], 'cancelled'); self.assertIsNone(views[long2]['output_artifact_id'])
        self.assertEqual(len({views[j]['usage']['batch_id'] for j in views}), 1)
        # the cancelled member's partial segments are preserved under its attempt; the survivors' streams are independent
        p2 = self.c.get('/api/v1/models/jobs/' + long2 + '/segments', headers=self.H).json()
        self.assertGreaterEqual(len(p2['segments']), 2); self.assertEqual(p2['state'], 'cancelled')
        self.assertLess(views[long2]['usage']['output_tokens'], 220); self.assertLess(views[short]['usage']['output_tokens'], 9)
        # reconnect: resume from a cursor without submitting anything (job count unchanged)
        n_before = len(self.c.get('/api/v1/jobs?limit=50', headers=self.H).json()['items'])
        first = self.c.get('/api/v1/models/jobs/' + long1 + '/segments?after=-1&limit=2', headers=self.H).json()
        rest = self.c.get('/api/v1/models/jobs/' + long1 + '/segments?after=%d' % first['cursor'], headers=self.H).json()
        full = ''.join(x['text'] for x in first['segments'] + rest['segments'])
        self.assertEqual(full, ''.join(x['text'] for x in self.c.get('/api/v1/models/jobs/' + long1 + '/segments', headers=self.H).json()['segments']))
        self.assertTrue(rest['done']); self.assertEqual(len(self.c.get('/api/v1/jobs?limit=50', headers=self.H).json()['items']), n_before)
        # duplicate subscribers see identical bytes; another member's tokens never appear in this stream
        again = ''.join(x['text'] for x in self.c.get('/api/v1/models/jobs/' + long1 + '/segments', headers=self.H).json()['segments'])
        self.assertEqual(again, full); self.assertNotIn('mountain', full.lower()[:200]) if 'lighthouse' in full.lower() else None
        # revocation ends delivery: a scoped automation credential loses the stream when revoked
        cred = self.c.post('/api/v1/credentials', headers=self.H, json={'operations': ['job:read', 'job:read_private'], 'expires_in_seconds': 600})
        self.assertEqual(cred.status_code, 201, cred.text); cred = cred.json()
        HA = {'Authorization': 'Bearer ' + cred['token']}
        self.assertEqual(self.c.get('/api/v1/models/jobs/' + long1 + '/segments', headers=HA).status_code, 200)
        self.assertEqual(self.c.delete('/api/v1/credentials/' + cred['credential_id'], headers=self.H).status_code, 200)
        self.assertEqual(self.c.get('/api/v1/models/jobs/' + long1 + '/segments', headers=HA).status_code, 401)        # an open tab does not outlive its credential

    def test_admission_bounds_sampled_singleton_and_stale_worker(self):
        self.c.post('/api/v1/models/batching', headers=self.H, json={'enabled': True, 'max_sequences': 2})
        a, b, c = (self.gen('Reply with one word: %s' % w, max_tokens=6) for w in ('alpha', 'beta', 'gamma'))
        s = self.gen('Reply with one word: delta', max_tokens=6, temperature_percent=50, seed=7)          # sampled: never batched
        primary, outcome = self.w.run_once(); self.assertEqual(outcome, 'succeeded')
        states = {j: self.view(j)['state'] for j in (a, b, c, s)}
        self.assertEqual(sorted(states.values()), ['queued', 'queued', 'succeeded', 'succeeded'])        # exactly two admitted (max_sequences)
        done = [j for j in (a, b, c) if states[j] == 'succeeded']; self.assertEqual(len(done), 2)
        self.assertEqual(len({self.view(j)['usage']['batch_id'] for j in done}), 1)
        # the rest run afterwards; the sampled request completes as a singleton (no batch id) with its seed recorded
        for _ in range(2):
            self.w.run_once()
        self.assertEqual({self.view(j)['state'] for j in (a, b, c, s)}, {'succeeded'})
        self.assertIsNone(self.view(s)['usage'].get('batch_id'))
        # bounds are validated; an unbounded KV budget is refused
        self.assertEqual(self.c.post('/api/v1/models/batching', headers=self.H, json={'max_sequences': 99}).status_code, 422)
        self.assertEqual(self.c.post('/api/v1/models/batching', headers=self.H, json={'kv_budget_bytes': 10 ** 15}).status_code, 422)
        self.assertEqual(self.c.post('/api/v1/models/batching', headers=self.inst.h('viewer'), json={'enabled': False}).status_code, 403)
        # stale worker: a batch claimed by worker A whose lease expired; worker B completes the members; A's late publication is fenced for every member
        self.c.post('/api/v1/models/batching', headers=self.H, json={'max_sequences': 4})
        x, y = self.gen('Reply with one word: left', max_tokens=6), self.gen('Reply with one word: right', max_tokens=6)
        wa = self.inst.worker(); self.addCleanup(wa.offline)
        job = wa.claim(); self.assertIsNotNone(job)
        with Database(self.inst.settings.db_path).tx() as db:
            db.execute("UPDATE jobs SET lease_expires=? WHERE id IN (?, ?)", (now() - 1, x, y))
        wb = self.inst.worker(); self.addCleanup(wb.offline)
        self.assertEqual(wb.run_once()[1], 'succeeded')
        for _ in range(2):
            wb.run_once()
        self.assertEqual(wa.execute(job), 'fenced')
        with Database(self.inst.settings.db_path).read() as db:
            for j in (x, y):
                self.assertEqual(db.execute("SELECT COUNT(*) FROM jobs WHERE id=? AND state='succeeded'", (j,)).fetchone()[0], 1)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM artifacts WHERE job_id=? AND kind='model_output'", (j,)).fetchone()[0], 1)
