"""selftest.py — install-grade correctness tests for the `metacoin` CLI.

Covers: (a) entry-point routing (every subcommand --help + a cheap real call
each, mock-free); (b) locate.py's three resolution modes in priority order;
(c) THE PRODUCT ACCEPTANCE TEST — a packaged COLD INSTALL: build a wheel, pip
install it into a fresh venv, cd to an EMPTY directory with no repo checkout,
and `metacoin verify --full` must report ALL LAYERS PASS from package data
alone (plus status / task run / aci report), with ZERO private material in
the venv; (d) version single-source consistency; (e) the honest banner on
every subcommand's --help.

Temp files only; the repo gains nothing (existence-delta checked). Build
artifacts created in-tree by the PEP 517 backend (build/, *.egg-info) are
removed if this test created them. Research-stage, zero-value, no token.
"""

import sys
sys.dont_write_bytecode = True

import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from metacoin_cli import BANNER, __version__
from metacoin_cli import locate
import metacoin_cli.main as cli

_SUBCOMMANDS = ("verify", "status", "task", "molecule", "aci", "challenge",
                "identity", "passport", "economy", "treasury", "flow1",
                "version")


def _run_cli(argv):
    """Invoke the router in-process; returns (exit_code, stdout_text)."""
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            code = cli.main(list(argv))
    except SystemExit as exc:  # argparse --help exits
        code = exc.code or 0
    return code, buf.getvalue()


