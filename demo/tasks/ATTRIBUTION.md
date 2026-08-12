# Attribution and license notice for demo/tasks/

Copyright (c) 2023-2026 MetaCoin-Lab. Licensed under SML-1.0 — see
[`LICENSE.md`](../../LICENSE.md).

**Why the task source files in this directory carry no in-file copyright
header, deliberately:** every task's provenance molecule records a
`task_spec.spec_hash` — the SHA-256 of the task source file's exact bytes
(computed in `protocol/work_molecule.py` at molecule build time, and part of
each molecule's content-addressed WMID). Those WMIDs are anchored on the
ledger in every molecule-catalog generation, and every generation must keep
rebuilding **byte-identically** forever (the generation-lock rule, enforced
by `metacoin verify` on every run). Prepending even a comment to any file in
this directory changes its spec hash, which changes its WMID in any fresh
rebuild — which is exactly the tamper-detection working as designed, and
exactly why these files are edit-frozen in practice.

Copyright and the SML-1.0 license cover these files through
[`LICENSE.md`](../../LICENSE.md) and this notice, the same way they cover
every other file in the repository; the absence of an in-file header changes
nothing about their licensing. New task files added in future milestones
inherit the same rule from their first anchored record onward.
