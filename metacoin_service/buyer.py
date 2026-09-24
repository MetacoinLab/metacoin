"""Production x402 BUYER adapter: the agent pays a remote protected resource over HTTP.

Direction: this service (payer, key held in a private file) buys `next-compute` from a
remote resource server (recipient) using the official x402 SDK client with real EIP-3009
signing (`x402[evm]`). Every step is real code; what is NOT available in this environment is
the operator configuration and credential (see `status()`), which the adapter reports as
missing configuration, distinct from missing code.

Bindings enforced before signing: exact amount from the journal request, configured network,
configured asset, pinned recipient (when configured), timeout bound, and the payment
identifier `wc_<request digest>` (when the server declares the extension). The signature
covers from/to/value/validAfter/validBefore/nonce and the asset contract; identifier and
metadata are unsigned and enforced by the resource server.

Durable submission: the signed payload and its requirements are persisted in the buyer's own
private SQLite file BEFORE the paid request is sent, so a later process can reconcile by
re-presenting the identical authorization. An EIP-3009 nonce can settle at most once, so
re-presentation cannot create a second transfer; the server either returns the recorded
settlement (idempotent by payment identifier) or refuses, in which case the outcome stays
unknown. Nothing here funds, broadcasts, or sends anything unless configured.
"""
import hashlib
import json
import os
import sqlite3
import stat
import time
from pathlib import Path
from urllib.parse import urlparse
from experiments.private_receipts import receipt as merkle
from integrations.x402.legacy_adapter import request_digest
from .db import now
from .errors import ServiceError

CAPABILITY = 'x402_http_buyer'
RECIPIENT_TOKEN = 'remote-x402-resource'
REQUIRED = ('buyer_resource_url', 'buyer_key_file', 'buyer_network', 'buyer_asset', 'buyer_max_amount')


def status(settings):
    """Distinguish code from configuration and credential availability."""
    try:
        import x402.mechanisms.evm  # noqa: F401
        sdk_evm = True
    except ImportError:
        sdk_evm = False
    missing = [k for k in REQUIRED if not getattr(settings, k, None)]
    key_ok = False
    if settings.buyer_key_file:
        try:
            info = os.stat(settings.buyer_key_file)
            key_ok = stat.S_ISREG(info.st_mode) and not info.st_mode & 0o077
        except OSError:
            key_ok = False
    return {'capability': CAPABILITY, 'code_implemented': True, 'sdk_evm_extra_installed': sdk_evm,
            'configuration_missing': missing, 'credential_file_usable': key_ok,
            'available': sdk_evm and not missing and key_ok, 'externally_validated': False,
            'note': 'real SDK client + EIP-3009 signing + durable submission + re-presentation reconciliation; '
                    'no funded key, resource or network is configured in this environment'}


