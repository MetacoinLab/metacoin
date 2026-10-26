# Starter kit: an independently written provider and receipt consumer (order 2026-10-23 §21 extension 7)

Two small programs that talk to a MetaCoin settlement domain through the **public API only**, written without importing the
service or the `metacoin_provider` package, so that a third party can check the protocol against a second implementation:

- `minimal_provider.py` — publish a resource offer, poll for reservations, activate, run a local executor, deliver, read earnings.
  Needs: a provider credential file (0600, `{"token": ...}` obtained by the domain administrator through `metacoin-provider enroll`
  or `POST /native/actors/enroll`), the base URL (https beyond loopback with `--ca`), an executor command.
- `receipt_consumer.py` — fetch receipts and the export, verify every receipt signature under roots **you** pinned (never a key that
  arrived with the receipt), re-derive the pool overlap, lot partition and obligation totals from the export, print a verdict.
  Signature verification uses the `cryptography` library when present (an established implementation); otherwise it reports
  `unverified` rather than silently trusting (`--allow-pure-python` delegates to `metacoin_provider.wire`, the pure-Python verifier
  shipped with the client, as an explicitly chosen fallback).
- `compat_test.py` — runs the versioned conformance vectors (`metacoin_provider/conformance/v1/fixtures.json`, schema
  `metacoin-provider-conformance/v2`) through this kit's own canonical encoding and verifier: canonical bytes, digests, signature
  verdicts and the v2 pool / lot rules must agree with the fixtures. Exit code 0 only when every case agrees.

Minimal installation: Python 3.10+, no third-party package required (`cryptography` optional for receipt verification). Copy this
directory anywhere; run `python compat_test.py` first.
