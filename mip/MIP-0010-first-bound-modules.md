# MIP-0010 — The first bound modules: the contract carried forward, its zero-bound assertion retired

**Status:** Accepted · **Layer:** Protocol · **Supersedes:** MIP-0009 · **Depends on:** none
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

MIP-0009 (idx 75 <!--idx:75=mip_decision_recorded-->) ratified the task
interface contract and, in its Verification section, asserted the state of
the registry on the day it was written: "bound modules: 0". The first two
modules born under the law — task-0019 and task-0020, the CEA-pinned
Sabatier equilibrium family — registered the next day, and that assertion
went red **by design**: the same mechanism by which MIP-0005's and
MIP-0006's era assertions went red when their eras moved (idx 71
<!--idx:71=mip_decision_recorded-->, idx 73
<!--idx:73=mip_decision_recorded-->). This MIP is the named successor:
it carries MIP-0009's contract forward verbatim by reference, changes no
clause, retires only the zero-bound verify-run assertion, and replaces it
with the forward-stable form the law's own checker prints — the
grandfathered count (fixed by era at eighteen) and the violation count
(zero), never the bound count, which is meant to grow.

## Motivation

A frozen document must not assert a number that the protocol's own
progress is designed to change. MIP-0008 (idx 74
<!--idx:74=mip_decision_recorded-->) stated the era mechanics precisely —
"the number 'grandfathered by era: 18' is forward-stable by construction —
every later registration lands after the law index" — and its own
verification block expected exactly that stable number. MIP-0009's block
expected the complementary, unstable one. The first bound registration
(task-0019's first anchored reference lands after idx 74, so the chain,
not a list, binds it) proved the point within a day, and the honest
response is the one the governance path already exercised three times: a
successor MIP, through the same lifecycle, saying plainly what moved and
what did not.

What did not move: every clause of the contract. task-0019 and task-0020
were written against MIP-0009's text, linted by `protocol/task_law_check.py`
as bound modules, and passed all nine rules — after the checker first
refused four real violations in their drafts (an exact-SI constant emitted
unrounded, a disclaimer split across a line break, stoichiometry keys
without units, a 1e-9 tolerance that cannot live at six decimals). The
law's first enforcement was against its own authors' code, and it held.

## Specification

1. **MIP-0009's contract remains protocol law, unchanged.** Clauses 1–11
   of MIP-0009 — the port, the four-key result (C1), the era-2 canonical
   rule (C2), the six-decimal boundary (C3), the output hash (C4), the
   spec hash, the docstring tags (C5), the five registration points (C6),
   refuse-never-coerce, the shared era mechanics, and the attribution —
   are restated here by reference and none is amended. MIP-0009's file
   stays immutable-by-citation under its anchored sha256; only its
   verify-run blocks are retired from execution by this supersession.
2. **The forward-stable verification form.** A MIP that asserts the
   registry's state asserts only quantities the era mechanics hold fixed:
   the grandfathered count (eighteen, by the law index at idx 74) and the
   violation count (zero, or the build is red). The bound count is a
   progress figure and is never an expectation in a frozen file.
3. **The first bound modules, named for the record.** task-0019
   (Sabatier equilibrium constant from the pinned NASA CEA polynomials)
   and task-0020 (equilibrium conversion with an honest negative at the
   700 K / 1 bar reference point) are the first modules the law binds;
   their registration records follow this decision on the chain.

## Backwards compatibility

Nothing changes for any anchored record, frozen module, verifier, adapter,
or the checker: the contract's text, the checker's clauses, and the era
mechanics are exactly those of MIP-0008/MIP-0009. The single effect is
that MIP-0009's verify-run blocks no longer execute in CI (retired by
supersession, the file untouched), and this MIP's blocks execute in their
place.

## Honest limitations

This MIP corrects an authoring error in a frozen governance file by the
only honest route the protocol allows — a successor, not an edit — and
says so. It does not claim the checker is complete: rule 1's assertion
classes remain review-enforced, the unit vocabulary grows by amendment
(this batch added `dimensionless`/`unitless` and the J/mol family), and
the four refused violations were caught by form, not by physics. Same
operator, single review seat, and every record says so. Not consensus,
not payment, not a token; zero-value research-stage.

## Verification

The law's checker over the whole registry — the eighteen pre-law modules
grandfathered by era and skipped, every bound module clean, the verdict
CLEAN (the bound count is printed and deliberately not expected):

```verify-run
$ python3 protocol/task_law_check.py --check
VERDICT: TASK-LAW CLEAN — grandfathered modules are skipped by registration era, never by name; bound modules answer to MIP-0008 and MIP-0009  (trimmed)
```
<!--expect:grandfathered by era: 18-->
<!--expect:violations: 0-->
<!--expect:VERDICT: TASK-LAW CLEAN-->

The contract's clause-by-clause refusals still hold on fixtures — carried
forward from MIP-0009 so they never retire with the superseded block:

```verify-run
$ python3 protocol/task_law_check.py --selftest
contract breach — canonical not sign-of-zero-free (C2)              : PASS  (trimmed)
```
<!--expect:contract breach — not the four-key dict (C1)-->
<!--expect:contract breach — canonical not sign-of-zero-free (C2)-->
<!--expect:contract breach — unrounded float (C3)-->
<!--expect:contract breach — output_hash not over canonical (C4)-->
<!--expect:contract breach — docstring lacks TX tag/disclaimers (C5)-->
<!--expect:C6 names every missing registration point for an unregistered id (4)-->
<!--expect:ALL CHECKS BEHAVED CORRECTLY-->
