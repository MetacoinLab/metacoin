"""Calibration mathematics shared by the compute child (primary fit with numpy's SVD-based lstsq) and the pure-Python
verification/prediction paths (Householder QR reference, metrics, standardization, domain checks).

Conventions bound to every fitted model:
  - features are standardized with mean/std learned from the TRAINING split only (zero-variance columns are dropped
    and reported); the target is not transformed;
  - intercept: an explicit unpenalized intercept column when requested;
  - ridge: penalty lambda * ||w||^2 on standardized coefficients (intercept unpenalized), solved as an augmented
    least-squares problem (no explicit inverse);
  - split: chronological (first train_fraction by row order), random (seeded shuffle) or index (explicit lists);
  - metrics: RMSE, MAE, max absolute error, R^2 on train and evaluation rows, with counts and the target unit;
  - prediction interval: empirical central interval of EVALUATION residuals at the declared level (an estimate under
    the assumption that new inputs resemble the evaluation rows; never a hard bound);
  - domain: per-feature [min, max] of the training rows; a query outside is labelled extrapolation."""
import math
import random
from decimal import Decimal, InvalidOperation

MAX_ROWS, MAX_FEATURES = 5000, 16
COEFF_TOLERANCE = {'rel': 1e-6, 'abs': 1e-9}


def to_float(v):
    if isinstance(v, bool):
        raise ValueError('boolean is not a number')
    if isinstance(v, int):
        return float(v)
    if isinstance(v, str):
        try:
            d = Decimal(v)
        except InvalidOperation:
            raise ValueError('not a decimal string: %r' % v[:20]) from None
        if not d.is_finite():
            raise ValueError('non-finite value')
        return float(d)
    if isinstance(v, float):
        if not math.isfinite(v):
            raise ValueError('non-finite value')
        return v
    raise ValueError('unsupported numeric type')


def split_rows(n, spec):
    """Returns (train_indices, eval_indices) and a description bound to the model."""
    method = spec.get('method', 'chronological')
    if method == 'index':
        tr, ev = spec['train'], spec['eval']
        return list(tr), list(ev), {'method': 'index', 'train': len(tr), 'eval': len(ev)}
    frac = spec.get('train_fraction_percent', 80) / 100.0
    k = max(1, int(round(n * frac)))
    if n >= 2:
        k = min(k, n - 1)
    idx = list(range(n))
    if method == 'random':
        rng = random.Random(spec.get('seed', 0))
        rng.shuffle(idx)
        return sorted(idx[:k]), sorted(idx[k:]), {'method': 'random', 'seed': spec.get('seed', 0), 'train': k, 'eval': n - k}
    return idx[:k], idx[k:], {'method': 'chronological', 'train': k, 'eval': n - k, 'note': 'first rows in dataset order train; later rows evaluate (reveals drift)'}


def standardize_stats(X_train, columns):
    stats, kept, dropped = [], [], []
    for j, name in enumerate(columns):
        col = [r[j] for r in X_train]
        mean = sum(col) / len(col)
        var = sum((c - mean) ** 2 for c in col) / len(col)
        std = math.sqrt(var)
        if std <= 1e-12 * max(1.0, abs(mean)):
            dropped.append(name)
        else:
            kept.append(j); stats.append((mean, std))
    return kept, stats, dropped


def design(rows, kept, stats, intercept, scaling='standardize'):
    out = []
    for r in rows:
        z = []
        for (j, (m, s)) in zip(kept, stats):
            z.append((r[j] - m) / s if scaling == 'standardize' else r[j])
        if intercept:
            z.append(1.0)
        out.append(z)
    return out


