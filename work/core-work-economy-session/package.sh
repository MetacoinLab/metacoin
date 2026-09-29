#!/usr/bin/env bash
# Assemble the Order 08 delivery directory with an explicit allowlist. Never copies keys, credentials, live databases,
# model weights, private prompts/documents, vectors, node identities, wallets, venvs or environment files.
set -u
DELIVER="$1"; A="$2"; BASE="$(cat "$A/BASE_REVISION.txt")"; HEAD="$(git rev-parse HEAD)"
mkdir -p "$DELIVER/test-logs" "$DELIVER/clean-export-logs" "$DELIVER/journeys" "$DELIVER/benchmarks" "$DELIVER/browser-screenshots" "$DELIVER/manifests" "$DELIVER/examples"
git diff "$BASE" "$HEAD" -- . ':!work' > "$DELIVER/metacoin-core-work-economy-session-vs-${BASE:0:7}.patch"
git log --format='%H %s' "$BASE..$HEAD" > "$DELIVER/commits.txt"
echo "$HEAD" > "$DELIVER/SOURCE_REVISION.txt"; echo "$BASE" > "$DELIVER/BASE_REVISION.txt"
TMPX="$(mktemp -d)"; git archive --format=tar --prefix=metacoin-core-work-economy/ "$HEAD" | tar -x -C "$TMPX" && rm -rf "$TMPX/metacoin-core-work-economy/work" && (cd "$TMPX" && zip -qr "$DELIVER/metacoin-core-work-economy-source.zip" metacoin-core-work-economy) && rm -rf "$TMPX"
git ls-files | grep -v '^work/' > "$DELIVER/SOURCE_ALLOWLIST.txt"
cp metacoin_service/requirements.lock "$DELIVER/requirements-service.lock"
cp metacoin_service/requirements-compute.txt "$DELIVER/requirements-compute.txt"
cp metacoin_service/README.md "$DELIVER/SERVICE_README.md"
cp metacoin_service/verify_bundle.py "$DELIVER/verify_bundle.py"
cp metacoin_service/economy/verify_work.py "$DELIVER/verify_work.py"
cp metacoin_service/examples/work_client.py "$DELIVER/work_client_example.py"
cp metacoin_service/economy/terms.py "$DELIVER/CONTRACT_SCHEMA_terms.py"
cp metacoin_service/economy/journal.py "$DELIVER/ACCOUNTING_MODEL_journal.py"
mkdir -p "$DELIVER/synthetic-fixtures" "$DELIVER/demonstration" "$DELIVER/protocol-data"
# runtime data the public repo deliberately does not track (.gitignore): the operator's anchored ledger data and mission verdict.
# They are required by legacy_task_replay (registered hashes) and the mission portfolio; the clean-export run copies them into
# the fresh tree, and README.txt says where they belong.
cp protocol/ledger_data.jsonl "$DELIVER/protocol-data/ledger_data.jsonl"; cp mission_verdict.json "$DELIVER/protocol-data/mission_verdict.json"
sha256sum protocol/ledger_data.jsonl mission_verdict.json > "$DELIVER/protocol-data/SHA256SUMS.txt"
printf '%s\n' "Runtime data outside the git archive (gitignored in the public repo by design):" "  protocol-data/ledger_data.jsonl   -> <repo>/protocol/ledger_data.jsonl   (anchored ledger entries; registered task hashes read by legacy_task_replay)" "  protocol-data/mission_verdict.json -> <repo>/mission_verdict.json        (anchored mission verdict; read-only source of the mission portfolio)" "Copy them to those paths before running the service, the journeys or the demonstration from the archive. Digests in SHA256SUMS.txt match BASELINE_DIGESTS.txt / FINAL_DIGESTS.txt." > "$DELIVER/protocol-data/README.txt"
cp experiments/work_contracts/fixtures.py "$DELIVER/synthetic-fixtures/work_contract_fixtures.py"
cp metacoin_service/tests/test_resource_plan_service.py "$DELIVER/synthetic-fixtures/resource_plan_sample_fixture.py" 2>/dev/null
cp metacoin_service/tests/demo_work_economy.py "$DELIVER/demonstration/demo_work_economy.py"
cp "$A"/demonstration/* "$DELIVER/demonstration/" 2>/dev/null
cp "$A"/interpreters.json "$DELIVER/" 2>/dev/null
cp work/core-work-economy-session/SESSION_STATE.md "$DELIVER/"
cp "$A"/logs/*.log "$DELIVER/test-logs/" 2>/dev/null
cp "$A"/journeys/*.json "$DELIVER/journeys/" 2>/dev/null
cp "$A"/benchmarks/*.json "$A"/*benchmark*.json "$A"/*benchmark*.log "$DELIVER/benchmarks/" 2>/dev/null
cp "$A"/clean-export/* "$DELIVER/clean-export-logs/" 2>/dev/null
cp "$A"/shots/* "$DELIVER/browser-screenshots/" 2>/dev/null
cp "$A"/manifests/* "$DELIVER/manifests/" 2>/dev/null
cp "$A"/examples/* "$DELIVER/examples/" 2>/dev/null
for f in FEATURE_LEDGER.json MIGRATIONS.json INTERPRETERS.json CANDIDATE.json live-upgrade-history.json live-upgrade.log live-journal-replay.json live-work-terms-inspect.json patch-check.json live-health.json migrate.json backup-manifest.json restore-check.json verification-results.json IMPLEMENTATION_REPORT.md DELIVERY_INDEX.md ISSUES.md ARCHITECTURE_LEDGER.md BASELINE_DIGESTS.txt FINAL_DIGESTS.txt openapi.json mcp-schema.json contract-schema.json receipt-schema.json accounting-model.md live-status.json live-capabilities.json live-upgrade.json work-benchmark.json fault-campaign.json demonstration.json privacy-scan.json producers.jsonl; do
  [ -f "$A/$f" ] && cp "$A/$f" "$DELIVER/"
done
echo "== scans ($(date -u +%Y-%m-%dT%H:%M:%SZ)) archive $HEAD base $BASE" > "$DELIVER/DELIVERY_SCANS.txt"
{ unzip -Z1 "$DELIVER/metacoin-core-work-economy-source.zip" | grep -iE "\.age$|\.ed25519|bootstrap\.json|sqlite|\.venv|\.env$|/work/|node-.*\.json$|\.safetensors|\.npy$|\.onnx$|artifacts/[0-9a-f]{32}" && echo "PRIVATE/UNWANTED MATERIAL IN ARCHIVE"; } || echo "archive allowlist ok: no keys, credentials, databases, weights, vectors, node identities, venv or work dir" >> "$DELIVER/DELIVERY_SCANS.txt"
{ grep -rlE "mck_[A-Za-z0-9_-]{20}|mcn_[A-Za-z0-9_-]{20}" "$DELIVER" --include='*.json' --include='*.log' --include='*.txt' --include='*.patch' --include='*.md' | grep -v -E "openapi|capabilities" && echo "TOKEN-LIKE STRING FOUND"; } || echo "no bearer/node-credential-shaped strings in logs/results" >> "$DELIVER/DELIVERY_SCANS.txt"
{ grep -rl "AGE-SECRET-KEY\|BEGIN PRIVATE KEY\|private_key_hex" "$DELIVER" | grep -v -E "\.patch$|SERVICE_README|openapi|source\.zip" && echo "SECRET MATERIAL FOUND"; } || echo "no secret key material outside source text" >> "$DELIVER/DELIVERY_SCANS.txt"
{ grep -rlE "RP_TEST|ANALYSIS_PRIVATE_LABEL|CANARY-|hunter2|TERMS_TEST_|CLIENT_run-|BROWSER_J|/home/zhangd2/\.local/state" "$DELIVER" --include='*.json' --include='*.md' --include='*.txt' --include='*.log' | grep -v -E "SOURCE_ALLOWLIST|\.patch$" && echo "PRIVATE LABEL / PATH IN RESULTS (review)"; } || echo "no private labels or live-state paths in results" >> "$DELIVER/DELIVERY_SCANS.txt"
unzip -tq "$DELIVER/metacoin-core-work-economy-source.zip" >> "$DELIVER/DELIVERY_SCANS.txt" 2>&1
(cd "$DELIVER" && find . -type f ! -name MANIFEST_SHA256SUMS.txt -print0 | sort -z | xargs -0 sha256sum > MANIFEST_SHA256SUMS.txt && sha256sum -c --quiet MANIFEST_SHA256SUMS.txt && echo "manifest ok: $(wc -l < MANIFEST_SHA256SUMS.txt) files")
cat "$DELIVER/DELIVERY_SCANS.txt"; du -sh "$DELIVER"
