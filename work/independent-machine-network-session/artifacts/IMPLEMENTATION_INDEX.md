# Implementation index — independent machine network (order 2026-10-23)

## Required groups → code

| group | code | journeys |
|---|---|---|
| A | `metacoin_service/native/signing.py, native/signer.py (policy, context binding), native/controller.py (await_signature), native/service.py (require_external_custody, settlement_preview), economy/money.py` | 4, 5, 6, 7, 8, 40 |
| B | `provider_client/metacoin_provider/{client,api,signer,__main__}.py, economy/board.py (identity_view), economy/routes.py (/work/providers/{id}/identity)` | 3, 9, 10, 15 |
| C | `native/pools.py (sweep, admissible, Pools), native/market.py (reserve), db.py 048` | 11, 12, 13, 14, 15 |
| D | `native/subcontracts.py, native/mission_contracts.py (redaction), native/controller.py (award close on reject), db.py 050` | 16, 17, 18, 19, 20, 21 |
| E | `native/products.py, db.py 052, economy/ops.py (product_delivery fault)` | 22, 23, 24, 25 |
| F | `native/relay.py, db.py 053` | 26, 27, 28, 29, 30 |
| G | `native/lots.py, native/domains.py (return_request_partial, partial certificates, accounting), native/protocol.py (v2 fields), db.py 051` | 31, 32, 33, 34, 35 |
| H | `native/controller.py (generations, continuity, _fence), native/controller_runner.py, db.py 054` | 36, 37, 38, 39, 40 |
| I | `native/portfolio.py, native/planner.py, native/mission_planning.py` | 41, 42, 43, 44 |
| J | `console_ui.py, templates/network_*.html, mcp_server.py, client_cli.py` | 46, 45, 2 |

## Cross-cutting items → code

| item | code | journeys |
|---|---|---|
| §1 demonstration | `tests/journeys_network.py (_demo)` | 0 |
| §17 model/checker/vectors | `native/model_network.py, tests/test_native_model_network.py, native/checker.py, provider_client/metacoin_provider/verify.py, native/conformance_fixtures.py` | 45, 3 |
| §18 endurance/benchmarks | `endurance_network.py, benchmark_network.py, run_benchmark.py, db.py (FairGate), tests/test_db_contention.py` | 48 |
| §19 migrations | `db.py, MIGRATIONS.json, MIGRATION_NOTES.md` | 47 |
| §22 packaging | `package.sh, clean_export.sh` | 49, 50 |

## §21 extensions → code

| extension | status | code | journeys |
|---|---|---|---|
| 1 bidding | completed | `native/bidding.py, native/controller.py (opened branch), native/mission_contracts.py (selection validation)` | 51 |
| 2 substitution | completed | `native/substitution.py, routes (missions/{id}/substitute)` | 52 |
| 3 route planning | completed | `native/route_planning.py, routes (relay/plan), MCP relay_plan` | 53 |
| 4 subscriptions | completed | `native/subscriptions.py, native/controller.py (not_before wait), routes (subscriptions)` | 54 |
| 5 offline console | completed | `native/offline_console.py` | 55 |
| 6 second host / BPv7 | blocked (external) | `native/hostconfig.py` | (unit tests) |
| 7 starter kit | completed | `provider_client/starter_kit/` | (unit tests) |
| 8 scenarios | completed | `native/scenarios.py, routes (portfolio/scenarios), MCP evidence_scenarios` | 56 |

## Journeys → evidence

