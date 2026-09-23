# x402 payment boundary: inventory, capabilities, and the SDK loopback harness

Updated 2026-09-23. Two things live here: the adapter over MetaCoin's existing
x402-class simulation (`legacy_adapter.py`), and an OFFLINE compatibility
harness against the pinned official x402 Python SDK (`loopback_harness.py`,
`loopback_adapter.py`, `test_loopback.py`). Neither settles anything anywhere.

## 1. Integration inventory (what actually exists in this checkout)

| Item | Finding |
|---|---|
| Owner's existing x402 implementation | `demo/x402_spend_stub.py::buy_compute` (called by `demo/agent_loop.py`, `demo/economy_demo.py`), guarded by the faucet's `spend()`. It declares itself a zero-value simulation: no HTTP 402 exchange, no network, no facilitator, no signatures. Searched on 2026-09-23: this repository (all branches: only `main`), `~/projects/metacoin-lab` (mentions x402 only in watch/revenue notes), and no other local checkout. **No newer x402 implementation was found.** |
| Entry point wrapped by Step 01 | `integrations/x402/legacy_adapter.py::LegacyAdapter` (capability `legacy_simulation`), request type = the journal's bound action request, response type = `{state, request_digest, reference, capability, compute_units}` |
| Installed / pinned version | the stub has no version; the protocol core is Python ≥ 3.12 standard library with zero runtime dependencies (pyproject) |
| Transport / payment scheme | none in the stub. The harness below uses SDK 2.24.0's V2 headers `PAYMENT-REQUIRED`, `PAYMENT-SIGNATURE`, `PAYMENT-RESPONSE` in-process |
| Network / asset bindings | stub: `local-simulation` / `Test-META`. Harness: CAIP-2 `eip155:84532` and the Base-Sepolia USDC address **as identifiers only**; nothing is contacted |
| Signing / authorization | stub: none. Harness: none (the `evm` extra is not installed; payloads carry a constant placeholder). In the real `exact` scheme the EIP-712 signature covers from/to/value/validAfter/validBefore/nonce with the asset contract as domain; the payment identifier, `extra`, and `resource` are **not signed** |
| Idempotency / reconciliation | stub adapter: per-process memory keyed by request id + request digest; reconciliation only from the same instance. Harness: payment-identifier extension bound to the request digest; the facilitator double records settlements by identifier; `settlement_pending` is retried exactly once by the SDK resource server |
| Test isolation from live spending | the stub moves zero-value Test-META inside one process; the harness has no network path at all; `test_loopback.py` skips when the SDK is absent; the core CI job never installs it |
| Absent or unverified here | real HTTP over a socket, facilitator service, on-chain settlement, signature verification, SDK middleware for FastAPI/Flask, any x402 network fee behaviour, remote CI |

## 2. Capability table

| Capability | legacy_simulation | durable_test_simulation (test facility) | x402_loopback_test (SDK harness) | Real settlement |
|---|---|---|---|---|
| Transport | in-process call | in-process call | in-process SDK HTTP server/client objects, no socket | unavailable |
| Settlement | zero-value faucet | file-backed synthetic balance | facilitator double | unavailable |
| Signature coverage | none | none | none (placeholder); real-scheme coverage documented above | not exercised |
| Idempotency | per-process memory | durable file by request id + digest | payment identifier = `wc_<request digest>`, double record | – |
| Reconciliation | same instance only | authoritative, key voided on absence | double lookup by identifier | – |
| Status | implemented, simulated | implemented, testing facility | SDK/transport-tested (offline) | deferred; needs a rail, credentials and the owner's decision |

## 3. The loopback harness: what was tested and what it proves

SDK: `x402` 2.24.0 from PyPI (wheel sha256
`515171258b32af36c05b2a6aa8d2e86ff6cf70ecc731151562fdd90859987fe1`), core
dependencies pydantic 2.13.5 and nest-asyncio 1.6.0, installed into an isolated
virtual environment. It is **not** a dependency of the protocol package.

