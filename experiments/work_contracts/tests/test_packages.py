"""Package exchange: round trip, missing/substituted/extra members, versions, sizes, private inclusion."""
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
import zipfile
from experiments.private_receipts import receipt as merkle
from experiments.work_contracts import acceptance, contract, fixtures, packages


def rewrite(src, dst, mutate):
    """Copy a zip while letting `mutate(name, data) -> (name, data) | None | list` alter members."""
    with zipfile.ZipFile(src) as archive, zipfile.ZipFile(dst, 'w') as out:
        for info in archive.infolist():
            result = mutate(info.filename, archive.read(info))
            for name, data in ([result] if isinstance(result, tuple) else result or []):
                new = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                new.external_attr = info.external_attr
                out.writestr(new, data)


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)
        self.terms, self.inputs, self.evidence = fixtures.prepare('pkg', 'INFEASIBLE')
        self.pin = contract.digest(self.terms)
        audited = acceptance.audit(self.terms, self.pin, self.inputs, self.evidence)
        self.bundle, self.root = audited['bundle'], audited['evidence_root']
        self.public = self.dir / 'public.zip'
        packages.export_public(self.terms, self.pin, self.bundle, self.root, self.public)
        self.private = self.dir / 'private.zip'
        packages.export_private(self.terms, self.inputs, self.evidence, self.private)

    def variant(self, mutate, name='variant.zip'):
        path = self.dir / name
        rewrite(self.public, path, mutate)
        return path

    def test_round_trip_and_extraction_permissions(self):
        out = self.dir / 'out'
        result = packages.import_public(self.public, self.pin, self.root, out)
        self.assertTrue(result['verification']['membership_verified'])
        self.assertEqual(sorted(os.listdir(out)), sorted(packages.PUBLIC_MEMBERS))
        self.assertEqual(out.stat().st_mode & 0o777, 0o700)
        self.assertEqual((out / 'contract.json').stat().st_mode & 0o777, 0o600)
        with self.assertRaises(FileExistsError):  # never overwrite an existing directory
            packages.import_public(self.public, self.pin, self.root, out)
        self.assertEqual(self.public.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(FileExistsError):
            packages.export_public(self.terms, self.pin, self.bundle, self.root, self.public)
        private_out = self.dir / 'priv'
        self.assertTrue(packages.import_private(self.private, private_out)['imported'])
        self.assertEqual(sorted(os.listdir(private_out)), sorted(packages.PRIVATE_MEMBERS))

    def test_operator_pins_rule_the_package_pins(self):
        with self.assertRaisesRegex(merkle.Invalid, 'package pin differs'):
            packages.import_public(self.public, '0' * 64, self.root)
        forged = self.variant(lambda n, d: (n, merkle.canonical({'expected_contract_digest': '0' * 64,
                                                                 'expected_evidence_root': self.root,
                                                                 'trust_source': 'x'}) + b'\n') if n == 'pins.json' else (n, d))
        with self.assertRaisesRegex(merkle.Invalid, 'package manifest mismatch'):
            packages.import_public(forged, '0' * 64, self.root)

    def test_missing_substituted_extra_duplicate_and_traversal_members(self):
        cases = {
            'missing': lambda n, d: None if n == 'README.txt' else (n, d),
            'substituted': lambda n, d: (n, d.replace(b'INFEASIBLE', b'FEASIBLE')) if n == 'public-bundle.json' else (n, d),
            'extra': lambda n, d: [(n, d), ('extra.py', b'print(1)')] if n == 'README.txt' else (n, d),
            'duplicate': lambda n, d: [(n, d), (n, d)] if n == 'README.txt' else (n, d),
            'traversal': lambda n, d: ('../README.txt', d) if n == 'README.txt' else (n, d),
            'absolute': lambda n, d: ('/tmp/README.txt', d) if n == 'README.txt' else (n, d),
            'journal': lambda n, d: [(n, d), ('journal.sqlite', b'SQLite format 3\0')] if n == 'README.txt' else (n, d),
        }
        for name, mutate in cases.items():
            with self.subTest(case=name), self.assertRaises(merkle.Invalid):
                packages.import_public(self.variant(mutate, name + '.zip'), self.pin, self.root, self.dir / (name + '-out'))
            self.assertFalse((self.dir / (name + '-out')).exists(), 'nothing may be extracted on failure')

    def test_unknown_version_and_symlink_member(self):
        def bump(n, d):
            if n == 'manifest.json':
                m = json.loads(d)
                m['schema'] = 'metacoin-work-contract-public-package/v2'
                return (n, merkle.canonical(m) + b'\n')
            return (n, d)
        with self.assertRaisesRegex(merkle.Invalid, 'unsupported package version'):
            packages.import_public(self.variant(bump), self.pin, self.root)
        path = self.dir / 'symlink.zip'
        with zipfile.ZipFile(self.public) as archive, zipfile.ZipFile(path, 'w') as out:
            for info in archive.infolist():
                data = archive.read(info)
                new = zipfile.ZipInfo(info.filename, date_time=(1980, 1, 1, 0, 0, 0))
                new.external_attr = ((stat.S_IFLNK | 0o777) << 16) if info.filename == 'README.txt' else info.external_attr
                out.writestr(new, b'/etc/passwd' if info.filename == 'README.txt' else data)
        with self.assertRaisesRegex(merkle.Invalid, 'not a regular file'):
            packages.import_public(path, self.pin, self.root)

    def test_oversized_member_and_archive(self):
        big = self.variant(lambda n, d: (n, b'x' * (packages.MAX_MEMBER + 1)) if n == 'README.txt' else (n, d), 'big.zip')
        with self.assertRaisesRegex(merkle.Invalid, 'too large'):
            packages.import_public(big, self.pin, self.root)
        # a header that lies about its size is caught by the actual read length
        path = self.dir / 'liar.zip'
        rewrite(self.public, path, lambda n, d: (n, d))
        with open(path, 'r+b') as stream:
            data = stream.read()
        with zipfile.ZipFile(path) as archive:
            info = archive.getinfo('README.txt')
        # Simplest oversize-on-disk case: pad the archive beyond MAX_ARCHIVE
        with open(self.dir / 'padded.zip', 'wb') as stream:
            stream.write(data + b'\0' * (packages.MAX_ARCHIVE + 1))
        with self.assertRaisesRegex(merkle.Invalid, 'too large'):
            packages.import_public(self.dir / 'padded.zip', self.pin, self.root)

    def test_private_material_never_enters_a_public_package(self):
        with self.assertRaises(merkle.Invalid):  # a vault is not a bundle: refused before packaging
            packages.export_public(self.terms, self.pin, self.evidence, self.root, self.dir / 'x.zip')
        self.assertFalse((self.dir / 'x.zip').exists())
        smuggled = self.variant(lambda n, d: (n, merkle.canonical(self.inputs) + b'\n') if n == 'README.txt' else (n, d))
        with self.assertRaisesRegex(merkle.Invalid, 'private material'):
            packages.import_public(smuggled, self.pin, self.root)
        with zipfile.ZipFile(self.public) as archive:
            for name in archive.namelist():
                text = archive.read(name)
                self.assertNotIn(b'SYNTHETIC_PRIVATE_CANARY_73', text)
                self.assertNotIn(b'margin_width', text)
                self.assertNotIn(packages.PRIVATE_MARKER, text)

    def test_private_package_structure_is_validated_not_executed(self):
        tampered = self.dir / 'tampered-private.zip'
        rewrite(self.private, tampered, lambda n, d: (n, d.replace(b'"padding"', b'"padDing"')) if n == 'private-evidence-vault.json' else (n, d))
        with self.assertRaises(merkle.Invalid):
            packages.import_private(tampered, self.dir / 'tp')
        self.assertFalse((self.dir / 'tp').exists())
        with self.assertRaisesRegex(merkle.Invalid, 'not permitted'):
            packages.import_private(self.public, self.dir / 'wrong-kind')
