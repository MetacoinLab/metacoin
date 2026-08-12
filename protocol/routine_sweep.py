# Copyright (c) 2023-2026 MetaCoin-Lab.
# Licensed under SML-1.0 — see LICENSE.md.
"""routine_sweep.py — COORDINATOR ROUTINE SWEEP v1 (operational tooling).

================== HONESTY / SCOPE (READ ME) ==================
Research-stage, ZERO-VALUE, no token. Standard library only.

THE STANDING COMMAND: one invocation the coordinator runs before every work
session and weekly from now on. It re-verifies the full stack and reports ONE
verdict — SWEEP CLEAN, or SWEEP FINDINGS (n) with a non-zero exit. Sections,
in order:

  1. layers      — verify_everything --full (every anchored layer re-derived)
  2. suites      — both self-test suites (protocol + demo), counts reported
  3. kit         — continuity --verify-kit on the standing kit path
                   (reserves included; named skip if no kit on this machine)
  4. mirrors     — continuity --check-mirror against every configured mirror
                   directory (named skip if none; DIVERGED is a finding)
  5. identity    — actor_identity --identity-health summary with the risk
                   lines (alarming states are findings; a machine with no
                   local keychains — CI, fresh clones — takes a NAMED skip:
                   the public corpus carries verifiability, never
                   operatorship)
  6. evidence    — evidence-bundle reconciliation: every protocol/evidence/
                   file re-checked against its anchoring record (orphans,
                   tampered self-hashes, and derivably-missing files NAMED)
  7. git         — hygiene: clean tree, no unexpected untracked files, HEAD
                   == origin/main (or named divergence), CI status of HEAD
                   (named skip when offline/unavailable)
  8. drift       — EXPECTED EVOLUTION, never findings: the live-vs-anchored
                   generation drift set, path_count and current-chain ACI vs
                   the NEWEST anchored epoch observation (the frozen pairwise
                   baseline stays cited as epoch zero; it is the comparison
                   point only until a first epoch is anchored), and the
                   sampled current-chain ACI_k profile (typed, with
                   intervals). The sweep's job is to notice UNEXPECTED
                   change; expected drift is listed under its own heading.
  8b. mip        — anchored governance decisions re-derive: cited file
                   present, sha256 matches the anchored pin (immutability-
                   by-citation), structural checks pass; NAMED skip while
                   the chain carries no MIP decisions.
  8d. private-repo — coordinator-side hygiene of the private incubation
                   clone (~/projects/metacoin-lab): the same forbidden-
                   material patterns the public repo enforces (keychains,
                   secrets, ledger, kit) — findings on any hit; NAMED
                   skip when the clone is absent (CI, fresh clones).
  8c. release    — the release-readiness gate's verdict + named gaps as
                   INFORMATION (MIP-0005: NOT-READY is the expected state
                   between releases; a change in the gap list deserves a
                   glance, never an alarm — this section cannot fail).
  9. docs        — the verified documentation suite: doc_verify --check
                   (every doc command executed, every number chain-checked,
                   every idx reference resolved; named skip if docs/ absent)

ZERO LEDGER WRITES: the sweep only reads. The self-test asserts the real
ledger byte-identical before/after (the continuity idiom).

REPORT, NOT PROTOCOL OBJECT: the sweep report may show the run date because
NOTHING in it is hashed or anchored — the no-timestamps rule binds hashed
artifacts (reports, catalogs, certificates), not operational logs. The
distinction is stated in the report itself.

EVIDENCE RECONCILIATION MECHANICS (the audit's manual check, automated): a
file reconciles when an anchored record cites it — by content hash (sha256
of the file's canonical JSON or raw bytes, or a recognized self-hash field
value appearing in a payload), by the 12-hex id embedded in its filename
(prefix of a cited 64-hex value), or by the named-drill rule (the two
planned-rejection demonstration inputs, cited by their anchored drill
events). Files carrying a recognized self-hash field must also RECOMPUTE it
(sha256 of the canonical document minus that field) — a tampered copy is
named even though its interior citations still match. The reverse direction
derives expected basenames from the ledger (signed challenge rounds, uptime
epochs, rotations, drills, participant bundles, task submissions, anchored
catalogs/reports) and names anything missing. Deep re-derivation of contents
stays verify_everything's job (layer 1); this section audits LINKAGE and
self-integrity.

Usage:
    python3 protocol/routine_sweep.py            # the standing sweep
    python3 protocol/routine_sweep.py --json     # machine-readable report
    python3 protocol/routine_sweep.py --selftest # fixtures + temp files only
"""

# Suppress __pycache__/*.pyc so importing protocol modules leaves no stray files.
import sys
sys.dont_write_bytecode = True

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import time

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

_PROTO_DIR = os.path.dirname(os.path.abspath(__file__))

import protocol.actor_identity as actor_identity
import protocol.agent_concentration as agent_concentration
import protocol.continuity as continuity
import protocol.work_molecule as work_molecule

SWEEP_SCHEMA = "routine-sweep-report/0.1"  # ops report: never hashed, never anchored

# Standing paths the sweep checks on the coordinator machine.
DEFAULT_KIT_DIR = "continuity_kit"
DEFAULT_MIRROR_DIRS = ("mirror_export",)

# Recognized SELF-HASH fields (the anti-circularity pattern: the field is the
# sha256 of the canonical document with the field itself excluded).
_SELF_HASH_FIELDS = ("report_hash", "catalog_hash", "certificate_hash",
                     "epoch_hash", "economy_log_hash", "bundle_hash")
# Document fields whose values anchored records cite directly.
_CITED_FIELDS = ("challenge_id", "output_hash", "new_root", "prev_root")
# The two planned-drill demonstration inputs, cited by their anchored events
# (whose payloads carry no hash of the file — the drill record IS the citation).
_STATIC_DRILLS = {"heartbeat_forged_drill.json": "heartbeat_rejected",
                  "rotation_forged_drill.json": "actor_key_rotation_rejected"}
# Anchoring events -> statically named evidence artifacts.
_STATIC_EXPECTED = {
    "aci_baseline_anchored": ("aci_report.json",),
    "aci_korder_baseline_anchored": ("aci_korder_report.json",),
    # economy_demo_summary_anchored is handled per-generation in
    # _expected_evidence (gen-1 keeps the static economy_log.json name;
    # later generations ship economy_log_gen<N>.json)
    "metering_evidence_anchored": ("metering_report.json",),
    "cut_certificate_anchored": ("cut_cert.json",),
    "trust_vector_catalog_anchored": ("tv_catalog.json",),
    "passport_catalog_anchored": ("passport_catalog.json",),
    "heartbeat_rejected": ("heartbeat_forged_drill.json",),
    "actor_key_rotation_rejected": ("rotation_forged_drill.json",),
}

