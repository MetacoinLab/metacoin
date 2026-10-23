# Native protocol interfaces (API, CLI, console, MCP)

All operations are authorized server-side by role and permission (`native:read`, `native:transfer`, `native:export`, `native:transport`, `native:dispute`, `native:admin`); public inspection is separate from authority to spend. Strict JSON (no floats), `Idempotency-Key` on mutations, structured errors.

## HTTP API (`/api/v1`)
| Operation | Route | Permission |
|---|---|---|
| Domain init / view / connectivity / rotate | `POST GET /native/domain`, `POST /native/domain/connectivity`, `POST /native/domain/rotate` | native:admin (read: native:read) |
| Actors / accounts | `POST /native/actors`, `GET /native/accounts`, `GET /native/accounts/me`, `GET /native/accounts/{id}` | native:admin + admin:credentials / native:read |
| Development fixture funding | `POST /native/fixtures/fund` | native:admin, dev only, zero-value asset only |
| Transfer / simulate / refund | `POST /native/transfers`, `POST /native/transfers/simulate`, `POST /native/refunds` | native:transfer (holder) |
| Intents / obligations / audit / spec / export record | `GET /native/intents[/{id}]`, `GET /native/obligations[/{id}]`, `GET /native/audit`, `GET /native/spec`, `GET /native/export` | native:read |
| Peers | `POST /native/peers`, `GET /native/peers/{id}` | native:admin / native:read |
| Allocations | `POST /native/allocations/export`, `POST /native/allocations/recover`, `POST /native/allocations/{id}/resume`, `POST /native/allocations/{id}/return`, `GET /native/allocations[/{id}]` | native:export (holder) / native:read |
| Transport | `GET /native/transport/outbox`, `POST /native/transport/outbox/mark`, `POST /native/transport/inbox`, `POST /native/transport/observation`, `POST /native/transport/gc` | native:transport (gc: native:admin) |
| Recovery | `GET /native/recovery`, `POST /native/recovery/reconcile` | native:read / native:admin |
| Disputes | `POST /native/disputes`, `GET /native/disputes[/{id}]`, `POST /native/disputes/{id}/resolve` | native:dispute (party) / reviewer or admin |
| Procurement | `POST /native/procurement/plan`, `GET /native/procurement/benchmark` | work:read |
| Lunar products | `GET /lunar/sources`, `GET /lunar/examples/{name}`, `GET /lunar/products[/{job}]`, `GET /lunar/products/{job}/preview.png`, `GET /lunar/products/{job}/download` | job:read (private result by the job policy) |
| Work on native assets | the existing `/work/*` routes with a native asset id in the terms; `/work/treasury?asset=<native asset>`; `/acquisition-plans/{id}/materialize` with `asset` | unchanged permissions |

## CLI (`python -m metacoin_service.client_cli --credential-file F [--base URL] <command>`)
`native-domain native-domain-init native-accounts native-account native-actor-create (--out private file) native-fund native-transfer native-simulate native-intents native-intent native-obligations native-export native-allocations native-allocation native-allocation-return native-recover native-peer-pin native-peer native-outbox native-inbox native-bundle-write native-bundle-import native-courier native-observation native-rotate native-connectivity native-recovery native-reconcile native-audit native-export-record native-check (offline) native-vectors (offline) native-model (offline) native-spec native-disputes native-dispute-open native-dispute-resolve procurement-plan procurement-benchmark lunar-sources lunar-products lunar-product lunar-download lunar-example`. Credentials come from 0600 files; tokens never appear in arguments or output.

Offline tools (no service): `python -m metacoin_service.native.checker EXPORT --roots ROOTS`, `python -m metacoin_service.native.vectors`, `python -m metacoin_service.native.model`, `python -m metacoin_service.lunar {terrain|route|pack|reproduce|check|sources|example}`, `python -m metacoin_service.native.courier {push|write-bundle|import-bundle}`.

