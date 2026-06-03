"""task-0003-power-eclipse — deterministic orbital eclipse + solar power/energy budget.

Research demo only. A lab-useful, fully deterministic spacecraft power task: it propagates
a circular Earth orbit, classifies each step as sunlight or umbra using a cylindrical
Earth-shadow model, and integrates a simple battery state-of-charge energy budget over one
orbit. It maps to the NASA Technology Taxonomy TX03 (Aerospace Power and Energy Storage).
The computation is deterministic and reproducible by machine, which is exactly what
MIP-0002 Gate 2 (independent re-run yields a byte-identical hash) checks.

HONEST SIMPLIFICATIONS (stated plainly, not hidden):
  * The Sun direction is a FIXED unit vector in ECI (+X), a documented stand-in for a real
    solar ephemeris. There is no ephemeris and no seasonal beta-angle variation; this is a
    single representative geometry (effectively beta = 0, Sun in the orbital plane's
    reference frame), chosen for determinism. Real mission analysis must use an ephemeris.
  * Earth's shadow is modeled as a CYLINDER (umbra only, no penumbra, no atmospheric
    refraction, no oblateness). This slightly overstates the umbra versus a true conical
    shadow but is the standard first-order textbook model.
  * The orbit is an ideal circle (eccentricity 0); the energy integration is forward Euler
    at a fixed time step. These are first-order approximations, adequate for a reproducible
    demo, not for flight power sizing.

Test-META is a zero-value testnet placeholder and never mints base supply (MIP-0001
paragraph 3, MIP-0002 paragraph 8). Not financial, legal, or flight-engineering advice.
No NASA affiliation or endorsement.

Standard library only (math, json, hashlib). Every emitted float is rounded to a fixed
number of decimals so re-runs are byte-identical and the SHA-256 output hash is stable.

Interface is identical to task-0001 and task-0002 so the verifier and agent loop can use
all tasks interchangeably: compute() -> dict, canonical_json(result) -> str,
output_hash(result) -> str.
"""

import json
import math

# --- Physical constants -----------------------------------------------------
# Earth's standard gravitational parameter mu = G * M_earth, in km^3 / s^2.
MU_EARTH_KM3_S2 = 398600.4418
# Earth mean equatorial radius, in km (WGS-84). Used as the shadow-cylinder radius.
R_EARTH_KM = 6378.137

# --- Fixed orbit inputs (part of the reproducibility hash) ------------------
# Ideal circular orbit (eccentricity 0). Same parameter style as task-0002.
SEMI_MAJOR_AXIS_KM = 7000.0     # a — circular orbit radius, kilometers
INCLINATION_DEG = 51.6          # i — inclination, degrees (ISS-like)

# Fixed Sun direction in ECI: a documented unit vector along +X. This is a deterministic
# stand-in for a real solar ephemeris (see HONEST SIMPLIFICATIONS in the module docstring).
SUN_DIR_ECI = (1.0, 0.0, 0.0)

TIME_STEP_S = 60                # fixed sampling / integration step, seconds

# --- Fixed power & energy inputs (part of the reproducibility hash) ---------
P_SOLAR_W = 300.0               # solar array output while in sunlight, watts
P_LOAD_W = 100.0                # spacecraft load, watts, drawn at all times
BATTERY_CAPACITY_WH = 120.0     # battery capacity (upper clamp), watt-hours
BATTERY_INITIAL_WH = 120.0      # initial state of charge, watt-hours (starts full)
BATTERY_FLOOR_WH = 20.0         # survival floor: SOC must never fall below this, watt-hours

# Number of decimal places every emitted float is rounded to. Fixed rounding is what makes
# the canonical JSON byte-stable across runs (and thus the SHA-256 reproducible).
ROUND_DECIMALS = 6


def _eci_position(a: float, inclination_rad: float, theta_rad: float):
    """ECI position (km) of a circular orbit at in-plane angle theta.

    Perifocal (orbital-plane) position is [a*cos(theta), a*sin(theta), 0]. With RAAN = 0
    and argument of latitude measured from the +X axis, the perifocal->ECI transform is a
    pure rotation about the x-axis by the inclination, so:
        x = a*cos(theta)
        y = a*sin(theta)*cos(i)
        z = a*sin(theta)*sin(i)
    """
    x_pf = a * math.cos(theta_rad)
    y_pf = a * math.sin(theta_rad)
    x = x_pf
    y = y_pf * math.cos(inclination_rad)
    z = y_pf * math.sin(inclination_rad)
    return x, y, z


