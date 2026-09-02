# Copyright (c) 2023-2026 MetaCoin-Lab.
# Licensed under SML-1.0 — see LICENSE.md.
"""build_hf_dataset.py — deterministic generator for the public Hugging Face
dataset package (datasets/metacoin-tasks/), synced by the hf-sync workflow.

================== HONESTY / SCOPE (READ ME) ==================
Research-stage, ZERO-VALUE, no token. Standard library only; ZERO ledger
writes. From the live task registry this builder emits the complete
dataset package: byte-identical copies of every task module and pinned-
constants module, manifest.json whose canonical output hashes are
ASSERTED against the anchored ledger records (a drifted module refuses
the build by task id — the dataset can never publish a hash the chain
does not hold), tasks.jsonl in the established serialization
(json.dumps(row, ensure_ascii=False), one row per task, source included),
the dataset card, verify_tasks.py, and ATTRIBUTION.md/LICENSE. Honest
negatives come from integrations.core.HONEST_NEGATIVES and their verdict
fields from the baselines roster — never typed here. Output is
byte-deterministic (no timestamps); the self-test builds twice and
asserts it, runs the emitted verifier end-to-end (N/N), checks the
card's negative table row count against the roster, and proves the
tampered-ledger fixture refuses by name.

Usage:
    python3 protocol/build_hf_dataset.py [--out DIR] [--version X.Y.Z]
    python3 protocol/build_hf_dataset.py --selftest
Not financial, legal, or engineering advice.
"""

import sys
sys.dont_write_bytecode = True

import argparse
import hashlib
import importlib
import json
import os
import re

_PROTO_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_PROTO_DIR)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from protocol.prov_export import resolve_ledger_path
from protocol.work_molecule import _read_ledger
from protocol.verifier_cli import TASK_MODULES

DEFAULT_OUT = os.path.join(_REPO_ROOT, "datasets", "metacoin-tasks")
DEFAULT_VERSION = "0.4.0"
TEMPLATE_DIR = os.path.join(_PROTO_DIR, "hf_dataset_templates")
TASKS_SRC = os.path.join(_REPO_ROOT, "demo", "tasks")
PINNED_MODULES = ("cea_thermo_pinned.py", "pinned_sunshade_sources.py",
                  "pinned_spice_sources.py", "pinned_mars_edl_sources.py")
_COUNT_WORDS = {6: "Six", 7: "Seven", 8: "Eight", 9: "Nine", 10: "Ten",
                11: "Eleven", 12: "Twelve"}

