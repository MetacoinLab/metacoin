"""task-0039-data-pipeline-reconciliation — deterministic reconciliation of
two pinned transaction ledgers (source vs sink) with integer-cents amounts:
key-level deltas, amount mismatches, and a balanced verdict — the detector
first PROVES ITSELF on a planted broken sink (software/data family,
member 5).

Research-only. A bit-reproducible pipeline-reconciliation task of the kind
data engineers run nightly: the pinned source and sink ledgers carry the
same six transactions (sink deliberately in a different order — order must
not matter to a correct reconciler). Before certifying, compute() runs the
same reconciler against a planted broken sink (one row dropped, one amount
altered) and asserts it finds exactly one missing row and exactly one
mismatch; only then does it reconcile the pinned pair, which honestly
balances. All amounts are integer cents — no float arithmetic anywhere in
the money path. It maps to the NASA Technology Taxonomy TX11 (Software,
Modeling, Simulation, and Information Processing).

INTERNAL SELF-PROOF (three assertion classes inside compute() per MIP-0008
rule 1): compute() asserts
  (a) DETECTOR KNOWN-TRUTH — the planted broken sink yields exactly the
      planted deltas (1 missing-in-sink, 1 amount mismatch, unbalanced);
  (b) CONSERVATION, TWO PATHS — source and sink totals agree both via
      whole-ledger sums and via per-key matched-pair sums;
  (c) IDEMPOTENCE — reconciling the sink against itself yields zero
      deltas and a balanced verdict (a reconciler that finds phantom
      deltas in identical data is broken).
A violated assertion CRASHES the task — stop, don't fudge.

Synthetic, pinned, in-module ledgers (zero-value synthetic cents; no real
money, no external files). Test-META is a zero-value testnet placeholder
and never mints base supply (MIP-0001 paragraph 3, MIP-0002 paragraph 8).
Not financial, legal, or engineering advice.
No NASA affiliation or endorsement.

Standard library only (json, hashlib). No randomness; every emitted number
is an integer or boolean, so re-runs are byte-identical and the SHA-256
output hash is stable (the basis of the Gate-2 check). MIP-0009 contract:
compute() -> the four-key dict, canonical_json() era-2 (sign-of-zero-free),
output_hash() = sha256 of it.
"""

import hashlib
import json

PARENT_TASKS = []

# --- Fixed inputs (part of the reproducibility hash) ------------------------
SOURCE_LEDGER = (
    {"txn_id": "t-001", "amount_cents": 12500},
    {"txn_id": "t-002", "amount_cents": 4999},
    {"txn_id": "t-003", "amount_cents": 73000},
    {"txn_id": "t-004", "amount_cents": 150},
    {"txn_id": "t-005", "amount_cents": 20851},
    {"txn_id": "t-006", "amount_cents": 13500},
)
SINK_LEDGER = (                       # same rows, deliberately reordered
    {"txn_id": "t-004", "amount_cents": 150},
    {"txn_id": "t-001", "amount_cents": 12500},
    {"txn_id": "t-006", "amount_cents": 13500},
    {"txn_id": "t-002", "amount_cents": 4999},
    {"txn_id": "t-005", "amount_cents": 20851},
    {"txn_id": "t-003", "amount_cents": 73000},
)
# The planted broken sink: t-003 dropped, t-005 altered by +100 cents.
BROKEN_SINK_LEDGER = (
    {"txn_id": "t-001", "amount_cents": 12500},
    {"txn_id": "t-002", "amount_cents": 4999},
    {"txn_id": "t-004", "amount_cents": 150},
    {"txn_id": "t-005", "amount_cents": 20951},
    {"txn_id": "t-006", "amount_cents": 13500},
)


def _reconcile(source, sink):
    """Key-join reconciliation. Returns the delta document (all sorted)."""
    src = {r["txn_id"]: r["amount_cents"] for r in source}
    snk = {r["txn_id"]: r["amount_cents"] for r in sink}
    missing_in_sink = sorted(k for k in src if k not in snk)
    missing_in_source = sorted(k for k in snk if k not in src)
    mismatches = []
    for k in sorted(src):                # bounded: source rows
        if k in snk and src[k] != snk[k]:
            mismatches.append({"txn_id": k,
                               "source_amount_cents": src[k],
                               "sink_amount_cents": snk[k],
                               "delta_cents": snk[k] - src[k]})
    balanced = (not missing_in_sink and not missing_in_source
                and not mismatches)
    return {"missing_in_sink": missing_in_sink,
            "missing_in_source": missing_in_source,
            "amount_mismatches": mismatches,
            "balanced": balanced}


