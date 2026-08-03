"""prov_export.py — PROV-compatible Work Molecule export (schema "prov-export/0.1").

================== HONESTY / SCOPE (READ ME) ==================
Research-stage, ZERO-VALUE, no token, no money, no mainnet, no networking, no payments.

This module answers the paper's open interoperability item: express a Work Molecule
in W3C PROV core concepts as a plain PROV-JSON-shaped document (stdlib only; no
external vocabulary is fetched, no PROV library is used). The mapping:

    task execution        -> prov:Activity (one per molecule)
    result artifact       -> prov:Entity, prov:wasGeneratedBy the execution
    task spec + sources   -> prov:Entity, prov:used by the execution
    actors                -> prov:Agent, prov:wasAssociatedWith (with prov:role)
    parent_work_ids       -> prov:wasDerivedFrom (result derived from parent
                             work-molecule entities)
    verification events   -> SEPARATE prov:Activity records that prov:used the
                             result entity. DELIBERATE MODELING CHOICE: a
                             verification examines an already-generated artifact;
                             it does not generate it and is not part of the
                             generating activity, so it is its own activity whose
                             only PROV link is `used` on the primary entity.
                             Challenge events map the same way (their own class).
    ledger anchor         -> prov:Entity carrying metacoin:ledgerAnchor — a
                             NAMESPACED custom attribute, because PROV has no
                             anchoring concept; the primary entity points at it
                             via a metacoin:ledgerAnchor reference, never via an
                             invented PROV relation.

HEADLINE MAPPING CAVEAT (the semantic gap): PROV makes the OPEN-WORLD assumption —
absence of a statement reads as UNKNOWN. The molecule's three-state honesty rule
distinguishes asserted-empty ([] — we affirmatively claim none exist) from
not-captured (null + a machine-readable provenance_debt entry). A naive translation
would silently collapse both into "unknown". So:
    asserted-empty        -> explicit metacoin:assertedEmpty attribute on the
                             execution activity, listing the molecule fields
                             affirmatively asserted empty;
    provenance_debt and   -> metacoin:provenanceDebt entities, NEVER dropped
    debt_reduction           (debt_reduction keeps its history: original reason,
                             reducing citation, what remains open).
A plain PROV consumer that ignores metacoin:* extensions loses exactly this honesty
layer — that is the semantic gap of this export, stated in mapping_caveats inside
every export.

NOTHING IN THE EXPORT CLAIMS MORE THAN THE MOLECULE DOES:
  * No invented PROV relations. The only relation containers ever emitted are the
    _RELATION_ALLOWLIST below; the self-test scans an export's every nested key
    against the known-PROV-relation list to prove no others appear.
  * No prov:startTime/endTime — the molecule records coarse EVENT timestamps (the
    execution_records partial debt), not activity intervals; event times are
    carried verbatim as metacoin:eventTimestamp.
  * No executor association is fabricated: if no actor carries an executing role,
    the execution activity simply has no wasAssociatedWith (matching the molecule).
  * Every metacoin:* extension used is listed in the export's own `extensions`
    manifest with a one-line meaning; the validator checks the manifest is exactly
    the set of extensions used (no unlisted usage, no unused listing).

DETERMINISM: an export contains NO construction-time timestamp; every value is a
verbatim copy from the molecule. The same molecule exports byte-identically twice,
and the source WMID is cited inside (source_work_id).

Standard library only (json, argparse, os, sys; tempfile in the self-test).
Molecule construction is REUSED from protocol/work_molecule.py (build_molecule) —
nothing is reassembled here; ledger resolution follows verify_everything.py's
pattern (live ledger when present, else the published snapshot). Not legal,
financial, investment, or security-certification advice.

Usage:
    python3 protocol/prov_export.py --export task-0002                  # writes prov_task-0002.json
    python3 protocol/prov_export.py --export task-0002 --as-of 17
    python3 protocol/prov_export.py --export-catalog                    # all tasks
    python3 protocol/prov_export.py --export-catalog --generation 0.2
    python3 protocol/prov_export.py --validate prov_task-0002.json
    python3 protocol/prov_export.py --selftest                          # temp-only
"""

# Suppress __pycache__/*.pyc so importing protocol modules below leaves no stray files.
import sys
sys.dont_write_bytecode = True

import argparse
import json
import os

