# Copyright (c) 2023-2026 MetaCoin-Lab.
# Licensed under SML-1.0 — see LICENSE.md.
"""physical_work.py — THE PHYSICAL-WORK RECORD: attested device measurements
plus a re-derived analysis, anchored as one record class.

================== HONESTY / SCOPE (READ ME) ==================
Research-stage, ZERO-VALUE, no token, no money, no mainnet, no payments.

THE LABEL, VERBATIM ON EVERY RECORD: "MHS-shaped, pre-standard, simulated
device". The device interface here is SHAPED LIKE the publicly described
Model Hardware Standard (read/write primitives, a manifest of capabilities
and enforced safety limits, a state dictionary) — it is NOT the standard,
which is a limited research preview and not yet open source; nothing here
is a claim about its specification, and NO physical instrument was
operated: the device is a simulation whose signing keys derive from a
PUBLISHED seed (so its signatures prove the verification path, not
secrecy).

THE EPISTEMOLOGY, VERBATIM ON EVERY RECORD: "attested measurements are not
re-derived; only the analysis is". A measurement is a fact about the world
at an instant — a verifier cannot recompute it, only check that the device
that reported it signed it (one-time hash-based signature under the
device's declared root), that the snapshot bytes hash to what was signed,
and that nobody edited them since. The ANALYSIS over those snapshots is
deterministic stdlib arithmetic with a canonical output: it IS re-derived
bit-exact on every verification, exactly like a task. The VERDICT lives
inside that canonical output, honest-negative form included
(run_acceptable: false with the reason) — a run the analysis rejects is a
first-class result, not a discarded one. SAFETY-LIMIT REFUSALS are
first-class too: a write the manifest's limits block is recorded, signed
by the device, and verified against the anchored limits table — the
blocked fault is evidence of mechanism, never presented as detected fraud.

THE MetaCoin MAP (one line each): device manifest + identity root <->
actor key declaration / passport (the device declares itself); the
safety-limits table <-> the anchored parameter table (drift refuses by
name); refusals <-> the anchored attack drills; run_acceptable:false <->
the honest negatives; the deterministic analysis <-> the task contract
(MIP-0009 four-key shape, era-2 canonical form, sha256 acceptance); the
state dictionary <-> the ledger as bus.

Record class by the pulse / mission-verdict / envelope / parameter-table
precedent: protocol module + external_verifier --anchor-physical-work
--confirm path + verify_everything layer + sweep evidence expectation; no
MIP (it ratifies no new capability — signatures, tables, canonical form
and honest negatives are all existing machinery). Scanner-invisible
payload (no top-level task ids). NO WALL-CLOCK FIELD IN THE HASHED
DOCUMENT: snapshot times are the simulated run clock in seconds.

Usage:
    python3 protocol/physical_work.py --generate [--out physical_work.json]
    python3 protocol/physical_work.py --verify physical_work.json
    python3 protocol/physical_work.py --status
    python3 protocol/physical_work.py --selftest
Standard library only. Not financial, legal, or engineering advice.
"""

import sys
sys.dont_write_bytecode = True

import argparse
import copy
import hashlib
import json
import math
import os

_PROTO_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_PROTO_DIR)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from protocol.prov_export import resolve_ledger_path
from protocol.work_molecule import _read_ledger, find_evidence_file
import protocol.actor_identity as actor_identity
import protocol.parameter_table as parameter_table

SCHEMA = "physical_work/0.1"
EVENT = "physical_work_recorded"
STATUS = "physical-work-confirmed"
LIMITS_SCHEMA = "device-limits-table/0.1"
MANIFEST_SCHEMA = "device-manifest/0.1"

LABEL = "MHS-shaped, pre-standard, simulated device"
EPISTEMOLOGY = "attested measurements are not re-derived; only the analysis is"
TX_TAG = "TX04"   # NASA Technology Taxonomy: Robotics and Autonomous Systems —
                  # bounded-autonomous operation of instruments. The honest
                  # tag: this is device autonomy, not a science domain claim.
REFUSAL_RULE = ("a physical-work record anchors only if every snapshot "
                "signature verifies under the device's declared root with "
                "no one-time key reused, the analysis re-derives its "
                "canonical output hash bit-exact, the manifest's limits "
                "equal the limits table byte-for-byte, and every refusal "
                "names the limit it enforced — a drifted limit, a "
                "re-signed snapshot, or a re-tuned analysis refuses by name")

