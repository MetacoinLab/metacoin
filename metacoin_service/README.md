# MetaCoin work service

An operational application over the Step 01 work-contract core: authenticated HTTP API,
server-rendered console, durable scientific job queue, age-encrypted private artifacts,
Ed25519-signed review decisions, an x402 sale route over real HTTP, retention, backup and
restore, and four exact decision functions on the declared energy-interval model.
Python 3.12, optional extra `service` (`pip install -e '.[service]'`, pins in
`requirements.lock`). The protocol core and the experiments remain standard-library only
and never import this package.

## Run it (loopback only)

```sh
python3 -m venv .venv-service && .venv-service/bin/pip install -r metacoin_service/requirements.lock
export PYTHONPATH=.                                   # run from the repository root
SVC=".venv-service/bin/python -m metacoin_service --home ~/.local/state/metacoin-service --provider-mode test-http"
$SVC init                                             # keys, database, principals, one-time credentials (0600)
$SVC credentials path                                 # where the bootstrap credentials are; values are never printed
$SVC serve --port 8402                                # API + console on http://127.0.0.1:8402 (tmux window 1)
$SVC worker                                           # background worker (tmux window 2)
$SVC status | health | backup DIR | restore DIR | migrate | cleanup | reviewer-key rotate|revoke ID | credentials rotate|revoke ID
```

Console: `http://127.0.0.1:8402/console/` (sign in with a bearer credential from the
bootstrap file). API schema: `/api/openapi.json`, interactive docs `/api/docs`. Client CLI:
`python -m metacoin_service.client_cli --credential-file FILE create|freeze|submit [--reuse]|poll|result|review-request|decide|export|compare|
dataset-create|datasets|workflow-create|workflow-run|run-status|run-cancel|campaign-create|campaign-status|campaign-control|services|quote|invoke|
usage|queue|workers|grants|grant-issue|grant-stop|events|lineage|share|projection|action|budget-tree|status-ops|search|reuse-lookup|artifacts`.
Agent runner (policy-limited, checkpointed): `python -m metacoin_service.agent_runner --credential-file F --policy-file P --checkpoint C plan|execute|status|follow --service KIND --inputs-file I`.
Worker options: `worker --name NAME --capabilities kind1,kind2 [--once] [--stop-file F]`; a worker refuses a database whose schema differs from its code.
Stop: `tmux kill-session -t metacoin-service` (or kill the pids in `<home>/run/`).

Development mode is plain HTTP on loopback and says so; cookies are `Secure` only when
`METACOIN_SERVICE_DEV_HTTP=0`, which is required for any non-loopback deployment (TLS).
`production` provider mode fails closed unless facilitator URL (https), network, asset,
pay-to and a credential file are all configured.

## Feature ledger

