# Native space protocol session — checkpoint

Order: `~/CLAUDE_CODE_METACOIN_NATIVE_SPACE_PROTOCOL_ORDER_2026-10-22.md` (replaces the lunar-machine-economy order).
Started: 2026-10-22T09:33:03Z (`START`). Base: `0ded29d` (evidence/acquisition-assurance records commit; source candidate 7159389) — `artifacts/BASE_REVISION.txt`.
Branch: `protocol/native-space-settlement-v0` (local, not pushed).

## Commits
- `6986181` native core (protocol, migration 042, service, routes, actor role)
- `a0e6583` economy hooks (native assets in terms/offers, award reservations, obligations, native rail, treasury, x402-disabled switch, 80-unit loop test)
- `a34e895` inter-domain layer (peers, allocations, envelopes, courier, tombstones, rotation, quarantine, migration 043) + 9 two-domain tests
- `a119fff` lunar products (terrain tile, route feasibility, scenario pack; LOLA LDEM_4 extracts; reference; integration) + lunar journeys
- `7f47de2` disputes, procurement, evidence bridge, checker/vectors/model, export, console/MCP/CLI + tests
- (working tree) journeys harness, browser script, benchmark, docs, packaging scripts, evidence-field split for large lunar results, agent native operations — to be committed after run 2

## Verified so far (disposable instances, simulation mode, x402 disabled)
- Unit suites green at 7f47de2: test_native_protocol 2, test_native_domains 9, test_native_lunar 3, test_native_conformance 5, test_native_model 21, test_native_interfaces 9, test_native_evidence 8; inherited test_work_money/board/terms/evidence, test_decision_service, test_service green.
- Journeys run 1 (`artifacts/journeys/journeys-native-run1.json`): 17 passed / 23 failed / 1 blocked — failures were harness defects (cumulative balances, alice funding, j19 cleanup leaving quarantine, grant policy shape, migrations column) and one product defect (lunar results > 64 KiB evidence field → fixed by splitting grids into bounded fields). Run 2 in progress.

## Running processes owned by this session
- Background: journeys harness run 2 (two API + two worker processes in temp homes; self-terminating), unit suites run. No tmux use.
- Live service (tmux `metacoin-service`, pids 2827392/2827403, 127.0.0.1:8402) at 3e34126/schema 040 — untouched, not the baseline, not upgraded.

## Next action
Read run 2; fix remaining journey defects; commit; benchmark (`metacoin_service.benchmark_native`) on a harness instance; package (`package.sh`), clean export (`clean_export.sh`, journey 39), FEATURE_LEDGER, HANDOFF, final report. Extensions after that if time remains.
