# MHS-shaped physical work — attested measurements, re-derived analysis, first-class refusals

Research-stage. This directory is the protocol's **physical-work layer** in
the shape of the publicly described Model Hardware Standard (MHS): a device
exposes `read` / `write` primitives, a reference manifest (what it can
measure, what can be adjusted, what safety limits are enforced, who it is),
and a state dictionary. **It is MHS-shaped, pre-standard, and simulated.**
MHS is a limited research preview that is not yet open source; nothing here
is a claim about its specification, and no physical instrument was operated.
This README is in the doc-verify scan set: its chain tokens and verify-run
block are mechanically verified by protocol/doc_verify.py on every CI run.

## The epistemology, stated first

**Attested measurements are not re-derived; only the analysis is.** A
measurement is a fact about the world at an instant. A verifier cannot
recompute it; it can check that the device that reported it signed it
(a one-time hash-based signature under the device's declared root), that
the snapshot bytes still hash to what was signed, and that nobody edited
them since. The **analysis** over those snapshots is deterministic
standard-library arithmetic with a canonical output and a SHA-256 hash —
it is re-derived bit-exact on every verification, exactly like a task. The
**verdict** lives inside that canonical output, honest-negative form
included: `run_acceptable: false` with the reason is a first-class result.
**Safety-limit refusals** are first-class too: a write the manifest's
limits block is recorded, signed by the device, and verified against the
limits table. This is what "verifiable physical work" can honestly mean
today, and what it cannot: a green run proves the record's integrity and
the analysis arithmetic, never that the measurements are true or that an
instrument moved.

## The record model (`protocol/physical_work.py`)

```
PhysicalWorkRecord = {
  label:  "MHS-shaped, pre-standard, simulated device"      (verbatim, required)
  epistemology: "attested measurements are not re-derived; only the analysis is"
  device_manifest: {capabilities, safety_limits, identity{merkle_root}, simulated: true}
  device_manifest_hash                                       (sha256 of the canonical manifest)
  safety_limits_table: device-limits-table/0.1               (the parameter-table shape)
  safety_limits_hash                                         (the parameter-table canonical hash)
  runs: [{ state_snapshots: [{t_s, values, snapshot_hash, device_signature}]   ATTESTED
           analysis: {method, r2, plateau_detected, energy_j, verdict{run_acceptable, reason}}
           output_hash }]                                                     RE-DERIVED
  refusals: [{t_s, requested_write, limit, refused: true, state_changed: false,
              reason, refusal_hash, device_signature}]                        FIRST-CLASS
  record_hash                                                (self-hash, era-2 canonical form)
}
```

Verification (`verify_record`): every snapshot signature under the
manifest's root with no one-time key reused; every run's analysis re-derived
and compared bit-exact (a flipped verdict fails); the manifest's limits equal
the table byte-for-byte (a loosened limit is DRIFT, refused by name); every
refusal names a limit the table carries and actually enforces it. The
self-test proves each of those failure modes loudly: a tampered snapshot, a
re-hashed snapshot without the device's signature, a loosened manifest limit,
a loosened table, a manufactured-success verdict.

MetaCoin map: manifest + identity root ↔ actor declaration / passport ·
limits table ↔ the anchored parameter table · refusals ↔ the attack drills ·
`run_acceptable: false` ↔ the honest negatives · the analysis ↔ the task
contract · the state dictionary ↔ the ledger as bus.

## The demo (mirrors the public CMU description)

A simulated plate reader with a power meter. Run 1: a dose-response over a
wide concentration range saturates — the top responses plateau and the fit
misses the pre-stated R² ≥ 0.9 rule, so the analysis **rejects the run**
(the honest negative). Run 2: the range is adjusted and the curve is
**accepted**. Between them, one blocked write (a shaker speed above the
manifest's limit) is recorded as a refusal with `state_changed: false`.
Every snapshot carries the device's signature; the simulated device's keys
derive from a published seed, so the signatures prove the verification
path, not secrecy.

```verify-run
$ python3 integrations/mhs/mhs_sim.py --demo
run-1: run_acceptable=False R^2=0.893029 plateau=True  (trimmed)
run-2: run_acceptable=True R^2=0.984747 plateau=False
refusal t=255s: shaker_speed_rpm=1800 exceeds shaker_speed_rpm_max=1500 (state_changed=False)
```
<!--expect:run-1: run_acceptable=False-->
<!--expect:run-2: run_acceptable=True-->
<!--expect:state_changed=False-->

```verify-run
$ python3 integrations/mhs/mhs_sim.py --selftest
--- self-test invariants ---  (trimmed)
```
<!--expect:over-limit write is REFUSED before any state change-->
<!--expect:ALL CASES BEHAVED CORRECTLY-->

The record class on the chain: `physical_work_recorded`, anchored through
`external_verifier --anchor-physical-work --confirm` after the coordinator
re-verifies every signature and re-derives every analysis; the
`physical work` layer of `metacoin verify` re-checks the shipped evidence
on every run. The chain currently holds
<!--chain:entry_count-->109<!--/chain--> entries (tip index
<!--chain:tip_index-->108<!--/chain-->).

## Three control paths (the public description's shape)

- **code** — `MhsShapedDevice` and `StateBus` in `mhs_sim.py`;
- **CLI** — `python3 integrations/mhs/mhs_sim.py --state | --manifest | --read NAME | --write NAME VALUE`;
- **tool calls** — `tool_schema()` returns the three primitives (`mhs_read`, `mhs_write`, `mhs_manifest`) as JSON-schema tool descriptions any MCP-style harness can register. No MCP server ships here; the shapes do.

## The vision bridge

The definition sentence names "humans, AI agents, and bounded-autonomous
robots" as the workers a fee-funded treasury would pay. This layer is where
"bounded-autonomous" becomes mechanical: the bound is the manifest's limits
table, anchored and drift-refusing; the autonomy is the deterministic
analysis that accepts or rejects a run on a pre-stated rule; and the robot
becomes a ledger actor — it declares an identity root, signs what it
measured, and its refusals are on the record beside its results. Nothing is
paid for anything here (no token exists); what exists is the record a
payment rule would need.

## What an action-layer standard provides, and what this layer provides

Two columns, no ranking. The left column is MHS as publicly described
(quoted where the words are Anthropic's); the right column is this
directory. Neither replaces the other: an action layer is where a device is
operated, and this layer is where what happened is recorded so that a
stranger can check it afterwards.

| An action-layer standard (MHS, public description) | This layer (MHS-shaped, simulated) |
|---|---|
| A device vocabulary: "commands like 'read' (for example, 'get temperature') or 'write' (for example, 'set temperature')—that any hardware device can understand and act on." | The same two verbs over a simulated state dictionary. A write is checked against the manifest's limits and refused before any state changes. No real driver. |
| A reference file: "what it can measure, what can be adjusted, and what safety limits will be enforced." | A manifest with those three parts plus an identity root and `simulated: true`, hashed onto the record. The limits are mirrored in an anchored device-limits table; a manifest that disagrees with the table is refused as drift, by name. |
| Safety enforced at the moment of action: in the public CMU account, six induced fault conditions were "correctly blocked" before any device moved. | The refusal becomes a record: signed by the device, `state_changed: false`, verified to name a limit the table actually enforces. It says nothing about how well any real driver enforces its limits. |
| Access for agents through "MCP, the command line interface, and code files (APIs)"; model-agnostic. | The same three shapes (a tool schema, a CLI, Python classes) and no MCP server. The shapes exist so a harness can be pointed at the record, not so a device can be run. |
| The run happens and a decision is made: in the public CMU account, run 1 was rejected below R² 0.9 and run 2 was accepted without human intervention. | The decision is a deterministic analysis with a rule stated before the data (R² ≥ 0.9 and no plateau), a canonical output, and a hash. Anyone re-derives it bit-exact from a fresh clone, and the rejected run is kept as a result rather than discarded. |
| Real measurements from a real instrument, held in the rig's own memory and logs. | Snapshots from a simulated device, signed under a published seed, attested and never re-derived. A green verification proves the record was not edited; it does not prove a measurement was true. |
| Findings and deployment guidance to be released with the standard when it is open-sourced. | An append-only chain entry holding the manifest hash, the limits hash, the attested snapshots, the analysis hashes, and the refusals, checkable by a stranger after the fact. |
| A limited research preview run by Anthropic with named partners; the specification is not yet public. | Pre-standard and research-stage, zero-value, no token. Re-shaped to the published specification when there is one, and labeled simulated until then. |

## Stated limitations

Simulated device, published signing seed, one record class, one analysis
(OLS on log-concentration with a plateau probe). No MCP server, no real
driver, no thermal or safety model of any instrument. When MHS is open
sourced, the manifest and primitives will be re-shaped to the published
specification and this label will change only then.

---

Research-only; zero-value; no token (MIP-0001 ¶3, MIP-0002 ¶8). No
affiliation with or endorsement by Anthropic, HHMI Janelia, Carnegie Mellon
University, Genentech, QuEra Computing, or the gently project; MHS is their
work and their name. Not financial, legal, or engineering advice. Licensed
under SML-1.0 — see [`../../LICENSE.md`](../../LICENSE.md).
