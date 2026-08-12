# Copyright (c) 2023-2026 MetaCoin-Lab.
# Licensed under SML-1.0 — see LICENSE.md.
"""main.py — the unified `metacoin` command (thin argparse router).

EVERY subcommand delegates to an existing, already-verified protocol/demo
module — no protocol logic is reimplemented here. Each subcommand's --help
states what a pass PROVES (deterministic re-derivability of anchored claims)
and what it does NOT (independence, usefulness, value).

Coordinator operations (ledger WRITES — anchoring, registration, rotation
anchoring, drills) are DELIBERATELY excluded from this public CLI in v0.1.0:
a stranger's product verifies and participates; it does not write to the
coordinator's ledger. See protocol/external_verifier.py for those.

Research-stage, ZERO-VALUE, no token. Standard library only.
"""

# Keep installed environments byte-cache-clean, same discipline as the modules.
import sys
sys.dont_write_bytecode = True

import argparse
import json
import os

# Direct-script convenience (python3 metacoin_cli/main.py from a checkout):
# put the repo root on the path, mirroring the protocol modules' own idiom.
# Installed console-script/-m invocations never need this.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from metacoin_cli import BANNER, __version__
from metacoin_cli import locate

_EXCLUDED_NOTE = ("coordinator operations (ledger writes) are deliberately "
                  "not part of this CLI in v0.1.0 — see "
                  "protocol/external_verifier.py")


def _paths(args):
    """Resolved resource paths per the locate priority order."""
    return locate.paths_for(locate.find_repo(getattr(args, "repo", None)))


def _ledger_args(args, flag="--ledger"):
    """The explicit ledger-source argument list for a passthrough module."""
    return [flag, _paths(args)["ledger"]]


# ----------------------------------------------------------------------------
# Subcommand implementations (thin; heavy lifting is in the modules)
# ----------------------------------------------------------------------------
def _cmd_verify(args):
    import protocol.verify_everything as ve
    p = _paths(args)
    argv = ["--quick" if args.quick else "--full",
            "--snapshot", p["snapshot"], "--anchor-file", p["anchor"]]
    live = p["ledger"] if p["ledger"].endswith(".jsonl") else os.path.join(
        os.path.dirname(p["snapshot"]), "ledger_data.jsonl")
    argv += ["--ledger", live]  # ve degrades gracefully when absent
    if not args.quiet:
        print(f"metacoin {__version__} — {p['source_note']}")
    return ve.main(argv)


def _cmd_status(args):
    import protocol.audit as audit
    import protocol.work_molecule as work_molecule
    p = _paths(args)
    ok, reason, details = audit.verify_snapshot_file(p["snapshot"])
    entries = work_molecule._read_ledger(p["ledger"])
    events = {}
    for e in entries:
        ev = e.get("payload", {}).get("event", "?")
        events[ev] = events.get(ev, 0) + 1
    anchor = {}
    try:
        with open(p["anchor"], "r", encoding="utf-8") as f:
            anchor = json.load(f)
    except (OSError, json.JSONDecodeError):
        pass
    status = {
        "version": __version__,
        "source": p["source_note"],
        "snapshot_verifies": bool(ok),
        "entry_count": len(entries),
        "tip_index": entries[-1].get("index") if entries else None,
        "tip_hash": entries[-1].get("hash") if entries else None,
        "anchor_tip_matches": anchor.get("tip_hash") ==
        (entries[-1].get("hash") if entries else None),
        "record_types": dict(sorted(events.items())),
        "banner": BANNER,
    }
    if args.json:
        print(json.dumps(status, indent=2, sort_keys=True))
        return 0 if ok and status["anchor_tip_matches"] else 1
    print(f"metacoin {__version__} — {p['source_note']}")
    print(f"chain    : {len(entries)} entries, tip index "
          f"{status['tip_index']}, tip {str(status['tip_hash'])[:16]}..")
    print(f"snapshot : {'VERIFIED' if ok else 'FAILED'} ({reason})")
    print(f"anchor   : {'tip matches the committed anchor' if status['anchor_tip_matches'] else 'TIP MISMATCH vs committed anchor'}")
    print("records  : " + ", ".join(f"{k} x{v}"
                                    for k, v in sorted(events.items())))
    print(f"honesty  : {BANNER}")
    return 0 if ok and status["anchor_tip_matches"] else 1


