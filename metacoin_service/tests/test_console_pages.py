"""Console pages for the new object families render for a session (owner and viewer) without leaking inputs."""
import unittest
from metacoin_service.tests.test_service import Instance
from metacoin_service.tests.test_agents import policy
from metacoin_service.tests.test_budgets import two_node_definition

PAGES = ['/console/services', '/console/datasets', '/console/workflows', '/console/campaigns', '/console/queue', '/console/agents', '/console/usage']


class ConsolePageTests(unittest.TestCase):
    def setUp(self):
        self.inst = Instance(); self.addCleanup(self.inst.close); self.c = self.inst.client; self.H = self.inst.h('owner')

    def session(self, role):
        s = self.c.post('/api/v1/session', json={'token': self.inst.tok[role]})
        return {'metacoin_session': s.cookies['metacoin_session']}, s.json()['csrf']

    def test_pages_render_with_content_and_controls(self):
        # populate every family
        jid = self.inst.job(); self.inst.worker().run_once()
        csv = "duration_s,harvest_low_mW,harvest_high_mW,load_low_mW,load_high_mW\n10,600,800,500,500\n"
        self.c.post('/api/v1/datasets', headers=self.H, json={'name': 'series', 'kind': 'temporal_series', 'format': 'csv', 'content': csv, 'provenance': 'declared'})
        wid = self.c.post('/api/v1/workflows', headers=self.H, json={'definition': two_node_definition('console')}).json()['id']
        rid = self.c.post('/api/v1/workflows/' + wid + '/runs', headers=self.H, json={'budget_ceiling': 2}).json()['run_id']
        self.c.post('/api/v1/runs/' + rid + '/advance', headers=self.H)
        g = self.c.post('/api/v1/agents/grants', headers=self.H, json={'policy': policy()}).json()
        cookies, csrf = self.session('owner')
        for path in PAGES + ['/console/runs/' + rid]:
            r = self.c.get(path, cookies=cookies)
            self.assertEqual(r.status_code, 200, path + ' ' + r.text[:200])
            self.assertNotIn('USER_PRIVATE_LABEL', r.text)
            self.assertNotIn('Traceback', r.text)
        self.assertIn('temporal-energy', self.c.get('/console/services', cookies=cookies).text)
        self.assertIn('series', self.c.get('/console/datasets', cookies=cookies).text)
        self.assertIn(rid, self.c.get('/console/workflows', cookies=cookies).text)
        self.assertIn(g['grant_id'], self.c.get('/console/agents', cookies=cookies).text)
        # queue page lists the registered worker; the owner can drain it from the console (CSRF-protected form)
        wk = self.c.get('/api/v1/workers', headers=self.H).json()['items'][0]
        self.assertIn(wk['name'], self.c.get('/console/queue', cookies=cookies).text)
        self.assertEqual(self.c.post('/console/workers/' + wk['id'] + '/drain', cookies=cookies, data={'csrf': 'wrong'}).status_code, 403)
        self.assertEqual(self.c.post('/console/workers/' + wk['id'] + '/drain', cookies=cookies, data={'csrf': csrf}, follow_redirects=False).status_code, 303)
        self.assertEqual(self.c.get('/api/v1/workers', headers=self.H).json()['items'][0]['state'], 'draining')
        # stopping a grant from the console
        self.assertEqual(self.c.post('/console/agents/' + g['grant_id'] + '/stop', cookies=cookies, data={'csrf': csrf}, follow_redirects=False).status_code, 303)
        self.assertEqual(self.c.get('/api/v1/agents/grants/' + g['grant_id'], headers=self.H).json()['state'], 'stopped')
        # a viewer sees the read-only pages and no admin controls
        vcookies, _ = self.session('viewer')
        for path in ['/console/services', '/console/datasets', '/console/workflows', '/console/campaigns', '/console/queue', '/console/usage', '/console/agents']:
            r = self.c.get(path, cookies=vcookies)
            self.assertEqual(r.status_code, 200, path)
            self.assertNotIn('Drain', r.text); self.assertNotIn('>Stop<', r.text)
        self.assertEqual(self.c.get('/console/runs/' + rid, cookies=vcookies).status_code, 200)


if __name__ == '__main__':
    unittest.main()
