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
  <img src="https://img.shields.io/github/last-commit/MetacoinLab/metacoin?label=last%20commit" alt="Last commit">
  <img src="https://img.shields.io/github/commit-activity/m/MetacoinLab/metacoin?label=commits" alt="Commit activity">
  <img src="https://img.shields.io/github/languages/top/MetacoinLab/metacoin" alt="Top language">
  <br>
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

<!--era-pin:entry_count=59 tip_hash_prefix=7575d1c32de2-->
> **Protocol state as of ledger entry <!--era:entry_count-->59<!--/era--> (tip index <!--era:tip_index-->58<!--/era-->, hash `<!--era:tip_hash_prefix-->7575d1c32de2<!--/era-->…`, August 2026).** Every number in this README hangs off that declared chain point, and `protocol/doc_verify.py` mechanically re-checks each tagged number against the chain **at that point** on every CI run — so this document stays verifiably green as the chain grows, and goes red only if it misstates its own era. The README is updated in deliberate monthly batches; the chain and `docs/` move faster and carry the live numbers.

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

<p align="center"><img src="assets/two-flow.svg" width="720" alt="Two-flow isolation: base emission mints only from objective work; a fee-funded treasury pays judged work and can never mint base supply"></p>

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

<p align="center"><img src="assets/three-gate.svg" width="720" alt="Three-gate verification: compliance, then integrity, reproducibility, and usefulness gates before treasury settlement"></p>

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

