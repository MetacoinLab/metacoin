#!/usr/bin/env bash
#
# run_protocol_selftests.sh — single entry point for all protocol self-tests.
#
# Research-stage. Runs each protocol self-test in turn, captures its exit code,
# and prints a summary table at the end. Exits non-zero if any test failed.
#
# Each protocol self-test uses a temporary file and never the default runtime
# ledger (protocol/ledger_data.jsonl), so running this leaves no stray state.
#
# Note: deliberately does NOT use `set -e`. We want every self-test to run
# even if an earlier one fails, so we capture exit codes individually and
# report the full picture instead of aborting on the first failure.

# Resolve the protocol/ directory so the script works from any CWD.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Self-tests to run, in order. Add future protocol self-tests here
# (e.g. R2 attestation, R3 multi-node) and they are picked up automatically.
TESTS=(
    "ledger.py"
    "attest.py"
    "external_verifier.py"
    "audit.py"
)

# Parallel arrays: names and their captured exit codes.
NAMES=()
CODES=()

for test in "${TESTS[@]}"; do
    echo
    echo "============================================================"
    echo ">>> RUNNING: $test"
    echo "============================================================"

    python3 "$SCRIPT_DIR/$test"
    code=$?

    NAMES+=("$test")
    CODES+=("$code")

    echo "--- exit code: $code ---"
done

# Summary table.
echo
echo "============================================================"
echo "SUMMARY"
echo "============================================================"

failures=0
for i in "${!NAMES[@]}"; do
    if [[ "${CODES[$i]}" -eq 0 ]]; then
        status="PASS"
    else
        status="FAIL"
        failures=$((failures + 1))
    fi
    printf "  %-4s (exit %-3s)  %s\n" "$status" "${CODES[$i]}" "${NAMES[$i]}"
done

echo "------------------------------------------------------------"
total="${#NAMES[@]}"
passed=$((total - failures))
echo "  $passed/$total passed, $failures failed"
echo "============================================================"

# Exit non-zero if any self-test failed.
if [[ "$failures" -gt 0 ]]; then
    exit 1
fi
exit 0
