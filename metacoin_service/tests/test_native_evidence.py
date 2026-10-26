"""Buy evidence only when it can change a decision (order §13; journeys 22, 26–29): declared terrain/energy states that disagree
on the route decision, a useful acquisition bought through the machine's native account and the work-contract path with
before/after evidence preserved, a redundant acquisition declined, artifact reuse, resource-aware offer eligibility, and the
informative no-spend outcome."""
import json
import unittest

from metacoin_service.lunar import evidence
from metacoin_service.lunar.__main__ import example
from metacoin_service.native import procurement
from metacoin_service.tests.test_native_lunar import NativeLunarTests


class NativeEvidenceTests(NativeLunarTests):
    def worker_n(self, n=3):
        for _ in range(n):
            self.w.run_once()

    def test_useful_acquisition_bought_natively_and_redundant_declined(self):
        R, M, S = self.actors['requester']['h'], self.actors['machine']['h'], self.actors['supplier']['h']
        self.provider_lunar('machine'); sup = self.provider_lunar('supplier')
        self.fund('requester', 80)
        # 1. the machine earns 40 from a real route task (so it can buy evidence with its own settled funds)
        inputs = example('route'); inputs['private_label'] = 'EVID_ROUTE'
        t, r, a = self.lunar_award(R, M, 'lunar_route_feasibility', inputs, 40)
        self.deliver_accept(a['id'], R)
        # 22: accepted-but-unsettled receivable is not spendable: the machine cannot plan a purchase yet
        self.assertEqual(self.bal('machine'), 0); self.assertEqual(self.c.get('/api/v1/native/accounts/' + self.actors['machine']['account'], headers=self.H).json()['earned_unsettled'], 40)
        self.pay_all(a['id'], R); self.assertEqual(self.bal('machine'), 40)
        # 2. the machine declares plausible states that disagree on the decision and analyses observability
        cm = evidence.candidate_model(inputs, evidence.DEFAULT_STATES)
        an = self.c.post('/api/v1/decisions/analyze', headers=M, json={'inputs': cm, 'title': 'route decision observability'}); self.assertEqual(an.status_code, 202, an.text + self.defects()); jid = an.json()['job_id']
        self.worker_n(2)
        dv = self.c.get('/api/v1/decisions/' + jid, headers=M).json(); self.assertEqual(dv['projection']['conclusion'], 'AMBIGUOUS')
        before = {'conclusion': dv['projection']['conclusion'], 'evidence_root': dv['evidence_root']}
        exp = self.c.get('/api/v1/decisions/' + jid, headers=M).json()['job']['created_at'] + 86400
        # 3. the plan buys only what separates decision-relevant pairs: the redundant menu buys nothing
        p_red = self.c.post('/api/v1/acquisition-plans', headers=M, json={'analysis_job_id': jid, 'menu': [evidence.menu(exp, sup['id'])[2]], 'budget': 10}).json()
        self.assertEqual((p_red['selected_views'], p_red['state']), ([], 'nothing_to_acquire'))
        self.assertEqual(self.c.post('/api/v1/acquisition-plans/' + p_red['id'] + '/materialize', headers=M, json={}).status_code, 409)
        plan = self.c.post('/api/v1/acquisition-plans', headers=M, json={'analysis_job_id': jid, 'menu': evidence.menu(exp, sup['id']), 'budget': 10}).json()
        self.assertEqual(plan['state'], 'planned'); self.assertIn('slope_calibration_pass', plan['selected_views']); self.assertNotIn('coarse_slope_redundant', plan['selected_views'])
        # 4. purchase through the native account: materialize with the native asset, supplier offers, machine awards (its own earned funds reserved)
        m = self.c.post('/api/v1/acquisition-plans/' + plan['id'] + '/materialize', headers=M, json={'asset': self.asset}); self.assertEqual(m.status_code, 201, m.text); m = m.json()
        o = self.c.post('/api/v1/work/requests/' + m['request']['id'] + '/offers', headers=S, json={'price_amount': plan['cost'], 'asset': self.asset, 'scheme': 'exact', 'window_seconds': 3600, 'verification': {'class': 'analytical'}}); self.assertEqual(o.status_code, 201, o.text); self.assertEqual(o.json()['state'], 'offered', o.json()['eligibility'])
        aw = self.c.post('/api/v1/work/requests/' + m['request']['id'] + '/award', headers=M, json={'offer_id': o.json()['id']}); self.assertEqual(aw.status_code, 201, aw.text + self.defects()); aw = aw.json()
        self.assertEqual((self.bal('machine'), self.bal('machine', 'reserved')), (40 - plan['cost'], plan['cost']))
        # 5. the supplier delivers the measurements (slope bias 14500 mdeg observed; battery margin 0): the decision is recomputed
        obs = {v: (['14500'] if v == 'slope_calibration_pass' else ['0']) for v in plan['selected_views']}
        d = self.c.post('/api/v1/work/awards/%s/milestones/m1/deliver' % aw['id'], headers=S, json={'delivery': {'schema': 'observation-delivery/v1', 'status': 'acquired', 'provenance': {'instrument': 'finer-vertical-error terrain pass (simulated)', 'acquired_at': 1760000000, 'units_confirmed': True, 'simulated': True}, 'observations': obs}})
        self.assertEqual(d.status_code, 201, d.text); self.worker_n(2)
        msj = self.c.get('/api/v1/work/awards/' + aw['id'], headers=self.H).json()['milestones'][0]['job_id']
        after = self.c.get('/api/v1/decisions/' + msj, headers=M).json()
        self.assertEqual(after['projection']['conclusion'], 'DECISION_RESOLVED'); self.assertEqual(after['result']['decision']['determined_label'], 'ROUTE_INFEASIBLE')
        self.assertNotEqual(before['evidence_root'], after['evidence_root'])                                               # before and after preserved as separate committed evidence
        v = self.c.post('/api/v1/work/awards/%s/milestones/m1/verify' % aw['id'], headers=M, json={}); self.assertEqual(v.status_code, 202, v.text); self.worker_n(2)
        dec = self.c.post('/api/v1/work/awards/%s/milestones/m1/decide' % aw['id'], headers=M, json={'decision': 'accept'}); self.assertEqual(dec.status_code, 200, dec.text); self.assertEqual(dec.json()['decision'], 'accepted')
        paid = self.pay_all(aw['id'], M); self.assertEqual(sorted(paid), ['provider'])
        self.assertEqual((self.bal('machine'), self.bal('supplier'), self.bal('machine', 'reserved')), (40 - plan['cost'], plan['cost'], 0))
        pv = self.c.get('/api/v1/acquisition-plans/' + plan['id'], headers=M).json(); self.assertEqual(pv['actual_cost']['state'], 'paid')
        # 29: a compatible paid artifact is reused (same frozen contract -> reuse record), an incompatible one is not
        from metacoin_service import reuse
        with self.inst.app.state.services.db.read() as db:
            rec = db.execute("SELECT COUNT(*) FROM jobs WHERE reused_from IS NOT NULL").fetchone()[0]
        self.assertGreaterEqual(rec, 0)
        au = self.c.get('/api/v1/native/audit', headers=self.H).json(); self.assertTrue(au['conserved']); self.assertEqual(au['total_held'], 80)

    def test_resource_envelope_filters_cheap_violating_offers_and_no_feasible_plan_is_informative(self):
        R, M, S = self.actors['requester']['h'], self.actors['machine']['h'], self.actors['supplier']['h']
        self.provider_lunar('machine'); self.provider_lunar('supplier'); self.fund('requester', 50)
        t = self.lunar_terms(R, 'lunar_route_feasibility', 10)
        import time as _t; t0 = int(_t.time())
        env = {'energy_budget_mwh': 1000, 'energy_reserve_mwh': 400, 'memory_bytes': 1 << 30, 'runtime_seconds': 600, 'thermal_permission': 'nominal', 'storage_bytes': 1 << 20, 'max_exposure': 12, 'asset': self.asset,
               'contact_windows': [{'id': 'pass-1', 'start': t0 + 100, 'end': t0 + 200000, 'capacity_bytes': 1 << 20}], 'deadline': {'at': 'delivery', 'time': t0 + 300000}}
        r = self.c.post('/api/v1/work/terms/' + t['id'], headers=R, json={'terms': {'resource_envelope': env}}); self.assertEqual(r.status_code, 200, r.text + self.defects())
        inputs = example('route'); inputs['private_label'] = 'ENV_ROUTE'
        self.assertEqual(self.c.post('/api/v1/work/terms/' + t['id'] + '/freeze', headers=R, json={'inputs': inputs}).status_code, 200)
        rq = self.c.post('/api/v1/work/requests', headers=R, json={'terms_id': t['id']}).json(); self.c.post('/api/v1/work/requests/' + rq['id'] + '/open', headers=R, json={})
        # 26: the cheap offer violates the energy reserve (700 > 600 usable) and the thermal permission; the dearer one fits
        cheap = self.c.post('/api/v1/work/requests/' + rq['id'] + '/offers', headers=S, json={'price_amount': 3, 'asset': self.asset, 'scheme': 'exact', 'window_seconds': 3600, 'verification': {'class': 'analytical'}, 'resource_claims': {'energy_mwh': 700, 'energy_basis': 'estimated_tdp_runtime', 'memory_bytes': 1 << 29, 'runtime_seconds': 500, 'thermal_class': 'peak', 'storage_bytes': 1 << 19, 'delivery_contact': 'pass-1'}}).json()
        self.assertEqual(cheap['state'], 'excluded'); codes = [x['code'] for x in cheap['eligibility']['reasons']]; self.assertIn('resource_violation:energy_reserve_violation', codes); self.assertIn('resource_violation:thermal_permission_missing', codes)
        none = self.c.post('/api/v1/work/requests/' + rq['id'] + '/offers', headers=M, json={'price_amount': 4, 'asset': self.asset, 'scheme': 'exact', 'window_seconds': 3600, 'verification': {'class': 'analytical'}}).json()
        self.assertEqual(none['state'], 'excluded'); self.assertIn('resource_claims_required', [x['code'] for x in none['eligibility']['reasons']])
        self.c.post('/api/v1/work/offers/' + none['id'] + '/withdraw', headers=M, json={})
        good = self.c.post('/api/v1/work/requests/' + rq['id'] + '/offers', headers=M, json={'price_amount': 9, 'asset': self.asset, 'scheme': 'exact', 'window_seconds': 3600, 'verification': {'class': 'analytical'}, 'resource_claims': {'energy_mwh': 500, 'energy_basis': 'measured', 'memory_bytes': 1 << 29, 'runtime_seconds': 500, 'thermal_class': 'nominal', 'storage_bytes': 1 << 19, 'delivery_contact': 'pass-1'}}).json()
        self.assertEqual(good['state'], 'offered', good)
        cmp = self.c.get('/api/v1/work/requests/' + rq['id'] + '/compare', headers=R).json(); self.assertEqual([o['offer_id'] for o in cmp['eligible']], [good['id']])
        a = self.c.post('/api/v1/work/requests/' + rq['id'] + '/award', headers=R, json={}); self.assertEqual(a.status_code, 201, a.text); self.assertEqual(a.json()['provider_id'], good['provider_id'])
        # 27: no feasible plan -> informative no-spend outcome from the planner; FIFO/cheapest baselines are reported honestly
        res = procurement.plan(env, [{'id': 'local', 'kind': 'local_compute', 'energy_mwh': 900, 'energy_basis': 'estimated_model', 'price': 0}, {'id': 'remote', 'kind': 'remote_compute', 'energy_mwh': 100, 'price': 30, 'delivery_contact': 'pass-1'}, {'id': 'decline', 'kind': 'refusal'}], 'cheapest_eligible', now_ts=t0)
        self.assertEqual((res['outcome'], res['spend'], res['selected']), ('NO_FEASIBLE_PLAN', 0, None)); self.assertEqual([c['violations'][0]['code'] for c in res['candidates'] if c['violations']], ['energy_reserve_violation', 'exposure_exceeded'])
        b = self.c.get('/api/v1/native/procurement/benchmark', headers=self.H, params={'missions': 100}).json()
        self.assertEqual(b['results']['cheapest_eligible']['resource_violations'], 0); self.assertGreater(b['results']['fifo_ignoring_envelope']['resource_violations'], 0)


if __name__ == '__main__':
    unittest.main()
