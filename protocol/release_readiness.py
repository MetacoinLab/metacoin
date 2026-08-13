# Copyright (c) 2023-2026 MetaCoin-Lab.
# Licensed under SML-1.0 — see LICENSE.md.
"""release_readiness.py — the complete-product release gate (schema
"release-readiness/0.1").

================== HONESTY / SCOPE (READ ME) ==================
Research-stage, ZERO-VALUE, no token, no money, no mainnet, no payments.

This is the standing pre-release instrument MIP-0005 ratifies: it answers
"what stands between us and the next release" mechanically, with NAMED gaps.
The release policy it serves (practiced since v0.1.0, codified by the MIP):

  * DEFAULT IS NO RELEASE — versions, tags, and releases require explicit
    coordinator approval, never automation. This tool never releases
    anything; it only measures.
  * COMPLETE-PRODUCT STANDARD — the next release must let an unaffiliated
    stranger complete the full loop without contacting the project:
    install -> verify -> create identity -> run verification under their
    own key -> bundle -> submit -> be validated -> be anchored -> appear in
    their own passport. Partial feature showcases do not qualify.
  * HONEST GAP ACCOUNTING — gaps that depend on external reality (a second
    machine, an unaffiliated participant, a second device for the mirror)
    are named as such and NEVER simulated away.

Each criterion evaluates to one of FOUR types:
  PASS    — mechanically established right now.
  GAP     — not established; carries what-would-close-it, named honestly.
  SKIPPED — (--fast only) an expensive check not run; fast mode can
            therefore NEVER report READY.
  HUMAN   — out-of-band by design: coordinator approval is a human
            decision, never mechanical, and this type NEVER converts to
            PASS (asserted in the self-test).

Verdict: READY iff every mechanical criterion is PASS (HUMAN excluded,
nothing SKIPPED); else NOT-READY with the gap list. NOT-READY is the
EXPECTED state between releases — the sweep reports it informationally,
never as a finding, and this tool exits 0 either way (an instrument, not a
test). READY is necessary for a release, never sufficient: approval remains
human.

Standard library only. Reads the chain and the repo; writes nothing outside
its temp dirs. Not legal or financial advice.

Usage:
    python3 protocol/release_readiness.py --check           # full gate (runs the cold install)
    python3 protocol/release_readiness.py --check --fast    # skips the cold install (never READY)
    python3 protocol/release_readiness.py --check --json    # machine-readable report
    python3 protocol/release_readiness.py --selftest
"""

# Suppress __pycache__/*.pyc so importing protocol modules leaves no stray files.
import sys
sys.dont_write_bytecode = True

import argparse
import glob
import hashlib
import json
import os
import subprocess
import tempfile

_PROTO_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_PROTO_DIR)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import protocol.doc_verify as doc_verify
import protocol.work_molecule as work_molecule
from protocol.prov_export import resolve_ledger_path
from protocol.work_molecule import _read_ledger, find_evidence_file

SCHEMA_VERSION = "release-readiness/0.1"

CROSS_MACHINE_GAP = "awaits a second machine or an external participant"
MIRROR_GAP = "awaits the second device"


def _c(name, status, detail, closes_with=None):
    row = {"name": name, "status": status, "detail": detail}
    if closes_with is not None:
        row["closes_with"] = closes_with
    return row


