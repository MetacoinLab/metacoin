"""Empirical verification portfolios (Deliverable F).

A portfolio is a versioned combination of complementary checks with their DECLARED dependency relationships (operator,
implementation, model, input source, instrument, hardware, library, challenge authority: shared / distinct / unknown;
unknown is never counted as distinct). Portfolios are assessed on preregistered fault campaigns stored as evaluation
suites (item type `fault_case`, split dev | held-out): every case is a synthetic itemwise / heat / sample / decision
computation with one injected failure family, a clean case or a valid negative. Each component reports caught / passed /
abstained / unsupported; the portfolio rejects when any component catches, abstains when any abstains and nothing catches,
otherwise accepts. The report separates suite coverage (what THIS suite exercised) from a statistical operational rate,
which is only computed when the suite declares its cases as independent representative draws; zero escapes carry an exact
one-sided bound, never a zero-error claim. Portfolios compared on the same cases use paired outcomes."""
import hashlib
import json
import random
import secrets
import time

from experiments.private_receipts import receipt as merkle
from .. import history, temporal
from ..db import now
from ..errors import ServiceError
from . import checker, kernels as K

PORTFOLIO_SCHEMA = 'metacoin-verification-portfolio/v1'
DIMENSIONS = ('operator', 'implementation', 'model', 'input_source', 'instrument', 'hardware', 'library', 'challenge_authority')
FAMILIES = {
    'units_or_scale': 'wrong physical units or calibration scale applied to every output item',
    'shared_wrong_model': 'producer and replica (and any same-model reference) use the same wrong model; only a different evidence source can expose it',
    'single_item_corrupt': 'exactly one corrupted output item',
    'burst_corrupt': 'a contiguous burst of corrupted output items',
    'subgroup_corrupt': 'faults concentrated in one subgroup of items (every item of one parameter level)',
    'heat_initial_condition': 'wrong initial/boundary field; the final transition is self-consistent so a last-step check cannot establish the trajectory',
    'sampler_mapping': 'producer-stored sample parameters are self-consistent with the stored outcomes but are not what the declared seed mapping produces',
    'overflow_nonfinite_boundary': 'integer overflow, non-finite value or an off-by-one at a boundary item',
    'wrong_contract_binding': 'valid artifact bytes attached to the wrong contract / result root',
    'correlated_workers': 'two nominally different workers deliver the same wrong output',
    'expired_or_mismatched_quote': 'an expired or mismatched measurement quote presented as current',
    'unsupported_domain': 'an operation outside the supported domain (checks must abstain, not accept)',
    'ambiguous_observation': 'a deliberately ambiguous observation: the correct outcome is a valid AMBIGUOUS result, not a rejection',
    'clean': 'a correct result (measures false rejection)',
    'valid_negative': 'a correct result whose scientific outcome is negative (INFEASIBLE everywhere) — must be accepted, not rejected',
}
COMPONENTS = {
    'byte_integrity': {'claim': 'stored output bytes equal the committed digest', 'cost': 1, 'dependencies': {'operator': 'shared', 'implementation': 'distinct', 'model': 'unknown', 'input_source': 'shared', 'instrument': 'unknown', 'hardware': 'shared', 'library': 'shared', 'challenge_authority': 'unknown'}},
    'contract_binding': {'claim': 'the result root is bound to the frozen contract digest and input root', 'cost': 1, 'dependencies': {'operator': 'shared', 'implementation': 'distinct', 'model': 'unknown', 'input_source': 'shared', 'instrument': 'unknown', 'hardware': 'shared', 'library': 'shared', 'challenge_authority': 'unknown'}},
    'schema_units': {'claim': 'declared units and schema of the output match the contract', 'cost': 1, 'dependencies': {'operator': 'shared', 'implementation': 'distinct', 'model': 'shared', 'input_source': 'shared', 'instrument': 'unknown', 'hardware': 'shared', 'library': 'shared', 'challenge_authority': 'unknown'}},
    'invariants': {'claim': 'row invariants (outcome codes, margin ordering, finiteness)', 'cost': 1, 'dependencies': {'operator': 'shared', 'implementation': 'distinct', 'model': 'shared', 'input_source': 'shared', 'instrument': 'unknown', 'hardware': 'shared', 'library': 'shared', 'challenge_authority': 'unknown'}},
    'itemwise_exact_full': {'claim': 'every item recomputed by the reference implementation under the declared model and compared exactly', 'cost': 'N', 'dependencies': {'operator': 'shared', 'implementation': 'distinct', 'model': 'shared', 'input_source': 'shared', 'instrument': 'unknown', 'hardware': 'shared', 'library': 'distinct', 'challenge_authority': 'unknown'}},
    'itemwise_exact_sampled': {'claim': 'a plan-bound uniform sample of items recomputed exactly (params m, alpha, budget)', 'cost': 'k', 'dependencies': {'operator': 'shared', 'implementation': 'distinct', 'model': 'shared', 'input_source': 'shared', 'instrument': 'unknown', 'hardware': 'shared', 'library': 'distinct', 'challenge_authority': 'shared'}},
    'replica_same_code': {'claim': 'the producer implementation re-run (another worker) agrees byte for byte', 'cost': 'N', 'dependencies': {'operator': 'shared', 'implementation': 'shared', 'model': 'shared', 'input_source': 'shared', 'instrument': 'unknown', 'hardware': 'distinct', 'library': 'shared', 'challenge_authority': 'unknown'}},
    'heat_final_transition': {'claim': 'the last heat step recomputed from the stored penultimate field', 'cost': 1, 'dependencies': {'operator': 'shared', 'implementation': 'distinct', 'model': 'shared', 'input_source': 'shared', 'instrument': 'unknown', 'hardware': 'shared', 'library': 'distinct', 'challenge_authority': 'unknown'}},
    'heat_whole_trajectory': {'claim': 'the whole heat trajectory recomputed from the declared initial condition', 'cost': 'steps', 'dependencies': {'operator': 'shared', 'implementation': 'distinct', 'model': 'shared', 'input_source': 'shared', 'instrument': 'unknown', 'hardware': 'shared', 'library': 'distinct', 'challenge_authority': 'unknown'}},
    'sample_reevaluation': {'claim': 'producer-stored sample parameters re-evaluated; the sampler mapping is NOT regenerated', 'cost': 'N', 'dependencies': {'operator': 'shared', 'implementation': 'distinct', 'model': 'shared', 'input_source': 'shared', 'instrument': 'unknown', 'hardware': 'shared', 'library': 'distinct', 'challenge_authority': 'unknown'}},
    'sampler_regeneration': {'claim': 'sample parameters regenerated from the declared seed mapping by a second implementation and compared', 'cost': 'N', 'dependencies': {'operator': 'shared', 'implementation': 'distinct', 'model': 'shared', 'input_source': 'shared', 'instrument': 'unknown', 'hardware': 'shared', 'library': 'distinct', 'challenge_authority': 'unknown'}},
    'quote_validity': {'claim': 'measurement quotes current and bound to the plan digest at the moment of use', 'cost': 1, 'dependencies': {'operator': 'shared', 'implementation': 'distinct', 'model': 'unknown', 'input_source': 'shared', 'instrument': 'unknown', 'hardware': 'shared', 'library': 'shared', 'challenge_authority': 'unknown'}},
    'witness_check': {'claim': 'decision witnesses re-decided by the independent checker; ambiguity is a valid result', 'cost': 'pairs', 'dependencies': {'operator': 'shared', 'implementation': 'distinct', 'model': 'shared', 'input_source': 'shared', 'instrument': 'unknown', 'hardware': 'shared', 'library': 'distinct', 'challenge_authority': 'unknown'}},
    'model_challenge': {'claim': 'an independently sourced observation compared with the model prediction (only when the case supplies one)', 'cost': 1, 'dependencies': {'operator': 'unknown', 'implementation': 'distinct', 'model': 'distinct', 'input_source': 'distinct', 'instrument': 'distinct', 'hardware': 'distinct', 'library': 'distinct', 'challenge_authority': 'unknown'}},
}
ADMINISTRATIVE = ('byte_integrity', 'contract_binding', 'schema_units', 'quote_validity')
BASE = {'schema': 'temporal-energy-input/v1', 'capacity': 10_000, 'initial_low': 6_000, 'initial_high': 6_000, 'reserve': 2_000,
        'segments': [{'duration': 10, 'harvest_low': 600, 'harvest_high': 800, 'load_low': 500, 'load_high': 500, 'leakage_low': 0, 'leakage_high': 0},
                     {'duration': 30, 'harvest_low': 0, 'harvest_high': 0, 'load_low': 100, 'load_high': 200, 'leakage_low': 0, 'leakage_high': 5}],
        'units': {'energy': 'mJ', 'power': 'mW', 'duration': 's'},
        'assumptions': ['piecewise_constant_power_bounds', 'independent_interval_bounds', 'powers_at_usable_energy_boundary', 'saturation_at_capacity', 'constant_reserve', 'virtual_energy_below_reserve_for_diagnostics', 'no_unmodeled_loads', 'no_recharge_physics'],
        'provenance': 'synthetic', 'private_label': 'PORTFOLIO_SYNTHETIC'}
