# Copyright (c) 2023-2026 MetaCoin-Lab.
# Licensed under SML-1.0 — see LICENSE.md.
"""continuity.py — coordinator CONTINUITY & MIRROR-READINESS v0 (operational tooling).

================== HONESTY / SCOPE (READ ME) ==================
Research-stage, ZERO-VALUE, no token. Standard library only.

OPERATIONAL TOOLING, NOT PROTOCOL EVENTS: this module performs ZERO ledger
writes — running every mode leaves protocol/ledger_data.jsonl byte-identical
(the self-test asserts it by hash). Backups, restore rehearsals, and mirror
checks are facts about the COORDINATOR'S MACHINE, not about the work; not
everything deserves an anchor.

THE REAL SINGLE POINT OF FAILURE this module exists to eliminate: the live
ledger and every Lamport private keychain exist on ONE machine. Hash-based
one-time signatures have no recovery phrase, no re-derivation, no reset — a
lost keychain is IDENTITY DEATH for its actor: history stays verifiable
forever, but nothing can ever sign as that actor again.

TWO DIFFERENT SURVIVALS, and they are not interchangeable:
  * The PUBLIC corpus (published snapshot + committed anchor + evidence
    bundle) preserves VERIFIABILITY forever: any machine, any time, can
    re-verify every anchored claim from a clone.
  * The PRIVATE kit (keychains + live ledger + working state) preserves
    OPERATORSHIP: only a machine holding it can continue signing and
    anchoring as the existing actors.
From public data alone, a new machine can verify everything but can never
sign as any existing actor — this boundary is PROVEN by the public-only
restore rehearsal (verification passes, every signing attempt fails with the
honest reason), not just stated.

CRITICALITY IS CLASSIFIED HONESTLY PER FILE:
  * identity-fatal                      — private keychains: unrecoverable if
                                          lost (no copy anywhere else, by
                                          design — they must never enter the
                                          repo or any published artifact)
  * ledger-critical-snapshot-recoverable — the live ledger: losing the file
                                          loses no CONTENT (the published
                                          snapshot holds every entry and the
                                          write rehearsal proves reconstruction)
  * regenerable                          — working state replayable from
                                          anchored records (treasury/economy)
  * unregistered-flagged                 — anything stateful-LOOKING that the
                                          registry does not know: flagged
                                          loudly, included in the kit, never
                                          silently ignored

PRIVATE MATERIAL NEVER ENTERS the repo, the report, or any committed path:
the manifest lists paths + sha256s + criticality classes, never contents; kit
export refuses any git-tracked destination; every kit operation warns loudly.

IDENTITY-SURVIVABILITY RESERVES (actor_identity.py --stage-reserve) live in
the kit as reserve_<actor>/ directories: a successor PRIVATE keychain plus a
pre-signed rotation certificate, anchored NEVER until a real emergency or
planned rotation. --verify-kit re-validates every reserve against the
CURRENT chain state: a reserve that would anchor today passes; a stale one
(root since rotated) is FLAGGED retired, never silently passed; a tampered
or unanchorable one — or a kit keychain copy that fails to mark the
reserve's spent signing index — is a NAMED failure. Reserves raise
AVAILABILITY, not confidentiality: a stolen kit yields both chains.

MIRROR-READINESS (the second device's standing job, once it arrives):
periodically pull the repo, run --export-mirror into its own storage, and run
--check-mirror against what it holds. IDENTICAL is quiet health; BEHIND is a
verified prefix awaiting refresh; DIVERGED is THE ALARM — a mirror that
verifies internally but is not a prefix of the current chain is evidence of
rewriting somewhere (on either side), and the check names the first divergent
index and exits loudly. Divergence alarms are the point.

Usage:
    python3 protocol/continuity.py --inventory
    python3 protocol/continuity.py --export-kit continuity_kit
    python3 protocol/continuity.py --verify-kit continuity_kit
    python3 protocol/continuity.py --restore-rehearsal continuity_kit
    python3 protocol/continuity.py --boundary-rehearsal   # public-only proof
    python3 protocol/continuity.py --export-mirror mirror_export
    python3 protocol/continuity.py --check-mirror mirror_export
    python3 protocol/continuity.py --selftest             # temp-only
"""

# Suppress __pycache__/*.pyc so importing protocol modules leaves no stray files.
import sys
sys.dont_write_bytecode = True

import argparse
import glob
import hashlib
import json
import os
import shutil
import subprocess
import time

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

_PROTO_DIR = os.path.dirname(os.path.abspath(__file__))

import protocol.actor_identity as actor_identity
import protocol.audit as audit
import protocol.ledger as ledger_mod
import protocol.work_molecule as work_molecule
import demo.economy_demo as economy_demo
import demo.metastar_treasury as metastar_treasury
# the mirror BEHIND verdict REUSES the intake rung-4 prefix logic — a mirror is
# healthy exactly when it is a verified prefix point of the current chain
from protocol.external_verifier import _chain_prefix_point

MANIFEST_SCHEMA = "recovery-manifest/0.1"
MIRROR_MANIFEST_SCHEMA = "mirror-manifest/0.1"
MANIFEST_NAME = "recovery_manifest.json"
MIRROR_MANIFEST_NAME = "mirror_manifest.json"

# Criticality classes (see the module docstring for what each honestly means).
IDENTITY_FATAL = "identity-fatal"
LEDGER_RECOVERABLE = "ledger-critical-snapshot-recoverable"
REGENERABLE = "regenerable"
UNREGISTERED = "unregistered-flagged"

# The KNOWN-STATE REGISTRY: every coordinator-private/critical file this
# project has created, with its honest criticality. Absent entries are skipped
# (a fresh clone has none of these); anything stateful-LOOKING but not listed
# here is FLAGGED by the pattern scan below, never silently ignored.
KNOWN_STATE = (
    {"path": "keychain.json", "criticality": IDENTITY_FATAL,
     "note": "spark-agent-same-operator root A (retired by the idx-41 rotation "
             "— retired roots sign nothing new, but the file is still private)"},
    {"path": "keychain_gen2.json", "criticality": IDENTITY_FATAL,
     "note": "spark-agent-same-operator ACTIVE root B"},
    {"path": "keychain_uptime.json", "criticality": IDENTITY_FATAL,
     "note": "spark-uptime-node active root (Flow-1 heartbeat signing)"},
    {"path": "keychain_participant.json", "criticality": IDENTITY_FATAL,
     "note": "rehearsal-participant-01 active root (participant intake "
             "rehearsal identity)"},
    {"path": "protocol/attest_key.secret", "criticality": IDENTITY_FATAL,
     "note": "R2 software attestation MAC key (if present)"},
    {"path": "protocol/ledger_data.jsonl", "criticality": LEDGER_RECOVERABLE,
     "note": "the live append-only chain; every entry also lives in the "
             "published snapshot (reconstruction proven by the write "
             "rehearsal)"},
    {"path": "treasury_state.json", "criticality": REGENERABLE,
     "note": "re-derives from the anchored economy + Gate-3 records"},
    {"path": "economy_state.json", "criticality": REGENERABLE,
     "note": "deterministic 30-simulated-day replay reproduces it"},
    {"path": "economy_log.json", "criticality": REGENERABLE,
     "note": "deterministic replay reproduces it (hash anchored at idx 19)"},
)

# What counts as stateful-LOOKING for the discovery scan (repo root and
# protocol/, top level): private keychains, mutable state files, secret keys,
# and any live ledger file. Matches are classified by the registry; anything
# unmatched-by-registry is flagged.
_STATE_PATTERNS = ("keychain*.json", "*_state.json", "*.secret",
                   "*ledger_data*.jsonl")

