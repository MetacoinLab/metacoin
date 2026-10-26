# Custody and threat model — independent machine network (order 2026-10-23)

## Who holds which key
| party | key | where | can do | cannot do |
|---|---|---|---|---|
| domain authority (per domain) | Ed25519 authority key | service `keys_dir/native/authority-<domain>.ed25519` (0600) | sign allocation certificates, acknowledgments, return certificates, commit checkpoints, envelopes | spend any actor's funds (an authority signature is never an intent signature) |
| requester, primary machine, supplier, sub-contractor, verifier, relay, contribution recipient (dom-c) | Ed25519 actor keys | **their own signer processes** (`sig-requester`, `sig-machine`, `sig-supplier`, `sig-others`), never the service | sign intents whose context the signer verified against its durable policy | be used by the service: there is no fallback path; the API and worker hold no actor key (`keys_dir/native/actor-*` empty on dom-c) |
| development actors (dom-a / dom-b fixtures, claim journeys) | Ed25519 actor keys | service `keys_dir/native/actor-*` (0600), labelled `development` | sign on the actor's behalf (development custody) | pass as operator custody (labelled everywhere; creation disabled on dom-c) |
| witness | Ed25519 witness key | witness process home | acknowledge checkpoints, refuse lower sequences and conflicting histories | order transactions or release funds |
| provider client | none | — | pin roots and verify receipts, bundles and exports offline; talk to the public API only | hold or derive any private key |

## What the signer separation protects against, and what it does not
Protects against the **service** spending on an actor's behalf (no key, no fallback), against a mutated request (recipient, amount,
asset, domain, obligation, context: refused before signing), against a replayed authorization (strictly increasing sequence at
the signer; `nonce_replayed` at the service), against delegation widening, and against an unavailable signer causing a
silent fallback (the action waits). It does **not** protect against a host-level compromise: every signer here runs on the same
host under the same operator as the service (`isolation_mode: process-separated`). Operator-separated hosts are the stronger
configuration (TWO_HOST.md, hostconfig generator) and were not rehearsed (no authorized second host).

## Authority and independence
Each domain is a single ordering authority with one fenced writer. Peers, witnesses and the three local domains are run by one
operator: replicated processes are not independent validators; equivocation is detected where two signed histories meet
(witness `conflicting_checkpoint`, peer `conflicting_signed_history`) and never prevented. Providers declare an identity
separation; the service discloses shared operators and never counts a declaration as independence (journey 9).

## Money
No path mints base META. Objective issuance is separate from mission payments; mission payments come from requester funds;
the treasury receives fees only and awards never exceed confirmed fee revenue (journeys 1, 50, demonstration); fixtures fund a
zero-value development asset in isolated homes only. Receivables, reserved and in-transit amounts never spend
(`insufficient_settled_funds`, `not_spendable`); a planner recommendation is not spending authorization.

## Threats considered in this order (and where they are refused)
| threat | refusal / evidence |
|---|---|
| overselling a shared interval across overlapping offers, racing requests | pool sweep inside the reservation transaction (`pool_capacity_exceeded`; journeys 11–13, endurance) |
| reducing capacity under committed reservations | `capacity_below_commitments` (14) |
| funding a sub-contract from the parent's expected earnings or from receivables | `circular_funding`, `insufficient_settled_funds` (17) |
| a sub-contractor reading the parent's private inputs | redaction and compartments (21) |
| double charge after a crash between settlement and delivery | idempotent purchase/entitlement, `product_delivery` fault point (24) |
| relay overcharging, undelivered bytes paid, receipt lost, duplicate forward | per-hop reservation bound by ceiling, KiB actually forwarded, `relay_receipt` fault, idempotent forward (26–30) |
| retiring another owner's claim, retiring more than owned, consuming a parent twice, mixed assets, missing lineage | owner-only retirement over explicit lots (31–35); legacy claims get no invented provenance (47) |
| a stale controller mutating after takeover; a self-issued generation; takeover of a quarantined domain | fenced generations (36–39) |
| a signed-but-unsubmitted intent poisoning the sequence; replaying an authorization | allocation at preparation, `nonce_replayed` (custody unit test; demonstration) |
| a plan spending pending remote claims or overselling a pool | independent validator (44) |
| a restored stale database | witness-acknowledged checkpoint → quarantine (39) |

## Known limits (kept visible)
Same operator everywhere; process-separated signers; cross-domain exports remain a development-custody action in this order;
no BPv7; the Apollo 11 LDEM_4 extract is a grid-scale demonstration, not flight navigation.
