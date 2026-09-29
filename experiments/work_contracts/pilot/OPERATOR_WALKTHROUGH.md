# Operator walkthrough (local pilot, no source edits)

Run every command from the repository root with Python 3.12+. The scripted
version is `pilot/walkthrough.sh`; `tests/test_walkthrough.py` runs it verbatim.
Roles below are trusted local roles, not authenticated identities.

| Step | Role | Command | Reads | Writes | Side effects |
|---|---|---|---|---|---|
| 1 | owner | `cli fixture --out inputs.json --outcome INDETERMINATE` | – | inputs.json (0600) | none |
| 2 | owner | `cli prepare --input inputs.json --out-dir owner --job J --expires-at T [--require-feasible] [--hide-outcome] [--disclose-explanation] [--capability …]` | inputs | owner/{contract,private-input-vault,owner-pin}.json | none |
| 3 | owner | `cli campaign init --journal j.sqlite --campaign C --limit N` | – | j.sqlite (0600) | creates the fixed, nonrenewing campaign |
| 4 | owner | `cli register --journal j.sqlite --contract … --expected-contract-digest PIN` | contract | journal row | one action entitlement for J |
| 5 | worker | `cli execute --contract … --expected-contract-digest PIN --input-vault … --out evidence.json` | private vault | private evidence (0600) | none |
| 6 | auditor | `cli record-audit --journal j.sqlite --job J --input-vault … --evidence-vault … --out bundle.json` | both vaults | bundle.json; journal acceptance | acceptance recorded (spend still not permitted) |
| 7 | public | `cli verify --contract … --expected-contract-digest PIN --bundle bundle.json --expected-root ROOT` | contract, bundle | – | none |
| 8 | auditor | `cli explain --contract … --expected-contract-digest PIN --input-vault … [--added-usable-energy N]` | private vault | – (PRIVATE output) | none |
| 9 | actor | `cli request --journal j.sqlite --job J --request-id R --out request.json` | journal | request.json | none |
| 10 | actor | `cli dispatch … --dry-run --adapter …` | journal, request | – | none (checks everything, reserves nothing) |
| 11 | actor | `cli dispatch --journal j.sqlite --request request.json --adapter durable-test-simulation --provider-state p.json --provider-initial-balance B` | journal, request | journal action row; provider file | reserves budget, dispatches once |
| 12 | actor | `cli status … --request-id R`, `cli reconcile … --request-id R --adapter … --provider-state p.json` | journal, provider | may record an authoritative outcome | never resubmits |
| 13 | actor | repeat step 11 | – | – | no new right, no new debit |
| 14 | owner → reviewer | `cli export-public … --out pub.zip`; reviewer: `cli import-public --package pub.zip --expected-contract-digest PIN --expected-root ROOT --out-dir fresh` | bundle | zip (0600); fresh dir (0700) | none |
| 15 | owner → auditor | `cli export-private … --out priv.zip`; auditor: `cli import-private --package priv.zip --out-dir fresh` | vaults | zip marked PRIVATE | none; sending it is a separate human decision |
| 17 | owner | `cli campaign show --journal j.sqlite` | journal | – | none |

Adapter choice on `dispatch`/`reconcile`:

- `legacy-simulation`: the existing `demo/x402_spend_stub.py` path. The faucet is
  in-memory and **process-scoped**: each CLI invocation funds a fresh fixture
  faucet, and outcomes cannot be reconciled from a later process. The output
  says so (`adapter_session: process-scoped`). Use it for one-process demos.
- `durable-test-simulation`: a file-backed **testing facility** whose
  request-bound outcomes survive process exit, so `reconcile` from a later
  process works. It is not a payment system.

Refusals: exit code 2, a JSON object `{refused, code, reason, action}` on stdout
and one line `REFUSED <CODE>: <action>` on stderr. Codes are listed by
`cli capabilities`. No private value, path, or exception text is printed.

Manual steps counted in the 2026-09-23 rehearsal: 2 (copying the contract pin
and the evidence root from the owner's files into later commands). Missing
prerequisites: none beyond Python 3.12 and bash. Ambiguous errors observed:
none after refusal codes were added; before them every failure printed the same
sentence.
