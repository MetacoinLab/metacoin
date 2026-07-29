"""external_verifier.py — MetaCoin R3 COORDINATOR for the external-verifier-pilot.

================== CRITICAL HONESTY NOTICE (READ ME) ==================
This is the coordinator side of the "external-verifier-pilot". It is research-stage and
ZERO-VALUE. It is NOT decentralized consensus, NOT mainnet, NOT payment, NOT a token.

What it does: an external verifier (ideally a separate person/org on a separate machine —
another box, a CI runner, a VPS, a collaborator's laptop) re-runs the same reproducible
task and submits the resulting canonical output_hash (see protocol/verifier_cli.py). The
coordinator recomputes the SAME task's hash LOCALLY and compares:
  * match    -> status "externally-verified"
  * mismatch -> status "external-mismatch"
  * malformed-> status "rejected"
Evaluated outcomes (verified AND mismatch — both are real, audit-worthy events) are anchored
into the R1 tamper-evident hash-chained ledger (protocol/ledger.py). Rejected (malformed)
submissions are validated BEFORE the ledger is touched and are NOT anchored, so malformed
external input never enters the chain.

PRECISE, HONEST CLAIM (the limitation that defines this pilot):
  A matching output_hash proves the result is REPRODUCIBLE — independently re-derived to the
  same canonical value. It does NOT by itself cryptographically prove the external verifier
  EXECUTED the task: a hash is a short string and can be copied. The honest claim a match
  supports is "an external party submitted a reproducibly-matching result", NOT "we proved
  independent execution". Independence improves when the verifier is a distinct person/org.
  Stronger execution-proof would require verifier-held signing keys and/or hardware
  attestation — future work, behind this same interface.

Standard library only (json, hashlib, os, time, subprocess, platform, argparse) plus the
existing protocol components. The single-host simulation appears ONLY inside the self-test
below, clearly labeled "local test harness, not the product".
"""

import argparse
import hashlib
import json
import os
import sys
import time

# Make `from protocol...` resolve when run directly (repo root on sys.path).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from protocol.ledger import Ledger, DEFAULT_LEDGER_PATH
import protocol.verifier_cli as verifier_cli
import protocol.audit as audit  # reused to independently re-derive chain/anchor claims
import protocol.work_molecule as work_molecule  # reused to REBUILD molecule catalogs
import protocol.agent_concentration as agent_concentration  # reused to RECOMPUTE ACI reports
import demo.economy_demo as economy_demo  # reused to RE-RUN the full simulated economy
import demo.task_metering as task_metering  # reused to RE-METER tasks (plausibility check)
import protocol.cut_certificate as cut_certificate  # reused to FULLY VERIFY cut certificates
import protocol.trust_vector as trust_vector  # reused to REBUILD trust-vector catalogs
import protocol.challenge_response as challenge_response  # reused to VERIFY challenges
import protocol.actor_identity as actor_identity  # scheme constants for key registration
import protocol.gate3_process as gate3_process  # reused to RECOMPUTE prechecks/windows
import demo.metastar_treasury as metastar_treasury  # reused to RE-DERIVE anchored fees
import demo.flow1_uptime as flow1_uptime  # reused to RE-VERIFY heartbeats + epochs
import protocol.metawork_passport as metawork_passport  # reused to REBUILD passports

# The required fields and the fixed (const) fields of a valid submission. Mirrors
# protocol/verifier_submission.schema.json (which is the human-readable contract).
_REQUIRED_FIELDS = (
    "event", "stage", "topology", "task_id", "verifier_id", "machine_fingerprint",
    "timestamp", "output_hash", "repo_commit", "environment_summary", "zero_value", "no_token",
)
_CONST_FIELDS = {
    "event": "external_verifier_submission",
    "stage": "R3",
    "zero_value": True,
    "no_token": True,
}
_HEX = set("0123456789abcdef")

# topology is validated against an allowed SET (not a single const): a submission may be an
# external-verifier-pilot OR an honest same-machine self-recompute. The anchored record
# carries the submitted topology VERBATIM (never silently rewritten), and the event/status
# naming differs per topology so no same-machine record can read as cross-machine ("external")
# verification.
_ALLOWED_TOPOLOGIES = ("external-verifier-pilot", "same-machine-self-recompute")
_TOPOLOGY_PROFILE = {
    "external-verifier-pilot": {
        "event": "external_verification_result",
        "verified_status": "externally-verified",
        "mismatch_status": "external-mismatch",
    },
    "same-machine-self-recompute": {
        "event": "self_recompute_result",
        "verified_status": "locally-verified",
        "mismatch_status": "local-mismatch",
    },
}

# Honest self-description for the anchored records: the ledger entry is a REAL reproducibility
# attestation; the task it concerns is an illustrative space-engineering calculation.
_TASK_CLASS = "illustrative-demo"

# The honest reproducibility-not-execution limitation, embedded in every external-pilot record.
LIMITATION_NOTE = (
    "A matching output_hash proves REPRODUCIBILITY (the result was independently re-derived "
    "to the same canonical value); it does NOT cryptographically prove the external verifier "
    "executed the task (a hash can be copied). Independence improves when the verifier is a "
    "separate person/org; execution-proof would need verifier-held signing keys and/or "
    "hardware attestation (future work, same interface). external-verifier-pilot — not "
    "consensus, not mainnet, not payment, not a token; zero-value research-stage."
)

# The honest limitation for a SAME-MACHINE self-recompute (used instead of LIMITATION_NOTE).
SAME_MACHINE_LIMITATION_NOTE = (
    "SAME-MACHINE self-recompute (research-stage, zero-value). The coordinator generated and "
    "re-evaluated this submission on the SAME host; a matching output_hash proves the result "
    "is REPRODUCIBLE run-to-run on this machine. This is NOT cross-machine and NOT independent "
    "third-party verification — no separate party, machine, or platform is involved, so it "
    "adds no cross-party independence (it is a stronger sibling of the deterministic re-run "
    "check). A hash can be copied. Not consensus, not mainnet, not payment, not a token."
)

# --- agent-verifier-result schema (a DIFFERENT record type than a task submission) -------
# An agent_verifier_attestation asserts three claims (chain + anchor + task reproduction),
# not a single task submission. The coordinator re-derives all three on the Spark before
# anchoring — it never just trusts the result file.
_AGENT_RESULT_EVENT = "agent_verifier_attestation"
# Real schema emitted by jiyu's agent_verifier.py: top-level claims + a task_reproductions
# ARRAY (each item: task_id, recomputed_hash, recorded_hash, match). The Spark re-derives
# each task itself rather than trusting these values.
_AGENT_RESULT_REQUIRED = (
    "event", "verdict", "chain_verified", "tip_matches_anchor",
    "verifier_id", "zero_value", "no_token", "task_reproductions",
)
_AGENT_REP_REQUIRED = ("task_id", "recomputed_hash", "recorded_hash", "match")

# Canonical committed published-snapshot path (the artifact the agent verified).
_DEFAULT_PUBLISHED_SNAPSHOT_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "ledger_published.json"
)

AGENT_LIMITATION_NOTE = (
    "mechanical re-derivation by the same operator on a second machine — proves "
    "REPRODUCIBILITY, NOT independent third-party verification; verdicts/hashes can be "
    "copied. The agent is a deterministic script (no LLM judgment). Not consensus, not "
    "mainnet, not payment, not a token; zero-value research-stage."
)

# --- work-molecule catalog anchoring (a THIRD record type) -------------------------------
# The catalog lists the WMID of every task's Work Molecule (protocol/work_molecule.py).
# The coordinator REBUILDS all molecules itself and recomputes the catalog hash before
# anchoring — the same coordinator-reconfirms pattern as agent results: never trust the file.
# Generation-aware (CONSTITUTIONAL RULE: debt is reduced only by APPENDING evidence):
# both catalog generations are acceptable and each is rebuilt in ITS OWN molecule
# schema, so the anchored idx-17 0.2 generation and any later 0.3 generation coexist —
# neither replaces the other, and the anchored record names its molecule_schema.
_CATALOG_EVENT = "work_molecule_catalog_anchored"
_CATALOG_GENERATIONS = {
    work_molecule.CATALOG_SCHEMA_VERSION_01: work_molecule.SCHEMA_VERSION_02,
    work_molecule.CATALOG_SCHEMA_VERSION_02: work_molecule.SCHEMA_VERSION_03,
}

CATALOG_LIMITATION_NOTE = (
    "Work Molecules are READ-ONLY ASSEMBLIES of same-operator records, assembled by the "
    "same operator; a matching WMID proves DETERMINISTIC RECONSTRUCTIBILITY of the "
    "provenance object from the recorded evidence, NOT independence and NOT execution "
    "(hashes can be copied). Provenance-debt fields (energy/compute cost, TEE hardware "
    "attestation, execution detail) remain open and are listed explicitly inside each "
    "molecule. Not consensus, not mainnet, not payment, not a token; zero-value "
    "research-stage."
)

# --- metering-evidence anchoring (a SIXTH record type) -----------------------------------
# CONSTITUTIONAL RULE: provenance debt is reduced ONLY by appending new evidence
# objects — this record is that appended evidence. No existing ledger record,
# submission file, or anchored artifact is modified; the idx-17 catalog's 0.2 WMIDs
# stay valid forever (the record carries no task_id/task_ids keys, so the molecule
# citation scanner never sees it).
#
# The coordinator PLAUSIBILITY-reconfirms, NOT byte-reconfirms: timing is inherently
# non-deterministic run to run, so re-metering can never reproduce the submitted
# report's bytes. What CAN be mechanically re-derived is (a) every output_hash matches
# the canonical ledger-recorded hash, (b) the report is well-formed with honest labels,
# (c) the energy arithmetic is exact, and (d) the coordinator's own re-metered timings
# land inside a sanity band. The anchored report_hash therefore fixes WHAT WAS CLAIMED
# at measurement time — a different integrity model than the byte-reproducible task
# hashes, stated on the record itself.
_METERING_EVENT = "metering_evidence_anchored"
_METERING_CONFIRMED_STATUS = "metering-evidence-confirmed"
_METERING_SANITY_MAX_WALL_S = 60.0  # any task re-metering slower than this is implausible

# --- trust-vector-catalog anchoring (an EIGHTH record type) ------------------------------
# The Trust Vector catalog lists the tv_hash of every task's six-component evidence
# vector (protocol/trust_vector.py). The coordinator REBUILDS all vectors itself and
# recomputes the catalog hash before anchoring — the same coordinator-reconfirms
# pattern: never trust the file. The anchored record AFFIRMS the no-scalar rule: no
# combined trust score exists anywhere, by design.
_TV_EVENT = "trust_vector_catalog_anchored"
_TV_CONFIRMED_STATUS = "trust-vector-catalog-confirmed"
NO_SCALAR_AFFIRMATION = "no combined trust score exists by design"

TV_LIMITATION_NOTE = (
    "Trust vectors are mechanical assemblies of same-operator evidence; component E "
    "cites the anchored maximal-concentration baseline; component U is honestly "
    "empty pending Gate 3; a matching catalog hash proves deterministic "
    "re-derivability, not trustworthiness; not consensus, not payment, not a token; "
    "research-stage."
)


# --- MetaWork passport catalog (per-actor histories, never a leaderboard) ----------------
_PASSPORT_EVENT = "passport_catalog_anchored"
_PASSPORT_STATUS = "passport-catalog-confirmed"

NO_LEADERBOARD_AFFIRMATION = ("no rank, score, rating, leaderboard, or "
                              "percentile exists anywhere, by mechanical rule")
UWW_TRANSPARENCY_STATEMENT = (
    "Useful-Work-per-Watt is transparency only, never a minting trigger: the "
    "emission path [flow1_uptime grep audit] and the treasury path "
    "[metastar_treasury grep audit] neither import nor read passport "
    "artifacts — money modules are mechanically blind to this metric"
)
PASSPORT_LIMITATION_NOTE = (
    "Passports are mechanical assemblies of same-operator anchored history; "
    "work-per-watt is an illustrative transparency figure at demo scale "
    "(estimated microjoule energies), mechanically unreadable by money paths; "
    "a matching catalog hash proves deterministic re-derivability, not merit; "
    "not consensus, not payment, not a token; research-stage."
)


# --- Flow-1 uptime emission records ------------------------------------------------------
# The uptime epoch record anchors Flow 1's flagship channel: OBJECTIVE per-slot
# emission from Lamport-signed liveness proofs over SIMULATED slots. The record
# carries NO task keys (emission concerns no task — scanner-invisible, asserted
# in the self-test) and its key_indices list feeds the ledger-wide CROSS-TYPE
# one-time-key scan (actor_identity.anchored_key_uses).
_HEARTBEAT_EVENT = "heartbeat_rejected"
_HEARTBEAT_REJECTED_STATUS = "heartbeat-forged-rejected"
_EPOCH_EVENT = "uptime_epoch_anchored"
_EPOCH_STATUS = "uptime-epoch-confirmed"

FLOW1_LIMITATION_NOTE = (
    "Flow 1 signed-heartbeat uptime emission v0: OBJECTIVE per-slot rule (a "
    "valid signed heartbeat emits the fixed amount; a missed slot emits "
    "exactly 0 — an objective fact, not a penalty), bounded by construction "
    "(the epoch refuses to run if slots x per-slot could exceed the cap), over "
    "SIMULATED slot indices, not wall-clock — the tip binding proves chain-"
    "state ordering, never real-world time. Same-operator node whose keychain "
    "the coordinator holds: liveness evidence, not third-party infrastructure. "
    "Zero-value Test-META; not consensus, not payment, not a token; "
    "research-stage."
)
MISSED_SLOT_STATEMENT = (
    f"slot {flow1_uptime.MISSED_SLOT_DRILL} emitted 0 by objective rule — no "
    "heartbeat, no emission, no discretion"
)
TWO_FLOW_SEPARATION_STATEMENT = (
    "Flow 1 emission has no treasury path [flow1_uptime audit]; Flow 2 "
    "treasury has no mint path [metastar_treasury audit] — the constitutional "
    "separation is mechanical in both directions"
)


# --- treasury + Gate-3 records (the Two-Flow constitution on-ledger) ---------------------
# The treasury config record anchors the SECOND flow's constitution: fee-funded
# from the anchored economy, bounded caps/budgets, conservation asserted, no
# mint path. Gate-3 lifecycle records (provisional/challenge/clawback/
# finalization) carry a SINGULAR top-level task_id ON PURPOSE: a Gate-3 process
# event about a work item SHOULD join that task's molecule history — the
# challenge phase's event name contains 'challenge', so the molecule assembler
# files it under challenge_events; the other phases land in verification_events
# as process events (routing asserted in the self-test). Frozen anchored
# generations stay valid via generation-locked rebuilds (cadence policy).
_TREASURY_EVENT = "treasury_config_anchored"
_TREASURY_STATUS = "treasury-config-confirmed"

TREASURY_LIMITATION_NOTE = (
    "MetaStar Treasury v0 under same-operator custody over a SIMULATED economy: "
    "zero-value Test-META accounting, not payment. Its only inflow is fees "
    "derived from the anchored economy record; its only outflows are bounded "
    "bounty payments; conservation (balance + outstanding == fees) is asserted "
    "at every operation and no mint path exists — an adjudication failure can "
    "at worst drain one capped bounty, never the monetary base. Not consensus, "
    "not payment, not a token; research-stage."
)

_GATE3_STATUS = {
    gate3_process.EVENT_PROVISIONAL: "gate3-provisional-confirmed",
    gate3_process.EVENT_CHALLENGE: "gate3-challenge-filed",
    gate3_process.EVENT_CLAWBACK: "gate3-clawback-confirmed",
    gate3_process.EVENT_FINALIZATION: "gate3-finalization-confirmed",
}

GATE3_LIMITATION_NOTE = (
    "Gate-3 bounded optimistic process v0: the machine pre-check is a "
    "MECHANICAL checklist with ledger citations (no LLM anywhere); the "
    "challenge window is entry-count-based (simulated, deterministic); the "
    "council is a same-operator SINGLE SEAT executing a fixed scripted rule — "
    "no discretion exists yet. The PROCESS is real; substantive usefulness "
    "judgment is honestly absent: process-passed is NOT useful. Zero-value, "
    "not consensus, not payment, not a token; research-stage."
)
PROCESS_PASSED_STATEMENT = (
    "process-passed under mechanical pre-check and an unchallenged window; "
    "substantive usefulness not assessed — the judgment seat is honestly vacant"
)


def _anchored_treasury_flows(entries):
    """(config_entry, fees, granted, clawed, by_bounty) replayed from ANCHORED
    records only — the coordinator never trusts local treasury state."""
    config_entry = None
    granted = clawed = 0.0
    by_bounty = {}
    for e in entries:
        p = e.get("payload") if isinstance(e, dict) else None
        if not isinstance(p, dict):
            continue
        if (p.get("event") == _TREASURY_EVENT
                and p.get("status") == _TREASURY_STATUS):
            config_entry = e
        elif (p.get("event") == gate3_process.EVENT_PROVISIONAL
                and p.get("status") == _GATE3_STATUS[gate3_process.EVENT_PROVISIONAL]):
            granted = round(granted + p.get("amount", 0), 6)
            by_bounty.setdefault(p.get("bounty_id"), {})["provisional"] = e
        elif (p.get("event") == gate3_process.EVENT_CHALLENGE
                and p.get("status") == _GATE3_STATUS[gate3_process.EVENT_CHALLENGE]):
            by_bounty.setdefault(p.get("bounty_id"), {})["challenge"] = e
        elif (p.get("event") == gate3_process.EVENT_CLAWBACK
                and p.get("status") == _GATE3_STATUS[gate3_process.EVENT_CLAWBACK]):
            clawed = round(clawed + p.get("amount_returned", 0), 6)
            by_bounty.setdefault(p.get("bounty_id"), {})["clawback"] = e
        elif (p.get("event") == gate3_process.EVENT_FINALIZATION
                and p.get("status") == _GATE3_STATUS[gate3_process.EVENT_FINALIZATION]):
            by_bounty.setdefault(p.get("bounty_id"), {})["finalization"] = e
    fees = (config_entry["payload"]["total_fees_collected"]
            if config_entry else 0.0)
    return (config_entry, fees, granted, clawed, by_bounty)


# --- actor-key registration (a TENTH record type) ----------------------------------------
# An actor registers the PUBLIC Merkle root of their one-time-signature keychain
# (protocol/actor_identity.py). v0 policy: ONE active root per actor — duplicate
# actor or duplicate root registrations are rejected; key ROTATION is future
# work behind this same record type, and we say so rather than pretend.
# The validator rejects any declaration containing private material anywhere.
_REGISTRATION_EVENT = "actor_key_registered"
_REGISTRATION_STATUS = "actor-key-registered"

REGISTRATION_LIMITATION_NOTE = (
    "Actor key registration under same-operator key custody: the coordinator "
    "generated and holds this keychain, so signatures under this root prove "
    "KEY-POSSESSION CONTINUITY, not third-party identity — the layer becomes "
    "identity-meaningful when an external actor generates and registers their "
    "OWN root. One-time discipline is a hard protocol rule: each key index "
    "signs exactly once, and anchored reuse is mechanically rejected. One "
    "active root per actor in v0; rotation is future work. Not consensus, not "
    "payment, not a token; research-stage."
)


def _contains_private_material(obj) -> bool:
    """True if any dict key anywhere contains 'private' — registration must be
    public-only, mechanically enforced."""
    if isinstance(obj, dict):
        return any("private" in str(k).lower() or _contains_private_material(v)
                   for k, v in obj.items())
    if isinstance(obj, list):
        return any(_contains_private_material(v) for v in obj)
    return False


# --- challenge-response anchoring (a NINTH record type) ----------------------------------
# A challenge-response round (protocol/challenge_response.py) upgrades "reproduced
# the hash" to "demonstrated possession of the full result under a fresh nonce" —
# the first defense against the ledger's oldest caveat, "a hash can be copied".
# The record carries a SINGULAR task_id key ON PURPOSE: it IS a genuine
# verification event for that task and SHOULD join the task's molecule history
# (the scanner classifies its 'challenge' event name into challenge_events).
# Anchored FROZEN generations stay valid via generation-locked (as_of) rebuilds.
_CHALLENGE_EVENT = "challenge_response_result"
_CHALLENGE_VERIFIED_STATUS = "challenge-verified"
_CHALLENGE_FAILED_STATUS = "challenge-failed"

CHALLENGE_LIMITATION_NOTE = (
    "Nonce-bound possession proof — demonstrates the responder held the complete "
    "task result under a fresh challenge, defeating hash-copying from public "
    "records; it does NOT prove where/when execution occurred nor exclude "
    "result-sharing between cooperating parties (and the public task source means "
    "anyone can execute). This first exercise is same-operator (coordinator "
    "challenging its own agent identity) and says so. Not consensus, not payment, "
    "not a token; research-stage."
)
CHALLENGE_DRILL_NOTE = (
    " PLANNED COPY-ATTACK DRILL: this failure is a deliberate demonstration that a "
    "response forged from the PUBLIC output_hash alone is mechanically rejected — "
    "the protocol's oldest caveat ('a hash can be copied') now has an anchored "
    "counter-demonstration, not detected fraud."
)


# --- cut-certificate anchoring (a SEVENTH record type) -----------------------------------
# A cut certificate summarizes a verified provenance subgraph so later verifiers can
# accept it at bounded cost (protocol/cut_certificate.py). THE COST ASYMMETRY IS THE
# DESIGN: the coordinator performs the EXPENSIVE full verification (rebuild every
# interior molecule, recompute every WMID and the aggregate) exactly once, HERE, at
# anchoring — that anchor-time full proof is what makes every later cheap
# accept_by_anchor (one anchored-hash lookup + one retrievability probe) sound.
# Compression, never erasure: the summarized evidence stays retained and re-provable.
_CUT_EVENT = cut_certificate.CUT_EVENT
_CUT_CONFIRMED_STATUS = cut_certificate.CUT_CONFIRMED_STATUS

CUT_LIMITATION_NOTE = (
    "First-generation cut certificate over a FLAT provenance graph (no parent edges "
    "exist yet among the thirteen molecules), so this is a degenerate cut exercising "
    "the mechanism — non-trivial cuts await molecules with declared parents. Full "
    "verification was performed by the coordinator at anchoring; subsequent cheap "
    "acceptance is conditional on this anchor plus continued retrievability of the "
    "molecules. Proves deterministic re-derivability of the summarized set, not "
    "independence; not consensus, not payment, not a token; research-stage."
)

METERING_LIMITATION_NOTE = (
    "First-generation compute/energy evidence by the same operator on one host: "
    "wall/CPU times MEASURED (time.perf_counter / resource.getrusage), energy "
    "ESTIMATED from an assumed constant power figure (no hardware power telemetry "
    "exists on this host — that debt remains open). Timing is non-reproducible by "
    "nature, so this record fixes the CLAIM made at measurement time, not a "
    "reproducible computation; verification of plausibility = re-metering reproduces "
    "the same output_hashes and same order-of-magnitude timings, not identical bytes. "
    "Reduces but does not eliminate the energy_and_compute_evidence provenance debt. "
    "Not consensus, not payment, not a token; zero-value research-stage."
)


def validate_submission(submission: dict):
    """Validate a submission against the R3 schema/required fields. Returns (ok, reason)."""
    if not isinstance(submission, dict):
        return (False, "submission is not a JSON object")

    for field in _REQUIRED_FIELDS:
        if field not in submission:
            return (False, f"missing required field '{field}'")

    for key, expected in _CONST_FIELDS.items():
        if submission.get(key) != expected:
            return (False, f"field '{key}' must be {expected!r} (got {submission.get(key)!r})")

    # topology is an allowed-set field (not a single const) — accept both the external pilot
    # and the honest same-machine self-recompute; reject anything else.
    if submission.get("topology") not in _ALLOWED_TOPOLOGIES:
        return (False, f"field 'topology' must be one of {_ALLOWED_TOPOLOGIES} "
                       f"(got {submission.get('topology')!r})")

    if not isinstance(submission["verifier_id"], str) or not submission["verifier_id"]:
        return (False, "verifier_id must be a non-empty string")
    if not isinstance(submission["machine_fingerprint"], str):
        return (False, "machine_fingerprint must be a string")
    if not isinstance(submission["timestamp"], (int, float)) or isinstance(submission["timestamp"], bool):
        return (False, "timestamp must be a number")
    if not isinstance(submission["repo_commit"], str):
        return (False, "repo_commit must be a string")
    if not isinstance(submission["environment_summary"], dict):
        return (False, "environment_summary must be a JSON object")

    oh = submission["output_hash"]
    if not isinstance(oh, str) or len(oh) != 64 or any(c not in _HEX for c in oh):
        return (False, "output_hash must be a 64-char lowercase hex sha256")

    if not isinstance(submission["task_id"], str):
        return (False, "task_id must be a string")
    try:
        verifier_cli.normalize_task_id(submission["task_id"])
    except KeyError as exc:
        return (False, f"unknown task_id: {exc}")

    return (True, "ok: submission conforms to the external-verifier-pilot schema")