_PROTO_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_PROTO_DIR)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# REUSE molecule construction and the task registry — do NOT reassemble either.
from protocol.work_molecule import (build_molecule, SCHEMA_VERSION_02,
                                    SCHEMA_VERSION_03, DEFAULT_SCHEMA_VERSION,
                                    SUPPORTED_SCHEMAS)
from protocol.verifier_cli import TASK_MODULES, normalize_task_id

EXPORT_SCHEMA = "prov-export/0.1"

# Ledger resolution (verify_everything.py's pattern): live when present
# (coordinator machine), else the published snapshot (fresh clone) — entry
# content is identical either way.
DEFAULT_LIVE_LEDGER = os.path.join(_PROTO_DIR, "ledger_data.jsonl")
DEFAULT_SNAPSHOT = os.path.join(_PROTO_DIR, "ledger_published.json")


def resolve_ledger_path():
    return DEFAULT_LIVE_LEDGER if os.path.exists(DEFAULT_LIVE_LEDGER) \
        else DEFAULT_SNAPSHOT


# The ONLY PROV relations this export ever emits. Anything else would be a claim
# the molecule does not make.
_RELATION_ALLOWLIST = ("used", "wasAssociatedWith", "wasDerivedFrom",
                       "wasGeneratedBy")

# Known PROV-DM relation names OUTSIDE the allowlist; the validator (and the
# self-test's key scan) prove none of these appear anywhere in an export.
_FORBIDDEN_PROV_RELATIONS = (
    "actedOnBehalfOf", "alternateOf", "hadMember", "hadPrimarySource",
    "mentionOf", "specializationOf", "wasAttributedTo", "wasEndedBy",
    "wasInfluencedBy", "wasInformedBy", "wasInvalidatedBy", "wasQuotedFrom",
    "wasRevisionOf", "wasStartedBy",
)

# Structural (non-relation) top-level keys of an export document.
_STRUCTURAL_KEYS = ("schema", "source_work_id", "source_molecule_schema",
                    "prefix", "extensions", "mapping_caveats",
                    "entity", "activity", "agent")

# Master registry of every metacoin:* extension this exporter can emit, with its
# one-line meaning. Each export's `extensions` manifest is EXACTLY the subset used
# in that document (validated both directions).
_EXTENSION_REGISTRY = {
    "metacoin:assertedEmpty":
        "molecule fields affirmatively asserted empty ([]) — PROV's open-world "
        "assumption would otherwise read their absence as unknown",
    "metacoin:provenanceDebt":
        "prov:type of an entity carrying a not-captured gap (or its reduction "
        "history) — debt is never dropped in translation",
    "metacoin:debtRecord":
        "verbatim copy of one provenance_debt / debt_reduction entry",
    "metacoin:debtState":
        "'open' (debt entry) or 'reduced' (debt_reduction entry, history kept)",
    "metacoin:ledgerAnchor":
        "PROV has no anchoring concept: on the anchor entity, its prov:type; on "
        "the result entity, a reference to the anchor entity",
    "metacoin:entryIndex": "ledger entry index of the anchor entry",
    "metacoin:entryHash": "ledger entry hash (chain citation)",
    "metacoin:taskExecution": "prov:type of the task-execution activity",
    "metacoin:verificationEvent":
        "prov:type of a verification activity (deliberate modeling: verifications "
        "are separate activities that prov:used the result entity)",
    "metacoin:challengeEvent":
        "prov:type of a challenge activity (same modeling as verifications)",
    "metacoin:eventName": "molecule event name, verbatim (e.g. "
                          "agent_verifier_attestation)",
    "metacoin:eventRecord": "verbatim copy of the molecule's event record",
    "metacoin:eventTimestamp":
        "coarse event timestamp copied from execution_records — deliberately NOT "
        "prov:startTime/endTime (the molecule records events, not intervals; see "
        "the execution_records partial debt)",
    "metacoin:ledgerIndex": "ledger index the event record cites",
    "metacoin:resultArtifact": "prov:type of the primary result entity",
    "metacoin:sha256": "content hash of the result artifact",
    "metacoin:taskSpec": "prov:type of the task-spec entity",
    "metacoin:specRecord": "verbatim copy of the molecule's task_spec group",
    "metacoin:sourceArtifact": "prov:type of an external source-artifact entity",
    "metacoin:workMolecule":
        "prov:type of a parent work-molecule entity (wasDerivedFrom target)",
    "metacoin:workMoleculeId": "the parent molecule's WMID",
    "metacoin:kind": "actor kind, verbatim (e.g. autonomous-agent)",
    "metacoin:operatorRelationship":
        "declared operator relationship, verbatim — the independence honesty label",
    "metacoin:roles": "actor roles, verbatim",
    "metacoin:executionRecords":
        "verbatim copy of the molecule's coarse execution timeline (partial debt "
        "applies: no durations, exit codes, or step traces)",
    "metacoin:energyAndComputeEvidence":
        "verbatim energy/compute evidence including its measured/estimated labels "
        "— labels are never upgraded in translation",
    "metacoin:hardwareEvidence":
        "verbatim hardware evidence (fingerprints; tee_attestation null = debt)",
    "metacoin:manifests": "verbatim model/tool/environment manifests group",
    "metacoin:scopeAndLimitations":
        "verbatim scope/limitation honesty block from the molecule",
}

