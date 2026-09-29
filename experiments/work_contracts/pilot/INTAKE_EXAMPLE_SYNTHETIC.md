# Pilot intake — filled SYNTHETIC example

Everything here is synthetic. No team, instrument, or dataset behind it exists;
it shows what a complete intake looks like for the fixture the walkthrough uses.

## 1. Decision or deliverable
- Decision: whether an instrument's stored usable energy covers one 600 s no-recharge interval with a fixed 100,000 mJ reserve.
- Deliverable: outcome plus, privately, the minimum additional usable energy and the dominant uncertainty source.
- Recipient: the synthetic owner role `local-owner`; needed before the next synthetic planning step.

## 2. Inputs
- Format: `available_low/available_high` (mJ), `reserve` (mJ), `segments[].{duration s, power_low mW, power_high mW}`.
- Provenance: synthetic (`provenance: "synthetic"`); the private label `SYNTHETIC_PRIVATE_CANARY_73` is a leak canary, not data.
- Access: inputs stay in the owner's private vault (plaintext, mode 0600); only the auditor role recomputes.

## 3. Model and applicability
- Model: `outage-energy-bounds/v0`. Bounds are intervals, not distributions. No recharge, no thermal or aging effects, no peak-power limit.
- Applicability: the owner asserts the usable-energy bounds and the load bounds are on the same accounting boundary.
- Accepted gaps: correlated uncertainties are not modeled; the margin decomposition is an exact interval-width split, not a probability.

## 4. Acceptance criteria
- Accepted outcomes: FEASIBLE, INFEASIBLE, INDETERMINATE (valid analysis policy).
- A valid INFEASIBLE counts as completed work: yes. A valid INDETERMINATE: yes.
- Boundary: `available_low >= required_high` is FEASIBLE at equality.

## 5. Audit and disclosure
- Full private evidence: the `local-auditor` role on the owner's machine.
- Public: the seven binding fields plus the outcome. The margin explanation stays private (`--disclose-explanation` not set).
- Private package channel: same machine, `import-private` into a fresh 0700 directory.
- Trust pins: the reviewer reads `owner-pin.json` and the audit summary directly from the owner, not from the package.

## 6. Resources and payment direction
- Budget: campaign cap 3 Test-META (zero value), one action of amount 1 per job.
- Direction: the agent role buys imaginary next-step compute after acceptance. Funding comes from the separately labeled lunar-link fixture task, not from this analysis.
- Rail: `durable_test_simulation` (file-backed testing facility) in the walkthrough; `legacy_simulation` in the demo.

## 7. Time, evidence, disputes
- Expiry: one hour after preparation.
- Evidence availability: `retention_seconds: 86400` is recorded; nothing deletes anything.
- Dispute: `owner-auditor-review-no-automatic-refund` is recorded; no arbitration exists.

## 8. Effort and repeat use
- Rehearsed operator time for the 17-step walkthrough on 2026-09-23: about 1 second of compute; the human reading time is not measured here. An external team's acceptance time is unknown until observed.
- Second use would be worthwhile if the team has a second bounded energy question with interval inputs.

## 9. Observations
- First-pilot observation: not performed (no external team).
- Repeat use: not performed.
