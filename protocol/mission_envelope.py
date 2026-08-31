# Copyright (c) 2023-2026 MetaCoin-Lab.
# Licensed under SML-1.0 — see LICENSE.md.
"""mission_envelope.py — THE FEASIBILITY ENVELOPE: from an anchored mission
verdict's own flip structure, the minimal parameter set under which the
verdict would flip — anchored as a SECOND record, honestly labeled.

================== HONESTY / SCOPE (READ ME) ==================
Research-stage, ZERO-VALUE, no token, no money, no mainnet, no payments.

THE LABEL, VERBATIM ON EVERY RECORD: "engineered scenario: the parameter
set under which the claim becomes feasible — not a claim about present
capability". The envelope answers "what would it take?" from the anchored
chain's own structure; it asserts nothing about what exists. Every
present-day comparator is PINNED with provenance (document-cited tier —
the largest demonstrated electromagnetic-launcher shots, the only
flight-demonstrated ISRU production rate, the thinnest flown sail films)
so each envelope parameter is a stated MULTIPLE of something real.

THE BASELINE STANDS UNTOUCHED, MECHANICALLY: generation re-derives the
anchored mission-0002 verdict bit-exact and REFUSES to produce an envelope
if the recomputed verdict hash differs from the anchored record — the
envelope is derived FROM the baseline, never a re-tuning of it. Node tasks
are re-run live and hash-checked against their anchored records the same
way (the mission chain's own discipline).

WHAT THE ENVELOPE FOUND (derived, not asserted — the record carries it):
  * launch throughput FLIPS it: 3.743403x the stated 1 t/min — minimally
    FOUR drivers of the stated class; each baseline shot is ~129x the
    largest demonstrated EM-launcher shot energy, at a sustained cadence
    never demonstrated;
  * the acceptable horizon FLIPS it at 187.17 yr — a policy constant, so
    it carries no capability anchor and says so;
  * film areal density CANNOT flip it: duration(sigma) is bounded below
    (~178.75 yr at ~22 g/m2, bounded ternary search) because a lighter
    sail equilibrates farther sunward and required area grows faster than
    mass falls — thinner film defeats itself; the lever is closed by
    geometry, not manufacturing;
  * ISRU is a co-requirement, not a lever: the flip needs in-situ metal
    production ~1.9e7x the only flight-demonstrated ISRU rate;
  * THE DUST NODE (task-0028) STAYS FALSE UNDER EVERY PARAMETER ABOVE —
    the L1 e-folding is the geometry of the equilibrium itself, so the
    claim's 'temporary dust shade' variant has NO envelope, and the record
    is REQUIRED to say so (validation refuses an envelope without the
    dust note).

NO TIMESTAMPS IN THE HASHED ARTIFACT; era-2 canonical form; envelope_hash
is the anti-circularity self-hash. ANCHORING: external_verifier
--anchor-mission-envelope ENVELOPE_JSON --confirm (the human gate); the
coordinator re-derives the whole document bit-exact first. Record class by
the pulse/mission-verdict precedent: protocol module + --confirm path +
verify_everything layer + sweep evidence expectation; no MIP (it ratifies
no new capability — it reads records the chain already carries).

Usage:
    python3 protocol/mission_envelope.py --generate [--out mission_envelope.json]
    python3 protocol/mission_envelope.py --verify mission_envelope.json
    python3 protocol/mission_envelope.py --status
    python3 protocol/mission_envelope.py --selftest
Standard library only. Not financial, legal, or flight-engineering advice.
No NASA affiliation or endorsement.
"""

import sys
sys.dont_write_bytecode = True

import argparse
import hashlib
import json
import math
import os

_PROTO_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_PROTO_DIR)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from protocol.prov_export import resolve_ledger_path
from protocol.work_molecule import _read_ledger, find_evidence_file
import protocol.mission_chain as mission_chain
import protocol.verifier_cli as verifier_cli

