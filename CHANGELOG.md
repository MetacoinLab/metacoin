# Changelog — keyed to the ledger, not the calendar

> **THE DOC CONTRACT.** Everything in this document is [BUILT] fact,
> mechanically verified by protocol/doc_verify.py on every CI run. Entries
> are keyed by anchored ledger index range — the chain is the timeline,
> and a record's position in it is its date; every typed idx reference
> below is resolved against the chain. Work that writes nothing to the
> ledger (integrations, generators, gate runs) is listed under the tip it
> accompanied. Newest first. Research-stage; zero-value; no token.

## idx 101–107 — the software/data-engineering transfer family

Six tasks of the kind coding and data agents actually face — schema
migration consistency, API-contract satisfiability, dependency
resolution, configuration audit, pipeline reconciliation, test-coverage
gap — built under the same law and scored by the same bit-exact rule as
the physics library, with two honest negatives (task-0035
`migration_valid: false`, task-0040 `coverage_target_met: false`).
Self-recompute records at idx 101–106
<!--idx:101=self_recompute_result--><!--idx:106=self_recompute_result-->,
the batch attestation at idx 107 <!--idx:107=agent_verifier_attestation-->.
The claim they support, and the ones they do not, are stated in
[docs/TRANSFER.md](docs/TRANSFER.md): the abstention probe transfers to
deterministic software/data tasks — the baseline mock's manufactured
success on task-0040 is caught by the identical detector that catches it
on task-0012 — and nothing here is open-ended. The Inspect adapter gained
a `family` parameter so the family runs alone; the honest-negative roster
is nine. Molecule catalogs untouched per cadence; README era-pinned.

## idx 100 — the anchored parameter table (cFE Table Services adoption)

Every behavior-changing protocol constant — thresholds, epoch sizes, fee
parameters, rounding precision, the assumed power figure, sampler
version, gate timeouts — now lives in ONE canonical table
(`protocol/parameter_table.py`, 32 parameters), whose era-2 hash is
anchored at idx 100 <!--idx:100=parameter_table_recorded-->. Owner
modules read their constants from the table; the new `parameter table`
verification layer asserts every effective constant equals the anchored
table and refuses BY NAME on drift, so changing any constant is a new
anchored table version — a governance event (a new MIP where the value
is MIP-pinned, a successor config record where anchored-config, this
record class otherwise). Table v1 is value-preserving: no behavior
changed and no anchored hash moved, asserted by the full battery. The
table era is chain-decided at the anchor index (the sampler-era rule).

## At tip idx 99 — repo work with no ledger writes

- **README generators**: `protocol/mission_graph_svg.py` (the mission DAG,
  drawn from the anchored verdict's evidence file — colors from anchored
  node verdicts, never recomputed) and `protocol/status_board_svg.py` (the
  status board, every tile asserted equal to its doc_verify token).
  Both stdlib-only, byte-deterministic, self-tested.
- **Integrations**: the [Inspect adapter](integrations/inspect/),
  the [HAL benchmark package](integrations/hal/),
  [model baselines](integrations/baselines/), and the
  [Open MCT trust-ledger console](integrations/openmct/) — a viewer, not
  a verifier, and its README says so first. All four READMEs sit in the
  doc-verify scan set.
- **Release gate, full run**: READY on every mechanical criterion, with
  the human seat explicit — verbatim: *"DEFAULT IS NO RELEASE: a release
  proceeds only from READY plus recorded coordinator approval
  (MIP-0005)."*
- **Seeded-sampling meta-record discipline**: sampled ACI rows carry
  {seed, m, M, sampler version}; `verify_sampled_row` re-runs the
  anchored seed bit-exact, and the era boundary is chain-decided.

## idx 97–99 — EDL beachhead and mission-0001-v3

Tasks 0033 (arrival/capture interface) and 0034 (EDL deceleration budget:
RK2 planar entry over the pinned NASA GRC atmosphere fits — the
heavy-lander class honestly cannot reach its parachute gate, the sought
negative found rather than tuned away). mission-0001-v3 anchored at idx 99
<!--idx:99=mission_verdict_recorded-->: 11 nodes, 17 edges,
`mission_feasible: FALSE` with five quantified bottlenecks; the chain's
100th entry.

## idx 94–96 — SPICE beachhead and mission-0001-v2

