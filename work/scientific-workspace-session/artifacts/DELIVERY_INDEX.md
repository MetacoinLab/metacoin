# Delivery index (scientific workspace expansion, executed 2026-09-28)

| Path | What it is |
|---|---|
| `IMPLEMENTATION_REPORT.md` | the final report: new actions, evidence by category, findings, defects, limits, live instance, elapsed time |
| `FEATURE_LEDGER.json` | per-feature status with entry points, evidence and known limits (Groups A–F, ops, backlog items) |
| `verification-results.json` | the consolidated verification chain at the candidate revision: suites, journeys, records, by category |
| `ISSUES.md` | defects found and fixed during the order, with coverage |
| `metacoin-scientific-workspace-source.zip` | clean tracked-source export of the candidate (`SOURCE_REVISION.txt`), allowlisted files only (`SOURCE_ALLOWLIST.txt`) |
| `metacoin-scientific-workspace-session-vs-be2d60c.patch`, `commits.txt`, `BASE_REVISION.txt` | the session's changes against the exact recorded base |
| `requirements-service.lock`, `requirements-compute.txt` | pinned dependencies of the API venv and of the compute/model interpreter (scipy, pypdf, onnxruntime, rapidocr pins) |
| `MIGRATIONS.json` | migrations 026–031 with transaction and rollback notes |
| `openapi.json`, `mcp-schema.json` | API schema (354 paths) and MCP tool schemas (32 tools, protocol 2025-11-25, mcp SDK 1.26.0) |
| `SCIENTIFIC_MODEL_resource_plan.py` | the executable model specification of robust-resource-plan/v1 (docstring = mathematical contract; simulator; oracle) |
| `verify_bundle.py` | the offline verifier for signed result bundles |
| `synthetic-document-fixtures/` | the PDF fixtures (dev, held-out) with their manifest |
| `examples/` | a workflow package export, a restricted projection export, a signed result bundle, a measurement request (synthetic) |
| `journeys/` | the 24 acceptance journeys: final run at the candidate plus the earlier runs/reruns that found the defects |
| `test-logs/` | consolidated suite, science suite, local-chain suite, regression batches, journeys |
| `benchmarks/`, `generation-batching-benchmark.json`, `continuous-equality.json`, `adaptive-campaign-benchmark.json` | measurements with their conditions and sample sizes |
| `eval-set-agent-behavior-v2.json`, `document-extraction-results-baseline.json` | agent evaluation (dev/validation/held-out) and document extraction results (dev 42/42, held-out 14/14) |
| `live-status.json`, `live-capabilities.json`, `live-upgrade.json` | the upgraded live instance after backup, migration and restart |
| `local-chain-record.json` | the private local-chain scenarios (upto), including lost-response reconciliation |
| `browser-screenshots/` | Playwright evidence of the console at desktop and narrow widths |
| `clean-export-logs/` | journey 24: fresh venv from the lock (local wheels only), init, status, suites, local chain, journeys from the archive |
| `SESSION_STATE.md`, `producers.jsonl`, `recorder-selftest.jsonl` | sanitized checkpoint, background producer ledger, step-recorder self-test |
| `DELIVERY_SCANS.txt`, `MANIFEST_SHA256SUMS.txt` | allowlist/secret/private-label scans and the checksum manifest |
