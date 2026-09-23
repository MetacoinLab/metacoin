"""CLI refusal codes and redaction on exceptional paths, dry-run boundary, legacy session labels."""
from contextlib import redirect_stdout, redirect_stderr
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from experiments.private_receipts import receipt as merkle
from experiments.work_contracts import cli, fixtures

CANARY = 'PRIVATE_CANARY_VALUE_991'


def invoke(*args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main([str(a) for a in args])
    return code, json.loads(out.getvalue()), err.getvalue()


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.expiry = int(time.time()) + 3600

    def prepare(self, job='job', outcome='INFEASIBLE', *flags):
        data = fixtures.inputs(outcome)
        data['private_label'] = CANARY
        merkle.write_new(self.root / (job + '-input.json'), data)
        code, prepared, _ = invoke('prepare', '--input', self.root / (job + '-input.json'), '--out-dir', self.root / job,
                                   '--job', job, '--expires-at', self.expiry, *flags)
        self.assertEqual(code, 0)
        return prepared['contract_digest']

    def test_refusal_paths_redact_private_values_and_paths_and_carry_codes(self):
        pin = self.prepare()
        owner = self.root / 'job'
        common = ('--contract', owner / 'contract.json', '--expected-contract-digest', pin)
        invoke('execute', *common, '--input-vault', owner / 'private-input-vault.json', '--out', self.root / 'ev.json')
        other = self.prepare('other', 'FEASIBLE')
        cases = {
            'PIN_MISMATCH': ('execute', '--contract', owner / 'contract.json', '--expected-contract-digest', other,
                             '--input-vault', owner / 'private-input-vault.json', '--out', self.root / 'x1.json'),
            'EVIDENCE_MISMATCH': ('audit', *common, '--input-vault', owner / 'private-input-vault.json',
                                  '--evidence-vault', self.root / 'other' / 'private-input-vault.json', '--out', self.root / 'x2.json'),
            'OUTPUT_EXISTS': ('execute', *common, '--input-vault', owner / 'private-input-vault.json', '--out', self.root / 'ev.json'),
            'FILE_MISSING': ('execute', *common, '--input-vault', self.root / 'absent.json', '--out', self.root / 'x3.json'),
            'JOURNAL_MISSING': ('status', '--journal', self.root / 'none.sqlite', '--request-id', 'r'),
            'EXPIRED': ('prepare', '--input', self.root / 'job-input.json', '--out-dir', self.root / 'late', '--job', 'late',
                        '--expires-at', 1),
        }
        for expected, args in cases.items():
            with self.subTest(code=expected):
                code, result, err = invoke(*args)
                self.assertEqual(code, 2)
                self.assertEqual(result['code'], expected)
                for text in (json.dumps(result), err):
                    self.assertNotIn(CANARY, text)
                    self.assertNotIn(self.temp.name, text)
                    self.assertNotIn('Traceback', text)

    def test_journal_commands_dry_run_and_legacy_session_are_explicit(self):
        pin = self.prepare('job', 'INDETERMINATE')
        owner = self.root / 'job'
        journal = self.root / 'journal.sqlite'
        self.assertEqual(invoke('campaign', 'init', '--journal', journal, '--campaign', 'c', '--limit', 2)[0], 0)
        self.assertEqual(invoke('campaign', 'init', '--journal', journal, '--campaign', 'c', '--limit', 2)[1]['code'], 'CAMPAIGN_MISMATCH')
        self.assertEqual(invoke('register', '--journal', journal, '--contract', owner / 'contract.json',
                                '--expected-contract-digest', pin)[0], 0)
        invoke('execute', '--contract', owner / 'contract.json', '--expected-contract-digest', pin,
               '--input-vault', owner / 'private-input-vault.json', '--out', self.root / 'ev.json')
        code, result, _ = invoke('request', '--journal', journal, '--job', 'job', '--request-id', 'r', '--out', self.root / 'req.json')
        self.assertEqual(result['code'], 'WORK_NOT_ACCEPTED')
        code, audited, _ = invoke('record-audit', '--journal', journal, '--job', 'job', '--input-vault', owner / 'private-input-vault.json',
                                  '--evidence-vault', self.root / 'ev.json', '--out', self.root / 'bundle.json')
        self.assertTrue(audited['work_completed'] and not audited['spend_permitted'])
        self.assertEqual(invoke('request', '--journal', journal, '--job', 'job', '--request-id', 'r', '--out', self.root / 'req.json')[0], 0)
        code, dry, _ = invoke('dispatch', '--journal', journal, '--request', self.root / 'req.json', '--dry-run',
                              '--adapter', 'legacy-simulation')
        self.assertEqual((code, dry['dry_run'], dry['dispatched'], dry['would_reserve'], dry['campaign_exposure']), (0, True, False, True, 0))
        self.assertEqual(invoke('campaign', 'show', '--journal', journal)[1]['exposure'], 0)  # dry run reserved nothing
        code, done, _ = invoke('dispatch', '--journal', journal, '--request', self.root / 'req.json', '--adapter', 'legacy-simulation')
        self.assertEqual((code, done['state'], done['adapter_session']), (0, 'CONFIRMED', 'process-scoped'))
        self.assertIn('vanish', done['persistence'])
        self.assertEqual(invoke('campaign', 'show', '--journal', journal)[1]['exposure'], 1)
        self.assertEqual(invoke('status', '--journal', journal, '--request-id', 'r')[1]['state'], 'CONFIRMED')
        self.assertEqual(invoke('status', '--journal', journal, '--request-id', 'r', '--actor', 'x')[1]['code'], 'UNAUTHORIZED')
        # durable provider needs an explicit initial balance to be created
        code, refused, _ = invoke('reconcile', '--journal', journal, '--request-id', 'r', '--adapter', 'durable-test-simulation',
                                  '--provider-state', self.root / 'p.json')
        self.assertEqual(refused['code'], 'ADAPTER_CAPABILITY')
        listing = invoke('campaign', 'show', '--journal', journal)[1]
        self.assertNotIn(CANARY, json.dumps(listing))
        self.assertEqual(listing['jobs'][0]['action']['state'], 'CONFIRMED')

    def test_hide_outcome_policy_keeps_outcome_out_of_public_paths(self):
        pin = self.prepare('hidden', 'INFEASIBLE', '--hide-outcome', '--require-feasible')
        owner = self.root / 'hidden'
        common = ('--contract', owner / 'contract.json', '--expected-contract-digest', pin)
        invoke('execute', *common, '--input-vault', owner / 'private-input-vault.json', '--out', self.root / 'hev.json')
        code, audited, _ = invoke('audit', *common, '--input-vault', owner / 'private-input-vault.json',
                                  '--evidence-vault', self.root / 'hev.json', '--out', self.root / 'hb.json')
        self.assertEqual((code, audited['work_completed']), (0, False))  # auditor-side: policy not met
        code, public, _ = invoke('verify', *common, '--bundle', self.root / 'hb.json', '--expected-root', audited['expected_evidence_root'])
        self.assertEqual(code, 0)
        self.assertNotIn('outcome', public['disclosed'])
        self.assertNotIn('INFEASIBLE', json.dumps(public) + (self.root / 'hb.json').read_text())

    def test_capabilities_table_is_machine_readable_and_honest(self):
        code, table, _ = invoke('capabilities')
        self.assertEqual(code, 0)
        self.assertEqual(table['transport']['http_402'], 'unavailable')
        self.assertFalse(table['adapters']['legacy-simulation']['real_funds'])
        self.assertEqual(table['contract_fields']['retention_seconds'], 'descriptive;no-deletion-service-implemented')
        self.assertEqual(table['external_team_pilot'], 'not-performed')
