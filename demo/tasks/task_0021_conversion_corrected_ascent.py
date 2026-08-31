"""task-0021-conversion-corrected-ascent — deterministic ascent feasibility at
the THERMODYNAMICALLY HONEST conversion: the propellant-mass-to-delta-v bridge
between task-0020's equilibrium conversion and task-0018's ascent requirement
(the third task born under MIP-0008 / MIP-0009 law; parented on task-0020 AND
task-0017 AND task-0018 — the node where the mission chain's two branches meet).

Research-only. A bit-reproducible connector task built because the mission
chain has a REAL gap in it: task-0017's propellant budget (and through it
task-0018's ascent verdict) consumes task-0015's mass balance at the ASSUMED
92 % single-pass conversion, while task-0020 proves the equilibrium conversion
at the chain's reference operating point (700 K, 1 bar) is only ~0.81 — and no
anchored task said what the ascent budget becomes at the conversion the
thermodynamics actually allows. This task closes that gap with real physics,
not narrative glue: task-0015's single-pass mass balance is LINEAR in
conversion by construction (every product stream is a fixed multiple of the
moles of CO2 converted), so the corrected propellant load is the parent budget
scaled by (equilibrium conversion / assumed conversion), and the corrected
achievable delta-v follows from the Tsiolkovsky rocket equation with
task-0017's own fixed dry mass and Isp, judged against task-0018's published
required delta-v. It maps to the NASA Technology Taxonomy TX01 (Propulsion
Systems): the ascent propellant budget re-priced at the honest operating point.

THE HONEST NEGATIVE, DEEPENED ON PURPOSE: task-0018 is already infeasible by
~2489 m/s at the assumed conversion; at the equilibrium conversion the
propellant load shrinks by the factor ~0.881 and the shortfall GROWS (~2689
m/s). The constants are NOT to be tuned to manufacture success: this task
exists to quantify how the two anchored negatives compound, and its verdict
field feasible_at_equilibrium_conversion is FALSE at these fixed constants.

THE PROVENANCE EDGES, ENFORCED AT EXECUTION TIME: compute() CALLS
task_0020.compute(), task_0017.compute(), and task_0018.compute() directly,
recomputes each parent's canonical output hash LIVE, and asserts equality with
the pinned EXPECTED_*_OUTPUT_HASH constants — drifted parents are refused,
not consumed. (The three calls execute the whole upstream chain: task-0020
re-proves task-0019 and task-0015, task-0018 re-proves task-0017 which
re-proves task-0015.) The recomputed parent hashes are recorded in this task's
own inputs block, so the emitted artifact carries its lineage verbatim.

INTERNAL SELF-PROOF (four assertion classes, all inside compute() per
MIP-0008 rule 1): compute() asserts
  (a) PARENT-HASH LIVENESS for all three parents (the provenance edges);
  (b) KNOWN-TRUTH, linear scaling — the corrected propellant derived by
      scaling the parent's published total equals the corrected propellant
      re-derived independently from the parent's published LOX/CH4 streams
      with the oxidizer binding (the binding reactant is conversion-invariant
      for a stoichiometric feed: both streams scale by the same factor);
  (c) KNOWN-TRUTH, delta-v cross-check — ln-form and mass-ratio-form of the
      rocket equation agree to within 1e-9 m/s BEFORE rounding; and
  (d) BOUNDS — the scale factor lies in (0, 1] and the corrected delta-v is
      positive and no greater than the parent's published achievable delta-v;
      the margin arithmetic closes EXACTLY on the rounded published figures.
A violated assertion CRASHES the task — stop, don't fudge.

Test-META is a zero-value testnet placeholder and never mints base supply
(MIP-0001 paragraph 3, MIP-0002 paragraph 8). Illustrative bookkeeping on the
parents' published artifacts (linear single-pass rescale; no recycle, kinetics,
boil-off, or vehicle resizing) — NOT a flight ascent design. Not financial,
legal, or flight-engineering advice. No NASA affiliation or endorsement.

Standard library only (json, math, hashlib) plus the three parent task modules.
No randomness. Every emitted float is rounded to a fixed number of decimals so
re-runs are byte-identical and the SHA-256 output hash is stable (the basis of
the Gate-2 check). MIP-0009 contract: compute() -> the four-key dict,
canonical_json() era-2 (sign-of-zero-free), output_hash() = sha256 of it.
"""