_HEX64 = re.compile(r"[0-9a-f]{64}")
_NAME_ID = re.compile(r"_([0-9a-f]{12})\.json$")


def canonical_json(obj) -> str:
    """Canonical JSON: sorted keys, compact separators, ASCII — byte-stable for hashing."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _section(name, findings, details, skipped=False):
    return {"section": name,
            "status": ("skip" if skipped else
                       "findings" if findings else "pass"),
            "findings": findings, "details": details}


def _run(cmd, cwd, timeout):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                          timeout=timeout)


# ----------------------------------------------------------------------------
# 1+2: layers and suites (subprocess — each tool judges itself; we read verdicts)
# ----------------------------------------------------------------------------
def section_layers(base_dir: str = _REPO_ROOT) -> dict:
    findings, details = [], []
    try:
        r = _run([sys.executable,
                  os.path.join(base_dir, "protocol", "verify_everything.py"),
                  "--full"], base_dir, 900)
        line = next((ln for ln in r.stdout.splitlines()
                     if ln.startswith("RESULT")), "no RESULT line")
        if r.returncode == 0 and "ALL LAYERS PASS" in line:
            details.append(f"verify_everything --full: {line}")
        else:
            findings.append(f"verify_everything --full FAILED: {line} "
                            f"(exit {r.returncode})")
    except (OSError, subprocess.SubprocessError) as exc:
        findings.append(f"verify_everything --full could not run: {exc}")
    return _section("layers", findings, details)


def parse_suite_summary(text: str):
    """The runners' summary line: 'N/M passed, F failed' -> (N, M, F) or None."""
    m = re.search(r"(\d+)/(\d+) passed, (\d+) failed", text)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def section_suites(base_dir: str = _REPO_ROOT) -> dict:
    findings, details = [], []
    for label, runner in (("protocol", os.path.join("protocol",
                                                    "run_protocol_selftests.sh")),
                          ("demo", os.path.join("demo",
                                                "run_all_selftests.sh"))):
        try:
            r = _run(["bash", os.path.join(base_dir, runner)], base_dir, 1800)
            parsed = parse_suite_summary(r.stdout)
            if parsed is None:
                findings.append(f"{label} suite: no summary line found "
                                f"(exit {r.returncode})")
            elif parsed[2] or r.returncode != 0:
                findings.append(f"{label} suite: {parsed[0]}/{parsed[1]} "
                                f"passed, {parsed[2]} FAILED "
                                f"(exit {r.returncode})")
            else:
                details.append(f"{label} suite: {parsed[0]}/{parsed[1]} passed")
        except (OSError, subprocess.SubprocessError) as exc:
            findings.append(f"{label} suite could not run: {exc}")
    return _section("suites", findings, details)


# ----------------------------------------------------------------------------
# 3+4+5: continuity kit, mirrors, identity health
# ----------------------------------------------------------------------------
def section_kit(base_dir: str = _REPO_ROOT, kit_dir: str = None) -> dict:
    kit = kit_dir or os.path.join(base_dir, DEFAULT_KIT_DIR)
    if not os.path.isdir(kit):
        return _section("kit", [], [f"no continuity kit at {kit} — SKIPPED "
                                    "(named; export one before relying on "
                                    "this machine surviving)"], skipped=True)
    findings, details = [], []
    try:
        ok, results = continuity.verify_kit(kit, base_dir=base_dir)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return _section("kit", [f"verify-kit could not run on {kit}: {exc}"],
                        [])
    good = sum(1 for r in results if r["status"] in ("ok", "reserve-ok"))
    details.append(f"verify-kit {kit}: {good}/{len(results)} entries clean")
    for r in results:
        if r["status"] in ("ok", "reserve-ok"):
            continue
        line = f"{r['path']}: [{r['status']}] {r['detail']}"
        if r["status"] == "reserve-retired":
            details.append("FLAGGED " + line)  # retired reserves: loud, not fatal
        else:
            findings.append(line)
    if not ok and not findings:
        findings.append("verify-kit reported failure without a named row")
    return _section("kit", findings, details)


def section_mirrors(base_dir: str = _REPO_ROOT, mirror_dirs=None) -> dict:
    candidates = [os.path.join(base_dir, d)
                  for d in (mirror_dirs or DEFAULT_MIRROR_DIRS)]
    present = [d for d in candidates if os.path.isdir(d)]
    if not present:
        return _section("mirrors", [],
                        ["no configured mirror directories present — SKIPPED "
                         "(named; the second device's standing job once it "
                         "arrives)"], skipped=True)
    findings, details = [], []
    for d in present:
        try:
            v = continuity.check_mirror(d, base_dir=base_dir)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            findings.append(f"{d}: check-mirror could not run: {exc}")
            continue
        if v["verdict"] in ("IDENTICAL", "BEHIND"):
            details.append(f"{d}: {v['verdict']} — {v['detail']}")
        else:
            findings.append(f"{d}: {v['verdict']} — {v['detail']}")
    return _section("mirrors", findings, details)


# Risk-line fragments that make an identity risk a FINDING (not just a fact).
_IDENTITY_ALARMS = ("EXHAUSTED", "FAILS verification", "STALE",
                    "no local keychain", "no reserve")


def section_identity(base_dir: str = _REPO_ROOT) -> dict:
    # PUBLIC-CORPUS MODE (CI, fresh clones): with no local keychain files
    # this machine holds no operatorship, so identity health is not this
    # machine's check — a NAMED skip, exactly like the kit and mirrors. The
    # coordinator machine (which holds keychains) always runs the full check.
    if not any(n.startswith("keychain") and n.endswith(".json")
               for n in os.listdir(base_dir)):
        return _section("identity", [],
                        ["no local keychains on this machine — identity "
                         "health is a coordinator-machine check; SKIPPED "
                         "(named; the public corpus carries verifiability, "
                         "never operatorship)"], skipped=True)
    findings, details = [], []
    try:
        doc = actor_identity.identity_health(base_dir=base_dir)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return _section("identity", [f"identity-health could not run: {exc}"],
                        [])
    for a in doc["actors"]:
        details.append(
            f"{a['actor_id']}: root {a['active_root'][:16]}.., "
            f"{a['keys_remaining']}/{a['keys_total']} keys remaining, "
            f"reserve {a['reserve']['state']}")
        for risk in a["risks"]:
            if any(alarm in risk for alarm in _IDENTITY_ALARMS):
                findings.append(f"{a['actor_id']}: {risk}")
            else:
                details.append(f"  risk: {risk}")
    return _section("identity", findings, details)


