# Copyright (c) 2023-2026 MetaCoin-Lab.
# Licensed under SML-1.0 — see LICENSE.md.
"""pulse.py — THE PULSE RECORD: a public, machine-readable weekly health
snapshot of the whole stack, derived live, hashed, and anchored.

================== HONESTY / SCOPE (READ ME) ==================
Research-stage, ZERO-VALUE, no token, no money, no mainnet, no payments.

PURPOSE. A stranger — or a grant reviewer — reads ONE file and knows the
system is alive and every gate is green: evidence, not marketing. The pulse
is derived entirely from the chain and from gate RUNS; nothing in it is
hand-written.

THE RULE THAT MAKES IT EVIDENCE: a pulse that cannot be green honestly is
NOT GENERATED. `--generate` runs the full battery — the routine sweep (every
anchored layer re-derived, both self-test suites, evidence reconciliation,
git hygiene incl. CI status, the release gate, the executed documentation
suite), the task-law checker, the documentation statistics, and the real
cold install (wheel -> fresh venv -> `metacoin verify` in an empty
directory) — and REFUSES, writing nothing, if any gate reports anything but
green. There is no "pulse with findings"; the absence of a fresh pulse is
itself the signal.

NO TIMESTAMPS IN THE HASHED ARTIFACT (the house rule for hashed objects):
pulse.json is a deterministic function of (chain state, repository commit,
gate outcomes). Its date is the anchoring record's own `anchored_at`, the
same way every other record dates itself; mirror freshness is expressed as
chain entries since the last attestation, never as wall-clock days. Two
generations at the same commit and chain point produce the same bytes and
the same `pulse_hash` — which is what `--verify` proves.

CANONICAL FORM. `pulse_hash` = sha256 of the era-2 canonical JSON (sorted
keys, compact separators, ASCII, sign-of-zero-free — ledger idx 67) of the
document WITHOUT its own `pulse_hash` field (the anti-circularity self-hash
the evidence reconciliation already recognizes).

ANCHORING. `protocol/external_verifier.py --anchor-pulse pulse.json
--confirm` recomputes the hash, re-checks every gate field for green, binds
the pulse's chain point to a real prefix of the ledger, and anchors
`pulse_recorded` with the hash and the headline numbers; the file ships as
`protocol/evidence/pulse_<hash12>.json`. The record class enters by
PRECEDENT (passport catalog idx 40, mirror attestation idx 72): protocol
code + a coordinator --confirm path + a verify_everything layer + a sweep
evidence expectation, no MIP required for a record class that ratifies no
new capability. Weekly cadence rides the routine sweep: after a CLEAN sweep,
generate and anchor the pulse; the --confirm stays human.

Usage:
    python3 protocol/pulse.py --generate [--out pulse.json]   # full battery
    python3 protocol/pulse.py --verify pulse.json               # re-derive + compare
    python3 protocol/pulse.py --status                          # latest anchored pulse re-derives?
    python3 protocol/pulse.py --selftest                        # fixtures only
Standard library only. Not financial or legal advice.
"""

import sys
sys.dont_write_bytecode = True

import argparse
import hashlib
import json
import os
import re
import subprocess
import tempfile

_PROTO_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_PROTO_DIR)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from protocol.prov_export import resolve_ledger_path
from protocol.work_molecule import _read_ledger, find_evidence_file

PULSE_SCHEMA = "pulse/0.1"
PULSE_EVENT = "pulse_recorded"
PULSE_STATUS = "pulse-confirmed"
INTEGRATION_DIRS = ("integrations/inspect", "integrations/hal",
                    "integrations/baselines")
_SUITE_RE = re.compile(r"(\d+)/(\d+) passed")

REFUSAL_RULE = ("a pulse that cannot be green honestly is not generated — "
                "the absence of a fresh pulse is itself the signal")


