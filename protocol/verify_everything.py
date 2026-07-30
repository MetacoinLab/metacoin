"""verify_everything.py — MetaCoin ONE-COMMAND FULL-STACK VERIFIER.

================== PRODUCT GOAL (READ ME) ==================
Any stranger clones the public repo and runs ONE command that mechanically
re-verifies every layer — chain, tasks, molecules (both generations),
concentration, economy, metering claims, cut certificate, trust vectors,
challenge-response records, actor-identity root chains (registrations +
rotations, with historical signatures checked as-of their contemporaneous
roots) — with ZERO local-only inputs and ZERO LLM judgment:

    python3 protocol/verify_everything.py --full

Everything needed ships in the clone: the published ledger snapshot
(protocol/ledger_published.json), the committed tip anchor
(protocol/ledger_anchor.json), and the privacy-checked evidence bundle
(protocol/evidence/). The live runtime ledger is NOT required (it is used
automatically when present, i.e. on the coordinator's machine).

Two modes, honestly labeled per layer:
  --full  : every layer is RE-DERIVED from scratch and compared to its anchored
            ledger record — lines are labeled VERIFIED-FULL (metering is the one
            honest exception: timing is non-reproducible, so that layer is a
            CLAIM-CHECK — the shipped report must hash-match the anchored claim,
            its arithmetic and labels must be exact, and its output hashes must
            match fresh re-runs; the timings themselves cannot be re-derived).
  --quick : bounded-cost acceptance — anchored-hash lookups over the shipped
            evidence artifacts plus the cut certificate's one-molecule
            retrievability probe. Lines are labeled ACCEPTED-BY-ANCHOR and quick
            is NEVER presented as proof: it is conditional on the anchors and on
            continued retrievability, exactly like accept_by_anchor.

Exit code 0 only if every layer passes. The final block prints the honest
boundary: everything verified here is SAME-OPERATOR, zero-value evidence; a pass
proves deterministic re-derivability of the recorded claims, NOT independence,
NOT usefulness, NOT value.

Standard library only. Every layer REUSES the existing verified component —
nothing is reimplemented. Not legal, financial, investment, or
security-certification advice.

Usage:
    python3 protocol/verify_everything.py --full
    python3 protocol/verify_everything.py --quick
    python3 protocol/verify_everything.py --selftest   # temp-only
"""

# Suppress __pycache__/*.pyc so importing protocol modules below leaves no stray files.
import sys
sys.dont_write_bytecode = True

import argparse
import hashlib
import json
import os

# Make `from protocol...` resolve when run directly (repo root on path).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# REUSE every existing verified component — this module only orchestrates.
import protocol.actor_identity as actor_identity
import protocol.audit as audit
import protocol.agent_concentration as agent_concentration
import protocol.challenge_response as challenge_response
import protocol.cut_certificate as cut_certificate
import protocol.gate3_process as gate3_process
import protocol.metawork_passport as metawork_passport
import protocol.trust_vector as trust_vector
import demo.flow1_uptime as flow1_uptime
import demo.metastar_treasury as metastar_treasury
import protocol.verifier_cli as verifier_cli
import protocol.work_molecule as work_molecule
import demo.economy_demo as economy_demo
import demo.task_metering as task_metering

_PROTO_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_LIVE_LEDGER = os.path.join(_PROTO_DIR, "ledger_data.jsonl")
DEFAULT_SNAPSHOT = os.path.join(_PROTO_DIR, "ledger_published.json")
DEFAULT_ANCHOR = os.path.join(_PROTO_DIR, "ledger_anchor.json")

FULL = "VERIFIED-FULL"
CLAIM = "CLAIM-CHECK"
ANCHORED = "ACCEPTED-BY-ANCHOR"

HONEST_BOUNDARY = """\
------------------------------------------------------------------ honest boundary
Everything above is SAME-OPERATOR, zero-value, research-stage evidence. A full
pass establishes that every anchored claim RE-DERIVES deterministically from the
shipped evidence on YOUR machine. It does NOT establish: independent multi-party
verification (the anchored ACI baseline quantifies maximal same-operator
concentration), usefulness of the work (Gate 3 is not implemented; every trust
vector says 'not-assessed'), hardware-rooted execution proof (no TEE — open
debt), measured energy (estimates from an assumed power figure), or any monetary
value (no token exists). ACCEPTED-BY-ANCHOR lines are bounded-cost acceptance
conditional on the committed anchors — not re-proof. Not consensus, not payment,
not investment advice."""


def _canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _find_task_hash(entries, task_id):
    """First recorded output hash for task_id (the agent_verifier convention)."""
    for e in entries:
        p = e.get("payload", {})
        if p.get("task_id") == task_id:
            for key in ("local_output_hash", "output_hash", "submitted_output_hash"):
                if isinstance(p.get(key), str):
                    return p[key]
    return None


def _find_anchor_payload(entries, event, status):
    """(index, payload) of the highest-index entry with event+status, else (None, None)."""
    found = (None, None)
    for e in entries:
        p = e.get("payload") if isinstance(e, dict) else None
        if isinstance(p, dict) and p.get("event") == event and p.get("status") == status:
            found = (e["index"], p)
    return found


def _sha256_excluding(doc: dict, field: str) -> str:
    content = {k: v for k, v in doc.items() if k != field}
    return hashlib.sha256(_canonical(content).encode("utf-8")).hexdigest()


def _load_evidence_json(basename: str):
    """Load an evidence artifact via the standard discovery (repo root -> bundle)."""
    path = work_molecule.find_evidence_file(basename)
    if path is None:
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


