# Delivery index (24-hour expansion, executed 2026-09-27)

| Path | What it is |
|---|---|
| `IMPLEMENTATION_REPORT.md` | the final report: usable operations, evidence, defects fixed, limits, live instance, elapsed time |
| `FEATURE_LEDGER.json` | per-feature status (implemented-and-verified / local-chain-only / not started) with entry points and evidence |
| `verification-results.json` | passes, failures, skips and blocked items from the final verification chain, by category |
| `metacoin-24h-expansion-source.zip` | clean source export of the packaged HEAD (`SOURCE_REVISION.txt`), allowlisted files only (`SOURCE_ALLOWLIST.txt`) |
| `metacoin-24h-expansion-session-vs-3b6f292.patch`, `commits.txt` | the session's changes against the declared base |
| `requirements-service.lock` | pinned dependencies of the service environment |
| `MIGRATIONS.json` | migrations 014-025 with transaction and restore notes |
| `openapi.json`, `mcp-schema.json` | API and MCP schemas of the packaged revision |
| `live-status.json`, `live-capabilities.json` | the upgraded live instance after restart (schema 025, models promoted, smoke jobs) |
| `journeys/journeys-expansion.json` | the twenty acceptance journeys with evidence and caveats |
| `test-logs/` | suite, local-chain, records, benchmark, endurance and journey logs |
| `benchmarks/benchmarks-expansion.json`, `endurance.json` | measurements with their conditions |
| `knowledge-eval.json`, `eval-set-agent-behavior.json`, `local-chain-record.json` | evaluation and local-chain records |
| `browser-screenshots/` | Playwright evidence of the console journeys |
| `clean-export-logs/` | journey 20: fresh venv from the lock, init, status, tests and journeys from the archive |
| `SESSION_STATE.md` | sanitized recovery checkpoint: what is done, what remains, how to resume |
| `DELIVERY_SCANS.txt`, `MANIFEST_SHA256SUMS.txt` | allowlist/secret scans and the checksum manifest (consistency with the manifest only) |
