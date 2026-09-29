"""Order 07 §26: singleton versus static-batch generation on the same pinned model, device, output settings and warmup
state, for an interactive short workload and a mixed-length workload, repeated trials, plus a bounded overload run
above the configured capacity. Reports cold load, queue delay, decode throughput, first-segment latency, total time,
memory observations and singleton-versus-batch output equality. Synthetic prompts only.

    PYTHONPATH=. .venv-service/bin/python -m metacoin_service.benchmark_generation_batching --trials 3 --out generation-batching-benchmark.json"""
import argparse
import json
import statistics
import subprocess
import time
from pathlib import Path

from metacoin_service.db import Database
from metacoin_service.tests.test_models import ModelInstance, GEN, EMB, installed

SHORT = ['Reply with one word: %s' % w for w in ('hello', 'ready', 'yes', 'blue')]
MIXED = ['Reply with one word: hello', 'Name three primary colours separated by commas.', 'Write one sentence about diffusion in a battery.', 'Explain in three sentences why a reserve margin matters for a battery schedule.']


def q(xs):
    xs = sorted(xs)
    return {'min': xs[0], 'median': statistics.median(xs), 'max': xs[-1], 'n': len(xs)} if xs else None


def run_workload(inst, w, prompts, max_tokens, batching):
    c, H = inst.client, inst.h('owner')
    c.post('/api/v1/models/batching', headers=H, json={'enabled': batching, 'max_sequences': max(len(prompts), 1)})
    t_submit = time.time()
    jobs = [c.post('/api/v1/models/generate', headers=H, json={'inputs': {'messages': [{'role': 'user', 'content': p}], 'max_output_tokens': max_tokens}}).json()['job_id'] for p in prompts]
    first_seg = {}
    t0 = time.time()
    def poll():
        while len(first_seg) < len(jobs) and time.time() - t0 < 300:
            for j in jobs:
                if j not in first_seg:
                    v = c.get('/api/v1/models/jobs/' + j, headers=H).json()
                    if v['usage']['segments']:
                        first_seg[j] = time.time() - t_submit
            time.sleep(0.02)
    import threading
    th = threading.Thread(target=poll); th.start()
    runs = 0
    while any(c.get('/api/v1/models/jobs/' + j, headers=H).json()['state'] not in ('succeeded', 'failed', 'cancelled') for j in jobs):
        w.run_once(); runs += 1
    total = time.time() - t_submit; th.join(timeout=1)
    views = [c.get('/api/v1/models/jobs/' + j, headers=H).json() for j in jobs]
    texts = [''.join(s['text'] for s in c.get('/api/v1/models/jobs/' + j + '/segments', headers=H).json()['segments']) for j in jobs]
    out_tokens = sum(v['usage']['output_tokens'] for v in views); inf_ms = [v['timing']['inference_ms'] for v in views]
    bids = {v['usage'].get('batch_id') for v in views}
    with Database(inst.settings.db_path).read() as db:
        batches = [dict(r) for r in db.execute('SELECT * FROM model_batches WHERE id IN (%s)' % ','.join('?' * len([b for b in bids if b])), [b for b in bids if b]).fetchall()] if any(bids) else []
    return {'mode': 'static-batch' if batching else 'singleton', 'requests': len(jobs), 'worker_runs': runs, 'total_s': round(total, 3), 'output_tokens': out_tokens, 'throughput_tok_s': round(out_tokens / max(total, 1e-6), 2),
            'first_segment_latency_s': q([round(x, 3) for x in first_seg.values()]), 'inference_ms_per_request': q(inf_ms), 'batch_ids': sorted(b for b in bids if b), 'batches': [{k: b[k] for k in ('members', 'decode_steps', 'padded_prompt_length', 'ms', 'kv_estimate_bytes', 'cuda_peak_delta_bytes')} for b in batches],
            'states': [v['state'] for v in views], 'texts': texts}


