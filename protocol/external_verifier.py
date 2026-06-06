"""external_verifier.py — MetaCoin R3 COORDINATOR for the external-verifier-pilot.

================== CRITICAL HONESTY NOTICE (READ ME) ==================
This is the coordinator side of the "external-verifier-pilot". It is research-stage and
ZERO-VALUE. It is NOT decentralized consensus, NOT mainnet, NOT payment, NOT a token.

What it does: an external verifier (ideally a separate person/org on a separate machine —
another box, a CI runner, a VPS, a collaborator's laptop) re-runs the same reproducible
task and submits the resulting canonical output_hash (see protocol/verifier_cli.py). The
coordinator recomputes the SAME task's hash LOCALLY and compares:
  * match    -> status "externally-verified"
  * mismatch -> status "external-mismatch"
  * malformed-> status "rejected"
Evaluated outcomes (verified AND mismatch — both are real, audit-worthy events) are anchored
into the R1 tamper-evident hash-chained ledger (protocol/ledger.py). Rejected (malformed)
submissions are validated BEFORE the ledger is touched and are NOT anchored, so malformed
external input never enters the chain.

PRECISE, HONEST CLAIM (the limitation that defines this pilot):
  A matching output_hash proves the result is REPRODUCIBLE — independently re-derived to the
  same canonical value. It does NOT by itself cryptographically prove the external verifier
  EXECUTED the task: a hash is a short string and can be copied. The honest claim a match
  supports is "an external party submitted a reproducibly-matching result", NOT "we proved
  independent execution". Independence improves when the verifier is a distinct person/org.
  Stronger execution-proof would require verifier-held signing keys and/or hardware
  attestation — future work, behind this same interface.

Standard library only (json, hashlib, os, time, subprocess, platform, argparse) plus the
existing protocol components. The single-host simulation appears ONLY inside the self-test
below, clearly labeled "local test harness, not the product".
"""

import argparse
import json
import os
import sys
import time

# Make `from protocol...` resolve when run directly (repo root on sys.path).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from protocol.ledger import Ledger, DEFAULT_LEDGER_PATH
import protocol.verifier_cli as verifier_cli
import protocol.audit as audit  # reused to independently re-derive chain/anchor claims

# The required fields and the fixed (const) fields of a valid submission. Mirrors
# protocol/verifier_submission.schema.json (which is the human-readable contract).
_REQUIRED_FIELDS = (
    "event", "stage", "topology", "task_id", "verifier_id", "machine_fingerprint",
    "timestamp", "output_hash", "repo_commit", "environment_summary", "zero_value", "no_token",
)
_CONST_FIELDS = {
    "event": "external_verifier_submission",
    "stage": "R3",
    "topology": "external-verifier-pilot",
    "zero_value": True,
    "no_token": True,
}
_HEX = set("0123456789abcdef")

# The honest reproducibility-not-execution limitation, embedded in every ledger record.
LIMITATION_NOTE = (
    "A matching output_hash proves REPRODUCIBILITY (the result was independently re-derived "
    "to the same canonical value); it does NOT cryptographically prove the external verifier "
    "executed the task (a hash can be copied). Independence improves when the verifier is a "
    "separate person/org; execution-proof would need verifier-held signing keys and/or "
    "hardware attestation (future work, same interface). external-verifier-pilot — not "
    "consensus, not mainnet, not payment, not a token; zero-value research-stage."
)

# --- agent-verifier-result schema (a DIFFERENT record type than a task submission) -------
# An agent_verifier_attestation asserts three claims (chain + anchor + task reproduction),
# not a single task submission. The coordinator re-derives all three on the Spark before
# anchoring — it never just trusts the result file.
_AGENT_RESULT_EVENT = "agent_verifier_attestation"
# Real schema emitted by jiyu's agent_verifier.py: top-level claims + a task_reproductions
# ARRAY (each item: task_id, recomputed_hash, recorded_hash, match). The Spark re-derives
# each task itself rather than trusting these values.
_AGENT_RESULT_REQUIRED = (
    "event", "verdict", "chain_verified", "tip_matches_anchor",
    "verifier_id", "zero_value", "no_token", "task_reproductions",
)
_AGENT_REP_REQUIRED = ("task_id", "recomputed_hash", "recorded_hash", "match")

# Canonical committed published-snapshot path (the artifact the agent verified).
_DEFAULT_PUBLISHED_SNAPSHOT_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "ledger_published.json"
)

AGENT_LIMITATION_NOTE = (
    "mechanical re-derivation by the same operator on a second machine — proves "
    "REPRODUCIBILITY, NOT independent third-party verification; verdicts/hashes can be "
    "copied. The agent is a deterministic script (no LLM judgment). Not consensus, not "
    "mainnet, not payment, not a token; zero-value research-stage."
)


