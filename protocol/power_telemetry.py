# Copyright (c) 2023-2026 MetaCoin-Lab.
# Licensed under SML-1.0 — see LICENSE.md.
"""power_telemetry.py — hardware power telemetry probe v0 (schemas
"sensor-inventory/0.1" and "power-characterization/0.1").

================== HONESTY / SCOPE (READ ME) ==================
Research-stage, ZERO-VALUE, no token, no money, no mainnet, no networking, no payments.

This module attempts to pay down the last named provenance debt — "hardware power
telemetry" — with REAL measured power on the local host. It does three things, each
honest about what it can and cannot claim:

  1. DISCOVERY (--probe): read-only inventory of every power-ish sensor this host
     actually exposes (hwmon power/energy files, INA-class i2c devices, nvidia-smi,
     tegrastats, RAPL powercap), each with a per-source verdict. A sensor is USABLE
     only if it passes an EMPIRICAL load-response screen: sampled at idle and again
     under a pinned CPU busy loop, its reading must rise by more than the noise band.
     A sensor that reads real watts but does not observe the CPU domain (e.g. a
     GPU-die-only rail, a fan actuator's own motor power) is honestly UNUSABLE for
     characterizing CPU-bound task work, and the inventory says exactly why, with the
     measured evidence. If NOTHING usable exists, that is a legitimate finding — the
     verdict is "telemetry debt remains open on this host: no readable power sensor
     observes the CPU domain", and characterization is a NAMED SKIP, never fabricated.

  2. CHARACTERIZATION (--characterize): MEASUREMENT-PHYSICS CONSTRAINT — our tasks
     complete in MICROSECONDS; no polled sensor (hwmon ~ms refresh, nvidia-smi ~1 Hz,
     RAPL counters) resolves per-task power directly, and pretending otherwise would
     be fabrication. The honest upgrade path is HOST-LEVEL SUSTAINED-LOAD DELTA:
     (i) idle_power_w = median of >= 30 samples over >= 15 s quiescent;
     (ii) loaded_power_w = same sampling while a sustained calibration load runs (a
     pinned pure-Python busy loop — the same instruction-mix class as our tasks);
     (iii) active_power_delta_w = loaded - idle, with sample stddev for both phases.
     Per-task energy then remains cpu_time x delta_w — an ESTIMATE grounded in
     measured power rather than an assumed nameplate. THE ESTIMATE LABEL STAYS.

  3. COMPARISON (--compare, report-only): recompute the anchored metering report's
     per-task energy estimates with the measured delta_w next to the assumed-15.0 W
     figures. The anchored idx-20 record STANDS UNCHANGED — its label was honest
     ("assumed"); any upgrade lands as a NEW metering generation at the next
     milestone batch, per the generation cadence policy in work_molecule.py.

WHAT THIS MODULE NEVER DOES:
  * It NEVER writes to the ledger. Probe/characterize/compare are read-only with
    respect to the repo; their outputs are gitignored local working files.
  * It NEVER fabricates a reading. Every value in an inventory or characterization
    is either read from a real sensor or absent with a named reason.
  * It NEVER upgrades the "estimated" energy label. Measured host power narrows the
    assumption; per-task energy is still cpu_time x constant-power, an estimate.

PRIVACY: reports carry sensor names, paths, units, and readings only — never device
serial numbers, UUIDs, or hostnames. (A GPU marketing SKU such as "NVIDIA GB10" is a
product name, not a device identifier, and is kept as context for the verdict.)

DETERMINISM-OF-FORMAT: sensor readings are MEASUREMENTS and legitimately vary run to
run — the metering integrity model applies: a written report FIXES THE CLAIM made at
measurement time; it is not a recomputable value. The report FORMAT (keys, labels,
notes, sorted-key canonical layout) is deterministic and regression-tested; only the
sampled numbers move.

Standard library only (json, os, sys, argparse, glob, shutil, subprocess, statistics
via hand-rolled helpers, tempfile in the self-test). subprocess is used ONLY for
read-only probing of nvidia-smi/tegrastats and for spawning the calibration busy-loop
child (pure Python, killed on completion). The metering report is resolved through
work_molecule.find_evidence_file — the same discovery order as molecule builds.
Not legal, financial, investment, or security-certification advice.

Usage:
    python3 protocol/power_telemetry.py --probe                    # writes sensor_inventory.json
    python3 protocol/power_telemetry.py --characterize             # writes power_characterization.json (or named skip)
    python3 protocol/power_telemetry.py --compare                  # report-only table (or named skip)
    python3 protocol/power_telemetry.py --selftest                 # temp/fixture-only; writes nothing into the repo
"""

# Suppress __pycache__/*.pyc so importing protocol modules below leaves no stray files.
import sys
sys.dont_write_bytecode = True

import argparse
import glob
import json
import os
import shutil
import subprocess
import time

_PROTO_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_PROTO_DIR)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# REUSE the evidence-file discovery order (repo root, then published bundle) — the
# comparison must read the SAME metering report molecule builds absorb.
from protocol.work_molecule import find_evidence_file

INVENTORY_SCHEMA = "sensor-inventory/0.1"
CHARACTERIZATION_SCHEMA = "power-characterization/0.1"

