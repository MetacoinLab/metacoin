# Architecture: the ledger, layer by layer

> **THE DOC CONTRACT.** Everything in this document is [BUILT] fact,
> mechanically verified by protocol/doc_verify.py on every CI run: every
> ledger index cited below is checked to exist on the live chain with exactly
> the stated event type, every stated number is tagged with the chain point it
> describes and re-checked against live state, and nothing here claims more
> than `metacoin verify` proves.
>
> Chain point: tip index <!--chain:tip_index-->59<!--/chain-->, hash
> <!--chain:tip_hash_prefix-->058f4391f8a1<!--/chain-->…,
> <!--chain:entry_count-->60<!--/chain--> entries, genesis
> <!--chain:genesis_hash_prefix-->71fe94035edd<!--/chain-->….

The protocol's entire public state is one append-only hash chain of
<!--chain:entry_count-->60<!--/chain--> entries. Every layer described below is
*derived* from those entries plus the shipped evidence bundle — there is no
hidden state. This document walks the chain in the order it was built and
says, for each layer, what it proves and what it deliberately does not.

## The work layer — idx 0-16

The chain opens with genesis (idx 0 <!--idx:0=ledger_genesis-->) and the
verification corpus for the first 13 deterministic demo tasks. (The registry
now holds <!--chain:task_count-->17<!--/chain--> tasks and — since the idx
48-52 milestone batch, per the cadence policy — all
<!--chain:recorded_task_count-->17<!--/chain--> are recorded; between
milestones, newly registered tasks stay registered-but-unanchored and every
verifier reports them by name rather than absorbing them silently.)

- One **external verification** (idx 1
  <!--idx:1=external_verification_result-->): a second machine re-derived
  task-0002's canonical output hash and the match was recorded.
- **Agent-verifier attestations** (idx 2, idx 3, idx 16
  <!--idx:16=agent_verifier_attestation-->): mechanical (no-LLM) re-runs of
  the whole corpus from other same-operator platforms.
- **Self-recompute records** for all remaining tasks (idx 4-15
  <!--idx:4=self_recompute_result--><!--idx:15=self_recompute_result-->):
  the same host re-verified its own results — records explicitly labeled as
  adding *no cross-party independence*.

**Proves:** cross-platform deterministic reproducibility of all
<!--chain:recorded_task_count-->17<!--/chain--> recorded task outputs.
**Deliberately does not:**
independence (every verifier is the same operator, and each record's
`operator_relationship` field says so) or execution proof (a matching hash can
be copied; the protocol says "re-derived", never "proven executed").

## The provenance layer — idx 17, 20, 21, 22 (and idx 26)

- **Work-molecule catalog, generation 1** (idx 17
  <!--idx:17=work_molecule_catalog_anchored-->): every task's complete
  evidence assembled into a content-addressed Work Molecule (schema 0.2); the
  catalog hash anchored.
- **Metering evidence** (idx 20 <!--idx:20=metering_evidence_anchored-->):
  wall/CPU times measured for all tasks; energy *estimated* from an assumed
  <!--chain:assumed_power_w-->15.0<!--/chain--> W constant — the labels
  (`measured` vs `estimated`) are part of the anchored claim and are never
  upgraded by translation or rebuild.
