"""Exact kernels for decision evidence (standard library only; no floats cross the input boundary).

Finite decision observability (Deliverable C): enumerated rational hypotheses with task labels, declared linear
views with componentwise closed noise boxes. Two hypotheses with different labels are separated by a view iff some
output coordinate has |A x_i - A x_j| > 2 eps (strict; touching boxes are ambiguous). For an unresolved pair the
kernel emits a concrete shared observation vector inside both boxes (the witness), which `checker` re-validates
from the raw hypotheses without trusting these projections.

Acquisition planning (Deliverable D): exhaustive subset search over a finite candidate menu (<= 12) maximising the
number of resolved cross-label pairs, then minimum integer cost, then fewest views, then lexicographic ids.
A pair count is not entropy, expected value, or a probability of a correct decision.

Sampling (Deliverable B): exact hypergeometric miss probability C(N-m,k)/C(N,k) decided by integer comparison
(no floating point near the threshold); a log-domain bound is provided for display and cross-checked against the
exact arithmetic. Reliability statistics (Deliverable F): Wilson interval, exact one-sided binomial upper bound
for zero (or few) failures, paired discordant comparison. Every function documents what it assumes."""
import hashlib
import itertools
import json
import math
import re
from fractions import Fraction

MODEL_ID = 'finite-decision-observability/v1'
ANALYSIS_SCHEMA = 'decision-observability-input/v1'
ANALYSIS_RESULT_SCHEMA = 'decision-observability-result/v1'
PLAN_SCHEMA = 'finite-acquisition-plan/v1'
SAMPLING_SCHEMA = 'finite-population-sampling-plan/v1'
LIMITS = {'hypotheses': 32, 'coordinates': 16, 'views': 64, 'rows_per_view': 16, 'total_rows': 256, 'menu_exact': 12, 'id_chars': 64, 'label_chars': 64, 'text_chars': 256,
          'rational_chars': 48, 'population': 1_000_000, 'exact_terms': 200_000, 'sample_cap': 1_000_000}
OBJECTIVES = ('unweighted_pairs/v1', 'weighted_pairs/v1')
CONCLUSIONS = ('AMBIGUOUS', 'SEPARATED_ON_DECLARED_SET', 'NO_DECISION_RELEVANT_ALTERNATIVES', 'DETERMINED_WITHIN_DECLARED_MODEL', 'AMBIGUOUS_AFTER_OBSERVATION', 'MODEL_INCONSISTENT_WITH_OBSERVATIONS')
# explicit, validated unit conversions: a coefficient that changes units is never implicit
CONVERSIONS = {('mJ', 'J'): Fraction(1, 1000), ('J', 'mJ'): Fraction(1000), ('mW', 'W'): Fraction(1, 1000), ('W', 'mW'): Fraction(1000), ('ms', 's'): Fraction(1, 1000), ('s', 'ms'): Fraction(1000),
               ('mK', 'K'): Fraction(1, 1000), ('K', 'mK'): Fraction(1000), ('mm', 'm'): Fraction(1, 1000), ('m', 'mm'): Fraction(1000), ('g', 'kg'): Fraction(1, 1000), ('kg', 'g'): Fraction(1000)}
_RATIONAL = re.compile(r'^-?[0-9]{1,24}(\.[0-9]{1,24})?$|^-?[0-9]{1,24}/[1-9][0-9]{0,24}$')


class KernelInvalid(ValueError):
    """Refusal with a stable code; never echoes private values."""

    def __init__(self, code, **detail):
        super().__init__(code)
        self.code, self.detail = code, detail


def _err(code, **detail):
    raise KernelInvalid(code, **detail)


def canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode()


def digest(obj):
    return hashlib.sha256(canonical(obj)).hexdigest()


# ---- exact numbers -------------------------------------------------------------------------------------------------------
def rational(value, code='rational'):
    """Integers, decimal strings or 'p/q' strings only. Floats, bools, NaN, infinity and unbounded strings are refused."""
    if isinstance(value, bool) or isinstance(value, float):
        _err(code, allowed='integer, decimal string or p/q string; floats and booleans are refused')
    if isinstance(value, int):
        if abs(value) >= 10 ** 24:
            _err(code, allowed='|integer| < 10^24')
        return Fraction(value)
    if isinstance(value, str):
        if len(value) > LIMITS['rational_chars'] or not _RATIONAL.match(value):
            _err(code, allowed='decimal or p/q string of at most %d characters' % LIMITS['rational_chars'])
        return Fraction(value)
    _err(code, allowed='integer or string')


def integer(value, lo, hi, code='integer'):
    if type(value) is not int or isinstance(value, bool) or not lo <= value <= hi:
        _err(code, allowed='integer %d..%d' % (lo, hi))
    return value


