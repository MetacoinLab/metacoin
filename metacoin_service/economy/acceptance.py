"""Acceptance evaluation: ONE server-side path used by API, console, CLI and MCP (Order 08 §14).

Input: frozen WorkTerms, the milestone, and the execution record (a job row with its contract, evidence and any
verification jobs / reviews). Output: a decision CANDIDATE with a structured trace naming passed / failed / unknown /
not-applicable predicates, the scientific conclusion, the execution state and the payment class the policy assigns.
Evaluation never spends, never publishes and never calls the network; authorized acceptance and payment transitions
are explicit operations elsewhere. The four state dimensions stay separate in the result."""
import json

from experiments.work_contracts import energy_analysis as energy
from .. import verification as verification_mod
from ..errors import ServiceError
from . import terms as terms_mod

EXECUTION_STATES = {'queued': 'queued', 'running': 'running', 'succeeded': 'completed', 'failed': 'failed', 'cancelled': 'cancelled'}
SCIENCE_KINDS = {'energy_audit': 'outcome', 'temporal_energy': 'outcome', 'safe_runtime': 'status', 'task_selection': 'status', 'resource_plan': 'status', 'legacy_task_replay': 'not_applicable'}
NEGATIVE_OUTCOMES = ('INFEASIBLE',)
DIAGNOSTIC_OUTCOMES = ('INDETERMINATE',)
VERIFICATION_RANK = {'none': 0, 'analytical': 1, 'sampled_reference': 2, 'replica': 2, 'full_reference': 3, 'full_exact': 4}


def science_conclusion(kind, job):
    """FEASIBLE / INFEASIBLE / INDETERMINATE / not_applicable / unknown, derived from the job's committed outcome."""
    if job is None or job['state'] != 'succeeded':
        return 'unknown' if (job is not None and job['state'] in ('queued', 'running')) else 'not_applicable' if job is None else 'no_valid_evidence'
    out = job['outcome']
    if kind in ('energy_audit', 'temporal_energy'):
        return out if out in energy.OUTCOMES else 'unknown'
    if kind == 'resource_plan':
        return {'optimal_within_tolerance': 'FEASIBLE', 'feasible_incumbent_no_optimality_claim': 'FEASIBLE', 'infeasible_by_solver': 'INFEASIBLE', 'infeasible_established_by_enumeration': 'INFEASIBLE',
                'limit_no_candidate': 'INDETERMINATE', 'numerical_failure': 'INDETERMINATE', 'candidate_rejected_by_checker': 'INDETERMINATE', 'invalid_input': 'INDETERMINATE'}.get(out, 'unknown')
    if kind in ('safe_runtime', 'task_selection'):
        return {'FEASIBLE': 'FEASIBLE', 'INFEASIBLE': 'INFEASIBLE', 'INDETERMINATE': 'INDETERMINATE'}.get(out, 'not_applicable')
    return 'not_applicable'


def _verifications(db, job):
    return db.execute("SELECT * FROM verification_jobs WHERE target_job_id=? ORDER BY created_at", (job['id'],)).fetchall() if job else []


