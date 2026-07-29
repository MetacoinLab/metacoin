"""work_molecule.py — MetaCoin WORK MOLECULE v0 (schemas "work-molecule/0.2" and "/0.3").

================== CONSTITUTIONAL RULE (READ ME) ==================
Provenance debt is reduced ONLY by appending new evidence objects. No existing ledger
record, submission file, or anchored artifact is modified. The idx-17 catalog's
work-molecule/0.2 WMIDs must remain valid forever: a 0.2-mode rebuild must still match
them after any later evidence exists. New measurements produce NEW records; molecules
built at schema 0.3 absorb them and get NEW WMIDs — both generations verifiable,
neither replacing the other.

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

SCHEMA HISTORY:
  work-molecule/0.1 — initial schema; ledger_anchor carried the build-time chain tip.
  work-molecule/0.2 — ledger_anchor is ENTRY-LEVEL ONLY (entry_index/entry_hash of the
    primary event). Entry-level citations are stable under append-only growth; a
    build-time tip made WMIDs change whenever the ledger grew, breaking the promise
    that the same records always reconstruct to the same WMID.
  work-molecule/0.3 — absorbs APPEND-ONLY supplementary evidence (the current default).
    If a confirmed metering-evidence anchor exists on the ledger for this catalog,
    energy_and_compute_evidence is POPULATED from it (per-task wall/CPU/energy figures
    copied from the local metering report file when present and hash-matching the
    anchor, else an aggregate-only citation with a note), and the energy provenance_debt
    entry is REPLACED by a debt_reduction entry that preserves the debt HISTORY (the
    original reason, the reducing ledger citation, and what still remains open). Wall
    and CPU times are MEASURED; energy is an ESTIMATE from an assumed constant power
    figure — the labels are copied verbatim and never upgraded. 0.3 molecules are
    deterministic GIVEN the ledger + the metering report file (every timestamp/figure
    is copied, none created). tee_attestation debt and the execution_records partial
    debt remain open. 0.2-mode builds remain byte-for-byte identical to the idx-17
    generation — the two generations coexist; neither replaces the other.

Usage:
    python3 protocol/work_molecule.py --task task-0001
    python3 protocol/work_molecule.py --task task-0002 --submission jiyu_submission.json --out wm_task-0002.json
    python3 protocol/work_molecule.py --catalog --out wm_catalog_v03.json
    python3 protocol/work_molecule.py --catalog --molecule-schema work-molecule/0.2 --out wm_catalog.json
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

# Both molecule generations are first-class citizens: 0.2 is the regression-locked
# idx-17 generation (must rebuild byte-identical forever), 0.3 is the default going
# forward (absorbs append-only supplementary evidence). Each maps to its own catalog
# schema so the two catalog generations are distinguishable at a glance.
SCHEMA_VERSION_02 = "work-molecule/0.2"
SCHEMA_VERSION_03 = "work-molecule/0.3"
DEFAULT_SCHEMA_VERSION = SCHEMA_VERSION_03
SUPPORTED_SCHEMAS = (SCHEMA_VERSION_02, SCHEMA_VERSION_03)
CATALOG_SCHEMA_VERSION_01 = "work-molecule-catalog/0.1"
CATALOG_SCHEMA_VERSION_02 = "work-molecule-catalog/0.2"
_CATALOG_SCHEMA_FOR_MOLECULE = {
    SCHEMA_VERSION_02: CATALOG_SCHEMA_VERSION_01,
    SCHEMA_VERSION_03: CATALOG_SCHEMA_VERSION_02,
}

# The metering-evidence ledger record 0.3 molecules absorb (see external_verifier.py's
# --anchor-metering-report). Only a CONFIRMED anchor reduces debt; the record itself
# carries no task_id/task_ids keys, so it is invisible to the citation scan below and
# can never change a 0.2 WMID.
_METERING_EVENT = "metering_evidence_anchored"
_METERING_CONFIRMED_STATUS = "metering-evidence-confirmed"

# Default local metering report artifact (gitignored; per-task figures are copied from
# it only when its recomputed report_hash matches the anchored one).
DEFAULT_METERING_REPORT_PATH = os.path.join(_REPO_ROOT, "metering_report.json")

# What the metering evidence does NOT cover — carried in every debt_reduction entry so
# reduced debt is never mistaken for eliminated debt.
ENERGY_DEBT_REMAINING = "hardware power telemetry; per-step traces"

# Fixed honesty notes for the absorbed energy evidence. Timing data lives ONLY in the
# supplementary evidence chain (report + anchor); it is non-reproducible by nature, so
# these notes state the claim-fixing (not recompute-verifiable) integrity model.
_ENERGY_PER_TASK_NOTE = (
    "wall/CPU times MEASURED, energy ESTIMATED (cpu_time x assumed constant power; no "
    "hardware power telemetry on this host). Figures copied verbatim from the anchored "
    "metering report; timing is non-reproducible run to run, so the cited report_hash "
    "fixes the claim made at measurement time, not a recomputable value."
)
_ENERGY_AGGREGATE_NOTE = (
    "per-task figures not available locally (metering report file absent or not "
    "hash-matching the anchored report_hash); citing the anchored aggregate totals "
    "only. wall/CPU times MEASURED, energy ESTIMATED (assumed constant power; no "
    "hardware power telemetry). Timing is non-reproducible run to run, so the cited "
    "report_hash fixes the claim made at measurement time, not a recomputable value."
)

# Submission files with non-standard names (auto-discovery expects sub_<task-id>.json).
# task-0002's external-pilot submission predates that naming convention. Used only when
# the file actually exists (these are gitignored local artifacts, absent in CI/clones —
# without them the molecule honestly records environment_summary as null + debt).
_CATALOG_SUBMISSIONS = {"task-0002": "jiyu_submission.json"}

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


def _find_metering_anchor(entries: list):
    """Return the highest-index CONFIRMED metering-evidence entry, or None.

    Only a confirmed anchor reduces debt; rejected/mismatch outcomes never do.
    """
    anchor = None
    for e in entries:
        if not isinstance(e, dict):
            continue
        p = e.get("payload")
        if (isinstance(p, dict) and p.get("event") == _METERING_EVENT
                and p.get("status") == _METERING_CONFIRMED_STATUS):
            anchor = e
    return anchor


def _energy_evidence_from_anchor(anchor_entry: dict, short_task_id: str,
                                 metering_report_path) -> dict:
    """Assemble the 0.3 energy_and_compute_evidence object from a metering anchor.

    Everything is COPIED, never created: the citation/labels/power figures come from
    the anchored ledger record; the per-task wall/CPU/energy figures come from the
    local metering report file ONLY when its recomputed report_hash matches the
    anchored one (else the anchored aggregate totals are cited with an honest note).
    Deterministic given the ledger + the report file — no timestamps are minted here.
    """
    payload = anchor_entry["payload"]
    evidence = {"source": f"ledger:{anchor_entry['index']}"}
    evidence.update(_copy_present(payload, (
        "report_schema", "report_hash", "assumed_cpu_power_w", "power_method",
    )))
    if isinstance(payload.get("labels"), dict):
        evidence["labels"] = dict(payload["labels"])

    # Per-task figures: only from a local report file that hash-matches the anchor.
    row = None
    if metering_report_path and os.path.exists(metering_report_path):
        try:
            with open(metering_report_path, "r", encoding="utf-8") as f:
                report = json.load(f)
        except (json.JSONDecodeError, OSError):
            report = None
        if isinstance(report, dict):
            content = {k: v for k, v in report.items() if k != "report_hash"}
            recomputed = hashlib.sha256(
                canonical_json(content).encode("utf-8")).hexdigest()
            if recomputed == report.get("report_hash") == payload.get("report_hash"):
                for r in report.get("per_task", []):
                    if isinstance(r, dict) and r.get("task_id") == short_task_id:
                        row = r
                        break

    if row is not None:
        evidence.update(_copy_present(row, (
            "wall_time_s", "cpu_time_s", "energy_j_estimate",
        )))
        evidence["note"] = _ENERGY_PER_TASK_NOTE
    else:
        evidence["aggregate_totals"] = _copy_present(payload, (
            "total_wall_time_s", "total_cpu_time_s", "total_energy_j_estimate",
        ))
        evidence["note"] = _ENERGY_AGGREGATE_NOTE
    return evidence


# ----------------------------------------------------------------------------
# Construction (read-only assembly; NO new claims, NO ledger writes)
# ----------------------------------------------------------------------------
def build_molecule(task_id: str, ledger_path: str = DEFAULT_LEDGER_PATH,
                   submission_path: str = None,
                   schema_version: str = DEFAULT_SCHEMA_VERSION,
                   metering_report_path: str = DEFAULT_METERING_REPORT_PATH) -> dict:
    """Assemble the Work Molecule for `task_id` from existing records, read-only.

    Scans the ledger for every entry referencing the task (payload task_id match or
    membership in a batch attestation's task_ids), plus the matching submission file
    (sub_<task-id>.json auto-discovered in the repo root, or `submission_path`).
    Fills all 14 target-field groups per the three-state rule and auto-generates the
    provenance_debt list from the actual nulls. Raises ValueError on missing inputs.

    `schema_version` selects the generation (CONSTITUTIONAL RULE above): 0.2 rebuilds
    the idx-17 generation byte-for-byte; 0.3 (default) additionally absorbs a confirmed
    metering-evidence anchor into energy_and_compute_evidence and converts that debt
    entry into a debt_reduction entry — appending evidence, never altering records.
    """
    if schema_version not in SUPPORTED_SCHEMAS:
        raise ValueError(f"unsupported molecule schema {schema_version!r}; "
                         f"supported: {SUPPORTED_SCHEMAS}")
    short = normalize_task_id(task_id)  # KeyError (with known ids) on unknown task

    # --- gather the cited ledger entries (read-only) ----------------------------------
    entries = _read_ledger(ledger_path)
    cited = [e for e in entries
             if isinstance(e, dict) and _payload_references_task(e.get("payload"), short)]
    if not cited:
        raise ValueError(f"no ledger entries reference {short} in {ledger_path}")
    cited.sort(key=lambda e: e["index"])

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

    # --- ledger_anchor: ENTRY-LEVEL ONLY (schema 0.2 stability fix) -------------------
    # Entry-level citations are stable under append-only growth. 0.1 also recorded the
    # build-time chain tip here, which made WMIDs change whenever the ledger grew —
    # so the same records no longer reconstructed to the same WMID. The per-entry
    # citations in verification_events plus this primary citation are sufficient;
    # tip freshness is the committed anchor file's job, not the molecule's.
    ledger_anchor = {
        "entry_index": primary["index"],
        "entry_hash": primary["hash"],
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

    # --- energy_and_compute_evidence: 0.2 is always not-captured; 0.3 absorbs a
    # confirmed metering anchor when one exists (APPEND-ONLY debt reduction) ----------
    metering_anchor = None
    energy_evidence = None
    if schema_version == SCHEMA_VERSION_03:
        metering_anchor = _find_metering_anchor(entries)
        if metering_anchor is not None:
            energy_evidence = _energy_evidence_from_anchor(
                metering_anchor, short, metering_report_path)

    molecule = {
        "schema": schema_version,
        "task_spec": task_spec,
        "parent_work_ids": [],        # asserted-empty: v0 tasks have no work lineage
        "actors": actors,
        "source_artifact_hashes": [], # asserted-empty: inputs are constants embedded
                                      # in the hashed task source file; no external
                                      # input artifacts exist
        "manifests": manifests,
        "execution_records": execution_records,
        "hardware_evidence": hardware_evidence,
        "energy_and_compute_evidence": energy_evidence,  # null = not-captured -> debt
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
    # 0.3 debt reduction: the energy field is populated by ledger evidence, so its debt
    # entry becomes a debt_reduction entry — history preserved (original_reason), the
    # reducing citation named (reduced_by), and what stays open stated (remaining).
    if energy_evidence is not None:
        debt.append({
            "field": "energy_and_compute_evidence",
            "reduced_by": f"ledger:{metering_anchor['index']}",
            "remaining": ENERGY_DEBT_REMAINING,
            "original_reason": _DEBT_REASONS["energy_and_compute_evidence"],
        })
    debt.sort(key=lambda d: (d["field"], d.get("severity", "")))
    molecule["provenance_debt"] = debt

    molecule["work_id"] = compute_work_id(molecule)
    return molecule


# ----------------------------------------------------------------------------
# Catalog: the WMIDs of every task's molecule, content-addressed the same way
# ----------------------------------------------------------------------------
def compute_catalog_hash(catalog: dict) -> str:
    """SHA-256 hex of the canonical JSON of the catalog WITHOUT its catalog_hash field
    (same anti-circularity pattern as the WMID and the ledger entry hash)."""
    content = {k: v for k, v in catalog.items() if k != "catalog_hash"}
    return hashlib.sha256(canonical_json(content).encode("utf-8")).hexdigest()


def build_catalog(ledger_path: str = DEFAULT_LEDGER_PATH, task_ids=None,
                  schema_version: str = DEFAULT_SCHEMA_VERSION,
                  metering_report_path: str = DEFAULT_METERING_REPORT_PATH) -> dict:
    """Build + validate the molecule for every known task and return the catalog dict.

    Deterministic and timestamp-free like the molecules themselves: two runs over the
    same inputs are byte-identical. Each molecule is validated (with ledger recheck)
    before its WMID enters the catalog; any invalid molecule raises ValueError.
    `task_ids` narrows the set (used by self-tests over fixture ledgers); the default
    is every task in the registry. `schema_version` selects the molecule generation;
    the catalog schema follows it (0.2 molecules -> catalog/0.1, the regression-locked
    idx-17 generation; 0.3 molecules -> catalog/0.2).
    """
    if schema_version not in SUPPORTED_SCHEMAS:
        raise ValueError(f"unsupported molecule schema {schema_version!r}; "
                         f"supported: {SUPPORTED_SCHEMAS}")
    if task_ids is None:
        task_ids = sorted(TASK_MODULES)
    entries = []
    for tid in sorted(task_ids):
        short = normalize_task_id(tid)
        submission_path = None
        special = _CATALOG_SUBMISSIONS.get(short)
        if special is not None:
            candidate = os.path.join(_REPO_ROOT, special)
            if os.path.exists(candidate):
                submission_path = candidate
        molecule = build_molecule(short, ledger_path=ledger_path,
                                  submission_path=submission_path,
                                  schema_version=schema_version,
                                  metering_report_path=metering_report_path)
        ok, reasons = validate(molecule, ledger_path=ledger_path)
        if not ok:
            raise ValueError(f"molecule for {short} does not validate: {reasons}")
        entries.append({"task_id": short, "work_id": molecule["work_id"]})
    catalog = {
        "schema": _CATALOG_SCHEMA_FOR_MOLECULE[schema_version],
        "molecule_schema": schema_version,
        "entries": entries,  # already sorted by task_id
    }
    catalog["catalog_hash"] = compute_catalog_hash(catalog)
    return catalog


# ----------------------------------------------------------------------------
# Validation (mechanical, no LLM)
# ----------------------------------------------------------------------------
def validate(molecule, ledger_path: str = None):
    """Mechanically validate a molecule. Returns (ok, reasons).

    Checks: (a) supported schema version (0.2 or 0.3); (b) exact top-level key set +
    types; (c) work_id recomputes from content; (d) provenance_debt bidirectional
    consistency (every null has a debt entry, every debt entry names a real field,
    partial entries point at populated fields, no nulls inside lists; 0.3 only:
    debt_reduction entries point at POPULATED fields, a populated-via-reduction energy
    field has a debt_reduction entry and vice versa, and 0.2 molecules may carry no
    reduction entries at all); (e) with a ledger: every cited (ledger_index,
    entry_hash) — including the ledger_anchor's primary citation — exists, its hash
    RE-DERIVES from entry content, and the entry actually references this task; a
    debt_reduction's reduced_by citation must name a confirmed metering-evidence entry
    whose report_hash matches the absorbed evidence. All checks are entry-level (never
    tip-level), so validation stays true under append-only ledger growth.
    """
    reasons = []
    if not isinstance(molecule, dict):
        return (False, ["molecule is not a JSON object"])

    # (a) schema version — both generations are valid forever (constitutional rule)
    if molecule.get("schema") not in SUPPORTED_SCHEMAS:
        reasons.append(f"unsupported schema {molecule.get('schema')!r}; "
                       f"supported: {SUPPORTED_SCHEMAS}")
    is_03 = molecule.get("schema") == SCHEMA_VERSION_03

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
    for sub in ("entry_index", "entry_hash"):
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

    # (d) provenance_debt bidirectional consistency. Three entry shapes:
    #   plain full     {field, reason}                       <-> a null field
    #   partial        {field, reason, severity: "partial"}  <-> a populated field
    #   debt_reduction {field, reduced_by, remaining, original_reason} (0.3 only)
    #                                                        <-> a populated field
    body = {k: v for k, v in molecule.items() if k != "provenance_debt"}
    null_paths = set(_find_null_paths(body))
    for p in sorted(null_paths):
        if "[" in p:
            reasons.append(f"null inside a list at {p} — the three-state rule places "
                           "not-captured markers only in object fields")
    null_paths = {p for p in null_paths if "[" not in p}
    full_debt, partial_debt, reduced_debt = set(), set(), set()
    for i, d in enumerate(molecule["provenance_debt"]):
        if not (isinstance(d, dict) and isinstance(d.get("field"), str)):
            reasons.append(f"provenance_debt[{i}] must be an object with a string "
                           "'field'")
            continue
        if "reduced_by" in d:
            # debt_reduction entry: history preserved, remaining debt stated
            if not is_03:
                reasons.append(f"provenance_debt[{i}] is a debt_reduction entry, which "
                               f"only exists in {SCHEMA_VERSION_03} molecules")
            for key in ("reduced_by", "remaining", "original_reason"):
                if not isinstance(d.get(key), str) or not d.get(key):
                    reasons.append(f"provenance_debt[{i}].{key} missing or not a "
                                   "non-empty string (debt_reduction entries must "
                                   "preserve the debt history)")
            reduced_debt.add(d["field"])
            continue
        if not isinstance(d.get("reason"), str):
            reasons.append(f"provenance_debt[{i}] must carry a string 'reason'")
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
    for p in sorted(reduced_debt):
        found, value = _resolve_path(molecule, p)
        if not found or value is None:
            reasons.append(f"debt_reduction entry {p} must point at a populated field "
                           "— a reduction claim over a still-missing value is a lie")
    # A populated-via-reduction energy field must carry its debt_reduction entry (and
    # vice versa, covered just above): at this stage ledger evidence is the ONLY way
    # energy_and_compute_evidence gets populated, so the pairing is mandatory.
    if (is_03 and isinstance(molecule.get("energy_and_compute_evidence"), dict)
            and "energy_and_compute_evidence" not in reduced_debt):
        reasons.append("energy_and_compute_evidence is populated but carries no "
                       "debt_reduction entry — reduced debt must stay on the record, "
                       "never silently erased")

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
            anchor_citation = {
                "ledger_index": molecule["ledger_anchor"].get("entry_index"),
                "entry_hash": molecule["ledger_anchor"].get("entry_hash"),
            }
            cited = list(molecule["verification_events"]) + \
                list(molecule.get("challenge_events") or []) + [anchor_citation]
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

            # debt_reduction citations: reduced_by must name a CONFIRMED metering
            # anchor whose report_hash matches the absorbed evidence. (Checked apart
            # from the loop above — a metering record deliberately references no task.)
            energy = molecule.get("energy_and_compute_evidence")
            for d in molecule["provenance_debt"]:
                if not (isinstance(d, dict) and isinstance(d.get("reduced_by"), str)):
                    continue
                ref = d["reduced_by"]
                idx = None
                if ref.startswith("ledger:"):
                    try:
                        idx = int(ref.split(":", 1)[1])
                    except ValueError:
                        idx = None
                entry = by_index.get(idx)
                if entry is None:
                    reasons.append(f"debt_reduction cites {ref!r}, which does not "
                                   "exist in the ledger")
                    continue
                p = entry.get("payload") or {}
                if (p.get("event") != _METERING_EVENT
                        or p.get("status") != _METERING_CONFIRMED_STATUS):
                    reasons.append(f"debt_reduction cites ledger entry {idx}, which is "
                                   "not a confirmed metering-evidence record")
                if (isinstance(energy, dict)
                        and energy.get("report_hash") != p.get("report_hash")):
                    reasons.append(f"absorbed energy evidence report_hash does not "
                                   f"match the cited metering anchor at index {idx}")

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
    parser.add_argument("--catalog", action="store_true",
                        help="build + validate molecules for ALL known tasks and write "
                             "the WMID catalog (default out: wm_catalog_v03.json for "
                             "0.3, wm_catalog.json for 0.2; gitignored)")
    parser.add_argument("--molecule-schema", default=DEFAULT_SCHEMA_VERSION,
                        choices=list(SUPPORTED_SCHEMAS),
                        help="molecule generation to build (default "
                             f"{DEFAULT_SCHEMA_VERSION}; {SCHEMA_VERSION_02} rebuilds "
                             "the regression-locked idx-17 generation byte-for-byte)")
    parser.add_argument("--metering-report", default=DEFAULT_METERING_REPORT_PATH,
                        help="local metering report for 0.3 per-task energy figures "
                             f"(default: {DEFAULT_METERING_REPORT_PATH}; used only "
                             "when it hash-matches the anchored report)")
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

    if args.catalog:
        try:
            catalog = build_catalog(ledger_path=args.ledger,
                                    schema_version=args.molecule_schema,
                                    metering_report_path=args.metering_report)
        except (KeyError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        text = json.dumps(catalog, indent=2, sort_keys=True)
        print(text)
        out = args.out or ("wm_catalog.json"
                           if args.molecule_schema == SCHEMA_VERSION_02
                           else "wm_catalog_v03.json")
        with open(out, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        print(f"wrote work-molecule catalog ({len(catalog['entries'])} entries, "
              f"{catalog['molecule_schema']}) to {out}", file=sys.stderr)
        return 0

    if not args.task:
        parser.error("one of --task, --catalog, --validate, or --selftest is required")

    try:
        molecule = build_molecule(args.task, ledger_path=args.ledger,
                                  submission_path=args.submission,
                                  schema_version=args.molecule_schema,
                                  metering_report_path=args.metering_report)
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

        # [1] build + validate (0.2 mode — the regression-locked idx-17 generation)
        m1 = build_molecule("task-0001", ledger_path=fixture_ledger,
                            submission_path=fixture_sub,
                            schema_version=SCHEMA_VERSION_02)
        ok, reasons = validate(m1, ledger_path=fixture_ledger)
        checks.append(("fixture molecule validates (with ledger recheck)", ok))
        if not ok:
            for r in reasons:
                print(f"    unexpected: {r}")

        # [2] determinism: second build is byte-identical, same WMID
        m2 = build_molecule("task-0001", ledger_path=fixture_ledger,
                            submission_path=fixture_sub,
                            schema_version=SCHEMA_VERSION_02)
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

        # [7b] schema 0.2 stability: appending an UNRELATED entry to the ledger must
        # NOT change the molecule's WMID (entry-level citations only — no build-time
        # tip in the molecule), and the molecule must still validate afterwards.
        led.append({"event": "unrelated_marker", "note": "selftest growth probe — "
                    "must not appear in any molecule", "zero_value": True,
                    "no_token": True})
        m3 = build_molecule("task-0001", ledger_path=fixture_ledger,
                            submission_path=fixture_sub,
                            schema_version=SCHEMA_VERSION_02)
        checks.append(("ledger growth does not change the WMID (0.2)",
                       m3["work_id"] == m1["work_id"]))
        ok, reasons = validate(m3, ledger_path=fixture_ledger)
        checks.append(("molecule still validates after ledger growth", ok))
        if not ok:
            for r in reasons:
                print(f"    unexpected: {r}")

        # [7c] catalog: deterministic, anti-circular hash, entries match the molecules
        c1 = build_catalog(ledger_path=fixture_ledger, task_ids=["task-0001"],
                           schema_version=SCHEMA_VERSION_02)
        c2 = build_catalog(ledger_path=fixture_ledger, task_ids=["task-0001"],
                           schema_version=SCHEMA_VERSION_02)
        checks.append(("catalog builds byte-identical twice",
                       canonical_json(c1) == canonical_json(c2)))
        checks.append(("catalog_hash recomputes (anti-circularity)",
                       compute_catalog_hash(c1) == c1["catalog_hash"]))
        checks.append(("catalog entries are {task_id, work_id} pairs",
                       c1["schema"] == CATALOG_SCHEMA_VERSION_01
                       and c1["molecule_schema"] == SCHEMA_VERSION_02
                       and [sorted(e) for e in c1["entries"]] ==
                       [["task_id", "work_id"]]))

        # ---- schema 0.3: append-only absorption of metering evidence -----------------
        # [7d] a ledger WITHOUT a metering anchor still builds 0.3 gracefully: the
        # energy field stays not-captured (null) with its plain debt entry intact.
        m03_bare = build_molecule("task-0001", ledger_path=fixture_ledger,
                                  submission_path=fixture_sub,
                                  metering_report_path=None)
        ok, reasons = validate(m03_bare, ledger_path=fixture_ledger)
        checks.append(("0.3 without metering anchor: debt intact, validates",
                       ok and m03_bare["schema"] == SCHEMA_VERSION_03
                       and m03_bare["energy_and_compute_evidence"] is None
                       and any(d["field"] == "energy_and_compute_evidence"
                               and "reason" in d
                               for d in m03_bare["provenance_debt"])))
        if not ok:
            for r in reasons:
                print(f"    unexpected: {r}")

        # Fixture metering evidence: a report file + its CONFIRMED anchor, appended to
        # the fixture ledger (append-only — nothing above is modified).
        fixture_labels = {"wall": "measured", "cpu": "measured", "energy": "estimated"}
        fixture_report = {
            "schema": "metering-report/0.1",
            "assumed_cpu_power_w": 15.0,
            "power_method": "assumed-nameplate; no hardware telemetry on this host",
            "per_task": [{
                "task_id": "task-0001", "output_hash": "f" * 64,
                "wall_time_s": 0.001, "cpu_time_s": 0.0008,
                "energy_j_estimate": 0.012, "labels": dict(fixture_labels),
            }],
        }
        fixture_report["report_hash"] = hashlib.sha256(canonical_json(
            {k: v for k, v in fixture_report.items() if k != "report_hash"}
        ).encode("utf-8")).hexdigest()
        fixture_report_path = os.path.join(tmp_dir, "metering_report_fixture.json")
        with open(fixture_report_path, "w", encoding="utf-8") as f:
            json.dump(fixture_report, f)
        metering_entry = led.append({
            "event": _METERING_EVENT, "status": _METERING_CONFIRMED_STATUS,
            "stage": "R-provenance-debt", "topology": "same-machine-metering",
            "report_schema": "metering-report/0.1",
            "report_hash": fixture_report["report_hash"],
            "task_count": 1, "assumed_cpu_power_w": 15.0,
            "power_method": "assumed-nameplate; no hardware telemetry on this host",
            "labels": dict(fixture_labels),
            "total_wall_time_s": 0.001, "total_cpu_time_s": 0.0008,
            "total_energy_j_estimate": 0.012,
            "operator_relationship": "same-operator",
            "limitation_note": "selftest fixture — synthetic metering anchor",
            "zero_value": True, "no_token": True,
        })

        # [7e] 0.3 with the anchor + hash-matching report: energy populated per-task,
        # debt_reduction present (history preserved), validates, deterministic, and the
        # 0.2 build is UNCHANGED (append-only: new evidence never rewrites the past).
        m03a = build_molecule("task-0001", ledger_path=fixture_ledger,
                              submission_path=fixture_sub,
                              metering_report_path=fixture_report_path)
        m03b = build_molecule("task-0001", ledger_path=fixture_ledger,
                              submission_path=fixture_sub,
                              metering_report_path=fixture_report_path)
        ok, reasons = validate(m03a, ledger_path=fixture_ledger)
        energy = m03a["energy_and_compute_evidence"]
        reduction = next((d for d in m03a["provenance_debt"] if "reduced_by" in d), None)
        checks.append(("0.3 absorbs metering evidence (per-task figures + labels)",
                       ok and isinstance(energy, dict)
                       and energy["source"] == f"ledger:{metering_entry['index']}"
                       and energy["wall_time_s"] == 0.001
                       and energy["cpu_time_s"] == 0.0008
                       and energy["energy_j_estimate"] == 0.012
                       and energy["labels"] == fixture_labels))
        if not ok:
            for r in reasons:
                print(f"    unexpected: {r}")
        checks.append(("debt_reduction preserves history (original_reason+remaining)",
                       reduction is not None
                       and reduction["field"] == "energy_and_compute_evidence"
                       and reduction["reduced_by"] == f"ledger:{metering_entry['index']}"
                       and reduction["remaining"] == ENERGY_DEBT_REMAINING
                       and reduction["original_reason"] ==
                       _DEBT_REASONS["energy_and_compute_evidence"]))
        checks.append(("0.3 deterministic given ledger + report (byte-identical)",
                       canonical_json(m03a) == canonical_json(m03b)))
        checks.append(("0.3 WMID is a NEW identity (differs from 0.2)",
                       m03a["work_id"] != m1["work_id"]))
        m1_after = build_molecule("task-0001", ledger_path=fixture_ledger,
                                  submission_path=fixture_sub,
                                  schema_version=SCHEMA_VERSION_02)
        checks.append(("0.2 build UNCHANGED by the metering anchor (append-only)",
                       canonical_json(m1_after) == canonical_json(m1)))

        # [7f] no local report file -> aggregate-only citation, still valid+deterministic
        m03agg = build_molecule("task-0001", ledger_path=fixture_ledger,
                                submission_path=fixture_sub,
                                metering_report_path=None)
        ok, _ = validate(m03agg, ledger_path=fixture_ledger)
        checks.append(("0.3 aggregate-only citation when report file absent",
                       ok and "wall_time_s" not in m03agg["energy_and_compute_evidence"]
                       and m03agg["energy_and_compute_evidence"]["aggregate_totals"] ==
                       {"total_wall_time_s": 0.001, "total_cpu_time_s": 0.0008,
                        "total_energy_j_estimate": 0.012}))

        # [7g] debt_reduction bidirectional consistency is enforced both ways, and a
        # forged reduced_by citation is caught by the ledger recheck.
        t = copy.deepcopy(m03a)
        t["provenance_debt"] = [d for d in t["provenance_debt"]
                                if "reduced_by" not in d]
        t["work_id"] = compute_work_id(t)
        ok, _ = validate(t)
        checks.append(("populated-via-reduction without a reduction entry fails",
                       not ok))
        t = copy.deepcopy(m03a)
        t["energy_and_compute_evidence"] = None
        t["work_id"] = compute_work_id(t)
        ok, _ = validate(t)
        checks.append(("reduction entry over a null field fails (a reduction claim "
                       "over a missing value is a lie)", not ok))
        t = copy.deepcopy(m03a)
        for d in t["provenance_debt"]:
            if "reduced_by" in d:
                d["reduced_by"] = "ledger:0"  # genesis: exists, but not a metering record
        t["work_id"] = compute_work_id(t)
        ok, _ = validate(t, ledger_path=fixture_ledger)
        checks.append(("forged reduced_by citation is detected (chain recheck)",
                       not ok))

        # [7h] the 0.3 catalog is its own generation: catalog/0.2 + molecule/0.3
        c03 = build_catalog(ledger_path=fixture_ledger, task_ids=["task-0001"],
                            metering_report_path=fixture_report_path)
        c03b = build_catalog(ledger_path=fixture_ledger, task_ids=["task-0001"],
                             metering_report_path=fixture_report_path)
        checks.append(("0.3 catalog: schema pair + deterministic + anti-circular hash",
                       c03["schema"] == CATALOG_SCHEMA_VERSION_02
                       and c03["molecule_schema"] == SCHEMA_VERSION_03
                       and canonical_json(c03) == canonical_json(c03b)
                       and compute_catalog_hash(c03) == c03["catalog_hash"]
                       and c03["catalog_hash"] != c1["catalog_hash"]))

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

        # [9] real-ledger build (READ-ONLY) when the runtime ledger exists locally:
        # both generations validate, and the 0.2 catalog is REGRESSION-LOCKED to the
        # anchored idx-17 generation (the constitutional rule, mechanically enforced).
        if os.path.exists(DEFAULT_LEDGER_PATH):
            rm = build_molecule("task-0001", ledger_path=DEFAULT_LEDGER_PATH,
                                schema_version=SCHEMA_VERSION_02)
            ok, reasons = validate(rm, ledger_path=DEFAULT_LEDGER_PATH)
            checks.append(("real-ledger 0.2 molecule (task-0001) validates", ok))
            if not ok:
                for r in reasons:
                    print(f"    unexpected: {r}")
            rm3 = build_molecule("task-0001", ledger_path=DEFAULT_LEDGER_PATH)
            ok, reasons = validate(rm3, ledger_path=DEFAULT_LEDGER_PATH)
            checks.append(("real-ledger 0.3 molecule (task-0001) validates", ok))
            if not ok:
                for r in reasons:
                    print(f"    unexpected: {r}")
            real_entries = _read_ledger(DEFAULT_LEDGER_PATH)
            idx17 = next((e for e in real_entries
                          if e["payload"].get("event") ==
                          "work_molecule_catalog_anchored"
                          and e["payload"].get("molecule_schema") ==
                          SCHEMA_VERSION_02), None)
            if idx17 is not None:
                cat02 = build_catalog(ledger_path=DEFAULT_LEDGER_PATH,
                                      schema_version=SCHEMA_VERSION_02)
                checks.append(("0.2 catalog REGRESSION-LOCK: rebuild matches the "
                               "anchored idx-17 catalog_hash",
                               cat02["catalog_hash"] ==
                               idx17["payload"]["catalog_hash"]))
            else:
                print("    (no anchored 0.2 catalog on the real ledger — regression "
                      "lock SKIPPED)")
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
