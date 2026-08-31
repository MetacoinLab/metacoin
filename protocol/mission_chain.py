# Copyright (c) 2023-2026 MetaCoin-Lab.
# Licensed under SML-1.0 — see LICENSE.md.
"""mission_chain.py — THE ANCHORED MISSION VERIFICATION CHAIN: the protocol's
first MISSION-level verdict, a typed DAG over anchored tasks whose conclusion
is itself an anchored, bit-exact re-derivable object.

================== HONESTY / SCOPE (READ ME) ==================
Research-stage, ZERO-VALUE, no token, no money, no mainnet, no payments.

PURPOSE. Every anchored task computes one piece of physics; nobody — including
the tools that compute the pieces — anchors the WHOLE mission conclusion
cryptographically. This module formalizes "Mars ISRU refuel-and-ascent"
(mission-0001) as a typed DAG over EXISTING anchored tasks, derives the
mission-level verdict from what those tasks already say, and emits a
mission_verdict/0.1 document in era-2 canonical form with a self-hash. The
verifier re-derives the ENTIRE verdict from the chain — re-run every node
task, rebuild the DAG, recompute the document — bit-exact or refuse.

THE HEADLINE IS AN HONEST NEGATIVE, KEPT ON PURPOSE: mission_feasible is the
AND over the constraining nodes, and it is FALSE — the ascent does not close
(task-0018, short ~2489 m/s), it closes even less at the thermodynamically
honest conversion (task-0021, short ~2689 m/s), the reactor operating point
fails the chain's assumed conversion (task-0020, short 0.109), and the
Mars-class telemetry link does not close (task-0012, −1.64 dB). NO PARAMETER
IS TUNED TO FORCE A STORY — the chain computes what the anchored tasks
already say, and a verification protocol must be comfortable anchoring "no"
at mission level exactly as it does at task level.

THE REFUSAL RULE (the pulse's rule, adapted): a mission-level FALSE is a
deliverable; a DRIFTED NODE is not. Generation refuses — writing nothing —
if any node's live recompute mismatches its anchored ledger hash, if the DAG
fails its own typing (unknown endpoint, unknown edge type, cycle, a feeds
edge with no executable PARENT_TASKS declaration behind it), or if a
constraining node's verdict field is missing.

THE EDGE TYPES (the established trio):
  feeds      — the child consumes the parent's published values at execution
               time and asserts the parent's pinned hash live (the executable
               provenance edge; every feeds edge here is backed by a
               PARENT_TASKS declaration, and the builder refuses one that is
               not);
  informs    — the node's finding shapes the mission analysis (here: the
               flip-condition classes) without gating the verdict;
  constrains — the node's verdict field gates mission_feasible (the AND).

NO TIMESTAMPS IN THE HASHED ARTIFACT (the house rule for hashed objects):
the document is a deterministic function of (task modules, DAG definition,
per-node anchored records). Its date is the anchoring record's own
`anchored_at`. Per-node anchored references are FIRST-RECORD indices — stable
under any later append — so the same chain always re-derives the same bytes.

CANONICAL FORM. `verdict_hash` = sha256 of the era-2 canonical JSON (sorted
keys, compact separators, ASCII, sign-of-zero-free — ledger idx 67) of the
document WITHOUT its own `verdict_hash` field (the anti-circularity self-hash
the pulse record established).

ANCHORING. `protocol/external_verifier.py --anchor-mission-verdict
mission_verdict.json --confirm` re-derives the whole verdict from this chain
(never trusts the file), compares bit-exact, and anchors
`mission_verdict_recorded` with the verdict hash + quantified headline; the
file ships as `protocol/evidence/mission_verdict_<hash12>.json`. The record
class enters by PRECEDENT (passport catalog idx 40, mirror attestation idx
72, pulse idx 80): protocol code + a coordinator --confirm path + a
verify_everything layer + a sweep evidence expectation, no MIP required for a
record class that ratifies no new capability — every node, edge, and law this
verdict composes is already anchored. The anchored payload carries NO
top-level task_id/task_ids keys (scanner-invisible, the metering/pulse
precedent), so frozen molecule-catalog generations never see it.

Usage:
    python3 protocol/mission_chain.py --generate [--out mission_verdict.json]
    python3 protocol/mission_chain.py --verify mission_verdict.json
    python3 protocol/mission_chain.py --status
    python3 protocol/mission_chain.py --selftest      # fixtures only
Standard library only. Not financial, legal, or flight-engineering advice.
No NASA affiliation or endorsement.
"""

import sys
sys.dont_write_bytecode = True

import argparse
import hashlib
import json
import os

_PROTO_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_PROTO_DIR)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from protocol.prov_export import resolve_ledger_path
from protocol.work_molecule import _read_ledger, find_evidence_file
import protocol.verifier_cli as verifier_cli

MISSION_SCHEMA = "mission_verdict/0.1"
MISSION_ID = "mission-0001-mars-isru-refuel-ascent"
MISSION_EVENT = "mission_verdict_recorded"
MISSION_STATUS = "mission-verdict-confirmed"
MISSION_SINK = "mission-0001"          # the DAG's sink pseudo-node
EDGE_TYPES = ("feeds", "informs", "constrains")

REFUSAL_RULE = ("a mission-level FALSE is a deliverable; a drifted node is "
                "not — generation refuses unless every node re-derives its "
                "anchored hash and the DAG type-checks")

# ----------------------------------------------------------------------------
# THE MISSION DEFINITION: mission-0001 "Mars ISRU refuel-and-ascent".
# Nodes are EXISTING anchored tasks; roles: 'upstream' (feeds the chain, no
# verdict field) or 'constraining' (its verdict field gates the AND).
# ----------------------------------------------------------------------------
NODES = (
    {"task": "task-0015", "role": "upstream",
     "title": "Sabatier ISRU single-pass propellant mass balance (TX07)"},
    {"task": "task-0019", "role": "upstream",
     "title": "CEA-pinned Sabatier equilibrium constant K_eq(T) (TX07)"},
    {"task": "task-0017", "role": "upstream",
     "title": "ISRU ascent propellant budget at the assumed conversion (TX01)"},
    {"task": "task-0020", "role": "constraining",
     "verdict_field": "reference_conversion_acceptable",
     "title": "Sabatier equilibrium conversion vs the chain's assumption (TX07)"},
    {"task": "task-0018", "role": "constraining",
     "verdict_field": "feasible",
     "title": "Mars ascent feasibility at the assumed conversion (TX17)"},
    {"task": "task-0021", "role": "constraining",
     "verdict_field": "feasible_at_equilibrium_conversion",
     "title": "Mars ascent feasibility at the equilibrium conversion (TX01)"},
    {"task": "task-0012", "role": "constraining",
     "verdict_field": "link_closes",
     "title": "Mars-class X-band telemetry link budget (TX05)"},
)

