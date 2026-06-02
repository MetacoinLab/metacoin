# MIP-0002 — Proof-of-Useful-Space-Work (PoUSW)

**Status:** Draft · **Layer:** Protocol · **Depends on:** MIP-0001 (Genesis)
**Note:** Research specification only. No token exists. Not financial or legal advice.

This MIP defines how the **MetaStar Treasury** (Flow 2 in MIP-0001) verifies and pays for subjective "useful space work." It does NOT govern base emission, which mints only from objective channels (MIP-0001 §3) and never from anything here. The separation is the invariant. PoUSW validates work through three independent gates, all required for full settlement. Hardware attestation is one gate, never the whole proof: proving a computation ran honestly is not the same as proving it was useful.

## 1. Scope and core principle
- Applies to: treasury-funded bounties — space simulation/synthetic data, AI-agent research, robot task bounties, tool contributions.
- Does NOT apply to: base emission (objective infra/liquidity/PoH only — MIP-0001).
- Principle: integrity ≠ reproducibility ≠ usefulness. Each verified separately; cheap gates run first and filter the expensive, subjective gate to a small surface.

## 2. The three gates
### Gate 1 — Integrity ("was it actually run as claimed?")
- Accepted, vendor-agnostic: any conforming TEE/attestation (NVIDIA Confidential Computing, Intel TDX, AMD SEV-SNP, future equivalents) OR a deterministic re-run by validators.
- No vendor privileged; non-attested work can still pass via deterministic re-run.
- Proves execution integrity. Does NOT prove value. (Necessary, not sufficient.)

### Gate 2 — Reproducibility ("can anyone re-derive it independently?")
Every submission carries: content hashes of all inputs (IPFS/Arweave-anchored), exact model/harness/prompt/temperature/seed, full dependency manifest, deterministic re-run recipe. The pipeline re-runs and compares output hashes; mismatch → auto-reject.

**Computational Complexity Ceiling (required):** a task is admissible only if verifying the proof is bounded and far cheaper than generating it (the ZK ethos: checking ≪ producing). A job that cannot be verified inside the budget is ineligible for Flow 2 and must be decomposed via its Skill Manifest. This protects validators from being bankrupted by a 48-hour simulation costly to re-run.

### Gate 3 — Usefulness ("does it matter for the mission?"), as a bounded optimistic oracle
1. Instant machine pre-check: Gatekeeper MetaAgents, programmed with the NASA Technology Taxonomy schema, mark the submission optimistically valid.
2. Bounded provisional release: the agent's bounded wallet may spend up to its cap immediately so the work→earn→recharge loop keeps machine speed. Full settlement is withheld until the challenge window closes.
3. Challenge window: any staker may raise an on-chain, stake-weighted dispute.
4. Human council as supreme court: an elected, rotating, stake-weighted council adjudicates only challenged submissions, with anti-collusion rules and public records. Unchallenged work finalizes automatically.

## 3. Compliance gate (runtime, required)
Before eligibility, the runtime (OpenShell-class sandbox) must produce signed compliance attestations proving all data and code used came from permissive public-domain or verified open-source sources. Guards against export control (ITAR/EAR — space tech is genuinely controlled) and copyright/proprietary scraping. Failing the attestation → rejected before Gates 1–3. The human operator remains legally responsible (MIP-0003); this gate is protection, not a waiver. Not legal advice — counsel required before launch.

## 4. MetaWork Passport
A portable, cryptographic record of an actor's verified contribution history (human/agent/robot): append-only log of passed submissions bound to ActorID; sybil-resistant; raises reputation weighting and challenge thresholds for consistently honest actors. Never affects base emission or grants automatic payout — it informs reputation, not minting.

## 5. Useful-Space-Work-per-Watt (metric only)
Verified useful output per unit energy/compute, published per submission and actor for transparency and grant prioritization. Metric only — it does NOT mechanically set emission or payout (that would reintroduce a gameable printer). Efficiency is rewarded through grant ranking, not base issuance.

## 6. Flagship category: reproducible space simulation & synthetic data
The strongest treasury category, because it uniquely passes Gate 2 by machine: high-fidelity space-robotics simulation, trajectory/kinematics optimization, synthetic egocentric training datasets, benchmarks — mapped to the NASA Technology Taxonomy (e.g. Robotic Systems; Aerospace Power & Energy Storage) and the ISAM problem set. Output is an open, cryptographically verified digital-twin library any lab or company can trust because Gates 1–2 prove honest, reproducible execution.

## 7. Settlement flow
submit → [Compliance gate] → [Gate 1 Integrity] → [Gate 2 Reproducibility + complexity ceiling] → [Gate 3 machine pre-check → bounded provisional release] → [challenge window] → finalize (or council adjudication if challenged) → MetaWork Passport updated · Work-per-Watt recorded

## 8. Invariants (must not be violated)
- Nothing here mints base supply. All payouts are from the fee-funded treasury (MIP-0001 §5).
- No vendor privileged; attestation is one accepted integrity proof among several.
- Passport and Work-per-Watt influence reputation/prioritization only — never automatic minting.
- A task that cannot be cheaply verified is ineligible, not exempted.

## 9. Compliance & limits
Research only; not legal or financial advice. Export-control and securities review by qualified counsel required before any launch, conversion, or payout. The compliance gate reduces risk but does not eliminate operator liability.
