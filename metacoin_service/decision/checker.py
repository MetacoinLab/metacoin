"""Independent witness checker (standard library only; does NOT import `kernels`).

Given the raw hypotheses, the raw views and a claimed ambiguity witness, it recomputes every projection itself and
decides whether the shared observation lies inside BOTH closed noise boxes of the two named hypotheses. It never uses the
producer's projections. A fabricated witness (a point outside one box, a pair with the same label, an unknown id) fails.
It also re-decides separation for a claimed separated pair. Used by the verification class `analytical` for decision
kinds and by the offline replay tool."""
from fractions import Fraction

CHECKER_ID = 'decision-witness-checker/v1'


def _frac(v):
    if isinstance(v, bool) or isinstance(v, float):
        raise ValueError('non-exact value')
    return Fraction(v)


def _factor(view, row_index, coord_units):
    """Unit factor of a row: 1 when the output unit equals the coordinate unit, else the declared conversion factor."""
    row = view['rows'][row_index]
    used = {coord_units[j] for j, a in enumerate(row) if _frac(a) != 0}
    out = view['output_units'][row_index]
    if not used or next(iter(used)) == out:
        return Fraction(1)
    conv = view.get('conversion') or {}
    return _frac(conv.get('factor', 0))


def project(view, state, coord_units):
    return [_factor(view, i, coord_units) * sum((_frac(a) * _frac(x) for a, x in zip(row, state)), Fraction(0)) for i, row in enumerate(view['rows'])]


def check_witness(hypotheses, views, coordinates, witness):
    """Returns {'ok': bool, 'checks': [...]} for one witness {pair, shared_observation: {view_id: [values]}}."""
    units = [c['unit'] for c in coordinates]
    by_id = {h['id']: h for h in hypotheses}
    checks = []
    a, b = (witness.get('pair') or [None, None])[:2]
    checks.append({'check': 'pair_known', 'ok': a in by_id and b in by_id and a != b})
    if not checks[-1]['ok']:
        return {'ok': False, 'checks': checks, 'checker': CHECKER_ID}
    checks.append({'check': 'labels_differ', 'ok': by_id[a]['label'] != by_id[b]['label']})
    obs = witness.get('shared_observation') or {}
    vids = {v['id'] for v in views}
    checks.append({'check': 'every_declared_view_witnessed', 'ok': set(obs) == vids, 'detail': {'missing': sorted(vids - set(obs)), 'unknown': sorted(set(obs) - vids)}})
    inside = True
    for v in views:
        if v['id'] not in obs:
            inside = False; continue
        try:
            o = [_frac(x) for x in obs[v['id']]]
            pa, pb = project(v, by_id[a]['state'], units), project(v, by_id[b]['state'], units)
            eps = [_frac(e) for e in v['noise']]
            if len(o) != len(pa):
                inside = False; checks.append({'check': 'view:' + v['id'], 'ok': False, 'detail': 'shape'}); continue
            ok_a = all(abs(p - x) <= e for p, x, e in zip(pa, o, eps)); ok_b = all(abs(p - x) <= e for p, x, e in zip(pb, o, eps))
            checks.append({'check': 'view:' + v['id'], 'ok': ok_a and ok_b, 'detail': {'inside_box_a': ok_a, 'inside_box_b': ok_b}})
            inside = inside and ok_a and ok_b
        except (ValueError, ZeroDivisionError, TypeError):
            inside = False; checks.append({'check': 'view:' + v['id'], 'ok': False, 'detail': 'invalid value'})
    checks.append({'check': 'observation_inside_both_boxes', 'ok': inside})
    return {'ok': all(c['ok'] for c in checks), 'checks': checks, 'checker': CHECKER_ID, 'meaning': 'the pair cannot be told apart by any declared view under the declared noise: a constructive reason to withhold the decision'}


def check_separated(hypotheses, views, coordinates, claim):
    """Re-decide a claimed separated pair: some named view must have a coordinate gap strictly greater than 2*eps."""
    units = [c['unit'] for c in coordinates]
    by_id = {h['id']: h for h in hypotheses}
    a, b = claim['pair']
    if a not in by_id or b not in by_id:
        return {'ok': False, 'checks': [{'check': 'pair_known', 'ok': False}], 'checker': CHECKER_ID}
    found = []
    for v in views:
        if v['id'] not in set(claim.get('separating_views') or []):
            continue
        pa, pb = project(v, by_id[a]['state'], units), project(v, by_id[b]['state'], units)
        if any(abs(x - y) > 2 * _frac(e) for x, y, e in zip(pa, pb, v['noise'])):
            found.append(v['id'])
    ok = bool(found) and set(found) == set(claim.get('separating_views') or [])
    return {'ok': ok, 'checks': [{'check': 'separating_views_confirmed', 'ok': ok, 'detail': {'confirmed': found}}], 'checker': CHECKER_ID}


