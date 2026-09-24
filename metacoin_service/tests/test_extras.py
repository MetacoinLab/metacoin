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
