# MIP-0003 — Operator responsibility

**Status:** Draft · **Layer:** Protocol · **Supersedes:** none · **Depends on:** none
**Note:** Research specification only. No token exists. Not financial or legal advice.

> **THE DOC CONTRACT.** Every claim in this MIP is mechanically checkable:
> ledger citations are typed (`<!--idx:N=event-->`) and resolved against the
> chain, and the Verification section's command blocks are executed — by
> `protocol/mip_process.py --check` during the lifecycle and, because
> `mip/*.md` is in the scan set, mechanically verified by
> protocol/doc_verify.py on every CI run. Once a decision on this MIP is
> anchored, the anchored record pins this file's sha256: the file becomes
> immutable-by-citation, and amendments are new MIPs.

## Summary

This MIP is the operator-responsibility proposal that MIP-0002 §3 has cited
since its drafting: the human operator remains responsible for what they run
and what they anchor — a compliance gate is protection, never a waiver. Its
content principle: **it codifies operator responsibilities ALREADY PRACTICED
and mechanically enforced in this repository — it ratifies existing norms
and invents nothing.** Every duty below cites the machinery that enforces it
today, and the Verification section executes a sample of that machinery.
Where MIP-0002 §3 speaks of legal responsibility, this MIP adds the honest
research-stage rider: nothing here is legal advice, counsel is required
before anything launches, and no gate or attestation transfers the
operator's responsibility to the tooling.

## Motivation

