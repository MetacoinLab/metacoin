"""Decision evidence through the real entry points: analysis jobs with private witnesses and disclosure-policed projections,
the independent checker as a verification class, acquisition plans (preview, nothing-to-acquire, expiry), materialisation
into work terms, award with the existing reservation, provider deliveries released to the held job, the five delivery
states and their payments under the frozen terms, assurance previews, idempotent and lost-response settlement, crash
recovery without duplicate publication, scoped credentials that analyse but cannot purchase, authorised bundles replayed
offline, and verification portfolios with paired fault campaigns."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from metacoin_service.tests.test_work_terms import TermsInstance
from metacoin_service.decision import kernels as K, operations as ops

ROOT = Path(__file__).resolve().parents[2]
HOT = [{'id': 'hot_cell_1', 'label': 'DO_NOT_START', 'state': [100, 20], 'provenance': 'thermal model v3'}, {'id': 'hot_cell_2', 'label': 'START_UNDER_DECLARED_MODEL', 'state': [20, 100], 'provenance': 'thermal model v3'}]
HOT3 = HOT + [{'id': 'hot_cell_3', 'label': 'START_UNDER_DECLARED_MODEL', 'state': [100, 60], 'provenance': 'thermal model v3'}]     # cell_1 alone cannot separate hot_cell_1 from hot_cell_3
TOTAL = {'id': 'total', 'rows': [[1, 1]], 'noise': [2], 'output_units': ['mJ'], 'cost': 0}


def analysis_inputs(views=None, observations=None, disclosure=None, **kw):
    d = {'schema': K.ANALYSIS_SCHEMA, 'model': {'id': K.MODEL_ID, 'version': 1}, 'candidate_set_version': 'cs-2026-10-22', 'measurement_menu_version': 'mm-1',
         'coordinates': [{'name': 'cell_1', 'unit': 'mJ'}, {'name': 'cell_2', 'unit': 'mJ'}], 'hypotheses': HOT, 'views': views if views is not None else [TOTAL],
         'question': 'may the heater start?', 'private_label': 'DECISION_TEST'}
    if observations is not None:
        d['observations'] = observations
    if disclosure is not None:
        d['disclosure'] = disclosure
    d.update(kw)
    return d


def menu(expires, provider_id=None):
    e = [{'id': 'total_again', 'rows': [[1, 1]], 'noise': [2], 'output_units': ['mJ'], 'cost': 1, 'quote_expires_at': expires, 'execution_modes': ['local_worker']},
         {'id': 'near_parallel', 'rows': [[1, '1001/1000']], 'noise': [1], 'output_units': ['mJ'], 'cost': 1, 'quote_expires_at': expires, 'execution_modes': ['local_worker']},
         {'id': 'cell_1_sensor', 'rows': [[1, 0]], 'noise': [2], 'output_units': ['mJ'], 'cost': 3, 'quote_expires_at': expires, 'execution_modes': ['local_worker', 'instrument']}]
    if provider_id:
        for x in e:
            x['provider_id'] = provider_id
    return e


def delivery(obs, status='acquired', note=None, **prov):
    d = {'schema': ops.DELIVERY_SCHEMA, 'status': status, 'provenance': dict({'instrument': 'cell-1 calorimeter (simulated)', 'acquired_at': 1760000000, 'units_confirmed': True, 'simulated': True}, **prov)}
    if obs is not None:
        d['observations'] = obs
    if note:
        d['note'] = note
    return d


class DecisionBase(unittest.TestCase):
    def setUp(self):
        self.inst = TermsInstance(); self.addCleanup(self.inst.close); self.c = self.inst.client; self.H = self.inst.h('owner'); self.R = self.inst.h('reviewer'); self.V = self.inst.h('viewer')
        self.w = self.inst.worker(); self.addCleanup(self.w.offline)
        self.assertEqual(self.c.put('/api/v1/budgets/workspace', headers=self.H, json={'ceiling': 1000}).status_code, 200)
        p = self.c.post('/api/v1/work/providers', headers=self.H, json={'name': 'lab', 'capabilities': {'kinds': ['evidence_acquisition', 'energy_audit'], 'verification_classes': ['analytical', 'full_exact'], 'payment_schemes': ['exact', 'upto']}, 'pay_to': 'provider:lab'}).json()
        self.pv = {'id': p['id'], 'h': {'Authorization': 'Bearer ' + p['credential']['token']}, 'principal_id': p['principal_id']}

    def run_worker(self, n=3):
        for _ in range(n):
            self.w.run_once()

    def analyze(self, inputs, h=None):
        r = self.c.post('/api/v1/decisions/analyze', headers=h or self.H, json={'inputs': inputs, 'title': 'heater start'}); self.assertEqual(r.status_code, 202, r.text)
        self.run_worker(2)
        return self.c.get('/api/v1/decisions/' + r.json()['decision_job_id'], headers=h or self.H).json()

    def plan(self, jid, budget, expires=None, provider_id=None, create=True):
        body = {'analysis_job_id': jid, 'menu': menu(expires or (self.now() + 3600), provider_id), 'budget': budget}
        r = self.c.post('/api/v1/acquisition-plans' + ('' if create else '/preview'), headers=self.H, json=body); self.assertEqual(r.status_code, 201 if create else 200, r.text)
        return r.json()

    def now(self):
        from metacoin_service.db import now
        return now()

    def acquire(self, budget=3, diagnostic=0, price=None, expires=None, inputs=None):
        """analysis -> plan -> materialize -> offer -> award; returns (analysis, plan, materialized, award)."""
        an = self.analyze(inputs or analysis_inputs()); self.assertEqual(an['projection']['conclusion'], 'AMBIGUOUS')
        pl = self.plan(an['job']['id'], budget, expires=expires, provider_id=self.pv['id'])
        m = self.c.post('/api/v1/acquisition-plans/' + pl['id'] + '/materialize', headers=self.H, json={'diagnostic_amount': diagnostic}); self.assertEqual(m.status_code, 201, m.text); m = m.json()
        o = self.c.post('/api/v1/work/requests/' + m['request']['id'] + '/offers', headers=self.pv['h'], json={'price_amount': price if price is not None else pl['cost'], 'asset': 'action-units', 'scheme': 'exact', 'window_seconds': 3600, 'verification': {'class': 'analytical', 'distinct_verifier': False}}); self.assertEqual(o.status_code, 201, o.text)
        a = self.c.post('/api/v1/work/requests/' + m['request']['id'] + '/award', headers=self.H, json={'offer_id': o.json()['id']}); self.assertEqual(a.status_code, 201, a.text)
        return an, pl, m, a.json()

    def deliver(self, aid, d, h=None, expect=201):
        r = self.c.post('/api/v1/work/awards/%s/milestones/m1/deliver' % aid, headers=h or self.pv['h'], json={'delivery': d}); self.assertEqual(r.status_code, expect, r.text); return r.json()

    def verify_ms(self, aid, cls='analytical'):
        r = self.c.post('/api/v1/work/awards/%s/milestones/m1/verify' % aid, headers=self.H, json={'class': cls}); self.assertEqual(r.status_code, 202, r.text); self.run_worker(2); return r.json()

    def evaluate(self, aid):
        r = self.c.post('/api/v1/work/awards/%s/milestones/m1/evaluate' % aid, headers=self.H, json={}); self.assertEqual(r.status_code, 200, r.text); return r.json()

    def decide(self, aid, decision='accept', reason=None, expect=200):
        r = self.c.post('/api/v1/work/awards/%s/milestones/m1/decide' % aid, headers=self.H, json={'decision': decision, 'reason': reason}); self.assertEqual(r.status_code, expect, r.text); return r.json()


class ObservabilityServiceTests(DecisionBase):
    def test_analysis_witnesses_privacy_checker_and_disclosure(self):
        # J1: repeated identical totals stay ambiguous through the real job path
        an = self.analyze(analysis_inputs(views=[dict(TOTAL, id='total_%d' % i) for i in range(5)]))
        self.assertEqual((an['state'], an['projection']['conclusion'], an['readings']['label']), ('succeeded', 'AMBIGUOUS', 'ambiguous'))
        self.assertEqual(an['projection']['counts']['ambiguous_pairs'], 1); self.assertNotIn('disclosed_witnesses', an['projection'])
        jid = an['job']['id']
        w = self.c.get('/api/v1/decisions/%s/witnesses' % jid, headers=self.H).json()
        self.assertEqual(w['ambiguity_witnesses'][0]['shared_observation']['total_0'], ['120']); self.assertEqual(w['ambiguity_witnesses'][0]['pair'], ['hot_cell_1', 'hot_cell_2'])
        # the viewer sees counts only: no ids, labels, witness values; witnesses endpoint refused; private inputs/result absent
        self.assertEqual(self.c.get('/api/v1/decisions/%s/witnesses' % jid, headers=self.V).status_code, 403)
        pv = self.c.get('/api/v1/decisions/%s' % jid, headers=self.V).json()
        self.assertNotIn('result', pv); self.assertNotIn('inputs', pv); self.assertNotIn('hot_cell', json.dumps(pv)); self.assertNotIn('DO_NOT_START', json.dumps(pv))
        # J5/J6: the independent checker (verification class analytical) passes; a tampered result fails it
        v = self.c.post('/api/v1/verification', headers=self.H, json={'job_id': jid, 'class': 'analytical'}); self.assertEqual(v.status_code, 202, v.text); self.run_worker(2)
        vs = self.c.get('/api/v1/verification/' + v.json()['id'], headers=self.H).json(); self.assertEqual(vs['state'], 'passed'); self.assertIn('decision-witness-checker/v1', vs['result']['statement'])
        st = self.c.get('/api/v1/verification/' + v.json()['id'] + '/assurance', headers=self.H).json()
        self.assertEqual((st['claim_type'], st['coverage'], st['comparison']['semantics'], st['independence']['implementation'], st['outcome']), ('witness_check', 'whole_result', 'enumerated_predicate', True, 'passed'))
        from metacoin_service.decision import checker
        res = an['result']; fake = json.loads(json.dumps(res)); fake['ambiguity_witnesses'][0]['shared_observation']['total_0'] = ['300']
        self.assertFalse(checker.check_analysis(an['inputs'], fake)['ok'])
        # J2: the complementary measurement separates; J9: scaling the view and its noise consistently keeps the decision
        sep = self.analyze(analysis_inputs(views=[TOTAL, {'id': 'cell_1_sensor', 'rows': [[1, 0]], 'noise': [2], 'output_units': ['mJ'], 'cost': 3}]))
        self.assertEqual((sep['projection']['conclusion'], sep['readings']['label']), ('SEPARATED_ON_DECLARED_SET', 'separated within these assumptions'))
        scaled = self.analyze(analysis_inputs(views=[TOTAL, {'id': 'cell_1_sensor', 'rows': [['7', 0]], 'noise': ['14'], 'output_units': ['mJ'], 'cost': 3}]))
        self.assertEqual(scaled['projection']['conclusion'], 'SEPARATED_ON_DECLARED_SET')
        # J3/J4: near-parallel under noise and touching intervals stay ambiguous
        self.assertEqual(self.analyze(analysis_inputs(views=[TOTAL, {'id': 'np', 'rows': [[1, '1001/1000']], 'noise': [1], 'output_units': ['mJ'], 'cost': 1}]))['projection']['conclusion'], 'AMBIGUOUS')
        touch = self.analyze(analysis_inputs(views=[{'id': 'c1', 'rows': [[1, 0]], 'noise': [40], 'output_units': ['mJ'], 'cost': 1}]))
        self.assertEqual(touch['projection']['conclusion'], 'AMBIGUOUS'); self.assertEqual(touch['result']['ambiguity_witnesses'][0]['shared_observation']['c1'], ['60'])
        # J10: observations outside every hypothesis -> model inconsistency, no nearest hypothesis
        inc = self.analyze(analysis_inputs(observations={'total': ['500']}))
        self.assertEqual((inc['projection']['conclusion'], inc['readings']['label'], inc['result']['conditioning']['surviving']), ('MODEL_INCONSISTENT_WITH_OBSERVATIONS', 'unsupported', []))
        # disclosure policy: labels disclosed -> the projection names the determined label; ids still withheld
        det = self.analyze(analysis_inputs(views=[TOTAL, {'id': 'cell_1_sensor', 'rows': [[1, 0]], 'noise': [2], 'output_units': ['mJ'], 'cost': 3}], observations={'cell_1_sensor': ['99']}, disclosure={'ids': False, 'labels': True, 'witness_values': False}))
        pj = self.c.get('/api/v1/decisions/%s/projection' % det['job']['id'], headers=self.V).json()
        self.assertEqual((pj['conclusion'], pj.get('determined_label')), ('DETERMINED_WITHIN_DECLARED_MODEL', 'DO_NOT_START')); self.assertNotIn('hot_cell', json.dumps(pj))
        # invalid inputs are refused at contract validation (422), not executed
        bad = self.c.post('/api/v1/decisions/analyze', headers=self.H, json={'inputs': analysis_inputs(hypotheses=[dict(HOT[0], state=[1.5, 2]), HOT[1]])}); self.assertEqual(bad.status_code, 422, bad.text)
        lst = self.c.get('/api/v1/decisions', headers=self.H).json()['items']; self.assertGreaterEqual(len(lst), 6)


class AcquisitionJourneyTests(DecisionBase):
    def test_plan_preview_budget_and_nothing_to_acquire(self):
        an = self.analyze(analysis_inputs()); jid = an['job']['id']
        p3 = self.plan(jid, 3, create=False); self.assertEqual((p3['selected_views'], p3['cost'], p3['optimization_status'], p3['stored']), (['cell_1_sensor'], 3, 'exact', False))
        self.assertEqual(p3['ambiguities_before'], [['hot_cell_1', 'hot_cell_2']]); self.assertEqual(p3['remaining_ambiguities'], [])
        # J8: the stored plan agrees with the independent brute-force oracle
        oracle = K.plan_oracle(an['inputs'], menu(self.now() + 3600), 3); self.assertEqual((oracle['selected_views'], oracle['cost']), (p3['selected_views'], p3['cost']))
        # J7: budget 2 buys nothing: no terms, no reservation, no charge
        p2 = self.plan(jid, 2); self.assertEqual((p2['selected_views'], p2['state'], p2['nothing_to_acquire']), ([], 'nothing_to_acquire', True))
        m = self.c.post('/api/v1/acquisition-plans/' + p2['id'] + '/materialize', headers=self.H, json={}); self.assertEqual((m.status_code, m.json()['detail']['code']), (409, 'nothing_to_acquire'))
        with self.inst.app.state.services.db.read() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM budget_reservations WHERE ref_type='work_award'").fetchone()[0], 0); self.assertEqual(db.execute('SELECT COUNT(*) FROM work_terms').fetchone()[0], 0)
        # a menu larger than the exact bound is refused explicitly, never solved heuristically
        big = {'analysis_job_id': jid, 'menu': [dict(menu(self.now() + 3600)[0], id='c%d' % i) for i in range(13)], 'budget': 5}
        r = self.c.post('/api/v1/acquisition-plans/preview', headers=self.H, json=big); self.assertEqual((r.status_code, r.json()['detail']['code']), (422, 'menu_size_unsupported'))
        # an expired quote in the menu is refused at planning
        r = self.c.post('/api/v1/acquisition-plans/preview', headers=self.H, json={'analysis_job_id': jid, 'menu': menu(self.now() - 1), 'budget': 3}); self.assertEqual(r.json()['detail']['code'], 'quote_expired')

    def test_full_journey_resolved_then_ambiguous_then_inconsistent_all_paid_equally(self):
        amounts = {}
        for obs, want, inputs in ((['99'], 'DECISION_RESOLVED', None), (['99'], 'DECISION_AMBIGUOUS', analysis_inputs(hypotheses=HOT3)), (['500'], 'MODEL_INCONSISTENT_WITH_OBSERVATIONS', None)):
            an, pl, m, a = self.acquire(inputs=inputs)
            aw = self.c.get('/api/v1/work/awards/' + a['id'], headers=self.H).json()
            ms = aw['milestones'][0]; self.assertEqual((ms['state'], aw['reserved']), ('executing', 3)); self.assertIn('awaiting', ms['blocked_reason'])
            job = self.c.get('/api/v1/jobs/' + ms['job_id'], headers=self.H).json(); self.assertEqual(job['state'], 'queued')
            self.run_worker(1); self.assertEqual(self.c.get('/api/v1/jobs/' + ms['job_id'], headers=self.H).json()['state'], 'queued')       # held: nothing runs before delivery
            self.assertEqual(self.c.get('/api/v1/acquisition-plans/' + pl['id'], headers=self.H).json()['actual_cost']['state'], 'reserved')
            spec = self.c.get('/api/v1/work/awards/%s/milestones/m1/delivery-spec' % a['id'], headers=self.pv['h']).json()
            self.assertEqual(([v['id'] for v in spec['views']], spec['ready_for_delivery']), (['cell_1_sensor'], True)); self.assertNotIn('hot_cell', json.dumps(spec))
            self.assertEqual(self.c.get('/api/v1/work/awards/%s/milestones/m1/delivery-spec' % a['id'], headers=self.V).status_code, 403)
            d = self.deliver(a['id'], delivery({'cell_1_sensor': obs})); self.assertEqual((d['status'], d['job_hold']), ('acquired', False))
            self.run_worker(2)
            dv = self.c.get('/api/v1/decisions/' + ms['job_id'], headers=self.H).json(); self.assertEqual((dv['state'], dv['projection']['conclusion']), ('succeeded', want), dv.get('job'))
            if want == 'DECISION_AMBIGUOUS':
                self.assertEqual((dv['result']['decision']['surviving'], dv['result']['decision']['remaining_ambiguities'], dv['readings']['label']), (['hot_cell_1', 'hot_cell_3'], 1, 'ambiguous'))
            self.assertTrue(dv['result']['independent_check']['ok']); self.assertEqual(dv['delivery']['delivery_digest'], d['delivery_digest'])
            ev = self.evaluate(a['id']); self.assertEqual((ev['decision_candidate'], ev['science']), ('pending', want))
            ap = self.c.get('/api/v1/work/awards/%s/milestones/m1/assurance' % a['id'], headers=self.H).json(); self.assertEqual(ap['status'], 'missing evidence'); self.assertEqual(ap['next_checks'], ['analytical']); self.assertIn('absence of a check is never success', ap['plain'])
            self.verify_ms(a['id'])
            ap = self.c.get('/api/v1/work/awards/%s/milestones/m1/assurance' % a['id'], headers=self.H).json(); self.assertEqual(ap['status'], 'satisfied'); self.assertIn('Checked: verification', ap['plain'])
            ev = self.evaluate(a['id']); self.assertEqual((ev['decision_candidate'], ev['payment_class'], ev['payable_amount']), ('accepted', 'complete', 3), ev)
            dec = self.decide(a['id']); amounts[want] = dec['entitlement']['amount']
            self.assertEqual(self.c.get('/api/v1/acquisition-plans/' + pl['id'], headers=self.H).json()['actual_cost'], {'state': 'payable', 'amount': 3, 'settled': False})
            # settle on the application journal: duplicate prepare replays, a second submit is refused; one charge
            eid = dec['entitlement']['id']
            i1 = self.c.post('/api/v1/work/entitlements/%s/prepare' % eid, headers=self.H, json={}).json(); i2 = self.c.post('/api/v1/work/entitlements/%s/prepare' % eid, headers=self.H, json={}).json()
            self.assertEqual(i1['id'], i2['id']); self.assertTrue(i2.get('replayed'))
            self.assertEqual(self.c.post('/api/v1/work/intents/%s/authorize' % i1['id'], headers=self.H, json={}).json()['state'], 'authorized')
            s1 = self.c.post('/api/v1/work/intents/%s/submit' % i1['id'], headers=self.H, json={}).json(); self.assertEqual(s1['state'], 'settled', s1)
            s2 = self.c.post('/api/v1/work/intents/%s/submit' % i1['id'], headers=self.H, json={}); self.assertIn(s2.status_code, (200, 409))
            rep = self.c.post('/api/v1/work/journal/replay', headers=self.H, json={}).json(); self.assertTrue(rep['consistent'])
            self.assertEqual(self.c.get('/api/v1/acquisition-plans/' + pl['id'], headers=self.H).json()['actual_cost']['state'], 'paid')
        self.assertEqual(set(amounts.values()), {3})
        # the lineage of the last acquisition: analysis -> plan -> terms -> award -> job -> delivery
        lin = self.c.get('/api/v1/lineage/acquisition_plan/' + pl['id'], headers=self.H).json(); self.assertIn('work_terms', json.dumps(lin)); self.assertIn('planned_from', json.dumps(lin))
        # J25: a legacy determination contract alongside still reads with its own (v2 by default now, v1 when frozen so) semantics
        t = self.c.post('/api/v1/work/terms', headers=self.H, json={'template': 'determination'}).json(); insp = self.c.get('/api/v1/work/terms/' + t['id'] + '/inspect', headers=self.H).json()
        self.assertIn('claim-specific/v2', insp['what_counts_as_delivery']['verification_semantics'])

    def test_invalid_delivery_failed_acquisition_duplicates_and_quote_expiry(self):
        # J17: a report that the evidence could not be acquired earns exactly the frozen diagnostic amount (2 here), nothing more
        an, pl, m, a = self.acquire(diagnostic=2)
        self.deliver(a['id'], delivery(None, status='could_not_acquire', note='instrument offline'))
        self.run_worker(2); ev = self.evaluate(a['id']); self.assertEqual((ev['science'], ev['decision_candidate']), ('ACQUISITION_FAILED', 'pending'))
        self.verify_ms(a['id']); ev = self.evaluate(a['id']); self.assertEqual((ev['decision_candidate'], ev['payment_class'], ev['payable_amount']), ('accepted', 'diagnostic', 2))
        dec = self.decide(a['id']); self.assertEqual(dec['entitlement']['amount'], 2)
        # with diagnostic 0 the same report is rejected and nothing is payable
        an, pl, m, a = self.acquire(diagnostic=0)
        self.deliver(a['id'], delivery(None, status='could_not_acquire')); self.run_worker(2); self.verify_ms(a['id'])
        ev = self.evaluate(a['id']); self.assertEqual((ev['science'], ev['decision_candidate'], ev['payable_amount']), ('ACQUISITION_FAILED', 'rejected', 0))
        d = self.decide(a['id'], decision='reject', reason='no evidence'); self.assertIsNone(d['entitlement'])
        # malformed delivery (floats; wrong view id; missing units confirmation): DELIVERY_INVALID, rejected, no payment, record preserved
        self.assertEqual(self.c.post('/api/v1/work/awards/wa_x/milestones/m1/deliver', headers=self.pv['h'], content=b'{"delivery": {"observations": {"cell_1_sensor": [99.5]}}}', headers_extra=None).status_code if False else self.c.post('/api/v1/work/awards/wa_x/milestones/m1/deliver', headers=dict(self.pv['h'], **{'Content-Type': 'application/json'}), content=b'{"delivery": {"observations": {"cell_1_sensor": [99.5]}}}').status_code, 422)   # floats never cross the API boundary
        for bad in (delivery({'cell_1_sensor': ['abc']}), delivery({'total': ['99']}), delivery({'cell_1_sensor': ['99']}, units_confirmed=False)):
            an, pl, m, a = self.acquire()
            self.deliver(a['id'], bad); self.run_worker(2); self.verify_ms(a['id'])
            ev = self.evaluate(a['id']); self.assertEqual((ev['science'], ev['decision_candidate'], ev['payable_amount']), ('DELIVERY_INVALID', 'rejected', 0), ev['reason'])
            dv = self.c.get('/api/v1/decisions/' + ev['job_id'], headers=self.H).json(); self.assertEqual(dv['readings']['label'], 'unsupported'); self.assertTrue(dv['result']['reason'])
        # duplicate identical delivery replays; a different second delivery is refused; a viewer or the requester cannot deliver
        an, pl, m, a = self.acquire()
        d1 = self.deliver(a['id'], delivery({'cell_1_sensor': ['99']})); d2 = self.deliver(a['id'], delivery({'cell_1_sensor': ['99']})); self.assertEqual(d1['id'], d2['id']); self.assertTrue(d2['replayed'])
        self.deliver(a['id'], delivery({'cell_1_sensor': ['98']}), expect=409)
        self.deliver(a['id'], delivery({'cell_1_sensor': ['99']}), h=self.H, expect=403); self.deliver(a['id'], delivery({'cell_1_sensor': ['99']}), h=self.V, expect=403)
        # quote expiry: a plan whose quote expires before materialisation is refused; one expiring between materialisation and award is refused at award with nothing reserved
        an2 = self.analyze(analysis_inputs()); short = self.plan(an2['job']['id'], 3, expires=self.now() + 2, provider_id=self.pv['id'])
        with self.inst.app.state.services.db.tx() as db:
            db.execute('UPDATE acquisition_plans SET expires_at=? WHERE id=?', (self.now() - 1, short['id']))
        r = self.c.post('/api/v1/acquisition-plans/' + short['id'] + '/materialize', headers=self.H, json={}); self.assertEqual((r.status_code, r.json()['code'], r.json()['detail']['code']), (400, 'EXPIRED', 'quote_expired'))
        late = self.plan(an2['job']['id'], 3, expires=self.now() + 3600, provider_id=self.pv['id'])
        mm = self.c.post('/api/v1/acquisition-plans/' + late['id'] + '/materialize', headers=self.H, json={}).json()
        o = self.c.post('/api/v1/work/requests/' + mm['request']['id'] + '/offers', headers=self.pv['h'], json={'price_amount': 3, 'asset': 'action-units', 'scheme': 'exact', 'window_seconds': 3600, 'verification': {'class': 'analytical', 'distinct_verifier': False}}).json()
        with self.inst.app.state.services.db.tx() as db:
            db.execute('UPDATE acquisition_plans SET expires_at=? WHERE id=?', (self.now() - 1, late['id']))
            before = db.execute("SELECT COUNT(*) FROM budget_reservations WHERE ref_type='work_award'").fetchone()[0]
        r = self.c.post('/api/v1/work/requests/' + mm['request']['id'] + '/award', headers=self.H, json={'offer_id': o['id']}); self.assertEqual((r.status_code, r.json()['code'], r.json()['detail']['code']), (400, 'EXPIRED', 'quote_expired_at_award'))
        with self.inst.app.state.services.db.read() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM budget_reservations WHERE ref_type='work_award'").fetchone()[0], before)
        self.assertEqual(self.c.get('/api/v1/acquisition-plans/' + late['id'], headers=self.H).json()['state'], 'expired')

    def test_duplicate_award_lost_response_and_crash_resume(self):
        # J18/J20: the same award request replayed with the idempotency key and by offer identity makes one reservation; a crash at evidence
        # publication after delivery recovers without a second artifact or a second hold release
        an = self.analyze(analysis_inputs()); pl = self.plan(an['job']['id'], 3, provider_id=self.pv['id'])
        m = self.c.post('/api/v1/acquisition-plans/' + pl['id'] + '/materialize', headers=self.H, json={}).json()
        o = self.c.post('/api/v1/work/requests/' + m['request']['id'] + '/offers', headers=self.pv['h'], json={'price_amount': 3, 'asset': 'action-units', 'scheme': 'exact', 'window_seconds': 3600, 'verification': {'class': 'analytical', 'distinct_verifier': False}}).json()
        hk = dict(self.H, **{'Idempotency-Key': 'award-once'})
        a1 = self.c.post('/api/v1/work/requests/' + m['request']['id'] + '/award', headers=hk, json={'offer_id': o['id']}).json(); a2 = self.c.post('/api/v1/work/requests/' + m['request']['id'] + '/award', headers=hk, json={'offer_id': o['id']}).json()
        a3 = self.c.post('/api/v1/work/requests/' + m['request']['id'] + '/award', headers=self.H, json={'offer_id': o['id']}).json()
        self.assertEqual(a1['id'], a2['id']); self.assertEqual(a3['id'], a1['id']); self.assertTrue(a3.get('replayed'))
        with self.inst.app.state.services.db.read() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM budget_reservations WHERE ref_type='work_award'").fetchone()[0], 1)
        self.inst.settings.limits['test_hooks'] = 1
        self.deliver(a1['id'], delivery({'cell_1_sensor': ['99']}))
        self.c.post('/api/v1/ops/faults', headers=self.H, json={'fault': 'evidence_publication'})
        try:
            self.w.run_once()
        except Exception:
            pass
        self.c.post('/api/v1/ops/faults', headers=self.H, json={'fault': 'evidence_publication', 'disarm': True})
        with self.inst.app.state.services.db.tx() as db:
            j = db.execute('SELECT id, state FROM jobs WHERE id=(SELECT job_id FROM work_milestones WHERE award_id=?)', (a1['id'],)).fetchone(); self.assertEqual(j['state'], 'running')
            db.execute('UPDATE jobs SET lease_expires=1 WHERE id=?', (j['id'],))
        self.inst.reopen(); self.c = self.inst.client; self.w = self.inst.worker(); self.run_worker(2)
        with self.inst.app.state.services.db.read() as db:
            self.assertEqual(db.execute('SELECT state FROM jobs WHERE id=?', (j['id'],)).fetchone()['state'], 'succeeded')
            self.assertEqual(db.execute("SELECT COUNT(*) FROM artifacts WHERE job_id=? AND kind='evidence_vault' AND deleted_at IS NULL", (j['id'],)).fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM job_deliveries WHERE job_id=?', (j['id'],)).fetchone()[0], 1)
        self.assertEqual(self.c.get('/api/v1/decisions/' + j['id'], headers=self.H).json()['projection']['conclusion'], 'DECISION_RESOLVED')

    def test_scoped_credential_can_analyse_but_not_purchase_and_cross_workspace_is_invisible(self):
        # J19: analysis never implies spending authority
        r = self.c.post('/api/v1/credentials', headers=self.H, json={'operations': ['contract:create', 'contract:freeze', 'job:submit', 'job:read', 'job:read_private', 'contract:read'], 'expires_in_seconds': 3600}); self.assertEqual(r.status_code, 201, r.text)
        S = {'Authorization': 'Bearer ' + r.json()['token']}
        an = self.analyze(analysis_inputs(), h=S); self.assertEqual(an['projection']['conclusion'], 'AMBIGUOUS')
        pl = self.c.post('/api/v1/acquisition-plans', headers=S, json={'analysis_job_id': an['job']['id'], 'menu': menu(self.now() + 3600), 'budget': 3}); self.assertEqual(pl.status_code, 201, pl.text)
        mat = self.c.post('/api/v1/acquisition-plans/' + pl.json()['id'] + '/materialize', headers=S, json={}); self.assertEqual(mat.status_code, 403, mat.text)
        # the provider (another principal, same workspace but not the owner) cannot read witnesses or the private model; a different workspace sees nothing
        self.assertEqual(self.c.get('/api/v1/decisions/%s/witnesses' % an['job']['id'], headers=self.pv['h']).status_code, 403)
        self.assertEqual(self.c.get('/api/v1/acquisition-plans/' + pl.json()['id'], headers=self.pv['h']).json().get('remaining_ambiguities'), None)
        from metacoin_service.tests.test_service import Instance
        other = Instance(provider_mode='simulation'); self.addCleanup(other.close)
        self.assertEqual(other.client.get('/api/v1/decisions/%s' % an['job']['id'], headers=other.h('owner')).status_code, 404)
        self.assertEqual(other.client.get('/api/v1/acquisition-plans/' + pl.json()['id'], headers=other.h('owner')).status_code, 404)
        err = self.c.get('/api/v1/decisions/j_doesnotexist/witnesses', headers=self.V).json(); self.assertNotIn('hot_cell', json.dumps(err))

    def test_bundle_replays_offline_without_service_secrets(self):
        # J23: the authorised bundle replays witnesses, conditioning and verification signatures from a clean process
        an, pl, m, a = self.acquire()
        self.deliver(a['id'], delivery({'cell_1_sensor': ['99']})); self.run_worker(2); self.verify_ms(a['id'])
        jid = self.c.get('/api/v1/work/awards/' + a['id'], headers=self.H).json()['milestones'][0]['job_id']
        self.assertEqual(self.c.get('/api/v1/decisions/%s/bundle' % jid, headers=self.V).status_code, 403)
        z = self.c.get('/api/v1/decisions/%s/bundle' % jid, headers=self.H); self.assertEqual(z.status_code, 200)
        pub = self.c.get('/api/v1/verification/' + self.c.get('/api/v1/verification?job_id=' + jid, headers=self.H).json()['items'][0]['id'] + '/statement', headers=self.H).json()['public_key_hex']
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'bundle.zip'; p.write_bytes(z.content)
            env = {'PATH': os.environ.get('PATH', ''), 'PYTHONPATH': str(ROOT), 'HOME': d}
            out = subprocess.run([sys.executable, '-m', 'metacoin_service.decision.replay', str(p), '--trust-root', pub, '--json'], cwd=d, env=env, capture_output=True, text=True, timeout=120)
            self.assertEqual(out.returncode, 0, out.stderr[-500:] + out.stdout[-500:])
            rep = json.loads(out.stdout)
            self.assertTrue(rep['integrity']['ok']); self.assertTrue(rep['signatures']['trusted']); self.assertTrue(rep['scientific_replay']['ok']); self.assertEqual(rep['scientific_replay']['kind'], 'evidence_acquisition')
            self.assertNotIn('ed25519', json.dumps(rep).lower().replace('ed25519 public', ''))
            # untrusted root: signatures consistent but untrusted; science still replays
            out2 = subprocess.run([sys.executable, '-m', 'metacoin_service.decision.replay', str(p), '--json'], cwd=d, env=env, capture_output=True, text=True, timeout=120)
            rep2 = json.loads(out2.stdout); self.assertFalse(rep2['signatures']['trusted']); self.assertTrue(rep2['scientific_replay']['ok'])
            # a tampered bundle fails integrity
            import zipfile
            with zipfile.ZipFile(p) as zin:
                files = {n: zin.read(n) for n in zin.namelist()}
            files['result.json'] = files['result.json'].replace(b'DECISION_RESOLVED', b'DECISION_AMBIGUOUS')
            q = Path(d) / 'tampered.zip'
            with zipfile.ZipFile(q, 'w') as zout:
                for n, b in files.items():
                    zout.writestr(n, b)
            out3 = subprocess.run([sys.executable, '-m', 'metacoin_service.decision.replay', str(q), '--json'], cwd=d, env=env, capture_output=True, text=True, timeout=120)
            self.assertNotEqual(out3.returncode, 0); self.assertFalse(json.loads(out3.stdout)['integrity']['ok'])


class AssuranceTests(DecisionBase):
    def test_requirement_validation_and_preview(self):
        # unknown versions and dimensions are refused; an unachievable claim for the kind is refused with the achievable list
        an, pl, m, a = self.acquire(); self.deliver(a['id'], delivery({'cell_1_sensor': ['99']})); self.run_worker(2)
        jid = self.c.get('/api/v1/work/awards/' + a['id'], headers=self.H).json()['milestones'][0]['job_id']
        base = {'schema': 'metacoin-assurance-requirement/v1', 'claim_type': 'witness_check', 'coverage': 'whole_result', 'comparison': {'semantics': 'enumerated_predicate'}, 'independence': {'implementation': True}}
        for bad, code in ((dict(base, schema='metacoin-assurance-requirement/v9'), 'requirement_schema'), (dict(base, independence={'telepathy': True}), 'requirement_independence'), (dict(base, claim_type='itemwise_reference_exact', coverage='every_item', comparison={'semantics': 'exact'}), 'requirement_unachievable_for_kind'),
                          (dict(base, sampling={'min_sample_count': 3}), 'requirement_sampling_coverage'), (dict(base, independence={'operator': 'yes'}), 'requirement_independence')):
            r = self.c.post('/api/v1/verification/assurance/evaluate', headers=self.H, json={'job_id': jid, 'requirement': bad}); self.assertEqual((r.status_code, r.json()['detail']['code']), (422, code), bad)
        pv = self.c.post('/api/v1/verification/assurance/evaluate', headers=self.H, json={'job_id': jid, 'requirement': base}).json()
        self.assertEqual((pv['status'], pv['next_checks'][0]['class']), ('missing', 'analytical'))
        # operator independence can never be observed on this instance: the requirement is reported missing with the clause named, never satisfied
        self.verify_ms(a['id'])
        pv = self.c.post('/api/v1/verification/assurance/evaluate', headers=self.H, json={'job_id': jid, 'requirement': dict(base, independence={'implementation': True, 'operator': True})}).json()
        self.assertEqual(pv['status'], 'missing'); self.assertIn('independence:operator', pv['missing_clauses'])
        ok = self.c.post('/api/v1/verification/assurance/evaluate', headers=self.H, json={'job_id': jid, 'requirement': base}).json(); self.assertTrue(ok['satisfied'])
        # a same-implementation regression (full_exact) does not satisfy the witness_check requirement by class or rank
        self.verify_ms(a['id'], cls='full_exact')
        reg = dict(base, claim_type='same_implementation_regression', comparison={'semantics': 'exact'}, independence={})
        self.assertTrue(self.c.post('/api/v1/verification/assurance/evaluate', headers=self.H, json={'job_id': jid, 'requirement': reg}).json()['satisfied'])
        self.assertFalse(self.c.post('/api/v1/verification/assurance/evaluate', headers=self.H, json={'job_id': jid, 'requirement': dict(reg, independence={'implementation': True})}).json()['satisfied'])
        claims = self.c.get('/api/v1/verification/assurance/claims', headers=self.V).json(); self.assertIn('temporal_batch', claims['claims']); self.assertIn('every_item->sampled_items', claims['implications'])

    def test_legacy_rank_substitution_is_reproduced_under_v1_and_refused_under_v2(self):
        """The VERIFICATION_RANK investigation (Deliverable A): under policy v1 a passed replica (rank 2) satisfies a requested
        sampled_reference predicate (rank 2) and an earlier failed audit is ignored by a later pass; under v2 neither holds.
        The verification rows are inserted directly (a replica needs a second compute backend); the evaluator is the real one."""
        from metacoin_service.economy import acceptance, terms as terms_mod
        from metacoin_service.db import now
        an = self.analyze(analysis_inputs()); jid = an['job']['id']
        with self.inst.app.state.services.db.tx() as db:
            job = db.execute('SELECT * FROM jobs WHERE id=?', (jid,)).fetchone(); contract = db.execute('SELECT * FROM contracts WHERE id=?', (job['contract_id'],)).fetchone()
            doc = json.loads(contract['contract_json'])
            def row(vid, cls, state):
                st = {'schema': 'metacoin-verification-statement/v1', 'class': cls, 'outcome': 'passed' if state == 'passed' else 'failed', 'auditor_digest': 'x' * 64, 'scope': {'checked': 1, 'total': 1}}
                db.execute("INSERT INTO verification_jobs (id, workspace, target_job_id, requested_by, class, params_json, state, preview_json, result_commitment, result_json, statement_json, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                           (vid, job['workspace'], jid, 'p', cls, '{}', state, '{}', job['evidence_root'], json.dumps({'outcome': st['outcome'], 'checks': []}), json.dumps(st), now()))
            row('vf_replica_pass', 'replica', 'passed')
            t = terms_mod.template('determination', self.inst.ids['owner'], 'ws_default', kind='energy_audit')
            t['operation'] = {'kind': 'decision_observability', 'compatibility': 'same_verifier_digest', 'contract_id': contract['id'], 'contract_digest': contract['contract_digest'], 'input_root': contract['input_root'], 'model_id': doc['model_id'], 'verifier_id': doc['verifier_id'], 'verifier_digest': doc['verifier_digest']}
            t['eligibility']['capabilities'] = ['decision_observability']
            t['deliverables'] = [{'key': 'determination', 'type': 'decision_evidence', 'required': True}]
            preds = [{'id': 'complete', 'type': 'artifact_complete'}, {'id': 'source', 'type': 'source_revision'}, {'id': 'sampled', 'type': 'verification_passed', 'params': {'class': 'sampled_reference', 'distinct_verifier': False}}]
            v1 = dict(t, acceptance={'schema': terms_mod.POLICY_SCHEMA_V1, 'predicates': preds, 'outcomes': {'AMBIGUOUS': 'accept'}, 'required_verification': {'class': 'sampled_reference', 'distinct_verifier': False}, 'payment_rule': {'complete': 10, 'partial': 0, 'diagnostic': 0, 'outcome_neutral': True}})
            v2 = dict(t, acceptance=dict(v1['acceptance'], schema=terms_mod.POLICY_SCHEMA))
            terms_mod.validate(v1, 'frozen'); terms_mod.validate(v2, 'frozen')
            e1 = acceptance.evaluate(db, v1, 'm1', job); e2 = acceptance.evaluate(db, v2, 'm1', job)
            p1 = next(x for x in e1['trace'] if x['predicate'] == 'sampled'); p2 = next(x for x in e2['trace'] if x['predicate'] == 'sampled')
            self.assertEqual((p1['result'], p1['detail']['class'], p1['detail']['substituted_class']), ('passed', 'replica', True)); self.assertIn('legacy-rank', p1['detail']['semantics'])      # REPRODUCED: a replica satisfies a sampled_reference predicate under v1
            self.assertEqual((p2['result'], e2['decision_candidate']), ('unknown', 'pending')); self.assertIn('claim-specific', p2['detail']['semantics'])
            # cherry-picking: a failed sampled audit followed by a passed one is accepted under v1, refused under v2
            row('vf_sampled_fail', 'sampled_reference', 'failed'); row('vf_sampled_pass', 'sampled_reference', 'passed')
            e1 = acceptance.evaluate(db, v1, 'm1', job); e2 = acceptance.evaluate(db, v2, 'm1', job)
            p1 = next(x for x in e1['trace'] if x['predicate'] == 'sampled'); p2 = next(x for x in e2['trace'] if x['predicate'] == 'sampled')
            self.assertEqual((p1['result'], p1['detail']['earlier_failures_ignored']), ('passed', ['vf_sampled_fail']))
            self.assertEqual((p2['result'], p2['detail']['verification_id']), ('failed', 'vf_sampled_fail'))
            self.assertIn('legacy-rank/v1', e1['policy_semantics']); self.assertIn('claim-specific/v2', e2['policy_semantics'])


class PortfolioTests(DecisionBase):
    def test_portfolios_campaign_paired_report_and_selector(self):
        comps = self.c.get('/api/v1/portfolios', headers=self.H).json(); self.assertIn('shared_wrong_model', comps['families']); self.assertEqual(comps['components']['replica_same_code']['dependencies']['implementation'], 'shared')
        mk = lambda name, cs: self.c.post('/api/v1/portfolios', headers=self.H, json={'name': name, 'components': cs}).json()
        thin = mk('replica-only', [{'component': 'byte_integrity'}, {'component': 'contract_binding'}, {'component': 'replica_same_code'}])
        sampled = mk('sampled', [{'component': 'byte_integrity'}, {'component': 'contract_binding'}, {'component': 'schema_units'}, {'component': 'invariants'}, {'component': 'itemwise_exact_sampled', 'params': {'min_defects': 4, 'alpha': '1/20', 'budget': 12}}, {'component': 'quote_validity'}])
        full = mk('full', [{'component': 'byte_integrity'}, {'component': 'contract_binding'}, {'component': 'schema_units'}, {'component': 'invariants'}, {'component': 'itemwise_exact_full'}, {'component': 'heat_whole_trajectory'}, {'component': 'sampler_regeneration'}, {'component': 'quote_validity'}, {'component': 'witness_check'}])
        self.assertEqual(thin['common_mode'], ['input_source', 'library', 'operator']); self.assertIn('instrument', thin['unknown_dimensions'])
        bad = self.c.post('/api/v1/portfolios', headers=self.H, json={'name': 'x', 'components': [{'component': 'replica_same_code', 'dependencies': {'implementation': 'independent'}}]}); self.assertEqual(bad.status_code, 422)
        suite = self.c.post('/api/v1/portfolios/fault-suite', headers=self.H, json={'name': 'faults', 'seed': 7, 'cases_per_family': 2, 'holdout_per_family': 1}); self.assertEqual(suite.status_code, 201, suite.text); suite = suite.json()
        self.assertEqual(suite['item_count'], 45)
        # held-out items are hidden to non-admin listings
        shown = self.c.get('/api/v1/evaluation/suites', headers=self.V).json()['items'][0]['items']; self.assertTrue(all('family' not in it for it in shown if it['hidden']))
        camp = self.c.post('/api/v1/portfolios/campaigns', headers=self.H, json={'suite_id': suite['id'], 'portfolio_ids': [thin['id'], sampled['id'], full['id']], 'split': 'dev'}); self.assertEqual(camp.status_code, 201, camp.text); camp = camp.json()
        rep = camp['report']; self.assertEqual(rep['cases'], 30); self.assertIsNone(rep['holdout_uses'])
        T, S, F = (rep['portfolios'][p['id']] for p in (thin, sampled, full))
        self.assertEqual(F['totals']['false_acceptance'], 2); self.assertEqual(F['totals']['false_rejection'], 0)                  # full catches everything except the shared wrong model (2 dev cases)
        self.assertEqual(rep['common_mode']['families_no_portfolio_caught'], ['shared_wrong_model']); self.assertIn('measurement acquisition', rep['common_mode']['reading'])
        self.assertGreater(T['totals']['false_acceptance'], F['totals']['false_acceptance'])
        self.assertEqual(S['by_family']['single_item_corrupt']['cases'], 2); self.assertEqual(S['suite_coverage']['clean'], None)
        self.assertFalse(F['statistical']['applicable']); self.assertIn('suite coverage only', F['statistical']['note'])
        pair = rep['paired'][thin['id'] + '|' + full['id']]; self.assertGreater(pair['paired_outcomes']['only_b_correct'], 0); self.assertEqual(pair['paired_outcomes']['only_a_correct'], 0)
        self.assertEqual(pair['residual_escape_ratio_a_over_b']['estimate_status'][:14], 'point estimate')
        # a representative suite with zero escapes keeps a nonzero bound (J22)
        rs = self.c.post('/api/v1/portfolios/fault-suite', headers=self.H, json={'name': 'rep', 'seed': 3, 'cases_per_family': 3, 'holdout_per_family': 0, 'families': ['single_item_corrupt', 'burst_corrupt', 'clean'], 'representative_draws': True}).json()
        c2 = self.c.post('/api/v1/portfolios/campaigns', headers=self.H, json={'suite_id': rs['id'], 'portfolio_ids': [full['id']], 'split': 'dev'}).json()['report']['portfolios'][full['id']]
        self.assertEqual(c2['totals']['false_acceptance'], 0); self.assertGreater(c2['statistical']['false_acceptance_upper_95'], 0); self.assertIn('not a zero-error claim', c2['statistical']['zero_failure_note'])
        # held-out use is counted; a second use increments the counter
        h1 = self.c.post('/api/v1/portfolios/campaigns', headers=self.H, json={'suite_id': suite['id'], 'portfolio_ids': [full['id']], 'split': 'held-out'}).json(); h2 = self.c.post('/api/v1/portfolios/campaigns', headers=self.H, json={'suite_id': suite['id'], 'portfolio_ids': [full['id']], 'split': 'held-out'}).json()
        self.assertEqual((h1['report']['holdout_uses'], h2['report']['holdout_uses']), (1, 2))
        # selector: cheapest portfolio covering the required families on the dev split; a model-level gap recommends a measurement source
        sel = self.c.post('/api/v1/portfolios/select', headers=self.H, json={'suite_id': suite['id'], 'required_families': ['single_item_corrupt', 'burst_corrupt', 'wrong_contract_binding', 'heat_initial_condition', 'sampler_mapping']}).json()
        self.assertEqual(sel['selected']['portfolio_id'], full['id'])
        sel2 = self.c.post('/api/v1/portfolios/select', headers=self.H, json={'suite_id': suite['id'], 'required_families': ['shared_wrong_model', 'single_item_corrupt']}).json()
        self.assertIsNone(sel2['selected']); self.assertTrue(sel2['recommendation']['measurement_source'])


if __name__ == '__main__':
    unittest.main()
