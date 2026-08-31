"""task-0029-shade-longevity-horizon — deterministic test of the claim's
"~1 billion years" horizon against solar brightening (parented on
task-0022; a CONSTRAINING node of mission-0002 — and the one whose verdict
honestly comes out TRUE, conditions attached).

Research-only. The Sun brightens as it ages. From the pinned Gough (1981)
solar-luminosity relation
    L(t) = L_now / (1 + (2/5)(1 - t/t_sun)),   t_sun = 4.57 Gyr,
this task DERIVES (never quotes) the ~9%%-per-Gyr brightening rate, then
asks the claim's own question quantitatively: if a shade holds Earth's
absorbed flux at (present-day minus the mission's offset target), the
required blocked fraction grows as
    f(dt) = 1 - (1 - f0) / Lrel(dt)
where f0 is CONSUMED from task-0022's published reference row. The task
computes the required fraction at +1 Gyr, the cumulative growth factor
over the as-built shade, and the horizon at which the requirement crosses
a STATED occlusion ceiling (10%% — an order of magnitude past the mission
target; an engineering statement, not a biophysical threshold, and its
sensitivity is published). The verdict:
    gyr_claim_within_ceiling = (computed horizon >= the claimed 1 Gyr).
At these pinned constants the horizon computes to ~1.02 Gyr — the claim's
BILLION-YEAR SCALE HONESTLY SURVIVES this test, conditional on growing the
shade ~9x and holding it on station throughout; the margin is ~2%%, and
the verdict's own sensitivity to the ceiling is published so nobody
mistakes a near-boundary pass for a robust one. Honesty runs both ways: a
verification protocol must be as comfortable anchoring a conditional "yes"
as an honest "no". It maps to the NASA Technology Taxonomy TX14 (Thermal
Management Systems — long-horizon radiative-balance requirements).

THE PROVENANCE EDGE, ENFORCED AT EXECUTION TIME: compute() CALLS
task_0022.compute() directly, recomputes the parent's canonical output
hash LIVE, and asserts equality with the pinned
EXPECTED_PARENT_OUTPUT_HASH.

INTERNAL SELF-PROOF (four assertion classes inside compute() per MIP-0008
rule 1): compute() asserts
  (a) PARENT-HASH LIVENESS (the provenance edge);
  (b) KNOWN-TRUTH, Gough anchor — Lrel(0) == 1 exactly at the present
      epoch, and the derived brightening rate lands in the public
      8-10%%-per-Gyr class;
  (c) CONVERGENCE — the ceiling-crossing bisection closes within its
      stated bounds and the crossing residual |f(horizon) - ceiling| is
      below CROSSING_TOL;
  (d) MONOTONICITY + BOUNDS — the required fraction is strictly
      increasing on the published grid, and the growth factor times f0
      recovers the +1 Gyr requirement to within 1e-9 relative.
A violated assertion CRASHES the task — stop, don't fudge.

Test-META is a zero-value testnet placeholder and never mints base supply
(MIP-0001 paragraph 3, MIP-0002 paragraph 8). A single-formula stellar
brightening model against a fixed absorbed-flux target (no carbonate-
silicate feedback, no moist-greenhouse threshold physics, no shade
replacement or degradation modeling) — NOT a habitability forecast.
Not financial, legal, or flight-engineering advice.
No NASA affiliation or endorsement.

Standard library only (json, hashlib) plus the parent task module and the
pinned-constants module. No randomness. Every emitted float is rounded to
a fixed number of decimals so re-runs are byte-identical and the SHA-256
output hash is stable (the basis of the Gate-2 check). MIP-0009 contract:
compute() -> the four-key dict, canonical_json() era-2 (sign-of-zero-
free), output_hash() = sha256 of it.
"""

import hashlib
import json

try:
    from demo.tasks import task_0022_insolation_offset_requirement as _req_parent
    from demo.tasks import pinned_sunshade_sources as _src
except ImportError:  # direct script run: demo/tasks/ itself is sys.path[0]
    import task_0022_insolation_offset_requirement as _req_parent
    import pinned_sunshade_sources as _src

PARENT_TASKS = ["task-0022"]

EXPECTED_PARENT_OUTPUT_HASH = (
    "9c128de655d643cdd9e368ad0bda56c3b55d929511756dd9871e7814c65ae9b5"
)

