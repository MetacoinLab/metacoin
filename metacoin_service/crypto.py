"""Key custody: age (pyrage) for artifact encryption, Ed25519 (cryptography) for review signatures.

Encryption and signing keys are distinct files under <home>/keys (0700/0600),
outside the database, exports and logs. The service holds the reviewer keys
(server-managed custody after authenticated reviewer authorization); this is
stated in the capability output and is NOT non-custodial review.
Trust for verification comes from the reviewer_keys table, never from a key
embedded in an envelope.
"""
import hashlib
import os
from pathlib import Path
from .errors import ServiceError

try:
    import pyrage
    from pyrage import x25519 as _age
    AGE_AVAILABLE = True
except ImportError:            # capability error, never plaintext fallback
    pyrage = None
    AGE_AVAILABLE = False
try:
    from cryptography.hazmat.primitives.asymmetric import ed25519 as _ed
    from cryptography.hazmat.primitives import serialization as _ser
    from cryptography.exceptions import InvalidSignature
    ED25519_AVAILABLE = True
except ImportError:
    ED25519_AVAILABLE = False

ARTIFACT_FORMAT = 'age-x25519/v1'
SIGNATURE_DOMAIN = b'metacoin/review-envelope/v1\0'


def _write_private(path, data):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _read_private(path):
    path = Path(path)
    info = path.lstat()
    if info.st_mode & 0o077 or not path.is_file():
        raise ServiceError('CAPABILITY_UNAVAILABLE', 'key file permissions')
    return path.read_bytes()


# ---- age ------------------------------------------------------------------
def require_age():
    if not AGE_AVAILABLE:
        raise ServiceError('CAPABILITY_UNAVAILABLE', 'age encryption (pyrage) not installed')


def generate_age_identity(path):
    require_age()
    identity = _age.Identity.generate()
    _write_private(path, str(identity).encode('ascii') + b'\n')
    return str(identity.to_public())


def load_age_identity(path):
    require_age()
    if not Path(path).exists():
        raise ServiceError('CAPABILITY_UNAVAILABLE', 'decryption identity missing; restore the key material')
    return _age.Identity.from_str(_read_private(path).decode('ascii').strip())


def age_recipient(public_str):
    require_age()
    return _age.Recipient.from_str(public_str)


def encrypt_bytes(plaintext, recipient_public_strs):
    require_age()
    if not recipient_public_strs:
        raise ServiceError('VALIDATION', 'recipients')
    return pyrage.encrypt(plaintext, [age_recipient(r) for r in recipient_public_strs])


def decrypt_bytes(ciphertext, identity):
    require_age()
    try:
        return pyrage.decrypt(ciphertext, [identity])
    except pyrage.DecryptError as exc:
        raise ServiceError('FORBIDDEN', 'not a recipient of this artifact or ciphertext altered') from None


# ---- Ed25519 --------------------------------------------------------------
def require_ed25519():
    if not ED25519_AVAILABLE:
        raise ServiceError('CAPABILITY_UNAVAILABLE', 'Ed25519 (cryptography) not installed')


def generate_signing_key(path):
    require_ed25519()
    key = _ed.Ed25519PrivateKey.generate()
    raw = key.private_bytes(_ser.Encoding.Raw, _ser.PrivateFormat.Raw, _ser.NoEncryption())
    _write_private(path, raw)
    return key.public_key().public_bytes(_ser.Encoding.Raw, _ser.PublicFormat.Raw).hex()


def load_signing_key(path):
    require_ed25519()
    if not Path(path).exists():
        raise ServiceError('CAPABILITY_UNAVAILABLE', 'signing key missing')
    return _ed.Ed25519PrivateKey.from_private_bytes(_read_private(path))


def key_id_for(public_hex):
    return 'k_' + hashlib.sha256(bytes.fromhex(public_hex)).hexdigest()[:24]


def sign(private_key, message_bytes):
    return private_key.sign(SIGNATURE_DOMAIN + message_bytes).hex()


def verify(public_hex, message_bytes, signature_hex):
    require_ed25519()
    try:
        _ed.Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_hex)).verify(bytes.fromhex(signature_hex), SIGNATURE_DOMAIN + message_bytes)
        return True
    except (InvalidSignature, ValueError):
        return False