- **Reproducible task library** — <!--era:recorded_task_count-->17<!--/era--> reproducible space-engineering tasks spanning NASA-taxonomy domains (orbital mechanics & Lambert transfer, propulsion/Hohmann, robotics/ISAM arm IK, power budget, thermal equilibrium, ballistic re-entry/EDL, deep-space comms & Doppler, comms access windows, rover path planning, rendezvous/docking, FDIR fault management, Sabatier ISRU chemistry, TRIAD attitude determination, ISRU ascent propellant budgeting), wired into a task-agnostic verifier and an earn→verify→spend agent loop; the full demo and protocol self-test suites run on every push. The library carries its **first real provenance edge**: task-0017 (ascent budget) consumes task-0015's (Sabatier) output, recomputes the parent's canonical hash live on every execution, and refuses drifted input.
- **R1 — Tamper-evident ledger** — an append-only, hash-chained ledger; verification recomputes every hash from content and detects mutation, reordering, insertion, and deletion.
- **R2 — Honest attestation** — software-rooted HMAC attestation, anchored to the ledger. The hardware was investigated and found to have **no usable TPM/TEE**, so the mechanism is honestly labeled *software-rooted, not hardware* — a future hardware/public-key root drops into the same interface.
- **R3 — External verifier + coordinator** — an external party re-derives a task hash; the coordinator anchors the outcome only after **independently recomputing it** (a matching hash proves reproducibility, not who executed the task).
- **Public auditability** — `audit.py` exports a verifiable snapshot, verifies it standalone (no original ledger needed), and commits a tiny public tip anchor — closing the external-anchor gap so anyone can confirm the chain was not silently rewritten.
- **Autonomous agent-verifier** — a published, CI-tested tool that fetches the public snapshot, mechanically re-verifies the chain, checks the tip against the committed anchor, and re-runs the recorded task — **no LLM/AI judgment, hashes and re-runs only.**
- **Cross-platform reproducibility** — the canonical task hash `ff03231f…ba300c` reproduces byte-for-byte on **macOS (arm64)**, **Linux (aarch64)**, **Linux x86-64 (CI)**, and **Windows 11 (AMD64)** for the original thirteen-task era — and the CI environment independently re-derives all **seventeen** recorded task hashes against the published snapshot on every push (same-operator; reproducibility evidence, not third-party independence).
- **Full demo↔protocol wiring** — every recorded task is anchored in the tamper-evident ledger via honestly-labeled same-machine self-recompute evaluations plus **two batch autonomous-agent attestations** (idx 16 over the 13-task era; idx 52 re-deriving every recorded hash across both eras, the 13 historical outputs matching to the digit), each record carrying explicit honesty labels (`task_class: illustrative-demo`, topology, zero-value/no-token, limitation notes).
- **Provenance layer — Work Molecules** (`work-molecule/0.2`→`0.3`) — content-addressed provenance objects, one per task, each assembling the task's spec hash, actors, manifests, execution timestamps, hardware evidence, verification citations, and result hash into a single deterministic document with **three-state field semantics** (populated / asserted-empty / not-captured) and machine-readable **provenance debt**: missing evidence (energy cost, TEE attestation, execution detail) is listed explicitly, never fabricated. <!--era:catalog_anchor_count-->4<!--/era--> catalog generations are anchored — every one rebuilt independently by the coordinator before anchoring, every one still verifiable byte-for-byte forever (generation-locked rebuilds) — and generation 4 carries the chain's **first parented molecule**: task-0017's WMID contains its parent's WMID, so tampering with the parent cascades detection to the child.
- **Concentration self-measurement, now a time series** (`aci-report/0.1` → `aci-epoch-observation/0.1`) — the protocol measured its **own** agent-concentration baseline: pairwise **ACI <!--era:baseline_pairwise_aci-->0.99365<!--/era-->** over **<!--era:baseline_path_count-->28<!--/era--> same-operator verification paths** (five evidence dimensions; missing metadata scored worst-case, never as independence), anchored deliberately as the maximal-concentration starting point. The **k-order profile** (idx 44) extends it beyond pairs — with a hand-computed fixture demonstrating the pairwise blind spot it exists to close — and the **longitudinal epoch series** (from idx 57, ratified by MIP-0004) turns the measurement into a baseline over time: each epoch cites every prior anchored measurement and re-derives its deltas from anchored values only. Descriptive evidence, never a minting trigger.
- **Simulated agent economy, two anchored generations** — a deterministic earn→verify→spend loop over **30 simulated day indices** (not wall-clock time — nothing ran for 30 real days): zero-value Test-META is earned only on verified work, spent on simulated compute via the x402 stub, and a **planned tamper drill** per generation (labeled in-log as a drill, not fraud — day 17 in generation 1, day 23 in generation 2, deliberately different days) proves the rejection path. Generation 1 (13-task roster, idx 19) is frozen forever and still replays to the anchored hash to the digit; generation 2 (17-task roster, idx 55) anchored beside it with an on-record *replaces-nothing* statement. The **treasury accumulates across generations** (idx 56): each funding root's fees independently re-derived (3.0 + 3.0 = 6.0), conservation exact on the record (balance 5.2 + outstanding 0.8 == 6.0), caps and budgets restated unchanged — a budget change would be a governance event, not a funding extension.
- **Provenance debt paydown — compute/energy evidence** (`metering-report/0.1`, `work-molecule/0.3`) — wall-clock and CPU time were **measured** for all thirteen tasks and anchored **append-only** (idx 20); energy is labeled **estimated** (CPU time × an assumed 15 W nameplate figure — no hardware power telemetry exists on this host, and that remains open debt). Timing is non-reproducible by nature, so the anchored `report_hash` fixes the claim made at measurement time, not a recomputable value; the coordinator re-metered every task for plausibility (same output hashes, sane timings) before anchoring. Molecules absorb the evidence as a **new generation** (`work-molecule/0.3`, thirteen new WMIDs, idx 21) with machine-readable **debt_reduction** records that preserve the debt history — while the original 0.2 catalog stays verifiable forever: debt is reduced only by appending evidence, never by modifying a record.
- **Cut certificates** (`cut-certificate/0.1`) — bounded verification of the provenance graph: the coordinator runs the **expensive full proof once, at anchoring** (rebuild every interior molecule, recompute every WMID and the aggregate hash), and every later verifier can **accept cheaply** — with acceptance explicitly **conditional on the anchor plus continued retrievability** of the molecules (compression, never erasure). Two cuts are anchored: the first (idx 22) covered the then-flat graph and its record honestly calls itself degenerate; the **first non-trivial cut** (idx 54) is rooted at the parented molecule with a **real declared provenance edge crossing the boundary** — the boundary molecule is referenced, never rebuilt; that is the bound.
- **Trust Vector** (`trust-vector/0.1`) — the per-work evidence vector: **six separately-verified components** per task — integrity (deterministic re-run, software-rooted, no TEE), reproducibility (facts: event counts, statuses, canonical hash), independence (per-work actor count **plus the anchored maximal-concentration baseline**), provenance completeness (counted three-state slots, open debts and debt-reductions listed), usefulness (**honestly empty**: "not-assessed" — Gate 3's judgment seat is vacant and says so), and verification cost (estimated energy; ρ ≈ 1 stated as trivially uninformative). **No combined score exists, by mechanical rule** — the self-test scans every key for aggregation-shaped names and validation rejects any smuggled scalar. The 13-vector catalog was anchored (idx 23, regenerated at idx 27) only after the coordinator independently rebuilt every vector.

**Anchored since the last README batch (the idx 24–58 era):**

- **Challenge-response possession proofs** — nonce-bound rounds in which a verifier must re-derive a task under a fresh challenge; three verified rounds anchored, and the **copy-attack replay drill defeated twice** (idx 25, idx 30): a replayed response stays refuted on every re-verification, forever.
- **The complete identity lifecycle** — actor identity as a Merkle root over one-time Lamport keys: registrations, signed challenge rounds, staged key reserves, a legitimate **key rotation** (idx 41), and two defeated drills — **key reuse** rejected by the ledger-wide index scan and a **forged rotation** (idx 42) rejected from public material alone. Continuity of key possession, never proof of who operates a key: operator relationships remain declared, `-claimed`.
- **The two-flow constitution, executable** — the treasury as code with conservation asserted at every operation and **no mint path in the module** (the self-test greps for one); the Gate-3 bounded-optimistic lifecycle exercised end-to-end (grants, a challenged wrongful grant **clawed back** — bounded failure proven live at idx 35 — and a clean finalization) with the usefulness judgment seat **honestly vacant**; Flow-1 uptime emission from signed heartbeats with the **missed slot honestly paying zero** and a **forged heartbeat defeated** (idx 38).
- **MetaWork Passports + Useful-Work-per-Watt** — per-actor verified-contribution histories (idx 40) with a mechanical **no-leaderboard rule**, and UWW published as transparency only — neither ever mints or ranks.
- **Participant intake pipeline** — the six-rung validation ladder (schema → identity → signature → facts → replay scan → honest labels) rehearsed end-to-end on-ledger: identity registered (idx 45), a bundle anchored **participant-verified** (idx 46), and a **tampered bundle refused at the named rung** (idx 47). Nothing auto-anchors: a human `--confirm` gates every write.
- **Coordinator continuity + the weekly sentry** — recovery-manifest, public mirror export, and restore rehearsals proving the four capabilities (verify / sign / write / replay) from a restored home — including the public-only boundary rehearsal where signing honestly fails; a scheduled **weekly sweep** re-derives every layer, reconciles the evidence bundle both directions, and reports drift under its own expected-evolution heading.
- **The verified docs suite** — `docs/` (and `mip/`) under a mechanical anti-rot contract: every command executed in a fresh-clone sandbox, every number recomputed from live state, every ledger citation resolved, on every CI run. This README is **era-pinned** under the same machinery (see the state block above).
- **MIP governance, exercised** — <!--era:mip_decision_count-->1<!--/era--> anchored MIP decision (idx 58): MIP-0004 (ratifying the concentration epoch series) walked Draft → mechanical check → single-seat decision → anchored record, with the **seat statement on-chain** — the review seat has one occupant and says so — and the decided file pinned by sha256: **immutable-by-citation, amendments are new MIPs**.
- **One-command full-stack verification** — `python3 protocol/verify_everything.py --full` (or `metacoin verify`) mechanically re-verifies **every layer above** — chain+anchor, all seventeen tasks, four molecule generations, concentration (baseline, k-order, epoch series), both economy generations, metering claims, both cut certificates, trust vectors, challenges, identity, treasury+Gate-3, governance, Flow-1 emission, passports, and participant intake — with zero LLM judgment, and is **fresh-clone proven**: every layer's evidence ships in-repo (`protocol/evidence/`, privacy-checked), so a stranger's clone verifies end-to-end with no local-only inputs.

**Defeated attacks on the record:** eight scripted adversarial demonstrations — six distinct on-ledger attacks defeated across <!--era:drill_entry_count-->7<!--/era--> drill-labeled entries (two challenge replays, a Gate-3 wrongful grant challenged *and* clawed back, a forged heartbeat, a forged key rotation, a tampered intake bundle) plus the two planned in-log economy tamper rejections (one per generation). Every one is labeled a drill on its record — scripted by the operator, never presented as "detected fraud" — and every refusal re-proves on every verification run.

### The ledger, by layer

The chain is past per-line listing (<!--era:entry_count-->59<!--/era--> entries); the full record-by-record narration lives in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md), mechanically verified on every CI run. The map:

