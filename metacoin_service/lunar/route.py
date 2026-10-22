"""Route feasibility product (lunar-route-feasibility/v1): bounded exact search on a terrain tile under a declared rover model.
FEASIBLE needs a path whose every cell is passable even at the slope upper bound and whose upper-bound energy fits the budget
minus the protected reserve; INFEASIBLE needs the exhaustive search to fail even under the optimistic bounds (nodata treated
as flat); anything in between (goal reachable only through uncertain or missing cells, or a search limit) is INDETERMINATE.
The result carries a checkable witness: the path, or the closed reachable set."""
import heapq

from experiments.private_receipts.receipt import Invalid, canonical
from . import terrain

INPUT_SCHEMA = 'lunar-route-input/v1'
RESULT_SCHEMA = 'lunar-route-result/v1'
MODEL_ID = 'lunar-route-feasibility/v1'
OUTCOMES = ('FEASIBLE', 'INFEASIBLE', 'INDETERMINATE')
ROVER_FIELDS = {'max_slope_milli_deg', 'energy_budget_mwh', 'reserve_mwh', 'flat_cost_uwh_per_m', 'slope_cost_uwh_per_m_per_deg', 'max_path_length_m'}
MOVES = [(-1, 0, 'n'), (1, 0, 's'), (0, -1, 'w'), (0, 1, 'e'), (-1, -1, 'nw'), (-1, 1, 'ne'), (1, -1, 'sw'), (1, 1, 'se')]
ASSUMPTIONS = ['energy per step = length * (flat cost + slope cost * slope of the entered cell); no slope sign (uphill and downhill cost alike), no battery dynamics, no thermal or illumination terms',
               'passability is the slope of the entered cell against the rover limit; the grid-scale slope is a proxy (sub-pixel hazards are not modelled)',
               'eight-neighbour moves; diagonal length = sqrt(ns^2 + ew^2) per row; the search minimizes energy, then checks length (a shorter-but-costlier route is not explored: a length violation is INDETERMINATE, not INFEASIBLE)',
               'INFEASIBLE is exact for the declared model and tile only (nothing outside the tile is considered)',
               'safety-critical control must not depend on this product or on any payment response']


def _int(v, lo, hi, name):
    if type(v) is not int or isinstance(v, bool) or not lo <= v <= hi:
        raise Invalid('%s: integer in [%d, %d] required' % (name, lo, hi))
    return v


def validate(data):
    canonical(data)
    if type(data) is not dict or data.get('schema') != INPUT_SCHEMA or set(data) - {'schema', 'terrain', 'start', 'goal', 'rover', 'search', 'private_label'}:
        raise Invalid('schema: ' + INPUT_SCHEMA + ' with terrain, start, goal, rover, search')
    terrain.validate(data.get('terrain'))
    L, S = data['terrain']['source']['lines'], data['terrain']['source']['samples']
    for k in ('start', 'goal'):
        p = data.get(k)
        if type(p) is not dict or set(p) != {'line', 'sample'}:
            raise Invalid(k + ': {line, sample}')
        _int(p['line'], 0, L - 1, k + '.line'); _int(p['sample'], 0, S - 1, k + '.sample')
    rv = data.get('rover')
    if type(rv) is not dict or set(rv) != ROVER_FIELDS:
        raise Invalid('rover: ' + ', '.join(sorted(ROVER_FIELDS)))
    _int(rv['max_slope_milli_deg'], 0, 90000, 'max_slope_milli_deg'); _int(rv['energy_budget_mwh'], 0, 10 ** 12, 'energy_budget_mwh'); _int(rv['reserve_mwh'], 0, 10 ** 12, 'reserve_mwh')
    _int(rv['flat_cost_uwh_per_m'], 0, 10 ** 9, 'flat_cost_uwh_per_m'); _int(rv['slope_cost_uwh_per_m_per_deg'], 0, 10 ** 9, 'slope_cost_uwh_per_m_per_deg'); _int(rv['max_path_length_m'], 1, 10 ** 9, 'max_path_length_m')
    if rv['reserve_mwh'] > rv['energy_budget_mwh']:
        raise Invalid('reserve exceeds budget')
    se = data.get('search')
    if type(se) is not dict or set(se) != {'max_expansions'}:
        raise Invalid('search: {max_expansions}')
    _int(se['max_expansions'], 1, 200000, 'max_expansions')
    return data


