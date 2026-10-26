"""The two decision job kinds executed by the ordinary worker path (child process, evidence vault, lineage):

  decision_observability   analyse a declared finite candidate model (hypotheses, views, optional observations)
  evidence_acquisition     the frozen operation of a decision-evidence-acquisition contract: the candidate model, the
                           selected menu and the validation method are frozen inputs; the provider's observation delivery
                           arrives after award (job_deliveries) and is conditioned here; outcomes separate valid-resolved,
                           valid-ambiguous, model-inconsistent, delivery-invalid and acquisition-failed."""
import hashlib
import json

from experiments.private_receipts.receipt import Invalid, canonical
from . import kernels as K
from . import checker

ACQUISITION_SCHEMA = 'evidence-acquisition-input/v1'
ACQUISITION_RESULT_SCHEMA = 'evidence-acquisition-result/v1'
ACQUISITION_MODEL_ID = 'decision-evidence-acquisition/v1'
DELIVERY_SCHEMA = 'observation-delivery/v1'
DECISION_OUTCOMES = ('DECISION_RESOLVED', 'DECISION_AMBIGUOUS', 'MODEL_INCONSISTENT_WITH_OBSERVATIONS', 'DELIVERY_INVALID', 'ACQUISITION_FAILED')
ANALYSIS_OUTCOMES = K.CONCLUSIONS
VALIDATION_METHOD = 'closed-noise-box-conditioning/v1'


def implementation_digest():
    from pathlib import Path
    h = hashlib.sha256(b'metacoin/decision-operations/v1\0')
    here = Path(__file__).parent
    for name in ('kernels.py', 'operations.py'):
        h.update((here / name).read_bytes())
    return h.hexdigest()


def checker_digest():
    from pathlib import Path
    return hashlib.sha256(b'metacoin/decision-checker/v1\0' + (Path(__file__).parent / 'checker.py').read_bytes()).hexdigest()


def _invalid(exc):
    return Invalid('%s: %s' % (exc.code, json.dumps(exc.detail, sort_keys=True)[:200]))


# ---- decision_observability ---------------------------------------------------------------------------------------------------
def validate_analysis(data):
    canonical(data)
    try:
        K.normalize(data)
    except K.KernelInvalid as exc:
        raise _invalid(exc)
    return data


def run_analysis(inputs):
    try:
        result = K.analyze(inputs)
    except K.KernelInvalid as exc:
        raise _invalid(exc)
    summary = {k: result[k] for k in ('result_schema', 'model_id', 'model_version', 'candidate_set_version', 'measurement_menu_version', 'candidate_model_digest', 'views_digest', 'hypotheses', 'views', 'decision_relevant_pairs', 'conclusion', 'vacuous', 'dimensionless_model', 'scope')}
    summary['ambiguous_pairs'] = len(result['ambiguity_witnesses']); summary['separated_pairs'] = len(result['separated_pairs'])
    if result.get('conditioning'):
        c = result['conditioning']
        summary['conditioning'] = {'observed_views': c['observed_views'], 'surviving_count': len(c['surviving']), 'excluded_count': len(c['excluded']), 'conclusion': c['conclusion'], 'determined_label': c['determined_label'] if result['disclosure']['labels'] else ('withheld-by-disclosure-policy' if c['determined_label'] else None)}
    summary['disclosure'] = result['disclosure']
    return result, result['conclusion'], summary