_OPEN_WORLD_CAVEAT = (
    "HEADLINE SEMANTIC GAP: PROV's open-world assumption reads the ABSENCE of a "
    "statement as unknown, but the work molecule distinguishes asserted-empty "
    "([] — we affirmatively claim none exist) from not-captured (null + a "
    "provenance_debt entry). Translation therefore carries asserted-empty fields "
    "as an explicit metacoin:assertedEmpty attribute and every debt entry as a "
    "metacoin:provenanceDebt entity. A plain PROV consumer that ignores "
    "metacoin:* extensions loses exactly this honesty layer."
)
_VERIFICATION_CAVEAT = (
    "DELIBERATE MODELING CHOICE: verification and challenge events are SEPARATE "
    "prov:Activity records linked to the primary result entity via prov:used — "
    "they examine an already-generated artifact; they neither generate it nor "
    "belong to the generating activity."
)
_TIME_CAVEAT = (
    "No prov:startTime/endTime anywhere: the molecule records coarse EVENT "
    "timestamps (execution_records, a declared partial debt), not activity "
    "intervals. Event times are carried verbatim as metacoin:eventTimestamp."
)
_AGENT_CAVEAT = (
    "No executor association is fabricated: agents are wasAssociatedWith the "
    "activities their molecule roles actually tie them to (verifier_id matches); "
    "if no actor carries an executing role, the execution activity has no "
    "wasAssociatedWith — mirroring the molecule, not an omission of this export."
)


# ----------------------------------------------------------------------------
# molecule -> PROV mapping
# ----------------------------------------------------------------------------
def _asserted_empty_paths(obj, prefix=""):
    """Dotted paths of every asserted-empty ([]) field in the molecule, mirroring
    the molecule's own three-state walk (dicts recursed; [] collected)."""
    paths = []
    if isinstance(obj, dict):
        for key in sorted(obj):
            sub = f"{prefix}.{key}" if prefix else key
            value = obj[key]
            if value == []:
                paths.append(sub)
            elif isinstance(value, dict):
                paths.extend(_asserted_empty_paths(value, sub))
    return paths


