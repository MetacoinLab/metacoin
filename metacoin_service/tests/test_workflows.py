"""Workflow engine: validation, a dataset -> temporal -> review gate -> export run through the API,
outcome conditions, rejected review blocking, cancellation, restart continuity, estimates."""
import json
import unittest
from metacoin_service.tests.test_service import Instance, own_inputs
from metacoin_service import workflows

CSV_DEFICIT = "duration_s,harvest_low_mW,harvest_high_mW,load_low_mW,load_high_mW\n3,0,0,3000,3000\n100,500,500,0,0\n"
CSV_OK = "duration_s,harvest_low_mW,harvest_high_mW,load_low_mW,load_high_mW\n10,600,800,500,500\n10,0,0,100,200\n"


def definition(dataset_node=None, **extra):
    nodes = [dataset_node or {'id': 'data', 'type': 'dataset', 'bind': 'series'},
             {'id': 'temporal', 'type': 'temporal_energy', 'depends_on': ['data'], 'input': 'data',
              'parameters': {'capacity': 10000, 'initial_low': 6000, 'initial_high': 6000, 'reserve': 2000}},
             {'id': 'gate', 'type': 'review_gate', 'depends_on': ['temporal'], 'input': 'temporal'},
             {'id': 'out', 'type': 'export', 'depends_on': ['gate', {'node': 'temporal', 'require': 'accepted_review'}], 'input': 'temporal',
              'fields': ['outcome', 'model_id', 'evidence_root', 'review_decision', 'envelope_digest']}]
    d = {'schema': workflows.SCHEMA, 'name': 'temporal review pipeline', 'nodes': nodes, 'outputs': ['out']}
    d.update(extra)
    return d


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.inst = Instance()
        self.addCleanup(self.inst.close)
        self.c = self.inst.client

    def dataset(self, csv=CSV_OK):
        r = self.c.post('/api/v1/datasets', headers=self.inst.h('owner'), json={'name': 'series', 'kind': 'temporal_series', 'format': 'csv', 'content': csv, 'provenance': 'declared'})
        self.assertEqual(r.status_code, 201, r.text)
        return r.json()['version_id']

    def test_validation_errors_name_the_node(self):
        bad = definition()
        bad['nodes'][1]['depends_on'] = ['data', 'gate']       # cycle: temporal <- gate <- temporal
        r = self.c.post('/api/v1/workflows/validate', headers=self.inst.h('owner'), json={'definition': bad})
        self.assertEqual(r.status_code, 422)
        self.assertTrue(any(e['code'] == 'cycle' for e in r.json()['detail']['errors']))
        cases = {
            'unknown_dependency': lambda d: d['nodes'][1].update(depends_on=['nowhere']),
            'unsupported_type': lambda d: d['nodes'][1].update(type='python'),
            'duplicate_node': lambda d: d['nodes'].append(dict(d['nodes'][0])),
            'parameter_mapping': lambda d: d['nodes'][1]['parameters'].update(reserve={'from': {'node': 'data', 'field': 'os.system'}}),
            'export_params': lambda d: d['nodes'][3].update(fields=['private_label']),
            'outputs': lambda d: d.update(outputs=['ghost']),
            'top_level_fields': lambda d: d.update(script='import os'),
        }
        for code, mutate in cases.items():
            d = definition(); mutate(d)
            r = self.c.post('/api/v1/workflows/validate', headers=self.inst.h('owner'), json={'definition': d})
            self.assertEqual(r.status_code, 422, code)
            detail = r.json()['detail']
            found = detail.get('code') == code or any(e['code'] == code for e in detail.get('errors', []))
            self.assertTrue(found, (code, detail))
        big = definition(); big['nodes'] += [{'id': 'n%d' % i, 'type': 'safe_runtime', 'inputs': {}} for i in range(40)]
        self.assertEqual(self.c.post('/api/v1/workflows/validate', headers=self.inst.h('owner'), json={'definition': big}).json()['detail']['code'], 'node_count')
        ok = self.c.post('/api/v1/workflows/validate', headers=self.inst.h('owner'), json={'definition': definition()}).json()
        self.assertEqual((ok['valid'], ok['order']), (True, ['data', 'temporal', 'gate', 'out']))
        self.assertEqual(ok['estimate']['service_nodes'], 1)

    def run_to_completion(self, wid, bindings, max_ticks=10):
        r = self.c.post('/api/v1/workflows/' + wid + '/runs', headers=self.inst.h('owner'), json={'bindings': bindings})
        self.assertEqual(r.status_code, 202, r.text)
        rid = r.json()['run_id']
        for _ in range(max_ticks):
            self.inst.worker().run_once()
            view = self.c.post('/api/v1/runs/' + rid + '/advance', headers=self.inst.h('owner')).json()
            if view['state'] in ('waiting_review', 'completed', 'blocked', 'partially_failed', 'failed', 'cancelled'):
                return rid, view['state']
        return rid, view['state']

    def test_dataset_temporal_review_export_journey(self):
        vid = self.dataset()
        w = self.c.post('/api/v1/workflows', headers=self.inst.h('owner'), json={'definition': definition()})
        self.assertEqual(w.status_code, 201); wid = w.json()['id']
        self.assertEqual(self.c.post('/api/v1/workflows', headers=self.inst.h('owner'), json={'definition': definition()}).status_code, 200)   # same digest, not duplicated
        preview = self.c.post('/api/v1/workflows/' + wid + '/runs', headers=self.inst.h('owner'), json={'bindings': {'series': vid}, 'preview': True}).json()
        self.assertTrue(preview['preview'] and preview['estimate']['max_job_attempts'] == 3)
        self.assertEqual(self.c.get('/api/v1/runs', headers=self.inst.h('owner')).json()['items'], [])          # preview created nothing
        rid, state = self.run_to_completion(wid, {'series': vid})
        self.assertEqual(state, 'waiting_review')
        view = self.c.get('/api/v1/runs/' + rid, headers=self.inst.h('owner')).json()
        nodes = {n['node_id']: n for n in view['nodes']}
        self.assertEqual((nodes['data']['state'], nodes['temporal']['state'], nodes['gate']['state'], nodes['out']['state']), ('succeeded', 'succeeded', 'waiting_review', 'waiting_dependency'))
        self.assertEqual(nodes['temporal']['outcome'], 'FEASIBLE')
        self.assertEqual(nodes['temporal']['binding']['upstream'][0]['dataset_version_id'], vid)
        self.assertFalse(view['deliverables']['out'])
        jid = nodes['temporal']['job_id']
        # duplicate review delivery does not enqueue duplicate work
        self.assertEqual(self.c.post('/api/v1/reviews/' + jid + '/decision', headers=self.inst.h('reviewer'), json={'decision': 'accepted'}).status_code, 200)
        self.assertEqual(self.c.post('/api/v1/reviews/' + jid + '/decision', headers=self.inst.h('reviewer'), json={'decision': 'accepted'}).status_code, 200)
        self.inst.reopen(); self.c = self.inst.client                                                   # service restart mid-run
        final = self.c.post('/api/v1/runs/' + rid + '/advance', headers=self.inst.h('owner')).json()
        self.assertEqual(final['state'], 'completed')
        view = self.c.get('/api/v1/runs/' + rid, headers=self.inst.h('owner')).json()
        self.assertTrue(view['deliverables']['out'])
        export_id = [n for n in view['nodes'] if n['node_id'] == 'out'][0]['artifact_id']
        exported = json.loads(self.c.get('/api/v1/artifacts/' + export_id + '/export', headers=self.inst.h('viewer')).text)
        self.assertEqual(sorted(exported['fields']), ['envelope_digest', 'evidence_root', 'model_id', 'outcome', 'review_decision'])
        self.assertEqual(exported['fields']['outcome'], 'FEASIBLE')
        self.assertNotIn('6000', json.dumps(exported))                                                    # parameters stay private
        self.assertEqual(len(self.c.get('/api/v1/runs/' + rid, headers=self.inst.h('owner')).json()['nodes']), 4)
        viewer_view = self.c.get('/api/v1/runs/' + rid, headers=self.inst.h('viewer')).json()
        self.assertNotIn('outcome', viewer_view['nodes'][1])
        self.assertLessEqual({'used_input', 'produced'}, {e['relation'] for e in self.c.get('/api/v1/lineage/workflow_run/' + rid, headers=self.inst.h('owner')).json()['edges']})

    def test_outcome_condition_and_rejected_review_block_downstream(self):
        vid = self.dataset(CSV_DEFICIT)
        d = definition()
        d['nodes'][3]['depends_on'] = ['gate', {'node': 'temporal', 'require': {'outcome_in': ['FEASIBLE']}}]
        wid = self.c.post('/api/v1/workflows', headers=self.inst.h('owner'), json={'definition': d}).json()['id']
        rid, state = self.run_to_completion(wid, {'series': vid})
        self.assertEqual(state, 'waiting_review')
        jid = [n for n in self.c.get('/api/v1/runs/' + rid, headers=self.inst.h('owner')).json()['nodes'] if n['node_id'] == 'temporal'][0]['job_id']
        self.assertEqual(self.c.get('/api/v1/jobs/' + jid, headers=self.inst.h('owner')).json()['outcome'], 'INFEASIBLE')   # valid negative, executed fine
        self.c.post('/api/v1/reviews/' + jid + '/decision', headers=self.inst.h('reviewer'), json={'decision': 'accepted'})
        final = self.c.post('/api/v1/runs/' + rid + '/advance', headers=self.inst.h('owner')).json()
        self.assertEqual(final['state'], 'blocked')
        out = [n for n in self.c.get('/api/v1/runs/' + rid, headers=self.inst.h('owner')).json()['nodes'] if n['node_id'] == 'out'][0]
        self.assertIn('outcome not in FEASIBLE', out['blocked_reason'])
        # rejected review blocks the gate itself
        vid2 = self.dataset()
        wid2 = self.c.post('/api/v1/workflows', headers=self.inst.h('owner'), json={'definition': definition()}).json()['id']
        rid2, _ = self.run_to_completion(wid2, {'series': vid2})
        jid2 = [n for n in self.c.get('/api/v1/runs/' + rid2, headers=self.inst.h('owner')).json()['nodes'] if n['node_id'] == 'temporal'][0]['job_id']
        self.c.post('/api/v1/reviews/' + jid2 + '/decision', headers=self.inst.h('reviewer'), json={'decision': 'rejected'})
        self.assertEqual(self.c.post('/api/v1/runs/' + rid2 + '/advance', headers=self.inst.h('owner')).json()['state'], 'blocked')

    def test_cancellation_at_queued_and_waiting_review_keeps_evidence(self):
        vid = self.dataset()
        wid = self.c.post('/api/v1/workflows', headers=self.inst.h('owner'), json={'definition': definition()}).json()['id']
        rid = self.c.post('/api/v1/workflows/' + wid + '/runs', headers=self.inst.h('owner'), json={'bindings': {'series': vid}}).json()['run_id']
        view = self.c.get('/api/v1/runs/' + rid, headers=self.inst.h('owner')).json()
        self.assertEqual([n['state'] for n in view['nodes']][:2], ['succeeded', 'queued'])              # job queued, worker not yet run
        r = self.c.post('/api/v1/runs/' + rid + '/cancel', headers=self.inst.h('owner')).json()
        self.assertIsNone(self.inst.worker().run_once())                                                   # cancelled job is never claimed
        r = self.c.post('/api/v1/runs/' + rid + '/advance', headers=self.inst.h('owner')).json()
        self.assertEqual(r['state'], 'cancelled')
        self.assertEqual(self.c.post('/api/v1/runs/' + rid + '/cancel', headers=self.inst.h('owner')).status_code, 409)
        # cancel while waiting for review: evidence and the job result stay
        rid2, state = self.run_to_completion(wid, {'series': vid})
        self.assertEqual(state, 'waiting_review')
        self.c.post('/api/v1/runs/' + rid2 + '/cancel', headers=self.inst.h('owner'))
        view = self.c.get('/api/v1/runs/' + rid2, headers=self.inst.h('owner')).json()
        self.assertEqual(view['state'], 'cancelled')
        temporal = [n for n in view['nodes'] if n['node_id'] == 'temporal'][0]
        self.assertEqual(temporal['state'], 'succeeded')
        self.assertEqual(self.c.get('/api/v1/jobs/' + temporal['job_id'] + '/result', headers=self.inst.h('owner')).status_code, 200)
        self.assertEqual(self.c.post('/api/v1/workflows/' + wid + '/runs', headers=self.inst.h('viewer'), json={'bindings': {'series': vid}}).status_code, 403)

    def test_inline_inputs_and_parameter_mapping_from_upstream(self):
        sr = {'available_low': 1_000_000, 'available_high': 1_100_000, 'reserve': 100_000, 'fixed_segments': [{'duration': 600, 'power_low': 800, 'power_high': 1000}],
              'variable_power_low': 100, 'variable_power_high': 250, 'duration_cap': 3600, 'units': {'energy': 'mJ', 'power': 'mW', 'duration': 's'},
              'assumptions': ['no_recharge', 'usable_energy_at_load_boundary', 'piecewise_constant_power_bounds', 'no_unmodeled_loads'], 'provenance': 'synthetic', 'private_label': 'x'}
        d = {'schema': workflows.SCHEMA, 'name': 'chain', 'outputs': ['audit'], 'nodes': [
            {'id': 'runtime', 'type': 'safe_runtime', 'inputs': sr},
            {'id': 'audit', 'type': 'energy_audit', 'depends_on': ['runtime'], 'inputs': own_inputs(),
             'parameters': {'reserve': {'from': {'node': 'runtime', 'field': 'safe_duration'}}}}]}
        wid = self.c.post('/api/v1/workflows', headers=self.inst.h('owner'), json={'definition': d}).json()['id']
        rid, state = self.run_to_completion(wid, {})
        self.assertEqual(state, 'completed')
        audit = [n for n in self.c.get('/api/v1/runs/' + rid, headers=self.inst.h('owner')).json()['nodes'] if n['node_id'] == 'audit'][0]
        self.assertEqual(audit['binding']['upstream'][0]['field'], 'safe_duration')
        self.assertEqual(audit['binding']['upstream'][0]['value'], 1200)
        inputs = self.c.get('/api/v1/contracts/' + audit['contract_id'] + '/inputs', headers=self.inst.h('owner')).json()['inputs']
        self.assertEqual(inputs['reserve'], 1200)