R2_ACCEPT_THRESHOLD = 0.9            # the acceptance rule (stated, fixed)
PLATEAU_SPREAD_AU = 0.05             # top-3 responses within this = saturation
ROUND = 6
MAX_SNAPSHOTS = 64                   # R2 bound for every loop over snapshots
DEVICE_SEED = "metacoin-simulated-plate-reader-v1"   # PUBLISHED on purpose
DEVICE_KEY_COUNT = 32

# --- the simulated device's manifest (MHS-shaped) ---------------------------
SAFETY_LIMITS = (                    # name, value, unit, governance
    ("incubation_temperature_c_max", 45.0, "degC", "anchored-config"),
    ("power_w_max", 25.0, "W", "anchored-config"),
    ("read_wavelength_nm_max", 850, "nm", "anchored-config"),
    ("read_wavelength_nm_min", 340, "nm", "anchored-config"),
    ("shaker_speed_rpm_max", 1500, "rpm", "anchored-config"),
)
CAPABILITIES = {
    "reads": ["absorbance_au", "plate_present", "temperature_c", "power_w"],
    "writes": ["shaker_speed_rpm", "incubation_temperature_c",
               "read_wavelength_nm"],
}


# ----------------------------------------------------------------------------
# canonical form (era-2)
# ----------------------------------------------------------------------------
def _sign_safe_zero(obj):
    if isinstance(obj, float):
        return 0.0 if obj == 0.0 else obj
    if isinstance(obj, dict):
        return {k: _sign_safe_zero(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sign_safe_zero(v) for v in obj]
    return obj


def canonical_json(obj) -> str:
    return json.dumps(_sign_safe_zero(obj), sort_keys=True,
                      separators=(",", ":"), ensure_ascii=True)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def record_hash(doc: dict) -> str:
    body = {k: v for k, v in doc.items() if k != "record_hash"}
    return sha256_hex(canonical_json(body))


# ----------------------------------------------------------------------------
# the simulated device (MHS-shaped: manifest + read/write + refusals)
# ----------------------------------------------------------------------------
class LimitRefusal(Exception):
    """A write the manifest's safety limits block. First-class evidence."""


def _seeded_privates(seed: str, key_count: int):
    """Private one-time secrets derived from a PUBLISHED seed — a simulation
    fixture: the signatures prove the verification path, never secrecy."""
    return [[[hashlib.sha256(f"{seed}:{k}:{i}:0".encode()).hexdigest(),
              hashlib.sha256(f"{seed}:{k}:{i}:1".encode()).hexdigest()]
             for i in range(256)] for k in range(key_count)]


def limits_table_doc() -> dict:
    """The device-limits table in the anchored-parameter-table shape."""
    return {
        "schema": LIMITS_SCHEMA,
        "table_version": 1,
        "refusal_rule": "a write outside these limits is refused before any "
                        "state changes and the refusal is recorded",
        "parameters": [
            {"name": n, "value": v, "unit": u, "governance": g}
            for n, v, u, g in SAFETY_LIMITS
        ],
        "zero_value": True,
        "no_token": True,
    }


def limits_hash(table: dict) -> str:
    return parameter_table.table_hash(table)


class SimulatedDevice:
    """A plate reader with a built-in power meter, simulated. Read/write
    primitives, a manifest, an enforced limits table, a signing identity."""

    def __init__(self, device_id="sim-plate-reader-01", seed=DEVICE_SEED):
        self.device_id = device_id
        self.keychain = actor_identity.build_keychain_from_privates(
            device_id, _seeded_privates(seed, DEVICE_KEY_COUNT))
        self.limits = {n: v for n, v, _, _ in SAFETY_LIMITS}
        self.state = {                    # the state dictionary
            "shaker_speed_rpm": 300, "incubation_temperature_c": 37.0,
            "read_wavelength_nm": 562, "plate_present": True,
            "temperature_c": 37.0, "power_w": 0.0, "absorbance_au": 0.0,
        }
        self._t_s = 0
        self._next_key = 0

    def manifest(self) -> dict:
        return {
            "schema": MANIFEST_SCHEMA,
            "device_id": self.device_id,
            "kind": "simulated-plate-reader-with-power-meter",
            "simulated": True,
            "label": LABEL,
            "capabilities": copy.deepcopy(CAPABILITIES),
            "safety_limits": {n: v for n, v, _, _ in SAFETY_LIMITS},
            "identity": {
                "actor_id": self.device_id,
                "scheme": self.keychain["scheme"],
                "key_count": self.keychain["key_count"],
                "merkle_root": self.keychain["merkle_root"],
                "key_derivation": "deterministic from a published seed "
                                  "(simulation fixture; not secret)",
            },
        }

    def read(self, name: str):
        if name not in CAPABILITIES["reads"] and name not in self.state:
            raise KeyError(f"not a readable capability: {name}")
        return self.state[name]

    def write(self, name: str, value):
        """Enforced limits: refuse BEFORE any state changes."""
        if name not in CAPABILITIES["writes"]:
            raise KeyError(f"not a writable capability: {name}")
        hi, lo = self.limits.get(f"{name}_max"), self.limits.get(f"{name}_min")
        if hi is not None and value > hi:
            raise LimitRefusal(f"{name}={value} exceeds {name}_max={hi}")
        if lo is not None and value < lo:
            raise LimitRefusal(f"{name}={value} is below {name}_min={lo}")
        self.state[name] = value

    def _sign(self, digest_hex: str) -> dict:
        sig = actor_identity.sign(self.keychain, self._next_key,
                                  digest_hex.encode("utf-8"))
        self._next_key += 1
        return sig

    def snapshot(self, dt_s: int, values: dict) -> dict:
        """An attested snapshot: the device signs the hash of (t, values)."""
        self._t_s += dt_s
        body = {"t_s": self._t_s, "values": values}
        h = sha256_hex(canonical_json(body))
        return {"t_s": self._t_s, "values": values, "snapshot_hash": h,
                "device_signature": self._sign(h)}

    def refusal(self, dt_s: int, name: str, value) -> dict:
        """Attempt a blocked write; record the block, signed."""
        self._t_s += dt_s
        try:
            self.write(name, value)
        except LimitRefusal as exc:
            limit_name = f"{name}_max" if value > self.limits.get(
                f"{name}_max", math.inf) else f"{name}_min"
            body = {"t_s": self._t_s, "requested_write": {name: value},
                    "limit": {limit_name: self.limits[limit_name]},
                    "refused": True, "reason": str(exc),
                    "state_changed": False}
            h = sha256_hex(canonical_json(body))
            body["refusal_hash"] = h
            body["device_signature"] = self._sign(h)
            return body
        raise AssertionError("a refusal fixture must be refused")


# ----------------------------------------------------------------------------
# the deterministic analysis (re-derived bit-exact on every verification)
# ----------------------------------------------------------------------------
def analyze(snapshots: list) -> dict:
    """Dose-response fit over attested snapshots: absorbance vs
    log10(concentration), ordinary least squares, R^2, a plateau probe,
    and metered energy. Assertions crash rather than fudge."""
    assert 3 <= len(snapshots) <= MAX_SNAPSHOTS, "snapshot count out of bounds"
    conc = [s["values"]["concentration_um"] for s in snapshots]
    absb = [s["values"]["absorbance_au"] for s in snapshots]
    assert all(c > 0 for c in conc), "concentrations must be positive"
    assert conc == sorted(conc) and len(set(conc)) == len(conc), \
        "concentrations must be strictly increasing"
    x = [math.log10(c) for c in conc]
    n = len(x)
    mx, my = sum(x) / n, sum(absb) / n
    sxx = sum((xi - mx) ** 2 for xi in x)
    sxy = sum((xi - mx) * (yi - my) for xi, yi in zip(x, absb))
    slope = sxy / sxx
    intercept = my - slope * mx
    ss_res = sum((yi - (intercept + slope * xi)) ** 2
                 for xi, yi in zip(x, absb))
    ss_tot = sum((yi - my) ** 2 for yi in absb)
    r2 = 1.0 - ss_res / ss_tot
    assert -1e-9 <= r2 <= 1.0 + 1e-9, "R^2 outside [0, 1]"
    top3 = sorted(absb)[-3:]
    plateau = (top3[-1] - top3[0]) < PLATEAU_SPREAD_AU
    # metered energy: power_w integrated over the snapshot intervals
    energy_j = 0.0
    prev_t = snapshots[0]["t_s"]
    for s in snapshots[1:]:                          # bounded: MAX_SNAPSHOTS
        dt = s["t_s"] - prev_t
        assert dt > 0, "snapshot clock must advance"
        energy_j += s["values"]["power_w"] * dt
        prev_t = s["t_s"]
    assert energy_j >= 0.0, "energy cannot be negative"
    # conservation: every snapshot contributes exactly one point
    assert n == len(snapshots), "point count != snapshot count"
    r2r = round(r2, ROUND)
    acceptable = (r2r >= R2_ACCEPT_THRESHOLD) and not plateau
    if acceptable:
        reason = (f"R^2 {r2r} >= {R2_ACCEPT_THRESHOLD} and no saturation "
                  "plateau: the curve is accepted")
    elif plateau:
        reason = (f"saturation: the top three responses span "
                  f"{round(top3[-1] - top3[0], ROUND)} AU < "
                  f"{PLATEAU_SPREAD_AU}; R^2 {r2r} — the concentration "
                  "range must be adjusted (honest negative)")
    else:
        reason = (f"R^2 {r2r} < {R2_ACCEPT_THRESHOLD}: fit criterion not "
                  "met (honest negative)")
    return {
        "method": "OLS of absorbance_au on log10(concentration_um); R^2; "
                  "top-3 plateau probe; trapezoid-free rectangular power "
                  "integration",
        "n_points": n,
        "concentration_range_um": [conc[0], conc[-1]],
        "slope_au_per_decade": round(slope, ROUND),
        "intercept_au": round(intercept, ROUND),
        "r2": r2r,
        "plateau_detected": plateau,
        "energy_j": round(energy_j, ROUND),
        "verdict": {"run_acceptable": acceptable, "reason": reason,
                    "acceptance_rule": f"R^2 >= {R2_ACCEPT_THRESHOLD} and "
                                       "no plateau"},
    }


def output_hash(analysis: dict) -> str:
    return sha256_hex(canonical_json(analysis))


# ----------------------------------------------------------------------------
# the demo (mirrors the public CMU description: run 1 rejected, run 2 accepted,
# one blocked fault) — a SIMULATED fixture, deterministic
# ----------------------------------------------------------------------------
def _response(c, emax=1.6, ec50=8.0, hill=1.2):
    return emax * c ** hill / (ec50 ** hill + c ** hill)


def _noise(i, amp=0.006):
    x = (1664525 * (i + 7) + 1013904223) & 0xFFFFFFFF
    return amp * (((x >> 8) % 2001) / 1000.0 - 1.0)


RUN1_CONCENTRATIONS_UM = [0.5, 2.0, 8.0, 32.0, 128.0, 512.0, 1024.0, 2048.0]
RUN2_CONCENTRATIONS_UM = [1.0, 2.0, 3.0, 5.0, 8.0, 12.0, 18.0, 25.0]


def _run(device: SimulatedDevice, run_id: str, purpose: str, concs: list,
         power_w: float) -> dict:
    snaps = []
    for i, c in enumerate(concs):                    # bounded: len(concs)
        device.state["power_w"] = power_w
        device.state["absorbance_au"] = round(_response(c) + _noise(i), ROUND)
        snaps.append(device.snapshot(30, {
            "concentration_um": c,
            "absorbance_au": device.read("absorbance_au"),
            "power_w": device.read("power_w"),
            "temperature_c": device.read("temperature_c"),
            "plate_present": device.read("plate_present"),
        }))
    analysis = analyze(snaps)
    return {"run_id": run_id, "purpose": purpose, "state_snapshots": snaps,
            "analysis": analysis, "output_hash": output_hash(analysis)}


def build_record() -> dict:
    device = SimulatedDevice()
    manifest = device.manifest()
    table = limits_table_doc()
    run1 = _run(device, "run-1", "dose-response, initial concentration range",
                RUN1_CONCENTRATIONS_UM, 18.5)
    refusal = device.refusal(15, "shaker_speed_rpm", 1800)
    run2 = _run(device, "run-2", "dose-response, concentration range "
                "adjusted after run-1's rejection", RUN2_CONCENTRATIONS_UM,
                18.5)
    doc = {
        "schema": SCHEMA,
        "label": LABEL,
        "epistemology": EPISTEMOLOGY,
        "tx_tag": TX_TAG,
        "simulated": True,
        "device_manifest": manifest,
        "device_manifest_hash": sha256_hex(canonical_json(manifest)),
        "safety_limits_table": table,
        "safety_limits_hash": limits_hash(table),
        "runs": [run1, run2],
        "refusals": [refusal],
        "refusal_rule": REFUSAL_RULE,
        "zero_value": True,
        "no_token": True,
    }
    doc["record_hash"] = record_hash(doc)
    return doc


# ----------------------------------------------------------------------------
# verification (signatures ATTESTED; analysis RE-DERIVED; limits by table)
# ----------------------------------------------------------------------------
def verify_record(doc: dict) -> tuple:
    """(ok, reasons, stats). Never rebuilds a snapshot."""
    reasons, stats = [], {"snapshots": 0, "signatures_ok": 0, "runs": 0,
                          "refusals": 0}
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA:
        return (False, ["not a physical_work/0.1 document"], stats)
    for key in ("label", "epistemology", "device_manifest",
                "device_manifest_hash", "safety_limits_table",
                "safety_limits_hash", "runs", "refusals", "record_hash"):
        if key not in doc:
            reasons.append(f"missing {key}")
    if reasons:
        return (False, reasons, stats)
    if doc["label"] != LABEL:
        reasons.append("the verbatim simulated-device label is missing or "
                       "altered")
    if doc["epistemology"] != EPISTEMOLOGY:
        reasons.append("the verbatim epistemology line is missing or altered")
    if doc.get("simulated") is not True:
        reasons.append("simulated must be true on this record class today")
    if record_hash(doc) != doc["record_hash"]:
        reasons.append("record_hash does not recompute from the document")
    manifest = doc["device_manifest"]
    if sha256_hex(canonical_json(manifest)) != doc["device_manifest_hash"]:
        reasons.append("device_manifest_hash does not recompute")
    table = doc["safety_limits_table"]
    if limits_hash(table) != doc["safety_limits_hash"]:
        reasons.append("safety_limits_hash does not recompute from the table")
    if table.get("schema") != LIMITS_SCHEMA:
        reasons.append("limits table schema mismatch")
    table_map = {p["name"]: p["value"] for p in table.get("parameters", [])}
    names = [p["name"] for p in table.get("parameters", [])]
    if names != sorted(names):
        reasons.append("limits table parameters must be sorted by name")
    if manifest.get("safety_limits") != table_map:
        reasons.append("manifest safety_limits differ from the limits table "
                       "— DRIFT (refused by name: "
                       + ", ".join(sorted(set(table_map)
                                          ^ set(manifest.get("safety_limits",
                                                             {})))
                                   or ["values differ"]) + ")")
    root = manifest.get("identity", {}).get("merkle_root")
    used = set()

    def _check_sig(sig, digest_hex, where):
        ok, why = actor_identity.verify_signature(
            sig, root, digest_hex.encode("utf-8"))
        if not ok:
            reasons.append(f"{where}: device signature fails ({why[0]})")
            return
        if sig["key_index"] in used:
            reasons.append(f"{where}: one-time key index "
                           f"{sig['key_index']} reused")
        used.add(sig["key_index"])
        stats["signatures_ok"] += 1

    for run in doc["runs"]:
        stats["runs"] += 1
        snaps = run.get("state_snapshots", [])
        if len(snaps) > MAX_SNAPSHOTS:
            reasons.append(f"{run.get('run_id')}: too many snapshots")
            continue
        for s in snaps:
            stats["snapshots"] += 1
            body = {"t_s": s["t_s"], "values": s["values"]}
            if sha256_hex(canonical_json(body)) != s["snapshot_hash"]:
                reasons.append(f"{run['run_id']} t={s['t_s']}: snapshot_hash "
                               "does not match the snapshot bytes — edited "
                               "after signing")
                continue
            _check_sig(s["device_signature"], s["snapshot_hash"],
                       f"{run['run_id']} t={s['t_s']}")
        try:
            fresh = analyze(snaps)
        except AssertionError as exc:
            reasons.append(f"{run.get('run_id')}: analysis refuses: {exc}")
            continue
        if canonical_json(fresh) != canonical_json(run.get("analysis")):
            reasons.append(f"{run['run_id']}: analysis does not re-derive "
                           "bit-exact")
        if output_hash(fresh) != run.get("output_hash"):
            reasons.append(f"{run['run_id']}: output_hash mismatch")
    for r in doc["refusals"]:
        stats["refusals"] += 1
        body = {k: v for k, v in r.items()
                if k not in ("refusal_hash", "device_signature")}
        if sha256_hex(canonical_json(body)) != r.get("refusal_hash"):
            reasons.append("refusal: refusal_hash does not match its bytes")
            continue
        if r.get("refused") is not True or r.get("state_changed") is not False:
            reasons.append("refusal must be refused=true, state_changed=false")
        (name, value), = r["requested_write"].items()
        (limit_name, limit_value), = r["limit"].items()
        if table_map.get(limit_name) != limit_value:
            reasons.append(f"refusal cites {limit_name}={limit_value} but the "
                           f"limits table says {table_map.get(limit_name)}")
        within = (value <= limit_value if limit_name.endswith("_max")
                  else value >= limit_value)
        if within:
            reasons.append(f"refusal of {name}={value} names no violated "
                           "limit — a refusal must enforce a limit")
        _check_sig(r["device_signature"], r["refusal_hash"], "refusal")
    return (not reasons, reasons, stats)


def headline(doc: dict) -> dict:
    """On-chain numbers (scanner-invisible: no task_id/task_ids keys)."""
    runs = doc["runs"]
    return {
        "device_id": doc["device_manifest"]["device_id"],
        "runs": len(runs),
        "accepted": sum(1 for r in runs
                        if r["analysis"]["verdict"]["run_acceptable"]),
        "rejected": sum(1 for r in runs
                        if not r["analysis"]["verdict"]["run_acceptable"]),
        "refusals": len(doc["refusals"]),
        "run_verdicts": [{"run_id": r["run_id"],
                          "run_acceptable": r["analysis"]["verdict"]
                          ["run_acceptable"],
                          "r2": r["analysis"]["r2"],
                          "energy_j": r["analysis"]["energy_j"]}
                         for r in runs],
        "snapshots_attested": sum(len(r["state_snapshots"]) for r in runs),
        "limits_enforced": len(doc["safety_limits_table"]["parameters"]),
    }


def rederive(entries) -> dict:
    """The verify layer's path: the anchored record's evidence file must
    verify (signatures attested, analysis re-derived) and match the
    anchored hashes. Raises ValueError with a named refusal."""
    recs = [e for e in entries
            if e.get("payload", {}).get("event") == EVENT
            and e["payload"].get("status") == STATUS]
    if not recs:
        raise ValueError("REFUSED: no physical_work_recorded on this chain")
    out = []
    for rec in recs:
        p = rec["payload"]
        path = find_evidence_file(f"physical_work_{p['record_hash'][:12]}.json")
        if path is None:
            raise ValueError(f"REFUSED: idx {rec['index']} cites record "
                             f"{p['record_hash'][:12]} but no evidence ships")
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
        ok, reasons, stats = verify_record(doc)
        if not ok:
            raise ValueError(f"REFUSED: idx {rec['index']}: "
                             + "; ".join(reasons[:3]))
        for key in ("record_hash", "device_manifest_hash",
                    "safety_limits_hash"):
            if doc[key] != p.get(key):
                raise ValueError(f"REFUSED: idx {rec['index']}: evidence "
                                 f"{key} != anchored {key}")
        if p.get("label") != LABEL or p.get("epistemology") != EPISTEMOLOGY:
            raise ValueError(f"REFUSED: idx {rec['index']}: the anchored "
                             "record lacks the verbatim label/epistemology")
        out.append({"index": rec["index"], "stats": stats,
                    "headline": headline(doc)})
    return {"records": out}


def build_payload(doc: dict) -> dict:
    """The ledger payload (what external_verifier anchors after re-confirming)."""
    return {
        "event": EVENT, "stage": "R-physical",
        "topology": "same-operator-coordinator-physical-work-simulated",
        "status": STATUS,
        "record_schema": SCHEMA,
        "record_hash": doc["record_hash"],
        "device_manifest_hash": doc["device_manifest_hash"],
        "safety_limits_hash": doc["safety_limits_hash"],
        "label": LABEL,
        "epistemology": EPISTEMOLOGY,
        "simulated": True,
        "tx_tag": TX_TAG,
        "headline": headline(doc),
        "refusal_rule": REFUSAL_RULE,
        "zero_value": True,
        "no_token": True,
    }


def status(entries=None, echo=print) -> int:
    entries = (entries if entries is not None
               else _read_ledger(resolve_ledger_path()))
    try:
        out = rederive(entries)
    except ValueError as exc:
        if "no physical_work_recorded" in str(exc):
            echo("PHYSICAL-WORK STATUS: OK — no physical-work record "
                 "anchored on the chain yet (named)")
            return 0
        echo(f"PHYSICAL-WORK STATUS: BROKEN — {exc}")
        return 1
    for r in out["records"]:
        h = r["headline"]
        echo(f"PHYSICAL-WORK STATUS: OK — idx {r['index']}: "
             f"{r['stats']['signatures_ok']} device signatures verified "
             f"(attested), {h['runs']} runs re-derived bit-exact "
             f"({h['accepted']} accepted / {h['rejected']} rejected), "
             f"{h['refusals']} safety-limit refusal(s) on the record; "
             f"simulated device, MHS-shaped")
    return 0


# ----------------------------------------------------------------------------
# self-test (deterministic build; tampered-snapshot + loosened-limit fixtures)
# ----------------------------------------------------------------------------
def _selftest() -> int:
    print("=== protocol/physical_work.py self-test (read-only; no ledger "
          "writes) ===")
    print("Attested measurements are not re-derived; only the analysis is.\n")
    checks = []
    doc, doc2 = build_record(), build_record()
    ok, reasons, stats = verify_record(doc)
    for r in reasons:
        print("    FINDING:", r)
    checks.append(("the demo record builds and verifies (signatures + "
                   f"analysis + limits; {stats['signatures_ok']} signatures)",
                   ok and stats["signatures_ok"] == 17))
    checks.append(("deterministic: two builds byte-identical, same hash",
                   canonical_json(doc) == canonical_json(doc2)
                   and doc["record_hash"] == doc2["record_hash"]))
    v1 = doc["runs"][0]["analysis"]["verdict"]
    v2 = doc["runs"][1]["analysis"]["verdict"]
    checks.append(("run-1 is the honest negative: run_acceptable false, "
                   f"R^2 {doc['runs'][0]['analysis']['r2']} with a plateau",
                   v1["run_acceptable"] is False
                   and doc["runs"][0]["analysis"]["r2"] < R2_ACCEPT_THRESHOLD
                   and doc["runs"][0]["analysis"]["plateau_detected"]))
    checks.append(("run-2 is accepted: run_acceptable true, "
                   f"R^2 {doc['runs'][1]['analysis']['r2']} > 0.98",
                   v2["run_acceptable"] is True
                   and doc["runs"][1]["analysis"]["r2"] > 0.98))
    checks.append(("one safety-limit refusal on the record, state unchanged",
                   len(doc["refusals"]) == 1
                   and doc["refusals"][0]["state_changed"] is False))
    checks.append(("the verbatim label + epistemology ride the record",
                   doc["label"] == LABEL and doc["epistemology"] == EPISTEMOLOGY))
    checks.append(("headline is scanner-invisible (no task_id/task_ids)",
                   "task_id" not in json.dumps(headline(doc))
                   and "task_ids" not in json.dumps(headline(doc))))

    def _keys(obj):
        stack = [obj]
        while stack:
            o = stack.pop()
            if isinstance(o, dict):
                for k, v in o.items():
                    yield k
                    stack.append(v)
            elif isinstance(o, list):
                stack.extend(o)
    checks.append(("no wall-clock field in the hashed document",
                   not any(k in ("anchored_at", "evaluated_at", "timestamp")
                           for k in _keys(doc))))
    # FIXTURE 1: a tampered snapshot (one absorbance nudged) FAILS LOUDLY
    t = copy.deepcopy(doc)
    t["runs"][1]["state_snapshots"][3]["values"]["absorbance_au"] += 0.01
    ok_t, why_t, _ = verify_record(t)
    checks.append(("TAMPERED SNAPSHOT fails loudly (bytes != signed hash)",
                   not ok_t and any("edited after signing" in w for w in why_t)))
    # FIXTURE 2: a re-hashed, re-signed-by-nobody snapshot still fails
    t2 = copy.deepcopy(doc)
    s = t2["runs"][1]["state_snapshots"][3]
    s["values"]["absorbance_au"] += 0.01
    s["snapshot_hash"] = sha256_hex(canonical_json({"t_s": s["t_s"],
                                                     "values": s["values"]}))
    ok_t2, why_t2, _ = verify_record(t2)
    checks.append(("re-hashed snapshot without the device's signature fails "
                   "(signature over different bytes)",
                   not ok_t2 and any("signature fails" in w for w in why_t2)))
    # FIXTURE 3: LOOSENED LIMIT in the manifest FAILS by name (drift)
    t3 = copy.deepcopy(doc)
    t3["device_manifest"]["safety_limits"]["shaker_speed_rpm_max"] = 2000
    t3["device_manifest_hash"] = sha256_hex(canonical_json(t3["device_manifest"]))
    t3["record_hash"] = record_hash(t3)
    ok_t3, why_t3, _ = verify_record(t3)
    checks.append(("LOOSENED LIMIT (manifest 2000 rpm vs table 1500) fails "
                   "by name as DRIFT",
                   not ok_t3 and any("DRIFT" in w for w in why_t3)))
    # FIXTURE 4: loosening the TABLE too makes the refusal name no violation
    t4 = copy.deepcopy(t3)
    for p in t4["safety_limits_table"]["parameters"]:
        if p["name"] == "shaker_speed_rpm_max":
            p["value"] = 2000
    t4["safety_limits_hash"] = limits_hash(t4["safety_limits_table"])
    t4["record_hash"] = record_hash(t4)
    ok_t4, why_t4, _ = verify_record(t4)
    checks.append(("loosening the table as well: the recorded refusal no "
                   "longer enforces a limit -> refused",
                   not ok_t4 and any("names no violated limit" in w
                                     or "limits table says" in w
                                     for w in why_t4)))
    # FIXTURE 5: a re-tuned analysis (verdict flipped) fails bit-exact
    t5 = copy.deepcopy(doc)
    t5["runs"][0]["analysis"]["verdict"]["run_acceptable"] = True
    t5["record_hash"] = record_hash(t5)
    ok_t5, why_t5, _ = verify_record(t5)
    checks.append(("a flipped verdict (manufactured success) fails "
                   "re-derivation bit-exact",
                   not ok_t5 and any("re-derive bit-exact" in w for w in why_t5)))
    # FIXTURE 6: the device refuses an over-limit write before any change
    dev = SimulatedDevice()
    before = dict(dev.state)
    try:
        dev.write("shaker_speed_rpm", 1800)
        refused = False
    except LimitRefusal:
        refused = True
    checks.append(("the device refuses an over-limit write before any state "
                   "change", refused and dev.state == before))
    out = []
    checks.append(("status names 'no record yet' as OK on a chain without one",
                   status([{"index": 0, "payload": {"event": "x"}}],
                          echo=out.append) == 0 and "no physical-work" in out[0]))
    print("--- self-test invariants ---")
    failures = 0
    for name, passed in checks:
        print(f"{name:72s}: {'PASS' if passed else 'FAIL'}")
        failures += not passed
    ok_all = failures == 0
    print("\n=== self-test summary: "
          + ("ALL CHECKS BEHAVED CORRECTLY" if ok_all else "FAILURE — see above")
          + " ===")
    return 0 if ok_all else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="The physical-work record (research-stage, ZERO-VALUE, "
                    "no token): attested device snapshots + a re-derived "
                    "analysis with honest-negative verdicts and first-class "
                    "safety refusals. MHS-shaped, simulated device.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--generate", action="store_true",
                      help="build the simulated demo record (writes only "
                           "--out; no ledger write)")
    mode.add_argument("--verify", metavar="RECORD_JSON")
    mode.add_argument("--status", action="store_true")
    mode.add_argument("--selftest", action="store_true")
    parser.add_argument("--out", default=os.path.join(_REPO_ROOT,
                                                      "physical_work.json"))
    args = parser.parse_args(argv)
    if args.selftest:
        return _selftest()
    if args.generate:
        doc = build_record()
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=1, sort_keys=True)
        h = headline(doc)
        print(f"physical-work record written: {args.out}\n  record_hash "
              f"{doc['record_hash']}\n  runs {h['runs']} (accepted "
              f"{h['accepted']}, rejected {h['rejected']}), refusals "
              f"{h['refusals']}, snapshots {h['snapshots_attested']}\n  "
              f"label: {LABEL}")
        return 0
    if args.verify:
        with open(args.verify, encoding="utf-8") as f:
            doc = json.load(f)
        ok, reasons, stats = verify_record(doc)
        print(("VERIFIED" if ok else "REFUSED") + f" — {stats}")
        for r in reasons:
            print("  -", r)
        return 0 if ok else 1
    if args.status:
        return status()
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
