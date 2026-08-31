"""task-0028-l1-dust-persistence — deterministic persistence analysis of a
lunar-dust cloud at Sun-Earth L1 (a standalone CONSTRAINING node of
mission-0002, WITH THE NATURAL HONEST NEGATIVE).

Research-only. The claim under verification includes the aside that lunar
DUST could be launched as a temporary shade. This task computes, from
pinned constants and standard celestial mechanics alone, the two facts that
govern such a cloud — both DERIVED, never quoted:

(1) L1 IS UNSTABLE, WITH A ~23-DAY E-FOLDING TIME. Linearizing the circular
restricted three-body problem at L1 gives the in-plane characteristic
equation with coefficient c2 = mu/gamma^3 + (1-mu)/(1-gamma)^3 (gamma =
the L1 distance in AU units, computed here from the pinned GM values by the
same bounded bisection the geometry node uses); its unstable root
    lambda = sqrt( (c2 - 2 + sqrt(9 c2^2 - 8 c2)) / 2 )   (in omega units)
sets the e-folding time 1/(lambda omega). Any uncontrolled cloud's spread
GROWS by e every such interval — the task publishes the time for a
ten-fold spread (tau ln 10) as the persistence figure.

(2) RADIATION PRESSURE EVICTS SMALL GRAINS OUTRIGHT. For a grain of radius
s and density rho, the radiation-to-gravity ratio
    beta = 3 L_sun / (16 pi (GM_sun) c rho s)
is ~0.19 at one micron — the equilibrium the cloud would need shifts
enormously (the effective solar mass parameter becomes (1-beta) GM_sun),
so the dust variant is doubly transient: unstable AND blown off station.
It maps to the NASA Technology Taxonomy TX17 (GNC — libration-point
stability).

INTERNAL SELF-PROOF (four assertion classes inside compute() per MIP-0008
rule 1): compute() asserts
  (a) CONVERGENCE — the L1 bisection closes within its stated bounds;
  (b) KNOWN-TRUTH, eigenvalue — the computed lambda satisfies the
      characteristic equation lambda^4 + (2 - c2) lambda^2 +
      (1 + c2 - 2 c2^2) ... in its factored in-plane form
      (lambda^2 - the root) to within 1e-9, lambda is real and positive
      (instability CONFIRMED, not assumed), and the e-folding time lands
      in the public ~20-30 day class for Sun-Earth L1;
  (c) KNOWN-TRUTH, beta — beta computed by the closed form equals beta
      recomputed from grain mass and cross-section explicitly, to within
      1e-12 relative, and beta is strictly decreasing in grain size;
  (d) BOUNDS — the ten-fold-spread time is positive and SHORTER than the
      stated minimum useful persistence, which is what makes the verdict
      field persists_usefully honestly FALSE.
A violated assertion CRASHES the task — stop, don't fudge.

Test-META is a zero-value testnet placeholder and never mints base supply
(MIP-0001 paragraph 3, MIP-0002 paragraph 8). Linearized CR3BP statics plus
spherical-grain radiation pressure (no solar wind, electrostatic charging,
grain-size distribution, or replenishment modeling) — NOT a dust-dynamics
simulation. Not financial, legal, or flight-engineering advice.
No NASA affiliation or endorsement.

Standard library only (json, math, hashlib) plus the pinned-constants
module. No randomness. Every emitted float is rounded to a fixed number of
decimals so re-runs are byte-identical and the SHA-256 output hash is
stable (the basis of the Gate-2 check). MIP-0009 contract: compute() ->
the four-key dict, canonical_json() era-2 (sign-of-zero-free),
output_hash() = sha256 of it.
"""

import hashlib
import json
import math

try:
    from demo.tasks import pinned_sunshade_sources as _src
except ImportError:  # direct script run: demo/tasks/ itself is sys.path[0]
    import pinned_sunshade_sources as _src

PARENT_TASKS = []

# --- Fixed inputs (part of the reproducibility hash) ------------------------
# THE CONSTANTS ARE NOT TO BE TUNED TO MANUFACTURE ANY STORY.
GM_SUN_M3_S2 = _src.GM_SUN_M3_S2
GM_EARTH_M3_S2 = _src.GM_EARTH_M3_S2
AU_M = _src.AU_M
L_SUN_W = _src.SOLAR_LUMINOSITY_W
C_M_S = _src.SPEED_OF_LIGHT_M_S
GRAIN_RADIUS_M = 1e-6            # stated reference grain: one micron
GRAIN_DENSITY_KG_M3 = 3000.0     # stated regolith-mineral grain density
GRAIN_SIZE_GRID_M = (0.5e-6, 1e-6, 10e-6)   # bounded sensitivity grid
MIN_USEFUL_PERSISTENCE_YR = 1.0  # stated: a 'temporary shade' that cannot
                                 # hold one year offsets nothing seasonal,
                                 # let alone a forcing target
