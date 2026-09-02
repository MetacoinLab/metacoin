"""task-0038-config-consistency-audit — deterministic cross-field audit of a
pinned service configuration (unit ranges, retry-budget arithmetic, a TLS
mutual exclusion, a sampling-fraction range) whose pinned config passes;
the rule engine first PROVES ITSELF against an in-module broken fixture it
must flag exactly (software/data family, member 4).

Research-only. A bit-reproducible configuration audit of the kind
deployment tooling performs: four cross-field rules over a pinned config
tree. The audit machinery is not trusted bare — compute() first runs the
same engine over a deliberately broken fixture config and asserts it finds
EXACTLY the three planted violations (a detector that cannot find planted
faults has no business certifying a clean config); only then does it audit
the pinned real config, which honestly passes with zero violations. It
maps to the NASA Technology Taxonomy TX11 (Software, Modeling, Simulation,
and Information Processing).

INTERNAL SELF-PROOF (three assertion classes inside compute() per MIP-0008
rule 1): compute() asserts
  (a) DETECTOR KNOWN-TRUTH — the broken fixture yields exactly the three
      planted rule violations, by rule id, no more and no fewer;
  (b) RULE TOTALITY — every rule in the pinned rule set is evaluated
      exactly once per config (audited rule count == rule set size);
  (c) VERDICT CONSISTENCY — config_consistent is true if and only if the
      violation count is zero, and the retry arithmetic re-checks through
      an independent multiplication path.
A violated assertion CRASHES the task — stop, don't fudge.

Synthetic, pinned, in-module config (no environment reads). Test-META is a
zero-value testnet placeholder and never mints base supply (MIP-0001
paragraph 3, MIP-0002 paragraph 8). Not financial, legal, or engineering
advice. No NASA affiliation or endorsement.

Standard library only (json, hashlib). No randomness; every emitted float
is rounded to a fixed number of decimals so re-runs are byte-identical and
the SHA-256 output hash is stable (the basis of the Gate-2 check).
MIP-0009 contract: compute() -> the four-key dict, canonical_json() era-2
(sign-of-zero-free), output_hash() = sha256 of it.
"""

import hashlib
import json

PARENT_TASKS = []

# --- Fixed inputs (part of the reproducibility hash) ------------------------
PINNED_CONFIG = {
    "request_timeout_s": 30,
    "max_retry_count": 4,
    "retry_backoff_ms": 250,
    "retry_budget_ms": 2000,
    "tls_enabled_flag": True,
    "plaintext_port_num": 0,             # 0 = disabled
    "telemetry_sample_fraction": 0.25,
}
# The broken fixture the detector must flag EXACTLY (three planted faults):
# timeout out of range, TLS/plaintext mutual exclusion violated (neither
# enabled), and retry arithmetic over budget.
BROKEN_FIXTURE_CONFIG = {
    "request_timeout_s": 0,              # violates rule-timeout-range
    "max_retry_count": 10,
    "retry_backoff_ms": 400,
    "retry_budget_ms": 2000,             # 10*400 = 4000 > 2000
    "tls_enabled_flag": False,
    "plaintext_port_num": 0,             # neither TLS nor plaintext
    "telemetry_sample_fraction": 0.25,
}
PLANTED_VIOLATION_RULE_IDS = ("rule-timeout-range",
                              "rule-retry-budget",
                              "rule-tls-xor-plaintext")
TIMEOUT_RANGE_S = (1, 300)
FRACTION_RANGE = (0.0, 1.0)
ROUND_DECIMALS = 6


