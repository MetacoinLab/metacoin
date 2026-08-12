# Copyright (c) 2023-2026 MetaCoin-Lab.
# Licensed under SML-1.0 — see LICENSE.md.
"""agent_verifier.py — MetaCoin AUTONOMOUS AGENT-VERIFIER (fully mechanical, no AI oracle).

================== HONESTY / SCOPE (READ ME) ==================
Research-stage, ZERO-VALUE, no token, no money, no mainnet, no networking, no payments.

This is a standalone agent that lets a SEPARATE machine independently verify the MetaCoin
protocol ledger AND re-derive the recorded work, MECHANICALLY — with no human in the loop
and, crucially, NO trusted-AI / LLM oracle. Every bit of trust this script produces comes
from DETERMINISTIC REPRODUCTION (re-running code and comparing SHA-256 hashes) and from a
tip-against-committed-anchor check. It NEVER calls an LLM/API, never asks a model to "judge"
anything, and never trusts a recorded result — it reproduces the result and compares hashes.

What it does, in order, given a locally-cloned repo:

  1. READ the public artifacts (does NOT create them): the published snapshot
     protocol/ledger_published.json and the committed tip anchor protocol/ledger_anchor.json.
  2. VERIFY THE CHAIN by REUSING protocol/audit.py's standalone snapshot verification
     (audit.verify_snapshot_obj) — it re-walks every entry from genesis, recomputes every
     hash from content, and checks linkage + index sequencing + header tip. No reimplementation.
  3. CONFIRM TIP-AGAINST-ANCHOR: the snapshot's recomputed tip_hash/tip_index must exactly
     equal the COMMITTED anchor's tip_hash/tip_index. This is the external-anchor check that
     proves the chain was not silently rewritten (the full-suffix-rewrite gap R1 documented).
  4. INDEPENDENTLY RE-DERIVE THE RECORDED WORK: for each ledger entry that records an external
     verification of a runnable task (e.g. the task-0002 entry with an output_hash), RE-RUN
     that task locally on THIS machine via the SAME path verifier_cli.py uses
     (module.compute() -> module.output_hash()) and confirm the recomputed hash equals the
     hash recorded in the ledger. Entries that are not task-verification events (e.g. the
     genesis marker) are skipped gracefully.
  5. PRODUCE a result file (agent_verifier_result.json) — it does NOT submit anything to any
     ledger. The file carries honest labels and a verdict that is "verified" only if ALL
     mechanical checks passed, else "failed-with-reasons".

PRECISE, HONEST CLAIM (the important limitation, same as the rest of the protocol):
  A matching output_hash proves the result is REPRODUCIBLE — independently re-derived to the
  same canonical value. It does NOT, by itself, cryptographically prove WHO executed the task
  (a hash is a short string and can be copied). Independence is only strengthened when the
  verifier is operated by a SEPARATE person/organisation; running under the SAME operator as
  the publisher adds no cross-party independence. Verifier-held signing keys now exist
  (protocol/actor_identity.py: anchored Lamport one-time roots with rotation), but under
  same-operator custody they prove KEY-POSSESSION CONTINUITY, not cross-party identity;
  hardware attestation remains future work — same interface either way.

Standard library only (json, hashlib, os, subprocess, sys, argparse; plus time for the
timestamp). Chain hashing/verification is REUSED from protocol/audit.py + protocol/ledger.py,
and task execution + the coarse non-PII machine fingerprint are REUSED from
protocol/verifier_cli.py — nothing is reimplemented here. Not legal, financial, investment,
or security-certification advice. No NASA affiliation or endorsement.

Usage:
    python3 protocol/agent_verifier.py --verifier-id jiyu-agent-same-operator
    python3 protocol/agent_verifier.py --verifier-id alice-agent --out agent_verifier_result.json
    python3 protocol/agent_verifier.py --selftest      # mechanical self-test, writes no stray files
"""

# Suppress __pycache__/*.pyc so importing the protocol/demo modules below leaves no stray
# files (this verifier promises NOT to create anything beyond its intended --out result).
# Set immediately after the docstring, BEFORE the heavy protocol imports, so it takes effect.
import sys
sys.dont_write_bytecode = True

import argparse
import json
import os
import tempfile
import time

