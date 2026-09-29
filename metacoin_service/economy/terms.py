"""WorkTerms v1 (`metacoin-work-terms/v1`): the machine-readable agreement for one unit of judged work.

What it binds (Order 08 §11): requester identity, provider eligibility, the frozen operation (kind, input root, model and
verifier identities from the underlying job contract), typed deliverables with schemas, a declarative acceptance policy
(a bounded program over trusted validators, never uploaded code), an honest-negative payment rule, privacy terms,
integer budget with explicit asset and scale, deadlines, a bounded milestone DAG, failure treatment, dispute policy,
delegation and reassignment permissions. Unknown consequential fields FAIL validation; nothing is silently ignored.

Four state dimensions stay separate everywhere (§13): execution (job), science (outcome), acceptance (decision),
payment (entitlement / intent). This module is pure: validation, canonical digests, structured differences with a
schema-defined consequential/cosmetic classification, an upgrade preview from WorkContract v0, and reusable templates.
"""
import hashlib
import json

from experiments.private_receipts import receipt as merkle
from experiments.work_contracts import contract as v0_terms, energy_analysis as energy
from ..errors import ServiceError

SCHEMA = 'metacoin-work-terms/v1'
POLICY_SCHEMA = 'metacoin-acceptance-policy/v1'
DIGEST_TAG = b'metacoin/work-terms/v1\0'
LIMITS = {'milestones': 16, 'deliverables': 16, 'predicates': 24, 'title_chars': 128, 'text_chars': 2000, 'list_items': 32, 'policy_bytes': 32 * 1024, 'terms_bytes': 128 * 1024}

# Typed deliverables (§12): what can be checked automatically and what needs an explicitly authorized reviewer.
DELIVERABLE_TYPES = {
    'exact_task_output': {'kinds': ('legacy_task_replay',), 'automatic': ('artifact_complete', 'exact_output_hash', 'source_revision', 'schema_valid'), 'review_required': False,
                          'claim': 'canonical output of a frozen deterministic task compared exactly with its registered hash (legacy exact protocol rule, unchanged)'},
    'determination': {'kinds': ('energy_audit', 'temporal_energy', 'safe_runtime', 'task_selection'), 'automatic': ('artifact_complete', 'outcome_in', 'verification_passed', 'source_revision', 'schema_valid'), 'review_required': False,
                      'claim': 'a valid FEASIBLE / INFEASIBLE / INDETERMINATE determination under the declared exact model; a negative is a first-class deliverable'},
    'numerical_witness': {'kinds': ('resource_plan',), 'automatic': ('artifact_complete', 'status_in', 'verification_passed', 'source_revision', 'schema_valid'), 'review_required': False,
                          'claim': 'a schedule/witness checked exactly by the independent integer simulator under the declared model; global optimality is claimed only with the solver status that says so'},
    'reproducible_workflow_result': {'kinds': ('temporal_batch', 'monte_carlo_reliability', 'heat_diffusion', 'calibration_fit'), 'automatic': ('artifact_complete', 'verification_passed', 'source_revision', 'schema_valid', 'numerical_tolerance'), 'review_required': False,
                                     'claim': 'numerical result replayed or audited under its manifest class and declared tolerance (not the legacy exact rule)'},
    'reviewed_dataset_transformation': {'kinds': ('document_import',), 'automatic': ('artifact_complete', 'source_revision'), 'review_required': True,
                                        'claim': 'a dataset transformation whose mapping a designated reviewer confirmed; automation checks completeness and provenance only'},
    'evidence_linked_report': {'kinds': ('analysis_report',), 'automatic': ('artifact_complete', 'source_revision'), 'review_required': True,
                               'claim': 'a report whose numbers are checked against evidence; interpretation requires an authorized reviewer'},
    'bounded_model_explanation': {'kinds': ('text_generation',), 'automatic': ('artifact_complete', 'schema_valid'), 'review_required': True,
                                  'claim': 'model output is data; only evidence checks and an authorized review can make it acceptable'},
}
PREDICATE_TYPES = ('artifact_complete', 'outcome_in', 'status_in', 'verification_passed', 'exact_output_hash', 'source_revision', 'schema_valid', 'numerical_tolerance', 'review_signature', 'invariants')
OUTCOME_TREATMENTS = ('accept', 'accept_diagnostic', 'reject')
PAYMENT_CLASSES = ('complete', 'partial', 'diagnostic')
SELECTION_POLICIES = ('lowest_eligible_price', 'weighted', 'manual')
TIE_BREAKS = ('earliest_offer', 'provider_id', 'shortest_window')
FAILURE_PATHS = ('stop_downstream', 'cancel_dependents', 'continue_independent')
REASSIGN_CONDITIONS = ('missed_acknowledgement', 'terminal_failure', 'requester_withdrawal')
RESOLVERS = ('designated_reviewer', 'requester_reviewer_pair', 'deterministic_replay')
ASSETS = {'action-units': {'scale': 0, 'rail': 'application journal (budget units of the workspace action asset)'},
          'local-chain-token': {'scale': 0, 'rail': 'private py-evm chain (x402 upto/exact on pinned contracts); synthetic, never production'}}
SCHEMES = ('exact', 'upto')
VERIFICATION_CLASSES = ('full_exact', 'full_reference', 'analytical', 'sampled_reference', 'replica', 'none')
CONSEQUENTIAL_TOP = ('operation', 'deliverables', 'acceptance', 'payment', 'privacy', 'deadlines', 'eligibility', 'selection', 'milestones', 'failure_treatment', 'dispute', 'delegation', 'reassignment', 'max_awards', 'requester')
COSMETIC_TOP = ('title', 'purpose', 'notes')
TOP_FIELDS = ('schema',) + CONSEQUENTIAL_TOP + COSMETIC_TOP


