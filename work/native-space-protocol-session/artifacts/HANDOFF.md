# Handoff — MetaCoin native space-work and settlement protocol (order 2026-10-22)

## What exists
A MetaCoin-native settlement core (signed intents, actor accounts with disjoint categories, obligations, treasury on fees, disputes) bound to the existing work economy (terms → offers → award → execution → verification → acceptance → native settlement), two separately configured settlement domains exchanging destination-bound allocations through signed envelopes carried by an online courier or file bundles, bounded disconnected spending, rotation epochs, rollback quarantine with peer-observation reconciliation, three lunar products on real LOLA LDEM_4 extracts with an independent CPU reference, resource- and contact-aware procurement with baselines, evidence acquisition inside the mission, an independent conformance checker with deterministic vectors and a bounded state model, and product interfaces (API, CLI, six console views, MCP read/simulate/commit/spend tools). x402 is optional: `METACOIN_X402_DISABLED=1` runs everything; `metacoin_service/native/*` never imports it.

## One command sequence (isolated local product + native economic journey)
```
cd <export>/metacoin-native-space-protocol
python3 -m venv .venv && .venv/bin/pip install -r metacoin_service/requirements.lock
export PYTHONPATH=$PWD METACOIN_X402_DISABLED=1
.venv/bin/python -m metacoin_service --home ./svc-home --provider-mode simulation init          # prints the private credential file path
.venv/bin/python -m unittest metacoin_service.tests.test_native_protocol metacoin_service.tests.test_native_domains metacoin_service.tests.test_native_lunar
.venv/bin/python -m metacoin_service.tests.journeys_native --only 1,2,21,22 --out journeys.json      # two domain processes; the 80-unit loop is journey 21
.venv/bin/python -m metacoin_service.native.vectors && .venv/bin/python -m metacoin_service.native.model
```
Interactive: `serve --port 8402` then the console at `/console/native`, `/console/lunar`; CLI `python -m metacoin_service.client_cli --credential-file <0600 file> native-...`; MCP `python -m metacoin_service.mcp_server`.

## Independent verification
`python -m metacoin_service.native.checker EXPORT.json --roots ROOTS.json` over `GET /api/v1/native/export` with roots pinned out of band; `python -m metacoin_service.lunar check <kind> inputs.json result.json`; `python -m metacoin_service.lunar reproduce inputs.json --manifest manifest.json`.

## Where the records are
`work/native-space-protocol-session/` (SESSION_STATE.md, artifacts/journeys/*.json, artifacts/shots/*.png, artifacts/benchmarks/*.json, artifacts/logs/*.log, MIGRATIONS.json, MIGRATION_NOTES.md, TRUST_MODEL.md, FEATURE_LEDGER.json); `docs/native/` (PROTOCOL.md, protocol-spec.json, ARCHITECTURE_AND_TRUST.md, INTERFACES.md).

## Not done / limits (honest)
- Live service not upgraded (stays 3e34126 / schema 040); the exact operation is in MIGRATION_NOTES.md.
- Branch not pushed; no PR (no publication authorization covers this branch).
- Extensions 1–10 of §22 not built (operator-owned signer, milestone resource reservations, provider package, DTN adapter, second host, stronger witnesses, marketplace, portfolio scheduling, external adapter boundary tests, conformance kit beyond the checker/vectors shipped here).
- Same operator for both domains; development keys service-custodied; equivocation detected not prevented; rollback detection needs the watermark file or a peer; returns only full-and-unspent; one allocation lifecycle per id; application-level transport (not BPv7/LunaNet).
- Lunar products: grid-scale slopes from a 7.58 km/pixel DEM; tiles ≤ 64×64; no GPU path (no measured benefit at this size); evidence-acquisition deliveries are simulated observations.
