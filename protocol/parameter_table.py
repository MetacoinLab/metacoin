# Copyright (c) 2023-2026 MetaCoin-Lab.
# Licensed under SML-1.0 — see LICENSE.md.
"""parameter_table.py — the anchored protocol parameter table (adoption #1
from the cFE Table Services discipline: configuration as a versioned,
hash-anchored TABLE, never scattered literals).

================== HONESTY / SCOPE (READ ME) ==================
Research-stage, ZERO-VALUE, no token. Standard library only. Every
behavior-changing protocol constant — thresholds, epoch sizes, fee
parameters, rounding precision, the assumed power figure, sampler
version, gate timeouts — lives in ONE canonical table below. Owner
modules READ their constants from this table (get()); the table's era-2
canonical hash is anchored on the ledger as a `parameter_table_recorded`
record, so changing ANY constant is a new table version and a governance
event: parameters marked `mip:<ID>` are pinned by an accepted MIP and
change only through a new MIP; `anchored-config` parameters restate an
anchored configuration record and change only alongside a successor
config record; `chain-decided-era` parameters name era boundaries the
chain itself decided; `frozen-generation` parameters are replayed
history whose change would break anchored re-derivations by design;
everything else is `table-version` (a new anchored table record per
precedent — the record class is the governance path, as it was for the
sampler era, mission verdicts, and the pulse; no MIP is required where
no MIP pinned the value).

Table v1 is VALUE-PRESERVING: it collects the constants exactly as they
were — no anchored hash may move, and the full battery asserts it.

Era rule (chain-decided, the sampler-era pattern): records at ledger
index >= PARAM_TABLE_ERA_FROM_LEDGER_INDEX are governed by the anchored
table; earlier records are pre-table era, and their effective constants
are BY CONSTRUCTION the same values (v1 snapshot), so historic
validation is the identity statement, stated rather than assumed.

The verify layer (verify_everything, `parameter table`) re-derives the
live table, compares its hash against the anchored record, and asserts
every owner module's effective attribute equals the table — refusing BY
NAME on any drifted constant. The self-test proves the refusal with a
silently-edited-constant fixture that must FAIL loudly.

Usage:
    python3 protocol/parameter_table.py --show
    python3 protocol/parameter_table.py --generate   # working doc for anchoring
    python3 protocol/parameter_table.py --selftest
Not financial, legal, or engineering advice.
"""

import sys
sys.dont_write_bytecode = True

import argparse
import hashlib
import importlib
import json
import os

_PROTO_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_PROTO_DIR)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

SCHEMA = "parameter-table/0.1"
TABLE_VERSION = 1
PARAM_TABLE_EVENT = "parameter_table_recorded"
PARAM_TABLE_STATUS = "parameter-table-confirmed"

# Chain-decided era boundary (the SAMPLER_ERA_FROM_LEDGER_INDEX pattern):
# the ledger index of the first anchored parameter-table record. Records at
# or after this index are table-governed; earlier records are pre-table era
# whose effective constants equal table v1 by construction (value-preserving
# snapshot). Set once at anchoring, from the chain.
PARAM_TABLE_ERA_FROM_LEDGER_INDEX = 100

REFUSAL_RULE = ("a live constant that differs from the anchored table at "
                "the current era is DRIFT and refuses by parameter name — "
                "a value change is a new anchored table version, never a "
                "silent edit")

GOVERNANCE_CLASSES = ("table-version", "mip:MIP-0008", "mip:MIP-0009",
                      "chain-decided-era", "anchored-config",
                      "frozen-generation")