| User operation | Entry point | Persistence | Auth / authz | Tests | Status | Known limits |
|---|---|---|---|---|---|---|
| Create draft, validate inputs and policy, freeze (pin), amend with lineage | `POST/PATCH /api/v1/contracts…`, console form | `contracts`, encrypted `draft_input` and `input_vault` artifacts | owner; workspace-scoped | test_01, test_15 | implemented-and-verified | drafts live in encrypted artifacts; an amendment is a new version, never an edit |
| Submit job, poll, read result, cancel, batch submit | `POST /api/v1/jobs`, `/jobs/batch`, `GET /jobs/{id}`, `/result`, `/cancel`, `/batches/{id}` | `jobs`, `attempts`, `batches`, `evidence_vault` artifacts | owner submits; result private to owner and designated reviewer | test_01, 02, 03, 03b, batch | implemented-and-verified | one job per contract version; queue limits per workspace |
| Background worker with leases, fencing, retries, cancellation, recovery | `python -m metacoin_service worker` | `jobs.lease_*`, `attempts` | worker identity; allowlisted kinds only | test_02, 03, 03b | implemented-and-verified | child process with rlimits is a limits boundary, not a sandbox for hostile code |
| Authentication, roles, sessions, CSRF, revocation, rotation, scoped credentials | bearer / `POST /api/v1/session` / `POST /api/v1/credentials` | `principals`, `credentials` (peppered hash), `sessions` | server-side; role table; scope ⊆ issuer ∩ automation set | test_04, 16, scoped | implemented-and-verified | no passwords; credentials are proof of possession, not of organizational independence |
| Encrypted private artifacts, export as ciphertext, delete payload | artifact store; `GET /artifacts/{id}/export`, `DELETE` | `artifacts` + `<home>/artifacts/*.age` | owner/designated reviewer; viewer public only | test_07, 14 | implemented-and-verified | plaintext exists in memory and in the worker pipe; unlink is not secure erasure; capability error without keys |
| Signed review decision, verification against trust table, rotation/revocation | `GET /reviews/{job}/evidence`, `POST /reviews/{job}/decision`, `POST /reviews/verify` | `reviews`, `reviewer_keys`, `<home>/keys/reviewer-*.ed25519` | designated reviewer only; server-managed key custody | test_05_06, 06b | implemented-and-verified | custodial signing (stated); signatures authenticate issuer and statement, not scientific truth |
| Bounded payment action (agent buys compute), dry run, reconcile, budget view | `POST /api/v1/actions`, `/actions/{job}/reconcile`, `GET /budget`, console | experiments journal (`journal.sqlite`), `payment_actions`, `<home>/buyer.sqlite` | owner; reviewer/viewer read budget | test_12, 12b, 13, ProductionBuyerTests (4) | implemented; production buyer implemented and tested against this service's own sale route with real EIP-3009 signatures (throwaway unfunded key) | production configuration and credential absent here (`buyer.status()` names them); no external resource, facilitator or chain contacted |
| Sell an accepted result over x402 (customer pays the service) | `GET /api/v1/x402/jobs/{job}/public-bundle` (402 → PAYMENT-SIGNATURE → 200) | `sales` | unauthenticated route; bindings enforced at the SDK hook | X402SocketTests 10, 11 | SDK/transport-tested over TCP with a facilitator double; production client path real but unexercised | no external settlement observed; identifier/extra/resource are unsigned metadata enforced server-side |
| Safe-runtime calculator, plan comparison, task selection | contract kinds `safe_runtime`, `plan_comparison`, `task_selection` via API, worker, console, client CLI | evidence vault per job | as any job | test_08, 09, optimizer | implemented-and-verified with independent exact references | conditional on the declared interval model; not a hardware guarantee |
| Append-only history with hash chain; public projection | `GET /jobs/{id}/history`, `GET /history`, console pages | `events` | history:read; viewer sees only own-workspace, non-private refs | test_02, 04 | implemented-and-verified | chain detects accidental alteration only |
| Retention deadline and cleanup, explicit deletion | `cleanup` command, `DELETE /artifacts/{id}` | `artifacts.retention_deadline/deleted_at` | owner / operator | test_14 | implemented-and-verified | reviewers who already received plaintext cannot be made to forget it |
| Backup, restore into a fresh location, migrations, status, health | operator commands | SQLite backup API; manifest declares contents | operator (local) | test_13 | implemented-and-verified | keys excluded by default; restore sets a reconciliation gate; no automatic resubmission |
| Saved templates; run comparison across a template; compare two runs | `/api/v1/templates…`, `GET /jobs/{a}/compare/{b}` | `templates`, `contracts.template_id` | owner writes; viewer permitted differences only | templates, comparison | implemented-and-verified | templates never hold inputs |
| Console journey | `/console/…`, stylesheet at `/console/static/console.css` | same services | session cookie + CSRF | test_17 + `tests/browser/journey.py` (32) + `journey2.py` (23) in headless Chromium 153 via Playwright 1.63 | implemented-and-verified in a real browser at 1280 px and 400 px | screenshots in the delivery; Firefox not exercised |

## Workflows, services and agents (order 2026-09-24)

