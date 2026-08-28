"""task-0019-sabatier-equilibrium-constant — deterministic Sabatier equilibrium
constant K_eq(T) from NASA CEA's pinned 9-coefficient polynomials (THE FIRST
TASK BORN UNDER MIP-0008 / MIP-0009 LAW, and the first CEA-pinned task).

Research-only. A bit-reproducible thermochemistry task: for the methanation
reaction CO2 + 4 H2 -> CH4 + 2 H2O (all gas), the standard Gibbs energy of
reaction dG(T) = dH(T) - T dS(T) is evaluated on a bounded temperature grid
from the NASA 9-coefficient polynomials of the four species, and the
equilibrium constant follows as ln K_eq(T) = -dG(T) / (R T). The coefficient
blocks are NOT typed into this file: they are pinned verbatim in
demo/tasks/cea_thermo_pinned.py with full provenance (nasa/cea v3.3.3, tag
commit 059439a3..., data/thermo.inp sha256 fa774657..., fetched 2026-08-28) —
the PASS-1 provenance-pinning discipline in its first real use. It maps to the
NASA Technology Taxonomy TX07 (Exploration Destination Systems — in-situ
resource utilization): the thermodynamic ceiling of the Mars Sabatier reactor
that task-0015's mass balance assumes and task-0017's ascent budget consumes.

THE POLYNOMIAL EVALUATION is implemented exactly in CEA's documented form
(cea_thermo_pinned module docstring): H/(RT) and S/R from a1..a7, b1, b2 per
temperature interval; the interval is chosen as the first whose [t_low, t_high]
contains T (the fits are continuous at the joins, and this task PROVES that at
the 1000 K join rather than assuming it).

INTERNAL SELF-PROOF (four assertion classes, all inside compute() per
MIP-0008 rule 1): compute() asserts
  (a) KNOWN-TRUTH, in-file — for every species, H(298.15 K) evaluated from
      the polynomial equals the block's OWN Hf(298.15) header field to within
      HF_TOL_J_MOL (the file checks itself against itself);
  (b) KNOWN-TRUTH, join continuity — at the 1000 K interval boundary the two
      adjacent fits of every species agree in H and S to within JOIN_TOL;
  (c) LITERATURE SANITY — the reaction enthalpy at 298.15 K lies within
      LITERATURE_DH_TOL_J_MOL of the widely quoted -165.0 kJ/mol for CO2
      methanation (a tolerance check against a published figure, THEN the
      round-and-hash discipline governs the emitted number);
  (d) VAN'T HOFF CONSISTENCY — between every adjacent pair of grid points,
      the change in ln K equals -(dH_mid / R) * (1/T2 - 1/T1) to within
      VANT_HOFF_TOL (dH evaluated at the pair's midpoint temperature): the
      emitted curve is self-consistent with its own enthalpy.
A violated assertion CRASHES the task — stop, don't fudge.

Test-META is a zero-value testnet placeholder and never mints base supply
(MIP-0001 paragraph 3, MIP-0002 paragraph 8). Ideal-gas standard-state
thermochemistry (pure-species polynomials; no fugacity, no side reactions
such as reverse water-gas shift, no kinetics) — NOT a reactor design.
Not financial, legal, or flight-engineering advice. No NASA affiliation or
endorsement; the CEA data are reproduced under Apache-2.0 with attribution.

Standard library only (json, math, hashlib) plus the pinned data module. No
randomness. Every emitted float is rounded to a fixed number of decimals so
re-runs are byte-identical and the SHA-256 output hash is stable (the basis of
the Gate-2 check). MIP-0009 contract: compute() -> the four-key dict,
canonical_json() era-2 (sign-of-zero-free), output_hash() = sha256 of it.
"""

import hashlib
import json
import math

# The pinned CEA data module: package import when loaded as
# demo.tasks.task_0019_...; bare import when run as a script from demo/tasks/.
try:
    from demo.tasks import cea_thermo_pinned as _cea
except ImportError:  # direct script run: demo/tasks/ itself is sys.path[0]
    import cea_thermo_pinned as _cea

# No parent TASK: this task consumes pinned external data, not another task's
# output. (task-0020 is parented on THIS task.)
PARENT_TASKS = []

