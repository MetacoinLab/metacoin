"""Group A (Order 08 §11–§18): WorkTerms v1 draft/validate/freeze/inspect/compare/amend, the declarative acceptance policy
evaluated through one server path, honest-negative economics, and the independent acceptance fixtures of §15 whose
expected decisions are DERIVED FROM THE STATED POLICY (asserted explicitly), not copied from the implementation."""
import json
import unittest

from experiments.work_contracts import fixtures as v0_fixtures
from metacoin_service.tests.test_service import Instance, own_inputs
from metacoin_service.economy import terms as terms_mod, legacy_bridge


class TermsInstance(Instance):
    def __init__(self):
        super().__init__(provider_mode='simulation')


def energy_inputs(outcome):
    return dict(v0_fixtures.inputs(outcome), private_label='TERMS_TEST_' + outcome)


class WorkTermsTests(unittest.TestCase):
    def setUp(self):
        self.inst = TermsInstance(); self.addCleanup(self.inst.close); self.c = self.inst.client; self.H = self.inst.h('owner')
        self.w = self.inst.worker(); self.addCleanup(self.w.offline)

    # ---- helpers ------------------------------------------------------------------------------------------------
    def create(self, template='determination', **kw):
        r = self.c.post('/api/v1/work/terms', headers=self.H, json=dict({'template': template}, **kw))
        self.assertEqual(r.status_code, 201, r.text); return r.json()

    def freeze(self, tid, inputs, **kw):
        r = self.c.post('/api/v1/work/terms/' + tid + '/freeze', headers=self.H, json=dict({'inputs': inputs}, **kw))
        self.assertEqual(r.status_code, 200, r.text); return r.json()

    def run_job(self, contract_id, until=None):
        r = self.c.post('/api/v1/jobs', headers=self.H, json={'contract_id': contract_id}); self.assertEqual(r.status_code, 202, r.text)
        jid = r.json()['id']
        for _ in range(6):
            self.w.run_once()
            j = self.c.get('/api/v1/jobs/' + jid, headers=self.H).json()
            if j['state'] in ('succeeded', 'failed', 'cancelled'):
                break
        return jid, j

    def verify(self, jid, cls='full_exact'):
        r = self.c.post('/api/v1/verification', headers=self.H, json={'job_id': jid, 'class': cls}); self.assertEqual(r.status_code, 202, r.text)
        vid = r.json()['id']
        for _ in range(6):
            self.w.run_once()
            v = self.c.get('/api/v1/verification/' + vid, headers=self.H).json()
            if v['state'] in ('passed', 'failed', 'incomplete'):
                return v
        return v

    def evaluate(self, tid, jid=None, **kw):
        r = self.c.post('/api/v1/work/terms/' + tid + '/evaluate', headers=self.H, json=dict({'job_id': jid} if jid else {}, **kw)); self.assertEqual(r.status_code, 200, r.text); return r.json()

    # ---- draft, validation, inspection ----------------------------------------------------------------------------
    def test_template_validate_inspect_and_unknown_fields_fail(self):
        t = self.create()
        self.assertEqual((t['state'], t['version'], t['kind']), ('draft', 1, 'energy_audit'))
        insp = self.c.get('/api/v1/work/terms/' + t['id'] + '/inspect', headers=self.H).json()
        self.assertEqual(insp['hidden_defaults'], 'none: every consequential field is explicit in the frozen terms')
        self.assertEqual(insp['what_counts_as_delivery']['payment_rule']['outcome_neutral'], True)
        self.assertIn('INFEASIBLE', insp['what_counts_as_delivery']['outcomes'])
        # unknown consequential field fails validation rather than being ignored
        bad = dict(t['terms']); bad['payment'] = dict(bad['payment'], bonus_for_positive=5)
        r = self.c.post('/api/v1/work/terms/validate', headers=self.H, json={'terms': bad})
        self.assertEqual(r.status_code, 200); self.assertFalse(r.json()['valid']); self.assertEqual(r.json()['refusal']['detail']['code'], 'payment_unknown_field')
        bad2 = dict(t['terms']); bad2['milestones'] = [dict(bad2['milestones'][0], depends_on=['m1'])]
        r = self.c.post('/api/v1/work/terms/validate', headers=self.H, json={'terms': bad2}); self.assertEqual(r.json()['refusal']['detail']['code'], 'milestone_dependency_unknown')
        bad3 = dict(t['terms']); bad3['milestones'] = [dict(bad3['milestones'][0], max_payment=999)]
        r = self.c.post('/api/v1/work/terms/validate', headers=self.H, json={'terms': bad3}); self.assertEqual(r.json()['refusal']['detail']['code'], 'milestones_exceed_ceiling')
        # a deliverable type that needs a reviewer without a review predicate is refused
        bad4 = dict(t['terms']); bad4['operation'] = {'kind': 'text_generation', 'compatibility': 'same_model_id'}; bad4['deliverables'] = [{'key': 'x', 'type': 'bounded_model_explanation', 'required': True}]; bad4['eligibility'] = dict(bad4['eligibility'], capabilities=['text_generation'])
        r = self.c.post('/api/v1/work/terms/validate', headers=self.H, json={'terms': bad4}); self.assertIn(r.json()['refusal']['detail']['code'], ('predicate_not_applicable_to_deliverables', 'review_signature_required'))
        templates = self.c.get('/api/v1/work/terms/templates', headers=self.H).json()
        self.assertEqual(set(templates['templates']), {'determination', 'infeasibility_witness', 'independent_replay', 'diagnostic_delivery'})
        # viewer can read but not create
        self.assertEqual(self.c.post('/api/v1/work/terms', headers=self.inst.h('viewer'), json={'template': 'determination'}).status_code, 403)
        self.assertEqual(self.c.get('/api/v1/work/terms/' + t['id'], headers=self.inst.h('viewer')).status_code, 200)

    def test_freeze_binds_operation_and_frozen_terms_never_change(self):
        t = self.create()
        f = self.freeze(t['id'], energy_inputs('FEASIBLE'))
        self.assertEqual(f['state'], 'frozen'); self.assertTrue(f['digest']); op = f['terms']['operation']
        self.assertTrue(op['contract_id'].startswith('ct_')); self.assertEqual(len(op['input_root']), 64); self.assertEqual(op['model_id'], 'outage-energy-bounds/v0')
        self.assertEqual(self.c.post('/api/v1/work/terms/' + t['id'], headers=self.H, json={'terms': {'title': 'x'}}).status_code, 409)
        again = self.c.get('/api/v1/work/terms/' + t['id'], headers=self.H).json(); self.assertEqual(again['digest'], f['digest'])
        up = self.c.get('/api/v1/work/contracts/' + op['contract_id'] + '/upgrade-preview', headers=self.H).json()
        self.assertTrue(up['requires_new_agreement']); self.assertEqual(up['to'], terms_mod.SCHEMA)

    # ---- honest negative economics (§18) ---------------------------------------------------------------------------
    def test_positive_and_negative_determinations_accepted_equally_fabricated_and_unverified_refused(self):
        outcomes = {}
        for want in ('FEASIBLE', 'INFEASIBLE'):
            t = self.create(ceiling=10)
            f = self.freeze(t['id'], energy_inputs(want))
            jid, j = self.run_job(f['contract_id'])
            self.assertEqual((j['state'], j['outcome']), ('succeeded', want))
            before = self.evaluate(t['id'], jid)
            # POLICY-DERIVED expectation: replay predicate unknown until a full_exact verification passes -> pending, no payment
            self.assertEqual((before['decision_candidate'], before['science'], before['execution'], before['payment_class'], before['payable_amount']), ('pending', want, 'completed', 'none', 0))
            self.assertEqual([x['result'] for x in before['trace'] if x['type'] == 'verification_passed'], ['unknown'])
            v = self.verify(jid); self.assertEqual(v['state'], 'passed')
            after = self.evaluate(t['id'], jid)
            self.assertEqual((after['decision_candidate'], after['science'], after['payment_class'], after['payable_amount']), ('accepted', want, 'complete', 10))
            outcomes[want] = after
        self.assertEqual(outcomes['FEASIBLE']['payable_amount'], outcomes['INFEASIBLE']['payable_amount'])       # outcome-neutral: the negative earns the same
        self.assertIn('verified negative', outcomes['INFEASIBLE']['reason'])
        # unverified negative under a policy that requires verification: not accepted (pending), never paid as complete
        t2 = self.create(ceiling=10); f2 = self.freeze(t2['id'], energy_inputs('INFEASIBLE')); jid2, _ = self.run_job(f2['contract_id'])
        ev2 = self.evaluate(t2['id'], jid2); self.assertEqual((ev2['decision_candidate'], ev2['payment_class']), ('pending', 'none'))
        # fabricated positive: a job from ANOTHER contract (other inputs) presented against these terms fails source_revision
        other = self.inst.contract(inputs=own_inputs('OTHER_LABEL_1')); jid3, j3 = self.run_job(other); self.assertEqual(j3['state'], 'succeeded')
        ev3 = self.evaluate(t2['id'], jid3)
        self.assertEqual(ev3['decision_candidate'], 'rejected'); self.assertIn('source', [x['predicate'] for x in ev3['trace'] if x['result'] == 'failed'])
        # INDETERMINATE under the determination template is a diagnostic delivery (policy: accept_diagnostic, diagnostic amount = 5)
        t4 = self.create(ceiling=10); f4 = self.freeze(t4['id'], energy_inputs('INDETERMINATE')); jid4, j4 = self.run_job(f4['contract_id']); self.assertEqual(j4['outcome'], 'INDETERMINATE')
        self.verify(jid4); ev4 = self.evaluate(t4['id'], jid4)
        self.assertEqual((ev4['decision_candidate'], ev4['science'], ev4['payment_class'], ev4['payable_amount']), ('accepted', 'INDETERMINATE', 'diagnostic', 5))

    def test_crash_is_execution_failure_not_a_negative_finding(self):
        t = self.create(ceiling=10)
        f = self.freeze(t['id'], energy_inputs('INFEASIBLE'))
        import os
        os.environ['METACOIN_TEST_EXEC_DELAY_SECONDS'] = '5'
        self.inst.settings.limits['job_timeout_seconds'] = 1; self.inst.settings.limits['job_max_retries'] = 0
        try:
            jid, j = self.run_job(f['contract_id'])
        finally:
            del os.environ['METACOIN_TEST_EXEC_DELAY_SECONDS']
        self.assertEqual((j['state'], j['error_code']), ('failed', 'TIMEOUT'))
        ev = self.evaluate(t['id'], jid)
        self.assertEqual((ev['decision_candidate'], ev['execution'], ev['science'], ev['payment_class']), ('rejected', 'failed', 'no_valid_evidence', 'none'))
        self.assertIn('not a scientific negative', ev['reason'])
        self.assertTrue(all(x['result'] == 'failed' for x in ev['trace']))          # execution-failure record preserved; no predicate passed

    # ---- §15 acceptance fixtures with policy-derived expectations -----------------------------------------------------
    def test_acceptance_fixtures(self):
        reg = legacy_bridge.registry()['task-0018']['registered_hash']
        # 1. correct exact result (legacy replay against the registered hash) -> accepted, complete
        t = self.create('independent_replay', registered_hash=reg, ceiling=3)
        f = self.freeze(t['id'], {'schema': legacy_bridge.INPUT_SCHEMA, 'task_id': 'task-0018'})
        jid, j = self.run_job(f['contract_id']); self.assertEqual((j['state'], j['outcome']), ('succeeded', 'EXACT_MATCH'))
        ev = self.evaluate(t['id'], jid); self.assertEqual((ev['decision_candidate'], ev['payment_class'], ev['payable_amount'], ev['science']), ('accepted', 'complete', 3, 'not_applicable'))
        # 2. one-field tamper: the terms pin a different registered hash -> exact predicate fails -> rejected
        t2 = self.create('independent_replay', registered_hash='0' * 64, ceiling=3)
        f2 = self.freeze(t2['id'], {'schema': legacy_bridge.INPUT_SCHEMA, 'task_id': 'task-0018'})
        jid2, _ = self.run_job(f2['contract_id']); ev2 = self.evaluate(t2['id'], jid2)
        self.assertEqual(ev2['decision_candidate'], 'rejected'); self.assertEqual([x['predicate'] for x in ev2['trace'] if x['result'] == 'failed'], ['exact'])
        # 3. missing artifact: nothing executed -> pending / not_applicable predicates, nothing payable
        t3 = self.create(ceiling=10); f3 = self.freeze(t3['id'], energy_inputs('FEASIBLE'))
        ev3 = self.evaluate(t3['id']); self.assertEqual((ev3['decision_candidate'], ev3['execution'], ev3['payment_class']), ('pending', 'not_started', 'none'))
        self.assertTrue(all(x['result'] == 'not_applicable' for x in ev3['trace']))
        # 4. wrong source revision: evidence of another contract -> source_revision fails
        other = self.inst.contract(inputs=own_inputs('FIXTURE_OTHER')); jid4, _ = self.run_job(other)
        ev4 = self.evaluate(t3['id'], jid4); self.assertEqual(ev4['decision_candidate'], 'rejected'); self.assertIn('source', [x['predicate'] for x in ev4['trace'] if x['result'] == 'failed'])
        # 5. tolerance boundary: numerical_tolerance on worst_margin at exactly abs_tol passes; one unit beyond fails (integer mJ)
        jid5, j5 = self.run_job(f3['contract_id']); self.verify(jid5)
        wm = self.c.get('/api/v1/jobs/' + jid5, headers=self.H).json()['summary']['worst_margin']
        for delta, expect in ((0, 'accepted'), (3, 'accepted'), (4, 'rejected')):
            tt = self.create(ceiling=10, overrides={'acceptance': dict(t3['terms']['acceptance'], predicates=t3['terms']['acceptance']['predicates'] + [{'id': 'tol', 'type': 'numerical_tolerance', 'params': {'field': 'worst_margin', 'expected': wm + delta, 'abs_tol': 3, 'unit': 'mJ'}}])})
            ff = self.freeze(tt['id'], energy_inputs('FEASIBLE'))
            jj, _ = self.run_job(ff['contract_id']); self.verify(jj)
            e = self.evaluate(tt['id'], jj); self.assertEqual(e['decision_candidate'], expect, (delta, e['trace']))
        # 6. unsupported validator revision: the policy pins a verifier digest no installed auditor has -> stays unknown, never accepted
        preds = [p for p in t3['terms']['acceptance']['predicates'] if p['id'] != 'replay'] + [{'id': 'replay', 'type': 'verification_passed', 'params': {'class': 'full_exact', 'distinct_verifier': False, 'verifier_digest': 'f' * 64}}]
        t6 = self.create(ceiling=10, overrides={'acceptance': dict(t3['terms']['acceptance'], predicates=preds)})
        f6 = self.freeze(t6['id'], energy_inputs('FEASIBLE')); jid6, _ = self.run_job(f6['contract_id']); self.verify(jid6)
        ev6 = self.evaluate(t6['id'], jid6); self.assertEqual((ev6['decision_candidate'], ev6['payment_class']), ('pending', 'none'))
        # 7. valid negative under its contract -> accepted (covered above; asserted again on the diagnostic template with amounts 10/3/5)
        t7 = self.create('diagnostic_delivery', ceiling=10); f7 = self.freeze(t7['id'], energy_inputs('INFEASIBLE')); jid7, _ = self.run_job(f7['contract_id']); self.verify(jid7)
        ev7 = self.evaluate(t7['id'], jid7); self.assertEqual((ev7['decision_candidate'], ev7['payment_class'], ev7['payable_amount']), ('accepted', 'complete', 10))

    # ---- amendments, counteroffers, supersession (§17) --------------------------------------------------------------------
    def test_amendments_are_new_revisions_with_schema_classified_differences(self):
        t = self.create(ceiling=10); f = self.freeze(t['id'], energy_inputs('FEASIBLE'))
        # cosmetic: title only
        a = self.c.post('/api/v1/work/terms/' + t['id'] + '/amend', headers=self.H, json={'terms': {'title': 'renamed determination'}, 'expected_version': 1}); self.assertEqual(a.status_code, 201, a.text); a = a.json()
        self.assertEqual((a['version'], a['previous_id'], a['difference']['consequential'], a['requires_new_agreement']), (2, t['id'], False, False))
        self.assertEqual(a['difference']['changes'][0]['classification'], 'cosmetic')
        # a second pending amendment on the same version is refused (no two current revisions)
        self.assertEqual(self.c.post('/api/v1/work/terms/' + t['id'] + '/amend', headers=self.H, json={'terms': {'title': 'other'}}).status_code, 409)
        # stale version check
        self.assertEqual(self.c.post('/api/v1/work/terms/' + t['id'] + '/amend', headers=self.H, json={'terms': {'title': 'z'}, 'expected_version': 7}).status_code, 409)
        # freeze the revision -> the old one is explicitly superseded, its digest and terms untouched
        fr = self.c.post('/api/v1/work/terms/' + a['id'] + '/freeze', headers=self.H, json={}); self.assertEqual(fr.status_code, 200, fr.text); fr = fr.json()
        self.assertEqual((fr['state'], fr['superseded']), ('frozen', t['id']))
        old = self.c.get('/api/v1/work/terms/' + t['id'], headers=self.H).json(); self.assertEqual((old['state'], old['digest'], old['superseded_by']), ('superseded', f['digest'], a['id']))
        self.assertNotEqual(fr['digest'], f['digest'])
        # consequential: tightening acceptance after evidence exists creates a new agreement; the old revision's meaning is unchanged
        jid, _ = self.run_job(f['contract_id'])
        b = self.c.post('/api/v1/work/terms/' + a['id'] + '/amend', headers=self.H, json={'terms': {'acceptance': dict(fr['terms']['acceptance'], outcomes={'FEASIBLE': 'accept', 'INFEASIBLE': 'reject', 'INDETERMINATE': 'reject'}, payment_rule=dict(fr['terms']['acceptance']['payment_rule'], outcome_neutral=False))}}).json()
        self.assertTrue(b['difference']['consequential']); self.assertTrue(b['requires_new_agreement'])
        self.assertTrue(any(c['path'].startswith('acceptance.outcomes') for c in b['difference']['changes']))
        cmp = self.c.get('/api/v1/work/terms/' + a['id'] + '/compare/' + b['id'], headers=self.H).json(); self.assertTrue(cmp['consequential'])
        # payment destination / recipient changes are consequential too
        d = self.c.post('/api/v1/work/terms/' + b['id'] + '/withdraw', headers=self.H).json(); self.assertEqual(d['state'], 'withdrawn')
        e = self.c.post('/api/v1/work/terms/' + a['id'] + '/amend', headers=self.H, json={'terms': {'deadlines': dict(fr['terms']['deadlines'], delivery_seconds=2 * 86400)}}).json()
        self.assertTrue(e['difference']['consequential']); self.assertEqual(e['difference']['changes'][0]['path'], 'deadlines.delivery_seconds')
        # the old frozen evaluation still uses the old policy
        ev = self.evaluate(a['id'], jid); self.assertEqual(ev['terms_digest'], fr['digest'])


if __name__ == '__main__':
    unittest.main()
