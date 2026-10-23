# Clean start (executable; verified by `clean_export.sh` from the source archive — journey 49)

```
unzip metacoin-independent-machine-network-source.zip && cd metacoin-independent-machine-network
cp ../protocol-data/ledger_data.jsonl protocol/ledger_data.jsonl && cp ../protocol-data/mission_verdict.json mission_verdict.json   # runtime data shipped next to the archive
python3 -m venv .venv && .venv/bin/pip install -r metacoin_service/requirements.lock
export PYTHONPATH=$PWD METACOIN_X402_DISABLED=1
.venv/bin/python -m metacoin_service.deploy_local up --home ./local-home --domain-id dom-local      # API + worker + signer + witness on loopback; prints the admin credential FILE path (never the token)
.venv/bin/python -m metacoin_service.deploy_local status --home ./local-home
```
Custody-separated instance (no development actors; every spend through a signer): add `METACOIN_REQUIRE_EXTERNAL_CUSTODY=1` to the
environment of `deploy_local up`, enrol actors with `metacoin-provider ... enroll` (public key from that party's signer) and answer
signing requests with `metacoin-provider --home DIR sign-pending` or `client_cli native-sign-pending`.

Then, with the printed administrator credential file `ADMIN` and base URL `BASE`:
```
python3 -m venv .venv-client && (cd provider_client && PYTHONPATH=$PWD ../.venv-client/bin/python -m metacoin_provider conformance)    # 32 cases, v2
.venv-client/bin/python -m metacoin_provider --base BASE --signer <signer url> --signer-token <signer token file> enroll --admin-cred ADMIN --name prov-1 --ceiling 1000 --out prov.json
.venv-client/bin/python -m metacoin_provider --base BASE --cred prov.json --home ./prov-home --signer <signer url> --signer-token <token> --actor prov-1 init --name prov-1   # pins identity + versions
.venv-client/bin/python -m metacoin_provider --home ./prov-home handshake
.venv/bin/python -m metacoin_service.client_cli --credential-file ADMIN --base BASE native-operator
.venv/bin/python -m metacoin_service.tests.journeys_network --only 1,2,4,11,16,22,26,31,36,42,0 --out journeys.json   # three fresh domain process sets (dom-c custody separated); the 80-unit example is journey 1
.venv/bin/python -m metacoin_service.native.model_network                                                            # bounded model report
.venv/bin/python -m metacoin_service.deploy_local down --home ./local-home
```
Console: `BASE/console` (log in with the token from the credential file): `/console/operator`, `/console/providers`, `/console/pools`,
`/console/missions`, `/console/portfolio`, `/console/products`, `/console/relay`, `/console/missions/<id>/continuity`.
Offline verification: `metacoin-provider verify <export or bundle>.json --roots roots.json` (recomputes pool overlap, lot partition,
obligations); `python -m metacoin_service.native.checker EXPORT.json --roots ROOTS.json`.
Second host: `python -m metacoin_service.native.hostconfig generate --role provider --service-url https://HOST:8443 --ca ca.pem --out ./host-b` then `hostconfig check ./host-b`.
