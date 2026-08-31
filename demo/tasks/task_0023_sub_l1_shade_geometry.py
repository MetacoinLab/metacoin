"""task-0023-sub-l1-shade-geometry — deterministic sunshade equilibrium and
occulting-area computation at the radiation-pressure-shifted sub-L1 point
(parented on task-0022; the mission-0002 geometry node).

Research-only. A bit-reproducible celestial-mechanics task in three steps:
(1) THE EQUILIBRIUM. In the Sun-Earth rotating frame, on the Sun-Earth line,
a shade of areal density sigma feels solar gravity inward and Earth gravity,
centrifugal force, and radiation pressure outward. The classical L1 balance
    GM_s/r^2 = GM_e/(R-r)^2 + omega^2 r          (omega^2 = G(M_s+M_e)/R^3)
gains the radiation term S0 (R/r)^2 / (c sigma) for an absorbing film, and
the equilibrium moves SUNWARD of L1 (sub-L1) — solved here by bounded
bisection (the task-0020 pattern), never quoted.
(2) THE OCCULTING FRACTION. Seen from Earth, a small centered shade of area
A at distance d covers solid angle A/d^2 of the Sun's disc solid angle
pi (R_sun/AU)^2; with uniform disc brightness the blocked-flux fraction is
their ratio, so the required area is
    A = f * pi (R_sun/AU)^2 * d^2
for the fraction f CONSUMED from task-0022's published reference row.
(3) THE FULL-DISC CHECK. From any point on Earth the shade's angular offset
is at most R_earth/d; the task PROVES the shade disc stays entirely on the
solar disc from every Earth point (theta_sun > theta_shade + offset), which
is what justifies penumbra efficiency 1.0 — computed, not assumed.
It maps to the NASA Technology Taxonomy TX17 (Guidance, Navigation &
Control — libration-point equilibrium geometry).

THE PROVENANCE EDGE, ENFORCED AT EXECUTION TIME: compute() CALLS
task_0022.compute() directly, recomputes the parent's canonical output hash
LIVE, and asserts equality with the pinned EXPECTED_PARENT_OUTPUT_HASH.
Every physical constant is pinned with provenance in
demo/tasks/pinned_sunshade_sources.py (IAU B3 fetched-hashed values).

INTERNAL SELF-PROOF (four assertion classes inside compute() per MIP-0008
rule 1): compute() asserts
  (a) PARENT-HASH LIVENESS (the provenance edge);
  (b) CONVERGENCE — the bisection closes to within POSITION_TOL_M in at
      most MAX_BISECTION_ITER steps and the force residual at the solution
      is below RESIDUAL_TOL_M_S2 (the loop bound is stated, rule 2);
  (c) KNOWN-TRUTH — with radiation pressure OFF the same solver reproduces
      classical L1 within 1%% of the Hill-sphere approximation
      R (mu/3)^(1/3), and the radiation-shifted equilibrium lies strictly
      SUNWARD of classical L1;
  (d) FULL-DISC BOUND — the on-disc angular margin is strictly positive
      (theta_sun - theta_shade - R_earth/d > 0), so the stated occulting
      efficiency of 1.0 is proved for this geometry.
A violated assertion CRASHES the task — stop, don't fudge.

Test-META is a zero-value testnet placeholder and never mints base supply
(MIP-0001 paragraph 3, MIP-0002 paragraph 8). Collinear equilibrium
statics with an absorbing flat film (no halo-orbit dynamics, station-keeping,
limb darkening, off-axis tilt, or wavelength dependence) — NOT a flight
design. Not financial, legal, or flight-engineering advice.
No NASA affiliation or endorsement.

Standard library only (json, math, hashlib) plus the parent task module and
the pinned-constants module. No randomness. Every emitted float is rounded
to a fixed number of decimals so re-runs are byte-identical and the SHA-256
output hash is stable (the basis of the Gate-2 check). MIP-0009 contract:
compute() -> the four-key dict, canonical_json() era-2 (sign-of-zero-free),
output_hash() = sha256 of it.
"""

import hashlib
import json
import math

# THE PARENT TASK and the pinned sources. Package import when loaded as
# demo.tasks.task_0023_...; bare import when run as a script.
try:
    from demo.tasks import task_0022_insolation_offset_requirement as _req_parent
    from demo.tasks import pinned_sunshade_sources as _src
except ImportError:  # direct script run: demo/tasks/ itself is sys.path[0]
    import task_0022_insolation_offset_requirement as _req_parent
    import pinned_sunshade_sources as _src

# Declared parentage (consumed by protocol/work_molecule.py: the molecule
# builder resolves each entry to the parent's WMID within the same build).
PARENT_TASKS = ["task-0022"]

