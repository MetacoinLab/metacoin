"""task-0009-power-budget — deterministic spacecraft power budget & solar-array energy balance.

Research-only. A bit-reproducible spacecraft power-systems task: it sizes solar-array
generation against the spacecraft load over one orbit (a sunlit phase and an eclipse phase)
and computes the battery energy balance, accounting for battery round-trip loss, to decide
whether the orbit is power-positive. It maps to the NASA Technology Taxonomy TX03 (Aerospace
Power and Energy Storage). The computation is deterministic and reproducible by machine —
exactly what MIP-0002 Gate 2 (independent re-run yields a byte-identical hash) checks.

Test-META is a zero-value testnet placeholder and never mints base supply (MIP-0001
paragraph 3, MIP-0002 paragraph 8). Illustrative first-order figures (single fixed sun
incidence, no array degradation/temperature/shadowing, no depth-of-discharge or thermal
limits, no peak-power tracking) — NOT a flight design. Not financial, legal, or
flight-engineering advice. No NASA affiliation or endorsement.

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
SOLAR_CONSTANT_W_M2 = 1361.0          # AM0 solar irradiance at 1 AU, W/m^2
ARRAY_AREA_M2 = 12.0                  # total solar-array area, m^2
CELL_EFFICIENCY = 0.30               # photovoltaic cell efficiency (fraction)
PACKING_FACTOR = 0.90                # active-cell packing factor on the array (fraction)
SUN_INCIDENCE_DEG = 23.5             # angle between array normal and sun line, sunlit phase
ORBIT_PERIOD_MIN = 92.68             # orbital period, minutes
ECLIPSE_FRACTION = 0.38              # fraction of the orbit spent in eclipse
LOAD_SUNLIT_W = 1200.0               # spacecraft load during the sunlit phase, W
LOAD_ECLIPSE_W = 900.0               # spacecraft load during eclipse, W
BATTERY_ROUND_TRIP_EFFICIENCY = 0.90  # battery charge->discharge round-trip efficiency

# Number of decimal places every emitted float is rounded to. Fixed rounding is what makes
# the canonical JSON byte-stable across runs (and thus the SHA-256 reproducible).
ROUND_DECIMALS = 6


def compute() -> dict:
    """Compute the per-phase power/energy budget and the orbit battery balance.

    Units are kept consistent: durations in minutes are converted to hours (min/60) so
    that power (W) * time (h) yields energy in watt-hours (Wh).

      sunlit_min  = T * (1 - eclipse_fraction);  eclipse_min = T * eclipse_fraction
      array_output_w = S * A * eff * packing * cos(incidence)        (sunlit only)
      energy_generated_wh = array_output_w * (sunlit_min/60)
      load_sunlit_wh  = LOAD_SUNLIT  * (sunlit_min/60)
      load_eclipse_wh = LOAD_ECLIPSE * (eclipse_min/60)
      surplus_sunlit_wh        = energy_generated_wh - load_sunlit_wh   (available to recharge)
      battery_discharge_wh     = load_eclipse_wh                        (battery supplies eclipse load)
      battery_charge_needed_wh = battery_discharge_wh / round_trip_eff  (store more than returned)
      energy_margin_wh         = surplus_sunlit_wh - battery_charge_needed_wh  (>0 => power-positive)
      orbit_average_load_w     = (load_sunlit_wh + load_eclipse_wh) / (T/60)
    """
    incidence_rad = math.radians(SUN_INCIDENCE_DEG)

    sunlit_min = ORBIT_PERIOD_MIN * (1.0 - ECLIPSE_FRACTION)
    eclipse_min = ORBIT_PERIOD_MIN * ECLIPSE_FRACTION

    # Solar-array output during the sunlit phase (zero in eclipse).
    array_output_w = (
        SOLAR_CONSTANT_W_M2 * ARRAY_AREA_M2 * CELL_EFFICIENCY
        * PACKING_FACTOR * math.cos(incidence_rad)
    )

    # Energy over each phase (Wh), via minutes -> hours.
    energy_generated_wh = array_output_w * (sunlit_min / 60.0)
    load_sunlit_wh = LOAD_SUNLIT_W * (sunlit_min / 60.0)
    load_eclipse_wh = LOAD_ECLIPSE_W * (eclipse_min / 60.0)

    # Battery energy balance over the orbit.
    surplus_sunlit_wh = energy_generated_wh - load_sunlit_wh
    battery_discharge_wh = load_eclipse_wh
    battery_charge_needed_wh = battery_discharge_wh / BATTERY_ROUND_TRIP_EFFICIENCY
    energy_margin_wh = surplus_sunlit_wh - battery_charge_needed_wh
    orbit_average_load_w = (load_sunlit_wh + load_eclipse_wh) / (ORBIT_PERIOD_MIN / 60.0)
    power_positive = energy_margin_wh > 0.0

    results = [
        {
            "phase": "sunlit",
            "duration_min": round(sunlit_min, ROUND_DECIMALS),
            "array_output_w": round(array_output_w, ROUND_DECIMALS),
            "energy_generated_wh": round(energy_generated_wh, ROUND_DECIMALS),
            "load_w": round(LOAD_SUNLIT_W, ROUND_DECIMALS),
            "load_energy_wh": round(load_sunlit_wh, ROUND_DECIMALS),
        },
        {
            "phase": "eclipse",
            "duration_min": round(eclipse_min, ROUND_DECIMALS),
            "array_output_w": 0.0,
            "energy_generated_wh": 0.0,
            "load_w": round(LOAD_ECLIPSE_W, ROUND_DECIMALS),
            "load_energy_wh": round(load_eclipse_wh, ROUND_DECIMALS),
        },
    ]

    return {
        "task_id": "task-0009-power-budget",
        "inputs": {
            "solar_constant_w_m2": SOLAR_CONSTANT_W_M2,
            "array_area_m2": ARRAY_AREA_M2,
            "cell_efficiency": CELL_EFFICIENCY,
            "packing_factor": PACKING_FACTOR,
            "sun_incidence_deg": SUN_INCIDENCE_DEG,
            "orbit_period_min": ORBIT_PERIOD_MIN,
            "eclipse_fraction": ECLIPSE_FRACTION,
            "load_sunlit_w": LOAD_SUNLIT_W,
            "load_eclipse_w": LOAD_ECLIPSE_W,
            "battery_round_trip_efficiency": BATTERY_ROUND_TRIP_EFFICIENCY,
            "round_decimals": ROUND_DECIMALS,
        },
        "results": results,
        "summary": {
            "array_output_w": round(array_output_w, ROUND_DECIMALS),
            "energy_generated_wh": round(energy_generated_wh, ROUND_DECIMALS),
            "surplus_sunlit_wh": round(surplus_sunlit_wh, ROUND_DECIMALS),
            "battery_discharge_wh": round(battery_discharge_wh, ROUND_DECIMALS),
            "battery_charge_needed_wh": round(battery_charge_needed_wh, ROUND_DECIMALS),
            "energy_margin_wh": round(energy_margin_wh, ROUND_DECIMALS),
            "orbit_average_load_w": round(orbit_average_load_w, ROUND_DECIMALS),
            "power_positive": bool(power_positive),
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
