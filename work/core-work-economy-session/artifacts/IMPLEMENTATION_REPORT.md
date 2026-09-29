# MetaCoin core work economy — implementation report (Order 08, 2026-09-29)

## What the four parties can do now

**An owner / requester** writes typed work terms (what counts as delivery, an explicit acceptance policy with a predicate trace, milestones with dependencies, outcome-neutral payment rules that pay a verified INFEASIBLE the same as a verified FEASIBLE, deadlines, eligibility, a visible selection policy, privacy and reassignment policies), freezes them against committed inputs, opens a private request, compares eligible offers under the declared policy, awards one in a single guarded transaction that reserves budget, watches execution / science / acceptance / payment as four separate dimensions, verifies, accepts or rejects with reasons, pays through a payment intent bound to the entitlement (application journal or the private local chain, exact or capped), reconciles lost responses by transaction hash or Permit2 nonce, allocates fee-backed treasury budgets, defines recurring procurement programs with per-run and aggregate ceilings, runs portfolio budget scenarios under declared utilities, drafts contracts from mission bottlenecks, grants read-only audit access, exports bundles, projections and encrypted audit packages, and previews foreign packages without trusting or paying anything. Console pages: work overview, terms, request comparison, contract detail with the next authorized action, disputes, budgets/journal/treasury, reconciliation, provider history, mission portfolio.

**A provider** registers with signed capability revisions and a payout address, sees request previews without private inputs, submits binding offers (excluded ones carry structured reasons), acknowledges awards, executes through the ordinary worker transport (same host) or an enrolled node, receives provider receipts, opens disputes, proposes bounded counterexample challenges against receipts, curates a disclosed portfolio of its own receipts, and is described by evidence-based history dimensions with sample sizes instead of a score.

**A verifier / reviewer** is assigned with honest independence labels, verifies in a separate worker process, is paid for verification work independently of the verdict, reads receipts with custody labels, decides disputes under the declared resolver authority (superseding, never overwriting, the original decision), and can verify any bundle offline with the portable verifier and a pinned trust root (key substitution fails even for mathematically valid signatures).

**A bounded agent** (MCP or the CLI under an issued grant) drafts requests, checks provider compatibility, compares offers, awards only under an explicit `work:award` grant operation and its amount ceiling, inspects evidence, evaluates acceptance, prepares and opens disputes and reconciles budgets; injected instructions inside offers or documents are data.

## Identities
| item | value |
|---|---|
| branch | `service/real-features` (local, not pushed) |
| base revision | `103bde963011cfcaccb3b7986dd403e547f17bbd` |
| candidate revision (source) | `3e3412678ab487ee0792807d9f5b902895180584` (differs from the verification revision `1fe5e1b12639a94454d97f59306c262ad529b4a4` only by relocating the §62 client example to `metacoin_service/examples/` and relaxing one test assertion for exported trees without `.git`; no service code changed; intermediate candidate `65fe783`) |
| records commit | `created after packaging (work/ session records only); hash in commits.txt of the final delivery pass and in the final message` |
| loaded live revision (tmux `metacoin-service`, 127.0.0.1:8402) | `3e3412678ab487ee0792807d9f5b902895180584` |
| schema | `040_work_programs_pricing_challenges` (migrations 032–040 added this session) |
| delivery path | `~/metacoin-core-work-economy-delivery-2026-09-29/` |
| elapsed work | 2 h 54 min to the start of packaging (START 2026-09-29T03:06:55Z, end 2026-09-29T06:01:19Z (packaging start)) |
| interpreters | API venv Python 3.12.3 (fastapi, x402 2.24.0 + evm), compute `/usr/bin/python3` (numpy/scipy/torch, CUDA) |

## Status by class
- **Implemented and exercised**: Groups A–F (terms, board, evidence/receipts/disputes/delegation, payments/journal/treasury/refunds, compartments/audit grants/retention/projections/key rotation, mission portfolio/resource evidence/observations); §61 packages + import preview; §62 client example + CLI; §63 console; §64 MCP; §65 notifications; §66 demonstration; §67 measurements; §68 benchmark/observability; §38 provider history; §39 rotation; §73 fault campaign; §74 migration + live upgrade; §76 items 1–10 (see FEATURE_LEDGER.json for the exact operation and evidence of each).
- **Implemented but dependency-blocked**: external settlement on a public network (payer guard refuses production; the x402 SDK in this checkout has no offer-receipt extension, so offer signatures are service-custodied); energy counters (energy reported unavailable or as a labelled estimate); organizational verifier independence (only process separation on one host).
- **Unsupported by design**: work payments minting base supply; Test-META value; anchoring application records to the protocol ledger; public request announcements; contacting external providers.
- **Runtime data outside the archive**: `protocol/ledger_data.jsonl` and `mission_verdict.json` are gitignored in the public repo by design; the delivery carries them in `protocol-data/` and the clean-export run places them (journey 40 found their absence first: legacy replay and the mission portfolio need them).
- **Unfinished / limited**: selective per-viewer disclosure policies for provider portfolios beyond role scope (§76.6); the private local chain lives in the API process (a restart discards it and reconciliation then reports rail identity changed).

