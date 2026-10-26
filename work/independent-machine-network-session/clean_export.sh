#!/usr/bin/env bash
# Journey 49 (reproduction): unzip the source archive into a clean directory -> fresh venv from the lock (local wheel caches,
# --no-index) -> the documented clean-start sequence (CLEAN_START.md) -> unit suites -> provider client conformance v2 in a
# second standard-library-only venv -> the bounded network model -> a representative subset of the network journeys through real
# processes (custody-separated domain included) with x402 disabled, all from the extraction, never from the development checkout.
set -u
ZIP="$1"; S="$2"; OUT="$(cd "$3" && pwd)"; rm -f "$OUT/step-rcs.txt"; FRESH="$S/fresh-network"; rm -rf "$FRESH"; mkdir -p "$FRESH"; NAME=metacoin-independent-machine-network
(cd "$FRESH" && unzip -q "$ZIP") && cd "$FRESH/$NAME" || exit 1
PD="$(dirname "$ZIP")/protocol-data"; [ -d "$PD" ] && { cp "$PD/ledger_data.jsonl" protocol/ledger_data.jsonl; cp "$PD/mission_verdict.json" mission_verdict.json; echo "runtime data placed from protocol-data/"; }
mkdir -p work/independent-machine-network-session/artifacts; git -C "$(dirname "$ZIP")" rev-parse HEAD >/dev/null 2>&1; cp "$(dirname "$ZIP")/BASE_REVISION.txt" work/independent-machine-network-session/artifacts/BASE_REVISION.txt 2>/dev/null
echo "files: $(find . -type f | wc -l)"; echo "sha256 of archive: $(sha256sum "$ZIP" | cut -d' ' -f1)"
python3 -m venv .venv && .venv/bin/pip install -q --no-index --find-links "$S/svcdeps" --find-links "$S/x402deps" --find-links "$S/evmdeps" --find-links "$S/extdeps" --find-links "$S/expansiondeps" --find-links "$S/scideps" -r metacoin_service/requirements.lock 2>&1 | tail -2
echo "venv rc=$?"
rc=0
run() { s=$(date +%s); env -i PATH="$PATH" HOME="$HOME" LANG=C.UTF-8 PYTHONPATH="$PWD" METACOIN_X402_DISABLED=1 "$@" > "$OUT/$(echo "$*" | tr ' /.' '___' | cut -c1-80).log" 2>&1; r=$?; echo "rc=$r $(( $(date +%s)-s ))s :: $*"; echo "$r $*" >> "$OUT/step-rcs.txt"; [ $r -ne 0 ] && rc=1; }
run .venv/bin/python -m metacoin_service.deploy_local up --home ./local-home --domain-id dom-clean
run .venv/bin/python -m metacoin_service.deploy_local status --home ./local-home
run .venv/bin/python -m metacoin_service.deploy_local down --home ./local-home
run .venv/bin/python -m unittest metacoin_service.tests.test_native_pools metacoin_service.tests.test_native_custody metacoin_service.tests.test_native_subcontracts metacoin_service.tests.test_native_products metacoin_service.tests.test_native_relay metacoin_service.tests.test_native_lots metacoin_service.tests.test_native_continuity metacoin_service.tests.test_native_portfolio metacoin_service.tests.test_native_onboarding metacoin_service.tests.test_console_network metacoin_service.tests.test_native_conformance metacoin_service.tests.test_native_extensions
run .venv/bin/python -m metacoin_service.tests.test_native_model_network
python3 -m venv .venv-client && (cd provider_client && PYTHONPATH="$PWD" ../.venv-client/bin/python -m metacoin_provider conformance > "$OUT/provider-conformance.json" 2>&1); echo "provider conformance rc=$? ($(python3 -c "import json; d=json.load(open('$OUT/provider-conformance.json')); print(d['version'], d['passed'], 'of', d['cases'])" 2>/dev/null))"
run .venv/bin/python -m metacoin_service.native.vectors
run .venv/bin/python -m metacoin_service.native.hostconfig generate --role provider --service-url https://host-a.example:8443 --ca /etc/metacoin/ca.pem --out ./hostcfg-sample
run .venv/bin/python -m metacoin_service.native.hostconfig check ./hostcfg-sample
run .venv/bin/python -m metacoin_service.tests.journeys_network --only 1,2,3,4,9,11,12,16,18,22,23,26,31,32,36,37,42,44,0,51,53,54 --out "$OUT/journeys-network-from-clean-export.json"
python3 - "$OUT/journeys-network-from-clean-export.json" "$ZIP" <<'PY'
import json, sys, hashlib
p, z = sys.argv[1:3]; d = json.load(open(p)); d['archive_sha256'] = hashlib.sha256(open(z, 'rb').read()).hexdigest()
import os; rcs = [l.split(' ', 1) for l in open(os.path.join(os.path.dirname(p), 'step-rcs.txt')).read().splitlines() if l.strip()]
d['step_results'] = [{'rc': int(r), 'step': c[:160]} for r, c in rcs]; d['all_steps_ok'] = all(int(r) == 0 for r, _ in rcs); d['steps'] = 'clean-start (deploy_local up/status/down), 11 unit suites, model replay, provider conformance v2 in a standard-library venv, vectors, hostconfig generate/check, 18 journeys + demonstration + 3 extension journeys'; json.dump(d, open(p, 'w'), indent=1)
PY
echo "CLEAN_EXPORT rc=$rc"
