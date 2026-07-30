# Changelog

All notable changes to the MetaCoin protocol. Research-stage, zero-value, no
token — a passing verification proves deterministic re-derivability of the
anchored claims, not independence, not usefulness, not value.

## v0.1.0 — 2026-07-30 — first installable release

The protocol becomes an installable, versioned package with a unified CLI.
Packaging is additive: no module moved, no anchored-hash-feeding code path
changed, and every previous invocation (`python3 protocol/X.py`, the selftest
runners, CI, the fresh-clone simulation) works unchanged.

### Added
- `pyproject.toml` (setuptools, zero runtime dependencies, Python >= 3.9) and
  the `metacoin_cli` package: a thin argparse router — every subcommand
  delegates to the existing, already-verified modules.
- The `metacoin` command: `verify` (the flagship: every layer re-derived),
  `status`, `task run|list`, `molecule build|catalog`, `aci report
  [--korder]`, `challenge issue|respond|verify`, `identity
  generate|declare|rotate|verify`, `passport build|catalog`, `economy
  replay`, `treasury status`, `flow1 verify-epoch`, `version`. Coordinator
  operations (ledger writes) are deliberately excluded from the public CLI;
  they remain in `protocol/external_verifier.py`.
- The complete verification corpus ships as package data (published snapshot,
  committed tip anchor, privacy-checked evidence bundle): a pip-installed
  `metacoin` with no repository checkout fully verifies from an empty
  directory. A cold-install acceptance test (fresh venv, offline install,
  empty CWD) enforces this in the protocol selftest suite (15/15).

### The ledger at this release (45 entries, tip index 44, all re-derivable)
- Chain + committed tip anchor + published snapshot (tamper-evident,
  externally anchored).
- 13 reproducible aerospace tasks, each verified and anchored.
- Work-molecule provenance catalogs (two frozen generations), the cut
  certificate, and six-component trust vectors (no combined scalar exists,
  by mechanical rule).
- Concentration self-measurement: the pairwise ACI baseline (0.99365 over 28
  same-operator paths — maximal concentration, measured and published
  deliberately) and the higher-order ACI_k profile (exact enumeration,
  k = 2..4, one candidate S_k construction — the calibration question is
  open and the record says so).
- The metering claim record (wall/CPU measured, energy estimated — labeled).
- The simulated 30-day economy and the Two-Flow constitution: objective
  Flow-1 uptime emission (signed heartbeats, missed slot = honest zero) and
  the Flow-2 treasury (cannot mint, by construction) with the Gate-3
  mechanical lifecycle (the usefulness judgment seat is honestly vacant).
- Actor identity: Lamport one-time key roots, signed challenge rounds, and
  the key-rotation lifecycle (signed root handoff; history verifies against
  contemporaneous roots forever).
- MetaWork passports (histories, never leaderboards — no rank/score exists).

### Six defeated-attack drills, anchored as rejections
1. Copy attack (idx 25): a response forged from the public output_hash alone
   is rejected — possession under a fresh nonce cannot be faked from hashes.
2. One-time key reuse (idx 30): a signature reusing a consumed index is
   rejected by the ledger-wide cross-type scan.
3. Economy tamper drill (inside the anchored idx-19 log): a tampered
   submission earns nothing.
4. Gate-3 bounded failure (idx 34/35): a challenged bounty is clawed back —
   maximum exposure is one bounty cap, never the base.
5. Forged heartbeat (idx 38): a heartbeat signed by the wrong chain is
   rejected; the slot emits exactly zero.
6. Forged rotation (idx 42): a rotation certificate signed with an
   already-consumed key index is rejected — published signature material
   cannot hand an identity to an attacker's root.

### Disclaimers (unchanged, constitutional)
Same-operator evidence throughout; the anchored ACI baseline quantifies it.
No consensus, no mainnet, no payments, no token, no monetary value. Not
legal, financial, investment, or security-certification advice.
