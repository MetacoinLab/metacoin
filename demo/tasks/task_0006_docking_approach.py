"""task-0006-docking-approach — deterministic rendezvous & docking approach-corridor check.

Research demo only. A lab-useful, fully deterministic in-space servicing task: it propagates
the relative motion of a chaser spacecraft about a target in a circular reference orbit
using the closed-form Clohessy-Wiltshire (CW / Hill's) state-transition solution, then checks
the chaser against a docking approach corridor (an approach cone plus a safe closing-speed
limit) at each step.

NASA Technology Taxonomy node: this maps most directly to TX17 (Guidance, Navigation, and
Control) — specifically Rendezvous, Proximity Operations, and Docking/Capture — which is the
GN&C capability underpinning In-Space Servicing, Assembly, and Manufacturing (ISAM). (The
task brief suggested "TX13"; TX13 is actually Ground, Test, and Surface Systems, so the
TX17 RPOD node is the accurate home for relative-motion docking dynamics.)

The computation is deterministic and reproducible by machine — it uses the CLOSED-FORM CW
state-transition equations (analytic), NOT numerical integration, so there is no time-step
truncation or step-size dependence to threaten the byte-identical hash MIP-0002 Gate 2 checks.

MODEL — Clohessy-Wiltshire / Hill's equations (LVLH frame at the target):
  x = radial (R-bar, +x outward), y = along-track (V-bar, +y along velocity), z = cross-track.
  Reference mean motion n = sqrt(mu / a^3). Closed-form propagation of [x,y,z,vx,vy,vz]:
    x(t)  = (4-3cos nt) x0 + (sin nt / n) vx0 + (2/n)(1-cos nt) vy0
    y(t)  = 6(sin nt - nt) x0 + y0 + (2/n)(cos nt - 1) vx0 + (1/n)(4 sin nt - 3 nt) vy0
    z(t)  = cos nt z0 + (sin nt / n) vz0
    vx(t) = 3n sin nt x0 + cos nt vx0 + 2 sin nt vy0
    vy(t) = 6n(cos nt - 1) x0 - 2 sin nt vx0 + (4 cos nt - 3) vy0
    vz(t) = -n sin nt z0 + cos nt vz0
  The radial/along-track coupling (e.g. a pure along-track push induces radial motion) is the
  characteristic CW signature; it is what curves the relative trajectory.

HONEST SIMPLIFICATIONS (stated plainly, not hidden):
  * CW is a LINEARIZED approximation valid only for close proximity (separation << orbit
    radius) and a CIRCULAR reference orbit. It is the standard docking-regime model, not full
    nonlinear two-body / J2 / drag dynamics.
  * The chaser is a point mass: no attitude, no shape, no plume or contact dynamics.
  * This is open-loop coasting relative motion: NO sensor noise, NO navigation filter, and NO
    control loop / thrusters. The corridor and speed limit are evaluated, not enforced.
  * The approach corridor is idealized geometry (an exact cone about one axis, a single speed
    cap); real corridors add range-dependent speed gates, keep-out zones, and attitude limits.

Test-META is a zero-value testnet placeholder and never mints base supply (MIP-0001
paragraph 3, MIP-0002 paragraph 8). Not financial, legal, or flight-engineering advice.
No NASA affiliation or endorsement.

Standard library only (math, json, hashlib). Every emitted float is rounded to a fixed
number of decimals so re-runs are byte-identical and the SHA-256 output hash is stable.

Interface is identical to the other tasks so the verifier and agent loop can use them
interchangeably: compute() -> dict, canonical_json(result) -> str, output_hash(result) -> str.
"""

import json
import math

# --- Physical constants / reference orbit -----------------------------------
# Earth's standard gravitational parameter mu = G * M_earth, in km^3 / s^2.
MU_EARTH_KM3_S2 = 398600.4418
# Circular reference-orbit radius, kilometers (same family as the other tasks).
REF_ORBIT_RADIUS_KM = 7000.0
# Mean motion n = sqrt(mu / a^3), in rad/s. Computed once from the reference orbit.
MEAN_MOTION_RAD_S = math.sqrt(MU_EARTH_KM3_S2 / REF_ORBIT_RADIUS_KM**3)

# --- Fixed initial relative state (part of the reproducibility hash) --------
# Chaser starts on the +y (V-bar) approach axis, a few hundred meters out, with a small
# along-track closing velocity toward the target/docking port at the origin. Meters, m/s.
INIT_REL_POS_M = (0.0, 300.0, 0.0)    # (x radial, y along-track, z cross-track)
INIT_REL_VEL_M_S = (0.0, -0.50, 0.0)  # small PURE along-track closing velocity (-y = toward target)

# --- Fixed docking corridor + limits (part of the reproducibility hash) -----
# Docking port is on the +y (V-bar) axis; the approach corridor is a cone of this half-angle
# about +y with apex at the port (origin). The chaser is "in corridor" when it is in front of
# the port (y > 0) and within the cone half-angle of the +y axis.
DOCKING_AXIS = "+y"                   # along-track / V-bar approach
CONE_HALF_ANGLE_DEG = 10.0            # approach-cone half-angle, degrees
MAX_CLOSING_SPEED_M_S = 0.3           # maximum safe closing speed, m/s
CAPTURE_RANGE_M = 50.0                # "close range" for a nominal approach, meters

