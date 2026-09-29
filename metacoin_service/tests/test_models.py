"""Local model services through real entry points: registry (pinned artifacts, allowlisted loader), promotion,
genuine generation and embeddings by the worker's runtime child, durable segments with a cursor, usage by measured
tokens under a quote, cancellation with preserved partial output, retire/revoke semantics, access refusals."""
import json
import os
import threading
import time
import unittest

from metacoin_service.tests.test_service import Instance
from metacoin_service.compute.engine import compute_interpreter
from metacoin_service.compute import npy
from metacoin_service.models import registry as registry_mod

RUNTIME = compute_interpreter(type('S', (), {'compute_python': ''})())
HAVE_TORCH = bool(RUNTIME and RUNTIME.get('torch'))
GEN = {'model_id': 'qwen2.5-0.5b-instruct', 'hub_repo': 'Qwen/Qwen2.5-0.5B-Instruct', 'revision': '7ae557604adf67be50417f59c2c2f167def9a775', 'operations': ['generate'], 'license': 'apache-2.0'}
EMB = {'model_id': 'all-minilm-l6-v2', 'hub_repo': 'sentence-transformers/all-MiniLM-L6-v2', 'revision': '1110a243fdf4706b3f48f1d95db1a4f5529b4d41', 'operations': ['embed'], 'license': 'apache-2.0'}


def installed(settings, spec):
    return registry_mod.local_dir(settings, spec['hub_repo'], spec['revision']).is_dir()


class ModelInstance(Instance):
    def register_defaults(self):
        H = self.h('owner')
        out = {}
        for spec in (GEN, EMB):
            r = self.client.post('/api/v1/models', headers=H, json=spec)
            assert r.status_code == 201, r.text
            out[spec['operations'][0]] = r.json()['id']
            r = self.client.post('/api/v1/models/' + r.json()['id'] + '/promote', headers=H, json={'operation': spec['operations'][0], 'evidence': {'source': 'test registration'}})
            assert r.status_code == 200, r.text
        return out

    def wait(self, jid, timeout=180):
        deadline = time.time() + timeout
        while time.time() < deadline:
            v = self.client.get('/api/v1/models/jobs/' + jid, headers=self.h('owner')).json()
            if v['state'] in ('succeeded', 'failed', 'cancelled'):
                return v
            time.sleep(0.3)
        return v


@unittest.skipUnless(HAVE_TORCH, 'no torch-capable interpreter on this host')
class ModelRegistryTests(unittest.TestCase):
    def setUp(self):
        self.inst = ModelInstance(); self.addCleanup(self.inst.close); self.c = self.inst.client; self.H = self.inst.h('owner')
        if not installed(self.inst.settings, GEN) or not installed(self.inst.settings, EMB):
            self.skipTest('pinned model artifacts not installed in the model store')

    def test_registration_binds_identity_and_refuses_unsafe_inputs(self):
        r = self.c.post('/api/v1/models', headers=self.H, json=GEN)
        self.assertEqual(r.status_code, 201, r.text)
        v = r.json()
        self.assertEqual((v['architecture'], v['loader'], v['weight_format'], v['installed'], v['status'], v['license']), ('Qwen2ForCausalLM', 'causal_lm', 'safetensors', True, 'registered', 'apache-2.0'))
        self.assertEqual(len(v['weight_digest']), 64); self.assertEqual(len(v['tokenizer_digest']), 64)
        self.assertFalse(v['callable']); self.assertIn('no runtime has loaded', v['readiness'])
        # immutable: same revision again is a conflict; unknown fields, paths, unpinned revisions, unsupported ops are refused
        self.assertEqual(self.c.post('/api/v1/models', headers=self.H, json=GEN).status_code, 409)
        for bad, code in ((dict(GEN, model_id='x2', hub_repo='../../etc'), 422), (dict(GEN, model_id='x3', revision='main'), 422), (dict(GEN, model_id='x4', local_dir='/tmp'), 422),
                          (dict(GEN, model_id='x5', operations=['embed']), 422), (dict(GEN, model_id='x6', license='proprietary'), 422)):
            self.assertEqual(self.c.post('/api/v1/models', headers=self.H, json=bad).status_code, code, bad)
        # an unregistered artifact identity is registered but not installed; no default exists for generate until promoted
        r = self.c.post('/api/v1/models', headers=self.H, json=dict(GEN, model_id='absent-model', revision='0' * 40))
        self.assertEqual((r.status_code, r.json()['installed']), (201, False)); self.assertTrue(r.json()['install_problems'])
        self.assertEqual(self.c.post('/api/v1/models/' + r.json()['id'] + '/promote', headers=self.H, json={'operation': 'generate'}).status_code, 409)
        g = self.c.post('/api/v1/models/generate', headers=self.H, json={'inputs': {'messages': [{'role': 'user', 'content': 'hi'}], 'max_output_tokens': 8}})
        self.assertEqual((g.status_code, g.json()['detail']['code']), (501, 'no_default_model'))
        # roles: reviewer/viewer/worker cannot register; viewer can list without private config
        for role in ('reviewer', 'viewer', 'worker'):
            self.assertEqual(self.c.post('/api/v1/models', headers=self.inst.h(role), json=dict(GEN, model_id='r-' + role)).status_code, 403)
        lst = self.c.get('/api/v1/models', headers=self.inst.h('viewer')).json()['items']
        self.assertTrue(lst); self.assertNotIn('config', lst[0])


