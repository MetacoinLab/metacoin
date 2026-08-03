"""task-0014-fdir-state-machine — deterministic FDIR autonomous state machine.

Research-only. A bit-reproducible fault-detection-isolation-recovery (FDIR) task: a
simulated spacecraft power/sensor subsystem is stepped through a FIXED 50-step run with
a FIXED fault-injection schedule (a stuck temperature sensor at step 12, an overcurrent
transient at step 27), and a four-state FDIR machine {nominal, safe-mode, recovery,
nominal-restored} with a PUBLISHED transition table detects, isolates, and recovers from
both faults. It maps to the NASA Technology Taxonomy TX10 (Autonomous Systems). The
computation is deterministic and reproducible by machine — exactly what MIP-0002 Gate 2
(independent re-run yields a byte-identical hash) checks.

BOUNDED AUTONOMY — THE MISSION THEME: this task IS the protocol's own operating
principle in miniature. The FDIR machine exercises bounded autonomous response: every
transition it may take is declared in TRANSITION_TABLE before the run, fault handling is
one-fault-at-a-time (detection is only evaluated in the nominal states), and the machine
has NO discretionary moves — an event either has a declared transition or the run fails
loudly. Autonomy here means executing a published policy fast, never inventing one.

INTERNAL SELF-PROOF (log-consistency check): compute() re-plays the emitted event log
through TRANSITION_TABLE with an INDEPENDENT replay routine and asserts (a) every logged
transition is exactly the table's declared move, (b) the state chain is continuous
(each entry's state_before equals the previous entry's state_after), (c) the mismatch
count is exactly 0, (d) both scheduled faults were detected at their derivable steps
(stuck sensor: injection step + the 3-identical-readings detection latency; overcurrent:
same step), and (e) the final state is nominal-restored. A violated assertion CRASHES
the task — stop, don't fudge.

Test-META is a zero-value testnet placeholder and never mints base supply (MIP-0001
paragraph 3, MIP-0002 paragraph 8). Illustrative subsystem simulation (fixed synthetic
telemetry; no real avionics modeled) — NOT a flight FDIR design. Not financial, legal,
or flight-engineering advice. No NASA affiliation or endorsement.

Standard library only (math, json, hashlib). No randomness. Every emitted float is
rounded to a fixed number of decimals so re-runs are byte-identical and the SHA-256
output hash is stable (the basis of the Gate-2 check).

Interface is identical to the other tasks so the verifier and agent loop can use them
interchangeably: compute() -> dict, canonical_json(result) -> str, output_hash(result) -> str.
"""

import json
import math

# --- Fixed inputs (part of the reproducibility hash) ------------------------
# Changing any of these changes the canonical output and therefore the Gate-2 hash.
RUN_STEPS = 50                      # fixed step count — the loop never exits early
SENSOR_STUCK_STEP = 12              # fault 1: temperature sensor freezes at this step
SENSOR_STUCK_DURATION = 5           # ...and stays frozen for this many steps
OVERCURRENT_STEP = 27               # fault 2: single-step overcurrent transient
OVERCURRENT_AMPS = 9.5              # transient magnitude, A (far above the threshold)
STUCK_DETECT_COUNT = 3              # N identical consecutive readings = stuck sensor
CURRENT_LIMIT_AMPS = 8.0            # overcurrent detection threshold, A
ISOLATION_STEPS = 2                 # fixed safe-mode dwell before isolation-complete
RECOVERY_STEPS = 3                  # fixed recovery dwell before recovery-complete
ROUND_DECIMALS = 6

# Synthetic nominal telemetry (deterministic closed forms of the step index):
#   temperature_c(k) = 20 + 5*sin(0.3k)   — a slowly varying thermal reading
#   current_a(k)     = 4 + 0.5*sin(0.2k)  — a nominal bus current well under limit
TEMP_BASE_C = 20.0
TEMP_AMPL_C = 5.0
TEMP_RATE = 0.3
CURR_BASE_A = 4.0
CURR_AMPL_A = 0.5
CURR_RATE = 0.2