# ----------------------------------------------------------------------------
# 6: evidence-bundle reconciliation (linkage + self-integrity, both directions)
# ----------------------------------------------------------------------------
def _entry_tokens(entries):
    """Per-entry 64-hex token sets drawn from the canonical payload."""
    out = []
    for e in entries:
        p = e.get("payload") if isinstance(e, dict) else None
        if isinstance(p, dict):
            out.append((e.get("index"), set(_HEX64.findall(canonical_json(p))),
                        p))
    return out


def _expected_evidence(entries) -> dict:
    """{basename: 'ledger:N (why)'} — evidence files the ledger says must exist."""
    expected = {}

    def want(name, idx, why):
        expected.setdefault(name, f"ledger:{idx} ({why})")

    for e in entries:
        p = e.get("payload") if isinstance(e, dict) else None
        if not isinstance(p, dict):
            continue
        idx = e.get("index")
        event = p.get("event")
        for name in _STATIC_EXPECTED.get(event, ()):
            want(name, idx, event)
        if (event == "challenge_response_result"
                and "signature_valid" in p
                and isinstance(p.get("challenge_id"), str)):
            cid12 = p["challenge_id"][:12]
            want(f"challenge_{cid12}.json", idx, "signed challenge round")
            want(f"response_{cid12}.json", idx, "signed challenge round")
        if event == "uptime_epoch_anchored" and isinstance(
                p.get("epoch_hash"), str):
            want(f"uptime_epoch_{p['epoch_hash'][:12]}.json", idx,
                 "anchored uptime epoch")
        if (event == "actor_key_rotated"
                and p.get("status") == "actor-key-rotated"
                and isinstance(p.get("new_root"), str)):
            want(f"rotation_cert_{p['new_root'][:12]}.json", idx,
                 "anchored rotation")
        if isinstance(p.get("bundle_sha256"), str):
            want(f"participant_bundle_{p['bundle_sha256'][:12]}.json", idx,
                 "participant intake record")
        if (event == "self_recompute_result"
                and isinstance(p.get("task_id"), str)):
            want(f"sub_{p['task_id']}.json", idx, "self-recompute submission")
        if (event == "external_verification_result"
                and p.get("task_id") in work_molecule._CATALOG_SUBMISSIONS):
            want(work_molecule._CATALOG_SUBMISSIONS[p["task_id"]], idx,
                 "external verification submission")
        if (event == "economy_demo_summary_anchored"
                and p.get("status") == "economy-demo-confirmed"):
            gen = p.get("generation", 1)
            want("economy_log.json" if gen == 1
                 else f"economy_log_gen{gen}.json", idx,
                 f"anchored economy generation {gen}")
        if event == "work_molecule_catalog_anchored":
            want("wm_catalog.json" if p.get("molecule_schema")
                 == "work-molecule/0.2" else "wm_catalog_v03.json", idx,
                 "anchored molecule catalog")
        if (event == "aci_epoch_observed"
                and p.get("status") == "aci-epoch-confirmed"
                and isinstance(p.get("report_hash"), str)):
            # the longitudinal series ships one hash-named copy per epoch
            want(f"aci_epoch_{p['report_hash'][:12]}.json", idx,
                 "anchored ACI epoch observation")
        if (event == "cut_certificate_anchored"
                and isinstance(p.get("boundary_count"), int)
                and p["boundary_count"] > 0
                and isinstance(p.get("certificate_hash"), str)):
            # non-trivial cuts (a real edge crosses the boundary) ship a
            # hash-named evidence copy; the first degenerate cut keeps the
            # static cut_cert.json name in _STATIC_EXPECTED
            want(f"cut_cert_{p['certificate_hash'][:12]}.json", idx,
                 "anchored non-trivial cut certificate")
    return expected


def reconcile_evidence(evidence_dir: str, entries: list):
    """Both directions of the reconciliation. Returns (findings, details)."""
    findings, details = [], []
    if not os.path.isdir(evidence_dir):
        return ([f"evidence directory missing: {evidence_dir}"], [])
    toks = _entry_tokens(entries)
    kinds = {"hash": 0, "prefix": 0, "named-drill": 0}
    names = sorted(n for n in os.listdir(evidence_dir)
                   if os.path.isfile(os.path.join(evidence_dir, n)))
    for name in names:
        path = os.path.join(evidence_dir, name)
        with open(path, "rb") as f:
            raw = f.read()
        try:
            doc = json.loads(raw)
        except json.JSONDecodeError:
            doc = None
        # self-integrity: a recognized self-hash field must recompute
        if isinstance(doc, dict):
            for field in _SELF_HASH_FIELDS:
                if isinstance(doc.get(field), str):
                    recomputed = _sha256_hex(canonical_json(
                        {k: v for k, v in doc.items()
                         if k != field}).encode("utf-8"))
                    if recomputed != doc[field]:
                        findings.append(
                            f"TAMPERED: {name} — its {field} does not "
                            "recompute from its own content (the "
                            "anti-circularity self-hash is broken)")
                    break
        # linkage: an anchored record must cite this file
        candidates = {_sha256_hex(raw)}
        if doc is not None:
            candidates.add(_sha256_hex(canonical_json(doc).encode("utf-8")))
            if isinstance(doc, dict):
                for field in _SELF_HASH_FIELDS + _CITED_FIELDS:
                    if isinstance(doc.get(field), str):
                        candidates.add(doc[field])
        cited = None
        for idx, tokset, payload in toks:
            if candidates & tokset:
                cited = (idx, "hash")
                break
        if cited is None:
            m = _NAME_ID.search(name)
            if m:
                for idx, tokset, payload in toks:
                    if any(t.startswith(m.group(1)) for t in tokset):
                        cited = (idx, "prefix")
                        break
        if cited is None and name in _STATIC_DRILLS:
            for idx, tokset, payload in toks:
                if payload.get("event") == _STATIC_DRILLS[name]:
                    cited = (idx, "named-drill")
                    break
        if cited is None:
            findings.append(f"ORPHAN: {name} — no anchored record cites it "
                            "(by hash, filename id, or drill event); every "
                            "published evidence file must have an anchoring "
                            "record")
        else:
            kinds[cited[1]] += 1
    for name, why in sorted(_expected_evidence(entries).items()):
        if name not in names:
            findings.append(f"MISSING: {name} — required by {why} but absent "
                            "from the evidence bundle")
    details.append(f"{len(names)} evidence files checked: "
                   + ", ".join(f"{v} reconciled by {k}"
                               for k, v in sorted(kinds.items()) if v)
                   + f"; {len(_expected_evidence(entries))} ledger-derived "
                     "expectations checked")
    details.append("deep content re-derivation is layer 1's job "
                   "(verify_everything); this section audits linkage + "
                   "self-hash integrity")
    return (findings, details)