def text(value, code, maxlen=None, allow_empty=False):
    maxlen = maxlen or LIMITS['text_chars']
    if type(value) is not str or (not allow_empty and not value) or len(value) > maxlen:
        _err(code, allowed='string of 1..%d characters' % maxlen)
    return value


def frac_str(x):
    return str(x) if isinstance(x, Fraction) else str(Fraction(x))


# ---- finite decision observability -------------------------------------------------------------------------------------
def _view_norm(v, coords, units, code):
    if type(v) is not dict or set(v) - {'id', 'rows', 'noise', 'output_units', 'conversion', 'cost', 'provenance', 'quote_expires_at', 'provider_id', 'execution_modes', 'instrument'}:
        _err(code + '_fields', allowed=['id', 'rows', 'noise', 'output_units', 'conversion?', 'cost?', 'provenance?', 'quote_expires_at?', 'provider_id?', 'execution_modes?', 'instrument?'])
    vid = text(v.get('id'), code + '_id', LIMITS['id_chars'])
    rows = v.get('rows')
    if type(rows) is not list or not 1 <= len(rows) <= LIMITS['rows_per_view']:
        _err(code + '_rows', view=vid, allowed='1..%d rows' % LIMITS['rows_per_view'])
    dim = len(coords)
    nrows = []
    for r in rows:
        if type(r) is not list or len(r) != dim:
            _err(code + '_row_dimension', view=vid, expected=dim)
        nrows.append(tuple(rational(x, code + '_coefficient') for x in r))
    noise = v.get('noise')
    if type(noise) is not list or len(noise) != len(rows):
        _err(code + '_noise_shape', view=vid, expected=len(rows))
    eps = tuple(rational(e, code + '_noise') for e in noise)
    if any(e < 0 for e in eps):
        _err(code + '_noise_negative', view=vid)
    ou = v.get('output_units')
    if type(ou) is not list or len(ou) != len(rows) or not all(type(u) is str and 1 <= len(u) <= 16 for u in ou):
        _err(code + '_output_units', view=vid, expected=len(rows))
    conv = v.get('conversion')
    factors = []
    for i, (row, out_unit) in enumerate(zip(nrows, ou)):
        used = {units[j] for j, a in enumerate(row) if a != 0}
        if len(used) > 1:
            _err(code + '_mixed_units_in_row', view=vid, row=i, note='a row may combine coordinates of one unit only; coefficients are dimensionless weights')
        base = next(iter(used)) if used else out_unit
        if base == out_unit:
            factors.append(Fraction(1))
            continue
        if conv is None or type(conv) is not dict or set(conv) != {'from', 'to', 'factor'}:
            _err(code + '_unit_mismatch', view=vid, row=i, note='the row output unit differs from its coordinate unit: declare conversion {from, to, factor} matching the validated table')
        f = rational(conv['factor'], code + '_conversion_factor')
        if (conv['from'], conv['to']) != (base, out_unit) or CONVERSIONS.get((base, out_unit)) != f:
            _err(code + '_conversion_not_validated', view=vid, row=i, known=sorted('%s->%s' % k for k in CONVERSIONS))
        factors.append(f)
    cost = v.get('cost', 0)
    integer(cost, 0, 10 ** 12, code + '_cost')
    if 'provenance' in v:
        text(v['provenance'], code + '_provenance')
    if 'quote_expires_at' in v:
        integer(v['quote_expires_at'], 0, 2 ** 40, code + '_quote_expires_at')
    if 'execution_modes' in v and (type(v['execution_modes']) is not list or not v['execution_modes'] or not set(v['execution_modes']) <= {'local_worker', 'node', 'instrument'}):
        _err(code + '_execution_modes', allowed=['local_worker', 'node', 'instrument'])
    return {'id': vid, 'rows': tuple(nrows), 'noise': eps, 'output_units': list(ou), 'factors': tuple(factors), 'cost': cost, 'provenance': v.get('provenance'),
            'quote_expires_at': v.get('quote_expires_at'), 'provider_id': v.get('provider_id'), 'execution_modes': v.get('execution_modes'), 'instrument': v.get('instrument')}


