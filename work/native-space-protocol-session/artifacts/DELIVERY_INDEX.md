# Delivery index — MetaCoin native space-work and settlement protocol (order 2026-10-22)

| Item | Where |
|---|---|
| Reviewable branch | `protocol/native-space-settlement-v0` (local; commits listed in `commits.txt`); SOURCE_REVISION.txt / BASE_REVISION.txt / RECORDS_COMMIT.txt |
| Source archive + patch vs base | `metacoin-native-space-protocol-source.zip`, `metacoin-native-space-protocol-session-vs-<base>.patch`, `patch-check.json` |
| Dependency locks | `requirements-service.lock`, `requirements-compute.txt` |
| Protocol specification | `spec/PROTOCOL.md`, `spec/protocol-spec.json` (rendered from `metacoin_service.native.protocol.spec()`) |
| Conformance vectors + state model | `vectors/vectors.json`, `vectors/vector-results.json`, `vectors/state-model-depth5.json`; `native_vectors.py`, `native_model.py` |
| Independent checker | `native_checker.py` (`python -m metacoin_service.native.checker EXPORT --roots ROOTS`) |
| Migration notes | `MIGRATIONS.json`, `MIGRATION_NOTES.md` (ids 042/043 chosen by inspection; isolated migration = journey 40; live service untouched) |
| Architecture and trust model | `docs/ARCHITECTURE_AND_TRUST.md`, `TRUST_MODEL.md` |
| API / CLI / MCP documentation | `docs/INTERFACES.md`, `openapi.json` |
| Working native mission example | journey 21 in `journeys/journeys-native-run<N>.json` (80-unit loop with real terrain work); `HANDOFF.md` one-command sequence |
| Browser evidence | `browser-screenshots/*.png` + journey 38 checks |
| Fault results | journeys 8, 12, 19 (fault points at every export/import boundary; backup restore) |
| Benchmarks | `benchmarks/native-benchmark.json` (transfers p50/p95, native vs application-journal settlement, replay, storage, recovery); procurement baselines in journey 27 |
| Feature ledger | `FEATURE_LEDGER.json` (implemented / executed / environment / unresolved per capability) |
| Lunar data with provenance | `lunar-data/` (LDEM_4 extracts + label; example pack inputs/manifest) |
| Test logs | `test-logs/`, `journeys/`, `clean-export-logs/` |
| Handoff | `HANDOFF.md`, `SESSION_STATE.md`, `IMPLEMENTATION_REPORT.md` |
| Integrity | `MANIFEST_SHA256SUMS.txt`, `DELIVERY_SCANS.txt`, `privacy-scan.json` |