# --- Fixed inputs (part of the reproducibility hash) ------------------------
# Changing any of these changes the canonical output and therefore the Gate-2
# hash. The constants are NOT to be tuned to manufacture agreement.
R_J_MOL_K = _cea.GAS_CONSTANT_J_MOL_K
SPECIES = ("CO2", "H2", "CH4", "H2O")
# Stoichiometric coefficients (products positive): CO2 + 4 H2 -> CH4 + 2 H2O
NU = {"CO2": -1.0, "H2": -4.0, "CH4": 1.0, "H2O": 2.0}
T_GRID_START_K = 400.0
T_GRID_STOP_K = 1200.0
T_GRID_STEP_K = 50.0
MAX_GRID_POINTS = 64          # the loop bound (17 points are used)
T_REFERENCE_K = 298.15
T_JOIN_K = 1000.0             # the interval boundary proved continuous
LITERATURE_DH_298_J_MOL = -165000.0   # widely quoted CO2-methanation enthalpy
LITERATURE_DH_TOL_J_MOL = 1000.0
HF_TOL_J_MOL = 10.0
JOIN_TOL_J_MOL = 0.01         # H continuity at the 1000 K join
JOIN_TOL_J_MOL_K = 0.001      # S continuity at the 1000 K join
VANT_HOFF_TOL = 0.01          # absolute, in ln-K units, per adjacent pair
ROUND_DECIMALS = 6


def _interval(species: dict, t_K: float) -> dict:
    """First pinned interval whose [t_low, t_high] contains t_K (bounded scan)."""
    for iv in species["intervals"]:  # bounded by the pinned interval count (<= 3)
        if iv["t_low_K"] <= t_K <= iv["t_high_K"]:
            return iv
    raise ValueError(f"temperature {t_K} K outside the pinned polynomial range")


def _h_s(species: dict, t_K: float, iv: dict = None):
    """(H in J/mol, S in J/(mol K)) from the NASA 9-coefficient form."""
    iv = iv if iv is not None else _interval(species, t_K)
    a1, a2, a3, a4, a5, a6, a7 = iv["a"]
    b1, b2 = iv["b"]
    ln_t = math.log(t_K)
    h_rt = (-a1 / t_K ** 2 + a2 * ln_t / t_K + a3 + a4 * t_K / 2.0
            + a5 * t_K ** 2 / 3.0 + a6 * t_K ** 3 / 4.0 + a7 * t_K ** 4 / 5.0
            + b1 / t_K)
    s_r = (-a1 / (2.0 * t_K ** 2) - a2 / t_K + a3 * ln_t + a4 * t_K
           + a5 * t_K ** 2 / 2.0 + a6 * t_K ** 3 / 3.0 + a7 * t_K ** 4 / 4.0
           + b2)
    return h_rt * R_J_MOL_K * t_K, s_r * R_J_MOL_K


def reaction_dh_ds(data: dict, t_K: float):
    """(dH, dS) of reaction at t_K, J/mol and J/(mol K)."""
    dh = 0.0
    ds = 0.0
    for sp in SPECIES:  # bounded: four species
        h, s = _h_s(data[sp], t_K)
        dh += NU[sp] * h
        ds += NU[sp] * s
    return dh, ds


def ln_k_eq(data: dict, t_K: float) -> float:
    dh, ds = reaction_dh_ds(data, t_K)
    return -(dh - t_K * ds) / (R_J_MOL_K * t_K)


