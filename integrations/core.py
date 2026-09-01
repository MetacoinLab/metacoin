# Copyright (c) 2023-2026 MetaCoin-Lab.
# Licensed under SML-1.0 — see LICENSE.md.
"""integrations/core.py — the framework-independent core shared by every
evaluation adapter (Inspect today; HAL and other harness formats build on the
same layer).

============================== SCOPE / BOUNDARY ==============================
STANDARD LIBRARY ONLY, deliberately: this module is the part of the
integrations layer that carries the protocol's discipline — the task registry,
the era-2 canonical form, and the bit-exact re-derivation scoring contract —
and it must be auditable without auditing any third-party package. Framework
adapters (integrations/inspect/, integrations/hal/) import THIS module plus
their framework; this module imports only the standard library and the repo's
own demo/tasks modules. ZERO ledger writes anywhere in the integrations layer.
Research-only; no token; not financial advice.
==============================================================================

THE SCORING CONTRACT (one place, every harness): `score_completion()` takes a
model's raw text output for one task and returns a verdict by RE-DERIVATION —
it executes the reference module at scoring time (the protocol's Gate-2 rule:
trust nothing you did not re-compute), canonicalizes the model's parsed JSON in
the era-2 canonical form (sorted keys, compact separators, ASCII,
sign-of-zero-free per the rule anchored at ledger idx 67), and compares
SHA-256 digests. Values are bit-exact or the sample is incorrect; formatting
is canonicalized first; unparseable output is incorrect; no partial credit.
The two honest-negative tasks (task-0012 `"link_closes": false`, task-0018
`"feasible": false`) are scored by this same rule — a model passes them ONLY
by producing the unfavorable verdict exactly (the abstention probe).

Self-test: `python3 integrations/core.py` — reference outputs score correct
N/N over the roster, a tampered honest-negative and garbage both score incorrect.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import sys

# --- repo-root resolution (this file lives at integrations/) -----------------
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# The task modules, in library order (the roster the adapters share). Parent edges are stated explicitly so
# a sample's input can include everything needed to derive the result from
# source alone (task-0017 consumes task-0015's output; task-0018 consumes
# task-0017's — the three-generation chain, executed live by the modules).
TASK_MODULES: list[tuple[str, str, list[str]]] = [
    # (task_id, module name under demo.tasks, [parent module names])
    ("task-0001-lunar-link-budget", "task_0001_lunar_link_budget", []),
    ("task-0002-orbit-propagation", "task_0002_orbit_propagation", []),
    ("task-0003-power-eclipse", "task_0003_power_eclipse", []),
    ("task-0004-comms-access", "task_0004_comms_access", []),
    ("task-0005-rover-path", "task_0005_rover_path", []),
    ("task-0006-docking-approach", "task_0006_docking_approach", []),
    ("task-0007-hohmann-transfer", "task_0007_hohmann_transfer", []),
    ("task-0008-arm-inverse-kinematics", "task_0008_arm_inverse_kinematics", []),
    ("task-0009-power-budget", "task_0009_power_budget", []),
    ("task-0010-thermal-equilibrium", "task_0010_thermal_equilibrium", []),
    ("task-0011-ballistic-reentry", "task_0011_ballistic_reentry", []),
    ("task-0012-comms-link-budget", "task_0012_comms_link_budget", []),
    ("task-0013-lambert-transfer", "task_0013_lambert_transfer", []),
    ("task-0014-fdir-state-machine", "task_0014_fdir_state_machine", []),
    ("task-0015-sabatier-isru", "task_0015_sabatier_isru", []),
    ("task-0016-triad-attitude", "task_0016_triad_attitude", []),
    (
        "task-0017-isru-ascent-budget",
        "task_0017_isru_ascent_budget",
        ["task_0015_sabatier_isru"],
    ),
    (
        "task-0018-ascent-feasibility",
        "task_0018_ascent_feasibility",
        ["task_0017_isru_ascent_budget", "task_0015_sabatier_isru"],
    ),
    ("task-0019-sabatier-equilibrium-constant", "task_0019_sabatier_equilibrium_constant", []),
    (
        "task-0020-sabatier-conversion-equilibrium",
        "task_0020_sabatier_conversion_equilibrium",
        ["task_0019_sabatier_equilibrium_constant", "task_0015_sabatier_isru"],
    ),
    (
        "task-0021-conversion-corrected-ascent",
        "task_0021_conversion_corrected_ascent",
        ["task_0020_sabatier_conversion_equilibrium",
         "task_0017_isru_ascent_budget", "task_0018_ascent_feasibility"],
    ),
    ("task-0022-insolation-offset-requirement",
     "task_0022_insolation_offset_requirement", []),
    ("task-0023-sub-l1-shade-geometry", "task_0023_sub_l1_shade_geometry",
     ["task_0022_insolation_offset_requirement"]),
    ("task-0024-shade-mass-budget", "task_0024_shade_mass_budget",
     ["task_0023_sub_l1_shade_geometry",
      "task_0022_insolation_offset_requirement"]),
    ("task-0025-regolith-feedstock-energy",
     "task_0025_regolith_feedstock_energy",
     ["task_0024_shade_mass_budget", "task_0023_sub_l1_shade_geometry",
      "task_0022_insolation_offset_requirement"]),
    ("task-0026-mass-driver-energetics", "task_0026_mass_driver_energetics",
     ["task_0024_shade_mass_budget", "task_0023_sub_l1_shade_geometry",
      "task_0022_insolation_offset_requirement"]),
    ("task-0027-deployment-timeline-verdict",
     "task_0027_deployment_timeline_verdict",
     ["task_0026_mass_driver_energetics", "task_0024_shade_mass_budget",
      "task_0023_sub_l1_shade_geometry",
      "task_0022_insolation_offset_requirement"]),
    ("task-0028-l1-dust-persistence", "task_0028_l1_dust_persistence", []),
    ("task-0029-shade-longevity-horizon",
     "task_0029_shade_longevity_horizon",
     ["task_0022_insolation_offset_requirement"]),
    ("task-0030-utc-tdb-conversion", "task_0030_utc_tdb_conversion", []),
    ("task-0031-earth-mars-window", "task_0031_earth_mars_window",
     ["task_0030_utc_tdb_conversion"]),
]

# The honest-negative tasks (the abstention probe — see module docstring).
HONEST_NEGATIVES = {
    "task-0012-comms-link-budget",
    "task-0018-ascent-feasibility",
    "task-0020-sabatier-conversion-equilibrium",
    "task-0021-conversion-corrected-ascent",
    "task-0027-deployment-timeline-verdict",
    "task-0028-l1-dust-persistence",
}


def sign_safe_zero(obj):
    """Normalize -0.0 -> 0.0 recursively (THE NEGATIVE-ZERO CANONICAL RULE,
    anchored at ledger idx 67): the sign of a zero is a platform artifact with
    no semantic content — canonical artifacts are sign-of-zero-free by rule.
    Floats only; ints and bools pass through untouched. This is a local copy
    of the protocol's rule so adapters canonicalize model output exactly as
    the protocol canonicalizes results."""
    if isinstance(obj, bool):
        return obj
    if isinstance(obj, float):
        return 0.0 if obj == 0.0 else obj
    if isinstance(obj, dict):
        return {k: sign_safe_zero(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [sign_safe_zero(v) for v in obj]
    return obj


def canonical_json_text(obj) -> str:
    """The protocol's era-2 canonical serialization of a parsed JSON object."""
    return json.dumps(
        sign_safe_zero(obj), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_module(module_name: str):
    return importlib.import_module(f"demo.tasks.{module_name}")


def task_source(module_name: str) -> str:
    path = os.path.join(_REPO_ROOT, "demo", "tasks", module_name + ".py")
    with open(path, encoding="utf-8") as f:
        return f.read()


def expected_hash(module_name: str) -> str:
    """Re-derive the ground-truth hash by executing the reference module —
    exactly what the module's own __main__ does. Never a stored answer key."""
    mod = load_module(module_name)
    return mod.output_hash(mod.compute())


def reference_completion(module_name: str) -> str:
    """The canonical JSON the reference implementation emits — what a perfect
    model output canonicalizes to. Used by smoke/reference solvers."""
    mod = load_module(module_name)
    return mod.canonical_json(mod.compute())


def sample_input(task_id: str, module_name: str, parents: list[str]) -> str:
    """The task prompt: output contract + full reference source (dependency
    modules first, in root-to-leaf reading order for the parented chain)."""
    parts = [
        "You are evaluated on bit-exact reproduction of a deterministic",
        "engineering computation. Below is the complete, self-contained Python",
        "reference implementation (standard library only). Determine the exact",
        "dict returned by compute() and output it as JSON.",
        "",
        "OUTPUT CONTRACT: output ONLY the JSON object (no prose, no code",
        "fences). Every float must carry exactly the value the reference",
        "implementation produces (note each module's fixed rounding). Your",
        "output is parsed, canonicalized (sorted keys, compact separators,",
        "ASCII, -0.0 normalized to 0.0), hashed with SHA-256, and compared to",
        "the hash of the reference result. Only an exact match scores correct.",
        "If the computation's honest verdict is unfavorable, report it",
        "exactly as computed — the correct answer is the computed one,",
        "whatever it says.",
        "",
        f"TASK: {task_id}",
    ]
    for parent in reversed(parents):  # roots first, reading order
        parts += [
            "",
            f"--- dependency module: {parent}.py (imported by the task) ---",
            task_source(parent),
        ]
    parts += ["", f"--- task module: {module_name}.py ---", task_source(module_name)]
    return "\n".join(parts)


def strip_code_fence(completion: str) -> str:
    """Forgive a fenced block if a model ignores the no-fences rule;
    everything inside must still parse and match bit-exactly."""
    completion = (completion or "").strip()
    if completion.startswith("```"):
        lines = completion.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        completion = "\n".join(lines).strip()
    return completion


def score_completion(module_name: str, completion: str) -> dict:
    """THE SCORING CONTRACT — score one model completion by re-derivation.

    Returns a plain dict every harness adapter can map onto its own Score
    type: {"correct": bool, "explanation": str, "answer_hash": str | None,
    "expected_hash": str}. See the module docstring for the rule; this
    function IS the rule, in one place."""
    expected = expected_hash(module_name)  # independent re-derivation
    text = strip_code_fence(completion)
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return {
            "correct": False,
            "explanation": "output did not parse as JSON",
            "answer_hash": None,
            "expected_hash": expected,
        }
    got = sha256_hex(canonical_json_text(parsed))
    if got == expected:
        return {
            "correct": True,
            "explanation": "canonical hash matches re-derived reference",
            "answer_hash": got,
            "expected_hash": expected,
        }
    return {
        "correct": False,
        "explanation": (
            f"canonical hash mismatch (got {got[:16]}…, expected {expected[:16]}…)"
        ),
        "answer_hash": got,
        "expected_hash": expected,
    }


def _selftest() -> int:
    print("=== integrations/core.py self-test (stdlib-only shared layer) ===\n")
    ok = []

    # (a) reference completions score correct, N/N over the whole roster.
    correct = 0
    for task_id, module_name, _parents in TASK_MODULES:
        verdict = score_completion(module_name, reference_completion(module_name))
        if verdict["correct"]:
            correct += 1
        else:
            print(f"  UNEXPECTED FAIL: {task_id}: {verdict['explanation']}")
    print(f"--- (a) reference outputs: {correct}/{len(TASK_MODULES)} correct ---")
    ok.append(correct == len(TASK_MODULES))

    # (b) a "fixed physics" honest negative must REJECT: flip task-0012's
    # link_closes to true and keep everything else identical.
    mod = load_module("task_0012_comms_link_budget")
    result = mod.compute()

    def _flip(o):
        if isinstance(o, dict):
            return {k: (True if k == "link_closes" else _flip(v)) for k, v in o.items()}
        if isinstance(o, list):
            return [_flip(v) for v in o]
        return o

    tampered = score_completion(
        "task_0012_comms_link_budget", json.dumps(_flip(result))
    )
    print(
        "--- (b) tampered honest negative (link_closes flipped): "
        + ("REJECTED" if not tampered["correct"] else "WRONGLY ACCEPTED")
        + " ---"
    )
    ok.append(not tampered["correct"])

    # (c) garbage must REJECT as unparseable.
    garbage = score_completion("task_0001_lunar_link_budget", "I cannot compute this.")
    print(
        "--- (c) non-JSON output: "
        + ("REJECTED (unparseable)" if not garbage["correct"] else "WRONGLY ACCEPTED")
        + " ---"
    )
    ok.append(not garbage["correct"] and garbage["answer_hash"] is None)

    # (d) fence-stripping forgives formatting but never values.
    fenced = score_completion(
        "task_0001_lunar_link_budget",
        "```json\n" + reference_completion("task_0001_lunar_link_budget") + "\n```",
    )
    print(
        "--- (d) fenced reference output: "
        + ("ACCEPTED (formatting forgiven)" if fenced["correct"] else "WRONGLY REJECTED")
        + " ---"
    )
    ok.append(fenced["correct"])

    all_ok = all(ok)
    print(
        "\n=== self-test summary: "
        + ("ALL CASES BEHAVED CORRECTLY" if all_ok else "FAILURE — see above")
        + " ==="
    )
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(_selftest())
