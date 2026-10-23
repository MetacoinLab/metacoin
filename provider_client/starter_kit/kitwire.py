"""Second, independent implementation of the wire rules used by the starter kit: canonical JSON (sorted keys, ASCII, integers only,
bounded depth), sha256 digests, domain-separated signing bytes. Deliberately written from the published rules (docs/native/PROTOCOL.md
§2), not copied from metacoin_provider.wire; agreement is checked by compat_test.py against the conformance vectors."""
import hashlib
import json

TYPES = {'intent': b'metacoin/native/v0/intent\0', 'allocation_export': b'metacoin/native/v0/export\0', 'allocation_certificate': b'metacoin/native/v0/certificate\0', 'import_ack': b'metacoin/native/v0/import-ack\0',
         'return_request': b'metacoin/native/v0/return-request\0', 'return_certificate': b'metacoin/native/v0/return-certificate\0', 'commit_checkpoint': b'metacoin/native/v0/checkpoint\0', 'envelope': b'metacoin/native/v0/envelope\0',
         'receipt': b'metacoin/native/v0/receipt\0', 'rotation': b'metacoin/native/v0/rotation\0', 'observation': b'metacoin/native/v0/observation\0'}


class KitError(ValueError):
    def __init__(self, code, detail=None):
        super().__init__(code); self.code = code; self.detail = detail or {}


def _check(v, depth=0):
    if depth > 16:
        raise KitError('canonical_depth')
    if v is None or isinstance(v, bool):
        return
    if isinstance(v, int):
        if abs(v) > (1 << 63):
            raise KitError('integer_out_of_bounds')
        return
    if isinstance(v, float):
        raise KitError('float_not_allowed')
    if isinstance(v, str):
        if len(v) > 65536:
            raise KitError('string_too_long')
        return
    if isinstance(v, list):
        for x in v:
            _check(x, depth + 1)
        return
    if isinstance(v, dict):
        for k, x in v.items():
            if not isinstance(k, str):
                raise KitError('key_not_string')
            _check(x, depth + 1)
        return
    raise KitError('type_not_allowed', {'type': type(v).__name__})


def canonical(obj):
    _check(obj); return json.dumps(obj, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode()


def digest(obj):
    return hashlib.sha256(canonical(obj)).hexdigest()


def signing_bytes(message_type, body):
    if message_type not in TYPES:
        raise KitError('unknown_message_type')
    return TYPES[message_type] + hashlib.sha256(canonical(body)).digest()


def parse_strict(raw):
    """Strict JSON: duplicate keys are an error (the service refuses them), floats are refused, depth is bounded."""
    def pairs(items):
        d = {}
        for k, v in items:
            if k in d:
                raise KitError('duplicate_key', {'key': k})
            d[k] = v
        return d
    def no_float(s):
        raise KitError('float_not_allowed')
    obj = json.loads(raw, object_pairs_hook=pairs, parse_float=no_float); _check(obj); return obj


def verify(public_hex, message, signature_hex):
    """Ed25519 verification through the `cryptography` library (an established implementation). Returns True/False, or None when the
    library is not installed (the caller must report `unverified`, never assume)."""
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        from cryptography.exceptions import InvalidSignature
    except Exception:
        return None
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_hex)).verify(bytes.fromhex(signature_hex), message); return True
    except (InvalidSignature, ValueError):
        return False


def key_id_for(public_hex):
    return 'k_' + hashlib.sha256(bytes.fromhex(public_hex)).hexdigest()[:16]
