# MetaCoin — Phase 1 Agentic Testnet Demo

**Research-only. This is a testnet demonstration, not a product.** `Test-META` is a
**zero-value testnet placeholder token** — it is not for sale, has no price, no market,
and is not an investment of any kind. Nothing here is financial, investment, or legal
advice. See [`../legal/disclaimers.md`](../legal/disclaimers.md).

This directory holds a **working Phase 1 demo** for the loop described in
[`../ROADMAP.md`](../ROADMAP.md) (Phase 1) and [`../WHITEPAPER.md`](../WHITEPAPER.md) §10.
It runs entirely in-process using the Python **standard library only** — there is **no
real ledger, no network, no wallet, and no payment**. The faucet is an in-memory dictionary
and the x402 "payment" is a simulated function call. The point is to demonstrate the
*verification loop* (MIP-0002), vendor-neutrally, in software.

## Goal: the verified earn→spend loop

Demonstrate, end to end, that an autonomous agent can earn a zero-value testnet token by
completing an **objectively verifiable, reproducible task**, and then spend it to fund its
next unit of work — proving the *verification loop*, not the hardware. Six genuinely
space-relevant tasks are now wired through this loop.

## The loop (as implemented in `agent_loop.py`)

```
   ┌──────────────────────────────────────────────────────────────────────┐
   │                                                                        │
   │   1. ASSIGN ── a round is given a specific reproducible task           │
   │        │         (one of task-0001 … task-0006, per round)             │
   │        ▼                                                                │
   │   2. RUN ───── the task's compute() runs deterministically             │
   │        │                                                                │
   │        ▼                                                                │
   │   3. PROVE ─── build a submission: the result + its canonical          │
   │        │         SHA-256 output hash (a tampered round alters one       │
   │        ▼         number after hashing, to be caught at Gate 2)          │
   │   4. VERIFY ── the task-agnostic verifier checks Gate 1 + Gate 2        │
   │        │         (Gate 3 is out of scope for this software demo)        │
   │        ▼                                                                │
   │   5. DISPENSE ─ on pass ONLY, the faucet dispenses zero-value           │
   │        │         Test-META to the agent's in-memory balance            │
   │        ▼                                                                │
   │   6. SPEND ──── the agent spends Test-META via a simulated x402-class   │
   │        │         call to "buy compute" for the next round              │
   │        └──────────────────────────► back to 1. ASSIGN                   │
   │                                                                        │
   └──────────────────────────────────────────────────────────────────────┘
```

In a production setting, ASSIGN/PROVE/VERIFY would map to a GitHub issue, a Pull Request,
and CI. In this demo the whole cycle runs **in one process** against an in-memory ledger;
CI's role here is to run the self-tests on every push (see below), not to gate payments.

## The six reproducible tasks

All six tasks are **deterministic and byte-reproducible**: identical inputs produce a
byte-identical canonical JSON and therefore a stable SHA-256 output hash. All expose the
same interface — `compute() -> dict`, `canonical_json(result) -> str`,
`output_hash(result) -> str` — so the verifier and loop treat them interchangeably. Each
task's honest simplifications are stated plainly in its module docstring (summarized below).

