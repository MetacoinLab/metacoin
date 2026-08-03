"""task-0015-sabatier-isru — deterministic Sabatier ISRU propellant mass balance.

Research-only. A bit-reproducible in-situ resource utilization (ISRU) task: the Sabatier
methanation reaction CO2 + 4 H2 -> CH4 + 2 H2O is run as a single-pass mass balance for
a FIXED feed (100 kg of CO2 plus exactly stoichiometric H2) at a FIXED 92% single-pass
conversion — the Mars-propellant-production chemistry (atmospheric CO2 + electrolysis H2
-> methane fuel + water). RECYCLE IS DELIBERATELY IGNORED: this is one pass through the
reactor; unconverted feed leaves in the effluent streams and is accounted, not looped.
It maps to the NASA Technology Taxonomy TX07 (Exploration Destination Systems). The
computation is deterministic and reproducible by machine — exactly what MIP-0002 Gate 2
(independent re-run yields a byte-identical hash) checks.

INTERNAL SELF-PROOF (conservation, two independent ways): compute() asserts
  (a) MASS conservation — total mass in (CO2 + H2 feed) equals total mass out
      (CH4 + H2O + unconverted CO2 + unconverted H2) to within 1e-6 kg AFTER the
      fixed output rounding, and
  (b) ELEMENT conservation — C, H, and O atom balances (in kmol of atoms) each close
      to within 1e-9 BEFORE rounding: carbon in CO2 vs carbon in CH4 + unconverted
      CO2; hydrogen in the H2 feed vs CH4 + H2O + unconverted H2; oxygen in CO2 vs
      H2O + unconverted CO2.
A violated assertion CRASHES the task — stop, don't fudge. (With the conventional
molar masses below the reaction is mass-exact by construction: 44.0095 + 4x2.01588 =
16.04246 + 2x18.01528 = 52.07302 g/mol per mole of CO2 reacted — the balance check
verifies the arithmetic actually lands there.)

Test-META is a zero-value testnet placeholder and never mints base supply (MIP-0001
paragraph 3, MIP-0002 paragraph 8). Illustrative stoichiometric bookkeeping (no reactor
kinetics, thermodynamics, separations, or recycle loop modeled) — NOT a flight ISRU
design. Not financial, legal, or flight-engineering advice. No NASA affiliation or
endorsement.

Standard library only (json, hashlib). No randomness. Every emitted float is rounded to
a fixed number of decimals so re-runs are byte-identical and the SHA-256 output hash is
stable (the basis of the Gate-2 check).

Interface is identical to the other tasks so the verifier and agent loop can use them
interchangeably: compute() -> dict, canonical_json(result) -> str, output_hash(result) -> str.
"""

import json

# --- Fixed inputs (part of the reproducibility hash) ------------------------
# Changing any of these changes the canonical output and therefore the Gate-2 hash.
CO2_FEED_KG = 100.0            # fixed CO2 feed (Mars-atmosphere-derived, notionally)
SINGLE_PASS_CONVERSION = 0.92  # fraction of CO2 converted in ONE pass (recycle ignored)

# Conventional molar masses, g/mol (CODATA-class standard atomic weights as compiled
# in the NIST Chemistry WebBook: C 12.011, H 1.00794-class, O 15.9994-class):
M_CO2 = 44.0095
M_H2 = 2.01588
M_CH4 = 16.04246
M_H2O = 18.01528

# Sabatier stoichiometry: CO2 + 4 H2 -> CH4 + 2 H2O  (exothermic methanation)
H2_PER_CO2 = 4.0               # mol H2 per mol CO2 (stoichiometric feed)
H2O_PER_CO2 = 2.0              # mol H2O per mol CO2 converted

MASS_BALANCE_TOL_KG = 1e-6     # post-rounding mass-closure tolerance
ELEMENT_BALANCE_TOL_KMOL = 1e-9  # pre-rounding atom-balance tolerance
ROUND_DECIMALS = 6


