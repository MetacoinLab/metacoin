"""task-0012-comms-link-budget — deterministic deep-space RF link budget & Doppler shift.

Research-only. A bit-reproducible deep-space communications task: it computes an X-band
downlink budget (EIRP, free-space path loss, received power, C/N0, Eb/N0, and link margin
against a required Eb/N0 threshold) and the first-order Doppler shift of the carrier. The
whole budget is closed-form in decibels — NO iteration. It maps to the NASA Technology
Taxonomy TX05 (Communications, Navigation, and Orbital Debris Tracking Systems). The
computation is deterministic and reproducible by machine — exactly what MIP-0002 Gate 2
(independent re-run yields a byte-identical hash) checks.

The Doppler figure is the NON-RELATIVISTIC first-order approximation (f * v/c); at 25 km/s,
v/c ~ 8.3e-5, so relativistic corrections are negligible at this precision but are NOT
modeled. The link budget is illustrative first-order (single lumped line loss, no atmospheric
or pointing/polarization losses, no coding gain, idealized antenna gains) — NOT a flight
design. Test-META is a zero-value testnet placeholder and never mints base supply (MIP-0001
paragraph 3, MIP-0002 paragraph 8). Not financial, legal, or flight-engineering advice. No
NASA affiliation or endorsement.

Standard library only (math, json, hashlib). Closed-form (no iteration, no randomness), and
every emitted float is rounded to a fixed number of decimals so re-runs are byte-identical
and the SHA-256 output hash is stable (the basis of the Gate-2 check).

Interface is identical to the other tasks so the verifier and agent loop can use them
interchangeably: compute() -> dict, canonical_json(result) -> str, output_hash(result) -> str.
"""

import json
import math

# --- Fixed inputs (part of the reproducibility hash) ------------------------
# Changing any of these changes the canonical output and therefore the Gate-2 hash.
SPEED_OF_LIGHT_M_S = 299792458.0   # speed of light, m/s
FREQUENCY_HZ = 8.4e9              # X-band downlink carrier frequency, Hz
TX_POWER_W = 20.0                 # spacecraft transmit power, W
TX_ANTENNA_GAIN_DBI = 35.0        # spacecraft (high-gain) antenna gain, dBi
RX_ANTENNA_GAIN_DBI = 74.0        # ground station antenna gain, dBi (DSN 70 m class)
DISTANCE_KM = 4.0e8              # one-way range, km (~Mars-distance order)
SYSTEM_NOISE_TEMP_K = 30.0        # receive system noise temperature, K
DATA_RATE_BPS = 100000.0          # downlink data rate, bits/s
BOLTZMANN_DBW_HZ_K = -228.6       # Boltzmann constant, 10*log10(k) in dBW/Hz/K
LINE_LOSSES_DB = 2.0             # transmit-side line/feed losses, dB
RELATIVE_VELOCITY_M_S = 25000.0   # line-of-sight relative velocity for Doppler, m/s
REQUIRED_EBN0_DB = 2.5           # required Eb/N0 threshold for the link to close, dB

# Number of decimal places every emitted float is rounded to. Fixed rounding is what makes
# the canonical JSON byte-stable across runs (and thus the SHA-256 reproducible).
ROUND_DECIMALS = 6


