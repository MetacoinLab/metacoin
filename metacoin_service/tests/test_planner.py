"""§51 structured planning: a valid bounded plan; hallucinated service; invented schema field; hidden extra node;
over-budget plan; scope escalation; refused plans stay validation errors; acceptance executes once (idempotent) and
a planning retry never creates a job. The model-assisted path with prompt injection from retrieved text is in
test_planner_model (needs the pinned models)."""
import json
import unittest

from metacoin_service.tests.test_compute_engine import ComputeInstance, HAVE_RUNTIME, batch_spec
from metacoin_service.tests.test_agents import policy


@unittest.skipUnless(HAVE_RUNTIME, 'no numpy-capable compute interpreter on this host')
class PlannerTests(unittest.TestCase):
    def setUp(self):
        self.inst = ComputeInstance(); self.addCleanup(self.inst.close); self.c = self.inst.client; self.H = self.inst.h('owner')

    def agent(self, **over):
        g = self.c.post('/api/v1/agents/grants', headers=self.H, json={'policy': policy(**over)})
        self.assertEqual(g.status_code, 201, g.text)
        return g.json()['grant_id'], {'Authorization': 'Bearer ' + g.json()['token']}

    def jobs(self):
        return [j['id'] for j in self.c.get('/api/v1/jobs?limit=50', headers=self.H).json()['items']]

    def test_valid_plan_refusals_and_idempotent_acceptance(self):
        spec = batch_spec(private_label='PLAN')
        r = self.c.post('/api/v1/agents/plans', headers=self.H, json={'goal': 'sweep the reserve for the demo battery', 'kind': 'temporal_batch', 'inputs': spec, 'verify': 'analytical'})
        self.assertEqual(r.status_code, 201, r.text); p = r.json()
        self.assertTrue(p['valid']); self.assertEqual([x['operation'] for x in p['resolved']], ['invoke', 'verification_request'])
        self.assertGreater(p['resolved'][0]['exposure']['amount_max'], 0); self.assertIn('no quote created', p['resolved'][0]['exposure']['basis'])
        self.assertTrue(any(line.startswith('s1: invoke temporal_batch') for line in p['readable'])); self.assertFalse(p['auto_execute_permitted'])
        self.assertEqual(self.jobs(), [])                                                                  # planning executed nothing
        # planning again (a retry) creates another draft, still no job and no quote
        p2 = self.c.post('/api/v1/agents/plans', headers=self.H, json={'goal': 'sweep again', 'kind': 'temporal_batch', 'inputs': spec}).json()
        self.assertEqual(self.jobs(), []); self.assertEqual(self.c.get('/api/v1/quotes', headers=self.H).status_code in (200, 404), True)
        # hallucinated service, invented schema field, hidden extra node, too many steps
        bad = self.c.post('/api/v1/agents/plans', headers=self.H, json={'goal': 'x', 'kind': 'quantum_oracle', 'inputs': spec}).json()
        self.assertEqual((bad['valid'], bad['refusals'][0]['code']), (False, 'unknown_service'))
        bad = self.c.post('/api/v1/agents/plans', headers=self.H, json={'goal': 'x', 'kind': 'temporal_batch', 'inputs': dict(spec, transfer_to='0xabc')}).json()
        self.assertEqual((bad['valid'], bad['refusals'][0]['code']), (False, 'inputs_invalid')); self.assertIn('unexpected', json.dumps(bad['refusals'][0]['detail']))
        bad = self.c.post('/api/v1/agents/plans', headers=self.H, json={'goal': 'x', 'kind': 'temporal_batch', 'inputs': dict(spec, steps=-5)}).json()
        self.assertEqual((bad['valid'], bad['refusals'][0]['code']), (False, 'inputs_invalid'))
        hidden = {'schema': 'metacoin-agent-plan/v1', 'goal': 'x', 'steps': [{'id': 's1', 'operation': 'invoke', 'service_kind': 'temporal_batch', 'inputs': spec, 'depends_on': []},
                                                                            {'id': 's2', 'operation': 'action:create', 'depends_on': [], 'inputs': {'amount': 10 ** 9}}]}
        bad = self.c.post('/api/v1/agents/plans', headers=self.H, json={'goal': 'x', 'draft': hidden}).json()
        self.assertEqual((bad['valid'], [x['code'] for x in bad['refusals']]), (False, ['operation_not_allowed']))
        big = dict(hidden, steps=[{'id': 's%d' % i, 'operation': 'invoke', 'service_kind': 'temporal_batch', 'inputs': spec, 'depends_on': []} for i in range(9)])
        self.assertEqual(self.c.post('/api/v1/agents/plans', headers=self.H, json={'goal': 'x', 'draft': big}).json()['refusals'][0]['code'], 'graph_limit')
        sneaky = dict(hidden, steps=[dict(hidden['steps'][0], recipient='0xevil')])
        self.assertEqual(self.c.post('/api/v1/agents/plans', headers=self.H, json={'goal': 'x', 'draft': sneaky}).json()['refusals'][0]['code'], 'unknown_field')
        # a refused plan cannot be accepted; the valid one executes exactly once
        self.assertEqual(self.c.post('/api/v1/agents/plans/' + bad['id'] + '/accept', headers=self.H).status_code, 409)
        acc = self.c.post('/api/v1/agents/plans/' + p['id'] + '/accept', headers=self.H); self.assertEqual(acc.status_code, 200, acc.text); acc = acc.json()
        self.assertEqual(acc['state'], 'executed'); jid = acc['execution']['steps'][0]['job_id']; self.assertEqual(self.jobs(), [jid])
        self.assertEqual(acc['execution']['steps'][1]['target_job_id'], jid)
        again = self.c.post('/api/v1/agents/plans/' + p['id'] + '/accept', headers=self.H).json()
        self.assertEqual((again['execution']['steps'][0]['job_id'], self.jobs()), (jid, [jid]))
        self.assertEqual(self.c.get('/api/v1/quotes/' + acc['execution']['steps'][0]['quote_id'], headers=self.H).json()['state'], 'consumed')
        self.assertEqual(self.c.get('/api/v1/agents/plans/' + p['id'], headers=self.inst.h('viewer')).json()['digest'], p['digest'])

    def test_agent_grants_bound_planning_and_execution(self):
        spec = batch_spec(private_label='AG')
        # scope escalation: the grant permits temporal_energy only
        gid, A = self.agent(permitted_services=['temporal_energy'])
        p = self.c.post('/api/v1/agents/plans', headers=A, json={'goal': 'x', 'kind': 'temporal_batch', 'inputs': spec}).json()
        self.assertEqual([x['code'] for x in p['refusals']], ['service_not_permitted'])
        # over budget: per-action ceiling below the exposure of one batch
        gid, A = self.agent(permitted_services=['temporal_batch'], ceilings={'total_amount': 1, 'per_action_amount': 1, 'max_jobs': 2, 'max_workflows': 1, 'concurrency': 2})
        p = self.c.post('/api/v1/agents/plans', headers=A, json={'goal': 'x', 'kind': 'temporal_batch', 'inputs': spec}).json()
        self.assertIn('exposure_exceeds_ceiling', [x['code'] for x in p['refusals']]); self.assertEqual(self.jobs(), [])
        # a covered plan under a grant with a mandatory review gate needs a workspace decision; the agent cannot self-accept
        gid, A = self.agent(permitted_services=['temporal_batch'], ceilings={'total_amount': 10 ** 6, 'per_action_amount': 10 ** 6, 'max_jobs': 1, 'max_workflows': 1, 'concurrency': 2})
        p = self.c.post('/api/v1/agents/plans', headers=A, json={'goal': 'x', 'kind': 'temporal_batch', 'inputs': spec}).json()
        self.assertTrue(p['valid']); self.assertFalse(p['auto_execute_permitted'])
        self.assertEqual(self.c.post('/api/v1/agents/plans/' + p['id'] + '/accept', headers=A).json()['detail']['code'], 'decision_required')
        self.assertEqual(self.jobs(), [])
        # verification step needs a permission the agent lacks
        p2 = self.c.post('/api/v1/agents/plans', headers=A, json={'goal': 'x', 'kind': 'temporal_batch', 'inputs': spec, 'verify': 'analytical'}).json()
        self.assertEqual([x['code'] for x in p2['refusals']], ['operation_not_granted'])
        # explicit automatic execution: a grant without the mandatory gate covers exactly one invoke -> the agent may accept; the job ceiling then blocks a second
        gid, A = self.agent(permitted_services=['temporal_batch'], review_gate_mandatory=False, ceilings={'total_amount': 10 ** 6, 'per_action_amount': 10 ** 6, 'max_jobs': 1, 'max_workflows': 1, 'concurrency': 2})
        p = self.c.post('/api/v1/agents/plans', headers=A, json={'goal': 'x', 'kind': 'temporal_batch', 'inputs': spec}).json()
        self.assertTrue(p['auto_execute_permitted'])
        acc = self.c.post('/api/v1/agents/plans/' + p['id'] + '/accept', headers=A); self.assertEqual(acc.status_code, 200, acc.text)
        self.assertEqual(len(self.jobs()), 1)
        p3 = self.c.post('/api/v1/agents/plans', headers=A, json={'goal': 'x', 'kind': 'temporal_batch', 'inputs': spec}).json()
        self.assertIn('job_ceiling', [x['code'] for x in p3['refusals']])
        g = self.c.get('/api/v1/agents/grants/' + gid, headers=self.H).json()
        self.assertEqual(g['counters']['jobs_created'], 1)
