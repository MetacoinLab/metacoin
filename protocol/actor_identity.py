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

================== ROTATION LIFECYCLE (CONSTITUTIONAL) ==================
One-time keys DEPLETE, so without rotation an actor's identity is disposable.
The rotation lifecycle ("root-rotation/0.1") closes that gap under three rules:

  * A rotation is a CRYPTOGRAPHIC HANDOFF: the new root is accepted only when
    the rotation certificate is signed by an UNUSED key of the actor's current
    active root — continuity is proven, never asserted. No unsigned or
    third-party rotation path exists.
  * History is FOREVER verifiable against the root that was active when it was
    anchored: signature verification of a historical record uses the root
    active as-of that record's index (active_root_asof — the generation-lock
    idiom applied to identity; a record anchored at ledger index N is checked
    against the root active as-of N-1, the history its coordinator saw).
    Rotation retires a root for FUTURE signing only; it rewrites nothing.
  * EXHAUSTION is enforced: when all key indices of the active root are
    consumed (per the ledger-wide cross-type scan), signing refuses with a
    named reason directing to rotation. One active root per actor at all
    times; the chain of roots is LINEAR — no forks, and a second rotation
    from an already-retired root is mechanically rejected.

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
ROTATION_SCHEMA = "root-rotation/0.1"

# The ledger record types the root-chain walk reads (registration + rotation).
_REGISTRATION_EVENT = "actor_key_registered"
_REGISTRATION_STATUS = "actor-key-registered"
_ROTATION_EVENT = "actor_key_rotated"
_ROTATION_STATUS = "actor-key-rotated"

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
         force_reuse: bool = False, ledger_source=None) -> dict:
    """Sign sha256(message_bytes) with one-time key `key_index`.

    Refuses locally if the keychain marks the index used (the one-time
    discipline), then marks it used — callers persisting the keychain file must
    write it back. With a `ledger_source` (entries list or path) the LEDGER-WIDE
    discipline is enforced at signing time too: an index consumed by ANY
    anchored record type refuses here, a root RETIRED by an anchored rotation
    refuses entirely (history stays verifiable via as-of resolution; new
    signatures do not), and full EXHAUSTION refuses with the named reason
    directing to rotation. `force_reuse=True` bypasses ONLY these refusals and
    exists solely for planned DRILL construction — ledger-side verification
    still hard-rejects anchored reuse.
    """
    if not (0 <= key_index < keychain["key_count"]):
        raise ValueError(f"key_index {key_index} out of range "
                         f"(key_count {keychain['key_count']})")
    if not force_reuse:
        local = {i for i in keychain["used_indices"] if isinstance(i, int)}
        if len(local & set(range(keychain["key_count"]))) >= keychain["key_count"]:
            first_unused_index(keychain)  # raises the named EXHAUSTION reason
        if ledger_source is not None:
            entries = _read_entries(ledger_source)
            chain = root_chain(keychain["actor_id"], entries)
            mine = [el for el in chain
                    if el["merkle_root"] == keychain["merkle_root"]]
            if mine and chain[-1]["merkle_root"] != keychain["merkle_root"]:
                succ = chain[chain.index(mine[-1]) + 1]
                raise ValueError(
                    f"root retired: {keychain['merkle_root'][:16]}.. was "
                    f"handed off by the rotation anchored at ledger index "
                    f"{succ['ledger_index']} — a retired root signs NOTHING "
                    "new (its history remains verifiable via as-of "
                    "resolution); sign under the active root "
                    f"{chain[-1]['merkle_root'][:16]}..")
            onchain = {u["key_index"]: u["ledger_index"]
                       for u in uses_for_root(keychain["actor_id"], entries,
                                              keychain["merkle_root"])}
            if len(local | set(onchain)) >= keychain["key_count"]:
                probe = dict(keychain)
                probe["used_indices"] = sorted(local | set(onchain))
                first_unused_index(probe)  # raises the named EXHAUSTION reason
            if key_index in onchain:
                raise ValueError(
                    f"one-time key index {key_index} is already CONSUMED "
                    f"on-chain (anchored record at ledger index "
                    f"{onchain[key_index]}) — the OTS discipline is "
                    "ledger-wide, not just local; use a fresh index")
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


