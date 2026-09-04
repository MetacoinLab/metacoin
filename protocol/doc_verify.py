# Copyright (c) 2023-2026 MetaCoin-Lab.
# Licensed under SML-1.0 — see LICENSE.md.
"""doc_verify.py — the documentation anti-rot machine (doc suite "docs/0.1").

================== HONESTY / SCOPE (READ ME) ==================
Research-stage, ZERO-VALUE, no token, no money, no mainnet, no networking, no payments.

The four documents under docs/ (PARTICIPATE, ARCHITECTURE, VERIFICATION,
TRUST-MODEL) state THE DOC CONTRACT: everything in them is [BUILT] fact,
mechanically verified on every CI run. This module is the machine that makes the
contract true. Documentation that is merely written rots the moment the chain
grows or a command changes; documentation whose every command is EXECUTED and
whose every number is RECOMPUTED cannot silently rot — it fails CI instead, by
name.

Three verified constructs inside the docs:

  1. COMMAND BLOCKS — fenced blocks whose info string is `verify-run`:
         ```verify-run
         $ <command>
         <pasted real output — display only; trimmed for volume, never altered>
         ```
         <!--expect:SUBSTRING-->        (zero or more, immediately after the fence)
     --check executes each command, in document order, inside ONE shared
     fresh-clone sandbox (git clone of this repo at HEAD into a temp dir — the
     stranger's starting state; state persists across blocks so multi-step
     walkthroughs run exactly as a reader would run them). Asserts exit 0 and
     that every expect-substring appears in the combined output. The PASTED
     output is presentation, not assertion — real outputs legitimately vary in
     timing digits; the expect-substrings are the load-bearing claims.

  2. CHAIN NUMBER TOKENS — inline markers
         <!--chain:KEY-->VALUE<!--/chain-->
     rendered at doc-build time (--render) from live state and re-verified by
     --check: the verifier recomputes KEY from the ledger snapshot / evidence
     bundle / runner scripts and diffs against VALUE. No number in the docs is
     ever written from memory. --render is a DELIBERATE HUMAN STEP, not
     automatic: after legitimate chain growth the coordinator re-renders and
     commits, so docs change only in reviewed commits and --check stays red in
     between — stale docs are a named CI failure, never a silent drift.

  3. LEDGER-INDEX REFERENCES — every `idx N` / `idx N-M` in prose is verified
     to exist on the chain; the typed form
         <!--idx:N=event_name-->
     additionally asserts entry N carries exactly that event. A doc cannot cite
     a ledger record that is not there, nor mislabel what a record is.

MIP DOCUMENTS (mip/MIP-*.md) are in the --check scan set too: typed idx
references resolved and verify-run blocks executed exactly like the docs'
(no contract line required — pre-process drafts predate it). --render NEVER
touches mip/: an anchored MIP file is immutable-by-citation (the anchored
decision record pins its sha256), so nothing may rewrite it — which is also
why MIP files must cite typed idx references instead of chain tokens
(protocol/mip_process.py --check refuses tokens).

WHAT THIS MODULE NEVER DOES: it never writes to the ledger (read-only against
the live-or-snapshot chain, same resolution as every verifier); --render
rewrites ONLY the VALUE spans between chain markers in docs/ files, nothing
else (and never mip/); --check writes nothing at all outside its temp sandbox.

Standard library only (json, os, re, sys, argparse, shutil, subprocess,
tempfile). Ledger resolution and evidence discovery are REUSED from
protocol/prov_export.py and protocol/work_molecule.py; the task registry from
protocol/verifier_cli.py. Not legal, financial, investment, or
security-certification advice.

Usage:
    python3 protocol/doc_verify.py --check              # CI mode: verify everything
    python3 protocol/doc_verify.py --check --no-exec    # tokens + idx refs only
    python3 protocol/doc_verify.py --render             # re-render number tokens (human step)
    python3 protocol/doc_verify.py --selftest           # fixtures; writes nothing
"""

# Suppress __pycache__/*.pyc so importing protocol modules below leaves no stray files.
import sys
sys.dont_write_bytecode = True

import argparse
import json
import os
import re
import shutil
import subprocess
import tempfile

_PROTO_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_PROTO_DIR)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# REUSE: chain-source resolution, ledger reader, evidence discovery, task registry.
from protocol.prov_export import resolve_ledger_path
from protocol.work_molecule import (_read_ledger, find_evidence_file,
                                    _payload_references_task)
from protocol.verifier_cli import TASK_MODULES

DOCS_DIR = os.path.join(_REPO_ROOT, "docs")
# Fixed processing order: multi-step walkthrough state (participate init -> run
# -> bundle) must execute in the order a reader would.
DOC_FILES = ("PARTICIPATE.md", "ARCHITECTURE.md", "VERIFICATION.md",
             "TRUST-MODEL.md", "TOUR.md", "COLLABORATE.md", "PULSE.md",
             # The software/data transfer family's evidence doc (joined
             # with the task-0035..0040 batch).
             "TRANSFER.md",
             # Integration READMEs joined the scan set 2026-08-31 after a
             # link audit caught them still claiming "the 18-task library" —
             # public-facing numeric claims may not live outside the
             # anti-rot machine. Entries carrying a "/" are repo-relative.
             "integrations/inspect/README.md",
             "integrations/hal/README.md",
             "integrations/baselines/README.md",
             "integrations/openmct/README.md",
             "integrations/mhs/README.md",
             # The ledger-native changelog: keyed by idx range, typed idx
             # references only (no chain tokens — past ranges never rot).
             "./CHANGELOG.md")
# TOUR.md is ERA-PINNED like the README (era tokens, no chain tokens):
# --render never touches it, and --check verifies its tagged numbers at
# its own declared as-of point via the same check_readme machinery.
TOUR_PATH = os.path.join(DOCS_DIR, "TOUR.md")


