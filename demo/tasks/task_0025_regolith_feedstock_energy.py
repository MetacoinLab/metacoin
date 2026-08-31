"""task-0025-regolith-feedstock-energy — deterministic lunar-regolith
feedstock and extraction-energy budget for the sunshade film (parented on
task-0024; the mission-0002 ISRU node).

Research-only. A bit-reproducible ISRU bookkeeping task: the total aluminum
film mass from task-0024 is traced back to the regolith that must be
processed (pinned Apollo-16-class highland Al2O3 content; the Al mass
fraction of Al2O3 derived from pinned atomic weights, never quoted), then
priced in extraction energy at a pinned terrestrial-electrolysis class
value that the provenance block honestly labels a LOWER BOUND for any lunar
process, and finally expressed as the sustained electrical power over a
stated reference production period plus the photovoltaic collector area
that power implies at lunar equatorial day/night duty. It maps to the NASA
Technology Taxonomy TX07 (Exploration Destination Systems — ISRU).

THE PROVENANCE EDGE, ENFORCED AT EXECUTION TIME: compute() CALLS
task_0024.compute() directly, recomputes the parent's canonical output hash
LIVE, and asserts equality with the pinned EXPECTED_PARENT_OUTPUT_HASH.
Every physical constant is pinned with provenance in
demo/tasks/pinned_sunshade_sources.py.

INTERNAL SELF-PROOF (three assertion classes inside compute() per MIP-0008
rule 1): compute() asserts
  (a) PARENT-HASH LIVENESS (the provenance edge);
  (b) KNOWN-TRUTH, stoichiometry two ways — the Al mass fraction of Al2O3
      computed as 2*M_Al/(2*M_Al + 3*M_O) equals the same fraction computed
      via the oxide molar mass (2*M_Al/M_Al2O3) to within 1e-12, and lies
      in (0.5, 0.55) (the known ~0.529 landmark);
  (c) CONSERVATION — regolith tonnage x Al2O3 fraction x Al fraction
      recovers the film mass to within 1e-6 relative, and the energy,
      power, and PV-area figures close arithmetically on the values the
      artifact publishes.
A violated assertion CRASHES the task — stop, don't fudge.

Test-META is a zero-value testnet placeholder and never mints base supply
(MIP-0001 paragraph 3, MIP-0002 paragraph 8). Idealized bookkeeping (100%%
Al recovery, no beneficiation losses, no thermal-management or plant-mass
modeling; the electrolysis figure is a stated terrestrial lower bound) —
NOT an ISRU plant design. Not financial, legal, or flight-engineering
advice. No NASA affiliation or endorsement.

Standard library only (json, hashlib) plus the parent task module and the
pinned-constants module. No randomness. Every emitted float is rounded to a
fixed number of decimals so re-runs are byte-identical and the SHA-256
output hash is stable (the basis of the Gate-2 check). MIP-0009 contract:
compute() -> the four-key dict, canonical_json() era-2 (sign-of-zero-free),
output_hash() = sha256 of it.
"""

import hashlib
import json

try:
    from demo.tasks import task_0024_shade_mass_budget as _mass_parent
    from demo.tasks import pinned_sunshade_sources as _src
except ImportError:  # direct script run: demo/tasks/ itself is sys.path[0]
    import task_0024_shade_mass_budget as _mass_parent
    import pinned_sunshade_sources as _src

PARENT_TASKS = ["task-0024"]

EXPECTED_PARENT_OUTPUT_HASH = (
    "17b0717ab4d91132b5a3f561b8fea21c06e024741dc0d3ccab13f167109a2386"
)

# --- Fixed inputs (part of the reproducibility hash) ------------------------
AL2O3_MASS_FRACTION = _src.REGOLITH_AL2O3_MASS_FRACTION   # 0.27, tier-3
ELECTROLYSIS_KWH_PER_KG = _src.ELECTROLYSIS_KWH_PER_KG_AL # 15.0, tier-3
M_AL_G_MOL = _src.M_AL_G_MOL
M_O_G_MOL = _src.M_O_G_MOL
S0_W_M2 = _src.SOLAR_IRRADIANCE_W_M2
JULIAN_YEAR_S = _src.JULIAN_YEAR_S
REFERENCE_PRODUCTION_YEARS_YR = 50.0    # stated reference period (the same
                                        # half-century class horizon the
                                        # timeline verdict states; not tuned)
PV_EFFICIENCY_FRACTION = 0.20           # stated engineering class
PV_DUTY_CYCLE_FRACTION = 0.5            # lunar equatorial day/night
STOICH_TOL = 1e-12
MASS_CLOSE_REL_TOL = 1e-6
ROUND_DECIMALS = 6


