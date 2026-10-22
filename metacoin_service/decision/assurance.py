"""Claim-specific assurance (Deliverable A).

A verification class is not a universal measure of strength: a replica, a sampled check, an exact recomputation and an
analytical invariant establish different things. An assurance REQUIREMENT names the claim a contract needs (claim type,
coverage, comparison semantics, independence dimensions, sampling bound, checker) and an assurance STATEMENT is derived
from a signed verification record of the delivered evidence root. `evaluate` decides compatibility clause by clause: the
same operation and version, the same input and result commitments, the same claim type, a justified coverage
implication (every item => a sample of items; nothing else), the same comparison semantics, every required independence
dimension OBSERVED true (unknown is not independent), the sampling bound actually bound and met, and a passed outcome.
A class name or a numeric rank never satisfies a requirement; absence is never success; a detected defect on the same
bindings fails the requirement even if a later attempt passed (no cherry-picking)."""
import hashlib
import json

from fractions import Fraction

REQUIREMENT_SCHEMA = 'metacoin-assurance-requirement/v1'
STATEMENT_SCHEMA = 'metacoin-assurance-statement/v1'
CLAIM_TYPES = ('itemwise_reference_exact', 'whole_result_reference', 'analytical_invariants', 'replica_agreement', 'same_implementation_regression', 'witness_check', 'legacy_exact_replay',
               'final_transition_reference', 'producer_sample_reevaluation')
COVERAGES = ('every_item', 'sampled_items', 'whole_result', 'final_transition', 'invariants_only', 'producer_stored_samples')
COMPARISONS = ('exact', 'tolerance', 'invariant', 'enumerated_predicate')
INDEPENDENCE_DIMENSIONS = ('implementation', 'process', 'operator', 'organization', 'input_source', 'instrument', 'hardware', 'library', 'challenge_authority')
OUTCOMES = ('passed', 'failed', 'inconclusive', 'unsupported', 'pending')
# what each (kind, verification class) establishes: claim type, coverage, comparison, implementation independence
CLAIMS = {
    'temporal_batch': {'full_exact': ('itemwise_reference_exact', 'every_item', 'exact', True), 'sampled_reference': ('itemwise_reference_exact', 'sampled_items', 'exact', True), 'analytical': ('analytical_invariants', 'invariants_only', 'invariant', True), 'replica': ('replica_agreement', 'whole_result', 'exact', False)},
    'monte_carlo_reliability': {'full_reference': ('producer_sample_reevaluation', 'producer_stored_samples', 'exact', 'partial'), 'analytical': ('analytical_invariants', 'invariants_only', 'invariant', True), 'replica': ('replica_agreement', 'whole_result', 'exact', False)},
    'heat_diffusion': {'full_reference': ('whole_result_reference', 'whole_result', 'tolerance', True), 'analytical': ('analytical_invariants', 'invariants_only', 'invariant', True), 'sampled_reference': ('final_transition_reference', 'final_transition', 'tolerance', True), 'replica': ('replica_agreement', 'whole_result', 'tolerance', False)},
    'calibration_fit': {'full_reference': ('whole_result_reference', 'whole_result', 'tolerance', True)},
    'resource_plan': {'full_reference': ('whole_result_reference', 'whole_result', 'exact', True), 'analytical': ('analytical_invariants', 'invariants_only', 'invariant', True)},
    'temporal_energy': {'full_exact': ('same_implementation_regression', 'whole_result', 'exact', False)}, 'energy_audit': {'full_exact': ('same_implementation_regression', 'whole_result', 'exact', False)},
    'safe_runtime': {'full_exact': ('same_implementation_regression', 'whole_result', 'exact', False)}, 'plan_comparison': {'full_exact': ('same_implementation_regression', 'whole_result', 'exact', False)},
    'task_selection': {'full_exact': ('same_implementation_regression', 'whole_result', 'exact', False)},
    'legacy_task_replay': {'full_exact': ('legacy_exact_replay', 'whole_result', 'exact', False)},
    'decision_observability': {'full_exact': ('same_implementation_regression', 'whole_result', 'exact', False), 'analytical': ('witness_check', 'whole_result', 'enumerated_predicate', True)},
    'evidence_acquisition': {'full_exact': ('same_implementation_regression', 'whole_result', 'exact', False), 'analytical': ('witness_check', 'whole_result', 'enumerated_predicate', True)},
}
IMPLICATIONS = {('every_item', 'sampled_items'): 'every item checked exactly is a sample of all N items (k = N): the miss probability is zero for any m >= 1'}
LEGACY_RANK_NOTE = 'legacy-rank/v1: a passing verification whose class rank is >= the requested rank satisfies the predicate (replica and sampled_reference share rank 2); not claim-specific; preserved for terms frozen under metacoin-acceptance-policy/v1'


