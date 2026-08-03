"""trust_vector.py — MetaCoin TRUST VECTOR v0 (schema "trust-vector/0.1").

================== CONCEPT (READ ME) ==================
The Trust Vector T(W) is the paper's per-work evidence vector (§12) as code: for one
Work Molecule it assembles SIX separately-verified components — Integrity,
Reproducibility, indEpendence, Provenance-completeness, Usefulness, verification Cost
— each carrying its own evidence citations and its own limitations.

THE HARD RULE (the paper's, enforced mechanically): there is NO combined scalar.
Trust is not one number; collapsing six incommensurable evidence dimensions into a
score would manufacture false confidence and create a gameable target. The self-test
mechanically asserts that no key matching an aggregation pattern
(overall/combined/score/total/rating/grade/rank) exists anywhere in a vector, and
validation rejects any vector that grows one.

================== HONESTY / SCOPE (READ ME) ==================
Research-stage, ZERO-VALUE, no token, no money, no mainnet, no networking, no payments.

Component honesty, stated once here and again inside every vector:
  * I (integrity) is categorical: deterministic re-run, software-rooted — NO TEE
    exists (open provenance debt), so integrity is proven by re-execution, not
    hardware.
  * E (independence) reports the per-molecule actor count AND cites the anchored
    maximal-concentration baseline (pairwise ACI over all same-operator paths). A
    per-molecule actor count is NOT independence; every recorded path is same-operator.
  * U (usefulness) is the HONEST EMPTY: Gate 3's usefulness JUDGMENT seat is
    vacant (protocol/gate3_process.py provides only the mechanical bounty
    lifecycle with a scripted adjudication — no usefulness judgment), so U is
    always 'not-assessed' — no usefulness judgment exists anywhere in the
    protocol. NOTE: the USEFULNESS_REASON constant below keeps its original
    wording BY DESIGN — it is embedded in every vector and feeds the anchored
    catalog hashes (ledger:23/27), which must re-derive forever; anchored
    phrasing is data, not documentation.
  * C (verification cost) copies the molecule's absorbed metering evidence (energy
    labeled ESTIMATED, never measured) and states the honest, unimpressive truth:
    for these demo tasks verification IS full re-execution, so C_v ≈ C_g (ρ ≈ 1) —
    the complexity ceiling is satisfied trivially and uninformatively.

Determinism: no construction timestamps; every value is copied or counted from the
molecule and the ledger, so two builds are byte-identical. tv_hash is the SHA-256 of
the canonical JSON with the tv_hash field excluded (the WMID anti-circularity
pattern). The catalog follows the idx-17 record-shape precedent:
{"schema": "trust-vector-catalog/0.1", "vector_entries": [{task_id, tv_hash} ...],
"catalog_hash": ...}.

P counting policy (pinned by policy_version): walk the molecule body (all fields
except schema/work_id/provenance_debt), recursing into dicts; each leaf slot is
classified null -> not-captured, [] -> asserted-empty, anything else -> populated
(non-empty lists count as one populated slot). completeness_fraction =
(populated + asserted_empty) / total, rounded to 6 decimals — asserted-empty is an
affirmative claim, not missing evidence.

Standard library only (json, hashlib, os, argparse). Molecule construction is REUSED
from protocol/work_molecule.py (0.3 generation, evidence-bundle discovery, snapshot
or live-ledger source). Not legal, financial, investment, or security-certification
advice.

Usage:
    python3 protocol/trust_vector.py --task task-0002
    python3 protocol/trust_vector.py --all --out tv_catalog.json
    python3 protocol/trust_vector.py --validate tv_catalog.json
    python3 protocol/trust_vector.py --selftest   # temp-only; writes nothing
"""

# Suppress __pycache__/*.pyc so importing protocol modules below leaves no stray files.
import sys
sys.dont_write_bytecode = True

import argparse
import hashlib
import json
import os
import re

# Make `from protocol...` resolve when run directly (repo root on path).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# REUSE existing components — do NOT reimplement them.
from protocol.verifier_cli import TASK_MODULES, normalize_task_id
import protocol.work_molecule as work_molecule

_PROTO_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_LEDGER_PATH = os.path.join(_PROTO_DIR, "ledger_data.jsonl")

SCHEMA_VERSION = "trust-vector/0.1"
CATALOG_SCHEMA_VERSION = "trust-vector-catalog/0.1"
POLICY_VERSION = SCHEMA_VERSION  # each component pins the policy it was built under

# The six component names, in canonical order. Exactly these — no more, no less.
COMPONENTS = ("integrity", "reproducibility", "independence",
              "provenance_completeness", "usefulness", "verification_cost")

