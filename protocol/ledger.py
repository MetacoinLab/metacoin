# Copyright (c) 2023-2026 MetaCoin-Lab.
# Licensed under SML-1.0 — see LICENSE.md.
"""ledger.py — research-stage append-only, hash-chained, verifiable event ledger.

============================ SAFETY / SCOPE (READ ME) ============================
Research-stage protocol component, NOT mainnet. This records ZERO-VALUE protocol
events only (verification results, test earn/spend events, task proof hashes, future
attestation records). It is NOT a financial ledger: there are no transferable balances,
no real token, no money, no wallet, no networking, no payments, and no public deployment.
"earn"/"spend" entries are zero-value testnet bookkeeping (MIP-0001 paragraph 3, MIP-0002
paragraph 8). Not financial, investment, or legal advice. Standard library only.
=================================================================================

What it provides:
  * API-append-only: entries are added ONLY through append(); there is no edit or delete
    API. (External file tampering is a separate threat, handled by verify_chain().)
  * Hash-chained: every entry commits to the previous entry's hash, so the entries form a
    tamper-evident chain anchored at a fixed genesis.
  * Independently verifiable: anyone holding the ledger file can recompute the entire chain
    from scratch with verify_chain() — it recomputes every hash from content and never
    trusts the stored hash.

Storage: JSON Lines (one JSON object per line) at a configurable path. Default
protocol/ledger_data.jsonl. The first append to a nonexistent/empty file creates the
genesis entry at index 0.

Entry schema (exactly these five keys):
    {"index": int, "timestamp": float, "payload": dict, "prev_hash": str, "hash": str}

Hashing:
  * SHA-256 over the canonical JSON of the entry content EXCLUDING the "hash" field, i.e.
    over {"index", "timestamp", "payload", "prev_hash"} with sorted keys and compact
    separators. Because prev_hash is part of the hashed content, any change to a past entry
    (or the chain linkage) changes that entry's hash and is detected on recomputation.
  * Genesis prev_hash is exactly 64 zeros ("0"*64) — a fixed, documented anchor meaning
    "no predecessor".

HONEST LIMITATION: a standalone hash chain detects external mutation, deletion, insertion,
reordering, and hash modification (everything verify_chain() checks). It does NOT by itself
stop an attacker who rewrites the ENTIRE suffix of the chain (re-indexing and re-hashing
every later entry) — that produces an internally consistent chain. Preventing that requires
binding the tip to an external anchor (a signature or a published checkpoint hash) — which
protocol/audit.py now provides (the committed tip anchor protocol/ledger_anchor.json plus
published snapshots, cross-checked by agent_verifier.py); it remains out of scope for THIS
component, which stays a pure hash-linked append log.
"""

import hashlib
import json
import os
import time

# Genesis predecessor anchor: 64 hex zeros means "no previous entry".
GENESIS_PREV_HASH = "0" * 64

# The exact set of top-level keys every entry must have.
_ENTRY_KEYS = frozenset(("index", "timestamp", "payload", "prev_hash", "hash"))

# Default ledger path: protocol/ledger_data.jsonl, resolved next to this file so it is
# independent of the current working directory.
DEFAULT_LEDGER_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "ledger_data.jsonl"
)


