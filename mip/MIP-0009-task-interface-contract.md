# MIP-0009 — The task interface contract: the protocol's typed port, ratified as law

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

Every task module the protocol has ever verified exposes the same three
functions and emits the same four-key result, serialized by the same
canonical rule, rounded at the same boundary, hashed the same way, and
registered at the same points — a contract that has held de facto from
the first locally-verified record at idx 4
<!--idx:4=self_recompute_result--> through the eighteenth at idx 63
<!--idx:63=self_recompute_result-->, and that every adapter (Inspect,
HAL, the baseline harness, the external verifier) depends on without a
document to cite. This MIP writes the contract down as law and frames it
the way flight software frames component boundaries: as a **typed
port** — components interact only through the contract, and a violation
is refused loudly, never coerced. It is enforced by the same checker
MIP-0008 ships (`protocol/task_law_check.py`), forward-only under the same
era mechanics, and it becomes the citable specification that the
CEA/SPICE task authoring will follow. The framing is adapted from the
public F´ flight-software framework's typed-port doctrine and cited as
such; no NASA affiliation or endorsement exists or is implied.

## Motivation

A convention that every consumer relies on and no document states is a
contract waiting to be broken by accident. Four facts on the chain make
the contract worth ratifying now. First, it already carries the
protocol's evidence: the locally-verified records, the agent-verifier
attestations, and the five molecule-catalog generations (the fifth at
idx 65 <!--idx:65=work_molecule_catalog_anchored-->) all commit to output
hashes and spec hashes computed exactly as this MIP specifies. Second,
the one time the canonical rule was found wanting — the IEEE-754
negative-zero divergence that broke cross-platform bit-reproducibility —
the protocol repaired it append-only as the era-2 rule at idx 67
<!--idx:67=task_hash_era_recorded-->, and that rule ("sign-of-zero-free")
has lived in verifier code rather than in a specification ever since.
Third, the integrations layer now ships three adapters that re-implement
the contract's consumer side, and a fourth (the CEA family) is about to
be authored against it. Fourth, the library is about to grow, and the
authors of the next modules should be able to read one page and know
exactly what a task IS.

The doctrine this MIP adapts is F´'s: components expose typed ports, the
framework checks the types at the boundary, and nothing crosses a port
that does not match its type. For MetaCoin the port is
`compute() / canonical_json() / output_hash()`; the "type" is the four-key
result plus the canonical, rounding, and hashing rules; and the framework
check is the contract clause set of `protocol/task_law_check.py`. The MIP
path that ratifies it is the one exercised at idx 73
<!--idx:73=mip_decision_recorded-->.

## Specification

1. **The port.** A task module exposes exactly three public functions:
   `compute() -> dict` (no parameters; deterministic; no randomness
   beyond a fixed, recorded seed), `canonical_json(result: dict) -> str`,
   and `output_hash(result: dict) -> str`. Optional declared parentage is
   a module-level `PARENT_TASKS` list of short task ids, and a parented
   task recomputes its parent live and asserts the pinned parent hash —
   the executable provenance edge task-0017 and task-0018 already carry.
2. **The four-key result (clause C1).** `compute()` returns a dict with
   exactly the keys `task_id`, `inputs`, `results`, `summary`. `task_id`
   is a string beginning with the registered short id (`task-NNNN`);
   `inputs` carries every fixed constant that participates in the hash
   (including the seed and the rounding decimals); `results` carries the
   per-step or per-quantity records; `summary` carries the headline
   figures — including an honest negative when the physics says no.
3. **The canonical rule, era 2 (clause C2).** `canonical_json` returns
   `json.dumps` of the result with `sort_keys=True`, separators
   `(",", ":")`, `ensure_ascii=True`, and every float normalized
   sign-of-zero-free (`-0.0 → 0.0`, recursively through dicts and
   lists) — the rule anchored at idx 67. The checker proves it twice: on
   the module's own result, and on a probe containing `-0.0` inside a
   list and a dict. A bound module's `canonical_json` therefore carries
   the normalization itself rather than relying on the verifier layer to
   apply it.
4. **The rounding boundary (clause C3).** Every float the result carries
   is stable under `round(x, 6)`: rounding happens at the output
   boundary, before serialization, at six decimals — the library-wide
   `ROUND_DECIMALS` — so re-runs are byte-identical across platforms.