def _err(code, **detail):
    raise ServiceError('VALIDATION', dict({'code': code}, **detail))


def _int(v, lo, hi, code):
    if type(v) is not int or isinstance(v, bool) or not lo <= v <= hi:
        _err(code, allowed='integer %d..%d' % (lo, hi))
    return v


def _str(v, code, maxlen=LIMITS['text_chars'], allow_empty=False):
    if type(v) is not str or (not allow_empty and not v) or len(v) > maxlen:
        _err(code, allowed='string of at most %d characters' % maxlen)
    return v


def _keys(obj, allowed, code, required=()):
    if type(obj) is not dict:
        _err(code, allowed='object')
    unknown = set(obj) - set(allowed)
    if unknown:
        _err(code + '_unknown_field', unknown=sorted(unknown), note='unknown consequential fields fail validation; nothing is silently ignored')
    missing = [k for k in required if k not in obj]
    if missing:
        _err(code + '_missing_field', missing=missing)
    return obj


def _enum_list(v, allowed, code, minimum=1):
    if type(v) is not list or len(v) < minimum or len(v) > LIMITS['list_items'] or len(set(v)) != len(v) or not set(v) <= set(allowed):
        _err(code, allowed=list(allowed))
    return v


# ---- validation -----------------------------------------------------------------------------------------------------
def validate_policy(policy, deliverable_types):
    """Acceptance policy: a bounded declarative program over trusted validators (§14). Never executable code."""
    _keys(policy, ('schema', 'predicates', 'outcomes', 'valid_negative', 'required_verification', 'payment_rule'), 'acceptance', required=('schema', 'predicates', 'outcomes', 'required_verification', 'payment_rule'))
    if policy['schema'] != POLICY_SCHEMA:
        _err('acceptance_schema', allowed=[POLICY_SCHEMA])
    if len(merkle.canonical(policy)) > LIMITS['policy_bytes']:
        _err('acceptance_policy_too_large', max_bytes=LIMITS['policy_bytes'])
    preds = policy['predicates']
    if type(preds) is not list or not 1 <= len(preds) <= LIMITS['predicates']:
        _err('acceptance_predicates', allowed='1..%d predicates' % LIMITS['predicates'])
    seen = set()
    for p in preds:
        _keys(p, ('id', 'type', 'params'), 'predicate', required=('id', 'type'))
        _str(p['id'], 'predicate_id', 64)
        if p['id'] in seen:
            _err('predicate_id_duplicate', id=p['id'])
        seen.add(p['id'])
        if p['type'] not in PREDICATE_TYPES:
            _err('predicate_type', allowed=list(PREDICATE_TYPES))
        params = p.get('params', {})
        if type(params) is not dict:
            _err('predicate_params')
        if p['type'] == 'outcome_in':
            _enum_list(params.get('outcomes'), energy.OUTCOMES, 'predicate_outcomes')
        if p['type'] == 'status_in':
            from ..compute import resource_plan as _rp
            _enum_list(params.get('statuses'), _rp.STATUSES, 'predicate_statuses')
        if p['type'] == 'verification_passed':
            _keys(params, ('class', 'distinct_verifier', 'verifier_digest', 'min_sample_count'), 'predicate_verification', required=('class',))
            if params['class'] not in VERIFICATION_CLASSES[:-1]:
                _err('predicate_verification_class', allowed=list(VERIFICATION_CLASSES[:-1]))
        if p['type'] == 'exact_output_hash':
            _keys(params, ('registered_hash', 'field'), 'predicate_exact', required=('registered_hash',))
            merkle._hex(params['registered_hash'])
        if p['type'] == 'numerical_tolerance':
            _keys(params, ('field', 'expected', 'abs_tol', 'unit'), 'predicate_tolerance', required=('field', 'expected', 'abs_tol', 'unit'))
            _str(params['field'], 'predicate_field', 64); _str(params['unit'], 'predicate_unit', 32)
            for k in ('expected', 'abs_tol'):
                if type(params[k]) not in (int, float) or isinstance(params[k], bool):
                    _err('predicate_tolerance_number', field=k)
            if params['abs_tol'] < 0:
                _err('predicate_tolerance_negative')
        if p['type'] == 'review_signature':
            _keys(params, ('decision',), 'predicate_review')
        if p['type'] == 'invariants':
            _enum_list(params.get('names'), ('margin_ordering', 'reserve_nonnegative', 'outcome_consistent'), 'predicate_invariants')
        if p['type'] == 'schema_valid':
            _keys(params, ('result_schema', 'model_id'), 'predicate_schema')
    outcomes = policy['outcomes']
    if type(outcomes) is not dict or not outcomes or not set(outcomes) <= set(energy.OUTCOMES) | {'not_applicable'} or not set(outcomes.values()) <= set(OUTCOME_TREATMENTS):
        _err('acceptance_outcomes', allowed={'keys': list(energy.OUTCOMES) + ['not_applicable'], 'values': list(OUTCOME_TREATMENTS)})
    neg = policy.get('valid_negative', {'requires': ['verification_passed'], 'note': ''})
    _keys(neg, ('requires', 'note'), 'valid_negative', required=('requires',))
    _enum_list(neg['requires'], ('verification_passed', 'artifact_complete', 'review_signature', 'source_revision'), 'valid_negative_requires')
    rv = policy['required_verification']
    _keys(rv, ('class', 'distinct_verifier', 'verifier_digest', 'min_sample_count'), 'required_verification', required=('class', 'distinct_verifier'))
    if rv['class'] not in VERIFICATION_CLASSES:
        _err('required_verification_class', allowed=list(VERIFICATION_CLASSES))
    if type(rv['distinct_verifier']) is not bool:
        _err('required_verification_distinct')
    if rv.get('verifier_digest') is not None:
        merkle._hex(rv['verifier_digest'])
    if 'min_sample_count' in rv and rv['min_sample_count'] is not None:
        _int(rv['min_sample_count'], 1, 4096, 'required_verification_min_sample_count')
    pr = policy['payment_rule']
    _keys(pr, ('complete', 'partial', 'diagnostic', 'outcome_neutral'), 'payment_rule', required=('complete', 'partial', 'diagnostic', 'outcome_neutral'))
    for k in PAYMENT_CLASSES:
        _int(pr[k], 0, 10 ** 15, 'payment_rule_' + k)
    if type(pr['outcome_neutral']) is not bool:
        _err('payment_rule_outcome_neutral')
    if pr['outcome_neutral'] and any(t == 'accept' for t in outcomes.values()) and len({t for t in outcomes.values()}) > 1 and 'INFEASIBLE' in outcomes and outcomes.get('INFEASIBLE') == 'reject' and outcomes.get('FEASIBLE') == 'accept':
        _err('payment_rule_contradiction', note='outcome_neutral cannot be combined with rejecting the negative outcome')
    if any(t in ('accept', 'accept_diagnostic') for t in outcomes.values()) and pr['complete'] == 0 and pr['diagnostic'] == 0 and pr['partial'] == 0:
        pass  # zero-price determinations are permitted (e.g. treasury-funded verification work priced separately)
    ok_types = {t for dt in deliverable_types for t in DELIVERABLE_TYPES[dt]['automatic']} | {'review_signature', 'invariants', 'numerical_tolerance'}
    bad = [p['id'] for p in preds if p['type'] not in ok_types]
    if bad:
        _err('predicate_not_applicable_to_deliverables', predicates=bad, deliverable_types=sorted(deliverable_types))
    return policy