def normalize(data):
    """Validate and normalise a decision-observability input. Returns (coords, hypotheses, views, observations, meta)."""
    if type(data) is not dict:
        _err('input_object')
    allowed = {'schema', 'model', 'candidate_set_version', 'measurement_menu_version', 'coordinates', 'hypotheses', 'views', 'observations', 'disclosure', 'question', 'private_label', 'provenance'}
    if set(data) - allowed:
        _err('input_unknown_field', unknown=sorted(set(data) - allowed))
    if data.get('schema') != ANALYSIS_SCHEMA:
        _err('input_schema', allowed=[ANALYSIS_SCHEMA])
    model = data.get('model')
    if type(model) is not dict or set(model) != {'id', 'version'} or model['id'] != MODEL_ID or type(model['version']) is not int or not 1 <= model['version'] <= 1:
        _err('model_version', allowed={'id': MODEL_ID, 'version': 1})
    for k in ('candidate_set_version', 'measurement_menu_version'):
        text(data.get(k), k, 64)
    coords = data.get('coordinates')
    if type(coords) is not list or not 1 <= len(coords) <= LIMITS['coordinates']:
        _err('coordinates', allowed='1..%d {name, unit}' % LIMITS['coordinates'])
    names, units = [], []
    for c in coords:
        if type(c) is not dict or set(c) != {'name', 'unit'}:
            _err('coordinate_fields', allowed=['name', 'unit'])
        names.append(text(c['name'], 'coordinate_name', 32)); units.append(text(c['unit'], 'coordinate_unit', 16))
    if len(set(names)) != len(names):
        _err('coordinate_duplicate')
    dim = len(coords)
    hs = data.get('hypotheses')
    if type(hs) is not list or not 2 <= len(hs) <= LIMITS['hypotheses']:
        _err('hypotheses', allowed='2..%d' % LIMITS['hypotheses'])
    hyps = []
    for h in hs:
        if type(h) is not dict or set(h) - {'id', 'label', 'state', 'provenance'} or not {'id', 'label', 'state'} <= set(h):
            _err('hypothesis_fields', allowed=['id', 'label', 'state', 'provenance?'])
        hid = text(h['id'], 'hypothesis_id', LIMITS['id_chars']); lab = text(h['label'], 'hypothesis_label', LIMITS['label_chars'])
        if type(h['state']) is not list or len(h['state']) != dim:
            _err('hypothesis_dimension', hypothesis=hid, expected=dim)
        if 'provenance' in h:
            text(h['provenance'], 'hypothesis_provenance')
        hyps.append({'id': hid, 'label': lab, 'state': tuple(rational(x, 'hypothesis_coordinate') for x in h['state']), 'provenance': h.get('provenance')})
    if len({h['id'] for h in hyps}) != len(hyps):
        _err('hypothesis_duplicate_id')
    vs = data.get('views')
    if type(vs) is not list or len(vs) > LIMITS['views']:
        _err('views', allowed='0..%d' % LIMITS['views'])
    views = [_view_norm(v, coords, units, 'view') for v in vs]
    if len({v['id'] for v in views}) != len(views):
        _err('view_duplicate_id')
    if sum(len(v['rows']) for v in views) > LIMITS['total_rows']:
        _err('view_total_rows', allowed=LIMITS['total_rows'])
    obs = data.get('observations')
    observations = None
    if obs is not None:
        if type(obs) is not dict or not obs or set(obs) - {v['id'] for v in views}:
            _err('observations_view_ids', note='observations are keyed by declared view ids')
        observations = {}
        for vid, vals in obs.items():
            v = next(x for x in views if x['id'] == vid)
            if type(vals) is not list or len(vals) != len(v['rows']):
                _err('observation_shape', view=vid, expected=len(v['rows']))
            observations[vid] = tuple(rational(x, 'observation_value') for x in vals)
    disc = data.get('disclosure', {'ids': False, 'labels': False, 'witness_values': False})
    if type(disc) is not dict or set(disc) != {'ids', 'labels', 'witness_values'} or not all(type(x) is bool for x in disc.values()):
        _err('disclosure', allowed={'ids': 'bool', 'labels': 'bool', 'witness_values': 'bool'})
    if 'question' in data:
        text(data['question'], 'question', 512)
    if 'private_label' in data:
        text(data['private_label'], 'private_label', 128)
    if 'provenance' in data:
        text(data['provenance'], 'provenance')
    meta = {'coordinates': [{'name': n, 'unit': u} for n, u in zip(names, units)], 'model': dict(model), 'candidate_set_version': data['candidate_set_version'], 'measurement_menu_version': data['measurement_menu_version'],
            'disclosure': dict(disc), 'question': data.get('question'), 'dimensionless': all(u == 'dimensionless' for u in units)}
    return hyps, views, observations, meta


def project(view, state):
    return tuple(f * sum((a * b for a, b in zip(row, state)), Fraction(0)) for row, f in zip(view['rows'], view['factors']))


