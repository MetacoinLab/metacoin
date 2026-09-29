"""Bounded model-based testing of the payment state machine.

A tiny reference model, written separately from execution_state.py, is driven
with the same seeded action sequences as the real journal; observable state
(per-action state class, exposure, provider debits) must agree after every
step. Alphabet and depth are finite and documented; this explores, it does not
prove. Set METACOIN_MODEL_SEQUENCES to explore more.
"""
import os
from pathlib import Path
import random
import tempfile
import unittest
from experiments.private_receipts import receipt as merkle
from experiments.work_contracts import contract, fixtures
from experiments.work_contracts.execution_state import Journal
from experiments.work_contracts.tests.durable_provider import DurableProvider

NOW = 1_900_000_000
JOBS = ('a', 'b')
AMOUNT, CAP = 2, 3
ALPHABET = ('dispatch:ok', 'dispatch:lose_ack', 'dispatch:never_reached',
            'reconcile:durable', 'reconcile:amnesiac', 'expire', 'restart')
DEPTH = 8


class Model:
    """Reference semantics. States: NONE, RESERVED, UNRESOLVED, CONFIRMED, FAILED."""

    def __init__(self):
        self.state = {job: 'NONE' for job in JOBS}
        self.intent = {job: False for job in JOBS}      # provider accepted the request
        self.record = {job: False for job in JOBS}      # provider holds a durable outcome
        self.expired = False
        self.debits = 0

    @property
    def exposure(self):
        return sum(AMOUNT for job in JOBS if self.state[job] not in ('NONE', 'FAILED'))

    def dispatch(self, job, mode):
        state = self.state[job]
        if state in ('CONFIRMED', 'FAILED', 'UNRESOLVED'):
            return 'existing'                           # retry: no new right, no resubmission
        if state == 'NONE':
            if self.expired:
                return 'EXPIRED'
            if self.exposure + AMOUNT > CAP:
                return 'BUDGET_EXHAUSTED'
            self.state[job] = 'RESERVED'
        if self.expired:                                # RESERVED but never dispatched
            self.state[job] = 'FAILED'
            return 'released'
        self.state[job] = 'UNRESOLVED'                  # intent durable before the call
        if mode == 'never_reached':
            return 'lost'
        self.intent[job] = self.record[job] = True
        self.debits += 1
        if mode == 'ok':
            self.state[job] = 'CONFIRMED'
        return 'answered' if mode == 'ok' else 'lost'

    def reconcile(self, job, kind):
        state = self.state[job]
        if state == 'NONE':
            return 'unknown'
        if state == 'RESERVED' and self.expired:
            self.state[job] = 'FAILED'
        elif state == 'UNRESOLVED' and kind == 'durable':
            if self.record[job]:
                self.state[job] = 'CONFIRMED'
            elif not self.intent[job]:
                self.state[job] = 'FAILED'              # authoritative absence; key voided
                self.intent[job] = 'voided'
        return self.state[job]


class LoseAck(DurableProvider):
    def submit(self, request):
        super().submit(request)
        raise ConnectionError('acknowledgement lost')


class NeverReached(DurableProvider):
    def submit(self, request):
        raise ConnectionError('dropped before the provider')


CLASS = {'RESERVED': 'RESERVED', 'SUBMISSION_PENDING': 'UNRESOLVED', 'OUTCOME_UNKNOWN': 'UNRESOLVED',
         'CONFIRMED': 'CONFIRMED', 'FAILED_CONFIRMED': 'FAILED'}


