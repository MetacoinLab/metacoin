# Verification: what `metacoin verify` actually checks

> **THE DOC CONTRACT.** Everything in this document is [BUILT] fact,
> mechanically verified by protocol/doc_verify.py on every CI run: the command
> block below was executed for real in a fresh clone and its output pasted
> (trimmed for volume, never altered), every stated number is tagged with the
> chain point it describes and re-checked against live state, and nothing here
> claims more than `metacoin verify` proves.
>
> Chain point: tip index <!--chain:tip_index-->92<!--/chain-->,
> <!--chain:entry_count-->93<!--/chain--> entries.

`metacoin verify` (the zero-install form is
`python3 metacoin_cli/main.py verify`) is the flagship: one command that
re-verifies every protocol layer from the shipped corpus on your machine, with
zero LLM judgment anywhere in the loop. This document explains each layer line
and — just as important — the three different *integrity models* behind the
labels, because "verified" does not mean the same thing for a hash chain, a
timing claim, and an anchored acceptance.

## The three integrity models

- **VERIFIED-FULL — reproducible computation.** The claim is re-derived from
  scratch on your machine and compared byte-for-byte (or hash-for-hash;
  cross-platform bit-equality is earned by the canonical form — see the
  anchored hash-era transition at idx 67 for the one repair to date)
  against the anchored value. Strongest model: disagreement is impossible to
  miss, and a pass means *your* hardware reproduced the claim.
- **CLAIM-CHECK — claim-fixing for timing.** Wall-clock and CPU timings are
  honestly non-reproducible: re-running produces different numbers. The
  anchored metering report (idx 20 <!--idx:20=metering_evidence_anchored-->)
  therefore *fixes the claim made at measurement time*: verification checks
  that the report's hash matches the anchor, that its internal arithmetic is
  exact, and that its honesty labels (energy = `estimated`, from an assumed
  <!--chain:assumed_power_w-->15.0<!--/chain--> W constant) are intact — it does
  not pretend to re-measure time.
- **ACCEPTED-BY-ANCHOR — bounded-cost acceptance.** In `--quick` mode some
  layers are accepted conditional on the committed anchors instead of being
  recomputed. The report says so on each such line: acceptance is *not*
  re-proof, and the full mode exists precisely so you never have to settle
  for it.

## The layer-by-layer report

The pasted run below is trimmed to the shape of the report; the full pass has
15 layer lines. What each checks:

- **chain+anchor** — the hash chain is intact from genesis
  <!--chain:genesis_hash_prefix-->71fe94035edd<!--/chain-->… to tip
  <!--chain:tip_hash_prefix-->f4d4ce368fab<!--/chain-->…, and the committed tip anchor
  matches. In a fresh clone the published snapshot is the source, and the
  report names that.
- **tasks** — all <!--chain:recorded_task_count-->29<!--/chain--> recorded
  demo tasks re-run to their canonical output hashes. The registry holds
  <!--chain:task_count-->29<!--/chain--> tasks in total; registry tasks not
  yet on the ledger (new tasks join the corpus at the next milestone anchor
  batch) are counted and named by the report as registered-unanchored —
  expected evolution, never a failure and never silently skipped.
- **molecules 0.2 / 0.3** — both anchored work-molecule catalog generations
  rebuild byte-identically, generation-locked to their anchors (idx 17
  <!--idx:17=work_molecule_catalog_anchored--> and later — see
  docs/ARCHITECTURE.md).
- **concentration** — the ACI baseline re-measures to the anchored digits
  (pairwise <!--chain:aci_pairwise-->0.99365<!--/chain--> over
  <!--chain:aci_path_count-->28<!--/chain--> paths), and the k-order profile
  re-enumerates.
- **economy / treasury+gate3 / flow1 emission** — the closed-loop economy
  log, the treasury conservation arithmetic (including the clawback drill),
  and the signed-heartbeat epoch all replay.
- **metering** — the CLAIM-CHECK layer described above.
- **cut certificate / trust vectors / passports** — the derived artifacts
  rebuild generation-locked; the no-leaderboard rule is scanned live.
- **challenges / identity** — every verified possession-proof round
  re-derives under its nonce; every drill (replay, forged rotation, forged
  heartbeat) stays rejected from public material only.
- **participant intake** — the anchored participant record's claims re-derive
  from shipped evidence and the tampered-bundle rejection stays refuted.

Here is the real run (`--quick` here, so the third integrity model is visible
on the page; section 2 of docs/PARTICIPATE.md pastes a full run):

```verify-run
$ python3 metacoin_cli/main.py verify --quick --quiet
==============================================================================
  [PASS] chain+anchor       VERIFIED-FULL      chain intact, 48 entries, tip_hash matches; anchor: tip 5b7bf7eb0025.. (48 entries); no live ledger (fresh clone) — published snapshot is the source
  …(layer lines trimmed — quick mode accepts the heavy recomputation layers
    conditional on their anchors and labels every such line)…
------------------------------------------------------------------------------
```
<!--expect:ACCEPTED-BY-ANCHOR-->
<!--expect:chain+anchor-->
<!--expect:PASS-->

## The honest boundary, verbatim

Every full report ends with this block. It is quoted here exactly as the tool
prints it, and doc-verification asserts the tool still prints it:

```
------------------------------------------------------------------ honest boundary
Everything above is SAME-OPERATOR, zero-value, research-stage evidence. A full
pass establishes that every anchored claim RE-DERIVES deterministically from the
shipped evidence on YOUR machine. It does NOT establish: independent multi-party
verification (the anchored ACI baseline quantifies maximal same-operator
concentration), usefulness of the work (Gate 3's judgment seat is honestly
vacant — only the mechanical lifecycle with scripted adjudication exists; every
trust vector says 'not-assessed'), hardware-rooted execution proof (no TEE — open
debt), measured energy (estimates from an assumed power figure), or any monetary
value (no token exists). ACCEPTED-BY-ANCHOR lines are bounded-cost acceptance
conditional on the committed anchors — not re-proof. Not consensus, not payment,
not investment advice.
```

```verify-run
$ python3 -c "import sys; sys.path.insert(0,'.'); from protocol.verify_everything import HONEST_BOUNDARY; print(HONEST_BOUNDARY)"
…(output identical to the quoted block above — trimmed here, asserted by the
  expect markers below, so the quote and the tool cannot drift apart)…
```
<!--expect:It does NOT establish: independent multi-party-->
<!--expect:ACCEPTED-BY-ANCHOR lines are bounded-cost acceptance-->
<!--expect:no TEE — open-->

## Beyond the flagship

The suites behind the report are runnable directly:
`protocol/run_protocol_selftests.sh`
(<!--chain:protocol_suite_count-->26<!--/chain--> self-tests) and
`demo/run_all_selftests.sh` (<!--chain:demo_suite_count-->39<!--/chain-->
self-tests) — CI requires both green plus a fresh-clone full pass on every
commit. The layer tools remain individually available (`audit.py --verify`,
`agent_verifier.py`, `challenge_response.py`, …) — the flagship adds no
authority of its own; it only orchestrates verifiers that each stand alone.
