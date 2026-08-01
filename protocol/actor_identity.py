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

========== PRE-STAGED ROTATION RESERVES (IDENTITY SURVIVABILITY) ==========
A rotation certificate is valid whenever it is signed by an UNUSED key of the
actor's active root — nothing requires it to be anchored immediately.
Pre-staging exploits exactly that: generate a successor keychain + sign its
rotation certificate NOW, store both in the continuity kit, anchor NEVER —
until needed. If the active chain is later lost, exhausted, or suspected
compromised, the coordinator anchors the pre-staged certificate and the actor
continues under the successor root with ZERO dependence on the (possibly
lost) old chain beyond the already-made signature. Pre-staging is
PREPARATION, not a protocol event: staging performs ZERO ledger writes;
anchoring a reserve certificate is a deliberate coordinator act
(external_verifier.py --rotate-actor-key) that only happens IF a real
emergency or planned rotation arrives.

HONEST LIMITS, stated: pre-staging consumes one one-time index per actor NOW
(the index is spent the moment the signature exists — never reusable); a
stolen kit yields BOTH the active and successor chains — reserves raise
AVAILABILITY, not confidentiality (the kit's offline-custody warning covers
this); and a reserve is SINGLE-USE — it must be re-staged after any anchored
rotation, because its prev_root is then retired.

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
    python3 protocol/actor_identity.py --stage-reserve --actor <id> [--keys 32] [--kit-dir continuity_kit]
    python3 protocol/actor_identity.py --identity-health [--json]
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
import subprocess
import time

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
# Pre-staged rotation reserves (identity survivability) + identity health
# ----------------------------------------------------------------------------
RESERVE_SCHEMA = "identity-reserve/0.1"
HEALTH_SCHEMA = "identity-health/0.1"
_RESERVE_PREFIX = "reserve_"
RESERVE_KEYCHAIN_NAME = "successor_keychain.json"
RESERVE_CERT_NAME = "rotation_certificate.json"
RESERVE_META_NAME = "reserve.json"
DEFAULT_KIT_DIR = os.path.join(_REPO_ROOT, "continuity_kit")

RESERVE_WARNING = (
    "the reserve holds the SUCCESSOR PRIVATE KEYCHAIN — continuity-kit "
    "custody rules apply, and a stolen kit now yields BOTH the active and "
    "successor chains (reserves raise AVAILABILITY, not confidentiality). "
    "Anchor the certificate ONLY on a real emergency or planned rotation; a "
    "reserve is SINGLE-USE and must be re-staged after any anchored rotation."
)


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _dump_json(path: str, obj: dict):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, sort_keys=True)


def _default_ledger_source(base_dir: str = _REPO_ROOT) -> str:
    """The corpus this machine holds: the live ledger when present, else the
    published snapshot (a fresh clone's view)."""
    live = os.path.join(base_dir, "protocol", "ledger_data.jsonl")
    return live if os.path.exists(live) else os.path.join(
        base_dir, "protocol", "ledger_published.json")


def _tracked_destination_refusal(dest: str, base_dir: str = _REPO_ROOT):
    """Reason `dest` is unsafe for private material, or None. Mirrors
    continuity._export_destination_refusal ON PURPOSE (~15 lines) so the
    identity BASE layer keeps importing nothing above it: outside the repo is
    fine; inside the repo the path must be git-ignored."""
    dest_abs = os.path.abspath(dest)
    base_abs = os.path.abspath(base_dir)
    if not (dest_abs == base_abs or dest_abs.startswith(base_abs + os.sep)):
        return None  # outside the repo entirely
    rel = os.path.relpath(dest_abs, base_abs)
    try:
        probe = subprocess.run(["git", "check-ignore", "-q",
                                rel.rstrip("/") + "/"],
                               cwd=base_abs, capture_output=True, timeout=10)
        if probe.returncode == 0:
            return None  # inside the repo but git-ignored: safe
        return (f"destination {dest!r} is INSIDE the repository and NOT "
                "git-ignored — refusing to write a successor PRIVATE keychain "
                "into a trackable path (use a directory outside the repo, or "
                "a gitignored one like continuity_kit/)")
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        return (f"destination {dest!r} is inside the repository and git is "
                "unavailable to prove it is ignored — refusing (use a "
                "directory outside the repo)")


def find_active_keychain(actor_id: str, ledger_source, base_dir: str = _REPO_ROOT):
    """Locate the top-level keychain file holding the actor's ACTIVE root.
    Mirrors continuity.capability_sign's discovery ON PURPOSE. Returns
    (path, keychain, active_chain_element); (None, None, active) when no local
    file holds the active root; (None, None, None) when unregistered."""
    chain = root_chain(actor_id, _read_entries(ledger_source))
    if not chain:
        return (None, None, None)
    active = chain[-1]
    for name in sorted(os.listdir(base_dir)):
        if not (name.startswith("keychain") and name.endswith(".json")):
            continue
        p = os.path.join(base_dir, name)
        if not os.path.isfile(p):
            continue
        try:
            with open(p, "r", encoding="utf-8") as f:
                kc = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        if isinstance(kc, dict) and kc.get("merkle_root") == active["merkle_root"]:
            return (p, kc, active)
    return (None, None, active)