ENVELOPE_SCHEMA = "mission_envelope/0.1"
ENVELOPE_EVENT = "mission_envelope_recorded"
ENVELOPE_STATUS = "mission-envelope-confirmed"
TARGET_MISSION_ID = mission_chain.MISSION_ID_0002

LABEL = ("engineered scenario: the parameter set under which the claim "
         "becomes feasible — not a claim about present capability")

REFUSAL_RULE = ("an envelope derives FROM the anchored baseline, never "
                "re-tunes it — generation refuses unless the baseline "
                "verdict and every node task re-derive their anchored "
                "hashes bit-exact")

DUST_NOTE = ("task-0028 stays FALSE under every parameter in this "
             "envelope: the ~23-day L1 e-folding is the geometry of the "
             "equilibrium itself, not a tunable constant — the claim's "
             "'temporary dust shade' variant has no feasibility envelope")

# Present-day demonstrated comparators, PINNED (document-cited tier — the
# access discipline of demo/tasks/pinned_sunshade_sources.py; these are
# comparators for the envelope record, consumed by no task module).
PRESENT_DAY_ANCHORS = {
    "em_launcher": {
        "tier": "document-cited",
        "description": ("largest demonstrated electromagnetic-launcher "
                        "shots: U.S. Navy railgun program, ~32 MJ muzzle-"
                        "energy class, ~10 kg projectiles at ~2.4 km/s, "
                        "single-shot demonstrations (program wound down "
                        "2021; public DoD/ONR reporting)"),
        "muzzle_energy_MJ": 32.0,
    },
    "isru_rate": {
        "tier": "document-cited",
        "description": ("the only flight-demonstrated in-situ resource "
                        "production rate of any species: MOXIE (Mars 2020 "
                        "rover, NASA/JPL), peak ~12 g/hr O2, ~122 g total "
                        "(public NASA reporting); the comparison to lunar "
                        "aluminum is cross-species and says so"),
        "peak_rate_g_per_hr": 12.0,
    },
    "sail_film": {
        "tier": "document-cited",
        "description": ("thinnest flown solar-sail membranes: 4.5 um "
                        "aluminized Mylar, LightSail 2 (The Planetary "
                        "Society, 2019), film-only ~6.3 g/m2; 7.5 um "
                        "polyimide, IKAROS (JAXA, 2010)"),
        "film_only_g_per_m2": 6.3,
    },
}

MAX_SEARCH_ITER = 200            # the stated bound for every search loop
SIGMA_SEARCH_RANGE_KG_M2 = (0.004, 0.06)
HOURS_PER_JULIAN_YEAR = 365.25 * 24.0
ROUND_DECIMALS = 6


# ----------------------------------------------------------------------------
# canonical form (era-2)
# ----------------------------------------------------------------------------
def _sign_safe_zero(obj):
    return json.loads(json.dumps(obj),
                      parse_float=lambda t: 0.0 if float(t) == 0.0 else float(t))


def canonical_json(obj) -> str:
    return json.dumps(_sign_safe_zero(obj), sort_keys=True,
                      separators=(",", ":"), ensure_ascii=True)


def envelope_hash(doc: dict) -> str:
    body = {k: v for k, v in doc.items() if k != "envelope_hash"}
    return hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()


# ----------------------------------------------------------------------------
# derivation (bounded, deterministic — every input hash-checked)
# ----------------------------------------------------------------------------
def _anchored_record(entries, mission_id):
    """(ledger_index, payload) of the latest anchored verdict for the mission."""
    found = (None, None)
    for e in entries:
        p = e.get("payload", {})
        if (p.get("event") == mission_chain.MISSION_EVENT
                and p.get("status") == mission_chain.MISSION_STATUS
                and p.get("mission_id") == mission_id):
            found = (e["index"], p)
    return found


