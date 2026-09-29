"""§58: the executable examples run against a real instance through public interfaces only (separate processes,
credential files, documented environment variables)."""
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

from metacoin_service.tests.journeys_expansion import Journeys, PY
from metacoin_service.tests.test_models import GEN, EMB
from metacoin_service.tests.test_service import ROOT

EX = ROOT / 'examples' / 'expansion'


class ExampleTests(unittest.TestCase):
    def setUp(self):
        self.j = Journeys(); self.addCleanup(self.j.close)
        if self.j.models_ok:
            rc, reg = self.j.cli('owner', 'model-register', '--model-id', GEN['model_id'], '--hub-repo', GEN['hub_repo'], '--revision', GEN['revision'], '--operations', 'generate', '--license', 'apache-2.0')
            self.j.cli('owner', 'model-action', reg['id'], 'promote', '--operation', 'generate')
            rc, rege = self.j.cli('owner', 'model-register', '--model-id', EMB['model_id'], '--hub-repo', EMB['hub_repo'], '--revision', EMB['revision'], '--operations', 'embed', '--license', 'apache-2.0')
            self.j.cli('owner', 'model-action', rege['id'], 'promote', '--operation', 'embed')
        self.j.worker_bg('w-examples')
        self.env = {'PATH': os.environ.get('PATH', ''), 'HOME': os.environ.get('HOME', '/'), 'LANG': 'C.UTF-8', 'PYTHONPATH': str(ROOT), 'METACOIN_BASE_URL': self.j.base, 'METACOIN_CREDENTIAL_FILE': str(self.j.creds['owner'])}

    def run_example(self, name, extra=None, timeout=600):
        p = subprocess.run([PY, str(EX / name)], cwd=str(EX), env=dict(self.env, **(extra or {})), capture_output=True, text=True, timeout=timeout)
        try:
            out = json.loads(p.stdout)
        except ValueError:
            out = {'stdout': p.stdout[-400:], 'stderr': p.stderr[-600:]}
        return p.returncode, out

    def test_examples_run_through_public_interfaces(self):
        rc, out = self.run_example('calibrated_prediction.py')
        self.assertEqual(rc, 0, out); self.assertTrue(out['verification_passed']); self.assertEqual((out['inside']['domain_status'], out['outside']['domain_status']), ('interpolation', 'extrapolation'))
        rc, out = self.run_example('audited_result.py')
        self.assertEqual(rc, 0, out); self.assertEqual(out['state'], 'passed'); self.assertTrue(out['signature_valid'])
        rc, out = self.run_example('mcp_bounded_job.py')
        self.assertEqual(rc, 0, out); self.assertTrue(out['plan']['valid']); self.assertEqual(out['state'], 'queued') if out['state'] == 'queued' else self.assertIn(out['state'], ('running', 'succeeded'))
        if self.j.models_ok:
            rc, out = self.run_example('private_answer.py')
            self.assertEqual(rc, 0, out); self.assertEqual(out['status'], 'answered'); self.assertTrue(all(c['valid'] for c in out['citations']))
        # federated: TLS coordinator started by the harness, documented env variables only
        self.j.start_tls_api()
        rc, out = self.run_example('federated_execution.py', {'METACOIN_TLS_BASE_URL': 'https://127.0.0.1:%d' % self.j.tls_port, 'METACOIN_TLS_CA': self.j.tls['ca']})
        self.assertEqual(rc, 0, out); self.assertEqual(out['state'], 'succeeded'); self.assertIn('result:from_node', out['transfers'])
        # a missing credential file is reported precisely, without a traceback
        p = subprocess.run([PY, str(EX / 'calibrated_prediction.py')], cwd=str(EX), env={k: v for k, v in self.env.items() if k != 'METACOIN_CREDENTIAL_FILE'}, capture_output=True, text=True, timeout=60)
        self.assertNotEqual(p.returncode, 0); self.assertIn('METACOIN_CREDENTIAL_FILE', p.stdout + p.stderr); self.assertNotIn('Traceback', p.stderr)
