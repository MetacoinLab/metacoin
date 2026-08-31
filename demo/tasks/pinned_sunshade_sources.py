"""pinned_sunshade_sources — the pinned physical constants for the mission-0002
"L1 sunshade via lunar mass driver" task family, each with its own provenance
block (the CEA pinning discipline, second use).

Research-only. This module is DATA with provenance headers, not a task: the
sunshade family (task-0022 … task-0029) computes from these constants with the
standard library. NOTHING here is from memory alone: every constant carries a
`provenance` block naming its public source, and the blocks are honest about
HOW each value was obtained, in three tiers:

  fetched-hashed   — the source document was fetched on the recorded date and
                     its exact bytes hashed (the CEA thermo.inp discipline);
  fetched-page     — the value was read from the named public page/API on the
                     recorded date (page bytes not archived here; URL + date
                     recorded so anyone can re-fetch);
  document-cited   — the publisher refuses non-browser access (403) or the
                     historical page is decommissioned, so the value is cited
                     to the printed document (title, DOI/edition, table) and
                     the access refusal is RECORDED, never papered over.

A tier-3 citation is a weaker pin than a hash and says so — the protocol
records the honest provenance it could obtain on the recorded date, and a
later pass can upgrade tiers append-only. Access notes as of 2026-08-31: the
NASA NSSDC planetary fact sheets (the library's earlier citation habit)
redirect to nasa.gov/nssdc (decommissioned as direct pages); ipcc.ch and
journals.ametsoc.org refuse non-browser fetches (HTTP 403).

Sources are public scientific documents used independently; no affiliation
with, or endorsement by, the IAU, IPCC, NASA/JPL, or any author is implied.
Test-META is a zero-value testnet placeholder and never mints base supply
(MIP-0001 paragraph 3, MIP-0002 paragraph 8). Not financial, legal, or
flight-engineering advice. No NASA affiliation or endorsement.
"""

# ---------------------------------------------------------------------------
# TIER 1 — fetched-hashed: IAU 2015 Resolution B3 nominal conversion constants
# (Mamajek, Prsa, Torres, Harmanec, Asplund et al., "IAU 2015 Resolution B3 on
# Recommended Nominal Conversion Constants for Selected Solar and Planetary
# Properties", arXiv:1510.07674v1, 26 Oct 2015). Values are IAU-defined EXACT
# nominal SI values (the resolution's own table, pages 3).
# ---------------------------------------------------------------------------
IAU_B3_PROVENANCE = {
    "tier": "fetched-hashed",
    "source": "IAU 2015 Resolution B3 (arXiv:1510.07674v1)",
    "fetched_from": "https://arxiv.org/pdf/1510.07674",
    "fetched_utc": "2026-08-31",
    "sha256_pdf": "a4a38ecf4ab6ac71a38780456beffbc43c17c98ed320e82585a127057e498417",
    "note": "nominal values are IAU-defined exact conversion constants",
}
SOLAR_IRRADIANCE_W_M2 = 1361.0        # 1 S_sun^N (B3 table)
SOLAR_LUMINOSITY_W = 3.828e26         # 1 L_sun^N
SOLAR_RADIUS_M = 6.957e8              # 1 R_sun^N
GM_SUN_M3_S2 = 1.3271244e20           # 1 (GM)_sun^N
GM_EARTH_M3_S2 = 3.986004e14          # 1 (GM)_E^N
EARTH_EQ_RADIUS_M = 6.3781e6          # 1 R_eE^N
AU_M = 1.495978707e11                 # IAU 2012 Resolution B2 exact au
                                      # (restated in B3 endnote 4)

# ---------------------------------------------------------------------------
# TIER 2 — fetched-page: JPL Solar System Dynamics, planetary satellite
# physical parameters (the Moon row). The chain DERIVES lunar escape velocity
# from GM and R rather than quoting a fact-sheet number.
# ---------------------------------------------------------------------------
JPL_MOON_PROVENANCE = {
    "tier": "fetched-page",
    "source": "JPL SSD planetary satellite physical parameters",
    "fetched_from": "https://ssd.jpl.nasa.gov/sats/phys_par/",
    "fetched_utc": "2026-08-31",
    "gm_basis": "DE440",
    "radius_basis": "Archinal et al. (2018), mean radius",
}
GM_MOON_KM3_S2 = 4902.800             # +/- 0.001 (DE440)
R_MOON_KM = 1737.4                    # +/- 0.1 (mean radius)

# ---------------------------------------------------------------------------
# TIER 2 — fetched-page: the AR6-assessed total anthropogenic effective
# radiative forcing, 2019 relative to 1750, as restated in the open-access
# annual-indicators paper (their Table 3, quoting IPCC AR6 WG1).
# ---------------------------------------------------------------------------
ERF_PROVENANCE = {
    "tier": "fetched-page",
    "source": ("Forster et al. 2023, 'Indicators of Global Climate Change "
               "2022', Earth Syst. Sci. Data 15, 2295, Table 3 (AR6-assessed "
               "value, 1750-2019)"),
    "doi": "10.5194/essd-15-2295-2023",
    "fetched_from": "https://essd.copernicus.org/articles/15/2295/2023/",
    "fetched_utc": "2026-08-31",
    "quoted": "2.72 [1.96 to 3.48] W m-2",
}
ERF_ANTHROPOGENIC_2019_W_M2 = 2.72

