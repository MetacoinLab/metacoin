"""Independent CPU reference for the lunar products: written separately from terrain.py/route.py (Fraction gradients, atan2,
explicit neighbour walks) so a verification can compare the producer's result with a second arrangement of the same declared
model. Tolerance: 1 milli-degree on slopes (rounding of two float paths), exact on flags, energies, lengths and witnesses."""
import math
from fractions import Fraction

from . import route as route_mod, terrain as terrain_mod

CHECKER_ID = 'lunar-reference-checker/v1'
SLOPE_TOL_MDEG = 1


def slope_cell(h, i, j, ns, ew, sigma):
    """Returns (slope, low, high, flag) by its own arrangement: Fraction gradients, atan2 of hypot."""
    L, S = len(h), len(h[0])
    if h[i][j] is None:
        return None, None, None, 2
    edge = i in (0, L - 1) or j in (0, S - 1)
    jl, jr = max(j - 1, 0), min(j + 1, S - 1); iu, idn = max(i - 1, 0), min(i + 1, L - 1)
    vals = (h[i][jl], h[i][jr], h[iu][j], h[idn][j])
    if any(v is None for v in vals):
        return None, None, None, 2
    gx = Fraction(h[i][jr] - h[i][jl], (jr - jl) * ew); gy = Fraction(h[iu][j] - h[idn][j], (idn - iu) * ns)
    ex = Fraction(2 * sigma, (jr - jl) * ew); ey = Fraction(2 * sigma, (idn - iu) * ns)
    def deg(a, b):
        return int(round(math.degrees(math.atan2(math.hypot(float(a), float(b)), 1.0)) * 1000))
    return deg(gx, gy), deg(max(abs(gx) - ex, 0), max(abs(gy) - ey, 0)), deg(abs(gx) + ex, abs(gy) + ey), 1 if edge else 0


def check_terrain(inputs, result):
    """Recompute every cell independently; compare slopes within tolerance and flags exactly; recompute stats."""
    src = inputs['source']; par = inputs.get('parameters', terrain_mod.DEFAULT_PARAMETERS); scale = src['units']['dn_scaling_factor_milli']; nodata = src.get('nodata_dn')
    h = [[None if (nodata is not None and v == nodata) else v * scale for v in r] for r in src['dn']]
    checks = []; mismatches = 0; nodata_cells = 0; over = 0; mx = 0
    for i in range(len(h)):
        ns, ew = terrain_mod.row_spacing(src, i)
        for j in range(len(h[0])):
            s, lo, hi, f = slope_cell(h, i, j, ns, ew, par['vertical_error_mm'])
            got = (result['slope_milli_deg'][i][j], result['slope_low_milli_deg'][i][j], result['slope_high_milli_deg'][i][j], result['flags'][i][j])
            ok = f == got[3] and ((s is None and got[0] is None) or (s is not None and got[0] is not None and abs(s - got[0]) <= SLOPE_TOL_MDEG and abs(lo - got[1]) <= SLOPE_TOL_MDEG and abs(hi - got[2]) <= SLOPE_TOL_MDEG))
            mismatches += not ok
            if s is None:
                nodata_cells += 1
            else:
                over += s > par['slope_threshold_milli_deg']; mx = max(mx, s)
    checks.append({'check': 'slope_cells_within_tolerance', 'ok': mismatches == 0, 'detail': {'mismatches': mismatches, 'tolerance_milli_deg': SLOPE_TOL_MDEG}})
    st = result['stats']
    checks.append({'check': 'stats', 'ok': st['nodata_cells'] == nodata_cells and abs(st['max_slope_milli_deg'] - mx) <= SLOPE_TOL_MDEG and abs(st['cells_over_threshold'] - over) <= 2, 'detail': {'reference': {'nodata_cells': nodata_cells, 'max_slope_milli_deg': mx, 'cells_over_threshold': over}, 'producer': st}})
    checks.append({'check': 'outcome', 'ok': result['outcome'] == ('TILE_PARTIAL' if nodata_cells else 'TILE_COMPLETE')})
    checks.append({'check': 'heights', 'ok': result['height_mm'] == h})
    checks.append({'check': 'provenance_bound', 'ok': result['provenance'].get('product_id') == src.get('product_id') and result['provenance'].get('image_sha256') == src.get('image_sha256') and result['provenance'].get('label') == src.get('label')})
    return {'ok': all(c['ok'] for c in checks), 'checks': checks, 'checker_id': CHECKER_ID}


def _passable(tile, i, j, rover, bound):
    v = (tile['slope_high_milli_deg'] if bound == 'high' else tile['slope_low_milli_deg'])[i][j]
    if v is None:
        return bound == 'low'
    return v <= rover['max_slope_milli_deg']