# Each edge justified in one line. Every `feeds` edge is an EXECUTABLE
# provenance edge (child recomputes parent live and asserts the pinned hash);
# the builder mechanically refuses a feeds edge not backed by the child
# module's PARENT_TASKS declaration, and a declaration the DAG omits.
EDGES = (
    {"src": "task-0015", "dst": "task-0017", "type": "feeds",
     "justification": "task-0017 consumes task-0015's published CH4/H2O "
                      "product masses, parent recomputed live and hash-asserted"},
    {"src": "task-0017", "dst": "task-0018", "type": "feeds",
     "justification": "task-0018 consumes task-0017's published achievable "
                      "delta-v, parent recomputed live and hash-asserted"},
    {"src": "task-0019", "dst": "task-0020", "type": "feeds",
     "justification": "task-0020 consumes task-0019's published ln K_eq(T) "
                      "grid, parent recomputed live and hash-asserted"},
    {"src": "task-0015", "dst": "task-0020", "type": "feeds",
     "justification": "task-0020 consumes task-0015's assumed single-pass "
                      "conversion as the judged threshold, hash-asserted"},
    {"src": "task-0020", "dst": "task-0021", "type": "feeds",
     "justification": "task-0021 consumes task-0020's equilibrium conversion "
                      "and threshold at the reference point, hash-asserted"},
    {"src": "task-0017", "dst": "task-0021", "type": "feeds",
     "justification": "task-0021 consumes task-0017's propellant budget and "
                      "fixed vehicle constants unchanged, hash-asserted"},
    {"src": "task-0018", "dst": "task-0021", "type": "feeds",
     "justification": "task-0021 consumes task-0018's published required and "
                      "assumed-conversion achievable delta-v, hash-asserted"},
    {"src": "task-0020", "dst": MISSION_SINK, "type": "constrains",
     "justification": "the reactor operating point must satisfy the "
                      "conversion the upstream mass balance assumes"},
    {"src": "task-0018", "dst": MISSION_SINK, "type": "constrains",
     "justification": "the ascent must close at the assumed conversion"},
    {"src": "task-0021", "dst": MISSION_SINK, "type": "constrains",
     "justification": "the ascent must close at the thermodynamically honest "
                      "equilibrium conversion"},
    {"src": "task-0012", "dst": MISSION_SINK, "type": "constrains",
     "justification": "mission operations require the Mars-class telemetry "
                      "link to close at the fixed constants"},
    {"src": "task-0017", "dst": MISSION_SINK, "type": "informs",
     "justification": "task-0017's binding-reactant finding (the oxidizer "
                      "binds) names the flip lever the analysis quotes — "
                      "supplementary oxygen production — without gating the "
                      "verdict"},
)

NAMED_GAP = ("no anchored Earth->Mars transfer or launch-window "
             "task exists yet — the DAG stops at the Mars surface "
             "and says so")

# Orbit/transfer tasks in the library that are NOT nodes, with the honest
# reason: their anchored constants are Earth-centered, and wiring them to a
# Mars surface mission would be narrative glue, refused by rule. The named
# gap: no anchored Earth->Mars transfer or launch-window task exists yet.
EXCLUDED_NODES = (
    {"task": "task-0002", "reason": "two-body propagation about EARTH "
                                    "(mu, radius Earth-pinned) — no physical "
                                    "edge to a Mars surface mission"},
    {"task": "task-0007", "reason": "Hohmann transfer LEO->GEO about EARTH — "
                                    "not an Earth->Mars transfer"},
    {"task": "task-0013", "reason": "Lambert solver on an Earth-centered "
                                    "reference case (Curtis 5.2) — not a "
                                    "Mars-bound trajectory"},
)

# What would flip each failed constraining node: parameter CLASSES drawn from
# the tasks' OWN sensitivity structure (their published inputs and findings),
# quoted by field name — never a tuned number, never new physics.
FLIP_CONDITIONS = {
    "task-0020": {
        "parameter_classes": [
            "operating temperature (task-0020's own grid: acceptable only at "
            "or below its max_acceptable_temperature_at_1_bar_K)",
            "operating pressure (the grid's 10 bar rows extend the acceptable "
            "envelope to max_acceptable_temperature_at_10_bar_K)",
            "the assumed threshold itself (task-0015's single_pass_conversion "
            "— a recycle loop, which task-0015 deliberately ignores)",
        ],
        "source": "task-0020 summary envelope fields + task-0015 inputs"},
    "task-0018": {
        "parameter_classes": [
            "ISRU throughput scale (task-0015 co2_feed_kg — the propellant "
            "load in the rocket-equation log argument)",
            "vehicle dry mass (task-0017 dry_mass_kg)",
            "engine Isp (task-0017 isp_s)",
            "supplementary oxygen production (task-0017's binding_reactant is "
            "the oxidizer — its own binding_note names CO2 electrolysis / "
            "water mining)",
            "requirement side: target altitude and loss allowances (task-0018 "
            "target_altitude_m, gravity_loss_m_s, steering_loss_m_s)",
        ],
        "source": "task-0017/0018 inputs + task-0017 binding finding"},
    "task-0021": {
        "parameter_classes": [
            "every task-0018 lever (the corrected budget reuses task-0017's "
            "vehicle constants unchanged)",
            "every task-0020 lever (the conversion scale is the bridge's "
            "multiplier: cooler or pressurized operation raises it)",
        ],
        "source": "task-0021 inherits both parents' sensitivity structures"},
    "task-0012": {
        "parameter_classes": [
            "data rate (task-0012 data_rate_bps — Eb/N0 rises dB-for-dB as "
            "the rate falls)",
            "transmit power / antenna gains (tx_power_w, tx_antenna_gain_dbi, "
            "rx_antenna_gain_dbi)",
            "range (distance_km — path loss)",
            "required threshold (required_ebn0_db — coding gain the budget "
            "deliberately omits)",
        ],
        "source": "task-0012 inputs (closed-form dB budget)"},
}


# ----------------------------------------------------------------------------
# MISSION-0002: "L1 sunshade via lunar mass driver" — the protocol's first
# CIVILIZATION-SCALE CLAIM DECOMPOSITION. A public, unverified feasibility
# assertion (recorded verbatim in the pinned-sources claim block) is
# decomposed into anchored tasks and judged by what they compute. Schema
# mission_verdict/0.2: the record class gains claim-under-verification
# provenance (claim_source) and an explicit not_modeled list.
# ----------------------------------------------------------------------------
MISSION_ID_0002 = "mission-0002-l1-sunshade-lunar-mass-driver"
MISSION_SCHEMA_0002 = "mission_verdict/0.2"

