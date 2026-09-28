"""§65-1 evaluation-run registry: immutable suites, runs through ordinary jobs, mechanical scoring, hidden items,
comparison across revisions and the promotion gate."""
import unittest

from metacoin_service.tests.test_models import ModelInstance, HAVE_TORCH, installed, GEN, EMB


@unittest.skipUnless(HAVE_TORCH, 'no torch-capable interpreter on this host')
class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.inst = ModelInstance(); self.addCleanup(self.inst.close); self.c = self.inst.client; self.H = self.inst.h('owner')
        if not installed(self.inst.settings, GEN) or not installed(self.inst.settings, EMB):
            self.skipTest('pinned model artifacts not installed in the model store')
        self.ids = self.inst.register_defaults()
        self.w = self.inst.worker(); self.addCleanup(self.w.offline)

    def test_suite_run_score_compare_and_gate(self):
        items = [{'id': 'g1', 'type': 'generation', 'prompt': 'Name the chemical symbol for water.', 'max_output_tokens': 16, 'must_contain': ['H2O'], 'must_not_contain': ['hunter2']},
                 {'id': 'g2', 'type': 'generation', 'prompt': 'Answer with one word: what colour is the clear daytime sky?', 'max_output_tokens': 8, 'must_contain': ['blue'], 'hidden': True},
                 {'id': 'g3', 'type': 'generation', 'prompt': 'Write the number twelve as digits only.', 'max_output_tokens': 6, 'must_contain': ['12']}]
        s = self.c.post('/api/v1/evaluation/suites', headers=self.H, json={'name': 'structural', 'items': items, 'threshold_percent': 66}).json()
        self.assertEqual((s['version'], s['item_count'], len(s['digest'])), (1, 3, 64))
        viewer_view = self.c.get('/api/v1/evaluation/suites', headers=self.inst.h('viewer')).json()['items'][0]
        self.assertNotIn('prompt', viewer_view['items'][1]); self.assertIn('prompt', viewer_view['items'][0])          # hidden items stay hidden from non-admins
        self.assertEqual(self.c.post('/api/v1/evaluation/suites', headers=self.H, json={'name': 'bad', 'items': [{'id': 'x', 'type': 'shell'}]}).status_code, 422)
        r = self.c.post('/api/v1/evaluation/suites/' + s['id'] + '/runs', headers=self.H, json={}).json()
        self.assertEqual((r['state'], len(r['jobs'])), ('running', 3))
        for _ in range(3):
            self.assertEqual(self.w.run_once()[1], 'succeeded')
        scored = self.c.get('/api/v1/evaluation/runs/' + r['id'], headers=self.H).json()
        self.assertEqual((scored['state'], scored['total']), ('scored', 3)); self.assertIsNotNone(scored['percent'])
        self.assertTrue(all(any(c['check'] == 'output_limit_honoured' and c['ok'] for c in x['checks']) for x in scored['results']))
        again = self.c.get('/api/v1/evaluation/runs/' + r['id'], headers=self.H).json()
        self.assertEqual(again['scored_at'], scored['scored_at'])
        # a second revision of the same artifact: compare item outcomes on the same immutable suite
        reg2 = self.c.post('/api/v1/models', headers=self.H, json=dict(GEN, model_id='qwen-candidate')).json()
        r2 = self.c.post('/api/v1/evaluation/suites/' + s['id'] + '/runs', headers=self.H, json={'model_revision_id': reg2['id']}).json()
        for _ in range(3):
            self.assertEqual(self.w.run_once()[1], 'succeeded')
        cmp = self.c.get('/api/v1/evaluation/compare/%s/%s' % (r['id'], r2['id']), headers=self.H).json()
        self.assertEqual(cmp['a']['revision'], self.ids['generate']); self.assertEqual(cmp['b']['revision'], reg2['id']); self.assertEqual(cmp['unchanged'], 3)
        # promotion gate: with the gate set to this suite, a revision without a passing scored run cannot be promoted
        reg3 = self.c.post('/api/v1/models', headers=self.H, json=dict(GEN, model_id='qwen-ungated')).json()
        self.c.post('/api/v1/evaluation/gate', headers=self.H, json={'suite_id': s['id']})
        refused = self.c.post('/api/v1/models/' + reg3['id'] + '/promote', headers=self.H, json={'operation': 'generate'})
        self.assertEqual((refused.status_code, refused.json()['detail']['code']), (409, 'evaluation_required'))
        scored2 = self.c.get('/api/v1/evaluation/runs/' + r2['id'], headers=self.H).json()
        if scored2['meets_threshold']:
            self.assertEqual(self.c.post('/api/v1/models/' + reg2['id'] + '/promote', headers=self.H, json={'operation': 'generate'}).status_code, 200)
        else:
            self.assertEqual(self.c.post('/api/v1/models/' + reg2['id'] + '/promote', headers=self.H, json={'operation': 'generate'}).status_code, 409)
        self.c.post('/api/v1/evaluation/gate', headers=self.H, json={'suite_id': None})
        self.assertEqual(self.c.post('/api/v1/models/' + reg3['id'] + '/promote', headers=self.H, json={'operation': 'generate'}).status_code, 200)
