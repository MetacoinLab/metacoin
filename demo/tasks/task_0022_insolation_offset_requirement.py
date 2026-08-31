"""task-0022-insolation-offset-requirement — deterministic conversion of a
pinned radiative-forcing target into the required fractional reduction of
solar irradiance (the root node of the mission-0002 sunshade chain; the
fourth task born under MIP-0008 / MIP-0009 law).

Research-only. A bit-reproducible planetary-energy-balance task: to offset an
effective radiative forcing dF (W/m2), the top-of-atmosphere ABSORBED solar
flux must fall by dF, and with mean absorbed insolation S0(1-A)/4 the
required fractional reduction of the solar constant is

    f = 4 dF / (S0 (1 - A))

— DERIVED here from the pinned constants, never quoted from the sunshade
literature (whose classic ~1.7-1.8%% figure for doubled CO2 falls out of the
same algebra and is reproduced on this task's grid as a cross-check). The
REFERENCE target is the FETCHED AR6-assessed total anthropogenic ERF for
2019 vs 1750 (2.72 W/m2, tier-2 provenance); the doubled-CO2 row (3.93
W/m2) is tier-3 document-cited. Every constant is pinned with provenance in
demo/tasks/pinned_sunshade_sources.py. It maps to the NASA Technology
Taxonomy TX14 (Thermal Management Systems — planetary radiative-energy
balance mapped to the nearest taxonomy home).

INTERNAL SELF-PROOF (three assertion classes inside compute() per MIP-0008
rule 1): compute() asserts
  (a) KNOWN-TRUTH, two derivations — the fraction computed by the closed
      form above equals the fraction recovered by explicitly balancing
      absorbed power (f * S0/4 * (1-A) == dF) to within 1e-12 W/m2;
  (b) BOUNDS — every computed fraction lies in (0, 1), and the doubled-CO2
      row lands inside the classic 1.4-2.0%% class band (a literature
      cross-check on derived physics, not a tuned target);
  (c) MONOTONICITY — the required fraction is strictly increasing in dF.
A violated assertion CRASHES the task — stop, don't fudge.

Test-META is a zero-value testnet placeholder and never mints base supply
(MIP-0001 paragraph 3, MIP-0002 paragraph 8). First-order global-mean energy
balance (no climate dynamics, feedbacks, efficacy factors, or regional
structure) — NOT a climate model. Not financial, legal, or
flight-engineering advice. No NASA affiliation or endorsement.

Standard library only (json, hashlib) plus the pinned-constants module. No
randomness. Every emitted float is rounded to a fixed number of decimals so
re-runs are byte-identical and the SHA-256 output hash is stable (the basis
of the Gate-2 check). MIP-0009 contract: compute() -> the four-key dict,
canonical_json() era-2 (sign-of-zero-free), output_hash() = sha256 of it.
"""

import hashlib
import json

# THE PINNED SOURCES (data with provenance headers, not a task — the CEA
# pinning discipline, second use). Package import when loaded as
# demo.tasks.task_0022_...; bare import when run as a script.
try:
    from demo.tasks import pinned_sunshade_sources as _src
except ImportError:  # direct script run: demo/tasks/ itself is sys.path[0]
    import pinned_sunshade_sources as _src

PARENT_TASKS = []

# --- Fixed inputs (part of the reproducibility hash) ------------------------
# Changing any of these changes the canonical output and therefore the Gate-2
# hash. THE CONSTANTS ARE NOT TO BE TUNED TO MANUFACTURE ANY STORY.
S0_W_M2 = _src.SOLAR_IRRADIANCE_W_M2          # IAU B3 nominal, fetched-hashed
BOND_ALBEDO = _src.EARTH_BOND_ALBEDO          # document-cited (0.294)
ERF_TARGETS_W_M2 = (                          # the grid, smallest first
    _src.ERF_ANTHROPOGENIC_2019_W_M2,         # 2.72 — FETCHED (reference)
    _src.ERF_2XCO2_W_M2,                      # 3.93 — document-cited
)
REFERENCE_ERF_W_M2 = _src.ERF_ANTHROPOGENIC_2019_W_M2
CLASSIC_BAND_FRACTION = (0.014, 0.020)        # the 2xCO2 literature class
BALANCE_TOL_W_M2 = 1e-12
ROUND_DECIMALS = 6


