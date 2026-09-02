"""task-0034-edl-deceleration-budget — deterministic ballistic Mars-entry
deceleration budget across published ballistic-coefficient classes, WITH
THE NATURAL HONEST NEGATIVE (parented on task-0033; a CONSTRAINING node of
mission-0001-v3).

Research-only. A bit-reproducible EDL task: from task-0033's entry-
interface speed (CONSUMED live, hash-asserted) and the MSL-class entry
flight-path angle, a planar ballistic (zero-lift) entry is integrated with
a FIXED-step, hard-capped loop through the fetched-and-hashed NASA GRC
Mars atmosphere curve fits (pinned_mars_edl_sources):

    dv/dt = -rho v^2/(2 beta) - g sin(gamma)
    dgamma/dt = (v/r - g/v) cos(gamma)
    dh/dt = v sin(gamma)

(the standard planar entry set with gamma negative descending; a midpoint
RK2 step keeps the energy audit below 1e-4 relative)

for each published ballistic-coefficient class (Viking ~64 kg/m^2,
MSL-class ~146, heavy-lander ~400 — the multi-tonne/ISRU-cargo class the
mission chain actually needs). Each class is judged at the parachute-
deploy gate: Mach at the deploy altitude against the DGB qualification
ceiling, and peak deceleration against a stated limit. It maps to the
NASA Technology Taxonomy TX09 (Entry, Descent, and Landing).

THE HONEST NEGATIVES, KEPT ON PURPOSE — the Braun & Manning thesis,
RE-DERIVED rather than quoted: only the Viking-class coefficient reaches
the deploy gate subsonic-transition-ready (Mach ~2.0); the MSL class
BALLISTIC honestly fails (Mach ~6.3 — which is exactly why the real MSL flew
a lifting guided entry, a capability this task deliberately does not
model and says so), and the heavy-lander class fails catastrophically
(Mach ~14.5) — the payload class the surface chain needs honestly cannot
decelerate within limits under ballistic entry + supersonic chute. The
verdict field the mission chain consumes is
    reference_class_decelerates = (the heavy-lander class passes) = FALSE.
NOTHING IS TUNED: entry state from the anchored chain, atmosphere and
classes and gates from pinned published sources.

INTERNAL SELF-PROOF (four assertion classes inside compute() per MIP-0008
rule 1): compute() asserts
  (a) PARENT-HASH LIVENESS (task-0033);
  (b) CONSERVATION — for every class, dissipated energy + final mechanical
      energy recovers the initial mechanical energy to within the stated
      integrator tolerance (fixed-step error bound, published);
  (c) KNOWN-TRUTH — the Viking-class run lands inside the flown/
      qualified DGB deploy envelope (Mach 1.0-2.2: Viking deployed near
      Mach 1.1, the qualification ceiling is ~2.2), every peak deceleration lands in the published 5-15 g
      Mars-entry class, and absolute temperature stays positive over the
      whole drag domain (the pinned model's stated validity);
  (d) BOUNDS/MONOTONICITY — every integration terminates within its hard
      step cap, and deploy-gate speed is strictly increasing in ballistic
      coefficient.
A violated assertion CRASHES the task — stop, don't fudge.

Planar ballistic point-mass only: NO lift/guidance (MSL's actual remedy),
no supersonic retropropulsion, no aeroshell thermal analysis, no winds.
Test-META is a zero-value testnet placeholder and never mints base supply
(MIP-0001 paragraph 3, MIP-0002 paragraph 8). Not financial, legal, or
flight-engineering advice. No NASA affiliation or endorsement.

Standard library only (json, math, hashlib) plus the parent task module
and the pinned-constants modules. No randomness; a fixed-step explicit
integrator with a hard iteration cap (cross-platform-stable IEEE
arithmetic, the library's fixed-loop discipline). Every emitted float is
rounded to a fixed number of decimals so re-runs are byte-identical and
the SHA-256 output hash is stable (the basis of the Gate-2 check).
MIP-0009 contract: compute() -> the four-key dict, canonical_json() era-2
(sign-of-zero-free), output_hash() = sha256 of it.
"""

