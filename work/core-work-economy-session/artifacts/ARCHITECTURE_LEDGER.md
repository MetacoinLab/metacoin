# Architecture ledger (Order 08 §5): each new operation → reused implementation → authority check
| New operation | Reused implementation | Authority check |
|---|---|---|
| WorkTerms draft/validate/compare/freeze/inspect | contracts.Contracts (job contract for the operation; input vault; freeze digest) + economy.terms (typed deliverables, acceptance policy, milestones) | contract:create/freeze/amend (owner), principal.workspace |
| Acceptance policy evaluation | verification.gate + verification_jobs statements, reviews signatures, energy/resource_plan outcome sets | evaluation is read-only; acceptance transition requires work:accept |
| Request board / offers / awards | catalog quotes binding pattern, budgets.reserve (kind 'work_award'), agents.guard for agent principals, approvals.gate for consequential awards | work:request (owner), work:offer (provider role), award = requester only |
| Provider execution | jobs.submit + worker (local fixture) or federation node claim (execution_locations=[nd_…]) | job lease fencing; node signed requests |
| Receipts | metering.ensure_service_key + crypto.sign, merkle.canonical | signer custody recorded per receipt |
| Entitlements / payment intents / settlement | x402_http upto machinery on the local chain (payer key = chain dev account), metered_settlements pattern | work:pay (owner) + production guard (test-http only) |
| Journal | new double-entry tables; replay compares with live views | budget:read for views; postings only by server business events |
| Treasury | journal fee postings → treasury accounts; awards reuse WorkTerms | owner/admin:credentials for allocation |
| Audit grants | sharing.py pattern generalised to contract scope; artifacts store recipients | grant holder authenticated; audit:* permissions |
| Offline packages | packages.result_bundle + verify_bundle.py + crypto.encrypt_bytes (age) | export requires artifact:export |
| Legacy bridge | demo/tasks modules + protocol ledger registered hashes (read-only) as job kind legacy_task_replay | contract kinds table; worker_exec child |
| Mission portfolio | mission_verdict.json / ledger entries read-only; analyses/campaigns references | work:request |
