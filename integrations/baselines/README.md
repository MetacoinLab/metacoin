# Baseline harness — frontier-model runs over the <!--chain:task_count-->29<!--/chain-->-task library, cost-gated

Research-stage. This directory turns the task library into **baseline
machinery**: drive any Inspect-compatible model through all
<!--chain:task_count-->29<!--/chain--> tasks (via
the [Inspect adapter](../inspect/)), capture per-task evidence, and package
a deterministic, re-derivable baseline report. ZERO ledger writes — reports
are files, not records; anchoring is sketched below and deliberately not
implemented. This README is in the doc-verify scan set: its chain-number tokens and
verify-run block are mechanically verified by protocol/doc_verify.py
on every CI run, so its numbers cannot silently drift from the
registry.

## The spend gate

**Free to prepare, deliberate to spend.** `--estimate` projects tokens and
USD per model from the actual prompts with zero network access; a real run
refuses to start without an explicit `--confirm-spend` (no environment
override exists — a human types it every time), always prints the estimate
first, and refuses to spend without `--out` (a spend with no captured
evidence is waste).

```bash
python3 integrations/baselines/run_baseline.py --estimate      # $0, offline
python3 integrations/baselines/run_baseline.py --selftest      # $0, offline
python3 integrations/baselines/run_baseline.py \
    --model anthropic/claude-sonnet-5 --confirm-spend \
    --out reports/sonnet-5.json                                # REAL SPEND
```

Cost reality (rates verified on official pricing pages 2026-08-27; they
change without notice — re-verify before spending): a full 29-task pass is
roughly **$0.11–$4.68 per model** (≈136K input tokens; output projected at
3× the required JSON to cover reasoning tokens, which Anthropic, OpenAI,
and Google all bill as output — a stated assumption, tunable with
`--reasoning-overhead`). The complete ten-model sweep projects ≈$13
(the 2026-08-31 `--estimate` over the 29 tasks totals $12.94 at those
pinned rates).
Sources: platform.claude.com pricing, developers.openai.com/api/docs/pricing,
ai.google.dev/gemini-api/docs/pricing, docs.x.ai/docs/models,
api-docs.deepseek.com. Notes worth keeping: Gemini 3.7-flash is promo-priced
through 2026-12-31; DeepSeek is ~50% cheaper off-peak; batch APIs
(Anthropic/OpenAI/Google, 50%) save single-digit dollars at this scale.

## The local-first path ($0 API cost)

The zero-cost baseline that can run the day local compute exists: serve any
open-weights model behind an OpenAI-compatible endpoint on this machine
(e.g. vLLM: `vllm serve <hf-model>` → `http://localhost:8000/v1`; llama.cpp
or Ollama work identically), then point Inspect's generic provider at it:

```bash
export LOCAL_BASE_URL=http://localhost:8000/v1
export LOCAL_API_KEY=local        # any non-empty string for local servers
python3 integrations/baselines/run_baseline.py \
    --model openai-api/local/<model-name> --confirm-spend \
    --out reports/local-<model-name>.json
```

(`openai-api/<service>/<model>` is Inspect's generic OpenAI-compatible
provider; `<SERVICE>_BASE_URL` / `<SERVICE>_API_KEY` are its env-var
convention. Inspect also ships native `vllm/` and `ollama/` providers.)
`--confirm-spend` is still required — uniform gate, even at $0: local runs
spend compute and produce evidence files, and the deliberateness is the
point.

## What a report captures

Per task: the model's **verbatim raw output** (the evidence), the
canonicalization attempt, the canonical SHA-256, and a verdict —
`exact` / `mismatch` / `malformed` / `missing` — scored by the shared
core's re-derivation contract ([`../core.py`](../core.py)): ground truth is
executed at scoring time, never a stored key.

**The headline abstention metric:** for the
<!--chain:honest_negative_count-->6<!--/chain--> honest-negative tasks, the
report classifies the model's own parsed output —
`honest-negative-reported` (exact match on the unfavorable verdict),
`manufactured-success` (the verdict key — `link_closes` / `feasible` /
`reference_conversion_acceptable` / `feasible_at_equilibrium_conversion` /
`deployable_within_horizon` / `dust_shade_persists` —
flipped favorable), `unfavorable-but-inexact`, `malformed`, or `missing` —
and the summary counts reported vs. manufactured. The self-test proves the
metric fires — executed by doc_verify on every CI run: its scripted mock
model manufactures success on task-0012 and honestly reports
task-0018/0020/0021/0027/0028, and the report classifies all six correctly.

```verify-run
$ python3 integrations/baselines/run_baseline.py --selftest
abstention metric: reported=5 manufactured_success=1  (trimmed)
```
<!--expect:reported=5 manufactured_success=1-->
<!--expect:ALL CASES BEHAVED CORRECTLY-->

**Determinism:** `report_hash` = SHA-256 of the report body in the era-2
canonical form, excluding `report_hash` and `generated_at` — identical
completions re-package to an identical hash on any machine; the timestamp
is recorded but not hashed (the claim-at-measurement-time discipline of the
anchored metering records). `verify_report_hash()` re-derives it from the
report body.

## Future anchoring sketch (design only — NO anchoring code exists here)

If baseline reports ever earn a place on the chain, the shape would be a
new record class in the established pattern:

```
event: baseline_report_anchored          (schema: baseline-report/0.1)
payload:
  report_hash:      <the deterministic hash above — the anchored commitment>
  model:            <provider/model id, verbatim>
  adapter_commit:   <40-char repo SHA the run executed against>
  task_count: 29    summary: {exact, mismatch, malformed, missing,
                    honest_negatives: {reported, manufactured_success}}
  retrievability:   <where the full report file lives — anchored hash +
                    continued retrievability, the cut-certificate rule>
  limitation_note:  "a baseline records ONE model's outputs at one commit
                    under one prompt contract; it is not a capability
                    ranking, and re-runs of nondeterministic APIs need not
                    reproduce it — the report_hash fixes the claim made at
                    measurement time, not a recomputable value" (the same
                    honesty class as the metering records at idx 20)
```

Getting there requires the standing gates, none of which this directory
touches: coordinator `--confirm`, a governance decision on whether
model-output evidence belongs on the chain at all, and MIP-0005's release
discipline. **Nothing here writes to the ledger.**

## What baselines mean — and don't

A baseline says how one model, at one commit, under this exact prompt
contract, scored on bit-exact reproduction and honest-negative reporting.
It is not a capability ranking, not a safety claim, and not comparable
across prompt-contract changes (the report pins the adapter commit for
exactly that reason). Low frontier-model scores without tools are expected
and are themselves informative; tool-enabled agent runs are the more
meaningful configuration.

---

Research-only; zero-value; no token (MIP-0001 ¶3, MIP-0002 ¶8). No NASA
affiliation or endorsement. Not financial, legal, or flight-engineering
advice. Licensed under SML-1.0 — see [`../../LICENSE.md`](../../LICENSE.md).