def step_length_mm(ns, ew, di, dj):
    if di and dj:
        return int(round((ns * ns + ew * ew) ** 0.5))
    return ns if di else ew


def step_cost_uwh(length_mm, slope_mdeg, rover):
    # micro-Wh: length(m) * (flat + slope_cost * deg) with integer arithmetic in milli-units
    return (length_mm * (rover['flat_cost_uwh_per_m'] * 1000 + rover['slope_cost_uwh_per_m_per_deg'] * slope_mdeg)) // 1_000_000


def _search(tile, start, goal, rover, max_exp, bound):
    """Dijkstra on energy. bound = 'high' (conservative: slope upper bounds, nodata impassable) or 'low' (optimistic: slope lower bounds, nodata flat)."""
    L, S = tile['lines'], tile['samples']
    sl = tile['slope_high_milli_deg'] if bound == 'high' else tile['slope_low_milli_deg']
    def cell_slope(i, j):
        v = sl[i][j]
        return (None if bound == 'high' else 0) if v is None else v
    # the rover already stands on the start cell: its slope is not a passability test (the checker applies the same exemption)
    dist = {(start['line'], start['sample']): (0, 0)}; prev = {}; closed = set(); heap = [(0, 0, start['line'], start['sample'])]; exp = 0; limit_hit = False
    while heap:
        e, ln, i, j = heapq.heappop(heap)
        if (i, j) in closed:
            continue
        closed.add((i, j)); exp += 1
        if (i, j) == (goal['line'], goal['sample']):
            break
        if exp > max_exp:
            limit_hit = True; break
        ns, ew = tile['row_spacing_mm'][i]
        for di, dj, _ in MOVES:
            ni, nj = i + di, j + dj
            if not (0 <= ni < L and 0 <= nj < S) or (ni, nj) in closed:
                continue
            s = cell_slope(ni, nj)
            if s is None or s > rover['max_slope_milli_deg']:
                continue
            length = step_length_mm(ns, ew, di, dj); ne = e + step_cost_uwh(length, s, rover); nl = ln + length
            if (ni, nj) not in dist or ne < dist[(ni, nj)][0]:
                dist[(ni, nj)] = (ne, nl); prev[(ni, nj)] = (i, j); heapq.heappush(heap, (ne, nl, ni, nj))
    g = (goal['line'], goal['sample'])
    return {'reached': g in closed, 'expansions': exp, 'dist': dist, 'prev': prev, 'closed': closed, 'limit_hit': limit_hit, 'reason': None}


def _path(prev, start, goal):
    p = [(goal['line'], goal['sample'])]
    while p[-1] != (start['line'], start['sample']):
        p.append(prev[p[-1]])
    return [list(x) for x in reversed(p)]


def _closure_witness(tile, closed, rover, bound):
    """Rows of '0'/'1' marking the closed reachable set; the checker verifies every neighbour of a reachable cell is reachable or blocked."""
    L, S = tile['lines'], tile['samples']
    return [''.join('1' if (i, j) in closed else '0' for j in range(S)) for i in range(L)]


