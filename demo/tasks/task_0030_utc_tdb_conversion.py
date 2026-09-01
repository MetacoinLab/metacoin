"""task-0030-utc-tdb-conversion — deterministic UTC <-> TDB/ET time-system
conversion at pinned epochs from the pinned NAIF leapseconds-kernel data
(the SPICE beachhead's root node; law-native under MIP-0008/0009).

Research-only. A bit-reproducible time-systems task implementing exactly the
formulation the leapseconds kernel documents in its own comments (Moyer
1981; JPL IOM 314.5-942), with every constant pinned fetched-and-hashed in
demo/tasks/pinned_spice_sources.py (naif0012.tls, sha256 678e32bd...):

    ET - UTC = DELTA_AT(epoch) + DELTA_T_A + K sin E
    E = M + EB sin M,   M = M0 + M1 * t

where t is ET seconds past J2000 — a mildly implicit relation solved here by
a FIXED three-pass refinement (the correction term is ~1.7 ms, so one pass
is already sub-microsecond; three passes are bounded overkill). The UTC
seconds-past-J2000 count is calendar-based (proleptic Gregorian via the
standard library), matching the kernel's DELTET convention. The task
publishes ET for the mission-0001-v2 transfer grid's 20 pinned epochs plus
the J2000 control epoch — the ET values downstream task-0031 consumes and
the committed extractor evaluated the pinned ephemeris states at. It maps
to the NASA Technology Taxonomy TX17 (Guidance, Navigation & Control —
time systems).

INTERNAL SELF-PROOF (three assertion classes inside compute() per MIP-0008
rule 1): compute() asserts
  (a) KNOWN-TRUTH, the J2000 anchor — ET(2000-01-01T12:00:00 UTC) lands in
      the public 64.183-64.185 s band (the canonical str2et landmark), and
      DELTA_AT resolves to 37 s for every grid epoch (the table's final
      entry, in force since 2017);
  (b) ROUND-TRIP IDENTITY — for every epoch, converting the published ET
      back to UTC seconds reproduces the calendar count to within 1e-9 s
      BEFORE rounding (the inverse applies the same fixed-pass relation);
  (c) MONOTONICITY + CONVERGENCE — ET is strictly increasing across the
      ordered grid, and the implicit relation's third-pass update moves the
      value by less than 1e-12 s (the fixed iteration genuinely converged).
A violated assertion CRASHES the task — stop, don't fudge.

SPICE kernels are U.S. government works distributed by NAIF ("No fees or
licensing are required"; redistribution of unmodified kernels permitted —
their rules page, quoted in the pinned module). Proleptic-Gregorian
calendar arithmetic only; epochs before 1972 are outside this task's
domain and refused by the table lookup. Test-META is a zero-value testnet
placeholder and never mints base supply (MIP-0001 paragraph 3, MIP-0002
paragraph 8). Not financial, legal, or flight-engineering advice.
No NASA affiliation or endorsement.

Standard library only (json, math, hashlib, datetime) plus the pinned-
constants module. No randomness. Every emitted float is rounded to a fixed
number of decimals so re-runs are byte-identical and the SHA-256 output
hash is stable (the basis of the Gate-2 check). MIP-0009 contract:
compute() -> the four-key dict, canonical_json() era-2 (sign-of-zero-free),
output_hash() = sha256 of it.
"""

import datetime
import hashlib
import json
import math

try:
    from demo.tasks import pinned_spice_sources as _src
except ImportError:  # direct script run: demo/tasks/ itself is sys.path[0]
    import pinned_spice_sources as _src

PARENT_TASKS = []

# --- Fixed inputs (part of the reproducibility hash) ------------------------
DELTA_T_A_S = _src.DELTET_DELTA_T_A_S
K_S = _src.DELTET_K_S
EB_RAD = _src.DELTET_EB_RAD
M0_RAD = _src.DELTET_M0_RAD
M1_RAD_PER_S = _src.DELTET_M1_RAD_PER_S
DELTA_AT_TABLE = _src.DELTA_AT_TABLE
GRID_UTC = ((("J2000", "2000-01-01T12:00:00"),)
            + _src.DEPARTURE_EPOCHS_UTC + _src.ARRIVAL_EPOCHS_UTC)
J2000_ANCHOR_BAND_S = (64.183, 64.185)   # the canonical str2et landmark
REFINEMENT_PASSES = 3                    # the stated fixed iteration (rule 2)
ROUNDTRIP_TOL_S = 1e-9
CONVERGENCE_TOL_S = 1e-12
ROUND_DECIMALS = 6

_J2000_CAL = datetime.datetime(2000, 1, 1, 12, 0, 0)


def _delta_at_s(utc_string: str) -> int:
    """TAI - UTC for the epoch, from the pinned table (refuses pre-1972)."""
    date = utc_string[:10]
    applicable = [s for s, frm in DELTA_AT_TABLE if frm <= date]
    if not applicable:
        raise ValueError(f"epoch {utc_string} predates the leap-second era")
    return applicable[-1]


def _calendar_seconds_past_j2000(utc_string: str) -> float:
    dt = datetime.datetime.fromisoformat(utc_string)
    return (dt - _J2000_CAL).total_seconds()