def _read_entries(ledger_source) -> list:
    """Ledger entries from EITHER a pre-read entries list, a live JSONL path,
    or a published-snapshot path. Mirrors work_molecule._read_ledger's dual
    file format ON PURPOSE (identical entry dicts either way) — reimplemented
    in ~10 lines so the identity BASE layer keeps importing nothing above it."""
    if isinstance(ledger_source, list):
        return ledger_source
    if not os.path.exists(ledger_source):
        raise ValueError(f"ledger file does not exist: {ledger_source}")
    with open(ledger_source, "r", encoding="utf-8") as f:
        text = f.read()
    if text.lstrip().startswith("{"):
        try:
            doc = json.loads(text)
        except json.JSONDecodeError:
            doc = None
        if isinstance(doc, dict) and isinstance(doc.get("entries"), list):
            return doc["entries"]  # published-snapshot form
    return [json.loads(line) for line in text.splitlines() if line.strip()]


# ----------------------------------------------------------------------------
# Root chains: registration -> rotation -> rotation ... (linear, one active)
# ----------------------------------------------------------------------------
def root_chain(actor_id: str, ledger_source) -> list:
    """The actor's LINEAR chain of anchored roots, in ledger order.

    Walks the first confirmed registration and every confirmed rotation whose
    prev_root equals the chain tip at that point — linearity is enforced by
    the walk itself, so a hypothetical forked or out-of-order rotation record
    simply never extends the chain. Returns
    [{merkle_root, key_count, ledger_index, kind}, ...] ([] if unregistered).
    """
    chain = []
    for e in _read_entries(ledger_source):
        p = e.get("payload") if isinstance(e, dict) else None
        if not isinstance(p, dict):
            continue
        if (not chain and p.get("event") == _REGISTRATION_EVENT
                and p.get("status") == _REGISTRATION_STATUS
                and p.get("actor_id") == actor_id):
            chain.append({"merkle_root": p.get("merkle_root"),
                          "key_count": p.get("key_count"),
                          "ledger_index": e.get("index"),
                          "kind": "registration"})
        elif (chain and p.get("event") == _ROTATION_EVENT
                and p.get("status") == _ROTATION_STATUS
                and p.get("actor_id") == actor_id
                and p.get("prev_root") == chain[-1]["merkle_root"]):
            chain.append({"merkle_root": p.get("new_root"),
                          "key_count": p.get("new_key_count"),
                          "ledger_index": e.get("index"),
                          "kind": "rotation"})
    return chain


def active_root_asof(actor_id: str, ledger_source, as_of_index: int = None):
    """The actor's ACTIVE root at a chain point: the last registration/rotation
    record with ledger index <= as_of_index (full history when None).

    This is how ALL signature verification resolves its root: a record
    anchored at ledger index N verifies against active_root_asof(..., N-1) —
    the root that was active when the record was made — so history stays
    verifiable FOREVER across rotations, while a new signature (as_of None)
    is held to the current active root. Returns the chain element dict
    ({merkle_root, key_count, ledger_index, kind}) or None if unregistered.
    """
    entries = _read_entries(ledger_source)
    if as_of_index is not None:
        entries = [e for e in entries
                   if isinstance(e.get("index"), int)
                   and e["index"] <= as_of_index]
    chain = root_chain(actor_id, entries)
    return chain[-1] if chain else None


