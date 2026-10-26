"""Benchmarks of the independent machine network (order 2026-10-23 §18) against a running instance (and a peer domain for the
lineage part): shared-pool contention (racing reservations across two overlapping offers of one pool vs a plain offer), public
provider overhead (the standard-library client vs raw API calls for the same operations), signer round trips (prepare / sign /
submit stages vs development custody) and claim-lineage growth (partial retirements as the lot table grows, accounting latency,
export size). Same host, loopback, SQLite; numbers are relative, not capacity claims.

    PYTHONPATH=. python -m metacoin_service.benchmark_network BASE OWNER_CRED --signer URL --signer-token FILE --peer-base URL --peer-cred FILE --peer-domain dom-b --out benchmark-network.json"""
import argparse
import json
import os
import statistics
import sys
import threading
import time

from .benchmark_native import client, pct
from .tests.test_service import ROOT

sys.path.insert(0, str(ROOT / 'provider_client'))


def stage(xs):
    return {'p50_s': round(pct(xs, 0.5), 4), 'p95_s': round(pct(xs, 0.95), 4), 'mean_s': round(statistics.mean(xs), 4), 'n': len(xs)} if xs else None


def main(argv=None):
    ap = argparse.ArgumentParser(); ap.add_argument('base'); ap.add_argument('owner_cred'); ap.add_argument('--signer'); ap.add_argument('--signer-token'); ap.add_argument('--peer-base'); ap.add_argument('--peer-cred'); ap.add_argument('--peer-domain', default='dom-b')
    ap.add_argument('--transfers', type=int, default=60); ap.add_argument('--contenders', type=int, default=16); ap.add_argument('--lots', type=int, default=40); ap.add_argument('--out'); a = ap.parse_args(argv)
    go = client(a.base, json.load(open(a.owner_cred))['token']); out = {'schema': 'metacoin-network-benchmark/v1', 'started_at': int(time.time()), 'environment': {'python': sys.version.split()[0], 'host': 'single host, loopback, SQLite on local disk', 'cpus': os.cpu_count()}}
    st, dom, _ = go('GET', '/api/v1/native/domain'); asset = dom['default_asset']; tag = str(int(time.time()))
    def actor(name, fund=0):
        s, x, _ = go('POST', '/api/v1/native/actors', {'name': name + '-' + tag}); go('POST', '/api/v1/native/fixtures/fund', {'account_id': x['account_id'], 'amount': fund}) if fund else None; return x, client(a.base, x['credential']['token'])
    # 1. signer round trips ------------------------------------------------------------------------------------------------------------
    dev, dgo = actor('bn-dev', a.transfers + 10); payee, _ = actor('bn-payee'); lat_dev = []
    for i in range(a.transfers):
        s, b, dt = dgo('POST', '/api/v1/native/transfers', {'to_account': payee['account_id'], 'amount': 1, 'request_id': 'dev-%s-%d' % (tag, i)}); lat_dev.append(dt)
    sig = None
    if a.signer:
        from .native.signer import SignerClient
        sg = SignerClient(a.signer, a.signer_token); s, e = sg.call('POST', '/enroll', {'name': 'bn-ext-' + tag, 'ceiling': 10 ** 6})
        s, ext, _ = go('POST', '/api/v1/native/actors/enroll', {'name': 'bn-ext-' + tag, 'public_hex': e['public_hex'], 'key_id': e['key_id'], 'signer_id': e['signer_id']}); sg.call('POST', '/bind', {'actor': 'bn-ext-' + tag, 'account': ext['account_id']})
        go('POST', '/api/v1/native/fixtures/fund', {'account_id': ext['account_id'], 'amount': a.transfers + 10}); ego = client(a.base, ext['credential']['token']); stages = {'prepare': [], 'sign': [], 'submit': [], 'total': []}
        for i in range(a.transfers):
            t0 = time.perf_counter(); s, m, _ = ego('POST', '/api/v1/native/transfers/prepare', {'to_account': payee['account_id'], 'amount': 1, 'request_id': 'ext-%s-%d' % (tag, i)}); t1 = time.perf_counter(); s2, sgn = sg.sign('bn-ext-' + tag, m['message']); t2 = time.perf_counter()
            s3, r, _ = ego('POST', '/api/v1/native/transfers/submit', {'message': m['message'], 'signature_hex': sgn['signature_hex'], 'key_id': sgn['key_id']}); t3 = time.perf_counter()
            stages['prepare'].append(t1 - t0); stages['sign'].append(t2 - t1); stages['submit'].append(t3 - t2); stages['total'].append(t3 - t0)
        sig = {k: stage(v) for k, v in stages.items()}
        # context-bound signing requests (the agent loop): prepare a reservation intent with context, sign with context, submit
        sup0, sgo0 = actor('bn-ctx-sup'); t = int(time.time()); s, off, _ = sgo0('POST', '/api/v1/resources/offers', {'kind': 'contact', 'resource_id': 'ctx-' + tag, 'capacity': 100000, 'price_per_unit': 1, 'window_start': t - 1, 'window_end': t + 3600}); ctx_lat = []
        for i in range(min(a.transfers, 30)):
            t0 = time.perf_counter(); s, prep, _ = ego('POST', '/api/v1/resources/reservations/prepare', {'offer_id': off['id'], 'offer_version': off['version'], 'quantity': 1, 'request_id': 'ctx-%s-%d' % (tag, i)})
            if s != 200:
                break
            s2, sgn = sg.sign('bn-ext-' + tag, prep['message'], context=prep['context']); s3, r, _ = ego('POST', '/api/v1/resources/reservations', dict(prep['reserve_body'], native_signature={'message': prep['message'], 'signature_hex': sgn['signature_hex'], 'key_id': sgn['key_id']})); ctx_lat.append(time.perf_counter() - t0)
        sig['context_bound_reservation_total'] = stage(ctx_lat)
    out['signer_round_trips'] = {'samples': a.transfers, 'development_custody_transfer': stage(lat_dev), 'external_signer_transfer': sig, 'overhead_p50_s': round(sig['total']['p50_s'] - pct(lat_dev, 0.5), 4) if sig else None, 'note': 'an external-custody payment costs three round trips (prepare, sign at the signer, submit); the signer is a separate process on loopback'}
    # 2. shared-pool contention vs plain offer -------------------------------------------------------------------------------------------
    sup, sgo = actor('bn-sup'); t = int(time.time()) + 600
    s, pool, _ = sgo('POST', '/api/v1/resources/pools', {'kind': 'contact', 'label': 'bench', 'slot_unit': 'channel', 'units_per_slot_second': 1, 'resolution_seconds': 1, 'schedule': [{'start': t, 'end': t + 7200, 'capacity': 4}]})
    s, oa, _ = sgo('POST', '/api/v1/resources/offers', {'kind': 'contact', 'resource_id': 'pa-' + tag, 'capacity': 4 * 7200, 'price_per_unit': 1, 'window_start': t, 'window_end': t + 7200, 'pool_id': pool['id']}); s, ob, _ = sgo('POST', '/api/v1/resources/offers', {'kind': 'contact', 'resource_id': 'pb-' + tag, 'capacity': 4 * 7200, 'price_per_unit': 1, 'window_start': t + 1800, 'window_end': t + 9000, 'pool_id': pool['id']})
    s, plain, _ = sgo('POST', '/api/v1/resources/offers', {'kind': 'contact', 'resource_id': 'plain-' + tag, 'capacity': 4 * 600, 'price_per_unit': 1, 'window_start': t, 'window_end': t + 7200})
    buyers = [actor('bn-buyer-%d' % i, 5000)[1] for i in range(a.contenders)]
    def race(label, mk_body):
        res = []; lock = threading.Lock(); barrier = threading.Barrier(a.contenders)
        def contend(i):
            barrier.wait(); s, b, dt = buyers[i]('POST', '/api/v1/resources/reservations', mk_body(i))
            with lock:
                res.append((s, (b.get('detail') or {}).get('code') if s != 201 else 'ok', dt))
        th = [threading.Thread(target=contend, args=(i,)) for i in range(a.contenders)]; t0 = time.perf_counter(); [x.start() for x in th]; [x.join() for x in th]; el = time.perf_counter() - t0
        return {'label': label, 'contenders': a.contenders, 'accepted': sum(1 for s_, _, _ in res if s_ == 201), 'refused': {c: sum(1 for _, cc, _ in res if cc == c) for c in set(cc for _, cc, _ in res if cc != 'ok')}, 'elapsed_s': round(el, 3), 'latency': stage([dt for _, _, dt in res])}
    pool_race = race('shared pool, two overlapping offers, same 10-minute interval (capacity 4 channels)', lambda i: {'offer_id': (oa if i % 2 == 0 else ob)['id'], 'offer_version': 1, 'quantity': 600, 'request_id': 'pr-%s-%d' % (tag, i), 'interval_start': t + 1800, 'interval_end': t + 2400})
    plain_race = race('plain offer (implicit pool), capacity 4 x 600 units', lambda i: {'offer_id': plain['id'], 'offer_version': 1, 'quantity': 600, 'request_id': 'pl-%s-%d' % (tag, i)})
    s, pv, _ = go('GET', '/api/v1/resources/pools/' + pool['id'])
    out['pool_contention'] = {'shared_pool': pool_race, 'plain_offer': plain_race, 'pool_after': {'peak_slots_used': pv.get('peak_slots_used'), 'all_within_capacity': pv.get('all_within_capacity')}, 'note': 'both races admit exactly the pool capacity; the pool path adds the interval sweep inside the reservation transaction'}
    # 3. public provider overhead -------------------------------------------------------------------------------------------------------
    prov_cli = None
    try:
        from metacoin_provider.api import Api
        prov, pgo = actor('bn-prov'); cred = os.path.join(os.path.dirname(a.owner_cred), 'bn-prov-cred-%s.json' % tag); json.dump({'token': prov['credential']['token']}, open(cred, 'w')); os.chmod(cred, 0o600)
        api = Api(a.base, cred); raw_lat, cli_lat = [], []; t = int(time.time())
        for i in range(20):
            t0 = time.perf_counter(); pgo('POST', '/api/v1/resources/offers', {'kind': 'compute', 'resource_id': 'raw-%s-%d' % (tag, i), 'capacity': 1000, 'price_per_unit': 1, 'window_start': t - 1, 'window_end': t + 3600}); raw_lat.append(time.perf_counter() - t0)
            t0 = time.perf_counter(); api.post('/api/v1/resources/offers', {'kind': 'compute', 'resource_id': 'cli-%s-%d' % (tag, i), 'capacity': 1000, 'price_per_unit': 1, 'window_start': t - 1, 'window_end': t + 3600}, expect=(201,)); cli_lat.append(time.perf_counter() - t0)
        t0 = time.perf_counter(); hs = api.get('/api/v1/capabilities'); hs_dt = time.perf_counter() - t0
        prov_cli = {'raw_api_offer_publish': stage(raw_lat), 'client_offer_publish': stage(cli_lat), 'overhead_p50_s': round(pct(cli_lat, 0.5) - pct(raw_lat, 0.5), 4), 'capabilities_round_trip_s': round(hs_dt, 4), 'note': 'the standard-library client (urllib, strict JSON parsing, bounded sizes) vs the benchmark\'s raw urllib client; both on loopback'}
        os.unlink(cred)
    except Exception as exc:
        prov_cli = {'error': repr(exc)[:200]}
    out['provider_overhead'] = prov_cli
    # 4. claim lineage growth -------------------------------------------------------------------------------------------------------------
    if a.peer_base:
        from .native import courier
        pgo = client(a.peer_base, json.load(open(a.peer_cred))['token']); s, pdom, _ = pgo('GET', '/api/v1/native/domain'); s, bob, _ = pgo('POST', '/api/v1/native/actors', {'name': 'bn-bob-' + tag}); s, carol, _ = pgo('POST', '/api/v1/native/actors', {'name': 'bn-carol-' + tag}); bgo = client(a.peer_base, bob['credential']['token'])
        exp, xgo = actor('bn-exp', 20 * a.lots + 10)
        class Ep:
            def __init__(self, g):
                self.g = g
            def call(self, method, path, body=None, raw=None):
                s, b, _ = self.g(method, path, body); return s, b
        A_ep, B_ep = Ep(go), Ep(pgo); rows = []
        for i in range(a.lots):
            s, al, _ = xgo('POST', '/api/v1/native/allocations/export', {'destination_domain': pdom['id'], 'recipient_account': bob['account_id'], 'amount': 20, 'request_id': 'lot-%s-%d' % (tag, i)}); courier.push(A_ep, B_ep, pdom['id'])
            bgo('POST', '/api/v1/native/transfers', {'to_account': carol['account_id'], 'amount': 7, 'asset': asset, 'request_id': 'sp-%s-%d' % (tag, i)})
            t0 = time.perf_counter(); s, r, dt_ret = bgo('POST', '/api/v1/native/allocations/%s/return' % al['id'], {'amount': 13}); courier.push(B_ep, A_ep, dom['id']); dt_full = time.perf_counter() - t0
            s, acc, dt_acc = pgo('GET', '/api/v1/native/accounting?asset=' + asset); s, lots, dt_lots = pgo('GET', '/api/v1/native/accounts/%s/lots' % bob['account_id'])
            if i in (0, a.lots // 4, a.lots // 2, 3 * a.lots // 4, a.lots - 1):
                s, ex, dt_ex = pgo('GET', '/api/v1/native/export'); rows.append({'allocations': i + 1, 'lots_total': len(ex.get('claim_lots', [])), 'retire_request_s': round(dt_ret, 4), 'retire_to_release_s': round(dt_full, 4), 'accounting_s': round(dt_acc, 4), 'lots_view_s': round(dt_lots, 4), 'export_s': round(dt_ex, 4), 'export_bytes': len(json.dumps(ex)), 'partition_ok': acc.get('lots', {}).get('partition_ok')})
        s, auA, _ = go('GET', '/api/v1/native/audit?asset=' + asset); s, auB, _ = pgo('GET', '/api/v1/native/audit?asset=' + asset)
        out['lineage_growth'] = {'allocations': a.lots, 'rows': rows, 'conserved': auA.get('conserved') and auB.get('conserved'), 'note': 'each allocation of 20 is split 13/7 at the peer and 13 retired; latency per retirement and the accounting / export cost as the lot table grows'}
    out['finished_at'] = int(time.time())
    if a.out:
        with open(a.out, 'w') as f:
            json.dump(out, f, indent=1)
    print(json.dumps(out, indent=1)[:4000]); return 0


if __name__ == '__main__':
    raise SystemExit(main())
