"""work_molecule.py — MetaCoin WORK MOLECULE v0 (schema "work-molecule/0.1").

================== HONESTY / SCOPE (READ ME) ==================
Research-stage, ZERO-VALUE, no token, no money, no mainnet, no networking, no payments.

A Work Molecule is the smallest complete verifiable unit of machine work: a single
versioned JSON document that assembles EVERYTHING already recorded about one task run —
spec identity, actors, manifests, execution timestamps, hardware evidence, verification
events, challenges, the result hash, and the ledger anchor — into one content-addressed
artifact (its WMID = SHA-256 of its canonical JSON).

WHAT THIS MODULE NEVER DOES:
  * It NEVER writes to the real ledger. Molecules are DERIVED, READ-ONLY artifacts
    assembled FROM existing records; the ledger remains the canonical record.
  * It NEVER fabricates evidence. Construction makes NO new claims: every populated
    field is a verbatim copy of an existing record or a DECLARED derivation (with its
    method string); every verification event carries its (ledger_index, entry_hash)
    citation so the copy is mechanically checkable against the chain.
  * It NEVER hides a gap. Three-state rule, enforced by validation:
      - populated      : a value plus its source citation;
      - asserted-empty : [] — we affirmatively claim none exist (e.g. no parent work,
                         no challenges filed, no external source artifacts);
      - not-captured   : null PLUS a matching machine-readable provenance_debt entry.
    provenance_debt is bidirectionally consistent: every null (or partial) evidence
    field has a debt entry, and every debt entry names a real field. Missing evidence
    (e.g. energy/compute cost, TEE attestation) stays EXPLICIT — never filled in.

DETERMINISM: a molecule contains NO construction-time timestamp — every timestamp is
copied from source records. Building the same molecule twice from the same inputs
produces byte-identical canonical JSON and therefore the same WMID.

WMID anti-circularity (same pattern as the ledger's entry hash): the work_id is the
SHA-256 hex digest of the canonical JSON of the molecule WITH THE work_id FIELD
EXCLUDED, so the identifier commits to all content without referencing itself.

HONEST LIMITATION on task_spec.spec_hash: it is computed from the task source file
bytes in the CURRENT checkout, while task_spec.repo_commit is copied from the cited
ledger record. Equality of the file across those two states is checkable with git
(content-addressed), but is NOT proven here; the fixed spec_hash_note says exactly this.

Standard library only (json, hashlib, argparse, os, sys; plus tempfile/shutil/copy in
the self-test). Task-id normalization is REUSED from protocol/verifier_cli.py and the
fixture ledger in the self-test is written via protocol/ledger.py — nothing is
reimplemented. The canonical-JSON helper is deliberately per-module (house style: each
file stands alone for external verifiers). Not legal, financial, investment, or
security-certification advice. No NASA affiliation or endorsement.

Usage:
    python3 protocol/work_molecule.py --task task-0001
    python3 protocol/work_molecule.py --task task-0002 --submission jiyu_submission.json --out wm_task-0002.json
    python3 protocol/work_molecule.py --validate wm_task-0002.json --ledger protocol/ledger_data.jsonl
    python3 protocol/work_molecule.py --selftest      # temp-only; writes nothing into the repo
"""

# Suppress __pycache__/*.pyc so importing protocol modules below leaves no stray files.
# Set immediately after the docstring, BEFORE any protocol imports, so it takes effect.
import sys
sys.dont_write_bytecode = True

import argparse
import hashlib
import json
import os

# Make `from protocol...` resolve when run directly (repo root on path).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# REUSE the existing task-id registry — do NOT reimplement it.
from protocol.verifier_cli import TASK_MODULES, normalize_task_id

_PROTO_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_LEDGER_PATH = os.path.join(_PROTO_DIR, "ledger_data.jsonl")

SCHEMA_VERSION = "work-molecule/0.1"

# Declared derivation method for task_spec.spec_hash, plus the honest caveat that the
# hash is taken from the CURRENT checkout while repo_commit is copied from the record.
SPEC_HASH_METHOD = "sha256-of-task-source-file@repo_commit"
SPEC_HASH_NOTE = (
    "spec_hash is the SHA-256 of the task source file bytes in the CURRENT checkout; "
    "repo_commit is copied verbatim from the cited ledger record. That the file is "
    "unchanged between the two is checkable via git (content-addressed) but is NOT "
    "proven here."
)

# The exact set of top-level keys every v0 molecule must have — no more, no less.
# (task_spec_hash lives inside task_spec; actor_ids inside actors; model_and_tool
#  manifests inside manifests — the 14 target-field groups plus schema + debt.)
_TOP_KEYS = (
    "schema",
    "work_id",
    "task_spec",
    "parent_work_ids",
    "actors",
    "source_artifact_hashes",
    "manifests",
    "execution_records",
    "hardware_evidence",
    "energy_and_compute_evidence",
    "verification_events",
    "challenge_events",
    "result_artifact_hash",
    "ledger_anchor",
    "scope_and_limitations",
    "provenance_debt",
)