# ----------------------------------------------------------------------------
# canonical form
# ----------------------------------------------------------------------------
def _sign_safe_zero(obj):
    return json.loads(json.dumps(obj),
                      parse_float=lambda t: 0.0 if float(t) == 0.0 else float(t))


def canonical_json(obj) -> str:
    return json.dumps(_sign_safe_zero(obj), sort_keys=True,
                      separators=(",", ":"), ensure_ascii=True)


def pulse_hash(doc: dict) -> str:
    body = {k: v for k, v in doc.items() if k != "pulse_hash"}
    return hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()


# ----------------------------------------------------------------------------
# pure assembly (fixture-testable)
# ----------------------------------------------------------------------------
GATE_KEYS = ("verify_everything", "suites", "task_law", "doc_verify",
             "cold_install", "sweep")


def gates_all_green(gates: dict):
    """(ok, reasons) — the refusal rule, applied to a gates dict."""
    reasons = []
    ve = gates.get("verify_everything", {})
    if not (ve.get("passed") is True and isinstance(ve.get("layers"), int)
            and ve["layers"] > 0):
        reasons.append("verify_everything not ALL LAYERS PASS")
    for name in ("demo", "protocol"):
        s = gates.get("suites", {}).get(name, {})
        if not (isinstance(s.get("passed"), int) and s.get("passed") == s.get("total")
                and s["total"] > 0):
            reasons.append(f"{name} suite not N/N")
    tl = gates.get("task_law", {})
    if not (tl.get("clean") is True and tl.get("violations") == 0):
        reasons.append("task law not CLEAN")
    dv = gates.get("doc_verify", {})
    if not (dv.get("clean") is True and dv.get("findings") == 0):
        reasons.append("doc_verify not CLEAN")
    ci = gates.get("cold_install", {})
    if ci.get("passed") is not True:
        reasons.append("cold install not PASS")
    sw = gates.get("sweep", {})
    if not (sw.get("findings") == 0 and sw.get("verdict") == "SWEEP CLEAN"):
        reasons.append("sweep not CLEAN")
    return (not reasons, reasons)


def corpus_from_chain(entries, task_registry: dict, honest_negatives: int,
                      mip_files: list, superseded: set,
                      integration_pins: dict, evidence_count: int) -> dict:
    recorded = 0
    from protocol.work_molecule import _payload_references_task
    for sid in task_registry:
        if any(_payload_references_task(e.get("payload"), sid) for e in entries):
            recorded += 1
    decisions = [e["payload"] for e in entries
                 if e.get("payload", {}).get("event") == "mip_decision_recorded"]
    return {
        "tasks": {"registered": len(task_registry), "recorded": recorded,
                  "honest_negatives": honest_negatives},
        "mips": {"files": len(mip_files),
                 "decisions": len(decisions),
                 "accepted": sum(1 for d in decisions if d.get("decision") == "accepted"),
                 "retained_as_draft": sum(1 for d in decisions
                                          if d.get("decision") == "retained-as-draft"),
                 "superseded": len(superseded)},
        "integrations": [{"path": p, "commit": integration_pins[p]}
                         for p in INTEGRATION_DIRS],
        "evidence_files": evidence_count,
    }


def participation_from_chain(entries) -> dict:
    tip = entries[-1]["index"]
    actors = {e["payload"].get("actor_id") for e in entries
              if e.get("payload", {}).get("event") == "actor_key_registered"}
    cross = [e for e in entries
             if e.get("payload", {}).get("event") == "participant_result_anchored"
             and e["payload"].get("status") == "participant-verified"]
    rejected = [e for e in entries
                if e.get("payload", {}).get("event") == "participant_intake_rejected"]
    mirrors = [e for e in entries
               if e.get("payload", {}).get("event") == "mirror_attestation_anchored"]
    last_mirror = mirrors[-1]["index"] if mirrors else None
    return {
        "registered_actors": len(actors),
        "participant_verified_records": len(cross),
        "intake_rejections": len(rejected),
        "mirror": {"last_attested_idx": last_mirror,
                   "entries_since_attestation": (tip - last_mirror
                                                 if last_mirror is not None else None)},
        "independence_note": ("every actor on the chain is the same operator; "
                              "independence is measured, not claimed"),
    }