# ----------------------------------------------------------------------------
# The layered verification (each layer returns (label, ok, detail))
# ----------------------------------------------------------------------------
def run_verification(full: bool, snapshot_path: str = DEFAULT_SNAPSHOT,
                     anchor_path: str = DEFAULT_ANCHOR,
                     live_ledger_path: str = DEFAULT_LIVE_LEDGER) -> tuple:
    """Run every layer. Returns (all_ok, result_rows, source_note).

    result_rows: [(layer_name, mode_label, ok, detail_str), ...]. The ledger
    SOURCE is the live ledger when present (coordinator machine), else the
    published snapshot (fresh clone) — entry content is identical either way,
    which layer 1 confirms whenever both exist.
    """
    rows = []

    # --- layer 1: chain + anchor (always fully verified — it is cheap) ------------
    snap_ok, snap_reason, snap_details = audit.verify_snapshot_file(snapshot_path)
    anchor_ok = False
    anchor_detail = "anchor file unreadable"
    try:
        with open(anchor_path, "r", encoding="utf-8") as f:
            anchor = json.load(f)
        anchor_ok = (snap_ok
                     and anchor.get("tip_hash") == snap_details.get("tip_hash")
                     and anchor.get("entry_count") == snap_details.get("entry_count"))
        anchor_detail = (f"tip {str(anchor.get('tip_hash'))[:12]}.. "
                         f"({anchor.get('entry_count')} entries)")
    except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
        anchor_detail = f"anchor file unreadable: {exc}"

    have_live = os.path.exists(live_ledger_path)
    live_ok = True
    live_note = "no live ledger (fresh clone) — published snapshot is the source"
    if have_live:
        from protocol.ledger import Ledger
        live_ok, live_reason = Ledger(live_ledger_path).verify_chain()
        live_entries = work_molecule._read_ledger(live_ledger_path)
        live_tip = live_entries[-1]["hash"] if live_entries else None
        live_ok = bool(live_ok and snap_ok
                       and live_tip == snap_details.get("tip_hash"))
        live_note = ("live ledger verifies and its tip matches the snapshot"
                     if live_ok else
                     f"live ledger problem: {live_reason}; or tip != snapshot tip")
    chain_ok = bool(snap_ok and anchor_ok and live_ok)
    rows.append(("chain+anchor", FULL, chain_ok,
                 f"{snap_reason}; anchor: {anchor_detail}; {live_note}"))
    if not snap_ok:
        return (False, rows, "aborted: the published snapshot does not verify")

    source = live_ledger_path if have_live else snapshot_path
    source_note = ("source: live ledger (coordinator machine)" if have_live
                   else "source: published snapshot (fresh clone)")
    entries = work_molecule._read_ledger(source)

    # anchored records every later layer compares against
    idx17_i, idx17 = _find_anchor_payload(entries, "work_molecule_catalog_anchored",
                                          "molecule-catalog-confirmed")
    idx18_i, idx18 = _find_anchor_payload(entries, "aci_baseline_anchored",
                                          "aci-baseline-confirmed")
    idx19_i, idx19 = _find_anchor_payload(entries, "economy_demo_summary_anchored",
                                          "economy-demo-confirmed")
    idx20_i, idx20 = _find_anchor_payload(entries, "metering_evidence_anchored",
                                          "metering-evidence-confirmed")
    idx22_i, idx22 = _find_anchor_payload(entries, "cut_certificate_anchored",
                                          "cut-certificate-confirmed")
    idx23_i, idx23 = _find_anchor_payload(entries, "trust_vector_catalog_anchored",
                                          "trust-vector-catalog-confirmed")
    # idx17 helper found the HIGHEST catalog anchor; split by generation instead:
    gen1 = gen2 = None
    gen1_i = gen2_i = None
    for e in entries:
        p = e.get("payload", {})
        if (p.get("event") == "work_molecule_catalog_anchored"
                and p.get("status") == "molecule-catalog-confirmed"):
            if p.get("molecule_schema") == work_molecule.SCHEMA_VERSION_02:
                gen1_i, gen1 = e["index"], p
            elif p.get("molecule_schema") == work_molecule.SCHEMA_VERSION_03:
                gen2_i, gen2 = e["index"], p

    # --- layer 2: the thirteen tasks ---------------------------------------------
    task_ids = sorted(verifier_cli.TASK_MODULES)
    canonical = {}
    if full:
        bad = []
        for tid in task_ids:
            module = verifier_cli.load_task(tid)
            local = module.output_hash(module.compute())
            canonical[tid] = local
            recorded = _find_task_hash(entries, tid)
            if recorded != local:
                bad.append(tid)
        rows.append(("tasks (13 re-run)", FULL, not bad,
                     "all 13 canonical hashes re-derived and match the ledger"
                     if not bad else f"hash mismatch: {bad}"))
    else:
        probe = "task-0002"
        module = verifier_cli.load_task(probe)
        local = module.output_hash(module.compute())
        ok = _find_task_hash(entries, probe) == local
        rows.append(("tasks (1 probe)", FULL, ok,
                     f"probe {probe} re-run matches the ledger (12 others not "
                     "re-run in --quick)"))

    # --- layer 3: molecule catalogs, both generations ------------------------------
    if gen1 is None or gen2 is None:
        rows.append(("molecules", FULL, False, "missing anchored catalog(s)"))
    elif full:
        # GENERATION-LOCKED rebuilds (as_of = anchor index - 1): an anchored
        # catalog re-derives against the chain state it was built over, while
        # unbounded rebuilds legitimately absorb later task-referencing events
        # (challenge records) — those get NEW generations, never rewrites.
        cat02 = work_molecule.build_catalog(
            ledger_path=source, schema_version=work_molecule.SCHEMA_VERSION_02,
            as_of_index=gen1_i - 1)
        cat03 = work_molecule.build_catalog(ledger_path=source,
                                            as_of_index=gen2_i - 1)
        ok02 = (cat02["catalog_hash"] == gen1["catalog_hash"]
                and cat02["entries"] == gen1["catalog_entries"])
        ok03 = (cat03["catalog_hash"] == gen2["catalog_hash"]
                and cat03["entries"] == gen2["catalog_entries"])
        rows.append(("molecules 0.2", FULL, ok02,
                     f"13 WMIDs rebuilt == anchored idx-{gen1_i} catalog "
                     "(generation-locked)"))
        rows.append(("molecules 0.3", FULL, ok03,
                     f"13 WMIDs rebuilt == anchored idx-{gen2_i} catalog "
                     "(generation-locked)"))
    else:
        ok02 = ok03 = False
        f02 = _load_evidence_json("wm_catalog.json")
        f03 = _load_evidence_json("wm_catalog_v03.json")
        if isinstance(f02, dict):
            ok02 = (_sha256_excluding(f02, "catalog_hash") == f02.get("catalog_hash")
                    == gen1["catalog_hash"])
        if isinstance(f03, dict):
            ok03 = (_sha256_excluding(f03, "catalog_hash") == f03.get("catalog_hash")
                    == gen2["catalog_hash"])
        rows.append(("molecules 0.2", ANCHORED, ok02,
                     f"shipped catalog hash-matches anchored idx-{gen1_i} "
                     "(no rebuild)"))
        rows.append(("molecules 0.3", ANCHORED, ok03,
                     f"shipped catalog hash-matches anchored idx-{gen2_i} "
                     "(no rebuild)"))

    # --- layer 4: concentration (ACI: pairwise + higher-order k-order profile) ------
    # The pairwise baseline re-derives generation-locked as before. When a
    # k-order baseline is anchored, its EXACT-enumeration profile re-derives at
    # its recorded as-of chain point AND the S_2 consistency is re-proved LIVE:
    # ACI_2 via the S_k machinery must equal the anchored pairwise ACI to the
    # digit. (The pairwise blind-spot fixture is exercised in --selftest, not
    # here — --full re-derives anchored claims; fixtures are test material.)
    idxko_i, idxko = _find_anchor_payload(entries, "aci_korder_baseline_anchored",
                                          "aci-korder-confirmed")
    if idx18 is None:
        rows.append(("concentration", FULL, False, "no anchored ACI baseline"))
    elif full:
        report = agent_concentration.compute_report(
            agent_concentration.build_paths(ledger_path=source,
                                            as_of_index=idx18_i - 1))
        ok = (report["report_hash"] == idx18["report_hash"]
              and report["path_count"] == idx18["path_count"])
        ko_note = "no k-order baseline anchored yet"
        if idxko is not None:
            ko_ks = list(idxko.get("k_values_computed", [])) + \
                list(idxko.get("k_values_refused", []))
            ko = agent_concentration.compute_korder_report(
                agent_concentration.build_paths(
                    ledger_path=source,
                    as_of_index=idxko.get("as_of_ledger_index")),
                k_max=max(ko_ks),
                as_of_ledger_index=idxko.get("as_of_ledger_index"))
            aci2 = next((r["aci_k"] for r in ko["profile"] if r["k"] == 2),
                        None)
            ok = (ok and ko["report_hash"] == idxko.get("report_hash")
                  and [dict(r) for r in ko["profile"]] == idxko.get("profile")
                  and aci2 == idx18.get("pairwise_aci"))
            ko_note = (f"k-order profile {sorted(r['k'] for r in ko['profile'])} "
                       f"re-enumerated == anchored idx-{idxko_i}; ACI_2 == "
                       "pairwise to the digit")
        rows.append(("concentration", FULL, ok,
                     f"ACI re-measured (generation-locked): hash == anchored "
                     f"idx-{idx18_i}, {report['path_count']} paths, "
                     f"pairwise {report['pairwise_aci']:.5f} (same-operator); "
                     f"{ko_note}"))
    else:
        f = _load_evidence_json("aci_report.json")
        ok = (isinstance(f, dict)
              and agent_concentration.compute_report_hash(f) == f.get("report_hash")
              == idx18["report_hash"])
        ko_note = "no k-order baseline anchored yet"
        if idxko is not None:
            fk = _load_evidence_json("aci_korder_report.json")
            ok = (ok and isinstance(fk, dict)
                  and agent_concentration.compute_report_hash(fk)
                  == fk.get("report_hash") == idxko.get("report_hash"))
            ko_note = f"k-order report hash-matches anchored idx-{idxko_i}"
        rows.append(("concentration", ANCHORED, ok,
                     f"shipped report hash-matches anchored idx-{idx18_i} "
                     f"(no re-measurement); {ko_note}"))

    # --- layer 5: simulated economy -------------------------------------------------
    if idx19 is None:
        rows.append(("economy", FULL, False, "no anchored economy summary"))
    elif full:
        log = economy_demo.simulate_all()
        ok = log["economy_log_hash"] == idx19["economy_log_hash"]
        rows.append(("economy", FULL, ok,
                     f"30-simulated-day economy re-run: log hash == anchored "
                     f"idx-{idx19_i}"))
    else:
        f = _load_evidence_json("economy_log.json")
        ok = (isinstance(f, dict)
              and economy_demo.compute_log_hash(f) == f.get("economy_log_hash")
              == idx19["economy_log_hash"])
        rows.append(("economy", ANCHORED, ok,
                     f"shipped log hash-matches anchored idx-{idx19_i} (no re-run)"))

    # --- layer 6: metering claims (CLAIM-CHECK by nature: timing is not
    #     byte-reproducible; what is checkable is the anchored claim's integrity,
    #     its exact arithmetic/labels, and that its output hashes match re-runs) ----
    if idx20 is None:
        rows.append(("metering", CLAIM, False, "no anchored metering evidence"))
    else:
        f = _load_evidence_json("metering_report.json")
        problems = []
        if not isinstance(f, dict):
            problems.append("shipped metering_report.json missing/unreadable")
        else:
            if not (task_metering.compute_report_hash(f) == f.get("report_hash")
                    == idx20["report_hash"]):
                problems.append("report_hash != anchored claim")
            for row in f.get("per_task", []):
                if row.get("labels") != task_metering.LABELS:
                    problems.append(f"labels wrong for {row.get('task_id')}")
                if row.get("energy_j_estimate") != round(
                        row.get("cpu_time_s", 0) * f.get("assumed_cpu_power_w", 0), 6):
                    problems.append(f"energy arithmetic wrong for {row.get('task_id')}")
                expected = (canonical.get(row.get("task_id"))
                            if full else _find_task_hash(entries, row.get("task_id")))
                if row.get("output_hash") != expected:
                    problems.append(f"output_hash wrong for {row.get('task_id')}")
        rows.append(("metering", CLAIM, not problems,
                     f"anchored idx-{idx20_i} claim intact: hashes match "
                     f"{'re-runs' if full else 'ledger'}, labels honest "
                     "(energy=estimated), arithmetic exact — timings themselves "
                     "are claims, not re-derivable"
                     if not problems else "; ".join(problems[:3])))

    # --- layer 7: cut certificate ----------------------------------------------------
    if idx22 is None:
        rows.append(("cut certificate", FULL, False, "no anchored cut certificate"))
    elif full:
        cert = cut_certificate.build_cut(task_ids, ledger_path=source,
                                         as_of_index=idx22_i - 1)
        hash_ok = cert["certificate_hash"] == idx22["certificate_hash"]
        v_ok, _v_reasons = cut_certificate.verify_full(cert, ledger_path=source,
                                                       as_of_index=idx22_i - 1)
        rows.append(("cut certificate", FULL, hash_ok and v_ok,
                     f"rebuilt cut == anchored idx-{idx22_i}; full verification "
                     "re-proved (13 interior molecules, generation-locked)"))
    else:
        cert = _load_evidence_json("cut_cert.json")
        if cert is None:
            rows.append(("cut certificate", ANCHORED, False,
                         "shipped cut_cert.json missing/unreadable"))
        else:
            accepted, note = cut_certificate.accept_by_anchor(cert,
                                                              ledger_path=source)
            rows.append(("cut certificate", ANCHORED, accepted,
                         note.split(". ")[0] if accepted else note))

    # --- layer 8: trust vectors ------------------------------------------------------
    if idx23 is None:
        rows.append(("trust vectors", FULL, False,
                     "no anchored trust-vector catalog"))
    elif full:
        tv = trust_vector.build_tv_catalog(ledger_path=source,
                                           as_of_index=idx23_i - 1)
        ok = (tv["catalog_hash"] == idx23["catalog_hash"]
              and len(tv["vector_entries"]) == idx23["vector_count"])
        rows.append(("trust vectors", FULL, ok,
                     f"13 six-component vectors rebuilt == anchored idx-{idx23_i} "
                     "(generation-locked; no combined scalar exists, by design)"))
    else:
        f = _load_evidence_json("tv_catalog.json")
        ok = (isinstance(f, dict)
              and trust_vector.compute_catalog_hash(f) == f.get("catalog_hash")
              == idx23["catalog_hash"])
        rows.append(("trust vectors", ANCHORED, ok,
                     f"shipped catalog hash-matches anchored idx-{idx23_i} "
                     "(no rebuild)"))

    # --- layer 9: challenge-response records (both modes; cheap) --------------------
    # Each anchored round is re-derived: the task is re-run HERE and the expected
    # response_hash recomputed from the anchored nonce. Verified rounds must
    # re-derive; failed rounds (planned drills) must stay refuted.
    ch_records = [(e["index"], e["payload"]) for e in entries
                  if isinstance(e.get("payload"), dict)
                  and e["payload"].get("event") == "challenge_response_result"
                  and e["payload"].get("status") in ("challenge-verified",
                                                     "challenge-failed")]
    if not ch_records:
        rows.append(("challenges", FULL, True,
                     "no challenge records on the chain yet (nothing to re-verify)"))
    else:
        problems = []
        n_verified = n_failed = 0
        for idx, p in ch_records:
            module = verifier_cli.load_task(p["task_id"])
            result = module.compute()
            expected = challenge_response.compute_response_hash(
                p["nonce"], module.canonical_json(result))
            own_hash = module.output_hash(result)
            if p["status"] == "challenge-verified":
                n_verified += 1
                if p["response_hash"] != expected or p["output_hash"] != own_hash:
                    problems.append(f"idx {idx}: verified round does not re-derive")
            else:
                n_failed += 1
                if p["response_hash"] == expected:
                    # possession re-derives — only legitimate when the rejection
                    # was at the SIGNATURE level (e.g. the key-reuse drill:
                    # honest possession, violated one-time discipline)
                    reason = str(p.get("coordinator_reconfirmed", {})
                                 .get("first_failure_reason"))
                    if not (p.get("signed") and ("reuse" in reason
                                                 or "signature" in reason)):
                        problems.append(f"idx {idx}: failed round actually "
                                        "re-derives — its rejection was wrong")
        rows.append(("challenges", FULL, not problems,
                     f"{n_verified} verified round(s) re-derive under their "
                     f"nonces; {n_failed} failed round(s) (planned drills) stay "
                     "refuted — possession proof holds"
                     if not problems else "; ".join(problems[:3])))

    # --- layer 10: actor identity (root chains: registrations + rotations +
    #     signed rounds) -----------------------------------------------------------
    # Verification needs ONLY public material: the anchored roots, the rotation
    # certificate evidence copies, and the signed challenge/response evidence
    # copies (a signature carries its own leaf pubkey + Merkle path). The
    # private keychains are never needed — asserted by the fresh-clone test,
    # which has no keychain file at all. Root CHAINS are walked per actor:
    # every anchored rotation must extend the linear chain and its certificate
    # must still fully verify as-of anchor time; every signed record verifies
    # against the root active AS-OF its own index (rotation retires roots for
    # FUTURE signing only — no historical signature may break); the
    # forged-rotation drill must STAY rejected for its consumed-index reason.
    reg_records = [(e["index"], e["payload"]) for e in entries
                   if isinstance(e.get("payload"), dict)
                   and e["payload"].get("event") == "actor_key_registered"
                   and e["payload"].get("status") == "actor-key-registered"]
    rot_records = [(e["index"], e["payload"]) for e in entries
                   if isinstance(e.get("payload"), dict)
                   and e["payload"].get("event") == "actor_key_rotated"
                   and e["payload"].get("status") == "actor-key-rotated"]
    rot_drills = [(e["index"], e["payload"]) for e in entries
                  if isinstance(e.get("payload"), dict)
                  and e["payload"].get("event") == "actor_key_rotation_rejected"
                  and e["payload"].get("status") == "actor-key-rotation-rejected"]
    signed_records = [(i, p) for i, p in ch_records if p.get("signed")]
    if not reg_records and not signed_records:
        rows.append(("identity", FULL, True,
                     "no actor-key registrations on the chain yet"))
    else:
        problems = []
        roots = {}
        for idx, p in reg_records:
            root = p.get("merkle_root")
            if not (isinstance(root, str) and len(root) == 64):
                problems.append(f"idx {idx}: malformed registered root")
            roots[idx] = root
        for idx, p in rot_records:
            roots[idx] = p.get("new_root")
            prev = actor_identity.active_root_asof(p.get("actor_id"), entries,
                                                  as_of_index=idx - 1)
            if (prev is None or prev["merkle_root"] != p.get("prev_root")
                    or prev["ledger_index"] != p.get("prev_root_ledger_index")):
                problems.append(f"idx {idx}: rotation does not extend the "
                                "actor's linear root chain")
                continue
            cert = _load_evidence_json(
                f"rotation_cert_{str(p.get('new_root'))[:12]}.json")
            if cert is None:
                problems.append(f"idx {idx}: rotation certificate evidence "
                                "file missing")
                continue
            cert_ok, _cr = actor_identity.verify_rotation_certificate(
                cert, entries, as_of_index=idx - 1)
            if (not cert_ok or cert.get("prev_root") != p.get("prev_root")
                    or cert.get("new_root") != p.get("new_root")
                    or cert.get("key_index") != p.get("key_index")):
                problems.append(f"idx {idx}: anchored rotation certificate "
                                "does not re-verify as-of anchor time")
        for idx, p in rot_drills:
            cert = _load_evidence_json("rotation_forged_drill.json")
            if cert is None:
                problems.append(f"idx {idx}: forged-rotation drill evidence "
                                "file missing")
                continue
            still_bad, dr = actor_identity.verify_rotation_certificate(
                cert, entries, as_of_index=idx - 1)
            if still_bad or not any("reuse" in r for r in dr):
                problems.append(f"idx {idx}: forged rotation no longer "
                                "rejects for its consumed-index violation")
        n_ok = 0
        for idx, p in signed_records:
            cid12 = p["challenge_id"][:12]
            ch = _load_evidence_json(f"challenge_{cid12}.json")
            resp = _load_evidence_json(f"response_{cid12}.json")
            if ch is None or resp is None:
                problems.append(f"idx {idx}: signed-round evidence files missing")
                continue
            if (ch.get("challenge_id") != p["challenge_id"]
                    or resp.get("response_hash") != p["response_hash"]):
                problems.append(f"idx {idx}: evidence files do not match the "
                                "anchored record")
                continue
            # historical re-verification: scan exactly the history the
            # coordinator saw at anchor time (generation-lock idiom)
            verdict, _reasons = challenge_response.verify_response(
                ch, resp, ledger_path=source, as_of_index=idx - 1)
            expected_ok = p["status"] == "challenge-verified"
            if verdict != expected_ok:
                problems.append(f"idx {idx}: re-verification verdict {verdict} "
                                f"contradicts anchored status {p['status']}")
                continue
            sig = resp.get("signature", {})
            reg_idx = p.get("key_root_ledger_index")
            if roots.get(reg_idx) != sig.get("merkle_root"):
                problems.append(f"idx {idx}: signature root does not match the "
                                f"cited registration/rotation at idx {reg_idx}")
                continue
            n_ok += 1
        rows.append(("identity", FULL, not problems,
                     f"{len(reg_records)} key root(s) + {len(rot_records)} "
                     f"rotation(s) walk linear chains; {n_ok} signed round(s) "
                     "re-verified as-of their contemporaneous roots from "
                     "public material only (reuse + forged-rotation drills "
                     "stay rejected)" if not problems
                     else "; ".join(problems[:3])))

    # --- layer 11: Two-Flow treasury + Gate-3 process (both modes; cheap) -----------
    # Conservation and every lifecycle precondition are re-derived from ANCHORED
    # records + the anchored economy: fees recompute, prechecks recompute with
    # their citations, window arithmetic replays, the bounded-failure drill
    # stays upheld, and balance + outstanding == fees exactly.
    treas = [(e["index"], e["payload"]) for e in entries
             if isinstance(e.get("payload"), dict)
             and e["payload"].get("event") == "treasury_config_anchored"
             and e["payload"].get("status") == "treasury-config-confirmed"]
    g3_records = [(e["index"], e["payload"]) for e in entries
                  if isinstance(e.get("payload"), dict)
                  and e["payload"].get("event") in gate3_process.LIFECYCLE_EVENTS
                  and str(e["payload"].get("status", "")).startswith("gate3-")]
    if not treas and not g3_records:
        rows.append(("treasury+gate3", FULL, True,
                     "no treasury/Gate-3 records on the chain yet"))
    else:
        problems = []
        fees = 0.0
        for idx, p in treas:
            try:
                root, _pd, total = metastar_treasury.derive_fees(source)
            except ValueError as exc:
                problems.append(f"idx {idx}: fee re-derivation failed: {exc}")
                continue
            if p.get("funding_root") != root or p.get(
                    "total_fees_collected") != total:
                problems.append(f"idx {idx}: anchored fees do not re-derive "
                                f"from the anchored economy ({total})")
            fees = total
        granted = clawed = 0.0
        n_final = n_claw = 0
        for idx, p in g3_records:
            phase = p["event"]
            if phase == gate3_process.EVENT_PROVISIONAL:
                granted = round(granted + p["amount"], 6)
                pre = gate3_process.submit_work_item(
                    {"task_id": p["task_id"], "work_id": p["work_id"],
                     "taxonomy_tag": p["taxonomy_tag"]}, ledger_path=source)
                if not pre["passed"]:
                    problems.append(f"idx {idx}: anchored pre-check no longer "
                                    "recomputes as passed")
            elif phase == gate3_process.EVENT_CHALLENGE:
                if not gate3_process.challenge_in_window(
                        p["provisional_ledger_index"], idx):
                    problems.append(f"idx {idx}: anchored challenge was "
                                    "out-of-window")
            elif phase == gate3_process.EVENT_CLAWBACK:
                n_claw += 1
                clawed = round(clawed + p["amount_returned"], 6)
                if (gate3_process.adjudicate(p)["verdict"] != "upheld"
                        or "never the base" not in
                        p.get("bounded_failure", {}).get("statement", "")):
                    problems.append(f"idx {idx}: bounded-failure clawback does "
                                    "not replay")
            elif phase == gate3_process.EVENT_FINALIZATION:
                n_final += 1
                closed, _note = gate3_process.window_closed(
                    p["provisional_ledger_index"], entries, p["bounty_id"])
                if not closed:
                    problems.append(f"idx {idx}: finalization window does not "
                                    "replay as closed")
                stated = p.get("treasury_totals", {}).get("balance")
                if stated != round(fees - (granted - clawed), 6):
                    problems.append(f"idx {idx}: stated balance {stated} breaks "
                                    "conservation against anchored flows")
        rows.append(("treasury+gate3", FULL, not problems,
                     f"conservation holds: fees {fees} − outstanding "
                     f"{round(granted - clawed, 6)} == balance "
                     f"{round(fees - (granted - clawed), 6)}; {n_claw} "
                     "bounded-failure drill(s) stay upheld, "
                     f"{n_final} finalization(s) replay with closed windows; "
                     "prechecks recompute with citations"
                     if not problems else "; ".join(problems[:3])))

    # --- layer 12: Flow-1 uptime emission (both modes; ~9 signature verifies) -------
    # Root integrity via the identity layer's registrations; every anchored
    # epoch's heartbeat signatures re-verified from the shipped evidence copy
    # (public material only); emission arithmetic replayed under the objective
    # rule; the missed-slot zero confirmed; the forged drill stays rejected;
    # the two-flow separation statements present on the record.
    ep_records = [(e["index"], e["payload"]) for e in entries
                  if isinstance(e.get("payload"), dict)
                  and e["payload"].get("event") == "uptime_epoch_anchored"
                  and e["payload"].get("status") == "uptime-epoch-confirmed"]
    hb_drills = [(e["index"], e["payload"]) for e in entries
                 if isinstance(e.get("payload"), dict)
                 and e["payload"].get("event") == "heartbeat_rejected"
                 and e["payload"].get("status") == "heartbeat-forged-rejected"]
    if not ep_records and not hb_drills:
        rows.append(("flow1 emission", FULL, True,
                     "no uptime-emission records on the chain yet"))
    else:
        problems = []
        reg_roots = {e["index"]: e["payload"].get("merkle_root")
                     for e in entries
                     if isinstance(e.get("payload"), dict)
                     and e["payload"].get("event") == "actor_key_registered"}
        n_hb = 0
        for idx, p in ep_records:
            f = _load_evidence_json(f"uptime_epoch_{p['epoch_hash'][:12]}.json")
            if not isinstance(f, dict) or f.get("epoch_hash") != p["epoch_hash"]:
                problems.append(f"idx {idx}: epoch evidence file missing or "
                                "hash-mismatched")
                continue
            root = reg_roots.get(p.get("key_root_ledger_index"))
            ok, _reasons = flow1_uptime.verify_epoch(f, root,
                                                     ledger_path=source,
                                                     as_of_index=idx - 1)
            if not ok:
                problems.append(f"idx {idx}: anchored epoch does not re-verify")
                continue
            s = f["summary"]
            if (p["total_emitted"] != round(
                    s["verified_slots"] * f["per_slot_emission"], 6)
                    or p["total_emitted"] > p["epoch_cap"]
                    or any(row["emitted"] != 0.0 for row in f["emission_table"]
                           if row["slot"] in s["missed_slots"])):
                problems.append(f"idx {idx}: emission arithmetic violates the "
                                "objective rule")
            if ("no discretion" not in p.get("missed_slot_statement", "")
                    or "both directions" not in p.get("two_flow_separation", "")):
                problems.append(f"idx {idx}: constitutional statements missing")
            n_hb += len(f.get("heartbeats", []))
        for idx, p in hb_drills:
            f = _load_evidence_json("heartbeat_forged_drill.json")
            if not isinstance(f, dict):
                problems.append(f"idx {idx}: forged-drill evidence file missing")
                continue
            root = reg_roots.get(p.get("key_root_ledger_index"))
            still_fails, _r = flow1_uptime.verify_heartbeat(
                f, root, ledger_path=source, as_of_index=idx - 1)
            if still_fails or p.get("emitted") != 0.0:
                problems.append(f"idx {idx}: forged heartbeat no longer rejects "
                                "or emitted nonzero")
        rows.append(("flow1 emission", FULL, not problems,
                     f"{len(ep_records)} epoch(s): {n_hb} heartbeat signatures "
                     "re-verified, objective arithmetic replayed, missed-slot "
                     f"zero confirmed; {len(hb_drills)} forged drill(s) stay "
                     "rejected; separation statements present"
                     if not problems else "; ".join(problems[:3])))

    # --- layer 13: MetaWork passports (both modes; full catalog rebuild) ------------
    # The anchored catalog re-derives generation-locked; one rich passport is
    # citation-rechecked against the chain; the no-leaderboard rule is scanned
    # LIVE on the rebuilt passport (a history, never a leaderboard).
    pp_records = [(e["index"], e["payload"]) for e in entries
                  if isinstance(e.get("payload"), dict)
                  and e["payload"].get("event") == "passport_catalog_anchored"
                  and e["payload"].get("status") == "passport-catalog-confirmed"]
    if not pp_records:
        rows.append(("passports", FULL, True,
                     "no passport catalog on the chain yet"))
    else:
        problems = []
        for idx, p in pp_records[-1:]:
            try:
                cat = metawork_passport.build_passport_catalog(
                    ledger_path=source, as_of_index=idx - 1)
            except (KeyError, ValueError) as exc:
                problems.append(f"idx {idx}: catalog rebuild failed: {exc}")
                continue
            if (cat["catalog_hash"] != p["catalog_hash"]
                    or len(cat["entries"]) != p["actor_count"]):
                problems.append(f"idx {idx}: rebuilt catalog does not match "
                                "the anchored hash/count")
                continue
            sample_actor = cat["entries"][-1]["actor_id"]
            sample = metawork_passport.build_passport(
                sample_actor, ledger_path=source, as_of_index=idx - 1)
            ok, _reasons = metawork_passport.validate_passport(
                sample, ledger_path=source)
            if not ok:
                problems.append(f"idx {idx}: sample passport fails citation "
                                "recheck")
            if metawork_passport.leaderboard_violations(sample):
                problems.append(f"idx {idx}: no-leaderboard rule violated live")
            if ("mechanical rule" not in p.get("no_leaderboard", "")
                    or "mechanically blind" not in p.get("uww_transparency", "")):
                problems.append(f"idx {idx}: constitutional affirmations missing")
        rows.append(("passports", FULL, not problems,
                     f"catalog re-derived ({pp_records[-1][1]['actor_count']} "
                     "actors, generation-locked); sample passport citation-"
                     "rechecked; no-leaderboard rule scanned live; UWW stays "
                     "transparency-only" if not problems
                     else "; ".join(problems[:3])))

    all_ok = all(ok for _l, _m, ok, _d in rows)
    return (all_ok, rows, source_note)