# Make `from protocol...` / `import demo...` resolve when run directly (repo root on path).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# REUSE the existing protocol verification + task-execution code — do NOT reimplement it.
#   * audit.verify_snapshot_obj  -> standalone chain re-walk (recompute every hash, check tip)
#   * verifier_cli.*             -> task load/run, coarse machine fingerprint, env, repo commit
from protocol.audit import verify_snapshot_obj
from protocol.verifier_cli import (
    normalize_task_id,
    load_task,
    machine_fingerprint,
    environment_summary,
    get_repo_commit,
)

_PROTO_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PUBLISHED_PATH = os.path.join(_PROTO_DIR, "ledger_published.json")
DEFAULT_ANCHOR_PATH = os.path.join(_PROTO_DIR, "ledger_anchor.json")
DEFAULT_OUT_PATH = "agent_verifier_result.json"

# Recorded-output-hash fields a task-verification entry may carry, in priority order. Both
# local_output_hash (coordinator's locally recomputed value) and submitted_output_hash (what
# an external verifier submitted) are recorded; for a genuinely verified entry they are equal.
_RECORDED_HASH_FIELDS = ("output_hash", "local_output_hash", "submitted_output_hash")

HONEST_NOTE = (
    "Autonomous agent-verifier attestation (research-stage, zero-value, no token). This is a "
    "FULLY MECHANICAL verification: a standalone re-walk of the published chain (every hash "
    "recomputed from content), a tip-against-committed-anchor check, and a DETERMINISTIC "
    "re-run of each recorded task on this machine with SHA-256 hash comparison. NO LLM/AI "
    "judgment or oracle is involved at any step. A matching output_hash proves REPRODUCIBILITY "
    "(the result was independently re-derived to the same canonical value); it does NOT prove "
    "WHO executed the task (a hash can be copied). If this verifier runs under the SAME "
    "operator as the publisher, that adds NO cross-party independence; genuine cross-party / "
    "execution proof needs verifier-held signing keys and/or hardware attestation (future "
    "work). Not consensus, not mainnet, not payment, not a token."
)


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------
def _load_json(path):
    """Load a JSON file. Returns (obj, None) on success or (None, reason) on failure."""
    if not os.path.exists(path):
        return (None, f"file does not exist: {path}")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return (json.load(f), None)
    except json.JSONDecodeError as exc:
        return (None, f"not valid JSON ({exc}): {path}")
    except OSError as exc:
        return (None, f"could not read ({exc}): {path}")


def _extract_task_verification(payload):
    """If `payload` records an external verification of a RUNNABLE task, return
    (short_task_id, recorded_hash, recorded_hash_field); otherwise return None.

    Non-task entries (e.g. the genesis marker, which has no task_id) yield None and are
    skipped gracefully. An entry only qualifies if its task_id maps to a known task module
    AND it carries a recorded output hash — so this re-runs exactly what was recorded.
    """
    if not isinstance(payload, dict):
        return None
    task_id = payload.get("task_id")
    if not task_id:
        return None
    try:
        short = normalize_task_id(task_id)
    except KeyError:
        return None  # references a task we cannot run -> not reproducible here, skip
    for field in _RECORDED_HASH_FIELDS:
        recorded = payload.get(field)
        if isinstance(recorded, str) and recorded:
            return (short, recorded, field)
    return None


def _reproduce_task(short_task_id, recorded_hash, recorded_hash_field, entry_index):
    """Re-run the task locally (same path verifier_cli uses) and compare hashes.

    Imports the task module, calls compute() then output_hash() — i.e. it RE-DERIVES the
    result from scratch on this machine instead of trusting the recorded value.
    """
    record = {
        "task_id": short_task_id,
        "ledger_entry_index": entry_index,
        "recorded_hash": recorded_hash,
        "recorded_hash_field": recorded_hash_field,
        "recomputed_hash": None,
        "match": False,
    }
    try:
        module = load_task(short_task_id)
        result = module.compute()
        recomputed = module.output_hash(result)
    except Exception as exc:  # re-run failed -> not reproduced (honest non-match)
        record["error"] = f"{type(exc).__name__}: {exc}"
        return record
    record["recomputed_hash"] = recomputed
    record["match"] = (recomputed == recorded_hash)
    return record