```text
work            idx 0-16, 48-52    genesis; external verification; agent attestations;
                                   self-recomputes — the 13-task then 17-task eras
provenance      idx 17, 20-22,     molecule catalogs generations 1-4; metering evidence;
                    26, 53-54      both cut certificates (idx 54: the first non-trivial cut)
concentration   idx 18, 44, 57     pairwise baseline; k-order profile; the epoch series
economy         idx 19, 55-56      economy generations 1-2; the cross-generation
                                   treasury funding extension
trust           idx 23, 27, 40     trust-vector catalogs (no combined score);
                                   MetaWork passports (history, never a leaderboard)
challenge +     idx 24-25, 28-30,  nonce possession rounds + defeated replay drills;
  identity          37, 41-43      Lamport roots, rotation, defeated forged-rotation drill
two-flow        idx 31-36, 38-39   treasury constitution + Gate-3 lifecycle (bounded
                                   clawback drill); Flow-1 heartbeat epoch + forged-
                                   heartbeat drill
intake          idx 45-47          participant identity; six-rung verified bundle;
                                   tampered-bundle rejection
governance      idx 58             MIP-0004 accepted — the first anchored MIP decision
                                   (single-seat, stated on the record)

public tip anchor: 7575d1c3… (59 entries; committed at protocol/ledger_anchor.json)
```

