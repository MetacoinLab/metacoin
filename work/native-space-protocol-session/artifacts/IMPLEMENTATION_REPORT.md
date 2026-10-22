# Implementation report — MetaCoin native space-work and settlement protocol (order 2026-10-22)

## The demonstrated native economic cycle (journey 21, real processes, x402 disabled, facilitator blocked)

Requester fixture-funded with 80 units of the zero-value development asset; every other account started at zero. A real route-feasibility task on the Apollo 11 LOLA LDEM_4 extract was computed by the worker, verified by the independent lunar reference, accepted under frozen terms and settled natively: machine 40, verifier 3, treasury 2 (fees additional to the 40), requester kept 35. Before earning, the machine's purchase was refused (`400`). The machine then bought a distinct computation for 10 from its earned balance (supplier 9, treasury 1), and the treasury reserved and paid a 2-unit award through ordinary work acceptance. Final balances {"r80": 35, "m80": 32, "s80": 9, "v80": 3, "treasury": 1}, total 80. Authority consumed: 45 reserved then released to 35 on the requester; 10 on the machine; 2 on the treasury. Nothing minted; audit conserved.

Across domains (journeys 8–20): exports locked backing before any certificate; imports credited once (duplicates idempotent, acks replayed); a lost acknowledgment left the backing locked (no refund, no re-export); every export/import crash boundary recovered without duplicated ownership (held across both domains = fixture: True); preallocated authority supported bounded spending across a cut link with file bundles; a full unspent return closed through the reverse protocol and a partial return was refused; a conflicting signed history quarantined the peer; a backup restore entered quarantine (`quarantine`) and left it only after the peer's signed observation reconstructed the missing allocation as locked backing.

## Revisions and time
- Base: `0ded29d90706d4e5aafbd2e59b6d029d66d08702` (evidence/acquisition-assurance records commit; source candidate 7159389)
- Source candidate: `35d194ac18c053212828870a563a8c523c3535c3` on branch `protocol/native-space-settlement-v0` (not pushed; GitHub and live main unchanged)
- Loaded revision in the journeys: `35d194ac18c053212828870a563a8c523c3535c3`; live service: 3e34126 / schema 040, untouched
- Started 2026-10-22T09:33:03Z; this report built at 2026-10-22T11:31:33Z

## Verification
- Forty journeys (`journeys/journeys-native-final.json`): 40 passed, 0 failed, 0 blocked (none).
- Unit suites on the candidate: all OK.
- Browser (journey 38): 13 checks passed, screenshots in `browser-screenshots/`.
- Benchmark (`benchmarks/native-benchmark.json`): 300 concurrent transfers p50 0.021s p95 0.343s, 87/s, all correct; native vs application-journal settlement on identical workloads: pay p50 0.056s vs 0.057s, awards accepted 6/6 vs 6/6; journal replay 0.081s consistent; storage 4468736 bytes.
- Clean export (journey 39): 15 passed / 0 failed on archive 7c668cc872b3ab6b.

## What settled where
- Locally (domain-local settlement): every work payment and transfer in both domains carries a commit sequence and an authority receipt.
- Across domains: allocations acknowledged after the courier round trip (reconciled inter-domain settlement); at the end of the run the audit of domain A reported backing 25 against domain B claims 24 (pending backing not yet imported: 1).
- Treasury: funded only by settled fees; awards reserve from the treasury account; over-allocation and duplicate allocation refused (journey 31).

## Required failures, skips and caveats
- Journey 39 is recorded from the clean export run, not inside the main run.
- Same operator and host for both domains; development keys service-custodied; equivocation detected not prevented; rollback detection needs the watermark file or a peer observation; returns full-and-unspent only; application-level transport (not BPv7).
- Lunar products are grid-scale (7.58 km/pixel); tiles ≤ 64×64; no GPU path; evidence-acquisition deliveries are simulated.
- Live service not upgraded; branch not pushed.
## Remaining extensions (§22)
None of the ten prioritized extensions was started; the required groups consumed the session.
