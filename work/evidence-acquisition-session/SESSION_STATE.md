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
- [ ] A kernels + tests