DEFAULT_INVENTORY_PATH = os.path.join(_REPO_ROOT, "sensor_inventory.json")
DEFAULT_CHARACTERIZATION_PATH = os.path.join(_REPO_ROOT, "power_characterization.json")

# The anchored metering assumption this probe tries to upgrade (trust_vector.py's
# metering report, anchored at ledger idx-20). Read here for the comparison only.
ASSUMED_CPU_POWER_W = 15.0
METERING_REPORT_BASENAME = "metering_report.json"

# Discovery-stage load-response screen: short by design (it is a usability gate, not
# the characterization). A sensor "observes the CPU domain" only if a pinned busy
# core moves its reading by more than the noise band AND more than an absolute floor
# (a single modern core under sustained full load draws well over this; a sensor
# that cannot see 0.5 W of CPU work cannot ground our energy estimates).
SCREEN_SAMPLES = 10
SCREEN_INTERVAL_S = 0.4
LOAD_RESPONSE_FLOOR_W = 0.5
NOISE_SD_MULTIPLIER = 3.0

# Characterization sampling: >= 30 samples over >= 15 s per phase (docstring step ii).
CHAR_SAMPLES = 30
CHAR_INTERVAL_S = 0.5

# The honest nothing-found finding, verbatim in inventory verdicts and skip messages.
DEBT_OPEN_VERDICT = ("telemetry debt remains open on this host: no readable power "
                     "sensor observes the CPU domain")

MEASUREMENT_PHYSICS_NOTE = (
    "tasks complete in microseconds; no polled sensor resolves per-task power "
    "directly. The honest upgrade path is host-level sustained-load delta "
    "characterization; per-task energy remains cpu_time x delta_w, an ESTIMATE."
)
INTEGRITY_NOTE = (
    "sensor readings are measurements and vary run to run; this report fixes the "
    "claim made at measurement time (metering integrity model), it is not a "
    "recomputable value. Report format is deterministic; sampled numbers move."
)
CHARACTERIZATION_LIMITATION = (
    "host-level sustained-load characterization; per-task energy remains "
    "cpu_time x delta_w, an ESTIMATE grounded in measured power rather than an "
    "assumed nameplate — the estimate label stays"
)


# ----------------------------------------------------------------------------
# small statistics helpers (hand-rolled so the self-test can hand-compute them)
# ----------------------------------------------------------------------------
def _median(values):
    ordered = sorted(values)
    n = len(ordered)
    if n == 0:
        raise ValueError("median of empty sample")
    mid = n // 2
    if n % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _sample_sd(values):
    """Sample standard deviation (n-1 denominator); 0.0 for n < 2."""
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    return (sum((v - mean) ** 2 for v in values) / (n - 1)) ** 0.5


def _round6(x):
    return round(x, 6)


# ----------------------------------------------------------------------------
# calibration load: a pinned pure-Python busy loop (same instruction-mix class as
# the demo tasks — interpreter bytecode over small integers, no vector units)
# ----------------------------------------------------------------------------
_BUSY_LOOP_CHILD = (
    "import os\n"
    "try:\n"
    "    os.sched_setaffinity(0, {0})\n"
    "except (AttributeError, OSError):\n"
    "    pass\n"
    "x = 0\n"
    "while True:\n"
    "    x = (x * 31 + 7) % 1000003\n"
)


class _BusyLoad:
    """Context manager that runs the pinned busy-loop child for the duration of the
    loaded sampling phase. Killed (not just terminated) on exit so no child ever
    outlives the probe."""

    def __enter__(self):
        self._proc = subprocess.Popen([sys.executable, "-c", _BUSY_LOOP_CHILD])
        time.sleep(1.0)  # let the load settle before the first loaded sample
        return self

    def __exit__(self, *exc):
        self._proc.kill()
        self._proc.wait()
        return False