def build_prov_export(molecule: dict) -> dict:
    """Map one Work Molecule dict to a PROV-JSON-shaped export. Pure function of
    the molecule: no I/O, no timestamps, deterministic."""
    task_id = molecule["task_spec"]["task_id"]
    exec_id = f"metacoin:activity-execution-{task_id}"
    result_id = f"metacoin:entity-result-{task_id}"
    spec_id = f"metacoin:entity-spec-{task_id}"
    anchor = molecule["ledger_anchor"]
    anchor_id = f"metacoin:entity-ledger-anchor-{anchor['entry_index']}"

    entity, activity, agent = {}, {}, {}
    used, assoc, generated, derived = {}, {}, {}, {}

    # Event timestamps by cited ledger source, for verbatim carry-over onto the
    # verification/challenge activities (metacoin:eventTimestamp, never
    # prov:startTime/endTime — see _TIME_CAVEAT).
    exec_records = molecule.get("execution_records") or []
    ts_by_source = {r["source"]: r["timestamp"] for r in exec_records
                    if isinstance(r, dict) and "source" in r and "timestamp" in r}

    # --- primary activity: the task execution --------------------------------------
    activity[exec_id] = {
        "prov:type": "metacoin:taskExecution",
        "metacoin:assertedEmpty": _asserted_empty_paths(molecule),
        "metacoin:executionRecords": exec_records,
        "metacoin:energyAndComputeEvidence":
            molecule.get("energy_and_compute_evidence"),
        "metacoin:hardwareEvidence": molecule.get("hardware_evidence"),
        "metacoin:manifests": molecule.get("manifests"),
        "metacoin:scopeAndLimitations": molecule.get("scope_and_limitations"),
    }

    # --- entities: result (generated), spec + sources (used), ledger anchor --------
    entity[result_id] = {
        "prov:type": "metacoin:resultArtifact",
        "metacoin:sha256": molecule["result_artifact_hash"],
        "metacoin:ledgerAnchor": anchor_id,
    }
    generated[f"_:gen-{task_id}"] = {"prov:entity": result_id,
                                     "prov:activity": exec_id}
    entity[spec_id] = {
        "prov:type": "metacoin:taskSpec",
        "metacoin:specRecord": molecule["task_spec"],
    }
    used[f"_:use-spec-{task_id}"] = {"prov:activity": exec_id,
                                     "prov:entity": spec_id}
    for src_hash in molecule.get("source_artifact_hashes") or []:
        src_id = f"metacoin:entity-source-{src_hash[:12]}"
        entity[src_id] = {"prov:type": "metacoin:sourceArtifact",
                          "metacoin:sha256": src_hash}
        used[f"_:use-source-{src_hash[:12]}"] = {"prov:activity": exec_id,
                                                 "prov:entity": src_id}
    entity[anchor_id] = {
        "prov:type": "metacoin:ledgerAnchor",
        "metacoin:entryIndex": anchor["entry_index"],
        "metacoin:entryHash": anchor["entry_hash"],
    }

    # --- derivation: parent work -> this result ------------------------------------
    for parent_wmid in molecule.get("parent_work_ids") or []:
        parent_id = f"metacoin:entity-work-molecule-{parent_wmid[:12]}"
        entity[parent_id] = {"prov:type": "metacoin:workMolecule",
                             "metacoin:workMoleculeId": parent_wmid}
        derived[f"_:derive-{parent_wmid[:12]}"] = {
            "prov:generatedEntity": result_id,
            "prov:usedEntity": parent_id,
        }

    # --- agents ---------------------------------------------------------------------
    role_by_actor = {}
    for actor in molecule.get("actors") or []:
        actor_id = actor["actor_id"]
        record = {"metacoin:roles": actor["roles"]}
        if "kind" in actor:
            record["metacoin:kind"] = actor["kind"]
        if "operator_relationship" in actor:
            record["metacoin:operatorRelationship"] = \
                actor["operator_relationship"]
        agent[f"metacoin:agent-{actor_id}"] = record
        role_by_actor[actor_id] = actor["roles"]

    # --- verification + challenge events: separate activities that used the result --
    def _event_activity(event, prov_type):
        idx = event["ledger_index"]
        act_id = f"metacoin:activity-{'challenge' if prov_type.endswith('challengeEvent') else 'verification'}-ledger-{idx}"
        record = {
            "prov:type": prov_type,
            "metacoin:eventName": event["event"],
            "metacoin:ledgerIndex": idx,
            "metacoin:entryHash": event["entry_hash"],
            "metacoin:eventRecord": event,
        }
        ts = ts_by_source.get(f"ledger:{idx}")
        if ts is not None:
            record["metacoin:eventTimestamp"] = ts
        activity[act_id] = record
        used[f"_:use-result-ledger-{idx}"] = {"prov:activity": act_id,
                                              "prov:entity": result_id}
        verifier = event.get("verifier_id")
        if verifier and verifier in role_by_actor:
            roles = role_by_actor[verifier]
            assoc[f"_:assoc-ledger-{idx}"] = {
                "prov:activity": act_id,
                "prov:agent": f"metacoin:agent-{verifier}",
                "prov:role": "verifier" if "verifier" in roles else roles[0],
            }

    for event in molecule.get("verification_events") or []:
        _event_activity(event, "metacoin:verificationEvent")
    for event in molecule.get("challenge_events") or []:
        _event_activity(event, "metacoin:challengeEvent")

    # --- provenance debt: entities, never dropped -----------------------------------
    for i, debt in enumerate(molecule.get("provenance_debt") or []):
        entity[f"metacoin:entity-provenance-debt-{i}"] = {
            "prov:type": "metacoin:provenanceDebt",
            "metacoin:debtState": "reduced" if "reduced_by" in debt else "open",
            "metacoin:debtRecord": debt,
        }

    doc = {
        "schema": EXPORT_SCHEMA,
        "source_work_id": molecule["work_id"],
        "source_molecule_schema": molecule["schema"],
        "prefix": {"prov": "http://www.w3.org/ns/prov#",
                   "metacoin": "urn:metacoin:prov-extension:"},
        "mapping_caveats": {
            "open_world_asserted_empty": _OPEN_WORLD_CAVEAT,
            "verification_modeling": _VERIFICATION_CAVEAT,
            "timestamps": _TIME_CAVEAT,
            "agents": _AGENT_CAVEAT,
        },
        "entity": entity,
        "activity": activity,
        "agent": agent,
        "wasGeneratedBy": generated,
        "used": used,
    }
    if assoc:
        doc["wasAssociatedWith"] = assoc
    if derived:
        doc["wasDerivedFrom"] = derived

    # Extensions manifest: EXACTLY the metacoin:* names used in this document.
    used_ext = _collect_doc_extensions(doc)
    unknown = used_ext - set(_EXTENSION_REGISTRY)
    if unknown:  # exporter bug guard — every emitted extension must be registered
        raise ValueError(f"unregistered metacoin extensions emitted: {unknown}")
    doc["extensions"] = {name: _EXTENSION_REGISTRY[name]
                         for name in sorted(used_ext)}
    return doc


