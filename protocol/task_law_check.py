# Copyright (c) 2023-2026 MetaCoin-Lab.
# Licensed under SML-1.0 — see LICENSE.md.
"""task_law_check.py — the mechanical enforcement of MIP-0008 (task-code
discipline) and MIP-0009 (the task interface contract).

================== HONESTY / SCOPE (READ ME) ==================
Research-stage, ZERO-VALUE, no token, no money, no mainnet, no payments.

A law without its enforcement is a wish. This module is the checker both
MIPs ship with: it lints every task module the protocol registers and
refuses, by name, each violation of the two laws. Standard library only
(ast, importlib, hashlib, json). Not legal or financial advice. No NASA
affiliation or endorsement — the rules below are ADAPTED from public
sources (Holzmann's "Power of 10", IEEE Computer 2006; JPL D-60411's
waiver discipline; F´'s typed-port doctrine; the Mars Climate Orbiter
units mishap) and cited as such.

FORWARD-ONLY LAW — GRANDFATHERING BY ERA, NOT BY NAME. The chain decides
which modules the law binds:

    registration index of a task  = the LOWEST ledger index whose payload
                                    references its task_id (the same
                                    work_molecule._payload_references_task
                                    reading the recorded_task_count docs
                                    token uses)
    law index                     = the index of the ACCEPTED
                                    mip_decision_recorded record for
                                    MIP-0008 (None while the law is pending)
    GRANDFATHERED                 = registration index exists AND is lower
                                    than the law index (or the law is pending)
    BOUND                         = everything else: a module the chain had
                                    never referenced before the law existed

A grandfathered module is SKIPPED BY ERA — never evaluated against either
law — because its bytes are edit-frozen: its recorded output hashes are
anchored on the chain (self_recompute_result and later records) and, for
the era-2 pair, its spec hash is anchored at the era transition. Binding
those modules retroactively would force edits that drift anchored
values; the law therefore starts where the chain says it starts. No name
list exists anywhere in this file: add a task to the registry, and until
its first anchored reference lands AFTER the law index it is bound; land
a reference before the law and it is grandfathered — the chain, not the
author, decides.

THE RULES (MIP-0008, task-code discipline), each refused by name:
  R1 assertion density   compute() carries >= 2 assert statements, each
                         with a message (the physical-invariant classes —
                         conservation, bounds, known-truth — are the
                         review's job; the count and the message are the
                         machine's)
  R2 bounded loops       every `while` test names its bound (a Name
                         containing MAX / BOUND / LIMIT, or an int literal);
                         `while True` and itertools-style infinite
                         iterators (count/cycle/repeat) are refused
  R3 recursion by waiver a function reaching itself through the module's
                         call graph is refused unless a module-level
                         P10_WAIVERS entry names it with a stated bound,
                         a written justification, and the approving MIP
                         (whose file must exist beside the others)
  R4 units in names      every numeric field the module emits (int/float,
                         not bool) carries its unit as a name suffix, is a
                         declared dimensionless kind (count, index, seed,
                         ratio, ...), or sits beside an explicit `unit`
                         sibling field
THE CONTRACT (MIP-0009, the typed port), each refused by name:
  C1 four-key result     compute() -> dict with exactly task_id / inputs /
                         results / summary; task_id begins with the
                         registered short id
  C2 canonical rules     canonical_json(result) is the era-2 canonical
                         form: sorted keys, compact separators, ASCII,
                         sign-of-zero-free (idx 67) — proven on the
                         result AND on a -0.0 probe
  C3 rounding boundary   every emitted float is stable under round(x, 6)
  C4 output hash         output_hash(result) == sha256(canonical_json)
  C5 docstring tags      the module docstring carries a NASA-taxonomy TX
                         tag and both standing disclaimers
  C6 registration        the registered id appears at every registration
                         point (verify_gates registry, run_all_selftests
                         runner, integrations/core roster, gate3 taxonomy)

Usage:
    python3 protocol/task_law_check.py --check                 # the registry
    python3 protocol/task_law_check.py --module PATH --task-id task-NNNN
    python3 protocol/task_law_check.py --selftest              # fixtures only
"""

# Suppress __pycache__/*.pyc so importing task modules leaves no stray files.
import sys
sys.dont_write_bytecode = True

import argparse
import ast
import hashlib
import importlib
import importlib.util
import json
import os
import re
import tempfile

_PROTO_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_PROTO_DIR)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from protocol.prov_export import resolve_ledger_path
from protocol.work_molecule import _payload_references_task, _read_ledger

