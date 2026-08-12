# MIP-0006 — The cross-machine era: release criteria carried forward, first external-reality gap closed

**Status:** Accepted · **Layer:** Protocol · **Supersedes:** MIP-0005 · **Depends on:** none
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

This MIP does exactly what MIP-0005 said its successor would do, on exactly
the trigger MIP-0005 named. The cross-machine criterion of the release gate
closed with chain evidence — the first cross-machine participation,
anchored at idx 69 <!--idx:69=actor_key_registered--> and idx 70
<!--idx:70=participant_result_anchored--> — which turned MIP-0005's
anchored era assertion ("two named gaps") red **by design**. Per MIP-0005's
own Honest limitations section, that red is the governance signal for a
successor: this MIP carries all four of MIP-0005's rules forward verbatim,
re-asserts the gate's honest state in the new era (one named
external-reality gap remains), and retires MIP-0005's era-assertion blocks
from execution while that file stays immutable-by-citation.

## Motivation

MIP-0005 anchored the release discipline together with a snapshot of the
world as it stood: NOT-READY with two gaps that only external reality could
close — "a second machine or an external participant" and "the second
device" for the mirror. On 2026-08-11 the first of those closed honestly.
A second physical machine (macOS arm64, a different platform family from
every coordinator machine on the chain) ran the public verifier
end-to-end, reproduced all 23 recorded task results byte-for-byte under
the era-2 canonical rule (idx 67 <!--idx:67=task_hash_era_recorded-->),
signed the bundle under its own one-time key, and passed the full six-rung
intake ladder. The registration and the participant-verified record are
anchored at idx 69 <!--idx:69=actor_key_registered--> and idx 70
<!--idx:70=participant_result_anchored-->, both labeled with the topology
the fingerprint — never the declaration — decided:
`cross-machine-same-operator`.

An era assertion that stays green after its era has moved would be the
exact dishonesty the labeling discipline forbids. The MIP path itself is
how the assertion moves: a successor, through the same exercised process
that accepted MIP-0005 (idx 60 <!--idx:60=mip_decision_recorded-->).

## Specification

1. **MIP-0005's four rules remain protocol law, unchanged.** Default is NO
   release; the complete-product standard governs the next release;
   `protocol/release_readiness.py` is the standing mechanical gate and
   READY plus recorded human approval is the only path to a release; gaps
   that depend on external reality are named honestly, never simulated
   away. This MIP restates them by reference and changes none of them —
   MIP-0005's file stays immutable-by-citation under its anchored sha256,
   and only its *era-assertion verify-run blocks* are retired from
   execution by this supersession.
2. **The cross-machine criterion is PASS, with chain evidence.** A verified
   participant bundle whose machine fingerprint matches no coordinator
   machine on the chain exists: idx 70
   <!--idx:70=participant_result_anchored-->, fingerprint carried on the
   record, key root registered at idx 69
   <!--idx:69=actor_key_registered-->. The gate derives this criterion
   from the chain and the anchored evidence bundle — never from this
   document.
3. **The honest topology label is part of the closure.** Both records say
   `cross-machine-same-operator`: the participant is the SAME operator on
   a second machine, decided by the machine fingerprint, with the
   relationship claim (`same-operator-claimed`) carried verbatim and
   endorsing nothing. Cross-machine closes the "second machine" half of
   MIP-0005's gap wording exactly; the unaffiliated-participant milestone
   REMAINS OPEN and is not claimed, diminished, or approximated by this
   MIP.
4. **The era assertion, updated.** The gate today reports NOT-READY with
   exactly one named external-reality gap: the independent mirror awaits
   the second device. The Verification section makes that state part of
   this ratified document's proof — so the day the mirror gap closes (or
   any criterion regresses), this document's checks go red **by design**,
   and that red is again the governance signal for a successor MIP
   declaring `Supersedes: MIP-0006`.

## Backwards compatibility

Nothing in the gate, the intake ladder, or any anchored record changes.
The criterion flip this MIP ratifies already happened on-chain (idx 69–70)
and the gate derived it mechanically; this MIP is the governance layer
catching up to anchored reality, which is the designed direction — the
chain leads, the era assertion follows through the MIP path.

## Honest limitations

Cross-machine is NOT independence: the same operator controls both
machines, every verification path on the chain remains same-operator, and
the concentration series is unmoved — a `-claimed` relationship can never
lower measured concentration, and a second machine does not add a second
party. The gate remains NOT-READY; fast mode still can never report READY;
coordinator approval remains HUMAN and never converts to PASS. The next
meaningful milestone is still operational, not code: an unaffiliated third
party running the public verifier and submitting a result. Not consensus,
not payment, not a token; zero-value research-stage.

## Verification

The gate runs, and today it reports NOT-READY with the cross-machine
criterion PASS on its chain evidence and exactly one named
external-reality gap remaining — this era's honest state, made part of
this ratified document's proof:

```verify-run
$ python3 protocol/release_readiness.py --check --fast
VERDICT: NOT-READY — 1 named gap(s) stand between here and the next release  (trimmed)
```
<!--expect:VERDICT: NOT-READY-->
<!--expect:idx 70: participant fingerprint differs from every coordinator machine on the chain-->
<!--expect:awaits the second device-->

The gate's own honesty invariants hold, carried forward from MIP-0005's
proof so they never retire with it — the HUMAN type never converts to
PASS, fast mode can never establish READY, and a fixture cross-machine
participant flips its criterion while the same-machine rehearsal never
can:

```verify-run
$ python3 protocol/release_readiness.py --selftest
coordinator approval is HUMAN — never PASS, never GAP               : PASS  (trimmed)
```
<!--expect:coordinator approval is HUMAN-->
<!--expect:ALL CHECKS BEHAVED CORRECTLY-->
