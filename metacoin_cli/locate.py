# Copyright (c) 2023-2026 MetaCoin-Lab.
# Licensed under SML-1.0 — see LICENSE.md.
"""locate.py — resource resolution for the `metacoin` CLI.

Resolves WHERE the verification corpus (published snapshot, tip anchor, live
ledger, evidence bundle) comes from, in documented priority order:

  1. an explicit --repo PATH (must contain protocol/ledger_published.json);
  2. a repository checkout at/above the current working directory — the
     existing clone-and-run behavior, unchanged;
  3. the INSTALLED PACKAGE DATA (the default every protocol module already
     uses: paths relative to its own __file__, which in an installed
     environment is site-packages — the snapshot, anchor, and evidence bundle
     ship as package data, so a pip-installed metacoin with no checkout at
     all still fully verifies).

Mode 3 needs no path plumbing at all (module defaults already point there);
modes 1-2 are expressed by passing explicit --ledger/--snapshot/--anchor-file
arguments to the underlying modules. Research-stage, zero-value, no token.
"""

import os

_MARKER = os.path.join("protocol", "ledger_published.json")


def _is_repo_root(path: str) -> bool:
    return os.path.isfile(os.path.join(path, _MARKER))


def find_repo(explicit: str = None):
    """The resolved repo root per the priority order, or None (= use the
    installed package data via the modules' own defaults).

    Raises ValueError when an explicit --repo does not hold the corpus — a
    wrong path must fail loudly, never fall through to different data.
    """
    if explicit is not None:
        root = os.path.abspath(explicit)
        if not _is_repo_root(root):
            raise ValueError(
                f"--repo {explicit!r} does not contain {_MARKER} — not a "
                "MetaCoin checkout (the corpus lives at protocol/"
                "ledger_published.json); refusing to fall back silently")
        return root
    probe = os.path.abspath(os.getcwd())
    for _ in range(4):  # cwd and a few ancestors — enough for subdir usage
        if _is_repo_root(probe):
            return probe
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    return None


def paths_for(repo_root):
    """{snapshot, anchor, ledger, source_note} for a resolved root (None =
    package data). `ledger` is the live JSONL when present (coordinator
    checkout), else the published snapshot — mirroring verify_everything's
    source rule so every subcommand reads the same chain state."""
    if repo_root is None:
        import protocol.verify_everything as ve
        proto = os.path.dirname(os.path.abspath(ve.__file__))
        note = "source: installed package data"
    else:
        proto = os.path.join(repo_root, "protocol")
        note = f"source: repository checkout at {repo_root}"
    snapshot = os.path.join(proto, "ledger_published.json")
    anchor = os.path.join(proto, "ledger_anchor.json")
    live = os.path.join(proto, "ledger_data.jsonl")
    ledger = live if os.path.exists(live) else snapshot
    return {"snapshot": snapshot, "anchor": anchor, "ledger": ledger,
            "source_note": note}