def _synthetic_rotation_entry(cert: dict, tip_index: int) -> dict:
    """A TEMP rehearsal ledger entry for `cert` — mirrors the scan-feeding
    payload shape external_verifier.rotate_actor_key anchors ON PURPOSE
    (event/status/roots + signed/signer_actor_id/key_index), so the rehearsal
    exercises the same as-of resolution and cross-type scan a real anchor
    would. Never appended to any real chain."""
    return {"index": tip_index + 1, "payload": {
        "event": _ROTATION_EVENT, "status": _ROTATION_STATUS,
        "actor_id": cert["actor_id"], "prev_root": cert["prev_root"],
        "new_root": cert["new_root"], "new_key_count": cert["new_key_count"],
        "signed": True, "signer_actor_id": cert["actor_id"],
        "key_index": cert["key_index"],
        "rehearsal_note": "TEMP in-memory anchor rehearsal — never the real "
                          "chain", "zero_value": True, "no_token": True}}


def anchor_rehearsal(cert: dict, ledger_source) -> dict:
    """The round-trip proof that a certificate WOULD anchor today, on a temp
    in-memory copy of the ledger entries (the real chain is never touched):
    (1) the certificate passes the same validation the anchor path runs;
    (2) after a rehearsal anchor the successor root is the actor's ACTIVE
    root (as-of resolution flips); (3) the signing index is consumed per the
    ledger-wide cross-type scan. Returns {ok, checks: [(name, bool)]}."""
    entries = list(_read_entries(ledger_source))
    ok0, reasons = verify_rotation_certificate(cert, entries)
    tip = max((e.get("index") for e in entries
               if isinstance(e, dict) and isinstance(e.get("index"), int)),
              default=-1)
    anchored = entries + [_synthetic_rotation_entry(cert, tip)]
    active = active_root_asof(cert["actor_id"], anchored)
    flipped = (active is not None
               and active["merkle_root"] == cert["new_root"])
    consumed = any(u["key_index"] == cert["key_index"]
                   for u in uses_for_root(cert["actor_id"], anchored,
                                          cert["prev_root"]))
    checks = [("certificate verifies against the current chain "
               "(the same validation the anchor path runs)", ok0),
              ("after the rehearsal anchor the successor root is ACTIVE "
               "(as-of resolution flips)", flipped),
              ("the signing index is consumed per the ledger-wide "
               "cross-type scan", consumed)]
    return {"ok": all(passed for _, passed in checks), "checks": checks,
            "reasons": reasons}


def scan_reserves(kit_dir: str) -> list:
    """Discover reserve directories (reserve_<actor>[...]) in a kit and check
    their INTERNAL integrity: reserve.json readable, listed files present with
    matching sha256, certificate/successor cross-consistent (successor root ==
    cert new_root, leaf-hashes hash matches, same actor), no private material
    in the certificate. Chain-state judgement is reserve_status's job.
    Returns [{dir, path, meta, certificate, successor_root, problems}]."""
    out = []
    if not os.path.isdir(kit_dir):
        return out
    for name in sorted(os.listdir(kit_dir)):
        p = os.path.join(kit_dir, name)
        if not (name.startswith(_RESERVE_PREFIX) and os.path.isdir(p)):
            continue
        row = {"dir": name, "path": p, "meta": None, "certificate": None,
               "successor_root": None, "problems": []}
        out.append(row)
        try:
            with open(os.path.join(p, RESERVE_META_NAME), "r",
                      encoding="utf-8") as f:
                meta = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            row["problems"].append(f"unreadable {RESERVE_META_NAME}: {exc}")
            continue
        row["meta"] = meta
        if meta.get("schema") != RESERVE_SCHEMA:
            row["problems"].append(f"reserve schema must be {RESERVE_SCHEMA!r}")
        for f_row in meta.get("files", []):
            fp = os.path.join(p, str(f_row.get("name", "")))
            if not os.path.isfile(fp):
                row["problems"].append(f"missing reserve file "
                                       f"{f_row.get('name')}")
            elif _sha256_file(fp) != f_row.get("sha256"):
                row["problems"].append(
                    f"{f_row.get('name')} sha256 does not match "
                    f"{RESERVE_META_NAME} — the reserve copy was altered or "
                    "corrupted")
        if row["problems"]:
            continue
        try:
            with open(os.path.join(p, RESERVE_CERT_NAME), "r",
                      encoding="utf-8") as f:
                cert = json.load(f)
            with open(os.path.join(p, RESERVE_KEYCHAIN_NAME), "r",
                      encoding="utf-8") as f:
                succ = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            row["problems"].append(f"unreadable reserve payload: {exc}")
            continue
        row["certificate"] = cert
        row["successor_root"] = succ.get("merkle_root")
        if '"private"' in canonical_json(cert):
            row["problems"].append("certificate carries PRIVATE material — a "
                                   "rotation certificate is public-only")
        if succ.get("merkle_root") != cert.get("new_root"):
            row["problems"].append("successor keychain root does not match "
                                   "the certificate's new_root")
        elif _sha256_hex(canonical_json(
                succ.get("leaf_hashes", [])).encode("utf-8")) \
                != cert.get("new_leaf_hashes_hash"):
            row["problems"].append("successor leaf hashes do not match the "
                                   "certificate's new_leaf_hashes_hash")
        if not (succ.get("actor_id") == cert.get("actor_id")
                == meta.get("actor_id")):
            row["problems"].append("actor_id disagrees between reserve.json, "
                                   "certificate, and successor keychain")
    return out


