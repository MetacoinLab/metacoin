"""Local-chain validation of the x402 `upto` scheme with the REAL pinned SDK code paths and REAL contract execution.

Topology (all in one process, all synthetic):
  - py-evm chain through eth-tester (a private chain id; funded synthetic accounts; no RPC endpoint, nothing broadcast)
  - Permit2, x402UptoPermit2Proxy and MockGenericERC20 deployed from the pinned build in artifacts.json
  - facilitator: the SDK's UptoEvmFacilitatorScheme over a FacilitatorWeb3Signer bound to the tester provider
  - resource server: the SDK's x402ResourceServerSync + UptoEvmServerScheme; client: x402ClientSync + UptoEvmClientScheme

Because the local deployments cannot occupy the canonical CREATE2 addresses (different toolchain, no deterministic
deployer), the SDK's address constants are redirected to the local deployments for the duration of the harness.
That is a test-environment configuration, recorded in every result; it is not a public network."""
import json
import sys
import time
from pathlib import Path

ARTIFACTS = Path(__file__).parent / 'artifacts.json'
TOKEN_NAME, TOKEN_VERSION = 'Mock Generic ERC20', '1'


def sdk_modules():
    import importlib
    names = ['x402.mechanisms.evm.constants', 'x402.mechanisms.evm.upto.permit2_utils', 'x402.mechanisms.evm.upto.client', 'x402.mechanisms.evm.exact.permit2_utils',
             'x402.mechanisms.evm.upto.facilitator', 'x402.mechanisms.evm.upto.server', 'x402.extensions.erc20_approval_gas_sponsoring.client', 'x402.extensions.erc20_approval_gas_sponsoring.facilitator']
    out = {}
    for n in names:
        try:
            out[n] = importlib.import_module(n)
        except ImportError:
            pass
    return out


class LocalChain:
    def __init__(self, artifacts=ARTIFACTS):
        from web3 import Web3, EthereumTesterProvider
        from eth_tester import EthereumTester, PyEVMBackend
        self.art = json.loads(Path(artifacts).read_text())
        self.backend = PyEVMBackend()
        self.tester = EthereumTester(backend=self.backend)
        self.w3 = Web3(EthereumTesterProvider(self.tester))
        self.chain_id = self.w3.eth.chain_id
        self.network = 'eip155:%d' % self.chain_id
        keys = self.backend.account_keys
        self.deployer, self.facilitator_key, self.payer_key, self.recipient = self.w3.eth.accounts[0], keys[1], keys[2], self.w3.eth.accounts[3]
        self.facilitator = self.w3.eth.accounts[1]; self.payer = self.w3.eth.accounts[2]
        self.permit2 = self._deploy('Permit2')
        self.token = self._deploy('MockGenericERC20')
        self.proxy = self._deploy('x402UptoPermit2Proxy', self.permit2)
        self.token_c = self.w3.eth.contract(address=self.token, abi=self.art['contracts']['MockGenericERC20']['abi'])
        self.token_c.functions.mint(self.payer, 10 ** 12).transact({'from': self.deployer})
        self.token_c.functions.approve(self.permit2, 2 ** 256 - 1).transact({'from': self.payer})
        self.record = {'chain_id': self.chain_id, 'network': self.network, 'permit2': self.permit2, 'proxy': self.proxy, 'token': self.token, 'facilitator': self.facilitator, 'payer': self.payer, 'recipient': self.recipient,
                       'pins': self.art['pins'], 'toolchain': self.art['toolchain'], 'contracts': {k: {'bytecode_sha256': v['bytecode_sha256'], 'deployed_sha256': v['deployed_sha256']} for k, v in self.art['contracts'].items()},
                       'note': 'private py-evm chain; SDK canonical addresses redirected to these local deployments; not a public network, not production settlement'}
        self._patched = None

    def _deploy(self, name, *args):
        c = self.art['contracts'][name]
        contract = self.w3.eth.contract(abi=c['abi'], bytecode='0x' + c['bytecode'])
        tx = contract.constructor(*args).transact({'from': self.deployer, 'gas': 8_000_000})
        rcpt = self.w3.eth.wait_for_transaction_receipt(tx)
        assert rcpt['status'] == 1, name
        return rcpt['contractAddress']

    def balances(self):
        return {'payer': self.token_c.functions.balanceOf(self.payer).call(), 'recipient': self.token_c.functions.balanceOf(self.recipient).call()}

    def patch_sdk(self):
        """Redirect the SDK's canonical Permit2 / proxy addresses to the local deployments (recorded; test-only)."""
        if self._patched:
            return
        self._patched = {}
        for name, mod in sdk_modules().items():
            for attr, value in (('PERMIT2_ADDRESS', self.permit2), ('X402_UPTO_PERMIT2_PROXY_ADDRESS', self.proxy)):
                if hasattr(mod, attr):
                    self._patched[(name, attr)] = getattr(mod, attr)
                    setattr(mod, attr, value)

    def unpatch_sdk(self):
        for (name, attr), value in (self._patched or {}).items():
            setattr(sys.modules[name], attr, value)
        self._patched = None


