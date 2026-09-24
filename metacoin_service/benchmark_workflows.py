"""Measurements for the workflow/services features on this machine (order §44). Medians and nearest-rank p95.

    PYTHONPATH=. .venv-service/bin/python -m metacoin_service.benchmark_workflows --samples 20 --out FILE

Everything runs in a temporary home in simulation provider mode unless stated; cache-hit latency is
separated from new computation, transport (HTTP round trip through the in-process ASGI client) from
scientific runtime (child process), and scheduling cost from job runtime. Not throughput claims.
"""
import argparse
import json
import math
import platform
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def summarize(ns):
    s = sorted(ns)
    return {'samples': len(s), 'median_ms': round(statistics.median(s) / 1e6, 3), 'p95_ms': round(s[math.ceil(0.95 * len(s)) - 1] / 1e6, 3),
            'min_ms': round(s[0] / 1e6, 3), 'max_ms': round(s[-1] / 1e6, 3)}


def timed(fn):
    t0 = time.perf_counter_ns(); r = fn(); return time.perf_counter_ns() - t0, r


def run(samples):
    from fastapi.testclient import TestClient
    from metacoin_service import api, artifacts, bootstrap, config, db as database, scheduling, temporal, worker as worker_mod
    from metacoin_service.tests.test_service import own_inputs
    from metacoin_service.tests.test_agents import TEMPORAL
    out = {'scope': 'service overhead on this machine; medians over N samples; not throughput; not a production claim',
           'python': sys.version.split()[0], 'platform': platform.platform(), 'machine': platform.machine(), 'samples': samples,
           'provider_mode': 'simulation', 'quantile': 'nearest-rank p95', 'input_sizes': {'temporal_segments': len(TEMPORAL['segments']), 'energy_audit_segments': len(own_inputs()['segments'])}}
    try:
        out['revision'] = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    except Exception:
        out['revision'] = 'unavailable'
    with tempfile.TemporaryDirectory() as tmp:
        settings = config.Settings(home=Path(tmp) / 'home', provider_mode='simulation')
        info = bootstrap.init(settings)
        creds = json.load(open(info['credential_file']))
        tok = {r: e['token'] for r, e in creds['principals'].items()}
        H = {'Authorization': 'Bearer ' + tok['owner']}
        policy = {'reviewer_id': creds['principals']['reviewer']['principal_id']}
        client = TestClient(api.create_app(settings))
        D = database.Database(settings.db_path)
        worker = worker_mod.Worker(D, artifacts.ArtifactStore(settings), settings)
        # 1. catalog query latency and quote latency (transport + db)
        t_cat, t_quote = [], []
        sid = next(s['id'] for s in client.get('/api/v1/services', headers=H).json()['items'] if s['kind'] == 'temporal_energy')
        for _ in range(samples):
            t_cat.append(timed(lambda: client.get('/api/v1/services?input_type=temporal_series', headers=H))[0])
            t_quote.append(timed(lambda: client.post('/api/v1/services/' + sid + '/quote', headers=H, json={'inputs': TEMPORAL}))[0])
        out['catalog_list_http'] = summarize(t_cat); out['quote_http'] = summarize(t_quote)
        # 2. temporal verifier scientific runtime (pure function, no transport) and a 512-segment input
        t_sci, t_sci_big = [], []
        big = dict(TEMPORAL, segments=[dict(TEMPORAL['segments'][0]) for _ in range(512)])
        for _ in range(samples):
            t_sci.append(timed(lambda: temporal.analyze(TEMPORAL))[0])
            t_sci_big.append(timed(lambda: temporal.analyze(big))[0])
        out['temporal_analyze_1_segment'] = summarize(t_sci); out['temporal_analyze_512_segments'] = summarize(t_sci_big)
        # 3. queue delay (submit -> claim), scheduling cost (fair order over a backlog), job runtime (child process), tx duration
        cids = []
        for i in range(samples):
            cid = client.post('/api/v1/contracts', headers=H, json={'kind': 'energy_audit', 'title': 'b%d' % i, 'inputs': own_inputs('B%d' % i), 'policy': policy}).json()['id']
            assert client.post('/api/v1/contracts/' + cid + '/freeze', headers=H).status_code == 200; cids.append(cid)
        t_submit, submit_at = [], {}
        for cid in cids:
            dt, r = timed(lambda: client.post('/api/v1/jobs', headers=H, json={'contract_id': cid}))
            t_submit.append(dt); submit_at[r.json()['id']] = time.perf_counter_ns()
        out['job_submit_http_tx'] = summarize(t_submit)
        t_sched = []
        with D.read() as db:
            for _ in range(samples):
                t_sched.append(timed(lambda: scheduling.fair_order(db, worker.capabilities))[0])
        out['scheduling_fair_order_over_%d_queued' % samples] = summarize(t_sched)
        t_claim, t_exec, t_queue_delay = [], [], []
        for _ in range(samples):
            dt, job = timed(worker.claim)
            t_claim.append(dt); t_queue_delay.append(time.perf_counter_ns() - submit_at[job['id']])
            t_exec.append(timed(lambda: worker.execute(job))[0])
        out['claim_tx'] = summarize(t_claim); out['job_runtime_child_process_energy_audit'] = summarize(t_exec)
        out['queue_delay_submit_to_claim_single_worker_serial'] = summarize(t_queue_delay)
        with D.read() as db:
            sizes = [r[0] for r in db.execute("SELECT size_plaintext FROM artifacts WHERE kind='evidence_vault'")]
        out['evidence_artifact_bytes'] = {'samples': len(sizes), 'median': statistics.median(sizes) if sizes else None, 'max': max(sizes) if sizes else None}
        # 4. cache hit vs new computation
        t_hit = []
        for i in range(samples):
            cid = client.post('/api/v1/contracts', headers=H, json={'kind': 'energy_audit', 'title': 'r%d' % i, 'inputs': own_inputs('B%d' % i), 'policy': policy}).json()['id']
            assert client.post('/api/v1/contracts/' + cid + '/freeze', headers=H).status_code == 200
            dt, r = timed(lambda: client.post('/api/v1/jobs', headers=H, json={'contract_id': cid, 'reuse': True}))
            assert r.json()['reused_from'], r.text
            t_hit.append(dt)
        out['reuse_cache_hit_submit_to_committed'] = summarize(t_hit)
        out['new_computation_submit_to_committed_estimate'] = {'note': 'job_submit_http_tx + claim_tx + job_runtime_child_process (serial)',
                                                                'median_ms': round(out['job_submit_http_tx']['median_ms'] + out['claim_tx']['median_ms'] + out['job_runtime_child_process_energy_audit']['median_ms'], 3)}
        # 5. campaign evaluation throughput (grid of N candidates through the real tick + worker)
        grid = {'name': 'bench', 'kind': 'temporal_energy', 'base': TEMPORAL, 'axes': [{'path': 'capacity', 'values': [8000 + 100 * i for i in range(samples)]}]}
        r = client.post('/api/v1/campaigns', headers=H, json={'definition': grid})
        if r.status_code == 201:
            cmp_id = r.json()['campaign_id']; client.post('/api/v1/campaigns/' + cmp_id + '/run', headers=H); t0 = time.perf_counter_ns(); done = 0
            for _ in range(samples * 3):
                worker.tick_workflows(); worker.run_once()
                v = client.get('/api/v1/campaigns/' + cmp_id, headers=H).json()
                if v['done'] >= v['total_candidates']:
                    done = v['done']; break
            dt = time.perf_counter_ns() - t0
            out['campaign_grid_throughput'] = {'candidates': done, 'total_ms': round(dt / 1e6, 1), 'per_candidate_ms': round(dt / 1e6 / max(done, 1), 1), 'workers': 1}
        else:
            out['campaign_grid_throughput'] = {'skipped': r.text[:200]}
        # 6. event poll latency and db transaction duration
        t_ev, t_tx = [], []
        for _ in range(samples):
            t_ev.append(timed(lambda: client.get('/api/v1/events?after=0&limit=100', headers=H))[0])
            def tx():
                with D.tx() as db:
                    db.execute("UPDATE meta SET value=value WHERE key='service_signing_public'")
            t_tx.append(timed(tx)[0])
        out['events_poll_100_http'] = summarize(t_ev); out['db_write_tx_fsync'] = summarize(t_tx)
    return out


def main():
    p = argparse.ArgumentParser(); p.add_argument('--samples', type=int, default=20); p.add_argument('--out')
    a = p.parse_args()
    result = run(a.samples)
    text = json.dumps(result, indent=1)
    if a.out:
        Path(a.out).write_text(text)
    print(text)


if __name__ == '__main__':
    main()