NODES_0002 = (
    {"task": "task-0022", "role": "upstream",
     "title": "Required insolation reduction from the pinned forcing "
              "target (TX14)"},
    {"task": "task-0023", "role": "upstream",
     "title": "Sub-L1 shade equilibrium and occulting area (TX17)"},
    {"task": "task-0024", "role": "upstream",
     "title": "Shade mass budget (TX12)"},
    {"task": "task-0025", "role": "upstream",
     "title": "Regolith feedstock, extraction energy, lunar power (TX07)"},
    {"task": "task-0026", "role": "upstream",
     "title": "Mass-driver launch energetics and throughput (TX01)"},
    {"task": "task-0027", "role": "constraining",
     "verdict_field": "deployable_within_horizon",
     "title": "Deployment timeline vs a climate-relevant horizon (TX01)"},
    {"task": "task-0028", "role": "constraining",
     "verdict_field": "dust_shade_persists",
     "title": "L1 dust-cloud persistence — the claim's 'temporary shade' "
              "aside (TX17)"},
    {"task": "task-0029", "role": "constraining",
     "verdict_field": "gyr_claim_within_ceiling",
     "title": "Longevity horizon vs the claimed billion years (TX14)"},
)

EDGES_0002 = (
    {"src": "task-0022", "dst": "task-0023", "type": "feeds",
     "justification": "task-0023 consumes task-0022's published required "
                      "fraction, parent recomputed live and hash-asserted"},
    {"src": "task-0023", "dst": "task-0024", "type": "feeds",
     "justification": "task-0024 consumes task-0023's published area and "
                      "areal density, hash-asserted"},
    {"src": "task-0024", "dst": "task-0025", "type": "feeds",
     "justification": "task-0025 consumes task-0024's published film mass "
                      "as the aluminum to be won from regolith, "
                      "hash-asserted"},
    {"src": "task-0024", "dst": "task-0026", "type": "feeds",
     "justification": "task-0026 consumes task-0024's published total mass "
                      "as the tonnage to be launched, hash-asserted"},
    {"src": "task-0026", "dst": "task-0027", "type": "feeds",
     "justification": "task-0027 consumes task-0026's published deployment "
                      "duration, hash-asserted"},
    {"src": "task-0022", "dst": "task-0029", "type": "feeds",
     "justification": "task-0029 consumes task-0022's published offset "
                      "target as the fraction the shade must hold, "
                      "hash-asserted"},
    {"src": "task-0027", "dst": MISSION_SINK, "type": "constrains",
     "justification": "the shade must deploy within a climate-relevant "
                      "horizon at the claim's stated launch architecture"},
    {"src": "task-0028", "dst": MISSION_SINK, "type": "constrains",
     "justification": "the claim's dust variant must persist usefully to "
                      "count as a shade at all"},
    {"src": "task-0029", "dst": MISSION_SINK, "type": "constrains",
     "justification": "the claimed billion-year horizon must survive solar "
                      "brightening within the stated occlusion ceiling"},
    {"src": "task-0025", "dst": MISSION_SINK, "type": "informs",
     "justification": "the GW-class lunar power and hundred-megatonne "
                      "feedstock scale shape the flip analysis without "
                      "gating the verdict"},
)

EXCLUDED_NODES_0002 = (
    {"task": "task-0013", "reason": "the Lambert solver is Earth-centered "
                                    "(Curtis 5.2) — not a Moon-to-sub-L1 "
                                    "transfer; task-0026 instead carries a "
                                    "stated 0.5 km/s insertion allowance, "
                                    "named as such"},
    {"task": "task-0002", "reason": "two-body propagation about Earth — no "
                                    "physical edge to shade station-keeping "
                                    "at an unstable libration point (a "
                                    "named non-modeled gap)"},
)

NAMED_GAP_0002 = ("no anchored Moon-to-sub-L1 trajectory task exists "
                  "(task-0026's transfer is a stated allowance), and no "
                  "station-keeping/controls task exists — the not_modeled "
                  "list carries the full honesty")

NOT_MODELED_0002 = (
    "station-keeping and attitude control at an unstable equilibrium — the "
    "deployed shade needs continuous control authority (the e-folding time "
    "task-0028 derives applies to anything uncontrolled there)",
    "thermal balance, film degradation, and replacement logistics over the "
    "horizon",
    "radiation and micrometeoroid damage to a ~30 g/m2 film",
    "transfer-trajectory fidelity beyond the stated 0.5 km/s insertion "
    "allowance",
    "climate-system response beyond global-mean energy balance (regional "
    "effects, feedbacks, efficacy)",
    "governance, liability, and unilateral-deployment questions — outside "
    "physics entirely",
)

FLIP_CONDITIONS_0002 = {
    "task-0027": {
        "parameter_classes": [
            "driver count (task-0026 driver_count — the ~3.75x overrun "
            "flips with ~4 parallel drivers at the stated shot and cadence)",
            "cadence and shot mass (task-0026 cadence_shots_per_hr, "
            "shot_mass_kg — the overrun factor divides through their "
            "product)",
            "film areal density (task-0023 "
            "shade_areal_density_kg_per_m2 — a thinner film scales the "
            "whole launched mass down linearly)",
            "the offset target itself (task-0022's pinned reference ERF — "
            "a smaller target shrinks area, mass, and years together)",
        ],
        "source": "task-0026 inputs + the upstream 0023/0022 constants"},
    "task-0028": {
        "parameter_classes": [
            "no passive parameter flips it: the ~23-day e-folding is the "
            "geometry of L1 itself (grain size only tunes the radiation-"
            "pressure eviction on top)",
            "continuous replenishment faster than the e-folding, or active "
            "station-keeping — which is no longer dust but the controlled "
            "shade the main chain already prices",
        ],
        "source": "task-0028's own structure: the instability is geometry, "
                  "not a tunable constant"},
    # task-0029's verdict is TRUE, so no flip entry appears in the record
    # (the builder lists flips for FAILED nodes only); its near-boundary
    # sensitivity is carried by its own figures (verdict_flip_ceiling).
}


def _claim_source():
    """The claim-under-verification block, from the pinned-sources module
    (single source of truth; imported lazily so the protocol package does
    not import demo modules at module load)."""
    from demo.tasks import pinned_sunshade_sources as src
    return dict(src.CLAIM_PROVENANCE)


MISSIONS = {
    MISSION_ID: {
        "schema": MISSION_SCHEMA,
        "nodes": NODES,
        "edges": EDGES,
        "excluded": EXCLUDED_NODES,
        "named_gap": NAMED_GAP,
        "flips": FLIP_CONDITIONS,
        "claim_source": None,
        "not_modeled": None,
    },
    MISSION_ID_0002: {
        "schema": MISSION_SCHEMA_0002,
        "nodes": NODES_0002,
        "edges": EDGES_0002,
        "excluded": EXCLUDED_NODES_0002,
        "named_gap": NAMED_GAP_0002,
        "flips": FLIP_CONDITIONS_0002,
        "claim_source": _claim_source,
        "not_modeled": NOT_MODELED_0002,
    },
}


# ----------------------------------------------------------------------------
# canonical form (era-2, the pulse's exact discipline)
# ----------------------------------------------------------------------------
def _sign_safe_zero(obj):
    return json.loads(json.dumps(obj),
                      parse_float=lambda t: 0.0 if float(t) == 0.0 else float(t))


