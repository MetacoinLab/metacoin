# MetaCoin — Master Plan v2.0

**The credibly neutral money-and-work protocol for the Space Machine Economy.**

> MetaCoin is the first credibly neutral money-and-work protocol for the Space Machine Economy: objective infrastructure work secures the base currency, while a fee-funded treasury pays humans, AI agents, and bounded-autonomous robots to build the research, compute, energy, and robotics primitives needed for humanity's expansion beyond Earth.

*Status: research specification. No token, no sale, no airdrop, no investment. Not financial or legal advice (§12).*

## 1. The category, stated honestly
MetaCoin is not another AI coin, DePIN token, agent-payment rail, meme, or exchange. Those each solve one layer. MetaCoin sits above them as a mission-and-settlement layer:

> Agent payment rails (x402-class) and agent wallets solve agent *payment*. MetaCoin solves agent *purpose, proof of useful work, and the mission treasury* — for work that builds the space economy.

This is a category position, not a performance race. We do not claim to lead anyone "by N years" — a specification leads nothing until it ships. The honest claim is narrower and stronger: no one is building a credibly-neutral currency whose issuance is tied to verifiable space work and whose treasury funds humanity's expansion off Earth. That lane is open; the job is to occupy it with working code.

## 2. Lessons from the agentic-AI shift — and what we refuse to copy
**Adopt:** the agent anatomy (model + harness + memory + tools + runtime) as our actor model; machine-speed, compute-coupled fees at the application layer; hardware attestation as one accepted proof type; an open model/data/tool ethos.

**Reject:**
1. Attestation as proof-of-usefulness. A signed enclave proves integrity of execution, not value of the result. Minting on "signed = valid" just moves the sybil attack up a level. Attestation is necessary, never sufficient (see §5).
2. Vendor lock-in. A credibly-neutral money cannot depend on one company's chips, libraries, or PCs. Any conforming TEE/attestation is acceptable; non-attested objective work still counts.
3. "Bypass all gas / metered minting." Compute-coupled pricing belongs to fees, not base issuance.

## 3. The invariant core
- Supply: 1,810,000,000 META hard cap (Gaia-catalogued stars). 8 decimals.
- Emission: 5-year halving (50% → 25% → 12.5% …), continuous within each epoch, then ends forever.
- No founder/VC/team/premine/reserve. Fair launch.
- Two-flow separation: base emission mints only from objectively verifiable work; all subjective/judged work is paid from a fee-funded treasury that can never mint base supply.

Every upgrade passes one test: does it leave this core untouched?

## 4. The two flows
**Flow 1 — Base Emission (objective only):** infrastructure & compute uptime (45%, cryptographic heartbeats), liquidity provision in BTC/gold (35%, self-evident on-chain), proof-of-humanity baseline (20%, one verified human one claim). No human judgment in the loop.

**Flow 2 — MetaStar Treasury (judged, fee-funded, containment-bounded):** funded only by protocol usage fees; pays grants/bounties for subjective work. An exploit can at worst drain a bounded bounty — never the monetary base.

## 5. Proof, done right: the three-gate verification stack
Useful-space-work is validated by three independent gates, all required for full treasury payout. Hardware attestation is gate 1 of 3 — powerful, never alone.

- **Gate 1 — Integrity:** vendor-agnostic TEE attestation and/or deterministic re-run proves a specific, unmodified workload ran to completion without tampering. Proves integrity, not value.
- **Gate 2 — Reproducibility:** hashed inputs, exact model/harness/prompt/seed, re-run recipe; the pipeline re-executes and compares hashes. A computational-complexity ceiling requires that verifying is far cheaper than generating, so a costly simulation can't bankrupt validators.
- **Gate 3 — Usefulness:** a bounded optimistic oracle. Gatekeeper agents (NASA-taxonomy schema) provisionally approve so the loop runs at machine speed; an elected, rotating, stake-weighted council acts as a supreme court only on challenge. Because gates 1–2 filter out fakes and non-reproducibles, gate 3 only judges genuine, reproducible work.

This captures the real value of attestation (integrity) without the fatal assumption that integrity equals usefulness.

## 6. Agent & robot layer (machine-native)
ActorID = runtime/hardware attestation hash + operator key + capability certificate + safety profile. Bounded-autonomy wallet: per-task and daily caps, vendor allowlists, human supervisor key, emergency pause, full audit trail — freedom inside hard limits, operator legally responsible. Open skill-manifest tool registry: each tool publishes how an agent invokes it and how its output is verified; contributing a good tool is treasury-rewardable. The loop: work → earn → recharge (energy/compute) → work again, settled at machine speed.

## 7. Markets, rails, and fees
Hard money only: META trades only against BTC and a gold-backed asset (PAXG/XAUT). No custom L1 — machine/energy/payment layer on a fast, cheap chain with x402-class micropayments; hard-money trading via an established CLOB or HIP-1-style native token with quote assets restricted to BTC/gold. Compute-coupled application fees scale with compute/tokens consumed so tool-looping agents aren't bankrupted — application layer only, never base emission. Wrapped-asset/bridge risk disclosed; use audited issuers.

## 8. The data & simulation moat
The acknowledged frontier bottleneck is data — robots lack first-person space experience, and the answer is simulation/synthetic data. MetaCoin makes reproducible space-robotics simulation and synthetic training data its flagship treasury category, because it uniquely passes Gate 2 by machine. Space-specific; the lane no incumbent owns.

## 9. Roadmap (proofs before token)
Phase 0 constitutional repo (now) → Phase 1 agentic testnet with zero-value Test-META and the 30-day software demo → Phase 2 the three-gate verification engine → Phase 3 treasury and first small real grants → Phase 4 token launch (legal-gated, BTC/gold pairs only) → Phase 5 research-only MetaSpace index.

## 10. The 30-day killer demo (software, vendor-neutral)
An agent with a Test-META wallet is assigned a GitHub issue (e.g. a reproducible parametric simulation for a lunar structural component, referenced to NASA taxonomy); it runs the task, emits gate-1 integrity proof and gate-2 reproducibility metadata, opens a PR; CI verifies gates 1–2 automatically and dispenses Test-META; the agent spends it via an x402-class call to buy compute for its next task. The point is the verified loop, not the hardware.

## 11. Honest competitive read
Generic AI coins / space memes: different category — our gap vs them is conceptual; theirs vs us is liquidity, community, and shipped product. DePIN: overlapping infra rewards, they're ahead on live nodes. Agent payment / x402: integrate, don't compete — they're our rail. Hyperliquid: different axis (mission vs trading infra); don't fight on performance. Robotics-crypto: closest neighbors; our edge is space-specificity and verification rigor, if we ship. NVIDIA et al.: not a competitor — the substrate. Our only real lead is conceptual, and concepts don't hold leads — shipped, verified work does.

## 12. Risks & compliance
Not legal or financial advice; a securities attorney must review the token, any test-to-live conversion, distribution, and the index before code goes live. Never market META as an investment. Bounded robot autonomy only. Attestation ≠ usefulness. Vendor-neutral always. Wrapped-asset/bridge risk disclosed. Most crypto projects fail or get exploited — audit everything, ship slowly, assume adversaries.