def evaluate_submission(submission: dict, ledger: Ledger) -> dict:
    """Evaluate an external submission against the LOCAL canonical hash and anchor the outcome.

    Returns {"evaluation": <result dict>, "ledger_entry": <entry or None>}.
      * malformed -> status 'rejected', NOT anchored (validation precedes any ledger write)
      * hash match -> status 'externally-verified', anchored
      * hash mismatch -> status 'external-mismatch', anchored
    """
    ok, reason = validate_submission(submission)
    if not ok:
        evaluation = {
            "event": "submission_rejected",
            "stage": "R3",
            "topology": submission.get("topology") if isinstance(submission, dict) else None,
            "status": "rejected",
            "reason": reason,
            "match": False,
            "anchored": False,
            "zero_value": True,
            "no_token": True,
            "limitation_note": LIMITATION_NOTE,
            "evaluated_at": time.time(),
        }
        return {"evaluation": evaluation, "ledger_entry": None}

    task_id = submission["task_id"]
    module = verifier_cli.load_task(task_id)
    local_result = module.compute()
    local_hash = module.output_hash(local_result)

    submitted_hash = submission["output_hash"]
    match = (submitted_hash == local_hash)

    # Carry the submitted topology VERBATIM and name the event/status from its profile, so a
    # same-machine self-recompute can never read as cross-machine ("external") verification.
    topo = submission["topology"]
    profile = _TOPOLOGY_PROFILE[topo]
    status = profile["verified_status"] if match else profile["mismatch_status"]
    note = LIMITATION_NOTE if topo == "external-verifier-pilot" else SAME_MACHINE_LIMITATION_NOTE

    evaluation = {
        "event": profile["event"],
        "stage": "R3",
        "topology": topo,
        "status": status,
        "match": match,
        "task_id": task_id,
        "task_class": _TASK_CLASS,
        "verifier_id": submission["verifier_id"],
        "verifier_machine_fingerprint": submission["machine_fingerprint"],
        "submitted_output_hash": submitted_hash,
        "local_output_hash": local_hash,
        "verifier_repo_commit": submission.get("repo_commit", "unknown"),
        "anchored": True,
        "zero_value": True,
        "no_token": True,
        "limitation_note": note,
        "evaluated_at": time.time(),
    }
    if topo == "same-machine-self-recompute":
        # explicit, in addition to the verifier_id encoding it, so the relationship is unmissable
        evaluation["operator_relationship"] = "same-operator"
    entry = ledger.append(evaluation)
    return {"evaluation": evaluation, "ledger_entry": entry}


# ----------------------------------------------------------------------------
# Agent-verifier-result anchoring: ingest an agent_verifier_attestation, RE-DERIVE its
# claims on the Spark (never trust the file), and anchor an honest record.
# ----------------------------------------------------------------------------
def validate_agent_result(result: dict):
    """Structurally validate an agent_verifier_attestation result (real nested schema).

    Returns (ok, reason). Requires the top-level claim fields PLUS a non-empty
    task_reproductions array where each item carries task_id/recomputed_hash/recorded_hash/match.
    """
    if not isinstance(result, dict):
        return (False, "agent result is not a JSON object")
    for field in _AGENT_RESULT_REQUIRED:
        if field not in result:
            return (False, f"missing required field '{field}'")
    if result.get("event") != _AGENT_RESULT_EVENT:
        return (False, f"field 'event' must be {_AGENT_RESULT_EVENT!r} (got {result.get('event')!r})")
    if result.get("zero_value") is not True:
        return (False, "zero_value must be true")
    if result.get("no_token") is not True:
        return (False, "no_token must be true")
    if not isinstance(result.get("verdict"), str) or not result["verdict"]:
        return (False, "verdict must be a non-empty string")
    if not isinstance(result.get("chain_verified"), bool):
        return (False, "chain_verified must be a boolean")
    if not isinstance(result.get("tip_matches_anchor"), bool):
        return (False, "tip_matches_anchor must be a boolean")
    if not isinstance(result.get("verifier_id"), str) or not result["verifier_id"]:
        return (False, "verifier_id must be a non-empty string")

    reps = result.get("task_reproductions")
    if not isinstance(reps, list) or not reps:
        return (False, "task_reproductions must be a non-empty array")
    for i, rep in enumerate(reps):
        if not isinstance(rep, dict):
            return (False, f"task_reproductions[{i}] is not a JSON object")
        for key in _AGENT_REP_REQUIRED:
            if key not in rep:
                return (False, f"task_reproductions[{i}] missing '{key}'")
        if not isinstance(rep["task_id"], str):
            return (False, f"task_reproductions[{i}].task_id must be a string")
        for hk in ("recomputed_hash", "recorded_hash"):
            h = rep[hk]
            if not isinstance(h, str) or len(h) != 64 or any(c not in _HEX for c in h):
                return (False, f"task_reproductions[{i}].{hk} must be a 64-char lowercase hex sha256")
        if not isinstance(rep["match"], bool):
            return (False, f"task_reproductions[{i}].match must be a boolean")
        try:
            verifier_cli.normalize_task_id(rep["task_id"])
        except KeyError as exc:
            return (False, f"task_reproductions[{i}] unknown task_id: {exc}")
    return (True, "ok: conforms to the agent_verifier_attestation schema")


def _find_ledger_task_hash(entries: list, task_id: str):
    """Return the output_hash recorded in the ledger for `task_id`, or None if not present."""
    for entry in entries:
        payload = entry.get("payload", {})
        if payload.get("task_id") == task_id:
            for key in ("local_output_hash", "output_hash", "submitted_output_hash"):
                if isinstance(payload.get(key), str):
                    return payload[key]
    return None


def _rederive_agent_claims(result: dict, ledger: Ledger, snapshot_path: str, anchor_path: str) -> dict:
    """RE-DERIVE the agent's claims on the Spark (do NOT trust the result file).

    chain_verified    : the published snapshot independently verifies (audit.verify) AND the
                        live ledger verifies AND their tips agree.
    tip_matches_anchor: the published snapshot tip == the committed anchor tip.
    agent_tips_agree  : the agent's provided snapshot_tip_hash/anchor_tip_hash match the
                        Spark's own re-derived values (when the agent provided them).
    per_task          : for EACH task_reproduction, the Spark re-runs that task here and
                        confirms its hash equals BOTH the agent's recomputed_hash AND the
                        hash recorded in the live ledger for that task.
    """
    # 1. chain_verified — verify the published snapshot, the live ledger, and tip agreement.
    snap_ok, snap_reason, snap_details = audit.verify_snapshot_file(snapshot_path)
    snapshot_tip = snap_details.get("tip_hash") if snap_ok else None
    led_ok, led_reason = ledger.verify_chain()
    entries = ledger.read_all()
    ledger_tip = entries[-1]["hash"] if entries else None
    chain_verified = bool(snap_ok and led_ok and snapshot_tip is not None and snapshot_tip == ledger_tip)

    # 2. tip_matches_anchor — published snapshot tip vs committed anchor tip.
    anchor_tip = None
    try:
        with open(anchor_path, "r", encoding="utf-8") as f:
            anchor = json.load(f)
        if isinstance(anchor, dict):
            anchor_tip = anchor.get("tip_hash")
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        anchor_tip = None
    tip_matches = bool(snapshot_tip is not None and anchor_tip is not None and snapshot_tip == anchor_tip)

    # 2b. cross-check the AGENT's provided tips against the Spark's own (when provided).
    agent_snapshot_tip = result.get("snapshot_tip_hash")
    agent_anchor_tip = result.get("anchor_tip_hash")
    agent_tips_agree = bool(
        (agent_snapshot_tip is None or agent_snapshot_tip == snapshot_tip)
        and (agent_anchor_tip is None or agent_anchor_tip == anchor_tip)
    )

    # 3. per-task reproduction — re-run EACH claimed task; compare to the agent's recomputed
    #    hash AND to the hash recorded in the ledger for that task.
    per_task = []
    all_tasks_reproduced = True
    for rep in result.get("task_reproductions", []):
        tid = rep.get("task_id")
        agent_recomputed = rep.get("recomputed_hash")
        local_hash = None
        try:
            module = verifier_cli.load_task(tid)
            local_hash = module.output_hash(module.compute())
        except (KeyError, ImportError, AttributeError, ValueError, TypeError):
            local_hash = None
        ledger_recorded = _find_ledger_task_hash(entries, tid)
        matches_agent = bool(local_hash is not None and local_hash == agent_recomputed)
        matches_ledger = bool(
            local_hash is not None and ledger_recorded is not None and local_hash == ledger_recorded
        )
        reproduced = bool(matches_agent and matches_ledger)
        all_tasks_reproduced = all_tasks_reproduced and reproduced
        per_task.append({
            "task_id": tid,
            "local_output_hash": local_hash,
            "agent_recomputed_hash": agent_recomputed,
            "ledger_recorded_hash": ledger_recorded,
            "matches_agent_recomputed": matches_agent,
            "matches_ledger_recorded": matches_ledger,
            "reproduced": reproduced,
        })
    task_reproduced = bool(per_task and all_tasks_reproduced)

    return {
        "chain_verified": chain_verified,
        "tip_matches_anchor": tip_matches,
        "agent_tips_agree": agent_tips_agree,
        "task_reproduced": task_reproduced,
        "per_task": per_task,
        # extra transparency:
        "snapshot_tip_hash": snapshot_tip,
        "ledger_tip_hash": ledger_tip,
        "anchor_tip_hash": anchor_tip,
        "agent_snapshot_tip_hash": agent_snapshot_tip,
        "agent_anchor_tip_hash": agent_anchor_tip,
        "snapshot_verify_reason": snap_reason,
        "ledger_verify_reason": led_reason,
    }


def anchor_agent_result(result: dict, ledger: Ledger, snapshot_path: str, anchor_path: str,
                        operator_relationship: str = "same-operator") -> dict:
    """Validate + RE-DERIVE + anchor an agent_verifier_attestation. Returns {evaluation, ledger_entry}.

    Malformed -> 'rejected', NOT anchored. All claims re-derived True (chain, tip-vs-anchor,
    agent-tips-agree, every task reproduced) AND agent verdict 'verified' ->
    'agent-result-confirmed', anchored. Otherwise -> 'agent-result-mismatch', anchored (a
    real audit event). The anchored payload carries BOTH the agent's claim AND the Spark's
    per-task re-derivation, with honest labels.
    """
    ok, reason = validate_agent_result(result)
    if not ok:
        evaluation = {
            "event": _AGENT_RESULT_EVENT,
            "stage": "R-protocol",
            "topology": "agent-verifier",
            "status": "rejected",
            "reason": reason,
            "anchored": False,
            "zero_value": True,
            "no_token": True,
            "limitation_note": AGENT_LIMITATION_NOTE,
            "evaluated_at": time.time(),
        }
        return {"evaluation": evaluation, "ledger_entry": None}

    rederived = _rederive_agent_claims(result, ledger, snapshot_path, anchor_path)
    spark_all_true = (
        rederived["chain_verified"]
        and rederived["tip_matches_anchor"]
        and rederived["agent_tips_agree"]
        and rederived["task_reproduced"]
    )
    agent_verdict = result.get("verdict")
    status = ("agent-result-confirmed"
              if (spark_all_true and agent_verdict == "verified")
              else "agent-result-mismatch")

    evaluation = {
        "event": _AGENT_RESULT_EVENT,
        "stage": "R-protocol",
        "topology": "agent-verifier",
        "status": status,
        "task_class": _TASK_CLASS,
        "agent_verdict": agent_verdict,
        "spark_reconfirmed": {
            "chain_verified": rederived["chain_verified"],
            "tip_matches_anchor": rederived["tip_matches_anchor"],
            "agent_tips_agree": rederived["agent_tips_agree"],
            "task_reproduced": rederived["task_reproduced"],
            "per_task": [
                {
                    "task_id": pt["task_id"],
                    "local_output_hash": pt["local_output_hash"],
                    "matches_agent_recomputed": pt["matches_agent_recomputed"],
                    "matches_ledger_recorded": pt["matches_ledger_recorded"],
                    "reproduced": pt["reproduced"],
                }
                for pt in rederived["per_task"]
            ],
        },
        "task_ids": [pt["task_id"] for pt in rederived["per_task"]],
        "snapshot_tip_hash": rederived["snapshot_tip_hash"],
        "anchor_tip_hash": rederived["anchor_tip_hash"],
        "agent_snapshot_tip_hash": rederived["agent_snapshot_tip_hash"],
        "agent_anchor_tip_hash": rederived["agent_anchor_tip_hash"],
        "verifier_id": result.get("verifier_id"),
        # preserve the agent's own verifier_kind; record the attestation method separately.
        "verifier_kind": result.get("verifier_kind", "autonomous-agent"),
        "verifier_attestation_method": "mechanical-no-LLM",
        "operator_relationship": operator_relationship,
        "limitation_note": AGENT_LIMITATION_NOTE,
        "zero_value": True,
        "no_token": True,
        "anchored_at": time.time(),
    }
    entry = ledger.append(evaluation)
    return {"evaluation": evaluation, "ledger_entry": entry}


# --- ACI-baseline anchoring (a FOURTH record type) ---------------------------------------
# The ACI report (protocol/agent_concentration.py) is the protocol's self-measurement of
# verification-path concentration. The coordinator RECOMPUTES the FULL report itself
# (rebuild all molecules -> recompute every pairwise score -> compare report_hash) before
# anchoring — the same coordinator-reconfirms pattern: never trust the file.
_ACI_EVENT = "aci_baseline_anchored"

ACI_LIMITATION_NOTE = (
    "This is the protocol's DELIBERATE SELF-MEASUREMENT of its own maximal-concentration "
    "baseline: every verification path measured is operated by the SAME operator. ACI is "
    "DESCRIPTIVE evidence, never a minting trigger or reward signal, and a low value is "
    "not proof of independence. Pairwise ACI_2 only — higher-order concentration is not "
    "measured. Unknown metadata is scored worst-case (never as independence) and flagged. "
    "Not consensus, not mainnet, not payment, not a token; zero-value research-stage."
)


def validate_aci_report(report: dict):
    """Structurally validate an ACI report file. Returns (ok, reason).

    Internal inconsistency (a report_hash that does not recompute from the file's own
    content) is MALFORMED input -> rejected before any ledger write. Whether the
    MEASUREMENT is right is decided by the full recompute in anchor_aci_report.
    """
    if not isinstance(report, dict):
        return (False, "ACI report is not a JSON object")
    if report.get("schema") != agent_concentration.SCHEMA_VERSION:
        return (False, f"field 'schema' must be {agent_concentration.SCHEMA_VERSION!r} "
                       f"(got {report.get('schema')!r})")
    if report.get("weights_version") != agent_concentration.WEIGHTS_VERSION:
        return (False, f"field 'weights_version' must be "
                       f"{agent_concentration.WEIGHTS_VERSION!r} "
                       f"(got {report.get('weights_version')!r})")
    for field in ("path_count", "pair_count", "missing_metadata_count"):
        v = report.get(field)
        if not isinstance(v, int) or isinstance(v, bool) or v < 0:
            return (False, f"field '{field}' must be a non-negative integer")
    for field in ("pairwise_aci", "eis"):
        v = report.get(field)
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            return (False, f"field '{field}' must be a number")
    if not isinstance(report.get("concentration_profile"), dict):
        return (False, "concentration_profile must be a JSON object")
    cov = report.get("metadata_coverage")
    if not isinstance(cov, dict) or not isinstance(cov.get("coverage_ratio"), (int, float)):
        return (False, "metadata_coverage.coverage_ratio must be a number")
    rh = report.get("report_hash")
    if not isinstance(rh, str) or len(rh) != 64 or any(c not in _HEX for c in rh):
        return (False, "report_hash must be a 64-char lowercase hex sha256")
    if agent_concentration.compute_report_hash(report) != rh:
        return (False, "report_hash does not recompute from the report's own content "
                       "(internally inconsistent file)")
    return (True, "ok: conforms to the aci-report schema")


def anchor_aci_report(report: dict, ledger: Ledger) -> dict:
    """Validate + RECOMPUTE + anchor an ACI baseline report. Returns {evaluation, ledger_entry}.

    Malformed -> 'rejected', NOT anchored. Otherwise the coordinator RECOMPUTES the full
    report from the ledger itself (rebuild all molecules -> recompute ACI -> compare
    report_hash): match -> 'aci-baseline-confirmed'; anything else ->
    'aci-baseline-mismatch' (a real audit event). Both outcomes anchored.
    """
    ok, reason = validate_aci_report(report)
    if not ok:
        evaluation = {
            "event": _ACI_EVENT,
            "stage": "R-concentration",
            "topology": "same-machine-aci-measurement",
            "status": "rejected",
            "reason": reason,
            "anchored": False,
            "zero_value": True,
            "no_token": True,
            "limitation_note": ACI_LIMITATION_NOTE,
            "evaluated_at": time.time(),
        }
        return {"evaluation": evaluation, "ledger_entry": None}

    # RECOMPUTE the full report from this ledger (coordinator-reconfirms; never trust
    # the file): rebuild every molecule, re-derive every path, re-score every pair.
    recompute_error = None
    recomputed = None
    try:
        recomputed = agent_concentration.compute_report(
            agent_concentration.build_paths(ledger_path=ledger.path)
        )
    except (KeyError, ValueError) as exc:
        recompute_error = f"{type(exc).__name__}: {exc}"
    hash_matches = bool(recomputed is not None
                        and recomputed["report_hash"] == report["report_hash"])
    status = "aci-baseline-confirmed" if hash_matches else "aci-baseline-mismatch"

    # AGGREGATE NUMBERS ONLY — no per-path list. And deliberately NO task_id / task_ids
    # keys anywhere in this payload: work_molecule's citation scanner treats those keys
    # as "this record verifies that task" and would pull this record into every
    # molecule's verification_events, changing every WMID (and hence the ACI report
    # itself) each time a baseline is anchored. This record MEASURES the paths; it does
    # not verify tasks, so it must stay invisible to the scanner.
    evaluation = {
        "event": _ACI_EVENT,
        "stage": "R-concentration",
        "topology": "same-machine-aci-measurement",
        "status": status,
        "task_class": _TASK_CLASS,
        "report_schema": report["schema"],
        "weights_version": report["weights_version"],
        "report_hash": report["report_hash"],
        "path_count": report["path_count"],
        "pair_count": report["pair_count"],
        "pairwise_aci": report["pairwise_aci"],
        "eis": report["eis"],
        "concentration_profile": dict(report["concentration_profile"]),
        "missing_metadata_flag_count": report["missing_metadata_count"],
        "metadata_coverage_ratio": report["metadata_coverage"]["coverage_ratio"],
        "coordinator_reconfirmed": {
            "recomputed_report_hash": recomputed["report_hash"] if recomputed else None,
            "report_hash_matches": hash_matches,
            "recomputed_pairwise_aci": recomputed["pairwise_aci"] if recomputed else None,
            "recomputed_path_count": recomputed["path_count"] if recomputed else 0,
            "recompute_error": recompute_error,
        },
        "operator_relationship": "same-operator",
        "limitation_note": ACI_LIMITATION_NOTE,
        "zero_value": True,
        "no_token": True,
        "anchored_at": time.time(),
    }
    entry = ledger.append(evaluation)
    return {"evaluation": evaluation, "ledger_entry": entry}


# --- economy-demo summary anchoring (a FIFTH record type) --------------------------------
# The economy log (demo/economy_demo.py) records the deterministic SIMULATED 30-day
# earn->verify->spend loop. The coordinator RE-RUNS THE ENTIRE SIMULATION itself
# (fresh state, in-process) and compares log hashes before anchoring — the same
# coordinator-reconfirms pattern: never trust the file.
_ECONOMY_EVENT = "economy_demo_summary_anchored"

ECONOMY_LIMITATION_NOTE = (
    "Deterministic SIMULATED-day economy demo by the same operator — proves the "
    "earn->verify->spend loop MECHANICS with zero-value Test-META. The 30 days are "
    "simulated day indices, NOT real time; the loop is scripted determinism, NOT market "
    "behavior. The single rejection is a PLANNED tamper drill (labeled drill=true), not "
    "detected fraud. A matching log hash proves deterministic REPRODUCIBILITY of the "
    "simulation, not independence, not payment, not consensus, not a token; zero-value "
    "research-stage."
)


def validate_economy_log(log: dict):
    """Structurally validate an economy log file. Returns (ok, reason).

    Internal inconsistency (an economy_log_hash that does not recompute from the file's
    own content) is MALFORMED input -> rejected before any ledger write. Whether the
    SIMULATION is right is decided by the full re-run in anchor_economy_summary.
    """
    if not isinstance(log, dict):
        return (False, "economy log is not a JSON object")
    if log.get("schema") != economy_demo.SCHEMA_VERSION:
        return (False, f"field 'schema' must be {economy_demo.SCHEMA_VERSION!r} "
                       f"(got {log.get('schema')!r})")
    if log.get("simulated_days") != economy_demo.SIMULATED_DAYS:
        return (False, f"field 'simulated_days' must be {economy_demo.SIMULATED_DAYS} "
                       f"(got {log.get('simulated_days')!r})")
    per_day = log.get("per_day")
    if not isinstance(per_day, list) or len(per_day) != economy_demo.SIMULATED_DAYS:
        return (False, f"per_day must be a list of exactly "
                       f"{economy_demo.SIMULATED_DAYS} entries")
    summary = log.get("summary")
    if not isinstance(summary, dict):
        return (False, "summary must be a JSON object")
    for field in ("verified_count", "rejected_count", "total_earned", "total_spent",
                  "final_balance", "distinct_task_count"):
        v = summary.get(field)
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            return (False, f"summary.{field} must be a number")
    labels = log.get("labels")
    if not isinstance(labels, dict):
        return (False, "labels must be a JSON object")
    for field in ("zero_value", "no_token", "simulated_time"):
        if labels.get(field) is not True:
            return (False, f"labels.{field} must be true (honest labels are mandatory)")
    if labels.get("operator_relationship") != "same-operator":
        return (False, "labels.operator_relationship must be 'same-operator'")
    lh = log.get("economy_log_hash")
    if not isinstance(lh, str) or len(lh) != 64 or any(c not in _HEX for c in lh):
        return (False, "economy_log_hash must be a 64-char lowercase hex sha256")
    if economy_demo.compute_log_hash(log) != lh:
        return (False, "economy_log_hash does not recompute from the log's own content "
                       "(internally inconsistent file)")
    return (True, "ok: conforms to the economy-log schema")


def anchor_economy_summary(log: dict, ledger: Ledger) -> dict:
    """Validate + RE-RUN + anchor an economy-demo summary. Returns {evaluation, ledger_entry}.

    Malformed -> 'rejected', NOT anchored. Otherwise the coordinator RE-RUNS the entire
    30-simulated-day economy itself (fresh state, in-process) and compares log hashes:
    match -> 'economy-demo-confirmed'; anything else -> 'economy-demo-mismatch' (a real
    audit event). Both outcomes anchored.
    """
    ok, reason = validate_economy_log(log)
    if not ok:
        evaluation = {
            "event": _ECONOMY_EVENT,
            "stage": "Phase-1",
            "topology": "same-machine-simulated-economy",
            "status": "rejected",
            "reason": reason,
            "anchored": False,
            "zero_value": True,
            "no_token": True,
            "limitation_note": ECONOMY_LIMITATION_NOTE,
            "evaluated_at": time.time(),
        }
        return {"evaluation": evaluation, "ledger_entry": None}

    # RE-RUN the whole simulation (coordinator-reconfirms; never trust the file).
    rerun_error = None
    rerun = None
    try:
        rerun = economy_demo.simulate_all()
    except (AssertionError, KeyError, ValueError) as exc:
        rerun_error = f"{type(exc).__name__}: {exc}"
    hash_matches = bool(rerun is not None
                        and rerun["economy_log_hash"] == log["economy_log_hash"])
    status = "economy-demo-confirmed" if hash_matches else "economy-demo-mismatch"

    summary = log["summary"]
    # AGGREGATES ONLY — no per-day list. And deliberately NO task_id / task_ids keys
    # anywhere in this payload: work_molecule's citation scanner treats those keys as
    # "this record verifies that task" and would pull this record into every molecule's
    # verification_events, changing every WMID (and the ACI report) each time an
    # economy summary is anchored. distinct_task_count carries the coverage claim —
    # never a task list. This record summarizes a simulation; it verifies no task.
    evaluation = {
        "event": _ECONOMY_EVENT,
        "stage": "Phase-1",
        "topology": "same-machine-simulated-economy",
        "status": status,
        "task_class": _TASK_CLASS,
        "log_schema": log["schema"],
        "economy_log_hash": log["economy_log_hash"],
        "simulated_days": log["simulated_days"],
        "verified_count": summary["verified_count"],
        "rejected_count": summary["rejected_count"],
        # every rejection that was a PLANNED drill (drill=true in the log) — for the
        # 30-day demo this is the single day-17 tamper drill, never detected fraud
        "planned_drill_rejections": sum(
            1 for e in log["per_day"] if e.get("drill") and not e.get("verified")
        ),
        "total_earned": summary["total_earned"],
        "total_spent": summary["total_spent"],
        "final_balance": summary["final_balance"],
        "distinct_task_count": summary["distinct_task_count"],
        "coordinator_reconfirmed": {
            "rerun_log_hash": rerun["economy_log_hash"] if rerun else None,
            "log_hash_matches": hash_matches,
            "rerun_final_balance": rerun["summary"]["final_balance"] if rerun else None,
            "rerun_error": rerun_error,
        },
        "operator_relationship": "same-operator",
        "limitation_note": ECONOMY_LIMITATION_NOTE,
        "zero_value": True,
        "no_token": True,
        "anchored_at": time.time(),
    }
    entry = ledger.append(evaluation)
    return {"evaluation": evaluation, "ledger_entry": entry}