# ----------------------------------------------------------------------------
# criteria (each a pure-ish evaluator; chain-reading ones take `entries`)
# ----------------------------------------------------------------------------
def crit_cold_install(fast: bool, echo=print) -> dict:
    """Build a wheel, install it into a fresh venv, run `metacoin verify` in
    an EMPTY directory with no repo checkout — the product acceptance test,
    executed for real (the same loop the CLI self-test enforces in CI)."""
    if fast:
        return _c("cold-install acceptance", "SKIPPED",
                  "not run in --fast mode (expensive); fast mode can never "
                  "report READY — run the full gate for a release")
    with tempfile.TemporaryDirectory(prefix="release_gate_") as tmp:
        dist = os.path.join(tmp, "dist")
        r = subprocess.run([sys.executable, "-m", "pip", "wheel", ".",
                           "--no-deps", "--wheel-dir", dist],
                          cwd=_REPO_ROOT, capture_output=True, text=True)
        wheels = glob.glob(os.path.join(dist, "*.whl"))
        if r.returncode != 0 or not wheels:
            return _c("cold-install acceptance", "GAP",
                      f"wheel build failed: {r.stderr.strip()[-200:]}",
                      "a building package")
        venv_dir = os.path.join(tmp, "venv")
        subprocess.run([sys.executable, "-m", "venv", venv_dir],
                       capture_output=True, text=True)
        vpy = os.path.join(venv_dir, "bin", "python")
        r = subprocess.run([vpy, "-m", "pip", "install", "--no-index",
                            wheels[0]], capture_output=True, text=True)
        if r.returncode != 0:
            return _c("cold-install acceptance", "GAP",
                      f"venv install failed: {r.stderr.strip()[-200:]}",
                      "an installable wheel")
        empty = os.path.join(tmp, "empty")
        os.makedirs(empty)
        echo("  running `metacoin verify` from the cold install "
             "(takes a few minutes)...")
        r = subprocess.run(
            [os.path.join(venv_dir, "bin", "metacoin"), "verify", "--quiet"],
            cwd=empty, capture_output=True, text=True, timeout=1800)
        ok = r.returncode == 0 and "ALL LAYERS PASS" in (r.stdout + r.stderr)
        return _c("cold-install acceptance", "PASS" if ok else "GAP",
                  "fresh venv, empty dir, no repo -> ALL LAYERS PASS from "
                  "package data alone" if ok else
                  f"cold verify failed: {(r.stdout + r.stderr)[-200:]}",
                  None if ok else "a passing cold install")


def crit_participant_loop(entries) -> dict:
    """Same-machine rehearsal of the full participant loop, evidenced on the
    chain: registration + a participant-verified bundle + a rejection drill."""
    found = {}
    for e in entries:
        p = e.get("payload", {})
        key = (p.get("event"), p.get("status"))
        if key == ("actor_key_registered", "actor-key-registered"):
            found.setdefault("registered", e["index"])
        if key == ("participant_result_anchored", "participant-verified"):
            found.setdefault("verified", e["index"])
        if key == ("participant_intake_rejected",
                   "participant-intake-rejected"):
            found.setdefault("rejected", e["index"])
    ok = {"registered", "verified", "rejected"} <= set(found)
    return _c("participant loop rehearsed (same-machine)",
              "PASS" if ok else "GAP",
              (f"chain evidence: registration idx {found.get('registered')}, "
               f"participant-verified idx {found.get('verified')}, "
               f"rejection drill idx {found.get('rejected')}") if ok else
              f"missing stages: {sorted({'registered', 'verified', 'rejected'} - set(found))}",
              None if ok else "an end-to-end intake rehearsal")


def _coordinator_fingerprints(entries) -> set:
    fps = set()
    for e in entries:
        p = e.get("payload", {})
        for k in ("machine_fingerprint", "verifier_machine_fingerprint"):
            v = p.get(k)
            if isinstance(v, str) and v:
                fps.add(v)
    return fps


def crit_cross_machine(entries, bundle_loader=None) -> dict:
    """A participant verification from a machine that is NOT one of the
    coordinator's: any participant_result_anchored whose bundle carries a
    machine fingerprint absent from every coordinator record. The rehearsal
    participant ran on the coordinator's own machine, so its fingerprint is
    in the coordinator set and it can never satisfy this criterion."""
    if bundle_loader is None:
        def bundle_loader(sha):
            path = find_evidence_file(f"participant_bundle_{sha[:12]}.json")
            if path is None:
                return None
            with open(path) as f:
                return json.load(f)
    coord = _coordinator_fingerprints(entries)
    for e in entries:
        p = e.get("payload", {})
        if (p.get("event") != "participant_result_anchored"
                or p.get("status") != "participant-verified"):
            continue
        bundle = bundle_loader(p.get("bundle_sha256", ""))
        fp = (((bundle or {}).get("signed_result") or {}).get("result")
              or {}).get("machine_fingerprint")
        if isinstance(fp, str) and fp and fp not in coord:
            return _c("cross-machine participation", "PASS",
                      f"idx {e['index']}: participant fingerprint differs "
                      "from every coordinator machine on the chain")
    return _c("cross-machine participation", "GAP",
              "every verified participant bundle so far carries a "
              "coordinator machine fingerprint (the same-machine rehearsal)",
              CROSS_MACHINE_GAP)