TIME_STEP_S = 10                      # fixed sampling step, seconds
SPAN_S = 1500                         # propagation span, seconds

# Number of decimal places every emitted float is rounded to. Fixed rounding is what makes
# the canonical JSON byte-stable across runs (and thus the SHA-256 reproducible).
ROUND_DECIMALS = 6


def _cw_state(n: float, s0: tuple, t: float) -> tuple:
    """Closed-form Clohessy-Wiltshire state [x,y,z,vx,vy,vz] at time t from initial state s0."""
    x0, y0, z0, vx0, vy0, vz0 = s0
    nt = n * t
    s, c = math.sin(nt), math.cos(nt)

    x = (4.0 - 3.0 * c) * x0 + (s / n) * vx0 + (2.0 / n) * (1.0 - c) * vy0
    y = (
        6.0 * (s - nt) * x0
        + y0
        + (2.0 / n) * (c - 1.0) * vx0
        + (1.0 / n) * (4.0 * s - 3.0 * nt) * vy0
    )
    z = c * z0 + (s / n) * vz0

    vx = 3.0 * n * s * x0 + c * vx0 + 2.0 * s * vy0
    vy = 6.0 * n * (c - 1.0) * x0 - 2.0 * s * vx0 + (4.0 * c - 3.0) * vy0
    vz = -n * s * z0 + c * vz0
    return (x, y, z, vx, vy, vz)


def compute() -> dict:
    """Propagate the CW relative motion and evaluate the docking corridor at each step."""
    n = MEAN_MOTION_RAD_S
    s0 = INIT_REL_POS_M + INIT_REL_VEL_M_S
    cos_half_angle = math.cos(math.radians(CONE_HALF_ANGLE_DEG))

    num_steps = int(SPAN_S // TIME_STEP_S)

    trajectory = []
    min_range = None
    t_closest = 0
    in_corridor_count = 0
    max_closing_speed = None
    closest_idx = 0

    for k in range(num_steps + 1):
        t = k * TIME_STEP_S
        x, y, z, vx, vy, vz = _cw_state(n, s0, t)

        rng = math.sqrt(x * x + y * y + z * z)
        # Closing speed = rate of range decrease = -(r . v)/|r|. Positive => approaching.
        closing_speed = -(x * vx + y * vy + z * vz) / rng if rng > 0.0 else 0.0

        # In corridor: in front of the port (y>0) and within the cone half-angle of +y.
        in_corridor = (y > 0.0) and (rng > 0.0) and (y / rng >= cos_half_angle)
        speed_safe = closing_speed <= MAX_CLOSING_SPEED_M_S

        if in_corridor:
            in_corridor_count += 1
        if min_range is None or rng < min_range:
            min_range = rng
            t_closest = t
            closest_idx = k
        if max_closing_speed is None or closing_speed > max_closing_speed:
            max_closing_speed = closing_speed

        trajectory.append(
            {
                "t_s": t,
                "x_m": round(x, ROUND_DECIMALS),
                "y_m": round(y, ROUND_DECIMALS),
                "z_m": round(z, ROUND_DECIMALS),
                "range_m": round(rng, ROUND_DECIMALS),
                "in_corridor": bool(in_corridor),
                "closing_speed_ms": round(closing_speed, ROUND_DECIMALS),
                "speed_safe": bool(speed_safe),
            }
        )

    total_steps = len(trajectory)
    closest = trajectory[closest_idx]
    # Nominal approach: reaches close range AND is inside the corridor and within the speed
    # limit at the moment of closest approach.
    approach_nominal = bool(
        (min_range <= CAPTURE_RANGE_M)
        and closest["in_corridor"]
        and closest["speed_safe"]
    )

    return {
        "task_id": "task-0006-docking-approach",
        "inputs": {
            "mu_km3_s2": MU_EARTH_KM3_S2,
            "ref_orbit_radius_km": REF_ORBIT_RADIUS_KM,
            "mean_motion_rad_s": round(MEAN_MOTION_RAD_S, ROUND_DECIMALS),
            "init_rel_pos_m": list(INIT_REL_POS_M),
            "init_rel_vel_m_s": list(INIT_REL_VEL_M_S),
            "docking_axis": DOCKING_AXIS,
            "cone_half_angle_deg": CONE_HALF_ANGLE_DEG,
            "max_closing_speed_m_s": MAX_CLOSING_SPEED_M_S,
            "capture_range_m": CAPTURE_RANGE_M,
            "time_step_s": TIME_STEP_S,
            "span_s": SPAN_S,
            "round_decimals": ROUND_DECIMALS,
        },
        "results": trajectory,
        "summary": {
            "min_range_m": round(min_range, ROUND_DECIMALS),
            "time_of_closest_approach_s": t_closest,
            "fraction_of_time_in_corridor": round(in_corridor_count / total_steps, ROUND_DECIMALS),
            "max_closing_speed_ms": round(max_closing_speed, ROUND_DECIMALS),
            "approach_nominal": approach_nominal,
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