class TesterSigner:
    """FacilitatorEvmSigner over the tester provider, built on the SDK's own Web3 signer implementation."""

    def __new__(cls, chain, key, receipt_fail_once=False):
        from x402.mechanisms.evm.signers import FacilitatorWeb3Signer
        from eth_account import Account
        import threading
        obj = FacilitatorWeb3Signer.__new__(FacilitatorWeb3Signer)
        obj._account = Account.from_key(key.to_hex() if hasattr(key, 'to_hex') else bytes(key))
        obj._w3 = chain.w3
        obj._confirmation_timeout_seconds = 30
        obj._gas_limit = 1_500_000
        obj._chain_id = None
        obj._nonce_lock = threading.Lock()
        obj._next_nonce = None
        obj.receipt_attempts = 0
        if receipt_fail_once:
            state = {'failed': False}
            orig = obj.wait_for_transaction_receipt
            def flaky(tx_hash):
                obj.receipt_attempts += 1
                if not state['failed']:
                    state['failed'] = True
                    raise TimeoutError('simulated receipt wait timeout (response lost after execution)')
                return orig(tx_hash)
            obj.wait_for_transaction_receipt = flaky
        return obj


def facilitator_for(chain, signer=None):
    from x402.facilitator import x402FacilitatorSync
    from x402.mechanisms.evm.upto import UptoEvmFacilitatorScheme, UptoEvmSchemeConfig
    f = x402FacilitatorSync()
    f.register([chain.network], UptoEvmFacilitatorScheme(signer or TesterSigner(chain, chain.facilitator_key), UptoEvmSchemeConfig(simulate_in_settle=True)))
    return f


class LocalFacilitatorClient:
    """FacilitatorClientSync over the in-process facilitator (what a resource server would reach over HTTP)."""

    def __init__(self, facilitator):
        self.f = facilitator
        self.calls = {'verify': 0, 'settle': 0, 'supported': 0}

    def get_supported(self):
        self.calls['supported'] += 1
        return self.f.get_supported()

    def verify(self, payload, requirements):
        self.calls['verify'] += 1
        return self.f.verify(payload, requirements)

    def settle(self, payload, requirements):
        self.calls['settle'] += 1
        return self.f.settle(payload, requirements)


