"""agent_loop.py — Phase 1 demo orchestrator (the full WHITEPAPER paragraph 10 cycle).

This wires the demo end to end, one round per task attempt:

    ASSIGN -> RUN -> PROVE/BUILD SUBMISSION -> VERIFY -> DISPENSE -> SPEND -> repeat

Each round can be assigned a specific task (task-0001 lunar link budget OR task-0002
two-body orbit propagation). The task flows through compute(), build_submission(),
verify(), and dispense() via the now task-agnostic verifier and faucet (both accept an
optional task parameter, defaulting to task-0001). This means the same earn->verify->spend
loop runs on genuine astrodynamics work, not only the illustrative link-budget task.

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
from demo.verify_gates import verify
import demo.test_meta_faucet as faucet
from demo.x402_spend_stub import buy_compute

# Default task when a round does not name one (keeps prior single-task behavior unchanged).
DEFAULT_TASK = task_0001


def _tamper_first_numeric(result: dict) -> None:
    """Alter the first non-bool numeric field in result["summary"], in place (generic).

    Every task emits a "summary" dict of numeric fields, whereas "results" varies in shape
    (task-0005's results is a list of [row,col] lists, not dicts). Perturbing the summary
    therefore works for all tasks: bumping the first numeric (non-bool) value by 1.0 yields
    a result that no longer matches an independent re-run — which Gate 2 must reject.
    """
    summary = result["summary"]
    for key, value in summary.items():
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            summary[key] = value + 1.0
            return


def build_submission(task, tamper: bool = False) -> dict:
    """PROVE / BUILD SUBMISSION for the given task module.

    Run the task and package the result with its correct canonical output hash, using the
    assigned task's own compute()/canonical_json()/output_hash() (task-agnostic).

    When `tamper` is True we simulate a dishonest agent: we alter one number in the
    result AFTER computing the hash, so the submitted result no longer matches its
    claimed hash (and no longer matches an independent re-run). Gate 2 must reject this,
    for whichever task ran.
    """
    result = task.compute()

    # The canonical JSON is exactly what gets hashed; output_hash() canonicalizes
    # internally, but we materialize it here to make the proof step explicit. The hash is
    # computed from the HONEST result, before any tampering.
    canonical = task.canonical_json(result)
    claimed_hash = task.output_hash(result)
    task_id = result.get("task_id", getattr(task, "__name__", "unknown"))

    if tamper:
        result = copy.deepcopy(result)
        _tamper_first_numeric(result)  # flip a single numeric value

    return {
        "task_id": task_id,
        "result": result,
        "claimed_output_hash": claimed_hash,
        # Carried only for visibility/debugging; not used by the verifier.
        "_canonical_len": len(canonical),
    }


def run_round(round_num: int, address: str, task, tamper: bool, dispense_amount: int, spend_cost: int) -> dict:
    """Run one full cycle for `address` on `task` and return a summary dict (also prints a line)."""
    # 1. ASSIGN — the task for this round (task-0001 link budget or task-0002 orbit prop).
    # 2. RUN + 3. PROVE/BUILD SUBMISSION.
    submission = build_submission(task, tamper=tamper)
    task_id = submission["task_id"]

    # 4. VERIFY (for reporting). Gate-1 stand-in + Gate-2 reproducibility, for THIS task.
    verify_result = verify(submission, task=task)
    verify_passed = verify_result["passed"]

    # 5. DISPENSE — earns zero-value Test-META ONLY if verification passes. Note that
    #    dispense() independently re-verifies against the same task (defense in depth);
    #    earnings come from it.
    dispense_result = faucet.dispense(address, submission, amount=dispense_amount, task=task)
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
        "task_id": task_id,
        "verify_passed": verify_passed,
        "earned": earned,
        "spent": spent,
        "balance": balance,
        "compute_bought": compute_bought,
    }


def run_loop(scenario: list, address: str):
    """Run a sequence of rounds described by `scenario`.

    `scenario` is a list of dicts, each:
        {"task": <task module>, "tamper": bool, "dispense": int, "spend": int}
    "task" is optional and defaults to task-0001, so the number of rounds, which task each
    runs, and which are tampered are all fully configurable.

    Returns (totals, summaries): the aggregate totals dict, plus the list of per-round
    summary dicts (each carrying round, task_id, verify_passed, earned, spent, balance,
    compute_bought) so callers can assert per-round behavior, not only the aggregate.
    """
    summaries = []
    for i, cfg in enumerate(scenario, start=1):
        summaries.append(
            run_round(
                round_num=i,
                address=address,
                task=cfg.get("task", DEFAULT_TASK),
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

    totals = {
        "rounds": len(summaries),
        "passed": passed,
        "rejected": rejected,
        "total_earned": total_earned,
        "total_spent": total_spent,
        "final_balance": final_balance,
    }
    return totals, summaries


if __name__ == "__main__":
    AGENT = "agent-testnet-loop"

    # A short scenario exercising ALL ELEVEN real tasks and every path:
    #   R1 task-0001 honest : earn 2, spend 1                 -> balance 1
    #   R2 task-0002 honest : earn 2, spend 3 (drains)        -> balance 0   (real orbit task earns)
    #   R3 task-0002 TAMPER : verify FAILS, earn 0, and the   -> balance 0   (real task rejected)
    #                         spend attempt hits insufficient balance; the loop continues
    #   R4 task-0001 honest : recovers, earn 2, spend 1       -> balance 1
    #   R5 task-0003 honest : earn 2, spend 1                 -> balance 2   (eclipse/power task earns)
    #   R6 task-0004 honest : earn 2, spend 1                 -> balance 3   (comms-access task earns)
    #   R7 task-0005 honest : earn 2, spend 1                 -> balance 4   (rover-path task earns)
    #   R8 task-0006 honest : earn 2, spend 1                 -> balance 5   (docking-approach task earns)
    #   R9 task-0007 honest : earn 2, spend 1                 -> balance 6   (Hohmann-transfer task earns)
    #   R10 task-0008 honest: earn 2, spend 1                 -> balance 7   (arm-IK task earns)
    #   R11 task-0009 honest: earn 2, spend 1                 -> balance 8   (power-budget task earns)
    #   R12 task-0010 honest: earn 2, spend 1                 -> balance 9   (thermal-equilibrium task earns)
    #   R13 task-0011 honest: earn 2, spend 1                 -> balance 10  (ballistic-reentry task earns)
    scenario = [
        {"task": task_0001, "tamper": False, "dispense": 2, "spend": 1},
        {"task": task_0002, "tamper": False, "dispense": 2, "spend": 3},
        {"task": task_0002, "tamper": True, "dispense": 2, "spend": 1},
        {"task": task_0001, "tamper": False, "dispense": 2, "spend": 1},
        {"task": task_0003, "tamper": False, "dispense": 2, "spend": 1},
        {"task": task_0004, "tamper": False, "dispense": 2, "spend": 1},
        {"task": task_0005, "tamper": False, "dispense": 2, "spend": 1},
        {"task": task_0006, "tamper": False, "dispense": 2, "spend": 1},
        {"task": task_0007, "tamper": False, "dispense": 2, "spend": 1},
        {"task": task_0008, "tamper": False, "dispense": 2, "spend": 1},
        {"task": task_0009, "tamper": False, "dispense": 2, "spend": 1},
        {"task": task_0010, "tamper": False, "dispense": 2, "spend": 1},
        {"task": task_0011, "tamper": False, "dispense": 2, "spend": 1},
    ]

    # Independent ground truth for the PER-ROUND assertions: the task that must run each
    # round and whether verification must pass. Declared separately from `scenario` (which
    # only says which task to assign and whether to tamper) so the assertion is real
    # ground truth, not derived from the same config it is checking.
    expected_rounds = [
        {"task_id": "task-0001-lunar-link-budget", "verify_passed": True},
        {"task_id": "task-0002-orbit-propagation", "verify_passed": True},
        {"task_id": "task-0002-orbit-propagation", "verify_passed": False},
        {"task_id": "task-0001-lunar-link-budget", "verify_passed": True},
        {"task_id": "task-0003-power-eclipse", "verify_passed": True},
        {"task_id": "task-0004-comms-access", "verify_passed": True},
        {"task_id": "task-0005-rover-path", "verify_passed": True},
        {"task_id": "task-0006-docking-approach", "verify_passed": True},
        {"task_id": "task-0007-hohmann-transfer", "verify_passed": True},
        {"task_id": "task-0008-arm-inverse-kinematics", "verify_passed": True},
        {"task_id": "task-0009-power-budget", "verify_passed": True},
        {"task_id": "task-0010-thermal-equilibrium", "verify_passed": True},
        {"task_id": "task-0011-ballistic-reentry", "verify_passed": True},
    ]

    print("=== agent_loop.py — Phase 1 demo (assign -> run -> prove -> verify -> dispense -> spend) ===")
    print("Research-only. Test-META is a zero-value placeholder; compute is simulated.")
    print("Runs all eleven real tasks: task-0001 (link budget), task-0002 (two-body orbit "
          "propagation), task-0003 (eclipse/power budget), task-0004 (comms access), "
          "task-0005 (rover path), task-0006 (docking approach), task-0007 (Hohmann transfer), "
          "task-0008 (arm inverse kinematics), task-0009 (power budget), "
          "task-0010 (thermal equilibrium), task-0011 (ballistic re-entry).\n")

    totals, summaries = run_loop(scenario, AGENT)

    print()
    print("=== final summary ===")
    print(f"  rounds run      : {totals['rounds']}")
    print(f"  passed          : {totals['passed']}")
    print(f"  rejected        : {totals['rejected']}")
    print(f"  total earned    : {totals['total_earned']} Test-META (zero value)")
    print(f"  total spent     : {totals['total_spent']} Test-META (zero value)")
    print(f"  final balance   : {totals['final_balance']} Test-META (zero value)")

    # (1) PER-ROUND assertion — each round ran the expected task AND had the expected
    #     verify pass/fail outcome. This catches a reshuffle that leaves totals unchanged.
    print()
    print("=== per-round assertions (task identity + verify outcome) ===")
    per_round_ok = []
    count_ok = len(summaries) == len(expected_rounds)
    if not count_ok:
        print(f"  WRONG — ran {len(summaries)} rounds, expected {len(expected_rounds)}")
    for got, exp in zip(summaries, expected_rounds):
        id_ok = got["task_id"] == exp["task_id"]
        verify_ok = got["verify_passed"] == exp["verify_passed"]
        row_ok = id_ok and verify_ok
        per_round_ok.append(row_ok)
        print(
            f"  round {got['round']}: task {got['task_id']} (exp {exp['task_id']}) "
            f"-> {'match' if id_ok else 'MISMATCH'} | "
            f"verify {'PASS' if got['verify_passed'] else 'FAIL'} "
            f"(exp {'PASS' if exp['verify_passed'] else 'FAIL'}) "
            f"-> {'match' if verify_ok else 'MISMATCH'} | "
            f"{'OK' if row_ok else 'WRONG'}"
        )

    # (2) AGGREGATE assertion — kept exactly as before.
    expected = {
        "rounds": 13,
        "passed": 12,
        "rejected": 1,
        "total_earned": 24,
        "total_spent": 14,
        "final_balance": 10,
    }
    totals_ok = totals == expected

    ok = totals_ok and count_ok and all(per_round_ok)
    print()
    print(f"  per-round   : {'OK (all rounds matched expected task + verify outcome)' if (count_ok and all(per_round_ok)) else 'WRONG — see above'}")
    print(f"  aggregate   : {'OK (scenario totals match expected)' if totals_ok else f'WRONG — got {totals}, expected {expected}'}")
    print()
    print("=== self-test: " + ("OK (per-round AND aggregate assertions passed)" if ok else "WRONG — see above") + " ===")
    sys.exit(0 if ok else 1)