@unittest.skipUnless(HAVE_TORCH, 'no torch-capable interpreter on this host')
class ModelInferenceTests(unittest.TestCase):
    def setUp(self):
        self.inst = ModelInstance(); self.addCleanup(self.inst.close); self.c = self.inst.client; self.H = self.inst.h('owner')
        if not installed(self.inst.settings, GEN) or not installed(self.inst.settings, EMB):
            self.skipTest('pinned model artifacts not installed in the model store')
        self.ids = self.inst.register_defaults()
        self.w = self.inst.worker(); self.addCleanup(self.w.offline)

    def test_generation_embedding_segments_usage_and_lifecycle(self):
        # 1. genuine generation with a new bounded input (greedy, seeded), through the ordinary queue
        q = 'Name the three primary colours, separated by commas, and nothing else.'
        g = self.c.post('/api/v1/models/generate', headers=self.H, json={'inputs': {'messages': [{'role': 'system', 'content': 'Answer briefly.'}, {'role': 'user', 'content': q}], 'max_output_tokens': 24, 'seed': 3}})
        self.assertEqual(g.status_code, 202, g.text); jid = g.json()['job_id']
        view = self.c.get('/api/v1/models/jobs/' + jid, headers=self.H).json()
        self.assertEqual((view['state'], view['phase'], view['model']['revision_id']), ('queued', 'admitted', self.ids['generate']))
        t0 = time.time(); self.assertEqual(self.w.run_once()[1], 'succeeded'); first = time.time() - t0
        v = self.inst.wait(jid)
        self.assertEqual((v['state'], v['phase'], v['usage']['finish_reason'] in ('eos', 'length', 'stop')), ('succeeded', 'completed', True), v)
        self.assertGreater(v['usage']['output_tokens'], 0); self.assertGreater(v['usage']['input_tokens'], 10); self.assertGreater(v['timing']['load_ms'], 0)
        segs = self.c.get('/api/v1/models/jobs/' + jid + '/segments', headers=self.H).json()
        text = ''.join(s['text'] for s in segs['segments'])
        self.assertTrue(segs['done']); self.assertTrue(text.strip(), segs)
        self.assertTrue(any(w in text.lower() for w in ('red', 'blue', 'yellow', 'green')), text)     # a real answer, not a canned string
        out = self.c.get('/api/v1/models/jobs/' + jid + '/outputs/output.json', headers=self.H).json()
        self.assertEqual((out['text'], out['model_revision_id'], out['usage']['output_tokens']), (text, self.ids['generate'], v['usage']['output_tokens']))
        # resumable delivery: a cursor returns only later segments; repeated reads never create another generation
        later = self.c.get('/api/v1/models/jobs/' + jid + '/segments?after=%d' % (len(segs['segments']) - 2), headers=self.H).json()
        self.assertEqual(len(later['segments']), 1); self.assertEqual(later['segments'][0]['text'], segs['segments'][-1]['text'])
        job = self.c.get('/api/v1/jobs/' + jid, headers=self.H).json()
        self.assertEqual((job['outcome'], job['model']['verification']), ('GENERATED', 'none: model output is data, not a verified result'))
        # readiness is observed: the registry now reports the worker host as ready and the revision callable
        det = self.c.get('/api/v1/models/' + self.ids['generate'], headers=self.H).json()
        self.assertTrue(det['callable']); self.assertEqual(det['runtimes'][0]['state'], 'ready'); self.assertEqual(det['runtimes'][0]['device'], 'cuda' if RUNTIME.get('cuda') else 'cpu')
        # 2. a second request reuses the warm runtime (no reload): load_ms 0 and faster wall time
        g2 = self.c.post('/api/v1/models/generate', headers=self.H, json={'inputs': {'prompt': 'The capital of France is', 'max_output_tokens': 6}})
        t0 = time.time(); self.assertEqual(self.w.run_once()[1], 'succeeded'); second = time.time() - t0
        v2 = self.inst.wait(g2.json()['job_id'])
        self.assertEqual(v2['timing']['load_ms'], 0); self.assertLess(second, first)
        # 3. embeddings: vectors stored as a private npy artifact; similar sentences closer than an unrelated one
        texts = ['The heat equation describes diffusion of temperature.', 'Temperature diffusion follows the heat equation.', 'A Monte Carlo estimate uses random samples.']
        e = self.c.post('/api/v1/models/embed', headers=self.H, json={'inputs': {'texts': texts}})
        self.assertEqual(e.status_code, 202, e.text); ejid = e.json()['job_id']
        self.assertEqual(self.w.run_once()[1], 'succeeded')
        ev = self.inst.wait(ejid)
        self.assertEqual((ev['state'], ev['usage']['items']), ('succeeded', 3))
        raw = self.c.get('/api/v1/models/jobs/' + ejid + '/outputs/vectors.npy', headers=self.H).content
        vals, dtype, shape = npy.decode(raw)
        self.assertEqual((dtype, shape), ('<f8', [3, 384]))
        rows = [vals[i * 384:(i + 1) * 384] for i in range(3)]
        dot = lambda a, b: sum(x * y for x, y in zip(a, b))
        self.assertAlmostEqual(dot(rows[0], rows[0]), 1.0, places=5)
        self.assertGreater(dot(rows[0], rows[1]), dot(rows[0], rows[2]) + 0.2)
        meta = self.c.get('/api/v1/models/jobs/' + ejid + '/outputs/output.json', headers=self.H).json()
        self.assertEqual((meta['pooling'], meta['normalized'], meta['dim']), ('mean', True, 384))
        # 4. refusals: viewer cannot read segments/outputs; unknown sampling fields refused; oversized max refused; truncation refused unless explicit
        for path in ('/segments', '/outputs', '/outputs/output.json'):
            self.assertEqual(self.c.get('/api/v1/models/jobs/' + jid + path, headers=self.inst.h('viewer')).status_code, 403, path)
        bad = self.c.post('/api/v1/models/generate', headers=self.H, json={'inputs': {'prompt': 'x', 'max_output_tokens': 4, 'tools': ['shell']}})
        self.assertEqual((bad.status_code, bad.json()['detail']['code']), (422, 'model_input'))
        self.assertEqual(self.c.post('/api/v1/models/generate', headers=self.H, json={'inputs': {'prompt': 'x', 'max_output_tokens': 999999}}).status_code, 422)
        long = self.c.post('/api/v1/models/embed', headers=self.H, json={'inputs': {'texts': ['word ' * 600]}})
        self.assertEqual(long.status_code, 202); self.assertEqual(self.w.run_once()[1], 'failed')
        lv = self.inst.wait(long.json()['job_id']); self.assertEqual(lv['state'], 'failed'); self.assertIn('INPUT_TOO_LONG', lv['error'])
        ok = self.c.post('/api/v1/models/embed', headers=self.H, json={'inputs': {'texts': ['word ' * 600], 'truncate': True}})
        self.assertEqual(self.w.run_once()[1], 'succeeded'); okv = self.inst.wait(ok.json()['job_id'])
        self.assertEqual(json.loads(self.c.get('/api/v1/models/jobs/' + ok.json()['job_id'] + '/outputs/output.json', headers=self.H).content)['truncated'], [True])
        # 5. usage under a quote: quantity = tokens actually generated (measured), never the reserved allowance
        sid = next(s['id'] for s in self.c.get('/api/v1/services', headers=self.H).json()['items'] if s['kind'] == 'text_generation')
        inputs = {'schema': 'text-generation-input/v1', 'prompt': 'Count from one to three:', 'max_output_tokens': 200}
        qt = self.c.post('/api/v1/services/' + sid + '/quote', headers=self.H, json={'inputs': inputs})
        self.assertEqual((qt.status_code, qt.json()['quantity_max'], qt.json()['amount_max']), (201, 200, 200), qt.text)
        self.c.post('/api/v1/quotes/' + qt.json()['quote_id'] + '/accept', headers=self.H)
        inv = self.c.post('/api/v1/services/' + sid + '/invoke', headers=self.H, json={'quote_id': qt.json()['quote_id'], 'inputs': inputs}).json()
        self.assertEqual(self.w.run_once()[1], 'succeeded')
        uv = self.inst.wait(inv['job_id'])
        usage = [u for u in self.c.get('/api/v1/usage', headers=self.H).json()['items'] if u['job_id'] == inv['job_id']][0]
        self.assertEqual(usage['quantity'], uv['usage']['output_tokens']); self.assertLess(usage['quantity'], 200); self.assertTrue(usage['signature_valid'])
        # 6. cancellation mid-generation: partial output preserved, job cancelled, no usage record
        big = self.c.post('/api/v1/models/generate', headers=self.H, json={'inputs': {'prompt': 'Write a long story about a lighthouse keeper.', 'max_output_tokens': 400}})
        bjid = big.json()['job_id']
        def cancel_soon():
            time.sleep(1.5)
            self.c.post('/api/v1/jobs/' + bjid + '/cancel', headers=self.H)
        th = threading.Thread(target=cancel_soon); th.start()
        outcome = self.w.run_once()[1]; th.join()
        bv = self.inst.wait(bjid)
        self.assertEqual((outcome, bv['state'], bv['usage']['finish_reason']), ('cancelled', 'cancelled', 'cancelled'), bv)
        self.assertGreater(bv['usage']['segments'], 0); self.assertLess(bv['usage']['output_tokens'], 400)
        partial = self.c.get('/api/v1/models/jobs/' + bjid + '/segments', headers=self.H).json()
        self.assertTrue(partial['segments']); self.assertTrue(partial['done'])
        self.assertFalse([u for u in self.c.get('/api/v1/usage', headers=self.H).json()['items'] if u['job_id'] == bjid])
        # 7. retire: no new requests bind it, an already accepted job still runs; revoke: accepted job refused, history readable
        rid = self.ids['generate']
        pending = self.c.post('/api/v1/models/generate', headers=self.H, json={'inputs': {'prompt': 'Say yes.', 'max_output_tokens': 3}}).json()['job_id']
        self.assertEqual(self.c.post('/api/v1/models/' + rid + '/retire', headers=self.H, json={'reason': 'superseded'}).json()['status'], 'retired')
        self.assertEqual(self.c.post('/api/v1/models/generate', headers=self.H, json={'inputs': {'prompt': 'x', 'max_output_tokens': 3}}).json()['detail']['code'], 'no_default_model')
        self.assertEqual(self.c.post('/api/v1/models/generate', headers=self.H, json={'inputs': {'prompt': 'x', 'max_output_tokens': 3, 'model_revision_id': rid}}).json()['detail']['code'], 'model_retired')
        self.assertEqual(self.w.run_once()[1], 'succeeded'); self.assertEqual(self.inst.wait(pending)['state'], 'succeeded')
        pending2 = self.c.post('/api/v1/contracts', headers=self.H, json={'kind': 'text_generation', 'title': 'x', 'inputs': {'schema': 'text-generation-input/v1', 'prompt': 'x', 'max_output_tokens': 3, 'model_revision_id': rid}, 'policy': {'reviewer_id': self.inst.ids['reviewer']}})
        self.assertEqual(pending2.status_code, 409)
        # promote again then revoke: a queued job bound to the revoked revision fails with MODEL_REVOKED; old results stay readable
        self.assertEqual(self.c.post('/api/v1/models', headers=self.H, json=dict(GEN, model_id='qwen-again')).status_code, 201)
        again = [m for m in self.c.get('/api/v1/models', headers=self.H).json()['items'] if m['model_id'] == 'qwen-again'][0]['id']
        self.c.post('/api/v1/models/' + again + '/promote', headers=self.H, json={'operation': 'generate'})
        queued = self.c.post('/api/v1/models/generate', headers=self.H, json={'inputs': {'prompt': 'Say no.', 'max_output_tokens': 3}}).json()['job_id']
        self.assertEqual(self.c.post('/api/v1/models/' + again + '/revoke', headers=self.H, json={'reason': 'test revocation'}).json()['status'], 'revoked')
        self.assertEqual(self.w.run_once()[1], 'failed')
        rv = self.inst.wait(queued); self.assertEqual(rv['state'], 'failed'); self.assertIn('revoked', rv['error'])
        self.assertEqual(self.c.get('/api/v1/models/jobs/' + jid + '/outputs/output.json', headers=self.H).status_code, 200)
        # rollback of the default pointer to the retired revision is refused (not executable); promotions are recorded
        self.assertEqual(self.c.post('/api/v1/models/' + again + '/rollback-default', headers=self.H, json={'operation': 'generate'}).status_code, 409)
        proms = self.c.get('/api/v1/models/promotions', headers=self.H).json()['items']
        self.assertGreaterEqual(len(proms), 3)
        facts = self.c.get('/api/v1/models/runtime', headers=self.H).json()
        self.assertTrue(facts['installed']['torch']); self.assertTrue(facts['currently']['runtimes'])
