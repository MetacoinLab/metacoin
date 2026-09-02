# Copyright (c) 2023-2026 MetaCoin-Lab.
# Licensed under SML-1.0 — see LICENSE.md.
"""generate_view_data.py — the trust-ledger console's data generator: convert
the published ledger snapshot into one Open MCT-consumable JSON document.

============================== SCOPE / BOUNDARY ==============================
OPTIONAL INTEGRATION; STANDARD LIBRARY ONLY; ZERO LEDGER WRITES. This script
READS protocol/ledger_published.json (the artifact every fresh clone ships)
and WRITES one derived JSON file for the static Open MCT app in this
directory. It never touches the live ledger, keys, or anchoring machinery,
and nothing in protocol/, demo/, or metacoin_cli/ imports it. A VIEWER, not
a verifier: the numbers on the screen re-derive with verify_everything and
the TOUR — this only displays them. Research-only; no token; not financial
advice. Open MCT is NASA's open-source mission-control framework
(github.com/nasa/openmct, Apache-2.0), used here as an ordinary npm
dependency of this integration only; no NASA affiliation or endorsement.
==============================================================================

THE MAPPING (audit log -> mission-control telemetry):
  * every ledger entry        -> an event datum on the "Chain Events" stream
                                 {utc, index, event, status, note}
  * drills / rejections /     -> the "Drills & Refusals" stream (the defeated-
    refuted records              attack story as an annotated event timeline)
  * ACI baselines + epochs    -> the "Same-Operator Concentration" numeric
                                 stream (pairwise ACI over time — a plot)
  * mission verdict records   -> the "Mission Verdicts" stream (feasible flag,
                                 failed/constraining counts, verdict hash)
  * pulse records             -> the "Pulse Health" stream (layers, suite
                                 totals, sweep findings per anchored pulse)
Timestamps are the records' own anchored_at/evaluated_at wall-clock fields
(display data, not hashed artifacts — the house no-timestamps rule governs
hashed objects, and this file is derived display JSON, never anchored).
Determinism: the output is a pure function of the snapshot bytes; the
self-test builds twice and asserts byte-identity, and validates the schema
contract the plugin consumes (identifiers resolve, compositions close,
telemetry rows sorted by utc, domain/range hints present).

Usage:
    python3 integrations/openmct/generate_view_data.py            # writes data/trust_ledger.json
    python3 integrations/openmct/generate_view_data.py --selftest
"""

import argparse
import json
import os
import sys

sys.dont_write_bytecode = True

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(_HERE))
DEFAULT_SNAPSHOT = os.path.join(_REPO_ROOT, "protocol",
                                "ledger_published.json")
DEFAULT_OUT = os.path.join(_HERE, "data", "trust_ledger.json")

NAMESPACE = "metacoin"
GENERATOR_SCHEMA = "openmct-trust-ledger/0.1"

_DRILL_STATUSES = ("rejected", "refuted", "local-mismatch",
                   "external-mismatch", "heartbeat-rejected",
                   "participant_intake_rejected")


def _ts_ms(payload, fallback_ms):
    """Display timestamp: the record's own wall-clock field, else carry the
    previous entry's (monotone fill — display only, stated)."""
    for key in ("anchored_at", "evaluated_at", "observed_at", "timestamp"):
        v = payload.get(key)
        if isinstance(v, (int, float)) and v > 0:
            return int(v * 1000)
    return fallback_ms


def _metric_meta(name, unit=""):
    return [
        {"key": "utc", "source": "utc", "name": "Time", "format": "utc",
         "hints": {"domain": 1}},
        {"key": "value", "name": name, "unit": unit, "format": "float",
         "hints": {"range": 1}},
    ]


def _event_meta(fields):
    values = [{"key": "utc", "source": "utc", "name": "Time",
               "format": "utc", "hints": {"domain": 1}}]
    for i, (key, name) in enumerate(fields):
        values.append({"key": key, "name": name, "format": "string",
                       "hints": {"range": i + 1}})
    return values


