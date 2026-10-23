# Independent machine network session — checkpoint

Order: `~/metacoin-independent-machine-network-2026-10-23.zip` → CLAUDE_NEXT_ORDER.md (SHA256SUMS OK; 23 sections, groups A–J, 50 journeys). Started 2026-10-23T04:51Z.
Branch: `feature/independent-machine-network-v1` from `a6d672d` (trailing log commit over pointer 810aaaa / records 1a3f683 / source candidate 809b95e).
Live service: tmux `metacoin-service`, 127.0.0.1:8402, loaded `3e34126` / schema 040 — not the base, not upgraded, not restarted.
Migrations used: 048 resource_pools, 049 signing_requests, 050 mission_subcontracts, 051 claim_lots, 052 mission_products, 053 relay_service, 054 controller_generations. Next free id: 055.

## Commits on the branch
4f29752 sequence numbers + docs · 1b396f3 demo check + examples (full4 48/0/3 here) · 53c9d49 §21 extensions (candidate if full5 passes) · 5f4c601 session start · 9799307 C pools · 5b65144 A custody · c157063 D subcontracts · e8b3b59 G lots · d38456d E products · 24779b9 F relay · d3573ac H continuity · b87c178 I portfolio · 6313e97 B onboarding + J console/MCP + pool-aware planner + vectors v2 · 2e5158f harness/model/endurance/benchmark/hostconfig + accounting fixes · (next) docs + packaging scripts.

## Limitations verified against code (before changes)
- Verified: mission payments ran money.prepare/authorize/submit with service-held keys; market.reserve bounded capacity per offer only; return_request refused any consumed claim; no controller generation existed. All changed in this order.

## Progress
- [x] A B C D E F G H I J implemented with unit suites (all OK at 2e5158f)
- [x] §17 model (model_network.py: 340 states, 0 violations; replay tests), checker + client verifier v2, conformance fixtures v2 (32 cases)
- [x] 50-journey harness `tests/journeys_network.py` (+ browser script, demonstration on dom-c with four signers): full1 39/9/3 → full2 47/1/3 (sequence-number defect found by the demonstration, fixed) → full3 47/1/3 (reconnection check) → **full4 48/0/3 at 1b396f3** (48/49/50 blocked only on inputs produced later)
- [~] endurance: run1 at 1b396f3 (10 unexpected: 6 restart-window ConnectError, 4 LOCAL_OPERATION_FAILED uncaptured); run2 at 53c9d49 (`benchmarks/endurance-network-run2-53c9d49.json`: conserved, 0 duplicates, 6 LOCAL_OPERATION_FAILED: 4 pool_reservations, 2 signed_transfers); run3 at 53c9d49 + defects capture (`benchmarks/endurance-network.json` + `.defects-end-a.log`, started 08:55:46Z by the previous session, orphaned when that session died 08:59Z, finished 09:27:59Z rc=1): conserved, 0 duplicates, 10 unexpected = 4 LOCAL_OPERATION_FAILED + 5 execution_incomplete + 1 KeyError, backlog_age 1155 s, BOTH WORKERS offline at 783/787 s.
  DIAGNOSIS (2026-10-23 09:00–09:35Z, recovery session): every LOCAL_OPERATION_FAILED is `sqlite3.OperationalError: database is locked` (BEGIN IMMEDIATE waited the full 15 s busy timeout) MASKED by `Database.tx()` issuing ROLLBACK with no open transaction ("cannot rollback - no transaction is active" replaces the original; classified LOCAL_OPERATION_FAILED 409 instead of JOURNAL_BUSY 503). Reproduced deterministically (scratch script). Not a single long holder: the event log shows commits every second through the 768–784 s window (no quiet gap ≥ 4 s; witness acks ≤ 1 s for all 838 checkpoints; worker ticks ≤ 6 ms; commit ≈ 5 ms) → write-lock starvation under saturation bursts (SQLite's polling busy handler is unfair to long waiters; ~40 API threads + 2 workers contend). The same lock timeout inside the worker loop propagates out of run_forever → worker exits (workers table: offline at 783/787 s) → job backlog.
  FIX PLAN: (1) tx() rolls back only an open transaction and re-raises the original error; (2) JOURNAL_BUSY 503 with Retry-After and an explicit 'nothing begun, retry same request id' detail; (3) in-process fair write gate (bounded) ahead of BEGIN IMMEDIATE; (4) worker survives busy errors (bounded retry/continue); (5) defect log keeps the chained cause; (6) endurance script: busy refusals retried idempotently and reported separately, worker liveness + backlog bound added to `ok`; (7) regression tests; then final 30-min rerun.
- [ ] benchmarks before/after (`run_benchmark.py`) — run after endurance
- [ ] docs (PROTOCOL §12, ARCHITECTURE, INTERFACES, TWO_HOST appended) · artifacts: TRUST_MODEL, MIGRATION_NOTES, CLEAN_START, HANDOFF, DELIVERY_INDEX, EXTERNAL_TRACKS, IMPLEMENTATION_REPORT, FEATURE_LEDGER (ledger_build.py), IMPLEMENTATION_INDEX
- [ ] packaging (package.sh, clean_export.sh → journey 49), SHA256SUMS on fresh extraction, candidate record, records commit
- [x] §21 extensions 1,2,3,4,5,7,8 wired and tested (test_native_extensions 10/10) at 53c9d49; journeys 51–56 in full5 (running); 6 blocked (external)

## Owned processes
Journey/endurance/benchmark runs spawn their own API/worker/signer/witness processes in temp dirs and stop them; nothing of mine runs persistently. The leftover `--provider-mode test-http serve` processes from 2026-09-28 are not mine.

## Exact next action
(recovery session, 09:35Z) implement the fix plan above (db.py, api.py, worker.py, endurance_network.py, errors, docs, tests/test_db_contention.py) → run affected unit suites → commit → final 30-min endurance at the new candidate → run_benchmark.py → report/ledger → package.sh → clean_export.sh (journey 49) → rerun 48,49,50 → merge → CANDIDATE.json + RECORDS_COMMIT → final report.
