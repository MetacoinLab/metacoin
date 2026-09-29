# Accounting model of the work economy (Order 08 §49–§51)

Source of truth: `metacoin_service/economy/journal.py` (delivered as `ACCOUNTING_MODEL_journal.py`).

## Principles
- Double entry, integer base units, one scope per entry (`asset@network`, environment kept separately). Assets and environments are never mixed into one number.
- Every entry binds one unique business event (`event_key`): a retried submission, a replayed award or a re-observed settlement posts nothing twice. Per-attempt event keys (`submit:<intent>:<attempt>`) keep genuine re-submissions distinct from replays.
- Derived balances are rebuilt from postings by `replay()` and compared with the live operational views (entitlements, refunds, treasury); nothing patches a cached balance. `invariants()` checks each scope balances to zero and that exposure never exceeds submitted, unobserved intents.
- Application budget reservations are memorandum accounts, not money. Work payments never touch base issuance: no account in this journal represents META supply, and Test-META and rail assets (`action-units`, `local-chain-token`) are the only scopes.

## Accounts
| account | meaning |
|---|---|
| memo:budget_reserved / memo:budget_available | application reservations against the requester budget tree (memorandum) |
| expense:work_accepted | accepted obligations to providers |
| expense:platform_fee / expense:verifier | fee and verifier obligations of the requester |
| liability:payable | accepted, not yet submitted obligations (ref = entitlement) |
| exposure:pending | submitted, not yet observed transfers |
| asset:payer_cash | the payer's holdings (credit = paid out, debit = refund received) |
| treasury:revenue / treasury:cash | fees observed as settled to the treasury address |
| treasury:committed | treasury allocations reserved for awards (memorandum against revenue) |
| liability:refund_claim | repayment obligations until a reverse transfer is observed |
| liability:credit | application credits: a liability of this service, never returned currency |

## Event flow
1. `decision:<milestone>` — acceptance: debit expense:work_accepted, credit liability:payable (+ fee and verifier legs when the terms declare them).
2. `submit:<intent>:<attempt>` — payment submitted: liability:payable → exposure:pending (committed before the rail call so a lost response leaves exposure visible).
3. `settle:<intent>` — transfer observed: exposure:pending → asset:payer_cash; `headroom:<intent>` reverses the unowed difference of a capped (upto) payment; `fee:<intent>` credits treasury:revenue once.
4. `reconcile:<intent>` — expired and unused: exposure reversed only for intents that were actually submitted; renewal opens a new intent identity.
5. `refund:<request_key>` and `refund_settle:<…>` — recovery under the preauthorized arrangement; partial and duplicate requests are bounded by the refundable amount.
6. `treasury_alloc:<terms>` / `treasury_spend:<intent>` — fee-backed work: availability = confirmed revenue − committed − spent; an allocation above availability is refused.

## What the replay proves and what it does not
- Proves: every balance shown to a requester, provider, verifier or treasury view is reproducible from the postings; obligations, reserves and exposure match the live entitlement and intent tables; no protocol file, identity string or ledger byte changed (journey 38 checks the digests).
- Does not prove: external settlement finality (the private local chain is in-memory and synthetic), or economic price discovery (one operator plays every role).