def _collect_extensions(obj, into=None):
    """Every metacoin:* name used as an attribute key or a prov:type value
    within one record (recursive over its nested values)."""
    into = set() if into is None else into
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key.startswith("metacoin:"):
                into.add(key)
            if key == "prov:type" and isinstance(value, str) \
                    and value.startswith("metacoin:"):
                into.add(value)
            _collect_extensions(value, into)
    elif isinstance(obj, list):
        for item in obj:
            _collect_extensions(item, into)
    return into


def _collect_doc_extensions(doc):
    """Extensions used across a whole export document. STRUCTURE-AWARE: the KEYS
    of the entity/activity/agent containers are identifiers (which also live in
    the metacoin namespace) — identifiers are names of things, not extension
    vocabulary, so only the records themselves are scanned."""
    into = set()
    for container in ("entity", "activity", "agent"):
        for record in (doc.get(container) or {}).values():
            _collect_extensions(record, into)
    for relation in _RELATION_ALLOWLIST:
        records = doc.get(relation)
        if isinstance(records, dict):
            for record in records.values():
                _collect_extensions(record, into)
    return into


def _all_keys(obj, into=None):
    """Every dict key anywhere in the document (for the forbidden-relation scan)."""
    into = set() if into is None else into
    if isinstance(obj, dict):
        for key, value in obj.items():
            into.add(key)
            _all_keys(value, into)
    elif isinstance(obj, list):
        for item in obj:
            _all_keys(item, into)
    return into


