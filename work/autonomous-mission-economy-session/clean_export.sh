#!/usr/bin/env bash
# Journey 36 (reproduction part): unzip the source archive into a clean directory -> fresh venv from the lock (local wheel caches,
# --no-index) -> the documented clean-start sequence (CLEAN_START.md) -> unit suites -> provider client conformance in a second
# standard-library-only venv -> a bounded subset of the mission journeys through real processes with x402 disabled.
set -u
ZIP="$1"; S="$2"; OUT="$(cd "$3" && pwd)"; FRESH="$S/fresh-mission"; rm -rf "$FRESH"; mkdir -p "$FRESH"; NAME=metacoin-autonomous-mission-economy
(cd "$FRESH" && unzip -q "$ZIP") && cd "$FRESH/$NAME" || exit 1
PD="$(dirname "$ZIP")/protocol-data"; [ -d "$PD" ] && { cp "$PD/ledger_data.jsonl" protocol/ledger_data.jsonl; cp "$PD/mission_verdict.json" mission_verdict.json; echo "runtime data placed from protocol-data/"; }
echo "files: $(find . -type f | wc -l)"; echo "sha256 of archive: $(sha256sum "$ZIP" | cut -d' ' -f1)"
python3 -m venv .venv && .venv/bin/pip install -q --no-index --find-links "$S/svcdeps" --find-links "$S/x402deps" --find-links "$S/evmdeps" --find-links "$S/extdeps" --find-links "$S/expansiondeps" --find-links "$S/scideps" -r metacoin_service/requirements.lock 2>&1 | tail -2
echo "venv rc=$?"
rc=0
run() { s=$(date +%s); env -i PATH="$PATH" HOME="$HOME" LANG=C.UTF-8 PYTHONPATH="$PWD" METACOIN_X402_DISABLED=1 "$@" > "$OUT/$(echo "$*" | tr ' /.' '___' | cut -c1-80).log" 2>&1; r=$?; echo "rc=$r $(( $(date +%s)-s ))s :: $*"; [ $r -ne 0 ] && rc=1; }
# the documented clean start (CLEAN_START.md): one command brings up API + worker + signer + witness; status; down
run .venv/bin/python -m metacoin_service.deploy_local up --home ./local-home --domain-id dom-clean
run .venv/bin/python -m metacoin_service.deploy_local status --home ./local-home
run .venv/bin/python -m metacoin_service.deploy_local down --home ./local-home
run .venv/bin/python -m unittest metacoin_service.tests.test_native_protocol metacoin_service.tests.test_native_signer metacoin_service.tests.test_native_market metacoin_service.tests.test_native_missions metacoin_service.tests.test_native_planner metacoin_service.tests.test_native_lineage metacoin_service.tests.test_native_conformance
python3 -m venv .venv-client && (cd provider_client && PYTHONPATH="$PWD" ../.venv-client/bin/python -m metacoin_provider conformance > "$OUT/provider-conformance.json" 2>&1); echo "provider conformance rc=$? ($(python3 -c "import json; d=json.load(open('$OUT/provider-conformance.json')); print(d['passed'], 'of', d['cases'])" 2>/dev/null))"
run .venv/bin/python -m metacoin_service.native.vectors
run .venv/bin/python -m metacoin_service.tests.journeys_mission --only 1,2,3,8,9,10,13,15,17,18,19,20,22,24,26,27,29,33,34 --out "$OUT/journeys-mission-from-clean-export.json"
python3 - "$OUT/journeys-mission-from-clean-export.json" "$ZIP" <<'PY'
import json, sys, hashlib
p, z = sys.argv[1:3]; d = json.load(open(p)); d['archive_sha256'] = hashlib.sha256(open(z, 'rb').read()).hexdigest(); json.dump(d, open(p, 'w'), indent=1)
PY
echo "CLEAN_EXPORT rc=$rc"