# ----------------------------------------------------------------------------
# Work-molecule catalog anchoring: validate + REBUILD-and-compare + anchor.
# ----------------------------------------------------------------------------
def validate_molecule_catalog(catalog: dict):
    """Structurally validate a work-molecule catalog file. Returns (ok, reason).

    Internal inconsistency (a catalog_hash that does not recompute from the file's own
    content) is MALFORMED input -> rejected before any ledger write. Whether the WMIDs
    are actually RIGHT is decided by the rebuild in anchor_molecule_catalog.
    """
    if not isinstance(catalog, dict):
        return (False, "catalog is not a JSON object")
    # Generation-aware: both catalog generations are valid; the molecule schema must
    # match its own catalog schema (0.1<->molecule/0.2, 0.2<->molecule/0.3).
    if catalog.get("schema") not in _CATALOG_GENERATIONS:
        return (False, f"field 'schema' must be one of "
                       f"{sorted(_CATALOG_GENERATIONS)} (got {catalog.get('schema')!r})")
    expected_molecule_schema = _CATALOG_GENERATIONS[catalog["schema"]]
    if catalog.get("molecule_schema") != expected_molecule_schema:
        return (False, f"field 'molecule_schema' must be {expected_molecule_schema!r} "
                       f"for catalog schema {catalog['schema']!r} "
                       f"(got {catalog.get('molecule_schema')!r})")
    entries = catalog.get("entries")
    if not isinstance(entries, list) or not entries:
        return (False, "entries must be a non-empty array")
    seen = []
    for i, e in enumerate(entries):
        if not isinstance(e, dict) or set(e.keys()) != {"task_id", "work_id"}:
            return (False, f"entries[{i}] must be exactly {{task_id, work_id}}")
        wid = e["work_id"]
        if not isinstance(wid, str) or len(wid) != 64 or any(c not in _HEX for c in wid):
            return (False, f"entries[{i}].work_id must be a 64-char lowercase hex sha256")
        try:
            verifier_cli.normalize_task_id(e["task_id"])
        except (KeyError, TypeError) as exc:
            return (False, f"entries[{i}] unknown task_id: {exc}")
        seen.append(e["task_id"])
    if seen != sorted(seen) or len(seen) != len(set(seen)):
        return (False, "entries must be unique and sorted by task_id")
    ch = catalog.get("catalog_hash")
    if not isinstance(ch, str) or len(ch) != 64 or any(c not in _HEX for c in ch):
        return (False, "catalog_hash must be a 64-char lowercase hex sha256")
    if work_molecule.compute_catalog_hash(catalog) != ch:
        return (False, "catalog_hash does not recompute from the catalog's own content "
                       "(internally inconsistent file)")
    return (True, "ok: conforms to the work-molecule-catalog schema")


def anchor_molecule_catalog(catalog: dict, ledger: Ledger) -> dict:
    """Validate + REBUILD + anchor a work-molecule catalog. Returns {evaluation, ledger_entry}.

    Malformed -> 'rejected', NOT anchored. Otherwise the coordinator REBUILDS every
    listed molecule from the ledger itself (protocol/work_molecule.py), recomputes the
    catalog hash, and compares: all-match -> 'molecule-catalog-confirmed'; anything
    else -> 'molecule-catalog-mismatch' (a real audit event). Both outcomes anchored.
    """
    ok, reason = validate_molecule_catalog(catalog)
    if not ok:
        evaluation = {
            "event": _CATALOG_EVENT,
            "stage": "R-provenance",
            "topology": "same-machine-molecule-assembly",
            "status": "rejected",
            "reason": reason,
            "anchored": False,
            "zero_value": True,
            "no_token": True,
            "limitation_note": CATALOG_LIMITATION_NOTE,
            "evaluated_at": time.time(),
        }
        return {"evaluation": evaluation, "ledger_entry": None}

    # REBUILD the catalog from this ledger (coordinator-reconfirms; never trust the
    # file) — in the submitted catalog's OWN generation, so a 0.2 catalog is verified
    # against a 0.2 rebuild and a 0.3 catalog against a 0.3 rebuild.
    task_ids = [e["task_id"] for e in catalog["entries"]]
    rebuild_error = None
    rebuilt = None
    try:
        rebuilt = work_molecule.build_catalog(ledger_path=ledger.path, task_ids=task_ids,
                                              schema_version=catalog["molecule_schema"])
    except (KeyError, ValueError) as exc:
        rebuild_error = f"{type(exc).__name__}: {exc}"
    hash_matches = bool(rebuilt is not None
                        and rebuilt["catalog_hash"] == catalog["catalog_hash"])
    entries_match = bool(rebuilt is not None
                         and rebuilt["entries"] == catalog["entries"])
    status = ("molecule-catalog-confirmed" if (hash_matches and entries_match)
              else "molecule-catalog-mismatch")

    evaluation = {
        "event": _CATALOG_EVENT,
        "stage": "R-provenance",
        "topology": "same-machine-molecule-assembly",
        "status": status,
        "task_class": _TASK_CLASS,
        "molecule_schema": catalog["molecule_schema"],
        "catalog_schema": catalog["schema"],
        "catalog_hash": catalog["catalog_hash"],
        "catalog_entry_count": len(catalog["entries"]),
        # DELIBERATELY under "catalog_entries", NOT "task_ids": work_molecule's citation
        # scanner treats a payload "task_ids" list as "this record verifies those tasks"
        # and would pull this record into every molecule's verification_events — changing
        # every WMID each time a catalog is anchored. This record CITES molecules; it
        # does not verify tasks, so it must stay invisible to the scanner.
        "catalog_entries": catalog["entries"],
        "coordinator_reconfirmed": {
            "recomputed_catalog_hash": rebuilt["catalog_hash"] if rebuilt else None,
            "catalog_hash_matches": hash_matches,
            "entries_match": entries_match,
            "rebuilt_entry_count": len(rebuilt["entries"]) if rebuilt else 0,
            "rebuild_error": rebuild_error,
        },
        "operator_relationship": "same-operator",
        "limitation_note": CATALOG_LIMITATION_NOTE,
        "zero_value": True,
        "no_token": True,
        "anchored_at": time.time(),
    }
    entry = ledger.append(evaluation)
    return {"evaluation": evaluation, "ledger_entry": entry}


# ----------------------------------------------------------------------------
# Metering-evidence anchoring: validate + PLAUSIBILITY-reconfirm + anchor.
# ----------------------------------------------------------------------------
def validate_metering_report(report: dict):
    """Structurally validate a metering report file. Returns (ok, reason).

    Malformed input (wrong shape, missing/dishonest labels, inexact energy
    arithmetic, or a report_hash that does not recompute from the file's own
    content) is rejected BEFORE the ledger is touched. Whether the CLAIMED
    measurements are plausible is decided in anchor_metering_report.
    """
    if not isinstance(report, dict):
        return (False, "metering report is not a JSON object")
    if report.get("schema") != task_metering.SCHEMA_VERSION:
        return (False, f"field 'schema' must be {task_metering.SCHEMA_VERSION!r} "
                       f"(got {report.get('schema')!r})")
    power = report.get("assumed_cpu_power_w")
    if not isinstance(power, (int, float)) or isinstance(power, bool) or power <= 0:
        return (False, "assumed_cpu_power_w must be a positive number")
    if not isinstance(report.get("power_method"), str) or not report["power_method"]:
        return (False, "power_method must be a non-empty string")
    rows = report.get("per_task")
    if not isinstance(rows, list) or not rows:
        return (False, "per_task must be a non-empty array")
    seen = []
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            return (False, f"per_task[{i}] is not a JSON object")
        if not isinstance(row.get("task_id"), str):
            return (False, f"per_task[{i}].task_id must be a string")
        try:
            verifier_cli.normalize_task_id(row["task_id"])
        except KeyError as exc:
            return (False, f"per_task[{i}] unknown task_id: {exc}")
        seen.append(row["task_id"])
        oh = row.get("output_hash")
        if not isinstance(oh, str) or len(oh) != 64 or any(c not in _HEX for c in oh):
            return (False, f"per_task[{i}].output_hash must be a 64-char lowercase "
                           "hex sha256")
        for key in ("wall_time_s", "cpu_time_s", "energy_j_estimate"):
            v = row.get(key)
            if not isinstance(v, (int, float)) or isinstance(v, bool) or v <= 0:
                return (False, f"per_task[{i}].{key} must be a positive number")
        # honesty labels are mandatory and exact: times measured, energy estimated —
        # a report claiming its energy figure is 'measured' is dishonest input.
        if row.get("labels") != task_metering.LABELS:
            return (False, f"per_task[{i}].labels must be exactly "
                           f"{task_metering.LABELS} (honest labels are mandatory)")
        # the energy ESTIMATE must be exactly its documented derivation from the
        # row's own recorded fields (re-checkable arithmetic, not a free claim)
        expected = round(row["cpu_time_s"] * power, 6)
        if row["energy_j_estimate"] != expected:
            return (False, f"per_task[{i}].energy_j_estimate must equal "
                           f"round(cpu_time_s x assumed_cpu_power_w, 6) = {expected}")
    if seen != sorted(seen) or len(seen) != len(set(seen)):
        return (False, "per_task rows must be unique and sorted by task_id")
    rh = report.get("report_hash")
    if not isinstance(rh, str) or len(rh) != 64 or any(c not in _HEX for c in rh):
        return (False, "report_hash must be a 64-char lowercase hex sha256")
    if task_metering.compute_report_hash(report) != rh:
        return (False, "report_hash does not recompute from the report's own content "
                       "(internally inconsistent file)")
    return (True, "ok: conforms to the metering-report schema")


def anchor_metering_report(report: dict, ledger: Ledger) -> dict:
    """Validate + PLAUSIBILITY-reconfirm + anchor a metering report.

    Returns {evaluation, ledger_entry}. Malformed -> 'rejected', NOT anchored. A
    report whose output_hashes do not all match the canonical ledger-recorded hashes
    is also 'rejected', NOT anchored — it does not describe the canonical work.
    Otherwise the coordinator RE-METERS every listed task itself and checks its own
    run's plausibility (output hashes match the ledger; every wall time inside the
    sanity band): all-good -> 'metering-evidence-confirmed'; anything else ->
    'metering-evidence-mismatch' (a real audit event). NOT byte-reconfirmed on
    purpose: timing is non-deterministic, so the anchored report_hash fixes the
    claim, not a recomputable value (see the block comment at _METERING_EVENT).
    """
    ok, reason = validate_metering_report(report)
    rejected = None
    if not ok:
        rejected = reason
    else:
        # (a) every submitted output_hash must match the canonical hash the ledger
        # already records for that task — checked BEFORE any ledger write.
        entries = ledger.read_all()
        bad = []
        for row in report["per_task"]:
            recorded = _find_ledger_task_hash(entries, row["task_id"])
            if recorded is None or recorded != row["output_hash"]:
                bad.append(row["task_id"])
        if bad:
            rejected = ("output_hash does not match the canonical ledger-recorded "
                        f"hash for: {bad}")
    if rejected is not None:
        evaluation = {
            "event": _METERING_EVENT,
            "stage": "R-provenance-debt",
            "topology": "same-machine-metering",
            "status": "rejected",
            "reason": rejected,
            "anchored": False,
            "zero_value": True,
            "no_token": True,
            "limitation_note": METERING_LIMITATION_NOTE,
            "evaluated_at": time.time(),
        }
        return {"evaluation": evaluation, "ledger_entry": None}

    # PLAUSIBILITY-reconfirm: the coordinator re-meters the same tasks itself. It can
    # never reproduce the submitted bytes (timing varies), so it checks what IS
    # re-derivable: its own output hashes match the ledger, and its own wall times
    # stay inside the sanity band.
    remeter_error = None
    own = None
    try:
        own = task_metering.build_report(
            task_ids=[row["task_id"] for row in report["per_task"]])
    except (KeyError, RuntimeError) as exc:
        remeter_error = f"{type(exc).__name__}: {exc}"
    own_rows = own["per_task"] if own else []
    entries = ledger.read_all()
    own_hashes_ok = bool(own_rows) and all(
        _find_ledger_task_hash(entries, r["task_id"]) == r["output_hash"]
        for r in own_rows
    )
    max_wall = max((r["wall_time_s"] for r in own_rows), default=None)
    sanity_ok = bool(own_rows) and all(
        r["wall_time_s"] < _METERING_SANITY_MAX_WALL_S for r in own_rows
    )
    status = (_METERING_CONFIRMED_STATUS if (own_hashes_ok and sanity_ok)
              else "metering-evidence-mismatch")

    rows = report["per_task"]
    evaluation = {
        "event": _METERING_EVENT,
        "stage": "R-provenance-debt",
        "topology": "same-machine-metering",
        "status": status,
        "task_class": _TASK_CLASS,
        "report_schema": report["schema"],
        "report_hash": report["report_hash"],
        # a COUNT, never a task list — the molecule citation scanner treats payload
        # task_id/task_ids keys as "this record verifies that task" and would pull
        # this record into every molecule, changing every 0.2 WMID. This record
        # meters tasks; it verifies none, so it must stay invisible to the scanner.
        "task_count": len(rows),
        "assumed_cpu_power_w": report["assumed_cpu_power_w"],
        "power_method": report["power_method"],
        "total_wall_time_s": round(sum(r["wall_time_s"] for r in rows), 6),
        "total_cpu_time_s": round(sum(r["cpu_time_s"] for r in rows), 6),
        "total_energy_j_estimate": round(
            sum(r["energy_j_estimate"] for r in rows), 6),
        "labels": dict(task_metering.LABELS),
        "coordinator_reconfirmed": {
            "remetered_task_count": len(own_rows),
            "remetered_output_hashes_match_ledger": own_hashes_ok,
            "remetered_total_wall_time_s": (
                round(sum(r["wall_time_s"] for r in own_rows), 6) if own_rows else None),
            "remetered_total_cpu_time_s": (
                round(sum(r["cpu_time_s"] for r in own_rows), 6) if own_rows else None),
            "remetered_max_task_wall_time_s": max_wall,
            "timings_within_sanity_band": sanity_ok,
            "remeter_error": remeter_error,
        },
        "operator_relationship": "same-operator",
        "limitation_note": METERING_LIMITATION_NOTE,
        "zero_value": True,
        "no_token": True,
        "anchored_at": time.time(),
    }
    entry = ledger.append(evaluation)
    return {"evaluation": evaluation, "ledger_entry": entry}


# ----------------------------------------------------------------------------
# Cut-certificate anchoring: validate + FULL-verify + anchor.
# ----------------------------------------------------------------------------
def anchor_cut_certificate(cert: dict, ledger: Ledger) -> dict:
    """Validate + FULLY VERIFY + anchor a cut certificate. Returns {evaluation, ledger_entry}.

    Malformed (including an aggregate_hash or certificate_hash that does not
    recompute from the file's own content) -> 'rejected', NOT anchored. Otherwise
    the coordinator runs the EXPENSIVE full verification itself — rebuild every
    interior molecule from the ledger, recompute every WMID and the aggregate_hash
    (cut_certificate.verify_full): pass -> 'cut-certificate-confirmed'; anything
    else -> 'cut-certificate-mismatch' (a real audit event). The full proof happens
    exactly once, here — that is what makes later cheap accept_by_anchor sound.
    """
    ok, reasons = cut_certificate.validate_certificate(cert)
    if not ok:
        evaluation = {
            "event": _CUT_EVENT,
            "stage": "R-provenance",
            "topology": "same-machine-cut-verification",
            "status": "rejected",
            "reason": "; ".join(reasons),
            "anchored": False,
            "zero_value": True,
            "no_token": True,
            "limitation_note": CUT_LIMITATION_NOTE,
            "evaluated_at": time.time(),
        }
        return {"evaluation": evaluation, "ledger_entry": None}

    # ANCHOR-TIME FULL PROOF (the expensive path, run exactly once).
    verify_error = None
    full_ok = False
    full_reasons = []
    try:
        full_ok, full_reasons = cut_certificate.verify_full(cert,
                                                            ledger_path=ledger.path)
    except (KeyError, ValueError) as exc:
        verify_error = f"{type(exc).__name__}: {exc}"
    status = _CUT_CONFIRMED_STATUS if full_ok else "cut-certificate-mismatch"

    # COUNTS ONLY — no task/work-id lists: the molecule citation scanner treats
    # payload task_id/task_ids keys as "this record verifies that task" and would
    # pull this record into every molecule, moving every WMID. This record
    # summarizes molecules; it verifies no task, so it stays scanner-invisible.
    evaluation = {
        "event": _CUT_EVENT,
        "stage": "R-provenance",
        "topology": "same-machine-cut-verification",
        "status": status,
        "task_class": _TASK_CLASS,
        "certificate_schema": cert["schema"],
        "certificate_hash": cert["certificate_hash"],
        "aggregate_hash": cert["aggregate_hash"],
        "molecule_schema": cert["molecule_schema"],
        "interior_count": cert["interior_count"],
        "boundary_count": len(cert["boundary_input_ids"]),
        "verification_policy_version": cert["verification_policy_version"],
        "coordinator_reconfirmed": {
            "full_verification_passed": full_ok,
            "rebuilt_interior_count": cert["interior_count"] if full_ok else None,
            "first_failure_reason": (full_reasons[0] if full_reasons else None),
            "verify_error": verify_error,
        },
        "operator_relationship": "same-operator",
        "limitation_note": CUT_LIMITATION_NOTE,
        "zero_value": True,
        "no_token": True,
        "anchored_at": time.time(),
    }
    entry = ledger.append(evaluation)
    return {"evaluation": evaluation, "ledger_entry": entry}


# ----------------------------------------------------------------------------
# Trust-vector-catalog anchoring: validate + REBUILD-and-compare + anchor.
# ----------------------------------------------------------------------------
def anchor_trust_vector_catalog(catalog: dict, ledger: Ledger) -> dict:
    """Validate + REBUILD + anchor a trust-vector catalog. Returns {evaluation, ledger_entry}.

    Malformed -> 'rejected', NOT anchored. Otherwise the coordinator REBUILDS every
    listed vector from the ledger itself (protocol/trust_vector.py), recomputes the
    catalog hash, and compares: match -> 'trust-vector-catalog-confirmed'; anything
    else -> 'trust-vector-catalog-mismatch' (a real audit event). Both outcomes
    anchored. The record carries COUNTS only plus the no-scalar affirmation.
    """
    ok, reasons = trust_vector.validate_catalog(catalog)
    if not ok:
        evaluation = {
            "event": _TV_EVENT,
            "stage": "R-trust",
            "topology": "same-machine-trust-vector",
            "status": "rejected",
            "reason": "; ".join(reasons),
            "anchored": False,
            "zero_value": True,
            "no_token": True,
            "limitation_note": TV_LIMITATION_NOTE,
            "evaluated_at": time.time(),
        }
        return {"evaluation": evaluation, "ledger_entry": None}

    # REBUILD the catalog from this ledger (coordinator-reconfirms; never trust the file).
    task_ids = [e["task_id"] for e in catalog["vector_entries"]]
    rebuild_error = None
    rebuilt = None
    try:
        rebuilt = trust_vector.build_tv_catalog(ledger_path=ledger.path,
                                                task_ids=task_ids)
    except (KeyError, ValueError) as exc:
        rebuild_error = f"{type(exc).__name__}: {exc}"
    hash_matches = bool(rebuilt is not None
                        and rebuilt["catalog_hash"] == catalog["catalog_hash"])
    entries_match = bool(rebuilt is not None
                         and rebuilt["vector_entries"] == catalog["vector_entries"])
    status = (_TV_CONFIRMED_STATUS if (hash_matches and entries_match)
              else "trust-vector-catalog-mismatch")

    # COUNTS ONLY — no task or hash lists: the molecule citation scanner treats
    # payload task_id/task_ids keys as "this record verifies that task" and would
    # pull this record into every molecule, moving every WMID (and therefore every
    # trust vector). This record catalogs vectors; it verifies no task.
    evaluation = {
        "event": _TV_EVENT,
        "stage": "R-trust",
        "topology": "same-machine-trust-vector",
        "status": status,
        "task_class": _TASK_CLASS,
        "catalog_schema": catalog["schema"],
        "catalog_hash": catalog["catalog_hash"],
        "vector_count": len(catalog["vector_entries"]),
        "no_combined_scalar": NO_SCALAR_AFFIRMATION,
        "coordinator_reconfirmed": {
            "recomputed_catalog_hash": rebuilt["catalog_hash"] if rebuilt else None,
            "catalog_hash_matches": hash_matches,
            "entries_match": entries_match,
            "rebuilt_vector_count": len(rebuilt["vector_entries"]) if rebuilt else 0,
            "rebuild_error": rebuild_error,
        },
        "operator_relationship": "same-operator",
        "limitation_note": TV_LIMITATION_NOTE,
        "zero_value": True,
        "no_token": True,
        "anchored_at": time.time(),
    }
    entry = ledger.append(evaluation)
    return {"evaluation": evaluation, "ledger_entry": entry}


# ----------------------------------------------------------------------------
# Treasury config anchoring: validate + independent fee re-derivation + anchor.
# ----------------------------------------------------------------------------
def anchor_treasury_config(state: dict, ledger: Ledger) -> dict:
    """Validate + RE-DERIVE + anchor the treasury constitution. Returns
    {evaluation, ledger_entry}.

    The coordinator re-derives the fees INDEPENDENTLY from the anchored economy
    (never trusting the submitted state): a state claiming more fees than the
    anchored log yields, or violating conservation, is REJECTED — the
    conservation-violation path is the whole point of anchoring a constitution.
    """
    reason = None
    if not isinstance(state, dict):
        reason = "treasury state is not a JSON object"
    elif state.get("schema") != metastar_treasury.SCHEMA_VERSION:
        reason = (f"field 'schema' must be "
                  f"{metastar_treasury.SCHEMA_VERSION!r}")
    elif not isinstance(state.get("config"), dict):
        reason = "config must be a JSON object"
    else:
        try:
            metastar_treasury.assert_conservation(state)
        except (AssertionError, KeyError, TypeError) as exc:
            reason = f"conservation audit failed: {exc}"
    rederived = None
    if reason is None:
        try:
            funding_root, _per_day, total = metastar_treasury.derive_fees(
                ledger.path)
            rederived = (funding_root, total)
        except ValueError as exc:
            reason = f"fee re-derivation failed: {exc}"
    if reason is None:
        if state.get("funding_root") != rederived[0]:
            reason = (f"funding_root {state.get('funding_root')!r} does not "
                      f"match the anchored economy record ({rederived[0]})")
        elif round(state.get("total_fees_collected", -1), 6) != rederived[1]:
            reason = (f"claimed total_fees_collected "
                      f"{state.get('total_fees_collected')} does not re-derive "
                      f"from the anchored economy log (coordinator derived "
                      f"{rederived[1]}) — the treasury cannot claim units the "
                      "economy never produced")
    if reason is not None:
        evaluation = {
            "event": _TREASURY_EVENT,
            "stage": "R-treasury",
            "topology": "same-machine-treasury",
            "status": "rejected",
            "reason": reason,
            "anchored": False,
            "zero_value": True,
            "no_token": True,
            "limitation_note": TREASURY_LIMITATION_NOTE,
            "evaluated_at": time.time(),
        }
        return {"evaluation": evaluation, "ledger_entry": None}

    evaluation = {
        "event": _TREASURY_EVENT,
        "stage": "R-treasury",
        "topology": "same-machine-treasury",
        "status": _TREASURY_STATUS,
        "task_class": _TASK_CLASS,
        "treasury_schema": state["schema"],
        "fee_rate": state["config"]["fee_rate"],
        "per_bounty_cap": state["config"]["per_bounty_cap"],
        "category_budgets": dict(state["config"]["category_budgets"]),
        "funding_root": state["funding_root"],
        "total_fees_collected": state["total_fees_collected"],
        "conservation_statement": (
            "total_outflow + balance == total_fees_collected, asserted at "
            "every operation; the only inflow is anchored-economy fees; no "
            "mint path exists in the module"),
        "coordinator_reconfirmed": {
            "rederived_funding_root": rederived[0],
            "rederived_total_fees": rederived[1],
            "fees_match": True,
        },
        "operator_relationship": "same-operator",
        "limitation_note": TREASURY_LIMITATION_NOTE,
        "zero_value": True,
        "no_token": True,
        "anchored_at": time.time(),
    }
    entry = ledger.append(evaluation)
    return {"evaluation": evaluation, "ledger_entry": entry}


