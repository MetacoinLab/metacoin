"""Mission-economy measurements (order 2026-10-23 §15) against RUNNING processes: signer overhead (prepare → sign → submit vs a
development-custody transfer), reservation contention on one offer, offline message backlog recovery after a cut link, planner
runtime by problem size, and worker resource use over a burst of real lunar awards. Local, same-operator measurements on one
host: nothing here is adoption, external demand, lunar performance or production finality.

    PYTHONPATH=. .venv-service/bin/python -m metacoin_service.benchmark_mission BASE OWNER_CRED --signer URL --signer-token FILE [--peer-base URL --peer-cred FILE --peer-domain dom-b] [--worker-pid N] [--out file]"""
import argparse
import json
import os
import statistics
import threading
import time
import urllib.error
import urllib.request

from .benchmark_native import client, pct
from .native import planner as PL
from .native.signer import SignerClient


def proc_stats(pid):
    try:
        st = open('/proc/%d/stat' % pid).read().split(')')[-1].split(); hz = os.sysconf('SC_CLK_TCK'); cpu = (int(st[11]) + int(st[12])) / hz
        status = {l.split(':')[0]: l.split(':', 1)[1].strip() for l in open('/proc/%d/status' % pid)}
        return {'cpu_s': round(cpu, 3), 'rss_kb': int(status.get('VmRSS', '0 kB').split()[0]), 'peak_rss_kb': int(status.get('VmHWM', '0 kB').split()[0])}
    except Exception as exc:
        return {'error': type(exc).__name__}


