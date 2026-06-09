"""task-0011-ballistic-reentry — deterministic Allen-Eggers ballistic re-entry peaks.

Research-only. A bit-reproducible atmospheric-entry task: it evaluates the classic
Allen-Eggers first-order closed-form approximation for an unlifting (ballistic) entry to
find the peak deceleration and an estimate of the peak stagnation-point convective heat
flux. Everything is analytic — there is NO trajectory ODE integration, no iteration. It maps
to the NASA Technology Taxonomy TX09 (Entry, Descent, and Landing Systems). The computation
is deterministic and reproducible by machine — exactly what MIP-0002 Gate 2 (independent
re-run yields a byte-identical hash) checks.

Allen-Eggers is a FIRST-ORDER ANALYTIC APPROXIMATION (exponential isothermal atmosphere,
constant ballistic coefficient, constant flight-path angle, gravity and lift neglected along
the steep/fast portion). It is NOT a high-fidelity entry trajectory; real entry design uses
numerically integrated 3-DOF/6-DOF trajectories with varying atmosphere, aerodynamics, and
ablation. The figures here are illustrative order-of-magnitude values, NOT a flight design.

Test-META is a zero-value testnet placeholder and never mints base supply (MIP-0001
paragraph 3, MIP-0002 paragraph 8). Not financial, legal, or flight-engineering advice. No
NASA affiliation or endorsement.

Standard library only (math, json, hashlib). Closed-form (no iteration, no randomness), and
every emitted float is rounded to a fixed number of decimals so re-runs are byte-identical
and the SHA-256 output hash is stable (the basis of the Gate-2 check).

Interface is identical to the other tasks so the verifier and agent loop can use them
interchangeably: compute() -> dict, canonical_json(result) -> str, output_hash(result) -> str.
"""

import json
import math

# --- Fixed inputs (part of the reproducibility hash) ------------------------
# Changing any of these changes the canonical output and therefore the Gate-2 hash.
G0_M_S2 = 9.80665                  # standard gravity, m/s^2 (for g-conversion only)
RHO0_KG_M3 = 1.225                # sea-level atmospheric density, kg/m^3
SCALE_HEIGHT_M = 7200.0           # atmospheric scale height H, m
ENTRY_VELOCITY_M_S = 7800.0       # velocity at the atmospheric interface, m/s
ENTRY_ANGLE_DEG = 6.0            # flight-path angle below horizontal (gamma), deg
BALLISTIC_COEFF_KG_M2 = 400.0     # ballistic coefficient beta = m/(Cd*A), kg/m^2
ENTRY_ALTITUDE_M = 120000.0       # atmospheric interface altitude, m (scenario reference)
NOSE_RADIUS_M = 0.5              # effective nose radius for stagnation heating, m
SUTTON_GRAVES_K = 1.7415e-4       # Sutton-Graves stagnation heat-flux constant (SI units)

# Number of decimal places every emitted float is rounded to. Fixed rounding is what makes
# the canonical JSON byte-stable across runs (and thus the SHA-256 reproducible).
ROUND_DECIMALS = 6