def build_view_data(snapshot: dict) -> dict:
    entries = snapshot["entries"]
    tip = entries[-1]

    chain_rows, drill_rows, aci_rows = [], [], []
    mission_rows, pulse_rows = [], []
    last_ms = 0
    for e in entries:
        p = e.get("payload", {})
        ms = _ts_ms(p, last_ms)
        last_ms = ms
        event = str(p.get("event", ""))
        status = str(p.get("status", ""))
        chain_rows.append({"utc": ms, "index": e["index"], "event": event,
                           "status": status,
                           "hash12": str(e.get("hash", ""))[:12]})
        if p.get("drill") or status in _DRILL_STATUSES or "reject" in status:
            drill_rows.append({"utc": ms, "index": e["index"],
                               "event": event, "status": status,
                               "note": str(p.get("reason", "")
                                           or p.get("attack", "")
                                           or "labeled drill")[:120]})
        if event in ("aci_baseline_anchored", "aci_epoch_observed") and \
                isinstance(p.get("pairwise_aci"), (int, float)):
            aci_rows.append({"utc": ms, "value": float(p["pairwise_aci"]),
                             "index": e["index"]})
        if event == "mission_verdict_recorded" and status == \
                "mission-verdict-confirmed":
            h = p.get("headline", {})
            mission_rows.append({
                "utc": ms, "index": e["index"],
                "mission": str(p.get("mission_id", "")),
                "feasible": str(p.get("mission_feasible")),
                "constraining": str(h.get("constraining", "")),
                "failed": str(h.get("failed_constraining", "")),
                "hash12": str(p.get("verdict_hash", ""))[:12]})
        if event == "pulse_recorded" and status == "pulse-confirmed":
            h = p.get("headline", {})
            pulse_rows.append({
                "utc": ms, "index": e["index"],
                "layers": str(h.get("verify_everything_layers", "")),
                "demo_suite": str(h.get("demo_suite", "")),
                "protocol_suite": str(h.get("protocol_suite", "")),
                "sweep_findings": str(h.get("sweep_findings", "")),
                "cold_install": str(h.get("cold_install", ""))})

    def obj(key, name, otype, telemetry_values=None, composition=None):
        o = {"identifier": {"namespace": NAMESPACE, "key": key},
             "name": name, "type": otype, "location": f"{NAMESPACE}:root"}
        if telemetry_values is not None:
            o["telemetry"] = {"values": telemetry_values}
        if composition is not None:
            o["composition"] = [{"namespace": NAMESPACE, "key": c}
                                for c in composition]
        return o

    objects = {
        "root": {"identifier": {"namespace": NAMESPACE, "key": "root"},
                 "name": "MetaCoin Trust Ledger",
                 "type": "folder", "location": "ROOT",
                 "composition": [{"namespace": NAMESPACE, "key": k}
                                 for k in ("chain.events", "drills.events",
                                           "aci.pairwise",
                                           "missions.verdicts",
                                           "pulse.health")]},
        "chain.events": obj(
            "chain.events", "Chain Events (every anchored record)",
            "metacoin.events",
            _event_meta([("index", "Idx"), ("event", "Event"),
                         ("status", "Status"), ("hash12", "Hash")])),
        "drills.events": obj(
            "drills.events", "Drills & Refusals (defeated attacks)",
            "metacoin.events",
            _event_meta([("index", "Idx"), ("event", "Event"),
                         ("status", "Status"), ("note", "Note")])),
        "aci.pairwise": obj(
            "aci.pairwise", "Same-Operator Concentration (pairwise ACI)",
            "metacoin.metric", _metric_meta("Pairwise ACI")),
        "missions.verdicts": obj(
            "missions.verdicts", "Mission Verdicts (honest negatives)",
            "metacoin.events",
            _event_meta([("index", "Idx"), ("mission", "Mission"),
                         ("feasible", "Feasible"),
                         ("failed", "Failed"),
                         ("constraining", "Constraining"),
                         ("hash12", "Verdict hash")])),
        "pulse.health": obj(
            "pulse.health", "Pulse Health (anchored gate snapshots)",
            "metacoin.events",
            _event_meta([("index", "Idx"), ("layers", "Layers"),
                         ("demo_suite", "Demo"),
                         ("protocol_suite", "Protocol"),
                         ("sweep_findings", "Sweep findings"),
                         ("cold_install", "Cold install")])),
    }

    telemetry = {"chain.events": chain_rows, "drills.events": drill_rows,
                 "aci.pairwise": aci_rows, "missions.verdicts": mission_rows,
                 "pulse.health": pulse_rows}
    for rows in telemetry.values():
        rows.sort(key=lambda r: (r["utc"], r.get("index", 0)))

    utcs = [r["utc"] for r in chain_rows if r["utc"] > 0]
    return {
        "schema": GENERATOR_SCHEMA,
        "source": {"entry_count": len(entries),
                   "tip_index": tip["index"],
                   "tip_hash": tip.get("hash", "")},
        "time_bounds": {"start": (min(utcs) if utcs else 0) - 86400000,
                        "end": (max(utcs) if utcs else 0) + 86400000},
        "objects": objects,
        "telemetry": telemetry,
        "viewer_note": ("a VIEWER, not a verifier: every number here "
                        "re-derives from a fresh clone via "
                        "protocol/verify_everything.py --full and the TOUR"),
    }