def compute(data):
    validate(data)
    tile = terrain.compute(data['terrain'])
    start, goal, rover, max_exp = data['start'], data['goal'], data['rover'], data['search']['max_expansions']
    available = (rover['energy_budget_mwh'] - rover['reserve_mwh']) * 1000           # micro-Wh usable above the protected reserve
    base = {'result_schema': RESULT_SCHEMA, 'model_id': MODEL_ID, 'start': start, 'goal': goal, 'rover': rover, 'search': data['search'], 'energy_available_uwh': available, 'terrain_outcome': tile['outcome'], 'terrain_stats': tile['stats'],
            'terrain_provenance': {k: tile['provenance'][k] for k in ('kind', 'label', 'product_id', 'dn_sha256')}, 'assumptions': ASSUMPTIONS, 'scope': 'exact bounded search on the declared tile and rover model; grid-scale slopes; not physical traversability'}
    cons = _search(tile, start, goal, rover, max_exp, 'high')
    if cons['reached']:
        e, ln = cons['dist'][(goal['line'], goal['sample'])]
        path = _path(cons['prev'], start, goal)
        worst = max(tile['slope_high_milli_deg'][i][j] for i, j in path)
        if e <= available and ln <= rover['max_path_length_m'] * 1000:
            return dict(base, outcome='FEASIBLE', path=path, path_length_mm=ln, energy_uwh=e, worst_slope_high_milli_deg=worst, expansions=cons['expansions'], reason='a path exists whose every cell is passable at the slope upper bound and whose upper-bound energy fits above the reserve', witness='path')
        if e > available:
            # min-energy path exceeds the budget: also impossible optimistically? check the optimistic search before calling it infeasible
            opt = _search(tile, start, goal, rover, max_exp, 'low')
            if opt['reached'] and opt['dist'][(goal['line'], goal['sample'])][0] > available:
                return dict(base, outcome='INFEASIBLE', path=path, path_length_mm=ln, energy_uwh=e, min_energy_optimistic_uwh=opt['dist'][(goal['line'], goal['sample'])][0], expansions=cons['expansions'] + opt['expansions'], reason='even the cheapest route under optimistic slopes needs more energy than the budget above the reserve', witness='min_energy_bound')
            return dict(base, outcome='INDETERMINATE', path=path, path_length_mm=ln, energy_uwh=e, expansions=cons['expansions'] + opt['expansions'], reason='the conservative route exceeds the energy budget; an optimistic route may fit: the uncertainty on slopes decides', witness='path', optimistic_min_energy_uwh=opt['dist'].get((goal['line'], goal['sample']), (None,))[0])
        return dict(base, outcome='INDETERMINATE', path=path, path_length_mm=ln, energy_uwh=e, expansions=cons['expansions'], reason='the minimum-energy route is longer than max_path_length; a shorter feasible route may exist (not searched)', witness='path')
    if cons['limit_hit']:
        return dict(base, outcome='INDETERMINATE', expansions=cons['expansions'], reason='search limit reached before the reachable set was closed', witness=None)
    opt = _search(tile, start, goal, rover, max_exp, 'low')
    if opt['reached'] and not opt['limit_hit']:
        return dict(base, outcome='INDETERMINATE', expansions=cons['expansions'] + opt['expansions'], reason='the goal is reachable only through cells whose slope bounds straddle the limit or whose data are missing', witness='closure', reachable_conservative=_closure_witness(tile, cons['closed'], rover, 'high'),
                    uncertain_frontier=sum(1 for (i, j) in opt['closed'] if (i, j) not in cons['closed']), optimistic_energy_uwh=opt['dist'][(goal['line'], goal['sample'])][0])
    if opt['limit_hit']:
        return dict(base, outcome='INDETERMINATE', expansions=cons['expansions'] + opt['expansions'], reason='optimistic search limit reached', witness=None)
    return dict(base, outcome='INFEASIBLE', expansions=cons['expansions'] + opt['expansions'], reason='no path reaches the goal even when every uncertain cell is taken at its slope lower bound and missing data are assumed flat (exhaustive on the tile)', witness='closure',
                reachable_optimistic=_closure_witness(tile, opt['closed'], rover, 'low'), reachable_conservative=_closure_witness(tile, cons['closed'], rover, 'high'))


def summary(result):
    return {k: result.get(k) for k in ('result_schema', 'model_id', 'outcome', 'reason', 'witness', 'path_length_mm', 'energy_uwh', 'energy_available_uwh', 'worst_slope_high_milli_deg', 'expansions', 'terrain_outcome', 'terrain_provenance')}
