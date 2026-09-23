# Scientific WorkContract v0

Python 3.12+, standard library only. An experimental owner-local workflow for
private scientific inputs, explicit uncertainty, valid negative deliverables,
and budgeted use of MetaCoin's **existing x402-class simulation**.

```sh
python3 -m unittest discover -s experiments/work_contracts/tests -v
python3 -m experiments.work_contracts.cli demo
python3 -m experiments.work_contracts.benchmark --samples 30
```

The demo creates temporary SQLite state, agrees on contracts before execution,
privately audits three outcomes, checks public disclosures, buys imaginary
compute through `demo.x402_spend_stub.buy_compute`, retries without another debit,
rejects an altered result, and retains exposure after an acknowledgement is lost.
Temporary state is removed on exit. It never contacts a network or uses keys.
`--out /fresh/path/public-demo.json` writes public-only samples instead of printing
the full samples. Its included root pins are demonstrative, not authenticated
remote authority. Obtain real trusted pins independently of the receipt sender.

## Scientific meaning

The no-recharge model integrates nonnegative piecewise-constant power bounds.
Power in mW times seconds yields energy in mJ. A fixed reserve is added. FEASIBLE
means lower available energy covers upper demand; INFEASIBLE means upper energy
cannot cover even lower demand; otherwise the outcome is INDETERMINATE.

Inputs are bounded integers with explicit units, intervals and assumptions.
Bounds are not probabilities. No battery dynamics, peak-power limit, degradation,
thermal behavior or instrument authenticity is inferred. Initial usable energy
and loads must be on the same accounting boundary. Synthetic examples are not
measured flight data or evidence of customer adoption. The independent test
reference uses exact rational watt-hours; it does not call the production sum.

A prior `accepted_outcomes` policy determines whether valid negative/uncertain
analysis counts as completed work. Invalid data is refused. Acceptance permits
an attempt at a separately budgeted action; it does not itself move value.

## Three separate boundaries

| Operation | What it establishes | Remaining assumptions |
|---|---|---|
| Owner registration | The local journal pins terms and input commitment before the job | Trusted owner process, filesystem and clock; no remote identity authentication |
| Authorized private audit | Full committed structure and the declared result match local recomputation | Auditor sees private inputs; model applicability and measurement provenance are not proven |
| Public receipt verification | Disclosed fields belong to a separately trusted result root and match contract bindings | No hidden-computation proof, signature, anonymity or payment eligibility |
| Compute purchase | One local authorized action is reserved against job and campaign limits | `legacy_simulation`; no HTTP, chain, actual compute or real funds |

Do not expose `Journal.register`, `Journal.audit`, or caller-supplied actor strings
as public authenticated APIs. These are local administrative functions. The DB
and parent directory must be owner-controlled. Possession of a receipt, a job
identifier, or an unsigned `accepted` statement confers no authority.

## Owner / worker / auditor command-line walkthrough

Use a new temporary directory for this synthetic walkthrough. It will contain
PRIVATE PLAINTEXT vaults. Never publish that directory or the vault files.

```sh
metacoin_demo_dir="$(mktemp -d)"
metacoin_expiry="$(python3 -c 'import time; print(int(time.time()) + 3600)')"
python3 -m experiments.work_contracts.cli fixture \
  --out "$metacoin_demo_dir/inputs.json" --outcome INFEASIBLE
python3 -m experiments.work_contracts.cli prepare \
  --input "$metacoin_demo_dir/inputs.json" --out-dir "$metacoin_demo_dir/owner" \
  --job pilot-example --expires-at "$metacoin_expiry"
metacoin_pin="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["contract_digest"])' "$metacoin_demo_dir/owner/owner-pin.json")"
python3 -m experiments.work_contracts.cli execute \
  --contract "$metacoin_demo_dir/owner/contract.json" \
  --expected-contract-digest "$metacoin_pin" \
  --input-vault "$metacoin_demo_dir/owner/private-input-vault.json" \
  --out "$metacoin_demo_dir/private-evidence.json"
python3 -m experiments.work_contracts.cli audit \
  --contract "$metacoin_demo_dir/owner/contract.json" \
  --expected-contract-digest "$metacoin_pin" \
  --input-vault "$metacoin_demo_dir/owner/private-input-vault.json" \
  --evidence-vault "$metacoin_demo_dir/private-evidence.json" \
  --out "$metacoin_demo_dir/public-bundle.json" > "$metacoin_demo_dir/audit-summary.json"
metacoin_root="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["expected_evidence_root"])' "$metacoin_demo_dir/audit-summary.json")"
python3 -m experiments.work_contracts.cli verify \
  --contract "$metacoin_demo_dir/owner/contract.json" \
  --expected-contract-digest "$metacoin_pin" \
  --bundle "$metacoin_demo_dir/public-bundle.json" --expected-root "$metacoin_root"
```

The owner pin is obtained from the owner's earlier preparation, and the result
pin from the local authorized audit. Neither file is a signed attestation. The
CLI audit is read-only with respect to spending authority. Only a successful
`Journal.audit` records acceptance usable by `Journal.dispatch`.

For another compatible task instance, supply a JSON file with the fixture's
schema and actual parameters, marked `declared_unverified` until provenance is
independently established. Never alter the verifier or acceptance criteria after
seeing results under an already registered contract. `--require-feasible` changes
the prior completion policy; `--hide-outcome` keeps the outcome out of public
openings. Both options belong on the owner `prepare` command.

New schema versions and verifier changes intentionally invalidate new use of old
contracts. This is not a migration framework. Historical ledger encodings are
unaffected. The modules run from the repository checkout; they are intentionally
not added to the core installed wheel in this increment.

## Payment recovery and privacy limits

SQLite reservations use `BEGIN IMMEDIATE` and unique job/request constraints.
Identical retries reuse stored state. Changed requests or new request IDs cannot
reuse a job's single entitlement. Campaign exposure includes confirmed purchases,
reservations, pending submissions and unknown outcomes; it excludes only
conclusively failed actions. This campaign has no automatic refill.

Submission intent is persisted before calling the adapter. A loss between intent
and durable completion leaves pending/unknown state. Reconciliation queries the
adapter; it never resubmits. The current adapter remembers outcomes only in the
same process. After loss of that process it cannot resolve an uncertain debit;
the journal conservatively retains exposure. An old DB backup must not be
restored as if it represented current authorization state. The in-memory faucet
is not made durable by adding a SQLite journal. No claim of exactly-once network
settlement follows.

Only the guarded legacy faucet APIs change simulated balances. Initial funding
comes from a separately identified existing-task fixture. It is not issuance or
a reward earned by the new analysis. This action buys imaginary next-step compute;
it is not a customer payment for scientific work or an escrow release.

Private vaults remain plaintext, with exclusive 0600 file creation. Public output
omits raw input values and numerical margins. Root linkage and timing remain;
the disclosure policy can itself expose an outcome or acceptance condition.
Retention and dispute terms are recorded but no deletion scheduler, encrypted
backup service, arbitration service or automatic refund is implemented.

See `docs/experiments/WORK_CONTRACT_V0.md` for the pre-implementation relation,
trust model and measurement protocol. The next real-user milestone is an external
team agreeing on its own task and acceptance criteria, then choosing to use the
workflow again. This synthetic demo does not establish that milestone.
