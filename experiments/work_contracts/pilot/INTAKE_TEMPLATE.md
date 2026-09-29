# Pilot intake — real team template (UNFILLED)

Fill this in with the team before any execution. Do not paste confidential
data into a chat or a public repository; this file describes the task, it does
not carry the inputs. Bracketed items are placeholders, not defaults.

## 1. Decision or deliverable

- What decision will the result inform? `[ ]`
- Deliverable format the team needs (outcome only / outcome + margins / explanation of uncertainty): `[ ]`
- Who receives it, and by when: `[ ]`

## 2. Inputs

- Input format (fields, units, bounds vs. point values): `[ ]`
- Ownership and provenance (measured / vendor datasheet / modeled / assumed), with the source named for each parameter: `[ ]`
- Access restrictions (who may see the raw inputs; may they leave the team's machines?): `[ ]`
- Provenance label to record: `declared_unverified` unless independently established: `[ ]`

## 3. Model and applicability

- Model requested: `outage-energy-bounds/v0` (no-recharge interval, piecewise-constant power bounds, fixed reserve, integer mW / s / mJ). If the task needs anything else (recharge, temperature, aging, peak power, correlated bounds), say so here: `[ ]`
- Why the model's assumptions hold for this task (accounting boundary, bounds not distributions): `[ ]`
- Known gaps the team accepts: `[ ]`

## 4. Acceptance criteria (agreed BEFORE execution)

- Accepted outcomes for a completed contract: `[ FEASIBLE | INFEASIBLE | INDETERMINATE ]` (any subset)
- Does a valid negative (INFEASIBLE) count as completed work? `[yes/no]`
- Does a valid INDETERMINATE count as completed work? `[yes/no]`
- Exact boundary semantics accepted (equality counts as covered): `[confirm]`

## 5. Audit and disclosure

- Who may see the full private evidence (name the auditor role and the person/machine): `[ ]`
- What may be public: bindings only / bindings + outcome / + margin explanation (numbers become public): `[ ]`
- Channel for the PRIVATE audit package: `[ ]`
- Channel through which the reviewer obtains the trust pins (contract digest, evidence root) — must be independent of whoever sends the package: `[ ]`

## 6. Resources and payment direction

- Budget for the bounded next-step action (asset, amount, cap): `[ ]`
- Payment direction being exercised (agent buys compute after acceptance / customer pays for analysis / none): `[ ]`
- Rail: `legacy_simulation` (zero-value, in-process) / `durable_test_simulation` (file-backed test facility) / other (name, and its idempotency + reconciliation guarantees): `[ ]`

## 7. Time, evidence, disputes

- Authorization expiry (`expires_at`, unix seconds): `[ ]`
- Evidence availability period the team expects (recorded only; no deletion service exists): `[ ]`
- Dispute contact and process (recorded only; no arbitration exists): `[ ]`

## 8. Effort and repeat use

- Current time/cost the team spends to accept such a result today: `[ ]`
- What would make a second use worthwhile: `[ ]`

## 9. Observations to record (filled by the operator after the pilot)

- First-pilot observation: did the team's own reviewer accept or reject a result from the team's own task using the criteria in §4? `[not yet observed]`
- Repeat use: did the team voluntarily submit another genuine task? `[not yet observed]`
