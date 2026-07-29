"""cut_certificate.py — MetaCoin CUT CERTIFICATE v0 (schema "cut-certificate/0.1").

================== CONCEPT (READ ME) ==================
A cut certificate is a DETERMINISTIC SUMMARY of a verified provenance subgraph: it
names a set of Work Molecules (the INTERIOR), the WMIDs of declared parents outside
that set (the BOUNDARY inputs), and a content-address over both. It lets a verifier
ACCEPT the summarized set at BOUNDED cost — one anchored-hash lookup plus one
retrievability probe — instead of replaying all history. That acceptance is valid
ONLY while (a) the certificate's hash is anchored on the verified ledger and (b) the
underlying molecules remain retrievable (reconstructible from the retained evidence).
A cut certificate is COMPRESSION, never erasure: nothing summarized may be deleted,
and the full proof can always be re-run from the retained evidence.

The cost asymmetry is deliberate: the EXPENSIVE full verification (rebuild every
interior molecule, recompute every WMID and the aggregate) happens exactly once, at
anchoring time, by the coordinator; every later acceptance is cheap and explicitly
CONDITIONAL on that anchor plus continued retrievability — never a re-proof.

================== HONESTY / SCOPE (READ ME) ==================
Research-stage, ZERO-VALUE, no token, no money, no mainnet, no networking, no payments.

HONESTY RULE for the current data: the real provenance graph is FLAT — every 0.3
molecule's parent_work_ids is asserted-empty — so the first real certificate is a
DEGENERATE cut (13 roots, 0 boundary) that exercises the mechanism without exercising
traversal. The anchored record says exactly this. Non-trivial traversal (multi-
generation closure, boundary computation, cycle rejection) is proven by SYNTHETIC
fixtures in the self-test, not claimed on real data.

Determinism: a certificate contains NO construction timestamps; two builds over the
same ledger are byte-identical. The aggregate_hash covers exactly
{"molecule_schema", "interior", "boundary_input_ids"}; the certificate_hash is the
SHA-256 of the canonical JSON with the certificate_hash field excluded (the same
anti-circularity pattern as the ledger entry hash and the WMID).

Standard library only (json, hashlib, os, argparse). Molecule construction is REUSED
from protocol/work_molecule.py (schema 0.3, with the same submission auto-discovery
the anchored catalog used — so interior WMIDs match the idx-21 generation); the task
registry is REUSED from protocol/verifier_cli.py. The canonical-JSON helper is
deliberately per-module (house style: each file stands alone for external verifiers).
Not legal, financial, investment, or security-certification advice.

Usage:
    python3 protocol/cut_certificate.py --build --all --out cut_cert.json
    python3 protocol/cut_certificate.py --build --roots task-0001,task-0002
    python3 protocol/cut_certificate.py --verify-full cut_cert.json
    python3 protocol/cut_certificate.py --accept cut_cert.json
    python3 protocol/cut_certificate.py --selftest   # temp-only; writes nothing
"""

# Suppress __pycache__/*.pyc so importing protocol modules below leaves no stray files.
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

# REUSE existing components — do NOT reimplement them.
from protocol.verifier_cli import TASK_MODULES, normalize_task_id
import protocol.work_molecule as work_molecule

_PROTO_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_LEDGER_PATH = os.path.join(_PROTO_DIR, "ledger_data.jsonl")

SCHEMA_VERSION = "cut-certificate/0.1"
VERIFICATION_POLICY_VERSION = "cut-verify/0.1"

# v0 has no challenge intake; the only honest value. A filed challenge would suspend
# cheap acceptance until resolved (future work, behind this same field).
CHALLENGE_STATUS_NONE = "none-filed"

# Where the summarized evidence is retained and how any interior molecule is re-derived
# (compression, never erasure: the full proof stays re-runnable from these).
EVIDENCE_RETENTION = {
    "ledger_snapshot": "protocol/ledger_published.json",
    "rebuild_recipe": "python3 protocol/work_molecule.py --task <task_id>",
}

# The ledger record type that anchors a certificate (written by external_verifier.py's
# --anchor-cut-certificate after ITS OWN full verification). Only a CONFIRMED anchor
# supports cheap acceptance.
CUT_EVENT = "cut_certificate_anchored"
CUT_CONFIRMED_STATUS = "cut-certificate-confirmed"