def check_conditioning(hypotheses, views, coordinates, observations, claimed_surviving):
    """Recompute the surviving set under the observations and compare with the claim."""
    units = [c['unit'] for c in coordinates]
    surviving = []
    for h in hypotheses:
        ok = True
        for vid, vals in observations.items():
            v = next((x for x in views if x['id'] == vid), None)
            if v is None:
                return {'ok': False, 'checks': [{'check': 'observed_view_known', 'ok': False}], 'checker': CHECKER_ID}
            pred = project(v, h['state'], units)
            if any(abs(p - _frac(o)) > _frac(e) for p, o, e in zip(pred, vals, v['noise'])):
                ok = False; break
        if ok:
            surviving.append(h['id'])
    same = sorted(surviving) == sorted(claimed_surviving)
    return {'ok': same, 'checks': [{'check': 'surviving_set_recomputed', 'ok': same, 'detail': {'recomputed': sorted(surviving)}}], 'checker': CHECKER_ID}


def check_analysis(inputs, result):
    """Whole-result check of a decision-observability result against its raw inputs: every witness, every separated pair,
    the pair count, and (when present) the conditioning. Independent of the producing kernel."""
    hyps, views, coords = inputs['hypotheses'], inputs['views'], inputs['coordinates']
    checks = []
    labels = {}
    for h in hyps:
        labels[h['id']] = h['label']
    pairs = [(a['id'], b['id']) for i, a in enumerate(hyps) for b in hyps[i + 1:] if a['label'] != b['label']]
    checks.append({'check': 'decision_relevant_pair_count', 'ok': result.get('decision_relevant_pairs') == len(pairs), 'detail': {'recomputed': len(pairs)}})
    claimed = {tuple(w['pair']) for w in result.get('ambiguity_witnesses', [])} | {tuple(s['pair']) for s in result.get('separated_pairs', [])}
    checks.append({'check': 'every_pair_classified_once', 'ok': claimed == set(pairs) and len(result.get('ambiguity_witnesses', [])) + len(result.get('separated_pairs', [])) == len(pairs)})
    for w in result.get('ambiguity_witnesses', []):
        r = check_witness(hyps, views, coords, w)
        checks.append({'check': 'witness:%s|%s' % tuple(w['pair']), 'ok': r['ok']})
    for s in result.get('separated_pairs', []):
        r = check_separated(hyps, views, coords, s)
        checks.append({'check': 'separated:%s|%s' % tuple(s['pair']), 'ok': r['ok']})
    if result.get('conditioning'):
        c = result['conditioning']
        r = check_conditioning(hyps, views, coords, c['observations'], c['surviving'])
        checks.append({'check': 'conditioning_surviving_set', 'ok': r['ok']})
        expected = 'MODEL_INCONSISTENT_WITH_OBSERVATIONS' if not c['surviving'] else ('NO_DECISION_RELEVANT_ALTERNATIVES' if len(set(labels.values())) < 2 else ('DETERMINED_WITHIN_DECLARED_MODEL' if len({labels[i] for i in c['surviving']}) == 1 else 'AMBIGUOUS_AFTER_OBSERVATION'))
        checks.append({'check': 'conditioning_conclusion', 'ok': c.get('conclusion') == expected and result.get('conclusion') == expected})
    else:
        expected = 'NO_DECISION_RELEVANT_ALTERNATIVES' if len(set(labels.values())) < 2 else ('AMBIGUOUS' if result.get('ambiguity_witnesses') else 'SEPARATED_ON_DECLARED_SET')
        checks.append({'check': 'conclusion_consistent', 'ok': result.get('conclusion') == expected})
    return {'ok': all(c['ok'] for c in checks), 'checks': checks, 'checker': CHECKER_ID, 'scope': 'whole result re-decided from the raw hypotheses and views by an independent implementation (no producer projections used)'}
