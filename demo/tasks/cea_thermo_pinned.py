# Copyright (c) 2023-2026 MetaCoin-Lab.
# Licensed under SML-1.0 — see LICENSE.md.
"""cea_thermo_pinned — NASA CEA 9-coefficient thermodynamic polynomials for the
four Sabatier species, PINNED VERBATIM from the open-source CEA release.

Research-only. This module is DATA with a provenance header, not a task: the
Sabatier equilibrium family (task-0019, task-0020) evaluates these blocks
with the standard library. Every line of every species block below is a
byte-exact copy of the line in the fetched file (fixed-width columns,
leading spaces preserved) — extracted by a script from the file bytes, never
transcribed by hand, never from memory. Maps to NASA Technology Taxonomy TX07
(Exploration Destination Systems — ISRU) through the tasks that consume it.

PROVENANCE (the PASS-1 pinning discipline, first real use):
  source repository : https://github.com/nasa/cea
  release / tag     : v3.3.3 (published 2026-08-24T16:21:56Z)
  tag commit        : 059439a346c98874ca7dd153c8305ee249dfd733
  file path         : data/thermo.inp
  fetched from      : https://raw.githubusercontent.com/nasa/cea/059439a346c98874ca7dd153c8305ee249dfd733/data/thermo.inp
  fetched (UTC)     : 2026-08-28T09:19:14Z
  file size         : 1234323 bytes
  sha256(thermo.inp): fa7746572952d74e249e818a82a35c113829742fb421a308e167185528884363
  species blocks    : CO2 (file line 2701), H2 (line 5682), CH4 (line 2521), H2O (line 5755) — gas phase
  data references   : as carried in each block's own header line (Gurvich 1978/1991; Cox 1989; Woolley 1987; TRC) — the file's six-character reference-date codes
  license           : Apache License 2.0 (nasa/cea LICENSE.txt); this module reproduces four data blocks with attribution

THE POLYNOMIAL FORM (NASA 9-coefficient, as the file's exponent rows
"-2.0 -1.0 0.0 1.0 2.0 3.0 4.0" declare and as documented in NASA
RP-1311 / TP-2002-211556, McBride, Zehe & Gordon):
  Cp/R    = a1 T^-2 + a2 T^-1 + a3 + a4 T + a5 T^2 + a6 T^3 + a7 T^4
  H/(RT)  = -a1 T^-2 + a2 ln(T)/T + a3 + a4 T/2 + a5 T^2/3 + a6 T^3/4
            + a7 T^4/5 + b1/T
  S/R     = -a1 T^-2/2 - a2 T^-1 + a3 ln(T) + a4 T + a5 T^2/2 + a6 T^3/3
            + a7 T^4/4 + b2
Per interval the file carries a1..a7 on two rows (five + two fields of
sixteen characters, Fortran "D" exponents) followed by b1, b2. H is
absolute enthalpy on the file's reference state (Hf at 298.15 K is the
last field of each block's second line and is asserted by the tasks as a
known-truth check).

Not financial, legal, or flight-engineering advice. No NASA affiliation or
endorsement. Standard library only. No randomness.
"""

import hashlib

PROVENANCE = {
    "source_repository": "https://github.com/nasa/cea",
    "release_tag": "v3.3.3",
    "release_published_utc": "2026-08-24T16:21:56Z",
    "tag_commit": "059439a346c98874ca7dd153c8305ee249dfd733",
    "file_path": "data/thermo.inp",
    "fetched_utc": "2026-08-28T09:19:14Z",
    "file_size_bytes": 1234323,
    "file_sha256": "fa7746572952d74e249e818a82a35c113829742fb421a308e167185528884363",
    "license": "Apache-2.0",
    "polynomial_form": "NASA 9-coefficient (RP-1311 / TP-2002-211556)",
}

GAS_CONSTANT_J_MOL_K = 8.31446261815324  # exact SI (2019 redefinition)

