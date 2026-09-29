# Step 01 validation — September 18, 2026

Local synthetic experiment on macOS 26.5.1 arm64, Python 3.12.13. No real
instruments, customers, funds, ZK prover, or network payment were involved.

## Predefined hypotheses and observations

| Hypothesis | Observed evidence | Limit |
|---|---|---|
| Scientific negatives and uncertainty survive the workflow | FEASIBLE, INFEASIBLE and INDETERMINATE are preserved in demo/audit/public openings | Validity is conditional on the bounded energy model |
| Acceptance obeys the prior policy | Negative analysis completes under valid-analysis policy; feasible-only policy refuses it | Local owner sets the policy; customer usefulness untested |
| Altered terms/evidence do not authorize spending | Contract/input/program/result/root/actor/destination substitution tests pass | Installed code, auditor and local authorization context are trusted |
| Public output excludes private inputs/margins | Canary and field-exclusion tests cover public bundles, stdout/stderr and adapter metadata | Root/timing/actor linkage and allowed conclusions remain visible |
| Duplicate/ambiguous actions do not create another spend | Concurrent duplicate tests assert one adapter submission; budget races and acknowledgement loss preserve bounds | Process interruptions are injected at code boundaries, not power-loss hardware tests |

Thirty-one new unittest methods include 200 seeded randomized cases compared with
an independently written Fraction-based watt-hour calculation. Seed: 73019.
Properties check load/reserve monotonicity, energy monotonicity, widening bounds,
segment splitting, scaling and units. Six hand-derived outcome/boundary cases
are also checked. Tests are useful evidence, not a proof of program correctness.

The existing seventeen selective-disclosure tests also pass. Local repository
gates pass: 49/49 demo suites, 29/29 protocol suites, 21 full-verification layer
rows, task law with zero violations, and documentation checks. These suite counts
are not counts of independent security audits. No remote CI run was triggered.

## Timing observations

Three warmup runs followed by thirty measured runs per outcome. Timer:
perf_counter_ns. Values below are medians in milliseconds; the accompanying
measurement JSON includes nearest-rank p95, min/max, versions and exact sizes.

| Outcome | Raw analysis | Private audit + disclosure | Existing legacy purchase | Contract-gated purchase |
|---|---:|---:|---:|---:|
| FEASIBLE | 0.0101 | 1.0378 | 0.0008 | 0.9660 |
| INFEASIBLE | 0.0099 | 1.0342 | 0.0008 | 0.9500 |
| INDETERMINATE | 0.0102 | 1.0339 | 0.0008 | 0.9766 |

Purchase timings exclude setup funding, registration and private audit; the new
path includes durable authorization/reservation. The paths have different
security semantics, so this is added overhead, not an equivalent-service speed
comparison. Commit generation is not included in the private-audit timing.
No memory measurement or sustained-throughput claim was made.

Public opening bundles measured 4,760–4,768 bytes; raw analysis JSON was 438–449
bytes. Selective disclosure is larger here, while keeping numerical bounds and
margins out of public output. Do not reinterpret this as a compression result.
Random salts/positions change roots and can slightly change serialized length.

## Verification boundaries and next observation

The private auditor sees the input. Public verification proves membership only.
The payment adapter reuses the existing zero-value x402-class stub. A fresh
adapter cannot reconstruct a lost in-memory outcome: unknown exposure remains
reserved rather than being retried. Expired reservations that never reached
dispatch can be released without calling the payment adapter. Source pinning
covers the local analysis and acceptance dependency bundle, not every possible
execution environment or administrative action.

The next useful observation is an external team's real task and first acceptance,
followed by voluntary repeat use. Before adapting this model, agree on that
team's input/provenance boundary, acceptance rules, privacy constraints, negative
result policy and budget. Another receipt count is not evidence of adoption.