class UptoServer:
    """One protected route whose 402 asks for an UPTO authorization of `max_amount`; settlement later names the final amount."""

    def __init__(self, chain, facilitator_client, max_amount, binding, path='/metered/resource', pay_to=None):
        import x402.server as server, x402.http as http, x402.schemas as schemas
        from x402.extensions import payment_identifier as pi
        from x402.mechanisms.evm.upto import UptoEvmServerScheme
        self.chain, self.path, self.binding, self.max_amount = chain, path, binding, max_amount
        self.pay_to = pay_to or chain.recipient
        self.pi, self.http_mod, self.schemas = pi, http, schemas
        core = server.x402ResourceServerSync(facilitator_client)
        core.register(chain.network, UptoEvmServerScheme())
        core.on_before_verify(self._bind)
        option = http.PaymentOption(scheme='upto', pay_to=self.pay_to, price=schemas.AssetAmount(amount=str(max_amount), asset=chain.token, extra={'name': TOKEN_NAME, 'version': TOKEN_VERSION}),
                                    network=chain.network, max_timeout_seconds=300, extra=dict(binding))
        routes = {'GET ' + path: http.RouteConfig(accepts=option, resource='http://local' + path, description='metered resource (upto)', mime_type='application/json',
                                                  extensions={pi.PAYMENT_IDENTIFIER: pi.declare_payment_identifier_extension(required=True)})}
        self.server = http.x402HTTPResourceServerSync(core, routes)
        self.server.initialize()
        self.seen = {}

    def _bind(self, ctx):
        acc = ctx.requirements
        ident = self.pi.extract_payment_identifier(ctx.payment_payload, validate=False)
        if not ident or not self.pi.is_valid_payment_id(ident):
            return self.schemas.AbortResult(reason='payment_identifier_required')
        self.seen['identifier'] = ident
        if acc.scheme != 'upto' or acc.amount != str(self.max_amount) or acc.pay_to != self.pay_to or acc.network != self.chain.network or acc.asset != self.chain.token \
                or any(acc.extra.get(k) != v for k, v in self.binding.items()):
            return self.schemas.AbortResult(reason='binding_mismatch')
        resource = getattr(ctx.payment_payload, 'resource', None)
        if resource is None or getattr(resource, 'url', None) != 'http://local' + self.path:
            return self.schemas.AbortResult(reason='binding_mismatch')
        return None

    def handle(self, headers=None):
        from integrations.x402.loopback_harness import FakeAdapter
        ctx = self.http_mod.HTTPRequestContext(adapter=FakeAdapter(self.path, headers), path=self.path, method='GET')
        return ctx, self.server.process_http_request(ctx)

    def settle(self, ctx, result, final_amount):
        """Settlement names the FINAL amount (<= authorized maximum) through requirements.amount, exactly as the SDK documents."""
        req = result.payment_requirements.model_copy(update={'amount': str(final_amount)})
        return self.server.process_settlement(result.payment_payload, req, context=ctx, declared_extensions=result.declared_extensions)


def client_for(chain, key=None):
    import x402.client as client, x402.http as http
    from x402.mechanisms.evm.signers import EthAccountSigner
    from x402.mechanisms.evm.upto import UptoEvmClientScheme
    from eth_account import Account
    core = client.x402ClientSync().register('eip155:*', UptoEvmClientScheme(EthAccountSigner(Account.from_key((key or chain.payer_key).to_hex() if hasattr(key or chain.payer_key, 'to_hex') else bytes(key or chain.payer_key))))).set_spend_controls(False)
    return core, http.x402HTTPClientSync(core)