def validate(terms, mode='draft'):
    """Structural + semantic validation. mode='draft' allows `operation` without frozen bindings; mode='frozen' requires them."""
    raw = merkle.canonical(terms)
    if len(raw) > LIMITS['terms_bytes']:
        _err('terms_too_large', max_bytes=LIMITS['terms_bytes'])
    _keys(terms, TOP_FIELDS, 'terms', required=('schema', 'title', 'requester', 'operation', 'deliverables', 'acceptance', 'payment', 'privacy', 'deadlines', 'eligibility', 'selection', 'milestones', 'failure_treatment', 'dispute', 'delegation', 'reassignment', 'max_awards'))
    if terms['schema'] != SCHEMA:
        _err('terms_schema', allowed=[SCHEMA])
    _str(terms['title'], 'title', LIMITS['title_chars'])
    if 'notes' in terms:
        _str(terms['notes'], 'notes', allow_empty=True)
    if 'purpose' in terms:
        _keys(terms['purpose'], ('mission_id', 'node', 'question', 'affects', 'evidence_needed', 'source_revision'), 'purpose')
        for k in ('mission_id', 'node', 'question', 'evidence_needed', 'source_revision'):
            if k in terms['purpose']:
                _str(terms['purpose'][k], 'purpose_' + k, 256 if k != 'question' else LIMITS['text_chars'])
        if 'affects' in terms['purpose']:
            if type(terms['purpose']['affects']) is not list or len(terms['purpose']['affects']) > LIMITS['list_items'] or not all(type(x) is str and len(x) <= 128 for x in terms['purpose']['affects']):
                _err('purpose_affects')
    _keys(terms['requester'], ('principal_id', 'workspace'), 'requester', required=('principal_id', 'workspace'))
    op = terms['operation']
    _keys(op, ('kind', 'contract_id', 'contract_digest', 'input_root', 'inputs_digest', 'model_id', 'verifier_id', 'verifier_digest', 'implementation', 'compatibility'), 'operation', required=('kind',))
    from .. import contracts as contracts_mod
    if op['kind'] not in contracts_mod.KINDS + ('legacy_task_replay', 'analysis_report'):
        _err('operation_kind', allowed=list(contracts_mod.KINDS) + ['legacy_task_replay', 'analysis_report'])
    comp = op.get('compatibility', 'same_verifier_digest')
    if comp not in ('same_verifier_digest', 'same_model_id', 'declared_equivalent'):
        _err('operation_compatibility', allowed=['same_verifier_digest', 'same_model_id', 'declared_equivalent'])
    if mode == 'frozen':
        for k in ('contract_id', 'contract_digest', 'input_root', 'model_id', 'verifier_id', 'verifier_digest'):
            if k not in op or op[k] is None:
                _err('operation_unbound', field=k, note='freeze binds the operation to a frozen job contract before dispatch')
        merkle._hex(op['contract_digest']); merkle._hex(op['input_root'])
    dels = terms['deliverables']
    if type(dels) is not list or not 1 <= len(dels) <= LIMITS['deliverables']:
        _err('deliverables', allowed='1..%d typed deliverables' % LIMITS['deliverables'])
    dkeys, dtypes = set(), set()
    for d in dels:
        _keys(d, ('key', 'type', 'schema', 'required', 'max_bytes', 'media', 'partial_allowed', 'retention_seconds', 'provenance'), 'deliverable', required=('key', 'type', 'required'))
        _str(d['key'], 'deliverable_key', 64)
        if d['key'] in dkeys:
            _err('deliverable_key_duplicate', key=d['key'])
        dkeys.add(d['key'])
        if d['type'] not in DELIVERABLE_TYPES:
            _err('deliverable_type', allowed=list(DELIVERABLE_TYPES))
        if op['kind'] not in DELIVERABLE_TYPES[d['type']]['kinds']:
            _err('deliverable_type_kind_mismatch', type=d['type'], kind=op['kind'], allowed_kinds=list(DELIVERABLE_TYPES[d['type']]['kinds']))
        dtypes.add(d['type'])
        if type(d['required']) is not bool:
            _err('deliverable_required')
        if 'schema' in d:
            _str(d['schema'], 'deliverable_schema', 128)
        if 'max_bytes' in d:
            _int(d['max_bytes'], 1, 64 * 1024 * 1024, 'deliverable_max_bytes')
        if 'media' in d:
            _enum_list(d['media'], ('application/json', 'application/x-npy', 'text/markdown', 'application/pdf', 'image/svg+xml'), 'deliverable_media')
        if 'partial_allowed' in d and type(d['partial_allowed']) is not bool:
            _err('deliverable_partial_allowed')
        if 'retention_seconds' in d:
            _int(d['retention_seconds'], 3600, 10 * 365 * 86400, 'deliverable_retention')
        if 'provenance' in d:
            _keys(d['provenance'], ('bind_inputs', 'bind_method'), 'deliverable_provenance')
    validate_policy(terms['acceptance'], dtypes)
    review_needed = any(DELIVERABLE_TYPES[t]['review_required'] for t in dtypes)
    if review_needed and not any(p['type'] == 'review_signature' for p in terms['acceptance']['predicates']):
        _err('review_signature_required', note='these deliverable types need an explicitly authorized reviewer; add a review_signature predicate')
    pay = terms['payment']
    _keys(pay, ('asset', 'scale', 'ceiling', 'scheme', 'verifier_compensation', 'currency_note'), 'payment', required=('asset', 'scale', 'ceiling', 'scheme'))
    if pay['asset'] not in ASSETS:
        _err('payment_asset', allowed=list(ASSETS))
    if pay['scale'] != ASSETS[pay['asset']]['scale']:
        _err('payment_scale', allowed=ASSETS[pay['asset']]['scale'])
    _int(pay['ceiling'], 0, 10 ** 15, 'payment_ceiling')
    if pay['scheme'] not in SCHEMES:
        _err('payment_scheme', allowed=list(SCHEMES))
    _int(pay.get('verifier_compensation', 0), 0, 10 ** 15, 'verifier_compensation')
    pr = terms['acceptance']['payment_rule']
    if max(pr['complete'], pr['partial'], pr['diagnostic']) + pay.get('verifier_compensation', 0) > pay['ceiling']:
        _err('payment_rule_exceeds_ceiling', ceiling=pay['ceiling'], note='provider and verifier obligations must fit under the ceiling before dispatch')
    priv = terms['privacy']
    _keys(priv, ('inputs', 'evidence_disclosure', 'verifier_access', 'projection_default', 'provider_retention_seconds'), 'privacy', required=('inputs', 'evidence_disclosure', 'verifier_access', 'projection_default'))
    if priv['inputs'] not in ('requester_private', 'shared_with_provider_after_award', 'public_synthetic'):
        _err('privacy_inputs', allowed=['requester_private', 'shared_with_provider_after_award', 'public_synthetic'])
    _enum_list(priv['evidence_disclosure'], ('bindings', 'outcome', 'summary', 'explanation', 'verification_statement'), 'privacy_evidence_disclosure')
    if priv['verifier_access'] not in ('required_inputs_only', 'full_private', 'none'):
        _err('privacy_verifier_access')
    if priv['projection_default'] not in ('bindings_only', 'outcome_and_bindings', 'summary'):
        _err('privacy_projection_default')
    dl = terms['deadlines']
    _keys(dl, ('offer_seconds', 'acknowledge_seconds', 'delivery_seconds', 'dispute_seconds', 'at_deadline'), 'deadlines', required=('offer_seconds', 'acknowledge_seconds', 'delivery_seconds', 'dispute_seconds'))
    for k, hi in (('offer_seconds', 90 * 86400), ('acknowledge_seconds', 30 * 86400), ('delivery_seconds', 365 * 86400), ('dispute_seconds', 365 * 86400)):
        _int(dl[k], 1, hi, 'deadline_' + k)
    if dl.get('at_deadline', 'eligible') not in ('eligible', 'ineligible'):
        _err('deadline_at_deadline', allowed=['eligible', 'ineligible'], note='an action at the exact deadline second is eligible (inclusive) or not (exclusive); applied consistently')
    el = terms['eligibility']
    _keys(el, ('capabilities', 'verification_classes', 'payment_schemes', 'privacy_modes', 'operator_relationships', 'providers', 'execution_types'), 'eligibility', required=('capabilities', 'verification_classes', 'payment_schemes'))
    if type(el['capabilities']) is not list or not el['capabilities'] or not all(type(x) is str and len(x) <= 64 for x in el['capabilities']) or op['kind'] not in el['capabilities']:
        _err('eligibility_capabilities', note='must include the operation kind')
    _enum_list(el['verification_classes'], VERIFICATION_CLASSES, 'eligibility_verification_classes')
    _enum_list(el['payment_schemes'], SCHEMES, 'eligibility_payment_schemes')
    if 'privacy_modes' in el:
        _enum_list(el['privacy_modes'], ('requester_private', 'shared_with_provider_after_award', 'public_synthetic'), 'eligibility_privacy_modes')
    if 'operator_relationships' in el:
        _enum_list(el['operator_relationships'], ('same_operator', 'affiliated', 'independent_declared', 'unknown'), 'eligibility_operator_relationships')
    if 'execution_types' in el:
        _enum_list(el['execution_types'], ('local_worker', 'node'), 'eligibility_execution_types')
    if 'providers' in el and (type(el['providers']) is not list or len(el['providers']) > LIMITS['list_items'] or not all(type(x) is str for x in el['providers'])):
        _err('eligibility_providers')
    sel = terms['selection']
    _keys(sel, ('policy', 'tie_break', 'weights', 'manual_reason_required'), 'selection', required=('policy', 'tie_break'))
    if sel['policy'] not in SELECTION_POLICIES:
        _err('selection_policy', allowed=list(SELECTION_POLICIES))
    _enum_list(sel['tie_break'], TIE_BREAKS, 'selection_tie_break')
    if sel['policy'] == 'weighted':
        w = sel.get('weights')
        _keys(w or {}, ('price', 'window_seconds', 'verification_class_rank'), 'selection_weights', required=('price', 'window_seconds', 'verification_class_rank'))
        for k in w:
            _int(w[k], 0, 1000, 'selection_weight_' + k)
        if sum(w.values()) == 0:
            _err('selection_weights_zero')
    ms = terms['milestones']
    if type(ms) is not list or not 1 <= len(ms) <= LIMITS['milestones']:
        _err('milestones', allowed='1..%d' % LIMITS['milestones'])
    mkeys = []
    for m in ms:
        _keys(m, ('key', 'deliverables', 'max_payment', 'depends_on', 'deadline_seconds', 'on_failure', 'consume_partial', 'requires_acceptance_of', 'operation'), 'milestone', required=('key', 'deliverables', 'max_payment', 'depends_on', 'deadline_seconds', 'on_failure'))
        _str(m['key'], 'milestone_key', 64)
        if m['key'] in mkeys:
            _err('milestone_key_duplicate', key=m['key'])
        mkeys.append(m['key'])
        if type(m['deliverables']) is not list or not m['deliverables'] or not set(m['deliverables']) <= dkeys:
            _err('milestone_deliverables', allowed=sorted(dkeys))
        _int(m['max_payment'], 0, 10 ** 15, 'milestone_max_payment')
        _int(m['deadline_seconds'], 1, 365 * 86400, 'milestone_deadline')
        if m['on_failure'] not in FAILURE_PATHS:
            _err('milestone_on_failure', allowed=list(FAILURE_PATHS))
        if 'consume_partial' in m and type(m['consume_partial']) is not bool:
            _err('milestone_consume_partial')
        if type(m['depends_on']) is not list or len(m['depends_on']) > LIMITS['list_items']:
            _err('milestone_depends_on')
        if 'operation' in m:
            _keys(m['operation'], ('contract_id', 'contract_digest', 'input_root', 'kind'), 'milestone_operation', required=('contract_id', 'contract_digest', 'input_root'))
            merkle._hex(m['operation']['contract_digest']); merkle._hex(m['operation']['input_root'])
    for m in ms:
        if not set(m['depends_on']) <= set(mkeys) or m['key'] in m['depends_on']:
            _err('milestone_dependency_unknown', milestone=m['key'], allowed=mkeys)
        if 'requires_acceptance_of' in m and (type(m['requires_acceptance_of']) is not dict or not set(m['requires_acceptance_of']) <= set(m['depends_on']) or not set(m['requires_acceptance_of'].values()) <= {'accepted', 'accepted_or_valid_negative', 'accepted_positive', 'any_terminal'}):
            _err('milestone_requires_acceptance_of', allowed=['accepted', 'accepted_or_valid_negative', 'accepted_positive', 'any_terminal'])
    from ..workflows import kahn_order
    order, cyclic = kahn_order(mkeys, {m['key']: m['depends_on'] for m in ms})
    if cyclic:
        _err('milestone_cycle', milestones=cyclic)
    total = sum(m['max_payment'] for m in ms)
    if total > pay['ceiling']:
        _err('milestones_exceed_ceiling', total=total, ceiling=pay['ceiling'], note='milestone commitments are reserved once under the contract ceiling; they may not sum above it')
    ft = terms['failure_treatment']
    _keys(ft, ('execution_failure', 'invalid_evidence', 'missed_deadline', 'partial_fee_rule'), 'failure_treatment', required=('execution_failure', 'invalid_evidence', 'missed_deadline'))
    for k in ('execution_failure', 'invalid_evidence'):
        if ft[k] not in ('no_payment', 'diagnostic_fee_if_evidence'):
            _err('failure_treatment_' + k, allowed=['no_payment', 'diagnostic_fee_if_evidence'])
    if ft['missed_deadline'] not in ('cancel', 'reassign_permitted', 'extend_by_amendment_only'):
        _err('failure_treatment_missed_deadline', allowed=['cancel', 'reassign_permitted', 'extend_by_amendment_only'])
    if 'partial_fee_rule' in ft:
        _str(ft['partial_fee_rule'], 'partial_fee_rule', 256)
    dp = terms['dispute']
    _keys(dp, ('window_seconds', 'resolver', 'appeal', 'max_entries', 'at_deadline'), 'dispute', required=('window_seconds', 'resolver', 'appeal', 'max_entries'))
    _int(dp['window_seconds'], 60, 365 * 86400, 'dispute_window')
    if dp['resolver'] not in RESOLVERS:
        _err('dispute_resolver', allowed=list(RESOLVERS))
    if type(dp['appeal']) is not bool:
        _err('dispute_appeal')
    _int(dp['max_entries'], 1, 64, 'dispute_max_entries')
    if dp.get('at_deadline', 'close_unresolved') not in ('close_unresolved', 'accept_last_decision'):
        _err('dispute_at_deadline', allowed=['close_unresolved', 'accept_last_decision'])
    dg = terms['delegation']
    _keys(dg, ('allowed', 'max_depth', 'max_nodes', 'max_sub_budget', 'allowed_providers', 'artifact_scope'), 'delegation', required=('allowed',))
    if type(dg['allowed']) is not bool:
        _err('delegation_allowed')
    if dg['allowed']:
        _int(dg.get('max_depth', 1), 1, 3, 'delegation_max_depth'); _int(dg.get('max_nodes', 1), 1, 8, 'delegation_max_nodes'); _int(dg.get('max_sub_budget', 0), 0, pay['ceiling'], 'delegation_max_sub_budget')
        if 'allowed_providers' in dg and (type(dg['allowed_providers']) is not list or not all(type(x) is str for x in dg['allowed_providers'])):
            _err('delegation_allowed_providers')
        if dg.get('artifact_scope', 'derived_inputs_only') not in ('derived_inputs_only', 'declared_subtask_inputs'):
            _err('delegation_artifact_scope')
    ra = terms['reassignment']
    _keys(ra, ('allowed', 'conditions', 'physical_effects'), 'reassignment', required=('allowed', 'conditions'))
    if type(ra['allowed']) is not bool:
        _err('reassignment_allowed')
    _enum_list(ra['conditions'], REASSIGN_CONDITIONS, 'reassignment_conditions', minimum=0)
    if ra.get('physical_effects', 'none_repeatable_computation') not in ('none_repeatable_computation', 'possible_require_declared_policy'):
        _err('reassignment_physical_effects')
    _int(terms['max_awards'], 1, 8, 'max_awards')
    return terms


