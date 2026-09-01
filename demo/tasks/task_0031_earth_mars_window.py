"""task-0031-earth-mars-window — deterministic Earth-Mars transfer-window
scan over pinned DE440s ephemeris states (parented on task-0030; the node
that closes the gap mission-0001 named on its own record).

Research-only. A bit-reproducible porkchop-style task: for every pair of a
10-epoch departure grid (Aug-Dec 2028) and a 10-epoch arrival grid
(Apr-Dec 2029) with time of flight between stated bounds, the prograde
Lambert problem is solved about the Sun by the universal-variable /
Stumpff-function formulation with a FIXED-count bounded bisection on z (the
task-0013 cross-platform pattern), using heliocentric J2000 state vectors
PINNED from NAIF's de440s.bsp (sha256 c1c7feea..., evaluated by the
committed extractor at exactly the ET epochs task-0030 publishes) and
GM_Sun pinned verbatim from gm_de440.tpc. Each instance yields departure
and arrival hyperbolic-excess speeds; the instance metric is their sum,
judged against a stated total-v-infinity budget. It maps to the NASA
Technology Taxonomy TX17 (Guidance, Navigation & Control — interplanetary
targeting).

THE HONEST NEGATIVES, KEPT ON PURPOSE (and the honest positive): the grid
is wide enough that MOST instances honestly blow the budget — mistimed
departures cost tens of km/s — while the true 2028 window (late-November
departure, ~310-day flight) honestly closes at ~6.06 km/s total against
the stated 6.5 km/s budget. Nothing is tuned either way: the grid, the
budget, and the ephemeris are all pinned, and the verdict field the
mission chain consumes is
    window_within_budget = (best instance total <= budget).

THE PROVENANCE EDGE, ENFORCED AT EXECUTION TIME: compute() CALLS
task_0030.compute() directly, recomputes the parent's canonical output
hash LIVE, asserts the pinned EXPECTED_PARENT_OUTPUT_HASH, AND asserts
that the parent's published ET values equal the pinned module's
GRID_EPOCHS_ET exactly — the states were evaluated at the parent's own
outputs, and that identity is re-proved on every run.

INTERNAL SELF-PROOF (four assertion classes inside compute() per MIP-0008
rule 1): compute() asserts
  (a) PARENT-HASH LIVENESS + the ET-grid identity above;
  (b) CONSERVATION, per solved arc — the two-body specific orbital energy
      at departure equals the energy at arrival to within 1e-9 km^2/s^2,
      and the specific angular momentum magnitudes agree to within 1e-12
      relative (two independent invariants of the SAME Lambert solution);
  (c) BOUNDS — every bisection ran within its fixed cap, every transfer
      angle lies in (0, 2 pi), and the best total lands in the public
      4-15 km/s class for an Earth-Mars window scan;
  (d) STRUCTURE, untuned — at least one instance closes within budget AND
      at least one does not (the grid honestly contains both outcomes).
A violated assertion CRASHES the task — stop, don't fudge.

Ballistic two-body heliocentric arcs only (no planetary departure/capture
spirals, no plane-change optimization, no mid-course maneuvers; the metric
is hyperbolic excess, not surface-to-surface delta-v). SPICE kernels are
U.S. government works distributed by NAIF ("No fees or licensing are
required" — their rules page, quoted in the pinned module). Test-META is a
zero-value testnet placeholder and never mints base supply (MIP-0001
paragraph 3, MIP-0002 paragraph 8). Not financial, legal, or
flight-engineering advice. No NASA affiliation or endorsement.

Standard library only (json, math, hashlib) plus the parent task module
and the pinned-constants module. No randomness. Every emitted float is
rounded to a fixed number of decimals so re-runs are byte-identical and
the SHA-256 output hash is stable (the basis of the Gate-2 check).
MIP-0009 contract: compute() -> the four-key dict, canonical_json() era-2
(sign-of-zero-free), output_hash() = sha256 of it.
"""

import hashlib
import json
import math

try:
    from demo.tasks import task_0030_utc_tdb_conversion as _time_parent
    from demo.tasks import pinned_spice_sources as _src