# The verbatim species blocks: (file line number of the name line, lines).
SPECIES_BLOCKS = {
    "CO2": (2701, [
        "CO2               Gurvich,1991 pt1 p27 pt2 p24.\r",
        " 3 g 9/99 C   1.00O   2.00    0.00    0.00    0.00 0   44.0095000    -393510.000\r",
        "    200.000   1000.0007 -2.0 -1.0  0.0  1.0  2.0  3.0  4.0  0.0         9365.469\r",
        " 4.943650540D+04-6.264116010D+02 5.301725240D+00 2.503813816D-03-2.127308728D-07\r",
        "-7.689988780D-10 2.849677801D-13                -4.528198460D+04-7.048279440D+00\r",
        "   1000.000   6000.0007 -2.0 -1.0  0.0  1.0  2.0  3.0  4.0  0.0         9365.469\r",
        " 1.176962419D+05-1.788791477D+03 8.291523190D+00-9.223156780D-05 4.863676880D-09\r",
        "-1.891053312D-12 6.330036590D-16                -3.908350590D+04-2.652669281D+01\r",
        "   6000.000  20000.0007 -2.0 -1.0  0.0  1.0  2.0  3.0  4.0  0.0         9365.469\r",
        "-1.544423287D+09 1.016847056D+06-2.561405230D+02 3.369401080D-02-2.181184337D-06\r",
        " 6.991420840D-11-8.842351500D-16                -8.043214510D+06 2.254177493D+03\r",
    ]),
    "H2": (5682, [
        "H2                Ref-Elm. Gurvich,1978 pt1 p103 pt2 p31.\r",
        " 3 tpis78 H   2.00    0.00    0.00    0.00    0.00 0    2.0158800          0.000\r",
        "    200.000   1000.0007 -2.0 -1.0  0.0  1.0  2.0  3.0  4.0  0.0         8468.102\r",
        " 4.078323210D+04-8.009186040D+02 8.214702010D+00-1.269714457D-02 1.753605076D-05\r",
        "-1.202860270D-08 3.368093490D-12                 2.682484665D+03-3.043788844D+01\r",
        "   1000.000   6000.0007 -2.0 -1.0  0.0  1.0  2.0  3.0  4.0  0.0         8468.102\r",
        " 5.608128010D+05-8.371504740D+02 2.975364532D+00 1.252249124D-03-3.740716190D-07\r",
        " 5.936625200D-11-3.606994100D-15                 5.339824410D+03-2.202774769D+00\r",
        "   6000.000  20000.0007 -2.0 -1.0  0.0  1.0  2.0  3.0  4.0  0.0         8468.102\r",
        " 4.966884120D+08-3.147547149D+05 7.984121880D+01-8.414789210D-03 4.753248350D-07\r",
        "-1.371873492D-11 1.605461756D-16                 2.488433516D+06-6.695728110D+02\r",
    ]),
    "CH4": (2521, [
        "CH4               Gurvich,1991 pt1 p44 pt2 p36.\r",
        " 2 g 8/99 C   1.00H   4.00    0.00    0.00    0.00 0   16.0424600     -74600.000\r",
        "    200.000   1000.0007 -2.0 -1.0  0.0  1.0  2.0  3.0  4.0  0.0        10016.202\r",
        "-1.766850998D+05 2.786181020D+03-1.202577850D+01 3.917619290D-02-3.619054430D-05\r",
        " 2.026853043D-08-4.976705490D-12                -2.331314360D+04 8.904322750D+01\r",
        "   1000.000   6000.0007 -2.0 -1.0  0.0  1.0  2.0  3.0  4.0  0.0        10016.202\r",
        " 3.730042760D+06-1.383501485D+04 2.049107091D+01-1.961974759D-03 4.727313040D-07\r",
        "-3.728814690D-11 1.623737207D-15                 7.532066910D+04-1.219124889D+02\r",
    ]),
    "H2O": (5755, [
        "H2O               Hf:Cox,1989. Woolley,1987. TRC(10/88) tuv25.\r",
        " 2 g 8/89 H   2.00O   1.00    0.00    0.00    0.00 0   18.0152800    -241826.000\r",
        "    200.000   1000.0007 -2.0 -1.0  0.0  1.0  2.0  3.0  4.0  0.0         9904.092\r",
        "-3.947960830D+04 5.755731020D+02 9.317826530D-01 7.222712860D-03-7.342557370D-06\r",
        " 4.955043490D-09-1.336933246D-12                -3.303974310D+04 1.724205775D+01\r",
        "   1000.000   6000.0007 -2.0 -1.0  0.0  1.0  2.0  3.0  4.0  0.0         9904.092\r",
        " 1.034972096D+06-2.412698562D+03 4.646110780D+00 2.291998307D-03-6.836830480D-07\r",
        " 9.426468930D-11-4.822380530D-15                -1.384286509D+04-7.978148510D+00\r",
    ]),
}

