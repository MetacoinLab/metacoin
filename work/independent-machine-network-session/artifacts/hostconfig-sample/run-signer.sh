#!/usr/bin/env bash
# operator-controlled signer for THIS provider's keys; loopback only; never exposed
set -eu
H="$(dirname "$0")/signer"
[ -f "$H/token" ] || python -m metacoin_service.native.signer init --home "$H" --domains dom-a --signer-id signer-b
exec python -m metacoin_service.native.signer serve --home "$H" --port 9440