def uses_for_root(actor_id: str, ledger_source, merkle_root: str) -> list:
    """The actor's anchored one-time-key uses ATTRIBUTED to `merkle_root`.

    A use anchored at ledger index L consumed a key of the root active as-of
    L-1 (the root its coordinator verified it under), so post-rotation index
    numbering restarts honestly: (actor, index 0) under root B is a FRESH key
    even though (actor, index 0) under retired root A is consumed. Fallbacks
    are CONSERVATIVE (count the use) when attribution is impossible: an actor
    with no anchored chain (fixture ledgers), a use with no usable index, or
    the queried root absent from the chain.
    """
    entries = _read_entries(ledger_source)
    uses = [u for u in anchored_key_uses(entries) if u["actor_id"] == actor_id]
    chain = root_chain(actor_id, entries)
    if not any(el["merkle_root"] == merkle_root for el in chain):
        return uses
    out = []
    for u in uses:
        li = u.get("ledger_index")
        active = None
        if isinstance(li, int):
            candidates = [el for el in chain
                          if isinstance(el.get("ledger_index"), int)
                          and el["ledger_index"] < li]
            active = candidates[-1] if candidates else None
        if active is None or active["merkle_root"] == merkle_root:
            out.append(u)
    return out


def first_unused_index(keychain: dict, ledger_source=None) -> int:
    """First key index unused BOTH locally (used_indices) and on-chain (the
    ledger-wide cross-type scan, attributed to this keychain's root).

    Raises ValueError with the EXHAUSTION reason — naming rotation as the only
    continuation — when every index is consumed. Passing no ledger checks
    local marks only (fixture/offline use).
    """
    consumed = {i for i in keychain["used_indices"] if isinstance(i, int)}
    if ledger_source is not None:
        consumed |= {u["key_index"] for u in uses_for_root(
            keychain["actor_id"], ledger_source, keychain["merkle_root"])}
    unused = [i for i in range(keychain["key_count"]) if i not in consumed]
    if not unused:
        raise ValueError(
            f"root exhausted: all {keychain['key_count']} one-time key indices "
            f"of root {keychain['merkle_root'][:16]}.. are consumed (local "
            "marks + the ledger-wide cross-type scan) — this root can sign "
            "nothing further; rotate to a fresh root "
            "(make_rotation_certificate) to continue this actor's history")
    return unused[0]


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
# Rotation certificates: the cryptographic handoff between roots
# ----------------------------------------------------------------------------
def make_rotation_certificate(old_keychain: dict, new_keychain: dict,
                              ledger_source=None, key_index: int = None) -> dict:
    """Build the signed handoff certificate from `old_keychain`'s root to
    `new_keychain`'s root: {schema, actor_id, scheme, prev_root, new_root,
    new_key_count, new_leaf_hashes_hash, key_index, signature} — signed with
    an UNUSED old-root index over sha256(canonical cert-without-signature).

    Refuses if the actor_ids differ (a rotation hands ONE actor's identity to
    its own next root, never to another actor's) or if the old chain has no
    unused index. THE END-OF-LIFE FAILURE MODE, stated plainly: when every
    old-root key is already consumed, no rotation certificate can be signed —
    continuity has become cryptographically unprovable, and this actor's
    identity has reached end-of-life. That is WHY rotation must happen BEFORE
    exhaustion: pre-stage the next root while at least one unused key remains
    for the handoff signature. Marks the signing index used — callers
    persisting the old keychain file must write it back.
    """
    if old_keychain.get("actor_id") != new_keychain.get("actor_id"):
        raise ValueError(
            f"actor_id mismatch: {old_keychain.get('actor_id')!r} vs "
            f"{new_keychain.get('actor_id')!r} — a rotation hands ONE actor's "
            "identity to its own next root; no third-party path exists")
    if new_keychain["merkle_root"] == old_keychain["merkle_root"]:
        raise ValueError("new keychain must carry a DIFFERENT root — rotating "
                         "a root onto itself retires nothing")
    if key_index is None:
        try:
            key_index = first_unused_index(old_keychain, ledger_source)
        except ValueError:
            raise ValueError(
                "end of life: every one-time key index of the current root is "
                "already consumed, so no rotation certificate can be signed — "
                "key-possession continuity is now cryptographically "
                "unprovable. Rotation must happen BEFORE exhaustion: "
                "pre-stage the next root while an unused key remains for the "
                "handoff signature")
    cert = {
        "schema": ROTATION_SCHEMA,
        "actor_id": old_keychain["actor_id"],
        "scheme": old_keychain["scheme"],
        "prev_root": old_keychain["merkle_root"],
        "new_root": new_keychain["merkle_root"],
        "new_key_count": new_keychain["key_count"],
        "new_leaf_hashes_hash": _sha256_hex(
            canonical_json(new_keychain["leaf_hashes"]).encode("utf-8")),
        "key_index": key_index,
    }
    message = canonical_json(cert).encode("utf-8")
    cert["signature"] = sign(old_keychain, key_index, message,
                             ledger_source=ledger_source)
    return cert


