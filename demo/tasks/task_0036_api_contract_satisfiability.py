"""task-0036-api-contract-satisfiability — deterministic satisfiability of
two pinned typed request contracts by bounded exhaustive search: one
contract yields a concrete satisfying example, the other is PROVED
unsatisfiable over its whole finite domain (software/data family, member 2).

Research-only. A bit-reproducible contract-checking task of the kind API
tooling faces: a request type {x, y} with integer field domains and
cross-field constraints. Contract A is satisfiable (the lexicographically
first witness is emitted, plus the exact solution count); contract B adds
constraints that contradict (y == 2*x with y <= 5 while x >= 4 forces
y >= 8), so the bounded search exhausts the full domain and honestly
reports no witness — a PROOF by exhaustion at this domain size, not a
sampling claim. It maps to the NASA Technology Taxonomy TX11 (Software,
Modeling, Simulation, and Information Processing).

INTERNAL SELF-PROOF (three assertion classes inside compute() per MIP-0008
rule 1): compute() asserts
  (a) WITNESS VALIDITY — the found example re-satisfies every contract-A
      constraint through an independent re-evaluation path;
  (b) EXHAUSTIVENESS — the searched combination count equals the exact
      domain product for both contracts and stays under the stated bound;
  (c) KNOWN-TRUTH — contract A's solution count equals the closed-form
      count (x in [3..4] once y == 2x and x + y <= 12 bind), and contract
      B's emptiness re-proves from the necessary condition min(y) = 8 > 5.
A violated assertion CRASHES the task — stop, don't fudge.

Synthetic, pinned, in-module contracts (no external fetch). Test-META is a
zero-value testnet placeholder and never mints base supply (MIP-0001
paragraph 3, MIP-0002 paragraph 8). Not financial, legal, or engineering
advice. No NASA affiliation or endorsement.

Standard library only (json, hashlib). No randomness; all emitted numbers
are integers or booleans, so re-runs are byte-identical and the SHA-256
output hash is stable (the basis of the Gate-2 check). MIP-0009 contract:
compute() -> the four-key dict, canonical_json() era-2 (sign-of-zero-free),
output_hash() = sha256 of it.
"""

import hashlib
import json

PARENT_TASKS = []

# --- Fixed inputs (part of the reproducibility hash) ------------------------
X_DOMAIN = (1, 10)     # inclusive integer field domains (the typed contract)
Y_DOMAIN = (1, 20)
# Contract A: y == 2x, x + y <= 12, x >= 3   -> satisfiable (x in {3, 4})
# Contract B: y == 2x, y <= 5,      x >= 4   -> unsatisfiable (min y = 8)
CONTRACT_A_ID = "contract-a/doubling-with-budget"
CONTRACT_B_ID = "contract-b/doubling-under-cap"
MAX_SEARCH_COMBINATIONS = 1000   # R2 bound; the real product is 200


def _constraints_a(x, y):
    return y == 2 * x and x + y <= 12 and x >= 3


def _constraints_b(x, y):
    return y == 2 * x and y <= 5 and x >= 4


def _bounded_search(predicate):
    """Exhaustive lexicographic search over the full pinned domain.
    Returns (first_witness_or_None, solution_count, searched_count)."""
    witness, solutions, searched = None, 0, 0
    for x in range(X_DOMAIN[0], X_DOMAIN[1] + 1):     # bounded: 10
        for y in range(Y_DOMAIN[0], Y_DOMAIN[1] + 1):  # bounded: 20
            searched += 1
            assert searched <= MAX_SEARCH_COMBINATIONS, (
                f"loop bound violated: searched {searched} combinations, "
                f"bound {MAX_SEARCH_COMBINATIONS}")
            if predicate(x, y):
                solutions += 1
                if witness is None:
                    witness = {"x_unit": x, "y_unit": y}
    return witness, solutions, searched


def compute() -> dict:
    """Decide both contracts by exhaustion; emit witness or emptiness."""
    domain_product = ((X_DOMAIN[1] - X_DOMAIN[0] + 1)
                      * (Y_DOMAIN[1] - Y_DOMAIN[0] + 1))
    wit_a, count_a, searched_a = _bounded_search(_constraints_a)
    wit_b, count_b, searched_b = _bounded_search(_constraints_b)

    # --- SELF-PROOF (a): witness validity, independent re-evaluation -------
    assert wit_a is not None and (
        wit_a["y_unit"] == 2 * wit_a["x_unit"]
        and wit_a["x_unit"] + wit_a["y_unit"] <= 12
        and wit_a["x_unit"] >= 3), (
        f"witness validity violated: {wit_a} does not re-satisfy contract A "
        "under independent re-evaluation")

    # --- SELF-PROOF (b): exhaustiveness ------------------------------------
    assert searched_a == domain_product and searched_b == domain_product, (
        f"exhaustiveness violated: searched {searched_a}/{searched_b} "
        f"combinations, domain product is {domain_product} — the emptiness "
        "claim would be a sampling claim, not a proof")

    # --- SELF-PROOF (c): known-truth, closed form both ways ----------------
    closed_form_a = sum(1 for x in range(3, X_DOMAIN[1] + 1)
                        if 3 * x <= 12 and Y_DOMAIN[0] <= 2 * x <= Y_DOMAIN[1])
    assert count_a == closed_form_a, (
        f"known-truth violated: search found {count_a} contract-A solutions, "
        f"closed form gives {closed_form_a}")
    min_y_under_b = 2 * 4                # x >= 4 forces y = 2x >= 8
    assert count_b == 0 and min_y_under_b > 5, (
        f"known-truth violated: contract B reported {count_b} solutions but "
        f"min feasible y is {min_y_under_b} > 5 — emptiness must re-prove")

    return {
        "task_id": "task-0036-api-contract-satisfiability",
        "inputs": {
            "contract_a_id": CONTRACT_A_ID,
            "contract_b_id": CONTRACT_B_ID,
            "x_domain_min_unit": X_DOMAIN[0], "x_domain_max_unit": X_DOMAIN[1],
            "y_domain_min_unit": Y_DOMAIN[0], "y_domain_max_unit": Y_DOMAIN[1],
            "constraint_note": "A: y == 2x, x + y <= 12, x >= 3; "
                               "B: y == 2x, y <= 5, x >= 4",
            "max_search_combination_count": MAX_SEARCH_COMBINATIONS,
        },
        "results": [
            {"step": "contract_a",
             "satisfiable_flag": True,
             "first_witness": wit_a,
             "solution_count": count_a,
             "searched_combination_count": searched_a},
            {"step": "contract_b",
             "satisfiable_flag": False,
             "first_witness": None,
             "solution_count": count_b,
             "searched_combination_count": searched_b},
        ],
        "summary": {
            "contract_a_satisfiable": True,
            "contract_b_satisfiable": False,
            "satisfiability_decided": True,
            "honest_note": "contract B's 'no' is a proof by exhaustion over "
                           "the full 200-combination domain plus a "
                           "necessary-condition re-proof — reporting a "
                           "fabricated witness changes the hash and is a "
                           "REJECT",
            "self_proofs_checked": ["witness_independent_reevaluation",
                                    "search_exhaustiveness",
                                    "closed_form_counts_both_contracts"],
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
