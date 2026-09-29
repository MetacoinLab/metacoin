# Selective disclosure experiment

Local research prototype; zero runtime dependencies; Python 3.12+. Repository
license applies. No ledger writes, token, payment, or production deployment.

This experiment addresses a narrow prerequisite for private work receipts:
commit to evidence, retain it locally, and later reveal selected fields without
publishing the rest. Its proof kind is **salted-merkle-membership-only**.
It is NOT a Zcash implementation, a zero-knowledge computation proof, an
encryption scheme, or proof of issuer identity, work, usefulness, or independence.

From the repository root:

```sh
python3 -m unittest experiments.private_receipts.test_receipt -v
python3 -m experiments.private_receipts.demo
```

The read-only demo re-runs the existing task-0035 honest negative, checks its
output against its published ledger record and the snapshot against the local
anchor, and discloses only `verdict: false` and `scope`. Changing the disclosed
verdict to true fails membership verification. The fixture was already PUBLIC;
this cannot retroactively hide its data. Re-running locally is distinct from
what a remote verifier learns from a membership proof.

## Using your own synthetic evidence

Create an input JSON object with explicit top-level field names. Use integer
units or contract-defined decimal strings; floats are refused, not rounded.
In a new output directory, using filenames that do not yet exist:

```sh
python3 -m experiments.private_receipts.receipt commit \
  --input evidence.json --receipt receipt.json --vault private-vault.json
python3 -m experiments.private_receipts.receipt disclose \
  --vault private-vault.json --field verdict --field scope --out disclosure.json
python3 -m experiments.private_receipts.receipt verify \
  --bundle disclosure.json --expected-root TRUSTED_ROOT_HEX \
  --require verdict --require scope
```

`TRUSTED_ROOT_HEX` must be authenticated independently: e.g. the root agreed in
a signed job contract, or a separately witnessed log checkpoint. Copying a root
from an attacker's disclosure bundle authenticates nothing. This prototype
implements neither signing nor witness infrastructure. `--require` enforces
presence of verifier-selected fields, not their semantic truth. Inspect the
disclosed values against your acceptance policy separately.

The CLI returns 0 on membership success and 2 on refusal. Output explicitly says
`task_correctness_proven: false` and `issuer_authenticated: false`. Unknown proof
kinds fail closed; there is no “accept any future backend” switch.

## Format and trust boundary

The public receipt contains only version, fixed proof kind, fixed tree size
(64), and root. Fields occupy random slots; unused slots contain random padding.
Each leaf uses an independent 32-byte salt from `secrets`. Tags domain-separate
leaves, padding, internal nodes, and roots. A field leaf commits to its index,
salt, name, and canonical value. A disclosure opens one field and six siblings.
These are ordinary SHA-256 commitment/Merkle techniques, not a new cryptographic
primitive. The experimental encoding has not received external security review.

JSON is bounded (2 MiB serialized file, 512 KiB evidence, 64 KiB per field,
depth 16, 20,000 nodes, integers within the exact interoperable JSON range).
Duplicate keys, non-finite numbers, floats, unknown schema fields, duplicate
openings, boolean indices, and incorrect path lengths are rejected.
Unicode code points are preserved without normalization. This encoding is
separate from historical ledger eras and is NOT claimed to be RFC 8785 JCS.

New output files use exclusive creation and mode 0600. **The vault is plaintext,
not encrypted**: its values and salts must remain on protected local storage.
Parent directories must be trusted. If a file operation fails after vault
creation, a private vault can remain; the CLI never overwrites existing files.
Protect backups as carefully as the original. No files are transmitted.

| Threat | Prototype behavior / remaining boundary |
|---|---|
| Flip a disclosed false verdict to true | Rejected against the trusted root |
| Change field name, index, salt, path, or root | Rejected |
| Replace the entire receipt with a newly committed lie | Rejected ONLY if verifier retains an independently trusted root |
| Commit a false statement initially | Allowed; membership says nothing about truth |
| Guess a hidden low-entropy value | Random secret salts prevent simple public dictionary matching under SHA-256 assumptions; salt disclosure defeats that protection |
| Hide how many fields were committed | Fixed tree padding and random slots hide the count at receipt level; disclosures still leak a lower bound |
| Correlate disclosures for one receipt | Possible and intentional: the shared root links them |
| Discover issuer, IP, timestamps, sizes, or access patterns | Not protected by this module |
| Omit an unfavorable field | Explicit required-field policy helps; completeness needs a schema/contract or a ZK relation |
| A malicious producer commits two contradictory hidden fields | Membership alone cannot prove global schema validity or hidden-name uniqueness; the local builder enforces unique names, but a remote proof does not certify that builder ran |
| Replay a receipt for payment | Not prevented: settlement-specific nullifiers and atomic replay state are future work |
| Lose, steal, or intentionally withhold a vault | Availability/confidentiality failure; no recovery or encrypted storage implemented |
| Revoke a disclosure already received | Impossible; only future access could be revoked by a separate system |

## The next separately reviewed boundary

A real ZK backend must prove a specified relation over hidden evidence, bind the
program/verifying-key identity, contract, input commitment, output statement,
proof version, and any settlement nullifier as public inputs, and verify with
mock/dev acceptance disabled. Proving correct code does not prove truthful
sensor input, a unique human, fresh physical work, or market demand.

Do not anchor this experiment as a new record class until its schema, trust
roots, privacy leakage, and independent review are accepted. Existing anchored
source bytes, identity text, tasks, MIPs, and ledger eras remain unchanged.