5. **The output hash (clause C4).** `output_hash(result)` is the SHA-256
   hex digest of `canonical_json(result).encode("utf-8")` — the Gate-2
   reproducibility value that every verifier recomputes and every
   anchored task record carries.
6. **The spec hash.** A task's spec hash is the SHA-256 of the module
   source file's exact bytes in the checkout being verified — the
   content address the molecule catalogs commit to, and the reason
   anchored modules are edit-frozen. The checker prints it per module.
7. **The docstring tags (clause C5).** The module docstring names its
   NASA Technology Taxonomy tag (`TXnn`) and carries both standing
   disclaimers — "No NASA affiliation or endorsement" and "Not
   financial" (or legal, or flight-engineering) advice — so a reference
   module never travels without its honesty.
8. **The five registration points (clause C6).** A task is registered
   when its id appears at all of: `protocol/verifier_cli.py`
   `TASK_MODULES` (the registry the checker walks), `demo/verify_gates.py`
   `_TASK_REGISTRY`, `demo/run_all_selftests.sh` (the module runs as its
   own self-test), `integrations/core.py` `TASK_MODULES` (the adapter
   roster, with parent edges), and `protocol/gate3_process.py`
   `TASK_TAXONOMY`. A bound id missing at any point is refused by name.
9. **Refuse, never coerce — the typed-port rule.** Consumers of the port
   (verifiers, adapters, the molecule builder) depend on nothing beyond
   clauses 1–8; a result that violates a clause is refused with the
   clause named, never repaired, re-rounded, or re-serialized on the
   consumer's side. The checker is the boundary check; the adapters are
   its clients.
10. **Era and enforcement — shared with MIP-0008.** The contract binds
    modules by the same registration-era rule (registration index vs. the
    law index of MIP-0008's decision): grandfathered modules are skipped
    by era — their canonical form is honored by the verifier layer's
    era-2 mapping at idx 67, which is why their own `canonical_json` need
    not carry the normalization — and bound modules answer to clauses
    C1–C6 in `protocol/task_law_check.py --check`, in CI and in the
    task-addition path. This file is the citable specification: a new
    task's author reads clauses 1–9 and runs `--module PATH --task-id`.
11. **Attribution, standing.** The typed-port framing adapts the public
    F´ Flight Software Framework documentation (NASA JPL, open source)
    and Bocchino et al.'s description of its component-port model; the
    canonical rule's history is the protocol's own record. Public
    sources, cited by title; no NASA affiliation or endorsement.

## Backwards compatibility

Nothing changes for any anchored record, frozen module, verifier, or
adapter: the contract ratified here is the one they already implement,
and the eighteen pre-law modules are skipped by era exactly as under
MIP-0008 (their un-normalized `canonical_json` is honored through the
era-2 verifier-layer mapping, which is why clause 3's in-module
normalization binds forward only). The integrations' `score_completion`
core, the external verifier, and the molecule builder remain the
contract's consumers, unchanged; they gain a specification to cite.

## Honest limitations

The contract specifies the shape of a task, not its worth: a module can
satisfy every clause and compute something useless — the usefulness seat
remains honestly vacant. Clause C2's probe proves sign-of-zero handling on
one synthetic structure, not on every float a module might emit; clause
C3 proves rounding stability of the emitted values, not that six decimals
is the right precision for a given physics. Clause C6 checks presence at
the five points, not that the roster entries agree with one another
beyond the id (parent edges in the integrations roster are review-checked).
The spec hash is a content address of bytes in a checkout, not a proof
that those bytes were the ones anchored — the anchored records and the
verifier's commit pins carry that. Same-operator custody throughout; the
review seat has one occupant and says so. Not consensus, not payment, not
a token; zero-value research-stage.

## Verification

The contract's boundary check runs over the registry: every module today
is grandfathered by era and skipped, no bound module exists, and the
verdict is CLEAN:

```verify-run
$ python3 protocol/task_law_check.py --check
VERDICT: TASK-LAW CLEAN — grandfathered modules are skipped by registration era, never by name; bound modules answer to MIP-0008 and MIP-0009  (trimmed)
```
<!--expect:grandfathered by era: 18-->
<!--expect:bound modules: 0-->
<!--expect:VERDICT: TASK-LAW CLEAN-->

Each contract clause refuses its breach by name on fixtures — a three-key
result, a canonical form that is not sign-of-zero-free, an unrounded
float, an output hash not taken over the canonical bytes, a docstring
without its tags — and an unregistered id is refused at every missing
registration point:

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