# name, value, owner_module, owner_attribute, governance
# Units ride the names (task-law R4 idiom) where a unit exists.
PARAMETERS = (
    ("attest.attestation_version", 1,
     "protocol.attest", "ATTESTATION_VERSION", "table-version"),
    ("challenge.nonce_bytes", 32,
     "protocol.challenge_response", "NONCE_BYTES", "table-version"),
    ("concentration.default_sample_size", 20000,
     "protocol.agent_concentration", "DEFAULT_SAMPLE_SIZE", "table-version"),
    ("concentration.enum_limit", 200000,
     "protocol.agent_concentration", "ENUM_LIMIT", "table-version"),
    ("concentration.epoch_kmax", 6,
     "protocol.agent_concentration", "EPOCH_KMAX", "table-version"),
    ("concentration.sampler_era_from_ledger_index", 96,
     "protocol.agent_concentration", "SAMPLER_ERA_FROM_LEDGER_INDEX",
     "chain-decided-era"),
    ("concentration.sampler_version", "aci-sampler/1.0",
     "protocol.agent_concentration", "SAMPLER_VERSION", "table-version"),
    ("docs.command_timeout_s", 900,
     "protocol.doc_verify", "COMMAND_TIMEOUT_S", "table-version"),
    ("economy.compute_units_per_test_meta", 1,
     "demo.x402_spend_stub", "COMPUTE_UNITS_PER_TEST_META", "table-version"),
    ("economy.daily_compute_spend_test_meta", 1,
     "demo.economy_demo", "DAILY_COMPUTE_SPEND", "frozen-generation"),
    ("economy.daily_earn_test_meta", 2,
     "demo.economy_demo", "DAILY_EARN", "frozen-generation"),
    ("economy.initial_faucet_grant_test_meta", 0,
     "demo.economy_demo", "INITIAL_FAUCET_GRANT", "frozen-generation"),
    ("economy.round_decimals", 6,
     "demo.economy_demo", "ROUND_DECIMALS", "frozen-generation"),
    ("economy.simulated_days", 30,
     "demo.economy_demo", "SIMULATED_DAYS", "frozen-generation"),
    ("flow1.epoch_cap_test_meta", 5.0,
     "demo.flow1_uptime", "EPOCH_CAP", "anchored-config"),
    ("flow1.per_slot_emission_test_meta", 0.5,
     "demo.flow1_uptime", "PER_SLOT_EMISSION", "anchored-config"),
    ("flow1.slot_count", 10,
     "demo.flow1_uptime", "SLOT_COUNT", "anchored-config"),
    ("gate3.challenge_window_entries", 2,
     "protocol.gate3_process", "WINDOW_ENTRIES", "table-version"),
    ("metering.sanity_max_wall_s", 60.0,
     "protocol.external_verifier", "_METERING_SANITY_MAX_WALL_S",
     "table-version"),
    ("power.assumed_cpu_power_w", 15.0,
     "protocol.power_telemetry", "ASSUMED_CPU_POWER_W", "table-version"),
    ("power.char_interval_s", 0.5,
     "protocol.power_telemetry", "CHAR_INTERVAL_S", "table-version"),
    ("power.char_samples", 30,
     "protocol.power_telemetry", "CHAR_SAMPLES", "table-version"),
    ("power.load_response_floor_w", 0.5,
     "protocol.power_telemetry", "LOAD_RESPONSE_FLOOR_W", "table-version"),
    ("power.noise_sd_multiplier", 3.0,
     "protocol.power_telemetry", "NOISE_SD_MULTIPLIER", "table-version"),
    ("power.screen_interval_s", 0.4,
     "protocol.power_telemetry", "SCREEN_INTERVAL_S", "table-version"),
    ("power.screen_samples", 10,
     "protocol.power_telemetry", "SCREEN_SAMPLES", "table-version"),
    ("release_gate.cold_install_timeout_s", 1800,
     "protocol.release_readiness", "COLD_INSTALL_TIMEOUT_S", "table-version"),
    ("task_law.min_asserts", 2,
     "protocol.task_law_check", "MIN_ASSERTS", "mip:MIP-0008"),
    ("task_law.round_decimals", 6,
     "protocol.task_law_check", "ROUND_DECIMALS", "mip:MIP-0009"),
    ("treasury.category_budgets_test_meta", {"reproducible-space-task": 2.0},
     "demo.metastar_treasury", "CATEGORY_BUDGETS", "anchored-config"),
    ("treasury.fee_rate", 0.1,
     "demo.metastar_treasury", "FEE_RATE", "anchored-config"),
    ("treasury.per_bounty_cap_test_meta", 1.0,
     "demo.metastar_treasury", "PER_BOUNTY_CAP", "anchored-config"),
)