import hashlib
import json
import math

# THE PARENT TASKS. Package import when loaded as demo.tasks.task_0021_...;
# bare import when run as a script from demo/tasks/.
try:
    from demo.tasks import task_0020_sabatier_conversion_equilibrium as _conv_parent
    from demo.tasks import task_0017_isru_ascent_budget as _budget_parent
    from demo.tasks import task_0018_ascent_feasibility as _verdict_parent
except ImportError:  # direct script run: demo/tasks/ itself is sys.path[0]
    import task_0020_sabatier_conversion_equilibrium as _conv_parent
    import task_0017_isru_ascent_budget as _budget_parent
    import task_0018_ascent_feasibility as _verdict_parent

# Declared parentage (consumed by protocol/work_molecule.py: the molecule
# builder resolves each entry to the parent's WMID within the same build).
PARENT_TASKS = ["task-0020", "task-0017", "task-0018"]

# The parents' canonical Gate-2 output hashes, PINNED. compute() recomputes
# all three live and asserts equality — the executable provenance edges.
EXPECTED_CONV_PARENT_OUTPUT_HASH = (
    "755db37300b1f220748d10cc330c3286eb82ad7ec2d499029fca0954f4c26be3"
)
EXPECTED_BUDGET_PARENT_OUTPUT_HASH = (
    "01dfdf623cfba5cf55053a067ecc5305868481b8c6f20110745785d52f845125"
)
EXPECTED_VERDICT_PARENT_OUTPUT_HASH = (
    "d31160104ffb8495f16a45aa8d901c527b8203c0b95b47b0e7361fd634a1a1c8"
)

# --- Fixed inputs (part of the reproducibility hash) ------------------------
# Every physical constant is CONSUMED from a parent's published artifact,
# never restated here — the only fixed inputs this module owns are tolerances.
# THE CONSTANTS ARE NOT TO BE TUNED TO MANUFACTURE SUCCESS (docstring).
SCALING_CROSSCHECK_TOL_KG = 1e-6   # scaled total vs stream-wise re-derivation
DV_CROSSCHECK_TOL_M_S = 1e-9       # ln-form vs mass-ratio-form (pre-round)
ROUND_DECIMALS = 6


