# MetaCoin R3 — External Verifier Pilot

**Research-stage. Zero-value. No token, no payment, no wallet, no mainnet.** This is a
pilot for *reproducible useful-work verification by a real external party*. It is **NOT**
decentralized consensus, **NOT** mainnet, **NOT** a payment system, and **NOT** a token.

## What this is — and what it is NOT

**What it is:** a way for a **real external verifier** — another physical machine, a GitHub
Actions runner, a VPS, or a collaborator's laptop — to re-run the *same reproducible task*
locally, compute its canonical `output_hash`, and submit that result. A **coordinator** then
recomputes the same task's hash on its own machine and compares. The outcome (verified or
mismatch) is anchored as a permanent entry in the R1 tamper-evident hash-chained ledger.

**The precise, honest claim (read this carefully):**
> A matching `output_hash` proves the result is **REPRODUCIBLE** — it was independently
> re-derived to the same canonical value. It does **NOT**, by itself, cryptographically
> prove that the verifier actually **executed** the task. A hash is a short string and can
> be copied. So a successful match honestly supports *"an external party submitted a
> reproducibly-matching result"*, **not** *"we proved independent execution."*

**What would make it stronger (future work, same interface):**
- Independence improves when the verifier is operated by a **separate person or
  organization** on separate infrastructure (so copying the hash is a deliberate dishonest
  act by a distinct party, not a single host fooling itself).
- Genuine **execution-proof** would require verifier-held **signing keys** and/or
  **hardware attestation** (e.g. a TPM/GPU-CC quote). This host has **no usable hardware
  root of trust** (see R2), so today's optional signing is only a software-key MAC.

## Honesty labels you will see everywhere
- `topology: "external-verifier-pilot"` (not consensus).
- `stage: "R3"`, `zero_value: true`, `no_token: true`.
- A `limitation_note` / `honest_note` stating the reproducibility-not-execution caveat.
- Any attached MAC is labeled **symmetric software-key HMAC — not a public-key signature,
  not hardware-backed, and not a proof of execution** (it reuses the R2 component).

## Steps an external verifier follows

1. **Clone the repository** on a separate machine (or CI runner / VPS / laptop):
   ```bash
   git clone <repo-url> && cd metacoin
   ```
2. **Run the reproducible task locally** and produce a submission (standard library only,
   no dependencies to install):
   ```bash
   python3 protocol/verifier_cli.py --task task-0002 --verifier-id alice-laptop --out submission.json
   ```
   - `--task` accepts any known task id (`task-0001` … `task-0006`), short or full form.
   - `--verifier-id` should identify the verifier (ideally a separate person/org).
   - `--out` writes the submission JSON (omit it to print to stdout).
   - `--key <path>` (optional) attaches an honest R2 software-key MAC. It binds
     `task_id + output_hash` to a key the verifier holds; it does **not** prove execution.
3. **Send `submission.json` back** to the coordinator (email, PR, paste — any channel; this
   pilot does not include networking).
4. **The coordinator evaluates it** against its own locally recomputed hash:
   ```python
   from protocol.ledger import Ledger
   from protocol.external_verifier import evaluate_submission
   import json

   ledger = Ledger("protocol/ledger_data.jsonl")          # the R1 ledger
   submission = json.load(open("submission.json"))
   result = evaluate_submission(submission, ledger)
   print(result["evaluation"]["status"])                  # externally-verified / external-mismatch / rejected
   ```
5. **The ledger records the outcome.** A match → `externally-verified`; a mismatch →
   `external-mismatch`; both are anchored as tamper-evident ledger entries (a malformed
   submission is `rejected` and is **not** anchored). Anyone can later re-run
   `ledger.verify_chain()` to confirm the record was not altered.

## The submission contract
The required fields and types are documented in
[`verifier_submission.schema.json`](verifier_submission.schema.json) (JSON Schema). Key
fields: `event`, `stage`, `topology`, `task_id`, `verifier_id`, `machine_fingerprint`
(a **coarse, non-identifying** sha256 of platform + arch — no hostname/user/IP),
`timestamp`, `output_hash` (64-hex sha256), `repo_commit`, `environment_summary`,
`zero_value`, `no_token`.

## Components
| File | Role |
|---|---|
| `verifier_cli.py` | **Verifier-side** command. Re-runs a task and emits a submission. |
| `verifier_submission.schema.json` | JSON Schema for the submission contract. |
| `external_verifier.py` | **Coordinator.** `validate_submission()` + `evaluate_submission()`; anchors outcomes in the R1 ledger. Its `__main__` self-test is a **local test harness (single-host simulation), not the product.** |
| `ledger.py` (R1) | The tamper-evident hash-chained ledger the outcomes anchor into. |
| `attest.py` (R2) | The honest software-key MAC reused for the optional signing. |

## Run the self-test (local test harness)
```bash
python3 protocol/external_verifier.py
```
This simulates the three outcomes on a single host **for testing only** (it is clearly
labeled as a harness, not the pilot itself), using a temporary ledger and temporary key,
and leaves no stray files.

---
Research only; not legal, financial, investment, or security-certification advice.
Zero-value testnet research artifact (MIP-0001 §3, MIP-0002 §8).