class _NoLoad:
    """Injectable no-op load for self-tests (sample streams are injected anyway)."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# ----------------------------------------------------------------------------
# sensor sources — each yields (source dict for the inventory, sample_fn or None)
# ----------------------------------------------------------------------------
def _read_text(path):
    with open(path, "r") as f:
        return f.read().strip()


def _hwmon_sources(sys_root):
    """Scan <sys_root>/class/hwmon/hwmon*/ for power/energy/current-voltage files.
    power*_input is in microwatts, energy*_input in microjoules (kernel hwmon ABI)."""
    found = []
    for node in sorted(glob.glob(os.path.join(sys_root, "class", "hwmon", "hwmon*"))):
        try:
            name = _read_text(os.path.join(node, "name"))
        except OSError:
            name = "unknown"
        for pfile in sorted(glob.glob(os.path.join(node, "power*_input"))):
            try:
                sample_w = int(_read_text(pfile)) / 1e6
            except (OSError, ValueError) as exc:
                found.append(({"source": f"hwmon:{name}", "path_or_tool": pfile,
                               "unit": "uW", "sample_value_w": None,
                               "refresh_note": "unreadable",
                               "verdict": {"usable": False,
                                           "reason": f"read failed: {exc}"}}, None))
                continue

            def _sampler(path=pfile):
                return int(_read_text(path)) / 1e6

            found.append(({"source": f"hwmon:{name}", "path_or_tool": pfile,
                           "unit": "uW", "sample_value_w": _round6(sample_w),
                           "refresh_note": "kernel hwmon instantaneous power; "
                                           "refresh is driver-dependent (typ. ms)"},
                          _sampler))
        for efile in sorted(glob.glob(os.path.join(node, "energy*_input"))):
            try:
                sample_uj = int(_read_text(efile))
            except (OSError, ValueError):
                sample_uj = None
            # An energy counter is sampled as power by differencing two reads.
            def _esampler(path=efile, interval=0.25):
                before = int(_read_text(path))
                time.sleep(interval)
                after = int(_read_text(path))
                return (after - before) / 1e6 / interval

            found.append(({"source": f"hwmon:{name}", "path_or_tool": efile,
                           "unit": "uJ (counter; power derived by differencing)",
                           "sample_value_w": None if sample_uj is None else
                                             _round6(sample_uj / 1e6),
                           "refresh_note": "cumulative energy counter"},
                          None if sample_uj is None else _esampler))
        # Bare current/voltage pairs (INA-style channels surface here too): inventory
        # them for completeness; deriving power from a single instantaneous V*I pair
        # is noisy, so they are listed as context, not screened as candidates.
        currs = sorted(glob.glob(os.path.join(node, "curr*_input")))
        volts = sorted(glob.glob(os.path.join(node, "in*_input")))
        if currs and volts:
            found.append(({"source": f"hwmon:{name}", "path_or_tool": node,
                           "unit": "mA/mV pairs",
                           "sample_value_w": None,
                           "refresh_note": f"{len(currs)} current x {len(volts)} "
                                           "voltage channels; not screened (no "
                                           "declared pairing)",
                           "verdict": {"usable": False,
                                       "reason": "current/voltage channels without "
                                                 "a declared power rail pairing"}},
                          None))
    return found


def _i2c_ina_source(sys_root):
    """Presence check for INA3221-class i2c power monitors. Their measurement
    channels surface through the hwmon scan above; this names the skip when the
    device class is absent entirely."""
    names = []
    for nfile in sorted(glob.glob(os.path.join(sys_root, "bus", "i2c", "devices",
                                               "*", "name"))):
        try:
            n = _read_text(nfile)
        except OSError:
            continue
        if "ina" in n.lower():
            names.append(n)
    if names:
        return {"source": "i2c-ina", "path_or_tool": "/sys/bus/i2c",
                "unit": "(channels surface via hwmon)",
                "sample_value_w": None,
                "refresh_note": f"INA-class devices present: {sorted(set(names))}"}
    return {"source": "i2c-ina", "path_or_tool": "/sys/bus/i2c",
            "unit": None, "sample_value_w": None,
            "refresh_note": "SKIPPED (named): no INA-class i2c device on this host",
            "verdict": {"usable": False,
                        "reason": "no INA-class i2c device present"}}


def _nvidia_smi_source(which=shutil.which, run=subprocess.run):
    """nvidia-smi power.draw. Returns (source, sample_fn or None)."""
    if which("nvidia-smi") is None:
        return ({"source": "nvidia-smi", "path_or_tool": "nvidia-smi",
                 "unit": None, "sample_value_w": None,
                 "refresh_note": "SKIPPED (named): nvidia-smi not installed",
                 "verdict": {"usable": False, "reason": "tool absent"}}, None)
    try:
        r = run(["nvidia-smi", "--query-gpu=power.draw,name",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=10)
        first = r.stdout.strip().splitlines()[0] if r.stdout.strip() else ""
        draw_str, _, sku = first.partition(",")
        sample_w = float(draw_str)
    except (OSError, subprocess.SubprocessError, ValueError, IndexError) as exc:
        return ({"source": "nvidia-smi", "path_or_tool": "nvidia-smi",
                 "unit": "W", "sample_value_w": None,
                 "refresh_note": "SKIPPED (named): power.draw not readable on this "
                                 f"SKU ({exc})",
                 "verdict": {"usable": False,
                             "reason": "power.draw unsupported/unparseable"}}, None)

    def _sampler(run=run):
        r = run(["nvidia-smi", "--query-gpu=power.draw",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=10)
        return float(r.stdout.strip().splitlines()[0])

    return ({"source": "nvidia-smi", "path_or_tool": "nvidia-smi",
             "unit": "W", "sample_value_w": _round6(sample_w),
             "refresh_note": f"GPU rail as reported by driver (sku: {sku.strip()}); "
                             "~1 Hz refresh"}, _sampler)


def _parse_tegrastats_power_mw(line):
    """Extract (rail, milliwatts) pairs from one tegrastats line. Rails look like
    'VDD_IN 4321mW/4300mW' (instant/average); the instant figure is taken."""
    pairs = []
    tokens = line.split()
    for i, tok in enumerate(tokens):
        if tok.endswith("mW") and "/" in tok and i > 0:
            instant = tok.split("/", 1)[0]
            if instant.endswith("mW"):
                instant = instant[:-2]
            try:
                pairs.append((tokens[i - 1], int(instant)))
            except ValueError:
                continue
    return pairs


def _tegrastats_source(which=shutil.which, run=subprocess.run):
    if which("tegrastats") is None:
        return ({"source": "tegrastats", "path_or_tool": "tegrastats",
                 "unit": None, "sample_value_w": None,
                 "refresh_note": "SKIPPED (named): tegrastats not installed",
                 "verdict": {"usable": False, "reason": "tool absent"}}, None)
    try:
        r = run(["tegrastats", "--interval", "500"], capture_output=True,
                text=True, timeout=3)
        out = r.stdout
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or b"").decode() if isinstance(exc.stdout, bytes) \
            else (exc.stdout or "")
    except (OSError, subprocess.SubprocessError) as exc:
        return ({"source": "tegrastats", "path_or_tool": "tegrastats",
                 "unit": "mW", "sample_value_w": None,
                 "refresh_note": f"SKIPPED (named): failed to run ({exc})",
                 "verdict": {"usable": False, "reason": "tool failed"}}, None)
    lines = [ln for ln in out.splitlines() if ln.strip()]
    rails = _parse_tegrastats_power_mw(lines[-1]) if lines else []
    if not rails:
        return ({"source": "tegrastats", "path_or_tool": "tegrastats",
                 "unit": "mW", "sample_value_w": None,
                 "refresh_note": "SKIPPED (named): no mW rails in output",
                 "verdict": {"usable": False, "reason": "no power rails reported"}},
                None)
    total_w = sum(mw for _, mw in rails) / 1e3

    def _sampler(run=run):
        try:
            r = run(["tegrastats", "--interval", "500"], capture_output=True,
                    text=True, timeout=3)
            out = r.stdout
        except subprocess.TimeoutExpired as exc:
            out = (exc.stdout or b"").decode() if isinstance(exc.stdout, bytes) \
                else (exc.stdout or "")
        lines = [ln for ln in out.splitlines() if ln.strip()]
        return sum(mw for _, mw in
                   _parse_tegrastats_power_mw(lines[-1])) / 1e3 if lines else 0.0

    return ({"source": "tegrastats", "path_or_tool": "tegrastats",
             "unit": "mW", "sample_value_w": _round6(total_w),
             "refresh_note": f"rails: {[r for r, _ in rails]}"}, _sampler)


def _rapl_source(sys_root):
    """RAPL powercap energy counters (expected absent on aarch64 — named skip)."""
    zones = sorted(glob.glob(os.path.join(sys_root, "class", "powercap", "*",
                                          "energy_uj")))
    if not zones:
        return ({"source": "rapl", "path_or_tool": "/sys/class/powercap",
                 "unit": None, "sample_value_w": None,
                 "refresh_note": "SKIPPED (named): no powercap energy zones "
                                 "(expected on aarch64)",
                 "verdict": {"usable": False, "reason": "RAPL absent"}}, None)
    zfile = zones[0]
    try:
        sample_uj = int(_read_text(zfile))
    except (OSError, ValueError) as exc:
        return ({"source": "rapl", "path_or_tool": zfile, "unit": "uJ",
                 "sample_value_w": None,
                 "refresh_note": f"SKIPPED (named): unreadable ({exc})",
                 "verdict": {"usable": False, "reason": "energy_uj unreadable"}},
                None)

    def _sampler(path=zfile, interval=0.25):
        before = int(_read_text(path))
        time.sleep(interval)
        after = int(_read_text(path))
        return (after - before) / 1e6 / interval

    return ({"source": "rapl", "path_or_tool": zfile,
             "unit": "uJ (counter; power derived by differencing)",
             "sample_value_w": _round6(sample_uj / 1e6),
             "refresh_note": "cumulative package energy counter"}, _sampler)


# ----------------------------------------------------------------------------
# load-response screening (the empirical usability rule)
# ----------------------------------------------------------------------------
def _sample_phase(sample_fn, samples, interval_s):
    out = []
    for _ in range(samples):
        out.append(float(sample_fn()))
        time.sleep(interval_s) if interval_s else None
    return out


def screen_load_response(sample_fn, load_factory=_BusyLoad,
                         samples=SCREEN_SAMPLES, interval_s=SCREEN_INTERVAL_S,
                         loaded_sample_fn=None):
    """Empirical usability screen: does this sensor's reading respond to a pinned
    CPU busy loop? `loaded_sample_fn` (self-tests) overrides the sampler used
    during the loaded phase so streams can be injected without real load.

    usable iff delta_w >= max(LOAD_RESPONSE_FLOOR_W, NOISE_SD_MULTIPLIER * pooled
    phase sd) — i.e. the response clears both the absolute floor a real CPU load
    must show and the sensor's own observed noise."""
    idle = _sample_phase(sample_fn, samples, interval_s)
    with load_factory():
        loaded = _sample_phase(loaded_sample_fn or sample_fn, samples, interval_s)
    idle_med, loaded_med = _median(idle), _median(loaded)
    idle_sd, loaded_sd = _sample_sd(idle), _sample_sd(loaded)
    delta = loaded_med - idle_med
    noise_floor = NOISE_SD_MULTIPLIER * max(idle_sd, loaded_sd)
    threshold = max(LOAD_RESPONSE_FLOOR_W, noise_floor)
    usable = delta >= threshold
    if usable:
        reason = (f"responds to pinned CPU busy loop: delta {_round6(delta)} W >= "
                  f"threshold {_round6(threshold)} W")
    else:
        reason = (f"does not respond to CPU load: delta {_round6(delta)} W under a "
                  f"pinned busy loop is within noise/floor (threshold "
                  f"{_round6(threshold)} W) — sensor does not observe the CPU "
                  "domain where tasks run")
    return {"usable": usable, "reason": reason,
            "evidence": {"idle_median_w": _round6(idle_med),
                         "idle_sd_w": _round6(idle_sd),
                         "loaded_median_w": _round6(loaded_med),
                         "loaded_sd_w": _round6(loaded_sd),
                         "delta_w": _round6(delta),
                         "threshold_w": _round6(threshold),
                         "samples_per_phase": samples}}


