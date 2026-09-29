"""§65-7/8 service bundles with data-only compatibility checks and imports; §65-9 disagreement review with
evidence-linked reviewer decisions. The model part needs the pinned artifacts."""
import copy
import json
import unittest

from metacoin_service.tests.test_compute_engine import ComputeInstance, HAVE_RUNTIME, batch_spec
from metacoin_service.tests.test_models import ModelInstance, HAVE_TORCH, installed, GEN, EMB
from metacoin_service.tests.test_verification import corrupt_output, flip_first_outcome


class BundleInstance(ModelInstance, ComputeInstance):
    pass


@unittest.skipUnless(HAVE_RUNTIME, 'no numpy-capable compute interpreter on this host')
class BundleTests(unittest.TestCase):
    def test_export_check_import_between_instances(self):
        a = BundleInstance(); self.addCleanup(a.close); b = BundleInstance(); self.addCleanup(b.close)
        HA, HB = a.h('owner'), b.h('owner')
        models_ok = HAVE_TORCH and installed(a.settings, GEN) and installed(a.settings, EMB)
        ids = a.register_defaults() if models_ok else {}
        tpl = a.client.post('/api/v1/verification/policies', headers=HA, json={'name': 'strict', 'class': 'sampled_reference', 'params': {'sample_count': 32}}).json()
        a.client.post('/api/v1/approvals/policy', headers=HA, json={'required': ['model_promote']})
        if models_ok:
            a.client.post('/api/v1/models/warmup', headers=HA, json={'revision_ids': [ids['embed']], 'ceiling_bytes': 2 * 10 ** 9})
        r = a.client.post('/api/v1/bundles/export', headers=HA, json={'name': 'science-pack', 'services': ['temporal_batch', 'heat_diffusion'], 'models': list(ids.values()), 'verification_policies': [tpl['id']]})
        self.assertEqual(r.status_code, 200, r.text); bundle = r.json()
        self.assertEqual((bundle['schema'], len(bundle['services']), len(bundle['verification_policies']), bundle['approval_policy']['required']), ('metacoin-service-bundle/v1', 2, 1, ['model_promote']))
        text = json.dumps(bundle)
        for secret in ('mck_', 'mcn_', '"token"', 'AGE-SECRET', 'quote_id', 'grant_id'):
            self.assertNotIn(secret, text)
        self.assertEqual(a.client.post('/api/v1/bundles/export', headers=a.h('viewer'), json={'name': 'x'}).status_code, 403)
        # check on the fresh instance: compatible (same runtime); the built-in services already exist with identical terms
        chk = b.client.post('/api/v1/bundles/check', headers=HB, json={'bundle': bundle}).json()
        self.assertTrue(chk['compatible'], chk['blocking']); self.assertTrue(chk['nothing_executed'])
        self.assertEqual(len(b.client.get('/api/v1/models', headers=HB).json()['items']), 0)
        # tampered bundle: digest mismatch is blocking
        bad = copy.deepcopy(bundle); bad['services'][0]['price_per_unit'] = 0
        self.assertIn('bundle_digest', b.client.post('/api/v1/bundles/check', headers=HB, json={'bundle': bad}).json()['blocking'])
        self.assertEqual(b.client.post('/api/v1/bundles/import', headers=HB, json={'bundle': bad, 'apply': True}).status_code, 409)
        # a bundle naming a model whose weights are absent here reports the acquisition step and blocks
        absent = copy.deepcopy(bundle); absent['models'] = [{'model_id': 'ghost', 'hub_repo': 'nobody/ghost', 'revision': 'f' * 40, 'operations': ['generate'], 'license': 'apache-2.0', 'precision': 'auto', 'weight_digest': 'a' * 64, 'tokenizer_digest': None, 'resource_estimate_bytes': None, 'description': ''}]
        import hashlib
        from experiments.private_receipts import receipt as merkle
        absent['digest'] = hashlib.sha256(merkle.canonical({k: v for k, v in absent.items() if k != 'digest'})).hexdigest()
        chk2 = b.client.post('/api/v1/bundles/check', headers=HB, json={'bundle': absent}).json()
        ghost = next(i for i in chk2['items'] if i['item'].startswith('model.ghost'))
        self.assertFalse(chk2['compatible']); self.assertFalse(ghost['detail']['weights_present']); self.assertIn('install the pinned artifact', ghost['detail']['acquisition'])
        # dry run changes nothing; apply registers without promoting or loading
        dry = b.client.post('/api/v1/bundles/import', headers=HB, json={'bundle': bundle}).json()
        self.assertFalse(dry['applied']); self.assertEqual(len(b.client.get('/api/v1/verification/policies', headers=HB).json()['items']), 0)
        imp = b.client.post('/api/v1/bundles/import', headers=HB, json={'bundle': bundle, 'apply': True})
        self.assertEqual(imp.status_code, 200, imp.text); imp = imp.json()
        self.assertTrue(imp['applied']); self.assertEqual(len(imp['results']['verification_policies']), 1); self.assertEqual(imp['results']['approval_policy']['required'], ['model_promote'])
        self.assertEqual(b.client.get('/api/v1/approvals/policy', headers=HB).json()['required'], ['model_promote'])
        if models_ok:
            ms = b.client.get('/api/v1/models', headers=HB).json()['items']
            self.assertEqual(len(ms), 2); self.assertTrue(all(m['installed'] for m in ms))
            self.assertEqual(b.client.get('/api/v1/models/runtime', headers=HB).json()['defaults'], {})                  # nothing promoted
            self.assertEqual(b.client.get('/api/v1/models/runtime', headers=HB).json()['currently']['runtimes'], [])       # nothing loaded
            self.assertEqual(len(imp['results']['warmup']['revision_ids']), 1)
        # importing again is a no-op for services and models, and a new policy version
        imp2 = b.client.post('/api/v1/bundles/import', headers=HB, json={'bundle': bundle, 'apply': True}).json()
        self.assertEqual(imp2['results']['services'], []); self.assertEqual(imp2['results']['models'], [])


