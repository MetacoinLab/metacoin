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
- [ ] A · [ ] B · [ ] C · [ ] D · [ ] E · [ ] F · [ ] G · [ ] H · [ ] 36 journeys · [ ] docs/bench/migration/packaging