def rederive(entries) -> dict:
    """The whole envelope from scratch: re-prove the baseline bit-exact,
    re-run the node tasks, run the bounded searches, assemble. Raises
    ValueError under the refusal rule."""
    # --- the baseline stands untouched, mechanically -----------------------
    idx, payload = _anchored_record(entries, TARGET_MISSION_ID)
    if idx is None:
        raise ValueError(f"REFUSED ({REFUSAL_RULE}): no anchored "
                         f"{TARGET_MISSION_ID} verdict on the chain")
    baseline = mission_chain.rederive(entries, TARGET_MISSION_ID)
    if baseline["verdict_hash"] != payload.get("verdict_hash"):
        raise ValueError(f"REFUSED ({REFUSAL_RULE}): baseline re-derives to "
                         f"{baseline['verdict_hash'][:12]} but idx {idx} "
                         f"anchored {str(payload.get('verdict_hash'))[:12]}")

    # Node artifacts, re-run live (their hashes were just proved inside the
    # baseline re-derivation; read the figures from fresh compute()s).
    t22 = verifier_cli.load_task("task-0022").compute()
    geom = verifier_cli.load_task("task-0023")
    t26 = verifier_cli.load_task("task-0026").compute()
    t27 = verifier_cli.load_task("task-0027").compute()
    t25 = verifier_cli.load_task("task-0025").compute()
    t24 = verifier_cli.load_task("task-0024").compute()

    f_required = float(t22["summary"]["reference_required_fraction"])
    thru_kg_yr = float(t26["summary"]["throughput_t_per_yr"]) * 1e3
    duration_yr = float(t26["summary"]["deployment_duration_yr"])
    horizon_yr = float(t27["inputs"]["acceptable_horizon_yr"])
    overrun = float(t27["summary"]["overrun_factor_ratio"])
    shot_mass_kg = float(t26["inputs"]["shot_mass_kg"])
    cadence_hr = float(t26["inputs"]["cadence_shots_per_hr"])
    e_kinetic_mj_kg = float(t26["results"][1]["kinetic_energy_MJ"])
    film_mass_t = float(t24["summary"]["total_shade_mass_t"])
    regolith_million_t = float(
        t25["summary"]["regolith_processed_million_t"])
    power_mw = float(t25["summary"]["sustained_power_MW"])

    # LEVER 1 — launch throughput (the one that flips).
    drivers_needed = math.ceil(overrun)          # minimal integer drivers
    duration_at_flip_yr = duration_yr / drivers_needed
    assert duration_at_flip_yr <= horizon_yr, (
        "envelope arithmetic violated: the minimal integer driver count "
        "does not reach the horizon")
    per_shot_mj = e_kinetic_mj_kg * shot_mass_kg
    shot_multiple = per_shot_mj / PRESENT_DAY_ANCHORS[
        "em_launcher"]["muzzle_energy_MJ"]

    # LEVER 2 — film areal density (proved closed by geometry): minimize
    # duration(sigma) by bounded ternary search using task-0023's own
    # public equilibrium solver — the frozen anchored code, re-parametrized.
    omega_sun_sr = math.pi * (geom.R_SUN_M / geom.AU_M) ** 2

    def duration_at_sigma_yr(sigma):
        r_m, _it, _res = geom.solve_equilibrium(sigma)
        d_m = geom.AU_M - r_m
        return f_required * omega_sun_sr * d_m * d_m * sigma / thru_kg_yr

    lo, hi = SIGMA_SEARCH_RANGE_KG_M2
    for _ in range(MAX_SEARCH_ITER):    # bounded by MAX_SEARCH_ITER
        m1 = lo + (hi - lo) / 3.0
        m2 = hi - (hi - lo) / 3.0
        if duration_at_sigma_yr(m1) < duration_at_sigma_yr(m2):
            hi = m2
        else:
            lo = m1
        if hi - lo <= 1e-9:
            break
    sigma_min = 0.5 * (lo + hi)
    duration_floor_yr = duration_at_sigma_yr(sigma_min)
    # the floor must genuinely exceed the horizon, or the finding is wrong
    assert duration_floor_yr > horizon_yr, (
        f"envelope finding violated: duration floor {duration_floor_yr} yr "
        f"does not exceed the horizon {horizon_yr} yr — the sigma lever "
        "would flip after all")

    # LEVER 4 — ISRU co-requirement at the flipped (horizon-length) deployment.
    al_rate_t_yr = film_mass_t / horizon_yr
    regolith_rate_t_yr = regolith_million_t * 1e6 / horizon_yr
    moxie_g_yr = (PRESENT_DAY_ANCHORS["isru_rate"]["peak_rate_g_per_hr"]
                  * HOURS_PER_JULIAN_YEAR)
    isru_multiple = al_rate_t_yr * 1e6 / moxie_g_yr

    doc = {
        "schema": ENVELOPE_SCHEMA,
        "mission_id": TARGET_MISSION_ID,
        "label": LABEL,
        "baseline": {
            "verdict_hash": baseline["verdict_hash"],
            "anchored_ledger_index": idx,
            "untouched_note": "re-derived bit-exact against the anchored "
                              "record before this envelope was assembled; "
                              "nothing in the baseline chain was re-tuned",
        },
        "flip_scope_note": ("mission_feasible is the AND over three "
                            "constraining nodes; this envelope flips the "
                            "parameter-dependent one (task-0027) for the "
                            "CONTROLLED-SHADE architecture, with task-0029 "
                            "already holding conditionally — " + DUST_NOTE),
        "levers": [
            {"lever": "launch_throughput", "flips": True,
             "baseline_throughput_t_per_yr": round(thru_kg_yr / 1e3,
                                                   ROUND_DECIMALS),
             "required_throughput_multiple_ratio": round(overrun,
                                                         ROUND_DECIMALS),
             "minimal_integer_driver_count": drivers_needed,
             "deployment_at_flip_yr": round(duration_at_flip_yr,
                                            ROUND_DECIMALS),
             "present_day_anchor": PRESENT_DAY_ANCHORS["em_launcher"],
             "per_shot_energy_MJ": round(per_shot_mj, ROUND_DECIMALS),
             "per_shot_multiple_of_demonstrated_ratio": round(
                 shot_multiple, ROUND_DECIMALS),
             "honesty_note": "each baseline shot is ~129x the largest "
                             "demonstrated EM-launch shot energy, at a "
                             "sustained once-a-minute cadence with no "
                             "demonstrated analogue (single shots only); "
                             "the flip needs FOUR such drivers"},
            {"lever": "film_areal_density", "flips": False,
             "finding": "NO FLIP EXISTS: deployment duration over areal "
                        "density is bounded below — a lighter sail "
                        "equilibrates farther sunward and the required "
                        "area grows as distance squared faster than mass "
                        "falls; thinner film defeats itself (closed by "
                        "geometry, not manufacturing)",
             "duration_floor_yr": round(duration_floor_yr, ROUND_DECIMALS),
             "floor_sigma_g_per_m2": round(sigma_min * 1e3, ROUND_DECIMALS),
             "search_range_g_per_m2": [round(s * 1e3, ROUND_DECIMALS)
                                       for s in SIGMA_SEARCH_RANGE_KG_M2],
             "present_day_anchor": PRESENT_DAY_ANCHORS["sail_film"]},
            {"lever": "acceptable_horizon", "flips": True,
             "required_horizon_yr": round(duration_yr, ROUND_DECIMALS),
             "multiple_of_stated_ratio": round(overrun, ROUND_DECIMALS),
             "honesty_note": "a policy constant, not a capability — no "
                             "demonstrated-value anchor exists; expressed "
                             "against the stated 50-yr baseline only"},
            {"lever": "isru_production_rate", "flips": False,
             "role": "co-requirement of the throughput flip, not an "
                     "independent lever",
             "required_film_metal_t_per_yr": round(al_rate_t_yr,
                                                   ROUND_DECIMALS),
             "required_regolith_t_per_yr": round(regolith_rate_t_yr,
                                                 ROUND_DECIMALS),
             "required_sustained_power_MW": round(power_mw, ROUND_DECIMALS),
             "present_day_anchor": PRESENT_DAY_ANCHORS["isru_rate"],
             "multiple_of_demonstrated_ratio": round(isru_multiple,
                                                     ROUND_DECIMALS),
             "honesty_note": "~1.9e7x the only flight-demonstrated ISRU "
                             "rate (cross-species comparison, stated)"},
        ],
        "combined_minimal_scenario": {
            "driver_count": drivers_needed,
            "deployment_yr": round(duration_at_flip_yr, ROUND_DECIMALS),
            "isru_film_metal_t_per_yr": round(al_rate_t_yr, ROUND_DECIMALS),
            "dust_variant": "remains refuted (see flip_scope_note)",
            "longevity_conditions": "carried unchanged from task-0029: "
                                    "grow the shade ~8.64x over the Gyr, "
                                    "~2% margin at the stated ceiling",
        },
        "refusal_rule": REFUSAL_RULE,
        "no_timestamps_note": "hashed artifact: no wall-clock fields; the "
                              "anchoring record's anchored_at dates this "
                              "envelope",
        "zero_value": True,
        "no_token": True,
    }
    doc["envelope_hash"] = envelope_hash(doc)
    return doc


