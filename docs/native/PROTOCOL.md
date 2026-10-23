# MetaCoin native space-work and settlement protocol — `metacoin-native/v0` (policy `native-policy/v0`)

Status: development protocol of a zero-value development asset on one authorized host. It is not a launched currency, not a public network and not a Byzantine fault-tolerant system. x402 is an optional Earth-side adapter and is never imported by the native package.

## 1. Identifiers and encoding

- Asset id: `environment:authority:ledger:asset:scale` — regex `^(dev|test|production):[a-z0-9][a-z0-9-]{0,47}:[a-z0-9][a-z0-9-]{0,47}:[A-Za-z0-9][A-Za-z0-9-]{0,31}:(0|[1-9]|1[0-8])$`. Environments: dev, test, production. Scale 0..18 (integer base units).

- Amounts: integers in 1..4611686018427387903 (zero only where explicitly allowed). Floats, booleans, NaN and unbounded strings are refused at the boundary.

- Canonical encoding: canonical JSON (sorted keys, ASCII, integers only, depth <= 16, strings <= 4096); strict parsing rejects duplicate keys, floats and constants

- Signatures: Ed25519 over tag || sha256(canonical(body)); the tag separates message types; the body binds protocol, environment and authority

- Domain-separation tags: `intent`=`metacoin/native/v0/intent`, `allocation_export`=`metacoin/native/v0/export`, `allocation_certificate`=`metacoin/native/v0/certificate`, `import_ack`=`metacoin/native/v0/import-ack`, `return_request`=`metacoin/native/v0/return-request`, `return_certificate`=`metacoin/native/v0/return-certificate`, `commit_checkpoint`=`metacoin/native/v0/checkpoint`, `envelope`=`metacoin/native/v0/envelope`, `receipt`=`metacoin/native/v0/receipt`, `rotation`=`metacoin/native/v0/rotation`, `observation`=`metacoin/native/v0/observation`

## 2. Accounts and categories

Every actor account holds disjoint categories derived from the authoritative journal: `available`, `reserved`, `committed_outgoing`, `in_transit`, `disputed`. Derived views: `earned_unsettled`; `pending_claims`; `settled_in`; `settled_out`.

Value enters a domain only through (a) development fixture funding (`equity:fixture_funding`; dev only; administrator only; never reachable from work acceptance, agents or acceptance paths) or (b) an imported allocation backed by locked backing at its source (`backing:claim:<allocation>`). Value leaves only through an export (`backing:exported:<allocation>`). No path reaches base issuance.

## 3. Signed intents (domain-local transfers)

Fields: `actor_account`, `amount_ceiling`, `asset`, `authority`, `environment`, `grant_id`, `nonce`, `obligation_id`, `permitted_recipients`, `policy_version`, `protocol`, `purpose`, `recipient`, `request_id`, `type` (required: `actor_account`, `amount_ceiling`, `asset`, `authority`, `environment`, `nonce`, `policy_version`, `protocol`, `request_id`, `type`).

States: proposed → reserved → authorized → committed → settled → pending → uncertain → disputed → rejected → cancelled

| from | to | precondition | effect | idempotency |
|---|---|---|---|---|
| proposed | reserved | actor may act within its grant; available >= amount ceiling; recipient permitted; asset/domain/environment match | available -> reserved (journal); grant exposure counted | request_id: a repeated proposal returns the same intent |
| reserved | authorized | signer holds the account key; canonical message bound to protocol, environment, actor, authority, asset, ceiling, recipient, obligation, grant, nonce, policy | signature stored; nothing moves | same nonce -> same signature; a changed field is a new intent |
| authorized | committed | the domain authority (single fenced writer) orders the transition; obligation (if any) outstanding >= amount | reserved -> committed_outgoing (payer); obligation partially/fully satisfied | commit sequence assigned once; retries return the recorded commit |
| committed | settled | same transaction as commit in the local domain (domain-local settlement) | committed_outgoing -> recipient available; fee legs to the treasury account; receipt issued | settlement sequence equals commit sequence |
| reserved | cancelled | not yet committed | reserved -> available (release of provably unexposed amount) | repeat returns cancelled |
| authorized | cancelled | not yet committed; signature retained as evidence | reserved -> available | repeat returns cancelled |
| proposed | rejected | any precondition failed before value changed | none | the refusal is recorded with its reason |
| committed | uncertain | the response to the submitter was lost after commit | none: the commit stands; the submitter must reconcile by intent id | reconciliation returns settled |
| settled | disputed | a bounded dispute referencing this settlement is opened | the disputed amount is marked on the recipient (available -> disputed) only to the extent still available; history untouched | one dispute per settlement |

