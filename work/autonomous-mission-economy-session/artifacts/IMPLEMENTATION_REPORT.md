# Implementation report — MetaCoin autonomous mission economy (order 2026-10-23)

## The demonstrated machine economic behaviour (demonstration record, real processes, two domains, signers, x402 disabled and facilitator closed)

Funding table: {"d_req": 5000, "d_mach": 0, "d_sup": 0, "d_ver": 0, "d_con": 0, "treasury": 7, "dom-b:d_mach_b": 0, "dom-b:d_sup_b": 0} (zero-value development asset; only the requester fixture-funded).
1. The requester's frozen mission (terrain analysis on the pinned Apollo 11 LOLA LDEM_4 extract; revision digest recorded; linked input root) was executed by the machine provider through the background worker: acquisition accepted and settled (4), route feasibility computed and verified by the independent lunar reference (FEASIBLE).
2. **Blackout**: the link was cut before settlement. The controller still settled the accepted work locally from already reserved funds (provider 40, fee 2, verifier 3; one validated action per step), while the machine's export to dom-b was certified but the online transport was refused with `link_cut` (status 409). The operator screen listed what stayed possible and what was disabled, with reasons.
3. The machine bought follow-on compute from its **settled** earnings (20 cpu_ms from the supplier; measured delivery, verified product, settled once) still inside the blackout; before settlement the same purchase is refused (`insufficient_liquidity … receivables are not spendable`, journey 17).
4. **Outage**: the first contact window passed without the provider activating; the controller released the reservation in full (no payment), the planner replanned to the next window, the relay was delivered and settled exactly once (ledger spent/released recorded).
5. **Reconnection**: the certificate was delivered, the claim credited at dom-b, acknowledged and witnessed (sequence 40); accounting checks {"backing_matches_journal": true, "claims_matches_journal": true}; recovery state normal.
6. At dom-b the machine spent part of its imported claim on storage (lineage [['partially_consumed', 6, 0]]): the claim became nonreturnable.
7. The supplier, whose key lives in the external signer, could not spend unsigned ([409, 'signature_required']) and paid the verifier through the signer (settled).
8. The treasury funded a **distinct** recipient (2) from collected fees only (treasury available before the award: 9).
Final table: {"d_req": 1951, "d_mach": 14, "d_sup": 3019, "d_ver": 4, "d_con": 2, "treasury": 7, "dom-b:d_mach_b": 6, "dom-b:d_sup_b": 4}; conservation across both domains: True (dom-a held + backing locked = fixture funding; dom-b claims = held). Postings: 46 journal entries at dom-a and 5 at dom-b are in `demonstration/demonstration.json`. The six-actor 80-unit example with a distinct award recipient (journey 1) ends at {"r80": 35, "m80": 30, "s80": 9, "v80": 3, "c80": 2, "treasury": 1}, total 80.

Custody in the demonstration: mission payments and the machine's purchases run under development custody (service-held keys); external signer custody is exercised by the supplier here and by the client-enrolled provider (journey 33), and by journeys 3–7.

## User-visible functions
API (`/missions/*`, `/resources/*`, `/plans/solve`, `/native/operator`, signer-backed `/native/transfers/prepare|submit`, `/native/accounting`, `/native/accounts/{id}/lineage`, `/missions/{id}/bundle`), CLI (`mission-*`, `resource-*`, `native-operator`, `native-transfer-signed`), console (`/console/missions`, `/console/missions/{id}`, `/console/resources`, `/console/operator`), MCP (read / simulate / spend tools), separate executables (`native.signer`, `native.witness`, `deploy_local`), and the standard-library provider client `metacoin-provider` with offline verification. Browser journey 35: 12 checks passed, 0 failed; screenshots in `browser-screenshots/`.