def validate_submission(submission: dict):
    """Validate a submission against the R3 schema/required fields. Returns (ok, reason)."""
    if not isinstance(submission, dict):
        return (False, "submission is not a JSON object")

    for field in _REQUIRED_FIELDS:
        if field not in submission:
            return (False, f"missing required field '{field}'")

    for key, expected in _CONST_FIELDS.items():
        if submission.get(key) != expected:
            return (False, f"field '{key}' must be {expected!r} (got {submission.get(key)!r})")

    if not isinstance(submission["verifier_id"], str) or not submission["verifier_id"]:
        return (False, "verifier_id must be a non-empty string")
    if not isinstance(submission["machine_fingerprint"], str):
        return (False, "machine_fingerprint must be a string")
    if not isinstance(submission["timestamp"], (int, float)) or isinstance(submission["timestamp"], bool):
        return (False, "timestamp must be a number")
    if not isinstance(submission["repo_commit"], str):
        return (False, "repo_commit must be a string")
    if not isinstance(submission["environment_summary"], dict):
        return (False, "environment_summary must be a JSON object")

    oh = submission["output_hash"]
    if not isinstance(oh, str) or len(oh) != 64 or any(c not in _HEX for c in oh):
        return (False, "output_hash must be a 64-char lowercase hex sha256")

    if not isinstance(submission["task_id"], str):
        return (False, "task_id must be a string")
    try:
        verifier_cli.normalize_task_id(submission["task_id"])
    except KeyError as exc:
        return (False, f"unknown task_id: {exc}")

    return (True, "ok: submission conforms to the external-verifier-pilot schema")


def evaluate_submission(submission: dict, ledger: Ledger) -> dict:
    """Evaluate an external submission against the LOCAL canonical hash and anchor the outcome.

    Returns {"evaluation": <result dict>, "ledger_entry": <entry or None>}.
      * malformed -> status 'rejected', NOT anchored (validation precedes any ledger write)
      * hash match -> status 'externally-verified', anchored
      * hash mismatch -> status 'external-mismatch', anchored
    """
    ok, reason = validate_submission(submission)
    if not ok:
        evaluation = {
            "event": "external_verification_result",
            "stage": "R3",
            "topology": "external-verifier-pilot",
            "status": "rejected",
            "reason": reason,
            "match": False,
            "anchored": False,
            "zero_value": True,
            "no_token": True,
            "limitation_note": LIMITATION_NOTE,
            "evaluated_at": time.time(),
        }
        return {"evaluation": evaluation, "ledger_entry": None}

    task_id = submission["task_id"]
    module = verifier_cli.load_task(task_id)
    local_result = module.compute()
    local_hash = module.output_hash(local_result)

    submitted_hash = submission["output_hash"]
    match = (submitted_hash == local_hash)
    status = "externally-verified" if match else "external-mismatch"

    evaluation = {
        "event": "external_verification_result",
        "stage": "R3",
        "topology": "external-verifier-pilot",
        "status": status,
        "match": match,
        "task_id": task_id,
        "verifier_id": submission["verifier_id"],
        "verifier_machine_fingerprint": submission["machine_fingerprint"],
        "submitted_output_hash": submitted_hash,
        "local_output_hash": local_hash,
        "verifier_repo_commit": submission.get("repo_commit", "unknown"),
        "anchored": True,
        "zero_value": True,
        "no_token": True,
        "limitation_note": LIMITATION_NOTE,
        "evaluated_at": time.time(),
    }
    entry = ledger.append(evaluation)
    return {"evaluation": evaluation, "ledger_entry": entry}


# ----------------------------------------------------------------------------
# Agent-verifier-result anchoring: ingest an agent_verifier_attestation, RE-DERIVE its
# claims on the Spark (never trust the file), and anchor an honest record.
# ----------------------------------------------------------------------------
def validate_agent_result(result: dict):
    """Structurally validate an agent_verifier_attestation result (real nested schema).

    Returns (ok, reason). Requires the top-level claim fields PLUS a non-empty
    task_reproductions array where each item carries task_id/recomputed_hash/recorded_hash/match.
    """
    if not isinstance(result, dict):
        return (False, "agent result is not a JSON object")
    for field in _AGENT_RESULT_REQUIRED:
        if field not in result:
            return (False, f"missing required field '{field}'")
    if result.get("event") != _AGENT_RESULT_EVENT:
        return (False, f"field 'event' must be {_AGENT_RESULT_EVENT!r} (got {result.get('event')!r})")
    if result.get("zero_value") is not True:
        return (False, "zero_value must be true")
    if result.get("no_token") is not True:
        return (False, "no_token must be true")
    if not isinstance(result.get("verdict"), str) or not result["verdict"]:
        return (False, "verdict must be a non-empty string")
    if not isinstance(result.get("chain_verified"), bool):
        return (False, "chain_verified must be a boolean")
    if not isinstance(result.get("tip_matches_anchor"), bool):
        return (False, "tip_matches_anchor must be a boolean")
    if not isinstance(result.get("verifier_id"), str) or not result["verifier_id"]:
        return (False, "verifier_id must be a non-empty string")

    reps = result.get("task_reproductions")
    if not isinstance(reps, list) or not reps:
        return (False, "task_reproductions must be a non-empty array")
    for i, rep in enumerate(reps):
        if not isinstance(rep, dict):
            return (False, f"task_reproductions[{i}] is not a JSON object")
        for key in _AGENT_REP_REQUIRED:
            if key not in rep:
                return (False, f"task_reproductions[{i}] missing '{key}'")
        if not isinstance(rep["task_id"], str):
            return (False, f"task_reproductions[{i}].task_id must be a string")
        for hk in ("recomputed_hash", "recorded_hash"):
            h = rep[hk]
            if not isinstance(h, str) or len(h) != 64 or any(c not in _HEX for c in h):
                return (False, f"task_reproductions[{i}].{hk} must be a 64-char lowercase hex sha256")
        if not isinstance(rep["match"], bool):
            return (False, f"task_reproductions[{i}].match must be a boolean")
        try:
            verifier_cli.normalize_task_id(rep["task_id"])
        except KeyError as exc:
            return (False, f"task_reproductions[{i}] unknown task_id: {exc}")
    return (True, "ok: conforms to the agent_verifier_attestation schema")


