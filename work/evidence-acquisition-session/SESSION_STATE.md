# Evidence acquisition & scoped assurance session (order of 2026-10-22)

- Order: ~/CLAUDE_CODE_IMPLEMENTATION_ORDER.md (identical to the copy inside ~/metacoin-evidence-research-2026-10-22.zip; SHA256SUMS verified OK, 10 files)
- Reference kernels: 15/15 tests pass (python3 -m unittest test_reference in the extracted handoff)
- Starting state: branch service/real-features @ 825adf8 (== origin/service/real-features; origin/main 854619a is the merge of it), working tree clean, no stash
- Live service: tmux `metacoin-service` (windows api, worker), 127.0.0.1:8402, loaded revision 3e34126, schema 040_work_programs_pricing_challenges, provider-mode test-http
- Development branch: evidence/acquisition-assurance (from 825adf8)
- Interpreters: .venv-service (py3.12, no numpy); /usr/bin/python3 (numpy 2.2.6, torch 2.10 cu130, cuda available) for compute child
- Existing overlapping implementation: none for decision observability / sampling plans / assurance; existing verification.py (classes, challenges), economy/acceptance.py (VERIFICATION_RANK), evaluation.py (suites), design.py (D-optimal design) are the extension points.
- Migration ids free from 041.

## Progress ledger (update after each vertical slice)
- [x] A kernels + independent checker + tests (commit cf17402, 06:35Z)
- [x] Service layer, routes, migration 041, policy schema v2 / legacy v1 semantics, sampling plans, assurance predicate, portfolios + fault campaigns, offline replay tool (commit 3b559a6; test_decision_kernels 22 + test_decision_service 10 green; test_work_terms + test_work_evidence green)
- Interruption: session 9df2f649 stopped 07:01Z mid test run (login/session loss); recovered 07:5xZ by this session from the uncommitted tree; quote-expiry / lineage / terms / evidence regressions were already fixed in the tree at recovery — re-verified, then committed
- [x] CLI commands, MCP tools, console pages + award assurance/delivery sections; sampling-plan route tests (journeys 11-16) and interface tests green (commit: interfaces checkpoint, 08:3xZ)
- [x] Journey harness tests/journeys_decision.py: run1 24/24 API journeys passed (24 browser, 26 clean export recorded separately); browser script written; docs (artifacts/docs), MIGRATIONS.json, FEATURE_LEDGER.json, ARCHITECTURE.md written (commit: journeys checkpoint)
- [x] Browser journey 19/19 checks (commit 7159389 = source candidate)
- [x] Example bundle + offline replay (exit 0) + fixture results; docs; MIGRATIONS; FEATURE_LEDGER; ARCHITECTURE; HANDOFF; DELIVERY_INDEX
- [x] At source candidate 7159389: unit suite 287 OK (13 skipped), science 18 OK, chain 7 OK, journeys 26/26 (browser 19/19), journey 26 clean export rc=0 on archive 3adba626…, patch check equal, privacy scan 0 hits; delivery ~/metacoin-evidence-acquisition-delivery-2026-10-22 (61+ files); records commit follows (2026-10-22T08:38:34Z)

## Open items
- optional extensions 1-6 not started; live service not upgraded (stays 3e34126 / schema 040); branch not pushed
- [ ] Delivery directory + example bundle + replay command + handoff; inherited suites rerun