def canonical_json(obj) -> str:
    return json.dumps(_sign_safe_zero(obj), sort_keys=True,
                      separators=(",", ":"), ensure_ascii=True)


def verdict_hash(doc: dict) -> str:
    body = {k: v for k, v in doc.items() if k != "verdict_hash"}
    return hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()


# ----------------------------------------------------------------------------
# DAG typing (pure; fixture-testable)
# ----------------------------------------------------------------------------
def dag_findings(nodes, edges, declared_parents: dict) -> list:
    """Named findings against the typed-DAG rules; empty means well-typed.
    `declared_parents` = {task: [parent short ids]} from the modules'
    PARENT_TASKS declarations (or fixtures)."""
    findings = []
    ids = [n["task"] for n in nodes]
    if len(set(ids)) != len(ids):
        findings.append("duplicate node ids")
    known = set(ids) | {MISSION_SINK}
    feeds = {}
    for e in edges:
        if e.get("type") not in EDGE_TYPES:
            findings.append(f"edge {e.get('src')}->{e.get('dst')}: unknown "
                            f"type {e.get('type')!r}")
        if e.get("src") not in set(ids):
            findings.append(f"edge src {e.get('src')!r} is not a node")
        if e.get("dst") not in known:
            findings.append(f"edge dst {e.get('dst')!r} is not a node")
        if not (isinstance(e.get("justification"), str)
                and e["justification"].strip()):
            findings.append(f"edge {e.get('src')}->{e.get('dst')}: no "
                            "justification")
        if e.get("type") == "feeds":
            feeds.setdefault(e.get("dst"), set()).add(e.get("src"))
    # every feeds edge must be an EXECUTABLE parent edge, and vice versa
    # (among chain nodes): the DAG asserts nothing the code does not enforce.
    for n in nodes:
        declared = {p for p in declared_parents.get(n["task"], [])
                    if p in set(ids)}
        drawn = feeds.get(n["task"], set())
        for src in sorted(drawn - declared):
            findings.append(f"feeds edge {src}->{n['task']} has no "
                            "PARENT_TASKS declaration behind it")
        for src in sorted(declared - drawn):
            findings.append(f"declared parent edge {src}->{n['task']} is "
                            "missing from the DAG")
    # constraining nodes carry a verdict field and a constrains edge
    for n in nodes:
        is_con = n.get("role") == "constraining"
        has_field = isinstance(n.get("verdict_field"), str)
        has_edge = any(e.get("src") == n["task"] and e.get("type") == "constrains"
                       and e.get("dst") == MISSION_SINK for e in edges)
        if is_con and not has_field:
            findings.append(f"constraining node {n['task']} names no "
                            "verdict_field")
        if is_con != has_edge:
            findings.append(f"node {n['task']}: role {n.get('role')!r} and "
                            "constrains-edge presence disagree")
    # acyclicity: bounded Kahn peel over the task nodes (the sink cannot feed)
    remaining = {n["task"] for n in nodes}
    dep = {n["task"]: {e["src"] for e in edges
                       if e.get("dst") == n["task"] and e.get("src") in remaining}
           for n in nodes}
    for _ in range(len(nodes)):  # bounded: one peel per node
        free = sorted(t for t in remaining if not (dep[t] & remaining))
        if not free:
            break
        remaining -= set(free)
    if remaining:
        findings.append(f"cycle among nodes: {sorted(remaining)}")
    return findings


# ----------------------------------------------------------------------------
# per-node figures + bottlenecks (quoted from the anchored artifacts, never
# recomputed here — the verdict says what the tasks already say)
# ----------------------------------------------------------------------------
def _rows(result):
    return {d["quantity"]: d["value"] for d in result["results"]
            if isinstance(d, dict) and "quantity" in d}


def node_figures(task: str, result: dict) -> dict:
    s = result["summary"]
    if task == "task-0015":
        r = _rows(result)
        return {"ch4_product_kg": r["ch4_product_kg"],
                "h2o_product_kg": r["h2o_product_kg"],
                "assumed_single_pass_conversion_fraction":
                    result["inputs"]["single_pass_conversion"]}
    if task == "task-0019":
        ref = next(row for row in result["results"]
                   if row["temperature_K"] == 700.0)
        return {"ln_k_eq_at_700_K_dimensionless": ref["ln_k_eq_dimensionless"],
                "grid_points_count": len(result["results"])}
    if task == "task-0017":
        r = _rows(result)
        return {"usable_propellant_kg": r["usable_propellant_kg"],
                "achievable_dv_m_s": r["delta_v_m_s"],
                "binding_reactant": s["binding_reactant"]}
    if task == "task-0020":
        return {"equilibrium_conversion_fraction":
                    s["reference_equilibrium_conversion_fraction"],
                "assumed_conversion_fraction":
                    result["inputs"]["conversion_threshold_fraction"],
                "shortfall_fraction": s["reference_shortfall_fraction"],
                "max_acceptable_temperature_at_1_bar_K":
                    s["max_acceptable_temperature_at_1_bar_K"],
                "max_acceptable_temperature_at_10_bar_K":
                    s["max_acceptable_temperature_at_10_bar_K"]}
    if task == "task-0018":
        r = _rows(result)
        return {"required_dv_m_s": r["required_dv_m_s"],
                "achievable_dv_m_s": r["achievable_dv_m_s"],
                "margin_m_s": r["margin_m_s"],
                "shortfall_m_s": s["shortfall_m_s"]}
    if task == "task-0021":
        return {"corrected_dv_m_s": s["corrected_delta_v_m_s"],
                "shortfall_m_s": s["shortfall_m_s"],
                "shortfall_growth_m_s": s["shortfall_growth_m_s"],
                "conversion_scale_ratio": s["conversion_scale_ratio"]}
    if task == "task-0012":
        return {"ebn0_db": s["ebn0_db"],
                "link_margin_db": s["link_margin_db"]}
    if task == "task-0022":
        return {"reference_required_fraction":
                    s["reference_required_fraction"],
                "doubled_co2_required_fraction":
                    s["doubled_co2_required_fraction"]}
    if task == "task-0023":
        return {"required_area_km2": s["required_area_km2"],
                "shade_distance_from_earth_km":
                    s["shade_distance_from_earth_km"],
                "occulting_efficiency_fraction":
                    s["occulting_efficiency_fraction"]}
    if task == "task-0024":
        return {"total_shade_mass_million_t": s["total_shade_mass_million_t"],
                "unit_sails_count": s["unit_sails_count"]}
    if task == "task-0025":
        return {"regolith_processed_million_t":
                    s["regolith_processed_million_t"],
                "sustained_power_MW": s["sustained_power_MW"],
                "pv_area_km2": s["pv_area_km2"]}
    if task == "task-0026":
        return {"escape_velocity_km_s": s["escape_velocity_km_s"],
                "throughput_t_per_yr": s["throughput_t_per_yr"],
                "deployment_duration_yr": s["deployment_duration_yr"],
                "sustained_power_MW": s["sustained_power_MW"]}
    if task == "task-0027":
        return {"deployment_duration_yr":
                    result["results"][0]["deployment_duration_yr"],
                "acceptable_horizon_yr":
                    result["results"][0]["acceptable_horizon_yr"],
                "overrun_factor_ratio": s["overrun_factor_ratio"]}
    if task == "task-0028":
        return {"efold_time_days": s["efold_time_days"],
                "tenfold_spread_days": s["tenfold_spread_days"],
                "beta_at_1um_ratio": s["beta_at_1um_ratio"]}
    if task == "task-0029":
        return {"ceiling_horizon_Gyr": s["ceiling_horizon_Gyr"],
                "margin_Gyr": s["margin_Gyr"],
                "shade_growth_factor_at_claim_ratio":
                    s["shade_growth_factor_at_claim_ratio"],
                "verdict_flip_ceiling_fraction":
                    s["verdict_flip_ceiling_fraction"]}
    return {}


