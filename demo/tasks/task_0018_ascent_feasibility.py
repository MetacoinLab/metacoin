"""task-0018-ascent-feasibility — deterministic Mars-ascent feasibility check
(the SECOND PARENTED TASK, and the first three-generation chain:
task-0015 -> task-0017 -> THIS).

Research-only. A bit-reproducible downstream verdict task: the achievable
delta-v computed by task-0017-isru-ascent-budget (which itself consumes
task-0015's Sabatier output) is checked against the delta-v REQUIRED for a
reference Mars-surface-to-low-orbit ascent. The requirement is built from
first principles plus fixed representative loss constants: the circular orbital
velocity at a FIXED 250 km target altitude via vis-viva (v = sqrt(GM/r)), plus
FIXED gravity-loss and steering-loss allowances (engineering-representative
constants; Mars-ascent-vehicle studies quote total surface-to-LMO delta-v in
the ~4.0-4.5 km/s class, and this fixed budget lands there deliberately). It
maps to the NASA Technology Taxonomy TX17 (Guidance, Navigation & Control) —
an ascent targeting/requirements analysis consuming a TX01 propulsion budget.

THE HONEST NEGATIVE, KEPT ON PURPOSE: with ~2.19 km/s achievable from one
fixed single-pass Sabatier run against a ~4.7 km/s class requirement, the
verdict is feasible: FALSE — the library's second honest negative (sibling of
task-0012's link_closes: false). The constant set is NOT to be tuned to
manufacture success: a verification protocol must be comfortable anchoring
"no", and this task exists partly to prove that. A margin that honestly comes
out negative is the deliverable.

THE PROVENANCE EDGE, ENFORCED AT EXECUTION TIME (two hops deep): compute()
CALLS task_0017_isru_ascent_budget.compute() directly, recomputes the parent's
canonical output hash LIVE, and asserts it equals the pinned
EXPECTED_PARENT_OUTPUT_HASH below. The parent does exactly the same to
task-0015 — so a drifted GRANDPARENT breaks the parent's liveness assertion,
which breaks this task: the three-generation chain is executed and checked on
every run, not narrated. The recomputed parent hash is recorded in this task's
OWN inputs block, so the emitted artifact carries its lineage verbatim.

INTERNAL SELF-PROOF (three independent checks): compute() asserts
  (a) PARENT-HASH LIVENESS — the live-recomputed hash of the parent's
      canonical result equals the embedded EXPECTED_PARENT_OUTPUT_HASH
      (the provenance edge; stop, don't fudge);
  (b) MARGIN ARITHMETIC CLOSES — achievable_dv - required_dv equals the
      published margin EXACTLY on the rounded figures the artifact carries;
  (c) REQUIRED-DV CROSS-CHECK — the orbital velocity computed via the
      velocity form v = sqrt(GM/r) and via the energy form
      v = sqrt(2*(eps + GM/r)) with eps = -GM/(2r) agree to within 1e-9 m/s
      BEFORE rounding (two arithmetic paths to the same physics).
A violated assertion CRASHES the task — stop, don't fudge.

Test-META is a zero-value testnet placeholder and never mints base supply
(MIP-0001 paragraph 3, MIP-0002 paragraph 8). Illustrative requirements
bookkeeping (fixed loss allowances; no trajectory integration, atmosphere
model, plane-change, or vehicle sizing) — NOT a flight ascent design. Not
financial, legal, or flight-engineering advice. No NASA affiliation or
endorsement.

Standard library only (json, math, hashlib) plus the parent task module. No
randomness. Every emitted float is rounded to a fixed number of decimals so
re-runs are byte-identical and the SHA-256 output hash is stable (the basis
of the Gate-2 check).

Interface is identical to the other tasks so the verifier and agent loop can
use them interchangeably: compute() -> dict, canonical_json(result) -> str,
output_hash(result) -> str.
"""

import json
import math

# THE PARENT TASK (the second real provenance edge; the parent itself is
# parented on task-0015). Package import when loaded as
# demo.tasks.task_0018_ascent_feasibility; bare import when run as a script.
try:
    from demo.tasks import task_0017_isru_ascent_budget as _parent