def _find_ledger_task_hash(entries: list, task_id: str):
    """Return the output_hash recorded in the ledger for `task_id`, or None if not present."""
    for entry in entries:
        payload = entry.get("payload", {})
        if payload.get("task_id") == task_id:
            for key in ("local_output_hash", "output_hash", "submitted_output_hash"):
                if isinstance(payload.get(key), str):
                    return payload[key]
    return None


def _rederive_agent_claims(result: dict, ledger: Ledger, snapshot_path: str, anchor_path: str) -> dict:
    """RE-DERIVE the agent's claims on the Spark (do NOT trust the result file).

    chain_verified    : the published snapshot independently verifies (audit.verify) AND the
                        live ledger verifies AND their tips agree.
    tip_matches_anchor: the published snapshot tip == the committed anchor tip.
    agent_tips_agree  : the agent's provided snapshot_tip_hash/anchor_tip_hash match the
                        Spark's own re-derived values (when the agent provided them).
    per_task          : for EACH task_reproduction, the Spark re-runs that task here and
                        confirms its hash equals BOTH the agent's recomputed_hash AND the
                        hash recorded in the live ledger for that task.
    """
    # 1. chain_verified — verify the published snapshot, the live ledger, and tip agreement.
    snap_ok, snap_reason, snap_details = audit.verify_snapshot_file(snapshot_path)
    snapshot_tip = snap_details.get("tip_hash") if snap_ok else None
    led_ok, led_reason = ledger.verify_chain()
    entries = ledger.read_all()
    ledger_tip = entries[-1]["hash"] if entries else None
    chain_verified = bool(snap_ok and led_ok and snapshot_tip is not None and snapshot_tip == ledger_tip)

    # 2. tip_matches_anchor — published snapshot tip vs committed anchor tip.
    anchor_tip = None
    try:
        with open(anchor_path, "r", encoding="utf-8") as f:
            anchor = json.load(f)
        if isinstance(anchor, dict):
            anchor_tip = anchor.get("tip_hash")
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        anchor_tip = None
    tip_matches = bool(snapshot_tip is not None and anchor_tip is not None and snapshot_tip == anchor_tip)

    # 2b. cross-check the AGENT's provided tips against the Spark's own (when provided).
    agent_snapshot_tip = result.get("snapshot_tip_hash")
    agent_anchor_tip = result.get("anchor_tip_hash")
    agent_tips_agree = bool(
        (agent_snapshot_tip is None or agent_snapshot_tip == snapshot_tip)
        and (agent_anchor_tip is None or agent_anchor_tip == anchor_tip)
    )

    # 3. per-task reproduction — re-run EACH claimed task; compare to the agent's recomputed
    #    hash AND to the hash recorded in the ledger for that task.
    per_task = []
    all_tasks_reproduced = True
    for rep in result.get("task_reproductions", []):
        tid = rep.get("task_id")
        agent_recomputed = rep.get("recomputed_hash")
        local_hash = None
        try:
            module = verifier_cli.load_task(tid)
            local_hash = module.output_hash(module.compute())
        except (KeyError, ImportError, AttributeError, ValueError, TypeError):
            local_hash = None
        ledger_recorded = _find_ledger_task_hash(entries, tid)
        matches_agent = bool(local_hash is not None and local_hash == agent_recomputed)
        matches_ledger = bool(
            local_hash is not None and ledger_recorded is not None and local_hash == ledger_recorded
        )
        reproduced = bool(matches_agent and matches_ledger)
        all_tasks_reproduced = all_tasks_reproduced and reproduced
        per_task.append({
            "task_id": tid,
            "local_output_hash": local_hash,
            "agent_recomputed_hash": agent_recomputed,
            "ledger_recorded_hash": ledger_recorded,
            "matches_agent_recomputed": matches_agent,
            "matches_ledger_recorded": matches_ledger,
            "reproduced": reproduced,
        })
    task_reproduced = bool(per_task and all_tasks_reproduced)

    return {
        "chain_verified": chain_verified,
        "tip_matches_anchor": tip_matches,
        "agent_tips_agree": agent_tips_agree,
        "task_reproduced": task_reproduced,
        "per_task": per_task,
        # extra transparency:
        "snapshot_tip_hash": snapshot_tip,
        "ledger_tip_hash": ledger_tip,
        "anchor_tip_hash": anchor_tip,
        "agent_snapshot_tip_hash": agent_snapshot_tip,
        "agent_anchor_tip_hash": agent_anchor_tip,
        "snapshot_verify_reason": snap_reason,
        "ledger_verify_reason": led_reason,
    }


