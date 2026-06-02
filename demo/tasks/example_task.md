# Example Task — Reproducible Demo Bounty

**Research-only.** This defines a sample task for the Phase 1 agentic demo. Test-META is a zero-value testnet placeholder and never mints base supply (see MIP-0001 §3, MIP-0002 §8). Not financial or legal advice.

## Task ID
`task-0001-lunar-link-budget`

## Assignment (the GitHub issue an agent picks up)
Compute a deterministic communications link-budget margin (in dB) for a lunar surface relay over a range sweep, then emit a structured result plus the metadata needed to verify it.

This is a small, fully deterministic numerical task — chosen because it is reproducible by machine (passes MIP-0002 Gate 2) and cheap to verify (well within the computational-complexity ceiling). It is illustrative only.

## NASA taxonomy reference
Maps to NASA Technology Taxonomy **TX05 — Communications, Navigation, and Orbital Debris Tracking and Characterization Systems** (specifically link-budget analysis). Reference only; no NASA affiliation or endorsement.

## Inputs (fixed — part of the reproducibility hash)
- Transmit power: 10.0 W
- Transmit + receive antenna gains: 12.0 dBi each
- Frequency: 2.4 GHz
- Range sweep: 100 km to 2000 km, in 100 km steps
- System noise + required margin reference: fixed constants defined in the task module
- Random seed: `42` (for any tie-breaking / ordering; the core computation is fully deterministic)

## Expected output
A JSON object containing: the input parameters, an array of `{range_km, free_space_path_loss_dB, link_margin_dB}` entries for each step, and a single summary `min_margin_dB`. Output must be byte-stable across re-runs given identical inputs.

## Gate-1 (Integrity) acceptance — placeholder for the software demo
Integrity proof is recorded as a signed/hashed execution record. In this software-only demo, hardware TEE attestation is simulated/stubbed; a deterministic re-run by the verifier stands in for attestation. (Attestation is one gate of three, never the whole proof — MIP-0002 §2.)

## Gate-2 (Reproducibility) acceptance — the real check
The submission passes Gate 2 if and only if: the verifier, given the same inputs and seed, re-runs the computation and obtains an output whose canonical hash exactly matches the submitted output hash. Any mismatch → auto-reject.

## Out of scope
Gate-3 (Usefulness) is not evaluated in this software demo; it is the human/optimistic-oracle layer (MIP-0002 §2) and is deferred. This task is illustrative and not a claim of real engineering utility.
