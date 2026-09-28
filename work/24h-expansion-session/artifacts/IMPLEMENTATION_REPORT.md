# MetaCoin 24-hour expansion: implementation report

Order: `~/CLAUDE_CODE_24_HOUR_METACOIN_EXPANSION_2026-09-24.md` (executed 2026-09-27, branch `service/real-features`).
Starting revision `3b6f292` (schema 013). Final code revision `ead4d68` (schema 025, migrations 014-025); the packaged HEAD adds only a browser-journey harness fix (f0f77ba) and session records: `git diff --stat ead4d68 HEAD -- . ':!work' ':!metacoin_service/tests/browser'` is empty. Verification results are in `verification-results.json` (suite/local chain/records/benchmark/endurance at ead4d68; the twenty journeys re-run at the packaged HEAD).
Elapsed and interruptions: see the final section.

## What a user or agent can now do (entry points that exist and were exercised)

| Group | New operations | Entry points | Exercised by | Not verified / incomplete |
|---|---|---|---|---|
| A local models | register a pinned safetensors revision (hub repo + 40-hex commit, digests, license, allowlisted loader), promote/rollback/retire/revoke, run bounded generation with durable segments and measured usage, embeddings, operator load/unload, warmup residency under a ceiling, drain before numerical compute, batching of one submitter's embedding requests | `/api/v1/models*`, `models`/`model-register`/`model-action`/`generate`/`embed`/`model-warmup`, `/console/models` | test_models, test_batching_warmup, journeys 1-2, 15, live smoke on the upgraded instance (cuda generation, cpu embedding) | quality of a 0.5B model; generation not batched |
| B private knowledge | collections, immutable document versions, byte-addressed chunks, encrypted index versions, lexical/semantic/hybrid retrieval with explicit fusion, byte-checked citations, extractive/generative answers with an honest insufficient-evidence outcome, revocation reaching indexes/previews/cached answers | `/api/v1/knowledge/*`, `knowledge-*`, `/console/knowledge` | test_knowledge (corpus: recall 8/8, 10/10 expected statuses), journeys 2-5, examples/private_answer.py | no PDF parsing; citation validity is mechanical, not entailment |
| C calibration | numeric and performance datasets, verified least-squares fits, predictions with domain status and empirical intervals, approval as an advisory scheduler signal, replay, planning, bounded design suggestions | `/api/v1/calibration/*`, `calibration-*`, `/console/calibration` | test_calibration, journeys 6-7, examples/calibrated_prediction.py | linear models only |
| D verification | full/reference/analytical/sampled/replica audits with server challenges, signed statements, disputes, contract-bound gate, policy templates, disagreement review with evidence-linked decisions | `/api/v1/verification*`, `verification-*`, `disagreements`, `/console/verification` | test_verification, test_extensions, test_bundles_disagreements, journeys 8-10, examples/audited_result.py | same operator: not organizational independence |
| E federation | deliberate node enrollment, TLS + node credential + signed requests, execution-location policy, claim/lease/fencing, chunked transfers, recovery, drain/revoke/rotate | `/api/v1/nodes*`, `/node/v1/*`, `node-enroll`, `node-worker`, `serve --tls`, `/console/nodes` | test_federation, journeys 11-13, examples/federated_execution.py | one host, loopback; no mutual TLS |
| F variable price | quote with `scheme: upto`, 402 with Permit2 upto requirements for the ceiling, verified authorization creates the job, settlement of the measured amount after completion (below the ceiling for short generations), unused authorization on failure, replay-safe, `exact` fixed-price fallback | `POST /api/v1/services/{sid}/quote {scheme}`, `POST /api/v1/x402/services/{sid}/invoke`, `GET /api/v1/x402/settlements[/{id}]` | test_local_chain (contracts), test_upto_route (application route, 2), journey 14 (separate client process) | external production settlement never exercised; SDK addresses redirected to the local deployments in test-http |
| G MCP | stdio server with a scoped credential: discovery, validate, quote, submit, status, result, cancel, verification, models, knowledge search, usage, plans; resources | `python -m metacoin_service.mcp_server`, `mcp-connection` | test_mcp, journeys 16-17, examples/mcp_bounded_job.py | stdio only |
| §51 planning | typed plan drafts, model-assisted service-kind selection (model chooses a catalog kind only), validation with machine-readable refusals, stored drafts, idempotent acceptance, grant-bound automatic execution | `/api/v1/agents/plans*`, `plan`/`plans`/`plan-accept`, MCP `create_plan`/`plan_status`/`accept_plan`, `/console/agents` | test_planner, test_planner_model | selection accuracy of the small model (see the evaluation record) |
| §52 evaluation sets | versioned agent-behaviour set with held-out items, mechanical scoring, resource use recorded; evaluation registry with promotion gate | `tests/eval_sets/agent_behavior_v1.json`, `/api/v1/evaluation/*`, `eval-*` | test_planner_model, test_evaluation | subjective quality not scored |
| §53-58 product | approvals, usage statements, tracing, console pages (Models, Knowledge, Notebooks, Calibration, Verification, Nodes, Approvals, Statement, Agents), CLI commands, five executable examples | see README ledger | test_approvals_statements, test_console_expansion, test_examples, journey 19 (Playwright) | |
| §65 backlog | 1 evaluation registry, 2 notebooks, 3 design suggestions, 4 batching, 5 warmup, 6 policy templates, 7 bundles, 8 import checks, 9 disagreement review, 10 recovery rehearsal | see README ledger | dedicated tests | all ten implemented |