def anchor_agent_result(result: dict, ledger: Ledger, snapshot_path: str, anchor_path: str,
                        operator_relationship: str = "same-operator") -> dict:
    """Validate + RE-DERIVE + anchor an agent_verifier_attestation. Returns {evaluation, ledger_entry}.

    Malformed -> 'rejected', NOT anchored. All claims re-derived True (chain, tip-vs-anchor,
    agent-tips-agree, every task reproduced) AND agent verdict 'verified' ->
    'agent-result-confirmed', anchored. Otherwise -> 'agent-result-mismatch', anchored (a
    real audit event). The anchored payload carries BOTH the agent's claim AND the Spark's
    per-task re-derivation, with honest labels.
    """
    ok, reason = validate_agent_result(result)
    if not ok:
        evaluation = {
            "event": _AGENT_RESULT_EVENT,
            "stage": "R-protocol",
            "topology": "agent-verifier",
            "status": "rejected",
            "reason": reason,
            "anchored": False,
            "zero_value": True,
            "no_token": True,
            "limitation_note": AGENT_LIMITATION_NOTE,
            "evaluated_at": time.time(),
        }
        return {"evaluation": evaluation, "ledger_entry": None}

    rederived = _rederive_agent_claims(result, ledger, snapshot_path, anchor_path)
    spark_all_true = (
        rederived["chain_verified"]
        and rederived["tip_matches_anchor"]
        and rederived["agent_tips_agree"]
        and rederived["task_reproduced"]
    )
    agent_verdict = result.get("verdict")
    status = ("agent-result-confirmed"
              if (spark_all_true and agent_verdict == "verified")
              else "agent-result-mismatch")

    evaluation = {
        "event": _AGENT_RESULT_EVENT,
        "stage": "R-protocol",
        "topology": "agent-verifier",
        "status": status,
        "agent_verdict": agent_verdict,
        "spark_reconfirmed": {
            "chain_verified": rederived["chain_verified"],
            "tip_matches_anchor": rederived["tip_matches_anchor"],
            "agent_tips_agree": rederived["agent_tips_agree"],
            "task_reproduced": rederived["task_reproduced"],
            "per_task": [
                {
                    "task_id": pt["task_id"],
                    "local_output_hash": pt["local_output_hash"],
                    "matches_agent_recomputed": pt["matches_agent_recomputed"],
                    "matches_ledger_recorded": pt["matches_ledger_recorded"],
                    "reproduced": pt["reproduced"],
                }
                for pt in rederived["per_task"]
            ],
        },
        "task_ids": [pt["task_id"] for pt in rederived["per_task"]],
        "snapshot_tip_hash": rederived["snapshot_tip_hash"],
        "anchor_tip_hash": rederived["anchor_tip_hash"],
        "agent_snapshot_tip_hash": rederived["agent_snapshot_tip_hash"],
        "agent_anchor_tip_hash": rederived["agent_anchor_tip_hash"],
        "verifier_id": result.get("verifier_id"),
        # preserve the agent's own verifier_kind; record the attestation method separately.
        "verifier_kind": result.get("verifier_kind", "autonomous-agent"),
        "verifier_attestation_method": "mechanical-no-LLM",
        "operator_relationship": operator_relationship,
        "limitation_note": AGENT_LIMITATION_NOTE,
        "zero_value": True,
        "no_token": True,
        "anchored_at": time.time(),
    }
    entry = ledger.append(evaluation)
    return {"evaluation": evaluation, "ledger_entry": entry}


# ============================== SELF-TEST ====================================
# NOTE: the following is a LOCAL TEST HARNESS (single-host simulation), NOT the product.
# In real use the submission comes from a SEPARATE machine/person via verifier_cli.py.

def _rmtree(path):
    """Recursively remove a directory tree using os only (no shutil)."""
    if not os.path.exists(path):
        return
    for root, dirs, files in os.walk(path, topdown=False):
        for f in files:
            os.remove(os.path.join(root, f))
        for d in dirs:
            os.rmdir(os.path.join(root, d))
    os.rmdir(path)


def _copyfile(src, dst):
    """Copy a file using os only (no shutil) — for making independent temp ledger copies."""
    with open(src, "rb") as f:
        data = f.read()
    with open(dst, "wb") as g:
        g.write(data)


