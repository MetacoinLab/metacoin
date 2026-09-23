"""Journal adapter over the x402 SDK loopback harness (capability x402_loopback_test).

Connects the contract decision to the real SDK request/response path. The
facilitator is a TEST DOUBLE; nothing settles anywhere. Answers are bound to
the journal request digest; a settlement answer whose amount/network/pay-to
differ from the bound request is refused (the journal then keeps the outcome
unknown). Reconciliation looks up the double's record by payment identifier.
"""
from experiments.private_receipts import receipt as merkle
from integrations.x402 import loopback_harness as lb
from integrations.x402.legacy_adapter import request_digest


class LoopbackAdapter:
    capability = 'x402_loopback_test'
    CAPABILITIES = {'capability': capability,
                    'transport': 'in-process x402 SDK ' + lb.SDK_VERSION + ' HTTP server/client objects (no socket)',
                    'settlement': 'facilitator test double; no chain, no funds',
                    'asset': 'USDC contract address used as an identifier only', 'network': 'eip155:84532 identifier only',
                    'signature_coverage': 'none in this harness (evm extra absent; placeholder signature); in the real '
                                          'scheme the identifier, extra metadata and resource are unsigned',
                    'idempotency': 'payment-identifier extension bound to the request digest; double records by identifier',
                    'reconciliation': 'lookup of the double record by identifier (same double instance)',
                    'durable_outcomes': False, 'http_402': 'header-level compatibility only', 'facilitator': 'double',
                    'real_funds': False, 'sdk': {'package': 'x402', 'version': lb.SDK_VERSION, 'wheel_sha256': lb.SDK_WHEEL_SHA256,
                                                 'source': 'PyPI, installed in an isolated venv; not a core dependency'}}

    def __init__(self, facilitator, now=None, client_scheme=None):
        self.sdk = lb.load()
        if self.sdk is None:
            raise merkle.Invalid('x402 SDK unavailable')
        self.facilitator, self.now, self.client_scheme = facilitator, now, client_scheme

    def validate(self, request):
        expected = {'capability': self.capability, 'recipient': 'loopback-compute-provider', 'resource': 'next-compute',
                    'asset': 'usdc-test-identifier', 'network': 'eip155-84532'}
        if any(request.get(key) != value for key, value in expected.items()):
            raise merkle.Invalid('unsupported adapter capability or destination')

    def expected_units(self, request):
        return request['amount']          # settled atomic amount == bound amount

    def _outcome(self, request, digest, state, reference, units):
        return {'state': state, 'request_digest': digest, 'reference': reference,
                'capability': self.capability, 'compute_units': units}

    def _bound(self, request, response):
        """Semantic consistency of a settlement answer with the exact stored request."""
        return (response is not None and response.get('success') is True
                and response.get('amount') == str(request['amount'])
                and response.get('network') == lb.NETWORKS[request['network']]
                and isinstance(response.get('transaction'), str) and len(response['transaction']) == 66)

    def submit(self, request):
        self.validate(request)
        digest = request_digest(request)
        server = lb.LoopbackServer(self.sdk, self.facilitator, request, digest, self.now)
        result = lb.exchange(self.sdk, server, client=lb.build_client(self.sdk, self.client_scheme))
        if result['stage'] != 'settlement-attempted':
            # Refused before any settlement attempt: nothing could have moved.
            return self._outcome(request, digest, 'FAILED_CONFIRMED', 'loopback-refused-' + str(result.get('error')), 0)
        if result['success']:
            if not self._bound(request, result['settle_response']):
                raise merkle.Invalid('inconsistent adapter response')      # journal keeps OUTCOME_UNKNOWN
            return self._outcome(request, digest, 'CONFIRMED', result['transaction'], request['amount'])
        if result['error_reason'] == self.sdk.pending.ERR_SETTLEMENT_PENDING:
            raise merkle.Invalid('reconciliation unavailable for this adapter session')  # pending: unknown
        return self._outcome(request, digest, 'FAILED_CONFIRMED', 'loopback-' + str(result['error_reason']), 0)

    def reconcile(self, request):
        self.validate(request)
        digest = request_digest(request)
        record = self.facilitator.lookup(lb.payment_identifier(digest))
        if record is None:
            return None
        bound, response = record
        if bound['extra'].get('request_digest') != digest:
            raise merkle.Invalid('adapter reconciliation conflict')
        if not self._bound(request, response):
            return None
        return self._outcome(request, digest, 'CONFIRMED', response['transaction'], request['amount'])
