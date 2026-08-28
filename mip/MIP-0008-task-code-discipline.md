# MIP-0008 — Task-code discipline for the post-18 era: forward-only law, grandfathered by the chain

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

The task library is about to grow past its first eighteen modules into
the CEA/SPICE era, and the code that agents will read as reference
physics deserves a written discipline before that code exists. This MIP
turns four house habits into law for every task module the chain
registers AFTER this MIP's decision index — assertion density inside
`compute()`, a stated bound on every loop, recursion only under a
recorded waiver, and units carried in field names — and ships the
mechanical checker that refuses each violation by name
(`protocol/task_law_check.py`, in the protocol self-test runner and as a
standing CI step). The law is **forward-only by mechanism, not by
promise**: the eighteen existing modules are grandfathered because the
chain referenced them before the law existed, never because a list names
them. The rules are adapted from public engineering sources — Holzmann's
"Power of 10" (IEEE Computer, 2006), the JPL coding standard's waiver
discipline (D-60411), and the Mars Climate Orbiter mishap report
(1999) — and cited as such; no NASA affiliation or endorsement exists or
is implied.

## Motivation

The eighteen modules are edit-frozen for a hard reason, not a soft one:
their bytes feed anchored values. Every module's recorded output hash is
committed on the chain — from task-0001's first locally-verified record
at idx 4 <!--idx:4=self_recompute_result--> to task-0018's at idx 63
<!--idx:63=self_recompute_result--> — the molecule catalogs commit to
their content-addressed spec hashes (the fifth generation at idx 65
<!--idx:65=work_molecule_catalog_anchored-->), and the era-2 transition at
idx 67 <!--idx:67=task_hash_era_recorded--> anchors the corrected pair's
spec hashes by name. Editing a frozen module to satisfy a new rule would
drift those anchored values; a law that demanded it would be asking the
protocol to rewrite its own record. So the discipline must bind only
what comes next.

