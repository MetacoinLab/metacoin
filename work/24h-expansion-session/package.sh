#!/usr/bin/env bash
# Assemble the delivery directory with an explicit allowlist. Never copies keys, credentials, live databases,
# model weights, private prompts/documents, vectors, node identities, wallets, venvs or environment files.
set -u
DELIVER="$1"; A="$2"; BASE=3b6f292acfa258c07829021d0e09a2a10148d32d; HEAD="$(git rev-parse HEAD)"
mkdir -p "$DELIVER/test-logs" "$DELIVER/clean-export-logs" "$DELIVER/journeys" "$DELIVER/benchmarks" "$DELIVER/browser-screenshots" "$DELIVER/manifests"
git diff "$BASE" "$HEAD" -- . ':!work' > "$DELIVER/metacoin-24h-expansion-session-vs-3b6f292.patch"
git log --format='%H %s' "$BASE..$HEAD" > "$DELIVER/commits.txt"
echo "$HEAD" > "$DELIVER/SOURCE_REVISION.txt"
git archive --format=zip --prefix=metacoin-24h-expansion/ -o "$DELIVER/metacoin-24h-expansion-source.zip" "$HEAD"
git ls-files > "$DELIVER/SOURCE_ALLOWLIST.txt"
cp metacoin_service/requirements.lock "$DELIVER/requirements-service.lock"
cp metacoin_service/README.md "$DELIVER/SERVICE_README.md"
cp work/24h-expansion-session/SESSION_STATE.md "$DELIVER/"
cp "$A"/logs/*.log "$DELIVER/test-logs/" 2>/dev/null
cp "$A"/journeys/*.json "$DELIVER/journeys/" 2>/dev/null
cp "$A"/benchmarks/*.json "$DELIVER/benchmarks/" 2>/dev/null
cp "$A"/clean-export/* "$DELIVER/clean-export-logs/" 2>/dev/null
cp "$A"/shots/* "$DELIVER/browser-screenshots/" 2>/dev/null
cp "$A"/manifests/* "$DELIVER/manifests/" 2>/dev/null
for f in FEATURE_LEDGER.json MIGRATIONS.json verification-results.json IMPLEMENTATION_REPORT.md openapi.json live-status.json live-capabilities.json local-chain-record.json knowledge-eval.json endurance.json failure-campaign.json mcp-schema.json; do
  [ -f "$A/$f" ] && cp "$A/$f" "$DELIVER/"
done
echo "== scans ($(date -u +%Y-%m-%dT%H:%M:%SZ)) archive $HEAD" > "$DELIVER/DELIVERY_SCANS.txt"
{ unzip -Z1 "$DELIVER/metacoin-24h-expansion-source.zip" | grep -iE "\.age$|\.ed25519|bootstrap\.json|sqlite|\.venv|\.env$|/work/|node-.*\.json$|\.safetensors|\.npy$|artifacts/[0-9a-f]{32}" && echo "PRIVATE/UNWANTED MATERIAL IN ARCHIVE"; } || echo "archive allowlist ok: no keys, credentials, databases, weights, vectors, node identities, venv or work dir" >> "$DELIVER/DELIVERY_SCANS.txt"
{ grep -rlE "mck_[A-Za-z0-9_-]{20}|mcn_[A-Za-z0-9_-]{20}" "$DELIVER" --include='*.json' --include='*.log' --include='*.txt' --include='*.patch' --include='*.md' | grep -v -E "openapi|capabilities" && echo "TOKEN-LIKE STRING FOUND"; } || echo "no bearer/node-credential-shaped strings in logs/results" >> "$DELIVER/DELIVERY_SCANS.txt"
{ grep -rl "AGE-SECRET-KEY\|BEGIN PRIVATE KEY\|private_key_hex" "$DELIVER" | grep -v -E "\.patch$|SERVICE_README|openapi|source\.zip" && echo "SECRET MATERIAL FOUND"; } || echo "no secret key material outside source text" >> "$DELIVER/DELIVERY_SCANS.txt"
{ grep -rl "hunter2" "$DELIVER" | grep -v -E "\.patch$|source\.zip|SOURCE_ALLOWLIST" && echo "EVAL CORPUS TEXT IN RESULTS"; } || echo "no private document text in results" >> "$DELIVER/DELIVERY_SCANS.txt"
unzip -tq "$DELIVER/metacoin-24h-expansion-source.zip" >> "$DELIVER/DELIVERY_SCANS.txt" 2>&1
(cd "$DELIVER" && find . -type f ! -name MANIFEST_SHA256SUMS.txt -print0 | sort -z | xargs -0 sha256sum > MANIFEST_SHA256SUMS.txt && sha256sum -c --quiet MANIFEST_SHA256SUMS.txt && echo "manifest ok: $(wc -l < MANIFEST_SHA256SUMS.txt) files")
cat "$DELIVER/DELIVERY_SCANS.txt"; du -sh "$DELIVER"
