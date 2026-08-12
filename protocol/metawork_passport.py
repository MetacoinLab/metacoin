# Copyright (c) 2023-2026 MetaCoin-Lab.
# Licensed under SML-1.0 — see LICENSE.md.
"""metawork_passport.py — MetaWork PASSPORT v0 (schema "metawork-passport/0.1").

================== CONSTITUTIONAL RULES (READ ME) ==================
A passport is a HISTORY, not a leaderboard:

  * NO ranking exists: no rank/score/rating/leaderboard/percentile key may
    appear anywhere in a passport — enforced MECHANICALLY at construction (the
    builder refuses to emit one) and at validation (a smuggled key is
    rejected), the same idiom as the trust vector's no-combined-scalar rule.
    Passports of different actors are never compared by this module.
  * Useful-Work-per-Watt is TRANSPARENCY ONLY, never a minting trigger: the
    money modules (demo/flow1_uptime.py emission, demo/metastar_treasury.py
    treasury) neither import nor read passport artifacts — grep-audited in the
    self-test, and the anchored catalog record states it. A metric that money
    paths cannot mechanically see cannot become a gameable printer.
  * ANCHORED RECORDS ONLY: every passport field cites "ledger:N" evidence;
    nothing is asserted that a stranger cannot re-derive from the published
    snapshot. Missing states are honest ("none-registered", "not-computable"),
    and drill events are labeled drills — planned demonstrations, NOT
    misconduct.

================== HONESTY / SCOPE (READ ME) ==================
Research-stage, ZERO-VALUE, no token. Every actor in the current history is
SAME-OPERATOR (pseudonymous device/role handles of one operator — the anchored
ACI baseline quantifies this); a passport proves deterministic re-derivability
of a contribution HISTORY, not identity and not merit. Useful-Work-per-Watt is
an illustrative figure at demo scale (microjoule estimated energies) whose
energy input is ESTIMATED, never measured. Deterministic: timestamps are copied
only; two builds are byte-identical; generation-lock via as_of_index.

Standard library only. Not legal, financial, investment advice.

Usage:
    python3 protocol/metawork_passport.py --actor spark-agent-same-operator
    python3 protocol/metawork_passport.py --all --out passport_catalog.json
    python3 protocol/metawork_passport.py --validate passport_catalog.json
    python3 protocol/metawork_passport.py --selftest   # temp-only
"""

# Suppress __pycache__/*.pyc so importing protocol modules below leaves no stray files.
import sys
sys.dont_write_bytecode = True

import argparse
import hashlib
import json
import os
import re

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# REUSE existing components — do NOT reimplement them.
import protocol.actor_identity as actor_identity
import protocol.work_molecule as work_molecule

_PROTO_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_LEDGER_PATH = os.path.join(_PROTO_DIR, "ledger_data.jsonl")

SCHEMA_VERSION = "metawork-passport/0.1"
CATALOG_SCHEMA_VERSION = "metawork-passport-catalog/0.1"

# THE NO-LEADERBOARD RULE, as a mechanical pattern (validated AND enforced at
# construction): a history may never grow a ranking-shaped key.
_FORBIDDEN_KEY_RE = re.compile(r"rank|score|rating|leaderboard|percentile",
                               re.IGNORECASE)

# Statuses that count as a VERIFIED work unit for the UWW transparency metric
# (the definition is stated inside every passport that carries the metric).
_VERIFIED_STATUSES = ("externally-verified", "locally-verified",
                      "agent-result-confirmed", "challenge-verified")

UWW_NOTE = ("transparency metric; illustrative at demo scale (microjoule "
            "energies); never read by any emission or treasury path")

_HEX = set("0123456789abcdef")


