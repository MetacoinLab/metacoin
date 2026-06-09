"""task-0013-lambert-transfer — deterministic universal-variable Lambert solver.

Research-only. A bit-reproducible orbital-mechanics task: it solves the classic prograde
Lambert problem — given two position vectors and a time of flight, find the connecting
Keplerian transfer orbit and its terminal velocity vectors — using the universal-variable /
Stumpff-function formulation (Curtis, "Orbital Mechanics for Engineering Students",
Algorithm 5.2). It maps to the NASA Technology Taxonomy TX17 (Guidance, Navigation, and
Control). The computation is deterministic and reproducible by machine — exactly what
MIP-0002 Gate 2 (independent re-run yields a byte-identical hash) checks.

CROSS-PLATFORM DETERMINISM NOTE: this is the only demo task with an iterative solver. To
keep it byte-identical across platforms it uses a FIXED-iteration BISECTION root-finder on
the universal variable z — a hard, constant loop count (LAMBERT_ITERATIONS), NOT an
"iterate until a tolerance" loop. A fixed loop count removes any platform-dependent
step-count divergence, and bisection has no derivative blow-ups, so the bracketing sequence
is identical everywhere. A residual |F(z)| is computed only for transparency; it never
controls the loop length. Bisection on a fixed, safe bracket is the most numerically stable
choice across platforms.

Test-META is a zero-value testnet placeholder and never mints base supply (MIP-0001
paragraph 3, MIP-0002 paragraph 8). Illustrative two-body Keplerian transfer (no J2, drag,
third-body, or finite-burn modeling) — NOT a flight design. Not financial, legal, or
flight-engineering advice. No NASA affiliation or endorsement.

Standard library only (math, json, hashlib). No randomness. Every emitted float is rounded
to a fixed number of decimals so re-runs are byte-identical and the SHA-256 output hash is
stable (the basis of the Gate-2 check).

Interface is identical to the other tasks so the verifier and agent loop can use them
interchangeably: compute() -> dict, canonical_json(result) -> str, output_hash(result) -> str.
"""

import json
import math

# --- Fixed inputs (part of the reproducibility hash) ------------------------
# Changing any of these changes the canonical output and therefore the Gate-2 hash.
# Canonical Curtis Example 5.2 reference case (known-good answer for sanity-checking).
MU_EARTH_KM3_S2 = 398600.4418              # Earth gravitational parameter, km^3/s^2
R1_VEC_KM = [5000.0, 10000.0, 2100.0]      # initial position vector, km
R2_VEC_KM = [-14600.0, 2500.0, 7000.0]     # final position vector, km
TOF_S = 3600.0                            # time of flight, s (1 hour)
PROGRADE = True                           # prograde transfer branch
LAMBERT_ITERATIONS = 60                    # FIXED bisection iteration count (no early exit)

# Bisection bracket on the universal variable z. Chosen so that ACROSS THE WHOLE bracket:
#   * y(z) > 0  -- z stays above the y=0 lower limit (~ -4.3 for this geometry), and
#   * C(z) > 0  -- z stays below the first zero of the Stumpff C at (2*pi)^2 ~ 39.48,
# which guarantees F(z) is finite and real everywhere in the bracket. The prograde root
# (z ~ 1.53) lies strictly inside with a clean sign change F(low) < 0 < F(high). This is
# deliberately NOT [-100, 100]: that range would cross y < 0 and C = 0 and yield NaNs.
Z_BRACKET_LOW = -3.0
Z_BRACKET_HIGH = 20.0

# Number of decimal places every emitted float is rounded to. Fixed rounding is what makes
# the canonical JSON byte-stable across runs (and thus the SHA-256 reproducible).
ROUND_DECIMALS = 6


def _stumpff_c(z: float) -> float:
    """Stumpff function C(z) (Curtis eq. 3.50)."""
    if z > 0:
        s = math.sqrt(z)
        return (1.0 - math.cos(s)) / z
    if z < 0:
        s = math.sqrt(-z)
        return (math.cosh(s) - 1.0) / (-z)
    return 0.5


def _stumpff_s(z: float) -> float:
    """Stumpff function S(z) (Curtis eq. 3.49)."""
    if z > 0:
        s = math.sqrt(z)
        return (s - math.sin(s)) / (s ** 3)
    if z < 0:
        s = math.sqrt(-z)
        return (math.sinh(s) - s) / (s ** 3)
    return 1.0 / 6.0