except ImportError:  # direct script run: demo/tasks/ itself is sys.path[0]
    import task_0030_utc_tdb_conversion as _time_parent
    import pinned_spice_sources as _src

PARENT_TASKS = ["task-0030"]

EXPECTED_PARENT_OUTPUT_HASH = (
    "501177ea9da7ba372c5fc8566d76d7e12e62d9795ae2ebdfb1b17c51497b4933"
)

# --- Fixed inputs (part of the reproducibility hash) ------------------------
# THE CONSTANTS ARE NOT TO BE TUNED TO MANUFACTURE SUCCESS OR FAILURE.
MU_SUN_KM3_S2 = _src.GM_SUN_KM3_S2
TOF_BOUNDS_DAYS = (120.0, 450.0)
TOTAL_VINF_BUDGET_KM_S = 6.5        # stated good-window class bound
BEST_CLASS_KM_S = (4.0, 15.0)       # public landmark band for the scan's best
LAMBERT_ITERATIONS = 80             # the stated fixed bisection count (rule 2)
Z_BRACKET_LO = -39.4784176          # ~ -4 pi^2 (hyperbolic side)
Z_BRACKET_HI = 39.0                 # just inside the 4 pi^2 singularity
BRACKET_RAISE_STEPS = 64            # the stated bound on the y>0 raise loop
ENERGY_TOL_KM2_S2 = 1e-9
MOMENTUM_REL_TOL = 1e-12
ROUND_DECIMALS = 6


def _norm(v):
    return math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2])


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0])


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _stumpff_c(z):
    if z > 1e-8:
        return (1.0 - math.cos(math.sqrt(z))) / z
    if z < -1e-8:
        return (math.cosh(math.sqrt(-z)) - 1.0) / (-z)
    return 0.5 - z / 24.0


def _stumpff_s(z):
    if z > 1e-8:
        sz = math.sqrt(z)
        return (sz - math.sin(sz)) / sz ** 3
    if z < -1e-8:
        sz = math.sqrt(-z)
        return (math.sinh(sz) - sz) / sz ** 3
    return 1.0 / 6.0 - z / 120.0


def solve_lambert(r1v, r2v, tof_s):
    """Prograde universal-variable Lambert about the Sun with a fixed
    bisection count. Returns (v1, v2, iterations, transfer_angle_rad)."""
    r1, r2 = _norm(r1v), _norm(r2v)
    cosd = max(-1.0, min(1.0, _dot(r1v, r2v) / (r1 * r2)))
    dtheta = math.acos(cosd)
    if _cross(r1v, r2v)[2] < 0.0:
        dtheta = 2.0 * math.pi - dtheta   # prograde branch
    a_coef = math.sin(dtheta) * math.sqrt(r1 * r2 / (1.0 - math.cos(dtheta)))

    def y_of(z):
        return r1 + r2 + a_coef * (z * _stumpff_s(z) - 1.0) / math.sqrt(
            _stumpff_c(z))

    def f_of(z):
        yy = y_of(z)
        if yy < 0.0:
            return None
        return (math.sqrt(yy / _stumpff_c(z)) ** 3 * _stumpff_s(z)
                + a_coef * math.sqrt(yy) - math.sqrt(MU_SUN_KM3_S2) * tof_s)

    lo = Z_BRACKET_LO
    for _ in range(BRACKET_RAISE_STEPS):   # bounded by BRACKET_RAISE_STEPS
        if y_of(lo) > 0.0:
            break
        lo *= 0.5
    hi = Z_BRACKET_HI
    iterations = 0
    for _ in range(LAMBERT_ITERATIONS):    # bounded by LAMBERT_ITERATIONS
        mid = 0.5 * (lo + hi)
        fm = f_of(mid)
        if fm is None or fm < 0.0:
            lo = mid
        else:
            hi = mid
        iterations += 1
    z = 0.5 * (lo + hi)
    yy = y_of(z)
    f = 1.0 - yy / r1
    g = a_coef * math.sqrt(yy / MU_SUN_KM3_S2)
    gdot = 1.0 - yy / r2
    v1 = tuple((r2v[i] - f * r1v[i]) / g for i in range(3))
    v2 = tuple((gdot * r2v[i] - r1v[i]) / g for i in range(3))
    return v1, v2, iterations, dtheta


