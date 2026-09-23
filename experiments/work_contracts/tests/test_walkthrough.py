"""The documented walkthrough runs verbatim, from a fresh directory, without source edits."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[3]


class WalkthroughTests(unittest.TestCase):
    def test_walkthrough_script_verbatim(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / 'pilot'
            proc = subprocess.run(['bash', 'experiments/work_contracts/pilot/walkthrough.sh', str(work)],
                                  cwd=ROOT, capture_output=True, text=True, timeout=300)
            self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
            summary = json.loads(proc.stdout[proc.stdout.rindex('{'):proc.stdout.rindex('}') + 1])
            self.assertEqual(summary['public_outcome'], 'INDETERMINATE')
            self.assertTrue(summary['work_completed'])
            self.assertEqual(summary['dominant_uncertainty_source_private'], 'segment:0')
            self.assertEqual(summary['counterfactual'], 'FEASIBLE')
            self.assertTrue(summary['dry_run_would_reserve'])
            self.assertEqual((summary['dispatch_state'], summary['retry_state']), ('CONFIRMED', 'CONFIRMED'))
            self.assertEqual(summary['reconcile'], 'terminal-already')
            self.assertEqual((summary['provider_balance_after_retry'], summary['provider_dispatches']), (4, 1))
            self.assertTrue(summary['public_import_verified'] and summary['private_import'])
            self.assertEqual(summary['campaign_exposure'], 1)
            self.assertEqual(summary['refusals'], ['REQUEST_ID_REBOUND', 'ENTITLEMENT_CONSUMED'])
            # Public artifacts never carry the private canary or numeric margins.
            for name in ('public-bundle.json', 'public-verify.json', 'campaign.json', 'dry-run.json', 'dispatch.json'):
                text = (work / name).read_text()
                self.assertNotIn('SYNTHETIC_PRIVATE_CANARY_73', text, name)
                self.assertNotIn('margin_width', text, name)
            self.assertNotIn('SYNTHETIC_PRIVATE_CANARY_73', proc.stdout + proc.stderr)
            self.assertEqual((work / 'owner' / 'private-input-vault.json').stat().st_mode & 0o777, 0o600)
