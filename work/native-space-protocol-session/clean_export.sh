#!/usr/bin/env bash
# Journey 39: reproduce the archive in a clean directory: unzip -> fresh venv from the lock (local wheel caches, --no-index) ->
# init -> native and lunar suites -> a bounded subset of the native journeys through real processes with x402 disabled.
set -u
ZIP="$1"; S="$2"; OUT="$(cd "$3" && pwd)"; FRESH="$S/fresh-native"; rm -rf "$FRESH"; mkdir -p "$FRESH"
(cd "$FRESH" && unzip -q "$ZIP") && cd "$FRESH/metacoin-native-space-protocol" || exit 1
PD="$(dirname "$ZIP")/protocol-data"; [ -d "$PD" ] && { cp "$PD/ledger_data.jsonl" protocol/ledger_data.jsonl; cp "$PD/mission_verdict.json" mission_verdict.json; echo "runtime data placed from protocol-data/"; }
echo "files: $(find . -type f | wc -l)"; echo "sha256 of archive: $(sha256sum "$ZIP" | cut -d' ' -f1)"
python3 -m venv .venv && .venv/bin/pip install -q --no-index --find-links "$S/svcdeps" --find-links "$S/x402deps" --find-links "$S/evmdeps" --find-links "$S/extdeps" --find-links "$S/expansiondeps" --find-links "$S/scideps" -r metacoin_service/requirements.lock 2>&1 | tail -2
echo "venv rc=$?"
rc=0
run() { s=$(date +%s); env -i PATH="$PATH" HOME="$HOME" LANG=C.UTF-8 PYTHONPATH="$PWD" METACOIN_X402_DISABLED=1 "$@" > "$OUT/$(echo "$*" | tr ' /.' '___' | cut -c1-80).log" 2>&1; r=$?; echo "rc=$r $(( $(date +%s)-s ))s :: $*"; [ $r -ne 0 ] && rc=1; }
run .venv/bin/python -m metacoin_service --home ./svc-home --provider-mode simulation init
run .venv/bin/python -m metacoin_service --home ./svc-home status
run .venv/bin/python -m unittest metacoin_service.tests.test_native_protocol metacoin_service.tests.test_native_domains metacoin_service.tests.test_native_lunar metacoin_service.tests.test_native_conformance metacoin_service.tests.test_native_model metacoin_service.tests.test_native_interfaces
run .venv/bin/python -m metacoin_service.native.vectors
run .venv/bin/python -m metacoin_service.lunar reproduce metacoin_service/lunar/data/example-pack-inputs.json --manifest metacoin_service/lunar/data/example-pack-manifest.json
run .venv/bin/python -m metacoin_service.tests.journeys_native --only 1,2,4,5,8,9,12,16,17,18,21,22,36 --out "$OUT/journeys-native-from-clean-export.json"
python3 - "$OUT/journeys-native-from-clean-export.json" "$ZIP" <<'PY'
import json, sys, hashlib
p, z = sys.argv[1:3]; d = json.load(open(p)); d['archive_sha256'] = hashlib.sha256(open(z, 'rb').read()).hexdigest(); json.dump(d, open(p, 'w'), indent=1)
PY
echo "CLEAN_EXPORT rc=$rc"