def assemble_pulse(entries, repo_commit: str, gates: dict, corpus: dict,
                   participation: dict) -> dict:
    """The deterministic document. Raises ValueError under the refusal rule."""
    ok, reasons = gates_all_green(gates)
    if not ok:
        raise ValueError("REFUSED (" + REFUSAL_RULE + "): " + "; ".join(reasons))
    tip = entries[-1]
    doc = {
        "schema": PULSE_SCHEMA,
        "chain": {"entries": len(entries), "tip_index": tip["index"],
                  "tip_hash": tip["hash"]},
        "repo": {"commit": repo_commit},
        "gates": {k: gates[k] for k in GATE_KEYS},
        "corpus": corpus,
        "participation": participation,
        "refusal_rule": REFUSAL_RULE,
        "no_timestamps_note": ("hashed artifact: no wall-clock fields; the "
                               "anchoring record's anchored_at dates this pulse"),
        "zero_value": True,
        "no_token": True,
    }
    doc["pulse_hash"] = pulse_hash(doc)
    return doc


def headline(doc: dict) -> dict:
    """The numbers the anchoring record carries on-chain (scanner-invisible
    keys: no task_id / task_ids)."""
    g = doc["gates"]
    c = doc["corpus"]
    p = doc["participation"]
    return {
        "verify_everything_layers": g["verify_everything"]["layers"],
        "demo_suite": f"{g['suites']['demo']['passed']}/{g['suites']['demo']['total']}",
        "protocol_suite": f"{g['suites']['protocol']['passed']}/{g['suites']['protocol']['total']}",
        "task_law": {k: g["task_law"][k] for k in ("registered", "grandfathered",
                                                    "bound", "violations")},
        "doc_commands": g["doc_verify"]["commands"],
        "sweep_findings": g["sweep"]["findings"],
        "cold_install": "PASS" if g["cold_install"]["passed"] else "FAIL",
        "tasks_registered": c["tasks"]["registered"],
        "tasks_recorded": c["tasks"]["recorded"],
        "honest_negatives": c["tasks"]["honest_negatives"],
        "mip_decisions": c["mips"]["decisions"],
        "registered_actors": p["registered_actors"],
        "mirror_last_attested_idx": p["mirror"]["last_attested_idx"],
    }


def validate_pulse(doc) -> tuple:
    """(ok, reasons): shape, self-hash, and the refusal rule on the fields."""
    reasons = []
    if not isinstance(doc, dict) or doc.get("schema") != PULSE_SCHEMA:
        return (False, ["not a pulse/0.1 document"])
    for key in ("chain", "repo", "gates", "corpus", "participation", "pulse_hash"):
        if key not in doc:
            reasons.append(f"missing {key}")
    if reasons:
        return (False, reasons)
    if pulse_hash(doc) != doc["pulse_hash"]:
        reasons.append("pulse_hash does not recompute from the document")
    ok, why = gates_all_green(doc["gates"])
    if not ok:
        reasons.append("gates not green: " + "; ".join(why))
    for k in ("entries", "tip_index", "tip_hash"):
        if k not in doc["chain"]:
            reasons.append(f"chain.{k} missing")
    return (not reasons, reasons)


def chain_point_binds(doc: dict, entries) -> tuple:
    """The pulse's chain point must be a real prefix of THIS ledger."""
    c = doc["chain"]
    try:
        e = entries[c["tip_index"]]
    except (IndexError, TypeError, KeyError):
        return (False, "tip_index beyond this ledger")
    if e.get("hash") != c.get("tip_hash") or c.get("entries") != c["tip_index"] + 1:
        return (False, "chain point does not match this ledger's entry at that index")
    return (True, f"prefix-bound at idx {c['tip_index']}")