def separated(a, b, view):
    """Closed noise boxes are disjoint iff some coordinate gap is STRICTLY greater than 2 eps (touching = ambiguous)."""
    return any(abs(x - y) > 2 * e for x, y, e in zip(project(view, a['state']), project(view, b['state']), view['noise']))


def shared_observation(a, b, view):
    """A point inside both closed boxes when they intersect (the midpoint of the intersection interval per coordinate)."""
    out = []
    for x, y, e in zip(project(view, a['state']), project(view, b['state']), view['noise']):
        lo, hi = max(x - e, y - e), min(x + e, y + e)
        if lo > hi:
            return None
        out.append((lo + hi) / 2)
    return tuple(out)


def model_digest(hyps, views, meta):
    """Digest of the declared candidate model (hypotheses, coordinates, versions) WITHOUT the measurement menu."""
    return digest({'model': meta['model'], 'candidate_set_version': meta['candidate_set_version'], 'coordinates': meta['coordinates'],
                   'hypotheses': [{'id': h['id'], 'label': h['label'], 'state': [frac_str(x) for x in h['state']]} for h in hyps]})


def views_digest(views):
    return digest([{'id': v['id'], 'rows': [[frac_str(a) for a in r] for r in v['rows']], 'noise': [frac_str(e) for e in v['noise']], 'output_units': v['output_units'], 'cost': v['cost']} for v in views])


def analyze(data):
    """Full analysis: pairwise separation on the declared set, witnesses for unresolved pairs and, when observations are
    supplied, conditioning of the candidate set. Exact; conclusions are about the DECLARED model only."""
    hyps, views, observations, meta = normalize(data)
    labels = {h['label'] for h in hyps}
    pairs = [(a, b) for a, b in itertools.combinations(hyps, 2) if a['label'] != b['label']]
    separated_pairs, witnesses = [], []
    for a, b in pairs:
        sep = [v['id'] for v in views if separated(a, b, v)]
        if sep:
            separated_pairs.append({'pair': [a['id'], b['id']], 'labels': [a['label'], b['label']], 'separating_views': sep})
        else:
            w = {v['id']: [frac_str(x) for x in shared_observation(a, b, v)] for v in views}
            witnesses.append({'pair': [a['id'], b['id']], 'labels': [a['label'], b['label']], 'states': [[frac_str(x) for x in a['state']], [frac_str(x) for x in b['state']]], 'shared_observation': w})
    if len(labels) < 2:
        conclusion = 'NO_DECISION_RELEVANT_ALTERNATIVES'
    else:
        conclusion = 'AMBIGUOUS' if witnesses else 'SEPARATED_ON_DECLARED_SET'
    out = {'result_schema': ANALYSIS_RESULT_SCHEMA, 'model_id': MODEL_ID, 'model_version': meta['model']['version'], 'candidate_set_version': meta['candidate_set_version'], 'measurement_menu_version': meta['measurement_menu_version'],
           'candidate_model_digest': model_digest(hyps, views, meta), 'views_digest': views_digest(views), 'coordinates': meta['coordinates'], 'dimensionless_model': meta['dimensionless'],
           'hypotheses': len(hyps), 'labels': sorted(labels), 'views': len(views), 'decision_relevant_pairs': len(pairs), 'separated_pairs': separated_pairs, 'ambiguity_witnesses': witnesses,
           'conclusion': conclusion, 'vacuous': len(labels) < 2,
           'separation_rule': 'closed noise boxes; separated iff some output coordinate gap > 2*eps (strict); touching intervals are ambiguous',
           'assumptions': ['enumerated rational hypotheses only (no continuous state space)', 'declared linear views with componentwise bounded noise in output units', 'coefficients are dimensionless weights; unit conversions only through the validated table',
                           'no probability over hypotheses is implied by their number', 'a model-level decision is not calibrated physical truth'],
           'scope': 'enumerated hypotheses, declared linear operators and bounded noise only; no physical truth or Hausdorff-dimension claim', 'disclosure': meta['disclosure'], 'question': meta['question']}
    if observations is not None:
        survivors, excluded = [], []
        for h in hyps:
            bad = []
            for vid, vals in observations.items():
                v = next(x for x in views if x['id'] == vid)
                pred = project(v, h['state'])
                if any(abs(p - o) > e for p, o, e in zip(pred, vals, v['noise'])):
                    bad.append(vid)
            (excluded if bad else survivors).append((h, bad))
        surviving_labels = sorted({h['label'] for h, _ in survivors})
        if not survivors:
            cond = 'MODEL_INCONSISTENT_WITH_OBSERVATIONS'
        elif len(labels) < 2:
            cond = 'NO_DECISION_RELEVANT_ALTERNATIVES'
        elif len(surviving_labels) == 1:
            cond = 'DETERMINED_WITHIN_DECLARED_MODEL'
        else:
            cond = 'AMBIGUOUS_AFTER_OBSERVATION'
        out['conditioning'] = {'observed_views': sorted(observations), 'observations': {k: [frac_str(x) for x in v] for k, v in observations.items()}, 'surviving': [h['id'] for h, _ in survivors], 'excluded': [{'id': h['id'], 'by_views': bad} for h, bad in excluded],
                               'surviving_labels': surviving_labels, 'determined_label': surviving_labels[0] if len(surviving_labels) == 1 and survivors else None, 'conclusion': cond,
                               'coverage_limitation': 'determined WITHIN the declared candidate model: a state outside the enumerated set is not considered; no nearest hypothesis is chosen when none is compatible',
                               'remaining_witnesses': [w for w in witnesses if w['pair'][0] in {h['id'] for h, _ in survivors} and w['pair'][1] in {h['id'] for h, _ in survivors}]}
        out['conclusion'] = cond
    return out


