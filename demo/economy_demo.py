"""economy_demo.py — Phase 1 deterministic SIMULATED 30-day agent economy demo.

================================ HONESTY / SCOPE (READ ME) ================================
Research-stage, ZERO-VALUE, no token, no money, no mainnet, no networking, no payments.

This module runs a persistent, deterministic, SIMULATED 30-day economy loop: each
simulated day the agent runs one of the thirteen registered space-engineering tasks,
submits the result for verification, EARNS zero-value Test-META only if verification
passes (via the faucet, which has no verification-free credit path), and SPENDS
Test-META on simulated next-day compute via the x402 stub.

SIMULATED TIME: the 30 "days" are DETERMINISTIC DAY INDICES (0..29), not wall-clock
time. Nothing here ran for 30 real days, and no artifact of this module may imply it
did. There are NO wall-clock timestamps anywhere in the state, the log, or the summary
— day indices only.

DETERMINISM IS NOT MARKET BEHAVIOR: this is a scripted, deterministic economic loop
demonstrating the earn->verify->spend MECHANICS. It is not emergent market behavior,
not price discovery, not consensus, not payment. Test-META is a zero-value testnet
placeholder (MIP-0001 paragraph 3, MIP-0002 paragraph 8).

PLANNED TAMPER DRILL: on simulated day 17 the submission is DELIBERATELY perturbed
(one numeric summary field, the same tamper-helper pattern as agent_loop.py) so the
verifier must REJECT it. This is a PLANNED DRILL proving the economy handles rejection
(no earnings that day; spending continues from prior verified earnings). It is never
"detected fraud" — the tampering is scripted by the operator and labeled drill=true.

NEVER FUDGED: every day's verification outcome is recorded exactly as the verifier
returned it. If a day unexpectedly failed, the log would show it.

REUSE, NOT REIMPLEMENTATION: this module only calls the public APIs of the existing
demo modules — agent_loop.build_submission() (honest submission + the tamper-helper
pattern), verify_gates.verify() (task-agnostic Gate-1/Gate-2), the Test-META faucet
(dispense/spend/balance_of — the faucet owns ALL balance changes), and
x402_spend_stub.buy_compute() (which spends only through faucet.spend()). Each run
uses a fresh _Faucet instance — explicitly supported by x402_spend_stub.buy_compute's
contract ("the demo.test_meta_faucet module or a _Faucet instance") — so runs never
contaminate each other and the loop is deterministic.

INITIAL_FAUCET_GRANT is 0 BY DESIGN: the faucet deliberately has NO verification-free
credit path (crediting is a name-mangled private method reachable only from a passing
dispense()), so an ungated "initial grant" cannot exist without violating the faucet's
integrity invariant. The economy starts at zero and the first verified day funds the
loop.

PERSISTENCE: economy_state.json checkpoints (day, balance, log-so-far) so the demo can
advance one simulated day per invocation (--advance-day, resumable across processes)
or run all 30 at once (--run-all). Because the faucet is in-memory only, a resumed
process restores the carried balance by dispensing it against THAT day's verified
honest submission (the only credit path the faucet allows); the restore is internal
mechanics — it is NEVER counted as earnings in the log. REQUIRED PROPERTY (asserted in
the self-test): 30 x --advance-day and a single --run-all produce byte-identical logs.

Standard library only. Not financial or legal advice.

Usage:
    python3 demo/economy_demo.py --run-all
    python3 demo/economy_demo.py --advance-day     # one simulated day per call
    python3 demo/economy_demo.py --status
    python3 demo/economy_demo.py --reset
    python3 demo/economy_demo.py                   # no args -> self-test (house style)
"""

import argparse
import copy
import hashlib
import json
import os
import sys

# Make absolute imports resolve when run directly (repo root on sys.path).
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
from demo.agent_loop import build_submission, _tamper_first_numeric
from demo.verify_gates import verify
from demo.test_meta_faucet import _Faucet
from demo.x402_spend_stub import buy_compute

# ------------------------------ constants ------------------------------------
SCHEMA_VERSION = "economy-log/0.1"
STATE_SCHEMA_VERSION = "economy-state/0.1"

SIMULATED_DAYS = 30          # deterministic day INDICES 0..29 — never wall-clock time
SEED = 42                    # reserved per spec; NO randomness is used anywhere here
INITIAL_FAUCET_GRANT = 0     # BY DESIGN — the faucet has no verification-free credit
                             # path, so the economy starts at zero (see docstring)
