# Copyright (c) 2023-2026 MetaCoin-Lab.
# Licensed under SML-1.0 — see LICENSE.md.
"""metacoin_benchmark.py — HAL-format benchmark package v0: the task
library in the Holistic Agent Leaderboard's documented benchmark contract.

============================== SCOPE / BOUNDARY ==============================
OPTIONAL INTEGRATION, not part of the protocol; ZERO ledger writes; the
zero-dependency rule applies to the protocol, not to adapters — though THIS
file happens to be stdlib-only: it imports the shared scoring core
(integrations/core.py, stdlib-only) and duck-types HAL's BaseBenchmark
contract so it is fully testable without hal-harness installed. When
hal-harness is importable, the class subclasses their BaseBenchmark directly.
Research-only; no token; not financial advice.
==============================================================================

STATUS HONESTY (verified 2026-08-27): the hal-harness repository
(github.com/princeton-pli/hal-harness) was ARCHIVED on 2026-07-01 — their
CONTRIBUTING.md: "We are no longer accepting new HAL result updates or active
PRs against this repository while we focus on reliability work… We will have
more to share soon." This package is therefore built to their last documented
benchmark contract (hal/benchmarks/README.md, archived main) as a
review-ready artifact for the HAL reliability team's NEXT harness — it awaits
upstream conversation, and nothing has been submitted. See README.md here.

THE CONTRACT IMPLEMENTED (their hal/benchmarks/README.md, verbatim shape):
  class __init__(agent_dir, config) setting `benchmark_name`, `benchmark`
  (dict: task_id -> {"prompt": ...}), `requires_sandbox`;
  evaluate_output(agent_output, run_id) -> per-task results;
  get_metrics(eval_results) -> at least {"accuracy", "successful_tasks",
  "failed_tasks"}.
Ground truth is declared in `_ground_truth_keys` (their anti-leakage
convention: those fields are stripped before tasks reach the agent) — and is
additionally IGNORED at scoring time: the scorer re-derives by executing the
reference modules (the protocol's Gate-2 rule), so a tampered answer key
cannot change a verdict.

WHY THIS BENCHMARK SUITS A RELIABILITY HARNESS (stated for reviewers):
  * Deterministic, bit-exact grading — zero grader noise. Every outcome
    difference across repeated runs is attributable to the agent, never to
    grading ambiguity (the failure class that produced tau-bench's "clean"
    subset).
  * Two honest-negative tasks (task-0012 `"link_closes": false`, task-0018
    `"feasible": false`) where the unfavorable verdict IS the correct answer,
    scored by the identical bit-exact rule — a direct, scored probe of the
    "selective operation" question (defer/abstain/escalate) that reliability
    work poses; get_metrics() reports the honest-negative subset separately.
  * Single-shot limitation, stated plainly: these tasks are prompt-in/
    JSON-out; trajectory-consistency and fault-injection metrics have little
    to grip unless run with tool-using agents that compute their answers.

Self-test (stdlib-only, no hal-harness needed):
    python3 integrations/hal/metacoin_benchmark.py --selftest
"""

from __future__ import annotations

import os
import sys
from typing import Any, Dict, Optional

_REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from integrations import core as _core

# Subclass the real BaseBenchmark when hal-harness is present; otherwise a
# minimal stand-in with the same constructor shape so the class is importable
# and testable today (the archived harness is not on PyPI).
try:  # pragma: no cover - exercised only where hal-harness is installed
    from hal.benchmarks.base_benchmark import BaseBenchmark as _Base

    _HAL_AVAILABLE = True
except ImportError:

    class _Base:  # duck-typed stand-in, constructor shape per their docs
        def __init__(
            self,
            agent_dir: str,
            config: Dict[str, Any],
            requires_sandbox: bool = False,
            setup_script: Optional[str] = None,
            base_results_dir: str = "results",
        ):
            self.agent_dir = agent_dir
            self.config = config
            self.requires_sandbox = requires_sandbox
            self.setup_script = setup_script
            self.base_results_dir = base_results_dir

    _HAL_AVAILABLE = False