import hashlib
import json
import math

try:
    from demo.tasks import task_0033_mars_capture_entry_interface as _entry_parent
    from demo.tasks import pinned_spice_sources as _spice
    from demo.tasks import pinned_mars_edl_sources as _edl
except ImportError:  # direct script run: demo/tasks/ itself is sys.path[0]
    import task_0033_mars_capture_entry_interface as _entry_parent
    import pinned_spice_sources as _spice
    import pinned_mars_edl_sources as _edl

PARENT_TASKS = ["task-0033"]

EXPECTED_PARENT_OUTPUT_HASH = (
    "1c9d584b0c890edb302aa845412e473a0eeadd35622cbd94f8c3459cfb219dea"
)

# --- Fixed inputs (part of the reproducibility hash) ------------------------
# THE CONSTANTS ARE NOT TO BE TUNED TO MANUFACTURE SUCCESS OR FAILURE.
GM_MARS_M3_S2 = _spice.GM_MARS_KM3_S2 * 1e9
R_MARS_M = _spice.MARS_RADII_KM[0] * 1e3
BETA_CLASSES = _edl.BALLISTIC_COEFF_CLASSES_KG_M2
REFERENCE_CLASS = "heavy_lander_class"     # the mission-relevant class
GAMMA0_DEG = _edl.ENTRY_FLIGHT_PATH_ANGLE_DEG
H0_M = _edl.ENTRY_INTERFACE_ALTITUDE_M
DEPLOY_ALTITUDE_M = _edl.CHUTE_DEPLOY_ALTITUDE_M
MACH_LIMIT = _edl.CHUTE_DEPLOY_MACH_LIMIT
PEAK_G_LIMIT = _edl.PEAK_DECEL_LIMIT_G
DRAG_ONSET_ALTITUDE_M = 80e3   # stated: the pinned curve fit's honest
                               # ceiling (its temperature law breaks higher;
                               # density above is negligible for the budget)
DT_S = 0.05                    # fixed integrator step
MAX_STEPS = 400000             # the stated hard cap (rule 2)
G0_M_S2 = 9.80665              # standard gravity for g-load reporting
VIKING_DEPLOY_BAND_MACH = (1.0, 2.2)   # the flown/qualified DGB envelope
PEAK_G_CLASS = (5.0, 15.0)
ENERGY_CLOSE_REL_TOL = 1e-4    # midpoint-integrator conservation bound
ROUND_DECIMALS = 6


def _temperature_K(h_m: float) -> float:
    if h_m <= _edl.LOWER_ZONE_CEILING_M:
        t_c = _edl.T_LOWER_C0 + _edl.T_LOWER_LAPSE_C_PER_M * h_m
    else:
        t_c = _edl.T_UPPER_C0 + _edl.T_UPPER_LAPSE_C_PER_M * h_m
    return t_c + _edl.KELVIN_OFFSET_K


def _density_kg_m3(h_m: float) -> float:
    if h_m > DRAG_ONSET_ALTITUDE_M:
        return 0.0
    t_k = _temperature_K(h_m)
    assert t_k > 0.0, (
        f"pinned-model validity violated: absolute temperature {t_k} K at "
        f"{h_m} m inside the drag domain")
    p_kpa = _edl.P_SURFACE_KPA * math.exp(_edl.P_SCALE_PER_M * h_m)
    return p_kpa / (_edl.RHO_GAS_CONSTANT_KPA_M3_KG_K * t_k)


def _sound_speed_m_s(h_m: float) -> float:
    return math.sqrt(_edl.CO2_GAMMA_RATIO * _edl.GAS_CONSTANT_J_KG_K
                     * _temperature_K(h_m))


def _derivatives(v, gamma, h, beta_kg_m2):
    r = R_MARS_M + h
    g = GM_MARS_M3_S2 / r ** 2
    drag_a = _density_kg_m3(h) * v * v / (2.0 * beta_kg_m2)
    return (-drag_a - g * math.sin(gamma),
            (v / r - g / v) * math.cos(gamma),
            v * math.sin(gamma),
            drag_a)


