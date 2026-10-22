# Native space protocol session — checkpoint

Order: `~/CLAUDE_CODE_METACOIN_NATIVE_SPACE_PROTOCOL_ORDER_2026-10-22.md` (replaces the lunar-machine-economy order).
Started: 2026-10-22T09:33:03Z (see `START`). Base: `0ded29d` (evidence/acquisition-assurance records commit; source candidate 7159389) — `artifacts/BASE_REVISION.txt`.
Branch: `protocol/native-space-settlement-v0`.

## Commits so far
- `6986181` native core: protocol module, migration 042, native service (domain, actors, fixtures, signed idempotent transfers, obligations, refunds, audit), routes, role `actor`.
- (this checkpoint) economy hooks: native assets accepted in terms/offers; awards reserve the payer's native funds; acceptance creates native obligations (provider, fee, verifier); payment intents on rail `native` settle from the reservation with a signed intent, commit sequence and receipt; treasury view/allocate on native assets; `METACOIN_X402_DISABLED=1` switch; capabilities report `native` and `x402.required_by_native_core: false`.

## Verified (disposable instance, simulation mode, x402 disabled)
- `metacoin_service.tests.test_native_protocol`: 80-unit loop (§14) — 35/32/9/3/1 after the treasury award; 80 conserved; purchase before earning refused (`insufficient_available`); treasury award beyond revenue refused; 6 settlements with commit sequences 1..6, receipts, finality `domain_local_settlement`; journal replay consistent.
- Regression: test_work_money 4, test_work_board 5, test_work_terms 6, test_work_evidence 7 — all OK.

## Running processes
- Live service (tmux `metacoin-service`, pids 2827392/2827403, 127.0.0.1:8402) at 3e34126/schema 040 — untouched, NOT the baseline, not upgraded.
- No background jobs from this session.

## Next action
§7–§9 domains/allocations/envelopes/adapters (two configured domains), then §16 disputes/rotation/recovery, §11 lunar products, §12–§13, §17 interfaces, §18 checker/vectors, §19 forty journeys, §20–§24.
