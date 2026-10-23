"""Mission contracts and the bounded controller (order 2026-10-23 groups C, D; journeys 13–21): frozen terms with linked inputs,
partial delivery, valid negatives, authorized amendments, earn-then-buy, minimum reserve and unsettled revenue, model proposals
through the deterministic validator, blackout waits and learned revocation."""
import json
import time
import unittest

from metacoin_service.tests.test_native_protocol import NativeBase


class MissionBase(NativeBase):
    def setUp(self):
        super().setUp(); self.inst.settings.limits['test_hooks'] = 1; self.fund('requester', 200)
        self.R, self.M, self.S, self.V = (self.actors[k]['h'] for k in ('requester', 'machine', 'supplier', 'verifier'))
        self.prov = self.c.post('/api/v1/work/providers', headers=self.H, json={'name': 'machine-provider', 'principal_id': self.actors['machine']['principal_id'], 'capabilities': {'kinds': ['terrain_tile', 'route_feasibility', 'scenario_pack', 'energy_audit', 'evidence_acquisition'], 'verification_classes': ['analytical', 'full_exact'], 'payment_schemes': ['exact', 'upto']}}).json()
        self.sup = self.c.post('/api/v1/work/providers', headers=self.H, json={'name': 'supplier-provider', 'principal_id': self.actors['supplier']['principal_id'], 'capabilities': {'kinds': ['energy_audit', 'evidence_acquisition'], 'verification_classes': ['analytical', 'full_exact'], 'payment_schemes': ['exact']}}).json()
        t = int(time.time())
        self.c.post('/api/v1/resources/offers', headers=self.S, json={'kind': 'compute', 'resource_id': 'cpu-1', 'capacity': 100000, 'price_per_unit': 1, 'window_start': t - 1, 'window_end': t + 7200, 'energy_mwh_per_unit': 1})

    def mission(self, template='terrain_delivery', policy=None, **kw):
        pol = dict({'verifier': 3, 'verifier_pay_to': self.actors['verifier']['account'], 'permitted_providers': [self.prov['id']]}, **(policy or {}))
        r = self.c.post('/api/v1/missions', headers=self.R, json=dict({'title': template, 'template': template, 'policy': pol}, **kw)); self.assertEqual(r.status_code, 201, r.text + self.defects()); m = r.json()
        f = self.c.post('/api/v1/missions/' + m['id'] + '/freeze', headers=self.R, json={}); self.assertEqual(f.status_code, 200, f.text); return f.json()

    def step(self, mid, h=None, expect=200):
        r = self.c.post('/api/v1/missions/' + mid + '/step', headers=h or self.R, json={}); self.assertEqual(r.status_code, expect, r.text + self.defects()); return r.json()

    def view(self, mid, h=None):
        return self.c.get('/api/v1/missions/' + mid, headers=h or self.R).json()

    def ms(self, mid, key):
        return next(x for x in self.view(mid)['milestones'] if x['key'] == key)

    def offer_as_machine(self, request_id, price, cls='analytical'):
        r = self.c.post('/api/v1/work/requests/' + request_id + '/offers', headers=self.M, json={'price_amount': price, 'asset': self.asset, 'scheme': 'exact', 'window_seconds': 3600, 'verification': {'class': cls}}); self.assertEqual(r.status_code, 201, r.text); return r.json()

    def drive_work(self, mid, key, price, h=None):
        """open_request -> machine offers -> award -> worker -> verify -> worker -> decide (each step is the controller's choice)."""
        s = self.step(mid, h); self.assertEqual((s['performed']['action'], s['performed']['milestone']), ('open_request', key), s)
        self.offer_as_machine(self.ms(mid, key)['binding']['request_id'], price)
        s = self.step(mid, h); self.assertEqual(s['performed']['action'], 'award', s)
        for _ in range(3):
            self.w.run_once()
        s = self.step(mid, h); self.assertEqual(s['performed']['action'], 'verify', s)
        for _ in range(3):
            self.w.run_once()
        s = self.step(mid, h); self.assertEqual(s['performed']['action'], 'decide', s); return s


