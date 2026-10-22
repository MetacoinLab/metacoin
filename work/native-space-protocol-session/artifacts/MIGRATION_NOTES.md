# Migration notes (order 2026-10-22)

Migrations added: `042_native_protocol` (actor role; native_domains, native_peers, native_assets, native_accounts, native_intents, native_obligations, native_allocations, native_envelopes, native_tombstones, native_disputes, native_receipts), `043_native_authority_keys` (authority key history for local and pinned peer keys; indexes). Ids were chosen by inspecting `metacoin_service/db.py` (041 was the highest).

Forward only. Historical artifacts, receipts and journal entries are untouched; readers are versioned by schema ids inside the records. No historical balance is reinterpreted as a native asset.

## Isolated verification (journey 40)
A read-only `sqlite3` backup of the preserved live database (schema 040, 38 jobs, 172 artifacts) was migrated in an isolated copy with `python -m metacoin_service --home <copy> migrate`, the API started on the copy, old jobs read, a native domain initialized and a transfer settled, audit and journal replay consistent, recovery state normal. The live service was not touched.

## Exact live upgrade operation (NOT performed; requires session authorization)
1. Drain: wait until `GET /api/health` on 127.0.0.1:8402 reports queued=0 and running=0.
2. Backup with keys: `.venv-service/bin/python -m metacoin_service --home ~/.local/state/metacoin-service backup <private-dir> --include-keys` and validate the manifest.
3. Stop the recorded pids only (api 2827392, worker 2827403 at the time of writing; re-read them from `ps` inside the tmux windows `metacoin-service:api` and `:worker`); never kill the tmux session or process groups.
4. `git checkout` the packaged revision in `~/projects/metacoin`; `python -m metacoin_service --home ~/.local/state/metacoin-service migrate` (expected final id `043_native_authority_keys`).
5. Restart api and worker in their tmux windows with the same commands; `GET /api/health` must report the new revision; read an old job; run the essential native smoke (`native-domain-init`, `native-actor-create`, `native-fund`, `native-transfer`, `native-audit`).
6. Rollback: restore the backup directory with `restore`; already exported allocations (none on the live service today) would remain committed at their peers; the watermark file would quarantine a rolled-back domain.