class HttpBuyerAdapter:
    capability = CAPABILITY
    CAPABILITIES = {'capability': CAPABILITY, 'transport': 'HTTP over TCP via httpx (bounded timeouts, no cross-host redirects)',
                    'settlement': 'remote facilitator through the resource server; observed only via PAYMENT-RESPONSE',
                    'signature_coverage': 'EIP-3009 TransferWithAuthorization (from,to,value,validAfter,validBefore,nonce; asset as domain)',
                    'idempotency': 'payment-identifier wc_<request digest> + one nonce per submission, persisted before send',
                    'reconciliation': 're-presentation of the identical signed payload; authoritative only when the server returns the recorded settlement',
                    'durable_outcomes': True, 'http_402': True, 'facilitator': 'remote (not contacted directly)', 'real_funds': 'only if configured with a funded key'}

    def __init__(self, settings, clock=None):
        st = status(settings)
        if not st['available']:
            raise ServiceError('CAPABILITY_UNAVAILABLE', 'production buyer: ' + ('missing configuration ' + ','.join(st['configuration_missing']) if st['configuration_missing']
                                                                                 else 'credential file unusable' if not st['credential_file_usable'] else 'x402[evm] not installed'))
        self.settings = settings
        self.url = settings.buyer_resource_url
        parsed = urlparse(self.url)
        if parsed.scheme not in ('https', 'http') or not parsed.netloc:
            raise ServiceError('CAPABILITY_UNAVAILABLE', 'buyer resource URL invalid')
        if parsed.scheme == 'http' and parsed.hostname not in ('127.0.0.1', 'localhost', '::1'):
            raise ServiceError('CAPABILITY_UNAVAILABLE', 'plain HTTP resource URLs are limited to loopback')
        self.network, self.asset = settings.buyer_network, settings.buyer_asset
        self.max_amount = int(settings.buyer_max_amount)
        self.pay_to = settings.buyer_pay_to or None
        self.timeout = settings.limits['facilitator_timeout_seconds']
        self.clock = clock or now
        self.db_path = Path(settings.home) / 'buyer.sqlite'
        self._init_db()
        self._client = None

    # -- private persistence ----------------------------------------------------
    def _init_db(self):
        fd = os.open(self.db_path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        os.close(fd)
        with self._db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS submissions (request_id TEXT PRIMARY KEY, request_digest TEXT NOT NULL, '
                       'resource_url TEXT NOT NULL, identifier TEXT NOT NULL, requirements_json TEXT NOT NULL, payload_json TEXT NOT NULL, '
                       'state TEXT NOT NULL, response_json TEXT, reference TEXT, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL)')

    def _db(self):
        db = sqlite3.connect(self.db_path, timeout=15, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA synchronous=FULL')
        return db

    # -- SDK pieces ---------------------------------------------------------------
    def _sdk(self):
        if self._client is None:
            from eth_account import Account
            from x402.client import x402ClientSync
            from x402.http import x402HTTPClientSync
            from x402.mechanisms.evm.exact.client import ExactEvmScheme
            from x402.mechanisms.evm.signers import EthAccountSigner
            key = Path(self.settings.buyer_key_file).read_text().strip()
            account = Account.from_key(key)            # never logged; the key file is 0600
            core = x402ClientSync().register(self.network, ExactEvmScheme(EthAccountSigner(account)))
            core.set_spend_controls({'allowed_assets': [self.asset]} if False else False)   # bindings are checked explicitly below
            self._client = (core, x402HTTPClientSync(core), account.address)
        return self._client

    # -- adapter interface ---------------------------------------------------------
    def validate(self, request):
        expected = {'capability': self.capability, 'recipient': RECIPIENT_TOKEN, 'resource': 'next-compute'}
        if any(request.get(k) != v for k, v in expected.items()):
            raise ServiceError('ADAPTER_CAPABILITY', 'contract action does not name the remote x402 resource')
        if request.get('network') != self.network.replace(':', '-') or request.get('asset') != self.settings.buyer_asset_token:
            raise ServiceError('ADAPTER_CAPABILITY', 'contract network/asset tokens do not match the buyer configuration')
        if int(request['amount']) > self.max_amount:
            raise ServiceError('ADAPTER_CAPABILITY', 'amount exceeds the configured buyer ceiling')

    def expected_units(self, request):
        return request['amount']

    def _outcome(self, request, state, reference, units):
        return {'state': state, 'request_digest': request_digest(request), 'reference': reference,
                'capability': self.capability, 'compute_units': units}

    def _get(self, headers=None):
        import httpx
        # No redirects: a redirect could move the payment to another host.
        return httpx.get(self.url, headers=headers or {}, timeout=self.timeout, follow_redirects=False)

    def _bind(self, required, request):
        """Choose and check the requirement BEFORE anything is signed."""
        accepts = [a for a in required.accepts if a.scheme == 'exact' and a.network == self.network and a.asset == self.asset]
        if not accepts:
            raise ServiceError('ADAPTER_CAPABILITY', 'resource offers no requirement on the configured network/asset')
        acc = accepts[0]
        if acc.amount != str(request['amount']):
            raise ServiceError('BINDING_MISMATCH', 'resource price differs from the journal-authorized amount')
        if self.pay_to and acc.pay_to.lower() != self.pay_to.lower():
            raise ServiceError('BINDING_MISMATCH', 'resource recipient differs from the pinned recipient')
        if acc.max_timeout_seconds > 3600:
            raise ServiceError('BINDING_MISMATCH', 'offer validity window too long')
        return acc

    def submit(self, request):
        self.validate(request)
        digest = request_digest(request)
        identifier = 'wc_' + digest
        with self._db() as db:
            row = db.execute('SELECT * FROM submissions WHERE request_id=?', (request['request_id'],)).fetchone()
        if row is not None:
            if row['request_digest'] != digest:
                raise ServiceError('REQUEST_ID_REBOUND', 'buyer submission bound to different terms')
            return self._resume(request, row)          # never sign twice for one request
        core, http, address = self._sdk()
        first = self._get()
        if first.status_code != 402:
            return self._outcome(request, 'FAILED_CONFIRMED', 'resource-did-not-require-payment-' + str(first.status_code), 0)
        required = http.get_payment_required_response(lambda n: first.headers.get(n), first.content)
        acc = self._bind(required, request)
        extensions = dict(required.extensions or {})
        from integrations.x402 import loopback_harness as lb
        ns = lb.load()                 # provides the payment-identifier extension even when x402.extensions' bazaar extra is absent
        if ns is None:
            raise ServiceError('CAPABILITY_UNAVAILABLE', 'x402 SDK unavailable')
        ns.pi.append_payment_identifier_to_extensions(extensions, identifier)
        if ns.pi.PAYMENT_IDENTIFIER in extensions and ns.pi.extract_payment_identifier.__module__ and \
                (extensions[ns.pi.PAYMENT_IDENTIFIER].get('info') or {}).get('id') != identifier:
            raise ServiceError('ADAPTER_CAPABILITY', 'resource declares the payment-identifier extension but the identifier could not be attached')
        payload = core.create_payment_payload(required.model_copy(update={'accepts': [acc]}), extensions=extensions)
        if payload.accepted.model_dump() != acc.model_dump():
            raise ServiceError('BINDING_MISMATCH', 'client selected a different requirement')
        payload_json = payload.model_dump_json(by_alias=True, exclude_none=True)
        with self._db() as db:                          # durable BEFORE the paid request
            db.execute('INSERT INTO submissions VALUES (?,?,?,?,?,?,?,NULL,NULL,?,?)',
                       (request['request_id'], digest, self.url, identifier, acc.model_dump_json(by_alias=True), payload_json,
                        'SUBMITTED', self.clock(), self.clock()))
        return self._send(request, payload_json, acc)

    def _send(self, request, payload_json, acc):
        from x402.schemas import PaymentPayload
        core, http, _ = self._sdk()
        headers = http.encode_payment_signature_header(PaymentPayload.model_validate_json(payload_json))
        try:
            second = self._get(headers)
        except Exception:
            self._set(request['request_id'], 'OUTCOME_UNKNOWN', None, None)
            raise ServiceError('PROVIDER_UNAVAILABLE', 'no response after sending the payment; exposure retained')
        return self._classify(request, second, acc)

    def _classify(self, request, response, acc):
        from x402.http import PAYMENT_REQUIRED_HEADER, PAYMENT_RESPONSE_HEADER, safe_base64_decode
        core, http, _ = self._sdk()
        if response.status_code == 200 and response.headers.get(PAYMENT_RESPONSE_HEADER):
            settle = http.get_payment_settle_response(lambda n: response.headers.get(n))
            data = settle.model_dump(by_alias=True, exclude_none=True)
            consistent = (settle.success and settle.network == self.network and (data.get('amount') in (None, str(request['amount'])))
                          and isinstance(settle.transaction, str) and len(settle.transaction) == 66)
            if not consistent:
                self._set(request['request_id'], 'OUTCOME_UNKNOWN', data, None)
                raise ServiceError('ADAPTER_RESPONSE_INVALID', 'settlement answer inconsistent with the bound request')
            self._set(request['request_id'], 'CONFIRMED', data, settle.transaction)
            return self._outcome(request, 'CONFIRMED', settle.transaction, request['amount'])
        if response.status_code == 402:
            hdr = response.headers.get(PAYMENT_REQUIRED_HEADER)
            error = json.loads(safe_base64_decode(hdr)).get('error') if hdr else None
            if error == 'settlement_pending':
                self._set(request['request_id'], 'OUTCOME_UNKNOWN', {'error': error}, None)
                raise ServiceError('PROVIDER_UNAVAILABLE', 'settlement pending at the resource; reconcile later')
            # refused before or at settlement: nothing is known to have moved
            self._set(request['request_id'], 'FAILED_CONFIRMED', {'error': error}, None)
            return self._outcome(request, 'FAILED_CONFIRMED', 'resource-refused-' + str(error)[:60], 0)
        self._set(request['request_id'], 'OUTCOME_UNKNOWN', {'status': response.status_code}, None)
        raise ServiceError('PROVIDER_UNAVAILABLE', 'unexpected resource status; exposure retained')

    def _set(self, request_id, state, response, reference):
        with self._db() as db:
            db.execute('UPDATE submissions SET state=?, response_json=?, reference=?, updated_at=? WHERE request_id=?',
                       (state, json.dumps(response) if response is not None else None, reference, self.clock(), request_id))

    def _resume(self, request, row):
        if row['state'] == 'CONFIRMED':
            return self._outcome(request, 'CONFIRMED', row['reference'], request['amount'])
        if row['state'] == 'FAILED_CONFIRMED':
            return self._outcome(request, 'FAILED_CONFIRMED', 'resource-refused', 0)
        raise ServiceError('PROVIDER_UNAVAILABLE', 'earlier submission unresolved; reconcile')

    def reconcile(self, request):
        """Re-present the identical signed payload (same nonce, same identifier). Returns the
        outcome only when the resource answers authoritatively; otherwise None."""
        self.validate(request)
        with self._db() as db:
            row = db.execute('SELECT * FROM submissions WHERE request_id=?', (request['request_id'],)).fetchone()
        if row is None:
            return None
        if row['request_digest'] != request_digest(request):
            raise ServiceError('REQUEST_ID_REBOUND', 'buyer submission bound to different terms')
        if row['state'] == 'CONFIRMED':
            return self._outcome(request, 'CONFIRMED', row['reference'], request['amount'])
        if row['state'] == 'FAILED_CONFIRMED':
            return self._outcome(request, 'FAILED_CONFIRMED', 'resource-refused', 0)
        from x402.schemas import PaymentRequirements
        try:
            return self._send(request, row['payload_json'], PaymentRequirements.model_validate_json(row['requirements_json']))
        except ServiceError:
            return None