def section_evidence(base_dir: str = _REPO_ROOT, evidence_dir: str = None,
                     ledger_source=None) -> dict:
    evidence = evidence_dir or os.path.join(base_dir, "protocol", "evidence")
    entries = actor_identity._read_entries(
        ledger_source if ledger_source is not None
        else actor_identity._default_ledger_source(base_dir))
    findings, details = reconcile_evidence(evidence, entries)
    return _section("evidence", findings, details)


# ----------------------------------------------------------------------------
# 7: git hygiene
# ----------------------------------------------------------------------------
def section_git(repo_dir: str = _REPO_ROOT) -> dict:
    findings, details = [], []
    try:
        status = _run(["git", "status", "--porcelain"], repo_dir, 60)
    except (OSError, subprocess.SubprocessError) as exc:
        return _section("git", [], [f"git unavailable — SKIPPED (named): "
                                    f"{exc}"], skipped=True)
    if status.returncode != 0:
        return _section("git", [], ["not a git checkout — SKIPPED (named)"],
                        skipped=True)
    lines = [ln for ln in status.stdout.splitlines() if ln.strip()]
    for ln in lines:
        kind = ("untracked file outside the ignore set"
                if ln.startswith("??") else "uncommitted change")
        findings.append(f"dirty tree: {kind}: {ln[3:]}")
    if not lines:
        details.append("working tree clean (no modifications, no unexpected "
                       "untracked files)")

    head = _run(["git", "rev-parse", "HEAD"], repo_dir, 60)
    origin = _run(["git", "rev-parse", "origin/main"], repo_dir, 60)
    if origin.returncode != 0:
        details.append("no origin/main ref — remote comparison SKIPPED (named)")
    elif head.stdout.strip() == origin.stdout.strip():
        details.append(f"HEAD == origin/main ({head.stdout.strip()[:12]})")
    else:
        counts = _run(["git", "rev-list", "--left-right", "--count",
                       "origin/main...HEAD"], repo_dir, 60)
        behind, ahead = (counts.stdout.split()
                         if counts.returncode == 0 else ("?", "?"))
        findings.append(f"DIVERGENCE: HEAD {head.stdout.strip()[:12]} != "
                        f"origin/main {origin.stdout.strip()[:12]} "
                        f"({ahead} ahead, {behind} behind) — push or "
                        "reconcile before the work session")

    ci_note = "CI status: SKIPPED (named) — "
    if shutil.which("gh") is None:
        details.append(ci_note + "gh CLI not available")
    else:
        try:
            r = _run(["gh", "run", "list", "--commit", head.stdout.strip(),
                      "--limit", "1", "--json", "status,conclusion"],
                     repo_dir, 30)
            runs = json.loads(r.stdout) if r.returncode == 0 else None
            if not runs:
                details.append(ci_note + "no CI run found for HEAD (offline, "
                               "unpushed, or not yet started)")
            elif runs[0].get("status") != "completed":
                details.append(f"CI for HEAD: {runs[0].get('status')} "
                               "(in flight — not a finding)")
            elif runs[0].get("conclusion") == "success":
                details.append("CI for HEAD: success")
            else:
                findings.append(f"CI for HEAD concluded "
                                f"{runs[0].get('conclusion')!r} — "
                                "investigate before working on top of it")
        except (OSError, subprocess.SubprocessError,
                json.JSONDecodeError) as exc:
            details.append(ci_note + f"gh failed ({exc})")
    return _section("git", findings, details)