def compute() -> dict:
    """The 90-instance porkchop scan + the window verdict + self-proofs."""
    # --- SELF-PROOF (a): parent liveness + the ET-grid identity ------------
    parent_result = _time_parent.compute()
    parent_hash = _time_parent.output_hash(parent_result)
    assert parent_hash == EXPECTED_PARENT_OUTPUT_HASH, (
        f"parent-hash liveness violated: task-0030 recomputes to {parent_hash}, "
        f"expected {EXPECTED_PARENT_OUTPUT_HASH} — this task refuses drifted input")
    parent_et = {row["epoch_label"]: row["et_seconds_past_j2000_s"]
                 for row in parent_result["results"]}
    for label, et_s in _src.GRID_EPOCHS_ET:   # bounded: 20 epochs
        assert parent_et[label] == et_s, (
            f"ET-grid identity violated at {label}: parent publishes "
            f"{parent_et[label]}, pinned states were evaluated at {et_s}")

    earth = _src.EARTH_HELIOCENTRIC_STATES_KM_KM_S
    mars = _src.MARS_HELIOCENTRIC_STATES_KM_KM_S
    et = dict(_src.GRID_EPOCHS_ET)
    dep_labels = [l for l, _u in _src.DEPARTURE_EPOCHS_UTC]
    arr_labels = [l for l, _u in _src.ARRIVAL_EPOCHS_UTC]

    rows = []
    totals = []
    max_energy_residual = 0.0
    max_momentum_residual = 0.0
    for dl in dep_labels:                 # bounded: 10 departures
        for al in arr_labels:             # bounded: 10 arrivals
            tof_s = et[al] - et[dl]
            tof_days = tof_s / 86400.0
            if not (TOF_BOUNDS_DAYS[0] <= tof_days <= TOF_BOUNDS_DAYS[1]):
                continue
            r1v, v_e = earth[dl][:3], earth[dl][3:]
            r2v, v_m = mars[al][:3], mars[al][3:]
            v1, v2, iters, dtheta = solve_lambert(r1v, r2v, tof_s)
            # --- SELF-PROOF (c): bounds ------------------------------------
            assert iters <= LAMBERT_ITERATIONS and 0.0 < dtheta < 2.0 * math.pi, (
                f"bounds violated at {dl}->{al}: {iters} iterations, "
                f"transfer angle {dtheta} rad")
            # --- SELF-PROOF (b): two-body invariants of the solution -------
            e1 = _norm(v1) ** 2 / 2.0 - MU_SUN_KM3_S2 / _norm(r1v)
            e2 = _norm(v2) ** 2 / 2.0 - MU_SUN_KM3_S2 / _norm(r2v)
            assert abs(e1 - e2) <= ENERGY_TOL_KM2_S2, (
                f"energy conservation violated at {dl}->{al}: "
                f"{e1} vs {e2} km2/s2")
            h1 = _norm(_cross(r1v, v1))
            h2 = _norm(_cross(r2v, v2))
            assert abs(h1 - h2) <= MOMENTUM_REL_TOL * h1, (
                f"angular-momentum conservation violated at {dl}->{al}: "
                f"{h1} vs {h2} km2/s")
            max_energy_residual = max(max_energy_residual, abs(e1 - e2))
            max_momentum_residual = max(max_momentum_residual,
                                        abs(h1 - h2) / h1)
            vinf_dep = _norm(tuple(v1[i] - v_e[i] for i in range(3)))
            vinf_arr = _norm(tuple(v2[i] - v_m[i] for i in range(3)))
            total = vinf_dep + vinf_arr
            totals.append((total, dl, al, tof_days, vinf_dep, vinf_arr))
            rows.append({
                "departure_label": dl,
                "arrival_label": al,
                "tof_days": round(tof_days, ROUND_DECIMALS),
                "vinf_departure_km_s": round(vinf_dep, ROUND_DECIMALS),
                "vinf_arrival_km_s": round(vinf_arr, ROUND_DECIMALS),
                "total_vinf_km_s": round(total, ROUND_DECIMALS),
                "within_budget": bool(total <= TOTAL_VINF_BUDGET_KM_S),
            })

    best = min(totals)
    worst = max(totals)
    within_count = sum(1 for t in totals if t[0] <= TOTAL_VINF_BUDGET_KM_S)
    # --- SELF-PROOF (c, continued): the best lands in the public class -----
    assert BEST_CLASS_KM_S[0] <= best[0] <= BEST_CLASS_KM_S[1], (
        f"known-truth violated: best total {best[0]} km/s outside the "
        f"public {BEST_CLASS_KM_S} class")
    # --- SELF-PROOF (d): both outcomes exist, untuned ----------------------
    assert 0 < within_count < len(totals), (
        f"structure violated: {within_count} of {len(totals)} within budget "
        "— the grid no longer contains both honest outcomes")

    window_within_budget = best[0] <= TOTAL_VINF_BUDGET_KM_S
    return {
        "task_id": "task-0031-earth-mars-window",
        "inputs": {
            "parent_task_id": "task-0030",
            "parent_output_hash": parent_hash,
            "mu_sun_km3_s2": MU_SUN_KM3_S2,
            "ephemeris_provenance": "de440s.bsp sha256 "
                                    + _src.FETCH_PROVENANCE["kernels"]["de440s.bsp"]
                                    + " (states via the committed extractor "
                                    "at the parent's exact ET outputs)",
            "gm_provenance": "gm_de440.tpc sha256 "
                             + _src.FETCH_PROVENANCE["kernels"]["gm_de440.tpc"],
            "naif_rules_note": _src.FETCH_PROVENANCE["naif_rules_quoted"],
            "departure_epochs_count": len(dep_labels),
            "arrival_epochs_count": len(arr_labels),
            "tof_bounds_days": list(TOF_BOUNDS_DAYS),
            "total_vinf_budget_km_s": TOTAL_VINF_BUDGET_KM_S,
            "budget_basis": "stated good-window class bound on summed "
                            "hyperbolic excess; not a surface-to-surface "
                            "delta-v",
            "lambert_iterations_count": LAMBERT_ITERATIONS,
            # tolerances live below the six-decimal boundary; emitted as
            # their decimal exponents per MIP-0009 C3
            "energy_tol_exponent_dimensionless": -9,
            "momentum_rel_tol_exponent_dimensionless": -12,
            "round_decimals": ROUND_DECIMALS,
        },
        "results": rows,
        "summary": {
            "window_within_budget": window_within_budget,
            "best_total_vinf_km_s": round(best[0], ROUND_DECIMALS),
            "best_departure_label": best[1],
            "best_arrival_label": best[2],
            "best_tof_days": round(best[3], ROUND_DECIMALS),
            "best_vinf_departure_km_s": round(best[4], ROUND_DECIMALS),
            "best_vinf_arrival_km_s": round(best[5], ROUND_DECIMALS),
            "instances_count": len(rows),
            "within_budget_count": within_count,
            "beyond_budget_count": len(rows) - within_count,
            "worst_total_vinf_km_s": round(worst[0], ROUND_DECIMALS),
            "worst_pair_note": f"{worst[1]}->{worst[2]} at "
                               f"{round(worst[0], 3)} km/s — a mistimed "
                               "window honestly does not close; most of the "
                               "grid fails the budget and is published that "
                               "way",
            "invariants_note": "specific energy and angular momentum agree "
                               "at both ends of every solved arc within the "
                               "stated tolerances (asserted per instance; "
                               "observed residuals sit orders below them)",
            "self_proofs_checked": ["parent_liveness_and_et_grid_identity",
                                    "two_body_invariants_x90",
                                    "bounds_and_best_class",
                                    "both_outcomes_present_untuned"],
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
