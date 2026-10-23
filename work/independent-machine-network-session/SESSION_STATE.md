# Independent machine network session — checkpoint

Order: `~/metacoin-independent-machine-network-2026-10-23.zip` → CLAUDE_NEXT_ORDER.md (SHA256SUMS OK; 23 sections, groups A–J, 50 journeys). Started 2026-10-23T04:51Z.
Branch: `feature/independent-machine-network-v1` from `a6d672d` (trailing log commit over pointer 810aaaa / records 1a3f683 / source candidate 809b95e).
Live service: tmux `metacoin-service`, 127.0.0.1:8402, loaded `3e34126` / schema 040 — not the base, not upgraded, not restarted.
Migrations used: 048 resource_pools, 049 signing_requests, 050 mission_subcontracts, 051 claim_lots, 052 mission_products, 053 relay_service, 054 controller_generations. Next free id: 055.

## Commits on the branch
5f4c601 session start · 9799307 C pools · 5b65144 A custody · c157063 D subcontracts · e8b3b59 G lots · d38456d E products · 24779b9 F relay · d3573ac H continuity · b87c178 I portfolio · 6313e97 B onboarding + J console/MCP + pool-aware planner + vectors v2 · 2e5158f harness/model/endurance/benchmark/hostconfig + accounting fixes · (next) docs + packaging scripts.

## Limitations verified against code (before changes)
- Verified: mission payments ran money.prepare/authorize/submit with service-held keys; market.reserve bounded capacity per offer only; return_request refused any consumed claim; no controller generation existed. All changed in this order.

## Progress
- [x] A B C D E F G H I J implemented with unit suites (all OK at 2e5158f)
- [x] §17 model (model_network.py: 340 states, 0 violations; replay tests), checker + client verifier v2, conformance fixtures v2 (32 cases)
- [x] 50-journey harness `tests/journeys_network.py` (+ browser script, demonstration on dom-c with four signers): full1 39/9/3 → full2 47/1/3 (sequence-number defect found by the demonstration, fixed) → full3 47/1/3 (reconnection check) → **full4 48/0/3 at 1b396f3** (48/49/50 blocked only on inputs produced later)
- [~] endurance 30 min running since 07:40:45Z (`artifacts/benchmarks/endurance-network.json`, progress ticks; no unexpected failures at 660 s)
- [ ] benchmarks before/after (`run_benchmark.py`) — run after endurance
- [ ] docs (PROTOCOL §12, ARCHITECTURE, INTERFACES, TWO_HOST appended) · artifacts: TRUST_MODEL, MIGRATION_NOTES, CLEAN_START, HANDOFF, DELIVERY_INDEX, EXTERNAL_TRACKS, IMPLEMENTATION_REPORT, FEATURE_LEDGER (ledger_build.py), IMPLEMENTATION_INDEX
- [ ] packaging (package.sh, clean_export.sh → journey 49), SHA256SUMS on fresh extraction, candidate record, records commit
- [~] §21 extensions: modules written (bidding, substitution, route_planning, subscriptions, offline_console, scenarios, starter_kit) + tests + journeys 51–56; WIRING of controller/mission_contracts/routes/api/mcp waits for the endurance run to end (imported modules must not change while its API restarts) — script scratchpad/wire_extensions.py

## Owned processes
Journey/endurance/benchmark runs spawn their own API/worker/signer/witness processes in temp dirs and stop them; nothing of mine runs persistently. The leftover `--provider-mode test-http serve` processes from 2026-09-28 are not mine.

## Exact next action
When the endurance run ends (~08:11Z): apply wire_extensions.py → run test_native_extensions + affected suites → commit → full5 with --extensions → run_benchmark.py → ledger/report → package.sh → clean_export.sh (journey 49) → rerun 48,49,50 with the result files → merge into journeys-network.json → CANDIDATE.json + RECORDS_COMMIT → final report.
