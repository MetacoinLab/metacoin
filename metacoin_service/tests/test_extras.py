"""§46 extras: service compatibility preview and PROV-JSON lineage export."""
import json
import unittest
from metacoin_service.tests.test_service import Instance
from metacoin_service.tests.test_workflows import definition, CSV_OK


class ExtrasTests(unittest.TestCase):
    def setUp(self):
        self.inst = Instance(); self.addCleanup(self.inst.close); self.c = self.inst.client; self.H = self.inst.h('owner')
        self.services = {s['kind']: s for s in self.c.get('/api/v1/services', headers=self.H).json()['items']}
        self.vid = self.c.post('/api/v1/datasets', headers=self.H, json={'name': 'series', 'kind': 'temporal_series', 'format': 'csv', 'content': CSV_OK, 'provenance': 'declared'}).json()['version_id']

    def test_compatibility_preview_names_every_mismatch(self):
        ok = self.c.get('/api/v1/services/' + self.services['temporal_energy']['id'] + '/compatibility?dataset_version_id=' + self.vid, headers=self.H).json()
        self.assertTrue(ok['compatible'], ok)
        self.assertEqual({c['aspect'] for c in ok['checks']} >= {'service_status', 'verifier_version', 'input_type', 'units', 'payload_available', 'dataset_active', 'row_limit', 'privacy'}, True)
        bad = self.c.get('/api/v1/services/' + self.services['energy_audit']['id'] + '/compatibility?dataset_version_id=' + self.vid, headers=self.H).json()
        self.assertFalse(bad['compatible'])
        failing = {c['aspect']: c for c in bad['checks'] if not c['ok']}
        self.assertEqual(list(failing), ['input_type'])
        self.assertEqual((failing['input_type']['expected'], failing['input_type']['actual']), ('energy_intervals', 'temporal_series'))
        inline = self.c.get('/api/v1/services/' + self.services['safe_runtime']['id'] + '/compatibility?dataset_version_id=' + self.vid, headers=self.H).json()
        self.assertIn('inline JSON inputs', [c for c in inline['checks'] if c['aspect'] == 'input_type'][0]['explanation'])
        # retiring the dataset flips one named check; the viewer may preview too; foreign ids are not found
        ds = self.c.get('/api/v1/datasets', headers=self.H).json()
        did = (ds['items'] if isinstance(ds, dict) else ds)[0]['id'] if (ds['items'] if isinstance(ds, dict) else ds) else None
        if did:
            self.c.post('/api/v1/datasets/' + did + '/retire', headers=self.H)
            again = self.c.get('/api/v1/services/' + self.services['temporal_energy']['id'] + '/compatibility?dataset_version_id=' + self.vid, headers=self.H).json()
            self.assertIn('dataset_active', [c['aspect'] for c in again['checks'] if not c['ok']])
        self.assertEqual(self.c.get('/api/v1/services/' + self.services['temporal_energy']['id'] + '/compatibility?dataset_version_id=dv_nope', headers=self.inst.h('viewer')).status_code, 404)
        # a workflow node output feeding a service: bindable fields and target parameters are listed
        d = definition(); wid = self.c.post('/api/v1/workflows', headers=self.H, json={'definition': d}).json()['id']
        rid = self.c.post('/api/v1/workflows/' + wid + '/runs', headers=self.H, json={'bindings': {'series': self.vid}}).json().get('run_id')
        if rid:
            comp = self.c.get('/api/v1/services/' + self.services['energy_audit']['id'] + '/compatibility?run_id=' + rid + '&node_id=temporal', headers=self.H).json()
            by = {c['aspect']: c for c in comp['checks']}
            self.assertTrue(by['upstream_is_service_node']['ok'])
            self.assertIn('reserve', by['target_parameters']['actual'])
            self.assertEqual(by['upstream_state']['ok'], False)                      # not yet run

    def test_prov_json_export_is_well_formed_and_private_free(self):
        d = definition(); d['nodes'] = d['nodes'][:2]; d['outputs'] = ['temporal']
        wid = self.c.post('/api/v1/workflows', headers=self.H, json={'definition': d}).json()['id']
        rid = self.c.post('/api/v1/workflows/' + wid + '/runs', headers=self.H, json={'bindings': {'series': self.vid}}).json()['run_id']
        self.c.post('/api/v1/runs/' + rid + '/advance', headers=self.H); self.inst.worker().run_once(); self.c.post('/api/v1/runs/' + rid + '/advance', headers=self.H)
        v = self.c.get('/api/v1/runs/' + rid, headers=self.H).json()
        jid = [n for n in v['nodes'] if n['node_id'] == 'temporal'][0]['job_id']
        prov = self.c.get('/api/v1/lineage/job/' + jid + '/prov.json', headers=self.H).json()
        self.assertEqual(prov['prefix']['prov'], 'http://www.w3.org/ns/prov#')
        declared = set(prov['entity']) | set(prov['activity']) | set(prov['agent'])
        for rel in ('used', 'wasGeneratedBy', 'wasDerivedFrom', 'wasInformedBy', 'wasAssociatedWith'):
            for r in prov[rel].values():
                for ref in r.values():
                    self.assertIn(ref, declared, (rel, ref))
        self.assertIn('metacoin:job/' + jid, prov['activity'])
        self.assertIn('metacoin:dataset_version/' + self.vid, prov['entity'])
        self.assertTrue(any(u['prov:activity'] == 'metacoin:job/' + jid for u in prov['used'].values()))
        self.assertTrue(any(g['prov:activity'] == 'metacoin:job/' + jid for g in prov['wasGeneratedBy'].values()))
        text = json.dumps(prov)
        for canary in ('harvest_low', 'capacity', 'USER_PRIVATE', 'summary'):
            self.assertNotIn(canary, text)
        # the viewer gets the same identifier-only graph
        self.assertEqual(self.c.get('/api/v1/lineage/job/' + jid + '/prov.json', headers=self.inst.h('viewer')).json()['activity'].keys(), prov['activity'].keys())


