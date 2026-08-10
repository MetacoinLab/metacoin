# MIP-0004 — Concentration measurement epochs and the longitudinal baseline

**Status:** Accepted · **Layer:** Protocol · **Supersedes:** none · **Depends on:** none
**Note:** Research specification only. No token exists. Not financial or legal advice.

> **THE DOC CONTRACT.** Every claim in this MIP is mechanically checkable:
> ledger citations are typed (`<!--idx:N=event-->`) and resolved against the
> chain, and the Verification section's command blocks are executed — by
> `protocol/mip_process.py --check` during the lifecycle and (because
> `mip/*.md` is in the scan set) mechanically verified by
> protocol/doc_verify.py on every CI run. Once a decision on this MIP is
> anchored, the anchored record pins this file's sha256: the file becomes
> immutable-by-citation, and any later edit breaks re-derivation loudly.
> Amendments are new MIPs.

## Summary

This MIP ratifies the concentration longitudinal-observation series as
standing protocol machinery: the `aci-epoch-observation/0.1` schema as the
form of every future concentration measurement, the milestone-batched epoch
cadence, and the routine sweep's comparison-point rule (current chain versus
the newest anchored epoch, with the frozen first measurements cited as epoch
zero forever). The series turned the protocol's one-off self-measurement of
its own centralization into a time series with explicit epoch-over-epoch
deltas; this MIP makes that arrangement the ratified rule rather than an
implementation accident.

## Motivation

A single honest measurement is a point, not a story. The protocol anchored
its maximal-concentration baseline deliberately — pairwise ACI over 28 paths
at idx 18 <!--idx:18=aci_baseline_anchored--> and the higher-order profile at
idx 44 <!--idx:44=aci_korder_baseline_anchored--> — precisely so that nobody
would have to take the operator's word for how centralized the system is.
But the number that will eventually matter is a *change*: the first epoch
after an unaffiliated participant anchors verification paths. A change is
only measurable against a series, and a series is only trustworthy if every
point in it re-derives from anchored values. The second epoch (66 paths,
as-of chain point 56) was anchored at idx 57 <!--idx:57=aci_epoch_observed-->
with exactly that discipline: its deltas were computed from the anchored
baselines alone, re-derived — never remembered — and the coordinator rebuilt
the entire observation (citations and deltas included) before anchoring.
This MIP ratifies that discipline as the standing rule.

## Specification

1. **Schema.** `aci-epoch-observation/0.1` (built by
   `protocol/agent_concentration.py --epoch`) is the standing observation
   schema. An observation is the full concentration measurement at a fixed
   as-of chain point — pairwise ACI/EIS, the k-order profile reusing the
   existing machinery verbatim (exact enumeration where feasible at the
   current population; sampled rows always carrying their finite-population
   95% interval, domain-clipped with the clip stated), the Γ concentration
   profile, coverage and worst-case-scored unknown flags — plus citations of
   every prior anchored concentration record and explicit deltas against the
   most recent prior epoch. Per-k values only: no cross-k aggregate key
   exists, by construction.
2. **Delta rule.** The prior side of every delta is read off the cited
   anchored record; the current side is re-derived live. Nothing is
   remembered between epochs.
3. **Cadence.** Epochs are milestone-batched, like catalog generations and
   economy generations: one epoch observation per milestone batch that
   materially grows the path population — never automatic, never continuous.
4. **Anchoring.** Every epoch is anchored via
   `protocol/external_verifier.py --anchor-aci-epoch`; the coordinator
   rebuilds the whole observation at the same as-of point and compares
   `report_hash` before anchoring.
5. **Sweep comparison rule.** The routine sweep's drift section compares the
   current chain against the NEWEST anchored epoch. The frozen first
   measurements (idx 18, idx 44) are cited as epoch zero in every drift
   report, forever — they are never displaced, only preceded.
6. **Process notes (first lifecycle exercise).** The stages exercised for
   this MIP, completing the previously undocumented lifecycle by its minimal
   honest reading: *Draft* (the only stage the existing MIP files document)
   → *mechanical check* (`protocol/mip_process.py --check`: required
   sections, valid status, resolved ledger citations, executed verification
   blocks, file sha256) → *single-seat decision* → *anchored decision
   record* (`mip_decision_recorded`, the "transparent on-chain MIP" endpoint
   that MIP-0001 §7 promises). Anchored MIP files are immutable-by-citation;
   amendments are new MIPs. Numbering: a new MIP takes the next free number
   — MIP-0003 remains reserved by MIP-0002 §3's standing citation for the
   unwritten operator-responsibility MIP.

## Backwards compatibility

The frozen baselines are byte-locked forever: the idx-18 pairwise report and
the idx-44 k-order report must keep re-deriving hash-identical at their
anchored chain points. Both locks are enforced twice — in
`protocol/agent_concentration.py --selftest` (regression-lock checks) and in
`protocol/verify_everything.py --full` (the concentration layer) — and the
epoch machinery is additive: it reads the pairwise and k-order builders and
changed neither. No molecule generation, economy generation, or other
anchored record is touched by this MIP.

## Honest limitations

Every verification path in the series today is operated by the SAME
operator: the series is a longitudinal *self*-observation, and its deltas
describe same-operator path accumulation — never a change in independence.
A rising or flat near-1 ACI is the expected signature of that accumulation.
This MIP's own ratification is equally honest about itself: the review seat
has one occupant and says so. The mechanical checks are real; plural review
is not. Voting — including the anti-whale dampening MIP-0001 §7 promises —
does not exist at research stage, and a recorded decision proves the process
ran, not that the decision is wise. Zero-value research stage; not
consensus, not payment, not a token.

## Verification

The anchored epoch re-derives byte-identically from the published chain
alone — this command rebuilds the full observation at its anchored as-of
point and its output carries the anchored report hash:

```verify-run
$ python3 protocol/agent_concentration.py --epoch --as-of 56 --ledger protocol/ledger_published.json --out epoch_recheck.json
  "report_hash": "916158abc2349675..."  (full observation JSON; trimmed)
```
<!--expect:916158abc234-->

The standing verification stack — including the concentration layer's
regression locks on the frozen idx-18/44 baselines and the epoch rebuild —
passes end to end:

```verify-run
$ python3 protocol/verify_everything.py --quick
RESULT: ALL LAYERS PASS  (trimmed)
```
<!--expect:ALL LAYERS PASS-->
