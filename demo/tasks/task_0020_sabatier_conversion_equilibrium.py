"""task-0020-sabatier-conversion-equilibrium — deterministic Sabatier
equilibrium conversion at stated T and P, judged against task-0015's assumed
single-pass conversion, WITH AN HONEST NEGATIVE (the second task born under
MIP-0008 / MIP-0009 law; parented on task-0019 AND task-0015).

Research-only. A bit-reproducible equilibrium-extent task: for a
stoichiometric feed (1 mol CO2 + 4 mol H2) the equilibrium extent xi of
CO2 + 4 H2 -> CH4 + 2 H2O (all gas, ideal) at temperature T and pressure P
solves ln Q(xi) = ln K_eq(T), where ln K_eq(T) is CONSUMED from task-0019's
emitted grid (the CEA-pinned equilibrium constants) and the mission threshold
is CONSUMED from task-0015's inputs — the 92 % single-pass conversion that
task-0015's mass balance (and through it task-0017's ascent budget) assumes.
The verdict field the mission needs, per instance:
    conversion_acceptable = (equilibrium xi >= task-0015's assumed conversion)
It maps to the NASA Technology Taxonomy TX07 (Exploration Destination Systems
— ISRU): the thermodynamic reality check on the reactor operating point.

THE HONEST NEGATIVE, KEPT ON PURPOSE: methanation is exothermic, so
equilibrium turns against it as T rises. At the REFERENCE operating point
(700 K, 1 bar) the equilibrium conversion is ~0.81, BELOW the 0.92 that
task-0015 assumes — reference_conversion_acceptable is FALSE, and every
grid instance above ~600 K at 1 bar is false too. The thresholds and grid
are NOT to be tuned to manufacture success: a verification protocol must be
comfortable anchoring "no", and this task tells the upstream chain exactly
where its assumption stops being thermodynamically honest (cooler, or at
pressure).

THE PROVENANCE EDGES, ENFORCED AT EXECUTION TIME: compute() CALLS
task_0019.compute() and task_0015.compute() directly, recomputes each
parent's canonical output hash LIVE, and asserts equality with the pinned
EXPECTED_*_OUTPUT_HASH constants — drifted parents are refused, not consumed.
The recomputed parent hashes are recorded in this task's own inputs block, so
the emitted artifact carries its lineage verbatim.

INTERNAL SELF-PROOF (four assertion classes, all inside compute() per
MIP-0008 rule 1): compute() asserts
  (a) PARENT-HASH LIVENESS for both parents (the provenance edges);
  (b) CONVERGENCE — at every solved instance |ln Q(xi) - ln K| <=
      RESIDUAL_TOL after at most MAX_BISECTION_ITER bounded bisection steps
      (the loop bound is stated, per MIP-0008 rule 2);
  (c) KNOWN-TRUTH, Le Chatelier — at fixed P, xi is non-increasing with T
      (exothermic), and at fixed T, xi is non-decreasing with P (mole
      number decreases, 5 -> 3);
  (d) ELEMENT CONSERVATION — at every solved xi the C, H, O atom balances of
      the equilibrium mixture close to within ATOM_TOL.
A violated assertion CRASHES the task — stop, don't fudge.

Test-META is a zero-value testnet placeholder and never mints base supply
(MIP-0001 paragraph 3, MIP-0002 paragraph 8). Ideal-gas, single-reaction
equilibrium (no reverse water-gas shift, no carbon deposition, no kinetics,
no recycle) — NOT a reactor design. Not financial, legal, or
flight-engineering advice. No NASA affiliation or endorsement.

Standard library only (json, math, hashlib) plus the two parent task modules.
No randomness. Every emitted float is rounded to a fixed number of decimals so
re-runs are byte-identical and the SHA-256 output hash is stable (the basis of
the Gate-2 check). MIP-0009 contract: compute() -> the four-key dict,
canonical_json() era-2 (sign-of-zero-free), output_hash() = sha256 of it.
"""

import hashlib
import json
import math

# THE PARENT TASKS. Package import when loaded as demo.tasks.task_0020_...;
# bare import when run as a script from demo/tasks/.
try:
    from demo.tasks import task_0019_sabatier_equilibrium_constant as _keq_parent
    from demo.tasks import task_0015_sabatier_isru as _isru_parent
