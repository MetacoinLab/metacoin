# Copyright (c) 2023-2026 MetaCoin-Lab.
# Licensed under SML-1.0 — see LICENSE.md.
"""challenge_response.py — MetaCoin CHALLENGE-RESPONSE verification (schema "challenge-response/0.1").

================== THE MECHANISM (READ ME) ==================
Every ledger record to date honestly notes that a matching output_hash proves
REPRODUCIBILITY but not execution, because a hash can be copied from public
records. This module is the protocol's first defense against that copy attack.

A challenge binds verification to a fresh nonce:

    challenge = {challenge_id, task_id, nonce (32 random bytes hex), issued_for}
    response_hash = sha256(nonce_hex + ":" + canonical_json(result))

Since canonical_json(result) is never published (only its sha256, the
output_hash, appears in any public artifact — verified by survey against every
git-tracked file), a correct response_hash is computable ONLY by a party holding
the FULL result — i.e. one that actually executed the task or holds its complete
output. This upgrades the claim from "reproduced the hash" to "DEMONSTRATED
POSSESSION of the full result under a fresh nonce."

HONEST RESIDUAL (what this does NOT prove): possession-under-nonce still does
not prove WHERE or WHEN execution happened, nor rule out result-file sharing
between colluding parties — and for these open-source demo tasks anyone can
obtain the result by simply running the public code. What it DEFEATS is
copy-from-public-records: an actor holding only the published hashes cannot
answer a fresh challenge. That is the named threat, and the self-test's
copy_attack_defeated fixture proves the rejection mechanically.

DETERMINISM NOTE: nonce generation (secrets.token_hex) is the protocol's ONE
intentionally non-deterministic operation. Freshness IS the security property —
a predictable nonce would readmit precomputation of response hashes, recreating
the copy attack. Everything DOWNSTREAM of a given challenge is deterministic:
the same challenge honestly answered twice yields byte-identical responses, and
verification is a pure recompute.

Research-stage, ZERO-VALUE, no token, no money, no mainnet, no networking, no
payments. Standard library only (json, hashlib, secrets, os, argparse). Task
execution is REUSED from protocol/verifier_cli.py; ledger reading is REUSED from
protocol/work_molecule.py (live JSONL or published snapshot). The canonical-JSON
helper is deliberately per-module (house style). Not legal, financial,
investment, or security-certification advice.

Usage:
    python3 protocol/challenge_response.py --issue --task task-0007 --verifier spark-agent-same-operator --out challenge.json
    python3 protocol/challenge_response.py --respond challenge.json --out response.json
    python3 protocol/challenge_response.py --verify challenge.json response.json
    python3 protocol/challenge_response.py --selftest   # temp-only; writes nothing
"""

# Suppress __pycache__/*.pyc so importing protocol modules below leaves no stray files.
import sys
sys.dont_write_bytecode = True

import argparse
import hashlib
import json
import os
import secrets

# Make `from protocol...` resolve when run directly (repo root on path).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# REUSE existing components — do NOT reimplement them.
from protocol.verifier_cli import load_task, normalize_task_id
import protocol.work_molecule as work_molecule  # source-agnostic _read_ledger
import protocol.actor_identity as actor_identity  # Lamport OTS sign/verify

_PROTO_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_LEDGER_PATH = os.path.join(_PROTO_DIR, "ledger_data.jsonl")

SCHEMA_VERSION = "challenge-response/0.1"

_HEX = set("0123456789abcdef")

_CHALLENGE_KEYS = ("schema", "challenge_id", "task_id", "nonce", "issued_for",
                   "ledger_tip_at_issue")
_RESPONSE_KEYS = ("schema", "challenge_id", "task_id", "verifier_id",
                  "output_hash", "response_hash", "responded_against_tip")
# "signature" is the one OPTIONAL response field (actor-identity layer): a
# Lamport-Merkle signature over sha256(canonical response-without-signature).
_OPTIONAL_RESPONSE_KEYS = ("signature",)