# ---- evidence_acquisition --------------------------------------------------------------------------------------------------------
def validate_acquisition(data):
    canonical(data)
    if type(data) is not dict or data.get('schema') != ACQUISITION_SCHEMA:
        raise Invalid('schema must be ' + ACQUISITION_SCHEMA)
    required = {'schema', 'plan_id', 'plan_digest', 'candidate_model', 'selected_views', 'validation', 'max_budget', 'accepted_delivery_outcomes', 'private_label'}
    if set(data) - (required | {'question', 'decision_job_id', 'rounds'}) or not required <= set(data):
        raise Invalid('fields: ' + ', '.join(sorted(required)))
    if type(data['plan_id']) is not str or not data['plan_id'].startswith('ap_') or type(data['plan_digest']) is not str or len(data['plan_digest']) != 64:
        raise Invalid('plan binding')
    cm = data['candidate_model']
    try:
        hyps, existing, _, meta = K.normalize(dict(cm, observations=None))
        probe = K.normalize(dict(cm, observations=None, views=list(cm.get('views', [])) + list(data['selected_views'])))
    except K.KernelInvalid as exc:
        raise _invalid(exc)
    if type(data['selected_views']) is not list or not data['selected_views'] or len(data['selected_views']) > K.LIMITS['menu_exact']:
        raise Invalid('selected_views: 1..%d menu entries' % K.LIMITS['menu_exact'])
    if any('cost' not in v for v in data['selected_views']):
        raise Invalid('every selected view carries its quoted integer cost')
    val = data['validation']
    if type(val) is not dict or val.get('method') != VALIDATION_METHOD or type(val.get('checker_id')) is not str:
        raise Invalid('validation: {method: %s, checker_id, checker_digest?}' % VALIDATION_METHOD)
    if type(data['max_budget']) is not int or isinstance(data['max_budget'], bool) or not 0 <= data['max_budget'] <= 10 ** 12 or sum(v['cost'] for v in data['selected_views']) > data['max_budget']:
        raise Invalid('max_budget must cover the selected views')
    if type(data['accepted_delivery_outcomes']) is not list or not set(data['accepted_delivery_outcomes']) <= set(DECISION_OUTCOMES) or not data['accepted_delivery_outcomes']:
        raise Invalid('accepted_delivery_outcomes ⊆ ' + ', '.join(DECISION_OUTCOMES))
    if type(data['private_label']) is not str or not 1 <= len(data['private_label']) <= 128:
        raise Invalid('invalid private label')
    if 'rounds' in data and (type(data['rounds']) is not dict or set(data['rounds']) != {'max_rounds', 'round', 'stopping_rule'} or data['rounds']['max_rounds'] != 1 or data['rounds']['round'] != 1):
        raise Invalid('rounds: fixed single round in this version (max_rounds 1); adaptive purchases need a pre-authorized program')
    return data


def validate_delivery(delivery, selected_views):
    """Provider delivery: {schema, status: acquired|could_not_acquire, observations?: {view_id: [...]}, provenance: {...}, note?}."""
    if type(delivery) is not dict or delivery.get('schema') != DELIVERY_SCHEMA:
        return 'schema must be ' + DELIVERY_SCHEMA
    if set(delivery) - {'schema', 'status', 'observations', 'provenance', 'note'}:
        return 'unknown delivery field'
    if delivery.get('status') not in ('acquired', 'could_not_acquire'):
        return 'status: acquired | could_not_acquire'
    prov = delivery.get('provenance')
    if type(prov) is not dict or not prov or set(prov) - {'instrument', 'acquired_at', 'operator', 'method', 'units_confirmed', 'simulated'} or not all(type(v) in (str, int, bool) for v in prov.values()):
        return 'provenance: {instrument, acquired_at, operator?, method?, units_confirmed?, simulated?} with bounded scalar values'
    if 'instrument' not in prov or 'acquired_at' not in prov or type(prov['acquired_at']) is not int:
        return 'provenance needs instrument and integer acquired_at'
    if 'note' in delivery and (type(delivery['note']) is not str or len(delivery['note']) > 512):
        return 'note too long'
    if delivery['status'] == 'could_not_acquire':
        return None if 'observations' not in delivery else 'could_not_acquire carries no observations'
    obs = delivery.get('observations')
    want = {v['id']: len(v['rows']) for v in selected_views}
    if type(obs) is not dict or set(obs) != set(want):
        return 'observations must cover exactly the selected views: ' + ', '.join(sorted(want))
    for vid, vals in obs.items():
        if type(vals) is not list or len(vals) != want[vid]:
            return 'observation shape for ' + vid
        for x in vals:
            try:
                K.rational(x, 'observation_value')
            except K.KernelInvalid:
                return 'observation values must be exact (integers, decimal or p/q strings; no floats)'
    if prov.get('units_confirmed') is not True:
        return 'provenance.units_confirmed must be true: the provider confirms the declared output units'
    return None


