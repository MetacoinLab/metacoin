"""Constrained artifact exchange: a PUBLIC review package and a PRIVATE audit package.

Both are small zip files with a fixed member list and a manifest of sha256
hashes. Import parses data only; nothing in a package is ever executed. A
manifest establishes consistency of the included files relative to the
manifest, not who published them, whether the science is right, or whether
any payment is due. Trust pins (contract digest, evidence root) must come from
the operator through an independently trusted channel; the copy inside a
package is informational and only compared, never used as the pin.
"""
import hashlib
import os
from pathlib import Path
import stat
import zipfile
from experiments.private_receipts import receipt as merkle
from . import acceptance, contract as terms

PUBLIC_SCHEMA = 'metacoin-work-contract-public-package/v1'
PRIVATE_SCHEMA = 'metacoin-work-contract-private-audit-package/v1'
PUBLIC_MEMBERS = ('manifest.json', 'contract.json', 'public-bundle.json', 'pins.json', 'README.txt')
PRIVATE_MEMBERS = ('manifest.json', 'contract.json', 'private-input-vault.json',
                   'private-evidence-vault.json', 'PRIVATE-README.txt')
MAX_MEMBER = merkle.MAX_FILE          # 2 MiB per member (matches the JSON reader)
MAX_TOTAL = 4 * merkle.MAX_FILE       # 8 MiB extracted
MAX_ARCHIVE = 2 * merkle.MAX_FILE     # 4 MiB on disk
PRIVATE_MARKER = ('"' + merkle.SCHEMA + '/private-vault"').encode('ascii')

PUBLIC_README = b"""PUBLIC review package (MetaCoin WorkContract v0 experiment)

Contents: versioned contract terms, the permitted public openings (a salted
Merkle membership bundle), informational pins, and this manifest of hashes.
Verify with the operator's OWN pins, obtained independently of this package:

  python3 -m experiments.work_contracts.cli import-public --package <this.zip> \\
      --expected-contract-digest <pin> --expected-root <root> --out-dir <fresh dir>

What a green result establishes: the opened fields belong to the pinned result
root and match the contract bindings. What it does not establish: hidden
computation correctness, publisher identity, anonymity, or payment eligibility.
No private inputs, vaults, journals, keys or customer data belong in this file.
"""

PRIVATE_README = b"""PRIVATE AUDIT PACKAGE - do not publish, upload, or forward.

Contains PLAINTEXT private vaults (inputs and full evidence) for an authorized
auditor to recompute the result locally:

  python3 -m experiments.work_contracts.cli import-private --package <this.zip> --out-dir <fresh private dir>
  python3 -m experiments.work_contracts.cli record-audit ... (or the stateless `audit`)

Producing this file does not authorize sending it to anyone. Exchange it only
through the channel the owner and auditor agreed on in the contract intake.
"""


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _write_zip(path, members):
    """Exclusive 0600 creation; members is an ordered {name: bytes}."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            for name, data in members.items():
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))  # deterministic
                info.external_attr = (stat.S_IFREG | 0o600) << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, data)
        stream.flush()
        os.fsync(stream.fileno())


def _manifest(schema, files, extra):
    return merkle.canonical(dict(extra, schema=schema, files={name: _sha(data) for name, data in files.items()})) + b'\n'


def export_public(contract, expected_contract_digest, bundle, expected_root, out_path):
    """Refuses to package anything that does not verify against the operator's pins."""
    public = acceptance.verify_public(contract, expected_contract_digest, bundle, expected_root)
    files = {'contract.json': merkle.canonical(contract) + b'\n',
             'public-bundle.json': merkle.canonical(bundle) + b'\n',
             'pins.json': merkle.canonical({'expected_contract_digest': expected_contract_digest,
                                            'expected_evidence_root': expected_root,
                                            'trust_source': 'package-supplied-informational;'
                                                            'obtain-pins-independently-of-the-sender'}) + b'\n',
             'README.txt': PUBLIC_README}
    for data in files.values():
        if PRIVATE_MARKER in data:
            raise merkle.Invalid('package contains private material')
    manifest = _manifest(PUBLIC_SCHEMA, files, {'verifier_id': contract['verifier_id'],
                                                'verifier_status': public['verifier_status'],
                                                'disclosed_fields': sorted(public['disclosed'])})
    _write_zip(out_path, {'manifest.json': manifest, **files})
    return {'exported': True, 'package': PUBLIC_SCHEMA, 'manifest_sha256': _sha(manifest),
            'members': list(PUBLIC_MEMBERS), 'verifier_status': public['verifier_status'],
            'trust': 'manifest-consistency-only;publisher-not-authenticated'}