def digest(terms):
    validate(terms, 'frozen')
    return hashlib.sha256(DIGEST_TAG + merkle.canonical(terms)).hexdigest()


def draft_digest(terms):
    return hashlib.sha256(b'metacoin/work-terms-draft/v1\0' + merkle.canonical(terms)).hexdigest()


# ---- structured differences (§17) -----------------------------------------------------------------------------------
def _flatten(obj, prefix=''):
    out = {}
    if isinstance(obj, dict):
        for k in sorted(obj):
            out.update(_flatten(obj[k], prefix + ('.' if prefix else '') + k))
    elif isinstance(obj, list):
        out[prefix] = merkle.canonical(obj).decode()
    else:
        out[prefix] = obj
    return out


def compare(old, new):
    """Field-level differences with a classification that follows the schema, not an opinion: any change under a
    consequential top-level field is consequential; title/purpose/notes are cosmetic."""
    a, b = _flatten(old), _flatten(new)
    changes = []
    for path in sorted(set(a) | set(b)):
        if a.get(path) != b.get(path):
            top = path.split('.')[0]
            changes.append({'path': path, 'from': a.get(path), 'to': b.get(path), 'classification': 'consequential' if top in CONSEQUENTIAL_TOP else 'cosmetic' if top in COSMETIC_TOP else 'consequential'})
    return {'changes': changes, 'consequential': any(c['classification'] == 'consequential' for c in changes), 'count': len(changes),
            'rule': 'consequential = any change under ' + ', '.join(CONSEQUENTIAL_TOP) + '; cosmetic = ' + ', '.join(COSMETIC_TOP) + '. A consequential amendment needs a new agreement before new work uses it.'}