## Console
`/console/native` (domain: identity, custody, authority policy, connectivity, recovery; accounts; audit; allocations), `/console/native/accounts/{id}` (available, reserved, committed, in transit, disputed, earned-unsettled, pending inter-domain claims; simulate / transfer form for the holder), `/console/native/settlement` (local finality, remote acknowledgment, unresolved exposure in plain language; per-allocation explanation; return; link cut/restore), `/console/native/treasury` (fee receipts → allocations → payouts), `/console/native/missions/{award}` (deliverable, acceptance, resource envelope, maximum obligation, selected provider, native reservation), `/console/lunar[/{job}]` (product scope, provenance, preview, assumptions, verification, reproduction), `/console/native/recovery`.

## MCP (`python -m metacoin_service.mcp_server`, stdio)
Read: `native_domain_status native_account native_audit native_allocation_status native_treasury lunar_product lunar_sources`. Simulate: `native_simulate_transfer procurement_plan`. Commit: `native_export_allocation`. Spend: `native_transfer native_pay_entitlement`. Bounded authorize: `native_open_dispute`. No tool funds an account; annotations are metadata, the server enforces.

## Order 2026-10-23 additions

### HTTP API
| Operation | Route | Permission |
|---|---|---|
| External actor enrolment; key registration / revocation; keys view | `POST /native/actors/enroll`, `POST /native/accounts/{id}/keys`, `POST /native/accounts/{id}/keys/{kid}/revoke`, `GET /native/accounts/{id}/keys` | native:admin + admin:credentials / holder / native:read |
| Signed transfer (prepare → sign outside → submit) | `POST /native/transfers/prepare`, `POST /native/transfers/submit` | native:transfer (holder) |
| Claim lineage; accounting over backing/claims/returns; checkpoint | `GET /native/accounts/{id}/lineage`, `GET /native/accounting`, `POST /native/checkpoint` | native:read / native:admin |
| Subdivided export | `POST /native/allocations/export` with `portions` | native:export |
| Resource market | `GET /resources`, `POST GET /resources/offers`, `GET /resources/offers/{id}`, `POST /resources/offers/{id}/amend`, `POST /resources/quote`, `POST GET /resources/reservations`, `GET /resources/reservations/{id}`, `POST /resources/reservations/{id}/{activate|deliver|settle|cancel}`, `POST /resources/reservations/{id}/storage` (raw bytes) | work:read / native:transfer (buyer) / provider |
| Missions | `POST GET /missions`, `GET /missions/templates`, `GET /missions/{id}`, `/ledger`, `/explain`, `/bundle`, `POST /missions/{id}/{freeze|amend|pause|resume|step|run|propose|gate|plan}`, `POST /missions/{id}/revisions/{n}/authorize`, `POST /missions/plan` (joint), `POST /plans/solve` (raw problem) | work:read / owner / affected party |
| Operator screen | `GET /native/operator` | native:read + work:read |

### CLI (`python -m metacoin_service.client_cli --credential-file F --base URL ...`)
`native-actor-enroll native-keys native-key-revoke native-transfer-signed (prepare → sign → submit through the signer) native-lineage native-accounting native-checkpoint resources resource-offers resource-offer resource-publish resource-amend resource-quote resource-reserve resource-reservations resource-reservation resource-activate resource-deliver resource-storage-put resource-settle resource-cancel missions mission-templates mission-create [--freeze] mission mission-ledger mission-explain mission-action {freeze,amend,pause,resume,step,run,propose,gate} mission-authorize mission-plan mission-bundle native-operator`.
Separate executables: `python -m metacoin_service.native.signer {init|serve|health}`, `python -m metacoin_service.native.witness {init|serve}`, `python -m metacoin_service.deploy_local {up|status|down}`, `metacoin-provider` (provider_client/).

### Console
`/console/resources` (market, quote, reserve, offers), `/console/resources/reservations/{id}` (activate / deliver / settle / cancel with labelled evidence), `/console/missions` (builder from the three templates, list), `/console/missions/{id}` (graph/timeline, budget ledger projection, controller explanation with disabled actions and reasons, step/run/pause/resume/replan, revisions, milestone evidence, events), `/console/operator` (owns/owes, committed resources, missing evidence, blackout possibilities, pending cross-domain, recovery; link cut/restore).