def reserve_status(res: dict, ledger_source) -> dict:
    """Judge one scanned reserve against the CURRENT chain state. States:

      staged             — internally intact AND the certificate would anchor
                           today (re-verified, full anchor-path validation)
      retired-unanchored — deliberately replaced before anchoring
                           (--replace-reserve); its signing index stays spent
      retired-stale      — prev_root has since been rotated away: this reserve
                           can NEVER anchor (FLAGGED, never silently passed)
      anchored           — the reserve's rotation actually happened: single-use
                           consumed; promote the successor and re-stage
      invalid            — tampered/inconsistent/would-not-anchor: a reserve
                           that would not anchor is WORTHLESS (named failure)
    """
    if res["problems"]:
        return {"state": "invalid", "detail": "; ".join(res["problems"])}
    meta, cert = res["meta"], res["certificate"]
    if meta.get("status") == "retired-unanchored":
        return {"state": "retired-unanchored",
                "detail": "deliberately replaced before anchoring "
                          "(retired-unanchored, never a protocol event) — "
                          "its signing index stays spent forever; superseded "
                          "by a newer reserve"}
    entries = _read_entries(ledger_source)
    chain = root_chain(cert["actor_id"], entries)
    for el in chain:
        if el["merkle_root"] == cert["new_root"]:
            return {"state": "anchored",
                    "detail": f"this reserve WAS anchored (rotation at ledger "
                              f"index {el['ledger_index']}) — single-use, now "
                              "consumed; promote the successor keychain and "
                              "stage a fresh reserve"}
    if chain and cert["prev_root"] != chain[-1]["merkle_root"]:
        detail = (f"STALE: prev_root {cert['prev_root'][:16]}.. is no longer "
                  "the active root")
        for i, el in enumerate(chain[:-1]):
            if el["merkle_root"] == cert["prev_root"]:
                detail += (f" (retired by the rotation anchored at ledger "
                           f"index {chain[i + 1]['ledger_index']})")
                break
        return {"state": "retired-stale",
                "detail": detail + " — this reserve can NEVER anchor; "
                          "re-stage under the active root"}
    ok, reasons = verify_rotation_certificate(cert, entries)
    if ok:
        return {"state": "staged",
                "detail": f"certificate re-verified against the CURRENT chain "
                          f"(would anchor today): {cert['prev_root'][:16]}.. "
                          f"-> {cert['new_root'][:16]}.., signing index "
                          f"{cert['key_index']}"}
    return {"state": "invalid",
            "detail": "certificate would NOT anchor today: "
                      + "; ".join(reasons)}