if __name__ == '__main__':
    unittest.main()


class TemplateTests(unittest.TestCase):
    """§46(1): a validated graph template with integer parameter slots is instantiated into a new immutable definition."""

    def setUp(self):
        self.inst = Instance(); self.addCleanup(self.inst.close); self.c = self.inst.client; self.H = self.inst.h('owner')
        self.vid = self.c.post('/api/v1/datasets', headers=self.H, json={'name': 'series', 'kind': 'temporal_series', 'format': 'csv', 'content': CSV_OK, 'provenance': 'declared'}).json()['version_id']

    def template(self, **over):
        d = definition(); d['nodes'] = d['nodes'][:2]; d['outputs'] = ['temporal']
        d['nodes'][1]['parameters']['reserve'] = {'slot': 'reserve'}
        d['slots'] = {'reserve': {'type': 'integer', 'min': 0, 'max': 9000, 'description': 'reserve floor in mJ'}}
        d.update(over)
        return d

    def test_template_instantiation_is_validated_and_lineage_recorded(self):
        t = self.c.post('/api/v1/workflows', headers=self.H, json={'definition': self.template()})
        self.assertEqual(t.status_code, 201, t.text); tid = t.json()['id']
        # a template cannot run directly; the refusal names the slot
        r = self.c.post('/api/v1/workflows/' + tid + '/runs', headers=self.H, json={'bindings': {'series': self.vid}})
        self.assertEqual((r.status_code, r.json()['detail']['code'], r.json()['detail']['slots']), (422, 'template_has_parameter_slots', ['reserve']))
        # bad values are refused precisely
        for values, code in (({}, 'slot_values'), ({'reserve': 'x'}, 'slot_values'), ({'reserve': 9001}, 'slot_out_of_range'), ({'reserve': 1, 'extra': 2}, 'slot_values')):
            self.assertEqual(self.c.post('/api/v1/workflows/' + tid + '/instantiate', headers=self.H, json={'values': values}).json()['detail']['code'], code, values)
        self.assertEqual(self.c.post('/api/v1/workflows/' + tid + '/instantiate', headers=self.inst.h('viewer'), json={'values': {'reserve': 2000}}).status_code, 403)
        inst = self.c.post('/api/v1/workflows/' + tid + '/instantiate', headers=self.H, json={'values': {'reserve': 2000}, 'name': 'reserve 2000'})
        self.assertEqual(inst.status_code, 201, inst.text)
        d = inst.json()
        self.assertEqual((d['template_id'], d['values'], d['definition']['nodes'][1]['parameters']['reserve'], 'slots' in d['definition']), (tid, {'reserve': 2000}, 2000, False))
        self.assertNotEqual(d['digest'], t.json()['digest'])
        again = self.c.post('/api/v1/workflows/' + tid + '/instantiate', headers=self.H, json={'values': {'reserve': 2000}, 'name': 'reserve 2000'})
        self.assertEqual((again.status_code, again.json()['id']), (200, d['id']))                       # same values: same immutable instance
        lineage = self.c.get('/api/v1/lineage/workflow_definition/' + d['id'], headers=self.H).json()
        self.assertIn({'from': ['workflow_definition', tid], 'to': ['workflow_definition', d['id']], 'relation': 'derived_from'}, [{k: e[k] for k in ('from', 'to', 'relation')} for e in lineage['edges']])
        # the instance runs like any definition
        run = self.c.post('/api/v1/workflows/' + d['id'] + '/runs', headers=self.H, json={'bindings': {'series': self.vid}})
        self.assertEqual(run.status_code, 202, run.text)
        self.inst.worker().run_once(); self.c.post('/api/v1/runs/' + run.json()['run_id'] + '/advance', headers=self.H)
        self.assertEqual(self.c.get('/api/v1/runs/' + run.json()['run_id'], headers=self.H).json()['state'], 'completed')
        # slot declarations are validated; an undeclared slot reference names the parameter
        bad = self.template(); bad['nodes'][1]['parameters']['capacity'] = {'slot': 'cap'}
        self.assertEqual(self.c.post('/api/v1/workflows', headers=self.H, json={'definition': bad}).json()['detail']['errors'][0]['code'], 'undeclared_slot')
        bad = self.template(slots={'reserve': {'type': 'float'}})
        self.assertEqual(self.c.post('/api/v1/workflows', headers=self.H, json={'definition': bad}).json()['detail']['code'], 'slots')
        # a plain definition is not a template
        plain = self.c.post('/api/v1/workflows', headers=self.H, json={'definition': definition()}).json()['id']
        self.assertEqual(self.c.post('/api/v1/workflows/' + plain + '/instantiate', headers=self.H, json={'values': {}}).json()['detail']['code'], 'not_a_template')