def compute() -> dict:
    """Solve the prograde Lambert problem by fixed-iteration bisection on z.

    Universal-variable formulation (Curtis Algorithm 5.2):
      A    = sin(dtheta) * sqrt(r1*r2 / (1 - cos(dtheta)))
      y(z) = r1 + r2 + A*(z*S(z) - 1) / sqrt(C(z))
      F(z) = (y(z)/C(z))^1.5 * S(z) + A*sqrt(y(z)) - sqrt(mu)*TOF
    F(z) = 0 is solved by BISECTION with a FIXED loop count (no tolerance early-exit), then
    the Lagrange coefficients f, g, gdot recover the velocity vectors:
      f = 1 - y/r1;  g = A*sqrt(y/mu);  gdot = 1 - y/r2
      v1 = (R2 - f*R1)/g;  v2 = (gdot*R2 - R1)/g
    """
    mu = MU_EARTH_KM3_S2
    r1_vec = R1_VEC_KM
    r2_vec = R2_VEC_KM

    r1 = math.sqrt(sum(c * c for c in r1_vec))
    r2 = math.sqrt(sum(c * c for c in r2_vec))

    # Change in true anomaly. Prograde branch selected via the z-component of R1 x R2.
    dot_r1r2 = sum(r1_vec[i] * r2_vec[i] for i in range(3))
    cross_z = r1_vec[0] * r2_vec[1] - r1_vec[1] * r2_vec[0]
    cos_dtheta = max(-1.0, min(1.0, dot_r1r2 / (r1 * r2)))  # clamp for acos safety
    if PROGRADE:
        d_theta = math.acos(cos_dtheta) if cross_z >= 0 else 2.0 * math.pi - math.acos(cos_dtheta)
    else:
        d_theta = 2.0 * math.pi - math.acos(cos_dtheta) if cross_z >= 0 else math.acos(cos_dtheta)

    a_coeff = math.sin(d_theta) * math.sqrt(r1 * r2 / (1.0 - math.cos(d_theta)))

    def y_of(z: float) -> float:
        c = _stumpff_c(z)
        s = _stumpff_s(z)
        return r1 + r2 + a_coeff * (z * s - 1.0) / math.sqrt(c)

    def f_of(z: float) -> float:
        c = _stumpff_c(z)
        s = _stumpff_s(z)
        y = r1 + r2 + a_coeff * (z * s - 1.0) / math.sqrt(c)
        return (y / c) ** 1.5 * s + a_coeff * math.sqrt(y) - math.sqrt(mu) * TOF_S

    # --- Fixed-iteration bisection (the deterministic core) -----------------
    # Maintains a sign change with F(z_low) <= 0 <= F(z_high). The loop ALWAYS runs exactly
    # LAMBERT_ITERATIONS times — there is no tolerance-based early exit.
    z_low = Z_BRACKET_LOW
    z_high = Z_BRACKET_HIGH
    f_low = f_of(z_low)
    for _ in range(LAMBERT_ITERATIONS):
        z_mid = 0.5 * (z_low + z_high)
        f_mid = f_of(z_mid)
        same_sign = (f_low <= 0.0 and f_mid <= 0.0) or (f_low > 0.0 and f_mid > 0.0)
        if same_sign:
            z_low = z_mid
            f_low = f_mid
        else:
            z_high = z_mid
    z = 0.5 * (z_low + z_high)

    # Recover the transfer geometry and velocity vectors at the solution z.
    y = y_of(z)
    final_residual = abs(f_of(z))  # transparency only; did NOT control the loop length

    f_lag = 1.0 - y / r1
    g_lag = a_coeff * math.sqrt(y / mu)
    gdot_lag = 1.0 - y / r2

    v1_vec = [(r2_vec[i] - f_lag * r1_vec[i]) / g_lag for i in range(3)]
    v2_vec = [(gdot_lag * r2_vec[i] - r1_vec[i]) / g_lag for i in range(3)]
    v1_mag = math.sqrt(sum(c * c for c in v1_vec))
    v2_mag = math.sqrt(sum(c * c for c in v2_vec))

    # Semi-major axis via vis-viva at r1 (derivable from the recovered speed).
    semi_major_axis_km = 1.0 / (2.0 / r1 - v1_mag * v1_mag / mu)

    results = [
        {"quantity": "v1_vec_km_s", "value": [round(c, ROUND_DECIMALS) for c in v1_vec]},
        {"quantity": "v2_vec_km_s", "value": [round(c, ROUND_DECIMALS) for c in v2_vec]},
    ]

    return {
        "task_id": "task-0013-lambert-transfer",
        "inputs": {
            "mu_earth_km3_s2": MU_EARTH_KM3_S2,
            "r1_vec_km": list(R1_VEC_KM),
            "r2_vec_km": list(R2_VEC_KM),
            "tof_s": TOF_S,
            "prograde": PROGRADE,
            "lambert_iterations": LAMBERT_ITERATIONS,
            "z_bracket_low": Z_BRACKET_LOW,
            "z_bracket_high": Z_BRACKET_HIGH,
            "round_decimals": ROUND_DECIMALS,
        },
        "results": results,
        "summary": {
            "v1_magnitude_km_s": round(v1_mag, ROUND_DECIMALS),
            "v2_magnitude_km_s": round(v2_mag, ROUND_DECIMALS),
            "z_solution": round(z, ROUND_DECIMALS),
            "semi_major_axis_km": round(semi_major_axis_km, ROUND_DECIMALS),
            "final_residual": round(final_residual, ROUND_DECIMALS),
            "iterations": LAMBERT_ITERATIONS,
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
