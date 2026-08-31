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

# Self-tests to run, in order. Entries may include arguments (e.g.
# "task_metering.py --selftest"); the invocation below word-splits the entry while
# keeping SCRIPT_DIR (which may contain spaces) intact — same pattern as
# protocol/run_protocol_selftests.sh.
TESTS=(
    "tasks/task_0001_lunar_link_budget.py"
    "tasks/task_0002_orbit_propagation.py"
    "tasks/task_0003_power_eclipse.py"
    "tasks/task_0004_comms_access.py"
    "tasks/task_0005_rover_path.py"
    "tasks/task_0006_docking_approach.py"
    "tasks/task_0007_hohmann_transfer.py"
    "tasks/task_0008_arm_inverse_kinematics.py"
    "tasks/task_0009_power_budget.py"
    "tasks/task_0010_thermal_equilibrium.py"
    "tasks/task_0011_ballistic_reentry.py"
    "tasks/task_0012_comms_link_budget.py"
    "tasks/task_0013_lambert_transfer.py"
    "tasks/task_0014_fdir_state_machine.py"
    "tasks/task_0015_sabatier_isru.py"
    "tasks/task_0016_triad_attitude.py"
    "tasks/task_0017_isru_ascent_budget.py"
    "tasks/task_0018_ascent_feasibility.py"
    "tasks/cea_thermo_pinned.py"
    "tasks/task_0019_sabatier_equilibrium_constant.py"
    "tasks/task_0020_sabatier_conversion_equilibrium.py"
    "tasks/task_0021_conversion_corrected_ascent.py"
    "tasks/task_0022_insolation_offset_requirement.py"
    "tasks/task_0023_sub_l1_shade_geometry.py"
    "tasks/task_0024_shade_mass_budget.py"
    "tasks/task_0025_regolith_feedstock_energy.py"
    "tasks/task_0026_mass_driver_energetics.py"
    "tasks/task_0027_deployment_timeline_verdict.py"
    "tasks/task_0028_l1_dust_persistence.py"
    "tasks/task_0029_shade_longevity_horizon.py"
    "verify_gates.py"
    "test_meta_faucet.py"
    "x402_spend_stub.py"
    "agent_loop.py"
    "economy_demo.py"
    "task_metering.py --selftest"
    "metastar_treasury.py --selftest"
    "flow1_uptime.py --selftest"
    "participant_kit.py --selftest"
)

# Parallel arrays: names and their captured exit codes.
NAMES=()
CODES=()

for test in "${TESTS[@]}"; do
    echo
    echo "============================================================"
    echo ">>> RUNNING: $test"
    echo "============================================================"

    # Quoted prefix keeps a space-containing SCRIPT_DIR intact; unquoted $test
    # word-splits so any trailing arguments (e.g. --selftest) are passed separately.
    python3 "$SCRIPT_DIR/"$test
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
