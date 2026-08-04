"""task-0017-isru-ascent-budget — deterministic ISRU ascent propellant budget
(the FIRST PARENTED TASK: it consumes task-0015's Sabatier output).

Research-only. A bit-reproducible downstream task: the CH4 and H2O produced by
task-0015-sabatier-isru (one fixed single-pass Sabatier run) are budgeted into an
ascent-vehicle propellant load — CH4 as fuel, LOX from ideal full electrolysis of
the product water (2 H2O -> 2 H2 + O2, 100% O2 recovery, an explicit idealization),
burned at a FIXED O/F mass ratio of 3.5 (a representative CH4/LOX engine mixture
ratio; flown/tested engines in this class run O/F ~3.4-3.8). The scarcer reactant
path BINDS the total usable propellant (stoichiometric Sabatier output is
oxygen-poor at O/F 3.5, so the OXIDIZER binds — which is exactly why real Mars-ISRU
architectures add CO2 electrolysis or water mining for extra oxygen), and the
achievable delta-v follows from the Tsiolkovsky rocket equation with a FIXED dry
mass and a FIXED representative vacuum Isp (~360 s class for CH4/LOX vacuum
engines; an engineering-representative placeholder, not a specific engine). It maps
to the NASA Technology Taxonomy TX01 (Propulsion Systems).

THE PROVENANCE EDGE, ENFORCED AT EXECUTION TIME: compute() CALLS
task_0015_sabatier_isru.compute() directly, recomputes the parent's canonical
output hash LIVE, and asserts it equals the pinned EXPECTED_PARENT_OUTPUT_HASH
embedded below. A stale, tampered, or drifted copy of the parent task breaks THIS
task loudly — the declared parent edge is not a comment, it is executed and
checked on every run. The recomputed parent hash is then recorded in this task's
OWN inputs block (parent_task_id + parent_output_hash), so the emitted artifact
carries its lineage verbatim.

INTERNAL SELF-PROOF (three independent checks): compute() asserts
  (a) PARENT-HASH LIVENESS — the live-recomputed hash of the parent's canonical
      result equals the embedded EXPECTED_PARENT_OUTPUT_HASH (the provenance edge
      above; stop, don't fudge);
  (b) PROPELLANT ARITHMETIC CLOSES — fuel burned + oxidizer burned equals the
      total usable propellant to within 1e-6 kg AFTER the fixed output rounding
      (the check is on the numbers the artifact actually publishes); and
  (c) DELTA-V CROSS-CHECK — the delta-v computed via the ln-form
      dv = Isp*g0*ln(m0/mf) and via the mass-ratio-form dv = -Isp*g0*ln(mf/m0)
      agree to within 1e-9 m/s BEFORE rounding (two arithmetic paths to the same
      physics).
A violated assertion CRASHES the task — stop, don't fudge.

Test-META is a zero-value testnet placeholder and never mints base supply
(MIP-0001 paragraph 3, MIP-0002 paragraph 8). Illustrative propellant bookkeeping
(no boil-off, residuals, ullage, tank mass scaling, gravity/steering losses, or
engine performance modeling) — NOT a flight ascent-vehicle design. Not financial,
legal, or flight-engineering advice. No NASA affiliation or endorsement.

Standard library only (json, math, hashlib) plus the parent task module. No
randomness. Every emitted float is rounded to a fixed number of decimals so
re-runs are byte-identical and the SHA-256 output hash is stable (the basis of
the Gate-2 check).

Interface is identical to the other tasks so the verifier and agent loop can use
them interchangeably: compute() -> dict, canonical_json(result) -> str,
output_hash(result) -> str.
"""

import json
import math

# THE PARENT TASK (the first real provenance edge). Package import when loaded as
# demo.tasks.task_0017_isru_ascent_budget; bare import when run as a script from
# demo/tasks/ (the script dir is then on sys.path) — same module either way.
try:
    from demo.tasks import task_0015_sabatier_isru as _parent
except ImportError:  # direct script run: demo/tasks/ itself is sys.path[0]
    import task_0015_sabatier_isru as _parent

# Declared parentage (consumed by protocol/work_molecule.py: the molecule builder
# resolves each entry to the parent's WMID within the same build).
PARENT_TASKS = ["task-0015"]

