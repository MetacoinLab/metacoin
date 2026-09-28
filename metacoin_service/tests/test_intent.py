"""Group C without a model: deterministic eligibility filtering, clarification with a bound continuation, abstention,
grant-limited eligibility, plan refusal surfaced as unresolved, fully specified requests proceeding without friction."""
import unittest

from metacoin_service.tests.test_compute_engine import ComputeInstance, HAVE_RUNTIME, batch_spec, heat_spec
from metacoin_service.tests.test_agents import policy


@unittest.skipUnless(HAVE_RUNTIME, 'no compute interpreter')
class IntentTests(unittest.TestCase):
    def setUp(self):
        self.inst = ComputeInstance(); self.addCleanup(self.inst.close); self.c = self.inst.client; self.H = self.inst.h('owner')

    def compile(self, req, headers=None):
        r = self.c.post('/api/v1/agents/intents', headers=headers or self.H, json={'request': req}); self.assertEqual(r.status_code, 201, r.text); return r.json()

    def test_fully_specified_request_becomes_a_plan_without_friction(self):
        v = self.compile({'text': 'Sweep the reserve of the demo battery over the declared grid and audit it.', 'inputs': batch_spec(private_label='INT'), 'verify': 'analytical'})
        self.assertEqual(v['state'], 'plan'); it = v['intent']
        self.assertEqual(it['service_kind'], 'temporal_batch'); self.assertEqual(it['provenance']['service_choice'], 'single_eligible_service'); self.assertEqual(it['unresolved'], [])
        self.assertTrue(v['plan_id']); plan = self.c.get('/api/v1/agents/plans/' + v['plan_id'], headers=self.H).json()
        self.assertTrue(plan['valid']); self.assertEqual([s['operation'] for s in plan['resolved']], ['invoke', 'verification_request'])
        self.assertEqual(self.c.get('/api/v1/jobs', headers=self.H).json()['items'], [])                                   # compiling executes nothing
        self.assertTrue(any('Excluded: heat_diffusion' in line for line in v['explanation']))                             # typed schema excludes other services with a reason

    def test_missing_units_clarification_and_bound_continuation(self):
        v = self.compile({'text': 'Check whether the battery keeps a reserve of 2000 over the next 3600 with a load of 450.'})
        self.assertEqual(v['state'], 'clarification'); fields = {u['field']: u for u in v['intent']['unresolved']}
        self.assertIn('units', fields); self.assertIn('mJ', fields['units']['choices']); self.assertTrue(v['continuation_token'])
        self.assertEqual(self.c.get('/api/v1/jobs', headers=self.H).json()['items'], [])
        # wrong token refused; an answer outside the allowed choices refused; the right answers continue the SAME draft
        self.assertEqual(self.c.post('/api/v1/agents/intents/%s/continue' % v['id'], headers=self.H, json={'token': 'nope', 'answers': {'units': 'mJ'}}).status_code, 403)
        bad = self.c.post('/api/v1/agents/intents/%s/continue' % v['id'], headers=self.H, json={'token': v['continuation_token'], 'answers': {'units': 'furlongs'}}).json()
        self.assertEqual(bad['state'], 'clarification'); self.assertIn('answer_not_allowed', [x['code'] for x in bad['intent']['validation']])
        kinds = [u for u in bad['intent']['unresolved'] if u['field'] == 'kind']
        cont = self.c.post('/api/v1/agents/intents/%s/continue' % v['id'], headers=self.H, json={'token': bad['continuation_token'], 'answers': {'units': 'mJ', 'kind': 'temporal_batch'}, 'inputs': batch_spec(private_label='INT2')}).json()
        self.assertEqual((cont['id'], cont['state']), (v['id'], 'plan')); self.assertEqual(cont['intent']['answers']['units'], 'mJ'); self.assertEqual(cont['intent']['service_kind'], 'temporal_batch')
        self.assertEqual(cont['intent']['assumptions'].get('units'), 'mJ')

    def test_abstention_and_grant_scope(self):
        v = self.compile({'text': 'Book a launch window with the range safety office and email the crew.'})
        self.assertEqual(v['state'], 'abstention'); self.assertIn('no authorized supported service', v['intent']['disposition_reason'])
        # an agent grant limited to temporal_energy: temporal_batch is excluded with the grant reason, so the typed request cannot proceed
        g = self.c.post('/api/v1/agents/grants', headers=self.H, json={'policy': policy(permitted_services=['temporal_energy'])}).json()
        A = {'Authorization': 'Bearer ' + g['token']}
        v2 = self.compile({'text': 'run the batch sweep', 'inputs': batch_spec(private_label='INT3')}, headers=A)
        self.assertNotEqual(v2['state'], 'plan')
        tb = next(e for e in v2['intent']['eligibility'] if e['kind'] == 'temporal_batch'); self.assertFalse(tb['eligible']); self.assertTrue(any('grant' in r for r in tb['reasons']))
        self.assertEqual(self.c.get('/api/v1/jobs', headers=self.H).json()['items'], [])

    def test_invalid_typed_inputs_surface_as_unresolved_not_dispatch(self):
        v = self.compile({'text': 'sweep', 'kind': 'temporal_batch', 'inputs': dict(batch_spec(), steps=-5)})
        self.assertEqual(v['state'], 'clarification'); self.assertTrue(any(u['field'] == 'inputs' and 'inputs_invalid' in u['reason'] for u in v['intent']['unresolved']))
        self.assertEqual(self.c.get('/api/v1/jobs', headers=self.H).json()['items'], [])
        self.assertEqual(self.c.get('/api/v1/agents/intents/' + v['id'], headers=self.inst.h('viewer')).json()['id'], v['id'])
        self.assertEqual(len(self.c.get('/api/v1/agents/intents', headers=self.H).json()['items']), 1)
