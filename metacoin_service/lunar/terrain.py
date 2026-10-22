"""Terrain tile product (lunar-terrain-tile/v1): elevation and slope on a simple-cylindrical DN grid with explicit treatment of
nodata, tile edges, projection scale (east-west spacing shrinks with cos(latitude)), units and vertical uncertainty.
All inputs are integers (strict JSON); slopes are reported in milli-degrees with lower/upper bounds."""
import base64
import hashlib
import json
import math

from experiments.private_receipts.receipt import Invalid, canonical
from . import png

INPUT_SCHEMA = 'lunar-terrain-input/v1'
RESULT_SCHEMA = 'lunar-terrain-tile/v1'
MODEL_ID = 'lunar-terrain-tile/v1'
OUTCOMES = ('TILE_COMPLETE', 'TILE_PARTIAL')
MAX_CELLS = 64 * 64              # evidence fields are bounded (64 KiB each); 64x64 keeps every grid well inside
FLAG_OK, FLAG_EDGE, FLAG_NODATA = 0, 1, 2
ASSUMPTIONS = ['slope is the grid-scale gradient of a resampled DEM (pixel spacing as declared); features smaller than one pixel (rocks, small craters, scarps) are invisible',
               'simple cylindrical grid: north-south spacing is constant, east-west spacing is pixel_spacing * cos(latitude) per row (computed per row; polar rows are refused)',
               'heights are DN * scaling relative to the reference sphere; no geoid correction (slopes are unaffected by a constant offset)',
               'vertical error is a declared parameter applied as an independent bound on every neighbour (not a measured error model)',
               'edge cells use one-sided differences and are flagged; cells touching nodata have no slope (flagged nodata)',
               'this is not a traversability, traction, illumination or communication product']
DEFAULT_PARAMETERS = {'slope_threshold_milli_deg': 15000, 'vertical_error_mm': 1000, 'edge_policy': 'one_sided_flagged', 'nodata_policy': 'propagate_to_neighbours'}


def _int(v, lo, hi, name):
    if type(v) is not int or isinstance(v, bool) or not lo <= v <= hi:
        raise Invalid('%s: integer in [%d, %d] required' % (name, lo, hi))
    return v


def validate(data):
    canonical(data)
    if type(data) is not dict or data.get('schema') != INPUT_SCHEMA:
        raise Invalid('schema: ' + INPUT_SCHEMA)
    if set(data) - {'schema', 'source', 'parameters', 'units', 'private_label'}:
        raise Invalid('unknown terrain input field')
    src = data.get('source')
    if type(src) is not dict or src.get('schema') != 'lunar-terrain-source/v1' or src.get('kind') not in ('ldem_tile', 'synthetic'):
        raise Invalid('source: lunar-terrain-source/v1 ldem_tile or synthetic')
    lines = _int(src.get('lines'), 3, 64, 'lines'); samples = _int(src.get('samples'), 3, 64, 'samples')
    if lines * samples > MAX_CELLS:
        raise Invalid('tile too large')
    dn = src.get('dn')
    if type(dn) is not list or len(dn) != lines or any(type(r) is not list or len(r) != samples for r in dn):
        raise Invalid('dn: lines x samples integer grid')
    nodata = src.get('nodata_dn')
    if nodata is not None:
        _int(nodata, -32768, 32767, 'nodata_dn')
    for r in dn:
        for v in r:
            _int(v, -32768, 32767, 'dn value')
    _int(src.get('pixel_spacing_mm'), 1000, 100_000_000, 'pixel_spacing_mm')
    _int(src['units'].get('dn_scaling_factor_milli') if type(src.get('units')) is dict else None, 1, 1_000_000, 'dn_scaling_factor_milli')
    lat = _int(src.get('first_line_latitude_milli_deg'), -90000, 90000, 'first_line_latitude_milli_deg')
    _int(src.get('map_resolution_px_per_deg'), 1, 1024, 'map_resolution_px_per_deg')
    if src.get('projection') != 'simple_cylindrical':
        raise Invalid('projection: simple_cylindrical only')
    last_lat = lat - (lines - 1) * 1000 // src['map_resolution_px_per_deg']
    if lat >= 89500 or last_lat <= -89500:
        raise Invalid('tile reaches a pole: east-west scale degenerates in simple cylindrical projection')
    if src['kind'] == 'ldem_tile':
        for f in ('product_id', 'data_set_id', 'image_sha256', 'label_sha256', 'source_url', 'acquired_at', 'origin_line', 'origin_sample'):
            if src.get(f) in (None, ''):
                raise Invalid('ldem_tile provenance field missing: ' + f)
    if type(src.get('label')) is not str or (src['kind'] == 'synthetic' and 'SYNTHETIC' not in src['label']):
        raise Invalid('label: synthetic terrain must say SYNTHETIC')
    par = data.get('parameters', DEFAULT_PARAMETERS)
    if type(par) is not dict or set(par) != set(DEFAULT_PARAMETERS):
        raise Invalid('parameters: ' + ', '.join(sorted(DEFAULT_PARAMETERS)))
    _int(par['slope_threshold_milli_deg'], 0, 90000, 'slope_threshold_milli_deg'); _int(par['vertical_error_mm'], 0, 100_000_000, 'vertical_error_mm')
    if par['edge_policy'] != 'one_sided_flagged' or par['nodata_policy'] != 'propagate_to_neighbours':
        raise Invalid('edge_policy / nodata_policy: only the documented policies are implemented')
    if data.get('units', {'height': 'meter', 'slope': 'milli-degree', 'spacing': 'millimeter'}) != {'height': 'meter', 'slope': 'milli-degree', 'spacing': 'millimeter'}:
        raise Invalid('units: height meter, slope milli-degree, spacing millimeter')
    return data


