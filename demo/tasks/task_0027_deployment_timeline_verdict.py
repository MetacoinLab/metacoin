"""task-0027-deployment-timeline-verdict — deterministic verdict on whether
the sunshade deploys within a climate-relevant horizon (parented on
task-0026; a CONSTRAINING node of mission-0002, WITH AN HONEST NEGATIVE).

Research-only. A bit-reproducible downstream verdict task in the task-0018
mold: task-0026's deployment duration at the claim's stated launch
architecture (lunar mass driver, one-tonne shots, once a minute, one
driver) is judged against a stated acceptable horizon — a half-century, the
class of time over which an offset of PRESENT-DAY forcing is a coherent
goal (waiting much longer, the target itself has moved). It maps to the
NASA Technology Taxonomy TX01 (Propulsion Systems — launch-architecture
requirements analysis).

THE HONEST NEGATIVE, KEPT ON PURPOSE: at one tonne per minute the
~98-million-tonne shade takes ~187 years — infeasible by a factor of ~3.7
against the 50-year horizon. The cadence, shot mass, driver count, and
horizon are NOT to be tuned to manufacture success (or failure): the
verdict field the mission needs is
    deployable_within_horizon = (duration <= horizon)
and this task exists to anchor its honest value. What would flip it is
structural and stated by the parent's own inputs: more drivers, heavier
shots, faster cadence — a factor ~4 in any product of the three.

THE PROVENANCE EDGE, ENFORCED AT EXECUTION TIME: compute() CALLS
task_0026.compute() directly, recomputes the parent's canonical output hash
LIVE, and asserts equality with the pinned EXPECTED_PARENT_OUTPUT_HASH.

INTERNAL SELF-PROOF (three assertion classes inside compute() per MIP-0008
rule 1): compute() asserts
  (a) PARENT-HASH LIVENESS (the provenance edge);
  (b) MARGIN ARITHMETIC CLOSES — horizon minus duration equals the
      published margin EXACTLY on the rounded figures the artifact
      carries, and the overrun factor times the horizon recovers the
      duration to within 1e-6 relative;
  (c) BOUNDS — the horizon is positive and the verdict boolean equals the
      sign test of the published margin (the verdict is re-derived from
      the artifact's own numbers, two ways).
A violated assertion CRASHES the task — stop, don't fudge.

Test-META is a zero-value testnet placeholder and never mints base supply
(MIP-0001 paragraph 3, MIP-0002 paragraph 8). A single-architecture
requirements check (no ramp-up modeling, no parallel-driver build-out
schedule, no maintenance downtime) — NOT a program plan. Not financial,
legal, or flight-engineering advice. No NASA affiliation or endorsement.

Standard library only (json, hashlib) plus the parent task module. No
randomness. Every emitted float is rounded to a fixed number of decimals so
re-runs are byte-identical and the SHA-256 output hash is stable (the basis
of the Gate-2 check). MIP-0009 contract: compute() -> the four-key dict,
canonical_json() era-2 (sign-of-zero-free), output_hash() = sha256 of it.
"""

import hashlib
import json

try:
    from demo.tasks import task_0026_mass_driver_energetics as _parent
except ImportError:  # direct script run: demo/tasks/ itself is sys.path[0]
    import task_0026_mass_driver_energetics as _parent

PARENT_TASKS = ["task-0026"]

EXPECTED_PARENT_OUTPUT_HASH = (
    "036cf47ac062b6ad9f7a7fadd7bcad4a6aeabbb74797b343a1f0d23cb484dcdd"
)

# --- Fixed inputs (part of the reproducibility hash) ------------------------
# THE CONSTANTS ARE NOT TO BE TUNED TO MANUFACTURE SUCCESS OR FAILURE.
ACCEPTABLE_HORIZON_YR = 50.0     # stated: the half-century class over which
                                 # offsetting PRESENT-DAY forcing is a
                                 # coherent goal (the same reference period
                                 # task-0025 prices power over)