class AssuranceInvalid(ValueError):
    def __init__(self, code, **detail):
        super().__init__(code); self.code, self.detail = code, detail


def _err(code, **d):
    raise AssuranceInvalid(code, **d)


def _hex(v):
    return type(v) is str and len(v) == 64 and all(c in '0123456789abcdef' for c in v)


def _rational01(v, code):
    if type(v) is not str or '/' not in v and not v.replace('.', '', 1).isdigit():
        _err(code, allowed='rational string in (0, 1)')
    try:
        f = Fraction(v)
    except (ValueError, ZeroDivisionError):
        _err(code, allowed='rational string in (0, 1)')
    if not 0 < f < 1:
        _err(code, allowed='rational string in (0, 1)')
    return f


def validate_requirement(req, kind=None):
    """Structural validation; unknown schema versions, claim types, coverages, comparison semantics and independence
    dimensions are refused. With `kind`, the (claim, coverage) pair must be achievable by an installed verification class."""
    if type(req) is not dict:
        _err('requirement_object')
    allowed = {'schema', 'claim_type', 'coverage', 'comparison', 'independence', 'sampling', 'checker', 'operation', 'on_detected_defect', 'note'}
    if set(req) - allowed:
        _err('requirement_unknown_field', unknown=sorted(set(req) - allowed))
    if req.get('schema') != REQUIREMENT_SCHEMA:
        _err('requirement_schema', allowed=[REQUIREMENT_SCHEMA])
    if req.get('claim_type') not in CLAIM_TYPES:
        _err('requirement_claim_type', allowed=list(CLAIM_TYPES))
    if req.get('coverage') not in COVERAGES:
        _err('requirement_coverage', allowed=list(COVERAGES))
    cmp_ = req.get('comparison')
    if type(cmp_) is not dict or set(cmp_) - {'semantics', 'tolerance'} or cmp_.get('semantics') not in COMPARISONS:
        _err('requirement_comparison', allowed={'semantics': list(COMPARISONS), 'tolerance?': '{abs, rel} decimal strings'})
    if 'tolerance' in cmp_ and (type(cmp_['tolerance']) is not dict or set(cmp_['tolerance']) - {'abs', 'rel'}):
        _err('requirement_tolerance')
    ind = req.get('independence', {})
    if type(ind) is not dict or set(ind) - set(INDEPENDENCE_DIMENSIONS) or not all(type(v) is bool for v in ind.values()):
        _err('requirement_independence', allowed=list(INDEPENDENCE_DIMENSIONS), note='unknown dimensions are refused; values are booleans (required or not)')
    smp = req.get('sampling')
    if smp is not None:
        if type(smp) is not dict or set(smp) - {'min_sample_count', 'max_miss_probability', 'min_defects', 'attempt_policy', 'bound_plan_required'}:
            _err('requirement_sampling', allowed=['min_sample_count', 'max_miss_probability', 'min_defects', 'attempt_policy', 'bound_plan_required'])
        if 'min_sample_count' in smp and (type(smp['min_sample_count']) is not int or isinstance(smp['min_sample_count'], bool) or not 1 <= smp['min_sample_count'] <= 10 ** 6):
            _err('requirement_min_sample_count')
        if 'max_miss_probability' in smp:
            _rational01(smp['max_miss_probability'], 'requirement_max_miss_probability')
        if 'min_defects' in smp and (type(smp['min_defects']) is not int or isinstance(smp['min_defects'], bool) or not 1 <= smp['min_defects'] <= 10 ** 6):
            _err('requirement_min_defects')
        if smp.get('attempt_policy', 'single_fixed_size') != 'single_fixed_size':
            _err('requirement_attempt_policy', allowed=['single_fixed_size'], note='sequential testing is not implemented; this version is fixed-size')
        if req['coverage'] != 'sampled_items':
            _err('requirement_sampling_coverage', note='sampling clauses apply to sampled_items coverage only')
    chk = req.get('checker')
    if chk is not None and (type(chk) is not dict or set(chk) - {'digest', 'id'} or ('digest' in chk and not _hex(chk['digest']))):
        _err('requirement_checker', allowed=['digest (64 hex)', 'id'])
    op = req.get('operation')
    if op is not None and (type(op) is not dict or set(op) - {'kind', 'model_id', 'model_version', 'verifier_id'}):
        _err('requirement_operation', allowed=['kind', 'model_id', 'model_version', 'verifier_id'])
    if req.get('on_detected_defect', 'fail') != 'fail':
        _err('requirement_on_detected_defect', allowed=['fail'])
    if 'note' in req and (type(req['note']) is not str or len(req['note']) > 512):
        _err('requirement_note')
    if kind is not None:
        if kind not in CLAIMS:
            _err('requirement_unsupported_kind', kind=kind, supported=sorted(CLAIMS))
        achievable = [c for c, (ct, cov, cmp2, _) in CLAIMS[kind].items() if ct == req['claim_type'] and (cov == req['coverage'] or (cov, req['coverage']) in IMPLICATIONS) and cmp2 == req['comparison']['semantics']]
        if not achievable:
            _err('requirement_unachievable_for_kind', kind=kind, claim_type=req['claim_type'], coverage=req['coverage'], achievable=[{'class': c, 'claim_type': v[0], 'coverage': v[1], 'comparison': v[2]} for c, v in CLAIMS[kind].items()])
        if op and op.get('kind') not in (None, kind):
            _err('requirement_operation_kind_mismatch', kind=kind)
    return req


