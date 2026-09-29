"""Run the fault-injection suite verbosely and record a machine-readable campaign (Order 08 §73)."""
import json, re, subprocess, sys, time
from metacoin_service.economy.ops import FAULT_POINTS
t0 = time.time()
p = subprocess.run([sys.executable, "-m", "unittest", "-v", "metacoin_service.tests.test_work_faults"], capture_output=True, text=True, timeout=1800, env=dict(__import__("os").environ, PYTHONWARNINGS="ignore"))
lines = p.stderr.splitlines()
tests = []
for l in lines:
    m = re.match(r'^(test_\w+) \((\S+)\) \.\.\. .*?(ok|FAIL|ERROR|skipped.*)$', l)
    if m:
        tests.append({'test': m.group(1), 'suite': m.group(2).split('.')[-1], 'result': m.group(3)})
summary = [l for l in lines if l.startswith('Ran ') or l.startswith('OK') or l.startswith('FAILED')]
out = {'schema': 'metacoin-fault-campaign/v1', 'fault_points': list(FAULT_POINTS), 'tests': tests, 'summary': summary, 'exit': p.returncode, 'seconds': round(time.time() - t0, 1),
       'coverage': {'award_commit': 'test_award_and_reservation_faults_leave_no_partial_award', 'reservation_posting': 'test_award_and_reservation_faults_leave_no_partial_award',
                    'evidence_publication': 'test_evidence_verifier_and_decision_faults', 'verifier_completion': 'test_evidence_verifier_and_decision_faults', 'acceptance_decision': 'test_evidence_verifier_and_decision_faults',
                    'payment_signing': 'test_payment_faults_never_invent_a_settlement_or_release_exposure', 'payment_submission': 'test_payment_faults_never_invent_a_settlement_or_release_exposure', 'payment_observation': 'test_payment_faults_never_invent_a_settlement_or_release_exposure',
                    'fee_credit': 'test_fee_credit_and_refund_observation_faults', 'refund_observation': 'test_fee_credit_and_refund_observation_faults', 'adversarial_accounting (duplicate claims, replayed refunds, stale revisions, recipient substitution, journal replay)': 'test_adversarial_accounting_cases'},
       'method': 'fault points armed through /ops/faults on a disposable instance with limits.test_hooks=1; each armed point raises inside the transaction; the test then checks the durable state (no partial award, no invented settlement, exposure retained, journal replay consistent) and disarms explicitly',
       'stderr_tail': p.stderr[-1500:] if p.returncode else None}
json.dump(out, open(sys.argv[1], 'w'), indent=1)
print(json.dumps({'exit': p.returncode, 'tests': len(tests), 'summary': summary}))
