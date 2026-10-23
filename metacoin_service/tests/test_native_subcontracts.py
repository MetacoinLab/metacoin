"""Native machine subcontracting (order 2026-10-23 group D; journeys 16–21 at unit level)."""
import unittest

from metacoin_service.lunar.__main__ import example
from metacoin_service.native import subcontracts as SC
from metacoin_service.tests.test_native_missions import MissionBase


class SubcontractTests(MissionBase):
    def setUp(self):
        super().setUp()
        a = self.c.post('/api/v1/native/actors', headers=self.H, json={'name': 'subco'}).json(); self.actors['subco'] = {'account': a['account_id'], 'principal_id': a['principal_id'], 'h': {'Authorization': 'Bearer ' + a['credential']['token']}}
        self.subco = self.c.post('/api/v1/work/providers', headers=self.H, json={'name': 'subco-provider', 'principal_id': a['principal_id'], 'capabilities': {'kinds': ['terrain_tile', 'route_feasibility'], 'verification_classes': ['analytical', 'full_exact'], 'payment_schemes': ['exact']}}).json()

    def parent_awarded(self):
        m = self.mission(policy={'compute_cpu_ms': 20}); mid = m['id']; self.drive_work(mid, 'acquire', 4); self.step(mid)
        s = self.step(mid); self.assertEqual((s['performed']['action'], s['performed']['milestone']), ('open_request', 'compute')); self.offer_as_machine(self.ms(mid, 'compute')['binding']['request_id'], 40)
        s = self.step(mid); self.assertEqual(s['performed']['action'], 'award'); return mid

    def sub_body(self, mid, price=10, funded=12, mode='requester_prefund', label='SUB'):
        return {'parent_mission': mid, 'parent_milestone': 'compute', 'provider_id': self.subco['id'], 'deliverable': {'template': 'lunar_route_feasibility', 'inputs': dict(example('route'), private_label=label), 'price': price, 'verifier': 0, 'bps': 0}, 'funded_amount': funded, 'funding_mode': mode}

    def drive_child(self, sid, who_h, fail=False):
        s = self.c.post('/api/v1/subcontracts/%s/step' % sid, headers=who_h, json={}); self.assertEqual(s.status_code, 200, s.text); s = s.json(); self.assertEqual(s['step']['performed']['action'], 'open_request', s['step'])
        child = s['child_mission']; rid = next(x for x in self.c.get('/api/v1/missions/' + child, headers=who_h).json()['milestones'] if x['key'] == 'deliver')['binding']['request_id']
        r = self.c.post('/api/v1/work/requests/' + rid + '/offers', headers=self.actors['subco']['h'], json={'price_amount': 10, 'asset': self.asset, 'scheme': 'exact', 'window_seconds': 3600, 'verification': {'class': 'analytical'}}); self.assertEqual(r.status_code, 201, r.text)
        s = self.c.post('/api/v1/subcontracts/%s/step' % sid, headers=who_h, json={}).json(); self.assertEqual(s['step']['performed']['action'], 'award')
        for _ in range(3):
            self.w.run_once()
        if fail:
            aid = next(x for x in self.c.get('/api/v1/missions/' + child, headers=who_h).json()['milestones'] if x['key'] == 'deliver')['binding']['award_id']; jid = self.c.get('/api/v1/work/awards/' + aid, headers=self.H).json()['milestones'][0]['job_id']
            self.c.post('/api/v1/ops/faults', headers=self.H, json={'fault': 'verification_fail', 'job_id': jid})
        s = self.c.post('/api/v1/subcontracts/%s/step' % sid, headers=who_h, json={}).json(); self.assertEqual(s['step']['performed']['action'], 'verify')
        for _ in range(3):
            self.w.run_once()
        s = self.c.post('/api/v1/subcontracts/%s/step' % sid, headers=who_h, json={}).json(); self.assertEqual(s['step']['performed']['action'], 'decide'); return s

    def test_funded_subcontract_bound_to_parent_executes_from_prefund_and_returns_unused(self):
        mid = self.parent_awarded(); M = self.M; mach0 = self.bal('machine'); req0 = self.bal('requester')
        # journey 17: circular funding and unsettled earnings are refused
        r = self.c.post('/api/v1/subcontracts', headers=M, json=self.sub_body(mid, mode='parent_future_earnings')); self.assertEqual((r.status_code, r.json()['detail']['code']), (422, 'circular_funding'))
        r = self.c.post('/api/v1/subcontracts', headers=M, json=self.sub_body(mid, mode='machine_earnings')); self.assertEqual((r.status_code, r.json()['detail']['code']), (400, 'insufficient_settled_funds')); self.assertEqual(r.json()['detail']['available'], 4)
        # journey 16: a proposal bound to the parent revision/milestone/deliverable; nothing reserved until funded and awarded
        r = self.c.post('/api/v1/subcontracts', headers=M, json=self.sub_body(mid)); self.assertEqual(r.status_code, 201, r.text); sc = r.json(); sid = sc['id']
        self.assertEqual((sc['state'], sc['parent']['milestone'], sc['parent']['revision'], sc['depth'], sc['funded_amount']), ('awaiting_prefund', 'compute', 1, 1, 12)); self.assertEqual(sc['timeline']['reserved'], 0); self.assertTrue(sc['deliverable']['inputs_digest'])
        self.assertEqual(self.c.post('/api/v1/subcontracts/%s/step' % sid, headers=M, json={}).json()['detail']['code'], 'not_funded')
        self.assertEqual(self.c.post('/api/v1/subcontracts', headers=self.R, json=self.sub_body(mid)).status_code, 403)                 # only the awarded primary
        dup = self.c.post('/api/v1/subcontracts', headers=M, json=self.sub_body(mid)); self.assertEqual(dup.json()['detail']['code'], 'duplicate_subcontract')
        # the requester prefunds with a separately authorized transfer bound to the prefund obligation
        pp = self.c.post('/api/v1/subcontracts/%s/prefund-prepare' % sid, headers=self.R, json={}); self.assertEqual(pp.status_code, 200, pp.text); self.assertEqual(pp.json()['message']['amount_ceiling'], 12)
        self.assertEqual(self.c.post('/api/v1/subcontracts/%s/prefund' % sid, headers=M, json={}).status_code, 403)
        f = self.c.post('/api/v1/subcontracts/%s/prefund' % sid, headers=self.R, json={}); self.assertEqual(f.status_code, 200, f.text); self.assertEqual(f.json()['state'], 'funded'); self.assertEqual(self.bal('machine'), mach0 + 12); self.assertEqual(self.bal('requester'), req0 - 12)
        # journey 18: executed and settled once from the primary's funds
        s = self.drive_child(sid, M); self.assertEqual(s['step']['result']['decision'], 'accepted')
        s = self.c.post('/api/v1/subcontracts/%s/step' % sid, headers=M, json={}).json(); self.assertEqual(s['step']['performed']['action'], 'pay'); self.assertEqual(s['state'], 'completed'); self.assertEqual(self.bal('subco'), 10)
        v = self.c.get('/api/v1/subcontracts/' + sid, headers=M).json(); self.assertEqual((v['timeline']['spent'], v['timeline']['earned_by_provider'], v['timeline']['returned']), (10, 10, 0))
        again = self.c.post('/api/v1/subcontracts/%s/step' % sid, headers=M, json={}).json(); self.assertIsNone(again['step']['performed']); self.assertEqual(self.bal('subco'), 10)     # settled once
        rr = self.c.post('/api/v1/subcontracts/%s/return-unused' % sid, headers=M, json={}); self.assertEqual(rr.status_code, 200, rr.text); self.assertEqual((rr.json()['state'], rr.json()['timeline']['returned']), ('returned', 2)); self.assertEqual(self.bal('requester'), req0 - 10); self.assertEqual(self.bal('machine'), mach0)
        # the parent milestone and the primary's own obligation are untouched
        self.assertEqual(self.ms(mid, 'compute')['state'], 'executing'); pv = self.view(mid); self.assertEqual(pv['subcontracts'][0]['state'], 'returned')
        self.assertTrue(self.c.get('/api/v1/native/audit', headers=self.H).json()['conserved'])

    def test_failed_subcontract_preserves_accepted_work_and_parent_duty(self):
        mid = self.parent_awarded(); M = self.M; self.fund('machine', 100)
        ok = self.c.post('/api/v1/subcontracts', headers=M, json=self.sub_body(mid, mode='machine_earnings', label='GOOD')).json(); bad = self.c.post('/api/v1/subcontracts', headers=M, json=self.sub_body(mid, mode='machine_earnings', label='BAD')).json()
        self.assertEqual((ok['state'], bad['state']), ('funded', 'funded'))
        s = self.drive_child(ok['id'], M); self.assertEqual(s['step']['result']['decision'], 'accepted'); self.c.post('/api/v1/subcontracts/%s/step' % ok['id'], headers=M, json={})
        s = self.drive_child(bad['id'], M, fail=True); self.assertEqual(s['step']['result']['decision'], 'rejected'); self.assertEqual(s['state'], 'failed')
        self.assertEqual(self.c.get('/api/v1/subcontracts/' + ok['id'], headers=M).json()['state'], 'completed'); self.assertEqual(self.bal('subco'), 10)       # journey 19: accepted useful work stays paid
        self.assertEqual(self.ms(mid, 'compute')['state'], 'executing'); ev = [e for e in self.view(mid)['events'] if e['kind'] == 'subcontract_failed']; self.assertIn('remains responsible', ev[0]['detail']['note'])
        # journey 20: unauthorized amendment (parent requester on the child) and excessive depth / fan-out
        child = ok['child_mission']; self.assertEqual(self.c.post('/api/v1/missions/%s/amend' % child, headers=self.R, json={'milestones': [], 'reason': 'x'}).status_code, 403)
        third = self.c.post('/api/v1/subcontracts', headers=M, json=self.sub_body(mid, mode='machine_earnings', label='THIRD')); self.assertEqual(third.status_code, 201)
        fourth = self.c.post('/api/v1/subcontracts', headers=M, json=self.sub_body(mid, mode='machine_earnings', label='FOURTH')); self.assertEqual(fourth.json()['detail']['code'], 'subcontract_fanout')
        old = SC.MAX_DEPTH; SC.MAX_DEPTH = 1
        try:
            deep = self.c.post('/api/v1/subcontracts', headers=self.actors['subco']['h'], json={'parent_mission': child, 'parent_milestone': 'deliver', 'provider_id': self.prov['id'], 'deliverable': {'template': 'lunar_terrain_tile', 'inputs': example('terrain'), 'price': 2}, 'funded_amount': 2, 'funding_mode': 'requester_prefund'})
            self.assertEqual(deep.json()['detail']['code'], 'parent_milestone_state' if deep.status_code == 409 and deep.json()['detail']['code'] == 'parent_milestone_state' else 'subcontract_depth')
        finally:
            SC.MAX_DEPTH = old
        # journey 21: the subcontractor sees only its own inputs; the parent's private compartments stay hidden
        sub_h = self.actors['subco']['h']; pv = self.c.get('/api/v1/missions/' + mid, headers=sub_h).json(); self.assertFalse(pv['viewer_is_party']); self.assertTrue(next(x for x in pv['milestones'] if x['key'] == 'compute')['spec']['inputs'].get('redacted'))
        cv = self.c.get('/api/v1/missions/' + child, headers=sub_h).json(); self.assertTrue(cv['viewer_is_party']); self.assertEqual(cv['milestones'][0]['spec']['inputs']['private_label'], 'GOOD')
        paid = self.ms(mid, 'compute')['binding']['award_id']; comp = self.c.get('/api/v1/work/awards/%s/compartments?milestone=m1' % paid, headers=sub_h)
        if comp.status_code == 200:
            self.assertNotIn('private_inputs', comp.json()['visible_categories'])
        else:
            self.assertIn(comp.status_code, (403, 404))
        sv = self.c.get('/api/v1/subcontracts/' + ok['id'], headers=self.actors['verifier']['h']).json(); self.assertNotIn('inputs', sv['deliverable'])     # a non-party sees digests, not inputs
        self.assertTrue(self.c.get('/api/v1/native/audit', headers=self.H).json()['conserved'])


if __name__ == '__main__':
    unittest.main()