KIT_WARNING = (
    "THIS KIT CONTAINS PRIVATE KEY MATERIAL — offline storage only. Anyone "
    "holding it can sign as every actor above; without at least one copy OFF "
    "this machine, losing the machine is identity death for every actor. "
    "Reserve directories add SUCCESSOR keychains: a stolen kit yields BOTH "
    "the active and successor chains — reserves raise availability, not "
    "confidentiality. Never commit it, never publish it, never transmit it "
    "over anything you do not trust."
)

BOUNDARY_REASON = ("no private material: verifiability survives, "
                   "operatorship does not")

DEFAULT_ACTOR = "spark-agent-same-operator"
_PROBE_MESSAGE = b"continuity restore-rehearsal signing probe (temp-only)"


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _write_json(path: str, obj: dict):
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps(obj, indent=2, sort_keys=True) + "\n")


def _load_json(path: str):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ----------------------------------------------------------------------------
# Inventory: registry + pattern discovery -> recovery manifest (paths+hashes,
# NEVER contents)
# ----------------------------------------------------------------------------
def build_inventory(base_dir: str = _REPO_ROOT) -> dict:
    """Enumerate all coordinator-private/critical state under `base_dir`.

    Registry entries that exist are classified per the registry; pattern
    matches the registry does not know are FLAGGED (class unregistered-flagged)
    rather than silently ignored. The manifest lists path + sha256 + size +
    criticality — never file contents."""
    files = []
    seen = set()
    for entry in KNOWN_STATE:
        p = os.path.join(base_dir, entry["path"])
        if not os.path.exists(p):
            continue
        files.append({"path": entry["path"], "sha256": _sha256_file(p),
                      "size": os.path.getsize(p),
                      "criticality": entry["criticality"],
                      "note": entry["note"]})
        seen.add(entry["path"])

    flagged = []
    for scan_dir, prefix in ((base_dir, ""), (os.path.join(base_dir, "protocol"),
                                              "protocol/")):
        if not os.path.isdir(scan_dir):
            continue
        for pattern in _STATE_PATTERNS:
            for p in sorted(glob.glob(os.path.join(scan_dir, pattern))):
                rel = prefix + os.path.basename(p)
                if rel in seen or not os.path.isfile(p):
                    continue
                seen.add(rel)
                row = {"path": rel, "sha256": _sha256_file(p),
                       "size": os.path.getsize(p),
                       "criticality": UNREGISTERED,
                       "note": "stateful-looking but NOT in the known-state "
                               "registry — flagged, included in the kit; "
                               "register or retire it deliberately"}
                files.append(row)
                flagged.append(rel)

    files.sort(key=lambda f: f["path"])
    summary = {}
    for f in files:
        summary[f["criticality"]] = summary.get(f["criticality"], 0) + 1

    tip = None
    for src in (os.path.join(base_dir, "protocol", "ledger_data.jsonl"),
                os.path.join(base_dir, "protocol", "ledger_published.json")):
        if os.path.exists(src):
            entries = work_molecule._read_ledger(src)
            if entries:
                tip = {"tip_index": entries[-1].get("index"),
                       "tip_hash": entries[-1].get("hash")}
            break

    return {
        "schema": MANIFEST_SCHEMA,
        "generated_at": time.time(),
        "generated_against": tip,
        "files": files,
        "summary": summary,
        "flagged": flagged,
        "kit_warning": KIT_WARNING,
        "restore_instructions": (
            "on the replacement machine: clone the public repo (that restores "
            "VERIFIABILITY), copy every kit file to its listed path (that "
            "restores OPERATORSHIP), then run "
            "python3 protocol/continuity.py --restore-rehearsal <kit-dir> "
            "before trusting the restore"),
        "zero_value": True,
        "no_token": True,
    }


# ----------------------------------------------------------------------------
# Kit export / verify (refuses tracked destinations; warns loudly)
# ----------------------------------------------------------------------------
def _export_destination_refusal(dest: str, base_dir: str = _REPO_ROOT):
    """Reason the destination is unsafe for private material, or None.

    Outside the repo is fine; inside the repo the path must be git-ignored —
    a tracked (or trackable) destination risks committing private keys."""
    dest_abs = os.path.abspath(dest)
    base_abs = os.path.abspath(base_dir)
    if not (dest_abs == base_abs
            or dest_abs.startswith(base_abs + os.sep)):
        return None  # outside the repo entirely
    rel = os.path.relpath(dest_abs, base_abs)
    try:
        # probe the DIRECTORY form ('dir/'): a directory-only .gitignore
        # pattern like 'continuity_kit/' matches only that form until the
        # directory exists, and plain patterns match it too
        probe = subprocess.run(["git", "check-ignore", "-q",
                                rel.rstrip("/") + "/"],
                               cwd=base_abs, capture_output=True, timeout=10)
        if probe.returncode == 0:
            return None  # inside the repo but git-ignored: safe
        return (f"destination {dest!r} is INSIDE the repository and NOT "
                "git-ignored — refusing to export private key material into "
                "a trackable path (use a directory outside the repo, or a "
                "gitignored one like continuity_kit/)")
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        return (f"destination {dest!r} is inside the repository and git is "
                "unavailable to prove it is ignored — refusing (use a "
                "directory outside the repo)")


def export_kit(kit_dir: str, base_dir: str = _REPO_ROOT) -> dict:
    """Copy the full critical set + manifest into `kit_dir`. Refuses tracked
    destinations. Returns the manifest."""
    reason = _export_destination_refusal(kit_dir, base_dir)
    if reason:
        raise ValueError(reason)
    manifest = build_inventory(base_dir)
    if not manifest["files"]:
        raise ValueError("nothing to export: no known or discovered "
                         "coordinator state under " + base_dir)
    os.makedirs(kit_dir, exist_ok=True)
    for f in manifest["files"]:
        src = os.path.join(base_dir, f["path"])
        dst = os.path.join(kit_dir, f["path"])
        os.makedirs(os.path.dirname(dst) or kit_dir, exist_ok=True)
        shutil.copyfile(src, dst)
    _write_json(os.path.join(kit_dir, MANIFEST_NAME), manifest)
    return manifest