# ----------------------------------------------------------------------------
# Gate-3 lifecycle anchoring: per-phase coordinator reconfirm + anchor.
# ----------------------------------------------------------------------------
def anchor_gate3_event(request: dict, ledger: Ledger, drill: bool = False) -> dict:
    """Validate + RECONFIRM + anchor one Gate-3 lifecycle event. Returns
    {evaluation, ledger_entry}.

    request = {phase, bounty_id, and per-phase fields: work_ref+amount+category
    (provisional), grounds (challenge)}. The coordinator recomputes everything
    itself before anchoring: the machine pre-check (provisional), window
    arithmetic against the CURRENT chain (challenge/finalization — never
    fudged: a window that has not closed rejects finalization), the scripted
    adjudication rule and the bounded-failure arithmetic (clawback), and the
    treasury bounds from ANCHORED records only. Precondition failures ->
    'rejected', NOT anchored.
    """
    phase = request.get("phase") if isinstance(request, dict) else None
    bounty_id = request.get("bounty_id") if isinstance(request, dict) else None
    reason = None
    extra = {}
    if phase not in gate3_process.LIFECYCLE_EVENTS:
        reason = (f"phase must be one of {list(gate3_process.LIFECYCLE_EVENTS)} "
                  f"(got {phase!r})")
    elif not isinstance(bounty_id, str) or not bounty_id:
        reason = "bounty_id must be a non-empty string"

    entries = ledger.read_all()
    config_entry, fees, granted, clawed, by_bounty = (
        _anchored_treasury_flows(entries))
    slot = by_bounty.get(bounty_id, {})

    if reason is None and phase == gate3_process.EVENT_PROVISIONAL:
        work_ref = request.get("work_ref")
        amount = request.get("amount")
        category = request.get("category")
        if not (isinstance(work_ref, dict) and isinstance(amount, (int, float))
                and isinstance(category, str)):
            reason = "provisional requires work_ref, amount, category"
        elif config_entry is None:
            reason = "no anchored treasury config — bounties need a constitution"
        elif "provisional" in slot:
            reason = f"bounty {bounty_id!r} already has a provisional grant"
        else:
            amount = round(amount, 6)
            cfg = config_entry["payload"]
            gross = round(sum(
                b["provisional"]["payload"]["amount"] for b in by_bounty.values()
                if "provisional" in b
                and b["provisional"]["payload"].get("category") == category), 6)
            precheck = gate3_process.submit_work_item(work_ref,
                                                     ledger_path=ledger.path)
            if amount > cfg["per_bounty_cap"]:
                reason = (f"amount {amount} exceeds the anchored per_bounty_cap "
                          f"{cfg['per_bounty_cap']}")
            elif category not in cfg["category_budgets"]:
                reason = f"unknown category {category!r}"
            elif round(gross + amount, 6) > cfg["category_budgets"][category]:
                reason = (f"category {category!r} gross {gross} + {amount} "
                          f"would exceed budget "
                          f"{cfg['category_budgets'][category]}")
            elif round(amount, 6) > round(fees - (granted - clawed), 6):
                reason = (f"amount {amount} exceeds the anchored treasury "
                          f"balance {round(fees - (granted - clawed), 6)}")
            elif not precheck["passed"]:
                failed = [c["name"] for c in precheck["checks"]
                          if not c["passed"]]
                reason = f"machine pre-check FAILED: {failed}"
            else:
                extra = {
                    "task_id": work_ref["task_id"],  # deliberate molecule routing
                    "work_id": work_ref["work_id"],
                    "taxonomy_tag": work_ref["taxonomy_tag"],
                    "amount": amount,
                    "category": category,
                    "precheck": {"passed": True, "checks": precheck["checks"]},
                    "treasury_config_ledger_index": config_entry["index"],
                }

    if reason is None and phase == gate3_process.EVENT_CHALLENGE:
        grounds = request.get("grounds")
        if not isinstance(grounds, str) or not grounds:
            reason = "challenge requires grounds"
        elif "provisional" not in slot:
            reason = f"no provisional grant anchored for {bounty_id!r}"
        else:
            prov_idx = slot["provisional"]["index"]
            next_index = entries[-1]["index"] + 1  # where THIS record will land
            if not gate3_process.challenge_in_window(prov_idx, next_index):
                reason = (f"challenge window expired: this challenge would "
                          f"anchor at index {next_index}, outside "
                          f"{prov_idx}+1..{prov_idx}+"
                          f"{gate3_process.WINDOW_ENTRIES}")
            else:
                extra = {
                    "task_id": slot["provisional"]["payload"]["task_id"],
                    "grounds": grounds,
                    "provisional_ledger_index": prov_idx,
                    "window_entries": gate3_process.WINDOW_ENTRIES,
                }

    if reason is None and phase == gate3_process.EVENT_CLAWBACK:
        if "provisional" not in slot:
            reason = f"no provisional grant anchored for {bounty_id!r}"
        elif "challenge" not in slot:
            reason = (f"no challenge anchored for {bounty_id!r} — clawback "
                      "requires an adjudicated challenge")
        elif "clawback" in slot or "finalization" in slot:
            reason = f"bounty {bounty_id!r} is already resolved"
        else:
            prov = slot["provisional"]
            amount = prov["payload"]["amount"]
            verdict = gate3_process.adjudicate(slot["challenge"]["payload"])
            cap = config_entry["payload"]["per_bounty_cap"]
            balance_after = round(fees - (granted - clawed - amount), 6)
            extra = {
                "task_id": prov["payload"]["task_id"],
                "amount_returned": amount,
                "provisional_ledger_index": prov["index"],
                "challenge_ledger_index": slot["challenge"]["index"],
                "adjudication": verdict,
                "bounded_failure": {
                    "max_exposure": amount,
                    "per_bounty_cap": cap,
                    "statement": (
                        f"maximum possible exposure was {amount} (per-bounty "
                        f"cap {cap}); the monetary base (faucet flow) was "
                        "untouchable from this flow by construction; treasury "
                        f"balance restored to {balance_after} (fees {fees} "
                        f"minus outstanding "
                        f"{round(granted - clawed - amount, 6)}) — a failed "
                        "bounty costs the treasury budget, never the base"),
                },
            }

    if reason is None and phase == gate3_process.EVENT_FINALIZATION:
        if "provisional" not in slot:
            reason = f"no provisional grant anchored for {bounty_id!r}"
        elif "clawback" in slot:
            reason = f"bounty {bounty_id!r} was clawed back"
        elif "finalization" in slot:
            reason = f"bounty {bounty_id!r} already finalized"
        else:
            prov = slot["provisional"]
            closed, note = gate3_process.window_closed(prov["index"], entries,
                                                       bounty_id)
            if not closed:
                reason = f"cannot finalize: {note}"
            else:
                amount = prov["payload"]["amount"]
                extra = {
                    "task_id": prov["payload"]["task_id"],
                    "amount_paid": amount,
                    "provisional_ledger_index": prov["index"],
                    "window_note": note,
                    "process_statement": PROCESS_PASSED_STATEMENT,
                    "treasury_totals": {
                        "total_fees_collected": fees,
                        "total_paid": amount,
                        "balance": round(fees - (granted - clawed), 6),
                    },
                }

    if reason is not None:
        evaluation = {
            "event": phase if phase in gate3_process.LIFECYCLE_EVENTS
            else "gate3_process_event",
            "stage": "R-gate3",
            "topology": "same-operator-single-seat-council",
            "status": "rejected",
            "reason": reason,
            "anchored": False,
            "zero_value": True,
            "no_token": True,
            "limitation_note": GATE3_LIMITATION_NOTE,
            "evaluated_at": time.time(),
        }
        return {"evaluation": evaluation, "ledger_entry": None}

    evaluation = {
        "event": phase,
        "stage": "R-gate3",
        "topology": "same-operator-single-seat-council",
        "status": _GATE3_STATUS[phase],
        "task_class": _TASK_CLASS,
        "bounty_id": bounty_id,
        "process_schema": gate3_process.SCHEMA_VERSION,
        "operator_relationship": "same-operator",
        "limitation_note": GATE3_LIMITATION_NOTE,
        "zero_value": True,
        "no_token": True,
        "anchored_at": time.time(),
    }
    evaluation.update(extra)
    if drill:
        evaluation["drill"] = True
    entry = ledger.append(evaluation)
    return {"evaluation": evaluation, "ledger_entry": entry}


# ----------------------------------------------------------------------------
# Passport-catalog anchoring: validate + REBUILD-and-compare + anchor.
# ----------------------------------------------------------------------------
def anchor_passport_catalog(catalog: dict, ledger: Ledger) -> dict:
    """Validate + REBUILD + anchor a MetaWork passport catalog. Returns
    {evaluation, ledger_entry}. Malformed -> 'rejected', NOT anchored;
    otherwise the coordinator rediscovers every actor and rebuilds every
    passport itself: match -> 'passport-catalog-confirmed', else
    'passport-catalog-mismatch' (anchored audit event). COUNTS + catalog_hash
    only — no actor lists, no task keys (scanner-invisible)."""
    ok, reasons = metawork_passport.validate_catalog(catalog)
    if not ok:
        evaluation = {
            "event": _PASSPORT_EVENT,
            "stage": "R-passport",
            "topology": "same-machine-passport-assembly",
            "status": "rejected",
            "reason": "; ".join(reasons),
            "anchored": False,
            "zero_value": True,
            "no_token": True,
            "limitation_note": PASSPORT_LIMITATION_NOTE,
            "evaluated_at": time.time(),
        }
        return {"evaluation": evaluation, "ledger_entry": None}

    rebuild_error = None
    rebuilt = None
    try:
        rebuilt = metawork_passport.build_passport_catalog(
            ledger_path=ledger.path)
    except (KeyError, ValueError) as exc:
        rebuild_error = f"{type(exc).__name__}: {exc}"
    hash_matches = bool(rebuilt is not None
                        and rebuilt["catalog_hash"] == catalog["catalog_hash"])
    entries_match = bool(rebuilt is not None
                         and rebuilt["entries"] == catalog["entries"])
    status = (_PASSPORT_STATUS if (hash_matches and entries_match)
              else "passport-catalog-mismatch")

    evaluation = {
        "event": _PASSPORT_EVENT,
        "stage": "R-passport",
        "topology": "same-machine-passport-assembly",
        "status": status,
        "task_class": _TASK_CLASS,
        "catalog_schema": catalog["schema"],
        "catalog_hash": catalog["catalog_hash"],
        "actor_count": len(catalog["entries"]),
        "no_leaderboard": NO_LEADERBOARD_AFFIRMATION,
        "uww_transparency": UWW_TRANSPARENCY_STATEMENT,
        "coordinator_reconfirmed": {
            "recomputed_catalog_hash": rebuilt["catalog_hash"] if rebuilt else None,
            "catalog_hash_matches": hash_matches,
            "entries_match": entries_match,
            "rebuilt_actor_count": len(rebuilt["entries"]) if rebuilt else 0,
            "rebuild_error": rebuild_error,
        },
        "operator_relationship": "same-operator",
        "limitation_note": PASSPORT_LIMITATION_NOTE,
        "zero_value": True,
        "no_token": True,
        "anchored_at": time.time(),
    }
    entry = ledger.append(evaluation)
    return {"evaluation": evaluation, "ledger_entry": entry}


# ----------------------------------------------------------------------------
# Flow-1 anchoring: forged-heartbeat drill + uptime epoch, coordinator-reconfirmed.
# ----------------------------------------------------------------------------
def _find_actor_root(entries, actor_id):
    """(ledger_index, merkle_root) of the actor's registration, or (None, None)."""
    found = (None, None)
    for e in entries:
        p = e.get("payload") if isinstance(e, dict) else None
        if (isinstance(p, dict) and p.get("event") == _REGISTRATION_EVENT
                and p.get("status") == _REGISTRATION_STATUS
                and p.get("actor_id") == actor_id):
            found = (e["index"], p.get("merkle_root"))
    return found


def anchor_forged_heartbeat_drill(heartbeat: dict, ledger: Ledger) -> dict:
    """Verify a claimed-forged heartbeat FAILS against the actor's anchored
    root, then anchor the rejection as a drill. Returns {evaluation, ledger_entry}.

    A heartbeat that actually VERIFIES is rejected as input — this path anchors
    demonstrated rejections only, never real emissions. Flow 1's anchored
    counter-demonstration, sibling to the copy-attack and key-reuse drills.
    """
    reason = None
    if not isinstance(heartbeat, dict) or "signature" not in heartbeat:
        reason = "heartbeat is not a JSON object with a signature"
    entries = ledger.read_all()
    root_idx, root = (None, None)
    if reason is None:
        root_idx, root = _find_actor_root(entries, heartbeat.get("actor_id"))
        if root is None:
            reason = (f"no anchored key root for actor "
                      f"{heartbeat.get('actor_id')!r}")
    verdict = None
    hb_reasons = []
    if reason is None:
        verdict, hb_reasons = flow1_uptime.verify_heartbeat(
            heartbeat, root, ledger_path=ledger.path)
        if verdict:
            reason = ("heartbeat VERIFIES against the anchored root — not a "
                      "forgery; refusing to anchor a valid heartbeat as a "
                      "rejection drill")
    if reason is not None:
        evaluation = {
            "event": _HEARTBEAT_EVENT,
            "stage": "R-flow1",
            "topology": "same-operator-uptime-node",
            "status": "rejected",
            "reason": reason,
            "anchored": False,
            "zero_value": True,
            "no_token": True,
            "limitation_note": FLOW1_LIMITATION_NOTE,
            "evaluated_at": time.time(),
        }
        return {"evaluation": evaluation, "ledger_entry": None}

    evaluation = {
        "event": _HEARTBEAT_EVENT,
        "stage": "R-flow1",
        "topology": "same-operator-uptime-node",
        "status": _HEARTBEAT_REJECTED_STATUS,
        "task_class": _TASK_CLASS,
        "actor_id": heartbeat["actor_id"],
        "slot_index": heartbeat.get("slot_index"),
        "key_root_ledger_index": root_idx,
        "emitted": 0.0,  # a rejected heartbeat emits nothing, by objective rule
        "first_failure_reason": hb_reasons[0] if hb_reasons else None,
        "simulated_time": True,
        "drill": True,
        "operator_relationship": "same-operator",
        "limitation_note": FLOW1_LIMITATION_NOTE,
        "zero_value": True,
        "no_token": True,
        "anchored_at": time.time(),
    }
    entry = ledger.append(evaluation)
    return {"evaluation": evaluation, "ledger_entry": entry}


def anchor_uptime_epoch(epoch: dict, ledger: Ledger) -> dict:
    """Validate + FULLY RE-VERIFY + anchor an uptime epoch. Returns
    {evaluation, ledger_entry}.

    Malformed (bad shape or an epoch_hash that does not recompute) ->
    'rejected', NOT anchored; no anchored actor root -> rejected. Otherwise
    the coordinator re-verifies EVERY heartbeat signature and replays the
    emission arithmetic itself (flow1_uptime.verify_epoch): pass ->
    'uptime-epoch-confirmed'; fail -> 'uptime-epoch-mismatch' (anchored audit
    event). The record carries the consumed key_indices, feeding the
    ledger-wide cross-type one-time-key scan.
    """
    reason = None
    if not isinstance(epoch, dict):
        reason = "epoch is not a JSON object"
    elif epoch.get("schema") != flow1_uptime.SCHEMA_VERSION:
        reason = f"field 'schema' must be {flow1_uptime.SCHEMA_VERSION!r}"
    elif flow1_uptime.compute_epoch_hash(epoch) != epoch.get("epoch_hash"):
        reason = ("epoch_hash does not recompute from the file's own content "
                  "(internally inconsistent file)")
    entries = ledger.read_all()
    root_idx, root = (None, None)
    if reason is None:
        root_idx, root = _find_actor_root(entries, epoch.get("actor_id"))
        if root is None:
            reason = f"no anchored key root for actor {epoch.get('actor_id')!r}"
    if reason is not None:
        evaluation = {
            "event": _EPOCH_EVENT,
            "stage": "R-flow1",
            "topology": "same-operator-uptime-node",
            "status": "rejected",
            "reason": reason,
            "anchored": False,
            "zero_value": True,
            "no_token": True,
            "limitation_note": FLOW1_LIMITATION_NOTE,
            "evaluated_at": time.time(),
        }
        return {"evaluation": evaluation, "ledger_entry": None}

    ok, verify_reasons = flow1_uptime.verify_epoch(epoch, root,
                                                   ledger_path=ledger.path)
    status = _EPOCH_STATUS if ok else "uptime-epoch-mismatch"
    s = epoch["summary"]
    evaluation = {
        "event": _EPOCH_EVENT,
        "stage": "R-flow1",
        "topology": "same-operator-uptime-node",
        "status": status,
        "task_class": _TASK_CLASS,
        "epoch_schema": epoch["schema"],
        "epoch_hash": epoch["epoch_hash"],
        "actor_id": epoch["actor_id"],
        "key_root_ledger_index": root_idx,
        "slot_count": epoch["slot_count"],
        "verified_slots": s["verified_slots"],
        "missed_slots": list(s["missed_slots"]),
        "per_slot_emission": epoch["per_slot_emission"],
        "total_emitted": s["total_emitted"],
        "epoch_cap": epoch["epoch_cap"],
        "cap_respected": s["cap_respected"],
        # the consumed one-time indices — feeds the CROSS-TYPE reuse scan
        "key_indices": sorted(hb["key_index"] for hb in epoch["heartbeats"]),
        "missed_slot_statement": MISSED_SLOT_STATEMENT,
        "two_flow_separation": TWO_FLOW_SEPARATION_STATEMENT,
        "coordinator_reconfirmed": {
            "epoch_verified": ok,
            "heartbeats_reverified": len(epoch["heartbeats"]),
            "first_failure_reason": verify_reasons[0] if verify_reasons else None,
        },
        "simulated_time": True,
        "operator_relationship": "same-operator",
        "limitation_note": FLOW1_LIMITATION_NOTE,
        "zero_value": True,
        "no_token": True,
        "anchored_at": time.time(),
    }
    entry = ledger.append(evaluation)
    return {"evaluation": evaluation, "ledger_entry": entry}


# ----------------------------------------------------------------------------
# Actor-key registration: validate (public-only) + uniqueness + anchor.
# ----------------------------------------------------------------------------
def validate_key_declaration(declaration):
    """Structurally validate a PUBLIC key declaration. Returns (ok, reason).

    Rejects, before any ledger write: wrong schema/scheme, malformed root or
    counts, and — hard rule — ANY private material anywhere in the file.
    """
    if not isinstance(declaration, dict):
        return (False, "declaration is not a JSON object")
    if _contains_private_material(declaration):
        return (False, "declaration contains PRIVATE key material — registration "
                       "is public-only (root + counts + leaf-hash commitment); "
                       "never submit a keychain file")
    for field in ("schema", "actor_id", "scheme", "key_count", "merkle_root",
                  "leaf_hashes_hash"):
        if field not in declaration:
            return (False, f"missing required field '{field}'")
    if declaration["schema"] != actor_identity.DECLARATION_SCHEMA:
        return (False, f"field 'schema' must be "
                       f"{actor_identity.DECLARATION_SCHEMA!r}")
    if declaration["scheme"] != actor_identity.SCHEME:
        return (False, f"field 'scheme' must be {actor_identity.SCHEME!r} "
                       f"(got {declaration['scheme']!r})")
    if not isinstance(declaration["actor_id"], str) or not declaration["actor_id"]:
        return (False, "actor_id must be a non-empty string")
    kc = declaration["key_count"]
    if not isinstance(kc, int) or isinstance(kc, bool) or kc < 1 or kc & (kc - 1):
        return (False, "key_count must be a positive power of two")
    for field in ("merkle_root", "leaf_hashes_hash"):
        v = declaration[field]
        if not (isinstance(v, str) and len(v) == 64
                and all(c in _HEX for c in v)):
            return (False, f"{field} must be a 64-char lowercase hex sha256")
    return (True, "ok: conforms to the actor-key-declaration schema")


def register_actor_key(declaration: dict, ledger: Ledger) -> dict:
    """Validate + uniqueness-check + anchor an actor key registration.

    Malformed or private-material-carrying declarations -> 'rejected', NOT
    anchored. A duplicate actor_id or duplicate merkle_root is also rejected
    (one active root per actor in v0). Returns {evaluation, ledger_entry}.
    """
    ok, reason = validate_key_declaration(declaration)
    if ok:
        for e in ledger.read_all():
            p = e.get("payload", {})
            if (p.get("event") == _REGISTRATION_EVENT
                    and p.get("status") == _REGISTRATION_STATUS):
                if p.get("actor_id") == declaration["actor_id"]:
                    ok, reason = (False,
                                  f"actor {declaration['actor_id']!r} already has "
                                  f"an active root (ledger index {e['index']}) — "
                                  "one active root per actor in v0; rotation is "
                                  "future work")
                    break
                if p.get("merkle_root") == declaration["merkle_root"]:
                    ok, reason = (False,
                                  f"this merkle_root is already registered "
                                  f"(ledger index {e['index']})")
                    break
    if not ok:
        evaluation = {
            "event": _REGISTRATION_EVENT,
            "stage": "R-identity",
            "topology": "same-operator-key-custody",
            "status": "rejected",
            "reason": reason,
            "anchored": False,
            "zero_value": True,
            "no_token": True,
            "limitation_note": REGISTRATION_LIMITATION_NOTE,
            "evaluated_at": time.time(),
        }
        return {"evaluation": evaluation, "ledger_entry": None}

    evaluation = {
        "event": _REGISTRATION_EVENT,
        "stage": "R-identity",
        "topology": "same-operator-key-custody",
        "status": _REGISTRATION_STATUS,
        "task_class": _TASK_CLASS,
        "actor_id": declaration["actor_id"],
        "scheme": declaration["scheme"],
        "key_count": declaration["key_count"],
        "merkle_root": declaration["merkle_root"],
        "leaf_hashes_hash": declaration["leaf_hashes_hash"],
        "operator_relationship": "same-operator",
        "limitation_note": REGISTRATION_LIMITATION_NOTE,
        "zero_value": True,
        "no_token": True,
        "anchored_at": time.time(),
    }
    entry = ledger.append(evaluation)
    return {"evaluation": evaluation, "ledger_entry": entry}


# ----------------------------------------------------------------------------
# Challenge-response anchoring: validate + FULL-recompute-verify + anchor.
# ----------------------------------------------------------------------------
def anchor_challenge_result(challenge: dict, response: dict, ledger: Ledger,
                            drill: bool = False) -> dict:
    """Validate + VERIFY (full recompute) + anchor a challenge-response round.

    Malformed challenge/response -> 'rejected', NOT anchored. Otherwise the
    coordinator runs verify_response ITSELF (re-runs the task, re-derives both
    hashes from the nonce — the reconfirm pattern): pass -> 'challenge-verified';
    fail -> 'challenge-failed' (a real, anchorable audit event — and with
    drill=True, a PLANNED demonstration labeled as such). Returns
    {evaluation, ledger_entry}.
    """
    ok_c, c_reasons = challenge_response.validate_challenge(challenge)
    ok_r, r_reasons = challenge_response.validate_response(response)
    if not (ok_c and ok_r):
        evaluation = {
            "event": _CHALLENGE_EVENT,
            "stage": "R-challenge",
            "topology": "challenge-response-same-operator",
            "status": "rejected",
            "reason": "; ".join([f"challenge: {r}" for r in c_reasons] +
                                [f"response: {r}" for r in r_reasons]),
            "anchored": False,
            "zero_value": True,
            "no_token": True,
            "limitation_note": CHALLENGE_LIMITATION_NOTE,
            "evaluated_at": time.time(),
        }
        return {"evaluation": evaluation, "ledger_entry": None}

    # FULL recompute verification by the coordinator (never trust the files).
    verdict, reasons = challenge_response.verify_response(challenge, response,
                                                          ledger_path=ledger.path)
    status = _CHALLENGE_VERIFIED_STATUS if verdict else _CHALLENGE_FAILED_STATUS
    note = CHALLENGE_LIMITATION_NOTE + (CHALLENGE_DRILL_NOTE if drill else "")
    # actor-identity layer: record the signature FACTS (never the ~50 KB
    # signature itself — that lives in the evidence bundle; the record carries
    # what history-scanning needs: who signed with which one-time index).
    sig_info = challenge_response.signature_check(response, ledger.path)

    evaluation = {
        "event": _CHALLENGE_EVENT,
        "stage": "R-challenge",
        "topology": "challenge-response-same-operator",
        "status": status,
        "task_class": _TASK_CLASS,
        # SINGULAR task_id on purpose — this IS a genuine verification event for
        # the task and should join its molecule history (frozen generations stay
        # valid via generation-locked rebuilds; asserted in the self-test).
        "task_id": challenge["task_id"],
        "challenge_schema": challenge["schema"],
        "challenge_id": challenge["challenge_id"],
        "nonce": challenge["nonce"],
        "issued_for": challenge["issued_for"],
        "ledger_tip_at_issue": dict(challenge["ledger_tip_at_issue"]),
        "verifier_id": response["verifier_id"],
        "output_hash": response["output_hash"],
        "response_hash": response["response_hash"],
        "signed": sig_info["signed"],
        "coordinator_reconfirmed": {
            "verdict": verdict,
            "reason_count": len(reasons),
            "first_failure_reason": reasons[0] if reasons else None,
        },
        "operator_relationship": "same-operator",
        "limitation_note": note,
        "zero_value": True,
        "no_token": True,
        "anchored_at": time.time(),
    }
    if sig_info["signed"]:
        evaluation["signer_actor_id"] = sig_info["signer_actor_id"]
        evaluation["key_index"] = sig_info["key_index"]
        evaluation["signature_valid"] = sig_info["signature_valid"]
        evaluation["key_root_ledger_index"] = sig_info["key_root_ledger_index"]
    if drill:
        evaluation["drill"] = True
    entry = ledger.append(evaluation)
    return {"evaluation": evaluation, "ledger_entry": entry}


# ============================== SELF-TEST ====================================
# NOTE: the following is a LOCAL TEST HARNESS (single-host simulation), NOT the product.
# In real use the submission comes from a SEPARATE machine/person via verifier_cli.py.

def _rmtree(path):
    """Recursively remove a directory tree using os only (no shutil)."""
    if not os.path.exists(path):
        return
    for root, dirs, files in os.walk(path, topdown=False):
        for f in files:
            os.remove(os.path.join(root, f))
        for d in dirs:
            os.rmdir(os.path.join(root, d))
    os.rmdir(path)


def _copyfile(src, dst):
    """Copy a file using os only (no shutil) — for making independent temp ledger copies."""
    with open(src, "rb") as f:
        data = f.read()
    with open(dst, "wb") as g:
        g.write(data)