def evaluate(db, terms, milestone_key, job, provider_identity=None, store=None):
    """Returns the decision candidate. `job` is a sqlite row (or None when nothing executed)."""
    terms_mod.validate(terms, 'frozen')
    ms = next((m for m in terms['milestones'] if m['key'] == milestone_key), None)
    if ms is None:
        raise ServiceError('NOT_FOUND', 'milestone')
    pol = terms['acceptance']
    kind = terms['operation']['kind']
    contract = db.execute('SELECT * FROM contracts WHERE id=?', (job['contract_id'],)).fetchone() if job else None
    execution = EXECUTION_STATES.get(job['state'], 'unknown') if job else 'not_started'
    if job and job['state'] == 'running' and job['lease_expires'] and job['lease_expires'] < __import__('metacoin_service.db', fromlist=['now']).now():
        execution = 'unknown'
    science = science_conclusion(kind, job)
    summary = json.loads(job['summary_json']) if job and job['summary_json'] else {}
    doc = json.loads(contract['contract_json']) if contract and contract['contract_json'] else {}
    trace = []

    def add(pid, ptype, result, reason, detail=None):
        trace.append({'predicate': pid, 'type': ptype, 'result': result, 'reason': reason, 'detail': detail})

    verifs = _verifications(db, job)
    for p in pol['predicates']:
        pt, params = p['type'], p.get('params', {})
        if job is None or job['state'] != 'succeeded':
            add(p['id'], pt, 'not_applicable' if job is None else 'unknown' if job['state'] in ('queued', 'running') else 'failed', 'no committed evidence (execution %s)' % execution)
            continue
        if pt == 'artifact_complete':
            keys = [d['key'] for d in terms['deliverables'] if d['required'] and d['key'] in ms['deliverables']]
            ok = bool(job['evidence_artifact_id'] and job['evidence_root'])
            add(p['id'], pt, 'passed' if ok else 'failed', 'evidence vault committed with root' if ok else 'evidence artifact missing', {'required_deliverables': keys, 'evidence_root': job['evidence_root']})
        elif pt == 'source_revision':
            ok = contract is not None and contract['contract_digest'] == terms['operation']['contract_digest'] and contract['input_root'] == terms['operation']['input_root']
            add(p['id'], pt, 'passed' if ok else 'failed', 'evidence produced under the frozen operation (contract digest and input root)' if ok else 'evidence belongs to a different contract revision or input root',
                {'expected_contract_digest': terms['operation']['contract_digest'], 'actual': contract['contract_digest'] if contract else None})
        elif pt == 'schema_valid':
            want_model = params.get('model_id') or terms['operation'].get('model_id'); want_schema = params.get('result_schema')
            ok = doc.get('model_id') == want_model and (want_schema is None or summary.get('result_schema', doc.get('result_schema', want_schema)) == want_schema)
            add(p['id'], pt, 'passed' if ok else 'failed', 'model and result schema match the frozen operation' if ok else 'model or result schema differ', {'model_id': doc.get('model_id'), 'expected': want_model})
        elif pt == 'outcome_in':
            ok = job['outcome'] in params['outcomes']
            add(p['id'], pt, 'passed' if ok else 'failed', 'outcome %s is in the accepted set' % job['outcome'] if ok else 'outcome %s is not an accepted determination outcome' % job['outcome'], {'outcome': job['outcome']})
        elif pt == 'status_in':
            ok = job['outcome'] in params['statuses']
            add(p['id'], pt, 'passed' if ok else 'failed', 'solver status %s is an accepted status' % job['outcome'] if ok else 'status %s is not accepted (a limit or failure is never a witness)' % job['outcome'], {'status': job['outcome']})
        elif pt == 'exact_output_hash':
            got = summary.get(params.get('field', 'output_hash'))
            ok = got == params['registered_hash']
            add(p['id'], pt, 'passed' if ok else 'failed', 'canonical output hash equals the registered hash' if ok else 'canonical output hash differs from the registered hash', {'registered': params['registered_hash'], 'delivered': got})
        elif pt == 'verification_passed':
            want = params['class']; distinct = params.get('distinct_verifier', False)
            passed = [v for v in verifs if v['state'] == 'passed' and v['result_commitment'] == job['evidence_root'] and VERIFICATION_RANK.get(v['class'], 0) >= VERIFICATION_RANK[want]
                      and (params.get('min_sample_count') is None or (json.loads(v['params_json']).get('sample_count') or 0) >= params['min_sample_count'])
                      and (params.get('verifier_digest') is None or json.loads(v['statement_json'] or '{}').get('auditor_digest') == params['verifier_digest'])]
            if passed and distinct:
                # same-service audit = same custody; a distinct verifier needs a statement whose auditor is not the provider identity
                passed = [v for v in passed if provider_identity and json.loads(v['statement_json'] or '{}').get('auditor_id') != provider_identity and provider_identity != 'service']
            pending = [v for v in verifs if v['state'] in ('queued', 'awaiting_replica')]
            failed = [v for v in verifs if v['state'] in ('failed', 'disputed') and v['result_commitment'] == job['evidence_root']]
            if passed:
                add(p['id'], pt, 'passed', 'verification %s (%s) passed on this evidence commitment' % (passed[-1]['id'], passed[-1]['class']), {'verification_id': passed[-1]['id'], 'class': passed[-1]['class'], 'distinct_verifier': distinct})
            elif failed and not pending:
                add(p['id'], pt, 'failed', 'verification %s failed on this evidence commitment' % failed[-1]['id'], {'verification_id': failed[-1]['id'], 'class': failed[-1]['class']})
            elif pending:
                add(p['id'], pt, 'unknown', 'verification %s pending' % pending[-1]['id'], {'verification_id': pending[-1]['id']})
            else:
                add(p['id'], pt, 'unknown', 'no verification of class %s has been requested for this evidence' % want + (' by a distinct verifier' if distinct else ''), {'required_class': want, 'distinct_verifier': distinct})
        elif pt == 'review_signature':
            r = db.execute("SELECT decision FROM reviews WHERE job_id=? ORDER BY created_at DESC LIMIT 1", (job['id'],)).fetchone()
            want = params.get('decision', 'accepted')
            if r is None:
                add(p['id'], pt, 'unknown', 'no signed review recorded', None)
            else:
                add(p['id'], pt, 'passed' if r['decision'] == want else 'failed', 'signed review decision %s' % r['decision'], {'decision': r['decision']})
        elif pt == 'numerical_tolerance':
            got = summary.get(params['field'])
            if type(got) not in (int, float):
                add(p['id'], pt, 'unknown', 'field %s absent from the committed summary' % params['field'], None)
            else:
                ok = abs(got - params['expected']) <= params['abs_tol']
                add(p['id'], pt, 'passed' if ok else 'failed', '%s = %s within %s %s of %s' % (params['field'], got, params['abs_tol'], params['unit'], params['expected']) if ok else '%s = %s outside the declared tolerance' % (params['field'], got), {'value': got, 'unit': params['unit']})
        elif pt == 'invariants':
            checks = {}
            if 'margin_ordering' in params['names']:
                checks['margin_ordering'] = summary.get('worst_margin') is None or summary.get('best_margin') is None or summary['worst_margin'] <= summary['best_margin']
            if 'reserve_nonnegative' in params['names']:
                checks['reserve_nonnegative'] = (summary.get('required_low') or 0) >= 0
            if 'outcome_consistent' in params['names']:
                checks['outcome_consistent'] = job['outcome'] in energy.OUTCOMES or kind not in ('energy_audit', 'temporal_energy')
            ok = all(checks.values())
            add(p['id'], pt, 'passed' if ok else 'failed', 'invariants hold' if ok else 'an invariant failed', checks)
        else:
            add(p['id'], pt, 'not_applicable', 'predicate type has no evaluator for this kind', None)
    results = [t['result'] for t in trace]
    treatment = None
    if science in pol['outcomes']:
        treatment = pol['outcomes'][science]
    elif 'not_applicable' in pol['outcomes'] and science in ('not_applicable',):
        treatment = pol['outcomes']['not_applicable']
    elif science in ('unknown', 'no_valid_evidence'):
        treatment = None
    if job is None or job['state'] in ('queued', 'running'):
        decision, reason, pay_class = 'pending', 'execution not finished', 'none'
    elif job['state'] != 'succeeded':
        ft = terms['failure_treatment']['execution_failure']
        decision, reason = 'rejected', 'execution %s produced no valid evidence; a crash or timeout is not a scientific negative' % execution
        pay_class = 'diagnostic' if (ft == 'diagnostic_fee_if_evidence' and job['evidence_artifact_id']) else 'none'
    elif 'failed' in results:
        decision, reason, pay_class = 'rejected', 'failed predicates: ' + ', '.join(t['predicate'] for t in trace if t['result'] == 'failed'), 'none'
        if terms['failure_treatment']['invalid_evidence'] == 'diagnostic_fee_if_evidence' and all(t['result'] != 'failed' for t in trace if t['type'] in ('artifact_complete', 'source_revision')):
            pay_class = 'diagnostic'
    elif 'unknown' in results:
        decision, reason, pay_class = 'pending', 'unresolved predicates: ' + ', '.join(t['predicate'] for t in trace if t['result'] == 'unknown'), 'none'
    elif treatment is None:
        decision, reason, pay_class = 'rejected', 'scientific conclusion %s has no treatment in the frozen policy' % science, 'none'
    elif treatment == 'reject':
        decision, reason, pay_class = 'rejected', 'the frozen policy rejects outcome %s' % science, 'none'
    elif treatment == 'accept_diagnostic':
        decision, reason, pay_class = 'accepted', 'accepted as a diagnostic delivery (outcome %s)' % science, 'diagnostic'
    else:
        if science in NEGATIVE_OUTCOMES:
            req = pol.get('valid_negative', {}).get('requires', [])
            have = {t['type'] for t in trace if t['result'] == 'passed'}
            if not set(req) <= have:
                decision, reason, pay_class = 'rejected', 'a negative conclusion is valid only with ' + ', '.join(req) + '; missing ' + ', '.join(sorted(set(req) - have)), 'none'
            else:
                decision, reason, pay_class = 'accepted', 'verified negative conclusion accepted under the outcome-neutral rule', 'complete'
        else:
            decision, reason, pay_class = 'accepted', 'all predicates passed; outcome %s accepted' % science, 'complete'
    amount = pol['payment_rule'].get(pay_class, 0) if pay_class != 'none' else 0
    amount = min(amount, ms['max_payment'])
    return {'schema': 'metacoin-acceptance-evaluation/v1', 'milestone': milestone_key, 'decision_candidate': decision, 'reason': reason,
            'execution': execution, 'science': science, 'payment_class': pay_class, 'payable_amount': amount, 'asset': terms['payment']['asset'],
            'trace': trace, 'policy_digest': __import__('hashlib').sha256(b'metacoin/acceptance-policy/v1\0' + __import__('experiments.private_receipts.receipt', fromlist=['canonical']).canonical(pol)).hexdigest(),
            'job_id': job['id'] if job else None, 'evidence_root': job['evidence_root'] if job else None,
            'note': 'candidate only: acceptance and payment transitions are explicit authorized operations; evaluation spent nothing and called no network'}
