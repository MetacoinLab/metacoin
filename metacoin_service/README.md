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
`python -m metacoin_service.client_cli --credential-file FILE create|freeze|submit|poll|result|review-request|decide|export|compare`.
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