def _sign_safe_zero(obj):
    """Normalize -0.0 -> 0.0 recursively (THE NEGATIVE-ZERO CANONICAL RULE):
    the sign of a zero is a platform artifact of last-ulp cancellation with
    no semantic content — canonical artifacts are sign-of-zero-free by rule.
    Floats only; ints and bools pass through untouched."""
    if isinstance(obj, float):
        return 0.0 if obj == 0.0 else obj
    if isinstance(obj, dict):
        return {k: _sign_safe_zero(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sign_safe_zero(v) for v in obj]
    return obj


def canonical_json(obj) -> str:
    """Canonical JSON: sorted keys, compact separators, ASCII — byte-stable."""
    return json.dumps(_sign_safe_zero(obj), sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True)


def compute_passport_hash(passport: dict) -> str:
    """SHA-256 hex WITHOUT the passport_hash field (anti-circularity pattern)."""
    content = {k: v for k, v in passport.items() if k != "passport_hash"}
    return hashlib.sha256(canonical_json(content).encode("utf-8")).hexdigest()


def compute_catalog_hash(catalog: dict) -> str:
    """SHA-256 hex WITHOUT the catalog_hash field."""
    content = {k: v for k, v in catalog.items() if k != "catalog_hash"}
    return hashlib.sha256(canonical_json(content).encode("utf-8")).hexdigest()


def leaderboard_violations(obj, prefix="") -> list:
    """Every key path matching the forbidden ranking pattern (empty = the
    mechanical proof that no leaderboard exists)."""
    hits = []
    if isinstance(obj, dict):
        for k in sorted(obj):
            path = f"{prefix}.{k}" if prefix else k
            if _FORBIDDEN_KEY_RE.search(k):
                hits.append(path)
            hits.extend(leaderboard_violations(obj[k], path))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            hits.extend(leaderboard_violations(v, f"{prefix}[{i}]"))
    return hits


def _entries(ledger_path, as_of_index):
    entries = work_molecule._read_ledger(ledger_path)
    if as_of_index is not None:
        entries = [e for e in entries
                   if isinstance(e.get("index"), int)
                   and e["index"] <= as_of_index]
    return entries


# ----------------------------------------------------------------------------
# Actor discovery + passport construction (anchored records only)
# ----------------------------------------------------------------------------
def discover_actors(ledger_path: str = DEFAULT_LEDGER_PATH,
                    as_of_index: int = None) -> list:
    """Every actor id appearing in anchored records (verifier_id, actor_id,
    signer_actor_id, issued_for keys), sorted."""
    actors = set()
    for e in _entries(ledger_path, as_of_index):
        p = e.get("payload") if isinstance(e, dict) else None
        if not isinstance(p, dict):
            continue
        for key in ("verifier_id", "actor_id", "signer_actor_id", "issued_for"):
            v = p.get(key)
            if isinstance(v, str) and v:
                actors.add(v)
    return sorted(actors)


def _resolve_metering(entries):
    """(anchor_index, per_task_energy dict) from the confirmed metering anchor
    plus a hash-matching local report (evidence-bundle discovery); per-task map
    is {} when only the aggregate is resolvable."""
    anchor = None
    for e in entries:
        p = e.get("payload", {})
        if (p.get("event") == "metering_evidence_anchored"
                and p.get("status") == "metering-evidence-confirmed"):
            anchor = e
    if anchor is None:
        return (None, {})
    per_task = {}
    path = work_molecule.find_evidence_file("metering_report.json")
    if path is not None:
        try:
            with open(path, "r", encoding="utf-8") as f:
                report = json.load(f)
        except (json.JSONDecodeError, OSError):
            report = None
        if isinstance(report, dict):
            content = {k: v for k, v in report.items() if k != "report_hash"}
            recomputed = hashlib.sha256(
                canonical_json(content).encode("utf-8")).hexdigest()
            if recomputed == report.get("report_hash") == \
                    anchor["payload"].get("report_hash"):
                per_task = {r["task_id"]: r["energy_j_estimate"]
                            for r in report.get("per_task", [])}
    return (anchor["index"], per_task)


def build_passport(actor_id: str, ledger_path: str = DEFAULT_LEDGER_PATH,
                   as_of_index: int = None) -> dict:
    """Assemble the actor's verifiable contribution HISTORY from anchored
    records only. Deterministic; every field cites ledger evidence; honest
    absent-states; construction refuses any ranking-shaped key."""
    entries = _entries(ledger_path, as_of_index)

    # identity: registered root (or the honest none-registered) + key usage
    registration = None
    for e in entries:
        p = e.get("payload", {})
        if (p.get("event") == "actor_key_registered"
                and p.get("status") == "actor-key-registered"
                and p.get("actor_id") == actor_id):
            registration = e
    if registration is not None:
        identity = {
            "registered_root": registration["payload"]["merkle_root"],
            "scheme": registration["payload"].get("scheme"),
            "key_count": registration["payload"].get("key_count"),
            "evidence": f"ledger:{registration['index']}",
        }
    else:
        identity = {"state": "none-registered",
                    "note": "no key root anchored for this actor — signatures "
                            "cannot bind to it (honest absent-state, not a "
                            "fault)"}
    key_usage = sorted(
        ({"index": u["key_index"], "consumed_in": f"ledger:{u['ledger_index']}"}
         for u in actor_identity.anchored_key_uses(entries)
         if u["actor_id"] == actor_id),
        key=lambda x: (x["index"], x["consumed_in"]))
    identity["key_usage"] = key_usage

    # verification history: every anchored record naming this actor as verifier
    verification_history = []
    uptime_history = []
    for e in entries:
        p = e.get("payload", {})
        if p.get("verifier_id") == actor_id:
            item = {"ledger_index": e["index"], "event": p.get("event"),
                    "outcome": p.get("status")}
            if isinstance(p.get("task_id"), str):
                item["task_ref"] = p["task_id"]
            elif isinstance(p.get("task_ids"), list):
                item["task_ref"] = sorted(p["task_ids"])
            if p.get("drill"):
                item["drill"] = True
            verification_history.append(item)
        if (p.get("actor_id") == actor_id
                and p.get("event") == "uptime_epoch_anchored"):
            uptime_history.append({
                "ledger_index": e["index"], "event": p["event"],
                "outcome": p.get("status"),
                "verified_slots": p.get("verified_slots"),
                "missed_slots": p.get("missed_slots"),
                "total_emitted": p.get("total_emitted"),
                "evidence": f"ledger:{e['index']}",
            })
        if (p.get("actor_id") == actor_id
                and p.get("event") == "heartbeat_rejected"):
            uptime_history.append({
                "ledger_index": e["index"], "event": p["event"],
                "outcome": p.get("status"), "emitted": p.get("emitted"),
                "drill": bool(p.get("drill")),
                "evidence": f"ledger:{e['index']}",
            })
    verification_history.sort(key=lambda x: x["ledger_index"])
    uptime_history.sort(key=lambda x: x["ledger_index"])

    # treasury history: HONESTLY EMPTY in v0 — treasury/Gate-3 records carry no
    # per-actor attribution (the coordinator role is not an actor id); asserted
    # empty rather than invented.
    treasury_history = []

    # energy evidence + Useful-Work-per-Watt (transparency only)
    verified_events = [v for v in verification_history
                       if v.get("outcome") in _VERIFIED_STATUSES]
    task_refs = set()
    for v in verified_events:
        ref = v.get("task_ref")
        if isinstance(ref, str):
            task_refs.add(ref)
        elif isinstance(ref, list):
            task_refs.update(ref)
    metering_idx, per_task_energy = _resolve_metering(entries)
    metered_tasks = sorted(t for t in task_refs if t in per_task_energy)
    if metering_idx is not None and metered_tasks:
        energy = round(sum(per_task_energy[t] for t in metered_tasks), 6)
        energy_evidence = {
            "source": f"ledger:{metering_idx}",
            "metered_task_refs": metered_tasks,
            "energy_j_estimate": energy,
            "labels": {"energy": "estimated"},
        }
        useful_work_per_watt = {
            "verified_work_units": len(verified_events),
            "definition": "distinct anchored verification events by this "
                          "actor with a verified outcome",
            "energy_j_estimate": energy,
            "energy_evidence": f"ledger:{metering_idx}",
            "ratio": round(len(verified_events) / energy, 6) if energy else None,
            "labels": {"energy": "estimated"},
            "note": UWW_NOTE,
        }
        if useful_work_per_watt["ratio"] is None:
            useful_work_per_watt = {"state": "not-computable",
                                    "reason": "zero estimated energy"}
    else:
        energy_evidence = {"state": "not-captured",
                           "reason": "no anchored metering evidence covers "
                                     "this actor's executions"}
        useful_work_per_watt = {"state": "not-computable",
                                "reason": "no energy evidence for this actor "
                                          "(honest absence, not zero)"}

    passport = {
        "schema": SCHEMA_VERSION,
        "actor_id": actor_id,
        "identity": identity,
        "verification_history": verification_history,
        "uptime_history": uptime_history,
        "treasury_history": treasury_history,
        "energy_evidence": energy_evidence,
        "useful_work_per_watt": useful_work_per_watt,
        "scope_and_limitations": {
            "operator_relationship": "same-operator",
            "zero_value": True,
            "no_token": True,
            "stage": "R-passport",
            "note": ("a HISTORY, not a leaderboard: drill events are planned "
                     "demonstrations, not misconduct; treasury/Gate-3 records "
                     "carry no per-actor attribution in v0 (honestly empty); "
                     "same-operator pseudonymous handles, not identities"),
        },
    }
    violations = leaderboard_violations(passport)
    if violations:  # construction can never emit a ranking-shaped key
        raise ValueError(f"no-leaderboard rule violated at: {violations}")
    passport["passport_hash"] = compute_passport_hash(passport)
    return passport


def build_passport_catalog(ledger_path: str = DEFAULT_LEDGER_PATH,
                           actor_ids=None, as_of_index: int = None) -> dict:
    """Build + validate every actor's passport; return the catalog."""
    if actor_ids is None:
        actor_ids = discover_actors(ledger_path, as_of_index=as_of_index)
    entries = []
    for actor in sorted(actor_ids):
        passport = build_passport(actor, ledger_path=ledger_path,
                                  as_of_index=as_of_index)
        ok, reasons = validate_passport(passport, ledger_path=ledger_path)
        if not ok:
            raise ValueError(f"passport for {actor!r} does not validate: "
                             f"{reasons}")
        entries.append({"actor_id": actor,
                        "passport_hash": passport["passport_hash"]})
    catalog = {"schema": CATALOG_SCHEMA_VERSION, "entries": entries}
    catalog["catalog_hash"] = compute_catalog_hash(catalog)
    return catalog


# ----------------------------------------------------------------------------
# Validation (mechanical, no LLM)
# ----------------------------------------------------------------------------
def _collect_citations(obj) -> list:
    """Every 'ledger:N' string anywhere in the passport."""
    cites = []
    if isinstance(obj, dict):
        for v in obj.values():
            cites.extend(_collect_citations(v))
    elif isinstance(obj, list):
        for v in obj:
            cites.extend(_collect_citations(v))
    elif isinstance(obj, str) and obj.startswith("ledger:"):
        cites.append(obj)
    return cites


def validate_passport(passport, ledger_path: str = None):
    """Shape + THE NO-LEADERBOARD RULE + hash recompute + (with a ledger)
    every citation names a real, hash-re-deriving entry. Returns (ok, reasons)."""
    reasons = []
    if not isinstance(passport, dict):
        return (False, ["passport is not a JSON object"])
    expected = {"schema", "actor_id", "identity", "verification_history",
                "uptime_history", "treasury_history", "energy_evidence",
                "useful_work_per_watt", "scope_and_limitations",
                "passport_hash"}
    if set(passport.keys()) != expected:
        return (False, [f"top-level keys must be exactly {sorted(expected)}"])
    if passport["schema"] != SCHEMA_VERSION:
        reasons.append(f"schema must be {SCHEMA_VERSION!r}")
    for v in leaderboard_violations(passport):
        reasons.append(f"no-leaderboard rule violated: ranking-shaped key at "
                       f"{v} — a passport is a history, never a leaderboard")
    if compute_passport_hash(passport) != passport.get("passport_hash"):
        reasons.append("passport_hash does not recompute from content")
    scope = passport["scope_and_limitations"]
    if not (scope.get("zero_value") is True and scope.get("no_token") is True):
        reasons.append("honest labels zero_value/no_token are mandatory")

    if ledger_path is not None and not reasons:
        entries = work_molecule._read_ledger(ledger_path)
        by_index = {e.get("index"): e for e in entries if isinstance(e, dict)}
        for cite in sorted(set(_collect_citations(passport))):
            try:
                idx = int(cite.split(":", 1)[1])
            except ValueError:
                reasons.append(f"malformed citation {cite!r} — citations must "
                               "have the form 'ledger:<int>'")
                continue
            entry = by_index.get(idx)
            if entry is None:
                reasons.append(f"citation {cite} names no chain entry")
            elif work_molecule._ledger_entry_hash(entry) != entry.get("hash"):
                reasons.append(f"cited entry {idx} hash does not re-derive")
    return (not reasons, reasons)


def validate_catalog(catalog):
    """Structural catalog validation. Returns (ok, reasons)."""
    reasons = []
    if not isinstance(catalog, dict) or set(catalog.keys()) != \
            {"schema", "entries", "catalog_hash"}:
        return (False, ["top-level keys must be {schema, entries, catalog_hash}"])
    if catalog["schema"] != CATALOG_SCHEMA_VERSION:
        reasons.append(f"schema must be {CATALOG_SCHEMA_VERSION!r}")
    seen = []
    for i, e in enumerate(catalog["entries"]):
        if not isinstance(e, dict) or set(e.keys()) != \
                {"actor_id", "passport_hash"}:
            reasons.append(f"entries[{i}] must be exactly "
                           "{actor_id, passport_hash}")
            continue
        h = e["passport_hash"]
        if not (isinstance(h, str) and len(h) == 64
                and all(c in _HEX for c in h)):
            reasons.append(f"entries[{i}].passport_hash must be 64-hex")
        seen.append(e["actor_id"])
    if seen != sorted(seen) or len(seen) != len(set(seen)):
        reasons.append("entries must be unique and sorted by actor_id")
    if not reasons and compute_catalog_hash(catalog) != catalog["catalog_hash"]:
        reasons.append("catalog_hash does not recompute from content")
    return (not reasons, reasons)


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="metawork_passport.py",
        description=("MetaWork Passport v0 (research-stage, ZERO-VALUE, no "
                     "token): per-actor verifiable contribution histories from "
                     "anchored records only. A history, never a leaderboard."),
        epilog=("Useful-Work-per-Watt is transparency only — money paths are "
                "mechanically blind to it. Not consensus, not payment, not a "
                "token."),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--actor", help="build one actor's passport")
    mode.add_argument("--all", action="store_true",
                      help="build every discovered actor's passport + catalog")
    mode.add_argument("--validate", metavar="FILE",
                      help="validate a passport or catalog file")
    mode.add_argument("--selftest", action="store_true",
                      help="run the mechanical self-test (temp files only)")
    parser.add_argument("--ledger", default=DEFAULT_LEDGER_PATH,
                        help=f"ledger source (default: {DEFAULT_LEDGER_PATH})")
    parser.add_argument("--as-of-entry", type=int, default=None, metavar="N",
                        help="generation-lock: consider only entries <= N")
    parser.add_argument("--out", help="write the catalog here (gitignored)")
    args = parser.parse_args(argv)

    if args.selftest or not (args.actor or args.all or args.validate):
        return _selftest()

    if args.actor:
        passport = build_passport(args.actor, ledger_path=args.ledger,
                                  as_of_index=args.as_of_entry)
        print(json.dumps(passport, indent=2, sort_keys=True))
        return 0
    if args.all:
        catalog = build_passport_catalog(ledger_path=args.ledger,
                                         as_of_index=args.as_of_entry)
        text = json.dumps(catalog, indent=2, sort_keys=True)
        print(text)
        out = args.out or "passport_catalog.json"
        with open(out, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        print(f"wrote passport catalog ({len(catalog['entries'])} actors) "
              f"to {out}", file=sys.stderr)
        return 0
    with open(args.validate, "r", encoding="utf-8") as f:
        doc = json.load(f)
    if isinstance(doc, dict) and doc.get("schema") == CATALOG_SCHEMA_VERSION:
        ok, reasons = validate_catalog(doc)
    else:
        ok, reasons = validate_passport(doc, ledger_path=args.ledger)
    print(f"validation: {'VALID' if ok else 'INVALID'}")
    for r in reasons:
        print(f"  - {r}")
    return 0 if ok else 1


# ============================== SELF-TEST ====================================
def _selftest() -> int:
    """Mechanical self-test. Temp fixtures + conditional real-ledger checks."""
    import copy
    import shutil
    import tempfile

    from protocol.ledger import Ledger
    import protocol.external_verifier as external_verifier
    import protocol.verifier_cli as verifier_cli

    print("=== protocol/metawork_passport.py self-test (a history, never a "
          "leaderboard) ===\n")

    root_before = set(os.listdir(_REPO_ROOT))
    proto_before = set(os.listdir(_PROTO_DIR))
    checks = []
    tmp_dir = tempfile.mkdtemp(prefix=f"passport_selftest_{os.getpid()}_")
    try:
        fixture_ledger = os.path.join(tmp_dir, "ledger_fixture.jsonl")
        led = Ledger(fixture_ledger)
        led.append({"event": "ledger_genesis", "note": "selftest fixture",
                    "stage": "R-selftest", "zero_value": True, "no_token": True})
        external_verifier.evaluate_submission(
            verifier_cli.build_submission(
                "task-0001", "fixture-verifier",
                topology="same-machine-self-recompute"), led)
        led.append({"event": "actor_key_registered",
                    "status": "actor-key-registered",
                    "actor_id": "fixture-verifier", "merkle_root": "a" * 64,
                    "scheme": "lamport-sha256-merkle/0.1", "key_count": 4,
                    "zero_value": True, "no_token": True})
        led.append({"event": "challenge_response_result",
                    "status": "challenge-failed", "drill": True, "signed": True,
                    "signer_actor_id": "fixture-verifier", "key_index": 0,
                    "verifier_id": "fixture-verifier", "task_id": "task-0001",
                    "challenge_id": "c" * 64, "zero_value": True,
                    "no_token": True})

        # [1] build + validate with citation recheck; determinism
        p1 = build_passport("fixture-verifier", ledger_path=fixture_ledger)
        ok, reasons = validate_passport(p1, ledger_path=fixture_ledger)
        p2 = build_passport("fixture-verifier", ledger_path=fixture_ledger)
        checks.append(("passport validates (citations recheck) + deterministic",
                       ok and canonical_json(p1) == canonical_json(p2)))
        if not ok:
            for r in reasons:
                print(f"    unexpected: {r}")

        # [2] identity + key usage assembled from anchored records
        checks.append(("identity cites the registration; key usage from the "
                       "cross-type scan",
                       p1["identity"]["registered_root"] == "a" * 64
                       and p1["identity"]["evidence"] == "ledger:2"
                       and p1["identity"]["key_usage"] ==
                       [{"index": 0, "consumed_in": "ledger:3"}]))

        # [3] drill events carry drill:true (demonstrations, not misconduct)
        drill_items = [v for v in p1["verification_history"] if v.get("drill")]
        checks.append(("drill events labeled drill:true with the "
                       "misconduct-vs-drill note in scope",
                       len(drill_items) == 1
                       and "not misconduct" in
                       p1["scope_and_limitations"]["note"]))

        # [4] NO-LEADERBOARD rule: construction refuses + validation rejects
        try:
            bad = copy.deepcopy(p1)
            bad["identity"]["uptime_rank"] = 1
            recomputed = dict(bad)
            recomputed["passport_hash"] = compute_passport_hash(bad)
            ok, _ = validate_passport(recomputed)
            checks.append(("smuggled rank key rejected by validation", not ok))
        except ValueError:
            checks.append(("smuggled rank key rejected by validation", True))
        checks.append(("no ranking-shaped key exists (mechanical scan)",
                       leaderboard_violations(p1) == []))

        # [5] honest absent-states: unregistered actor + no energy evidence
        p_unreg = build_passport("never-registered-actor",
                                 ledger_path=fixture_ledger)
        checks.append(("unregistered actor gets the honest none-registered "
                       "state; UWW honestly not-computable",
                       p_unreg["identity"].get("state") == "none-registered"
                       and p_unreg["useful_work_per_watt"].get("state") ==
                       "not-computable"
                       and p1["useful_work_per_watt"].get("state") ==
                       "not-computable"))

        # [6] MONEY-MODULES-BLIND grep audit: emission and treasury neither
        # import nor read passports — the metric cannot become a printer
        needle = "pass" + "port"
        flow1_src = open(os.path.join(_REPO_ROOT, "demo", "flow1_uptime.py"),
                         encoding="utf-8").read()
        treasury_src = open(os.path.join(_REPO_ROOT, "demo",
                                         "metastar_treasury.py"),
                            encoding="utf-8").read()
        checks.append(("money modules are mechanically blind to passports "
                       "(grep audit)",
                       needle not in flow1_src.lower()
                       and needle not in treasury_src.lower()))

        # [7] catalog + generation-lock regression: as-of build ignores later
        # records and stays byte-stable as the chain grows
        c_before = build_passport_catalog(ledger_path=fixture_ledger)
        tip = led.read_all()[-1]["index"]
        led.append({"event": "unrelated_marker", "verifier_id":
                    "fixture-verifier", "status": "later-noise",
                    "zero_value": True, "no_token": True})
        c_locked = build_passport_catalog(ledger_path=fixture_ledger,
                                          as_of_index=tip)
        c_live = build_passport_catalog(ledger_path=fixture_ledger)
        ok_cat, _ = validate_catalog(c_before)
        checks.append(("catalog validates; generation-lock reproduces the "
                       "pre-growth catalog; live build legitimately differs",
                       ok_cat
                       and c_locked["catalog_hash"] == c_before["catalog_hash"]
                       and c_live["catalog_hash"] != c_before["catalog_hash"]))

        # [8] real-ledger conditional: build all passports; spark-agent's UWW
        # is computable (metering-linked); snapshot-source equivalence
        if os.path.exists(DEFAULT_LEDGER_PATH):
            rp = build_passport("spark-agent-same-operator")
            ok, reasons = validate_passport(rp,
                                            ledger_path=DEFAULT_LEDGER_PATH)
            checks.append(("real spark-agent passport validates; UWW "
                           "computable with estimated-energy label",
                           ok and rp["useful_work_per_watt"].get("ratio")
                           is not None
                           and rp["useful_work_per_watt"]["labels"]["energy"]
                           == "estimated"))
            if not ok:
                for r in reasons:
                    print(f"    unexpected: {r}")
            snap = os.path.join(_PROTO_DIR, "ledger_published.json")
            if os.path.exists(snap):
                rp_snap = build_passport("spark-agent-same-operator",
                                         ledger_path=snap)
                checks.append(("snapshot-source passport byte-identical",
                               canonical_json(rp_snap) == canonical_json(rp)))
        else:
            print("    (no runtime ledger — real-ledger checks SKIPPED)")
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