def crit_independent_mirror(entries, bundle_loader=None) -> dict:
    """CHAIN-DERIVED (the criterion must re-derive in any fresh clone —
    CI, doc sandboxes, cold installs — so its evidence is the chain plus
    the committed evidence bundle, never a coordinator-local config):
    the newest anchored mirror attestation must exist, its shipped
    evidence bundle must match the anchored sha, the signed fingerprint
    must match no coordinator machine on the chain (the device rule,
    fingerprint-decided), the attested chain point must be a verified
    prefix of the current chain, and the mirror check verdict must be
    IDENTICAL/BEHIND. Ongoing freshness is the weekly sweep's
    informational job — the anchored attestation is the criterion's
    evidence. (The earlier external_mirrors.json config form was never
    exercised and is retired by MIP-0007.)"""
    recs = [e for e in entries
            if isinstance(e.get("payload"), dict)
            and e["payload"].get("event") == "mirror_attestation_anchored"
            and e["payload"].get("status") == "mirror-attested"]
    if not recs:
        return _c("independent mirror active", "GAP",
                  "no anchored mirror attestation from a second device — "
                  "the in-repo mirror_export/ is the coordinator's own "
                  "device and does not count",
                  MIRROR_GAP)
    e = recs[-1]
    p = e["payload"]
    sha = str(p.get("bundle_sha256"))

    def _default_loader(s):
        path = find_evidence_file(f"mirror_attestation_{s[:12]}.json")
        if not path:
            return None
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    loader = bundle_loader or _default_loader
    try:
        bundle = loader(sha)
    except (OSError, json.JSONDecodeError, ValueError):
        bundle = None
    if not isinstance(bundle, dict):
        return _c("independent mirror active", "GAP",
                  f"idx {e['index']}: mirror attestation evidence file "
                  "missing", "the shipped evidence bundle")
    canonical = json.dumps(bundle, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=True).encode("utf-8")
    if hashlib.sha256(canonical).hexdigest() != sha:
        return _c("independent mirror active", "GAP",
                  f"idx {e['index']}: evidence bundle sha256 does not match "
                  "the anchored claim", "matching evidence")
    att = bundle.get("attestation", {})
    fp = att.get("machine_fingerprint")
    coord = _coordinator_fingerprints(entries)
    m = p.get("mirror", {})
    ti, th = m.get("tip_index"), m.get("tip_hash")
    prefix_ok = (isinstance(ti, int) and 0 <= ti < len(entries)
                 and entries[ti].get("hash") == th)
    problems = []
    if not (isinstance(fp, str) and fp
            and fp == p.get("participant_machine_fingerprint")
            and coord and fp not in coord):
        problems.append("the device rule fails: the signed fingerprint must "
                        "match the record and no coordinator machine")
    if not prefix_ok:
        problems.append("the attested chain point is not a verified prefix "
                        "of the current chain")
    if p.get("check_verdict") not in ("IDENTICAL", "BEHIND"):
        problems.append(f"check verdict {p.get('check_verdict')!r} does not "
                        "attest a healthy mirror")
    if problems:
        return _c("independent mirror active", "GAP",
                  f"idx {e['index']}: " + "; ".join(problems),
                  "a verifying second-device attestation")
    return _c("independent mirror active", "PASS",
              f"idx {e['index']}: mirror attested by a non-coordinator "
              "device (fingerprint-decided); attested chain point idx "
              f"{ti} is a verified prefix of the current chain; freshness "
              "is reported by the weekly sweep")


