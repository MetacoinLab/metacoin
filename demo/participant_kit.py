# Copyright (c) 2023-2026 MetaCoin-Lab.
# Licensed under SML-1.0 — see LICENSE.md.
"""participant_kit.py — the PARTICIPANT-side kit: one command each to join, verify, and
submit (`metacoin participate init | run | bundle`).

================== HONESTY / SCOPE (READ ME) ==================
Research-stage, ZERO-VALUE, no token, no money, no mainnet, no networking, no payments.

This is the missing half of the external-verifier pilot: a stranger could already VERIFY
the anchored evidence stack; this kit lets them PARTICIPATE end-to-end —

  init   : generate a Lamport one-time keychain (LOCAL AND PRIVATE — it never leaves this
           machine and is never transmitted; only the public Merkle-root declaration is),
           the registrable public identity declaration, and a participant profile created
           against the published chain tip.
  run    : run the FULL mechanical verifier (protocol/agent_verifier.py semantics: chain
           re-walk, tip-against-committed-anchor, deterministic re-run of every recorded
           task) and SIGN the result with one one-time key.
  bundle : assemble ONE self-contained submission bundle (identity declaration + signed
           verifier result + coarse environment summary + declared relationship + chain
           point) and print the exact GitHub-issue submission instructions with the
           bundle's sha256.

INDEPENDENCE IS CLAIMED, NEVER PROVEN, BY THIS PATH: the participant self-declares their
relationship to the coordinator's operator (unaffiliated / affiliated / same-operator) and
the label is carried with a mandatory "-claimed" suffix everywhere — the protocol can
verify signatures, hashes, and re-derivations; it CANNOT verify organizational
independence. The claim is recorded, not endorsed.

PRIVATE MATERIAL IS MECHANICALLY EXCLUDED: the bundle builder scans the assembled bundle
and REFUSES to emit it if any dict key anywhere contains "private" (the coordinator's
intake validator independently enforces the same rule — belt and braces).

DETERMINISM: given the same keychain, run record, and chain state, `bundle` is
byte-deterministic (it mints no timestamps of its own — the only timestamps are copied
verbatim from the run record), so its sha256 is stable and can be quoted in a submission.

Standard library only. REUSES the existing verified components — actor_identity for keys
and signatures, agent_verifier for the verification run, audit for the snapshot check —
nothing is reimplemented. Not legal, financial, investment, or security-certification
advice.

Usage (what a participant runs after cloning / installing):
    python3 demo/participant_kit.py init --handle alice --relationship unaffiliated
    python3 demo/participant_kit.py run
    python3 demo/participant_kit.py bundle --out bundle.json
    python3 demo/participant_kit.py --selftest   # temp-only; writes nothing
"""

# Suppress __pycache__/*.pyc so importing protocol modules leaves no stray files.
import sys
sys.dont_write_bytecode = True

import argparse
import hashlib
import json
import os

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import protocol.actor_identity as actor_identity
import protocol.agent_verifier as agent_verifier
import protocol.audit as audit

PROFILE_SCHEMA = "participant-profile/0.1"
RESULT_SCHEMA = "participant-result/0.1"
BUNDLE_SCHEMA = "participant-bundle/0.1"

# The three self-declarable relationships. ALWAYS suffixed "-claimed" when recorded:
# intake can verify signatures/hashes/re-derivations, never organizational independence.
RELATIONSHIP_CHOICES = ("unaffiliated", "affiliated", "same-operator")
CLAIMED_SUFFIX = "-claimed"

# Working-file names (CWD/--workdir; all gitignored — local artifacts, never committed;
# the canonical record is whatever the coordinator ANCHORS after human confirmation).
KEYCHAIN_FILE = "keychain_participant.json"   # PRIVATE — never transmitted
PROFILE_FILE = "participant.json"
DECLARATION_FILE = "participant_identity.json"
RESULT_FILE = "participant_result.json"
DEFAULT_BUNDLE_FILE = "bundle.json"

DEFAULT_PUBLISHED = agent_verifier.DEFAULT_PUBLISHED_PATH
DEFAULT_ANCHOR = agent_verifier.DEFAULT_ANCHOR_PATH