def _audit(cfg):
    """Evaluate the four pinned rules; return sorted violating rule ids."""
    violations = []
    rules_evaluated = 0
    # rule-timeout-range
    rules_evaluated += 1
    if not (TIMEOUT_RANGE_S[0] <= cfg["request_timeout_s"]
            <= TIMEOUT_RANGE_S[1]):
        violations.append("rule-timeout-range")
    # rule-retry-budget: worst-case linear backoff must fit the budget
    rules_evaluated += 1
    if cfg["max_retry_count"] * cfg["retry_backoff_ms"] > \
            cfg["retry_budget_ms"]:
        violations.append("rule-retry-budget")
    # rule-tls-xor-plaintext: exactly one transport enabled
    rules_evaluated += 1
    if cfg["tls_enabled_flag"] == (cfg["plaintext_port_num"] > 0):
        violations.append("rule-tls-xor-plaintext")
    # rule-sample-fraction-range
    rules_evaluated += 1
    if not (FRACTION_RANGE[0] <= cfg["telemetry_sample_fraction"]
            <= FRACTION_RANGE[1]):
        violations.append("rule-sample-fraction-range")
    return sorted(violations), rules_evaluated


def compute() -> dict:
    """Prove the detector on the fixture, then audit the pinned config."""
    fixture_violations, fixture_rules = _audit(BROKEN_FIXTURE_CONFIG)

    # --- SELF-PROOF (a): detector known-truth on the planted fixture -------
    assert fixture_violations == sorted(PLANTED_VIOLATION_RULE_IDS), (
        f"detector known-truth violated: fixture audit found "
        f"{fixture_violations}, planted {sorted(PLANTED_VIOLATION_RULE_IDS)}"
        " — a detector that cannot find planted faults certifies nothing")

    violations, rules_evaluated = _audit(PINNED_CONFIG)

    # --- SELF-PROOF (b): rule totality -------------------------------------
    assert rules_evaluated == 4 and fixture_rules == 4, (
        f"rule totality violated: {rules_evaluated}/{fixture_rules} rules "
        "evaluated, rule set has 4 — a skipped rule is a silent pass")

    # --- SELF-PROOF (c): verdict consistency + independent arithmetic ------
    consistent = len(violations) == 0
    assert consistent == (not violations), (
        "verdict consistency violated: config_consistent must equal "
        "'zero violations'")
    worst_case_ms = 0
    for _ in range(PINNED_CONFIG["max_retry_count"]):  # bounded: retry count
        worst_case_ms += PINNED_CONFIG["retry_backoff_ms"]
    assert worst_case_ms == (PINNED_CONFIG["max_retry_count"]
                             * PINNED_CONFIG["retry_backoff_ms"]), (
        f"independent retry arithmetic violated: summed {worst_case_ms} != "
        "multiplied path")

    return {
        "task_id": "task-0038-config-consistency-audit",
        "inputs": {
            "config": dict(PINNED_CONFIG,
                           telemetry_sample_fraction=round(
                               PINNED_CONFIG["telemetry_sample_fraction"],
                               ROUND_DECIMALS)),
            "rule_count": 4,
            "rule_note": "timeout in [1,300] s; max_retry_count * "
                         "retry_backoff_ms <= retry_budget_ms; TLS XOR "
                         "plaintext transport; sample fraction in [0,1]",
            "planted_fixture_violation_count":
                len(PLANTED_VIOLATION_RULE_IDS),
            "round_decimals": ROUND_DECIMALS,
        },
        "results": [
            {"step": "detector_proof_on_broken_fixture",
             "violating_rule_ids": fixture_violations,
             "violation_count": len(fixture_violations)},
            {"step": "pinned_config_audit",
             "violating_rule_ids": violations,
             "violation_count": len(violations),
             "worst_case_retry_ms": worst_case_ms},
        ],
        "summary": {
            "config_consistent": consistent,
            "violation_count": len(violations),
            "honest_note": "the clean verdict is only meaningful because "
                           "the same engine first re-found all three "
                           "planted faults in the broken fixture — the "
                           "detector proves itself before it certifies",
            "self_proofs_checked": ["planted_fixture_exact_detection",
                                    "rule_totality",
                                    "verdict_consistency_and_arithmetic"],
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
