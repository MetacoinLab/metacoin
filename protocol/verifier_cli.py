"""verifier_cli.py — MetaCoin R3 EXTERNAL VERIFIER tool (the verifier-side command).

================== CRITICAL HONESTY NOTICE (READ ME) ==================
This is the "external-verifier-pilot" for reproducible useful-work verification. It is:
  * NOT decentralized consensus, NOT mainnet, NOT a payment, NOT a token, NOT a wallet.
  * A research-stage, ZERO-VALUE pilot in which a REAL external party (another physical
    machine, a CI runner, a VPS, or a collaborator's laptop) re-runs the same reproducible
    task locally and submits the resulting canonical output_hash for the coordinator to
    compare against its own locally recomputed hash.

PRECISE, HONEST CLAIM (the important limitation):
  A matching output_hash proves the result is REPRODUCIBLE — it was independently
  re-derived to the same canonical value. It does NOT, by itself, cryptographically prove
  that THIS verifier actually executed the task: a hash is a short string and can be copied.
  So the honest claim a successful match supports is "an external party submitted a
  reproducibly-matching result", NOT "we proved independent execution". Independence is
  strengthened when the verifier is operated by a separate person/organization. Genuine
  execution-proof would require verifier-held signing keys and/or hardware attestation —
  future work, slotting in behind this same interface.

Standard library only (json, hashlib, os, time, subprocess, platform, argparse). Optional
signing delegates to the existing R2 component (protocol/attest.py) and is labeled exactly
as the R2 software-key MAC (symmetric, NOT a public-key signature, NOT hardware-backed, and
does NOT prove execution).

Usage (what an external verifier runs after cloning the repo):
    python3 protocol/verifier_cli.py --task task-0002 --verifier-id alice-laptop
    python3 protocol/verifier_cli.py --task task-0002 --verifier-id ci-runner --out submission.json
"""

import argparse
import hashlib
import importlib
import json
import os
import platform
import subprocess
import sys
import time

# Make `from protocol...` / `import demo...` resolve when run directly (repo root on path).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# Map a short task id to the demo task module that implements it. Each module exposes the
# standard interface compute()/canonical_json()/output_hash(). Generic: add ids here.
TASK_MODULES = {
    "task-0001": "demo.tasks.task_0001_lunar_link_budget",
    "task-0002": "demo.tasks.task_0002_orbit_propagation",
    "task-0003": "demo.tasks.task_0003_power_eclipse",
    "task-0004": "demo.tasks.task_0004_comms_access",
    "task-0005": "demo.tasks.task_0005_rover_path",
    "task-0006": "demo.tasks.task_0006_docking_approach",
    "task-0007": "demo.tasks.task_0007_hohmann_transfer",
}

# Honest note embedded in every submission (and visible in output).
HONEST_NOTE = (
    "external-verifier-pilot (research-stage, zero-value). A matching output_hash proves "
    "REPRODUCIBILITY (independently re-derived to the same canonical value); it does NOT "
    "prove this verifier executed the task (a hash can be copied). Not consensus, not "
    "mainnet, not payment, not a token."
)


def normalize_task_id(task_id: str) -> str:
    """Normalize a task id to a known short key (e.g. 'task-0002-orbit...' -> 'task-0002')."""
    if task_id in TASK_MODULES:
        return task_id
    parts = task_id.split("-")
    if len(parts) >= 2 and parts[0] == "task":
        short = f"task-{parts[1]}"
        if short in TASK_MODULES:
            return short
    raise KeyError(f"unknown task id {task_id!r}; known: {sorted(TASK_MODULES)}")


def load_task(task_id: str):
    """Import and return the demo task module for `task_id` (short or full form)."""
    return importlib.import_module(TASK_MODULES[normalize_task_id(task_id)])


def machine_fingerprint() -> str:
    """A COARSE, NON-identifying fingerprint: sha256 of platform string + machine arch.

    Deliberately uses only platform.platform() + platform.machine() — NO hostname, NO user,
    NO IP, NO MAC address. It is not PII; it only coarsely groups submissions by OS/arch.
    """
    raw = f"{platform.platform()}|{platform.machine()}"
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def environment_summary() -> dict:
    """A coarse, non-identifying environment summary (Python version + platform/arch)."""
    return {
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "note": "coarse, non-identifying environment summary (no hostname/user/IP)",
    }


def get_repo_commit() -> str:
    """Return `git rev-parse HEAD` if available, else 'unknown' (best-effort, read-only)."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        pass
    return "unknown"


def build_submission(task_id: str, verifier_id: str, key_path: str = None) -> dict:
    """Run the named task locally and build the external-verifier submission record.

    The output_hash is computed by the task module's own canonical_json/output_hash, so it
    matches whatever the coordinator recomputes locally for the same task. If `key_path` is
    given, an OPTIONAL R2 software-key MAC (HMAC-SHA256) is attached and labeled honestly —
    it binds this submission to a key the verifier holds but does NOT prove execution.
    """
    short = normalize_task_id(task_id)
    module = importlib.import_module(TASK_MODULES[short])

    result = module.compute()
    output_hash = module.output_hash(result)

    submission = {
        "event": "external_verifier_submission",
        "stage": "R3",
        "topology": "external-verifier-pilot",
        "task_id": short,
        "verifier_id": verifier_id,
        "machine_fingerprint": machine_fingerprint(),
        "timestamp": time.time(),
        "output_hash": output_hash,
        "repo_commit": get_repo_commit(),
        "environment_summary": environment_summary(),
        "zero_value": True,
        "no_token": True,
        "honest_note": HONEST_NOTE,
    }

    if key_path:
        # Delegate to the existing R2 software-rooted attestation (exact same honest MAC).
        from protocol.attest import SoftwareAttestor

        attestor = SoftwareAttestor(key_path)
        submission["r2_attestation"] = attestor.attest(
            short,
            output_hash,
            extra={"context": "external_verifier_submission", "verifier_id": verifier_id},
        )
        submission["signing_note"] = (
            "OPTIONAL R2 software-key MAC attached (symmetric HMAC-SHA256; NOT a public-key "
            "signature; NOT hardware-backed). It binds task_id+output_hash to a key the "
            "verifier holds; it does NOT prove the verifier executed the task."
        )

    return submission


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="verifier_cli.py",
        description=(
            "MetaCoin R3 external-verifier-pilot (research-stage, ZERO-VALUE). Re-run a "
            "reproducible task locally and emit a submission for the coordinator to compare "
            "against its own locally recomputed hash."
        ),
        epilog=(
            "HONEST LIMITATION: a matching output_hash proves REPRODUCIBILITY, not "
            "execution (a hash can be copied). Not consensus, not mainnet, not payment, not "
            "a token. Known tasks: " + ", ".join(sorted(TASK_MODULES)) + "."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--task", required=True,
                        help="task id to re-run, e.g. task-0002 (short or full form)")
    parser.add_argument("--verifier-id", required=True,
                        help="name/handle of the external verifier (ideally a separate person or org)")
    parser.add_argument("--out",
                        help="write the submission JSON to this file (default: print to stdout)")
    parser.add_argument("--key",
                        help="OPTIONAL path to a verifier software key file; attaches an honest "
                             "R2 symmetric MAC (NOT a signature, NOT hardware-backed, does NOT prove execution)")
    args = parser.parse_args(argv)

    try:
        submission = build_submission(args.task, args.verifier_id, key_path=args.key)
    except KeyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    text = json.dumps(submission, indent=2, sort_keys=True)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        print(f"wrote external-verifier submission to {args.out}", file=sys.stderr)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
