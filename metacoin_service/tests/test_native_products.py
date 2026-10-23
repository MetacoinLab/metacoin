"""Reusable mission products (order 2026-10-23 group E; journeys 22–25 at unit level)."""
import hashlib
import unittest

from metacoin_service.tests.test_native_custody import CustodyTests


class ProductTests(CustodyTests):
    def computed_job(self):
        m = self.mission(policy={'compute_cpu_ms': 20}); mid = m['id']; self.drive_work(mid, 'acquire', 4); self.step(mid)
        s = self.drive_work(mid, 'compute', 40); self.assertEqual(s['result']['decision'], 'accepted'); return mid, self.ms(mid, 'compute')['binding']['job_id']

    def test_publish_purchase_reuse_and_crash_recovery(self):
        mid, jid = self.computed_job(); self.fund('machine', 100); self.fund('verifier', 100)
        bad = self.c.post('/api/v1/products', headers=self.R, json={'source_job': jid, 'price': 5, 'license': 'all-rights-transferred'}); self.assertEqual((bad.status_code, bad.json()['detail']['code']), (422, 'license_unsupported'))
        r = self.c.post('/api/v1/products', headers=self.R, json={'source_job': jid, 'price': 5, 'license': 'processing-service', 'title': 'route feasibility, Apollo 11 tile'}); self.assertEqual(r.status_code, 201, r.text); p = r.json(); pid = p['id']
        self.assertEqual((p['version'], p['kind'], p['state'], p['license']['id']), (1, 'verified_computation', 'listed', 'processing-service')); self.assertEqual(p['source_data']['product_id'], 'LDEM_4'); self.assertTrue(p['source_data']['image_sha256']); self.assertEqual(p['conventions']['resolution_mm_per_pixel'], 7580840)
        self.assertIn('not ownership of public data', p['rights']); self.assertIn('no invented benefit', p['measured']['note']); self.assertNotIn('%', str(p['measured']))
        again = self.c.post('/api/v1/products', headers=self.R, json={'source_job': jid, 'price': 5, 'license': 'processing-service'}).json(); self.assertTrue(again.get('replayed'))       # same content, same version
        # journey 23: purchase through native settlement; access delivered once; retries idempotent
        m0 = self.bal('machine'); r0 = self.bal('requester')
        e = self.c.post('/api/v1/products/%s/purchase' % pid, headers=self.M, json={'request_id': 'buy1'}); self.assertEqual(e.status_code, 200, e.text); e = e.json(); self.assertEqual((e['state'], e['obligation']['state']), ('purchased', 'settled')); self.assertEqual((self.bal('machine'), self.bal('requester')), (m0 - 5, r0 + 5))
        d = self.c.get('/api/v1/products/entitlements/%s/download' % e['id'], headers=self.M); self.assertEqual(d.status_code, 200); self.assertEqual(hashlib.sha256(d.content).hexdigest(), p['content_hash'])
        self.assertEqual(self.c.get('/api/v1/products/entitlements/' + e['id'], headers=self.M).json()['state'], 'delivered')
        rep = self.c.post('/api/v1/products/%s/purchase' % pid, headers=self.M, json={'request_id': 'buy1'}).json(); self.assertTrue(rep['replayed']); self.assertEqual(self.bal('machine'), m0 - 5)
        self.assertEqual(self.c.get('/api/v1/products/entitlements/%s/download' % e['id'], headers=self.M).status_code, 200); self.assertEqual(self.bal('machine'), m0 - 5)
        self.assertEqual(self.c.get('/api/v1/products/entitlements/%s/download' % e['id'], headers=self.actors['verifier']['h']).status_code, 403)
        # journey 24: crash after settlement, before the response: the retry finds the settled purchase and delivers without charging again
        v0 = self.bal('verifier'); self.c.post('/api/v1/ops/faults', headers=self.H, json={'fault': 'product_delivery'})
        c = self.c.post('/api/v1/products/%s/purchase' % pid, headers=self.actors['verifier']['h'], json={'request_id': 'buy2'}); self.assertEqual(c.status_code, 409); self.c.post('/api/v1/ops/faults', headers=self.H, json={'fault': 'product_delivery', 'disarm': True})
        self.assertEqual(self.bal('verifier'), v0 - 5)                                                                                     # settled durably although the response was lost
        e2 = self.c.post('/api/v1/products/%s/purchase' % pid, headers=self.actors['verifier']['h'], json={'request_id': 'buy2'}).json(); self.assertTrue(e2['replayed']); self.assertEqual(e2['state'], 'purchased'); self.assertEqual(self.bal('verifier'), v0 - 5)
        self.assertEqual(self.c.get('/api/v1/products/entitlements/%s/download' % e2['id'], headers=self.actors['verifier']['h']).status_code, 200); self.assertEqual(self.bal('requester'), r0 + 10)
        # journey 25: incompatible reuse is refused with reasons
        ok = self.c.post('/api/v1/products/%s/check-reuse' % pid, headers=self.M, json={'resolution_mm_per_pixel': 7580840, 'units': {'slope': 'milli-degrees'}, 'source': 'apollo11_32'}).json(); self.assertTrue(ok['compatible'])
        bad = self.c.post('/api/v1/products/%s/check-reuse' % pid, headers=self.M, json={'resolution_mm_per_pixel': 100000, 'units': {'slope': 'degrees'}, 'source': 'tycho_48', 'assumptions': ['calibrated thermal model']}).json(); self.assertFalse(bad['compatible']); self.assertEqual(len(bad['reasons']), 4)
        self.assertTrue(self.c.get('/api/v1/native/audit', headers=self.H).json()['conserved']); self.assertEqual(self.c.get('/api/v1/products/' + pid, headers=self.M).json()['entitlements'], {'sold': 2, 'delivered': 2})

    def test_external_custody_buyer_signs_the_purchase(self):
        mid, jid = self.computed_job(); pid = self.c.post('/api/v1/products', headers=self.R, json={'source_job': jid, 'price': 3, 'license': 'verification-service'}).json()['id']
        self.enroll('xbuyer', 100); self.fund('xbuyer', 10); h = self.actors['xbuyer']['h']
        e = self.c.post('/api/v1/products/%s/purchase' % pid, headers=h, json={'request_id': 'xb1'}).json(); self.assertEqual(e['state'], 'awaiting_signature'); self.assertEqual(self.bal('xbuyer'), 10)
        self.assertEqual(self.c.get('/api/v1/products/entitlements/%s/download' % e['id'], headers=h).status_code, 409)
        out = self.sign_all('xbuyer'); self.assertEqual(out[0]['status'], 200, out); self.assertEqual(self.bal('xbuyer'), 7)
        self.assertEqual(self.c.get('/api/v1/products/entitlements/' + e['id'], headers=h).json()['state'], 'purchased'); self.assertEqual(self.c.get('/api/v1/products/entitlements/%s/download' % e['id'], headers=h).status_code, 200)

    def test_signed_funding_spending_and_settlement_without_fallback(self):
        pass                                                   # inherited custody flows are exercised in their own suite

    def test_development_custody_can_be_disabled_for_a_scenario(self):
        pass


if __name__ == '__main__':
    unittest.main()
