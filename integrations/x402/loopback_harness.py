"""OFFLINE loopback harness against the pinned official x402 Python SDK.

What it exercises, entirely in-process (no socket, no chain, no facilitator
service): the SDK's resource-server request processing (402 PAYMENT-REQUIRED
header, requirement matching, extension echo validation, verify/settle
lifecycle hooks), the SDK client (PAYMENT-SIGNATURE header construction with
the payment-identifier extension), and a facilitator TEST DOUBLE that keeps a
request-bound settlement record.

What it proves: request/response compatibility with x402 SDK 2.24.0 core, and
that our journal binding can be enforced at the resource server's
before-verify hook. What it does NOT prove: on-chain settlement, signature
validity (the `evm` extra is absent; payloads carry a constant placeholder
instead of an EIP-3009 signature), or any network behaviour.

Signature coverage in the real protocol (from the SDK source, mechanisms/evm/
exact): the EIP-712 message covers from/to/value/validAfter/validBefore/nonce
and the domain binds the asset contract. The payment identifier, `extra`
metadata (our contract digest) and `resource` are NOT signed. Their integrity
rests on the resource server: requirement matching (`extra` subset rule),
extension echo validation, and the application hook below.

The SDK is an optional dependency for this integration only; the protocol core
stays standard-library. Import fails closed (`available()` is False).
"""
import hashlib
import importlib
import json
import sys
import time
import types

SDK_VERSION = '2.24.0'
SDK_WHEEL_SHA256 = '515171258b32af36c05b2a6aa8d2e86ff6cf70ecc731151562fdd90859987fe1'
NETWORKS = {'eip155-84532': 'eip155:84532'}                      # contract token -> CAIP-2
ASSETS = {'usdc-test-identifier': '0x036CbD53842c5426634e7929541eC2318f3dCF7e'}  # identifier only
RECIPIENTS = {'loopback-compute-provider': '0x' + '11' * 20}    # synthetic pay-to address
PLACEHOLDER_SIGNATURE = 'loopback-unsigned-placeholder'
ERR_BINDING = 'work_contract_binding_mismatch'
ERR_IDENTIFIER = 'work_contract_payment_identifier_mismatch'

_sdk = None


def load():
    """Import the SDK lazily; returns a namespace or None when unavailable."""
    global _sdk
    if _sdk is not None:
        return _sdk or None
    try:
        x402 = importlib.import_module('x402')
        if getattr(x402, '__version__', SDK_VERSION) != SDK_VERSION:
            raise ImportError('unsupported x402 SDK version')
        # x402.extensions eagerly imports the bazaar extension, which needs
        # `idna`; the payment-identifier subpackage itself is dependency-free.
        if 'x402.extensions' not in sys.modules:
            try:
                importlib.import_module('x402.extensions')
            except ImportError:
                stub = types.ModuleType('x402.extensions')
                stub.__path__ = [x402.__path__[0] + '/extensions']
                sys.modules['x402.extensions'] = stub
        ns = types.SimpleNamespace()
        ns.schemas = importlib.import_module('x402.schemas')
        ns.server = importlib.import_module('x402.server')
        ns.client = importlib.import_module('x402.client')
        ns.http = importlib.import_module('x402.http')
        ns.pi = importlib.import_module('x402.extensions.payment_identifier')
        ns.pending = importlib.import_module('x402.pending_settlement_store')
    except ImportError:
        _sdk = False
        return None
    _sdk = ns
    return ns


def available():
    return load() is not None


def payment_identifier(request_digest):
    """Deterministic identifier bound to the complete economic request (16..128 chars, [A-Za-z0-9_-])."""
    return 'wc_' + request_digest


class LocalServerScheme:
    """Duck-typed SchemeNetworkServer for 'exact' with no EVM dependency."""
    scheme = 'exact'
    default_asset_transfer_method = 'eip3009'
    payment_flows = {'eip3009': {'supported': ('authorization',), 'default': 'authorization'}}

    def parse_price(self, price, network):
        return price

    def enhance_payment_requirements(self, requirements, supported_kind, extensions):
        return requirements


class LocalClientScheme:
    scheme = 'exact'

    def __init__(self, payer='0x' + '22' * 20, signature=PLACEHOLDER_SIGNATURE):
        self.payer, self.signature = payer, signature

    def create_payment_payload(self, requirements):
        # No EIP-3009 signing here (evm extra absent). Shape mirrors the real
        # inner payload so parsing paths are exercised; the signature is a placeholder.
        return {'signature': self.signature,
                'authorization': {'from': self.payer, 'to': requirements.pay_to, 'value': requirements.amount,
                                  'validAfter': '0', 'validBefore': str(int(time.time()) + requirements.max_timeout_seconds),
                                  'nonce': '0x' + hashlib.sha256(requirements.amount.encode()).hexdigest()}}