def _cmd_task(args):
    import protocol.verifier_cli as verifier_cli
    import protocol.gate3_process as gate3_process
    if args.action == "list":
        rows = []
        for tid in sorted(verifier_cli.TASK_MODULES):
            tag = gate3_process.TASK_TAXONOMY.get(tid)
            rows.append({"task_id": tid, "module": verifier_cli.TASK_MODULES[tid],
                         "taxonomy_tag": tag})
        if args.json:
            print(json.dumps(rows, indent=2, sort_keys=True))
        else:
            for r in rows:
                tag = r["taxonomy_tag"] or "(untagged — honestly absent)"
                print(f"{r['task_id']}  {tag:34s} {r['module']}")
        return 0
    module = verifier_cli.load_task(args.task_id)
    result = module.compute()
    out_hash = module.output_hash(result)
    if args.json:
        print(json.dumps({"task_id": args.task_id, "output_hash": out_hash},
                         indent=2, sort_keys=True))
    else:
        print(f"{args.task_id}: recomputed output_hash {out_hash}")
        if not args.quiet:
            print("(deterministic re-run on THIS machine — compare against "
                  "the anchored ledger record; a matching hash proves "
                  "reproducibility, not execution-by-anyone-else)")
    return 0


def _cmd_molecule(args):
    import protocol.work_molecule as work_molecule
    argv = _ledger_args(args)
    if args.action == "build":
        argv = ["--task", args.task_id] + argv
        if args.as_of is not None:
            argv += ["--as-of-entry", str(args.as_of)]
    else:
        argv = ["--catalog"] + argv
        if args.generation:
            argv += ["--molecule-schema", args.generation]
        if args.as_of is not None:
            argv += ["--as-of-entry", str(args.as_of)]
    return work_molecule.main(argv)


def _cmd_aci(args):
    import protocol.agent_concentration as agent_concentration
    argv = _ledger_args(args)
    if args.korder:
        argv = ["--korder", "--kmax", str(args.kmax)] + argv
        if args.as_of is not None:
            argv += ["--as-of", str(args.as_of)]
        argv += ["--out", args.out or "aci_korder_report.json"]
    else:
        argv = ["--report"] + argv + ["--out", args.out or "aci_report.json"]
    return agent_concentration.main(argv)


def _cmd_challenge(args):
    import protocol.challenge_response as challenge_response
    mode = {"issue": "--issue", "respond": "--respond",
            "verify": "--verify"}[args.action]
    argv = [mode] + list(args.rest or [])
    return challenge_response.main(argv + _ledger_args(args))


def _cmd_identity(args):
    import protocol.actor_identity as actor_identity
    mode = {"generate": "--generate", "declare": "--declare",
            "rotate": "--rotate", "verify": "--verify"}[args.action]
    argv = [mode] + list(args.rest or [])
    if args.action == "rotate":
        argv += _ledger_args(args)
    return actor_identity.main(argv)


def _cmd_passport(args):
    import protocol.metawork_passport as metawork_passport
    argv = (["--actor", args.actor_id] if args.action == "build"
            else ["--all"])
    return metawork_passport.main(argv + _ledger_args(args))


def _cmd_economy(args):
    import demo.economy_demo as economy_demo
    # explicit CWD outputs: the module's defaults sit next to the package,
    # which in an installed environment is site-packages — never write there
    return economy_demo.main(["--run-all",
                              "--state", os.path.abspath("economy_state.json"),
                              "--out", os.path.abspath("economy_log.json")])


def _cmd_treasury(args):
    import demo.metastar_treasury as metastar_treasury
    return metastar_treasury.main(
        ["--init", "--state", os.path.abspath("treasury_state.json")]
        + _ledger_args(args))


def _cmd_flow1(args):
    import demo.flow1_uptime as flow1_uptime
    import protocol.work_molecule as work_molecule
    p = _paths(args)
    epoch_path, root, as_of = args.epoch, args.root, None
    if epoch_path is None or root is None:
        # parameterless convenience: locate the ANCHORED epoch + its root, and
        # re-verify AS-OF the history the coordinator saw at anchor time (the
        # anchored record's own key_indices must not self-collide in the
        # ledger-wide reuse scan — the standing N-1 idiom)
        entries = work_molecule._read_ledger(p["ledger"])
        anchor = next((e for e in reversed(entries)
                       if e.get("payload", {}).get("event")
                       == "uptime_epoch_anchored"
                       and e["payload"].get("status")
                       == "uptime-epoch-confirmed"), None)
        if anchor is None:
            print("error: no anchored uptime epoch on the chain — pass an "
                  "explicit epoch file and --root", file=sys.stderr)
            return 2
        pl = anchor["payload"]
        as_of = anchor["index"] - 1
        if epoch_path is None:
            epoch_path = work_molecule.find_evidence_file(
                f"uptime_epoch_{pl['epoch_hash'][:12]}.json")
            if epoch_path is None:
                print("error: anchored epoch evidence file not found in the "
                      "evidence bundle", file=sys.stderr)
                return 2
        if root is None:
            reg = next(e for e in entries
                       if e.get("index") == pl["key_root_ledger_index"])
            root = reg["payload"]["merkle_root"]
    with open(epoch_path, "r", encoding="utf-8") as f:
        epoch = json.load(f)
    ok, reasons = flow1_uptime.verify_epoch(epoch, root,
                                            ledger_path=p["ledger"],
                                            as_of_index=as_of)
    print(f"epoch verification: {'PASS' if ok else 'FAIL'}"
          + (f" (as-of ledger index {as_of})" if as_of is not None else ""))
    for r in reasons:
        print(f"  - {r}")
    return 0 if ok else 1


