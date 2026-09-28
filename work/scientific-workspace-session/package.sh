#!/usr/bin/env bash
# Assemble the Order 07 delivery directory with an explicit allowlist. Never copies keys, credentials, live databases,
# model weights, private prompts/documents, vectors, node identities, wallets, venvs or environment files.
set -u
DELIVER="$1"; A="$2"; BASE="$(cat "$A/BASE_REVISION.txt")"; HEAD="$(git rev-parse HEAD)"
mkdir -p "$DELIVER/test-logs" "$DELIVER/clean-export-logs" "$DELIVER/journeys" "$DELIVER/benchmarks" "$DELIVER/browser-screenshots" "$DELIVER/manifests" "$DELIVER/examples"
git diff "$BASE" "$HEAD" -- . ':!work' > "$DELIVER/metacoin-scientific-workspace-session-vs-${BASE:0:7}.patch"
git log --format='%H %s' "$BASE..$HEAD" > "$DELIVER/commits.txt"
echo "$HEAD" > "$DELIVER/SOURCE_REVISION.txt"; echo "$BASE" > "$DELIVER/BASE_REVISION.txt"
TMPX="$(mktemp -d)"; git archive --format=tar --prefix=metacoin-scientific-workspace/ "$HEAD" | tar -x -C "$TMPX" && rm -rf "$TMPX/metacoin-scientific-workspace/work" && (cd "$TMPX" && zip -qr "$DELIVER/metacoin-scientific-workspace-source.zip" metacoin-scientific-workspace) && rm -rf "$TMPX"
git ls-files | grep -v '^work/' > "$DELIVER/SOURCE_ALLOWLIST.txt"
cp metacoin_service/requirements.lock "$DELIVER/requirements-service.lock"
cp metacoin_service/requirements-compute.txt "$DELIVER/requirements-compute.txt"
cp metacoin_service/README.md "$DELIVER/SERVICE_README.md"
cp metacoin_service/verify_bundle.py "$DELIVER/verify_bundle.py"
cp metacoin_service/compute/resource_plan.py "$DELIVER/SCIENTIFIC_MODEL_resource_plan.py"
cp -r metacoin_service/tests/document_fixtures "$DELIVER/synthetic-document-fixtures"
cp work/scientific-workspace-session/SESSION_STATE.md "$DELIVER/"
cp "$A"/logs/*.log "$DELIVER/test-logs/" 2>/dev/null
cp "$A"/journeys/*.json "$DELIVER/journeys/" 2>/dev/null
cp "$A"/benchmarks/*.json "$A"/*benchmark*.json "$A"/*benchmark*.log "$DELIVER/benchmarks/" 2>/dev/null
cp "$A"/clean-export/* "$DELIVER/clean-export-logs/" 2>/dev/null
cp "$A"/shots/* "$DELIVER/browser-screenshots/" 2>/dev/null
cp "$A"/manifests/* "$DELIVER/manifests/" 2>/dev/null
cp "$A"/examples/* "$DELIVER/examples/" 2>/dev/null
for f in FEATURE_LEDGER.json MIGRATIONS.json verification-results.json IMPLEMENTATION_REPORT.md DELIVERY_INDEX.md ISSUES.md openapi.json mcp-schema.json live-status.json live-capabilities.json live-upgrade.json local-chain-record.json eval-set-agent-behavior-v2.json document-extraction-results-baseline.json generation-batching-benchmark.json continuous-equality.json adaptive-campaign-benchmark.json failure-campaign.json producers.jsonl recorder-selftest.jsonl; do
  [ -f "$A/$f" ] && cp "$A/$f" "$DELIVER/"
done
echo "== scans ($(date -u +%Y-%m-%dT%H:%M:%SZ)) archive $HEAD base $BASE" > "$DELIVER/DELIVERY_SCANS.txt"
{ unzip -Z1 "$DELIVER/metacoin-scientific-workspace-source.zip" | grep -iE "\.age$|\.ed25519|bootstrap\.json|sqlite|\.venv|\.env$|/work/|node-.*\.json$|\.safetensors|\.npy$|\.onnx$|artifacts/[0-9a-f]{32}" && echo "PRIVATE/UNWANTED MATERIAL IN ARCHIVE"; } || echo "archive allowlist ok: no keys, credentials, databases, weights, vectors, node identities, venv or work dir" >> "$DELIVER/DELIVERY_SCANS.txt"
{ grep -rlE "mck_[A-Za-z0-9_-]{20}|mcn_[A-Za-z0-9_-]{20}" "$DELIVER" --include='*.json' --include='*.log' --include='*.txt' --include='*.patch' --include='*.md' | grep -v -E "openapi|capabilities" && echo "TOKEN-LIKE STRING FOUND"; } || echo "no bearer/node-credential-shaped strings in logs/results" >> "$DELIVER/DELIVERY_SCANS.txt"
{ grep -rl "AGE-SECRET-KEY\|BEGIN PRIVATE KEY\|private_key_hex" "$DELIVER" | grep -v -E "\.patch$|SERVICE_README|openapi|source\.zip" && echo "SECRET MATERIAL FOUND"; } || echo "no secret key material outside source text" >> "$DELIVER/DELIVERY_SCANS.txt"
{ grep -rlE "RP_TEST|ANALYSIS_PRIVATE_LABEL|CANARY-|hunter2|/home/zhangd2/\.local/state" "$DELIVER" --include='*.json' --include='*.md' --include='*.txt' --include='*.log' | grep -v -E "SOURCE_ALLOWLIST|\.patch$" && echo "PRIVATE LABEL / PATH IN RESULTS (review)"; } || echo "no private labels or live-state paths in results" >> "$DELIVER/DELIVERY_SCANS.txt"
unzip -tq "$DELIVER/metacoin-scientific-workspace-source.zip" >> "$DELIVER/DELIVERY_SCANS.txt" 2>&1
(cd "$DELIVER" && find . -type f ! -name MANIFEST_SHA256SUMS.txt -print0 | sort -z | xargs -0 sha256sum > MANIFEST_SHA256SUMS.txt && sha256sum -c --quiet MANIFEST_SHA256SUMS.txt && echo "manifest ok: $(wc -l < MANIFEST_SHA256SUMS.txt) files")
cat "$DELIVER/DELIVERY_SCANS.txt"; du -sh "$DELIVER"