# ----------------------------------------------------------------------------
# structural validation
# ----------------------------------------------------------------------------
def validate(doc) -> list:
    """Structural validation of one export document (no molecule/ledger access).
    Returns a list of problem strings; [] means valid. Checks: schema, top-level
    key allowlist, no forbidden PROV relation anywhere, every relation reference
    resolves, metacoin:ledgerAnchor references resolve, extensions manifest is
    exactly the set of extensions used, and the WMID is cited."""
    problems = []
    if not isinstance(doc, dict):
        return ["export is not a JSON object"]
    if doc.get("schema") != EXPORT_SCHEMA:
        problems.append(f"schema is {doc.get('schema')!r}, expected {EXPORT_SCHEMA!r}")
    if not doc.get("source_work_id"):
        problems.append("source_work_id (WMID citation) missing")

    allowed_top = set(_STRUCTURAL_KEYS) | set(_RELATION_ALLOWLIST)
    for key in doc:
        if key not in allowed_top:
            problems.append(f"top-level key {key!r} outside the structural + "
                            f"relation allowlist")
    forbidden_used = _all_keys(doc) & set(_FORBIDDEN_PROV_RELATIONS)
    if forbidden_used:
        problems.append(f"forbidden PROV relation name(s) present: "
                        f"{sorted(forbidden_used)} — the molecule makes no such "
                        "claim")

    ids = set()
    for container in ("entity", "activity", "agent"):
        records = doc.get(container)
        if not isinstance(records, dict):
            problems.append(f"missing/invalid container {container!r}")
            continue
        ids.update(records)

    for relation in _RELATION_ALLOWLIST:
        for rel_id, record in (doc.get(relation) or {}).items():
            if not isinstance(record, dict):
                problems.append(f"{relation}[{rel_id}] is not an object")
                continue
            for ref_key, ref in record.items():
                if ref_key == "prov:role":
                    continue
                if ref not in ids:
                    problems.append(f"{relation}[{rel_id}].{ref_key} references "
                                    f"{ref!r}, which is not a declared "
                                    "entity/activity/agent")

    for ent_id, record in (doc.get("entity") or {}).items():
        ref = record.get("metacoin:ledgerAnchor") if isinstance(record, dict) \
            else None
        if isinstance(ref, str) and ref not in ids:
            problems.append(f"entity[{ent_id}].metacoin:ledgerAnchor references "
                            f"{ref!r}, which is not declared")

    manifest = doc.get("extensions")
    if not isinstance(manifest, dict):
        problems.append("extensions manifest missing")
    else:
        used_ext = _collect_doc_extensions(doc)
        unlisted = used_ext - set(manifest)
        unused = set(manifest) - used_ext
        if unlisted:
            problems.append(f"metacoin extensions used but not in the manifest: "
                            f"{sorted(unlisted)}")
        if unused:
            problems.append(f"manifest lists unused extensions: {sorted(unused)}")
    return problems


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
def _canonical_bytes(doc) -> bytes:
    return (json.dumps(doc, sort_keys=True, indent=1) + "\n").encode()


def _export_one(task_id, schema_version, as_of_index, out_path=None):
    short = normalize_task_id(task_id)
    molecule = build_molecule(short, ledger_path=resolve_ledger_path(),
                              schema_version=schema_version,
                              as_of_index=as_of_index)
    doc = build_prov_export(molecule)
    problems = validate(doc)
    if problems:  # exporter bug guard: never write an invalid export
        raise ValueError(f"export failed self-validation: {problems}")
    path = out_path or os.path.join(_REPO_ROOT, f"prov_{short}.json")
    with open(path, "wb") as f:
        f.write(_canonical_bytes(doc))
    return short, doc, path


def _resolve_generation(generation):
    if generation in (None, "", DEFAULT_SCHEMA_VERSION, "0.3"):
        return SCHEMA_VERSION_03
    if generation in (SCHEMA_VERSION_02, "0.2"):
        return SCHEMA_VERSION_02
    raise ValueError(f"unknown generation {generation!r}; supported: "
                     f"{SUPPORTED_SCHEMAS} (short forms 0.2 / 0.3)")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="PROV-compatible Work Molecule export (three-state semantics "
                    "preserved via namespaced metacoin:* extensions)")
    parser.add_argument("--export", metavar="TASK_ID",
                        help="export one task's molecule as prov_<id>.json")
    parser.add_argument("--export-catalog", action="store_true",
                        help="export every catalog task")
    parser.add_argument("--generation", default=None,
                        help="molecule generation (0.2 or 0.3; default 0.3)")
    parser.add_argument("--as-of", type=int, default=None, metavar="N",
                        help="generation-lock: only ledger entries <= N")
    parser.add_argument("--out", help="output path (single --export only)")
    parser.add_argument("--validate", metavar="FILE",
                        help="structurally validate an existing export")
    parser.add_argument("--selftest", action="store_true",
                        help="run the fixture self-test (writes nothing)")
    args = parser.parse_args(argv)

    if args.selftest:
        return _selftest()

    if args.validate:
        with open(args.validate) as f:
            doc = json.load(f)
        problems = validate(doc)
        if problems:
            print(f"INVALID — {len(problems)} problem(s):")
            for p in problems:
                print(f"  - {p}")
            return 1
        print(f"VALID: {args.validate} (source_work_id "
              f"{doc['source_work_id'][:16]}..., "
              f"{len(doc.get('entity', {}))} entities, "
              f"{len(doc.get('activity', {}))} activities, "
              f"{len(doc.get('agent', {}))} agents)")
        return 0

    schema_version = _resolve_generation(args.generation)

    if args.export:
        short, doc, path = _export_one(args.export, schema_version, args.as_of,
                                       args.out)
        print(f"{short}: {len(doc['entity'])} entities, "
              f"{len(doc['activity'])} activities, {len(doc['agent'])} agents "
              f"-> {path}")
        return 0

    if args.export_catalog:
        for task_id in sorted(TASK_MODULES):
            try:
                short, doc, path = _export_one(task_id, schema_version,
                                               args.as_of)
            except (ValueError, KeyError) as exc:
                print(f"{task_id}: SKIPPED (named): {exc}")
                continue
            print(f"{short}: {len(doc['entity'])} entities, "
                  f"{len(doc['activity'])} activities, "
                  f"{len(doc['agent'])} agents -> {path}")
        return 0

    parser.print_help()
    return 2


