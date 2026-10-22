"""Reproducible scenario pack (lunar-scenario-pack/v1): a base terrain tile, a route question and seeded variations
(vertical noise, slope-limit and budget deltas) run through the two products; the manifest pins inputs, seeds, exact
operation identities (model ids + implementation digest), outputs and their digests, and the reproduction command.
`pack_files()` renders the downloadable pack; `reproduce()` recomputes it from the frozen inputs alone."""
import hashlib
import json

from experiments.private_receipts.receipt import Invalid, canonical
from . import route, terrain

INPUT_SCHEMA = 'lunar-scenario-input/v1'
RESULT_SCHEMA = 'lunar-scenario-pack/v1'
MODEL_ID = 'lunar-scenario-pack/v1'
OUTCOMES = ('PACK_COMPLETE',)
MAX_VARIATIONS = 16
MAX_CELLS = 64 * 64
REPRODUCE = 'PYTHONPATH=. python3 -m metacoin_service.lunar reproduce <pack-dir>/inputs.json --out <dir>   # compares every digest with manifest.json'
VARIATION_FIELDS = {'id', 'vertical_noise_mm', 'slope_limit_delta_milli_deg', 'energy_budget_delta_mwh'}


def splitmix(seed):
    """SplitMix64 as an infinite generator of 64-bit words: implementation-independent determinism (no library PRNG)."""
    x = seed & 0xFFFFFFFFFFFFFFFF
    while True:
        x = (x + 0x9E3779B97F4A7C15) & 0xFFFFFFFFFFFFFFFF
        z = x
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & 0xFFFFFFFFFFFFFFFF
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & 0xFFFFFFFFFFFFFFFF
        yield z ^ (z >> 31)


def _int(v, lo, hi, name):
    if type(v) is not int or isinstance(v, bool) or not lo <= v <= hi:
        raise Invalid('%s: integer in [%d, %d] required' % (name, lo, hi))
    return v


def validate(data):
    canonical(data)
    if type(data) is not dict or data.get('schema') != INPUT_SCHEMA or set(data) - {'schema', 'base_terrain', 'route', 'seed', 'variations', 'private_label'}:
        raise Invalid('schema: ' + INPUT_SCHEMA + ' with base_terrain, route, seed, variations')
    terrain.validate(data.get('base_terrain'))
    src = data['base_terrain']['source']
    if src['lines'] * src['samples'] > MAX_CELLS:
        raise Invalid('scenario pack tiles are limited to %d cells' % MAX_CELLS)
    rt = data.get('route')
    if type(rt) is not dict or set(rt) != {'start', 'goal', 'rover', 'search'}:
        raise Invalid('route: {start, goal, rover, search}')
    route.validate({'schema': route.INPUT_SCHEMA, 'terrain': data['base_terrain'], **rt})
    _int(data.get('seed'), 0, (1 << 63) - 1, 'seed')
    vs = data.get('variations')
    if type(vs) is not list or not 1 <= len(vs) <= MAX_VARIATIONS:
        raise Invalid('variations: 1..%d' % MAX_VARIATIONS)
    ids = set()
    for v in vs:
        if type(v) is not dict or set(v) != VARIATION_FIELDS or type(v['id']) is not str or not 1 <= len(v['id']) <= 32 or not v['id'].replace('-', '').replace('_', '').isalnum():
            raise Invalid('variation: ' + ', '.join(sorted(VARIATION_FIELDS)))
        if v['id'] in ids:
            raise Invalid('duplicate variation id')
        ids.add(v['id'])
        _int(v['vertical_noise_mm'], 0, 100_000_000, 'vertical_noise_mm'); _int(v['slope_limit_delta_milli_deg'], -90000, 90000, 'slope_limit_delta_milli_deg'); _int(v['energy_budget_delta_mwh'], -10 ** 12, 10 ** 12, 'energy_budget_delta_mwh')
    return data


def _digest(obj):
    return hashlib.sha256(canonical(obj)).hexdigest()


def variation_inputs(data, index):
    """The exact terrain + route inputs of variation `index` (deterministic from seed and index)."""
    v = data['variations'][index]; src = data['base_terrain']['source']; scale = src['units']['dn_scaling_factor_milli']
    rng = splitmix((data['seed'] * 1_000_003 + index) & 0xFFFFFFFFFFFFFFFF)
    noise_dn = v['vertical_noise_mm'] // scale
    dn = [[max(-32768, min(32767, x + ((next(rng) % (2 * noise_dn + 1)) - noise_dn))) if noise_dn else x for x in r] for r in src['dn']]
    tsrc = dict(src, dn=dn, label=src['label'] + (' + scenario noise %d mm (seed %d/%d)' % (v['vertical_noise_mm'], data['seed'], index) if noise_dn else ''))
    tinp = dict(data['base_terrain'], source=tsrc)
    rover = dict(data['route']['rover']); rover['max_slope_milli_deg'] = max(0, min(90000, rover['max_slope_milli_deg'] + v['slope_limit_delta_milli_deg'])); rover['energy_budget_mwh'] = max(rover['reserve_mwh'], rover['energy_budget_mwh'] + v['energy_budget_delta_mwh'])
    rinp = {'schema': route.INPUT_SCHEMA, 'terrain': tinp, 'start': data['route']['start'], 'goal': data['route']['goal'], 'rover': rover, 'search': data['route']['search']}
    return v, tinp, rinp


