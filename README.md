<p align="center">
  <img src="assets/metacoin-logo.svg" width="300" alt="MetaCoin emblem">
</p>

<h1 align="center">MetaCoin</h1>

<p align="center">
  <strong>The credibly neutral money-and-work protocol for the Space Machine Economy.</strong><br>
  <em>Money for machines building the stars.</em>
</p>

<p align="center">
  <a href="https://github.com/MetacoinLab/metacoin/actions"><img src="https://img.shields.io/github/actions/workflow/status/MetacoinLab/metacoin/ci.yml?branch=main&label=CI&logo=github" alt="CI"></a>
  <img src="https://img.shields.io/badge/stage-research-blue" alt="Research stage">
  <img src="https://img.shields.io/badge/token-none-lightgrey" alt="No token">
  <img src="https://img.shields.io/badge/license-SML--1.0-yellow" alt="SML-1.0">
</p>

> Gold was the money of the old world. Bitcoin became the money of the digital world. MetaCoin is *designed* to become the work-and-energy currency of the Space Machine Economy.

---

## Status

**Research stage. A protocol specification, not a product.** No token, no sale, no airdrop, no investment. Nothing here is financial, legal, or investment advice. See [`legal/`](legal/) and [`LICENSE.md`](LICENSE.md). The repository *is* the project — there is no marketing website.

This README describes two distinct things, and labels which is which:

- **`[SPEC]`** — the designed architecture (the proposal / what MetaCoin is meant to become).
- **`[BUILT]`** — what actually exists and has been mechanically verified in this repository today.

We keep that line sharp on purpose. A design is not a deployment, and we never write the future in the present tense.

---

## One-sentence thesis

> MetaCoin is a credibly neutral, fair-launch base currency for the Space Machine Economy: minted only through objective, programmatic, hard-to-fake infrastructure work — while a separate, fee-funded MetaStar Treasury pays humans, AI agents, and bounded-autonomous robots to build the software, energy, robotics, and research primitives of the space economy.

---

## What makes it different

- **Fair launch.** No founder allocation, no VC, no premine, no hidden reserve, no discretionary minting.
- **Unforgeable base money.** The base currency is *designed* to mint only from objectively verifiable work — infrastructure uptime, on-chain liquidity, proof-of-humanity — never from subjective judgment.
- **A fee-funded mission treasury for the judged work.** Subjective but valuable work (space research, AI-agent reports, robotics bounties, simulations) is paid from the MetaStar Treasury, which is funded only by protocol usage fees — never by minting base supply.
- **Hard money only.** Designed to be referenced against Bitcoin and gold-backed primitives (PAXG/XAUT).
- **Machine-native, for space.** Built around verifiable machine work and energy-constrained autonomous systems, on existing rails (Base / x402-class flows) rather than a new L1.

---

## Architecture `[SPEC]`

### The two-flow isolation

The single most important design decision: base emission and mission grants are strictly separated, so that subjective review can never mint base money.

```text
+---------------------------------------------------------------+
|                  TWO-FLOW ISOLATION  [SPEC]                   |
+---------------------------------------------------------------+
|                                                               |
|  FLOW 1 — BASE EMISSION  (objective only, Bitcoin-like)       |
|    infra uptime · liquidity · proof-of-humanity              |
|        |                                                      |
|        +--> mints --> 1.81B META hard cap, 5-year halving     |
|                                                               |
|  ===========  the base supply NEVER funds subjective work  == |
|                                                               |
|  FLOW 2 — METASTAR TREASURY  (fee-funded, bounded)            |
|    protocol-usage fees --> bounties for judged work:          |
|    AI research · robotics · simulations · open source         |
|                                                               |
+---------------------------------------------------------------+
```

Bitcoin mining is objective — any node can cheaply verify a valid block. Deciding whether an AI report is *useful* requires judgment. If that judgment minted base supply, a review committee would become a central bank. So judged work is routed to a bounded treasury instead, where a failed bounty costs only the treasury budget — never the monetary base.

> **Base emission = objective work only. Judged work = treasury bounty only.**

### Supply & emission `[SPEC]`

Designed total supply: **1,810,000,000 META** — one MetaCoin for each mapped star in humanity's great star catalogue. (A design parameter. **No token exists; nothing is minted, sold, or transferable.**)