def check_route(inputs, result):
    """Re-walk the path (passability at the upper bound, energy and length recomputed) or verify the closure witness: every
    neighbour of a reachable cell is reachable or blocked, and the goal is not reachable."""
    tile = terrain_mod.compute(inputs['terrain']); rover = inputs['rover']; checks = []
    start, goal = inputs['start'], inputs['goal']; L, S = tile['lines'], tile['samples']
    available = (rover['energy_budget_mwh'] - rover['reserve_mwh']) * 1000
    checks.append({'check': 'energy_available', 'ok': result['energy_available_uwh'] == available})
    if result.get('path'):
        p = [tuple(x) for x in result['path']]; ok_steps = p[0] == (start['line'], start['sample']) and p[-1] == (goal['line'], goal['sample']); e = 0; ln = 0; worst = 0
        for (i, j), (ni, nj) in zip(p, p[1:]):
            di, dj = ni - i, nj - j
            if max(abs(di), abs(dj)) != 1 or not (0 <= ni < L and 0 <= nj < S):
                ok_steps = False; break
            if not _passable(tile, ni, nj, rover, 'high'):
                ok_steps = False; break
            ns, ew = tile['row_spacing_mm'][i]; length = route_mod.step_length_mm(ns, ew, di, dj)
            e += route_mod.step_cost_uwh(length, tile['slope_high_milli_deg'][ni][nj], rover); ln += length; worst = max(worst, tile['slope_high_milli_deg'][ni][nj])
        checks.append({'check': 'path_steps_passable_at_upper_bound', 'ok': ok_steps})
        checks.append({'check': 'path_energy_and_length', 'ok': ok_steps and e == result.get('energy_uwh') and ln == result.get('path_length_mm'), 'detail': {'reference_energy_uwh': e, 'reference_length_mm': ln}})
        if result['outcome'] == 'FEASIBLE':
            checks.append({'check': 'feasible_within_budget_and_length', 'ok': ok_steps and e <= available and ln <= rover['max_path_length_m'] * 1000})
    if result.get('witness') == 'closure' and result['outcome'] in ('INFEASIBLE', 'INDETERMINATE'):
        key = 'reachable_optimistic' if result['outcome'] == 'INFEASIBLE' else 'reachable_conservative'; bound = 'low' if result['outcome'] == 'INFEASIBLE' else 'high'
        rows = result.get(key) or []
        closed = {(i, j) for i, r in enumerate(rows) for j, ch in enumerate(r) if ch == '1'}
        ok = (start['line'], start['sample']) in closed and (goal['line'], goal['sample']) not in closed and len(rows) == L and all(len(r) == S for r in rows)
        for (i, j) in closed:
            if not _passable(tile, i, j, rover, bound) and (i, j) != (start['line'], start['sample']):
                ok = False; break
            for di, dj, _ in route_mod.MOVES:
                ni, nj = i + di, j + dj
                if 0 <= ni < L and 0 <= nj < S and (ni, nj) not in closed and _passable(tile, ni, nj, rover, bound):
                    ok = False; break
            if not ok:
                break
        checks.append({'check': 'closure_witness_%s' % bound, 'ok': ok, 'detail': {'reachable_cells': len(closed)}})
        if result['outcome'] == 'INFEASIBLE' and result.get('reachable_conservative') is None:
            checks.append({'check': 'conservative_witness_present', 'ok': False})
    if result['outcome'] == 'INFEASIBLE' and result.get('witness') == 'min_energy_bound':
        checks.append({'check': 'min_energy_bound', 'ok': result.get('min_energy_optimistic_uwh', 0) > available})
    if result['outcome'] == 'INDETERMINATE' and result.get('witness') is None:
        checks.append({'check': 'limit_declared', 'ok': 'limit' in (result.get('reason') or '')})
    return {'ok': all(c['ok'] for c in checks), 'checks': checks, 'checker_id': CHECKER_ID}


def check_pack(inputs, result, sample_index=None):
    """Manifest digests recomputed from the inputs; one variation (sampled or all when small) re-walked by the route checker."""
    from . import scenario
    fresh = scenario.compute(inputs)['manifest']; m = result['manifest']; checks = []
    checks.append({'check': 'manifest_digests', 'ok': all(fresh[k] == m.get(k) for k in ('pack_id', 'inputs_digest', 'implementation_digest')) and [(v['terrain_result_digest'], v['route_result_digest']) for v in fresh['variations']] == [(v['terrain_result_digest'], v['route_result_digest']) for v in m.get('variations', [])]})
    idxs = range(len(inputs['variations'])) if sample_index is None else [sample_index % len(inputs['variations'])]
    for idx in idxs:
        v, tinp, rinp = scenario.variation_inputs(inputs, idx)
        rr = check_route(rinp, route_mod.compute(rinp)); tr = check_terrain(tinp, terrain_mod.compute(tinp))
        checks.append({'check': 'variation_%s_reference' % v['id'], 'ok': rr['ok'] and tr['ok'], 'detail': {'route_checks': len(rr['checks']), 'terrain_checks': len(tr['checks'])}})
    return {'ok': all(c['ok'] for c in checks), 'checks': checks, 'checker_id': CHECKER_ID}


def check(kind, inputs, result):
    return {'terrain_tile': check_terrain, 'route_feasibility': check_route, 'scenario_pack': check_pack}[kind](inputs, result)