# ---- inspection and upgrade preview (§11) ---------------------------------------------------------------------------
V0_FIELDS = ('schema', 'job_id', 'owner', 'auditor', 'input_authority', 'input_root', 'commitment_schema', 'evidence_kind', 'verifier_id', 'verifier_digest', 'result_schema', 'model_id', 'units', 'assumptions', 'uncertainty', 'domain',
             'accepted_outcomes', 'allowed_disclosures', 'required_disclosures', 'action', 'expires_at', 'dispute', 'retention_seconds', 'access')


def inspect(terms):
    """Human/agent-readable summary of what will count as delivery, before resources are reserved. No hidden defaults."""
    validate(terms)
    pol = terms['acceptance']
    return {'schema': SCHEMA, 'title': terms['title'], 'operation': terms['operation'], 'requester': terms['requester'],
            'deliverables': [{'key': d['key'], 'type': d['type'], 'required': d['required'], 'automatic_checks': list(DELIVERABLE_TYPES[d['type']]['automatic']), 'review_required': DELIVERABLE_TYPES[d['type']]['review_required'], 'claim': DELIVERABLE_TYPES[d['type']]['claim']} for d in terms['deliverables']],
            'what_counts_as_delivery': {'predicates': [{'id': p['id'], 'type': p['type'], 'params': p.get('params', {})} for p in pol['predicates']], 'outcomes': pol['outcomes'], 'valid_negative': pol.get('valid_negative'),
                                        'required_verification': pol['required_verification'], 'payment_rule': pol['payment_rule']},
            'payment': terms['payment'], 'milestones': [{'key': m['key'], 'max_payment': m['max_payment'], 'depends_on': m['depends_on'], 'on_failure': m['on_failure']} for m in terms['milestones']],
            'deadlines': terms['deadlines'], 'privacy': terms['privacy'], 'eligibility': terms['eligibility'], 'selection': terms['selection'], 'failure_treatment': terms['failure_treatment'], 'dispute': terms['dispute'],
            'delegation': terms['delegation'], 'reassignment': terms['reassignment'], 'max_awards': terms['max_awards'],
            'state_dimensions': {'execution': ['queued', 'running', 'completed', 'failed', 'cancelled', 'unknown'], 'science': list(energy.OUTCOMES) + ['not_applicable'], 'acceptance': ['pending', 'accepted', 'rejected', 'disputed', 'superseded'], 'payment': ['none', 'reserved', 'payable', 'authorized', 'submitted', 'settled', 'failed', 'unknown', 'void']},
            'hidden_defaults': 'none: every consequential field is explicit in the frozen terms'}


