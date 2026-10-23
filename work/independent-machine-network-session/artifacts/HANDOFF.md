# Handoff — MetaCoin independent machine network (order 2026-10-23)

## What exists
On the autonomous mission economy baseline (a6d672d): complete custody separation (context-bound signing requests, per-actor
signer policy, development custody disable, no fallback), an installable provider distribution with pinned identity and
independence disclosure, canonical resource pools with interval admission inside the reservation transaction, native
sub-contracting (prefund or settled earnings), versioned reusable products with provenance and idempotent entitlements, a two-hop
paid relay with separate evidence and bounded fees, partial return of claims over explicit lots (v2 messages refused by v1 peers),
fenced controller generations with a restart-safe runner, a portfolio planner with a liquidity split and an independent validator,
connected console / API / CLI / MCP surfaces, a bounded network model with replay tests, conformance vectors v2, a 50-journey
harness with the custody-separated demonstration, endurance and benchmark scripts, migrations 048–054 rehearsed on prior-state copies.

## One command sequence
`CLEAN_START.md` (verified from the source archive by `clean_export.sh`; results in `clean-export-logs/`). Developer examples with
executed commands and responses: `examples/DEVELOPER_EXAMPLES.md`.

## Where the records are
`work/independent-machine-network-session/` (SESSION_STATE.md, artifacts/journeys/journeys-network.json, artifacts/demonstration/,
artifacts/shots/, artifacts/benchmarks/, artifacts/logs/, FEATURE_LEDGER.json, IMPLEMENTATION_INDEX.md, MIGRATIONS.json,
verification-results.json, examples/). Delivery directory: see DELIVERY_INDEX.md.

## Not done / limits (honest)
- Live service not upgraded (3e34126 / schema 040); the tested operator upgrade sequence is in MIGRATION_NOTES.md. Branch not pushed; nothing published or merged (PR #1 authorization does not cover this branch).
- External tracks blocked, never counted as passed: real second host / separate operator (configuration generator only), separate OS identities (not performed), BPv7 (no daemon; the relay is not BPv7). See EXTERNAL_TRACKS.md.
- Same operator runs every domain, every signer and the witness; signers are process-separated on one host (weaker than host separation); equivocation is detected where two histories meet, never prevented.
- Cross-domain exports remain a development-custody action in this order (the claim in the demonstration is exported by a development-custody account at dom-a); transferred lots are not retirable by the transferee (they stay backed until re-exported).
- Legacy offers created before migration 048 keep per-offer capacity semantics; legacy consumed claims get no lineage (full return only).
- Planner optimality only where the oracle exhausts (≤3 missions, bounded fixtures); synthetic observations are labelled; the Apollo 11 tile is a grid-scale demonstration.
- `pip install` of the provider client needs setuptools; the archive run uses the package directory on PYTHONPATH.
- One SQLite writer per domain: under saturation bursts a request can be refused `503 JOURNAL_BUSY` (Retry-After; nothing begun; repeat the identical request). Transactions of one process queue FIFO, but API and worker processes still contend through SQLite's busy handler; the throughput ceiling of one writer on one file is unchanged (see ARCHITECTURE_AND_TRUST.md "One writer under saturation", ISSUES.md "Endurance defects", `benchmarks/endurance-network.json` → `busy`).

## §21 extensions — status (see FEATURE_LEDGER.json `extensions` and IMPLEMENTATION_REPORT.md)
See the "Extensions" section of IMPLEMENTATION_REPORT.md for what was completed, started or untouched after the required work.
Completed as vertical slices: 1 bidding, 2 substitution, 3 route planning, 4 subscriptions, 5 offline console, 7 starter kit, 8 scenarios (unit tests `tests/test_native_extensions.py`, journeys 51–56 with `--extensions`, docs PROTOCOL §12.9). 6 (second host / BPv7) stays blocked. Not built: console pages for the extensions (API, CLI-equivalent and MCP read tools exist).
