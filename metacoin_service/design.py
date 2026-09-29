"""§65-3 bounded experimental-design suggestions: rank a finite, user-provided set of candidate measurements under
a declared objective and cost policy, using a fitted calibration model's design geometry (pure Python).

Predicted utility is the model-based reduction of prediction variance (D-optimal log-determinant gain, or the
A-optimal variance reduction at declared target points) under the fitted linear model's assumptions. It is a
prediction: the *proven* information gain of a candidate is null until the measurement is recorded, the model is
refit and the comparison endpoint shows the change. Nothing here contacts an instrument, submits work or spends."""
import math

from .compute import calibration as cal
from .errors import ServiceError

MAX_CANDIDATES, MAX_SELECT, OBJECTIVES = 200, 50, ('reduce_overall_uncertainty', 'reduce_uncertainty_at_targets')


def _inverse(G):
    n = len(G)
    A = [row[:] + [1.0 if i == j else 0.0 for j in range(n)] for i, row in enumerate(G)]
    for c in range(n):
        piv = max(range(c, n), key=lambda r: abs(A[r][c]))
        if abs(A[piv][c]) < 1e-12:
            return None
        A[c], A[piv] = A[piv], A[c]
        f = A[c][c]; A[c] = [x / f for x in A[c]]
        for r in range(n):
            if r != c and A[r][c] != 0.0:
                g = A[r][c]; A[r] = [x - g * y for x, y in zip(A[r], A[c])]
    return [row[n:] for row in A]


def _z(manifest, feats):
    return [((feats[name] - float(m)) / float(s)) if manifest['scaling'] == 'standardize' else feats[name] for name, (m, s) in zip(manifest['kept_columns'], manifest['scaling_stats'])] + ([1.0] if manifest['intercept'] else [])


def _quad(Ginv, z):
    return sum(z[i] * sum(Ginv[i][j] * z[j] for j in range(len(z))) for i in range(len(z)))


def _bilinear(Ginv, a, b):
    return sum(a[i] * sum(Ginv[i][j] * b[j] for j in range(len(b))) for i in range(len(a)))


def _floats(features, expected):
    if type(features) is not dict or set(features) != set(expected):
        raise ServiceError('VALIDATION', {'code': 'features', 'expected': expected})
    out = {}
    for k, v in features.items():
        try:
            out[k] = cal.to_float(v)
        except ValueError as exc:
            raise ServiceError('VALIDATION', {'code': 'feature_value', 'feature': k, 'reason': str(exc)}) from None
    return out