def _selftest() -> int:
    # Record whether a REAL ledger already exists BEFORE the self-test, so the stray-ledger
    # guard flags only a ledger this self-test ACCIDENTALLY creates — not a legitimate
    # pre-existing real ledger (the self-test itself uses temp paths only).
    _real_ledger_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "ledger_data.jsonl"
    )
    _ledger_existed_before = os.path.exists(_real_ledger_path)

    tmp = os.path.join(
        os.environ.get("TMPDIR", "/tmp"), f"r3_selftest_{os.getpid()}_{int(time.time())}"
    )
    os.makedirs(tmp, exist_ok=False)

    checks = []
    sample_submission = None
    sample_evaluation = None
    signed_attestation = None
    agent_confirmed_payload = None
    try:
        ledger_path = os.path.join(tmp, "ledger.jsonl")
        ledger = Ledger(ledger_path)
        TASK = "task-0002"

        # --- LOCAL TEST HARNESS (single-host simulation; NOT the product) ---------------
        # We generate the external submissions on THIS host via the same code path
        # verifier_cli uses. In production these come from a separate machine/person.

        # (1) VALID submission (correct hash) -> externally-verified -> anchored
        valid_sub = verifier_cli.build_submission(TASK, "verifier-alpha (external, simulated)")
        res_valid = evaluate_submission(valid_sub, ledger)
        sample_submission, sample_evaluation = valid_sub, res_valid["evaluation"]
        checks.append((
            "EXTERNALLY-VERIFIED (correct hash -> anchored)",
            res_valid["evaluation"]["status"] == "externally-verified"
            and res_valid["evaluation"]["match"] is True
            and res_valid["ledger_entry"] is not None,
            res_valid["evaluation"]["status"],
        ))

        # (2) WRONG-hash submission -> external-mismatch -> anchored
        wrong_sub = verifier_cli.build_submission(TASK, "verifier-beta (external, simulated)")
        wrong_sub["output_hash"] = "0" * 64
        res_wrong = evaluate_submission(wrong_sub, ledger)
        checks.append((
            "EXTERNAL-MISMATCH (wrong hash -> anchored)",
            res_wrong["evaluation"]["status"] == "external-mismatch"
            and res_wrong["evaluation"]["match"] is False
            and res_wrong["ledger_entry"] is not None,
            res_wrong["evaluation"]["status"],
        ))

        # (3) MALFORMED submission -> rejected -> NOT anchored
        bad_sub = verifier_cli.build_submission(TASK, "verifier-gamma (external, simulated)")
        del bad_sub["output_hash"]
        res_bad = evaluate_submission(bad_sub, ledger)
        checks.append((
            "REJECTED (malformed -> NOT anchored)",
            res_bad["evaluation"]["status"] == "rejected"
            and res_bad["ledger_entry"] is None,
            f"{res_bad['evaluation']['status']} ({res_bad['evaluation']['reason']})",
        ))

        # (4) ledger chain still verifies after anchoring the two real outcomes
        chain_ok, chain_reason = ledger.verify_chain()
        checks.append(("LEDGER CHAIN VERIFY AFTER ANCHORING", chain_ok is True, chain_reason))

        # (5) OPTIONAL R2 software-key MAC, honestly labeled (uses a TEMP key)
        key_path = os.path.join(tmp, "verifier_key.secret")
        signed_sub = verifier_cli.build_submission(
            TASK, "verifier-delta (external, simulated)", key_path=key_path
        )
        signed_attestation = signed_sub.get("r2_attestation")
        checks.append((
            "OPTIONAL R2 SOFTWARE-KEY MAC (honestly labeled)",
            isinstance(signed_attestation, dict)
            and signed_attestation.get("root_of_trust") == "software-key",
            "root_of_trust=" + str(signed_attestation.get("root_of_trust") if signed_attestation else None),
        ))

        # --- AGENT-RESULT mode coverage (mechanical, no-LLM agent attestation) -----------
        # Build a base temp ledger (genesis + a task-0002 verification at index 1), then a
        # temp published-snapshot + temp anchor exported from it, then sample agent results.
        # All TEMP paths — never the real ledger/snapshot/anchor.
        agent_base = os.path.join(tmp, "agent_ledger.jsonl")
        abl = Ledger(agent_base)
        abl.append({"event": "ledger_genesis", "stage": "R-protocol",
                    "note": "temp chain-start (self-test)", "zero_value": True, "no_token": True})
        evaluate_submission(
            verifier_cli.build_submission(TASK, "jiyu-laptop-same-operator (simulated)"), abl
        )  # index 1: externally-verified task-0002
        agent_snap = os.path.join(tmp, "agent_published.json")
        audit.export_snapshot(agent_base, agent_snap)
        agent_anchor = os.path.join(tmp, "agent_anchor.json")
        audit.write_anchor(agent_base, agent_anchor)
        real_task_hash = verifier_cli.load_task(TASK).output_hash(
            verifier_cli.load_task(TASK).compute()
        )
        base_tip = abl.read_all()[-1]["hash"]  # snapshot/anchor tip the agent must agree with

        def _make_agent_result(recomputed_hash):
            """Sample in jiyu's REAL nested schema (task_reproductions array + tip fields)."""
            return {
                "event": "agent_verifier_attestation", "verdict": "verified",
                "chain_verified": True, "tip_matches_anchor": True,
                "verifier_id": "jiyu-agent-same-operator", "verifier_kind": "autonomous-agent",
                "snapshot_tip_hash": base_tip, "snapshot_tip_index": 1,
                "anchor_tip_hash": base_tip, "anchor_tip_index": 1,
                "task_reproductions": [{
                    "task_id": TASK, "recomputed_hash": recomputed_hash,
                    "recorded_hash": real_task_hash, "match": recomputed_hash == real_task_hash,
                    "ledger_entry_index": 1,
                }],
                "timestamp": 1.0, "zero_value": True, "no_token": True,
            }

        # (6) VALID agent result (real schema, correct hash) -> Spark re-derives -> confirmed
        ledger_a = os.path.join(tmp, "agent_A.jsonl")
        _copyfile(agent_base, ledger_a)
        out_a = anchor_agent_result(
            _make_agent_result(real_task_hash), Ledger(ledger_a), agent_snap, agent_anchor, "same-operator"
        )
        agent_confirmed_payload = out_a["evaluation"]
        sr_a = out_a["evaluation"].get("spark_reconfirmed", {})
        checks.append((
            "AGENT-RESULT CONFIRMED (real schema; Spark re-derives all claims -> anchored)",
            out_a["evaluation"]["status"] == "agent-result-confirmed"
            and out_a["ledger_entry"] is not None
            and sr_a.get("chain_verified") is True
            and sr_a.get("tip_matches_anchor") is True
            and sr_a.get("agent_tips_agree") is True
            and sr_a.get("task_reproduced") is True
            and sr_a["per_task"][0]["reproduced"] is True,
            out_a["evaluation"]["status"],
        ))

        # (7) WRONG recomputed_hash in task_reproductions -> Spark reproduces real hash, disagrees
        ledger_b = os.path.join(tmp, "agent_B.jsonl")
        _copyfile(agent_base, ledger_b)
        out_b = anchor_agent_result(
            _make_agent_result("1" * 64), Ledger(ledger_b), agent_snap, agent_anchor, "same-operator"
        )
        checks.append((
            "AGENT-RESULT MISMATCH (wrong recomputed_hash -> anchored audit event)",
            out_b["evaluation"]["status"] == "agent-result-mismatch"
            and out_b["ledger_entry"] is not None
            and out_b["evaluation"]["spark_reconfirmed"]["task_reproduced"] is False
            and out_b["evaluation"]["spark_reconfirmed"]["per_task"][0]["matches_agent_recomputed"] is False,
            out_b["evaluation"]["status"],
        ))

        # (8) MALFORMED agent result (no task_reproductions) -> rejected -> NOT anchored
        ledger_c = os.path.join(tmp, "agent_C.jsonl")
        _copyfile(agent_base, ledger_c)
        bad_agent = _make_agent_result(real_task_hash)
        del bad_agent["task_reproductions"]
        out_c = anchor_agent_result(bad_agent, Ledger(ledger_c), agent_snap, agent_anchor, "same-operator")
        checks.append((
            "AGENT-RESULT REJECTED (malformed -> NOT anchored)",
            out_c["evaluation"]["status"] == "rejected" and out_c["ledger_entry"] is None,
            f"{out_c['evaluation']['status']} ({out_c['evaluation'].get('reason')})",
        ))

        # --- report ---------------------------------------------------------
        print("=== protocol/external_verifier.py self-test — R3 EXTERNAL-VERIFIER-PILOT ===")
        print("HONEST: a matching output_hash proves REPRODUCIBILITY, NOT execution (a hash")
        print("can be copied). Not consensus, not mainnet, not payment, not a token; zero-value.")
        print("This self-test is a LOCAL TEST HARNESS (single-host simulation), NOT the product;")
        print("in real use the submission arrives from a SEPARATE machine/person.\n")

        print("--- sample VALID external submission (note honest topology + limitation) ---")
        print(json.dumps(sample_submission, indent=2, sort_keys=True))
        print()
        print("--- coordinator evaluation of that submission (externally-verified) ---")
        print(json.dumps(sample_evaluation, indent=2, sort_keys=True))
        print()
        print("--- OPTIONAL attached R2 software-key MAC (symmetric; not a signature; not hardware; not execution-proof) ---")
        print(json.dumps(signed_attestation, indent=2, sort_keys=True))
        print()
        print("--- sample ANCHORED agent-result payload (note spark_reconfirmed + honest labels) ---")
        print("    HONEST: the Spark RE-DERIVES the agent's claims here; it does not trust the file.")
        print(json.dumps(agent_confirmed_payload, indent=2, sort_keys=True))
        print()

        print("--- results ---")
        for label, passed, _detail in checks:
            print(f"{label}: {'PASS' if passed else 'FAIL'}")
        print()
        print("--- detail ---")
        for label, _passed, detail in checks:
            print(f"  {label}\n      -> {detail}")
        print()

        all_ok = all(passed for _l, passed, _d in checks)
        print("=== self-test summary: " +
              ("ALL CHECKS BEHAVED CORRECTLY" if all_ok else "FAILURE — see above") + " ===")
    finally:
        _rmtree(tmp)

    # Confirm no stray default files anywhere in protocol/.
    proto = os.path.dirname(os.path.abspath(__file__))
    stray_ledger = os.path.exists(_real_ledger_path) and not _ledger_existed_before
    stray_keys = [n for n in os.listdir(proto) if n.endswith(".secret")]
    stray_sub = os.path.exists(os.path.join(proto, "submission.json"))
    print(f"\nstray default ledger file: {'PRESENT (!!)' if stray_ledger else 'absent (good)'}")
    print(f"stray protocol/*.secret key files: {stray_keys if stray_keys else 'none (good)'}")
    print(f"stray submission.json: {'PRESENT (!!)' if stray_sub else 'absent (good)'}")

    ok_overall = (
        all(passed for _l, passed, _d in checks)
        and not stray_ledger
        and not stray_keys
        and not stray_sub
    )
    return 0 if ok_overall else 1


