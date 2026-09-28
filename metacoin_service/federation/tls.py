"""Isolated local trust domain for the node transport: a private CA and a server certificate for loopback names,
generated once under <home>/keys (never the host certificate store). Nodes pin the CA certificate; the coordinator
serves the node routes over TLS with the server certificate. Uses the maintained `cryptography` package."""
import datetime
import ipaddress
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID


def _write(path, data):
    import os
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as f:
        f.write(data)


def ensure_node_tls(settings, hosts=('127.0.0.1', 'localhost')):
    """Create (once) ca.pem, server.pem and server-key.pem under keys/node-tls; returns their paths."""
    d = Path(settings.keys_dir) / 'node-tls'
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    ca_cert, ca_key, srv_cert, srv_key = d / 'ca.pem', d / 'ca-key.pem', d / 'server.pem', d / 'server-key.pem'
    if ca_cert.exists() and srv_cert.exists() and srv_key.exists():
        return {'ca': str(ca_cert), 'cert': str(srv_cert), 'key': str(srv_key), 'created': False}
    now = datetime.datetime.now(datetime.timezone.utc)
    cak = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'MetaCoin local node CA (test trust domain)')])
    ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name).public_key(cak.public_key()).serial_number(x509.random_serial_number())
          .not_valid_before(now - datetime.timedelta(minutes=5)).not_valid_after(now + datetime.timedelta(days=3650))
          .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
          .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True, content_commitment=False, key_encipherment=False, data_encipherment=False, key_agreement=False, encipher_only=False, decipher_only=False), critical=True)
          .sign(cak, hashes.SHA256()))
    sk = ec.generate_private_key(ec.SECP256R1())
    san = []
    for h in hosts:
        try:
            san.append(x509.IPAddress(ipaddress.ip_address(h)))
        except ValueError:
            san.append(x509.DNSName(h))
    srv = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'metacoin-coordinator (local)')])).issuer_name(ca_name).public_key(sk.public_key())
           .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(minutes=5)).not_valid_after(now + datetime.timedelta(days=825))
           .add_extension(x509.SubjectAlternativeName(san), critical=False).add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
           .add_extension(x509.ExtendedKeyUsage([x509.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
           .sign(cak, hashes.SHA256()))
    if not ca_cert.exists():
        _write(ca_cert, ca.public_bytes(serialization.Encoding.PEM))
        _write(ca_key, cak.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    if not srv_cert.exists():
        _write(srv_cert, srv.public_bytes(serialization.Encoding.PEM))
        _write(srv_key, sk.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    return {'ca': str(ca_cert), 'cert': str(srv_cert), 'key': str(srv_key), 'created': True}
