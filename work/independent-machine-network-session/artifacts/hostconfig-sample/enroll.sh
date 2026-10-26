#!/usr/bin/env bash
# one-time: enroll this provider through the service administrator (admin credential supplied out of band) and initialise the pinned client home
set -eu
D="$(dirname "$0")"; . "$D/provider.env"
metacoin-provider --base "$METACOIN_BASE" ${METACOIN_CA:+--ca "$METACOIN_CA"} --signer "$METACOIN_SIGNER_URL" --signer-token "$METACOIN_SIGNER_TOKEN_FILE" enroll --admin-cred "$1" --name prov-b --ceiling 1000 --out "$D/provider-cred.json"
metacoin-provider --base "$METACOIN_BASE" ${METACOIN_CA:+--ca "$METACOIN_CA"} --cred "$D/provider-cred.json" --home "$D/home" --signer "$METACOIN_SIGNER_URL" --signer-token "$METACOIN_SIGNER_TOKEN_FILE" --actor prov-b init --provider-name prov-b