# ----------------------------------------------------------------------------
# STEP A1: discovery + inventory
# ----------------------------------------------------------------------------
def build_inventory(sys_root="/sys", which=shutil.which, run=subprocess.run,
                    screen=True, load_factory=_BusyLoad):
    """Probe every source and return (inventory dict, {source_label: sample_fn} for
    the usable ones). With screen=False, candidates are inventoried but left
    unscreened (verdict says so) — self-test fixtures use this to test the
    inventory shape without spawning load."""
    sources, samplers = [], {}
    candidates = []  # (source dict, sample_fn) pairs awaiting the screen

    for src, fn in _hwmon_sources(sys_root):
        if fn is not None and "verdict" not in src:
            candidates.append((src, fn))
        sources.append(src)
    sources.append(_i2c_ina_source(sys_root))
    for probe in (lambda: _nvidia_smi_source(which, run),
                  lambda: _tegrastats_source(which, run)):
        src, fn = probe()
        if fn is not None and "verdict" not in src:
            candidates.append((src, fn))
        sources.append(src)
    src, fn = _rapl_source(sys_root)
    if fn is not None and "verdict" not in src:
        candidates.append((src, fn))
    sources.append(src)

    for src, fn in candidates:
        if not screen:
            src["verdict"] = {"usable": False,
                              "reason": "candidate not screened (screen=False); "
                                        "usability requires the load-response test"}
            continue
        verdict = screen_load_response(fn, load_factory=load_factory)
        src["verdict"] = verdict
        if verdict["usable"]:
            samplers[f"{src['source']}@{src['path_or_tool']}"] = fn

    usable_labels = sorted(samplers)
    overall = (f"{len(usable_labels)} usable sensor(s): {usable_labels}"
               if usable_labels else DEBT_OPEN_VERDICT)
    inventory = {
        "schema": INVENTORY_SCHEMA,
        "platform_machine": os.uname().machine,
        "privacy_note": "sensor names/paths/units/readings only; no serial "
                        "numbers, UUIDs, or hostnames",
        "usability_rule": "usable iff reading rises under a pinned pure-Python CPU "
                          f"busy loop by >= max({LOAD_RESPONSE_FLOOR_W} W, "
                          f"{NOISE_SD_MULTIPLIER} x phase sd)",
        "measurement_physics_note": MEASUREMENT_PHYSICS_NOTE,
        "integrity_note": INTEGRITY_NOTE,
        "sources": sources,
        "usable_sources": usable_labels,
        "overall_verdict": overall,
    }
    return inventory, samplers


