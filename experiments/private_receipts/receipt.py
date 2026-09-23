"""Experimental salted Merkle disclosures. Membership only; NOT a ZK proof.

No ledger writes, signatures, encryption, payment, or claim of task correctness.
The verifier MUST obtain the expected receipt root through a trusted channel.
New encoding, isolated from MetaCoin's historic canonicalization eras.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sys

SCHEMA = "metacoin-selective-disclosure/0.1-experimental"
KIND = "salted-merkle-membership-only"
SIZE = 64
DEPTH = 6
MAX_FILE = 2 * 1024 * 1024
MAX_VALUE = 64 * 1024
MAX_EVIDENCE = 512 * 1024
DOMAIN = b"metacoin/selective-disclosure/v0/"
NAME = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
HEX = re.compile(r"[0-9a-f]{64}\Z")


class Invalid(ValueError):
    """A malformed or untrusted disclosure; fail closed."""


def _exact(obj, keys):
    if type(obj) is not dict or set(obj) != set(keys):
        raise Invalid("unexpected object fields")


def _hex(value):
    if type(value) is not str or not HEX.fullmatch(value):
        raise Invalid("expected a 32-byte lowercase hex value")
    return bytes.fromhex(value)


def _name(value):
    if type(value) is not str or not NAME.fullmatch(value):
        raise Invalid("field name must be lowercase ASCII snake_case")


def _index(value):
    if type(value) is not int or not 0 <= value < SIZE:
        raise Invalid("invalid leaf index")
    return value


def canonical(value):
    """Bounded JSON: no floats, non-finite values, duplicate keys, or coercion.

    Callers represent measurements as integers with explicit units, or decimal
    strings with an agreed contract. Do not silently round historical records.
    """
    todo = [(value, 0)]
    nodes = 0
    while todo:
        obj, depth = todo.pop()
        nodes += 1
        if depth > 16 or nodes > 20000:
            raise Invalid("JSON nesting/node limit exceeded")
        if type(obj) is dict:
            if not all(type(k) is str for k in obj):
                raise Invalid("JSON object keys must be strings")
            todo.extend((v, depth + 1) for v in obj.values())
        elif type(obj) is list:
            todo.extend((v, depth + 1) for v in obj)
        elif type(obj) is int:
            if abs(obj) > 9007199254740991:
                raise Invalid("integer exceeds interoperable JSON range")
        elif obj is None or type(obj) in (str, bool):
            pass
        else:
            raise Invalid("unsupported JSON type (floats are forbidden)")
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=True, allow_nan=False).encode("ascii")
    except (ValueError, TypeError, RecursionError) as exc:
        raise Invalid("invalid JSON") from exc
    if len(encoded) > MAX_FILE:
        raise Invalid("JSON size limit exceeded")
    return encoded


def _pairs(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise Invalid("duplicate JSON object key")
        out[key] = value
    return out


def parse(raw):
    if len(raw) > MAX_FILE:
        raise Invalid("file size limit exceeded")
    try:
        value = json.loads(raw, object_pairs_hook=_pairs)
        canonical(value)
        return value
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise Invalid("invalid or unsupported JSON") from exc


def _hash(tag, data):
    return hashlib.sha256(DOMAIN + tag + b"\0" + data).digest()


def _leaf(field):
    _exact(field, ("name", "value", "salt", "index"))
    _name(field["name"])
    index = _index(field["index"])
    data = canonical([field["name"], field["value"]])
    if len(data) > MAX_VALUE:
        raise Invalid("field value size limit exceeded")
    return _hash(b"leaf", index.to_bytes(2, "big") + _hex(field["salt"]) + data)


def _levels(leaves):
    levels = [leaves]
    for _ in range(DEPTH):
        row = levels[-1]
        levels.append([_hash(b"node", row[i] + row[i + 1])
                       for i in range(0, len(row), 2)])
    return levels


def _receipt(tree_root):
    return {"schema": SCHEMA, "kind": KIND, "tree_size": SIZE,
            "root": _hash(b"root", tree_root).hex()}


def _validate_receipt(receipt):
    _exact(receipt, ("schema", "kind", "tree_size", "root"))
    if (receipt["schema"] != SCHEMA or receipt["kind"] != KIND
            or type(receipt["tree_size"]) is not int
            or receipt["tree_size"] != SIZE):
        raise Invalid("unsupported receipt schema, kind, or tree size")
    _hex(receipt["root"])


def _vault_tree(vault):
    _exact(vault, ("schema", "receipt", "fields", "padding"))
    if vault["schema"] != SCHEMA + "/private-vault":
        raise Invalid("unsupported vault schema")
    _validate_receipt(vault["receipt"])
    fields, padding = vault["fields"], vault["padding"]
    if (type(fields) is not list or type(padding) is not list
            or not 1 <= len(fields) <= SIZE or len(fields) + len(padding) != SIZE):
        raise Invalid("vault must cover exactly 64 slots")
    leaves = [None] * SIZE
    names = set()
    total = 0
    for field in fields:
        leaf = _leaf(field)
        index = field["index"]
        if leaves[index] is not None or field["name"] in names:
            raise Invalid("duplicate vault field or slot")
        names.add(field["name"])
        leaves[index] = leaf
        total += len(canonical(field["value"]))
    if total > MAX_EVIDENCE:
        raise Invalid("evidence size limit exceeded")
    for pad in padding:
        _exact(pad, ("index", "nonce"))
        index = _index(pad["index"])
        if leaves[index] is not None:
            raise Invalid("duplicate padding slot")
        leaves[index] = _hash(b"padding", index.to_bytes(2, "big") + _hex(pad["nonce"]))
    levels = _levels(leaves)
    if _receipt(levels[-1][0]) != vault["receipt"]:
        raise Invalid("vault does not match its receipt")
    return levels


def commit(evidence):
    """Return (public receipt, PRIVATE plaintext vault). Never publish the vault."""
    if type(evidence) is not dict or not 1 <= len(evidence) <= SIZE:
        raise Invalid("evidence must contain between 1 and 64 fields")
    if len(canonical(evidence)) > MAX_EVIDENCE:
        raise Invalid("evidence size limit exceeded")
    slots = list(range(SIZE))
    secrets.SystemRandom().shuffle(slots)
    fields = [{"name": name, "value": value, "index": slots[i],
               "salt": secrets.token_hex(32)}
              for i, (name, value) in enumerate(sorted(evidence.items()))]
    leaves = [None] * SIZE
    for field in fields:
        leaves[field["index"]] = _leaf(field)
    padding = []
    for index in slots[len(fields):]:
        nonce = secrets.token_hex(32)
        padding.append({"index": index, "nonce": nonce})
        leaves[index] = _hash(b"padding", index.to_bytes(2, "big") + bytes.fromhex(nonce))
    receipt = _receipt(_levels(leaves)[-1][0])
    vault = {"schema": SCHEMA + "/private-vault", "receipt": receipt,
             "fields": fields, "padding": padding}
    # Detach all nested values from caller-owned mutable structures.
    return parse(canonical(receipt)), parse(canonical(vault))


def disclose(vault, names):
    """Open only explicitly selected fields from a locally held private vault."""
    levels = _vault_tree(vault)
    if type(names) is not list or not names or len(names) > SIZE:
        raise Invalid("select between 1 and 64 field names explicitly")
    for name in names:
        _name(name)
    if len(names) != len(set(names)):
        raise Invalid("duplicate disclosure request")
    by_name = {f["name"]: f for f in vault["fields"]}
    if not set(names) <= set(by_name):
        raise Invalid("requested field is absent")
    openings = []
    for name in sorted(names):
        field = dict(by_name[name])
        index = field["index"]
        path = []
        for level in levels[:-1]:
            path.append(level[index ^ 1].hex())
            index //= 2
        field["path"] = path
        openings.append(field)
    return parse(canonical({"receipt": vault["receipt"], "disclosures": openings}))


def verify(bundle, expected_root, required=()):
    """Return opened fields after membership checks, never a work-valid verdict.

    expected_root must be pinned independently of this untrusted bundle. A
    sender can commit lies, or replace the entire receipt if no pin is used.
    required is verifier policy; membership cannot prove omitted fields absent.
    """
    canonical(bundle)
    _hex(expected_root)
    _exact(bundle, ("receipt", "disclosures"))
    receipt = bundle["receipt"]
    _validate_receipt(receipt)
    if not hmac.compare_digest(expected_root, receipt["root"]):
        raise Invalid("receipt root does not match the independently trusted pin")
    openings = bundle["disclosures"]
    if type(openings) is not list or not 1 <= len(openings) <= SIZE:
        raise Invalid("invalid disclosure count")
    seen_slots, values = set(), {}
    for opening in openings:
        _exact(opening, ("name", "value", "salt", "index", "path"))
        field = {k: v for k, v in opening.items() if k != "path"}
        node = _leaf(field)
        index = field["index"]
        if index in seen_slots or field["name"] in values:
            raise Invalid("duplicate disclosed field or slot")
        seen_slots.add(index)
        path = opening["path"]
        if type(path) is not list or len(path) != DEPTH:
            raise Invalid("Merkle path must contain exactly six siblings")
        for sibling in path:
            sib = _hex(sibling)
            node = _hash(b"node", sib + node if index & 1 else node + sib)
            index //= 2
        if not hmac.compare_digest(_receipt(node)["root"], expected_root):
            raise Invalid("disclosure does not belong to the trusted receipt")
        values[field["name"]] = field["value"]
    for name in required:
        _name(name)
        if name not in values:
            raise Invalid("required disclosure is missing")
    return values


def read(path):
    with open(path, "rb") as stream:
        return parse(stream.read(MAX_FILE + 1))


def write_new(path, value):
    """Exclusive, mode 0600 creation: never overwrite or follow a leaf symlink."""
    data = canonical(value) + b"\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("commit")
    create.add_argument("--input", required=True)
    create.add_argument("--receipt", required=True)
    create.add_argument("--vault", required=True)
    show = sub.add_parser("disclose")
    show.add_argument("--vault", required=True)
    show.add_argument("--field", action="append", required=True)
    show.add_argument("--out", required=True)
    check = sub.add_parser("verify")
    check.add_argument("--bundle", required=True)
    check.add_argument("--expected-root", required=True)
    check.add_argument("--require", action="append", default=[])
    args = parser.parse_args(argv)
    try:
        if args.command == "commit":
            paths = [Path(args.input).resolve(), Path(args.receipt).resolve(),
                     Path(args.vault).resolve()]
            if len(set(paths)) != 3:
                raise Invalid("input, receipt, and vault paths must differ")
            if os.path.lexists(args.receipt) or os.path.lexists(args.vault):
                raise Invalid("output already exists; choose fresh paths")
            receipt, vault = commit(read(args.input))
            write_new(args.vault, vault)
            write_new(args.receipt, receipt)
            print("Committed. Vault is PRIVATE PLAINTEXT; keep it off public storage.")
        elif args.command == "disclose":
            write_new(args.out, disclose(read(args.vault), args.field))
            print("Selected fields disclosed. Opened values and their linkage are public.")
        else:
            values = verify(read(args.bundle), args.expected_root, args.require)
            print(json.dumps({"membership_verified": True, "kind": KIND,
                              "disclosed_fields": sorted(values),
                              "task_correctness_proven": False,
                              "issuer_authenticated": False}))
        return 0
    except (Invalid, OSError) as exc:
        # Never echo malformed input, hidden values, paths, salts, or key material.
        print("REFUSED: invalid input, proof, trusted pin, or file operation.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