def _in_sunlight(position, sun_dir, earth_radius: float) -> bool:
    """Cylindrical-shadow sunlight test. Returns True if the satellite is lit.

    The satellite is in Earth's umbra IF AND ONLY IF both hold:
      1. It is on the anti-sunward side of Earth:  r . s_hat < 0.
      2. Its perpendicular distance from the Earth-Sun line is inside Earth's shadow
         cylinder:  sqrt(|r|^2 - (r . s_hat)^2) < R_earth.
    Otherwise it is in sunlight. (s_hat is the unit Sun direction; here SUN_DIR_ECI.)
    """
    x, y, z = position
    sx, sy, sz = sun_dir
    r_dot_s = x * sx + y * sy + z * sz
    r_mag_sq = x * x + y * y + z * z
    # Perpendicular distance from the Earth-Sun line (clamp tiny negative round-off to 0).
    perp = math.sqrt(max(0.0, r_mag_sq - r_dot_s * r_dot_s))
    in_umbra = (r_dot_s < 0.0) and (perp < earth_radius)
    return not in_umbra


def _clamp(value: float, low: float, high: float) -> float:
    """Clamp value to the inclusive range [low, high]."""
    return max(low, min(high, value))


def compute() -> dict:
    """Propagate one orbit, classify sunlight/umbra, and integrate the battery SOC.

    Method (all deterministic):
      1. Mean motion n = sqrt(mu / a^3); period T = 2*pi / n. Sample at fixed TIME_STEP_S
         from t = 0 up to and including the last step <= one full period.
      2. At each step compute the ECI position, test sunlight via the cylindrical-shadow
         model, record (t, in_sunlight, battery_wh), then advance the battery by forward
         Euler: dE_Wh = power_W * dt_s / 3600, where power = (P_solar - P_load) in sunlight
         and (-P_load) in umbra. SOC is clamped to [0, capacity] each step.
    """
    a = SEMI_MAJOR_AXIS_KM
    mu = MU_EARTH_KM3_S2
    inclination = math.radians(INCLINATION_DEG)

    mean_motion = math.sqrt(mu / a**3)          # rad/s
    period_s = 2.0 * math.pi / mean_motion      # seconds
    num_intervals = int(period_s // TIME_STEP_S)

    battery_wh = BATTERY_INITIAL_WH
    min_battery_raw = battery_wh
    eclipsed_count = 0

    steps = []
    for k in range(num_intervals + 1):
        t = k * TIME_STEP_S
        theta = mean_motion * t                  # circular: true anomaly = mean anomaly = n*t
        position = _eci_position(a, inclination, theta)
        lit = _in_sunlight(position, SUN_DIR_ECI, R_EARTH_KM)
        if not lit:
            eclipsed_count += 1

        min_battery_raw = min(min_battery_raw, battery_wh)

        steps.append(
            {
                "t_s": t,
                "in_sunlight": bool(lit),
                "battery_wh": round(battery_wh, ROUND_DECIMALS),
            }
        )

        # Forward-Euler battery update for the next step.
        power_w = (P_SOLAR_W - P_LOAD_W) if lit else (-P_LOAD_W)
        battery_wh = _clamp(
            battery_wh + power_w * TIME_STEP_S / 3600.0, 0.0, BATTERY_CAPACITY_WH
        )

    total_steps = len(steps)
    eclipse_fraction = eclipsed_count / total_steps
    eclipse_duration_s = eclipsed_count * TIME_STEP_S
    survives_orbit = min_battery_raw >= BATTERY_FLOOR_WH

    return {
        "task_id": "task-0003-power-eclipse",
        "inputs": {
            "mu_km3_s2": MU_EARTH_KM3_S2,
            "earth_radius_km": R_EARTH_KM,
            "semi_major_axis_km": SEMI_MAJOR_AXIS_KM,
            "inclination_deg": INCLINATION_DEG,
            "sun_dir_eci": list(SUN_DIR_ECI),
            "time_step_s": TIME_STEP_S,
            "p_solar_w": P_SOLAR_W,
            "p_load_w": P_LOAD_W,
            "battery_capacity_wh": BATTERY_CAPACITY_WH,
            "battery_initial_wh": BATTERY_INITIAL_WH,
            "battery_floor_wh": BATTERY_FLOOR_WH,
            "round_decimals": ROUND_DECIMALS,
        },
        "results": steps,
        "summary": {
            "orbital_period_s": round(period_s, ROUND_DECIMALS),
            "num_steps": total_steps,
            "eclipse_fraction": round(eclipse_fraction, ROUND_DECIMALS),
            "eclipse_duration_s": eclipse_duration_s,
            "min_battery_wh": round(min_battery_raw, ROUND_DECIMALS),
            "survives_orbit": bool(survives_orbit),
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