# ----------------------------------------------------------------------------
# STEP A2: sustained-load host characterization
# ----------------------------------------------------------------------------
def characterize_phases(idle_samples, loaded_samples):
    """Pure arithmetic core (hand-checkable in the self-test): medians, sample
    sds, and the active-power delta from two injected sample streams."""
    idle_med, loaded_med = _median(idle_samples), _median(loaded_samples)
    return {"idle_power_w": _round6(idle_med),
            "idle_sd_w": _round6(_sample_sd(idle_samples)),
            "loaded_power_w": _round6(loaded_med),
            "loaded_sd_w": _round6(_sample_sd(loaded_samples)),
            "active_power_delta_w": _round6(loaded_med - idle_med)}


def characterize(sample_fn, sensor_source, load_factory=_BusyLoad,
                 samples=CHAR_SAMPLES, interval_s=CHAR_INTERVAL_S):
    """Full sustained-load-delta characterization against one usable sensor."""
    idle = _sample_phase(sample_fn, samples, interval_s)
    with load_factory():
        loaded = _sample_phase(sample_fn, samples, interval_s)
    stats = characterize_phases(idle, loaded)
    doc = {
        "schema": CHARACTERIZATION_SCHEMA,
        "method": "sustained-load-delta",
        "sensor_source": sensor_source,
        "samples_per_phase": samples,
        "duration_s_per_phase": _round6(samples * interval_s),
        "calibration_load": "pinned pure-Python busy loop (one core; same "
                            "instruction-mix class as the demo tasks)",
        "labels": {"power": "measured-host-level"},
        "limitation": CHARACTERIZATION_LIMITATION,
        "integrity_note": INTEGRITY_NOTE,
    }
    doc.update(stats)
    return doc