# The parent's canonical Gate-2 output hash, PINNED. compute() recomputes it
# live and asserts equality — the executable provenance edge.
EXPECTED_PARENT_OUTPUT_HASH = (
    "9c128de655d643cdd9e368ad0bda56c3b55d929511756dd9871e7814c65ae9b5"
)

# --- Fixed inputs (part of the reproducibility hash) ------------------------
# Changing any of these changes the canonical output and therefore the Gate-2
# hash. THE CONSTANTS ARE NOT TO BE TUNED TO MANUFACTURE ANY STORY.
GM_SUN_M3_S2 = _src.GM_SUN_M3_S2
GM_EARTH_M3_S2 = _src.GM_EARTH_M3_S2
AU_M = _src.AU_M
S0_W_M2 = _src.SOLAR_IRRADIANCE_W_M2
R_SUN_M = _src.SOLAR_RADIUS_M
R_EARTH_M = _src.EARTH_EQ_RADIUS_M
C_M_S = _src.SPEED_OF_LIGHT_M_S
SHADE_AREAL_DENSITY_KG_PER_M2 = 0.030   # stated film class: ~10 um Al film
                                        # (2700 kg/m3) + structure allowance
                                        # — an engineering-representative
                                        # stated constant, not a pinned fact
MAX_BISECTION_ITER = 200                # the stated loop bound (rule 2)
POSITION_TOL_M = 1e-6                   # bracket width at convergence
RESIDUAL_TOL_M_S2 = 1e-9                # net force residual at the solution
HILL_APPROX_TOL_FRACTION = 0.01         # classical-L1 cross-check tolerance
ROUND_DECIMALS = 6


def _net_sunward_accel(r_m: float, sigma_kg_m2: float) -> float:
    """Net sunward acceleration on the Sun-Earth line at distance r from the
    Sun: solar gravity inward minus Earth gravity, centrifugal, and (for
    sigma > 0) radiation pressure outward. Zero at equilibrium."""
    d_m = AU_M - r_m
    omega2 = (GM_SUN_M3_S2 + GM_EARTH_M3_S2) / AU_M ** 3
    a_rp = ((S0_W_M2 * (AU_M / r_m) ** 2) / (C_M_S * sigma_kg_m2)
            if sigma_kg_m2 > 0.0 else 0.0)
    return (GM_SUN_M3_S2 / r_m ** 2 - GM_EARTH_M3_S2 / d_m ** 2
            - omega2 * r_m - a_rp)


def solve_equilibrium(sigma_kg_m2: float):
    """Bounded bisection for the collinear equilibrium between 0.9 AU and
    (AU - 10^7 m). The net sunward acceleration is positive sunward of the
    root and negative earthward of it, so the bracket holds one root.
    Returns (r_m, iterations, residual_m_s2)."""
    lo, hi = 0.9 * AU_M, AU_M - 1e7
    iterations = 0
    for _ in range(MAX_BISECTION_ITER):  # bounded by MAX_BISECTION_ITER
        mid = 0.5 * (lo + hi)
        if _net_sunward_accel(mid, sigma_kg_m2) > 0.0:
            lo = mid
        else:
            hi = mid
        iterations += 1
        if hi - lo <= POSITION_TOL_M:
            break
    r_m = 0.5 * (lo + hi)
    return r_m, iterations, abs(_net_sunward_accel(r_m, sigma_kg_m2))