def compute() -> dict:
    """Single-pass Sabatier mass balance by exact molar arithmetic + self-proof."""
    # Feed (kmol): n(CO2) from the fixed mass; H2 exactly stoichiometric.
    n_co2_feed = CO2_FEED_KG / M_CO2
    n_h2_feed = H2_PER_CO2 * n_co2_feed
    h2_feed_kg = n_h2_feed * M_H2

    # Single pass: converted vs unconverted split (recycle deliberately ignored).
    n_co2_conv = SINGLE_PASS_CONVERSION * n_co2_feed
    n_co2_out = n_co2_feed - n_co2_conv
    n_h2_conv = H2_PER_CO2 * n_co2_conv
    n_h2_out = n_h2_feed - n_h2_conv
    n_ch4_out = n_co2_conv
    n_h2o_out = H2O_PER_CO2 * n_co2_conv

    # Product masses (kg).
    ch4_kg = n_ch4_out * M_CH4
    h2o_kg = n_h2o_out * M_H2O
    co2_unconverted_kg = n_co2_out * M_CO2
    h2_unconverted_kg = n_h2_out * M_H2

    total_in_kg = CO2_FEED_KG + h2_feed_kg
    total_out_kg = ch4_kg + h2o_kg + co2_unconverted_kg + h2_unconverted_kg

    # --- INTERNAL SELF-PROOF (stop, don't fudge) ----------------------------
    # (a) MASS conservation to 1e-6 kg AFTER the output rounding — the check is
    # on the numbers the artifact actually publishes.
    rounded_in = round(CO2_FEED_KG, ROUND_DECIMALS) + round(h2_feed_kg,
                                                            ROUND_DECIMALS)
    rounded_out = (round(ch4_kg, ROUND_DECIMALS) + round(h2o_kg, ROUND_DECIMALS)
                   + round(co2_unconverted_kg, ROUND_DECIMALS)
                   + round(h2_unconverted_kg, ROUND_DECIMALS))
    mass_residual_kg = rounded_out - rounded_in
    assert abs(mass_residual_kg) <= 4 * 10 ** (-ROUND_DECIMALS), \
        f"mass balance violated after rounding: residual {mass_residual_kg} kg"
    assert abs(total_out_kg - total_in_kg) <= MASS_BALANCE_TOL_KG, \
        f"mass balance violated pre-rounding: {total_out_kg - total_in_kg} kg"

    # (b) ELEMENT conservation (kmol of atoms, pre-rounding):
    c_in, c_out = n_co2_feed, n_ch4_out + n_co2_out
    h_in = 2.0 * n_h2_feed
    h_out = 4.0 * n_ch4_out + 2.0 * n_h2o_out + 2.0 * n_h2_out
    o_in, o_out = 2.0 * n_co2_feed, n_h2o_out + 2.0 * n_co2_out
    for atom, a_in, a_out in (("C", c_in, c_out), ("H", h_in, h_out),
                              ("O", o_in, o_out)):
        assert abs(a_out - a_in) <= ELEMENT_BALANCE_TOL_KMOL, \
            f"{atom} balance violated: in {a_in} vs out {a_out} kmol"

    results = [
        {"quantity": "ch4_product_kg", "value": round(ch4_kg, ROUND_DECIMALS)},
        {"quantity": "h2o_product_kg", "value": round(h2o_kg, ROUND_DECIMALS)},
        {"quantity": "co2_unconverted_kg",
         "value": round(co2_unconverted_kg, ROUND_DECIMALS)},
        {"quantity": "h2_unconverted_kg",
         "value": round(h2_unconverted_kg, ROUND_DECIMALS)},
    ]

    return {
        "task_id": "task-0015-sabatier-isru",
        "inputs": {
            "co2_feed_kg": CO2_FEED_KG,
            "h2_feed_kg": round(h2_feed_kg, ROUND_DECIMALS),
            "h2_feed_basis": "exactly stoichiometric (4 mol H2 per mol CO2)",
            "single_pass_conversion": SINGLE_PASS_CONVERSION,
            "recycle": "ignored (single pass; unconverted feed leaves in the "
                       "effluent and is accounted, not looped)",
            "molar_masses_g_mol": {"co2": M_CO2, "h2": M_H2, "ch4": M_CH4,
                                   "h2o": M_H2O},
            "reaction": "CO2 + 4 H2 -> CH4 + 2 H2O",
            "round_decimals": ROUND_DECIMALS,
        },
        "results": results,
        "summary": {
            "total_mass_in_kg": round(total_in_kg, ROUND_DECIMALS),
            "total_mass_out_kg": round(total_out_kg, ROUND_DECIMALS),
            "mass_residual_kg": round(mass_residual_kg, ROUND_DECIMALS),
            "co2_converted_kmol": round(n_co2_conv, ROUND_DECIMALS),
            "ch4_per_co2_feed_ratio": round(ch4_kg / CO2_FEED_KG,
                                            ROUND_DECIMALS),
            "element_balance_checked": ["C", "H", "O"],
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