def _canonical(obj) -> str:
    """Canonical JSON: sorted keys, compact separators, ASCII — byte-stable for hashing."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _content_for_hash(entry: dict) -> dict:
    """The hashed content of an entry: everything EXCEPT the 'hash' field itself."""
    return {
        "index": entry["index"],
        "timestamp": entry["timestamp"],
        "payload": entry["payload"],
        "prev_hash": entry["prev_hash"],
    }


def _compute_hash(content: dict) -> str:
    """SHA-256 hex digest of the canonical JSON of the (hash-excluded) entry content."""
    return hashlib.sha256(_canonical(content).encode("utf-8")).hexdigest()


class Ledger:
    """An append-only, hash-chained event ledger backed by a JSON Lines file."""

    def __init__(self, path: str = DEFAULT_LEDGER_PATH):
        self.path = path

    # --- write API ----------------------------------------------------------
    def append(self, payload: dict) -> dict:
        """Append a new entry committing to the current tip, and return the stored entry.

        The first append to a missing/empty ledger creates the genesis entry (index 0,
        prev_hash = 64 zeros). Subsequent entries take index = tip.index + 1 and
        prev_hash = tip.hash. Payload must be a JSON object (dict).
        """
        if not isinstance(payload, dict):
            raise TypeError("payload must be a dict (a JSON object)")

        entries = self.read_all()
        if not entries:
            index = 0
            prev_hash = GENESIS_PREV_HASH
        else:
            tip = entries[-1]
            index = tip["index"] + 1
            prev_hash = tip["hash"]

        content = {
            "index": index,
            "timestamp": time.time(),
            "payload": payload,
            "prev_hash": prev_hash,
        }
        entry = dict(content)
        entry["hash"] = _compute_hash(content)

        # Append exactly one canonical-JSON line; flush + fsync for durability.
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(_canonical(entry) + "\n")
            f.flush()
            os.fsync(f.fileno())
        return entry

    # --- read API -----------------------------------------------------------
    def read_all(self) -> list:
        """Return all entries as parsed dicts (raises on malformed JSON — see verify_chain
        for graceful, reason-returning validation)."""
        if not os.path.exists(self.path):
            return []
        entries = []
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    entries.append(json.loads(line))
        return entries

    def tip(self):
        """Return the last entry, or None if the ledger is empty."""
        if not os.path.exists(self.path):
            return None
        last = None
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    last = line
        return json.loads(last) if last is not None else None

    # --- verification -------------------------------------------------------
    def verify_chain(self):
        """Recompute and validate the entire chain from scratch.

        Returns (ok, reason). Checks, in order, for each line/entry:
          * line parses as JSON and is a JSON object (else: malformed)
          * entry has exactly the five schema keys
          * index/payload/prev_hash/hash have the right types (payload must be a dict)
          * index equals its position (starts at 0, sequential, no gaps/reorder/insert)
          * stored hash equals the hash recomputed from content (no payload/hash mutation)
          * genesis prev_hash is exactly 64 zeros; every other prev_hash equals the actual
            hash of the previous entry (no broken/forged linkage)
        """
        if not os.path.exists(self.path):
            return (True, "ok: no ledger file (vacuously valid, empty chain)")

        # Read raw non-empty lines with their 1-based file line numbers.
        raw = []
        with open(self.path, "r", encoding="utf-8") as f:
            for lineno, line in enumerate(f, start=1):
                if line.strip():
                    raw.append((lineno, line.strip()))

        if not raw:
            return (True, "ok: empty ledger (vacuously valid)")

        prev_hash_actual = None
        for position, (lineno, text) in enumerate(raw):
            # 1) parse
            try:
                entry = json.loads(text)
            except json.JSONDecodeError as exc:
                return (False, f"line {lineno}: malformed JSON line ({exc})")
            if not isinstance(entry, dict):
                return (False, f"line {lineno}: entry is not a JSON object")

            # 2) exact schema keys
            keys = set(entry.keys())
            if keys != set(_ENTRY_KEYS):
                missing = sorted(set(_ENTRY_KEYS) - keys)
                extra = sorted(keys - set(_ENTRY_KEYS))
                return (
                    False,
                    f"line {lineno}: entry has wrong fields "
                    f"(missing={missing}, unexpected={extra})",
                )

            # 3) field types (bool is excluded from int; payload must be a dict)
            if not isinstance(entry["index"], int) or isinstance(entry["index"], bool):
                return (False, f"line {lineno}: 'index' is not an integer")
            if not isinstance(entry["payload"], dict):
                return (False, f"line {lineno}: 'payload' is not a JSON object")
            if not isinstance(entry["prev_hash"], str) or not isinstance(entry["hash"], str):
                return (False, f"line {lineno}: 'prev_hash'/'hash' must be strings")

            # 4) index sequencing (catches deletion gaps, reordering, insertion shifts)
            if entry["index"] != position:
                return (
                    False,
                    f"index out of sequence at position {position}: found index "
                    f"{entry['index']} (expected {position}) — gap/reorder/insert",
                )

            # 5) recompute hash from content; never trust the stored hash
            recomputed = _compute_hash(_content_for_hash(entry))
            if recomputed != entry["hash"]:
                return (
                    False,
                    f"index {entry['index']}: stored hash does not match recomputed hash "
                    f"(content was altered)",
                )

            # 6) chain linkage
            if position == 0:
                if entry["prev_hash"] != GENESIS_PREV_HASH:
                    return (
                        False,
                        f"index 0: genesis prev_hash is not 64 zeros "
                        f"(found {entry['prev_hash'][:12]}...)",
                    )
            else:
                if entry["prev_hash"] != prev_hash_actual:
                    return (
                        False,
                        f"index {entry['index']}: prev_hash does not match the previous "
                        f"entry's hash (broken/forged chain link)",
                    )

            prev_hash_actual = entry["hash"]

        return (True, f"ok: {len(raw)} entries verified from genesis")


# ============================== SELF-TEST ====================================

def _selftest() -> int:
    """Honest-append + tamper-detection self-test. Uses a temp file, never the default."""
    import shutil
    import tempfile

    def read_raw(path):
        with open(path, "r", encoding="utf-8") as f:
            return [ln.rstrip("\n") for ln in f if ln.strip()]

    def write_raw(path, lines):
        with open(path, "w", encoding="utf-8") as f:
            for ln in lines:
                f.write(ln + "\n")

    tmpdir = tempfile.mkdtemp(prefix="ledger_selftest_")
    ok_overall = True
    try:
        honest = os.path.join(tmpdir, "ledger.jsonl")
        led = Ledger(honest)

        # --- honest appends: genesis, proof_record, earn_test_meta, spend_test_meta -----
        led.append({"event": "genesis", "note": "research-stage chain start; zero-value"})
        led.append({
            "event": "proof_record",
            "task_id": "task-0002-orbit-propagation",
            "output_hash": "ff03231fc53a0505f648e2ec24e251ced41c896fbfd2553ffaa914ffd5ba300c",
            "verdict": "PASS",
        })
        led.append({
            "event": "earn_test_meta",
            "address": "agent-testnet",
            "amount": 2,
            "note": "verified work credit (zero-value Test-META)",
        })
        led.append({
            "event": "spend_test_meta",
            "address": "agent-testnet",
            "amount": 1,
            "note": "buy simulated compute (zero-value Test-META)",
        })

        honest_ok, honest_reason = led.verify_chain()

        # --- attacks (each on a fresh copy of the honest ledger) ------------------------
        def fresh(name):
            p = os.path.join(tmpdir, name)
            shutil.copy(honest, p)
            return p

        results = []  # (label, detected_bool, reason)

        # 1) mutate payload in a past entry, leave its stored hash unchanged
        p = fresh("attack_mutate_payload.jsonl")
        lines = read_raw(p)
        e = json.loads(lines[1])
        e["payload"]["verdict"] = "TAMPERED-PASS"
        lines[1] = _canonical(e)
        write_raw(p, lines)
        ok, reason = Ledger(p).verify_chain()
        results.append(("MUTATE PAYLOAD", not ok, reason))

        # 2) mutate the stored hash of a past entry
        p = fresh("attack_mutate_hash.jsonl")
        lines = read_raw(p)
        e = json.loads(lines[1])
        e["hash"] = "0" * 64
        lines[1] = _canonical(e)
        write_raw(p, lines)
        ok, reason = Ledger(p).verify_chain()
        results.append(("MUTATE HASH", not ok, reason))

        # 3) break prev_hash link (recompute that entry's hash so it is self-consistent,
        #    isolating the chain-linkage check from the hash-recompute check)
        p = fresh("attack_break_prev.jsonl")
        lines = read_raw(p)
        e = json.loads(lines[2])
        e["prev_hash"] = "f" * 64
        e["hash"] = _compute_hash(_content_for_hash(e))
        lines[2] = _canonical(e)
        write_raw(p, lines)
        ok, reason = Ledger(p).verify_chain()
        results.append(("BREAK PREV_HASH", not ok, reason))

        # 4) delete an entry to create an index gap
        p = fresh("attack_delete.jsonl")
        lines = read_raw(p)
        del lines[2]
        write_raw(p, lines)
        ok, reason = Ledger(p).verify_chain()
        results.append(("DELETE ENTRY", not ok, reason))

        # 5) reorder two entries
        p = fresh("attack_reorder.jsonl")
        lines = read_raw(p)
        lines[1], lines[2] = lines[2], lines[1]
        write_raw(p, lines)
        ok, reason = Ledger(p).verify_chain()
        results.append(("REORDER ENTRY", not ok, reason))

        # 6) insert a forged (internally self-consistent, correctly linked) entry mid-chain
        p = fresh("attack_insert.jsonl")
        lines = read_raw(p)
        e1 = json.loads(lines[1])
        forged = {
            "index": 2,
            "timestamp": time.time(),
            "payload": {"event": "forged_spend", "address": "attacker", "amount": 999},
            "prev_hash": e1["hash"],
        }
        forged["hash"] = _compute_hash(forged)
        lines.insert(2, _canonical(forged))
        write_raw(p, lines)
        ok, reason = Ledger(p).verify_chain()
        results.append(("INSERT FORGED ENTRY", not ok, reason))

        # 7) corrupt one JSON line
        p = fresh("attack_corrupt_json.jsonl")
        lines = read_raw(p)
        lines[2] = lines[2][: len(lines[2]) // 2] + "  <<<corrupt"
        write_raw(p, lines)
        ok, reason = Ledger(p).verify_chain()
        results.append(("CORRUPT JSON", not ok, reason))

        # --- the untouched honest ledger must still verify ------------------------------
        honest_again_ok, honest_again_reason = led.verify_chain()

        # --- report ---------------------------------------------------------------------
        print("=== protocol/ledger.py self-test (append-only hash-chained ledger) ===")
        print("Research-stage, zero-value events only. Temp file; default ledger untouched.\n")

        print("--- ledger entries (honest chain) ---")
        print(f"  {'idx':>3}  {'prev_hash':<14}  {'hash':<14}  payload.event")
        for entry in led.read_all():
            print(
                f"  {entry['index']:>3}  {entry['prev_hash'][:12]+'..':<14}  "
                f"{entry['hash'][:12]+'..':<14}  {entry['payload'].get('event')}"
            )
        print()

        print("--- verification results ---")
        print(f"HONEST VERIFY: {'PASS' if honest_ok else 'FAIL'}")
        for label, detected, _reason in results:
            print(f"{label}: {'DETECTED' if detected else 'MISSED (!!)'}")
        print(f"HONEST STILL VERIFIES: {'PASS' if honest_again_ok else 'FAIL'}")
        print()

        print("--- detection reasons (detail) ---")
        print(f"  HONEST VERIFY            : {honest_reason}")
        for label, _detected, reason in results:
            print(f"  {label:<24} : {reason}")
        print(f"  HONEST STILL VERIFIES    : {honest_again_reason}")
        print()

        # --- overall pass/fail ----------------------------------------------------------
        ok_overall = (
            honest_ok
            and honest_again_ok
            and all(detected for _l, detected, _r in results)
        )
        print("=== self-test summary: " +
              ("ALL CHECKS BEHAVED CORRECTLY" if ok_overall else "FAILURE — see above") +
              " ===")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    return 0 if ok_overall else 1


if __name__ == "__main__":
    import sys

    sys.exit(_selftest())
