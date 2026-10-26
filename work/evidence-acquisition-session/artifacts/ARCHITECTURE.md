# Evidence acquisition and scoped assurance — architecture note (order of 2026-10-22)

Everything new lives in `metacoin_service/decision/` and is executed, stored and paid through the existing paths: contracts
and jobs (the worker, leases, evidence vaults, lineage), the economy (work terms, requests, offers, awards, entitlements,
intents, the journal), verification (classes, commitment-first challenges, signed statements), the evaluation registry
(suites), and the three interfaces (API, CLI, console) plus MCP under the existing grant system. No new queue, journal,
identity or artifact store. x402 remains an optional rail adapter: nothing in the decision package imports it, and every
acquisition journey settles on the application journal unless the requester chooses a chain asset.

## Scientific operations (worker child, exact rational arithmetic, standard library only)
- `decision_observability` (kind): enumerated rational hypotheses with decision labels, declared linear views with
  componentwise closed noise boxes, optional observations. Two hypotheses with different labels are separated by a view
  iff some output coordinate has |A x_i − A x_j| > 2ε (strict: touching boxes are ambiguous). Unresolved pairs get a
  concrete shared observation inside both boxes (the witness). Observations condition the set: no survivor →
  `MODEL_INCONSISTENT_WITH_OBSERVATIONS`; survivors with one label → `DETERMINED_WITHIN_DECLARED_MODEL`; a single-label
  candidate set → `NO_DECISION_RELEVANT_ALTERNATIVES` (vacuous, labelled so). Limits: 32 hypotheses, 16 coordinates,
  64 views; floats, bools-as-int, NaN, duplicates, unit mismatches and unvalidated conversions are refused at validation.
- `checker.py`: an independent witness checker that re-projects from the raw hypotheses and views; it never reads the
  producer's projections. It is the verification class `analytical` for decision kinds and the core of offline replay.
- Acquisition planning: exhaustive subset search over ≤ 12 quoted candidate views maximising resolved cross-label pairs,
  then minimum integer cost, then fewest views, then ids; a brute-force oracle re-derives the optimum in tests.
  Weighted priorities exist only as a versioned, requester-frozen objective. A pair count is not entropy or probability.
- `evidence_acquisition` (kind): the frozen operation of an acquisition contract. Its job is created at award on hold;
  the provider's observation delivery (an `observation_delivery` artifact in `job_deliveries`) releases it; the worker
  conditions the frozen candidate model on the delivery and the checker re-decides it. Outcomes: `DECISION_RESOLVED`,
  `DECISION_AMBIGUOUS`, `MODEL_INCONSISTENT_WITH_OBSERVATIONS`, `DELIVERY_INVALID`, `ACQUISITION_FAILED`.
- Sampling plans: smallest k with C(N−m,k)/C(N,k) ≤ α by exact integer comparison and binary search; a log-domain bound
  is display-only and conservatively rounded, never a proof of zero. Fixtures: N=1000, α=1/100 → k=368 (m=10), 990 (m=1).
  The plan binds N, the frame digest (result commitment + N), m (an assumption), α, the budget, the checker digest and the
  fault model BEFORE the challenge; the existing commitment-first challenge then draws its seed; one fixed-size attempt per
  plan. The signed statement carries the conditional detection bound and recomputes offline.

## Assurance (Deliverable A)
`assurance.py` defines typed requirements (`metacoin-assurance-requirement/v1`: claim type, coverage, comparison
semantics, independence dimensions, sampling, checker) and derives statements from signed verification records. Policy
schema `metacoin-acceptance-policy/v2` makes `verification_passed` claim-specific (same class on the delivered root; an
earlier detected defect is not overridden by a later pass) and adds `assurance_satisfied`; frozen v1 terms keep their
rank semantics byte-for-byte and are shown with `legacy-rank/v1` wording. The VERIFICATION_RANK investigation is an
executable regression: under v1 a passed replica satisfies a requested sampled_reference predicate and a failed audit is
ignored by a later pass; under v2 neither holds.

## Contracts, money, privacy
Template `decision-evidence-acquisition/v1` freezes the question, candidate-model digest, menu digest, validation method,
disclosure policy, allowed providers, maximum budget and accepted outcomes. Payment rules pay valid agreed work
(resolved, ambiguous, model-inconsistent alike); a could-not-acquire report is payable only by a frozen diagnostic amount;
malformed deliveries earn nothing. Budget is reserved only at award, through the existing reservation; quote expiry and the
model binding are rechecked at materialisation and again inside the award transaction. Witnesses, raw candidates and
observations are private artifacts; projections disclose counts and, per the input's disclosure policy, ids / labels /
witness values. Bundles for offline replay contain the raw inputs, result, delivery, signed statements and plans, and the
service public key — never a private key or credential.

## Portfolios (Deliverable F)
Versioned combinations of components with declared dependency relationships (shared / distinct / unknown per dimension;
unknown is never counted as distinct). Fault campaigns are evaluation suites of synthetic cases across the twelve
failure families plus clean and valid-negative cases, split dev / held-out (held-out uses are counted). Reports give
false acceptance, false rejection, abstention, cost and latency by family, suite coverage separately from a statistical
rate (only for suites declared as representative draws; zero escapes keep an exact one-sided bound), paired outcomes
between portfolios, and the common-mode floor with a recommendation for a different evidence source.

## What is not claimed
Finite-scale spread-out diagnostics, Hausdorff-dimension preservation, independence from added workers, inherited quantum
fidelity, operational failure probabilities from test counts, zero-knowledge properties of signed receipts, and
calibrated physical truth of a model-level decision.