def compute(data, with_files=False):
    validate(data)
    from .operations import implementation_digest
    variations, files = [], {}
    for idx in range(len(data['variations'])):
        v, tinp, rinp = variation_inputs(data, idx)
        tile = terrain.compute(tinp); rt = route.compute(rinp)
        tile_wo_preview = {k: val for k, val in tile.items() if k != 'preview_png_base64'}
        rec = {'id': v['id'], 'index': idx, 'variation': v, 'terrain_input_digest': _digest(tinp), 'route_input_digest': _digest(rinp), 'terrain_result_digest': _digest(tile_wo_preview), 'route_result_digest': _digest(rt),
               'reference_outputs': {'terrain_outcome': tile['outcome'], 'max_slope_milli_deg': tile['stats']['max_slope_milli_deg'], 'cells_over_threshold': tile['stats']['cells_over_threshold'], 'route_outcome': rt['outcome'], 'path_length_mm': rt.get('path_length_mm'), 'energy_uwh': rt.get('energy_uwh'), 'expansions': rt['expansions'], 'reason': rt['reason']}}
        variations.append(rec)
        if with_files:
            files['variations/%s/terrain.json' % v['id']] = canonical(tile_wo_preview); files['variations/%s/route.json' % v['id']] = canonical(rt); files['variations/%s/preview.png' % v['id']] = __import__('base64').b64decode(tile['preview_png_base64'])
            files['variations/%s/inputs.json' % v['id']] = canonical({'terrain': tinp, 'route': rinp})
    ops = [{'operation': 'terrain_tile', 'model_id': terrain.MODEL_ID, 'result_schema': terrain.RESULT_SCHEMA}, {'operation': 'route_feasibility', 'model_id': route.MODEL_ID, 'result_schema': route.RESULT_SCHEMA}, {'operation': 'scenario_pack', 'model_id': MODEL_ID, 'result_schema': RESULT_SCHEMA}]
    manifest = {'schema': RESULT_SCHEMA, 'pack_id': _digest({'inputs': data, 'ops': ops}), 'inputs_digest': _digest(data), 'seed': data['seed'], 'prng': 'SplitMix64 keyed by seed*1000003+index; noise = uniform integer in [-n, n] DN per cell, row-major',
                'operations': ops, 'implementation_digest': implementation_digest(), 'base_terrain_provenance': {k: data['base_terrain']['source'].get(k) for k in ('kind', 'label', 'product_id', 'data_set_id', 'image_sha256', 'label_sha256', 'source_url', 'acquired_at')},
                'variations': variations, 'summary': {'count': len(variations), 'route_outcomes': {o: sum(1 for x in variations if x['reference_outputs']['route_outcome'] == o) for o in route.OUTCOMES}},
                'reproduction': REPRODUCE, 'assumptions': {'terrain': terrain.ASSUMPTIONS, 'route': route.ASSUMPTIONS}, 'scope': 'declared model only; variations perturb the declared inputs, they do not sample physical uncertainty'}
    result = {'result_schema': RESULT_SCHEMA, 'model_id': MODEL_ID, 'outcome': 'PACK_COMPLETE', 'manifest': manifest}
    if with_files:
        files['manifest.json'] = canonical(manifest); files['inputs.json'] = canonical(data); files['README.txt'] = README.encode()
        return result, files
    return result


README = """MetaCoin lunar scenario pack (lunar-scenario-pack/v1)
manifest.json      pinned inputs digest, seed/PRNG, exact operations (model ids + implementation digest), per-variation input/result digests and reference outputs
inputs.json        the frozen pack inputs (base terrain source with provenance, route question, seed, variations)
variations/<id>/   inputs.json (exact terrain + route inputs of that variation), terrain.json, route.json, preview.png
Reproduce: %s
A coarse DEM does not establish rock avoidance, traction or safe physical navigation; read manifest.json assumptions.
""" % REPRODUCE


def pack_zip(data):
    import io
    import zipfile
    result, files = compute(data, with_files=True)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        for name in sorted(files):
            info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0)); info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, files[name])
    return result, buf.getvalue()


def reproduce(inputs, manifest):
    """Recompute from inputs alone and compare every digest with a manifest; returns the comparison (no network, no database)."""
    fresh = compute(inputs)['manifest']
    checks = [{'check': 'inputs_digest', 'ok': fresh['inputs_digest'] == manifest.get('inputs_digest')}, {'check': 'pack_id', 'ok': fresh['pack_id'] == manifest.get('pack_id')}, {'check': 'implementation_digest', 'ok': fresh['implementation_digest'] == manifest.get('implementation_digest')}]
    mv = {v['id']: v for v in manifest.get('variations', [])}
    for v in fresh['variations']:
        m = mv.get(v['id']) or {}
        for k in ('terrain_input_digest', 'route_input_digest', 'terrain_result_digest', 'route_result_digest', 'reference_outputs'):
            checks.append({'check': '%s.%s' % (v['id'], k), 'ok': v[k] == m.get(k)})
    return {'ok': all(c['ok'] for c in checks), 'checks': checks, 'variations': len(fresh['variations'])}


def summary(result):
    m = result['manifest']
    return {'result_schema': RESULT_SCHEMA, 'model_id': MODEL_ID, 'outcome': result['outcome'], 'pack_id': m['pack_id'], 'inputs_digest': m['inputs_digest'], 'seed': m['seed'], 'variations': m['summary']['count'], 'route_outcomes': m['summary']['route_outcomes'], 'implementation_digest': m['implementation_digest'], 'base_terrain': m['base_terrain_provenance']}