What comes next is measurable today. The 2026-08-28 audit of the library
found 35 `assert` statements concentrated in five files (the parented
chain's liveness proofs), thirteen of eighteen modules with no in-function
assertion at all, iterative solvers that are bounded by design ("bounded
Newton-Raphson") but whose bounds live in comments and constants rather
than in checkable form, exactly two `while` loops (task-0005's Dijkstra,
structurally bounded by a finite grid and never counter-bounded), and one
narrow recursion — `_sign_safe_zero`, the negative-zero canonicalizer,
copied across the protocol and two adapters — whose bound (the depth of
our own canonical artifacts) is real and unstated. None of that is a
defect in frozen code; all of it is a pattern the next forty tasks should
not inherit.

The sources this MIP adapts say the same thing in their own words.
Holzmann's rule 5: "The odds of intercepting defects increase with
assertion density"; rule 2: "All loops must have a fixed upper-bound";
rule 1: "do not use … direct or indirect recursion". JPL D-60411 on
deviations: "deviations allowed, provided that an adequate justification
is given" — and "an independent institutional approval process must be
followed for significant deviations", which for this protocol is exactly
the MIP path this file is walking, exercised most recently at idx 73
<!--idx:73=mip_decision_recorded-->. The Mars Climate Orbiter mishap
board's root cause, "failure to use metric units in the coding of a ground
software file", is the reason a unit lives in a field's name and not in a
reader's memory. And cFS's ut_assert charter — "explicitly write
verification statements that assert whether a condition is true or
false" — is the assertion culture our self-tests already practice
outside the task modules; this MIP moves it inside.

## Specification

1. **Who the law binds — the chain decides, never a name list.** For
   every task in the registry (`protocol/verifier_cli.py`
   `TASK_MODULES`), the checker computes its *registration index*: the
   lowest ledger index whose payload references the task's id (the same
   `work_molecule._payload_references_task` reading that the
   `recorded_task_count` documentation token uses). The *law index* is
   the index of the ACCEPTED `mip_decision_recorded` record for this MIP.
   A module is **grandfathered** when its registration index exists and
   is lower than the law index (or the law is not yet anchored);
   otherwise it is **bound** — including any module the chain has never
   referenced at all. Grandfathered modules are **skipped by era**: no
   rule below is evaluated against them, and the checker prints that
   skip with the two indices that decided it. The eighteen pre-law
   modules therefore stay exactly as anchored, and the number
   "grandfathered by era: 18" is forward-stable by construction — every
   later registration lands after the law index.
2. **Rule 1 — assertion density.** A bound module's `compute()` carries
   at least two `assert` statements, each with a message. The house
   assertion classes — conservation (a sum closes), bounds (a quantity
   lies in its physical range), known-truth (two derivations agree, or
   a pinned parent hash recomputes) — are what the messages must name;
   the checker enforces the count and the presence of a message, and
   the review enforces the class. An assertion that fails crashes the
   task: stop, don't fudge.
3. **Rule 2 — every loop states its bound.** A bound module's `while`
   test must name its bound (a name containing `MAX`, `BOUND`, or
   `LIMIT`, or an integer literal); `while True` is refused, as are
   `for` loops over the infinite iterators (`count`, `cycle`,
   `repeat`). `for` loops over `range(...)` and finite collections are
   bounded by their iterable and pass as written — already the house
   style ("bounded Newton-Raphson"), now law.
4. **Rule 3 — recursion only with a recorded waiver.** A function that
   reaches itself through the module's call graph is refused unless a
   module-level `P10_WAIVERS` literal carries an entry with all five
   fields: `rule` (`"recursion"`), `function`, `bound` (the stated
   termination bound), `justification` (written), and `approved_by` (a
   MIP id whose file exists in `mip/`). This is the JPL waiver form
   with the MIP process as the independent approval. **The standing
   precedent:** `_sign_safe_zero` (protocol/attest.py and its copies)
   recurses over JSON structure with a real but unstated bound; it
   lives in frozen and protocol code outside this law's reach today,
   and the next time any copy of it is touched inside a bound module it
   needs exactly this waiver — bound stated ("the depth of the module's
   own canonical result"), justification written, MIP cited.
5. **Rule 4 — units in field names.** Every numeric field a bound module
   emits (`int`/`float`, never `bool`) carries its unit as the name's
   last token (`_km`, `_m_s2`, `_dB`, `_kg_per_s`, …), or names a
   declared dimensionless kind (`_count`, `_index`, `_seed`, `_ratio`,
   `_efficiency`, …), or sits beside an explicit string `unit` sibling
   field. The MCO lesson, made mechanical: the checker executes the
   module once and walks the emitted result.
6. **Enforcement ships with the law.** `protocol/task_law_check.py`
   `--check` lints the whole registry (bound modules under rules 1–4
   and MIP-0009's contract clauses; grandfathered modules skipped by
   era); `--module PATH --task-id task-NNNN` lints one file while it is
   being authored — the task-addition path; `--selftest` proves the
   checker on fixtures: a compliant synthetic task passes, each
   violation is refused by its rule name, a waivered recursion passes
   and a waiver citing a non-existent MIP is refused, and the real
   registry reports all eighteen pre-law modules skipped by era. The
   self-test runs in `protocol/run_protocol_selftests.sh`; `--check`
   runs as a standing CI step. Standard library only; the module is
   never executed for the static rules and executed exactly once for
   the runtime ones.
7. **Attribution, standing.** Rules 1–3 adapt Holzmann's "The Power of
   10: Rules for Developing Safety-Critical Code" (IEEE Computer, June
   2006) and the waiver discipline of the JPL Institutional Coding
   Standard for the C Programming Language (JPL D-60411, 2009); rule 4
   adapts the Mars Climate Orbiter Mishap Investigation Board Phase I
   Report (November 1999); the in-function assertion culture cites the
   OSAL ut_assert README. All are public documents, cited by title and
   date; adapting a discipline claims no relationship with its authors.
   No NASA affiliation or endorsement.

## Backwards compatibility

No anchored record, no frozen module, no existing self-test, and no
adapter changes. The eighteen modules keep their bytes, their anchored
output hashes, their era-2 mapping, and their catalog spec hashes; the
checker reports each as grandfathered with the indices that decided it.
The only behavioral additions are one self-test in the protocol runner
(the suite count grows by one, and the documentation tokens re-render
to say so) and one CI step that today evaluates zero bound modules and
reports CLEAN. Task modules authored after this MIP's decision — the
CEA family, the SPICE beachhead, and everything after — are bound from
their first registration, and their authors get the checker before
their first anchored record exists.

## Honest limitations

The checker verifies the *form* of the discipline, not its *truth*: two
messaged assertions can be vacuous, a named `MAX_ITER` can be set
absurdly high, a waiver's justification can be thin, and a unit suffix
can be wrong — the review seat, which has one occupant and says so on
every governance record, is where those are caught. Rule 1's class
discipline (conservation / bounds / known-truth) is stated as law and
enforced by review only. Rule 2 accepts a literal integer as a bound,
which is a bound and not necessarily a good one. Recursion detection is
by name within the module's own call graph; recursion through imported
helpers is invisible to it by design (an imported helper is governed
where it lives). The unit table is a finite vocabulary and will grow by
amendment when the CEA/SPICE tasks need units it lacks — a missing unit
is refused loudly, never guessed. Grandfathering by era means a module
registered before the law but never anchored would be bound; no such
module exists, and the self-test proves the rule on fixtures. Not
consensus, not payment, not a token; zero-value research-stage.

## Verification

The registry today: eighteen modules, every one grandfathered by its
registration era (first anchored reference below the law index) and
skipped — no bound module exists yet, and the verdict is CLEAN:

```verify-run
$ python3 protocol/task_law_check.py --check
VERDICT: TASK-LAW CLEAN — grandfathered modules are skipped by registration era, never by name; bound modules answer to MIP-0008 and MIP-0009  (trimmed)
```
<!--expect:grandfathered by era: 18-->
<!--expect:violations: 0-->
<!--expect:VERDICT: TASK-LAW CLEAN-->

The checker's own proof — a compliant synthetic task passes, each of the
four rules refuses its violation by name, a waivered recursion passes and
a waiver citing an unwritten MIP is refused, and the grandfathering
mechanics hold on a fixture chain:

```verify-run
$ python3 protocol/task_law_check.py --selftest
unwaivered recursion refused by name (R3)                           : PASS  (trimmed)
```
<!--expect:compliant synthetic task passes (bound, 0 violations)-->
<!--expect:grandfathered module skipped by era (rules not run)-->
<!--expect:missing assertions refused by name (R1)-->
<!--expect:unbounded loop refused by name (R2)-->
<!--expect:unwaivered recursion refused by name (R3)-->
<!--expect:waiver citing a non-existent MIP refused (R3)-->
<!--expect:unitless numeric field refused by name (R4)-->
<!--expect:ALL CHECKS BEHAVED CORRECTLY-->