class MissionTests(MissionBase):
    def test_multi_milestone_frozen_terms_linked_inputs_partial_and_followon(self):
        m = self.mission(policy={'compute_cpu_ms': 20}); mid = m['id']
        self.assertEqual((m['state'], m['frozen_revision'], [x['state'] for x in m['milestones']]), ('frozen', 1, ['ready', 'pending', 'pending', 'pending', 'pending', 'pending']))
        self.assertTrue(m['revisions'][0]['digest'])
        s = self.drive_work(mid, 'acquire', 4); self.assertEqual(s['result']['decision'], 'accepted'); acq = self.ms(mid, 'acquire'); self.assertTrue(acq['binding']['terms_digest']); self.assertTrue(acq['binding']['input_root'])
        s = self.step(mid); self.assertEqual((s['performed']['action'], s['performed']['milestone']), ('pay', 'settle_acquire')); self.assertEqual(self.bal('machine'), 4)
        s = self.drive_work(mid, 'compute', 40); self.assertEqual((s['result']['decision'], s['result']['science']), ('accepted', 'FEASIBLE'))
        # journey 17: the machine cannot buy its follow-on before settlement (receivable only)
        ex = self.c.get('/api/v1/missions/' + mid + '/explain', headers=self.M).json(); fo = next(a for a in ex['actions'] if a['milestone'] == 'followon' and a['action'] == 'reserve'); self.assertFalse(fo['permitted']); self.assertIn('insufficient_liquidity', fo['reason']); self.assertIn('receivables 40', fo['reason'])
        s = self.step(mid, self.M); self.assertIsNone(s['performed']); self.assertEqual(s['state'], 'waiting'); self.assertIn('insufficient_liquidity', s['wait_reason'])
        # the optional storage milestone has no offer: it waits without blocking settlement (journey 14 partial delivery semantics)
        s = self.step(mid); self.assertEqual(s['performed']['action'], 'pay', s); paid = s['result']['paid']; self.assertEqual(sorted(p['rail'] for p in paid), ['native'] * 3)
        self.assertEqual((self.bal('machine'), self.bal('verifier'), self.bal(self.dom['accounts']['treasury'])), (44, 3, 2))
        led = self.c.get('/api/v1/missions/' + mid + '/ledger', headers=self.R).json(); self.assertEqual((led['totals']['spent'], led['owner_account']['reserved']), (49, 0))
        # journey 17 (after): the machine now buys compute from its settled earnings as the provider actor
        s = self.step(mid, self.M); self.assertEqual(s['performed']['action'], 'reserve', s); self.assertEqual((self.bal('machine', 'reserved'), self.bal('machine')), (20, 24))
        st = self.view(mid)['state']; self.assertIn(st, ('running', 'waiting'))
        hist = [e['kind'] for e in self.view(mid)['events']]; self.assertIn('action', hist); self.assertIn('wait', hist)

    def test_mandatory_failure_stops_dependents_valid_negative_pays(self):
        # valid negative (journey 15): a strict slope limit on Tycho makes the route INFEASIBLE; the frozen policy pays it
        from metacoin_service.lunar.__main__ import example
        route = example('route', 'tycho_48'); route['rover']['max_slope_milli_deg'] = 4000; route['private_label'] = 'NEG'
        graph = {'milestones': [{'key': 'compute', 'kind': 'work', 'depends': [], 'spec': {'template': 'lunar_route_feasibility', 'inputs': route, 'price': 10, 'bps': 0, 'verifier': 0, 'permitted_providers': [self.prov['id']], 'verification_class': 'analytical'}}, {'key': 'settle', 'kind': 'settle', 'depends': ['compute'], 'spec': {'of': 'compute'}}, {'key': 'extra', 'kind': 'work', 'depends': ['compute'], 'spec': {'template': 'lunar_terrain_tile', 'inputs': example('terrain'), 'price': 2, 'bps': 0, 'verifier': 0}}]}
        m = self.mission(graph=graph, template=None); mid = m['id']
        s = self.drive_work(mid, 'compute', 10); self.assertEqual((s['result']['decision'], s['result']['science']), ('accepted', 'INFEASIBLE'))
        s = self.step(mid); self.assertEqual(s['performed']['action'], 'pay'); self.assertEqual(self.bal('machine'), 10)
        # journey 14: a mandatory milestone that fails stops dependents; the accepted work stays paid
        graph2 = {'milestones': [{'key': 'first', 'kind': 'work', 'depends': [], 'spec': {'template': 'lunar_terrain_tile', 'inputs': dict(example('terrain'), private_label='FIRST'), 'price': 3, 'bps': 0, 'verifier': 0}}, {'key': 'bad', 'kind': 'work', 'depends': ['first'], 'spec': {'template': 'lunar_route_feasibility', 'inputs': dict(example('route'), private_label='BAD'), 'price': 5, 'bps': 0, 'verifier': 0}}, {'key': 'after', 'kind': 'settle', 'depends': ['bad'], 'spec': {'of': 'bad'}}, {'key': 'pay_first', 'kind': 'settle', 'depends': ['first'], 'spec': {'of': 'first'}}]}
        m2 = self.mission(graph=graph2, template=None); mid2 = m2['id']
        self.drive_work(mid2, 'first', 3)
        s = self.step(mid2); self.assertEqual((s['performed']['action'], s['performed']['milestone']), ('pay', 'pay_first')); self.assertEqual(self.bal('machine'), 13)   # incurred obligations settle before new spending
        s = self.step(mid2); self.assertEqual(s['performed']['action'], 'open_request'); self.offer_as_machine(self.ms(mid2, 'bad')['binding']['request_id'], 5); s = self.step(mid2); self.assertEqual(s['performed']['action'], 'award')
        for _ in range(3):
            self.w.run_once()
        jid = self.c.get('/api/v1/work/awards/' + self.ms(mid2, 'bad')['binding']['award_id'], headers=self.H).json()['milestones'][0]['job_id']
        self.c.post('/api/v1/ops/faults', headers=self.H, json={'fault': 'verification_fail', 'job_id': jid})
        s = self.step(mid2); self.assertEqual(s['performed']['action'], 'verify')
        for _ in range(3):
            self.w.run_once()
        s = self.step(mid2); self.assertEqual((s['performed']['action'], s['result']['decision']), ('decide', 'rejected'))
        self.assertEqual((self.ms(mid2, 'bad')['state'], self.ms(mid2, 'after')['state']), ('failed', 'skipped'))
        s = self.step(mid2); self.assertIsNone(s['performed']); self.assertEqual(self.bal('machine'), 13); self.assertIn(self.view(mid2)['state'], ('completed', 'waiting'))

    def test_amendment_authority_and_replan_bounds(self):
        m = self.mission(); mid = m['id']; self.drive_work(mid, 'acquire', 4)
        # unauthorized mutation: the provider cannot amend; the owner cannot touch an awarded/accepted milestone
        self.assertEqual(self.c.post('/api/v1/missions/' + mid + '/amend', headers=self.M, json={'milestones': [], 'reason': 'x'}).status_code, 403)
        r = self.c.post('/api/v1/missions/' + mid + '/amend', headers=self.R, json={'milestones': [{'key': 'acquire', 'kind': 'work', 'spec': {'template': 'lunar_terrain_tile', 'inputs': {}, 'price': 1}}], 'reason': 'cheaper'}); self.assertEqual((r.status_code, r.json()['detail']['code']), (409, 'milestone_committed'))
        # an authorized amendment of an unstarted milestone naming the provider needs that provider's authorization
        new_spec = dict(self.ms(mid, 'compute')['spec'], price=30, permitted_providers=[self.prov['id']])
        r = self.c.post('/api/v1/missions/' + mid + '/amend', headers=self.R, json={'milestones': [{'key': 'compute', 'kind': 'work', 'depends': ['acquire'], 'spec': new_spec}], 'reason': 'renegotiated price'}); self.assertEqual(r.status_code, 200, r.text); v = r.json()
        rev = v['revisions'][-1]; self.assertEqual((rev['revision'], rev['state'], sorted(rev['affected'])), (2, 'proposed', sorted([self.actors['requester']['principal_id'], self.actors['machine']['principal_id']])))
        self.assertEqual(self.c.post('/api/v1/missions/%s/revisions/2/authorize' % mid, headers=self.V, json={}).status_code, 403)
        v = self.c.post('/api/v1/missions/%s/revisions/2/authorize' % mid, headers=self.M, json={}).json(); self.assertEqual((v['frozen_revision'], v['revisions'][-1]['state'], v['revisions'][0]['state']), (2, 'frozen', 'superseded'))
        self.assertEqual(self.ms(mid, 'compute')['spec']['price'], 30); self.assertEqual(self.ms(mid, 'acquire')['state'], 'accepted')     # incurred work untouched
        s = self.step(mid); self.assertEqual(s['performed']['action'], 'pay')
        s = self.drive_work(mid, 'compute', 30); self.assertEqual(s['result']['payable_amount'], 30)

    def test_reserve_ceilings_proposals_blackout_and_revocation(self):
        pol = {'schema': 'metacoin-agent-policy/v1', 'permitted_services': ['route_feasibility', 'terrain_tile'], 'allowed_operations': ['native:read', 'native:transfer', 'work:read', 'work:award'], 'ceilings': {'total_amount': 60, 'per_action_amount': 50, 'max_jobs': 10, 'max_workflows': 0, 'concurrency': 1}, 'validity_seconds': 3600, 'review_gate_mandatory': False, 'input_visibility': 'own'}
        g = self.c.post('/api/v1/agents/grants', headers=self.H, json={'policy': pol}); self.assertEqual(g.status_code, 201, g.text); g = g.json()
        own = self.c.get('/api/v1/native/accounts/me', headers=self.H).json()['id']; self.c.post('/api/v1/native/fixtures/fund', headers=self.H, json={'account_id': own, 'amount': 200}); self.R = self.H
        self.actors['requester'] = {'account': own, 'principal_id': self.c.get('/api/v1/me', headers=self.H).json()['principal_id'], 'h': self.H}
        m = self.mission(policy={'min_cash_reserve': 150, 'per_action_ceiling': 45}, grant_id=g['grant_id']); mid = m['id']
        # journey 18: minimum reserve: 200 available - 150 reserve = 50 spendable; the acquire (4) fits, compute (45) fits exactly, nothing more
        ex = self.c.get('/api/v1/missions/' + mid + '/explain', headers=self.R).json(); self.assertEqual(ex['context']['spendable_settled'], 50)
        self.drive_work(mid, 'acquire', 4)
        ex = self.c.get('/api/v1/missions/' + mid + '/explain', headers=self.R).json(); op = next(a for a in ex['actions'] if a['action'] == 'open_request'); self.assertTrue(op['permitted'], op)   # 46 spendable >= 45 ceiling
        m2 = self.mission(policy={'min_cash_reserve': 198, 'per_action_ceiling': 45}, grant_id=g['grant_id'])['id']
        ex2 = self.c.get('/api/v1/missions/' + m2 + '/explain', headers=self.R).json(); op2 = next(a for a in ex2['actions'] if a['action'] == 'open_request'); self.assertFalse(op2['permitted']); self.assertIn('insufficient_liquidity', op2['reason'])
        # journey 19: a model-proposed unsafe action is rejected by the deterministic validator with a recorded reason
        pr = self.c.post('/api/v1/missions/' + mid + '/propose', headers=self.R, json={'action': 'reserve', 'milestone': 'store'}).json(); self.assertFalse(pr['proposal']['accepted']); self.assertIn(pr['proposal']['reason'], ('not_a_permissible_action',) + tuple(x for x in [pr['proposal']['reason']] if 'insufficient' in x or 'no_offer' in x))
        pr2 = self.c.post('/api/v1/missions/' + mid + '/propose', headers=self.R, json={'action': 'pay', 'milestone': 'settle'}).json(); self.assertFalse(pr2['proposal']['accepted'])
        ev = self.view(mid)['events']; self.assertTrue(any(e['kind'] == 'proposal_rejected' for e in ev))
        # journey 20: blackout: relay/evidence actions are refused with link_cut; local actions proceed; exhausted -> visible wait
        self.c.post('/api/v1/native/domain/connectivity', headers=self.H, json={'state': 'cut'})
        ex = self.c.get('/api/v1/missions/' + mid + '/explain', headers=self.R).json(); self.assertEqual(ex['context']['connectivity'], 'cut')
        s = self.step(mid); self.assertEqual((s['performed']['action'], s['performed']['milestone']), ('pay', 'settle_acquire'), s)   # local settlement proceeds during the blackout
        self.c.post('/api/v1/native/domain/connectivity', headers=self.H, json={'state': 'online'})
        # journey 21: revoke the grant: new authorized work stops, incurred obligations stay visible and payable
        self.drive_work(mid, 'compute', 40)
        self.c.post('/api/v1/agents/grants/' + g['grant_id'] + '/revoke', headers=self.R, json={})
        ex = self.c.get('/api/v1/missions/' + mid + '/explain', headers=self.R).json(); self.assertEqual(ex['stop_reason'], 'grant_revoked')
        pay = [a for a in ex['actions'] if a['action'] == 'pay']; others = [a for a in ex['actions'] if a['action'] != 'pay']
        self.assertTrue(all(not a['permitted'] and 'grant_revoked' in a['reason'] for a in others))
        if pay:
            self.assertTrue(pay[0]['permitted']); s = self.step(mid); self.assertEqual(s['performed']['action'], 'pay')
        led = self.c.get('/api/v1/missions/' + mid + '/ledger', headers=self.R).json(); self.assertGreaterEqual(led['totals']['spent'], 4)


