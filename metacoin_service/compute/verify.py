"""Verification phase run by the worker (pure Python, no numpy) on the child's published outputs, before
scientific acceptance. Each function returns a machine-readable status stating which mode actually ran,
how much was checked, and every mismatch (bounded)."""
import json
from . import inputs, npy, reference
from .kernels import TEMPORAL_COLUMNS, OUTCOME_CODES
from .. import temporal

MAX_MISMATCHES = 20


def temporal_batch(data, files, manifest):
    vals, dtype, shape = npy.read_bytes(files['results.npy']) if hasattr(npy, 'read_bytes') else npy.decode(files['results.npy'])
    total = inputs.batch_total(data)
    w = len(TEMPORAL_COLUMNS)
    if shape != [total, w] or dtype != '<i8':
        return {'mode': 'exact_all', 'passed': False, 'checked': 0, 'mismatches': [{'reason': 'results shape %s does not match the accepted batch [%d, %d]' % (shape, total, w)}]}
    threshold, sampled = manifest['verification_policy']['exact_all_threshold'], manifest['verification_policy']['sampled_count']
    requested = data.get('verification', 'auto')
    if requested == 'exact_all' or (requested == 'auto' and total <= threshold):
        indices, mode = range(total), 'exact_all'
    else:
        step = max(1, total // sampled)
        indices, mode = sorted(set(range(0, total, step)) | {total - 1}), 'exact_sampled'
    mismatches, checked = [], 0
    for i in indices:
        row = vals[i * w:(i + 1) * w]
        sid, sc, _ = inputs.scenario(data, i)
        ref = reference.temporal_reference_row(sc)
        checked += 1
        if row != ref:
            mismatches.append({'index': i, 'id': sid, 'columns': [c for c, a, b in zip(TEMPORAL_COLUMNS, row, ref) if a != b]})
            if len(mismatches) >= MAX_MISMATCHES:
                break
    return {'mode': mode, 'passed': not mismatches, 'checked': checked, 'total': total, 'rule': manifest['verification_policy']['rule'], 'mismatches': mismatches,
            'statement': 'every checked scenario was recomputed by the temporal-energy/v1 reference verifier and compared exactly' + ('' if mode == 'exact_all' else '; unchecked scenarios are not individually proven')}


def monte_carlo(data, files, manifest, summary):
    audit = json.loads(files['samples_audit.json'])
    total = data['samples']
    mode = 'exact_all' if len(audit) >= total else 'sampled_audit'
    mismatches, checked = [], 0
    for entry in audit:
        p = entry['params']
        var = {}
        if 'initial_energy' in p:
            var['initial_low'] = var['initial_high'] = p['initial_energy']
        for k in ('reserve', 'harvest_scale_percent', 'load_scale_percent', 'leakage_scale_percent'):
            if k in p:
                var[k] = p[k]
        sc = inputs.apply_variation(data['base'], var)
        ref = temporal.analyze(sc)
        checked += 1
        if (1 if ref['outcome'] == 'FEASIBLE' else 0) != entry['outcome']:
            mismatches.append({'index': entry['index'], 'kernel': entry['outcome'], 'reference': ref['outcome']})
            if len(mismatches) >= MAX_MISMATCHES:
                break
    consistent = 0 <= summary['events'] <= summary['samples'] == total
    if mode == 'exact_all' and not mismatches:
        consistent = consistent and sum(e['outcome'] for e in audit) == summary['events']
    return {'mode': mode, 'passed': not mismatches and consistent, 'checked': checked, 'total': total, 'rule': manifest['verification_policy']['rule'], 'mismatches': mismatches,
            'counts_consistent': consistent,
            'statement': ('all samples' if mode == 'exact_all' else '%d audited samples' % checked) + ' re-evaluated by the temporal-energy/v1 reference from the regenerated parameters; the sampler mapping itself is covered by the unit reference tests, not by this audit'}


def heat(data, files, manifest, summary):
    p = inputs.validate_heat(data)
    rx, ry = float(p['rx']), float(p['ry'])
    nx, ny = p['nx'], p['ny']
    fvals, fd, fshape = npy.decode(files['field.npy'])
    checks, passed = [], True
    def add(name, ok, detail):
        nonlocal passed
        checks.append({'check': name, 'ok': bool(ok), 'detail': detail}); passed = passed and bool(ok)
    add('shape', fshape == [ny, nx] and fd == '<f8', fshape)
    rows = [fvals[j * nx:(j + 1) * nx] for j in range(ny)]
    finite = all(v == v and abs(v) != float('inf') for v in fvals)
    add('finite', finite, 'every value finite')
    from decimal import Decimal
    bv = {s: float(Decimal(data['boundary']['values'][s])) for s in ('left', 'right', 'top', 'bottom')}
    add('boundary_fixed', all(rows[j][0] == bv['left'] and rows[j][-1] == bv['right'] for j in range(ny)) and all(rows[0][i] == bv['bottom'] and rows[-1][i] == bv['top'] for i in range(nx)), 'Dirichlet values unchanged')
    f0 = __import__('metacoin_service.compute.kernels', fromlist=['heat_initial_field']).heat_initial_field(data, p)
    lo0, hi0 = min(min(r) for r in f0), max(max(r) for r in f0)
    eps = 1e-9 * max(1.0, abs(lo0), abs(hi0))
    add('discrete_maximum_principle', lo0 - eps <= min(fvals) and max(fvals) <= hi0 + eps, {'initial_range': [lo0, hi0], 'final_range': [min(fvals), max(fvals)], 'assumption': 'r_x + r_y <= 1/2'})
    tol = manifest['verification_policy']['tolerance']
    scale = max(1.0, abs(lo0), abs(hi0))
    if 'field_prev.npy' in files and p['steps'] >= 1:
        pv, _, pshape = npy.decode(files['field_prev.npy'])
        prev = [pv[j * nx:(j + 1) * nx] for j in range(ny)]
        recomputed = reference.heat_reference(prev, rx, ry, 1)
        diff = reference.heat_max_abs_diff(recomputed, rows)
        add('last_step_recomputed', diff <= tol['abs'] + tol['rel'] * scale, {'max_abs_diff': diff, 'tolerance': tol})
    mode = 'invariants_and_last_step'
    if nx * ny * p['steps'] <= manifest['verification_policy']['exact_reference_max_cell_steps']:
        ref = reference.heat_reference(f0, rx, ry, p['steps'])
        diff = reference.heat_max_abs_diff(ref, rows)
        add('exact_reference_full_run', diff <= tol['abs'] + tol['rel'] * scale, {'max_abs_diff': diff, 'cell_steps': nx * ny * p['steps']})
        mode = 'exact_reference'
    return {'mode': mode, 'passed': passed, 'checks': checks, 'tolerance': tol,
            'statement': 'invariants and an independent scalar recomputation' + (' of the whole run' if mode == 'exact_reference' else ' of the final step') + '; evidence under the FTCS model assumptions, not a physical validation'}


def run(kind, data, files, manifest, summary):
    if kind == 'temporal_batch':
        return temporal_batch(data, files, manifest)
    if kind == 'monte_carlo_reliability':
        return monte_carlo(data, files, manifest, summary)
    return heat(data, files, manifest, summary)
