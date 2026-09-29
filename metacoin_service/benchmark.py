"""Service measurements on this machine (order §27). Medians and nearest-rank p95 over N samples.

Measures: authenticated job submission (HTTP, in-process ASGI), queue delay and computation
separately (worker claim -> child process -> publish), private audit (recomputation from
decrypted vaults), encrypted artifact round trip (age encrypt + decrypt of an evidence vault),
signed review verification (Ed25519 + trust table), safe-runtime calculation, and the full
x402 test-mode exchange over a real TCP socket (client process -> server process -> facilitator
double over HTTP). fsync and encryption are included where stated. Memory is not measured.
"""
import argparse
import json
import math
import os
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
    return {'samples': len(s), 'median_ms': statistics.median(s) / 1e6, 'p95_ms': s[math.ceil(0.95 * len(s)) - 1] / 1e6,
            'min_ms': s[0] / 1e6, 'max_ms': s[-1] / 1e6}


def run(samples):
    from fastapi.testclient import TestClient
    from experiments.work_contracts import fixtures
    from metacoin_service import api, artifacts, bootstrap, config, crypto, db as database, reviews, science, worker as worker_mod
    from metacoin_service.tests.test_service import free_port, own_inputs
    out = {'scope': 'service overhead on this machine; not throughput; not a production claim',
           'python': sys.version.split()[0], 'platform': platform.platform(), 'machine': platform.machine(),
           'samples': samples, 'quantile': 'nearest-rank p95', 'includes': {'fsync': 'SQLite synchronous=FULL on every commit',
                                                                            'encryption': 'age x25519 for every private artifact',
                                                                            'signature': 'Ed25519 sign on decision, verify on read'}}
    try:
        out['revision'] = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    except Exception:
        out['revision'] = 'unavailable'
    with tempfile.TemporaryDirectory() as tmp:
        settings = config.Settings(home=Path(tmp) / 'home', provider_mode='test-http')
        info = bootstrap.init(settings)
        creds = json.load(open(info['credential_file']))
        tok = {r: e['token'] for r, e in creds['principals'].items()}
        h = lambda r: {'Authorization': 'Bearer ' + tok[r]}
        client = TestClient(api.create_app(settings))
        worker = worker_mod.Worker(database.Database(settings.db_path), artifacts.ArtifactStore(settings), settings)
        t_submit, t_queue, t_compute, t_audit, t_verify = [], [], [], [], []
        jids = []
        for i in range(samples + 3):
            cid = client.post('/api/v1/contracts', headers=h('owner'), json={'kind': 'energy_audit', 'title': 'b', 'inputs': own_inputs(),
                                                                             'policy': {'reviewer_id': creds['principals']['reviewer']['principal_id']}}).json()['id']
            client.post('/api/v1/contracts/' + cid + '/freeze', headers=h('owner'))
            t0 = time.perf_counter_ns()
            jid = client.post('/api/v1/jobs', headers=h('owner'), json={'contract_id': cid}).json()['id']
            t1 = time.perf_counter_ns()
            job = worker.claim()
            t2 = time.perf_counter_ns()
            worker.execute(job)
            t3 = time.perf_counter_ns()
            client.post('/api/v1/jobs/' + jid + '/review-request', headers=h('owner'))
            t4 = time.perf_counter_ns()
            client.get('/api/v1/reviews/' + jid + '/evidence', headers=h('reviewer'))
            t5 = time.perf_counter_ns()
            client.post('/api/v1/reviews/' + jid + '/decision', headers=h('reviewer'), json={'decision': 'accepted'})
            t6 = time.perf_counter_ns()
            client.get('/api/v1/reviews/' + jid, headers=h('viewer'))
            t7 = time.perf_counter_ns()
            if i >= 3:
                t_submit.append(t1 - t0); t_queue.append(t2 - t1); t_compute.append(t3 - t2)
                t_audit.append(t5 - t4); t_verify.append(t7 - t6)
                jids.append(jid)
        out['authenticated_job_submission_http'] = summarize(t_submit)
        out['worker_claim_queue_delay'] = summarize(t_queue)
        out['worker_execute_child_process_encrypt_publish'] = summarize(t_compute)
        out['private_audit_decrypt_and_recompute_http'] = summarize(t_audit)
        out['signed_review_read_and_verify_http'] = summarize(t_verify)
        # encrypted artifact round trip (in memory)
        store = artifacts.ArtifactStore(settings)
        with database.Database(settings.db_path).read() as db:
            pub = store.service_public(db)
        vault_bytes = json.dumps(fixtures.prepare('x')[2]).encode()
        t_enc, t_dec = [], []
        ident = store.identity()
        for _ in range(samples + 3):
            t0 = time.perf_counter_ns(); ct = crypto.encrypt_bytes(vault_bytes, [pub]); t1 = time.perf_counter_ns()
            crypto.decrypt_bytes(ct, ident); t2 = time.perf_counter_ns()
            t_enc.append(t1 - t0); t_dec.append(t2 - t1)
        out['age_encrypt_evidence_vault'] = dict(summarize(t_enc[3:]), plaintext_bytes=len(vault_bytes), ciphertext_bytes=len(ct))
        out['age_decrypt_evidence_vault'] = summarize(t_dec[3:])
        sr = {'available_low': 1_000_000, 'available_high': 1_100_000, 'reserve': 100_000,
              'fixed_segments': [{'duration': 600, 'power_low': 800, 'power_high': 1000}], 'variable_power_low': 100,
              'variable_power_high': 250, 'duration_cap': 3600, 'units': {'energy': 'mJ', 'power': 'mW', 'duration': 's'},
              'assumptions': ['no_recharge', 'usable_energy_at_load_boundary', 'piecewise_constant_power_bounds', 'no_unmodeled_loads'],
              'provenance': 'synthetic', 'private_label': 'x'}
        t_sr = []
        for _ in range(samples + 3):
            t0 = time.perf_counter_ns(); science.safe_runtime(sr); t_sr.append(time.perf_counter_ns() - t0)
        out['safe_runtime_calculation'] = summarize(t_sr[3:])
        # full x402 exchange over TCP
        port = free_port()
        env = dict(os.environ, PYTHONPATH=str(ROOT))
        proc = subprocess.Popen([sys.executable, '-m', 'metacoin_service', '--home', str(settings.home), '--provider-mode', 'test-http',
                                 'serve', '--port', str(port)], cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        import httpx
        base = 'http://127.0.0.1:' + str(port)
        for _ in range(100):
            try:
                if httpx.get(base + '/api/health', timeout=1).status_code == 200:
                    break
            except Exception:
                time.sleep(0.1)
        try:
            t_x = []
            for jid in jids[:samples]:
                t0 = time.perf_counter_ns()
                p = subprocess.run([sys.executable, '-m', 'metacoin_service.tests.x402_client', base, jid], cwd=ROOT, env=env,
                                   capture_output=True, text=True, timeout=60)
                t_x.append(time.perf_counter_ns() - t0)
                assert json.loads(p.stdout.strip().splitlines()[-1])['second_status'] == 200, p.stdout[-300:]
            out['x402_full_exchange_tcp_client_process'] = dict(summarize(t_x), note='includes client interpreter start-up; two HTTP round trips; facilitator double over HTTP')
        finally:
            proc.terminate(); proc.wait(timeout=10)
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--samples', type=int, default=30)
    parser.add_argument('--out')
    args = parser.parse_args()
    result = run(args.samples)
    text = json.dumps(result, indent=2)
    if args.out:
        fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as stream:
            stream.write(text + '\n')
    else:
        print(text)


if __name__ == '__main__':
    main()
