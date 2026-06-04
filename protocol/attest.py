"""attest.py — MetaCoin R2: HONEST software-rooted attestation, anchored to the R1 ledger.

================== CRITICAL HONESTY NOTICE (READ ME — THIS IS THE POINT) ==================
This attestation is SOFTWARE-ROOTED. It is NOT hardware attestation. There is NO TPM, NO
TEE/secure-enclave, and NO GPU confidential-compute backing on this host. The R2 hardware
investigation confirmed there is no usable hardware root of trust here:
  * the kernel logged "No TPM chip found, activating TPM-bypass";
  * the systemd TPM2 services were skipped;
  * IMA's boot_aggregate is all-zeros (software-anchored, not hardware-anchored);
  * Secure Boot is enabled but is boot-integrity only — NOT an attestation root.

Therefore the trust root here is a SOFTWARE-HELD KEY, and every record says so explicitly
("root_of_trust": "software-key"). This module deliberately does NOT imply TPM, TEE, or
enclave backing anywhere.

Signing primitive: standard library only, so there is no public-key/asymmetric signature
available. We use HMAC-SHA256 with a locally-stored secret as the integrity primitive. That
is a SYMMETRIC software-key MAC, not a signature:
  * It proves integrity/authenticity to anyone who HOLDS THE SAME SECRET KEY.
  * It is NOT publicly verifiable (unlike a public-key signature) and is NOT hardware-backed.
  * It is an honest placeholder. A future hardware-rooted attestation (fTPM quote, NVIDIA
    GPU-CC attestation) or a public-key signature would slot in behind the SAME interface
    (attest / verify_attestation) and would set a different, truthful "root_of_trust" value.

Anchoring: each attestation record is appended as a payload to the R1 tamper-evident,
hash-chained ledger (protocol/ledger.py), so attestations become permanent, independently
re-verifiable ledger entries. The two layers are independent and complementary:
  * the LEDGER detects external file tampering of stored records (no key needed to detect);
  * the MAC detects content tampering / wrong-key to anyone holding the secret key.

Research-stage, ZERO-VALUE. Not a financial ledger; no token, money, wallet, network, or
payment (MIP-0001 paragraph 3, MIP-0002 paragraph 8). Standard library only. Not legal,
financial, or security-certification advice.
=========================================================================================
"""

import hashlib
import hmac
import json
import os
import secrets
import sys
import time

# Make `from protocol.ledger import ...` resolve when run directly (repo root on sys.path).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from protocol.ledger import Ledger  # the R1 tamper-evident hash-chained ledger

# Default software-key path (gitignored). A real signing key is NEVER committed.
DEFAULT_KEY_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "attest_key.secret"
)

# The honest, fixed trust-root label embedded in every record.
ROOT_OF_TRUST = "software-key"

# Honest description of the signing primitive (embedded in every record and MAC-covered).
MAC_ALGORITHM = (
    "HMAC-SHA256 (symmetric software-key MAC; requires the same secret key to verify; "
    "NOT a public-key signature and NOT hardware-backed)"
)

# Honest machine note (embedded in every record and MAC-covered).
MACHINE_NOTE = (
    "no hardware root of trust available on this host (no TPM: kernel logged 'No TPM chip "
    "found, activating TPM-bypass'; TPM2 services skipped; IMA boot_aggregate all-zeros; "
    "Secure Boot is boot-integrity only). Software-key MAC only — NOT hardware-backed."
)

# Schema version for forward compatibility.
ATTESTATION_VERSION = 1