```sh
python3 -m venv /path/to/venv && /path/to/venv/bin/pip install x402==2.24.0
/path/to/venv/bin/python -m unittest integrations.x402.test_loopback -v     # 9 tests
python3 -m unittest integrations.x402.test_loopback                          # stdlib interpreter: 8 skipped, reported
```

Composition (all duck-typed against SDK protocols, executed in the tests):
`x402ResourceServerSync` + a local `exact` scheme server + a facilitator
double, wrapped by `x402HTTPResourceServerSync` with one route per
journal-authorized request; `x402ClientSync` + a local client scheme +
`x402HTTPClientSync`; the payment-identifier extension declared `required`.
The route's `extra` carries the contract digest, job id, evidence root and
request digest; a `before_verify` hook enforces the journal binding (amount,
pay-to, network, asset, both digests, the payment identifier, and the resource).

Observed refusals (`test_transport_level_refusals_from_the_sdk`,
`test_application_level_binding_at_the_hook`):

| Case | Where refused | Observed |
|---|---|---|
| altered amount, wrong pay-to, wrong network, stale offer (maxTimeoutSeconds changed), changed `extra` (contract digest) | SDK transport: `find_matching_requirements` | 402, "No matching payment requirements" |
| invalid signature | facilitator double `verify` | 402, `invalid_signature` (the double checks a placeholder, not cryptography) |
| missing payment identifier | SDK `validate_extensions` echo check / hook | 402 |
| foreign payment identifier | application hook | 402, `work_contract_payment_identifier_mismatch` |
| **changed `resource`** | **not refused by the SDK resource server in 2.24.0**; refused by the application hook | 402, `work_contract_binding_mismatch` |
| same identifier twice | facilitator double idempotency | one debit, same transaction |
| mismatched settlement answer (amount/network differ) | `LoopbackAdapter._bound` | journal keeps `OUTCOME_UNKNOWN`, exposure retained |
| `settlement_pending` once / always | SDK retries once | CONFIRMED after retry / `OUTCOME_UNKNOWN`, later reconciled from the record |
| insufficient funds at the double | facilitator double | `FAILED_CONFIRMED`, budget released |
| expired authorization | harness refuses to build an offer | no 402 is ever produced |

What this establishes: our journal-bound request can be expressed as SDK 2.24.0
payment requirements, an SDK client produces a payload the SDK server matches
and verifies, lifecycle hooks can refuse on application bindings, and the
adapter maps SDK settle responses onto the journal's bound outcome shape
without ever confirming an inconsistent answer. What it does not establish:
on-chain settlement, signature validity, network timeouts, facilitator
behaviour, or fee handling. A contract digest checked in `extra` by the
resource server is a server-side check; it is not covered by the payer's
signature. Do not describe this harness as x402 settlement.

## 4. Economic direction

The action modelled everywhere here is the **agent buying imaginary next-step
compute after accepted analysis**; fixture funding comes from the separately
labeled lunar-link task and is not a reward for the new analysis. A customer
paying for analysis, escrow, refunds and disputes are not implemented; the
contract's dispute string is descriptive. Prepayment for a resource is a
bounded input cost authorized by the contract, not outcome-contingent payment.

## 5. Next step toward a real rail

Choose the rail (facilitator URL, network, asset, funded test key) and install
the SDK's `evm` extra in the integration environment only. Replace the
facilitator double with `HTTPFacilitatorClientSync`, keep the hook, and make
`LoopbackAdapter.reconcile` query the facilitator's authoritative record. The
two properties the journal needs from that rail before its answers may release
budget: a durable request-bound outcome, and an authoritative "no record"
answer that refuses later submission. Test on a test network first; nothing in
this order authorizes funded operation.

References checked against the pinned source, not only the documentation:
https://docs.x402.org/core-concepts/http-402,
https://docs.x402.org/extensions/payment-identifier,
https://docs.x402.org/extensions/offer-receipt,
https://docs.x402.org/advanced-concepts/lifecycle-hooks.
