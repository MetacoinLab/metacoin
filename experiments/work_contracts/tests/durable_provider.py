"""TESTING FACILITY — not a payment system and not part of the application journal.

A provider double that keeps request-bound outcomes in its OWN file so that a
fresh process can reconcile an earlier action. It models a rail whose
idempotency keys are durable and whose "not found" answer is authoritative
(the key is voided on lookup, so a late submission with that key is refused).
With durable=False it models the opposite: a provider that keeps no record, so
absence means nothing and the journal must keep exposure.

State file: {"balance": int, "intents": {rid: digest}, "outcomes": {rid: outcome},
             "voided": [rid], "dispatches": int}
Writes are atomic (temp + fsync + rename) under an exclusive flock so several
processes can share one provider file in tests.
"""
import fcntl
import json
import os
import time
from experiments.private_receipts import receipt as merkle
from integrations.x402.legacy_adapter import request_digest

UNITS_PER_AMOUNT = 1
CAPABILITY = 'durable_test_simulation'


class DurableProvider:
    capability = CAPABILITY
    CAPABILITIES = {'capability': CAPABILITY, 'transport': 'in-process-function-call',
                    'settlement': 'zero-value-simulation', 'asset': 'Test-META', 'network': 'local-simulation',
                    'signature_coverage': 'none', 'idempotency': 'durable-file-by-request-id-and-digest',
                    'reconciliation': 'authoritative-lookup-with-void-on-absence', 'durable_outcomes': True,
                    'http_402': False, 'facilitator': False, 'real_funds': False,
                    'role': 'testing facility only'}

    def __init__(self, path, initial_balance=None, durable=True, hook=None):
        self.path = os.fspath(path)
        self.durable = durable
        self.hook = hook or (lambda point: None)
        if initial_balance is not None:
            self._write({'balance': initial_balance, 'intents': {}, 'outcomes': {}, 'voided': [], 'dispatches': 0})

    # -- file primitives -------------------------------------------------
    def _write(self, state):
        tmp = self.path + '.tmp'
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(merkle.canonical(state))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, self.path)

    def _read(self):
        with open(self.path, 'rb') as stream:
            return merkle.parse(stream.read())

    class _locked:
        def __init__(self, provider):
            self.provider = provider

        def __enter__(self):
            self.fd = os.open(self.provider.path + '.lock', os.O_RDWR | os.O_CREAT, 0o600)
            fcntl.flock(self.fd, fcntl.LOCK_EX)
            return self.provider._read()

        def __exit__(self, *exc):
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)

    # -- adapter interface -------------------------------------------------
    def validate(self, request):
        expected = {'capability': self.capability, 'recipient': 'legacy-compute-provider',
                    'resource': 'next-compute', 'asset': 'Test-META', 'network': 'local-simulation'}
        if any(request.get(key) != value for key, value in expected.items()):
            raise merkle.Invalid('unsupported adapter capability or destination')

    def expected_units(self, request):
        return request['amount'] * UNITS_PER_AMOUNT

    def _outcome(self, request, state, reference, units):
        return {'state': state, 'request_digest': request_digest(request), 'reference': reference,
                'capability': self.capability, 'compute_units': units}

    def submit(self, request):
        self.validate(request)
        rid, digest = request['request_id'], request_digest(request)
        # Phase 1 (locked): idempotency lookup and durable intent, before any effect.
        with self._locked(self) as state:
            if self.durable:
                old = state['outcomes'].get(rid)
                if old is not None:
                    if old['request_digest'] != digest:
                        raise merkle.Invalid('adapter idempotency conflict')
                    return dict(old)
                if rid in state['voided']:
                    return self._outcome(request, 'FAILED_CONFIRMED', 'voided-before-submission', 0)
                state['intents'][rid] = digest
                self._write(state)
        self.hook('after_intent')
        slow = int(os.environ.get('METACOIN_TEST_SLOW_SUBMIT_MS', '0'))
        if slow:
            time.sleep(slow / 1000)         # processing window: a concurrent lookup sees "accepted, pending"
        # Phase 2 (locked): the economic effect and, if durable, its bound outcome.
        with self._locked(self) as state:
            if self.durable and rid in state['voided']:
                return self._outcome(request, 'FAILED_CONFIRMED', 'voided-before-submission', 0)
            state['dispatches'] += 1
            if state['balance'] >= request['amount']:
                state['balance'] -= request['amount']
                outcome = self._outcome(request, 'CONFIRMED', 'durable-' + rid, self.expected_units(request))
            else:
                outcome = self._outcome(request, 'FAILED_CONFIRMED', 'durable-insufficient-' + rid, 0)
            if self.durable:
                state['outcomes'][rid] = outcome
            self._write(state)
        self.hook('after_effect_before_response')
        return dict(outcome)

    def reconcile(self, request):
        self.validate(request)
        if not self.durable:
            return None                     # a provider without records cannot help
        rid, digest = request['request_id'], request_digest(request)
        with self._locked(self) as state:
            old = state['outcomes'].get(rid)
            if old is not None:
                if old['request_digest'] != digest:
                    raise merkle.Invalid('adapter reconciliation conflict')
                return dict(old)
            if rid in state['intents']:
                return None                 # accepted, effect status unknown: keep exposure
            if rid not in state['voided']:
                state['voided'].append(rid)  # authoritative absence: refuse any later submit
                self._write(state)
            return self._outcome(request, 'FAILED_CONFIRMED', 'provider-no-record', 0)

    def snapshot(self):
        return self._read()