LAW_MIP_ID = "MIP-0008"
CONTRACT_MIP_ID = "MIP-0009"
ROUND_DECIMALS = 6
MIN_ASSERTS = 2
REQUIRED_RESULT_KEYS = ("task_id", "inputs", "results", "summary")
_MIP_DIR = os.path.join(_REPO_ROOT, "mip")

_BOUND_NAME_RE = re.compile(r"MAX|BOUND|LIMIT")
_INFINITE_ITERATORS = ("count", "cycle", "repeat")
_TX_TAG_RE = re.compile(r"\bTX\d{2}\b")
_DISCLAIMERS = ("No NASA affiliation", "Not financial")
_MIP_ID_RE = re.compile(r"^MIP-\d{4}$")
_WAIVER_KEYS = ("rule", "function", "bound", "justification", "approved_by")

# R4 — unit suffixes accepted at the END of a field name (after the last
# underscore). Compound units use underscores for "per" (m_s, m_s2, km3_s2)
# and digits for powers, exactly the house style the 18 modules already use.
UNIT_SUFFIXES = frozenset("""
m km cm mm um nm m2 km2 m3 km3 s ms us ns min h hr day days yr Gyr
kg g mg t N kN Pa kPa MPa bar J kJ MJ W kW MW Wh kWh A mA V mV Hz kHz MHz GHz
K degC deg rad mrad arcsec arcmin dB dBi dBW dBm dBHz
m_s km_s m_s2 km_s2 m3_s2 km3_s2 kg_s g_s mol kmol mol_s W_m2 kg_m3 g_cm3
rpm pct percent ppm au AU ly bps kbps Mbps Gbps bit bits bytes B
eV keV MeV sr lux lm cd T uT nT ohm S F H Sv Gy Bq
J_mol kJ_mol J_mol_K kJ_mol_K
""".split())
# R4 — dimensionless kinds, accepted as a suffix or as the whole name.
DIMENSIONLESS_SUFFIXES = frozenset("""
count n num index idx id seed decimals steps iters iterations ratio fraction
frac coefficient coef factor efficiency unit flag code rows cols cells order k
generation epoch version mach cosine sine angle_ratio quality
dimensionless unitless
""".split())


# ----------------------------------------------------------------------------
# Era: who the law binds (the chain decides)
# ----------------------------------------------------------------------------
def law_index(entries, mip_id: str = LAW_MIP_ID):
    """Index of the ACCEPTED decision record for `mip_id`, or None (pending)."""
    for e in entries:
        p = e.get("payload") if isinstance(e, dict) else None
        if (isinstance(p, dict)
                and p.get("event") == "mip_decision_recorded"
                and p.get("mip_id") == mip_id
                and p.get("decision") == "accepted"):
            return e["index"]
    return None


def registration_index(entries, short_task_id: str):
    """Lowest ledger index whose payload references the task, or None."""
    for e in entries:
        if isinstance(e, dict) and _payload_references_task(
                e.get("payload"), short_task_id):
            return e["index"]
    return None


def era_status(entries, short_task_id: str, mip_id: str = LAW_MIP_ID):
    """('grandfathered'|'bound', first_index, law_idx, reason)."""
    law_idx = law_index(entries, mip_id)
    first = registration_index(entries, short_task_id)
    if first is None:
        return ("bound", None, law_idx,
                "never referenced by an anchored record — the law binds it")
    if law_idx is None:
        return ("grandfathered", first, None,
                f"first anchored reference idx {first} predates the law "
                f"({mip_id} decision not yet on the chain)")
    if first < law_idx:
        return ("grandfathered", first, law_idx,
                f"first anchored reference idx {first} < law idx {law_idx}")
    return ("bound", first, law_idx,
            f"first anchored reference idx {first} >= law idx {law_idx}")


# ----------------------------------------------------------------------------
# Static rules (MIP-0008) — AST only, the module is never executed here
# ----------------------------------------------------------------------------
def _function_defs(tree):
    return [n for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]


def rule_assert_density(tree):
    """R1: compute() carries >= MIN_ASSERTS asserts, each with a message."""
    compute = next((f for f in _function_defs(tree) if f.name == "compute"),
                   None)
    if compute is None:
        return (False, "no compute() function")
    asserts = [n for n in ast.walk(compute) if isinstance(n, ast.Assert)]
    unmessaged = sum(1 for a in asserts if a.msg is None)
    ok = len(asserts) >= MIN_ASSERTS and unmessaged == 0
    return (ok, f"{len(asserts)} assert(s) in compute() "
                f"(minimum {MIN_ASSERTS}); {unmessaged} without a message")


