"""task-0010-thermal-equilibrium — deterministic passive radiative equilibrium temperature.

Research-only. A bit-reproducible spacecraft thermal-control task: it solves the closed-form
passive radiative equilibrium temperature of a spacecraft surface via the Stefan-Boltzmann
law. At steady state the absorbed power (direct solar + Earth-reflected albedo + Earth
infrared + internal electronics dissipation) equals the radiated power, which fixes the
equilibrium temperature T_eq in a single closed-form expression (no iteration). It maps to
the NASA Technology Taxonomy TX14 (Thermal Management Systems). The computation is
deterministic and reproducible by machine — exactly what MIP-0002 Gate 2 (independent re-run
yields a byte-identical hash) checks.

Test-META is a zero-value testnet placeholder and never mints base supply (MIP-0001
paragraph 3, MIP-0002 paragraph 8). Illustrative first-order figures (single lumped
isothermal surface, fixed view factors folded into constant fluxes, no conduction, no
transient/eclipse cycling, no multi-node network, gray-body assumption) — NOT a flight
design. Not financial, legal, or flight-engineering advice. No NASA affiliation or endorsement.

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
STEFAN_BOLTZMANN_W_M2_K4 = 5.670374419e-8  # Stefan-Boltzmann constant, W/(m^2 K^4)
SOLAR_FLUX_W_M2 = 1361.0          # direct solar flux (AM0 at 1 AU), W/m^2
ALBEDO_FLUX_W_M2 = 408.0          # Earth-reflected solar flux (~0.3 albedo), W/m^2
EARTH_IR_FLUX_W_M2 = 237.0        # Earth infrared (outgoing longwave) flux, W/m^2
ABSORPTIVITY_ALPHA = 0.40         # solar absorptance of the surface (fraction)
EMISSIVITY_EPS = 0.85             # infrared emittance of the surface (fraction)
ABSORBING_AREA_M2 = 4.0           # area facing the incident heat fluxes, m^2
RADIATING_AREA_M2 = 12.0          # total radiating area, m^2
INTERNAL_DISSIPATION_W = 150.0    # onboard electronics heat dissipation, W

# Number of decimal places every emitted float is rounded to. Fixed rounding is what makes
# the canonical JSON byte-stable across runs (and thus the SHA-256 reproducible).
ROUND_DECIMALS = 6


def compute() -> dict:
    """Solve the closed-form passive radiative equilibrium temperature.

    At steady state, absorbed power = radiated power. Absorbed contributions:

      absorbed_solar_w    = alpha * solar_flux  * absorbing_area   (solar absorbed via alpha)
      absorbed_albedo_w   = alpha * albedo_flux * absorbing_area   (reflected solar via alpha)
      absorbed_earth_ir_w = eps   * earth_ir    * absorbing_area   (IR absorbed via emissivity)
      total_absorbed_w    = solar + albedo + earth_ir + internal_dissipation

    Equilibrium temperature (Stefan-Boltzmann, single closed-form root, no iteration):

      T_eq = ( total_absorbed_w / (eps * sigma * radiating_area) ) ** 0.25      [K]

    Self-check (the correctness proof): recompute the radiated power at T_eq and confirm the
    net flux is ~0 — this verifies T_eq actually balances the absorbed load.

      radiated_power_w = eps * sigma * radiating_area * T_eq**4
      net_flux_w       = total_absorbed_w - radiated_power_w   (must be ~0)
    """
    absorbed_solar_w = ABSORPTIVITY_ALPHA * SOLAR_FLUX_W_M2 * ABSORBING_AREA_M2
    absorbed_albedo_w = ABSORPTIVITY_ALPHA * ALBEDO_FLUX_W_M2 * ABSORBING_AREA_M2
    absorbed_earth_ir_w = EMISSIVITY_EPS * EARTH_IR_FLUX_W_M2 * ABSORBING_AREA_M2

    total_absorbed_w = (
        absorbed_solar_w + absorbed_albedo_w + absorbed_earth_ir_w + INTERNAL_DISSIPATION_W
    )

    # Closed-form equilibrium temperature (fourth root of the radiative balance).
    radiative_coeff = EMISSIVITY_EPS * STEFAN_BOLTZMANN_W_M2_K4 * RADIATING_AREA_M2
    t_eq_k = (total_absorbed_w / radiative_coeff) ** 0.25
    t_eq_c = t_eq_k - 273.15

    # Self-check: radiated power at T_eq should match the absorbed load (net flux ~0).
    radiated_power_w = radiative_coeff * t_eq_k ** 4
    net_flux_w = total_absorbed_w - radiated_power_w

    results = [
        {"source": "solar", "absorbed_w": round(absorbed_solar_w, ROUND_DECIMALS)},
        {"source": "albedo", "absorbed_w": round(absorbed_albedo_w, ROUND_DECIMALS)},
        {"source": "earth_ir", "absorbed_w": round(absorbed_earth_ir_w, ROUND_DECIMALS)},
        {"source": "internal", "absorbed_w": round(INTERNAL_DISSIPATION_W, ROUND_DECIMALS)},
    ]

    return {
        "task_id": "task-0010-thermal-equilibrium",
        "inputs": {
            "stefan_boltzmann_w_m2_k4": STEFAN_BOLTZMANN_W_M2_K4,
            "solar_flux_w_m2": SOLAR_FLUX_W_M2,
            "albedo_flux_w_m2": ALBEDO_FLUX_W_M2,
            "earth_ir_flux_w_m2": EARTH_IR_FLUX_W_M2,
            "absorptivity_alpha": ABSORPTIVITY_ALPHA,
            "emissivity_eps": EMISSIVITY_EPS,
            "absorbing_area_m2": ABSORBING_AREA_M2,
            "radiating_area_m2": RADIATING_AREA_M2,
            "internal_dissipation_w": INTERNAL_DISSIPATION_W,
            "round_decimals": ROUND_DECIMALS,
        },
        "results": results,
        "summary": {
            "total_absorbed_w": round(total_absorbed_w, ROUND_DECIMALS),
            "t_eq_k": round(t_eq_k, ROUND_DECIMALS),
            "t_eq_c": round(t_eq_c, ROUND_DECIMALS),
            "radiated_power_w": round(radiated_power_w, ROUND_DECIMALS),
            "net_flux_w": round(net_flux_w, ROUND_DECIMALS),
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
