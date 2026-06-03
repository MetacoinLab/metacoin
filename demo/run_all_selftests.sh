#!/usr/bin/env bash
#
# run_all_selftests.sh — single entry point for all demo self-tests.
#
# Research-only. Runs each demo self-test in turn, captures its exit code,
# and prints a summary table at the end. Exits non-zero if any test failed.
#
# Note: deliberately does NOT use `set -e`. We want every self-test to run
# even if an earlier one fails, so we capture exit codes individually and
# report the full picture instead of aborting on the first failure.

# Resolve the demo/ directory so the script works from any CWD.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Self-tests to run, in order.
TESTS=(
    "tasks/task_0001_lunar_link_budget.py"
    "tasks/task_0002_orbit_propagation.py"
    "tasks/task_0003_power_eclipse.py"
    "verify_gates.py"
    "test_meta_faucet.py"
    "x402_spend_stub.py"
    "agent_loop.py"
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
