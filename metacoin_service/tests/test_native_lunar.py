"""Lunar products on the native work path (order §11, §14 step 1; journeys 21, 23, 24, 25): a route-feasibility task on real
LOLA LDEM_4 data computed by the worker, verified by the independent lunar reference, accepted under frozen terms and
settled natively; a verified INFEASIBLE earns the contracted amount; a synthetic tile is labelled; products are inspectable."""
import base64
import json
import unittest

from metacoin_service.lunar import reference, sources
from metacoin_service.lunar.__main__ import example
from metacoin_service.tests.test_native_protocol import NativeBase


class NativeLunarTests(NativeBase):
    def lunar_terms(self, requester_h, template, price, bps=0, verifier=0, verifier_pay_to=None, funding='requester'):
        ceiling = price + price * bps // 10000 + verifier
        t = self.c.post('/api/v1/work/terms', headers=requester_h, json={'template': template, 'ceiling': ceiling, 'amount': price, 'asset': self.asset}); self.assertEqual(t.status_code, 201, t.text); t = t.json()
        pay = dict(t['terms']['payment'], scheme='exact', fee_policy={'schema': 'metacoin-fee-policy/v1', 'treasury_bps': bps, 'rounding': 'floor_fee_remainder_to_provider'}, funding=funding, verifier_compensation=verifier)
        if verifier:
            pay['verifier_pay_to'] = verifier_pay_to
        r = self.c.post('/api/v1/work/terms/' + t['id'], headers=requester_h, json={'terms': {'payment': pay}}); self.assertEqual(r.status_code, 200, r.text); return r.json()

    def lunar_award(self, requester_h, provider_h, template, inputs, price, **kw):
        t = self.lunar_terms(requester_h, template, price, **kw)
        f = self.c.post('/api/v1/work/terms/' + t['id'] + '/freeze', headers=requester_h, json={'inputs': inputs}); self.assertEqual(f.status_code, 200, f.text)
        r = self.c.post('/api/v1/work/requests', headers=requester_h, json={'terms_id': t['id']}).json(); self.c.post('/api/v1/work/requests/' + r['id'] + '/open', headers=requester_h, json={})
        o = self.c.post('/api/v1/work/requests/' + r['id'] + '/offers', headers=provider_h, json={'price_amount': price, 'asset': self.asset, 'scheme': 'exact', 'window_seconds': 3600, 'verification': {'class': 'analytical'}}); self.assertEqual(o.status_code, 201, o.text)
        a = self.c.post('/api/v1/work/requests/' + r['id'] + '/award', headers=requester_h, json={'offer_id': o.json()['id']}); self.assertEqual(a.status_code, 201, a.text + self.defects())
        return t, r, a.json()

    def provider_lunar(self, who):
        p = self.c.post('/api/v1/work/providers', headers=self.H, json={'name': who + '-lunar', 'principal_id': self.actors[who]['principal_id'], 'capabilities': {'kinds': ['terrain_tile', 'route_feasibility', 'scenario_pack', 'energy_audit'], 'verification_classes': ['analytical', 'full_exact'], 'payment_schemes': ['exact', 'upto']}})
        self.assertEqual(p.status_code, 201, p.text); return p.json()

    def test_route_on_real_lola_tile_computed_verified_accepted_and_paid_natively(self):
        R, M = self.actors['requester']['h'], self.actors['machine']['h']
        self.provider_lunar('machine'); self.fund('requester', 80)
        inputs = example('route', 'apollo11_32'); inputs['private_label'] = 'LUNAR_J21'
        t, r, a = self.lunar_award(R, M, 'lunar_route_feasibility', inputs, 40, bps=500, verifier=3, verifier_pay_to=self.actors['verifier']['account'])
        self.assertEqual(a['ceiling'], 45)
        d = self.deliver_accept(a['id'], R)
        self.assertEqual((d['decision'], d['payment_class'], d['payable_amount']), ('accepted', 'complete', 40))
        aw = self.c.get('/api/v1/work/awards/' + a['id'], headers=self.H).json()
        ms = aw['milestones'][0]; self.assertEqual(ms['dimensions'], {'execution': 'completed', 'science': 'FEASIBLE', 'acceptance': 'accepted', 'payment': 'payable'})
        # the verification record is the independent reference (not the producer implementation)
        jid = ms['attempts'][-1]['job_id']
        # product view: provenance of the real data, assumptions, preview, scope; the result agrees with the CLI reference check
        pv = self.c.get('/api/v1/lunar/products/' + jid, headers=self.H); self.assertEqual(pv.status_code, 200, pv.text); pv = pv.json()
        vs = pv['verifications']; self.assertEqual((vs[0]['class'], vs[0]['state']), ('analytical', 'passed')); self.assertIn('independent lunar reference', vs[0]['support'])
        self.assertEqual(pv['kind'], 'route_feasibility'); self.assertEqual(pv['provenance']['product_id'], 'LDEM_4'); self.assertIn('NASA PDS', pv['provenance']['label']); self.assertTrue(pv['assumptions'])
        self.assertEqual(pv['outcome'], 'FEASIBLE'); self.assertTrue(pv['reproduction']['command'])
        chk = reference.check_route(inputs, pv['result']); self.assertTrue(chk['ok'], chk)
        paid = self.pay_all(a['id'], R); self.assertEqual(sorted(paid), ['fee', 'provider', 'verifier'])
        self.assertEqual((self.bal('requester'), self.bal('machine'), self.bal('verifier'), self.bal(self.dom['accounts']['treasury'])), (35, 40, 3, 2))

    def test_verified_infeasible_earns_the_contract_and_synthetic_is_labelled(self):
        R, M = self.actors['requester']['h'], self.actors['machine']['h']
        self.provider_lunar('machine'); self.fund('requester', 30)
        inputs = example('route', 'tycho_48'); inputs['rover']['max_slope_milli_deg'] = 4000; inputs['private_label'] = 'LUNAR_J25'
        t, r, a = self.lunar_award(R, M, 'lunar_route_feasibility', inputs, 10)
        d = self.deliver_accept(a['id'], R); self.assertEqual((d['decision'], d['payable_amount']), ('accepted', 10))
        aw = self.c.get('/api/v1/work/awards/' + a['id'], headers=self.H).json(); self.assertEqual(aw['milestones'][0]['dimensions']['science'], 'INFEASIBLE')
        jid = aw['milestones'][0]['attempts'][-1]['job_id']; pv = self.c.get('/api/v1/lunar/products/' + jid, headers=self.H).json()
        self.assertEqual((pv['outcome'], pv['result']['witness']), ('INFEASIBLE', 'closure'))
        self.pay_all(a['id'], R); self.assertEqual(self.bal('machine'), 10)
        # a synthetic terrain tile product is labelled SYNTHETIC end to end and carries a preview
        syn = example('terrain'); syn['source'] = sources.synthetic(seed=5, lines=16, samples=16); syn['private_label'] = 'LUNAR_SYN'
        t2, r2, a2 = self.lunar_award(R, M, 'lunar_terrain_tile', syn, 5)
        d2 = self.deliver_accept(a2['id'], R); self.assertEqual(d2['decision'], 'accepted')
        aw2 = self.c.get('/api/v1/work/awards/' + a2['id'], headers=self.H).json(); jid2 = aw2['milestones'][0]['attempts'][-1]['job_id']
        pv2 = self.c.get('/api/v1/lunar/products/' + jid2, headers=self.H).json(); self.assertIn('SYNTHETIC', pv2['provenance']['label']); self.assertIsNone(pv2['provenance']['image_sha256'])
        png = self.c.get('/api/v1/lunar/products/' + jid2 + '/preview.png', headers=self.H); self.assertEqual(png.status_code, 200); self.assertTrue(png.content.startswith(b'\x89PNG'))
        # a viewer sees scope and provenance but not the private result grid
        V = self.inst.h('viewer'); pv3 = self.c.get('/api/v1/lunar/products/' + jid2, headers=V); self.assertEqual(pv3.status_code, 200); self.assertIsNone(pv3.json().get('result')); self.assertIn('SYNTHETIC', pv3.json()['provenance']['label'])

    def test_scenario_pack_downloadable_and_reproducible(self):
        R, M = self.actors['requester']['h'], self.actors['machine']['h']
        self.provider_lunar('machine'); self.fund('requester', 20)
        inputs = example('pack'); inputs['private_label'] = 'LUNAR_PACK'
        t, r, a = self.lunar_award(R, M, 'lunar_scenario_pack', inputs, 8)
        d = self.deliver_accept(a['id'], R); self.assertEqual(d['decision'], 'accepted')
        aw = self.c.get('/api/v1/work/awards/' + a['id'], headers=self.H).json(); jid = aw['milestones'][0]['attempts'][-1]['job_id']
        pv = self.c.get('/api/v1/lunar/products/' + jid, headers=self.H).json(); self.assertEqual(pv['outcome'], 'PACK_COMPLETE'); man = pv['result']['manifest']
        self.assertEqual(man['summary']['count'], 4); self.assertGreaterEqual(man['summary']['route_outcomes']['INFEASIBLE'], 1); self.assertGreaterEqual(man['summary']['route_outcomes']['FEASIBLE'], 1)
        z = self.c.get('/api/v1/lunar/products/' + jid + '/download', headers=self.H); self.assertEqual(z.status_code, 200); self.assertEqual(z.headers['content-type'], 'application/zip')
        import io, zipfile
        zf = zipfile.ZipFile(io.BytesIO(z.content)); names = zf.namelist()
        self.assertIn('manifest.json', names); self.assertIn('inputs.json', names); self.assertTrue(any(n.endswith('preview.png') for n in names)); self.assertTrue(any(n.endswith('route.json') for n in names))
        from metacoin_service.lunar import scenario
        rep = scenario.reproduce(json.loads(zf.read('inputs.json')), json.loads(zf.read('manifest.json'))); self.assertTrue(rep['ok'], rep)
        self.assertEqual(json.loads(zf.read('manifest.json'))['pack_id'], man['pack_id'])


if __name__ == '__main__':
    unittest.main()
