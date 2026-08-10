"""mip_process.py — the MIP lifecycle machine (schema "mip-record/0.1").

================== HONESTY / SCOPE (READ ME) ==================
Research-stage, ZERO-VALUE, no token, no money, no mainnet, no payments.

MetaCoin's improvement proposals live in mip/ as prose documents. The
lifecycle the existing files document is minimal — a `**Status:** Draft`
header and MIP-0001 §7's promise that changes happen "only by transparent
on-chain MIPs". This module completes that lifecycle by its MINIMAL HONEST
reading and makes each stage mechanical:

    Draft -> mechanical check (--check) -> single-seat decision
          -> anchored decision record (--record-decision --confirm)

THE SEAT STATEMENT (the Gate-3 vacancy idiom): the review seat has ONE
occupant and says so on every anchored record. The mechanical checks are
real — required sections, a valid status, every ledger citation resolved
against the chain, every verification command executed clean, the file's
sha256 — but plural review, voting, and the anti-whale dampening MIP-0001
§7 promises do not exist at research stage. A recorded decision proves the
PROCESS ran, not that the decision is wise.

IMMUTABILITY-BY-CITATION: the anchored record pins the decided file's
sha256. From that moment the committed MIP file is immutable — any later
edit breaks re-derivation loudly (verify_everything's governance layer and
the routine sweep both recompute the hash), and that breakage is correct:
amendments are new MIPs, never edits to anchored ones.

NO AUTO-ANCHORING: --record-decision writes NOTHING without --confirm (the
same human gate, for the same reason, as participant intake — a
validation tool that auto-anchored would let any file spam the ledger).

REUSE, NOT REIMPLEMENTATION: ledger citations, chain-token detection, and
verification-block parsing/execution reuse protocol/doc_verify.py's
machinery verbatim; ledger writes go through protocol/ledger.Ledger like
every other anchor. Standard library only. Not legal or financial advice.

Usage:
    python3 protocol/mip_process.py --check mip/MIP-0004-concentration-epochs.md
    python3 protocol/mip_process.py --record-decision <file> --status accepted            # dry-run
    python3 protocol/mip_process.py --record-decision <file> --status accepted --confirm  # anchors
    python3 protocol/mip_process.py --selftest
"""

# Suppress __pycache__/*.pyc so importing protocol modules leaves no stray files.
import sys
sys.dont_write_bytecode = True

import argparse
import hashlib
import json
import os
import re
import tempfile
import time

_PROTO_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_PROTO_DIR)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# REUSE: doc_verify's citation/command machinery, ledger resolution + writer.
import protocol.doc_verify as doc_verify
from protocol.ledger import Ledger
from protocol.prov_export import resolve_ledger_path
from protocol.work_molecule import _read_ledger

RECORD_SCHEMA = "mip-record/0.1"
_MIP_EVENT = "mip_decision_recorded"

# The lifecycle statuses a MIP file may declare. "Draft" is the only stage
# the pre-existing MIP files document; Accepted/Rejected are the minimal
# honest completion (the anchored decision endpoint MIP-0001 §7 promises).
VALID_STATUSES = ("Draft", "Accepted", "Rejected")
DECISIONS = ("accepted", "rejected")

# Required sections for a MIP walked through this process (the template this
# first exercise sets; pre-process drafts simply have not been walked yet).
REQUIRED_SECTIONS = ("Summary", "Motivation", "Specification",
                     "Backwards compatibility", "Honest limitations",
                     "Verification")