# The FDIR states and the PUBLISHED transition table — the machine's complete,
# bounded policy. Key: (state, event) -> next state. Any (state, event) pair not
# declared here keeps the state ("no declared move" is only legal for the
# nominal-telemetry event; the self-proof replay enforces exactly that).
STATE_NOMINAL = "nominal"
STATE_SAFE = "safe-mode"
STATE_RECOVERY = "recovery"
STATE_RESTORED = "nominal-restored"
EVENT_NOMINAL = "nominal-telemetry"
EVENT_FAULT_STUCK = "fault-detected:sensor-stuck"
EVENT_FAULT_OVERCURRENT = "fault-detected:overcurrent"
EVENT_ISOLATED = "isolation-complete"
EVENT_RECOVERED = "recovery-complete"

TRANSITION_TABLE = {
    (STATE_NOMINAL, EVENT_FAULT_STUCK): STATE_SAFE,
    (STATE_NOMINAL, EVENT_FAULT_OVERCURRENT): STATE_SAFE,
    (STATE_RESTORED, EVENT_FAULT_STUCK): STATE_SAFE,
    (STATE_RESTORED, EVENT_FAULT_OVERCURRENT): STATE_SAFE,
    (STATE_SAFE, EVENT_ISOLATED): STATE_RECOVERY,
    (STATE_RECOVERY, EVENT_RECOVERED): STATE_RESTORED,
}


def _temperature_c(step: int) -> float:
    """Nominal temperature telemetry; the stuck fault freezes the READING, not this."""
    return TEMP_BASE_C + TEMP_AMPL_C * math.sin(TEMP_RATE * step)


def _current_a(step: int) -> float:
    if step == OVERCURRENT_STEP:
        return OVERCURRENT_AMPS
    return CURR_BASE_A + CURR_AMPL_A * math.sin(CURR_RATE * step)


def _sensor_reading_c(step: int) -> float:
    """The reading the FDIR machine SEES: frozen at the stuck-step value for the
    fault window, nominal otherwise."""
    if SENSOR_STUCK_STEP <= step < SENSOR_STUCK_STEP + SENSOR_STUCK_DURATION:
        return _temperature_c(SENSOR_STUCK_STEP)
    return _temperature_c(step)


def _replay_log(event_log: list) -> int:
    """INDEPENDENT re-play of the emitted log through TRANSITION_TABLE (the
    self-proof core). Returns the number of mismatches: transitions that are not
    the table's declared move, non-declared moves on non-nominal events, state
    changes on nominal telemetry, or a broken state chain."""
    mismatches = 0
    state = STATE_NOMINAL
    for entry in event_log:
        if entry["state_before"] != state:
            mismatches += 1  # broken chain
        declared = TRANSITION_TABLE.get((entry["state_before"], entry["event"]))
        if declared is None:
            # only nominal telemetry may keep the state without a declared move
            if entry["event"] != EVENT_NOMINAL \
                    or entry["state_after"] != entry["state_before"]:
                mismatches += 1
        elif entry["state_after"] != declared:
            mismatches += 1
        state = entry["state_after"]
    return mismatches


