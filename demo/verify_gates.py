"""verify_gates.py — task-agnostic Gate-1 / Gate-2 verifier for the Phase 1 agentic demo.

Research-only. Implements the CI-side checks from MIP-0002:
  * Gate 1 — Integrity (here: a documented software stand-in for hardware attestation)
  * Gate 2 — Reproducibility (the real check: independent re-run + canonical-hash match)

This verifier is TASK-AGNOSTIC: it works with any task module that exposes the standard
interface — compute() -> dict, canonical_json(result) -> str, output_hash(result) -> str.
task-0001 (lunar link budget) and task-0002 (two-body orbit propagation) both satisfy it.
The verification functions accept the task as a parameter: either the task module itself,
or a task-identifier string that maps to a registered module. When no task is specified
they default to task-0001, so existing single-task callers behave exactly as before.

Gate 3 (Usefulness) is intentionally OUT OF SCOPE for this software demo; it is the
bounded optimistic-oracle + human-council layer described in MIP-0002 paragraph 2 and is
deferred. Test-META is a zero-value testnet placeholder and never mints base supply
(MIP-0001 paragraph 3, MIP-0002 paragraph 8). Standard library only. Not financial or
legal advice.

A *submission* is a dict shaped like:
    {
        "result": <dict>,                 # the agent's claimed task result
        "claimed_output_hash": <str>,     # the agent's claimed SHA-256 of its result
    }
"""

import os
import sys

# Make the requested absolute import path work when this file is run directly. The script
# directory is .../demo, so the repo root is its parent. Adding the repo root to sys.path
# lets `demo` resolve as a namespace package (no __init__.py required on Python 3.3+).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import demo.tasks.task_0001_lunar_link_budget as task_0001
import demo.tasks.task_0002_orbit_propagation as task_0002
import demo.tasks.task_0003_power_eclipse as task_0003
import demo.tasks.task_0004_comms_access as task_0004
import demo.tasks.task_0005_rover_path as task_0005
import demo.tasks.task_0006_docking_approach as task_0006
import demo.tasks.task_0007_hohmann_transfer as task_0007
import demo.tasks.task_0008_arm_inverse_kinematics as task_0008
import demo.tasks.task_0009_power_budget as task_0009
import demo.tasks.task_0010_thermal_equilibrium as task_0010
import demo.tasks.task_0011_ballistic_reentry as task_0011
import demo.tasks.task_0012_comms_link_budget as task_0012
import demo.tasks.task_0013_lambert_transfer as task_0013
import demo.tasks.task_0014_fdir_state_machine as task_0014
import demo.tasks.task_0015_sabatier_isru as task_0015
import demo.tasks.task_0016_triad_attitude as task_0016

# Default task when a caller assumes a single task (keeps prior behavior unchanged).
_DEFAULT_TASK = task_0001

# Registry mapping a canonical task-identifier string to its module. The keys match the
# "task_id" each task's compute() emits, so a submission's task_id can also resolve here.
_TASK_REGISTRY = {
    "task-0001-lunar-link-budget": task_0001,
    "task-0002-orbit-propagation": task_0002,
    "task-0003-power-eclipse": task_0003,
    "task-0004-comms-access": task_0004,
    "task-0005-rover-path": task_0005,
    "task-0006-docking-approach": task_0006,
    "task-0007-hohmann-transfer": task_0007,
    "task-0008-arm-inverse-kinematics": task_0008,
    "task-0009-power-budget": task_0009,
    "task-0010-thermal-equilibrium": task_0010,
    "task-0011-ballistic-reentry": task_0011,
    "task-0012-comms-link-budget": task_0012,
    "task-0013-lambert-transfer": task_0013,
    "task-0014-fdir-state-machine": task_0014,
    "task-0015-sabatier-isru": task_0015,
    "task-0016-triad-attitude": task_0016,
}


def _resolve_task(task=None):
    """Resolve `task` to a task module exposing the standard interface.

    Accepts:
      * None        -> the default task module (task-0001), so behavior is unchanged.
      * a string    -> looked up in _TASK_REGISTRY (a task identifier).
      * a module    -> used directly, after checking it exposes the required interface.

    Raises KeyError for an unknown identifier and TypeError for an object missing the
    compute()/canonical_json()/output_hash() interface.
    """
    if task is None:
        return _DEFAULT_TASK
    if isinstance(task, str):
        try:
            return _TASK_REGISTRY[task]
        except KeyError:
            raise KeyError(
                f"unknown task identifier {task!r}; known tasks: {sorted(_TASK_REGISTRY)}"
            )
    # Assume a module-like object; verify it speaks the standard task interface.
    for attr in ("compute", "canonical_json", "output_hash"):
        if not callable(getattr(task, attr, None)):
            raise TypeError(
                f"task object {task!r} is missing required interface method {attr}()"
            )
    return task


