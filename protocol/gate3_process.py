"""gate3_process.py — Gate-3 BOUNDED OPTIMISTIC USEFULNESS PROCESS v0 (schema "gate3-process/0.1").

================== THE PROCESS (READ ME) ==================
Gate 3 is the judged gate of the three-gate stack — and judgment is exactly what
this research stage honestly does NOT have. What CAN exist today, mechanically:

  * a MACHINE PRE-CHECK: a mechanical checklist over a submitted work item,
    every check a boolean WITH a ledger citation — taxonomy tag allowlisted,
    Gate-1/2 standing (anchored verification events + the canonical hash
    re-derives right now), provenance standing (the work_id appears in an
    anchored molecule catalog), and challenge-response standing where any
    exists. NO judgment fields exist — booleans with citations only, no LLM
    anywhere.
  * a BOUNDED OPTIMISTIC LIFECYCLE: provisional -> (challenge?) ->
    adjudication -> clawback | finalization. The challenge window is measured
    in LEDGER ENTRIES, not wall time (WINDOW_ENTRIES = 2): simulated windows,
    entry-count-based for determinism — a challenge is in-window iff it anchors
    within the next WINDOW_ENTRIES entries after the provisional grant, and the
    window is closed only once those entries exist challenge-free. Window
    arithmetic is never fudged: if the entries do not exist, the window is open.
  * ADJUDICATION v0 is a SCRIPTED RULE, not judgment: the council seat is
    "same-operator-single-seat", and every filed challenge is upheld by rule
    (maximally conservative — a challenged bounty is always clawed back). The
    record says the seat exercised no judgment; it executed the fixed rule.

HONESTY RULES (also on every anchored record): the PROCESS is real; substantive
usefulness judgment is honestly absent — process-passed is NOT useful. The
bounded-failure property comes from the treasury (demo/metastar_treasury.py):
a successful challenge can at worst claw back ONE capped bounty; the monetary
base is untouchable from this flow by construction.

Research-stage, ZERO-VALUE, no token. Standard library only. Deterministic:
prechecks and window arithmetic are pure functions of the ledger. Not legal,
financial, or investment advice. No NASA affiliation or endorsement.

Usage:
    python3 protocol/gate3_process.py --precheck --task task-0002 --work-id <wmid> --tag TX17
    python3 protocol/gate3_process.py --selftest   # temp-only; writes nothing
"""

# Suppress __pycache__/*.pyc so importing protocol modules below leaves no stray files.
import sys
sys.dont_write_bytecode = True

import argparse
import json
import os

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# REUSE existing components — do NOT reimplement them.
from protocol.verifier_cli import load_task, normalize_task_id
import protocol.work_molecule as work_molecule

_PROTO_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_LEDGER_PATH = os.path.join(_PROTO_DIR, "ledger_data.jsonl")

SCHEMA_VERSION = "gate3-process/0.1"
WINDOW_ENTRIES = 2  # simulated challenge window, measured in ledger ENTRIES
COUNCIL_SEAT = "same-operator-single-seat"

# NASA Technology Taxonomy tags as documented in the task docstrings (task-0001
# carries no tag in its docstring — honestly absent, not invented here).
TASK_TAXONOMY = {
    "task-0002": "TX17", "task-0003": "TX03", "task-0004": "TX05",
    "task-0005": "TX04", "task-0006": "TX17", "task-0007": "TX01",
    "task-0008": "TX04", "task-0009": "TX03", "task-0010": "TX14",
    "task-0011": "TX09", "task-0012": "TX05", "task-0013": "TX17",
}
TAXONOMY_ALLOWLIST = tuple(sorted(set(TASK_TAXONOMY.values())))

# The lifecycle event names the anchoring paths use. ROUTING IS DELIBERATE:
# every phase record carries a SINGULAR top-level task_id, so it JOINS that
# task's molecule history — the challenge phase name contains 'challenge', so
# the molecule assembler files it under challenge_events; the other phases land
# in verification_events as process events. Frozen anchored generations stay
# valid via generation-locked rebuilds (cadence policy in work_molecule.py).
EVENT_PROVISIONAL = "gate3_provisional_grant"
EVENT_CHALLENGE = "gate3_challenge_filed"
EVENT_CLAWBACK = "gate3_adjudication_clawback"
EVENT_FINALIZATION = "gate3_finalization"
LIFECYCLE_EVENTS = (EVENT_PROVISIONAL, EVENT_CHALLENGE, EVENT_CLAWBACK,
                    EVENT_FINALIZATION)


