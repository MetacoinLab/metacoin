# Two-host configuration (service host and provider host)

Status: documented and exercised on **one** host only. Running the two configurations below on a single machine is a
single-host test; it does not produce a multi-operator result. A second host is used only where access is already
authorized. Nothing in this document was verified across a real network boundary in the 2026-10-23 session.

## Host A: settlement domain (operator A)

    python -m metacoin_service.deploy_local up --home /srv/metacoin-a --port 8410 --domain-id dom-a

This starts, on loopback of host A: the API and console, a background worker, the operator-controlled signer
(`metacoin_service.native.signer`, keys under `/srv/metacoin-a/signer`, bearer token in `signer/token`, 0600) and the
commit-checkpoint witness (`metacoin_service.native.witness`). The domain `dom-a` is initialised and its authority key is pinned
in the witness. x402 is disabled (`METACOIN_X402_DISABLED=1`) and its facilitator URL points at a closed port.

To expose the API beyond loopback, run the API process yourself with TLS (`python -m metacoin_service --home ... serve --tls
--host 0.0.0.0 --port 8443`, certificate from `metacoin_service node-tls`) and publish `ca.pem` to host B out of band. The
signer and the witness stay on loopback of host A; they are never exposed. Provider hosts talk to the **API** only.

## Host B: provider (operator B)

    pip install ./provider_client                                       # standard library only
    metacoin-provider conformance                                      # pins the client wire format to the service's
    python -m metacoin_service.native.signer init --home ~/signer-b --domains dom-a --signer-id signer-b
    python -m metacoin_service.native.signer serve --home ~/signer-b --port 9440 &

Operator B's signer holds operator B's actor keys; it is bound to domain `dom-a` (the domain whose intents it will sign). The
signer module is part of the service package; copy `metacoin_service/native/signer.py` plus `protocol.py` and `crypto.py`, or
install the service package on host B without starting a service. The signer never contacts host A.

Enrolment is an operator-A step (administrator credential on host A), using the public key that operator B's signer generated:

    metacoin-provider --base https://host-a:8443 --signer http://127.0.0.1:9440 --signer-token ~/signer-b/token \
        enroll --admin-cred admin-a.json --name prov-b --ceiling 1000 --out prov-b.json

In a real two-operator setting the `enroll` call runs on host A with the public key handed over out of band; the provider
credential file (0600) is handed back to operator B. The service stores the public identity only (`custody:
external-signer:signer-b`, `private_key_held_by_service: false`).

Pin the roots once, over the authenticated channel at enrolment time, and keep them:

    metacoin-provider --base https://host-a:8443 --cred prov-b.json roots > roots-a.json

Then the cycle: `offer-publish`, wait for a reservation (`reservations`), `activate`, `deliver-compute --executor "<python> -m
metacoin_service.lunar task"` (the product is computed on host B; the service verifies it against the public lunar reference and
labels the CPU time as a provider assertion), `settle` by the buyer, `receipts --roots roots-a.json` and `verify` offline.

## What crosses the boundary, and what does not

| crosses host A → host B | never leaves host A | never leaves host B |
|---|---|---|
| API responses (offers, reservations, awards, receipts, exports, mission bundles) | service database, authority key, witness token, admin credential | operator B's actor private key, signer token |
| the domain's authority public key (pinned by B as a root) | signer A's keys | provider credential file |

Receipts and bundles verify on host B without host A (`metacoin-provider verify`), under roots B pinned itself.

## Not provided

No Byzantine fault tolerance and no independent validators: host A remains the single ordering authority of `dom-a`. A
provider on host B detects equivocation only where two signed histories meet (see `docs/native/ARCHITECTURE_AND_TRUST.md`).
