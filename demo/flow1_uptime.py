"""flow1_uptime.py — FLOW 1 SIGNED-HEARTBEAT UPTIME EMISSION v0 (schema "uptime-emission/0.1").

================== CONSTITUTIONAL RULES (READ ME) ==================
This is Flow 1's flagship channel — objective base emission from cryptographic
liveness proofs — and it completes the Two-Flow picture in code:

  * EMISSION IS OBJECTIVE ONLY: a slot emits the fixed PER_SLOT_EMISSION iff a
    valid signed heartbeat exists for it — no judgment fields, no discretionary
    adjustment, no partial credit. A missed slot emits EXACTLY 0: an objective
    fact, not a penalty (the emission table carries no penalty or judgment key
    of any kind, asserted by the self-test).
  * EMISSION IS BOUNDED: SLOT_COUNT x PER_SLOT_EMISSION must not exceed
    EPOCH_CAP — checked BEFORE an epoch can run (bounded by construction, not
    by later clamping), and total_emitted is replayed from the emission table,
    never trusted from running state.
  * SEPARATION BOTH WAYS: this module has NO path to the treasury (grep-audited
    in the self-test, mirroring metastar_treasury's no-mint audit). Flow 1
    emission cannot fund judged work; Flow 2 treasury cannot mint. The
    constitutional separation is mechanical in both directions.
  * SIMULATED TIME: slots are deterministic indices, not wall-clock. A
    heartbeat's ledger_tip_at_emit binds it to a chain state — that proves the
    heartbeat was created at-or-after that state (chain-state ORDERING), and
    honestly cannot prove real-world time or continuous liveness between slots.
  * ONE-TIME KEY DISCIPLINE: every heartbeat consumes one Lamport key index,
    and the ledger-wide reuse scan (actor_identity.anchored_key_uses) is
    CROSS-TYPE: an index consumed by a challenge round is consumed here too,
    and vice versa.

================== HONESTY / SCOPE (READ ME) ==================
Research-stage, ZERO-VALUE, no token, no money, no payments. Emitted units are
zero-value Test-META accounting entries over SIMULATED slots, produced by a
same-operator node whose keychain the coordinator itself holds — liveness
evidence, not third-party infrastructure. Slot MISSED_SLOT_DRILL is skipped on
purpose (the planned missed-slot drill: the honest zero, demonstrated).

Deterministic GIVEN the keychain + chain state: Lamport signing is
deterministic once keys exist (keychain generation remains the one random
step). Standard library only. Not financial, investment, or legal advice.

Usage:
    python3 demo/flow1_uptime.py --run-epoch --keychain keychain_uptime.json --out uptime_epoch.json
    python3 demo/flow1_uptime.py --verify-epoch uptime_epoch.json --root <hex>
    python3 demo/flow1_uptime.py --selftest   # temp-only; writes nothing
"""

# Suppress __pycache__/*.pyc so importing protocol modules below leaves no stray files.
import sys
sys.dont_write_bytecode = True

import argparse
import hashlib
import json
import os

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# REUSE existing components — the identity layer signs, the ledger binds.
import protocol.actor_identity as actor_identity
import protocol.work_molecule as work_molecule  # source-agnostic ledger reading

SCHEMA_VERSION = "uptime-emission/0.1"
SLOT_COUNT = 10
PER_SLOT_EMISSION = 0.5
EPOCH_CAP = 5.0
MISSED_SLOT_DRILL = 6  # 0-indexed: the planned missed-slot (honest-zero) drill
ACTOR_ID = "spark-uptime-node"
DEFAULT_LEDGER_PATH = os.path.join(_REPO_ROOT, "protocol", "ledger_data.jsonl")


