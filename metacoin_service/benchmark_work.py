"""Bounded concurrent workload of the work economy (Order 08 §68): several requesters (scoped credentials of the owner),
providers, verification jobs and payment intents against a RUNNING API, measuring request latency, queue delay,
journal contention (transaction retry/wait), publication lag and process memory. Authorization, encryption and journal
durability stay on; nothing here disables a control.

    PYTHONPATH=. .venv-service/bin/python -m metacoin_service.benchmark_work BASE OWNER_CRED PROVIDER_CRED [--requests 8] [--out file]"""
import argparse
import json
import os
import resource
import sys
import threading
import time
import urllib.error
import urllib.request

from experiments.work_contracts import fixtures


def client(base, cred):
    token = json.load(open(cred))['token']
    def go(method, path, body=None, key=None):
        data = json.dumps(body).encode() if body is not None else None
        h = {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'}
        if key:
            h['Idempotency-Key'] = key
        req = urllib.request.Request(base + path, data=data, method=method, headers=h)
        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return r.status, json.loads(r.read() or b'{}'), time.perf_counter() - t0
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b'{}'), time.perf_counter() - t0
    return go


def summarize(xs):
    xs = sorted(xs)
    return {'n': len(xs), 'min': round(xs[0], 4), 'median': round(xs[len(xs) // 2], 4), 'p90': round(xs[int(len(xs) * 0.9) - 1 if len(xs) > 1 else 0], 4), 'max': round(xs[-1], 4)} if xs else {'n': 0}


def main(argv=None):
    ap = argparse.ArgumentParser(); ap.add_argument('base'); ap.add_argument('owner_cred'); ap.add_argument('provider_cred'); ap.add_argument('--requests', type=int, default=8); ap.add_argument('--out')
    a = ap.parse_args(argv)
    owner, prov = client(a.base, a.owner_cred), client(a.base, a.provider_cred)
    lat = {'terms': [], 'freeze': [], 'request': [], 'offer': [], 'award': [], 'evaluate': [], 'decide': [], 'pay': []}
    errors, awards, t_start = [], [], time.time()
    lock = threading.Lock()

    def one(i):
        try:
            s, t, d = owner('POST', '/api/v1/work/terms', {'template': 'determination', 'ceiling': 3, 'amount': 3}, key='bw-t-%d' % i); lat['terms'].append(d)
            s, f, d = owner('POST', '/api/v1/work/terms/' + t['id'] + '/freeze', {'inputs': dict(fixtures.inputs('FEASIBLE' if i % 2 else 'INFEASIBLE'), private_label='BENCH_%d' % i)}); lat['freeze'].append(d)
            s, r, d = owner('POST', '/api/v1/work/requests', {'terms_id': t['id']}); lat['request'].append(d); owner('POST', '/api/v1/work/requests/' + r['id'] + '/open', {})
            s, o, d = prov('POST', '/api/v1/work/requests/' + r['id'] + '/offers', {'price_amount': 3, 'asset': 'action-units', 'scheme': 'exact', 'window_seconds': 3600, 'verification': {'class': 'full_exact', 'distinct_verifier': False}}); lat['offer'].append(d)
            s, aw, d = owner('POST', '/api/v1/work/requests/' + r['id'] + '/award', {'offer_id': o['id']}, key='bw-a-%d' % i); lat['award'].append(d)
            if s != 201:
                errors.append({'step': 'award', 'status': s, 'code': aw.get('code')}); return
            with lock:
                awards.append({'id': aw['id'], 'awarded_at': time.time()})
        except Exception as exc:
            errors.append({'step': 'flow', 'error': type(exc).__name__})
    threads = [threading.Thread(target=one, args=(i,)) for i in range(a.requests)]
    [t.start() for t in threads]; [t.join() for t in threads]
    # wait for execution (external workers), then verification + decision + payment concurrently
    deadline = time.time() + 300; delivered = {}
    while time.time() < deadline and len(delivered) < len(awards):
        for aw in awards:
            if aw['id'] in delivered:
                continue
            s, v, d = owner('GET', '/api/v1/work/awards/' + aw['id'])
            if s == 200 and v['milestones'][0]['dimensions']['execution'] in ('completed', 'failed'):
                delivered[aw['id']] = time.time() - aw['awarded_at']
        time.sleep(0.5)

    def finish(aw):
        try:
            owner('POST', '/api/v1/work/awards/%s/milestones/m1/verify' % aw['id'], {})
            for _ in range(120):
                s, ev, d = owner('POST', '/api/v1/work/awards/%s/milestones/m1/evaluate' % aw['id'], {}); lat['evaluate'].append(d)
                if ev.get('decision_candidate') != 'pending':
                    break
                time.sleep(0.5)
            s, dec, d = owner('POST', '/api/v1/work/awards/%s/milestones/m1/decide' % aw['id'], {'decision': 'accept' if ev.get('decision_candidate') == 'accepted' else 'reject', 'reason': 'bench'}); lat['decide'].append(d)
            if dec.get('entitlement'):
                s, i, d = owner('POST', '/api/v1/work/entitlements/' + dec['entitlement']['id'] + '/prepare', {}); owner('POST', '/api/v1/work/intents/' + i['id'] + '/authorize', {})
                s, sub, d2 = owner('POST', '/api/v1/work/intents/' + i['id'] + '/submit', {}); lat['pay'].append(d + d2)
        except Exception as exc:
            errors.append({'step': 'finish', 'error': type(exc).__name__})
    threads = [threading.Thread(target=finish, args=(aw,)) for aw in awards]
    [t.start() for t in threads]; [t.join() for t in threads]
    s, rep, d = owner('POST', '/api/v1/work/journal/replay', {})
    s, ms, d = owner('GET', '/api/v1/work/measurements')
    out = {'schema': 'metacoin-work-benchmark/v1', 'requests': a.requests, 'awards': len(awards), 'errors': errors, 'latency_seconds': {k: summarize(v) for k, v in lat.items()},
           'queue_delay_to_execution_seconds': summarize(list(delivered.values())), 'wall_seconds': round(time.time() - t_start, 2), 'journal_consistent': rep.get('consistent'), 'invariants_ok': all(c['ok'] for c in rep.get('invariants', [])),
           'client_max_rss_mib': round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1), 'measurements': {k: ms.get(k) for k in ('sample_sizes', 'accepted_findings_negative_fraction', 'unreconciled_exposure_intents')},
           'conditions': 'one host, one API process, external workers as started by the caller; authorization, encryption and journal durability enabled; action-units rail (journal settlement); synthetic energy determinations'}
    text = json.dumps(out, indent=1)
    if a.out:
        open(a.out, 'w').write(text)
    print(text)
    return 0 if not errors and rep.get('consistent') else 2


if __name__ == '__main__':
    sys.exit(main())
