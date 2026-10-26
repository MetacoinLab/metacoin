# Migration notes (order 2026-10-23, independent machine network)

Migrations added, ids chosen by inspecting `metacoin_service/db.py` (047 was the highest; 048–054 free; next free: 055), forward only,
additive (`MIGRATIONS.json` lists every statement): `048_resource_pools` (pools, commitments; `pool_id` on offers; interval columns on
reservations), `049_signing_requests`, `050_mission_subcontracts`, `051_claim_lots` (lots; `returned_amount` on allocations; lots
created only for unconsumed active lineage rows — never for consumed historical claims), `052_mission_products` (products,
entitlements), `053_relay_service` (routes, per-hop queues), `054_controller_generations`. No historical balance, receipt or journal
entry is rewritten; wire formats are versioned (`return_request` v2 carries `partial`, `lots`, `returned_before`; a v1 peer refuses it
with `message_fields` instead of reading it as a full return).

## Rehearsal (journey 47, passed at 1b396f3)
1. **Preserved live database** (schema `040_work_programs_pricing_challenges`, 38 jobs, 0 journal entries): read-only `sqlite3` backup into an
   isolated home, whole-snapshot copy taken before migrating, `python -m metacoin_service --home <copy> migrate` → `054_controller_generations`
   (rc 0, ten new tables); job count, journal count and the digest of all journal balances identical before and after; the API started on the
   copy (revision 1b396f3), read old jobs, initialised a native domain, answered pools / products / relay / subcontracts / missions, journal
   replay consistent. Rollback = restoring the whole pre-migration snapshot (verified: its digest equals the before-state). The live service at
   127.0.0.1:8402 was neither upgraded nor restarted (`live-untouched.json`).
2. **Genuine prior state from the base revision** (`a6d672d`, schema `047_mission_contracts`): two domains spawned from a git worktree of the base
   with its own modules, holding old accounts (alice 65 after funding, reservations and exports), a plain offer with a reserved reservation, a
   frozen unfinished mission, and two cross-domain claims at the peer (one 20-unit claim partly consumed: 13 kept, 7 spent to another account;
   one 5-unit claim untouched). After migrating both copies: balance digests identical; alice 65; the reservation still `reserved`; the legacy
   offer has no back-filled pool and remains reservable under per-offer semantics; the mission is still `frozen`; audit conserved; at the peer
   the claim balance is unchanged (18), the **untouched** old claim became a retirable lot (5, active) and a partial return of 2 succeeds, the
   **consumed** old claim got no invented lineage: partial return is refused (`missing_lineage`), it is reported `legacy_without_lineage` and
   only a full return of its remaining backing stays possible; the accounting partition check passes.

## Operator upgrade sequence (tested on copies; NOT performed on the live service — requires explicit authorization)
1. Drain: `GET /api/health` on 127.0.0.1:8402 reports queued=0 and running=0.
2. Backup with keys: `.venv-service/bin/python -m metacoin_service --home ~/.local/state/metacoin-service backup <private-dir> --include-keys`; verify the manifest; keep the whole directory as the rollback snapshot.
3. Stop only the recorded pids (re-read from `ps` inside tmux `metacoin-service`); never kill the tmux session.
4. `git checkout <source candidate>`; `python -m metacoin_service --home ~/.local/state/metacoin-service migrate` (expected final id `054_controller_generations`; it also applies 041–047 on a 040 database).
5. Restart api and worker with the same commands; `GET /api/health` must report the new revision; read an old job; smoke `native-domain`, `resources`, `pools`, `missions`, `native-operator` through the CLI.
6. Optional components: a signer per economic party (`native.signer init/serve`), a witness (`native.witness init/serve`), `METACOIN_REQUIRE_EXTERNAL_CUSTODY=1` for a custody-separated instance (no development actors can then be created).
7. Rollback: restore the whole backup directory (database + keys) from step 2 and start the previous revision; never delete new tables under a running process.
