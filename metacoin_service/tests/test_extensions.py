"""Extension backlog items: verification policy templates (§65-6) bound before execution and not weakenable after a
result is seen; recovery rehearsal (§65-10) in an isolated directory that leaves the service untouched."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from metacoin_service.tests.test_compute_engine import ComputeInstance, HAVE_RUNTIME, batch_spec
from metacoin_service.tests.test_service import ROOT, ENV

PY = sys.executable


@unittest.skipUnless(HAVE_RUNTIME, 'no numpy-capable compute interpreter on this host')
class ExtensionTests(unittest.TestCase):
    def setUp(self):
        self.inst = ComputeInstance(); self.addCleanup(self.inst.close); self.c = self.inst.client; self.H = self.inst.h('owner'); self.R = self.inst.h('reviewer')
        self.w = self.inst.worker(); self.addCleanup(self.w.offline)

    def test_verification_policy_template_binds_and_cannot_be_weakened(self):
        tpl = self.c.post('/api/v1/verification/policies', headers=self.H, json={'name': 'strict-sampled', 'class': 'sampled_reference', 'params': {'sample_count': 64}, 'max_work': 4096}).json()
        self.assertEqual((tpl['version'], tpl['class'], tpl['verifier_current']), (1, 'sampled_reference', True))
        self.assertEqual(self.c.post('/api/v1/verification/policies', headers=self.H, json={'name': 'x', 'class': 'full_exact', 'max_work': 10 ** 9}).status_code, 422)
        r = self.c.post('/api/v1/contracts', headers=self.H, json={'kind': 'temporal_batch', 'title': 'tpl', 'inputs': batch_spec(private_label='TPL'), 'policy': {'reviewer_id': self.inst.ids['reviewer'], 'verification_policy_id': tpl['id']}})
        self.assertEqual(r.status_code, 201, r.text); cid = r.json()['id']
        self.assertEqual(self.c.post('/api/v1/contracts/' + cid + '/freeze', headers=self.H).status_code, 200)
        frozen = self.c.get('/api/v1/contracts/' + cid, headers=self.H).json()
        self.assertEqual((frozen['policy']['required_verification'], frozen['policy']['verification_policy_id']), ('sampled_reference', tpl['id']))
        jid = self.c.post('/api/v1/jobs', headers=self.H, json={'contract_id': cid}).json()['id']
        self.assertEqual(self.w.run_once()[1], 'succeeded')
        # a weaker audit (fewer samples than the template) does not open the gate; the template's minimum does
        v8 = self.c.post('/api/v1/verification', headers=self.H, json={'job_id': jid, 'class': 'sampled_reference', 'params': {'sample_count': 8}}).json()
        self.assertEqual(self.w.run_once()[1], 'succeeded')
        refused = self.c.post('/api/v1/jobs/' + jid + '/review-request', headers=self.H).json()['detail']
        self.assertEqual((refused['code'], refused['minimum_sample_count']), ('awaiting_verification', 64))
        v64 = self.c.post('/api/v1/verification', headers=self.H, json={'job_id': jid, 'class': 'sampled_reference', 'params': {'sample_count': 64}}).json()
        self.assertEqual(self.w.run_once()[1], 'succeeded')
        self.assertEqual(self.c.post('/api/v1/jobs/' + jid + '/review-request', headers=self.H).status_code, 200)
        # retiring the template refuses new contracts but does not change frozen ones; a new version is a new id
        self.c.post('/api/v1/verification/policies/' + tpl['id'] + '/retire', headers=self.H)
        r2 = self.c.post('/api/v1/contracts', headers=self.H, json={'kind': 'temporal_batch', 'title': 'tpl2', 'inputs': batch_spec(private_label='TPL2'), 'policy': {'reviewer_id': self.inst.ids['reviewer'], 'verification_policy_id': tpl['id']}}).json()
        self.assertEqual(self.c.post('/api/v1/contracts/' + r2['id'] + '/freeze', headers=self.H).json()['detail']['code'], 'verification_policy_unavailable')
        tpl2 = self.c.post('/api/v1/verification/policies', headers=self.H, json={'name': 'strict-sampled', 'class': 'sampled_reference', 'params': {'sample_count': 128}}).json()
        self.assertEqual((tpl2['version'], tpl2['id'] != tpl['id']), (2, True))
        self.assertEqual(len(self.c.get('/api/v1/verification/policies', headers=self.inst.h('viewer')).json()['items']), 2)
        self.assertEqual(self.c.post('/api/v1/verification/policies', headers=self.inst.h('viewer'), json={'name': 'v', 'class': 'analytical'}).status_code, 403)

    def test_recovery_rehearsal_is_isolated_and_reports_missing_pieces(self):
        jid = self.inst.compute_job('temporal_batch', batch_spec(private_label='REH')); self.assertEqual(self.w.run_once()[1], 'succeeded')
        dest = Path(tempfile.mkdtemp()) / 'rehearsal'
        p = subprocess.run([PY, '-m', 'metacoin_service', '--home', str(self.inst.home), 'rehearse-recovery', str(dest)], cwd=ROOT, env=ENV, capture_output=True, text=True, timeout=300)
        self.assertEqual(p.returncode, 0, p.stderr[-500:])
        out = json.loads(p.stdout)
        self.assertEqual((out['checks']['integrity'], out['checks']['foreign_keys'], out['restore']['keys_restored'], out['checks']['sample_artifact_decrypts']), ('ok', 0, True, True))
        self.assertEqual(out['checks']['reconciliation_gate'], '1'); self.assertEqual(out['missing_keys'], []); self.assertTrue((dest / 'REHEARSAL.json').exists())
        self.assertEqual(self.c.get('/api/v1/jobs/' + jid, headers=self.H).json()['state'], 'succeeded')       # the live instance is untouched
        self.assertEqual(self.c.get('/api/v1/status', headers=self.H).status_code, 200)
        # a rehearsal without the keys reports the missing material instead of an empty success
        dest2 = Path(tempfile.mkdtemp()) / 'r2'
        from metacoin_service import ops, config
        manifest = ops.backup(self.inst.settings, dest2 / 'backup', include_keys=False)
        restored = config.Settings(home=dest2 / 'home', provider_mode='simulation'); rest = ops.restore(dest2 / 'backup', restored)
        self.assertFalse(rest['keys_restored'])
        from metacoin_service.artifacts import ArtifactStore
        from metacoin_service.db import Database
        with Database(restored.db_path).read() as db:
            row = db.execute("SELECT id, workspace FROM artifacts WHERE encrypted=1 LIMIT 1").fetchone()
            with self.assertRaises(Exception):
                ArtifactStore(restored).load(db, row['id'], row['workspace'])