class MissionPlanningTests(MissionBase):
    """Journeys 22–25 at mission level: the planner reads the market and the owner's authority, never locks capacity; competing
    missions share capacity; a missed contact window is released and replanned without duplicate payments; reserve/deadline
    conflicts return a witness."""
    def graph(self, key='link', kind='contact', qty=8, deadline=None):
        sp = {'offer_kind': kind, 'quantity': qty, 'max_price_per_unit': 5}
        if deadline:
            sp['deadline_at'] = deadline
        return {'milestones': [{'key': key, 'kind': 'resource', 'depends': [], 'spec': sp}]}

    def offer(self, kind, cap, price, ws, we, h=None):
        r = self.c.post('/api/v1/resources/offers', headers=h or self.S, json={'kind': kind, 'resource_id': '%s-%d-%d' % (kind, ws, price), 'capacity': cap, 'price_per_unit': price, 'window_start': ws, 'window_end': we}); self.assertEqual(r.status_code, 201, r.text); return r.json()

    def test_joint_plan_shares_capacity_and_reservations_revalidate(self):
        t = int(time.time()); cheap = self.offer('contact', 10, 1, t - 1, t + 3600); dear = self.offer('contact', 20, 4, t - 1, t + 3600, h=self.V)
        a = self.mission(graph=self.graph(qty=6), template=None)['id']; b = self.mission(graph=self.graph(qty=6), template=None)['id']
        r = self.c.post('/api/v1/missions/plan', headers=self.R, json={'mission_ids': [a, b], 'objective': 'lowest_cost', 'apply': True}); self.assertEqual(r.status_code, 200, r.text); pl = r.json()
        self.assertEqual((pl['status'], pl['binding'], pl['incumbent']['cost']), ('optimal', False, 30)); self.assertEqual(sorted(pl['incumbent']['choice'].values()), sorted([cheap['id'], dear['id']]))
        self.assertEqual(sorted(pl['applied']), sorted([a + ':link', b + ':link']))
        self.assertEqual(self.c.get('/api/v1/resources/offers/' + cheap['id'], headers=self.R).json()['available'], 10)      # nothing locked by planning
        sa = self.step(a); sb = self.step(b); self.assertEqual((sa['performed']['action'], sb['performed']['action']), ('reserve', 'reserve')); self.assertTrue(sa['result']['planned'] and sb['result']['planned'])
        self.assertEqual({sa['performed']['offer_id'], sb['performed']['offer_id']}, {cheap['id'], dear['id']})
        self.assertEqual(self.c.get('/api/v1/resources/offers/' + cheap['id'], headers=self.R).json()['available'], 4)
        # a third mission planned alone would also take the cheap offer; the market, not the plan, refuses overcommitment
        c3 = self.mission(graph=self.graph(qty=6), template=None)['id']; pl3 = self.c.post('/api/v1/missions/' + c3 + '/plan', headers=self.R, json={'objective': 'lowest_cost'}).json()
        self.assertEqual(pl3['incumbent']['choice'][c3 + ':link'], dear['id'])     # planner reads live availability (cheap has 4 left)
        self.assertEqual(self.bal('requester', 'reserved'), 30)

    def test_missed_window_released_and_replanned_without_duplicate_payment(self):
        t = int(time.time()); early = self.offer('contact', 10, 1, t - 1, t + 2); late = self.offer('contact', 10, 2, t + 4, t + 3600)
        m = self.mission(graph=self.graph(qty=5), template=None)['id']
        pl = self.c.post('/api/v1/missions/' + m + '/plan', headers=self.R, json={'objective': 'earliest_delivery', 'apply': True}).json(); self.assertEqual(pl['incumbent']['choice'][m + ':link'], early['id'])
        s = self.step(m); self.assertEqual((s['performed']['action'], s['performed']['offer_id']), ('reserve', early['id'])); first = s['result']['reservation_id']; self.assertEqual(self.bal('requester', 'reserved'), 5)
        time.sleep(2.2)
        s = self.step(m); self.assertEqual(s['performed']['action'], 'release_missed_reservation', s); self.assertEqual(s['result']['released_amount'], 5); self.assertEqual(self.bal('requester', 'reserved'), 0)
        self.assertEqual(self.c.get('/api/v1/resources/reservations/' + first, headers=self.R).json()['state'], 'cancelled')
        pl2 = self.c.post('/api/v1/missions/' + m + '/plan', headers=self.R, json={'objective': 'earliest_delivery', 'apply': True}).json(); self.assertEqual(pl2['incumbent']['choice'][m + ':link'], late['id'])
        s = self.step(m); self.assertEqual((s['performed']['action'], s['performed']['offer_id'], s['result']['attempt']), ('reserve', late['id'], 2)); second = s['result']['reservation_id']; self.assertNotEqual(first, second)
        ms = self.ms(m, 'link'); self.assertEqual(len(ms['binding']['previous_reservations']), 1)
        # the provider activates and delivers the second reservation; settlement pays exactly once
        self.c.post('/api/v1/resources/reservations/' + second + '/activate', headers=self.S, json={}); r = self.c.post('/api/v1/resources/reservations/' + second + '/deliver', headers=self.S, json={'bytes': 5 * 1024}); self.assertEqual(r.status_code, 200, r.text)
        s = self.step(m); self.assertEqual(s['performed']['action'], 'settle_reservation')
        obls = self.c.get('/api/v1/missions/' + m + '/ledger', headers=self.R).json(); self.assertEqual((obls['totals']['spent'], obls['totals']['released']), (10, 5))
        self.assertEqual(self.bal('supplier'), 10); self.assertEqual(self.bal('requester'), 190)
        self.assertTrue(any(e['kind'] == 'window_missed' for e in self.view(m)['events']))

    def test_reserve_deadline_conflict_returns_witness(self):
        t = int(time.time()); self.offer('contact', 10, 3, t + 600, t + 3600)
        m = self.mission(graph=self.graph(qty=5), template=None, policy={'min_cash_reserve': 190})['id']
        pl = self.c.post('/api/v1/missions/' + m + '/plan', headers=self.R, json={'objective': 'lowest_cost'}).json()
        self.assertEqual((pl['status'], pl['witness']['constraint'], pl['witness']['minimum_cost'], pl['problem']['budgets'][m]), ('infeasible', 'budget', 15, 10))
        m2 = self.mission(graph=self.graph(qty=5), template=None)['id']
        pl2 = self.c.post('/api/v1/missions/' + m2 + '/plan', headers=self.R, json={'objective': 'lowest_cost', 'deadline_at': t + 300}).json()
        self.assertEqual((pl2['status'], pl2['witness']['constraint']), ('infeasible', 'deadline'))
        self.assertTrue(any(e['kind'] == 'plan' and e['detail']['status'] == 'infeasible' for e in self.view(m2)['events']))
        pl3 = self.c.post('/api/v1/missions/' + m2 + '/plan', headers=self.R, json={'objective': 'lowest_cost', 'planner': 'oracle'}).json(); self.assertEqual(pl3['status'], 'optimal')