DAILY_EARN = 2               # mirrors agent_loop's standard round (dispense=2)
DAILY_COMPUTE_SPEND = 1      # mirrors agent_loop's standard round (spend=1)
TAMPER_DRILL_DAY = 17        # 0-indexed simulated day of the PLANNED tamper drill
ROUND_DECIMALS = 6           # applied to every numeric that enters the log (all
                             # current economy quantities are integer Test-META)

AGENT_ADDRESS = "economy-sim-agent"

# The 13 registered tasks in task-id order; day d runs TASKS[d % 13], so every task is
# exercised (30 days over 13 tasks: tasks 0001-0004 three times, the rest twice).
TASKS = [
    ("task-0001", task_0001), ("task-0002", task_0002), ("task-0003", task_0003),
    ("task-0004", task_0004), ("task-0005", task_0005), ("task-0006", task_0006),
    ("task-0007", task_0007), ("task-0008", task_0008), ("task-0009", task_0009),
    ("task-0010", task_0010), ("task-0011", task_0011), ("task-0012", task_0012),
    ("task-0013", task_0013),
]

DEFAULT_STATE_PATH = os.path.join(_REPO_ROOT, "economy_state.json")
DEFAULT_LOG_PATH = os.path.join(_REPO_ROOT, "economy_log.json")

LABELS = {
    "zero_value": True,
    "no_token": True,
    "simulated_time": True,
    "operator_relationship": "same-operator",
}