# ----------------------------------------------------------------------------
# self-test (real task-0002 export via the resolved ledger source + synthetic
# fixtures; temp-only, repo gains no files)
# ----------------------------------------------------------------------------
def _selftest() -> int:
    import copy
    import hashlib
    import tempfile

    print("=== protocol/prov_export.py self-test (fixtures + task-0002; "
          "read-only) ===")
    print("Rich-case task-0002 export structure, three-state survival, "
          "determinism,")
    print("the relation allowlist, and a synthetic parented fixture. The ledger "
          "is read")
    print("at most (never written); the repo gains no files.\n")

    root_before = set(os.listdir(_REPO_ROOT))
    proto_before = set(os.listdir(_PROTO_DIR))
    ledger_sha_before = None
    if os.path.exists(DEFAULT_LIVE_LEDGER):
        with open(DEFAULT_LIVE_LEDGER, "rb") as f:
            ledger_sha_before = hashlib.sha256(f.read()).hexdigest()

    checks = []

    # [1] task-0002 rich case: multi-actor, multi-event, challenge, debt history.
    molecule = build_molecule("task-0002", ledger_path=resolve_ledger_path())
    doc = build_prov_export(molecule)
    checks.append(("task-0002 export is structurally valid", validate(doc) == []))
    checks.append(("task-0002 WMID is cited inside the export",
                   doc["source_work_id"] == molecule["work_id"]))

    exec_id = "metacoin:activity-execution-task-0002"
    result_id = "metacoin:entity-result-task-0002"
    n_ver = len(molecule["verification_events"])
    n_chal = len(molecule["challenge_events"])
    checks.append(("one agent per molecule actor "
                   f"({len(molecule['actors'])} actors)",
                   len(doc["agent"]) == len(molecule["actors"])))
    checks.append(("one execution + one activity per verification/challenge event "
                   f"(1+{n_ver}+{n_chal})",
                   len(doc["activity"]) == 1 + n_ver + n_chal))
    checks.append(("result entity wasGeneratedBy the execution activity",
                   any(r == {"prov:entity": result_id, "prov:activity": exec_id}
                       for r in doc["wasGeneratedBy"].values())))
    ver_used = [r for r in doc["used"].values()
                if r["prov:entity"] == result_id]
    checks.append(("every verification/challenge activity prov:used the result "
                   "entity (deliberate modeling choice)",
                   len(ver_used) == n_ver + n_chal))
    checks.append(("verifier agents are wasAssociatedWith their verification "
                   "activities with prov:role",
                   len(doc.get("wasAssociatedWith", {})) > 0
                   and all(r.get("prov:role") == "verifier"
                           for r in doc["wasAssociatedWith"].values())))

    # [2] three-state survival: asserted-empty AND debt both present in output.
    empties = doc["activity"][exec_id]["metacoin:assertedEmpty"]
    checks.append(("asserted-empty fields survive as metacoin:assertedEmpty "
                   "(parent_work_ids + source_artifact_hashes present)",
                   "parent_work_ids" in empties
                   and "source_artifact_hashes" in empties))
    debt_entities = [e for e in doc["entity"].values()
                     if e.get("prov:type") == "metacoin:provenanceDebt"]
    checks.append(("every provenance_debt entry survives as a debt entity "
                   f"({len(molecule['provenance_debt'])} entries)",
                   len(debt_entities) == len(molecule["provenance_debt"])))
    reduced = [e for e in debt_entities
               if e["metacoin:debtState"] == "reduced"]
    checks.append(("the energy debt_reduction keeps its history (state 'reduced', "
                   "original reason + remaining present)",
                   len(reduced) == 1
                   and "original_reason" in reduced[0]["metacoin:debtRecord"]
                   and "remaining" in reduced[0]["metacoin:debtRecord"]))
    checks.append(("energy labels are carried verbatim, never upgraded",
                   doc["activity"][exec_id]["metacoin:energyAndComputeEvidence"]
                   ["labels"]["energy"] == "estimated"))
    checks.append(("the open-world caveat is the headline mapping caveat",
                   "open-world" in doc["mapping_caveats"]
                   ["open_world_asserted_empty"].lower()
                   or "OPEN-WORLD" in doc["mapping_caveats"]
                   ["open_world_asserted_empty"]))

    # [3] determinism: same molecule -> byte-identical export, twice.
    doc2 = build_prov_export(copy.deepcopy(molecule))
    checks.append(("export is deterministic (byte-identical on a second build)",
                   _canonical_bytes(doc) == _canonical_bytes(doc2)))

    # [4] relation allowlist: no key anywhere in the export is a PROV relation
    # outside the allowlist (the 'grep' — a full nested key scan).
    keys_used = _all_keys(doc)
    checks.append(("no forbidden PROV relation name appears anywhere in the "
                   "export", not keys_used & set(_FORBIDDEN_PROV_RELATIONS)))
    bad = copy.deepcopy(doc)
    bad["wasInformedBy"] = {"_:x": {"prov:informed": exec_id,
                                    "prov:informant": exec_id}}
    problems = validate(bad)
    checks.append(("validator rejects an injected forbidden relation "
                   "(wasInformedBy)",
                   any("wasInformedBy" in p for p in problems)))

    # [5] validator catches a dangling reference and a manifest gap.
    broken = copy.deepcopy(doc)
    broken["used"]["_:use-spec-task-0002"]["prov:entity"] = "metacoin:entity-ghost"
    checks.append(("validator rejects a dangling reference",
                   any("ghost" in p for p in validate(broken))))
    gap = copy.deepcopy(doc)
    del gap["extensions"]["metacoin:assertedEmpty"]
    checks.append(("validator rejects an incomplete extensions manifest",
                   any("not in the manifest" in p for p in validate(gap))))

    # [6] synthetic parented fixture: a hand-built molecule with parent_work_ids
    # and a source artifact exercises wasDerivedFrom + used-source mapping.
    parent_wmid = "p" * 64
    fixture = copy.deepcopy(molecule)
    fixture["parent_work_ids"] = [parent_wmid]
    fixture["source_artifact_hashes"] = ["s" * 64]
    fdoc = build_prov_export(fixture)
    parent_id = f"metacoin:entity-work-molecule-{parent_wmid[:12]}"
    checks.append(("parented fixture emits wasDerivedFrom (result derived from "
                   "the parent work-molecule entity)",
                   any(r == {"prov:generatedEntity": result_id,
                             "prov:usedEntity": parent_id}
                       for r in fdoc.get("wasDerivedFrom", {}).values())))
    checks.append(("parented fixture stays structurally valid and drops the "
                   "fields from assertedEmpty",
                   validate(fdoc) == []
                   and "parent_work_ids" not in
                   fdoc["activity"][exec_id]["metacoin:assertedEmpty"]))

    # [7] CLI round-trip in a temp dir: export writes the file, --validate reads
    # it back VALID.
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "prov_task-0002.json")
        code_export = main(["--export", "task-0002", "--out", out])
        code_validate = main(["--validate", out])
        checks.append(("CLI export + validate round-trip (exit 0, file written)",
                       code_export == 0 and code_validate == 0
                       and os.path.exists(out)))

    # Zero-write guarantees.
    ledger_sha_after = None
    if os.path.exists(DEFAULT_LIVE_LEDGER):
        with open(DEFAULT_LIVE_LEDGER, "rb") as f:
            ledger_sha_after = hashlib.sha256(f.read()).hexdigest()
    checks.append(("ledger untouched (sha256 identical before/after, or absent "
                   "both times)", ledger_sha_before == ledger_sha_after))
    stray_root = set(os.listdir(_REPO_ROOT)) - root_before
    stray_proto = set(os.listdir(_PROTO_DIR)) - proto_before
    checks.append(("repo gained no files (existence delta empty)",
                   not stray_root and not stray_proto))

    failures = 0
    for name, passed in checks:
        print(f"{name:70s}: {'PASS' if passed else 'FAIL'}")
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
