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
- [ ] Browser journey script (journey 24)
- [ ] Journey harness (26 journeys incl. browser 24, legacy 25, clean export 26) against a running instance
- [ ] Docs generated from behaviour (OpenAPI, CLI, MCP), migration/rollback notes, feature ledger, architecture note
- [ ] Delivery directory + example bundle + replay command + handoff; inherited suites rerun
