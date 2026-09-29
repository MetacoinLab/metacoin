"""Backlog 3/4/8/9: reconciliation of conflicting sources under an explicit rule, measurement-request artifacts derived from
recorded evidence, package upgrade preview, and the decision review queue."""
import json
import unittest
from metacoin_service.tests.test_compute_engine import ComputeInstance, HAVE_RUNTIME, batch_spec
from metacoin_service.tests.test_documents import DocInstance, pdf
from metacoin_service.tests.test_resource_plan_service import sample
from metacoin_service import workflows as wf_mod


@unittest.skipUnless(HAVE_RUNTIME, 'no compute interpreter')
class ReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.inst = DocInstance(); self.addCleanup(self.inst.close); self.c = self.inst.client; self.H = self.inst.h('owner')
        self.w = self.inst.worker(); self.addCleanup(self.w.offline)

    def post(self, path, body, status=201, headers=None):
        r = self.c.post(path, headers=headers or self.H, json=body); self.assertEqual(r.status_code, status, r.text); return r.json()

    def test_reconcile_conflicting_sources_and_measurement_request(self):
        cid = self.post('/api/v1/knowledge/collections', {'name': 'rc'})['id']
        r = self.c.post('/api/v1/documents/import?name=repeated-headers.pdf&collection_id=' + cid, headers=dict(self.H, **{'Content-Type': 'application/pdf'}), content=pdf('repeated-headers.pdf')); v = r.json(); self.assertEqual(self.w.run_once()[1], 'succeeded')
        d = self.c.get('/api/v1/documents/' + v['id'], headers=self.H).json(); tid = d['tables'][0]['id']
        cell = self.c.get('/api/v1/documents/tables/' + tid, headers=self.H).json()['rows'][1][2]          # '82' mW
        sources = [{'kind': 'table_cell', 'ref': {'table_id': tid, 'row': 1, 'col': 2}, 'value': cell, 'unit': 'mW', 'interpretation': 'point', 'label': 'scan table'},
                   {'kind': 'manual', 'value': '0.09', 'unit': 'W', 'interpretation': 'point', 'label': 'datasheet'},
                   {'kind': 'manual', 'value': ['0.08', '0.095'], 'unit': 'W', 'interpretation': 'interval', 'label': 'bench interval'}]
        # a rule is mandatory and must be justified; averaging without justification is refused
        self.assertEqual(self.post('/api/v1/reconciliations', {'quantity': {'name': 'idle power'}, 'unit': 'mW', 'sources': sources, 'rule': {'method': 'mean'}}, 422)['detail']['code'], 'rule')
        rec = self.post('/api/v1/reconciliations', {'quantity': {'name': 'idle power'}, 'unit': 'mW', 'sources': sources, 'rule': {'method': 'select_source', 'selected': 0, 'justification': 'the scanned table is the calibrated bench log'}})
        self.assertTrue(rec['conflict']['conflicting']); self.assertEqual(rec['conflict']['point_values'], [82, 90]); self.assertEqual(rec['conflict']['hull'], [80, 95])
        self.assertEqual(rec['result'], {'value': 82, 'interval': None, 'from_source': 0}); self.assertEqual(rec['sources'][0]['declared_matches_source'], True)
        self.assertEqual(rec['sources'][2]['base'], {'low': 80, 'high': 95, 'directed': [None, None]})
        mean = self.post('/api/v1/reconciliations', {'quantity': {'name': 'idle power'}, 'unit': 'mW', 'sources': sources, 'rule': {'method': 'mean', 'justification': 'two independent point readings'}})
        self.assertEqual((mean['result']['value'], mean['result']['exact']), (86, '86'))
        hull = self.post('/api/v1/reconciliations', {'quantity': {'name': 'idle power'}, 'unit': 'mW', 'sources': sources, 'rule': {'method': 'interval_hull', 'justification': 'robust envelope'}})
        self.assertEqual(hull['result']['interval'], [80, 95])
        # a declared value that does not match the cited cell is recorded, not silently accepted; an inexact point conversion is refused
        wrong = self.post('/api/v1/reconciliations', {'quantity': {'name': 'idle power'}, 'unit': 'mW', 'sources': [dict(sources[0], value='83')], 'rule': {'method': 'select_source', 'selected': 0, 'justification': 'x'}})
        self.assertEqual(wrong['sources'][0]['declared_matches_source'], False); self.assertEqual(wrong['conflict']['mismatching_declarations'], [0])
        self.assertEqual(self.post('/api/v1/reconciliations', {'quantity': {'name': 'p'}, 'unit': 'mW', 'sources': [{'kind': 'manual', 'value': '0.0001234', 'unit': 'W', 'interpretation': 'point'}], 'rule': {'method': 'min', 'justification': 'x'}}, 422)['detail']['code'], 'unconvertible')
        self.assertEqual(self.post('/api/v1/reconciliations', {'quantity': {'name': 'p'}, 'unit': 'mW', 'sources': [{'kind': 'manual', 'value': '5', 'unit': 'J', 'interpretation': 'point'}], 'rule': {'method': 'min', 'justification': 'x'}}, 422)['detail']['code'], 'source_dimension')
        # viewer sees structure only
        vv = self.c.get('/api/v1/reconciliations/' + rec['id'], headers=self.inst.h('viewer')).json(); self.assertNotIn('sources', vv); self.assertIn('withheld', vv)
        # measurement request from a plan's sensitivity and the reconciliation conflict: a local artifact, nothing sent
        jid = self.post('/api/v1/compute/resource-plans', {'inputs': sample()}, 202)['job_id']; self.assertEqual(self.w.run_once()[1], 'succeeded')
        mq = self.post('/api/v1/measurement-requests', {'quantity': 'reserve', 'unit': 'mJ', 'required_precision': {'abs': '100', 'unit': 'mJ'}, 'acceptable_format': {'kind': 'csv', 'columns': ['reserve', 'unit', 'timestamp']}, 'from': {'plan_job_id': jid, 'reconciliation_id': rec['id']}})
        self.assertEqual(mq['schema'], 'metacoin-measurement-request/v1'); self.assertTrue(any(r['source'] == 'plan_sensitivity' and r['decision_changed'] for r in mq['decision_relevance']))
        self.assertTrue(any(r['source'] == 'reconciliation_conflict' for r in mq['decision_relevance'])); self.assertIn('nothing was sent', mq['delivery']); self.assertTrue(mq['missing_evidence'])
        exp = self.c.get('/api/v1/measurement-requests/' + mq['id'] + '/export', headers=self.H).json()
        self.assertEqual(exp['request']['id'], mq['id']); self.assertNotIn('RP_TEST', json.dumps(exp))
        self.assertEqual(self.c.get('/api/v1/measurement-requests/' + mq['id'], headers=self.inst.h('viewer')).json()['id'], mq['id'])
        self.assertEqual(self.c.get('/api/v1/measurement-requests', headers=self.H).json()['items'][0]['id'], mq['id'])

    def test_upgrade_preview_and_review_queue(self):
        wid = self.post('/api/v1/workflows', {'definition': {'schema': wf_mod.SCHEMA, 'name': 'p', 'outputs': ['o'], 'nodes': [{'id': 'plan', 'type': 'resource_plan', 'inputs': sample()}, {'id': 'o', 'type': 'export', 'depends_on': ['plan'], 'input': 'plan', 'fields': ['outcome']}]}})['id']
        p1 = self.post('/api/v1/packages', {'name': 'up', 'workflow_id': wid, 'delivery_policy': {'gate': 'required_verification', 'required_class': 'full_reference'}, 'example': {'plan': sample()}})
        wid2 = self.post('/api/v1/workflows', {'definition': {'schema': wf_mod.SCHEMA, 'name': 'p2', 'outputs': ['o'], 'nodes': [{'id': 'plan', 'type': 'resource_plan', 'inputs': sample()}, {'id': 'b', 'type': 'temporal_batch', 'inputs': batch_spec(private_label='UP')}, {'id': 'o', 'type': 'export', 'depends_on': ['plan', 'b'], 'input': 'plan', 'fields': ['outcome']}]}})['id']
        p2 = self.post('/api/v1/packages', {'name': 'up', 'workflow_id': wid2, 'delivery_policy': {'gate': 'required_verification', 'required_class': 'analytical'}, 'example': {'plan': sample()}})
        self.c.put('/api/v1/budgets/workspace', headers=self.H, json={'ceiling': 10 ** 6})
        inst = self.post('/api/v1/packages/' + p1['id'] + '/instantiate', {'inputs': {'plan': sample()}})
        q = self.post('/api/v1/packages/' + p1['id'] + '/quote', {'workflow_id': inst['workflow_id']}); run = self.post('/api/v1/packages/' + p1['id'] + '/runs', {'quote_id': q['quote_id']}, 202)
        pv = self.c.get('/api/v1/packages/%s/upgrade-preview/%s' % (p1['id'], p2['id']), headers=self.H).json()
        areas = {c['area'] for c in pv['changes']}
        self.assertTrue({'operations', 'verification_policy', 'resource_bounds'} <= areas, areas); self.assertTrue(pv['same_name']); self.assertEqual((pv['old']['version'], pv['new']['version']), (1, 2))
        self.assertTrue(any(c['path'] == 'temporal_batch' and c['new'] == 'added' for c in pv['changes'])); self.assertTrue(pv['migration_required'])
        self.assertEqual([r['id'] for r in pv['affected']['runs']], [run['id']]); self.assertIn('nothing is repointed', pv['note'])
        self.assertTrue(any(c['kind'] == 'temporal_batch' and c['amount_per_unit'] is not None for c in pv['costs']))
        self.assertEqual(self.c.get('/api/v1/packages/runs/' + run['id'], headers=self.H).json()['package_id'], p1['id'])       # the run keeps its identity
        # review queue: a text-only edit never queues; a stale result does; an unsupported claim does; a frozen result without verification does
        jid = self.post('/api/v1/compute/resource-plans', {'inputs': sample()}, 202)['job_id']; self.assertEqual(self.w.run_once()[1], 'succeeded')
        a = self.post('/api/v1/analyses', {'name': 'queue study', 'blocks': [{'id': 'aim', 'type': 'text', 'text': 'aim'}, {'id': 'assump', 'type': 'assumption_table', 'rows': [{'name': 'reserve', 'value': 20000, 'unit': 'mJ'}]}, {'id': 'run', 'type': 'run_result', 'ref_kind': 'job', 'ref_id': jid, 'fields': ['objective'], 'depends_on': ['assump']}]})
        self.assertEqual(self.c.get('/api/v1/review-queue', headers=self.H).json()['count'], 0)
        blocks = [{k: x for k, x in b.items() if k not in ('status', 'stale', 'requires', 'reference', 'reference_drift')} for b in a['blocks']]
        blocks[0]['text'] = 'aim, reworded'
        v2 = self.post('/api/v1/analyses/' + a['id'] + '/revisions', {'blocks': blocks, 'expected_version': 1})
        self.assertEqual(self.c.get('/api/v1/review-queue', headers=self.H).json()['count'], 0)                       # harmless text edit
        blocks[1] = dict(blocks[1], rows=[{'name': 'reserve', 'value': 25000, 'unit': 'mJ'}])
        v3 = self.post('/api/v1/analyses/' + a['id'] + '/revisions', {'blocks': blocks, 'expected_version': 2})
        rq = self.c.get('/api/v1/review-queue', headers=self.H).json()
        self.assertEqual(rq['count'], 1); self.assertEqual(rq['items'][0]['reasons'][0]['code'], 'stale_evidence'); self.assertEqual(rq['items'][0]['severity'], 'high')
        self.post('/api/v1/analyses/' + a['id'] + '/freeze', {'version': 3}, 200)
        rq2 = self.c.get('/api/v1/review-queue', headers=self.H).json()
        self.assertIn('insufficient_verification', [r['code'] for r in rq2['items'][0]['reasons']])


if __name__ == '__main__':
    unittest.main()
