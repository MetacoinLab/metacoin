# Delivery index — MetaCoin core work economy (Order 08)

| file / directory | what it is |
|---|---|
| IMPLEMENTATION_REPORT.md | lead report: what owner / provider / verifier / agent can do; identities; status classes; evidence; limits |
| FEATURE_LEDGER.json | every feature with its entry points, evidence and status (implemented-and-exercised vs not) |
| metacoin-core-work-economy-source.zip | git archive of the candidate revision (SOURCE_REVISION.txt), `work/` excluded |
| metacoin-core-work-economy-session-vs-<base>.patch | session patch from BASE_REVISION.txt to the candidate; patch-check.json proves it reproduces the tree |
| commits.txt, SOURCE_REVISION.txt, BASE_REVISION.txt, CANDIDATE.json | exact identities |
| requirements-service.lock, requirements-compute.txt, INTERPRETERS.json | dependency locks and actual interpreters / chain artifact identities |
| MIGRATIONS.json, migrate.json, backup-manifest.json, restore-check.json, live-health.json, live-status.json, live-upgrade.json | schema changes (032–040), the live upgrade with backup and isolated restore |
| openapi.json, mcp-schema.json | API and MCP tool schemas of the candidate |
| contract-schema.json, receipt-schema.json, CONTRACT_SCHEMA_terms.py | WorkTerms v1 / acceptance policy / templates; receipt kinds and claims |
| accounting-model.md, ACCOUNTING_MODEL_journal.py | double-entry model, accounts, event flow, what replay proves |
| verify_work.py, verify_bundle.py | portable offline verifiers |
| work_client_example.py | HTTP-only client walkthrough (request → offer → award → evidence → verify → reconcile) |
| synthetic-fixtures/ | synthetic energy-determination and resource-plan fixtures |
| demonstration/ | §66 script, demonstration.json (17 steps), complete and restricted bundles of the negative contract, projections (private / collaborator / public-ready), offline verifier reports |
| journeys/ | forty journey results (journeys-economy.json) and the clean-export journey subset |
| browser-screenshots/ | Playwright evidence at desktop and narrow widths |
| benchmarks/work-benchmark.json | bounded concurrent workload measurements (same host, indicative) |
| fault-campaign.json | fault points, tests and outcomes |
| verification-results.json, test-logs/ | suite results and logs |
| privacy-scan.json, DELIVERY_SCANS.txt | privacy scan of results and packaged tree |
| ISSUES.md, ARCHITECTURE_LEDGER.md, SESSION_STATE.md | defects found and fixed, reuse ledger, session record |
| BASELINE_DIGESTS.txt, FINAL_DIGESTS.txt | identity / protocol file digests before and after |
| clean-export-logs/ | journey 40: reproduction from the archive in a fresh venv |
| MANIFEST_SHA256SUMS.txt | checksum manifest of every delivered file |
