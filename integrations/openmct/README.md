# Trust-ledger console — the anchored audit log on a mission-control screen

Research-stage. This directory puts the MetaCoin ledger on an
[Open MCT](https://github.com/nasa/openmct) screen — NASA's open-source
mission-control framework (Apache-2.0), used here as an ordinary npm
dependency of this integration only. No NASA affiliation or endorsement.
This README is in the doc-verify scan set: its chain-number tokens and
verify-run block are mechanically verified by protocol/doc_verify.py
on every CI run, so its numbers cannot silently drift from the chain.

## A VIEWER, not a verifier — stated first

Nothing on this screen proves anything. The proof lives one directory up:
`python3 protocol/verify_everything.py --full` re-derives every anchored
claim from a fresh clone, and `docs/TOUR.md` walks it in 15 minutes. This
console DISPLAYS the same record the verifier proves — the
<!--chain:entry_count-->108<!--/chain-->-entry anchored audit log
(tip <!--chain:tip_index-->107<!--/chain-->) — because an append-only
cryptographic log and a mission-ops timeline are, structurally, the same
object, and seeing the record the way an operations room would see it is
the fastest honest demonstration this protocol has. Read-only, no live
coupling: the app consumes one JSON file generated from the published
snapshot that every clone ships.

## The dependency boundary

The protocol's zero-runtime-dependency rule is untouched: nothing in
`protocol/`, `demo/`, or `metacoin_cli/` imports this directory, the data
generator is standard-library Python, and the cold install passes with
this directory deleted. Open MCT arrives only via `npm install` HERE
(pinned in package.json), the same boundary discipline as
[`../inspect/`](../inspect/).

## What the screen shows (the telemetry mapping)

| Console object | Source on the chain |
|---|---|
| **Chain Events** | every anchored record, one event datum per ledger entry: {utc, index, event, status, hash prefix} |
| **Drills & Refusals** | the defeated attacks and refused inputs, as an annotated event timeline — the honesty story, on a timeline |
| **Same-Operator Concentration** | pairwise ACI from the anchored baselines and epoch observations, as a numeric plot |
| **Mission Verdicts** | every `mission_verdict_recorded` record: mission id, feasible flag (honest FALSEs included), failed/constraining counts, verdict hash |
| **Pulse Health** | every anchored pulse's gate headline: layers, suite totals, sweep findings, cold-install verdict |

Timestamps are the records' own `anchored_at` wall-clock fields — display
data; the hashed artifacts themselves stay timestamp-free per the house
rule, and this generated file is never anchored.

## Run it

```bash
python3 integrations/openmct/generate_view_data.py   # snapshot -> data/trust_ledger.json
cd integrations/openmct && npm install               # pinned Open MCT (this dir only)
npm run serve                                        # http://localhost:8081
```

The generator's self-test proves the contract without npm or a browser —
deterministic output, resolvable identifiers, closed compositions, sorted
telemetry — executed by doc_verify on every CI run:

```verify-run
$ python3 integrations/openmct/generate_view_data.py --selftest
deterministic: two builds byte-identical  (trimmed)
```
<!--expect:deterministic: two builds byte-identical-->
<!--expect:every ledger entry appears exactly once on the chain stream-->
<!--expect:ALL CHECKS BEHAVED CORRECTLY-->

## Intent, honestly

This is the Flight Software Workshop 2027 showpiece candidate ("the
deployed protocol + the trust-ledger view" — the abstract window opens
October 2026): a demonstration that a verification protocol's record is
legible to the operations culture it borrows its discipline from.
Screenshots will be added once the view is exercised in a browser; v0
ships the machinery, not the marketing. No token; zero value; not
financial, legal, or flight-engineering advice.

---

Research-only; zero-value; no token (MIP-0001 ¶3, MIP-0002 ¶8). ZERO
ledger writes. Open MCT is (c) NASA, Apache-2.0 — attribution in
package.json and here; no NASA affiliation or endorsement. Licensed under
SML-1.0 — see [`../../LICENSE.md`](../../LICENSE.md).