def compute() -> dict:
    """Conversion-corrected ascent budget and verdict + the self-proofs."""
    # --- SELF-PROOF (a): parent-hash liveness, all three parents ------------
    conv_result = _conv_parent.compute()
    conv_hash = _conv_parent.output_hash(conv_result)
    assert conv_hash == EXPECTED_CONV_PARENT_OUTPUT_HASH, (
        f"parent-hash liveness violated: task-0020 recomputes to {conv_hash}, "
        f"expected {EXPECTED_CONV_PARENT_OUTPUT_HASH} — this task refuses drifted input")
    budget_result = _budget_parent.compute()
    budget_hash = _budget_parent.output_hash(budget_result)
    assert budget_hash == EXPECTED_BUDGET_PARENT_OUTPUT_HASH, (
        f"parent-hash liveness violated: task-0017 recomputes to {budget_hash}, "
        f"expected {EXPECTED_BUDGET_PARENT_OUTPUT_HASH} — this task refuses drifted input")
    verdict_result = _verdict_parent.compute()
    verdict_hash = _verdict_parent.output_hash(verdict_result)
    assert verdict_hash == EXPECTED_VERDICT_PARENT_OUTPUT_HASH, (
        f"parent-hash liveness violated: task-0018 recomputes to {verdict_hash}, "
        f"expected {EXPECTED_VERDICT_PARENT_OUTPUT_HASH} — this task refuses drifted input")

    # The numbers this bridge judges are the numbers the parents PUBLISH.
    # task-0020: the equilibrium conversion at the chain's reference operating
    # point, and the assumed conversion it was judged against (which is
    # task-0015's own input, consumed by 0020 and not restated here).
    xi_eq = float(conv_result["summary"]["reference_equilibrium_conversion_fraction"])
    xi_assumed = float(conv_result["inputs"]["conversion_threshold_fraction"])
    t_ref_K = float(conv_result["summary"]["reference_temperature_K"])
    p_ref_bar = float(conv_result["summary"]["reference_pressure_bar"])
    # task-0017: the propellant budget at the assumed conversion + the fixed
    # vehicle constants the corrected budget must reuse unchanged.
    budget_values = {d["quantity"]: d["value"] for d in budget_result["results"]}
    propellant_assumed_kg = budget_values["usable_propellant_kg"]
    lox_available_kg = budget_values["lox_available_kg"]
    ch4_available_kg = float(budget_result["inputs"]["ch4_from_parent_kg"])
    o_f_ratio = float(budget_result["inputs"]["o_f_ratio"])
    dry_mass_kg = float(budget_result["inputs"]["dry_mass_kg"])
    isp_s = float(budget_result["inputs"]["isp_s"])
    g0_m_s2 = float(budget_result["inputs"]["g0_m_s2"])
    # task-0018: the requirement, and the achievable figure at the assumed
    # conversion (the bound the corrected figure must sit below).
    verdict_values = {d["quantity"]: d["value"] for d in verdict_result["results"]}
    required_dv_m_s = verdict_values["required_dv_m_s"]
    achievable_assumed_dv_m_s = verdict_values["achievable_dv_m_s"]

    # The bridge: task-0015's single-pass mass balance is linear in conversion
    # (every product stream is a fixed multiple of the CO2 converted), so the
    # honest propellant load is the parent budget scaled by xi_eq/xi_assumed.
    scale = xi_eq / xi_assumed
    propellant_scaled_kg = scale * propellant_assumed_kg

    # --- SELF-PROOF (b): stream-wise re-derivation, oxidizer binding --------
    # Both product streams scale by the same factor, so the binding reactant
    # is conversion-invariant: the oxidizer bound (LOX-poor at O/F 3.5) that
    # task-0017 proved at the assumed conversion still binds here.
    lox_corrected_kg = scale * lox_available_kg
    ch4_corrected_available_kg = scale * ch4_available_kg
    assert lox_corrected_kg < o_f_ratio * ch4_corrected_available_kg, (
        "binding-reactant invariance violated: the oxidizer no longer binds "
        "after scaling — impossible for a uniform rescale of both streams")
    ch4_corrected_burned_kg = lox_corrected_kg / o_f_ratio
    propellant_streamwise_kg = lox_corrected_kg + ch4_corrected_burned_kg
    scaling_residual_kg = propellant_streamwise_kg - propellant_scaled_kg
    assert abs(scaling_residual_kg) <= SCALING_CROSSCHECK_TOL_KG, (
        f"linear-scaling cross-check violated: stream-wise corrected "
        f"propellant {propellant_streamwise_kg} kg vs scaled total "
        f"{propellant_scaled_kg} kg (residual {scaling_residual_kg} kg)")

    # Tsiolkovsky with the parent's own fixed vehicle constants.
    m0_kg = dry_mass_kg + propellant_streamwise_kg
    mf_kg = dry_mass_kg
    ve_m_s = isp_s * g0_m_s2
    corrected_dv_ln_form = ve_m_s * math.log(m0_kg / mf_kg)
    corrected_dv_mass_ratio_form = -ve_m_s * math.log(mf_kg / m0_kg)
    # --- SELF-PROOF (c): two arithmetic paths to the same physics -----------
    dv_residual = corrected_dv_ln_form - corrected_dv_mass_ratio_form
    assert abs(dv_residual) <= DV_CROSSCHECK_TOL_M_S, (
        f"delta-v cross-check violated: ln-form {corrected_dv_ln_form} vs "
        f"mass-ratio-form {corrected_dv_mass_ratio_form} (residual {dv_residual} m/s)")

    # --- SELF-PROOF (d): bounds, and the margin closes on rounded figures ---
    assert 0.0 < scale <= 1.0, (
        f"scale bound violated: xi_eq/xi_assumed = {scale} outside (0, 1] — "
        "the equilibrium conversion cannot exceed the assumed conversion here")
    assert 0.0 < corrected_dv_ln_form <= achievable_assumed_dv_m_s, (
        f"delta-v bound violated: corrected {corrected_dv_ln_form} m/s not in "
        f"(0, {achievable_assumed_dv_m_s}] — less propellant cannot buy more delta-v")
    required_rounded = round(required_dv_m_s, ROUND_DECIMALS)
    corrected_rounded = round(corrected_dv_ln_form, ROUND_DECIMALS)
    margin_rounded = round(corrected_rounded - required_rounded, ROUND_DECIMALS)
    assert corrected_rounded - required_rounded == margin_rounded, (
        f"margin arithmetic violated: {corrected_rounded} - "
        f"{required_rounded} != {margin_rounded}")
    feasible = margin_rounded >= 0.0
    shortfall_m_s = round(max(0.0, required_rounded - corrected_rounded),
                          ROUND_DECIMALS)
    assumed_shortfall_m_s = round(max(0.0, required_rounded
                                      - round(achievable_assumed_dv_m_s,
                                              ROUND_DECIMALS)), ROUND_DECIMALS)

    results = [
        {"step": "corrected_propellant",
         "conversion_scale_ratio": round(scale, ROUND_DECIMALS),
         "lox_corrected_kg": round(lox_corrected_kg, ROUND_DECIMALS),
         "ch4_corrected_burned_kg": round(ch4_corrected_burned_kg,
                                          ROUND_DECIMALS),
         "corrected_usable_propellant_kg": round(propellant_streamwise_kg,
                                                 ROUND_DECIMALS)},
        {"step": "corrected_delta_v",
         "initial_mass_kg": round(m0_kg, ROUND_DECIMALS),
         "final_mass_kg": round(mf_kg, ROUND_DECIMALS),
         "corrected_delta_v_m_s": round(corrected_dv_ln_form, ROUND_DECIMALS)},
        {"step": "verdict",
         "required_dv_m_s": required_rounded,
         "margin_m_s": margin_rounded},
    ]

    return {
        "task_id": "task-0021-conversion-corrected-ascent",
        "inputs": {
            # THE PROVENANCE EDGES, on the artifact itself: each parent named
            # and its canonical hash as recomputed LIVE by this very run
            # (asserted equal to the pinned expectations above).
            "parent_output_hashes": {"task-0020": conv_hash,
                                     "task-0017": budget_hash,
                                     "task-0018": verdict_hash},
            "equilibrium_conversion_fraction": xi_eq,
            "assumed_conversion_fraction": xi_assumed,
            "reference_operating_point_K": t_ref_K,
            "reference_operating_pressure_bar": p_ref_bar,
            "conversion_source": "task-0020 summary at its reference operating "
                                 "point (consumed, not restated)",
            "propellant_assumed_kg": propellant_assumed_kg,
            "lox_available_from_parent_kg": lox_available_kg,
            "ch4_available_from_parent_kg": ch4_available_kg,
            "o_f_ratio": o_f_ratio,
            "dry_mass_kg": dry_mass_kg,
            "isp_s": isp_s,
            "g0_m_s2": g0_m_s2,
            "vehicle_constants_source": "task-0017 inputs (the corrected budget "
                                        "reuses the parent's fixed vehicle "
                                        "constants unchanged)",
            "required_dv_from_parent_m_s": required_dv_m_s,
            "achievable_assumed_dv_from_parent_m_s": achievable_assumed_dv_m_s,
            "linearity_basis": "task-0015's single-pass mass balance is linear "
                               "in conversion by construction: every product "
                               "stream is a fixed multiple of the CO2 converted",
            "round_decimals": ROUND_DECIMALS,
        },
        "results": results,
        "summary": {
            "feasible_at_equilibrium_conversion": feasible,
            # the size of the honest "no", as a plain number (the first numeric
            # summary field — the generic tamper-drill helper perturbs it to
            # prove Gate-2 rejection)
            "shortfall_m_s": shortfall_m_s,
            "shortfall_at_assumed_conversion_m_s": assumed_shortfall_m_s,
            "shortfall_growth_m_s": round(shortfall_m_s - assumed_shortfall_m_s,
                                          ROUND_DECIMALS),
            "corrected_delta_v_m_s": round(corrected_dv_ln_form, ROUND_DECIMALS),
            "conversion_scale_ratio": round(scale, ROUND_DECIMALS),
            "verdict_note": "at the thermodynamically honest equilibrium "
                            "conversion the ascent shortfall GROWS — the two "
                            "anchored negatives compound; an honest negative, "
                            "deepened on purpose: the constants are not tuned "
                            "to manufacture success",
            "self_proofs_checked": ["parent_hash_liveness_x3",
                                    "binding_reactant_invariance",
                                    "linear_scaling_crosscheck",
                                    "delta_v_crosscheck",
                                    "bounds_and_margin_closure"],
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