def main(argv=None):
    ap = argparse.ArgumentParser(); ap.add_argument('base'); ap.add_argument('owner_cred'); ap.add_argument('--signer'); ap.add_argument('--signer-token'); ap.add_argument('--peer-base'); ap.add_argument('--peer-cred'); ap.add_argument('--peer-domain', default='dom-b')
    ap.add_argument('--worker-pid', type=int); ap.add_argument('--transfers', type=int, default=60); ap.add_argument('--contenders', type=int, default=16); ap.add_argument('--backlog', type=int, default=40); ap.add_argument('--awards', type=int, default=4); ap.add_argument('--out'); a = ap.parse_args(argv)
    go = client(a.base, json.load(open(a.owner_cred))['token']); out = {'schema': 'metacoin-mission-benchmark/v1', 'started_at': int(time.time()), 'environment': {'python': os.sys.version.split()[0], 'host': 'single host, loopback, SQLite on local disk', 'cpus': os.cpu_count()}}
    st, dom, _ = go('GET', '/api/v1/native/domain'); asset = dom['default_asset']; tag = str(int(time.time()))
    # 1. signer overhead -----------------------------------------------------------------------------------------------------------------
    st, dev, _ = go('POST', '/api/v1/native/actors', {'name': 'bench-dev-' + tag}); go('POST', '/api/v1/native/fixtures/fund', {'account_id': dev['account_id'], 'amount': a.transfers + 10})
    st, payee, _ = go('POST', '/api/v1/native/actors', {'name': 'bench-payee-' + tag}); dgo = client(a.base, dev['credential']['token']); lat_dev = []
    for i in range(a.transfers):
        s, b, dt = dgo('POST', '/api/v1/native/transfers', {'to_account': payee['account_id'], 'amount': 1, 'request_id': 'dev-%s-%d' % (tag, i)}); lat_dev.append(dt)
    sig = None
    if a.signer:
        sg = SignerClient(a.signer, a.signer_token); s, e = sg.call('POST', '/enroll', {'name': 'bench-ext-' + tag, 'ceiling': a.transfers + 10})
        s, ext, _ = go('POST', '/api/v1/native/actors/enroll', {'name': 'bench-ext-' + tag, 'public_hex': e['public_hex'], 'key_id': e['key_id'], 'signer_id': e['signer_id']}); sg.call('POST', '/bind', {'actor': 'bench-ext-' + tag, 'account': ext['account_id']})
        go('POST', '/api/v1/native/fixtures/fund', {'account_id': ext['account_id'], 'amount': a.transfers + 10}); ego = client(a.base, ext['credential']['token']); stages = {'prepare': [], 'sign': [], 'submit': [], 'total': []}
        for i in range(a.transfers):
            t0 = time.perf_counter(); s, m, d1 = ego('POST', '/api/v1/native/transfers/prepare', {'to_account': payee['account_id'], 'amount': 1, 'request_id': 'ext-%s-%d' % (tag, i)}); t1 = time.perf_counter(); s2, sgn = sg.sign('bench-ext-' + tag, m['message']); t2 = time.perf_counter()
            s3, r, d3 = ego('POST', '/api/v1/native/transfers/submit', {'message': m['message'], 'signature_hex': sgn['signature_hex'], 'key_id': sgn['key_id']}); t3 = time.perf_counter()
            stages['prepare'].append(t1 - t0); stages['sign'].append(t2 - t1); stages['submit'].append(t3 - t2); stages['total'].append(t3 - t0)
        sig = {k: {'p50_s': round(pct(v, 0.5), 4), 'p95_s': round(pct(v, 0.95), 4), 'mean_s': round(statistics.mean(v), 4)} for k, v in stages.items()}
    out['signer_overhead'] = {'samples': a.transfers, 'development_custody_transfer': {'p50_s': round(pct(lat_dev, 0.5), 4), 'p95_s': round(pct(lat_dev, 0.95), 4), 'mean_s': round(statistics.mean(lat_dev), 4)}, 'external_signer_transfer': sig, 'overhead_p50_s': round(sig['total']['p50_s'] - pct(lat_dev, 0.5), 4) if sig else None, 'note': 'three round trips (prepare, sign, submit) on loopback; the signer records exposure durably before answering'}
    # 2. reservation contention ------------------------------------------------------------------------------------------------------------
    st, sup, _ = go('POST', '/api/v1/native/actors', {'name': 'bench-sup-' + tag}); sgo = client(a.base, sup['credential']['token']); t = int(time.time())
    s, offer, _ = sgo('POST', '/api/v1/resources/offers', {'kind': 'contact', 'resource_id': 'bench-' + tag, 'capacity': a.contenders * 5 // 2, 'price_per_unit': 1, 'window_start': t - 1, 'window_end': t + 3600})
    buyers = []
    for i in range(a.contenders):
        s, b, _ = go('POST', '/api/v1/native/actors', {'name': 'bench-buyer-%s-%d' % (tag, i)}); go('POST', '/api/v1/native/fixtures/fund', {'account_id': b['account_id'], 'amount': 10}); buyers.append(client(a.base, b['credential']['token']))
    res = []; lock = threading.Lock(); barrier = threading.Barrier(a.contenders)
    def contend(i):
        barrier.wait(); s, b, dt = buyers[i]('POST', '/api/v1/resources/reservations', {'offer_id': offer['id'], 'offer_version': offer['version'], 'quantity': 5, 'request_id': 'c-%d' % i})
        with lock:
            res.append((s, (b.get('detail') or {}).get('code') if s != 201 else 'ok', dt))
    th = [threading.Thread(target=contend, args=(i,)) for i in range(a.contenders)]; t0 = time.perf_counter(); [x.start() for x in th]; [x.join() for x in th]; el = time.perf_counter() - t0
    s, ov, _ = go('GET', '/api/v1/resources/offers/' + offer['id'])
    out['reservation_contention'] = {'contenders': a.contenders, 'quantity_each': 5, 'capacity': offer['capacity'], 'accepted': sum(1 for s_, _, _ in res if s_ == 201), 'refused': {c: sum(1 for _, cc, _ in res if cc == c) for c in set(cc for _, cc, _ in res if cc != 'ok')}, 'elapsed_s': round(el, 3), 'p50_s': round(pct([d for _, _, d in res], 0.5), 4), 'p95_s': round(pct([d for _, _, d in res], 0.95), 4), 'reserved_after': ov['reserved'], 'available_after': ov['available'], 'consistent': ov['reserved'] + ov['available'] == offer['capacity'] and ov['reserved'] == 5 * sum(1 for s_, _, _ in res if s_ == 201)}
    # 3. offline backlog recovery ------------------------------------------------------------------------------------------------------------
    if a.peer_base:
        from .native import courier
        pgo = client(a.peer_base, json.load(open(a.peer_cred))['token']); s, pdom, _ = pgo('GET', '/api/v1/native/domain'); s, pact, _ = pgo('POST', '/api/v1/native/actors', {'name': 'bench-peer-' + tag})
        st, exp, _ = go('POST', '/api/v1/native/actors', {'name': 'bench-exporter-' + tag}); go('POST', '/api/v1/native/fixtures/fund', {'account_id': exp['account_id'], 'amount': a.backlog + 5}); xgo = client(a.base, exp['credential']['token'])
        go('POST', '/api/v1/native/domain/connectivity', {'state': 'cut'}); t0 = time.perf_counter(); ok_exports = 0
        for i in range(a.backlog):
            s, r, _ = xgo('POST', '/api/v1/native/allocations/export', {'destination_domain': pdom['id'], 'recipient_account': pact['account_id'], 'amount': 1, 'request_id': 'bl-%s-%d' % (tag, i)}); ok_exports += s == 201
        queued_s = time.perf_counter() - t0; go('POST', '/api/v1/native/domain/connectivity', {'state': 'online'})
        class Ep:
            def __init__(self, g):
                self.g = g
            def call(self, method, path, body=None, raw=None):
                s, b, _ = self.g(method, path, body); return s, b
        t1 = time.perf_counter(); delivered = 0; rounds = 0
        while rounds < 50:
            r = courier.push(Ep(go), Ep(pgo), pdom['id']); rounds += 1; delivered += r.get('delivered', 0) if r.get('ok') else 0
            if r.get('ok') and r.get('delivered', 0) == 0:
                break
        rec = time.perf_counter() - t1; s, pb, _ = pgo('GET', '/api/v1/native/accounts/' + pact['account_id'] + '?asset=' + asset)
        out['offline_backlog_recovery'] = {'queued_exports': ok_exports, 'queue_time_s': round(queued_s, 3), 'recovery_time_s': round(rec, 3), 'courier_rounds': rounds, 'items_per_s': round(ok_exports / rec, 1) if rec else None, 'credited_at_peer': pb.get('available'), 'complete': pb.get('available') == ok_exports}
    # 4. planner runtime ---------------------------------------------------------------------------------------------------------------------
    import random
    sizes = []
    for n_tasks, n_offers in ((6, 8), (10, 10), (14, 12), (20, 12)):
        r = random.Random(n_tasks); tasks = [{'id': 't%d' % i, 'kind': r.choice(['compute', 'storage', 'contact']), 'quantity': r.randint(1, 6), 'duration_slots': r.randint(1, 3), 'depends': [('t%d' % j) for j in range(i) if r.random() < 0.3]} for i in range(n_tasks)]
        offers = [{'id': 'o%d' % i, 'kind': ['compute', 'storage', 'contact'][i % 3], 'available': r.randint(10, 30), 'price_per_unit': r.randint(1, 6), 'energy_mwh_per_unit': r.randint(0, 2), 'window_start': r.randint(0, 3) * 60, 'window_end': r.randint(6, 12) * 60} for i in range(n_offers)]
        prob = {'horizon': {'start': 0, 'end': 720, 'slot_seconds': 60}, 'tasks': tasks, 'offers': offers, 'budget': 10 ** 6, 'objective': 'lowest_cost'}
        row = {'tasks': n_tasks, 'offers': n_offers}
        for planner in ('baseline', 'improved') + (('oracle',) if n_tasks <= 6 else ()):
            try:
                res_ = PL.solve(prob, planner, timeout_ms=5000); row[planner] = {'status': res_['status'], 'runtime_ms': res_['runtime_ms'], 'nodes': res_.get('nodes'), 'objective': (res_.get('incumbent') or {}).get('objective_value')}
            except Exception as exc:
                row[planner] = {'error': type(exc).__name__}
        s, r2, dt = go('POST', '/api/v1/plans/solve', {'problem': prob, 'planner': 'improved', 'timeout_ms': 5000}); row['api_round_trip_s'] = round(dt, 4); sizes.append(row)
    out['planner_runtime'] = {'sizes': sizes, 'note': 'in-process solve and one API round trip per size; the improved planner is exhaustive under its bound (status optimal) or reports feasible_timeout'}
    # 5. worker resource use over a burst of real lunar awards ------------------------------------------------------------------------------
    from .lunar.__main__ import example
    st, req, _ = go('POST', '/api/v1/native/actors', {'name': 'bench-req-' + tag}); go('POST', '/api/v1/native/fixtures/fund', {'account_id': req['account_id'], 'amount': 100 * a.awards}); rgo = client(a.base, req['credential']['token'])
    st, mach, _ = go('POST', '/api/v1/native/actors', {'name': 'bench-mach-' + tag}); go('POST', '/api/v1/work/providers', {'name': 'bench-mach-' + tag, 'principal_id': mach['principal_id'], 'capabilities': {'kinds': ['terrain_tile', 'route_feasibility'], 'verification_classes': ['analytical'], 'payment_schemes': ['exact']}}); mgo = client(a.base, mach['credential']['token'])
    before = proc_stats(a.worker_pid) if a.worker_pid else None; t0 = time.perf_counter(); awards = []
    for i in range(a.awards):
        s, tm, _ = rgo('POST', '/api/v1/work/terms', {'template': 'lunar_route_feasibility', 'ceiling': 10, 'amount': 10, 'asset': asset}); rgo('POST', '/api/v1/work/terms/' + tm['id'], {'terms': {'payment': dict(tm['terms']['payment'], scheme='exact', funding='requester'), 'eligibility': dict(tm['terms']['eligibility'], payment_schemes=['exact'])}})
        inp = example('route'); inp['private_label'] = 'BENCH%d' % i; rgo('POST', '/api/v1/work/terms/%s/freeze' % tm['id'], {'inputs': inp}); s, rq, _ = rgo('POST', '/api/v1/work/requests', {'terms_id': tm['id']}); rgo('POST', '/api/v1/work/requests/%s/open' % rq['id'], {})
        s, of, _ = mgo('POST', '/api/v1/work/requests/%s/offers' % rq['id'], {'price_amount': 10, 'asset': asset, 'scheme': 'exact', 'window_seconds': 3600, 'verification': {'class': 'analytical'}}); s, aw, _ = rgo('POST', '/api/v1/work/requests/%s/award' % rq['id'], {'offer_id': of['id']}); awards.append(aw.get('id'))
    done = 0; deadline = time.time() + 600
    while time.time() < deadline and done < len(awards):
        done = 0
        for aid in awards:
            s, v, _ = rgo('GET', '/api/v1/work/awards/' + aid); done += v['milestones'][0]['dimensions']['execution'] in ('completed', 'failed')
        if done < len(awards):
            time.sleep(1)
    el = time.perf_counter() - t0; after = proc_stats(a.worker_pid) if a.worker_pid else None
    out['worker_resource_use'] = {'awards': a.awards, 'completed': done, 'elapsed_s': round(el, 2), 'jobs_per_s': round(done / el, 3) if el else None, 'worker_before': before, 'worker_after': after, 'worker_cpu_delta_s': round(after['cpu_s'] - before['cpu_s'], 3) if before and after and 'cpu_s' in before and 'cpu_s' in after else None, 'note': 'one background worker process; CPU from /proc stat, RSS from /proc status; lunar route search on the pinned tile'}
    out['finished_at'] = int(time.time())
    if a.out:
        with open(a.out, 'w') as f:
            json.dump(out, f, indent=1)
    print(json.dumps(out, indent=1)[:3000]); return 0


if __name__ == '__main__':
    raise SystemExit(main())
