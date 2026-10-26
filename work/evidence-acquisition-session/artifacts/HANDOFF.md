# Handoff — evidence acquisition and scoped assurance (order of 2026-10-22)

**Publication status.** Nothing from this order is on GitHub. The work is on the local branch
`evidence/acquisition-assurance` (base `825adf8`, the published `service/real-features` head whose merge is `main`
`854619a`). The owner's established publication workflow (push the branch, open a PR against `main`, CI, merge by an
authorized account) has not been run; the CI workflow is stdlib-only and does not run the service suites, which were run
here on the DGX and are recorded in this delivery.

**Live service.** Unchanged: tmux `metacoin-service`, 127.0.0.1:8402, revision `3e34126`, schema 040. Loading this
candidate there needs the established backup/restore/migrate/respawn process (`work/core-work-economy-session/live_upgrade.sh`
is the last used script; migration 041 is additive, see MIGRATIONS.json).

**To resume.** `git checkout evidence/acquisition-assurance`; the suites to run are listed in verification-results.json;
`work/evidence-acquisition-session/SESSION_STATE.md` carries the progress ledger and the open items; the optional
extensions 1–6 of the order are not started.

**What the owner decides.** Whether to publish (push + PR), whether to upgrade the live service, and whether the
optional extensions are worth their measured cost.

**Session history.** The first session (9df2f649, 06:22–07:01Z) built the kernels and the service layer and was cut off
mid test run; this session recovered the uncommitted tree, re-verified it, and completed interfaces, journeys, docs and
delivery without resetting anything.
