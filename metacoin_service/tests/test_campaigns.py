"""Campaigns: deterministic grids, invalid/failed candidates, budget limits, resume after restart,
pause/cancel, adaptive bisection vs exhaustive reference, monotonicity refusal, Pareto, refinement."""
import json
import unittest
from metacoin_service.tests.test_service import Instance
from metacoin_service.tests.test_workflows import CSV_OK
from metacoin_service import campaigns, temporal


def base_temporal(**kw):
    d = {'schema': temporal.INPUT_SCHEMA, 'capacity': 10_000, 'initial_low': 6_000, 'initial_high': 6_000, 'reserve': 2_000,
         'segments': [{'duration': 10, 'harvest_low': 600, 'harvest_high': 800, 'load_low': 500, 'load_high': 500, 'leakage_low': 0, 'leakage_high': 0},
                      {'duration': 10, 'harvest_low': 0, 'harvest_high': 0, 'load_low': 100, 'load_high': 200, 'leakage_low': 0, 'leakage_high': 0}],
         'units': dict(temporal.UNITS), 'assumptions': list(temporal.ASSUMPTIONS), 'provenance': 'synthetic', 'private_label': 'CAMPAIGN_PRIVATE_31'}
    d.update(kw); return d


class CampaignTests(unittest.TestCase):
    def setUp(self):
        self.inst = Instance(); self.addCleanup(self.inst.close); self.c = self.inst.client

    def create(self, definition, preview=False):
        r = self.c.post('/api/v1/campaigns', headers=self.inst.h('owner'), json={'definition': definition, 'preview': preview})
        return r

    def drive(self, cid, max_rounds=40):
        for _ in range(max_rounds):
            self.inst.worker().run_once()
            state = self.c.post('/api/v1/campaigns/' + cid + '/tick', headers=self.inst.h('owner')).json()['state']
            if state in ('completed', 'cancelled', 'budget_exhausted', 'paused'):
                return state
        return state

    def test_grid_preview_enumeration_and_results(self):
        definition = {'name': 'reserve x load', 'kind': 'temporal_energy', 'base': base_temporal(),
                      'axes': [{'path': 'reserve', 'values': [1000, 3000, 5000]}, {'path': 'load_scale_percent', 'start': 100, 'stop': 300, 'step': 100}]}
        pv = self.create(definition, preview=True).json()
        self.assertEqual((pv['preview'], pv['total_candidates'], pv['estimate']['total_segments']), (True, 9, 18))
        self.assertEqual(self.c.get('/api/v1/campaigns', headers=self.inst.h('owner')).json()['items'], [])
        created = self.create(definition).json()
        cid = created['campaign_id']
        rows = self.c.get('/api/v1/campaigns/' + cid + '/results', headers=self.inst.h('owner')).json()['rows']
        self.assertEqual([r['params'] for r in rows][:4], [{'load_scale_percent': 100, 'reserve': 1000}, {'load_scale_percent': 200, 'reserve': 1000},
                                                          {'load_scale_percent': 300, 'reserve': 1000}, {'load_scale_percent': 100, 'reserve': 3000}])
        self.assertEqual(self.c.post('/api/v1/campaigns/' + cid + '/run', headers=self.inst.h('owner')).json()['state'], 'running')
        self.assertEqual(self.drive(cid), 'completed')
        view = self.c.get('/api/v1/campaigns/' + cid, headers=self.inst.h('owner')).json()
        self.assertEqual((view['done'], view['progress']['denominator'], view['by_state']), (9, 9, {'succeeded': 9}))
        table = self.c.get('/api/v1/campaigns/' + cid + '/results', headers=self.inst.h('owner')).json()['rows']
        outcomes = {(r['params']['reserve'], r['params']['load_scale_percent']): r['outcome'] for r in table}
        self.assertEqual(outcomes[(1000, 100)], 'FEASIBLE')            # reserve low, load nominal
        self.assertIn(outcomes[(5000, 300)], ('INFEASIBLE', 'INDETERMINATE'))
        # monotone along load scale at fixed reserve
        order = {'FEASIBLE': 2, 'INDETERMINATE': 1, 'INFEASIBLE': 0}
        for reserve in (1000, 3000, 5000):
            seq = [order[outcomes[(reserve, s)]] for s in (100, 200, 300)]
            self.assertEqual(seq, sorted(seq, reverse=True))
        csv = self.c.get('/api/v1/campaigns/' + cid + '/results.csv', headers=self.inst.h('owner')).text
        self.assertTrue(csv.startswith('index,load_scale_percent,reserve,state,outcome,'))
        self.assertIn('temporal-energy/v1', csv)
        svg = self.c.get('/api/v1/campaigns/' + cid + '/plot.svg', headers=self.inst.h('owner')).text
        self.assertEqual(svg.count('<rect'), 9)
        viewer = self.c.get('/api/v1/campaigns/' + cid + '/results', headers=self.inst.h('viewer')).json()
        self.assertNotIn('result', viewer['rows'][0]); self.assertEqual(viewer['rows'][0]['outcome'], 'withheld')
        self.assertNotIn('CAMPAIGN_PRIVATE_31', json.dumps(viewer) + csv + svg)
        # Pareto over the completed grid: min reserve, min load scale... choose max reserve & max load among FEASIBLE
        par = self.c.post('/api/v1/campaigns/' + cid + '/pareto', headers=self.inst.h('owner'),
                          json={'objectives': [{'field': 'reserve', 'direction': 'max'}, {'field': 'load_scale_percent', 'direction': 'max'}]}).json()
        front = {(x['params']['reserve'], x['params']['load_scale_percent']) for x in par['non_dominated']}
        self.assertTrue(front)
        for a in front:
            for b in front:
                self.assertFalse(a != b and a[0] >= b[0] and a[1] >= b[1] and a != b and (a[0] > b[0] or a[1] > b[1]), (a, b))
        self.assertTrue(all('dominated_by' in e or 'not in required' in e['reason'] for e in par['excluded']))
        missing = self.c.post('/api/v1/campaigns/' + cid + '/pareto', headers=self.inst.h('owner'), json={'objectives': [{'field': 'no_such_field', 'direction': 'min'}]}).json()
        self.assertEqual(missing['non_dominated'], [])
        self.assertTrue(all('missing objective' in e['reason'] or 'not in required' in e['reason'] for e in missing['excluded']))

    def test_limits_invalid_candidates_resume_pause_cancel(self):
        too_big = {'name': 'big', 'kind': 'temporal_energy', 'base': base_temporal(), 'axes': [{'path': 'reserve', 'start': 0, 'stop': 63, 'step': 1}, {'path': 'capacity', 'start': 1, 'stop': 64, 'step': 1}]}
        r = self.create(too_big, preview=True)
        self.assertEqual((r.status_code, r.json()['detail']['code']), (422, 'axis_values' if False else 'too_many_evaluations'))
        # capacity below initial energy -> invalid candidates, not zeros
        definition = {'name': 'cap sweep', 'kind': 'temporal_energy', 'base': base_temporal(), 'axes': [{'path': 'capacity', 'values': [5000, 7000, 12000, 20000]}]}
        cid = self.create(definition).json()['campaign_id']
        self.c.post('/api/v1/campaigns/' + cid + '/run', headers=self.inst.h('owner'))
        self.inst.worker().run_once()
        self.c.post('/api/v1/campaigns/' + cid + '/pause', headers=self.inst.h('owner'))
        jobs_before = self.c.get('/api/v1/jobs', headers=self.inst.h('owner')).json()['items']
        self.inst.reopen(); self.c = self.inst.client                                         # restart while paused
        self.assertEqual(self.c.get('/api/v1/campaigns/' + cid, headers=self.inst.h('owner')).json()['state'], 'paused')
        self.c.post('/api/v1/campaigns/' + cid + '/resume', headers=self.inst.h('owner'))
        self.assertEqual(self.drive(cid), 'completed')
        table = self.c.get('/api/v1/campaigns/' + cid + '/results', headers=self.inst.h('owner')).json()['rows']
        self.assertEqual([r['state'] for r in table], ['invalid', 'succeeded', 'succeeded', 'succeeded'])
        self.assertEqual(table[0]['reason'], 'MODEL_DOMAIN')
        jobs = self.c.get('/api/v1/jobs', headers=self.inst.h('owner')).json()['items']
        self.assertEqual(len(jobs), 3)                                                       # exactly one job per valid candidate, no duplicates after restart
        # cancel mid-way keeps completed evidence
        definition2 = {'name': 'cancelled', 'kind': 'temporal_energy', 'base': base_temporal(), 'axes': [{'path': 'reserve', 'start': 0, 'stop': 9000, 'step': 1000}]}
        cid2 = self.create(definition2).json()['campaign_id']
        self.c.post('/api/v1/campaigns/' + cid2 + '/run', headers=self.inst.h('owner'))
        self.inst.worker().run_once(); self.inst.worker().run_once()
        self.c.post('/api/v1/campaigns/' + cid2 + '/tick', headers=self.inst.h('owner'))
        view = self.c.post('/api/v1/campaigns/' + cid2 + '/cancel', headers=self.inst.h('owner')).json()
        self.assertEqual(view['state'], 'cancelled')
        self.assertGreaterEqual(view['by_state'].get('succeeded', 0), 2)
        self.assertGreaterEqual(view['by_state'].get('cancelled', 0), 1)
        self.assertEqual(self.c.post('/api/v1/campaigns/' + cid2 + '/run', headers=self.inst.h('owner')).status_code, 409)
        self.assertEqual(self.c.post('/api/v1/campaigns/' + cid2 + '/pause', headers=self.inst.h('viewer')).status_code, 403)

    def test_adaptive_bisection_matches_exhaustive_reference_and_refuses_non_monotone(self):
        base = base_temporal(initial_low=3000, initial_high=3000, reserve=1000, capacity=10000,
                             segments=[{'duration': 10, 'harvest_low': 0, 'harvest_high': 0, 'load_low': 100, 'load_high': 100, 'leakage_low': 0, 'leakage_high': 0}] * 3)
        # exhaustive reference over reserve 0..3000 step 100: FEASIBLE iff 3000 - 3000*... energy after 30 s at 100 mW = 3000-3000=0 -> reserve must be 0
        # use load 50: final 3000-1500=1500 -> feasible iff reserve <= 1500 (monotone up_worse)
        base['segments'] = [dict(s, load_low=50, load_high=50) for s in base['segments']]
        ref = {}
        for reserve in range(0, 3100, 100):
            ref[reserve] = temporal.analyze(campaigns.apply_params('temporal_energy', base, {'reserve': reserve}))['outcome']
        max_feasible = max(r for r, o in ref.items() if o == 'FEASIBLE')
        self.assertEqual(max_feasible, 1500)
        definition = {'name': 'max reserve', 'kind': 'temporal_energy', 'base': base, 'adaptive': {'objective': 'max_feasible', 'axis': 'reserve', 'lo': 0, 'hi': 3000, 'max_evaluations': 16}}
        cid = self.create(definition).json()['campaign_id']
        self.c.post('/api/v1/campaigns/' + cid + '/run', headers=self.inst.h('owner'))
        state = self.drive(cid, 60)
        view = self.c.get('/api/v1/campaigns/' + cid, headers=self.inst.h('owner')).json()
        self.assertEqual(state, 'completed')
        self.assertEqual(view['adaptive']['stopping_reason'], 'bracket_closed')
        self.assertEqual(view['adaptive']['boundary'], 1500)
        self.assertLessEqual(len(view['adaptive']['evaluated']), 16)
        self.assertTrue(all(e['value'] in ref for e in view['adaptive']['evaluated'] if e['value'] % 100 == 0))
        # budget exhausted -> unresolved bracket, no fabricated optimum
        small = dict(definition, name='tiny budget', adaptive=dict(definition['adaptive'], max_evaluations=3))
        cid2 = self.create(small).json()['campaign_id']
        self.c.post('/api/v1/campaigns/' + cid2 + '/run', headers=self.inst.h('owner'))
        self.assertEqual(self.drive(cid2, 30), 'budget_exhausted')
        adaptive = self.c.get('/api/v1/campaigns/' + cid2, headers=self.inst.h('owner')).json()['adaptive']
        self.assertEqual(adaptive['stopping_reason'], 'evaluation_budget_exhausted')
        self.assertIsNone(adaptive['boundary'])
        self.assertTrue(adaptive['remaining_bracket'][0] < adaptive['remaining_bracket'][1])
        # non-monotone axis refused; wrong direction refused
        r = self.create({'name': 'x', 'kind': 'safe_runtime', 'base': {'available_low': 1_000_000, 'available_high': 1_100_000, 'reserve': 100_000, 'fixed_segments': [], 'variable_power_low': 0, 'variable_power_high': 10, 'duration_cap': 100,
                                                                     'units': {'energy': 'mJ', 'power': 'mW', 'duration': 's'}, 'assumptions': ['no_recharge', 'usable_energy_at_load_boundary', 'piecewise_constant_power_bounds', 'no_unmodeled_loads'], 'provenance': 'synthetic', 'private_label': 'x'},
                         'adaptive': {'objective': 'min_feasible', 'axis': 'duration_cap', 'lo': 1, 'hi': 100, 'max_evaluations': 8}}, preview=True)
        self.assertEqual(r.json()['detail']['code'], 'not_monotone')
        r = self.create(dict(definition, adaptive=dict(definition['adaptive'], objective='min_feasible')), preview=True)
        self.assertEqual(r.json()['detail']['code'], 'objective_direction')

    def test_dataset_backed_campaign_and_refinement(self):
        vid = self.c.post('/api/v1/datasets', headers=self.inst.h('owner'), json={'name': 's', 'kind': 'temporal_series', 'format': 'csv', 'content': CSV_OK, 'provenance': 'declared'}).json()['version_id']
        definition = {'name': 'ds', 'kind': 'temporal_energy', 'base': {'dataset_version_id': vid, 'parameters': {'capacity': 10000, 'initial_low': 6000, 'initial_high': 6000, 'reserve': 2000}},
                      'axes': [{'path': 'harvest_scale_percent', 'values': [50, 100]}]}
        cid = self.create(definition).json()['campaign_id']
        self.c.post('/api/v1/campaigns/' + cid + '/run', headers=self.inst.h('owner'))
        self.assertEqual(self.drive(cid), 'completed')
        rows = self.c.get('/api/v1/campaigns/' + cid + '/results', headers=self.inst.h('owner')).json()['rows']
        self.assertEqual(len(rows), 2)
        self.assertIn('used_input', {e['relation'] for e in self.c.get('/api/v1/lineage/campaign/' + cid, headers=self.inst.h('owner')).json()['edges']})
        # refinement of a temporal job as an immutable derived artifact
        jid = rows[1]['job_id']
        before = self.c.get('/api/v1/jobs/' + jid + '/result', headers=self.inst.h('owner')).json()['result']
        r = self.c.post('/api/v1/jobs/' + jid + '/refine', headers=self.inst.h('owner'), json={'refinements': [{'segment': 1, 'field': 'load', 'low': 100, 'high': 120}]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()['hypothetical'])
        self.assertEqual(self.c.get('/api/v1/jobs/' + jid + '/result', headers=self.inst.h('owner')).json()['result'], before)
        self.assertEqual(self.c.post('/api/v1/jobs/' + jid + '/refine', headers=self.inst.h('owner'), json={'refinements': [{'segment': 1, 'field': 'load', 'low': 50, 'high': 120}]}).status_code, 409)
        self.assertEqual(self.c.post('/api/v1/jobs/' + jid + '/refine', headers=self.inst.h('viewer'), json={'refinements': []}).status_code, 403)