def bottleneck_entry(task: str, result: dict) -> dict:
    """The quantified size of a failed constraining node's 'no'."""
    s = result["summary"]
    if task == "task-0020":
        return {"quantity": "conversion_shortfall_fraction",
                "shortfall_fraction": s["reference_shortfall_fraction"],
                "statement": "equilibrium conversion "
                             f"{s['reference_equilibrium_conversion_fraction']} "
                             "< assumed "
                             f"{result['inputs']['conversion_threshold_fraction']} "
                             "at the 700 K / 1 bar reference point"}
    if task == "task-0018":
        r = _rows(result)
        return {"quantity": "ascent_shortfall_m_s",
                "shortfall_m_s": s["shortfall_m_s"],
                "statement": f"achievable {r['achievable_dv_m_s']} m/s < "
                             f"required {r['required_dv_m_s']} m/s at the "
                             "assumed conversion"}
    if task == "task-0021":
        return {"quantity": "corrected_ascent_shortfall_m_s",
                "shortfall_m_s": s["shortfall_m_s"],
                "statement": f"corrected achievable {s['corrected_delta_v_m_s']}"
                             " m/s at the equilibrium conversion — the "
                             "shortfall grows by "
                             f"{s['shortfall_growth_m_s']} m/s over task-0018's"}
    if task == "task-0012":
        return {"quantity": "link_shortfall_db",
                "shortfall_db": round(-s["link_margin_db"], 6),
                "statement": f"link margin {s['link_margin_db']} dB — the "
                             "X-band budget does not close at Mars-class range"}
    if task == "task-0027":
        return {"quantity": "deployment_shortfall_yr",
                "shortfall_yr": s["shortfall_yr"],
                "overrun_factor_ratio": s["overrun_factor_ratio"],
                "statement": f"{result['results'][0]['deployment_duration_yr']}"
                             " yr to deploy at the claim's stated cadence vs "
                             f"a {result['results'][0]['acceptable_horizon_yr']}"
                             " yr climate-relevant horizon — "
                             f"{s['overrun_factor_ratio']}x over"}
    if task == "task-0028":
        return {"quantity": "dust_persistence_shortfall_yr",
                "shortfall_yr": s["shortfall_yr"],
                "statement": f"a dust cloud at L1 ten-folds its spread in "
                             f"{s['tenfold_spread_days']} days (derived "
                             f"e-folding {s['efold_time_days']} days) vs a "
                             "one-year minimum useful persistence"}
    if task == "task-0029":
        return {"quantity": "longevity_margin_Gyr",
                "shortfall_Gyr": round(max(0.0, -s["margin_Gyr"]), 6),
                "statement": f"ceiling horizon {s['ceiling_horizon_Gyr']} Gyr "
                             "vs the claimed 1 Gyr"}
    # a node this table does not know still gets an honest generic entry
    # (fixture chains exercise this; every real mission-0001 node is specific)
    return {"quantity": "constraining_verdict_false",
            "statement": f"{task}: constraining verdict field is false"}


# ----------------------------------------------------------------------------
# assembly (pure; fixture-testable). Raises ValueError under the refusal rule.
# ----------------------------------------------------------------------------
def build_mission_verdict(node_results: dict, node_hashes: dict,
                          anchored_refs: dict, declared_parents: dict,
                          nodes=NODES, edges=EDGES,
                          mission_id=MISSION_ID, schema=MISSION_SCHEMA,
                          excluded=EXCLUDED_NODES, flips=FLIP_CONDITIONS,
                          named_gap=NAMED_GAP, claim_source=None,
                          not_modeled=None) -> dict:
    """The deterministic document. `node_results`/`node_hashes` are live
    recomputes; `anchored_refs` = {task: {ledger_index, output_hash}} with the
    hash already translated to the current era. The honest negatives FLOW
    THROUGH; only drift and mistyping refuse. Defaults reproduce
    mission-0001 byte-identically; a 0.2-schema mission adds claim_source
    (the public assertion under verification, verbatim with date) and
    not_modeled (the named gaps) — absent keys are omitted, never
    null-padded."""
    findings = dag_findings(nodes, edges, declared_parents)
    for n in nodes:
        t = n["task"]
        if t not in node_results or t not in node_hashes:
            findings.append(f"node {t}: no live recompute supplied")
            continue
        ref = anchored_refs.get(t)
        if not isinstance(ref, dict) or "output_hash" not in ref:
            findings.append(f"node {t}: no anchored record on the chain")
        elif node_hashes[t] != ref["output_hash"]:
            findings.append(f"node {t}: live recompute {node_hashes[t][:12]} "
                            f"!= anchored {str(ref['output_hash'])[:12]} "
                            "(drifted node)")
        if n.get("role") == "constraining" and t in node_results:
            v = node_results[t]["summary"].get(n.get("verdict_field"))
            if not isinstance(v, bool):
                findings.append(f"node {t}: verdict field "
                                f"{n.get('verdict_field')!r} missing or "
                                "non-boolean")
    if findings:
        raise ValueError("REFUSED (" + REFUSAL_RULE + "): "
                         + "; ".join(findings))

    node_verdicts = {}
    bottlenecks = []
    flip_map = {}
    for n in nodes:
        t = n["task"]
        result = node_results[t]
        verdict = (bool(result["summary"][n["verdict_field"]])
                   if n["role"] == "constraining" else None)
        node_verdicts[t] = {"role": n["role"], "verdict": verdict,
                            "figures": node_figures(t, result)}
        if verdict is False:
            bottlenecks.append({"task": t, **bottleneck_entry(t, result)})
            if t in flips:
                flip_map[t] = flips[t]
    constraining = [node_verdicts[n["task"]]["verdict"] for n in nodes
                    if n["role"] == "constraining"]
    mission_feasible = all(constraining)

    doc = {
        "schema": schema,
        "mission_id": mission_id,
        "mission_feasible": mission_feasible,
        "constraining_nodes_count": len(constraining),
        "failed_constraining_nodes_count": sum(1 for v in constraining
                                               if v is False),
        "dag": {
            "nodes": [{"task": n["task"], "role": n["role"],
                       "title": n["title"],
                       **({"verdict_field": n["verdict_field"]}
                          if n["role"] == "constraining" else {}),
                       "output_hash": node_hashes[n["task"]],
                       "anchored_ledger_index":
                           anchored_refs[n["task"]]["ledger_index"],
                       "declared_parents": sorted(
                           p for p in declared_parents.get(n["task"], [])
                           if p in {m["task"] for m in nodes})}
                      for n in nodes],
            "edges": [dict(e) for e in edges],
            "edge_types": list(EDGE_TYPES),
            "sink": MISSION_SINK,
            "excluded_nodes": [dict(x) for x in excluded],
            "named_gap": named_gap,
        },
        "node_verdicts": node_verdicts,
        "bottlenecks": bottlenecks,
        "what_would_flip_it": flip_map,
        "verdict_rule": "mission_feasible = AND over the constraining nodes' "
                        "verdict fields; honest negatives flow through",
        "refusal_rule": REFUSAL_RULE,
        "no_timestamps_note": ("hashed artifact: no wall-clock fields; the "
                               "anchoring record's anchored_at dates this "
                               "verdict"),
        "honest_boundary": ("same-operator, zero-value evidence: a matching "
                            "verdict proves deterministic re-derivability of "
                            "the mission conclusion from anchored tasks, NOT "
                            "independence, usefulness, or flight fidelity — "
                            "every node is an illustrative fixed-constant "
                            "calculation and says so in its own artifact"),
        "zero_value": True,
        "no_token": True,
    }
    # 0.2-schema missions carry claim-under-verification provenance and the
    # named non-modeled gaps; absent keys are omitted, never null-padded.
    if claim_source is not None:
        doc["claim_source"] = dict(claim_source)
    if not_modeled is not None:
        doc["not_modeled"] = list(not_modeled)
    doc["verdict_hash"] = verdict_hash(doc)
    return doc