| Task | What it computes | NASA Technology Taxonomy | File |
|---|---|---|---|
| **task-0001** | Lunar link budget: free-space path loss and link margin over a range sweep. *Illustrative figures, not an engineering claim.* | **TX05** (Communications, Navigation, and Orbital Debris Tracking & Characterization) | `tasks/task_0001_lunar_link_budget.py` |
| **task-0002** | Two-body (Keplerian) orbit propagation around Earth: ECI position/velocity over one ISS-like orbit, via a bounded Newton-Raphson solve of Kepler's equation. | **TX17** (Guidance, Navigation, and Control) / astrodynamics | `tasks/task_0002_orbit_propagation.py` |
| **task-0003** | Orbital eclipse + solar-power/energy budget: per-step sunlight/umbra classification (cylindrical-shadow model) and a battery state-of-charge integration over one orbit. **Simplification:** a **fixed-Sun (single-β) model** — a deterministic stand-in for a solar ephemeris, representative of one geometry, not a seasonal average. | **TX03** (Aerospace Power and Energy Storage) | `tasks/task_0003_power_eclipse.py` |
| **task-0004** | Ground-station communication access windows: topocentric elevation of the satellite over a fixed station, with discrete passes (start/end/duration/max-elevation) above an elevation mask. **Simplification:** a **fixed sidereal epoch** (GMST₀ = 0) and a **spherical Earth** (no ellipsoid, no terrain, no refraction). | **TX05** (Communications, Navigation, and Orbital Debris Tracking & Characterization) | `tasks/task_0004_comms_access.py` |
| **task-0005** | Rover lowest-energy path planning over synthetic terrain: Dijkstra (heapq) over an integer-cost grid where uphill moves cost extra. **Simplification:** **synthetic terrain** (a closed-form height function, not a real DEM), a **simplified energy model**, and **grid discretization** (4-connectivity). Determinism is guaranteed by integer costs and a **documented fixed total-order tie-break** (heap key `(cost, cell_index, cell)`). | **TX04** (Robotic Systems) | `tasks/task_0005_rover_path.py` |
| **task-0006** | Rendezvous/docking approach-corridor check: closed-form Clohessy-Wiltshire relative motion vs. an approach cone + safe closing-speed limit. **Simplification:** **linearized CW**, a **circular reference orbit**, **open-loop** motion (no control loop, no sensor noise), and an **idealized corridor**. | **TX17** (GN&C — Rendezvous, Proximity Operations & Docking / RPOD) | `tasks/task_0006_docking_approach.py` |

Reproducibility is achieved by fixing all inputs, using documented deterministic methods
(e.g. task-0002's Kepler solve has a fixed tolerance **and** a hard max-iteration cap, so it
is bit-reproducible and can never loop unboundedly; task-0005 uses integer costs plus a fixed
tie-break so the path never depends on heap-ordering luck; task-0006 uses the **closed-form**
CW state transition — no numerical integration, hence no step-size dependence), and rounding
every emitted float to a fixed number of decimals so the canonical JSON is byte-stable across
runs.

## Mapping to the MIP-0002 three-gate stack

The demo exercises the verification stack from
[`../mip/MIP-0002-proof-of-useful-space-work.md`](../mip/MIP-0002-proof-of-useful-space-work.md).
**Attestation is one gate of three, never the whole proof.**

| MIP-0002 gate | Question | In this demo |
|---|---|---|
| **Gate 1 — Integrity** | Was the workload actually run, unmodified? | A **documented software stand-in**, not real hardware: `verify_gates.py` re-runs the task and confirms the execution is deterministic. This is *not* a hardware TEE attestation — production Gate 1 is a vendor-agnostic TEE attestation and/or validator re-run (MIP-0002 §2). The stand-in proves execution stability only. |
| **Gate 2 — Reproducibility** | Can anyone re-derive it independently? | **The real check.** The verifier independently re-runs `compute()`, recomputes the canonical hash, and **auto-rejects** unless *both* the submitted hash *and* the submitted result are byte-identical to the re-run. `assert_task_reproducible()` adds an active **two-run** check: it runs `compute()` twice and asserts the two canonical hashes match. |
| **Gate 3 — Usefulness** | Does it matter for the mission? | **Out of scope / deferred.** Gate 3 is the bounded optimistic oracle + human council in MIP-0002 §2; here we automate only Gates 1–2, which is sufficient to prove the earn→spend loop. |

> **Invariant (MIP-0001 §3, MIP-0002 §8):** nothing in this demo mints base supply.
> Test-META is a testnet faucet artifact with zero value, dispensed only for objectively
> verifiable Gate-1/Gate-2 work. It models the loop; it is not money.

## Components

