"""§21 extensions (order 2026-10-23): 1 multi-provider bidding under a declared deterministic policy with private offers and funded
ceilings; 2 explicit provider substitution for unstarted milestones with affected-party authorization; 3 shared route planning
across relay offers with independently checked exposure; 4 bounded prepaid subscriptions with stop/revoke and no renewal;
5 offline console from a signed export; 7 starter kit compatibility; 8 evidence-vs-delay scenario comparison under declared
assumptions."""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

from metacoin_service.lunar.__main__ import example
from metacoin_service.native import bidding as B, offline_console as OC, route_planning as RP, scenarios as SC
from metacoin_service.tests.test_native_missions import MissionBase
from metacoin_service.tests.test_service import ROOT


class PureKernels(unittest.TestCase):
    def test_bidding_policies_are_deterministic_and_bounded_by_the_ceiling(self):
        offers = [{'offer_id': 'o1', 'provider_id': 'p1', 'price_amount': 10, 'window_seconds': 100, 'sequence': 1}, {'offer_id': 'o2', 'provider_id': 'p2', 'price_amount': 12, 'window_seconds': 50, 'sequence': 2}]
        sel = B.validate({'policy': 'declared_preference', 'preference': ['p2'], 'min_bids': 2, 'bidding_window_seconds': 60})
        self.assertFalse(B.select(sel, offers[:1], 1000, 1010)['ready']); self.assertTrue(B.select(sel, offers[:1], 1000, 1061)['ready'])        # window closed: one bid suffices
        self.assertEqual(B.select(sel, offers, 1000, 1010)['chosen']['offer_id'], 'o2'); self.assertEqual(B.select(B.validate({'policy': 'lowest_price'}), offers, 0, 0)['chosen']['offer_id'], 'o1')
        self.assertEqual(B.select(B.validate({'policy': 'earliest_window'}), offers, 0, 0)['chosen']['offer_id'], 'o2'); self.assertEqual(B.select(sel, offers, 1000, 1010, ceiling=11)['chosen']['offer_id'], 'o1')
        self.assertFalse(B.select(sel, offers, 1000, 1010, ceiling=5)['ready']); self.assertRaises(ValueError, B.validate, {'policy': 'reputation'}); self.assertRaises(ValueError, B.validate, {'policy': 'declared_preference'})

    def test_route_planning_and_independent_exposure(self):
        os_ = [{'id': 'c%d' % i, 'version': 1, 'kind': 'contact', 'provider_account': 'na_%d' % i, 'price_per_unit': [1, 2, 3][i % 3], 'window_start': 0, 'window_end': 10 ** 9, 'available': 1000, 'bandwidth_bytes_per_s': 10 ** 6} for i in range(5)]
        pl = RP.plan(os_, 3000, 3, 100, 500, recipient_account='na_1'); best = pl['routes'][0]
        self.assertEqual(best['total_fee'], 3); self.assertNotIn('na_1', [h['relay_account'] for r in pl['routes'] for h in r['hops']])      # the recipient never relays its own message
        self.assertTrue(all(r['hop_count'] <= 3 for r in pl['routes'])); self.assertTrue(RP.check_exposure(best['hops'], os_, 3000, 100)['ok'])
        bad = RP.check_exposure(best['hops'] + best['hops'], os_, 3000, 100); self.assertIn('relay_account_repeated', [p['code'] for p in bad['problems']])
        self.assertFalse(RP.check_exposure(best['hops'], os_, 3000, 2)['ok']); self.assertRaises(ValueError, RP.plan, os_, 10, 5, 100, 500)
        self.assertEqual(RP.plan(os_, 3000, 2, 2, 500)['routes'], [])                                                                       # nothing under the ceiling: no route, nothing reserved

    def test_scenarios_label_estimates_and_find_the_breakeven(self):
        a = {'p_feasible_bps': 6000, 'evidence_price': 10, 'evidence_delay_slots': 2, 'evidence_reliability_bps': 9000, 'followon_price': 40, 'followon_delay_slots': 3, 'delay_cost_per_slot': 1, 'deadline_slots': 10, 'value_if_feasible': 100, 'loss_if_infeasible_proceeded': 60}
        r = SC.compare(a); self.assertEqual(r['recommended_under_assumptions'], 'buy_evidence_then_decide'); self.assertTrue(all('not a guarantee' in row['label'] for row in r['rows']))
        r2 = SC.compare(dict(a, p_feasible_bps=9900)); self.assertEqual(r2['recommended_under_assumptions'], 'proceed_without_evidence'); self.assertIsNotNone(r['breakeven_p_feasible_bps_for_proceeding'])
        self.assertRaises(ValueError, SC.compare, dict(a, p_feasible_bps=1.5)); self.assertRaises(ValueError, SC.compare, {k: v for k, v in a.items() if k != 'deadline_slots'})

    def test_starter_kit_agrees_with_the_conformance_vectors(self):
        p = subprocess.run([sys.executable, str(ROOT / 'provider_client' / 'starter_kit' / 'compat_test.py')], capture_output=True, text=True, timeout=120); out = json.loads(p.stdout)
        self.assertEqual(p.returncode, 0, out.get('failed')); self.assertTrue(out['ok']); self.assertGreaterEqual(out['cases'], 20)


