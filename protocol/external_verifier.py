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

import json
import os
import sys
import time

# Make `from protocol...` resolve when run directly (repo root on sys.path).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from protocol.ledger import Ledger
import protocol.verifier_cli as verifier_cli

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


def _selftest() -> int:
    tmp = os.path.join(
        os.environ.get("TMPDIR", "/tmp"), f"r3_selftest_{os.getpid()}_{int(time.time())}"
    )
    os.makedirs(tmp, exist_ok=False)

    checks = []
    sample_submission = None
    sample_evaluation = None
    signed_attestation = None
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
    stray_ledger = os.path.exists(os.path.join(proto, "ledger_data.jsonl"))
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


if __name__ == "__main__":
    sys.exit(_selftest())