def verify_kit(kit_dir: str, base_dir: str = _REPO_ROOT) -> tuple:
    """Hash-check every kit file against its manifest, then re-validate every
    PRE-STAGED ROTATION RESERVE (reserve_<actor>/ directories) against the
    CURRENT chain state. Returns (ok, results):
    results = [{path, status, detail}], where status is ok|missing|mismatch
    for manifest files and reserve-ok|reserve-retired|reserve-invalid|
    reserve-kit-stale for reserves. A stale reserve (root since rotated) is
    FLAGGED retired, never silently passed — but is not a kit failure; a
    tampered/unanchorable reserve, or a kit keychain copy that fails to mark
    a staged reserve's spent signing index, IS a failure."""
    manifest = _load_json(os.path.join(kit_dir, MANIFEST_NAME))
    results = []
    ok = True
    for f in manifest.get("files", []):
        p = os.path.join(kit_dir, f["path"])
        if not os.path.exists(p):
            results.append({"path": f["path"], "status": "missing",
                            "detail": "file listed in the manifest is absent "
                                      "from the kit"})
            ok = False
            continue
        actual = _sha256_file(p)
        if actual != f["sha256"]:
            results.append({"path": f["path"], "status": "mismatch",
                            "detail": f"sha256 {actual[:16]}.. does not match "
                                      f"the manifest {f['sha256'][:16]}.. — "
                                      "the kit copy was altered or corrupted"})
            ok = False
        else:
            results.append({"path": f["path"], "status": "ok",
                            "detail": f"sha256 matches ({f['criticality']})"})

    reserves = actor_identity.scan_reserves(kit_dir)
    if reserves:
        entries = actor_identity._read_entries(
            actor_identity._default_ledger_source(base_dir))
        for res in reserves:
            st = actor_identity.reserve_status(res, entries)
            path = res["dir"] + "/"
            if st["state"] == "staged":
                # kit keychain freshness: the ACTIVE keychain copy in the kit
                # must already mark the reserve's spent signing index —
                # restoring a stale copy would risk one-time index reuse
                cert = res["certificate"]
                stale_copy = None
                for name in sorted(os.listdir(kit_dir)):
                    if not (name.startswith("keychain")
                            and name.endswith(".json")):
                        continue
                    try:
                        kc = _load_json(os.path.join(kit_dir, name))
                    except (json.JSONDecodeError, OSError):
                        continue
                    if (isinstance(kc, dict)
                            and kc.get("merkle_root") == cert["prev_root"]):
                        if cert["key_index"] not in kc.get("used_indices",
                                                           []):
                            stale_copy = name
                        break
                if stale_copy:
                    results.append({
                        "path": path, "status": "reserve-kit-stale",
                        "detail": f"kit keychain copy {stale_copy} does NOT "
                                  f"mark the reserve's spent signing index "
                                  f"{cert['key_index']} — restoring that "
                                  "copy would risk one-time index reuse; "
                                  "re-export the kit"})
                    ok = False
                else:
                    results.append({"path": path, "status": "reserve-ok",
                                    "detail": st["detail"]})
            elif st["state"] in ("retired-stale", "retired-unanchored",
                                 "anchored"):
                results.append({
                    "path": path, "status": "reserve-retired",
                    "detail": st["detail"] + " (FLAGGED retired — not a kit "
                              "failure, but this reserve provides NO "
                              "survivability; re-stage)"})
            else:
                results.append({"path": path, "status": "reserve-invalid",
                                "detail": st["detail"]})
                ok = False
    return (ok, results)


# ----------------------------------------------------------------------------
# Restore rehearsal: assemble a temp coordinator home and prove capabilities
# ----------------------------------------------------------------------------
def restore_home(dest: str, kit_dir: str = None, base_dir: str = _REPO_ROOT):
    """Assemble a restored coordinator home at `dest`: the PUBLIC corpus (the
    repo's git-tracked files — exactly what a fresh clone has), overlaid with
    the PRIVATE kit when one is given. Returns the list of kit-restored paths
    ([] for a public-only restore)."""
    ls = subprocess.run(["git", "ls-files", "-z"], cwd=base_dir,
                        capture_output=True, timeout=60)
    if ls.returncode != 0:
        raise ValueError("not a git checkout — cannot assemble the public "
                         "corpus for a restore rehearsal")
    for rel in ls.stdout.decode("utf-8").split("\0"):
        if not rel:
            continue
        src = os.path.join(base_dir, rel)
        if not os.path.exists(src):
            continue  # tracked but deleted locally
        dst = os.path.join(dest, rel)
        os.makedirs(os.path.dirname(dst) or dest, exist_ok=True)
        shutil.copyfile(src, dst)
    restored = []
    if kit_dir is not None:
        manifest = _load_json(os.path.join(kit_dir, MANIFEST_NAME))
        for f in manifest.get("files", []):
            src = os.path.join(kit_dir, f["path"])
            dst = os.path.join(dest, f["path"])
            os.makedirs(os.path.dirname(dst) or dest, exist_ok=True)
            shutil.copyfile(src, dst)
            restored.append(f["path"])
    return restored


def _home_ledger_source(home: str) -> str:
    live = os.path.join(home, "protocol", "ledger_data.jsonl")
    return live if os.path.exists(live) else os.path.join(
        home, "protocol", "ledger_published.json")


def capability_verify(home: str) -> tuple:
    """(a) The restored coordinator re-verifies EVERYTHING: run the home's own
    verify_everything --full as a subprocess (so path resolution is the
    restored home's, never this checkout's)."""
    r = subprocess.run(
        [sys.executable, os.path.join(home, "protocol", "verify_everything.py"),
         "--full"],
        cwd=home, capture_output=True, text=True, timeout=900)
    ok = r.returncode == 0 and "ALL LAYERS PASS" in r.stdout
    tail = next((ln for ln in r.stdout.splitlines() if ln.startswith("RESULT")),
                "no RESULT line")
    return (ok, f"verify_everything --full in the restored home: {tail}")


def capability_sign(home: str, actor: str = DEFAULT_ACTOR) -> tuple:
    """(b) The restored coordinator can SIGN as the existing actor: a restored
    keychain must hold the actor's CURRENT ACTIVE root; a probe signature is
    produced (marking only the restored COPY) and verified. Fails with the
    honest boundary reason when no private material was restored."""
    entries = actor_identity._read_entries(_home_ledger_source(home))
    active = actor_identity.active_root_asof(actor, entries)
    if active is None:
        return (False, f"actor {actor!r} has no anchored root chain in the "
                       "restored corpus")
    keychain = None
    for p in sorted(glob.glob(os.path.join(home, "keychain*.json"))):
        try:
            kc = _load_json(p)
        except (json.JSONDecodeError, OSError):
            continue
        if (isinstance(kc, dict)
                and kc.get("merkle_root") == active["merkle_root"]):
            keychain = kc
            break
    if keychain is None:
        return (False, f"{BOUNDARY_REASON} (no restored keychain holds "
                       f"{actor!r}'s active root "
                       f"{str(active['merkle_root'])[:16]}.. — from public "
                       "data a machine can verify everything but can never "
                       "sign as any existing actor)")
    key_index = actor_identity.first_unused_index(keychain, entries)
    signature = actor_identity.sign(keychain, key_index, _PROBE_MESSAGE,
                                    ledger_source=entries)
    ok, reasons = actor_identity.verify_signature(
        signature, active["merkle_root"], _PROBE_MESSAGE)
    return (ok, (f"probe signed with one-time index {key_index} under the "
                 f"active root {active['merkle_root'][:16]}.. and verified "
                 "(only the restored COPY's used-index mark advanced)"
                 if ok else "probe signature failed: " + "; ".join(reasons)))


def capability_write(home: str) -> tuple:
    """(c) The restored coordinator can WRITE: append a temp-only probe entry
    to a COPY of the restored ledger and verify the copy's full chain. When no
    live ledger was restored (public-only mode), the ledger is RECONSTRUCTED
    from the published snapshot first — proving the snapshot-recoverable
    class. The real ledger is never touched."""
    live = os.path.join(home, "protocol", "ledger_data.jsonl")
    note = "restored live ledger"
    if not os.path.exists(live):
        snap = _load_json(os.path.join(home, "protocol",
                                       "ledger_published.json"))
        with open(live, "w", encoding="utf-8") as f:
            for e in snap.get("entries", []):
                f.write(ledger_mod._canonical(e) + "\n")
        note = ("ledger RECONSTRUCTED from the published snapshot "
                "(snapshot-recoverable, proven)")
    work = os.path.join(home, "continuity_write_probe.jsonl")
    shutil.copyfile(live, work)
    led = ledger_mod.Ledger(work)
    ok_before, _ = led.verify_chain()
    n_before = len(led.read_all())
    entry = led.append({
        "event": "continuity_restore_rehearsal_probe",
        "note": "temp-only write probe on a COPY of the restored ledger — "
                "never anchored, never the real chain",
        "zero_value": True, "no_token": True,
    })
    ok_after, reason = led.verify_chain()
    ok = bool(ok_before and ok_after and entry["index"] == n_before)
    return (ok, f"{note}; probe appended at copy-index {entry['index']} and "
                f"the copy re-verified ({reason})")