BUNDLE_HONEST_NOTE = (
    "participant-bundle (research-stage, zero-value, no token). The signed verifier "
    "result proves the holder of the declared key root produced a full mechanical "
    "verification run (chain re-walk + tip-against-anchor + deterministic task re-runs); "
    "the relationship label is SELF-DECLARED and carries '-claimed' because the protocol "
    "cannot verify organizational independence — the claim is recorded, not endorsed. "
    "This bundle contains PUBLIC material only; the private keychain never leaves the "
    "participant's machine. Not consensus, not mainnet, not payment, not a token."
)


def canonical_json(obj) -> str:
    """Canonical JSON: sorted keys, compact separators, ASCII — byte-stable for hashing."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _contains_private_material(obj) -> bool:
    """True if any dict key anywhere contains 'private'. Mirrors
    external_verifier._contains_private_material ON PURPOSE (reimplemented in
    ~6 lines so the participant kit imports no coordinator module — the same
    rule enforced independently on both sides of the intake)."""
    if isinstance(obj, dict):
        return any("private" in str(k).lower() or _contains_private_material(v)
                   for k, v in obj.items())
    if isinstance(obj, list):
        return any(_contains_private_material(v) for v in obj)
    return False


def _load(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _write(path: str, obj: dict):
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps(obj, indent=2, sort_keys=True) + "\n")


def claimed_relationship(relationship: str) -> str:
    """The recorded form of a declared relationship: ALWAYS '-claimed'-suffixed."""
    if relationship not in RELATIONSHIP_CHOICES:
        raise ValueError(f"relationship must be one of {RELATIONSHIP_CHOICES} "
                         f"(got {relationship!r})")
    return relationship + CLAIMED_SUFFIX


# ----------------------------------------------------------------------------
# init: keychain (PRIVATE) + public declaration + profile
# ----------------------------------------------------------------------------
def init_participant(handle: str, relationship: str = "unaffiliated",
                     key_count: int = 32, workdir: str = ".",
                     published_path: str = DEFAULT_PUBLISHED) -> dict:
    """Create the participant identity: PRIVATE keychain, PUBLIC declaration, profile.

    Refuses to overwrite an existing keychain (an identity is stateful — clobbering
    it would orphan any indices already consumed). The profile records the published
    chain tip the identity was created against, and the relationship in its
    '-claimed' form. Returns {keychain_path, declaration_path, profile_path, profile}.
    """
    if not isinstance(handle, str) or not handle:
        raise ValueError("handle must be a non-empty string")
    rel_claimed = claimed_relationship(relationship)
    keychain_path = os.path.join(workdir, KEYCHAIN_FILE)
    if os.path.exists(keychain_path):
        raise ValueError(
            f"refusing to overwrite the existing keychain at {keychain_path} — a "
            "keychain is a stateful identity (used one-time indices are marked in it); "
            "move it away deliberately if you really want a fresh identity")

    snap_ok, snap_reason, details = audit.verify_snapshot_file(published_path)
    if not snap_ok:
        raise ValueError(f"published snapshot does not verify ({snap_reason}) — refusing "
                         "to create an identity against an unverifiable chain")

    keychain = actor_identity.generate_keychain(handle, key_count=key_count)
    _write(keychain_path, keychain)
    declaration = actor_identity.public_declaration(keychain)
    declaration_path = os.path.join(workdir, DECLARATION_FILE)
    _write(declaration_path, declaration)

    profile = {
        "schema": PROFILE_SCHEMA,
        "handle": handle,
        "actor_id": handle,
        "relationship_declared": relationship,
        "relationship_claimed": rel_claimed,
        "created_against": {
            "snapshot_tip_index": details.get("tip_index"),
            "snapshot_tip_hash": details.get("tip_hash"),
        },
        "keychain_file": KEYCHAIN_FILE,
        "declaration_file": DECLARATION_FILE,
        "zero_value": True,
        "no_token": True,
        "honest_note": (
            "relationship is SELF-DECLARED and recorded with '-claimed' — the protocol "
            "verifies signatures/hashes/re-derivations, never organizational "
            "independence; the private keychain never leaves this machine"),
    }
    profile_path = os.path.join(workdir, PROFILE_FILE)
    _write(profile_path, profile)
    return {"keychain_path": keychain_path, "declaration_path": declaration_path,
            "profile_path": profile_path, "profile": profile}


# ----------------------------------------------------------------------------
# run: full mechanical verification + one-time signature over the result
# ----------------------------------------------------------------------------
def run_participant(workdir: str = ".", published_path: str = DEFAULT_PUBLISHED,
                    anchor_path: str = DEFAULT_ANCHOR) -> dict:
    """Run the FULL verifier (agent_verifier.run_verification: chain re-walk +
    tip-vs-anchor + re-run of every recorded task) and sign the result.

    Signs sha256(canonical result) with the first one-time key index unused both
    locally and per the published chain's cross-type scan, then persists the
    used-index mark back to the (stateful) keychain file. Returns the signed
    result record (also written to participant_result.json).
    """
    profile = _load(os.path.join(workdir, PROFILE_FILE))
    keychain = _load(os.path.join(workdir, KEYCHAIN_FILE))
    result = agent_verifier.run_verification(
        profile["handle"], published_path=published_path, anchor_path=anchor_path)

    ledger_source = published_path if os.path.exists(published_path) else None
    key_index = actor_identity.first_unused_index(keychain, ledger_source)
    signature = actor_identity.sign(keychain, key_index,
                                    canonical_json(result).encode("utf-8"),
                                    ledger_source=ledger_source)
    _write(os.path.join(workdir, KEYCHAIN_FILE), keychain)  # persist used-index mark

    signed = {
        "schema": RESULT_SCHEMA,
        "handle": profile["handle"],
        "actor_id": profile["actor_id"],
        "result": result,
        "signature": signature,
        "key_index": key_index,
        "merkle_root": keychain["merkle_root"],
    }
    _write(os.path.join(workdir, RESULT_FILE), signed)
    return signed


# ----------------------------------------------------------------------------
# bundle: ONE self-contained, deterministic, public-only submission object
# ----------------------------------------------------------------------------
def build_bundle(workdir: str = ".", out_path: str = None) -> tuple:
    """Assemble the submission bundle from the init/run working files.

    Deterministic given keychain + chain (no timestamps are minted here — the only
    timestamps are the ones inside the copied run record). REFUSES to emit a bundle
    containing any private material, a malformed relationship label, or a signature
    that does not verify against the declared root. Returns (bundle, sha256_hex);
    writes the bundle to `out_path` when given.
    """
    profile = _load(os.path.join(workdir, PROFILE_FILE))
    declaration = _load(os.path.join(workdir, DECLARATION_FILE))
    signed = _load(os.path.join(workdir, RESULT_FILE))

    rel = profile.get("relationship_claimed")
    if (not isinstance(rel, str) or not rel.endswith(CLAIMED_SUFFIX)
            or rel[: -len(CLAIMED_SUFFIX)] not in RELATIONSHIP_CHOICES):
        raise ValueError(
            f"relationship label {rel!r} is malformed — it must be one of "
            f"{RELATIONSHIP_CHOICES} suffixed {CLAIMED_SUFFIX!r} (independence is "
            "claimed, never proven, by this path)")
    if declaration.get("actor_id") != profile.get("actor_id"):
        raise ValueError("declaration actor_id does not match the profile actor_id")

    result = signed["result"]
    signature = signed["signature"]
    ok, reasons = actor_identity.verify_signature(
        signature, declaration["merkle_root"], canonical_json(result).encode("utf-8"))
    if not ok:
        raise ValueError("refusing to bundle: the run signature does not verify "
                         "against the declared root — " + "; ".join(reasons))

    bundle = {
        "schema": BUNDLE_SCHEMA,
        "event": "participant_bundle",
        "handle": profile["handle"],
        "actor_id": profile["actor_id"],
        "relationship_claimed": rel,
        "identity_declaration": declaration,
        "signed_result": {
            "result": result,
            "signature": signature,
            "key_index": signed["key_index"],
        },
        "environment_summary": result.get("environment_summary"),
        "chain_point": {
            "snapshot_tip_index": result.get("snapshot_tip_index"),
            "snapshot_tip_hash": result.get("snapshot_tip_hash"),
        },
        "zero_value": True,
        "no_token": True,
        "honest_note": BUNDLE_HONEST_NOTE,
    }
    if _contains_private_material(bundle):
        raise ValueError(
            "refusing to build: the assembled bundle contains PRIVATE key material "
            "(a dict key containing 'private') — the bundle is public-only by "
            "mechanical rule; never feed a keychain file into a bundle")

    sha = _sha256_hex(canonical_json(bundle).encode("utf-8"))
    if out_path:
        _write(out_path, bundle)
    return (bundle, sha)


def print_submission_instructions(handle: str, out_path: str, sha: str):
    """The exact submission steps (the README's existing GitHub-issue path)."""
    print("\nNEXT STEP — submit this bundle (the README's GitHub-issue path):")
    print(f"  1. Open a GitHub Issue titled: Participant bundle: {handle}")
    print(f"  2. Attach or paste {out_path}   (sha256: {sha})")
    print("  3. The coordinator validates it with external_verifier.py --intake "
          "(six named checks, each with evidence), and only a human --confirm "
          "anchors the outcome — pass or rejection — to the public ledger.")
    print(f"\nYour keychain ({KEYCHAIN_FILE}) is PRIVATE: it never leaves this "
          "machine and is NOT part of the bundle (private material is "
          "mechanically refused at build AND at intake).")


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="participant_kit.py",
        description=(
            "MetaCoin participant kit (research-stage, ZERO-VALUE, no token): init a "
            "local PRIVATE keychain + public identity, run the full mechanical "
            "verifier and sign the result, and build one self-contained submission "
            "bundle. With no action, runs the self-test (temp files only)."
        ),
        epilog=(
            "HONESTY: the relationship you declare is recorded with a '-claimed' "
            "suffix — the protocol verifies signatures, hashes, and re-derivations; "
            "it cannot verify organizational independence. Your keychain file holds "
            "PRIVATE material: never commit it, never submit it. Not consensus, not "
            "mainnet, not payment, not a token."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("action", nargs="?", choices=["init", "run", "bundle"],
                        help="init (keychain+identity+profile), run (verify+sign), "
                             "bundle (assemble the submission)")
    parser.add_argument("--handle", help="participant handle (init; becomes actor_id)")
    parser.add_argument("--relationship", choices=list(RELATIONSHIP_CHOICES),
                        default="unaffiliated",
                        help="SELF-DECLARED relationship to the coordinator's operator "
                             "(recorded as '<value>-claimed'; default unaffiliated)")
    parser.add_argument("--keys", type=int, default=32,
                        help="one-time key count for init (power of two; default 32)")
    parser.add_argument("--workdir", default=".",
                        help="directory for the participant working files (default: CWD)")
    parser.add_argument("--published", default=DEFAULT_PUBLISHED,
                        help=f"published snapshot to verify (default {DEFAULT_PUBLISHED})")
    parser.add_argument("--anchor", default=DEFAULT_ANCHOR,
                        help=f"committed tip anchor (default {DEFAULT_ANCHOR})")
    parser.add_argument("--out", help="bundle output path (default: bundle.json in "
                                      "--workdir)")
    parser.add_argument("--selftest", action="store_true",
                        help="run the mechanical self-test (temp files only)")
    args = parser.parse_args(argv)

    if args.selftest or args.action is None:
        return _selftest()

    try:
        if args.action == "init":
            if not args.handle:
                parser.error("init requires --handle")
            out = init_participant(args.handle, args.relationship,
                                   key_count=args.keys, workdir=args.workdir,
                                   published_path=args.published)
            p = out["profile"]
            print(f"participant identity created for handle {p['handle']!r}")
            print(f"  PRIVATE keychain    : {out['keychain_path']}  (NEVER commit or "
                  "transmit this file — it holds one-time signing secrets)")
            print(f"  public declaration  : {out['declaration_path']}  (this is what "
                  "gets registered)")
            print(f"  profile             : {out['profile_path']}")
            print(f"  relationship        : {p['relationship_claimed']}  (SELF-DECLARED "
                  "— recorded as a claim, not endorsed)")
            print(f"  created against tip : index {p['created_against']['snapshot_tip_index']}, "
                  f"{str(p['created_against']['snapshot_tip_hash'])[:16]}..")
            print("next: participate run")
            return 0

        if args.action == "run":
            signed = run_participant(args.workdir, args.published, args.anchor)
            r = signed["result"]
            print(f"verification run complete for {signed['handle']!r}:")
            print(f"  verdict            : {r['verdict']}")
            print(f"  chain_verified     : {r['chain_verified']}")
            print(f"  tip_matches_anchor : {r['tip_matches_anchor']}")
            print(f"  tasks reproduced   : "
                  f"{sum(1 for t in r['task_reproductions'] if t['match'])}"
                  f"/{len(r['task_reproductions'])}")
            print(f"  signed with        : one-time key index {signed['key_index']} "
                  f"under root {signed['merkle_root'][:16]}..")
            print(f"  written            : {os.path.join(args.workdir, RESULT_FILE)}")
            print("next: participate bundle")
            return 0 if r["verdict"] == "verified" else 1

        out_path = args.out or os.path.join(args.workdir, DEFAULT_BUNDLE_FILE)
        bundle, sha = build_bundle(args.workdir, out_path)
        print(f"bundle written: {out_path}")
        print(f"  schema               : {bundle['schema']}")
        print(f"  handle / actor_id    : {bundle['handle']}")
        print(f"  relationship_claimed : {bundle['relationship_claimed']}")
        print(f"  chain point          : index "
              f"{bundle['chain_point']['snapshot_tip_index']}, "
              f"{str(bundle['chain_point']['snapshot_tip_hash'])[:16]}..")
        print(f"  bundle sha256        : {sha}")
        print_submission_instructions(bundle["handle"], out_path, sha)
        return 0
    except (ValueError, FileNotFoundError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


# ============================== SELF-TEST ====================================
def _selftest() -> int:
    """Mechanical self-test: init/run/bundle round-trip against a TEMP corpus,
    private-material refusal, bundle determinism, '-claimed' enforcement.
    Temp files only; the repo gains nothing."""
    import copy
    import shutil
    import tempfile

    from protocol.ledger import Ledger
    import protocol.verifier_cli as verifier_cli

    print("=== demo/participant_kit.py self-test (participant init/run/bundle) ===")
    print("Research-stage, zero-value. Temp corpus + temp workdir; repo untouched.\n")

    checks = []
    root_before = set(os.listdir(_REPO_ROOT))
    tmp = tempfile.mkdtemp(prefix=f"participant_kit_selftest_{os.getpid()}_")
    try:
        # --- TEMP corpus: a tiny verifiable chain with one runnable-task record,
        # exported to a snapshot + anchor (the artifacts a participant clones) ---
        TASK = "task-0002"
        module = verifier_cli.load_task(TASK)
        task_hash = module.output_hash(module.compute())
        led = Ledger(os.path.join(tmp, "ledger.jsonl"))
        led.append({"event": "ledger_genesis", "note": "temp chain-start (self-test)",
                    "zero_value": True, "no_token": True})
        led.append({"event": "external_verification_result",
                    "status": "externally-verified", "task_id": TASK,
                    "output_hash": task_hash, "zero_value": True, "no_token": True})
        published = os.path.join(tmp, "published.json")
        anchor = os.path.join(tmp, "anchor.json")
        audit.export_snapshot(led.path, published)
        audit.write_anchor(led.path, anchor)

        work = os.path.join(tmp, "work")
        os.makedirs(work)

        # [a] init: private keychain + public declaration + '-claimed' profile
        out = init_participant("selftest-participant", "unaffiliated",
                               key_count=4, workdir=work,
                               published_path=published)
        keychain = _load(out["keychain_path"])
        declaration = _load(out["declaration_path"])
        profile = out["profile"]
        checks.append(("init writes PRIVATE keychain + public declaration + profile",
                       _contains_private_material(keychain)
                       and not _contains_private_material(declaration)
                       and declaration["merkle_root"] == keychain["merkle_root"]))
        checks.append(("profile relationship ALWAYS carries '-claimed'",
                       profile["relationship_claimed"] == "unaffiliated-claimed"))
        checks.append(("profile records the chain tip it was created against",
                       profile["created_against"]["snapshot_tip_index"] == 1))
        # ...and every declarable relationship gets the suffix
        checks.append(("every relationship choice maps to a '-claimed' form",
                       all(claimed_relationship(r) == r + CLAIMED_SUFFIX
                           for r in RELATIONSHIP_CHOICES)))
        try:
            claimed_relationship("independent")  # not a declarable value
            checks.append(("unknown relationship value refused", False))
        except ValueError:
            checks.append(("unknown relationship value refused", True))
        try:
            init_participant("selftest-participant", workdir=work,
                             published_path=published)
            checks.append(("re-init refuses to clobber an existing keychain", False))
        except ValueError as exc:
            checks.append(("re-init refuses to clobber an existing keychain",
                           "refusing to overwrite" in str(exc)))

        # [b] run: full verification + one-time signature; keychain marked used
        signed = run_participant(work, published, anchor)
        result = signed["result"]
        sig_ok, _ = actor_identity.verify_signature(
            signed["signature"], declaration["merkle_root"],
            canonical_json(result).encode("utf-8"))
        keychain_after = _load(out["keychain_path"])
        checks.append(("run: verifier verdict 'verified' on the temp corpus",
                       result["verdict"] == "verified"
                       and result["chain_verified"] is True
                       and result["tip_matches_anchor"] is True))
        checks.append(("run: signature verifies against the declared root",
                       sig_ok))
        checks.append(("run: used one-time index persisted back to the keychain",
                       signed["key_index"] in keychain_after["used_indices"]))

        # [c] bundle: round-trip + public-only + deterministic sha256
        bundle_path = os.path.join(work, DEFAULT_BUNDLE_FILE)
        bundle, sha1 = build_bundle(work, bundle_path)
        _bundle2, sha2 = build_bundle(work)  # rebuild, no write
        reloaded = _load(bundle_path)
        checks.append(("bundle round-trips and matches its schema",
                       reloaded["schema"] == BUNDLE_SCHEMA
                       and reloaded == bundle))
        checks.append(("bundle carries NO private material",
                       not _contains_private_material(bundle)))
        checks.append(("bundle sha256 is deterministic across rebuilds",
                       sha1 == sha2 and len(sha1) == 64))
        checks.append(("bundle relationship carries '-claimed'",
                       bundle["relationship_claimed"].endswith(CLAIMED_SUFFIX)))

        # [d] PRIVATE-MATERIAL REFUSAL: a result file poisoned with a private key
        # fragment must make the bundle builder refuse (mechanical, not advisory)
        poisoned = copy.deepcopy(signed)
        poisoned["result"]["private_key_fragment"] = keychain["keys"][0]["private"][0]
        _write(os.path.join(work, RESULT_FILE), poisoned)
        try:
            build_bundle(work)
            checks.append(("poisoned bundle REFUSED (private material)", False))
        except ValueError as exc:
            # the poisoned result also breaks the signature; either refusal is a
            # correct mechanical stop — but scrub the signature reason by testing
            # the private-material scan directly too
            checks.append(("poisoned bundle REFUSED (private material)",
                           "refusing" in str(exc)))
        poisoned_bundle = copy.deepcopy(bundle)
        poisoned_bundle["identity_declaration"]["private"] = "x"
        checks.append(("private-material scan catches a poisoned assembled bundle",
                       _contains_private_material(poisoned_bundle)))
        _write(os.path.join(work, RESULT_FILE), signed)  # restore

        # [e] '-claimed' ENFORCEMENT at build time: a profile stripped of the
        # suffix is refused
        bad_profile = dict(profile)
        bad_profile["relationship_claimed"] = "unaffiliated"
        _write(os.path.join(work, PROFILE_FILE), bad_profile)
        try:
            build_bundle(work)
            checks.append(("bundle refuses a relationship without '-claimed'", False))
        except ValueError as exc:
            checks.append(("bundle refuses a relationship without '-claimed'",
                           "-claimed" in str(exc)))
        _write(os.path.join(work, PROFILE_FILE), profile)  # restore

        # [f] a tampered result invalidates the signature -> bundle refuses
        tampered = copy.deepcopy(signed)
        tampered["result"]["verdict"] = "verified-but-tampered"
        _write(os.path.join(work, RESULT_FILE), tampered)
        try:
            build_bundle(work)
            checks.append(("bundle refuses a result the signature does not cover",
                           False))
        except ValueError as exc:
            checks.append(("bundle refuses a result the signature does not cover",
                           "signature does not verify" in str(exc)))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

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