def _print_report(all_ok, rows, source_note, mode_name):
    print("=" * 78)
    print(f"MetaCoin FULL-STACK VERIFICATION ({mode_name}) — research-stage, "
          "zero-value, no token")
    print(f"{source_note}")
    print("=" * 78)
    for layer, mode, ok, detail in rows:
        print(f"  [{'PASS' if ok else 'FAIL'}] {layer:18s} {mode:18s} {detail}")
    print("-" * 78)
    print(f"RESULT: {'ALL LAYERS PASS' if all_ok else 'FAILURE — see above'}")
    print(HONEST_BOUNDARY)


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="verify_everything.py",
        description=("MetaCoin one-command full-stack verifier (research-stage, "
                     "ZERO-VALUE, no token). Mechanically re-verifies every layer "
                     "from a fresh clone; no LLM judgment anywhere."),
        epilog=("--full re-derives everything (metering is a claim-check by "
                "nature); --quick is bounded-cost anchored acceptance, never "
                "presented as proof. Exit 0 only when every layer passes."),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--full", action="store_true",
                      help="re-derive every layer and compare to its anchor")
    mode.add_argument("--quick", action="store_true",
                      help="bounded-cost anchored acceptance (labeled, conditional)")
    mode.add_argument("--selftest", action="store_true",
                      help="run the mechanical self-test (temp files only)")
    parser.add_argument("--snapshot", default=DEFAULT_SNAPSHOT,
                        help=f"published snapshot (default {DEFAULT_SNAPSHOT})")
    parser.add_argument("--anchor-file", default=DEFAULT_ANCHOR,
                        help=f"committed tip anchor (default {DEFAULT_ANCHOR})")
    parser.add_argument("--ledger", default=DEFAULT_LIVE_LEDGER,
                        help="live ledger path if present (default "
                             f"{DEFAULT_LIVE_LEDGER}; absent in a fresh clone)")
    args = parser.parse_args(argv)

    if args.selftest or not (args.full or args.quick):
        return _selftest()

    all_ok, rows, source_note = run_verification(
        full=args.full, snapshot_path=args.snapshot,
        anchor_path=args.anchor_file, live_ledger_path=args.ledger)
    _print_report(all_ok, rows, source_note, "--full" if args.full else "--quick")
    return 0 if all_ok else 1