def _selftest() -> int:
    # Record whether a REAL ledger already exists BEFORE the self-test, so the stray-ledger
    # guard flags only a ledger this self-test ACCIDENTALLY creates — not a legitimate
    # pre-existing real ledger (the self-test itself uses temp paths only).
    _real_ledger_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "ledger_data.jsonl"
    )
    _ledger_existed_before = os.path.exists(_real_ledger_path)

    tmp = os.path.join(
        os.environ.get("TMPDIR", "/tmp"), f"r3_selftest_{os.getpid()}_{int(time.time())}"
    )
    os.makedirs(tmp, exist_ok=False)

    checks = []
    sample_submission = None
    sample_evaluation = None
    sample_same_evaluation = None
    signed_attestation = None
    agent_confirmed_payload = None
    try:
        ledger_path = os.path.join(tmp, "ledger.jsonl")
        ledger = Ledger(ledger_path)
        TASK = "task-0002"

        # --- LOCAL TEST HARNESS (single-host simulation; NOT the product) ---------------
        # We generate the external submissions on THIS host via the same code path
        # verifier_cli uses. In production these come from a separate machine/person.

        # (1) VALID submission (correct hash) -> externally-verified -> anchored
        valid_sub = verifier_cli.build_submission(TASK, "verifier-alpha (external, simulated)")
        res_valid = evaluate_submission(valid_sub, ledger)
        sample_submission, sample_evaluation = valid_sub, res_valid["evaluation"]
        checks.append((
            "EXTERNALLY-VERIFIED (correct hash -> anchored)",
            res_valid["evaluation"]["status"] == "externally-verified"
            and res_valid["evaluation"]["match"] is True
            and res_valid["ledger_entry"] is not None,
            res_valid["evaluation"]["status"],
        ))

        # (1b) the anchored external result carries the honest task_class label
        checks.append((
            "TASK_CLASS LABEL on anchored result (illustrative-demo)",
            res_valid["evaluation"].get("task_class") == "illustrative-demo",
            f"task_class={res_valid['evaluation'].get('task_class')!r}",
        ))

        # (1c) HONEST SAME-MACHINE self-recompute -> locally-verified (NOT 'external') -> anchored.
        # No reader can mistake this for cross-machine verification: topology, event, and status
        # all avoid 'external', and operator_relationship is recorded explicitly.
        same_machine_sub = verifier_cli.build_submission(
            TASK, "spark-local-same-operator (simulated)",
            topology="same-machine-self-recompute",
        )
        res_same = evaluate_submission(same_machine_sub, ledger)
        sample_same_evaluation = res_same["evaluation"]
        ev_s = res_same["evaluation"]
        checks.append((
            "SAME-MACHINE SELF-RECOMPUTE (locally-verified; event/topology never 'external')",
            ev_s["status"] == "locally-verified"
            and ev_s["event"] == "self_recompute_result"
            and ev_s["topology"] == "same-machine-self-recompute"
            and ev_s.get("task_class") == "illustrative-demo"
            and ev_s.get("operator_relationship") == "same-operator"
            and "external" not in ev_s["status"]
            and "external" not in ev_s["event"]
            and res_same["ledger_entry"] is not None,
            f"{ev_s['status']} / {ev_s['event']} / {ev_s['topology']}",
        ))

        # (2) WRONG-hash submission -> external-mismatch -> anchored
        wrong_sub = verifier_cli.build_submission(TASK, "verifier-beta (external, simulated)")
        wrong_sub["output_hash"] = "0" * 64
        res_wrong = evaluate_submission(wrong_sub, ledger)
        checks.append((
            "EXTERNAL-MISMATCH (wrong hash -> anchored)",
            res_wrong["evaluation"]["status"] == "external-mismatch"
            and res_wrong["evaluation"]["match"] is False
            and res_wrong["ledger_entry"] is not None,
            res_wrong["evaluation"]["status"],
        ))

        # (3) MALFORMED submission -> rejected -> NOT anchored
        bad_sub = verifier_cli.build_submission(TASK, "verifier-gamma (external, simulated)")
        del bad_sub["output_hash"]
        res_bad = evaluate_submission(bad_sub, ledger)
        checks.append((
            "REJECTED (malformed -> NOT anchored)",
            res_bad["evaluation"]["status"] == "rejected"
            and res_bad["ledger_entry"] is None,
            f"{res_bad['evaluation']['status']} ({res_bad['evaluation']['reason']})",
        ))

        # (4) ledger chain still verifies after anchoring the two real outcomes
        chain_ok, chain_reason = ledger.verify_chain()
        checks.append(("LEDGER CHAIN VERIFY AFTER ANCHORING", chain_ok is True, chain_reason))

        # (5) OPTIONAL R2 software-key MAC, honestly labeled (uses a TEMP key)
        key_path = os.path.join(tmp, "verifier_key.secret")
        signed_sub = verifier_cli.build_submission(
            TASK, "verifier-delta (external, simulated)", key_path=key_path
        )
        signed_attestation = signed_sub.get("r2_attestation")
        checks.append((
            "OPTIONAL R2 SOFTWARE-KEY MAC (honestly labeled)",
            isinstance(signed_attestation, dict)
            and signed_attestation.get("root_of_trust") == "software-key",
            "root_of_trust=" + str(signed_attestation.get("root_of_trust") if signed_attestation else None),
        ))

        # --- AGENT-RESULT mode coverage (mechanical, no-LLM agent attestation) -----------
        # Build a base temp ledger (genesis + a task-0002 verification at index 1), then a
        # temp published-snapshot + temp anchor exported from it, then sample agent results.
        # All TEMP paths — never the real ledger/snapshot/anchor.
        agent_base = os.path.join(tmp, "agent_ledger.jsonl")
        abl = Ledger(agent_base)
        abl.append({"event": "ledger_genesis", "stage": "R-protocol",
                    "note": "temp chain-start (self-test)", "zero_value": True, "no_token": True})
        evaluate_submission(
            verifier_cli.build_submission(TASK, "jiyu-laptop-same-operator (simulated)"), abl
        )  # index 1: externally-verified task-0002
        agent_snap = os.path.join(tmp, "agent_published.json")
        audit.export_snapshot(agent_base, agent_snap)
        agent_anchor = os.path.join(tmp, "agent_anchor.json")
        audit.write_anchor(agent_base, agent_anchor)
        real_task_hash = verifier_cli.load_task(TASK).output_hash(
            verifier_cli.load_task(TASK).compute()
        )
        base_tip = abl.read_all()[-1]["hash"]  # snapshot/anchor tip the agent must agree with

        def _make_agent_result(recomputed_hash):
            """Sample in jiyu's REAL nested schema (task_reproductions array + tip fields)."""
            return {
                "event": "agent_verifier_attestation", "verdict": "verified",
                "chain_verified": True, "tip_matches_anchor": True,
                "verifier_id": "jiyu-agent-same-operator", "verifier_kind": "autonomous-agent",
                "snapshot_tip_hash": base_tip, "snapshot_tip_index": 1,
                "anchor_tip_hash": base_tip, "anchor_tip_index": 1,
                "task_reproductions": [{
                    "task_id": TASK, "recomputed_hash": recomputed_hash,
                    "recorded_hash": real_task_hash, "match": recomputed_hash == real_task_hash,
                    "ledger_entry_index": 1,
                }],
                "timestamp": 1.0, "zero_value": True, "no_token": True,
            }

        # (6) VALID agent result (real schema, correct hash) -> Spark re-derives -> confirmed
        ledger_a = os.path.join(tmp, "agent_A.jsonl")
        _copyfile(agent_base, ledger_a)
        out_a = anchor_agent_result(
            _make_agent_result(real_task_hash), Ledger(ledger_a), agent_snap, agent_anchor, "same-operator"
        )
        agent_confirmed_payload = out_a["evaluation"]
        sr_a = out_a["evaluation"].get("spark_reconfirmed", {})
        checks.append((
            "AGENT-RESULT CONFIRMED (real schema; Spark re-derives all claims -> anchored)",
            out_a["evaluation"]["status"] == "agent-result-confirmed"
            and out_a["ledger_entry"] is not None
            and sr_a.get("chain_verified") is True
            and sr_a.get("tip_matches_anchor") is True
            and sr_a.get("agent_tips_agree") is True
            and sr_a.get("task_reproduced") is True
            and sr_a["per_task"][0]["reproduced"] is True,
            out_a["evaluation"]["status"],
        ))

        # (6b) the anchored agent-result carries the honest task_class label
        checks.append((
            "AGENT-RESULT task_class label (illustrative-demo)",
            out_a["evaluation"].get("task_class") == "illustrative-demo",
            f"task_class={out_a['evaluation'].get('task_class')!r}",
        ))

        # (7) WRONG recomputed_hash in task_reproductions -> Spark reproduces real hash, disagrees
        ledger_b = os.path.join(tmp, "agent_B.jsonl")
        _copyfile(agent_base, ledger_b)
        out_b = anchor_agent_result(
            _make_agent_result("1" * 64), Ledger(ledger_b), agent_snap, agent_anchor, "same-operator"
        )
        checks.append((
            "AGENT-RESULT MISMATCH (wrong recomputed_hash -> anchored audit event)",
            out_b["evaluation"]["status"] == "agent-result-mismatch"
            and out_b["ledger_entry"] is not None
            and out_b["evaluation"]["spark_reconfirmed"]["task_reproduced"] is False
            and out_b["evaluation"]["spark_reconfirmed"]["per_task"][0]["matches_agent_recomputed"] is False,
            out_b["evaluation"]["status"],
        ))

        # (8) MALFORMED agent result (no task_reproductions) -> rejected -> NOT anchored
        ledger_c = os.path.join(tmp, "agent_C.jsonl")
        _copyfile(agent_base, ledger_c)
        bad_agent = _make_agent_result(real_task_hash)
        del bad_agent["task_reproductions"]
        out_c = anchor_agent_result(bad_agent, Ledger(ledger_c), agent_snap, agent_anchor, "same-operator")
        checks.append((
            "AGENT-RESULT REJECTED (malformed -> NOT anchored)",
            out_c["evaluation"]["status"] == "rejected" and out_c["ledger_entry"] is None,
            f"{out_c['evaluation']['status']} ({out_c['evaluation'].get('reason')})",
        ))

        # --- MOLECULE-CATALOG mode coverage (rebuild-and-compare anchoring) --------------
        # Uses a copy of the main temp ledger (which holds task-0002 evaluations). All
        # TEMP paths — the real ledger is never touched.
        cat_ledger_path = os.path.join(tmp, "catalog_ledger.jsonl")
        _copyfile(ledger_path, cat_ledger_path)
        cat = work_molecule.build_catalog(ledger_path=cat_ledger_path, task_ids=[TASK])
        out_cat = anchor_molecule_catalog(cat, Ledger(cat_ledger_path))
        ev_cat = out_cat["evaluation"]
        cat_chain_ok, _cat_reason = Ledger(cat_ledger_path).verify_chain()

        # (9) valid catalog -> coordinator rebuild confirms -> anchored; and the payload
        # must NOT carry "task_ids"/"task_id" (the key-name choice that keeps this record
        # invisible to work_molecule's citation scanner).
        checks.append((
            "MOLECULE-CATALOG CONFIRMED (rebuilt from ledger -> anchored)",
            ev_cat["status"] == "molecule-catalog-confirmed"
            and out_cat["ledger_entry"] is not None
            and ev_cat["coordinator_reconfirmed"]["catalog_hash_matches"] is True
            and ev_cat["coordinator_reconfirmed"]["entries_match"] is True
            and cat_chain_ok is True,
            ev_cat["status"],
        ))
        checks.append((
            "CATALOG RECORD carries no task_ids/task_id key (scanner-invisible)",
            "task_ids" not in ev_cat and "task_id" not in ev_cat
            and "catalog_entries" in ev_cat,
            f"keys={sorted(k for k in ev_cat if k.startswith('task'))}",
        ))

        # (9b) WMID stability across the anchor append: rebuilding the catalog from the
        # now-grown ledger must reproduce the SAME catalog_hash (work-molecule/0.2's
        # entry-level anchoring + the catalog_entries key-name choice, both proven here).
        cat_after = work_molecule.build_catalog(ledger_path=cat_ledger_path, task_ids=[TASK])
        checks.append((
            "CATALOG REBUILD AFTER ANCHOR unchanged (growth-stable WMIDs)",
            cat_after["catalog_hash"] == cat["catalog_hash"]
            and cat_after["entries"] == cat["entries"],
            f"catalog_hash={cat_after['catalog_hash'][:16]}..",
        ))

        # (10) internally-inconsistent catalog_hash -> rejected -> NOT anchored
        bad_cat = dict(cat)
        bad_cat["catalog_hash"] = "0" * 64
        cat_ledger_b = os.path.join(tmp, "catalog_ledger_b.jsonl")
        _copyfile(ledger_path, cat_ledger_b)
        out_badcat = anchor_molecule_catalog(bad_cat, Ledger(cat_ledger_b))
        checks.append((
            "CATALOG REJECTED (inconsistent catalog_hash -> NOT anchored)",
            out_badcat["evaluation"]["status"] == "rejected"
            and out_badcat["ledger_entry"] is None,
            f"{out_badcat['evaluation']['status']} ({out_badcat['evaluation'].get('reason')})",
        ))

        # (11) internally consistent but WRONG work_id -> coordinator rebuild disagrees ->
        # 'molecule-catalog-mismatch', anchored (a real audit event)
        tampered_cat = json.loads(json.dumps(cat))
        tampered_cat["entries"][0]["work_id"] = "1" * 64
        tampered_cat["catalog_hash"] = work_molecule.compute_catalog_hash(tampered_cat)
        cat_ledger_c = os.path.join(tmp, "catalog_ledger_c.jsonl")
        _copyfile(ledger_path, cat_ledger_c)
        out_tcat = anchor_molecule_catalog(tampered_cat, Ledger(cat_ledger_c))
        checks.append((
            "CATALOG MISMATCH (wrong work_id -> rebuild disagrees -> anchored audit event)",
            out_tcat["evaluation"]["status"] == "molecule-catalog-mismatch"
            and out_tcat["ledger_entry"] is not None
            and out_tcat["evaluation"]["coordinator_reconfirmed"]["entries_match"] is False,
            out_tcat["evaluation"]["status"],
        ))

        # --- ACI-BASELINE mode coverage (recompute-and-compare anchoring) ----------------
        # The recompute path rebuilds a molecule for EVERY known task, so the fixture
        # ledger must reference all of them: one same-machine evaluation per task.
        # All TEMP paths — the real ledger is never touched.
        aci_ledger_path = os.path.join(tmp, "aci_ledger.jsonl")
        aled = Ledger(aci_ledger_path)
        aled.append({"event": "ledger_genesis", "stage": "R-protocol",
                     "note": "temp chain-start (self-test)", "zero_value": True,
                     "no_token": True})
        for tid in sorted(verifier_cli.TASK_MODULES):
            evaluate_submission(
                verifier_cli.build_submission(
                    tid, "spark-local-same-operator (simulated)",
                    topology="same-machine-self-recompute"),
                aled,
            )
        aci_report = agent_concentration.compute_report(
            agent_concentration.build_paths(ledger_path=aci_ledger_path)
        )

        # (12) valid report -> coordinator recompute confirms -> anchored; the payload
        # must carry aggregates only and NO task_id/task_ids keys (scanner-invisible),
        # and re-measuring from the grown ledger must reproduce the same report_hash.
        aci_ledger_a = os.path.join(tmp, "aci_ledger_a.jsonl")
        _copyfile(aci_ledger_path, aci_ledger_a)
        out_aci = anchor_aci_report(aci_report, Ledger(aci_ledger_a))
        ev_aci = out_aci["evaluation"]
        aci_chain_ok, _aci_reason = Ledger(aci_ledger_a).verify_chain()
        checks.append((
            "ACI-BASELINE CONFIRMED (full recompute from ledger -> anchored)",
            ev_aci["status"] == "aci-baseline-confirmed"
            and out_aci["ledger_entry"] is not None
            and ev_aci["coordinator_reconfirmed"]["report_hash_matches"] is True
            and aci_chain_ok is True,
            ev_aci["status"],
        ))
        checks.append((
            "ACI RECORD aggregates only, no task_id-like keys (scanner-invisible)",
            "task_id" not in ev_aci and "task_ids" not in ev_aci
            and "paths" not in ev_aci and "missing_metadata_flags" not in ev_aci,
            f"task-keys={sorted(k for k in ev_aci if 'task_id' in k)}",
        ))
        aci_after = agent_concentration.compute_report(
            agent_concentration.build_paths(ledger_path=aci_ledger_a)
        )
        checks.append((
            "ACI REMEASURE AFTER ANCHOR unchanged (record invisible to the measurement)",
            aci_after["report_hash"] == aci_report["report_hash"],
            f"report_hash={aci_after['report_hash'][:16]}..",
        ))

        # (13) internally-inconsistent report_hash -> rejected -> NOT anchored
        bad_aci = dict(aci_report)
        bad_aci["report_hash"] = "0" * 64
        aci_ledger_b = os.path.join(tmp, "aci_ledger_b.jsonl")
        _copyfile(aci_ledger_path, aci_ledger_b)
        out_badaci = anchor_aci_report(bad_aci, Ledger(aci_ledger_b))
        checks.append((
            "ACI REJECTED (inconsistent report_hash -> NOT anchored)",
            out_badaci["evaluation"]["status"] == "rejected"
            and out_badaci["ledger_entry"] is None,
            f"{out_badaci['evaluation']['status']} ({out_badaci['evaluation'].get('reason')})",
        ))

        # (14) internally consistent but WRONG measurement -> coordinator recompute
        # disagrees -> 'aci-baseline-mismatch', anchored (a real audit event)
        tampered_aci = json.loads(json.dumps(aci_report))
        tampered_aci["pairwise_aci"] = 0.0
        tampered_aci["report_hash"] = agent_concentration.compute_report_hash(tampered_aci)
        aci_ledger_c = os.path.join(tmp, "aci_ledger_c.jsonl")
        _copyfile(aci_ledger_path, aci_ledger_c)
        out_taci = anchor_aci_report(tampered_aci, Ledger(aci_ledger_c))
        checks.append((
            "ACI MISMATCH (wrong measurement -> recompute disagrees -> anchored audit event)",
            out_taci["evaluation"]["status"] == "aci-baseline-mismatch"
            and out_taci["ledger_entry"] is not None
            and out_taci["evaluation"]["coordinator_reconfirmed"]["report_hash_matches"] is False,
            out_taci["evaluation"]["status"],
        ))

        # --- ECONOMY-SUMMARY mode coverage (re-run-and-compare anchoring) ----------------
        # The economy simulation is ledger-independent (it runs tasks + the faucet), so
        # one in-memory run serves as the "submitted" log. All TEMP ledger paths.
        econ_log = economy_demo.simulate_all()

        # (15) valid log -> coordinator full re-run confirms -> anchored; aggregates
        # only, and NO task_id/task_ids keys (scanner-invisible).
        econ_ledger_a = os.path.join(tmp, "econ_ledger_a.jsonl")
        _copyfile(ledger_path, econ_ledger_a)
        out_econ = anchor_economy_summary(econ_log, Ledger(econ_ledger_a))
        ev_econ = out_econ["evaluation"]
        econ_chain_ok, _econ_reason = Ledger(econ_ledger_a).verify_chain()
        checks.append((
            "ECONOMY-SUMMARY CONFIRMED (full simulation re-run -> anchored)",
            ev_econ["status"] == "economy-demo-confirmed"
            and out_econ["ledger_entry"] is not None
            and ev_econ["coordinator_reconfirmed"]["log_hash_matches"] is True
            and econ_chain_ok is True,
            ev_econ["status"],
        ))
        checks.append((
            "ECONOMY RECORD aggregates only, no task_id-like keys (scanner-invisible)",
            "task_id" not in ev_econ and "task_ids" not in ev_econ
            and "per_day" not in ev_econ
            and ev_econ["planned_drill_rejections"] == ev_econ["rejected_count"] == 1,
            f"task-keys={sorted(k for k in ev_econ if 'task_id' in k)}",
        ))

        # (16) internally-inconsistent economy_log_hash -> rejected -> NOT anchored
        bad_econ = dict(econ_log)
        bad_econ["economy_log_hash"] = "0" * 64
        econ_ledger_b = os.path.join(tmp, "econ_ledger_b.jsonl")
        _copyfile(ledger_path, econ_ledger_b)
        out_badecon = anchor_economy_summary(bad_econ, Ledger(econ_ledger_b))
        checks.append((
            "ECONOMY REJECTED (inconsistent log hash -> NOT anchored)",
            out_badecon["evaluation"]["status"] == "rejected"
            and out_badecon["ledger_entry"] is None,
            f"{out_badecon['evaluation']['status']} "
            f"({out_badecon['evaluation'].get('reason')})",
        ))

        # (17) internally consistent but WRONG simulation content -> coordinator re-run
        # disagrees -> 'economy-demo-mismatch', anchored (a real audit event)
        tampered_econ = json.loads(json.dumps(econ_log))
        tampered_econ["summary"]["final_balance"] = 9999
        tampered_econ["economy_log_hash"] = economy_demo.compute_log_hash(tampered_econ)
        econ_ledger_c = os.path.join(tmp, "econ_ledger_c.jsonl")
        _copyfile(ledger_path, econ_ledger_c)
        out_tecon = anchor_economy_summary(tampered_econ, Ledger(econ_ledger_c))
        checks.append((
            "ECONOMY MISMATCH (wrong content -> re-run disagrees -> anchored audit event)",
            out_tecon["evaluation"]["status"] == "economy-demo-mismatch"
            and out_tecon["ledger_entry"] is not None
            and out_tecon["evaluation"]["coordinator_reconfirmed"]["log_hash_matches"] is False,
            out_tecon["evaluation"]["status"],
        ))

        # --- METERING-EVIDENCE mode coverage (plausibility-reconfirm anchoring) ----------
        # Uses copies of the ACI fixture ledger (genesis + one evaluation per task, so
        # every task has a canonical ledger-recorded hash). All TEMP paths.
        met_report = task_metering.build_report()

        # (19) valid report -> coordinator re-meters, confirms plausibility -> anchored;
        # and the 0.2 catalog rebuild is UNCHANGED by the anchor (append-only: new
        # evidence must never move an old WMID).
        met_ledger_a = os.path.join(tmp, "met_ledger_a.jsonl")
        _copyfile(aci_ledger_path, met_ledger_a)
        cat02_before = work_molecule.build_catalog(
            ledger_path=met_ledger_a, task_ids=[TASK],
            schema_version=work_molecule.SCHEMA_VERSION_02)
        out_met = anchor_metering_report(met_report, Ledger(met_ledger_a))
        ev_met = out_met["evaluation"]
        met_chain_ok, _met_reason = Ledger(met_ledger_a).verify_chain()
        cr_met = ev_met.get("coordinator_reconfirmed", {})
        checks.append((
            "METERING CONFIRMED (re-metered plausibility, NOT byte-reconfirm -> anchored)",
            ev_met["status"] == "metering-evidence-confirmed"
            and out_met["ledger_entry"] is not None
            and cr_met.get("remetered_output_hashes_match_ledger") is True
            and cr_met.get("timings_within_sanity_band") is True
            and met_chain_ok is True,
            ev_met["status"],
        ))
        cat02_after_met = work_molecule.build_catalog(
            ledger_path=met_ledger_a, task_ids=[TASK],
            schema_version=work_molecule.SCHEMA_VERSION_02)
        checks.append((
            "METERING RECORD scanner-invisible (no task keys; 0.2 WMIDs unmoved)",
            "task_id" not in ev_met and "task_ids" not in ev_met
            and "per_task" not in ev_met
            and ev_met["task_count"] == len(met_report["per_task"])
            and cat02_after_met["catalog_hash"] == cat02_before["catalog_hash"],
            f"task-keys={sorted(k for k in ev_met if 'task_id' in k)}",
        ))

        # (20) honest labels on the anchored record: times measured, energy ESTIMATED
        checks.append((
            "METERING RECORD honest labels (wall/cpu measured, energy estimated)",
            ev_met.get("labels") == {"wall": "measured", "cpu": "measured",
                                     "energy": "estimated"}
            and ev_met.get("assumed_cpu_power_w") == task_metering.ASSUMED_CPU_POWER_W
            and ev_met.get("power_method") == task_metering.POWER_METHOD,
            f"labels={ev_met.get('labels')}",
        ))

        # (21) internally consistent report whose output_hash does NOT match the
        # canonical ledger-recorded hash -> rejected, NOT anchored (the report does
        # not describe the canonical work)
        bad_hash_report = json.loads(json.dumps(met_report))
        bad_hash_report["per_task"][0]["output_hash"] = "2" * 64
        bad_hash_report["report_hash"] = task_metering.compute_report_hash(bad_hash_report)
        met_ledger_b = os.path.join(tmp, "met_ledger_b.jsonl")
        _copyfile(aci_ledger_path, met_ledger_b)
        out_badhash = anchor_metering_report(bad_hash_report, Ledger(met_ledger_b))
        checks.append((
            "METERING REJECTED (output_hash not the canonical one -> NOT anchored)",
            out_badhash["evaluation"]["status"] == "rejected"
            and out_badhash["ledger_entry"] is None,
            f"{out_badhash['evaluation']['status']} "
            f"({out_badhash['evaluation'].get('reason')})",
        ))

        # (22) internally-inconsistent report_hash -> rejected -> NOT anchored
        bad_met = dict(met_report)
        bad_met["report_hash"] = "0" * 64
        out_badmet = anchor_metering_report(bad_met, Ledger(met_ledger_b))
        checks.append((
            "METERING REJECTED (inconsistent report_hash -> NOT anchored)",
            out_badmet["evaluation"]["status"] == "rejected"
            and out_badmet["ledger_entry"] is None,
            f"{out_badmet['evaluation']['status']} "
            f"({out_badmet['evaluation'].get('reason')})",
        ))

        # (23) missing honesty labels -> rejected -> NOT anchored (labels mandatory)
        nolabel_report = json.loads(json.dumps(met_report))
        del nolabel_report["per_task"][0]["labels"]
        nolabel_report["report_hash"] = task_metering.compute_report_hash(nolabel_report)
        out_nolabel = anchor_metering_report(nolabel_report, Ledger(met_ledger_b))
        checks.append((
            "METERING REJECTED (missing labels -> NOT anchored)",
            out_nolabel["evaluation"]["status"] == "rejected"
            and out_nolabel["ledger_entry"] is None,
            f"{out_nolabel['evaluation']['status']} "
            f"({out_nolabel['evaluation'].get('reason')})",
        ))

        # (24) SECOND-GENERATION catalog: with the metering anchor on the ledger, the
        # 0.3 catalog absorbs the evidence and anchors through the SAME path, its
        # record naming its own generation — while the 0.2 generation stays valid.
        cat03 = work_molecule.build_catalog(
            ledger_path=met_ledger_a, task_ids=sorted(verifier_cli.TASK_MODULES))
        out_cat03 = anchor_molecule_catalog(cat03, Ledger(met_ledger_a))
        ev_cat03 = out_cat03["evaluation"]
        checks.append((
            "CATALOG GEN-2 CONFIRMED (0.3 rebuild -> anchored, generation on record)",
            ev_cat03["status"] == "molecule-catalog-confirmed"
            and out_cat03["ledger_entry"] is not None
            and ev_cat03["molecule_schema"] == work_molecule.SCHEMA_VERSION_03
            and ev_cat03["catalog_schema"] == work_molecule.CATALOG_SCHEMA_VERSION_02
            and ev_cat03["coordinator_reconfirmed"]["catalog_hash_matches"] is True,
            f"{ev_cat03['status']} ({ev_cat03.get('molecule_schema')})",
        ))

        # --- CUT-CERTIFICATE mode coverage (anchor-time full proof) ----------------------
        # Reuses met_ledger_a (13 tasks + metering + gen-2 catalog anchors), so the
        # cut summarizes the same 0.3 generation the catalog anchored. All TEMP paths.
        cut_cert = cut_certificate.build_cut(sorted(verifier_cli.TASK_MODULES),
                                             ledger_path=met_ledger_a)

        # (25) valid certificate -> coordinator FULL verification (the expensive
        # proof, run exactly once, at anchoring) -> anchored
        out_cut = anchor_cut_certificate(cut_cert, Ledger(met_ledger_a))
        ev_cut = out_cut["evaluation"]
        cut_chain_ok, _cut_reason = Ledger(met_ledger_a).verify_chain()
        checks.append((
            "CUT-CERT CONFIRMED (coordinator full proof at anchor time -> anchored)",
            ev_cut["status"] == "cut-certificate-confirmed"
            and out_cut["ledger_entry"] is not None
            and ev_cut["coordinator_reconfirmed"]["full_verification_passed"] is True
            and ev_cut["coordinator_reconfirmed"]["rebuilt_interior_count"] == 13
            and cut_chain_ok is True,
            ev_cut["status"],
        ))

        # (26) counts only, scanner-invisible; and the CHEAP path now works: one
        # anchored-hash lookup + a single-molecule retrievability probe
        accepted, cost_note = cut_certificate.accept_by_anchor(
            cut_cert, ledger_path=met_ledger_a)
        checks.append((
            "CUT RECORD counts only, scanner-invisible; cheap acceptance works",
            "task_id" not in ev_cut and "task_ids" not in ev_cut
            and "interior" not in ev_cut and "root_work_ids" not in ev_cut
            and "boundary_input_ids" not in ev_cut
            and ev_cut["interior_count"] == 13 and ev_cut["boundary_count"] == 0
            and accepted and "rebuilt 1 of 13" in cost_note,
            f"task-keys={sorted(k for k in ev_cut if 'task_id' in k)}",
        ))

        # (27) internally-inconsistent aggregate_hash -> rejected -> NOT anchored
        bad_agg = json.loads(json.dumps(cut_cert))
        bad_agg["aggregate_hash"] = "0" * 64
        bad_agg["certificate_hash"] = cut_certificate.compute_certificate_hash(bad_agg)
        cut_ledger_b = os.path.join(tmp, "cut_ledger_b.jsonl")
        _copyfile(aci_ledger_path, cut_ledger_b)
        out_badagg = anchor_cut_certificate(bad_agg, Ledger(cut_ledger_b))
        checks.append((
            "CUT REJECTED (aggregate_hash mismatch -> NOT anchored)",
            out_badagg["evaluation"]["status"] == "rejected"
            and out_badagg["ledger_entry"] is None,
            f"{out_badagg['evaluation']['status']} "
            f"({out_badagg['evaluation'].get('reason')})",
        ))

        # (28) malformed certificate_hash -> rejected -> NOT anchored; and an
        # internally CONSISTENT but wrong WMID -> full proof disagrees ->
        # 'cut-certificate-mismatch', anchored (a real audit event)
        bad_cut = dict(cut_cert)
        bad_cut["certificate_hash"] = "0" * 64
        out_badcut = anchor_cut_certificate(bad_cut, Ledger(cut_ledger_b))
        tampered_cut = json.loads(json.dumps(cut_cert))
        old_wid = tampered_cut["interior"][0]["work_id"]
        tampered_cut["interior"][0]["work_id"] = "1" * 64
        tampered_cut["root_work_ids"] = sorted(
            ("1" * 64 if r == old_wid else r) for r in tampered_cut["root_work_ids"])
        tampered_cut["aggregate_hash"] = cut_certificate.compute_aggregate_hash(
            tampered_cut["molecule_schema"], tampered_cut["interior"],
            tampered_cut["boundary_input_ids"])
        tampered_cut["certificate_hash"] = cut_certificate.compute_certificate_hash(
            tampered_cut)
        out_tcut = anchor_cut_certificate(tampered_cut, Ledger(cut_ledger_b))
        checks.append((
            "CUT REJECTED (malformed) + CUT MISMATCH (wrong WMID -> audit event)",
            out_badcut["evaluation"]["status"] == "rejected"
            and out_badcut["ledger_entry"] is None
            and out_tcut["evaluation"]["status"] == "cut-certificate-mismatch"
            and out_tcut["ledger_entry"] is not None
            and out_tcut["evaluation"]["coordinator_reconfirmed"]
                ["full_verification_passed"] is False,
            f"{out_badcut['evaluation']['status']} / {out_tcut['evaluation']['status']}",
        ))

        # --- TRUST-VECTOR-CATALOG mode coverage (rebuild-and-compare anchoring) ----------
        # Reuses met_ledger_a (13 tasks + metering + gen-2 catalog + cut anchors);
        # vectors there use aggregate-citation C and no global baseline (no ACI
        # anchor on the fixture) — both honest, both deterministic. All TEMP paths.
        tv_cat = trust_vector.build_tv_catalog(ledger_path=met_ledger_a)

        # (29) valid catalog -> coordinator rebuilds all 13 vectors -> anchored;
        # counts only + the no-scalar affirmation; scanner-invisible.
        out_tv = anchor_trust_vector_catalog(tv_cat, Ledger(met_ledger_a))
        ev_tv = out_tv["evaluation"]
        tv_chain_ok, _tv_reason = Ledger(met_ledger_a).verify_chain()
        checks.append((
            "TRUST-VECTOR CATALOG CONFIRMED (13 vectors rebuilt -> anchored)",
            ev_tv["status"] == "trust-vector-catalog-confirmed"
            and out_tv["ledger_entry"] is not None
            and ev_tv["vector_count"] == 13
            and ev_tv["coordinator_reconfirmed"]["catalog_hash_matches"] is True
            and tv_chain_ok is True,
            ev_tv["status"],
        ))
        checks.append((
            "TV RECORD counts only + no-scalar affirmation (scanner-invisible)",
            "task_id" not in ev_tv and "task_ids" not in ev_tv
            and "vector_entries" not in ev_tv
            and ev_tv["no_combined_scalar"] == NO_SCALAR_AFFIRMATION,
            f"task-keys={sorted(k for k in ev_tv if 'task_id' in k)}",
        ))

        # (30) internally-inconsistent catalog_hash -> rejected -> NOT anchored
        bad_tv = dict(tv_cat)
        bad_tv["catalog_hash"] = "0" * 64
        tv_ledger_b = os.path.join(tmp, "tv_ledger_b.jsonl")
        _copyfile(aci_ledger_path, tv_ledger_b)
        out_badtv = anchor_trust_vector_catalog(bad_tv, Ledger(tv_ledger_b))
        checks.append((
            "TV REJECTED (inconsistent catalog_hash -> NOT anchored)",
            out_badtv["evaluation"]["status"] == "rejected"
            and out_badtv["ledger_entry"] is None,
            f"{out_badtv['evaluation']['status']} "
            f"({out_badtv['evaluation'].get('reason')})",
        ))

        # (31) internally consistent but WRONG tv_hash -> coordinator rebuild
        # disagrees -> 'trust-vector-catalog-mismatch', anchored (audit event)
        tampered_tv = json.loads(json.dumps(tv_cat))
        tampered_tv["vector_entries"][0]["tv_hash"] = "1" * 64
        tampered_tv["catalog_hash"] = trust_vector.compute_catalog_hash(tampered_tv)
        out_ttv = anchor_trust_vector_catalog(tampered_tv, Ledger(tv_ledger_b))
        checks.append((
            "TV MISMATCH (wrong tv_hash -> rebuild disagrees -> anchored audit event)",
            out_ttv["evaluation"]["status"] == "trust-vector-catalog-mismatch"
            and out_ttv["ledger_entry"] is not None
            and out_ttv["evaluation"]["coordinator_reconfirmed"]["entries_match"] is False,
            out_ttv["evaluation"]["status"],
        ))

        # --- CHALLENGE-RESPONSE mode coverage (verify-and-anchor + copy attack) ----------
        # Fresh copy of the ACI fixture ledger (genesis + 13 task evaluations).
        ch_ledger = os.path.join(tmp, "challenge_ledger.jsonl")
        _copyfile(aci_ledger_path, ch_ledger)
        pre_tip = Ledger(ch_ledger).read_all()[-1]["index"]
        cat02_frozen_before = work_molecule.build_catalog(
            ledger_path=ch_ledger, task_ids=[TASK],
            schema_version=work_molecule.SCHEMA_VERSION_02, as_of_index=pre_tip)

        # (32) honest round anchored as challenge-verified (task_id joins history)
        ch_c = challenge_response.issue_challenge(TASK, "selftest-agent",
                                                  ledger_path=ch_ledger)
        ch_resp = challenge_response.respond(ch_c, ledger_path=ch_ledger)
        out_chv = anchor_challenge_result(ch_c, ch_resp, Ledger(ch_ledger))
        ev_chv = out_chv["evaluation"]
        ch_chain_ok, _ch_reason = Ledger(ch_ledger).verify_chain()
        checks.append((
            "CHALLENGE VERIFIED (full recompute -> anchored; task_id joins history)",
            ev_chv["status"] == "challenge-verified"
            and out_chv["ledger_entry"] is not None
            and ev_chv["coordinator_reconfirmed"]["verdict"] is True
            and ev_chv["task_id"] == TASK
            and ch_chain_ok is True,
            ev_chv["status"],
        ))

        # (33) the COPY ATTACK anchored as a failed audit event (drill-labeled):
        # a response forged from the PUBLIC output_hash alone must be rejected by
        # the possession check and anchored as 'challenge-failed'.
        ch_c2 = challenge_response.issue_challenge(TASK, "selftest-agent",
                                                   ledger_path=ch_ledger)
        honest2 = challenge_response.respond(ch_c2, ledger_path=ch_ledger)
        forged = dict(honest2)
        forged["response_hash"] = hashlib.sha256(
            (ch_c2["nonce"] + ":" + honest2["output_hash"]).encode("utf-8")
        ).hexdigest()
        out_chf = anchor_challenge_result(ch_c2, forged, Ledger(ch_ledger),
                                          drill=True)
        ev_chf = out_chf["evaluation"]
        checks.append((
            "COPY ATTACK anchored as challenge-failed (drill-labeled audit event)",
            ev_chf["status"] == "challenge-failed"
            and out_chf["ledger_entry"] is not None
            and ev_chf.get("drill") is True
            and "possession" in str(
                ev_chf["coordinator_reconfirmed"]["first_failure_reason"])
            and "PLANNED COPY-ATTACK DRILL" in ev_chf["limitation_note"],
            ev_chf["status"],
        ))

        # (34) malformed challenge/response -> rejected -> NOT anchored
        bad_resp = dict(honest2)
        del bad_resp["response_hash"]
        out_badch = anchor_challenge_result(ch_c2, bad_resp, Ledger(ch_ledger))
        checks.append((
            "CHALLENGE REJECTED (malformed response -> NOT anchored)",
            out_badch["evaluation"]["status"] == "rejected"
            and out_badch["ledger_entry"] is None,
            f"{out_badch['evaluation']['status']} "
            f"({out_badch['evaluation'].get('reason')})",
        ))

        # (35) FROZEN vs EVOLVING after the challenge anchors: the generation-locked
        # 0.2 rebuild is UNCHANGED (frozen generations stay valid forever), while
        # the unbounded rebuild LEGITIMATELY changes (the challenge records join the
        # task's molecule as challenge_events — new genuine evidence, new WMID).
        cat02_frozen_after = work_molecule.build_catalog(
            ledger_path=ch_ledger, task_ids=[TASK],
            schema_version=work_molecule.SCHEMA_VERSION_02, as_of_index=pre_tip)
        cat02_unbounded_after = work_molecule.build_catalog(
            ledger_path=ch_ledger, task_ids=[TASK],
            schema_version=work_molecule.SCHEMA_VERSION_02)
        mol_after = work_molecule.build_molecule(TASK, ledger_path=ch_ledger)
        checks.append((
            "FROZEN generation unchanged (as-of lock); unbounded rebuild absorbs "
            "the challenge events (designed evolution)",
            cat02_frozen_after["catalog_hash"] == cat02_frozen_before["catalog_hash"]
            and cat02_unbounded_after["catalog_hash"] !=
            cat02_frozen_before["catalog_hash"]
            and len(mol_after["challenge_events"]) == 2,
            f"challenge_events={len(mol_after['challenge_events'])}",
        ))

        # --- ACTOR-IDENTITY mode coverage (registration + signed challenges) -------------
        # Continues on ch_ledger (which already carries unsigned challenge
        # records — proving backward compatibility below). Small keychain (4
        # one-time keys) for speed; the scheme is size-independent.
        kc = actor_identity.generate_keychain("selftest-agent", key_count=4)
        decl = actor_identity.public_declaration(kc)
        out_reg = register_actor_key(decl, Ledger(ch_ledger))
        reg_idx = (out_reg["ledger_entry"] or {}).get("index")

        # (36) registration anchored; duplicate + private-material rejected
        out_dup = register_actor_key(decl, Ledger(ch_ledger))
        bad_decl = dict(decl)
        bad_decl["private_backup"] = {"keys": "oops"}
        out_priv = register_actor_key(bad_decl, Ledger(ch_ledger))
        checks.append((
            "ACTOR KEY REGISTERED; duplicate + private-material rejected",
            out_reg["evaluation"]["status"] == "actor-key-registered"
            and out_reg["ledger_entry"] is not None
            and out_dup["evaluation"]["status"] == "rejected"
            and "one active root" in out_dup["evaluation"]["reason"]
            and out_priv["evaluation"]["status"] == "rejected"
            and "PRIVATE" in out_priv["evaluation"]["reason"],
            f"root@idx {reg_idx}",
        ))

        # (37) SIGNED honest round -> verified, with the signature facts recorded
        ch_s = challenge_response.issue_challenge(TASK, "selftest-agent",
                                                  ledger_path=ch_ledger)
        resp_s = challenge_response.respond(ch_s, ledger_path=ch_ledger,
                                            keychain=kc, key_index=0)
        out_sv = anchor_challenge_result(ch_s, resp_s, Ledger(ch_ledger))
        ev_sv = out_sv["evaluation"]
        checks.append((
            "SIGNED CHALLENGE VERIFIED (signature facts on the record)",
            ev_sv["status"] == "challenge-verified"
            and ev_sv["signed"] is True
            and ev_sv["signature_valid"] is True
            and ev_sv["key_index"] == 0
            and ev_sv["signer_actor_id"] == "selftest-agent"
            and ev_sv["key_root_ledger_index"] == reg_idx,
            ev_sv["status"],
        ))

        # (38) UNSIGNED rounds still work (backward compatible)
        ch_u = challenge_response.issue_challenge(TASK, "selftest-agent",
                                                  ledger_path=ch_ledger)
        resp_u = challenge_response.respond(ch_u, ledger_path=ch_ledger)
        out_uv = anchor_challenge_result(ch_u, resp_u, Ledger(ch_ledger))
        checks.append((
            "UNSIGNED round still verifies (backward compatible; signed=false)",
            out_uv["evaluation"]["status"] == "challenge-verified"
            and out_uv["evaluation"]["signed"] is False,
            out_uv["evaluation"]["status"],
        ))

        # (39) KEY-REUSE attack: sign a NEW challenge with the already-anchored
        # index 0 (drill-force path) -> hard reject naming the OTS violation
        ch_r = challenge_response.issue_challenge(TASK, "selftest-agent",
                                                  ledger_path=ch_ledger)
        resp_r = challenge_response.respond(ch_r, ledger_path=ch_ledger,
                                            keychain=kc, key_index=0,
                                            force_reuse=True)
        out_rv = anchor_challenge_result(ch_r, resp_r, Ledger(ch_ledger),
                                         drill=True)
        ev_rv = out_rv["evaluation"]
        checks.append((
            "KEY-REUSE rejected (one-time discipline; drill-labeled audit event)",
            ev_rv["status"] == "challenge-failed"
            and ev_rv.get("drill") is True
            and "one-time key index reuse" in str(
                ev_rv["coordinator_reconfirmed"]["first_failure_reason"]),
            ev_rv["status"],
        ))

        # (40) FORGED signature: flip a revealed secret -> challenge-failed with
        # signature_valid False
        resp_f = challenge_response.respond(ch_r, ledger_path=ch_ledger,
                                            keychain=kc, key_index=1)
        resp_f["signature"]["revealed_secrets"][3] = "0" * 64
        out_fv = anchor_challenge_result(ch_r, resp_f, Ledger(ch_ledger))
        checks.append((
            "FORGED signature rejected (signature_valid False on the record)",
            out_fv["evaluation"]["status"] == "challenge-failed"
            and out_fv["evaluation"]["signature_valid"] is False,
            out_fv["evaluation"]["status"],
        ))

        # --- TWO-FLOW (treasury + Gate-3) mode coverage -----------------------------------
        # Fresh copy of the ACI fixture ledger + an anchored economy (funding
        # root) + an anchored 0.3 catalog (provenance standing). The lifecycle
        # rehearses EXACTLY the real exercise's ordering, including the honest
        # window arithmetic for the finalized bounty.
        g3_ledger = os.path.join(tmp, "gate3_ledger.jsonl")
        _copyfile(aci_ledger_path, g3_ledger)
        anchor_economy_summary(econ_log, Ledger(g3_ledger))
        g3_cat = work_molecule.build_catalog(ledger_path=g3_ledger,
                                             task_ids=["task-0002", "task-0008"])
        anchor_molecule_catalog(g3_cat, Ledger(g3_ledger))
        wid2 = g3_cat["entries"][0]["work_id"]
        wid8 = g3_cat["entries"][1]["work_id"]

        # (41) treasury constitution: confirmed; forged fees + malformed rejected
        t_state = metastar_treasury.collect_fees(
            metastar_treasury._new_state(), ledger_path=g3_ledger)
        out_tc = anchor_treasury_config(t_state, Ledger(g3_ledger))
        forged = json.loads(json.dumps(t_state))
        forged["total_fees_collected"] = 999.0
        forged["balance"] = 999.0
        forged["entries"][0]["amount"] = 999.0
        out_forged = anchor_treasury_config(forged, Ledger(g3_ledger))
        out_malformed = anchor_treasury_config({"schema": "nope"},
                                               Ledger(g3_ledger))
        checks.append((
            "TREASURY CONFIG CONFIRMED (fees re-derived); forged fees + "
            "malformed REJECTED",
            out_tc["evaluation"]["status"] == "treasury-config-confirmed"
            and out_tc["ledger_entry"] is not None
            and out_tc["evaluation"]["total_fees_collected"] == 3.0
            and out_forged["evaluation"]["status"] == "rejected"
            and "never produced" in out_forged["evaluation"]["reason"]
            and out_malformed["evaluation"]["status"] == "rejected",
            out_tc["evaluation"]["status"],
        ))

        # (42) the full two-bounty lifecycle, real-exercise ordering:
        # prov#1 -> prov#2 -> challenge#1 (drill, in-window at G1+2) ->
        # clawback#1 -> finalize#2 (window G2+1..G2+2 = the two #1 events,
        # challenge-free for #2 -> closed clean)
        g3l = Ledger(g3_ledger)
        out_p1 = anchor_gate3_event(
            {"phase": gate3_process.EVENT_PROVISIONAL, "bounty_id": "b-1",
             "work_ref": {"task_id": "task-0002", "work_id": wid2,
                          "taxonomy_tag": "TX17"},
             "amount": 1.0, "category": "reproducible-space-task"}, g3l)
        out_p2 = anchor_gate3_event(
            {"phase": gate3_process.EVENT_PROVISIONAL, "bounty_id": "b-2",
             "work_ref": {"task_id": "task-0008", "work_id": wid8,
                          "taxonomy_tag": "TX04"},
             "amount": 0.8, "category": "reproducible-space-task"}, g3l)
        out_ch = anchor_gate3_event(
            {"phase": gate3_process.EVENT_CHALLENGE, "bounty_id": "b-1",
             "grounds": "planned bounded-failure demonstration"}, g3l,
            drill=True)
        out_cb = anchor_gate3_event(
            {"phase": gate3_process.EVENT_CLAWBACK, "bounty_id": "b-1"}, g3l,
            drill=True)
        out_fin = anchor_gate3_event(
            {"phase": gate3_process.EVENT_FINALIZATION, "bounty_id": "b-2"},
            g3l)
        g3_chain_ok, _g3_reason = g3l.verify_chain()
        checks.append((
            "GATE-3 LIFECYCLE: grant+grant+challenge(drill)+clawback+finalize "
            "all confirmed, chain intact",
            out_p1["evaluation"]["status"] == "gate3-provisional-confirmed"
            and out_p1["evaluation"]["precheck"]["passed"] is True
            and out_p2["evaluation"]["status"] == "gate3-provisional-confirmed"
            and out_ch["evaluation"]["status"] == "gate3-challenge-filed"
            and out_ch["evaluation"].get("drill") is True
            and out_cb["evaluation"]["status"] == "gate3-clawback-confirmed"
            and out_cb["evaluation"]["adjudication"]["verdict"] == "upheld"
            and "never the base" in
            out_cb["evaluation"]["bounded_failure"]["statement"]
            and out_fin["evaluation"]["status"] == "gate3-finalization-confirmed"
            and "closed clean" in out_fin["evaluation"]["window_note"]
            and out_fin["evaluation"]["treasury_totals"]["balance"] == 2.2
            and g3_chain_ok is True,
            f"{out_fin['evaluation']['status']} "
            f"(balance {out_fin['evaluation'].get('treasury_totals', {}).get('balance')})",
        ))

        # (43) violations rejected: late challenge (window expired for b-2),
        # premature finalize (fresh grant, window open), failed-precheck grant
        out_late = anchor_gate3_event(
            {"phase": gate3_process.EVENT_CHALLENGE, "bounty_id": "b-2",
             "grounds": "too late"}, g3l)
        out_p3 = anchor_gate3_event(
            {"phase": gate3_process.EVENT_PROVISIONAL, "bounty_id": "b-3",
             "work_ref": {"task_id": "task-0002", "work_id": wid2,
                          "taxonomy_tag": "TX17"},
             "amount": 0.1, "category": "reproducible-space-task"}, g3l)
        out_early = anchor_gate3_event(
            {"phase": gate3_process.EVENT_FINALIZATION, "bounty_id": "b-3"},
            g3l)
        out_badpre = anchor_gate3_event(
            {"phase": gate3_process.EVENT_PROVISIONAL, "bounty_id": "b-4",
             "work_ref": {"task_id": "task-0002", "work_id": "0" * 64,
                          "taxonomy_tag": "TX17"},
             "amount": 0.05, "category": "reproducible-space-task"}, g3l)
        checks.append((
            "GATE-3 VIOLATIONS REJECTED: late challenge, premature finalize, "
            "failed pre-check",
            out_late["evaluation"]["status"] == "rejected"
            and "window expired" in out_late["evaluation"]["reason"]
            and out_p3["evaluation"]["status"] == "gate3-provisional-confirmed"
            and out_early["evaluation"]["status"] == "rejected"
            and "window OPEN" in out_early["evaluation"]["reason"]
            and out_badpre["evaluation"]["status"] == "rejected"
            and "pre-check FAILED" in out_badpre["evaluation"]["reason"],
            f"late={out_late['evaluation']['status']} "
            f"early={out_early['evaluation']['status']}",
        ))

        # (44) DELIBERATE MOLECULE ROUTING: the gate3 records join task-0002's
        # molecule — the challenge under challenge_events (event name contains
        # 'challenge'), provisional/clawback among verification_events — while
        # the FROZEN pre-gate3 generation stays locked (as-of rebuild unchanged)
        pre_g3_tip = out_tc["ledger_entry"]["index"] - 1
        mol_before = work_molecule.build_molecule(
            "task-0002", ledger_path=g3_ledger, as_of_index=pre_g3_tip)
        mol_after = work_molecule.build_molecule("task-0002",
                                                 ledger_path=g3_ledger)
        ch_events = {e["event"] for e in mol_after["challenge_events"]}
        ve_events = {e["event"] for e in mol_after["verification_events"]}
        mol_frozen = work_molecule.build_molecule(
            "task-0002", ledger_path=g3_ledger, as_of_index=pre_g3_tip)
        checks.append((
            "GATE-3 ROUTING: challenge->challenge_events, grant/clawback->"
            "verification_events; frozen as-of rebuild unchanged",
            gate3_process.EVENT_CHALLENGE in ch_events
            and gate3_process.EVENT_PROVISIONAL in ve_events
            and gate3_process.EVENT_CLAWBACK in ve_events
            and work_molecule.canonical_json(mol_frozen) == work_molecule.canonical_json(mol_before),
            f"ch={sorted(ch_events)}",
        ))

        # --- FLOW-1 (uptime emission) mode coverage ---------------------------------------
        # Fresh copy of the ACI fixture ledger; register the uptime actor's
        # root, run an epoch, anchor it; then the drills and the cross-type
        # reuse extension. Small keychain (16 keys) for speed.
        f1_ledger = os.path.join(tmp, "flow1_ledger.jsonl")
        _copyfile(aci_ledger_path, f1_ledger)
        f1_kc = actor_identity.generate_keychain(flow1_uptime.ACTOR_ID,
                                                 key_count=16)
        register_actor_key(actor_identity.public_declaration(f1_kc),
                           Ledger(f1_ledger))
        epoch = flow1_uptime.run_epoch(f1_kc, ledger_path=f1_ledger)

        # (45) epoch confirmed: every signature re-verified, arithmetic
        # replayed, statements + key_indices on record; scanner-invisible
        out_ep = anchor_uptime_epoch(epoch, Ledger(f1_ledger))
        ev_ep = out_ep["evaluation"]
        f1_chain_ok, _f1_reason = Ledger(f1_ledger).verify_chain()
        checks.append((
            "UPTIME EPOCH CONFIRMED (9/10 slots, 4.5 under cap; statements + "
            "key_indices on record; scanner-invisible)",
            ev_ep["status"] == "uptime-epoch-confirmed"
            and out_ep["ledger_entry"] is not None
            and ev_ep["verified_slots"] == 9 and ev_ep["missed_slots"] == [6]
            and ev_ep["total_emitted"] == 4.5
            and ev_ep["cap_respected"] is True
            and ev_ep["key_indices"] == list(range(9))
            and "no discretion" in ev_ep["missed_slot_statement"]
            and "both directions" in ev_ep["two_flow_separation"]
            and "task_id" not in ev_ep and "task_ids" not in ev_ep
            and f1_chain_ok is True,
            ev_ep["status"],
        ))

        # (46) CROSS-TYPE reuse via the anchored epoch: a signed CHALLENGE
        # round reusing an epoch-consumed index is rejected by the shared scan
        ch_f1 = challenge_response.issue_challenge(TASK, flow1_uptime.ACTOR_ID,
                                                   ledger_path=f1_ledger)
        resp_f1 = challenge_response.respond(ch_f1, ledger_path=f1_ledger,
                                             keychain=f1_kc, key_index=0,
                                             force_reuse=True)
        out_xr = anchor_challenge_result(ch_f1, resp_f1, Ledger(f1_ledger))
        checks.append((
            "CROSS-TYPE REUSE rejected (epoch consumed index 0; challenge "
            "reusing it fails, citing the epoch record)",
            out_xr["evaluation"]["status"] == "challenge-failed"
            and "one-time key index reuse" in str(
                out_xr["evaluation"]["coordinator_reconfirmed"]
                ["first_failure_reason"]),
            out_xr["evaluation"]["status"],
        ))

        # (47) forged-heartbeat drill: signed with the WRONG actor's keychain
        # (internally valid Lamport material, wrong root) -> anchored rejection;
        # an HONEST heartbeat is refused as drill input
        wrong_kc = actor_identity.generate_keychain("selftest-agent",
                                                    key_count=2)
        forged = flow1_uptime.emit_heartbeat(wrong_kc, 0,
                                             ledger_path=f1_ledger)
        forged["actor_id"] = flow1_uptime.ACTOR_ID
        forged["signature"]["actor_id"] = flow1_uptime.ACTOR_ID
        out_fh = anchor_forged_heartbeat_drill(forged, Ledger(f1_ledger))
        honest_hb = flow1_uptime.emit_heartbeat(f1_kc, 9,
                                                ledger_path=f1_ledger)
        out_hh = anchor_forged_heartbeat_drill(honest_hb, Ledger(f1_ledger))
        checks.append((
            "FORGED HEARTBEAT anchored as rejected (drill); honest heartbeat "
            "refused as drill input",
            out_fh["evaluation"]["status"] == "heartbeat-forged-rejected"
            and out_fh["evaluation"].get("drill") is True
            and out_fh["evaluation"]["emitted"] == 0.0
            and out_fh["evaluation"]["first_failure_reason"] is not None
            and out_hh["evaluation"]["status"] == "rejected"
            and "not a forgery" in out_hh["evaluation"]["reason"],
            f"{out_fh['evaluation']['status']} / {out_hh['evaluation']['status']}",
        ))

        # (48) malformed + mismatch epochs: hash-inconsistent -> rejected;
        # consistent-but-wrong summary -> re-verification disagrees -> mismatch
        bad_ep = dict(epoch)
        bad_ep["epoch_hash"] = "0" * 64
        out_badep = anchor_uptime_epoch(bad_ep, Ledger(f1_ledger))
        tampered_ep = json.loads(json.dumps(epoch))
        tampered_ep["summary"]["total_emitted"] = 5.0
        tampered_ep["summary"]["verified_slots"] = 10
        tampered_ep["epoch_hash"] = flow1_uptime.compute_epoch_hash(tampered_ep)
        out_tep = anchor_uptime_epoch(tampered_ep, Ledger(f1_ledger))
        checks.append((
            "EPOCH REJECTED (bad hash) + MISMATCH (inflated emission -> "
            "re-verification disagrees, anchored audit event)",
            out_badep["evaluation"]["status"] == "rejected"
            and out_badep["ledger_entry"] is None
            and out_tep["evaluation"]["status"] == "uptime-epoch-mismatch"
            and out_tep["ledger_entry"] is not None,
            f"{out_badep['evaluation']['status']} / {out_tep['evaluation']['status']}",
        ))

        # --- PASSPORT-CATALOG mode coverage -----------------------------------------------
        # Reuses f1_ledger (rich history: 13 evals + registration + epoch +
        # challenge records). (49) confirmed + scanner-invisible; (50)
        # malformed rejected + tampered hash -> mismatch audit event.
        pp_cat = metawork_passport.build_passport_catalog(ledger_path=f1_ledger)
        out_pp = anchor_passport_catalog(pp_cat, Ledger(f1_ledger))
        ev_pp = out_pp["evaluation"]
        pp_chain_ok, _pp_reason = Ledger(f1_ledger).verify_chain()
        checks.append((
            "PASSPORT CATALOG CONFIRMED (all passports rebuilt; counts only; "
            "no-leaderboard + UWW-transparency affirmations on record)",
            ev_pp["status"] == "passport-catalog-confirmed"
            and out_pp["ledger_entry"] is not None
            and ev_pp["actor_count"] == len(pp_cat["entries"])
            and "mechanical rule" in ev_pp["no_leaderboard"]
            and "mechanically blind" in ev_pp["uww_transparency"]
            and "task_id" not in ev_pp and "task_ids" not in ev_pp
            and "entries" not in ev_pp
            and pp_chain_ok is True,
            f"{ev_pp['status']} ({ev_pp['actor_count']} actors)",
        ))
        bad_pp = dict(pp_cat)
        bad_pp["catalog_hash"] = "0" * 64
        out_badpp = anchor_passport_catalog(bad_pp, Ledger(f1_ledger))
        tampered_pp = json.loads(json.dumps(pp_cat))
        tampered_pp["entries"][0]["passport_hash"] = "1" * 64
        tampered_pp["catalog_hash"] = metawork_passport.compute_catalog_hash(
            tampered_pp)
        out_tpp = anchor_passport_catalog(tampered_pp, Ledger(f1_ledger))
        checks.append((
            "PASSPORT REJECTED (bad hash) + MISMATCH (tampered passport_hash "
            "-> rebuild disagrees, anchored audit event)",
            out_badpp["evaluation"]["status"] == "rejected"
            and out_badpp["ledger_entry"] is None
            and out_tpp["evaluation"]["status"] == "passport-catalog-mismatch"
            and out_tpp["ledger_entry"] is not None,
            f"{out_badpp['evaluation']['status']} / {out_tpp['evaluation']['status']}",
        ))

        # (41) STABILITY after temp anchors onto a copy of the REAL ledger
        # (conditional — the runtime ledger is gitignored and absent in CI): a fresh
        # economy anchor must leave the 0.2 catalog (idx-17), the ACI report_hash
        # (idx-18), and the economy hash (idx-19) unchanged; and when the real ledger
        # already carries metering evidence + a gen-2 catalog, the 0.3 rebuild must
        # match its anchored catalog_hash too (QUADRUPLE stability).
        real_ledger = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "ledger_data.jsonl"
        )
        real_entries = []
        if os.path.exists(real_ledger):
            with open(real_ledger, "r", encoding="utf-8") as f:
                real_entries = [json.loads(ln) for ln in f if ln.strip()]
        idx17 = next((e for e in real_entries
                      if e["payload"].get("event") == "work_molecule_catalog_anchored"
                      and e["payload"].get("molecule_schema") ==
                      work_molecule.SCHEMA_VERSION_02), None)
        idx18 = next((e for e in real_entries
                      if e["payload"].get("event") == "aci_baseline_anchored"), None)
        if idx17 is not None and idx18 is not None:
            triple_ledger = os.path.join(tmp, "triple_ledger.jsonl")
            _copyfile(real_ledger, triple_ledger)
            out_triple = anchor_economy_summary(econ_log, Ledger(triple_ledger))
            # GENERATION-LOCKED rebuilds (as_of = anchor index - 1): frozen
            # generations must re-derive forever; unbounded rebuilds legitimately
            # drift once later task-referencing events (challenge records) exist.
            cat_after = work_molecule.build_catalog(
                ledger_path=triple_ledger,
                schema_version=work_molecule.SCHEMA_VERSION_02,
                as_of_index=idx17["index"] - 1)
            aci_after = agent_concentration.compute_report(
                agent_concentration.build_paths(ledger_path=triple_ledger,
                                                as_of_index=idx18["index"] - 1)
            )
            checks.append((
                "TRIPLE STABILITY after temp anchor (0.2 WMIDs + ACI hash + economy hash)",
                out_triple["evaluation"]["status"] == "economy-demo-confirmed"
                and cat_after["catalog_hash"] == idx17["payload"]["catalog_hash"]
                and aci_after["report_hash"] == idx18["payload"]["report_hash"]
                and out_triple["evaluation"]["coordinator_reconfirmed"]["log_hash_matches"] is True,
                f"catalog={cat_after['catalog_hash'][:12]}.. "
                f"aci={aci_after['report_hash'][:12]}..",
            ))
            gen2 = next((e for e in reversed(real_entries)
                         if e["payload"].get("event") == "work_molecule_catalog_anchored"
                         and e["payload"].get("molecule_schema") ==
                         work_molecule.SCHEMA_VERSION_03), None)
            if gen2 is not None:
                cat03_after = work_molecule.build_catalog(
                    ledger_path=triple_ledger, as_of_index=gen2["index"] - 1)
                checks.append((
                    "QUADRUPLE STABILITY: 0.3 rebuild matches the anchored gen-2 catalog",
                    cat03_after["catalog_hash"] == gen2["payload"]["catalog_hash"],
                    f"catalog_v03={cat03_after['catalog_hash'][:12]}..",
                ))
            else:
                print("    (no anchored gen-2 catalog on the real ledger yet — 0.3 "
                      "stability leg SKIPPED; check (24) covers the mechanism)")
            cut_anchor = next((e for e in reversed(real_entries)
                               if e["payload"].get("event") == _CUT_EVENT
                               and e["payload"].get("status") ==
                               _CUT_CONFIRMED_STATUS), None)
            if cut_anchor is not None:
                cut_after = cut_certificate.build_cut(
                    sorted(verifier_cli.TASK_MODULES), ledger_path=triple_ledger,
                    as_of_index=cut_anchor["index"] - 1)
                cut_accept, _cut_note = cut_certificate.accept_by_anchor(
                    cut_after, ledger_path=triple_ledger)
                checks.append((
                    "QUINTUPLE STABILITY: cut rebuild matches the anchored "
                    "certificate + cheap acceptance holds",
                    cut_after["certificate_hash"] ==
                    cut_anchor["payload"]["certificate_hash"]
                    and cut_accept is True,
                    f"cut={cut_after['certificate_hash'][:12]}..",
                ))
            else:
                print("    (no anchored cut certificate on the real ledger yet — "
                      "cut stability leg SKIPPED; checks (25)-(26) cover the "
                      "mechanism)")
            tv_anchor = next((e for e in reversed(real_entries)
                              if e["payload"].get("event") == _TV_EVENT
                              and e["payload"].get("status") ==
                              _TV_CONFIRMED_STATUS), None)
            if tv_anchor is not None:
                tv_after = trust_vector.build_tv_catalog(
                    ledger_path=triple_ledger, as_of_index=tv_anchor["index"] - 1)
                checks.append((
                    "SEXTUPLE STABILITY: trust-vector rebuild matches the "
                    "anchored catalog",
                    tv_after["catalog_hash"] ==
                    tv_anchor["payload"]["catalog_hash"],
                    f"tv={tv_after['catalog_hash'][:12]}..",
                ))
            else:
                print("    (no anchored trust-vector catalog on the real ledger "
                      "yet — TV stability leg SKIPPED; checks (29)-(31) cover the "
                      "mechanism)")
        else:
            print("    (no real ledger with idx-17/idx-18 anchors present — real-ledger "
                  "stability check SKIPPED; the scanner-invisibility checks above "
                  "cover the mechanism)")

        # --- report ---------------------------------------------------------
        print("=== protocol/external_verifier.py self-test — R3 EXTERNAL-VERIFIER-PILOT ===")
        print("HONEST: a matching output_hash proves REPRODUCIBILITY, NOT execution (a hash")
        print("can be copied). Not consensus, not mainnet, not payment, not a token; zero-value.")
        print("This self-test is a LOCAL TEST HARNESS (single-host simulation), NOT the product;")
        print("in real use the submission arrives from a SEPARATE machine/person.\n")

        print("--- sample VALID external submission (note honest topology + limitation) ---")
        print(json.dumps(sample_submission, indent=2, sort_keys=True))
        print()
        print("--- coordinator evaluation of that submission (externally-verified) ---")
        print(json.dumps(sample_evaluation, indent=2, sort_keys=True))
        print()
        print("--- sample SAME-MACHINE self-recompute evaluation (locally-verified; NOT external; "
              "carries task_class + operator_relationship) ---")
        print(json.dumps(sample_same_evaluation, indent=2, sort_keys=True))
        print()
        print("--- OPTIONAL attached R2 software-key MAC (symmetric; not a signature; not hardware; not execution-proof) ---")
        print(json.dumps(signed_attestation, indent=2, sort_keys=True))
        print()
        print("--- sample ANCHORED agent-result payload (note spark_reconfirmed + honest labels) ---")
        print("    HONEST: the Spark RE-DERIVES the agent's claims here; it does not trust the file.")
        print(json.dumps(agent_confirmed_payload, indent=2, sort_keys=True))
        print()

        print("--- results ---")
        for label, passed, _detail in checks:
            print(f"{label}: {'PASS' if passed else 'FAIL'}")
        print()
        print("--- detail ---")
        for label, _passed, detail in checks:
            print(f"  {label}\n      -> {detail}")
        print()

        all_ok = all(passed for _l, passed, _d in checks)
        print("=== self-test summary: " +
              ("ALL CHECKS BEHAVED CORRECTLY" if all_ok else "FAILURE — see above") + " ===")
    finally:
        _rmtree(tmp)

    # Confirm no stray default files anywhere in protocol/.
    proto = os.path.dirname(os.path.abspath(__file__))
    stray_ledger = os.path.exists(_real_ledger_path) and not _ledger_existed_before
    stray_keys = [n for n in os.listdir(proto) if n.endswith(".secret")]
    stray_sub = os.path.exists(os.path.join(proto, "submission.json"))
    print(f"\nstray default ledger file: {'PRESENT (!!)' if stray_ledger else 'absent (good)'}")
    print(f"stray protocol/*.secret key files: {stray_keys if stray_keys else 'none (good)'}")
    print(f"stray submission.json: {'PRESENT (!!)' if stray_sub else 'absent (good)'}")

    ok_overall = (
        all(passed for _l, passed, _d in checks)
        and not stray_ledger
        and not stray_keys
        and not stray_sub
    )
    return 0 if ok_overall else 1


