# Autonomous mission economy session — checkpoint

Order: `~/metacoin-autonomous-mission-economy-2026-10-23.zip` → CLAUDE_NEXT_ORDER.md (SHA256SUMS OK). Started 2026-10-23T02:13:24Z.
Branch: `feature/autonomous-mission-economy-v1` from `6a9b592` (pointer commit over records `c5263df`, source candidate `35d194a`, base of that order `0ded29d`). Working tree was clean; no stash; no other process editing the checkout.
Live service: tmux `metacoin-service`, pids 2827392/2827403, 127.0.0.1:8402, loaded `3e34126` / schema 040 — not the baseline, not upgraded.
Next free migration id: 044.

## Six-actor audit (§2)
The previous demonstration (journey 21 of the native order) awarded the treasury-funded 2-unit contract to the machine provider on purpose (`A.award('owner', 'm80', ..., funding='treasury')`): the machine ended at 32 because it was also the award recipient. Nothing was minted or mis-posted (total 80, audit conserved, journal consistent). The order's six-account table needs a distinct award recipient; this order adds that replay (journey 1) and keeps the previous record untouched.

## Reuse map
- Actors/accounts/grants: `native/service.py` (actor role, accounts with categories, agents.guard with native:transfer), `agents.py` grants.
- Reservations/obligations/journal: `native/ledger.py` on `economy/journal.py`; `native_obligations`; work award reservation hooks.
- Evidence/assurance/workers/contracts: economy terms→offers→award→worker→verification→acceptance; decision package; lunar products.
- Cross-domain: `native/domains.py` allocations, envelopes, courier; checker/vectors/model.
- Metering: `metering.record_for_job` (actual bounded execution measurements).
One authoritative monetary state: the journal. The planner, controller and market never introduce a second balance.

## Plan (groups → migrations)
A signer (separate executable, `native/signer.py`; service stores public keys only) · B resource market (044) · C missions (045) · D controller · E planner · F allocation subdivision/lineage (046) · G envelope v1 + witness checkpoints (046) · H provider client + fixtures + deploy.

## Progress
- [x] A (7c79273) · [x] B, F, G (22da130) · [x] C, D (12f2df0) · [x] E (3338009) · [x] H (f00ca39) · [x] operator screen (8d5b5f4) · [x] 36 journeys + §12 demo (abd1e5e, 809b95e: 36 passed / 0 failed / 1 blocked pending clean export) · [x] browser (journey 35, 12 checks) · [x] migration rehearsal (journey 36 part A on a live-DB copy) · [x] benchmarks before/after + mission + endurance · [x] docs/ledger · [ ] packaging + clean export + j36 + SHA256SUMS
Migrations added: 044_native_account_keys, 045_claim_lineage_checkpoints_fragments, 046_resource_market, 047_mission_contracts (next free: 048).
Unit suites green at f00ca39: native_signer 4, native_lineage 20, native_witness 22, native_market 5, native_missions 7, native_planner 4, native_provider_client 2, client conformance 22 cases, decision_service 10, agent_paid 1, native_protocol 2, native_domains 9.
Owned processes: none left running (deploy_local test torn down). Live service untouched.
Source candidate: 809b95e. Next action: records commit → package.sh → clean_export.sh → journey 36 rerun with METACOIN_MISSION_CLEAN_EXPORT_JSON → merge → final commit → SHA256SUMS on a fresh extraction → report.