# ============================== SELF-TEST ====================================
def _selftest() -> int:
    """Mechanical self-test: the real full run must pass; a tampered snapshot must
    fail; and THE PRODUCT ACCEPTANCE TEST — a fresh-clone simulation (git-tracked
    files only, copied to a temp dir) must fully verify with no access to the live
    ledger or any untracked file."""
    import shutil
    import subprocess
    import tempfile

    print("=== protocol/verify_everything.py self-test (the product acceptance) ===\n")

    checks = []
    tmp_dir = tempfile.mkdtemp(prefix=f"verify_everything_selftest_{os.getpid()}_")
    try:
        # [1] the real stack verifies end-to-end (uses live ledger when present,
        # else the committed snapshot — both paths are the product)
        all_ok, rows, _note = run_verification(full=True)
        checks.append(("real --full run: every layer passes", all_ok))
        if not all_ok:
            for layer, mode, ok, detail in rows:
                if not ok:
                    print(f"    FAIL {layer}: {detail}")

        # [1b] --quick also passes and is honestly labeled (no full-proof claims
        # on anchored-acceptance lines)
        q_ok, q_rows, _ = run_verification(full=False)
        anchored_labels = [m for _l, m, _o, _d in q_rows if m == ANCHORED]
        checks.append(("--quick run passes with ACCEPTED-BY-ANCHOR labeling",
                       q_ok and len(anchored_labels) >= 5))

        # [1c] the pairwise BLIND-SPOT fixture is exercised HERE (selftest,
        # not --full — fixtures are test material, --full re-derives anchored
        # claims): two triples with identical pairwise marginals (every
        # within-triple pair S=0.3) that k=3 separates by exactly 0.2 — the
        # hidden common ancestor visible only above the pair level.
        import itertools as _it
        bs = agent_concentration.blindspot_fixture_paths()
        pairs_equal = all(
            abs(agent_concentration.subset_score(pr) - 0.3) < 1e-9
            for triple in (bs[:3], bs[3:])
            for pr in _it.combinations(triple, 2))
        s3_hidden = agent_concentration.subset_score(bs[:3])
        s3_control = agent_concentration.subset_score(bs[3:])
        checks.append(("blind-spot fixture: identical pairwise marginals; "
                       "k=3 separates hidden ancestor by 0.2 exactly",
                       pairs_equal and abs(s3_hidden - 0.3) < 1e-9
                       and abs(s3_control - 0.1) < 1e-9))

        # [2] tampered snapshot -> chain layer fails -> overall failure
        tampered = os.path.join(tmp_dir, "tampered_published.json")
        with open(DEFAULT_SNAPSHOT, "r", encoding="utf-8") as f:
            snap = json.load(f)
        snap["entries"][1]["payload"]["status"] = "TAMPERED"
        with open(tampered, "w", encoding="utf-8") as f:
            json.dump(snap, f)
        t_ok, t_rows, _ = run_verification(full=True, snapshot_path=tampered,
                                           live_ledger_path=os.path.join(
                                               tmp_dir, "no_such_ledger.jsonl"))
        checks.append(("tampered snapshot is detected (chain layer fails)",
                       not t_ok and not t_rows[0][2]))

        # [3] FRESH-CLONE SIMULATION (the product acceptance test): copy ONLY
        # git-tracked files to a temp dir and run --full there. No live ledger, no
        # untracked artifacts — exactly what a stranger's clone contains.
        clone_dir = os.path.join(tmp_dir, "fresh_clone")
        try:
            _ls = subprocess.run(
                ["git", "ls-files", "-z"], cwd=_REPO_ROOT, capture_output=True,
                timeout=30)
            # a failed `git ls-files` (e.g. this copy is not a git checkout)
            # returns empty stdout — which would split to a truthy [''] and
            # crash the simulation on an empty clone; check the exit code
            tracked = (_ls.stdout.decode("utf-8").split("\0")
                       if _ls.returncode == 0 else None)
        except (FileNotFoundError, subprocess.SubprocessError, OSError):
            tracked = None
        if tracked and any(tracked):
            for rel in tracked:
                if not rel:
                    continue
                src = os.path.join(_REPO_ROOT, rel)
                if not os.path.exists(src):
                    continue  # tracked but deleted locally
                dst = os.path.join(clone_dir, rel)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copyfile(src, dst)
            result = subprocess.run(
                [sys.executable, os.path.join(clone_dir, "protocol",
                                              "verify_everything.py"), "--full"],
                cwd=clone_dir, capture_output=True, text=True, timeout=600)
            # the clone must contain NO private key material (keychains are
            # untracked) — signed-round verification uses public material only
            no_private = not any(
                n.startswith("keychain") for n in os.listdir(clone_dir))
            passed = (result.returncode == 0
                      and "ALL LAYERS PASS" in result.stdout and no_private)
            checks.append(("FRESH-CLONE simulation: --full passes on tracked "
                           "files only (no live ledger, no local artifacts, NO "
                           "private key material)", passed))
            if not passed:
                print("    fresh-clone output tail:")
                for line in result.stdout.splitlines()[-15:]:
                    print(f"      {line}")
        else:
            print("    (git unavailable or not a git checkout — fresh-clone "
                  "simulation SKIPPED, named; the [1] full run above already "
                  "covered this copy's own files)")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    print("--- self-test invariants ---")
    failures = 0
    for name, passed in checks:
        print(f"{name:70s}: {'PASS' if passed else 'FAIL'}")
        if not passed:
            failures += 1

    ok = failures == 0
    print("\n=== self-test summary: " +
          ("ALL CHECKS BEHAVED CORRECTLY" if ok else "FAILURE — see above") + " ===")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