def export_private(contract, input_vault, evidence_vault, out_path):
    merkle._vault_tree(input_vault)
    merkle._vault_tree(evidence_vault)
    terms.validate(contract, mode='historical')
    files = {'contract.json': merkle.canonical(contract) + b'\n',
             'private-input-vault.json': merkle.canonical(input_vault) + b'\n',
             'private-evidence-vault.json': merkle.canonical(evidence_vault) + b'\n',
             'PRIVATE-README.txt': PRIVATE_README}
    manifest = _manifest(PRIVATE_SCHEMA, files, {'privacy': 'PRIVATE-PLAINTEXT-VAULTS;do-not-publish'})
    _write_zip(out_path, {'manifest.json': manifest, **files})
    return {'exported': True, 'package': PRIVATE_SCHEMA, 'manifest_sha256': _sha(manifest),
            'privacy': 'PRIVATE-PLAINTEXT-VAULTS;do-not-publish;exchange-only-via-agreed-channel'}


def _read_members(path, allowed):
    """Strict zip reading: whitelist names, regular files only, bounded sizes, no duplicates."""
    if os.stat(path).st_size > MAX_ARCHIVE:
        raise merkle.Invalid('package member too large')
    members, total = {}, 0
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            if info.filename in members:
                raise merkle.Invalid('package member duplicated')
            if info.filename not in allowed:            # covers traversal, absolute paths, extras
                raise merkle.Invalid('package member not permitted')
            mode = info.external_attr >> 16
            if info.is_dir() or (mode and not stat.S_ISREG(mode)):
                raise merkle.Invalid('package member is not a regular file')
            if info.file_size > MAX_MEMBER:
                raise merkle.Invalid('package member too large')
            with archive.open(info) as stream:
                data = stream.read(MAX_MEMBER + 1)
            if len(data) != info.file_size or len(data) > MAX_MEMBER:
                raise merkle.Invalid('package member too large')
            total += len(data)
            if total > MAX_TOTAL:
                raise merkle.Invalid('package member too large')
            members[info.filename] = data
    if set(members) != set(allowed):
        raise merkle.Invalid('package manifest mismatch')
    return members


def _check_manifest(members, schema):
    manifest = merkle.parse(members['manifest.json'])
    if type(manifest) is not dict or manifest.get('schema') != schema:
        raise merkle.Invalid('unsupported package version')
    files = manifest.get('files')
    if type(files) is not dict or set(files) != set(members) - {'manifest.json'}:
        raise merkle.Invalid('package manifest mismatch')
    for name, digest in files.items():
        if _sha(members[name]) != digest:
            raise merkle.Invalid('package manifest mismatch')
    return manifest


def _extract(members, out_dir):
    directory = Path(out_dir)
    directory.mkdir(mode=0o700, parents=False, exist_ok=False)
    for name, data in members.items():
        fd = os.open(directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())


def import_public(path, expected_contract_digest, expected_root, out_dir=None):
    """Verify with the OPERATOR's pins; the package's own pins are only compared."""
    members = _read_members(path, PUBLIC_MEMBERS)
    for data in members.values():
        if PRIVATE_MARKER in data:
            raise merkle.Invalid('package contains private material')
    manifest = _check_manifest(members, PUBLIC_SCHEMA)
    pins = merkle.parse(members['pins.json'])
    if (type(pins) is not dict or pins.get('expected_contract_digest') != expected_contract_digest
            or pins.get('expected_evidence_root') != expected_root):
        raise merkle.Invalid('package pin differs from operator pin')
    contract = merkle.parse(members['contract.json'])
    bundle = merkle.parse(members['public-bundle.json'])
    public = acceptance.verify_public(contract, expected_contract_digest, bundle, expected_root)
    if out_dir is not None:
        _extract(members, out_dir)
    return {'imported': out_dir is not None, 'package': PUBLIC_SCHEMA,
            'manifest_sha256': _sha(members['manifest.json']), 'verification': public,
            'manifest_verifier_status': manifest.get('verifier_status'),
            'trust': 'manifest-consistency-only;publisher-not-authenticated;pins-from-operator'}


def import_private(path, out_dir):
    """Structural validation only (vaults parse and match their own receipts).
    Recomputation and acceptance are the auditor's separate step."""
    members = _read_members(path, PRIVATE_MEMBERS)
    _check_manifest(members, PRIVATE_SCHEMA)
    contract = merkle.parse(members['contract.json'])
    terms.validate(contract, mode='historical')
    for name in ('private-input-vault.json', 'private-evidence-vault.json'):
        merkle._vault_tree(merkle.parse(members[name]))
    _extract(members, out_dir)
    return {'imported': True, 'package': PRIVATE_SCHEMA, 'manifest_sha256': _sha(members['manifest.json']),
            'verifier_status': terms.verifier_status(contract),
            'privacy': 'PRIVATE-PLAINTEXT-VAULTS-EXTRACTED;keep-directory-private',
            'next_step': 'record-audit (journal) or audit (stateless) recomputes and checks acceptance'}
