"""Verifier evolution, retry/expiry semantics, response binding, lock scope, refusal codes."""
from copy import deepcopy
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
from experiments.private_receipts import receipt as merkle
from experiments.work_contracts import acceptance, contract, energy_analysis as energy, fixtures, refusals, verifiers
from experiments.work_contracts.execution_state import Journal
from integrations.x402.legacy_adapter import LegacyAdapter

NOW = 1_900_000_000
HERE = Path(__file__).parent
HISTORICAL = HERE / 'fixtures' / 'historical_v0_public_demo.json'


class VerifierEvolutionTests(unittest.TestCase):
    """Receipts produced by the superseded v0 bundle (the 2026-09-18 handoff's
    public demo output) stay verifiable read-only; nothing new may be
    registered, executed or paid under that bundle."""

    def setUp(self):
        self.samples = merkle.read(HISTORICAL)['public_samples']
        self.assertEqual(len(self.samples), 3)

    def test_historical_public_receipts_verify_as_historical(self):
        for sample in self.samples:
            terms = sample['contract']
            self.assertEqual(terms['verifier_id'], 'local-energy-audit/v0')
            self.assertIn(terms['verifier_digest'], verifiers.HISTORICAL)
            self.assertEqual(contract.verifier_status(terms), 'historical')
            public = acceptance.verify_public(terms, sample['expected_contract_digest'],
                                              sample['bundle'], sample['expected_evidence_root'])
            self.assertEqual(public['verifier_status'], 'historical')
            self.assertIn(public['disclosed']['outcome'], energy.OUTCOMES)
            self.assertFalse(public['task_correctness_proven'])

    def test_historical_bundle_cannot_authorize_anything_new(self):
        sample = self.samples[0]
        terms, pin = sample['contract'], sample['expected_contract_digest']
        with self.assertRaisesRegex(merkle.Invalid, 'superseded'):
            contract.validate(terms)
        with self.assertRaisesRegex(merkle.Invalid, 'superseded'):
            acceptance.execute(terms, pin, fixtures.agree('x')[1])
        with tempfile.TemporaryDirectory() as tmp:
            journal = Journal(Path(tmp) / 'j.sqlite', 'campaign', 3)
            with self.assertRaisesRegex(merkle.Invalid, 'superseded'):
                journal.register(terms, pin, 'local-owner', NOW)

    def test_unknown_or_mismatched_bundles_are_refused_even_read_only(self):
        sample = self.samples[0]
        terms = deepcopy(sample['contract'])
        for key, value in (('verifier_digest', 'e' * 64), ('verifier_id', contract.VERIFIER)):
            altered = dict(terms, **{key: value})
            with self.subTest(key=key), self.assertRaisesRegex(merkle.Invalid, 'unknown verifier bundle'):
                contract.validate(altered, mode='historical')
        with self.assertRaises(ValueError):
            contract.validate(terms, mode='whatever')

    def test_current_bundle_is_computed_and_differs_from_v0(self):
        current = contract.verifier_digest()
        self.assertNotIn(current, verifiers.HISTORICAL)
        terms = fixtures.agree('now')[0]
        self.assertEqual(contract.verifier_status(terms), 'current')
        self.assertEqual(terms['verifier_id'], verifiers.CURRENT_ID)
        # Journal inspection labels superseded registrations without touching them.
        with tempfile.TemporaryDirectory() as tmp:
            journal = Journal(Path(tmp) / 'j.sqlite', 'campaign', 3)
            old = self.samples[0]
            with sqlite3.connect(journal.path) as db:  # simulate a pre-upgrade registration
                db.execute('INSERT INTO jobs(id, contract, digest) VALUES (?, ?, ?)',
                           (old['contract']['job_id'], merkle.canonical(old['contract']).decode(),
                            old['expected_contract_digest']))
            listing = journal.inspect()
            self.assertEqual(listing['jobs'][0]['verifier_status'], 'historical')
            with self.assertRaisesRegex(merkle.Invalid, 'superseded'):
                journal.request(old['contract']['job_id'], 'r')


class JournalSemanticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'journal.sqlite'
        self.state = Journal(self.path, 'campaign', 3)
        self.faucet = fixtures.funded_faucet(amount=10)
        self.adapter = LegacyAdapter(self.faucet)

    def add(self, job='job', outcome='FEASIBLE', **kwargs):
        terms, inputs, evidence = fixtures.prepare(job, outcome, expires_at=NOW + 100, **kwargs)
        self.state.register(terms, contract.digest(terms), 'local-owner', NOW)
        return terms, inputs, evidence, self.state.audit(job, inputs, evidence, 'local-auditor', NOW)

    def test_identical_retry_after_expiry_returns_recorded_state_without_new_right(self):
        self.add()
        request = self.state.request('job', 'request')
        with patch.object(self.adapter, 'submit', wraps=self.adapter.submit) as submit:
            first = self.state.dispatch(request, 'agent-fixture', self.adapter, NOW)
            late = self.state.dispatch(request, 'agent-fixture', self.adapter, NOW + 100)
            self.assertEqual(submit.call_count, 1)
        self.assertEqual((first['state'], late['state']), ('CONFIRMED', 'CONFIRMED'))
        self.assertEqual(self.faucet.balance_of('agent-fixture'), 9)
        with self.assertRaisesRegex(merkle.Invalid, 'authorization expired'):
            self.state.dispatch(self.state.request('job', 'fresh'), 'agent-fixture', self.adapter, NOW + 100)
        self.add('b')
        with self.assertRaisesRegex(merkle.Invalid, 'authorization expired'):
            self.state.dispatch(self.state.request('b', 'b-request'), 'agent-fixture', self.adapter, NOW + 100)
        self.assertEqual(self.state.exposure(), 1)
        # Read-only paths remain available after expiry.
        self.assertEqual(self.state.status('request', 'agent-fixture')['state'], 'CONFIRMED')
        self.assertEqual(self.state.reconcile('request', 'agent-fixture', self.adapter, NOW + 100)['reconciliation'],
                         'terminal-already')

    def test_semantically_inconsistent_answers_preserve_uncertainty(self):
        class WrongUnits(LegacyAdapter):
            def submit(self, request):
                return dict(super().submit(request), compute_units=999)

        class FailureWithUnits(LegacyAdapter):
            def submit(self, request):
                return dict(super().submit(request), state='FAILED_CONFIRMED', compute_units=1)

        for index, adapter in enumerate((WrongUnits(self.faucet), FailureWithUnits(self.faucet))):
            job = 'job-' + str(index)
            self.add(job)
            result = self.state.dispatch(self.state.request(job, 'r-' + str(index)), 'agent-fixture', adapter, NOW)
            self.assertEqual(result['state'], 'OUTCOME_UNKNOWN')
        self.assertEqual(self.state.exposure(), 2)

    def test_duplicate_and_conflicting_answers_in_any_order(self):
        self.add()
        request = self.state.request('job', 'request')
        confirmed = self.state.dispatch(request, 'agent-fixture', self.adapter, NOW)['result']
        self.state._finish('request', confirmed, self.adapter)  # duplicate: no-op
        conflicting = dict(confirmed, state='FAILED_CONFIRMED', compute_units=0)
        with self.assertRaisesRegex(merkle.Invalid, 'conflicting terminal outcome'):
            self.state._finish('request', conflicting, self.adapter)
        self.assertEqual(self.state.status('request', 'agent-fixture')['result'], confirmed)
        # An answer for a row that never recorded submission intent is unbound.
        self.add('b')
        second = self.state.request('b', 'second')
        with patch.object(self.state, '_claim', return_value=False):
            self.state.dispatch(second, 'agent-fixture', self.adapter, NOW)
        answer = dict(confirmed, request_digest=self.state.status('second', 'agent-fixture') and
                      __import__('integrations.x402.legacy_adapter', fromlist=['request_digest']).request_digest(second))
        with self.assertRaisesRegex(merkle.Invalid, 'unbound adapter response'):
            self.state._finish('second', answer, self.adapter)
        self.assertEqual(self.state.status('second', 'agent-fixture')['state'], 'RESERVED')

    def test_private_audit_does_not_hold_the_write_lock(self):
        terms, inputs, evidence = fixtures.prepare('job', expires_at=NOW + 100)
        self.state.register(terms, contract.digest(terms), 'local-owner', NOW)
        observed = {}
        real = acceptance.audit

        def slow_audit(*args, **kwargs):
            db = sqlite3.connect(self.path, timeout=1, isolation_level=None)
            try:
                db.execute('BEGIN IMMEDIATE')  # would raise 'database is locked' under the old design
                observed['lock_free'] = True
                db.execute('ROLLBACK')
            finally:
                db.close()
            return real(*args, **kwargs)

        with patch('experiments.work_contracts.acceptance.audit', side_effect=slow_audit):
            result = self.state.audit('job', inputs, evidence, 'local-auditor', NOW)
        self.assertTrue(observed['lock_free'] and result['work_completed'])

    def test_audit_recheck_refuses_when_another_root_was_recorded_meanwhile(self):
        terms, inputs, evidence = fixtures.prepare('job', expires_at=NOW + 100)
        self.state.register(terms, contract.digest(terms), 'local-owner', NOW)
        _, other = acceptance.execute(terms, contract.digest(terms), inputs)
        real = acceptance.audit
        raced = []

        def racing_audit(*args, **kwargs):
            if not raced:  # race exactly once; the racer's own audit runs unpatched logic
                raced.append(True)
                Journal(self.path, 'campaign', 3).audit('job', inputs, other, 'local-auditor', NOW)
            return real(*args, **kwargs)

        with patch('experiments.work_contracts.acceptance.audit', side_effect=racing_audit):
            with self.assertRaisesRegex(merkle.Invalid, 'accepted evidence root is immutable'):
                self.state.audit('job', inputs, evidence, 'local-auditor', NOW)
        self.assertEqual(self.state.inspect()['jobs'][0]['audited'], True)

    def test_journal_refuses_directory_writable_by_others(self):
        shared = Path(self.temp.name) / 'shared'
        shared.mkdir()
        shared.chmod(0o707)  # explicit: mkdir's mode is subject to umask
        try:
            with self.assertRaisesRegex(merkle.Invalid, 'writable by others'):
                Journal(shared / 'j.sqlite', 'campaign', 3)
        finally:
            shared.chmod(0o700)

    def test_inspect_withholds_outcome_under_hide_policy(self):
        self.add('shown')
        self.add('hidden', 'INFEASIBLE', disclose_outcome=False)
        listing = {job['job_id']: job for job in self.state.inspect()['jobs']}
        self.assertEqual(listing['shown']['scientific_outcome'], 'FEASIBLE')
        self.assertEqual(listing['hidden']['scientific_outcome'], 'withheld-by-policy')
        self.assertNotIn('INFEASIBLE', json.dumps(listing['hidden']))
        self.assertEqual(self.state.inspect()['available'], 3)

    def test_concurrent_reserve_plus_status_and_reconcile_during_delayed_answer(self):
        self.add()
        request = self.state.request('job', 'request')
        gate = threading.Event()
        seen = {}
        path = self.path

        class Delayed(LegacyAdapter):
            def submit(self, request):
                answer = super().submit(request)
                seen['pending'] = Journal(path, 'campaign', 3).status('request', 'agent-fixture')['state']
                seen['reconciled'] = Journal(path, 'campaign', 3).reconcile('request', 'agent-fixture', self)['state']
                gate.set()
                return answer

        adapter = Delayed(self.faucet)
        result = self.state.dispatch(request, 'agent-fixture', adapter, NOW)
        self.assertTrue(gate.is_set())
        self.assertEqual(seen['pending'], 'SUBMISSION_PENDING')
        # Reconciliation from inside the same adapter instance resolves it early;
        # the late dispatcher answer is then an identical duplicate, not a conflict.
        self.assertEqual(seen['reconciled'], 'CONFIRMED')
        self.assertEqual(result['state'], 'CONFIRMED')
        self.assertEqual(self.faucet.balance_of('agent-fixture'), 9)