def integrate_entry(beta_kg_m2: float, v0_m_s: float):
    """Fixed-step midpoint (RK2) planar ballistic entry from the interface
    to the deploy altitude. Returns {final v, final h, peak g, steps,
    dissipated J/kg} — the dissipation is sampled at the midpoint state, so
    the mechanical-energy audit closes to the integrator's second-order
    accuracy."""
    v, gamma, h = v0_m_s, math.radians(GAMMA0_DEG), H0_M
    peak_a = 0.0
    dissipated_j_kg = 0.0
    steps = 0
    for _ in range(MAX_STEPS):          # bounded by MAX_STEPS
        if h <= DEPLOY_ALTITUDE_M:
            break
        dv1, dg1, dh1, _d1 = _derivatives(v, gamma, h, beta_kg_m2)
        vm = v + dv1 * DT_S / 2.0
        gm = gamma + dg1 * DT_S / 2.0
        hm = h + dh1 * DT_S / 2.0
        dv2, dg2, dh2, drag_mid = _derivatives(vm, gm, hm, beta_kg_m2)
        dissipated_j_kg += drag_mid * vm * DT_S
        v += dv2 * DT_S
        gamma += dg2 * DT_S
        h += dh2 * DT_S
        peak_a = max(peak_a, drag_mid)
        steps += 1
    return {"v_m_s": v, "h_m": h, "peak_g": peak_a / G0_M_S2,
            "steps": steps, "dissipated_j_kg": dissipated_j_kg}


