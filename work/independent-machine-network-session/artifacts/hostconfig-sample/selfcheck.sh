#!/usr/bin/env bash
# run on the provider host after enroll.sh: pins and versions must match; prints JSON
set -eu
D="$(dirname "$0")"; . "$D/provider.env"
metacoin-provider --home "$D/home" handshake
metacoin-provider conformance