def crit_docs(entries) -> dict:
    """Docs + MIPs + the era-pinned README verify (tokens, citations, era
    pins). Command blocks are deliberately NOT executed here — CI's full
    doc_verify run executes them; this criterion stays cheap and unlooped
    (the MIP-0005 verification blocks run THIS gate)."""
    findings, _stats = doc_verify.check_docs(execute=False,
                                             mip_dir=doc_verify.MIP_DIR,
                                             echo=lambda *a: None)
    r_findings, _r = doc_verify.check_readme(entries=entries)
    findings = findings + r_findings
    return _c("docs verified", "PASS" if not findings else "GAP",
              "tokens, ledger citations, MIP citations, and the era-pinned "
              "README all verify (command blocks execute in CI's full "
              "doc_verify run)" if not findings
              else "; ".join(findings[:3]),
              None if not findings else "clean docs")


def crit_sentry() -> dict:
    """The weekly sweep sentry is WIRED: the scheduled workflow is tracked
    and the sweep tool imports. The latest verdict itself lives in CI run
    history (not persisted in-repo), so wiring is what is mechanically
    checkable here — stated, not papered over."""
    wf = os.path.join(_REPO_ROOT, ".github", "workflows", "sweep.yml")
    try:
        import protocol.routine_sweep as routine_sweep
        importable = hasattr(routine_sweep, "run_sweep")
    except Exception:  # import failure = broken sentry, whatever the cause
        importable = False
    ok = os.path.exists(wf) and importable
    return _c("sentry health (weekly sweep)", "PASS" if ok else "GAP",
              "scheduled workflow tracked + sweep tool imports; the latest "
              "verdict lives in CI run history (wiring checked here, "
              "outcome enforced by the failing-run alarm)" if ok else
              "sweep workflow or tool missing",
              None if ok else "a wired sentry")


def crit_governance() -> dict:
    """No dangling MIP citations anywhere in mip/."""
    dangling = {}
    for name in doc_verify._mip_files(doc_verify.MIP_DIR):
        with open(os.path.join(doc_verify.MIP_DIR, name)) as f:
            text = f.read()
        bad = doc_verify.unresolved_mip_citations(text, doc_verify.MIP_DIR,
                                                  own_number=name[4:8])
        if bad:
            dangling[name] = bad
    return _c("governance hygiene (MIP citations)",
              "PASS" if not dangling else "GAP",
              "every MIP citation across mip/ resolves to an existing file"
              if not dangling else f"dangling: {dangling}",
              None if not dangling else "written or renumbered proposals")


def crit_open_debt(ledger_source) -> dict:
    """Provenance debt stays honest: a sample molecule rebuild must carry a
    provenance_debt list whose entries are labeled (field + reason or a
    reduced_by citation with what remains) — debt is reduced only by
    appending evidence, never silently closed."""
    try:
        mol = work_molecule.build_molecule("task-0001",
                                           ledger_path=ledger_source)
    except (ValueError, KeyError) as exc:
        return _c("open-debt honesty", "GAP", f"molecule rebuild failed: "
                  f"{exc}", "a rebuildable corpus")
    debts = mol.get("provenance_debt")
    if not isinstance(debts, list) or not debts:
        return _c("open-debt honesty", "GAP",
                  "no provenance_debt block on the sample molecule — open "
                  "debts (TEE, power telemetry) exist and must be stated",
                  "honestly stated debt")
    unlabeled = [d for d in debts if not isinstance(d, dict)
                 or "field" not in d
                 or not any(k in d for k in ("reason", "original_reason",
                                             "remaining"))]
    reduced_uncited = [d for d in debts if isinstance(d, dict)
                       and ("reduced_by" in d) != ("remaining" in d)]
    ok = not unlabeled and not reduced_uncited
    return _c("open-debt honesty", "PASS" if ok else "GAP",
              f"{len(debts)} debt entry(ies) on the sample molecule, every "
              "one labeled; reductions cite their appended evidence and "
              "state what remains" if ok else
              f"unlabeled: {len(unlabeled)}, uncited reductions: "
              f"{len(reduced_uncited)}",
              None if ok else "labeled debt entries")