def _doc_path(docs_dir, name):
    """Resolve a DOC_FILES entry: plain basenames live in docs_dir;
    entries with a "/" are repo-relative, resolved against docs_dir's
    PARENT — so the self-test's fixture docs_dir simply reports them
    'missing from' (a finding its fixtures filter), never executes them."""
    if "/" in name:
        return os.path.join(os.path.dirname(docs_dir), name)
    return os.path.join(docs_dir, name)

# MIP documents (mip/MIP-*.md) join the scan set: their typed idx references
# are resolved and their verify-run blocks executed exactly like the docs'.
# Differences, both deliberate: no DOC-CONTRACT line is required (pre-process
# drafts predate the contract), and --render NEVER touches mip/ — an anchored
# MIP file is immutable-by-citation (its sha256 is pinned by the anchored
# decision record), which is also why a MIP must not embed chain tokens
# (protocol/mip_process.py --check refuses them).
MIP_DIR = os.path.join(_REPO_ROOT, "mip")


def _mip_files(mip_dir):
    """Sorted MIP markdown basenames in `mip_dir` ([] if absent)."""
    if mip_dir is None or not os.path.isdir(mip_dir):
        return []
    return sorted(n for n in os.listdir(mip_dir)
                  if n.startswith("MIP-") and n.endswith(".md"))


_MIP_CITE_RE = re.compile(r"\bMIP-(\d{4})\b")
_MIP_SUPERSEDES_RE = re.compile(r"\*\*Supersedes:\*\* ([^·\n]+)")


def _superseded_mips(mip_dir):
    """MIP numbers some other MIP declares it supersedes. A superseded MIP
    stays immutable-by-citation (its anchored hash still re-derives), but
    its verify-run blocks are RETIRED from execution: an anchored document
    may assert its era's honest state (e.g. MIP-0005's named release gaps),
    and when reality moves past that state the successor MIP retires the
    assertion without ever editing the anchored file."""
    superseded = set()
    for name in _mip_files(mip_dir):
        with open(os.path.join(mip_dir, name)) as f:
            header = f.read(2000)
        m = _MIP_SUPERSEDES_RE.search(header)
        if m:
            superseded |= set(_MIP_CITE_RE.findall(m.group(1)))
    return superseded


def unresolved_mip_citations(text, mip_dir, own_number=None):
    """Cited MIP numbers with no matching MIP-NNNN-*.md beside them.

    A MIP (or any doc) may not cite a proposal that does not exist — a
    dangling citation is a promise the corpus cannot back (MIP-0002 §3
    pointed at an unwritten MIP-0003 for months; this check makes that
    state loud). `own_number` excludes the citing file's own id."""
    existing = {n[4:8] for n in _mip_files(mip_dir)}
    return sorted({f"MIP-{n}" for n in _MIP_CITE_RE.findall(text)
                   if n != own_number and n not in existing})

CONTRACT_MARKER = "mechanically verified by protocol/doc_verify.py"

# README is ERA-PINNED, not live-rendered: it declares its as-of chain point
# ONCE (the era-pin marker) and every tagged number is checked against the
# chain AT that point — generation-lock semantics for prose. The README stays
# verifiably green as the chain grows and goes red only if it misstates its
# own declared era (wrong pin, or a wrong number at the pinned state). The
# monthly README batch moves the pin deliberately, like a --render.
README_PATH = os.path.join(_REPO_ROOT, "README.md")
_ERA_PIN_RE = re.compile(
    r"<!--era-pin:entry_count=(\d+) tip_hash_prefix=([0-9a-f]{12})-->")
_ERA_TOKEN_RE = re.compile(r"<!--era:([a-z0-9_]+)-->(.*?)<!--/era-->", re.S)

_TOKEN_RE = re.compile(r"<!--chain:([a-z0-9_]+)-->(.*?)<!--/chain-->", re.S)
_IDX_TYPED_RE = re.compile(r"<!--idx:(\d+)=([a-z0-9_]+)-->")
_IDX_PROSE_RE = re.compile(r"\bidx (\d+)(?:[-–](\d+))?")
_FENCE_RE = re.compile(r"^```verify-run\n\$ (.+?)\n(.*?)^```\n((?:<!--expect:.*?-->\n)*)",
                       re.S | re.M)
_EXPECT_RE = re.compile(r"<!--expect:(.*?)-->")

from protocol.parameter_table import get as _param_table_get
COMMAND_TIMEOUT_S = _param_table_get("docs.command_timeout_s")


# ----------------------------------------------------------------------------
# chain number tokens: KEY -> recompute-from-live-state
# ----------------------------------------------------------------------------
def _count_runner_tests(script_path):
    """Count entries of the TESTS=( ... ) array in a runner script — the same
    list the runner executes, so the doc's suite count can never drift from
    the wiring."""
    with open(script_path) as f:
        text = f.read()
    body = re.search(r"TESTS=\((.*?)\)", text, re.S)
    if not body:
        raise ValueError(f"no TESTS array in {script_path}")
    return len(re.findall(r'^\s*"[^"]+"', body.group(1), re.M))


def _evidence_json(basename):
    path = find_evidence_file(basename)
    if path is None:
        raise FileNotFoundError(f"evidence file {basename} not found")
    with open(path) as f:
        return json.load(f)