- **Catalog generation 2** (idx 21) absorbs that metering evidence into
  schema-0.3 molecules; **generation 3** (idx 26
  <!--idx:26=work_molecule_catalog_anchored-->) absorbs the later
  challenge records; **generation 4** (idx 53) opens the parented-provenance
  era, narrated in its own layer below. The chain carries
  <!--chain:catalog_anchor_count-->4<!--/chain--> catalog anchors in total —
  and **every generation stays verifiable forever**: a generation-locked
  rebuild (`--as-of` the anchor's chain point) must re-derive each anchored
  catalog byte-for-byte. That is the **generation/cadence model**: new
  evidence is *appended*, molecules rebuilt at the next milestone *absorb* it
  under new identifiers, and no anchored artifact is ever edited.
- **Cut certificate** (idx 22 <!--idx:22=cut_certificate_anchored-->): a
  self-contained certificate over the verified interior of the chain at that
  point — degenerate by honest admission (the provenance graph was flat; no
  edge crossed a cut boundary until idx 54).

**Proves:** the evidence trail for each unit of work is complete, content-
addressed, and rebuild-stable, with gaps stated as machine-readable
provenance debt. **Deliberately does not:** fill the gaps — TEE attestation
and hardware power telemetry remain open debts, stated inside the molecules
themselves (see docs/TRUST-MODEL.md).

## The concentration layer — idx 18, 44, and the epoch series from idx 57

The protocol measured its own centralization before anyone else could ask:
pairwise ACI <!--chain:aci_pairwise-->0.99365<!--/chain--> across
<!--chain:aci_path_count-->28<!--/chain--> verification paths
(<!--chain:aci_pair_count-->378<!--/chain--> pairs) anchored at idx 18
<!--idx:18=aci_baseline_anchored--> — the maximal same-operator baseline,
published deliberately. The k-order profile (idx 44
<!--idx:44=aci_korder_baseline_anchored-->) extends this beyond pairs, because
pairwise scoring provably misses group dependency. Missing metadata scores as
worst-case dependence, never as independence.

Since idx 57 <!--idx:57=aci_epoch_observed--> the measurement is a **time
series**: an epoch observation repeats the full measurement at a fixed chain
point, cites every prior anchored concentration record (the frozen idx-18/44
baselines are epoch zero), and anchors explicit epoch-over-epoch deltas —
prior values read off the anchored records, current values re-derived live,
nothing remembered between epochs. The second epoch (66 paths, as-of 56)
shows exactly what its anchored interpretation paragraph says it shows: all
growth to date is same-operator path accumulation, and a rising near-1 ACI
is that accumulation's expected signature. The number to watch is the first
epoch after an unaffiliated participant anchors verification paths — the
series exists so that day has a baseline. Sampled profile rows always carry
their finite-population 95% interval (clipped to the [0,1] domain and saying
so); per-k values only — no cross-k aggregate exists, by construction.

**Proves:** the concentration status is measured and anchored, not narrated —
and now longitudinally: each epoch's deltas re-derive from anchored values
alone. **Deliberately does not:** treat a future low ACI as proof of
independence, or an epoch delta as independence change — descriptive evidence
only, never a minting trigger.

## The economy and treasury layer — idx 19, 31

A 30-simulated-day closed-loop economy demo (idx 19
<!--idx:19=economy_demo_summary_anchored-->) and the treasury configuration
(idx 31 <!--idx:31=treasury_config_anchored-->) anchor the zero-value
accounting demos. **Proves:** the earn→verify→spend arithmetic re-derives and
conservation holds. **Deliberately does not:** create value — no token exists,
and every artifact says so.

## The trust-vector layer — idx 23, 27

Six-component trust vectors per task (idx 23
<!--idx:23=trust_vector_catalog_anchored-->, regenerated at idx 27) with **no
combined scalar, by design** — collapsing to one number would manufacture a
ranking the evidence does not support. Every vector's usefulness component
says `not-assessed`: Gate 3's judgment seat is honestly vacant.

## The challenge-response layer — idx 24-25, 29-30, 43

Nonce-bound possession proofs: a verifier is challenged to re-derive a task
under a fresh nonce. Verified rounds at idx 24
<!--idx:24=challenge_response_result-->, idx 29 and idx 43
<!--idx:43=challenge_response_result-->; **deliberate replay/reuse drills**
at idx 25 <!--idx:25=challenge_response_result--> and idx 30 stay refused on
re-verification, forever. **Proves:** possession at challenge time.
**Deliberately does not:** prove execution history or identity.

## The identity layer — idx 28, 37, 41-42, 45

Actor identity is a Merkle root over one-time keys, walked as a linear chain:
registrations at idx 28 <!--idx:28=actor_key_registered-->, idx 37 and idx 45;
a legitimate **key rotation** at idx 41 <!--idx:41=actor_key_rotated-->; a
**forged-rotation drill** at idx 42 <!--idx:42=actor_key_rotation_rejected-->
that stays rejected from public material alone. The chain currently carries
<!--chain:actor_count-->3<!--/chain--> registered actors. **Proves:**
continuity of key possession across time and rotation. **Deliberately does
not:** prove who operates a key, or that two actors are different people —
operator relationships remain declared, `-claimed`.

## The two-flow layer — idx 31-36, 38-39, 40

The two emission flows of the whitepaper, exercised end-to-end at research
scale:

- **Flow 2 (work)**: Gate-3 provisional grants (idx 32
  <!--idx:32=gate3_provisional_grant-->, idx 33), a filed challenge (idx 34
  <!--idx:34=gate3_challenge_filed-->) adjudicated into a **clawback** (idx 35
  <!--idx:35=gate3_adjudication_clawback-->) — the bounded-failure drill —
  and a clean finalization with closed windows (idx 36
  <!--idx:36=gate3_finalization-->).
- **Flow 1 (uptime)**: a signed-heartbeat epoch (idx 39
  <!--idx:39=uptime_epoch_anchored-->) with a **forged-heartbeat drill**
  (idx 38 <!--idx:38=heartbeat_rejected-->) rejected on the record.
- **Passports** (idx 40 <!--idx:40=passport_catalog_anchored-->): per-actor
  history summaries for <!--chain:passport_actor_count-->6<!--/chain-->
  actors — history, never a leaderboard.

## The intake layer — idx 45-47

The participant pipeline rehearsed on-ledger: identity registered (idx 45
<!--idx:45=actor_key_registered-->), a bundle validated through all six rungs
and anchored **participant-verified** (idx 46
<!--idx:46=participant_result_anchored-->), and a tampered bundle
**mechanically refused at the named rung** (idx 47
<!--idx:47=participant_intake_rejected-->). Every record honestly labeled
same-operator rehearsal.

## The parented-provenance layer — idx 48-54

The milestone batch that anchored the 16-task era and recorded the chain's
first REAL provenance edge:

- **Four self-recompute records** (idx 48-51
  <!--idx:48=self_recompute_result--><!--idx:51=self_recompute_result-->)
  bring tasks 0014-0017 into the recorded corpus under the same honesty
  envelope as idx 4-15 — same-machine, same-operator, explicitly *no*
  cross-party independence.
- **The batch attestation** (idx 52 <!--idx:52=agent_verifier_attestation-->)
  mechanically re-derives every recorded hash across both eras on one record:
  the 13 historical outputs match their idx-16 hashes to the digit, and the
  four new tasks reproduce alongside them.
- **Catalog generation 4** (idx 53
  <!--idx:53=work_molecule_catalog_anchored-->): 17 molecules, one of them the
  chain's **first parented molecule**. task-0017 (ISRU ascent propellant
  budget) declares task-0015 (Sabatier ISRU) as its parent because its
  `compute()` literally consumes the parent's output — it recomputes the
  parent's canonical hash live on every execution and refuses drifted input,
  and its molecule carries the parent's WMID inside its own hashed content
  plus a `parents_resolution` block citing how the edge resolved. Tampering
  with the parent therefore cascades detection to the child.
