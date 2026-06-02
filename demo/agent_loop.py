"""agent_loop.py — Phase 1 demo orchestrator (the full WHITEPAPER paragraph 10 cycle).

This wires the demo end to end, one round per task attempt:

    ASSIGN -> RUN -> PROVE/BUILD SUBMISSION -> VERIFY -> DISPENSE -> SPEND -> repeat

Research-only. Test-META is a ZERO-VALUE testnet placeholder — not MetaCoin, not base
supply, no monetary value (MIP-0001 paragraph 3, MIP-0002 paragraph 8). The "compute"
bought via the x402 stub is simulated and imaginary. Nothing here mints base supply or
moves anything of value. Not financial or legal advice. Standard library only.

The orchestrator only calls the public APIs of the other modules; it never touches the
faucet ledger directly. Earnings come solely from dispense() (which re-verifies), and
spending goes solely through the x402 stub -> faucet.spend().
"""

import copy
import os
import sys

# Make absolute imports resolve when run directly (repo root on sys.path; `demo` is a
# Python 3 namespace package, no __init__.py required).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from demo.tasks.task_0001_lunar_link_budget import canonical_json, compute, output_hash
from demo.verify_gates import verify
import demo.test_meta_faucet as faucet
from demo.x402_spend_stub import buy_compute

# The single task referenced by this demo (see demo/tasks/example_task.md).
TASK_ID = "task-0001-lunar-link-budget"


def build_submission(tamper: bool = False) -> dict:
    """PROVE / BUILD SUBMISSION.

    Run the task and package the result with its correct canonical output hash.

    When `tamper` is True we simulate a dishonest agent: we alter one number in the
    result AFTER computing the hash, so the submitted result no longer matches its
    claimed hash (and no longer matches an independent re-run). Gate 2 must reject this.
    """
    result = compute()

    # The canonical JSON is exactly what gets hashed; output_hash() canonicalizes
    # internally, but we materialize it here to make the proof step explicit.
    canonical = canonical_json(result)
    claimed_hash = output_hash(result)

    if tamper:
        result = copy.deepcopy(result)
        result["results"][0]["link_margin_dB"] += 1.0  # flip a single value

    return {
        "task_id": TASK_ID,
        "result": result,
        "claimed_output_hash": claimed_hash,
        # Carried only for visibility/debugging; not used by the verifier.
        "_canonical_len": len(canonical),
    }


def run_round(round_num: int, address: str, tamper: bool, dispense_amount: int, spend_cost: int) -> dict:
    """Run one full cycle for `address` and return a summary dict (also prints a line)."""
    # 1. ASSIGN — reference the lunar link-budget task.
    task_id = TASK_ID

    # 2. RUN + 3. PROVE/BUILD SUBMISSION.
    submission = build_submission(tamper=tamper)

    # 4. VERIFY (for reporting). Gate-1 stand-in + Gate-2 reproducibility.
    verify_result = verify(submission)
    verify_passed = verify_result["passed"]

    # 5. DISPENSE — earns zero-value Test-META ONLY if verification passes. Note that
    #    dispense() independently re-verifies (defense in depth); earnings come from it.
    dispense_result = faucet.dispense(address, submission, amount=dispense_amount)
    earned = dispense_result["amount"] if dispense_result["dispensed"] else 0

    # 6. SPEND — try to buy simulated compute for the next round via the x402 stub.
    #    If the balance is insufficient, the agent simply can't buy more compute.
    receipt = buy_compute(faucet, address, spend_cost)
    if receipt["purchased"]:
        spent = receipt["cost"]
        compute_bought = receipt["compute_units"]
        spend_note = ""
    else:
        spent = 0
        compute_bought = 0
        spend_note = f"could not buy compute ({receipt['reason']})"

    balance = faucet.balance_of(address)

    # Build a human-readable note column.
    notes = []
    if not verify_passed:
        notes.append("submission REJECTED — earned nothing")
    if spend_note:
        notes.append(spend_note)
    note_str = ("  | note: " + "; ".join(notes)) if notes else ""

    print(
        f"Round {round_num} | task {task_id} | "
        f"verify: {'PASS' if verify_passed else 'FAIL'} | "
        f"earned: +{earned} | spent: -{spent} | "
        f"balance: {balance} | compute: {compute_bought} units{note_str}"
    )

    return {
        "round": round_num,
        "verify_passed": verify_passed,
        "earned": earned,
        "spent": spent,
        "balance": balance,
        "compute_bought": compute_bought,
    }


def run_loop(scenario: list, address: str) -> dict:
    """Run a sequence of rounds described by `scenario` and return aggregate totals.

    `scenario` is a list of dicts, each: {"tamper": bool, "dispense": int, "spend": int}.
    This makes the number of rounds (and which are tampered) fully configurable.
    """
    summaries = []
    for i, cfg in enumerate(scenario, start=1):
        summaries.append(
            run_round(
                round_num=i,
                address=address,
                tamper=cfg.get("tamper", False),
                dispense_amount=cfg.get("dispense", 1),
                spend_cost=cfg.get("spend", 1),
            )
        )

    total_earned = sum(s["earned"] for s in summaries)
    total_spent = sum(s["spent"] for s in summaries)
    passed = sum(1 for s in summaries if s["verify_passed"])
    rejected = sum(1 for s in summaries if not s["verify_passed"])
    final_balance = faucet.balance_of(address)

    return {
        "rounds": len(summaries),
        "passed": passed,
        "rejected": rejected,
        "total_earned": total_earned,
        "total_spent": total_spent,
        "final_balance": final_balance,
    }


if __name__ == "__main__":
    AGENT = "agent-testnet-loop"

    # A short scenario exercising every path:
    #   R1 honest      : earn 2, spend 1            -> balance 1
    #   R2 honest      : earn 2, spend 3 (drains)   -> balance 0
    #   R3 TAMPERED    : verify FAILS, earn 0, and  -> balance 0, cannot buy compute
    #                    spend attempt hits insufficient balance
    #   R4 honest      : recovers, earn 2, spend 1  -> balance 1
    scenario = [
        {"tamper": False, "dispense": 2, "spend": 1},
        {"tamper": False, "dispense": 2, "spend": 3},
        {"tamper": True, "dispense": 2, "spend": 1},
        {"tamper": False, "dispense": 2, "spend": 1},
    ]

    print("=== agent_loop.py — Phase 1 demo (assign -> run -> prove -> verify -> dispense -> spend) ===")
    print("Research-only. Test-META is a zero-value placeholder; compute is simulated.\n")

    totals = run_loop(scenario, AGENT)

    print()
    print("=== final summary ===")
    print(f"  rounds run      : {totals['rounds']}")
    print(f"  passed          : {totals['passed']}")
    print(f"  rejected        : {totals['rejected']}")
    print(f"  total earned    : {totals['total_earned']} Test-META (zero value)")
    print(f"  total spent     : {totals['total_spent']} Test-META (zero value)")
    print(f"  final balance   : {totals['final_balance']} Test-META (zero value)")

    # Sanity self-check for the scripted scenario above.
    expected = {
        "rounds": 4,
        "passed": 3,
        "rejected": 1,
        "total_earned": 6,
        "total_spent": 5,
        "final_balance": 1,
    }
    ok = totals == expected
    print()
    print("=== self-test: " + ("OK (scenario totals match expected)" if ok else f"WRONG — got {totals}, expected {expected}") + " ===")
    sys.exit(0 if ok else 1)