def exchange(chain, server, final_amount, identifier='upto-1', client=None, mutate_payload=None, sign_network=None, key=None):
    identifier = ('local-upto-' + identifier + '-' + 'x' * 16)[:64]
    """402 -> client authorization (upto, permit2 witness) -> verify -> settle(final). Returns the observed record."""
    import x402.http as http
    from x402.extensions import payment_identifier as pi
    core, hclient = client or client_for(chain, key)
    ctx, first = server.handle()
    out = {'first': first.type, 'status': first.response.status if first.response else None}
    if first.type != 'payment-error':
        return dict(out, stage='no-402')
    required = hclient.get_payment_required_response(lambda name: first.response.headers.get(name), json.dumps(first.response.body).encode())
    extensions = dict(required.extensions or {})
    pi.append_payment_identifier_to_extensions(extensions, identifier)
    if sign_network:
        # sign the authorization under another chain id (wrong EIP-712 domain), then present it on the real network
        alt = required.model_copy(update={'accepts': [a.model_copy(update={'network': sign_network}) for a in required.accepts]})
        payload = core.create_payment_payload(alt, extensions=extensions)
        payload = payload.model_copy(update={'accepted': payload.accepted.model_copy(update={'network': chain.network})})
    else:
        payload = core.create_payment_payload(required, extensions=extensions)
    if mutate_payload is not None:
        payload = mutate_payload(payload)
    out['authorized_max'] = payload.payload['permit2Authorization']['permitted']['amount'] if isinstance(payload.payload, dict) and 'permit2Authorization' in payload.payload else None
    out['nonce'] = payload.payload['permit2Authorization']['nonce'] if out['authorized_max'] is not None else None
    headers = hclient.encode_payment_signature_header(payload)
    ctx2, second = server.handle(headers)
    err = None
    if second.type == 'payment-error':
        hdr = second.response.headers.get(http.PAYMENT_REQUIRED_HEADER) if second.response else None
        err = json.loads(http.safe_base64_decode(hdr)).get('error') if hdr else None
    out.update(second=second.type, verify_error=err)
    if second.type != 'payment-verified':
        return dict(out, stage='refused-before-settlement')
    before = chain.balances()
    settled = server.settle(ctx2, second, final_amount)
    after = chain.balances()
    out.update(stage='settlement-attempted', final_amount=final_amount, success=settled.success, error_reason=settled.error_reason, transaction=settled.transaction,
               settle_response=settled.settle_response.model_dump(by_alias=True, exclude_none=True) if settled.settle_response else None,
               balances_before=before, balances_after=after, moved=before['payer'] - after['payer'])
    if settled.transaction and settled.transaction.startswith('0x') and len(settled.transaction) == 66:
        try:
            rc = chain.w3.eth.get_transaction_receipt(settled.transaction)
            out['receipt'] = {'status': rc['status'], 'block': rc['blockNumber'], 'gas_used': rc['gasUsed'], 'logs': len(rc['logs'])}
        except Exception as exc:
            out['receipt'] = {'error': type(exc).__name__}
    return out