# ----------------------------------------------------------------------------
# Core verification (writes NO files — caller decides whether/where to persist).
# ----------------------------------------------------------------------------
def run_verification(verifier_id, published_path=DEFAULT_PUBLISHED_PATH,
                     anchor_path=DEFAULT_ANCHOR_PATH):
    """Perform the full mechanical verification and return the result dict.

    The returned dict is the agent_verifier_attestation record. It is built purely from
    deterministic checks (hash recomputation + re-run + anchor comparison); no LLM/AI is
    consulted. The overall verdict is "verified" iff every check passed.
    """
    reasons = []  # human-readable reasons the verdict is NOT "verified"

    chain_verified = False
    chain_reason = None
    tip_matches_anchor = False
    snapshot_tip_hash = None
    snapshot_tip_index = None
    anchor_tip_hash = None
    anchor_tip_index = None
    task_reproductions = []

    # --- 1) read the public artifacts (do NOT create them) ----------------------------
    snapshot, snap_err = _load_json(published_path)
    anchor, anchor_err = _load_json(anchor_path)
    if snap_err:
        reasons.append(f"artifacts not published: published snapshot unavailable ({snap_err})")
    if anchor_err:
        reasons.append(f"artifacts not published: committed anchor unavailable ({anchor_err})")

    # --- 2) verify the chain by REUSING audit.py's standalone verifier -----------------
    if snapshot is not None:
        ok, reason, details = verify_snapshot_obj(snapshot)
        chain_verified = bool(ok)
        chain_reason = reason
        if ok:
            snapshot_tip_hash = details.get("tip_hash")
            snapshot_tip_index = details.get("tip_index")
        else:
            reasons.append(f"chain verification failed: {reason}")

    # --- 3) confirm tip-against-anchor (the external-anchor check) ---------------------
    if isinstance(anchor, dict):
        anchor_tip_hash = anchor.get("tip_hash")
        anchor_tip_index = anchor.get("tip_index")
    if chain_verified and isinstance(anchor, dict):
        tip_matches_anchor = (
            snapshot_tip_hash == anchor_tip_hash
            and snapshot_tip_index == anchor_tip_index
        )
        if not tip_matches_anchor:
            reasons.append(
                "snapshot tip does not match committed anchor "
                f"(snapshot tip_index={snapshot_tip_index} tip_hash={snapshot_tip_hash}; "
                f"anchor tip_index={anchor_tip_index} tip_hash={anchor_tip_hash}) "
                "— possible silent chain rewrite"
            )

    # --- 4) independently re-derive the recorded work ---------------------------------
    # Iterate the published entries; re-run every entry that records a runnable task.
    entries = snapshot.get("entries") if isinstance(snapshot, dict) else None
    if isinstance(entries, list):
        for entry in entries:
            payload = entry.get("payload") if isinstance(entry, dict) else None
            extracted = _extract_task_verification(payload)
            if extracted is None:
                continue  # genesis / non-task event -> skip gracefully
            short, recorded_hash, recorded_field = extracted
            record = _reproduce_task(
                short, recorded_hash, recorded_field,
                entry.get("index") if isinstance(entry, dict) else None,
            )
            task_reproductions.append(record)
            if not record["match"]:
                reasons.append(
                    f"task {record['task_id']} did NOT reproduce "
                    f"(recorded={record['recorded_hash']} recomputed={record['recomputed_hash']})"
                )

    if chain_verified and not task_reproductions:
        reasons.append("no task-verification entries found to reproduce (nothing to re-derive)")

    verdict = "verified" if not reasons else "failed-with-reasons"

    result = {
        "event": "agent_verifier_attestation",
        "verifier_kind": "autonomous-agent",
        "verifier_id": verifier_id,
        "machine_fingerprint": machine_fingerprint(),
        "timestamp": time.time(),
        "snapshot_tip_hash": snapshot_tip_hash,
        "snapshot_tip_index": snapshot_tip_index,
        "anchor_tip_hash": anchor_tip_hash,
        "anchor_tip_index": anchor_tip_index,
        "chain_verified": chain_verified,
        "chain_verify_reason": chain_reason,
        "tip_matches_anchor": tip_matches_anchor,
        "task_reproductions": task_reproductions,
        "verdict": verdict,
        "reasons": reasons,
        # honest labels
        "research_stage": True,
        "stage": "R-protocol",
        "zero_value": True,
        "no_token": True,
        "repo_commit": get_repo_commit(),
        "environment_summary": environment_summary(),
        "honest_note": HONEST_NOTE,
    }
    return result