| journey | title | status | evidence |
|---|---|---|---|
| 1 | Recover the completed baseline and reproduce the six-actor 80-unit native replay | passed | journeys/journeys-network.json → results[journey=1] |
| 2 | Run the new connected mission with x402 disabled and its facilitator unreachable | passed | journeys/journeys-network.json → results[journey=2] |
| 3 | Enroll a clean provider without server database imports or copied private state. | passed | journeys/journeys-network.json → results[journey=3] |
| 4 | Execute requester funding, machine spending and supplier settlement through conf | passed | journeys/journeys-network.json → results[journey=4] |
| 5 | Reject altered recipient, amount, asset, domain and obligation before signing. | passed | journeys/journeys-network.json → results[journey=5] |
| 6 | Crash and restart a signer around authorization persistence without extra exposu | passed | journeys/journeys-network.json → results[journey=6] |
| 7 | Rotate a key while preserving historical verification and outstanding obligation | passed | journeys/journeys-network.json → results[journey=7] |
| 8 | Refuse a delegation expanding recipient scope or aggregate budget. | passed | journeys/journeys-network.json → results[journey=8] |
| 9 | Detect and disclose shared-operator deployment rather than labelling it independ | passed | journeys/journeys-network.json → results[journey=9] |
| 10 | Reject an unpinned peer or incompatible critical protocol version. | passed | journeys/journeys-network.json → results[journey=10] |
| 11 | Publish overlapping offers against one pool and refuse overselling their shared  | passed | journeys/journeys-network.json → results[journey=11] |
| 12 | Race reservations across two offers while conserving both capacity and funds. | passed | journeys/journeys-network.json → results[journey=12] |
| 13 | Handle boundary-touching intervals and unit conversions exactly. | passed | journeys/journeys-network.json → results[journey=13] |
| 14 | Refuse a capacity reduction conflicting with committed reservations. | passed | journeys/journeys-network.json → results[journey=14] |
| 15 | Drain a provider without erasing delivered work or accepted obligations. | passed | journeys/journeys-network.json → results[journey=15] |
| 16 | Create a funded subcontract bound to parent terms and permitted inputs. | passed | journeys/journeys-network.json → results[journey=16] |
| 17 | Refuse circular funding based on the parent's expected future earnings. | passed | journeys/journeys-network.json → results[journey=17] |
| 18 | Execute a subcontract from settled machine earnings and settle it once. | passed | journeys/journeys-network.json → results[journey=18] |
| 19 | Fail a subcontract milestone while preserving separately accepted useful work. | passed | journeys/journeys-network.json → results[journey=19] |
| 20 | Refuse an unauthorized amendment and excessive delegation depth. | passed | journeys/journeys-network.json → results[journey=20] |
| 21 | Prevent private artifact disclosure to an unauthorized subcontractor. | passed | journeys/journeys-network.json → results[journey=21] |
| 22 | Publish a versioned mission product with explicit provenance and rights. | passed | journeys/journeys-network.json → results[journey=22] |
| 23 | Purchase and reuse a compatible product through native settlement. | passed | journeys/journeys-network.json → results[journey=23] |
| 24 | Recover a crash after payment but before entitlement delivery without charging a | passed | journeys/journeys-network.json → results[journey=24] |
| 25 | Refuse reuse when resolution, units or required assumptions are incompatible. | passed | journeys/journeys-network.json → results[journey=25] |
| 26 | Execute a paid two-hop relay with separate queues and bounded fees. | passed | journeys/journeys-network.json → results[journey=26] |
| 27 | Distinguish an intermediate relay receipt from final application delivery. | passed | journeys/journeys-network.json → results[journey=27] |
| 28 | Recover lost receipts, duplicate delivery and a relay restart without duplicate  | passed | journeys/journeys-network.json → results[journey=28] |
| 29 | Miss a contact window and replan without unsafe financial release. | passed | journeys/journeys-network.json → results[journey=29] |
| 30 | Enforce payload, retention, recipient and fee limits at relay admission. | passed | journeys/journeys-network.json → results[journey=30] |
| 31 | Split a 20-unit claim, spend 7 and retire only the holder's remaining 13. | passed | journeys/journeys-network.json → results[journey=31] |
| 32 | Release 13 at the source once while leaving the supplier's 7 fully backed. | passed | journeys/journeys-network.json → results[journey=32] |
| 33 | Refuse duplicate parent consumption and attempts to retire another owner's claim | passed | journeys/journeys-network.json → results[journey=33] |
| 34 | Exercise every retirement/certificate/release/acknowledgment crash boundary. | passed | journeys/journeys-network.json → results[journey=34] |
| 35 | Reject mixed-asset or missing-lineage return evidence. | passed | journeys/journeys-network.json → results[journey=35] |
| 36 | Reconstruct a mission after controller failure and acquire a valid fenced genera | passed | journeys/journeys-network.json → results[journey=36] |
| 37 | Reject stale-controller mutations after takeover. | passed | journeys/journeys-network.json → results[journey=37] |
| 38 | Refuse self-issued takeover when the authority cannot establish a new generation | passed | journeys/journeys-network.json → results[journey=38] |
| 39 | Quarantine a stale authority restore using witness/peer evidence. | passed | journeys/journeys-network.json → results[journey=39] |
| 40 | Preserve uncertain obligations when a signer, witness or external worker is unav | passed | journeys/journeys-network.json → results[journey=40] |
| 41 | Match bounded portfolio fixtures against an exhaustive independent oracle. | passed | journeys/journeys-network.json → results[journey=41] |
| 42 | Schedule competing missions across shared pools and domain-local liquidity. | passed | journeys/journeys-network.json → results[journey=42] |
| 43 | Purchase bounded evidence and replan only eligible uncommitted work. | passed | journeys/journeys-network.json → results[journey=43] |
| 44 | Reject a planner output spending pending remote claims or overselling resources. | passed | journeys/journeys-network.json → results[journey=44] |
| 45 | Verify a portable bundle offline and reject tampered claims, fees and signatures | passed | journeys/journeys-network.json → results[journey=45] |
| 46 | Complete enrollment-to-settlement in the real browser, including disruption reco | passed | journeys/journeys-network.json → results[journey=46] + browser-screenshots/ |
| 47 | Rehearse migration on prior-state copies and preserve balances, old receipts and | passed | journeys/journeys-network.json → results[journey=47] |
| 48 | Run a sustained mixed workload with real contention, restarts and auditable reco | passed | journeys/journeys-network.json → results[journey=48] + benchmarks/endurance-network.json |
| 49 | Reproduce clean installation and a representative full mission from the delivery | passed | journeys/journeys-network.json → results[journey=49] + clean-export-logs/ |
| 50 | Verify the complete final money/resource accounting and all required feature sta | passed | journeys/journeys-network.json → results[journey=50] |
| 51 | Extension 1: multi-provider bidding under a declared deterministic policy with p | passed | journeys/journeys-network.json → results[journey=51] |
| 52 | Extension 2: explicit provider substitution for an unstarted milestone with affe | passed | journeys/journeys-network.json → results[journey=52] |
| 53 | Extension 3: shared route planning across relay offers with bounded hops and ind | passed | journeys/journeys-network.json → results[journey=53] |
| 54 | Extension 4: bounded prepaid subscription (no renewal); first period delivered a | passed | journeys/journeys-network.json → results[journey=54] |
| 55 | Extension 5: offline operator console rendered from a signed export with explici | passed | journeys/journeys-network.json → results[journey=55] |
| 56 | Extension 8: scenario comparison of extra evidence versus delay under fixed decl | passed | journeys/journeys-network.json → results[journey=56] |
| 0 | Economically complete mission under custody separation: requester, primary machi | passed | journeys/journeys-network.json → results[journey=0] + demonstration/demonstration.json |

External tracks (blocked, never counted as passed): second_host_separate_operator — blocked: no authorized reachable second host; configuration generator tested statically; bpv7 — blocked: no installable RFC 9171 daemon (bpv7-probe.json); the relay service is not BPv7; separate_os_identities — not performed: no privileged accounts or host changes are part of this order