def _cmd_participate(args):
    import demo.participant_kit as participant_kit
    p = _paths(args)
    argv = [args.action, "--published", p["snapshot"], "--anchor", p["anchor"]]
    if args.handle:
        argv += ["--handle", args.handle]
    if args.relationship:
        argv += ["--relationship", args.relationship]
    if args.keys is not None:
        argv += ["--keys", str(args.keys)]
    if args.out:
        argv += ["--out", args.out]
    if not args.quiet:
        print(f"metacoin {__version__} — {p['source_note']}")
    return participant_kit.main(argv)


def _cmd_version(args):
    if args.json:
        print(json.dumps({"version": __version__, "banner": BANNER},
                         indent=2, sort_keys=True))
    else:
        print(f"metacoin-protocol {__version__} — {BANNER}")
    return 0


# ----------------------------------------------------------------------------
# Parser assembly
# ----------------------------------------------------------------------------
def _add_common(sp):
    sp.add_argument("--repo", help="explicit MetaCoin checkout to read the "
                                   "corpus from (default: auto-detect the "
                                   "CWD checkout, else installed package data)")
    sp.add_argument("--json", action="store_true",
                    help="machine-readable output where supported (report-"
                         "style subcommands already emit JSON natively)")
    sp.add_argument("--quiet", action="store_true",
                    help="suppress the CLI's own banner lines")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="metacoin",
        description=("MetaCoin protocol CLI — verify the complete anchored "
                     "evidence stack and exercise every read/participate "
                     "capability. Research-stage, ZERO-VALUE, no token."),
        epilog=f"{BANNER}. {_EXCLUDED_NOTE}.",
    )
    parser.add_argument("--version", action="version",
                        version=f"metacoin-protocol {__version__}")
    sub = parser.add_subparsers(dest="command")

    def add(name, fn, help_, proves, **kw):
        sp = sub.add_parser(
            name, help=help_, description=f"{help_}\n\n{proves}",
            epilog=BANNER,
            formatter_class=argparse.RawDescriptionHelpFormatter, **kw)
        sp.set_defaults(fn=fn)
        _add_common(sp)
        return sp

    sp = add("verify", _cmd_verify,
             "re-verify EVERY protocol layer (the flagship)",
             "PROVES: every anchored claim re-derives deterministically from "
             "the shipped corpus on YOUR machine. DOES NOT prove: "
             "independence, usefulness, or value.")
    mode = sp.add_mutually_exclusive_group()
    mode.add_argument("--full", action="store_true",
                      help="re-derive everything (default)")
    mode.add_argument("--quick", action="store_true",
                      help="bounded-cost anchored acceptance (never proof)")

    add("status", _cmd_status,
        "one-screen honest chain state",
        "PROVES: the snapshot verifies and matches the committed anchor. "
        "DOES NOT prove: anything about the work beyond re-derivability.")

    sp = add("task", _cmd_task, "run or list the registered reproducible tasks",
             "PROVES: a matching output_hash means deterministic "
             "reproducibility on this machine. DOES NOT prove: who executed "
             "it (a hash can be copied).")
    sp.add_argument("action", choices=["run", "list"])
    sp.add_argument("task_id", nargs="?",
                    help="task id for 'run', e.g. task-0007")

    sp = add("molecule", _cmd_molecule,
             "assemble Work Molecules (complete provenance units)",
             "PROVES: the molecule re-derives from anchored records; missing "
             "evidence stays explicit in provenance_debt, never fabricated.")
    sp.add_argument("action", choices=["build", "catalog"])
    sp.add_argument("task_id", nargs="?", help="task id for 'build'")
    sp.add_argument("--as-of", type=int, dest="as_of",
                    help="generation-lock: only ledger entries <= N")
    sp.add_argument("--generation", choices=["work-molecule/0.2",
                                             "work-molecule/0.3"],
                    help="catalog generation (default 0.3)")

    sp = add("aci", _cmd_aci, "concentration self-measurement (ACI)",
             "PROVES: how dependent the verification paths are on shared "
             "infrastructure (descriptive; low is NOT independence; unknown "
             "metadata scores worst-case). Higher-order profile via --korder.")
    sp.add_argument("action", choices=["report"])
    sp.add_argument("--korder", action="store_true",
                    help="higher-order ACI_k profile (exact enumeration only)")
    sp.add_argument("--kmax", type=int, default=4)
    sp.add_argument("--as-of", type=int, dest="as_of")
    sp.add_argument("--out")

    sp = add("challenge", _cmd_challenge,
             "nonce-bound possession proofs (issue/respond/verify)",
             "PROVES: the responder held the FULL result under a fresh nonce "
             "(defeats hash-copying). DOES NOT prove: where/when execution "
             "happened.")
    sp.add_argument("action", choices=["issue", "respond", "verify"])
    sp.add_argument("rest", nargs=argparse.REMAINDER,
                    help="passthrough args for protocol/challenge_response.py")

    sp = add("identity", _cmd_identity,
             "actor identity: Lamport one-time keychains + rotation",
             "PROVES: key-possession continuity under an anchored root "
             "(same-operator custody today — NOT third-party identity). "
             "Keychain files hold PRIVATE material; never commit them.")
    sp.add_argument("action", choices=["generate", "declare", "rotate",
                                       "verify"])
    sp.add_argument("rest", nargs=argparse.REMAINDER,
                    help="passthrough args for protocol/actor_identity.py")

    sp = add("passport", _cmd_passport,
             "per-actor verifiable contribution histories",
             "PROVES: a mechanical assembly of anchored history — a HISTORY, "
             "never a leaderboard (no rank/score exists, by mechanical rule).")
    sp.add_argument("action", choices=["build", "catalog"])
    sp.add_argument("actor_id", nargs="?", help="actor id for 'build'")

    sp = add("economy", _cmd_economy,
             "replay the anchored 30-simulated-day economy (full re-run)",
             "PROVES: the earn->verify->spend loop re-derives to the anchored "
             "log hash. Zero-value Test-META; simulated day indices, not real "
             "time. (writes economy_state/log.json into the CWD)")
    sp.add_argument("action", choices=["replay"])

    sp = add("treasury", _cmd_treasury,
             "re-derive the Two-Flow treasury constitution from anchored "
             "records",
             "PROVES: fees re-derive from the anchored economy; Flow 2 cannot "
             "mint. (writes treasury_state.json into the CWD)")
    sp.add_argument("action", choices=["status"])

    sp = add("flow1", _cmd_flow1,
             "re-verify the anchored Flow-1 uptime epoch (signed heartbeats)",
             "PROVES: every heartbeat signature re-verifies and the objective "
             "emission arithmetic replays. DOES NOT prove: real-world time "
             "or third-party infrastructure (same-operator, simulated slots).")
    sp.add_argument("action", choices=["verify-epoch"])
    sp.add_argument("epoch", nargs="?",
                    help="epoch JSON (default: the anchored epoch's evidence "
                         "copy)")
    sp.add_argument("--root", help="expected Merkle root (default: from the "
                                   "anchored registration)")

    sp = add("participate", _cmd_participate,
             "participate end-to-end: init identity, verify + sign, bundle",
             "PROVES: the bundle's signed result re-derives the anchored stack "
             "under your own key root. DOES NOT prove: independence — the "
             "relationship you declare is recorded '-claimed', never endorsed. "
             "Your keychain stays PRIVATE on this machine (working files land "
             "in the CWD; the coordinator anchors only after a human --confirm).")
    sp.add_argument("action", choices=["init", "run", "bundle"])
    sp.add_argument("--handle", help="participant handle for 'init' "
                                     "(becomes the actor id)")
    sp.add_argument("--relationship",
                    choices=["unaffiliated", "affiliated", "same-operator"],
                    help="SELF-DECLARED relationship to the coordinator's "
                         "operator (recorded as '<value>-claimed'; default "
                         "unaffiliated)")
    sp.add_argument("--keys", type=int,
                    help="one-time key count for 'init' (power of two; "
                         "default 32)")
    sp.add_argument("--out", help="bundle output path for 'bundle' "
                                  "(default bundle.json)")

    add("version", _cmd_version, "version + the honest banner",
        "Single-source version (metacoin_cli.__version__).")

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 0
    if args.command == "task" and args.action == "run" and not args.task_id:
        parser.error("task run requires a task id (see: metacoin task list)")
    if args.command == "molecule" and args.action == "build" and not args.task_id:
        parser.error("molecule build requires a task id")
    if args.command == "passport" and args.action == "build" and not args.actor_id:
        parser.error("passport build requires an actor id")
    if args.command == "participate" and args.action == "init" and not args.handle:
        parser.error("participate init requires --handle")
    try:
        return args.fn(args)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
