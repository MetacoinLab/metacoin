"""Wire format of the MetaCoin native protocol as an independent, standard-library-only implementation for provider clients and
offline verifiers (order 2026-10-23 group H). Canonical JSON, message digests, domain-separated signing bytes and Ed25519
signature verification (RFC 8032, pure Python) are reproduced here without importing the service. Equality with the service
implementation is pinned by the versioned conformance fixtures (metacoin_provider/conformance/v1)."""
import hashlib
import json

PROTOCOL = 'metacoin-native/v0'
POLICY_VERSION = 'native-policy/v0'
SIGNATURE_DOMAIN = b'metacoin/review-envelope/v1\0'
MESSAGE_TYPES = {
    'intent': b'metacoin/native/v0/intent\0', 'allocation_export': b'metacoin/native/v0/export\0', 'allocation_certificate': b'metacoin/native/v0/certificate\0',
    'import_ack': b'metacoin/native/v0/import-ack\0', 'return_request': b'metacoin/native/v0/return-request\0', 'return_certificate': b'metacoin/native/v0/return-certificate\0',
    'commit_checkpoint': b'metacoin/native/v0/checkpoint\0', 'envelope': b'metacoin/native/v0/envelope\0', 'receipt': b'metacoin/native/v0/receipt\0',
    'rotation': b'metacoin/native/v0/rotation\0', 'observation': b'metacoin/native/v0/observation\0'}


class WireError(ValueError):
    def __init__(self, code, detail=None):
        super().__init__(code); self.code = code; self.detail = detail or {}


def canonical(obj):
    """Sorted keys, ASCII, integers only (no floats), bounded strings and depth; byte-identical to the service encoding."""
    def check(v, depth=0):
        if depth > 16:
            raise WireError('canonical_depth')
        if isinstance(v, bool) or v is None or isinstance(v, int):
            if isinstance(v, int) and not isinstance(v, bool) and abs(v) > (1 << 63):
                raise WireError('integer_out_of_bounds')
            return
        if isinstance(v, float):
            raise WireError('float_forbidden')
        if isinstance(v, str):
            if len(v) > 4096:
                raise WireError('string_too_long')
            return
        if isinstance(v, list):
            [check(x, depth + 1) for x in v]; return
        if isinstance(v, dict):
            for k, x in v.items():
                if not isinstance(k, str):
                    raise WireError('key_not_string')
                check(x, depth + 1)
            return
        raise WireError('unsupported_type', {'type': type(v).__name__})
    check(obj)
    return json.dumps(obj, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode()


def parse_strict(raw):
    def no_dupes(pairs):
        d = {}
        for k, v in pairs:
            if k in d:
                raise WireError('duplicate_key', {'key': k})
            d[k] = v
        return d
    try:
        return json.loads(raw, object_pairs_hook=no_dupes, parse_float=lambda s: (_ for _ in ()).throw(WireError('float_forbidden')), parse_constant=lambda s: (_ for _ in ()).throw(WireError('constant_forbidden')))
    except WireError:
        raise
    except Exception as exc:
        raise WireError('malformed_json', {'error': type(exc).__name__})


def digest(body):
    return hashlib.sha256(canonical(body)).hexdigest()


def signing_bytes(message_type, body):
    tag = MESSAGE_TYPES.get(message_type)
    if tag is None:
        raise WireError('unknown_message_type', {'type': message_type})
    return tag + hashlib.sha256(canonical(body)).digest()


def key_id_for(public_hex):
    return 'k_' + hashlib.sha256(bytes.fromhex(public_hex)).hexdigest()[:24]


# ---- Ed25519 verification (RFC 8032 §5.1), extended twisted Edwards coordinates, no third-party code -------------------------------
_P = (1 << 255) - 19
_Q = (1 << 252) + 27742317777372353535851937790883648493
_D = (-121665 * pow(121666, _P - 2, _P)) % _P
_I = pow(2, (_P - 1) // 4, _P)


def _inv(x):
    return pow(x, _P - 2, _P)


def _decode_point(s):
    if len(s) != 32:
        raise WireError('point_length')
    y = int.from_bytes(s, 'little'); sign = y >> 255; y &= (1 << 255) - 1
    if y >= _P:
        raise WireError('point_y_out_of_range')
    x2 = (y * y - 1) * _inv(_D * y * y + 1) % _P
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P:
        x = x * _I % _P
    if (x * x - x2) % _P:
        raise WireError('point_not_on_curve')
    if x == 0 and sign:
        raise WireError('point_invalid_sign')
    if (x & 1) != sign:
        x = _P - x
    return (x, y, 1, x * y % _P)


def _add(p, q):
    x1, y1, z1, t1 = p; x2, y2, z2, t2 = q
    a = (y1 - x1) * (y2 - x2) % _P; b = (y1 + x1) * (y2 + x2) % _P; c = 2 * t1 * t2 * _D % _P; d = 2 * z1 * z2 % _P
    e, f, g, h = b - a, d - c, d + c, b + a
    return (e * f % _P, g * h % _P, f * g % _P, e * h % _P)


def _mul(p, e):
    r = (0, 1, 1, 0)
    while e:
        if e & 1:
            r = _add(r, p)
        p = _add(p, p); e >>= 1
    return r


def _encode_point(p):
    x, y, z, _ = p; zi = _inv(z); x, y = x * zi % _P, y * zi % _P
    return (y | ((x & 1) << 255)).to_bytes(32, 'little')


_BY = 4 * _inv(5) % _P
_B = _decode_point(_BY.to_bytes(32, 'little'))


def ed25519_verify(public_bytes, message, signature):
    """True when `signature` is a valid Ed25519 signature of `message` under `public_bytes` (cofactorless check, as OpenSSL)."""
    try:
        if len(signature) != 64 or len(public_bytes) != 32:
            return False
        a = _decode_point(public_bytes); r = _decode_point(signature[:32]); s = int.from_bytes(signature[32:], 'little')
        if s >= _Q:
            return False
        h = int.from_bytes(hashlib.sha512(signature[:32] + public_bytes + message).digest(), 'little') % _Q
        return _encode_point(_mul(_B, s)) == _encode_point(_add(r, _mul(a, h)))
    except WireError:
        return False


def verify(public_hex, message_bytes, signature_hex):
    """Service-compatible verification: the service prefixes every signature with SIGNATURE_DOMAIN."""
    try:
        return ed25519_verify(bytes.fromhex(public_hex), SIGNATURE_DOMAIN + message_bytes, bytes.fromhex(signature_hex))
    except ValueError:
        return False