def compute() -> dict:
    """Compute the closed-form X-band link budget and first-order Doppler shift.

    All link math is done consistently in decibels:

      wavelength_m       = c / f
      fspl_db            = 20*log10(4*pi*distance_m / wavelength_m)
      tx_power_dbw       = 10*log10(P_tx)
      eirp_dbw           = tx_power_dbw + tx_gain_dbi - line_losses_db
      received_power_dbw = eirp_dbw - fspl_db + rx_gain_dbi
      n0_dbw_hz          = 10*log10(k) + 10*log10(T_sys)
      cn0_db_hz          = received_power_dbw - n0_dbw_hz
      ebn0_db            = cn0_db_hz - 10*log10(data_rate)
      link_margin_db     = ebn0_db - required_ebn0_db   (link closes iff >= 0)

    Doppler (NON-RELATIVISTIC first-order approximation; v/c ~ 8.3e-5 here):
      doppler_shift_hz   = f * (v_rel / c)
      doppler_ppm        = (v_rel / c) * 1e6
    """
    wavelength_m = SPEED_OF_LIGHT_M_S / FREQUENCY_HZ
    distance_m = DISTANCE_KM * 1000.0

    # Free-space path loss (dB) over the one-way range.
    fspl_db = 20.0 * math.log10(4.0 * math.pi * distance_m / wavelength_m)

    # Transmit chain -> EIRP -> received power (all dB / dBW).
    tx_power_dbw = 10.0 * math.log10(TX_POWER_W)
    eirp_dbw = tx_power_dbw + TX_ANTENNA_GAIN_DBI - LINE_LOSSES_DB
    received_power_dbw = eirp_dbw - fspl_db + RX_ANTENNA_GAIN_DBI

    # Noise power spectral density, then carrier-to-noise-density and Eb/N0.
    n0_dbw_hz = BOLTZMANN_DBW_HZ_K + 10.0 * math.log10(SYSTEM_NOISE_TEMP_K)
    cn0_db_hz = received_power_dbw - n0_dbw_hz
    ebn0_db = cn0_db_hz - 10.0 * math.log10(DATA_RATE_BPS)
    link_margin_db = ebn0_db - REQUIRED_EBN0_DB
    link_closes = link_margin_db >= 0.0

    # First-order (non-relativistic) Doppler shift of the carrier.
    doppler_shift_hz = FREQUENCY_HZ * (RELATIVE_VELOCITY_M_S / SPEED_OF_LIGHT_M_S)
    doppler_ppm = (RELATIVE_VELOCITY_M_S / SPEED_OF_LIGHT_M_S) * 1e6

    results = [
        {"stage": "eirp_dbw", "value": round(eirp_dbw, ROUND_DECIMALS)},
        {"stage": "fspl_db", "value": round(fspl_db, ROUND_DECIMALS)},
        {"stage": "received_power_dbw", "value": round(received_power_dbw, ROUND_DECIMALS)},
        {"stage": "cn0_db_hz", "value": round(cn0_db_hz, ROUND_DECIMALS)},
        {"stage": "ebn0_db", "value": round(ebn0_db, ROUND_DECIMALS)},
    ]

    return {
        "task_id": "task-0012-comms-link-budget",
        "inputs": {
            "speed_of_light_m_s": SPEED_OF_LIGHT_M_S,
            "frequency_hz": FREQUENCY_HZ,
            "tx_power_w": TX_POWER_W,
            "tx_antenna_gain_dbi": TX_ANTENNA_GAIN_DBI,
            "rx_antenna_gain_dbi": RX_ANTENNA_GAIN_DBI,
            "distance_km": DISTANCE_KM,
            "system_noise_temp_k": SYSTEM_NOISE_TEMP_K,
            "data_rate_bps": DATA_RATE_BPS,
            "boltzmann_dbw_hz_k": BOLTZMANN_DBW_HZ_K,
            "line_losses_db": LINE_LOSSES_DB,
            "relative_velocity_m_s": RELATIVE_VELOCITY_M_S,
            "required_ebn0_db": REQUIRED_EBN0_DB,
            "round_decimals": ROUND_DECIMALS,
        },
        "results": results,
        "summary": {
            "wavelength_m": round(wavelength_m, ROUND_DECIMALS),
            "fspl_db": round(fspl_db, ROUND_DECIMALS),
            "eirp_dbw": round(eirp_dbw, ROUND_DECIMALS),
            "received_power_dbw": round(received_power_dbw, ROUND_DECIMALS),
            "cn0_db_hz": round(cn0_db_hz, ROUND_DECIMALS),
            "ebn0_db": round(ebn0_db, ROUND_DECIMALS),
            "link_margin_db": round(link_margin_db, ROUND_DECIMALS),
            "doppler_shift_hz": round(doppler_shift_hz, ROUND_DECIMALS),
            "doppler_ppm": round(doppler_ppm, ROUND_DECIMALS),
            "link_closes": bool(link_closes),
        },
    }


def canonical_json(result: dict) -> str:
    """Serialize the result deterministically.

    sort_keys=True, fixed compact separators, and ensure_ascii=True make the output
    byte-stable across runs and platforms (assuming identical rounded float values).
    """
    return json.dumps(
        result,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


def output_hash(result: dict) -> str:
    """Return the SHA-256 hex digest of the canonical JSON (the Gate-2 reproducibility hash)."""
    import hashlib

    return hashlib.sha256(canonical_json(result).encode("utf-8")).hexdigest()


if __name__ == "__main__":
    _result = compute()
    print(canonical_json(_result))
    print("sha256:" + output_hash(_result))