def householder_lstsq(A, b, ridge=0.0, n_pen=None):
    """Pure-Python least squares by Householder QR on the (optionally ridge-augmented) matrix. Returns (x, rank, r_diag).
    Independent of numpy; used as the verification reference and for small in-process refits."""
    m, n = len(A), len(A[0]) if A else 0
    M = [list(r) for r in A]; v = list(b)
    if ridge > 0:
        n_pen = n if n_pen is None else n_pen
        s = math.sqrt(ridge)
        for j in range(n_pen):
            row = [0.0] * n; row[j] = s
            M.append(row); v.append(0.0)
    m = len(M)
    rank = 0
    diag = []
    for k in range(min(m, n)):
        col = [M[i][k] for i in range(k, m)]
        norm = math.sqrt(sum(c * c for c in col))
        if norm <= 1e-14 * max(1.0, max(abs(M[i][j]) for i in range(m) for j in range(n))):
            diag.append(0.0); continue
        alpha = -norm if col[0] >= 0 else norm
        u = col[:]; u[0] -= alpha
        un = math.sqrt(sum(c * c for c in u))
        if un == 0:
            diag.append(alpha); rank += 1; continue
        u = [c / un for c in u]
        for j in range(k, n):
            dot = sum(u[i - k] * M[i][j] for i in range(k, m))
            for i in range(k, m):
                M[i][j] -= 2 * u[i - k] * dot
        dot = sum(u[i - k] * v[i] for i in range(k, m))
        for i in range(k, m):
            v[i] -= 2 * u[i - k] * dot
        diag.append(M[k][k]); rank += 1
    x = [0.0] * n
    for k in range(min(m, n) - 1, -1, -1):
        if abs(M[k][k]) <= 1e-14:
            x[k] = 0.0; continue
        x[k] = (v[k] - sum(M[k][j] * x[j] for j in range(k + 1, n))) / M[k][k]
    return x, rank, diag


def predict_rows(Z, coef):
    return [sum(a * b for a, b in zip(z, coef)) for z in Z]


def metrics(y, yhat):
    n = len(y)
    if n == 0:
        return {'n': 0}
    res = [a - b for a, b in zip(y, yhat)]
    mean = sum(y) / n
    ss_tot = sum((a - mean) ** 2 for a in y)
    ss_res = sum(r * r for r in res)
    return {'n': n, 'rmse': math.sqrt(ss_res / n), 'mae': sum(abs(r) for r in res) / n, 'max_abs_error': max(abs(r) for r in res),
            'r2': (1 - ss_res / ss_tot) if ss_tot > 0 else None, 'residual_mean': sum(res) / n}


def empirical_interval(residuals, level_percent):
    """Central empirical interval of residuals; None when fewer than 5 residuals (too few to claim a level)."""
    if len(residuals) < 5:
        return None
    r = sorted(residuals)
    alpha = (100 - level_percent) / 200.0
    lo = r[max(0, int(math.floor(alpha * (len(r) - 1))))]
    hi = r[min(len(r) - 1, int(math.ceil((1 - alpha) * (len(r) - 1))))]
    return {'level_percent': level_percent, 'low_offset': lo, 'high_offset': hi, 'basis': 'empirical central interval of %d evaluation residuals' % len(r),
            'assumption': 'new inputs resemble the evaluation rows; not a worst-case bound'}


def domain_of(X_train, columns):
    return {name: [min(r[j] for r in X_train), max(r[j] for r in X_train)] for j, name in enumerate(columns)}


def domain_status(features, domain):
    """Domain bounds may arrive as decimal strings (evidence format) or floats."""
    outside = {k: v for k, v in features.items() if k in domain and not (float(domain[k][0]) <= v <= float(domain[k][1]))}
    return ('extrapolation' if outside else 'interpolation'), outside


def apply_model(manifest, features):
    """Prediction from a stored manifest (pure Python): standardize with stored stats, dot with coefficients."""
    kept, stats = manifest['kept_columns'], manifest['scaling_stats']
    coef = [float(c) for c in manifest['coefficients']]
    z = []
    for name, (m, s) in zip(kept, stats):
        z.append((features[name] - float(m)) / float(s) if manifest['scaling'] == 'standardize' else features[name])
    if manifest['intercept']:
        z.append(1.0)
    return sum(a * b for a, b in zip(z, coef))
