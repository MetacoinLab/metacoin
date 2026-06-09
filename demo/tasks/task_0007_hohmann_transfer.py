"""task-0007-hohmann-transfer — deterministic Hohmann transfer delta-v budget.

Research-only. A bit-reproducible classic orbital-mechanics task: the two-impulse Hohmann
transfer between two coplanar circular orbits around Earth (here a 400 km LEO parking orbit
up to GEO). It maps to the NASA Technology Taxonomy TX01 (In-Space Propulsion Systems),
to which orbital-transfer delta-v budgeting is directly relevant. The computation is
deterministic and reproducible by machine — exactly what MIP-0002 Gate 2 (independent
re-run yields a byte-identical hash) checks.

Test-META is a zero-value testnet placeholder and never mints base supply (MIP-0001
paragraph 3, MIP-0002 paragraph 8). Illustrative two-body, impulsive-burn figures — NOT a
flight design (no finite-burn losses, no plane change, no perturbations, no launch
windows). Not financial, legal, or flight-engineering advice. No NASA affiliation or
endorsement.

Standard library only (math, json, hashlib). The math is closed-form (no iteration, no
randomness), and every emitted float is rounded to a fixed number of decimals so re-runs
are byte-identical and the SHA-256 output hash is stable (the basis of the Gate-2 check).

Interface is identical to the other tasks so the verifier and agent loop can use them
interchangeably: compute() -> dict, canonical_json(result) -> str, output_hash(result) -> str.
"""

import json
import math

# --- Physical constants -----------------------------------------------------
# Earth's standard gravitational parameter mu = G * M_earth, in km^3 / s^2.
MU_EARTH_KM3_S2 = 398600.4418
# Earth mean equatorial radius, in km (WGS-84).
R_EARTH_KM = 6378.137

# --- Fixed inputs (part of the reproducibility hash) ------------------------
# Departure (parking) circular-orbit altitude and arrival circular-orbit altitude.
# Changing either changes the canonical output and therefore the Gate-2 hash.
H1_KM = 400.0       # departure circular orbit altitude, km (LEO)
H2_KM = 35786.0     # arrival circular orbit altitude, km (GEO)

# Number of decimal places every emitted float is rounded to. Fixed rounding is what makes
# the canonical JSON byte-stable across runs (and thus the SHA-256 reproducible).
ROUND_DECIMALS = 6


def compute() -> dict:
    """Compute the two-impulse Hohmann transfer delta-v budget and return the result.

    Method (all closed-form, two-body, impulsive burns):
      r1 = R_EARTH + H1,  r2 = R_EARTH + H2
      circular speeds:    v1 = sqrt(mu/r1),  v2 = sqrt(mu/r2)
      transfer ellipse:   a_t = (r1 + r2)/2
      vis-viva on it:     vp = sqrt(mu*(2/r1 - 1/a_t))  (perigee, at r1)
                          va = sqrt(mu*(2/r2 - 1/a_t))  (apogee, at r2)
      burns:              dv1 = vp - v1,  dv2 = v2 - va,  dv_total = |dv1| + |dv2|
      transfer time:      t = pi * sqrt(a_t^3 / mu)      (half the ellipse period), seconds
    """
    mu = MU_EARTH_KM3_S2
    r1 = R_EARTH_KM + H1_KM
    r2 = R_EARTH_KM + H2_KM

    # Circular-orbit speeds at the two radii.
    v1 = math.sqrt(mu / r1)
    v2 = math.sqrt(mu / r2)

    # Transfer ellipse and its perigee/apogee speeds (vis-viva).
    a_t = (r1 + r2) / 2.0
    vp = math.sqrt(mu * (2.0 / r1 - 1.0 / a_t))
    va = math.sqrt(mu * (2.0 / r2 - 1.0 / a_t))

    # The two burns and total delta-v.
    dv1 = vp - v1
    dv2 = v2 - va
    dv_total = abs(dv1) + abs(dv2)

    # Transfer time = half the period of the transfer ellipse.
    t_transfer_s = math.pi * math.sqrt(a_t**3 / mu)
    t_transfer_hr = t_transfer_s / 3600.0

    results = [
        {
            "burn": 1,
            "location": "perigee",
            "r_km": round(r1, ROUND_DECIMALS),
            "v_before_km_s": round(v1, ROUND_DECIMALS),
            "v_after_km_s": round(vp, ROUND_DECIMALS),
            "delta_v_km_s": round(dv1, ROUND_DECIMALS),
        },
        {
            "burn": 2,
            "location": "apogee",
            "r_km": round(r2, ROUND_DECIMALS),
            "v_before_km_s": round(va, ROUND_DECIMALS),
            "v_after_km_s": round(v2, ROUND_DECIMALS),
            "delta_v_km_s": round(dv2, ROUND_DECIMALS),
        },
    ]

    return {
        "task_id": "task-0007-hohmann-transfer",
        "inputs": {
            "mu_km3_s2": MU_EARTH_KM3_S2,
            "earth_radius_km": R_EARTH_KM,
            "departure_altitude_km": H1_KM,
            "arrival_altitude_km": H2_KM,
            "round_decimals": ROUND_DECIMALS,
        },
        "results": results,
        "summary": {
            "dv1_km_s": round(dv1, ROUND_DECIMALS),
            "dv2_km_s": round(dv2, ROUND_DECIMALS),
            "dv_total_km_s": round(dv_total, ROUND_DECIMALS),
            "transfer_time_s": round(t_transfer_s, ROUND_DECIMALS),
            "transfer_time_hr": round(t_transfer_hr, ROUND_DECIMALS),
            "a_transfer_km": round(a_t, ROUND_DECIMALS),
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
