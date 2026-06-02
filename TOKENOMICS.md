# Tokenomics

This summarizes the monetary design. The authoritative source is `mip/MIP-0001-genesis.md`; this file must stay consistent with it. Research only; no token exists; not investment advice.

## Supply
- **Hard cap:** 1,810,000,000 META — fixed forever, never increased.
- **Decimals:** 8.
- **Rationale:** scaled to humanity's largest actual star catalogue (ESA Gaia, ~1.8B mapped stars) — one MetaCoin per charted star.

## No reserve, fair launch
No founder allocation, no VC/investor allocation, no team premine, no hidden reserve, no insider pre-distribution. Genesis balances are zero. The entire supply enters circulation only through emission, earned via objective work.

## Two-flow separation (the invariant)
- **Base emission** mints only from objectively verifiable work — infrastructure/compute uptime, on-chain liquidity, and a proof-of-humanity baseline. Unforgeable; no human quality judgment.
- **MetaStar Treasury** is funded only by protocol usage fees and pays all subjective/judged work (research, AI-agent reports, robot bounties) as grants. It can never mint base supply.

Subjective judgment never touches the monetary base. An exploit can at worst drain a bounded treasury bounty — never the base currency.

## Base-emission channels (defaults, pending confirmation)
| Channel | Share | Verification |
|---|---|---|
| Infrastructure & compute uptime | 45% | Cryptographic heartbeats; optional TEE attestation |
| Liquidity provision (BTC/gold) | 35% | Self-evident on-chain |
| Proof-of-humanity baseline | 20% | One verified human, one claim |

## Emission schedule — 5-year halving
Each 5-year epoch releases half the remaining supply, dripped continuously per block, then ends forever.

| Epoch | Years | META released | Cumulative |
|---|---|---|---|
| 1 | 0–5 | 905,000,000 | 50.000% |
| 2 | 5–10 | 452,500,000 | 75.000% |
| 3 | 10–15 | 226,250,000 | 87.500% |
| 4 | 15–20 | 113,125,000 | 93.750% |
| 5 | 20–25 | 56,562,500 | 96.875% |
| 6+ | 25+ | geometric tail → terminal release | → 100% |

## Markets
Hard money only: META is designed to trade only against Bitcoin and a gold-backed asset (e.g. PAXG/XAUT). No fiat pairs, no meme pairs. Wrapped BTC/gold and bridges carry issuer/bridge risk; use audited issuers and disclose openly.

## Governance
Supply, decimals, halving cadence, the emission/treasury separation, and the channel set are fixed in code and amendable only by transparent on-chain MIPs, with anti-whale voting dampening.

Provisional and subject to change. Research only; not investment, financial, or legal advice.
