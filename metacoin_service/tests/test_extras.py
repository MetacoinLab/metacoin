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
