# The pulse: is this thing alive, and is every gate green?

> **THE DOC CONTRACT.** Everything in this document is [BUILT] fact,
> mechanically verified by protocol/doc_verify.py on every CI run: the
> command block below is executed for real in a fresh clone and its output
> pasted (trimmed for volume, never altered), every stated number is tagged
> with the chain point it describes and re-checked against live state, and
> nothing here claims more than the anchored pulse record proves.
>
> Chain point: tip index <!--chain:tip_index-->79<!--/chain-->,
> <!--chain:entry_count-->80<!--/chain--> entries.

## What a pulse is

A **pulse** is one machine-readable file — `protocol/evidence/pulse_<hash>.json`
— that says, with a hash anchored on the chain, that at a named chain point
and a named commit the whole stack was green: every anchored layer
re-derived, both self-test suites at N/N, the task law clean, every
documentation command executed and every stated number chain-checked, a real
cold install (wheel → fresh venv → `metacoin verify` in an empty directory)
passing, and the routine sweep reporting zero findings.

Nothing in it is hand-written. `protocol/pulse.py --generate` derives every
value from the chain and from real gate **runs**, and it enforces one rule:
**a pulse that cannot be green honestly is not generated.** There is no
"pulse with findings" — the absence of a fresh pulse is itself the signal.
The file carries no timestamps (hashed artifacts never do here); its date is
the anchoring record's own `anchored_at`, and the coordinator's `--confirm`
between generation and anchoring stays human. Weekly cadence rides the
routine sweep: a clean sweep, then a pulse.

## The latest pulse

| | |
|---|---|
| Anchored at ledger index | <!--chain:pulse_idx-->none<!--/chain--> (pulse <!--chain:pulse_count-->none<!--/chain--> of the series) |
| Anchored on (UTC) | <!--chain:pulse_date-->none<!--/chain--> |
| pulse_hash | `<!--chain:pulse_hash_prefix-->none<!--/chain-->…` |
| Chain point it describes | <!--chain:pulse_entries-->none<!--/chain--> entries, tip index <!--chain:pulse_tip_index-->none<!--/chain--> (<!--chain:pulse_entries_since-->none<!--/chain--> entries since) |
| Repository commit | `<!--chain:pulse_commit-->none<!--/chain-->` |
| verify_everything | ALL LAYERS PASS — <!--chain:pulse_layers-->none<!--/chain--> layers |
| Self-test suites (demo / protocol) | <!--chain:pulse_demo_suite-->none<!--/chain--> / <!--chain:pulse_protocol_suite-->none<!--/chain--> |
| Task law (MIP-0008/0009) | <!--chain:pulse_task_law-->none<!--/chain--> |
| Documentation suite | CLEAN — <!--chain:pulse_doc_commands-->none<!--/chain--> commands executed |
| Cold install | <!--chain:pulse_cold_install-->none<!--/chain--> |
| Routine sweep findings | <!--chain:pulse_sweep_findings-->none<!--/chain--> |
| Recorded tasks (honest negatives) | <!--chain:pulse_tasks-->none<!--/chain--> (<!--chain:pulse_honest_negatives-->none<!--/chain-->) |
| Anchored MIP decisions | <!--chain:pulse_mip_decisions-->none<!--/chain--> |
| Registered actors | <!--chain:pulse_actors-->none<!--/chain--> (all the same operator — independence is measured, not claimed) |
| Mirror last attested at idx | <!--chain:pulse_mirror_idx-->none<!--/chain--> |

Every value above is rendered from the latest `pulse_recorded` record on the
chain by `protocol/doc_verify.py --render` and re-checked on every CI run.

## The one-command check

From any clone, without a live ledger or any private material:

```verify-run
$ python3 protocol/pulse.py --status
PULSE STATUS: OK  (trimmed)
```
<!--expect:PULSE STATUS: OK-->

`--status` finds the latest anchored pulse, loads its shipped evidence file,
recomputes the self-hash, re-applies the refusal rule to the file's gate
fields, checks the headline numbers against the record, and binds the
pulse's chain point to a real prefix of the chain. `verify_everything`
carries the same check as its `pulse` layer, so `metacoin verify` covers it
too. To re-derive a pulse from scratch at the same state (the full battery,
minutes): `python3 protocol/pulse.py --verify protocol/evidence/pulse_<hash>.json`.

## What a pulse does NOT establish

That the work is useful (the usefulness seat is honestly vacant), that any
verifier is independent (every actor on the chain is the same operator, and
the pulse says so), or that anything is released (the default remains NO
release; READY is necessary, never sufficient). A pulse proves that the
stack was green, mechanically and reproducibly, at the chain point it names
— and that someone chose to say so on the record. Research-stage; zero-value;
no token.