## 4. Allocations (cross-domain value conservation)

States: reserved, export_committed, certified, imported, acknowledged, return_requested, returned, quarantined

| from | to | where | precondition | effect | crash boundary |
|---|---|---|---|---|---|
| - | reserved | source | source account available >= amount; destination domain pinned | available -> in_transit (journal) | source reservation |
| reserved | export_committed | source | authority orders the export; epoch current | in_transit -> backing:exported (nonspendable at the source, irrevocable once a certificate is issued) | source export commit |
| export_committed | certified | source | certificate signed by the source authority (bound to destination, asset, amount, allocation id, epoch, policy, commit evidence) and placed in the outbox | certificate durable; source backing stays locked | certificate publication |
| certified | imported | destination | certificate verifies under the pinned source root; destination == this domain; allocation id unseen (tombstone check); epoch policy | claim credited: backing:claim -> recipient available at the destination (spendable there) | destination durable import |
| imported | acknowledged | both | signed import acknowledgment delivered to the source | source marks the allocation acknowledged; nothing moves | acknowledgment |
| imported|acknowledged | return_requested | destination | the full allocation is unspent at the destination (partial return unsupported) | destination claim made nonspendable (available -> in_transit) and a signed return request issued | return initiation |
| return_requested | returned | source | return request verifies under the pinned destination root and names an allocation this source exported | backing:exported -> source account available; destination burns its claim on the return certificate | return completion |
| * | quarantined | either | conflicting signed histories, cloned authority or an unresolved rollback watermark | the allocation and the domain's outward spending are frozen until an administrator reconciles | none (safety state) |

Certificate fields: `allocation_id`, `amount`, `asset`, `commit_digest`, `commit_sequence`, `destination_authority`, `environment`, `issued_at`, `policy_version`, `protocol`, `recipient_account`, `source_authority`, `source_epoch`, `type`. Import acknowledgment: `allocation_id`, `destination_authority`, `environment`, `import_sequence`, `imported_at`, `protocol`, `source_authority`, `type`. Return request: `allocation_id`, `amount`, `asset`, `destination_authority`, `destination_sequence`, `environment`, `protocol`, `recipient_account`, `requested_at`, `source_authority`, `type`. Return certificate: `allocation_id`, `amount`, `asset`, `destination_authority`, `environment`, `protocol`, `returned_at`, `source_authority`, `source_sequence`, `type`. Rotation: `authority`, `environment`, `grace_seconds`, `new_epoch`, `new_key_id`, `new_public_hex`, `old_epoch`, `old_key_id`, `protocol`, `rotated_at`, `type`. Observation: `allocations`, `environment`, `issued_at`, `observed`, `observer`, `protocol`, `type`.

Rules: exports are irrevocable once certified; the source backing stays locked until a return certificate; returns are full-and-unspent only (partial return is refused, never simulated); time-based expiry never recreates value; a lost acknowledgment is retried by the sender and deduplicated (tombstone) by the receiver, which replays the acknowledgment.

## 5. Envelopes and transport

