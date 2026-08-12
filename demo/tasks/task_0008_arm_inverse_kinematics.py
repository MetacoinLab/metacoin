"""task-0008-arm-inverse-kinematics — deterministic 2-link planar arm inverse kinematics.

Research-only. A bit-reproducible robotics task: the classic closed-form inverse kinematics
of a two-link planar manipulator, solving for the joint angles that place the end-effector
at each of a fixed set of target points, then verifying each solution by forward kinematics.
It maps to the NASA Technology Taxonomy TX04 (Robotic Systems) and is directly relevant to
in-space servicing/assembly manipulators (ISAM). The computation is deterministic and
reproducible by machine — exactly what MIP-0002 Gate 2 (independent re-run yields a
byte-identical hash) checks.

Test-META is a zero-value testnet placeholder and never mints base supply (MIP-0001
paragraph 3, MIP-0002 paragraph 8). Illustrative idealized kinematics (rigid massless links,
no joint limits, no dynamics, no collisions, elbow-down branch only) — NOT a flight design.
Not financial, legal, or flight-engineering advice. No NASA affiliation or endorsement.

Standard library only (math, json, hashlib). Closed-form (no iteration, no randomness);
unreachable targets are handled gracefully (flagged, with null angle fields) and never crash
or produce NaN. Every emitted float is rounded to a fixed number of decimals so re-runs are
byte-identical and the SHA-256 output hash is stable (the basis of the Gate-2 check).

Interface is identical to the other tasks so the verifier and agent loop can use them
interchangeably: compute() -> dict, canonical_json(result) -> str, output_hash(result) -> str.
"""

import json
import math

# --- Fixed inputs (part of the reproducibility hash) ------------------------
# Link lengths of the planar arm, in meters. Changing either changes the canonical
# output and therefore the Gate-2 hash. Reachable annulus: |L1-L2| <= |target| <= L1+L2.
L1_M = 1.0          # link 1 length, meters
L2_M = 0.8          # link 2 length, meters

# Fixed target end-effector points [x, y] in meters. All chosen to be reachable given
# L1 + L2 = 1.8 and |L1 - L2| = 0.2.
TARGETS = [
    [1.2, 0.5],
    [0.0, 1.5],
    [1.5, 0.0],
    [-0.6, 1.0],
]

# Number of decimal places every emitted float is rounded to. Fixed rounding is what makes
# the canonical JSON byte-stable across runs (and thus the SHA-256 reproducible).
ROUND_DECIMALS = 6


def compute() -> dict:
    """Solve closed-form inverse kinematics for each target and verify by forward kinematics.

    For each target [x, y] (all closed-form, elbow-down branch):
      r2 = x^2 + y^2
      cos_theta2 = (r2 - L1^2 - L2^2) / (2*L1*L2)
      reachable  iff -1 <= cos_theta2 <= 1
      theta2 = acos(cos_theta2)
      k1 = L1 + L2*cos(theta2),  k2 = L2*sin(theta2)
      theta1 = atan2(y, x) - atan2(k2, k1)
    Forward-kinematics check (proves correctness):
      fk_x = L1*cos(theta1) + L2*cos(theta1+theta2)
      fk_y = L1*sin(theta1) + L2*sin(theta1+theta2)
      position_error = sqrt((fk_x-x)^2 + (fk_y-y)^2)   (~0 for a correct solution)
    Angles are computed in radians and reported in degrees for readability. Unreachable
    targets are flagged reachable=false with null angle/fk fields (clamping is used only for
    the reachability test, never to fudge an out-of-range acos).
    """
    l1 = L1_M
    l2 = L2_M

    results = []
    reachable_count = 0
    max_position_error = 0.0
    theta2_deg_sum = 0.0

    for target in TARGETS:
        tx, ty = target[0], target[1]
        r2 = tx * tx + ty * ty
        cos_theta2 = (r2 - l1 * l1 - l2 * l2) / (2.0 * l1 * l2)
        reachable = (-1.0 <= cos_theta2 <= 1.0)

        if not reachable:
            # Handle gracefully: no angle/fk fields, never call acos out of range.
            results.append({
                "target_x_m": round(tx, ROUND_DECIMALS),
                "target_y_m": round(ty, ROUND_DECIMALS),
                "reachable": False,
                "theta1_deg": None,
                "theta2_deg": None,
                "fk_x_m": None,
                "fk_y_m": None,
                "position_error_m": None,
            })
            continue

        theta2 = math.acos(cos_theta2)
        k1 = l1 + l2 * math.cos(theta2)
        k2 = l2 * math.sin(theta2)
        theta1 = math.atan2(ty, tx) - math.atan2(k2, k1)

        # Forward-kinematics check.
        fk_x = l1 * math.cos(theta1) + l2 * math.cos(theta1 + theta2)
        fk_y = l1 * math.sin(theta1) + l2 * math.sin(theta1 + theta2)
        position_error = math.sqrt((fk_x - tx) ** 2 + (fk_y - ty) ** 2)

        theta1_deg = math.degrees(theta1)
        theta2_deg = math.degrees(theta2)

        reachable_count += 1
        max_position_error = max(max_position_error, position_error)
        theta2_deg_sum += theta2_deg

        results.append({
            "target_x_m": round(tx, ROUND_DECIMALS),
            "target_y_m": round(ty, ROUND_DECIMALS),
            "reachable": True,
            "theta1_deg": round(theta1_deg, ROUND_DECIMALS),
            "theta2_deg": round(theta2_deg, ROUND_DECIMALS),
            "fk_x_m": round(fk_x, ROUND_DECIMALS),
            "fk_y_m": round(fk_y, ROUND_DECIMALS),
            "position_error_m": round(position_error, ROUND_DECIMALS),
        })

    mean_theta2_deg = (theta2_deg_sum / reachable_count) if reachable_count else 0.0

    return {
        "task_id": "task-0008-arm-inverse-kinematics",
        "inputs": {
            "l1_m": L1_M,
            "l2_m": L2_M,
            "round_decimals": ROUND_DECIMALS,
            "targets": [[t[0], t[1]] for t in TARGETS],
        },
        "results": results,
        "summary": {
            "target_count": len(TARGETS),
            "reachable_count": reachable_count,
            "unreachable_count": len(TARGETS) - reachable_count,
            # Worst forward-kinematics error across reachable targets — ~0 if IK is correct.
            "max_position_error_m": round(max_position_error, ROUND_DECIMALS),
            "mean_theta2_deg": round(mean_theta2_deg, ROUND_DECIMALS),
        },
    }


# THE NEGATIVE-ZERO CANONICAL RULE (hash-era 2): -0.0 vs 0.0 is a platform
# artifact of last-ulp libm cancellation with no semantic content — the
# 2026-08 macOS incident proved a single sign-of-zero bit was the ONLY
# cross-platform divergence in this task's output. Canonical artifacts are
# sign-of-zero-free by rule; the hash-era transition is anchored on-chain
# (task_hash_era_recorded), and era-1 values remain re-derivable at their
# recorded commits. Same rule as every protocol serializer.
def _sign_safe_zero(obj):
    if isinstance(obj, float):
        return 0.0 if obj == 0.0 else obj
    if isinstance(obj, dict):
        return {k: _sign_safe_zero(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sign_safe_zero(v) for v in obj]
    return obj


def canonical_json(result: dict) -> str:
    """Serialize the result deterministically.

    sort_keys=True, fixed compact separators, and ensure_ascii=True make the output
    byte-stable across runs and platforms (assuming identical rounded float values).
    """
    return json.dumps(
        _sign_safe_zero(result),
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