_BY_NAME = {p[0]: p for p in PARAMETERS}


def get(name: str):
    """The one read path owner modules use. Refuses an unknown name.
    Mutable values are deep-copied so no consumer can corrupt the table."""
    if name not in _BY_NAME:
        raise KeyError(f"unknown protocol parameter {name!r} — the table is "
                       "the single source; add it via a new table version")
    value = _BY_NAME[name][1]
    if isinstance(value, (dict, list)):
        import copy
        return copy.deepcopy(value)
    return value


# ---------------------------------------------------------------------------
# Canonical document + hash (era-2 rules: sorted, compact, ASCII,
# sign-of-zero-free via the JSON round-trip parse_float hook — no recursion).
# ---------------------------------------------------------------------------
def _sign_safe_zero(obj):
    return json.loads(json.dumps(obj),
                      parse_float=lambda t: 0.0 if float(t) == 0.0
                      else float(t))


def canonical_json(doc: dict) -> str:
    return json.dumps(_sign_safe_zero(doc), sort_keys=True,
                      separators=(",", ":"), ensure_ascii=True)


def table_hash(doc: dict) -> str:
    return hashlib.sha256(canonical_json(doc).encode("utf-8")).hexdigest()


def build_table_doc() -> dict:
    """The canonical table document (what gets hashed and anchored)."""
    return {
        "schema": SCHEMA,
        "table_version": TABLE_VERSION,
        "value_preserving_note": ("v1 collects every constant at its "
                                  "pre-table value — no anchored hash may "
                                  "move; asserted by the full battery"),
        "refusal_rule": REFUSAL_RULE,
        "parameters": [
            {"name": n, "value": v, "owner_module": m,
             "owner_attribute": a, "governance": g}
            for n, v, m, a, g in PARAMETERS
        ],
        "zero_value": True,
        "no_token": True,
    }


def validate_table(doc: dict):
    """Structural validation. Returns (ok, reasons)."""
    reasons = []
    if doc.get("schema") != SCHEMA:
        reasons.append(f"schema must be {SCHEMA}")
    if not isinstance(doc.get("table_version"), int) or \
            doc.get("table_version") < 1:
        reasons.append("table_version must be a positive integer")
    if doc.get("refusal_rule") != REFUSAL_RULE:
        reasons.append("refusal_rule must be stated verbatim")
    params = doc.get("parameters")
    if not isinstance(params, list) or not params:
        reasons.append("parameters must be a non-empty list")
    else:
        names = [p.get("name") for p in params]
        if names != sorted(names):
            reasons.append("parameters must be sorted by name")
        if len(set(names)) != len(names):
            reasons.append("parameter names must be unique")
        for p in params:
            if p.get("governance") not in GOVERNANCE_CLASSES:
                reasons.append(f"{p.get('name')}: unknown governance class "
                               f"{p.get('governance')!r}")
    if not (doc.get("zero_value") is True and doc.get("no_token") is True):
        reasons.append("zero_value and no_token must both be true")
    return (not reasons, reasons)


def effective_value(owner_module: str, owner_attribute: str):
    """The value the LIVE code actually runs with."""
    mod = importlib.import_module(owner_module)
    if not hasattr(mod, owner_attribute):
        raise ValueError(f"{owner_module}.{owner_attribute} does not exist — "
                         "the table names a constant the code no longer has")
    return getattr(mod, owner_attribute)