@unittest.skipUnless(HAVE_RUNTIME, 'no numpy-capable compute interpreter on this host')
class DisagreementTests(unittest.TestCase):
    def test_groups_differences_and_evidence_bound_decisions(self):
        inst = ComputeInstance(); self.addCleanup(inst.close); c = inst.client; H = inst.h('owner'); R = inst.h('reviewer')
        w = inst.worker(); self.addCleanup(w.offline)
        jid = inst.compute_job('temporal_batch', batch_spec(private_label='DIS')); self.assertEqual(w.run_once()[1], 'succeeded')
        corrupt_output(inst, jid, flip_first_outcome, rewrite_vault=True)
        v1 = c.post('/api/v1/verification', headers=H, json={'job_id': jid, 'class': 'full_exact', 'params': {}}).json(); self.assertEqual(w.run_once()[1], 'succeeded')
        v2 = c.post('/api/v1/verification', headers=H, json={'job_id': jid, 'class': 'sampled_reference', 'params': {'sample_count': 364}}).json(); self.assertEqual(w.run_once()[1], 'succeeded')
        groups = c.get('/api/v1/verification/disagreements', headers=H).json()['groups']
        g = next(x for x in groups if x['target_job_id'] == jid)
        self.assertEqual(sorted(r['verification_id'] for r in g['records']), sorted([v1['id'], v2['id']])); self.assertTrue(g['open']); self.assertEqual(g['producer']['kind'], 'compute')
        self.assertEqual(g['differences'], []); self.assertIn('not explained', g['reading'])
        rec = next(r for r in g['records'] if r['verification_id'] == v1['id'])
        self.assertEqual((rec['state'], rec['outcome']), ('failed', 'failed')); self.assertGreaterEqual(rec['mismatch_count'], 1)
        # decisions: owner cannot decide; wrong evidence refused; the designated reviewer decides with bound evidence commitments
        body = {'decision': 'auditor_upheld', 'note': 'the first outcome was altered after commitment; the reference recomputation stands', 'evidence': [{'kind': 'verification', 'id': v1['id']}, {'kind': 'job', 'id': jid}]}
        self.assertEqual(c.post('/api/v1/verification/' + v1['id'] + '/decide', headers=H, json=body).status_code, 403)
        self.assertEqual(c.post('/api/v1/verification/' + v1['id'] + '/decide', headers=R, json=dict(body, evidence=[{'kind': 'job', 'id': 'j_nope'}])).status_code, 404)
        self.assertEqual(c.post('/api/v1/verification/' + v1['id'] + '/decide', headers=R, json=dict(body, decision='truth')).status_code, 422)
        d = c.post('/api/v1/verification/' + v1['id'] + '/decide', headers=R, json=body); self.assertEqual(d.status_code, 200, d.text); d = d.json()
        self.assertEqual((d['state'], d['resolution']['decision'], len(d['resolution']['evidence'])), ('resolved', 'auditor_upheld', 2))
        self.assertEqual(len(d['resolution']['evidence'][1]['commitment']), 64); self.assertIn('not an automatic truth', d['resolution']['adjudication'])
        self.assertEqual(c.post('/api/v1/verification/' + v1['id'] + '/decide', headers=R, json=body).status_code, 409)         # already decided
        g2 = next(x for x in c.get('/api/v1/verification/disagreements', headers=H).json()['groups'] if x['target_job_id'] == jid)
        self.assertTrue(g2['open'])                                                                                             # v2 is still open
        # the administrative decision leaves the audit outcome (and therefore any gate that needs a passing audit) unchanged; a fresh audit can still be requested
        self.assertEqual(d['result']['outcome'], 'failed'); self.assertIn('administrative record only', d['resolution']['effect'])
        self.assertEqual(c.post('/api/v1/verification', headers=H, json={'job_id': jid, 'class': 'analytical', 'params': {}}).status_code, 202)