def check_scaled_consistency(data, view_id, factor):
    """Helper for the scaling property: scale a view's rows AND its noise by the same positive rational; separation decisions are invariant."""
    f = Fraction(factor)
    d = json.loads(json.dumps(data))
    for v in d['views']:
        if v['id'] == view_id:
            v['rows'] = [[frac_str(rational(a) * f) for a in r] for r in v['rows']]
            v['noise'] = [frac_str(rational(e) * f) for e in v['noise']]
    return d


# ---- acquisition planning ----------------------------------------------------------------------------------------------------
def plan_acquisition(data, candidates, budget, objective='unweighted_pairs/v1', weights=None):
    """Exact subset search over a finite menu. `data` is an analysis input whose `views` are the EXISTING (sunk) views;
    `candidates` are menu entries in the same view format (each with an integer cost). Returns the chosen subset and the
    before/after resolution, with the optimality status stated for this finite objective only."""
    integer(budget, 0, 10 ** 12, 'budget')
    if objective not in OBJECTIVES:
        _err('objective', allowed=list(OBJECTIVES))
    if type(candidates) is not list or not 1 <= len(candidates) <= LIMITS['menu_exact']:
        _err('menu_size_unsupported', allowed='1..%d candidates for exact search; larger menus are refused rather than solved heuristically' % LIMITS['menu_exact'])
    hyps, existing, _, meta = normalize(dict(data, observations=None))
    probe = normalize(dict(data, observations=None, views=list(data.get('views', [])) + candidates))
    cands = probe[1][len(existing):]
    if len({v['id'] for v in existing} & {v['id'] for v in cands}) or len({v['id'] for v in cands}) != len(cands):
        _err('menu_duplicate_view_id')
    pairs = [(a, b) for a, b in itertools.combinations(hyps, 2) if a['label'] != b['label']]
    w = {}
    if objective == 'weighted_pairs/v1':
        if type(weights) is not dict or not weights or len(weights) > 64:
            _err('weights', note='weighted_pairs/v1 needs requester-frozen integer weights keyed "LABEL_A|LABEL_B" (sorted)')
        for k, val in weights.items():
            text(k, 'weight_key', 2 * LIMITS['label_chars'] + 1); integer(val, 0, 10 ** 9, 'weight_value')
            w[k] = val
        if any('|'.join(sorted((a['label'], b['label']))) not in w for a, b in pairs):
            _err('weights_incomplete', note='every cross-label pair needs a frozen weight')
    def weight(i):
        a, b = pairs[i]
        return w['|'.join(sorted((a['label'], b['label'])))] if w else 1
    covered = {i for i, (a, b) in enumerate(pairs) if any(separated(a, b, v) for v in existing)}
    coverage = [{i for i, (a, b) in enumerate(pairs) if separated(a, b, v)} for v in cands]
    best, best_sel = None, None
    for mask in range(1 << len(cands)):
        sel = [i for i in range(len(cands)) if mask >> i & 1]
        cost = sum(cands[i]['cost'] for i in sel)
        if cost > budget:
            continue
        union = set(covered)
        for i in sel:
            union |= coverage[i]
        score = (-sum(weight(i) for i in union), cost, len(sel), sorted(cands[i]['id'] for i in sel))
        if best is None or score < best:
            best, best_sel = score, sel
    chosen = [cands[i] for i in best_sel]
    after = set(covered)
    for i in best_sel:
        after |= coverage[i]
    remaining = [{'pair': [pairs[i][0]['id'], pairs[i][1]['id']], 'labels': [pairs[i][0]['label'], pairs[i][1]['label']]} for i in range(len(pairs)) if i not in after]
    unaffordable = [v['id'] for v in cands if v['cost'] > budget]
    per_candidate = [{'id': v['id'], 'cost': v['cost'], 'resolves_pairs': sorted(coverage[i] - covered), 'new_pairs': len(coverage[i] - covered), 'selected': i in best_sel, 'quote_expires_at': v['quote_expires_at'], 'provider_id': v['provider_id'], 'execution_modes': v['execution_modes']} for i, v in enumerate(cands)]
    return {'schema': PLAN_SCHEMA, 'objective': objective, 'weights': w or None, 'budget': budget, 'selected_views': [v['id'] for v in chosen], 'cost': best[1], 'individual_costs': {v['id']: v['cost'] for v in chosen},
            'resolved_before': len(covered), 'resolved_after': len(after), 'total_relevant_pairs': len(pairs), 'weighted_resolved_after': -best[0], 'remaining_ambiguities': remaining, 'unaffordable_candidates': unaffordable,
            'candidates': per_candidate, 'subsets_examined': 1 << len(cands), 'optimality': 'exact: exhaustive over the declared finite candidate menu for this objective (%s); no claim beyond the menu' % objective,
            'optimization_status': 'exact', 'candidate_model_digest': model_digest(hyps, existing, meta), 'existing_views_digest': views_digest(existing), 'menu_digest': views_digest(cands),
            'assumptions': ['each candidate can be purchased once at its quoted integer cost', 'delivered measurements obey the declared noise bounds (sensor errors as declared, not calibrated here)',
                            'existing observations are sunk costs', 'execution cost is the quoted cost only; no delivery-time or failure cost is modelled', 'pair coverage is not entropy, expected scientific value or a probability of a correct decision'],
            'nothing_to_acquire': not chosen, 'predicted': 'under the declared model the selected views separate %d of %d decision-relevant pairs; the prediction is proven only after delivery, conditioning and checking' % (len(after), len(pairs))}


