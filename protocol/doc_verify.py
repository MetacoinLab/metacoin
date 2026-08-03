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

WHAT THIS MODULE NEVER DOES: it never writes to the ledger (read-only against
the live-or-snapshot chain, same resolution as every verifier); --render
rewrites ONLY the VALUE spans between chain markers in docs/ files, nothing
else; --check writes nothing at all outside its temp sandbox.

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
from protocol.work_molecule import _read_ledger, find_evidence_file
from protocol.verifier_cli import TASK_MODULES

DOCS_DIR = os.path.join(_REPO_ROOT, "docs")
# Fixed processing order: multi-step walkthrough state (participate init -> run
# -> bundle) must execute in the order a reader would.
DOC_FILES = ("PARTICIPATE.md", "ARCHITECTURE.md", "VERIFICATION.md",
             "TRUST-MODEL.md")

CONTRACT_MARKER = "mechanically verified by protocol/doc_verify.py"

_TOKEN_RE = re.compile(r"<!--chain:([a-z0-9_]+)-->(.*?)<!--/chain-->", re.S)
_IDX_TYPED_RE = re.compile(r"<!--idx:(\d+)=([a-z0-9_]+)-->")
_IDX_PROSE_RE = re.compile(r"\bidx (\d+)(?:[-–](\d+))?")
_FENCE_RE = re.compile(r"^```verify-run\n\$ (.+?)\n(.*?)^```\n((?:<!--expect:.*?-->\n)*)",
                       re.S | re.M)
_EXPECT_RE = re.compile(r"<!--expect:(.*?)-->")

COMMAND_TIMEOUT_S = 900


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
        "actor_count": str(len({e["payload"]["actor_id"] for e in entries
                                if e.get("payload", {}).get("event")
                                == "actor_key_registered"})),
        "drill_entry_count": str(sum(1 for e in entries
                                     if e.get("payload", {}).get("drill"))),
        "catalog_anchor_count": str(sum(
            1 for e in entries if e.get("payload", {}).get("event")
            == "work_molecule_catalog_anchored")),
        "protocol_suite_count": str(_count_runner_tests(
            os.path.join(repo_root, "protocol", "run_protocol_selftests.sh"))),
        "demo_suite_count": str(_count_runner_tests(
            os.path.join(repo_root, "demo", "run_all_selftests.sh"))),
        "aci_pairwise": f"{round(aci['pairwise_aci'], 5)}",
        "aci_path_count": str(aci["path_count"]),
        "aci_pair_count": str(aci["pair_count"]),
        "passport_actor_count": str(len(passports["entries"])),
        "assumed_power_w": f"{metering['assumed_cpu_power_w']}",
    }


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
               sandbox_dir=None, echo=print):
    """Verify every doc construct. Returns (findings list, stats dict).
    `sandbox_dir` (self-test fixtures) skips the git clone and runs command
    blocks in the given directory instead."""
    findings = []
    stats = {"docs": 0, "tokens": 0, "idx_refs": 0, "commands": 0}
    entries = _read_ledger(resolve_ledger_path())
    tokens = compute_tokens(repo_root, entries)

    doc_blocks = []
    for name in DOC_FILES:
        path = os.path.join(docs_dir, name)
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
        path = os.path.join(docs_dir, name)
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

    # [10] the real four docs check clean — commands executed for real in a
    # fresh-clone sandbox (this IS the doc contract being enforced).
    print("checking the real docs (executes every verify-run block in a "
          "fresh-clone sandbox — takes a few minutes)...")
    real_findings, stats = check_docs(echo=print)
    for f in real_findings:
        print(f"    FINDING: {f}")
    checks.append((f"the real four docs check clean ({stats['tokens']} tokens, "
                   f"{stats['idx_refs']} idx refs, {stats['commands']} "
                   "commands)", real_findings == [] and stats["docs"] == 4))

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
        findings, stats = check_docs(execute=not args.no_exec)
        print(f"\ndocs checked: {stats['docs']}/{len(DOC_FILES)} | chain "
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