def verify_gate1_integrity(submission: dict, task=None) -> dict:
    """Gate 1 — Integrity. PLACEHOLDER (software stand-in for hardware attestation).

    In production, Gate 1 is satisfied by a vendor-agnostic TEE attestation OR a
    deterministic re-run by validators (MIP-0002 paragraph 2). Real hardware attestation
    is not available in this software demo, so here we use the *deterministic re-run*
    stand-in: we run the task twice and confirm the execution is reproducible (stable).
    This proves execution integrity only.

    IMPORTANT: attestation is one gate of three, never the whole proof. Integrity does
    not prove value, and it does not prove the agent's *submitted* result is correct —
    that is Gate 2's job. This stub deliberately does not look at the submission's
    claimed result or hash. The task to re-run is resolved from `task` (default task-0001).
    """
    task_mod = _resolve_task(task)
    first = task_mod.compute()
    second = task_mod.compute()
    deterministic = first == second
    return {
        "gate": "gate1_integrity",
        "passed": bool(deterministic),
        "reason": (
            "PASS (software stand-in): deterministic re-run reproduced an identical "
            "execution; used in place of hardware TEE attestation. Attestation is one "
            "gate of three, never the whole proof (MIP-0002 paragraph 2)."
            if deterministic
            else "REJECT: task execution was not deterministic on re-run."
        ),
    }


def verify_gate2_reproducibility(submission: dict, task=None) -> dict:
    """Gate 2 — Reproducibility. The real check.

    The verifier independently re-runs the task's compute(), recomputes the canonical
    SHA-256 hash, and compares against the submission. It passes IF AND ONLY IF:
        submitted_hash == recomputed_hash  AND  submitted_result == recomputed_result
    Any mismatch is an automatic rejection (MIP-0002 paragraph 2, Gate 2). The task to
    re-run is resolved from `task` (default task-0001).
    """
    task_mod = _resolve_task(task)
    submitted_result = submission.get("result")
    submitted_hash = submission.get("claimed_output_hash")

    recomputed_result = task_mod.compute()
    recomputed_hash = task_mod.output_hash(recomputed_result)

    hash_match = submitted_hash == recomputed_hash
    result_match = submitted_result == recomputed_result
    passed = hash_match and result_match

    if passed:
        reason = (
            "PASS: submitted output hash and result are byte-identical to an "
            "independent re-run."
        )
    else:
        problems = []
        if not hash_match:
            problems.append("submitted hash != recomputed hash")
        if not result_match:
            problems.append("submitted result != recomputed result")
        reason = "REJECT (auto-reject): " + "; ".join(problems) + "."

    return {
        "gate": "gate2_reproducibility",
        "passed": bool(passed),
        "submitted_hash": submitted_hash,
        "recomputed_hash": recomputed_hash,
        "reason": reason,
    }


def verify(submission: dict, task=None) -> dict:
    """Run Gate 1 then Gate 2 for `task`; overall pass requires BOTH gates to pass.

    Gate 3 (Usefulness) is out of scope for this software demo — its bounded
    MECHANICAL lifecycle (bounty caps, challenge windows, scripted adjudication)
    now lives in protocol/gate3_process.py, while the usefulness JUDGMENT seat
    itself stays honestly vacant (MIP-0002 paragraph 2). Here we automate only
    Gates 1-2, which is sufficient to prove the earn->spend loop. The task
    defaults to task-0001 so existing single-task callers are unaffected.
    """
    gate1 = verify_gate1_integrity(submission, task)
    gate2 = verify_gate2_reproducibility(submission, task)
    overall = bool(gate1["passed"] and gate2["passed"])
    return {
        "passed": overall,
        "gate1": gate1,
        "gate2": gate2,
        "note": "Gate 3 (usefulness) is deferred to the oracle/council and not evaluated here.",
    }


def assert_task_reproducible(task=None) -> dict:
    """Actively assert a task is reproducible by running compute() TWICE independently.

    This is the active two-run reproducibility assertion (previously missing): it does not
    look at any submission — it runs the task's own compute() twice, canonically hashes
    each run, and reports whether the two hashes are byte-identical. A task is reproducible
    IF AND ONLY IF hash_run1 == hash_run2. This is the property Gate 2 relies on: an
    independent re-run must reproduce the same canonical output.

    Returns: {task, reproducible, hash_run1, hash_run2, reason}. The task is resolved from
    `task` (module or identifier; default task-0001).
    """
    task_mod = _resolve_task(task)

    first = task_mod.compute()
    second = task_mod.compute()
    hash_run1 = task_mod.output_hash(first)
    hash_run2 = task_mod.output_hash(second)
    reproducible = hash_run1 == hash_run2

    # Prefer the task's self-declared id; fall back to the module name.
    task_id = first.get("task_id", getattr(task_mod, "__name__", "unknown"))

    reason = (
        "REPRODUCIBLE: two independent compute() runs produced byte-identical canonical hashes."
        if reproducible
        else "NOT REPRODUCIBLE: the two independent runs produced different canonical hashes."
    )

    return {
        "task": task_id,
        "reproducible": bool(reproducible),
        "hash_run1": hash_run1,
        "hash_run2": hash_run2,
        "reason": reason,
    }


