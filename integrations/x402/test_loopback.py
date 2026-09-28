# EXPERIMENTAL — private test chain only — not the protocol's money layer, which remains [SPEC] and zero-value by MIP-0001/0002; chain-agnostic law unchanged.
"""x402 SDK loopback: compatibility and refusal matrix. Skipped when the SDK is absent.

Run with an interpreter that has the pinned SDK (see integrations/x402/README.md):
    <venv>/bin/python -m unittest integrations.x402.test_loopback -v
"""
from pathlib import Path
import tempfile
import unittest
from experiments.private_receipts import receipt as merkle
from experiments.work_contracts import contract, fixtures
from experiments.work_contracts.execution_state import Journal
from integrations.x402 import loopback_harness as lb
from integrations.x402.legacy_adapter import request_digest

NOW = 1_900_000_000


@unittest.skipUnless(lb.available(), 'x402 SDK ' + lb.SDK_VERSION + ' not installed (optional integration)')
class LoopbackTests(unittest.TestCase):
    def setUp(self):
        self.sdk = lb.load()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.journal = Journal(Path(self.temp.name) / 'j.sqlite', 'campaign', 3)

    def job(self, name='job', amount=1):
        terms, inputs, evidence = fixtures.prepare(name, expires_at=NOW + 100, amount=amount, capability='x402_loopback_test')
        self.journal.register(terms, contract.digest(terms), 'local-owner', NOW)
        self.journal.audit(name, inputs, evidence, 'local-auditor', NOW)
        return self.journal.request(name, 'r-' + name)

    def facilitator(self, **kwargs):
        return lb.FacilitatorDouble(self.sdk, 'eip155:84532', **kwargs)

    def test_sdk_pin_and_capability_declaration(self):
        import x402
        self.assertEqual(x402.__version__, lb.SDK_VERSION)
        from integrations.x402.loopback_adapter import LoopbackAdapter
        caps = LoopbackAdapter.CAPABILITIES
        self.assertFalse(caps['real_funds'])
        self.assertIn('unsigned', caps['signature_coverage'])

    def test_contract_gated_exchange_through_the_journal(self):
        from integrations.x402.loopback_adapter import LoopbackAdapter
        request = self.job()
        fac = self.facilitator()
        adapter = LoopbackAdapter(fac, now=NOW)
        result = self.journal.dispatch(request, 'agent-fixture', adapter, NOW)
        self.assertEqual(result['state'], 'CONFIRMED')
        self.assertTrue(result['result']['reference'].startswith('0x'))
        self.assertEqual((fac.settle_calls, fac.balance), (1, 9))
        # identical retry: no new exchange, no new debit
        self.assertEqual(self.journal.dispatch(request, 'agent-fixture', adapter, NOW), result)
        self.assertEqual((fac.settle_calls, fac.balance), (1, 9))
        # reconciliation from the double's record binds the same digest
        self.assertEqual(self.journal.reconcile('r-job', 'agent-fixture', adapter, NOW)['reconciliation'], 'terminal-already')
        self.assertEqual(adapter.reconcile(request)['reference'], result['result']['reference'])

    def test_transport_level_refusals_from_the_sdk(self):
        request = self.job()
        digest = request_digest(request)
        server = lb.LoopbackServer(self.sdk, self.facilitator(), request, digest, NOW)

        def altered(field, value):
            def mutate(payload):
                return payload.model_copy(update={'accepted': payload.accepted.model_copy(update={field: value})})
            return mutate

        cases = {
            'altered amount': altered('amount', '2'), 'wrong recipient': altered('pay_to', '0x' + '33' * 20),
            'wrong network': altered('network', 'eip155:1'), 'stale offer timeout': altered('max_timeout_seconds', 7),
            'changed contract digest in extra': altered('extra', {'name': 'USDC', 'version': '2', 'contract_digest': '0' * 64,
                                                                   'job_id': 'job', 'evidence_root': request['evidence_root'],
                                                                   'request_digest': digest}),
            'changed resource': lambda p: p.model_copy(update={'resource': self.sdk.schemas.ResourceInfo(url='http://loopback/other')}),
        }
        for name, mutate in cases.items():
            with self.subTest(case=name):
                out = lb.exchange(self.sdk, server, mutate_payload=mutate)
                self.assertEqual(out['stage'], 'refused-before-settlement', out)
                self.assertEqual(out['second'], 'payment-error')
                self.assertIsNotNone(out['error'])
        # invalid signature: refused by the facilitator double's verify
        out = lb.exchange(self.sdk, server, client=lb.build_client(self.sdk, lb.LocalClientScheme(signature='forged')))
        self.assertEqual((out['stage'], out['error']), ('refused-before-settlement', 'invalid_signature'))
        # missing payment identifier: the echo check or the hook refuses
        dropped = lambda p: p.model_copy(update={'extensions': {}})
        out = lb.exchange(self.sdk, server, mutate_payload=dropped)
        self.assertEqual(out['stage'], 'refused-before-settlement')
        self.assertEqual(server.facilitator.settle_calls, 0)

    def test_application_level_binding_at_the_hook(self):
        request = self.job()
        digest = request_digest(request)
        server = lb.LoopbackServer(self.sdk, self.facilitator(), request, digest, NOW)
        out = lb.exchange(self.sdk, server, identifier='wc_' + '9' * 64)   # a valid-looking but foreign identifier
        self.assertEqual((out['stage'], out['error']), ('refused-before-settlement', lb.ERR_IDENTIFIER))
        # the same identifier reused for a second exchange is idempotent at the double: one debit
        first = lb.exchange(self.sdk, server)
        second = lb.exchange(self.sdk, server)
        self.assertTrue(first['success'] and second['success'])
        self.assertEqual(first['transaction'], second['transaction'])
        self.assertEqual(server.facilitator.balance, 9)
        # expired authorization never builds an offer
        with self.assertRaises(ValueError):
            lb.LoopbackServer(self.sdk, self.facilitator(), request, digest, NOW + 100)

    def test_mismatched_settlement_answer_keeps_the_outcome_unknown(self):
        from integrations.x402.loopback_adapter import LoopbackAdapter
        request = self.job()
        fac = self.facilitator(mode='mismatched_response')
        result = self.journal.dispatch(request, 'agent-fixture', LoopbackAdapter(fac, now=NOW), NOW)
        self.assertEqual(result['state'], 'OUTCOME_UNKNOWN')
        self.assertEqual(self.journal.exposure(), 1)
        self.assertEqual(fac.balance, 9)   # the double did debit: exposure must stay

    def test_settlement_pending_retry_and_reconciliation(self):
        from integrations.x402.loopback_adapter import LoopbackAdapter
        request = self.job()
        once = self.facilitator(mode='pending_once')
        result = self.journal.dispatch(request, 'agent-fixture', LoopbackAdapter(once, now=NOW), NOW)
        self.assertEqual((result['state'], once.settle_calls), ('CONFIRMED', 2))   # the SDK retries exactly once
        second = self.job('b')
        always = self.facilitator(mode='always_pending')
        adapter = LoopbackAdapter(always, now=NOW)
        result = self.journal.dispatch(second, 'agent-fixture', adapter, NOW)
        self.assertEqual((result['state'], always.settle_calls), ('OUTCOME_UNKNOWN', 2))
        self.assertEqual(self.journal.reconcile('r-b', 'agent-fixture', adapter, NOW)['state'], 'OUTCOME_UNKNOWN')
        always.pending_left = 0   # the rail later completes; the record becomes available
        self.assertEqual(lb.exchange(self.sdk, lb.LoopbackServer(self.sdk, always, second, request_digest(second), NOW))['success'], True)
        self.assertEqual(self.journal.reconcile('r-b', 'agent-fixture', adapter, NOW)['state'], 'CONFIRMED')
        self.assertEqual(self.journal.exposure(), 2)

    def test_insufficient_funds_is_a_confirmed_failure_that_releases_budget(self):
        from integrations.x402.loopback_adapter import LoopbackAdapter
        request = self.job('big', amount=3)
        fac = self.facilitator(balance=1)
        result = self.journal.dispatch(request, 'agent-fixture', LoopbackAdapter(fac, now=NOW), NOW)
        self.assertEqual((result['state'], result['result']['reference']), ('FAILED_CONFIRMED', 'loopback-insufficient_funds'))
        self.assertEqual(self.journal.exposure(), 0)

    def test_no_private_values_reach_the_wire(self):
        request = self.job()
        server = lb.LoopbackServer(self.sdk, self.facilitator(), request, request_digest(request), NOW)
        ctx, first = server.handle()
        wire = self.sdk.http.safe_base64_decode(first.response.headers[self.sdk.http.PAYMENT_REQUIRED_HEADER])
        for secret in ('SYNTHETIC_PRIVATE_CANARY_73', 'available_low', 'margin', 'segments'):
            self.assertNotIn(secret, wire)
        self.assertIn(request['contract_digest'], wire)


class SkipReportTests(unittest.TestCase):
    def test_reports_availability_honestly(self):
        # Under the stdlib-only interpreter this documents the skip; under the venv it documents presence.
        self.assertIn(lb.available(), (True, False))
