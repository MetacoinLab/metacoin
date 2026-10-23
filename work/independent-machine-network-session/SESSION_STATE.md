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
- [x] 50-journey harness `tests/journeys_network.py` (+ browser script, demonstration on dom-c with four signers): shakedown run full1 39/9/3; fixes applied; full2 running (artifacts/journeys/journeys-network-full2.json)
- [ ] endurance ≥30 min (`endurance_network.py`, smoke 90 s OK after fixes) — run after full2
- [ ] benchmarks before/after (`run_benchmark.py`) — run after endurance
- [ ] docs (PROTOCOL §12, ARCHITECTURE, INTERFACES, TWO_HOST appended) · artifacts: TRUST_MODEL, MIGRATION_NOTES, CLEAN_START, HANDOFF, DELIVERY_INDEX, EXTERNAL_TRACKS, IMPLEMENTATION_REPORT, FEATURE_LEDGER (ledger_build.py), IMPLEMENTATION_INDEX
- [ ] packaging (package.sh, clean_export.sh → journey 49), SHA256SUMS on fresh extraction, candidate record, records commit
- [ ] §21 extensions (after required work)

## Owned processes
Journey/endurance/benchmark runs spawn their own API/worker/signer/witness processes in temp dirs and stop them; nothing of mine runs persistently. The leftover `--provider-mode test-http serve` processes from 2026-09-28 are not mine.

## Exact next action
Wait for full2 → fix any failure → rerun → endurance 30 min (background) → benchmarks → ledger/report → package → clean export (journey 49) → journey 50 with the feature ledger → records commit → final report.
