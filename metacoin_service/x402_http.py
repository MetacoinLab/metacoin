"""x402 over real HTTP: the service SELLS access to an accepted job's public bundle.

Direction: a customer pays the service (pay_to) for a result. This is distinct
from the journal's agent-buys-compute action. Transport and requirement
matching come from the official SDK; the application binds the route, job,
contract digest, evidence root, amount, asset, network and payment identifier
at the before-verify hook (unsigned metadata is enforced server-side).

Facilitator: test-http mode uses the SDK's own HTTPFacilitatorClientSync against
a deterministic facilitator DOUBLE served by this app (so the production client
code path runs over a socket); production mode points that client at the
operator-configured https URL with auth headers from a credential file. The
production path is real code but unexercised against any external facilitator.
"""
import hashlib
import json
import secrets
import threading
import time
from experiments.private_receipts import receipt as merkle
from . import history
from .db import now
from .errors import ServiceError

TEST_NETWORK, TEST_ASSET, TEST_PAY_TO = 'eip155:84532', '0x036CbD53842c5426634e7929541eC2318f3dCF7e', '0x' + '11' * 20
PLACEHOLDER_SIGNATURE = 'loopback-unsigned-placeholder'
ERR_BINDING = 'work_contract_binding_mismatch'


def sdk():
    from integrations.x402 import loopback_harness as lb
    ns = lb.load()
    if ns is None:
        raise ServiceError('CAPABILITY_UNAVAILABLE', 'x402 SDK not installed')
    return ns


class StarletteAdapter:
    def __init__(self, request, body):
        self.request, self.body = request, body

    def get_header(self, name): return self.request.headers.get(name)
    def get_method(self): return self.request.method
    def get_path(self): return self.request.url.path
    def get_url(self): return str(self.request.url)
    def get_accept_header(self): return self.request.headers.get('accept', 'application/json')
    def get_user_agent(self): return self.request.headers.get('user-agent', '')
    def get_query_params(self): return dict(self.request.query_params)
    def get_query_param(self, name): return self.request.query_params.get(name)
    def get_body(self): return self.body