| Epoch | Years | META released | Cumulative |
|---|---:|---:|---:|
| 1 | 0–5 | 905,000,000 | 50.000% |
| 2 | 5–10 | 452,500,000 | 75.000% |
| 3 | 10–15 | 226,250,000 | 87.500% |
| 4 | 15–20 | 113,125,000 | 93.750% |
| 5 | 20–25 | 56,562,500 | 96.875% |

Designed base-emission channels (objective only, no human judgment in the loop): infrastructure & compute uptime **45%** (cryptographic heartbeats; optional TEE attestation) · BTC/gold liquidity provision **35%** (self-evident on-chain) · proof-of-humanity baseline **20%** (one verified human, one claim). Values marked default, pending confirmation; the *structure* — two-flow separation, no reserve — is the invariant.

### Proof-of-Useful-Space-Work: the three-gate stack `[SPEC]`

> PoUSW governs the **treasury** (Flow 2) — it never mints base supply. It validates judged work through three independent gates, all required for full settlement. The core principle: **integrity ≠ reproducibility ≠ usefulness** — each is verified separately, and cheap gates run first to filter the expensive, subjective gate down to a small surface.

```text
   submit
     |
     v
 [ COMPLIANCE ]  signed attestation: inputs/code are public-domain or
     |           verified open-source (export-control + copyright guard)
     v
 [ GATE 1 — INTEGRITY ]      was it actually run as claimed?
     |   vendor-agnostic TEE attestation  AND/OR  deterministic re-run.
     |   proves execution integrity — NOT value. (necessary, not sufficient)
     v
 [ GATE 2 — REPRODUCIBILITY ]  can anyone re-derive it independently?
     |   hashed inputs + exact model/harness/prompt/seed + re-run recipe;
     |   pipeline re-runs and compares hashes. verifying must be far
     |   cheaper than generating (the complexity ceiling).
     v
 [ GATE 3 — USEFULNESS ]   does it matter for the mission?
     |   bounded optimistic oracle: machine pre-check (NASA taxonomy) →
     |   bounded provisional release → challenge window → rotating,
     |   stake-weighted council adjudicates ONLY challenged work.
     v
  finalize  ->  MetaWork Passport updated  ·  Useful-Work-per-Watt recorded
```

Hardware attestation is **Gate 1 of 3 — powerful, never alone.** A signed enclave proves a workload ran untampered; it does not prove the result was useful. This is exactly why minting on "signed = valid" only moves the sybil attack up a level — and why MetaCoin refuses to do it.

- **Objective PoUSW** (eligible for base emission): signed node/oracle uptime, liquidity proofs, reproducible compute-job proofs, proof-of-humanity.
- **Subjective PoUSW** (treasury bounty only): AI reports, paper reproduction, robotics design, simulations, tool contributions.

### The data & simulation moat `[SPEC]`

The frontier bottleneck for space robotics is **data** — machines lack first-person space experience, and the answer is simulation and synthetic data. MetaCoin makes reproducible space-robotics simulation and synthetic training data its flagship treasury category, precisely because it uniquely passes Gate 2 *by machine*: high-fidelity simulation, trajectory/kinematics optimization, and synthetic datasets, mapped to the NASA Technology Taxonomy and the ISAM problem set. A **MetaWork Passport** records each actor's verified contribution history; **Useful-Work-per-Watt** is published per submission as a transparency metric — never as an automatic minting trigger (that would reintroduce a gameable printer).

### MetaAgent & bounded robots `[SPEC]`

Agentic payment rails let agents *pay*. MetaCoin defines what useful space-machine work agents should be *paid for*. Every MetaAgent report must carry reproducibility metadata (source hashes, model/version, seed, output hash) or it is ineligible for payment. Robots get **bounded** autonomy — spending caps, allowed-resource sets, a human supervisor key, an emergency pause — never unlimited autonomy. Phases 1–2 use software agents and simulated/terrestrial robotics only; real orbital/lunar deployment is a long-term research direction, not a near-term claim.

---

## What's built & verified `[BUILT]`

Everything in this section exists in the repository and is exercised by CI. Each item is deliberately scoped — these are mechanical, deterministic facts, not marketing.