- **The first non-trivial cut certificate** (idx 54
  <!--idx:54=cut_certificate_anchored-->): interior {task-0017's molecule},
  boundary {task-0015's WMID} — a real declared provenance edge crosses the
  cut boundary, and the boundary molecule is *referenced, not rebuilt*. That
  is the bound: a cut verifies its interior and only names its inputs. (The
  idx-22 certificate keeps its degenerate-cut record forever; both anchored
  cuts are fully re-proved, generation-locked, on every `metacoin verify`.)

**Proves:** work can verifiably *consume* prior verified work — the provenance
edge is enforced at execution time (parent-hash liveness), at construction
time (a declared edge that cannot resolve fails the build loudly), and at
verification time (edge resolution + DAG check + WMID cascade).
**Deliberately does not:** claim the edge implies independence or usefulness —
the same same-operator boundary and vacant Gate-3 judgment seat apply to the
parented work exactly as to everything else.

## The economy-generation layer — idx 55-56

The economy became GENERATIONAL and the treasury learned to accumulate across
generations — without touching a single anchored record:

- **Economy generation 2** (idx 55
  <!--idx:55=economy_demo_summary_anchored-->): the 17-task era's own
  30-simulated-day economy under schema `economy-log/0.2` — a frozen 17-task
  roster pinned at the idx-53 era, a **day-23** planned tamper drill
  (deliberately not generation 1's day 17, so any log excerpt names its era
  at a glance), earnings only on verified work, initial grant 0. The
  coordinator re-ran the entire generation-2 simulation before anchoring,
  and the record states on-chain that *generation 1 (ledger:19) remains
  anchored and is unaffected; this record starts a new generation, it
  replaces nothing.* The gen-1 replay still reproduces the idx-19 hash to
  the digit — pinned in the selftest and re-proved on every `metacoin
  verify`.
- **The treasury funding extension** (idx 56
  <!--idx:56=treasury_funding_extended-->): funding_roots [ledger:19,
  ledger:55] — each root's fees independently re-derived from its OWN
  generation's re-run (3.0 + 3.0 = 6.0); the carried grant/clawback/
  finalization history restated from the anchored idx 32-36 records with
  citations; conservation exact ON the record (balance 5.2 + outstanding
  0.8 == 6.0); caps and budgets restated **unchanged** from the anchored
  constitution (ledger:31) — a budget change would be a governance event,
  which a funding extension is not allowed to be. The historical idx 31-36
  records keep re-deriving under their original single root, undisturbed.

**Proves:** anchored eras are immutable *and* composable — a new economic era
anchors beside the old one, and the treasury's fee base grows only by
re-derivable, anchored economic activity (still no mint path, conservation
asserted at every step).
**Deliberately does not:** revalue anything — both generations are zero-value
simulated accounting, the drill rejections are planned demonstrations, and
scripted determinism is still not market behavior.

## The governance layer — idx 58-59

The MIP path was exercised end-to-end for the first time, with a real
subject: MIP-0004 ("Concentration measurement epochs and the longitudinal
baseline") ratifies the epoch series of the concentration layer above. The
lifecycle the mip/ directory documents is minimal — a Draft status and
MIP-0001 §7's promise of "transparent on-chain MIPs" — so the exercise
completed it by its minimal honest reading: *Draft* (committed) →
*mechanical check* (`protocol/mip_process.py --check`: required sections,
valid status, every ledger citation resolved against the chain, every
verify-run block executed in a fresh-clone sandbox, file sha256) →
*single-seat decision* → *anchored decision record* (idx 58
<!--idx:58=mip_decision_recorded-->), recorded only behind the same human
`--confirm` gate as participant intake.

The second walk (idx 59 <!--idx:59=mip_decision_recorded-->) closed the
repository's last dangling citation: MIP-0003 ("Operator responsibility")
is the proposal MIP-0002 §3 had cited since its drafting — the human
operator remains responsible; a compliance gate is protection, never a
waiver. It codifies five operator duties *already practiced and
mechanically enforced* (disclosure, deliberate anchoring, key custody,
honest labeling, history immutability), each citing its enforcement, with
executed verification blocks sampling that machinery. The mechanical check
gained a **citation resolver** in the same batch: every MIP cited anywhere
in mip/ must resolve to an existing file, so a dangling citation is now a
named CI failure instead of a months-long silent promise.

Two properties are the point:

- **The seat statement.** The record says, on-chain: *the review seat has
  one occupant and says so* — plural review, voting, and the anti-whale
  dampening MIP-0001 §7 promises do not exist at research stage. A recorded
  decision proves the process ran, not that the decision is wise (the
  Gate-3 vacancy idiom, applied to governance).
- **Immutability-by-citation.** The record pins the decided file's sha256.
  From that moment the committed MIP file is immutable: the governance
  layer of `metacoin verify` and the sweep's mip section recompute the hash
  on every run, an edit breaks re-derivation loudly, and amendments are new
  MIPs.

**Proves:** the governance process is mechanical, gated, and anchored — a
decision leaves a re-derivable record citing an immutable document.
**Deliberately does not:** claim legitimacy beyond one operator's seat — the
process is real; plural review is not, and every record says so.

## The six defeated-attack drills

The chain does not only record successes — it records attacks staged against
itself and their refusal, and re-verification re-proves each refusal on every
run. Across <!--chain:drill_entry_count-->7<!--/chain--> drill-labeled
entries, six distinct attacks:

1. Challenge replay against task-0007 — idx 25, stays refused.
2. Challenge replay against task-0008 — idx 30, stays refused.
3. Gate-3 wrongful grant — challenged (idx 34) and clawed back (idx 35), the
   bounded-failure path proven live (one drill across two entries).
4. Forged heartbeat — idx 38, rejected by signature verification.
5. Forged key rotation — idx 42, rejected from public material alone.
6. Tampered participant bundle — idx 47, refused at the named intake rung.

Every drill is labeled `drill` on its ledger record — defeating your own
staged attack is evidence of mechanism, never presented as detected fraud.
