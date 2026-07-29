"""actor_identity.py — MetaCoin ACTOR IDENTITY v0 (scheme "lamport-sha256-merkle/0.1").

================== THE MECHANISM (READ ME) ==================
Stdlib Python has no asymmetric signatures, but HASH-BASED one-time signatures
need only sha256. Lamport OTS: a private key is 256 pairs of 32-byte random
secrets; the public key is the sha256 of each secret; signing a 32-byte message
digest reveals, per bit, one secret of the pair; verification hashes the revealed
secrets against the public key. Each keypair signs EXACTLY ONCE — revealing more
than one message's secret subsets progressively leaks the private key, so index
reuse is a HARD PROTOCOL RULE, mechanically rejected wherever a signature is
verified against the ledger.

An actor's compact identity is the MERKLE ROOT over N one-time public keys
(leaf = sha256 of the canonical {index, public} object, so the leaf binds its own
index). A signature carries its key index, the 256 revealed secrets, the full
leaf public key, and the Merkle authentication path to the anchored root — so
verification needs NOTHING but the signature and the anchored root: pure,
deterministic, no key material.

SECURITY HONESTY (also on every anchored record): in the current same-operator
setting the coordinator generates and holds all keys, so a valid signature
proves KEY-POSSESSION CONTINUITY under an anchored root — NOT third-party
identity. The layer becomes identity-meaningful when an external actor generates
their OWN keychain and registers their OWN root.

DETERMINISM NOTE: key generation (secrets.token_bytes) is intentionally random —
like challenge-nonce issuance, unpredictability IS the security property.
Everything downstream — signing, verification, Merkle paths — is deterministic:
the same keychain signing the same bytes yields byte-identical signatures.

STATEFULNESS NOTE: a keychain is a STATEFUL artifact — signing marks the key
index used (used_indices), and the CLI persists that mark back to the keychain
file. The private keychain file is NEVER committed (gitignored); only the public
declaration (actor_id, scheme, key_count, merkle_root, leaf_hashes hash — no
private material) is ever registered on the ledger.

HONEST COST: hash-based one-time signatures are BIG — per key, the private and
public halves are 256x2x32 bytes (~16 KiB) each, and a signature carries ~24 KiB
(256 revealed secrets + the full leaf pubkey + the Merkle path). That is the
price of a pure-sha256 trust base; stated, not hidden.

Research-stage, ZERO-VALUE, no token. Standard library only (hashlib, secrets,
json, os, argparse). The canonical-JSON helper is deliberately per-module (house
style). Not legal, financial, investment, or security-certification advice.

Usage:
    python3 protocol/actor_identity.py --generate --actor spark-agent-same-operator --keys 32 --out keychain.json
    python3 protocol/actor_identity.py --declare keychain.json --out actor_key_declaration.json
    python3 protocol/actor_identity.py --sign keychain.json --index 0 --message-file msg.bin --out sig.json
    python3 protocol/actor_identity.py --verify sig.json --root <hex> --message-file msg.bin
    python3 protocol/actor_identity.py --selftest   # temp-only; writes nothing
"""

# Suppress __pycache__/*.pyc so importing protocol modules below leaves no stray files.
import sys
sys.dont_write_bytecode = True

import argparse
import hashlib
import json
import os
import secrets

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

_PROTO_DIR = os.path.dirname(os.path.abspath(__file__))

SCHEME = "lamport-sha256-merkle/0.1"
KEYCHAIN_SCHEMA = "actor-keychain/0.1"
DECLARATION_SCHEMA = "actor-key-declaration/0.1"

_BITS = 256  # sha256 digest bits; one secret pair per bit
_HEX = set("0123456789abcdef")