# ----------------------------------------------------------------------------
# STEP A3: comparison (report-only; the anchored record stands unchanged)
# ----------------------------------------------------------------------------
def build_comparison(characterization, metering_report):
    """Recompute per-task energy estimates with measured delta_w next to the
    anchored assumed-nameplate figures. Labels are PRESERVED: both columns are
    estimates (cpu_time x constant power); only the power grounding differs."""
    delta_w = characterization["active_power_delta_w"]
    assumed_w = metering_report.get("assumed_cpu_power_w", ASSUMED_CPU_POWER_W)
    rows = []
    for task in metering_report.get("per_task", []):
        cpu_s = task["cpu_time_s"]
        rows.append({
            "task_id": task["task_id"],
            "cpu_time_s": cpu_s,
            "energy_j_assumed": _round6(cpu_s * assumed_w),
            "energy_j_measured_grounded": _round6(cpu_s * delta_w),
            "labels": {"energy": "estimated"},  # the estimate label stays
        })
    return {
        "assumed_cpu_power_w": assumed_w,
        "measured_active_power_delta_w": delta_w,
        "per_task": rows,
        "note": "report-only comparison. The anchored idx-20 metering record "
                "stands unchanged (its label was honest: power 'assumed'); the "
                "measured-grounded upgrade lands in the next metering generation "
                "at the next milestone batch. Both columns remain ESTIMATES "
                "(cpu_time x constant power); only the power grounding differs.",
    }


def _print_comparison(comparison):
    print(f"assumed nameplate:      {comparison['assumed_cpu_power_w']} W")
    print(f"measured active delta:  {comparison['measured_active_power_delta_w']} W")
    print(f"{'task':<12} {'cpu_time_s':>12} {'assumed J':>14} {'measured-gnd J':>16}")
    for row in comparison["per_task"]:
        print(f"{row['task_id']:<12} {row['cpu_time_s']:>12} "
              f"{row['energy_j_assumed']:>14} "
              f"{row['energy_j_measured_grounded']:>16}")
    print("\n" + comparison["note"])


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
def _write_json(path, doc):
    with open(path, "w") as f:
        json.dump(doc, f, indent=2, sort_keys=True)
        f.write("\n")


def _cmd_probe(out_path):
    print("=== power telemetry probe (read-only; screening candidates under a "
          "pinned busy loop) ===")
    inventory, _ = build_inventory()
    _write_json(out_path, inventory)
    for src in inventory["sources"]:
        verdict = src.get("verdict", {})
        state = "USABLE" if verdict.get("usable") else "unusable"
        print(f"  [{state:>8}] {src['source']:<12} {src['path_or_tool']}")
        if verdict.get("reason"):
            print(f"             reason: {verdict['reason']}")
    print(f"\noverall: {inventory['overall_verdict']}")
    print(f"inventory written to {out_path}")
    return 0


def _cmd_characterize(inventory_path, out_path):
    if not os.path.exists(inventory_path):
        print("no sensor inventory found — running discovery first")
        _cmd_probe(inventory_path)
    with open(inventory_path) as f:
        inventory = json.load(f)
    if not inventory.get("usable_sources"):
        print(f"characterization SKIPPED (named): {DEBT_OPEN_VERDICT}")
        print("(absence of a usable sensor is a legitimate finding, not an error; "
              "nothing was fabricated and nothing was written)")
        return 0
    # Re-discover to get a live sampler for the first usable source (samplers are
    # closures over device paths/tools and cannot be persisted in JSON).
    _, samplers = build_inventory()
    if not samplers:
        print("characterization SKIPPED (named): usable sensor in the stored "
              "inventory did not re-screen as usable on this run")
        return 0
    label = sorted(samplers)[0]
    print(f"characterizing against {label} "
          f"({CHAR_SAMPLES} samples x {CHAR_INTERVAL_S}s per phase)...")
    doc = characterize(samplers[label], label)
    _write_json(out_path, doc)
    print(f"idle   {doc['idle_power_w']} W (sd {doc['idle_sd_w']})")
    print(f"loaded {doc['loaded_power_w']} W (sd {doc['loaded_sd_w']})")
    print(f"delta  {doc['active_power_delta_w']} W")
    print(f"characterization written to {out_path}")
    return 0


def _cmd_compare(characterization_path):
    if not os.path.exists(characterization_path):
        print(f"comparison SKIPPED (named): no characterization file at "
              f"{characterization_path} — on this host: {DEBT_OPEN_VERDICT}")
        return 0
    report_path = find_evidence_file(METERING_REPORT_BASENAME)
    if report_path is None:
        print("comparison SKIPPED (named): metering report not found in repo root "
              "or the published evidence bundle")
        return 0
    with open(characterization_path) as f:
        characterization = json.load(f)
    with open(report_path) as f:
        metering = json.load(f)
    _print_comparison(build_comparison(characterization, metering))
    return 0