REVIEW_SEAT = "same-operator-single-seat"
SEAT_STATEMENT = (
    "the review seat has one occupant and says so — plural review, voting, "
    "and anti-whale dampening (MIP-0001 §7) do not exist at research stage; "
    "this record proves the process ran, not that the decision is wise"
)
AMENDMENT_RULE = (
    "the anchored record pins the decided file's sha256: the committed MIP "
    "file is immutable-by-citation from this moment, a later edit breaks "
    "re-derivation loudly, and amendments are new MIPs"
)
MIP_LIMITATION_NOTE = (
    "First-exercise MIP lifecycle under same-operator custody: the "
    "mechanical checks are real (required sections, valid status, ledger "
    "citations resolved against the chain, verification blocks executed, "
    "file sha256 pinned) and the decision is recorded on-chain — but the "
    "review seat has ONE occupant and says so. A recorded decision proves "
    "the PROCESS ran, not that the decision is wise; plural review and "
    "voting do not exist yet. Anchored MIP files are immutable-by-citation; "
    "amendments are new MIPs. Not consensus, not mainnet, not payment, not "
    "a token; zero-value research-stage."
)

_ID_RE = re.compile(r"MIP-(\d{4})")
_HEADING_RE = re.compile(r"^# (MIP-\d{4}) — (.+)$", re.M)
_STATUS_RE = re.compile(r"\*\*Status:\*\* ([A-Za-z]+)")


