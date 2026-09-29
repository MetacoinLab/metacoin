#!/usr/bin/env bash
# Step recorder: run one command, capture its exit status FIRST, then append a JSON line to the ledger.
# usage: run_step.sh <ledger.jsonl> <step-name> <log-path> -- <command...>
LEDGER="$1"; NAME="$2"; LOG="$3"; shift 4
START=$(date -u +%Y-%m-%dT%H:%M:%SZ); T0=$(date +%s)
"$@" > "$LOG" 2>&1
RC=$?
END=$(date -u +%Y-%m-%dT%H:%M:%SZ)
printf '{"step":"%s","rc":%d,"start":"%s","end":"%s","seconds":%d,"log":"%s","cmd":"%s"}\n' "$NAME" "$RC" "$START" "$END" $(( $(date +%s) - T0 )) "$LOG" "$(printf '%s ' "$@" | sed 's/"/\\"/g' | cut -c1-300)" >> "$LEDGER"
exit $RC
