"""Sustained mixed workload of the independent machine network (order 2026-10-23 §18): at least 30 minutes of bounded mixed
mission work through real processes with a fixed seed, real contention on one shared pool behind two overlapping offers,
queue pressure from a work loop paid natively, signed payments through an external signer, cross-domain exports with partial
retirement of claim lots, periodic partitions (link cut / online), signer and API restarts, a hard timeout and durable progress
counters. Nothing sleeps idly: every loop keeps issuing requests; refusals are classified as expected (capacity, draining,
signature pending, quarantine) or unexpected. At the end: conservation on both domains, duplicate-settlement census, pool
within capacity, lot partition, tail latencies, backlog age, memory of the API process, recovery times.

    METACOIN_X402_DISABLED=1 PYTHONPATH=. .venv-service/bin/python -m metacoin_service.endurance_network --seconds 1800 --seed 7 --out endurance-network.json --progress progress.json"""
import argparse
import json
import os
import random
import sqlite3
import threading
import time

from .native import courier
from .tests.journeys_mission import MDom, SignerProcess, WitnessProcess
from .tests.test_work_terms import energy_inputs

EXPECTED = {'pool_capacity_exceeded', 'capacity_exhausted', 'provider_draining', 'stale_quote', 'domain_quarantined', 'connectivity_cut', 'signature_required', 'native_signature_required', 'insufficient_available', 'BUDGET_EXHAUSTED', 'window_closed', 'retire_exceeds_owned', 'missing_lineage', 'state', 'nonce_stale', 'signer_exposure_ceiling'}


def rss_kb(pid):
    try:
        for line in open('/proc/%d/status' % pid):
            if line.startswith('VmRSS'):
                return int(line.split()[1])
    except Exception:
        return None