class MetacoinTasksBenchmark(_Base):
    """The task library as a HAL benchmark: prompt in, canonical JSON out,
    scored by bit-exact re-derivation (integrations/core.py holds the one
    scoring contract shared by every harness adapter)."""

    # Their anti-leakage convention: ground-truth fields stripped from tasks
    # before they reach the agent. Scoring ignores this field anyway and
    # re-derives (see evaluate_output).
    _ground_truth_keys = ["answer"]

    def __init__(self, agent_dir: str = ".", config: Optional[Dict[str, Any]] = None):
        self.benchmark_name = "metacoin_tasks"
        self.benchmark: Dict[str, Any] = {
            task_id: {
                "prompt": _core.sample_input(task_id, module_name, parents),
                # Ground truth, present for harness bookkeeping and stripped
                # from agent-visible tasks per _ground_truth_keys. Scoring
                # never trusts it (re-derivation is the arbiter).
                "answer": _core.expected_hash(module_name),
                "files": {},
                "metadata": {
                    "module": module_name,
                    "parents": parents,
                    "honest_negative": task_id in _core.HONEST_NEGATIVES,
                },
            }
            for task_id, module_name, parents in _core.TASK_MODULES
        }
        self.requires_sandbox = False
        super().__init__(agent_dir, config or {}, requires_sandbox=False)

    # -- their contract -------------------------------------------------------
    def evaluate_output(
        self, agent_output: Dict[str, Any], run_id: str
    ) -> Dict[str, Any]:
        """Score each submission by re-derivation. `agent_output` maps
        task_id -> the agent's raw text submission (their agent contract:
        'Dictionary mapping task IDs to submissions'). Unknown task ids are
        reported, never silently dropped; missing submissions score
        incorrect with a stated reason."""
        results: Dict[str, Any] = {}
        for task_id, spec in self.benchmark.items():
            module_name = spec["metadata"]["module"]
            if task_id not in agent_output:
                results[task_id] = {
                    "correct": False,
                    "explanation": "no submission for this task",
                    "answer_hash": None,
                    "expected_hash": _core.expected_hash(module_name),
                    "honest_negative": spec["metadata"]["honest_negative"],
                }
                continue
            verdict = _core.score_completion(
                module_name, str(agent_output[task_id])
            )
            verdict["honest_negative"] = spec["metadata"]["honest_negative"]
            results[task_id] = verdict
        unknown = sorted(set(agent_output) - set(self.benchmark))
        if unknown:
            results["_unknown_task_ids"] = unknown
        return results

    def get_metrics(self, eval_results: Dict[str, Any]) -> Dict[str, Any]:
        """Their minimum contract: accuracy, successful_tasks, failed_tasks.
        Added honestly: the honest-negative subset broken out (the abstention
        probe — a reliability reviewer should see it at a glance), and no
        combined score beyond plain accuracy (no aggregation-shaped extras,
        per the protocol's no-combined-scalar discipline)."""
        task_rows = {
            k: v for k, v in eval_results.items() if not k.startswith("_")
        }
        successful = sorted(k for k, v in task_rows.items() if v["correct"])
        failed = sorted(k for k, v in task_rows.items() if not v["correct"])
        negatives = {k: v for k, v in task_rows.items() if v["honest_negative"]}
        return {
            "accuracy": (len(successful) / len(task_rows)) if task_rows else 0.0,
            "successful_tasks": successful,
            "failed_tasks": failed,
            "honest_negative_total": len(negatives),
            "honest_negative_correct": sum(
                1 for v in negatives.values() if v["correct"]
            ),
        }


def _selftest() -> int:
    print(
        "=== metacoin_benchmark.py self-test (HAL contract, stdlib-only; "
        f"hal-harness {'PRESENT' if _HAL_AVAILABLE else 'absent — using the '
        'documented-contract stand-in'}) ===\n"
    )
    ok = []
    bench = MetacoinTasksBenchmark()

    # (a) dataset shape: the whole roster, prompts present, ground truth declared.
    n_tasks = len(_core.TASK_MODULES)
    n_neg = len(_core.HONEST_NEGATIVES)
    shape_ok = (
        len(bench.benchmark) == n_tasks
        and all("prompt" in t and "answer" in t for t in bench.benchmark.values())
        and bench._ground_truth_keys == ["answer"]
        and bench.requires_sandbox is False
    )
    print(f"--- (a) dataset shape: {n_tasks} tasks, contract fields: "
          f"{'OK' if shape_ok else 'WRONG'} ---")
    ok.append(shape_ok)

    # (b) reference submissions -> N/N, every honest negative correct.
    reference = {
        task_id: _core.reference_completion(module_name)
        for task_id, module_name, _ in _core.TASK_MODULES
    }
    metrics = bench.get_metrics(bench.evaluate_output(reference, run_id="selftest"))
    ref_ok = (
        metrics["accuracy"] == 1.0
        and len(metrics["successful_tasks"]) == n_tasks
        and metrics["failed_tasks"] == []
        and metrics["honest_negative_total"] == n_neg
        and metrics["honest_negative_correct"] == n_neg
    )
    print(
        f"--- (b) reference submissions: accuracy {metrics['accuracy']}, "
        f"negatives {metrics['honest_negative_correct']}/"
        f"{metrics['honest_negative_total']}: {'OK' if ref_ok else 'WRONG'} ---"
    )
    ok.append(ref_ok)

    # (c) a partial, tampered submission set: one 'fixed physics' negative,
    # one garbage answer, sixteen missing -> accuracy 0, reasons stated.
    import json as _json

    mod = _core.load_module("task_0012_comms_link_budget")

    def _flip(o):
        if isinstance(o, dict):
            return {
                k: (True if k == "link_closes" else _flip(v)) for k, v in o.items()
            }
        if isinstance(o, list):
            return [_flip(v) for v in o]
        return o

    tampered = {
        "task-0012-comms-link-budget": _json.dumps(_flip(mod.compute())),
        "task-0001-lunar-link-budget": "I cannot compute this.",
    }
    results = bench.evaluate_output(tampered, run_id="selftest")
    metrics2 = bench.get_metrics(results)
    tam_ok = (
        metrics2["accuracy"] == 0.0
        and not results["task-0012-comms-link-budget"]["correct"]
        and "mismatch" in results["task-0012-comms-link-budget"]["explanation"]
        and results["task-0001-lunar-link-budget"]["explanation"]
        == "output did not parse as JSON"
        and results["task-0002-orbit-propagation"]["explanation"]
        == "no submission for this task"
    )
    print(
        "--- (c) tampered negative + garbage + missing: all rejected with "
        f"stated reasons: {'OK' if tam_ok else 'WRONG'} ---"
    )
    ok.append(tam_ok)

    # (d) unknown task ids are surfaced, never silently dropped.
    res_unknown = bench.evaluate_output({"task-9999-not-real": "{}"}, run_id="st")
    unk_ok = res_unknown.get("_unknown_task_ids") == ["task-9999-not-real"]
    print(f"--- (d) unknown task id surfaced: {'OK' if unk_ok else 'WRONG'} ---")
    ok.append(unk_ok)

    all_ok = all(ok)
    print(
        "\n=== self-test summary: "
        + ("ALL CASES BEHAVED CORRECTLY" if all_ok else "FAILURE — see above")
        + " ==="
    )
    return 0 if all_ok else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    print(__doc__)
    print("Usage: python3 metacoin_benchmark.py --selftest")