def _sha256_file(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def check_mip(path: str, ledger_source: str = None, execute: bool = True,
              sandbox_dir: str = None, echo=print) -> dict:
    """Run every mechanical check on a MIP file. Returns the verdict dict
    (no writes, ever): {mip_id, title, file, file_sha256, status, passed,
    checks: [{name, passed, detail}], idx_references, verify_run_blocks}.

    `execute=False` skips verification-block execution (the cheap
    re-derivation mode verify_everything uses; CI executes them via
    doc_verify's mip/ scan and --check's default). `sandbox_dir` (self-test
    fixtures) runs blocks in the given directory instead of a fresh clone.
    """
    checks = []
    text = None
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError as exc:
        return {"mip_id": None, "title": None, "file": path,
                "file_sha256": None, "status": None, "passed": False,
                "checks": [{"name": "file-readable", "passed": False,
                            "detail": str(exc)}],
                "idx_references": 0, "verify_run_blocks": 0}

    rel = os.path.relpath(os.path.abspath(path), _REPO_ROOT)
    file_sha = _sha256_file(path)

    # [1] identity: filename number == heading number; a title exists
    m_head = _HEADING_RE.search(text)
    m_name = _ID_RE.search(os.path.basename(path))
    mip_id = m_head.group(1) if m_head else None
    title = m_head.group(2).strip() if m_head else None
    checks.append({
        "name": "identity",
        "passed": bool(m_head and m_name
                       and m_head.group(1) == f"MIP-{m_name.group(1)}"),
        "detail": (f"{mip_id} — {title}" if m_head else
                   "no '# MIP-NNNN — Title' heading"),
    })

    # [2] status declared and valid
    m_status = _STATUS_RE.search(text)
    status = m_status.group(1) if m_status else None
    checks.append({
        "name": "status-valid",
        "passed": status in VALID_STATUSES,
        "detail": (f"status {status!r}" if status else
                   "no '**Status:** X' header line"),
    })

    # [3] required sections present (## headings)
    section_heads = {h.strip().lower()
                     for h in re.findall(r"^## (.+)$", text, re.M)}
    missing = [s for s in REQUIRED_SECTIONS
               if s.lower() not in section_heads]
    checks.append({
        "name": "required-sections",
        "passed": not missing,
        "detail": ("all present" if not missing else f"missing: {missing}"),
    })

    # [4] no chain tokens: an anchored MIP file is immutable-by-citation, and
    # a live-recomputed number inside an immutable file rots by design — MIPs
    # cite typed idx references (stable forever) instead
    tokens = doc_verify._TOKEN_RE.findall(text)
    checks.append({
        "name": "no-chain-tokens",
        "passed": not tokens,
        "detail": ("none (correct: immutable files must not embed "
                   "live-recomputed numbers)" if not tokens else
                   f"chain tokens present: {[k for k, _v in tokens]}"),
    })

    # [5] every ledger citation resolves (doc_verify's idx machinery, reused)
    entries = _read_ledger(ledger_source if ledger_source is not None
                           else resolve_ledger_path())
    idx_findings = []
    doc_verify._check_idx_refs(text, rel, entries, idx_findings)
    n_refs = (len(doc_verify._IDX_PROSE_RE.findall(text))
              + len(doc_verify._IDX_TYPED_RE.findall(text)))
    checks.append({
        "name": "ledger-citations-resolve",
        "passed": not idx_findings,
        "detail": (f"{n_refs} reference(s) resolve" if not idx_findings
                   else "; ".join(idx_findings)[:300]),
    })

    # [6] verification blocks present and (by default) executed clean in a
    # fresh-clone sandbox — the doc_verify command machinery, reused
    blocks = doc_verify._parse_command_blocks(text)
    checks.append({
        "name": "verification-blocks-present",
        "passed": bool(blocks),
        "detail": f"{len(blocks)} verify-run block(s)",
    })
    if blocks and execute:
        block_findings = []
        doc_blocks = [(rel, cmd, expects) for cmd, expects in blocks]
        if sandbox_dir is not None:
            doc_verify._run_command_blocks(doc_blocks, sandbox_dir,
                                           block_findings, echo)
        else:
            with tempfile.TemporaryDirectory() as tmp:
                sandbox, reason = doc_verify._make_sandbox(_REPO_ROOT, tmp)
                if sandbox is None:
                    block_findings.append(f"sandbox unavailable: {reason}")
                else:
                    doc_verify._run_command_blocks(doc_blocks, sandbox,
                                                   block_findings, echo)
        checks.append({
            "name": "verification-blocks-execute",
            "passed": not block_findings,
            "detail": ("all clean" if not block_findings
                       else "; ".join(block_findings)[:300]),
        })

    return {
        "mip_id": mip_id,
        "title": title,
        "file": rel,
        "file_sha256": file_sha,
        "status": status,
        "passed": all(c["passed"] for c in checks),
        "checks": checks,
        "idx_references": n_refs,
        "verify_run_blocks": len(blocks),
    }


def build_decision_record(verdict: dict, decision: str) -> dict:
    """The mip_decision_recorded payload for a checked file + a decision.
    Pure function; scanner-invisible keys (no task_id/task_ids anywhere)."""
    if decision not in DECISIONS:
        raise ValueError(f"decision must be one of {DECISIONS} "
                         f"(got {decision!r})")
    if decision == "accepted":
        if not verdict["passed"]:
            failed = [c["name"] for c in verdict["checks"]
                      if not c["passed"]]
            raise ValueError("refused: an ACCEPTANCE requires every "
                             f"mechanical check to pass (failing: {failed}) "
                             "— record a rejection instead, or fix the file")
        if verdict["status"] != "Accepted":
            raise ValueError(
                f"refused: the file's Status header says "
                f"{verdict['status']!r} but the decision is 'accepted' — "
                "the record pins the post-decision file, so the file must "
                "declare the state being anchored")
    return {
        "event": _MIP_EVENT,
        "record_schema": RECORD_SCHEMA,
        "stage": "R-governance",
        "topology": "same-operator-single-seat-review",
        "status": f"mip-{decision}",
        "decision": decision,
        "mip_id": verdict["mip_id"],
        "mip_title": verdict["title"],
        "file": verdict["file"],
        "file_sha256": verdict["file_sha256"],
        "mechanical_check": {
            "passed": verdict["passed"],
            "checks": [dict(c) for c in verdict["checks"]],
            "idx_references": verdict["idx_references"],
            "verify_run_blocks": verdict["verify_run_blocks"],
        },
        "review_seat": REVIEW_SEAT,
        "seat_statement": SEAT_STATEMENT,
        "amendment_rule": AMENDMENT_RULE,
        "operator_relationship": "same-operator",
        "limitation_note": MIP_LIMITATION_NOTE,
        "zero_value": True,
        "no_token": True,
        "anchored_at": time.time(),
    }


def record_decision(path: str, decision: str, confirm: bool,
                    ledger_path: str = None, echo=print) -> dict:
    """Check the file, build the decision record, and — ONLY with
    confirm=True — anchor it. Returns {verdict, record, ledger_entry}.
    Without confirm, the would-be record is shown and NOTHING is written."""
    ledger_path = (ledger_path if ledger_path is not None
                   else os.path.join(_PROTO_DIR, "ledger_data.jsonl"))
    verdict = check_mip(path, ledger_source=ledger_path, echo=echo)
    record = build_decision_record(verdict, decision)  # raises on refusal
    if not confirm:
        return {"verdict": verdict, "record": record, "ledger_entry": None}
    entry = Ledger(ledger_path).append(record)
    return {"verdict": verdict, "record": record, "ledger_entry": entry}


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
def _print_verdict(verdict: dict):
    print(f"mip   : {verdict['mip_id']} — {verdict['title']}")
    print(f"file  : {verdict['file']}")
    print(f"sha256: {verdict['file_sha256']}")
    print(f"status: {verdict['status']}")
    for c in verdict["checks"]:
        print(f"  {'PASS' if c['passed'] else 'FAIL'}  {c['name']:28s} "
              f"{c['detail']}")
    print("MIP-CHECK: " + ("CLEAN — every mechanical check passed"
                           if verdict["passed"] else
                           "FINDINGS — see the failing checks above"))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="mip_process.py",
        description=(
            "MIP lifecycle machine (research-stage, ZERO-VALUE, no token): "
            "mechanical checks on a MIP file, and the single-seat decision "
            "record — honestly labeled, never auto-anchored."
        ),
        epilog=(
            "THE SEAT STATEMENT: the review seat has one occupant and says "
            "so. A recorded decision proves the process ran, not that the "
            "decision is wise. Anchored MIP files are immutable-by-citation; "
            "amendments are new MIPs. Not consensus, not payment, not a "
            "token."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", metavar="MIP_MD",
                      help="run every mechanical check on a MIP file "
                           "(verdict only; writes nothing)")
    mode.add_argument("--record-decision", metavar="MIP_MD",
                      help="check the file and record a lifecycle decision; "
                           "WRITES NOTHING without --confirm")
    mode.add_argument("--selftest", action="store_true",
                      help="run the fixture self-test (temp files only; "
                           "default with no args)")
    parser.add_argument("--status", choices=DECISIONS,
                        help="with --record-decision: the decision to record")
    parser.add_argument("--confirm", action="store_true",
                        help="with --record-decision: the HUMAN gate — "
                             "actually anchor the shown record")
    parser.add_argument("--ledger",
                        default=os.path.join(_PROTO_DIR, "ledger_data.jsonl"),
                        help="ledger path (default: the real persistent "
                             "ledger)")
    args = parser.parse_args(argv)

    if args.check is not None:
        verdict = check_mip(args.check)
        _print_verdict(verdict)
        return 0 if verdict["passed"] else 1

    if args.record_decision is not None:
        if not args.status:
            parser.error("--record-decision requires --status "
                         "accepted|rejected")
        try:
            out = record_decision(args.record_decision, args.status,
                                  args.confirm, ledger_path=args.ledger)
        except (ValueError, OSError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        _print_verdict(out["verdict"])
        print(f"decision      : {out['record']['decision']} "
              f"(review seat: {out['record']['review_seat']})")
        print(f"seat statement: {out['record']['seat_statement']}")
        if out["ledger_entry"] is None:
            print("anchored: NO — dry run (re-run with --confirm to anchor "
                  "exactly the record above)")
            return 0
        print(f"anchored at ledger index: {out['ledger_entry']['index']} "
              f"(path: {args.ledger})")
        ok, reason = Ledger(args.ledger).verify_chain()
        print(f"chain verify: {'OK' if ok else 'FAIL'} — {reason}")
        return 0 if ok else 1

    return _selftest()


# ============================== SELF-TEST ====================================
def _selftest() -> int:
    """Fixture self-test: check pass/fail paths, the no-write-without-confirm
    gate, anchoring, and the real MIP file. Temp files only."""
    import shutil

    print("=== protocol/mip_process.py self-test (temp files only) ===")
    print("Mechanical checks, the single-seat decision gate, and anchoring —")
    print("the review seat has one occupant and every record says so.\n")

    root_before = set(os.listdir(_REPO_ROOT))
    proto_before = set(os.listdir(_PROTO_DIR))
    checks = []
    quiet = lambda *a, **k: None
    tmp = tempfile.mkdtemp(prefix=f"mip_selftest_{os.getpid()}_")
    try:
        # fixture ledger: genesis only (citations resolve against idx 0)
        fixture_ledger = os.path.join(tmp, "ledger_fixture.jsonl")
        led = Ledger(fixture_ledger)
        led.append({"event": "ledger_genesis", "note": "selftest fixture",
                    "stage": "R-selftest", "zero_value": True,
                    "no_token": True})
        sandbox = os.path.join(tmp, "sandbox")
        os.makedirs(sandbox)

        def _fixture(name="MIP-9999-fixture.md", status="Accepted",
                     sections=REQUIRED_SECTIONS, idx_line="cites idx 0 "
                     "<!--idx:0=ledger_genesis-->.",
                     block=("```verify-run\n$ python3 -c \"print('ok')\"\n"
                            "ok\n```\n<!--expect:ok-->\n"),
                     extra=""):
            num = _ID_RE.search(name).group(1)
            body = [f"# MIP-{num} — Fixture proposal",
                    f"**Status:** {status} · **Layer:** Protocol", ""]
            for s in sections:
                body.append(f"## {s}")
                body.append(f"{idx_line}" if s == "Motivation" else "prose.")
            body.append(block)
            body.append(extra)
            path = os.path.join(tmp, name)
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(body) + "\n")
            return path

        # [1] a complete fixture checks CLEAN (blocks run in the fixture
        # sandbox; citations resolve on the fixture ledger)
        good = _fixture()
        v = check_mip(good, ledger_source=fixture_ledger,
                      sandbox_dir=sandbox, echo=quiet)
        checks.append(("complete fixture checks clean",
                       v["passed"] and v["mip_id"] == "MIP-9999"
                       and v["verify_run_blocks"] == 1))

        # [2] named failures: missing section / bad status / stale idx /
        # failing verify-run / chain token present
        def _failing(check_name, **kw):
            p = _fixture(**kw)
            vv = check_mip(p, ledger_source=fixture_ledger,
                           sandbox_dir=sandbox, echo=quiet)
            failed = {c["name"] for c in vv["checks"] if not c["passed"]}
            return (not vv["passed"]) and check_name in failed
        checks.append(("missing section fails by name",
                       _failing("required-sections",
                                name="MIP-9101-nosec.md",
                                sections=REQUIRED_SECTIONS[:-1])))
        checks.append(("invalid status fails by name",
                       _failing("status-valid", name="MIP-9102-badstatus.md",
                                status="Ratified")))
        checks.append(("stale ledger citation fails by name",
                       _failing("ledger-citations-resolve",
                                name="MIP-9103-staleidx.md",
                                idx_line="cites idx 9999.")))
        checks.append(("failing verify-run block fails by name",
                       _failing("verification-blocks-execute",
                                name="MIP-9104-badblock.md",
                                block="```verify-run\n$ python3 -c \""
                                      "import sys; sys.exit(3)\"\nx\n```\n")))
        checks.append(("chain token in a MIP fails by name (immutable files "
                       "must not embed live numbers)",
                       _failing("no-chain-tokens", name="MIP-9105-token.md",
                                extra="<!--chain:entry_count-->1"
                                      "<!--/chain-->")))

        # [3] NO WRITE WITHOUT CONFIRM: a dry-run decision returns the
        # would-be record and the ledger is byte-identical after
        sha_before = _sha256_file(fixture_ledger)
        out_dry = None
        real_check = check_mip  # keep names readable

        def _record(path, decision, confirm):
            # route the record path through the fixture sandbox for blocks
            verdict = real_check(path, ledger_source=fixture_ledger,
                                 sandbox_dir=sandbox, echo=quiet)
            record = build_decision_record(verdict, decision)
            entry = (Ledger(fixture_ledger).append(record) if confirm
                     else None)
            return {"verdict": verdict, "record": record,
                    "ledger_entry": entry}

        out_dry = _record(good, "accepted", confirm=False)
        checks.append(("no write without --confirm (ledger byte-identical; "
                       "record shown)",
                       out_dry["ledger_entry"] is None
                       and _sha256_file(fixture_ledger) == sha_before
                       and out_dry["record"]["event"] == _MIP_EVENT))

        # [4] confirmed acceptance anchors; envelope honest + scanner-
        # invisible; chain verifies
        out_acc = _record(good, "accepted", confirm=True)
        rec = out_acc["record"]
        chain_ok, _r = Ledger(fixture_ledger).verify_chain()
        checks.append(("confirmed acceptance anchors (seat statement, "
                       "amendment rule, scanner-invisible; chain OK)",
                       out_acc["ledger_entry"] is not None
                       and rec["status"] == "mip-accepted"
                       and rec["review_seat"] == REVIEW_SEAT
                       and "one occupant" in rec["seat_statement"]
                       and "new MIPs" in rec["amendment_rule"]
                       and "task_id" not in rec and "task_ids" not in rec
                       and rec["file_sha256"] == _sha256_file(good)
                       and chain_ok is True))

        # [5] acceptance REFUSED on a failing file (named reason) and on a
        # Draft-status file (the record pins the post-decision file);
        # rejection of a failing file IS recordable (with results carried)
        bad = _fixture(name="MIP-9998-bad.md", sections=REQUIRED_SECTIONS[:-1])
        try:
            _record(bad, "accepted", confirm=False)
            checks.append(("acceptance refused when checks fail", False))
        except ValueError as exc:
            checks.append(("acceptance refused when checks fail",
                           "requires every mechanical check" in str(exc)))
        draft = _fixture(name="MIP-9997-draft.md", status="Draft")
        try:
            _record(draft, "accepted", confirm=False)
            checks.append(("acceptance refused while the file still says "
                           "Draft", False))
        except ValueError as exc:
            checks.append(("acceptance refused while the file still says "
                           "Draft", "must declare the state" in str(exc)))
        out_rej = _record(bad, "rejected", confirm=True)
        checks.append(("rejection of a failing file records honestly "
                       "(check results carried)",
                       out_rej["record"]["status"] == "mip-rejected"
                       and out_rej["record"]["mechanical_check"]["passed"]
                       is False))

        # [6] the REAL MIP file checks clean structurally (blocks not
        # executed here — doc_verify's mip/ scan and CI execute them; this
        # keeps the selftest fast and the responsibilities separated)
        real_mip = os.path.join(_REPO_ROOT, "mip",
                                "MIP-0004-concentration-epochs.md")
        v_real = check_mip(real_mip, execute=False, echo=quiet)
        checks.append(("the real MIP-0004 passes every structural check "
                       "(sections, status, citations, hash)",
                       v_real["passed"] and v_real["mip_id"] == "MIP-0004"
                       and v_real["verify_run_blocks"] >= 1))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    stray_root = sorted(set(os.listdir(_REPO_ROOT)) - root_before)
    stray_proto = sorted(set(os.listdir(_PROTO_DIR)) - proto_before)
    checks.append(("no stray files in repo root", not stray_root))
    checks.append(("no stray files in protocol/", not stray_proto))

    print("--- self-test invariants ---")
    failures = 0
    for name, passed in checks:
        print(f"{name:68s}: {'PASS' if passed else 'FAIL'}")
        if not passed:
            failures += 1
    if stray_root:
        print(f"    stray in repo root: {stray_root}")

    ok = failures == 0
    print("\n=== self-test summary: " +
          ("ALL CHECKS BEHAVED CORRECTLY" if ok else "FAILURE — see above")
          + " ===")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