def run_acquisition(inputs, delivery):
    """Condition the frozen candidate model on the delivered observations. Returns (result, outcome, summary)."""
    cm = inputs['candidate_model']; sel = inputs['selected_views']
    base = {'result_schema': ACQUISITION_RESULT_SCHEMA, 'model_id': ACQUISITION_MODEL_ID, 'plan_id': inputs['plan_id'], 'plan_digest': inputs['plan_digest'], 'validation': inputs['validation'],
            'selected_views': [v['id'] for v in sel], 'quoted_cost': sum(v['cost'] for v in sel), 'max_budget': inputs['max_budget'], 'delivery_digest': hashlib.sha256(canonical(delivery)).hexdigest() if delivery is not None else None,
            'checker_id': checker.CHECKER_ID, 'checker_digest': checker_digest()}
    if delivery is None:
        raise Invalid('no observation delivery attached to this attempt')
    problem = validate_delivery(delivery, sel)
    if problem is not None:
        result = dict(base, outcome='DELIVERY_INVALID', reason=problem, delivery_status=delivery.get('status') if isinstance(delivery, dict) else None, provenance=delivery.get('provenance') if isinstance(delivery, dict) else None)
        return result, 'DELIVERY_INVALID', {k: result[k] for k in ('result_schema', 'model_id', 'plan_id', 'outcome', 'reason', 'quoted_cost', 'selected_views')}
    if delivery['status'] == 'could_not_acquire':
        result = dict(base, outcome='ACQUISITION_FAILED', reason=delivery.get('note') or 'provider reported it could not acquire the evidence', provenance=delivery['provenance'])
        return result, 'ACQUISITION_FAILED', {k: result[k] for k in ('result_schema', 'model_id', 'plan_id', 'outcome', 'reason', 'quoted_cost', 'selected_views')}
    model = dict(cm, views=list(cm.get('views', [])) + list(sel), observations=delivery['observations'])
    try:
        analysis = K.analyze(model)
    except K.KernelInvalid as exc:
        result = dict(base, outcome='DELIVERY_INVALID', reason='%s' % exc.code, provenance=delivery['provenance'])
        return result, 'DELIVERY_INVALID', {k: result[k] for k in ('result_schema', 'model_id', 'plan_id', 'outcome', 'reason', 'quoted_cost', 'selected_views')}
    cond = analysis['conditioning']
    outcome = {'DETERMINED_WITHIN_DECLARED_MODEL': 'DECISION_RESOLVED', 'AMBIGUOUS_AFTER_OBSERVATION': 'DECISION_AMBIGUOUS', 'MODEL_INCONSISTENT_WITH_OBSERVATIONS': 'MODEL_INCONSISTENT_WITH_OBSERVATIONS', 'NO_DECISION_RELEVANT_ALTERNATIVES': 'DECISION_RESOLVED'}[cond['conclusion']]
    # the independent checker re-decides the conditioning from the raw model (never from the kernel's projections)
    chk = checker.check_conditioning(model['hypotheses'], model['views'], model['coordinates'], cond['observations'], cond['surviving'])
    result = dict(base, outcome=outcome, analysis=analysis, provenance=delivery['provenance'], observations=delivery['observations'], independent_check=chk,
                  decision={'conclusion': cond['conclusion'], 'determined_label': cond['determined_label'], 'surviving': cond['surviving'], 'surviving_labels': cond['surviving_labels'], 'remaining_ambiguities': len(cond['remaining_witnesses']), 'coverage_limitation': cond['coverage_limitation']},
                  scope='conditioning of the frozen candidate model on the delivered observations under the declared noise bounds; a model-level decision, not calibrated physical truth')
    if not chk['ok']:
        outcome = 'DELIVERY_INVALID'; result['outcome'] = outcome; result['reason'] = 'independent checker disagreed with the conditioning'
    disc = analysis['disclosure']
    summary = {'result_schema': ACQUISITION_RESULT_SCHEMA, 'model_id': ACQUISITION_MODEL_ID, 'plan_id': inputs['plan_id'], 'plan_digest': inputs['plan_digest'], 'outcome': outcome, 'selected_views': [v['id'] for v in sel], 'quoted_cost': result['quoted_cost'],
               'surviving_count': len(cond['surviving']), 'surviving_labels': cond['surviving_labels'] if disc['labels'] else len(cond['surviving_labels']), 'determined_label': cond['determined_label'] if disc['labels'] else ('withheld-by-disclosure-policy' if cond['determined_label'] else None),
               'remaining_ambiguities': len(cond['remaining_witnesses']), 'independent_check_ok': chk['ok'], 'checker_id': checker.CHECKER_ID, 'delivery_digest': base['delivery_digest'], 'provenance_instrument': delivery['provenance'].get('instrument'), 'simulated': delivery['provenance'].get('simulated')}
    return result, outcome, summary