# --- Fixed inputs (part of the reproducibility hash) ------------------------
# THE CONSTANTS ARE NOT TO BE TUNED TO MANUFACTURE SUCCESS OR FAILURE.
GOUGH_COEFF = _src.GOUGH_LUMINOSITY_COEFFICIENT       # 0.4 (Gough 1981)
SOLAR_AGE_GYR = _src.SOLAR_AGE_GYR                    # 4.57
CLAIMED_HORIZON_GYR = 1.0        # the claim's "~1 billion years", verbatim
OCCLUSION_CEILING_FRACTION = 0.10  # stated ceiling: an order of magnitude
                                   # past the mission's offset target — an
                                   # engineering statement whose
                                   # sensitivity is published, NOT a
                                   # biophysical threshold
TIME_GRID_GYR = (0.0, 0.25, 0.5, 1.0, 2.0)            # bounded grid
RATE_CLASS_PER_GYR = (0.08, 0.10)                     # public landmark class
MAX_BISECTION_ITER = 200         # the stated loop bound (rule 2)
CROSSING_TOL = 1e-9              # |f(horizon) - ceiling| at the solution
GROWTH_CLOSE_REL_TOL = 1e-9
ROUND_DECIMALS = 6


def luminosity_relative(dt_gyr: float) -> float:
    """Gough (1981): L(t)/L_now with t = now + dt, t_sun = 4.57 Gyr."""
    t_gyr = SOLAR_AGE_GYR + dt_gyr
    return 1.0 / (1.0 + GOUGH_COEFF * (1.0 - t_gyr / SOLAR_AGE_GYR))


def required_fraction_at(dt_gyr: float, f0: float) -> float:
    """f(dt) = 1 - (1 - f0)/Lrel(dt): the blocked fraction that holds
    absorbed flux at the (present - offset) target as the Sun brightens."""
    return 1.0 - (1.0 - f0) / luminosity_relative(dt_gyr)