# ============================== REAL COORDINATOR CLI =========================
# Importable functions and the self-test above are unchanged. The CLI below lets the
# coordinator ingest a REAL submission file and anchor the outcome into a REAL persistent
# ledger. With NO arguments, main() runs the self-test on a temp ledger and never touches
# the real ledger (so CI's `python3 protocol/external_verifier.py` keeps running 3/3).

BANNER = (
    "MetaCoin R3 external-verifier pilot — research-stage, zero-value. "
    "A matching hash proves REPRODUCIBILITY, not execution."
)


def _cmd_genesis(ledger_path: str) -> int:
    """Append a neutral chain-start marker IFF the ledger is empty; refuse otherwise.

    Exit 0 on success; non-zero if the ledger already has entries (never double-genesis).
    """
    print(BANNER)
    ledger = Ledger(ledger_path)
    if ledger.read_all():
        print(f"ledger already initialized, genesis exists at index 0 (path: {ledger_path})")
        print("refusing to double-genesis")
        return 1
    payload = {
        "event": "ledger_genesis",
        "stage": "R-protocol",
        "note": (
            "MetaCoin research-stage protocol ledger chain-start marker. "
            "Zero-value, no token, no money. Research only."
        ),
        "zero_value": True,
        "no_token": True,
    }
    entry = ledger.append(payload)
    print(f"genesis written: index {entry['index']}, hash {entry['hash'][:16]}... (path: {ledger_path})")
    ok, reason = ledger.verify_chain()
    print(f"chain verify: {'OK' if ok else 'FAIL'} — {reason}")
    return 0 if ok else 1