# ----------------------------------------------------------------------------
# 8: drift — EXPECTED EVOLUTION, reported under its own heading, never failure
# ----------------------------------------------------------------------------
def section_drift(base_dir: str = _REPO_ROOT, ledger_source=None) -> dict:
    details = []
    src = (ledger_source if ledger_source is not None
           else actor_identity._default_ledger_source(base_dir))
    entries = actor_identity._read_entries(src)

    last_cat = None
    idx18 = idxko = last_epoch = None
    for e in entries:
        p = e.get("payload") if isinstance(e, dict) else None
        if not isinstance(p, dict):
            continue
        if p.get("event") == "work_molecule_catalog_anchored":
            last_cat = e
        elif p.get("event") == "aci_baseline_anchored":
            idx18 = e
        elif p.get("event") == "aci_korder_baseline_anchored":
            idxko = e
        elif (p.get("event") == "aci_epoch_observed"
                and p.get("status") == "aci-epoch-confirmed"):
            last_epoch = e

    # generation drift: which tasks' live molecules moved past the last
    # anchored catalog generation (new verification events -> new WMIDs)
    if last_cat is not None:
        lock = last_cat["index"] - 1
        drifted, unanchored = [], []
        for tid in sorted(work_molecule.TASK_MODULES):
            sub = None
            if tid in work_molecule._CATALOG_SUBMISSIONS:
                sub = work_molecule.find_evidence_file(
                    work_molecule._CATALOG_SUBMISSIONS[tid])
            try:
                live = work_molecule.build_molecule(tid, ledger_path=src,
                                                    submission_path=sub)
            except ValueError:
                # registry task with no ledger records yet: no molecule exists
                # on ANY chain state — typed below, never a crash or a finding
                unanchored.append(tid)
                continue
            try:
                locked = work_molecule.build_molecule(tid, ledger_path=src,
                                                      submission_path=sub,
                                                      as_of_index=lock)
            except ValueError:
                # recorded only AFTER the lock point: the molecule did not
                # exist in the anchored generation at all — maximal drift,
                # absorbed by the next catalog generation
                drifted.append(tid)
                continue
            if live["work_id"] != locked["work_id"]:
                drifted.append(tid)
        details.append(
            f"EXPECTED EVOLUTION — generation drift vs the catalog anchored "
            f"at ledger:{last_cat['index']}: {len(drifted)} molecule(s) "
            f"absorbed post-anchor events {drifted} — new generations are "
            "how the catalog evolves; never a rewrite")
        if unanchored:
            details.append(
                f"EXPECTED EVOLUTION — registered-unanchored tasks: "
                f"{len(unanchored)} {unanchored} have no ledger records yet "
                "(the cadence policy: new tasks join the corpus at the next "
                "milestone anchor batch; until then they are registry-only)")

    paths = agent_concentration.build_paths(ledger_path=src)
    if last_epoch is not None:
        # the longitudinal series took over as the comparison point: current
        # vs the NEWEST anchored epoch; the frozen pairwise baseline stays
        # cited as epoch zero, never displaced
        pe = last_epoch["payload"]
        rep = agent_concentration.compute_report(paths)
        zero = (f"; frozen epoch-zero baseline ledger:{idx18['index']} "
                f"({idx18['payload'].get('path_count')} paths, pairwise "
                f"{idx18['payload'].get('pairwise_aci'):.6f}) stands"
                if idx18 is not None else "")
        details.append(
            f"EXPECTED EVOLUTION — path_count now {len(paths)} vs "
            f"{pe.get('path_count')} at the newest anchored epoch "
            f"(ledger:{last_epoch['index']}, as-of "
            f"{pe.get('as_of_ledger_index')}); pairwise ACI now "
            f"{rep['pairwise_aci']:.6f} vs epoch {pe.get('pairwise_aci'):.6f}"
            f" (same-operator accumulation){zero}")
    elif idx18 is not None:
        p18 = idx18["payload"]
        rep = agent_concentration.compute_report(paths)
        details.append(
            f"EXPECTED EVOLUTION — path_count now {len(paths)} vs frozen "
            f"{p18.get('path_count')} at the pairwise baseline "
            f"(ledger:{idx18['index']}); pairwise ACI now "
            f"{rep['pairwise_aci']:.6f} vs anchored "
            f"{p18.get('pairwise_aci'):.6f} (same-operator baseline holds)")
    if idxko is not None:
        pko = idxko["payload"]
        ko = agent_concentration.compute_korder_report(paths, k_max=6)
        rows = ", ".join(agent_concentration.profile_row_text(r)
                         for r in ko["profile"])
        details.append(
            f"EXPECTED EVOLUTION — current-chain ACI_k profile (typed; "
            f"sampled rows carry intervals): {rows} | anchored k-order "
            f"baseline (ledger:{idxko['index']}, path_count "
            f"{pko.get('path_count')}, as-of {pko.get('as_of_ledger_index')}) "
            "stands unchanged — a sampled current-chain profile is a sweep "
            "observable, not an anchor")
    if not details:
        details.append("no anchored baselines found to measure drift against")
    # drift is EXPECTED EVOLUTION by definition: this section never emits
    # findings — unexpected change shows up in sections 1-7
    return _section("drift", [], details)


# ----------------------------------------------------------------------------
# 8b: mip — anchored governance decisions re-derive (named skip if none)
# ----------------------------------------------------------------------------
def section_mip(base_dir: str = _REPO_ROOT, ledger_source=None) -> dict:
    """Every anchored MIP decision re-derives: the cited file exists, its
    sha256 matches the anchored pin (immutability-by-citation), and the
    structural mechanical checks still pass. Verify-run blocks execute in
    the docs section's doc_verify run, not here. Named skip if the chain
    carries no MIP decisions yet."""
    import protocol.mip_process as mip_process
    findings, details = [], []
    src = (ledger_source if ledger_source is not None
           else actor_identity._default_ledger_source(base_dir))
    entries = actor_identity._read_entries(src)
    mips = [(e["index"], e["payload"]) for e in entries
            if isinstance(e.get("payload"), dict)
            and e["payload"].get("event") == "mip_decision_recorded"]
    if not mips:
        return _section("mip", [],
                        ["no anchored MIP decisions on the chain yet — "
                         "SKIPPED (named)"], skipped=True)
    for idx, p in mips:
        # shared semantics with verify_everything: frozen records alarm on
        # any drift; retained-as-draft pins are as-reviewed, so an evolved
        # draft is an informational detail, never a finding
        state, note = mip_process.review_drift(p, base_dir)
        if state in ("file-missing", "frozen-BROKEN"):
            findings.append(f"ledger:{idx}: {note}")
            continue
        retained = p.get("decision") == "retained-as-draft"
        fpath = os.path.join(base_dir, p.get("file", ""))
        v = mip_process.check_mip(fpath, ledger_source=src, execute=False,
                                  echo=lambda *a: None,
                                  draft_expectations=retained)
        if not retained and not v["passed"]:
            failed = [c["name"] for c in v["checks"] if not c["passed"]]
            findings.append(f"ledger:{idx}: structural checks fail: {failed}")
            continue
        details.append(f"ledger:{idx}: {p.get('mip_id')} ({p.get('decision')}"
                       f", seat {p.get('review_seat')}) re-derives — {note}")
    return _section("mip", findings, details)


# ----------------------------------------------------------------------------
# 8d: private-repo hygiene — coordinator-side only (named skip elsewhere)
# ----------------------------------------------------------------------------
# The three-layer policy (metacoin-lab/WORKFLOW.md): public repo = product,
# private repo = strategic incubation, offline = keys/ledger/kit. Layer 3
# material must never enter ANY GitHub repo — the public repo enforces this
# with its forbidden-material scans; this section applies the same patterns
# to the private incubation clone, when present on this machine.
PRIVATE_REPO_DIR = os.path.expanduser("~/projects/metacoin-lab")
_PRIVATE_FORBIDDEN = ("keychain*.json", "*.secret", "*.pem", "*.key",
                      "ledger_data.jsonl", "recovery_manifest.json",
                      "continuity_kit")