# ----------------------------------------------------------------------------
# live collection (the full battery)
# ----------------------------------------------------------------------------
def _git(args, cwd=_REPO_ROOT):
    r = subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def collect_gates(echo=print) -> dict:
    """Run every gate for real. Never raises on a red gate — returns the
    facts; assemble_pulse applies the refusal rule."""
    import protocol.routine_sweep as sweep
    import protocol.verify_everything as ve
    import protocol.doc_verify as dv
    import protocol.task_law_check as tl
    import protocol.release_readiness as rr
    quiet = lambda *a, **k: None

    echo("[1/5] routine sweep (layers, suites, evidence, git, release, docs)...")
    report = sweep.run_sweep()
    sections = {s["section"]: s for s in report["sections"]}
    suites = {}
    for d in sections.get("suites", {}).get("details", []):
        m = _SUITE_RE.search(d)
        name = "protocol" if d.startswith("protocol") else "demo"
        if m:
            suites[name] = {"passed": int(m.group(1)), "total": int(m.group(2))}
    for name in ("demo", "protocol"):
        suites.setdefault(name, {"passed": 0, "total": 0})

    echo("[2/5] verify_everything --full (layer count)...")
    all_ok, rows, _note = ve.run_verification(full=True)
    echo("[3/5] task law + documentation statistics...")
    law = tl.check_registry(echo=quiet)
    n_gf = sum(1 for v in law["verdicts"] if v["era"] == "grandfathered")
    docs_findings, stats = dv.check_docs(execute=False, echo=quiet,
                                         mip_dir=dv.MIP_DIR)
    docs_section_ok = sections.get("docs", {}).get("status") == "pass"
    echo("[4/5] cold install (wheel -> fresh venv -> metacoin verify)...")
    cold = rr.crit_cold_install(fast=False, echo=quiet)
    echo("[5/5] assembling...")
    return {
        "verify_everything": {"passed": bool(all_ok), "layers": len(rows),
                              "layers_failed": [r[0] for r in rows if not r[2]]},
        "suites": suites,
        "task_law": {"clean": bool(law["clean"]),
                     "registered": len(law["verdicts"]), "grandfathered": n_gf,
                     "bound": len(law["verdicts"]) - n_gf,
                     "violations": sum(len(v["violations"]) for v in law["verdicts"])
                     + len(law["registration_findings"]),
                     "law_index": law["law_index"]},
        "doc_verify": {"clean": bool(docs_section_ok and not docs_findings),
                       "findings": len(docs_findings)
                       + (0 if docs_section_ok else 1),
                       "docs": stats["docs"], "mips": stats["mips"],
                       "tokens": stats["tokens"], "idx_refs": stats["idx_refs"],
                       "commands": stats["commands"],
                       "commands_executed_in": "the sweep's docs section (fresh-clone sandbox)"},
        "cold_install": {"passed": cold["status"] == "PASS",
                         "detail": cold["detail"][:160]},
        "sweep": {"verdict": report["verdict"], "findings": report["finding_count"],
                  "sections": [s["section"] for s in report["sections"]]},
    }


def collect_corpus(entries) -> dict:
    from protocol.verifier_cli import TASK_MODULES
    from integrations.core import HONEST_NEGATIVES
    import protocol.doc_verify as dv
    mip_files = dv._mip_files(dv.MIP_DIR)
    superseded = dv._superseded_mips(dv.MIP_DIR)
    pins = {p: _git(["log", "-1", "--format=%h", "--", p]) for p in INTEGRATION_DIRS}
    ev_dir = os.path.join(_PROTO_DIR, "evidence")
    n_ev = len([n for n in os.listdir(ev_dir)
                if os.path.isfile(os.path.join(ev_dir, n))]) if os.path.isdir(ev_dir) else 0
    return corpus_from_chain(entries, TASK_MODULES, len(HONEST_NEGATIVES),
                             mip_files, superseded, pins, n_ev)