# The exact top-level key set of a v0 certificate — no more, no less.
_TOP_KEYS = (
    "schema",
    "molecule_schema",
    "root_work_ids",
    "interior",
    "boundary_input_ids",
    "interior_count",
    "aggregate_hash",
    "verification_policy_version",
    "challenge_status",
    "evidence_retention",
    "certificate_hash",
)

_HEX = set("0123456789abcdef")


# ----------------------------------------------------------------------------
# Canonical JSON + hashes (per-module helper, same discipline as ledger.py)
# ----------------------------------------------------------------------------
def canonical_json(obj) -> str:
    """Canonical JSON: sorted keys, compact separators, ASCII — byte-stable for hashing."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def compute_aggregate_hash(molecule_schema: str, interior: list,
                           boundary_input_ids: list) -> str:
    """SHA-256 hex over canonical {"molecule_schema", "interior", "boundary_input_ids"}.

    This is the content-address of the SUMMARIZED SET itself, independent of the
    certificate envelope around it.
    """
    content = {
        "molecule_schema": molecule_schema,
        "interior": interior,
        "boundary_input_ids": boundary_input_ids,
    }
    return hashlib.sha256(canonical_json(content).encode("utf-8")).hexdigest()


def compute_certificate_hash(cert: dict) -> str:
    """SHA-256 hex of the canonical JSON of the certificate WITHOUT its
    certificate_hash field (the same anti-circularity pattern as the WMID)."""
    content = {k: v for k, v in cert.items() if k != "certificate_hash"}
    return hashlib.sha256(canonical_json(content).encode("utf-8")).hexdigest()


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


# ----------------------------------------------------------------------------
# Molecule access (REUSED construction, same discovery as the anchored catalog)
# ----------------------------------------------------------------------------
def _build_molecule_for(task_id: str, ledger_path: str) -> dict:
    """Build the 0.3 molecule for `task_id` with the SAME submission auto-discovery
    build_catalog uses, so WMIDs here match the anchored catalog generation."""
    short = normalize_task_id(task_id)
    submission_path = None
    special = work_molecule._CATALOG_SUBMISSIONS.get(short)
    if special is not None:
        candidate = os.path.join(_REPO_ROOT, special)
        if os.path.exists(candidate):
            submission_path = candidate
    return work_molecule.build_molecule(short, ledger_path=ledger_path,
                                        submission_path=submission_path)


def _molecule_pool(ledger_path: str) -> dict:
    """Every buildable+valid registry molecule, keyed by WMID (the cut's resolver).

    Registry tasks with no ledger records are silently absent (they cannot be part
    of any cut over this ledger); an INVALID buildable molecule raises."""
    pool = {}
    for tid in sorted(TASK_MODULES):
        try:
            m = _build_molecule_for(tid, ledger_path)
        except (KeyError, ValueError):
            continue
        ok, reasons = work_molecule.validate(m, ledger_path=ledger_path)
        if not ok:
            raise ValueError(f"molecule for {tid} does not validate: {reasons}")
        pool[m["work_id"]] = m
    return pool


# ----------------------------------------------------------------------------
# Cut closure (generic over a WMID -> molecule resolver, so synthetic fixtures
# can prove the non-trivial traversal the real flat graph cannot exercise yet)
# ----------------------------------------------------------------------------
def close_cut(root_work_ids, resolver: dict):
    """Close the cut from `root_work_ids` over parent edges. Returns (interior,
    boundary_ids).

    interior: every resolvable molecule reachable from the roots via parent_work_ids
    (roots included), i.e. root + ancestors. boundary_ids: declared parent WMIDs that
    do NOT resolve to a molecule in `resolver` — inputs from outside the summarized
    set, recorded but NOT verified by the cut (that is the bound). Cycles among
    parent edges are REJECTED (provenance must be a DAG): raises ValueError.
    """
    interior = {}   # work_id -> molecule
    boundary = set()
    state = {}      # work_id -> "open" (on stack) | "done"
    for root in root_work_ids:
        if root not in resolver:
            raise ValueError(f"root work_id {root} does not resolve to a molecule")
        stack = [(root, False)]
        while stack:
            wid, children_done = stack.pop()
            if children_done:
                state[wid] = "done"
                continue
            if state.get(wid) == "done":
                continue
            if state.get(wid) == "open":
                raise ValueError(
                    f"cycle detected through work_id {wid} — provenance parent "
                    "edges must form a DAG; a cyclic graph cannot be cut"
                )
            state[wid] = "open"
            stack.append((wid, True))
            molecule = resolver[wid]
            interior[wid] = molecule
            for parent in molecule.get("parent_work_ids", []):
                if parent in resolver:
                    if state.get(parent) == "open":
                        raise ValueError(
                            f"cycle detected through work_id {parent} — provenance "
                            "parent edges must form a DAG; a cyclic graph cannot "
                            "be cut"
                        )
                    if state.get(parent) != "done":
                        stack.append((parent, False))
                else:
                    boundary.add(parent)
    return (list(interior.values()), sorted(boundary))


def assemble_certificate(interior_molecules: list, boundary_input_ids: list,
                         root_work_ids: list) -> dict:
    """Assemble the deterministic v0 certificate from closed-cut components.

    All interior molecules must share one schema (0.3 — the generation this v0 cuts
    over). No timestamps are minted: two assemblies over the same inputs are
    byte-identical.
    """
    schemas = sorted({m.get("schema") for m in interior_molecules})
    if schemas != [work_molecule.SCHEMA_VERSION_03]:
        raise ValueError(f"interior molecules must all be "
                         f"{work_molecule.SCHEMA_VERSION_03!r} (got {schemas})")
    interior = sorted(
        ({"task_id": m["task_spec"]["task_id"], "work_id": m["work_id"]}
         for m in interior_molecules),
        key=lambda e: e["task_id"],
    )
    molecule_schema = work_molecule.SCHEMA_VERSION_03
    cert = {
        "schema": SCHEMA_VERSION,
        "molecule_schema": molecule_schema,
        "root_work_ids": sorted(root_work_ids),
        "interior": interior,
        "boundary_input_ids": sorted(boundary_input_ids),
        "interior_count": len(interior),
        "aggregate_hash": compute_aggregate_hash(
            molecule_schema, interior, sorted(boundary_input_ids)),
        "verification_policy_version": VERIFICATION_POLICY_VERSION,
        "challenge_status": CHALLENGE_STATUS_NONE,
        "evidence_retention": dict(EVIDENCE_RETENTION),
    }
    cert["certificate_hash"] = compute_certificate_hash(cert)
    return cert


def build_cut(root_task_ids, ledger_path: str = DEFAULT_LEDGER_PATH) -> dict:
    """Build the cut certificate rooted at `root_task_ids` over the real ledger.

    Builds the 0.3 molecule pool, closes the interior transitively over
    parent_work_ids (rejecting cycles), computes the boundary (declared parents that
    resolve to no molecule), and assembles the certificate. On the current FLAT graph
    every closure is degenerate (interior == roots, boundary == []).
    """
    roots = sorted({normalize_task_id(t) for t in root_task_ids})
    pool = _molecule_pool(ledger_path)
    by_task = {m["task_spec"]["task_id"]: m for m in pool.values()}
    missing = [t for t in roots if t not in by_task]
    if missing:
        raise ValueError(f"no buildable molecule for root task(s): {missing}")
    root_wids = sorted(by_task[t]["work_id"] for t in roots)
    interior_molecules, boundary = close_cut(root_wids, pool)
    return assemble_certificate(interior_molecules, boundary, root_wids)


# ----------------------------------------------------------------------------
# Structural validation (mechanical, no LLM)
# ----------------------------------------------------------------------------
def validate_certificate(cert):
    """Mechanically validate a certificate's structure. Returns (ok, reasons).

    Structure + internal consistency only (exact key set, sorted/unique lists,
    roots inside the interior, boundary disjoint from the interior, aggregate_hash
    and certificate_hash recompute from the file's own content). Whether the
    summarized WMIDs are actually RIGHT is verify_full's job.
    """
    reasons = []
    if not isinstance(cert, dict):
        return (False, ["certificate is not a JSON object"])
    missing = [k for k in _TOP_KEYS if k not in cert]
    unknown = [k for k in cert if k not in _TOP_KEYS]
    if missing:
        reasons.append(f"missing fields: {missing}")
    if unknown:
        reasons.append(f"unknown fields: {unknown}")
    if reasons:
        return (False, reasons)

    if cert["schema"] != SCHEMA_VERSION:
        reasons.append(f"schema must be {SCHEMA_VERSION!r} (got {cert['schema']!r})")
    if cert["molecule_schema"] != work_molecule.SCHEMA_VERSION_03:
        reasons.append(f"molecule_schema must be "
                       f"{work_molecule.SCHEMA_VERSION_03!r} for v0 certificates "
                       f"(got {cert['molecule_schema']!r})")
    if cert["verification_policy_version"] != VERIFICATION_POLICY_VERSION:
        reasons.append(f"verification_policy_version must be "
                       f"{VERIFICATION_POLICY_VERSION!r}")
    if cert["challenge_status"] != CHALLENGE_STATUS_NONE:
        reasons.append(f"challenge_status must be {CHALLENGE_STATUS_NONE!r} in v0 "
                       "(no challenge intake exists yet)")
    if cert["evidence_retention"] != EVIDENCE_RETENTION:
        reasons.append("evidence_retention must state the retained snapshot and the "
                       "rebuild recipe exactly (compression, never erasure)")

    def _hex64(v):
        return isinstance(v, str) and len(v) == 64 and all(c in _HEX for c in v)

    interior = cert["interior"]
    if not isinstance(interior, list) or not interior:
        reasons.append("interior must be a non-empty array")
        interior = []
    interior_wids = []
    task_ids = []
    for i, e in enumerate(interior):
        if not isinstance(e, dict) or set(e.keys()) != {"task_id", "work_id"}:
            reasons.append(f"interior[{i}] must be exactly {{task_id, work_id}}")
            continue
        if not isinstance(e["task_id"], str):
            reasons.append(f"interior[{i}].task_id must be a string")
        if not _hex64(e["work_id"]):
            reasons.append(f"interior[{i}].work_id must be a 64-char lowercase hex "
                           "sha256")
        task_ids.append(e["task_id"])
        interior_wids.append(e["work_id"])
    if task_ids != sorted(task_ids) or len(task_ids) != len(set(task_ids)):
        reasons.append("interior must be unique and sorted by task_id")
    if len(interior_wids) != len(set(interior_wids)):
        reasons.append("interior work_ids must be unique")
    if cert["interior_count"] != len(interior):
        reasons.append(f"interior_count must equal len(interior) "
                       f"({cert['interior_count']} != {len(interior)})")

    roots = cert["root_work_ids"]
    if (not isinstance(roots, list) or not roots
            or roots != sorted(set(roots)) or not all(_hex64(r) for r in roots)):
        reasons.append("root_work_ids must be a non-empty sorted unique list of "
                       "64-char hashes")
    else:
        outside = [r for r in roots if r not in set(interior_wids)]
        if outside:
            reasons.append(f"root_work_ids not inside the interior: {outside}")

    boundary = cert["boundary_input_ids"]
    if (not isinstance(boundary, list)
            or boundary != sorted(set(boundary))
            or not all(_hex64(b) for b in boundary)):
        reasons.append("boundary_input_ids must be a sorted unique list of 64-char "
                       "hashes ([] asserted-empty for a flat graph)")
    else:
        overlap = sorted(set(boundary) & set(interior_wids))
        if overlap:
            reasons.append(f"boundary_input_ids overlap the interior: {overlap}")

    if not reasons:
        expected_agg = compute_aggregate_hash(cert["molecule_schema"], interior,
                                              boundary)
        if cert["aggregate_hash"] != expected_agg:
            reasons.append("aggregate_hash does not recompute from the "
                           "certificate's own interior/boundary content")
        if compute_certificate_hash(cert) != cert["certificate_hash"]:
            reasons.append("certificate_hash does not recompute from content")

    return (not reasons, reasons)


# ----------------------------------------------------------------------------
# The EXPENSIVE path: full verification (what PROVES a certificate)
# ----------------------------------------------------------------------------
def verify_full(cert, ledger_path: str = DEFAULT_LEDGER_PATH):
    """Fully verify a certificate against the ledger. Returns (ok, reasons).

    Rebuilds EVERY interior molecule from the ledger, recomputes every WMID and the
    aggregate_hash from the rebuilt set, and checks graph closure: every declared
    parent of a rebuilt interior molecule is either interior or a declared boundary
    input, and every boundary input is declared by at least one interior molecule.
    Boundary WMIDs themselves are checked as DECLARED, never rebuilt — that is the
    bound: the cut verifies its interior and only NAMES its inputs. This is the
    expensive path; it runs once at anchoring (see external_verifier.py), which is
    what makes accept_by_anchor's cheapness sound.
    """
    ok, reasons = validate_certificate(cert)
    if not ok:
        return (False, reasons)

    rebuilt = []
    for e in cert["interior"]:
        try:
            m = _build_molecule_for(e["task_id"], ledger_path)
        except (KeyError, ValueError) as exc:
            reasons.append(f"interior molecule {e['task_id']} cannot be rebuilt: "
                           f"{exc}")
            continue
        mok, mreasons = work_molecule.validate(m, ledger_path=ledger_path)
        if not mok:
            reasons.append(f"rebuilt molecule {e['task_id']} does not validate: "
                           f"{mreasons}")
            continue
        if m["work_id"] != e["work_id"]:
            reasons.append(f"WMID mismatch for {e['task_id']}: certificate claims "
                           f"{e['work_id']}, rebuild produced {m['work_id']}")
        rebuilt.append(m)

    if not reasons:
        interior_entries = sorted(
            ({"task_id": m["task_spec"]["task_id"], "work_id": m["work_id"]}
             for m in rebuilt),
            key=lambda e: e["task_id"],
        )
        recomputed_agg = compute_aggregate_hash(
            cert["molecule_schema"], interior_entries, cert["boundary_input_ids"])
        if recomputed_agg != cert["aggregate_hash"]:
            reasons.append("aggregate_hash does not recompute from the REBUILT "
                           "interior set")
        # graph closure: declared parents vs interior/boundary (boundary DECLARED,
        # not rebuilt — the bound)
        interior_wids = {m["work_id"] for m in rebuilt}
        boundary = set(cert["boundary_input_ids"])
        declared_parents = set()
        for m in rebuilt:
            for parent in m.get("parent_work_ids", []):
                declared_parents.add(parent)
                if parent not in interior_wids and parent not in boundary:
                    reasons.append(f"declared parent {parent} of "
                                   f"{m['task_spec']['task_id']} is neither interior "
                                   "nor a declared boundary input — the cut is not "
                                   "closed")
        for b in sorted(boundary - declared_parents):
            reasons.append(f"boundary_input_id {b} is declared by no interior "
                           "molecule")

    return (not reasons, reasons)


# ----------------------------------------------------------------------------
# The CHEAP path: acceptance by anchor (bounded cost, honestly conditional)
# ----------------------------------------------------------------------------
def accept_by_anchor(cert, ledger_path: str = DEFAULT_LEDGER_PATH):
    """Accept a certificate at bounded cost. Returns (accepted, cost_note).

    Checks (1) the certificate is structurally valid, (2) a CONFIRMED cut anchor
    with this certificate_hash exists on the ledger, and (3) a retrievability probe:
    rebuild exactly ONE interior molecule (the first by task_id — a deterministic
    choice, chosen for reproducibility; a production policy would sample) and check
    its WMID. Acceptance is CONDITIONAL on the anchor remaining on the verified
    chain and on continued retrievability of all interior molecules — it is NOT a
    re-proof; the full proof happened once, at anchoring.
    """
    ok, reasons = validate_certificate(cert)
    if not ok:
        return (False, f"not accepted — structurally invalid: {reasons}")

    try:
        entries = _read_ledger(ledger_path)
    except (ValueError, json.JSONDecodeError) as exc:
        return (False, f"not accepted — ledger unreadable: {exc}")
    anchor = None
    for e in entries:
        p = e.get("payload") if isinstance(e, dict) else None
        if (isinstance(p, dict) and p.get("event") == CUT_EVENT
                and p.get("status") == CUT_CONFIRMED_STATUS
                and p.get("certificate_hash") == cert["certificate_hash"]):
            anchor = e
    if anchor is None:
        return (False, "not accepted — no CONFIRMED cut anchor with this "
                       "certificate_hash exists on the ledger (cheap acceptance is "
                       "only sound after the coordinator's anchor-time full proof)")

    probe = cert["interior"][0]
    try:
        m = _build_molecule_for(probe["task_id"], ledger_path)
    except (KeyError, ValueError) as exc:
        return (False, f"not accepted — retrievability probe failed: interior "
                       f"molecule {probe['task_id']} cannot be rebuilt ({exc})")
    if m["work_id"] != probe["work_id"]:
        return (False, f"not accepted — retrievability probe failed: rebuilt WMID "
                       f"for {probe['task_id']} is {m['work_id']}, certificate "
                       f"claims {probe['work_id']}")

    cost_note = (
        f"accepted at bounded cost: rebuilt 1 of {cert['interior_count']} interior "
        f"molecules (retrievability probe: {probe['task_id']}) + 1 anchored-hash "
        f"lookup (ledger index {anchor['index']}). CONDITIONAL acceptance — valid "
        "only while the anchor remains on the verified chain and all interior "
        "molecules remain retrievable; this is NOT a re-proof (the full proof ran "
        "once, at anchoring)."
    )
    return (True, cost_note)


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="cut_certificate.py",
        description=(
            "MetaCoin cut certificate v0 (research-stage, ZERO-VALUE, no token). "
            "Deterministic summary of a verified provenance subgraph: full "
            "verification proves it once; anchored acceptance is cheap and "
            "conditional. Compression, never erasure."
        ),
        epilog=(
            "HONESTY: the current real graph is FLAT (no parent edges), so real "
            "certificates are degenerate cuts exercising the mechanism; non-trivial "
            "traversal is proven by synthetic self-test fixtures. Not consensus, "
            "not payment, not a token."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--build", action="store_true",
                      help="build a certificate (with --all or --roots)")
    mode.add_argument("--verify-full", metavar="CERT_JSON",
                      help="EXPENSIVE full verification: rebuild every interior "
                           "molecule and recompute all hashes")
    mode.add_argument("--accept", metavar="CERT_JSON",
                      help="CHEAP acceptance: anchored-hash lookup + one-molecule "
                           "retrievability probe (conditional, not a re-proof)")
    mode.add_argument("--selftest", action="store_true",
                      help="run the mechanical self-test (temp files only)")
    parser.add_argument("--all", action="store_true",
                        help="with --build: root the cut at every known task")
    parser.add_argument("--roots",
                        help="with --build: comma-separated root task ids "
                             "(e.g. task-0001,task-0002)")
    parser.add_argument("--ledger", default=DEFAULT_LEDGER_PATH,
                        help=f"ledger JSONL to read (default: {DEFAULT_LEDGER_PATH})")
    parser.add_argument("--out",
                        help="with --build: also write the certificate JSON here "
                             "(e.g. cut_cert.json; gitignored)")
    args = parser.parse_args(argv)

    if args.selftest or not (args.build or args.verify_full or args.accept):
        return _selftest()

    if args.build:
        if args.all:
            roots = sorted(TASK_MODULES)
        elif args.roots:
            roots = [t.strip() for t in args.roots.split(",") if t.strip()]
        else:
            parser.error("--build requires --all or --roots")
        try:
            cert = build_cut(roots, ledger_path=args.ledger)
        except (KeyError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        text = json.dumps(cert, indent=2, sort_keys=True)
        print(text)
        if args.out:
            with open(args.out, "w", encoding="utf-8") as f:
                f.write(text + "\n")
            print(f"wrote cut certificate ({cert['interior_count']} interior, "
                  f"{len(cert['boundary_input_ids'])} boundary) to {args.out}",
                  file=sys.stderr)
        return 0

    path = args.verify_full or args.accept
    with open(path, "r", encoding="utf-8") as f:
        cert = json.load(f)

    if args.verify_full:
        ok, reasons = verify_full(cert, ledger_path=args.ledger)
        print(f"certificate_hash : {cert.get('certificate_hash')}")
        print(f"full verification: {'PASS' if ok else 'FAIL'} "
              f"(rebuilt {cert.get('interior_count')} interior molecules)")
        for r in reasons:
            print(f"  - {r}")
        return 0 if ok else 1

    accepted, note = accept_by_anchor(cert, ledger_path=args.ledger)
    print(f"certificate_hash : {cert.get('certificate_hash')}")
    print(f"acceptance       : {'ACCEPTED' if accepted else 'NOT ACCEPTED'}")
    print(f"  {note}")
    return 0 if accepted else 1


# ============================== SELF-TEST ====================================
def _selftest() -> int:
    """Mechanical self-test. Temp files only; writes nothing into the repo.

    The REAL logic proof lives here: the flat-graph path runs against a temp fixture
    ledger, and the non-trivial traversal (multi-generation closure, boundary
    membership, cycle rejection) runs against SYNTHETIC molecules — honestly, since
    the real graph has no parent edges yet.
    """
    import copy
    import shutil
    import tempfile

    from protocol.ledger import Ledger
    import protocol.external_verifier as external_verifier
    import protocol.verifier_cli as verifier_cli

    print("=== protocol/cut_certificate.py self-test (mechanical; temp files only) ===")
    print("Flat-graph certificates exercised on a fixture ledger; non-trivial")
    print("traversal/boundary/cycle behavior proven on SYNTHETIC molecules.\n")

    root_before = set(os.listdir(_REPO_ROOT))
    proto_before = set(os.listdir(_PROTO_DIR))

    checks = []  # (name, passed)
    tmp_dir = tempfile.mkdtemp(prefix=f"cut_certificate_selftest_{os.getpid()}_")
    try:
        # --- fixture ledger: genesis + two same-machine task evaluations ------------
        fixture_ledger = os.path.join(tmp_dir, "ledger_fixture.jsonl")
        led = Ledger(fixture_ledger)
        led.append({"event": "ledger_genesis", "note": "selftest fixture",
                    "stage": "R-selftest", "zero_value": True, "no_token": True})
        for tid in ("task-0001", "task-0002"):
            external_verifier.evaluate_submission(
                verifier_cli.build_submission(
                    tid, "selftest-same-operator (simulated)",
                    topology="same-machine-self-recompute"),
                led,
            )

        # [a] flat-graph certificate: builds, verifies fully, degenerate shape
        c1 = build_cut(["task-0001", "task-0002"], ledger_path=fixture_ledger)
        ok, reasons = verify_full(c1, ledger_path=fixture_ledger)
        checks.append(("flat cut: verify_full passes (interior rebuilt+matched)", ok))
        if not ok:
            for r in reasons:
                print(f"    unexpected: {r}")
        checks.append(("flat cut is honestly degenerate (roots==interior, "
                       "boundary asserted-empty)",
                       c1["interior_count"] == 2
                       and c1["boundary_input_ids"] == []
                       and c1["root_work_ids"] ==
                       sorted(e["work_id"] for e in c1["interior"])))

        # [f] determinism: two builds byte-identical
        c2 = build_cut(["task-0001", "task-0002"], ledger_path=fixture_ledger)
        checks.append(("two builds byte-identical (canonical JSON)",
                       canonical_json(c1) == canonical_json(c2)))

        # --- synthetic 3-generation chain A -> B -> C (parents point backwards) ------
        def _synth(task_id, parents):
            m = {"schema": work_molecule.SCHEMA_VERSION_03,
                 "task_spec": {"task_id": task_id},
                 "parent_work_ids": list(parents)}
            m["work_id"] = hashlib.sha256(
                canonical_json(m).encode("utf-8")).hexdigest()
            return m

        a = _synth("task-synth-a", [])
        b = _synth("task-synth-b", [a["work_id"]])
        c = _synth("task-synth-c", [b["work_id"]])

        # [b1] full-chain closure from C resolves all three generations
        full_pool = {m["work_id"]: m for m in (a, b, c)}
        interior, boundary = close_cut([c["work_id"]], full_pool)
        checks.append(("synthetic closure C->B->A: interior {A,B,C}, no boundary",
                       sorted(m["work_id"] for m in interior) ==
                       sorted(full_pool) and boundary == []))

        # [b2] sub-range cut {B,C} with A outside the pool: A's WMID is BOUNDARY —
        # declared, not verified (the bound)
        sub_pool = {m["work_id"]: m for m in (b, c)}
        interior, boundary = close_cut([c["work_id"]], sub_pool)
        cert_bc = assemble_certificate(interior, boundary,
                                       [c["work_id"]])
        checks.append(("synthetic sub-cut: interior exactly {B,C}, boundary "
                       "exactly {A}",
                       sorted(m["work_id"] for m in interior) ==
                       sorted([b["work_id"], c["work_id"]])
                       and boundary == [a["work_id"]]
                       and cert_bc["boundary_input_ids"] == [a["work_id"]]
                       and [e["task_id"] for e in cert_bc["interior"]] ==
                       ["task-synth-b", "task-synth-c"]))
        ok, reasons = validate_certificate(cert_bc)
        checks.append(("synthetic sub-cut certificate validates structurally", ok))
        if not ok:
            for r in reasons:
                print(f"    unexpected: {r}")

        # [c] cycle injection is rejected (provenance must be a DAG)
        a_cyc = dict(a)
        a_cyc["parent_work_ids"] = [c["work_id"]]  # A -> C closes the loop
        cyc_pool = {a_cyc["work_id"]: a_cyc, b["work_id"]: b, c["work_id"]: c}
        try:
            close_cut([c["work_id"]], cyc_pool)
            checks.append(("cycle among parent edges is rejected", False))
        except ValueError:
            checks.append(("cycle among parent edges is rejected", True))

        # [d] tamper an interior WMID (hashes recomputed, so internally consistent):
        # verify_full must fail on the WMID mismatch against the rebuild
        t = copy.deepcopy(c1)
        old_wid = t["interior"][0]["work_id"]
        t["interior"][0]["work_id"] = "1" * 64
        t["root_work_ids"] = sorted(
            ("1" * 64 if r == old_wid else r) for r in t["root_work_ids"])
        t["aggregate_hash"] = compute_aggregate_hash(
            t["molecule_schema"], t["interior"], t["boundary_input_ids"])
        t["certificate_hash"] = compute_certificate_hash(t)
        ok, _ = verify_full(t, ledger_path=fixture_ledger)
        checks.append(("tampered interior WMID fails verify_full (rebuild "
                       "disagrees)", not ok))

        # [e] cheap acceptance: fails without an anchor; passes with one; fails
        # when the probed molecule cannot be reproduced as claimed
        accepted, note = accept_by_anchor(c1, ledger_path=fixture_ledger)
        checks.append(("accept_by_anchor FAILS without a confirmed anchor",
                       not accepted and "no CONFIRMED cut anchor" in note))
        led.append({"event": CUT_EVENT, "status": CUT_CONFIRMED_STATUS,
                    "certificate_hash": c1["certificate_hash"],
                    "note": "selftest fixture anchor", "zero_value": True,
                    "no_token": True})
        accepted, note = accept_by_anchor(c1, ledger_path=fixture_ledger)
        checks.append(("accept_by_anchor passes with an anchor (1-molecule probe)",
                       accepted and "rebuilt 1 of 2" in note
                       and "NOT a re-proof" in note))
        # anchor a certificate whose probed WMID is wrong: the anchor matches but
        # the retrievability probe must catch the irreproducible claim
        bad = copy.deepcopy(c1)
        old_wid = bad["interior"][0]["work_id"]
        bad["interior"][0]["work_id"] = "2" * 64
        bad["root_work_ids"] = sorted(
            ("2" * 64 if r == old_wid else r) for r in bad["root_work_ids"])
        bad["aggregate_hash"] = compute_aggregate_hash(
            bad["molecule_schema"], bad["interior"], bad["boundary_input_ids"])
        bad["certificate_hash"] = compute_certificate_hash(bad)
        led.append({"event": CUT_EVENT, "status": CUT_CONFIRMED_STATUS,
                    "certificate_hash": bad["certificate_hash"],
                    "note": "selftest fixture anchor (bad claim)",
                    "zero_value": True, "no_token": True})
        accepted, note = accept_by_anchor(bad, ledger_path=fixture_ledger)
        checks.append(("accept_by_anchor FAILS when the probed molecule does not "
                       "reproduce", not accepted and "probe failed" in note))

        # the honest flat certificate still accepts after the extra anchors
        accepted, _ = accept_by_anchor(c1, ledger_path=fixture_ledger)
        checks.append(("honest certificate still accepts (append-only growth)",
                       accepted))
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