| User operation | Entry point | Persistence | Auth / authz | Tests | Status | Known limits |
|---|---|---|---|---|---|---|
| Time-dependent energy feasibility (`temporal-energy/v1`): exact envelopes with saturation, first uncertain / infeasible boundary, spill bounds, dominant uncertainty, refinement | contract kind `temporal_energy`; service `temporal-energy`; workflow node `temporal_energy` | evidence vault | as any job | test_temporal (10) incl. per-second exact reference and exhaustive tiny domain | implemented-and-verified | declared interval model, 512 segments; not a calibrated battery |
| Bounded CSV/JSON datasets, immutable versions, retire, lineage (PROV mapping, PROV-JSON export) | `POST/GET /api/v1/datasets…`, `GET /lineage/{type}/{id}[/prov.json]`, console Datasets | `datasets`, `dataset_versions`, `lineage_edges`, encrypted payloads | owner writes; readers see commitments, not rows | test_workflows, test_extras | implemented-and-verified | integers only, formulas refused, 512 rows / 1 MiB |
| Typed workflow DAG: dataset → services → review gate → export; conditions; cancellation; admission estimate; run detail | `POST /workflows`, `POST /workflows/{id}/runs` (preview), `GET /runs/{id}`, `/advance`, `/cancel`, console Workflows / run page | `workflow_definitions`, `workflow_runs`, `workflow_nodes` | owner starts; viewer reads projections | test_workflows (5), journeys 1/8, browser | implemented-and-verified | node types are installed kinds; scheduler tick runs in the worker |
| Scientific campaigns: deterministic grids, adaptive bisection (monotone axes only), pause/resume/cancel, restart-safe, Pareto with interval dominance, refinement, CSV and SVG | `POST /campaigns` (preview), `/campaigns/{id}/{run|pause|resume|cancel}`, `/results[.csv]`, `/plot.svg`, `/pareto`, console Campaigns | `sci_campaigns`, `sci_campaign_candidates` | owner | test_campaigns (4), journey 2 | implemented-and-verified | grid bounded by application limits |
| Service catalog with separate status facts, bound quotes, bazaar discovery, priced invocation over x402, compatibility preview | `GET /services`, `/services/{id}/validate|quote|x402-discovery|compatibility`, `POST /quotes/{id}/accept`, `POST /services/{id}/invoke` (simulation) / `POST /x402/services/{id}/invoke` (paid), console Services | `services`, `quotes`, `invoke_sales` | owner/agent; quote bound to principal, request digest, revision, expiry, provider mode | test_catalog (3, TCP with the installed SDK), journey 3 | SDK/transport-tested with a local facilitator double | no external settlement; `upto` scheme not wired (SDK has it; the double lacks Permit2) |
| Signed usage statements per completed evaluation; reused results meter at zero | `GET /usage`, `/usage/{id}`, console Usage | `usage_records` (UNIQUE per job) | budget:read | test_catalog, test_reuse_sharing | implemented-and-verified | assessed charge is not settlement |
| Agent grants: immutable policy bound to a scoped credential, server-side guard on every mutation, conservative counters, stop/revoke, simulation; checkpointed agent runner | `POST/GET /agents/grants`, `/stop`, `/revoke`, `/simulate`, console Agents; `agent_runner` | `policy_grants` | owner issues (cannot exceed self); agent cannot widen | test_agents (5), test_agent_paid (1: pays over x402 in test-http, re-presents a checkpointed payment identifier after interruption), journey 4 | implemented-and-verified | exposure is checked against the policy before anything is signed |
| Hierarchical budgets workspace → run → node: atomic reservation along the chain, commit on success, release on failure/cancel, explained refusals, preview | `GET /budgets/tree`, `POST /budgets/preview`, `PUT /budgets/workspace`; run view `budget` | `budget_nodes`, `budget_reservations` | owner sets; budget:read views | test_budgets (2), journey 5 (two worker processes) | implemented-and-verified | governs workflow-run jobs; the economic journal stays the hard cap for actions |
| Workers with capabilities, heartbeats, draining, fair deterministic scheduling, queue view with waiting reasons, persisted quotas | `GET /queue`, `/workers`, `POST /workers/{id}/drain|resume`, `GET/PUT /quotas`, console Queue | `workers`, `quotas` | admin drains; job:read views | test_scheduling (3), test_failures_workflow (killed worker, fencing) | implemented-and-verified | fair share per submitter; no resource-aware placement |
| Events: cursor polling and SSE with ids, resume and filters | `GET /events`, `GET /events/stream` | `events` | history:read | test_events (TCP) | implemented-and-verified | bounded stream lifetime; reconnect with `Last-Event-ID` |
| Explicit result reuse keyed by inputs digest + verifier digest | `GET /reuse/lookup`, `POST /jobs {reuse:true}` | `result_cache`, `jobs.reused_from` | job:submit | test_reuse_sharing, journey 7 | implemented-and-verified | no review or payment entitlement is implied |
| Selective sharing: allowlisted projection per grantee, signed bundle, verification | `POST/GET /jobs/{id}/shares`, `DELETE /shares/{id}`, `GET /jobs/{id}/projection`, `POST /projections/verify` | `shares` | owner grants; grantee reads exactly the fields | test_reuse_sharing (canaries), journey 6 | implemented-and-verified | same-workspace principals only |
| Workflow templates: integer parameter slots (`slots` + `{slot: name}` parameters) instantiated into a new immutable definition with lineage; templates cannot run directly | `POST /workflows/{id}/instantiate` | `workflow_definitions`, `lineage_edges` | contract:create | test_extras.TemplateTests | implemented-and-verified | integer slots on service-node parameters; dataset slots stay run-time bindings |
| Campaign branching: fork a grid campaign from succeeded candidates with explicit changed assumptions; compare branch to original candidate by candidate | `POST /campaigns/{id}/branch`, `GET /campaigns/{a}/compare/{b}` | `sci_campaigns`, `lineage_edges` (`derived_from`) | owner forks (private base); viewers compare field names only | test_extras.BranchTests | implemented-and-verified | grid campaigns only; the branch re-evaluates everything |
| Scheduled local runs: explicitly enabled, bounded workflows at local times in an IANA zone (DST-aware: non-existent times skip, ambiguous times fire once), overlap policy skip/queue, per-run budget ceiling, max runs, disable control; funded kinds and templates refused | `POST/GET /schedules`, `POST /schedules/{id}/enable|disable|run-now|delete` | `schedules` (migration 011) | job:submit creates; job:cancel disables | test_extras.ScheduleTests (fake clock) | implemented-and-verified | the worker's scheduler tick starts due runs; no recurring action entitlements exist |
| Search, CSV result table, operational status, Prometheus-style metrics | `GET /search`, `/results.csv`, `/status`, `/api/metrics` | none (queries) | job:read / history:read | test_ops_search | implemented-and-verified | bounded filtering; no full-text index |