class RefusalCodeTests(unittest.TestCase):
    SOURCES = [HERE.parent / name for name in ('contract.py', 'energy_analysis.py', 'acceptance.py',
                                                'execution_state.py', 'explanation.py', 'cli.py', 'packages.py')]
    SOURCES += [Path(merkle.__file__), HERE.parent.parent.parent / 'integrations' / 'x402' / 'legacy_adapter.py']

    def test_every_refusal_message_is_a_constant_with_a_code(self):
        pattern = re.compile(r'Invalid\(((?:\'[^\']*\'|"[^"]*"|[^()])*?)\)', re.S)
        for path in self.SOURCES:
            if not path.exists():
                continue
            for match in pattern.finditer(path.read_text()):
                arg = match.group(1).strip()
                if arg in ('', 'ValueError'):
                    continue  # class definition / base class reference
                with self.subTest(file=path.name, arg=arg):
                    self.assertRegex(arg, r'^(\'[^\']*\'|"[^"]*")$', 'refusal message must be one string literal')
                    self.assertIn(arg[1:-1], refusals.CODES)
        for code in set(refusals.CODES.values()):
            self.assertIn(code, refusals.ACTIONS)

    def test_classification_never_echoes_foreign_exception_text(self):
        secret = 'PRIVATE_PATH_CANARY'
        for exc in (FileNotFoundError(secret), FileExistsError(secret), PermissionError(secret),
                    sqlite3.OperationalError('database is locked ' + secret), KeyError(secret), OSError(secret)):
            code, message = refusals.classify(exc)
            self.assertIsNone(message)
            self.assertIn(code, refusals.ACTIONS)
        self.assertEqual(refusals.classify(merkle.Invalid('campaign budget exhausted')), ('BUDGET_EXHAUSTED', 'campaign budget exhausted'))