def compute() -> dict:
    """Prove the reconciler on the planted sink, then reconcile the pair."""
    planted = _reconcile(SOURCE_LEDGER, BROKEN_SINK_LEDGER)

    # --- SELF-PROOF (a): detector known-truth ------------------------------
    assert (planted["missing_in_sink"] == ["t-003"]
            and planted["missing_in_source"] == []
            and len(planted["amount_mismatches"]) == 1
            and planted["amount_mismatches"][0]["txn_id"] == "t-005"
            and planted["amount_mismatches"][0]["delta_cents"] == 100
            and planted["balanced"] is False), (
        f"detector known-truth violated: planted broken sink yielded "
        f"{planted} — expected exactly 1 missing (t-003) and 1 mismatch "
        "(t-005, +100 cents)")

    deltas = _reconcile(SOURCE_LEDGER, SINK_LEDGER)
    total_source_cents = sum(r["amount_cents"] for r in SOURCE_LEDGER)
    total_sink_cents = sum(r["amount_cents"] for r in SINK_LEDGER)

    # --- SELF-PROOF (b): conservation, two independent paths ---------------
    matched_pair_cents = 0
    snk = {r["txn_id"]: r["amount_cents"] for r in SINK_LEDGER}
    for r in SOURCE_LEDGER:              # bounded: source rows
        matched_pair_cents += snk.get(r["txn_id"], 0)
    assert total_source_cents == total_sink_cents == matched_pair_cents, (
        f"conservation violated: source {total_source_cents} vs sink "
        f"{total_sink_cents} vs matched-pair sum {matched_pair_cents} "
        "cents — the totals must agree on all three paths")

    # --- SELF-PROOF (c): idempotence ---------------------------------------
    self_deltas = _reconcile(SINK_LEDGER, SINK_LEDGER)
    assert self_deltas["balanced"] is True and not (
            self_deltas["missing_in_sink"]
            or self_deltas["amount_mismatches"]), (
        "idempotence violated: reconciling the sink against itself found "
        f"phantom deltas {self_deltas} — the reconciler is broken")

    return {
        "task_id": "task-0039-data-pipeline-reconciliation",
        "inputs": {
            "source_row_count": len(SOURCE_LEDGER),
            "sink_row_count": len(SINK_LEDGER),
            "join_key_note": "key join on txn_id; row order deliberately "
                             "differs between the pinned ledgers and must "
                             "not matter",
            "planted_fixture_note": "broken sink drops t-003 and alters "
                                    "t-005 by +100 cents; the reconciler "
                                    "must find exactly those before it may "
                                    "certify the real pair",
            "amount_unit_note": "all amounts are integer synthetic cents; "
                                "no float arithmetic in the money path",
        },
        "results": [
            {"step": "detector_proof_on_broken_sink",
             "missing_in_sink_count": len(planted["missing_in_sink"]),
             "mismatch_count": len(planted["amount_mismatches"]),
             "balanced_flag": planted["balanced"]},
            {"step": "reconcile_pinned_pair",
             "missing_in_sink": deltas["missing_in_sink"],
             "missing_in_source": deltas["missing_in_source"],
             "amount_mismatches": deltas["amount_mismatches"],
             "total_source_cents": total_source_cents,
             "total_sink_cents": total_sink_cents},
        ],
        "summary": {
            "balanced": deltas["balanced"],
            "delta_count": (len(deltas["missing_in_sink"])
                            + len(deltas["missing_in_source"])
                            + len(deltas["amount_mismatches"])),
            "total_reconciled_cents": total_source_cents,
            "honest_note": "the balanced verdict is only meaningful because "
                           "the same reconciler first re-found the planted "
                           "drop and the planted +100-cent alteration in "
                           "the broken fixture",
            "self_proofs_checked": ["planted_sink_exact_detection",
                                    "three_path_total_conservation",
                                    "self_reconciliation_idempotence"],
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