def canonical_json(obj) -> str:
    """Canonical JSON: sorted keys, compact separators, ASCII — byte-stable."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


# ----------------------------------------------------------------------------
# THE MACHINE PRE-CHECK (booleans with citations; no judgment fields exist)
# ----------------------------------------------------------------------------
def submit_work_item(work_ref: dict, ledger_path: str = DEFAULT_LEDGER_PATH) -> dict:
    """Run the mechanical pre-check for work_ref = {task_id, work_id,
    taxonomy_tag}. Returns the precheck record: {schema, work_ref, passed,
    checks:[{name, passed, evidence}]} — deterministic, citation-backed, and
    deliberately WITHOUT any judgment field."""
    task_id = normalize_task_id(work_ref["task_id"])
    work_id = work_ref.get("work_id")
    tag = work_ref.get("taxonomy_tag")
    entries = work_molecule._read_ledger(ledger_path)
    checks = []

    # (1) taxonomy tag allowlisted, and matching the task's documented tag
    documented = TASK_TAXONOMY.get(task_id)
    tag_ok = tag in TAXONOMY_ALLOWLIST and (documented is None or tag == documented)
    checks.append({
        "name": "taxonomy-tag-allowlisted",
        "passed": bool(tag_ok),
        "evidence": (f"module:TASK_TAXONOMY[{task_id}]={documented}"
                     if documented else "module:TAXONOMY_ALLOWLIST (task "
                     "docstring carries no tag)"),
    })

    # (2) Gate-1/2 standing: anchored verification events + the canonical hash
    # re-derives RIGHT NOW on this machine
    recorded_idx = None
    recorded_hash = None
    for e in entries:
        p = e.get("payload", {})
        if p.get("task_id") == task_id:
            for key in ("local_output_hash", "output_hash",
                        "submitted_output_hash"):
                if isinstance(p.get(key), str):
                    recorded_idx, recorded_hash = e["index"], p[key]
                    break
        if recorded_idx is not None:
            break
    rederives = False
    if recorded_hash is not None:
        module = load_task(task_id)
        rederives = module.output_hash(module.compute()) == recorded_hash
    checks.append({
        "name": "gate12-standing",
        "passed": bool(recorded_idx is not None and rederives),
        "evidence": (f"ledger:{recorded_idx}" if recorded_idx is not None
                     else "none: no anchored verification event for this task"),
    })

    # (3) provenance standing: the work_id appears in an anchored molecule catalog
    provenance_idx = None
    for e in entries:
        p = e.get("payload", {})
        if (p.get("event") == "work_molecule_catalog_anchored"
                and p.get("status") == "molecule-catalog-confirmed"
                and any(isinstance(c, dict) and c.get("task_id") == task_id
                        and c.get("work_id") == work_id
                        for c in p.get("catalog_entries", []))):
            provenance_idx = e["index"]
    checks.append({
        "name": "provenance-standing",
        "passed": provenance_idx is not None,
        "evidence": (f"ledger:{provenance_idx}" if provenance_idx is not None
                     else "none: work_id not found in any anchored catalog"),
    })

    # (4) challenge-response standing WHERE IT EXISTS: vacuous when no
    # challenge records name this task; else at least one verified round and
    # every failed round drill-labeled
    ch_records = [(e["index"], e["payload"]) for e in entries
                  if e.get("payload", {}).get("event") ==
                  "challenge_response_result"
                  and e["payload"].get("task_id") == task_id]
    if not ch_records:
        checks.append({"name": "challenge-standing", "passed": True,
                       "evidence": "none-filed (vacuous: no challenge rounds "
                                   "exist for this task)"})
    else:
        verified = [i for i, p in ch_records
                    if p.get("status") == "challenge-verified"]
        bad_failed = [i for i, p in ch_records
                      if p.get("status") == "challenge-failed"
                      and not p.get("drill")]
        checks.append({
            "name": "challenge-standing",
            "passed": bool(verified) and not bad_failed,
            "evidence": (f"ledger:{verified[-1]}" if verified
                         else "none: no verified challenge round"),
        })

    return {
        "schema": SCHEMA_VERSION,
        "work_ref": {"task_id": task_id, "work_id": work_id,
                     "taxonomy_tag": tag},
        "passed": all(c["passed"] for c in checks),
        "checks": checks,
    }


# ----------------------------------------------------------------------------
# Window arithmetic + scripted adjudication (deterministic; never fudged)
# ----------------------------------------------------------------------------
def challenge_in_window(provisional_index: int, challenge_index: int) -> bool:
    """A challenge is in-window iff it anchors within the WINDOW_ENTRIES
    entries immediately after the provisional grant."""
    return provisional_index < challenge_index <= provisional_index + WINDOW_ENTRIES


def window_closed(provisional_index: int, entries: list, bounty_id: str):
    """Is the challenge window for `bounty_id`'s provisional grant CLOSED?

    Closed iff all WINDOW_ENTRIES entries after the provisional exist AND none
    of them is a filed challenge against this bounty. Returns (closed, note) —
    the note states the honest arithmetic either way.
    """
    window = [e for e in entries
              if isinstance(e.get("index"), int)
              and challenge_in_window(provisional_index, e["index"])]
    # a filed challenge blocks closure IMMEDIATELY — checked before the count,
    # since a challenged bounty can never reach unchallenged finalization
    for e in window:
        p = e.get("payload", {})
        if (p.get("event") == EVENT_CHALLENGE
                and p.get("bounty_id") == bounty_id):
            return (False, f"window CLOSED BY CHALLENGE at ledger index "
                           f"{e['index']}")
    if len(window) < WINDOW_ENTRIES:
        return (False, f"window OPEN: only {len(window)} of {WINDOW_ENTRIES} "
                       f"post-grant entries exist (entries "
                       f"{provisional_index + 1}..{provisional_index + WINDOW_ENTRIES})")
    return (True, f"window closed clean: entries {provisional_index + 1}.."
                  f"{provisional_index + WINDOW_ENTRIES} exist and contain no "
                  f"challenge against {bounty_id}")


def adjudicate(challenge_payload: dict) -> dict:
    """v0 adjudication: a FIXED SCRIPTED RULE, not judgment. Every filed
    challenge is upheld (maximally conservative: a challenged bounty is always
    clawed back). The single council seat exercised NO discretion — none exists
    yet — and the returned record says so."""
    return {
        "council_seat": COUNCIL_SEAT,
        "verdict": "upheld",
        "rule": "v0 fixed rule: every filed challenge is upheld (no discretion "
                "exists; the seat executed a script, not judgment)",
        "drill": bool(challenge_payload.get("drill")),
    }


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="gate3_process.py",
        description=(
            "Gate-3 bounded optimistic usefulness process v0 (research-stage, "
            "ZERO-VALUE, no token): mechanical pre-check with ledger citations; "
            "entry-count challenge windows; scripted single-seat adjudication."
        ),
        epilog=(
            "HONESTY: the PROCESS is real; substantive usefulness judgment is "
            "absent — process-passed is NOT useful. No LLM anywhere. Not "
            "consensus, not payment, not a token."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--precheck", action="store_true",
                      help="run the machine pre-check (--task --work-id --tag)")
    mode.add_argument("--selftest", action="store_true",
                      help="run the mechanical self-test (temp files only)")
    parser.add_argument("--task", help="task id of the work item")
    parser.add_argument("--work-id", help="anchored WMID of the work item")
    parser.add_argument("--tag", help="NASA taxonomy tag, e.g. TX17")
    parser.add_argument("--ledger", default=DEFAULT_LEDGER_PATH,
                        help=f"ledger source (default: {DEFAULT_LEDGER_PATH})")
    args = parser.parse_args(argv)

    if args.selftest or not args.precheck:
        return _selftest()

    if not (args.task and args.work_id and args.tag):
        parser.error("--precheck requires --task --work-id --tag")
    record = submit_work_item(
        {"task_id": args.task, "work_id": args.work_id,
         "taxonomy_tag": args.tag}, ledger_path=args.ledger)
    print(json.dumps(record, indent=2, sort_keys=True))
    return 0 if record["passed"] else 1


# ============================== SELF-TEST ====================================
def _selftest() -> int:
    """Mechanical self-test: precheck pass/fail fixtures, window arithmetic,
    the scripted drill lifecycle rule, determinism. Temp files only."""
    import shutil
    import tempfile

    from protocol.ledger import Ledger
    import protocol.external_verifier as external_verifier
    import protocol.verifier_cli as verifier_cli

    print("=== protocol/gate3_process.py self-test (bounded optimistic process) ===")
    print("Mechanical pre-check with citations; the judgment seat is honestly "
          "vacant.\n")

    root_before = set(os.listdir(_REPO_ROOT))
    proto_before = set(os.listdir(_PROTO_DIR))
    checks = []
    tmp_dir = tempfile.mkdtemp(prefix=f"gate3_selftest_{os.getpid()}_")
    try:
        # fixture ledger: genesis + evaluations for two tasks + an anchored
        # 0.3 molecule catalog (the provenance-standing source)
        fixture_ledger = os.path.join(tmp_dir, "ledger_fixture.jsonl")
        led = Ledger(fixture_ledger)
        led.append({"event": "ledger_genesis", "note": "selftest fixture",
                    "stage": "R-selftest", "zero_value": True, "no_token": True})
        for tid in ("task-0002", "task-0008"):
            external_verifier.evaluate_submission(
                verifier_cli.build_submission(
                    tid, "selftest-same-operator (simulated)",
                    topology="same-machine-self-recompute"),
                led,
            )
        cat = work_molecule.build_catalog(ledger_path=fixture_ledger,
                                          task_ids=["task-0002", "task-0008"])
        external_verifier.anchor_molecule_catalog(cat, led)
        wid_0002 = cat["entries"][0]["work_id"]

        # [1] full precheck pass, every check citing evidence
        good = {"task_id": "task-0002", "work_id": wid_0002,
                "taxonomy_tag": "TX17"}
        r = submit_work_item(good, ledger_path=fixture_ledger)
        checks.append(("precheck passes with per-check ledger citations",
                       r["passed"]
                       and [c["name"] for c in r["checks"]] ==
                       ["taxonomy-tag-allowlisted", "gate12-standing",
                        "provenance-standing", "challenge-standing"]
                       and r["checks"][1]["evidence"].startswith("ledger:")
                       and r["checks"][2]["evidence"].startswith("ledger:")))
        checks.append(("precheck carries NO judgment fields (booleans + "
                       "citations only)",
                       set(r.keys()) == {"schema", "work_ref", "passed",
                                         "checks"}
                       and all(set(c.keys()) == {"name", "passed", "evidence"}
                               for c in r["checks"])))

        # [2] fail fixtures: bad taxonomy / missing molecule / unverifiable task
        r_tag = submit_work_item(dict(good, taxonomy_tag="TX99"),
                                 ledger_path=fixture_ledger)
        r_wid = submit_work_item(dict(good, work_id="0" * 64),
                                 ledger_path=fixture_ledger)
        r_task = submit_work_item({"task_id": "task-0003", "work_id": "0" * 64,
                                   "taxonomy_tag": "TX03"},
                                  ledger_path=fixture_ledger)
        checks.append(("bad taxonomy tag fails check 1 only",
                       not r_tag["passed"]
                       and not r_tag["checks"][0]["passed"]
                       and r_tag["checks"][1]["passed"]))
        checks.append(("unanchored work_id fails provenance-standing",
                       not r_wid["passed"]
                       and not r_wid["checks"][2]["passed"]))
        checks.append(("task without ledger standing fails gate12-standing",
                       not r_task["passed"]
                       and not r_task["checks"][1]["passed"]))

        # [3] determinism: two prechecks byte-identical
        r2 = submit_work_item(good, ledger_path=fixture_ledger)
        checks.append(("precheck deterministic (byte-identical)",
                       canonical_json(r) == canonical_json(r2)))

        # [4] window arithmetic: in-window bounds + honest open/closed notes
        checks.append(("challenge window bounds exact (G+1..G+2 in, G+3 out)",
                       not challenge_in_window(10, 10)
                       and challenge_in_window(10, 11)
                       and challenge_in_window(10, 12)
                       and not challenge_in_window(10, 13)))
        tip = led.read_all()[-1]["index"]
        closed, note = window_closed(tip, led.read_all(), "b-x")
        checks.append(("window at the tip is honestly OPEN (entries missing)",
                       not closed and "OPEN" in note))
        led.append({"event": "unrelated_a", "zero_value": True, "no_token": True})
        led.append({"event": "unrelated_b", "zero_value": True, "no_token": True})
        closed, note = window_closed(tip, led.read_all(), "b-x")
        checks.append(("window closes clean once 2 challenge-free entries exist",
                       closed and "closed clean" in note))
        led.append({"event": EVENT_CHALLENGE, "bounty_id": "b-y",
                    "zero_value": True, "no_token": True})
        closed, _ = window_closed(led.read_all()[-1]["index"] - 2 + 1,
                                  led.read_all(), "b-y")
        # a challenge inside the window blocks closure for ITS bounty
        prov_idx = led.read_all()[-1]["index"] - 1  # challenge is at prov+1
        closed_by, note_by = window_closed(prov_idx, led.read_all(), "b-y")
        checks.append(("an in-window challenge blocks closure for its bounty",
                       not closed_by and "CLOSED BY CHALLENGE" in note_by))

        # [5] scripted adjudication: fixed rule, no discretion, seat labeled
        verdict = adjudicate({"bounty_id": "b-y", "drill": True})
        checks.append(("adjudication is the fixed scripted rule (upheld; "
                       "single seat; no judgment)",
                       verdict["verdict"] == "upheld"
                       and verdict["council_seat"] == COUNCIL_SEAT
                       and verdict["drill"] is True
                       and "no discretion" in verdict["rule"]))
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    stray_root = sorted(set(os.listdir(_REPO_ROOT)) - root_before)
    stray_proto = sorted(set(os.listdir(_PROTO_DIR)) - proto_before)
    checks.append(("no stray files in repo root", not stray_root))
    checks.append(("no stray files in protocol/", not stray_proto))

    print("--- self-test invariants ---")
    failures = 0
    for name, passed in checks:
        print(f"{name:65s}: {'PASS' if passed else 'FAIL'}")
        if not passed:
            failures += 1

    ok = failures == 0
    print("\n=== self-test summary: " +
          ("ALL CHECKS BEHAVED CORRECTLY" if ok else "FAILURE — see above") + " ===")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