def compute() -> dict:
    """Run the 50-step FDIR simulation, then SELF-PROVE the emitted log."""
    state = STATE_NOMINAL
    event_log = []
    fault_detections = []       # [{step, event}] for the summary
    recent_readings = []        # sliding window for the stuck-sensor detector
    dwell = 0                   # steps spent in the current handling state

    for step in range(RUN_STEPS):
        reading = _sensor_reading_c(step)
        current = _current_a(step)
        recent_readings.append(round(reading, ROUND_DECIMALS))
        if len(recent_readings) > STUCK_DETECT_COUNT:
            recent_readings.pop(0)

        # Event derivation — bounded policy, one fault at a time: fault detection
        # is evaluated ONLY in the nominal states; handling states emit their
        # fixed-dwell milestones and nothing else.
        if state in (STATE_NOMINAL, STATE_RESTORED):
            stuck = (len(recent_readings) == STUCK_DETECT_COUNT
                     and len(set(recent_readings)) == 1)
            if current > CURRENT_LIMIT_AMPS:
                event = EVENT_FAULT_OVERCURRENT
            elif stuck:
                event = EVENT_FAULT_STUCK
            else:
                event = EVENT_NOMINAL
        elif state == STATE_SAFE:
            dwell += 1
            event = EVENT_ISOLATED if dwell >= ISOLATION_STEPS else EVENT_NOMINAL
        else:  # STATE_RECOVERY
            dwell += 1
            event = EVENT_RECOVERED if dwell >= RECOVERY_STEPS else EVENT_NOMINAL

        next_state = TRANSITION_TABLE.get((state, event), state)
        if event != EVENT_NOMINAL:
            if event in (EVENT_FAULT_STUCK, EVENT_FAULT_OVERCURRENT):
                fault_detections.append({"step": step, "event": event})
                recent_readings = []   # a fresh window after each detection
            dwell = 0
        event_log.append({
            "step": step,
            "state_before": state,
            "event": event,
            "state_after": next_state,
            "sensor_reading_c": round(reading, ROUND_DECIMALS),
            "bus_current_a": round(current, ROUND_DECIMALS),
        })
        state = next_state

    # --- INTERNAL SELF-PROOF (stop, don't fudge) ----------------------------
    # (a,b,c) independent replay: 0 mismatches or crash.
    mismatch_count = _replay_log(event_log)
    assert mismatch_count == 0, \
        f"log-consistency check failed: {mismatch_count} mismatch(es)"
    # (d) both scheduled faults detected at their DERIVABLE steps: the stuck
    # sensor needs STUCK_DETECT_COUNT identical readings, so detection lands at
    # injection + (STUCK_DETECT_COUNT - 1); the overcurrent trips its threshold
    # on the injection step itself.
    expected_stuck_detect = SENSOR_STUCK_STEP + STUCK_DETECT_COUNT - 1
    assert [d["step"] for d in fault_detections] == \
        [expected_stuck_detect, OVERCURRENT_STEP], \
        f"fault detections at {fault_detections}, expected steps " \
        f"{[expected_stuck_detect, OVERCURRENT_STEP]}"
    # (e) the run ends recovered.
    assert state == STATE_RESTORED, f"final state {state}, expected restored"

    transition_counts = {}
    for entry in event_log:
        if entry["state_after"] != entry["state_before"]:
            key = f"{entry['state_before']}->{entry['state_after']}"
            transition_counts[key] = transition_counts.get(key, 0) + 1

    return {
        "task_id": "task-0014-fdir-state-machine",
        "inputs": {
            "run_steps": RUN_STEPS,
            "sensor_stuck_step": SENSOR_STUCK_STEP,
            "sensor_stuck_duration": SENSOR_STUCK_DURATION,
            "overcurrent_step": OVERCURRENT_STEP,
            "overcurrent_amps": OVERCURRENT_AMPS,
            "stuck_detect_count": STUCK_DETECT_COUNT,
            "current_limit_amps": CURRENT_LIMIT_AMPS,
            "isolation_steps": ISOLATION_STEPS,
            "recovery_steps": RECOVERY_STEPS,
            "transition_table": sorted(
                f"{s}|{e}->{n}" for (s, e), n in TRANSITION_TABLE.items()),
            "round_decimals": ROUND_DECIMALS,
        },
        "results": [
            {"quantity": "event_log", "value": event_log},
        ],
        "summary": {
            "final_state": state,
            "fault_detections": fault_detections,
            "state_transitions": {k: transition_counts[k]
                                  for k in sorted(transition_counts)},
            "log_consistency_mismatches": mismatch_count,
            "steps_run": RUN_STEPS,
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