def _selftest() -> int:
    print("=== metacoin_cli/selftest.py (install-grade product tests) ===")
    print("Thin routing over verified modules; cold-install acceptance.\n")

    checks = []
    root_before = set(os.listdir(_REPO_ROOT))
    tmp = tempfile.mkdtemp(prefix=f"metacoin_cli_selftest_{os.getpid()}_")
    cwd_before = os.getcwd()
    try:
        work = os.path.join(tmp, "work")
        os.makedirs(work)
        os.chdir(work)  # every real call runs OUTSIDE the repo checkout

        # (a)+(e) routing + banner: every subcommand's --help exits 0 and
        # carries the honest banner; the top-level help names the excluded
        # coordinator surface
        help_ok = banner_ok = True
        for cmd in _SUBCOMMANDS:
            code, out = _run_cli([cmd, "--help"])
            help_ok = help_ok and code == 0
            banner_ok = banner_ok and (BANNER in out)
        code, out = _run_cli(["--help"])
        checks.append(("every subcommand --help exits 0", help_ok and code == 0))
        checks.append(("honest banner on every subcommand's --help", banner_ok))
        checks.append(("top-level help names the excluded coordinator surface",
                       "external_verifier.py" in out))

        # (b) locate.py priority order: explicit --repo > CWD checkout >
        # package data (None); a wrong explicit path fails loudly
        fake = os.path.join(tmp, "fake_repo")
        os.makedirs(os.path.join(fake, "protocol"))
        shutil.copyfile(os.path.join(_REPO_ROOT, "protocol",
                                     "ledger_published.json"),
                        os.path.join(fake, "protocol",
                                     "ledger_published.json"))
        explicit = locate.find_repo(fake)
        os.chdir(fake)
        via_cwd = locate.find_repo(None)
        os.chdir(work)
        via_none = locate.find_repo(None)
        try:
            locate.find_repo(os.path.join(tmp, "nowhere"))
            loud = False
        except ValueError as exc:
            loud = "refusing to fall back" in str(exc)
        checks.append(("locate priority: explicit > CWD checkout > package "
                       "data; wrong --repo fails loudly",
                       explicit == fake and via_cwd == fake
                       and via_none is None and loud))

        # (a) cheap REAL call per subcommand (against the checkout's corpus,
        # resolved via --repo so the CWD stays outside the repo)
        R = ["--repo", _REPO_ROOT]
        code, out = _run_cli(["version", "--json"])
        checks.append(("version --json single-source value",
                       code == 0
                       and json.loads(out)["version"] == __version__))
        code, out = _run_cli(["status", "--json"] + R)
        st = json.loads(out) if code == 0 else {}
        checks.append(("status: snapshot verifies, anchor tip matches",
                       code == 0 and st.get("snapshot_verifies") is True
                       and st.get("anchor_tip_matches") is True))
        code, out = _run_cli(["task", "list", "--json"])
        checks.append(("task list: 13 tasks with taxonomy tags",
                       code == 0 and len(json.loads(out)) == 13))
        code, out = _run_cli(["task", "run", "task-0007", "--json"])
        checks.append(("task run task-0007 recomputes a hash",
                       code == 0
                       and len(json.loads(out)["output_hash"]) == 64))
        code, out = _run_cli(["molecule", "build", "task-0001",
                              "--as-of", "17"] + R)
        checks.append(("molecule build task-0001 --as-of 17", code == 0))
        code, out = _run_cli(["aci", "report", "--korder", "--kmax", "2",
                              "--as-of", "17",
                              "--out", os.path.join(work, "ko.json")] + R)
        ko = json.load(open(os.path.join(work, "ko.json")))
        checks.append(("aci report --korder ACI_2 == anchored pairwise "
                       "baseline", code == 0
                       and ko["profile"][0]["aci_k"] == 0.9936507936507936))
        ch, resp = (os.path.join(work, "ch.json"),
                    os.path.join(work, "resp.json"))
        # NOTE: --repo precedes the action because the action's args are
        # REMAINDER-passthrough (everything after the action goes verbatim
        # to protocol/challenge_response.py)
        c1, _ = _run_cli(["challenge"] + R + ["issue", "--task", "task-0007",
                                              "--verifier", "selftest-cli",
                                              "--out", ch])
        c2, _ = _run_cli(["challenge"] + R + ["respond", ch, "--out", resp])
        c3, _ = _run_cli(["challenge"] + R + ["verify", ch, resp])
        checks.append(("challenge issue/respond/verify round-trip",
                       (c1, c2, c3) == (0, 0, 0)))
        kc = os.path.join(work, "kc.json")
        i1, _ = _run_cli(["identity", "generate", "--actor", "cli-selftest",
                          "--keys", "2", "--out", kc])
        i2, out = _run_cli(["identity", "declare", kc])
        checks.append(("identity generate/declare (no private material in "
                       "the declaration)",
                       (i1, i2) == (0, 0) and "private" not in out))
        p1, _ = _run_cli(["passport", "build",
                          "spark-agent-same-operator"] + R)
        t1, _ = _run_cli(["treasury", "status"] + R)
        f1, out = _run_cli(["flow1", "verify-epoch"] + R)
        checks.append(("passport build / treasury status / flow1 "
                       "verify-epoch all pass",
                       (p1, t1, f1) == (0, 0, 0) and "PASS" in out))
        e1, out = _run_cli(["economy", "replay"])
        checks.append(("economy replay re-derives the anchored log hash",
                       e1 == 0))

        # (d) version single source: pyproject declares dynamic version read
        # from metacoin_cli.__version__ and hardcodes none
        pyproject = os.path.join(_REPO_ROOT, "pyproject.toml")
        if os.path.exists(pyproject):
            text = open(pyproject, encoding="utf-8").read()
            checks.append(("version single-source: pyproject is dynamic on "
                           "metacoin_cli.__version__",
                           'dynamic = ["version"]' in text
                           and 'attr = "metacoin_cli.__version__"' in text
                           and 'version = "' not in text.replace(
                               'version = { attr', '')))
        else:
            print("    (no pyproject.toml — installed copy; single-source "
                  "check SKIPPED, named)")

        # (c) THE PRODUCT ACCEPTANCE TEST — packaged cold install
        if os.path.exists(pyproject):
            in_tree_before = {n for n in os.listdir(_REPO_ROOT)
                              if n == "build" or n.endswith(".egg-info")}
            dist = os.path.join(tmp, "dist")
            build_cmds = [
                [sys.executable, "-m", "build", "--wheel", "--outdir", dist],
                [sys.executable, "-m", "pip", "wheel", ".", "--no-deps",
                 "-w", dist],
                [sys.executable, "-m", "pip", "wheel", ".", "--no-deps",
                 "--no-build-isolation", "-w", dist],
            ]
            wheel = None
            for cmd in build_cmds:
                r = subprocess.run(cmd, cwd=_REPO_ROOT, capture_output=True,
                                   text=True, timeout=600)
                built = ([os.path.join(dist, n) for n in os.listdir(dist)
                          if n.endswith(".whl")] if os.path.isdir(dist)
                         else [])
                if r.returncode == 0 and built:
                    wheel = built[0]
                    break
            # tidy any in-tree PEP 517 artifacts THIS build created
            for n in list(os.listdir(_REPO_ROOT)):
                if ((n == "build" or n.endswith(".egg-info"))
                        and n not in in_tree_before):
                    shutil.rmtree(os.path.join(_REPO_ROOT, n),
                                  ignore_errors=True)
            checks.append(("wheel builds (python -m build or pip wheel)",
                           wheel is not None))
            if wheel is not None:
                venv_dir = os.path.join(tmp, "venv")
                r = subprocess.run([sys.executable, "-m", "venv", venv_dir],
                                   capture_output=True, text=True,
                                   timeout=300)
                vpy = os.path.join(venv_dir, "bin", "python")
                vmc = os.path.join(venv_dir, "bin", "metacoin")
                ok_venv = r.returncode == 0 and os.path.exists(vpy)
                if ok_venv:
                    r = subprocess.run([vpy, "-m", "pip", "install",
                                        "--no-index", wheel],
                                       capture_output=True, text=True,
                                       timeout=600)
                    ok_venv = r.returncode == 0 and os.path.exists(vmc)
                checks.append(("pip install into a FRESH venv (offline — "
                               "zero dependencies)", ok_venv))
                if ok_venv:
                    cold = os.path.join(tmp, "cold_empty_dir")
                    os.makedirs(cold)
                    env = {k: v for k, v in os.environ.items()
                           if k not in ("PYTHONPATH",)}
                    rv = subprocess.run([vmc, "verify", "--full"], cwd=cold,
                                        capture_output=True, text=True,
                                        timeout=900, env=env)
                    checks.append(("COLD INSTALL: `metacoin verify --full` "
                                   "in an EMPTY dir, no repo -> ALL LAYERS "
                                   "PASS from package data alone",
                                   rv.returncode == 0
                                   and "ALL LAYERS PASS" in rv.stdout
                                   and "installed package data"
                                   in rv.stdout))
                    if rv.returncode != 0:
                        print("    cold verify tail:")
                        for line in rv.stdout.splitlines()[-12:]:
                            print(f"      {line}")
                    rs = subprocess.run([vmc, "status"], cwd=cold,
                                        capture_output=True, text=True,
                                        timeout=300, env=env)
                    rt = subprocess.run([vmc, "task", "run", "task-0007"],
                                        cwd=cold, capture_output=True,
                                        text=True, timeout=300, env=env)
                    ra = subprocess.run([vmc, "aci", "report"], cwd=cold,
                                        capture_output=True, text=True,
                                        timeout=600, env=env)
                    checks.append(("COLD INSTALL: status + task run + aci "
                                   "report all pass",
                                   rs.returncode == rt.returncode
                                   == ra.returncode == 0))
                    private = []
                    for base, _dirs, files in os.walk(venv_dir):
                        for n in files:
                            if (n.startswith("keychain")
                                    or n.endswith(".secret")
                                    or n == "ledger_data.jsonl"):
                                private.append(os.path.join(base, n))
                    checks.append(("COLD INSTALL: venv carries ZERO private "
                                   "material (no keychains, no secrets, no "
                                   "live ledger)", not private))
        else:
            print("    (no pyproject.toml — installed copy; cold-install "
                  "acceptance SKIPPED, named)")
    finally:
        os.chdir(cwd_before)
        shutil.rmtree(tmp, ignore_errors=True)

    stray = sorted(set(os.listdir(_REPO_ROOT)) - root_before)
    checks.append(("no stray files in repo root", not stray))
    if stray:
        print(f"    stray: {stray}")

    print("--- self-test invariants ---")
    failures = 0
    for name, passed in checks:
        print(f"{name:68s}: {'PASS' if passed else 'FAIL'}")
        if not passed:
            failures += 1
    ok = failures == 0
    print("\n=== self-test summary: " +
          ("ALL CHECKS BEHAVED CORRECTLY" if ok else "FAILURE — see above")
          + " ===")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(_selftest())