def _periodic_term_s(et_s: float) -> float:
    m = M0_RAD + M1_RAD_PER_S * et_s
    e = m + EB_RAD * math.sin(m)
    return K_S * math.sin(e)


def utc_to_et(utc_string: str):
    """(et_s, last_update_s): the kernel-documented relation, fixed passes."""
    naive_s = _calendar_seconds_past_j2000(utc_string)
    base_s = naive_s + _delta_at_s(utc_string) + DELTA_T_A_S
    et_s = base_s
    last_update_s = 0.0
    for _ in range(REFINEMENT_PASSES):   # bounded by REFINEMENT_PASSES
        new_et_s = base_s + _periodic_term_s(et_s)
        last_update_s = abs(new_et_s - et_s)
        et_s = new_et_s
    return et_s, last_update_s


def et_to_calendar_seconds(et_s: float, delta_at_s: int) -> float:
    """The inverse map back to calendar UTC seconds past J2000."""
    return et_s - delta_at_s - DELTA_T_A_S - _periodic_term_s(et_s)


def compute() -> dict:
    """ET for the pinned grid + the J2000 anchor, with the self-proofs."""
    rows = []
    et_values = []
    for label, utc in GRID_UTC:          # bounded: the 21 pinned epochs
        et_s, last_update_s = utc_to_et(utc)
        # --- SELF-PROOF (c): the fixed iteration genuinely converged -------
        assert last_update_s <= CONVERGENCE_TOL_S, (
            f"convergence violated at {label}: third-pass update "
            f"{last_update_s} s exceeds {CONVERGENCE_TOL_S}")
        d_at = _delta_at_s(utc)
        # --- SELF-PROOF (b): round-trip identity ---------------------------
        back_s = et_to_calendar_seconds(et_s, d_at)
        naive_s = _calendar_seconds_past_j2000(utc)
        assert abs(back_s - naive_s) <= ROUNDTRIP_TOL_S, (
            f"round-trip violated at {label}: {back_s} vs {naive_s} "
            f"(residual {back_s - naive_s} s)")
        et_values.append((label, et_s))
        rows.append({"epoch_label": label,
                     "utc": utc,
                     "delta_at_s": d_at,
                     "et_seconds_past_j2000_s": round(et_s, ROUND_DECIMALS)})

    # --- SELF-PROOF (a): the J2000 anchor and the modern DELTA_AT ----------
    j2000_offset_s = et_values[0][1]
    assert J2000_ANCHOR_BAND_S[0] <= j2000_offset_s <= J2000_ANCHOR_BAND_S[1], (
        f"known-truth violated: ET at the J2000 UTC epoch is "
        f"{j2000_offset_s} s, outside the canonical {J2000_ANCHOR_BAND_S} band")
    for row in rows[1:]:                 # bounded: the 20 grid epochs
        assert row["delta_at_s"] == 37, (
            f"known-truth violated: DELTA_AT at {row['epoch_label']} is "
            f"{row['delta_at_s']}, not the table's in-force 37 s")
    # --- SELF-PROOF (c, continued): strict monotonicity over the grid ------
    for i in range(1, len(et_values) - 1):   # bounded: ordered grid pairs
        assert et_values[i + 1][1] > et_values[i][1], (
            f"monotonicity violated between {et_values[i][0]} and "
            f"{et_values[i + 1][0]}")

    return {
        "task_id": "task-0030-utc-tdb-conversion",
        "inputs": {
            "delta_t_a_s": DELTA_T_A_S,
            "k_s": K_S,
            "eb_rad": EB_RAD,
            "m0_rad": M0_RAD,
            # M1 = 1.99096871e-7 rad/s has significant digits below the
            # six-decimal boundary; emitted as an exact integer scaled by a
            # stated power of ten per MIP-0009 C3 (the computation uses the
            # pinned float): M1 = 199096871 x 10^-15 rad/s
            "m1_scaled_integer_dimensionless": 199096871,
            "m1_scale_exponent_dimensionless": -15,
            "leap_second_entries_count": len(DELTA_AT_TABLE),
            "lsk_provenance": "naif0012.tls, sha256 "
                              + _src.FETCH_PROVENANCE["kernels"]["naif0012.tls"],
            "naif_rules_note": _src.FETCH_PROVENANCE["naif_rules_quoted"],
            "formulation": "ET-UTC = DELTA_AT + DELTA_T_A + K sin(E); "
                           "E = M + EB sin M; M = M0 + M1 t (the kernel's "
                           "own documented DELTET relation)",
            "refinement_passes_count": REFINEMENT_PASSES,
            "round_decimals": ROUND_DECIMALS,
        },
        "results": rows,
        "summary": {
            "epochs_converted_count": len(rows),
            "j2000_et_minus_utc_s": round(j2000_offset_s, ROUND_DECIMALS),
            "grid_delta_at_s": 37,
            "anchor_note": "the J2000 control epoch reproduces the canonical "
                           "~64.184 s ET-UTC offset from pinned constants "
                           "alone — derived, not quoted",
            "self_proofs_checked": ["j2000_anchor_and_delta_at",
                                    "round_trip_identity_x21",
                                    "convergence_and_monotonicity"],
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