def compute() -> dict:
    """Evaluate the Allen-Eggers ballistic-entry peak deceleration and peak-heating estimate.

    All results are standard closed forms (no ODE integration). Derivation sketch:

      Velocity-altitude relation (Allen-Eggers, ballistic):
          V(rho) = V_e * exp( -rho*H / (2*beta*sin(gamma)) )

      Peak deceleration  a = rho*V^2/(2*beta).  Maximizing over altitude gives the density
      rho_amax = beta*sin(gamma)/H, hence (both unambiguous, textbook):
          V_amax = V_e * exp(-1/2)                              (peak-g velocity)
          a_max  = V_e^2 * sin(gamma) / (2 * e * H)             (peak deceleration magnitude)
          h_amax = H * ln( rho0*H / (beta*sin(gamma)) )         (peak-g altitude)

      Peak convective heating q ~ rho^0.5 * V^3 (Sutton-Graves).  Writing u = rho*H/(2*beta*
      sin(gamma)) so q ~ u^0.5 * exp(-3u); d/du = 0 gives u = 1/6, hence (also textbook):
          V_qmax   = V_e * exp(-1/6)                            (peak-heating velocity)
          rho_qmax = beta*sin(gamma)/(3*H) = rho_amax / 3       (peak-heating density)
          q_peak   = K * sqrt(rho_qmax / R_nose) * V_qmax^3     (Sutton-Graves stagnation flux)

    These are the well-established Allen-Eggers peak-g and peak-heating results, not invented
    constants. Gravity/lift are neglected and the atmosphere is exponential-isothermal.
    """
    gamma_rad = math.radians(ENTRY_ANGLE_DEG)
    sin_gamma = math.sin(gamma_rad)

    # --- Peak deceleration (Allen-Eggers, exact closed form) ----------------
    # a_max = V_e^2 * sin(gamma) / (2 * e * H);  peak occurs at V = V_e * exp(-1/2).
    a_max_m_s2 = (ENTRY_VELOCITY_M_S ** 2 * sin_gamma) / (2.0 * math.e * SCALE_HEIGHT_M)
    a_max_g = a_max_m_s2 / G0_M_S2
    v_amax_m_s = ENTRY_VELOCITY_M_S * math.exp(-0.5)
    # Altitude of peak-g: rho_amax = beta*sin(gamma)/H -> h = H*ln(rho0*H/(beta*sin(gamma))).
    h_amax_m = SCALE_HEIGHT_M * math.log(
        (RHO0_KG_M3 * SCALE_HEIGHT_M) / (BALLISTIC_COEFF_KG_M2 * sin_gamma)
    )

    # --- Peak stagnation-point convective heating (Allen-Eggers + Sutton-Graves) ---
    # Peak heating point: u = 1/6 -> V_qmax = V_e*exp(-1/6), rho_qmax = beta*sin(gamma)/(3H).
    v_qmax_m_s = ENTRY_VELOCITY_M_S * math.exp(-1.0 / 6.0)
    rho_qmax_kg_m3 = (BALLISTIC_COEFF_KG_M2 * sin_gamma) / (3.0 * SCALE_HEIGHT_M)
    h_qmax_m = SCALE_HEIGHT_M * math.log(RHO0_KG_M3 / rho_qmax_kg_m3)
    # Sutton-Graves stagnation-point convective heat flux at the peak-heating point.
    q_peak_w_m2 = (
        SUTTON_GRAVES_K * math.sqrt(rho_qmax_kg_m3 / NOSE_RADIUS_M) * v_qmax_m_s ** 3
    )

    results = [
        {
            "event": "peak_deceleration",
            "altitude_m": round(h_amax_m, ROUND_DECIMALS),
            "velocity_m_s": round(v_amax_m_s, ROUND_DECIMALS),
            "deceleration_m_s2": round(a_max_m_s2, ROUND_DECIMALS),
            "deceleration_g": round(a_max_g, ROUND_DECIMALS),
        },
        {
            "event": "peak_heating_estimate",
            "altitude_m": round(h_qmax_m, ROUND_DECIMALS),
            "velocity_m_s": round(v_qmax_m_s, ROUND_DECIMALS),
            "heat_flux_w_m2": round(q_peak_w_m2, ROUND_DECIMALS),
        },
    ]

    return {
        "task_id": "task-0011-ballistic-reentry",
        "inputs": {
            "g0_m_s2": G0_M_S2,
            "rho0_kg_m3": RHO0_KG_M3,
            "scale_height_m": SCALE_HEIGHT_M,
            "entry_velocity_m_s": ENTRY_VELOCITY_M_S,
            "entry_angle_deg": ENTRY_ANGLE_DEG,
            "ballistic_coeff_kg_m2": BALLISTIC_COEFF_KG_M2,
            "entry_altitude_m": ENTRY_ALTITUDE_M,
            "nose_radius_m": NOSE_RADIUS_M,
            "sutton_graves_k": SUTTON_GRAVES_K,
            "round_decimals": ROUND_DECIMALS,
        },
        "results": results,
        "summary": {
            "peak_deceleration_m_s2": round(a_max_m_s2, ROUND_DECIMALS),
            "peak_deceleration_g": round(a_max_g, ROUND_DECIMALS),
            "peak_decel_altitude_m": round(h_amax_m, ROUND_DECIMALS),
            "peak_decel_velocity_m_s": round(v_amax_m_s, ROUND_DECIMALS),
            "peak_heat_flux_w_m2": round(q_peak_w_m2, ROUND_DECIMALS),
            "peak_heat_altitude_m": round(h_qmax_m, ROUND_DECIMALS),
            "peak_heat_velocity_m_s": round(v_qmax_m_s, ROUND_DECIMALS),
        },
    }


def canonical_json(result: dict) -> str:
    """Serialize the result deterministically.

    sort_keys=True, fixed compact separators, and ensure_ascii=True make the output
    byte-stable across runs and platforms (assuming identical rounded float values).
    """
    return json.dumps(
        result,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


def output_hash(result: dict) -> str:
    """Return the SHA-256 hex digest of the canonical JSON (the Gate-2 reproducibility hash)."""
    import hashlib

    return hashlib.sha256(canonical_json(result).encode("utf-8")).hexdigest()


if __name__ == "__main__":
    _result = compute()
    print(canonical_json(_result))
    print("sha256:" + output_hash(_result))