### MCP tools (analysis vs spending)
READ: `operator_screen`, `resource_market`, `mission_status`, `mission_explain`, `mission_plan` (analysis; locks nothing). SIMULATE: `resource_quote`. SPEND (grant + idempotency): `resource_reserve`, `mission_step`, `mission_propose` (one validated action or a recorded rejection).

## Order 2026-10-23 (independent machine network) additions

### HTTP API
| Operation | Route | Permission |
|---|---|---|
| Signing requests (external custody): pending, view, submit signature, cancel | `GET /native/signing/pending`, `GET /native/signing/requests/{id}`, `POST /native/signing/requests/{id}/submit`, `POST .../cancel` | holder |
| Signed reservation: prepare (message + context + reserve body) → sign at the signer → reserve with `native_signature` | `POST /resources/reservations/prepare`, `POST /resources/reservations` | native:transfer (holder) |
| Resource pools: create, list, view (offers, commitments, peak, within-capacity), amend / drain / close | `POST GET /resources/pools`, `GET /resources/pools/{id}`, `POST /resources/pools/{id}/{amend|drain|close}` | work:offer (owner) / native:read |
| Provider identity and independence disclosure | `GET /work/providers/{id}/identity` | work:read |
| Sub-contracts: propose, list, view, `prefund-prepare` / `prefund` (requester), `step`, `return-unused` (primary) | `POST GET /subcontracts`, `GET /subcontracts/{id}`, `POST /subcontracts/{id}/{action}` | primary / requester / parties |
| Mission products: publish, list, view, purchase (idempotent), `check-reuse`, entitlements, download | `POST GET /products`, `GET /products/{id}`, `POST /products/{id}/{purchase|check-reuse}`, `GET /products/entitlements[/{id}[/download]]` | owner / buyer |
| Relay routes: create (bounded fees), submit bytes, forward per hop, message (recipient), acknowledge, settle, cancel-unused | `POST GET /relay/routes`, `GET /relay/routes/{id}`, `POST /relay/routes/{id}/submit` (octet-stream), `POST /relay/routes/{id}/{forward|acknowledge|settle|cancel-unused}`, `GET /relay/routes/{id}/message` | sender / relay / recipient |
| Claim lots: per-account lots and retirable total; consolidate; partial return (`amount`) | `GET /native/accounts/{id}/lots`, `POST /native/accounts/{id}/lots/consolidate`, `POST /native/allocations/{id}/return {amount}` | holder |
| Liquidity split | `GET /native/accounts/{id}/liquidity[?asset=]` | native:read |
| Portfolio: plan (reference / oracle / improved, non-binding), validate (independent), compare rolling vs fixed | `POST /portfolio/{plan|validate|compare}` | work:read |
| Continuity and fenced takeover; generation in `step`/`run`/`propose` (`generation` or header `X-Controller-Generation`) | `GET /missions/{id}/continuity`, `POST /missions/{id}/takeover` | parties |

### CLI
`python -m metacoin_service.client_cli ... native-signing-requests native-sign-pending` (the actor's agent loop), the provider CLI
`metacoin-provider {init, handshake, capabilities, drain, withdraw, earnings, signing-pending, sign-pending, signer-policy}` with
`--home` (pinned identity and versions, 0600 config) and `--ca`; `python -m metacoin_service.native.controller_runner` (restart-safe
fenced controller), `python -m metacoin_service.native.hostconfig {generate|check}` (second-host configuration generator).

### Console
`/console/providers` (identity separation, custody, outcomes), `/console/pools` (create a pool + offer as a provider), `/console/subcontracts/{id}`,
`/console/products` (purchase form; an expired session preserves the unsent form and pays nothing), `/console/relay`,
`/console/missions/{id}/continuity` (+ takeover), `/console/portfolio?missions=` (liquidity split, non-binding comparison). Every
page shows environment, asset, custody and domain.

### MCP tools
READ (lock nothing, spend nothing): `pools`, `portfolio_plan`, `plan_validate`, `mission_continuity`, `subcontract_status`, `products`,
`product_check_reuse`, `relay_routes`. SPEND (idempotency key, grant envelope): `subcontract_propose`, `product_purchase`.