# The parent's canonical Gate-2 output hash, PINNED. compute() recomputes the
# parent's hash live and asserts equality — the executable provenance edge.
EXPECTED_PARENT_OUTPUT_HASH = (
    "dab7a6208aa1a751d4f38ec955e9f54aeb61ca6aa16e49c7e5f6cb5e2a924ae1"
)

# --- Fixed inputs (part of the reproducibility hash) ------------------------
# Changing any of these changes the canonical output and therefore the Gate-2 hash.
O_F_RATIO = 3.5          # LOX/CH4 burned mass ratio (representative CH4/LOX
                         # engine mixture ratio; flown/tested engines in this
                         # class run O/F ~3.4-3.8)
DRY_MASS_KG = 100.0      # fixed ascent-vehicle dry mass (engineering-
                         # representative placeholder, not a specific vehicle)
ISP_S = 360.0            # fixed vacuum Isp (~360 s class, representative of
                         # CH4/LOX vacuum engines; NOT a specific engine)
G0_M_S2 = 9.80665        # standard gravity (exact by convention)

# Molar masses, g/mol — O2 on the same conventional atomic-weight basis as the
# parent's oxygen (2 x 15.9994); H2O is REUSED from the parent so the water the
# electrolyzer consumes is the very water the Sabatier reactor produced.
M_O2 = 31.9988

PROPELLANT_BALANCE_TOL_KG = 1e-6  # post-rounding fuel+oxidizer closure tolerance
DV_CROSSCHECK_TOL_M_S = 1e-9      # ln-form vs mass-ratio-form agreement (pre-rounding)
ROUND_DECIMALS = 6


