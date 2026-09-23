# WorkContract v0: design and trust boundary

Experimental, local-only acceptance followed by a bounded zero-value compute
purchase. Core and adapter use Python 3.12 standard library. No historical data
or anchored task is modified. The existing x402 stub is reused unchanged.

## Acceptance relation (specified before implementation)

accepted_work = authorized_contract AND matching_committed_inputs AND
allowed_verifier AND valid_model_domain AND matching_recomputed_result AND
satisfied_completion_policy.

spend_permitted = accepted_work AND authorized_actor AND allowed_action AND
current_authorization AND available_budget AND unused_action_entitlement.

The owner registers an immutable contract in a trusted local SQLite journal
before execution. Its pre-agreed input root is the trust anchor, independent
of the subsequently supplied result. Registration is an administrative local
operation, not a public unauthenticated endpoint. The journal and its parent
directory must be controlled by the owner. Local process identity is the access
boundary: actor strings alone are NOT remote authentication. No submitted
accepted flag or unsigned audit statement is authoritative.

The authorized auditor receives the complete private input and evidence vaults,
validates every committed field/slot, and independently recomputes the pinned
energy model. Only this in-process audit may record acceptance. Public users
verify selective membership against a separately obtained root; they cannot
verify hidden computation or issuer identity. The result root is retained in
the trusted journal after successful audit; public distribution requires a
separate authenticated channel that this prototype does not implement.

One job has one next-compute entitlement in one owner campaign. The same job
cannot be re-registered with changed terms or renewed by changing a request ID.
The campaign is a fixed, nonrenewing budget. Atomic SQLite reservations bind
contract, receipt, principal, adapter, recipient/resource, amount, asset/network,
expiry, and request identity. Result data stays outside payment metadata.

Payment states: RESERVED -> SUBMISSION_PENDING -> CONFIRMED,
FAILED_CONFIRMED, or OUTCOME_UNKNOWN. Submission intent is durable before the
adapter is called. A restart treats SUBMISSION_PENDING as unknown; it never
redispatches that action. Unknown reservations retain budget. This deliberately
trades availability for conservative no-blind-retry behavior. A separate local
SQLite transaction cannot provide exactly-once external settlement.

The legacy adapter can reconcile only outcomes retained in that same adapter
instance. A lost process loses its in-memory faucet and outcome knowledge; the
journal remains unknown and requires external recovery evidence. There is no
manual 'mark paid' or arbitrary unsigned reconciliation override. A confirmed
local simulation is not a confirmed network payment.

## Scientific model

Inputs specify a bounded no-recharge interval with piecewise-constant power
bounds (mW), positive durations (s), usable-energy bounds and reserve (mJ).
Demand bounds are reserve + sum(power bound * duration). All quantities use
bounded integers. Bounds are not probabilities. Worst-case sufficiency yields
FEASIBLE; best-case insufficiency yields INFEASIBLE; otherwise INDETERMINATE.
No assertion of peak-power ability, electrochemistry, temperature, measurement
truth, or flight safety follows. Private numerical margins are not disclosed.

The allowlisted verifier pins this module's bytes and the acceptance/validation
implementation dependency bundle. Contract schema and evidence versions are
separate from historic ledger eras. Installed code is trusted; accepting an
arbitrary program digest provided by a submitter is forbidden.

## Disclosure and lifecycle

The contract fixes allowed/required result fields. Required binding fields are
contract_digest, input_root, verifier_id, verifier_digest, result_schema,
model_id, and scope. Outcome is optional and must be explicitly permitted.
Audit results may be valid yet rejected by a completion policy requiring
FEASIBLE. Invalid inputs are refusals, never scientific negative findings.
Private vaults remain plaintext; mode 0600 is only access restriction. The
contract declares an audit retention period, but this version does not implement
a deletion daemon or encrypted backups. Already disclosed fields cannot be
revoked. Root linkage, request timing, and simulation actor linkage remain.

## Scope inventory

Reviewed public baseline: e3299171541a656a0a539f239a31670a50f7282a.
Existing payment entry: demo/x402_spend_stub.py::buy_compute, called by
agent_loop.py/economy_demo.py and guarded by faucet.spend. No x402 SDK, HTTP
transport, chain, facilitator, signed offers, or payment-identifier extension
is present in this checkout. Capability is explicitly legacy_simulation.
The owner may have a newer integration elsewhere; this inventory describes
only the accessible checkout, not every MetaCoin deployment.

## Experimental protocol (specified before measurements)

Hypotheses: preserve three scientific outcomes; refuse substituted terms and
proofs; limit public information to the allowlist; enforce budget and entitlement
under concurrency; hold uncertain outcomes without duplicate dispatch.
Use hand-derived examples and a separately written Fraction-based reference,
seeded randomized inputs and monotonicity/splitting/unit properties. Compare
bare legacy compute purchase with contract-gated purchase as workflow overhead,
not equivalent security. Also time the same energy inputs with a raw-result
baseline versus private audit/disclosure. Thirty measured repetitions follow
three warmups. Report median, nearest-rank p95, environment, sample/seed and
serialized public bytes. No timing threshold in correctness CI.

## Addendum 2026-09-23: verifier bundle v1

Changes to the trust model above, in force from verifier `local-energy-audit/v1`:

- Verifier evolution is explicit. `experiments/work_contracts/verifiers.py`
  allowlists superseded bundle digests. Historical bundles permit public
  verification and journal inspection only; new registration, audit and
  spending require the current bundle. Old receipts are never re-pinned or
  edited. The allowlist file is policy and is not part of the digested bundle.
- The private audit runs outside the journal's write lock, bound to a snapshot
  of the registration (contract bytes + pin); acceptance is recorded only after
  an atomic recheck of that snapshot, the expiry, and root immutability.
- Expiry gates new reservations only. An identical retry of an existing action
  returns its recorded state; status and reconciliation stay available.
- Adapter answers are checked for semantic consistency (units versus bound
  amount; failures grant nothing) and an answer for a row that never recorded
  submission intent is unbound. Inconsistent answers preserve uncertainty.
- Evidence gains two audit-only fields, `margin_explanation` and
  `dominant_uncertainty_source` (see `explanation.py`). They are public only
  when the contract lists them; public verification checks the explanation's
  versioned envelope. Old contracts never list them and remain valid.
- Refusals carry stable codes (`refusals.py`); the CLI never prints private
  values, paths or foreign exception text.
- Public and private packages (`packages.py`) are whitelisted, size-bounded zip
  files with a sha256 manifest; import parses data only. The manifest proves
  consistency with itself, not publisher identity, scientific truth, or payment
  eligibility. The operator's pins are the only pins used.
- A file-backed durable test provider exists for tests and the walkthrough. It
  is a testing facility that models a rail with durable idempotency keys and an
  authoritative, key-voiding "no record" answer. It is not a payment system.