def row_spacing(src, i):
    """(north-south spacing mm, east-west spacing mm) of row i: east-west shrinks with cos(latitude)."""
    lat_mdeg = src['first_line_latitude_milli_deg'] - i * 1000 // src['map_resolution_px_per_deg']
    ew = int(round(src['pixel_spacing_mm'] * math.cos(math.radians(lat_mdeg / 1000.0))))
    return src['pixel_spacing_mm'], max(ew, 1)


def _slope_mdeg(gx, gy):
    return int(round(math.degrees(math.atan(math.hypot(gx, gy))) * 1000))


def compute(data):
    validate(data)
    src = data['source']; par = data.get('parameters', DEFAULT_PARAMETERS)
    L, S, dn, nodata = src['lines'], src['samples'], src['dn'], src.get('nodata_dn')
    scale = src['units']['dn_scaling_factor_milli']              # mm per DN
    sigma = par['vertical_error_mm']
    h = [[None if (nodata is not None and v == nodata) else v * scale for v in r] for r in dn]
    slope, lo, hi, flags = [], [], [], []
    stats = {'nodata_cells': 0, 'edge_cells': 0, 'cells_over_threshold': 0, 'cells_uncertain_at_threshold': 0, 'max_slope_milli_deg': 0, 'min_height_mm': None, 'max_height_mm': None}
    thr = par['slope_threshold_milli_deg']
    for i in range(L):
        ns, ew = row_spacing(src, i)
        rs, rl, rh, rf = [], [], [], []
        for j in range(S):
            if h[i][j] is None:
                rs.append(None); rl.append(None); rh.append(None); rf.append(FLAG_NODATA); stats['nodata_cells'] += 1; continue
            stats['min_height_mm'] = h[i][j] if stats['min_height_mm'] is None else min(stats['min_height_mm'], h[i][j])
            stats['max_height_mm'] = h[i][j] if stats['max_height_mm'] is None else max(stats['max_height_mm'], h[i][j])
            # east-west gradient
            edge = False
            if 0 < j < S - 1:
                a, b = h[i][j - 1], h[i][j + 1]; dx = 2 * ew; ex = 2 * sigma / dx if dx else 0
            else:
                a, b = (h[i][j], h[i][j + 1]) if j == 0 else (h[i][j - 1], h[i][j]); dx = ew; ex = 2 * sigma / dx; edge = True
            if 0 < i < L - 1:
                c, d = h[i + 1][j], h[i - 1][j]; dy = 2 * ns; ey = 2 * sigma / dy
            else:
                c, d = (h[i][j], h[i - 1][j]) if i == L - 1 else (h[i + 1][j], h[i][j]); dy = ns; ey = 2 * sigma / dy; edge = True
            if None in (a, b, c, d):
                rs.append(None); rl.append(None); rh.append(None); rf.append(FLAG_NODATA); stats['nodata_cells'] += 1; continue
            gx = (b - a) / dx; gy = (d - c) / dy
            s = _slope_mdeg(gx, gy); s_hi = _slope_mdeg(abs(gx) + ex, abs(gy) + ey); s_lo = _slope_mdeg(max(abs(gx) - ex, 0), max(abs(gy) - ey, 0))
            rs.append(s); rl.append(s_lo); rh.append(s_hi); rf.append(FLAG_EDGE if edge else FLAG_OK)
            stats['edge_cells'] += edge; stats['max_slope_milli_deg'] = max(stats['max_slope_milli_deg'], s)
            if s > thr:
                stats['cells_over_threshold'] += 1
            if s_lo <= thr < s_hi:
                stats['cells_uncertain_at_threshold'] += 1
        slope.append(rs); lo.append(rl); hi.append(rh); flags.append(rf)
    outcome = 'TILE_PARTIAL' if stats['nodata_cells'] else 'TILE_COMPLETE'
    prov = {k: src.get(k) for k in ('kind', 'label', 'product_id', 'data_set_id', 'product_version', 'source_url', 'label_url', 'image_sha256', 'label_sha256', 'acquired_at', 'acquired_by', 'projection', 'datum', 'units', 'sample_type', 'orientation', 'origin_line', 'origin_sample', 'first_line_latitude_milli_deg', 'first_sample_longitude_milli_deg', 'redistribution', 'region', 'what_this_is_not')}
    prov['dn_sha256'] = hashlib.sha256(json.dumps(dn, separators=(',', ':')).encode()).hexdigest()
    preview = preview_png(slope, flags, thr)
    return {'result_schema': RESULT_SCHEMA, 'model_id': MODEL_ID, 'outcome': outcome, 'lines': L, 'samples': S, 'height_mm': h, 'slope_milli_deg': slope, 'slope_low_milli_deg': lo, 'slope_high_milli_deg': hi, 'flags': flags,
            'flag_legend': {'0': 'interior central difference', '1': 'tile edge: one-sided difference (wider bounds)', '2': 'nodata or nodata neighbour: no slope'}, 'row_spacing_mm': [list(row_spacing(src, i)) for i in range(L)],
            'parameters': par, 'stats': stats, 'uncertainty': {'vertical_error_mm': sigma, 'method': 'independent +/- vertical error on each neighbour propagated to gradient bounds; slope_low/high are the resulting bounds', 'threshold_milli_deg': thr},
            'assumptions': ASSUMPTIONS, 'provenance': prov, 'preview_png_base64': base64.b64encode(preview).decode(), 'preview_legend': 'grayscale = slope from 0 (black) to the declared threshold (white, clipped above); magenta = nodata',
            'scope': 'grid-scale elevation and slope of the declared tile under the stated assumptions; not physical traversability'}


def preview_png(slope, flags, thr):
    top = max(thr, 1)                                                     # white = at or above the declared slope threshold
    rows = [[None if f == 2 else min(255, (s * 255) // top) for s, f in zip(rs, rf)] for rs, rf in zip(slope, flags)]
    return png.gray_png(rows, scale=4 if len(rows) <= 64 else 2)


def summary(result):
    return {'result_schema': RESULT_SCHEMA, 'model_id': MODEL_ID, 'outcome': result['outcome'], 'lines': result['lines'], 'samples': result['samples'], 'stats': result['stats'], 'source_kind': result['provenance']['kind'], 'source_label': result['provenance']['label'], 'product_id': result['provenance']['product_id'], 'dn_sha256': result['provenance']['dn_sha256']}