def headline(doc: dict) -> dict:
    """On-chain numbers (scanner-invisible: no task_id/task_ids keys)."""
    levers = {v["lever"]: v["flips"] for v in doc["levers"]}
    launch = next(v for v in doc["levers"]
                  if v["lever"] == "launch_throughput")
    return {
        "label": doc["label"],
        "lever_flips": levers,
        "minimal_driver_count": launch["minimal_integer_driver_count"],
        "throughput_multiple": launch["required_throughput_multiple_ratio"],
        "per_shot_multiple_of_demonstrated":
            launch["per_shot_multiple_of_demonstrated_ratio"],
        "isru_multiple_of_demonstrated": next(
            v for v in doc["levers"]
            if v["lever"] == "isru_production_rate"
        )["multiple_of_demonstrated_ratio"],
        "duration_floor_yr_over_areal_density": next(
            v for v in doc["levers"]
            if v["lever"] == "film_areal_density")["duration_floor_yr"],
    }


def validate_envelope(doc) -> tuple:
    """(ok, reasons): shape, self-hash, the verbatim label, and the
    REQUIRED dust note (an envelope may not go quiet about the variant
    that has no envelope)."""
    reasons = []
    if not isinstance(doc, dict) or doc.get("schema") != ENVELOPE_SCHEMA:
        return (False, ["not a mission_envelope/0.1 document"])
    for key in ("mission_id", "label", "baseline", "levers",
                "flip_scope_note", "combined_minimal_scenario",
                "envelope_hash"):
        if key not in doc:
            reasons.append(f"missing {key}")
    if reasons:
        return (False, reasons)
    if envelope_hash(doc) != doc["envelope_hash"]:
        reasons.append("envelope_hash does not recompute from the document")
    if doc["label"] != LABEL:
        reasons.append("the verbatim engineered-scenario label is missing "
                       "or altered")
    if DUST_NOTE not in doc.get("flip_scope_note", ""):
        reasons.append("the required dust-variant note is missing — the "
                       "record must say which part of the claim has no "
                       "envelope")
    if not isinstance(doc["baseline"].get("verdict_hash"), str):
        reasons.append("baseline verdict_hash missing")
    flips = [v.get("flips") for v in doc["levers"]]
    if True not in flips:
        reasons.append("an envelope with no flipping lever is a verdict, "
                       "not an envelope")
    return (not reasons, reasons)