except ImportError:  # direct script run: demo/tasks/ itself is sys.path[0]
    import task_0019_sabatier_equilibrium_constant as _keq_parent
    import task_0015_sabatier_isru as _isru_parent

# Declared parentage (consumed by protocol/work_molecule.py: the molecule
# builder resolves each entry to the parent's WMID within the same build).
PARENT_TASKS = ["task-0019", "task-0015"]

# The parents' canonical Gate-2 output hashes, PINNED. compute() recomputes
# both live and asserts equality — the executable provenance edges.
EXPECTED_KEQ_PARENT_OUTPUT_HASH = (
    "86269d4065cbff31c88bd4145b6c0add2991b426f20724d9c28c6c8e7a5c5f61"
)
EXPECTED_ISRU_PARENT_OUTPUT_HASH = (
    "dab7a6208aa1a751d4f38ec955e9f54aeb61ca6aa16e49c7e5f6cb5e2a924ae1"
)

# --- Fixed inputs (part of the reproducibility hash) ------------------------
# Changing any of these changes the canonical output and therefore the Gate-2
# hash. THE CONSTANTS ARE NOT TO BE TUNED TO MANUFACTURE SUCCESS (docstring).
T_GRID_K = (400.0, 500.0, 600.0, 700.0, 800.0, 900.0, 1000.0, 1100.0, 1200.0)
P_GRID_BAR = (1.0, 10.0)
P_STANDARD_BAR = 1.0            # the polynomials' standard-state pressure
T_REFERENCE_K = 700.0           # the reference operating point judged in summary
P_REFERENCE_BAR = 1.0
FEED_CO2_MOL = 1.0              # stoichiometric feed basis
FEED_H2_MOL = 4.0
MAX_BISECTION_ITER = 200        # the stated loop bound (rule 2)
XI_LOWER = 1e-12                # open interval: ln of zero is undefined
XI_UPPER = 1.0 - 1e-12
RESIDUAL_TOL = 1e-9             # |ln Q - ln K| at the solution
ATOM_TOL_MOL = 1e-12
MONOTONE_TOL = 1e-9
ROUND_DECIMALS = 6


def _ln_q(xi: float, p_bar: float) -> float:
    """ln of the reaction quotient in mole fractions, corrected for pressure
    (delta nu = -2): Q = y_CH4 y_H2O^2 / (y_CO2 y_H2^4) * (P/P0)^-2."""
    n_co2 = FEED_CO2_MOL - xi
    n_h2 = FEED_H2_MOL - 4.0 * xi
    n_ch4 = xi
    n_h2o = 2.0 * xi
    n_tot = n_co2 + n_h2 + n_ch4 + n_h2o
    return (math.log(n_ch4 / n_tot) + 2.0 * math.log(n_h2o / n_tot)
            - math.log(n_co2 / n_tot) - 4.0 * math.log(n_h2 / n_tot)
            - 2.0 * math.log(p_bar / P_STANDARD_BAR))


def solve_extent(ln_k: float, p_bar: float):
    """Bounded bisection for xi in (0, 1) with ln Q(xi) = ln K. Returns
    (xi, iterations, residual). ln Q is strictly increasing in xi, so the
    bracket [XI_LOWER, XI_UPPER] always contains exactly one root."""
    lo, hi = XI_LOWER, XI_UPPER
    iterations = 0
    for _ in range(MAX_BISECTION_ITER):  # bounded by MAX_BISECTION_ITER
        mid = 0.5 * (lo + hi)
        if _ln_q(mid, p_bar) < ln_k:
            lo = mid
        else:
            hi = mid
        iterations += 1
        if hi - lo <= 1e-15:
            break
    xi = 0.5 * (lo + hi)
    return xi, iterations, abs(_ln_q(xi, p_bar) - ln_k)