def capability_replay(home: str) -> tuple:
    """(d) Regenerable state actually regenerates: fees re-derive from the
    restored corpus and match the anchored treasury config; the deterministic
    economy replay reproduces the anchored log hash."""
    src = _home_ledger_source(home)
    entries = work_molecule._read_ledger(src)
    problems = []
    treasury = next((e["payload"] for e in reversed(entries)
                     if e.get("payload", {}).get("event")
                     == "treasury_config_anchored"
                     and e["payload"].get("status")
                     == "treasury-config-confirmed"), None)
    if treasury is None:
        problems.append("no anchored treasury config in the restored corpus")
    else:
        # against ITS OWN recorded root — later economy generations must
        # never disturb the anchored config's re-derivation
        root, _per_day, total = metastar_treasury.derive_fees(
            src, funding_root=treasury.get("funding_root"))
        if (total != treasury.get("total_fees_collected")
                or root != treasury.get("funding_root")):
            problems.append(f"fees {total} do not re-derive to the anchored "
                            f"{treasury.get('total_fees_collected')}")
    econs = [e["payload"] for e in entries
             if e.get("payload", {}).get("event")
             == "economy_demo_summary_anchored"
             and e["payload"].get("status") == "economy-demo-confirmed"]
    if not econs:
        problems.append("no anchored economy summary in the restored corpus")
    from protocol.verifier_cli import era2_expectation
    for econ in econs:
        # one frozen simulation per anchored generation — replay each
        # (era-aware: the current-era replay expectation may live on an
        # anchored hash-era transition record)
        gen = econ.get("generation", 1)
        log = economy_demo.simulate_all(gen)
        expected_log = (era2_expectation(entries,
                                         f"economy_gen{gen}_log_hash")
                        or econ.get("economy_log_hash"))
        if log["economy_log_hash"] != expected_log:
            problems.append(f"economy generation {econ.get('generation', 1)} "
                            "replay hash does not match the anchored summary")
    ok = not problems
    return (ok, "fees re-derive to the anchored total and every economy "
                "generation's replay reproduces its anchored log hash — the "
                "regenerable class actually regenerates"
                if ok else "; ".join(problems))


def restore_rehearsal(kit_dir: str = None, base_dir: str = _REPO_ROOT,
                      actor: str = DEFAULT_ACTOR) -> dict:
    """Assemble a restored home in a temp dir and prove the four capabilities:
    verify / sign / write / replay. `kit_dir=None` is the PUBLIC-ONLY boundary
    rehearsal, where sign is EXPECTED to fail with the honest reason. Cleans
    the temp home; returns {mode, capabilities: {name: {ok, detail}},
    restored_private_files}."""
    import tempfile
    home = tempfile.mkdtemp(prefix=f"continuity_restore_{os.getpid()}_")
    try:
        restored = restore_home(home, kit_dir, base_dir)
        caps = {}
        for name, fn in (("verify", capability_verify),
                         ("sign", lambda h: capability_sign(h, actor)),
                         ("write", capability_write),
                         ("replay", capability_replay)):
            try:
                ok, detail = fn(home)
            except Exception as exc:  # a capability crash is a failed capability
                ok, detail = False, f"{type(exc).__name__}: {exc}"
            caps[name] = {"ok": ok, "detail": detail}
        return {"mode": "kit-restore" if kit_dir else "public-only",
                "capabilities": caps,
                "restored_private_files": restored}
    finally:
        shutil.rmtree(home, ignore_errors=True)


# ----------------------------------------------------------------------------
# Mirror-readiness: export the PUBLIC mirror set; check a mirror for drift
# ----------------------------------------------------------------------------
def export_mirror(mirror_dir: str, base_dir: str = _REPO_ROOT) -> dict:
    """Copy the PUBLIC mirror set (published snapshot, committed anchor, full
    evidence bundle) into `mirror_dir` + a manifest with hashes, the chain
    point, and the one-command verification recipe. Public material only —
    but the tracked-destination refusal applies anyway (a mirror inside the
    repo would shadow the corpus)."""
    reason = _export_destination_refusal(mirror_dir, base_dir)
    if reason:
        raise ValueError(reason)
    proto = os.path.join(base_dir, "protocol")
    snapshot_path = os.path.join(proto, "ledger_published.json")
    entries = work_molecule._read_ledger(snapshot_path)
    files = []
    os.makedirs(mirror_dir, exist_ok=True)
    to_copy = [("ledger_published.json", snapshot_path),
               ("ledger_anchor.json", os.path.join(proto,
                                                   "ledger_anchor.json"))]
    evidence_dir = os.path.join(proto, "evidence")
    if os.path.isdir(evidence_dir):
        for name in sorted(os.listdir(evidence_dir)):
            p = os.path.join(evidence_dir, name)
            if os.path.isfile(p):
                to_copy.append((os.path.join("evidence", name), p))
    for rel, src in to_copy:
        dst = os.path.join(mirror_dir, rel)
        os.makedirs(os.path.dirname(dst) or mirror_dir, exist_ok=True)
        shutil.copyfile(src, dst)
        files.append({"path": rel, "sha256": _sha256_file(src)})
    manifest = {
        "schema": MIRROR_MANIFEST_SCHEMA,
        "generated_at": time.time(),
        "tip_index": entries[-1].get("index") if entries else None,
        "tip_hash": entries[-1].get("hash") if entries else None,
        "entry_count": len(entries),
        "files": files,
        "verification_recipe": (
            "python3 protocol/audit.py --verify <mirror>/ledger_published.json "
            "(standalone chain re-walk), or from any repo clone: "
            "python3 protocol/verify_everything.py --full"),
        "standing_job": (
            "second device: periodically pull, re-run --export-mirror into "
            "your own storage, and run --check-mirror against what you hold — "
            "IDENTICAL is quiet health, BEHIND means refresh, DIVERGED is the "
            "alarm"),
        "zero_value": True,
        "no_token": True,
    }
    _write_json(os.path.join(mirror_dir, MIRROR_MANIFEST_NAME), manifest)
    return manifest


MIRROR_ATTESTATION_SCHEMA = "mirror-attestation/0.1"
MIRROR_ATTESTATION_BUNDLE_SCHEMA = "mirror-attestation-bundle/0.1"
MIRROR_ATTESTATION_SCOPE = (
    "SAME-OPERATOR second-device mirror: protects against coordinator-disk "
    "loss and detects coordinator-side rewriting (a DIVERGED verdict from a "
    "machine the coordinator's workflow does not touch). It is NOT "
    "third-party archival — that remains open, named alongside the "
    "unaffiliated-participant milestone.")