class StateModelTests(unittest.TestCase):
    def run_sequence(self, rng, log, depth=DEPTH):
        with tempfile.TemporaryDirectory() as tmp:
            path, provider_path = Path(tmp) / 'j.sqlite', Path(tmp) / 'p.json'
            journal = Journal(path, 'campaign', CAP)
            DurableProvider(provider_path, initial_balance=10)
            requests = {}
            for job in JOBS:
                terms, inputs, evidence = fixtures.prepare(job, expires_at=NOW + 100, amount=AMOUNT,
                                                           capability='durable_test_simulation')
                journal.register(terms, contract.digest(terms), 'local-owner', NOW)
                journal.audit(job, inputs, evidence, 'local-auditor', NOW)
                requests[job] = journal.request(job, 'r-' + job)
            model, now = Model(), NOW
            for _ in range(depth):
                action = rng.choice(ALPHABET)
                job = rng.choice(JOBS)
                log.append((action, job))
                kind, _, mode = action.partition(':')
                if kind == 'expire':
                    model.expired, now = True, NOW + 100
                elif kind == 'restart':
                    journal = Journal(path, 'campaign', CAP)
                elif kind == 'dispatch':
                    adapter = {'ok': DurableProvider, 'lose_ack': LoseAck, 'never_reached': NeverReached}[mode](provider_path)
                    expected = model.dispatch(job, mode)
                    try:
                        journal.dispatch(requests[job], 'agent-fixture', adapter, now)
                        actual = 'no-refusal'
                    except merkle.Invalid as exc:
                        actual = {'campaign budget exhausted': 'BUDGET_EXHAUSTED', 'authorization expired': 'EXPIRED'}.get(str(exc), str(exc))
                    if expected in ('BUDGET_EXHAUSTED', 'EXPIRED'):
                        self.assertEqual(actual, expected, log)
                    else:
                        self.assertEqual(actual, 'no-refusal', log)
                else:
                    model.reconcile(job, mode)
                    provider = DurableProvider(provider_path, durable=(mode == 'durable'))
                    if model.state[job] == 'NONE':
                        with self.assertRaises(merkle.Invalid):
                            journal.reconcile('r-' + job, 'agent-fixture', provider, now)
                    else:
                        journal.reconcile('r-' + job, 'agent-fixture', provider, now)
                # Observable agreement after every step.
                for j in JOBS:
                    if model.state[j] == 'NONE':
                        with self.assertRaises(merkle.Invalid):
                            journal.status('r-' + j, 'agent-fixture')
                    else:
                        self.assertEqual(CLASS[journal.status('r-' + j, 'agent-fixture')['state']], model.state[j], log)
                self.assertEqual(journal.exposure(), model.exposure, log)
                self.assertLessEqual(journal.exposure(), CAP, log)
                snap = DurableProvider(provider_path).snapshot()
                self.assertEqual((snap['dispatches'], 10 - snap['balance']), (model.debits, model.debits * AMOUNT), log)

    def test_seeded_sequences_agree_with_the_reference_model(self):
        count = int(os.environ.get('METACOIN_MODEL_SEQUENCES', '40'))
        rng = random.Random(2026_09_23)
        for index in range(count):
            log = []
            with self.subTest(sequence=index):
                self.run_sequence(rng, log)

    def test_recorded_counterexample_shapes(self):
        """Sequences that exercise every alphabet symbol at least once; kept as
        fixed regressions so a future change re-checks them deterministically."""
        fixed = [['dispatch:never_reached', 'a', 'reconcile:durable', 'a', 'dispatch:ok', 'a', 'restart', 'a'],
                 ['dispatch:lose_ack', 'a', 'reconcile:amnesiac', 'a', 'restart', 'b', 'reconcile:durable', 'a'],
                 ['dispatch:ok', 'a', 'dispatch:ok', 'b', 'expire', 'a', 'dispatch:ok', 'b'],
                 ['expire', 'a', 'dispatch:ok', 'a', 'reconcile:durable', 'a', 'dispatch:lose_ack', 'b']]
        for steps in fixed:
            pairs = list(zip(steps[::2], steps[1::2]))
            rng = random.Random(0)
            rng.choice = lambda seq, _it=iter([x for pair in pairs for x in pair]): next(_it)
            with self.subTest(steps=steps):
                self.run_sequence(rng, [], depth=len(pairs))