def verify_rotation_certificate(cert, ledger_source, as_of_index: int = None):
    """Verify a rotation certificate against the anchored chain state. Returns
    (ok, reasons); pure and deterministic, needs no key material.

    Checks, in order: structure; the actor has an anchored root chain; (1)
    ONE-TIME DISCIPLINE — the signing index must be UNUSED under the claimed
    prev_root per the ledger-wide cross-type scan (violations listed FIRST,
    the house convention); (2) LINEAR CHAIN — prev_root must be the CURRENT
    active root (a rotation from an already-retired root is rejected: no
    forks); (3) the new root must not already exist anywhere on the chain;
    (4) the signature verifies over sha256(canonical cert-without-signature)
    under prev_root and binds to the certificate's actor and index.
    `as_of_index` is the as-of idiom for re-verifying a rotation anchored at
    index N against the history its coordinator saw (pass N-1).
    """
    if not isinstance(cert, dict):
        return (False, ["certificate is not a JSON object"])
    reasons = []
    for key in ("schema", "actor_id", "scheme", "prev_root", "new_root",
                "new_key_count", "new_leaf_hashes_hash", "key_index",
                "signature"):
        if key not in cert:
            reasons.append(f"missing field {key} (an UNSIGNED rotation does "
                           "not exist — continuity is proven, never asserted)"
                           if key == "signature" else f"missing field {key}")
    if reasons:
        return (False, reasons)
    if cert["schema"] != ROTATION_SCHEMA:
        reasons.append(f"schema must be {ROTATION_SCHEMA!r}")
    if cert["scheme"] != SCHEME:
        reasons.append(f"scheme must be {SCHEME!r}")
    for field in ("prev_root", "new_root", "new_leaf_hashes_hash"):
        v = cert[field]
        if not (isinstance(v, str) and len(v) == 64
                and all(c in _HEX for c in v)):
            reasons.append(f"{field} must be a 64-char lowercase hex sha256")
    kc = cert["new_key_count"]
    if not isinstance(kc, int) or isinstance(kc, bool) or kc < 1 or kc & (kc - 1):
        reasons.append("new_key_count must be a positive power of two")
    if not isinstance(cert["key_index"], int) or isinstance(cert["key_index"], bool):
        reasons.append("key_index must be an integer")
    if reasons:
        return (False, reasons)

    entries = _read_entries(ledger_source)
    if as_of_index is not None:
        entries = [e for e in entries
                   if isinstance(e.get("index"), int)
                   and e["index"] <= as_of_index]
    chain = root_chain(cert["actor_id"], entries)
    if not chain:
        return (False, [f"no anchored root chain for actor "
                        f"{cert['actor_id']!r} — register a root before "
                        "rotating one"])

    # (1) one-time discipline FIRST (house convention: reuse is the headline)
    if any(el["merkle_root"] == cert["prev_root"] for el in chain):
        for use in uses_for_root(cert["actor_id"], entries, cert["prev_root"]):
            if use["key_index"] == cert["key_index"]:
                reasons.append(
                    f"one-time key index reuse (violation of OTS discipline): "
                    f"the rotation certificate is signed with "
                    f"({cert['actor_id']!r}, index {cert['key_index']}), "
                    f"already consumed in the anchored record at ledger index "
                    f"{use['ledger_index']} — a rotation must be signed by an "
                    "UNUSED key of the current root")
                break

    # (2) linear chain: rotation is only valid from the CURRENT active root
    active = chain[-1]
    if cert["prev_root"] != active["merkle_root"]:
        mine = [el for el in chain if el["merkle_root"] == cert["prev_root"]]
        if mine:
            succ = chain[chain.index(mine[-1]) + 1]
            reasons.append(
                f"linear-chain violation: prev_root "
                f"{cert['prev_root'][:16]}.. was RETIRED by the rotation "
                f"anchored at ledger index {succ['ledger_index']} — the chain "
                "of roots is linear (no forks); rotation is only valid from "
                f"the current active root {active['merkle_root'][:16]}..")
        else:
            reasons.append(f"prev_root {cert['prev_root'][:16]}.. does not "
                           "name any root in this actor's anchored chain")

    # (3) the new root must be genuinely new
    if cert["new_root"] == cert["prev_root"]:
        reasons.append("new_root equals prev_root — rotating a root onto "
                       "itself retires nothing")
    for e in entries:
        p = e.get("payload") if isinstance(e, dict) else None
        if not isinstance(p, dict):
            continue
        if ((p.get("event") == _REGISTRATION_EVENT
             and p.get("status") == _REGISTRATION_STATUS
             and p.get("merkle_root") == cert["new_root"])
                or (p.get("event") == _ROTATION_EVENT
                    and p.get("status") == _ROTATION_STATUS
                    and p.get("new_root") == cert["new_root"])):
            reasons.append(f"new_root is already anchored on the ledger "
                           f"(index {e.get('index')}) — a rotation must hand "
                           "off to a FRESH root")
            break

    # (4) the handoff signature, over the canonical cert WITHOUT its signature,
    # under the claimed prev_root — plus actor/index binding
    sig = cert["signature"]
    if not isinstance(sig, dict):
        reasons.append("signature is not a JSON object")
        return (False, reasons)
    message = canonical_json(
        {k: v for k, v in cert.items() if k != "signature"}).encode("utf-8")
    ok, sig_reasons = verify_signature(sig, cert["prev_root"], message)
    reasons.extend(f"signature: {r}" for r in sig_reasons)
    if sig.get("actor_id") != cert["actor_id"]:
        reasons.append(f"signature actor_id {sig.get('actor_id')!r} does not "
                       f"match the certificate actor_id "
                       f"{cert['actor_id']!r} — signed by a different actor's "
                       "key")
    if sig.get("key_index") != cert["key_index"]:
        reasons.append("signature key_index does not match the certificate "
                       "key_index")
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
    mode.add_argument("--rotate", metavar="OLD_KEYCHAIN_JSON",
                      help="build a signed root-rotation certificate handing "
                           "off to --new-keychain (signed with an UNUSED "
                           "old-root index; the old file's used-index mark is "
                           "persisted back)")
    mode.add_argument("--selftest", action="store_true",
                      help="run the mechanical self-test (temp files only)")
    parser.add_argument("--actor", help="actor id for --generate")
    parser.add_argument("--new-keychain", metavar="NEW_KEYCHAIN_JSON",
                        help="with --rotate: the PRIVATE keychain whose root "
                             "the certificate hands off to")
    parser.add_argument("--ledger",
                        help="with --rotate/--sign: ledger source for the "
                             "ledger-wide consumed-index scan (recommended: "
                             "the real ledger or published snapshot)")
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
                             or args.verify or args.rotate):
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
                             force_reuse=args.drill_force_index,
                             ledger_source=args.ledger)
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

    if args.rotate:
        if not args.new_keychain:
            parser.error("--rotate requires --new-keychain")
        with open(args.rotate, "r", encoding="utf-8") as f:
            old_keychain = json.load(f)
        with open(args.new_keychain, "r", encoding="utf-8") as f:
            new_keychain = json.load(f)
        try:
            cert = make_rotation_certificate(old_keychain, new_keychain,
                                             ledger_source=args.ledger)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        # persist the used-index mark (the OLD keychain file is stateful)
        with open(args.rotate, "w", encoding="utf-8") as f:
            json.dump(old_keychain, f, indent=2, sort_keys=True)
        text = json.dumps(cert, indent=2, sort_keys=True)
        print(text)
        if args.out:
            with open(args.out, "w", encoding="utf-8") as f:
                f.write(text + "\n")
            print(f"wrote rotation certificate to {args.out}", file=sys.stderr)
        print(f"handoff {cert['prev_root'][:16]}.. -> {cert['new_root'][:16]}.. "
              f"signed with old-root index {cert['key_index']} — anchor via "
              "external_verifier.py --rotate-actor-key", file=sys.stderr)
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

    # ---------------- ROTATION LIFECYCLE fixtures ----------------
    # In-memory ledger fixtures (entries lists — the identity layer's ledger
    # readers are source-agnostic), exercising every constitutional rule.
    def _reg_entry(idx, chain_kc):
        return {"index": idx, "payload": {
            "event": _REGISTRATION_EVENT, "status": _REGISTRATION_STATUS,
            "actor_id": chain_kc["actor_id"],
            "merkle_root": chain_kc["merkle_root"],
            "key_count": chain_kc["key_count"]}}

    def _rot_entry(idx, cert):
        return {"index": idx, "payload": {
            "event": _ROTATION_EVENT, "status": _ROTATION_STATUS,
            "actor_id": cert["actor_id"], "prev_root": cert["prev_root"],
            "new_root": cert["new_root"],
            "new_key_count": cert["new_key_count"],
            "signed": True, "signer_actor_id": cert["actor_id"],
            "key_index": cert["key_index"]}}

    def _use_entry(idx, actor, ki, cid="a" * 64):
        return {"index": idx, "payload": {
            "event": "challenge_response_result",
            "status": "challenge-verified", "signed": True,
            "signer_actor_id": actor, "key_index": ki, "challenge_id": cid}}

    kcA = build_keychain_from_privates("rot-actor", _fixture_privates(4, "rotA"))
    kcB = build_keychain_from_privates("rot-actor", _fixture_privates(4, "rotB"))

    # [i] HONEST ROTATION round-trip: cert signed with the first index unused
    # both locally and on-chain; verifies; once anchored, as-of resolution
    # returns A before the rotation and B after it
    led = [_reg_entry(1, kcA), _use_entry(2, "rot-actor", 0)]
    kA = copy.deepcopy(kcA)
    cert = make_rotation_certificate(kA, kcB, ledger_source=led)
    ok, reasons = verify_rotation_certificate(cert, led)
    checks.append(("honest rotation certificate verifies (unused-index handoff)",
                   ok and cert["key_index"] == 1  # index 0 is consumed on-chain
                   and cert["prev_root"] == kcA["merkle_root"]
                   and cert["new_root"] == kcB["merkle_root"]))
    if not ok:
        for r in reasons:
            print(f"    unexpected: {r}")
    led_rotated = led + [_rot_entry(3, cert)]
    asof_pre = active_root_asof("rot-actor", led_rotated, as_of_index=2)
    asof_now = active_root_asof("rot-actor", led_rotated)
    checks.append(("as-of resolution: root A before the rotation, root B after",
                   asof_pre is not None and asof_now is not None
                   and asof_pre["merkle_root"] == kcA["merkle_root"]
                   and asof_now["merkle_root"] == kcB["merkle_root"]
                   and asof_now["ledger_index"] == 3))

    # [j] FORGED ROTATIONS: consumed-index signature -> rejected with the reuse
    # reason FIRST; a different actor's key -> rejected at the root; unsigned ->
    # rejected (continuity is proven, never asserted)
    content = {k: v for k, v in cert.items() if k != "signature"}
    stolen = dict(content)
    stolen["key_index"] = 0  # consumed at fixture ledger index 2
    stolen["signature"] = sign(copy.deepcopy(kcA), 0,
                               canonical_json(stolen).encode("utf-8"),
                               force_reuse=True)
    ok, reasons = verify_rotation_certificate(stolen, led)
    checks.append(("forged rotation (consumed index) rejected, reuse reason first",
                   not ok and "one-time key index reuse" in reasons[0]))
    kcX = build_keychain_from_privates("rot-actor", _fixture_privates(4, "rotX"))
    intruder = dict(content)
    intruder["signature"] = sign(copy.deepcopy(kcX), 0,
                                 canonical_json(intruder).encode("utf-8"))
    ok, reasons = verify_rotation_certificate(intruder, led)
    checks.append(("forged rotation (another chain's key) rejected at the root",
                   not ok and any("Merkle path" in r for r in reasons)))
    ok, reasons = verify_rotation_certificate(content, led)
    checks.append(("unsigned rotation rejected (no unsigned path exists)",
                   not ok and any("UNSIGNED rotation does not exist" in r
                                  for r in reasons)))

    # [k] LINEAR CHAIN: a second rotation from already-retired root A is
    # rejected — the chain of roots has no forks
    kcC = build_keychain_from_privates("rot-actor", _fixture_privates(4, "rotC"))
    fork = {k: v for k, v in cert.items() if k != "signature"}
    fork["new_root"] = kcC["merkle_root"]
    fork["new_leaf_hashes_hash"] = _sha256_hex(
        canonical_json(kcC["leaf_hashes"]).encode("utf-8"))
    fork["key_index"] = 2  # unused under A, so the LINEAR reason leads
    fork["signature"] = sign(copy.deepcopy(kcA), 2,
                             canonical_json(fork).encode("utf-8"))
    ok, reasons = verify_rotation_certificate(fork, led_rotated)
    checks.append(("rotation from a retired root rejected (linear-chain rule)",
                   not ok and any("linear-chain violation" in r
                                  for r in reasons)))
    # ...and signing anything new under the retired root refuses locally too
    try:
        sign(copy.deepcopy(kcA), 3, message, ledger_source=led_rotated)
        checks.append(("new signature under a retired root refuses", False))
    except ValueError as exc:
        checks.append(("new signature under a retired root refuses",
                       "root retired" in str(exc)))

    # [l] EXHAUSTION fixture: a 2-key chain, both indices consumed on-chain —
    # sign refuses with the rotation-directing reason, and a rotation cert
    # from it refuses with the end-of-life reason (rotate BEFORE exhaustion)
    kcE = build_keychain_from_privates("exh-actor", _fixture_privates(2, "exh"))
    led_exh = [_reg_entry(1, kcE), _use_entry(2, "exh-actor", 0),
               _use_entry(3, "exh-actor", 1, cid="b" * 64)]
    try:
        sign(copy.deepcopy(kcE), 0, message, ledger_source=led_exh)
        checks.append(("exhausted root: sign refuses, directing to rotation",
                       False))
    except ValueError as exc:
        checks.append(("exhausted root: sign refuses, directing to rotation",
                       "root exhausted" in str(exc)
                       and "make_rotation_certificate" in str(exc)))
    kcE2 = build_keychain_from_privates("exh-actor", _fixture_privates(2, "exh2"))
    try:
        make_rotation_certificate(copy.deepcopy(kcE), kcE2,
                                  ledger_source=led_exh)
        checks.append(("exhausted root: rotation refuses with the end-of-life "
                       "reason", False))
    except ValueError as exc:
        checks.append(("exhausted root: rotation refuses with the end-of-life "
                       "reason", "end of life" in str(exc)
                       and "BEFORE exhaustion" in str(exc)))

    # [m] HISTORICAL CONTINUITY: a signature made under root A (the anchored
    # use at fixture index 2) still verifies against the as-of root AFTER the
    # rotation to B — while the same signature FAILS against the now-active
    # root (a new signature cannot hide under a retired root)
    hist_sig = sign(copy.deepcopy(kcA), 0, message)  # root-A signature
    root_asof = active_root_asof("rot-actor", led_rotated, as_of_index=1)
    ok_hist, _ = verify_signature(hist_sig, root_asof["merkle_root"], message)
    ok_now, _ = verify_signature(hist_sig, asof_now["merkle_root"], message)
    checks.append(("historical record verifies via as-of root; the retired "
                   "root authenticates nothing NEW", ok_hist and not ok_now))

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