def attest_mirror(mirror_dir: str, workdir: str, base_dir: str = _REPO_ROOT,
                  out_path: str = None) -> tuple:
    """Build and SIGN a mirror attestation on the mirror device itself.

    Runs check_mirror first (IDENTICAL/BEHIND required — a diverged or
    invalid mirror attests nothing), then signs the attested facts (the
    mirror's chain point, its manifest hash, the check verdict, and THIS
    machine's coarse fingerprint) with the next unused one-time key from
    the participant keychain in `workdir` — the same keychain, the same
    ledger-wide one-time discipline, and the same fingerprint source as a
    participant bundle, so the coordinator can verify the attestation with
    machinery it already trusts. Returns (bundle, bundle_sha256); writes
    the bundle to `out_path` when given. The keychain file is persisted
    with the used-index mark (stateful, like every signing path)."""
    import demo.participant_kit as participant_kit
    import protocol.verifier_cli as verifier_cli
    check = check_mirror(mirror_dir, base_dir=base_dir)
    if check["verdict"] not in ("IDENTICAL", "BEHIND"):
        raise ValueError(
            f"refusing to attest a mirror whose check verdict is "
            f"{check['verdict']!r} ({check['detail']}) — only "
            "IDENTICAL/BEHIND mirrors attest")
    manifest_path = os.path.join(mirror_dir, MIRROR_MANIFEST_NAME)
    manifest = _load_json(manifest_path)
    kc_path = os.path.join(workdir, participant_kit.KEYCHAIN_FILE)
    with open(kc_path, "r", encoding="utf-8") as f:
        keychain = json.load(f)
    snapshot = os.path.join(base_dir, "protocol", "ledger_published.json")
    ledger_source = snapshot if os.path.exists(snapshot) else None
    attestation = {
        "schema": MIRROR_ATTESTATION_SCHEMA,
        "event": "mirror_attestation",
        "actor_id": keychain["actor_id"],
        "handle": keychain["actor_id"],
        "machine_fingerprint": verifier_cli.machine_fingerprint(),
        "mirror": {
            "tip_index": manifest.get("tip_index"),
            "tip_hash": manifest.get("tip_hash"),
            "entry_count": manifest.get("entry_count"),
            "manifest_sha256": _sha256_file(manifest_path),
            "file_count": len(manifest.get("files", [])),
        },
        "check": {"verdict": check["verdict"], "detail": check["detail"]},
        "honest_scope": MIRROR_ATTESTATION_SCOPE,
        "zero_value": True,
        "no_token": True,
        "attested_at": time.time(),
    }
    key_index = actor_identity.first_unused_index(keychain, ledger_source)
    signature = actor_identity.sign(
        keychain, key_index,
        participant_kit.canonical_json(attestation).encode("utf-8"),
        ledger_source=ledger_source)
    with open(kc_path, "w", encoding="utf-8") as f:
        json.dump(keychain, f, indent=2)  # persist the used-index mark
    bundle = {
        "schema": MIRROR_ATTESTATION_BUNDLE_SCHEMA,
        "attestation": attestation,
        "signature": signature,
        "key_index": key_index,
    }
    sha = hashlib.sha256(
        participant_kit.canonical_json(bundle).encode("utf-8")).hexdigest()
    if out_path:
        _write_json(out_path, bundle)
    return bundle, sha