def compute_tokens(repo_root=_REPO_ROOT, entries=None):
    """Recompute every chain token from live state. Returns {key: str_value}.
    Every value is a string exactly as it must appear between the markers."""
    if entries is None:
        entries = _read_ledger(resolve_ledger_path())
    tip = entries[-1]
    aci = _evidence_json("aci_report.json")
    passports = _evidence_json("passport_catalog.json")
    metering = _evidence_json("metering_report.json")
    return {
        "entry_count": str(len(entries)),
        "tip_index": str(tip["index"]),
        "tip_hash_prefix": tip["hash"][:12],
        "genesis_hash_prefix": entries[0]["hash"][:12],
        "task_count": str(len(TASK_MODULES)),
        # Registered vs RECORDED can legitimately differ: new tasks stay
        # unanchored until the next milestone batch (the cadence policy), so
        # docs must be able to state each number honestly.
        "recorded_task_count": str(sum(
            1 for tid in TASK_MODULES
            if any(_payload_references_task(e.get("payload"), tid)
                   for e in entries if isinstance(e, dict)))),
        "actor_count": str(len({e["payload"]["actor_id"] for e in entries
                                if e.get("payload", {}).get("event")
                                == "actor_key_registered"})),
        "drill_entry_count": str(sum(1 for e in entries
                                     if e.get("payload", {}).get("drill"))),
        "catalog_anchor_count": str(sum(
            1 for e in entries if e.get("payload", {}).get("event")
            == "work_molecule_catalog_anchored")),
        # honest-negative roster size (integrations/core.py HONEST_NEGATIVES —
        # the abstention-probe subset the integration READMEs cite); local
        # import: integrations/ ships in the repo but not in the wheel
        "honest_negative_count": str(len(__import__(
            "integrations.core", fromlist=["HONEST_NEGATIVES"]
        ).HONEST_NEGATIVES)),
        "protocol_suite_count": str(_count_runner_tests(
            os.path.join(repo_root, "protocol", "run_protocol_selftests.sh"))),
        "demo_suite_count": str(_count_runner_tests(
            os.path.join(repo_root, "demo", "run_all_selftests.sh"))),
        "aci_pairwise": f"{round(aci['pairwise_aci'], 5)}",
        "aci_path_count": str(aci["path_count"]),
        "aci_pair_count": str(aci["pair_count"]),
        "passport_actor_count": str(len(passports["entries"])),
        "assumed_power_w": f"{metering['assumed_cpu_power_w']}",
        **_pulse_tokens(entries),
    }


def _pulse_tokens(entries):
    """Tokens read from the LATEST anchored pulse record (chain-derived,
    deterministic): the numbers docs/PULSE.md renders. 'none' before the
    first pulse exists."""
    recs = [e for e in entries if isinstance(e.get("payload"), dict)
            and e["payload"].get("event") == "pulse_recorded"
            and e["payload"].get("status") == "pulse-confirmed"]
    keys = ("pulse_idx", "pulse_hash_prefix", "pulse_date", "pulse_entries",
            "pulse_tip_index", "pulse_commit", "pulse_layers", "pulse_demo_suite",
            "pulse_protocol_suite", "pulse_task_law", "pulse_doc_commands",
            "pulse_sweep_findings", "pulse_cold_install", "pulse_tasks",
            "pulse_honest_negatives", "pulse_mip_decisions", "pulse_actors",
            "pulse_mirror_idx", "pulse_entries_since", "pulse_count")
    if not recs:
        return {k: "none" for k in keys}
    e = recs[-1]
    p = e["payload"]
    h = p.get("headline", {})
    tl = h.get("task_law", {})
    import datetime as _dt
    date = _dt.datetime.utcfromtimestamp(p.get("anchored_at", 0)).strftime("%Y-%m-%d")
    return {
        "pulse_idx": str(e["index"]),
        "pulse_hash_prefix": str(p.get("pulse_hash", ""))[:12],
        "pulse_date": date,
        "pulse_entries": str(p.get("as_of_chain", {}).get("entries", "")),
        "pulse_tip_index": str(p.get("as_of_chain", {}).get("tip_index", "")),
        "pulse_commit": str(p.get("repo_commit", ""))[:8],
        "pulse_layers": str(h.get("verify_everything_layers", "")),
        "pulse_demo_suite": str(h.get("demo_suite", "")),
        "pulse_protocol_suite": str(h.get("protocol_suite", "")),
        "pulse_task_law": (f"{tl.get('grandfathered')} grandfathered / "
                           f"{tl.get('bound')} bound / {tl.get('violations')} violations"),
        "pulse_doc_commands": str(h.get("doc_commands", "")),
        "pulse_sweep_findings": str(h.get("sweep_findings", "")),
        "pulse_cold_install": str(h.get("cold_install", "")),
        "pulse_tasks": str(h.get("tasks_recorded", "")),
        "pulse_honest_negatives": str(h.get("honest_negatives", "")),
        "pulse_mip_decisions": str(h.get("mip_decisions", "")),
        "pulse_actors": str(h.get("registered_actors", "")),
        "pulse_mirror_idx": str(h.get("mirror_last_attested_idx", "")),
        "pulse_entries_since": str(len(entries) - 1 - int(p.get("as_of_chain", {}).get("tip_index", 0))),
        "pulse_count": str(len(recs)),
    }


def compute_era_tokens(entries, pin_count):
    """Era-pinned token values: every value is derived from the chain AT the
    declared as-of point (the first `pin_count` entries) and NOTHING else —
    no live files, no registry state that later growth could move. Returns
    {key: str} exactly as each value must appear between era markers."""
    era = entries[:pin_count]
    tip = era[-1]
    payloads = [e.get("payload", {}) for e in era if isinstance(e, dict)]
    epoch = next((p for p in reversed(payloads)
                  if p.get("event") == "aci_epoch_observed"
                  and p.get("status") == "aci-epoch-confirmed"), {})
    baseline = next((p for p in reversed(payloads)
                     if p.get("event") == "aci_baseline_anchored"
                     and p.get("status") == "aci-baseline-confirmed"), {})
    return {
        "entry_count": str(len(era)),
        "tip_index": str(tip["index"]),
        "tip_hash_prefix": tip["hash"][:12],
        "recorded_task_count": str(sum(
            1 for tid in TASK_MODULES
            if any(_payload_references_task(e.get("payload"), tid)
                   for e in era if isinstance(e, dict)))),
        "drill_entry_count": str(sum(1 for p in payloads if p.get("drill"))),
        "catalog_anchor_count": str(sum(
            1 for p in payloads
            if p.get("event") == "work_molecule_catalog_anchored")),
        "mip_decision_count": str(sum(
            1 for p in payloads
            if p.get("event") == "mip_decision_recorded")),
        "mission_verdict_count": str(sum(
            1 for p in payloads
            if p.get("event") == "mission_verdict_recorded"
            and p.get("status") == "mission-verdict-confirmed")),
        "epoch_path_count": str(epoch.get("path_count", "")),
        "epoch_pairwise_aci": (f"{round(epoch['pairwise_aci'], 6)}"
                               if "pairwise_aci" in epoch else ""),
        "baseline_path_count": str(baseline.get("path_count", "")),
        "baseline_pairwise_aci": (f"{round(baseline['pairwise_aci'], 5)}"
                                  if "pairwise_aci" in baseline else ""),
    }