class FacilitatorDouble:
    """TEST DOUBLE: verify accepts the placeholder signature only; settle debits a
    synthetic balance once per payment identifier and keeps the record so a
    later lookup (reconciliation) returns the same answer. Modes inject faults."""

    def __init__(self, sdk, network, balance=10, mode='ok'):
        self.sdk, self.network, self.balance, self.mode = sdk, network, balance, mode
        self.records = {}            # payment identifier -> (bound requirement fields, SettleResponse dict)
        self.settle_calls = self.verify_calls = 0
        self.pending_left = 1 if mode == 'pending_once' else (10**9 if mode == 'always_pending' else 0)

    def get_supported(self):
        s = self.sdk.schemas
        return s.SupportedResponse(kinds=[s.SupportedKind(x402_version=2, scheme='exact', network=self.network)])

    def verify(self, payload, requirements):
        self.verify_calls += 1
        ok = payload.payload.get('signature') == PLACEHOLDER_SIGNATURE
        return self.sdk.schemas.VerifyResponse(is_valid=ok, invalid_reason=None if ok else 'invalid_signature',
                                               payer=payload.payload.get('authorization', {}).get('from'))

    def settle(self, payload, requirements):
        s = self.sdk.schemas
        self.settle_calls += 1
        identifier = self.sdk.pi.extract_payment_identifier(payload, validate=False)
        if self.pending_left > 0:
            self.pending_left -= 1
            return s.SettleResponse(success=False, error_reason=self.sdk.pending.ERR_SETTLEMENT_PENDING,
                                    transaction='0x' + 'ee' * 32, network=requirements.network)
        if identifier in self.records:                      # idempotent by identifier
            return s.SettleResponse(**self.records[identifier][1])
        amount = int(requirements.amount)
        if self.balance < amount:
            return s.SettleResponse(success=False, error_reason='insufficient_funds', transaction='', network=requirements.network)
        self.balance -= amount
        transaction = '0x' + hashlib.sha256(('tx' + str(identifier)).encode()).hexdigest()
        response = {'success': True, 'transaction': transaction, 'network': requirements.network,
                    'payer': payload.payload.get('authorization', {}).get('from'), 'amount': requirements.amount}
        if self.mode == 'mismatched_response':
            response.update(amount=str(amount + 1), network='eip155:1')
        self.records[identifier] = ({'amount': requirements.amount, 'pay_to': requirements.pay_to,
                                     'network': requirements.network, 'extra': dict(requirements.extra)}, response)
        return s.SettleResponse(**response)

    def lookup(self, identifier):
        return None if identifier not in self.records else self.records[identifier]


class FakeAdapter:
    """Minimal HTTPAdapter: the SDK reads headers/method/path/url through this."""

    def __init__(self, path, headers=None, method='GET'):
        self.headers = {k.upper(): v for k, v in (headers or {}).items()}
        self.path, self.method, self.url = path, method, 'http://loopback' + path

    def get_header(self, name):
        return self.headers.get(name.upper())

    def get_method(self):
        return self.method

    def get_path(self):
        return self.path

    def get_url(self):
        return self.url

    def get_accept_header(self):
        return 'application/json'

    def get_user_agent(self):
        return 'metacoin-loopback/0'

    def get_query_params(self):
        return None

    def get_query_param(self, name):
        return None

    def get_body(self):
        return None