def upgrade_preview(v0_contract):
    """What WorkTerms v1 adds over a WorkContract v0 (energy determination) and whether conversion needs a new agreement."""
    v0_terms.validate(v0_contract, mode='historical')
    added = ['typed deliverables with schemas', 'declarative acceptance policy with predicate trace', 'honest-negative payment rule (complete/partial/diagnostic)', 'milestone DAG with acceptance-gated dependencies',
             'provider eligibility, selection policy, offers and awards', 'deadlines with explicit boundary rule', 'delegation and reassignment permissions', 'dispute workflow policy', 'integer payment with asset/scale and scheme']
    carried = {'input_root': v0_contract['input_root'], 'verifier_id': v0_contract['verifier_id'], 'verifier_digest': v0_contract['verifier_digest'], 'model_id': v0_contract['model_id'], 'accepted_outcomes': v0_contract['accepted_outcomes'],
               'disclosures': v0_contract['required_disclosures'], 'expires_at': v0_contract['expires_at'], 'retention_seconds': v0_contract['retention_seconds']}
    return {'from': v0_terms.SCHEMA, 'to': SCHEMA, 'adds': added, 'carried_over': carried, 'requires_new_agreement': True,
            'reason': 'v1 adds consequential fields (payment rule, acceptance predicates, deadlines) that v0 never agreed; the v0 contract stays valid under its own semantics and is never rewritten',
            'v0_semantics_preserved': 'digest metacoin/work-contract/v0, accepted_outcomes and disclosure policy unchanged'}


