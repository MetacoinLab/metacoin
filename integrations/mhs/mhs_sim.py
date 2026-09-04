# Copyright (c) 2023-2026 MetaCoin-Lab.
# Licensed under SML-1.0 — see LICENSE.md.
"""integrations/mhs/mhs_sim.py — the MHS-SHAPED device surface over the
protocol's physical-work record (simulated device; pre-standard).

============================== SCOPE / BOUNDARY ==============================
STANDARD LIBRARY ONLY. ZERO ledger writes. This file is the thin surface
that looks like the publicly described Model Hardware Standard — a
device exposes `read(name)` / `write(name, value)` primitives, a
reference manifest (capabilities + enforced safety limits + identity),
and a state dictionary any client can inspect — wrapped around
protocol/physical_work.py, which owns the record model, the analysis,
and verification. "MHS-shaped, pre-standard, simulated device": the
standard itself is a limited research preview and not yet open source;
nothing here is a claim about its specification, and no instrument was
operated. Licensed SML-1.0 with the rest of the repository (house
policy; a FOSS carve-out of the verification toolkit is a separate,
operator-owned decision).
==============================================================================

Three control paths, mirroring the public description (agents reach devices
"using standard protocols, such as the Model Context Protocol", "the
command line interface, and code files"):
  * code: `StateBus` + `MhsShapedDevice` below (import and call);
  * CLI:  `python3 integrations/mhs/mhs_sim.py --state | --read NAME |
          --write NAME VALUE | --demo | --selftest`;
  * a tool-call surface: `tool_schema()` returns the read/write/manifest
    primitives as JSON-schema tool descriptions any MCP-style harness can
    register (no MCP server is shipped here; the shapes are).

The demo mirrors the public CMU description: run 1 rejected by the
analysis (saturation, R^2 < 0.9 — the honest negative), run 2 accepted
after the concentration range is adjusted, plus one blocked write recorded
as a first-class refusal. Every measurement is ATTESTED by the device's
one-time signature; only the analysis is re-derived.

Self-test: `python3 integrations/mhs/mhs_sim.py --selftest`.
Research-only; no token; not financial, legal, or engineering advice.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import protocol.physical_work as pw


class StateBus:
    """The shared state dictionary: every device variable readable through
    one interface (the ledger-as-bus idea at device scale). Read-only view
    over the device's own dictionary; writes go through the device so the
    limits are enforced before any state changes."""

    def __init__(self, device: pw.SimulatedDevice):
        self._device = device

    def snapshot(self) -> dict:
        return dict(self._device.state)

    def keys(self) -> list:
        return sorted(self._device.state)


class MhsShapedDevice:
    """read / write / manifest — the three primitives, plus the state bus."""

    def __init__(self, device_id: str = "sim-plate-reader-01"):
        self._device = pw.SimulatedDevice(device_id)
        self.bus = StateBus(self._device)

    def manifest(self) -> dict:
        return self._device.manifest()

    def read(self, name: str):
        return self._device.read(name)

    def write(self, name: str, value) -> dict:
        """Returns {"ok": True} or a refusal dict; never raises on a limit
        block — the refusal is the result."""
        try:
            self._device.write(name, value)
            return {"ok": True, "name": name, "value": value}
        except pw.LimitRefusal as exc:
            return {"ok": False, "refused": True, "name": name,
                    "value": value, "reason": str(exc)}


def tool_schema() -> list:
    """Tool descriptions any MCP-style harness can register (shapes only)."""
    return [
        {"name": "mhs_read", "description": "Read one device variable.",
         "input_schema": {"type": "object", "properties": {
             "name": {"type": "string", "enum": pw.CAPABILITIES["reads"]}},
             "required": ["name"]}},
        {"name": "mhs_write", "description": "Write one device variable; "
         "refused before any state change if outside the manifest's "
         "safety limits.",
         "input_schema": {"type": "object", "properties": {
             "name": {"type": "string", "enum": pw.CAPABILITIES["writes"]},
             "value": {"type": "number"}}, "required": ["name", "value"]}},
        {"name": "mhs_manifest", "description": "The device reference: "
         "capabilities, enforced safety limits, identity root.",
         "input_schema": {"type": "object", "properties": {}}},
    ]


def _selftest() -> int:
    print("=== integrations/mhs/mhs_sim.py self-test (MHS-shaped surface; "
          "stdlib-only; zero ledger writes) ===\n")
    ok = []
    dev = MhsShapedDevice()
    m = dev.manifest()
    ok.append(("manifest carries capabilities, safety limits, identity root, "
               "the simulated flag and the verbatim label",
               set(m["capabilities"]) == {"reads", "writes"}
               and m["safety_limits"] == {n: v for n, v, _, _ in
                                          pw.SAFETY_LIMITS}
               and len(m["identity"]["merkle_root"]) == 64
               and m["simulated"] is True and m["label"] == pw.LABEL))
    ok.append(("read primitive returns the state dictionary's value",
               dev.read("temperature_c") == dev.bus.snapshot()["temperature_c"]))
    r = dev.write("shaker_speed_rpm", 900)
    ok.append(("in-limit write succeeds and the bus shows it",
               r["ok"] and dev.bus.snapshot()["shaker_speed_rpm"] == 900))
    before = dev.bus.snapshot()
    r2 = dev.write("shaker_speed_rpm", 1800)
    ok.append(("over-limit write is REFUSED before any state change",
               r2.get("refused") is True and dev.bus.snapshot() == before))
    ok.append(("unknown capability is not silently accepted",
               _raises(lambda: dev.read("laser_power_mw"))))
    schema = tool_schema()
    ok.append(("tool schema exposes exactly the three primitives",
               [t["name"] for t in schema] == ["mhs_read", "mhs_write",
                                               "mhs_manifest"]))
    doc = pw.build_record()
    v_ok, reasons, stats = pw.verify_record(doc)
    h = pw.headline(doc)
    ok.append(("the demo record verifies: run-1 rejected, run-2 accepted, "
               "one refusal, all snapshots attested",
               v_ok and h["rejected"] == 1 and h["accepted"] == 1
               and h["refusals"] == 1
               and stats["signatures_ok"] == h["snapshots_attested"] + 1))
    print("--- self-test invariants ---")
    fails = 0
    for name, passed in ok:
        print(f"{name:72s}: {'PASS' if passed else 'FAIL'}")
        fails += not passed
    print("\n=== self-test summary: "
          + ("ALL CASES BEHAVED CORRECTLY" if not fails else "FAILURE — see above")
          + " ===")
    return 0 if not fails else 1


def _raises(fn) -> bool:
    try:
        fn()
    except KeyError:
        return True
    return False


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--state", action="store_true")
    p.add_argument("--manifest", action="store_true")
    p.add_argument("--read", metavar="NAME")
    p.add_argument("--write", nargs=2, metavar=("NAME", "VALUE"))
    p.add_argument("--demo", action="store_true",
                   help="build the simulated demo record and print the "
                        "verdicts (writes nothing)")
    p.add_argument("--selftest", action="store_true")
    a = p.parse_args(argv)
    if a.selftest:
        return _selftest()
    dev = MhsShapedDevice()
    if a.manifest:
        print(json.dumps(dev.manifest(), indent=1, sort_keys=True)); return 0
    if a.state:
        print(json.dumps(dev.bus.snapshot(), indent=1, sort_keys=True)); return 0
    if a.read:
        print(json.dumps({a.read: dev.read(a.read)})); return 0
    if a.write:
        print(json.dumps(dev.write(a.write[0], float(a.write[1])))); return 0
    if a.demo:
        doc = pw.build_record()
        for run in doc["runs"]:
            v = run["analysis"]["verdict"]
            print(f"{run['run_id']}: run_acceptable={v['run_acceptable']} "
                  f"R^2={run['analysis']['r2']} plateau="
                  f"{run['analysis']['plateau_detected']} energy_j="
                  f"{run['analysis']['energy_j']} — {v['reason']}")
        for r in doc["refusals"]:
            print(f"refusal t={r['t_s']}s: {r['reason']} (state_changed="
                  f"{r['state_changed']})")
        print(f"record_hash {doc['record_hash']} — {pw.LABEL}; "
              f"{pw.EPISTEMOLOGY}")
        return 0
    p.print_help(); return 2


if __name__ == "__main__":
    sys.exit(main())