def headline(doc: dict) -> dict:
    """The numbers the anchoring record carries on-chain (scanner-invisible:
    no task_id / task_ids keys anywhere at payload top level)."""
    shortfalls = {}
    for b in doc["bottlenecks"]:
        value = next((b[k] for k in sorted(b)
                      if k.startswith("shortfall_")), None)
        shortfalls[b["quantity"]] = value
    return {
        "mission_feasible": doc["mission_feasible"],
        "nodes": len(doc["dag"]["nodes"]),
        "edges": len(doc["dag"]["edges"]),
        "constraining": doc["constraining_nodes_count"],
        "failed_constraining": doc["failed_constraining_nodes_count"],
        "bottleneck_shortfalls": shortfalls,
    }


def validate_verdict(doc) -> tuple:
    """(ok, reasons): shape, self-hash, and the AND bound to the fields.
    Schema 0.1 is the idx-82 class; 0.2 adds required claim-under-
    verification provenance (claim_source) and the not_modeled list."""
    reasons = []
    if not isinstance(doc, dict) or doc.get("schema") not in (
            MISSION_SCHEMA, MISSION_SCHEMA_0002):
        return (False, ["not a mission_verdict/0.1 or /0.2 document"])
    if doc.get("schema") == MISSION_SCHEMA_0002:
        cs = doc.get("claim_source")
        if not (isinstance(cs, dict) and cs.get("quoted")
                and cs.get("date") and cs.get("author")):
            reasons.append("0.2 schema requires claim_source with quoted "
                           "text, author, and date")
        if not (isinstance(doc.get("not_modeled"), list)
                and doc["not_modeled"]):
            reasons.append("0.2 schema requires a non-empty not_modeled list")
    for key in ("mission_id", "mission_feasible", "dag", "node_verdicts",
                "bottlenecks", "what_would_flip_it", "verdict_hash"):
        if key not in doc:
            reasons.append(f"missing {key}")
    if reasons:
        return (False, reasons)
    if verdict_hash(doc) != doc["verdict_hash"]:
        reasons.append("verdict_hash does not recompute from the document")
    cons = [v.get("verdict") for v in doc["node_verdicts"].values()
            if v.get("role") == "constraining"]
    if doc["mission_feasible"] != all(bool(v) for v in cons):
        reasons.append("mission_feasible is not the AND over the "
                       "constraining node verdicts")
    failed = sum(1 for v in cons if v is False)
    if doc.get("failed_constraining_nodes_count") != failed:
        reasons.append("failed-constraining count disagrees with the fields")
    if failed and len(doc["bottlenecks"]) != failed:
        reasons.append("bottleneck entries disagree with the failed nodes")
    return (not reasons, reasons)


# ----------------------------------------------------------------------------
# live derivation (re-run every node, read the chain)
# ----------------------------------------------------------------------------
def _first_task_record(entries, short_id):
    """(ledger_index, recorded_hash) of the FIRST record carrying an output
    hash for the task — the agent_verifier convention, stable under append."""
    for e in entries:
        p = e.get("payload", {}) if isinstance(e, dict) else {}
        if p.get("task_id") == short_id:
            for key in ("local_output_hash", "output_hash",
                        "submitted_output_hash"):
                if isinstance(p.get(key), str):
                    return (e["index"], p[key])
    return (None, None)


def rederive(entries, mission_id: str = MISSION_ID) -> dict:
    """Re-run every node task of the named mission, rebuild its DAG,
    recompute the verdict — the whole document from scratch. Raises
    ValueError under the refusal rule (unknown missions included)."""
    if mission_id not in MISSIONS:
        raise ValueError(f"REFUSED ({REFUSAL_RULE}): unknown mission "
                         f"{mission_id!r} — defined: {sorted(MISSIONS)}")
    m = MISSIONS[mission_id]
    era_map = verifier_cli.load_hash_era_map(entries)
    node_results, node_hashes, anchored, parents = {}, {}, {}, {}
    for n in m["nodes"]:
        t = n["task"]
        module = verifier_cli.load_task(t)
        result = module.compute()
        node_results[t] = result
        node_hashes[t] = module.output_hash(result)
        parents[t] = list(getattr(module, "PARENT_TASKS", []))
        idx, recorded = _first_task_record(entries, t)
        if idx is not None:
            anchored[t] = {"ledger_index": idx,
                           "output_hash": verifier_cli.era_expected_hash(
                               t, recorded, era_map)}
    claim = m["claim_source"]
    return build_mission_verdict(
        node_results, node_hashes, anchored, parents,
        nodes=m["nodes"], edges=m["edges"], mission_id=mission_id,
        schema=m["schema"], excluded=m["excluded"], flips=m["flips"],
        named_gap=m["named_gap"],
        claim_source=claim() if callable(claim) else claim,
        not_modeled=m["not_modeled"])


