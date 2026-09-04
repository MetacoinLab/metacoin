# Copyright (c) 2023-2026 MetaCoin-Lab.
# Licensed under SML-1.0 — see LICENSE.md.
"""hero_svg.py — deterministic SVG hero banner for the README header.

================== HONESTY / SCOPE (READ ME) ==================
Presentation only. Every word on the banner is a FROZEN, operator-owned
string read from protocol/identity_text.py — this module cannot introduce
wording, and its self-test refuses any text node that is not one of those
constants. Standard library only; ZERO ledger writes; byte-deterministic
(the starfield is a seeded linear-congruential sequence, not random()).
Palette: the status-board / mission-DAG dark panel family plus the emblem's
own gold and blue. No gradients for decoration (the embedded emblem carries
its own, as shipped). No claims beyond the frozen text.

Usage:
    python3 protocol/hero_svg.py            # -> assets/hero.svg
    python3 protocol/hero_svg.py --selftest
Not financial, legal, or engineering advice.
"""

import sys
sys.dont_write_bytecode = True

import argparse
import os
import re
from xml.sax.saxutils import escape

_PROTO_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_PROTO_DIR)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from protocol import identity_text as T

DEFAULT_OUT = os.path.join(_REPO_ROOT, "assets", "hero.svg")
EMBLEM_PATH = os.path.join(_REPO_ROOT, "assets", "metacoin-logo.svg")

W, H = 1400, 520
BG = "#0b0e14"          # status-board ground
PANEL = "#141a23"
EDGE = "#2a3442"
TEXT = "#e6edf3"
MUTED = "#9aa7b3"
SLATE = "#5c6f82"
GOLD = "#ffc23a"        # the emblem's gold
BLUE = "#2f6bff"        # the emblem's blue
STAR_SEED = 0x4D43      # 'MC' — fixed; the field never changes between runs
STAR_COUNT = 160


def _stars(seed=STAR_SEED, n=STAR_COUNT):
    """Deterministic starfield: a 32-bit LCG (Numerical Recipes constants)."""
    x = seed & 0xFFFFFFFF
    out = []
    for _ in range(n):
        x = (1664525 * x + 1013904223) & 0xFFFFFFFF
        px = (x >> 8) % W
        x = (1664525 * x + 1013904223) & 0xFFFFFFFF
        py = (x >> 8) % H
        x = (1664525 * x + 1013904223) & 0xFFFFFFFF
        r = 0.5 + ((x >> 8) % 100) / 100.0          # 0.50 .. 1.49 px
        x = (1664525 * x + 1013904223) & 0xFFFFFFFF
        op = 0.25 + ((x >> 8) % 60) / 100.0         # 0.25 .. 0.84
        out.append((px, py, r, op))
    return out


def _emblem_inner():
    """The shipped emblem, nested as an inner <svg> (its own defs travel with
    it; nothing here references them)."""
    with open(EMBLEM_PATH, encoding="utf-8") as f:
        svg = f.read()
    body = re.sub(r"^.*?<svg[^>]*>", "", svg, count=1, flags=re.S)
    body = re.sub(r"</svg>\s*$", "", body, flags=re.S)
    return body


def _text(x, y, size, fill, content, anchor="start", style="", tspans=None):
    """One <text> whose joined text equals exactly one frozen string."""
    attrs = (f'x="{x}" y="{y}" font-size="{size}" fill="{fill}" '
             f'text-anchor="{anchor}"' + (f' {style}' if style else ""))
    if tspans is None:
        return f'<text {attrs}>{escape(content)}</text>'
    parts = []
    for i, (line, dy) in enumerate(tspans):
        parts.append(f'<tspan x="{x}" dy="{dy}">{escape(line)}</tspan>')
    return f'<text {attrs}>' + "".join(parts) + '</text>'


def _split_tagline(text):
    """Two lines, broken at the last ' for ' (layout only; joined with one
    space the lines re-form the constant — the self-test checks that)."""
    i = text.rfind(" for ")
    return [text[:i], text[i + 1:]]


def _glyph(kind, cx, cy):
    """Original geometric glyphs: an ingot, a block, an orbit ring."""
    if kind == "GOLD":      # ingot: a trapezoid with a top face
        return (f'<polygon points="{cx-30},{cy+14} {cx+30},{cy+14} '
                f'{cx+22},{cy-6} {cx-22},{cy-6}" fill="none" stroke="{GOLD}" '
                f'stroke-width="2.5" stroke-linejoin="round"/>'
                f'<polygon points="{cx-22},{cy-6} {cx+22},{cy-6} '
                f'{cx+16},{cy-18} {cx-16},{cy-18}" fill="none" '
                f'stroke="{GOLD}" stroke-width="2.5" stroke-linejoin="round"/>')
    if kind == "BITCOIN":   # block: a square with a chained second outline
        return (f'<rect x="{cx-22}" y="{cy-22}" width="40" height="40" '
                f'fill="none" stroke="{TEXT}" stroke-width="2.5"/>'
                f'<rect x="{cx-12}" y="{cy-12}" width="40" height="40" '
                f'fill="none" stroke="{MUTED}" stroke-width="1.5" '
                f'stroke-dasharray="4 3"/>')
    # METACOIN: a small orbit ring with a body on the ring
    return (f'<circle cx="{cx}" cy="{cy}" r="6" fill="{GOLD}"/>'
            f'<ellipse cx="{cx}" cy="{cy}" rx="30" ry="12" fill="none" '
            f'stroke="{BLUE}" stroke-width="2" '
            f'transform="rotate(-20 {cx} {cy})"/>'
            f'<circle cx="{cx+27}" cy="{cy-8}" r="3.5" fill="{TEXT}"/>')


