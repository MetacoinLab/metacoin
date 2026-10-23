# Native space-work and settlement system — architecture and trust model (order 2026-10-22)

## Dependency direction (enforced)
```
native/protocol.py (types, canonical bytes, published transition tables)
   └── native/ledger.py (categories on the authoritative journal)        ──┐
        └── native/service.py (domain, actors, fixtures, intents, obligations, refunds, audit, disputes, work hooks)
             └── native/domains.py (peers, allocations, envelopes, inbox/outbox, rotation, recovery)
economy/* (terms, board, acceptance, money, treasury) call INTO native for native assets; native never imports economy routes or any adapter.
Interfaces: native/routes.py, lunar/routes.py, console_ui.py pages, mcp_server.py tools, client_cli.py commands.
Optional Earth adapters: metacoin_service/x402_http.py, buyer.py, integrations/x402 — never imported by native/*, lunar/*, decision/* (tests/test_native_conformance.py asserts it; METACOIN_X402_DISABLED=1 proves the core runs without them).
```

## Single fenced writer
One SQLite database per domain, `BEGIN IMMEDIATE` transactions, one API process per domain. Domain commit sequence (`native_domains.commit_sequence`) orders every economic transition; receipts carry it. Workers use lease fencing (unchanged).

## What crosses domains
Only signed, bounded envelopes carrying allocation certificates, import acknowledgments, return requests/certificates, rotation notices and observations. Trust roots are pinned by the operator (`POST /native/peers`) or rotated by a notice signed by the retiring key; nothing in a bundle can add or change a root.

## Crash boundaries
Export: reserve → export commit → certificate+outbox, each its own durable step; `POST /native/allocations/recover` resumes from state. Import: credit and acknowledgment share one transaction (a crash before the ack is queued leaves no credit and no tombstone). Return: initiation (claim nonspendable) and completion (backing unlocked) are separate durable steps. Fault points: `native_reserve native_commit native_response native_export_reserve native_export_commit native_certificate_publish native_import_commit native_ack native_return_init native_return_complete` (disposable instances only).

## Recovery and quarantine
A watermark file outside the database (`keys_dir/native/watermark-<domain>.json`) records the last committed sequence at every inter-domain commitment. A database whose sequence is below it enters `quarantine`: exports, settlements and transfers are refused until `POST /native/recovery/reconcile` finds every allocation a peer observed (signed `observation` messages) locally — missing exported allocations can be reconstructed as locked backing only (never as spendable funds). Detected: rollback below the watermark, conflicting signed histories per allocation id, two allocations with one commit sequence, certificates addressed elsewhere, replayed ids. Not detected: a rollback that also restores the watermark and has no peer; equivocation is detected where two histories meet, never prevented.

## Custody
Actor keys are development keys held by the service on the actor's behalf (0600 files under `keys_dir/native`): not operator custody, not independent signers. Both domains run under the same operator on the same host: replicated processes are not independent validators. The operator-owned signer (extension 1) is not built.

## Treasury
The MetaStar treasury is a native account funded only by protocol fees settled into it; awards reserve from that account (`treasury.allocate` → available→reserved) and are paid through ordinary work acceptance. Available = confirmed fee receipts − commitments − spending. No path reaches base issuance; identity and monetary files are digest-checked.

## Lunar products
`lunar/terrain.py`, `route.py`, `scenario.py` are pure Python; `reference.py` is the independent CPU reference used by the `analytical` verification class and by the portable verifier; `full_exact` is a same-implementation regression check and is labelled so. Real data: NASA PDS LOLA LDEM_4 extracts with the full-image digests and label; synthetic terrain is labelled SYNTHETIC end to end.

## Order 2026-10-23 additions

### Custody (updated)
Three custody classes coexist: development custody (service-held actor keys, labelled `development`), **external signer custody** (`external-signer:<id>`: the operator-controlled signer process holds the key; the service holds the public identity, verifies signatures and refuses unsigned spending with `signature_required`), and authority custody (the domain key). The signer is a separate executable with its own token and store; it is still operated by the same operator in this session, so it is operator-controlled custody, not an independent validator.

### Witness
A commit-checkpoint witness (separate process, pinned identity, freshness policy) gives rollback detection that does not depend on a file next to the database. It is run by the same operator here; it detects a stale restore and refuses contradictory checkpoints, and it cannot prevent an authority from signing two histories.

### What the controller is and is not
The mission controller is deterministic: it enumerates explicit actions from recorded state, validates each against recorded constraints and performs at most one per step. An LLM may propose; the validator decides; nothing in the economic path depends on a model output. Blackout behaviour is a consequence of the link state recorded in the domain, not a timer.

### Provider client boundary
`metacoin_provider` talks to the public API only, verifies receipts and bundles under roots it pins itself, and never holds a private key (payments go through a signer the provider's operator runs). Its wire format is pinned to the service's by versioned conformance fixtures.

### Two hosts
Documented in `docs/native/TWO_HOST.md`; exercised on one host only. BPv7 (RFC 9171) transport: no installable daemon was available in this environment; the envelope/bundle layer is transport-agnostic and a BPv7 adapter remains unverified.