def compute() -> dict:
    """Equilibrium conversion grid + the mission verdict, with the self-proofs."""
    # --- SELF-PROOF (a): parent-hash liveness, both parents (the provenance edges).
    keq_result = _keq_parent.compute()
    keq_hash = _keq_parent.output_hash(keq_result)
    assert keq_hash == EXPECTED_KEQ_PARENT_OUTPUT_HASH, (
        f"parent-hash liveness violated: task-0019 recomputes to {keq_hash}, "
        f"expected {EXPECTED_KEQ_PARENT_OUTPUT_HASH} — this task refuses drifted input")
    isru_result = _isru_parent.compute()
    isru_hash = _isru_parent.output_hash(isru_result)
    assert isru_hash == EXPECTED_ISRU_PARENT_OUTPUT_HASH, (
        f"parent-hash liveness violated: task-0015 recomputes to {isru_hash}, "
        f"expected {EXPECTED_ISRU_PARENT_OUTPUT_HASH} — this task refuses drifted input")

    # The threshold the mission chain assumes — consumed from the parent's
    # PUBLISHED inputs, never restated here.
    threshold = float(isru_result["inputs"]["single_pass_conversion"])
    # ln K_eq per grid temperature — consumed from task-0019's PUBLISHED
    # (rounded) rows: the number the parent's artifact carries is the number
    # this task judges.
    ln_k_by_t = {row["temperature_K"]: row["ln_k_eq_dimensionless"]
                 for row in keq_result["results"]}

    rows = []
    xi_table = {}
    max_residual = 0.0
    max_iterations = 0
    for p_bar in P_GRID_BAR:          # bounded: two pressures
        for t_K in T_GRID_K:          # bounded: nine temperatures
            ln_k = ln_k_by_t[t_K]
            xi, iterations, residual = solve_extent(ln_k, p_bar)
            # --- SELF-PROOF (b): convergence within the stated bound.
            assert residual <= RESIDUAL_TOL and iterations <= MAX_BISECTION_ITER, (
                f"convergence violated at {t_K} K / {p_bar} bar: residual {residual:.3e} "
                f"after {iterations} iterations (bound {MAX_BISECTION_ITER})")
            # --- SELF-PROOF (d): element conservation at the solved extent.
            n_co2, n_h2, n_ch4, n_h2o = (FEED_CO2_MOL - xi, FEED_H2_MOL - 4.0 * xi,
                                         xi, 2.0 * xi)
            c_bal = abs((n_co2 + n_ch4) - FEED_CO2_MOL)
            h_bal = abs((2.0 * n_h2 + 4.0 * n_ch4 + 2.0 * n_h2o) - 2.0 * FEED_H2_MOL)
            o_bal = abs((2.0 * n_co2 + n_h2o) - 2.0 * FEED_CO2_MOL)
            assert max(c_bal, h_bal, o_bal) <= ATOM_TOL_MOL, (
                f"element conservation violated at {t_K} K / {p_bar} bar: "
                f"C {c_bal:.2e} H {h_bal:.2e} O {o_bal:.2e} mol")
            max_residual = max(max_residual, residual)
            max_iterations = max(max_iterations, iterations)
            xi_table[(t_K, p_bar)] = xi
            rows.append({
                "temperature_K": t_K,
                "pressure_bar": p_bar,
                "ln_k_eq_dimensionless": ln_k,
                "equilibrium_conversion_fraction": round(xi, ROUND_DECIMALS),
                "ch4_yield_mol": round(n_ch4, ROUND_DECIMALS),
                "h2o_yield_mol": round(n_h2o, ROUND_DECIMALS),
                "total_moles_mol": round(n_co2 + n_h2 + n_ch4 + n_h2o, ROUND_DECIMALS),
                "bisection_iterations_count": iterations,
                "conversion_acceptable": bool(xi >= threshold),
            })

    # --- SELF-PROOF (c): Le Chatelier known truths on the solved table.
    for p_bar in P_GRID_BAR:          # bounded: two pressures
        for i in range(len(T_GRID_K) - 1):  # bounded: eight adjacent pairs
            assert xi_table[(T_GRID_K[i + 1], p_bar)] <= xi_table[(T_GRID_K[i], p_bar)] + MONOTONE_TOL, (
                f"Le Chatelier violated: conversion rose with temperature at {p_bar} bar "
                f"between {T_GRID_K[i]} and {T_GRID_K[i + 1]} K")
    for t_K in T_GRID_K:              # bounded: nine temperatures
        assert xi_table[(t_K, P_GRID_BAR[1])] >= xi_table[(t_K, P_GRID_BAR[0])] - MONOTONE_TOL, (
            f"Le Chatelier violated: conversion fell with pressure at {t_K} K")

    xi_ref = xi_table[(T_REFERENCE_K, P_REFERENCE_BAR)]
    acceptable_count = sum(1 for r in rows if r["conversion_acceptable"])
    # Highest grid temperature at which the threshold still holds, per pressure
    # (None if no grid point qualifies) — the operating envelope, honestly.
    max_ok_t = {}
    for p_bar in P_GRID_BAR:          # bounded: two pressures
        ok_ts = [t for t in T_GRID_K if xi_table[(t, p_bar)] >= threshold]
        max_ok_t[f"max_acceptable_temperature_at_{p_bar:g}_bar_K"] = (
            max(ok_ts) if ok_ts else None)

    return {
        "task_id": "task-0020-sabatier-conversion-equilibrium",
        "inputs": {
            "reaction": "CO2 + 4 H2 -> CH4 + 2 H2O (all gas, ideal mixture)",
            "feed_co2_mol": FEED_CO2_MOL,
            "feed_h2_mol": FEED_H2_MOL,
            "t_grid_K": list(T_GRID_K),
            "p_grid_bar": list(P_GRID_BAR),
            "p_standard_bar": P_STANDARD_BAR,
            "t_reference_K": T_REFERENCE_K,
            "p_reference_bar": P_REFERENCE_BAR,
            "conversion_threshold_fraction": threshold,
            "threshold_source": "task-0015 inputs.single_pass_conversion (consumed, not restated)",
            "ln_k_source": "task-0019 results (published rounded rows, consumed)",
            "parent_output_hashes": {"task-0019": keq_hash, "task-0015": isru_hash},
            "max_bisection_iter_count": MAX_BISECTION_ITER,
            # the tolerance is 1e-9 — below the six-decimal boundary, so it is
            # emitted as its decimal exponent (an integer), per MIP-0009 C3
            "residual_tol_exponent_dimensionless": int(round(math.log10(RESIDUAL_TOL))),
            "round_decimals": ROUND_DECIMALS,
        },
        "results": rows,
        "summary": {
            "reference_temperature_K": T_REFERENCE_K,
            "reference_pressure_bar": P_REFERENCE_BAR,
            "reference_equilibrium_conversion_fraction": round(xi_ref, ROUND_DECIMALS),
            "reference_conversion_acceptable": bool(xi_ref >= threshold),
            "reference_shortfall_fraction": round(max(0.0, threshold - xi_ref), ROUND_DECIMALS),
            "acceptable_instances_count": acceptable_count,
            "instances_count": len(rows),
            **max_ok_t,
            "max_residual_dimensionless": round(max_residual, ROUND_DECIMALS),
            "max_bisection_iterations_count": max_iterations,
            "honest_negative_note": (
                "the reference point fails the upstream chain's assumption; the "
                "constants are not tuned to manufacture success"),
            "self_proofs_checked": ["parent_hash_liveness_x2", "bisection_convergence",
                                    "le_chatelier_monotonicity", "element_conservation"],
        },
    }


def _sign_safe_zero(obj):
    """Era-2 canonical rule (ledger idx 67): -0.0 -> 0.0 throughout, WITHOUT
    recursion (MIP-0008 rule 3) — a JSON round-trip with a float parse hook."""
    return json.loads(json.dumps(obj),
                      parse_float=lambda text: 0.0 if float(text) == 0.0 else float(text))


def canonical_json(result: dict) -> str:
    """Era-2 canonical serialization: sorted keys, compact, ASCII, sign-of-zero-free."""
    return json.dumps(_sign_safe_zero(result), sort_keys=True,
                      separators=(",", ":"), ensure_ascii=True)


def output_hash(result: dict) -> str:
    """SHA-256 hex digest of the canonical JSON (the Gate-2 reproducibility hash)."""
    return hashlib.sha256(canonical_json(result).encode("utf-8")).hexdigest()


if __name__ == "__main__":
    _result = compute()
    print(canonical_json(_result))
    print("sha256:" + output_hash(_result))