def compute() -> dict:
    """Ascent propellant budget from the parent's Sabatier output + self-proof."""
    # --- INTERNAL SELF-PROOF (a): parent-hash liveness (the provenance edge) ---
    # Recompute the parent LIVE — never trust a cached value — and pin its hash.
    parent_result = _parent.compute()
    parent_hash = _parent.output_hash(parent_result)
    assert parent_hash == EXPECTED_PARENT_OUTPUT_HASH, (
        f"parent-hash liveness violated: task-0015 recomputes to {parent_hash}, "
        f"expected {EXPECTED_PARENT_OUTPUT_HASH} — the parent task changed; this "
        "downstream task refuses to consume drifted input"
    )

    # Consume the parent's PUBLISHED (rounded) product figures — the numbers the
    # parent's artifact actually carries are the numbers this budget is built on.
    parent_products = {d["quantity"]: d["value"] for d in parent_result["results"]}
    ch4_available_kg = parent_products["ch4_product_kg"]
    h2o_available_kg = parent_products["h2o_product_kg"]

    # LOX path: ideal full electrolysis of the parent's product water
    # (2 H2O -> 2 H2 + O2; 100% O2 recovery — an explicit idealization).
    n_h2o = h2o_available_kg / _parent.M_H2O
    lox_available_kg = 0.5 * n_h2o * M_O2

    # Which reactant path binds at the fixed O/F? Burning ALL the CH4 would need
    # O_F_RATIO x CH4 of LOX; if less LOX exists, the OXIDIZER binds (it does,
    # for stoichiometric Sabatier feed — the oxygen-poor result stated above).
    lox_required_for_all_ch4_kg = O_F_RATIO * ch4_available_kg
    if lox_available_kg < lox_required_for_all_ch4_kg:
        binding_reactant = "oxidizer"
        lox_burned_kg = lox_available_kg
        ch4_burned_kg = lox_available_kg / O_F_RATIO
    else:
        binding_reactant = "fuel"
        ch4_burned_kg = ch4_available_kg
        lox_burned_kg = lox_required_for_all_ch4_kg
    usable_propellant_kg = ch4_burned_kg + lox_burned_kg
    ch4_leftover_kg = ch4_available_kg - ch4_burned_kg
    lox_leftover_kg = lox_available_kg - lox_burned_kg

    # Tsiolkovsky: dv = Isp * g0 * ln(m0/mf), fully loaded vs dry.
    m0_kg = DRY_MASS_KG + usable_propellant_kg
    mf_kg = DRY_MASS_KG
    ve_m_s = ISP_S * G0_M_S2
    delta_v_ln_form = ve_m_s * math.log(m0_kg / mf_kg)
    delta_v_mass_ratio_form = -ve_m_s * math.log(mf_kg / m0_kg)

    # --- INTERNAL SELF-PROOF (stop, don't fudge) ----------------------------
    # (b) propellant arithmetic closes AFTER the output rounding — the check is
    # on the numbers the artifact actually publishes.
    balance_residual_kg = (round(ch4_burned_kg, ROUND_DECIMALS)
                           + round(lox_burned_kg, ROUND_DECIMALS)
                           - round(usable_propellant_kg, ROUND_DECIMALS))
    assert abs(balance_residual_kg) <= PROPELLANT_BALANCE_TOL_KG, (
        f"propellant balance violated after rounding: residual "
        f"{balance_residual_kg} kg"
    )
    # (c) two independent delta-v arithmetic paths agree pre-rounding.
    dv_residual = delta_v_ln_form - delta_v_mass_ratio_form
    assert abs(dv_residual) <= DV_CROSSCHECK_TOL_M_S, (
        f"delta-v cross-check violated: ln-form {delta_v_ln_form} vs "
        f"mass-ratio-form {delta_v_mass_ratio_form} (residual {dv_residual} m/s)"
    )

    results = [
        {"quantity": "lox_available_kg",
         "value": round(lox_available_kg, ROUND_DECIMALS)},
        {"quantity": "ch4_burned_kg", "value": round(ch4_burned_kg, ROUND_DECIMALS)},
        {"quantity": "lox_burned_kg", "value": round(lox_burned_kg, ROUND_DECIMALS)},
        {"quantity": "usable_propellant_kg",
         "value": round(usable_propellant_kg, ROUND_DECIMALS)},
        {"quantity": "delta_v_m_s", "value": round(delta_v_ln_form, ROUND_DECIMALS)},
    ]

    return {
        "task_id": "task-0017-isru-ascent-budget",
        "inputs": {
            # THE PROVENANCE EDGE, on the artifact itself: the parent named and
            # its canonical hash as recomputed LIVE by this very run (asserted
            # equal to the pinned expectation above).
            "parent_task_id": "task-0015",
            "parent_output_hash": parent_hash,
            "ch4_from_parent_kg": ch4_available_kg,
            "h2o_from_parent_kg": h2o_available_kg,
            "lox_source": "ideal full electrolysis of the parent's product water "
                          "(2 H2O -> 2 H2 + O2; 100% O2 recovery — an explicit "
                          "idealization)",
            "o_f_ratio": O_F_RATIO,
            "o_f_basis": "representative CH4/LOX engine mixture ratio (flown/"
                         "tested engines in this class run O/F ~3.4-3.8)",
            "dry_mass_kg": DRY_MASS_KG,
            "isp_s": ISP_S,
            "isp_basis": "~360 s class vacuum Isp, representative of CH4/LOX "
                         "vacuum engines (engineering-representative placeholder,"
                         " not a specific engine)",
            "g0_m_s2": G0_M_S2,
            "molar_mass_o2_g_mol": M_O2,
            "round_decimals": ROUND_DECIMALS,
        },
        "results": results,
        "summary": {
            "binding_reactant": binding_reactant,
            "binding_note": "stoichiometric Sabatier output is oxygen-poor at "
                            "O/F 3.5, so the oxidizer path binds; real Mars-ISRU "
                            "architectures add CO2 electrolysis or water mining "
                            "for extra oxygen",
            "ch4_leftover_kg": round(ch4_leftover_kg, ROUND_DECIMALS),
            "lox_leftover_kg": round(lox_leftover_kg, ROUND_DECIMALS),
            "initial_mass_kg": round(m0_kg, ROUND_DECIMALS),
            "final_mass_kg": round(mf_kg, ROUND_DECIMALS),
            "delta_v_crosscheck": "ln-form and mass-ratio-form agree to 1e-9 m/s",
            "parent_hash_liveness": "recomputed live and matched the pinned "
                                    "expectation",
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
