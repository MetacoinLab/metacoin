# MIP-0007 — The READY-mechanical era: last external-reality gap closed, approval stays human

**Status:** Accepted · **Layer:** Protocol · **Supersedes:** MIP-0006 · **Depends on:** none
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

The second and last of MIP-0005's external-reality gaps closed with chain
evidence: the second device now holds a verified public mirror, attested
by a SIGNED record anchored at idx 72
<!--idx:72=mirror_attestation_anchored-->. That turned MIP-0006's
anchored one-gap era assertion red **by design** — the same trigger, the
same successor path MIP-0006 itself named. This MIP carries the four
release rules forward verbatim, ratifies the mirror criterion's
chain-derived grounding, re-asserts the new era's honest state (zero
named gaps; the full gate reports READY; approval remains HUMAN and the
default remains NO release), and names the two standing opens that no
criterion claims to close: third-party archival and unaffiliated
participation.

## Motivation

MIP-0005 named its two external-reality gaps precisely: "a second
machine or an external participant" (closed at idx 69–70
<!--idx:69=actor_key_registered--><!--idx:70=participant_result_anchored-->,
ratified by MIP-0006 at idx 71 <!--idx:71=mip_decision_recorded-->) and
"a second device for the mirror". On 2026-08-17 the second closed the
same honest way: the macOS arm64 machine — the same registered
participant whose bundle passed the intake ladder — built the public
mirror from its own clone, verified it IDENTICAL against the published
chain, and SIGNED the attested facts (the mirror's chain point, its
manifest hash, the check verdict, and its own machine fingerprint) with
the next unused one-time key under its registered Lamport root. The
coordinator verified six named checks — schema and no private material;
signature under the ACTIVE registered root; one-time-key discipline
ledger-wide; the attested chain point a verified prefix of the live
chain; **the device rule** (the signed fingerprint matches no
coordinator machine on the chain — fingerprint-decided, never declared);
and the honest-scope statement — and anchored the record at idx 72
<!--idx:72=mirror_attestation_anchored-->.

An era assertion that stays green after its era has moved would be the
exact dishonesty the labeling discipline forbids; MIP-0006's blocks went
red on schedule, and this successor moves the assertion through the same
exercised path that accepted MIP-0005 (idx 60
<!--idx:60=mip_decision_recorded-->) and MIP-0006.

## Specification

1. **MIP-0005's four rules remain protocol law, unchanged.** Default is
   NO release; the complete-product standard governs the next release;
   `protocol/release_readiness.py` is the standing mechanical gate and
   READY plus recorded human approval is the only path to a release;
   gaps that depend on external reality are named honestly, never
   simulated away. MIP-0005 and MIP-0006 stay immutable-by-citation
   under their anchored sha256s; only their era-assertion verify-run
   blocks are retired by supersession.
2. **The mirror criterion is PASS, with chain evidence and an honest
   scope.** A verified mirror exists on a device that is not the
   coordinator's: idx 72 <!--idx:72=mirror_attestation_anchored-->, the
   fingerprint carried inside the SIGNED attestation bytes and on the
   record. The scope is stated on the record itself and repeated here:
   a SAME-OPERATOR second-device mirror protects against
   coordinator-disk loss and detects coordinator-side rewriting (a
   DIVERGED verdict from a machine the coordinator's workflow does not
   touch); it is **NOT third-party archival**. An attestation that
   omits that statement is mechanically refused.
3. **The criterion's grounding, ratified.** Criterion 4 derives from
   COMMITTED PUBLIC material only — the anchored attestation record
   plus the shipped evidence bundle (sha-matched, device rule enforced,
   attested chain point a verified prefix) — so it re-derives in any
   fresh clone, CI sandbox, or cold install, exactly like the
   cross-machine criterion. The earlier gitignored
   `external_mirrors.json` config form, never exercised, is retired.
   Attestation FRESHNESS is the weekly sweep's informational job
   (re-attestation at the operator's cadence, each consuming a one-time
   key index); staleness nudges, and never flips the gate — the
   anchored record is the criterion's evidence.
4. **The era assertion: READY-mechanical, approval human.** With every
   external-reality criterion closed, the full-mode gate reports READY —
   and READY changes nothing about releasing: it is necessary, never
   sufficient; the HUMAN approval criterion never converts to a machine
   verdict; fast mode still can never establish READY (asserted below);
   the default remains NO release. The Verification section makes the
   zero-gap state part of this ratified document's proof — so any
   criterion regression, any new gap, or the next era change turns
   these blocks red **by design**, and that red is again the governance
   signal for a successor MIP declaring `Supersedes: MIP-0007`.

## Backwards compatibility

Nothing in the gate's policy, the intake ladder, or any anchored record
changes. The criterion flip this MIP ratifies already happened on-chain
(idx 72) and the gate derived it mechanically; the one retirement (the
never-exercised config form of criterion 4) removes a path that no
record, no document, and no workflow ever used. The chain leads, the
era assertion follows through the MIP path — the designed direction,
now exercised three times.

## Honest limitations

Two opens stand, and no criterion in this MIP claims them: **third-party
archival** (every mirror device belongs to the same operator; a copy
held by someone else remains future work) and **unaffiliated
participation** (every verification path on the chain is the same
operator; the concentration series continues to say so). READY is a
statement about mechanical capability — install, verify, participate,
mirror — not about product quality, demand, or safety; the usefulness
seat remains honestly vacant; reproduction proves reproducibility, not
execution; energy figures remain estimates on hosts without power
telemetry. Not consensus, not payment, not a token; zero-value
research-stage.

## Verification

The fast gate reports the zero-gap era honestly — no named gaps, the
mirror and cross-machine criteria PASS on their chain evidence, and
fast mode itself can never establish READY (the full gate, which runs
the real cold install, is the release instrument):

```verify-run
$ python3 protocol/release_readiness.py --check --fast
VERDICT: NOT-READY — no gaps found, but fast mode skipped expensive checks — fast mode can never establish READY  (trimmed)
```
<!--expect:no gaps found, but fast mode skipped expensive checks-->
<!--expect:fast mode can never establish READY-->
<!--expect:mirror attested by a non-coordinator device-->
<!--expect:idx 70: participant fingerprint differs from every coordinator machine on the chain-->
<!--expect:out-of-band: human decision, never mechanical-->

The gate's honesty invariants hold, carried forward so they never
retire with a superseded era — the HUMAN type never converts to PASS, a
coordinator-device "mirror" can never pass the device rule, and
tampered mirror evidence is refused by its sha:

```verify-run
$ python3 protocol/release_readiness.py --selftest
mirror criterion: a coordinator-device 'mirror' can never pass (the anti-simulation device rule): PASS  (trimmed)
```
<!--expect:coordinator approval is HUMAN-->
<!--expect:a coordinator-device 'mirror' can never pass-->
<!--expect:tampered evidence refused by sha-->
<!--expect:ALL CHECKS BEHAVED CORRECTLY-->
