# MetaCoin — Phase 1 Agentic Testnet Demo

**Research-only. This is a testnet specification, not a product.** `Test-META` is a
**zero-value testnet placeholder token** — it is not for sale, has no price, no market,
and is not an investment of any kind. Nothing here is financial, investment, or legal
advice. See [`../legal/disclaimers.md`](../legal/disclaimers.md).

This directory holds the **skeleton** for the Phase 1 demo described in
[`../ROADMAP.md`](../ROADMAP.md) (Phase 1) and [`../WHITEPAPER.md`](../WHITEPAPER.md) §10.
At this step there is **no working logic** — only structure and specs. Stub modules carry
a single `# TODO` header and will be implemented in later steps.

## Goal: the 30-day verified loop

Demonstrate, end to end, that an autonomous agent can earn a zero-value testnet token by
completing an **objectively verifiable, reproducible task**, and then spend it to fund its
next unit of work — proving the *verification loop*, not the hardware. The point is to show
that the MetaCoin proof model (MIP-0002) works in software, vendor-neutrally.

## The loop

```
   ┌──────────────────────────────────────────────────────────────────────┐
   │                                                                        │
   │   1. ASSIGN ── agent is given a reproducible task (a GitHub issue)     │
   │        │         e.g. a parametric simulation referenced to the        │
   │        │         NASA Technology Taxonomy (see tasks/example_task.md)  │
   │        ▼                                                                │
   │   2. RUN ───── agent executes the task in a sandboxed runtime          │
   │        │                                                                │
   │        ▼                                                                │
   │   3. PROVE ─── agent emits:                                            │
   │        │         • Gate-1 integrity proof (attestation / det. re-run)  │
   │        │         • Gate-2 reproducibility metadata (hashes, seed, …)   │
   │        ▼                                                                │
   │   4. SUBMIT ── agent opens a Pull Request with artifacts + metadata    │
   │        │                                                                │
   │        ▼                                                                │
   │   5. VERIFY ── CI automatically checks Gate 1 + Gate 2                 │
   │        │         (gate 3 is out of scope for this software demo)       │
   │        ▼                                                                │
   │   6. DISPENSE ─ on pass, the Test-META faucet dispenses zero-value     │
   │        │         Test-META to the agent's testnet wallet              │
   │        ▼                                                                │
   │   7. SPEND ──── agent spends Test-META via an x402-class call to       │
   │        │         buy compute for the next task                        │
   │        └──────────────────────────► back to 1. ASSIGN                  │
   │                                                                        │
   └──────────────────────────────────────────────────────────────────────┘
```

## Mapping to the MIP-0002 three-gate stack

The demo exercises the verification stack from
[`../mip/MIP-0002-proof-of-useful-space-work.md`](../mip/MIP-0002-proof-of-useful-space-work.md).
**Hardware attestation is one gate of three, never the whole proof.**

| MIP-0002 gate | Question | In this demo | Stub |
|---|---|---|---|
| **Gate 1 — Integrity** | Was the workload actually run, unmodified? | Vendor-agnostic TEE attestation **and/or** a deterministic re-run by CI. | `verify_gates.py` |
| **Gate 2 — Reproducibility** | Can anyone re-derive it independently? | Hashed inputs, exact model/harness/prompt/seed, dependency manifest, re-run recipe; CI re-runs and compares output hashes. A **computational-complexity ceiling** keeps verification far cheaper than generation. | `verify_gates.py` |
| **Gate 3 — Usefulness** | Does it matter for the mission? | **Out of scope for this software demo.** Gate 3 is the bounded optimistic oracle + human council in MIP-0002 §2; here we only automate Gates 1–2, which is sufficient to prove the earn→spend loop. | — (future) |

> **Invariant (MIP-0001 §3, MIP-0002 §8):** nothing in this demo mints base supply.
> Test-META is a testnet faucet artifact with zero value, dispensed only for
> objectively verifiable Gate-1/Gate-2 work. It models the loop; it is not money.

## Files in this directory

| File | Role (skeleton — `# TODO` only at this step) |
|---|---|
| `agent_loop.py` | Orchestrates the loop: assign → run → prove → submit → spend. |
| `verify_gates.py` | CI-side Gate-1 (integrity) and Gate-2 (reproducibility) checks. |
| `test_meta_faucet.py` | Dispenses zero-value Test-META on verified pass. |
| `x402_spend_stub.py` | x402-class call to spend Test-META on compute for the next task. |
| `tasks/example_task.md` | A sample reproducible task (the assigned GitHub issue). |

## Scope of this step

- ✅ Directory structure and specifications.
- ✅ Stub files with `# TODO` headers, no logic.
- ❌ No working agent, no real verification, no faucet, no payments — added in later Phase 1 steps.

## Compliance

Research only; not legal or financial advice. Test-META has **zero value** and is a
testnet placeholder. A securities attorney must review any token design, any test-to-live
conversion, and any distribution before any value-bearing system is built. Export-control
(ITAR/EAR) review is required for any space-technology work. See
[`../legal/disclaimers.md`](../legal/disclaimers.md).
