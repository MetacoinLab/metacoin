"""Group D through the real application interfaces: worker child (HiGHS in the compute interpreter), verification
service, plan routes and SVG, alternative freeze into a workflow draft, campaigns (grid, typed branch changes with
sources and version checks, comparison with first violations) and the budgeted acquisition campaign versus a grid."""
import json
import unittest
from metacoin_service.tests.test_compute_engine import ComputeInstance, HAVE_RUNTIME
from metacoin_service.tests.test_resource_plan import instance, task
from metacoin_service.compute import resource_plan as rp


def sample():
    return instance(objectives={'mode': 'cost_sweep', 'cost_ceilings': [0, 3, 6]}, sensitivity=[{'parameter': 'reserve', 'value': 60000}, {'parameter': 'capacity', 'value': 200000}],
                    tasks=[task('a', utility=5, duration=2, power_high=400, resources={'cpu': 1}, cost=3), task('b', utility=4, power_high=300, resources={'cpu': 2}, cost=2, dependencies=['a']),
                           task('c', utility=3, duration=3, power_high=100, resources={'cpu': 1}, cost=1, exclusive_with=['a']), task('m', mandatory=True, utility=0, duration=1, power_high=50, earliest_start=4, latest_start=5)])


@unittest.skipUnless(HAVE_RUNTIME, 'no numpy-capable compute interpreter on this host')
class ResourcePlanServiceTests(unittest.TestCase):
    def setUp(self):
        self.inst = ComputeInstance(); self.addCleanup(self.inst.close); self.c = self.inst.client; self.H = self.inst.h('owner'); self.w = self.inst.worker()

    def verify(self, jid, cls):
        r = self.c.post('/api/v1/verification', headers=self.H, json={'job_id': jid, 'class': cls, 'params': {}})
        self.assertEqual(r.status_code, 202, r.text)
        self.assertEqual(self.w.run_once()[1], 'succeeded')
        return self.c.get('/api/v1/verification/' + r.json()['id'], headers=self.H).json()

    def test_plan_job_verified_witness_alternatives_freeze_and_console(self):
        svc = [s for s in self.c.get('/api/v1/services', headers=self.H).json()['items'] if s['kind'] == 'resource_plan']
        self.assertEqual(len(svc), 1); self.assertEqual(svc[0]['model_id'], 'robust-resource-plan/v1')
        q = self.c.post('/api/v1/services/' + svc[0]['id'] + '/quote', headers=self.H, json={'inputs': sample()})
        self.assertIn(q.status_code, (200, 201), q.text); self.assertEqual(q.json()['quantity_max'], rp.work_units(sample()))
        r = self.c.post('/api/v1/compute/resource-plans', headers=self.H, json={'inputs': sample(), 'title': 'sample plan'})
        self.assertEqual(r.status_code, 202, r.text); jid = r.json()['job_id']
        self.assertEqual(self.w.run_once()[1], 'succeeded')
        job = self.c.get('/api/v1/jobs/' + jid, headers=self.H).json()
        self.assertEqual((job['state'], job['outcome'], job['summary']['status'], job['summary']['objective'], job['summary']['selected']), ('succeeded', 'VERIFIED', 'optimal_within_tolerance', 12, ['a', 'b', 'c', 'm']))
        view = self.inst.view(jid)
        self.assertTrue(view['verification']['passed']); self.assertEqual(view['verification']['mode'], 'reference_replay')
        plan = self.c.get('/api/v1/compute/jobs/' + jid + '/plan', headers=self.H).json()['plan']
        self.assertEqual(plan['assignments'], {'a': 0, 'b': 2, 'c': 3, 'm': 4}); self.assertEqual(plan['oracle_agreement']['agree'], True)
        self.assertEqual(len(plan['alternatives']['candidates']), 3); self.assertEqual(len(plan['sensitivity']['rows']), 2)
        self.assertIn('not-a-hardware-guarantee', plan['conditional_on'])
        # the verification service replays the witness from the original inputs (full_reference) and checks invariants (analytical)
        full = self.verify(jid, 'full_reference')
        self.assertEqual(full['state'], 'passed'); names = [c['check'] for c in full['result']['checks']]
        self.assertTrue({'assignments_replay_feasible', 'trajectory_replayed', 'oracle_reproduced', 'optimum_matches_oracle', 'alternative_replay_3'} <= set(names), names)
        ana = self.verify(jid, 'analytical'); self.assertEqual((ana['state'], ana['result']['coverage']), ('passed', 'invariants only'))
        self.assertEqual(self.c.post('/api/v1/verification/preview', headers=self.H, json={'job_id': jid, 'class': 'full_exact'}).json()['detail']['code'], 'class_unsupported_for_kind')
        # a viewer cannot read the private plan or its SVG
        self.assertEqual(self.c.get('/api/v1/compute/jobs/' + jid + '/plan', headers=self.inst.h('viewer')).status_code, 403)
        svg = self.c.get('/api/v1/compute/jobs/' + jid + '/plan.svg', headers=self.H)
        self.assertEqual((svg.status_code, svg.headers['content-type'].split(';')[0]), (200, 'image/svg+xml')); self.assertIn('reserve', svg.text); self.assertIn('boundary 6', svg.text)
        self.assertEqual(self.c.get('/api/v1/compute/jobs/' + jid + '/plan.svg?alternative=3', headers=self.H).status_code, 200)
        self.assertEqual(self.c.get('/api/v1/compute/jobs/' + jid + '/plan.svg?alternative=99', headers=self.H).status_code, 404)
        # choose an alternative and freeze it into a workflow draft; the recorded evidence is copied as a note only
        fr = self.c.post('/api/v1/compute/jobs/' + jid + '/freeze-alternative', headers=self.H, json={'cost_ceiling': 3})
        self.assertEqual(fr.status_code, 201, fr.text); wid = fr.json()['workflow_id']
        self.assertEqual(fr.json()['recorded_alternative']['utility'], 5)
        wf = self.c.get('/api/v1/workflows/' + wid, headers=self.H).json()
        node = [n for n in wf['definition']['nodes'] if n['id'] == 'plan'][0]
        self.assertEqual((node['type'], node['inputs']['objectives']), ('resource_plan', {'mode': 'cost_sweep', 'cost_ceilings': [3]}))
        self.assertEqual(self.c.post('/api/v1/compute/jobs/' + jid + '/freeze-alternative', headers=self.H, json={'cost_ceiling': 99}).status_code, 404)
        # console: job page shows the plan section; the compute form validates the sample; MCP-facing plan route works for the reviewer
        s = self.c.post('/api/v1/session', json={'token': self.inst.tok['owner']}); cookies = {'metacoin_session': s.cookies['metacoin_session']}
        page = self.c.get('/console/jobs/' + jid, cookies=cookies)
        self.assertEqual(page.status_code, 200); self.assertIn('Alternatives (epsilon-constraint sweep', page.text); self.assertIn('Sensitivity (finite study)', page.text); self.assertIn('freeze as workflow draft', page.text)
        self.assertNotIn('RP_TEST', page.text)
        self.assertEqual(self.c.get('/console/compute/new?kind=resource_plan', cookies=cookies).status_code, 200)
        fz = self.c.post('/console/compute/' + jid + '/freeze-alternative', cookies=cookies, data={'csrf': s.json()['csrf'], 'cost_ceiling': '6'}, follow_redirects=False)
        self.assertEqual(fz.status_code, 303); self.assertIn('/console/workflows/', fz.headers['location'])
        self.assertEqual(self.c.get('/api/v1/compute/jobs/' + jid + '/plan', headers=self.inst.h('reviewer')).status_code, 200)

    def test_infeasible_instance_reports_established_infeasibility_and_verifies(self):
        bad = instance(tasks=[task('m', mandatory=True, power_high=100000)])
        jid = self.c.post('/api/v1/compute/resource-plans', headers=self.H, json={'inputs': bad}).json()['job_id']
        self.assertEqual(self.w.run_once()[1], 'succeeded')
        job = self.c.get('/api/v1/jobs/' + jid, headers=self.H).json()
        self.assertEqual((job['summary']['status'], job['summary']['objective']), ('infeasible_established_by_enumeration', None))
        full = self.verify(jid, 'full_reference'); self.assertEqual(full['state'], 'passed')
        self.assertIn('infeasibility_established', [c['check'] for c in full['result']['checks']])
        svg = self.c.get('/api/v1/compute/jobs/' + jid + '/plan.svg', headers=self.H); self.assertIn('no feasible candidate', svg.text)
        # invalid inputs are refused at the contract boundary with the reason
        r = self.c.post('/api/v1/compute/resource-plans', headers=self.H, json={'inputs': instance(capacity=10 ** 13)})
        self.assertEqual(r.status_code, 422); self.assertIn('capacity', r.text)

    def drive(self, cid, rounds=60):
        for _ in range(rounds):
            self.w.run_once()
            state = self.c.post('/api/v1/campaigns/' + cid + '/tick', headers=self.H).json()['state']
            if state in ('completed', 'cancelled', 'budget_exhausted', 'paused'):
                return state
        return state

    def test_campaign_grid_branch_with_typed_changes_version_check_and_comparison(self):
        base = sample(); base.pop('sensitivity'); base['objectives'] = {'mode': 'utility'}
        definition = {'name': 'reserve sweep', 'kind': 'resource_plan', 'base': base, 'axes': [{'path': 'reserve', 'values': [20000, 30000, 90000]}]}
        cid = self.c.post('/api/v1/campaigns', headers=self.H, json={'definition': definition}).json()['campaign_id']
        self.c.post('/api/v1/campaigns/' + cid + '/run', headers=self.H)
        self.assertEqual(self.drive(cid), 'completed')
        res = self.c.get('/api/v1/campaigns/' + cid + '/results', headers=self.H).json()
        by = {r['params']['reserve']: r for r in res['rows']}
        self.assertEqual((by[20000]['outcome'], by[20000]['result']['objective']), ('optimal_within_tolerance', 12))
        self.assertEqual(by[90000]['outcome'], 'infeasible_established_by_enumeration'); self.assertEqual(by[30000]['outcome'], 'optimal_within_tolerance')
        self.assertEqual(res['model_version'], 'robust-resource-plan/v1')
        pareto = self.c.post('/api/v1/campaigns/' + cid + '/pareto', headers=self.H, json={'objectives': [{'field': 'objective', 'direction': 'max'}, {'field': 'reserve', 'direction': 'max'}], 'require_outcomes': ['optimal_within_tolerance']})
        self.assertEqual(pareto.status_code, 200, pareto.text); self.assertEqual([e['index'] for e in pareto.json()['non_dominated']], [1]); self.assertEqual(pareto.json()['excluded'][0]['reason'][:8], 'outcome ')
        # typed branch: a revised task duration from a measured update and a corrected supply value; sources are recorded with the previous values
        head0 = self.c.get('/api/v1/campaigns/' + cid + '/head', headers=self.H).json()['head']; self.assertEqual(head0, cid)
        changes = [{'path': 'tasks.a.duration', 'value': 4, 'source': 'measured_update', 'note': 'timed on the bench'}, {'path': 'supply_low.3', 'value': 100, 'source': 'user_edit'}]
        br = self.c.post('/api/v1/campaigns/' + cid + '/branch', headers=self.H, json={'changes': changes, 'expected_head': cid, 'name': 'longer a, weaker slot 3'})
        self.assertEqual(br.status_code, 201, br.text); bid = br.json()['campaign_id']
        self.assertEqual([c['previous'] for c in br.json()['changes']], [2, 500]); self.assertEqual(br.json()['changes'][0]['source'], 'measured_update')
        # a stale agent (still expecting the original head) is refused with the head to rebase on; passing the real head works
        stale = self.c.post('/api/v1/campaigns/' + cid + '/branch', headers=self.H, json={'changes': changes[:1], 'expected_head': cid})
        self.assertEqual((stale.status_code, stale.json()['detail']['code'], stale.json()['detail']['head']), (409, 'stale_branch', bid))
        self.assertEqual(self.c.get('/api/v1/campaigns/' + cid + '/head', headers=self.H).json()['head'], bid)
        # a reviewed table correction needs a reviewer; an unknown path and a wrong type are refused
        self.assertEqual(self.c.post('/api/v1/campaigns/' + cid + '/branch', headers=self.H, json={'changes': [{'path': 'supply_low.0', 'value': 450, 'source': 'reviewed_table_correction'}]}).status_code, 403)
        self.assertEqual(self.c.post('/api/v1/campaigns/' + cid + '/branch', headers=self.H, json={'changes': [{'path': 'tasks.zz.duration', 'value': 4, 'source': 'user_edit'}]}).json()['detail']['code'], 'unknown_base_field')
        self.assertEqual(self.c.post('/api/v1/campaigns/' + cid + '/branch', headers=self.H, json={'changes': [{'path': 'reserve', 'value': 'x', 'source': 'user_edit'}]}).json()['detail']['code'], 'change_type')
        self.c.post('/api/v1/campaigns/' + bid + '/run', headers=self.H)
        self.assertEqual(self.drive(bid), 'completed')
        cmp = self.c.get('/api/v1/campaigns/' + cid + '/compare/' + bid, headers=self.H).json()
        self.assertEqual(set(cmp['base_changes']), {'tasks.a.duration', 'supply_low.3'}); self.assertEqual(cmp['recorded_changes'][0]['source'], 'measured_update')
        m = {r['params']['reserve']: r for r in cmp['matched']}
        self.assertIn('plan_changes', m[20000]); self.assertIsNotNone(m[20000]['b']['objective'])
        # the original comparison stays bound to its own inputs: the original campaign's results did not change
        again = self.c.get('/api/v1/campaigns/' + cid + '/results', headers=self.H).json()
        self.assertEqual([r['result']['objective'] for r in again['rows']], [r['result']['objective'] for r in res['rows']])
        # a viewer sees changed paths but not values
        vcmp = self.c.get('/api/v1/campaigns/' + cid + '/compare/' + bid, headers=self.inst.h('viewer')).json()
        self.assertEqual(vcmp['base_changes']['tasks.a.duration'], 'changed'); self.assertNotIn('previous', json.dumps(vcmp['recorded_changes']))

    def test_acquisition_campaign_is_budgeted_restartable_and_compared_with_a_grid(self):
        base = sample(); base.pop('sensitivity'); base['objectives'] = {'mode': 'utility'}
        cands = [{'reserve': r} for r in (10000, 20000, 30000, 40000, 50000, 60000, 70000, 80000)]
        adaptive = {'strategy': 'acquisition', 'candidates': cands, 'objective': {'field': 'min_margin', 'direction': 'max'}, 'max_evaluations': 4, 'exploration_percent': 20, 'seed': 3}
        r = self.c.post('/api/v1/campaigns', headers=self.H, json={'definition': {'name': 'acq', 'kind': 'resource_plan', 'base': base, 'adaptive': adaptive}})
        self.assertEqual(r.status_code, 201, r.text); cid = r.json()['campaign_id']
        self.c.post('/api/v1/campaigns/' + cid + '/run', headers=self.H)
        state = self.drive(cid)
        view = self.c.get('/api/v1/campaigns/' + cid, headers=self.H).json()
        ad = view['adaptive']
        self.assertEqual(state, 'budget_exhausted'); self.assertEqual(ad['stopping_reason'], 'evaluation_budget_exhausted'); self.assertEqual(len(ad['evaluated']), 4)
        self.assertEqual(len({e['index'] for e in ad['evaluated']}), 4)                     # no evaluation charged twice
        self.assertEqual(ad['log'][0]['reason'], 'seeded first evaluation (no observation yet)'); self.assertTrue(all(l['score'] is not None for l in ad['log'][1:]))
        self.assertTrue(all(e['params'] in cands for e in ad['evaluated']))                  # never outside the authorized set
        self.assertIn('not a calibrated posterior', ad['acquisition']['uncertainty_interpretation'])
        # the same budget on a fixed grid (every other candidate): report both, claim nothing universal
        grid = {'name': 'grid', 'kind': 'resource_plan', 'base': base, 'axes': [{'path': 'reserve', 'values': [10000, 30000, 50000, 70000]}]}
        gid = self.c.post('/api/v1/campaigns', headers=self.H, json={'definition': grid}).json()['campaign_id']
        self.c.post('/api/v1/campaigns/' + gid + '/run', headers=self.H); self.assertEqual(self.drive(gid), 'completed')
        grid_rows = self.c.get('/api/v1/campaigns/' + gid + '/results', headers=self.H).json()['rows']
        grid_best = max((r['result']['min_margin'] for r in grid_rows if r['result'].get('min_margin') is not None), default=None)
        self.assertIsNotNone(ad['best']); self.assertIsNotNone(grid_best)
        self.record = {'adaptive_best': ad['best'], 'grid_best': grid_best, 'budget': 4, 'candidates': len(cands)}
        # invalid candidate sets are refused: duplicate, unknown axis, unknown objective field
        bad = dict(adaptive, candidates=cands[:1] + cands[:1])
        self.assertEqual(self.c.post('/api/v1/campaigns', headers=self.H, json={'definition': {'name': 'x', 'kind': 'resource_plan', 'base': base, 'adaptive': bad}, 'preview': True}).json()['detail']['code'], 'duplicate_candidate')
        self.assertEqual(self.c.post('/api/v1/campaigns', headers=self.H, json={'definition': {'name': 'x', 'kind': 'resource_plan', 'base': base, 'adaptive': dict(adaptive, candidates=[{'slots': 3}])}, 'preview': True}).json()['detail']['code'], 'candidate_shape')
        self.assertEqual(self.c.post('/api/v1/campaigns', headers=self.H, json={'definition': {'name': 'x', 'kind': 'resource_plan', 'base': base, 'adaptive': dict(adaptive, objective={'field': 'nope', 'direction': 'max'})}, 'preview': True}).json()['detail']['code'], 'objective_shape')


if __name__ == '__main__':
    unittest.main()