def drift_findings(doc: dict = None) -> list:
    """Compare every owner module's effective constant against the table.
    Returns a list of refusal strings, one per drifted constant, BY NAME."""
    if doc is None:
        doc = build_table_doc()
    findings = []
    for p in doc["parameters"]:
        try:
            live = effective_value(p["owner_module"], p["owner_attribute"])
        except (ImportError, ValueError) as exc:
            findings.append(f"{p['name']}: {exc}")
            continue
        if live != p["value"] or type(live) is not type(p["value"]):
            findings.append(
                f"{p['name']}: DRIFT — the anchored table holds "
                f"{p['value']!r} but {p['owner_module']}."
                f"{p['owner_attribute']} is {live!r}; " + REFUSAL_RULE)
    return findings


def era_for_index(ledger_index: int) -> str:
    """Chain-decided era rule for a record at the given index."""
    if ledger_index >= PARAM_TABLE_ERA_FROM_LEDGER_INDEX:
        return "table-era"
    return ("pre-table-era (constants equal table v1 by construction — "
            "the v1 snapshot is value-preserving)")


def rederive(entries) -> dict:
    """Find the latest anchored table record, re-prove it against the live
    code, and return {payload, doc, findings}. Refuses absence and drift."""
    rec = None
    for e in entries:
        p = e.get("payload", {})
        if (p.get("event") == PARAM_TABLE_EVENT
                and p.get("status") == PARAM_TABLE_STATUS):
            rec = p
    if rec is None:
        raise ValueError("no anchored parameter_table_recorded on this "
                         "chain")
    from protocol.work_molecule import find_evidence_file
    path = find_evidence_file(
        f"parameter_table_{rec['table_hash'][:12]}.json")
    if path is None:
        raise ValueError(f"anchored table {rec['table_hash'][:12]} has no "
                         "shipped evidence file")
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    if table_hash(doc) != rec["table_hash"]:
        raise ValueError("evidence file does not hash to the anchored "
                         "table_hash")
    live = build_table_doc()
    findings = []
    if table_hash(live) != rec["table_hash"]:
        anchored_by_name = {p["name"]: p for p in doc["parameters"]}
        for p in live["parameters"]:
            a = anchored_by_name.get(p["name"])
            if a is None:
                findings.append(f"{p['name']}: not in the anchored table — "
                                "a new parameter is a new table version")
            elif a["value"] != p["value"]:
                findings.append(f"{p['name']}: table source edited — "
                                f"anchored {a['value']!r} vs live "
                                f"{p['value']!r}; " + REFUSAL_RULE)
        for name in anchored_by_name:
            if name not in {p["name"] for p in live["parameters"]}:
                findings.append(f"{name}: removed from the live table — "
                                "removal is a new table version")
        if not findings:
            findings.append("table hash drifted in non-parameter fields")
    findings.extend(drift_findings(doc))
    return {"payload": rec, "doc": doc, "findings": findings}


def build_payload() -> dict:
    """The ledger payload for anchoring (scanner-invisible: no top-level
    task_id/task_ids keys)."""
    doc = build_table_doc()
    gov = {}
    for p in doc["parameters"]:
        gov[p["governance"]] = gov.get(p["governance"], 0) + 1
    return {
        "event": PARAM_TABLE_EVENT,
        "status": PARAM_TABLE_STATUS,
        "schema": SCHEMA,
        "table_version": TABLE_VERSION,
        "table_hash": table_hash(doc),
        "parameter_count": len(doc["parameters"]),
        "governance_counts": {k: gov[k] for k in sorted(gov)},
        "governance_note": ("v1 is value-preserving (no behavior change; "
                            "no anchored hash moves). A future value change "
                            "is a NEW anchored table version — via a new "
                            "MIP where the parameter is mip-pinned, via a "
                            "successor config record where anchored-config, "
                            "and via this record class otherwise, per the "
                            "sampler-era / mission-verdict / pulse "
                            "precedent."),
        "era_rule": ("records at ledger index >= this record's index are "
                     "table-governed; earlier records are pre-table era "
                     "whose constants equal v1 by construction"),
        "refusal_rule": REFUSAL_RULE,
        "operator_relationship": "same-operator",
        "zero_value": True,
        "no_token": True,
    }


