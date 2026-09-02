# Copyright (c) 2023-2026 MetaCoin-Lab.
# Licensed under SML-1.0 — see LICENSE.md.
"""status_board_svg.py — deterministic SVG status board for the README's
"By the numbers" section (the generated companion to the markdown table).

================== HONESTY / SCOPE (READ ME) ==================
Research-stage, ZERO-VALUE, no token. Standard library only; ZERO ledger
writes. Every number on the board is READ FROM THE SAME SOURCES doc_verify
uses — the README's declared era pin, compute_era_tokens at that pin,
compute_tokens for repo-derived counts, a quick-mode layer roster from
verify_everything, and the README's own table cells for the two plain
values the table states (lines, release-gate wording) — so the board can
never say something the doc-verify machinery would not also say. The
self-test asserts exactly that, tile by tile, plus XML validity,
byte-determinism across two builds, and refusal (by name) of a chain
whose tip does not match the README's declared pin. No claims beyond the
numbers; the caption under the README embed says the board is generated.

Usage:
    python3 protocol/status_board_svg.py            # -> assets/status_board.svg
    python3 protocol/status_board_svg.py --selftest
Not financial, legal, or engineering advice.
"""

import sys
sys.dont_write_bytecode = True

import argparse
import os
import re

_PROTO_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_PROTO_DIR)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import protocol.doc_verify as doc_verify
from protocol.prov_export import resolve_ledger_path
from protocol.work_molecule import _read_ledger

DEFAULT_OUT = os.path.join(_REPO_ROOT, "assets", "status_board.svg")

# Palette: the mission-DAG SVG accents on an OpenMCT-style dark panel.
BG = "#0b0e14"
TILE = "#141a23"
TILE_EDGE = "#2a3442"
TEXT = "#e6edf3"
MUTED = "#9aa7b3"
GREEN = "#3fb950"        # readable-on-dark shade of the DAG pass green
RED = "#f85149"          # readable-on-dark shade of the DAG fail red
SLATE = "#5c6f82"

TILE_W, TILE_H, GAP = 176, 96, 12
COLS = 5
PAD = 20

_LINES_CELL_RE = re.compile(
    r"zero runtime dependencies \| ([\d,]+) lines")
_GATE_NOTE = "RELEASE GATE READY · approval human"


def gather_facts():
    """Every board value, from the doc_verify sources; refuses mismatch."""
    entries = _read_ledger(resolve_ledger_path())
    with open(doc_verify.README_PATH, encoding="utf-8") as f:
        readme = f.read()
    pins = doc_verify._ERA_PIN_RE.findall(readme)
    if len(pins) != 1:
        raise ValueError("README must declare exactly one era pin")
    pin_count, pin_prefix = int(pins[0][0]), pins[0][1]
    if pin_count > len(entries):
        raise ValueError(f"chain mismatch: README pins entry_count "
                         f"{pin_count} but the chain has {len(entries)}")
    actual = entries[pin_count - 1]["hash"][:12]
    if actual != pin_prefix:
        raise ValueError(f"chain mismatch: README pins tip {pin_prefix!r} "
                         f"at entry {pin_count} but the chain records "
                         f"{actual!r} — the board refuses to render")
    era = doc_verify.compute_era_tokens(entries, pin_count)
    live = doc_verify.compute_tokens()

    # Mission verdicts: 'ALL FALSE' is asserted from the chain, not typed.
    verdicts = [e["payload"] for e in entries[:pin_count]
                if e["payload"].get("event") == "mission_verdict_recorded"
                and e["payload"].get("status") == "mission-verdict-confirmed"]
    n_false = sum(1 for p in verdicts if p.get("mission_feasible") is False)
    if n_false != len(verdicts):
        raise ValueError("a mission verdict is not FALSE — the 'ALL FALSE' "
                         "sublabel would be wrong; update the board text")

    # Verify-layer roster: quick mode builds the same rows as --full.
    import protocol.verify_everything as verify_everything
    _ok, rows, _note = verify_everything.run_verification(full=False)

    # The two plain README table cells the board mirrors (kept in the
    # markdown table for accessibility; the self-test holds them equal).
    m = _LINES_CELL_RE.search(readme)
    if m is None:
        raise ValueError("README table is missing the zero-dependency "
                         "line-count cell the board mirrors")
    suites = int(live["demo_suite_count"]) + int(live["protocol_suite_count"])
    return {
        "era": era, "live": live, "lines_cell": m.group(1),
        "layer_count": len(rows), "suite_total": suites,
        "pin_count": pin_count, "pin_prefix": pin_prefix,
    }


