"""Console workflow pages of the independent machine network (group J) render for a signed-in operator, show environment,
asset, custody and domain, and an expired session never loses an unsent form nor resubmits it."""
import unittest

from metacoin_service.tests.test_native_missions import MissionBase


class ConsoleNetworkTests(MissionBase):
    def login(self, token):
        r = self.c.post('/console/login', data={'token': token}, follow_redirects=False); self.assertEqual(r.status_code, 303); return r.cookies

    def test_pages_render_with_context_and_expired_session_keeps_draft(self):
        m = self.mission(policy={'compute_cpu_ms': 20}); mid = m['id']; self.drive_work(mid, 'acquire', 4); self.step(mid)
        ck = self.login(self.inst.tok['owner'])
        for path, needle in (('/console/providers', 'Identity separation'), ('/console/pools', 'Resource pools'), ('/console/products', 'Mission products'), ('/console/relay', 'Relay routes'), ('/console/portfolio', 'Portfolio'), ('/console/missions/%s/continuity' % mid, 'Active generation'), ('/console/operator', 'What each machine owns'), ('/console/missions/' + mid, 'continuity page'), ('/console/portfolio?missions=%s' % mid, 'spendable')):
            r = self.c.get(path, cookies=ck); self.assertEqual(r.status_code, 200, (path, r.text[:300])); self.assertIn(needle, r.text, path)
            self.assertIn('environment <code>dev</code>', r.text) if path != '/console/operator' and not path.startswith('/console/missions/' + mid) else None
        # a provider creates a pool + offer through the console form (work:offer belongs to providers, not the administrator)
        sk = self.login(self.actors['supplier']['h']['Authorization'].split(' ', 1)[1]); scsrf = self.c.get('/console/pools', cookies=sk).text.split('name="csrf" value="')[1].split('"')[0]
        r = self.c.post('/console/pools', data={'csrf': scsrf, 'kind': 'compute', 'label': 'rig-1', 'slot_unit': 'cpu_slot', 'units_per_slot_second': '1000', 'capacity': '1', 'hours': '2', 'publish_offer': '1', 'offer_capacity': '1000', 'price': '1'}, cookies=sk, follow_redirects=False); self.assertEqual(r.status_code, 303, r.text[:300])
        self.assertIn('rig-1', self.c.get('/console/pools', cookies=ck).text)
        csrf = self.c.get('/console/pools', cookies=ck).text.split('name="csrf" value="')[1].split('"')[0]
        # expired session: a mutating form (a paid purchase) is not performed; the draft is shown; nothing is paid
        self.c.post('/console/logout', data={'csrf': csrf}, cookies=ck, follow_redirects=False)
        r = self.c.post('/console/products/mp_x/purchase', data={'csrf': csrf, 'request_id': 'draft-1'}, cookies=ck, follow_redirects=False)
        self.assertEqual(r.status_code, 401); self.assertIn('Unsent form preserved', r.text); self.assertIn('draft-1', r.text); self.assertIn('Nothing was submitted and nothing was paid', r.text)
        self.assertEqual(self.c.get('/api/v1/products/entitlements', headers=self.R).json()['items'], [])


if __name__ == '__main__':
    unittest.main()