def main(argv=None):
    ap = argparse.ArgumentParser(); ap.add_argument('--seconds', type=int, default=1800); ap.add_argument('--seed', type=int, default=7); ap.add_argument('--out'); ap.add_argument('--progress'); ap.add_argument('--hard-timeout', type=int, default=0); a = ap.parse_args(argv)
    os.environ['METACOIN_X402_DISABLED'] = '1'; rnd = random.Random(a.seed); hard = a.hard_timeout or a.seconds + 900; t_begin = time.time()
    W = WitnessProcess(); sgA = SignerProcess(domains=('end-a',)); sgB = SignerProcess(domains=('end-b',))
    wenv = {'METACOIN_WITNESS_URL': W.base, 'METACOIN_WITNESS_TOKEN_FILE': str(W.home / 'token'), 'METACOIN_WITNESS_KEY_ID': W.identity['key_id']}
    A = MDom('end-a', dict(wenv, METACOIN_SIGNER_URL=sgA.base, METACOIN_SIGNER_TOKEN_FILE=str(sgA.home / 'token'))); B = MDom('end-b', dict(wenv, METACOIN_SIGNER_URL=sgB.base, METACOIN_SIGNER_TOKEN_FILE=str(sgB.home / 'token'))); A.pin(B); B.pin(A)
    for d in (A, B):
        v = d.domain(); W.client.call('POST', '/pin', {'domain': d.id, 'key_id': v['authority']['key_id'], 'public_hex': v['authority']['public_hex']})
    A.start_worker('w2-end-a')
    keys = ('transfers', 'signed_transfers', 'pool_reservations', 'work', 'cross_domain', 'lots'); stats = {k: {'ok': 0, 'expected': {}, 'unexpected': {}, 'latency': []} for k in keys}; lock = threading.Lock(); stop = threading.Event(); events = []
    progress = {'started_at': int(t_begin), 'seed': a.seed, 'seconds_planned': a.seconds, 'ticks': []}
    restart_marks = []; samples = []
    def count(k, ok, code=None, dt=None, body=None):
        with lock:
            s = stats[k]
            if ok:
                s['ok'] += 1
            else:
                near_restart = any(abs(time.time() - m) <= 15 for m in restart_marks); conn = 'ConnectError' in (code or '') or 'ReadError' in (code or '') or 'RemoteProtocolError' in (code or '')
                bucket = 'expected' if (code in EXPECTED or (code or '').startswith('transport:') or (conn and near_restart)) else 'unexpected'; key = (code + ' (during restart)') if (conn and near_restart and not (code or '').startswith('transport:')) else (code or 'unknown')
                s[bucket][key] = s[bucket].get(key, 0) + 1
                if bucket == 'unexpected' and len(samples) < 12:
                    samples.append({'at_s': round(time.time() - t_begin, 1), 'loop': k, 'code': code, 'body': (json.dumps(body, default=str)[:400] if body is not None else None)})
            if dt is not None:
                s['latency'].append(dt)
    def safe(fn, k):
        t0 = time.perf_counter()
        try:
            st, r = fn(); ok = st in (200, 201); code = None
            if not ok:
                d = r.get('detail') if isinstance(r, dict) else None; code = (d.get('code') if isinstance(d, dict) else None) or (r.get('code') if isinstance(r, dict) else None) or 'HTTP_%s' % st
            count(k, ok, code, time.perf_counter() - t0, None if ok else r); return st, r
        except Exception as exc:
            count(k, False, 'transport:' + type(exc).__name__, time.perf_counter() - t0); time.sleep(0.3); return 0, {}
    def mark(ev, **kw):
        with lock:
            events.append(dict({'at_s': round(time.time() - t_begin, 1), 'event': ev}, **kw))
    try:
        payer = A.actor('payer', 20000); payee = A.actor('payee'); sup = A.actor('sup'); A.provider('sup', kinds=('energy_audit',)); req = A.actor('req', 20000); mach = A.actor('mach'); A.provider('mach', kinds=('energy_audit',)); exp = A.actor('exp', 5000); bob = B.actor('bob'); carol = B.actor('carol')
        ext = A.enroll_external(sgA, 'ext-payer', 10 ** 9); A.fund('ext-payer', 20000)
        buyers = [A.actor('buyer%d' % i, 100000) for i in range(4)]; t = int(time.time())
        pool = A.call('post', '/api/v1/resources/pools', role='sup', json={'kind': 'contact', 'label': 'shared antenna', 'slot_unit': 'channel', 'units_per_slot_second': 1, 'resolution_seconds': 1, 'schedule': [{'start': t - 60, 'end': t + a.seconds + 7200, 'capacity': 2}]})[1]
        offers = []
        for i in range(2):
            st, o = A.call('post', '/api/v1/resources/offers', role='sup', json={'kind': 'contact', 'resource_id': 'end-%d' % i, 'capacity': 2 * (a.seconds + 7260), 'price_per_unit': 1, 'window_start': t - 60, 'window_end': t + a.seconds + 7200, 'pool_id': pool['id']}); assert st == 201, o; offers.append(o)
        funding = A.audit()['fixture_funding']
        def transfers():
            i = 0
            while not stop.is_set():
                rid = 'e-t-%d' % i; safe(lambda: A.call('post', '/api/v1/native/transfers', role='payer', json={'to_account': payee['account'], 'amount': 1, 'request_id': rid}), 'transfers'); i += 1
        def signed():
            i = 0
            while not stop.is_set():
                rid = 'e-s-%d' % i; i += 1
                def go():
                    st, m = A.call('post', '/api/v1/native/transfers/prepare', role='ext-payer', json={'to_account': payee['account'], 'amount': 1, 'request_id': rid})
                    if st != 200:
                        return st, m
                    s, sig = sgA.client.sign('ext-payer', m['message'])
                    if s != 200:
                        return s, sig
                    return A.submit('ext-payer', m['message'], sig['signature_hex'], sig['key_id'])
                safe(go, 'signed_transfers'); time.sleep(0.05)
        def reservations(bi):
            i = 0; who = 'buyer%d' % bi; r_ = random.Random(a.seed * 100 + bi)
            while not stop.is_set():
                rid = 'e-r-%d-%d' % (bi, i); i += 1; o = offers[r_.randint(0, 1)]; now_ = int(time.time()); s0 = now_ + r_.randint(0, 30); dur = r_.randint(5, 40)
                st, r = safe(lambda: A.call('post', '/api/v1/resources/reservations', role=who, json={'offer_id': o['id'], 'offer_version': o['version'], 'quantity': dur, 'request_id': rid, 'interval_start': s0, 'interval_end': s0 + dur}), 'pool_reservations')
                if st == 201:
                    if i % 3 == 0:
                        safe(lambda: A.res_action(who, r['id'], 'cancel'), 'pool_reservations')
                    else:
                        safe(lambda: A.res_action('sup', r['id'], 'activate'), 'pool_reservations'); safe(lambda: A.res_action('sup', r['id'], 'deliver', {'bytes': dur * 1024}), 'pool_reservations'); safe(lambda: A.res_action('sup', r['id'], 'settle'), 'pool_reservations')
                time.sleep(0.05)
        def work():
            i = 0
            while not stop.is_set():
                t0 = time.perf_counter()
                try:
                    tt, rr, oo, (sa, aw) = A.award('req', 'mach', 'determination', energy_inputs('FEASIBLE' if i % 2 else 'INFEASIBLE'), 3)
                    if sa != 201:
                        d = aw.get('detail') if isinstance(aw, dict) else None; count('work', False, (d.get('code') if isinstance(d, dict) else None) or aw.get('code', 'award_%s' % sa), None, aw); time.sleep(1); continue
                    v, d = A.deliver_accept(aw['id'], 'req', timeout=180); paid = A.pay_all(aw['id'], 'req') if d else {}
                    count('work', bool(d) and d.get('decision') == 'accepted' and paid.get('provider', {}).get('rail') == 'native', None if d else 'execution_incomplete', time.perf_counter() - t0)
                except Exception as exc:
                    count('work', False, 'exception:' + type(exc).__name__, time.perf_counter() - t0); time.sleep(1)
                i += 1
        def cross():
            i = 0
            while not stop.is_set():
                rid = 'e-x-%d' % i; st, r = safe(lambda: A.call('post', '/api/v1/native/allocations/export', role='exp', json={'destination_domain': 'end-b', 'recipient_account': bob['account'], 'amount': 4, 'request_id': rid}), 'cross_domain'); i += 1
                try:
                    out = courier.push(A.ep(), B.ep(), 'end-b')
                    if st == 201 and out.get('ok'):
                        B.call('post', '/api/v1/native/transfers', role='bob', json={'to_account': carol['account'], 'amount': 1, 'asset': A.asset, 'request_id': 'e-l-%d' % i})            # the claim splits: 3 stay with bob, 1 goes to carol
                        s2, r2 = safe(lambda: B.call('post', '/api/v1/native/allocations/%s/return' % r['id'], role='bob', json={'amount': 3}), 'lots'); courier.push(B.ep(), A.ep(), 'end-a')
                    elif not out.get('ok'):
                        count('cross_domain', False, 'transport:' + str(out.get('stage')))
                except Exception as exc:
                    count('cross_domain', False, 'courier:' + type(exc).__name__)
                time.sleep(0.2)
        threads = [threading.Thread(target=transfers, daemon=True), threading.Thread(target=signed, daemon=True), threading.Thread(target=work, daemon=True), threading.Thread(target=cross, daemon=True)] + [threading.Thread(target=reservations, args=(i,), daemon=True) for i in range(4)]
        t_start = time.time(); [x.start() for x in threads]; restarts = {'api': 0, 'signer': 0}; partitions = 0; recovery = []
        # disruption schedule (fixed by the seed): every ~5 min a partition of 20 s; signer restart at 1/3; API restart at 1/2 and 5/6
        plan = sorted([('partition', x) for x in range(240, a.seconds - 60, 300)] + [('signer', a.seconds // 3), ('api', a.seconds // 2), ('api', a.seconds * 5 // 6)], key=lambda x: x[1]); next_tick = 60
        while time.time() - t_start < a.seconds and time.time() - t_begin < hard:
            el = time.time() - t_start
            if plan and el >= plan[0][1]:
                kind, _ = plan.pop(0); t0 = time.time()
                if kind == 'partition':
                    A.connectivity('cut'); mark('partition_cut'); time.sleep(20); A.connectivity('online'); mark('partition_online'); partitions += 1
                elif kind == 'signer':
                    restart_marks.append(time.time()); sgA.restart(); restarts['signer'] += 1; mark('signer_restarted', seconds=round(time.time() - t0, 2)); recovery.append({'signer': round(time.time() - t0, 2)})
                elif kind == 'api':
                    restart_marks.append(time.time()); A.restart_api(); restart_marks.append(time.time()); restarts['api'] += 1; dt = round(time.time() - t0, 2); mark('api_restarted', seconds=dt); recovery.append({'api': dt})
            if el >= next_tick:
                with lock:
                    tick = {'at_s': round(el), 'ok': {k: stats[k]['ok'] for k in keys}, 'unexpected': {k: sum(stats[k]['unexpected'].values()) for k in keys}, 'api_rss_kb': rss_kb(A.api.pid)}
                progress['ticks'].append(tick)
                if a.progress:
                    with open(a.progress, 'w') as f:
                        json.dump(progress, f)
                next_tick += 60
            time.sleep(0.5)
        stop.set(); [x.join(timeout=240) for x in threads]; elapsed = round(time.time() - t_start, 1); time.sleep(3)
        for _ in range(3):                                                           # flush in-flight cross-domain messages so the final accounting is over a quiescent state
            try:
                courier.push(A.ep(), B.ep(), 'end-b'); courier.push(B.ep(), A.ep(), 'end-a')
            except Exception:
                pass
        # final checks
        auA = A.audit(); auB = B.audit(A.asset); acc = A.call('get', '/api/v1/native/accounting')[1]; accB = B.call('get', '/api/v1/native/accounting', params={'asset': A.asset})[1]; pv = A.call('get', '/api/v1/resources/pools/' + pool['id'])[1]
        db = sqlite3.connect('file:%s?mode=ro' % A.inst.settings.db_path, uri=True)
        dup_intents = db.execute("SELECT actor_account, request_id, COUNT(*) c FROM native_intents WHERE state='settled' GROUP BY actor_account, request_id HAVING c > 1").fetchall()
        dup_res = db.execute("SELECT buyer_account, request_id, COUNT(*) c FROM resource_reservations GROUP BY buyer_account, request_id HAVING c > 1").fetchall()
        res_states = {r[0]: r[1] for r in db.execute('SELECT state, COUNT(*) FROM resource_reservations GROUP BY state').fetchall()}; jobs = {r[0]: r[1] for r in db.execute('SELECT state, COUNT(*) FROM jobs GROUP BY state').fetchall()}
        oldest_queued = db.execute("SELECT MIN(created_at) FROM jobs WHERE state IN ('queued','leased','running')").fetchone()[0]; db.close()
        held_total = auA['total_held'] + auA['backing_locked_for_exports']; conserved = auA['conserved'] and held_total == auA['fixture_funding'] and auB['claims_backed_elsewhere'] == auB['total_held'] and all(acc['checks'].values()) and all(accB['checks'].values()) and accB['lots']['partition_ok']
        pct = lambda xs, q: (sorted(xs)[min(len(xs) - 1, int(q * len(xs)))] if xs else None); unexpected = sum(sum(s['unexpected'].values()) for s in stats.values()); all_lat = [x for s in stats.values() for x in s['latency']]
        try:
            A.api_log.flush(); api_tail = open(A.api_log.name, 'rb').read()[-6000:].decode('utf-8', 'replace')
        except Exception:
            api_tail = None
        out = {'schema': 'metacoin-endurance-network/v1', 'seconds': elapsed, 'seed': a.seed, 'restarts': restarts, 'partitions': partitions, 'events': events, 'ticks': progress['ticks'], 'unexpected_samples': samples, 'api_stderr_tail': api_tail,
               'loops': {k: {'ok': v['ok'], 'expected_refusals': v['expected'], 'unexpected_failures': v['unexpected'], 'per_s': round(v['ok'] / elapsed, 2), 'p50_s': round(pct(v['latency'], 0.5), 4) if v['latency'] else None, 'p95_s': round(pct(v['latency'], 0.95), 4) if v['latency'] else None, 'p99_s': round(pct(v['latency'], 0.99), 4) if v['latency'] else None} for k, v in stats.items()},
               'summary': {'completed': sum(s['ok'] for s in stats.values()), 'refused_expected': sum(sum(s['expected'].values()) for s in stats.values()), 'unexpected_failures': unexpected, 'duplicate_settlements': len(dup_intents) + len(dup_res), 'conserved': conserved, 'latency_p99_s': round(pct(all_lat, 0.99), 4) if all_lat else None, 'recovery_seconds': recovery, 'backlog_age_s': (int(time.time()) - oldest_queued) if oldest_queued else 0, 'api_rss_kb_last': rss_kb(A.api.pid)},
               'final': {'audit_a': {k: auA[k] for k in ('total_held', 'fixture_funding', 'backing_locked_for_exports', 'claims_backed_elsewhere', 'conserved')}, 'audit_b_asset_a': {k: auB[k] for k in ('total_held', 'claims_backed_elsewhere', 'conserved')}, 'accounting_checks_a': acc['checks'], 'accounting_b': {'checks': accB['checks'], 'lots': accB['lots']}, 'pool': {'peak_slots_used': pv['peak_slots_used'], 'all_within_capacity': pv['all_within_capacity'], 'commitments': len(pv['commitments'])}, 'reservation_states': res_states, 'jobs': jobs, 'duplicate_settled_intents': dup_intents, 'duplicate_reservations': dup_res},
               'time_accounting': {'wall_clock_s': elapsed, 'planned_contact_time': 'contact reservations are half-open intervals on the shared pool (seconds); no emulator delay is injected', 'transport_emulator_delay_s': 0, 'compute_time': 'energy_audit jobs executed by the background worker (see jobs)'},
               'ok': conserved and unexpected == 0 and not dup_intents and not dup_res and all(stats[k]['ok'] > 0 for k in keys) and elapsed >= min(a.seconds, 1800) - 5, 'note': 'same host, loopback, SQLite; restarts interrupt in-flight requests whose retries reuse request ids (transport errors are expected refusals); partitions refuse cross-domain delivery (expected) while local work continues'}
        if a.out:
            with open(a.out, 'w') as f:
                json.dump(out, f, indent=1)
        print(json.dumps({k: out[k] for k in ('seconds', 'restarts', 'partitions', 'summary', 'ok')}, indent=1)); return 0 if out['ok'] else 1
    finally:
        A.close(); B.close(); sgA.close(); sgB.close(); W.close()


if __name__ == '__main__':
    raise SystemExit(main())