def render_svg() -> str:
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
        f'viewBox="0 0 {W} {H}" font-family="monospace">',
        f'<title>{escape(T.TAGLINE)}</title>',
        f'<rect width="{W}" height="{H}" fill="{BG}"/>',
    ]
    for px, py, r, op in _stars():
        out.append(f'<circle cx="{px}" cy="{py}" r="{r:.2f}" fill="{TEXT}" '
                   f'opacity="{op:.2f}"/>')
    # emblem, left
    out.append('<svg x="48" y="34" width="216" height="216" '
               'viewBox="0 0 800 800">' + _emblem_inner() + '</svg>')
    # tagline (two layout lines) + motto
    l1, l2 = _split_tagline(T.TAGLINE)
    out.append(_text(300, 96, 34, TEXT, T.TAGLINE, style='font-weight="bold"',
                     tspans=[(l1, 0), (l2, 44)]))
    out.append(_text(300, 190, 21, GOLD, T.MOTTO, style='font-style="italic"'))
    # lineage strip: three panels on a thin orbit line
    y0, ph, pw = 292, 110, 380
    xs = [110, 510, 910]
    out.append(f'<path d="M 60,{y0 + 40} Q 700,{y0 - 70} 1340,{y0 + 40}" '
               f'fill="none" stroke="{SLATE}" stroke-width="1" '
               f'stroke-dasharray="3 5"/>')
    for (name, caption), x in zip(T.LINEAGE_STAGES, xs):
        out.append(f'<g data-stage="{name}">'
                   f'<rect x="{x}" y="{y0}" width="{pw}" height="{ph}" rx="8" '
                   f'fill="{PANEL}" stroke="{EDGE}"/>'
                   + _glyph(name, x + 44, y0 + 52)
                   + _text(x + 96, y0 + 44, 18, TEXT, name,
                           style='letter-spacing="3"')
                   + _text(x + 96, y0 + 70, 12, MUTED, caption,
                           tspans=_wrap(caption, 36))
                   + '</g>')
    # the lineage sentence, verbatim, beneath the strip (two layout lines
    # broken at a sentence boundary; joined with one space they re-form it)
    cut = T.LINEAGE.index(" MetaCoin is designed")
    out.append(_text(W // 2, 462, 15, MUTED, T.LINEAGE, anchor="middle",
                     style='font-style="italic"',
                     tspans=[(T.LINEAGE[:cut], 0), (T.LINEAGE[cut + 1:], 22)]))
    out.append('</svg>')
    return "\n".join(out) + "\n"


def _wrap(text, width):
    """Greedy word wrap into tspans; joined with single spaces the lines
    re-form the caption exactly."""
    words, lines, cur = text.split(" "), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > width:
            lines.append(cur)
            cur = w
        else:
            cur = (cur + " " + w) if cur else w
    lines.append(cur)
    return [(ln, 0 if i == 0 else 16) for i, ln in enumerate(lines)]


def text_nodes(svg_text):
    """Every <text> element's content: tspans joined by one space (layout
    line breaks are not words), plus the <title>."""
    import xml.etree.ElementTree as ET
    ns = "{http://www.w3.org/2000/svg}"
    root = ET.fromstring(svg_text)
    nodes = []
    for el in root.iter(f"{ns}text"):
        spans = el.findall(f"{ns}tspan")
        if spans:
            nodes.append(" ".join((s.text or "") for s in spans))
        else:
            nodes.append(el.text or "")
    for el in root.iter(f"{ns}title"):
        nodes.append(el.text or "")
    return nodes


def _selftest() -> int:
    import xml.etree.ElementTree as ET
    print("=== protocol/hero_svg.py self-test (read-only) ===\n")
    checks = []
    svg1, svg2 = render_svg(), render_svg()
    checks.append(("deterministic: two renders byte-identical", svg1 == svg2))
    root = ET.fromstring(svg1)
    ns = "{http://www.w3.org/2000/svg}"
    checks.append(("valid XML; the emblem is embedded as an inner <svg>",
                   len(root.findall(f"{ns}svg")) == 1))
    nodes = text_nodes(svg1)
    stray = [n for n in nodes if n not in T.FROZEN]
    checks.append((f"every text node ({len(nodes)}) equals a frozen "
                   "identity string — no stray words", not stray))
    for s in stray:
        print(f"    STRAY: {s!r}")
    for want in (T.TAGLINE, T.MOTTO, T.LINEAGE):
        checks.append((f"the banner carries {want[:32]!r}… verbatim",
                       want in nodes))
    stages = [g.attrib["data-stage"] for g in root.iter(f"{ns}g")
              if "data-stage" in g.attrib]
    checks.append(("three lineage panels in order (GOLD, BITCOIN, METACOIN)",
                   stages == [n for n, _ in T.LINEAGE_STAGES]))
    checks.append(("no gradient or filter defined by this module (the "
                   "emblem's own defs excepted)",
                   svg1.count("<linearGradient") + svg1.count("<radialGradient")
                   == open(EMBLEM_PATH).read().count("Gradient id=")))
    if os.path.exists(DEFAULT_OUT):
        with open(DEFAULT_OUT, encoding="utf-8") as f:
            checks.append(("the committed assets/hero.svg equals a fresh "
                           "render (stale asset refused)", f.read() == svg1))
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
        description="Deterministic README hero banner from the frozen "
                    "identity text (presentation only; no wording of its own).")
    parser.add_argument("--out", default=DEFAULT_OUT)
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args(argv)
    if args.selftest:
        return _selftest()
    svg = render_svg()
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(svg)
    print(f"svg written: {args.out} ({len(text_nodes(svg))} text nodes, "
          f"all frozen identity strings)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