def compute() -> dict:
    """K_eq(T) on the bounded grid, with the four self-proofs."""
    data = {sp: _cea.parse_species(sp) for sp in SPECIES}

    # --- SELF-PROOF (a): in-file known truth — the polynomial reproduces the
    # block's own Hf(298.15) header field for every species.
    for sp in SPECIES:  # bounded: four species
        h_298, _s = _h_s(data[sp], T_REFERENCE_K)
        assert abs(h_298 - data[sp]["hf_298_J_mol"]) <= HF_TOL_J_MOL, (
            f"known-truth violated: {sp} H(298.15 K) from the polynomial is "
            f"{h_298:.3f} J/mol but the pinned block's own Hf field says "
            f"{data[sp]['hf_298_J_mol']:.3f} J/mol")

    # --- SELF-PROOF (b): join continuity at 1000 K — the two adjacent fits agree.
    for sp in SPECIES:  # bounded: four species
        iv_low = data[sp]["intervals"][0]
        iv_high = data[sp]["intervals"][1]
        h_lo, s_lo = _h_s(data[sp], T_JOIN_K, iv_low)
        h_hi, s_hi = _h_s(data[sp], T_JOIN_K, iv_high)
        assert (abs(h_lo - h_hi) <= JOIN_TOL_J_MOL
                and abs(s_lo - s_hi) <= JOIN_TOL_J_MOL_K), (
            f"join continuity violated for {sp} at {T_JOIN_K} K: "
            f"dH={h_lo - h_hi:.6f} J/mol, dS={s_lo - s_hi:.6f} J/(mol K)")

    # --- SELF-PROOF (c): literature sanity at 298.15 K (tolerance, then rounding).
    dh_298, ds_298 = reaction_dh_ds(data, T_REFERENCE_K)
    assert abs(dh_298 - LITERATURE_DH_298_J_MOL) <= LITERATURE_DH_TOL_J_MOL, (
        f"literature sanity violated: dH(298.15 K) = {dh_298:.1f} J/mol vs the "
        f"published ~{LITERATURE_DH_298_J_MOL:.0f} J/mol (tol {LITERATURE_DH_TOL_J_MOL})")

    # --- The grid (bounded loop: MAX_GRID_POINTS) ---
    n_points = int(round((T_GRID_STOP_K - T_GRID_START_K) / T_GRID_STEP_K)) + 1
    assert 2 <= n_points <= MAX_GRID_POINTS, "grid size outside the stated bound"
    rows = []
    raw = []  # (T, dH, lnK) unrounded, for the Van't Hoff proof
    for i in range(n_points):  # bounded by MAX_GRID_POINTS
        t_K = T_GRID_START_K + i * T_GRID_STEP_K
        dh, ds = reaction_dh_ds(data, t_K)
        dg = dh - t_K * ds
        lnk = -dg / (R_J_MOL_K * t_K)
        raw.append((t_K, dh, lnk))
        rows.append({
            "temperature_K": t_K,
            "delta_h_J_mol": round(dh, ROUND_DECIMALS),
            "delta_s_J_mol_K": round(ds, ROUND_DECIMALS),
            "delta_g_J_mol": round(dg, ROUND_DECIMALS),
            "ln_k_eq_dimensionless": round(lnk, ROUND_DECIMALS),
            "log10_k_eq_dimensionless": round(lnk / math.log(10.0), ROUND_DECIMALS),
            "polynomial_interval_index": data["CO2"]["intervals"].index(
                _interval(data["CO2"], t_K)),
        })

    # --- SELF-PROOF (d): Van't Hoff consistency between adjacent grid points.
    max_vant_hoff_residual = 0.0
    for i in range(n_points - 1):  # bounded by MAX_GRID_POINTS
        t1, _dh1, lnk1 = raw[i]
        t2, _dh2, lnk2 = raw[i + 1]
        dh_mid, _ds_mid = reaction_dh_ds(data, 0.5 * (t1 + t2))
        predicted = -(dh_mid / R_J_MOL_K) * (1.0 / t2 - 1.0 / t1)
        residual = abs((lnk2 - lnk1) - predicted)
        max_vant_hoff_residual = max(max_vant_hoff_residual, residual)
        assert residual <= VANT_HOFF_TOL, (
            f"Van't Hoff consistency violated between {t1} K and {t2} K: "
            f"d(lnK)={lnk2 - lnk1:.6f} vs predicted {predicted:.6f}")

    # The sign change of ln K (exothermic reaction turns unfavorable) — the
    # temperature above which K_eq < 1 on this grid, reported honestly.
    first_unfavorable_K = next((t for t, _dh, lnk in raw if lnk < 0.0), None)

    return {
        "task_id": "task-0019-sabatier-equilibrium-constant",
        "inputs": {
            "reaction": "CO2 + 4 H2 -> CH4 + 2 H2O (all gas, standard state)",
            "species": list(SPECIES),
            "stoichiometry": {"unit": "mol per mol of CO2 reacted (products positive)",
                              **{sp: NU[sp] for sp in SPECIES}},
            # emitted at the six-decimal boundary (MIP-0009 C3); the computation
            # uses the exact SI value carried by the pinned module
            "gas_constant_J_mol_K": round(R_J_MOL_K, ROUND_DECIMALS),
            "t_grid_start_K": T_GRID_START_K,
            "t_grid_stop_K": T_GRID_STOP_K,
            "t_grid_step_K": T_GRID_STEP_K,
            "t_reference_K": T_REFERENCE_K,
            "t_join_K": T_JOIN_K,
            "literature_delta_h_298_J_mol": LITERATURE_DH_298_J_MOL,
            "literature_delta_h_tol_J_mol": LITERATURE_DH_TOL_J_MOL,
            "hf_tol_J_mol": HF_TOL_J_MOL,
            "vant_hoff_tol_dimensionless": VANT_HOFF_TOL,
            "data_provenance": dict(_cea.PROVENANCE),
            "pinned_blocks_sha256": _cea.PINNED_BLOCKS_SHA256,
            "round_decimals": ROUND_DECIMALS,
        },
        "results": rows,
        "summary": {
            "delta_h_298_J_mol": round(dh_298, ROUND_DECIMALS),
            "delta_s_298_J_mol_K": round(ds_298, ROUND_DECIMALS),
            "ln_k_eq_298_dimensionless": round(ln_k_eq(data, T_REFERENCE_K), ROUND_DECIMALS),
            "grid_points_count": n_points,
            "max_vant_hoff_residual_dimensionless": round(max_vant_hoff_residual, ROUND_DECIMALS),
            "first_unfavorable_temperature_K": first_unfavorable_K,
            "self_proofs_checked": ["hf_298_in_file", "join_continuity_1000K",
                                    "literature_dh_298", "vant_hoff_adjacent"],
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
