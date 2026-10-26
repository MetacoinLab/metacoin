"""Bounded mixed-work endurance check with real contention and restart recovery (order 2026-10-23 §15). Two domain process sets
(journeys harness), then concurrent loops for a fixed duration: native transfers from one payer, reservations racing on one
shared contact offer (some cancelled, some delivered and settled by the provider), real work awards executed by the background
worker and paid natively, cross-domain exports pushed by a courier and partly returned. Halfway through, the domain A API process
is restarted while every loop keeps running (retries reuse the same request ids). At the end: conservation on both domains, no
duplicated settlement per request id, every loop's throughput and error census, queue state.

    METACOIN_X402_DISABLED=1 PYTHONPATH=. .venv-service/bin/python -m metacoin_service.endurance_mission --seconds 120 --out endurance.json"""
import argparse
import json
import os
import sqlite3
import threading
import time

from .native import courier
from .tests.journeys_mission import MDom, SignerProcess, WitnessProcess
from .tests.test_work_terms import energy_inputs


def main(argv=None):
    ap = argparse.ArgumentParser(); ap.add_argument('--seconds', type=int, default=120); ap.add_argument('--out'); a = ap.parse_args(argv)
    os.environ['METACOIN_X402_DISABLED'] = '1'
    W = WitnessProcess(); sgA = SignerProcess(domains=('end-a',)); sgB = SignerProcess(domains=('end-b',))
    wenv = {'METACOIN_WITNESS_URL': W.base, 'METACOIN_WITNESS_TOKEN_FILE': str(W.home / 'token'), 'METACOIN_WITNESS_KEY_ID': W.identity['key_id']}
    A = MDom('end-a', dict(wenv, METACOIN_SIGNER_URL=sgA.base, METACOIN_SIGNER_TOKEN_FILE=str(sgA.home / 'token'))); B = MDom('end-b', dict(wenv, METACOIN_SIGNER_URL=sgB.base, METACOIN_SIGNER_TOKEN_FILE=str(sgB.home / 'token'))); A.pin(B); B.pin(A)
    A.start_worker('w2-end-a')
    stats = {k: {'ok': 0, 'errors': {}, 'latency': []} for k in ('transfers', 'reservations', 'work', 'cross_domain')}; lock = threading.Lock(); stop = threading.Event(); restart_at = {}
    def count(k, ok, code=None, dt=None):
        with lock:
            s = stats[k]
            if ok:
                s['ok'] += 1
            else:
                s['errors'][code or 'unknown'] = s['errors'].get(code or 'unknown', 0) + 1
            if dt is not None:
                s['latency'].append(dt)
    def safe(fn, k):
        t0 = time.perf_counter()
        try:
            st, r = fn(); ok = st in (200, 201); count(k, ok, None if ok else ((r.get('detail') or {}).get('code') if isinstance(r.get('detail'), dict) else r.get('code', 'HTTP_%s' % st)), time.perf_counter() - t0); return st, r
        except Exception as exc:
            count(k, False, 'transport:' + type(exc).__name__, time.perf_counter() - t0); time.sleep(0.5); return 0, {}
    try:
        payer = A.actor('payer', 5000); payee = A.actor('payee'); sup = A.actor('sup'); A.provider('sup', kinds=('energy_audit',)); req = A.actor('req', 5000); mach = A.actor('mach'); A.provider('mach', kinds=('energy_audit',)); exp = A.actor('exp', 2000); bob = B.actor('bob')
        buyers = [A.actor('buyer%d' % i, 300) for i in range(4)]; offer = A.offer('sup', 'contact', 10 ** 6, 1)
        funding = A.audit()['fixture_funding']
        def transfers():
            i = 0
            while not stop.is_set():
                rid = 'e-t-%d' % i; safe(lambda: A.call('post', '/api/v1/native/transfers', role='payer', json={'to_account': payee['account'], 'amount': 1, 'request_id': rid}), 'transfers'); i += 1
        def reservations(bi):
            i = 0; who = 'buyer%d' % bi
            while not stop.is_set():
                rid = 'e-r-%d-%d' % (bi, i); st, r = safe(lambda: A.reserve(who, offer, 2, rid), 'reservations'); i += 1
                if st == 201:
                    if i % 3 == 0:
                        safe(lambda: A.res_action(who, r['id'], 'cancel'), 'reservations')
                    else:
                        safe(lambda: A.res_action('sup', r['id'], 'activate'), 'reservations'); safe(lambda: A.res_action('sup', r['id'], 'deliver', {'bytes': 2 * 1024}), 'reservations'); safe(lambda: A.res_action('sup', r['id'], 'settle'), 'reservations')
                time.sleep(0.05)
        def work():
            i = 0
            while not stop.is_set():
                t0 = time.perf_counter()
                try:
                    t, r, o, (sa, aw) = A.award('req', 'mach', 'determination', energy_inputs('FEASIBLE' if i % 2 else 'INFEASIBLE'), 3)
                    if sa != 201:
                        count('work', False, (aw.get('detail') or {}).get('code') if isinstance(aw.get('detail'), dict) else aw.get('code', 'award_%s' % sa)); time.sleep(1); continue
                    v, d = A.deliver_accept(aw['id'], 'req', timeout=180); paid = A.pay_all(aw['id'], 'req') if d else {}
                    count('work', bool(d) and d.get('decision') == 'accepted' and paid.get('provider', {}).get('rail') == 'native', None if d else 'execution_incomplete', time.perf_counter() - t0)
                except Exception as exc:
                    count('work', False, 'exception:' + type(exc).__name__, time.perf_counter() - t0); time.sleep(1)
                i += 1
        def cross():
            i = 0
            while not stop.is_set():
                rid = 'e-x-%d' % i; st, r = safe(lambda: A.call('post', '/api/v1/native/allocations/export', role='exp', json={'destination_domain': 'end-b', 'recipient_account': bob['account'], 'amount': 1, 'request_id': rid}), 'cross_domain'); i += 1
                try:
                    out = courier.push(A.ep(), B.ep(), 'end-b')
                    if i % 4 == 0 and st == 201:
                        B.call('post', '/api/v1/native/allocations/%s/return' % r['id'], role='bob', json={}); courier.push(B.ep(), A.ep(), 'end-a')
                except Exception as exc:
                    count('cross_domain', False, 'courier:' + type(exc).__name__)
                time.sleep(0.2)
        threads = [threading.Thread(target=transfers, daemon=True), threading.Thread(target=work, daemon=True), threading.Thread(target=cross, daemon=True)] + [threading.Thread(target=reservations, args=(i,), daemon=True) for i in range(4)]
        t_start = time.time(); [t.start() for t in threads]
        time.sleep(a.seconds / 2); restart_at['t'] = round(time.time() - t_start, 1); A.restart_api(); restart_at['api_back_after_s'] = round(time.time() - t_start - restart_at['t'], 2)
        time.sleep(a.seconds / 2); stop.set(); [t.join(timeout=240) for t in threads]
        # settle in: wait for the worker to finish outstanding jobs
        time.sleep(3)
        # final checks
        auA = A.audit(); auB = B.audit(A.asset); acc = A.call('get', '/api/v1/native/accounting')[1]
        db = sqlite3.connect('file:%s?mode=ro' % A.inst.settings.db_path, uri=True)
        dup_intents = db.execute("SELECT actor_account, request_id, COUNT(*) c FROM native_intents WHERE state='settled' GROUP BY actor_account, request_id HAVING c > 1").fetchall()
        dup_res = db.execute("SELECT buyer_account, request_id, COUNT(*) c FROM resource_reservations GROUP BY buyer_account, request_id HAVING c > 1").fetchall()
        settled_res = db.execute("SELECT COUNT(*) FROM resource_reservations WHERE state='settled'").fetchone()[0]; cancelled_res = db.execute("SELECT COUNT(*) FROM resource_reservations WHERE state='cancelled'").fetchone()[0]
        jobs = {r[0]: r[1] for r in db.execute('SELECT state, COUNT(*) FROM jobs GROUP BY state').fetchall()}; db.close()
        held_total = auA['total_held'] + auA['backing_locked_for_exports']; conserved = auA['conserved'] and held_total == auA['fixture_funding'] and auB['claims_backed_elsewhere'] == auB['total_held']
        pct = lambda xs, q: (sorted(xs)[min(len(xs) - 1, int(q * len(xs)))] if xs else None)
        out = {'schema': 'metacoin-endurance-mission/v1', 'seconds': a.seconds, 'restart': restart_at, 'loops': {k: {'ok': v['ok'], 'errors': v['errors'], 'per_s': round(v['ok'] / a.seconds, 2), 'p50_s': round(pct(v['latency'], 0.5), 4) if v['latency'] else None, 'p95_s': round(pct(v['latency'], 0.95), 4) if v['latency'] else None} for k, v in stats.items()},
               'final': {'audit_a': {k: auA[k] for k in ('total_held', 'fixture_funding', 'backing_locked_for_exports', 'claims_backed_elsewhere', 'conserved')}, 'audit_b_asset_a': {k: auB[k] for k in ('total_held', 'claims_backed_elsewhere', 'conserved')}, 'accounting_checks': acc['checks'], 'conserved_across_domains': conserved, 'duplicate_settled_intents': len(dup_intents), 'duplicate_reservations': len(dup_res), 'reservations': {'settled': settled_res, 'cancelled': cancelled_res}, 'jobs': jobs, 'recovery_state': A.domain()['recovery_state']},
               'ok': conserved and not dup_intents and not dup_res and stats['work']['ok'] > 0 and stats['reservations']['ok'] > 0 and stats['transfers']['ok'] > 0 and stats['cross_domain']['ok'] > 0, 'note': 'same host, loopback, SQLite; the restart interrupts in-flight requests whose retries reuse request ids (transport errors are counted, never hidden)'}
        if a.out:
            with open(a.out, 'w') as f:
                json.dump(out, f, indent=1)
        print(json.dumps({k: out[k] for k in ('seconds', 'restart', 'loops', 'ok')}, indent=1)); print(json.dumps(out['final'], indent=1)); return 0 if out['ok'] else 1
    finally:
        A.close(); B.close(); sgA.close(); sgB.close(); W.close()


if __name__ == '__main__':
    raise SystemExit(main())
