"""task-0033-mars-capture-entry-interface — deterministic Mars-arrival
state: from task-0031's transfer-window arrival v-infinity to the entry-
interface speed and the propulsive-capture alternative (parented on
task-0031; the node that closes mission-0001-v2's own named gap).

Research-only. A bit-reproducible arrival-energetics task: the best
transfer window's arrival hyperbolic excess (CONSUMED from task-0031's
published summary, hash-asserted) fixes the arrival energy, and vis-viva
does the rest — both from GM_Mars pinned fetched-and-hashed in
demo/tasks/pinned_spice_sources.py (gm_de440.tpc) and the MSL-convention
entry-interface altitude pinned in demo/tasks/pinned_mars_edl_sources.py:

  entry-interface speed  v_EI = sqrt(v_inf^2 + 2 GM/r_EI)   (direct entry)
  capture delta-v        dv   = sqrt(v_inf^2 + 2 GM/r_p) - sqrt(GM/r_p)
                               (periapsis burn into a circular parking
                                orbit at a stated altitude)

It maps to the NASA Technology Taxonomy TX09 (Entry, Descent, and Landing
— the arrival interface the EDL budget consumes).

INTERNAL SELF-PROOF (three assertion classes inside compute() per MIP-0008
rule 1): compute() asserts
  (a) PARENT-HASH LIVENESS (task-0031, the provenance edge);
  (b) KNOWN-TRUTH — the entry speed computed via vis-viva equals the speed
      recovered from energy bookkeeping (v^2/2 - GM/r == v_inf^2/2, two
      arithmetic paths) to within 1e-9 relative, and v_EI lands in the
      published Mars-arrival 5.5-7.5 km/s class (MSL flew ~5.8-6);
  (c) BOUNDS/ORDERING — v_EI exceeds local escape speed (a hyperbolic
      arrival must), the capture delta-v is positive and smaller than the
      entry speed, and both scale monotonically with v_inf (checked by
      recomputing at v_inf + 0.1 km/s).
A violated assertion CRASHES the task — stop, don't fudge.

Point-mass two-body arrival only (no plane geometry, no aerocapture, no
gravity losses on the capture burn). SPICE kernel data are U.S. government
works distributed by NAIF ("No fees or licensing are required" — quoted in
the pinned module). Test-META is a zero-value testnet placeholder and
never mints base supply (MIP-0001 paragraph 3, MIP-0002 paragraph 8).
Not financial, legal, or flight-engineering advice.
No NASA affiliation or endorsement.

Standard library only (json, math, hashlib) plus the parent task module
and the pinned-constants modules. No randomness. Every emitted float is
rounded to a fixed number of decimals so re-runs are byte-identical and
the SHA-256 output hash is stable (the basis of the Gate-2 check).
MIP-0009 contract: compute() -> the four-key dict, canonical_json() era-2
(sign-of-zero-free), output_hash() = sha256 of it.
"""

import hashlib
import json
import math

try:
    from demo.tasks import task_0031_earth_mars_window as _window_parent
    from demo.tasks import pinned_spice_sources as _spice
    from demo.tasks import pinned_mars_edl_sources as _edl
except ImportError:  # direct script run: demo/tasks/ itself is sys.path[0]
    import task_0031_earth_mars_window as _window_parent
    import pinned_spice_sources as _spice
    import pinned_mars_edl_sources as _edl

PARENT_TASKS = ["task-0031"]

EXPECTED_PARENT_OUTPUT_HASH = (
    "e25ac48b02efe5b722efb800085d91f280dfbed8b7ce9deba775514f5fbf21b0"
)

# --- Fixed inputs (part of the reproducibility hash) ------------------------
GM_MARS_KM3_S2 = _spice.GM_MARS_KM3_S2          # gm_de440, fetched-hashed
R_MARS_KM = _spice.MARS_RADII_KM[0]             # pck00011 equatorial radius
ENTRY_INTERFACE_ALTITUDE_KM = _edl.ENTRY_INTERFACE_ALTITUDE_M / 1e3
PARKING_ORBIT_ALTITUDE_KM = 400.0               # stated parking-orbit class
ARRIVAL_CLASS_KM_S = (5.5, 7.5)                 # published Mars-arrival band
ENERGY_CROSSCHECK_REL_TOL = 1e-9
MONOTONE_PROBE_KM_S = 0.1
ROUND_DECIMALS = 6


def _v_at_radius(v_inf_km_s: float, r_km: float) -> float:
    """Vis-viva on the arrival hyperbola: v = sqrt(v_inf^2 + 2 GM / r)."""
    return math.sqrt(v_inf_km_s ** 2 + 2.0 * GM_MARS_KM3_S2 / r_km)


def _capture_dv(v_inf_km_s: float, r_p_km: float) -> float:
    """Periapsis burn from the arrival hyperbola into a circular orbit."""
    return _v_at_radius(v_inf_km_s, r_p_km) - math.sqrt(
        GM_MARS_KM3_S2 / r_p_km)