def requirement_digest(req):
    from experiments.private_receipts.receipt import canonical
    return hashlib.sha256(b'metacoin/assurance-requirement/v1\0' + canonical(req)).hexdigest()


def classes_for(kind, req):
    return [c for c, (ct, cov, cmp2, _) in CLAIMS.get(kind, {}).items() if ct == req['claim_type'] and (cov == req['coverage'] or (cov, req['coverage']) in IMPLICATIONS) and cmp2 == req['comparison']['semantics']]


# ---- statements ---------------------------------------------------------------------------------------------------------
def observed_independence(kind, cls, provider_execution, statement, result, replica_backends=None):
    """Observed provenance per dimension: True / False / 'unknown' / 'partial'. Unknown is reported as unknown."""
    ct, cov, cmp2, impl = CLAIMS.get(kind, {}).get(cls, (None, None, None, 'unknown'))
    hw = False
    if cls == 'replica' and replica_backends:
        hw = len(set(replica_backends)) > 1
    return {'implementation': impl, 'process': provider_execution == 'node', 'operator': False, 'organization': False, 'input_source': False, 'instrument': 'unknown',
            'hardware': hw if cls == 'replica' else False, 'library': 'unknown', 'challenge_authority': False if cls == 'sampled_reference' else 'unknown',
            'notes': {'challenge_authority': 'server-side CSPRNG of this instance: not independent of a malicious server/operator' if cls == 'sampled_reference' else None,
                      'implementation': {True: 'independent reference implementation (shared components disclosed)', False: 'same implementation or same code on another backend', 'partial': 'reference re-evaluation of producer-stored samples; the sampler mapping is not regenerated', 'unknown': 'unknown'}.get(impl, 'unknown')}}