def validate_view_data(doc: dict):
    """(ok, reasons): the schema contract plugin.js consumes."""
    reasons = []
    if doc.get("schema") != GENERATOR_SCHEMA:
        reasons.append("wrong schema")
    objs = doc.get("objects", {})
    tele = doc.get("telemetry", {})
    for key, o in objs.items():
        if o["identifier"]["key"] != key:
            reasons.append(f"{key}: identifier mismatch")
        for child in o.get("composition", []):
            if child["key"] not in objs:
                reasons.append(f"{key}: composition child {child['key']} "
                               "unresolved")
        if o.get("type", "").startswith("metacoin.") and key not in tele:
            reasons.append(f"{key}: telemetry object without rows")
        values = (o.get("telemetry") or {}).get("values", [])
        if values:
            hints = [v.get("hints", {}) for v in values]
            if not any(h.get("domain") for h in hints):
                reasons.append(f"{key}: no domain hint")
            if not any(h.get("range") for h in hints):
                reasons.append(f"{key}: no range hint")
    for key, rows in tele.items():
        if key not in objs:
            reasons.append(f"telemetry {key}: no object")
        if any(rows[i]["utc"] > rows[i + 1]["utc"]
               for i in range(len(rows) - 1)):
            reasons.append(f"telemetry {key}: rows not sorted by utc")
    if not tele.get("chain.events"):
        reasons.append("chain.events is empty")
    return (not reasons, reasons)


def generate(snapshot_path=DEFAULT_SNAPSHOT, out_path=DEFAULT_OUT) -> dict:
    with open(snapshot_path, encoding="utf-8") as f:
        snapshot = json.load(f)
    doc = build_view_data(snapshot)
    ok, reasons = validate_view_data(doc)
    if not ok:
        raise ValueError("generated view data violates its own contract: "
                         + "; ".join(reasons))
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=1, sort_keys=True)
        f.write("\n")
    return doc


def _selftest() -> int:
    print("=== integrations/openmct/generate_view_data.py self-test "
          "(read-only; writes temp files only) ===\n")
    import tempfile
    checks = []
    with open(DEFAULT_SNAPSHOT, encoding="utf-8") as f:
        snapshot = json.load(f)
    d1 = build_view_data(snapshot)
    d2 = build_view_data(snapshot)
    checks.append(("deterministic: two builds byte-identical",
                   json.dumps(d1, sort_keys=True)
                   == json.dumps(d2, sort_keys=True)))
    ok, reasons = validate_view_data(d1)
    checks.append(("schema contract holds on the real snapshot "
                   f"({len(d1['telemetry']['chain.events'])} chain events, "
                   f"{len(d1['telemetry']['drills.events'])} drill rows, "
                   f"{len(d1['telemetry']['aci.pairwise'])} ACI points, "
                   f"{len(d1['telemetry']['missions.verdicts'])} verdicts, "
                   f"{len(d1['telemetry']['pulse.health'])} pulses)",
                   ok and not reasons))
    checks.append(("every ledger entry appears exactly once on the chain "
                   "stream",
                   len(d1["telemetry"]["chain.events"])
                   == d1["source"]["entry_count"]))
    checks.append(("the mission verdicts stream carries the anchored "
                   "records (>= 3 on this chain)",
                   len(d1["telemetry"]["missions.verdicts"]) >= 3))
    broken = json.loads(json.dumps(d1))
    broken["objects"]["root"]["composition"].append(
        {"namespace": NAMESPACE, "key": "no.such.object"})
    checks.append(("a dangling composition child is refused by the "
                   "validator", not validate_view_data(broken)[0]))
    unsorted_doc = json.loads(json.dumps(d1))
    if len(unsorted_doc["telemetry"]["aci.pairwise"]) >= 2:
        unsorted_doc["telemetry"]["aci.pairwise"].reverse()
        checks.append(("unsorted telemetry rows are refused by the validator",
                       not validate_view_data(unsorted_doc)[0]))
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "trust_ledger.json")
        generate(out_path=out)
        with open(out, encoding="utf-8") as f:
            reloaded = json.load(f)
        checks.append(("generate() round-trips through disk and re-validates",
                       validate_view_data(reloaded)[0]))
    print("--- self-test invariants ---")
    failures = 0
    for name, passed in checks:
        print(f"{name:72s}: {'PASS' if passed else 'FAIL'}")
        failures += not passed
    ok = failures == 0
    print("\n=== self-test summary: "
          + ("ALL CHECKS BEHAVED CORRECTLY" if ok else "FAILURE — see above")
          + " ===")
    return 0 if ok else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Trust-ledger console data generator (research-stage, "
                    "ZERO-VALUE, no token): published snapshot -> Open MCT "
                    "view data. A viewer, not a verifier.")
    parser.add_argument("--snapshot", default=DEFAULT_SNAPSHOT)
    parser.add_argument("--out", default=DEFAULT_OUT)
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args(argv)
    if args.selftest:
        return _selftest()
    doc = generate(args.snapshot, args.out)
    print(f"view data written: {args.out}")
    print(f"  {doc['source']['entry_count']} entries -> "
          f"{len(doc['telemetry']['chain.events'])} chain events, "
          f"{len(doc['telemetry']['drills.events'])} drill rows, "
          f"{len(doc['telemetry']['aci.pairwise'])} ACI points, "
          f"{len(doc['telemetry']['missions.verdicts'])} mission verdicts, "
          f"{len(doc['telemetry']['pulse.health'])} pulse snapshots")
    return 0


if __name__ == "__main__":
    sys.exit(main())