class BranchTests(unittest.TestCase):
    """§46(3): fork a campaign from authorized results with explicit changed assumptions and compare branch to original."""

    def setUp(self):
        self.inst = Instance(); self.addCleanup(self.inst.close); self.c = self.inst.client; self.H = self.inst.h('owner')

    def drive(self, cid, ticks=12):
        self.c.post('/api/v1/campaigns/' + cid + '/run', headers=self.H)
        for _ in range(ticks):
            self.inst.worker().tick_workflows(); self.inst.worker().run_once()
            if self.c.get('/api/v1/campaigns/' + cid, headers=self.H).json()['state'] == 'completed':
                return 'completed'
        return self.c.get('/api/v1/campaigns/' + cid, headers=self.H).json()['state']

    def test_branch_with_changed_assumptions_and_compare(self):
        from metacoin_service.tests.test_agents import TEMPORAL
        base = dict(TEMPORAL, private_label='BRANCH_SYNTHETIC')
        definition = {'name': 'reserve sweep', 'kind': 'temporal_energy', 'base': base, 'axes': [{'path': 'reserve', 'values': [1000, 5000, 7000]}]}
        a = self.c.post('/api/v1/campaigns', headers=self.H, json={'definition': definition}).json()['campaign_id']
        self.assertEqual(self.drive(a), 'completed')
        # refusals: no change, unknown field, unauthorized candidate, viewer
        self.assertEqual(self.c.post('/api/v1/campaigns/' + a + '/branch', headers=self.H, json={}).json()['detail']['code'], 'no_change')
        self.assertEqual(self.c.post('/api/v1/campaigns/' + a + '/branch', headers=self.H, json={'base_changes': {'nope': 1}}).json()['detail']['code'], 'unknown_base_field')
        self.assertEqual(self.c.post('/api/v1/campaigns/' + a + '/branch', headers=self.H, json={'candidate_indexes': [99]}).json()['detail']['code'], 'candidate_not_authorized')
        self.assertEqual(self.c.post('/api/v1/campaigns/' + a + '/branch', headers=self.inst.h('viewer'), json={'base_changes': {'capacity': 1}}).status_code, 403)
        # branch: lower initial energy is an explicit changed assumption; the branch re-evaluates the same grid
        b = self.c.post('/api/v1/campaigns/' + a + '/branch', headers=self.H, json={'base_changes': {'initial_low': 2000, 'initial_high': 2000}, 'name': 'reserve sweep / low start'})
        self.assertEqual(b.status_code, 201, b.text)
        bid = b.json()['campaign_id']
        self.assertEqual((b.json()['branched_from'], b.json()['total_candidates']), (a, 3))
        self.assertIn('branch-of:' + a, self.c.get('/api/v1/campaigns/' + bid, headers=self.H).json()['definition']['tags'])
        self.assertEqual(self.drive(bid), 'completed')
        cmp_ = self.c.get('/api/v1/campaigns/' + a + '/compare/' + bid, headers=self.H).json()
        self.assertEqual(cmp_['base_changes'], {'initial_low': {'a': 6000, 'b': 2000}, 'initial_high': {'a': 6000, 'b': 2000}})
        self.assertEqual((cmp_['summary']['matched'], cmp_['summary']['only_in_a'], cmp_['summary']['only_in_b']), (3, 0, 0))
        self.assertGreaterEqual(cmp_['summary']['worsened'], 1)                           # a lower start can only make feasibility worse
        self.assertEqual(cmp_['summary']['improved'], 0)
        self.assertTrue(all(c['direction'] == 'worsened' for c in cmp_['changes']))
        # the viewer sees which fields changed, never the values
        vcmp = self.c.get('/api/v1/campaigns/' + a + '/compare/' + bid, headers=self.inst.h('viewer')).json()
        self.assertEqual(vcmp['base_changes'], {'initial_low': 'changed', 'initial_high': 'changed'})
        # candidate selection: the branch grid is the smallest product grid containing the selected succeeded candidates
        results = self.c.get('/api/v1/campaigns/' + a + '/results', headers=self.H).json()['rows']
        ok_idx = [r['index'] for r in results if r['state'] == 'succeeded'][:2]
        s = self.c.post('/api/v1/campaigns/' + a + '/branch', headers=self.H, json={'candidate_indexes': ok_idx, 'base_changes': {'capacity': 20000}}).json()
        self.assertEqual(s['total_candidates'], 2)
        self.assertEqual(s['axes'], [{'path': 'reserve', 'values': sorted(results[i]['params']['reserve'] for i in ok_idx)}])
        lineage = self.c.get('/api/v1/lineage/campaign/' + bid, headers=self.H).json()
        self.assertIn({'from': ['campaign', a], 'to': ['campaign', bid], 'relation': 'derived_from'}, [{k: e[k] for k in ('from', 'to', 'relation')} for e in lineage['edges']])
