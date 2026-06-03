"""task-0002-orbit-propagation — deterministic two-body orbital propagator.

Research-only. This is the first genuinely space-useful reference task for the Phase 1
agentic demo: a bit-reproducible two-body (Keplerian) orbit propagator around Earth.
It maps to the NASA Technology Taxonomy TX17 (Guidance, Navigation, and Control) /
astrodynamics area. The computation is deterministic and reproducible by machine, which
is exactly what MIP-0002 Gate 2 (independent re-run yields a byte-identical hash) checks.

Test-META is a zero-value testnet placeholder and never mints base supply (MIP-0001
paragraph 3, MIP-0002 paragraph 8). Not financial, legal, or flight-engineering advice.
No NASA affiliation or endorsement.

Standard library only (math, json, hashlib, random). The propagator solves Kepler's
equation with a bounded Newton-Raphson iteration (fixed tolerance AND a hard max-iteration
cap, so it is bit-reproducible and never an unbounded loop), converts mean -> eccentric ->
true anomaly, and emits ECI position/velocity at fixed time steps. Every emitted float is
rounded to a fixed number of decimals so re-runs are byte-identical and the SHA-256 output
hash is stable (the basis of the Gate-2 check).

Interface is identical to task-0001 so the verifier and agent loop can use the two tasks
interchangeably: compute() -> dict, canonical_json(result) -> str, output_hash(result) -> str.
"""

import json
import math
import random

# --- Physical constant ------------------------------------------------------
# Earth's standard gravitational parameter mu = G * M_earth, in km^3 / s^2.
# This is the WGS-84 / EGM-derived value commonly used in astrodynamics.
MU_EARTH_KM3_S2 = 398600.4418

# --- Fixed inputs (part of the reproducibility hash) ------------------------
# Classical (Keplerian) orbital elements of an ISS-like, near-circular orbit.
# Changing any of these changes the canonical output and therefore the Gate-2 hash.
SEMI_MAJOR_AXIS_KM = 7000.0     # a — semi-major axis, kilometers
ECCENTRICITY = 0.001            # e — eccentricity (near-circular)
INCLINATION_DEG = 51.6          # i — inclination, degrees (ISS-like)
RAAN_DEG = 0.0                  # Omega — right ascension of ascending node, degrees
ARG_PERIGEE_DEG = 0.0           # omega — argument of perigee, degrees
TRUE_ANOMALY0_DEG = 0.0         # nu0 — true anomaly at epoch, degrees (starts at perigee)

TIME_STEP_S = 60                # fixed sampling step, seconds (one sample per 60 s)
SEED = 42                       # seed for any tie-breaking/ordering; core math is deterministic

# Bounded Newton-Raphson controls for Kepler's equation. The fixed tolerance gives a
# deterministic stopping point; the hard cap guarantees the loop can never run unbounded
# (so the task is both bit-reproducible and guaranteed to terminate).
KEPLER_TOLERANCE = 1e-12        # convergence tolerance on the Newton-Raphson step
KEPLER_MAX_ITER = 100           # hard maximum iteration cap

# Number of decimal places every emitted float is rounded to. Fixed rounding is what
# makes the canonical JSON byte-stable across runs (and thus the SHA-256 reproducible).
ROUND_DECIMALS = 6


def _solve_kepler(mean_anomaly: float, eccentricity: float) -> float:
    """Solve Kepler's equation M = E - e*sin(E) for the eccentric anomaly E.

    Uses Newton-Raphson on  f(E) = E - e*sin(E) - M,  f'(E) = 1 - e*cos(E):

        E_{k+1} = E_k - (E_k - e*sin(E_k) - M) / (1 - e*cos(E_k))

    The iteration stops when the absolute correction drops below KEPLER_TOLERANCE, and is
    hard-capped at KEPLER_MAX_ITER iterations so it can never loop unboundedly. The initial
    guess E0 = M is standard and converges quickly for small eccentricities.
    """
    eccentric_anomaly = mean_anomaly  # E0 = M
    for _ in range(KEPLER_MAX_ITER):
        f = eccentric_anomaly - eccentricity * math.sin(eccentric_anomaly) - mean_anomaly
        f_prime = 1.0 - eccentricity * math.cos(eccentric_anomaly)
        delta = f / f_prime
        eccentric_anomaly -= delta
        if abs(delta) < KEPLER_TOLERANCE:
            break
    return eccentric_anomaly


