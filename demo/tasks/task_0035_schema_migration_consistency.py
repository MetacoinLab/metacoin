"""task-0035-schema-migration-consistency — deterministic schema v1->v2
migration over a pinned record set, with an integrity verdict the migration
honestly FAILS: dropping the region key cannot preserve username uniqueness
(the first software/data-engineering task family member; HONEST NEGATIVE).

Research-only. A bit-reproducible data-engineering task of the kind coding
agents face daily: schema v1 keys user records by (region_code, username);
the pinned v2 migration drops region_code (global usernames) and renames
plan_code -> tier_code through a total pinned map. The migration itself is
mechanical; the VERDICT is the deliverable — and at these pinned records
the answer is honestly NO: two usernames exist in both regions, so the
stated uniqueness invariant cannot survive the key change. The correct
canonical result contains migration_valid: false with the violating keys;
"fixing" the data to report success changes the hash and is a REJECT. It
maps to the NASA Technology Taxonomy TX11 (Software, Modeling, Simulation,
and Information Processing) — no taxonomy extension needed: the family is
software engineering, which TX11 covers.

INTERNAL SELF-PROOF (three assertion classes inside compute() per MIP-0008
rule 1): compute() asserts
  (a) CONSERVATION — the migrated record count equals the pinned source
      count (a migration may never drop or invent records silently);
  (b) KNOWN-TRUTH, TWO PATHS — the uniqueness violations found by the
      sorted-scan detector equal those found by an independent
      occurrence-counting detector, key for key;
  (c) BOUNDS/MONOTONICITY — every violating username occurs at least
      twice in the migrated set, the violation count is strictly below
      the record count, and removing one pinned duplicate on a probe
      subset strictly reduces the violation count.
A violated assertion CRASHES the task — stop, don't fudge.

Synthetic, pinned, in-module data (no external fetch; nothing personal —
the usernames are computing-history first names). Test-META is a
zero-value testnet placeholder and never mints base supply (MIP-0001
paragraph 3, MIP-0002 paragraph 8). Not financial, legal, or engineering
advice. No NASA affiliation or endorsement.

Standard library only (json, hashlib). No randomness. All emitted numbers
are integers or booleans, so re-runs are byte-identical and the SHA-256
output hash is stable (the basis of the Gate-2 check). MIP-0009 contract:
compute() -> the four-key dict, canonical_json() era-2 (sign-of-zero-free),
output_hash() = sha256 of it.
"""

import hashlib
import json

PARENT_TASKS = []

# --- Fixed inputs (part of the reproducibility hash) ------------------------
SCHEMA_V1_ID = "user-record/1.0"
SCHEMA_V2_ID = "user-record/2.0"
# v1 records, keyed by (region_code, username). Two usernames deliberately
# exist in both regions — the honest collision the verdict is about.
SOURCE_RECORDS_V1 = (
    {"region_code": "na", "username": "ada",     "plan_code": "pro",
     "signup_day_index": 12},
    {"region_code": "eu", "username": "ada",     "plan_code": "basic",
     "signup_day_index": 40},
    {"region_code": "na", "username": "lin",     "plan_code": "basic",
     "signup_day_index": 7},
    {"region_code": "eu", "username": "lin",     "plan_code": "pro",
     "signup_day_index": 33},
    {"region_code": "na", "username": "grace",   "plan_code": "pro",
     "signup_day_index": 3},
    {"region_code": "eu", "username": "alan",    "plan_code": "basic",
     "signup_day_index": 21},
    {"region_code": "na", "username": "edsger",  "plan_code": "basic",
     "signup_day_index": 55},
    {"region_code": "eu", "username": "barbara", "plan_code": "pro",
     "signup_day_index": 61},
)
TIER_MAP = {"basic": "B", "pro": "P"}   # total over every pinned plan_code
# v2 invariants under verification (the migration contract):
#   I1 conservation: record count preserved
#   I2 totality: every plan_code maps through TIER_MAP
#   I3 uniqueness: username is a global unique key after region_code drops
UNIQUENESS_FIELD = "username"


