# Step 01 acceptance matrix, contract-field enforcement, and payment transitions

Written 2026-09-23 for the local WorkContract v0 workflow (verifier bundle
`local-energy-audit/v1`). Every row below is exercised by a test named in the
last column; the tests live in `experiments/work_contracts/tests/`.

## 1. Acceptance matrix

Four decisions are kept separate for every case: the scientific outcome, the
completion decision under the contract's `accepted_outcomes` policy, what the
public bundle may show, and whether a bounded action may be authorized.

| Case | Scientific outcome | Completion decision | Public disclosure | Action authorization | Refusal code | Test |
|---|---|---|---|---|---|---|
| Valid positive (worst case covered) | FEASIBLE | completed | bindings (+ outcome if permitted) | one reservation permitted | – | `test_workflow.StateTests`, walkthrough |
| Valid negative (best case not covered) | INFEASIBLE | completed under valid-analysis policy | as above; conclusion stays negative | permitted (the contract paid for honest analysis) | – | `EvidenceTests.test_negative_work_and_public_proof_boundaries` |
| Valid negative under feasible-only policy | INFEASIBLE | NOT completed (policy) | bundle still verifiable; outcome hidden if policy hides it | refused: `WORK_NOT_ACCEPTED` | WORK_NOT_ACCEPTED | `test_feasible_only_policy_and_optional_outcome`, `test_cli.test_hide_outcome_policy…` |
| Valid indeterminate (bounds overlap) | INDETERMINATE | completed only if policy lists it | as above; uncertainty preserved, never coerced | permitted under that policy | – | walkthrough (INDETERMINATE case) |
| Malformed input (floats, duplicate keys, reversed bounds, zero duration, >128 segments, wrong units, bool/int confusion) | none (refusal, not a negative finding) | not completed | nothing | refused | INPUT_INVALID / MODEL_DOMAIN | `EnergyTests.test_units_and_invalid_model_domain`, `test_strict_json_ingress` |
| Malformed or forged evidence (changed leaf, salt, index, root, duplicate hidden names, extra hidden field) | none | not completed | verification refused | refused | EVIDENCE_INVALID / EVIDENCE_MISMATCH / PIN_MISMATCH | `test_full_audit_rejects_committed_lies…`, `test_public_tamper_missing_fields_root_substitution_and_kind` |
| Unauthorized (wrong owner, auditor or actor role; wrong recipient/resource/asset/network) | unchanged | registration/audit refused, or action refused | – | refused | UNAUTHORIZED_OR_EXPIRED / SPEND_REFUSED / BINDING_MISMATCH | `test_registration_pinning…`, `test_rebinding_each_action_field…` |
| Expired (registration, audit, or a NEW reservation after `expires_at`) | unchanged | new authorization refused | verification still works | refused; identical retry of an existing action returns its recorded state | EXPIRED / UNAUTHORIZED_OR_EXPIRED | `test_actor_expiry…`, `test_hardening.test_identical_retry_after_expiry…` |
| Tampered terms (changed field, replaced pin, forged `accepted`) | unchanged | refused | refused | refused | PIN_MISMATCH / CONTRACT_INVALID | `test_substituted_contract_program_and_input` |
| Unsupported version (unknown contract schema, unknown verifier digest, foreign evidence kind) | unchanged | refused | refused | refused | CONTRACT_VERSION / VERIFIER_UNKNOWN / EVIDENCE_VERSION | `VerifierEvolutionTests.test_unknown_or_mismatched_bundles…` |
| Superseded verifier (allowlisted historical bundle, e.g. the 2026-09-18 v0 output) | as recorded | no NEW audit | public verification permitted, labeled `historical` | refused | VERIFIER_SUPERSEDED | `VerifierEvolutionTests` (fixture `tests/fixtures/historical_v0_public_demo.json`) |

Reading the matrix: the scientific verdict is never the payment decision by
itself. A negative conclusion fulfills a contract for honest analysis and fails
a contract that requires robust feasibility. Malformed evidence is a refusal.

## 2. Contract fields: enforced, descriptive, or unsupported

Also emitted by `python3 -m experiments.work_contracts.cli capabilities`.