| File | Role |
|---|---|
| `tasks/task_0001_lunar_link_budget.py` | Reproducible task-0001 (link budget, TX05). Prints canonical JSON + SHA-256 when run. |
| `tasks/task_0002_orbit_propagation.py` | Reproducible task-0002 (two-body orbit propagation, TX17). Prints canonical JSON + SHA-256 when run. |
| `tasks/task_0003_power_eclipse.py` | Reproducible task-0003 (eclipse + power/energy budget, TX03; fixed-Sun model). Prints canonical JSON + SHA-256 when run. |
| `tasks/task_0004_comms_access.py` | Reproducible task-0004 (ground-station comms access windows, TX05; fixed sidereal epoch, spherical Earth). Prints canonical JSON + SHA-256 when run. |
| `tasks/task_0005_rover_path.py` | Reproducible task-0005 (rover lowest-energy path planning, TX04; synthetic terrain, integer-cost Dijkstra with documented tie-break). Prints canonical JSON + SHA-256 when run. |
| `tasks/task_0006_docking_approach.py` | Reproducible task-0006 (rendezvous/docking CW approach-corridor check, TX17/RPOD; closed-form CW, open-loop). Prints canonical JSON + SHA-256 when run. |
| `verify_gates.py` | **Task-agnostic** verifier: a task registry + `_resolve_task()`, the Gate-1 integrity stand-in, the Gate-2 reproducibility check, and `assert_task_reproducible()` (active two-run hash compare). Its self-test exercises all six tasks (honest PASS, tampered REJECT, two-run reproducible). Defaults to task-0001 when no task is given. |
| `test_meta_faucet.py` | Zero-value Test-META faucet over an **in-memory** ledger. `dispense()` credits **only** on a passing `verify()`; `spend()` is a guarded debit (rejects insufficient/non-positive amounts). Crediting and debiting are name-mangled private methods reachable only through these two guarded entry points. |
| `x402_spend_stub.py` | **Simulated** x402-class micropayment. Spends Test-META only via `faucet.spend()` to "buy" imaginary compute units — no real x402 network, HTTP 402 flow, or payment. |
| `agent_loop.py` | Orchestrates the full cycle for **all six** tasks end-to-end: assign → run → prove → verify → dispense → spend. Its self-test asserts per-round **task identity** and **verify pass/fail outcome** (against independent hardcoded expectations), plus the aggregate totals. |
| `run_all_selftests.sh` | One-command runner that executes all ten self-tests, prints a summary table, and exits non-zero if any fail. |
| `tasks/example_task.md` | A sample reproducible-task spec (the conceptual assigned issue). |

CI: [`../.github/workflows/ci.yml`](../.github/workflows/ci.yml) runs
`demo/run_all_selftests.sh` on every push and pull request to `main` (Ubuntu, Python 3.x,
no dependencies installed — the demo is standard-library only).

## How to run

Each module is self-testing: running it executes its own checks and exits non-zero on
failure. From the **repository root**:

Run a single task (prints its canonical JSON and SHA-256 output hash):

```bash
python3 demo/tasks/task_0001_lunar_link_budget.py
python3 demo/tasks/task_0002_orbit_propagation.py
python3 demo/tasks/task_0003_power_eclipse.py
python3 demo/tasks/task_0004_comms_access.py
python3 demo/tasks/task_0005_rover_path.py
python3 demo/tasks/task_0006_docking_approach.py
```

Run an individual component self-test:

```bash
python3 demo/verify_gates.py        # all six tasks: honest PASS, tampered REJECT, two-run reproducible
python3 demo/test_meta_faucet.py    # dispense-on-verify and guarded-spend invariants
python3 demo/x402_spend_stub.py     # simulated micropayment within/over balance
python3 demo/agent_loop.py          # full loop over all six tasks + per-round assertions
```

Run **all ten** self-tests at once (this is what CI runs):

```bash
bash demo/run_all_selftests.sh
```

The runner prints a per-test header and a final summary table; expect **10/10 passed** and
exit code 0.

## Compliance

Research only; not legal or financial advice. Test-META has **zero value** and is a
testnet placeholder. A securities attorney must review any token design, any test-to-live
conversion, and any distribution before any value-bearing system is built. Export-control
(ITAR/EAR) review is required for any space-technology work. See
[`../legal/disclaimers.md`](../legal/disclaimers.md).
