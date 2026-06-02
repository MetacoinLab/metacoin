"""verify_gates.py — Gate-1 / Gate-2 verifier for the Phase 1 agentic demo.

Research-only. Implements the CI-side checks from MIP-0002:
  * Gate 1 — Integrity (here: a documented software stand-in for hardware attestation)
  * Gate 2 — Reproducibility (the real check: independent re-run + canonical-hash match)

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

from demo.tasks.task_0001_lunar_link_budget import compute, output_hash


def verify_gate1_integrity(submission: dict) -> dict:
    """Gate 1 — Integrity. PLACEHOLDER (software stand-in for hardware attestation).

    In production, Gate 1 is satisfied by a vendor-agnostic TEE attestation OR a
    deterministic re-run by validators (MIP-0002 paragraph 2). Real hardware attestation
    is not available in this software demo, so here we use the *deterministic re-run*
    stand-in: we run the task twice and confirm the execution is reproducible (stable).
    This proves execution integrity only.

    IMPORTANT: attestation is one gate of three, never the whole proof. Integrity does
    not prove value, and it does not prove the agent's *submitted* result is correct —
    that is Gate 2's job. This stub deliberately does not look at the submission's
    claimed result or hash.
    """
    first = compute()
    second = compute()
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


def verify_gate2_reproducibility(submission: dict) -> dict:
    """Gate 2 — Reproducibility. The real check.

    The verifier independently re-runs the task's compute(), recomputes the canonical
    SHA-256 hash, and compares against the submission. It passes IF AND ONLY IF:
        submitted_hash == recomputed_hash  AND  submitted_result == recomputed_result
    Any mismatch is an automatic rejection (MIP-0002 paragraph 2, Gate 2).
    """
    submitted_result = submission.get("result")
    submitted_hash = submission.get("claimed_output_hash")

    recomputed_result = compute()
    recomputed_hash = output_hash(recomputed_result)

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


def verify(submission: dict) -> dict:
    """Run Gate 1 then Gate 2; overall pass requires BOTH gates to pass.

    Gate 3 (Usefulness) is out of scope for this software demo — it is the bounded
    optimistic-oracle + human-council layer (MIP-0002 paragraph 2) and is deferred. Here
    we automate only Gates 1-2, which is sufficient to prove the earn->spend loop.
    """
    gate1 = verify_gate1_integrity(submission)
    gate2 = verify_gate2_reproducibility(submission)
    overall = bool(gate1["passed"] and gate2["passed"])
    return {
        "passed": overall,
        "gate1": gate1,
        "gate2": gate2,
        "note": "Gate 3 (usefulness) is deferred to the oracle/council and not evaluated here.",
    }


if __name__ == "__main__":
    import copy

    def _print_case(label: str, expectation: str, outcome: dict) -> bool:
        verdict = "PASS" if outcome["passed"] else "REJECTED"
        ok = (expectation == "PASS") == outcome["passed"]
        print(f"--- {label} ---")
        print(f"  expected : {expectation}")
        print(f"  verdict  : {verdict}")
        print(f"  gate1    : {outcome['gate1']['reason']}")
        print(f"  gate2    : {outcome['gate2']['reason']}")
        print(f"  self-test: {'OK' if ok else 'WRONG — verifier behaved incorrectly'}")
        print()
        return ok

    # (a) HONEST submission — built from a real run with the correct hash. Must PASS.
    honest_result = compute()
    honest_submission = {
        "result": honest_result,
        "claimed_output_hash": output_hash(honest_result),
    }

    # (b) TAMPERED RESULT — alter one number but keep the original (now-stale) hash.
    tampered_result_submission = {
        "result": copy.deepcopy(honest_result),
        "claimed_output_hash": output_hash(honest_result),  # old, "correct-looking" hash
    }
    # Flip a single value in the first result row.
    tampered_result_submission["result"]["results"][0]["link_margin_dB"] += 1.0

    # (c) TAMPERED HASH — correct result, but a garbage/wrong hash.
    tampered_hash_submission = {
        "result": copy.deepcopy(honest_result),
        "claimed_output_hash": "0" * 64,  # not a valid hash of this result
    }

    print("=== verify_gates.py self-test (Gate-1 stand-in + Gate-2 real check) ===\n")
    results_ok = []
    results_ok.append(_print_case("(a) HONEST", "PASS", verify(honest_submission)))
    results_ok.append(_print_case("(b) TAMPERED RESULT", "REJECTED", verify(tampered_result_submission)))
    results_ok.append(_print_case("(c) TAMPERED HASH", "REJECTED", verify(tampered_hash_submission)))

    all_ok = all(results_ok)
    print("=== self-test summary: " + ("ALL CASES BEHAVED CORRECTLY" if all_ok else "FAILURE — see above") + " ===")
    sys.exit(0 if all_ok else 1)