# ----------------------------------------------------------------------------
# status (the docs' verify block)
# ----------------------------------------------------------------------------
def status(entries=None, echo=print) -> int:
    entries = (entries if entries is not None
               else _read_ledger(resolve_ledger_path()))
    recs = [e for e in entries
            if e.get("payload", {}).get("event") == ENVELOPE_EVENT
            and e["payload"].get("status") == ENVELOPE_STATUS]
    if not recs:
        echo("ENVELOPE STATUS: OK — no mission envelope anchored on the "
             "chain yet (named)")
        return 0
    rec = recs[-1]
    p = rec["payload"]
    path = find_evidence_file(
        f"mission_envelope_{p['envelope_hash'][:12]}.json")
    if path is None:
        echo(f"ENVELOPE STATUS: BROKEN — idx {rec['index']} cites envelope "
             f"{p['envelope_hash'][:12]} but no evidence file ships")
        return 1
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    ok, reasons = validate_envelope(doc)
    try:
        exact = canonical_json(rederive(entries)) == canonical_json(doc)
    except ValueError as exc:
        exact = False
        reasons.append(str(exc)[:160])
    if ok and exact and doc["envelope_hash"] == p["envelope_hash"]:
        echo(f"ENVELOPE STATUS: OK — idx {rec['index']} re-derives "
             f"BIT-EXACT (baseline re-proved, searches re-run); the "
             "engineered-scenario label and the dust-variant refusal ride "
             "the record")
        return 0
    echo("ENVELOPE STATUS: BROKEN — " + "; ".join(
        reasons + ([] if exact else ["re-derivation is not bit-exact"])))
    return 1