def stage_reserve(actor_id: str, kit_dir: str = DEFAULT_KIT_DIR,
                  base_dir: str = _REPO_ROOT, ledger_source=None,
                  key_count: int = 32, replace: bool = False) -> dict:
    """Pre-stage a rotation reserve for `actor_id`: generate a successor
    keychain, sign its rotation certificate with the lowest unused active
    index (the honest picker: local marks + the ledger-wide cross-type scan),
    verify it fully, and store both in kit_dir/reserve_<actor>/. ZERO ledger
    writes — the reserve is anchored NEVER, until a real emergency or planned
    rotation. The signing index is spent the moment the signature exists: the
    ACTIVE local keychain's mark is persisted before anything else can fail.

    Refuses: a trackable kit destination; a second reserve while an unanchored
    one exists (one reserve per actor — a second would burn indices
    pointlessly; `replace=True` re-stages deliberately, naming the old reserve
    retired-unanchored); an exhausted active chain (the end-of-life reason,
    via make_rotation_certificate)."""
    refusal = _tracked_destination_refusal(kit_dir, base_dir)
    if refusal:
        raise ValueError(refusal)
    if ledger_source is None:
        ledger_source = _default_ledger_source(base_dir)
    entries = _read_entries(ledger_source)
    kc_path, keychain, active = find_active_keychain(actor_id, entries,
                                                     base_dir)
    if active is None:
        raise ValueError(f"no anchored root chain for actor {actor_id!r} — "
                         "register a root before staging a reserve")
    if keychain is None:
        raise ValueError(
            f"no local keychain under {base_dir} holds {actor_id!r}'s ACTIVE "
            f"root {active['merkle_root'][:16]}.. — a reserve must be signed "
            "by the active chain (restore it from the continuity kit first)")

    # one reserve per actor: refuse while a usable (or tampered) one exists;
    # the rename itself happens only AFTER the new reserve fully verifies
    need_retire = None
    current_dir = os.path.join(kit_dir, _RESERVE_PREFIX + actor_id)
    if os.path.isdir(current_dir):
        rows = [r for r in scan_reserves(kit_dir)
                if r["dir"] == _RESERVE_PREFIX + actor_id]
        state = (reserve_status(rows[0], entries)["state"] if rows
                 else "invalid")
        if state in ("staged", "invalid") and not replace:
            raise ValueError(
                f"actor {actor_id!r} already has an unanchored reserve "
                f"({current_dir}, state {state}) — one reserve per actor: a "
                "second would burn one-time indices pointlessly. Re-stage "
                "deliberately with --replace-reserve (the old reserve is "
                "renamed and named retired-unanchored; its already-spent "
                "signing index stays spent forever)")
        need_retire = rows[0] if rows else {"meta": None}

    successor = generate_keychain(actor_id, key_count)
    # raises the end-of-life reason on exhaustion; picks the lowest index
    # unused BOTH locally and on-chain (first_unused_index, the honest picker)
    cert = make_rotation_certificate(keychain, successor,
                                     ledger_source=entries)

    # the signing index is SPENT the moment the signature exists — persist the
    # ACTIVE keychain's used-index mark FIRST, before anything else can fail
    _dump_json(kc_path, keychain)

    # a reserve that would not anchor is WORTHLESS — assert the full
    # anchor-path validation and the temp-anchor round trip NOW
    ok, reasons = verify_rotation_certificate(cert, entries)
    if not ok:
        raise RuntimeError("staged certificate failed the anchor-path "
                           "validation (the spent index stays spent "
                           "regardless): " + "; ".join(reasons))
    if '"private"' in canonical_json(cert):
        raise RuntimeError("staged certificate carries PRIVATE material — "
                           "refusing to store it as a reserve")
    rehearsal = anchor_rehearsal(cert, entries)
    if not rehearsal["ok"]:
        failed = [name for name, passed in rehearsal["checks"] if not passed]
        raise RuntimeError("temp-anchor rehearsal failed: " + "; ".join(failed))

    retired_previous = None
    if need_retire is not None:
        old_meta = need_retire.get("meta")
        tag = (str(old_meta.get("new_root", ""))[:12]
               if isinstance(old_meta, dict) else "") or "unreadable"
        target = os.path.join(kit_dir,
                              f"{_RESERVE_PREFIX}{actor_id}_retired_{tag}")
        n = 1
        while os.path.exists(target):
            n += 1
            target = os.path.join(
                kit_dir, f"{_RESERVE_PREFIX}{actor_id}_retired_{tag}_{n}")
        os.replace(current_dir, target)
        if isinstance(old_meta, dict):
            old_meta["status"] = "retired-unanchored"
            old_meta["retired_reason"] = (
                "deliberately replaced by a newer reserve before anchoring "
                "(--replace-reserve) — never a protocol event; the old "
                "signing index stays spent forever")
            old_meta["retired_at"] = time.time()
            _dump_json(os.path.join(target, RESERVE_META_NAME), old_meta)
        retired_previous = os.path.basename(target)

    os.makedirs(current_dir, exist_ok=True)
    kc_out = os.path.join(current_dir, RESERVE_KEYCHAIN_NAME)
    cert_out = os.path.join(current_dir, RESERVE_CERT_NAME)
    _dump_json(kc_out, successor)
    _dump_json(cert_out, cert)
    tip = max((e.get("index") for e in entries
               if isinstance(e, dict) and isinstance(e.get("index"), int)),
              default=None)
    meta = {
        "schema": RESERVE_SCHEMA,
        "actor_id": actor_id,
        "status": "staged",
        "prev_root": cert["prev_root"],
        "new_root": cert["new_root"],
        "new_key_count": cert["new_key_count"],
        "key_index": cert["key_index"],
        "staged_at": time.time(),
        "staged_against": {"tip_index": tip, "entry_count": len(entries)},
        "files": [{"name": RESERVE_KEYCHAIN_NAME,
                   "sha256": _sha256_file(kc_out)},
                  {"name": RESERVE_CERT_NAME,
                   "sha256": _sha256_file(cert_out)}],
        "anchor_policy": "anchor NEVER until a real emergency or planned "
                         "rotation — anchoring is a deliberate coordinator "
                         "act (external_verifier.py --rotate-actor-key), "
                         "never automatic",
        "warning": RESERVE_WARNING,
        "zero_value": True,
        "no_token": True,
    }
    _dump_json(os.path.join(current_dir, RESERVE_META_NAME), meta)
    return {"actor_id": actor_id, "prev_root": cert["prev_root"],
            "new_root": cert["new_root"],
            "new_key_count": cert["new_key_count"],
            "key_index": cert["key_index"], "active_keychain": kc_path,
            "reserve_dir": current_dir, "verified": True,
            "rehearsal": rehearsal, "retired_previous": retired_previous}


