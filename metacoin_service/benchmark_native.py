"""Native path measurements (order §20) against a RUNNING API: transfer latency (p50/p95) under concurrency, native-rail
work settlement vs the application-journal rail on the same workload, storage growth, journal replay time, message bytes
of the inter-domain bundle, and recovery time (export recovery sweep). Verification and failed work are counted in the
economic summary. Local, same-operator measurements: nothing here is adoption, external demand, lunar performance or
production finality.

    PYTHONPATH=. .venv-service/bin/python -m metacoin_service.benchmark_native BASE OWNER_CRED [--transfers 200] [--concurrency 8] [--awards 6] [--out file]"""
import argparse
import json
import os
import statistics
import threading
import time
import urllib.error
import urllib.request

from experiments.work_contracts import fixtures


def client(base, token):
    def go(method, path, body=None, key=None):
        data = json.dumps(body).encode() if body is not None else None
        h = {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'}
        if key:
            h['Idempotency-Key'] = key
        req = urllib.request.Request(base + path, data=data, method=method, headers=h); t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return r.status, json.loads(r.read() or b'{}'), time.perf_counter() - t0
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b'{}'), time.perf_counter() - t0
    return go


def pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))] if xs else None


def main(argv=None):
    ap = argparse.ArgumentParser(); ap.add_argument('base'); ap.add_argument('owner_cred'); ap.add_argument('--transfers', type=int, default=200); ap.add_argument('--concurrency', type=int, default=8); ap.add_argument('--awards', type=int, default=6); ap.add_argument('--out')
    a = ap.parse_args(argv)
    go = client(a.base, json.load(open(a.owner_cred))['token'])
    out = {'schema': 'metacoin-native-benchmark/v0', 'workload': {'transfers': a.transfers, 'concurrency': a.concurrency, 'awards_per_rail': a.awards}, 'started_at': int(time.time())}
    st, dom, _ = go('GET', '/api/v1/native/domain')
    if st != 200:
        st, dom, _ = go('POST', '/api/v1/native/domain', {'domain_id': 'bench-dom'})
    asset = dom['default_asset']
    st, h0, _ = go('GET', '/api/health')
    go('PUT', '/api/v1/budgets/workspace', {'ceiling': 10 ** 6})                      # the application-journal baseline reserves under the workspace memo tree
    actors = {}
    for name in ('payer', 'payee', 'machine', 'supplier'):
        st, act, _ = go('POST', '/api/v1/native/actors', {'name': 'bench-' + name + '-' + str(int(time.time()))}); actors[name] = act
    go('POST', '/api/v1/native/fixtures/fund', {'account_id': actors['payer']['account_id'], 'amount': a.transfers * 2 + 1000})
    # 1. transfers under concurrency (one payer account, many request ids): latency and correctness
    lat, errors, lock = [], [], threading.Lock(); payer_tok = actors['payer']['credential']['token']; pgo = client(a.base, payer_tok)
    def worker(ix):
        for j in range(ix, a.transfers, a.concurrency):
            s, b, dt = pgo('POST', '/api/v1/native/transfers', {'to_account': actors['payee']['account_id'], 'amount': 1, 'request_id': 'bench-%d' % j})
            with lock:
                lat.append(dt)
                if s != 201:
                    errors.append(b.get('code'))
    t0 = time.perf_counter(); th = [threading.Thread(target=worker, args=(i,)) for i in range(a.concurrency)]
    [t.start() for t in th]; [t.join() for t in th]; elapsed = time.perf_counter() - t0
    st, payee, _ = go('GET', '/api/v1/native/accounts/' + actors['payee']['account_id'])
    out['transfers'] = {'count': a.transfers, 'elapsed_s': round(elapsed, 3), 'throughput_per_s': round(a.transfers / elapsed, 1), 'p50_s': round(pct(lat, 0.5), 4), 'p95_s': round(pct(lat, 0.95), 4), 'max_s': round(max(lat), 4), 'errors': errors[:5], 'payee_available_after': payee['available'], 'correct': payee['available'] == a.transfers and not errors}
    # 2. work settlement on the native rail vs the application journal (same determination task, same provider)
    def settle_cycle(asset_id, label):
        prov_name = 'bench-prov-' + label + '-' + str(int(time.time()))
        if asset_id == 'action-units':
            s, pv, _ = go('POST', '/api/v1/work/providers', {'name': prov_name, 'capabilities': {'kinds': ['energy_audit'], 'verification_classes': ['full_exact'], 'payment_schemes': ['exact']}, 'pay_to': 'provider:' + prov_name})
            ph = pv['credential']['token']
        else:
            s, pv, _ = go('POST', '/api/v1/work/providers', {'name': prov_name, 'principal_id': actors['machine']['principal_id'], 'capabilities': {'kinds': ['energy_audit'], 'verification_classes': ['full_exact'], 'payment_schemes': ['exact']}})
            ph = actors['machine']['credential']['token']
        pgo2 = client(a.base, ph); times = {'award': [], 'to_accepted': [], 'pay': []}; fails = 0; verification_cost = 0
        if asset_id != 'action-units':
            go('POST', '/api/v1/native/fixtures/fund', {'account_id': actors['payer']['account_id'], 'amount': 10 * a.awards})
        rq = pgo if asset_id != 'action-units' else go
        for i in range(a.awards):
            s, t, _ = rq('POST', '/api/v1/work/terms', {'template': 'determination', 'ceiling': 10, 'asset': asset_id})
            if s != 201:
                fails += 1; continue
            rq('POST', '/api/v1/work/terms/' + t['id'] + '/freeze', {'inputs': dict(fixtures.inputs('FEASIBLE'), private_label='BENCH_%d' % i)})
            s, r, _ = rq('POST', '/api/v1/work/requests', {'terms_id': t['id']}); rq('POST', '/api/v1/work/requests/' + r['id'] + '/open', {})
            s, o, _ = pgo2('POST', '/api/v1/work/requests/' + r['id'] + '/offers', {'price_amount': 10, 'asset': asset_id, 'scheme': 'exact', 'window_seconds': 3600, 'verification': {'class': 'full_exact'}})
            t1 = time.perf_counter(); s, aw, dt = rq('POST', '/api/v1/work/requests/' + r['id'] + '/award', {'offer_id': o['id']}); times['award'].append(dt)
            if s != 201:
                fails += 1; continue
            deadline = time.time() + 240; accepted = False
            while time.time() < deadline:
                s, v, _ = rq('GET', '/api/v1/work/awards/' + aw['id']); ms = v['milestones'][0]
                if ms['dimensions']['execution'] == 'completed' and ms['dimensions']['acceptance'] != 'accepted' and not ms.get('verification_requested'):
                    rq('POST', '/api/v1/work/awards/%s/milestones/m1/verify' % aw['id'], {}); ms['verification_requested'] = True; verification_cost += 1
                if ms['dimensions']['execution'] == 'completed':
                    s, ev, _ = rq('POST', '/api/v1/work/awards/%s/milestones/m1/evaluate' % aw['id'], {})
                    if ev.get('decision_candidate') == 'accepted':
                        rq('POST', '/api/v1/work/awards/%s/milestones/m1/decide' % aw['id'], {'decision': 'accept'}); accepted = True; break
                if ms['dimensions']['execution'] in ('failed',):
                    break
                time.sleep(0.5)
            times['to_accepted'].append(time.perf_counter() - t1)
            if not accepted:
                fails += 1; continue
            s, v, _ = rq('GET', '/api/v1/work/awards/' + aw['id']); ent = [e for e in v['entitlements'] if e['state'] == 'payable']
            for e in ent:
                t2 = time.perf_counter(); s, i, _ = rq('POST', '/api/v1/work/entitlements/' + e['id'] + '/prepare', {}); rq('POST', '/api/v1/work/intents/' + i['id'] + '/authorize', {}); s, sub, _ = rq('POST', '/api/v1/work/intents/' + i['id'] + '/submit', {}); times['pay'].append(time.perf_counter() - t2)
                if sub.get('state') != 'settled':
                    fails += 1
        return {'rail': label, 'awards': a.awards, 'failed_or_unaccepted': fails, 'verification_jobs': verification_cost, 'award_p50_s': round(pct(times['award'], 0.5) or 0, 4), 'award_p95_s': round(pct(times['award'], 0.95) or 0, 4), 'to_accepted_p50_s': round(pct(times['to_accepted'], 0.5) or 0, 3), 'pay_p50_s': round(pct(times['pay'], 0.5) or 0, 4), 'pay_p95_s': round(pct(times['pay'], 0.95) or 0, 4)}
    out['settlement'] = {'native': settle_cycle(asset, 'native'), 'application_journal': settle_cycle('action-units', 'application-journal')}
    # 3. replay time, storage, message bytes, recovery sweep
    t0 = time.perf_counter(); s, rep, _ = go('POST', '/api/v1/work/journal/replay', {}); out['journal_replay'] = {'seconds': round(time.perf_counter() - t0, 3), 'consistent': rep.get('consistent'), 'scopes': len(rep.get('scopes', {}))}
    s, ob, _ = go('GET', '/api/v1/native/transport/outbox?transport=bundle'); out['bundle_bytes_queued'] = len(json.dumps(ob).encode()); out['queued_envelopes'] = len(ob.get('items', []))
    t0 = time.perf_counter(); s, rec, _ = go('POST', '/api/v1/native/allocations/recover', {}); out['recovery_sweep'] = {'seconds': round(time.perf_counter() - t0, 4), 'resumed': len(rec.get('resumed', []))}
    s, h1, _ = go('GET', '/api/health'); out['health'] = {'revision': h1.get('revision'), 'queued': h1.get('queued')}
    s, au, _ = go('GET', '/api/v1/native/audit'); out['audit'] = {k: au.get(k) for k in ('conserved', 'total_held', 'fixture_funding')}
    home = os.environ.get('METACOIN_BENCH_HOME')
    if home and os.path.exists(os.path.join(home, 'service.sqlite')):
        out['storage_bytes'] = {'service_sqlite': os.path.getsize(os.path.join(home, 'service.sqlite')), 'wal': os.path.getsize(os.path.join(home, 'service.sqlite-wal')) if os.path.exists(os.path.join(home, 'service.sqlite-wal')) else 0}
    out['economic_summary'] = {'protocol_operations': a.transfers + 2 * a.awards * 4, 'accepted_work_products': 2 * a.awards - out['settlement']['native']['failed_or_unaccepted'] - out['settlement']['application_journal']['failed_or_unaccepted'], 'same_operator_transfer_volume': a.transfers, 'external_demand': 0,
                              'note': 'operation count is not accepted work; all volume is same-operator; verification jobs and unaccepted awards are included above'}
    out['finished_at'] = int(time.time())
    text = json.dumps(out, indent=1)
    if a.out:
        open(a.out, 'w').write(text)
    print(text); return 0


if __name__ == '__main__':
    raise SystemExit(main())