def generate(echo=print) -> dict:
    entries = _read_ledger(resolve_ledger_path())
    commit = _git(["rev-parse", "HEAD"])
    gates = collect_gates(echo=echo)
    return assemble_pulse(entries, commit, gates, collect_corpus(entries),
                          participation_from_chain(entries))


# ----------------------------------------------------------------------------
# status of the latest anchored pulse (cheap; the docs' verify block)
# ----------------------------------------------------------------------------
def latest_pulse_record(entries):
    recs = [e for e in entries if e.get("payload", {}).get("event") == PULSE_EVENT
            and e["payload"].get("status") == PULSE_STATUS]
    return recs[-1] if recs else None


def status(entries=None, echo=print) -> int:
    entries = entries if entries is not None else _read_ledger(resolve_ledger_path())
    rec = latest_pulse_record(entries)
    if rec is None:
        echo("PULSE STATUS: OK — no pulse anchored on the chain yet (named; the "
             "first pulse is generated after a clean sweep)")
        return 0
    p = rec["payload"]
    path = find_evidence_file(f"pulse_{p['pulse_hash'][:12]}.json")
    if path is None:
        echo(f"PULSE STATUS: BROKEN — idx {rec['index']} cites pulse "
             f"{p['pulse_hash'][:12]} but no evidence file ships")
        return 1
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    ok, reasons = validate_pulse(doc)
    bound, why = chain_point_binds(doc, entries)
    head_ok = headline(doc) == p.get("headline")
    if ok and bound and doc["pulse_hash"] == p["pulse_hash"] and head_ok:
        g = doc["gates"]
        echo(f"PULSE STATUS: OK — idx {rec['index']} re-derives: pulse_hash "
             f"{p['pulse_hash'][:12]} recomputes from the shipped file, {why}, "
             "headline numbers match the record; gates green at that point: "
             f"{g['verify_everything']['layers']} layers, suites "
             f"{g['suites']['demo']['passed']}/{g['suites']['demo']['total']} + "
             f"{g['suites']['protocol']['passed']}/{g['suites']['protocol']['total']}, "
             f"task law {g['task_law']['grandfathered']}/{g['task_law']['bound']}/"
             f"{g['task_law']['violations']} (grandfathered/bound/violations), "
             f"sweep findings {g['sweep']['findings']}, cold install "
             f"{'PASS' if g['cold_install']['passed'] else 'FAIL'}; "
             f"{len(entries) - 1 - doc['chain']['tip_index']} entries since")
        return 0
    echo("PULSE STATUS: BROKEN — " + "; ".join(reasons + ([] if bound else [why])
                                               + ([] if head_ok else ["headline mismatch"])
                                               + ([] if doc.get("pulse_hash") == p["pulse_hash"]
                                                  else ["file hash != record hash"])))
    return 1


# ----------------------------------------------------------------------------
# self-test (fixtures only; nothing runs the battery)
# ----------------------------------------------------------------------------
def _fixture_entries(n=5):
    ents = []
    prev = "0" * 64
    for i in range(n):
        h = hashlib.sha256(f"{prev}:{i}".encode()).hexdigest()
        ents.append({"index": i, "hash": h, "prev_hash": prev,
                     "payload": {"event": "ledger_genesis" if i == 0 else "x"}})
        prev = h
    return ents


def _green_gates():
    return {
        "verify_everything": {"passed": True, "layers": 16, "layers_failed": []},
        "suites": {"demo": {"passed": 30, "total": 30},
                   "protocol": {"passed": 24, "total": 24}},
        "task_law": {"clean": True, "registered": 20, "grandfathered": 18,
                     "bound": 2, "violations": 0, "law_index": 74},
        "doc_verify": {"clean": True, "findings": 0, "docs": 7, "mips": 10,
                       "tokens": 50, "idx_refs": 300, "commands": 32,
                       "commands_executed_in": "fixture"},
        "cold_install": {"passed": True, "detail": "fixture"},
        "sweep": {"verdict": "SWEEP CLEAN", "findings": 0, "sections": ["layers"]},
    }


