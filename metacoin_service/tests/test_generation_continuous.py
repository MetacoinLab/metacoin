"""§66-2 continuous generation admission: a real continuous-batching session (transformers ContinuousBatchingManager on the
pinned model) admits compatible queued requests while it runs and removes members as they finish or are cancelled; each
member keeps its own segments, usage, terminal state and accounting; static padded batching stays the explicit fallback."""
import json
import os
import threading
import time
import unittest
from pathlib import Path

from metacoin_service.db import Database
from metacoin_service.tests.test_models import ModelInstance, HAVE_TORCH, installed, GEN, EMB


@unittest.skipUnless(HAVE_TORCH, 'no torch')
class ContinuousBatchingTests(unittest.TestCase):
    def setUp(self):
        self.inst = ModelInstance(); self.addCleanup(self.inst.close); self.c = self.inst.client; self.H = self.inst.h('owner')
        if not installed(self.inst.settings, GEN) or not installed(self.inst.settings, EMB):
            self.skipTest('pinned model artifacts not installed in the model store')
        self.inst.register_defaults()
        self.w = self.inst.worker(); self.addCleanup(self.w.offline)

    def gen(self, prompt, max_tokens=24, **extra):
        r = self.c.post('/api/v1/models/generate', headers=self.H, json={'inputs': dict({'messages': [{'role': 'user', 'content': prompt}], 'max_output_tokens': max_tokens}, **extra)})
        self.assertEqual(r.status_code, 202, r.text); return r.json()['job_id']

    def view(self, jid):
        return self.c.get('/api/v1/models/jobs/' + jid, headers=self.H).json()

    def test_admission_during_session_cancellation_accounting_and_static_fallback(self):
        facts = self.c.get('/api/v1/models/runtime', headers=self.H).json()
        r = self.c.post('/api/v1/models/batching', headers=self.H, json={'enabled': True, 'max_sequences': 4, 'modes': ['static', 'continuous'], 'wait_ms': 1500})
        self.assertEqual(r.status_code, 200, r.text)
        # singleton reference for the short prompt (batching off)
        self.c.post('/api/v1/models/batching', headers=self.H, json={'enabled': False})
        solo = self.gen('Reply with one word: hello', 8); self.assertEqual(self.w.run_once(), (solo, 'succeeded'))
        solo_text = ''.join(x['text'] for x in self.c.get('/api/v1/models/jobs/' + solo + '/segments', headers=self.H).json()['segments'])
        self.c.post('/api/v1/models/batching', headers=self.H, json={'enabled': True, 'modes': ['static', 'continuous']})
        long1 = self.gen('Write a long story about a lighthouse keeper and the sea, many paragraphs.', 200)
        admitted = {}
        def late_submitter():
            for _ in range(600):
                time.sleep(0.05)
                v = self.view(long1)
                if (v['usage'] or {}).get('segments', 0) >= 2:
                    break
            admitted['long2'] = self.gen('Write a long story about a mountain climber and the storm, many paragraphs.', 200)
            admitted['short'] = self.gen('Reply with one word: hello', 8)
            for _ in range(600):
                time.sleep(0.05)
                v = self.view(admitted['long2'])
                if (v['usage'] or {}).get('segments', 0) >= 2:
                    self.c.post('/api/v1/jobs/' + admitted['long2'] + '/cancel', headers=self.H); return
        th = threading.Thread(target=late_submitter); th.start()
        primary, outcome = self.w.run_once(); th.join()
        self.assertEqual((primary, outcome), (long1, 'succeeded'))
        long2, short = admitted['long2'], admitted['short']
        views = {j: self.view(j) for j in (long1, long2, short)}
        self.assertEqual((views[long1]['state'], views[short]['state'], views[long2]['state']), ('succeeded', 'succeeded', 'cancelled'), {j: v['state'] for j, v in views.items()})
        bids = {v['usage']['batch_id'] for v in views.values()}
        self.assertEqual(len(bids), 1); bid = bids.pop()
        self.assertEqual(sorted(v['usage']['batch_position'] for v in views.values()), [0, 1, 2])            # admission order
        with Database(self.inst.settings.db_path).read() as db:
            b = dict(db.execute('SELECT * FROM model_batches WHERE id=?', (bid,)).fetchone())
        outcome_json = json.loads(b['outcome_json'])
        self.assertEqual((b['mode'], b['members'], b['cancelled_members'], outcome_json['admitted_during_session']), ('continuous', 3, 1, 2))
        adm = outcome_json['admissions']; self.assertEqual([a['position'] for a in adm], [0, 1, 2]); self.assertLessEqual(adm[0]['at'], adm[1]['at'])
        # independent accounting and outputs
        for j in (long1, long2, short):
            self.assertGreater(views[j]['usage']['output_tokens'], 0)
        self.assertEqual(views[long2]['usage']['finish_reason'], 'cancelled'); self.assertIsNone(views[long2]['output_artifact_id'])
        self.assertGreaterEqual(len(self.c.get('/api/v1/models/jobs/' + long2 + '/segments', headers=self.H).json()['segments']), 2)     # partial output preserved
        self.assertLess(views[short]['usage']['output_tokens'], 9)
        short_text = ''.join(x['text'] for x in self.c.get('/api/v1/models/jobs/' + short + '/segments', headers=self.H).json()['segments'])
        self.assertTrue(short_text.strip()); self.assertNotIn('lighthouse', short_text.lower())
        with Database(self.inst.settings.db_path).read() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM usage_records WHERE job_id IN (?,?,?)', (long1, long2, short)).fetchone()[0], db.execute("SELECT COUNT(*) FROM jobs WHERE id IN (?,?,?) AND state='succeeded' AND quote_id IS NOT NULL", (long1, long2, short)).fetchone()[0])
        record = Path(os.environ.get('METACOIN_CONTINUOUS_EQUALITY_OUT', '/dev/null'))
        equal = short_text == solo_text
        if str(record) != '/dev/null':
            record.write_text(json.dumps({'singleton_vs_continuous_equal_short_prompt': equal, 'singleton': solo_text, 'continuous': short_text, 'batch': b, 'admissions': adm}, default=str, indent=1))
        facts = self.c.get('/api/v1/models/runtime', headers=self.H).json()['generation_batching']
        self.assertIn('enabled: continuous admission', facts['continuous']); self.assertTrue(any(r['id'] == bid for r in facts['recent_batches']))
        # explicit static fallback: with modes ['static'] the same cohort runs as one padded batch
        self.c.post('/api/v1/models/batching', headers=self.H, json={'modes': ['static']})
        a, bb = self.gen('Reply with one word: alpha', 6), self.gen('Reply with one word: beta', 6)
        self.assertEqual(self.w.run_once()[1], 'succeeded')
        with Database(self.inst.settings.db_path).read() as db:
            modes = [r[0] for r in db.execute('SELECT mode FROM model_batches ORDER BY started_at')]
        self.assertEqual(modes[-1], 'static'); self.assertEqual(self.view(a)['usage']['batch_id'], self.view(bb)['usage']['batch_id'])
        # a sampled request never joins a session
        self.c.post('/api/v1/models/batching', headers=self.H, json={'modes': ['static', 'continuous']})
        s = self.gen('Reply with one word: delta', 6, temperature_percent=50, seed=3); g = self.gen('Reply with one word: gamma', 6)
        self.w.run_once(); self.w.run_once()
        self.assertIsNone(self.view(s)['usage'].get('batch_id')); self.assertEqual(self.view(s)['state'], 'succeeded')


if __name__ == '__main__':
    unittest.main()