def _names_in(node):
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def rule_bounded_loops(tree):
    """R2: every while names its bound; no infinite iterators."""
    findings = []
    for n in ast.walk(tree):
        if isinstance(n, ast.While):
            test = n.test
            if isinstance(test, ast.Constant) and test.value is True:
                findings.append(f"line {n.lineno}: `while True` has no bound")
                continue
            named = any(_BOUND_NAME_RE.search(x) for x in _names_in(test))
            literal = any(isinstance(x, ast.Constant)
                          and isinstance(x.value, int)
                          and not isinstance(x.value, bool)
                          for x in ast.walk(test))
            if not (named or literal):
                findings.append(f"line {n.lineno}: while-test names no bound "
                                "(no MAX/BOUND/LIMIT name, no int literal)")
        elif isinstance(n, ast.For):
            it = n.iter
            fn = None
            if isinstance(it, ast.Call):
                if isinstance(it.func, ast.Name):
                    fn = it.func.id
                elif isinstance(it.func, ast.Attribute):
                    fn = it.func.attr
            if fn in _INFINITE_ITERATORS:
                findings.append(f"line {n.lineno}: for over an infinite "
                                f"iterator ({fn})")
    n_loops = sum(1 for n in ast.walk(tree)
                  if isinstance(n, (ast.While, ast.For)))
    return (not findings,
            f"{n_loops} loop(s), every bound stated" if not findings
            else "; ".join(findings))


def _call_graph(tree):
    graph = {}
    for f in _function_defs(tree):
        calls = set()
        for n in ast.walk(f):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name):
                calls.add(n.func.id)
        graph.setdefault(f.name, set()).update(calls)
    return graph


def recursive_functions(tree):
    """Names of functions that reach themselves through the call graph."""
    graph = _call_graph(tree)
    out = []
    for start in graph:
        seen, stack = set(), list(graph[start])
        while stack:  # bounded: each name is pushed at most once (seen set)
            cur = stack.pop()
            if cur == start:
                out.append(start)
                break
            if cur in seen or cur not in graph:
                continue
            seen.add(cur)
            stack.extend(graph[cur])
    return sorted(out)


def waivers(tree, mip_dir: str = _MIP_DIR):
    """(valid_waivers, findings) from a module-level P10_WAIVERS literal."""
    entries, findings = [], []
    for n in tree.body:
        if (isinstance(n, ast.Assign) and len(n.targets) == 1
                and isinstance(n.targets[0], ast.Name)
                and n.targets[0].id == "P10_WAIVERS"):
            try:
                value = ast.literal_eval(n.value)
            except (ValueError, SyntaxError):
                findings.append("P10_WAIVERS is not a literal list")
                continue
            if not isinstance(value, list):
                findings.append("P10_WAIVERS is not a list")
                continue
            for w in value:
                missing = [k for k in _WAIVER_KEYS
                           if not (isinstance(w, dict)
                                   and isinstance(w.get(k), str)
                                   and w.get(k).strip())]
                if missing:
                    findings.append(f"waiver missing {missing}")
                    continue
                mip = w["approved_by"]
                if not _MIP_ID_RE.match(mip):
                    findings.append(f"waiver approved_by {mip!r} is not a "
                                    "MIP id")
                    continue
                exists = any(f.startswith(mip + "-") and f.endswith(".md")
                             for f in (os.listdir(mip_dir)
                                       if os.path.isdir(mip_dir) else []))
                if not exists:
                    findings.append(f"waiver cites {mip} but no such MIP "
                                    "file exists")
                    continue
                entries.append(w)
    return entries, findings


def rule_recursion(tree, mip_dir: str = _MIP_DIR):
    """R3: recursion only with a recorded waiver."""
    rec = recursive_functions(tree)
    valid, findings = waivers(tree, mip_dir)
    waived = {w["function"] for w in valid if w["rule"] == "recursion"}
    unwaivered = [f for f in rec if f not in waived]
    if unwaivered:
        findings.append(f"unwaivered recursion: {unwaivered}")
    ok = not findings
    detail = ("no recursion" if not rec else
              f"recursion in {rec}, waived by MIP-cited record")
    return (ok, detail if ok else "; ".join(findings))


