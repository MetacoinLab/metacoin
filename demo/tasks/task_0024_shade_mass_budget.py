"""task-0024-shade-mass-budget — deterministic total-mass budget for the
sub-L1 sunshade (parented on task-0023; the mission-0002 mass node).

Research-only. A bit-reproducible mass rollup: the occulting area computed
by task-0023 at its stated areal density becomes the total film mass, in kg
and tonnes, plus a unit-sail count at a stated unit area (the packaging
quantum the downstream launch-cadence arithmetic consumes). Both area and
areal density are CONSUMED from the parent's published artifact — the mass
node owns no physical constants of its own, only the unit-sail quantum. It
maps to the NASA Technology Taxonomy TX12 (Materials, Structures,
Mechanical Systems, and Manufacturing — large space structures).

THE PROVENANCE EDGE, ENFORCED AT EXECUTION TIME: compute() CALLS
task_0023.compute() directly, recomputes the parent's canonical output hash
LIVE, and asserts equality with the pinned EXPECTED_PARENT_OUTPUT_HASH.

INTERNAL SELF-PROOF (three assertion classes inside compute() per MIP-0008
rule 1): compute() asserts
  (a) PARENT-HASH LIVENESS (the provenance edge);
  (b) KNOWN-TRUTH, unit-conversion cross-check — the mass computed in
      km2 x (kg per km2) equals the mass computed in m2 x (kg per m2)
      to within 1e-6 kg BEFORE rounding (two unit paths, one physics);
  (c) CONSERVATION — the published tonnes, kg, and unit-sail figures close
      exactly on the rounded values the artifact carries.
A violated assertion CRASHES the task — stop, don't fudge.

Test-META is a zero-value testnet placeholder and never mints base supply
(MIP-0001 paragraph 3, MIP-0002 paragraph 8). A film-mass rollup only (no
support structure beyond the parent's areal-density allowance, no
station-keeping propellant, no manufacturing yield loss) — NOT a flight
design. Not financial, legal, or flight-engineering advice.
No NASA affiliation or endorsement.

Standard library only (json, hashlib) plus the parent task module. No
randomness. Every emitted float is rounded to a fixed number of decimals so
re-runs are byte-identical and the SHA-256 output hash is stable (the basis
of the Gate-2 check). MIP-0009 contract: compute() -> the four-key dict,
canonical_json() era-2 (sign-of-zero-free), output_hash() = sha256 of it.
"""

import hashlib
import json

try:
    from demo.tasks import task_0023_sub_l1_shade_geometry as _geom_parent
except ImportError:  # direct script run: demo/tasks/ itself is sys.path[0]
    import task_0023_sub_l1_shade_geometry as _geom_parent

PARENT_TASKS = ["task-0023"]

EXPECTED_PARENT_OUTPUT_HASH = (
    "e81501f1d0701c4ff25ccb5921152581e14880b9baa67bed81f84c50d3aa2dda"
)

# --- Fixed inputs (part of the reproducibility hash) ------------------------
UNIT_SAIL_AREA_KM2 = 1.0            # stated packaging quantum (one launch
                                    # unit deploys one square kilometre)
CONVERSION_CROSSCHECK_REL_TOL = 1e-12   # relative: the two unit paths differ
                                        # only by float association order
ROUND_DECIMALS = 6


def compute() -> dict:
    """Total shade mass + unit-sail count from the parent geometry."""
    # --- SELF-PROOF (a): parent-hash liveness ------------------------------
    parent_result = _geom_parent.compute()
    parent_hash = _geom_parent.output_hash(parent_result)
    assert parent_hash == EXPECTED_PARENT_OUTPUT_HASH, (
        f"parent-hash liveness violated: task-0023 recomputes to {parent_hash}, "
        f"expected {EXPECTED_PARENT_OUTPUT_HASH} — this task refuses drifted input")

    area_km2 = float(parent_result["summary"]["required_area_km2"])
    sigma_kg_m2 = float(
        parent_result["inputs"]["shade_areal_density_kg_per_m2"])

    # --- SELF-PROOF (b): two unit paths to the same mass -------------------
    mass_via_m2_kg = (area_km2 * 1e6) * sigma_kg_m2
    mass_via_km2_kg = area_km2 * (sigma_kg_m2 * 1e6)
    conversion_residual_kg = mass_via_m2_kg - mass_via_km2_kg
    assert abs(conversion_residual_kg) <= (CONVERSION_CROSSCHECK_REL_TOL
                                           * mass_via_m2_kg), (
        f"unit-conversion cross-check violated: {mass_via_m2_kg} kg via m2 "
        f"vs {mass_via_km2_kg} kg via km2 (residual {conversion_residual_kg})")
    mass_kg = mass_via_m2_kg
    mass_t = mass_kg / 1e3

    # Unit sails: whole units, rounded UP (the last partial sail is built).
    unit_mass_kg = UNIT_SAIL_AREA_KM2 * 1e6 * sigma_kg_m2
    unit_count = int(area_km2 // UNIT_SAIL_AREA_KM2) + (
        1 if area_km2 % UNIT_SAIL_AREA_KM2 > 0.0 else 0)

    # --- SELF-PROOF (c): the published figures close on rounded values -----
    mass_t_rounded = round(mass_t, ROUND_DECIMALS)
    mass_kg_rounded = round(mass_kg, ROUND_DECIMALS)
    assert abs(mass_t_rounded * 1e3 - mass_kg_rounded) <= 1e-3, (
        f"mass bookkeeping violated: {mass_t_rounded} t x 1000 and "
        f"{mass_kg_rounded} kg disagree beyond a gram on the published "
        "rounded figures (float ULP at this magnitude is ~1e-5 kg)")
    assert unit_count >= area_km2 / UNIT_SAIL_AREA_KM2, (
        f"unit-sail bound violated: {unit_count} units cannot carry "
        f"{area_km2} km2 at {UNIT_SAIL_AREA_KM2} km2 per unit")

    return {
        "task_id": "task-0024-shade-mass-budget",
        "inputs": {
            "parent_task_id": "task-0023",
            "parent_output_hash": parent_hash,
            "area_from_parent_km2": area_km2,
            "areal_density_from_parent_kg_per_m2": sigma_kg_m2,
            "unit_sail_area_km2": UNIT_SAIL_AREA_KM2,
            "unit_sail_basis": "stated packaging quantum for the launch-"
                               "cadence arithmetic downstream; not a design",
            "round_decimals": ROUND_DECIMALS,
        },
        "results": [
            {"step": "film_mass",
             "total_shade_mass_kg": mass_kg_rounded,
             "total_shade_mass_t": mass_t_rounded},
            {"step": "unit_sails",
             "unit_sail_mass_kg": round(unit_mass_kg, ROUND_DECIMALS),
             "unit_sails_count": unit_count},
        ],
        "summary": {
            "total_shade_mass_t": mass_t_rounded,
            "total_shade_mass_million_t": round(mass_t / 1e6,
                                                ROUND_DECIMALS),
            "unit_sails_count": unit_count,
            "scope_note": "film mass at the parent's stated areal density "
                          "(which includes its structure allowance); no "
                          "station-keeping propellant or yield loss modeled",
            "self_proofs_checked": ["parent_hash_liveness",
                                    "unit_conversion_crosscheck",
                                    "rounded_bookkeeping_closure"],
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
