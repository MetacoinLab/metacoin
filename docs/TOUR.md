# The 15-minute independent review

> **THE DOC CONTRACT.** Everything in this document is [BUILT] fact,
> mechanically verified by protocol/doc_verify.py on every CI run: every
> command below is executed in a fresh-clone sandbox, every ledger index
> cited is checked to exist on the chain with exactly the stated event type,
> and every tagged number is era-pinned — checked against the chain **at
> this document's declared as-of point**, so the tour stays verifiably
> green as the chain grows and goes red only if it misstates its own era.

<!--era-pin:entry_count=72 tip_hash_prefix=00a9db5f75de-->
**Reviewed era:** ledger entry <!--era:entry_count-->72<!--/era--> (tip
index <!--era:tip_index-->71<!--/era-->, hash
`<!--era:tip_hash_prefix-->00a9db5f75de<!--/era-->…`, August 2026).

**Design principle: this tour makes no claims.** It hands you the commands
that let the repository make its own case — including its negative results.
Run everything below from a fresh clone (`git clone` this repository, `cd`
in; Python 3 standard library only, nothing to install for the tour
itself). The command blocks in this file are executed verbatim by CI in
exactly that starting state; their combined machine time is under ten
seconds — the fifteen minutes is yours, for reading what they print.

---

## Minute 0-3 — don't trust, reproduce: the flagship

One command re-checks every layer of the anchored evidence stack — chain
and tip anchor, all <!--era:recorded_task_count-->18<!--/era--> recorded
tasks, <!--era:catalog_anchor_count-->5<!--/era--> molecule-catalog
generations, concentration, both economy generations, cuts, trust vectors,
challenges, identity, treasury, governance, uptime emission, passports,
intake — with no LLM judgment anywhere. The bounded-cost form runs in
under a second:

```verify-run
$ python3 protocol/verify_everything.py --quick
RESULT: ALL LAYERS PASS  (trimmed)
```
<!--expect:ALL LAYERS PASS-->

**What a pass does NOT establish:** `--quick` is conditional acceptance
against the committed anchors, not a re-proof. The full re-derivation is
the same command with `--full` (a few minutes: every task re-run, every
molecule rebuilt, every generation re-locked) — or `pip install .` and
`metacoin verify`. CI executes the full form from a bare pip install in an
empty directory on every push, and requires it to pass from package data
alone.

## Minute 3-5 — the honest negatives (read this section first if you read only one)

A verification protocol that can only say "yes" is a marketing machine.
This library anchors its own "no", twice. task-0012's deep-space link
budget does not close — margin **−1.635481 dB** — and the anchored record
says so:

```verify-run
$ python3 demo/tasks/task_0012_comms_link_budget.py
{"...":"...","link_closes":false,"link_margin_db":-1.635481,...}  (trimmed)
```
<!--expect:"link_closes":false-->
<!--expect:-1.635481-->

task-0018's Mars-ascent feasibility check consumes task-0017's ISRU
propellant budget and finds it **does not close** — required ≈4680 m/s vs
≈2191 m/s achievable, margin **−2489.44 m/s** — and the constants are
deliberately not tuned to manufacture success:

```verify-run
$ python3 demo/tasks/task_0018_ascent_feasibility.py
{"...":"...","feasible":false,"margin_m_s":-2489.44234,...}  (trimmed)
```
<!--expect:"feasible":false-->
<!--expect:-2489.44234-->

**What a pass does NOT establish:** that the physics is flight-grade (both
tasks state their engineering-representative constants and idealizations
in their docstrings). The point is narrower and checkable: negative
verdicts reproduce bit-for-bit and are anchored like any other fact.
(Bit-reproducibility across PLATFORMS is a property the canonical form
must earn — a real macOS run once diverged by a single sign-of-zero bit,
and the anchored era-2 canonical rule at idx 67 is the repair; the chain
records the whole story.)

## Minute 5-7 — the self-measurement

The protocol measured its own centralization before anyone could ask, and
publishes the worst case: every verification path is the SAME operator.
The frozen baseline (<!--era:baseline_path_count-->28<!--/era--> paths,
pairwise ACI <!--era:baseline_pairwise_aci-->0.99365<!--/era-->) is epoch
zero of a longitudinal series; the latest anchored epoch measures
<!--era:epoch_pairwise_aci-->0.998508<!--/era--> over
<!--era:epoch_path_count-->66<!--/era--> paths — rising toward 1 exactly
as same-operator accumulation should, which is what the anchored
interpretation says it is:

```verify-run
$ python3 protocol/agent_concentration.py --report --ledger protocol/ledger_published.json --out aci_report_tour.json
  "interpretation": "... the maximal-concentration baseline ..."  (trimmed)
```
<!--expect:maximal-concentration baseline-->
<!--expect:pairwise_aci-->

**What a pass does NOT establish:** independence — a low future ACI would
not prove it either; missing metadata scores worst-case, never as
independence. The anchored epoch series exists so the first epoch after an
unaffiliated participant anchors verification paths has a baseline to be
measured against.

## Minute 7-9 — the defeated attacks

The chain records attacks staged against itself and their refusal. Two
examples: the copy-attack (a replayed challenge response, idx 25) and a
tampered participant bundle refused at its named validation rung (idx 47).
The census — <!--era:drill_entry_count-->7<!--/era--> drill-labeled
entries covering six distinct on-ledger attacks, plus two planned in-log
economy tamper rejections (one per economy generation), eight scripted
adversarial demonstrations in all:

```verify-run
$ python3 -c "import json; es=json.load(open('protocol/ledger_published.json'))['entries']; ds=[(e['index'], e['payload']['event']) for e in es if e['payload'].get('drill')]; print('drill-labeled entries:', len(ds)); [print(' idx', i, ev) for i, ev in ds]"
drill-labeled entries: 7  (trimmed)
```
<!--expect:drill-labeled entries: 7-->
<!--expect:idx 25 challenge_response_result-->
<!--expect:idx 47 participant_intake_rejected-->

**What a pass does NOT establish:** that real adversaries were defeated —
every drill is scripted by the operator and labeled `drill` on its record,
never "detected fraud". The refutations themselves re-derive inside the
flagship command's challenge and intake layers on every run.

## Minute 9-11 — provenance depth

Work consumes prior verified work, and the chain can prove it. task-0018's
molecule carries its parent's content-address inside its own hashed
identity; the parent's molecule does the same for its parent — a
three-generation chain (0015 → 0017 → 0018) whose edges are enforced at
execution time (a drifted ancestor crashes the descendant), at
construction time, and at verification time:

```verify-run
$ python3 protocol/work_molecule.py --task task-0018 --ledger protocol/ledger_published.json
  "parent_work_ids": ["..."], "parents_resolution": {...}  (trimmed)
```
<!--expect:parent_work_ids-->
<!--expect:parents_resolution-->

Bounded verification across that ancestry: the anchored multi-hop cut
certificate (idx 66) was fully proven once at anchoring; a later reviewer
accepts it cheaply — one anchored-hash lookup plus a retrievability probe —
with the conditionality stated, not hidden:

```verify-run
$ python3 protocol/cut_certificate.py --accept protocol/evidence/cut_cert_33d6e3a3b8e1.json --ledger protocol/ledger_published.json
acceptance       : ACCEPTED  (trimmed)
```
<!--expect:acceptance       : ACCEPTED-->
<!--expect:CONDITIONAL acceptance-->

**What a pass does NOT establish:** usefulness of the work, or independence
of the actors — the edge is a reproducibility fact, and the cut's cheap
acceptance is explicitly conditional on the anchor plus retrievability,
never a re-proof.

## Minute 11-13 — the gates the project points at itself

Ask the repository what stands between it and its next release; it answers
mechanically, with named gaps it refuses to simulate away:

```verify-run
$ python3 protocol/release_readiness.py --check --fast
VERDICT: NOT-READY — 1 named gap(s) stand between here and the next release  (trimmed)
```
<!--expect:VERDICT: NOT-READY-->
<!--expect:idx 70: participant fingerprint differs from every coordinator machine on the chain-->
<!--expect:awaits the second device-->

One gap fewer than the last reviewed era: a second physical machine ran
the public verifier and its bundle passed the full intake ladder (idx
69–70, topology `cross-machine-same-operator` — decided by the machine
fingerprint, never the declaration). Same operator, second machine: the
independence milestone is untouched, and the gate says exactly that.

Governance runs through the same discipline. Every proposal file has an
anchored lifecycle state — <!--era:mip_decision_count-->6<!--/era-->
decisions: four accepted MIPs frozen immutable-by-citation (the newest,
MIP-0006, superseding MIP-0005's two-gap era assertion on exactly the
trigger that MIP named), and the two ambitious June drafts honestly
**retained as drafts**, because accepting them would have ratified
capabilities (voting, attestation hardware, token economics) that do not
exist:

```verify-run
$ python3 -c "import json; es=json.load(open('protocol/ledger_published.json'))['entries']; rs=[e['payload'] for e in es if e['payload'].get('event')=='mip_decision_recorded']; print('anchored MIP decisions:', len(rs)); [print(' ', p['mip_id'], p['status']) for p in rs]"
anchored MIP decisions: 6  (trimmed)
```
<!--expect:anchored MIP decisions: 6-->
<!--expect:MIP-0006 mip-accepted-->
<!--expect:MIP-0002 mip-retained-as-draft-->

**What a pass does NOT establish:** that NOT-READY is a temporary
embarrassment — it is the expected state between releases, reported
weekly by the sweep as information; and a recorded governance decision
proves the process ran, not that the decision is wise (the review seat has
one occupant, and every record says so).

## Minute 13-15 — what none of this proves

The report you have been running ends with its own boundary; read it from
the source rather than from this document:

```verify-run
$ python3 -c "import sys; sys.path.insert(0,'.'); from protocol.verify_everything import HONEST_BOUNDARY; print(HONEST_BOUNDARY)"
Everything above is SAME-OPERATOR, zero-value, research-stage evidence.  (trimmed)
```
<!--expect:SAME-OPERATOR-->
<!--expect:no token exists-->

In short: every verifier so far is the same operator; the units are
zero-value placeholders; no token exists; usefulness judgment is honestly
vacant; TEE attestation and hardware power telemetry are open, stated
debts. The next meaningful milestone is operational, not code: an
unaffiliated third party running the public verifier and submitting a
result. The participant path for that is built and rehearsed end-to-end —
including the rejection path — and documented in
[`docs/PARTICIPATE.md`](PARTICIPATE.md). These documents will say so until
it happens.
