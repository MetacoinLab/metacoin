"""Read-only bridge from anchored task-0035 to selective disclosure.

Uses PUBLIC synthetic data. Prints no private vault; writes nothing.
Recomputation happens locally; public membership does NOT prove recomputation.
"""
import copy
import hashlib
import json
from pathlib import Path

from experiments.private_receipts import receipt as r
from protocol import audit, verifier_cli


def main():
    root = Path(__file__).resolve().parents[2]
    snapshot = root / "protocol/ledger_published.json"
    ok, reason, details = audit.verify_snapshot_file(str(snapshot))
    if not ok:
        raise ValueError(reason)
    anchor = json.loads((root / "protocol/ledger_anchor.json").read_text())
    if (details["tip_hash"] != anchor["tip_hash"]
            or details["entry_count"] != anchor["entry_count"]):
        raise ValueError("snapshot does not match committed anchor")
    entries = json.loads(snapshot.read_text())["entries"]
    task = verifier_cli.load_task("task-0035")
    result = task.compute()
    result_hash = task.output_hash(result)
    matches = [e for e in entries if e["payload"].get("task_id") == "task-0035"
               and e["payload"].get("local_output_hash") == result_hash]
    if not matches:
        raise ValueError("recomputed result does not match recorded task-0035")
    evidence = {
        "task_id": result["task_id"],
        "reference_source_sha256": hashlib.sha256(Path(task.__file__).read_bytes()).hexdigest(),
        "source_ledger_tip": details["tip_hash"],
        "result_hash": result_hash,
        "full_result": result,
        "verdict": result["summary"]["migration_valid"],
        "scope": "locally-recomputed-public-synthetic-fixture",
    }
    public, vault = r.commit(evidence)
    bundle = r.disclose(vault, ["verdict", "scope"])
    # Local demo pin only. A real verifier needs an authenticated external pin.
    values = r.verify(bundle, public["root"], ["verdict", "scope"])
    changed = copy.deepcopy(bundle)
    next(f for f in changed["disclosures"] if f["name"] == "verdict")["value"] = True
    try:
        r.verify(changed, public["root"])
        tamper_rejected = False
    except r.Invalid:
        tamper_rejected = True
    if not tamper_rejected or values["verdict"] is not False:
        raise ValueError("honest-negative preservation failed")
    print(json.dumps({
        "source_task": "task-0035", "anchored_task_index": matches[0]["index"],
        "local_recomputation_matches_anchor": True,
        "selectively_disclosed": values,
        "manufactured_success_tamper_rejected": tamper_rejected,
        "receipt_bytes": len(r.canonical(public)),
        "disclosure_bytes": len(r.canonical(bundle)),
        "kind": r.KIND, "zk_proof_implemented": False,
        "public_disclosure_proves_task_execution": False,
        "source_data_already_public": True,
        "root_trust": "local-demo-only; not independently witnessed",
        "ledger_writes": 0,
    }, indent=2))


if __name__ == "__main__":
    main()
