# Migration notes (order 2026-10-23)

Migrations added, ids chosen by inspecting `metacoin_service/db.py` (043 was the highest; 044–047 free; next free 048):
`044_native_account_keys` (external/rotated/delegate key history per account), `045_claim_lineage_checkpoints_fragments`
(claim lineage and consumption, commit checkpoints, transport fragments), `046_resource_market` (offers, reservations),
`047_mission_contracts` (contracts, revisions, milestones, events). Forward only; no historical balance, receipt or journal
entry is rewritten; receipts keep verifying (intents now export their signer key id and the account key history so that
receipts and intents signed by rotated or delegate keys verify offline, a versioned addition to the export record).

## Rehearsal on a copy of prior state (journey 36, migration part: passed)
A read-only `sqlite3` backup of the preserved live database (schema `040_work_programs_pricing_challenges`, 38 jobs, 172
artifacts, 0 journal entries) was migrated in an isolated copy with `python -m metacoin_service --home <copy> migrate` to
`047_mission_contracts`; counts and the digest of all journal balances (recomputed from postings) were identical before and
after; the API started on the copy, read old jobs, initialised a native domain, answered the market, mission and operator
routes, and the journal replay was consistent. The live service was neither upgraded nor restarted (`live-untouched.json`).

## Upgrade / rollback handoff (NOT performed; requires explicit authorization)
1. Drain: `GET /api/health` on 127.0.0.1:8402 reports queued=0 and running=0.
2. Backup with keys: `.venv-service/bin/python -m metacoin_service --home ~/.local/state/metacoin-service backup <private-dir> --include-keys`; verify the manifest.
3. Stop only the recorded pids (api 2827392, worker 2827403 at the time of writing; re-read from `ps` inside tmux `metacoin-service`); never kill the tmux session.
4. `git checkout <source candidate>`; `python -m metacoin_service --home ~/.local/state/metacoin-service migrate` (expected final id `047_mission_contracts`).
5. Restart api and worker in their tmux windows with the same commands; `GET /api/health` must report the new revision; read an old job; run the smoke `native-domain`, `resources`, `missions`, `native-operator` through the CLI.
6. Optional components: start a signer (`native.signer init/serve`) and a witness (`native.witness init/serve`) and set `METACOIN_SIGNER_URL/TOKEN_FILE`, `METACOIN_WITNESS_URL/TOKEN_FILE/KEY_ID`; without them the service runs with development custody and watermark-only rollback detection, which it reports.
7. Rollback: `restore` from the backup directory; the new tables are additive, so a rollback of the code alone (older revision on the migrated database) also works for reads, but the older code does not know the 044–047 tables and must not be used to write mission or market state.