# Recorded-output-hash fields a task-verification entry may carry, in priority order
# (same convention as agent_verifier.py).
_RECORDED_HASH_FIELDS = ("output_hash", "local_output_hash", "submitted_output_hash")

# Payload keys copied verbatim (when present) into a verification_events entry.
_TASK_EVENT_COPY_KEYS = (
    "status", "match", "local_output_hash", "submitted_output_hash",
    "verifier_repo_commit", "operator_relationship",
)
_ATTESTATION_COPY_KEYS = (
    "status", "verifier_attestation_method", "verifier_kind", "operator_relationship",
)

# Honest, canned reasons for the debt entries this v0 knows it cannot fill. Any other
# null gets the generic fallback — still explicit, never silent.
_DEBT_REASONS = {
    "hardware_evidence.tee_attestation":
        "no TEE/hardware attestation intake exists yet (Phase 2 roadmap); machine "
        "fingerprints are coarse platform hashes, not execution proof",
    "energy_and_compute_evidence":
        "wall-clock time, CPU time, and energy were never measured for any existing run",
    "manifests.environment_summary":
        "no submission file with an environment summary is available for this task run",
    "task_spec.task_class":
        "no cited ledger record carries a task_class label",
    "task_spec.repo_commit":
        "no cited record carries a repo commit",
}
_DEBT_FALLBACK_REASON = "not captured in any existing record"
_EXECUTION_PARTIAL_DEBT = {
    "field": "execution_records",
    "severity": "partial",
    "reason": "only coarse event timestamps were captured; no durations, exit codes, "
              "or step traces exist in any record",
}


