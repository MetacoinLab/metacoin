"""§65-4 inference batching (embeddings of one submitter share a forward pass; per-request cancellation, accounting,
output identity and isolation preserved) and §65-5 warmup policies (explicit residency under a ceiling, drained
when numerical compute needs memory, actual state reported)."""
import math
import unittest

from metacoin_service.db import Database
from metacoin_service.tests.test_models import ModelInstance, HAVE_TORCH, installed, GEN, EMB


def cosine(a, b):
    return sum(x * y for x, y in zip(a, b)) / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


@unittest.skipUnless(HAVE_TORCH, 'no torch-capable interpreter on this host')
class BatchingWarmupTests(unittest.TestCase):
    def setUp(self):
        self.inst = ModelInstance(); self.addCleanup(self.inst.close); self.c = self.inst.client; self.H = self.inst.h('owner')
        if not installed(self.inst.settings, GEN) or not installed(self.inst.settings, EMB):
            self.skipTest('pinned model artifacts not installed in the model store')
        self.ids = self.inst.register_defaults()
        self.w = self.inst.worker(); self.addCleanup(self.w.offline)

    def embed_job(self, texts, quoted=False, **extra):
        if not quoted:
            r = self.c.post('/api/v1/models/embed', headers=self.H, json={'inputs': dict({'texts': texts}, **extra)})
            self.assertEqual(r.status_code, 202, r.text); return r.json()['job_id']
        # purchased path: a quote per request, so usage accounting under batching can be checked per job
        sid = next(s['id'] for s in self.c.get('/api/v1/services', headers=self.H).json()['items'] if s['kind'] == 'text_embedding')
        inputs = dict({'schema': 'text-embedding-input/v1', 'texts': texts}, **extra)
        q = self.c.post('/api/v1/services/' + sid + '/quote', headers=self.H, json={'inputs': inputs}); self.assertEqual(q.status_code, 201, q.text)
        self.assertEqual(self.c.post('/api/v1/quotes/' + q.json()['quote_id'] + '/accept', headers=self.H).status_code, 200)
        r = self.c.post('/api/v1/services/' + sid + '/invoke', headers=self.H, json={'quote_id': q.json()['quote_id'], 'inputs': inputs})
        self.assertEqual(r.status_code, 202, r.text); return r.json()['job_id']

    def vectors_of(self, jid):
        from metacoin_service.compute import container, npy
        from metacoin_service.artifacts import ArtifactStore
        with Database(self.inst.settings.db_path).read() as db:
            aid = db.execute('SELECT output_artifact_id FROM model_requests WHERE job_id=?', (jid,)).fetchone()[0]
            files = container.unpack(ArtifactStore(self.inst.settings).load(db, aid, 'ws_default'))
        import json
        meta = json.loads(files['output.json']); arr = npy.decode(files['vectors.npy'])
        vals = arr['values'] if isinstance(arr, dict) else arr[0]
        dim = meta['dim']
        return meta, [vals[i * dim:(i + 1) * dim] for i in range(meta['items'])]

    def test_batching_preserves_cancellation_accounting_identity_and_isolation(self):
        solo = self.embed_job(['The reserve is 2000 mJ.']); self.assertEqual(self.w.run_once()[1], 'succeeded')
        meta_solo, vec_solo = self.vectors_of(solo)
        self.assertIsNone(meta_solo['batch'])
        j1 = self.embed_job(['alpha particle', 'beta decay', 'gamma ray'], quoted=True)
        j2 = self.embed_job(['The reserve is 2000 mJ.', 'diffusion'], quoted=True)
        j3 = self.embed_job(['to be cancelled'])
        self.inst.settings.limits['model_batch_max_chars'] = 20000
        self.assertEqual(self.c.post('/api/v1/jobs/' + j3 + '/cancel', headers=self.H).status_code, 200)
        primary, outcome = self.w.run_once()
        self.assertIn(primary, (j1, j2)); self.assertEqual(outcome, 'succeeded')
        views = {j: self.c.get('/api/v1/models/jobs/' + j, headers=self.H).json() for j in (j1, j2, j3)}
        self.assertEqual((views[j1]['state'], views[j2]['state'], views[j3]['state']), ('succeeded', 'succeeded', 'cancelled'))
        m1, v1 = self.vectors_of(j1); m2, v2 = self.vectors_of(j2)
        self.assertEqual((m1['batch']['members'], m2['batch']['members'], {m1['batch']['position'], m2['batch']['position']}, m1['batch']['id'] == m2['batch']['id']), (2, 2, {0, 1}, True))
        j4 = self.embed_job(['a' * 4000] * 6)                                           # too many chars for the batch limit: would not have joined
        self.assertEqual((m1['items'], m2['items'], len(v1), len(v2)), (3, 2, 3, 2))
        self.assertEqual(views[j1]['usage']['items'], 3); self.assertEqual(views[j2]['usage']['items'], 2)
        self.assertEqual(views[j1]['usage']['input_tokens'], sum(m1['tokens'])); self.assertGreater(views[j1]['usage']['input_tokens'], 0)
        # output identity: the batched vector for the same text matches the solo vector to float precision of a padded batch
        self.assertGreater(cosine(v2[0], vec_solo[0]), 0.9999)
        self.assertLess(cosine(v2[1], vec_solo[0]), 0.9)                                   # and the other member's text is a different vector (no mixing)
        # usage rows (purchased path) are separate, bound per job, measured from each job's own items; the cancelled job has none
        usage = {u['job_id']: u for u in self.c.get('/api/v1/usage', headers=self.H).json()['items']}
        self.assertEqual((usage[j1]['quantity'], usage[j2]['quantity']), (3, 2)); self.assertNotIn(j3, usage); self.assertTrue(usage[j1]['signature_valid'] and usage[j2]['signature_valid'])
        # the big job runs alone (a compatible small job submitted with it does not fit the char limit either way)
        j4b = self.embed_job(['small'])
        first = self.w.run_once(); self.assertEqual(first[1], 'succeeded')
        self.assertEqual({j4, j4b} - {first[0]}, {j4b if first[0] == j4 else j4})
        other = (j4b if first[0] == j4 else j4)
        self.assertEqual(self.c.get('/api/v1/models/jobs/' + other, headers=self.H).json()['state'], 'queued')
        self.assertEqual(self.w.run_once(), (other, 'succeeded')); m4, _ = self.vectors_of(j4); self.assertIsNone(m4['batch'])
        # batching disabled: two compatible jobs run separately
        self.inst.settings.limits['model_batch_enabled'] = 0
        j5, j6 = self.embed_job(['x']), self.embed_job(['y'])
        first = self.w.run_once(); self.assertEqual(first[1], 'succeeded'); self.assertIn(first[0], (j5, j6))
        rest = j6 if first[0] == j5 else j5
        self.assertEqual(self.c.get('/api/v1/models/jobs/' + rest, headers=self.H).json()['state'], 'queued')          # not batched: still queued after one run
        self.assertEqual(self.w.run_once(), (rest, 'succeeded')); m5, _ = self.vectors_of(rest); self.assertIsNone(m5['batch'])

    def test_warmup_policy_ceiling_drain_and_reporting(self):
        host = self.w.models.host
        with Database(self.inst.settings.db_path).read() as db:
            est = {r['id']: r['resource_estimate_bytes'] for r in db.execute('SELECT id, resource_estimate_bytes FROM model_revisions').fetchall()}
        emb, gen = self.ids['embed'], self.ids['generate']
        # refusals: unknown revision, viewer role
        self.assertEqual(self.c.post('/api/v1/models/warmup', headers=self.H, json={'revision_ids': ['mr_nope']}).status_code, 404)
        self.assertEqual(self.c.post('/api/v1/models/warmup', headers=self.inst.h('viewer'), json={'revision_ids': [emb]}).status_code, 403)
        # policy: embed then generate under a ceiling that only fits the embedding model
        r = self.c.post('/api/v1/models/warmup', headers=self.H, json={'revision_ids': [emb, gen], 'ceiling_bytes': est[emb] + est[gen] // 2})
        self.assertEqual(r.status_code, 200, r.text)
        acted = host.apply_warmup(self.w.models.registry)
        self.assertEqual(acted['loaded'], [emb]); self.assertEqual(acted['refused'][0]['revision_id'], gen); self.assertIn('ceiling', acted['refused'][0]['reason'])
        facts = self.c.get('/api/v1/models/runtime', headers=self.H).json()
        res = {x['revision_id']: x for x in facts['warmup']['resident']}
        self.assertEqual((res[emb]['state'], res[emb]['warm']), ('ready', True)); self.assertNotIn(gen, res)
        self.assertEqual(facts['warmup']['policy']['revision_ids'], [emb, gen])
        # idle cleanup never evicts a warm runtime
        self.inst.settings.limits['model_idle_unload_seconds'] = 1
        host.children[emb].last_used -= 10
        self.assertEqual(host.idle_cleanup(), 0); self.assertIn(emb, host.children)
        # numerical compute needing memory drains the warm runtime (floor above what the host can offer), state is reported honestly
        drained = host.drain_for_compute(10 ** 18, 'test compute')
        self.assertEqual(drained['drained'], [emb])
        facts = self.c.get('/api/v1/models/runtime', headers=self.H).json()
        res = {x['revision_id']: x for x in facts['warmup']['resident']}
        self.assertEqual((res[emb]['state'], res[emb]['warm'], res[emb]['drain_reason']), ('unloaded', True, 'test compute'))
        # the next loop iteration re-warms it (memory is available again); disabling the policy releases it
        self.assertEqual(host.apply_warmup(self.w.models.registry)['loaded'], [emb])
        self.c.post('/api/v1/models/warmup', headers=self.H, json={'enabled': False, 'revision_ids': []})
        self.assertEqual(host.apply_warmup(self.w.models.registry)['released'], [emb]); self.assertNotIn(emb, host.children)
        self.assertEqual({x['revision_id']: x['warm'] for x in self.c.get('/api/v1/models/runtime', headers=self.H).json()['currently']['runtimes']}[emb], False)
