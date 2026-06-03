"""task-0004-comms-access — deterministic ground-station communication access windows.

Research demo only. A lab-useful, fully deterministic comms task: it propagates a circular
Earth orbit, rotates a fixed ground station with the Earth, computes the satellite's
topocentric elevation as seen from that station at each step, and extracts the discrete
access windows (passes) where the satellite is above a minimum elevation mask. It maps to
the NASA Technology Taxonomy TX05 (Communications, Navigation). The computation is
deterministic and reproducible by machine, which is exactly what MIP-0002 Gate 2
(independent re-run yields a byte-identical hash) checks.

HONEST SIMPLIFICATIONS (stated plainly, not hidden):
  * The initial sidereal angle (GMST at t=0) is FIXED at 0. This is a deterministic
    stand-in for a real epoch/sidereal-time lookup: it fixes the inertial orientation of
    the ground station at t=0. Real planning must use the actual sidereal time at the
    chosen epoch (the choice of GMST0 only shifts which passes fall in the window).
  * The ground station is placed on a SPHERICAL Earth of radius R_EARTH at a fixed
    geodetic-as-geocentric latitude/longitude (no WGS-84 ellipsoid, no site altitude,
    no terrain). Local vertical is taken as the geocentric radial, which is exact for a
    sphere. It sits at the orbit's inclination latitude, under the orbit's high-latitude
    turning region, so it sees regular passes. The orbit is an ideal circle (e = 0).
  * Elevation uses straight-line geometry: no atmospheric refraction, no light-time delay.

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

# --- Physical constants -----------------------------------------------------
# Earth's standard gravitational parameter mu = G * M_earth, in km^3 / s^2.
MU_EARTH_KM3_S2 = 398600.4418
# Earth mean equatorial radius, in km (WGS-84), used as the spherical-Earth radius.
R_EARTH_KM = 6378.137
# Earth inertial rotation rate, in rad/s (sidereal).
OMEGA_EARTH_RAD_S = 7.2921159e-5

# --- Fixed orbit inputs (part of the reproducibility hash) ------------------
# Ideal circular orbit (eccentricity 0). Same parameter style as task-0002/0003.
SEMI_MAJOR_AXIS_KM = 7000.0     # a — circular orbit radius, kilometers
INCLINATION_DEG = 51.6          # i — inclination, degrees (ISS-like)

# --- Fixed ground station (part of the reproducibility hash) ----------------
# At the orbit's inclination latitude on the prime meridian, on a spherical Earth: this
# sits under the orbit's high-latitude turning region and sees regular passes. A
# deterministic, documented choice.
GROUND_STATION_LAT_DEG = 51.6   # geocentric latitude, degrees (matches orbit inclination)
GROUND_STATION_LON_DEG = 0.0    # longitude, degrees (east positive)
GMST0_RAD = 0.0                 # sidereal angle at t=0 (fixed simplification)

# --- Fixed access / sampling inputs (part of the reproducibility hash) ------
ELEVATION_MASK_DEG = 10.0       # minimum elevation for line-of-sight access, degrees
TIME_STEP_S = 10                # fixed sampling step, seconds
SPAN_S = 43200                  # propagation span, seconds (12 hours, several orbits)

# Number of decimal places every emitted float is rounded to. Fixed rounding is what makes
# the canonical JSON byte-stable across runs (and thus the SHA-256 reproducible).
ROUND_DECIMALS = 6


def _sat_eci(a: float, inclination_rad: float, mean_motion: float, t: float):
    """ECI position (km) of the circular orbit at time t.

    Perifocal position [a*cos(theta), a*sin(theta), 0] with theta = n*t, rotated to ECI by
    the inclination about the x-axis (RAAN = 0, argument of latitude from +X):
        x = a*cos(theta)
        y = a*sin(theta)*cos(i)
        z = a*sin(theta)*sin(i)
    """
    theta = mean_motion * t
    x_pf = a * math.cos(theta)
    y_pf = a * math.sin(theta)
    return (x_pf, y_pf * math.cos(inclination_rad), y_pf * math.sin(inclination_rad))


def _gs_eci(lat_rad: float, lon_rad: float, t: float):
    """ECI position (km) of the ground station at time t, on a spherical Earth.

    The station is fixed in ECEF; its ECI position is the ECEF position rotated about the
    z-axis by the Earth-rotation angle theta_g = GMST0 + omega_earth * t.
    """
    r = R_EARTH_KM
    x_ecef = r * math.cos(lat_rad) * math.cos(lon_rad)
    y_ecef = r * math.cos(lat_rad) * math.sin(lon_rad)
    z_ecef = r * math.sin(lat_rad)

    theta_g = GMST0_RAD + OMEGA_EARTH_RAD_S * t
    cos_g, sin_g = math.cos(theta_g), math.sin(theta_g)
    x_eci = x_ecef * cos_g - y_ecef * sin_g
    y_eci = x_ecef * sin_g + y_ecef * cos_g
    return (x_eci, y_eci, z_ecef)


def _elevation_deg(sat, gs) -> float:
    """Topocentric elevation (degrees) of `sat` as seen from ground station `gs` (both ECI).

    Local vertical at the station is the geocentric radial (exact for a sphere). With the
    range vector rho = sat - gs and the up unit vector up = gs / |gs|:
        sin(elevation) = (rho . up) / |rho|
    """
    rx, ry, rz = sat[0] - gs[0], sat[1] - gs[1], sat[2] - gs[2]
    rho_norm = math.sqrt(rx * rx + ry * ry + rz * rz)
    gs_norm = math.sqrt(gs[0] ** 2 + gs[1] ** 2 + gs[2] ** 2)
    up = (gs[0] / gs_norm, gs[1] / gs_norm, gs[2] / gs_norm)
    sin_elev = (rx * up[0] + ry * up[1] + rz * up[2]) / rho_norm
    # Clamp tiny round-off outside [-1, 1] before asin.
    sin_elev = max(-1.0, min(1.0, sin_elev))
    return math.degrees(math.asin(sin_elev))


def compute() -> dict:
    """Propagate the orbit, compute elevation each step, and extract access windows.

    For each step t = 0, dt, ... up to and including SPAN_S, compute the satellite and
    ground-station ECI positions and the topocentric elevation. The satellite has access
    when elevation >= ELEVATION_MASK_DEG. Contiguous runs of in-access steps are collapsed
    into discrete windows, each reported with start_t_s, end_t_s, duration_s, and
    max_elevation_deg (duration is the time from the first to the last in-access sample).
    """
    a = SEMI_MAJOR_AXIS_KM
    mu = MU_EARTH_KM3_S2
    inclination = math.radians(INCLINATION_DEG)
    lat = math.radians(GROUND_STATION_LAT_DEG)
    lon = math.radians(GROUND_STATION_LON_DEG)
    mean_motion = math.sqrt(mu / a**3)

    num_steps = int(SPAN_S // TIME_STEP_S)

    windows = []
    current = None  # open window: {"start_t_s", "end_t_s", "max_elev_raw"}

    for k in range(num_steps + 1):
        t = k * TIME_STEP_S
        sat = _sat_eci(a, inclination, mean_motion, t)
        gs = _gs_eci(lat, lon, t)
        elev = _elevation_deg(sat, gs)

        if elev >= ELEVATION_MASK_DEG:
            if current is None:
                current = {"start_t_s": t, "end_t_s": t, "max_elev_raw": elev}
            else:
                current["end_t_s"] = t
                if elev > current["max_elev_raw"]:
                    current["max_elev_raw"] = elev
        else:
            if current is not None:
                windows.append(current)
                current = None

    # Close a window still open at the end of the span.
    if current is not None:
        windows.append(current)

    access_windows = []
    for w in windows:
        duration = w["end_t_s"] - w["start_t_s"]
        access_windows.append(
            {
                "start_t_s": w["start_t_s"],
                "end_t_s": w["end_t_s"],
                "duration_s": duration,
                "max_elevation_deg": round(w["max_elev_raw"], ROUND_DECIMALS),
            }
        )

    num_passes = len(access_windows)
    total_access_s = sum(w["duration_s"] for w in access_windows)
    longest_pass_s = max((w["duration_s"] for w in access_windows), default=0)
    max_elev_overall = max((w["max_elev_raw"] for w in windows), default=0.0)

    return {
        "task_id": "task-0004-comms-access",
        "inputs": {
            "mu_km3_s2": MU_EARTH_KM3_S2,
            "earth_radius_km": R_EARTH_KM,
            "omega_earth_rad_s": OMEGA_EARTH_RAD_S,
            "semi_major_axis_km": SEMI_MAJOR_AXIS_KM,
            "inclination_deg": INCLINATION_DEG,
            "ground_station_lat_deg": GROUND_STATION_LAT_DEG,
            "ground_station_lon_deg": GROUND_STATION_LON_DEG,
            "gmst0_rad": GMST0_RAD,
            "elevation_mask_deg": ELEVATION_MASK_DEG,
            "time_step_s": TIME_STEP_S,
            "span_s": SPAN_S,
            "round_decimals": ROUND_DECIMALS,
        },
        "results": access_windows,
        "summary": {
            "num_passes": num_passes,
            "total_access_s": total_access_s,
            "longest_pass_s": longest_pass_s,
            "max_elevation_deg_overall": round(max_elev_overall, ROUND_DECIMALS),
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
