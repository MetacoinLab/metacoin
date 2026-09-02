"""task-0040-test-coverage-gap — deterministic root-to-leaf path coverage of
a pinned call graph against a pinned test map: the coverage target is
HONESTLY NOT MET (7 of 10 paths covered, 0.70 < the 0.90 target) — the
uncovered paths are the deliverable (software/data family, member 6;
HONEST NEGATIVE).

Research-only. A bit-reproducible coverage audit of the kind CI tooling
computes: a small pinned call DAG (entry 'main', 10 root-to-leaf paths
enumerated by iterative DFS — no recursion, per law) and a pinned test map
naming which paths each test exercises. The correct canonical answer is
coverage_target_met: false with the three uncovered paths listed; padding
the test map or lowering the target to manufacture a pass changes the hash
and is a REJECT. It maps to the NASA Technology Taxonomy TX11 (Software,
Modeling, Simulation, and Information Processing).

INTERNAL SELF-PROOF (three assertion classes inside compute() per MIP-0008
rule 1): compute() asserts
  (a) KNOWN-TRUTH, TWO PATHS — the DFS path count equals the independent
      topological dynamic-programming count over the same DAG;
  (b) PARTITION — covered plus uncovered paths partition the enumerated
      set exactly (no path lost, none double-counted), and every
      test-map path names a real enumerated path;
  (c) BOUNDS/ACYCLICITY — DFS steps stay under the stated bound and no
      node repeats within any emitted path (the DAG claim is checked,
      not assumed).
A violated assertion CRASHES the task — stop, don't fudge.

Synthetic, pinned, in-module graph and test map (no repo scanning).
Test-META is a zero-value testnet placeholder and never mints base supply
(MIP-0001 paragraph 3, MIP-0002 paragraph 8). Not financial, legal, or
engineering advice. No NASA affiliation or endorsement.

Standard library only (json, hashlib). No randomness; the one emitted
float (the coverage fraction) is rounded to a fixed number of decimals so
re-runs are byte-identical and the SHA-256 output hash is stable (the
basis of the Gate-2 check). MIP-0009 contract: compute() -> the four-key
dict, canonical_json() era-2 (sign-of-zero-free), output_hash() = sha256
of it.
"""

import hashlib
import json

PARENT_TASKS = []

# --- Fixed inputs (part of the reproducibility hash) ------------------------
ENTRY_NODE = "main"
CALL_GRAPH = {                          # adjacency; absent key = leaf
    "main": ("auth", "api", "health", "metrics"),
    "auth": ("login", "token"),
    "api": ("read", "write", "admin", "delete"),
    "write": ("validate_w", "commit"),
    "admin": ("validate_a", "audit"),
}
TEST_MAP = {
    "test_login": ("main/auth/login",),
    "test_token": ("main/auth/token",),
    "test_health": ("main/health",),
    "test_metrics": ("main/metrics",),
    "test_read": ("main/api/read",),
    "test_write_validate": ("main/api/write/validate_w",),
    "test_write_commit": ("main/api/write/commit",),
}
COVERAGE_TARGET_FRACTION = 0.9
MAX_DFS_STEPS = 200                     # R2 bound; the real walk is tiny
ROUND_DECIMALS = 6


def _enumerate_paths():
    """Iterative DFS (explicit stack — no recursion) over the pinned DAG.
    Returns sorted 'a/b/c' root-to-leaf path strings."""
    paths, steps = [], 0
    stack = [(ENTRY_NODE, (ENTRY_NODE,))]
    while stack and steps < MAX_DFS_STEPS:   # R2: explicit named bound
        steps += 1
        node, path = stack.pop()
        children = CALL_GRAPH.get(node, ())
        if not children:
            paths.append("/".join(path))
            continue
        for child in reversed(children):  # bounded: fan-out
            assert child not in path, (
                f"acyclicity violated: {child!r} repeats on path "
                f"{'/'.join(path)} — this is not the declared DAG")
            stack.append((child, path + (child,)))
    assert not stack, (
        f"DFS bound violated: {steps} steps hit the {MAX_DFS_STEPS} bound "
        "with work remaining — the pinned graph is larger or more cyclic "
        "than declared")
    return sorted(paths), steps