def compute() -> dict:
    """The three-class EDL budget + the honest-negative verdict."""
    # --- SELF-PROOF (a): parent-hash liveness ------------------------------
    parent_result = _entry_parent.compute()
    parent_hash = _entry_parent.output_hash(parent_result)
    assert parent_hash == EXPECTED_PARENT_OUTPUT_HASH, (
        f"parent-hash liveness violated: task-0033 recomputes to {parent_hash}, "
        f"expected {EXPECTED_PARENT_OUTPUT_HASH} — this task refuses drifted input")
    v0_m_s = float(
        parent_result["summary"]["entry_interface_speed_km_s"]) * 1e3

    rows = []
    verdicts = {}
    deploy_speeds = []
    for name, beta in BETA_CLASSES:     # bounded: three classes
        out = integrate_entry(beta, v0_m_s)
        # --- SELF-PROOF (d): the integration terminated at the gate --------
        assert out["steps"] < MAX_STEPS and out["h_m"] <= DEPLOY_ALTITUDE_M + 100.0, (
            f"bounds violated for {name}: {out['steps']} steps ended at "
            f"{out['h_m']} m")
        # --- SELF-PROOF (b): energy bookkeeping closes ---------------------
        r0, rf = R_MARS_M + H0_M, R_MARS_M + out["h_m"]
        e0 = v0_m_s ** 2 / 2.0 - GM_MARS_M3_S2 / r0
        ef = out["v_m_s"] ** 2 / 2.0 - GM_MARS_M3_S2 / rf
        residual = abs((ef + out["dissipated_j_kg"]) - e0)
        assert residual <= ENERGY_CLOSE_REL_TOL * abs(e0), (
            f"energy conservation violated for {name}: residual {residual} "
            f"J/kg against initial {e0} (tolerance "
            f"{ENERGY_CLOSE_REL_TOL} relative, midpoint bound)")
        mach = out["v_m_s"] / _sound_speed_m_s(out["h_m"])
        # --- SELF-PROOF (c): peak deceleration in the published class ------
        assert PEAK_G_CLASS[0] <= out["peak_g"] <= PEAK_G_CLASS[1], (
            f"known-truth violated: {name} peak {out['peak_g']} g outside "
            f"the published {PEAK_G_CLASS} Mars-entry class")
        within = (mach <= MACH_LIMIT) and (out["peak_g"] <= PEAK_G_LIMIT)
        verdicts[name] = within
        deploy_speeds.append(out["v_m_s"])
        rows.append({
            "class_label": name,
            "ballistic_coefficient_kg_per_m2": beta,
            "deploy_altitude_speed_m_s": round(out["v_m_s"], ROUND_DECIMALS),
            "deploy_mach_ratio": round(mach, ROUND_DECIMALS),
            "peak_deceleration_g_dimensionless": round(out["peak_g"],
                                                       ROUND_DECIMALS),
            "integration_steps_count": out["steps"],
            "within_limits": within,
        })

    # --- SELF-PROOF (c, continued): the Viking-class flown band ------------
    viking_mach = rows[0]["deploy_mach_ratio"]
    assert VIKING_DEPLOY_BAND_MACH[0] <= viking_mach <= VIKING_DEPLOY_BAND_MACH[1], (
        f"known-truth violated: Viking-class deploy Mach {viking_mach} "
        f"outside the flown {VIKING_DEPLOY_BAND_MACH} band")
    # --- SELF-PROOF (d, continued): monotone in beta -----------------------
    for i in range(len(deploy_speeds) - 1):   # bounded: two pairs
        assert deploy_speeds[i + 1] > deploy_speeds[i], (
            "monotonicity violated: deploy speed did not rise with "
            "ballistic coefficient")

    reference_ok = verdicts[REFERENCE_CLASS]
    msl_row = rows[1]
    heavy_row = rows[2]
    return {
        "task_id": "task-0034-edl-deceleration-budget",
        "inputs": {
            "parent_task_id": "task-0033",
            "parent_output_hash": parent_hash,
            "entry_speed_from_parent_m_s": round(v0_m_s, ROUND_DECIMALS),
            "entry_flight_path_angle_deg": GAMMA0_DEG,
            "atmosphere_provenance": _edl.GRC_ATMOSPHERE_PROVENANCE[
                "source"] + " (sha256 "
                + _edl.GRC_ATMOSPHERE_PROVENANCE["sha256_page"][:12] + ")",
            "atmosphere_basis": _edl.GRC_ATMOSPHERE_PROVENANCE[
                "basis_quoted"],
            "classes_provenance": _edl.EDL_CLASSES_PROVENANCE["source"],
            "drag_onset_altitude_m": DRAG_ONSET_ALTITUDE_M,
            "drag_onset_basis": "the pinned curve fit's honest validity "
                                "ceiling (its temperature law breaks "
                                "higher; density above is negligible)",
            "deploy_altitude_m": DEPLOY_ALTITUDE_M,
            "deploy_mach_limit_ratio": MACH_LIMIT,
            "peak_decel_limit_g_dimensionless": PEAK_G_LIMIT,
            "reference_class_label": REFERENCE_CLASS,
            "reference_basis": "the multi-tonne/ISRU-cargo class the "
                               "surface chain actually needs to land",
            "integrator_dt_s": DT_S,
            "max_steps_count": MAX_STEPS,
            "round_decimals": ROUND_DECIMALS,
        },
        "results": rows,
        "summary": {
            "reference_class_decelerates": reference_ok,
            # the size of the honest "no", as a plain number
            "reference_deploy_mach_excess_ratio": round(
                max(0.0, heavy_row["deploy_mach_ratio"] - MACH_LIMIT),
                ROUND_DECIMALS),
            "classes_within_limits_count": sum(verdicts.values()),
            "classes_count": len(rows),
            "viking_class_deploy_mach_ratio": viking_mach,
            "msl_class_deploy_mach_ratio": msl_row["deploy_mach_ratio"],
            "heavy_class_deploy_mach_ratio": heavy_row["deploy_mach_ratio"],
            "verdict_note": "the Braun & Manning thesis, re-derived: only "
                            "the Viking-class coefficient reaches the "
                            "chute gate within the DGB ceiling; the MSL "
                            "class fails BALLISTIC (the real MSL flew "
                            "lifting guided entry — not modeled, stated) "
                            "and the heavy-lander class the mission needs "
                            "arrives hypersonic — an honest negative, "
                            "kept on purpose",
            "self_proofs_checked": ["parent_hash_liveness",
                                    "energy_closure_x3",
                                    "viking_band_peak_g_and_model_validity",
                                    "termination_and_beta_monotonicity"],
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