# THE HARD RULE, as a mechanical pattern: no key anywhere in a vector may match this
# (an aggregation-shaped name would be a combined trust scalar smuggled in).
_FORBIDDEN_KEY_RE = re.compile(r"overall|combined|score|total|rating|grade|rank",
                               re.IGNORECASE)

_HEX = set("0123456789abcdef")

# Fixed component texts (constants so vectors stay deterministic and greppable).
INTEGRITY_LEVEL = "deterministic-re-run; software-rooted (no TEE)"
USEFULNESS_LEVEL = "not-assessed"
USEFULNESS_REASON = ("Gate 3 (usefulness) is not implemented; no usefulness "
                     "judgment exists in the protocol")
RATIO_NOTE = ("for these demo tasks verification IS full re-execution, so "
              "C_v ≈ C_g (ρ ≈ 1); the complexity ceiling is satisfied "
              "trivially and uninformatively")


# ----------------------------------------------------------------------------
# Canonical JSON + hashes (per-module helper, same discipline as ledger.py)
# ----------------------------------------------------------------------------
def canonical_json(obj) -> str:
    """Canonical JSON: sorted keys, compact separators, ASCII — byte-stable for hashing."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def compute_tv_hash(vector: dict) -> str:
    """SHA-256 hex of the canonical JSON of the vector WITHOUT its tv_hash field
    (the same anti-circularity pattern as the WMID and the ledger entry hash)."""
    content = {k: v for k, v in vector.items() if k != "tv_hash"}
    return hashlib.sha256(canonical_json(content).encode("utf-8")).hexdigest()


def compute_catalog_hash(catalog: dict) -> str:
    """SHA-256 hex of the canonical JSON of the catalog WITHOUT its catalog_hash field."""
    content = {k: v for k, v in catalog.items() if k != "catalog_hash"}
    return hashlib.sha256(canonical_json(content).encode("utf-8")).hexdigest()


def scalar_rule_violations(obj, prefix="") -> list:
    """Every key path in `obj` whose key matches the forbidden aggregation pattern.

    THE HARD RULE's enforcement: an empty return is the mechanical proof that no
    combined trust scalar exists anywhere in the structure.
    """
    hits = []
    if isinstance(obj, dict):
        for k in sorted(obj):
            path = f"{prefix}.{k}" if prefix else k
            if _FORBIDDEN_KEY_RE.search(k):
                hits.append(path)
            hits.extend(scalar_rule_violations(obj[k], path))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            hits.extend(scalar_rule_violations(v, f"{prefix}[{i}]"))
    return hits


# ----------------------------------------------------------------------------
# Molecule access (same discovery as the anchored catalog generation)
# ----------------------------------------------------------------------------
def _build_molecule_for(task_id: str, ledger_path: str,
                        as_of_index: int = None) -> dict:
    """Build the 0.3 molecule with the SAME submission discovery build_catalog uses,
    so the vector describes exactly the anchored catalog generation. `as_of_index`
    is the generation-lock rebuild mode (see work_molecule)."""
    short = normalize_task_id(task_id)
    submission_path = None
    special = work_molecule._CATALOG_SUBMISSIONS.get(short)
    if special is not None:
        submission_path = work_molecule.find_evidence_file(special)
    return work_molecule.build_molecule(short, ledger_path=ledger_path,
                                        submission_path=submission_path,
                                        as_of_index=as_of_index)


def _find_anchor(entries: list, event: str, status: str):
    """Highest-index ledger entry with the given confirmed event/status, or None."""
    found = None
    for e in entries:
        p = e.get("payload") if isinstance(e, dict) else None
        if isinstance(p, dict) and p.get("event") == event and p.get("status") == status:
            found = e
    return found


# ----------------------------------------------------------------------------
# Component builders (every value COPIED or COUNTED — never judged)
# ----------------------------------------------------------------------------
def _component_integrity(molecule: dict) -> dict:
    evidence = [f"ledger:{ve['ledger_index']}"
                for ve in molecule["verification_events"]]
    evidence.append("molecule-debt:hardware_evidence.tee_attestation")
    return {
        "level": INTEGRITY_LEVEL,
        "policy_version": POLICY_VERSION,
        "evidence": evidence,
        "limitations": ("no TEE/hardware attestation exists (open provenance debt); "
                        "integrity rests on deterministic re-execution and the "
                        "hash-chained ledger, both operated by the same operator"),
    }


def _component_reproducibility(molecule: dict) -> dict:
    events = molecule["verification_events"]
    platforms = []
    env = molecule["manifests"].get("environment_summary")
    if isinstance(env, dict) and isinstance(env.get("platform"), str):
        platforms = [env["platform"]]
    return {
        "value": {
            "event_count": len(events),
            "distinct_statuses": sorted({ve.get("status") for ve in events
                                         if isinstance(ve.get("status"), str)}),
            "platforms": platforms,
            "canonical_hash": molecule["result_artifact_hash"],
        },
        "policy_version": POLICY_VERSION,
        "evidence": [f"ledger:{ve['ledger_index']}" for ve in events],
        "limitations": ("facts only: counts and recorded statuses of same-operator "
                        "reproduction events; a matching hash proves reproducibility, "
                        "not who executed the task (hashes can be copied)"),
    }


def _component_independence(molecule: dict, ledger_entries: list) -> dict:
    rels = sorted({a.get("operator_relationship") for a in molecule["actors"]
                   if isinstance(a.get("operator_relationship"), str)})
    value = {
        "distinct_verifier_actor_count": len(molecule["actors"]),
        "operator_relationship": rels[0] if len(rels) == 1 else rels,
    }
    evidence = [f"ledger:{ve['ledger_index']}"
                for ve in molecule["verification_events"]]
    aci = _find_anchor(ledger_entries, "aci_baseline_anchored",
                       "aci-baseline-confirmed")
    if aci is not None:
        value["global_baseline"] = {
            "pairwise_aci": aci["payload"]["pairwise_aci"],
            "source": f"ledger:{aci['index']}",
        }
        evidence.append(f"ledger:{aci['index']}")
    return {
        "value": value,
        "policy_version": POLICY_VERSION,
        "evidence": evidence,
        "limitations": ("all recorded verification paths are same-operator; the "
                        "per-molecule actor count is NOT independence, and the cited "
                        "anchored baseline quantifies maximal concentration"),
    }


def _classify_slots(obj) -> tuple:
    """(populated, asserted_empty, not_captured) leaf-slot counts per the pinned
    P counting policy (see module docstring)."""
    populated = asserted_empty = not_captured = 0
    if isinstance(obj, dict):
        for v in obj.values():
            p, a, n = _classify_slots(v)
            populated += p
            asserted_empty += a
            not_captured += n
    elif obj is None:
        not_captured = 1
    elif isinstance(obj, list) and not obj:
        asserted_empty = 1
    else:
        populated = 1
    return (populated, asserted_empty, not_captured)


def _component_provenance_completeness(molecule: dict) -> dict:
    body = {k: v for k, v in molecule.items()
            if k not in ("schema", "work_id", "provenance_debt")}
    populated, asserted_empty, not_captured = _classify_slots(body)
    denominator = populated + asserted_empty + not_captured
    open_debts = []
    debt_reductions = []
    for d in molecule["provenance_debt"]:
        if "reduced_by" in d:
            debt_reductions.append({"field": d["field"],
                                    "reduced_by": d["reduced_by"]})
        else:
            open_debts.append({"field": d["field"],
                               "severity": d.get("severity", "full")})
    return {
        "value": {
            "populated_count": populated,
            "asserted_empty_count": asserted_empty,
            "not_captured_count": not_captured,
            "completeness_fraction": round(
                (populated + asserted_empty) / denominator, 6),
            "open_debts": open_debts,
            "debt_reductions": debt_reductions,
        },
        "policy_version": POLICY_VERSION,
        "evidence": [f"ledger:{molecule['ledger_anchor']['entry_index']}"],
        "limitations": ("counts follow the pinned leaf-slot policy; completeness of "
                        "RECORDED evidence, not correctness or usefulness — open "
                        "debts are listed, never hidden"),
    }


def _component_usefulness() -> dict:
    return {
        "level": USEFULNESS_LEVEL,
        "reason": USEFULNESS_REASON,
        "policy_version": POLICY_VERSION,
        "evidence": [],  # asserted-empty: there is genuinely nothing to cite
        "limitations": ("the honest empty: any usefulness value here would be "
                        "fabricated; Gate 3 remains future work"),
    }


def _component_verification_cost(molecule: dict) -> dict:
    energy = molecule.get("energy_and_compute_evidence")
    evidence = []
    if isinstance(energy, dict):
        evidence = [energy["source"]]
        if "wall_time_s" in energy:
            value = {
                "scope": "per-task",
                "wall_time_s": energy["wall_time_s"],
                "cpu_time_s": energy["cpu_time_s"],
                "energy_j_estimate": energy["energy_j_estimate"],
                "labels": dict(energy.get("labels", {})),
                "ratio_note": RATIO_NOTE,
            }
            limitations = ("wall/CPU measured, energy ESTIMATED (assumed constant "
                           "power; no hardware telemetry — open debt); timing is "
                           "non-reproducible, the cited anchor fixes the claim")
        else:
            totals = energy.get("aggregate_totals", {})
            value = {
                "scope": "catalog-aggregate",
                "wall_time_s": totals.get("total_wall_time_s"),
                "cpu_time_s": totals.get("total_cpu_time_s"),
                "energy_j_estimate": totals.get("total_energy_j_estimate"),
                "labels": dict(energy.get("labels", {})),
                "ratio_note": RATIO_NOTE,
            }
            limitations = ("aggregate-citation mode: per-task figures were not "
                           "resolvable locally, so the catalog-wide anchored totals "
                           "are cited; energy ESTIMATED, timing non-reproducible")
    else:
        value = {"scope": "not-captured", "ratio_note": RATIO_NOTE}
        limitations = ("no metering evidence is anchored for this ledger; "
                       "verification cost remains open provenance debt")
    return {
        "value": value,
        "policy_version": POLICY_VERSION,
        "evidence": evidence,
        "limitations": limitations,
    }


# ----------------------------------------------------------------------------
# Vector + catalog construction
# ----------------------------------------------------------------------------
def build_vector(task_id: str, ledger_path: str = DEFAULT_LEDGER_PATH,
                 as_of_index: int = None) -> dict:
    """Build T(W) for one task's 0.3 molecule. Deterministic; raises on bad input.
    `as_of_index` is the generation-lock rebuild mode: a catalog anchored at ledger
    index N rebuilds exactly with as_of_index = N - 1."""
    molecule = _build_molecule_for(task_id, ledger_path, as_of_index=as_of_index)
    ok, reasons = work_molecule.validate(molecule, ledger_path=ledger_path)
    if not ok:
        raise ValueError(f"molecule for {task_id} does not validate: {reasons}")
    entries = work_molecule._read_ledger(ledger_path)
    if as_of_index is not None:
        entries = [e for e in entries
                   if isinstance(e.get("index"), int) and e["index"] <= as_of_index]
    vector = {
        "schema": SCHEMA_VERSION,
        "task_id": molecule["task_spec"]["task_id"],
        "work_id": molecule["work_id"],
        "molecule_schema": molecule["schema"],
        "components": {
            "integrity": _component_integrity(molecule),
            "reproducibility": _component_reproducibility(molecule),
            "independence": _component_independence(molecule, entries),
            "provenance_completeness": _component_provenance_completeness(molecule),
            "usefulness": _component_usefulness(),
            "verification_cost": _component_verification_cost(molecule),
        },
    }
    violations = scalar_rule_violations(vector)
    if violations:  # construction can never emit an aggregation-shaped key
        raise ValueError(f"scalar rule violated at: {violations}")
    vector["tv_hash"] = compute_tv_hash(vector)
    return vector


def build_tv_catalog(ledger_path: str = DEFAULT_LEDGER_PATH, task_ids=None,
                     as_of_index: int = None) -> dict:
    """Build + validate the vector for every known task; return the catalog dict
    ({schema, vector_entries, catalog_hash} — the idx-17 record-shape precedent).
    `as_of_index` is the generation-lock rebuild mode (see build_vector).

    REGISTERED-BUT-UNRECORDED TASKS ARE SKIPPED when the default roster is in
    effect (the work_molecule.build_catalog / cut_certificate._molecule_pool
    semantics): a vector derives from a molecule, and a molecule requires citing
    records — this keeps anchored generations pinned to their historical roster
    by chain state alone as the registry grows. An explicit `task_ids` list
    still raises for an unrecorded task."""
    skip_unrecorded = task_ids is None
    if task_ids is None:
        task_ids = sorted(TASK_MODULES)
    vector_entries = []
    for tid in sorted(normalize_task_id(t) for t in task_ids):
        try:
            vector = build_vector(tid, ledger_path=ledger_path,
                                  as_of_index=as_of_index)
        except ValueError as exc:
            if skip_unrecorded and "no ledger entries reference" in str(exc):
                continue
            raise
        ok, reasons = validate_vector(vector, ledger_path=ledger_path)
        if not ok:
            raise ValueError(f"vector for {tid} does not validate: {reasons}")
        vector_entries.append({"task_id": tid, "tv_hash": vector["tv_hash"]})
    catalog = {
        "schema": CATALOG_SCHEMA_VERSION,
        "vector_entries": vector_entries,  # already sorted by task_id
    }
    catalog["catalog_hash"] = compute_catalog_hash(catalog)
    return catalog


# ----------------------------------------------------------------------------
# Validation (mechanical, no LLM)
# ----------------------------------------------------------------------------
def validate_vector(vector, ledger_path: str = None):
    """Mechanically validate a trust vector. Returns (ok, reasons).

    Checks: exact top-level shape; exactly the six components, each with
    policy_version/evidence/limitations and a level or value; THE HARD RULE (no
    aggregation-shaped key anywhere); U is always the honest empty; tv_hash
    recomputes; with a ledger, every "ledger:N" evidence citation names an entry
    whose stored hash re-derives from its content.
    """
    reasons = []
    if not isinstance(vector, dict):
        return (False, ["vector is not a JSON object"])
    expected_keys = {"schema", "task_id", "work_id", "molecule_schema",
                     "components", "tv_hash"}
    if set(vector.keys()) != expected_keys:
        return (False, [f"top-level keys must be exactly {sorted(expected_keys)} "
                        f"(got {sorted(vector.keys())})"])
    if vector["schema"] != SCHEMA_VERSION:
        reasons.append(f"schema must be {SCHEMA_VERSION!r}")
    if vector["molecule_schema"] != work_molecule.SCHEMA_VERSION_03:
        reasons.append(f"molecule_schema must be "
                       f"{work_molecule.SCHEMA_VERSION_03!r}")
    wid = vector["work_id"]
    if not (isinstance(wid, str) and len(wid) == 64
            and all(c in _HEX for c in wid)):
        reasons.append("work_id must be a 64-char lowercase hex sha256")

    comps = vector["components"]
    if not isinstance(comps, dict) or set(comps.keys()) != set(COMPONENTS):
        reasons.append(f"components must be exactly {sorted(COMPONENTS)}")
    else:
        for name in COMPONENTS:
            c = comps[name]
            if not isinstance(c, dict):
                reasons.append(f"components.{name} is not an object")
                continue
            if c.get("policy_version") != POLICY_VERSION:
                reasons.append(f"components.{name}.policy_version must be "
                               f"{POLICY_VERSION!r}")
            if not isinstance(c.get("evidence"), list):
                reasons.append(f"components.{name}.evidence must be a list")
            if not isinstance(c.get("limitations"), str) or not c.get("limitations"):
                reasons.append(f"components.{name}.limitations must be a non-empty "
                               "string")
            if "level" not in c and "value" not in c:
                reasons.append(f"components.{name} must carry a level or a value")
        u = comps.get("usefulness")
        if isinstance(u, dict) and (u.get("level") != USEFULNESS_LEVEL
                                    or u.get("reason") != USEFULNESS_REASON):
            reasons.append("usefulness must be the honest empty: level "
                           f"{USEFULNESS_LEVEL!r} with its fixed reason (no "
                           "usefulness judgment exists in the protocol)")

    # THE HARD RULE: no combined scalar anywhere.
    violations = scalar_rule_violations(vector)
    for v in violations:
        reasons.append(f"scalar rule violated: aggregation-shaped key at {v} — "
                       "no combined trust score may exist, by design")

    if compute_tv_hash(vector) != vector.get("tv_hash"):
        reasons.append("tv_hash does not recompute from content")

    # ledger recheck: every cited entry exists and its hash re-derives
    if ledger_path is not None and not reasons:
        try:
            entries = work_molecule._read_ledger(ledger_path)
        except (ValueError, json.JSONDecodeError) as exc:
            entries = None
            reasons.append(f"ledger recheck impossible: {exc}")
        if entries is not None:
            by_index = {e.get("index"): e for e in entries if isinstance(e, dict)}
            for name in COMPONENTS:
                for cite in comps[name].get("evidence", []):
                    if not (isinstance(cite, str) and cite.startswith("ledger:")):
                        continue
                    try:
                        idx = int(cite.split(":", 1)[1])
                    except ValueError:
                        reasons.append(f"components.{name} cites malformed "
                                       f"{cite!r} — citations must have the "
                                       "form 'ledger:<int>'")
                        continue
                    entry = by_index.get(idx)
                    if entry is None:
                        reasons.append(f"components.{name} cites ledger entry {idx}, "
                                       "which does not exist")
                        continue
                    rederived = work_molecule._ledger_entry_hash(entry)
                    if rederived != entry.get("hash"):
                        reasons.append(f"cited ledger entry {idx} hash does not "
                                       "re-derive from its content")

    return (not reasons, reasons)


def validate_catalog(catalog):
    """Structurally validate a trust-vector catalog file. Returns (ok, reasons)."""
    reasons = []
    if not isinstance(catalog, dict):
        return (False, ["catalog is not a JSON object"])
    if set(catalog.keys()) != {"schema", "vector_entries", "catalog_hash"}:
        return (False, ["top-level keys must be exactly "
                        "{schema, vector_entries, catalog_hash}"])
    if catalog["schema"] != CATALOG_SCHEMA_VERSION:
        reasons.append(f"schema must be {CATALOG_SCHEMA_VERSION!r}")
    entries = catalog["vector_entries"]
    if not isinstance(entries, list) or not entries:
        reasons.append("vector_entries must be a non-empty array")
        entries = []
    seen = []
    for i, e in enumerate(entries):
        if not isinstance(e, dict) or set(e.keys()) != {"task_id", "tv_hash"}:
            reasons.append(f"vector_entries[{i}] must be exactly {{task_id, tv_hash}}")
            continue
        h = e["tv_hash"]
        if not (isinstance(h, str) and len(h) == 64
                and all(c in _HEX for c in h)):
            reasons.append(f"vector_entries[{i}].tv_hash must be a 64-char "
                           "lowercase hex sha256")
        try:
            normalize_task_id(e["task_id"])
        except (KeyError, TypeError) as exc:
            reasons.append(f"vector_entries[{i}] unknown task_id: {exc}")
        seen.append(e["task_id"])
    if seen != sorted(seen) or len(seen) != len(set(seen)):
        reasons.append("vector_entries must be unique and sorted by task_id")
    if not reasons and compute_catalog_hash(catalog) != catalog["catalog_hash"]:
        reasons.append("catalog_hash does not recompute from content")
    return (not reasons, reasons)


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="trust_vector.py",
        description=(
            "MetaCoin Trust Vector v0 (research-stage, ZERO-VALUE, no token). Six "
            "separately-verified evidence components per work — NO combined scalar, "
            "by mechanical rule."
        ),
        epilog=(
            "HONESTY: all evidence is same-operator (E cites the anchored maximal-"
            "concentration baseline); U is always 'not-assessed' (Gate 3's "
            "judgment seat is vacant); energy in C is ESTIMATED. A matching hash proves "
            "deterministic re-derivability, not trustworthiness. Not consensus, "
            "not payment, not a token."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--task", help="build the trust vector for one task")
    mode.add_argument("--all", action="store_true",
                      help="build all vectors and write the tv catalog")
    mode.add_argument("--validate", metavar="FILE",
                      help="validate a vector or catalog JSON file (add --ledger "
                           "to recheck citations)")
    mode.add_argument("--selftest", action="store_true",
                      help="run the mechanical self-test (temp files only)")
    parser.add_argument("--ledger", default=DEFAULT_LEDGER_PATH,
                        help=f"ledger source: live JSONL or a published snapshot "
                             f"(default: {DEFAULT_LEDGER_PATH})")
    parser.add_argument("--out",
                        help="with --all: write the catalog here "
                             "(default: tv_catalog.json; gitignored)")
    args = parser.parse_args(argv)

    if args.selftest or not (args.task or args.all or args.validate):
        return _selftest()

    if args.task:
        try:
            vector = build_vector(args.task, ledger_path=args.ledger)
        except (KeyError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(json.dumps(vector, indent=2, sort_keys=True))
        return 0

    if args.all:
        try:
            catalog = build_tv_catalog(ledger_path=args.ledger)
        except (KeyError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        text = json.dumps(catalog, indent=2, sort_keys=True)
        print(text)
        out = args.out or "tv_catalog.json"
        with open(out, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        print(f"wrote trust-vector catalog ({len(catalog['vector_entries'])} "
              f"vectors) to {out}", file=sys.stderr)
        return 0

    with open(args.validate, "r", encoding="utf-8") as f:
        doc = json.load(f)
    if isinstance(doc, dict) and doc.get("schema") == CATALOG_SCHEMA_VERSION:
        ok, reasons = validate_catalog(doc)
    else:
        ok, reasons = validate_vector(doc, ledger_path=args.ledger)
    print(f"validation: {'VALID' if ok else 'INVALID'}")
    for r in reasons:
        print(f"  - {r}")
    return 0 if ok else 1


# ============================== SELF-TEST ====================================
def _selftest() -> int:
    """Mechanical self-test. Temp files only; writes nothing into the repo."""
    import copy
    import shutil
    import tempfile

    from protocol.ledger import Ledger
    import protocol.audit as audit
    import protocol.external_verifier as external_verifier
    import protocol.verifier_cli as verifier_cli

    print("=== protocol/trust_vector.py self-test (mechanical; temp files only) ===")
    print("Six components, separately cited; NO combined scalar, by mechanical rule.\n")

    root_before = set(os.listdir(_REPO_ROOT))
    proto_before = set(os.listdir(_PROTO_DIR))

    checks = []  # (name, passed)
    tmp_dir = tempfile.mkdtemp(prefix=f"trust_vector_selftest_{os.getpid()}_")
    try:
        # --- fixture ledger: genesis + task-0001 eval + metering + ACI anchors --------
        fixture_ledger = os.path.join(tmp_dir, "ledger_fixture.jsonl")
        led = Ledger(fixture_ledger)
        led.append({"event": "ledger_genesis", "note": "selftest fixture",
                    "stage": "R-selftest", "zero_value": True, "no_token": True})
        external_verifier.evaluate_submission(
            verifier_cli.build_submission(
                "task-0001", "selftest-same-operator (simulated)",
                topology="same-machine-self-recompute"),
            led,
        )
        fixture_labels = {"wall": "measured", "cpu": "measured",
                          "energy": "estimated"}
        real_hash = verifier_cli.load_task("task-0001").output_hash(
            verifier_cli.load_task("task-0001").compute())
        fixture_report = {
            "schema": "metering-report/0.1",
            "assumed_cpu_power_w": 15.0,
            "power_method": "assumed-nameplate; no hardware telemetry on this host",
            "per_task": [{
                "task_id": "task-0001", "output_hash": real_hash,
                "wall_time_s": 0.001, "cpu_time_s": 0.0008,
                "energy_j_estimate": 0.012, "labels": dict(fixture_labels),
            }],
        }
        fixture_report["report_hash"] = hashlib.sha256(canonical_json(
            {k: v for k, v in fixture_report.items() if k != "report_hash"}
        ).encode("utf-8")).hexdigest()
        report_path = os.path.join(tmp_dir, "metering_report_fixture.json")
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(fixture_report, f)
        led.append({
            "event": "metering_evidence_anchored",
            "status": "metering-evidence-confirmed",
            "stage": "R-provenance-debt", "topology": "same-machine-metering",
            "report_schema": "metering-report/0.1",
            "report_hash": fixture_report["report_hash"],
            "task_count": 1, "assumed_cpu_power_w": 15.0,
            "power_method": "assumed-nameplate; no hardware telemetry on this host",
            "labels": dict(fixture_labels),
            "total_wall_time_s": 0.001, "total_cpu_time_s": 0.0008,
            "total_energy_j_estimate": 0.012,
            "operator_relationship": "same-operator",
            "limitation_note": "selftest fixture", "zero_value": True,
            "no_token": True,
        })
        aci_entry = led.append({
            "event": "aci_baseline_anchored", "status": "aci-baseline-confirmed",
            "stage": "R-concentration", "topology": "same-machine-aci-measurement",
            "pairwise_aci": 0.99, "path_count": 1, "report_hash": "e" * 64,
            "limitation_note": "selftest fixture", "zero_value": True,
            "no_token": True,
        })

        # The fixture molecule needs the fixture metering report resolvable via the
        # DEFAULT discovery... it is not (temp path), so build_vector's molecule
        # falls back to aggregate-citation mode UNLESS we build via an explicit
        # molecule. Vector construction goes through build_molecule's default
        # discovery, so component C exercises aggregate mode here (per-task mode is
        # covered by work_molecule's own selftest) — both modes are deterministic.

        # [1] build + validate (with ledger citation recheck)
        v1 = build_vector("task-0001", ledger_path=fixture_ledger)
        ok, reasons = validate_vector(v1, ledger_path=fixture_ledger)
        checks.append(("fixture vector validates (with citation recheck)", ok))
        if not ok:
            for r in reasons:
                print(f"    unexpected: {r}")

        # [2] determinism: two builds byte-identical
        v2 = build_vector("task-0001", ledger_path=fixture_ledger)
        checks.append(("two builds byte-identical (canonical JSON)",
                       canonical_json(v1) == canonical_json(v2)))

        # [3] THE HARD RULE: no aggregation-shaped key exists; a poisoned copy is
        # mechanically detected and rejected
        checks.append(("no combined scalar anywhere (mechanical scan)",
                       scalar_rule_violations(v1) == []))
        poisoned = copy.deepcopy(v1)
        poisoned["components"]["integrity"]["overall_trust_score"] = 0.99
        poisoned["tv_hash"] = compute_tv_hash(poisoned)
        ok, _ = validate_vector(poisoned)
        checks.append(("a smuggled combined score is rejected", not ok))

        # [4] U is always the honest empty
        u = v1["components"]["usefulness"]
        checks.append(("usefulness is 'not-assessed' with the fixed reason and "
                       "asserted-empty evidence",
                       u["level"] == USEFULNESS_LEVEL
                       and u["reason"] == USEFULNESS_REASON
                       and u["evidence"] == []))

        # [5] E cites the anchored concentration baseline from the ledger
        e = v1["components"]["independence"]["value"]
        checks.append(("E cites the ledger's ACI baseline (value + source)",
                       e.get("global_baseline", {}).get("pairwise_aci") == 0.99
                       and e.get("global_baseline", {}).get("source") ==
                       f"ledger:{aci_entry['index']}"))

        # [6] P reflects the molecule's debt state (energy reduced via the metering
        # anchor -> debt_reduction listed; TEE debt still open)
        p = v1["components"]["provenance_completeness"]["value"]
        checks.append(("P lists the energy debt_reduction with its citation and "
                       "keeps the open TEE debt",
                       any(dr["field"] == "energy_and_compute_evidence"
                           and dr["reduced_by"].startswith("ledger:")
                           for dr in p["debt_reductions"])
                       and any(od["field"] == "hardware_evidence.tee_attestation"
                               for od in p["open_debts"])))

        # [7] C copies the absorbed metering evidence with the estimated label and
        # the honest trivial-rho note
        c = v1["components"]["verification_cost"]["value"]
        checks.append(("C carries estimated-energy labels + the honest "
                       "trivial-rho ratio_note",
                       c["labels"].get("energy") == "estimated"
                       and c["ratio_note"] == RATIO_NOTE
                       and c["scope"] in ("per-task", "catalog-aggregate")))

        # [8] tampered evidence citation is caught by the chain recheck
        t = copy.deepcopy(v1)
        t["components"]["integrity"]["evidence"][0] = "ledger:999"
        t["tv_hash"] = compute_tv_hash(t)
        ok, _ = validate_vector(t, ledger_path=fixture_ledger)
        checks.append(("forged evidence citation is detected (chain recheck)",
                       not ok))

        # [9] snapshot-source equivalence: vector from a published snapshot of the
        # fixture ledger is byte-identical
        snap = os.path.join(tmp_dir, "fixture_published.json")
        audit.export_snapshot(fixture_ledger, snap)
        v_snap = build_vector("task-0001", ledger_path=snap)
        checks.append(("snapshot-source vector byte-identical to live-ledger vector",
                       canonical_json(v_snap) == canonical_json(v1)))

        # [10] catalog: deterministic, anti-circular, idx-17-precedent shape
        c1 = build_tv_catalog(ledger_path=fixture_ledger, task_ids=["task-0001"])
        c2 = build_tv_catalog(ledger_path=fixture_ledger, task_ids=["task-0001"])
        ok, reasons = validate_catalog(c1)
        checks.append(("tv catalog: shape + determinism + hash recompute",
                       ok and canonical_json(c1) == canonical_json(c2)
                       and c1["vector_entries"][0]["tv_hash"] == v1["tv_hash"]))
        if not ok:
            for r in reasons:
                print(f"    unexpected: {r}")

        # [11] real-ledger build (READ-ONLY) when the runtime ledger exists locally
        if os.path.exists(DEFAULT_LEDGER_PATH):
            rv = build_vector("task-0002", ledger_path=DEFAULT_LEDGER_PATH)
            ok, reasons = validate_vector(rv, ledger_path=DEFAULT_LEDGER_PATH)
            checks.append(("real-ledger vector (task-0002) validates", ok))
            if not ok:
                for r in reasons:
                    print(f"    unexpected: {r}")
            real_snap = os.path.join(_PROTO_DIR, "ledger_published.json")
            if os.path.exists(real_snap):
                rv_snap = build_vector("task-0002", ledger_path=real_snap)
                checks.append(("real snapshot-source vector byte-identical",
                               canonical_json(rv_snap) == canonical_json(rv)))
        else:
            print("    (no runtime ledger present — real-ledger build SKIPPED; "
                  "fixture checks above cover the same paths)")
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
    if stray_root:
        print(f"    stray in repo root: {stray_root}")
    if stray_proto:
        print(f"    stray in protocol/: {stray_proto}")

    ok = failures == 0
    print("\n=== self-test summary: " +
          ("ALL CHECKS BEHAVED CORRECTLY" if ok else "FAILURE — see above") + " ===")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