def statement_from_verification(vrow, target, tcontract, provider_execution='local_worker', sampling=None, replica_backends=None):
    """Derive the assurance statement of one verification record (sqlite rows or dicts) for the target job."""
    kind, cls = target['kind'], vrow['class']
    st = json.loads(vrow['statement_json']) if vrow.get('statement_json') else {}
    res = json.loads(vrow['result_json']) if vrow.get('result_json') else {}
    doc = json.loads(tcontract['contract_json']) if tcontract.get('contract_json') else {}
    ct, cov, cmp2, _ = CLAIMS.get(kind, {}).get(cls, (None, None, None, None))
    state = vrow['state']
    outcome = {'passed': 'passed', 'failed': 'failed', 'disputed': 'failed', 'resolved': 'failed', 'incomplete': 'inconclusive', 'queued': 'pending', 'awaiting_replica': 'pending'}.get(state, 'pending')
    resolution = json.loads(vrow['resolution_json']) if vrow.get('resolution_json') else None
    if state == 'resolved' and resolution and resolution.get('decision') in ('producer_upheld', 'environment_difference'):
        outcome = 'inconclusive'            # adjudicated false alarm by the designated reviewer: no longer a detected defect, never a pass
    if ct is None:
        outcome = 'unsupported'
    params = json.loads(vrow['params_json']) if vrow.get('params_json') else {}
    scope = st.get('scope') or {}
    items = scope.get('items')
    detected = 0
    for c in res.get('checks') or []:
        d = c.get('detail') if isinstance(c.get('detail'), dict) else {}
        detected += len(d.get('mismatches') or []) + len(d.get('violations') or [])
    out = {'schema': STATEMENT_SCHEMA, 'verification_id': vrow['id'], 'class': cls, 'claim_type': ct, 'coverage': cov, 'comparison': {'semantics': cmp2, 'tolerance': st.get('tolerance') if cmp2 == 'tolerance' else None},
           'operation': {'kind': kind, 'model_id': doc.get('model_id'), 'model_version': (json.loads(tcontract['params_json'] or '{}') if tcontract.get('params_json') else {}).get('manifest_version'), 'verifier_id': doc.get('verifier_id'), 'verifier_digest': doc.get('verifier_digest')},
           'input_commitment': tcontract.get('input_root'), 'result_commitment': vrow['result_commitment'], 'target_job_id': target['id'],
           'checked_scope': {'checked': scope.get('checked'), 'total': scope.get('total'), 'coverage_text': scope.get('coverage'), 'items_digest': hashlib.sha256(json.dumps(items).encode()).hexdigest() if items else None, 'item_count': len(items) if items else None},
           'population_frame': ({'population': scope.get('total'), 'frame_digest': frame_digest(vrow['result_commitment'], scope.get('total')), 'ordering': 'item index i in 0..N-1 of the committed result array'} if cov == 'sampled_items' else None),
           'checker': checker_identity(kind, cls, st), 'independence': observed_independence(kind, cls, provider_execution, st, res, replica_backends),
           'sampling': None, 'challenge_digest': st.get('challenge_digest'), 'detected_defects': detected, 'outcome': outcome, 'verification_state': state, 'reviewer_resolution': (resolution or {}).get('decision'),
           'signed_statement_schema': st.get('schema'), 'issued_at': st.get('issued_at'),
           'legacy_statement_class_text': st.get('claim')}
    if cov == 'sampled_items':
        out['sampling'] = dict(sampling or {}, sample_count=params.get('sample_count'), plan_bound=bool(sampling), challenge_policy='verification-challenge/v1 (server-side seed drawn after the result commitment; ranks by sha256(seed||commitment||i); uniform without replacement under a random-oracle/computational-pseudorandomness assumption)')
    return out


def checker_identity(kind, cls, st):
    """The checker that actually decided the claim: the independent witness checker for decision kinds' analytical class, else the
    verification service implementation (its digest covers the reference kernels it dispatches to)."""
    if kind in ('decision_observability', 'evidence_acquisition') and cls == 'analytical':
        from . import checker as dchecker, operations as dops
        return {'id': dchecker.CHECKER_ID, 'digest': dops.checker_digest(), 'dependencies': st.get('shared_code'), 'dispatched_by': st.get('auditor_id')}
    return {'id': st.get('auditor_id'), 'digest': st.get('auditor_digest'), 'dependencies': st.get('shared_code')}


def frame_digest(result_commitment, total):
    return hashlib.sha256(('frame/v1|%s|%s' % (result_commitment, total)).encode()).hexdigest()


