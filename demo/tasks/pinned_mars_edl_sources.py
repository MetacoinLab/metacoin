"""pinned_mars_edl_sources — Mars atmosphere and entry-parameter data pinned
for the EDL task family (task-0033, task-0034), with per-block provenance
(the CEA pinning discipline, fourth use).

Research-only. This module is DATA with provenance headers, not a task.
Tier-1 (fetched-and-hashed): the NASA Glenn Research Center Mars Atmosphere
Model page, whose curve fits are reproduced verbatim below. Tier-3
(document-cited, access honesty per the pinning discipline): the published
entry-parameter classes (ballistic coefficients, entry flight-path angle,
parachute-deploy Mach ceiling). The GM and radius of Mars are NOT restated
here — the EDL tasks consume them from demo/tasks/pinned_spice_sources.py
(gm_de440.tpc / pck00011.tpc, fetched-and-hashed 2026-09-01).

No NASA affiliation or endorsement; public documents used independently and
cited by title. Test-META is a zero-value testnet placeholder and never
mints base supply (MIP-0001 paragraph 3, MIP-0002 paragraph 8). Not
financial, legal, or flight-engineering advice.
"""

# ---------------------------------------------------------------------------
# TIER 1 — fetched-hashed: NASA GRC "Mars Atmosphere Model - Metric Units".
# The page states the model "was developed from measurements of the Martian
# atmosphere made by the Mars Global Surveyor in April 1996" (curve fits
# credited on-page, 1999). AN EDUCATIONAL CURVE-FIT CLASS MODEL, stated: two
# zones, linear temperature and exponential pressure; the temperature fit
# goes unphysical above ~112 km (T+273.1 crosses zero), so the EDL task
# applies drag only below its stated onset altitude and asserts positive
# absolute temperature throughout the drag domain.
# ---------------------------------------------------------------------------
GRC_ATMOSPHERE_PROVENANCE = {
    "tier": "fetched-hashed",
    "source": "NASA Glenn Research Center, 'Mars Atmosphere Model - "
              "Metric Units' (Beginner's Guide series)",
    "fetched_from": "https://www.grc.nasa.gov/www/k-12/airplane/atmosmrm.html",
    "fetched_utc": "2026-09-02T01:47:26Z",
    "size_bytes": 12810,
    "sha256_page": "246e8c0ec32de5dad2c7c37a9e025e1b83f958c7ddcb452f6e6404e985f94a81",
    "quoted_fits": ("lower (h<=7000 m): T = -31 - 0.000998*h [C]; "
                    "upper: T = -23.4 - 0.00222*h [C]; both zones: "
                    "p = .699 * exp(-0.00009*h) [kPa]; "
                    "r = p / [.1921 * (T + 273.1)] [kg/m^3]"),
    "basis_quoted": "developed from measurements of the Martian atmosphere "
                    "made by the Mars Global Surveyor in April 1996",
}
# The fits, verbatim as module constants (h in metres):
LOWER_ZONE_CEILING_M = 7000.0
T_LOWER_C0 = -31.0
T_LOWER_LAPSE_C_PER_M = -0.000998
T_UPPER_C0 = -23.4
T_UPPER_LAPSE_C_PER_M = -0.00222
P_SURFACE_KPA = 0.699
P_SCALE_PER_M = -0.00009
RHO_GAS_CONSTANT_KPA_M3_KG_K = 0.1921   # r = p / (0.1921 * (T + 273.1))
KELVIN_OFFSET_K = 273.1                 # the model's own offset, verbatim
# Derived from the pinned fit constant, never quoted separately:
# 0.1921 kPa*m^3/(kg*K) = 192.1 J/(kg*K) — the specific gas constant the
# sound-speed computation uses, with the CO2 heat-capacity ratio below.
GAS_CONSTANT_J_KG_K = RHO_GAS_CONSTANT_KPA_M3_KG_K * 1000.0
CO2_GAMMA_RATIO = 1.29                  # heat-capacity ratio class value for
                                        # the CO2 atmosphere (standard
                                        # compressible-flow figure)

# ---------------------------------------------------------------------------
# TIER 3 — document-cited entry-parameter classes.
# ---------------------------------------------------------------------------
EDL_CLASSES_PROVENANCE = {
    "tier": "document-cited",
    "source": ("Braun & Manning 2007, 'Mars Exploration Entry, Descent and "
               "Landing Challenges', J. Spacecraft & Rockets 44(2), "
               "doi:10.2514/1.25116 — the canonical statement of the "
               "ballistic-coefficient classes (Viking ~64 kg/m^2; "
               "MSL-class ~140-150 kg/m^2; multi-tonne landers >>), the "
               "supersonic-parachute (DGB) qualification ceiling near "
               "Mach 2, and the thesis this task family re-derives: high "
               "ballistic-coefficient payloads reach parachute altitude "
               "too fast"),
    "entry_state_source": ("MSL entry-interface class values (radius "
                           "3522.2 km, i.e. ~125 km altitude; inertial "
                           "entry speed ~5.8-6 km/s; entry flight-path "
                           "angle ~-15.5 deg): Way et al. 2013, 'Mars "
                           "Science Laboratory: Entry, Descent, and "
                           "Landing System Performance', IEEE Aerospace "
                           "class reporting"),
    "access_note": "journal publishers refuse non-browser fetch; cited by "
                   "title and DOI per the pinning discipline",
}
BALLISTIC_COEFF_CLASSES_KG_M2 = (
    ("viking_class", 64.0),
    ("msl_class", 146.0),
    ("heavy_lander_class", 400.0),   # the multi-tonne/ISRU-cargo class the
                                     # mission chain actually needs to land
)
ENTRY_INTERFACE_ALTITUDE_M = 125e3   # MSL EI convention (3522.2 km radius)
ENTRY_FLIGHT_PATH_ANGLE_DEG = -15.5  # MSL-class EI angle
CHUTE_DEPLOY_ALTITUDE_M = 10e3       # DGB deploy altitude class
CHUTE_DEPLOY_MACH_LIMIT = 2.1        # DGB qualification ceiling class
PEAK_DECEL_LIMIT_G = 15.0            # stated hardware/crew-tolerance class


if __name__ == "__main__":
    import json
    blocks = {k: v for k, v in sorted(globals().items())
              if k.endswith("_PROVENANCE")}
    consts = {k: v for k, v in sorted(globals().items())
              if k.upper() == k and not k.endswith("_PROVENANCE")
              and isinstance(v, (int, float, tuple))}
    print(json.dumps({"constants": {k: str(v) for k, v in consts.items()},
                      "provenance_blocks": list(blocks)}, indent=1))