ITEMS = 24


# ---- synthetic operations -------------------------------------------------------------------------------------------------------
def _scenarios(inputs, n=ITEMS):
    out = []
    for i in range(n):
        s = json.loads(json.dumps(inputs)); s['reserve'] = 500 + 300 * (i % 6); s['initial_low'] = s['initial_high'] = 4000 + 500 * (i // 6)
        out.append(s)
    return out


def _row(scenario):
    r = temporal.analyze(scenario)
    return [{'FEASIBLE': 0, 'INFEASIBLE': 1, 'INDETERMINATE': 2}[r['outcome']], r['min_reserve_margin_pessimistic'], r['min_reserve_margin_optimistic'], r['final_energy_bounds'][0], r['final_energy_bounds'][1]]


def _heat_case(rng, wrong_initial=False):
    nx = ny = 5; rx = ry = 0.2; steps = 6
    init = [[0.0] * nx for _ in range(ny)]
    for j in range(1, ny - 1):
        for i in range(1, nx - 1):
            init[j][i] = 1.0 + 0.1 * ((i * 7 + j * 3) % 5)
    from ..compute.reference import heat_reference
    declared = {'nx': nx, 'ny': ny, 'rx': rx, 'ry': ry, 'steps': steps, 'initial': init}
    start = init if not wrong_initial else [[v * 1.5 for v in row] for row in init]
    traj = [heat_reference(start, rx, ry, k) for k in (steps - 1, steps)]
    return {'kind': 'heat', 'inputs': declared, 'producer_output': {'field_prev': traj[0], 'field': traj[1]}}


def _mc_case(rng, wrong_mapping=False):
    seed = rng.randint(1, 10 ** 6); n = 16
    def mapping(seed, i):
        return 400 + (seed * 7919 + i * 104729) % 1500
    params = [mapping(seed, i) for i in range(n)]
    if wrong_mapping:
        params = [mapping(seed + 1, i) for i in range(n)]            # self-consistent with the stored outcomes but not the declared mapping
    base = json.loads(json.dumps(BASE))
    samples = []
    for p in params:
        s = json.loads(json.dumps(base)); s['reserve'] = p
        samples.append({'params': {'reserve': p}, 'outcome': 1 if temporal.analyze(s)['outcome'] == 'FEASIBLE' else 0})
    return {'kind': 'mc', 'inputs': {'seed': seed, 'samples': n, 'base': base}, 'producer_output': {'samples_audit': samples, 'events': sum(s['outcome'] for s in samples)}}


def _decision_case(rng, ambiguous=True):
    hs = [{'id': 'h1', 'label': 'A', 'state': [100, 20]}, {'id': 'h2', 'label': 'B', 'state': [20, 100]}]
    views = [{'id': 'total', 'rows': [[1, 1]], 'noise': [2], 'output_units': ['mJ'], 'cost': 0}] + ([] if ambiguous else [{'id': 'c1', 'rows': [[1, 0]], 'noise': [2], 'output_units': ['mJ'], 'cost': 3}])
    inp = {'schema': K.ANALYSIS_SCHEMA, 'model': {'id': K.MODEL_ID, 'version': 1}, 'candidate_set_version': 'p', 'measurement_menu_version': 'p', 'coordinates': [{'name': 'c1', 'unit': 'mJ'}, {'name': 'c2', 'unit': 'mJ'}], 'hypotheses': hs, 'views': views}
    return {'kind': 'decision', 'inputs': inp, 'producer_output': K.analyze(inp)}


def make_case(family, rng):
    """One preregistered fault case; `expected` is accept | reject | abstain (what a correct portfolio must do)."""
    base = json.loads(json.dumps(BASE))
    scen = _scenarios(base)
    rows = [_row(s) for s in scen]
    commit = hashlib.sha256(K.canonical(rows)).hexdigest()
    case = {'family': family, 'kind': 'itemwise', 'inputs': base, 'scenarios': scen, 'producer_output': rows, 'commitment': commit, 'binding': {'contract_digest': 'c' * 64, 'input_root': 'i' * 64, 'result_root': commit}, 'declared_units': 'mJ', 'output_units': 'mJ',
            'quote': {'expires_at': now() + 3600, 'plan_digest': 'p' * 64, 'bound_plan_digest': 'p' * 64}, 'expected': 'reject', 'seed': rng.randint(0, 2 ** 31)}
    if family == 'clean':
        case['expected'] = 'accept'
    elif family == 'valid_negative':
        neg = json.loads(json.dumps(base)); neg['initial_low'] = neg['initial_high'] = 1000; neg['reserve'] = 9000
        case['inputs'] = neg; case['scenarios'] = [dict(s, initial_low=1000, initial_high=1000, reserve=9000) for s in scen]; case['producer_output'] = [_row(s) for s in case['scenarios']]
        case['commitment'] = case['binding']['result_root'] = hashlib.sha256(K.canonical(case['producer_output'])).hexdigest(); case['expected'] = 'accept'
    elif family == 'units_or_scale':
        case['producer_output'] = [[r[0]] + [v * 1000 for v in r[1:]] for r in rows]; case['output_units'] = 'uJ'
    elif family == 'shared_wrong_model':
        wrong = json.loads(json.dumps(base)); wrong['segments'][1]['load_low'] = 20; wrong['segments'][1]['load_high'] = 20            # the model everyone agrees on is wrong; truth has load 100..200
        case['inputs'] = wrong; case['scenarios'] = _scenarios(wrong); case['producer_output'] = [_row(s) for s in case['scenarios']]
        case['truth_scenarios'] = scen; case['external_observation'] = {'item': 3, 'row': rows[3]}
    elif family == 'single_item_corrupt':
        i = rng.randrange(ITEMS); r = list(rows[i]); r[1] += 1; case['producer_output'] = rows[:i] + [r] + rows[i + 1:]
    elif family == 'burst_corrupt':
        i = rng.randrange(ITEMS - 6); case['producer_output'] = rows[:i] + [[r[0]] + [v + 7 for v in r[1:]] for r in rows[i:i + 6]] + rows[i + 6:]
    elif family == 'subgroup_corrupt':
        level = rng.randrange(6); case['producer_output'] = [([r[0]] + [v - 3 for v in r[1:]] if (j % 6) == level else r) for j, r in enumerate(rows)]
    elif family == 'overflow_nonfinite_boundary':
        mode = rng.choice(('overflow', 'nonfinite', 'boundary'))
        if mode == 'overflow':
            r = list(rows[-1]); r[3] = 2 ** 63; case['producer_output'] = rows[:-1] + [r]
        elif mode == 'nonfinite':
            r = list(rows[0]); r[2] = 'nan'; case['producer_output'] = [r] + rows[1:]
        else:
            r = list(rows[-1]); r[4] = r[4] + 1; case['producer_output'] = rows[:-1] + [r]
        case['mode'] = mode
    elif family == 'wrong_contract_binding':
        case['binding'] = {'contract_digest': 'd' * 64, 'input_root': 'j' * 64, 'result_root': commit}; case['bound_to'] = {'contract_digest': 'c' * 64, 'input_root': 'i' * 64}
    elif family == 'correlated_workers':
        i = rng.randrange(ITEMS); r = list(rows[i]); r[1] -= 2; bad = rows[:i] + [r] + rows[i + 1:]
        case['producer_output'] = bad; case['replica_output'] = bad                              # both workers wrong in the same way
    elif family == 'expired_or_mismatched_quote':
        case['quote'] = {'expires_at': now() - 10, 'plan_digest': 'p' * 64, 'bound_plan_digest': 'q' * 64}
    elif family == 'heat_initial_condition':
        case.update(_heat_case(rng, wrong_initial=True))
    elif family == 'sampler_mapping':
        case.update(_mc_case(rng, wrong_mapping=True))
    elif family == 'unsupported_domain':
        case['kind'] = 'unsupported'; case['expected'] = 'abstain'
    elif family == 'ambiguous_observation':
        case.update(_decision_case(rng, ambiguous=True)); case['expected'] = 'accept'
    if 'commitment' in case and case['kind'] == 'itemwise' and family not in ('wrong_contract_binding',):
        case['commitment'] = hashlib.sha256(K.canonical(case['producer_output'])).hexdigest()
        if family not in ('valid_negative',):
            case['binding']['result_root'] = case['commitment']
    return case


# ---- component checks ------------------------------------------------------------------------------------------------------------
def _ref_rows(case):
    return [_row(s) for s in case['scenarios']]


def _finite(v):
    return type(v) is int and abs(v) < 2 ** 62


def run_component(name, params, case):
    kind = case['kind']
    out = lambda result, cost=1, detail=None: {'component': name, 'result': result, 'cost_units': cost, 'detail': detail}
    if name == 'byte_integrity':
        if kind != 'itemwise':
            return out('unsupported', 0)
        return out('caught' if hashlib.sha256(K.canonical(case['producer_output'])).hexdigest() != case['commitment'] else 'passed')
    if name == 'contract_binding':
        b = case.get('binding') or {}
        bound = case.get('bound_to') or {'contract_digest': 'c' * 64, 'input_root': 'i' * 64}
        ok = b.get('contract_digest') == bound['contract_digest'] and b.get('input_root') == bound['input_root'] and b.get('result_root') == case.get('commitment')
        return out('passed' if ok else 'caught', 1, {'bound': ok})
    if name == 'schema_units':
        if kind != 'itemwise':
            return out('unsupported', 0)
        return out('passed' if case.get('output_units') == case.get('declared_units') else 'caught')
    if name == 'invariants':
        if kind != 'itemwise':
            return out('unsupported', 0)
        bad = [i for i, r in enumerate(case['producer_output']) if not (r[0] in (0, 1, 2) and all(_finite(v) for v in r[1:]) and r[1] <= r[2] and r[3] <= r[4])]
        return out('caught' if bad else 'passed', 1, {'violations': bad[:5]})
    if name == 'itemwise_exact_full':
        if kind != 'itemwise':
            return out('unsupported', 0)
        ref = _ref_rows(case); mism = [i for i, (a, b) in enumerate(zip(case['producer_output'], ref)) if a != b]
        return out('caught' if mism else 'passed', len(ref), {'mismatches': mism[:5]})
    if name == 'itemwise_exact_sampled':
        if kind != 'itemwise':
            return out('unsupported', 0)
        n = len(case['producer_output']); m = params.get('min_defects', 4); alpha = params.get('alpha', '1/20'); budget = params.get('budget', 12)
        plan = K.sampling_plan(n, m, alpha, min(budget, n))
        if not plan['feasible']:
            return out('abstained', 0, {'reason': 'sample_budget_insufficient', 'required': plan['required_samples'], 'budget': budget})
        k = plan['required_samples']
        ranked = sorted(range(n), key=lambda i: hashlib.sha256(('%d|%s|%d' % (case['seed'], case['commitment'], i)).encode()).digest())
        idx = sorted(ranked[:k]); ref = _ref_rows(case)
        mism = [i for i in idx if case['producer_output'][i] != ref[i]]
        return out('caught' if mism else 'passed', k, {'sampled': idx, 'mismatches': mism, 'bound': plan['achieved_bound']['exact']})
    if name == 'replica_same_code':
        if kind != 'itemwise':
            return out('unsupported', 0)
        replica = case.get('replica_output') or _ref_rows(case)
        if case['family'] in ('shared_wrong_model',):
            replica = _ref_rows(case)                                                             # same wrong model: agrees
        return out('caught' if replica != case['producer_output'] else 'passed', len(replica))
    if name == 'heat_final_transition':
        if kind != 'heat':
            return out('unsupported', 0)
        from ..compute.reference import heat_reference, heat_max_abs_diff
        d = heat_max_abs_diff(heat_reference(case['producer_output']['field_prev'], case['inputs']['rx'], case['inputs']['ry'], 1), case['producer_output']['field'])
        return out('caught' if d > 1e-9 else 'passed', 1, {'max_abs_diff': d, 'scope': 'one transition'})
    if name == 'heat_whole_trajectory':
        if kind != 'heat':
            return out('unsupported', 0)
        from ..compute.reference import heat_reference, heat_max_abs_diff
        d = heat_max_abs_diff(heat_reference(case['inputs']['initial'], case['inputs']['rx'], case['inputs']['ry'], case['inputs']['steps']), case['producer_output']['field'])
        return out('caught' if d > 1e-9 else 'passed', case['inputs']['steps'], {'max_abs_diff': d})
    if name == 'sample_reevaluation':
        if kind != 'mc':
            return out('unsupported', 0)
        bad = 0
        for s in case['producer_output']['samples_audit']:
            sc = json.loads(json.dumps(case['inputs']['base'])); sc['reserve'] = s['params']['reserve']
            bad += (1 if temporal.analyze(sc)['outcome'] == 'FEASIBLE' else 0) != s['outcome']
        return out('caught' if bad else 'passed', len(case['producer_output']['samples_audit']), {'note': 'stored parameters re-evaluated; mapping not regenerated'})
    if name == 'sampler_regeneration':
        if kind != 'mc':
            return out('unsupported', 0)
        seed = case['inputs']['seed']
        regen = [400 + (seed * 7919 + i * 104729) % 1500 for i in range(case['inputs']['samples'])]
        stored = [s['params']['reserve'] for s in case['producer_output']['samples_audit']]
        return out('caught' if regen != stored else 'passed', len(regen))
    if name == 'quote_validity':
        q = case.get('quote') or {}
        ok = q.get('expires_at', 0) > now() and q.get('plan_digest') == q.get('bound_plan_digest')
        return out('passed' if ok else 'caught', 1, {'expired': q.get('expires_at', 0) <= now(), 'mismatched': q.get('plan_digest') != q.get('bound_plan_digest')})
    if name == 'witness_check':
        if kind != 'decision':
            return out('unsupported', 0)
        chk = checker.check_analysis(case['inputs'], case['producer_output'])
        return out('passed' if chk['ok'] else 'caught', len(chk['checks']), {'conclusion': case['producer_output']['conclusion'], 'note': 'AMBIGUOUS is a valid, checked result'})
    if name == 'model_challenge':
        obs = case.get('external_observation')
        if not obs or kind != 'itemwise':
            return out('unsupported', 0, {'reason': 'no independently sourced observation in this case'})
        pred = case['producer_output'][obs['item']]
        return out('caught' if pred != obs['row'] else 'passed', 1, {'item': obs['item']})
    raise ServiceError('VALIDATION', {'code': 'component', 'allowed': sorted(COMPONENTS)})


def run_portfolio(portfolio, case):
    t0 = time.time()
    comps = [run_component(c['component'], c.get('params', {}), case) for c in portfolio['components']]
    caught = [c['component'] for c in comps if c['result'] == 'caught']
    abst = [c['component'] for c in comps if c['result'] == 'abstained']
    scientific = [c for c in comps if c['result'] != 'unsupported' and c['component'] not in ADMINISTRATIVE]
    if caught:
        verdict = 'reject'
    elif abst or not scientific:
        verdict = 'abstain'                                              # administrative checks alone never accept a scientific result
    else:
        verdict = 'accept'
    exp = case['expected']
    cls = {('accept', 'accept'): 'correct_accept', ('reject', 'reject'): 'caught', ('abstain', 'abstain'): 'correct_abstention', ('accept', 'reject'): 'false_rejection', ('reject', 'accept'): 'false_acceptance',
           ('reject', 'abstain'): 'abstention', ('accept', 'abstain'): 'abstention', ('abstain', 'accept'): 'false_acceptance', ('abstain', 'reject'): 'false_rejection'}[(exp, verdict)]
    return {'verdict': verdict, 'expected': exp, 'classification': cls, 'components': comps, 'cost_units': sum(c['cost_units'] for c in comps), 'latency_ms': int((time.time() - t0) * 1000), 'caught_by': caught, 'abstained_by': abst}


class Portfolios:
    def __init__(self, settings, services):
        self.settings, self.svc = settings, services

    @staticmethod
    def families():
        return FAMILIES

    @staticmethod
    def components():
        return {k: {'claim': v['claim'], 'cost': v['cost'], 'dependencies': v['dependencies']} for k, v in COMPONENTS.items()}

    def create(self, db, principal, body):
        principal.require('verification:submit'); principal.require('contract:create')
        if type(body) is not dict or set(body) - {'name', 'components', 'coverage_statement', 'note'}:
            raise ServiceError('VALIDATION', {'code': 'fields', 'allowed': ['name', 'components', 'coverage_statement?', 'note?']})
        name = body.get('name'); comps = body.get('components')
        if type(name) is not str or not 1 <= len(name) <= 64 or type(comps) is not list or not 1 <= len(comps) <= 12:
            raise ServiceError('VALIDATION', 'name / components (1..12)')
        norm = []
        for c in comps:
            if type(c) is not dict or c.get('component') not in COMPONENTS or set(c) - {'component', 'params', 'dependencies'}:
                raise ServiceError('VALIDATION', {'code': 'component', 'allowed': sorted(COMPONENTS)})
            deps = dict(COMPONENTS[c['component']]['dependencies'])
            for k, v in (c.get('dependencies') or {}).items():
                if k not in DIMENSIONS or v not in ('shared', 'distinct', 'unknown'):
                    raise ServiceError('VALIDATION', {'code': 'dependency', 'dimensions': list(DIMENSIONS), 'values': ['shared', 'distinct', 'unknown']})
                deps[k] = v
            p = c.get('params') or {}
            if type(p) is not dict or set(p) - {'min_defects', 'alpha', 'budget'}:
                raise ServiceError('VALIDATION', {'code': 'params', 'allowed': ['min_defects', 'alpha', 'budget']})
            norm.append({'component': c['component'], 'params': p, 'dependencies': deps, 'claim': COMPONENTS[c['component']]['claim']})
        common = {d: 'shared' for d in DIMENSIONS if all(c['dependencies'][d] == 'shared' for c in norm)}
        unknown = sorted(d for d in DIMENSIONS if any(c['dependencies'][d] == 'unknown' for c in norm))
        pf = {'schema': PORTFOLIO_SCHEMA, 'name': name, 'components': norm, 'common_mode': sorted(common), 'unknown_dimensions': unknown, 'coverage_statement': body.get('coverage_statement') or 'coverage is established by a fault campaign on a named suite, not by the component names',
              'rule': 'reject if any component catches; abstain if any abstains and none catches; accept otherwise. Unknown independence is unknown, not distinct.'}
        version = db.execute('SELECT COALESCE(MAX(version),0)+1 FROM verification_portfolios WHERE workspace=? AND name=?', (principal.workspace, name)).fetchone()[0]
        pid = 'vpf_' + secrets.token_hex(6)
        digest = hashlib.sha256(merkle.canonical(pf)).hexdigest()
        db.execute('INSERT INTO verification_portfolios VALUES (?,?,?,?,?,?,?,?,NULL)', (pid, principal.workspace, name, version, json.dumps(pf), digest, principal.id, now()))
        history.record(db, principal.workspace, principal.id, 'verification.requested', 'verification_portfolio', pid, {'name': name, 'version': version, 'components': [c['component'] for c in norm]})
        return self.view(db, principal, pid)

    def row(self, db, principal, pid):
        r = db.execute('SELECT * FROM verification_portfolios WHERE id=? AND workspace=?', (pid, principal.workspace)).fetchone()
        if r is None:
            raise ServiceError('NOT_FOUND', 'portfolio')
        return r

    def view(self, db, principal, pid):
        principal.require('job:read')
        r = self.row(db, principal, pid)
        camps = db.execute('SELECT id, suite_id, split, created_at FROM portfolio_campaigns WHERE workspace=? AND portfolios_json LIKE ? ORDER BY created_at DESC', (principal.workspace, '%' + pid + '%')).fetchall()
        return dict(json.loads(r['portfolio_json']), id=pid, version=r['version'], digest=r['digest'], created_by=r['created_by'], created_at=r['created_at'], campaigns=[dict(c) for c in camps])

    def list(self, db, principal):
        principal.require('job:read')
        return [self.view(db, principal, r['id']) for r in db.execute('SELECT id FROM verification_portfolios WHERE workspace=? ORDER BY name, version', (principal.workspace,)).fetchall()]

    # ---- fault suites in the evaluation registry --------------------------------------------------------------------------------------
    def create_fault_suite(self, db, principal, body):
        if type(body) is not dict or set(body) - {'name', 'families', 'seed', 'cases_per_family', 'holdout_per_family', 'representative_draws'}:
            raise ServiceError('VALIDATION', {'code': 'fields', 'allowed': ['name', 'families?', 'seed', 'cases_per_family', 'holdout_per_family', 'representative_draws?']})
        fams = body.get('families') or sorted(FAMILIES)
        if type(fams) is not list or not fams or not set(fams) <= set(FAMILIES):
            raise ServiceError('VALIDATION', {'code': 'families', 'allowed': sorted(FAMILIES)})
        seed = body.get('seed', 1); k = body.get('cases_per_family', 2); h = body.get('holdout_per_family', 1)
        for v in (seed, k, h):
            if type(v) is not int or isinstance(v, bool) or v < 0:
                raise ServiceError('VALIDATION', 'seed / cases_per_family / holdout_per_family: non-negative integers')
        if k + h == 0 or len(fams) * (k + h) > 64:
            raise ServiceError('VALIDATION', {'code': 'suite_size', 'max_items': 64})
        rng = random.Random(seed)
        items = []
        for f in fams:
            for i in range(k + h):
                items.append({'id': ('%s-%d' % (f, i))[:32], 'type': 'fault_case', 'family': f, 'split': 'dev' if i < k else 'held-out', 'hidden': i >= k, 'case_seed': rng.randint(0, 2 ** 31 - 1), 'expected': None})
        suite = self.svc.evaluation.create_suite(db, principal, body.get('name') or 'fault-campaign', items, 100)
        if body.get('representative_draws'):
            db.execute('INSERT INTO meta (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', ('suite_representative:' + suite['id'], '1'))
        return dict(suite, families=fams, preregistered=True, representative_draws=bool(body.get('representative_draws')), note='cases are generated deterministically from case_seed at run time; held-out items are hidden and their use is counted')

    # ---- campaigns -------------------------------------------------------------------------------------------------------------------------
    def run_campaign(self, db, principal, body):
        principal.require('verification:submit')
        if type(body) is not dict or set(body) - {'suite_id', 'portfolio_ids', 'split'}:
            raise ServiceError('VALIDATION', {'code': 'fields', 'allowed': ['suite_id', 'portfolio_ids', 'split (dev | held-out)']})
        suite = self.svc.evaluation.suite(db, principal, body.get('suite_id'))
        split = body.get('split', 'dev')
        if split not in ('dev', 'held-out'):
            raise ServiceError('VALIDATION', {'code': 'split', 'allowed': ['dev', 'held-out']})
        pids = body.get('portfolio_ids')
        if type(pids) is not list or not 1 <= len(pids) <= 6:
            raise ServiceError('VALIDATION', 'portfolio_ids: 1..6')
        pfs = {pid: json.loads(self.row(db, principal, pid)['portfolio_json']) for pid in pids}
        items = [it for it in json.loads(suite['items_json']) if it.get('type') == 'fault_case' and it.get('split', 'dev') == split]
        if not items:
            raise ServiceError('CONFLICT', {'code': 'no_fault_cases', 'split': split})
        if split == 'held-out':
            key = 'holdout_uses:' + suite['id']
            row = db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
            uses = int(row['value']) + 1 if row else 1
            db.execute('INSERT INTO meta (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, str(uses)))
        else:
            uses = None
        representative = db.execute('SELECT value FROM meta WHERE key=?', ('suite_representative:' + suite['id'],)).fetchone() is not None
        results = {pid: [] for pid in pids}
        for it in items:
            case = make_case(it['family'], random.Random(it['case_seed']))
            for pid in pids:
                r = run_portfolio(pfs[pid], case)
                results[pid].append({'item': it['id'], 'family': it['family'], **{k: r[k] for k in ('verdict', 'expected', 'classification', 'cost_units', 'latency_ms', 'caught_by', 'abstained_by')}})
        report = self._report(pids, pfs, results, items, representative, uses, split)
        cid = 'pc_' + secrets.token_hex(6)
        db.execute('INSERT INTO portfolio_campaigns VALUES (?,?,?,?,?,?,?,?)', (cid, principal.workspace, suite['id'], split, json.dumps(pids), json.dumps({'results': results, 'report': report}), principal.id, now()))
        history.record(db, principal.workspace, principal.id, 'verification.requested', 'portfolio_campaign', cid, {'suite_id': suite['id'], 'split': split, 'portfolios': pids, 'cases': len(items), 'holdout_uses': uses})
        return self.campaign_view(db, principal, cid)

    @staticmethod
    def _report(pids, pfs, results, items, representative, holdout_uses, split):
        rep = {'split': split, 'cases': len(items), 'independent_unit': 'one preregistered fault case', 'holdout_uses': holdout_uses, 'portfolios': {}, 'paired': {}, 'common_mode': {}}
        for pid in pids:
            rows = results[pid]; fam = {}
            for r in rows:
                d = fam.setdefault(r['family'], {'cases': 0, 'caught': 0, 'false_acceptance': 0, 'false_rejection': 0, 'abstention': 0, 'correct_accept': 0, 'correct_abstention': 0, 'cost_units': 0, 'latency_ms': 0})
                d['cases'] += 1; d[r['classification']] += 1; d['cost_units'] += r['cost_units']; d['latency_ms'] += r['latency_ms']
            fault_rows = [r for r in rows if r['expected'] == 'reject']
            clean_rows = [r for r in rows if r['expected'] == 'accept']
            esc = sum(1 for r in fault_rows if r['classification'] == 'false_acceptance'); fr = sum(1 for r in clean_rows if r['classification'] == 'false_rejection')
            totals = {'fault_cases': len(fault_rows), 'caught': sum(1 for r in fault_rows if r['classification'] == 'caught'), 'false_acceptance': esc, 'abstention_on_faults': sum(1 for r in fault_rows if r['classification'] == 'abstention'),
                      'clean_cases': len(clean_rows), 'false_rejection': fr, 'abstention_on_clean': sum(1 for r in clean_rows if r['classification'] == 'abstention'), 'cost_units': sum(r['cost_units'] for r in rows), 'latency_ms': sum(r['latency_ms'] for r in rows)}
            coverage = {f: (d['caught'] / d['cases'] if d['cases'] and f not in ('clean', 'valid_negative', 'ambiguous_observation', 'unsupported_domain') else None) for f, d in fam.items()}
            stat = {'applicable': representative, 'note': 'suite coverage only: the cases are a preregistered fixed suite, not independent representative draws from an operational population' if not representative else 'cases declared as independent representative draws by the suite'}
            if representative and fault_rows:
                stat['false_acceptance_upper_95'] = K.binomial_upper(esc, len(fault_rows), 0.05)
                stat['zero_failure_note'] = ('0 escapes in %d cases: one-sided 95%% upper bound %.4f (exact binomial inversion); not a zero-error claim' % (len(fault_rows), stat['false_acceptance_upper_95'])) if esc == 0 else None
            rep['portfolios'][pid] = {'name': pfs[pid]['name'], 'by_family': fam, 'totals': totals, 'suite_coverage': coverage, 'statistical': stat, 'common_mode_dimensions': pfs[pid]['common_mode'], 'unknown_dimensions': pfs[pid]['unknown_dimensions']}
        fams_all = sorted({r['family'] for r in results[pids[0]] if r['expected'] == 'reject'})
        never = [f for f in fams_all if all(all(r['classification'] != 'caught' for r in results[pid] if r['family'] == f) for pid in pids)]
        rep['common_mode'] = {'families_no_portfolio_caught': never, 'reading': 'a family no compute-only portfolio catches is a residual risk floor; more computation cannot resolve a shared model error — a different evidence source (measurement acquisition) is the recommendation' if never else 'every fault family was caught by at least one portfolio on this suite'}
        for i, a in enumerate(pids):
            for b in pids[i + 1:]:
                fa = [r['classification'] in ('caught', 'correct_accept', 'correct_abstention') for r in results[a]]; fb = [r['classification'] in ('caught', 'correct_accept', 'correct_abstention') for r in results[b]]
                nf = sum(1 for r in results[a] if r['expected'] == 'reject')
                ea = sum(1 for r in results[a] if r['classification'] == 'false_acceptance'); eb = sum(1 for r in results[b] if r['classification'] == 'false_acceptance')
                rep['paired'][a + '|' + b] = {'paired_outcomes': K.paired_comparison(fa, fb), 'residual_escape_ratio_a_over_b': K.residual_ratio(ea, eb, max(nf, 1)) if nf else None, 'cost_units': [rep['portfolios'][a]['totals']['cost_units'], rep['portfolios'][b]['totals']['cost_units']],
                                               'note': 'same cases, paired analysis; marginal rates are never multiplied; the label is an empirical improvement on THIS suite, not a code distance'}
        return rep

    def campaign_view(self, db, principal, cid):
        principal.require('job:read')
        r = db.execute('SELECT * FROM portfolio_campaigns WHERE id=? AND workspace=?', (cid, principal.workspace)).fetchone()
        if r is None:
            raise ServiceError('NOT_FOUND', 'campaign')
        data = json.loads(r['report_json'])
        res = data['results']
        if r['split'] == 'held-out' and not principal.can('model:admin'):
            res = {pid: [{k: v for k, v in x.items() if k in ('item', 'classification')} for x in rows] for pid, rows in res.items()}
        return {'id': cid, 'suite_id': r['suite_id'], 'split': r['split'], 'portfolio_ids': json.loads(r['portfolios_json']), 'report': data['report'], 'results': res, 'started_by': r['started_by'], 'created_at': r['created_at']}

    def list_campaigns(self, db, principal):
        principal.require('job:read')
        return [{'id': r['id'], 'suite_id': r['suite_id'], 'split': r['split'], 'portfolio_ids': json.loads(r['portfolios_json']), 'created_at': r['created_at']} for r in db.execute('SELECT * FROM portfolio_campaigns WHERE workspace=? ORDER BY created_at DESC LIMIT 100', (principal.workspace,)).fetchall()]

    def select(self, db, principal, body):
        """Cheapest portfolio whose DEV-split campaign on the named suite caught every required family; otherwise the gap and a
        recommendation. Never uses held-out results for selection and never trusts a provider's self-reported score."""
        principal.require('job:read')
        if type(body) is not dict or set(body) - {'suite_id', 'required_families', 'max_cost_units'}:
            raise ServiceError('VALIDATION', {'code': 'fields', 'allowed': ['suite_id', 'required_families', 'max_cost_units?']})
        req = body.get('required_families')
        if type(req) is not list or not req or not set(req) <= set(FAMILIES):
            raise ServiceError('VALIDATION', {'code': 'required_families', 'allowed': sorted(FAMILIES)})
        cap = body.get('max_cost_units')
        camps = db.execute("SELECT * FROM portfolio_campaigns WHERE workspace=? AND suite_id=? AND split='dev' ORDER BY created_at DESC", (principal.workspace, body.get('suite_id'))).fetchall()
        seen, candidates = set(), []
        for c in camps:
            rep = json.loads(c['report_json'])['report']
            for pid, pr in rep['portfolios'].items():
                if pid in seen:
                    continue
                seen.add(pid)
                cov = pr['suite_coverage']; cost = pr['totals']['cost_units']
                full = [f for f in req if cov.get(f) == 1.0]; gaps = [f for f in req if cov.get(f) != 1.0]
                candidates.append({'portfolio_id': pid, 'name': pr['name'], 'campaign_id': c['id'], 'cost_units': cost, 'covered': full, 'gaps': gaps, 'false_rejection': pr['totals']['false_rejection'], 'within_budget': cap is None or cost <= cap})
        ok = [c for c in candidates if not c['gaps'] and c['within_budget']]
        ok.sort(key=lambda c: (c['cost_units'], c['false_rejection'], c['portfolio_id']))
        out = {'required_families': req, 'candidates': candidates, 'selected': ok[0] if ok else None, 'basis': 'dev-split campaign results on suite %s; held-out results are not used for selection; no provider self-report is an input' % body.get('suite_id')}
        if not ok:
            best = sorted(candidates, key=lambda c: (len(c['gaps']), c['cost_units'])) if candidates else []
            uncovered = best[0]['gaps'] if best else req
            out['recommendation'] = {'closest': best[0] if best else None, 'uncovered': uncovered,
                                     'measurement_source': 'shared_wrong_model' in uncovered or 'units_or_scale' in uncovered,
                                     'note': 'the uncovered families include a model-level fault that no amount of recomputation resolves: commission an independent measurement (acquisition plan) instead of more compute checks' if ('shared_wrong_model' in uncovered) else 'add a component covering the gap or raise the budget; coverage claims come from campaigns, not from names'}
        return out
