# Clean start (executable; verified by `clean_export.sh` from the source archive)

```
unzip metacoin-autonomous-mission-economy-source.zip && cd metacoin-autonomous-mission-economy
cp ../protocol-data/ledger_data.jsonl protocol/ledger_data.jsonl && cp ../protocol-data/mission_verdict.json mission_verdict.json   # runtime data shipped next to the archive
python3 -m venv .venv && .venv/bin/pip install -r metacoin_service/requirements.lock
export PYTHONPATH=$PWD METACOIN_X402_DISABLED=1
.venv/bin/python -m metacoin_service.deploy_local up --home ./local-home --domain-id dom-local      # API + worker + signer + witness on loopback; prints the admin credential FILE path (never the token)
.venv/bin/python -m metacoin_service.deploy_local status --home ./local-home
```
Then, with the printed administrator credential file `ADMIN` and base URL `BASE`:
```
python3 -m venv .venv-client && (cd provider_client && PYTHONPATH=$PWD ../.venv-client/bin/python -m metacoin_provider conformance)
.venv-client/bin/python -m metacoin_provider --base BASE --signer <signer url> --signer-token <signer token file> enroll --admin-cred ADMIN --name prov-1 --ceiling 1000 --out prov.json   # (run from provider_client/ with PYTHONPATH=$PWD, or pip install ./provider_client)
.venv/bin/python -m metacoin_service.client_cli --credential-file ADMIN --base BASE native-operator
.venv/bin/python -m metacoin_service.tests.journeys_mission --only 1,2,13,17,20 --out journeys.json   # two fresh domain process sets; the 80-unit example is journey 1
.venv/bin/python -m metacoin_service.deploy_local down --home ./local-home
```
Console: `BASE/console` (log in with the token from the credential file): `/console/operator`, `/console/missions`, `/console/resources`.
Offline verification: `metacoin-provider verify <export or bundle>.json --roots roots.json`; `python -m metacoin_service.native.checker EXPORT.json --roots ROOTS.json`.