# ============================== REAL COORDINATOR CLI =========================
# Importable functions and the self-test above are unchanged. The CLI below lets the
# coordinator ingest a REAL submission file and anchor the outcome into a REAL persistent
# ledger. With NO arguments, main() runs the self-test on a temp ledger and never touches
# the real ledger (so CI's `python3 protocol/external_verifier.py` keeps running 3/3).

BANNER = (
    "MetaCoin R3 external-verifier pilot — research-stage, zero-value. "
    "A matching hash proves REPRODUCIBILITY, not execution."
)


def _cmd_genesis(ledger_path: str) -> int:
    """Append a neutral chain-start marker IFF the ledger is empty; refuse otherwise.

    Exit 0 on success; non-zero if the ledger already has entries (never double-genesis).
    """
    print(BANNER)
    ledger = Ledger(ledger_path)
    if ledger.read_all():
        print(f"ledger already initialized, genesis exists at index 0 (path: {ledger_path})")
        print("refusing to double-genesis")
        return 1
    payload = {
        "event": "ledger_genesis",
        "stage": "R-protocol",
        "note": (
            "MetaCoin research-stage protocol ledger chain-start marker. "
            "Zero-value, no token, no money. Research only."
        ),
        "zero_value": True,
        "no_token": True,
    }
    entry = ledger.append(payload)
    print(f"genesis written: index {entry['index']}, hash {entry['hash'][:16]}... (path: {ledger_path})")
    ok, reason = ledger.verify_chain()
    print(f"chain verify: {'OK' if ok else 'FAIL'} — {reason}")
    return 0 if ok else 1