def _dp_path_count():
    """Independent count: leaf-path DP over the DAG (bounded two passes)."""
    counts = {}

    def _nodes_postorder():
        # Kahn-style: process nodes whose children are all counted.
        pending = set(CALL_GRAPH) | {c for cs in CALL_GRAPH.values()
                                     for c in cs}
        ordered = []
        for _ in range(len(pending) + 1):  # bounded: node count
            ready = [n for n in sorted(pending)
                     if all(c in counts or c not in pending
                            for c in CALL_GRAPH.get(n, ()))]
            for n in ready:
                counts[n] = (sum(counts.get(c, 1)
                                 for c in CALL_GRAPH[n])
                             if CALL_GRAPH.get(n) else 1)
                pending.discard(n)
                ordered.append(n)
            if not pending:
                break
        return ordered

    _nodes_postorder()
    return counts[ENTRY_NODE]


def compute() -> dict:
    """Enumerate, partition into covered/uncovered, and report honestly."""
    all_paths, dfs_steps = _enumerate_paths()

    # --- SELF-PROOF (a): known-truth path count, two independent paths -----
    assert len(all_paths) == _dp_path_count(), (
        f"two-path count violated: DFS enumerated {len(all_paths)} paths, "
        f"topological DP counts {_dp_path_count()}")

    covered = set()
    for test_id in sorted(TEST_MAP):     # bounded: test map
        for p in TEST_MAP[test_id]:
            assert p in all_paths, (
                f"partition violated: test {test_id!r} names path {p!r} "
                "which the graph does not contain — a test may not cover "
                "a phantom path")
            covered.add(p)
    uncovered = sorted(set(all_paths) - covered)

    # --- SELF-PROOF (b): exact partition -----------------------------------
    assert len(covered) + len(uncovered) == len(all_paths), (
        f"partition violated: {len(covered)} covered + {len(uncovered)} "
        f"uncovered != {len(all_paths)} total")

    coverage = round(len(covered) / len(all_paths), ROUND_DECIMALS)
    target_met = coverage >= COVERAGE_TARGET_FRACTION

    # --- SELF-PROOF (c): the negative is real, not a rounding artifact -----
    assert 10 * len(covered) < 9 * len(all_paths), (
        f"known-truth violated: {len(covered)}/{len(all_paths)} covered "
        "does not sit below the 0.9 target in exact integer arithmetic — "
        "the pinned negative would be a rounding artifact")

    return {
        "task_id": "task-0040-test-coverage-gap",
        "inputs": {
            "entry_node_id": ENTRY_NODE,
            "graph_node_count": len(set(CALL_GRAPH)
                                    | {c for cs in CALL_GRAPH.values()
                                       for c in cs}),
            "test_count": len(TEST_MAP),
            "coverage_target_fraction": COVERAGE_TARGET_FRACTION,
            "max_dfs_step_count": MAX_DFS_STEPS,
            "round_decimals": ROUND_DECIMALS,
        },
        "results": [
            {"step": "path_enumeration",
             "total_path_count": len(all_paths),
             "dfs_step_count": dfs_steps},
            {"step": "coverage_partition",
             "covered_path_count": len(covered),
             "uncovered_path_count": len(uncovered),
             "uncovered_paths": uncovered,
             "coverage_fraction": coverage},
        ],
        "summary": {
            "coverage_target_met": target_met,
            "coverage_fraction": coverage,
            "uncovered_path_count": len(uncovered),
            "uncovered_paths": uncovered,
            "honest_note": "the correct answer is NO: 7 of 10 root-to-leaf "
                           "paths are covered (0.7 < the 0.9 target); the "
                           "three uncovered admin/delete paths are the "
                           "deliverable — padding the test map to "
                           "manufacture a pass changes the hash and is a "
                           "REJECT",
            "self_proofs_checked": ["two_path_count_dfs_vs_dp",
                                    "exact_partition_no_phantom_paths",
                                    "bounds_acyclicity_and_exact_shortfall"],
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
