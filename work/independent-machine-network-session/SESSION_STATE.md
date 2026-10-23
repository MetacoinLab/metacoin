# Independent machine network session — checkpoint

Order: `~/metacoin-independent-machine-network-2026-10-23.zip` → CLAUDE_NEXT_ORDER.md (SHA256SUMS OK; 23 sections, groups A–J, 50 journeys). Started 2026-10-23T04:51Z.
Branch: `feature/independent-machine-network-v1` from `a6d672d` (trailing log commit over pointer 810aaaa / records 1a3f683 / source candidate 809b95e). Working tree was clean; no stash; no other process editing the checkout (only the live service tmux session, untouched).
Live service: tmux `metacoin-service`, 127.0.0.1:8402, loaded `3e34126` / schema 040 — not the base, not upgraded.
Next free migration id: 052 (048 pools, 049 signing requests, 050 subcontracts, 051 claim lots used).

## Limitations verified against code (before changes)
- Verified: mission payments ran money.prepare/authorize/submit with service-held keys (controller 'pay'); market.reserve bounded capacity per offer only; return_request refused any consumed claim (return_partial_unsupported); no controller generation existed. All four changed in this order (groups A, C, G, H).

## Plan (groups → migrations)
A custody separation (signer policy, no fallback) · B provider distribution/onboarding · C shared pools (048) · D subcontracts (049) · E mission products (050) · F paid relay (051) · G partial return with claim lots (052) · H fenced continuity (053) · I portfolio planner · J console workflow.

## Progress
- [x] C (9799307: pools 048) · [x] A (5b65144: signer policy, signing requests 049) · [x] D (c157063: subcontracts 050) · [x] G (claim lots 051, partial returns v2) · [ ] B · [ ] E · [ ] F · [ ] H · [ ] I · [ ] J · [ ] 50 journeys · [ ] model/checker/vectors · [ ] endurance ≥30 min · [ ] migration rehearsal · [ ] packaging