# ---- templates (§76-5): reusable determination / infeasibility-witness / independent-replay / diagnostic-delivery ----
def _base(requester, workspace, kind, title, ceiling, asset='action-units'):
    return {'schema': SCHEMA, 'title': title, 'requester': {'principal_id': requester, 'workspace': workspace}, 'operation': {'kind': kind, 'compatibility': 'same_verifier_digest'},
            'payment': {'asset': asset, 'scale': ASSETS[asset]['scale'], 'ceiling': ceiling, 'scheme': 'exact', 'verifier_compensation': 0},
            'privacy': {'inputs': 'requester_private', 'evidence_disclosure': ['bindings', 'outcome'], 'verifier_access': 'required_inputs_only', 'projection_default': 'outcome_and_bindings'},
            'deadlines': {'offer_seconds': 86400, 'acknowledge_seconds': 3600, 'delivery_seconds': 86400, 'dispute_seconds': 7 * 86400, 'at_deadline': 'eligible'},
            'eligibility': {'capabilities': [kind], 'verification_classes': ['full_exact', 'full_reference'], 'payment_schemes': ['exact', 'upto'], 'operator_relationships': ['same_operator', 'affiliated', 'independent_declared', 'unknown'], 'execution_types': ['local_worker', 'node']},
            'selection': {'policy': 'lowest_eligible_price', 'tie_break': ['earliest_offer', 'provider_id']},
            'failure_treatment': {'execution_failure': 'no_payment', 'invalid_evidence': 'no_payment', 'missed_deadline': 'reassign_permitted'},
            'dispute': {'window_seconds': 7 * 86400, 'resolver': 'designated_reviewer', 'appeal': False, 'max_entries': 20, 'at_deadline': 'close_unresolved'},
            'delegation': {'allowed': False}, 'reassignment': {'allowed': True, 'conditions': ['missed_acknowledgement', 'terminal_failure'], 'physical_effects': 'none_repeatable_computation'}, 'max_awards': 1}