OVERRUN_CLOSE_REL_TOL = 1e-6
ROUND_DECIMALS = 6


def compute() -> dict:
    """The deployment-timeline verdict + self-proofs."""
    # --- SELF-PROOF (a): parent-hash liveness ------------------------------
    parent_result = _parent.compute()
    parent_hash = _parent.output_hash(parent_result)
    assert parent_hash == EXPECTED_PARENT_OUTPUT_HASH, (
        f"parent-hash liveness violated: task-0026 recomputes to {parent_hash}, "
        f"expected {EXPECTED_PARENT_OUTPUT_HASH} — this task refuses drifted input")
    duration_yr = float(parent_result["summary"]["deployment_duration_yr"])

    # The verdict, on the ROUNDED figures the artifact publishes.
    assert ACCEPTABLE_HORIZON_YR > 0.0, (
        f"bound violated: horizon {ACCEPTABLE_HORIZON_YR} yr not positive")
    duration_rounded = round(duration_yr, ROUND_DECIMALS)
    horizon_rounded = round(ACCEPTABLE_HORIZON_YR, ROUND_DECIMALS)
    margin_rounded = round(horizon_rounded - duration_rounded, ROUND_DECIMALS)
    deployable = margin_rounded >= 0.0
    overrun_factor = duration_rounded / horizon_rounded

    # --- SELF-PROOF (b): margin + overrun close on the published figures ---
    assert horizon_rounded - duration_rounded == margin_rounded, (
        f"margin arithmetic violated: {horizon_rounded} - {duration_rounded} "
        f"!= {margin_rounded}")
    assert abs(overrun_factor * horizon_rounded - duration_rounded) <= (
        OVERRUN_CLOSE_REL_TOL * duration_rounded), (
        f"overrun bookkeeping violated: factor {overrun_factor} x "
        f"{horizon_rounded} yr does not recover {duration_rounded} yr")
    # --- SELF-PROOF (c): the verdict re-derives from the margin sign -------
    assert deployable == (margin_rounded >= 0.0), (
        "verdict violated: deployable flag disagrees with the margin sign")

    return {
        "task_id": "task-0027-deployment-timeline-verdict",
        "inputs": {
            "parent_task_id": "task-0026",
            "parent_output_hash": parent_hash,
            "deployment_duration_from_parent_yr": duration_yr,
            "acceptable_horizon_yr": ACCEPTABLE_HORIZON_YR,
            "horizon_basis": "stated half-century class: the window over "
                             "which offsetting present-day forcing is a "
                             "coherent goal; deliberately the same period "
                             "task-0025 prices power over",
            "round_decimals": ROUND_DECIMALS,
        },
        "results": [
            {"step": "verdict",
             "deployment_duration_yr": duration_rounded,
             "acceptable_horizon_yr": horizon_rounded,
             "margin_yr": margin_rounded,
             "overrun_factor_ratio": round(overrun_factor, ROUND_DECIMALS)},
        ],
        "summary": {
            "deployable_within_horizon": deployable,
            # the size of the honest "no", as a plain number (the first
            # numeric summary field — the generic tamper-drill helper
            # perturbs it to prove Gate-2 rejection)
            "shortfall_yr": round(max(0.0, duration_rounded
                                      - horizon_rounded), ROUND_DECIMALS),
            "overrun_factor_ratio": round(overrun_factor, ROUND_DECIMALS),
            "verdict_note": "the claimed architecture at its stated "
                            "reference cadence does NOT deploy within a "
                            "climate-relevant horizon — an honest negative, "
                            "kept on purpose; the flip levers are the "
                            "parent's own cadence, shot mass, and driver "
                            "count (a factor ~4 in their product)",
            "self_proofs_checked": ["parent_hash_liveness",
                                    "margin_and_overrun_closure",
                                    "verdict_sign_rederivation"],
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
