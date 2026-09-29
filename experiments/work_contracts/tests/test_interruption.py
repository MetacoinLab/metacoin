"""Process-level interruption and concurrency: real child processes, exact crash points.

These are software recovery boundaries (a hard os._exit at a chosen point), not
power-loss or storage-controller tests. Where a rail cannot say what happened,
retaining exposure as OUTCOME_UNKNOWN is the expected conservative result.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from experiments.private_receipts import receipt as merkle
from experiments.work_contracts import contract, fixtures
from experiments.work_contracts.execution_state import Journal
from experiments.work_contracts.tests.durable_provider import DurableProvider
from experiments.work_contracts.tests.interruption_child import CRASH_EXIT

NOW = 1_900_000_000
ROOT = Path(__file__).resolve().parents[3]
POINTS = ('before_reserve_commit', 'after_reserve_commit', 'after_intent_commit',
          'after_effect_before_response', 'before_confirm_commit', 'after_confirm_commit')


class ProcessHarness(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)
        self.journal_path = self.dir / 'journal.sqlite'
        self.provider_path = self.dir / 'provider.json'
        self.journal = Journal(self.journal_path, 'campaign', 3)
        DurableProvider(self.provider_path, initial_balance=10)

    def job(self, name='job', amount=1):
        terms, inputs, evidence = fixtures.prepare(name, expires_at=NOW + 100, amount=amount,
                                                   capability='durable_test_simulation')
        self.journal.register(terms, contract.digest(terms), 'local-owner', NOW)
        self.journal.audit(name, inputs, evidence, 'local-auditor', NOW)

    def request_file(self, job='job', request_id='request'):
        path = self.dir / (job + '-' + request_id + '.json')
        merkle.write_new(path, self.journal.request(job, request_id))
        return path

    def child(self, request_path, *extra, env=None, wait=True):
        cmd = [sys.executable, '-m', 'experiments.work_contracts.tests.interruption_child',
               '--journal', str(self.journal_path), '--provider', str(self.provider_path),
               '--request', str(request_path), *extra]
        proc = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                env=dict(os.environ, **(env or {})))
        return self.finish(proc) if wait else proc

    def finish(self, proc):
        out, err = proc.communicate(timeout=60)
        payload = json.loads(out.strip().splitlines()[-1]) if out.strip() else None
        return proc.returncode, payload, err

    def provider(self, durable=True):
        return DurableProvider(self.provider_path, durable=durable)

    def state(self, request_id='request'):
        try:
            return Journal(self.journal_path, 'campaign', 3).status(request_id, 'agent-fixture')['state']
        except merkle.Invalid:
            return 'NOT_REQUESTED'


class InterruptionPointTests(ProcessHarness):
    def test_each_point_with_a_durable_provider(self):
        for point in POINTS:
            with self.subTest(point=point):
                self.setUp()
                self.job()
                request = self.request_file()
                code, payload, err = self.child(request, '--crash-at', point)
                self.assertEqual(code, CRASH_EXIT, err)
                self.assertIsNone(payload)
                fresh = Journal(self.journal_path, 'campaign', 3)
                snap = self.provider().snapshot()
                if point == 'before_reserve_commit':
                    with self.assertRaisesRegex(merkle.Invalid, 'unknown action'):
                        fresh.status('request', 'agent-fixture')
                    self.assertEqual((fresh.exposure(), snap['dispatches']), (0, 0))
                    expected_after = 'CONFIRMED'
                elif point == 'after_reserve_commit':
                    self.assertEqual((self.state(), fresh.exposure(), snap['dispatches']), ('RESERVED', 1, 0))
                    expected_after = 'CONFIRMED'
                elif point == 'after_intent_commit':
                    self.assertEqual((self.state(), fresh.exposure(), snap['dispatches']), ('SUBMISSION_PENDING', 1, 0))
                    self.assertNotIn('request', snap['intents'])
                    expected_after = 'FAILED_CONFIRMED'  # authoritative absence, key voided
                elif point in ('after_effect_before_response', 'before_confirm_commit'):
                    self.assertEqual((self.state(), fresh.exposure(), snap['dispatches'], snap['balance']),
                                     ('SUBMISSION_PENDING', 1, 1, 9))
                    expected_after = 'CONFIRMED'
                else:
                    self.assertEqual((self.state(), fresh.exposure(), snap['dispatches'], snap['balance']),
                                     ('CONFIRMED', 1, 1, 9))
                    expected_after = 'CONFIRMED'
                # Recovery: reconcile never submits; dispatch resumes only a RESERVED action.
                if point == 'before_reserve_commit':
                    with self.assertRaisesRegex(merkle.Invalid, 'unknown action'):
                        fresh.reconcile('request', 'agent-fixture', self.provider(), NOW)
                    reconciled = None
                else:
                    reconciled = fresh.reconcile('request', 'agent-fixture', self.provider(), NOW)
                resumed = fresh.dispatch(merkle.read(request), 'agent-fixture', self.provider(), NOW)
                after = self.provider().snapshot()
                self.assertEqual(resumed['state'], expected_after, point)
                if expected_after == 'CONFIRMED':
                    self.assertEqual((after['dispatches'], after['balance'], fresh.exposure()), (1, 9, 1))
                else:
                    self.assertEqual((after['dispatches'], after['balance'], fresh.exposure()), (0, 10, 0))
                    self.assertEqual(reconciled['result']['reference'], 'provider-no-record')
                    self.assertIn('request', after['voided'])
                    # a late in-flight submission with the voided key is refused by the provider
                    late = self.provider().submit(merkle.read(request))
                    self.assertEqual((late['state'], late['reference']), ('FAILED_CONFIRMED', 'voided-before-submission'))
                    self.assertEqual(self.provider().snapshot()['balance'], 10)
                self.temp.cleanup()

    def test_points_with_a_provider_that_keeps_no_record(self):
        for point in ('after_intent_commit', 'after_effect_before_response', 'before_confirm_commit'):
            with self.subTest(point=point):
                self.setUp()
                self.job()
                request = self.request_file()
                code, _, err = self.child(request, '--crash-at', point, '--amnesiac')
                self.assertEqual(code, CRASH_EXIT, err)
                snap = self.provider(durable=False).snapshot()
                moved = point != 'after_intent_commit'
                self.assertEqual((self.state(), snap['balance']), ('SUBMISSION_PENDING', 9 if moved else 10))
                fresh = Journal(self.journal_path, 'campaign', 3)
                # Nothing authoritative exists: exposure stays, nothing is resubmitted.
                for _ in range(2):
                    self.assertEqual(fresh.reconcile('request', 'agent-fixture', self.provider(False), NOW)['state'],
                                     'OUTCOME_UNKNOWN')
                    self.assertEqual(fresh.dispatch(merkle.read(request), 'agent-fixture', self.provider(False), NOW)['state'],
                                     'OUTCOME_UNKNOWN')
                self.assertEqual(fresh.exposure(), 1)
                self.assertEqual(self.provider(False).snapshot()['dispatches'], 1 if moved else 0)
                self.temp.cleanup()


class CrossProcessConcurrencyTests(ProcessHarness):
    def race(self, request_paths, extra=()):
        barrier = self.dir / 'go'
        procs = [self.child(path, '--barrier', str(barrier), *extra, wait=False,
                            env={'METACOIN_TEST_SLOW_SUBMIT_MS': '150'}) for path in request_paths]
        time.sleep(0.5)  # let every child reach the barrier poll loop (import time), then release
        barrier.write_text('go')
        return [self.finish(proc) for proc in procs]

    def test_identical_requests_from_six_processes_dispatch_once(self):
        self.job()
        request = self.request_file()
        results = self.race([request] * 6)
        for code, payload, err in results:
            self.assertEqual(code, 0, err)
            self.assertIn(payload['result']['state'], ('CONFIRMED', 'SUBMISSION_PENDING'))
        snap = self.provider().snapshot()
        self.assertEqual((snap['dispatches'], snap['balance'], self.state(), self.journal.exposure()),
                         (1, 9, 'CONFIRMED', 1))

    def test_two_jobs_race_for_one_remaining_slot(self):
        self.job('a', amount=2)
        self.job('b', amount=2)
        results = self.race([self.request_file('a', 'ra'), self.request_file('b', 'rb')])
        outcomes = sorted(payload['result']['state'] if payload['ok'] else payload['code'] for _, payload, _ in results)
        self.assertEqual(outcomes, ['BUDGET_EXHAUSTED', 'CONFIRMED'])
        snap = self.provider().snapshot()
        self.assertEqual((snap['dispatches'], snap['balance'], self.journal.exposure()), (1, 8, 2))

    def test_one_job_under_two_request_ids_across_processes(self):
        self.job()
        results = self.race([self.request_file('job', 'r1'), self.request_file('job', 'r2')])
        outcomes = sorted(payload['result']['state'] if payload['ok'] else payload['code'] for _, payload, _ in results)
        self.assertEqual(outcomes, ['CONFIRMED', 'ENTITLEMENT_CONSUMED'])
        self.assertEqual(self.provider().snapshot()['dispatches'], 1)

    def test_reconcile_racing_a_delayed_answer_keeps_exposure_then_confirms(self):
        self.job()
        request = self.request_file()
        proc = self.child(request, wait=False, env={'METACOIN_TEST_SLOW_SUBMIT_MS': '600'})
        deadline = time.monotonic() + 10
        while self.state() != 'SUBMISSION_PENDING':  # wait for an observable state, not a guess
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.01)
        early = Journal(self.journal_path, 'campaign', 3).reconcile('request', 'agent-fixture', self.provider(), NOW)
        self.assertEqual((early['state'], early['reconciliation']), ('OUTCOME_UNKNOWN', 'unavailable-exposure-retained'))
        code, payload, err = self.finish(proc)
        self.assertEqual(code, 0, err)
        self.assertEqual(payload['result']['state'], 'CONFIRMED')
        snap = self.provider().snapshot()
        self.assertEqual((snap['dispatches'], snap['balance'], self.state(), self.journal.exposure()), (1, 9, 'CONFIRMED', 1))