def check_readme(readme_path=README_PATH, entries=None):
    """Era-pinned README verification. Returns (findings, stats).

    No era-pin marker -> named non-finding (stats['pinned'] False): a
    pre-batch README simply has not opted in. With a marker: the PIN itself
    must match the chain (enough entries exist and the pinned tip hash
    prefix matches — a README cannot claim an era the chain never had), and
    every `<!--era:KEY-->VALUE<!--/era-->` must equal KEY's value computed
    at the pinned chain state. Live-tip growth beyond the pin NEVER reddens
    an era-pinned claim. Typed/prose idx references are checked against the
    full chain (records never vanish, so a citation is era-independent)."""
    findings = []
    stats = {"pinned": False, "era_tokens": 0, "idx_refs": 0}
    if entries is None:
        entries = _read_ledger(resolve_ledger_path())
    if not os.path.exists(readme_path):
        return ([f"README missing: {readme_path}"], stats)
    with open(readme_path) as f:
        text = f.read()
    name = os.path.basename(readme_path)

    _check_idx_refs(text, name, entries, findings)
    stats["idx_refs"] = (len(_IDX_PROSE_RE.findall(text))
                         + len(_IDX_TYPED_RE.findall(text)))

    pins = _ERA_PIN_RE.findall(text)
    era_tokens_found = _ERA_TOKEN_RE.findall(text)
    if not pins:
        if era_tokens_found:
            findings.append(f"{name}: era tokens present but no era-pin "
                            "marker declares the as-of point")
        return (findings, stats)
    if len(pins) > 1:
        findings.append(f"{name}: {len(pins)} era-pin markers — the as-of "
                        "point must be declared exactly once")
        return (findings, stats)
    stats["pinned"] = True
    pin_count, pin_prefix = int(pins[0][0]), pins[0][1]
    if pin_count < 1 or pin_count > len(entries):
        findings.append(f"{name}: era pin claims entry_count {pin_count} but "
                        f"the chain has {len(entries)} — a README cannot "
                        "claim an era the chain never had")
        return (findings, stats)
    actual_prefix = entries[pin_count - 1]["hash"][:12]
    if actual_prefix != pin_prefix:
        findings.append(f"{name}: era pin says tip {pin_prefix!r} at entry "
                        f"{pin_count} but the chain records "
                        f"{actual_prefix!r} — the declared era is misstated")
        return (findings, stats)

    era_values = compute_era_tokens(entries, pin_count)
    for key, value in era_tokens_found:
        stats["era_tokens"] += 1
        if key not in era_values:
            findings.append(f"{name}: unknown era token {key!r}")
        elif value != era_values[key]:
            findings.append(f"{name}: era token {key} says {value!r} but the "
                            f"chain at the pinned era (entry_count "
                            f"{pin_count}) computes {era_values[key]!r}")
    return (findings, stats)


# ----------------------------------------------------------------------------
# Identity freeze: the README header may carry ONLY the operator-owned
# strings in protocol/identity_text.py, byte-for-byte (markup stripped).
# ----------------------------------------------------------------------------
_MARKUP_RE = re.compile(r"</?(?:strong|em|b|i|p)>|\*\*|\*")
_HEADER_END = "<!--era-pin:"


def _normalize_identity(text):
    return " ".join(_MARKUP_RE.sub("", text).split())


def check_identity(readme_path=README_PATH, hero_path=None):
    """The README header (everything above the era-pin marker) is checked
    against the frozen identity strings: every <strong>, <em>, and blockquote
    in it must equal one of the constants after markup is stripped, the
    DEFINITION must be present, and the embedded hero banner's text nodes
    must all be frozen strings (with TAGLINE, MOTTO, LINEAGE present). Any
    drift is a finding BY NAME. Returns (findings, stats)."""
    from protocol import identity_text as T
    findings, stats = [], {"header_strings": 0, "hero_nodes": 0}
    if not os.path.exists(readme_path):
        return ([f"README missing: {readme_path}"], stats)
    with open(readme_path, encoding="utf-8") as f:
        text = f.read()
    name = os.path.basename(readme_path)
    header = text.split(_HEADER_END, 1)[0]
    found = []
    for m in re.finditer(r"<strong>(.*?)</strong>|<em>(.*?)</em>", header, re.S):
        found.append(m.group(1) if m.group(1) is not None else m.group(2))
    for m in re.finditer(r"<blockquote[^>]*>(.*?)</blockquote>", header, re.S):
        found.append(m.group(1))
    md_quote = [ln[2:] for ln in header.splitlines() if ln.startswith("> ")]
    if md_quote:
        found.extend(md_quote)
    seen = set()
    for raw in found:
        norm = _normalize_identity(raw)
        if not norm:
            continue
        stats["header_strings"] += 1
        # a bold phrase inside DEFINITION is not a string of its own
        if norm in T.FROZEN or norm in T.DEFINITION_BOLD:
            seen.add(norm)
        else:
            findings.append(f"{name}: header text is not a frozen identity "
                            f"string (protocol/identity_text.py): {norm[:80]!r}")
    if T.DEFINITION not in seen:
        findings.append(f"{name}: header does not carry DEFINITION verbatim")
    hero_rel = "assets/hero.svg"
    if hero_rel not in header:
        findings.append(f"{name}: header does not embed {hero_rel}")
    hero_path = hero_path or os.path.join(os.path.dirname(readme_path), hero_rel)
    if os.path.exists(hero_path):
        from protocol.hero_svg import text_nodes
        with open(hero_path, encoding="utf-8") as f:
            nodes = text_nodes(f.read())
        stats["hero_nodes"] = len(nodes)
        for n in nodes:
            if n not in T.FROZEN:
                findings.append(f"{name}: {hero_rel} carries a text node that "
                                f"is not a frozen identity string: {n[:80]!r}")
        for want, label in ((T.TAGLINE, "TAGLINE"), (T.MOTTO, "MOTTO"),
                            (T.LINEAGE, "LINEAGE")):
            if want not in nodes:
                findings.append(f"{name}: {hero_rel} does not carry {label}")
    else:
        findings.append(f"{name}: {hero_rel} is missing")
    return (findings, stats)