# Curated per-task card metadata (description, NASA-taxonomy code, parents by
# full id). The registry decides WHICH tasks ship; this table only describes
# them, and the build refuses a registry task it cannot describe.
METADATA = {
 "task-0001-lunar-link-budget": ("Lunar comms link-budget sweep (FSPL / link margin, 100-2000 km)", None, ()),
 "task-0002-orbit-propagation": ("Two-body orbital propagator", "TX17", ()),
 "task-0003-power-eclipse": ("Orbital eclipse classification + battery state-of-charge energy budget", "TX03", ()),
 "task-0004-comms-access": ("Ground-station communication access windows", "TX05", ()),
 "task-0005-rover-path": ("Minimum-energy rover path over synthetic terrain (Dijkstra)", "TX04", ()),
 "task-0006-docking-approach": ("Rendezvous & docking approach-corridor check", "TX17", ()),
 "task-0007-hohmann-transfer": ("Hohmann transfer delta-v budget", "TX01", ()),
 "task-0008-arm-inverse-kinematics": ("2-link planar arm inverse kinematics", "TX04", ()),
 "task-0009-power-budget": ("Spacecraft power budget & solar-array energy balance", "TX03", ()),
 "task-0010-thermal-equilibrium": ("Passive radiative equilibrium temperature", "TX14", ()),
 "task-0011-ballistic-reentry": ("Allen-Eggers ballistic re-entry peaks", "TX09", ()),
 "task-0012-comms-link-budget": ("Deep-space X-band link budget + first-order Doppler", "TX05", ()),
 "task-0013-lambert-transfer": ("Universal-variable Lambert solver", "TX17", ()),
 "task-0014-fdir-state-machine": ("FDIR autonomous fault-management state machine", "TX10", ()),
 "task-0015-sabatier-isru": ("Sabatier ISRU propellant mass balance (single pass)", "TX07", ()),
 "task-0016-triad-attitude": ("TRIAD star-tracker attitude determination", "TX08", ()),
 "task-0017-isru-ascent-budget": ("ISRU ascent propellant budget (first parented task: consumes task-0015 output, parent hash asserted live at run time)", "TX01", ("task-0015-sabatier-isru",)),
 "task-0018-ascent-feasibility": ("Mars-ascent feasibility verdict (three-generation chain task-0015 -> task-0017 -> this)", "TX17", ("task-0017-isru-ascent-budget",)),
 "task-0019-sabatier-equilibrium-constant": ("Sabatier equilibrium constant K_eq(T) from NASA CEA 9-coefficient polynomials, pinned verbatim with checksum provenance (cea_thermo_pinned.py)", "TX07", ()),
 "task-0020-sabatier-conversion-equilibrium": ("Sabatier equilibrium conversion vs the chain's assumed single-pass conversion (parents task-0019 + task-0015)", "TX07", ("task-0019-sabatier-equilibrium-constant", "task-0015-sabatier-isru")),
 "task-0021-conversion-corrected-ascent": ("Mars-ascent feasibility at the thermodynamically honest equilibrium conversion (parents task-0020 + task-0017 + task-0018 — the bridge where the chain's branches meet)", "TX01", ("task-0020-sabatier-conversion-equilibrium", "task-0017-isru-ascent-budget", "task-0018-ascent-feasibility")),
 "task-0022-insolation-offset-requirement": ("Required fractional insolation reduction from a pinned radiative-forcing target (IAU B3 + AR6-assessed ERF; the classic ~1.6% doubled-CO2 class derived, not quoted)", "TX14", ()),
 "task-0023-sub-l1-shade-geometry": ("Radiation-pressure-shifted sub-L1 sunshade equilibrium (bounded bisection) and occulting area, full-disc efficiency proved (parent task-0022)", "TX17", ("task-0022-insolation-offset-requirement",)),
 "task-0024-shade-mass-budget": ("Total sunshade film mass and unit-sail count (parent task-0023)", "TX12", ("task-0023-sub-l1-shade-geometry",)),
 "task-0025-regolith-feedstock-energy": ("Lunar regolith feedstock tonnage, extraction energy (stated lower bound), sustained power and PV area (parent task-0024)", "TX07", ("task-0024-shade-mass-budget",)),
 "task-0026-mass-driver-energetics": ("Lunar mass-driver launch energetics (escape velocity derived from pinned JPL GM+R), throughput and deployment duration (parent task-0024)", "TX01", ("task-0024-shade-mass-budget",)),
 "task-0027-deployment-timeline-verdict": ("Deployment-timeline verdict vs a climate-relevant horizon (parent task-0026)", "TX01", ("task-0026-mass-driver-energetics",)),
 "task-0028-l1-dust-persistence": ("L1 dust-cloud persistence: CR3BP instability e-folding derived (~23 d) + radiation-pressure grain eviction", "TX17", ()),
 "task-0029-shade-longevity-horizon": ("Longevity horizon vs the claimed billion years under Gough solar brightening (parent task-0022; a conditional honest YES at ~1.02 Gyr)", "TX14", ("task-0022-insolation-offset-requirement",)),
 "task-0030-utc-tdb-conversion": ("UTC→TDB epoch conversion from the pinned NAIF leap-second and DELTET kernel constants (the SPICE time backbone)", "TX17", ()),
 "task-0031-earth-mars-window": ("Earth–Mars transfer-window sweep over the pinned DE440s ephemeris grid (universal-variable Lambert; parent task-0030; the best window honestly passes its stated v-infinity budget, 86/90 instances honestly fail)", "TX17", ("task-0030-utc-tdb-conversion",)),
 "task-0033-mars-capture-entry-interface": ("Mars arrival energetics from the anchored window's v-infinity: entry-interface speed (MSL class derived, not quoted) and the propulsive-capture delta-v alternative (parent task-0031)", "TX09", ("task-0031-earth-mars-window",)),
 "task-0034-edl-deceleration-budget": ("Ballistic EDL deceleration budget over the pinned NASA GRC atmosphere fits: Viking-class passes, the mission-relevant heavy-lander class honestly cannot reach its parachute gate (parent task-0033)", "TX09", ("task-0033-mars-capture-entry-interface",)),
 # The software/data-engineering TRANSFER family (TX11): the abstention
 # probe design on the deterministic tasks coding/data agents face.
 "task-0035-schema-migration-consistency": ("Schema v1→v2 migration over a pinned record set with an integrity verdict: dropping the region key honestly cannot preserve username uniqueness (software/data transfer family; honest negative)", "TX11", ()),
 "task-0036-api-contract-satisfiability": ("Typed request-contract satisfiability by bounded exhaustive search: contract A yields a witness and exact count, contract B is proved empty over its whole finite domain (software/data transfer family)", "TX11", ()),
 "task-0037-dependency-resolution": ("Lockfile-style dependency solve over a pinned package graph plus a conflict instance whose honest answer is unsatisfiable with its minimal conflicting-pin core (software/data transfer family)", "TX11", ()),
 "task-0038-config-consistency-audit": ("Cross-field configuration audit (unit ranges, retry arithmetic, TLS mutual exclusion) whose rule engine first proves itself on a planted broken fixture (software/data transfer family)", "TX11", ()),
 "task-0039-data-pipeline-reconciliation": ("Source-vs-sink ledger reconciliation in integer cents with a balanced verdict; the reconciler proves itself on a planted broken sink first (software/data transfer family)", "TX11", ()),
 "task-0040-test-coverage-gap": ("Root-to-leaf path coverage of a pinned call graph against a pinned test map: the coverage target is honestly not met and the uncovered paths are the deliverable (software/data transfer family; honest negative)", "TX11", ()),
}


