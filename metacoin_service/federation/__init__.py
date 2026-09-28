"""Trusted worker-node federation: deliberately enrolled node identities with operator-approved capability limits,
an authenticated transport (TLS with a pinned local CA plus a node bearer credential and an Ed25519 request
signature), bounded scoped artifact transfer, server-side leases and fencing, and a remote worker process that
never touches the coordinator's database or filesystem. Federation means authenticated coordination among approved
workers on this host's trust domain; it is not a permissionless network, consensus, or attestation."""