Four NAIF kernels fetched and hashed; a pure-stdlib DAF/SPK Type-2
Chebyshev reader; tasks 0030 (UTC→TDB) and 0031 (the Earth–Mars transfer
window — the one constraining node that honestly passes). mission-0001-v2
at idx 96 <!--idx:96=mission_verdict_recorded--> closes v1's named gap and
supersedes it on-chain by extension; the superseded verdict still
re-derives bit-exact.

## idx 83–93 — the civilization-scale claim, decomposed

Eight pinned-constant tasks (idx 83–90: flux fraction → shade area → mass
→ lunar aluminum → launch energy → deployment cadence → dust variant →
billion-year horizon). Verdict at idx 91
<!--idx:91=mission_verdict_recorded-->: `mission_feasible: FALSE` — two
constraints fail quantified, one passes conditionally. The feasibility
envelope at idx 92 <!--idx:92=mission_envelope_recorded--> states what
would flip it under its verbatim "engineered scenario" label. The
claim-source correction at idx 93 <!--idx:93=anchored_record_correction-->
replaces a paraphrase with the verified verbatim post and URL,
append-only — the idx 91 verdict untouched.

## idx 81–82 — the first mission-level verdict

Task 0021 (the conversion-corrected ascent budget, an honest negative that
compounds two others) and mission-0001 at idx 82
<!--idx:82=mission_verdict_recorded-->: the first typed DAG over anchored
tasks whose mission verdict re-derives bit-exact — FALSE, 4/4 constraining
nodes failing.

## idx 77–80 — CEA-era tasks and the first pulse

Tasks 0019 and 0020 anchor NASA-CEA-polynomial thermochemistry (pinned
verbatim with checksums); the batch attestation at idx 79; the first
anchored pulse at idx 80 <!--idx:80=pulse_recorded--> — a signed,
re-derivable "the whole stack was green here" record.

## idx 74–76 — task law

MIP-0008, MIP-0009, MIP-0010: messaged assertions, bounded loops,
recursion by waiver, unit-suffixed field names, and the canonical
four-key interface — binding every task module registered after the
decision records, with grandfathering decided by era, never by name.

## idx 69–73 — the cross-machine era and the mirror

A second machine registered (idx 69), participated with a verified bundle
(idx 70), and attested an independent mirror (idx 72
<!--idx:72=mirror_attestation_anchored-->) — fingerprint-decided topology,
the first records from outside the coordinator's machine set. MIP-0006
and MIP-0007 anchored beside them.

## idx 67–68 — canonical-form era transition + first self-correction

Cross-machine re-derivation exposed IEEE-754 negative zeros in canonical
artifacts. The repair: a chain-wide era transition (idx 67
<!--idx:67=task_hash_era_recorded-->) and the first append-only correction
record (idx 68 <!--idx:68=anchored_record_correction-->). No record
rewritten; era-1 hashes still verify.

## idx 48–66 — second task era, generations, epoch series, governance

The 17-task era re-derived and attested (idx 48–52); molecule catalog
generations 4 and 5 with the first parented molecule and the first
non-trivial cut certificate; the economy's second generation and the
cross-generation treasury extension; the concentration epoch series
(MIP-0004); the MIP process itself exercised on-chain (idx 58–62).

## idx 28–47 — identity, two-flow, intake

Lamport-root actor identity with a legitimate rotation and a forged
rotation defeated; the treasury constitution as code with a wrongful
Gate-3 grant challenged and clawed back; Flow-1 heartbeat emission with a
forged heartbeat defeated; MetaWork passports; the six-rung participant
intake with a tampered bundle refused at the named rung.

## idx 17–27 — provenance, concentration, trust

Work-molecule catalogs (content-addressed, three-state field semantics,
machine-readable provenance debt); the anchored maximal-concentration ACI
baseline; metering evidence; cut certificates; trust vectors with a
mechanical no-combined-score rule; challenge-response rounds with the
replay drill defeated.

## idx 0–16 — genesis and the first task era

The hash-chained ledger; external verification (a second machine
re-deriving a task hash at idx 1); the 13-task era; self-recompute
evaluations with honest same-operator labels; the first batch
agent-verifier attestation at idx 16.

---

Research-only; zero-value; no token (MIP-0001 ¶3, MIP-0002 ¶8). Not
financial, legal, or engineering advice. Licensed under SML-1.0 — see
[`LICENSE.md`](LICENSE.md).