## Defects found and fixed during the session (evidence in the test names)

- Single-document collections could never satisfy the fixed BM25 threshold (idf saturates at log 4/3), so extractive answers declined; informative-term coverage now counts as lexical evidence and the corpus evaluation still gives 10/10 expected statuses (test_knowledge, journey 19).
- `/api/health` reported the working tree's HEAD instead of the loaded code's revision for a long-running service; the revision is now read once at process start.
- The node worker printed a stray return value after its JSON (journey 11); backup/restore skipped the node TLS key subdirectory (journey 18).
- Journeys that submit real jobs ran without a worker after the per-journey worker change (6-9, 15, 16, 19) and the browser runner could exit without output; both fixed, and crashes are now reported as failed checks.
- The calibration console context accepted duplicate keywords (design form); the backup helper shadowed its artifact counter (rehearsal).
- Batched embeddings: usage rows exist only for quoted work, so the batching test now purchases the requests it accounts for.
- The refreshed dependency lock carried an unused `signinwithethereum` pin whose `eth-account<0.14` requirement conflicts with the `eth-tester`/`eth-account 0.14.0` set the local chain needs; a fresh install from the lock was impossible (clean export, first attempt). The pin is removed (nothing imports it); the lock now resolves offline from the wheel caches.
- Browser journey 19 failed only in the full run: with several collections on the Knowledge page the runner clicked the first collection's answer button (empty query) and waited for a redirect that never came. Direct HTTP calls showed the service route answered instantly; the click is now scoped to the collection's own form. The service code was not changed for this (harness commit f0f77ba on top of the verified ead4d68).

## Test outcomes versus implementation scope

See `verification-results.json` (generated from the final chain) and `journeys/journeys-expansion.json`. Skips are variants that need torch or the pinned
artifacts and are listed with their reasons; the model and GPU paths ran in the model-enabled suite and journeys (models_present=true) and on the
live instance. Local-chain contract behaviour, the in-process facilitator paths and external production settlement are reported as separate categories.

## What remains

- External production settlement (x402 facilitator on a public network) was never exercised; the production branch is code only.
- The small model's plan selection accuracy is limited (recorded in `eval-set-agent-behavior.json`); the evaluation reports it rather than tuning to it.
- Knowledge collections and datasets are deliberately excluded from bundles (private data).
- The TLS federation evidence is two processes on one host; multi-machine behaviour is untested.

## Live instance after the upgrade

`live-status.json` and `live-capabilities.json`: tmux session `metacoin-service` (window 0 API on 127.0.0.1:8402, window 1 `live-worker` with cpu+cuda),
provider mode test-http, schema 025, both pinned models registered and promoted, a real generation (cuda) and embedding (cpu) completed after the restart.
Backup before the migration: `~/.local/state/metacoin-service-backups/pre-migration-014-1790572008` (with keys, 0600). Start/stop/status:
`PYTHONPATH=. .venv-service/bin/python -m metacoin_service --home ~/.local/state/metacoin-service --provider-mode test-http serve --port 8402`,
`... worker --name live-worker`, `... status`; stop with SIGTERM to those two processes (task-owned). Credentials are never printed; the bootstrap file stays 0600.

## Elapsed time and interruptions

Session start 2026-09-27 20:04 MDT (order read, inventory, model download 20:07-20:20). Packaging started 2026-09-27 23:45 MDT: about 3 h 41 min of wall-clock
work for the whole order, well inside the 24-hour budget; the remaining budget was not consumed because no required or backlog item was left.
Interruptions: one context compaction (~21:40, no work lost: the session state file and git checkpoints carried the state), one operator stop
around 22:20 followed by a resume instruction (~22:27), one self-inflicted shell termination while stopping the live processes (the stop had
already taken effect; the tmux session `metacoin-service` was recreated with the same two windows). Background verification chains ran while
features were built, from worktree snapshots so that edits never changed a running run.