class ExtensionServiceTests(MissionBase):
    def setUp(self):
        super().setUp(); self.fund('requester', 2000)
        a = self.c.post('/api/v1/native/actors', headers=self.H, json={'name': 'rival'}).json(); self.actors['rival'] = {'account': a['account_id'], 'principal_id': a['principal_id'], 'h': {'Authorization': 'Bearer ' + a['credential']['token']}}
        self.rival = self.c.post('/api/v1/work/providers', headers=self.H, json={'name': 'rival-provider', 'principal_id': a['principal_id'], 'capabilities': {'kinds': ['terrain_tile', 'route_feasibility'], 'verification_classes': ['analytical', 'full_exact'], 'payment_schemes': ['exact']}}).json()

    def work_graph(self, selection=None, extra=None):
        spec = dict({'template': 'lunar_route_feasibility', 'inputs': dict(example('route'), private_label='EXT'), 'price': 20, 'bps': 0, 'verifier': 0}, **(extra or {}))
        if selection:
            spec['selection'] = selection
        return {'milestones': [{'key': 'job', 'kind': 'work', 'depends': [], 'spec': spec}, {'key': 'pay', 'kind': 'settle', 'depends': ['job'], 'spec': {'of': 'job'}}]}

    def offer(self, h, rid, price):
        r = self.c.post('/api/v1/work/requests/' + rid + '/offers', headers=h, json={'price_amount': price, 'asset': self.asset, 'scheme': 'exact', 'window_seconds': 3600, 'verification': {'class': 'analytical'}}); self.assertEqual(r.status_code, 201, r.text); return r.json()

    def test_bidding_waits_for_the_window_then_selects_by_the_declared_policy(self):
        m = self.mission(graph=self.work_graph({'policy': 'lowest_price', 'min_bids': 2, 'bidding_window_seconds': 3600}), template=None); mid = m['id']
        s = self.step(mid); self.assertEqual(s['performed']['action'], 'open_request'); rid = self.ms(mid, 'job')['binding']['request_id']
        o1 = self.offer(self.M, rid, 18); s = self.step(mid); self.assertIsNone(s['performed']); self.assertIn('bidding', (s.get('wait_reason') or '') + json.dumps(s.get('rejected')))   # one bid, window open: nothing awarded
        o2 = self.offer(self.actors['rival']['h'], rid, 15)
        rv = self.c.get('/api/v1/work/requests/' + rid, headers=self.actors['rival']['h']).json(); self.assertEqual([o['id'] for o in rv['offers']], [o2['id']])                 # private: a provider sees only its own offer
        s = self.step(mid); self.assertEqual((s['performed']['action'], s['performed']['offer_id']), ('award', o2['id'])); b = self.ms(mid, 'job')['binding']; self.assertEqual(b['selection']['policy'], 'lowest_price'); self.assertEqual([x['offer_id'] for x in b['selection']['ranking']], [o2['id'], o1['id']])
        # declared preference beats price within the ceiling; an offer above the funded ceiling is never chosen
        m2 = self.mission(graph=self.work_graph({'policy': 'declared_preference', 'preference': [self.prov['id']], 'min_bids': 1}), template=None); mid2 = m2['id']; self.step(mid2); rid2 = self.ms(mid2, 'job')['binding']['request_id']
        self.offer(self.actors['rival']['h'], rid2, 10); self.offer(self.M, rid2, 19); s = self.step(mid2); self.assertEqual(s['result']['provider_id'], self.prov['id'])
        m3 = self.mission(graph=self.work_graph({'policy': 'lowest_price', 'min_bids': 1}), template=None); mid3 = m3['id']; self.step(mid3); rid3 = self.ms(mid3, 'job')['binding']['request_id']
        self.offer(self.actors['rival']['h'], rid3, 25); s = self.step(mid3); self.assertIsNone(s['performed'])
        self.assertEqual(self.c.post('/api/v1/missions', headers=self.R, json={'title': 'bad', 'graph': self.work_graph({'policy': 'reputation'}), 'policy': {}}).json()['detail']['code'], 'selection')

    def test_substitution_needs_every_affected_party_and_leaves_awarded_work_alone(self):
        m = self.mission(graph=self.work_graph(extra={'permitted_providers': [self.prov['id']]}), template=None); mid = m['id']
        r = self.c.post('/api/v1/missions/%s/substitute' % mid, headers=self.R, json={'milestone': 'job', 'provider_id': self.rival['id'], 'reason': 'machine unavailable'}); self.assertEqual(r.status_code, 200, r.text); sub = r.json()
        self.assertEqual(sub['to_provider'], self.rival['id']); self.assertIn(self.actors['machine']['principal_id'], sub['affected_parties']); self.assertIn(self.actors['rival']['principal_id'], sub['affected_parties'])
        self.assertEqual(self.view(mid)['frozen_revision'], 1)                                                                                 # not active until the affected parties authorize
        for h in (self.M, self.actors['rival']['h']):
            self.assertEqual(self.c.post('/api/v1/missions/%s/revisions/%d/authorize' % (mid, sub['revision']), headers=h, json={}).status_code, 200)
        self.assertEqual(self.view(mid)['frozen_revision'], 2); s = self.step(mid); rid = self.ms(mid, 'job')['binding']['request_id']
        self.offer(self.actors['rival']['h'], rid, 20); s = self.step(mid); self.assertEqual((s.get('result') or {}).get('provider_id'), self.rival['id'], json.dumps(s)[:600])
        self.assertEqual(self.c.post('/api/v1/missions/%s/substitute' % mid, headers=self.R, json={'milestone': 'job', 'provider_id': self.prov['id']}).json()['detail']['code'], 'milestone_committed')   # awarded work keeps its provider
        self.assertEqual(self.c.post('/api/v1/missions/%s/substitute' % mid, headers=self.M, json={'milestone': 'job', 'provider_id': self.prov['id']}).status_code, 403)

    def test_relay_plan_route_then_create_it(self):
        self.fund('requester', 500); t = int(time.time()); accts = {}
        for i, (name, price) in enumerate((('supplier', 1), ('rival', 2), ('machine', 3))):
            r = self.c.post('/api/v1/resources/offers', headers=self.actors[name]['h'], json={'kind': 'contact', 'resource_id': 'rp-%d' % i, 'capacity': 10000, 'price_per_unit': price, 'window_start': t - 1, 'window_end': t + 3600, 'bandwidth_bytes_per_s': 5_000_000}); self.assertEqual(r.status_code, 201, r.text)
        pl = self.c.post('/api/v1/relay/plan', headers=self.R, json={'bytes': 3000, 'recipient_account': self.actors['verifier']['account'], 'max_hops': 2, 'fee_ceiling': 20}); self.assertEqual(pl.status_code, 200, pl.text); pl = pl.json()
        best = pl['routes'][0]; self.assertEqual(best['hop_count'], 1); self.assertEqual(best['total_fee'], 3)
        two = next(r for r in pl['routes'] if r['hop_count'] == 2); chk = self.c.post('/api/v1/relay/plan', headers=self.R, json={'bytes': 3000, 'fee_ceiling': 20, 'route_hops': two['hops']}).json(); self.assertTrue(chk['ok']); self.assertEqual(chk['total_exposure'], two['total_fee'])
        self.assertFalse(self.c.post('/api/v1/relay/plan', headers=self.R, json={'bytes': 3000, 'fee_ceiling': 5, 'route_hops': two['hops']}).json()['ok'])
        rt = self.c.post('/api/v1/relay/routes', headers=self.R, json={'message_id': 'planned', 'recipient_account': self.actors['verifier']['account'], 'bytes': 3000, 'hops': [{k: h[k] for k in ('offer_id', 'offer_version', 'relay_account')} for h in two['hops']], 'fee_ceiling': 20, 'dispute_policy': 'receiver_ack_required'}); self.assertEqual(rt.status_code, 201, rt.text); self.assertEqual(rt.json()['fees_bound'], two['total_fee'])

    def test_subscription_periods_are_prepaid_bounded_and_stoppable_without_renewal(self):
        start = int(time.time()); r = self.c.post('/api/v1/subscriptions', headers=self.R, json={'provider_id': self.prov['id'], 'template': 'lunar_route_feasibility', 'inputs': dict(example('route'), private_label='SUBS'), 'price': 10, 'periods': 3, 'period_seconds': 3600, 'start_at': start}); self.assertEqual(r.status_code, 201, r.text); sub = r.json(); mid = sub['mission']
        self.assertEqual((len(sub['periods']), sub['renewal'], sub['ledger']['ceiling']), (3, 'none', 30))
        s = self.step(mid); self.assertEqual((s['performed']['action'], s['performed']['milestone']), ('open_request', 'period_1')); rid = self.ms(mid, 'period_1')['binding']['request_id']; self.offer(self.M, rid, 10); s = self.step(mid); self.assertEqual(s['performed']['action'], 'award')
        s = self.step(mid); self.assertIsNone(s['performed'] if s['performed'] and s['performed']['milestone'] == 'period_2' else None)       # period 2 waits for its not_before
        ex = self.c.get('/api/v1/missions/%s/explain' % mid, headers=self.R).json(); self.assertTrue(any('not_before' in (a.get('reason') or '') for a in ex['actions']))
        for _ in range(3):
            self.w.run_once()
        self.step(mid)
        for _ in range(3):
            self.w.run_once()
        self.step(mid); s = self.step(mid); self.assertEqual(s['performed']['action'], 'pay'); paid = self.bal('machine')
        st = self.c.post('/api/v1/subscriptions/%s/stop' % mid, headers=self.R, json={'reason': 'enough'}); self.assertEqual(st.status_code, 200, st.text); st = st.json(); self.assertEqual((st['stopped'], st['cancelled'], st['delivered']), (2, 2, 1))
        self.assertEqual(st['ledger']['ceiling'], 10, json.dumps(st)[:800]); self.assertEqual(self.bal('machine'), paid); self.assertEqual(self.view(mid)['frozen_revision'], 2)
        # a provider revokes another subscription: unstarted periods cancelled, nothing taken from either side
        r2 = self.c.post('/api/v1/subscriptions', headers=self.R, json={'provider_id': self.prov['id'], 'template': 'lunar_route_feasibility', 'inputs': dict(example('route'), private_label='SUBS2'), 'price': 10, 'periods': 2, 'period_seconds': 3600, 'start_at': start + 7200}).json()
        self.assertEqual(self.c.post('/api/v1/subscriptions/%s/revoke' % r2['mission'], headers=self.actors['rival']['h'], json={}).status_code, 403)
        rv = self.c.post('/api/v1/subscriptions/%s/revoke' % r2['mission'], headers=self.M, json={'reason': 'capacity gone'}); self.assertEqual(rv.status_code, 200, rv.text); self.assertEqual((rv.json()['stopped'], rv.json()['remaining']), (2, 0))
        self.assertTrue(self.c.get('/api/v1/native/audit', headers=self.H).json()['conserved'])

    def test_offline_console_renders_a_verified_export_with_freshness(self):
        ex = self.c.get('/api/v1/native/export', headers=self.H).json(); d = self.c.get('/api/v1/native/domain', headers=self.H).json(); roots = {d['id']: [{'key_id': d['authority']['key_id'], 'public_hex': d['authority']['public_hex'], 'epoch': d['authority']['epoch']}]}
        page, rep = OC.render(ex, roots); self.assertIn('Freshness', page); self.assertIn('no live connection', page); self.assertIn('Independent checker', page); self.assertTrue(rep['summary']['authorized_bookkeeping'])
        with tempfile.TemporaryDirectory() as tmp:
            json.dump(ex, open(os.path.join(tmp, 'e.json'), 'w')); json.dump(roots, open(os.path.join(tmp, 'r.json'), 'w'))
            p = subprocess.run([sys.executable, '-m', 'metacoin_service.native.offline_console', os.path.join(tmp, 'e.json'), '--roots', os.path.join(tmp, 'r.json'), '--out', os.path.join(tmp, 'page.html')], capture_output=True, text=True, cwd=str(ROOT), env=dict(os.environ, PYTHONPATH=str(ROOT))); self.assertEqual(p.returncode, 0, p.stderr[-300:]); self.assertGreater(os.path.getsize(os.path.join(tmp, 'page.html')), 1000)

    def test_scenarios_route(self):
        a = {'p_feasible_bps': 6000, 'evidence_price': 10, 'evidence_delay_slots': 2, 'evidence_reliability_bps': 9000, 'followon_price': 40, 'followon_delay_slots': 3, 'delay_cost_per_slot': 1, 'deadline_slots': 10, 'value_if_feasible': 100, 'loss_if_infeasible_proceeded': 60}
        r = self.c.post('/api/v1/portfolio/scenarios', headers=self.R, json={'assumptions': a}); self.assertEqual(r.status_code, 200, r.text); self.assertIn('not a guarantee', r.json()['rows'][0]['label'])
        self.assertEqual(self.c.post('/api/v1/portfolio/scenarios', headers=self.R, json={'assumptions': {'p_feasible_bps': 5}}).status_code, 422)


if __name__ == '__main__':
    unittest.main()
