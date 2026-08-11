# MIP-0005 — Release criteria and the complete-product gate

**Status:** Accepted · **Layer:** Protocol · **Supersedes:** none · **Depends on:** none
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

This MIP codifies the release discipline the project has practiced since
its only release — ratifying the standing policy, inventing nothing — and
ratifies `protocol/release_readiness.py` as the standing mechanical gate
that answers, at any moment, *what stands between us and the next release*,
with named gaps. Four rules: the default is NO release; the next release
must meet the complete-product standard; the gate is the pre-release
instrument and READY plus recorded human approval is the only path to a
release; and gaps that depend on external reality are named honestly,
never simulated away.

## Motivation

The discipline exists because of a distinction the repository has enforced
from the start: a *showcase* demonstrates features; a *product* lets a
stranger complete a loop. Exactly one release has ever shipped — the
`v0.1.0` tag stands as shipped, cut deliberately by the coordinator — and
every batch since has deliberately NOT released, because the next
meaningful release is not "more features shown" but "the full participant
loop completable by an unaffiliated stranger without contacting the
project." That standard needs a mechanical instrument, or distance-to-done
degrades into mood. The gate is that instrument, and this MIP makes the
policy governance law through the exercised MIP path (idx 58
<!--idx:58=mip_decision_recorded-->, idx 59
<!--idx:59=mip_decision_recorded-->).

## Specification

1. **Default is NO release.** Versions, tags, and releases require explicit
   coordinator approval, never automation. No tool in this repository —
   including the gate this MIP ratifies — creates, tags, or publishes a
   release. `v0.1.0` stands as shipped.
2. **The complete-product standard.** The next release must let an
   unaffiliated stranger complete the full loop without contacting the
   project: install → verify → create identity → run verification under
   their own key → bundle → submit → be validated → be anchored → appear in
   their own passport. Partial feature showcases do not qualify, however
   polished.
3. **The mechanical gate.** `protocol/release_readiness.py --check`
   (schema `release-readiness/0.1`) is the standing pre-release instrument.
   Each criterion reports one of four types: PASS (mechanically established
   now), GAP (named, with what-would-close-it), SKIPPED (`--fast` mode
   only, which can therefore never report READY), or HUMAN (out-of-band by
   design). A release may proceed only from a full-mode READY verdict plus
   recorded coordinator approval. The criteria and their groundings:
   - *cold-install acceptance* — executed for real: wheel → fresh venv →
     empty directory → `metacoin verify` must pass from package data alone.
   - *participant loop rehearsed (same-machine)* — chain evidence: a
     registration, a participant-verified bundle (idx 46
     <!--idx:46=participant_result_anchored-->), and a rejection drill
     (idx 47 <!--idx:47=participant_intake_rejected-->).
   - *cross-machine participation* — chain evidence: a verified participant
     bundle whose machine fingerprint matches no coordinator machine on the
     chain. The same-machine rehearsal can never satisfy this, by
     construction.
   - *independent mirror active* — a verified mirror on a device that is
     not the coordinator's (the in-repo `mirror_export/` does not count).
   - *docs verified* — tokens, ledger citations, MIP citations, and the
     era-pinned README verify (command blocks execute in CI's full
     doc_verify run — this criterion stays cheap and unlooped because the
     Verification section below runs the gate itself).
   - *sentry health* — the weekly sweep sentry is wired (workflow tracked,
     tool imports); the latest verdict lives in CI run history and is
     stated as such, not papered over.
   - *governance hygiene* — every MIP citation across `mip/` resolves; a
     dangling citation is a named failure.
   - *open-debt honesty* — a sample molecule rebuild must carry its
     provenance-debt block with every entry labeled and every reduction
     citing its appended evidence; debt is never silently closed.
   - *coordinator approval* — always HUMAN: a human decision, never
     mechanical, and the HUMAN type never converts to PASS.
4. **Honest gap accounting.** Gaps that depend on external reality — a
   second machine or an unaffiliated participant, a second device for the
   mirror — are named as exactly that. Simulating them away (fabricating a
   participant, pointing a "mirror" at the same disk) would satisfy the
   letter of the check by destroying the thing it measures.

The routine sweep reports the gate's verdict under an INFORMATIONAL
heading: NOT-READY is the expected state between releases and can never be
a sweep finding — a change in the gap list deserves a glance, not an alarm.

## Backwards compatibility

Codification only — zero behavior change. The release history is already
exactly what this MIP requires (one deliberate release; no automation has
ever tagged or published anything), and the gate is additive tooling that
reads the chain and the repo and writes nothing. No anchored record is
touched.

## Honest limitations

The gate measures **capability-exercised**, not product quality and not
demand: READY means the mechanical loop is whole, not that the product is
good, wanted, or safe to launch — approval remains a human judgment, and
the HUMAN criterion exists precisely so no dashboard can claim otherwise.
Two of today's gaps are facts about the world, not the code, and the gate
does not pretend a machine can close them. This MIP's own Verification
asserts today's NOT-READY state with its named gaps — so the day a gap
closes, this document's checks go red **by design**: that red is the
governance signal to supersede this MIP's era assertion through the same
MIP path (a successor declaring `Supersedes: MIP-0005` retires these
blocks from execution while this file stays immutable-by-citation). Not
consensus, not payment, not a token; zero-value research-stage.

## Verification

The gate runs, and today it reports NOT-READY with exactly the two
external-reality gaps — today's honest state, made part of this ratified
document's proof:

```verify-run
$ python3 protocol/release_readiness.py --check --fast
VERDICT: NOT-READY — 2 named gap(s) stand between here and the next release  (trimmed)
```
<!--expect:VERDICT: NOT-READY-->
<!--expect:awaits a second machine or an external participant-->
<!--expect:awaits the second device-->

The gate's own honesty invariants hold — the HUMAN type never converts to
PASS, fast mode can never establish READY, and a fixture cross-machine
participant flips its criterion while the same-machine rehearsal never can:

```verify-run
$ python3 protocol/release_readiness.py --selftest
coordinator approval is HUMAN — never PASS, never GAP               : PASS  (trimmed)
```
<!--expect:coordinator approval is HUMAN-->
<!--expect:ALL CHECKS BEHAVED CORRECTLY-->
