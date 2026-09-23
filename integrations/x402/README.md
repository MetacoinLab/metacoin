# Existing x402-class payment boundary

`legacy_adapter.py` wraps `demo/x402_spend_stub.py::buy_compute`; it does not
replace that implementation or touch private faucet balances. The shared
adapter instance serializes access to its in-memory faucet and memoizes an
outcome by request ID and bound request digest. The persistent work journal
provides the campaign/job authorization boundary separately.

Capability: `legacy_simulation`. Asset: zero-value Test-META. Network:
`local-simulation`. Resource: imaginary next-compute. No installed x402 SDK,
HTTP exchange, facilitator, chain settlement, or signed offer implementation
was found in the reviewed public checkout. This says nothing about an owner's
newer implementation in another repository or branch.

The current official x402 documents describe V2 transport headers, idempotency,
signed offers/receipts and lifecycle hooks. They are references for a future
adapter to the owner's actual integration; we have not claimed wire compatibility:

- https://docs.x402.org/core-concepts/http-402
- https://docs.x402.org/extensions/payment-identifier
- https://docs.x402.org/extensions/offer-receipt
- https://docs.x402.org/advanced-concepts/lifecycle-hooks

The application checks request bindings locally; no payment signature covers
these bindings in this simulation. Arbitrary JSON extensions must not be treated
as authenticated when a real SDK is connected. An incoming charge for work and
an outgoing compute purchase are distinct payment directions. Prepayment is
not retrospectively made contingent on scientific correctness.

The adapter's reconciliation cache is not durable. With a fresh process, absence
of an outcome means unknown, not failed. The journal refuses blind resubmission.
Future adapter work must declare actual signature coverage, idempotency and
finality guarantees, network/asset/recipient binding, credential handling and
reconciliation behavior before claiming stronger settlement capabilities.