# ----------------------------------------------------------------------------
# Human-readable summary
# ----------------------------------------------------------------------------
def print_summary(result, published_path, anchor_path):
    print("=== MetaCoin autonomous agent-verifier (mechanical / deterministic) ===")
    print("research-stage, zero-value, no token. NO LLM/AI judgment — hashes and re-runs only.\n")
    print(f"published snapshot : {published_path}")
    print(f"committed anchor   : {anchor_path}")
    print(f"verifier_id        : {result['verifier_id']}  (kind: {result['verifier_kind']})\n")

    # [1] chain
    cv = result["chain_verified"]
    print("[1] chain verification (REUSING audit.py standalone verify):")
    print(f"    {'VERIFIED' if cv else 'FAILED'} — {result.get('chain_verify_reason')}")
    if result.get("snapshot_tip_hash"):
        print(f"        tip_index={result['snapshot_tip_index']} "
              f"tip_hash={result['snapshot_tip_hash']}")

    # [2] tip vs anchor
    tm = result["tip_matches_anchor"]
    print("[2] tip-against-anchor (external-anchor check — chain not silently rewritten):")
    print(f"    snapshot tip == committed anchor tip : {'YES' if tm else 'NO'}")
    if result.get("anchor_tip_hash"):
        print(f"        anchor tip_index={result['anchor_tip_index']} "
              f"tip_hash={result['anchor_tip_hash']}")

    # [3] reproduction
    reps = result["task_reproductions"]
    print("[3] independent re-derivation of recorded work (re-run on THIS machine):")
    if not reps:
        print("    (no task-verification entries found to reproduce)")
    for r in reps:
        status = "MATCH" if r["match"] else "MISMATCH"
        if r.get("error"):
            print(f"    {r['task_id']} (entry {r['ledger_entry_index']}): RE-RUN ERROR — {r['error']}")
        else:
            print(f"    {r['task_id']} (entry {r['ledger_entry_index']}): "
                  f"recomputed {r['recomputed_hash']}")
            print(f"        {'==' if r['match'] else '!='} recorded {r['recorded_hash']}  -> {status}")
    matches = sum(1 for r in reps if r["match"])
    print(f"    ({matches}/{len(reps)} tasks reproduced)")

    # verdict
    print()
    print(f"overall verdict: {result['verdict'].upper()}")
    if result["reasons"]:
        print("reasons:")
        for reason in result["reasons"]:
            print(f"  - {reason}")


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="agent_verifier.py",
        description=(
            "MetaCoin autonomous agent-verifier (research-stage, ZERO-VALUE, no token). "
            "Independently verify the published ledger snapshot, confirm its tip against the "
            "committed anchor, and RE-RUN the recorded task(s) locally to confirm the recorded "
            "hashes reproduce. FULLY MECHANICAL: hashes + re-runs only, NO LLM/AI oracle."
        ),
        epilog=(
            "A matching output_hash proves REPRODUCIBILITY, not WHO executed the task (a hash "
            "can be copied). Same-operator runs add no cross-party independence. Not consensus, "
            "not mainnet, not payment, not a token. Exit: 0 = verdict 'verified'; non-zero "
            "otherwise."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--verifier-id",
                        help="name/handle of this autonomous verifier (ideally a separate person/org)")
    parser.add_argument("--out", default=DEFAULT_OUT_PATH,
                        help=f"write the agent-verifier result JSON here (default: {DEFAULT_OUT_PATH})")
    parser.add_argument("--published", default=DEFAULT_PUBLISHED_PATH,
                        help=f"published snapshot to verify (default: {DEFAULT_PUBLISHED_PATH})")
    parser.add_argument("--anchor", default=DEFAULT_ANCHOR_PATH,
                        help=f"committed tip anchor to check against (default: {DEFAULT_ANCHOR_PATH})")
    parser.add_argument("--selftest", action="store_true",
                        help="run the mechanical self-test against the real published artifacts "
                             "without creating any stray file (writes its result to a temp path, "
                             "then removes it) and does not modify the repo")
    args = parser.parse_args(argv)

    if args.selftest:
        return _selftest(published_path=args.published, anchor_path=args.anchor)

    if not args.verifier_id:
        parser.error("--verifier-id is required (unless --selftest)")

    result = run_verification(args.verifier_id, args.published, args.anchor)
    print_summary(result, args.published, args.anchor)

    text = json.dumps(result, indent=2, sort_keys=True)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(text + "\n")
    print(f"\nwrote agent-verifier result to {args.out}")

    return 0 if result["verdict"] == "verified" else 1