def _cmd_evaluate(submission_path: str, ledger_path: str) -> int:
    """Load a submission file, evaluate it, and anchor the outcome into the ledger.

    Exit codes: 0 = externally-verified; non-zero = external-mismatch or rejected.
    A missing/invalid file is 'rejected' and anchors nothing.
    """
    print(BANNER)

    # Load the submission file defensively; any load failure is a rejection (no anchor).
    reason = None
    submission = None
    try:
        with open(submission_path, "r", encoding="utf-8") as f:
            submission = json.load(f)
    except FileNotFoundError:
        reason = f"submission file not found: {submission_path}"
    except json.JSONDecodeError as exc:
        reason = f"submission file is not valid JSON ({exc})"
    except OSError as exc:
        reason = f"could not read submission file ({exc})"
    if reason is None and not isinstance(submission, dict):
        reason = "submission JSON is not an object"
    if reason is not None:
        print("status: rejected")
        print(f"reason: {reason}")
        print("anchored: no (nothing written to the ledger)")
        return 1

    ledger = Ledger(ledger_path)
    result = evaluate_submission(submission, ledger)
    ev = result["evaluation"]
    entry = result["ledger_entry"]
    status = ev["status"]

    print(f"status: {status}")
    print(f"task_id: {submission.get('task_id', 'unknown')}")
    print(f"verifier_id: {submission.get('verifier_id', 'unknown')}")

    if status == "rejected":
        print(f"reason: {ev.get('reason')}")
        print("anchored: no (malformed submissions are not written to the ledger)")
        return 1

    print(f"submitted_output_hash: {ev['submitted_output_hash']}")
    print(f"local_output_hash:     {ev['local_output_hash']}")
    print(f"match: {ev['match']}")
    if entry is not None:
        print(f"anchored at ledger index: {entry['index']} (path: {ledger_path})")
    ok, vreason = ledger.verify_chain()
    print(f"chain verify: {'OK' if ok else 'FAIL'} — {vreason}")

    # 0 only for a genuine verification (external pilot OR same-machine self-recompute);
    # a mismatch is a real but non-zero outcome.
    return 0 if status in ("externally-verified", "locally-verified") else 1


def _cmd_anchor_agent_result(result_path: str, ledger_path: str, snapshot_path: str,
                             anchor_path: str, operator_relationship: str) -> int:
    """Load an agent_verifier_attestation, RE-DERIVE its claims on the Spark, anchor the outcome.

    Exit codes: 0 = agent-result-confirmed; non-zero = mismatch or rejected.
    A missing/invalid file is 'rejected' and anchors nothing.
    """
    print(BANNER)

    # Load defensively; any load failure is a rejection (no anchor).
    reason = None
    result = None
    try:
        with open(result_path, "r", encoding="utf-8") as f:
            result = json.load(f)
    except FileNotFoundError:
        reason = f"agent-result file not found: {result_path}"
    except json.JSONDecodeError as exc:
        reason = f"agent-result file is not valid JSON ({exc})"
    except OSError as exc:
        reason = f"could not read agent-result file ({exc})"
    if reason is None and not isinstance(result, dict):
        reason = "agent-result JSON is not an object"
    if reason is not None:
        print("status: rejected")
        print(f"reason: {reason}")
        print("anchored: no (nothing written to the ledger)")
        return 1

    ledger = Ledger(ledger_path)
    out = anchor_agent_result(result, ledger, snapshot_path, anchor_path, operator_relationship)
    ev = out["evaluation"]
    entry = out["ledger_entry"]
    status = ev["status"]

    print(f"status: {status}")
    print(f"verifier_id: {result.get('verifier_id', 'unknown')}")
    print(f"agent_verdict: {result.get('verdict', 'unknown')}")

    if status == "rejected":
        print(f"reason: {ev.get('reason')}")
        print("anchored: no (malformed -> not written to the ledger)")
        return 1

    sr = ev["spark_reconfirmed"]
    print(f"task_ids: {ev.get('task_ids')}")
    print("spark re-derivation (independently re-derived here — NOT trusting the agent's file):")
    print(f"  chain_verified     : {sr['chain_verified']}")
    print(f"  tip_matches_anchor : {sr['tip_matches_anchor']}")
    print(f"  agent_tips_agree   : {sr['agent_tips_agree']}")
    print(f"  task_reproduced    : {sr['task_reproduced']}")
    for pt in sr.get("per_task", []):
        print(f"    - {pt['task_id']}: local={pt['local_output_hash']} "
              f"matches_agent={pt['matches_agent_recomputed']} "
              f"matches_ledger={pt['matches_ledger_recorded']} reproduced={pt['reproduced']}")
    if entry is not None:
        print(f"anchored at ledger index: {entry['index']} (path: {ledger_path})")
    ok, vreason = ledger.verify_chain()
    print(f"chain verify: {'OK' if ok else 'FAIL'} — {vreason}")

    # 0 only when the Spark re-derivation confirms the agent's 'verified' verdict.
    return 0 if status == "agent-result-confirmed" else 1


def _cmd_anchor_molecule_catalog(catalog_path: str, ledger_path: str) -> int:
    """Load a work-molecule catalog, REBUILD it from the ledger, anchor the outcome.

    Exit codes: 0 = molecule-catalog-confirmed; non-zero = mismatch or rejected.
    A missing/invalid file is 'rejected' and anchors nothing.
    """
    print(BANNER)

    # Load defensively; any load failure is a rejection (no anchor).
    reason = None
    catalog = None
    try:
        with open(catalog_path, "r", encoding="utf-8") as f:
            catalog = json.load(f)
    except FileNotFoundError:
        reason = f"catalog file not found: {catalog_path}"
    except json.JSONDecodeError as exc:
        reason = f"catalog file is not valid JSON ({exc})"
    except OSError as exc:
        reason = f"could not read catalog file ({exc})"
    if reason is None and not isinstance(catalog, dict):
        reason = "catalog JSON is not an object"
    if reason is not None:
        print("status: rejected")
        print(f"reason: {reason}")
        print("anchored: no (nothing written to the ledger)")
        return 1

    ledger = Ledger(ledger_path)
    out = anchor_molecule_catalog(catalog, ledger)
    ev = out["evaluation"]
    entry = out["ledger_entry"]
    status = ev["status"]

    print(f"status: {status}")
    if status == "rejected":
        print(f"reason: {ev.get('reason')}")
        print("anchored: no (malformed -> not written to the ledger)")
        return 1

    cr = ev["coordinator_reconfirmed"]
    print(f"molecule_schema: {ev['molecule_schema']}")
    print(f"catalog_hash:            {ev['catalog_hash']}")
    print(f"recomputed_catalog_hash: {cr['recomputed_catalog_hash']}")
    print("coordinator rebuild (independently re-assembled here — NOT trusting the file):")
    print(f"  catalog_hash_matches : {cr['catalog_hash_matches']}")
    print(f"  entries_match        : {cr['entries_match']}")
    print(f"  rebuilt_entry_count  : {cr['rebuilt_entry_count']}")
    for e in ev["catalog_entries"]:
        print(f"    - {e['task_id']}: {e['work_id']}")
    if entry is not None:
        print(f"anchored at ledger index: {entry['index']} (path: {ledger_path})")
    ok, vreason = ledger.verify_chain()
    print(f"chain verify: {'OK' if ok else 'FAIL'} — {vreason}")

    # 0 only when the coordinator's rebuild confirms every WMID in the catalog.
    return 0 if status == "molecule-catalog-confirmed" else 1


def _cmd_anchor_aci_report(report_path: str, ledger_path: str) -> int:
    """Load an ACI report, RECOMPUTE it from the ledger, anchor the outcome.

    Exit codes: 0 = aci-baseline-confirmed; non-zero = mismatch or rejected.
    A missing/invalid file is 'rejected' and anchors nothing.
    """
    print(BANNER)

    # Load defensively; any load failure is a rejection (no anchor).
    reason = None
    report = None
    try:
        with open(report_path, "r", encoding="utf-8") as f:
            report = json.load(f)
    except FileNotFoundError:
        reason = f"ACI report file not found: {report_path}"
    except json.JSONDecodeError as exc:
        reason = f"ACI report file is not valid JSON ({exc})"
    except OSError as exc:
        reason = f"could not read ACI report file ({exc})"
    if reason is None and not isinstance(report, dict):
        reason = "ACI report JSON is not an object"
    if reason is not None:
        print("status: rejected")
        print(f"reason: {reason}")
        print("anchored: no (nothing written to the ledger)")
        return 1

    ledger = Ledger(ledger_path)
    out = anchor_aci_report(report, ledger)
    ev = out["evaluation"]
    entry = out["ledger_entry"]
    status = ev["status"]

    print(f"status: {status}")
    if status == "rejected":
        print(f"reason: {ev.get('reason')}")
        print("anchored: no (malformed -> not written to the ledger)")
        return 1

    cr = ev["coordinator_reconfirmed"]
    print(f"report_hash:            {ev['report_hash']}")
    print(f"recomputed_report_hash: {cr['recomputed_report_hash']}")
    print("coordinator recompute (full rebuild + re-score here — NOT trusting the file):")
    print(f"  report_hash_matches : {cr['report_hash_matches']}")
    print(f"  pairwise_aci        : {ev['pairwise_aci']}  (recomputed: {cr['recomputed_pairwise_aci']})")
    print(f"  eis                 : {ev['eis']}")
    print(f"  paths/pairs         : {ev['path_count']}/{ev['pair_count']}")
    print(f"  concentration       : {ev['concentration_profile']}")
    print(f"  missing-metadata    : {ev['missing_metadata_flag_count']} flags, "
          f"coverage {ev['metadata_coverage_ratio']:.4f}")
    if entry is not None:
        print(f"anchored at ledger index: {entry['index']} (path: {ledger_path})")
    ok, vreason = ledger.verify_chain()
    print(f"chain verify: {'OK' if ok else 'FAIL'} — {vreason}")

    # 0 only when the coordinator's own recompute reproduces the submitted report_hash.
    return 0 if status == "aci-baseline-confirmed" else 1