def _migrate(records):
    """The pinned v1->v2 migration: drop region_code, map plan->tier."""
    out = []
    for r in records:                    # bounded: pinned tuple
        out.append({"username": r["username"],
                    "tier_code": TIER_MAP[r["plan_code"]],
                    "signup_day_index": r["signup_day_index"]})
    return out


def _violations_by_scan(migrated):
    """Detector 1: sorted scan — adjacent equal keys are violations."""
    names = sorted(m[UNIQUENESS_FIELD] for m in migrated)
    hits = set()
    for i in range(1, len(names)):       # bounded: len(migrated)
        if names[i] == names[i - 1]:
            hits.add(names[i])
    return sorted(hits)


def _violations_by_count(migrated):
    """Detector 2 (independent path): occurrence counting."""
    seen = {}
    for m in migrated:                   # bounded: len(migrated)
        seen[m[UNIQUENESS_FIELD]] = seen.get(m[UNIQUENESS_FIELD], 0) + 1
    return sorted(k for k, n in seen.items() if n > 1)


def compute() -> dict:
    """Migrate, audit the invariants, and return the honest verdict."""
    migrated = _migrate(SOURCE_RECORDS_V1)

    # --- SELF-PROOF (a): conservation --------------------------------------
    assert len(migrated) == len(SOURCE_RECORDS_V1), (
        f"conservation violated: migration emitted {len(migrated)} records "
        f"from {len(SOURCE_RECORDS_V1)} — a migration may never drop or "
        "invent records silently")

    totality_ok = all(r["plan_code"] in TIER_MAP for r in SOURCE_RECORDS_V1)
    violating = _violations_by_scan(migrated)

    # --- SELF-PROOF (b): known-truth, two independent detectors ------------
    assert violating == _violations_by_count(migrated), (
        f"two-path violation detection disagrees: scan {violating} vs "
        f"count {_violations_by_count(migrated)} — the detector is broken, "
        "not the data")

    # --- SELF-PROOF (c): bounds + monotone probe ---------------------------
    for name in violating:               # bounded: violating list
        occurrences = sum(1 for m in migrated
                          if m[UNIQUENESS_FIELD] == name)
        assert occurrences >= 2, (
            f"bound violated: {name!r} flagged as duplicate but occurs "
            f"{occurrences} time(s)")
    assert 0 < len(violating) < len(migrated), (
        f"bound violated: violation count {len(violating)} must sit "
        f"strictly inside (0, {len(migrated)}) for this pinned negative")
    probe = [r for r in SOURCE_RECORDS_V1
             if not (r["region_code"] == "eu"
                     and r["username"] == violating[0])]
    probe_violations = _violations_by_count(_migrate(probe))
    assert len(probe_violations) == len(violating) - 1, (
        "monotonicity violated: removing one pinned duplicate did not "
        f"reduce the violation count ({len(violating)} -> "
        f"{len(probe_violations)})")

    migration_valid = totality_ok and not violating
    return {
        "task_id": "task-0035-schema-migration-consistency",
        "inputs": {
            "schema_v1_id": SCHEMA_V1_ID,
            "schema_v2_id": SCHEMA_V2_ID,
            "source_record_count": len(SOURCE_RECORDS_V1),
            "migration_note": "drop region_code (username becomes a global "
                              "key); rename plan_code -> tier_code via the "
                              "pinned total map",
            "uniqueness_field": UNIQUENESS_FIELD,
            "tier_map_entry_count": len(TIER_MAP),
        },
        "results": [
            {"step": "migrate",
             "migrated_record_count": len(migrated),
             "tier_totality_flag": totality_ok},
            {"step": "uniqueness_audit",
             "violating_keys": violating,
             "violation_count": len(violating)},
        ],
        "summary": {
            "migration_valid": migration_valid,
            "violating_keys": violating,
            "violation_count": len(violating),
            "honest_note": "the correct answer is NO: usernames "
                           + " and ".join(repr(v) for v in violating)
                           + " exist in both regions, so dropping "
                           "region_code cannot preserve uniqueness — the "
                           "negative verdict is the deliverable",
            "self_proofs_checked": ["record_conservation",
                                    "two_path_violation_detection",
                                    "bounds_and_monotone_probe"],
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
