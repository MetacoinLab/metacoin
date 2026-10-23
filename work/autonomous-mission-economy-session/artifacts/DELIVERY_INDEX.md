# Delivery index — MetaCoin autonomous mission economy (order 2026-10-23)

| Item | Where |
|---|---|
| Reviewable branch | `feature/autonomous-mission-economy-v1` (local; `commits.txt`); SOURCE_REVISION.txt / BASE_REVISION.txt / RECORDS_COMMIT.txt |
| Source archive + patch vs base | `metacoin-autonomous-mission-economy-source.zip`, `metacoin-autonomous-mission-economy-session-vs-<base>.patch` |
| Dependency locks | `requirements-service.lock`, `requirements-compute.txt`; the provider client has no dependencies |
| Protocol extension and trust model | `spec/PROTOCOL.md` (§11), `docs/ARCHITECTURE_AND_TRUST.md`, `TRUST_MODEL.md`, `TWO_HOST.md` |
| API / CLI / console / MCP documentation | `docs/INTERFACES.md`, `openapi.json`, `mcp-schema.json` |
| Independent checker and vectors | `native_checker.py` (+ key history), `vectors/`; provider client offline verifier `provider_client/metacoin_provider/verify.py`; conformance fixtures `conformance/provider-conformance-v1.json` |
| Provider client | `provider_client/` (standard library only; `metacoin-provider` CLI) |
| Resource and mission examples | `demonstration/demonstration.json` (funding table, steps, postings, final table, conservation), journeys 8–25 evidence, `examples/` |
| Migration results | `MIGRATIONS.json`, `MIGRATION_NOTES.md`, journey 36 evidence, `live-untouched.json` |
| Benchmarks | `benchmarks/benchmark-before.json`, `benchmark-after.json` (same native workload on the base revision and HEAD), `benchmark-mission.json`, `endurance-mission.json` |
| Screenshots | `browser-screenshots/*.png` (journey 35) |
| Failure logs | `test-logs/` (unit suites, journeys, benchmark, endurance), `ISSUES.md` |
| Feature ledger | `FEATURE_LEDGER.json` (implemented / exercised / environment / remaining limits) |
| Clean-start guide | `CLEAN_START.md`; verification `clean-export-logs/` |
| Handoff | `HANDOFF.md`, `SESSION_STATE.md`, `IMPLEMENTATION_REPORT.md`, `CANDIDATE.json`, `verification-results.json`, `bpv7-probe.json` |
| Integrity | `SHA256SUMS` (verified on a fresh extraction: `clean-export-logs/sha256-fresh-extraction.txt`), `DELIVERY_SCANS.txt` |