## Revisions and time
- Base: `6a9b5926bfd35c8b521847707d49ac2eacfe4647` (pointer commit of the native space protocol order; source candidate 35d194a, records c5263df).
- Source candidate: `809b95eb974337c934dd78c7e736678a3bf1c83c` on `feature/autonomous-mission-economy-v1`; journeys loaded `809b95eb974337c934dd78c7e736678a3bf1c83c`; records commit `c3b6767f12318d9e6617485e3df4c1e9c7573409`; packaged revision: SOURCE_REVISION.txt (source identical to the candidate; `work/` excluded).
- Live service: 3e34126 / schema 040 at 127.0.0.1:8402, untouched (`live-untouched.json`).
- Started 2026-10-23T02:13:24Z (session START); this report built 2026-10-23T04:17:34Z.

## Verification
- Journeys (`journeys/journeys-mission.json`): 37 passed, 0 failed, 0 blocked, 0 skipped of 37 (36 required journeys + the demonstration). Journey 36 (passed): migration rehearsal on an isolated copy of the live database (040 → 047, balances digest identical, old jobs readable) and the clean export of the packaged archive (19 passed / 0 failed journeys from the fresh extraction, archive sha256 74d41dd05e4963bf…; one-command clean start up/status/down, 7 unit suites, client conformance 22/22, vectors).
- Unit suites: 16 suites OK (`test-logs/unit-summary.txt`); provider client conformance 22/22 under the system Python with only the standard library.
- Endurance (150 s, API restarted at 75.0 s, back after 0.74 s): transfers 2163 ok, reservations 2989 ok (buyers ran dry: 1967 refusals, counted), work 45 awards accepted and paid natively, cross-domain 188 exports; transport errors during the restart: {"transfers": {"transport:ConnectError": 2}, "reservations": {"transport:ConnectError": 6}, "work": {"exception:ConnectError": 1}, "cross_domain": {"transport:ConnectError": 1, "courier:ConnectError": 1}} — conserved across domains: True, duplicate settled intents 0, duplicate reservations 0, jobs {"succeeded": 90}.

## Benchmarks (same host, loopback, SQLite; same workload on the base revision and HEAD)
| metric | before (6a9b592) | after (809b95e) |
|---|---|---|
| 300 concurrent native transfers: throughput / p50 / p95 | 87.2/s · 0.0207 s · 0.1913 s | 89.1/s · 0.0185 s · 0.4401 s |
| native settlement: award p50 / to-accepted p50 / pay p50 | 0.0319 / 1.14 / 0.0554 s | 0.0313 / 1.143 / 0.0512 s |
| journal replay | 0.078 s | 0.076 s |
| storage (service.sqlite) | 4157440 B | 4263936 B |

New measurements (after only): signer overhead p50 0.0095 s per payment (prepare 0.0031 + sign 0.0051 + submit 0.0153 s vs 0.014 s development custody; n=60); reservation contention 16 simultaneous buyers on capacity 40: 8 accepted, {'capacity_exhausted': 8} refused, consistent True, p95 0.2204 s; offline backlog 40 exports queued during a cut link recovered in 0.204 s (196.4 items/s, complete True); planner runtime [(6, 'optimal', 0), (10, 'infeasible', 2), (14, 'infeasible', 1), (20, 'infeasible', 296)] (tasks, status, ms; the random 10–20-task fixtures were infeasible by capacity, so those rows measure proof-of-infeasibility time; the 6-task row agrees with the oracle); worker over 4 lunar awards: 2.644 jobs/s, CPU +0.05 s, peak RSS 63728 kB. Sample sizes are small (4–300); tails are reported, not smoothed; the p95 of the 300-transfer run moved from 0.1913 to 0.4401 s between two single runs (noise of one SQLite host, not a measured regression).

## Required and optional items
Required groups A–H, the 36 journeys, the demonstration, migrations with rehearsal, benchmarks and endurance, docs, feature ledger, clean-start guide and packaging: done. Optional §16 extensions: not built beyond what the required groups cover. BPv7: not exercised (no installable daemon; `bpv7-probe.json`). Two hosts: documented, single host exercised. Nothing published, pushed or merged; live service not upgraded.