def canonical_json(obj) -> str:
    """Canonical JSON: sorted keys, compact separators, ASCII — byte-stable."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def compute_epoch_hash(epoch: dict) -> str:
    """SHA-256 hex of the canonical JSON WITHOUT the epoch_hash field (the
    standard anti-circularity pattern)."""
    content = {k: v for k, v in epoch.items() if k != "epoch_hash"}
    return hashlib.sha256(canonical_json(content).encode("utf-8")).hexdigest()


# ----------------------------------------------------------------------------
# Heartbeats
# ----------------------------------------------------------------------------
def emit_heartbeat(keychain: dict, slot_index: int,
                   ledger_path: str = DEFAULT_LEDGER_PATH) -> dict:
    """Emit a signed heartbeat for `slot_index`, consuming one Lamport key.

    ledger_tip_at_emit is the LIVENESS BINDING: it proves the heartbeat was
    created at-or-after that chain state. HONEST LIMIT: that is chain-state
    ORDERING only — it cannot prove real-world time, nor liveness between
    slots; slots are simulated indices.
    """
    entries = work_molecule._read_ledger(ledger_path)
    tip = entries[-1]
    # first index unused BOTH locally and on-chain (ledger-wide cross-type
    # scan, root-attributed) — raises the named EXHAUSTION reason directing
    # to rotation when the root has no keys left
    key_index = actor_identity.first_unused_index(keychain, entries)
    heartbeat = {
        "schema": SCHEMA_VERSION,
        "actor_id": keychain["actor_id"],
        "slot_index": slot_index,
        "key_index": key_index,
        "ledger_tip_at_emit": {"index": tip["index"], "hash": tip["hash"]},
    }
    message = canonical_json(heartbeat).encode("utf-8")
    heartbeat["signature"] = actor_identity.sign(keychain, key_index, message,
                                                 ledger_source=entries)
    return heartbeat


def verify_heartbeat(heartbeat, expected_root: str,
                     ledger_path: str = DEFAULT_LEDGER_PATH,
                     as_of_index: int = None):
    """Verify one heartbeat: structure, tip binding, Lamport signature under
    `expected_root`, and the LEDGER-WIDE cross-type one-time discipline.
    Deterministic; returns (ok, reasons). `as_of_index` bounds the reuse scan
    for historical re-verification (an epoch anchored at index N re-verifies
    with N-1, so it does not collide with its own anchored key_indices)."""
    reasons = []
    if not isinstance(heartbeat, dict):
        return (False, ["heartbeat is not a JSON object"])
    for key in ("schema", "actor_id", "slot_index", "key_index",
                "ledger_tip_at_emit", "signature"):
        if key not in heartbeat:
            reasons.append(f"missing field {key}")
    if reasons:
        return (False, reasons)
    if heartbeat["schema"] != SCHEMA_VERSION:
        reasons.append(f"schema must be {SCHEMA_VERSION!r}")
    sig = heartbeat["signature"]
    if not isinstance(sig, dict):
        return (False, reasons + ["signature is not a JSON object"])
    if sig.get("actor_id") != heartbeat["actor_id"]:
        reasons.append(f"signature actor_id {sig.get('actor_id')!r} does not "
                       f"match heartbeat actor_id {heartbeat['actor_id']!r}")
    if sig.get("key_index") != heartbeat["key_index"]:
        reasons.append("signature key_index does not match heartbeat key_index")

    entries = work_molecule._read_ledger(ledger_path)
    if as_of_index is not None:
        entries = [e for e in entries
                   if isinstance(e.get("index"), int)
                   and e["index"] <= as_of_index]

    # liveness binding: the cited tip must be a real, hash-re-deriving entry
    tip = heartbeat["ledger_tip_at_emit"]
    by_index = {e.get("index"): e for e in entries if isinstance(e, dict)}
    cited = by_index.get(tip.get("index"))
    if cited is None or cited.get("hash") != tip.get("hash"):
        reasons.append(f"ledger_tip_at_emit does not name a real chain entry "
                       f"(index {tip.get('index')})")
    elif work_molecule._ledger_entry_hash(cited) != cited.get("hash"):
        reasons.append(f"tip entry {tip.get('index')} hash does not re-derive")

    # the Lamport signature, over the canonical heartbeat WITHOUT its signature
    message = canonical_json(
        {k: v for k, v in heartbeat.items() if k != "signature"}).encode("utf-8")
    ok, sig_reasons = actor_identity.verify_signature(sig, expected_root, message)
    if not ok:
        reasons.extend(f"signature: {r}" for r in sig_reasons)

    # LEDGER-WIDE cross-type one-time discipline, ATTRIBUTED to the root the
    # heartbeat is checked under (uses_for_root): rotation honestly restarts
    # index numbering, while pre-rotation uses of THIS root always count
    for use in actor_identity.uses_for_root(heartbeat["actor_id"], entries,
                                            expected_root):
        if use["key_index"] == heartbeat["key_index"]:
            reasons.insert(0, (
                f"one-time key index reuse (violation of OTS discipline): "
                f"({heartbeat['actor_id']!r}, index {heartbeat['key_index']}) "
                f"was already used in the anchored record at ledger index "
                f"{use['ledger_index']}"))
            break

    return (not reasons, reasons)


# ----------------------------------------------------------------------------
# The epoch: objective, bounded emission over simulated slots
# ----------------------------------------------------------------------------
def run_epoch(keychain: dict, ledger_path: str = DEFAULT_LEDGER_PATH,
              slot_count: int = SLOT_COUNT,
              per_slot_emission: float = PER_SLOT_EMISSION,
              epoch_cap: float = EPOCH_CAP,
              missed_slots=(MISSED_SLOT_DRILL,)) -> dict:
    """Run one simulated uptime epoch: emit + verify a heartbeat per slot
    (skipping `missed_slots` — the planned honest-zero drill) and compute the
    OBJECTIVE emission table. BOUNDED BY CONSTRUCTION: refuses to run at all if
    slot_count x per_slot_emission could exceed epoch_cap."""
    if round(slot_count * per_slot_emission, 6) > epoch_cap:
        raise ValueError(
            f"refused: slot_count {slot_count} x per_slot_emission "
            f"{per_slot_emission} = {round(slot_count * per_slot_emission, 6)} "
            f"could exceed epoch_cap {epoch_cap} — emission is bounded by "
            "construction, never clamped after the fact")
    heartbeats = []
    table = []
    for slot in range(slot_count):
        if slot in missed_slots:
            table.append({"slot": slot, "heartbeat_present": False,
                          "verified": False, "emitted": 0.0})
            continue
        hb = emit_heartbeat(keychain, slot, ledger_path=ledger_path)
        ok, _reasons = verify_heartbeat(hb, keychain["merkle_root"],
                                        ledger_path=ledger_path)
        heartbeats.append(hb)
        table.append({"slot": slot, "heartbeat_present": True,
                      "verified": bool(ok),
                      "emitted": per_slot_emission if ok else 0.0})
    verified_slots = sum(1 for row in table if row["verified"])
    total = round(verified_slots * per_slot_emission, 6)
    assert total == round(sum(row["emitted"] for row in table), 6), \
        "emission replay mismatch: table and summary disagree"
    assert total <= epoch_cap, "epoch cap violated — impossible by construction"
    epoch = {
        "schema": SCHEMA_VERSION,
        "actor_id": keychain["actor_id"],
        "slot_count": slot_count,
        "per_slot_emission": per_slot_emission,
        "epoch_cap": epoch_cap,
        "simulated_time": True,
        "heartbeats": heartbeats,
        "emission_table": table,
        "summary": {
            "verified_slots": verified_slots,
            "missed_slots": sorted(missed_slots),
            "total_emitted": total,
            "epoch_cap": epoch_cap,
            "cap_respected": total <= epoch_cap,
        },
    }
    epoch["epoch_hash"] = compute_epoch_hash(epoch)
    return epoch


def verify_epoch(epoch, expected_root: str,
                 ledger_path: str = DEFAULT_LEDGER_PATH,
                 as_of_index: int = None):
    """Fully re-verify an epoch: hash integrity, every heartbeat signature,
    and the emission arithmetic replayed from scratch. Returns (ok, reasons)."""
    reasons = []
    if not isinstance(epoch, dict) or epoch.get("schema") != SCHEMA_VERSION:
        return (False, [f"schema must be {SCHEMA_VERSION!r}"])
    if compute_epoch_hash(epoch) != epoch.get("epoch_hash"):
        return (False, ["epoch_hash does not recompute from content"])
    if round(epoch["slot_count"] * epoch["per_slot_emission"], 6) > \
            epoch["epoch_cap"]:
        reasons.append("config could exceed the epoch cap — invalid epoch")
    by_slot = {hb.get("slot_index"): hb for hb in epoch.get("heartbeats", [])}
    total = 0.0
    verified_slots = 0
    for row in epoch.get("emission_table", []):
        slot = row["slot"]
        hb = by_slot.get(slot)
        if hb is None:
            if row["heartbeat_present"] or row["verified"] or row["emitted"]:
                reasons.append(f"slot {slot}: no heartbeat but the table claims "
                               "presence/emission — the objective rule is "
                               "no heartbeat, no emission")
            continue
        ok, hb_reasons = verify_heartbeat(hb, expected_root,
                                          ledger_path=ledger_path,
                                          as_of_index=as_of_index)
        if ok != row["verified"]:
            reasons.append(f"slot {slot}: verification ({ok}) contradicts the "
                           f"table ({row['verified']})"
                           + (f" — {hb_reasons[0]}" if hb_reasons else ""))
        expected_emit = epoch["per_slot_emission"] if ok else 0.0
        if row["emitted"] != expected_emit:
            reasons.append(f"slot {slot}: emitted {row['emitted']} violates the "
                           f"objective rule (expected {expected_emit})")
        if ok:
            verified_slots += 1
            total = round(total + epoch["per_slot_emission"], 6)
    s = epoch.get("summary", {})
    if (s.get("verified_slots") != verified_slots
            or s.get("total_emitted") != total
            or s.get("cap_respected") is not True
            or total > epoch["epoch_cap"]):
        reasons.append(f"summary does not replay: recomputed "
                       f"{verified_slots} slots / {total} emitted vs claimed "
                       f"{s.get('verified_slots')} / {s.get('total_emitted')}")
    return (not reasons, reasons)


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="flow1_uptime.py",
        description=(
            "Flow 1 signed-heartbeat uptime emission v0 (research-stage, "
            "ZERO-VALUE, no token): objective per-slot emission from Lamport-"
            "signed liveness proofs over SIMULATED slots."
        ),
        epilog=(
            "CONSTITUTIONAL: emission is objective (no judgment, missed slot = "
            "honest zero), bounded by construction, and has NO treasury path — "
            "the two flows are mechanically separated in both directions. Not "
            "payment, not a token."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--run-epoch", action="store_true",
                      help="emit + verify the epoch's heartbeats (--keychain)")
    mode.add_argument("--verify-epoch", metavar="EPOCH_JSON",
                      help="fully re-verify an epoch file against --root")
    mode.add_argument("--selftest", action="store_true",
                      help="run the mechanical self-test (temp files only)")
    parser.add_argument("--keychain", help="PRIVATE keychain file for --run-epoch "
                                           "(used indices are persisted back)")
    parser.add_argument("--root", help="expected Merkle root for --verify-epoch")
    parser.add_argument("--ledger", default=DEFAULT_LEDGER_PATH,
                        help=f"ledger source (default: {DEFAULT_LEDGER_PATH})")
    parser.add_argument("--out", help="write the epoch JSON here (gitignored)")
    args = parser.parse_args(argv)

    if args.selftest or not (args.run_epoch or args.verify_epoch):
        return _selftest()

    if args.run_epoch:
        if not args.keychain:
            parser.error("--run-epoch requires --keychain")
        with open(args.keychain, "r", encoding="utf-8") as f:
            keychain = json.load(f)
        try:
            epoch = run_epoch(keychain, ledger_path=args.ledger)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        with open(args.keychain, "w", encoding="utf-8") as f:
            json.dump(keychain, f, indent=2, sort_keys=True)  # stateful marks
        text = json.dumps(epoch, indent=2, sort_keys=True)
        if args.out:
            with open(args.out, "w", encoding="utf-8") as f:
                f.write(text + "\n")
            print(f"wrote epoch to {args.out}", file=sys.stderr)
        s = epoch["summary"]
        print(f"epoch: {s['verified_slots']}/{epoch['slot_count']} slots "
              f"verified, missed {s['missed_slots']} (honest zero), "
              f"total emitted {s['total_emitted']} under cap {s['epoch_cap']} "
              f"(zero-value, simulated slots)")
        print(f"epoch_hash: {epoch['epoch_hash']}")
        return 0

    if not args.root:
        parser.error("--verify-epoch requires --root")
    with open(args.verify_epoch, "r", encoding="utf-8") as f:
        epoch = json.load(f)
    ok, reasons = verify_epoch(epoch, args.root, ledger_path=args.ledger)
    print(f"epoch verification: {'PASS' if ok else 'FAIL'}")
    for r in reasons:
        print(f"  - {r}")
    return 0 if ok else 1


# ============================== SELF-TEST ====================================
def _selftest() -> int:
    """Mechanical self-test: the honest epoch, the honest zero, the forged
    heartbeat, the cap-by-construction refusal, the cross-type one-time
    discipline, the no-treasury-path audit, determinism. Temp files only."""
    import copy
    import shutil
    import tempfile

    from protocol.ledger import Ledger

    print("=== demo/flow1_uptime.py self-test (Flow 1: objective emission) ===")
    print("A slot emits iff a valid signed heartbeat exists; a missed slot is an")
    print("honest zero; the cap binds by construction; no treasury path exists.\n")

    demo_dir = os.path.dirname(os.path.abspath(__file__))
    root_before = set(os.listdir(_REPO_ROOT))
    demo_before = set(os.listdir(demo_dir))
    checks = []
    tmp_dir = tempfile.mkdtemp(prefix=f"flow1_selftest_{os.getpid()}_")
    try:
        fixture_ledger = os.path.join(tmp_dir, "ledger_fixture.jsonl")
        led = Ledger(fixture_ledger)
        led.append({"event": "ledger_genesis", "note": "selftest fixture",
                    "stage": "R-selftest", "zero_value": True, "no_token": True})

        # deterministic fixture keychain (16 keys covers the epoch's 9)
        def _fixture_privates(key_count, tag):
            return [[[hashlib.sha256(f"{tag}:{k}:{i}:0".encode()).hexdigest(),
                      hashlib.sha256(f"{tag}:{k}:{i}:1".encode()).hexdigest()]
                     for i in range(256)]
                    for k in range(key_count)]
        kc = actor_identity.build_keychain_from_privates(
            ACTOR_ID, _fixture_privates(16, "flow1"))

        # [1] the honest epoch: 9/10 verified, 4.5 emitted, cap respected
        e1 = run_epoch(copy.deepcopy(kc), ledger_path=fixture_ledger)
        s = e1["summary"]
        checks.append(("honest epoch: 9/10 slots verified, total 4.5 under "
                       "cap 5.0",
                       s["verified_slots"] == 9 and s["missed_slots"] == [6]
                       and s["total_emitted"] == 4.5
                       and s["cap_respected"] is True))
        ok, reasons = verify_epoch(e1, kc["merkle_root"],
                                   ledger_path=fixture_ledger)
        checks.append(("epoch fully re-verifies (hash + 9 signatures + "
                       "arithmetic)", ok))
        if not ok:
            for r in reasons:
                print(f"    unexpected: {r}")

        # [2] the missed slot is an OBJECTIVE zero: emitted exactly 0, and no
        # penalty/judgment field exists anywhere in the epoch
        row6 = e1["emission_table"][6]
        epoch_text = canonical_json(e1).lower()
        checks.append(("missed slot 6 emits exactly 0 with no penalty or "
                       "judgment field anywhere",
                       row6 == {"slot": 6, "heartbeat_present": False,
                                "verified": False, "emitted": 0.0}
                       and "penalt" not in epoch_text
                       and "judg" not in epoch_text
                       and "score" not in epoch_text))

        # [3] FORGED heartbeat (tampered signature) -> not verified, named reason
        forged_kc = copy.deepcopy(kc)
        hb = emit_heartbeat(forged_kc, 0, ledger_path=fixture_ledger)
        hb["signature"]["revealed_secrets"][5] = "0" * 64
        ok, reasons = verify_heartbeat(hb, kc["merkle_root"],
                                       ledger_path=fixture_ledger)
        checks.append(("forged heartbeat rejected with a named signature reason",
                       not ok and any("per-bit" in r for r in reasons)))

        # [4] cap violation: a config that COULD exceed the cap refuses to run
        try:
            run_epoch(copy.deepcopy(kc), ledger_path=fixture_ledger,
                      slot_count=12)
            checks.append(("over-cap config refuses to run (bounded by "
                           "construction)", False))
        except ValueError as exc:
            checks.append(("over-cap config refuses to run (bounded by "
                           "construction)", "bounded by construction"
                           in str(exc)))

        # [5] CROSS-TYPE one-time discipline: an index consumed by an anchored
        # SIGNED CHALLENGE record is (a) SKIPPED by honest emission — the
        # ledger-wide scan feeds index selection — and (b) rejected at
        # verification when a heartbeat is force-constructed over it anyway
        led.append({"event": "challenge_response_result", "signed": True,
                    "signer_actor_id": ACTOR_ID, "key_index": 0,
                    "status": "challenge-verified", "challenge_id": "x" * 64,
                    "zero_value": True, "no_token": True})
        hb_honest = emit_heartbeat(copy.deepcopy(kc), 0,
                                   ledger_path=fixture_ledger)
        checks.append(("honest emission SKIPS the on-chain-consumed index "
                       "(ledger-wide scan feeds selection)",
                       hb_honest["key_index"] == 1))
        entries_now = work_molecule._read_ledger(fixture_ledger)
        tip_now = entries_now[-1]
        hb0 = {"schema": SCHEMA_VERSION, "actor_id": ACTOR_ID,
               "slot_index": 0, "key_index": 0,
               "ledger_tip_at_emit": {"index": tip_now["index"],
                                      "hash": tip_now["hash"]}}
        hb0["signature"] = actor_identity.sign(
            copy.deepcopy(kc), 0, canonical_json(hb0).encode("utf-8"),
            force_reuse=True)
        ok, reasons = verify_heartbeat(hb0, kc["merkle_root"],
                                       ledger_path=fixture_ledger)
        checks.append(("CROSS-TYPE reuse rejected (challenge round then "
                       "heartbeat, same index)",
                       not ok and "one-time key index reuse" in reasons[0]))
        # ...and the reverse direction: an anchored epoch's key_indices block a
        # later challenge signature (proven via the shared cross-type scan)
        led.append({"event": "uptime_epoch_anchored", "actor_id": ACTOR_ID,
                    "key_indices": [3], "status": "uptime-epoch-confirmed",
                    "zero_value": True, "no_token": True})
        uses = actor_identity.anchored_key_uses(led.read_all())
        checks.append(("anchored epoch key_indices feed the same ledger-wide "
                       "scan (reverse direction)",
                       any(u["actor_id"] == ACTOR_ID and u["key_index"] == 3
                           for u in uses)))

        # [6] NO-TREASURY-PATH audit (mirror of the treasury's no-mint audit):
        # this module never references the treasury, and the treasury never
        # references this module — separation is mechanical in both directions
        flow1_src = open(os.path.abspath(__file__), encoding="utf-8").read()
        treasury_path = os.path.join(demo_dir, "metastar_treasury.py")
        treasury_src = open(treasury_path, encoding="utf-8").read()
        needle_t = "treas" + "ury"
        needle_f = "flow" + "1"
        code_only = "\n".join(  # audit real import statements only, not this
            ln for ln in flow1_src.split("\n")  # audit's own message strings
            if ln.strip().startswith(("import ", "from ")))
        checks.append(("separation audit: flow1 imports no treasury; treasury "
                       "references no flow1",
                       needle_t not in code_only
                       and needle_f not in treasury_src))

        # [7] determinism: two epochs from the same keychain byte-identical
        e2 = run_epoch(copy.deepcopy(kc), ledger_path=fixture_ledger)
        e3 = run_epoch(copy.deepcopy(kc), ledger_path=fixture_ledger)
        checks.append(("two epochs from the same keychain byte-identical",
                       canonical_json(e2) == canonical_json(e3)))

        # [8] emission replay: table sums to the summary exactly
        checks.append(("emission replay: table total == summary total",
                       round(sum(r["emitted"] for r in e1["emission_table"]), 6)
                       == e1["summary"]["total_emitted"]))
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    stray_root = sorted(set(os.listdir(_REPO_ROOT)) - root_before)
    stray_demo = sorted(set(os.listdir(demo_dir)) - demo_before)
    checks.append(("no stray files in repo root", not stray_root))
    checks.append(("no stray files in demo/", not stray_demo))

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