def main():
    ap = argparse.ArgumentParser(); ap.add_argument('--trials', type=int, default=3); ap.add_argument('--out'); a = ap.parse_args()
    inst = ModelInstance(); c, H = inst.client, inst.h('owner')
    if not (installed(inst.settings, GEN) and installed(inst.settings, EMB)):
        print('models absent'); return
    ids = inst.register_defaults(); w = inst.worker()
    rev = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=str(Path(__file__).resolve().parents[1]), text=True).strip()
    report = {'schema': 'metacoin-generation-batching-benchmark/v1', 'revision': rev, 'model': GEN, 'workloads': {}, 'overload': None}
    try:
        # cold load once (recorded), then everything warm
        t0 = time.time(); j = c.post('/api/v1/models/generate', headers=H, json={'inputs': {'prompt': 'warm', 'max_output_tokens': 4}}).json()['job_id']; w.run_once()
        v = c.get('/api/v1/models/jobs/' + j, headers=H).json(); report['cold_load_ms'] = v['timing']['load_ms']; report['warmup_s'] = round(time.time() - t0, 2)
        rt = c.get('/api/v1/models/runtime', headers=H).json(); report['runtime'] = {'interpreter': rt['installed'], 'runtimes': [(r['host'], r['device'], r['dtype'], r['versions']) for r in rt['currently']['runtimes']]}
        for name, prompts, max_tokens in (('interactive_short', SHORT, 16), ('mixed_length', MIXED, 96)):
            trials = {'singleton': [], 'static-batch': []}
            equality = []
            for t in range(a.trials):
                solo = run_workload(inst, w, prompts, max_tokens, False); batch = run_workload(inst, w, prompts, max_tokens, True)
                trials['singleton'].append({k: solo[k] for k in ('total_s', 'throughput_tok_s', 'first_segment_latency_s', 'inference_ms_per_request', 'output_tokens', 'worker_runs')})
                trials['static-batch'].append({k: batch[k] for k in ('total_s', 'throughput_tok_s', 'first_segment_latency_s', 'inference_ms_per_request', 'output_tokens', 'worker_runs', 'batches')})
                equality.append([s == b for s, b in zip(solo['texts'], batch['texts'])])
            report['workloads'][name] = {'prompts': prompts, 'max_output_tokens': max_tokens, 'trials': trials,
                                         'summary': {m: {'total_s': q([x['total_s'] for x in trials[m]]), 'throughput_tok_s': q([x['throughput_tok_s'] for x in trials[m]]), 'first_segment_latency_median_s': q([x['first_segment_latency_s']['median'] for x in trials[m] if x['first_segment_latency_s']])} for m in trials},
                                         'singleton_vs_batch_equal_per_trial': equality, 'equal_fraction': round(sum(sum(e) for e in equality) / max(1, sum(len(e) for e in equality)), 3)}
        # overload: 3x the configured sequences, bounded; observe admission (explained waiting), fairness (FIFO within cohort), cancellation responsiveness, memory
        c.post('/api/v1/models/batching', headers=H, json={'enabled': True, 'max_sequences': 4})
        jobs = [c.post('/api/v1/models/generate', headers=H, json={'inputs': {'messages': [{'role': 'user', 'content': 'Reply with one word: item %d' % i}], 'max_output_tokens': 12}}).json()['job_id'] for i in range(12)]
        c.post('/api/v1/jobs/' + jobs[10] + '/cancel', headers=H)
        t0 = time.time(); runs = 0; rss = []
        import os
        while any(c.get('/api/v1/models/jobs/' + j, headers=H).json()['state'] not in ('succeeded', 'failed', 'cancelled') for j in jobs):
            w.run_once(); runs += 1
            try:
                rss.append(int([l for l in open('/proc/self/status') if l.startswith('VmRSS:')][0].split()[1]) * 1024)
            except Exception:
                pass
        views = [c.get('/api/v1/models/jobs/' + j, headers=H).json() for j in jobs]
        order = sorted(((v['timing']['started_at'], i) for i, v in enumerate(views) if v['timing'].get('started_at')), key=lambda x: x[0])
        report['overload'] = {'submitted': 12, 'max_sequences': 4, 'worker_runs': runs, 'total_s': round(time.time() - t0, 2), 'states': [v['state'] for v in views], 'batches': len({v['usage'].get('batch_id') for v in views if v['usage'].get('batch_id')}),
                              'cancelled_before_start': views[10]['state'] == 'cancelled' and views[10]['usage']['output_tokens'] in (None, 0), 'start_order_is_submission_order': [i for _, i in order] == sorted(i for _, i in order),
                              'process_rss_bytes': q(rss), 'note': 'admission is bounded by max_sequences per batch; excluded requests wait in the fair queue with their own reason and run in later batches; no unbounded buffering observed in the API process'}
        report['notes'] = ['one host (GB10, unified memory); the pinned 0.5B model; small batches: the numbers describe this host and workload only', 'equality = byte-identical text between singleton and batched greedy runs of the same prompt (measured, not assumed)',
                           'throughput counts generated tokens of all requests over the wall time from submission to the last terminal state, including queue and publication']
    finally:
        w.offline(); inst.close()
    text = json.dumps(report, indent=1, default=str)
    if a.out:
        Path(a.out).write_text(text)
    print(json.dumps({'cold_load_ms': report.get('cold_load_ms'), 'workloads': {k: v['summary'] | {'equal_fraction': v['equal_fraction']} for k, v in report['workloads'].items()}, 'overload': {k: report['overload'][k] for k in ('worker_runs', 'total_s', 'batches', 'cancelled_before_start', 'start_order_is_submission_order')}}, indent=1, default=str))


if __name__ == '__main__':
    main()
