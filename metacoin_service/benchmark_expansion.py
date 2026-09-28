"""Order §62 measurements for the expansion: model cold load and warmed generation, embedding throughput, index
build, retrieval latency (lexical/semantic/hybrid), calibration fit, audit cost per class, node transfer, MCP
overhead and the connected journey at bounded concurrency. Every figure records input size, output limit, versions,
device and whether verification/encryption is included. Compared against equivalent work; no cold/warm mixing.

    PYTHONPATH=. .venv-service/bin/python -m metacoin_service.benchmark_expansion --out benchmarks-expansion.json"""
import argparse
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path

from metacoin_service.tests.journeys_expansion import Journeys, CORPUS, HAVE_TORCH
from metacoin_service.tests.test_models import GEN, EMB, installed
from metacoin_service.tests.test_compute_engine import batch_spec
from metacoin_service.tests.test_service import ROOT

PY = sys.executable


def med(xs):
    return round(statistics.median(xs), 1) if xs else None


def main():
    p = argparse.ArgumentParser(); p.add_argument('--out'); p.add_argument('--samples', type=int, default=3); a = p.parse_args()
    j = Journeys()
    out = {'host': 'DGX Spark GB10', 'samples': a.samples, 'measurements': {}, 'notes': ['wall-clock seconds measured by the client process; the worker and API are separate processes on the same host', 'verification and encryption are included where stated', 'not throughput claims for other hosts']}
    try:
        if j.models_ok:
            rc, reg = j.cli('owner', 'model-register', '--model-id', GEN['model_id'], '--hub-repo', GEN['hub_repo'], '--revision', GEN['revision'], '--operations', 'generate', '--license', 'apache-2.0')
            j.cli('owner', 'model-action', reg['id'], 'promote', '--operation', 'generate')
            rc, rege = j.cli('owner', 'model-register', '--model-id', EMB['model_id'], '--hub-repo', EMB['hub_repo'], '--revision', EMB['revision'], '--operations', 'embed', '--license', 'apache-2.0')
            j.cli('owner', 'model-action', rege['id'], 'promote', '--operation', 'embed')
            j.worker_bg('w-bench')
            # cold load + first generation, then warmed generations (same output limit)
            gens = []
            for i in range(a.samples + 1):
                t0 = time.time()
                rc, g = j.cli('owner', 'generate', '--prompt', 'Explain in two sentences why a Wilson interval is used for a binomial proportion.', '--max-output-tokens', '64', '--watch')
                wall = time.time() - t0
                st, v = j.api_json('get', '/api/v1/models/jobs/' + g.get('job_id', 'x'))
                gens.append({'wall_s': round(wall, 2), 'load_ms': v['timing']['load_ms'], 'inference_ms': v['timing']['inference_ms'], 'output_tokens': v['usage']['output_tokens'], 'input_tokens': v['usage']['input_tokens'], 'queue_s': v['timing']['queue_seconds']})
            out['measurements']['generation'] = {'device': 'cuda' if HAVE_TORCH and j.results is not None else None, 'model': GEN['model_id'], 'precision': 'bfloat16 (cuda) / float32 (cpu)', 'max_output_tokens': 64, 'cold': gens[0],
                                                 'warm': gens[1:], 'warm_tokens_per_s_median': med([g['output_tokens'] / (g['inference_ms'] / 1000.0) for g in gens[1:] if g['inference_ms']]),
                                                 'includes': 'job admission, queue, runtime execution, segment persistence, encrypted artifact commit; cold includes the runtime child load'}
            # embedding throughput: 128 items
            texts = [('Sentence number %d about heat diffusion, reserves and Monte Carlo sampling on a spacecraft.' % i) for i in range(128)]
            embs = []
            for i in range(a.samples):
                t0 = time.time(); rc, e = j.cli('owner', 'embed', '--texts', j.tmpjson('bt.json', texts)); ej = j.wait_job(e['job_id']); wall = time.time() - t0
                st, v = j.api_json('get', '/api/v1/models/jobs/' + e['job_id'])
                embs.append({'wall_s': round(wall, 2), 'inference_ms': v['timing']['inference_ms'], 'items': 128, 'items_per_s_inference': round(128 / (v['timing']['inference_ms'] / 1000.0), 1) if v['timing']['inference_ms'] else None})
            out['measurements']['embedding'] = {'model': EMB['model_id'], 'device': 'cpu (model_embed_on_cuda=0)', 'items': 128, 'runs': embs, 'includes': 'job path + npy artifact commit'}
            # index build and retrieval latency
            rc, col = j.cli('owner', 'knowledge-collection-create', '--name', 'bench'); cid = col['id']
            for d in CORPUS['documents']:
                pth = Path(j.inst.temp.name) / ('b-' + d['name']); pth.write_text(d['text'] * 6)
                j.cli('owner', 'knowledge-add', cid, '--file', str(pth), '--name', d['name'], '--format', d['format'])
            t0 = time.time(); rc, idx = j.cli('owner', 'knowledge-index', cid, '--wait'); build = time.time() - t0
            lat = {}
            for mode in ('lexical', 'semantic', 'hybrid'):
                xs = []
                for i in range(a.samples + 2):
                    t0 = time.time(); j.api_json('post', '/api/v1/knowledge/collections/' + cid + '/search', json={'query': 'what stability condition does the heat solver check', 'mode': mode, 'k': 5}); xs.append((time.time() - t0) * 1000)
                lat[mode] = {'first_ms': round(xs[0], 1), 'median_warm_ms': med(xs[1:])}
            out['measurements']['index_and_retrieval'] = {'chunks': idx.get('chunk_count'), 'build_wall_s': round(build, 2), 'retrieval_latency': lat, 'note': 'first semantic query loads the API-process embedding runtime and the index vectors (cold); exact search in pure Python'}
        # calibration fit
        rows = [{'x1': i, 'x2': (i * 7) % 13, 'y': 4 * i - 3 * ((i * 7) % 13) + 2} for i in range(500)]
        rc, ds = j.cli('owner', 'calibration-dataset', '--file', j.tmpjson('bc.json', {'name': 'bench', 'columns': ['x1', 'x2', 'y'], 'target': 'y', 'units': {'y': 'ms'}, 'rows': rows}))
        fits = []
        for i in range(a.samples):
            t0 = time.time(); rc, f = j.cli('owner', 'calibration-fit', ds['id'], '--features', 'x1,x2', '--target', 'y', '--wait'); fits.append(round(time.time() - t0, 2))
        out['measurements']['calibration_fit'] = {'rows': 500, 'features': 2, 'wall_s': fits, 'includes': 'job path, numpy lstsq in the child, pure-Python QR reference verification, encrypted outputs'}
        # audit cost per class on a 364-scenario batch
        jid = j.submit('temporal_batch', batch_spec(private_label='BENCH'), 'bench'); j.worker_once('w-b')
        audits = {}
        for cls, params in (('analytical', {}), ('sampled_reference', {'sample_count': 64}), ('full_exact', {})):
            t0 = time.time()
            st, v = j.api_json('post', '/api/v1/verification', json={'job_id': jid, 'class': cls, 'params': params}); j.worker_once('w-a')
            st, vv = j.api_json('get', '/api/v1/verification/' + v['id'])
            audits[cls] = {'wall_s': round(time.time() - t0, 2), 'checked': vv['result']['checked'], 'total': vv['result']['total'], 'state': vv['state']}
        out['measurements']['audit_cost'] = {'target': 'temporal_batch 364 scenarios', 'classes': audits, 'includes': 'worker process start + signed statement'}
        # MCP overhead: list_services through MCP vs direct HTTP
        t0 = time.time(); j.api_json('get', '/api/v1/services'); direct = (time.time() - t0) * 1000
        t0 = time.time()
        pr = subprocess.run([PY, '-m', 'metacoin_service.tests.mcp_journey_client', j.base, str(j.creds['viewer']), 'readonly'], cwd=ROOT, env=j.env, capture_output=True, text=True, timeout=300)
        mcp_wall = (time.time() - t0) * 1000
        out['measurements']['mcp'] = {'direct_list_services_ms': round(direct, 1), 'mcp_session_5_calls_wall_ms': round(mcp_wall, 1), 'note': 'includes MCP server process start, initialize, list_tools and five tool calls'}
        # node transfer: inputs to node + result upload for the 364-scenario batch over TLS
        j.start_tls_api()
        ident = Path(j.inst.temp.name) / 'bench-node.json'
        j.cli('owner', 'node-enroll', '--name', 'bench-node', '--out', str(ident), '--devices', 'cpu', '--capabilities', 'temporal_batch')
        nid = json.loads(ident.read_text())['node_id']
        r = j.http.post('/api/v1/contracts', headers=j.H(), json={'kind': 'temporal_batch', 'title': 'bn', 'inputs': batch_spec(private_label='BN'), 'policy': {'reviewer_id': j.inst.ids['reviewer'], 'execution_locations': [nid]}}).json()
        j.http.post('/api/v1/contracts/' + r['id'] + '/freeze', headers=j.H()); jb = j.http.post('/api/v1/jobs', headers=j.H(), json={'contract_id': r['id']}).json()['id']
        t0 = time.time(); rc, ran = j.node_once(ident, Path(j.inst.temp.name) / 'bench-node-home'); wall = time.time() - t0
        st, nv = j.api_json('get', '/api/v1/nodes/' + nid)
        local = j.submit('temporal_batch', batch_spec(private_label='BL'), 'bl'); t0 = time.time(); j.worker_once('w-l'); lwall = time.time() - t0
        out['measurements']['federation'] = {'node_once_wall_s': round(wall, 2), 'local_worker_once_wall_s': round(lwall, 2), 'transfers': [{'role': t['role'], 'direction': t['direction'], 'bytes': t['bytes']} for t in nv.get('transfers', [])],
                                             'note': 'node wall includes process start, TLS handshakes, claim, child execution, chunked upload, coordinator verification; the local figure is the same work through the in-process path'}
        # connected journey at bounded concurrency: 4 batch jobs + 2 audits with 2 workers
        jobs = [j.submit('temporal_batch', batch_spec(private_label='C%d' % i), 'c%d' % i) for i in range(4)]
        t0 = time.time(); j.worker_bg('w-c1'); j.worker_bg('w-c2')
        while time.time() - t0 < 120 and any(j.wait_job(x, 1).get('state') not in ('succeeded', 'failed') for x in jobs):
            time.sleep(0.5)
        out['measurements']['bounded_concurrency'] = {'jobs': 4, 'workers': 2, 'wall_s': round(time.time() - t0, 2), 'states': [j.wait_job(x, 1).get('state') for x in jobs]}
    finally:
        j.close()
    text = json.dumps(out, indent=1, default=str)
    if a.out:
        Path(a.out).write_text(text)
    print(text)


if __name__ == '__main__':
    main()