if __name__ == "__main__":
    import copy

    def _tamper(result: dict) -> dict:
        """Return a deep copy of `result` with the first non-bool numeric summary field bumped.

        Task-agnostic AND robust to result shape: every task emits a "summary" dict of
        numeric fields, whereas "results" varies (task-0005's results is a list of [row,col]
        lists, not dicts). Perturbing the summary therefore works for all tasks. Bumping the
        first numeric (non-bool) summary value by 1.0 yields a result that no longer matches
        an independent re-run (Gate 2 must reject it).
        """
        tampered = copy.deepcopy(result)
        summary = tampered["summary"]
        for key, value in summary.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                summary[key] = value + 1.0
                break
        return tampered

    # Tasks to exercise. Each must pass honest, reject tampered, and be two-run reproducible.
    TASKS = [
        ("task-0001-lunar-link-budget", task_0001),
        ("task-0002-orbit-propagation", task_0002),
        ("task-0003-power-eclipse", task_0003),
        ("task-0004-comms-access", task_0004),
        ("task-0005-rover-path", task_0005),
        ("task-0006-docking-approach", task_0006),
        ("task-0007-hohmann-transfer", task_0007),
        ("task-0008-arm-inverse-kinematics", task_0008),
        ("task-0009-power-budget", task_0009),
        ("task-0010-thermal-equilibrium", task_0010),
        ("task-0011-ballistic-reentry", task_0011),
        ("task-0012-comms-link-budget", task_0012),
        ("task-0013-lambert-transfer", task_0013),
        ("task-0014-fdir-state-machine", task_0014),
        ("task-0015-sabatier-isru", task_0015),
        ("task-0016-triad-attitude", task_0016),
    ]

    print("=== verify_gates.py self-test (task-agnostic: Gate-1 stand-in + Gate-2 + two-run reproducibility) ===\n")

    all_ok = []
    for task_id, task_mod in TASKS:
        print(f"########## {task_id} ##########")

        # (a) HONEST submission — real run with the correct hash. Must PASS.
        honest_result = task_mod.compute()
        honest_submission = {
            "result": honest_result,
            "claimed_output_hash": task_mod.output_hash(honest_result),
        }
        v_honest = verify(honest_submission, task=task_mod)
        a_ok = v_honest["passed"] is True
        print("  (a) HONEST submission     -> expected PASS   | "
              f"verdict {'PASS' if v_honest['passed'] else 'REJECTED'}   | "
              f"{'OK' if a_ok else 'WRONG'}")
        print(f"        gate1: {v_honest['gate1']['reason']}")
        print(f"        gate2: {v_honest['gate2']['reason']}")

        # (b) TAMPERED submission — alter one number, keep the original (now-stale) hash.
        tampered_submission = {
            "result": _tamper(honest_result),
            "claimed_output_hash": task_mod.output_hash(honest_result),
        }
        v_tampered = verify(tampered_submission, task=task_mod)
        b_ok = v_tampered["passed"] is False
        print("  (b) TAMPERED submission   -> expected REJECT | "
              f"verdict {'PASS' if v_tampered['passed'] else 'REJECTED'} | "
              f"{'OK' if b_ok else 'WRONG'}")
        print(f"        gate2: {v_tampered['gate2']['reason']}")

        # (c) Two-run reproducibility assertion — must be reproducible:True.
        repro = assert_task_reproducible(task_mod)
        c_ok = repro["reproducible"] is True
        print("  (c) two-run reproducibility -> expected reproducible:True | "
              f"got {repro['reproducible']} | {'OK' if c_ok else 'WRONG'}")
        print(f"        hash_run1: {repro['hash_run1']}")
        print(f"        hash_run2: {repro['hash_run2']}")
        print(f"        reason   : {repro['reason']}")

        print()
        all_ok.extend([a_ok, b_ok, c_ok])

    ok = all(all_ok)
    print("=== self-test summary: " + ("ALL CASES BEHAVED CORRECTLY (all tasks)" if ok else "FAILURE — see above") + " ===")
    sys.exit(0 if ok else 1)