# ---- compatibility -----------------------------------------------------------------------------------------------------------
def _clauses(req, st, binding):
    """Clause-by-clause compatibility of one statement with a requirement. `binding` = {kind, model_id, model_version, input_root, evidence_root}."""
    cl = []
    def add(name, ok, detail=None):
        cl.append({'clause': name, 'ok': bool(ok), 'detail': detail})
    add('statement_schema', st.get('schema') == STATEMENT_SCHEMA, st.get('schema'))
    add('operation_kind', st['operation']['kind'] == binding['kind'], {'statement': st['operation']['kind'], 'required': binding['kind']})
    add('model_id', st['operation'].get('model_id') == binding.get('model_id'), {'statement': st['operation'].get('model_id'), 'required': binding.get('model_id')})
    if binding.get('model_version') is not None:
        add('model_version', st['operation'].get('model_version') == binding.get('model_version'), {'statement': st['operation'].get('model_version'), 'required': binding.get('model_version')})
    add('input_commitment', st.get('input_commitment') == binding.get('input_root'))
    add('result_commitment', st.get('result_commitment') == binding.get('evidence_root'), {'statement': (st.get('result_commitment') or '')[:16], 'required': (binding.get('evidence_root') or '')[:16]})
    add('claim_type', st.get('claim_type') == req['claim_type'], {'statement': st.get('claim_type'), 'required': req['claim_type'], 'rule': 'a class name or rank never substitutes for the claim'})
    cov_ok = st.get('coverage') == req['coverage'] or (st.get('coverage'), req['coverage']) in IMPLICATIONS
    add('coverage', cov_ok, {'statement': st.get('coverage'), 'required': req['coverage'], 'implication': IMPLICATIONS.get((st.get('coverage'), req['coverage']))})
    add('comparison_semantics', (st.get('comparison') or {}).get('semantics') == req['comparison']['semantics'], {'statement': (st.get('comparison') or {}).get('semantics'), 'required': req['comparison']['semantics']})
    for dim, required in (req.get('independence') or {}).items():
        if required:
            obs = (st.get('independence') or {}).get(dim, 'unknown')
            add('independence:' + dim, obs is True, {'observed': obs, 'rule': 'unknown or partial provenance is not independence'})
    chk = req.get('checker') or {}
    if chk.get('digest'):
        add('checker_digest', (st.get('checker') or {}).get('digest') == chk['digest'])
    if chk.get('id'):
        add('checker_id', (st.get('checker') or {}).get('id') == chk['id'])
    smp = req.get('sampling')
    if smp:
        s = st.get('sampling') or {}
        if 'min_sample_count' in smp:
            k = (st.get('checked_scope') or {}).get('checked') if st.get('coverage') == 'every_item' else s.get('sample_count')
            add('sampling:min_sample_count', k is not None and k >= smp['min_sample_count'], {'statement': k, 'required': smp['min_sample_count']})
        if smp.get('bound_plan_required', 'max_miss_probability' in smp) and st.get('coverage') != 'every_item':
            add('sampling:plan_bound_before_challenge', bool(s.get('plan_bound')), {'plan_id': s.get('plan_id')})
        if 'max_miss_probability' in smp:
            if st.get('coverage') == 'every_item':
                add('sampling:max_miss_probability', True, {'achieved': '0 (every item checked)', 'required': smp['max_miss_probability']})
            else:
                ach = s.get('achieved_bound_exact')
                ok = ach is not None and Fraction(ach) <= Fraction(smp['max_miss_probability']) and s.get('feasible') is True
                add('sampling:max_miss_probability', ok, {'achieved': ach, 'required': smp['max_miss_probability'], 'feasible': s.get('feasible')})
        if 'min_defects' in smp and st.get('coverage') != 'every_item':
            add('sampling:min_defects_assumption', s.get('minimum_defects') == smp['min_defects'], {'statement': s.get('minimum_defects'), 'required': smp['min_defects']})
        if st.get('coverage') != 'every_item':
            add('sampling:no_failed_attempt_under_plan', not s.get('failed_attempts'), {'failed_attempts': s.get('failed_attempts')})
    return cl


