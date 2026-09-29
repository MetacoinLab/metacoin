#!/usr/bin/env bash
# Full non-demo local pilot walkthrough. Run from the repository root:
#   bash experiments/work_contracts/pilot/walkthrough.sh [work-dir]
# Uses SYNTHETIC inputs and the file-backed durable TEST provider (a testing
# facility, not a payment system) so that a later process can reconcile.
# Everything it creates lives under the work directory; private vaults there
# are PLAINTEXT. Never publish that directory.
set -euo pipefail
cli="python3 -m experiments.work_contracts.cli"
work="${1:-$(mktemp -d)}"
mkdir -p "$work"
chmod 700 "$work"
expiry="$(python3 -c 'import time; print(int(time.time()) + 3600)')"
json() { python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d[sys.argv[2]])' "$1" "$2"; }

echo "# 1 owner: synthetic input (INDETERMINATE case)"
$cli fixture --out "$work/inputs.json" --outcome INDETERMINATE
echo "# 2 owner: commit inputs, fix terms (valid negative/indeterminate counts as completed work)"
$cli prepare --input "$work/inputs.json" --out-dir "$work/owner" --job pilot-001 \
  --expires-at "$expiry" --capability durable_test_simulation > "$work/prepared.json"
pin="$(json "$work/owner/owner-pin.json" contract_digest)"
echo "# 3 owner: campaign journal (fixed cap 3, nonrenewing)"
$cli campaign init --journal "$work/journal.sqlite" --campaign pilot-campaign --limit 3 > /dev/null
echo "# 4 owner: register the contract (one action entitlement for pilot-001)"
$cli register --journal "$work/journal.sqlite" --contract "$work/owner/contract.json" --expected-contract-digest "$pin"
echo "# 5 worker: execute the pinned analysis (private evidence vault)"
$cli execute --contract "$work/owner/contract.json" --expected-contract-digest "$pin" \
  --input-vault "$work/owner/private-input-vault.json" --out "$work/private-evidence.json"
echo "# 6 auditor: full private recomputation, recorded in the journal; public bundle written"
$cli record-audit --journal "$work/journal.sqlite" --job pilot-001 \
  --input-vault "$work/owner/private-input-vault.json" --evidence-vault "$work/private-evidence.json" \
  --out "$work/public-bundle.json" > "$work/audit-summary.json"
root="$(json "$work/audit-summary.json" expected_evidence_root)"
echo "# 7 public verifier: membership + bindings against the OWNER-supplied pins"
$cli verify --contract "$work/owner/contract.json" --expected-contract-digest "$pin" \
  --bundle "$work/public-bundle.json" --expected-root "$root" > "$work/public-verify.json"
echo "# 8 auditor (private): where does the uncertainty come from? plus a hypothetical +60000 mJ"
$cli explain --contract "$work/owner/contract.json" --expected-contract-digest "$pin" \
  --input-vault "$work/owner/private-input-vault.json" --added-usable-energy 60000 > "$work/private-explanation.json"
echo "# 9 actor: build the bound action request (read-only)"
$cli request --journal "$work/journal.sqlite" --job pilot-001 --request-id req-001 --out "$work/request.json" > /dev/null
echo "# 10 actor: dry run (checks everything, reserves nothing, dispatches nothing)"
$cli dispatch --journal "$work/journal.sqlite" --request "$work/request.json" --dry-run \
  --adapter durable-test-simulation --provider-state "$work/provider.json" --provider-initial-balance 5 > "$work/dry-run.json"
echo "# 11 actor: dispatch through the durable TEST provider"
$cli dispatch --journal "$work/journal.sqlite" --request "$work/request.json" \
  --adapter durable-test-simulation --provider-state "$work/provider.json" > "$work/dispatch.json"
