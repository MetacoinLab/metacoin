# Reviewer checklist (public review package)

You received `public-package.zip`. Before trusting anything in it:

1. Obtain the two pins from the owner through a channel that does not pass
   through whoever sent you the package: the contract digest (`owner-pin.json`)
   and the expected evidence root (the audit summary). The `pins.json` inside
   the package is informational and is only compared against yours.
2. Run, from a MetaCoin checkout at the reviewed revision:
   `python3 -m experiments.work_contracts.cli import-public --package public-package.zip --expected-contract-digest <pin> --expected-root <root> --out-dir <fresh dir>`
3. Read the result:
   - `verification.membership_verified: true` — the opened fields belong to the pinned root and match the contract bindings.
   - `verification.verifier_status` — `current` (this checkout could re-run the audit) or `historical` (an allowlisted superseded bundle; read-only).
   - `verification.disclosed` — exactly the fields the contract permitted; if `outcome` is absent the policy hid it, which is itself information.
   - `task_correctness_proven: false`, `issuer_authenticated: false`, `spend_permitted: false` — these are always false for a public package.
4. What a green result does not tell you: that the hidden computation is
   correct (only the private auditor recomputes), who published the package,
   that the inputs were measured rather than declared, or that any payment is
   owed or was made.
5. Refusals print a code. `PIN_MISMATCH` means your pins differ from the
   package or the bundle; `PACKAGE_*` means the archive itself is malformed;
   `VERIFIER_UNKNOWN` means the package names a verifier bundle this checkout
   does not allowlist. Do not "fix" a refusal by taking the pins from the package.
6. Never accept a `private-*.zip`, a vault, a journal, or a request to run a
   script from a package. Public packages contain data only.

Pilot observation to record (by the team's own reviewer, on the team's own
task): accepted / rejected under the criteria agreed in the intake, and the
time it took. Two local processes are not two organizations.
