# Delivery index — MetaCoin independent machine network (order 2026-10-23)

| Item | Where |
|---|---|
| Reviewable branch | `feature/independent-machine-network-v1` (local; `commits.txt`); SOURCE_REVISION.txt / BASE_REVISION.txt / RECORDS_COMMIT.txt / CANDIDATE.json |
| Source archive + patch vs base | `metacoin-independent-machine-network-source.zip`, `metacoin-independent-machine-network-session-vs-<base>.patch` |
| Dependency locks | `requirements-service.lock`, `requirements-compute.txt`; the provider client has no dependencies |
| Protocol / architecture / custody | `spec/PROTOCOL.md` (§12), `docs/ARCHITECTURE_AND_TRUST.md`, `TRUST_MODEL.md`, `docs/TWO_HOST.md`, `EXTERNAL_TRACKS.md` |
| API / CLI / console / MCP | `docs/INTERFACES.md`, `openapi.json`, `mcp-schema.json`, executed examples `examples/DEVELOPER_EXAMPLES.md` + `developer-examples.json` |
| Independent checker, verifier and vectors | `native_checker.py` (v2 resource/lineage checks), `vectors/`, `conformance/provider-conformance-v2.json` (32 cases), `provider_client/metacoin_provider/verify.py` |
| Bounded model | `native_model_network.py`, `model-network-report.json`, replay tests `metacoin_service/tests/test_native_model_network.py` |
| Provider client | `provider_client/` (standard library only; `metacoin-provider` CLI with `init/handshake/drain/withdraw/sign-pending`) |
| Demonstration and journeys | `demonstration/demonstration.json` (keys, funding table, steps, postings, pool commitments, conditions), `journeys/journeys-network.json` (50 + demonstration), `IMPLEMENTATION_INDEX.md` (group → code, journey → evidence) |
| Migration results | `MIGRATIONS.json` (048–054), `MIGRATION_NOTES.md`, journey 47 evidence, `live-untouched.json` |
| Benchmarks and endurance | `benchmarks/benchmark-native-{before,after}.json`, `benchmark-mission-{before,after}.json`, `benchmark-network.json`, `endurance-network.json` (+ progress ticks; final run at the candidate) and the preserved earlier runs `endurance-network-first-run-1b396f3.json`, `endurance-network-run2-53c9d49.json`, `endurance-network-run3-53c9d49.json` + `endurance-network-run3-53c9d49.defects-end-a.log` (the masked lock-wait timeouts; see ISSUES.md "Endurance defects"), `endurance-network-run4-2ff4da5.json` (first fixed tree, ticket gate) and `benchmarks/at-2ff4da5/` (its before/after benchmarks that exposed the ticket gate's throughput cost) |
| Browser evidence | `browser-screenshots/*.png` (journey 46) |
| Deployment | `CLEAN_START.md` (verified from the archive: `clean-export-logs/`), `native_hostconfig.py` + `examples/hostconfig-sample/`, `deploy_local` |
| Contention regression suite | `metacoin_service/tests/test_db_contention.py` in the source archive (lock-wait timeout → JOURNAL_BUSY, FIFO gate storm, API 503 + Retry-After, worker survives a busy database) |
| Failure logs | `test-logs/` (unit suites, journeys full1–full4, endurance, benchmark), `ISSUES.md` (defects found and fixed, kept) |
| Feature ledger | `FEATURE_LEDGER.json` (implemented / exercised / topology-trust assumptions / unresolved limits; external tracks blocked) |
| Handoff | `HANDOFF.md`, `SESSION_STATE.md`, `IMPLEMENTATION_REPORT.md`, `verification-results.json`, `bpv7-probe.json` |
| Integrity | `SHA256SUMS` (verified on a fresh extraction: `clean-export-logs/sha256-fresh-extraction.txt`), `DELIVERY_SCANS.txt` |