def evaluate(req, statements, binding):
    """Decide whether any statement satisfies the requirement. Returns status in satisfied | failed | pending | inconclusive |
    missing | unsupported with the full clause trace, the missing clauses of the best candidate and the next checks to request."""
    validate_requirement(req)
    kind = binding['kind']
    candidates = []
    for st in statements:
        cl = _clauses(req, st, binding)
        bind_names = {'statement_schema', 'operation_kind', 'model_id', 'model_version', 'input_commitment', 'result_commitment', 'claim_type', 'coverage', 'comparison_semantics'}
        bound = all(c['ok'] for c in cl if c['clause'] in bind_names)
        candidates.append({'verification_id': st.get('verification_id'), 'class': st.get('class'), 'outcome': st.get('outcome'), 'bindings_match': bound, 'clauses': cl, 'missing_clauses': [c['clause'] for c in cl if not c['ok']],
                           'satisfied': bound and all(c['ok'] for c in cl) and st.get('outcome') == 'passed'})
    defects = [c for c in candidates if c['bindings_match'] and c['outcome'] == 'failed']
    sat = [c for c in candidates if c['satisfied']]
    pend = [c for c in candidates if c['bindings_match'] and c['outcome'] == 'pending']
    inc = [c for c in candidates if c['bindings_match'] and c['outcome'] == 'inconclusive']
    unsup = [c for c in candidates if c['outcome'] == 'unsupported']
    next_checks = [{'class': c, 'establishes': CLAIMS[kind][c][0], 'coverage': CLAIMS[kind][c][1]} for c in classes_for(kind, req)] if kind in CLAIMS else []
    if defects:
        status, reason = 'failed', 'a verification with the same bindings detected a defect (%s); a later passing attempt cannot override a detected defect' % defects[-1]['verification_id']
    elif sat:
        status, reason = 'satisfied', 'statement %s (%s) satisfies every clause' % (sat[-1]['verification_id'], sat[-1]['class'])
    elif pend:
        status, reason = 'pending', 'verification %s is still running' % pend[-1]['verification_id']
    elif inc:
        status, reason = 'inconclusive', 'verification %s did not complete its checks (incomplete assurance, not a pass)' % inc[-1]['verification_id']
    elif not next_checks and kind in CLAIMS:
        status, reason = 'unsupported', 'no installed verification class establishes this claim for kind %s' % kind
    elif unsup and not candidates:
        status, reason = 'unsupported', 'unsupported class'
    else:
        best = max(candidates, key=lambda c: -len(c['missing_clauses'])) if candidates else None
        status = 'missing'
        reason = ('no statement satisfies the requirement; closest %s misses %s' % (best['verification_id'], ', '.join(best['missing_clauses']))) if best else 'no verification of the required claim has been recorded for this evidence root'
    return {'schema': 'metacoin-assurance-evaluation/v1', 'status': status, 'satisfied': status == 'satisfied', 'reason': reason, 'requirement_digest': requirement_digest(req), 'requirement': req,
            'candidates': candidates, 'matched_statement': sat[-1]['verification_id'] if sat else None, 'defect_statement': defects[-1]['verification_id'] if defects else None,
            'missing_clauses': (sorted({m for c in candidates for m in c['missing_clauses']}) if candidates and status == 'missing' else []), 'next_checks': next_checks if status in ('missing', 'inconclusive') else [],
            'plain': plain_english(req, status, sat[-1] if sat else None, defects[-1] if defects else None)}


def plain_english(req, status, matched, defect):
    what = {'itemwise_reference_exact': 'items recomputed by an independent reference and compared exactly', 'whole_result_reference': 'the whole result recomputed by a reference within the declared tolerance',
            'analytical_invariants': 'structural invariants only (no recomputation)', 'replica_agreement': 'the same code re-run on another backend agreed (not an independent implementation)',
            'same_implementation_regression': 'recomputed by the same implementation (regression check, not independent)', 'witness_check': 'every ambiguity witness and separation re-decided by an independent checker',
            'legacy_exact_replay': 'frozen deterministic task replayed and compared with its registered hash', 'final_transition_reference': 'only the final transition recomputed; the earlier trajectory is unchecked',
            'producer_sample_reevaluation': 'producer-stored samples re-evaluated; the sampler mapping itself was not regenerated'}[req['claim_type']]
    cov = {'every_item': 'every item', 'sampled_items': 'a challenge-selected sample of items', 'whole_result': 'the whole result', 'final_transition': 'the final transition only', 'invariants_only': 'invariants only', 'producer_stored_samples': 'producer-stored samples'}[req['coverage']]
    head = 'Required: %s over %s.' % (what, cov)
    if status == 'satisfied':
        return head + ' Checked: verification %s established exactly this claim on this evidence root.' % matched['verification_id']
    if status == 'failed':
        return head + ' A check with the same bindings (%s) detected a defect: the requirement is NOT met.' % defect['verification_id']
    if status == 'pending':
        return head + ' A matching check is still running.'
    if status == 'inconclusive':
        return head + ' The matching check did not complete; incomplete assurance is not a pass.'
    if status == 'unsupported':
        return head + ' No installed check can establish this claim for this operation.'
    return head + ' Nothing has established it yet; absence of a check is never success.'


def preview(req, statements, binding):
    """Requirements, available evidence, satisfied clauses, missing clauses and next checks (the UI/API preview)."""
    ev = evaluate(req, statements, binding)
    return {'requirement': req, 'requirement_digest': ev['requirement_digest'], 'available_evidence': [{'verification_id': s.get('verification_id'), 'class': s.get('class'), 'claim_type': s.get('claim_type'), 'coverage': s.get('coverage'), 'outcome': s.get('outcome')} for s in statements],
            'status': ev['status'], 'satisfied': ev['satisfied'], 'satisfied_clauses': [c['clause'] for cand in ev['candidates'] for c in cand['clauses'] if c['ok']] if ev['matched_statement'] else [], 'missing_clauses': ev['missing_clauses'],
            'candidates': ev['candidates'], 'next_checks': ev['next_checks'], 'plain': ev['plain'], 'reason': ev['reason']}