class LoopbackServer:
    """One protected route per journal-authorized request, built from the bound request."""

    def __init__(self, sdk, facilitator, request, request_digest, now=None):
        self.sdk, self.facilitator, self.request, self.digest = sdk, facilitator, request, request_digest
        self.network = NETWORKS[request['network']]
        self.identifier = payment_identifier(request_digest)
        timeout = request['expires_at'] - (int(time.time()) if now is None else now)
        if timeout <= 0:
            raise ValueError('authorization expired; no offer is built')
        self.path = '/compute/' + request['request_id']
        core = sdk.server.x402ResourceServerSync(facilitator)
        core.register(self.network, LocalServerScheme())
        core.on_before_verify(self._bind)
        h = sdk.http
        # `extra` carries the journal binding. It is NOT signed by the payer; the
        # server enforces it (subset match + this hook), which is the honest scope.
        option = h.PaymentOption(scheme='exact', pay_to=RECIPIENTS[request['recipient']],
                                 price=sdk.schemas.AssetAmount(amount=str(request['amount']), asset=ASSETS[request['asset']],
                                                               extra={'name': 'USDC', 'version': '2'}),
                                 network=self.network, max_timeout_seconds=min(timeout, 300),
                                 extra={'contract_digest': request['contract_digest'], 'job_id': request['job_id'],
                                        'evidence_root': request['evidence_root'], 'request_digest': request_digest})
        routes = {'GET ' + self.path: h.RouteConfig(
            accepts=option, resource='http://loopback' + self.path, description='next-compute for ' + request['job_id'],
            mime_type='application/json',
            extensions={sdk.pi.PAYMENT_IDENTIFIER: sdk.pi.declare_payment_identifier_extension(required=True)})}
        self.http = h.x402HTTPResourceServerSync(core, routes)
        self.http.initialize()

    def _bind(self, ctx):
        """Application-level binding check at the SDK's before-verify hook."""
        accepted = ctx.requirements
        identifier = self.sdk.pi.extract_payment_identifier(ctx.payment_payload, validate=False)
        if identifier != self.identifier:
            return self.sdk.schemas.AbortResult(reason=ERR_IDENTIFIER)
        expected = {'amount': str(self.request['amount']), 'pay_to': RECIPIENTS[self.request['recipient']],
                    'network': self.network, 'asset': ASSETS[self.request['asset']]}
        if any(getattr(accepted, key) != value for key, value in expected.items()) \
                or accepted.extra.get('request_digest') != self.digest \
                or accepted.extra.get('contract_digest') != self.request['contract_digest']:
            return self.sdk.schemas.AbortResult(reason=ERR_BINDING)
        # Observed with SDK 2.24.0: the resource server does not compare the
        # payload's `resource` with the route, so a changed resource reaches
        # settlement unless the application checks it. Bind it here.
        resource = getattr(ctx.payment_payload, 'resource', None)
        if resource is None or getattr(resource, 'url', None) != 'http://loopback' + self.path:
            return self.sdk.schemas.AbortResult(reason=ERR_BINDING)
        return None

    def handle(self, headers=None):
        ctx = self.sdk.http.HTTPRequestContext(adapter=FakeAdapter(self.path, headers), path=self.path, method='GET')
        return ctx, self.http.process_http_request(ctx)

    def settle(self, ctx, result):
        return self.http.process_settlement(result.payment_payload, result.payment_requirements, context=ctx,
                                            declared_extensions=result.declared_extensions)


class Client:
    """SDK core client + HTTP client pair (the HTTP wrapper keeps the core private)."""

    def __init__(self, sdk, scheme=None):
        self.core = sdk.client.x402ClientSync().register('eip155:*', scheme or LocalClientScheme()).set_spend_controls(False)
        self.http = sdk.http.x402HTTPClientSync(self.core)


def build_client(sdk, scheme=None):
    return Client(sdk, scheme)


def error_of(sdk, result):
    header = result.response.headers.get(sdk.http.PAYMENT_REQUIRED_HEADER) if result.response else None
    return json.loads(sdk.http.safe_base64_decode(header)).get('error') if header else None


def exchange(sdk, server, client=None, identifier=None, mutate_payload=None):
    """Full client<->server<->facilitator loopback for one route. Returns a dict
    describing each step; nothing here is a network or chain event."""
    client = client or build_client(sdk)
    ctx, first = server.handle()
    out = {'first': first.type, 'status': first.response.status if first.response else None}
    if first.type != 'payment-error':
        return dict(out, stage='no-402')
    required = client.http.get_payment_required_response(lambda name: first.response.headers.get(name),
                                                         json.dumps(first.response.body).encode())
    extensions = dict(required.extensions or {})
    sdk.pi.append_payment_identifier_to_extensions(extensions, identifier or server.identifier)
    payload = client.core.create_payment_payload(required, extensions=extensions)
    if mutate_payload is not None:
        payload = mutate_payload(payload)
    headers = client.http.encode_payment_signature_header(payload)
    ctx2, second = server.handle(headers)
    out.update(second=second.type, error=error_of(sdk, second) if second.type == 'payment-error' else None,
               identifier=identifier or server.identifier)
    if second.type != 'payment-verified':
        return dict(out, stage='refused-before-settlement')
    settled = server.settle(ctx2, second)
    response = settled.settle_response.model_dump(by_alias=True, exclude_none=True) if settled.settle_response else None
    decoded = None
    if settled.headers.get(sdk.http.PAYMENT_RESPONSE_HEADER):
        decoded = client.http.get_payment_settle_response(lambda name: settled.headers.get(name)).model_dump(by_alias=True, exclude_none=True)
    return dict(out, stage='settlement-attempted', success=settled.success, error_reason=settled.error_reason,
                transaction=settled.transaction, settle_response=response, client_decoded=decoded,
                settle_calls=server.facilitator.settle_calls)