Migrations 003–011 add every table above; `migrate` applies them after a backup (`backup DIR`). A worker whose code
does not match the applied schema refuses to register (`schema_mismatch`).

## DGX compute engine (order 2026-09-24)

Compute jobs are ordinary contracts of kind `temporal_batch`, `monte_carlo_reliability` or `heat_diffusion`. The worker
runs them through `metacoin_service/compute/engine.py`: a task-owned child interpreter (`METACOIN_COMPUTE_PYTHON`, probed
by default: `/usr/bin/python3` with numpy and, on this DGX, torch 2.10+cu130 for the GB10) executes the allowlisted kernel
in bounded chunks, publishes encrypted checkpoints atomically, answers pause/cancel at chunk boundaries, and resumes
under a new fencing generation after interruption. A persisted verification phase runs before acceptance and states
which mode ran. Schema: migration 012 (`compute_runs`, `compute_checkpoints`, `compute_reservations`,
`compute_work_units`, `jobs.hold`).

| Operation | Entry point | Evidence | Status | Known limits |
|---|---|---|---|---|
| Capability facts: installed / configured / currently available / observed running | `GET /api/v1/compute/capabilities` · `compute-capabilities` · `/console/compute` | test_compute_engine, journeys | implemented-and-verified | `gpu_verified` needs a completed, verified cuda run on this instance |
| Temporal batches: thousands of scenarios (explicit list or grid) evaluated with exact int64 arithmetic on cpu (numpy) or cuda (torch); overflow bound on the host | kind `temporal_batch` · `compute-submit --kind temporal_batch` · form on `/console/compute/new` | test_compute_science (kernel = temporal-energy/v1 reference on 400 random + edge scenarios, both backends), journeys 1-2 | implemented-and-verified; cuda observed | batch shares one duration schedule; ≤ 4096 listed or ≤ 200 000 grid scenarios |
| Monte Carlo reliability: fixed sample count, indexed Philox stream, Wilson interval, point model + declared finite/uniform distributions | kind `monte_carlo_reliability` | test_compute_science (chunk invariance, per-sample reference agreement, marginals), journey 5 | implemented-and-verified | modulo mapping bias < range/2^64; probability is conditional on the declared model |
| 2-D heat diffusion (FTCS, float64, fixed Dirichlet): exact rational stability check, snapshots, field/plot export | kind `heat_diffusion` | test_compute_science (scalar reference, eigenmode factor, refinement trend), journey 6 | implemented-and-verified; cuda observed | explicit scheme only; not a physical validation |
| Checkpoints: encrypted containers, generations, retention, corrupted/foreign refusal; pause/resume/cancel; fenced recovery after worker loss | `POST /api/v1/compute/jobs/{id}/pause|resume|cancel`, `GET …/checkpoints` · `compute-pause|resume|cancel|watch` · job page | test_compute_engine, journeys 3-4 | implemented-and-verified | resume replays the uncommitted chunk (never billed twice) |
| Device reservations and slots (gpu 1, cpu 2 by default), waiting reasons, gpu-required waits without a cuda worker | `GET /api/v1/queue` | test_compute_engine, journey 7 | implemented-and-verified | slots are application reservations, not a hardware sandbox |
| Telemetry: child CPU/RSS from /proc, device-wide nvidia-smi readings, NVML total-energy counter delta | run view (owner) | journeys 2 | implemented-and-verified | energy is device-wide; memory unsupported on GB10 |
| Useful-work accounting: quotes bound to work units, usage = committed units | `POST /api/v1/services/{id}/quote`, `GET /api/v1/usage` | test_compute_engine, journey 9 (x402) | implemented-and-verified | fixed-price bundles; `upto` unsupported by the local double |
| Private outputs (npy/json), reproducibility bundle, heat SVG; workflow nodes and campaign candidates | `GET …/outputs[/name]`, `…/reproducibility`, `…/plot.svg` | test_compute_integration, journeys 8, 12 | implemented-and-verified | outputs readable by owner and assigned reviewer only |