- **Reproducible task demo** — six space-engineering tasks (link budget, orbit propagation, eclipse/power, comms access windows, rover path planning, rendezvous/docking), wired into a task-agnostic verifier and an earn→verify→spend agent loop. CI 10/10.
- **R1 — Tamper-evident ledger** — an append-only, hash-chained ledger; verification recomputes every hash from content and detects mutation, reordering, insertion, and deletion.
- **R2 — Honest attestation** — software-rooted HMAC attestation, anchored to the ledger. The hardware was investigated and found to have **no usable TPM/TEE**, so the mechanism is honestly labeled *software-rooted, not hardware* — a future hardware/public-key root drops into the same interface.
- **R3 — External verifier + coordinator** — an external party re-derives a task hash; the coordinator anchors the outcome only after **independently recomputing it** (a matching hash proves reproducibility, not who executed the task).
- **Public auditability** — `audit.py` exports a verifiable snapshot, verifies it standalone (no original ledger needed), and commits a tiny public tip anchor — closing the external-anchor gap so anyone can confirm the chain was not silently rewritten.
- **Autonomous agent-verifier** — a published, CI-tested tool that fetches the public snapshot, mechanically re-verifies the chain, checks the tip against the committed anchor, and re-runs the recorded task — **no LLM/AI judgment, hashes and re-runs only.**
- **Cross-platform reproducibility** — task `ff03231f…ba300c` reproduces byte-for-byte on **macOS (arm64)**, **Linux (aarch64 + CI x86-64)**, and **Windows 11 (AMD64)**.

### Current ledger

```text
idx 0  genesis (neutral chain-start marker)
idx 1  external verification (task reproduced)
idx 2  autonomous-agent attestation — macOS    (Spark-reconfirmed)
idx 3  autonomous-agent attestation — Windows  (Spark-reconfirmed)
public tip anchor: 7b71b88f…
```

**Honest boundary.** Every entry so far is the **same operator** on machines under direct control. This demonstrates strong **cross-platform reproducibility** — *not* independent multi-party consensus. The next meaningful milestone is operational, not code: an unaffiliated third party running the public verifier and submitting a result.

---

## Roadmap

| Phase | Goal | State |
|---|---|---|
| 0 | Constitutional repo — whitepaper, tokenomics, MIP-0001/0002, legal | `[BUILT]` |
| 1 | Agentic testnet — zero-value Test-META, 30-day earn→spend agent demo | demo + protocol spine `[BUILT]`; full Test-META faucet `[SPEC]` |
| 2 | Verification engine — the three-gate stack from MIP-0002 | `[SPEC]` |
| 3 | Treasury & first small real grants — milestone-based | `[SPEC]` |
| 4 | Token launch — legal-gated, BTC/gold pairs only | `[SPEC]` |
| 5 | MetaSpace research index — research only, no product without licensed partners | `[SPEC]` |

---

## Repository

- [`WHITEPAPER.md`](WHITEPAPER.md) — full architecture (Master Plan v2.0)
- [`TOKENOMICS.md`](TOKENOMICS.md) — supply, 5-year halving, the two-flow split
- [`ROADMAP.md`](ROADMAP.md) · [`MISSION.md`](MISSION.md) · [`HISTORY.md`](HISTORY.md)
- [`mip/MIP-0001-genesis.md`](mip/MIP-0001-genesis.md) · [`mip/MIP-0002-proof-of-useful-space-work.md`](mip/MIP-0002-proof-of-useful-space-work.md)
- [`protocol/`](protocol/) — ledger, attestation, external & autonomous verifiers, auditability
- [`demo/`](demo/) — six reproducible space-engineering tasks
- [`legal/`](legal/) — disclaimers and risk notes

## Lineage

The `@MetacoinLab` GitHub organization and the `metacoin.ai` domain were both registered in 2023, when the AI-machine-economy thesis was years ahead of the market. That foresight — not 2023 code — is the lineage. The 2026 work is the execution.

## License

Source-available under the **MetaCoin Sovereign Mission License v1.0 (SML-1.0)** — research, study, and local non-commercial execution are permitted; commercial/mainnet/fundraising use of the code or protected marks requires explicit authorization. SML-1.0 is **not** an OSI open-source license. See [`LICENSE.md`](LICENSE.md).

---

```text
============================================================
 RESEARCH-STAGE SPECIFICATION. NO TOKEN EXISTS. NO AIRDROP.
 NOT INVESTMENT, FINANCIAL, LEGAL, OR ENGINEERING ADVICE.
 The MetaSpace Index is a research/education concept only —
 not an investment product, no securities, no trading access.
============================================================
```