# ---------------------------------------------------------------------------
# TIER 3 — document-cited (access refusal recorded, 2026-08-31)
# ---------------------------------------------------------------------------
ERF_2XCO2_PROVENANCE = {
    "tier": "document-cited",
    "source": ("IPCC AR6 WG1 Chapter 7 (Forster et al. 2021), Table 7.4: "
               "assessed ERF for a doubling of CO2"),
    "doi": "10.1017/9781009157896.009",
    "access_note": "ipcc.ch refuses non-browser fetch (HTTP 403, 2026-08-31)",
}
ERF_2XCO2_W_M2 = 3.93                 # 3.93 +/- 0.47 W m-2 (Table 7.4)

BOND_ALBEDO_PROVENANCE = {
    "tier": "document-cited",
    "source": ("NASA NSSDC Earth Fact Sheet (Williams, D.R.), Bond albedo "
               "0.294; corroborated by Stephens et al. 2015, 'The albedo of "
               "Earth', Rev. Geophys. 53, 141 ('the albedo of Earth is "
               "0.29'), doi:10.1002/2014RG000449"),
    "access_note": ("nssdc.gsfc.nasa.gov fact sheets redirect to "
                    "nasa.gov/nssdc (decommissioned as direct pages, "
                    "verified 2026-08-31); Wiley refuses non-browser fetch"),
}
EARTH_BOND_ALBEDO = 0.294

REGOLITH_PROVENANCE = {
    "tier": "document-cited",
    "source": ("Lunar Sourcebook (Heiken, Vaniman & French, eds., Cambridge "
               "Univ. Press, 1991), Ch. 7: Apollo 16 highland soils average "
               "~27 wt% Al2O3 (highlands soil class 26-29 wt%); "
               "representative pinned value 0.27"),
    "access_note": "public reference work, cited by edition and chapter",
}
REGOLITH_AL2O3_MASS_FRACTION = 0.27

ELECTROLYSIS_PROVENANCE = {
    "tier": "document-cited",
    "source": ("modern Hall-Heroult primary aluminum smelting DC energy, "
               "~13-15 kWh per kg Al (U.S. DOE 'Bandwidth Study on Energy "
               "Use ... U.S. Aluminum Manufacturing', 2017; IEA aluminum "
               "tracking); pinned at the class ceiling 15.0 — an honest "
               "LOWER BOUND proxy for lunar molten-regolith electrolysis, "
               "which has no flown reference process"),
    "access_note": "public reference documents, cited by title and year",
}
ELECTROLYSIS_KWH_PER_KG_AL = 15.0

SOLAR_EVOLUTION_PROVENANCE = {
    "tier": "document-cited",
    "source": ("Gough, D.O. 1981, 'Solar interior structure and luminosity "
               "variations', Solar Physics 74, 21: L(t) = L_now / "
               "(1 + (2/5)(1 - t/t_sun)); solar age t_sun = 4.57 Gyr "
               "(Bahcall et al. 1995 class value)"),
    "access_note": ("formula citation — the ~10%%-per-Gyr brightening is "
                    "DERIVED from the pinned formula, never quoted"),
}
GOUGH_LUMINOSITY_COEFFICIENT = 0.4    # the 2/5 in Gough's relation
SOLAR_AGE_GYR = 4.57

# CODATA-class atomic weights, the same conventional basis as the library's
# existing molar masses (task-0015 docstring): Al 26.9815385, O 15.999.
ATOMIC_WEIGHT_PROVENANCE = {
    "tier": "document-cited",
    "source": ("IUPAC standard atomic weights as compiled in the NIST "
               "Chemistry WebBook (the library's task-0015 convention)"),
    "access_note": "same conventional basis as the anchored task-0015",
}
M_AL_G_MOL = 26.9815385
M_O_G_MOL = 15.999

# Exact by definition (SI): the speed of light; the Julian year.
SPEED_OF_LIGHT_M_S = 299792458.0      # SI exact
JULIAN_YEAR_S = 365.25 * 86400.0      # IAU Julian year, exact by convention

# ---------------------------------------------------------------------------
# THE CLAIM UNDER VERIFICATION (recorded verbatim; provenance of the CLAIM,
# not of a constant — the mission-0002 verdict record cites this block).
# ---------------------------------------------------------------------------
CLAIM_PROVENANCE = {
    "tier": "public-assertion",
    "author": "Elon Musk (@elonmusk)",
    "venue": "X (twitter.com) post",
    "date": "2026-08-30",
    "quoted": ("AI satellites that adjust solar power reaching Earth, "
               "launched by mass driver from the Moon to Earth-Sun L1, "
               "would be able to control Earth's temperature ... could "
               "prevent runaway warming ... for the next billion years or "
               "so ... you could launch lunar dust as a temporary shade"),
    "note": ("paraphrase-faithful composite of the public post thread as "
             "reported 2026-08-30; an unverified feasibility assertion, "
             "recorded here as the CLAIM the mission-0002 chain "
             "decomposes — quoting it verifies nothing and endorses "
             "nothing, and no affiliation with the author exists"),
}


if __name__ == "__main__":
    import json
    blocks = {k: v for k, v in sorted(globals().items())
              if k.endswith("_PROVENANCE")}
    consts = {k: v for k, v in sorted(globals().items())
              if k.upper() == k and not k.endswith("_PROVENANCE")
              and isinstance(v, (int, float))}
    print(json.dumps({"constants": consts,
                      "provenance_blocks": list(blocks)}, indent=1))
    print(f"{len(consts)} pinned constants, {len(blocks)} provenance blocks")
