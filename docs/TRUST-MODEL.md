# Trust model: what is proven, what is claimed, what is owed

> **THE DOC CONTRACT.** Everything in this document is [BUILT] fact,
> mechanically verified by protocol/doc_verify.py on every CI run: every
> stated number is tagged with the chain point it describes and re-checked
> against live state, every ledger index is checked to exist with the stated
> event type, and nothing here claims more than `metacoin verify` proves.
>
> Chain point: tip index <!--chain:tip_index-->57<!--/chain-->,
> <!--chain:entry_count-->58<!--/chain--> entries.

This is the consolidated honesty page: every limitation stated anywhere in the
corpus, in one place, with the measured numbers. Nothing here is an apology —
the protocol's position is that a verification system earns trust by proving
what its evidence *cannot* support, mechanically, before anyone asks.

## The same-operator status, measured

Every one of the <!--chain:entry_count-->58<!--/chain--> ledger entries was
produced by machines under one operator's control, and each record's
`operator_relationship` field says so. The protocol measured this
concentration itself and anchored the result at idx 18
<!--idx:18=aci_baseline_anchored-->: pairwise ACI
<!--chain:aci_pairwise-->0.99365<!--/chain--> across
<!--chain:aci_path_count-->28<!--/chain--> verification paths
(<!--chain:aci_pair_count-->378<!--/chain--> pairs) — the maximal
same-operator baseline. A k-order extension (idx 44
<!--idx:44=aci_korder_baseline_anchored-->) covers group structure that
pairwise scoring provably misses. Two standing rules keep the number honest:
missing metadata always scores as worst-case dependence (never as
independence), and ACI is descriptive evidence only — a future low value is
not proof of independence and never a minting trigger.

## `-claimed`: relationships are declared, not proven

Every relationship in the system — a participant's declared affiliation, an
actor's operator link — is recorded with a `-claimed` suffix
(`unaffiliated-claimed`, `same-operator-claimed`). The suffix is enforced
vocabulary, not style: intake rung 6 (`relationship-label-well-formed`)
refuses a bundle whose label pretends to more. The ledger records what was
*claimed* and by whom; it never converts a claim into an endorsement.

## What a signature proves: possession-continuity

Actor identity is a Merkle root over one-time keys. A verifying signature
proves that *whoever holds the keychain behind this root* produced this
record, and — across the rotation chain (idx 41
<!--idx:41=actor_key_rotated-->) — that possession has been continuous. It
does **not** prove who the holder is, that two roots are different people, or
that a key was not shared. The forged-rotation drill (idx 42
<!--idx:42=actor_key_rotation_rejected-->) shows the boundary being enforced
from public material alone.

## What a hash proves — and doesn't

A matching output hash proves **reproducibility**: the result was
independently re-derived to the same canonical value. It does **not** prove
execution — a hash can be copied. The corpus states this on every
verification record; the challenge-response layer narrows the gap with
nonce-bound possession proofs (a copied hash cannot answer a fresh nonce),
but execution proof proper would require verifier-held signing keys inside
attested hardware — which is exactly the first open debt below.

## The open debts

Machine-readable provenance debt is carried inside the work molecules
themselves; this is the prose index of what is owed, not a replacement for
those records:

- **TEE / hardware attestation** — no trusted-execution intake exists;
  machine fingerprints are coarse platform hashes, not execution proof.
- **Hardware power telemetry on this host** — energy figures are estimates
  (`cpu_time ×` an assumed <!--chain:assumed_power_w-->15.0<!--/chain--> W
  constant, anchored with honest `estimated` labels at idx 20
  <!--idx:20=metering_evidence_anchored-->). The coordinator host was
  physically probed for real power sensors
  (`protocol/power_telemetry.py --probe`): every readable sensor was
  empirically screened and none observes the CPU domain where tasks run —
  so the debt *remains open*, stated rather than papered over. A
  measured-power upgrade lands only as a new anchored metering generation.
- **Per-step execution traces** — execution records are coarse event
  timestamps; no durations, exit codes, or step traces exist in any record
  (the standing `partial` debt on every molecule).
- **Usefulness judgment** — Gate 3's seat is honestly vacant: every trust
  vector's usefulness component reads `not-assessed`, and no mechanism in the
  repo pretends otherwise.

## The missing milestone

The next meaningful step is operational, not code: **an unaffiliated third
party running the public verifier and submitting a result** — the first
cross-party independence record. The chain carries a full rehearsal of that
pipeline (idx 45-47, ending in a labeled same-operator rehearsal record at
idx 46 <!--idx:46=participant_result_anchored--> and a tampered-bundle
refusal at idx 47 <!--idx:47=participant_intake_rejected-->), so the path is
proven; the independence is not. Until a genuine submission arrives, every
document in this repository — including this one — says so.
