"""task_metering.py — MetaCoin compute/energy METERING runner (schema "metering-report/0.1").

================== CONSTITUTIONAL RULE (READ ME) ==================
Provenance debt is reduced ONLY by appending new evidence objects. No existing ledger
record, submission file, or anchored artifact is modified. The idx-17 catalog's
work-molecule/0.2 WMIDs must remain valid forever: a 0.2-mode rebuild must still match
them after this metering evidence exists. New measurements produce NEW records; molecules
built at schema 0.3 absorb them and get NEW WMIDs — both generations verifiable, neither
replacing the other.

================== HONESTY / SCOPE (READ ME) ==================
Research-stage, ZERO-VALUE, no token, no money, no mainnet, no networking, no payments.

This module runs each registered demo task ONCE under instrumentation and records:
  * wall_time_s  — MEASURED (time.perf_counter around the single compute() call)
  * cpu_time_s   — MEASURED (resource.getrusage user+sys delta; resource is stdlib on Linux)
  * energy_j_estimate — an ESTIMATE, never a measurement:
        energy_j_estimate = cpu_time_s x ASSUMED_CPU_POWER_W
    ASSUMED_CPU_POWER_W is an assumed constant nameplate figure for this host class.
    NO hardware power telemetry exists on this host (no RAPL/PMBus/TEE power counters
    were read) — that remains OPEN provenance debt. The estimate is labeled "estimated"
    with method "cpu-time-x-assumed-power" and must NEVER be presented as measured.

Every metered run's output_hash MUST equal the canonical hash of an unmetered reference
run — a metering run that changes the task output is a failure, by assertion. Task
output hashes are untouched by metering: timing data lives ONLY in this supplementary
evidence report, never inside anything whose hash must be reproducible.

HONEST DETERMINISM NOTE (the integrity model of this report):
  This report is NOT byte-reproducible — timing is inherently non-deterministic run to
  run, so two honest runs produce different bytes. Its integrity model is therefore
  DIFFERENT from the task hashes: report_hash fixes WHAT WAS CLAIMED at measurement
  time (a content-address of the claim, computed with the report_hash field excluded —
  the same anti-circularity pattern as the ledger entry hash and the WMID). Verifying
  PLAUSIBILITY = re-metering yields the same output_hashes and the same
  order-of-magnitude timings — NOT identical bytes. The self-test proves this honestly:
  a second metering run must produce DIFFERENT report bytes (we do not fake
  determinism) while reproducing IDENTICAL task output_hashes.

Standard library only (time, resource, os, json, hashlib, argparse). The task registry
is REUSED from protocol/verifier_cli.py — nothing is reimplemented. The canonical-JSON
helper is deliberately per-module (house style: each file stands alone for external
verifiers). Not legal, financial, investment, or security-certification advice. No NASA
affiliation or endorsement.

Usage:
    python3 demo/task_metering.py --task task-0001
    python3 demo/task_metering.py --all --out metering_report.json   # gitignored artifact
    python3 demo/task_metering.py --selftest
"""

# Suppress __pycache__/*.pyc so importing protocol modules below leaves no stray files.
# Set immediately after the docstring, BEFORE any protocol imports, so it takes effect.
import sys
sys.dont_write_bytecode = True

import argparse
import hashlib
import json
import os
import resource
import time

# Make `from protocol...` resolve when run directly (repo root on path).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# REUSE the existing task-id registry — do NOT reimplement it.
from protocol.verifier_cli import TASK_MODULES, load_task, normalize_task_id

SCHEMA_VERSION = "metering-report/0.1"

# The assumed constant CPU power figure the energy ESTIMATE is derived from. This is an
# assumed nameplate figure for this host class, NOT a measurement: no hardware power
# telemetry (RAPL/PMBus/TEE power counters) exists on this host — open provenance debt.
ASSUMED_CPU_POWER_W = 15.0
POWER_METHOD = "assumed-nameplate; no hardware telemetry on this host"

# Per-field honesty labels carried on every row: times are measured, energy is not.
LABELS = {"wall": "measured", "cpu": "measured", "energy": "estimated"}

# Fixed rounding for every recorded figure (seconds and joules): 6 decimals.
ROUND_DECIMALS = 6