# ----------------------------------------------------------------------------
# --check
# ----------------------------------------------------------------------------
def _check_tokens(text, doc_name, tokens, findings):
    for match in _TOKEN_RE.finditer(text):
        key, value = match.group(1), match.group(2)
        if key not in tokens:
            findings.append(f"{doc_name}: unknown chain token {key!r}")
        elif value != tokens[key]:
            findings.append(f"{doc_name}: chain token {key} says {value!r} but "
                            f"live state computes {tokens[key]!r} — doc is "
                            "stale (re-render deliberately and commit)")


def _check_idx_refs(text, doc_name, entries, findings):
    by_index = {e["index"]: e for e in entries if isinstance(e.get("index"), int)}
    for match in _IDX_PROSE_RE.finditer(text):
        low = int(match.group(1))
        high = int(match.group(2)) if match.group(2) else low
        for n in sorted({low, high}):
            if n not in by_index:
                findings.append(f"{doc_name}: prose cites idx {n}, which does "
                                f"not exist (chain has indices 0.."
                                f"{max(by_index)})")
        if high < low:
            findings.append(f"{doc_name}: idx range {low}-{high} is inverted")
    for match in _IDX_TYPED_RE.finditer(text):
        n, event = int(match.group(1)), match.group(2)
        entry = by_index.get(n)
        actual = (entry or {}).get("payload", {}).get("event")
        if entry is None:
            findings.append(f"{doc_name}: typed reference idx {n} does not exist")
        elif actual != event:
            findings.append(f"{doc_name}: typed reference says idx {n} is "
                            f"{event!r} but the chain records {actual!r}")


def _parse_command_blocks(text):
    """[(command, [expect substrings])] in document order."""
    blocks = []
    for match in _FENCE_RE.finditer(text):
        expects = _EXPECT_RE.findall(match.group(3))
        blocks.append((match.group(1).strip(), expects))
    return blocks


