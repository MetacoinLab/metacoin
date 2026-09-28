"""Order §63: a bounded endurance window with a mixed workload (model generation, retrieval, scientific compute,
audits, node work) at moderate concurrency against a temporary instance, sampling process memory, queue depth and
open files. Safe summaries only; no prompt or document content is retained.

    PYTHONPATH=. .venv-service/bin/python -m metacoin_service.endurance_expansion --minutes 15 --out endurance.json"""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from metacoin_service.tests.journeys_expansion import Journeys, CORPUS
from metacoin_service.tests.test_models import GEN, EMB
from metacoin_service.tests.test_compute_engine import batch_spec, mc_spec, heat_spec


def rss_of(pid):
    try:
        for line in open('/proc/%d/status' % pid):
            if line.startswith('VmRSS:'):
                return int(line.split()[1]) * 1024
    except OSError:
        return None


def fds_of(pid):
    try:
        return len(os.listdir('/proc/%d/fd' % pid))
    except OSError:
        return None


def main():
    p = argparse.ArgumentParser(); p.add_argument('--minutes', type=float, default=15); p.add_argument('--out'); a = p.parse_args()
    j = Journeys()
    samples, submitted, completed = [], {'generation': 0, 'answer': 0, 'batch': 0, 'mc': 0, 'heat': 0, 'audit': 0}, {}
    try:
        cid = None
        if j.models_ok:
            rc, reg = j.cli('owner', 'model-register', '--model-id', GEN['model_id'], '--hub-repo', GEN['hub_repo'], '--revision', GEN['revision'], '--operations', 'generate', '--license', 'apache-2.0')
            j.cli('owner', 'model-action', reg['id'], 'promote', '--operation', 'generate')
            rc, rege = j.cli('owner', 'model-register', '--model-id', EMB['model_id'], '--hub-repo', EMB['hub_repo'], '--revision', EMB['revision'], '--operations', 'embed', '--license', 'apache-2.0')
            j.cli('owner', 'model-action', rege['id'], 'promote', '--operation', 'embed')
            rc, col = j.cli('owner', 'knowledge-collection-create', '--name', 'endurance'); cid = col['id']
            for d in CORPUS['documents']:
                pth = Path(j.inst.temp.name) / ('e-' + d['name']); pth.write_text(d['text'])
                j.cli('owner', 'knowledge-add', cid, '--file', str(pth), '--name', d['name'], '--format', d['format'])
        w1 = j.worker_bg('w-e1'); w2 = j.worker_bg('w-e2')
        if cid:
            j.cli('owner', 'knowledge-index', cid, '--wait')
        t_end = time.time() + a.minutes * 60
        tick = 0
        api_pid = j.api.pid
        while time.time() < t_end:
            tick += 1
            st, q = j.api_json('get', '/api/v1/queue')
            depth = len(q.get('queued', []))
            if depth < 6:
                if j.models_ok and tick % 3 == 0:
                    st, g = j.api_json('post', '/api/v1/models/generate', json={'inputs': {'prompt': 'Give one sentence about diffusion (tick %d).' % tick, 'max_output_tokens': 24}}); submitted['generation'] += st == 202
                if j.models_ok and cid and tick % 5 == 0:
                    st, an = j.api_json('post', '/api/v1/knowledge/collections/' + cid + '/answers', json={'question': 'What interval does the reliability estimate use?', 'mode': 'extractive', 'k': 3}); submitted['answer'] += st == 202
                    j.api_json('post', '/api/v1/knowledge/collections/' + cid + '/search', json={'query': 'stability condition', 'mode': 'hybrid', 'k': 3})
                if tick % 2 == 0:
                    j.submit('temporal_batch', batch_spec(private_label='E%d' % tick), 'e%d' % tick); submitted['batch'] += 1
                if tick % 7 == 0:
                    j.submit('monte_carlo_reliability', mc_spec(private_label='EM%d' % tick, samples=50000), 'em%d' % tick); submitted['mc'] += 1
                if tick % 11 == 0:
                    j.submit('heat_diffusion', heat_spec(nx=128, ny=128, steps=2000, private_label='EH%d' % tick, device_policy='auto'), 'eh%d' % tick); submitted['heat'] += 1
                if tick % 4 == 0:
                    st, jobs = j.api_json('get', '/api/v1/jobs?state=succeeded&limit=20')
                    done = [x for x in jobs.get('items', []) if x['kind'] == 'temporal_batch']
                    if done:
                        st, v = j.api_json('post', '/api/v1/verification', json={'job_id': done[0]['id'], 'class': 'analytical', 'params': {}}); submitted['audit'] += st == 202
            st, status = j.api_json('get', '/api/v1/status')
            samples.append({'t': int(time.time()), 'queue_depth': depth, 'api_rss': rss_of(api_pid), 'api_fds': fds_of(api_pid), 'worker_rss': [rss_of(w.pid) for w in (w1, w2)], 'worker_fds': [fds_of(w.pid) for w in (w1, w2)],
                            'jobs_by_state': status.get('jobs_by_state'), 'tmp_files': len(list(Path(j.inst.home).rglob('*')))})
            time.sleep(5)
        # drain
        for _ in range(120):
            st, q = j.api_json('get', '/api/v1/queue')
            if not q.get('queued') and not q.get('running'):
                break
            time.sleep(2)
        st, status = j.api_json('get', '/api/v1/status')
        completed = status.get('jobs_by_state')
        st, mstat = j.api_json('get', '/api/v1/models/runtime')
    finally:
        j.close()
    first, last = samples[0], samples[-1]
    out = {'minutes': a.minutes, 'submitted': submitted, 'final_jobs_by_state': completed, 'samples': samples,
           'trend': {'api_rss_first': first['api_rss'], 'api_rss_last': last['api_rss'], 'api_fds_first': first['api_fds'], 'api_fds_last': last['api_fds'], 'worker_rss_first': first['worker_rss'], 'worker_rss_last': last['worker_rss'],
                     'max_queue_depth': max(s['queue_depth'] for s in samples), 'home_files_first': first['tmp_files'], 'home_files_last': last['tmp_files']},
           'runtimes_at_end': [r for r in (mstat.get('currently', {}) if isinstance(mstat, dict) else {}).get('runtimes', [])],
           'scope': 'bounded mixed-work window on one host; evidence about leaks/queues in this window only, not indefinite reliability'}
    text = json.dumps(out, indent=1, default=str)
    if a.out:
        Path(a.out).write_text(text)
    print(json.dumps({k: out[k] for k in ('submitted', 'final_jobs_by_state', 'trend')}, indent=1))


if __name__ == '__main__':
    main()