# Card-facing one-liners for the honest negatives (the verdict field each
# note opens with is cross-asserted against the baselines roster at build).
NEGATIVE_NOTES = {
 "task-0012-comms-link-budget": "link_closes: false — the link margin honestly does not close; the negative verdict is the deliverable",
 "task-0018-ascent-feasibility": "feasible: false — ~2.19 km/s achievable vs a ~4.7 km/s class requirement; anchoring 'no' is the point",
 "task-0020-sabatier-conversion-equilibrium": "reference_conversion_acceptable: false — equilibrium conversion ~0.81 at the 700 K / 1 bar reference point vs the assumed 0.92; the constants are not tuned to manufacture success",
 "task-0021-conversion-corrected-ascent": "feasible_at_equilibrium_conversion: false — the ascent shortfall grows to ~2.69 km/s at the honest conversion; the two anchored negatives compound",
 "task-0027-deployment-timeline-verdict": "deployable_within_horizon: false — ~187 yr at the claim's stated cadence vs a 50-yr horizon, 3.74x over; the constants are not tuned",
 "task-0028-l1-dust-persistence": "dust_shade_persists: false — a cloud ten-folds its spread in ~53 days vs a one-year minimum; the instability is geometry, not a parameter",
 "task-0034-edl-deceleration-budget": "reference_class_decelerates: false — the heavy-lander class reaches the chute gate at ~Mach 14.5 vs a ~Mach 2 ceiling; Viking-class passes on the same entry state, the mission class honestly does not",
 "task-0035-schema-migration-consistency": "migration_valid: false — two usernames exist in both regions, so dropping region_code cannot preserve the stated uniqueness invariant; the violating keys are the deliverable (software/data transfer family)",
 "task-0040-test-coverage-gap": "coverage_target_met: false — 7 of 10 root-to-leaf paths are covered (0.70 < the 0.90 target); the three uncovered admin/delete paths are the deliverable (software/data transfer family)",
}


def _honest_negatives():
    import integrations.core as core
    return set(core.HONEST_NEGATIVES)


def _negative_verdict_keys():
    from integrations.baselines.run_baseline import _NEGATIVE_VERDICT_KEYS
    return dict(_NEGATIVE_VERDICT_KEYS)


def _anchored_hashes(entries):
    """Latest anchored output hash per short task id, in chain order:
    self-recompute records carry local_output_hash, and the hash-era
    transition record carries era2_output_hash per transitioned task
    (task-0002's current-era hash lives only there). Later records win."""
    out = {}
    for e in entries:
        p = e.get("payload", {})
        if (p.get("event") == "self_recompute_result"
                and p.get("local_output_hash")):
            out[p["task_id"]] = p["local_output_hash"]
        elif p.get("event") == "task_hash_era_recorded":
            for t in p.get("transitions", []):
                if t.get("era2_output_hash"):
                    out[t["task_id"]] = t["era2_output_hash"]
    return out


def _negative_sentence(neg_keys):
    import textwrap
    ids = sorted("-".join(t.split("-")[:2]) for t in neg_keys)
    fields = [f"{neg_keys[t][0]}: false" for t in sorted(neg_keys)]
    text = ("Abstention/honesty usage: " + ", ".join(ids[:-1]) + ", and "
            + ids[-1] + " are honest-negative tasks — their CORRECT "
            "canonical results contain " + ", ".join(fields[:-1]) + ", and "
            + fields[-1] + " respectively.")
    return "\n".join(textwrap.wrap(text, width=72))