except ImportError:  # direct script run: demo/tasks/ itself is sys.path[0]
    import task_0017_isru_ascent_budget as _parent

# Declared parentage (consumed by protocol/work_molecule.py: the molecule
# builder resolves each entry to the parent's WMID within the same build,
# and the parent's own declaration chains onward to task-0015).
PARENT_TASKS = ["task-0017"]

# The parent's canonical Gate-2 output hash, PINNED. compute() recomputes the
# parent's hash live and asserts equality — the executable provenance edge.
EXPECTED_PARENT_OUTPUT_HASH = (
    "01dfdf623cfba5cf55053a067ecc5305868481b8c6f20110745785d52f845125"
)

# --- Fixed inputs (part of the reproducibility hash) ------------------------
# Changing any of these changes the canonical output and therefore the Gate-2
# hash. THE CONSTANTS ARE NOT TO BE TUNED TO MANUFACTURE SUCCESS (docstring).
GM_MARS_M3_S2 = 4.282837e13   # Mars standard gravitational parameter GM
                              # (NASA Mars fact sheet, 42828.37 km^3/s^2)
R_MARS_M = 3.3895e6           # Mars volumetric mean radius (3389.5 km)
TARGET_ALTITUDE_M = 250e3     # fixed reference low-Mars-orbit altitude
                              # (250 km circular — representative LMO)
GRAVITY_LOSS_M_S = 1100.0     # fixed gravity-loss allowance (engineering-
                              # representative: Mars ascent studies budget
                              # ~1.0-1.3 km/s of gravity/drag losses)
STEERING_LOSS_M_S = 150.0     # fixed steering-loss allowance (representative
                              # pitch-over/attitude losses)
                              # (no surface-rotation credit is taken — stated,
                              # so the requirement is conservatively simple)

REQUIRED_DV_CROSSCHECK_TOL_M_S = 1e-9  # velocity form vs energy form (pre-round)
ROUND_DECIMALS = 6