# ---------------------------------------------------------------------------
def _selftest() -> int:
    print("=== protocol/parameter_table.py self-test (read-only) ===\n")
    checks = []
    doc1, doc2 = build_table_doc(), build_table_doc()
    checks.append(("deterministic: two builds canonicalize byte-identical",
                   canonical_json(doc1) == canonical_json(doc2)))
    ok, reasons = validate_table(doc1)
    checks.append(("the live table validates structurally "
                   f"({len(doc1['parameters'])} parameters)",
                   ok and not reasons))
    checks.append(("parameter names are sorted and unique in the source",
                   [p[0] for p in PARAMETERS]
                   == sorted({p[0] for p in PARAMETERS})))
    try:
        get("no.such.parameter")
        refused = False
    except KeyError as exc:
        refused = "single source" in str(exc)
    checks.append(("an unknown parameter name refuses by name", refused))
    checks.append(("zero drift between the table and every owner module "
                   "(the migration is value-preserving)",
                   drift_findings(doc1) == []))

    # THE FIXTURE THE CLASS EXISTS FOR: silently edit a constant in the
    # live code -> the drift check must FAIL loudly, naming the parameter.
    import protocol.power_telemetry as power_telemetry
    original = power_telemetry.ASSUMED_CPU_POWER_W
    power_telemetry.ASSUMED_CPU_POWER_W = original + 1.0
    try:
        findings = drift_findings(doc1)
        loud = (len(findings) == 1
                and "power.assumed_cpu_power_w" in findings[0]
                and "DRIFT" in findings[0])
    finally:
        power_telemetry.ASSUMED_CPU_POWER_W = original
    checks.append(("a silently-edited constant FAILS loudly by name "
                   "(power.assumed_cpu_power_w fixture)", loud))
    checks.append(("the fixture restored cleanly (no residue)",
                   drift_findings(doc1) == []))

    # A silently edited TABLE source is equally loud (hash + name).
    tampered = build_table_doc()
    for p in tampered["parameters"]:
        if p["name"] == "gate3.challenge_window_entries":
            p["value"] = 3
    checks.append(("a silently-edited table value changes the anchored hash",
                   table_hash(tampered) != table_hash(doc1)))

    checks.append(("era rule: the anchor index opens the table era",
                   era_for_index(PARAM_TABLE_ERA_FROM_LEDGER_INDEX)
                   == "table-era"
                   and "pre-table-era" in era_for_index(
                       PARAM_TABLE_ERA_FROM_LEDGER_INDEX - 1)))
    payload = build_payload()
    checks.append(("payload is scanner-invisible (no top-level task ids) "
                   "and carries the era + refusal rules",
                   "task_id" not in payload and "task_ids" not in payload
                   and payload["refusal_rule"] == REFUSAL_RULE
                   and payload["table_hash"] == table_hash(doc1)))
    checks.append(("every governance class on the record is a known class",
                   set(p[4] for p in PARAMETERS) <= set(GOVERNANCE_CLASSES)))

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
        description="Anchored protocol parameter table (research-stage, "
                    "ZERO-VALUE, no token).")
    parser.add_argument("--show", action="store_true")
    parser.add_argument("--generate", action="store_true",
                        help="write parameter_table.json (working copy for "
                             "external_verifier --anchor-parameter-table)")
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args(argv)
    if args.selftest:
        return _selftest()
    doc = build_table_doc()
    if args.generate:
        out = os.path.join(os.getcwd(), "parameter_table.json")
        with open(out, "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=1)
            f.write("\n")
        print(f"table doc written: {out}")
        print(f"table_hash: {table_hash(doc)}")
        return 0
    print(canonical_json(doc) if not args.show else json.dumps(doc, indent=1))
    print(f"table_hash: {table_hash(doc)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
