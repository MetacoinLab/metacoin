# HAL benchmark package — the <!--chain:task_count-->21<!--/chain-->-task library in HAL's benchmark contract

Research-stage. This directory packages the MetaCoin task library in the
benchmark format of Princeton's
[Holistic Agent Leaderboard](https://hal.cs.princeton.edu/) (HAL) —
specifically as a candidate for the reliability line of work behind their
[Reliability Dashboard](https://hal.cs.princeton.edu/reliability).
This README is in the doc-verify scan set: its chain-number tokens and
verify-run block are mechanically verified by protocol/doc_verify.py
on every CI run, so its numbers cannot silently drift from the
registry.

## What this is — and what it awaits

**Status honesty first (verified 2026-08-27):** the
[hal-harness](https://github.com/princeton-pli/hal-harness) repository was
**archived on 2026-07-01**. Their CONTRIBUTING.md, verbatim: "We are no
longer accepting new HAL result updates or active PRs against this
repository while we focus on reliability work… We will have more to share
soon." There is therefore no open PR path today. This package is built to
their **last documented benchmark contract**
([hal/benchmarks/README.md](https://github.com/princeton-pli/hal-harness/blob/main/hal/benchmarks/README.md)
on the archived main) as a review-ready artifact: it awaits upstream
conversation with the HAL reliability team and whatever their next-generation
harness turns out to require. **Nothing has been submitted.**

## The contract implemented

`metacoin_benchmark.py` provides `MetacoinTasksBenchmark`:

- `benchmark_name = "metacoin_tasks"`; `requires_sandbox = False`; a
  dataset dict `task_id → {"prompt", "answer", "files", "metadata"}` for
  all <!--chain:task_count-->21<!--/chain--> tasks (prompts carry the
  complete reference source, dependency
  modules included for the parented tasks).
- `_ground_truth_keys = ["answer"]` — their anti-leakage convention
  (ground truth stripped before tasks reach the agent). Scoring ignores
  the stored answer anyway: `evaluate_output` re-derives by executing the
  reference modules (the protocol's Gate-2 rule), so a tampered answer key
  cannot change a verdict.
- `evaluate_output(agent_output, run_id)` scores each submission through
  the **shared scoring core** (`integrations/core.py`, stdlib-only — the
  same `score_completion` contract behind the
  [Inspect adapter](../inspect/)): parse, canonicalize in the era-2
  canonical form (sign-of-zero-free, per the rule anchored at ledger
  idx 67), SHA-256, exact match or incorrect. Missing submissions and
  unknown task ids are reported with stated reasons, never silently
  dropped.
- `get_metrics` returns their required `accuracy` / `successful_tasks` /
  `failed_tasks`, plus the honest-negative subset broken out — and nothing
  aggregation-shaped beyond plain accuracy.

The class subclasses HAL's real `BaseBenchmark` when hal-harness is
importable and an identically-shaped stand-in otherwise, so everything here
is testable today, standard library only:

```verify-run
$ python3 integrations/hal/metacoin_benchmark.py --selftest
--- (a) dataset shape: 21 tasks, contract fields: OK ---  (trimmed; 21/21
on reference submissions; tampered/garbage/missing all rejected)
```
<!--expect:dataset shape: 21 tasks-->
<!--expect:negatives 4/4: OK-->
<!--expect:ALL CASES BEHAVED CORRECTLY-->

## Why this suits a reliability harness

- **Zero grader noise.** Grading is deterministic and bit-exact, so every
  outcome difference across repeated runs is attributable to the agent,
  never to grading ambiguity — the failure class that forced tau-bench's
  "clean" subset. Outcome-consistency metrics get a clean substrate.
- **A scored abstention probe.** HAL's reliability methodology measures
  post-hoc confidence (risk–coverage), and its paper poses — but does not
  yet measure — "selective operation: can the system recognize when it
  should defer, abstain, or escalate?".
  <!--chain:honest_negative_count-->4<!--/chain--> tasks here (task-0012
  `"link_closes": false`; task-0018 `"feasible": false`; task-0020
  `"reference_conversion_acceptable": false`; task-0021
  `"feasible_at_equilibrium_conversion": false`) make the
  unfavorable verdict the scored-correct behavior under the identical
  bit-exact rule: an agent passes only by honestly reporting "no".
  `get_metrics` surfaces this subset separately.
- **Small task counts are precedented** in that setting (their headline
  tau-bench reliability split is a 26-task curated subset) and repeated-run
  protocols multiply cost, which favors compact benchmarks. Parameterized
  task variants (their stated wish: "generative benchmarks with
  parameterized test sets") are a natural extension of these deterministic
  generators — future work, not claimed here.

## Stated limitations

Single-shot prompt-in/JSON-out tasks: trajectory-consistency and
fault-injection metrics have little to grip unless run with tool-using
agents that compute their answers. These are first-order illustrative
engineering computations with stated simplifications, verified for
exactness — a probe set, not a general-capability benchmark, and
<!--chain:task_count-->21<!--/chain--> tasks
means wide per-metric confidence intervals under repeated-run protocols.

---

Research-only; zero-value; no token (MIP-0001 ¶3, MIP-0002 ¶8). ZERO ledger
writes. No NASA affiliation or endorsement. Not financial, legal, or
flight-engineering advice. Licensed under SML-1.0 — see
[`../../LICENSE.md`](../../LICENSE.md).