def required_fraction(erf_w_m2: float) -> float:
    """f = 4 dF / (S0 (1 - A)) — the closed-form energy-balance inversion."""
    return 4.0 * erf_w_m2 / (S0_W_M2 * (1.0 - BOND_ALBEDO))


def compute() -> dict:
    """The required-insolation-reduction grid + the self-proofs."""
    rows = []
    fractions = []
    for erf in ERF_TARGETS_W_M2:              # bounded: two targets
        f = required_fraction(erf)
        # --- SELF-PROOF (a): explicit power balance recovers the target ----
        recovered_w_m2 = f * (S0_W_M2 / 4.0) * (1.0 - BOND_ALBEDO)
        assert abs(recovered_w_m2 - erf) <= BALANCE_TOL_W_M2, (
            f"energy balance violated: f={f} recovers {recovered_w_m2} W/m2 "
            f"for target {erf} W/m2")
        # --- SELF-PROOF (b): physical bounds -------------------------------
        assert 0.0 < f < 1.0, (
            f"bound violated: required fraction {f} outside (0, 1)")
        fractions.append(f)
        rows.append({
            "erf_target_W_m2": erf,
            "required_fraction": round(f, ROUND_DECIMALS),
            "blocked_flux_at_earth_W_m2": round(f * S0_W_M2, ROUND_DECIMALS),
        })
    # --- SELF-PROOF (b, continued): the derived 2xCO2 row lands in the
    # classic sunshade-literature band (a check on derived physics).
    f_2x = fractions[1]
    assert CLASSIC_BAND_FRACTION[0] <= f_2x <= CLASSIC_BAND_FRACTION[1], (
        f"known-truth violated: derived 2xCO2 fraction {f_2x} outside the "
        f"classic band {CLASSIC_BAND_FRACTION}")
    # --- SELF-PROOF (c): monotone in the forcing target --------------------
    assert fractions[1] > fractions[0], (
        "monotonicity violated: required fraction did not increase with ERF")

    f_ref = fractions[0]
    return {
        "task_id": "task-0022-insolation-offset-requirement",
        "inputs": {
            "s0_W_m2": S0_W_M2,
            "s0_provenance": _src.IAU_B3_PROVENANCE["source"],
            "bond_albedo_fraction": BOND_ALBEDO,
            "albedo_provenance": _src.BOND_ALBEDO_PROVENANCE["source"],
            "erf_targets_W_m2": list(ERF_TARGETS_W_M2),
            "reference_erf_W_m2": REFERENCE_ERF_W_M2,
            "erf_reference_provenance": _src.ERF_PROVENANCE["source"],
            "erf_2xco2_provenance": _src.ERF_2XCO2_PROVENANCE["source"],
            "classic_band_fraction": list(CLASSIC_BAND_FRACTION),
            "balance_formula": "f = 4*dF / (S0*(1 - A)) — absorbed-flux "
                               "balance, derived not quoted",
            "round_decimals": ROUND_DECIMALS,
        },
        "results": rows,
        "summary": {
            "reference_required_fraction": round(f_ref, ROUND_DECIMALS),
            "reference_required_percent": round(100.0 * f_ref,
                                                ROUND_DECIMALS),
            "doubled_co2_required_fraction": round(f_2x, ROUND_DECIMALS),
            "classic_result_note": "the derived doubled-CO2 row reproduces "
                                   "the classic ~1.6-1.8% sunshade class "
                                   "from pinned constants alone",
            "self_proofs_checked": ["power_balance_recovery",
                                    "bounds_and_classic_band",
                                    "erf_monotonicity"],
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