def template(name, requester, workspace, *, ceiling=10, amount=None, kind=None, registered_hash=None, asset='action-units', title=None):
    amount = ceiling if amount is None else amount
    if name == 'determination':
        kind = kind or 'energy_audit'
        t = _base(requester, workspace, kind, title or 'Determination: is the scenario feasible under the declared exact model?', ceiling, asset)
        t['deliverables'] = [{'key': 'determination', 'type': 'determination', 'schema': energy.RESULT_SCHEMA, 'required': True, 'media': ['application/json'], 'provenance': {'bind_inputs': True, 'bind_method': True}}]
        t['acceptance'] = {'schema': POLICY_SCHEMA, 'predicates': [{'id': 'complete', 'type': 'artifact_complete'}, {'id': 'source', 'type': 'source_revision'}, {'id': 'schema', 'type': 'schema_valid'},
                                                                   {'id': 'outcome', 'type': 'outcome_in', 'params': {'outcomes': ['FEASIBLE', 'INFEASIBLE', 'INDETERMINATE']}},
                                                                   {'id': 'replay', 'type': 'verification_passed', 'params': {'class': 'full_exact', 'distinct_verifier': False}}],
                           'outcomes': {'FEASIBLE': 'accept', 'INFEASIBLE': 'accept', 'INDETERMINATE': 'accept_diagnostic'}, 'valid_negative': {'requires': ['verification_passed', 'artifact_complete'], 'note': 'a verified INFEASIBLE earns the same as a verified FEASIBLE'},
                           'required_verification': {'class': 'full_exact', 'distinct_verifier': False}, 'payment_rule': {'complete': amount, 'partial': 0, 'diagnostic': max(amount // 2, 0), 'outcome_neutral': True}}
        t['milestones'] = [{'key': 'm1', 'deliverables': ['determination'], 'max_payment': amount, 'depends_on': [], 'deadline_seconds': 86400, 'on_failure': 'stop_downstream'}]
        return t
    if name == 'infeasibility_witness':
        kind = kind or 'resource_plan'
        t = _base(requester, workspace, kind, title or 'Numerical witness: a checked schedule or an established infeasibility under the declared integer model', ceiling, asset)
        t['deliverables'] = [{'key': 'witness', 'type': 'numerical_witness', 'schema': 'robust-resource-plan/v1', 'required': True, 'media': ['application/json'], 'provenance': {'bind_inputs': True, 'bind_method': True}}]
        t['acceptance'] = {'schema': POLICY_SCHEMA, 'predicates': [{'id': 'complete', 'type': 'artifact_complete'}, {'id': 'source', 'type': 'source_revision'}, {'id': 'schema', 'type': 'schema_valid'},
                                                                   {'id': 'status', 'type': 'status_in', 'params': {'statuses': ['optimal_within_tolerance', 'feasible_incumbent_no_optimality_claim', 'infeasible_by_solver', 'infeasible_established_by_enumeration']}},
                                                                   {'id': 'replay', 'type': 'verification_passed', 'params': {'class': 'full_reference', 'distinct_verifier': False}}],
                           'outcomes': {'not_applicable': 'accept'}, 'valid_negative': {'requires': ['verification_passed'], 'note': 'infeasible_by_solver is accepted only with the independent simulator/oracle audit; limit_no_candidate is never a negative proof'},
                           'required_verification': {'class': 'full_reference', 'distinct_verifier': False}, 'payment_rule': {'complete': amount, 'partial': 0, 'diagnostic': 0, 'outcome_neutral': True}}
        t['milestones'] = [{'key': 'm1', 'deliverables': ['witness'], 'max_payment': amount, 'depends_on': [], 'deadline_seconds': 86400, 'on_failure': 'stop_downstream'}]
        return t
    if name == 'independent_replay':
        kind = kind or 'legacy_task_replay'
        t = _base(requester, workspace, kind, title or 'Independent replay of a frozen deterministic task', ceiling, asset)
        t['deliverables'] = [{'key': 'output', 'type': 'exact_task_output', 'schema': 'legacy-task-canonical-json', 'required': True, 'media': ['application/json'], 'provenance': {'bind_inputs': True, 'bind_method': True}}]
        preds = [{'id': 'complete', 'type': 'artifact_complete'}, {'id': 'source', 'type': 'source_revision'}, {'id': 'schema', 'type': 'schema_valid'}]
        if registered_hash:
            preds.append({'id': 'exact', 'type': 'exact_output_hash', 'params': {'registered_hash': registered_hash}})
        t['acceptance'] = {'schema': POLICY_SCHEMA, 'predicates': preds, 'outcomes': {'not_applicable': 'accept'}, 'valid_negative': {'requires': ['artifact_complete'], 'note': 'a task whose registered verdict is false is replayed exactly; the replay does not change the verdict'},
                           'required_verification': {'class': 'none', 'distinct_verifier': False}, 'payment_rule': {'complete': amount, 'partial': 0, 'diagnostic': 0, 'outcome_neutral': True}}
        t['eligibility']['verification_classes'] = ['full_exact', 'none']
        t['milestones'] = [{'key': 'm1', 'deliverables': ['output'], 'max_payment': amount, 'depends_on': [], 'deadline_seconds': 86400, 'on_failure': 'stop_downstream'}]
        return t
    if name == 'diagnostic_delivery':
        kind = kind or 'energy_audit'
        t = _base(requester, workspace, kind, title or 'Diagnostic delivery: a determination or a missing-evidence diagnosis', ceiling, asset)
        t['deliverables'] = [{'key': 'determination', 'type': 'determination', 'schema': energy.RESULT_SCHEMA, 'required': True, 'partial_allowed': True, 'media': ['application/json']}]
        t['acceptance'] = {'schema': POLICY_SCHEMA, 'predicates': [{'id': 'complete', 'type': 'artifact_complete'}, {'id': 'source', 'type': 'source_revision'}, {'id': 'outcome', 'type': 'outcome_in', 'params': {'outcomes': ['FEASIBLE', 'INFEASIBLE', 'INDETERMINATE']}},
                                                                   {'id': 'replay', 'type': 'verification_passed', 'params': {'class': 'full_exact', 'distinct_verifier': False}}],
                           'outcomes': {'FEASIBLE': 'accept', 'INFEASIBLE': 'accept', 'INDETERMINATE': 'accept_diagnostic'}, 'valid_negative': {'requires': ['verification_passed'], 'note': ''},
                           'required_verification': {'class': 'full_exact', 'distinct_verifier': False}, 'payment_rule': {'complete': amount, 'partial': amount // 3, 'diagnostic': amount // 2, 'outcome_neutral': True}}
        t['failure_treatment'] = {'execution_failure': 'no_payment', 'invalid_evidence': 'no_payment', 'missed_deadline': 'cancel', 'partial_fee_rule': 'diagnostic: INDETERMINATE with bounds evidence earns the diagnostic amount; partial: an accepted partial deliverable earns the partial amount'}
        t['milestones'] = [{'key': 'm1', 'deliverables': ['determination'], 'max_payment': amount, 'depends_on': [], 'deadline_seconds': 86400, 'on_failure': 'stop_downstream'}]
        return t
    raise ServiceError('VALIDATION', {'code': 'template', 'allowed': list(TEMPLATES)})


TEMPLATES = {'determination': 'outcome-neutral determination (energy_audit): FEASIBLE and INFEASIBLE both paid when verified; INDETERMINATE earns the diagnostic amount',
             'infeasibility_witness': 'numerical witness (resource_plan): feasible schedule checked exactly or infeasibility established; solver limits are never negative proofs',
             'independent_replay': 'exact replay of a frozen deterministic task compared with its registered hash (legacy exact rule)',
             'diagnostic_delivery': 'determination with pre-agreed complete / partial / diagnostic amounts'}
