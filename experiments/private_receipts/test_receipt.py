"""Adversarial boundaries for the experimental disclosure format."""
import copy
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest

from experiments.private_receipts import receipt as r


class ReceiptTests(unittest.TestCase):
    def setUp(self):
        self.evidence = {"verdict": False, "customer": "hidden-client-7",
                         "records": [{"name": "hidden-employee", "units": 23}]}
        self.public, self.vault = r.commit(self.evidence)
        self.bundle = r.disclose(self.vault, ["verdict"])
        self.root = self.public["root"]

    def test_negative_verdict_and_minimal_disclosure(self):
        self.assertEqual(r.verify(self.bundle, self.root, ["verdict"]), {"verdict": False})
        serialized = r.canonical(self.bundle)
        for secret in (b"hidden-client-7", b"hidden-employee", b"customer", b"records"):
            self.assertNotIn(secret, serialized)

    def test_fresh_commitments_hide_equality_at_receipt_level(self):
        other, _ = r.commit(self.evidence)
        self.assertNotEqual(self.public["root"], other["root"])
        self.assertEqual(set(self.public), {"schema", "kind", "tree_size", "root"})

    def test_fixed_padding_at_one_and_sixty_four_fields(self):
        for count in (1, 64):
            with self.subTest(count=count):
                public, vault = r.commit({f"f{i}": i for i in range(count)})
                self.assertEqual(public["tree_size"], 64)
                self.assertEqual(len(vault["fields"]) + len(vault["padding"]), 64)
                disclosure = r.disclose(vault, ["f0"])
                self.assertEqual(len(disclosure["disclosures"][0]["path"]), 6)
                self.assertEqual(r.verify(disclosure, public["root"]), {"f0": 0})

    def test_tamper_all_bound_opening_components(self):
        changes = {"name": "other", "value": True, "salt": "00" * 32,
                   "index": (self.bundle["disclosures"][0]["index"] + 1) % 64,
                   "path": ["00" * 32] * 6}
        for key, value in changes.items():
            with self.subTest(key=key):
                bad = copy.deepcopy(self.bundle)
                bad["disclosures"][0][key] = value
                with self.assertRaises(r.Invalid):
                    r.verify(bad, self.root)

    def test_receipt_substitution_rejected_by_external_pin(self):
        _, other_vault = r.commit({"verdict": True})
        other_bundle = r.disclose(other_vault, ["verdict"])
        with self.assertRaises(r.Invalid):
            r.verify(other_bundle, self.root)

    def test_missing_required_field_and_empty_bundle_fail(self):
        with self.assertRaises(r.Invalid):
            r.verify(self.bundle, self.root, ["customer"])
        bad = copy.deepcopy(self.bundle)
        bad["disclosures"] = []
        with self.assertRaises(r.Invalid):
            r.verify(bad, self.root)

    def test_duplicate_disclosure_fail(self):
        bad = copy.deepcopy(self.bundle)
        bad["disclosures"] *= 2
        with self.assertRaises(r.Invalid):
            r.verify(bad, self.root)

    def test_unknown_fields_and_proof_type_escalation_fail(self):
        for key, value in (("kind", "zk-computation-proof"),
                           ("schema", "future-version"), ("tree_size", 128),
                           ("signature", "pretend-authentication")):
            with self.subTest(key=key):
                bad = copy.deepcopy(self.bundle)
                bad["receipt"][key] = value
                with self.assertRaises(r.Invalid):
                    r.verify(bad, self.root)

    def test_noncanonical_and_ambiguous_encodings_fail(self):
        for raw in (b'{"a":1,"a":2}', b'{"a":NaN}', b'{"a":Infinity}',
                    b'{"a":-0.0}', b'{"a":1.25}', b'{"a":9007199254740992}',
                    b'{"a":{"x":1,"x":2}}', b'\xff'):
            with self.subTest(raw=raw):
                with self.assertRaises(r.Invalid):
                    r.parse(raw)
        bad = copy.deepcopy(self.bundle)
        bad["disclosures"][0]["index"] = True
        with self.assertRaises(r.Invalid):
            r.verify(bad, self.root)

    def test_unicode_roundtrip_is_exact(self):
        public, vault = r.commit({"text": "机器工作／é／e\u0301"})
        bundle = r.disclose(vault, ["text"])
        self.assertEqual(r.verify(bundle, public["root"])["text"], "机器工作／é／e\u0301")

    def test_mutated_vault_and_bad_padding_fail(self):
        for part in ("fields", "padding"):
            vault = copy.deepcopy(self.vault)
            vault[part][0]["index"] = (vault[part][0]["index"] + 1) % 64
            with self.assertRaises(r.Invalid):
                r.disclose(vault, ["verdict"])

    def test_resource_bounds(self):
        value = 1
        for _ in range(18):
            value = [value]
        for evidence in ({}, {f"f{i}": i for i in range(65)}, {"a": value},
                         {"a": "x" * r.MAX_VALUE}, {"bad-name": 1}):
            with self.assertRaises(r.Invalid):
                r.commit(evidence)
        with self.assertRaises(r.Invalid):
            r.parse(b" " * (r.MAX_FILE + 1))

    def test_no_shared_mutable_input_or_output(self):
        self.evidence["records"][0]["units"] = 99
        proof = r.disclose(self.vault, ["records"])
        self.assertEqual(r.verify(proof, self.root)["records"][0]["units"], 23)
        proof["disclosures"][0]["value"][0]["units"] = 100
        self.assertEqual(r.verify(r.disclose(self.vault, ["records"]), self.root)
                         ["records"][0]["units"], 23)

    def test_membership_does_not_assert_truth_or_execution(self):
        # A false claim can be committed. Acceptance MUST NOT imply its truth.
        public, vault = r.commit({"claim": "unexecuted arbitrary assertion"})
        values = r.verify(r.disclose(vault, ["claim"]), public["root"])
        self.assertEqual(values["claim"], "unexecuted arbitrary assertion")

    def test_external_known_answer_for_hash_layout_and_path_orientation(self):
        # Calculated directly from hashlib with literal leaf bytes and siblings,
        # independently of commit(), _leaf(), and _levels(). No production seed.
        pin = "356f4044edfb34a490a526d997dbbcf4ad6b9ae6864f2253551ed60ccd63e2d3"
        bundle = {"receipt": {"schema": r.SCHEMA, "kind": r.KIND,
                               "tree_size": 64, "root": pin},
                  "disclosures": [{"name": "verdict", "value": False,
                                   "salt": "00" * 32, "index": 5,
                                   "path": [bytes([i]).hex() * 32 for i in range(1, 7)]}]}
        self.assertEqual(r.verify(bundle, pin), {"verdict": False})

    def test_every_slot_can_be_opened(self):
        evidence = {f"field_{i}": {"value": i} for i in range(64)}
        public, vault = r.commit(evidence)
        self.assertEqual(r.verify(r.disclose(vault, list(evidence)), public["root"]), evidence)

    def test_safe_file_creation_and_cli_roundtrip(self):
        module = [sys.executable, "-m", "experiments.private_receipts.receipt"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inp, pub, vault, bundle = [root / n for n in ("input", "public", "vault", "bundle")]
            inp.write_text(json.dumps(self.evidence), encoding="utf-8")
            def run(*args):
                return subprocess.run(module + list(args), capture_output=True, text=True)
            result = run("commit", "--input", str(inp), "--receipt", str(pub), "--vault", str(vault))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(stat.S_IMODE(vault.stat().st_mode), 0o600)
            self.assertEqual(run("disclose", "--vault", str(vault), "--field", "verdict",
                                 "--out", str(bundle)).returncode, 0)
            pin = json.loads(pub.read_text())["root"]
            result = run("verify", "--bundle", str(bundle), "--expected-root", pin,
                         "--require", "verdict")
            self.assertEqual(result.returncode, 0, result.stderr)
            status = json.loads(result.stdout)
            self.assertFalse(status["task_correctness_proven"])
            self.assertFalse(status["issuer_authenticated"])
            original = vault.read_bytes()
            self.assertEqual(run("commit", "--input", str(inp), "--receipt", str(pub),
                                 "--vault", str(vault)).returncode, 2)
            self.assertEqual(original, vault.read_bytes())
            link = root / "link"
            link.symlink_to(vault)
            with self.assertRaises(FileExistsError):
                r.write_new(link, {"destroy": True})
            self.assertEqual(original, vault.read_bytes())


if __name__ == "__main__":
    unittest.main()