def section_private_repo(private_dir: str = None) -> dict:
    """Scan the private incubation clone for layer-3 (offline-only) material:
    keychains, signing secrets, key files, the live ledger, the recovery
    manifest, the continuity kit. Any hit is a FINDING (private material in
    a GitHub repo is a policy violation whichever repo it is); an absent
    clone is a NAMED skip (CI and fresh clones have no private checkout)."""
    import fnmatch
    d = private_dir if private_dir is not None else PRIVATE_REPO_DIR
    if not os.path.isdir(d):
        return _section("private-repo", [],
                        [f"no private incubation clone at {d} — SKIPPED "
                         "(named; coordinator-machine check only)"],
                        skipped=True)
    findings, details = [], []
    scanned = 0
    for root, dirs, files in os.walk(d):
        if ".git" in dirs:
            dirs.remove(".git")
        for name in list(dirs) + files:
            scanned += name not in dirs
            for pat in _PRIVATE_FORBIDDEN:
                if fnmatch.fnmatch(name, pat):
                    rel = os.path.relpath(os.path.join(root, name), d)
                    findings.append(
                        f"forbidden (offline-only) material in the private "
                        f"repo: {rel} matches {pat!r} — layer-3 files never "
                        "enter ANY GitHub repository")
    if not findings:
        details.append(f"private incubation clone clean: {scanned} file(s) "
                       "scanned, no keychain/secret/ledger/kit patterns "
                       "(the same forbidden-material discipline as the "
                       "public repo)")
    return _section("private-repo", findings, details)


# ----------------------------------------------------------------------------
# 8c: release — the readiness gate, INFORMATIONAL by definition
# ----------------------------------------------------------------------------
def section_release(base_dir: str = _REPO_ROOT) -> dict:
    """The release-readiness verdict as INFORMATION, never findings:
    NOT-READY is the expected state between releases (MIP-0005), so the gap
    list is reported under this heading the way drift is — a change in the
    gap list is worth a human glance, not an alarm. Fast mode (the cold
    install runs in CI's cli selftest already). This section NEVER emits
    findings; a broken gate shows up in the suites section instead."""
    import protocol.release_readiness as release_readiness
    details = []
    try:
        report = release_readiness.run_gate(fast=True, echo=lambda *a: None)
    except Exception as exc:  # noqa: BLE001 — informational section, never a crash
        return _section("release", [],
                        [f"gate could not run here ({exc}) — see the suites "
                         "section for the gate's own selftest"])
    details.append(f"INFORMATIONAL — verdict {report['verdict']}: "
                   f"{report['note']}")
    for c in report["criteria"]:
        if c["status"] == "GAP":
            details.append(f"  gap: {c['name']} — closes with: "
                           f"{c['closes_with']}")
    return _section("release", [], details)


# ----------------------------------------------------------------------------
# 9: docs — the verified documentation suite (the doc contract, enforced)
# ----------------------------------------------------------------------------
def section_docs(base_dir: str = _REPO_ROOT) -> dict:
    """Run doc_verify --check: every doc command executed in a fresh-clone
    sandbox, every stated number recomputed from live state, every idx
    reference resolved. Stale docs are FINDINGS (the deliberate --render +
    commit step is how they heal); an absent docs/ is a NAMED skip."""
    docs_dir = os.path.join(base_dir, "docs")
    tool = os.path.join(base_dir, "protocol", "doc_verify.py")
    if not os.path.isdir(docs_dir) or not os.path.exists(tool):
        return _section("docs", [], ["no docs/ suite on this checkout — "
                                     "SKIPPED (named)"], skipped=True)
    findings, details = [], []
    try:
        r = _run([sys.executable, tool, "--check"], base_dir, 1800)
    except (OSError, subprocess.SubprocessError) as exc:
        return _section("docs", [f"doc_verify could not run: {exc}"], [])
    tail = [ln for ln in r.stdout.splitlines() if ln.strip()]
    if r.returncode == 0:
        details.append(next((ln for ln in tail if ln.startswith("DOC-VERIFY")),
                            "DOC-VERIFY: CLEAN"))
        details.append(next((ln for ln in tail if ln.startswith("docs checked")),
                            ""))
    else:
        findings.extend(ln.strip() for ln in tail
                        if ln.strip().startswith("- ")
                        or ln.startswith("DOC-VERIFY"))
        if not findings:
            findings.append(f"doc_verify --check exited {r.returncode}")
    return _section("docs", findings, details)


# ----------------------------------------------------------------------------
# Assembly + verdict
# ----------------------------------------------------------------------------
def assemble_report(sections: list) -> dict:
    finding_count = sum(len(s["findings"]) for s in sections)
    return {
        "schema": SWEEP_SCHEMA,
        "run_date": time.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "timestamp_note": ("the run date appears because this is an "
                           "OPERATIONAL REPORT, not a protocol object — "
                           "nothing here is hashed or anchored; the "
                           "no-timestamps rule binds hashed artifacts"),
        "sections": sections,
        "finding_count": finding_count,
        "verdict": ("SWEEP CLEAN" if finding_count == 0
                    else f"SWEEP FINDINGS ({finding_count})"),
        "zero_value": True,
        "no_token": True,
    }


def run_sweep(base_dir: str = _REPO_ROOT) -> dict:
    return assemble_report([
        section_layers(base_dir),
        section_suites(base_dir),
        section_kit(base_dir),
        section_mirrors(base_dir),
        section_identity(base_dir),
        section_evidence(base_dir),
        section_git(base_dir),
        section_drift(base_dir),
        section_mip(base_dir),
        section_release(base_dir),
        section_private_repo(),
        section_docs(base_dir),
    ])


