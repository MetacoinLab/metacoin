# Delivery index — evidence acquisition and scoped assurance (order of 2026-10-22)

| file / directory | what it is |
|---|---|
| IMPLEMENTATION_REPORT.md | what a user can do now; identities; status classes; verification; scoped scientific statements; defects fixed |
| HANDOFF.md | publication status (nothing pushed), how to resume, what the owner must decide |
| ARCHITECTURE.md | new behaviour and scientific assumptions, what is not claimed |
| FEATURE_LEDGER.json | implemented / verified locally / externally unverified / deferred, with entry points and evidence |
| CANDIDATE.json, BASE_REVISION.txt, SOURCE_REVISION.txt, commits.txt, RECORDS_COMMIT.txt | exact identities |
| metacoin-evidence-acquisition-source.zip, *.patch, patch-check.json | source archive of the candidate and the session patch against its base, verified in isolation |
| MIGRATIONS.json | migration 041 (seven additive objects), rollback and recovery notes for SQLite |
| docs/ | OpenAPI (all routes and the decision subset), CLI command help, MCP decision tools, assurance and decision schemas — generated from the running implementation |
| decision_replay.py, decision_checker.py, decision_kernels.py, ASSURANCE_SCHEMA_assurance.py, verify_work.py | portable verification code |
| example-bundle/ | private-state-free decision and analysis bundles, TRUST_ROOT.txt, REPLAY_COMMAND.txt, replay outputs, fixture-results.json (numerical fixtures and independent-checker outputs) |
| journeys/ | the 26 acceptance journeys (journeys-decision.json) and the clean-export subset |
| browser-screenshots/ | Playwright evidence (journey 24) |
| test-logs/, verification-results.json | inherited unit suite, science, chain, decision suites, journeys |
| clean-export-logs/ | journey 26: reproduction from the archive in a fresh offline venv |
| privacy-scan.json, DELIVERY_SCANS.txt, MANIFEST_SHA256SUMS.txt | scans and checksums |
| IDENTITY_DIGESTS.txt | identity, protocol and monetary files unchanged against the base |
| SESSION_STATE.md, ISSUES (in SESSION_STATE), live-upgrade-* | session record; the live service was not changed by this order |