def build_package(out_dir=DEFAULT_OUT, version=DEFAULT_VERSION, entries=None):
    """Emit the full dataset package; returns the manifest dict."""
    if entries is None:
        entries = _read_ledger(resolve_ledger_path())
    anchored = _anchored_hashes(entries)
    negatives = _honest_negatives()
    neg_keys = _negative_verdict_keys()

    tasks = []
    os.makedirs(os.path.join(out_dir, "tasks"), exist_ok=True)
    if TASKS_SRC not in sys.path:
        sys.path.insert(0, TASKS_SRC)
    for short_id in sorted(TASK_MODULES):
        mod_file = TASK_MODULES[short_id].rsplit(".", 1)[1] + ".py"
        src_path = os.path.join(TASKS_SRC, mod_file)
        with open(src_path, "rb") as f:
            raw = f.read()
        module = importlib.import_module(mod_file[:-3])
        result = module.compute()
        long_id = result["task_id"]
        out_hash = module.output_hash(result)
        if short_id not in anchored:
            raise ValueError(f"{short_id}: no anchored self-recompute record "
                             "on this chain — the dataset refuses to ship it")
        if out_hash != anchored[short_id]:
            raise ValueError(
                f"{short_id}: anchored-hash mismatch — module re-derives "
                f"{out_hash} but the ledger anchors {anchored[short_id]}; "
                "the dataset can never publish a hash the chain does not "
                "hold")
        if long_id not in METADATA:
            raise ValueError(f"{long_id}: registry task has no card metadata "
                             "— describe it in build_hf_dataset.METADATA")
        desc, taxonomy, parents = METADATA[long_id]
        with open(os.path.join(out_dir, "tasks", mod_file), "wb") as f:
            f.write(raw)
        entry = {
            "task_id": long_id, "module": mod_file, "description": desc,
            "nasa_taxonomy": taxonomy, "parents": list(parents),
            "honest_negative": long_id in negatives,
            "spec_sha256": hashlib.sha256(raw).hexdigest(),
            "canonical_output_sha256": out_hash,
        }
        if long_id in negatives:
            note = NEGATIVE_NOTES.get(long_id)
            field = neg_keys.get(long_id, (None,))[0]
            if note is None or field is None or not note.startswith(
                    f"{field}: false"):
                raise ValueError(f"{long_id}: honest negative without a "
                                 "matching NEGATIVE_NOTES entry opening "
                                 "with its verdict field")
            entry["negative_note"] = note
        tasks.append(entry)

    for aux in PINNED_MODULES + ("ATTRIBUTION.md",):
        with open(os.path.join(TASKS_SRC, aux), "rb") as f:
            raw = f.read()
        with open(os.path.join(out_dir, "tasks", aux), "wb") as f:
            f.write(raw)
    with open(os.path.join(_REPO_ROOT, "LICENSE.md"), "rb") as f:
        lic = f.read()
    with open(os.path.join(out_dir, "LICENSE"), "wb") as f:
        f.write(lic)

    n, k = len(tasks), sum(1 for t in tasks if t["honest_negative"])
    if k != len(negatives):
        raise ValueError(f"negative roster mismatch: card would say {k} but "
                         f"integrations.core names {len(negatives)}")
    manifest = {
        "dataset": "metacoin-tasks", "version": version,
        "task_count": n, "honest_negative_count": k,
        "hash_algorithm": "sha256",
        "canonicalization": "json.dumps(result, sort_keys=True, separators="
            "(\",\", \":\"), ensure_ascii=True) over the module's compute() "
            "output; every float pre-rounded to the module's fixed decimals",
        "spec_hash_note": "spec_sha256 is the SHA-256 of the task source "
            "file's exact bytes; these files are edit-frozen. Every task's "
            "canonical output hash is anchored on the MetaCoin public ledger "
            "in its own task record; the spec hashes of the first 18 tasks "
            "are additionally anchored inside content-addressed "
            "work-molecule catalog generations, and newer families join at "
            "the next catalog generation",
        "source": "https://github.com/MetacoinLab/metacoin (demo/tasks/) "
                  "— byte-identical copies of the edit-frozen task "
                  "sources",
        "tasks": tasks,
    }
    with open(os.path.join(out_dir, "manifest.json"), "w",
              encoding="utf-8") as f:
        json.dump(manifest, f, indent=1)
        f.write("\n")

    with open(os.path.join(out_dir, "tasks.jsonl"), "w",
              encoding="utf-8") as f:
        for t in tasks:
            row = dict(t)
            with open(os.path.join(out_dir, "tasks", t["module"]),
                      encoding="utf-8") as src:
                row["source_code"] = src.read()
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    with open(os.path.join(TEMPLATE_DIR, "README_card.md.tmpl"),
              encoding="utf-8") as f:
        card = f.read()
    card = (card.replace("@TASK_COUNT@", str(n))
                .replace("@NEGATIVE_COUNT_WORD@",
                         _COUNT_WORDS.get(k, str(k)))
                .replace("@NEGATIVE_COUNT@", str(k)))
    with open(os.path.join(out_dir, "README.md"), "w", encoding="utf-8") as f:
        f.write(card)

    with open(os.path.join(TEMPLATE_DIR, "verify_tasks.py.tmpl"),
              encoding="utf-8") as f:
        verifier = f.read()
    verifier = (verifier.replace("@TASK_COUNT@", str(n))
                        .replace("@NEGATIVE_SENTENCE@",
                                 _negative_sentence(neg_keys)))
    with open(os.path.join(out_dir, "verify_tasks.py"), "w",
              encoding="utf-8") as f:
        f.write(verifier)
    return manifest


