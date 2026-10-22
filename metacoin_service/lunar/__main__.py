"""Command line for the lunar products (no service needed):
  python -m metacoin_service.lunar sources                       list bundled terrain sources with provenance
  python -m metacoin_service.lunar terrain <inputs.json>         compute a terrain tile (JSON result on stdout, --png <file>)
  python -m metacoin_service.lunar route <inputs.json>           route feasibility
  python -m metacoin_service.lunar pack <inputs.json> --out DIR  write a scenario pack directory (+ pack.zip)
  python -m metacoin_service.lunar reproduce <inputs.json> --manifest manifest.json   recompute and compare digests
  python -m metacoin_service.lunar check <kind> <inputs.json> <result.json>           independent reference check
  python -m metacoin_service.lunar example <name>                print example inputs (terrain|route|pack) for a bundled source"""
import argparse
import base64
import json
import sys
from pathlib import Path


def example(name, source='apollo11_32'):
    from . import sources, route, scenario, terrain
    src = sources.bundled(source)
    t = {'schema': terrain.INPUT_SCHEMA, 'source': src, 'parameters': dict(terrain.DEFAULT_PARAMETERS), 'units': {'height': 'meter', 'slope': 'milli-degree', 'spacing': 'millimeter'}}
    if name == 'terrain':
        return t
    rt = {'start': {'line': 2, 'sample': 2}, 'goal': {'line': src['lines'] - 3, 'sample': src['samples'] - 3}, 'rover': {'max_slope_milli_deg': 15000, 'energy_budget_mwh': 70_000_000, 'reserve_mwh': 10_000_000, 'flat_cost_uwh_per_m': 150_000, 'slope_cost_uwh_per_m_per_deg': 40_000, 'max_path_length_m': 2_000_000}, 'search': {'max_expansions': 20000}}
    if name == 'route':
        return {'schema': route.INPUT_SCHEMA, 'terrain': t, **rt}
    if name == 'pack':
        return {'schema': scenario.INPUT_SCHEMA, 'base_terrain': t, 'route': rt, 'seed': 20261022, 'variations': [{'id': 'nominal', 'vertical_noise_mm': 0, 'slope_limit_delta_milli_deg': 0, 'energy_budget_delta_mwh': 0}, {'id': 'noisy-2m', 'vertical_noise_mm': 2000, 'slope_limit_delta_milli_deg': 0, 'energy_budget_delta_mwh': 0}, {'id': 'strict-slope', 'vertical_noise_mm': 0, 'slope_limit_delta_milli_deg': -12000, 'energy_budget_delta_mwh': 0}, {'id': 'low-energy', 'vertical_noise_mm': 0, 'slope_limit_delta_milli_deg': 0, 'energy_budget_delta_mwh': -30_000_000}]}
    raise SystemExit('example: terrain | route | pack')


def main(argv=None):
    ap = argparse.ArgumentParser(prog='metacoin-lunar'); sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('sources')
    e = sub.add_parser('example'); e.add_argument('name'); e.add_argument('--source', default='apollo11_32')
    for c in ('terrain', 'route'):
        p = sub.add_parser(c); p.add_argument('inputs'); p.add_argument('--png')
    p = sub.add_parser('pack'); p.add_argument('inputs'); p.add_argument('--out', required=True)
    p = sub.add_parser('reproduce'); p.add_argument('inputs'); p.add_argument('--manifest', required=True)
    p = sub.add_parser('check'); p.add_argument('kind'); p.add_argument('inputs'); p.add_argument('result')
    a = ap.parse_args(argv)
    from . import operations, reference, scenario, sources
    if a.cmd == 'sources':
        print(json.dumps(sources.catalog(), indent=1)); return 0
    if a.cmd == 'example':
        print(json.dumps(example(a.name, a.source), separators=(',', ':'))); return 0
    if a.cmd in ('terrain', 'route'):
        inputs = json.loads(Path(a.inputs).read_text()); r, outcome, summary = operations.run('terrain_tile' if a.cmd == 'terrain' else 'route_feasibility', inputs)
        if a.png and 'preview_png_base64' in r:
            Path(a.png).write_bytes(base64.b64decode(r.pop('preview_png_base64')))
        print(json.dumps(r, separators=(',', ':'))); return 0
    if a.cmd == 'pack':
        inputs = json.loads(Path(a.inputs).read_text()); result, files = scenario.compute(inputs, with_files=True); out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
        for name, data in files.items():
            p = out / name; p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes(data)
        (out / 'pack.zip').write_bytes(scenario.pack_zip(inputs)[1])
        print(json.dumps({'pack_id': result['manifest']['pack_id'], 'files': sorted(files), 'out': str(out)}, indent=1)); return 0
    if a.cmd == 'reproduce':
        rep = scenario.reproduce(json.loads(Path(a.inputs).read_text()), json.loads(Path(a.manifest).read_text())); print(json.dumps(rep, indent=1)); return 0 if rep['ok'] else 1
    if a.cmd == 'check':
        rep = reference.check(a.kind, json.loads(Path(a.inputs).read_text()), json.loads(Path(a.result).read_text())); print(json.dumps(rep, indent=1)); return 0 if rep['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
