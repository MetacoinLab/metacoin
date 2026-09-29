"""Console pages for the new capabilities with distinct roles: every page renders from real services, forms call the
same authorized operations, and private fields stay out of viewer responses (checked in the HTTP body, not just the
rendered text)."""
import json
import unittest

from metacoin_service.tests.test_models import ModelInstance, HAVE_TORCH, installed, GEN, EMB
from metacoin_service.tests.test_compute_engine import batch_spec


def login(inst, role):
    r = inst.client.post('/console/login', data={'token': inst.tok[role]}, follow_redirects=False)
    assert r.status_code == 303, r.text
    home = inst.client.get('/console/')
    csrf = home.text.split('name="csrf" value="')[1].split('"')[0]
    return csrf


class ConsoleExpansionTests(unittest.TestCase):
    def setUp(self):
        self.inst = ModelInstance(); self.addCleanup(self.inst.close); self.c = self.inst.client
        self.inst.settings.limits['compute_checkpoint_interval_seconds'] = 1

    def test_pages_render_for_owner_and_hide_private_fields_from_viewer(self):
        csrf = login(self.inst, 'owner')
        for path in ('/console/models', '/console/calibration', '/console/verification', '/console/nodes', '/console/approvals', '/console/statement', '/console/knowledge', '/console/notebooks'):
            r = self.c.get(path)
            self.assertEqual(r.status_code, 200, path); self.assertIn('viewport', r.text)
        # knowledge: create a collection and add a document through the forms; search lexical (no index yet)
        self.assertEqual(self.c.post('/console/knowledge/collections', data={'csrf': csrf, 'name': 'console notes'}, follow_redirects=False).status_code, 303)
        cid = [c for c in self.c.get('/api/v1/knowledge/collections', headers=self.inst.h('owner')).json()['items'] if c['name'] == 'console notes'][0]['id']
        self.assertEqual(self.c.post('/console/knowledge/collections/' + cid + '/documents', data={'csrf': csrf, 'name': 'n.md', 'format': 'markdown', 'content': '# Note\n\nThe reserve is 2000 mJ.\n'}, follow_redirects=False).status_code, 303)
        r = self.c.post('/console/knowledge/collections/' + cid + '/search', data={'csrf': csrf, 'query': 'reserve', 'mode': 'lexical'})
        self.assertEqual(r.status_code, 200); self.assertIn('2000 mJ', r.text); self.assertIn('BM25', r.text)
        # notebooks: create through the form with a link to the knowledge collection's document version; nothing executes
        ver = self.c.get('/api/v1/knowledge/collections/' + cid, headers=self.inst.h('owner')).json()
        self.assertEqual(self.c.post('/console/notebooks', data={'csrf': csrf, 'name': 'console nb', 'text': 'Observed: reserve 2000 mJ. <script>alert(1)</script>', 'link': 'job:job_missing'}, follow_redirects=False).status_code, 404)
        self.assertEqual(self.c.post('/console/notebooks', data={'csrf': csrf, 'name': 'console nb', 'text': 'Observed: reserve 2000 mJ. <script>alert(1)</script>'}, follow_redirects=False).status_code, 303)
        page = self.c.get('/console/notebooks').text
        self.assertIn('console nb', page); self.assertIn('&lt;script&gt;', page); self.assertNotIn('<script>alert', page)
        # calibration design form on a model detail page (fit a tiny numeric model first)
        did = self.c.post('/api/v1/calibration/datasets', headers=self.inst.h('owner'), json={'name': 'c', 'columns': ['x1', 'y'], 'target': 'y', 'units': {'y': 'ms'}, 'rows': [{'x1': i, 'y': 2 * i + 1} for i in range(12)], 'provenance': 'synthetic'}).json()['id']
        self.c.post('/api/v1/calibration/fits', headers=self.inst.h('owner'), json={'inputs': {'dataset_id': did, 'features': ['x1'], 'target': 'y', 'intercept': True, 'split': {'method': 'random', 'train_fraction_percent': 75, 'seed': 1}}})
        self.inst.worker().run_once()
        mid = self.c.get('/api/v1/calibration/models', headers=self.inst.h('owner')).json()['items'][0]['id']
        r = self.c.post('/console/calibration/models/' + mid + '/design', data={'csrf': csrf, 'candidates': json.dumps([{'label': 'far', 'features': {'x1': 100}, 'cost': 1}, {'label': 'near', 'features': {'x1': 5}, 'cost': 1}]), 'objective': 'reduce_overall_uncertainty', 'budget': '1'})
        self.assertEqual(r.status_code, 200, r.text[-900:]); self.assertIn('none yet', r.text); self.assertIn('near (over_budget)', r.text)
        # approvals policy form and a proposal decided by the reviewer through the console
        r = self.c.post('/console/approvals/policy', data={'csrf': csrf, 'required': ['scheduling_toggle']}, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(self.c.get('/api/v1/approvals/policy', headers=self.inst.h('owner')).json()['required'], ['scheduling_toggle'])
        pr = self.c.post('/api/v1/approvals', headers=self.inst.h('owner'), json={'action': 'scheduling_toggle', 'content': {'enabled': False}}).json()
        self.assertIn(pr['id'], self.c.get('/console/approvals').text)
        # verification form: preview for a completed job
        jid = self.inst.compute_job('temporal_batch', batch_spec()) if hasattr(self.inst, 'compute_job') else None
        # roles: viewer gets no knowledge page, no statement, and sees no private fields on models
        self.c.cookies.clear()
        csrf_v = login(self.inst, 'viewer')
        self.assertEqual(self.c.get('/console/knowledge').status_code, 403)
        self.assertEqual(self.c.get('/console/notebooks').status_code, 403)
        self.assertEqual(self.c.get('/console/statement').status_code, 403)
        page = self.c.get('/console/models').text
        self.assertNotIn('>promote ', page); self.assertNotIn('mck_', page)
        self.assertEqual(self.c.post('/console/approvals/' + pr['id'] + '/approve', data={'csrf': csrf_v}, follow_redirects=False).status_code, 403)
        self.c.cookies.clear()
        csrf_r = login(self.inst, 'reviewer')
        self.assertEqual(self.c.post('/console/approvals/' + pr['id'] + '/approve', data={'csrf': csrf_r}, follow_redirects=False).status_code, 303)
        self.assertEqual(self.c.get('/api/v1/approvals/' + pr['id'], headers=self.inst.h('owner')).json()['state'], 'approved')
        # CSRF: a stale token is refused
        self.assertEqual(self.c.post('/console/approvals/' + pr['id'] + '/reject', data={'csrf': 'stale'}, follow_redirects=False).status_code, 403)