def _cmd_evaluate(submission_path: str, ledger_path: str) -> int:
    """Load a submission file, evaluate it, and anchor the outcome into the ledger.

    Exit codes: 0 = externally-verified; non-zero = external-mismatch or rejected.
    A missing/invalid file is 'rejected' and anchors nothing.
    """
    print(BANNER)

    # Load the submission file defensively; any load failure is a rejection (no anchor).
    reason = None
    submission = None
    try:
        with open(submission_path, "r", encoding="utf-8") as f:
            submission = json.load(f)
    except FileNotFoundError:
        reason = f"submission file not found: {submission_path}"
    except json.JSONDecodeError as exc:
        reason = f"submission file is not valid JSON ({exc})"
    except OSError as exc:
        reason = f"could not read submission file ({exc})"
    if reason is None and not isinstance(submission, dict):
        reason = "submission JSON is not an object"
    if reason is not None:
        print("status: rejected")
        print(f"reason: {reason}")
        print("anchored: no (nothing written to the ledger)")
        return 1

    ledger = Ledger(ledger_path)
    result = evaluate_submission(submission, ledger)
    ev = result["evaluation"]
    entry = result["ledger_entry"]
    status = ev["status"]

    print(f"status: {status}")
    print(f"task_id: {submission.get('task_id', 'unknown')}")
    print(f"verifier_id: {submission.get('verifier_id', 'unknown')}")

    if status == "rejected":
        print(f"reason: {ev.get('reason')}")
        print("anchored: no (malformed submissions are not written to the ledger)")
        return 1

    print(f"submitted_output_hash: {ev['submitted_output_hash']}")
    print(f"local_output_hash:     {ev['local_output_hash']}")
    print(f"match: {ev['match']}")
    if entry is not None:
        print(f"anchored at ledger index: {entry['index']} (path: {ledger_path})")
    ok, vreason = ledger.verify_chain()
    print(f"chain verify: {'OK' if ok else 'FAIL'} — {vreason}")

    # 0 only for a genuine external verification; mismatch is a real but non-zero outcome.
    return 0 if status == "externally-verified" else 1


