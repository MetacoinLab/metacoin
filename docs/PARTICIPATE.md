# Participating: the complete walkthrough

> **THE DOC CONTRACT.** Everything in this document is [BUILT] fact,
> mechanically verified by protocol/doc_verify.py on every CI run: every
> command block below was executed for real in a fresh clone and its output
> pasted (trimmed for volume, never altered), every stated number is tagged
> with the chain point it describes and re-checked against live state, and
> nothing here claims more than `metacoin verify` proves.
>
> Chain point of every output in this file: tip index
> <!--chain:tip_index-->73<!--/chain-->, hash
> <!--chain:tip_hash_prefix-->86c0a8f1e927<!--/chain-->…,
> <!--chain:entry_count-->74<!--/chain--> entries.

This is the stranger's path from nothing to a submitted, coordinator-validated
verification bundle. It is honest about what each step proves: everything you
run re-derives the coordinator's anchored claims on **your** machine; nothing
you run proves independence — the relationship you declare is recorded as a
claim, never endorsed. Research-stage, zero-value, no token.

## 1. Install

Two variants, both from source — the package is **not** published to PyPI.

Either install the CLI entry point with pip, straight from GitHub or from a
clone:

```
pip install git+https://github.com/MetacoinLab/metacoin   # straight from GitHub
# ...or from a clone / an unpacked release tarball:
#   pip install .
```

…after which the command is `metacoin`. Or run with **no install at all** from
a clone — every command below uses this zero-install form, which is exactly
what CI executes:

```verify-run
$ python3 metacoin_cli/main.py version --quiet
metacoin-protocol 0.1.0 — research-stage, zero-value, no token — verification proves deterministic re-derivability, not value
```
<!--expect:metacoin-protocol-->
<!--expect:zero-value-->

The complete verification corpus ships in the repository (the published ledger
snapshot, the committed tip anchor, and the privacy-checked evidence bundle
under `protocol/evidence/`), so a fresh clone verifies end-to-end with no
local-only inputs.

## 2. Verify before you trust anything

Run the flagship first. It re-verifies every protocol layer from the shipped
corpus on your machine:

```verify-run
$ python3 metacoin_cli/main.py verify --quiet
==============================================================================
  [PASS] chain+anchor       VERIFIED-FULL      chain intact, 48 entries, tip_hash matches; anchor: tip 5b7bf7eb0025.. (48 entries); no live ledger (fresh clone) — published snapshot is the source
  [PASS] tasks (13 re-run)  VERIFIED-FULL      all 13 canonical hashes re-derived and match the ledger
  …(13 more layer lines trimmed; every one [PASS] — see docs/VERIFICATION.md
    for the full list and what each line means)…
------------------------------------------------------------------------------
RESULT: ALL LAYERS PASS
------------------------------------------------------------------ honest boundary
Everything above is SAME-OPERATOR, zero-value, research-stage evidence. …
```
<!--expect:RESULT: ALL LAYERS PASS-->
<!--expect:honest boundary-->
<!--expect:VERIFIED-FULL-->

A full pass establishes that every anchored claim re-derives deterministically
from the shipped evidence. The report ends with the honest boundary — what a
pass does **not** establish (docs/VERIFICATION.md quotes it verbatim).

## 3. Create your participant identity

`participate init` generates a private one-time-key keychain and a public
declaration. The keychain never leaves your machine.

```verify-run
$ python3 metacoin_cli/main.py participate init --handle doc-walkthrough --quiet
participant identity created for handle 'doc-walkthrough'
  PRIVATE keychain    : ./keychain_participant.json  (NEVER commit or transmit this file — it holds one-time signing secrets)
  public declaration  : ./participant_identity.json  (this is what gets registered)
  profile             : ./participant.json
  relationship        : unaffiliated-claimed  (SELF-DECLARED — recorded as a claim, not endorsed)
  created against tip : index 47, 5b7bf7eb0025bc01..
next: participate run
```
<!--expect:participant identity created-->
<!--expect:NEVER commit-->
<!--expect:-claimed-->

Note the two honesty mechanics already visible: your relationship to the
coordinator's operator is **self-declared** and recorded with a `-claimed`
suffix (claimed, not proven), and the private keychain is named as material
that must never be transmitted — intake will mechanically refuse any bundle
that contains it.

## 4. Run the verification under your own key