def _canonical_json(obj) -> str:
    """Canonical JSON: sorted keys, compact separators, ASCII — byte-stable for the MAC.

    (Same serialization discipline as the R1 ledger, so both layers are deterministic.)
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


class SoftwareAttestor:
    """Software-rooted attestor: HMAC-SHA256 over canonical JSON with a locally-held key.

    HONEST LIMITATIONS: symmetric MAC (verifier must hold the same secret key; not publicly
    verifiable), and software-held (not hardware-rooted; no TPM/TEE/GPU-CC on this host).
    """

    def __init__(self, key_path: str = DEFAULT_KEY_PATH):
        self.key_path = key_path
        self.key = self._load_or_create_key()

    # --- key management -----------------------------------------------------
    def _load_or_create_key(self) -> bytes:
        """Load the 32-byte software key, or create it (0600) if absent via secrets."""
        if os.path.exists(self.key_path):
            with open(self.key_path, "rb") as f:
                key = f.read()
            if len(key) < 16:
                raise ValueError(f"software key at {self.key_path} is too short/corrupt")
            return key
        # Create with O_EXCL and mode 0600 so the secret is never group/world-readable.
        key = secrets.token_bytes(32)
        fd = os.open(self.key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, key)
        finally:
            os.close(fd)
        return key

    # --- MAC ----------------------------------------------------------------
    def _mac(self, content: dict) -> str:
        """HMAC-SHA256 hex digest over the canonical JSON of the MAC-covered content."""
        msg = _canonical_json(content).encode("utf-8")
        return hmac.new(self.key, msg, hashlib.sha256).hexdigest()

    # --- attestation --------------------------------------------------------
    def attest(self, task_id: str, output_hash: str, extra: dict = None) -> dict:
        """Produce a software-rooted attestation record for a reproducible task output.

        The record commits (via HMAC) to: schema version, task_id, the task's reproducible
        output_hash, a timestamp, the honest root_of_trust/mac_algorithm/machine_note labels,
        and an optional `extra` dict. Returns the full record including the `mac` field.
        """
        if extra is not None and not isinstance(extra, dict):
            raise TypeError("extra must be a dict or None")

        # Every field here is covered by the MAC. The only field NOT covered is "mac" itself.
        content = {
            "attestation_version": ATTESTATION_VERSION,
            "task_id": task_id,
            "output_hash": output_hash,
            "timestamp": time.time(),
            "root_of_trust": ROOT_OF_TRUST,          # honest: software-key (not hardware)
            "mac_algorithm": MAC_ALGORITHM,          # honest: symmetric MAC, not a signature
            "machine_note": MACHINE_NOTE,            # honest: no hardware root on this host
            "extra": extra if extra is not None else {},
        }
        record = dict(content)
        record["mac"] = self._mac(content)
        return record

    def verify_attestation(self, record: dict):
        """Recompute the MAC over the record content (excluding `mac`) and check it matches.

        Returns (ok, reason). HONEST: this requires the SAME secret key (symmetric MAC); it
        is not publicly verifiable and not hardware-backed. Also rejects any record that does
        not honestly declare a software-key root of trust.
        """
        if not isinstance(record, dict):
            return (False, "record is not a dict")
        if "mac" not in record or not isinstance(record["mac"], str):
            return (False, "record missing a string 'mac' field")

        content = {k: v for k, v in record.items() if k != "mac"}

        if content.get("root_of_trust") != ROOT_OF_TRUST:
            return (
                False,
                f"unexpected root_of_trust {content.get('root_of_trust')!r} "
                f"(this verifier only accepts honest '{ROOT_OF_TRUST}' records)",
            )

        expected = self._mac(content)
        if not hmac.compare_digest(expected, record["mac"]):
            return (
                False,
                "MAC mismatch — attestation content was altered or the wrong key was used "
                "(symmetric software-key MAC)",
            )
        return (
            True,
            "ok: software-key MAC verified (requires the same secret key; not publicly "
            "verifiable; not hardware-backed)",
        )

    # --- ledger anchoring ---------------------------------------------------
    def attest_and_record(self, ledger: Ledger, task_id: str, output_hash: str,
                          extra: dict = None) -> dict:
        """Attest, then append the attestation as a payload to the R1 ledger.

        Returns the resulting ledger entry. The attestation thus becomes a permanent,
        independently re-verifiable, tamper-evident chain entry.
        """
        record = self.attest(task_id, output_hash, extra)
        payload = {"event": "attestation_record", "attestation": record}
        return ledger.append(payload)


# ============================== SELF-TEST ====================================

def _selftest() -> int:
    """Self-test using a TEMP key file and TEMP ledger file — never the defaults."""
    import shutil
    import tempfile

    def read_raw(path):
        with open(path, "r", encoding="utf-8") as f:
            return [ln.rstrip("\n") for ln in f if ln.strip()]

    def write_raw(path, lines):
        with open(path, "w", encoding="utf-8") as f:
            for ln in lines:
                f.write(ln + "\n")

    tmpdir = tempfile.mkdtemp(prefix="attest_selftest_")
    checks = []  # (label, passed_bool, detail)
    sample_record = None
    try:
        key_path = os.path.join(tmpdir, "attest_key.secret")
        ledger_path = os.path.join(tmpdir, "ledger.jsonl")

        attestor = SoftwareAttestor(key_path)

        # Confirm the key file was created 0600.
        mode = oct(os.stat(key_path).st_mode & 0o777)

        SAMPLE_TASK = "task-0002-orbit-propagation"
        SAMPLE_HASH = "ff03231fc53a0505f648e2ec24e251ced41c896fbfd2553ffaa914ffd5ba300c"

        # 1) attest + verify -> True
        record = attestor.attest(SAMPLE_TASK, SAMPLE_HASH, extra={"note": "zero-value test"})
        sample_record = record
        ok1, reason1 = attestor.verify_attestation(record)
        checks.append(("SOFTWARE-KEY ATTEST + VERIFY", ok1 is True, reason1))

        # 2) tamper the record's output_hash -> verify False (MAC mismatch)
        tampered = json.loads(json.dumps(record))  # deep copy
        tampered["output_hash"] = "00" * 32
        ok2, reason2 = attestor.verify_attestation(tampered)
        checks.append(("TAMPER OUTPUT_HASH -> DETECTED (MAC)", ok2 is False, reason2))

        # 3) anchor into the ledger: chain verifies AND the inner attestation MAC verifies
        ledger = Ledger(ledger_path)
        ledger.append({"event": "proof_record", "task_id": SAMPLE_TASK,
                       "output_hash": SAMPLE_HASH, "verdict": "PASS"})
        entry = attestor.attest_and_record(ledger, SAMPLE_TASK, SAMPLE_HASH,
                                           extra={"round": "R2"})
        chain_ok, chain_reason = ledger.verify_chain()
        checks.append(("LEDGER-ANCHORED: CHAIN VERIFY", chain_ok is True, chain_reason))

        inner = entry["payload"]["attestation"]
        ok_inner, reason_inner = attestor.verify_attestation(inner)
        checks.append(("LEDGER-ANCHORED: MAC VERIFY", ok_inner is True, reason_inner))

        # 4) tamper the attestation payload INSIDE the ledger file (do NOT recompute the
        #    ledger entry hash) -> ledger.verify_chain() must catch it (tamper-evidence),
        #    independent of the MAC layer.
        attack_path = os.path.join(tmpdir, "ledger_attacked.jsonl")
        shutil.copy(ledger_path, attack_path)
        lines = read_raw(attack_path)
        atk = json.loads(lines[1])  # the attestation entry (index 1)
        atk["payload"]["attestation"]["output_hash"] = "11" * 32
        lines[1] = _canonical_json(atk)
        write_raw(attack_path, lines)
        atk_chain_ok, atk_chain_reason = Ledger(attack_path).verify_chain()
        checks.append(
            ("LEDGER TAMPER-EVIDENCE (mutated attestation payload) -> DETECTED",
             atk_chain_ok is False, atk_chain_reason)
        )

        # original (untouched) ledger must still verify
        again_ok, again_reason = ledger.verify_chain()
        checks.append(("ORIGINAL LEDGER STILL VERIFIES", again_ok is True, again_reason))

        # --- report ---------------------------------------------------------
        print("=== protocol/attest.py self-test — SOFTWARE-ROOTED attestation ===")
        print("HONEST: no hardware root of trust on this host (no TPM). Trust root is a")
        print("software-held key; HMAC-SHA256 is a symmetric MAC, NOT a public-key signature")
        print("and NOT hardware-backed. Temp key + temp ledger; defaults untouched.\n")

        print(f"software key file: {key_path} (mode {mode})\n")

        print("--- sample attestation record (note the honest labels) ---")
        print(json.dumps(sample_record, indent=2, sort_keys=True))
        print()

        print("--- results ---")
        for label, passed, _detail in checks:
            print(f"{label}: {'PASS' if passed else 'FAIL'}")
        print()

        print("--- detail ---")
        for label, _passed, detail in checks:
            print(f"  {label}\n      -> {detail}")
        print()

        all_ok = all(passed for _l, passed, _d in checks)
        print("=== self-test summary: " +
              ("ALL CHECKS BEHAVED CORRECTLY" if all_ok else "FAILURE — see above") + " ===")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    # Confirm the self-test left no stray default key / ledger files.
    stray_key = os.path.exists(DEFAULT_KEY_PATH)
    stray_ledger = os.path.exists(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "ledger_data.jsonl")
    )
    print(f"\nstray default key file ({DEFAULT_KEY_PATH}): "
          f"{'PRESENT (!!)' if stray_key else 'absent (good)'}")
    print(f"stray default ledger file: {'PRESENT (!!)' if stray_ledger else 'absent (good)'}")

    ok_overall = all(passed for _l, passed, _d in checks) and not stray_key and not stray_ledger
    return 0 if ok_overall else 1


if __name__ == "__main__":
    sys.exit(_selftest())
