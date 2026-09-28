#!/usr/bin/env bash
# Journey 24: reproduce the FINAL archive in a clean directory: unzip -> fresh venv from the lock (local wheel caches only,
# --no-index) -> init/status -> focused suites -> local chain -> model/document/optimization/report/package journeys.
# Model and OCR artifacts are NOT in the archive: the pinned model store and the compute interpreter's user site are
# discovered through the documented settings and their identities checked by the registry (weight digests) at registration.
set -u
ZIP="$1"; S="$2"; OUT="$(cd "$3" && pwd)"; FRESH="$S/fresh-ws"; rm -rf "$FRESH"; mkdir -p "$FRESH"
(cd "$FRESH" && unzip -q "$ZIP") && cd "$FRESH/metacoin-scientific-workspace" || exit 1
echo "files: $(find . -type f | wc -l)"; echo "sha256 of archive: $(sha256sum "$ZIP" | cut -d' ' -f1)"
python3 -m venv .venv && .venv/bin/pip install -q --no-index --find-links "$S/svcdeps" --find-links "$S/x402deps" --find-links "$S/evmdeps" --find-links "$S/extdeps" --find-links "$S/expansiondeps" --find-links "$S/scideps" -r metacoin_service/requirements.lock 2>&1 | tail -2
echo "venv rc=$?"
rc=0
run() { s=$(date +%s); env -i PATH="$PATH" HOME="$HOME" LANG=C.UTF-8 PYTHONPATH="$PWD" METACOIN_PLAYWRIGHT_PYTHON="$S/pwvenv/bin/python" PLAYWRIGHT_BROWSERS_PATH="$S/pw-browsers" "$@" > "$OUT/$(echo "$*" | tr ' /.' '___' | cut -c1-80).log" 2>&1; r=$?; echo "rc=$r $(( $(date +%s)-s ))s :: $*"; [ $r -ne 0 ] && rc=1; }
run .venv/bin/python -m metacoin_service --home ./svc-home --provider-mode test-http init
run .venv/bin/python -m metacoin_service --home ./svc-home status
run .venv/bin/python -m unittest metacoin_service.tests.test_documents metacoin_service.tests.test_resource_plan_service metacoin_service.tests.test_analyses metacoin_service.tests.test_packages metacoin_service.tests.test_intent metacoin_service.tests.test_verification metacoin_service.tests.test_reconciliation metacoin_service.tests.test_document_compare
run /usr/bin/python3 -m unittest metacoin_service.tests.test_resource_plan metacoin_service.tests.test_compute_science
run .venv/bin/python -m unittest integrations.x402.local_chain.test_local_chain metacoin_service.tests.test_packages_upto
run .venv/bin/python -m unittest metacoin_service.tests.test_generation_batching metacoin_service.tests.test_generation_continuous metacoin_service.tests.test_backlog_late
run .venv/bin/python -m metacoin_service.tests.journeys_workspace --only 1,2,4,6,8,11,14,16,17,18 --out "$OUT/journeys-from-clean-export.json"
echo "CLEAN_EXPORT rc=$rc"