def canonical_json(obj) -> str:
    """Canonical JSON: sorted keys, compact separators, ASCII — byte-stable for hashing."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _digest_bits(message_bytes: bytes):
    """The 256 bits of sha256(message), bit i = (digest[i//8] >> (7 - i%8)) & 1
    (most-significant bit first within each byte — fixed, documented order)."""
    digest = hashlib.sha256(message_bytes).digest()
    return digest.hex(), [(digest[i // 8] >> (7 - i % 8)) & 1 for i in range(_BITS)]


def leaf_hash(index: int, public_pairs: list) -> str:
    """Merkle leaf: sha256 of the canonical {index, public} object — the leaf
    BINDS its own index, so a signature cannot claim a different key slot."""
    return _sha256_hex(canonical_json(
        {"index": index, "public": public_pairs}).encode("utf-8"))


def merkle_root_of(leaves: list) -> str:
    """Merkle root over a power-of-two leaf list; parent = sha256 of the two
    concatenated child hex digests (utf-8) — fixed, documented convention."""
    level = list(leaves)
    while len(level) > 1:
        level = [_sha256_hex((level[i] + level[i + 1]).encode("utf-8"))
                 for i in range(0, len(level), 2)]
    return level[0]


def merkle_path_for(leaves: list, index: int) -> list:
    """Authentication path from leaf `index` to the root: a list of
    {sibling, position} where position names the SIBLING's side."""
    path = []
    level = list(leaves)
    pos = index
    while len(level) > 1:
        sibling = pos ^ 1
        path.append({"sibling": level[sibling],
                     "position": "left" if sibling < pos else "right"})
        level = [_sha256_hex((level[i] + level[i + 1]).encode("utf-8"))
                 for i in range(0, len(level), 2)]
        pos //= 2
    return path


def _walk_path(leaf: str, path: list) -> str:
    node = leaf
    for step in path:
        if step["position"] == "left":
            node = _sha256_hex((step["sibling"] + node).encode("utf-8"))
        else:
            node = _sha256_hex((node + step["sibling"]).encode("utf-8"))
    return node


# ----------------------------------------------------------------------------
# Keychain construction
# ----------------------------------------------------------------------------
def build_keychain_from_privates(actor_id: str, privates: list) -> dict:
    """Assemble a keychain from given private secret pairs (used by generate and
    by the deterministic self-test fixture). privates[k] = 256 pairs of 32-byte
    hex secrets. Everything derived (publics, leaves, root) is deterministic."""
    keys = []
    leaves = []
    for k, pairs in enumerate(privates):
        public = [[_sha256_hex(bytes.fromhex(a)), _sha256_hex(bytes.fromhex(b))]
                  for a, b in pairs]
        keys.append({"index": k, "private": pairs, "public": public})
        leaves.append(leaf_hash(k, public))
    return {
        "schema": KEYCHAIN_SCHEMA,
        "actor_id": actor_id,
        "scheme": SCHEME,
        "key_count": len(privates),
        "keys": keys,
        "leaf_hashes": leaves,
        "merkle_root": merkle_root_of(leaves),
        "used_indices": [],  # stateful: sign() appends here (one-time discipline)
    }


def generate_keychain(actor_id: str, key_count: int = 32) -> dict:
    """Generate a fresh keychain of `key_count` (power of two) one-time keys.

    INTENTIONALLY RANDOM (see the DETERMINISM NOTE): secrets.token_bytes per
    secret. The private material lives only in this dict / the local file the
    CLI writes — never on the ledger, never in the repo.
    """
    if not isinstance(actor_id, str) or not actor_id:
        raise ValueError("actor_id must be a non-empty string")
    if key_count < 1 or key_count & (key_count - 1) != 0:
        raise ValueError("key_count must be a power of two (Merkle tree over keys)")
    privates = [[[secrets.token_bytes(32).hex(), secrets.token_bytes(32).hex()]
                 for _ in range(_BITS)]
                for _ in range(key_count)]
    return build_keychain_from_privates(actor_id, privates)


def public_declaration(keychain: dict) -> dict:
    """The registrable PUBLIC declaration: root + counts + a hash over the leaf
    list — deliberately NO private material and NO per-key detail (a signature
    carries its own leaf pubkey + path when the time comes)."""
    return {
        "schema": DECLARATION_SCHEMA,
        "actor_id": keychain["actor_id"],
        "scheme": keychain["scheme"],
        "key_count": keychain["key_count"],
        "merkle_root": keychain["merkle_root"],
        "leaf_hashes_hash": _sha256_hex(
            canonical_json(keychain["leaf_hashes"]).encode("utf-8")),
    }


# ----------------------------------------------------------------------------
# Sign / verify
# ----------------------------------------------------------------------------
def sign(keychain: dict, key_index: int, message_bytes: bytes,
         force_reuse: bool = False) -> dict:
    """Sign sha256(message_bytes) with one-time key `key_index`.

    Refuses locally if the keychain marks the index used (the one-time
    discipline), then marks it used — callers persisting the keychain file must
    write it back. `force_reuse=True` bypasses ONLY the local refusal and exists
    solely for the planned key-reuse DRILL — ledger-side verification still
    hard-rejects anchored reuse.
    """
    if not (0 <= key_index < keychain["key_count"]):
        raise ValueError(f"key_index {key_index} out of range "
                         f"(key_count {keychain['key_count']})")
    if key_index in keychain["used_indices"] and not force_reuse:
        raise ValueError(f"one-time key index {key_index} is already used — "
                         "refusing to sign again (OTS discipline; use a fresh "
                         "index)")
    key = keychain["keys"][key_index]
    digest_hex, bits = _digest_bits(message_bytes)
    revealed = [key["private"][i][bits[i]] for i in range(_BITS)]
    signature = {
        "schema": SCHEME,
        "actor_id": keychain["actor_id"],
        "key_index": key_index,
        "message_digest": digest_hex,
        "revealed_secrets": revealed,
        "leaf_pubkey": [list(pair) for pair in key["public"]],
        "merkle_path": merkle_path_for(keychain["leaf_hashes"], key_index),
        "merkle_root": keychain["merkle_root"],
    }
    if key_index not in keychain["used_indices"]:
        keychain["used_indices"].append(key_index)
    return signature


def anchored_key_uses(entries: list) -> list:
    """Every anchored one-time-key use across ALL signed record types.

    Returns [{actor_id, key_index, ledger_index, payload}]: a record counts as
    a key use when it carries signer facts — a signed challenge round
    (signed:true + signer_actor_id + key_index) or a batch record naming its
    consumed indices (actor_id + key_indices list, e.g. an anchored uptime
    epoch). The one-time discipline is LEDGER-WIDE: an index consumed by any
    record type is consumed for every record type.
    """
    uses = []
    for e in entries:
        p = e.get("payload") if isinstance(e, dict) else None
        if not isinstance(p, dict):
            continue
        if (p.get("signed") is True and isinstance(p.get("key_index"), int)
                and isinstance(p.get("signer_actor_id"), str)):
            uses.append({"actor_id": p["signer_actor_id"],
                         "key_index": p["key_index"],
                         "ledger_index": e.get("index"), "payload": p})
        if (isinstance(p.get("key_indices"), list)
                and isinstance(p.get("actor_id"), str)):
            for ki in p["key_indices"]:
                if isinstance(ki, int):
                    uses.append({"actor_id": p["actor_id"], "key_index": ki,
                                 "ledger_index": e.get("index"), "payload": p})
    return uses


def verify_signature(signature, expected_root: str, message_bytes: bytes):
    """Verify a Lamport-Merkle signature against `expected_root`. Returns
    (ok, reasons). Pure and deterministic; needs no key material beyond the
    signature itself."""
    reasons = []
    if not isinstance(signature, dict):
        return (False, ["signature is not a JSON object"])
    for key in ("schema", "actor_id", "key_index", "message_digest",
                "revealed_secrets", "leaf_pubkey", "merkle_path", "merkle_root"):
        if key not in signature:
            reasons.append(f"missing field {key}")
    if reasons:
        return (False, reasons)
    if signature["schema"] != SCHEME:
        reasons.append(f"schema must be {SCHEME!r}")
    digest_hex, bits = _digest_bits(message_bytes)
    if signature["message_digest"] != digest_hex:
        reasons.append("message_digest mismatch: the signature is over DIFFERENT "
                       "bytes than the presented message")
    revealed = signature["revealed_secrets"]
    pub = signature["leaf_pubkey"]
    if not (isinstance(revealed, list) and len(revealed) == _BITS
            and isinstance(pub, list) and len(pub) == _BITS):
        reasons.append("revealed_secrets and leaf_pubkey must each have 256 slots")
        return (False, reasons)
    bad_bits = 0
    for i in range(_BITS):
        secret = revealed[i]
        if not (isinstance(secret, str) and len(secret) == 64
                and all(c in _HEX for c in secret)):
            bad_bits += 1
            continue
        if _sha256_hex(bytes.fromhex(secret)) != pub[i][bits[i]]:
            bad_bits += 1
    if bad_bits:
        reasons.append(f"per-bit check failed for {bad_bits} bit position(s): "
                       "revealed secret(s) do not hash to the public key halves")
    leaf = leaf_hash(signature["key_index"], pub)
    root = _walk_path(leaf, signature["merkle_path"])
    if root != expected_root:
        reasons.append("Merkle path does not authenticate this key under the "
                       f"expected root (got {root[:16]}.., expected "
                       f"{str(expected_root)[:16]}..)")
    if signature["merkle_root"] != expected_root:
        reasons.append("signature's claimed merkle_root differs from the "
                       "anchored/expected root")
    return (not reasons, reasons)


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="actor_identity.py",
        description=(
            "MetaCoin actor identity v0 (research-stage, ZERO-VALUE, no token): "
            "pure-stdlib Lamport-sha256 one-time signatures under a Merkle key "
            "root. Each key signs EXACTLY ONCE."
        ),
        epilog=(
            "SECURITY HONESTY: in the same-operator setting a valid signature "
            "proves KEY-POSSESSION CONTINUITY under an anchored root, not "
            "third-party identity. Keychain files hold PRIVATE material — never "
            "commit them. Not consensus, not payment, not a token."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--generate", action="store_true",
                      help="generate a fresh keychain (--actor, optional --keys)")
    mode.add_argument("--declare", metavar="KEYCHAIN_JSON",
                      help="emit the registrable PUBLIC declaration (no private "
                           "material) for a keychain")
    mode.add_argument("--sign", metavar="KEYCHAIN_JSON",
                      help="sign --message-file with one-time key --index "
                           "(marks the index used and persists the keychain)")
    mode.add_argument("--verify", metavar="SIG_JSON",
                      help="verify a signature against --root and --message-file")
    mode.add_argument("--selftest", action="store_true",
                      help="run the mechanical self-test (temp files only)")
    parser.add_argument("--actor", help="actor id for --generate")
    parser.add_argument("--keys", type=int, default=32,
                        help="key count for --generate (power of two; default 32)")
    parser.add_argument("--index", type=int, help="key index for --sign")
    parser.add_argument("--message-file", help="message bytes for --sign/--verify")
    parser.add_argument("--root", help="expected Merkle root hex for --verify")
    parser.add_argument("--drill-force-index", action="store_true",
                        help="DRILL ONLY: bypass the local one-time refusal so a "
                             "planned reuse demonstration can be constructed — "
                             "ledger verification still hard-rejects reuse")
    parser.add_argument("--out", help="write the generated/derived JSON here")
    args = parser.parse_args(argv)

    if args.selftest or not (args.generate or args.declare or args.sign
                             or args.verify):
        return _selftest()

    if args.generate:
        if not args.actor:
            parser.error("--generate requires --actor")
        try:
            keychain = generate_keychain(args.actor, key_count=args.keys)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        out = args.out or "keychain.json"
        with open(out, "w", encoding="utf-8") as f:
            json.dump(keychain, f, indent=2, sort_keys=True)
        print(f"wrote PRIVATE keychain ({keychain['key_count']} one-time keys, "
              f"root {keychain['merkle_root'][:16]}..) to {out} — NEVER commit "
              "this file", file=sys.stderr)
        return 0

    if args.declare:
        with open(args.declare, "r", encoding="utf-8") as f:
            keychain = json.load(f)
        decl = public_declaration(keychain)
        text = json.dumps(decl, indent=2, sort_keys=True)
        print(text)
        if args.out:
            with open(args.out, "w", encoding="utf-8") as f:
                f.write(text + "\n")
            print(f"wrote public declaration to {args.out}", file=sys.stderr)
        return 0

    if args.sign:
        if args.index is None or not args.message_file:
            parser.error("--sign requires --index and --message-file")
        with open(args.sign, "r", encoding="utf-8") as f:
            keychain = json.load(f)
        with open(args.message_file, "rb") as f:
            message = f.read()
        try:
            signature = sign(keychain, args.index, message,
                             force_reuse=args.drill_force_index)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        # persist the used-index mark (the keychain file is stateful)
        with open(args.sign, "w", encoding="utf-8") as f:
            json.dump(keychain, f, indent=2, sort_keys=True)
        text = json.dumps(signature, indent=2, sort_keys=True)
        print(text)
        if args.out:
            with open(args.out, "w", encoding="utf-8") as f:
                f.write(text + "\n")
            print(f"wrote signature to {args.out}", file=sys.stderr)
        return 0

    if not (args.root and args.message_file):
        parser.error("--verify requires --root and --message-file")
    with open(args.verify, "r", encoding="utf-8") as f:
        signature = json.load(f)
    with open(args.message_file, "rb") as f:
        message = f.read()
    ok, reasons = verify_signature(signature, args.root, message)
    print(f"verdict: {'VALID (authenticates under the given root)' if ok else 'INVALID'}")
    for r in reasons:
        print(f"  - {r}")
    return 0 if ok else 1


# ============================== SELF-TEST ====================================
def _selftest() -> int:
    """Mechanical self-test: honest round-trip plus the forgery fixtures — every
    way a signature can lie is mechanically caught. Temp/in-memory only."""
    import copy

    print("=== protocol/actor_identity.py self-test (Lamport-sha256-Merkle OTS) ===")
    print("Each key signs EXACTLY ONCE; possession-continuity, not identity.\n")

    root_before = set(os.listdir(_REPO_ROOT))
    proto_before = set(os.listdir(_PROTO_DIR))
    checks = []

    # deterministic fixture privates (documented: sha256 counters, NOT random —
    # so [g] can assert byte-identical signatures; real keychains use secrets)
    def _fixture_privates(key_count, tag):
        return [[[_sha256_hex(f"{tag}:{k}:{i}:0".encode()),
                  _sha256_hex(f"{tag}:{k}:{i}:1".encode())]
                 for i in range(_BITS)]
                for k in range(key_count)]

    kc = build_keychain_from_privates("selftest-actor", _fixture_privates(4, "fx"))
    message = b"metacoin selftest message"

    # [a] honest sign/verify round-trip
    sig = sign(copy.deepcopy(kc), 1, message)
    ok, reasons = verify_signature(sig, kc["merkle_root"], message)
    checks.append(("honest sign/verify round-trip", ok))
    if not ok:
        for r in reasons:
            print(f"    unexpected: {r}")

    # [b] SECRET-FORGERY: flip one revealed secret -> per-bit rejection
    t = copy.deepcopy(sig)
    t["revealed_secrets"][7] = "0" * 64
    ok, reasons = verify_signature(t, kc["merkle_root"], message)
    checks.append(("secret forgery rejected (per-bit check)",
                   not ok and any("per-bit" in r for r in reasons)))

    # [c] WRONG-KEY: valid signature against a DIFFERENT root -> Merkle rejection
    kc2 = build_keychain_from_privates("selftest-actor",
                                       _fixture_privates(4, "other"))
    ok, reasons = verify_signature(sig, kc2["merkle_root"], message)
    checks.append(("wrong root rejected (Merkle authentication)",
                   not ok and any("Merkle path" in r for r in reasons)))

    # [d] BIT-TAMPER: altered message -> digest mismatch
    ok, reasons = verify_signature(sig, kc["merkle_root"], message + b"!")
    checks.append(("altered message rejected (digest mismatch)",
                   not ok and any("message_digest mismatch" in r for r in reasons)))

    # [e] MERKLE-PATH forgery -> rejected
    t = copy.deepcopy(sig)
    t["merkle_path"][0]["sibling"] = "f" * 64
    ok, reasons = verify_signature(t, kc["merkle_root"], message)
    checks.append(("forged Merkle path rejected",
                   not ok and any("Merkle path" in r for r in reasons)))

    # [f] local one-time enforcement: second sign on the same index refuses
    stateful = copy.deepcopy(kc)
    sign(stateful, 2, message)
    try:
        sign(stateful, 2, b"another message")
        checks.append(("second sign on a used index refuses (OTS discipline)",
                       False))
    except ValueError:
        checks.append(("second sign on a used index refuses (OTS discipline)",
                       True))
    # ...and force_reuse exists ONLY for the drill (bypasses the local refusal)
    drill_sig = sign(stateful, 2, b"another message", force_reuse=True)
    checks.append(("--drill-force-index path signs anyway (drill construction)",
                   isinstance(drill_sig, dict)))

    # [g] determinism: fixed keychain + fixed message -> byte-identical
    # signatures; verification is always deterministic
    s1 = sign(copy.deepcopy(kc), 0, message)
    s2 = sign(copy.deepcopy(kc), 0, message)
    checks.append(("sign is deterministic given fixed key material",
                   canonical_json(s1) == canonical_json(s2)))
    v1 = verify_signature(s1, kc["merkle_root"], message)
    v2 = verify_signature(s1, kc["merkle_root"], message)
    checks.append(("verify is deterministic", v1 == v2))

    # [h] two GENERATED keychains never share a root (randomness is the point)
    g1 = generate_keychain("selftest-actor", key_count=2)
    g2 = generate_keychain("selftest-actor", key_count=2)
    checks.append(("two generated keychains never share a root",
                   g1["merkle_root"] != g2["merkle_root"]))

    # declaration hygiene: no private material anywhere in the declaration
    decl = public_declaration(kc)
    checks.append(("public declaration carries no private material",
                   "private" not in canonical_json(decl)
                   and decl["merkle_root"] == kc["merkle_root"]))

    # honest cost, stated
    priv_bytes = _BITS * 2 * 32
    sig_bytes = len(canonical_json(sig).encode("utf-8"))
    print(f"--- honest cost: private key {priv_bytes} B, public key {priv_bytes} B "
          f"(hashes), signature ~{sig_bytes} B (canonical JSON) ---\n")

    stray_root = sorted(set(os.listdir(_REPO_ROOT)) - root_before)
    stray_proto = sorted(set(os.listdir(_PROTO_DIR)) - proto_before)
    checks.append(("no stray files in repo root", not stray_root))
    checks.append(("no stray files in protocol/", not stray_proto))

    print("--- self-test invariants ---")
    failures = 0
    for name, passed in checks:
        print(f"{name:65s}: {'PASS' if passed else 'FAIL'}")
        if not passed:
            failures += 1

    ok = failures == 0
    print("\n=== self-test summary: " +
          ("ALL CHECKS BEHAVED CORRECTLY" if ok else "FAILURE — see above") + " ===")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