# ----------------------------------------------------------------------------
# Runtime rules — the module executes once; R4 and the contract read it
# ----------------------------------------------------------------------------
def _sign_safe_zero(obj):
    if isinstance(obj, float):
        return 0.0 if obj == 0.0 else obj
    if isinstance(obj, dict):
        return {k: _sign_safe_zero(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sign_safe_zero(v) for v in obj]
    return obj


def era2_canonical(obj) -> str:
    """The era-2 canonical form (idx 67): sorted, compact, ASCII, no -0.0."""
    return json.dumps(_sign_safe_zero(obj), sort_keys=True,
                      separators=(",", ":"), ensure_ascii=True)


def _unit_ok(key: str) -> bool:
    if key in DIMENSIONLESS_SUFFIXES or key in UNIT_SUFFIXES:
        return True
    tail = key.rsplit("_", 1)[-1]
    if tail in UNIT_SUFFIXES or tail in DIMENSIONLESS_SUFFIXES:
        return True
    # compound units carry the "per" denominator: kg_per_s, W_per_m2
    if "_per_" in key:
        return key.split("_per_")[-1] in UNIT_SUFFIXES
    # two-token tails such as m_s / m_s2 / km3_s2
    parts = key.split("_")
    return len(parts) >= 3 and "_".join(parts[-2:]) in UNIT_SUFFIXES


def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def unitless_fields(result, path="") -> list:
    """R4: numeric fields whose names carry no unit (and no `unit` sibling)."""
    bad = []
    if isinstance(result, dict):
        explicit_unit = isinstance(result.get("unit"), str)
        for k, v in result.items():
            here = f"{path}.{k}" if path else k
            if _is_number(v) or (isinstance(v, list)
                                 and v and all(_is_number(x) for x in v)):
                if not explicit_unit and not _unit_ok(str(k)):
                    bad.append(here)
            else:
                bad.extend(unitless_fields(v, here))
    elif isinstance(result, list):
        for i, v in enumerate(result):
            bad.extend(unitless_fields(v, f"{path}[{i}]"))
    return bad


def unstable_floats(obj, path="") -> list:
    """C3: floats not stable under round(x, ROUND_DECIMALS)."""
    bad = []
    if isinstance(obj, float):
        if round(obj, ROUND_DECIMALS) != obj:
            bad.append(path or "<root>")
    elif isinstance(obj, dict):
        for k, v in obj.items():
            bad.extend(unstable_floats(v, f"{path}.{k}" if path else k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            bad.extend(unstable_floats(v, f"{path}[{i}]"))
    return bad


def load_module(path: str = None, import_name: str = None):
    """Import a task module by dotted name (registry) or by file path."""
    if import_name:
        return importlib.import_module(import_name)
    spec = importlib.util.spec_from_file_location(
        "task_law_fixture_" + hashlib.sha256(path.encode()).hexdigest()[:8],
        path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def contract_checks(mod, short_task_id: str, source: str) -> list:
    """C1-C5 on a loaded module. Returns [(name, ok, detail)]."""
    out = []
    try:
        result = mod.compute()
    except Exception as exc:  # a crashing compute() breaches every clause
        return [("C1 four-key result", False, f"compute() raised {exc!r}")]
    keys_ok = (isinstance(result, dict)
               and tuple(sorted(result)) == tuple(sorted(REQUIRED_RESULT_KEYS)))
    tid_ok = (isinstance(result.get("task_id"), str)
              and result["task_id"].startswith(short_task_id)) \
        if isinstance(result, dict) else False
    out.append(("C1 four-key result", keys_ok and tid_ok,
                f"keys {sorted(result) if isinstance(result, dict) else '?'}"
                + ("" if tid_ok else f"; task_id does not begin with "
                                     f"{short_task_id!r}")))
    cj = getattr(mod, "canonical_json", None)
    oh = getattr(mod, "output_hash", None)
    if not callable(cj) or not callable(oh):
        out.append(("C2 canonical rules", False,
                    "canonical_json/output_hash missing"))
        return out
    probe = {"z": -0.0, "a": [-0.0, 1.5], "u": "é"}
    try:
        c_result = cj(result) == era2_canonical(result)
        c_probe = cj(probe) == era2_canonical(probe)
    except Exception as exc:
        c_result = c_probe = False
        out.append(("C2 canonical rules", False, f"canonical_json raised {exc!r}"))
    else:
        out.append(("C2 canonical rules", c_result and c_probe,
                    "era-2 canonical on the result and on the -0.0 probe"
                    if c_result and c_probe else
                    f"result canonical {'ok' if c_result else 'DIFFERS'}; "
                    f"-0.0 probe {'ok' if c_probe else 'NOT sign-of-zero-free'}"))
    bad_floats = unstable_floats(result)
    out.append(("C3 rounding boundary", not bad_floats,
                f"every float stable under round(x, {ROUND_DECIMALS})"
                if not bad_floats else f"unrounded: {bad_floats[:4]}"))
    try:
        expected = hashlib.sha256(cj(result).encode("utf-8")).hexdigest()
        h_ok = oh(result) == expected
    except Exception as exc:
        h_ok, expected = False, repr(exc)
    out.append(("C4 output hash", h_ok,
                "sha256(canonical_json) == output_hash" if h_ok
                else f"output_hash != sha256(canonical_json) ({expected[:16]})"))
    doc = ast.get_docstring(ast.parse(source)) or ""
    tx = bool(_TX_TAG_RE.search(doc))
    missing = [d for d in _DISCLAIMERS if d not in doc]
    out.append(("C5 docstring tags", tx and not missing,
                "TX tag + both disclaimers present" if tx and not missing
                else f"TX tag {'present' if tx else 'MISSING'}; missing "
                     f"disclaimers {missing}"))
    return out


# ----------------------------------------------------------------------------
# One module, one verdict
# ----------------------------------------------------------------------------
def check_module(path: str, short_task_id: str, entries,
                 import_name: str = None, mip_dir: str = _MIP_DIR,
                 law_mip_id: str = LAW_MIP_ID) -> dict:
    """Lint one task module under the two laws. Returns
    {task_id, file, spec_sha256, era, first_index, law_index, reason,
     evaluated, rules: [(name, ok, detail)], violations: [names], passed}."""
    with open(path, "rb") as f:
        raw = f.read()
    source = raw.decode("utf-8")
    spec_sha = hashlib.sha256(raw).hexdigest()
    era, first, law_idx, reason = era_status(entries, short_task_id,
                                             law_mip_id)
    verdict = {"task_id": short_task_id,
               "file": os.path.relpath(path, _REPO_ROOT),
               "spec_sha256": spec_sha, "era": era, "first_index": first,
               "law_index": law_idx, "reason": reason,
               "evaluated": era == "bound", "rules": [], "violations": [],
               "passed": True}
    if era == "grandfathered":
        return verdict
    tree = ast.parse(source)
    rules = [("R1 assertion density",) + rule_assert_density(tree),
             ("R2 bounded loops",) + rule_bounded_loops(tree),
             ("R3 recursion by waiver",) + rule_recursion(tree, mip_dir)]
    try:
        mod = load_module(path=None if import_name else path,
                          import_name=import_name)
    except Exception as exc:
        rules.append(("C1 four-key result", False, f"import failed: {exc!r}"))
        result = None
    else:
        rules.extend(contract_checks(mod, short_task_id, source))
        try:
            result = mod.compute()
        except Exception:
            result = None
    if isinstance(result, dict):
        bad = unitless_fields(result)
        rules.append(("R4 units in names", not bad,
                      "every numeric field carries its unit" if not bad
                      else f"unitless numeric field(s): {bad[:5]}"))
    verdict["rules"] = rules
    verdict["violations"] = [name for name, ok, _d in rules if not ok]
    verdict["passed"] = not verdict["violations"]
    return verdict


# ----------------------------------------------------------------------------
# The registry: every registered module, plus C6 registration completeness
# ----------------------------------------------------------------------------
def registry_completeness(ids_with_files) -> list:
    """C6: every BOUND id appears at the other registration points.
    `ids_with_files` = [(short_id, module_basename.py), ...]. Forward-only
    like every clause: grandfathered modules are not re-judged (task-0001
    honestly carries no taxonomy tag, and stays that way)."""
    from protocol.gate3_process import TASK_TAXONOMY
    import demo.verify_gates as vg
    with open(os.path.join(_REPO_ROOT, "demo", "run_all_selftests.sh")) as f:
        runner = f.read()
    from integrations.core import TASK_MODULES as core_roster
    core_ids = {tid.split("-")[0] + "-" + tid.split("-")[1]
                for tid, _m, _p in core_roster}
    gates_ids = {k[:9] for k in vg._TASK_REGISTRY}
    findings = []
    for sid, module_file in ids_with_files:
        where = []
        if sid not in gates_ids:
            where.append("demo/verify_gates.py registry")
        if module_file not in runner:
            where.append("demo/run_all_selftests.sh runner")
        if sid not in core_ids:
            where.append("integrations/core.py roster")
        if sid not in TASK_TAXONOMY:
            where.append("protocol/gate3_process.py taxonomy")
        if where:
            findings.append(f"{sid} missing at: {where}")
    return findings


def check_registry(entries=None, echo=print) -> dict:
    from protocol.verifier_cli import TASK_MODULES
    if entries is None:
        entries = _read_ledger(resolve_ledger_path())
    law_idx = law_index(entries)
    echo(f"law {LAW_MIP_ID}: "
         + (f"anchored at idx {law_idx}" if law_idx is not None
            else "pending (no accepted decision on the chain yet)"))
    verdicts = []
    for sid, import_name in TASK_MODULES.items():
        path = os.path.join(_REPO_ROOT, *import_name.split(".")) + ".py"
        v = check_module(path, sid, entries, import_name=import_name)
        verdicts.append(v)
        if v["era"] == "grandfathered":
            echo(f"  {sid}  grandfathered ({v['reason']}): SKIPPED by era")
        else:
            state = "CLEAN" if v["passed"] else "VIOLATION"
            echo(f"  {sid}  bound ({v['reason']}): {state}")
            for name, ok, detail in v["rules"]:
                echo(f"      {name:24s}: {'ok  ' if ok else 'FAIL'}  {detail}")
    c6 = registry_completeness(
        [(v["task_id"], os.path.basename(v["file"])) for v in verdicts
         if v["era"] == "bound"])
    for line in c6:
        echo(f"  C6 registration: {line}")
    n_gf = sum(1 for v in verdicts if v["era"] == "grandfathered")
    n_bound = len(verdicts) - n_gf
    violations = sum(len(v["violations"]) for v in verdicts) + len(c6)
    echo(f"registered modules: {len(verdicts)} | grandfathered by era: {n_gf} "
         f"| bound modules: {n_bound} | violations: {violations}")
    clean = violations == 0
    echo("VERDICT: " + ("TASK-LAW CLEAN" if clean else "TASK-LAW VIOLATED")
         + " — grandfathered modules are skipped by registration era, never "
           "by name; bound modules answer to MIP-0008 and MIP-0009")
    return {"verdicts": verdicts, "registration_findings": c6,
            "law_index": law_idx, "clean": clean}


# ============================== SELF-TEST ====================================
_FIXTURE_HEAD = '''"""fixture task — maps to NASA Technology Taxonomy TX01 (Propulsion).
No NASA affiliation or endorsement. Not financial or legal advice."""
import hashlib
import json

MAX_ITER = 8
ROUND_DECIMALS = 6
{waivers}

def _sign_safe_zero(obj):
    # non-recursive: a JSON round-trip with a parse hook normalizes -0.0
    return json.loads(json.dumps(obj),
                      parse_float=lambda t: 0.0 if float(t) == 0.0 else float(t))
'''

_FIXTURE_TAIL = '''

def canonical_json(result):
    return json.dumps({canon}, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True)


def output_hash(result):
    return hashlib.sha256({hashed}.encode("utf-8")).hexdigest()
'''

_COMPLIANT_COMPUTE = '''
def compute():
    total_kg = 0.0
    for i in range(4):
        total_kg += 2.5
    n = 0
    while n < MAX_ITER:
        n += 1
    assert total_kg == 10.0, "conservation: four 2.5 kg lots sum to 10 kg"
    assert n == MAX_ITER, "bound: the loop ran exactly MAX_ITER times"
    return {{"task_id": "task-{num}-fixture", "inputs": {{"lot_mass_kg": 2.5,
            "seed": 42}}, "results": [{{"step_index": i,
            "mass_kg": round(2.5 * (i + 1), ROUND_DECIMALS)}}
            for i in range(4)],
            "summary": {{"total_mass_kg": round(total_kg, ROUND_DECIMALS),
                        {extra_summary}}}}}
'''


def _write_fixture(tmp, num, compute_src=None, waivers_src="",
                   canon="_sign_safe_zero(result)",
                   hashed="canonical_json(result)", extra_summary="",
                   head=None):
    body = ((head if head is not None else _FIXTURE_HEAD)
            .format(waivers=waivers_src)
            + (compute_src if compute_src is not None
               else _COMPLIANT_COMPUTE).format(num="9002",  # the bound id
                                               extra_summary=extra_summary)
            + _FIXTURE_TAIL.format(canon=canon, hashed=hashed))
    path = os.path.join(tmp, f"task_{num}_fixture.py")
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)
    return path


def _selftest() -> int:
    import shutil
    print("=== protocol/task_law_check.py self-test (temp files only) ===")
    print("The law binds by registration era, never by name; every violation")
    print("is refused by name; the 18 grandfathered modules are skipped.\n")
    root_before = set(os.listdir(_REPO_ROOT))
    proto_before = set(os.listdir(_PROTO_DIR))
    checks = []
    quiet = lambda *a, **k: None
    tmp = tempfile.mkdtemp(prefix=f"task_law_selftest_{os.getpid()}_")
    try:
        mip_dir = os.path.join(tmp, "mip")
        os.makedirs(mip_dir)
        with open(os.path.join(mip_dir, "MIP-0008-fixture.md"), "w") as f:
            f.write("# MIP-0008 — fixture\n")
        # fixture chain: idx0 genesis; idx1 references task-9001 (pre-law);
        # idx2 the accepted MIP-0008 decision; idx3 references task-9002
        entries = [
            {"index": 0, "payload": {"event": "ledger_genesis"}},
            {"index": 1, "payload": {"event": "self_recompute_result",
                                     "task_id": "task-9001"}},
            {"index": 2, "payload": {"event": "mip_decision_recorded",
                                     "mip_id": "MIP-0008",
                                     "decision": "accepted"}},
            {"index": 3, "payload": {"event": "agent_verifier_attestation",
                                     "task_ids": ["task-9002"]}},
        ]
        pending = entries[:2]

        # [1] era mechanics
        checks.append(("law index read from the chain (idx 2)",
                       law_index(entries) == 2 and law_index(pending) is None))
        checks.append(("pre-law reference => grandfathered (idx 1 < law 2)",
                       era_status(entries, "task-9001")[0] == "grandfathered"))
        checks.append(("post-law reference => bound (idx 3 >= law 2)",
                       era_status(entries, "task-9002")[0] == "bound"))
        checks.append(("never-referenced => bound even while the law is pending",
                       era_status(pending, "task-9003")[0] == "bound"
                       and era_status(pending, "task-9001")[0]
                       == "grandfathered"))

        # [2] a compliant synthetic task passes every rule
        good = _write_fixture(tmp, "9002")
        v = check_module(good, "task-9002", entries, mip_dir=mip_dir)
        checks.append(("compliant synthetic task passes (bound, 0 violations)",
                       v["evaluated"] and v["passed"]
                       and len(v["rules"]) == 9))

        # [3] the same file, grandfathered, is skipped by era — not by name
        v_gf = check_module(good, "task-9001", entries, mip_dir=mip_dir)
        checks.append(("grandfathered module skipped by era (rules not run)",
                       not v_gf["evaluated"] and v_gf["rules"] == []
                       and v_gf["passed"]))

        def _violates(name, **kw):
            p = _write_fixture(tmp, kw.pop("num"), **kw)
            vv = check_module(p, "task-9002", entries, mip_dir=mip_dir)
            return (not vv["passed"]) and name in vv["violations"]

        # [4] each violation refused by name
        no_assert = _COMPLIANT_COMPUTE.replace(
            '    assert total_kg == 10.0, "conservation: four 2.5 kg lots '
            'sum to 10 kg"\n', "")
        checks.append(("missing assertions refused by name (R1)",
                       _violates("R1 assertion density", num="9101",
                                 compute_src=no_assert)))
        unbounded = _COMPLIANT_COMPUTE.replace("while n < MAX_ITER:",
                                               "while n < 8 + len([]) and True:")
        unbounded = _COMPLIANT_COMPUTE.replace(
            "    while n < MAX_ITER:\n        n += 1\n",
            "    while True:\n        n += 1\n        if n == 8:\n"
            "            break\n")
        checks.append(("unbounded loop refused by name (R2)",
                       _violates("R2 bounded loops", num="9102",
                                 compute_src=unbounded)))
        recursive = _COMPLIANT_COMPUTE + '''

def _depth(obj):
    if isinstance(obj, dict):
        return 1 + max([_depth(v) for v in obj.values()] + [0])
    return 0
'''
        checks.append(("unwaivered recursion refused by name (R3)",
                       _violates("R3 recursion by waiver", num="9103",
                                 compute_src=recursive)))
        waiver = ('P10_WAIVERS = [{"rule": "recursion", "function": "_depth", '
                  '"bound": "structure depth of the module\'s own result", '
                  '"justification": "walks a finite dict", '
                  '"approved_by": "MIP-0008"}]')
        vw = check_module(_write_fixture(tmp, "9104", compute_src=recursive,
                                         waivers_src=waiver),
                          "task-9002", entries, mip_dir=mip_dir)
        checks.append(("waivered recursion passes (bound + justification + "
                       "MIP cited)", vw["passed"]))
        bad_waiver = waiver.replace("MIP-0008", "MIP-4242")
        checks.append(("waiver citing a non-existent MIP refused (R3)",
                       _violates("R3 recursion by waiver", num="9105",
                                 compute_src=recursive,
                                 waivers_src=bad_waiver)))
        checks.append(("unitless numeric field refused by name (R4)",
                       _violates("R4 units in names", num="9106",
                                 extra_summary='"total": 10.0')))
        explicit = check_module(
            _write_fixture(tmp, "9107",
                           extra_summary='"reading": {"unit": "kg", '
                                         '"value": 10.0}'),
            "task-9002", entries, mip_dir=mip_dir)
        checks.append(("explicit `unit` sibling field satisfies R4",
                       explicit["passed"]))
        three_key = _COMPLIANT_COMPUTE.replace(
            '"summary": {{"total_mass_kg"', '"extra": {{"total_mass_kg"')
        checks.append(("contract breach — not the four-key dict (C1)",
                       _violates("C1 four-key result", num="9108",
                                 compute_src=three_key)))
        checks.append(("contract breach — canonical not sign-of-zero-free (C2)",
                       _violates("C2 canonical rules", num="9109",
                                 canon="result")))
        checks.append(("contract breach — unrounded float (C3)",
                       _violates("C3 rounding boundary", num="9110",
                                 extra_summary='"pi_rad": 3.14159265358979')))
        checks.append(("contract breach — output_hash not over canonical (C4)",
                       _violates("C4 output hash", num="9111",
                                 hashed='json.dumps(result)')))
        checks.append(("contract breach — docstring lacks TX tag/disclaimers "
                       "(C5)",
                       _violates("C5 docstring tags", num="9112",
                                 head=_FIXTURE_HEAD.replace(
                                     "TX01 (Propulsion).\nNo NASA affiliation "
                                     "or endorsement. ", ""))))

        # [5] the real registry: all 18 grandfathered, skipped by era
        real = check_registry(echo=quiet)
        n_gf = sum(1 for v in real["verdicts"] if v["era"] == "grandfathered")
        checks.append(("real registry: every pre-law module grandfathered by "
                       "era and skipped (18)", n_gf == 18))
        checks.append(("real registry: C6 holds over the bound set (none "
                       "today; forward-only)", real["registration_findings"] == []))
        c6 = registry_completeness([("task-9999", "task_9999_fixture.py")])
        checks.append(("C6 names every missing registration point for an "
                       "unregistered id (4)",
                       len(c6) == 1 and c6[0].count("'") == 8))
        checks.append(("real registry: TASK-LAW CLEAN", real["clean"]))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    stray_root = sorted(set(os.listdir(_REPO_ROOT)) - root_before)
    stray_proto = sorted(set(os.listdir(_PROTO_DIR)) - proto_before)
    checks.append(("no stray files in repo root", not stray_root))
    checks.append(("no stray files in protocol/", not stray_proto))

    print("--- self-test invariants ---")
    failures = 0
    for name, passed in checks:
        print(f"{name:68s}: {'PASS' if passed else 'FAIL'}")
        failures += not passed
    ok = failures == 0
    print("\n=== self-test summary: " +
          ("ALL CHECKS BEHAVED CORRECTLY" if ok else "FAILURE — see above")
          + " ===")
    return 0 if ok else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="MIP-0008 / MIP-0009 task-law checker (research-stage, "
                    "ZERO-VALUE, no token): lints every registered task "
                    "module; grandfathered modules are skipped by "
                    "registration era, never by name.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true",
                      help="lint the whole registry (CI mode; non-zero on "
                           "any violation)")
    mode.add_argument("--module", metavar="PATH",
                      help="lint one module file (requires --task-id)")
    mode.add_argument("--selftest", action="store_true",
                      help="fixture self-test (temp files only)")
    parser.add_argument("--task-id", metavar="task-NNNN",
                        help="with --module: the short id being registered")
    parser.add_argument("--ledger", default=None,
                        help="ledger source (default: live ledger if present, "
                             "else the published snapshot)")
    args = parser.parse_args(argv)
    if args.check:
        entries = _read_ledger(args.ledger or resolve_ledger_path())
        return 0 if check_registry(entries)["clean"] else 1
    if args.module:
        if not args.task_id:
            parser.error("--module requires --task-id")
        entries = _read_ledger(args.ledger or resolve_ledger_path())
        v = check_module(os.path.abspath(args.module), args.task_id, entries)
        print(json.dumps(v, indent=1, sort_keys=True))
        return 0 if v["passed"] else 1
    return _selftest()


if __name__ == "__main__":
    sys.exit(main())
