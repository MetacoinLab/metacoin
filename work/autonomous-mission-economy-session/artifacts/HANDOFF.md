# Handoff — MetaCoin autonomous mission economy (order 2026-10-23)

## What exists
On the native settlement core of the previous order: an operator-controlled signer (separate executable; the service holds public identities only), a commit-checkpoint witness, envelope bundle v1 with fragmentation and economic ids, subdivided exports with claim lineage and irrevocable retirement, a native resource reservation market (compute, storage, contact; measured deliveries with three evidence labels), mission contracts (versioned graphs, frozen revisions, authorized amendments, three executable templates), a bounded deterministic mission controller (explicit actions, validator, earn-then-buy, blackout waits, revocation), a bounded planner (baseline, branch-and-bound with bound/timeout/status, exhaustive oracle, witnesses), a portable standard-library provider client with offline receipt/bundle verification and versioned conformance fixtures, sealed mission bundles, an operator screen, one-command local deployment, and a two-host document. x402 stays disabled (`METACOIN_X402_DISABLED=1`), the facilitator URL closed; no native path imports it.

## One command sequence
See `CLEAN_START.md` (verified from the source archive by `clean_export.sh`; results in `clean-export-logs/`).

## Interfaces
`docs/INTERFACES.md` (API routes, CLI commands, console pages, MCP tools with analysis vs spending), `openapi.json`, `mcp-schema.json`.

## Where the records are
`work/autonomous-mission-economy-session/` (SESSION_STATE.md, artifacts/journeys/journeys-mission.json, artifacts/demonstration/demonstration.json, artifacts/shots/*.png, artifacts/benchmarks/*.json, artifacts/logs/*, FEATURE_LEDGER.json, MIGRATIONS.json, MIGRATION_NOTES.md, ISSUES.md, bpv7-probe.json); `docs/native/` (PROTOCOL.md §11, ARCHITECTURE_AND_TRUST.md, INTERFACES.md, TWO_HOST.md); `provider_client/`.

## Not done / limits (honest)
- Live service not upgraded (3e34126 / schema 040); the operation is in MIGRATION_NOTES.md. Branch not pushed; nothing published or merged.
- Two-host configuration documented, exercised on one host only; BPv7 transport unverified (no installable daemon; `bpv7-probe.json`).
- Same operator runs both domains, the signers and the witness: operator-controlled custody, not independent validators; equivocation detected where two histories meet, never prevented.
- Mission payments and the machine's purchases run under development custody in the demonstration; external signer custody is shown for the supplier and for the client-enrolled provider.
- Planner: capacity per offer window (no per-slot capacity), thermal not modelled, energy a declared assumption; optimality only when the search exhausts.
- Market: fixed-price offers only; returns full-and-unspent only; `pip install` of the client verified only where setuptools is available (the archive run used the package directory).
- Optional extensions of §16 not built beyond what the required groups cover.