def _fixture_corpus():
    return corpus_from_chain(_fixture_entries(), {"task-9001": "m"}, 1,
                             ["MIP-0001-x.md"], set(),
                             {p: "abc1234" for p in INTEGRATION_DIRS}, 3)


def _selftest() -> int:
    import copy
    print("=== protocol/pulse.py self-test (fixtures only; no gate runs, no writes) ===")
    print("A pulse that cannot be green honestly is not generated.\n")
    checks = []
    ents = _fixture_entries()
    part = participation_from_chain(ents)
    doc = assemble_pulse(ents, "abc1234", _green_gates(), _fixture_corpus(), part)
    doc2 = assemble_pulse(ents, "abc1234", _green_gates(), _fixture_corpus(), part)
    checks.append(("green gates assemble a pulse; the hash recomputes",
                   validate_pulse(doc)[0] and pulse_hash(doc) == doc["pulse_hash"]))
    checks.append(("deterministic: same inputs -> same bytes and hash",
                   canonical_json(doc) == canonical_json(doc2)
                   and doc["pulse_hash"] == doc2["pulse_hash"]))
    checks.append(("no wall-clock field anywhere in the hashed document",
                   not re.search(r"\b20\d\d-\d\d-\d\d", canonical_json(doc))
                   and "anchored_at" not in doc and "as_of_utc" not in doc))
    checks.append(("chain point binds to the fixture ledger (prefix rule)",
                   chain_point_binds(doc, ents)[0]))
    checks.append(("chain point refused against a different ledger",
                   not chain_point_binds(doc, _fixture_entries(3))[0]))
    for label, mutate in (
            ("one failed verify_everything layer", lambda g: g["verify_everything"].update(passed=False)),
            ("a demo suite at 29/30", lambda g: g["suites"]["demo"].update(passed=29)),
            ("one task-law violation", lambda g: g["task_law"].update(violations=1, clean=False)),
            ("one doc_verify finding", lambda g: g["doc_verify"].update(findings=1, clean=False)),
            ("a failed cold install", lambda g: g["cold_install"].update(passed=False)),
            ("one sweep finding", lambda g: g["sweep"].update(findings=1, verdict="SWEEP FINDINGS (1)"))):
        g = _green_gates()
        mutate(g)
        try:
            assemble_pulse(ents, "abc1234", g, _fixture_corpus(), part)
            refused = False
        except ValueError as exc:
            refused = "REFUSED" in str(exc)
        checks.append((f"refuses to generate with {label}", refused))
    tampered = copy.deepcopy(doc)
    tampered["gates"]["sweep"]["findings"] = 0
    tampered["corpus"]["tasks"]["registered"] = 99
    checks.append(("a tampered pulse fails validation (self-hash broken)",
                   not validate_pulse(tampered)[0]))
    forged = copy.deepcopy(doc)
    forged["gates"]["cold_install"]["passed"] = False
    forged["pulse_hash"] = pulse_hash(forged)
    checks.append(("a re-hashed pulse with a red gate still fails validation "
                   "(the refusal rule binds the fields, not only the hash)",
                   not validate_pulse(forged)[0]))
    checks.append(("headline carries the record's numbers (no task ids)",
                   headline(doc)["verify_everything_layers"] == 16
                   and "task_id" not in json.dumps(headline(doc))))
    # status on the fixture chain (no pulse) is a named OK
    out = []
    checks.append(("status names 'no pulse yet' as OK on a chain without one",
                   status(ents, echo=out.append) == 0 and "no pulse anchored" in out[0]))
    # status with a fixture record + evidence file in a temp evidence dir
    tmp = tempfile.mkdtemp(prefix=f"pulse_selftest_{os.getpid()}_")
    try:
        rec_entries = ents + [{"index": 5, "hash": "f" * 64, "prev_hash": ents[-1]["hash"],
                               "payload": {"event": PULSE_EVENT, "status": PULSE_STATUS,
                                           "pulse_hash": doc["pulse_hash"],
                                           "headline": headline(doc)}}]
        import protocol.work_molecule as wm
        orig = wm.find_evidence_file
        fpath = os.path.join(tmp, f"pulse_{doc['pulse_hash'][:12]}.json")
        with open(fpath, "w", encoding="utf-8") as f:
            json.dump(doc, f)
        globals()["find_evidence_file"] = lambda name: fpath if name == os.path.basename(fpath) else None
        out = []
        ok_status = status(rec_entries, echo=out.append) == 0 and "re-derives" in out[0]
        with open(fpath, "w", encoding="utf-8") as f:
            json.dump(tampered, f)
        out = []
        broken = status(rec_entries, echo=out.append) == 1 and "BROKEN" in out[0]
        globals()["find_evidence_file"] = orig
        checks.append(("status re-derives an anchored pulse from its evidence file", ok_status))
        checks.append(("status reports BROKEN when the evidence file is tampered", broken))
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    print("--- self-test invariants ---")
    failures = 0
    for name, passed in checks:
        print(f"{name:72s}: {'PASS' if passed else 'FAIL'}")
        failures += not passed
    ok = failures == 0
    print("\n=== self-test summary: " + ("ALL CHECKS BEHAVED CORRECTLY" if ok
                                         else "FAILURE — see above") + " ===")
    return 0 if ok else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="The pulse record (research-stage, ZERO-VALUE, no token): a "
                    "deterministic, hashed health snapshot derived from the chain "
                    "and real gate runs; refused if any gate is red.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--generate", action="store_true",
                      help="run the full battery and write the pulse (refuses on any red gate)")
    mode.add_argument("--verify", metavar="PULSE_JSON",
                      help="re-derive a pulse at the current state and compare")
    mode.add_argument("--status", action="store_true",
                      help="does the latest anchored pulse re-derive from its evidence file?")
    mode.add_argument("--selftest", action="store_true")
    parser.add_argument("--out", default=os.path.join(_REPO_ROOT, "pulse.json"))
    args = parser.parse_args(argv)
    if args.generate:
        try:
            doc = generate()
        except ValueError as exc:
            print(str(exc))
            print("pulse: NOT generated (nothing written)")
            return 1
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(canonical_json(doc) + "\n")
        print(f"pulse written: {args.out}")
        print(f"pulse_hash   : {doc['pulse_hash']}")
        print(f"chain point  : {doc['chain']['entries']} entries, tip idx "
              f"{doc['chain']['tip_index']} ({doc['chain']['tip_hash'][:12]}), "
              f"commit {doc['repo']['commit'][:8]}")
        print("headline     : " + json.dumps(headline(doc), sort_keys=True))
        return 0
    if args.verify:
        with open(args.verify, encoding="utf-8") as f:
            given = json.load(f)
        ok, reasons = validate_pulse(given)
        print("file validates: " + ("yes" if ok else "; ".join(reasons)))
        try:
            fresh = generate()
        except ValueError as exc:
            print(str(exc))
            print("PULSE VERIFY: FAILED — the current state cannot produce a green pulse")
            return 1
        if fresh["pulse_hash"] == given.get("pulse_hash"):
            print(f"PULSE VERIFY: MATCH — re-derived pulse_hash {fresh['pulse_hash'][:12]} "
                  "equals the file's (same chain point, same commit, same gate outcomes)")
            return 0
        diffs = [k for k in fresh if canonical_json(fresh[k]) != canonical_json(given.get(k))]
        print(f"PULSE VERIFY: DIFFERS in {diffs} — expected when the chain or commit moved; "
              f"file chain point {given.get('chain')}, current {fresh['chain']}")
        return 1
    if args.status:
        return status()
    return _selftest()


if __name__ == "__main__":
    sys.exit(main())