Envelope fields: `causality`, `created_at`, `environment`, `expires_at`, `id`, `message_type`, `payload_digest`, `protocol`, `receiver`, `sender`, `type`. Limits: bundle ≤ 1048576 bytes, ≤ 200 items, payload ≤ 65536 bytes, outbox ≤ 5000 queued (new commitments are refused when full), transport expiry 30 days (expiry never moves value), delivered copies garbage-collected after 30 days, tombstones never deleted, rotation grace default 7 days.

Two adapters drive one inbox rule set: the online courier (`python -m metacoin_service.native.courier push`, authenticated HTTP) and the file bundle (`write-bundle` / `import-bundle`). Importing never executes content, fetches URLs or accepts roots; every item is parsed strictly, verified under the receiver's pinned roots, deduplicated, then applied inside a savepoint. Artifact chunks never travel in envelopes; a receipt is not artifact acceptance and delivery is not settlement. This is application-level asynchronous transport, not BPv7 or LunaNet.

## 6. Finality levels (never an unqualified "final")
- `local_authorization`: the actor may attempt the action within its grant (nothing recorded yet)
- `durable_local_commit`: the domain authority ordered and durably recorded the economic transition (commit sequence)
- `delivery_acknowledgment`: another endpoint received a message or artifact (says nothing about acceptance or settlement)
- `work_acceptance`: the frozen contract policy produced an acceptance decision on the delivered evidence root
- `cross_domain_pending_claim`: an exported allocation whose import or backing is still being reconciled
- `domain_local_settlement`: the obligation was paid under this domain's declared rules (spendable here)
- `reconciled_inter_domain_settlement`: both domains' durable state and the backing satisfy the transfer policy (acknowledged allocation; no duplicated ownership)

## 7. Trust model
- Supported domain: a trusted ordering authority with one fenced writer (SQLite BEGIN IMMEDIATE on durable storage), authenticated actors, explicit administrator powers
- Not provided: Byzantine fault tolerance; independent validators (replicated processes of one operator are not independent); equivocation prevention by signature alone (a key holder can sign two histories); public-network finality; launched currency
- Detected: conflicting signed histories for one allocation id; certificates addressed to another domain; replayed allocation ids (tombstones); rollback below the export watermark after a backup restore
- Boundary: crash / retry tolerance is provided by durable commits and idempotent request ids; Byzantine authority behaviour is detected and quarantined where two signed histories meet, never prevented
- rollback detection: a durable watermark (keys_dir/native/watermark-<domain>.json) written outside the database at every inter-domain commitment, plus peers' signed observations. A restore that also restores the watermark file is detected only through a peer observation; a rollback with no peer and no watermark is undetectable (no independent witness exists).