def plan_oracle(data, candidates, budget):
    """Independent brute force: enumerate subsets by size with itertools, decide separation by interval disjointness
    (a different code path from `separated`), return the best (resolved, cost, count, ids) tuple."""
    hyps, existing, _, _ = normalize(dict(data, observations=None))
    cands = normalize(dict(data, observations=None, views=list(data.get('views', [])) + candidates))[1][len(existing):]
    pairs = [(a, b) for a, b in itertools.combinations(hyps, 2) if a['label'] != b['label']]
    def disjoint(a, b, v):
        for x, y, e in zip(project(v, a['state']), project(v, b['state']), v['noise']):
            if max(x - e, y - e) > min(x + e, y + e):
                return True
        return False
    best = None
    for size in range(len(cands) + 1):
        for combo in itertools.combinations(range(len(cands)), size):
            cost = sum(cands[i]['cost'] for i in combo)
            if cost > budget:
                continue
            resolved = sum(1 for a, b in pairs if any(disjoint(a, b, v) for v in existing) or any(disjoint(a, b, cands[i]) for i in combo))
            key = (-resolved, cost, len(combo), sorted(cands[i]['id'] for i in combo))
            if best is None or key < best:
                best = key
    return {'resolved_after': -best[0], 'cost': best[1], 'selected_views': best[3]}


# ---- exact finite-population sampling ----------------------------------------------------------------------------------------
def _terms(n, m, k):
    """miss = prod_{i<cnt} (n-removed-i)/(n-i) with cnt=min(k,m), removed=max(k,m) (the two products are equal)."""
    cnt, removed = (k, m) if k <= m else (m, k)
    return cnt, removed


def miss_probability(n, m, k):
    """Exact Fraction C(n-m,k)/C(n,k): probability that k items drawn uniformly without replacement from n miss all of m
    specific items. Bounded to LIMITS['exact_terms'] multiplications."""
    integer(n, 1, LIMITS['population'], 'population'); integer(m, 0, n, 'minimum_defects'); integer(k, 0, n, 'sample_count')
    if k > n - m:
        return Fraction(0)
    cnt, removed = _terms(n, m, k)
    if cnt > LIMITS['exact_terms']:
        _err('exact_terms_unsupported', allowed=LIMITS['exact_terms'])
    num = math.prod(range(n - removed - cnt + 1, n - removed + 1)) if cnt else 1
    den = math.prod(range(n - cnt + 1, n + 1)) if cnt else 1
    return Fraction(num, den)