def print_report(report: dict):
    print("=== coordinator routine sweep v1 (research-stage, zero-value, "
          "no token) ===")
    print(f"run date: {report['run_date']}  "
          "(report, not protocol object — nothing here is hashed)")
    for s in report["sections"]:
        print(f"\n[{s['section']}] {s['status'].upper()}")
        for d in s["details"]:
            print(f"  {d}")
        for f in s["findings"]:
            print(f"  FINDING: {f}")
    print(f"\nVERDICT: {report['verdict']}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="routine_sweep.py",
        description=(
            "MetaCoin coordinator routine sweep v1 (research-stage, "
            "ZERO-VALUE, no token): the standing pre-session/weekly health "
            "command. Read-only — ZERO ledger writes. With no mode, runs "
            "the sweep."
        ),
        epilog=(
            "Verdict: SWEEP CLEAN (exit 0) or SWEEP FINDINGS (n) (exit 1). "
            "Expected evolution (generation drift, path growth, current-chain "
            "ACI movement) is reported under its own heading and is never a "
            "finding — the sweep exists to notice UNEXPECTED change."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--json", action="store_true",
                        help="emit the full JSON report instead of text")
    parser.add_argument("--selftest", action="store_true",
                        help="run the mechanical self-test (fixtures + temp "
                             "files only; the real ledger is asserted "
                             "byte-identical before/after)")
    args = parser.parse_args(argv)

    if args.selftest:
        return _selftest()

    report = run_sweep()
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print_report(report)
    return 0 if report["finding_count"] == 0 else 1


# ============================== SELF-TEST ====================================
def _selftest() -> int:
    """Planted findings must surface in every fixture category (tampered
    evidence, orphan, missing, dirty tree, kit mismatch); clean fixtures
    yield SWEEP CLEAN; drift lines are never findings. Temp files only; the
    REAL ledger is asserted byte-identical before/after."""
    import tempfile

    print("=== protocol/routine_sweep.py self-test (fixtures; read-only) ===")
    print("Planted findings must surface; clean fixtures stay CLEAN; drift "
          "is never a finding.\n")

    checks = []
    root_before = set(os.listdir(_REPO_ROOT))
    real_ledger = os.path.join(_PROTO_DIR, "ledger_data.jsonl")
    ledger_sha_before = None
    if os.path.exists(real_ledger):
        with open(real_ledger, "rb") as f:
            ledger_sha_before = _sha256_hex(f.read())

    real_evidence = os.path.join(_PROTO_DIR, "evidence")
    entries = actor_identity._read_entries(
        actor_identity._default_ledger_source(_REPO_ROOT))

    tmp = tempfile.mkdtemp(prefix=f"routine_sweep_selftest_{os.getpid()}_")
    try:
        # [1] the REAL evidence bundle reconciles clean (every file cited,
        # every ledger-derived expectation present) — this is the baseline
        # the sweep will run against
        f_real, d_real = reconcile_evidence(real_evidence, entries)
        checks.append(("real evidence bundle reconciles clean (no orphans, "
                       "no missing, self-hashes recompute)", not f_real))
        for f in f_real:
            print(f"    unexpected: {f}")

        # [2] planted ORPHAN on a fixture copy -> named finding
        fx_ev = os.path.join(tmp, "evidence")
        shutil.copytree(real_evidence, fx_ev)
        with open(os.path.join(fx_ev, "planted_orphan.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"planted": True}, f)
        f_orphan, _ = reconcile_evidence(fx_ev, entries)
        checks.append(("planted orphan evidence file surfaces by name",
                       any("ORPHAN: planted_orphan.json" in x
                           for x in f_orphan)))
        os.remove(os.path.join(fx_ev, "planted_orphan.json"))

        # [3] TAMPERED self-hash file -> named TAMPER finding (the interior
        # citations still match, so only the self-hash recompute catches it)
        aci_path = os.path.join(fx_ev, "aci_report.json")
        with open(aci_path, "r", encoding="utf-8") as f:
            doc = json.load(f)
        doc["path_count"] = doc["path_count"] + 1  # tamper; keep report_hash
        with open(aci_path, "w", encoding="utf-8") as f:
            json.dump(doc, f)
        f_tamper, _ = reconcile_evidence(fx_ev, entries)
        checks.append(("tampered evidence file surfaces (self-hash does not "
                       "recompute)",
                       any("TAMPERED: aci_report.json" in x
                           for x in f_tamper)))
        shutil.copyfile(os.path.join(real_evidence, "aci_report.json"),
                        aci_path)

        # [4] REMOVED derivably-expected file -> named MISSING finding
        removed = "challenge_c88ff2c3da86.json"
        os.remove(os.path.join(fx_ev, removed))
        f_missing, _ = reconcile_evidence(fx_ev, entries)
        checks.append(("removed expected evidence file surfaces as MISSING "
                       "with its ledger citation",
                       any(x.startswith(f"MISSING: {removed}")
                           and "ledger:" in x for x in f_missing)))

        # [5] git hygiene fixture: clean repo -> no findings; dirty tree and
        # an untracked file -> named findings
        fx_git = os.path.join(tmp, "gitfx")
        os.makedirs(fx_git)
        env_git = ["git", "-c", "user.email=sweep@fixture",
                   "-c", "user.name=sweep-fixture"]
        subprocess.run(["git", "init", "-q", fx_git], capture_output=True,
                       timeout=30)
        with open(os.path.join(fx_git, "a.txt"), "w", encoding="utf-8") as f:
            f.write("clean\n")
        subprocess.run(["git", "add", "a.txt"], cwd=fx_git,
                       capture_output=True, timeout=30)
        subprocess.run(env_git + ["commit", "-q", "-m", "fixture"],
                       cwd=fx_git, capture_output=True, timeout=30)
        s_clean = section_git(fx_git)
        with open(os.path.join(fx_git, "a.txt"), "a", encoding="utf-8") as f:
            f.write("dirty\n")
        with open(os.path.join(fx_git, "untracked.txt"), "w",
                  encoding="utf-8") as f:
            f.write("x\n")
        s_dirty = section_git(fx_git)
        checks.append(("clean fixture repo: git section passes (origin "
                       "comparison SKIPPED, named)",
                       not s_clean["findings"]
                       and any("SKIPPED" in d for d in s_clean["details"])))
        checks.append(("dirty-tree simulation surfaces modification AND "
                       "untracked file as findings",
                       any("uncommitted change" in x
                           for x in s_dirty["findings"])
                       and any("untracked file" in x
                               for x in s_dirty["findings"])))

        # [6] kit fixture: exported kit verifies clean; a tampered kit file
        # is a named finding
        fx_repo = os.path.join(tmp, "kitfx")
        os.makedirs(fx_repo)
        with open(os.path.join(fx_repo, "keychain.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"fixture": True, "merkle_root": "0" * 64,
                       "used_indices": []}, f)
        with open(os.path.join(fx_repo, "treasury_state.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"fixture": True}, f)
        fx_kit = os.path.join(tmp, "kit")
        continuity.export_kit(fx_kit, base_dir=fx_repo)
        s_kit = section_kit(fx_repo, kit_dir=fx_kit)
        with open(os.path.join(fx_kit, "treasury_state.json"), "a",
                  encoding="utf-8") as f:
            f.write("\n")
        s_kit_bad = section_kit(fx_repo, kit_dir=fx_kit)
        s_kit_none = section_kit(fx_repo, kit_dir=os.path.join(tmp, "nokit"))
        checks.append(("kit fixture: clean kit passes; tampered kit file is "
                       "a named finding; absent kit is a NAMED skip",
                       not s_kit["findings"]
                       and any("treasury_state.json" in x and "mismatch" in x
                               for x in s_kit_bad["findings"])
                       and s_kit_none["status"] == "skip"
                       and any("SKIPPED" in d
                               for d in s_kit_none["details"])))

        # [7] assembly: all-clean sections -> SWEEP CLEAN; planted findings
        # -> SWEEP FINDINGS (n) and the count is exact
        clean = assemble_report([_section("a", [], ["ok"]),
                                 _section("b", [], [], skipped=True)])
        found = assemble_report([_section("a", ["x"], []),
                                 _section("b", ["y", "z"], [])])
        checks.append(("clean fixtures yield SWEEP CLEAN; planted findings "
                       "yield SWEEP FINDINGS (n) with the exact count",
                       clean["verdict"] == "SWEEP CLEAN"
                       and clean["finding_count"] == 0
                       and found["verdict"] == "SWEEP FINDINGS (3)"
                       and found["finding_count"] == 3))
        checks.append(("the report states the report-not-protocol-object "
                       "timestamp distinction",
                       "not a protocol object" in clean["timestamp_note"]))

        # [8] drift is typed EXPECTED EVOLUTION and can never flip the
        # verdict: the real drift section emits details only, no findings
        s_drift = section_drift(_REPO_ROOT)
        checks.append(("drift section: EXPECTED EVOLUTION details only — "
                       "never findings, never failure",
                       s_drift["findings"] == []
                       and s_drift["status"] == "pass"
                       and all("EXPECTED EVOLUTION" in d
                               for d in s_drift["details"])))
        checks.append(("drift section carries the typed sampled ACI_k rows "
                       "with intervals",
                       any("sampled" in d and "95% CI" in d
                           for d in s_drift["details"])))

        # [8e] PRIVATE-REPO HYGIENE fixtures: a planted keychain is a named
        # finding; a clean tree passes; an absent clone is a NAMED skip
        fx_priv = os.path.join(tmp, "private_repo")
        os.makedirs(os.path.join(fx_priv, "funding"))
        with open(os.path.join(fx_priv, "funding", "notes.md"), "w") as f:
            f.write("clean\n")
        s_clean_priv = section_private_repo(fx_priv)
        with open(os.path.join(fx_priv, "keychain_oops.json"), "w") as f:
            f.write("{}")
        s_dirty_priv = section_private_repo(fx_priv)
        s_absent_priv = section_private_repo(os.path.join(tmp, "nope"))
        checks.append(("private-repo hygiene: clean passes, planted keychain "
                       "is a named finding, absent clone is a NAMED skip",
                       s_clean_priv["findings"] == []
                       and s_clean_priv["status"] == "pass"
                       and any("keychain_oops.json" in x
                               for x in s_dirty_priv["findings"])
                       and s_absent_priv["status"] == "skip"))

        # [8d] the release-readiness section is INFORMATIONAL by definition:
        # a NOT-READY verdict (today's expected state, MIP-0005) emits
        # details only — NEVER findings, never a sweep failure
        s_rel = section_release(_REPO_ROOT)
        checks.append(("release section: NOT-READY is informational — "
                       "never findings, never failure",
                       s_rel["findings"] == []
                       and s_rel["status"] == "pass"
                       and any("INFORMATIONAL" in d and "NOT-READY" in d
                               for d in s_rel["details"])
                       and any("closes with" in d
                               for d in s_rel["details"])))

        # [8b] PUBLIC-CORPUS MODE: on a machine with no kit, no mirrors, and
        # no local keychains (CI, fresh clones), the coordinator-machine
        # sections all take NAMED skips and the sweep still reaches a
        # verdict from public data alone
        fx_pub = os.path.join(tmp, "public")
        os.makedirs(fx_pub)
        s_id_pub = section_identity(fx_pub)
        pub_report = assemble_report([
            section_kit(fx_pub, kit_dir=os.path.join(fx_pub, "nokit")),
            section_mirrors(fx_pub), s_id_pub])
        checks.append(("public-corpus mode: kit/mirror/identity all NAMED "
                       "skips; the sweep still reaches a verdict",
                       s_id_pub["status"] == "skip"
                       and any("SKIPPED" in d for d in s_id_pub["details"])
                       and all(s["status"] == "skip"
                               for s in pub_report["sections"])
                       and pub_report["verdict"] == "SWEEP CLEAN"))

        # [9] suite-summary parser on canned runner output
        checks.append(("suite summary parser reads the runners' counts",
                       parse_suite_summary("  16/16 passed, 0 failed\n")
                       == (16, 16, 0)
                       and parse_suite_summary("  15/17 passed, 2 failed")
                       == (15, 17, 2)
                       and parse_suite_summary("nothing here") is None))

        # [10] docs section: a checkout with no docs/ suite -> NAMED skip
        # (doc_verify's own self-test covers the finding paths; the sweep
        # only needs to prove it never invents a verdict where no docs exist)
        fx_nodocs = os.path.join(tmp, "nodocs")
        os.makedirs(fx_nodocs)
        s_nodocs = section_docs(fx_nodocs)
        checks.append(("docs section on a docs-less checkout is a NAMED skip",
                       s_nodocs["status"] == "skip"
                       and not s_nodocs["findings"]
                       and any("SKIPPED (named)" in d
                               for d in s_nodocs["details"])))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if ledger_sha_before is not None:
        with open(real_ledger, "rb") as f:
            checks.append(("REAL ledger byte-identical before/after (ZERO "
                           "ledger writes — the sweep only reads)",
                           _sha256_hex(f.read()) == ledger_sha_before))
    else:
        print("    (no real ledger on this machine — the byte-identical "
              "assertion leg is SKIPPED, named)")
    stray = sorted(set(os.listdir(_REPO_ROOT)) - root_before)
    checks.append(("no stray files in repo root", not stray))
    if stray:
        print(f"    stray: {stray}")

    print("--- self-test invariants ---")
    failures = 0
    for name, passed in checks:
        print(f"{name:68s}: {'PASS' if passed else 'FAIL'}")
        if not passed:
            failures += 1
    ok = failures == 0
    print("\n=== self-test summary: " +
          ("ALL CHECKS BEHAVED CORRECTLY" if ok else "FAILURE — see above")
          + " ===")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