**Honest boundary.** Every entry so far is the **same operator** on machines under direct control — including every self-recompute record, explicitly labeled as adding *no cross-party independence*. The concentration series **quantifies** that status and now tracks it over time: the frozen epoch-zero baseline measured pairwise ACI <!--era:baseline_pairwise_aci-->0.99365<!--/era--> over <!--era:baseline_path_count-->28<!--/era--> paths; the latest anchored epoch measures <!--era:epoch_pairwise_aci-->0.998508<!--/era--> over <!--era:epoch_path_count-->66<!--/era--> paths — rising toward 1 exactly as expected when one operator accumulates paths, which is what happened, and which is what the anchored record says. Self-declared relationships from intake are honored as declarations only (`-claimed`, never verified) and can never lower measured concentration. The catalog-wide claim remains **cross-platform reproducibility under one operator** — *not* independent multi-party consensus. The next meaningful milestone is still operational, not code: an unaffiliated third party running the public verifier and submitting a result. These docs will say so until it happens — the epoch series exists so that day has a baseline.

### Verify it yourself

Don't trust — reproduce. One command mechanically re-verifies **every layer** — sixteen of them, from the hash chain and all seventeen tasks through the epoch series, both economy generations, and the anchored MIP decision — with no LLM/AI judgment anywhere:

