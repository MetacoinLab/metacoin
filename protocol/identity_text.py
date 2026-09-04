# Copyright (c) 2023-2026 MetaCoin-Lab.
# Licensed under SML-1.0 — see LICENSE.md.
"""identity_text.py — the FROZEN identity strings.

OPERATOR-OWNED — edit only on the operator's explicit approval.

These five strings are the operator-approved identity text of the project,
byte-for-byte. Nothing else in the repository is allowed to introduce wording
for them: the README header is checked against these constants on every CI
run (protocol/doc_verify.py, check_identity), and the hero banner generator
(protocol/hero_svg.py) reads them from here and may render ONLY these strings
(its self-test refuses any text node that is not one of them). Layout, type,
colour, and line-wrapping are presentation and may change; the words may not.

Provenance: TAGLINE, MOTTO, and LINEAGE are the August-batch README header
(commit 5fc36db, lines 8, 9, 23); DEFINITION is that README's one-sentence
thesis (line 45). Restored verbatim and approved by the operator 2026-09-04.
STAKE is new text, supplied verbatim and approved by the operator 2026-09-03;
it sits in the README header as the blockquote directly below DEFINITION.
Research-stage; no token; not financial, legal, or engineering advice.
"""

TAGLINE = ("The credibly neutral money-and-work protocol for the Space "
           "Machine Economy.")

MOTTO = "Money for machines building the stars."

DEFINITION = (
    "MetaCoin is a credibly neutral, fair-launch base currency for the Space "
    "Machine Economy: minted only through objective, programmatic, "
    "hard-to-fake infrastructure work — while a separate, fee-funded MetaStar "
    "Treasury pays humans, AI agents, and bounded-autonomous robots to build "
    "the software, energy, robotics, and research primitives of the space "
    "economy.")

LINEAGE = ("Gold was the money of the old world. Bitcoin became the money of "
           "the digital world. MetaCoin is designed to become the "
           "work-and-energy currency of the Space Machine Economy.")

STAKE = (
    "Every hardware standard teaches agents and robots how to act. None of "
    "them proves what was actually done. MetaCoin is that missing layer: an "
    "append-only record where a machine's work counts only if a stranger can "
    "re-derive it, bit for bit — and where an honest \"it failed\" is worth "
    "as much as a success.")

# The lineage strip's three panels: a name and a caption. Every caption is a
# verbatim substring of LINEAGE (asserted below) — the strip re-arranges the
# frozen sentence, it does not paraphrase it.
LINEAGE_STAGES = (
    ("GOLD", "the money of the old world"),
    ("BITCOIN", "the money of the digital world"),
    ("METACOIN", "designed to become the work-and-energy currency of the "
                 "Space Machine Economy"),
)

# The key phrases the README sets in bold inside DEFINITION (presentation
# only; each must be a verbatim substring of DEFINITION — asserted below).
DEFINITION_BOLD = (
    "credibly neutral",
    "fair-launch",
    "objective, programmatic, hard-to-fake infrastructure work",
    "MetaStar Treasury",
    "humans, AI agents, and bounded-autonomous robots",
)

# The two phrases the README sets in bold inside STAKE (presentation only;
# each must be a verbatim substring of STAKE — asserted below).
STAKE_BOLD = (
    "None of them proves what was actually done.",
    'an honest "it failed" is worth as much as a success',
)

# Everything a generator may emit as a text node, and nothing else.
FROZEN = (TAGLINE, MOTTO, DEFINITION, LINEAGE, STAKE) + tuple(
    s for pair in LINEAGE_STAGES for s in pair)

for _name, _caption in LINEAGE_STAGES:
    assert _caption in LINEAGE, f"lineage caption not in LINEAGE: {_caption!r}"
    assert _name.lower() in LINEAGE.lower(), f"lineage name not in LINEAGE: {_name!r}"
for _phrase in DEFINITION_BOLD:
    assert _phrase in DEFINITION, f"bold phrase not in DEFINITION: {_phrase!r}"
for _phrase in STAKE_BOLD:
    assert _phrase in STAKE, f"bold phrase not in STAKE: {_phrase!r}"
del _name, _caption, _phrase