def compute() -> dict:
    """Sub-L1 equilibrium, occulting area, full-disc proof + self-proofs."""
    # --- SELF-PROOF (a): parent-hash liveness (the provenance edge) --------
    parent_result = _req_parent.compute()
    parent_hash = _req_parent.output_hash(parent_result)
    assert parent_hash == EXPECTED_PARENT_OUTPUT_HASH, (
        f"parent-hash liveness violated: task-0022 recomputes to {parent_hash}, "
        f"expected {EXPECTED_PARENT_OUTPUT_HASH} — this task refuses drifted input")
    # The required blocked fraction the mission chain assumes — consumed from
    # the parent's PUBLISHED summary, never restated here.
    f_required = float(parent_result["summary"]["reference_required_fraction"])

    # Classical L1 (radiation pressure off) — the known-truth control.
    r_l1_m, it_l1, res_l1 = solve_equilibrium(0.0)
    d_l1_m = AU_M - r_l1_m
    mu = GM_EARTH_M3_S2 / (GM_SUN_M3_S2 + GM_EARTH_M3_S2)
    d_hill_m = AU_M * (mu / 3.0) ** (1.0 / 3.0)
    # --- SELF-PROOF (c): Hill approximation agreement + sunward shift ------
    assert abs(d_l1_m - d_hill_m) / d_hill_m <= HILL_APPROX_TOL_FRACTION, (
        f"known-truth violated: solver L1 distance {d_l1_m} m vs Hill "
        f"approximation {d_hill_m} m disagree beyond {HILL_APPROX_TOL_FRACTION}")

    # The radiation-shifted sub-L1 equilibrium for the stated areal density.
    r_eq_m, it_eq, res_eq = solve_equilibrium(SHADE_AREAL_DENSITY_KG_PER_M2)
    d_eq_m = AU_M - r_eq_m
    assert d_eq_m > d_l1_m, (
        f"known-truth violated: radiation pressure must shift the equilibrium "
        f"sunward (d_eq {d_eq_m} m <= d_L1 {d_l1_m} m)")
    # --- SELF-PROOF (b): convergence within the stated bounds --------------
    for label, iters, res in (("L1", it_l1, res_l1), ("sub-L1", it_eq, res_eq)):
        assert iters <= MAX_BISECTION_ITER and res <= RESIDUAL_TOL_M_S2, (
            f"convergence violated at {label}: residual {res} m/s2 after "
            f"{iters} iterations (bounds {MAX_BISECTION_ITER}, {RESIDUAL_TOL_M_S2})")

    # The occulting area for the required fraction at the equilibrium.
    omega_sun_sr = math.pi * (R_SUN_M / AU_M) ** 2
    area_m2 = f_required * omega_sun_sr * d_eq_m ** 2
    area_km2 = area_m2 / 1e6

    # --- SELF-PROOF (d): the shade stays fully on the solar disc from every
    # point on Earth — the computed justification for efficiency 1.0.
    theta_sun_rad = R_SUN_M / AU_M
    theta_shade_rad = math.sqrt(area_m2 / math.pi) / d_eq_m
    max_offset_rad = R_EARTH_M / d_eq_m
    disc_margin_rad = theta_sun_rad - theta_shade_rad - max_offset_rad
    assert disc_margin_rad > 0.0, (
        f"full-disc bound violated: margin {disc_margin_rad} rad <= 0 — the "
        "penumbra-efficiency-1.0 statement would be false at this geometry")

    return {
        "task_id": "task-0023-sub-l1-shade-geometry",
        "inputs": {
            "parent_task_id": "task-0022",
            "parent_output_hash": parent_hash,
            "parent_required_fraction": f_required,
            "gm_sun_m3_s2": GM_SUN_M3_S2,
            "gm_earth_m3_s2": GM_EARTH_M3_S2,
            "au_m": AU_M,
            "s0_W_m2": S0_W_M2,
            "r_sun_m": R_SUN_M,
            "r_earth_m": R_EARTH_M,
            "constants_provenance": _src.IAU_B3_PROVENANCE["source"],
            "shade_areal_density_kg_per_m2": SHADE_AREAL_DENSITY_KG_PER_M2,
            "areal_density_basis": "~10 um aluminum film (2700 kg/m3) plus "
                                   "structure allowance — a stated "
                                   "engineering class, not a pinned fact",
            "sail_optics": "absorbing flat film (radiation-pressure "
                           "acceleration = flux/(c*sigma))",
            "max_bisection_iter_count": MAX_BISECTION_ITER,
            "round_decimals": ROUND_DECIMALS,
        },
        "results": [
            {"step": "classical_l1",
             "distance_from_earth_km": round(d_l1_m / 1e3, ROUND_DECIMALS),
             "hill_approximation_km": round(d_hill_m / 1e3, ROUND_DECIMALS),
             "bisection_iterations_count": it_l1},
            {"step": "sub_l1_equilibrium",
             "distance_from_earth_km": round(d_eq_m / 1e3, ROUND_DECIMALS),
             "sunward_shift_km": round((d_eq_m - d_l1_m) / 1e3,
                                       ROUND_DECIMALS),
             "bisection_iterations_count": it_eq},
            {"step": "occulting_geometry",
             # the solid angle is emitted in micro-steradians so the
             # six-decimal boundary keeps its significant figures
             "solar_disc_solid_angle_urad2_dimensionless": round(
                 omega_sun_sr * 1e6, ROUND_DECIMALS),
             "required_area_km2": round(area_km2, ROUND_DECIMALS),
             "on_disc_margin_mrad": round(disc_margin_rad * 1e3,
                                          ROUND_DECIMALS)},
        ],
        "summary": {
            "required_area_km2": round(area_km2, ROUND_DECIMALS),
            "shade_distance_from_earth_km": round(d_eq_m / 1e3,
                                                  ROUND_DECIMALS),
            "occulting_efficiency_fraction": 1.0,
            "efficiency_note": "efficiency 1.0 is PROVED for this geometry "
                               "(the shade stays fully on the solar disc "
                               "from every Earth point), not assumed",
            "self_proofs_checked": ["parent_hash_liveness",
                                    "bisection_convergence_x2",
                                    "hill_crosscheck_and_sunward_shift",
                                    "full_disc_margin"],
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