# ----------------------------------------------------------------------------
# self-test (the real derivation, cheap + fixture tampering; no writes)
# ----------------------------------------------------------------------------
def _selftest() -> int:
    import copy
    print("=== protocol/mission_envelope.py self-test (read-only; no ledger "
          "writes) ===")
    print("An envelope derives FROM the anchored baseline, never re-tunes "
          "it.\n")
    checks = []
    entries = _read_ledger(resolve_ledger_path())
    doc = rederive(entries)
    doc2 = rederive(entries)
    checks.append(("the real envelope derives and validates",
                   validate_envelope(doc)[0]))
    checks.append(("deterministic: same chain -> same bytes and hash",
                   canonical_json(doc) == canonical_json(doc2)
                   and doc["envelope_hash"] == doc2["envelope_hash"]))
    checks.append(("the verbatim engineered-scenario label rides the record",
                   doc["label"] == LABEL))
    checks.append(("the dust-variant refusal is present and REQUIRED",
                   DUST_NOTE in doc["flip_scope_note"]))
    checks.append(("the baseline binding carries the anchored verdict hash",
                   doc["baseline"]["verdict_hash"]
                   == mission_chain.rederive(
                       entries, TARGET_MISSION_ID)["verdict_hash"]))
    checks.append(("throughput flips, areal density provably cannot",
                   next(v for v in doc["levers"]
                        if v["lever"] == "launch_throughput")["flips"] is True
                   and next(v for v in doc["levers"]
                            if v["lever"] == "film_areal_density")["flips"]
                   is False))
    def _keys(obj):
        stack = [obj]
        while stack:                      # bounded: finite document
            o = stack.pop()
            if isinstance(o, dict):
                for k, v in o.items():
                    yield k
                    stack.append(v)
            elif isinstance(o, list):
                stack.extend(o)
    checks.append(("no wall-clock field anywhere in the hashed document",
                   not any(k in ("anchored_at", "evaluated_at", "timestamp")
                           or k.startswith("as_of") for k in _keys(doc))))
    checks.append(("headline is scanner-invisible (no task_id/task_ids)",
                   "task_id" not in json.dumps(headline(doc))
                   and "task_ids" not in json.dumps(headline(doc))))
    tampered = copy.deepcopy(doc)
    tampered["levers"][0]["minimal_integer_driver_count"] = 1
    checks.append(("a tampered envelope fails validation (self-hash broken)",
                   not validate_envelope(tampered)[0]))
    unlabeled = copy.deepcopy(doc)
    unlabeled["label"] = "feasible now"
    unlabeled["envelope_hash"] = envelope_hash(unlabeled)
    checks.append(("a re-hashed envelope without the verbatim label still "
                   "fails validation (the label binds, not only the hash)",
                   not validate_envelope(unlabeled)[0]))
    silent = copy.deepcopy(doc)
    silent["flip_scope_note"] = "all good"
    silent["envelope_hash"] = envelope_hash(silent)
    checks.append(("an envelope silent about the dust variant is refused",
                   not validate_envelope(silent)[0]))
    # a drifted baseline refuses: rerun against a truncated chain (the
    # anchored verdict record absent -> refusal by name)
    try:
        rederive(entries[:83])
        refused = False
    except ValueError as exc:
        refused = "REFUSED" in str(exc)
    checks.append(("a chain without the anchored baseline verdict refuses "
                   "by name", refused))
    out = []
    checks.append(("status names 'no envelope yet' as OK on a chain "
                   "without one",
                   status([{"index": 0, "payload": {"event": "x"}}],
                          echo=out.append) == 0 and "no mission envelope"
                   in out[0]))
    print("--- self-test invariants ---")
    failures = 0
    for name, passed in checks:
        print(f"{name:72s}: {'PASS' if passed else 'FAIL'}")
        failures += not passed
    ok = failures == 0
    print("\n=== self-test summary: "
          + ("ALL CHECKS BEHAVED CORRECTLY" if ok else "FAILURE — see above")
          + " ===")
    return 0 if ok else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="The mission feasibility envelope (research-stage, "
                    "ZERO-VALUE, no token): from the anchored mission-0002 "
                    "verdict's own flip structure, the minimal parameter "
                    "set under which it would flip — an engineered "
                    "scenario, never a claim about present capability.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--generate", action="store_true",
                      help="re-prove the baseline, run the bounded "
                           "searches, write the envelope (refuses on any "
                           "baseline drift)")
    mode.add_argument("--verify", metavar="ENVELOPE_JSON",
                      help="re-derive from this chain and compare bit-exact")
    mode.add_argument("--status", action="store_true")
    mode.add_argument("--selftest", action="store_true")
    parser.add_argument("--out",
                        default=os.path.join(_REPO_ROOT,
                                             "mission_envelope.json"))
    args = parser.parse_args(argv)
    if args.generate:
        try:
            doc = rederive(_read_ledger(resolve_ledger_path()))
        except ValueError as exc:
            print(str(exc))
            print("mission envelope: NOT generated (nothing written)")
            return 1
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(canonical_json(doc) + "\n")
        print(f"envelope written: {args.out}")
        print(f"envelope_hash  : {doc['envelope_hash']}")
        print("headline       : " + json.dumps(headline(doc),
                                               sort_keys=True))
        return 0
    if args.verify:
        with open(args.verify, encoding="utf-8") as f:
            given = json.load(f)
        ok, reasons = validate_envelope(given)
        print("file validates: " + ("yes" if ok else "; ".join(reasons)))
        try:
            fresh = rederive(_read_ledger(resolve_ledger_path()))
        except ValueError as exc:
            print(str(exc))
            print("ENVELOPE VERIFY: FAILED")
            return 1
        if ok and canonical_json(fresh) == canonical_json(given):
            print(f"ENVELOPE VERIFY: MATCH — bit-exact re-derivation, "
                  f"envelope_hash {fresh['envelope_hash'][:12]}")
            return 0
        print("ENVELOPE VERIFY: DIFFERS")
        return 1
    if args.status:
        return status()
    return _selftest()


if __name__ == "__main__":
    sys.exit(main())