## Evidence
- Client example (§62): end-to-end run against a disposable instance succeeded (examples/work-client-run.json).
- Journeys: 40 of 40 passed (39 at the verification revision, journey 40 on the final archive) at 1fe5e1b12639; browser: 23 checks passed, 0 failed (screenshots in `browser-screenshots/`); journey 40 (clean reproduction of the delivered archive `a891e3c4c51e7bc3…` in a fresh offline venv from the lock, runtime data placed from protocol-data/): init, status, work suites (45 tests), science, service, chain, money+faults and the 11-journey subset all rc=0 (clean-export-logs/); patch check equal (patch-check.json).
- Unit suites: 250 tests OK (13 skipped: optional hosts/hardware) in 516 s; science suite (compute interpreter): 18 tests OK; local chain + upto: 7 tests OK; fault campaign: 13 tests OK over 10 fault points (award_commit, reservation_posting, evidence_publication, verifier_completion, acceptance_decision, payment_signing, payment_submission, payment_observation, fee_credit, refund_observation).
- Demonstration (§66): 17 of 17 steps passed at 1fe5e1b12639 — a verified positive and a verified negative each accepted and paid 10 on the private chain, fees of 1 each observed at the treasury address, a fabricated positive rejected by contract-digest / input-root binding, a solver timeout classified `no_valid_evidence` with nothing payable, a duplicate claim refused (one entitlement, already paid), a mistaken rejection corrected by an authorized superseding acceptance, a treasury award funded from confirmed fee revenue paying an accepted negative, journal replay consistent, protocol files unchanged.
- Accounting: journal replay consistent with invariants in every run; base issuance, identity text, historical task outputs, ledger bytes and monetary parameters unchanged (BASELINE_DIGESTS.txt vs FINAL_DIGESTS.txt).
- Benchmark (§68, same host, indicative only): 8 concurrent requesters → 8 awards, 0 errors, wall 3.6 s; award median 0.021 s, evaluate median 0.006 s, decide median 0.017 s, pay (prepare+submit) median 0.031 s, queue delay to execution median 0.99 s; journal consistent True.
- Privacy scan: 32 result files scanned, 0 hits; session patch clean.

## Precise economic and scientific statements
- Accepted negatives: journeys 9, 32, 36 and the demonstration accept INFEASIBLE determinations with `payment_class=complete`; the provider history counts them as accepted deliveries.
- Duplicate claims: journey 15 and the demonstration (409 on a second decision with a new idempotency key; one entitlement per milestone and kind).
- Observed local payments: exact and capped transfers on the private py-evm chain (journeys 31–37); no public settlement occurred or is claimed.
- Treasury: fees are separate entitlements paid by the requester to the treasury address; availability = confirmed revenue − commitments − spending; a treasury award above availability is refused (BUDGET_EXHAUSTED).
- Base issuance: no code path in `metacoin_service/economy/*` touches `protocol/` or the mission verdict; the anchor-candidate operation never appends to the ledger.
- Same-host synthetic evidence: one operator plays every role; nothing here shows price discovery, anonymity, independent organizations or physical infrastructure work.

## Live upgrade (§74)
Four runs of `live_upgrade.sh` (live-upgrade-history.json): run 1 backed up with keys, proved an isolated restore, migrated 031 → 040 (032–040 applied) and loaded `1fe5e1b`; run 2 (loading the relocated-example candidate) caused an ~8 min API outage because stopping the pane processes closed the tmux session (ISSUES.md 31) and was recovered by recreating the session; run 3 loaded `65fe783` cleanly; run 4 loaded the final candidate `3e34126` (journal replay consistent, old artifact export 200, document / generation / plan smoke succeeded, loopback binding, credentials preserved). No data was modified during the outage.

## Defects found and fixed this session
See ISSUES.md (30 entries). Notable: journal event keys per submission attempt (fault campaign), journal replay scope for unpaid chain entitlements (demonstration), numerical-witness status read from the solver summary, agent grants without a work-award operation, a chain award to a provider without a chain payout address surfacing as an untraced internal defect (now an eligibility reason plus a private defects log).
