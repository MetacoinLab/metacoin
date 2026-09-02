# Copyright (c) 2023-2026 MetaCoin-Lab.
# Licensed under SML-1.0 — see LICENSE.md.
"""mission_graph_svg.py — deterministic SVG renderer for an anchored mission
verdict's DAG (the README diagram generator).

================== HONESTY / SCOPE (READ ME) ==================
Research-stage, ZERO-VALUE, no token. Standard library only; ZERO ledger
writes. The diagram is DERIVED from an anchored mission-verdict evidence
file (protocol/evidence/mission_verdict_<hash12>.json) — node colors come
from the ANCHORED node verdicts, never recomputed here, so the picture can
only show what the chain already proves. Deterministic: the same evidence
file renders byte-identical SVG (no timestamps, no randomness); the
self-test builds twice and asserts it, parses the XML, and checks that
every anchored node and edge appears exactly once with the right style:

  * fill colors — upstream slate, constraining TRUE green, constraining
    FALSE red (the honest negatives visibly marked with their verdict);
  * solid edges = feeds (data-enforced at execution time by parent-hash
    liveness); dashed edges = constrains/informs (declarative typing).

Usage:
    python3 protocol/mission_graph_svg.py [--mission MISSION_ID] [--out SVG]
    python3 protocol/mission_graph_svg.py --selftest
Not financial, legal, or flight-engineering advice.
"""

import sys
sys.dont_write_bytecode = True

import argparse
import json
import os

_PROTO_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_PROTO_DIR)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from protocol.prov_export import resolve_ledger_path
from protocol.work_molecule import _read_ledger, find_evidence_file
import protocol.mission_chain as mission_chain

DEFAULT_MISSION = mission_chain.MISSION_ID_0001V3
DEFAULT_OUT = os.path.join(_REPO_ROOT, "assets", "mission_chain.svg")

COL_W, ROW_H = 190, 64
BOX_W, BOX_H = 168, 44
PAD_X, PAD_Y = 24, 46
COLOR_UPSTREAM = "#5c6f82"
COLOR_PASS = "#2e7d32"
COLOR_FAIL = "#b71c1c"
COLOR_SINK_FAIL = "#7f0f0f"
COLOR_EDGE = "#9aa7b3"


def load_anchored_doc(mission_id: str) -> dict:
    """The anchored verdict evidence file for the mission (refuses absence)."""
    entries = _read_ledger(resolve_ledger_path())
    rec = None
    for e in entries:
        p = e.get("payload", {})
        if (p.get("event") == mission_chain.MISSION_EVENT
                and p.get("status") == mission_chain.MISSION_STATUS
                and p.get("mission_id") == mission_id):
            rec = p
    if rec is None:
        raise ValueError(f"no anchored verdict for {mission_id} on this chain")
    path = find_evidence_file(
        f"mission_verdict_{rec['verdict_hash'][:12]}.json")
    if path is None:
        raise ValueError(f"anchored verdict {rec['verdict_hash'][:12]} has "
                         "no shipped evidence file")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _layout(doc: dict):
    """Layered left-to-right by longest feeds-path depth; deterministic."""
    nodes = [n["task"] for n in doc["dag"]["nodes"]]
    feeds = [(e["src"], e["dst"]) for e in doc["dag"]["edges"]
             if e["type"] == "feeds"]
    depth = {t: 0 for t in nodes}
    for _ in range(len(nodes)):          # bounded: longest path < node count
        for s, d in feeds:
            if d in depth and depth[d] < depth[s] + 1:
                depth[d] = depth[s] + 1
    sink_col = max(depth.values()) + 1
    cols = {}
    pos = {}
    for t in nodes:                      # node order is the anchored order
        c = depth[t]
        r = cols.get(c, 0)
        cols[c] = r + 1
        pos[t] = (PAD_X + c * COL_W, PAD_Y + r * ROW_H)
    sink_rows = max(cols.values())
    pos[doc["dag"]["sink"]] = (PAD_X + sink_col * COL_W,
                               PAD_Y + (sink_rows - 1) * ROW_H // 2)
    width = PAD_X * 2 + (sink_col + 1) * COL_W - (COL_W - BOX_W)
    height = PAD_Y + sink_rows * ROW_H + 16
    return pos, width, height


def render_svg(doc: dict) -> str:
    verdicts = doc["node_verdicts"]
    pos, width, height = _layout(doc)
    sink = doc["dag"]["sink"]
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
        f'height="{height}" viewBox="0 0 {width} {height}" '
        f'font-family="monospace" font-size="11">',
        f'<title>{doc["mission_id"]} — anchored mission verdict DAG '
        f'(verdict_hash {doc["verdict_hash"][:12]})</title>',
        f'<text x="{PAD_X}" y="20" font-size="13" fill="#233">'
        f'{doc["mission_id"]} — mission_feasible: '
        f'{str(doc["mission_feasible"]).upper()} '
        f'({doc["failed_constraining_nodes_count"]}/'
        f'{doc["constraining_nodes_count"]} constraining nodes fail) '
        f'· anchored verdict {doc["verdict_hash"][:12]}</text>',
        f'<text x="{PAD_X}" y="34" font-size="10" fill="#567">solid = '
        'feeds (hash-enforced at execution) · dashed = constrains/informs '
        '(typed) · red = honest FALSE · green = TRUE · slate = '
        'upstream</text>',
    ]
    # edges first (under the boxes)
    for e in doc["dag"]["edges"]:
        x1, y1 = pos[e["src"]]
        x2, y2 = pos[e["dst"]]
        dash = '' if e["type"] == "feeds" else ' stroke-dasharray="6,4"'
        out.append(
            f'<line x1="{x1 + BOX_W}" y1="{y1 + BOX_H // 2}" '
            f'x2="{x2}" y2="{y2 + BOX_H // 2}" stroke="{COLOR_EDGE}" '
            f'stroke-width="1.4"{dash}/>')
    for t, (x, y) in sorted(pos.items()):
        if t == sink:
            fill = (COLOR_SINK_FAIL if not doc["mission_feasible"]
                    else COLOR_PASS)
            label, sub = "mission verdict", ("FALSE" if not
                                             doc["mission_feasible"]
                                             else "TRUE")
        else:
            v = verdicts[t]
            if v["role"] == "upstream":
                fill, sub = COLOR_UPSTREAM, "upstream"
            elif v["verdict"]:
                fill, sub = COLOR_PASS, "TRUE"
            else:
                fill, sub = COLOR_FAIL, "FALSE — honest no"
            label = t
        out.append(
            f'<g data-node="{t}"><rect x="{x}" y="{y}" width="{BOX_W}" '
            f'height="{BOX_H}" rx="6" fill="{fill}"/>'
            f'<text x="{x + 8}" y="{y + 18}" fill="#ffffff">{label}</text>'
            f'<text x="{x + 8}" y="{y + 34}" fill="#e6edf3" '
            f'font-size="10">{sub}</text></g>')
    out.append('</svg>')
    return "\n".join(out) + "\n"