echo "# 12 actor: status, then reconcile from a NEW process (durable record resolves it)"
$cli status --journal "$work/journal.sqlite" --request-id req-001 > "$work/status.json"
$cli reconcile --journal "$work/journal.sqlite" --request-id req-001 \
  --adapter durable-test-simulation --provider-state "$work/provider.json" > "$work/reconcile.json"
echo "# 13 actor: identical retry from a new process creates no new right and no new debit"
$cli dispatch --journal "$work/journal.sqlite" --request "$work/request.json" \
  --adapter durable-test-simulation --provider-state "$work/provider.json" > "$work/retry.json"
echo "# 14 owner: export the PUBLIC review package; a reviewer imports it with their own pins"
$cli export-public --contract "$work/owner/contract.json" --expected-contract-digest "$pin" \
  --bundle "$work/public-bundle.json" --expected-root "$root" --out "$work/public-package.zip" > /dev/null
$cli import-public --package "$work/public-package.zip" --expected-contract-digest "$pin" \
  --expected-root "$root" --out-dir "$work/reviewer" > "$work/import-public.json"
echo "# 15 owner -> auditor: PRIVATE audit package (exchange only via the agreed channel)"
$cli export-private --contract "$work/owner/contract.json" --input-vault "$work/owner/private-input-vault.json" \
  --evidence-vault "$work/private-evidence.json" --out "$work/private-package.zip" > /dev/null
$cli import-private --package "$work/private-package.zip" --out-dir "$work/auditor-copy" > "$work/import-private.json"
echo "# 16 refusals carry stable codes: the same request id with changed terms, and a second request id for a consumed entitlement"
python3 - "$work/request.json" "$work/tampered.json" <<'PY'
import json, sys
r = json.load(open(sys.argv[1])); r['amount'] = 2
open(sys.argv[2], 'w').write(json.dumps(r))
PY
set +e
$cli dispatch --journal "$work/journal.sqlite" --request "$work/tampered.json" \
  --adapter durable-test-simulation --provider-state "$work/provider.json" > "$work/refusal-tampered.json" 2>/dev/null
echo "   exit=$? code=$(json "$work/refusal-tampered.json" code)"
$cli request --journal "$work/journal.sqlite" --job pilot-001 --request-id req-002 --out "$work/request2.json" > /dev/null
$cli dispatch --journal "$work/journal.sqlite" --request "$work/request2.json" \
  --adapter durable-test-simulation --provider-state "$work/provider.json" > "$work/refusal-second.json" 2>/dev/null
echo "   exit=$? code=$(json "$work/refusal-second.json" code)"
set -e
echo "# 17 owner: campaign view"
$cli campaign show --journal "$work/journal.sqlite" > "$work/campaign.json"
python3 - "$work" <<'PY'
import json, sys
w = sys.argv[1]
load = lambda name: json.load(open(f'{w}/{name}'))
print(json.dumps({
    'public_outcome': load('public-verify.json')['disclosed']['outcome'],
    'work_completed': load('audit-summary.json')['work_completed'],
    'dominant_uncertainty_source_private': load('private-explanation.json')['explanation']['dominant_uncertainty_source'],
    'counterfactual': load('private-explanation.json')['counterfactual']['conditional_outcome'],
    'dry_run_would_reserve': load('dry-run.json')['would_reserve'],
    'dispatch_state': load('dispatch.json')['state'],
    'reconcile': load('reconcile.json')['reconciliation'],
    'retry_state': load('retry.json')['state'],
    'provider_balance_after_retry': json.load(open(f'{w}/provider.json'))['balance'],
    'provider_dispatches': json.load(open(f'{w}/provider.json'))['dispatches'],
    'public_import_verified': load('import-public.json')['verification']['membership_verified'],
    'private_import': load('import-private.json')['imported'],
    'campaign_exposure': load('campaign.json')['exposure'],
    'refusals': [load('refusal-tampered.json')['code'], load('refusal-second.json')['code']],
}, indent=2))
PY
echo "# done: $work (contains PRIVATE plaintext vaults; delete when finished)"