def check_mirror(mirror_dir: str, base_dir: str = _REPO_ROOT) -> dict:
    """Compare a mirror against the CURRENT published state. Verdicts:

      IDENTICAL — same verified tip (quiet health).
      BEHIND    — the mirror's tip is a VERIFIED PREFIX POINT of the current
                  chain (the intake rung-4 prefix rule, reused) — normal
                  between refreshes.
      DIVERGED  — the mirror verifies internally but is NOT a prefix of the
                  current chain (or is AHEAD of it): evidence of rewriting
                  somewhere, on either side. THE ALARM — named divergence
                  index, non-zero exit.

    Also hash-checks every mirror file against the mirror manifest (a tampered
    mirror copy is named per file). Returns {verdict, detail, ...}."""
    problems = []
    manifest = _load_json(os.path.join(mirror_dir, MIRROR_MANIFEST_NAME))
    for f in manifest.get("files", []):
        p = os.path.join(mirror_dir, f["path"])
        if not os.path.exists(p):
            problems.append(f"mirror file missing: {f['path']}")
        elif _sha256_file(p) != f["sha256"]:
            problems.append(f"mirror file altered: {f['path']} (sha256 does "
                            "not match the mirror manifest)")
    m_ok, m_reason, m_details = audit.verify_snapshot_file(
        os.path.join(mirror_dir, "ledger_published.json"))
    if not m_ok:
        problems.append(f"mirror snapshot does not verify: {m_reason}")
    if problems:
        return {"verdict": "INVALID", "detail": "; ".join(problems),
                "exit_code": 1}

    live = os.path.join(base_dir, "protocol", "ledger_data.jsonl")
    src = live if os.path.exists(live) else os.path.join(
        base_dir, "protocol", "ledger_published.json")
    cur = work_molecule._read_ledger(src)
    cur_tip_index = cur[-1].get("index") if cur else None
    cur_tip_hash = cur[-1].get("hash") if cur else None
    m_tip_index = m_details.get("tip_index")
    m_tip_hash = m_details.get("tip_hash")

    if m_tip_index == cur_tip_index and m_tip_hash == cur_tip_hash:
        return {"verdict": "IDENTICAL",
                "detail": f"mirror tip == current tip (index {cur_tip_index}, "
                          f"{str(cur_tip_hash)[:16]}..) — both verified",
                "exit_code": 0}
    if (isinstance(m_tip_index, int)
            and isinstance(cur_tip_index, int)
            and m_tip_index < cur_tip_index
            and _chain_prefix_point(cur, m_tip_index, m_tip_hash)):
        return {"verdict": "BEHIND",
                "detail": f"mirror tip (index {m_tip_index}) is a VERIFIED "
                          f"PREFIX POINT of the current chain (tip "
                          f"{cur_tip_index}) — {cur_tip_index - m_tip_index} "
                          "entr(ies) behind; refresh with --export-mirror",
                "exit_code": 0}

    # DIVERGED: name the first index where the histories part ways.
    mirror_entries = work_molecule._read_ledger(
        os.path.join(mirror_dir, "ledger_published.json"))
    divergence_index = None
    for i in range(min(len(mirror_entries), len(cur))):
        if mirror_entries[i].get("hash") != cur[i].get("hash"):
            divergence_index = i
            break
    if divergence_index is None:
        divergence_index = min(len(mirror_entries), len(cur))
    ahead = (isinstance(m_tip_index, int) and isinstance(cur_tip_index, int)
             and m_tip_index > cur_tip_index)
    return {"verdict": "DIVERGED",
            "detail": (f"mirror verifies internally but is NOT a prefix of "
                       f"the current chain — first divergent index "
                       f"{divergence_index}"
                       + (f"; mirror is AHEAD of the local chain (mirror tip "
                          f"{m_tip_index} > local tip {cur_tip_index}) — the "
                          "LOCAL chain lost entries" if ahead else "")
                       + ". A diverged mirror is evidence of rewriting "
                         "somewhere, on either side. Do not overwrite either "
                         "copy until the divergence is understood."),
            "divergence_index": divergence_index,
            "exit_code": 2}


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
def _print_capabilities(report: dict) -> bool:
    all_ok = True
    for name in ("verify", "sign", "write", "replay"):
        c = report["capabilities"][name]
        print(f"  [{'PASS' if c['ok'] else 'FAIL'}] {name:7s} {c['detail']}")
        all_ok = all_ok and c["ok"]
    return all_ok


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="continuity.py",
        description=(
            "MetaCoin coordinator continuity & mirror-readiness v0 "
            "(research-stage, ZERO-VALUE, no token). OPERATIONAL tooling: "
            "performs ZERO ledger writes — not everything deserves an anchor. "
            "With no mode, runs the self-test (temp files only)."
        ),
        epilog=(
            "TWO SURVIVALS: the public corpus preserves VERIFIABILITY forever; "
            "the private kit preserves OPERATORSHIP. From public data alone a "
            "machine can verify everything but can never sign as any existing "
            "actor. The kit contains PRIVATE KEY MATERIAL — offline storage "
            "only."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--inventory", action="store_true",
                      help="enumerate all coordinator-private/critical state "
                           "-> recovery_manifest.json (paths+hashes+classes, "
                           "never contents; unregistered stateful files are "
                           "FLAGGED)")
    mode.add_argument("--export-kit", metavar="DIR",
                      help="copy the critical set + manifest into DIR "
                           "(refuses any git-tracked destination; the kit "
                           "holds PRIVATE KEY MATERIAL — offline storage "
                           "only)")
    mode.add_argument("--verify-kit", metavar="DIR",
                      help="hash-check every kit file against its manifest; "
                           "any mismatch/missing is a named failure")
    mode.add_argument("--restore-rehearsal", metavar="KIT_DIR",
                      help="restore into a temp home and prove the four "
                           "capabilities: verify / sign / write / replay "
                           "(the real ledger is never touched)")
    mode.add_argument("--boundary-rehearsal", action="store_true",
                      help="the PUBLIC-ONLY proof: restore from git-tracked "
                           "files alone — verification must pass, signing "
                           "must fail with the honest reason")
    mode.add_argument("--export-mirror", metavar="DIR",
                      help="export the PUBLIC mirror set (snapshot + anchor + "
                           "evidence bundle) + manifest into DIR")
    mode.add_argument("--attest-mirror", metavar="DIR",
                      help="SIGN a mirror attestation for DIR (runs "
                           "--check-mirror first; requires --workdir with "
                           "the participant keychain; the SIGNED bundle is "
                           "public, the keychain never leaves the device)")
    mode.add_argument("--check-mirror", metavar="DIR",
                      help="compare a mirror against current published state: "
                           "IDENTICAL / BEHIND (verified prefix) / DIVERGED "
                           "(THE ALARM: named index, non-zero exit)")
    mode.add_argument("--selftest", action="store_true",
                      help="run the mechanical self-test (temp files only; "
                           "the real ledger is asserted byte-identical "
                           "before/after)")
    parser.add_argument("--workdir", default=None,
                        help="with --attest-mirror: the participant workdir "
                             "holding the private keychain (stays local)")
    parser.add_argument("--out", default=MANIFEST_NAME,
                        help="with --inventory: manifest output path "
                             f"(default {MANIFEST_NAME})")
    args = parser.parse_args(argv)

    if args.inventory:
        manifest = build_inventory()
        _write_json(args.out, manifest)
        print("coordinator state inventory (paths + hashes + classes — never "
              "contents):")
        for f in manifest["files"]:
            print(f"  [{f['criticality']}] {f['path']}  "
                  f"({f['size']} B, sha256 {f['sha256'][:16]}..)")
        for rel in manifest["flagged"]:
            print(f"  FLAGGED: {rel} is stateful-looking but NOT in the "
                  "known-state registry — register or retire it deliberately")
        print("summary: " + ", ".join(f"{k} x{v}" for k, v in
                                      sorted(manifest["summary"].items())))
        print(f"manifest written: {args.out}")
        print(f"WARNING: {KIT_WARNING}")
        return 0

    if args.export_kit:
        try:
            manifest = export_kit(args.export_kit)
        except ValueError as exc:
            print(f"REFUSED: {exc}", file=sys.stderr)
            return 2
        print(f"kit exported to {args.export_kit}: "
              f"{len(manifest['files'])} file(s), classes "
              + ", ".join(f"{k} x{v}" for k, v in
                          sorted(manifest["summary"].items())))
        print(f"WARNING: {KIT_WARNING}")
        print("next: --verify-kit, then --restore-rehearsal, then copy the "
              "kit to offline/second storage NOW — the tooling only matters "
              "if the kit leaves this machine.")
        return 0

    if args.verify_kit:
        ok, results = verify_kit(args.verify_kit)
        for r in results:
            print(f"  [{r['status'].upper():8s}] {r['path']}: {r['detail']}")
        print(f"kit verification: {'PASS' if ok else 'FAIL'}")
        return 0 if ok else 1

    if args.restore_rehearsal or args.boundary_rehearsal:
        kit = args.restore_rehearsal if args.restore_rehearsal else None
        report = restore_rehearsal(kit)
        print(f"restore rehearsal ({report['mode']}):")
        all_ok = _print_capabilities(report)
        if report["mode"] == "public-only":
            sign = report["capabilities"]["sign"]
            boundary_holds = (
                report["capabilities"]["verify"]["ok"]
                and report["capabilities"]["write"]["ok"]
                and report["capabilities"]["replay"]["ok"]
                and not sign["ok"] and BOUNDARY_REASON in sign["detail"])
            print("public-only boundary: "
                  + ("HOLDS — verifiability survives (verify/write/replay "
                     "pass), operatorship does not (signing fails with the "
                     "honest reason)" if boundary_holds
                     else "VIOLATED — see capabilities above"))
            return 0 if boundary_holds else 1
        print("restored coordinator is "
              + ("FULLY OPERATIONAL (all four capabilities)" if all_ok
                 else "NOT fully operational — see failures above"))
        return 0 if all_ok else 1

    if args.export_mirror:
        try:
            manifest = export_mirror(args.export_mirror)
        except ValueError as exc:
            print(f"REFUSED: {exc}", file=sys.stderr)
            return 2
        print(f"mirror exported to {args.export_mirror}: "
              f"{len(manifest['files'])} public file(s), tip_index "
              f"{manifest['tip_index']}, entry_count "
              f"{manifest['entry_count']}")
        print(f"recipe: {manifest['verification_recipe']}")
        return 0

    if args.attest_mirror:
        if not args.workdir:
            print("--attest-mirror requires --workdir (the participant "
                  "workdir holding the PRIVATE keychain)")
            return 2
        out = (args.out if args.out != MANIFEST_NAME
               else "mirror_attestation.json")
        try:
            bundle, sha = attest_mirror(args.attest_mirror, args.workdir,
                                        out_path=out)
        except (ValueError, OSError, KeyError) as exc:
            print(f"attestation refused: {exc}")
            return 2
        a = bundle["attestation"]
        print(f"mirror attestation SIGNED: {out}")
        print(f"  canonical sha256: {sha}")
        print(f"  attested chain point: tip_index {a['mirror']['tip_index']} "
              f"({a['mirror']['entry_count']} entries), check verdict "
              f"{a['check']['verdict']}")
        print(f"  signer: {a['actor_id']} (one-time key index "
              f"{bundle['key_index']}); fingerprint "
              f"{a['machine_fingerprint'][:28]}..")
        print("  scope: " + a["honest_scope"][:80] + "..")
        print("  send THIS FILE to the coordinator; the keychain stays here.")
        return 0

    if args.check_mirror:
        v = check_mirror(args.check_mirror)
        if v["verdict"] == "DIVERGED":
            print("=" * 70)
            print("!!! MIRROR DIVERGENCE ALARM !!!")
            print("=" * 70)
        print(f"mirror check: {v['verdict']} — {v['detail']}")
        return v["exit_code"]

    return _selftest()