```bash
# install the product (from source — not yet published to PyPI):
pip install git+https://github.com/MetacoinLab/metacoin        # straight from GitHub
# ...or from a clone / an unpacked release tarball:
#   pip install .

metacoin verify            # re-derive everything (--full is the default), ~a few minutes
metacoin verify --quick    # bounded-cost anchored acceptance
metacoin status            # one-screen honest chain state
metacoin participate       # the participant-kit path (six-rung validated bundles)
metacoin --help            # every protocol capability as a subcommand
```

The complete verification corpus ships **inside the package** — a pip-installed
`metacoin` fully verifies from an empty directory with no checkout at all (a
cold-install acceptance test in CI enforces it). The clone-and-run path works
exactly as before (`python3 protocol/verify_everything.py --full`); packaging
is additive.

Every layer's evidence ships in the repo (the published snapshot, the committed tip anchor, and the privacy-checked bundle in `protocol/evidence/`), so a fresh clone verifies completely — the CI self-test proves it by re-running the whole stack from a tracked-files-only copy. Each report line is labeled `VERIFIED-FULL`, `CLAIM-CHECK` (metering: timing is honestly non-reproducible), or `ACCEPTED-BY-ANCHOR` (`--quick`: conditional acceptance, never presented as proof), and the report ends with the honest boundary — what a pass does **not** establish. The layer-by-layer tools remain available (`audit.py --verify`, `agent_verifier.py --verifier-id "$(whoami)-independent"`, and the canonical worked example `ff03231f…ba300c` for task-0002).

### Submit your verification

If you ran the verifier and reached a verdict, you can submit it for anchoring:

1. Run `python3 protocol/agent_verifier.py --verifier-id <your-handle> --out result.json`
2. Open a GitHub Issue titled `Verification result: <your-handle>` and attach or paste `result.json`
3. The coordinator re-derives every claim locally (the same `--anchor-agent-result` path used for all existing entries) and anchors the outcome — match or mismatch — to the public ledger

The first verification from an unaffiliated party will be the project's first cross-party independence record — every entry so far is same-operator, and the ledger says so on each record.

---

## Roadmap

| Phase | Goal | State |
|---|---|---|
| 0 | Constitutional repo — whitepaper, tokenomics, MIP drafts, legal; governance lifecycle exercised (MIP-0004 anchored, idx 58) | `[BUILT]` |
| 1 | Agentic testnet — zero-value Test-META, 30-day earn→spend agent demo | `[BUILT]` (simulated days, zero value; two generations anchored at idx 19 and idx 55) |
| 2 | Verification engine — the three-gate stack from MIP-0002 | `[SPEC]` |
| 3 | Treasury & first small real grants — milestone-based | `[SPEC]` |
| 4 | Token launch — legal-gated, BTC/gold pairs only | `[SPEC]` |
| 5 | MetaSpace research index — research only, no product without licensed partners | `[SPEC]` |

---

## Repository

- [`WHITEPAPER.md`](WHITEPAPER.md) — full architecture (Master Plan v2.0)
- [`TOKENOMICS.md`](TOKENOMICS.md) — supply, 5-year halving, the two-flow split
- [`ROADMAP.md`](ROADMAP.md) · [`MISSION.md`](MISSION.md) · [`HISTORY.md`](HISTORY.md)
- [`docs/`](docs/) — the mechanically verified suite: architecture (the chain layer by layer), verification guide, trust model, participation
- [`mip/MIP-0001-genesis.md`](mip/MIP-0001-genesis.md) · [`mip/MIP-0002-proof-of-useful-space-work.md`](mip/MIP-0002-proof-of-useful-space-work.md) · [`mip/MIP-0004-concentration-epochs.md`](mip/MIP-0004-concentration-epochs.md) (accepted, anchored at idx 58)
- [`protocol/`](protocol/) — ledger, attestation, verifiers, auditability, molecule provenance, cuts, concentration, challenges, identity, treasury+Gate-3, uptime emission, passports, intake, continuity, sweep, MIP process, docs anti-rot
- [`demo/`](demo/) — the seventeen reproducible space-engineering tasks + the simulated economy generations
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
