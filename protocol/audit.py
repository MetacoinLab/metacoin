# Copyright (c) 2023-2026 MetaCoin-Lab.
# Licensed under SML-1.0 — see LICENSE.md.
"""audit.py — MetaCoin protocol auditability: export, standalone verify, and tip anchor.

================== HONESTY / SCOPE (READ ME) ==================
Research-stage, ZERO-VALUE, no token, no money, no mainnet. This tool makes the R1
hash-chained ledger INDEPENDENTLY AUDITABLE BY ANYONE, instead of merely existing on one
machine. Three capabilities:

  1. --export <out>  : write a verifiable point-in-time SNAPSHOT of the ledger (a header +
                       all entries verbatim). The chain is verified BEFORE export; a broken
                       chain is never published. The snapshot is shareable/publishable.
  2. --verify <snap> : a SELF-CONTAINED verifier anyone can run on a published snapshot
                       WITHOUT the original ledger file. It re-walks the entries from
                       genesis, recomputes every hash, checks linkage + index sequencing,
                       and confirms the header's tip_hash matches the recomputed last hash.
  3. --anchor <path> : write a TINY public ANCHOR file (tip_index, tip_hash, entry_count,
                       timestamp + honest labels) — meant to be COMMITTED to the repo.

Why this matters (and how it honestly closes a prior gap):
  * Until now the real ledger lived only on the Spark (gitignored, not shareable). Publishing
    a snapshot + committing the tiny anchor is what makes the chain auditable by anyone.
  * R1 documented an honest limitation: a standalone hash chain cannot, by itself, stop an
    attacker who rewrites the ENTIRE suffix (re-indexing and re-hashing every later entry),
    because that produces an internally consistent chain. The fix is an EXTERNAL anchor. The
    committed anchor file IS that external anchor: once tip_hash + entry_count are pinned in
    git history, any later snapshot can be checked against the committed tip to prove the
    chain was not silently rewritten. The anchor reveals ONLY the tip hash + count, NOT the
    ledger contents.

Standard library only (json, hashlib, os, time, argparse). Verification hashing is REUSED
from protocol/ledger.py (not reimplemented): this module imports ledger's _compute_hash,
_content_for_hash, _ENTRY_KEYS, and GENESIS_PREV_HASH and mirrors verify_chain's checks.
Not legal, financial, investment, or security-certification advice.
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

# Reuse R1's verification primitives — DO NOT reimplement hashing here.
from protocol.ledger import (
    Ledger,
    DEFAULT_LEDGER_PATH,
    GENESIS_PREV_HASH,
    _ENTRY_KEYS,
    _content_for_hash,
    _compute_hash,
)

SNAPSHOT_FORMAT = "metacoin-ledger-snapshot"
ANCHOR_FORMAT = "metacoin-ledger-anchor"

# Default committed anchor path (this file IS meant to be committed, unlike the ledger).
DEFAULT_ANCHOR_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "ledger_anchor.json"
)

BANNER = (
    "MetaCoin protocol auditability (audit.py) — research-stage, zero-value, no token. "
    "Snapshot = full verifiable copy; anchor = tiny public tip commitment."
)


# ----------------------------------------------------------------------------
# Standalone chain verification over an in-memory entries list.
# Mirrors protocol/ledger.py's verify_chain checks, reusing its hashing primitives.
# ----------------------------------------------------------------------------
def _verify_entries(entries) -> tuple:
    """Re-walk entries from genesis, recomputing every hash. Returns (ok, reason).

    Uses ledger.py's _compute_hash/_content_for_hash/_ENTRY_KEYS/GENESIS_PREV_HASH so the
    hashing and rules match the R1 ledger exactly; only the walk is local (over a list).
    """
    if not entries:
        return (False, "snapshot contains no entries")

    prev_hash_actual = None
    for position, entry in enumerate(entries):
        if not isinstance(entry, dict):
            return (False, f"entry at position {position} is not a JSON object")

        keys = set(entry.keys())
        if keys != set(_ENTRY_KEYS):
            missing = sorted(set(_ENTRY_KEYS) - keys)
            extra = sorted(keys - set(_ENTRY_KEYS))
            return (False, f"entry at position {position} has wrong fields "
                           f"(missing={missing}, unexpected={extra})")

        if not isinstance(entry["index"], int) or isinstance(entry["index"], bool):
            return (False, f"entry at position {position}: 'index' is not an integer")
        if not isinstance(entry["payload"], dict):
            return (False, f"entry at position {position}: 'payload' is not a JSON object")
        if not isinstance(entry["prev_hash"], str) or not isinstance(entry["hash"], str):
            return (False, f"entry at position {position}: 'prev_hash'/'hash' must be strings")

        if entry["index"] != position:
            return (False, f"index out of sequence at position {position}: found "
                           f"{entry['index']} (expected {position}) — gap/reorder/insert")

        recomputed = _compute_hash(_content_for_hash(entry))
        if recomputed != entry["hash"]:
            return (False, f"index {entry['index']}: stored hash does not match recomputed "
                           f"hash (content was altered)")

        if position == 0:
            if entry["prev_hash"] != GENESIS_PREV_HASH:
                return (False, "index 0: genesis prev_hash is not 64 zeros")
        else:
            if entry["prev_hash"] != prev_hash_actual:
                return (False, f"index {entry['index']}: prev_hash does not match the previous "
                               f"entry's hash (broken/forged chain link)")

        prev_hash_actual = entry["hash"]

    return (True, f"{len(entries)} entries verified from genesis")


# ----------------------------------------------------------------------------
# Shared ledger read + verify (refuse to publish/anchor a broken or empty chain).
# ----------------------------------------------------------------------------
def _read_and_verify_ledger(ledger_path: str) -> tuple:
    """Return (ok, reason, entries). Verifies the chain first; refuses broken/empty ledgers."""
    if not os.path.exists(ledger_path):
        return (False, f"ledger file does not exist: {ledger_path}", None)
    led = Ledger(ledger_path)
    ok, reason = led.verify_chain()
    if not ok:
        return (False, f"ledger chain does not verify ({reason}) — refusing", None)
    entries = led.read_all()
    if not entries:
        return (False, "ledger is empty (no entries) — refusing", None)
    return (True, "ok", entries)


# ----------------------------------------------------------------------------
# Export a verifiable snapshot.
# ----------------------------------------------------------------------------
def export_snapshot(ledger_path: str, out_path: str) -> tuple:
    """Verify the ledger, then write a snapshot (header + verbatim entries). Returns (ok, header|reason)."""
    ok, reason, entries = _read_and_verify_ledger(ledger_path)
    if not ok:
        return (False, reason)

    tip = entries[-1]
    header = {
        "format": SNAPSHOT_FORMAT,
        "snapshot_version": 1,
        "snapshot_timestamp": time.time(),
        "entry_count": len(entries),
        "tip_index": tip["index"],
        "tip_hash": tip["hash"],
        "stage": "R-protocol",
        "zero_value": True,
        "no_token": True,
        "research_only": True,
        "note": ("point-in-time snapshot of the MetaCoin protocol ledger; verify "
                 "independently with audit.py --verify"),
    }
    snapshot = {"snapshot_header": header, "entries": entries}
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, indent=2, sort_keys=True)
        f.write("\n")
    return (True, header)


# ----------------------------------------------------------------------------
# Independently verify a snapshot (no original ledger needed).
# ----------------------------------------------------------------------------
def verify_snapshot_obj(snapshot) -> tuple:
    """Verify a loaded snapshot object. Returns (ok, reason, details)."""
    if not isinstance(snapshot, dict):
        return (False, "snapshot is not a JSON object", {})
    header = snapshot.get("snapshot_header")
    entries = snapshot.get("entries")
    if not isinstance(header, dict):
        return (False, "snapshot missing a 'snapshot_header' object", {})
    if not isinstance(entries, list):
        return (False, "snapshot missing an 'entries' list", {})

    ok, reason = _verify_entries(entries)
    if not ok:
        return (False, f"chain verification failed: {reason}", {})

    if header.get("entry_count") != len(entries):
        return (False, f"header entry_count {header.get('entry_count')} != actual "
                       f"{len(entries)} entries", {})

    tip = entries[-1]
    recomputed_tip = _compute_hash(_content_for_hash(tip))
    if header.get("tip_hash") != recomputed_tip:
        return (False, "header tip_hash does not match the recomputed hash of the last "
                       "entry", {})
    if header.get("tip_index") != tip["index"]:
        return (False, f"header tip_index {header.get('tip_index')} != last entry index "
                       f"{tip['index']}", {})

    return (True, f"chain intact, {len(entries)} entries, tip_hash matches",
            {"entry_count": len(entries), "tip_index": tip["index"], "tip_hash": recomputed_tip})


def verify_snapshot_file(path: str) -> tuple:
    """Load and verify a snapshot file. Returns (ok, reason, details)."""
    if not os.path.exists(path):
        return (False, f"snapshot file does not exist: {path}", {})
    try:
        with open(path, "r", encoding="utf-8") as f:
            snapshot = json.load(f)
    except json.JSONDecodeError as exc:
        return (False, f"snapshot is not valid JSON ({exc})", {})
    except OSError as exc:
        return (False, f"could not read snapshot ({exc})", {})
    return verify_snapshot_obj(snapshot)


# ----------------------------------------------------------------------------
# Write a tiny public tip anchor (meant to be committed).
# ----------------------------------------------------------------------------
def write_anchor(ledger_path: str, anchor_path: str) -> tuple:
    """Verify the ledger, then write a tiny tip-only anchor file. Returns (ok, anchor|reason)."""
    ok, reason, entries = _read_and_verify_ledger(ledger_path)
    if not ok:
        return (False, reason)

    tip = entries[-1]
    anchor = {
        "format": ANCHOR_FORMAT,
        "anchor_version": 1,
        "tip_index": tip["index"],
        "tip_hash": tip["hash"],
        "entry_count": len(entries),
        "anchor_timestamp": time.time(),
        "stage": "R-protocol",
        "zero_value": True,
        "no_token": True,
        "research_only": True,
        "note": ("public commitment to the MetaCoin protocol ledger tip (research-stage, "
                 "zero-value, no token). Contains ONLY tip_hash + tip_index + entry_count — "
                 "NOT ledger contents. Commit this file; an auditor checks a published "
                 "snapshot's tip against it to confirm the chain was not silently rewritten."),
    }
    with open(anchor_path, "w", encoding="utf-8") as f:
        json.dump(anchor, f, indent=2, sort_keys=True)
        f.write("\n")
    return (True, anchor)


# ============================== CLI ==========================================
def _cmd_export(ledger_path: str, out_path: str) -> int:
    print(BANNER)
    ok, result = export_snapshot(ledger_path, out_path)
    if not ok:
        print(f"export refused: {result}")
        return 1
    h = result
    print(f"exported snapshot: {out_path}")
    print(f"  entries: {h['entry_count']}  tip_index: {h['tip_index']}  tip_hash: {h['tip_hash']}")
    print("  chain was verified before export. Verify independently with: "
          f"python3 protocol/audit.py --verify {out_path}")
    return 0


def _cmd_verify(snapshot_path: str) -> int:
    print(BANNER)
    ok, reason, details = verify_snapshot_file(snapshot_path)
    if ok:
        print(f"VERIFIED — {reason}")
        print(f"  entries: {details['entry_count']}  tip_index: {details['tip_index']}  "
              f"tip_hash: {details['tip_hash']}")
        return 0
    print(f"FAILED — {reason}")
    return 1


def _cmd_anchor(ledger_path: str, anchor_path: str) -> int:
    print(BANNER)
    ok, result = write_anchor(ledger_path, anchor_path)
    if not ok:
        print(f"anchor refused: {result}")
        return 1
    a = result
    print(f"wrote tip anchor: {anchor_path}")
    print(f"  tip_index: {a['tip_index']}  entry_count: {a['entry_count']}  tip_hash: {a['tip_hash']}")
    print("  public commitment to the TIP ONLY (not ledger contents). This file is meant to "
          "be committed to the repo.")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="audit.py",
        description=(
            "MetaCoin protocol auditability (research-stage, ZERO-VALUE, no token). Export a "
            "verifiable ledger snapshot, independently verify a snapshot, or write a tiny "
            "committable tip anchor. With NO arguments, runs the self-test on a TEMP ledger "
            "(does not touch the real ledger)."
        ),
        epilog=(
            "Publishing a snapshot + committing the tiny anchor is what makes the ledger "
            "independently auditable by anyone, and the committed anchor is the EXTERNAL "
            "anchor that closes R1's full-suffix-rewrite gap. Research only; zero-value; no "
            "token. Exit: 0 = ok/VERIFIED; non-zero = refused/FAILED/self-test failure."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--export", metavar="OUT_PATH",
                      help="export a verifiable snapshot of the ledger (refuses if the chain does not verify)")
    mode.add_argument("--verify", metavar="SNAPSHOT_PATH",
                      help="independently verify a published snapshot (no original ledger needed)")
    mode.add_argument("--anchor", nargs="?", const=DEFAULT_ANCHOR_PATH, metavar="ANCHOR_PATH",
                      help=f"write a tiny public tip-anchor file (default {DEFAULT_ANCHOR_PATH}); meant to be committed")
    parser.add_argument("--ledger", default=DEFAULT_LEDGER_PATH,
                        help=f"ledger path for --export/--anchor (default: the real ledger {DEFAULT_LEDGER_PATH})")
    args = parser.parse_args(argv)

    if args.export is not None:
        return _cmd_export(args.ledger, args.export)
    if args.verify is not None:
        return _cmd_verify(args.verify)
    if args.anchor is not None:
        return _cmd_anchor(args.ledger, args.anchor)
    # No command -> run the self-test (temp ledger only; never touches the real ledger).
    return _selftest()


# ============================== SELF-TEST ====================================
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


def _read_raw(path):
    with open(path, "r", encoding="utf-8") as f:
        return [ln.rstrip("\n") for ln in f if ln.strip()]


def _write_raw(path, lines):
    with open(path, "w", encoding="utf-8") as f:
        for ln in lines:
            f.write(ln + "\n")


def _selftest() -> int:
    # Record protocol/ state BEFORE the self-test so the stray-file guards flag only what
    # this self-test ACCIDENTALLY creates — not a legitimately committed anchor (the real
    # public tip anchor lives at DEFAULT_ANCHOR_PATH) or a pre-existing local snapshot.
    # The self-test itself uses temp paths only.
    _proto_dir = os.path.dirname(os.path.abspath(__file__))
    _anchor_existed_before = os.path.exists(DEFAULT_ANCHOR_PATH)
    _snaps_before = {n for n in os.listdir(_proto_dir) if "snapshot" in n.lower()}

    tmp = os.path.join(
        os.environ.get("TMPDIR", "/tmp"), f"audit_selftest_{os.getpid()}_{int(time.time())}"
    )
    os.makedirs(tmp, exist_ok=False)

    checks = []
    sample_header = None
    sample_anchor = None
    verified_line = None
    try:
        ledger_path = os.path.join(tmp, "ledger.jsonl")
        led = Ledger(ledger_path)
        led.append({"event": "ledger_genesis", "stage": "R-protocol",
                    "note": "temp chain-start (self-test)", "zero_value": True, "no_token": True})
        led.append({"event": "proof_record", "task_id": "task-0002",
                    "output_hash": "ff03231fc53a0505f648e2ec24e251ced41c896fbfd2553ffaa914ffd5ba300c",
                    "verdict": "PASS"})
        led.append({"event": "external_verification_result", "status": "externally-verified",
                    "task_id": "task-0002", "verifier_id": "selftest-verifier",
                    "zero_value": True, "no_token": True})

        # (1) export good chain
        snap_path = os.path.join(tmp, "snapshot.json")
        ok_exp, header = export_snapshot(ledger_path, snap_path)
        sample_header = header if ok_exp else None
        checks.append(("EXPORT (good chain)", ok_exp is True and os.path.exists(snap_path),
                       f"tip_index={header.get('tip_index') if ok_exp else header}"))

        # (2) export refuses a broken chain
        broken_ledger = os.path.join(tmp, "ledger_broken.jsonl")
        lines = _read_raw(ledger_path)
        e1 = json.loads(lines[1]); e1["payload"]["verdict"] = "TAMPERED"
        lines[1] = json.dumps(e1, sort_keys=True, separators=(",", ":"))
        _write_raw(broken_ledger, lines)
        broken_out = os.path.join(tmp, "should_not_exist.json")
        ok_broken, reason_broken = export_snapshot(broken_ledger, broken_out)
        checks.append(("EXPORT REFUSES BROKEN CHAIN",
                       ok_broken is False and not os.path.exists(broken_out), reason_broken))

        # (3) independent verify of the good snapshot
        ok_v, reason_v, details_v = verify_snapshot_file(snap_path)
        verified_line = f"VERIFIED — {reason_v}" if ok_v else f"FAILED — {reason_v}"
        checks.append(("INDEPENDENT VERIFY (good snapshot)", ok_v is True, reason_v))

        # (4) verify detects a tampered ENTRY
        tampered_entry_snap = os.path.join(tmp, "snapshot_tampered_entry.json")
        snap = json.load(open(snap_path))
        snap["entries"][1]["payload"]["verdict"] = "TAMPERED-PASS"
        json.dump(snap, open(tampered_entry_snap, "w"), indent=2, sort_keys=True)
        ok_te, reason_te, _ = verify_snapshot_file(tampered_entry_snap)
        checks.append(("VERIFY DETECTS TAMPERED ENTRY", ok_te is False, reason_te))

        # (5) verify detects a tampered header tip_hash
        tampered_hdr_snap = os.path.join(tmp, "snapshot_tampered_header.json")
        snap2 = json.load(open(snap_path))
        snap2["snapshot_header"]["tip_hash"] = "0" * 64
        json.dump(snap2, open(tampered_hdr_snap, "w"), indent=2, sort_keys=True)
        ok_th, reason_th, _ = verify_snapshot_file(tampered_hdr_snap)
        checks.append(("VERIFY DETECTS TAMPERED HEADER tip_hash", ok_th is False, reason_th))

        # (6) anchor is tiny: tip fields only, no entries
        anchor_path = os.path.join(tmp, "anchor.json")
        ok_a, anchor = write_anchor(ledger_path, anchor_path)
        sample_anchor = anchor if ok_a else None
        anchor_keys_ok = (
            ok_a is True
            and "entries" not in anchor
            and set(anchor.keys()) == {
                "format", "anchor_version", "tip_index", "tip_hash", "entry_count",
                "anchor_timestamp", "stage", "zero_value", "no_token", "research_only", "note",
            }
        )
        checks.append(("ANCHOR (tip-only, no entries)", anchor_keys_ok,
                       f"keys={sorted(anchor.keys()) if ok_a else anchor}"))

        # (7) auditor cross-check: snapshot tip == anchor tip
        cross_ok = (
            ok_exp and ok_a
            and header["tip_hash"] == anchor["tip_hash"]
            and header["tip_index"] == anchor["tip_index"]
            and header["entry_count"] == anchor["entry_count"]
        )
        checks.append(("SNAPSHOT TIP == ANCHOR TIP (auditor cross-check)", cross_ok,
                       f"snap tip={header['tip_hash'][:16]}.. anchor tip={anchor['tip_hash'][:16]}.."))

        # --- report ---
        print("=== protocol/audit.py self-test — export / independent-verify / tip-anchor ===")
        print("Research-stage, zero-value, no token. Temp ledger only; the REAL ledger and the")
        print("default anchor path are NOT touched. Publishing a snapshot + committing the tip")
        print("anchor is what makes the ledger independently auditable by anyone.\n")

        print("--- sample exported snapshot HEADER (honest labels + tip_hash) ---")
        print(json.dumps(sample_header, indent=2, sort_keys=True))
        print()
        print("--- sample tip ANCHOR file (tiny — tip only, NOT ledger contents) ---")
        print(json.dumps(sample_anchor, indent=2, sort_keys=True))
        print()
        print(f"--- independent --verify result ---\n{verified_line}\n")

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

    # Confirm the self-test CREATED no stray default anchor / snapshot in protocol/
    # (existence-delta: a legitimately committed anchor or pre-existing snapshot is allowed).
    stray_anchor = os.path.exists(DEFAULT_ANCHOR_PATH) and not _anchor_existed_before
    _snaps_after = {n for n in os.listdir(_proto_dir) if "snapshot" in n.lower()}
    stray_snaps = sorted(_snaps_after - _snaps_before)
    print(f"\nstray default anchor file ({DEFAULT_ANCHOR_PATH}): "
          f"{'CREATED BY SELF-TEST (!!)' if stray_anchor else 'none created (good)'}")
    print(f"stray snapshot files created in protocol/: {stray_snaps if stray_snaps else 'none (good)'}")
    print("(the real ledger is untouched — the self-test uses temp paths only)")

    ok_overall = all(passed for _l, passed, _d in checks) and not stray_anchor and not stray_snaps
    return 0 if ok_overall else 1


if __name__ == "__main__":
    sys.exit(main())