def identity_health(ledger_source=None, base_dir: str = _REPO_ROOT,
                    kit_dir: str = None) -> dict:
    """The one-screen truth per actor: active root + its ledger index, key
    counts (local + on-chain union), per-index consumption sources (ledger:N
    citations; local-only marks named, reserve signatures identified), reserve
    status, root-chain history, and NAMED risk lines. Facts and named risks
    only — no scores, no grades, no leaderboard. Read-only: ZERO writes."""
    if ledger_source is None:
        ledger_source = _default_ledger_source(base_dir)
    entries = _read_entries(ledger_source)
    if kit_dir is None:
        kit_dir = os.path.join(base_dir, "continuity_kit")
    reserves = scan_reserves(kit_dir)
    actor_ids = []
    for e in entries:
        p = e.get("payload") if isinstance(e, dict) else None
        if (isinstance(p, dict) and p.get("event") == _REGISTRATION_EVENT
                and p.get("status") == _REGISTRATION_STATUS
                and isinstance(p.get("actor_id"), str)
                and p["actor_id"] not in actor_ids):
            actor_ids.append(p["actor_id"])
    tip = None
    for e in reversed(entries):
        if isinstance(e, dict) and isinstance(e.get("index"), int):
            tip = e["index"]
            break

    actors = []
    for actor_id in actor_ids:
        chain = root_chain(actor_id, entries)
        active = chain[-1]
        total = active.get("key_count") if isinstance(
            active.get("key_count"), int) else 0
        kc_path, keychain, _ = find_active_keychain(actor_id, entries,
                                                    base_dir)
        onchain = {}
        for u in uses_for_root(actor_id, entries, active["merkle_root"]):
            if isinstance(u["key_index"], int):
                onchain.setdefault(u["key_index"], []).append(
                    u["ledger_index"])
        local = ({i for i in keychain["used_indices"] if isinstance(i, int)}
                 if keychain else set())
        consumed = sorted(set(onchain) | local)

        mine = [r for r in reserves
                if (isinstance(r["meta"], dict)
                    and r["meta"].get("actor_id") == actor_id)
                or r["dir"] == _RESERVE_PREFIX + actor_id]
        current = next((r for r in mine
                        if r["dir"] == _RESERVE_PREFIX + actor_id), None)
        if current is not None:
            st = reserve_status(current, entries)
            reserve = {"state": st["state"], "detail": st["detail"],
                       "dir": current["dir"]}
        else:
            reserve = {"state": "none", "detail": "no reserve staged",
                       "dir": None}
        reserve["retired_count"] = sum(1 for r in mine if r is not current)

        # which consumed indices are reserve-certificate signatures?
        reserve_indices = {}
        for r in mine:
            c = r.get("certificate")
            if (isinstance(c, dict)
                    and c.get("prev_root") == active["merkle_root"]
                    and isinstance(c.get("key_index"), int)):
                kind = ("staged" if r is current
                        and reserve["state"] == "staged" else "retired")
                reserve_indices[c["key_index"]] = (r["dir"], kind)
        consumed_rows = []
        for i in consumed:
            if i in onchain:
                srcs = [f"ledger:{li}" for li in onchain[i]]
            elif i in reserve_indices:
                d, kind = reserve_indices[i]
                srcs = [f"local-only: reserve rotation-certificate signature "
                        f"({d}, {kind} — kit-stored, unanchored)"]
            else:
                srcs = ["local-only (unanchored signature)"]
            consumed_rows.append({"key_index": i, "sources": srcs})
        remaining = max(0, total - len(consumed))

        risks = []
        if keychain is None:
            risks.append("no local keychain holds the ACTIVE root on this "
                         "machine — this actor cannot sign here; restore the "
                         "keychain from the continuity kit")
        if remaining == 0 and total:
            risks.append(f"active root is EXHAUSTED ({total}/{total} indices "
                         "consumed) — no new signature (not even a rotation) "
                         "can be made; only an already-staged reserve can "
                         "continue this actor")
        if reserve["state"] == "staged":
            risks.append("reserve staged, certificate verified against the "
                         "active root — identity loss is recoverable by "
                         "anchoring the reserve (single-use; re-stage after "
                         "any anchored rotation)")
        elif reserve["state"] == "retired-stale":
            risks.append("reserve is STALE (root since rotated) — flagged "
                         "retired, it can never anchor; re-stage under the "
                         "active root")
        elif reserve["state"] == "anchored":
            risks.append("reserve was ANCHORED (rotation complete) — promote "
                         "the successor keychain and stage a fresh reserve")
        elif reserve["state"] == "invalid":
            risks.append("reserve FAILS verification — it would not anchor; "
                         "investigate and re-stage: " + reserve["detail"])
        else:
            risks.append(f"{remaining}/{total} remaining, no reserve -> "
                         "stage one (--stage-reserve): identity loss is "
                         "otherwise fatal-by-default")
        if 0 < remaining <= max(2, total // 8):
            risks.append(f"only {remaining}/{total} unused one-time indices "
                         "remain on the active root — plan the anchored "
                         "rotation soon")

        actors.append({
            "actor_id": actor_id,
            "root_chain": chain,
            "active_root": active["merkle_root"],
            "active_root_ledger_index": active["ledger_index"],
            "keys_total": total,
            "keys_consumed": len(consumed),
            "keys_remaining": remaining,
            "consumed": consumed_rows,
            "local_keychain": (os.path.basename(kc_path) if kc_path
                               else None),
            "reserve": reserve,
            "risks": risks,
        })
    return {
        "schema": HEALTH_SCHEMA,
        "generated_against": {"tip_index": tip, "entry_count": len(entries)},
        "kit_dir": kit_dir,
        "actors": actors,
        "note": "facts and named risks only — no scores, no grades, no "
                "leaderboard",
        "zero_value": True,
        "no_token": True,
    }


def _print_health(doc: dict):
    print("=== identity health — facts and named risks (no scores, no "
          "leaderboard) ===")
    ga = doc["generated_against"]
    print(f"ledger: tip index {ga['tip_index']} ({ga['entry_count']} "
          f"entries); kit: {doc['kit_dir']}")
    for a in doc["actors"]:
        print(f"\nactor: {a['actor_id']}")
        parts = [f"{str(el['merkle_root'])[:16]}..({el['kind']}@"
                 f"{el['ledger_index']})" for el in a["root_chain"]]
        print(f"  root chain ({len(parts)}): " + " -> ".join(parts)
              + "  [tip is ACTIVE]")
        print(f"  keys: {a['keys_total']} total, {a['keys_consumed']} "
              f"consumed, {a['keys_remaining']} remaining (local + on-chain "
              "union)")
        for row in a["consumed"]:
            print(f"    index {row['key_index']} <- "
                  + ", ".join(row["sources"]))
        print("  local keychain: " + (a["local_keychain"] or
                                      "NONE holds the active root"))
        r = a["reserve"]
        line = f"  reserve: {r['state'].upper()}"
        if r["dir"]:
            line += f" ({r['dir']})"
        line += f" — {r['detail']}"
        if r["retired_count"]:
            line += f" [{r['retired_count']} retired reserve dir(s) kept]"
        print(line)
        for risk in a["risks"]:
            print(f"  risk: {risk}")


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
    mode.add_argument("--stage-reserve", action="store_true",
                      help="pre-stage a rotation reserve for --actor: "
                           "successor keychain + pre-signed certificate into "
                           "the continuity kit; ZERO ledger writes (anchor "
                           "NEVER until a real emergency or planned rotation)")
    mode.add_argument("--identity-health", action="store_true",
                      help="the one-screen truth per actor: roots, key "
                           "counts, consumption sources, reserve status, and "
                           "NAMED risks (facts only — no scores)")
    mode.add_argument("--selftest", action="store_true",
                      help="run the mechanical self-test (temp files only)")
    parser.add_argument("--actor", help="actor id for --generate/--stage-reserve")
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
    parser.add_argument("--kit-dir", default=DEFAULT_KIT_DIR,
                        help="with --stage-reserve/--identity-health: the "
                             "continuity-kit directory holding reserves "
                             "(default continuity_kit/; must be git-ignored "
                             "or outside the repo)")
    parser.add_argument("--replace-reserve", action="store_true",
                        help="with --stage-reserve: deliberately re-stage "
                             "over an existing unanchored reserve (the old "
                             "one is renamed and named retired-unanchored; "
                             "its spent signing index stays spent forever)")
    parser.add_argument("--json", action="store_true",
                        help="with --identity-health: emit the full JSON "
                             "document instead of the one-screen text")
    parser.add_argument("--out", help="write the generated/derived JSON here")
    args = parser.parse_args(argv)

    if args.selftest or not (args.generate or args.declare or args.sign
                             or args.verify or args.rotate
                             or args.stage_reserve or args.identity_health):
        return _selftest()

    if args.stage_reserve:
        if not args.actor:
            parser.error("--stage-reserve requires --actor")
        try:
            report = stage_reserve(args.actor, kit_dir=args.kit_dir,
                                   ledger_source=args.ledger,
                                   key_count=args.keys,
                                   replace=args.replace_reserve)
        except (ValueError, RuntimeError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(f"reserve staged for {report['actor_id']}:")
        print(f"  active root {report['prev_root'][:16]}.. -> successor root "
              f"{report['new_root'][:16]}.. "
              f"({report['new_key_count']} one-time keys)")
        print(f"  signing index {report['key_index']} of the active root is "
              f"SPENT NOW (marked in "
              f"{os.path.basename(report['active_keychain'])} — never "
              "reusable)")
        print("  certificate fully verified (the same validation the anchor "
              "path runs) and the temp-anchor rehearsal passed — it would "
              "anchor today; it is anchored NEVER until a real emergency or "
              "planned rotation")
        if report["retired_previous"]:
            print(f"  previous reserve retired-unanchored: "
                  f"{report['retired_previous']}")
        print(f"  written: {report['reserve_dir']}/ (successor keychain + "
              "certificate + reserve.json)")
        print(f"WARNING: {RESERVE_WARNING}", file=sys.stderr)
        return 0

    if args.identity_health:
        doc = identity_health(ledger_source=args.ledger,
                              kit_dir=args.kit_dir)
        if args.json:
            print(json.dumps(doc, indent=2, sort_keys=True))
        else:
            _print_health(doc)
        return 0

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
    real_ledger = os.path.join(_PROTO_DIR, "ledger_data.jsonl")
    ledger_sha_before = (_sha256_file(real_ledger)
                         if os.path.exists(real_ledger) else None)
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

    # ---------------- RESERVE (identity survivability) fixtures ----------------
    # Temp dirs only; the REAL ledger is asserted byte-identical at the end.
    import shutil
    import tempfile
    tmp = tempfile.mkdtemp(prefix=f"actor_identity_selftest_{os.getpid()}_")
    try:
        fxdir = os.path.join(tmp, "base")
        os.makedirs(fxdir)
        kit = os.path.join(tmp, "kit")
        kcR = build_keychain_from_privates("res-actor",
                                           _fixture_privates(4, "resA"))
        kcR_path = os.path.join(fxdir, "keychain_res.json")
        with open(kcR_path, "w", encoding="utf-8") as f:
            json.dump(kcR, f, indent=2, sort_keys=True)
        led_res = [_reg_entry(1, kcR), _use_entry(2, "res-actor", 0)]

        # [n1] stage: honest picker (index 0 consumed on-chain -> picks 1),
        # full verification + rehearsal, kit contents, persisted local mark
        rep = stage_reserve("res-actor", kit_dir=kit, base_dir=fxdir,
                            ledger_source=led_res)
        rdir = os.path.join(kit, _RESERVE_PREFIX + "res-actor")
        with open(kcR_path, "r", encoding="utf-8") as f:
            kc_file = json.load(f)
        checks.append(("stage-reserve: honest picker + verified + rehearsed "
                       "+ kit-stored",
                       rep["key_index"] == 1 and rep["verified"]
                       and rep["rehearsal"]["ok"]
                       and all(os.path.isfile(os.path.join(rdir, n))
                               for n in (RESERVE_KEYCHAIN_NAME,
                                         RESERVE_CERT_NAME,
                                         RESERVE_META_NAME))))
        checks.append(("stage-reserve: signing index marked consumed in the "
                       "ACTIVE local keychain (persisted)",
                       1 in kc_file["used_indices"]))

        # [n2] the round-trip proof: the staged reserve re-validates as
        # STAGED (would anchor today) and every rehearsal check passed
        rows = scan_reserves(kit)
        st = reserve_status(rows[0], led_res)
        checks.append(("staged reserve re-validates as STAGED (would anchor "
                       "today; internal integrity intact)",
                       len(rows) == 1 and not rows[0]["problems"]
                       and st["state"] == "staged"
                       and all(p for _, p in rep["rehearsal"]["checks"])))

        # [n3] double-stage refusal (one reserve per actor) + deliberate
        # --replace-reserve: old reserve retired BY NAME, next index burned
        try:
            stage_reserve("res-actor", kit_dir=kit, base_dir=fxdir,
                          ledger_source=led_res)
            checks.append(("double-stage refused (one reserve per actor)",
                           False))
        except ValueError as exc:
            checks.append(("double-stage refused (one reserve per actor)",
                           "one reserve per actor" in str(exc)
                           and "--replace-reserve" in str(exc)))
        rep2 = stage_reserve("res-actor", kit_dir=kit, base_dir=fxdir,
                             ledger_source=led_res, replace=True)
        retired = [d for d in os.listdir(kit)
                   if d.startswith(_RESERVE_PREFIX + "res-actor_retired")]
        with open(os.path.join(kit, retired[0], RESERVE_META_NAME), "r",
                  encoding="utf-8") as f:
            old_meta = json.load(f)
        checks.append(("--replace-reserve retires the old reserve BY NAME "
                       "(retired-unanchored) and burns the next index",
                       rep2["key_index"] == 2 and len(retired) == 1
                       and old_meta["status"] == "retired-unanchored"))

        # [n4] signing-index-consumed enforcement: locally, and via the
        # cross-type scan on a temp anchor (reuse named FIRST)
        with open(kcR_path, "r", encoding="utf-8") as f:
            kc_now = json.load(f)
        try:
            sign(kc_now, rep2["key_index"], b"any other message")
            checks.append(("staged signing index refuses any other "
                           "signature locally", False))
        except ValueError as exc:
            checks.append(("staged signing index refuses any other "
                           "signature locally", "already used" in str(exc)))
        with open(os.path.join(kit, _RESERVE_PREFIX + "res-actor",
                               RESERVE_CERT_NAME), "r",
                  encoding="utf-8") as f:
            cert2 = json.load(f)
        anchored = led_res + [_synthetic_rotation_entry(cert2, 2)]
        scan_sees = any(u["key_index"] == cert2["key_index"]
                        for u in uses_for_root("res-actor", anchored,
                                               cert2["prev_root"]))
        kcC = build_keychain_from_privates("res-actor",
                                          _fixture_privates(4, "resC"))
        rival = {k: v for k, v in cert2.items() if k != "signature"}
        rival["new_root"] = kcC["merkle_root"]
        rival["new_leaf_hashes_hash"] = _sha256_hex(
            canonical_json(kcC["leaf_hashes"]).encode("utf-8"))
        rival["signature"] = sign(copy.deepcopy(kcR), cert2["key_index"],
                                  canonical_json(rival).encode("utf-8"),
                                  force_reuse=True)
        ok_r, reasons_r = verify_rotation_certificate(rival, anchored)
        checks.append(("cross-type scan on a temp anchor: staged index "
                       "consumed; a rival signature is rejected, reuse FIRST",
                       scan_sees and not ok_r
                       and "one-time key index reuse" in reasons_r[0]))

        # [n5] end-of-life refusal: an exhausted active chain cannot stage
        eol_dir = os.path.join(tmp, "eol")
        os.makedirs(eol_dir)
        kcEol = build_keychain_from_privates("eol-actor",
                                             _fixture_privates(2, "eolA"))
        with open(os.path.join(eol_dir, "keychain_eol.json"), "w",
                  encoding="utf-8") as f:
            json.dump(kcEol, f, indent=2, sort_keys=True)
        led_eol = [_reg_entry(1, kcEol), _use_entry(2, "eol-actor", 0),
                   _use_entry(3, "eol-actor", 1, cid="c" * 64)]
        try:
            stage_reserve("eol-actor", kit_dir=os.path.join(tmp, "eolkit"),
                          base_dir=eol_dir, ledger_source=led_eol)
            checks.append(("exhausted chain: stage-reserve refuses with the "
                           "end-of-life reason", False))
        except ValueError as exc:
            checks.append(("exhausted chain: stage-reserve refuses with the "
                           "end-of-life reason",
                           "end of life" in str(exc)
                           and "BEFORE exhaustion" in str(exc)))

        # [n6] identity-health on fixtures: counts, citations, reserve
        # states (staged / none / retired-after-rotation), named risk lines
        doc = identity_health(ledger_source=led_res, base_dir=fxdir,
                              kit_dir=kit)
        ha = doc["actors"][0]
        srcs = {r["key_index"]: " ".join(r["sources"])
                for r in ha["consumed"]}
        checks.append(("identity-health: counts + citations correct (local "
                       "+ on-chain union; reserve signatures identified)",
                       ha["keys_total"] == 4 and ha["keys_consumed"] == 3
                       and ha["keys_remaining"] == 1
                       and "ledger:2" in srcs[0]
                       and "reserve rotation-certificate" in srcs[1]
                       and "reserve rotation-certificate" in srcs[2]))
        checks.append(("identity-health: staged reserve named in the risk "
                       "line",
                       ha["reserve"]["state"] == "staged"
                       and any("reserve staged" in r for r in ha["risks"])))
        kcH = build_keychain_from_privates("hb-actor",
                                           _fixture_privates(4, "hbA"))
        with open(os.path.join(fxdir, "keychain_hb.json"), "w",
                  encoding="utf-8") as f:
            json.dump(kcH, f, indent=2, sort_keys=True)
        led_h = led_res + [_reg_entry(3, kcH),
                           _use_entry(4, "hb-actor", 0, cid="d" * 64)]
        doc2 = identity_health(ledger_source=led_h, base_dir=fxdir,
                               kit_dir=kit)
        hb = next(x for x in doc2["actors"] if x["actor_id"] == "hb-actor")
        checks.append(("identity-health: no-reserve risk names the counts "
                       "and directs to staging",
                       hb["keys_remaining"] == 3
                       and any("no reserve -> stage one" in r
                               for r in hb["risks"])))
        kcZ = build_keychain_from_privates("res-actor",
                                           _fixture_privates(4, "resZ"))
        led_rot2 = led_h + [{"index": 5, "payload": {
            "event": _ROTATION_EVENT, "status": _ROTATION_STATUS,
            "actor_id": "res-actor", "prev_root": kcR["merkle_root"],
            "new_root": kcZ["merkle_root"], "new_key_count": 4,
            "signed": True, "signer_actor_id": "res-actor",
            "key_index": 3}}]
        doc3 = identity_health(ledger_source=led_rot2, base_dir=fxdir,
                               kit_dir=kit)
        hc = next(x for x in doc3["actors"] if x["actor_id"] == "res-actor")
        checks.append(("identity-health: reserve flagged retired after an "
                       "anchored rotation (stale, can never anchor)",
                       hc["reserve"]["state"] == "retired-stale"
                       and any("re-stage" in r for r in hc["risks"])))

        # [n7] tracked-path refusal: a trackable in-repo kit dir is refused
        # FIRST, before any key material or ledger state is touched (base_dir
        # is the real repo here, exactly as the CLI runs it)
        probe = os.path.join(_REPO_ROOT, "protocol", "reserve_refusal_probe")
        try:
            stage_reserve("res-actor", kit_dir=probe,
                          ledger_source=led_res)
            checks.append(("stage-reserve REFUSES a tracked in-repo kit "
                           "destination", False))
        except ValueError as exc:
            checks.append(("stage-reserve REFUSES a tracked in-repo kit "
                           "destination",
                           "refusing" in str(exc).lower()
                           and not os.path.exists(probe)))

        # [n8] identity-health runs read-only on the real corpus (live
        # ledger when present, else the published snapshot a clone has)
        smoke = identity_health()
        checks.append(("identity-health runs on the real corpus (read-only "
                       "smoke)",
                       isinstance(smoke, dict)
                       and len(smoke["actors"]) >= 1))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # ZERO-LEDGER-WRITES PROOF: pre-staging is preparation, not a protocol
    # event — the real ledger is byte-identical (the continuity idiom).
    if ledger_sha_before is not None:
        checks.append(("REAL ledger byte-identical before/after (ZERO "
                       "ledger writes — staging is preparation, not a "
                       "protocol event)",
                       _sha256_file(real_ledger) == ledger_sha_before))
    else:
        print("    (no real ledger on this machine — the byte-identical "
              "assertion leg is SKIPPED, named; all reserve legs above ran "
              "on temp fixtures)")

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