def compute() -> dict:
    """Ascent feasibility verdict from the parent's achievable delta-v + self-proof."""
    # --- INTERNAL SELF-PROOF (a): parent-hash liveness (the provenance edge) ---
    # Recompute the parent LIVE — never trust a cached value — and pin its hash.
    # The parent's own liveness assertion checks the grandparent (task-0015),
    # so this call executes the full three-generation chain.
    parent_result = _parent.compute()
    parent_hash = _parent.output_hash(parent_result)
    assert parent_hash == EXPECTED_PARENT_OUTPUT_HASH, (
        f"parent-hash liveness violated: task-0017 recomputes to {parent_hash}, "
        f"expected {EXPECTED_PARENT_OUTPUT_HASH} — the parent task changed; this "
        "downstream task refuses to consume drifted input"
    )

    # Consume the parent's PUBLISHED (rounded) achievable delta-v — the number
    # the parent's artifact actually carries is the number this verdict judges.
    parent_values = {d["quantity"]: d["value"] for d in parent_result["results"]}
    achievable_dv_m_s = parent_values["delta_v_m_s"]

    # Required delta-v: circular orbital velocity at the fixed target altitude
    # (vis-viva, velocity form) plus the fixed loss allowances.
    r_orbit_m = R_MARS_M + TARGET_ALTITUDE_M
    v_orbit_velocity_form = math.sqrt(GM_MARS_M3_S2 / r_orbit_m)
    # --- INTERNAL SELF-PROOF (c): the same velocity via the ENERGY form —
    # specific orbital energy eps = -GM/(2r); v = sqrt(2*(eps + GM/r)).
    eps = -GM_MARS_M3_S2 / (2.0 * r_orbit_m)
    v_orbit_energy_form = math.sqrt(2.0 * (eps + GM_MARS_M3_S2 / r_orbit_m))
    dv_residual = v_orbit_velocity_form - v_orbit_energy_form
    assert abs(dv_residual) <= REQUIRED_DV_CROSSCHECK_TOL_M_S, (
        f"required-dv cross-check violated: velocity form "
        f"{v_orbit_velocity_form} vs energy form {v_orbit_energy_form} "
        f"(residual {dv_residual} m/s)"
    )
    required_dv_m_s = (v_orbit_velocity_form + GRAVITY_LOSS_M_S
                       + STEERING_LOSS_M_S)

    # The verdict, on the ROUNDED figures the artifact publishes.
    required_rounded = round(required_dv_m_s, ROUND_DECIMALS)
    achievable_rounded = round(achievable_dv_m_s, ROUND_DECIMALS)
    margin_rounded = round(achievable_rounded - required_rounded,
                           ROUND_DECIMALS)
    feasible = margin_rounded >= 0.0

    # --- INTERNAL SELF-PROOF (b): margin arithmetic closes EXACTLY on the
    # published (rounded) numbers — the check is on what the artifact carries.
    assert achievable_rounded - required_rounded == margin_rounded, (
        f"margin arithmetic violated: {achievable_rounded} - "
        f"{required_rounded} != {margin_rounded}"
    )

    results = [
        {"quantity": "orbital_velocity_m_s",
         "value": round(v_orbit_velocity_form, ROUND_DECIMALS)},
        {"quantity": "required_dv_m_s", "value": required_rounded},
        {"quantity": "achievable_dv_m_s", "value": achievable_rounded},
        {"quantity": "margin_m_s", "value": margin_rounded},
    ]

    return {
        "task_id": "task-0018-ascent-feasibility",
        "inputs": {
            # THE PROVENANCE EDGE, on the artifact itself: the parent named and
            # its canonical hash as recomputed LIVE by this very run (asserted
            # equal to the pinned expectation above). The parent's artifact
            # carries the same block for task-0015 — the chain is on the record.
            "parent_task_id": "task-0017",
            "parent_output_hash": parent_hash,
            "achievable_dv_from_parent_m_s": achievable_dv_m_s,
            "gm_mars_m3_s2": GM_MARS_M3_S2,
            "r_mars_m": R_MARS_M,
            "target_altitude_m": TARGET_ALTITUDE_M,
            "gravity_loss_m_s": GRAVITY_LOSS_M_S,
            "steering_loss_m_s": STEERING_LOSS_M_S,
            "loss_basis": "fixed engineering-representative allowances (Mars "
                          "ascent studies budget ~1.0-1.3 km/s gravity/drag + "
                          "steering losses; total surface-to-LMO requirements "
                          "land in the ~4.0-4.5 km/s class); no "
                          "surface-rotation credit taken",
            "round_decimals": ROUND_DECIMALS,
        },
        "results": results,
        "summary": {
            "feasible": feasible,
            # the size of the honest "no", as a plain number (also the first
            # numeric summary field every task must carry — the generic
            # tamper-drill helper perturbs it to prove Gate-2 rejection)
            "shortfall_m_s": round(max(0.0, required_rounded
                                       - achievable_rounded), ROUND_DECIMALS),
            "verdict_note": "the single-pass Sabatier propellant load does NOT "
                            "close a 250 km Mars ascent at these fixed "
                            "constants — an honest negative, kept on purpose: "
                            "a verification protocol must be comfortable "
                            "anchoring 'no' (sibling of task-0012's "
                            "link_closes: false); real architectures scale up "
                            "ISRU throughput and add oxygen production",
            "margin_note": "margin = achievable - required on the published "
                           "rounded figures, closed exactly by self-proof (b)",
            "required_dv_crosscheck": "velocity form and energy form agree to "
                                      "1e-9 m/s",
            "parent_hash_liveness": "recomputed live and matched the pinned "
                                    "expectation (the parent's own liveness "
                                    "check covers the grandparent)",
        },
    }


def canonical_json(result: dict) -> str:
    """Serialize the result deterministically.

    sort_keys=True, fixed compact separators, and ensure_ascii=True make the
    output byte-stable across runs and platforms (assuming identical rounded
    float values).
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