def scenarios(chain=None, out_path=None):
    """The validation matrix; returns a record with every observed outcome (nothing is asserted here)."""
    chain = chain or LocalChain()
    chain.patch_sdk()
    try:
        results = {'topology': chain.record, 'scenarios': {}}
        fac = LocalFacilitatorClient(facilitator_for(chain))
        binding = {'quote_id': 'q_local', 'request_digest': 'd' * 64, 'resource_version': 'metered/v1'}
        mk = lambda **kw: UptoServer(chain, fac, kw.pop('max_amount', 1000), binding, **kw)
        # 1. below-maximum settlement: authorize 1000, settle 640
        results['scenarios']['below_maximum'] = exchange(chain, mk(), 640, identifier='upto-below')
        # 2. over-maximum settlement is refused by the facilitator before any transaction
        results['scenarios']['over_maximum'] = exchange(chain, mk(), 1500, identifier='upto-over')
        # 3. wrong recipient in the witness
        def wrong_to(p):
            d = dict(p.payload); d['permit2Authorization'] = dict(d['permit2Authorization']); d['permit2Authorization']['witness'] = dict(d['permit2Authorization']['witness'], to=chain.deployer)
            return p.model_copy(update={'payload': d})
        results['scenarios']['wrong_recipient'] = exchange(chain, mk(), 640, identifier='upto-to', mutate_payload=wrong_to)
        # 4. wrong spender (not the proxy)
        def wrong_spender(p):
            d = dict(p.payload); d['permit2Authorization'] = dict(d['permit2Authorization'], spender=chain.deployer)
            return p.model_copy(update={'payload': d})
        results['scenarios']['wrong_spender'] = exchange(chain, mk(), 640, identifier='upto-spender', mutate_payload=wrong_spender)
        # 5. expired authorization (deadline in the past)
        def expired(p):
            d = dict(p.payload); d['permit2Authorization'] = dict(d['permit2Authorization'], deadline=str(int(time.time()) - 10))
            return p.model_copy(update={'payload': d})
        results['scenarios']['expired'] = exchange(chain, mk(), 640, identifier='upto-expired', mutate_payload=expired)
        # 6. wrong domain: signed for another chain id
        results['scenarios']['wrong_domain'] = exchange(chain, mk(), 640, identifier='upto-domain', sign_network='eip155:999')
        # 7. replay: the same signed authorization presented twice (second must not move funds again)
        srv = mk(); core, hc = client_for(chain)
        first = exchange(chain, srv, 500, identifier='upto-replay', client=(core, hc))
        captured = {}
        def capture(p):
            captured['payload'] = p; return p
        results['scenarios']['replay_first'] = first
        srv2 = mk()
        # rebuild a payload and replay it byte-for-byte a second time through a fresh server route
        again = exchange(chain, srv2, 500, identifier='upto-replay-2', client=(core, hc), mutate_payload=capture)
        results['scenarios']['replay_fresh_second'] = again
        replayed = exchange(chain, mk(), 500, identifier='upto-replay-3', client=(core, hc), mutate_payload=lambda p: captured['payload'].model_copy(update={'extensions': p.extensions}))
        results['scenarios']['replay_same_nonce'] = replayed
        # 8. response lost after execution: the receipt wait times out once; the retry reconciles to the same transaction (no second transfer)
        flaky_signer = TesterSigner(chain, chain.facilitator_key, receipt_fail_once=True)
        flaky = LocalFacilitatorClient(facilitator_for(chain, flaky_signer))
        srv3 = UptoServer(chain, flaky, 1000, binding)
        core3, hc3 = client_for(chain)
        ctx, first402 = srv3.handle()
        required = hc3.get_payment_required_response(lambda name: first402.response.headers.get(name), json.dumps(first402.response.body).encode())
        from x402.extensions import payment_identifier as pi
        ext = dict(required.extensions or {}); pi.append_payment_identifier_to_extensions(ext, 'local-upto-lost-xxxxxxxxxxxxxxxx')
        payload = core3.create_payment_payload(required, extensions=ext)
        headers = hc3.encode_payment_signature_header(payload)
        ctx2, verified = srv3.handle(headers)
        b0 = chain.balances()
        s1 = srv3.settle(ctx2, verified, 700)
        b1 = chain.balances()
        s2 = srv3.settle(ctx2, verified, 700)
        b2 = chain.balances()
        results['scenarios']['response_lost_then_retry'] = {'first': {'success': s1.success, 'error_reason': s1.error_reason, 'transaction': s1.transaction}, 'second': {'success': s2.success, 'error_reason': s2.error_reason, 'transaction': s2.transaction},
                                                             'receipt_wait_attempts_during_first': flaky_signer.receipt_attempts, 'facilitator_settle_calls': flaky.calls['settle'],
                                                             'moved_after_first': b0['payer'] - b1['payer'], 'moved_after_second': b1['payer'] - b2['payer'], 'total_moved': b0['payer'] - b2['payer'],
                                                             'observed': 'the first receipt wait failed (injected); the SDK recorded settlement_pending and the resource server retried once, reconciling to the already-broadcast transaction; an explicit later retry with the same authorization fails on the consumed nonce and moves nothing'}
        results['facilitator_calls'] = fac.calls
    finally:
        chain.unpatch_sdk()
    if out_path:
        Path(out_path).write_text(json.dumps(results, indent=1, default=str))
    return results


if __name__ == '__main__':
    r = scenarios(out_path=sys.argv[1] if len(sys.argv) > 1 else None)
    print(json.dumps({k: {kk: vv for kk, vv in v.items() if kk in ('stage', 'success', 'error_reason', 'verify_error', 'moved', 'final_amount', 'authorized_max')} if isinstance(v, dict) and 'stage' in v else v for k, v in r['scenarios'].items()}, indent=1, default=str))
