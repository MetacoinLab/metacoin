"""Kind registry glue for the three lunar products (validators, model ids, implementation digest, run functions)."""
import hashlib
from pathlib import Path

from . import route, scenario, terrain

KINDS = ('terrain_tile', 'route_feasibility', 'scenario_pack')
VALIDATORS = {'terrain_tile': terrain.validate, 'route_feasibility': route.validate, 'scenario_pack': scenario.validate}
MODEL_IDS = {'terrain_tile': terrain.MODEL_ID, 'route_feasibility': route.MODEL_ID, 'scenario_pack': scenario.MODEL_ID}
OUTCOMES = {'terrain_tile': terrain.OUTCOMES, 'route_feasibility': route.OUTCOMES, 'scenario_pack': scenario.OUTCOMES}
VERIFIER_ID = 'lunar-products/v1'
FILES = ('terrain.py', 'route.py', 'scenario.py', 'png.py')


def implementation_digest():
    h = hashlib.sha256(b'metacoin/lunar-products/v1\0'); here = Path(__file__).parent
    for name in FILES:
        h.update(name.encode() + b'\0' + (here / name).read_bytes() + b'\0')
    return h.hexdigest()


def reference_digest():
    return hashlib.sha256(b'metacoin/lunar-reference/v1\0' + (Path(__file__).parent / 'reference.py').read_bytes()).hexdigest()


def run(kind, inputs):
    """Returns (result, outcome, summary)."""
    if kind == 'terrain_tile':
        r = terrain.compute(inputs); return r, r['outcome'], terrain.summary(r)
    if kind == 'route_feasibility':
        r = route.compute(inputs); return r, r['outcome'], route.summary(r)
    if kind == 'scenario_pack':
        r = scenario.compute(inputs); return r, r['outcome'], scenario.summary(r)
    raise KeyError(kind)


GRID_KEYS = ('height_mm', 'slope_milli_deg', 'slope_low_milli_deg', 'slope_high_milli_deg', 'flags', 'row_spacing_mm', 'reachable_optimistic', 'reachable_conservative', 'path', 'preview_png_base64')


def evidence_fields(result):
    """Split a product result into evidence-vault fields under the per-field limit: the large grids travel as separate
    `grid_<key>` fields; `result` keeps everything else. `reassemble` is the inverse."""
    core = {k: v for k, v in result.items() if k not in GRID_KEYS}
    out = {'result': core, 'grid_keys': [k for k in GRID_KEYS if k in result]}
    for k in out['grid_keys']:
        out['grid_' + k] = result[k]
    return out


def reassemble(values):
    """values: the evidence vault's {name: value} map (as full_values returns it)."""
    res = dict(values['result'])
    for k in values.get('grid_keys', []):
        if 'grid_' + k in values:
            res[k] = values['grid_' + k]
    return res