# ----------------------------------------------------------------------------
# self-test (fixtures + injected streams; temp-only, repo gains no files)
# ----------------------------------------------------------------------------
def _selftest() -> int:
    import tempfile
    import hashlib

    print("=== protocol/power_telemetry.py self-test (fixtures; read-only) ===")
    print("Fixture /sys trees, injected sample streams with hand-computed stats,")
    print("the honest nothing-usable path, and label preservation. No real load is")
    print("spawned; the ledger and repo are never written.\n")

    root_before = set(os.listdir(_REPO_ROOT))
    proto_before = set(os.listdir(_PROTO_DIR))
    ledger_path = os.path.join(_PROTO_DIR, "ledger_data.jsonl")
    ledger_sha_before = None
    if os.path.exists(ledger_path):
        with open(ledger_path, "rb") as f:
            ledger_sha_before = hashlib.sha256(f.read()).hexdigest()

    checks = []
    tol = 1e-9
    no_tool = lambda name: None

    def _no_run(*a, **k):
        raise OSError("no subprocess in fixtures")

    with tempfile.TemporaryDirectory() as tmp:
        # Fixture A: a hwmon node exposing a readable power rail (15 W in uW).
        usable_tree = os.path.join(tmp, "sys_usable")
        node = os.path.join(usable_tree, "class", "hwmon", "hwmon0")
        os.makedirs(node)
        with open(os.path.join(node, "name"), "w") as f:
            f.write("cpu_rail\n")
        with open(os.path.join(node, "power1_input"), "w") as f:
            f.write("15000000\n")
        inv_a, _ = build_inventory(sys_root=usable_tree, which=no_tool,
                                   run=_no_run, screen=False)
        rails = [s for s in inv_a["sources"] if s["source"] == "hwmon:cpu_rail"]
        checks.append(("fixture hwmon power rail is inventoried at 15.0 W (uW ABI)",
                       len(rails) == 1
                       and abs(rails[0]["sample_value_w"] - 15.0) < tol
                       and rails[0]["unit"] == "uW"))
        checks.append(("unscreened candidate is NOT usable (screen is the gate)",
                       inv_a["usable_sources"] == []
                       and "not screened" in rails[0]["verdict"]["reason"]))

        # Fixture B: fan-actuator-style rail (5 mW) + empty tool set -> every
        # source unusable -> the honest open-debt verdict, verbatim.
        unusable_tree = os.path.join(tmp, "sys_unusable")
        node_b = os.path.join(unusable_tree, "class", "hwmon", "hwmon0")
        os.makedirs(node_b)
        with open(os.path.join(node_b, "name"), "w") as f:
            f.write("acpi_fan\n")
        inv_b, samplers_b = build_inventory(sys_root=unusable_tree, which=no_tool,
                                            run=_no_run, screen=False)
        named_skips = [s for s in inv_b["sources"]
                       if "SKIPPED (named)" in (s.get("refresh_note") or "")]
        checks.append(("nothing-usable fixture yields the open-debt verdict verbatim",
                       inv_b["overall_verdict"] == DEBT_OPEN_VERDICT
                       and samplers_b == {}))
        checks.append(("absent i2c-INA / nvidia-smi / tegrastats / RAPL are all "
                       "NAMED skips", len(named_skips) == 4))

        # Screening on injected streams: flat sensor -> unusable; responsive
        # sensor -> usable. Streams are hand-designed; no load is spawned (_NoLoad).
        flat = iter([4.65, 4.66, 4.65, 4.64, 4.65, 4.66, 4.65, 4.64, 4.65, 4.66,
                     4.65, 4.66, 4.65, 4.64, 4.65, 4.66, 4.65, 4.64, 4.65, 4.66])
        v_flat = screen_load_response(lambda: next(flat), load_factory=_NoLoad,
                                      samples=10, interval_s=0)
        checks.append(("flat sensor screens unusable ('does not observe the CPU "
                       "domain')", not v_flat["usable"]
                       and "does not observe the CPU domain" in v_flat["reason"]))
        idle_stream = iter([10.0] * 10)
        loaded_stream = iter([16.0] * 10)
        v_resp = screen_load_response(lambda: next(idle_stream),
                                      load_factory=_NoLoad, samples=10,
                                      interval_s=0,
                                      loaded_sample_fn=lambda: next(loaded_stream))
        checks.append(("responsive sensor screens usable with delta 6.0 W",
                       v_resp["usable"]
                       and abs(v_resp["evidence"]["delta_w"] - 6.0) < tol))

        # Characterization arithmetic, HAND-COMPUTED:
        #   idle   [10.0, 10.4, 10.2]: median 10.2; mean 10.2;
        #          sd = sqrt(((0.2)^2+(0.2)^2+0)/2) = sqrt(0.04) = 0.2
        #   loaded [15.0, 15.8, 15.4]: median 15.4; sd = sqrt(0.16) = 0.4
        #   delta  = 15.4 - 10.2 = 5.2
        stats = characterize_phases([10.0, 10.4, 10.2], [15.0, 15.8, 15.4])
        checks.append(("characterization stats match hand computation "
                       "(10.2±0.2 / 15.4±0.4 / delta 5.2)",
                       abs(stats["idle_power_w"] - 10.2) < tol
                       and abs(stats["idle_sd_w"] - 0.2) < tol
                       and abs(stats["loaded_power_w"] - 15.4) < tol
                       and abs(stats["loaded_sd_w"] - 0.4) < tol
                       and abs(stats["active_power_delta_w"] - 5.2) < tol))

        # Full characterize() on an injected alternating stream (median of a
        # 10.0/12.0 alternation = 11.0; no real load via _NoLoad).
        alt = iter(([10.0, 12.0] * 30)[:60])
        doc = characterize(lambda: next(alt), "fixture@stream",
                           load_factory=_NoLoad, samples=30, interval_s=0)
        checks.append(("characterize() carries method/labels/limitation honestly",
                       doc["method"] == "sustained-load-delta"
                       and doc["labels"] == {"power": "measured-host-level"}
                       and "ESTIMATE" in doc["limitation"]
                       and doc["samples_per_phase"] == 30))
        checks.append(("default sampling meets the >=30 samples / >=15 s floor",
                       CHAR_SAMPLES >= 30 and CHAR_SAMPLES * CHAR_INTERVAL_S >= 15))

        # Comparison: labels preserved, arithmetic exact, anchored note present.
        fixture_char = {"active_power_delta_w": 5.2}
        fixture_meter = {"assumed_cpu_power_w": 15.0,
                         "per_task": [{"task_id": "task-9999",
                                       "cpu_time_s": 0.001}]}
        comp = build_comparison(fixture_char, fixture_meter)
        row = comp["per_task"][0]
        checks.append(("comparison keeps the 'estimated' label on BOTH columns",
                       row["labels"] == {"energy": "estimated"}
                       and abs(row["energy_j_assumed"] - 0.015) < tol
                       and abs(row["energy_j_measured_grounded"] - 0.0052) < tol))
        checks.append(("comparison says the anchored record stands unchanged",
                       "stands unchanged" in comp["note"]
                       and "next milestone batch" in comp["note"]))

        # Inventory format determinism: same fixture tree twice -> byte-identical
        # JSON (no timestamps in the format; only live readings vary, and the
        # fixture's reading is fixed).
        inv_a2, _ = build_inventory(sys_root=usable_tree, which=no_tool,
                                    run=_no_run, screen=False)
        checks.append(("inventory format is deterministic (fixture tree rebuilds "
                       "byte-identical)",
                       json.dumps(inv_a, sort_keys=True)
                       == json.dumps(inv_a2, sort_keys=True)))

        # tegrastats parser on a canned line.
        canned = ("11-15-2025 RAM 3456/7620MB SWAP 0/3810MB CPU [2%@1420] "
                  "VDD_IN 4321mW/4300mW VDD_CPU_GPU_CV 1234mW/1200mW")
        checks.append(("tegrastats parser extracts instant mW rails",
                       _parse_tegrastats_power_mw(canned)
                       == [("VDD_IN", 4321), ("VDD_CPU_GPU_CV", 1234)]))

    # Zero-write guarantees: ledger byte-identical, repo gains no files.
    ledger_sha_after = None
    if os.path.exists(ledger_path):
        with open(ledger_path, "rb") as f:
            ledger_sha_after = hashlib.sha256(f.read()).hexdigest()
    checks.append(("ledger untouched (sha256 identical before/after, or absent "
                   "both times)", ledger_sha_before == ledger_sha_after))
    stray_root = set(os.listdir(_REPO_ROOT)) - root_before
    stray_proto = set(os.listdir(_PROTO_DIR)) - proto_before
    checks.append(("repo gained no files (existence delta empty)",
                   not stray_root and not stray_proto))

    failures = 0
    for name, passed in checks:
        print(f"{name:70s}: {'PASS' if passed else 'FAIL'}")
        if not passed:
            failures += 1
    if stray_root:
        print(f"    stray in repo root: {stray_root}")
    if stray_proto:
        print(f"    stray in protocol/: {stray_proto}")

    ok = failures == 0
    print("\n=== self-test summary: " +
          ("ALL CHECKS BEHAVED CORRECTLY" if ok else "FAILURE — see above") + " ===")
    return 0 if ok else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="hardware power telemetry probe (read-only discovery, "
                    "sustained-load characterization, report-only comparison)")
    parser.add_argument("--probe", action="store_true",
                        help="discover sensors and write the inventory")
    parser.add_argument("--characterize", action="store_true",
                        help="sustained-load-delta characterization against the "
                             "first usable sensor (named skip if none)")
    parser.add_argument("--compare", action="store_true",
                        help="report-only assumed-vs-measured energy table")
    parser.add_argument("--inventory", default=DEFAULT_INVENTORY_PATH,
                        help=f"inventory path (default {DEFAULT_INVENTORY_PATH})")
    parser.add_argument("--characterization",
                        default=DEFAULT_CHARACTERIZATION_PATH,
                        help="characterization path (default "
                             f"{DEFAULT_CHARACTERIZATION_PATH})")
    parser.add_argument("--selftest", action="store_true",
                        help="run the fixture self-test (writes nothing)")
    args = parser.parse_args(argv)

    if args.selftest:
        return _selftest()
    if args.probe:
        return _cmd_probe(args.inventory)
    if args.characterize:
        return _cmd_characterize(args.inventory, args.characterization)
    if args.compare:
        return _cmd_compare(args.characterization)
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