Verify: `PYTHONPATH=. python3 -m unittest metacoin_service.tests.test_compute_science` (kernels, needs numpy/torch) and
`PYTHONPATH=. .venv-service/bin/python -m unittest metacoin_service.tests.test_compute_engine metacoin_service.tests.test_compute_integration`.
Journeys: `python -m metacoin_service.tests.journeys_compute`. Benchmarks: `python -m metacoin_service.benchmark_compute [--engine]`.

## 24-hour expansion (order 2026-09-24, executed 2026-09-27)

Seven connected groups on the same application: trusted local models, private knowledge with citations, calibration,
independent verification, worker-node federation, MCP, and the variable-price protocol gap. Schema: migrations 014-019.
Every operation is authorized server-side; model output is data and never widens a grant, signs a review or changes a
numerical result. Optional tracing: `METACOIN_TRACING=1` (+ `METACOIN_TRACE_FILE`) writes local JSON-line spans.

| Group | Operation | Entry point | Evidence | Status | Known limits |
|---|---|---|---|---|---|
| A models | Registry of pinned safetensors revisions (hub repo + 40-hex commit, weight/tokenizer digests, allowlisted loaders, license), install check, promote/rollback default, retire/revoke | `POST/GET /api/v1/models`, `POST /api/v1/models/{id}/promote|recheck|retire|revoke|load|unload` · `models`, `model-register`, `model-action` · `/console/models` | test_models, journey 1 | implemented-and-verified | single-file safetensors; architectures Qwen2/Llama (generate), BERT (embed); weights installed under `METACOIN_MODEL_STORE` by the operator or the recorded download script |
| A models | Bounded text generation as a job: task-owned transformers/torch runtime child, durable output segments with a cursor (resumable delivery), cancel, measured token usage under quotes, memory policy from `/proc/meminfo` | `POST /api/v1/models/generate`, `GET /api/v1/models/jobs/{id}[/segments|/outputs]` · `generate --watch` · models page form | test_models (real cuda generation, cancel, revoke), journey 1, 15 | implemented-and-verified | Qwen2.5-0.5B-Instruct: small model, modest quality; greedy repeatable on one device/version only; runtime child not traced |
| A models | Embeddings (mean pooling, L2-normalized, documented truncation policy) as private npy artifacts | `POST /api/v1/models/embed` · `embed` | test_models, journey 2 | implemented-and-verified | all-MiniLM-L6-v2, 256-token window; vectors never exported by default |
| B knowledge | Collections, immutable document versions (text/markdown/csv), deterministic byte-addressed chunks, encrypted index versions built by the worker, lexical/semantic/hybrid retrieval with explicit rank fusion, byte-checked citations, extractive and generative answers with insufficient-evidence outcome and lexical attribution, revocation reaching indexes/previews/cached answers | `/api/v1/knowledge/*` · `knowledge-*` · `/console/knowledge` | test_knowledge (eval corpus: recall@k 8/8, 10/10 expected statuses, injection contained), journeys 2-5 | implemented-and-verified | no PDF parser; exact search only (≤ 5000 chunks); citation validity is mechanical, not entailment |
| C calibration | Numeric and per-workspace performance datasets (censoring, units), OLS/ridge fits as verified compute jobs (numpy lstsq + pure-Python QR reference), predictions with domain status and empirical intervals, approval as an advisory scheduler signal with an operator toggle, replay, counterfactual planning | `/api/v1/calibration/*` · `calibration-*` · `/console/calibration` | test_calibration, journeys 6-7 | implemented-and-verified | linear models only; predictions advisory; freshness 30 days |
| D verification | Audit jobs: full_exact, full_reference, analytical (incl. closed-form heat eigenmode), server-challenged sampled_reference, replica on another backend; signed statements with public projection; disputes; contract-bound gate before review | `/api/v1/verification*` · `verification-*` · `/console/verification` | test_verification (corrupted results rejected; cuda replica; dispute), journeys 8-10 | implemented-and-verified | same host/operator: not organizational independence; non-compute kinds are regression checks by the same implementation |
| E federation | Deliberate node enrollment with operator limits, TLS (pinned local CA) + node credential + Ed25519 signed requests with replay guard, execution-location policy, server-side claim/lease/fencing, chunked resumable uploads, coordinator-side verification, expired-lease recovery, drain/disable/revoke/rotate | `/api/v1/nodes*`, `/node/v1/*` · `node-enroll`, `node-worker`, `serve --tls` · `/console/nodes` | test_federation, journeys 11-13 | implemented-and-verified (loopback processes) | one host; no mutual TLS; not multi-machine evidence; compute kinds only on nodes |
| F payments | x402 `upto` scheme validated on a private py-evm chain with pinned Permit2 + x402UptoPermit2Proxy + mock token: below-max settlement, over-max/recipient/spender/expiry/domain/replay refusals, lost-response reconciliation | `integrations/x402/local_chain` (build.py, harness.py, test_local_chain) · journey 14 | 6 tests | local contract behaviour verified; application metered-settlement route NOT implemented | SDK canonical addresses redirected to local deployments; not production settlement |
| G MCP | Stdio MCP server (mcp 1.26.0) over the HTTP API with a private scoped credential: discovery, validate, quote, submit, status, result, cancel, verification, models, knowledge search, usage; resources for services/schemas/job summaries/statements | `python -m metacoin_service.mcp_server` · `mcp-connection` | test_mcp (separate client process), journeys 16-17 | implemented-and-verified | stdio only; trust boundary = OS process + credential file |
| D product | Approval policies (bound content + revision, independent approver, expiry/stale/revoked/apply-failed), usage statements (JSON/CSV, grouped by asset/network/environment), tracing, console pages, CLI, backup inventory + restore-time model/index recovery report | `/api/v1/approvals*`, `/api/v1/statements[.csv]`, `/api/v1/tracing` | test_approvals_statements, test_console_expansion, journeys 18-19 | implemented-and-verified | approvals enforce configured identities only |