def compute() -> dict:
    """The arrival interface state + capture alternative + self-proofs."""
    # --- SELF-PROOF (a): parent-hash liveness ------------------------------
    parent_result = _window_parent.compute()
    parent_hash = _window_parent.output_hash(parent_result)
    assert parent_hash == EXPECTED_PARENT_OUTPUT_HASH, (
        f"parent-hash liveness violated: task-0031 recomputes to {parent_hash}, "
        f"expected {EXPECTED_PARENT_OUTPUT_HASH} — this task refuses drifted input")
    s = parent_result["summary"]
    v_inf = float(s["best_vinf_arrival_km_s"])
    window_label = f"{s['best_departure_label']}->{s['best_arrival_label']}"

    r_ei = R_MARS_KM + ENTRY_INTERFACE_ALTITUDE_KM
    r_p = R_MARS_KM + PARKING_ORBIT_ALTITUDE_KM
    v_ei = _v_at_radius(v_inf, r_ei)
    v_esc_ei = math.sqrt(2.0 * GM_MARS_KM3_S2 / r_ei)
    dv_capture = _capture_dv(v_inf, r_p)
    v_circ = math.sqrt(GM_MARS_KM3_S2 / r_p)

    # --- SELF-PROOF (b): energy bookkeeping, two paths ---------------------
    recovered_vinf_sq = v_ei ** 2 - 2.0 * GM_MARS_KM3_S2 / r_ei
    assert abs(recovered_vinf_sq - v_inf ** 2) <= (
        ENERGY_CROSSCHECK_REL_TOL * max(v_inf ** 2, 1.0)), (
        f"energy bookkeeping violated: v_EI recovers v_inf^2 = "
        f"{recovered_vinf_sq}, expected {v_inf ** 2} km2/s2")
    assert ARRIVAL_CLASS_KM_S[0] <= v_ei <= ARRIVAL_CLASS_KM_S[1], (
        f"known-truth violated: entry-interface speed {v_ei} km/s outside "
        f"the published {ARRIVAL_CLASS_KM_S} Mars-arrival class")

    # --- SELF-PROOF (c): bounds, ordering, monotonicity --------------------
    assert v_ei > v_esc_ei, (
        f"bound violated: hyperbolic arrival {v_ei} km/s does not exceed "
        f"local escape {v_esc_ei} km/s")
    assert 0.0 < dv_capture < v_ei, (
        f"bound violated: capture delta-v {dv_capture} km/s outside "
        f"(0, {v_ei})")
    assert (_v_at_radius(v_inf + MONOTONE_PROBE_KM_S, r_ei) > v_ei
            and _capture_dv(v_inf + MONOTONE_PROBE_KM_S, r_p) > dv_capture), (
        "monotonicity violated: a hotter arrival did not raise the entry "
        "speed and capture cost")

    return {
        "task_id": "task-0033-mars-capture-entry-interface",
        "inputs": {
            "parent_task_id": "task-0031",
            "parent_output_hash": parent_hash,
            "arrival_vinf_from_parent_km_s": v_inf,
            "window_note": f"the parent's best window ({window_label})",
            # emitted at the six-decimal boundary per MIP-0009 C3; the
            # computation uses the pinned module's full-precision value
            "gm_mars_km3_s2": round(GM_MARS_KM3_S2, ROUND_DECIMALS),
            "r_mars_km": R_MARS_KM,
            "gm_provenance": "gm_de440.tpc / pck00011.tpc, fetched-and-"
                             "hashed (pinned_spice_sources)",
            "entry_interface_altitude_km": ENTRY_INTERFACE_ALTITUDE_KM,
            "entry_interface_basis": _edl.EDL_CLASSES_PROVENANCE[
                "entry_state_source"],
            "parking_orbit_altitude_km": PARKING_ORBIT_ALTITUDE_KM,
            "round_decimals": ROUND_DECIMALS,
        },
        "results": [
            {"step": "direct_entry",
             "entry_interface_radius_km": round(r_ei, ROUND_DECIMALS),
             "entry_interface_speed_km_s": round(v_ei, ROUND_DECIMALS),
             "local_escape_speed_km_s": round(v_esc_ei, ROUND_DECIMALS)},
            {"step": "propulsive_capture",
             "periapsis_radius_km": round(r_p, ROUND_DECIMALS),
             "capture_dv_km_s": round(dv_capture, ROUND_DECIMALS),
             "circular_orbit_speed_km_s": round(v_circ, ROUND_DECIMALS)},
        ],
        "summary": {
            "entry_interface_speed_km_s": round(v_ei, ROUND_DECIMALS),
            "capture_dv_km_s": round(dv_capture, ROUND_DECIMALS),
            "arrival_vinf_km_s": round(v_inf, ROUND_DECIMALS),
            "class_note": "the derived entry-interface speed lands in the "
                          "flown MSL class (~5.8 km/s) from pinned "
                          "constants and the anchored window alone",
            "self_proofs_checked": ["parent_hash_liveness",
                                    "energy_two_paths_and_class",
                                    "bounds_ordering_monotonicity"],
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