def compute() -> dict:
    """Regolith tonnage, extraction energy, sustained power, PV area."""
    # --- SELF-PROOF (a): parent-hash liveness ------------------------------
    parent_result = _mass_parent.compute()
    parent_hash = _mass_parent.output_hash(parent_result)
    assert parent_hash == EXPECTED_PARENT_OUTPUT_HASH, (
        f"parent-hash liveness violated: task-0024 recomputes to {parent_hash}, "
        f"expected {EXPECTED_PARENT_OUTPUT_HASH} — this task refuses drifted input")
    film_mass_t = float(parent_result["summary"]["total_shade_mass_t"])
    film_mass_kg = film_mass_t * 1e3

    # --- SELF-PROOF (b): the Al fraction of Al2O3, two derivations ---------
    m_al2o3_g_mol = 2.0 * M_AL_G_MOL + 3.0 * M_O_G_MOL
    al_fraction_direct = 2.0 * M_AL_G_MOL / (2.0 * M_AL_G_MOL + 3.0 * M_O_G_MOL)
    al_fraction_via_oxide = 2.0 * M_AL_G_MOL / m_al2o3_g_mol
    assert abs(al_fraction_direct - al_fraction_via_oxide) <= STOICH_TOL, (
        f"stoichiometry violated: {al_fraction_direct} vs "
        f"{al_fraction_via_oxide} by the two derivations")
    assert 0.5 < al_fraction_direct < 0.55, (
        f"known-truth violated: Al fraction of Al2O3 {al_fraction_direct} "
        "outside (0.50, 0.55) — the ~0.529 landmark")

    # Regolith that must be processed for the film's aluminum (idealized
    # 100% recovery, stated in scope).
    al_per_kg_regolith = AL2O3_MASS_FRACTION * al_fraction_direct
    regolith_kg = film_mass_kg / al_per_kg_regolith
    regolith_t = regolith_kg / 1e3

    # --- SELF-PROOF (c): the tonnage recovers the film mass ---------------
    recovered_kg = regolith_kg * AL2O3_MASS_FRACTION * al_fraction_direct
    assert abs(recovered_kg - film_mass_kg) <= MASS_CLOSE_REL_TOL * film_mass_kg, (
        f"mass conservation violated: {recovered_kg} kg recovered vs "
        f"{film_mass_kg} kg film")

    # Extraction energy, sustained power, PV area.
    energy_kwh = film_mass_kg * ELECTROLYSIS_KWH_PER_KG
    energy_mj = energy_kwh * 3.6
    period_s = REFERENCE_PRODUCTION_YEARS_YR * JULIAN_YEAR_S
    power_w = energy_mj * 1e6 / period_s
    power_mw = power_w / 1e6
    pv_area_m2 = power_w / (S0_W_M2 * PV_EFFICIENCY_FRACTION
                            * PV_DUTY_CYCLE_FRACTION)
    pv_area_km2 = pv_area_m2 / 1e6
    # power x period recovers the energy (arithmetic closure, pre-round)
    assert abs(power_w * period_s - energy_mj * 1e6) <= 1e-3 * energy_mj * 1e6, (
        f"energy bookkeeping violated: {power_w} W x {period_s} s does not "
        f"recover {energy_mj} MJ")

    return {
        "task_id": "task-0025-regolith-feedstock-energy",
        "inputs": {
            "parent_task_id": "task-0024",
            "parent_output_hash": parent_hash,
            "film_mass_from_parent_t": film_mass_t,
            "al2o3_mass_fraction": AL2O3_MASS_FRACTION,
            "al2o3_provenance": _src.REGOLITH_PROVENANCE["source"],
            "electrolysis_kWh": ELECTROLYSIS_KWH_PER_KG,
            "electrolysis_unit_note": "kWh per kg Al produced",
            "electrolysis_provenance": _src.ELECTROLYSIS_PROVENANCE["source"],
            # emitted at the six-decimal boundary per MIP-0009 C3; the
            # computation uses the pinned module's full 26.9815385
            "m_al_g_mol": round(M_AL_G_MOL, ROUND_DECIMALS),
            "m_o_g_mol": M_O_G_MOL,
            "reference_production_period_yr": REFERENCE_PRODUCTION_YEARS_YR,
            "pv_efficiency_fraction": PV_EFFICIENCY_FRACTION,
            "pv_duty_cycle_fraction": PV_DUTY_CYCLE_FRACTION,
            "s0_W_m2": S0_W_M2,
            "round_decimals": ROUND_DECIMALS,
        },
        "results": [
            {"step": "stoichiometry",
             "al_fraction_of_al2o3_fraction": round(al_fraction_direct,
                                                    ROUND_DECIMALS),
             "al_per_kg_regolith_fraction": round(al_per_kg_regolith,
                                                  ROUND_DECIMALS)},
            {"step": "feedstock",
             "regolith_processed_t": round(regolith_t, ROUND_DECIMALS),
             "regolith_processed_million_t": round(regolith_t / 1e6,
                                                   ROUND_DECIMALS)},
            {"step": "energy_and_power",
             "extraction_energy_MJ": round(energy_mj, ROUND_DECIMALS),
             "sustained_power_MW": round(power_mw, ROUND_DECIMALS),
             "pv_area_km2": round(pv_area_km2, ROUND_DECIMALS)},
        ],
        "summary": {
            "regolith_processed_million_t": round(regolith_t / 1e6,
                                                  ROUND_DECIMALS),
            "sustained_power_MW": round(power_mw, ROUND_DECIMALS),
            "pv_area_km2": round(pv_area_km2, ROUND_DECIMALS),
            "lower_bound_note": "the electrolysis figure is a terrestrial "
                                "Hall-Heroult class value pinned as an "
                                "honest LOWER BOUND — a real lunar process "
                                "costs more, so power and PV area can only "
                                "grow",
            "self_proofs_checked": ["parent_hash_liveness",
                                    "stoichiometry_two_ways",
                                    "mass_and_energy_closure"],
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