## 8. Key rotation and revocation
A domain rotates to epoch n+1 with a new key; the retiring key signs the rotation notice and stays acceptable for messages created before `retired_at + grace`. A key signs only for its own epoch (a replacement key cannot authorize an earlier epoch's commitments). An offline counterparty learns a rotation only when the notice arrives; prior valid commitments are honoured. Maximum outstanding authority of a disconnected actor is its preallocated available balance plus any grant ceiling, never a remote balance.

## 9. Disputes
A party (payer, payee or administrator) may dispute a settled native obligation within 7 days; the only economic effect is a hold up to the disputed settlement on the payee account. A reviewer resolves: uphold refunds the hold to the payer as a new linked transition, dismiss returns it; the original obligation and intents are never rewritten.

## 10. What a development ledger is not
- a launched currency
- decentralized consensus
- public-network adoption
- space-qualified deployment

## 11. Extension v1 (order 2026-10-23): signers, witnesses, envelope v1, lineage, reservations, missions
Everything below keeps the v0 monetary rules: no minting of base META by any path, objective issuance separate from mission
payments, mission payments from requester funds, MetaStar awards from collected fees, zero-value development assets, bounded
integers, explicit units. The protocol id stays `metacoin-native/v0` for all signed messages; new message bodies are versioned
by their own `schema` fields.

### 11.1 Operator-controlled signing (`metacoin-signer/v1`)
- A separate executable (`python -m metacoin_service.native.signer`) holds actor keys; the service stores public identities only (`native_account_keys`, custody `external-signer:<id>`, `private_key_held_by_service: false`).
- Intents gain an optional `expires_at` (seconds); the signer refuses unknown fields, floats, other domains, other policy versions and expired intents, and records the consumed authorization durably **before** returning the signature (idempotent by `request_id`; a crash before the record leaves no exposure, a crash after it replays the same signature).
- Key registrations are signed by the current primary key with tag `rotation` over `{type: key_registration, account, actor, key_id, public_hex, kind: primary|delegate, epoch, scope, issued_at, grace_seconds}`. A retired key stops authorizing new intents at `retired_at + grace`; historical receipts keep verifying against the key history.
- Delegates carry a recipient set and a ceiling never wider than the principal's; both the signer and the service enforce them.

### 11.2 Commit-checkpoint witnesses (`metacoin-witness/v1`)
Checkpoints `{sequence, journal_root, ...}` are signed by the domain authority with tag `commit_checkpoint` and acknowledged by a pinned witness key. The witness refuses a lower sequence (`sequence_regression`) and a different statement for an acknowledged sequence (`conflicting_checkpoint`). A restored database whose sequence is below the last acknowledged checkpoint enters quarantine even when the local watermark file is missing. Where no witness and no watermark exist, recovery reports detection as **unavailable** rather than inferring a clean start.

### 11.3 Envelope bundle v1
Bundles announce `envelope_versions` (`['v0','v1']`), carry `economic_ids` (the allocation ids whose certificates are inside) and may fragment large items (`{fragment_of, index, count, data, digest}`, 16 KiB default). Fragmentation never alters signed content: receivers reassemble, verify the digest and only then apply the envelope rules (duplicate, reordered and expired items settle nothing twice and refund nothing). An unsupported version set is refused.

### 11.4 Claim lineage and subdivision
An export may be subdivided into portions with distinct allocation ids whose amounts sum exactly to the export. Imported units carry lineage; spending consumes unlineaged units first, then claims oldest-first; any consumption makes a claim nonreturnable; transferred units lose lineage. A return retires the claim irrevocably at the destination **before** the return certificate is issued; the accounting view exposes backing locked, active, consumed and retired claims, pending retirements and completed returns, cross-checked with the journal audit.

### 11.5 Resource reservation market
Offers (compute `cpu_ms`, storage `kib_seconds`, contact `kib`) are fixed-price, versioned records naming provider, resource, unit, time basis, capacity, price, asset, window, reservation policy, delivery evidence and cancellation terms, with energy per unit as a declared planning constraint. A quote is nonbinding; a reservation is atomic (version, capacity and buyer funds checked in one transaction) and refuses a stale quote without repricing. Delivery is measured (`metered_fact`), asserted (`provider_assertion`) and verified (`verified`) with the three labels kept apart; settlement pays the delivered portion once and releases the rest.

### 11.6 Mission contracts and the bounded controller
A mission is a versioned dependency graph of milestones (work, resource, evidence gate, settle, deliver). Freezing records a revision digest; awarded, executing and completed milestones are immutable; amendments create a new revision authorized by every affected party and may touch only unstarted work. The controller performs one validated action per step from an explicit enumeration over immutable offers; a model proposal is checked by the same deterministic validator (liquidity after the minimum cash reserve, per-action and mission ceilings, grant envelope, permitted recipients, offer price bounds, capacity, windows, energy, link state, revocation, quarantine) and every chosen and rejected action is published with its reason. Receivables are never spendable; a cut link leaves only local actions; a revoked grant stops new work but incurred obligations stay payable.

### 11.7 Planning
A plan is a schedule or an explicit unsatisfied-constraint witness over a finite discretised horizon; it never locks capacity or spends. Reservation acceptance revalidates the selected offers.