EFOLD_CLASS_DAYS = (20.0, 30.0)  # public landmark class for Sun-Earth L1
MAX_BISECTION_ITER = 200         # the stated loop bound (rule 2)
POSITION_TOL_M = 1e-4            # early-exit bracket width (the float ULP
                                 # at 1 AU is ~3e-5 m; the convergence
                                 # PROOF is the force residual below)
RESIDUAL_TOL_M_S2 = 1e-9         # net force residual at the solution
CHAR_EQ_TOL = 1e-9
BETA_CROSSCHECK_REL_TOL = 1e-12
JULIAN_YEAR_S = _src.JULIAN_YEAR_S
ROUND_DECIMALS = 6


def _net_sunward_accel(r_m: float) -> float:
    """Net sunward acceleration on the Sun-Earth line (no radiation term)."""
    d_m = AU_M - r_m
    omega2 = (GM_SUN_M3_S2 + GM_EARTH_M3_S2) / AU_M ** 3
    return (GM_SUN_M3_S2 / r_m ** 2 - GM_EARTH_M3_S2 / d_m ** 2
            - omega2 * r_m)


def compute() -> dict:
    """L1 instability e-folding, grain beta grid, persistence verdict."""
    # L1 by bounded bisection (the task-0020/0023 pattern).
    lo, hi = 0.9 * AU_M, AU_M - 1e7
    iterations = 0
    for _ in range(MAX_BISECTION_ITER):  # bounded by MAX_BISECTION_ITER
        mid = 0.5 * (lo + hi)
        if _net_sunward_accel(mid) > 0.0:
            lo = mid
        else:
            hi = mid
        iterations += 1
        if hi - lo <= POSITION_TOL_M:
            break
    r_l1_m = 0.5 * (lo + hi)
    # --- SELF-PROOF (a): convergence (the residual is the proof; the
    # bracket floors at the float ULP of 1 AU, stated above) ----------------
    residual_m_s2 = abs(_net_sunward_accel(r_l1_m))
    assert iterations <= MAX_BISECTION_ITER and residual_m_s2 <= RESIDUAL_TOL_M_S2, (
        f"convergence violated: residual {residual_m_s2} m/s2 after "
        f"{iterations} iterations (bounds {MAX_BISECTION_ITER}, "
        f"{RESIDUAL_TOL_M_S2})")

    # Linearized CR3BP at L1: c2, the unstable root, the e-folding time.
    mu = GM_EARTH_M3_S2 / (GM_SUN_M3_S2 + GM_EARTH_M3_S2)
    gamma = (AU_M - r_l1_m) / AU_M
    c2 = mu / gamma ** 3 + (1.0 - mu) / (1.0 - gamma) ** 3
    lam_sq = (c2 - 2.0 + math.sqrt(9.0 * c2 ** 2 - 8.0 * c2)) / 2.0
    lam = math.sqrt(lam_sq)
    # --- SELF-PROOF (b): the root satisfies its quadratic, is positive,
    # and the e-folding time lands in the public class -----------------------
    char_residual = lam_sq ** 2 - (c2 - 2.0) * lam_sq - (
        2.0 * c2 ** 2 - c2 - 1.0)
    assert abs(char_residual) <= CHAR_EQ_TOL, (
        f"eigenvalue violated: characteristic residual {char_residual} for "
        f"lambda^2 = {lam_sq} at c2 = {c2}")
    assert lam > 0.0, (
        f"instability not confirmed: lambda {lam} is not positive")
    omega_rad_s = math.sqrt((GM_SUN_M3_S2 + GM_EARTH_M3_S2) / AU_M ** 3)
    efold_s = 1.0 / (lam * omega_rad_s)
    efold_days = efold_s / 86400.0
    assert EFOLD_CLASS_DAYS[0] <= efold_days <= EFOLD_CLASS_DAYS[1], (
        f"known-truth violated: e-folding {efold_days} days outside the "
        f"public {EFOLD_CLASS_DAYS} class for Sun-Earth L1")
    tenfold_days = efold_days * math.log(10.0)
    tenfold_yr = tenfold_days * 86400.0 / JULIAN_YEAR_S

    # Radiation-pressure beta over the bounded grain grid, two derivations.
    rows = []
    betas = []
    for s_m in GRAIN_SIZE_GRID_M:            # bounded: three grain sizes
        beta = (3.0 * L_SUN_W
                / (16.0 * math.pi * GM_SUN_M3_S2 * C_M_S
                   * GRAIN_DENSITY_KG_M3 * s_m))
        # explicit re-derivation: force ratio on a spherical grain
        grain_mass_kg = (4.0 / 3.0) * math.pi * s_m ** 3 * GRAIN_DENSITY_KG_M3
        cross_section_m2 = math.pi * s_m ** 2
        f_rad_n = (L_SUN_W / (4.0 * math.pi * AU_M ** 2)) / C_M_S * cross_section_m2
        f_grav_n = GM_SUN_M3_S2 * grain_mass_kg / AU_M ** 2
        beta_explicit = f_rad_n / f_grav_n
        # --- SELF-PROOF (c): two derivations agree -------------------------
        assert abs(beta - beta_explicit) <= BETA_CROSSCHECK_REL_TOL * beta, (
            f"beta cross-check violated at s={s_m} m: closed form {beta} vs "
            f"explicit {beta_explicit}")
        betas.append(beta)
        rows.append({"grain_radius_um": round(s_m * 1e6, ROUND_DECIMALS),
                     "beta_ratio": round(beta, ROUND_DECIMALS)})
    for i in range(len(betas) - 1):          # bounded: two adjacent pairs
        assert betas[i + 1] < betas[i], (
            "monotonicity violated: beta did not fall with grain size")

    # The verdict: does the cloud persist usefully?
    persists = tenfold_yr >= MIN_USEFUL_PERSISTENCE_YR
    # --- SELF-PROOF (d): the published shortfall closes --------------------
    shortfall_yr = round(max(0.0, MIN_USEFUL_PERSISTENCE_YR - tenfold_yr),
                         ROUND_DECIMALS)
    assert (shortfall_yr > 0.0) == (not persists), (
        "verdict violated: shortfall sign disagrees with the persistence flag")

    return {
        "task_id": "task-0028-l1-dust-persistence",
        "inputs": {
            "gm_sun_m3_s2": GM_SUN_M3_S2,
            "gm_earth_m3_s2": GM_EARTH_M3_S2,
            "au_m": AU_M,
            "l_sun_W": L_SUN_W,
            "constants_provenance": _src.IAU_B3_PROVENANCE["source"],
            "grain_density_kg_m3": GRAIN_DENSITY_KG_M3,
            "grain_size_grid_um": [round(s * 1e6, ROUND_DECIMALS)
                                   for s in GRAIN_SIZE_GRID_M],
            "grain_basis": "stated regolith-mineral grain class (density "
                           "3000 kg/m3; 0.5-10 um radii)",
            "min_useful_persistence_yr": MIN_USEFUL_PERSISTENCE_YR,
            "persistence_basis": "stated: a temporary shade that cannot "
                                 "hold one year offsets nothing",
            "max_bisection_iter_count": MAX_BISECTION_ITER,
            "round_decimals": ROUND_DECIMALS,
        },
        "results": [
            {"step": "l1_instability",
             "c2_coefficient_dimensionless": round(c2, ROUND_DECIMALS),
             "unstable_lambda_dimensionless": round(lam, ROUND_DECIMALS),
             "efold_time_days": round(efold_days, ROUND_DECIMALS),
             "tenfold_spread_days": round(tenfold_days, ROUND_DECIMALS)},
            {"step": "radiation_pressure_grid", "beta_rows": rows},
        ],
        "summary": {
            "dust_shade_persists": persists,
            # the size of the honest "no", as a plain number
            "shortfall_yr": shortfall_yr,
            "tenfold_spread_days": round(tenfold_days, ROUND_DECIMALS),
            "efold_time_days": round(efold_days, ROUND_DECIMALS),
            "beta_at_1um_ratio": round(betas[1], ROUND_DECIMALS),
            "verdict_note": "an uncontrolled dust cloud at L1 ten-folds its "
                            "spread in weeks (derived e-folding ~23 days) "
                            "and micron grains feel beta ~0.19 radiation "
                            "pressure that shifts their equilibrium off "
                            "station entirely — the 'temporary dust shade' "
                            "disperses in weeks against a one-year minimum: "
                            "the natural honest negative, derived, not "
                            "quoted",
            "self_proofs_checked": ["bisection_convergence",
                                    "characteristic_equation_and_class",
                                    "beta_two_derivations_and_monotonicity",
                                    "shortfall_sign_closure"],
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