def miss_at_most(n, m, k, alpha):
    """Decide miss_probability(n,m,k) <= alpha by INTEGER comparison (no rounding). alpha is a Fraction in (0,1)."""
    if k > n - m:
        return True
    cnt, removed = _terms(n, m, k)
    if cnt > LIMITS['exact_terms']:
        _err('exact_terms_unsupported', allowed=LIMITS['exact_terms'])
    num = math.prod(range(n - removed - cnt + 1, n - removed + 1)) if cnt else 1
    den = math.prod(range(n - cnt + 1, n + 1)) if cnt else 1
    return num * alpha.denominator <= alpha.numerator * den


def miss_log_bound(n, m, k):
    """Log-domain estimate with a conservative upward rounding margin; for display only. Never returns a floating zero as
    proof: when the value underflows the result is reported as an upper bound string."""
    if k > n - m:
        return {'upper': 0.0, 'note': 'k > n - m: every sample contains a defective item (exact)'}
    cnt, removed = _terms(n, m, k)
    s = sum(math.log(n - removed - i) - math.log(n - i) for i in range(cnt))
    margin = 4 * cnt * 2.3e-16 + 1e-300
    upper = math.exp(min(0.0, s + margin)) if s + margin > -700 else None
    return {'upper': upper, 'log_upper': s + margin, 'rounding': 'upward (conservative) by %d ulps per factor' % 4, 'note': None if upper is not None else 'underflow: reported as log bound only, not as zero'}