def compute() -> dict:
    """Propagate the two-body orbit over one period and return the structured result.

    Method (all deterministic):
      1. Mean motion  n = sqrt(mu / a^3);  orbital period  T = 2*pi / n.
      2. At each sample time t (0, 60, 120, ... s, up to one period):
           mean anomaly      M = M0 + n*t        (M0 = 0 here, since nu0 = 0 at perigee)
           eccentric anomaly E = solve Kepler (bounded Newton-Raphson)
           true anomaly      nu = 2*atan2( sqrt(1+e)*sin(E/2), sqrt(1-e)*cos(E/2) )
           radius            r = a*(1 - e*cos(E))
         Perifocal (orbital-plane) state:
           r_pf = [ r*cos(nu), r*sin(nu), 0 ]
           v_pf = (sqrt(mu*a)/r) * [ -sin(E), sqrt(1-e^2)*cos(E), 0 ]
         Rotate perifocal -> ECI with the 3-1-3 matrix Q(Omega, i, omega). With
         Omega = omega = 0 this is a pure rotation about the x-axis by the inclination,
         but the general Q is used so the method stays correct if the elements change.
    """
    # Seed is fixed for reproducibility. The numeric computation does not depend on
    # randomness; the seed only governs any incidental tie-breaking/ordering (parity
    # with task-0001).
    random.seed(SEED)

    a = SEMI_MAJOR_AXIS_KM
    e = ECCENTRICITY
    mu = MU_EARTH_KM3_S2

    inclination = math.radians(INCLINATION_DEG)
    raan = math.radians(RAAN_DEG)
    arg_perigee = math.radians(ARG_PERIGEE_DEG)

    # Mean anomaly at epoch from the initial true anomaly. For nu0 = 0 this is 0, but we
    # compute it generally so the epoch can be moved without breaking the math.
    nu0 = math.radians(TRUE_ANOMALY0_DEG)
    e0 = 2.0 * math.atan2(
        math.sqrt(1.0 - e) * math.sin(nu0 / 2.0),
        math.sqrt(1.0 + e) * math.cos(nu0 / 2.0),
    )
    mean_anomaly0 = e0 - e * math.sin(e0)

    # Mean motion and orbital period.
    mean_motion = math.sqrt(mu / a**3)          # rad/s
    period_s = 2.0 * math.pi / mean_motion      # seconds

    # Perifocal -> ECI rotation matrix Q (3-1-3: Rz(Omega) Rx(i) Rz(omega)). We only need
    # the first two columns because the perifocal z-component is always zero.
    cos_O, sin_O = math.cos(raan), math.sin(raan)
    cos_i, sin_i = math.cos(inclination), math.sin(inclination)
    cos_w, sin_w = math.cos(arg_perigee), math.sin(arg_perigee)

    q11 = cos_O * cos_w - sin_O * sin_w * cos_i
    q12 = -cos_O * sin_w - sin_O * cos_w * cos_i
    q21 = sin_O * cos_w + cos_O * sin_w * cos_i
    q22 = -sin_O * sin_w + cos_O * cos_w * cos_i
    q31 = sin_w * sin_i
    q32 = cos_w * sin_i

    # Sample times: 0, dt, 2*dt, ... up to and including the last step <= one full period.
    num_intervals = int(period_s // TIME_STEP_S)

    steps = []
    max_radius_dev = 0.0
    for k in range(num_intervals + 1):
        t = k * TIME_STEP_S

        mean_anomaly = mean_anomaly0 + mean_motion * t
        ecc_anomaly = _solve_kepler(mean_anomaly, e)

        true_anomaly = 2.0 * math.atan2(
            math.sqrt(1.0 + e) * math.sin(ecc_anomaly / 2.0),
            math.sqrt(1.0 - e) * math.cos(ecc_anomaly / 2.0),
        )
        radius = a * (1.0 - e * math.cos(ecc_anomaly))

        # Perifocal position and velocity.
        x_pf = radius * math.cos(true_anomaly)
        y_pf = radius * math.sin(true_anomaly)
        v_scale = math.sqrt(mu * a) / radius
        vx_pf = v_scale * (-math.sin(ecc_anomaly))
        vy_pf = v_scale * (math.sqrt(1.0 - e * e) * math.cos(ecc_anomaly))

        # Rotate into ECI (perifocal z is zero, so only the first two columns contribute).
        x = q11 * x_pf + q12 * y_pf
        y = q21 * x_pf + q22 * y_pf
        z = q31 * x_pf + q32 * y_pf
        vx = q11 * vx_pf + q12 * vy_pf
        vy = q21 * vx_pf + q22 * vy_pf
        vz = q31 * vx_pf + q32 * vy_pf

        radius_mag = math.sqrt(x * x + y * y + z * z)
        max_radius_dev = max(max_radius_dev, abs(radius_mag - a))

        steps.append(
            {
                "t_s": t,
                "x_km": round(x, ROUND_DECIMALS),
                "y_km": round(y, ROUND_DECIMALS),
                "z_km": round(z, ROUND_DECIMALS),
                "vx_kms": round(vx, ROUND_DECIMALS),
                "vy_kms": round(vy, ROUND_DECIMALS),
                "vz_kms": round(vz, ROUND_DECIMALS),
            }
        )

    return {
        "task_id": "task-0002-orbit-propagation",
        "inputs": {
            "mu_km3_s2": MU_EARTH_KM3_S2,
            "semi_major_axis_km": SEMI_MAJOR_AXIS_KM,
            "eccentricity": ECCENTRICITY,
            "inclination_deg": INCLINATION_DEG,
            "raan_deg": RAAN_DEG,
            "arg_perigee_deg": ARG_PERIGEE_DEG,
            "true_anomaly0_deg": TRUE_ANOMALY0_DEG,
            "time_step_s": TIME_STEP_S,
            "seed": SEED,
            "kepler_tolerance": KEPLER_TOLERANCE,
            "kepler_max_iter": KEPLER_MAX_ITER,
            "round_decimals": ROUND_DECIMALS,
        },
        "results": steps,
        "summary": {
            "orbital_period_s": round(period_s, ROUND_DECIMALS),
            "num_steps": len(steps),
            # Sanity field: for a near-circular orbit the position magnitude should stay
            # close to the semi-major axis. For e = 0.001 the analytic bound is a*e = 7 km.
            "max_position_magnitude_deviation_km": round(max_radius_dev, ROUND_DECIMALS),
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