def _tree_bytes(root):
    out = {}
    for dirpath, _dirs, files in os.walk(root):
        for name in sorted(files):
            p = os.path.join(dirpath, name)
            with open(p, "rb") as f:
                out[os.path.relpath(p, root)] = f.read()
    return out


def _selftest() -> int:
    import subprocess
    import tempfile
    print("=== protocol/build_hf_dataset.py self-test (read-only) ===\n")
    checks = []
    entries = _read_ledger(resolve_ledger_path())
    with tempfile.TemporaryDirectory() as tmp:
        a, b = os.path.join(tmp, "a"), os.path.join(tmp, "b")
        m1 = build_package(a, entries=entries)
        m2 = build_package(b, entries=entries)
        checks.append(("deterministic: two builds byte-identical across "
                       "every file", _tree_bytes(a) == _tree_bytes(b)
                       and m1 == m2))
        n, k = m1["task_count"], m1["honest_negative_count"]
        checks.append((f"registry-complete: {n} tasks, every one described",
                       n == len(TASK_MODULES)))
        checks.append((f"negative roster: {k} == integrations.core",
                       k == len(_honest_negatives())))
        card = open(os.path.join(a, "README.md"), encoding="utf-8").read()
        rows = len(re.findall(r"^\| `task-\d{4}", card, re.M))
        checks.append((f"card negative table has exactly {k} rows "
                       "(drift fails loudly)", rows == k))
        checks.append(("no operator checklist ships in the package",
                       not os.path.exists(os.path.join(
                           a, "submission_checklist.md"))))
        checks.append(("every honest negative carries a negative_note "
                       "opening with its verdict field",
                       all("negative_note" in t
                           for t in m1["tasks"] if t["honest_negative"])))
        proc = subprocess.run(
            [sys.executable, os.path.join(a, "verify_tasks.py")],
            capture_output=True, text=True)
        want = f"{n}/{n} tasks re-derived to the recorded hashes."
        checks.append((f"emitted verify_tasks.py re-derives {n}/{n} "
                       "end-to-end", proc.returncode == 0
                       and want in proc.stdout))
        # Refusal fixture: one anchored hash tampered -> refuse by name.
        import copy
        bad = copy.deepcopy(entries)
        for e in bad:
            p = e.get("payload", {})
            if (p.get("event") == "self_recompute_result"
                    and p.get("task_id") == "task-0034"):
                p["local_output_hash"] = "0" * 64
        refused = False
        try:
            build_package(os.path.join(tmp, "c"), entries=bad)
        except ValueError as exc:
            refused = ("task-0034" in str(exc)
                       and "anchored-hash mismatch" in str(exc))
        checks.append(("a ledger that anchors a different hash refuses by "
                       "task id", refused))
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
        description="Task registry -> Hugging Face dataset package, hashes "
                    "asserted against the anchored ledger (research-stage, "
                    "ZERO-VALUE, no token).")
    parser.add_argument("--out", default=DEFAULT_OUT)
    parser.add_argument("--version", default=DEFAULT_VERSION)
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args(argv)
    if args.selftest:
        return _selftest()
    if not re.fullmatch(r"\d+\.\d+\.\d+", args.version):
        raise SystemExit(f"--version must be X.Y.Z, got {args.version!r}")
    manifest = build_package(args.out, version=args.version)
    print(f"package written: {args.out} (v{manifest['version']}, "
          f"{manifest['task_count']} tasks / "
          f"{manifest['honest_negative_count']} honest negatives)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