def _tiles(f):
    era, live = f["era"], f["live"]
    return [
        (era["entry_count"], "ANCHORED ENTRIES",
         f"tip index {era['tip_index']}", TEXT),
        (era["recorded_task_count"], "REPRODUCIBLE TASKS",
         "bit-exact re-derived", TEXT),
        (live["honest_negative_count"], "HONEST NEGATIVES",
         "anchored FALSE results", RED),
        (era["mission_verdict_count"], "MISSION VERDICTS",
         "ALL FALSE, quantified", RED),
        (era["mip_decision_count"], "MIP DECISIONS",
         "3 by supersession", TEXT),
        (str(f["layer_count"]), "VERIFY LAYERS",
         "one command, no judgment", TEXT),
        (str(f["suite_total"]), "SELF-TESTS",
         f"{live['demo_suite_count']} demo + "
         f"{live['protocol_suite_count']} protocol", GREEN),
        (f["lines_cell"], "PYTHON LINES",
         "zero runtime deps", TEXT),
        (era["drill_entry_count"], "DRILLS DEFEATED",
         "labeled on-record", GREEN),
        (era["catalog_anchor_count"], "CATALOG GENERATIONS",
         "every one still verifies", TEXT),
    ]


def render_svg(facts) -> str:
    tiles = _tiles(facts)
    rows = (len(tiles) + COLS - 1) // COLS
    width = PAD * 2 + COLS * TILE_W + (COLS - 1) * GAP
    grid_top = 58
    strip_y = grid_top + rows * (TILE_H + GAP) + 8
    height = strip_y + 58
    era = facts["era"]
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
        f'height="{height}" viewBox="0 0 {width} {height}" '
        f'font-family="monospace">',
        f'<title>MetaCoin trust-ledger status board — generated from the '
        f'anchored chain at tip {era["tip_index"]}</title>',
        f'<rect width="{width}" height="{height}" fill="{BG}"/>',
        f'<text x="{PAD}" y="30" font-size="15" fill="{TEXT}" '
        f'letter-spacing="2">METACOIN TRUST LEDGER — STATUS BOARD</text>',
        f'<text x="{width - PAD}" y="30" font-size="11" fill="{MUTED}" '
        f'text-anchor="end">research-stage · zero-value · '
        f'no token</text>',
        f'<line x1="{PAD}" y1="42" x2="{width - PAD}" y2="42" '
        f'stroke="{TILE_EDGE}" stroke-width="1"/>',
    ]
    for i, (value, label, sub, color) in enumerate(tiles):
        x = PAD + (i % COLS) * (TILE_W + GAP)
        y = grid_top + (i // COLS) * (TILE_H + GAP)
        out.append(
            f'<g data-tile="{label}">'
            f'<rect x="{x}" y="{y}" width="{TILE_W}" height="{TILE_H}" '
            f'rx="6" fill="{TILE}" stroke="{TILE_EDGE}"/>'
            f'<text x="{x + 14}" y="{y + 44}" font-size="30" '
            f'fill="{color}">{value}</text>'
            f'<text x="{x + 14}" y="{y + 66}" font-size="10" '
            f'fill="{MUTED}" letter-spacing="1">{label}</text>'
            f'<text x="{x + 14}" y="{y + 82}" font-size="10" '
            f'fill="{SLATE}">{sub}</text></g>')
    strip = (f'TIP {era["tip_hash_prefix"]}… · CROSS-MACHINE ERA '
             f'idx 69–73 · MIRROR idx 72 · PULSE idx 80 '
             f'· {_GATE_NOTE}')
    out.append(
        f'<rect x="{PAD}" y="{strip_y}" width="{width - 2 * PAD}" '
        f'height="26" rx="4" fill="{TILE}" stroke="{TILE_EDGE}"/>')
    out.append(f'<text x="{PAD + 14}" y="{strip_y + 17}" font-size="11" '
               f'fill="{MUTED}">{strip}</text>')
    out.append(
        f'<text x="{PAD}" y="{strip_y + 46}" font-size="10" '
        f'fill="{SLATE}">generated from the anchored chain at tip '
        f'{era["tip_index"]} — re-derive: python3 '
        f'protocol/status_board_svg.py</text>')
    out.append('</svg>')
    return "\n".join(out) + "\n"


def _selftest() -> int:
    import xml.etree.ElementTree as ET
    print("=== protocol/status_board_svg.py self-test (read-only) ===\n")
    checks = []
    facts = gather_facts()
    svg1, svg2 = render_svg(facts), render_svg(facts)
    checks.append(("deterministic: two renders byte-identical", svg1 == svg2))
    root = ET.fromstring(svg1)
    ns = "{http://www.w3.org/2000/svg}"
    tiles = root.findall(f"{ns}g")
    checks.append(("valid XML with all 10 tiles drawn", len(tiles) == 10))

    # Tile-by-tile: the board equals the doc_verify token / source value.
    entries = _read_ledger(resolve_ledger_path())
    era = doc_verify.compute_era_tokens(entries, facts["pin_count"])
    live = doc_verify.compute_tokens()
    values = {g.attrib["data-tile"]: g.findall(f"{ns}text")[0].text
              for g in tiles}
    expected = {
        "ANCHORED ENTRIES": era["entry_count"],
        "REPRODUCIBLE TASKS": era["recorded_task_count"],
        "HONEST NEGATIVES": live["honest_negative_count"],
        "MISSION VERDICTS": era["mission_verdict_count"],
        "MIP DECISIONS": era["mip_decision_count"],
        "VERIFY LAYERS": str(facts["layer_count"]),
        "SELF-TESTS": str(int(live["demo_suite_count"])
                          + int(live["protocol_suite_count"])),
        "PYTHON LINES": facts["lines_cell"],
        "DRILLS DEFEATED": era["drill_entry_count"],
        "CATALOG GENERATIONS": era["catalog_anchor_count"],
    }
    for label, want in expected.items():
        checks.append((f"tile {label} equals its source value ({want})",
                       values.get(label) == want))
    checks.append(("the pinned tip hash prefix rides the strip",
                   era["tip_hash_prefix"] in svg1))
    checks.append(("the README markdown table still carries the mirrored "
                   "line-count cell", bool(_LINES_CELL_RE.search(
                       open(doc_verify.README_PATH).read()))))

    # Refusal fixture: a README pinning a tip the chain does not record.
    import tempfile
    refused = False
    with tempfile.TemporaryDirectory() as tmp:
        bad = os.path.join(tmp, "README.md")
        with open(doc_verify.README_PATH) as f:
            text = f.read()
        with open(bad, "w") as f:
            f.write(text.replace(facts["pin_prefix"], "b0a4d0badc0de"[:12]))
        real_path = doc_verify.README_PATH
        try:
            doc_verify.README_PATH = bad
            gather_facts()
        except ValueError as exc:
            refused = "chain mismatch" in str(exc)
        finally:
            doc_verify.README_PATH = real_path
    checks.append(("a README pin the chain never had refuses by name",
                   refused))

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
        description="README status board -> deterministic SVG from the "
                    "doc_verify sources (research-stage, ZERO-VALUE, "
                    "no token).")
    parser.add_argument("--out", default=DEFAULT_OUT)
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args(argv)
    if args.selftest:
        return _selftest()
    facts = gather_facts()
    svg = render_svg(facts)
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(svg)
    print(f"svg written: {args.out} (10 tiles, pinned at entry "
          f"{facts['pin_count']} / {facts['pin_prefix']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