# ----------------------------------------------------------------------------
# Canonical JSON + WMID (per-module helper, same discipline as ledger.py/attest.py)
# ----------------------------------------------------------------------------
def canonical_json(obj) -> str:
    """Canonical JSON: sorted keys, compact separators, ASCII — byte-stable for hashing."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def compute_work_id(molecule: dict) -> str:
    """WMID: SHA-256 hex of the canonical JSON of the molecule WITHOUT its work_id field.

    Excluding work_id from its own preimage is the same anti-circularity pattern the
    ledger uses (an entry's hash covers everything except the hash field itself).
    """
    content = {k: v for k, v in molecule.items() if k != "work_id"}
    return hashlib.sha256(canonical_json(content).encode("utf-8")).hexdigest()


def _ledger_entry_hash(entry: dict) -> str:
    """Recompute a ledger entry's hash from its content (everything except 'hash').

    Same documented algorithm as protocol/ledger.py: SHA-256 over the canonical JSON of
    {index, timestamp, payload, prev_hash}. Recomputed here (3 lines) so validation
    RE-DERIVES the citation instead of trusting the stored hash field.
    """
    content = {
        "index": entry["index"],
        "timestamp": entry["timestamp"],
        "payload": entry["payload"],
        "prev_hash": entry["prev_hash"],
    }
    return hashlib.sha256(canonical_json(content).encode("utf-8")).hexdigest()


# ----------------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------------
def _read_ledger(ledger_path: str) -> list:
    """Read a JSON-Lines ledger file into a list of entry dicts (read-only)."""
    if not os.path.exists(ledger_path):
        raise ValueError(f"ledger file does not exist: {ledger_path}")
    entries = []
    with open(ledger_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    return entries


def _payload_references_task(payload, short_task_id: str) -> bool:
    """True if a ledger payload records something about `short_task_id` (direct task_id
    match, or membership in a batch attestation's task_ids list)."""
    if not isinstance(payload, dict):
        return False
    if payload.get("task_id") == short_task_id:
        return True
    task_ids = payload.get("task_ids")
    return isinstance(task_ids, list) and short_task_id in task_ids


def _copy_present(payload: dict, keys) -> dict:
    """Copy only the keys that are PRESENT in the source record — absent keys are
    omitted (never invented, never null-padded)."""
    return {k: payload[k] for k in keys if k in payload}


def _find_null_paths(obj, prefix="") -> list:
    """Dotted paths of every null value in a molecule (provenance_debt excluded by the
    caller). Nulls inside lists are returned with an [i] path so validation can reject
    them — the three-state rule places not-captured markers only in dict slots."""
    paths = []
    if isinstance(obj, dict):
        for k in sorted(obj):
            p = f"{prefix}.{k}" if prefix else k
            v = obj[k]
            if v is None:
                paths.append(p)
            else:
                paths.extend(_find_null_paths(v, p))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            p = f"{prefix}[{i}]"
            if v is None:
                paths.append(p)
            else:
                paths.extend(_find_null_paths(v, p))
    return paths


def _resolve_path(molecule: dict, dotted: str):
    """Resolve a dotted dict path (no list indices). Returns (found, value)."""
    node = molecule
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return (False, None)
        node = node[part]
    return (True, node)


def _role_for_topology(topology) -> str:
    """Honest actor role implied by a recorded topology label."""
    if topology == "same-machine-self-recompute":
        return "producer+verifier"  # the same host generated AND evaluated the work
    return "verifier"


# ----------------------------------------------------------------------------
# Construction (read-only assembly; NO new claims, NO ledger writes)
# ----------------------------------------------------------------------------
def build_molecule(task_id: str, ledger_path: str = DEFAULT_LEDGER_PATH,
                   submission_path: str = None) -> dict:
    """Assemble the v0 Work Molecule for `task_id` from existing records, read-only.

    Scans the ledger for every entry referencing the task (payload task_id match or
    membership in a batch attestation's task_ids), plus the matching submission file
    (sub_<task-id>.json auto-discovered in the repo root, or `submission_path`).
    Fills all 14 target-field groups per the three-state rule and auto-generates the
    provenance_debt list from the actual nulls. Raises ValueError on missing inputs.
    """
    short = normalize_task_id(task_id)  # KeyError (with known ids) on unknown task

    # --- gather the cited ledger entries (read-only) ----------------------------------
    entries = _read_ledger(ledger_path)
    cited = [e for e in entries
             if isinstance(e, dict) and _payload_references_task(e.get("payload"), short)]
    if not cited:
        raise ValueError(f"no ledger entries reference {short} in {ledger_path}")
    cited.sort(key=lambda e: e["index"])
    ledger_tip = entries[-1]

    # --- optional submission file (auto-discover sub_<task-id>.json) ------------------
    submission = None
    submission_file = None
    if submission_path is None:
        candidate = os.path.join(_REPO_ROOT, f"sub_{short}.json")
        if os.path.exists(candidate):
            submission_path = candidate
    if submission_path is not None:
        with open(submission_path, "r", encoding="utf-8") as f:
            submission = json.load(f)
        submission_file = os.path.basename(submission_path)
        sub_task = submission.get("task_id")
        if sub_task is None or normalize_task_id(sub_task) != short:
            raise ValueError(
                f"submission file {submission_file} is for task "
                f"{sub_task!r}, not {short}"
            )

    # --- classify cited entries -------------------------------------------------------
    task_events = []         # entries recording a verification of THIS task run
    attestation_events = []  # agent_verifier_attestation entries covering this task
    challenge_entries = []   # dispute/challenge entries (none exist yet in v0 data)
    other_events = []
    for e in cited:
        p = e["payload"]
        event = p.get("event", "")
        if "challenge" in event or "dispute" in event:
            challenge_entries.append(e)
        elif event == "agent_verifier_attestation":
            attestation_events.append(e)
        elif any(isinstance(p.get(f), str) and p.get(f) for f in _RECORDED_HASH_FIELDS):
            task_events.append(e)
        else:
            other_events.append(e)
    if not task_events:
        raise ValueError(f"no ledger entry records an output hash for {short}")

    # Primary entry: the lowest-index task-verification entry. Its recorded hash is the
    # molecule's result_artifact_hash and its limitation_note is copied verbatim.
    primary = task_events[0]
    primary_payload = primary["payload"]
    for field in _RECORDED_HASH_FIELDS:
        recorded = primary_payload.get(field)
        if isinstance(recorded, str) and recorded:
            result_artifact_hash = recorded
            break

    # --- task_spec (spec_hash is a DECLARED derivation; everything else copied) -------
    module_name = TASK_MODULES[short]
    spec_source_file = module_name.replace(".", "/") + ".py"
    spec_abs = os.path.join(_REPO_ROOT, *module_name.split(".")) + ".py"
    if not os.path.exists(spec_abs):
        raise ValueError(f"task source file not found: {spec_abs}")
    with open(spec_abs, "rb") as f:
        spec_hash = hashlib.sha256(f.read()).hexdigest()

    # repo_commit: copied from the highest-index cited record that carries one, else
    # from the submission file, else honestly null (-> debt).
    repo_commit = None
    for e in cited:
        rc = e["payload"].get("verifier_repo_commit") or e["payload"].get("repo_commit")
        if isinstance(rc, str) and rc:
            repo_commit = rc
    if repo_commit is None and submission is not None:
        rc = submission.get("repo_commit")
        if isinstance(rc, str) and rc:
            repo_commit = rc

    # task_class: copied from the highest-index cited record that carries one, else null.
    task_class = None
    for e in cited:
        tc = e["payload"].get("task_class")
        if isinstance(tc, str) and tc:
            task_class = tc

    task_spec = {
        "task_id": short,
        "task_class": task_class,
        "spec_source_file": spec_source_file,
        "spec_hash": spec_hash,
        "spec_hash_method": SPEC_HASH_METHOD,
        "spec_hash_note": SPEC_HASH_NOTE,
        "repo_commit": repo_commit,
    }

    # --- actors (one entry per unique verifier_id across cited records) ---------------
    actors_by_id = {}
    def _note_actor(actor_id, topology, payload):
        if not actor_id:
            return
        a = actors_by_id.setdefault(actor_id, {"actor_id": actor_id, "roles": set()})
        a["roles"].add(_role_for_topology(topology))
        for src_key, dst_key in (("verifier_kind", "kind"),
                                 ("operator_relationship", "operator_relationship")):
            if src_key in payload and dst_key not in a:
                a[dst_key] = payload[src_key]
    for e in cited:
        p = e["payload"]
        _note_actor(p.get("verifier_id"), p.get("topology"), p)
    if submission is not None:
        _note_actor(submission.get("verifier_id"), submission.get("topology"), submission)
    actors = []
    for actor_id in sorted(actors_by_id):
        a = actors_by_id[actor_id]
        a["roles"] = sorted(a["roles"])
        actors.append(a)

    # --- manifests (environment summary copied from the submission; models is
    #     asserted-empty, backed by the recorded mechanical-no-LLM attestation method) --
    env_summary = None
    if submission is not None and isinstance(submission.get("environment_summary"), dict):
        env_summary = dict(submission["environment_summary"])
    attestation_methods = sorted({
        e["payload"]["verifier_attestation_method"]
        for e in attestation_events
        if isinstance(e["payload"].get("verifier_attestation_method"), str)
    })
    manifests = {
        "environment_summary": env_summary,
        "models": [],  # asserted-empty: no LLM/model involved (see attestation_methods)
        "attestation_methods": attestation_methods,
    }

    # --- execution_records (coarse timestamps copied from records; partial by debt) ---
    execution_records = []
    if submission is not None and isinstance(submission.get("timestamp"), (int, float)):
        execution_records.append({
            "source": f"submission:{submission_file}",
            "event": submission.get("event", "external_verifier_submission"),
            "timestamp": submission["timestamp"],
            "actor_id": submission.get("verifier_id"),
        })
    for e in cited:
        p = e["payload"]
        ts = p.get("evaluated_at", p.get("anchored_at"))
        if isinstance(ts, (int, float)):
            execution_records.append({
                "source": f"ledger:{e['index']}",
                "event": p.get("event"),
                "timestamp": ts,
                "actor_id": p.get("verifier_id"),
            })
    execution_records.sort(key=lambda r: (r["timestamp"], r["source"]))

    # --- hardware_evidence (coarse fingerprints copied; TEE honestly not-captured) ----
    fingerprints = {}
    for e in cited:
        fp = e["payload"].get("verifier_machine_fingerprint")
        if isinstance(fp, str) and fp:
            key = (e["payload"].get("verifier_id"), fp)
            fingerprints.setdefault(key, set()).add(f"ledger:{e['index']}")
    if submission is not None:
        fp = submission.get("machine_fingerprint")
        if isinstance(fp, str) and fp:
            key = (submission.get("verifier_id"), fp)
            fingerprints.setdefault(key, set()).add(f"submission:{submission_file}")
    machine_fingerprints = [
        {"actor_id": actor_id, "fingerprint": fp, "cited_from": sorted(sources)}
        for (actor_id, fp), sources in sorted(fingerprints.items(),
                                              key=lambda kv: (str(kv[0][0]), kv[0][1]))
    ]
    hardware_evidence = {
        "machine_fingerprints": machine_fingerprints,
        "tee_attestation": None,  # not-captured -> provenance_debt
    }

    # --- verification_events (every cited entry, with its (index, hash) citation) -----
    verification_events = []
    for e in task_events + other_events:
        p = e["payload"]
        ve = {
            "ledger_index": e["index"],
            "entry_hash": e["hash"],
            "event": p.get("event"),
            "topology": p.get("topology"),
            "verifier_id": p.get("verifier_id"),
        }
        ve.update(_copy_present(p, _TASK_EVENT_COPY_KEYS))
        verification_events.append(ve)
    for e in attestation_events:
        p = e["payload"]
        ve = {
            "ledger_index": e["index"],
            "entry_hash": e["hash"],
            "event": p.get("event"),
            "topology": p.get("topology"),
            "verifier_id": p.get("verifier_id"),
            "verdict": p.get("agent_verdict"),
        }
        ve.update(_copy_present(p, _ATTESTATION_COPY_KEYS))
        # Copy this task's per-task reconfirmation record verbatim, when present.
        reconf = p.get("spark_reconfirmed")
        if isinstance(reconf, dict) and isinstance(reconf.get("per_task"), list):
            for t in reconf["per_task"]:
                if isinstance(t, dict) and t.get("task_id") == short:
                    ve["task_reconfirmation"] = dict(t)
                    break
        verification_events.append(ve)
    verification_events.sort(key=lambda v: v["ledger_index"])

    # --- challenge_events ([] = asserted-empty: no challenge/dispute entries exist) ---
    challenge_events = [
        {"ledger_index": e["index"], "entry_hash": e["hash"],
         "event": e["payload"].get("event")}
        for e in challenge_entries
    ]

    # --- ledger_anchor (primary citation + the chain tip as of this build) ------------
    ledger_anchor = {
        "entry_index": primary["index"],
        "entry_hash": primary["hash"],
        "tip_index": ledger_tip["index"],
        "tip_hash": ledger_tip["hash"],
    }

    # --- scope_and_limitations (verbatim from the primary entry) ----------------------
    scope = {
        "limitation_note": primary_payload.get("limitation_note"),
        "topology": primary_payload.get("topology"),
        "stage": primary_payload.get("stage"),
        "zero_value": primary_payload.get("zero_value"),
        "no_token": primary_payload.get("no_token"),
    }
    scope.update(_copy_present(primary_payload, ("operator_relationship",)))

    molecule = {
        "schema": SCHEMA_VERSION,
        "task_spec": task_spec,
        "parent_work_ids": [],        # asserted-empty: v0 tasks have no work lineage
        "actors": actors,
        "source_artifact_hashes": [], # asserted-empty: inputs are constants embedded
                                      # in the hashed task source file; no external
                                      # input artifacts exist
        "manifests": manifests,
        "execution_records": execution_records,
        "hardware_evidence": hardware_evidence,
        "energy_and_compute_evidence": None,  # not-captured -> provenance_debt
        "verification_events": verification_events,
        "challenge_events": challenge_events,
        "result_artifact_hash": result_artifact_hash,
        "ledger_anchor": ledger_anchor,
        "scope_and_limitations": scope,
    }

    # --- provenance_debt: auto-generated from the ACTUAL nulls (bidirectional) --------
    debt = [{"field": path, "reason": _DEBT_REASONS.get(path, _DEBT_FALLBACK_REASON)}
            for path in sorted(_find_null_paths(molecule))]
    debt.append(dict(_EXECUTION_PARTIAL_DEBT))
    debt.sort(key=lambda d: (d["field"], d.get("severity", "")))
    molecule["provenance_debt"] = debt

    molecule["work_id"] = compute_work_id(molecule)
    return molecule


# ----------------------------------------------------------------------------
# Validation (mechanical, no LLM)
# ----------------------------------------------------------------------------
def validate(molecule, ledger_path: str = None):
    """Mechanically validate a molecule. Returns (ok, reasons).

    Checks: (a) supported schema version; (b) exact top-level key set + types;
    (c) work_id recomputes from content; (d) provenance_debt bidirectional consistency
    (every null has a debt entry, every debt entry names a real field, partial entries
    point at populated fields, no nulls inside lists); (e) with a ledger: every cited
    (ledger_index, entry_hash) exists, its hash RE-DERIVES from entry content, and the
    entry actually references this task.
    """
    reasons = []
    if not isinstance(molecule, dict):
        return (False, ["molecule is not a JSON object"])

    # (a) schema version
    if molecule.get("schema") != SCHEMA_VERSION:
        reasons.append(f"unsupported schema {molecule.get('schema')!r}; "
                       f"expected {SCHEMA_VERSION!r}")

    # (b) exact key set + types
    missing = [k for k in _TOP_KEYS if k not in molecule]
    unknown = [k for k in molecule if k not in _TOP_KEYS]
    if missing:
        reasons.append(f"missing top-level fields: {missing}")
    if unknown:
        reasons.append(f"unknown top-level fields: {unknown}")
    _type_specs = (
        ("schema", str), ("work_id", str), ("task_spec", dict),
        ("parent_work_ids", list), ("actors", list), ("source_artifact_hashes", list),
        ("manifests", dict), ("execution_records", list), ("hardware_evidence", dict),
        ("verification_events", list), ("challenge_events", list),
        ("result_artifact_hash", str), ("ledger_anchor", dict),
        ("scope_and_limitations", dict), ("provenance_debt", list),
    )
    for key, typ in _type_specs:
        if key in molecule and not isinstance(molecule[key], typ):
            reasons.append(f"{key} must be {typ.__name__}, "
                           f"got {type(molecule[key]).__name__}")
    if "energy_and_compute_evidence" in molecule:
        v = molecule["energy_and_compute_evidence"]
        if v is not None and not isinstance(v, dict):
            reasons.append("energy_and_compute_evidence must be an object or null")
    if reasons:
        return (False, reasons)  # shape is broken; deeper checks would be noise

    # required sub-keys
    for sub in ("task_id", "spec_hash", "spec_hash_method"):
        if not isinstance(molecule["task_spec"].get(sub), str):
            reasons.append(f"task_spec.{sub} missing or not a string")
    for sub in ("limitation_note", "topology", "stage"):
        if not isinstance(molecule["scope_and_limitations"].get(sub), str):
            reasons.append(f"scope_and_limitations.{sub} missing or not a string")
    for sub in ("zero_value", "no_token"):
        if molecule["scope_and_limitations"].get(sub) is not True:
            reasons.append(f"scope_and_limitations.{sub} must be true "
                           "(research-stage honest labels are mandatory)")
    for sub in ("entry_index", "entry_hash", "tip_index", "tip_hash"):
        if sub not in molecule["ledger_anchor"]:
            reasons.append(f"ledger_anchor.{sub} missing")
    if not molecule["verification_events"]:
        reasons.append("verification_events is empty — a molecule must cite at least "
                       "one ledger verification entry")
    for i, ve in enumerate(molecule["verification_events"]):
        if not isinstance(ve, dict):
            reasons.append(f"verification_events[{i}] is not an object")
            continue
        if not isinstance(ve.get("ledger_index"), int):
            reasons.append(f"verification_events[{i}].ledger_index missing or not int")
        eh = ve.get("entry_hash")
        if not (isinstance(eh, str) and len(eh) == 64):
            reasons.append(f"verification_events[{i}].entry_hash missing or not a "
                           "64-char hash")

    # (c) work_id recomputes from content
    wid = molecule["work_id"]
    if not (len(wid) == 64 and all(c in "0123456789abcdef" for c in wid)):
        reasons.append("work_id is not a 64-char lowercase hex sha256")
    else:
        recomputed = compute_work_id(molecule)
        if recomputed != wid:
            reasons.append(f"work_id does not recompute from content "
                           f"(stored {wid}, recomputed {recomputed}) — "
                           "molecule content was altered")

    # (d) provenance_debt bidirectional consistency
    body = {k: v for k, v in molecule.items() if k != "provenance_debt"}
    null_paths = set(_find_null_paths(body))
    for p in sorted(null_paths):
        if "[" in p:
            reasons.append(f"null inside a list at {p} — the three-state rule places "
                           "not-captured markers only in object fields")
    null_paths = {p for p in null_paths if "[" not in p}
    full_debt, partial_debt = set(), set()
    for i, d in enumerate(molecule["provenance_debt"]):
        if not (isinstance(d, dict) and isinstance(d.get("field"), str)
                and isinstance(d.get("reason"), str)):
            reasons.append(f"provenance_debt[{i}] must be an object with string "
                           "'field' and 'reason'")
            continue
        (partial_debt if d.get("severity") == "partial" else full_debt).add(d["field"])
    for p in sorted(null_paths - full_debt):
        reasons.append(f"null field {p} has no provenance_debt entry — a missing value "
                       "must be explicit, never silent")
    for p in sorted(full_debt - null_paths):
        reasons.append(f"provenance_debt names {p} as not-captured, but that field is "
                       "not null — debt must match reality")
    for p in sorted(partial_debt):
        found, value = _resolve_path(molecule, p)
        if not found or value is None:
            reasons.append(f"partial provenance_debt entry {p} must point at a "
                           "populated field")

    # (e) ledger recheck (optional): re-derive every citation against the chain
    if ledger_path is not None:
        try:
            entries = _read_ledger(ledger_path)
        except (ValueError, json.JSONDecodeError) as exc:
            entries = None
            reasons.append(f"ledger recheck impossible: {exc}")
        if entries is not None:
            by_index = {e.get("index"): e for e in entries if isinstance(e, dict)}
            short = molecule["task_spec"].get("task_id")
            cited = list(molecule["verification_events"]) + \
                list(molecule.get("challenge_events") or [])
            for ve in cited:
                if not isinstance(ve, dict):
                    continue
                idx = ve.get("ledger_index")
                entry = by_index.get(idx)
                if entry is None:
                    reasons.append(f"cited ledger entry {idx} does not exist")
                    continue
                if entry.get("hash") != ve.get("entry_hash"):
                    reasons.append(f"cited entry_hash for ledger index {idx} does not "
                                   f"match the chain ({ve.get('entry_hash')} != "
                                   f"{entry.get('hash')})")
                    continue
                try:
                    rederived = _ledger_entry_hash(entry)
                except (KeyError, TypeError):
                    rederived = None
                if rederived != entry.get("hash"):
                    reasons.append(f"ledger entry {idx} hash does not re-derive from "
                                   "its content — chain record altered")
                if not _payload_references_task(entry.get("payload"), short):
                    reasons.append(f"cited ledger entry {idx} does not reference "
                                   f"{short}")

    return (not reasons, reasons)


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="work_molecule.py",
        description=(
            "MetaCoin Work Molecule v0 (research-stage, ZERO-VALUE, no token). Assemble "
            "the complete provenance unit for one task run from existing records "
            "(read-only; NEVER writes to the ledger), or mechanically validate one."
        ),
        epilog=(
            "A molecule COPIES existing records and DECLARES its derivations; missing "
            "evidence stays explicit in provenance_debt — never fabricated. Not "
            "consensus, not mainnet, not payment, not a token."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--task", help="task id to build a molecule for, e.g. task-0001")
    parser.add_argument("--submission",
                        help="optional path to the submission JSON for this run "
                             "(default: auto-discover sub_<task-id>.json in repo root)")
    parser.add_argument("--ledger", default=DEFAULT_LEDGER_PATH,
                        help=f"ledger JSONL to read (default: {DEFAULT_LEDGER_PATH})")
    parser.add_argument("--out",
                        help="also write the molecule JSON to this file "
                             "(e.g. wm_task-0001.json; gitignored)")
    parser.add_argument("--validate", metavar="FILE",
                        help="validate a molecule JSON file (add --ledger to recheck "
                             "citations against the chain)")
    parser.add_argument("--selftest", action="store_true",
                        help="run the mechanical self-test (temp files only; writes "
                             "nothing into the repo and never touches the real ledger)")
    args = parser.parse_args(argv)

    if args.selftest:
        return _selftest()

    if args.validate:
        with open(args.validate, "r", encoding="utf-8") as f:
            molecule = json.load(f)
        ok, reasons = validate(molecule, ledger_path=args.ledger)
        print(f"schema        : {molecule.get('schema')}")
        print(f"work_id       : {molecule.get('work_id')}")
        print(f"validation    : {'VALID' if ok else 'INVALID'}")
        for r in reasons:
            print(f"  - {r}")
        return 0 if ok else 1

    if not args.task:
        parser.error("one of --task, --validate, or --selftest is required")

    try:
        molecule = build_molecule(args.task, ledger_path=args.ledger,
                                  submission_path=args.submission)
    except (KeyError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    text = json.dumps(molecule, indent=2, sort_keys=True)
    print(text)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        print(f"wrote work molecule to {args.out}", file=sys.stderr)
    return 0


# ============================== SELF-TEST ====================================
def _selftest() -> int:
    """Mechanical self-test. Temp files only: builds a fixture ledger + submission in a
    temp dir via protocol/ledger.py, never touches the real ledger, and proves the
    repo gains no stray files (existence-delta check on the repo root and protocol/).
    """
    import copy
    import shutil
    import tempfile

    from protocol.ledger import Ledger

    print("=== protocol/work_molecule.py self-test (mechanical; temp files only) ===")
    print("Builds a fixture ledger + submission in a temp dir, then checks construction")
    print("determinism, WMID tamper-detection, and provenance-debt consistency.\n")

    root_before = set(os.listdir(_REPO_ROOT))
    proto_before = set(os.listdir(_PROTO_DIR))

    checks = []  # (name, passed)
    tmp_dir = tempfile.mkdtemp(prefix=f"work_molecule_selftest_{os.getpid()}_")
    try:
        # --- fixture ledger (temp path; the real ledger is NEVER written) -------------
        fixture_ledger = os.path.join(tmp_dir, "ledger_fixture.jsonl")
        led = Ledger(fixture_ledger)
        led.append({"event": "ledger_genesis", "note": "selftest fixture",
                    "stage": "R-selftest", "zero_value": True, "no_token": True})
        led.append({
            "event": "self_recompute_result",
            "stage": "R3",
            "topology": "same-machine-self-recompute",
            "task_id": "task-0001",
            "task_class": "selftest-fixture",
            "verifier_id": "selftest-verifier",
            "verifier_kind": "selftest-script",
            "operator_relationship": "same-operator",
            "verifier_machine_fingerprint": "sha256:" + "a" * 64,
            "verifier_repo_commit": "selftest-commit",
            "evaluated_at": 1234567890.0,
            "status": "locally-verified",
            "match": True,
            "local_output_hash": "f" * 64,
            "submitted_output_hash": "f" * 64,
            "limitation_note": "selftest fixture — synthetic record, zero-value, "
                               "no token; exercises the molecule assembler only",
            "zero_value": True,
            "no_token": True,
        })
        fixture_sub = os.path.join(tmp_dir, "sub_fixture.json")
        with open(fixture_sub, "w", encoding="utf-8") as f:
            json.dump({
                "event": "external_verifier_submission",
                "task_id": "task-0001",
                "verifier_id": "selftest-verifier",
                "topology": "same-machine-self-recompute",
                "timestamp": 1234567880.0,
                "output_hash": "f" * 64,
                "repo_commit": "selftest-commit",
                "machine_fingerprint": "sha256:" + "a" * 64,
                "environment_summary": {"python_version": "0.0.0",
                                        "platform": "selftest", "machine": "selftest"},
                "zero_value": True,
                "no_token": True,
            }, f)

        # [1] build + validate
        m1 = build_molecule("task-0001", ledger_path=fixture_ledger,
                            submission_path=fixture_sub)
        ok, reasons = validate(m1, ledger_path=fixture_ledger)
        checks.append(("fixture molecule validates (with ledger recheck)", ok))
        if not ok:
            for r in reasons:
                print(f"    unexpected: {r}")

        # [2] determinism: second build is byte-identical, same WMID
        m2 = build_molecule("task-0001", ledger_path=fixture_ledger,
                            submission_path=fixture_sub)
        checks.append(("two builds byte-identical (canonical JSON)",
                       canonical_json(m1) == canonical_json(m2)))
        checks.append(("two builds share the same work_id",
                       m1["work_id"] == m2["work_id"]))

        # [3] expected debt: the three v0 not-captured/partial entries, exactly
        debt_fields = sorted((d["field"], d.get("severity", "full"))
                             for d in m1["provenance_debt"])
        checks.append(("debt is exactly {energy, execution-partial, tee}",
                       debt_fields == [
                           ("energy_and_compute_evidence", "full"),
                           ("execution_records", "partial"),
                           ("hardware_evidence.tee_attestation", "full"),
                       ]))

        # [4] tampering a populated field breaks the work_id recompute
        t = copy.deepcopy(m1)
        t["result_artifact_hash"] = "0" * 64
        ok, _ = validate(t, ledger_path=fixture_ledger)
        checks.append(("tampered field is detected (work_id mismatch)", not ok))

        # [5] tamper + recompute work_id: the ledger citation recheck still catches it
        t = copy.deepcopy(m1)
        t["verification_events"][0]["entry_hash"] = "0" * 64
        t["work_id"] = compute_work_id(t)
        ok, _ = validate(t, ledger_path=fixture_ledger)
        checks.append(("forged citation is detected (chain recheck)", not ok))

        # [6] removing a debt entry while its field is null fails validation
        t = copy.deepcopy(m1)
        t["provenance_debt"] = [d for d in t["provenance_debt"]
                                if d["field"] != "energy_and_compute_evidence"]
        t["work_id"] = compute_work_id(t)
        ok, _ = validate(t)
        checks.append(("null without a debt entry fails (bidirectional check)", not ok))

        # [7] a new null without debt fails; the same value asserted-empty passes
        t = copy.deepcopy(m1)
        t["manifests"]["environment_summary"] = None
        t["work_id"] = compute_work_id(t)
        ok, _ = validate(t)
        checks.append(("not-captured (null) requires debt; validation enforces it",
                       not ok))
        t = copy.deepcopy(m1)
        t["challenge_events"] = []  # already asserted-empty; must stay valid
        t["work_id"] = compute_work_id(t)
        ok, _ = validate(t)
        checks.append(("asserted-empty ([]) needs no debt entry", ok))

        # [8] a submission for the wrong task is rejected at build time
        wrong_sub = os.path.join(tmp_dir, "sub_wrong.json")
        with open(wrong_sub, "w", encoding="utf-8") as f:
            json.dump({"task_id": "task-0002", "timestamp": 1.0}, f)
        try:
            build_molecule("task-0001", ledger_path=fixture_ledger,
                           submission_path=wrong_sub)
            checks.append(("mismatched submission file is rejected", False))
        except ValueError:
            checks.append(("mismatched submission file is rejected", True))

        # [9] real-ledger build (READ-ONLY) when the runtime ledger exists locally
        if os.path.exists(DEFAULT_LEDGER_PATH):
            rm = build_molecule("task-0001", ledger_path=DEFAULT_LEDGER_PATH)
            ok, reasons = validate(rm, ledger_path=DEFAULT_LEDGER_PATH)
            checks.append(("real-ledger molecule (task-0001) validates", ok))
            if not ok:
                for r in reasons:
                    print(f"    unexpected: {r}")
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
        print(f"{name:55s}: {'PASS' if passed else 'FAIL'}")
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
