# Evidence acquisition and scoped assurance — implementation report (order of 2026-10-22)

## What a user can do now
Through the API, CLI and console (and an agent through MCP under its grant): declare a finite candidate model (hypotheses
with decision labels, linear views, noise bounds) and get an exact answer to "can the current evidence decide this?" —
separated within these assumptions, ambiguous (with a private witness that an independent checker re-decides),
determined within the declared model, model inconsistent with the observations, or vacuous. Preview the next useful
measurement set under a budget with its expected cost, store the plan, turn it into a frozen acquisition contract, collect
offers, award (budget reserved atomically), let the provider deliver the observation, watch the decision update, read what
was checked in plain English, accept, pay exactly what the frozen terms say for valid work whatever the answer, export an
authorized bundle and replay the witnesses and sampling arithmetic offline. Bind an exact finite-population sampling plan
to a committed itemwise result before the challenge and read the conditional detection bound it actually establishes.
Compare verification portfolios on preregistered fault suites and see the common-mode floor that more computation cannot
remove.

## Identities
| item | value |
|---|---|
| branch | `evidence/acquisition-assurance` (local only; not pushed) |
| starting revision | `825adf8ea2d99f3211bc81ef421f40c6bfe91895` (origin/service/real-features; origin/main 854619a is its merge) |
| candidate revision (source) | `715938997629eb590e28dc0dcd88087551c0b675` |
| records commit | `created after packaging (work/ only); hash in RECORDS_COMMIT.txt of the delivery` |
| schema | `041_decision_evidence` on disposable instances; the live service stays at `3e34126` / schema 040 (not upgraded by this order) |
| delivery path | `~/metacoin-evidence-acquisition-delivery-2026-10-22/` |
| work time | interrupted session 06:22–07:01Z (kernels, service layer); recovery and completion from 2026-10-22T07:54Z to 2026-10-22T08:35Z (packaging start) (41 min of recovery and completion work in this session) |
| interpreters | API venv Python 3.12 (fastapi, x402 2.24.0 as an optional rail adapter), compute `/usr/bin/python3` (numpy/scipy/torch) |

## Status by class (see FEATURE_LEDGER.json)
- Implemented and verified locally: Deliverables A–G and journeys 1–26.
- Externally unverified: external settlement of acquisition payments (only the application journal and the private local
  chain were exercised; x402 is optional and never required by the decision package); physical instruments (deliveries are
  simulated observations labelled as such).
- Deferred: the six optional extensions; the live-service upgrade to schema 041 (this order built against disposable
  state and the running service was not changed); publication.

## Verification
- Unit suites: 287 tests OK (13 skipped: optional hosts/hardware) in 538 s — inherited suites plus the new decision suites; science (compute interpreter): 18 OK; local chain: 7 OK.
- Decision suites: test_decision_kernels 22, test_decision_service 10, test_decision_sampling 2, test_decision_surfaces 3 — all OK (included in the unit count).
- Journeys: 25 of 26 passed at 715938997629 (journey 26 recorded from the clean export); browser journey: 19 of 19 checks passed (screenshots in `browser-screenshots/`); clean exported tree
  (journey 26): the archive `3adba62636ae0e38…` installed from its lock in a fresh offline venv; init, status, decision suites (37 tests), science, service, chain, sampling and the 9-journey subset all rc=0 (clean-export-logs/); patch check equal (patch-check.json).
- Example bundle: `example-bundle/decision-bundle-example.zip` replays with the command in `REPLAY_COMMAND.txt`
  (exit 0, integrity ok, signatures trusted under TRUST_ROOT.txt, scientific replay ok); numerical fixtures in `example-bundle/fixture-results.json` (N=1000, α=1/100: k=368 for m=10, 990 for m=1).
- Privacy scan: 31 result files and the session patch scanned, 0 hits (synthetic fixture labels and disposable-instance credential paths in verbose test logs were replaced by placeholders).

## Scientific statements, scoped
- Separation is exact for the enumerated rational hypotheses, declared linear views and closed noise boxes; it is not
  physical truth, calibration or a probability.
- A sampling plan's bound is P(no sampled item fails | at least m of N items are defective) ≤ α under uniform sampling
  without replacement and a correct item checker; m is an assumption of the claim. It is never "the result is correct
  with probability 1−α". Server-side randomness is computational pseudorandomness, not independence from the operator.
- Assurance is claim-specific: a replica, a sampled check, an exact recomputation and an analytical invariant establish
  different things; a passed final-transition audit does not cover the trajectory; a passed replica does not satisfy an
  independent-reference requirement.
- Portfolio reports are suite coverage; a statistical rate appears only for suites declared representative, and zero
  escapes keep an exact one-sided bound. Common-model defects survive every computational check; the report says so and
  recommends a different evidence source.
- No base issuance is touched; work payments follow the frozen terms; fee-funded mission work and objective base issuance
  stay separate.

## Defects found and fixed during recovery
See `work/evidence-acquisition-session/SESSION_STATE.md`: the interrupted session's last edits (quote-expiry state,
lineage object types) were already in the recovered tree and re-verified; this session additionally found and fixed a
float leaking into the strict-JSON sampling-plan response, a plan invalidation that was rolled back with its refusal,
and a console template/context mismatch — each covered by a test.