def _cmd_anchor_economy_summary(log_path: str, ledger_path: str) -> int:
    """Load an economy log, RE-RUN the full simulation, anchor the outcome.

    Exit codes: 0 = economy-demo-confirmed; non-zero = mismatch or rejected.
    A missing/invalid file is 'rejected' and anchors nothing.
    """
    print(BANNER)

    # Load defensively; any load failure is a rejection (no anchor).
    reason = None
    log = None
    try:
        with open(log_path, "r", encoding="utf-8") as f:
            log = json.load(f)
    except FileNotFoundError:
        reason = f"economy log file not found: {log_path}"
    except json.JSONDecodeError as exc:
        reason = f"economy log file is not valid JSON ({exc})"
    except OSError as exc:
        reason = f"could not read economy log file ({exc})"
    if reason is None and not isinstance(log, dict):
        reason = "economy log JSON is not an object"
    if reason is not None:
        print("status: rejected")
        print(f"reason: {reason}")
        print("anchored: no (nothing written to the ledger)")
        return 1

    ledger = Ledger(ledger_path)
    out = anchor_economy_summary(log, ledger)
    ev = out["evaluation"]
    entry = out["ledger_entry"]
    status = ev["status"]

    print(f"status: {status}")
    if status == "rejected":
        print(f"reason: {ev.get('reason')}")
        print("anchored: no (malformed -> not written to the ledger)")
        return 1

    cr = ev["coordinator_reconfirmed"]
    print(f"economy_log_hash: {ev['economy_log_hash']}")
    print(f"rerun_log_hash:   {cr['rerun_log_hash']}")
    print("coordinator re-run (entire simulation re-executed here — NOT trusting the file):")
    print(f"  log_hash_matches      : {cr['log_hash_matches']}")
    print(f"  simulated_days        : {ev['simulated_days']} (day indices, not real time)")
    print(f"  verified/rejected     : {ev['verified_count']}/{ev['rejected_count']} "
          f"(planned drill rejections: {ev['planned_drill_rejections']})")
    print(f"  earned/spent/balance  : {ev['total_earned']}/{ev['total_spent']}/"
          f"{ev['final_balance']} Test-META (zero value)")
    print(f"  distinct tasks        : {ev['distinct_task_count']}")
    if entry is not None:
        print(f"anchored at ledger index: {entry['index']} (path: {ledger_path})")
    ok, vreason = ledger.verify_chain()
    print(f"chain verify: {'OK' if ok else 'FAIL'} — {vreason}")

    # 0 only when the coordinator's own full re-run reproduces the submitted log hash.
    return 0 if status == "economy-demo-confirmed" else 1


def _cmd_anchor_metering_report(report_path: str, ledger_path: str) -> int:
    """Load a metering report, RE-METER the tasks for plausibility, anchor the outcome.

    Exit codes: 0 = metering-evidence-confirmed; non-zero = mismatch or rejected.
    A missing/invalid file is 'rejected' and anchors nothing. Plausibility, NOT
    byte-reconfirmation: timing is non-deterministic, so the anchored report_hash
    fixes the claim made at measurement time.
    """
    print(BANNER)

    # Load defensively; any load failure is a rejection (no anchor).
    reason = None
    report = None
    try:
        with open(report_path, "r", encoding="utf-8") as f:
            report = json.load(f)
    except FileNotFoundError:
        reason = f"metering report file not found: {report_path}"
    except json.JSONDecodeError as exc:
        reason = f"metering report file is not valid JSON ({exc})"
    except OSError as exc:
        reason = f"could not read metering report file ({exc})"
    if reason is None and not isinstance(report, dict):
        reason = "metering report JSON is not an object"
    if reason is not None:
        print("status: rejected")
        print(f"reason: {reason}")
        print("anchored: no (nothing written to the ledger)")
        return 1

    ledger = Ledger(ledger_path)
    out = anchor_metering_report(report, ledger)
    ev = out["evaluation"]
    entry = out["ledger_entry"]
    status = ev["status"]

    print(f"status: {status}")
    if status == "rejected":
        print(f"reason: {ev.get('reason')}")
        print("anchored: no (malformed/non-canonical -> not written to the ledger)")
        return 1

    cr = ev["coordinator_reconfirmed"]
    print(f"report_hash: {ev['report_hash']}  (fixes the CLAIM; timing is not "
          "byte-reproducible)")
    print(f"tasks metered: {ev['task_count']}  labels: {ev['labels']}")
    print(f"totals: wall {ev['total_wall_time_s']}s  cpu {ev['total_cpu_time_s']}s  "
          f"energy~{ev['total_energy_j_estimate']}J "
          f"(ESTIMATED @ {ev['assumed_cpu_power_w']} W assumed)")
    print("coordinator re-metering (plausibility re-derived here — NOT trusting the file):")
    print(f"  output_hashes_match_ledger : {cr['remetered_output_hashes_match_ledger']}")
    print(f"  timings_within_sanity_band : {cr['timings_within_sanity_band']} "
          f"(max task wall {cr['remetered_max_task_wall_time_s']}s)")
    print(f"  remetered totals           : wall {cr['remetered_total_wall_time_s']}s  "
          f"cpu {cr['remetered_total_cpu_time_s']}s")
    if entry is not None:
        print(f"anchored at ledger index: {entry['index']} (path: {ledger_path})")
    ok, vreason = ledger.verify_chain()
    print(f"chain verify: {'OK' if ok else 'FAIL'} — {vreason}")

    # 0 only when the coordinator's own re-metering confirms plausibility.
    return 0 if status == _METERING_CONFIRMED_STATUS else 1


def _cmd_anchor_cut_certificate(cert_path: str, ledger_path: str) -> int:
    """Load a cut certificate, FULLY VERIFY it (anchor-time full proof), anchor the outcome.

    Exit codes: 0 = cut-certificate-confirmed; non-zero = mismatch or rejected.
    A missing/invalid file is 'rejected' and anchors nothing.
    """
    print(BANNER)

    # Load defensively; any load failure is a rejection (no anchor).
    reason = None
    cert = None
    try:
        with open(cert_path, "r", encoding="utf-8") as f:
            cert = json.load(f)
    except FileNotFoundError:
        reason = f"certificate file not found: {cert_path}"
    except json.JSONDecodeError as exc:
        reason = f"certificate file is not valid JSON ({exc})"
    except OSError as exc:
        reason = f"could not read certificate file ({exc})"
    if reason is None and not isinstance(cert, dict):
        reason = "certificate JSON is not an object"
    if reason is not None:
        print("status: rejected")
        print(f"reason: {reason}")
        print("anchored: no (nothing written to the ledger)")
        return 1

    ledger = Ledger(ledger_path)
    out = anchor_cut_certificate(cert, ledger)
    ev = out["evaluation"]
    entry = out["ledger_entry"]
    status = ev["status"]

    print(f"status: {status}")
    if status == "rejected":
        print(f"reason: {ev.get('reason')}")
        print("anchored: no (malformed -> not written to the ledger)")
        return 1

    cr = ev["coordinator_reconfirmed"]
    print(f"certificate_hash : {ev['certificate_hash']}")
    print(f"aggregate_hash   : {ev['aggregate_hash']}")
    print(f"interior/boundary: {ev['interior_count']}/{ev['boundary_count']} "
          f"({ev['molecule_schema']})")
    print("coordinator full verification (the EXPENSIVE proof, run once, here — "
          "later acceptance is cheap because of it):")
    print(f"  full_verification_passed : {cr['full_verification_passed']}")
    print(f"  rebuilt_interior_count   : {cr['rebuilt_interior_count']}")
    if cr["first_failure_reason"]:
        print(f"  first_failure_reason     : {cr['first_failure_reason']}")
    if entry is not None:
        print(f"anchored at ledger index: {entry['index']} (path: {ledger_path})")
    ok, vreason = ledger.verify_chain()
    print(f"chain verify: {'OK' if ok else 'FAIL'} — {vreason}")

    # 0 only when the coordinator's own full verification proved the certificate.
    return 0 if status == _CUT_CONFIRMED_STATUS else 1


def _cmd_anchor_trust_vector_catalog(catalog_path: str, ledger_path: str) -> int:
    """Load a trust-vector catalog, REBUILD all vectors from the ledger, anchor the outcome.

    Exit codes: 0 = trust-vector-catalog-confirmed; non-zero = mismatch or rejected.
    A missing/invalid file is 'rejected' and anchors nothing.
    """
    print(BANNER)

    # Load defensively; any load failure is a rejection (no anchor).
    reason = None
    catalog = None
    try:
        with open(catalog_path, "r", encoding="utf-8") as f:
            catalog = json.load(f)
    except FileNotFoundError:
        reason = f"catalog file not found: {catalog_path}"
    except json.JSONDecodeError as exc:
        reason = f"catalog file is not valid JSON ({exc})"
    except OSError as exc:
        reason = f"could not read catalog file ({exc})"
    if reason is None and not isinstance(catalog, dict):
        reason = "catalog JSON is not an object"
    if reason is not None:
        print("status: rejected")
        print(f"reason: {reason}")
        print("anchored: no (nothing written to the ledger)")
        return 1

    ledger = Ledger(ledger_path)
    out = anchor_trust_vector_catalog(catalog, ledger)
    ev = out["evaluation"]
    entry = out["ledger_entry"]
    status = ev["status"]

    print(f"status: {status}")
    if status == "rejected":
        print(f"reason: {ev.get('reason')}")
        print("anchored: no (malformed -> not written to the ledger)")
        return 1

    cr = ev["coordinator_reconfirmed"]
    print(f"catalog_hash:            {ev['catalog_hash']}")
    print(f"recomputed_catalog_hash: {cr['recomputed_catalog_hash']}")
    print(f"vectors: {ev['vector_count']}  ({ev['no_combined_scalar']})")
    print("coordinator rebuild (all vectors re-derived here — NOT trusting the file):")
    print(f"  catalog_hash_matches : {cr['catalog_hash_matches']}")
    print(f"  entries_match        : {cr['entries_match']}")
    print(f"  rebuilt_vector_count : {cr['rebuilt_vector_count']}")
    if entry is not None:
        print(f"anchored at ledger index: {entry['index']} (path: {ledger_path})")
    ok, vreason = ledger.verify_chain()
    print(f"chain verify: {'OK' if ok else 'FAIL'} — {vreason}")

    # 0 only when the coordinator's rebuild confirms every tv_hash in the catalog.
    return 0 if status == _TV_CONFIRMED_STATUS else 1


def _cmd_anchor_challenge_result(challenge_path: str, response_path: str,
                                 ledger_path: str, drill: bool) -> int:
    """Load a challenge + response, VERIFY by full recompute, anchor the outcome.

    Exit codes: 0 = challenge-verified; non-zero = challenge-failed or rejected.
    (With --drill a 'challenge-failed' outcome is the EXPECTED demonstration, but
    the exit code still reports the verification result truthfully.)
    """
    print(BANNER)

    # Load defensively; any load failure is a rejection (no anchor).
    docs = []
    for label, path in (("challenge", challenge_path), ("response", response_path)):
        try:
            with open(path, "r", encoding="utf-8") as f:
                doc = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
            print("status: rejected")
            print(f"reason: {label} file unreadable: {exc}")
            print("anchored: no (nothing written to the ledger)")
            return 1
        if not isinstance(doc, dict):
            print("status: rejected")
            print(f"reason: {label} JSON is not an object")
            print("anchored: no (nothing written to the ledger)")
            return 1
        docs.append(doc)
    challenge, response = docs

    ledger = Ledger(ledger_path)
    out = anchor_challenge_result(challenge, response, ledger, drill=drill)
    ev = out["evaluation"]
    entry = out["ledger_entry"]
    status = ev["status"]

    print(f"status: {status}" + ("  (PLANNED DRILL)" if ev.get("drill") else ""))
    if status == "rejected":
        print(f"reason: {ev.get('reason')}")
        print("anchored: no (malformed -> not written to the ledger)")
        return 1

    cr = ev["coordinator_reconfirmed"]
    print(f"task_id      : {ev['task_id']}   issued_for: {ev['issued_for']}")
    print(f"challenge_id : {ev['challenge_id']}")
    print(f"nonce        : {ev['nonce']}")
    print(f"output_hash  : {ev['output_hash']}")
    print(f"response_hash: {ev['response_hash']}")
    print("coordinator verification (task re-run + both hashes re-derived here):")
    print(f"  verdict              : {cr['verdict']}")
    if cr["first_failure_reason"]:
        print(f"  first_failure_reason : {cr['first_failure_reason']}")
    if ev.get("signed"):
        print(f"  signed               : True (actor {ev['signer_actor_id']!r}, "
              f"one-time key index {ev['key_index']}, signature_valid "
              f"{ev['signature_valid']}, root registered at ledger index "
              f"{ev['key_root_ledger_index']})")
    if entry is not None:
        print(f"anchored at ledger index: {entry['index']} (path: {ledger_path})")
    ok, vreason = ledger.verify_chain()
    print(f"chain verify: {'OK' if ok else 'FAIL'} — {vreason}")

    return 0 if status == _CHALLENGE_VERIFIED_STATUS else 1


def _cmd_register_actor_key(decl_path: str, ledger_path: str) -> int:
    """Load a public key declaration, validate (public-only), anchor the registration.

    Exit codes: 0 = actor-key-registered; non-zero = rejected.
    """
    print(BANNER)
    reason = None
    declaration = None
    try:
        with open(decl_path, "r", encoding="utf-8") as f:
            declaration = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
        reason = f"declaration file unreadable: {exc}"
    if reason is None and not isinstance(declaration, dict):
        reason = "declaration JSON is not an object"
    if reason is not None:
        print("status: rejected")
        print(f"reason: {reason}")
        print("anchored: no (nothing written to the ledger)")
        return 1

    ledger = Ledger(ledger_path)
    out = register_actor_key(declaration, ledger)
    ev = out["evaluation"]
    entry = out["ledger_entry"]
    print(f"status: {ev['status']}")
    if ev["status"] == "rejected":
        print(f"reason: {ev.get('reason')}")
        print("anchored: no (nothing written to the ledger)")
        return 1
    print(f"actor_id    : {ev['actor_id']}   ({ev['scheme']}, "
          f"{ev['key_count']} one-time keys)")
    print(f"merkle_root : {ev['merkle_root']}")
    print("NOTE: same-operator key custody — possession-continuity, not identity.")
    if entry is not None:
        print(f"anchored at ledger index: {entry['index']} (path: {ledger_path})")
    ok, vreason = ledger.verify_chain()
    print(f"chain verify: {'OK' if ok else 'FAIL'} — {vreason}")
    return 0


def _cmd_anchor_json(path: str, ledger_path: str, anchor_fn, expected_status):
    """Generic JSON-file anchoring command: load defensively, run `anchor_fn`,
    print the outcome payload. Exit 0 iff the record anchored (and matched
    `expected_status` when one is given)."""
    print(BANNER)
    try:
        with open(path, "r", encoding="utf-8") as f:
            doc = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
        print("status: rejected")
        print(f"reason: input file unreadable: {exc}")
        print("anchored: no (nothing written to the ledger)")
        return 1
    ledger = Ledger(ledger_path)
    out = anchor_fn(doc, ledger)
    ev = out["evaluation"]
    entry = out["ledger_entry"]
    print(f"status: {ev['status']}" + ("  (PLANNED DRILL)" if ev.get("drill")
                                       else ""))
    if entry is None:
        print(f"reason: {ev.get('reason')}")
        print("anchored: no (nothing written to the ledger)")
        return 1
    for key in ("funding_root", "total_fees_collected",
                "conservation_statement", "bounty_id", "task_id", "amount",
                "amount_returned", "amount_paid", "window_note",
                "process_statement", "treasury_totals", "actor_id",
                "slot_index", "epoch_hash", "verified_slots", "missed_slots",
                "total_emitted", "epoch_cap", "key_indices",
                "first_failure_reason", "missed_slot_statement",
                "two_flow_separation"):
        if key in ev:
            print(f"  {key}: {ev[key]}")
    if "bounded_failure" in ev:
        print(f"  bounded_failure: {ev['bounded_failure']['statement']}")
    if "precheck" in ev:
        for c in ev["precheck"]["checks"]:
            print(f"  precheck {c['name']}: {c['passed']} ({c['evidence']})")
    print(f"anchored at ledger index: {entry['index']} (path: {ledger_path})")
    ok, vreason = ledger.verify_chain()
    print(f"chain verify: {'OK' if ok else 'FAIL'} — {vreason}")
    if expected_status is not None and ev["status"] != expected_status:
        return 1
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="external_verifier.py",
        description=(
            "MetaCoin R3 external-verifier-pilot COORDINATOR (research-stage, ZERO-VALUE, "
            "no token). With NO arguments, runs the self-test on a TEMP ledger and does NOT "
            "touch the real ledger."
        ),
        epilog=(
            "Exit codes: 0 = externally-verified (or --genesis / self-test ok); non-zero = "
            "external-mismatch, rejected, genesis-refused, or self-test failure. HONEST "
            "LIMITATION: a matching hash proves REPRODUCIBILITY, not execution (a hash can be "
            "copied). Not consensus, not mainnet, not payment, not a token."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--genesis", action="store_true",
        help="append a neutral chain-start marker IFF the ledger is empty (refuses if already initialized)",
    )
    mode.add_argument(
        "--evaluate", metavar="SUBMISSION_JSON",
        help="evaluate an external-verifier submission file against the locally recomputed hash and anchor the outcome",
    )
    mode.add_argument(
        "--anchor-agent-result", metavar="AGENT_RESULT_JSON",
        help="ingest an agent_verifier_attestation; RE-DERIVE its chain/anchor/task claims on this host and anchor the outcome",
    )
    mode.add_argument(
        "--anchor-molecule-catalog", metavar="CATALOG_JSON",
        help="ingest a work-molecule catalog (work_molecule.py --catalog); REBUILD every "
             "molecule from the ledger, recompute the catalog hash, and anchor the outcome",
    )
    mode.add_argument(
        "--anchor-aci-report", metavar="ACI_REPORT_JSON",
        help="ingest an ACI report (agent_concentration.py --report); RECOMPUTE the full "
             "report from the ledger, compare report_hash, and anchor the outcome "
             "(aggregate numbers only)",
    )
    mode.add_argument(
        "--anchor-economy-summary", metavar="ECONOMY_LOG_JSON",
        help="ingest a 30-simulated-day economy log (economy_demo.py --run-all); RE-RUN "
             "the entire simulation, compare log hashes, and anchor the outcome "
             "(aggregates only)",
    )
    mode.add_argument(
        "--anchor-metering-report", metavar="METERING_REPORT_JSON",
        help="ingest a metering report (demo/task_metering.py --all); RE-METER every "
             "listed task for PLAUSIBILITY (not byte-reconfirmation — timing is "
             "non-deterministic) and anchor the outcome (aggregates + labels only; "
             "wall/CPU measured, energy ESTIMATED)",
    )
    mode.add_argument(
        "--anchor-cut-certificate", metavar="CUT_CERT_JSON",
        help="ingest a cut certificate (cut_certificate.py --build); FULLY VERIFY it "
             "(rebuild every interior molecule — the anchor-time full proof that "
             "makes later cheap acceptance sound) and anchor the outcome "
             "(counts only)",
    )
    mode.add_argument(
        "--anchor-trust-vector-catalog", metavar="TV_CATALOG_JSON",
        help="ingest a trust-vector catalog (trust_vector.py --all); REBUILD every "
             "six-component vector from the ledger, recompute the catalog hash, and "
             "anchor the outcome (counts only; no combined trust score exists by "
             "design)",
    )
    mode.add_argument(
        "--anchor-challenge-result", nargs=2,
        metavar=("CHALLENGE_JSON", "RESPONSE_JSON"),
        help="ingest a challenge + response (challenge_response.py); VERIFY by full "
             "recompute (task re-run, both hashes re-derived from the nonce) and "
             "anchor the outcome — verified AND failed rounds are both real audit "
             "events",
    )
    mode.add_argument(
        "--register-actor-key", metavar="DECLARATION_JSON",
        help="register an actor's PUBLIC one-time-signature key root "
             "(actor_identity.py --declare); rejects any file containing private "
             "material; one active root per actor in v0",
    )
    mode.add_argument(
        "--anchor-treasury-config", metavar="TREASURY_STATE_JSON",
        help="anchor the MetaStar Treasury constitution (metastar_treasury.py); "
             "the coordinator RE-DERIVES the fees from the anchored economy and "
             "rejects any state claiming units the economy never produced",
    )
    mode.add_argument(
        "--anchor-gate3-event", metavar="REQUEST_JSON",
        help="anchor one Gate-3 lifecycle event ({phase, bounty_id, ...}); the "
             "coordinator recomputes the machine pre-check, window arithmetic, "
             "scripted adjudication, and treasury bounds from ANCHORED records "
             "(use --drill for planned demonstrations)",
    )
    mode.add_argument(
        "--anchor-forged-heartbeat-drill", metavar="HEARTBEAT_JSON",
        help="anchor a demonstrated heartbeat REJECTION (the coordinator "
             "verifies it FAILS against the actor's anchored root; a valid "
             "heartbeat is refused as drill input)",
    )
    mode.add_argument(
        "--anchor-uptime-epoch", metavar="EPOCH_JSON",
        help="anchor a Flow-1 uptime epoch (flow1_uptime.py --run-epoch); the "
             "coordinator re-verifies every heartbeat signature and replays "
             "the objective emission arithmetic before anchoring",
    )
    mode.add_argument(
        "--anchor-passport-catalog", metavar="PASSPORT_CATALOG_JSON",
        help="anchor a MetaWork passport catalog (metawork_passport.py --all); "
             "the coordinator rediscovers every actor and rebuilds every "
             "passport before anchoring (counts + hash only; no leaderboard "
             "exists by mechanical rule)",
    )
    parser.add_argument(
        "--drill", action="store_true",
        help="with --anchor-challenge-result: label the anchored record as a "
             "PLANNED demonstration (e.g. the copy-attack or key-reuse drill), "
             "never detected fraud",
    )
    parser.add_argument(
        "--ledger", default=DEFAULT_LEDGER_PATH,
        help=f"ledger path (default: the REAL persistent ledger at {DEFAULT_LEDGER_PATH})",
    )
    parser.add_argument(
        "--snapshot", default=_DEFAULT_PUBLISHED_SNAPSHOT_PATH,
        help=f"published snapshot to re-verify for --anchor-agent-result (default {_DEFAULT_PUBLISHED_SNAPSHOT_PATH})",
    )
    parser.add_argument(
        "--anchor-file", default=audit.DEFAULT_ANCHOR_PATH,
        help=f"committed tip anchor to compare for --anchor-agent-result (default {audit.DEFAULT_ANCHOR_PATH})",
    )
    parser.add_argument(
        "--operator-relationship", default="same-operator",
        help="honest label for the verifier's relationship to this operator (default: same-operator)",
    )
    args = parser.parse_args(argv)

    if args.genesis:
        return _cmd_genesis(args.ledger)
    if args.evaluate is not None:
        return _cmd_evaluate(args.evaluate, args.ledger)
    if args.anchor_agent_result is not None:
        return _cmd_anchor_agent_result(
            args.anchor_agent_result, args.ledger, args.snapshot, args.anchor_file,
            args.operator_relationship,
        )
    if args.anchor_molecule_catalog is not None:
        return _cmd_anchor_molecule_catalog(args.anchor_molecule_catalog, args.ledger)
    if args.anchor_aci_report is not None:
        return _cmd_anchor_aci_report(args.anchor_aci_report, args.ledger)
    if args.anchor_economy_summary is not None:
        return _cmd_anchor_economy_summary(args.anchor_economy_summary, args.ledger)
    if args.anchor_metering_report is not None:
        return _cmd_anchor_metering_report(args.anchor_metering_report, args.ledger)
    if args.anchor_cut_certificate is not None:
        return _cmd_anchor_cut_certificate(args.anchor_cut_certificate, args.ledger)
    if args.anchor_trust_vector_catalog is not None:
        return _cmd_anchor_trust_vector_catalog(args.anchor_trust_vector_catalog,
                                                args.ledger)
    if args.anchor_challenge_result is not None:
        ch_path, resp_path = args.anchor_challenge_result
        return _cmd_anchor_challenge_result(ch_path, resp_path, args.ledger,
                                            args.drill)
    if args.register_actor_key is not None:
        return _cmd_register_actor_key(args.register_actor_key, args.ledger)
    if args.anchor_treasury_config is not None:
        return _cmd_anchor_json(args.anchor_treasury_config, args.ledger,
                                anchor_treasury_config, _TREASURY_STATUS)
    if args.anchor_gate3_event is not None:
        return _cmd_anchor_json(
            args.anchor_gate3_event, args.ledger,
            lambda doc, led: anchor_gate3_event(doc, led, drill=args.drill),
            None)
    if args.anchor_forged_heartbeat_drill is not None:
        return _cmd_anchor_json(args.anchor_forged_heartbeat_drill, args.ledger,
                                anchor_forged_heartbeat_drill,
                                _HEARTBEAT_REJECTED_STATUS)
    if args.anchor_uptime_epoch is not None:
        return _cmd_anchor_json(args.anchor_uptime_epoch, args.ledger,
                                anchor_uptime_epoch, _EPOCH_STATUS)
    if args.anchor_passport_catalog is not None:
        return _cmd_anchor_json(args.anchor_passport_catalog, args.ledger,
                                anchor_passport_catalog, _PASSPORT_STATUS)
    # No command -> run the self-test (temp ledger only; never touches the real ledger).
    return _selftest()


if __name__ == "__main__":
    sys.exit(main())