def generate(mission_id: str = MISSION_ID, echo=print) -> dict:
    entries = _read_ledger(resolve_ledger_path())
    doc = rederive(entries, mission_id)
    echo(f"mission verdict derived: {doc['mission_id']}")
    echo(f"mission_feasible: {doc['mission_feasible']} "
         f"({doc['failed_constraining_nodes_count']} of "
         f"{doc['constraining_nodes_count']} constraining nodes fail)")
    return doc


# ----------------------------------------------------------------------------
# status of the latest anchored verdict (the docs' verify block)
# ----------------------------------------------------------------------------
def latest_verdict_records(entries):
    """The latest anchored record PER MISSION, in mission-id order."""
    latest = {}
    for e in entries:
        p = e.get("payload", {})
        if (p.get("event") == MISSION_EVENT
                and p.get("status") == MISSION_STATUS):
            latest[p.get("mission_id")] = e
    return [latest[k] for k in sorted(latest)]


def status(entries=None, echo=print) -> int:
    entries = (entries if entries is not None
               else _read_ledger(resolve_ledger_path()))
    recs = latest_verdict_records(entries)
    if not recs:
        echo("MISSION STATUS: OK — no mission verdict anchored on the chain "
             "yet (named; the first verdict follows the mission chain's nodes)")
        return 0
    failures = 0
    for rec in recs:
        p = rec["payload"]
        path = find_evidence_file(
            f"mission_verdict_{p['verdict_hash'][:12]}.json")
        if path is None:
            echo(f"MISSION STATUS: BROKEN — idx {rec['index']} cites verdict "
                 f"{p['verdict_hash'][:12]} but no evidence file ships")
            failures += 1
            continue
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
        ok, reasons = validate_verdict(doc)
        try:
            fresh = rederive(entries, p.get("mission_id"))
            exact = canonical_json(fresh) == canonical_json(doc)
        except ValueError as exc:
            fresh, exact = None, False
            reasons.append(str(exc)[:160])
        if (ok and exact and doc["verdict_hash"] == p["verdict_hash"]
                and headline(doc) == p.get("headline")):
            echo(f"MISSION STATUS: OK — {p.get('mission_id')} idx "
                 f"{rec['index']} re-derives BIT-EXACT: every node re-run, "
                 f"DAG rebuilt, verdict_hash {p['verdict_hash'][:12]} "
                 f"recomputed; mission_feasible {doc['mission_feasible']} "
                 f"({doc['failed_constraining_nodes_count']}/"
                 f"{doc['constraining_nodes_count']} constraining nodes fail)")
        else:
            failures += 1
            echo(f"MISSION STATUS: BROKEN — {p.get('mission_id')}: " + "; ".join(
                reasons + ([] if exact else ["re-derivation is not bit-exact"])
                + ([] if doc.get("verdict_hash") == p.get("verdict_hash")
                   else ["file hash != record hash"])
                + ([] if headline(doc) == p.get("headline")
                   else ["headline mismatch"])))
    return 1 if failures else 0


# ----------------------------------------------------------------------------
# self-test (fixtures only; no ledger writes, no real task runs for the
# assembly rules — one structural check against the real registry at the end)
# ----------------------------------------------------------------------------
def _fixture_chain():
    nodes = ({"task": "task-9001", "role": "upstream", "title": "source"},
             {"task": "task-9002", "role": "constraining",
              "verdict_field": "feasible", "title": "verdict"})
    edges = ({"src": "task-9001", "dst": "task-9002", "type": "feeds",
              "justification": "consumes the source, hash-asserted"},
             {"src": "task-9002", "dst": MISSION_SINK, "type": "constrains",
              "justification": "gates the mission"})
    results = {
        "task-9001": {"task_id": "task-9001", "inputs": {"single_pass_conversion": 0.9},
                      "results": [{"quantity": "ch4_product_kg", "value": 1.0},
                                  {"quantity": "h2o_product_kg", "value": 2.0}],
                      "summary": {"x_kg": 1.0}},
        "task-9002": {"task_id": "task-9002", "inputs": {},
                      "results": [], "summary": {"feasible": False}},
    }
    hashes = {"task-9001": "a" * 64, "task-9002": "b" * 64}
    anchored = {"task-9001": {"ledger_index": 4, "output_hash": "a" * 64},
                "task-9002": {"ledger_index": 5, "output_hash": "b" * 64}}
    parents = {"task-9001": [], "task-9002": ["task-9001"]}
    return nodes, edges, results, hashes, anchored, parents