# sha256 of the four verbatim blocks concatenated (a change to any pinned
# line is refused by the self-test below).
PINNED_BLOCKS_SHA256 = "7d1388222366895b9d8ac4b8a0e9391218c54277abab870dd3c6b13297dd9294"

MAX_INTERVALS_PER_SPECIES = 3   # the file declares at most three for these four
FIELD_WIDTH_CHARS = 16


def _fields(row: str) -> list:
    """Split one coefficient row into its fixed-width numeric fields."""
    out = []
    for start in range(0, 5 * FIELD_WIDTH_CHARS, FIELD_WIDTH_CHARS):  # bounded: 5 fields
        text = row[start:start + FIELD_WIDTH_CHARS].strip().replace("D", "E")
        if text:
            out.append(float(text))
    return out


def parse_species(name: str) -> dict:
    """{'hf_298_J_mol': float, 'intervals': [{'t_low_K', 't_high_K', 'a': [a1..a7], 'b': [b1, b2]}]}
    from the pinned verbatim block. Bounded loops only; no recursion."""
    _line, block = SPECIES_BLOCKS[name]
    n_intervals = int(block[1][:2])
    assert 1 <= n_intervals <= MAX_INTERVALS_PER_SPECIES, "interval count outside the pinned bound"
    hf_298 = float(block[1][65:80])
    intervals = []
    for k in range(n_intervals):  # bounded by MAX_INTERVALS_PER_SPECIES
        header = block[2 + 3 * k]
        coeffs = _fields(block[3 + 3 * k]) + _fields(block[4 + 3 * k])
        assert len(coeffs) == 9, f"{name} interval {k}: expected 9 coefficients"
        intervals.append({"t_low_K": float(header[1:11]), "t_high_K": float(header[11:22]),
                           "a": coeffs[:7], "b": coeffs[7:]})
    return {"hf_298_J_mol": hf_298, "intervals": intervals}


def blocks_sha256() -> str:
    text = "".join("\n".join(lines) for _ln, lines in SPECIES_BLOCKS.values())
    return hashlib.sha256(text.encode("latin-1")).hexdigest()


if __name__ == "__main__":
    print("=== cea_thermo_pinned self-test (data integrity, no network) ===")
    ok = blocks_sha256() == PINNED_BLOCKS_SHA256
    print(f"pinned blocks sha256 {PINNED_BLOCKS_SHA256[:16]}...: {'PASS' if ok else 'FAIL'}")
    for sp in SPECIES_BLOCKS:
        d = parse_species(sp)
        print(f"{sp:4s} Hf(298.15) {d['hf_298_J_mol']:>12.1f} J/mol, {len(d['intervals'])} interval(s): "
              + ", ".join(f"[{iv['t_low_K']:.0f},{iv['t_high_K']:.0f}] K" for iv in d["intervals"]))
        ok = ok and all(len(iv["a"]) == 7 and len(iv["b"]) == 2 for iv in d["intervals"])
    print("provenance:", PROVENANCE["source_repository"], PROVENANCE["release_tag"],
          PROVENANCE["tag_commit"][:12], "sha256", PROVENANCE["file_sha256"][:16] + "...")
    print("=== self-test summary: " + ("ALL CHECKS BEHAVED CORRECTLY" if ok else "FAILURE") + " ===")
    raise SystemExit(0 if ok else 1)