def _cmd_anchor_agent_result(result_path: str, ledger_path: str, snapshot_path: str,
                             anchor_path: str, operator_relationship: str) -> int:
    """Load an agent_verifier_attestation, RE-DERIVE its claims on the Spark, anchor the outcome.

    Exit codes: 0 = agent-result-confirmed; non-zero = mismatch or rejected.
    A missing/invalid file is 'rejected' and anchors nothing.
    """
    print(BANNER)

    # Load defensively; any load failure is a rejection (no anchor).
    reason = None
    result = None
    try:
        with open(result_path, "r", encoding="utf-8") as f:
            result = json.load(f)
    except FileNotFoundError:
        reason = f"agent-result file not found: {result_path}"
    except json.JSONDecodeError as exc:
        reason = f"agent-result file is not valid JSON ({exc})"
    except OSError as exc:
        reason = f"could not read agent-result file ({exc})"
    if reason is None and not isinstance(result, dict):
        reason = "agent-result JSON is not an object"
    if reason is not None:
        print("status: rejected")
        print(f"reason: {reason}")
        print("anchored: no (nothing written to the ledger)")
        return 1

    ledger = Ledger(ledger_path)
    out = anchor_agent_result(result, ledger, snapshot_path, anchor_path, operator_relationship)
    ev = out["evaluation"]
    entry = out["ledger_entry"]
    status = ev["status"]

    print(f"status: {status}")
    print(f"verifier_id: {result.get('verifier_id', 'unknown')}")
    print(f"agent_verdict: {result.get('verdict', 'unknown')}")

    if status == "rejected":
        print(f"reason: {ev.get('reason')}")
        print("anchored: no (malformed -> not written to the ledger)")
        return 1

    sr = ev["spark_reconfirmed"]
    print(f"task_ids: {ev.get('task_ids')}")
    print("spark re-derivation (independently re-derived here — NOT trusting the agent's file):")
    print(f"  chain_verified     : {sr['chain_verified']}")
    print(f"  tip_matches_anchor : {sr['tip_matches_anchor']}")
    print(f"  agent_tips_agree   : {sr['agent_tips_agree']}")
    print(f"  task_reproduced    : {sr['task_reproduced']}")
    for pt in sr.get("per_task", []):
        print(f"    - {pt['task_id']}: local={pt['local_output_hash']} "
              f"matches_agent={pt['matches_agent_recomputed']} "
              f"matches_ledger={pt['matches_ledger_recorded']} reproduced={pt['reproduced']}")
    if entry is not None:
        print(f"anchored at ledger index: {entry['index']} (path: {ledger_path})")
    ok, vreason = ledger.verify_chain()
    print(f"chain verify: {'OK' if ok else 'FAIL'} — {vreason}")

    # 0 only when the Spark re-derivation confirms the agent's 'verified' verdict.
    return 0 if status == "agent-result-confirmed" else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="external_verifier.py",
        description=(
            "MetaCoin R3 external-verifier-pilot COORDINATOR (research-stage, ZERO-VALUE, "
            "no token). With NO arguments, runs the self-test on a TEMP ledger and does NOT "
            "touch the real ledger."
        ),
        epilog=(
            "Exit codes: 0 = externally-verified (or --genesis / self-test ok); non-zero = "
            "external-mismatch, rejected, genesis-refused, or self-test failure. HONEST "
            "LIMITATION: a matching hash proves REPRODUCIBILITY, not execution (a hash can be "
            "copied). Not consensus, not mainnet, not payment, not a token."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--genesis", action="store_true",
        help="append a neutral chain-start marker IFF the ledger is empty (refuses if already initialized)",
    )
    mode.add_argument(
        "--evaluate", metavar="SUBMISSION_JSON",
        help="evaluate an external-verifier submission file against the locally recomputed hash and anchor the outcome",
    )
    mode.add_argument(
        "--anchor-agent-result", metavar="AGENT_RESULT_JSON",
        help="ingest an agent_verifier_attestation; RE-DERIVE its chain/anchor/task claims on this host and anchor the outcome",
    )
    parser.add_argument(
        "--ledger", default=DEFAULT_LEDGER_PATH,
        help=f"ledger path (default: the REAL persistent ledger at {DEFAULT_LEDGER_PATH})",
    )
    parser.add_argument(
        "--snapshot", default=_DEFAULT_PUBLISHED_SNAPSHOT_PATH,
        help=f"published snapshot to re-verify for --anchor-agent-result (default {_DEFAULT_PUBLISHED_SNAPSHOT_PATH})",
    )
    parser.add_argument(
        "--anchor-file", default=audit.DEFAULT_ANCHOR_PATH,
        help=f"committed tip anchor to compare for --anchor-agent-result (default {audit.DEFAULT_ANCHOR_PATH})",
    )
    parser.add_argument(
        "--operator-relationship", default="same-operator",
        help="honest label for the verifier's relationship to this operator (default: same-operator)",
    )
    args = parser.parse_args(argv)

    if args.genesis:
        return _cmd_genesis(args.ledger)
    if args.evaluate is not None:
        return _cmd_evaluate(args.evaluate, args.ledger)
    if args.anchor_agent_result is not None:
        return _cmd_anchor_agent_result(
            args.anchor_agent_result, args.ledger, args.snapshot, args.anchor_file,
            args.operator_relationship,
        )
    # No command -> run the self-test (temp ledger only; never touches the real ledger).
    return _selftest()


if __name__ == "__main__":
    sys.exit(main())