# ============================== SELF-TEST ====================================
def _selftest() -> int:
    """Mechanical self-test: inventory flagging, kit round-trip + tamper
    detection, tracked-destination refusal, fixture + real restore
    capabilities, the public-only boundary, and the mirror verdict fixtures.
    Temp files only; the REAL ledger is asserted byte-identical before/after."""
    import tempfile

    print("=== protocol/continuity.py self-test (continuity & mirror-readiness) ===")
    print("Operational tooling: ZERO ledger writes — not everything deserves an anchor.\n")

    checks = []
    root_before = set(os.listdir(_REPO_ROOT))
    real_ledger = os.path.join(_PROTO_DIR, "ledger_data.jsonl")
    ledger_sha_before = (_sha256_file(real_ledger)
                         if os.path.exists(real_ledger) else None)
    tmp = tempfile.mkdtemp(prefix=f"continuity_selftest_{os.getpid()}_")
    try:
        # ---- fixture coordinator state (a tiny fake repo dir) --------------
        fx = os.path.join(tmp, "fixture_repo")
        os.makedirs(os.path.join(fx, "protocol"))
        fx_kc = actor_identity.build_keychain_from_privates(
            "continuity-fixture-actor",
            [[[hashlib.sha256(f"cf:{k}:{i}:{h}".encode()).hexdigest()
               for h in (0, 1)] for i in range(256)] for k in range(4)])
        _write_json(os.path.join(fx, "keychain.json"), fx_kc)
        led = ledger_mod.Ledger(os.path.join(fx, "protocol",
                                             "ledger_data.jsonl"))
        led.append({"event": "ledger_genesis", "note": "fixture",
                    "zero_value": True, "no_token": True})
        led.append({"event": "actor_key_registered",
                    "status": "actor-key-registered",
                    "actor_id": "continuity-fixture-actor",
                    "merkle_root": fx_kc["merkle_root"],
                    "key_count": 4, "zero_value": True, "no_token": True})
        _write_json(os.path.join(fx, "treasury_state.json"),
                    {"fixture": True})
        # the PLANTED UNKNOWN: stateful-looking, not in the registry
        _write_json(os.path.join(fx, "keychain_unknown.json"),
                    {"actor_id": "planted", "private_probe": "x"})

        # [1] inventory: registry classes + the planted unknown is FLAGGED
        inv = build_inventory(base_dir=fx)
        by_path = {f["path"]: f for f in inv["files"]}
        checks.append(("inventory classifies registry files by criticality",
                       by_path["keychain.json"]["criticality"]
                       == IDENTITY_FATAL
                       and by_path["protocol/ledger_data.jsonl"]["criticality"]
                       == LEDGER_RECOVERABLE
                       and by_path["treasury_state.json"]["criticality"]
                       == REGENERABLE))
        checks.append(("a planted unknown stateful file is FLAGGED, never "
                       "silently ignored",
                       "keychain_unknown.json" in inv["flagged"]
                       and by_path["keychain_unknown.json"]["criticality"]
                       == UNREGISTERED))
        secret = fx_kc["keys"][0]["private"][0][0]
        checks.append(("manifest lists paths+hashes, never contents (no "
                       "keychain secret appears in it)",
                       secret not in json.dumps(inv)))

        # [2] export refusal: a tracked in-repo destination is refused
        try:
            export_kit(os.path.join(_REPO_ROOT, "protocol",
                                    "kit_refusal_probe"))
            checks.append(("export-kit REFUSES a tracked in-repo destination",
                           False))
        except ValueError as exc:
            checks.append(("export-kit REFUSES a tracked in-repo destination",
                           "refusing" in str(exc).lower()
                           and not os.path.exists(
                               os.path.join(_REPO_ROOT, "protocol",
                                            "kit_refusal_probe"))))

        # [3] kit round-trip (fixture): export -> verify passes
        kit = os.path.join(tmp, "fixture_kit")
        export_kit(kit, base_dir=fx)
        ok_kit, results = verify_kit(kit)
        checks.append(("kit round-trip: export then verify-kit passes",
                       ok_kit and len(results) == len(inv["files"])))

        # [4] tamper detection: altered file -> named mismatch; removed file
        # -> named missing
        tampered = os.path.join(kit, "treasury_state.json")
        with open(tampered, "a", encoding="utf-8") as f:
            f.write("\n")
        ok_t, results_t = verify_kit(kit)
        status = {r["path"]: r["status"] for r in results_t}
        os.remove(os.path.join(kit, "keychain_unknown.json"))
        ok_m, results_m = verify_kit(kit)
        status_m = {r["path"]: r["status"] for r in results_m}
        checks.append(("verify-kit names tampered and missing kit files",
                       not ok_t and status["treasury_state.json"] == "mismatch"
                       and not ok_m
                       and status_m["keychain_unknown.json"] == "missing"))

        # [5] fixture restore capabilities: SIGN and WRITE on a restored home
        # built purely from the fixture kit (no git corpus involved)
        fx_home = os.path.join(tmp, "fixture_home")
        os.makedirs(os.path.join(fx_home, "protocol"))
        shutil.copyfile(os.path.join(fx, "protocol", "ledger_data.jsonl"),
                        os.path.join(fx_home, "protocol",
                                     "ledger_data.jsonl"))
        shutil.copyfile(os.path.join(fx, "keychain.json"),
                        os.path.join(fx_home, "keychain.json"))
        s_ok, s_detail = capability_sign(fx_home,
                                         actor="continuity-fixture-actor")
        w_ok, w_detail = capability_write(fx_home)
        checks.append(("fixture restore: SIGN capability (active-root "
                       "keychain found, probe verified)", s_ok))
        checks.append(("fixture restore: WRITE capability (probe on a COPY, "
                       "chain re-verified)", w_ok))
        # ...and the fixture's real ledger file was not extended by the probe
        checks.append(("write probe touched only the copy",
                       len(ledger_mod.Ledger(os.path.join(
                           fx_home, "protocol",
                           "ledger_data.jsonl")).read_all()) == 2))
        # sign refusal without the keychain: the boundary reason, verbatim
        os.remove(os.path.join(fx_home, "keychain.json"))
        s2_ok, s2_detail = capability_sign(fx_home,
                                           actor="continuity-fixture-actor")
        checks.append(("fixture restore without keychain: sign fails with "
                       "the honest boundary reason",
                       not s2_ok and BOUNDARY_REASON in s2_detail))

        # [6] PUBLIC-ONLY BOUNDARY on the real repo (tracked files only):
        # verification passes; signing fails with the honest reason. This is
        # the constitutional proof: verifiability survives, operatorship
        # does not.
        boundary = restore_rehearsal(kit_dir=None)
        b = boundary["capabilities"]
        checks.append(("PUBLIC-ONLY BOUNDARY: verify + write(reconstructed) "
                       "+ replay pass",
                       b["verify"]["ok"] and b["write"]["ok"]
                       and b["replay"]["ok"]
                       and "RECONSTRUCTED" in b["write"]["detail"]))
        checks.append(("PUBLIC-ONLY BOUNDARY: signing fails with the honest "
                       "reason (operatorship does not survive)",
                       not b["sign"]["ok"]
                       and BOUNDARY_REASON in b["sign"]["detail"]))

        # [7] REAL restore rehearsal (only when this machine holds private
        # state): export a temp kit from the real coordinator and prove all
        # four capabilities on the restored home
        if os.path.exists(real_ledger) and glob.glob(
                os.path.join(_REPO_ROOT, "keychain*.json")):
            real_kit = os.path.join(tmp, "real_kit")
            export_kit(real_kit)
            ok_rk, _r = verify_kit(real_kit)
            report = restore_rehearsal(real_kit)
            c = report["capabilities"]
            checks.append(("REAL restore rehearsal: kit verifies and all "
                           "four capabilities pass (verify/sign/write/"
                           "replay)",
                           ok_rk and all(c[n]["ok"] for n in
                                         ("verify", "sign", "write",
                                          "replay"))))
            for n in ("verify", "sign", "write", "replay"):
                if not c[n]["ok"]:
                    print(f"    FAIL {n}: {c[n]['detail']}")
        else:
            print("    (no private coordinator state on this machine — REAL "
                  "restore-rehearsal leg SKIPPED, named; the fixture + "
                  "boundary legs above cover the mechanism)")

        # [7b] PRE-STAGED ROTATION RESERVES join kit verification: staged
        # passes (re-validated against the CURRENT chain), a stale kit
        # keychain copy is a NAMED failure, a stale reserve (root since
        # rotated) is FLAGGED retired without failing the kit, and a
        # tampered reserve file is a NAMED failure.
        rkit = os.path.join(tmp, "reserve_kit")
        fx_ledger = os.path.join(fx, "protocol", "ledger_data.jsonl")
        actor_identity.stage_reserve("continuity-fixture-actor",
                                     kit_dir=rkit, base_dir=fx,
                                     ledger_source=fx_ledger)
        export_kit(rkit, base_dir=fx)  # kit copies AFTER staging: marks fresh
        ok_r, rows_r = verify_kit(rkit, base_dir=fx)
        rrow = {r["path"]: r for r in rows_r}.get(
            "reserve_continuity-fixture-actor/")
        checks.append(("verify-kit includes the staged reserve, re-validated "
                       "against the CURRENT chain",
                       ok_r and rrow is not None
                       and rrow["status"] == "reserve-ok"
                       and "would anchor today" in rrow["detail"]))

        kc_copy_path = os.path.join(rkit, "keychain.json")
        with open(kc_copy_path, "r", encoding="utf-8") as f:
            kc_copy_orig = f.read()
        kc_copy = json.loads(kc_copy_orig)
        kc_copy["used_indices"] = []
        _write_json(kc_copy_path, kc_copy)
        ok_ks, rows_ks = verify_kit(rkit, base_dir=fx)
        row_ks = {r["path"]: r for r in rows_ks}.get(
            "reserve_continuity-fixture-actor/")
        checks.append(("verify-kit FAILS a kit keychain copy that misses the "
                       "reserve's spent signing index",
                       not ok_ks
                       and row_ks["status"] == "reserve-kit-stale"))
        with open(kc_copy_path, "w", encoding="utf-8") as f:
            f.write(kc_copy_orig)

        kc_other = actor_identity.build_keychain_from_privates(
            "continuity-fixture-actor",
            [[[hashlib.sha256(f"co:{k}:{i}:{h}".encode()).hexdigest()
               for h in (0, 1)] for i in range(256)] for k in range(4)])
        ledger_mod.Ledger(fx_ledger).append({
            "event": "actor_key_rotated", "status": "actor-key-rotated",
            "actor_id": "continuity-fixture-actor",
            "prev_root": fx_kc["merkle_root"],
            "new_root": kc_other["merkle_root"], "new_key_count": 4,
            "signed": True, "signer_actor_id": "continuity-fixture-actor",
            "key_index": 3, "zero_value": True, "no_token": True})
        ok_st, rows_st = verify_kit(rkit, base_dir=fx)
        row_st = {r["path"]: r for r in rows_st}.get(
            "reserve_continuity-fixture-actor/")
        checks.append(("verify-kit FLAGS a stale reserve as retired (root "
                       "since rotated) without failing the kit",
                       ok_st and row_st["status"] == "reserve-retired"
                       and "STALE" in row_st["detail"]))

        with open(os.path.join(rkit, "reserve_continuity-fixture-actor",
                               actor_identity.RESERVE_KEYCHAIN_NAME), "a",
                  encoding="utf-8") as f:
            f.write("\n")
        ok_tp, rows_tp = verify_kit(rkit, base_dir=fx)
        row_tp = {r["path"]: r for r in rows_tp}.get(
            "reserve_continuity-fixture-actor/")
        checks.append(("verify-kit names a tampered reserve file "
                       "(sha256 mismatch -> reserve-invalid)",
                       not ok_tp and row_tp["status"] == "reserve-invalid"
                       and "sha256" in row_tp["detail"]))

        # [8] mirror fixtures: IDENTICAL / BEHIND / DIVERGED / manifest tamper
        mfx = os.path.join(tmp, "mirror_base")
        os.makedirs(os.path.join(mfx, "protocol"))
        mled = ledger_mod.Ledger(os.path.join(mfx, "protocol",
                                              "ledger_data.jsonl"))
        for i in range(4):
            mled.append({"event": f"fixture_{i}", "zero_value": True,
                         "no_token": True})
        audit.export_snapshot(mled.path, os.path.join(mfx, "protocol",
                                                      "ledger_published.json"))
        audit.write_anchor(mled.path, os.path.join(mfx, "protocol",
                                                   "ledger_anchor.json"))
        mdir = os.path.join(tmp, "mirror_copy")
        export_mirror(mdir, base_dir=mfx)
        v_id = check_mirror(mdir, base_dir=mfx)
        checks.append(("mirror check: IDENTICAL on a fresh export",
                       v_id["verdict"] == "IDENTICAL"
                       and v_id["exit_code"] == 0))

        mled.append({"event": "fixture_4", "zero_value": True,
                     "no_token": True})
        mled.append({"event": "fixture_5", "zero_value": True,
                     "no_token": True})
        v_behind = check_mirror(mdir, base_dir=mfx)
        checks.append(("mirror check: BEHIND when the chain grew (verified "
                       "prefix, entries counted)",
                       v_behind["verdict"] == "BEHIND"
                       and "2 entr" in v_behind["detail"]
                       and v_behind["exit_code"] == 0))

        # DIVERGED: rewrite the local chain from index 3 (truncate + append a
        # different suffix) — the mirror still verifies internally but is no
        # longer a prefix; the first divergent index must be NAMED (3)
        with open(mled.path, "r", encoding="utf-8") as f:
            lines = [ln for ln in f if ln.strip()]
        with open(mled.path, "w", encoding="utf-8") as f:
            f.writelines(lines[:3])
        mled.append({"event": "rewritten_suffix", "zero_value": True,
                     "no_token": True})
        v_div = check_mirror(mdir, base_dir=mfx)
        checks.append(("mirror check: DIVERGED is LOUD (non-zero exit, first "
                       "divergent index named)",
                       v_div["verdict"] == "DIVERGED"
                       and v_div["exit_code"] == 2
                       and v_div["divergence_index"] == 3
                       and "rewriting" in v_div["detail"]))

        # manifest tamper: altered mirror file -> INVALID, named
        with open(os.path.join(mdir, "ledger_anchor.json"), "a",
                  encoding="utf-8") as f:
            f.write("\n")
        v_tamper = check_mirror(mdir, base_dir=mfx)
        checks.append(("mirror manifest tamper detection (altered file "
                       "named)",
                       v_tamper["verdict"] == "INVALID"
                       and "ledger_anchor.json" in v_tamper["detail"]))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # ZERO-LEDGER-WRITES PROOF: the real ledger is byte-identical.
    if ledger_sha_before is not None:
        checks.append(("REAL ledger byte-identical before/after (zero "
                       "ledger writes — operational tooling, not protocol "
                       "events)",
                       _sha256_file(real_ledger) == ledger_sha_before))
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
          ("ALL CHECKS BEHAVED CORRECTLY" if ok else "FAILURE — see above") + " ===")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