def _make_sandbox(repo_root, tmp_dir):
    """Fresh-clone sandbox: git clone of the repo at HEAD — the stranger's
    starting state. Returns sandbox path or (None, reason)."""
    if shutil.which("git") is None:
        return None, "git unavailable"
    sandbox = os.path.join(tmp_dir, "doc_sandbox")
    r = subprocess.run(["git", "clone", "--quiet", repo_root, sandbox],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return None, f"git clone failed: {r.stderr.strip()[:200]}"
    return sandbox, None


def _run_command_blocks(doc_blocks, sandbox, findings, echo=print):
    """Execute every (doc_name, command, expects) in order inside the shared
    sandbox; named findings on non-zero exit or missing expect-substrings."""
    for doc_name, command, expects in doc_blocks:
        echo(f"  exec [{doc_name}] $ {command}")
        try:
            r = subprocess.run(command, shell=True, cwd=sandbox,
                               capture_output=True, text=True,
                               timeout=COMMAND_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            findings.append(f"{doc_name}: command timed out after "
                            f"{COMMAND_TIMEOUT_S}s: {command}")
            continue
        output = r.stdout + r.stderr
        if r.returncode != 0:
            findings.append(f"{doc_name}: command exited {r.returncode}: "
                            f"{command} — tail: {output.strip()[-300:]!r}")
            continue
        for expect in expects:
            if expect not in output:
                findings.append(f"{doc_name}: output of {command!r} lacks the "
                                f"expected substring {expect!r}")


def check_docs(docs_dir=DOCS_DIR, repo_root=_REPO_ROOT, execute=True,
               sandbox_dir=None, echo=print, mip_dir=None):
    """Verify every doc construct. Returns (findings list, stats dict).
    `sandbox_dir` (self-test fixtures) skips the git clone and runs command
    blocks in the given directory instead. `mip_dir` additionally scans MIP
    documents (idx references + verify-run blocks; no contract line
    required) — the real callers pass MIP_DIR, fixtures leave it None."""
    findings = []
    stats = {"docs": 0, "mips": 0, "tokens": 0, "idx_refs": 0, "commands": 0}
    entries = _read_ledger(resolve_ledger_path())
    tokens = compute_tokens(repo_root, entries)

    doc_blocks = []
    for name in DOC_FILES:
        path = _doc_path(docs_dir, name)
        if not os.path.exists(path):
            findings.append(f"{name}: missing from {docs_dir}")
            continue
        with open(path) as f:
            text = f.read()
        stats["docs"] += 1
        if CONTRACT_MARKER not in text:
            findings.append(f"{name}: THE DOC CONTRACT line is missing "
                            f"(must state: ...{CONTRACT_MARKER}...)")
        _check_tokens(text, name, tokens, findings)
        _check_idx_refs(text, name, entries, findings)
        stats["tokens"] += len(_TOKEN_RE.findall(text))
        stats["idx_refs"] += (len(_IDX_PROSE_RE.findall(text))
                              + len(_IDX_TYPED_RE.findall(text)))
        for command, expects in _parse_command_blocks(text):
            doc_blocks.append((name, command, expects))
    retired = _superseded_mips(mip_dir)
    for name in _mip_files(mip_dir):
        path = os.path.join(mip_dir, name)
        with open(path) as f:
            text = f.read()
        stats["mips"] += 1
        rel = f"mip/{name}"
        dangling = unresolved_mip_citations(text, mip_dir,
                                            own_number=name[4:8])
        if dangling:
            findings.append(f"{rel}: cites {dangling} but no such MIP "
                            "file(s) exist — a citation may not point at "
                            "an unwritten proposal")
        # chain tokens in a MIP would rot inside an immutable-by-citation
        # file; if one ever appears it is still value-checked here (a stale
        # value is a finding either way, and mip_process refuses them)
        _check_tokens(text, rel, tokens, findings)
        _check_idx_refs(text, rel, entries, findings)
        stats["tokens"] += len(_TOKEN_RE.findall(text))
        stats["idx_refs"] += (len(_IDX_PROSE_RE.findall(text))
                              + len(_IDX_TYPED_RE.findall(text)))
        if name[4:8] in retired:
            # superseded: citations/refs stay checked forever (above); the
            # era-state assertions in its blocks are retired from execution
            echo(f"  [{rel}] verify-run blocks RETIRED (superseded by a "
                 "later MIP; the file stays immutable-by-citation)")
            continue
        for command, expects in _parse_command_blocks(text):
            doc_blocks.append((rel, command, expects))
    stats["commands"] = len(doc_blocks)

    if doc_blocks and execute:
        if sandbox_dir is not None:
            _run_command_blocks(doc_blocks, sandbox_dir, findings, echo)
        else:
            with tempfile.TemporaryDirectory() as tmp:
                sandbox, reason = _make_sandbox(repo_root, tmp)
                if sandbox is None:
                    echo(f"  command execution SKIPPED (named): {reason} — "
                         "tokens and idx references were still verified")
                else:
                    _run_command_blocks(doc_blocks, sandbox, findings, echo)
    elif doc_blocks and not execute:
        echo("  command execution SKIPPED (named): --no-exec")
    return findings, stats


# ----------------------------------------------------------------------------
# --render (the deliberate human step)
# ----------------------------------------------------------------------------
def render_docs(docs_dir=DOCS_DIR, repo_root=_REPO_ROOT, echo=print):
    """Rewrite ONLY the VALUE spans between chain markers from live state.
    Returns the number of values changed. Prose, commands, and pasted outputs
    are never touched — if a command's real output changed, that is a content
    edit a human must make (and --check will say so via expect-substrings)."""
    tokens = compute_tokens(repo_root)
    changed = 0
    for name in DOC_FILES:
        path = _doc_path(docs_dir, name)
        if not os.path.exists(path):
            continue
        with open(path) as f:
            text = f.read()

        def _sub(match):
            nonlocal changed
            key, old = match.group(1), match.group(2)
            new = tokens.get(key, old)
            if new != old:
                changed += 1
                echo(f"  {name}: {key}: {old!r} -> {new!r}")
            return f"<!--chain:{key}-->{new}<!--/chain-->"

        new_text = _TOKEN_RE.sub(_sub, text)
        if new_text != text:
            with open(path, "w") as f:
                f.write(new_text)
    echo(f"render complete: {changed} value(s) updated"
         if changed else "render complete: docs already match live state")
    return changed


# ----------------------------------------------------------------------------
# self-test (fixtures + the real docs; temp-only)
# ----------------------------------------------------------------------------
def _selftest() -> int:
    import hashlib

    print("=== protocol/doc_verify.py self-test (fixtures + real docs; "
          "read-only) ===")
    print("Fixture docs with a wrong number, a failing command, and a stale idx")
    print("reference must each produce a NAMED finding; the real four docs must")
    print("check clean (every command executed in a fresh-clone sandbox). The")
    print("ledger is read at most (never written).\n")

    root_before = set(os.listdir(_REPO_ROOT))
    proto_before = set(os.listdir(_PROTO_DIR))
    ledger_path = os.path.join(_PROTO_DIR, "ledger_data.jsonl")
    ledger_sha_before = None
    if os.path.exists(ledger_path):
        with open(ledger_path, "rb") as f:
            ledger_sha_before = hashlib.sha256(f.read()).hexdigest()

    checks = []
    quiet = lambda *a, **k: None
    tokens_now = compute_tokens()

    with tempfile.TemporaryDirectory() as tmp:
        fixture_dir = os.path.join(tmp, "docs")
        os.makedirs(fixture_dir)
        sandbox = os.path.join(tmp, "sandbox")
        os.makedirs(sandbox)

        def _write_fixture(body):
            # Fixtures reuse the PARTICIPATE.md slot; the other three docs
            # missing yields 3 'missing' findings we filter out below.
            with open(os.path.join(fixture_dir, "PARTICIPATE.md"), "w") as f:
                f.write(f"contract: {CONTRACT_MARKER}\n\n{body}\n")

        def _findings(body, execute=True):
            _write_fixture(body)
            found, _ = check_docs(fixture_dir, _REPO_ROOT, execute=execute,
                                  sandbox_dir=sandbox, echo=quiet)
            return [f for f in found if ": missing from" not in f]

        # [1] wrong number token -> named stale finding
        wrong = _findings("chain has <!--chain:entry_count-->9999<!--/chain--> "
                          "entries.")
        checks.append(("wrong chain token yields a named stale-doc finding",
                       len(wrong) == 1 and "entry_count" in wrong[0]
                       and "stale" in wrong[0]))

        # [2] correct token (computed live) -> clean
        ok_body = (f"chain has <!--chain:entry_count-->"
                   f"{tokens_now['entry_count']}<!--/chain--> entries.")
        checks.append(("live-correct chain token checks clean",
                       _findings(ok_body) == []))

        # [3] unknown token key -> named finding
        unk = _findings("<!--chain:not_a_key-->1<!--/chain-->")
        checks.append(("unknown token key yields a named finding",
                       len(unk) == 1 and "not_a_key" in unk[0]))

        # [4] failing command block -> named finding with exit code
        fail_body = ("```verify-run\n$ python3 -c \"import sys; sys.exit(3)\"\n"
                     "output\n```\n")
        fail = _findings(fail_body)
        checks.append(("failing command block yields a named exit-code finding",
                       len(fail) == 1 and "exited 3" in fail[0]))

        # [5] passing command with missing expect-substring -> named finding
        expect_body = ("```verify-run\n$ python3 -c \"print('hello')\"\n"
                       "hello\n```\n<!--expect:goodbye-->\n")
        exp = _findings(expect_body)
        checks.append(("missing expect-substring yields a named finding",
                       len(exp) == 1 and "goodbye" in exp[0]))

        # [6] passing command with satisfied expect -> clean
        good_cmd = ("```verify-run\n$ python3 -c \"print('hello')\"\nhello\n"
                    "```\n<!--expect:hello-->\n")
        checks.append(("satisfied command block checks clean",
                       _findings(good_cmd) == []))

        # [7] stale idx reference -> named finding; typed mismatch -> named
        stale = _findings("see idx 9999 for details.")
        checks.append(("stale idx reference yields a named finding",
                       len(stale) == 1 and "9999" in stale[0]))
        mismatch = _findings("<!--idx:20=wrong_event_name-->")
        checks.append(("typed idx reference with wrong event yields a named "
                       "finding", len(mismatch) == 1
                       and "metering_evidence_anchored" in mismatch[0]))
        typed_ok = _findings("anchored at idx 20 "
                             "<!--idx:20=metering_evidence_anchored-->.")
        checks.append(("existing idx + correct typed reference check clean",
                       typed_ok == []))

        # [8] missing contract line -> named finding
        with open(os.path.join(fixture_dir, "PARTICIPATE.md"), "w") as f:
            f.write("no contract here\n")
        found, _ = check_docs(fixture_dir, _REPO_ROOT, execute=False,
                              sandbox_dir=sandbox, echo=quiet)
        checks.append(("missing DOC CONTRACT line yields a named finding",
                       any("DOC CONTRACT" in x for x in found)))

        # [9] --render round-trip: stale fixture value is rewritten to live
        _write_fixture("chain has <!--chain:entry_count-->1<!--/chain--> "
                       "entries.")
        # render only touches DOC_FILES in the given dir
        changed = render_docs(fixture_dir, _REPO_ROOT, echo=quiet)
        with open(os.path.join(fixture_dir, "PARTICIPATE.md")) as f:
            rendered = f.read()
        checks.append(("--render rewrites a stale token value to live state",
                       changed == 1
                       and f"<!--chain:entry_count-->{tokens_now['entry_count']}"
                           f"<!--/chain-->" in rendered))

        # [9b] ERA-PINNED README fixtures: the pin declares the as-of point;
        # every era token is checked against the chain AT that point.
        entries_now = _read_ledger(resolve_ledger_path())
        n_now = len(entries_now)

        def _readme_findings(body):
            p = os.path.join(tmp, "README_fixture.md")
            with open(p, "w") as f:
                f.write(body + "\n")
            found, st = check_readme(p, entries=entries_now)
            return found, st

        era_now = compute_era_tokens(entries_now, n_now)
        pin_now = (f"<!--era-pin:entry_count={n_now} "
                   f"tip_hash_prefix={entries_now[-1]['hash'][:12]}-->")
        ok_body = (f"{pin_now}\nchain has <!--era:entry_count-->{n_now}"
                   f"<!--/era--> entries.")
        f_ok, st_ok = _readme_findings(ok_body)
        checks.append(("era-pinned README: correct era checks green",
                       f_ok == [] and st_ok["pinned"]
                       and st_ok["era_tokens"] == 1))

        wrong_body = (f"{pin_now}\nchain has <!--era:entry_count-->9999"
                      f"<!--/era--> entries.")
        f_wrong, _ = _readme_findings(wrong_body)
        checks.append(("era-pinned README: wrong number AT the era is red",
                       len(f_wrong) == 1 and "9999" in f_wrong[0]
                       and "pinned era" in f_wrong[0]))

        # live-tip drift immunity: pin an OLD era (half the chain); the
        # token states the OLD entry_count and stays green even though the
        # live tip has moved far past it — generation-lock semantics
        old_n = max(1, n_now // 2)
        old_era = compute_era_tokens(entries_now, old_n)
        old_body = (f"<!--era-pin:entry_count={old_n} tip_hash_prefix="
                    f"{entries_now[old_n - 1]['hash'][:12]}-->\n"
                    f"chain had <!--era:entry_count-->{old_n}<!--/era--> "
                    f"entries and <!--era:drill_entry_count-->"
                    f"{old_era['drill_entry_count']}<!--/era--> drills.")
        f_old, st_old = _readme_findings(old_body)
        checks.append(("era-pinned README: live-tip drift does NOT redden "
                       "an old-era claim (generation-lock semantics)",
                       f_old == [] and st_old["pinned"] and old_n < n_now))

        # a misstated era (wrong pinned tip hash) is red at the PIN itself
        bad_pin = (f"<!--era-pin:entry_count={n_now} "
                   f"tip_hash_prefix={'0' * 12}-->\nx")
        f_badpin, _ = _readme_findings(bad_pin)
        checks.append(("era-pinned README: a misstated era pin is red by "
                       "name",
                       len(f_badpin) == 1 and "misstated" in f_badpin[0]))

        # era tokens without any pin -> red (numbers must hang off a
        # declared as-of point); no pin and no tokens -> named non-finding
        f_nopin, _ = _readme_findings(
            "chain has <!--era:entry_count-->1<!--/era--> entries.")
        f_plain, st_plain = _readme_findings("just prose, cites idx 0.")
        checks.append(("era tokens without a pin are red; a pinless plain "
                       "README is a named non-finding",
                       len(f_nopin) == 1 and "no era-pin" in f_nopin[0]
                       and f_plain == [] and st_plain["pinned"] is False))

        # [9c] SUPERSEDED MIPs: a later MIP declaring Supersedes retires the
        # earlier one's verify-run blocks from execution (an anchored file
        # may assert its era's state; the successor retires the assertion
        # without editing the immutable file) — citations stay checked
        fx_mip = os.path.join(tmp, "mip")
        os.makedirs(fx_mip)
        with open(os.path.join(fx_mip, "MIP-9001-old.md"), "w") as f:
            f.write("# MIP-9001 — Old era assertion\n"
                    "**Status:** Accepted · **Layer:** Protocol · "
                    "**Supersedes:** none\n\n"
                    "```verify-run\n$ python3 -c \"import sys; "
                    "sys.exit(3)\"\nx\n```\n")
        with open(os.path.join(fx_mip, "MIP-9002-new.md"), "w") as f:
            f.write("# MIP-9002 — Successor\n"
                    "**Status:** Accepted · **Layer:** Protocol · "
                    "**Supersedes:** MIP-9001\n\nprose.\n")
        _write_fixture("contract body")  # a clean docs fixture alongside
        sup_found, sup_stats = check_docs(fixture_dir, _REPO_ROOT,
                                          execute=True, sandbox_dir=sandbox,
                                          echo=quiet, mip_dir=fx_mip)
        sup_found = [x for x in sup_found if ": missing from" not in x]
        checks.append(("superseded MIP's failing verify-run block is "
                       "retired from execution (file stays immutable)",
                       sup_found == [] and sup_stats["mips"] == 2
                       and sup_stats["commands"] == 0))

    # [10] the real four docs check clean — commands executed for real in a
    # fresh-clone sandbox (this IS the doc contract being enforced).
    print("checking the real docs + MIPs (executes every verify-run block in "
          "a fresh-clone sandbox — takes a few minutes)...")
    real_findings, stats = check_docs(echo=print, mip_dir=MIP_DIR)
    for f in real_findings:
        print(f"    FINDING: {f}")
    checks.append((f"the real docs + MIPs check clean ({stats['docs']} docs, "
                   f"{stats['mips']} MIPs, {stats['tokens']} tokens, "
                   f"{stats['idx_refs']} idx refs, {stats['commands']} "
                   "commands)", real_findings == [] and stats["docs"] == len(DOC_FILES)
                   and stats["mips"] >= 3))

    # [10b] the real README checks clean under era-pin semantics (pinless
    # pre-batch READMEs are a named non-finding; once pinned, every era
    # token must match the chain at the declared as-of point)
    rn_findings, rn_stats = check_readme()
    t_findings, t_stats = check_readme(TOUR_PATH)
    rn_findings = rn_findings + t_findings
    for f in rn_findings:
        print(f"    FINDING: {f}")
    checks.append(("the real README + TOUR check clean (era-pinned: "
                   f"{rn_stats['pinned']}/{t_stats['pinned']}, "
                   f"{rn_stats['era_tokens']}+{t_stats['era_tokens']} era "
                   f"tokens)",
                   rn_findings == [] and t_stats["pinned"]))

    # [10c] IDENTITY FREEZE: the real README header + hero carry only the
    # operator-owned strings; a one-word drift in the header is red BY NAME.
    from protocol import identity_text as _T
    id_findings, id_stats = check_identity()
    for f in id_findings:
        print(f"    FINDING: {f}")
    checks.append(("identity freeze: README header + hero text equal "
                   f"protocol/identity_text.py ({id_stats['header_strings']} "
                   f"header strings, {id_stats['hero_nodes']} hero nodes)",
                   id_findings == [] and id_stats["hero_nodes"] >= 3))
    with tempfile.TemporaryDirectory() as tmp_id:
        drift = os.path.join(tmp_id, "README.md")
        with open(README_PATH, encoding="utf-8") as f:
            body = f.read()
        with open(drift, "w", encoding="utf-8") as f:
            f.write(body.replace("primitives of the space economy",
                                 "primitives of the space economies", 1))
        d_findings, _ = check_identity(drift, hero_path=os.path.join(
            _REPO_ROOT, "assets", "hero.svg"))
        checks.append(("identity freeze: a one-word header drift is red by "
                       "name", any("not a frozen identity string" in x
                                   for x in d_findings)))

    # Zero-write guarantees.
    ledger_sha_after = None
    if os.path.exists(ledger_path):
        with open(ledger_path, "rb") as f:
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


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="documentation anti-rot machine: every doc command "
                    "executed, every stated number chain-checked")
    parser.add_argument("--check", action="store_true",
                        help="CI mode: verify all docs, named findings, "
                             "non-zero on any")
    parser.add_argument("--no-exec", action="store_true",
                        help="with --check: skip command execution (tokens and "
                             "idx references only)")
    parser.add_argument("--render", action="store_true",
                        help="re-render chain tokens from live state "
                             "(deliberate human step after chain growth)")
    parser.add_argument("--selftest", action="store_true",
                        help="run the fixture self-test (writes nothing)")
    args = parser.parse_args(argv)

    if args.selftest:
        return _selftest()
    if args.render:
        render_docs()
        return 0
    if args.check:
        findings, stats = check_docs(execute=not args.no_exec,
                                     mip_dir=MIP_DIR)
        r_findings, r_stats = check_readme()
        findings.extend(r_findings)
        t_findings, t_stats = check_readme(TOUR_PATH)
        findings.extend(t_findings)
        i_findings, i_stats = check_identity()
        findings.extend(i_findings)
        readme_note = (f"era-pinned, {r_stats['era_tokens']} era tokens"
                       if r_stats["pinned"] else "no era pin (not yet opted "
                       "in)")
        readme_note += (f" | TOUR era-pinned, {t_stats['era_tokens']} era "
                        "tokens" if t_stats["pinned"] else " | TOUR unpinned")
        print(f"\ndocs checked: {stats['docs']}/{len(DOC_FILES)} | MIPs "
              f"scanned: {stats['mips']} | README: {readme_note}, "
              f"{r_stats['idx_refs']} idx refs | identity: "
              f"{i_stats['header_strings']} header strings + "
              f"{i_stats['hero_nodes']} hero text nodes frozen"
              f"{' (DRIFT)' if i_findings else ' (match)'} | chain "
              f"tokens: {stats['tokens']} | idx references: "
              f"{stats['idx_refs']} | command blocks: {stats['commands']}")
        if findings:
            print(f"DOC-VERIFY: {len(findings)} finding(s):")
            for f in findings:
                print(f"  - {f}")
            return 1
        print("DOC-VERIFY: CLEAN — every command executed, every number "
              "matches live state, every idx reference resolves")
        return 0
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