# ------------------------------ canonical helpers ----------------------------
def canonical_json(obj) -> str:
    """Canonical JSON: sorted keys, compact separators, ASCII — byte-stable for hashing."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def compute_log_hash(log: dict) -> str:
    """SHA-256 hex of the canonical log WITHOUT its economy_log_hash field (the same
    anti-circularity pattern as the WMID / catalog / ACI report hashes)."""
    content = {k: v for k, v in log.items() if k != "economy_log_hash"}
    return hashlib.sha256(canonical_json(content).encode("utf-8")).hexdigest()


def _num(x):
    """Round any float entering the log to ROUND_DECIMALS; integers pass through."""
    if isinstance(x, float):
        return round(x, ROUND_DECIMALS)
    return x


# ------------------------------ the daily mechanics --------------------------
def _run_day(faucet, day: int, balance_before: int) -> dict:
    """Run ONE simulated day against `faucet` and return the per-day log entry.

    verify -> earn (faucet.dispense; credits only on a passing verify) -> spend
    (x402 buy_compute -> faucet.spend). On TAMPER_DRILL_DAY the day's submission is a
    deliberately perturbed COPY (PLANNED DRILL — labeled drill=true, never fraud);
    the honest submission still exists so balance restoration stays possible.
    """
    short_id, module = TASKS[day % len(TASKS)]

    # RUN + PROVE: honest submission via agent_loop's builder (hash of the honest run).
    honest_submission = build_submission(module, tamper=False)
    output_hash = honest_submission["claimed_output_hash"]

    drill = (day == TAMPER_DRILL_DAY)
    if drill:
        # PLANNED TAMPER DRILL (scripted by the operator, labeled in-log as drill=true;
        # never "detected fraud"): perturb one numeric summary field on a deep copy —
        # the same tamper-helper pattern agent_loop uses — so Gate 2 must reject it.
        day_submission = {
            "task_id": honest_submission["task_id"],
            "result": copy.deepcopy(honest_submission["result"]),
            "claimed_output_hash": output_hash,
        }
        _tamper_first_numeric(day_submission["result"])
    else:
        day_submission = honest_submission

    # VERIFY (recorded exactly as returned — never fudged).
    verified = bool(verify(day_submission, task=module)["passed"])

    # EARN: dispense() independently re-verifies and credits ONLY on a pass.
    fb_before = faucet.balance_of(AGENT_ADDRESS)
    disp = faucet.dispense(AGENT_ADDRESS, day_submission, amount=DAILY_EARN, task=module)
    earned = DAILY_EARN if disp["dispensed"] else 0

    # SPEND: simulated next-day compute through the x402 stub -> faucet.spend().
    receipt = buy_compute(faucet, AGENT_ADDRESS, DAILY_COMPUTE_SPEND)
    spent = receipt["cost"] if receipt["purchased"] else 0

    # The faucet owns all balance changes — the tracked balance must agree with the
    # faucet's own delta for this day, or the economy is inconsistent (hard stop).
    fb_after = faucet.balance_of(AGENT_ADDRESS)
    if fb_after - fb_before != earned - spent:
        raise AssertionError(
            f"day {day}: faucet delta {fb_after - fb_before} != earned-spent "
            f"{earned - spent} — economy inconsistent"
        )
    balance = balance_before + earned - spent

    return {
        "day": day,
        "task": short_id,
        "output_hash": output_hash,
        "verified": verified,
        "drill": drill,
        "earned": _num(earned),
        "spent": _num(spent),
        "balance": _num(balance),
    }


def _build_log(per_day: list) -> dict:
    """Assemble the final economy log (schema economy-log/0.1) from per-day entries."""
    verified_count = sum(1 for e in per_day if e["verified"])
    log = {
        "schema": SCHEMA_VERSION,
        "simulated_days": SIMULATED_DAYS,
        "per_day": per_day,
        "summary": {
            "verified_count": verified_count,
            "rejected_count": len(per_day) - verified_count,
            "total_earned": _num(sum(e["earned"] for e in per_day)),
            "total_spent": _num(sum(e["spent"] for e in per_day)),
            "final_balance": _num(per_day[-1]["balance"] if per_day else INITIAL_FAUCET_GRANT),
            "distinct_task_count": len({e["task"] for e in per_day}),
        },
        "labels": dict(LABELS),
    }
    log["economy_log_hash"] = compute_log_hash(log)
    return log


def simulate_all() -> dict:
    """Run the full 30-simulated-day economy in memory (no files) and return the log.

    Pure and deterministic: a fresh faucet, day indices only, no randomness, no
    wall-clock reads. This is also the coordinator's independent re-run path.
    """
    faucet = _Faucet()  # fresh instance — sanctioned by buy_compute's contract
    balance = INITIAL_FAUCET_GRANT
    per_day = []
    for day in range(SIMULATED_DAYS):
        entry = _run_day(faucet, day, balance)
        balance = entry["balance"]
        per_day.append(entry)
    return _build_log(per_day)


# ------------------------------ persistence ----------------------------------
def _load_state(state_path: str) -> dict:
    if not os.path.exists(state_path):
        return {"schema": STATE_SCHEMA_VERSION, "day": 0,
                "balance": INITIAL_FAUCET_GRANT, "log": [], "labels": dict(LABELS)}
    with open(state_path, "r", encoding="utf-8") as f:
        state = json.load(f)
    if state.get("schema") != STATE_SCHEMA_VERSION:
        raise ValueError(f"unsupported state schema {state.get('schema')!r} in {state_path}")
    return state


def _save_state(state: dict, state_path: str) -> None:
    with open(state_path, "w", encoding="utf-8") as f:
        f.write(json.dumps(state, indent=2, sort_keys=True) + "\n")


def _write_log(log: dict, log_path: str) -> None:
    with open(log_path, "w", encoding="utf-8") as f:
        f.write(json.dumps(log, indent=2, sort_keys=True) + "\n")


def advance_day(state_path: str = DEFAULT_STATE_PATH,
                log_path: str = DEFAULT_LOG_PATH) -> dict:
    """Advance the persistent simulation by ONE simulated day (resumable).

    Uses a FRESH faucet every call (the faucet is in-memory only, so a resumed process
    cannot inherit one). The carried balance is restored by dispensing it against the
    day's verified honest submission — the ONLY credit path the faucet allows — and is
    never counted as earnings. Writes the final log when day 30 completes.
    """
    state = _load_state(state_path)
    day = state["day"]
    if day >= SIMULATED_DAYS:
        raise ValueError(f"simulation already complete ({SIMULATED_DAYS} simulated days); "
                         "use --reset to start over")

    faucet = _Faucet()
    balance = state["balance"]
    if balance > 0:
        # BALANCE RESTORE (internal mechanics, not earnings): re-credit the balance the
        # agent already earned on prior simulated days (recorded in the persisted log),
        # gated — like every credit — behind a verified honest submission for today's
        # task, because the faucet has no verification-free credit path.
        short_id, module = TASKS[day % len(TASKS)]
        restore = faucet.dispense(AGENT_ADDRESS, build_submission(module, tamper=False),
                                  amount=balance, task=module)
        if not restore["dispensed"]:
            raise AssertionError(f"day {day}: balance restore failed unexpectedly")

    entry = _run_day(faucet, day, balance)
    state["day"] = day + 1
    state["balance"] = entry["balance"]
    state["log"].append(entry)
    _save_state(state, state_path)

    if state["day"] == SIMULATED_DAYS:
        _write_log(_build_log(state["log"]), log_path)
    return entry


def run_all(state_path: str = DEFAULT_STATE_PATH,
            log_path: str = DEFAULT_LOG_PATH) -> dict:
    """Run all 30 simulated days from a clean start; write state + final log."""
    log = simulate_all()
    state = {"schema": STATE_SCHEMA_VERSION, "day": SIMULATED_DAYS,
             "balance": log["summary"]["final_balance"], "log": log["per_day"],
             "labels": dict(LABELS)}
    _save_state(state, state_path)
    _write_log(log, log_path)
    return log


# ------------------------------ CLI ------------------------------------------
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="economy_demo.py",
        description=(
            "MetaCoin Phase 1 deterministic SIMULATED 30-day agent economy demo "
            "(research-stage, ZERO-VALUE, no token). Simulated day indices, not real "
            "time; scripted determinism, not market behavior."
        ),
        epilog=(
            "Day 17 is a PLANNED tamper drill (labeled drill=true — never 'detected "
            "fraud'). Earnings only on verified work; the faucet has no "
            "verification-free credit path. Not consensus, not mainnet, not payment, "
            "not a token."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--run-all", action="store_true",
                      help="run all 30 simulated days from a clean start")
    mode.add_argument("--advance-day", action="store_true",
                      help="advance the persistent simulation by one simulated day")
    mode.add_argument("--reset", action="store_true",
                      help="clear the persistent state file")
    mode.add_argument("--status", action="store_true",
                      help="print the persistent simulation status")
    mode.add_argument("--selftest", action="store_true",
                      help="run the self-test (temp files only; default with no args)")
    parser.add_argument("--state", default=DEFAULT_STATE_PATH,
                        help=f"state file path (default: {DEFAULT_STATE_PATH})")
    parser.add_argument("--out", default=DEFAULT_LOG_PATH,
                        help=f"final log path (default: {DEFAULT_LOG_PATH})")
    args = parser.parse_args(argv)

    if args.reset:
        if os.path.exists(args.state):
            os.remove(args.state)
            print(f"cleared state file: {args.state}")
        else:
            print(f"no state file to clear at {args.state}")
        return 0

    if args.status:
        state = _load_state(args.state)
        print(f"simulated day : {state['day']}/{SIMULATED_DAYS} (day indices, not real time)")
        print(f"balance       : {state['balance']} Test-META (zero value)")
        print(f"logged days   : {len(state['log'])}")
        return 0

    if args.run_all:
        log = run_all(args.state, args.out)
        s = log["summary"]
        print(f"ran {SIMULATED_DAYS} SIMULATED days (day indices, not real time)")
        print(f"  verified {s['verified_count']} / rejected {s['rejected_count']} "
              f"(the rejection is the planned day-{TAMPER_DRILL_DAY} tamper drill)")
        print(f"  earned {s['total_earned']} / spent {s['total_spent']} / "
              f"final balance {s['final_balance']} Test-META (zero value)")
        print(f"  distinct tasks exercised: {s['distinct_task_count']}")
        print(f"  economy_log_hash: {log['economy_log_hash']}")
        print(f"wrote {args.out} and {args.state}")
        return 0

    if args.advance_day:
        entry = advance_day(args.state, args.out)
        drill_note = "  [PLANNED TAMPER DRILL — rejection expected]" if entry["drill"] else ""
        print(f"simulated day {entry['day']}: task {entry['task']} | "
              f"verify {'PASS' if entry['verified'] else 'FAIL'} | "
              f"earned +{entry['earned']} | spent -{entry['spent']} | "
              f"balance {entry['balance']}{drill_note}")
        if entry["day"] == SIMULATED_DAYS - 1:
            print(f"simulation complete — wrote {args.out}")
        return 0

    # No command (or --selftest) -> self-test, matching the demo-suite invocation style.
    return _selftest()


# ============================== SELF-TEST ====================================
def _selftest() -> int:
    """Temp-only self-test: determinism, run-all == day-by-day, drill placement,
    balance arithmetic, hash recompute, task coverage, and no-timestamp guarantee."""
    import shutil
    import tempfile

    print("=== demo/economy_demo.py self-test (temp files only; simulated days) ===")
    print("30 SIMULATED day indices — not wall-clock time. Zero-value Test-META only.\n")

    root_before = set(os.listdir(_REPO_ROOT))
    checks = []
    tmp = tempfile.mkdtemp(prefix=f"economy_selftest_{os.getpid()}_")
    try:
        # (1) determinism: two full runs are byte-identical (same log hash)
        log1 = simulate_all()
        log2 = simulate_all()
        checks.append(("two --run-all equivalents byte-identical",
                       canonical_json(log1) == canonical_json(log2)))
        checks.append(("economy_log_hash identical across runs",
                       log1["economy_log_hash"] == log2["economy_log_hash"]))

        # (2) run-all == 30 x advance-day (fresh faucet + balance restore every call)
        state_p = os.path.join(tmp, "state.json")
        log_p = os.path.join(tmp, "log.json")
        for _ in range(SIMULATED_DAYS):
            advance_day(state_p, log_p)
        with open(log_p, "r", encoding="utf-8") as f:
            log_daily = json.load(f)
        checks.append(("30 x --advance-day == one --run-all (byte-identical logs)",
                       canonical_json(log_daily) == canonical_json(log1)))

        # (3) the planned drill lands exactly on day 17: 29 verified / 1 rejected
        drill_days = [e["day"] for e in log1["per_day"] if e["drill"]]
        rejected_days = [e["day"] for e in log1["per_day"] if not e["verified"]]
        checks.append(("drill on day 17 exactly; 29 verified / 1 rejected",
                       drill_days == [TAMPER_DRILL_DAY]
                       and rejected_days == [TAMPER_DRILL_DAY]
                       and log1["summary"]["verified_count"] == SIMULATED_DAYS - 1
                       and log1["summary"]["rejected_count"] == 1))
        drill_entry = log1["per_day"][TAMPER_DRILL_DAY]
        checks.append(("drill day earns nothing but still spends",
                       drill_entry["earned"] == 0
                       and drill_entry["spent"] == DAILY_COMPUTE_SPEND))

        # (4) balance arithmetic self-consistent (per-day and aggregate)
        s = log1["summary"]
        agg_ok = (s["final_balance"]
                  == INITIAL_FAUCET_GRANT + s["total_earned"] - s["total_spent"])
        run_bal = INITIAL_FAUCET_GRANT
        per_ok = True
        for e in log1["per_day"]:
            run_bal = run_bal + e["earned"] - e["spent"]
            per_ok = per_ok and (e["balance"] == run_bal)
        checks.append(("final_balance == INITIAL + earned - spent (and per-day)",
                       agg_ok and per_ok))

        # (5) log hash recomputes (anti-circularity pattern)
        checks.append(("economy_log_hash recomputes from content",
                       compute_log_hash(log1) == log1["economy_log_hash"]))

        # (6) all 13 distinct tasks appear
        checks.append(("all 13 distinct tasks exercised",
                       s["distinct_task_count"] == len(TASKS)
                       and {e["task"] for e in log1["per_day"]}
                       == {t[0] for t in TASKS}))

        # (7) no wall-clock timestamps anywhere (day indices only)
        def _no_time_keys(obj):
            if isinstance(obj, dict):
                for k, v in obj.items():
                    lk = str(k).lower()
                    if "time" in lk or "date" in lk or lk.endswith("_at"):
                        return False
                    if not _no_time_keys(v):
                        return False
            elif isinstance(obj, list):
                return all(_no_time_keys(v) for v in obj)
            return True
        # "simulated_time" in labels is a boolean HONESTY LABEL, not a timestamp —
        # exclude labels from the key scan and assert its value is exactly True.
        scan = {k: v for k, v in log1.items() if k != "labels"}
        checks.append(("no wall-clock timestamps in the log (day indices only)",
                       _no_time_keys(scan) and log1["labels"]["simulated_time"] is True))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    stray = sorted(set(os.listdir(_REPO_ROOT)) - root_before)
    checks.append(("no stray files in repo root", not stray))

    print("--- self-test invariants ---")
    failures = 0
    for name, passed in checks:
        print(f"{name:58s}: {'PASS' if passed else 'FAIL'}")
        if not passed:
            failures += 1
    if stray:
        print(f"    stray in repo root: {stray}")

    ok = failures == 0
    print("\n=== self-test summary: " +
          ("ALL CHECKS BEHAVED CORRECTLY" if ok else "FAILURE — see above") + " ===")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