# ----------------------------------------------------------------------------
# Canonical JSON + report hash (per-module helper, same discipline as ledger.py)
# ----------------------------------------------------------------------------
def canonical_json(obj) -> str:
    """Canonical JSON: sorted keys, compact separators, ASCII — byte-stable for hashing."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def compute_report_hash(report: dict) -> str:
    """SHA-256 hex of the canonical JSON of the report WITHOUT its report_hash field
    (same anti-circularity pattern as the ledger entry hash and the WMID).

    NOTE this hash fixes WHAT WAS CLAIMED at measurement time; it is NOT reproducible
    by re-running (timing varies) — see the HONEST DETERMINISM NOTE in the docstring.
    """
    content = {k: v for k, v in report.items() if k != "report_hash"}
    return hashlib.sha256(canonical_json(content).encode("utf-8")).hexdigest()


# ----------------------------------------------------------------------------
# Metering
# ----------------------------------------------------------------------------
def meter_task(task_id: str) -> dict:
    """Run `task_id`'s compute() once under instrumentation and return its evidence row.

    First derives the canonical output hash from an UNMETERED reference run, then runs
    the single metered compute() and asserts the metered output hash equals it — a
    metering run that changes the task output is a failure, never a data point.
    wall_time_s and cpu_time_s (user+sys) are MEASURED; energy_j_estimate is DERIVED
    exactly as round(cpu_time_s x ASSUMED_CPU_POWER_W, 6) from the ROUNDED cpu time, so
    the arithmetic is re-checkable from the row's own recorded fields.
    """
    short = normalize_task_id(task_id)  # KeyError (with known ids) on unknown task
    module = load_task(short)

    # Canonical reference: an unmetered run (also warms imports/caches so the metered
    # run measures the computation, not first-call setup).
    canonical_hash = module.output_hash(module.compute())

    # The single metered run: getrusage user+sys delta + perf_counter wall clock.
    ru_before = resource.getrusage(resource.RUSAGE_SELF)
    wall_before = time.perf_counter()
    result = module.compute()
    wall_after = time.perf_counter()
    ru_after = resource.getrusage(resource.RUSAGE_SELF)

    metered_hash = module.output_hash(result)
    if metered_hash != canonical_hash:
        raise RuntimeError(
            f"metering run for {short} changed the task output "
            f"({metered_hash} != canonical {canonical_hash}) — metering must never "
            "alter the computation; this run is a failure, not a data point"
        )

    wall_time_s = round(wall_after - wall_before, ROUND_DECIMALS)
    cpu_time_s = round(
        (ru_after.ru_utime - ru_before.ru_utime)
        + (ru_after.ru_stime - ru_before.ru_stime),
        ROUND_DECIMALS,
    )
    # Derived from the ROUNDED cpu time so energy == round(cpu x power, 6) holds
    # exactly against the recorded fields (an external verifier can re-check it).
    energy_j_estimate = round(cpu_time_s * ASSUMED_CPU_POWER_W, ROUND_DECIMALS)

    return {
        "task_id": short,
        "output_hash": metered_hash,
        "wall_time_s": wall_time_s,
        "cpu_time_s": cpu_time_s,
        "energy_j_estimate": energy_j_estimate,
        "labels": dict(LABELS),
    }


def build_report(task_ids=None) -> dict:
    """Meter every task (default: all registered) and return the metering report dict.

    Rows are sorted by task_id. The report is honest about its own integrity model:
    report_hash content-addresses the claim; it is NOT reproducible by re-running.
    """
    if task_ids is None:
        task_ids = sorted(TASK_MODULES)
    per_task = [meter_task(tid) for tid in sorted(normalize_task_id(t) for t in task_ids)]
    report = {
        "schema": SCHEMA_VERSION,
        "assumed_cpu_power_w": ASSUMED_CPU_POWER_W,
        "power_method": POWER_METHOD,
        "per_task": per_task,
    }
    report["report_hash"] = compute_report_hash(report)
    return report


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="task_metering.py",
        description=(
            "MetaCoin compute/energy metering runner (research-stage, ZERO-VALUE, no "
            "token). Measures wall/CPU time for the registered demo tasks and derives "
            "an ESTIMATED energy figure from an assumed constant power — never "
            "presented as a measurement."
        ),
        epilog=(
            "HONESTY: wall/CPU times are MEASURED; energy is an ESTIMATE (cpu_time_s x "
            f"{ASSUMED_CPU_POWER_W} W assumed nameplate — no hardware power telemetry "
            "exists on this host). The report is NOT byte-reproducible (timing varies); "
            "report_hash fixes the claim, not a reproducible computation. Task output "
            "hashes are untouched. Not consensus, not payment, not a token."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--task", help="meter a single task, e.g. task-0001")
    mode.add_argument("--all", action="store_true",
                      help="meter every registered task and write the report")
    mode.add_argument("--selftest", action="store_true",
                      help="run the mechanical self-test (writes nothing into the repo)")
    parser.add_argument("--out", default="metering_report.json",
                        help="report path for --all (default: metering_report.json; "
                             "gitignored local artifact)")
    args = parser.parse_args(argv)

    if args.selftest or not (args.task or args.all):
        return _selftest()

    if args.task:
        try:
            row = meter_task(args.task)
        except (KeyError, RuntimeError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(json.dumps(row, indent=2, sort_keys=True))
        return 0

    try:
        report = build_report()
    except (KeyError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    text = json.dumps(report, indent=2, sort_keys=True)
    print(text)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(text + "\n")
    print(f"wrote metering report ({len(report['per_task'])} tasks) to {args.out}",
          file=sys.stderr)
    return 0


# ============================== SELF-TEST ====================================
def _selftest() -> int:
    """Mechanical self-test. Writes nothing into the repo (existence-delta checked).

    Proves the honest integrity model: output hashes always match canonical, times are
    positive, energy is EXACTLY the documented derivation, labels are present — and a
    second metering run produces DIFFERENT report bytes (we do not fake determinism)
    while reproducing IDENTICAL task output_hashes.
    """
    print("=== demo/task_metering.py self-test (mechanical; writes nothing) ===")
    print("Wall/CPU MEASURED; energy ESTIMATED (assumed power, no hardware telemetry).")
    print("The report is honestly NON-reproducible byte-wise; output hashes are stable.\n")

    demo_dir = os.path.dirname(os.path.abspath(__file__))
    root_before = set(os.listdir(_REPO_ROOT))
    demo_before = set(os.listdir(demo_dir))

    checks = []  # (name, passed)

    r1 = build_report()
    rows = r1["per_task"]

    # [1] shape: schema/power fields, all 13 tasks, sorted, unique
    checks.append(("report schema + assumed-power fields present",
                   r1["schema"] == SCHEMA_VERSION
                   and r1["assumed_cpu_power_w"] == ASSUMED_CPU_POWER_W
                   and r1["power_method"] == POWER_METHOD))
    ids = [r["task_id"] for r in rows]
    checks.append((f"all {len(TASK_MODULES)} tasks metered, sorted, unique",
                   ids == sorted(TASK_MODULES)))

    # [2] every metered output_hash matches a fresh canonical (unmetered) recompute
    all_canonical = all(
        r["output_hash"] == (lambda m: m.output_hash(m.compute()))(load_task(r["task_id"]))
        for r in rows
    )
    checks.append(("every output_hash matches the canonical recompute", all_canonical))

    # [3] all measured times are positive
    checks.append(("all wall/cpu times > 0",
                   all(r["wall_time_s"] > 0 and r["cpu_time_s"] > 0 for r in rows)))

    # [4] energy is EXACTLY the documented derivation from the recorded cpu time
    checks.append(("energy == round(cpu x assumed power, 6) exactly, every row",
                   all(r["energy_j_estimate"] ==
                       round(r["cpu_time_s"] * ASSUMED_CPU_POWER_W, ROUND_DECIMALS)
                       for r in rows)))

    # [5] honesty labels present on every row: wall/cpu measured, energy estimated
    checks.append(("labels {wall:measured, cpu:measured, energy:estimated} on every row",
                   all(r.get("labels") == LABELS for r in rows)))

    # [6] report_hash recomputes from content (anti-circularity)
    checks.append(("report_hash recomputes from content",
                   compute_report_hash(r1) == r1["report_hash"]))

    # [7] HONEST NON-DETERMINISM: a second run must produce DIFFERENT report bytes
    # (identical bytes would mean we faked timing determinism) while reproducing
    # IDENTICAL output_hashes. Timing granularity could in principle coincide, so
    # allow a couple of attempts before declaring failure.
    r2 = build_report()
    attempts = 1
    while r2["report_hash"] == r1["report_hash"] and attempts < 3:
        r2 = build_report()
        attempts += 1
    checks.append(("second run: DIFFERENT report bytes (no fake determinism)",
                   r2["report_hash"] != r1["report_hash"]))
    checks.append(("second run: IDENTICAL output_hashes (outputs untouched)",
                   [r["output_hash"] for r in r2["per_task"]] ==
                   [r["output_hash"] for r in rows]))

    # [8] unknown task id is rejected
    try:
        meter_task("task-9999")
        checks.append(("unknown task id is rejected", False))
    except KeyError:
        checks.append(("unknown task id is rejected", True))

    # [9] no stray files written into the repo
    stray_root = sorted(set(os.listdir(_REPO_ROOT)) - root_before)
    stray_demo = sorted(set(os.listdir(demo_dir)) - demo_before)
    checks.append(("no stray files in repo root", not stray_root))
    checks.append(("no stray files in demo/", not stray_demo))

    print("--- measured rows (times MEASURED; energy ESTIMATED, assumed "
          f"{ASSUMED_CPU_POWER_W} W) ---")
    for r in rows:
        print(f"  {r['task_id']}: wall {r['wall_time_s']:.6f}s  cpu "
              f"{r['cpu_time_s']:.6f}s  energy~{r['energy_j_estimate']:.6f}J  "
              f"hash {r['output_hash'][:12]}..")
    print()

    print("--- self-test invariants ---")
    failures = 0
    for name, passed in checks:
        print(f"{name:60s}: {'PASS' if passed else 'FAIL'}")
        if not passed:
            failures += 1
    if stray_root:
        print(f"    stray in repo root: {stray_root}")
    if stray_demo:
        print(f"    stray in demo/: {stray_demo}")

    ok = failures == 0
    print("\n=== self-test summary: " +
          ("ALL CHECKS BEHAVED CORRECTLY" if ok else "FAILURE — see above") + " ===")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