`participate run` re-derives the anchored stack and signs the outcome with one
of your one-time keys:

```verify-run
$ python3 metacoin_cli/main.py participate run --quiet
verification run complete for 'doc-walkthrough':
  verdict            : verified
  chain_verified     : True
  tip_matches_anchor : True
  tasks reproduced   : 18/18
  signed with        : one-time key index 0 under root e09ee5b955d7d5c9..
  written            : ./participant_result.json
next: participate bundle
```
<!--expect:verification run complete-->
<!--expect:verdict-->
<!--expect:written-->

## 5. Bundle it

```verify-run
$ python3 metacoin_cli/main.py participate bundle --quiet
  schema               : participant-bundle/0.1
  handle / actor_id    : doc-walkthrough
  relationship_claimed : unaffiliated-claimed
  chain point          : index 47, 5b7bf7eb0025bc01..
  bundle sha256        : …(varies per keychain — your run prints yours)…

NEXT STEP — submit this bundle (the README's GitHub-issue path):
  1. Open a GitHub Issue titled: Participant bundle: doc-walkthrough
  2. Attach or paste ./bundle.json   (sha256: …)
  3. The coordinator validates it with external_verifier.py --intake (six named checks, each with evidence), and only a human --confirm anchors the outcome — pass or rejection — to the public ledger.
```
<!--expect:participant-bundle/0.1-->
<!--expect:GitHub Issue-->
<!--expect:PRIVATE-->

## 6. Submit: a GitHub issue

Open a GitHub Issue on the repository titled `Participant bundle:
<your-handle>` and attach or paste your `bundle.json`. That is the whole
transport — no accounts, no keys uploaded, nothing beyond the public bundle.

## 7. What the coordinator's validation will do with it

Intake is a **six-rung validation ladder**, run by
`protocol/external_verifier.py --intake`. Each rung is a named check with
recorded evidence; a failed rung fails the ladder and the remaining rungs are
marked skipped, never silently passed. The rungs, in order:

1. `schema-and-no-private-material` — the bundle is well-formed and contains
   no signing secrets (private material is refused at build *and* at intake).
2. `identity-registrable-or-matching` — your declared key root is either new
   (registrable) or matches your already-registered identity.
3. `signature-verifies-under-declared-root` — the result signature verifies
   under a one-time key of the declared Merkle root.
4. `result-substance-rederived` — the coordinator re-runs your claims locally
   and requires your recorded chain tips to be verified **prefix points** of
   the live chain (the chain may legitimately have grown since you ran).
5. `one-time-key-discipline` — each one-time key is used at most once,
   replay refused.
6. `relationship-label-well-formed` — your self-declared relationship carries
   the `-claimed` suffix and is one of the declared vocabulary values.

A rejection is a **mechanical refusal at the named rung** — the ledger records
which rung and why; it is not an accusation of fraud. Both outcomes — pass or
rejection — are anchored. The chain already carries one of each from the
rehearsal: a participant-verified record at idx 46
<!--idx:46=participant_result_anchored--> and a tampered-bundle rejection at
idx 47 <!--idx:47=participant_intake_rejected--> (a deliberate drill, labeled
as such on the record).

## 8. What anchoring means — and what it does not

**Anchoring means:** your bundle's outcome becomes an append-only ledger entry
whose hash is chained to everything before it. Anyone who clones the
repository can re-derive your record's claims mechanically, forever, from
public material — that is what idx 46 already demonstrates for the rehearsal
participant.

**Anchoring does not mean:** that you are independent (your relationship
stays `-claimed` — self-declared, never endorsed), that a signature proves who
you are (it proves continuity of possession of your keychain), that a matching
hash proves you executed the tasks (a hash can be copied; matching honestly
supports "this party submitted a result that re-derives", not execution
proof), or that the work has value (zero-value research stage, no token).

That is the standing honesty of this project: every one of the
<!--chain:entry_count-->74<!--/chain--> entries so far is same-operator, and
each record says so. The anchored concentration baseline quantifies it —
pairwise ACI <!--chain:aci_pairwise-->0.99365<!--/chain--> across
<!--chain:aci_path_count-->28<!--/chain--> verification paths (measured at
idx 18 <!--idx:18=aci_baseline_anchored-->). The first bundle from an
unaffiliated party will be the project's first cross-party independence
record. It has not happened yet; these docs will say so until it does.