class OperatorScreenTests(MissionBase):
    def test_operator_screen_answers_the_six_questions_and_disables_with_reasons(self):
        m = self.mission(policy={'compute_cpu_ms': 20}); mid = m['id']; self.drive_work(mid, 'acquire', 4)
        o = self.c.get('/api/v1/native/operator', headers=self.H).json(); self.assertEqual(o['schema'], 'metacoin-operator-screen/v1')
        mach = next(x for x in o['machines'] if x['account'] == self.actors['machine']['account']); req = next(x for x in o['machines'] if x['account'] == self.actors['requester']['account'])
        self.assertEqual((mach['owed']['outstanding'], req['owes']['outstanding'], req['owns']['reserved']), (4, 4, 4))
        self.assertTrue(any(a['mission'] == mid and a['action'] == 'pay' for a in o['blackout']['possible_now']))
        self.c.post('/api/v1/native/domain/connectivity', headers=self.H, json={'state': 'cut'})
        o2 = self.c.get('/api/v1/native/operator', headers=self.H).json(); self.assertTrue(o2['blackout']['active']); self.assertEqual(o2['connectivity'], 'cut')
        self.assertTrue(any(a['action'] == 'pay' for a in o2['blackout']['possible_now']))      # local settlement from reserved funds stays possible
        self.c.post('/api/v1/native/domain/connectivity', headers=self.H, json={'state': 'online'})
        self.assertEqual(o['pending_cross_domain']['allocations'], []); self.assertEqual(o['recovery']['state'], 'normal'); self.assertTrue(o['audit']['conserved'])
        r = self.c.get('/api/v1/native/operator', headers=self.actors['machine']['h']); self.assertEqual(r.status_code, 200)   # a machine may read the screen (native:read + work:read)
        html = self.c.get('/console/operator', headers=self.H); self.assertIn(html.status_code, (200, 303, 401))


if __name__ == '__main__':
    unittest.main()