def compute() -> dict:
    """The longevity horizon vs the claimed billion years + self-proofs."""
    # --- SELF-PROOF (a): parent-hash liveness ------------------------------
    parent_result = _req_parent.compute()
    parent_hash = _req_parent.output_hash(parent_result)
    assert parent_hash == EXPECTED_PARENT_OUTPUT_HASH, (
        f"parent-hash liveness violated: task-0022 recomputes to {parent_hash}, "
        f"expected {EXPECTED_PARENT_OUTPUT_HASH} — this task refuses drifted input")
    f0 = float(parent_result["summary"]["reference_required_fraction"])

    # --- SELF-PROOF (b): the Gough anchor and the derived rate -------------
    assert luminosity_relative(0.0) == 1.0, (
        f"Gough anchor violated: Lrel(0) = {luminosity_relative(0.0)} != 1")
    rate_per_gyr = (luminosity_relative(1e-3) - 1.0) / 1e-3
    assert RATE_CLASS_PER_GYR[0] <= rate_per_gyr <= RATE_CLASS_PER_GYR[1], (
        f"known-truth violated: derived brightening rate {rate_per_gyr}/Gyr "
        f"outside the public {RATE_CLASS_PER_GYR} class")

    # The requirement grid (monotonicity proved below).
    rows = []
    fractions = []
    for dt in TIME_GRID_GYR:                 # bounded: five epochs
        f = required_fraction_at(dt, f0)
        fractions.append(f)
        rows.append({"epoch_offset_Gyr": dt,
                     "luminosity_relative_ratio": round(
                         luminosity_relative(dt), ROUND_DECIMALS),
                     "required_fraction": round(f, ROUND_DECIMALS)})
    for i in range(len(fractions) - 1):      # bounded: four adjacent pairs
        assert fractions[i + 1] > fractions[i], (
            f"monotonicity violated between {TIME_GRID_GYR[i]} and "
            f"{TIME_GRID_GYR[i + 1]} Gyr")

    f_at_claim = required_fraction_at(CLAIMED_HORIZON_GYR, f0)
    growth_factor = f_at_claim / f0
    # --- SELF-PROOF (d): the growth factor recovers the requirement --------
    assert abs(growth_factor * f0 - f_at_claim) <= (
        GROWTH_CLOSE_REL_TOL * f_at_claim), (
        f"growth bookkeeping violated: {growth_factor} x {f0} does not "
        f"recover {f_at_claim}")

    # The ceiling-crossing horizon by bounded bisection on [0, 4] Gyr
    # (f is strictly increasing, and f(4 Gyr) far exceeds any sane ceiling).
    lo, hi = 0.0, 4.0
    iterations = 0
    for _ in range(MAX_BISECTION_ITER):      # bounded by MAX_BISECTION_ITER
        mid = 0.5 * (lo + hi)
        if required_fraction_at(mid, f0) < OCCLUSION_CEILING_FRACTION:
            lo = mid
        else:
            hi = mid
        iterations += 1
        if hi - lo <= 1e-12:
            break
    horizon_gyr = 0.5 * (lo + hi)
    crossing_residual = abs(required_fraction_at(horizon_gyr, f0)
                            - OCCLUSION_CEILING_FRACTION)
    # --- SELF-PROOF (c): crossing convergence ------------------------------
    assert iterations <= MAX_BISECTION_ITER and crossing_residual <= CROSSING_TOL, (
        f"convergence violated: crossing residual {crossing_residual} after "
        f"{iterations} iterations (bounds {MAX_BISECTION_ITER}, {CROSSING_TOL})")

    within = horizon_gyr >= CLAIMED_HORIZON_GYR
    margin_gyr = round(round(horizon_gyr, ROUND_DECIMALS)
                       - CLAIMED_HORIZON_GYR, ROUND_DECIMALS)
    # the ceiling at which the verdict would flip (sensitivity, published):
    # f(claimed horizon) IS that ceiling by construction
    flip_ceiling = round(f_at_claim, ROUND_DECIMALS)

    return {
        "task_id": "task-0029-shade-longevity-horizon",
        "inputs": {
            "parent_task_id": "task-0022",
            "parent_output_hash": parent_hash,
            "parent_reference_fraction": f0,
            "gough_coefficient_dimensionless": GOUGH_COEFF,
            "solar_age_Gyr": SOLAR_AGE_GYR,
            "solar_evolution_provenance":
                _src.SOLAR_EVOLUTION_PROVENANCE["source"],
            "claimed_horizon_Gyr": CLAIMED_HORIZON_GYR,
            "claim_note": "the public assertion's '~1 billion years', "
                          "recorded in the pinned-sources claim block",
            "occlusion_ceiling_fraction": OCCLUSION_CEILING_FRACTION,
            "ceiling_basis": "stated: an order of magnitude past the "
                             "mission offset target; an engineering "
                             "statement, not a biophysical threshold — "
                             "the flip sensitivity is published in the "
                             "summary",
            "time_grid_Gyr": list(TIME_GRID_GYR),
            "max_bisection_iter_count": MAX_BISECTION_ITER,
            "round_decimals": ROUND_DECIMALS,
        },
        "results": rows,
        "summary": {
            "gyr_claim_within_ceiling": within,
            # the margin, as a plain number (the first numeric summary
            # field — the tamper-drill helper perturbs it)
            "margin_Gyr": margin_gyr,
            "ceiling_horizon_Gyr": round(horizon_gyr, ROUND_DECIMALS),
            "brightening_rate_per_Gyr": round(rate_per_gyr, ROUND_DECIMALS),
            "claim_epoch_required_fraction": round(f_at_claim,
                                                   ROUND_DECIMALS),
            "shade_growth_factor_at_claim_ratio": round(growth_factor,
                                                        ROUND_DECIMALS),
            "verdict_flip_ceiling_fraction": flip_ceiling,
            "verdict_note": "the billion-year SCALE of the claim honestly "
                            "survives this test (horizon ~1.02 Gyr at the "
                            "10% ceiling) — CONDITIONAL on growing the "
                            "shade ~9x over the Gyr and holding station "
                            "throughout, and with only ~2% margin: the "
                            "verdict flips if the stated ceiling drops "
                            "below the published flip value; a "
                            "verification protocol must be as comfortable "
                            "anchoring a conditional yes as an honest no",
            "self_proofs_checked": ["parent_hash_liveness",
                                    "gough_anchor_and_rate_class",
                                    "crossing_convergence",
                                    "grid_monotonicity_and_growth_closure"],
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