Verify: `PYTHONPATH=. .venv-service/bin/python -m unittest discover -s metacoin_service/tests -p 'test_*.py' -t .` (model tests need
the pinned artifacts under the model store and a torch-capable interpreter). Journeys: `python -m metacoin_service.tests.journeys_expansion`.
Local chain: `python -m integrations.x402.local_chain.build` then `python -m unittest integrations.x402.local_chain.test_local_chain`.

## Capability matrix (installed / configured / available / externally validated)

Machine-readable: `GET /api/v1/capabilities`. Three classes:

- **Working, verified locally**: authentication, sessions, scoped credentials, encryption at
  rest, signed reviews, job queue, retention, backup/restore, history, the four science
  functions, batch, templates, comparison, client CLI, console (real browser), x402 sale route
  in `test-http` (real SDK client/server over TCP; facilitator double verifies real EIP-3009
  signatures offline), production buyer adapter code path (against this service's own route).
- **Implemented, externally unverified**: production sale route with `HTTPFacilitatorClientSync`
  against an operator-configured https facilitator; production buyer against a remote resource.
  Both fail closed without configuration; neither has contacted an external party. No
  settlement observed anywhere.
- **Incomplete / not available**: nothing in the buyer adapter is a placeholder; what is missing
  is operator configuration and a credential: `METACOIN_BUYER_RESOURCE_URL`, `_KEY_FILE`
  (0600), `_NETWORK`, `_ASSET`, `_MAX_AMOUNT` (optional `_PAY_TO`). Chain-state facts (balance,
  nonce consumption) are never checked by the local double. External team participation: none.

## Payment correctness mapping (order §17)

| Event | Journal / sale state | Operator action |
|---|---|---|
| HTTP response lost after dispatch | action stays `SUBMISSION_PENDING`/`OUTCOME_UNKNOWN`; exposure retained | `POST /actions/{job}/reconcile` (never resubmits) |
| Worker interrupted | computation retried under a new lease; payment untouched (payment never starts from the worker) | none |
| Provider timeout / unknown | `OUTCOME_UNKNOWN`, exposure retained | reconcile; only an authoritative record resolves it |
| Duplicate client submission | identical request id → recorded state; other id → `ENTITLEMENT_CONSUMED`; other mode → refused | none |
| Sale settlement unknown / inconsistent | `sales.state=OUTCOME_UNKNOWN`; bundle not delivered | `POST /sales/{job}/reconcile` (test double only; production facilitator interface has no lookup) |
| Restore from backup | reconciliation gate set; nothing resumes | reconcile, then `clear-reconciliation-gate` |

Budget arithmetic is exact integers in one asset/network/unit per campaign. Exactly-once
external settlement, escrow, refunds and fair exchange are not claimed.

## Security checks performed (not an audit)

Real-browser passes (Playwright/Chromium): forbidden actions with separate identities, session
expiry, restart mid-session, failed job, private export refused on every method, anonymous
ranged requests refused, no private label in any unauthorized response.
Authorization at every sensitive route per role (test_04); mass assignment ignored
(`role`/`owner` fields); cross-workspace 404; guessed ids; revoked credentials; scoped
credentials cannot widen; CSRF on session mutations; duplicate JSON keys, floats, NaN and
oversize bodies refused; artifact names generated internally, exports are ciphertext for
private artifacts; hostile labels are escaped by Jinja autoescape; no HTTP input selects a
path, module, command or facilitator URL (provider endpoints come from operator
configuration only); private static files are not served by any web root. Not covered:
signed download URLs (not implemented), rate limiting beyond queue caps, TLS (out of scope
for loopback).