class FacilitatorDoubleState:
    """Deterministic facilitator double served over HTTP in test-http mode (test facility)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.settled = {}      # payment identifier -> settle response dict
        self.calls = {'verify': 0, 'settle': 0, 'supported': 0}
        self.pending_left = 0

    def supported(self):
        self.calls['supported'] += 1
        return {'kinds': [{'x402Version': 2, 'scheme': 'exact', 'network': TEST_NETWORK}], 'extensions': [], 'signers': {}}

    def verify(self, body):
        self.calls['verify'] += 1
        inner = body.get('paymentPayload', {}).get('payload', {})
        req = body.get('paymentRequirements', {})
        signature = inner.get('signature')
        if signature == PLACEHOLDER_SIGNATURE:
            ok, reason = True, None
        else:
            ok, reason = verify_eip3009_offline(inner, req)
        return {'isValid': ok, 'invalidReason': None if ok else reason, 'payer': inner.get('authorization', {}).get('from')}

    def settle(self, body):
        self.calls['settle'] += 1
        payload, req = body.get('paymentPayload', {}), body.get('paymentRequirements', {})
        identifier = (payload.get('extensions') or {}).get('payment-identifier', {}).get('info', {}).get('id')
        with self.lock:
            if self.pending_left > 0:
                self.pending_left -= 1
                return {'success': False, 'errorReason': 'settlement_pending', 'transaction': '0x' + 'ee' * 32, 'network': req.get('network')}
            if identifier in self.settled:
                return self.settled[identifier]
            response = {'success': True, 'transaction': '0x' + hashlib.sha256(('tx' + str(identifier)).encode()).hexdigest(),
                        'network': req.get('network'), 'payer': payload.get('payload', {}).get('authorization', {}).get('from'),
                        'amount': req.get('amount')}
            self.settled[identifier] = response
            return response

    def lookup(self, identifier):
        return self.settled.get(identifier)


def verify_eip3009_offline(inner, requirements):
    """Cryptographic check of a real EIP-3009 authorization (EIP-712 recovery) against the
    requirements: recovered signer == from, to == payTo, value == amount, validity window.
    No chain is consulted: balance and nonce state are NOT verified here (test facility)."""
    try:
        from eth_account import Account
        from eth_account.messages import encode_typed_data
        from x402.mechanisms.evm import eip712
        from x402.mechanisms.evm.types import ExactEIP3009Authorization
    except ImportError:
        return False, 'evm_verification_unavailable'
    auth = inner.get('authorization') or {}
    try:
        authorization = ExactEIP3009Authorization(from_address=auth['from'], to=auth['to'], value=str(auth['value']),
                                                  valid_after=str(auth['validAfter']), valid_before=str(auth['validBefore']), nonce=auth['nonce'])
        chain_id = int(str(requirements['network']).split(':')[1])
        extra = requirements.get('extra') or {}
        domain, types, primary, message = eip712.build_typed_data_for_signing(authorization, chain_id, requirements['asset'],
                                                                                extra.get('name', ''), extra.get('version', ''))
        domain_data = {k: v for k, v in {'name': domain.name, 'version': domain.version, 'chainId': domain.chain_id,
                                          'verifyingContract': domain.verifying_contract}.items() if v is not None}
        signable = encode_typed_data(domain_data=domain_data, message_types={primary: types[primary]}, message_data=message)
        recovered = Account.recover_message(signable, signature=inner['signature'])
    except Exception:
        return False, 'invalid_signature'
    if recovered.lower() != auth['from'].lower():
        return False, 'invalid_signature'
    if auth['to'].lower() != str(requirements.get('payTo', '')).lower() or str(auth['value']) != str(requirements.get('amount')):
        return False, 'authorization_does_not_match_requirements'
    if not int(auth['validAfter']) <= int(time.time()) <= int(auth['validBefore']):
        return False, 'authorization_expired'
    return True, None


class SaleService:
    def __init__(self, settings, store, jobs):
        self.settings, self.store, self.jobs = settings, store, jobs
        self.double = FacilitatorDoubleState()
        self._client = None

    def enabled(self):
        return self.settings.provider_mode in ('test-http', 'production')

    def terms(self):
        if self.settings.provider_mode == 'production':
            return self.settings.x402_network, self.settings.x402_asset, self.settings.x402_pay_to
        return TEST_NETWORK, TEST_ASSET, TEST_PAY_TO

    def facilitator_client(self, base_url):
        ns = sdk()
        from x402.http.facilitator_client import HTTPFacilitatorClientSync
        from x402.http.facilitator_client_base import FacilitatorConfig
        if self._client is None:
            if self.settings.provider_mode == 'production':
                headers = self._auth_headers()
                config = FacilitatorConfig(url=self.settings.facilitator_url, timeout=self.settings.limits['facilitator_timeout_seconds'],
                                           auth_provider=None)
                self._client = HTTPFacilitatorClientSync({'url': self.settings.facilitator_url,
                                                          'timeout': self.settings.limits['facilitator_timeout_seconds'],
                                                          'create_headers': lambda: headers})
            else:
                self._client = HTTPFacilitatorClientSync(FacilitatorConfig(url=base_url + '/facilitator-double',
                                                                           timeout=self.settings.limits['facilitator_timeout_seconds']))
        return self._client

    def _auth_headers(self):
        path = self.settings.facilitator_credential_file
        try:
            data = json.load(open(path))
        except (OSError, ValueError):
            raise ServiceError('CAPABILITY_UNAVAILABLE', 'facilitator credential file unreadable') from None
        if type(data) is not dict or not all(type(k) is str and type(v) is str for k, v in data.items()):
            raise ServiceError('CAPABILITY_UNAVAILABLE', 'facilitator credential file must map header names to values')
        return {'verify': dict(data), 'settle': dict(data), 'supported': dict(data)}

    def _build(self, base_url, job, contract, doc, price_amount):
        ns = sdk()
        from integrations.x402 import loopback_harness as lb
        network, asset, pay_to = self.terms()
        core = ns.server.x402ResourceServerSync(self.facilitator_client(base_url))
        core.register(network, lb.LocalServerScheme())
        path = '/api/v1/x402/jobs/' + job['id'] + '/public-bundle'
        expected = {'job_id': job['id'], 'contract_digest': contract['contract_digest'], 'evidence_root': job['evidence_root'],
                    'resource_version': 'public-bundle/v1', 'route': 'GET ' + path}
        seen = {}

        def bind(ctx):
            accepted = ctx.requirements
            # The payment identifier is the CLIENT's idempotency key (payment-identifier extension);
            # it must be present and well formed, and the sale is recorded under it.
            got = ns.pi.extract_payment_identifier(ctx.payment_payload, validate=False)
            if not got or not ns.pi.is_valid_payment_id(got):
                return ns.schemas.AbortResult(reason='work_contract_payment_identifier_required')
            seen['identifier'] = got
            if (accepted.amount != str(price_amount) or accepted.pay_to != pay_to or accepted.network != network
                    or accepted.asset != asset or any(accepted.extra.get(k) != v for k, v in expected.items())):
                return ns.schemas.AbortResult(reason=ERR_BINDING)
            resource = getattr(ctx.payment_payload, 'resource', None)
            if resource is None or getattr(resource, 'url', None) != base_url + path:
                return ns.schemas.AbortResult(reason=ERR_BINDING)
            return None
        core.on_before_verify(bind)
        option = ns.http.PaymentOption(scheme='exact', pay_to=pay_to,
                                       price=ns.schemas.AssetAmount(amount=str(price_amount), asset=asset, extra={'name': 'USDC', 'version': '2'}),
                                       network=network, max_timeout_seconds=300, extra=expected)
        routes = {'GET ' + path: ns.http.RouteConfig(accepts=option, resource=base_url + path,
                                                     description='public bundle of an accepted MetaCoin job', mime_type='application/json',
                                                     extensions={ns.pi.PAYMENT_IDENTIFIER: ns.pi.declare_payment_identifier_extension(required=True)})}
        server = ns.http.x402HTTPResourceServerSync(core, routes)
        server.initialize()
        return server, seen, expected, (network, asset, pay_to)

    def handle(self, db, request, body, base_url, job_id, workspace):
        """Full sale exchange for one request. Returns (status, headers, body_bytes)."""
        if not self.enabled():
            raise ServiceError('CAPABILITY_UNAVAILABLE', 'x402 sale route disabled in simulation provider mode')
        ns = sdk()
        job = db.execute('SELECT * FROM jobs WHERE id=? AND workspace=?', (job_id, workspace)).fetchone()
        if job is None or job['review_state'] != 'accepted':
            raise ServiceError('NOT_FOUND', 'no accepted job with a public bundle')
        contract = db.execute('SELECT * FROM contracts WHERE id=?', (job['contract_id'],)).fetchone()
        doc = merkle.parse(contract['contract_json'])
        review = db.execute('SELECT public_bundle_artifact_id FROM reviews WHERE job_id=?', (job_id,)).fetchone()
        if review is None or review['public_bundle_artifact_id'] is None:
            raise ServiceError('NOT_FOUND', 'no public bundle')
        price = json.loads(contract['policy_json'])['amount']
        try:
            server, seen, expected, (network, asset, pay_to) = self._build(base_url, job, contract, doc, price)
        except ServiceError:
            raise
        except Exception:
            raise ServiceError('PROVIDER_UNAVAILABLE', 'facilitator unreachable at initialization') from None
        ctx = ns.http.HTTPRequestContext(adapter=StarletteAdapter(request, body), path=request.url.path, method=request.method)
        result = server.process_http_request(ctx)
        if result.type == 'payment-error':
            return result.response.status, dict(result.response.headers), json.dumps(result.response.body or {}).encode()
        if result.type != 'payment-verified':
            raise ServiceError('CONFLICT', 'route not protected')
        identifier = seen['identifier']
        # Persist the sale intent BEFORE settlement so the outcome can be reconciled.
        requirements_digest = hashlib.sha256(result.payment_requirements.model_dump_json(by_alias=True).encode()).hexdigest()
        existing = db.execute('SELECT * FROM sales WHERE payment_id=?', (identifier,)).fetchone()
        if existing is not None and existing['job_id'] != job_id:
            raise ServiceError('REQUEST_ID_REBOUND', 'payment identifier already used for another sale')
        if existing is not None and existing['state'] == 'CONFIRMED':
            # Idempotent re-delivery of an already settled sale: same bound resource, recorded settlement.
            from x402.http.utils import encode_payment_response_header
            recorded = ns.schemas.SettleResponse(success=True, transaction=existing['transaction_ref'], network=existing['network'],
                                                 payer=existing['payer'], amount=existing['amount'])
            bundle = self.store.load(db, review['public_bundle_artifact_id'], workspace)
            return 200, {ns.http.PAYMENT_RESPONSE_HEADER: encode_payment_response_header(recorded), 'X-Sale-State': 'already-settled'}, bundle
        if existing is None:
            db.execute('INSERT INTO sales VALUES (?,?,?,?,?,?,?,?,?,?,NULL,NULL,?,?,?)',
                       (identifier, workspace, job_id, expected['route'], str(price), asset, network, pay_to,
                        self.settings.provider_mode, 'SUBMISSION_PENDING', requirements_digest, now(), now()))
            history.record(db, workspace, 'x402-route', 'sale.requested', 'job', job_id,
                           {'payment_id': identifier, 'amount': str(price), 'asset': asset, 'network': network, 'direction': 'customer-buys-result'})
        db.execute('COMMIT')      # intent durable before the external call
        db.execute('BEGIN IMMEDIATE')
        try:
            settled = server.process_settlement(result.payment_payload, result.payment_requirements, context=ctx,
                                                declared_extensions=result.declared_extensions)
        except Exception:
            db.execute("UPDATE sales SET state='OUTCOME_UNKNOWN', updated_at=? WHERE payment_id=?", (now(), identifier))
            history.record(db, workspace, 'x402-route', 'sale.unknown', 'job', job_id, {'payment_id': identifier})
            raise ServiceError('PROVIDER_UNAVAILABLE', 'settlement outcome unknown; reconcile before retrying')
        response = settled.settle_response.model_dump(by_alias=True, exclude_none=True) if settled.settle_response else {}
        consistent = (settled.success and response.get('amount') == str(price) and response.get('network') == network
                      and isinstance(settled.transaction, str) and len(settled.transaction) == 66)
        if settled.success and not consistent:
            db.execute("UPDATE sales SET state='OUTCOME_UNKNOWN', updated_at=? WHERE payment_id=?", (now(), identifier))
            history.record(db, workspace, 'x402-route', 'sale.unknown', 'job', job_id, {'payment_id': identifier, 'inconsistent': True})
            raise ServiceError('ADAPTER_RESPONSE_INVALID', 'settlement answer inconsistent with the bound sale')
        if not settled.success:
            state = 'OUTCOME_UNKNOWN' if settled.error_reason == 'settlement_pending' else 'FAILED_CONFIRMED'
            db.execute("UPDATE sales SET state=?, updated_at=? WHERE payment_id=?", (state, now(), identifier))
            history.record(db, workspace, 'x402-route', 'sale.failed' if state == 'FAILED_CONFIRMED' else 'sale.unknown', 'job', job_id,
                           {'payment_id': identifier, 'error_reason': settled.error_reason})
            return settled.response.status, dict(settled.headers), json.dumps({'error': settled.error_reason}).encode()
        db.execute("UPDATE sales SET state='CONFIRMED', transaction_ref=?, payer=?, updated_at=? WHERE payment_id=?",
                   (settled.transaction, settled.payer, now(), identifier))
        history.record(db, workspace, 'x402-route', 'sale.settled', 'job', job_id, {'payment_id': identifier, 'transaction': settled.transaction})
        bundle = self.store.load(db, review['public_bundle_artifact_id'], workspace)
        return 200, dict(settled.headers), bundle

    def handle_invoke(self, db, request, body, base_url, sid, quote, principal, invoke):
        """Priced invocation over x402: 402 requirements bound to the accepted quote and the request body
        digest; after settlement the quote is consumed and the job created. Amount = quote amount_max."""
        if not self.enabled():
            raise ServiceError('CAPABILITY_UNAVAILABLE', 'x402 invoke route disabled in simulation provider mode')
        ns = sdk()
        from integrations.x402 import loopback_harness as lb
        network, asset, pay_to = self.terms()
        if quote['provider_mode'] != self.settings.provider_mode:
            raise ServiceError('CONFLICT', 'quote provider mode differs from the service')
        path = '/api/v1/x402/services/' + sid + '/invoke'
        body_digest = hashlib.sha256(body).hexdigest()
        expected = {'quote_id': quote['id'], 'request_digest': quote['request_digest'], 'service_revision': str(quote['service_revision']),
                    'body_sha256': body_digest, 'resource_version': 'service-invoke/v1', 'route': 'POST ' + path}
        price = quote['amount_max']
        core = ns.server.x402ResourceServerSync(self.facilitator_client(base_url))
        core.register(network, lb.LocalServerScheme())
        seen = {}

        def bind(ctx):
            accepted = ctx.requirements
            got = ns.pi.extract_payment_identifier(ctx.payment_payload, validate=False)
            if not got or not ns.pi.is_valid_payment_id(got):
                return ns.schemas.AbortResult(reason='work_contract_payment_identifier_required')
            seen['identifier'] = got
            if quote['state'] == 'consumed':
                prior = db.execute("SELECT state FROM invoke_sales WHERE payment_id=? AND quote_id=?", (got, quote['id'])).fetchone()
                if prior is None or prior['state'] != 'CONFIRMED':
                    return ns.schemas.AbortResult(reason='work_contract_quote_consumed')
            if (accepted.amount != str(price) or accepted.pay_to != pay_to or accepted.network != network or accepted.asset != asset
                    or any(accepted.extra.get(k) != v for k, v in expected.items())):
                return ns.schemas.AbortResult(reason=ERR_BINDING)
            resource = getattr(ctx.payment_payload, 'resource', None)
            if resource is None or getattr(resource, 'url', None) != base_url + path:
                return ns.schemas.AbortResult(reason=ERR_BINDING)
            return None
        core.on_before_verify(bind)
        option = ns.http.PaymentOption(scheme='exact', pay_to=pay_to, price=ns.schemas.AssetAmount(amount=str(price), asset=asset, extra={'name': 'USDC', 'version': '2'}),
                                       network=network, max_timeout_seconds=300, extra=expected)
        routes = {'POST ' + path: ns.http.RouteConfig(accepts=option, resource=base_url + path, description='priced invocation under quote ' + quote['id'], mime_type='application/json',
                                                      extensions={ns.pi.PAYMENT_IDENTIFIER: ns.pi.declare_payment_identifier_extension(required=True)})}
        server = ns.http.x402HTTPResourceServerSync(core, routes)
        server.initialize()
        ctx = ns.http.HTTPRequestContext(adapter=StarletteAdapter(request, body), path=path, method='POST')
        result = server.process_http_request(ctx)
        if result.type == 'payment-error':
            return result.response.status, dict(result.response.headers), json.dumps(result.response.body or {}).encode()
        if result.type != 'payment-verified':
            raise ServiceError('CONFLICT', 'route not protected')
        identifier = seen['identifier']
        existing = db.execute('SELECT * FROM invoke_sales WHERE payment_id=?', (identifier,)).fetchone()
        if existing is not None and existing['state'] == 'CONFIRMED':
            from x402.http.utils import encode_payment_response_header
            recorded = ns.schemas.SettleResponse(success=True, transaction=existing['transaction_ref'], network=existing['network'], payer=existing['payer'], amount=existing['amount'])
            job = db.execute('SELECT id, contract_id, state FROM jobs WHERE quote_id=?', (quote['id'],)).fetchone()
            return 200, {ns.http.PAYMENT_RESPONSE_HEADER: encode_payment_response_header(recorded), 'X-Sale-State': 'already-settled'}, \
                json.dumps({'job_id': job['id'] if job else None, 'state': job['state'] if job else None, 'quote_id': quote['id'], 'replayed': True}).encode()
        requirements_digest = hashlib.sha256(result.payment_requirements.model_dump_json(by_alias=True).encode()).hexdigest()
        if existing is None:
            db.execute('INSERT INTO invoke_sales VALUES (?,?,?,NULL,?,?,?,?,?,?,?,NULL,NULL,?,?,?)',
                       (identifier, principal.workspace, quote['id'], expected['route'], str(price), asset, network, pay_to,
                        self.settings.provider_mode, 'SUBMISSION_PENDING', requirements_digest, now(), now()))
            history.record(db, principal.workspace, principal.id, 'sale.requested', 'quote', quote['id'], {'payment_id': identifier, 'amount': str(price), 'direction': 'customer-buys-invocation'})
        db.execute('COMMIT'); db.execute('BEGIN IMMEDIATE')
        try:
            settled = server.process_settlement(result.payment_payload, result.payment_requirements, context=ctx, declared_extensions=result.declared_extensions)
        except Exception:
            db.execute("UPDATE invoke_sales SET state='OUTCOME_UNKNOWN', updated_at=? WHERE payment_id=?", (now(), identifier))
            raise ServiceError('PROVIDER_UNAVAILABLE', 'settlement outcome unknown; reconcile before retrying')
        response = settled.settle_response.model_dump(by_alias=True, exclude_none=True) if settled.settle_response else {}
        consistent = settled.success and response.get('amount') == str(price) and response.get('network') == network and isinstance(settled.transaction, str) and len(settled.transaction) == 66
        if settled.success and not consistent:
            db.execute("UPDATE invoke_sales SET state='OUTCOME_UNKNOWN', updated_at=? WHERE payment_id=?", (now(), identifier))
            raise ServiceError('ADAPTER_RESPONSE_INVALID', 'settlement answer inconsistent with the bound sale')
        if not settled.success:
            state = 'OUTCOME_UNKNOWN' if settled.error_reason == 'settlement_pending' else 'FAILED_CONFIRMED'
            db.execute("UPDATE invoke_sales SET state=?, updated_at=? WHERE payment_id=?", (state, now(), identifier))
            return settled.response.status, dict(settled.headers), json.dumps({'error': settled.error_reason}).encode()
        out = invoke()                                   # consumes the quote atomically and creates the bound job
        db.execute("UPDATE invoke_sales SET state='CONFIRMED', transaction_ref=?, payer=?, job_id=?, updated_at=? WHERE payment_id=?",
                   (settled.transaction, settled.payer, out['job_id'], now(), identifier))
        history.record(db, principal.workspace, principal.id, 'sale.settled', 'job', out['job_id'], {'payment_id': identifier, 'transaction': settled.transaction, 'quote_id': quote['id']})
        return 202, dict(settled.headers), json.dumps(out).encode()

    def reconcile(self, db, principal, job_id):
        principal.require('action:reconcile')
        row = db.execute('SELECT * FROM sales WHERE job_id=? AND workspace=?', (job_id, principal.workspace)).fetchone()
        if row is None:
            raise ServiceError('NOT_FOUND', 'sale')
        if row['state'] in ('CONFIRMED', 'FAILED_CONFIRMED'):
            return dict(row, reconciliation='terminal-already')
        if row['provider_mode'] != 'test-http':
            return dict(row, reconciliation='unavailable: the facilitator interface exposes no authoritative lookup; exposure retained')
        record = self.double.lookup(row['payment_id'])
        if record is None:
            return dict(row, reconciliation='no-record-at-double; state retained')
        db.execute("UPDATE sales SET state='CONFIRMED', transaction_ref=?, payer=?, updated_at=? WHERE payment_id=?",
                   (record['transaction'], record.get('payer'), now(), row['payment_id']))
        history.record(db, principal.workspace, principal.id, 'sale.settled', 'job', job_id, {'payment_id': row['payment_id'], 'reconciled': True})
        return dict(db.execute('SELECT * FROM sales WHERE payment_id=?', (row['payment_id'],)).fetchone(), reconciliation='resolved-from-double-record')
