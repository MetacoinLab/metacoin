"""task-0001-lunar-link-budget — reference computation for the Phase 1 agentic demo.

Research-only. This is an illustrative, fully deterministic numerical task used to
exercise the MetaCoin proof model (MIP-0002 Gate 1 / Gate 2). Test-META is a zero-value
testnet placeholder and never mints base supply (MIP-0001 paragraph 3, MIP-0002
paragraph 8). Not financial or legal advice. No NASA affiliation or endorsement.

Spec: demo/tasks/example_task.md
Standard library only — no external dependencies. The computation is deterministic and
all emitted numbers are rounded to a fixed number of decimals so that re-runs are
byte-identical and the SHA-256 output hash is stable (the basis of the Gate-2 check).
"""

import json
import math
import random

# --- Physical constant ------------------------------------------------------
# Speed of light in vacuum, exact SI definition (meters per second).
SPEED_OF_LIGHT_M_S = 299792458.0

# --- Fixed inputs (must match demo/tasks/example_task.md exactly) -----------
# These inputs are part of the reproducibility hash; changing any of them changes
# the canonical output and therefore the Gate-2 hash.
TX_POWER_W = 10.0          # transmit power, watts
TX_GAIN_DBI = 12.0         # transmit antenna gain, dBi
RX_GAIN_DBI = 12.0         # receive antenna gain, dBi
FREQUENCY_HZ = 2.4e9       # carrier frequency, hertz (2.4 GHz)
RANGE_START_KM = 100       # range sweep start, kilometers
RANGE_STOP_KM = 2000       # range sweep stop (inclusive), kilometers
RANGE_STEP_KM = 100        # range sweep step, kilometers
SEED = 42                  # seed for any tie-breaking/ordering; core math is deterministic

# Number of decimal places every emitted float is rounded to. Fixed rounding is what
# makes the canonical JSON byte-stable across runs (and thus the SHA-256 reproducible).
ROUND_DECIMALS = 6


def _fspl_db(distance_m: float, frequency_hz: float) -> float:
    """Free-space path loss in dB.

    FSPL(dB) = 20*log10(distance_m) + 20*log10(frequency_hz) + 20*log10(4*pi / c)

    Each term:
      * 20*log10(distance_m)   — loss grows with distance
      * 20*log10(frequency_hz) — loss grows with frequency
      * 20*log10(4*pi / c)     — the constant from the Friis free-space equation,
                                 where c is the speed of light in m/s
    """
    return (
        20.0 * math.log10(distance_m)
        + 20.0 * math.log10(frequency_hz)
        + 20.0 * math.log10(4.0 * math.pi / SPEED_OF_LIGHT_M_S)
    )


def _dbw(power_w: float) -> float:
    """Convert absolute power in watts to dBW (decibels relative to 1 watt).

    dBW = 10*log10(power_w / 1 W). For 10.0 W this is exactly 10.0 dBW.
    """
    return 10.0 * math.log10(power_w / 1.0)


def compute() -> dict:
    """Run the deterministic link-budget sweep and return the structured result.

    For each range step:
      FSPL(dB)         = _fspl_db(distance_m, frequency)
      link_margin_dB   = tx_power_dBW + tx_gain_dBi + rx_gain_dBi - FSPL(dB)

    Note: 'link_margin_dB' here is the received signal level relative to 1 W after
    antenna gains and free-space loss, per the task spec's stated formula. It is an
    illustrative figure, not a claim of real engineering utility.
    """
    # Seed is fixed for reproducibility. The numeric computation below does not depend
    # on randomness; the seed only governs any incidental tie-breaking/ordering.
    random.seed(SEED)

    tx_power_dbw = _dbw(TX_POWER_W)

    steps = []
    for range_km in range(RANGE_START_KM, RANGE_STOP_KM + 1, RANGE_STEP_KM):
        distance_m = float(range_km) * 1000.0
        fspl_db = _fspl_db(distance_m, FREQUENCY_HZ)
        link_margin_db = tx_power_dbw + TX_GAIN_DBI + RX_GAIN_DBI - fspl_db
        steps.append(
            {
                "range_km": range_km,
                "free_space_path_loss_dB": round(fspl_db, ROUND_DECIMALS),
                "link_margin_dB": round(link_margin_db, ROUND_DECIMALS),
            }
        )

    min_margin_db = round(min(step["link_margin_dB"] for step in steps), ROUND_DECIMALS)

    return {
        "task_id": "task-0001-lunar-link-budget",
        "inputs": {
            "tx_power_W": TX_POWER_W,
            "tx_gain_dBi": TX_GAIN_DBI,
            "rx_gain_dBi": RX_GAIN_DBI,
            "frequency_Hz": FREQUENCY_HZ,
            "range_start_km": RANGE_START_KM,
            "range_stop_km": RANGE_STOP_KM,
            "range_step_km": RANGE_STEP_KM,
            "seed": SEED,
            "speed_of_light_m_s": SPEED_OF_LIGHT_M_S,
            "round_decimals": ROUND_DECIMALS,
        },
        "results": steps,
        "summary": {
            "min_margin_dB": min_margin_db,
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