def suggest(manifest, train_rows, body):
    """manifest: stored model manifest; train_rows: list of feature dicts the model was trained on; body: request."""
    feats = manifest['features']
    cands = body.get('candidates')
    if type(cands) is not list or not 1 <= len(cands) <= MAX_CANDIDATES:
        raise ServiceError('VALIDATION', 'candidates: 1..%d' % MAX_CANDIDATES)
    objective = body.get('objective', OBJECTIVES[0])
    if objective not in OBJECTIVES:
        raise ServiceError('VALIDATION', {'code': 'objective', 'allowed': OBJECTIVES})
    policy = body.get('cost_policy') or {}
    if type(policy) is not dict or policy.get('rank_by', 'utility_per_cost') not in ('utility_per_cost', 'utility') or (policy.get('budget') is not None and not isinstance(policy['budget'], (int, float, str))):
        raise ServiceError('VALIDATION', 'cost_policy: {budget?, rank_by: utility_per_cost|utility}')
    budget = float(policy['budget']) if policy.get('budget') is not None else None
    if budget is not None and not (budget >= 0 and math.isfinite(budget)):
        raise ServiceError('VALIDATION', 'budget')
    max_select = body.get('max_selected', MAX_SELECT)
    if type(max_select) is not int or not 1 <= max_select <= MAX_SELECT:
        raise ServiceError('VALIDATION', 'max_selected: 1..%d' % MAX_SELECT)
    targets = body.get('targets') or []
    if objective == 'reduce_uncertainty_at_targets' and not (type(targets) is list and 1 <= len(targets) <= 50):
        raise ServiceError('VALIDATION', 'targets: 1..50 feature dicts for this objective')
    parsed = []
    for i, c in enumerate(cands):
        if type(c) is not dict:
            raise ServiceError('VALIDATION', 'candidate %d' % i)
        vals = _floats(c.get('features'), feats)
        try:
            cost = cal.to_float(c.get('cost', 1))
        except ValueError:
            raise ServiceError('VALIDATION', 'candidate %d cost' % i) from None
        if not (cost >= 0 and math.isfinite(cost)):
            raise ServiceError('VALIDATION', 'candidate %d cost must be finite and >= 0' % i)
        label = c.get('label', 'c%d' % i)
        if type(label) is not str or len(label) > 64:
            raise ServiceError('VALIDATION', 'candidate label')
        parsed.append({'index': i, 'label': label, 'features': vals, 'cost': cost, 'z': _z(manifest, vals), 'domain_status': cal.domain_status(vals, manifest['domain'])[0]})
    tz = [_z(manifest, _floats(t, feats)) for t in targets]
    # Gram matrix of the standardized training design and its inverse (the model's own geometry)
    p = len(parsed[0]['z'])
    G = [[0.0] * p for _ in range(p)]
    for row in train_rows:
        z = _z(manifest, row)
        for i in range(p):
            for j in range(p):
                G[i][j] += z[i] * z[j]
    lam = float(manifest.get('ridge_lambda', '0') or 0)
    if lam:
        for i in range(p - (1 if manifest['intercept'] else 0)):
            G[i][i] += lam
    Ginv = _inverse(G)
    if Ginv is None:
        raise ServiceError('CONFLICT', {'code': 'singular_design', 'note': 'the training design is rank deficient; suggestions need a full-rank fitted geometry'})
    sigma2 = float(manifest['metrics']['train']['rmse']) ** 2
    seen, selected, remaining, spent = set(), [], list(parsed), 0.0
    def utility(c):
        h = _quad(Ginv, c['z'])
        if objective == 'reduce_overall_uncertainty':
            return math.log1p(h), h
        red = sum((_bilinear(Ginv, t, c['z']) ** 2) / (1.0 + h) for t in tz)     # A-optimal: Σ_t Δvar(t)
        return red, h
    rounds = 0
    while remaining and len(selected) < max_select:
        rounds += 1
        best, best_score = None, 0.0
        for c in remaining:
            if budget is not None and spent + c['cost'] > budget:
                c['skip'] = 'over_budget'; continue
            u, h = utility(c)
            score = (u / c['cost']) if policy.get('rank_by', 'utility_per_cost') == 'utility_per_cost' and c['cost'] > 0 else u
            c['last_utility'], c['last_leverage'] = u, h
            if score > best_score + 1e-15:
                best, best_score = c, score
        if best is None:
            break
        u, h = utility(best)
        selected.append({'rank': len(selected) + 1, 'label': best['label'], 'index': best['index'], 'features': {k: repr(v) for k, v in best['features'].items()}, 'cost': repr(best['cost']),
                         'domain_status': best['domain_status'], 'predicted_utility': repr(u), 'current_prediction_variance': repr(sigma2 * h), 'leverage': repr(h),
                         'proven_information_gain': None, 'basis': 'fitted linear model geometry (D-optimal log-det gain)' if objective == OBJECTIVES[0] else 'fitted linear model geometry (variance reduction summed over declared targets)'})
        spent += best['cost']
        # Sherman-Morrison update: the candidate is treated as measured for the next round (prediction, not proof)
        z = best['z']; Gz = [sum(Ginv[i][j] * z[j] for j in range(p)) for i in range(p)]
        for i in range(p):
            for j in range(p):
                Ginv[i][j] -= Gz[i] * Gz[j] / (1.0 + h)
        remaining.remove(best)
    not_selected = [{'label': c['label'], 'index': c['index'], 'reason': c.get('skip') or ('no_predicted_utility' if c.get('last_utility', 0) <= 1e-15 else 'budget_or_limit_reached'), 'domain_status': c['domain_status']} for c in remaining]
    return {'objective': objective, 'cost_policy': {'budget': repr(budget) if budget is not None else None, 'rank_by': policy.get('rank_by', 'utility_per_cost')}, 'selected': selected, 'not_selected': not_selected,
            'total_cost': repr(spent), 'training_rows': len(train_rows), 'residual_variance': repr(sigma2), 'targets': len(tz),
            'meaning': 'predicted utility under the fitted linear model; extrapolation candidates carry the model beyond its fitted domain and their utility estimate is less reliable',
            'proof': 'information gain is proven only after the measurement is recorded, the model is refit and the comparison shows the change; no candidate has been measured',
            'instrument_contact': 'none: suggestions only; nothing was submitted, scheduled or purchased'}