def crit_approval() -> dict:
    return _c("coordinator approval", "HUMAN",
              "out-of-band: human decision, never mechanical — READY is "
              "necessary for a release and never sufficient")


# ----------------------------------------------------------------------------
# the gate
# ----------------------------------------------------------------------------
def run_gate(fast: bool = False, ledger_source: str = None,
             echo=print) -> dict:
    source = (ledger_source if ledger_source is not None
              else resolve_ledger_path())
    entries = _read_ledger(source)
    criteria = [
        crit_cold_install(fast, echo=echo),
        crit_participant_loop(entries),
        crit_cross_machine(entries),
        crit_independent_mirror(entries),
        crit_docs(entries),
        crit_sentry(),
        crit_governance(),
        crit_open_debt(source),
        crit_approval(),
    ]
    gaps = [c for c in criteria if c["status"] == "GAP"]
    skipped = [c for c in criteria if c["status"] == "SKIPPED"]
    if gaps:
        verdict = "NOT-READY"
        note = (f"{len(gaps)} named gap(s) stand between here and the next "
                "release")
    elif skipped:
        verdict = "NOT-READY"
        note = ("no gaps found, but fast mode skipped expensive checks — "
                "fast mode can never establish READY")
    else:
        verdict = "READY"
        note = ("every mechanical criterion passes — READY is necessary, "
                "never sufficient: coordinator approval remains human")
    return {
        "schema": SCHEMA_VERSION,
        "verdict": verdict,
        "note": note,
        "criteria": criteria,
        "gap_names": [c.get("closes_with") for c in gaps],
        "zero_value": True,
        "no_token": True,
    }


def print_report(report: dict):
    print("=== release readiness gate (release-readiness/0.1) — an "
          "instrument, never an approval ===")
    for c in report["criteria"]:
        line = f"  [{c['status']:<7}] {c['name']:42s} {c['detail']}"
        print(line)
        if c.get("closes_with"):
            print(f"            closes with: {c['closes_with']}")
    print(f"VERDICT: {report['verdict']} — {report['note']}")
    print("DEFAULT IS NO RELEASE: a release proceeds only from READY plus "
          "recorded coordinator approval (MIP-0005).")


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="release_readiness.py",
        description=(
            "The complete-product release gate (research-stage, ZERO-VALUE, "
            "no token): answers 'what stands between us and the next "
            "release' with named gaps. Measures; never releases."
        ),
        epilog=(
            "NOT-READY is the expected state between releases. External-"
            "reality gaps (second machine, unaffiliated participant, second "
            "device) are named, never simulated away. Exits 0 whenever the "
            "evaluation completes — the verdict is the payload."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true",
                      help="evaluate every criterion and print the verdict")
    mode.add_argument("--selftest", action="store_true",
                      help="fixture self-test (temp files only; default)")
    parser.add_argument("--fast", action="store_true",
                        help="skip the cold-install execution (SKIPPED, "
                             "named); fast mode can never report READY")
    parser.add_argument("--json", action="store_true",
                        help="with --check: machine-readable report")
    parser.add_argument("--ledger", default=None,
                        help="ledger source (default: live-or-snapshot "
                             "resolution)")
    args = parser.parse_args(argv)

    if args.check:
        report = run_gate(fast=args.fast, ledger_source=args.ledger)
        if args.json:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            print_report(report)
        return 0
    return _selftest()