def sampling_plan(population, minimum_defects, alpha, max_samples, *, frame_digest=None, checker_digest=None, fault_model=None, sample_unit=None, defect_fraction=None):
    """Smallest k with C(N-m,k)/C(N,k) <= alpha; infeasible (never silently relaxed) when k exceeds the budget.
    `defect_fraction` ('p/q' string) converts to m = ceil(N * p/q) (documented ceiling rule) when minimum_defects is None."""
    n = integer(population, 1, LIMITS['population'], 'population')
    if minimum_defects is None:
        if defect_fraction is None:
            _err('minimum_defects_or_fraction_required')
        fr = rational(defect_fraction, 'defect_fraction')
        if not 0 < fr <= 1:
            _err('defect_fraction', allowed='rational in (0, 1]')
        m = -((-n * fr.numerator) // fr.denominator)
        rule = 'm = ceil(N * %s) = %d (ceiling rule: a fractional defect count rounds UP to the next whole item)' % (defect_fraction, m)
    else:
        m = integer(minimum_defects, 1, n, 'minimum_defects'); rule = 'm declared directly'
    a = rational(alpha, 'alpha')
    if not 0 < a < 1:
        _err('alpha', allowed='rational in (0, 1)')
    cap = integer(max_samples, 0, n, 'max_samples')
    lo, hi = 0, n
    while lo < hi:
        mid = (lo + hi) // 2
        if miss_at_most(n, m, mid, a):
            hi = mid
        else:
            lo = mid + 1
    k = lo
    feasible = k <= cap
    achieved_k = min(k, cap)
    exact_achieved = miss_probability(n, m, achieved_k) if _terms(n, m, achieved_k)[0] <= 4096 else None
    plan = {'schema': SAMPLING_SCHEMA, 'population': n, 'minimum_defects': m, 'minimum_defects_rule': rule, 'minimum_defects_meaning': 'an ASSUMPTION of the claim (at least m defective items), not an observed number of defects',
            'alpha': str(a), 'max_samples': cap, 'required_samples': k, 'feasible': feasible, 'evaluated_samples': achieved_k,
            'achieved_bound': {'exact': str(exact_achieved) if exact_achieved is not None else None, 'float': float(exact_achieved) if exact_achieved is not None else None, 'log_domain': miss_log_bound(n, m, achieved_k)},
            'required_budget': k, 'frame_digest': frame_digest, 'checker_digest': checker_digest, 'fault_model': fault_model or 'a defective item is detected whenever it is sampled (perfect itemwise checker); defects are fixed before sampling',
            'sample_unit': sample_unit or 'one committed item of the frozen population frame', 'sampling_model': 'uniform without replacement from an immutable population of N committed items',
            'claim': 'P(all k sampled items pass | at least m of N items are defective) <= alpha; this is NOT P(result correct | audit passed) and says nothing about model misspecification shared by producer and checker',
            'arithmetic': 'exact integer comparison of C(N-m,k)/C(N,k) against alpha (no floating point at the decision threshold); binary search over k'}
    if not feasible:
        plan['refusal'] = {'code': 'sample_budget_insufficient', 'required_samples': k, 'max_samples': cap, 'achieved_bound_at_budget': plan['achieved_bound'], 'note': 'alpha and m are never relaxed silently; raise the budget, accept a weaker claim explicitly, or use full recomputation'}
    return plan


def exhaustive_miss(n, m, k):
    """Independent oracle for tests: enumerate all k-subsets and count those avoiding the first m items."""
    bad = set(range(m)); draws = list(itertools.combinations(range(n), k))
    return Fraction(sum(1 for d in draws if not bad.intersection(d)), len(draws))


# ---- reliability statistics ---------------------------------------------------------------------------------------------------------
def wilson(failures, trials, z=1.959963984540054):
    integer(trials, 1, 10 ** 12, 'trials'); integer(failures, 0, trials, 'failures')
    if isinstance(z, bool) or not isinstance(z, (int, float)) or not math.isfinite(z) or z <= 0:
        _err('z')
    p = failures / trials; d = 1 + z * z / trials
    c = (p + z * z / (2 * trials)) / d; r = z * math.sqrt(p * (1 - p) / trials + z * z / (4 * trials * trials)) / d
    return max(0.0, c - r), min(1.0, c + r)


def zero_failure_upper(trials, alpha=0.05):
    """Exact one-sided binomial upper bound on the failure probability when 0 of n independent representative trials failed."""
    integer(trials, 1, 10 ** 12, 'trials')
    if isinstance(alpha, bool) or not isinstance(alpha, (int, float)) or not 0 < alpha < 1:
        _err('alpha')
    return -math.expm1(math.log(alpha) / trials)


def _log_binom_cdf(x, n, p):
    if p <= 0:
        return 0.0
    if p >= 1:
        return 0.0 if x < n else 1.0
    lp, lq = math.log(p), math.log1p(-p)
    terms = [math.lgamma(n + 1) - math.lgamma(i + 1) - math.lgamma(n - i + 1) + i * lp + (n - i) * lq for i in range(x + 1)]
    mx = max(terms)
    return mx + math.log(sum(math.exp(t - mx) for t in terms))


def binomial_upper(failures, trials, alpha=0.05):
    """One-sided Clopper-Pearson upper bound for x failures in n independent trials (bisection on the exact binomial CDF)."""
    integer(trials, 1, 10 ** 9, 'trials'); integer(failures, 0, trials, 'failures')
    if failures == 0:
        return zero_failure_upper(trials, alpha)
    if failures == trials:
        return 1.0
    lo, hi = failures / trials, 1.0
    for _ in range(200):
        mid = (lo + hi) / 2
        if math.exp(_log_binom_cdf(failures, trials, mid)) > alpha:
            lo = mid
        else:
            hi = mid
    return hi


def paired_comparison(outcomes_a, outcomes_b):
    """Paired outcomes on the SAME cases (True = the portfolio caught the fault / behaved correctly). Reports discordant
    counts and an exact two-sided sign-test p-value; never multiplies marginal rates."""
    if len(outcomes_a) != len(outcomes_b) or not outcomes_a:
        _err('paired_outcomes', note='equal-length non-empty lists over the same cases')
    b = sum(1 for x, y in zip(outcomes_a, outcomes_b) if (not x) and y); c = sum(1 for x, y in zip(outcomes_a, outcomes_b) if x and not y)
    n = b + c
    if n == 0:
        pval = 1.0
    else:
        k = min(b, c)
        cdf = sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n
        pval = min(1.0, 2 * cdf)
    return {'cases': len(outcomes_a), 'both_correct': sum(1 for x, y in zip(outcomes_a, outcomes_b) if x and y), 'both_wrong': sum(1 for x, y in zip(outcomes_a, outcomes_b) if not x and not y),
            'only_b_correct': b, 'only_a_correct': c, 'exact_sign_test_p_two_sided': pval, 'independent_unit': 'one preregistered fault case (paired across portfolios); repeated seeds or rounds inside a case are not fresh trials'}


def residual_ratio(failures_a, failures_b, trials, alpha=0.05):
    """Escape-rate ratio A/B with a zero-denominator rule: undefined estimate, bounded below by rate_A / upper_B."""
    integer(trials, 1, 10 ** 9, 'trials'); integer(failures_a, 0, trials, 'failures_a'); integer(failures_b, 0, trials, 'failures_b')
    ub = binomial_upper(failures_b, trials, alpha)
    out = {'rate_a': failures_a / trials, 'rate_b': failures_b / trials, 'upper_b': ub, 'estimate': (failures_a / failures_b) if failures_b else None,
           'lower_bound': (failures_a / trials) / ub if ub > 0 else None, 'note': 'ratio of escape rates on the same cases; common-mode faults (shared model, shared library) cap any reduction and are reported separately'}
    if failures_b == 0:
        out['estimate_status'] = 'undefined (zero denominator): the lower bound uses the one-sided upper bound on B at alpha=%s; no infinite improvement is claimed' % alpha
    else:
        out['estimate_status'] = 'point estimate; uncertainty from the paired analysis'
    return out