MIP-0002 §3 promised that operator responsibility would be specified
("The human operator remains legally responsible (MIP-0003); this gate is
protection, not a waiver") — and for months that citation pointed at
nothing. In a same-operator research stage, operator duties are not a
side concern; they are the whole trust story: every seat is occupied by one
operator, so what that operator discloses, gates, guards, labels, and
refuses to rewrite is exactly what a reader can trust. The protocol has
enforced these duties mechanically from the start; writing them down and
ratifying them through the governance path closes the repository's last
dangling MIP citation with content the chain already backs.

## Specification

The five duties. Each cites its standing enforcement — the citation IS the
audit trail.

1. **Disclosure.** Every anchored record names the operator relationship
   (`operator_relationship`, the honesty envelope in
   `protocol/external_verifier.py`). Unknown or missing relationship
   metadata scores as WORST-CASE dependence in the concentration
   measurement (`protocol/agent_concentration.py` — an unknown can never
   masquerade as independence), and participant-declared relationships are
   recorded with the `-claimed` suffix, honored as declarations and never
   endorsed (participant intake, idx 46
   <!--idx:46=participant_result_anchored-->; an unrecognized declaration
   joins the unknown pool and cannot lower measured concentration).
2. **Deliberate anchoring.** No auto-anchoring path exists: participant
   intake writes nothing without the human `--confirm` gate (a tampered
   bundle was refused at its named rung, idx 47
   <!--idx:47=participant_intake_rejected-->), MIP decisions dry-run by
   default (`protocol/mip_process.py --record-decision`, idx 58
   <!--idx:58=mip_decision_recorded-->), and every anchor of a submitted
   artifact is coordinator-reconfirmed — independently recomputed from the
   chain — before the append (the `coordinator_reconfirmed` block on the
   anchored records).
3. **Key custody.** One-time signature discipline is enforced by a
   ledger-wide, cross-record-type reuse scan (`protocol/challenge_response.py`
   — reuse is a hard reject, listed first; the planned reuse drill at idx 25
   <!--idx:25=challenge_response_result--> stays refuted forever); rotation
   happens before exhaustion with staged successor reserves (idx 41
   <!--idx:41=actor_key_rotated-->, forged rotation refused from public
   material at idx 42 <!--idx:42=actor_key_rotation_rejected-->); private
   material is never committed or transmitted (mechanical private-material
   scans in registration, rotation, and intake paths —
   `protocol/actor_identity.py` refuses declarations carrying secrets); and
   the continuity kit lives offline with the stolen-kit honesty stated in
   the kit itself: reserves protect AVAILABILITY, not confidentiality — a
   stolen kit yields both chains, and the kit manifest says so
   (`protocol/continuity.py`).
4. **Honest labeling.** Drills are labeled drills on their records, never
   "detected fraud" (the defeated-attack corpus, e.g. idx 38
   <!--idx:38=heartbeat_rejected-->); simulated time is labeled simulated
   (the economy's `simulated_time` label — day indices, never wall-clock);
   estimates are labeled estimated (metering energy, idx 20
   <!--idx:20=metering_evidence_anchored-->, fixed at an assumed nameplate
   figure and stated as such); vacant judgment seats are declared vacant
   (Gate-3 usefulness "not-assessed"; the single-occupant review seat on
   idx 58); and failures are anchored as facts (a mismatch is an audit
   event, not a suppressed error).
5. **History immutability.** The ledger is append-only and corrections are
   append-only too: provenance debt is reduced only by APPENDING evidence
   (the metering paydown at idx 20-21 with `debt_reduction` records that
   preserve the debt history), anchored generations are regression-locked
   forever (catalog, economy, concentration and cut anchors all re-derive
   at their own chain points on every `metacoin verify`), and anchored MIP
   files are immutable-by-citation. The operator's duty is to extend the
   record, never to rewrite it — and the tooling makes rewriting loud.

## Backwards compatibility

Codification only — zero behavior change. Every mechanism cited above
exists and is exercised by the self-test suites and CI today; this MIP adds
no new enforcement, changes no schema, and touches no anchored record. The
duties bind the operator, not the code.

## Honest limitations

A single operator holds every seat today — coordinator, verifier, reviewer,
and custodian — so these duties are self-imposed and self-audited until
plural operators exist. The enforcement citations are the audit: each duty
names machinery a reader can run, which is the strongest form of
self-imposed duty available at research stage, and still not independence.
The legal-responsibility language inherited from MIP-0002 §3 is a statement
of non-waiver, not legal analysis: not legal advice, counsel required
before any launch. Zero-value research stage; not consensus, not payment,
not a token.

## Verification

Deliberate anchoring — the no-write-without-confirm gate is selftest-proven
(alongside the whole MIP lifecycle ladder):

```verify-run
$ python3 protocol/mip_process.py --selftest
no write without --confirm (ledger byte-identical; record shown)    : PASS  (trimmed)
```
<!--expect:no write without --confirm-->
<!--expect:ALL CHECKS BEHAVED CORRECTLY-->

Disclosure — unknown relationship metadata scores worst-case dependence,
never independence:

```verify-run
$ python3 -c "import sys; sys.path.insert(0,'.'); from protocol.agent_concentration import score_pair; print('unknown-scores-worst-case', score_pair({}, {})['operator'])"
unknown-scores-worst-case 1.0
```
<!--expect:unknown-scores-worst-case 1.0-->

Key custody — public declarations carry no private material, and one-time
reuse is rejected with the reuse reason first:

```verify-run
$ python3 protocol/actor_identity.py --selftest
public declaration carries no private material                   : PASS  (trimmed)
```
<!--expect:public declaration carries no private material-->
<!--expect:reuse reason first-->

Honest labeling — simulated time is a mandatory label, asserted true:

```verify-run
$ python3 -c "import sys; sys.path.insert(0,'.'); from demo.economy_demo import LABELS; print('simulated_time-label', LABELS['simulated_time'])"
simulated_time-label True
```
<!--expect:simulated_time-label True-->

History immutability — mutation, deletion, reordering, and insertion are
all detected by chain verification:

```verify-run
$ python3 protocol/ledger.py
DELETE ENTRY: DETECTED  (trimmed)
```
<!--expect:DELETE ENTRY: DETECTED-->
<!--expect:HONEST STILL VERIFIES: PASS-->
