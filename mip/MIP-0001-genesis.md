# MIP-0001 — Genesis

**Status:** Draft · **Layer:** Protocol · **Supersedes:** none · **Depends on:** none
**Note:** Research specification only. No token exists. Not financial or legal advice.

This MIP defines MetaCoin's monetary base: total supply, emission schedule, the no-reserve fair-launch rule, and the hardcoded separation between **base emission** and the **fee-funded treasury**. All other MIPs depend on the structures defined here. Values marked `[DEFAULT]` are recommended starting points pending confirmation; the *structure* (two-flow separation, no reserve) is invariant.

## 1. Unit definitions
- **Name:** MetaCoin · **Ticker:** `META` (✦META) `[DEFAULT — alt: NOVA/VEGA/LUME]`
- **Total supply (hard cap):** 1,810,000,000 META — fixed forever, never increased.
- **Decimals:** 8 `[DEFAULT]`
- **Rationale:** scaled to humanity's largest actual star catalogue (ESA Gaia, ~1.8B mapped stars) — one MetaCoin per charted star.

## 2. Genesis state
- Premined balances: zero. No tokens to any founder, contributor, investor, treasury, or team wallet at genesis.
- No reserve. The treasury (§5) starts empty and fills only from protocol usage fees.
- The entire supply enters circulation only through §4, earned via the objective channels in §3.

## 3. The base-emission rule (invariant)
New base supply mints exclusively for objectively and cheaply verifiable work, no human quality judgment:
| Channel | Default share | Verification |
|---|---|---|
| Infrastructure & compute uptime | 45% `[DEFAULT]` | Cryptographic heartbeats; optional TEE attestation |
| Liquidity provision (BTC/gold) | 35% `[DEFAULT]` | Self-evident on-chain |
| Proof-of-humanity baseline | 20% `[DEFAULT]` | One verified human, one claim (PoH-gated) |

Hard rule: subjective work may never mint base supply; it is paid only from the treasury (§5).

## 4. Emission schedule — 5-year halving
Each 5-year epoch releases half the remaining supply, dripped continuously (per-block), then ends forever.
| Epoch | Years | META released | Cumulative |
|---|---|---|---|
| 1 | 0–5 | 905,000,000 | 50.000% |
| 2 | 5–10 | 452,500,000 | 75.000% |
| 3 | 10–15 | 226,250,000 | 87.500% |
| 4 | 15–20 | 113,125,000 | 93.750% |
| 5 | 20–25 | 56,562,500 | 96.875% |
| 6+ | 25+ | geometric tail → terminal release | → 100% |

## 5. The MetaStar Treasury (fee-funded, separate)
- Funded only by protocol usage fees (trading, machine energy payments, compute/API routing, donations). Never minted from §4.
- Pays subjective/judged work as grants and bounties (rubric: MIP-0002).
- Containment: an exploit can at worst drain a bounded bounty — never the base currency.
- No multisig backdoor; treasury spending follows the governed grants process.

## 6. Fair-launch guarantees (invariant)
No founder allocation; no VC/investor allocation; no team premine; no hidden reserve; no insider pre-distribution. The only "free" channel is the PoH baseline (§3) — a minority of emission, one-human-one-claim.

## 7. Governance
Supply, decimals, halving cadence, the emission/treasury separation, and the §3 channel set are fixed in code and amendable only by transparent on-chain MIPs. Voting uses anti-whale dampening (quadratic or capped).

## 8. Deferred (NOT required for genesis)
First trading rail; gold-backed quote asset(s) (PAXG/XAUT/both); proof-of-humanity provider. `[DECISIONS PENDING]`

## 9. Compliance
A securities attorney must review token design, any test-to-live conversion, distribution, and the research index before any code goes live. Research only; not investment, financial, or legal advice. No token exists.