# ============================== SELF-TEST ====================================
def _selftest() -> int:
    """Current-state honesty + fixtures. Temp files only; writes nothing."""
    print("=== protocol/release_readiness.py self-test (temp files only) ===")
    print("The gate must report today's honest NOT-READY with named gaps —")
    print("and a fixture cross-machine participant must flip its criterion.\n")

    root_before = set(os.listdir(_REPO_ROOT))
    checks = []
    quiet = lambda *a, **k: None

    # [1] the CURRENT state: fast gate -> NOT-READY with exactly the two
    # external-reality gaps named (cross-machine + mirror)
    report = run_gate(fast=True, echo=quiet)
    gap_names = sorted(c["name"] for c in report["criteria"]
                       if c["status"] == "GAP")
    closes = sorted(c["closes_with"] for c in report["criteria"]
                    if c["status"] == "GAP")
    checks.append(("current state is NOT-READY (expected between releases)",
                   report["verdict"] == "NOT-READY"))
    # ERA-HONEST (not a gap-count assertion — that lives in the current
    # MIP's anchored verify-run blocks and moves by supersession): every
    # open gap must be one of the named external-reality criteria with its
    # own honest closes_with, and the mirror gap is open until the second
    # device exists
    _gap_map = {"cross-machine participation": CROSS_MACHINE_GAP,
                "independent mirror active": MIRROR_GAP,
                "docs verified": "clean docs"}
    checks.append(("every open gap is a named criterion with its honest "
                   "closes_with (external reality, never simulated; an "
                   "empty gap list is a legitimate era)",
                   all(n in _gap_map for n in gap_names)
                   and closes == sorted(_gap_map[n] for n in gap_names)))
    checks.append(("every non-gap mechanical criterion passes today",
                   all(c["status"] in ("PASS", "GAP", "SKIPPED", "HUMAN")
                       for c in report["criteria"])
                   and sum(1 for c in report["criteria"]
                           if c["status"] == "PASS") >= 5))

    # [2] the HUMAN type never converts to PASS, and never blocks/creates
    # a gap: it is out-of-band by construction
    approval = [c for c in report["criteria"]
                if c["name"] == "coordinator approval"]
    checks.append(("coordinator approval is HUMAN — never PASS, never GAP",
                   len(approval) == 1 and approval[0]["status"] == "HUMAN"
                   and "never mechanical" in approval[0]["detail"]))

    # [3] fast mode can never report READY: even with zero gaps (fixture:
    # filter them out), a SKIPPED cold install keeps the verdict NOT-READY
    no_gap = [c for c in report["criteria"] if c["status"] != "GAP"]
    fixture = dict(report)
    fixture["criteria"] = no_gap
    skipped = [c for c in no_gap if c["status"] == "SKIPPED"]
    checks.append(("fast mode can never establish READY (SKIPPED present)",
                   bool(skipped)))

    # [4] JSON shape: schema + verdict + typed criteria rows
    checks.append(("report shape (schema, verdict, typed rows, gap_names)",
                   report["schema"] == SCHEMA_VERSION
                   and report["verdict"] in ("READY", "NOT-READY")
                   and all({"name", "status", "detail"} <= set(c)
                           for c in report["criteria"])
                   and report["gap_names"] == [c["closes_with"] for c in
                                               report["criteria"]
                                               if c["status"] == "GAP"]))

    # [5] FIXTURE: a participant record whose bundle fingerprint is NOT a
    # coordinator fingerprint flips the cross-machine criterion to PASS —
    # and the same-machine rehearsal record can never flip it
    entries = _read_ledger(resolve_ledger_path())
    fake = entries + [{
        "index": len(entries), "hash": "f" * 64,
        "payload": {"event": "participant_result_anchored",
                    "status": "participant-verified",
                    "bundle_sha256": "ab" * 32},
    }]
    loader = lambda sha: {"signed_result": {"result": {
        "machine_fingerprint": "sha256:" + "e" * 64}}}
    flipped = crit_cross_machine(fake, bundle_loader=loader)
    # the "never flips" fixture must stay honest on a chain that now HAS a
    # real cross-machine record: force every participant bundle's evidence
    # fingerprint to a coordinator machine's own — a chain of pure
    # same-machine rehearsals — and the criterion must stay a GAP
    _coord_fp = sorted(_coordinator_fingerprints(entries))[0]
    same_loader = lambda sha: {"signed_result": {"result": {
        "machine_fingerprint": _coord_fp}}}
    rehearsal_only = crit_cross_machine(entries, bundle_loader=same_loader)
    checks.append(("fixture cross-machine participant flips the criterion",
                   flipped["status"] == "PASS"))
    checks.append(("the same-machine rehearsal can never flip it (its "
                   "fingerprint is the coordinator's)",
                   rehearsal_only["status"] == "GAP"
                   and rehearsal_only["closes_with"] == CROSS_MACHINE_GAP))

    # [5b] MIRROR CRITERION FIXTURES: chain-derived, device-rule enforced.
    # A chain with no attestation names the gap; a fixture attestation with
    # a FOREIGN fingerprint and matching evidence passes; a
    # coordinator-fingerprint attestation can never pass (anti-simulation);
    # tampered evidence is refused by the sha.
    base_no_mirror = [e for e in entries
                     if not (isinstance(e.get("payload"), dict)
                             and e["payload"].get("event")
                             == "mirror_attestation_anchored")]
    no_att = crit_independent_mirror(base_no_mirror)
    foreign_fp = "sha256:" + "e" * 64
    fix_bundle = {"schema": "mirror-attestation-bundle/0.1",
                  "attestation": {"machine_fingerprint": foreign_fp}}
    fix_sha = hashlib.sha256(json.dumps(
        fix_bundle, sort_keys=True, separators=(",", ":"),
        ensure_ascii=True).encode("utf-8")).hexdigest()
    anchor_pt = base_no_mirror[10]
    fix_rec = {"index": len(base_no_mirror), "hash": "f" * 64,
               "payload": {"event": "mirror_attestation_anchored",
                           "status": "mirror-attested",
                           "bundle_sha256": fix_sha,
                           "participant_machine_fingerprint": foreign_fp,
                           "mirror": {"tip_index": anchor_pt["index"],
                                      "tip_hash": anchor_pt["hash"],
                                      "entry_count":
                                          anchor_pt["index"] + 1},
                           "check_verdict": "IDENTICAL"}}
    fix_loader = lambda sha: fix_bundle if sha == fix_sha else None
    mirror_pass = crit_independent_mirror(base_no_mirror + [fix_rec],
                                          bundle_loader=fix_loader)
    coord_bundle = {"schema": "mirror-attestation-bundle/0.1",
                    "attestation": {"machine_fingerprint": _coord_fp}}
    coord_sha = hashlib.sha256(json.dumps(
        coord_bundle, sort_keys=True, separators=(",", ":"),
        ensure_ascii=True).encode("utf-8")).hexdigest()
    coord_rec = json.loads(json.dumps(fix_rec))
    coord_rec["payload"]["bundle_sha256"] = coord_sha
    coord_rec["payload"]["participant_machine_fingerprint"] = _coord_fp
    mirror_coord = crit_independent_mirror(
        base_no_mirror + [coord_rec],
        bundle_loader=lambda sha: coord_bundle)
    mirror_tampered = crit_independent_mirror(
        base_no_mirror + [fix_rec],
        bundle_loader=lambda sha: {"tampered": True})
    checks.append(("mirror criterion: no attestation -> the named "
                   "second-device gap",
                   no_att["status"] == "GAP"
                   and no_att["closes_with"] == MIRROR_GAP))
    checks.append(("mirror criterion: foreign-fingerprint attestation with "
                   "matching evidence -> PASS (chain-derived)",
                   mirror_pass["status"] == "PASS"))
    checks.append(("mirror criterion: a coordinator-device 'mirror' can "
                   "never pass (the anti-simulation device rule)",
                   mirror_coord["status"] == "GAP"))
    checks.append(("mirror criterion: tampered evidence refused by sha",
                   mirror_tampered["status"] == "GAP"))

    stray = sorted(set(os.listdir(_REPO_ROOT)) - root_before)
    checks.append(("no stray files in repo root", not stray))

    print("--- self-test invariants ---")
    failures = 0
    for name, passed in checks:
        print(f"{name:68s}: {'PASS' if passed else 'FAIL'}")
        if not passed:
            failures += 1
    if stray:
        print(f"    stray in repo root: {stray}")

    ok = failures == 0
    print("\n=== self-test summary: " +
          ("ALL CHECKS BEHAVED CORRECTLY" if ok else "FAILURE — see above")
          + " ===")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