# Root resolution and the prior-use scan live in actor_identity (root_chain /
# active_root_asof / uses_for_root) — the signature layer reads them from there.


# ----------------------------------------------------------------------------
# Canonical JSON + hashes (per-module helper, same discipline as ledger.py)
# ----------------------------------------------------------------------------
def _sign_safe_zero(obj):
    """Normalize -0.0 -> 0.0 recursively (THE NEGATIVE-ZERO CANONICAL RULE):
    the sign of a zero is a platform artifact of last-ulp cancellation with
    no semantic content — canonical artifacts are sign-of-zero-free by rule.
    Floats only; ints and bools pass through untouched."""
    if isinstance(obj, float):
        return 0.0 if obj == 0.0 else obj
    if isinstance(obj, dict):
        return {k: _sign_safe_zero(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sign_safe_zero(v) for v in obj]
    return obj


def canonical_json(obj) -> str:
    """Canonical JSON: sorted keys, compact separators, ASCII — byte-stable for hashing."""
    return json.dumps(_sign_safe_zero(obj), sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True)


def compute_challenge_id(challenge: dict) -> str:
    """SHA-256 hex of the canonical JSON of the challenge WITHOUT its challenge_id
    field (the same anti-circularity pattern as the WMID and the ledger entry hash).
    The id therefore commits to the nonce, the task, the verifier binding, and the
    chain state at issue — none can be swapped without changing the id."""
    content = {k: v for k, v in challenge.items() if k != "challenge_id"}
    return hashlib.sha256(canonical_json(content).encode("utf-8")).hexdigest()


def compute_response_hash(nonce_hex: str, canonical_result: str) -> str:
    """The possession proof: sha256(nonce_hex + ":" + canonical_json(result)).

    Only a party holding the FULL canonical result bytes can compute this; the
    published output_hash (sha256 of the result alone) does not help, because the
    nonce is prepended INSIDE the preimage."""
    return hashlib.sha256(
        (nonce_hex + ":" + canonical_result).encode("utf-8")).hexdigest()


# ----------------------------------------------------------------------------
# Issue / respond / verify
# ----------------------------------------------------------------------------
def issue_challenge(task_id: str, verifier_id: str,
                    ledger_path: str = DEFAULT_LEDGER_PATH) -> dict:
    """Issue a fresh challenge for `task_id`, bound to `verifier_id` and to the
    current chain tip.

    The nonce is the protocol's ONE intentionally non-deterministic value (see the
    DETERMINISM NOTE in the module docstring): freshness is the security property.
    """
    short = normalize_task_id(task_id)
    if not isinstance(verifier_id, str) or not verifier_id:
        raise ValueError("verifier_id must be a non-empty string")
    entries = work_molecule._read_ledger(ledger_path)
    if not entries:
        raise ValueError(f"ledger at {ledger_path} is empty — a challenge must bind "
                         "to a real chain state")
    tip = entries[-1]
    challenge = {
        "schema": SCHEMA_VERSION,
        "task_id": short,
        "nonce": secrets.token_hex(32),  # 32 random bytes -> 64 hex chars
        "issued_for": verifier_id,
        "ledger_tip_at_issue": {"index": tip["index"], "hash": tip["hash"]},
    }
    challenge["challenge_id"] = compute_challenge_id(challenge)
    return challenge


def validate_challenge(challenge):
    """Structurally validate a challenge. Returns (ok, reasons)."""
    reasons = []
    if not isinstance(challenge, dict):
        return (False, ["challenge is not a JSON object"])
    missing = [k for k in _CHALLENGE_KEYS if k not in challenge]
    unknown = [k for k in challenge if k not in _CHALLENGE_KEYS]
    if missing:
        reasons.append(f"missing fields: {missing}")
    if unknown:
        reasons.append(f"unknown fields: {unknown}")
    if reasons:
        return (False, reasons)
    if challenge["schema"] != SCHEMA_VERSION:
        reasons.append(f"schema must be {SCHEMA_VERSION!r}")
    try:
        normalize_task_id(challenge["task_id"])
    except (KeyError, TypeError) as exc:
        reasons.append(f"unknown task_id: {exc}")
    nonce = challenge["nonce"]
    if not (isinstance(nonce, str) and len(nonce) == 64
            and all(c in _HEX for c in nonce)):
        reasons.append("nonce must be 64 lowercase hex chars (32 random bytes)")
    if not isinstance(challenge["issued_for"], str) or not challenge["issued_for"]:
        reasons.append("issued_for must be a non-empty string")
    tip = challenge["ledger_tip_at_issue"]
    if not (isinstance(tip, dict) and isinstance(tip.get("index"), int)
            and isinstance(tip.get("hash"), str)):
        reasons.append("ledger_tip_at_issue must be {index: int, hash: str}")
    if not reasons and compute_challenge_id(challenge) != challenge["challenge_id"]:
        reasons.append("challenge_id does not recompute from content "
                       "(challenge was altered)")
    return (not reasons, reasons)


def respond(challenge: dict, ledger_path: str = DEFAULT_LEDGER_PATH,
            keychain: dict = None, key_index: int = None,
            force_reuse: bool = False) -> dict:
    """Answer a challenge honestly: run the task, prove possession under the nonce.

    With a `keychain`, the response additionally carries a Lamport-Merkle
    signature over sha256(canonical response-without-signature), binding the
    response to the actor's anchored key root (key_index defaults to the first
    unused index; the caller must persist the keychain's used-index mark).
    `force_reuse` exists ONLY for the planned reuse drill.

    Deterministic downstream of the challenge (and of the key material): the same
    challenge answered twice yields byte-identical responses. Raises ValueError
    on a malformed challenge.
    """
    ok, reasons = validate_challenge(challenge)
    if not ok:
        raise ValueError(f"malformed challenge: {reasons}")
    module = load_task(challenge["task_id"])
    result = module.compute()
    canonical_result = module.canonical_json(result)
    entries = work_molecule._read_ledger(ledger_path)
    tip = entries[-1]
    response = {
        "schema": SCHEMA_VERSION,
        "challenge_id": challenge["challenge_id"],
        "task_id": challenge["task_id"],
        "verifier_id": challenge["issued_for"],
        "output_hash": module.output_hash(result),
        "response_hash": compute_response_hash(challenge["nonce"], canonical_result),
        "responded_against_tip": {"index": tip["index"], "hash": tip["hash"]},
    }
    if keychain is not None:
        if key_index is None:
            # first index unused BOTH locally and on-chain (raises the named
            # EXHAUSTION reason — directing to rotation — when none remain)
            key_index = actor_identity.first_unused_index(keychain, entries)
        message = canonical_json(response).encode("utf-8")
        response["signature"] = actor_identity.sign(
            keychain, key_index, message, force_reuse=force_reuse,
            ledger_source=entries)
    return response


def validate_response(response):
    """Structurally validate a response. Returns (ok, reasons)."""
    reasons = []
    if not isinstance(response, dict):
        return (False, ["response is not a JSON object"])
    missing = [k for k in _RESPONSE_KEYS if k not in response]
    unknown = [k for k in response
               if k not in _RESPONSE_KEYS and k not in _OPTIONAL_RESPONSE_KEYS]
    if "signature" in response and not isinstance(response["signature"], dict):
        reasons.append("signature, when present, must be a JSON object")
    if missing:
        reasons.append(f"missing fields: {missing}")
    if unknown:
        reasons.append(f"unknown fields: {unknown}")
    if reasons:
        return (False, reasons)
    if response["schema"] != SCHEMA_VERSION:
        reasons.append(f"schema must be {SCHEMA_VERSION!r}")
    for key in ("challenge_id", "output_hash", "response_hash"):
        v = response[key]
        if not (isinstance(v, str) and len(v) == 64 and all(c in _HEX for c in v)):
            reasons.append(f"{key} must be a 64-char lowercase hex sha256")
    if not isinstance(response["verifier_id"], str) or not response["verifier_id"]:
        reasons.append("verifier_id must be a non-empty string")
    tip = response["responded_against_tip"]
    if not (isinstance(tip, dict) and isinstance(tip.get("index"), int)
            and isinstance(tip.get("hash"), str)):
        reasons.append("responded_against_tip must be {index: int, hash: str}")
    return (not reasons, reasons)


def signature_check(response, ledger_source, as_of_index: int = None) -> dict:
    """Check a response's OPTIONAL actor-identity signature against the ledger.

    `ledger_source` is a path or a pre-read entries list. Returns a dict:
    {signed, signer_actor_id, key_index, signature_valid, key_root_ledger_index,
    reuse_detected, reasons}. Checks, in order: (1) ONE-TIME DISCIPLINE — the
    (actor_id, key_index) pair must not appear in ANY anchored record ATTRIBUTED
    to the same root (reuse = hard reject, listed FIRST; post-rotation index
    numbering restarts honestly, so a fresh root's index 0 never collides with
    a retired root's consumed index 0); (2) the signer has an ACTIVE root at
    this chain point — resolved AS-OF via actor_identity.active_root_asof over
    the registration + rotation chain; (3) the signature verifies over
    sha256(canonical response-without-signature) under that root; (4) the
    signature's actor binds to the response's verifier_id.

    `as_of_index` is the generation-lock idiom for HISTORICAL re-verification:
    when re-verifying a round anchored at ledger index N, pass N-1 so the scan
    AND the root resolution see exactly the history the coordinator saw at
    anchor time — a record anchored under a since-retired root keeps verifying
    against ITS root forever, while a LATER legitimate/rejected use must not
    retroactively fail an earlier honest round. Live anchoring omits it: full
    history, current active root (so a NEW signature under a retired root is
    rejected). key_root_ledger_index cites the registration OR rotation record
    that made the resolved root active.
    """
    info = {"signed": False, "signer_actor_id": None, "key_index": None,
            "signature_valid": None, "key_root_ledger_index": None,
            "reuse_detected": False, "reasons": []}
    sig = response.get("signature") if isinstance(response, dict) else None
    if sig is None:
        return info
    info["signed"] = True
    if not isinstance(sig, dict):
        info["reasons"].append("signature is not a JSON object")
        return info
    info["signer_actor_id"] = sig.get("actor_id")
    info["key_index"] = sig.get("key_index")

    entries = (work_molecule._read_ledger(ledger_source)
               if isinstance(ledger_source, str) else ledger_source)
    if as_of_index is not None:
        entries = [e for e in entries
                   if isinstance(e.get("index"), int) and e["index"] <= as_of_index]

    # (2, resolved first so the reuse scan can attribute to the right root)
    # the signer must have an ACTIVE anchored root at this chain point —
    # AS-OF resolution over the registration + rotation chain (entries are
    # already bounded by as_of_index above)
    active = actor_identity.active_root_asof(sig.get("actor_id"), entries)
    if active is None:
        info["signature_valid"] = False
        info["reasons"].append(f"no anchored key root registered for actor "
                               f"{sig.get('actor_id')!r} — a signature is only "
                               "meaningful under an anchored root")
        return info
    info["key_root_ledger_index"] = active["ledger_index"]

    # (1) one-time discipline against the WHOLE anchored history — first, so a
    # violation is always the headline reason. The scan is LEDGER-WIDE and
    # CROSS-TYPE (actor_identity.anchored_key_uses: signed challenge rounds,
    # batch records such as anchored uptime epochs, AND rotation-certificate
    # signatures), ATTRIBUTED to the resolved root (uses_for_root) so rotation
    # honestly restarts index numbering. It SKIPS records of this same round
    # (same challenge_id): re-verifying an already-anchored signed round must
    # not self-collide, and same-round reuse reveals no new secrets (identical
    # message digest). Any OTHER use of the index is always caught.
    for use in actor_identity.uses_for_root(sig.get("actor_id"), entries,
                                            active["merkle_root"]):
        if (use["key_index"] == sig.get("key_index")
                and use["payload"].get("challenge_id")
                != response.get("challenge_id")):
            info["reuse_detected"] = True
            info["reasons"].insert(0,
                f"one-time key index reuse (violation of OTS discipline): "
                f"({sig.get('actor_id')!r}, index {sig.get('key_index')}) was "
                f"already used in the anchored record at ledger index "
                f"{use['ledger_index']}")
            break

    # (3) full signature verification under the as-of active root, over the
    # canonical response WITHOUT its signature field
    message = canonical_json(
        {k: v for k, v in response.items() if k != "signature"}).encode("utf-8")
    ok, sig_reasons = actor_identity.verify_signature(
        sig, active["merkle_root"], message)
    info["signature_valid"] = ok
    info["reasons"].extend(f"signature: {r}" for r in sig_reasons)

    # (4) the signature must bind to the response's verifier identity
    if sig.get("actor_id") != response.get("verifier_id"):
        info["reasons"].append(
            f"signature actor_id {sig.get('actor_id')!r} does not match the "
            f"response verifier_id {response.get('verifier_id')!r}")
    return info


def verify_response(challenge, response, ledger_path: str = DEFAULT_LEDGER_PATH,
                    as_of_index: int = None):
    """Coordinator-side verification: FULL recompute, trusting neither file.

    Re-runs the task itself, re-derives the expected response_hash from the
    challenge nonce + its OWN canonical result, and compares BOTH hashes; checks
    challenge_id integrity, the challenge/response pairing, verifier binding, and
    that both tip bindings name real, hash-re-deriving entries on the chain.
    Returns (verdict, reasons) — verdict True only if every check passes.
    """
    reasons = []
    ok, c_reasons = validate_challenge(challenge)
    if not ok:
        return (False, [f"challenge: {r}" for r in c_reasons])
    ok, r_reasons = validate_response(response)
    if not ok:
        return (False, [f"response: {r}" for r in r_reasons])

    # pairing + bindings
    if response["challenge_id"] != challenge["challenge_id"]:
        reasons.append("response answers a DIFFERENT challenge (challenge_id "
                       "mismatch — nonce reuse or replay)")
    if response["task_id"] != challenge["task_id"]:
        reasons.append(f"task mismatch: challenge is for {challenge['task_id']}, "
                       f"response claims {response['task_id']}")
    if response["verifier_id"] != challenge["issued_for"]:
        reasons.append(f"verifier binding broken: challenge was issued for "
                       f"{challenge['issued_for']!r}, response claims "
                       f"{response['verifier_id']!r}")

    # tip bindings: both cited entries must exist on the chain and re-derive
    try:
        entries = work_molecule._read_ledger(ledger_path)
    except (ValueError, json.JSONDecodeError) as exc:
        return (False, reasons + [f"ledger unreadable: {exc}"])
    by_index = {e.get("index"): e for e in entries if isinstance(e, dict)}
    for label, tip in (("ledger_tip_at_issue", challenge["ledger_tip_at_issue"]),
                       ("responded_against_tip", response["responded_against_tip"])):
        entry = by_index.get(tip["index"])
        if entry is None or entry.get("hash") != tip["hash"]:
            reasons.append(f"{label} does not name a real chain entry "
                           f"(index {tip['index']})")
            continue
        if work_molecule._ledger_entry_hash(entry) != entry.get("hash"):
            reasons.append(f"{label} entry {tip['index']} hash does not re-derive "
                           "from its content")

    # the possession recompute: run the task HERE, derive both expectations
    module = load_task(challenge["task_id"])
    result = module.compute()
    canonical_result = module.canonical_json(result)
    own_output_hash = module.output_hash(result)
    expected_response_hash = compute_response_hash(challenge["nonce"],
                                                   canonical_result)
    # HASH-ERA tolerance (append-only code-era transitions): a round recorded
    # in an earlier era validates against ITS era — the claimed output must
    # translate to the current re-run via the anchored map, and the recorded
    # response then stands as the anchored era-1 possession fact (an era-1
    # response is not recomputable without era-1 code; the transition record
    # is the vouching anchor). Identity behavior when no transition exists.
    from protocol.verifier_cli import era_expected_hash, load_hash_era_map
    _era_map = load_hash_era_map(entries)
    _era_round = (era_expected_hash(challenge["task_id"],
                                    response["output_hash"], _era_map)
                  != response["output_hash"])
    if response["output_hash"] != own_output_hash:
        if not (_era_round and era_expected_hash(
                challenge["task_id"], response["output_hash"], _era_map)
                == own_output_hash):
            reasons.append(f"output_hash mismatch: coordinator re-derived "
                           f"{own_output_hash}, response claims "
                           f"{response['output_hash']}")
    if response["response_hash"] != expected_response_hash:
        if not (_era_round and not reasons):
            reasons.append("response_hash mismatch: the responder did NOT demonstrate "
                           "possession of the full result under this nonce (a public "
                           "output_hash cannot produce it — copy attack defeated)")

    # OPTIONAL actor-identity signature: when present it must fully verify
    # against the anchored root, and the one-time discipline is enforced against
    # the whole anchored history (reuse reasons come FIRST from signature_check).
    if "signature" in response:
        sig_info = signature_check(response, entries, as_of_index=as_of_index)
        reasons = sig_info["reasons"] + reasons if sig_info["reasons"] else reasons

    return (not reasons, reasons)


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="challenge_response.py",
        description=(
            "MetaCoin challenge-response verification (research-stage, ZERO-VALUE, "
            "no token). Nonce-bound possession proof: defeats copying verification "
            "hashes from public records."
        ),
        epilog=(
            "HONEST RESIDUAL: possession-under-nonce does not prove where/when "
            "execution happened, nor exclude result-sharing between colluding "
            "parties; the public task source means anyone CAN execute. It defeats "
            "copy-from-public-records. Not consensus, not payment, not a token."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--issue", action="store_true",
                      help="issue a fresh challenge (--task + --verifier)")
    mode.add_argument("--respond", metavar="CHALLENGE_JSON",
                      help="answer a challenge honestly (runs the task)")
    mode.add_argument("--verify", nargs=2,
                      metavar=("CHALLENGE_JSON", "RESPONSE_JSON"),
                      help="coordinator verification: full recompute of both hashes")
    mode.add_argument("--selftest", action="store_true",
                      help="run the mechanical self-test (temp files only)")
    parser.add_argument("--task", help="with --issue: task id, e.g. task-0007")
    parser.add_argument("--verifier",
                        help="with --issue: verifier id the challenge is bound to")
    parser.add_argument("--keychain",
                        help="with --respond: PRIVATE keychain file (actor_identity"
                             ".py) — the response gains a Lamport-Merkle signature; "
                             "the file's used-index mark is persisted back")
    parser.add_argument("--index", type=int,
                        help="with --respond --keychain: one-time key index "
                             "(default: first unused)")
    parser.add_argument("--drill-force-index", action="store_true",
                        help="DRILL ONLY: bypass the local one-time refusal to "
                             "construct a planned reuse demonstration — ledger "
                             "verification still hard-rejects reuse")
    parser.add_argument("--ledger", default=DEFAULT_LEDGER_PATH,
                        help=f"ledger source (default: {DEFAULT_LEDGER_PATH})")
    parser.add_argument("--out", help="write the challenge/response JSON here")
    args = parser.parse_args(argv)

    if args.selftest or not (args.issue or args.respond or args.verify):
        return _selftest()

    if args.issue:
        if not (args.task and args.verifier):
            parser.error("--issue requires --task and --verifier")
        try:
            challenge = issue_challenge(args.task, args.verifier,
                                        ledger_path=args.ledger)
        except (KeyError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        text = json.dumps(challenge, indent=2, sort_keys=True)
        print(text)
        if args.out:
            with open(args.out, "w", encoding="utf-8") as f:
                f.write(text + "\n")
            print(f"wrote challenge to {args.out}", file=sys.stderr)
        return 0

    if args.respond:
        with open(args.respond, "r", encoding="utf-8") as f:
            challenge = json.load(f)
        keychain = None
        if args.keychain:
            with open(args.keychain, "r", encoding="utf-8") as f:
                keychain = json.load(f)
        try:
            response = respond(challenge, ledger_path=args.ledger,
                               keychain=keychain, key_index=args.index,
                               force_reuse=args.drill_force_index)
        except (KeyError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        if keychain is not None:
            # persist the used-index mark (the keychain file is stateful)
            with open(args.keychain, "w", encoding="utf-8") as f:
                json.dump(keychain, f, indent=2, sort_keys=True)
        text = json.dumps(response, indent=2, sort_keys=True)
        print(text)
        if args.out:
            with open(args.out, "w", encoding="utf-8") as f:
                f.write(text + "\n")
            print(f"wrote response to {args.out}", file=sys.stderr)
        return 0

    ch_path, resp_path = args.verify
    with open(ch_path, "r", encoding="utf-8") as f:
        challenge = json.load(f)
    with open(resp_path, "r", encoding="utf-8") as f:
        response = json.load(f)
    verdict, reasons = verify_response(challenge, response,
                                       ledger_path=args.ledger)
    print(f"challenge_id : {challenge.get('challenge_id')}")
    print(f"verdict      : {'VERIFIED (possession demonstrated under fresh nonce)' if verdict else 'REJECTED'}")
    for r in reasons:
        print(f"  - {r}")
    return 0 if verdict else 1


# ============================== SELF-TEST ====================================
def _selftest() -> int:
    """Mechanical self-test. Temp files only. The ATTACK FIXTURES are the point:
    the headline check, copy_attack_defeated, proves an actor holding only the
    PUBLIC output_hash cannot answer a fresh challenge."""
    import shutil
    import tempfile

    from protocol.ledger import Ledger
    import protocol.external_verifier as external_verifier
    import protocol.verifier_cli as verifier_cli

    print("=== protocol/challenge_response.py self-test (attack fixtures) ===")
    print("Nonce-bound possession proof vs the copy attack ('a hash can be copied').\n")

    root_before = set(os.listdir(_REPO_ROOT))
    proto_before = set(os.listdir(_PROTO_DIR))

    checks = []
    tmp_dir = tempfile.mkdtemp(prefix=f"challenge_response_selftest_{os.getpid()}_")
    try:
        fixture_ledger = os.path.join(tmp_dir, "ledger_fixture.jsonl")
        led = Ledger(fixture_ledger)
        led.append({"event": "ledger_genesis", "note": "selftest fixture",
                    "stage": "R-selftest", "zero_value": True, "no_token": True})
        external_verifier.evaluate_submission(
            verifier_cli.build_submission(
                "task-0001", "selftest-same-operator (simulated)",
                topology="same-machine-self-recompute"),
            led,
        )

        # [a] honest round-trip -> verified
        ch = issue_challenge("task-0001", "selftest-agent", ledger_path=fixture_ledger)
        resp = respond(ch, ledger_path=fixture_ledger)
        verdict, reasons = verify_response(ch, resp, ledger_path=fixture_ledger)
        checks.append(("honest round-trip is VERIFIED", verdict))
        if not verdict:
            for r in reasons:
                print(f"    unexpected: {r}")

        # [b] THE HEADLINE: copy attack — an attacker holds the PUBLIC output_hash
        # only, and forges a response_hash from it. Must be REJECTED, and the
        # output_hash check alone must NOT save them (it passes — that is the
        # whole point of the copy attack) — the possession check catches it.
        forged = dict(resp)
        forged["response_hash"] = hashlib.sha256(
            (ch["nonce"] + ":" + resp["output_hash"]).encode("utf-8")).hexdigest()
        verdict, reasons = verify_response(ch, forged, ledger_path=fixture_ledger)
        copy_attack_defeated = (not verdict
                                and any("possession" in r for r in reasons)
                                and not any("output_hash mismatch" in r
                                            for r in reasons))
        checks.append(("copy_attack_defeated: public hash alone CANNOT answer a "
                       "fresh challenge", copy_attack_defeated))

        # [c] nonce reuse / replay: the same response against a DIFFERENT challenge
        ch2 = issue_challenge("task-0001", "selftest-agent",
                              ledger_path=fixture_ledger)
        verdict, reasons = verify_response(ch2, resp, ledger_path=fixture_ledger)
        checks.append(("replayed response against a new challenge is rejected",
                       not verdict
                       and any("challenge_id mismatch" in r for r in reasons)))

        # [d] wrong-task response
        ch3 = issue_challenge("task-0003", "selftest-agent",
                              ledger_path=fixture_ledger)
        resp3 = respond(ch3, ledger_path=fixture_ledger)
        verdict, _ = verify_response(ch, resp3, ledger_path=fixture_ledger)
        checks.append(("wrong-task response is rejected", not verdict))

        # [e] tampered result: a responder who modified the computation fails BOTH
        # hash checks
        module = load_task("task-0001")
        bad_result = module.compute()
        bad_result["tampered"] = True
        bad_cj = module.canonical_json(bad_result)
        tampered = dict(resp)
        tampered["output_hash"] = hashlib.sha256(
            bad_cj.encode("utf-8")).hexdigest()
        tampered["response_hash"] = compute_response_hash(ch["nonce"], bad_cj)
        verdict, reasons = verify_response(ch, tampered,
                                           ledger_path=fixture_ledger)
        checks.append(("tampered result fails BOTH output and response hashes",
                       not verdict
                       and any("output_hash mismatch" in r for r in reasons)
                       and any("response_hash mismatch" in r for r in reasons)))

        # [f] verifier-binding mismatch
        stolen = dict(resp)
        stolen["verifier_id"] = "someone-else"
        verdict, reasons = verify_response(ch, stolen, ledger_path=fixture_ledger)
        checks.append(("verifier-binding mismatch is rejected",
                       not verdict
                       and any("verifier binding" in r for r in reasons)))

        # [g] determinism downstream of the challenge: honest respond twice ->
        # byte-identical
        resp_again = respond(ch, ledger_path=fixture_ledger)
        checks.append(("same challenge answered twice -> identical response bytes",
                       canonical_json(resp) == canonical_json(resp_again)))

        # [h] freshness: issued challenges never share a nonce (or an id)
        nonces = {issue_challenge("task-0001", "selftest-agent",
                                  ledger_path=fixture_ledger)["nonce"]
                  for _ in range(8)} | {ch["nonce"], ch2["nonce"]}
        checks.append(("issued challenges never share a nonce (freshness)",
                       len(nonces) == 10))

        # [i] tampered challenge (swapped binding) fails the challenge_id recompute
        evil_ch = dict(ch)
        evil_ch["issued_for"] = "attacker"
        verdict, reasons = verify_response(evil_ch, resp,
                                           ledger_path=fixture_ledger)
        checks.append(("tampered challenge fails challenge_id integrity",
                       not verdict
                       and any("challenge_id does not recompute" in r
                               for r in reasons)))
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    stray_root = sorted(set(os.listdir(_REPO_ROOT)) - root_before)
    stray_proto = sorted(set(os.listdir(_PROTO_DIR)) - proto_before)
    checks.append(("no stray files in repo root", not stray_root))
    checks.append(("no stray files in protocol/", not stray_proto))

    print("--- self-test invariants ---")
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


if __name__ == "__main__":
    sys.exit(main())
