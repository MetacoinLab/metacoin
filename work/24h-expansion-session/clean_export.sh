#!/usr/bin/env bash
# Clean export reproduction of the FINAL archive: unzip -> fresh venv from the lock (local wheels) -> documented commands;
# base (no-model) checks plus model-enabled paths when the pinned artifacts are installed under the model store.
set -u
ZIP="$1"; S="$2"; OUT="$(cd "$3" && pwd)"; FRESH="$S/fresh-24h"; rm -rf "$FRESH"; mkdir -p "$FRESH"
(cd "$FRESH" && unzip -q "$ZIP") && cd "$FRESH/metacoin-24h-expansion" || exit 1
echo "files: $(find . -type f | wc -l)"
python3 -m venv .venv && .venv/bin/pip install -q --no-index --find-links "$S/svcdeps" --find-links "$S/x402deps" --find-links "$S/evmdeps" --find-links "$S/extdeps" --find-links "$S/expansiondeps" -r metacoin_service/requirements.lock 2>&1 | tail -2
echo "venv rc=$?"
rc=0
run() { s=$(date +%s); env -i PATH="$PATH" HOME="$HOME" LANG=C.UTF-8 PYTHONPATH="$PWD" "$@" > "$OUT/$(echo "$*" | tr ' /.' '___' | cut -c1-80).log" 2>&1; r=$?; echo "rc=$r $(( $(date +%s)-s ))s :: $*"; [ $r -ne 0 ] && rc=1; }
run .venv/bin/python -m metacoin_service --home ./svc-home --provider-mode test-http init
run .venv/bin/python -m metacoin_service --home ./svc-home status
run .venv/bin/python -m unittest metacoin_service.tests.test_approvals_statements metacoin_service.tests.test_verification metacoin_service.tests.test_calibration metacoin_service.tests.test_federation metacoin_service.tests.test_mcp metacoin_service.tests.test_planner metacoin_service.tests.test_upto_route metacoin_service.tests.test_bundles_disagreements metacoin_service.tests.test_notebooks metacoin_service.tests.test_examples
run python3 -m unittest metacoin_service.tests.test_compute_science
run .venv/bin/python -m unittest integrations.x402.local_chain.test_local_chain
run .venv/bin/python -m unittest metacoin_service.tests.test_models metacoin_service.tests.test_knowledge
run .venv/bin/python -m metacoin_service.tests.journeys_expansion --only 1,3,8,11,16 --out "$OUT/journeys-from-clean-export.json"
echo "CLEAN_EXPORT rc=$rc"