# ============================== SELF-TEST ====================================
def _selftest(published_path=DEFAULT_PUBLISHED_PATH, anchor_path=DEFAULT_ANCHOR_PATH):
    """Mechanical self-test: run the REAL verification against the published artifacts, but
    write the result only to a TEMP file (then remove it), and confirm no stray files were
    created and the repo was not modified. Because this verifier operates on the real
    published snapshot + anchor, the self-test IS essentially the real run.
    """
    print("=== protocol/agent_verifier.py self-test (mechanical; no stray files) ===")
    print("Runs the real verification against the published artifacts; writes its result only")
    print("to a temp path which it then deletes. The repo is read-only here.\n")

    # Snapshot protocol/ dir listing before, to prove we create no stray files there.
    proto_before = set(os.listdir(_PROTO_DIR))
    default_out_existed_before = os.path.exists(DEFAULT_OUT_PATH)

    result = run_verification("selftest-agent", published_path, anchor_path)
    print_summary(result, published_path, anchor_path)

    # Write to a temp out path, confirm it round-trips, then remove it (no stray artifact).
    # Cross-platform temp dir via tempfile (works on Windows/macOS/Linux).
    fd, tmp_out = tempfile.mkstemp(prefix=f"agent_verifier_selftest_{os.getpid()}_", suffix=".json")
    os.close(fd)
    round_trip_ok = False
    try:
        with open(tmp_out, "w", encoding="utf-8") as f:
            f.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
        with open(tmp_out, "r", encoding="utf-8") as f:
            reloaded = json.load(f)
        round_trip_ok = (reloaded.get("verdict") == result["verdict"])
    finally:
        if os.path.exists(tmp_out):
            os.remove(tmp_out)

    # TAMPER-DETECTION: a snapshot with one mutated payload must yield
    # 'failed-with-reasons' (the chain re-walk catches the hash break) — the
    # verifier is only trustworthy if it visibly refuses corrupted input.
    tamper_ok = False
    tamper_dir = tempfile.mkdtemp(prefix=f"agent_verifier_tamper_{os.getpid()}_")
    try:
        with open(published_path, "r", encoding="utf-8") as f:
            snap = json.load(f)
        snap["entries"][1]["payload"]["status"] = "TAMPERED"
        tampered_path = os.path.join(tamper_dir, "tampered_published.json")
        with open(tampered_path, "w", encoding="utf-8") as f:
            json.dump(snap, f)
        t_result = run_verification("selftest-agent", tampered_path, anchor_path)
        tamper_ok = (t_result["verdict"] == "failed-with-reasons"
                     and any("chain" in r or "hash" in r
                             for r in t_result.get("reasons", [])))
    finally:
        for name in os.listdir(tamper_dir):
            os.remove(os.path.join(tamper_dir, name))
        os.rmdir(tamper_dir)

    proto_after = set(os.listdir(_PROTO_DIR))
    stray_in_proto = sorted(proto_after - proto_before)
    stray_default_out = os.path.exists(DEFAULT_OUT_PATH) and not default_out_existed_before

    print()
    print("--- self-test invariants ---")
    print(f"verdict is 'verified'              : {'PASS' if result['verdict'] == 'verified' else 'FAIL'}")
    print(f"result JSON round-trips            : {'PASS' if round_trip_ok else 'FAIL'}")
    print(f"tampered snapshot -> failed-with-reasons: {'PASS' if tamper_ok else 'FAIL'}")
    print(f"no stray files in protocol/        : {'PASS' if not stray_in_proto else f'FAIL ({stray_in_proto})'}")
    print(f"no stray default {DEFAULT_OUT_PATH}: {'PASS' if not stray_default_out else 'FAIL'}")

    ok = (
        result["verdict"] == "verified"
        and round_trip_ok
        and tamper_ok
        and not stray_in_proto
        and not stray_default_out
    )
    print("\n=== self-test summary: " +
          ("ALL CHECKS BEHAVED CORRECTLY" if ok else "FAILURE — see above") + " ===")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