def _selftest() -> int:
    import xml.etree.ElementTree as ET
    print("=== protocol/mission_graph_svg.py self-test (read-only) ===\n")
    checks = []
    doc = load_anchored_doc(DEFAULT_MISSION)
    svg1, svg2 = render_svg(doc), render_svg(doc)
    checks.append(("deterministic: two renders byte-identical", svg1 == svg2))
    root = ET.fromstring(svg1)
    ns = "{http://www.w3.org/2000/svg}"
    groups = root.findall(f"{ns}g")
    lines = root.findall(f"{ns}line")
    n_nodes = len(doc["dag"]["nodes"])
    checks.append((f"valid XML with every anchored node + the sink drawn "
                   f"({n_nodes}+1 boxes)", len(groups) == n_nodes + 1))
    checks.append((f"every anchored edge drawn ({len(doc['dag']['edges'])})",
                   len(lines) == len(doc["dag"]["edges"])))
    solid = sum(1 for l in lines if "stroke-dasharray" not in l.attrib)
    feeds = sum(1 for e in doc["dag"]["edges"] if e["type"] == "feeds")
    checks.append(("solid edges == feeds edges (data-enforced vs declarative)",
                   solid == feeds))
    fails = sum(1 for v in doc["node_verdicts"].values()
                if v["verdict"] is False)
    red = svg1.count(COLOR_FAIL)
    checks.append((f"honest negatives visibly marked ({fails} red FALSE "
                   "nodes)", red == fails and "FALSE — honest no" in svg1))
    checks.append(("the verdict hash rides the diagram",
                   doc["verdict_hash"][:12] in svg1))
    try:
        load_anchored_doc("mission-9999-none")
        refused = False
    except ValueError:
        refused = True
    checks.append(("an unanchored mission refuses by name", refused))
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
        description="Anchored mission-verdict DAG -> deterministic SVG "
                    "(research-stage, ZERO-VALUE, no token).")
    parser.add_argument("--mission", default=DEFAULT_MISSION)
    parser.add_argument("--out", default=DEFAULT_OUT)
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args(argv)
    if args.selftest:
        return _selftest()
    doc = load_anchored_doc(args.mission)
    svg = render_svg(doc)
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(svg)
    print(f"svg written: {args.out} ({len(doc['dag']['nodes'])} nodes, "
          f"{len(doc['dag']['edges'])} edges, mission_feasible="
          f"{doc['mission_feasible']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
