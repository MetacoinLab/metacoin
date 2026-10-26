#!/usr/bin/env bash
# Post-endurance pipeline at the candidate (order 2026-10-23 §22–23): full journeys → ledger → benchmarks → package (pass 1) →
# clean export from the archive (journey 49 input) → full journeys with the endurance / clean-export / ledger inputs (48–50) →
# ledger → candidate record → report. Records commit, package pass 2 and the fresh-extraction check follow by hand.
#   finalize.sh <artifacts dir> <delivery dir> <deps scratch dir (svcdeps…)> <playwright python> <playwright browsers> <base worktree>
set -u
A="$1"; DELIVER="$2"; DEPS="$3"; PWPY="$4"; PWB="$5"; BASEWT="$6"; SESSION="$(dirname "$0")"; PY=.venv-service/bin/python
export METACOIN_X402_DISABLED=1 PYTHONPATH=. METACOIN_PLAYWRIGHT_PYTHON="$PWPY" PLAYWRIGHT_BROWSERS_PATH="$PWB" METACOIN_BASE_TREE="$BASEWT"
HEAD="$(git rev-parse HEAD)"; echo "finalize at $HEAD $(date -u +%H:%M:%SZ)"
step() { echo "== $1 $(date -u +%H:%M:%SZ)"; }
step "journeys (pass A)"; $PY -m metacoin_service.tests.journeys_network --extensions --shots "$A/shots" --demo "$A/demonstration/demonstration.json" --out "$A/journeys/journeys-network.json" > "$A/logs/journeys-network-passA.log" 2>&1; echo "rc=$?"; tail -1 "$A/logs/journeys-network-passA.log" | cut -c1-200
step "ledger"; $PY "$SESSION/ledger_build.py" "$A" | tail -1
step "benchmarks"; METACOIN_BENCH_WORKTREE="$BASEWT" $PY "$SESSION/run_benchmark.py" "$A/benchmarks" > "$A/logs/benchmark-run.log" 2>&1; echo "rc=$?"; grep -E "rc [1-9]" "$A/logs/benchmark-run.log" || echo "all benchmark modules rc 0"
step "package pass 1"; METACOIN_PACKAGE_REVISION="$HEAD" bash "$SESSION/package.sh" "$DELIVER" "$A" > "$A/logs/package-pass1.log" 2>&1; echo "rc=$?"; tail -3 "$A/logs/package-pass1.log"
step "clean export"; mkdir -p "$A/clean-export"; bash "$SESSION/clean_export.sh" "$DELIVER/metacoin-independent-machine-network-source.zip" "$DEPS" "$A/clean-export" > "$A/logs/clean-export-run.log" 2>&1; echo "rc=$?"; tail -4 "$A/logs/clean-export-run.log"
step "journeys (pass B, with inputs)"; METACOIN_NETWORK_ENDURANCE_JSON="$A/benchmarks/endurance-network.json" METACOIN_NETWORK_CLEAN_EXPORT_JSON="$A/clean-export/journeys-network-from-clean-export.json" METACOIN_NETWORK_FEATURE_LEDGER="$A/FEATURE_LEDGER.json" $PY -m metacoin_service.tests.journeys_network --extensions --shots "$A/shots" --demo "$A/demonstration/demonstration.json" --out "$A/journeys/journeys-network.json" > "$A/logs/journeys-network-passB.log" 2>&1; echo "rc=$?"; tail -1 "$A/logs/journeys-network-passB.log" | cut -c1-200
step "ledger + candidate + report"; $PY "$SESSION/ledger_build.py" "$A" | tail -1; $PY "$SESSION/candidate_build.py" "$A"; $PY "$SESSION/report_build.py" "$A"
echo "FINALIZE DONE $(date -u +%H:%M:%SZ)"