def _selftest() -> int:
    import copy
    print("=== protocol/mission_chain.py self-test (fixtures only; no ledger "
          "writes) ===")
    print("An honest mission-level negative flows through; a drifted node "
          "refuses.\n")
    checks = []
    nodes, edges, results, hashes, anchored, parents = _fixture_chain()
    doc = build_mission_verdict(results, hashes, anchored, parents,
                                nodes=nodes, edges=edges)
    doc2 = build_mission_verdict(results, hashes, anchored, parents,
                                 nodes=nodes, edges=edges)
    checks.append(("an honest FALSE assembles (never refused) and the AND is "
                   "false", validate_verdict(doc)[0]
                   and doc["mission_feasible"] is False
                   and doc["failed_constraining_nodes_count"] == 1))
    checks.append(("deterministic: same inputs -> same bytes and hash",
                   canonical_json(doc) == canonical_json(doc2)
                   and doc["verdict_hash"] == doc2["verdict_hash"]))
    def _keys(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                yield k
                yield from _keys(v)
        elif isinstance(obj, list):
            for v in obj:
                yield from _keys(v)
    checks.append(("no wall-clock field anywhere in the hashed document",
                   not any(k in ("anchored_at", "as_of_utc", "evaluated_at",
                                 "timestamp") or k.startswith("as_of")
                           for k in _keys(doc))))
    ok_true = copy.deepcopy(results)
    ok_true["task-9002"]["summary"]["feasible"] = True
    doc_t = build_mission_verdict(ok_true, hashes, anchored, parents,
                                  nodes=nodes, edges=edges)
    checks.append(("a TRUE verdict also assembles: feasible AND, no "
                   "bottlenecks", doc_t["mission_feasible"] is True
                   and doc_t["bottlenecks"] == []))
    for label, mutate in (
            ("a drifted node (live hash != anchored)",
             lambda h, a, e2: h.update({"task-9002": "c" * 64})),
            ("a node with no anchored record",
             lambda h, a, e2: a.pop("task-9002")),
            ("a feeds edge with no declared parent behind it",
             lambda h, a, e2: parents_mut.update({"task-9002": []})),
            ("an unknown edge type",
             lambda h, a, e2: e2.append({"src": "task-9001",
                                         "dst": "task-9002",
                                         "type": "vibes",
                                         "justification": "x"})),
            ("an edge to a non-node",
             lambda h, a, e2: e2.append({"src": "task-9001", "dst": "task-9999",
                                         "type": "informs",
                                         "justification": "x"}))):
        h2 = dict(hashes)
        a2 = {k: dict(v) for k, v in anchored.items()}
        e2 = [dict(e) for e in edges]
        parents_mut = {k: list(v) for k, v in parents.items()}
        mutate(h2, a2, e2)
        try:
            build_mission_verdict(results, h2, a2, parents_mut,
                                  nodes=nodes, edges=tuple(e2))
            refused = False
        except ValueError as exc:
            refused = "REFUSED" in str(exc)
        checks.append((f"refuses {label}", refused))
    # a cycle refuses (both nodes declare each other; edges drawn both ways)
    cyc_edges = tuple(list(edges) + [{"src": "task-9002", "dst": "task-9001",
                                     "type": "feeds",
                                     "justification": "x"}])
    cyc_parents = {"task-9001": ["task-9002"], "task-9002": ["task-9001"]}
    try:
        build_mission_verdict(results, hashes, anchored, cyc_parents,
                              nodes=nodes, edges=cyc_edges)
        refused = False
    except ValueError as exc:
        refused = "cycle" in str(exc)
    checks.append(("refuses a cyclic chain by name", refused))
    tampered = copy.deepcopy(doc)
    tampered["mission_feasible"] = True
    checks.append(("a tampered verdict fails validation (self-hash broken)",
                   not validate_verdict(tampered)[0]))
    forged = copy.deepcopy(doc)
    forged["mission_feasible"] = True
    forged["verdict_hash"] = verdict_hash(forged)
    checks.append(("a re-hashed verdict lying about the AND still fails "
                   "validation (the rule binds the fields, not only the hash)",
                   not validate_verdict(forged)[0]))
    checks.append(("headline carries the record's numbers, scanner-invisible "
                   "(no task_id/task_ids keys)",
                   headline(doc)["failed_constraining"] == 1
                   and "task_id" not in json.dumps(headline(doc))
                   and "task_ids" not in json.dumps(headline(doc))))
    # EVERY real mission definition type-checks against the REAL modules'
    # PARENT_TASKS declarations (imports only — no compute() run here)
    for mid, m in sorted(MISSIONS.items()):
        real_parents = {n["task"]: list(getattr(
            verifier_cli.load_task(n["task"]), "PARENT_TASKS", []))
            for n in m["nodes"]}
        checks.append((f"the real {mid[:12]} DAG type-checks against the "
                       "real PARENT_TASKS declarations",
                       dag_findings(m["nodes"], m["edges"], real_parents) == []))
    # 0.2-schema fixtures: claim_source + not_modeled carried and required
    doc02 = build_mission_verdict(
        results, hashes, anchored, parents, nodes=nodes, edges=edges,
        mission_id="mission-9002-fixture", schema=MISSION_SCHEMA_0002,
        excluded=(), flips={}, named_gap="fixture gap",
        claim_source={"author": "a public poster", "date": "2026-08-30",
                      "quoted": "a fixture assertion", "tier": "public-assertion"},
        not_modeled=["a fixture gap"])
    checks.append(("a 0.2 verdict carries claim_source + not_modeled and "
                   "validates", validate_verdict(doc02)[0]
                   and doc02["claim_source"]["quoted"] == "a fixture assertion"
                   and doc02["not_modeled"] == ["a fixture gap"]))
    bad02 = build_mission_verdict(
        results, hashes, anchored, parents, nodes=nodes, edges=edges,
        mission_id="mission-9002-fixture", schema=MISSION_SCHEMA_0002,
        excluded=(), flips={}, named_gap="fixture gap")
    checks.append(("a 0.2 verdict WITHOUT claim provenance fails validation "
                   "(claim-under-verification is required by the schema)",
                   not validate_verdict(bad02)[0]))
    checks.append(("a 0.1 verdict never carries claim_source (absent keys "
                   "omitted — the idx-82 bytes cannot move)",
                   "claim_source" not in doc and "not_modeled" not in doc))
    try:
        rederive([], "mission-9999-unknown")
        refused = False
    except ValueError as exc:
        refused = "unknown mission" in str(exc)
    checks.append(("rederive refuses an unknown mission by name", refused))
    out = []
    checks.append(("status names 'no verdict yet' as OK on a chain without "
                   "one", status([{"index": 0, "payload": {"event": "x"}}],
                                 echo=out.append) == 0
                   and "no mission verdict" in out[0]))
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
        description="The anchored mission verification chain (research-stage, "
                    "ZERO-VALUE, no token): mission-0001 as a typed DAG over "
                    "anchored tasks; the verdict re-derives bit-exact or is "
                    "refused. The headline is an honest mission-level FALSE.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--generate", action="store_true",
                      help="re-run every node, build the DAG, write the "
                           "verdict (refuses on any drifted node)")
    mode.add_argument("--verify", metavar="VERDICT_JSON",
                      help="re-derive the verdict from this chain and compare "
                           "bit-exact")
    mode.add_argument("--status", action="store_true",
                      help="does the latest anchored verdict re-derive "
                           "bit-exact from the chain?")
    mode.add_argument("--selftest", action="store_true")
    parser.add_argument("--mission", default=MISSION_ID,
                        choices=sorted(MISSIONS),
                        help="which mission to generate/verify "
                             f"(default {MISSION_ID})")
    parser.add_argument("--out",
                        default=os.path.join(_REPO_ROOT, "mission_verdict.json"))
    args = parser.parse_args(argv)
    if args.generate:
        try:
            doc = generate(args.mission)
        except ValueError as exc:
            print(str(exc))
            print("mission verdict: NOT generated (nothing written)")
            return 1
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(canonical_json(doc) + "\n")
        print(f"verdict written: {args.out}")
        print(f"verdict_hash   : {doc['verdict_hash']}")
        print("headline       : " + json.dumps(headline(doc), sort_keys=True))
        return 0
    if args.verify:
        with open(args.verify, encoding="utf-8") as f:
            given = json.load(f)
        ok, reasons = validate_verdict(given)
        print("file validates: " + ("yes" if ok else "; ".join(reasons)))
        try:
            fresh = rederive(_read_ledger(resolve_ledger_path()),
                             given.get("mission_id", args.mission))
        except ValueError as exc:
            print(str(exc))
            print("MISSION VERIFY: FAILED — the chain cannot re-derive a "
                  "verdict")
            return 1
        if ok and canonical_json(fresh) == canonical_json(given):
            print(f"MISSION VERIFY: MATCH — bit-exact re-derivation, "
                  f"verdict_hash {fresh['verdict_hash'][:12]}; "
                  f"mission_feasible {fresh['mission_feasible']}")
            return 0
        diffs = [k for k in fresh
                 if canonical_json(fresh[k]) != canonical_json(given.get(k))]
        print(f"MISSION VERIFY: DIFFERS in {diffs}")
        return 1
    if args.status:
        return status()
    return _selftest()


if __name__ == "__main__":
    sys.exit(main())