| Field | Status | Where |
|---|---|---|
| schema, commitment_schema, evidence_kind, result_schema, model_id, units, assumptions, uncertainty | enforced, exact match | `contract.validate` |
| job_id | enforced token; unique per journal; one action entitlement | `Journal.register`, `_reserve` |
| owner, auditor | enforced against the caller-supplied role string; **not authenticated** | `Journal.register/audit` |
| input_authority | descriptive | – |
| input_root | enforced trust anchor for the private input vault | `acceptance.inputs_for` |
| verifier_id, verifier_digest | enforced against the installed bundle (new authorization) or the historical allowlist (read-only) | `verifiers.py` |
| domain | enforced by input validation (integers, 1..128 segments, ≤ 2^53−1 aggregates) | `energy_analysis.validate/analyze` |
| accepted_outcomes | enforced completion policy | `acceptance.audit` |
| allowed_disclosures, required_disclosures | enforced on audit and public verification; required ⊆ allowed ⊆ public field set, else invalid | `contract.validate`, `acceptance.verify_public` |
| action.{actor, recipient, resource, amount, asset, network, capability} | enforced binding on reservation; `limit` is descriptive (equals amount in v0) | `Journal._reserve`, adapter `validate` |
| expires_at | enforced for new authorization only; status/reconcile remain available | `Journal` |
| dispute | descriptive; no arbitration exists | – |
| retention_seconds | descriptive; no deletion service exists | – |
| access | descriptive; local owner, process, filesystem and clock are trusted | – |

## 3. Payment state transitions (one action per job, one per request id)

```text
state               who moves it            to                 condition / effect
NOT_REQUESTED (no row)
  -> RESERVED       dispatch (new)          atomic tx          accepted work, current verifier, not expired,
                                                               binding matches, entitlement unused,
                                                               exposure + amount <= cap
RESERVED
  -> SUBMISSION_PENDING  dispatch/_claim    atomic tx          intent durable BEFORE the adapter is called
  -> FAILED_CONFIRMED    dispatch/reconcile atomic tx          expires_at passed and never claimed
                                                               (reference local-expired-before-dispatch)
SUBMISSION_PENDING
  -> CONFIRMED / FAILED_CONFIRMED  _finish  atomic tx          adapter answer bound to the stored request
                                                               digest and capability, semantically
                                                               consistent (units match amount; failures
                                                               grant 0)
  -> OUTCOME_UNKNOWN     dispatch (exception) / reconcile      lost, malformed or mismatched answer
OUTCOME_UNKNOWN
  -> CONFIRMED / FAILED_CONFIRMED  reconcile                   only from an authoritative adapter record;
                                                               never by resubmission
CONFIRMED, FAILED_CONFIRMED                                    terminal; a duplicate identical answer is a
                                                               no-op; a conflicting one is refused
                                                               (OUTCOME_CONFLICT) and never replaces it
```

Campaign invariant, checked inside the reservation transaction with checked
integers: `confirmed + reserved + pending + unknown <= cap`. Only
FAILED_CONFIRMED releases budget.

Identical retries return the recorded state (`dispatch` resumes only a RESERVED
row; it never re-submits a pending or unknown one). Reusing an identifier with
changed terms fails (`REQUEST_ID_REBOUND`). A new identifier for a consumed
entitlement fails (`ENTITLEMENT_CONSUMED`).

What the journal does **not** provide: exactly-once external settlement. With
the legacy in-memory adapter a new process has no record and an unknown action
stays unknown with exposure retained. With the file-backed **test** provider
(`tests/durable_provider.py`, a testing facility) a durable request-bound
record resolves it, and an authoritative "no record" answer voids the key so a
late submission is refused. A real rail must document the same two properties
before its answers can release budget.

## 4. Interruption points observed with real child processes

`tests/test_interruption.py` terminates a child with `os._exit` at each point
and asserts the state a fresh process observes (durable provider / no-record
provider):

| Point | Journal after crash | Provider | Recovery observed |
|---|---|---|---|
| before reservation commit | no row, exposure 0 | 0 dispatches | dispatch again → CONFIRMED |
| after reservation commit | RESERVED, exposure 1 | 0 | dispatch resumes → CONFIRMED, 1 dispatch |
| after intent commit, before adapter call | SUBMISSION_PENDING | no intent | durable: reconcile → FAILED_CONFIRMED (key voided; late submit refused, balance intact). no-record: OUTCOME_UNKNOWN, exposure kept |
| after effect, before response | SUBMISSION_PENDING | 1 dispatch, balance −1 | durable: reconcile → CONFIRMED. no-record: OUTCOME_UNKNOWN, exposure kept, no resubmission |
| after response, before confirmation commit | SUBMISSION_PENDING | as above | same as previous row |
| after confirmation commit, before return | CONFIRMED | as above | status/dispatch → CONFIRMED, still 1 dispatch |

These are software recovery boundaries, not power-loss or storage tests.
