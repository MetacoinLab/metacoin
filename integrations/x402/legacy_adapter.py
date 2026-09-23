"""Thin adapter over the existing zero-value stub; no HTTP or real settlement."""
import hashlib
import threading
from demo.x402_spend_stub import buy_compute
from experiments.private_receipts import receipt as merkle


def request_digest(request):
    return hashlib.sha256(b'metacoin/compute-action/v0\0' + merkle.canonical(request)).hexdigest()


class LegacyAdapter:
    capability = 'legacy_simulation'

    def __init__(self, faucet):
        self._faucet = faucet
        self._outcomes = {}
        self._lock = threading.Lock()

    def validate(self, request):
        expected = {'capability': self.capability, 'recipient': 'legacy-compute-provider',
                    'resource': 'next-compute', 'asset': 'Test-META', 'network': 'local-simulation'}
        if any(request.get(key) != value for key, value in expected.items()):
            raise merkle.Invalid('unsupported adapter capability or destination')

    def submit(self, request):
        self.validate(request)
        binding = request_digest(request)
        with self._lock:
            old = self._outcomes.get(request['request_id'])
            if old is not None:
                if old['request_digest'] != binding:
                    raise merkle.Invalid('adapter idempotency conflict')
                return dict(old)
            receipt = buy_compute(self._faucet, request['actor'], request['amount'])
            outcome = {'state': 'CONFIRMED' if receipt['purchased'] else 'FAILED_CONFIRMED',
                       'request_digest': binding, 'reference': 'sim-' + request['request_id'],
                       'capability': self.capability, 'compute_units': receipt.get('compute_units', 0)}
            self._outcomes[request['request_id']] = outcome
            return dict(outcome)

    def reconcile(self, request):
        self.validate(request)
        with self._lock:
            old = self._outcomes.get(request['request_id'])
            if old is not None and old['request_digest'] != request_digest(request):
                raise merkle.Invalid('adapter reconciliation conflict')
            return None if old is None else dict(old)
