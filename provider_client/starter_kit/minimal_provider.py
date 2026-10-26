"""Minimal provider written against the public API with the standard library only (no metacoin_provider import).

    python minimal_provider.py --base URL --cred prov.json [--ca ca.pem] publish --kind compute --resource rig-1 --capacity 100000 --price 1 --hours 2
    python minimal_provider.py --base URL --cred prov.json serve --executor "python -m metacoin_service.lunar task" [--once]
    python minimal_provider.py --base URL --cred prov.json earnings"""
import argparse
import json
import os
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request


def client(base, cred, ca=None):
    st = os.stat(cred)
    if st.st_mode & 0o077:
        raise SystemExit('credential file must be 0600')
    token = json.load(open(cred))['token']; u = base.split('://', 1)
    if u[0] != 'https' and not any(u[1].startswith(h) for h in ('127.0.0.1', 'localhost', '[::1]')):
        raise SystemExit('plain http is refused beyond loopback: use https and --ca')
    ctx = ssl.create_default_context(cafile=ca) if ca else None
    def call(method, path, body=None, raw=None, content_type='application/json'):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(base + path, data=data, method=method, headers={'Authorization': 'Bearer ' + token, 'Content-Type': content_type})
        try:
            with urllib.request.urlopen(req, timeout=120, context=ctx) as r:
                raw_out = r.read()
                if len(raw_out) > 8 * 1024 * 1024:
                    raise SystemExit('response too large')
                return r.status, json.loads(raw_out or b'{}')
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read() or b'{}')
            except ValueError:
                return e.code, {}
    return call


def main(argv=None):
    ap = argparse.ArgumentParser(); ap.add_argument('--base', required=True); ap.add_argument('--cred', required=True); ap.add_argument('--ca'); sub = ap.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('publish'); p.add_argument('--kind', default='compute'); p.add_argument('--resource', required=True); p.add_argument('--capacity', type=int, required=True); p.add_argument('--price', type=int, required=True); p.add_argument('--hours', type=int, default=2)
    s = sub.add_parser('serve'); s.add_argument('--executor', required=True); s.add_argument('--once', action='store_true'); s.add_argument('--poll', type=float, default=2.0); sub.add_parser('earnings'); sub.add_parser('reservations')
    a = ap.parse_args(argv); go = client(a.base, a.cred, a.ca)
    if a.cmd == 'publish':
        t = int(time.time()); st, r = go('POST', '/api/v1/resources/offers', {'kind': a.kind, 'resource_id': a.resource, 'capacity': a.capacity, 'price_per_unit': a.price, 'window_start': t - 1, 'window_end': t + 3600 * a.hours}); print(json.dumps(r)); return 0 if st == 201 else 1
    if a.cmd == 'earnings':
        st, r = go('GET', '/api/v1/native/accounts/me'); print(json.dumps(r)); return 0 if st == 200 else 1
    if a.cmd == 'reservations':
        st, r = go('GET', '/api/v1/resources/reservations'); print(json.dumps(r)); return 0 if st == 200 else 1
    done = set()
    while True:
        st, r = go('GET', '/api/v1/resources/reservations'); items = (r.get('items') or []) if st == 200 else []
        for res in items:
            if res['id'] in done or res['state'] not in ('reserved', 'active'):
                continue
            if res['state'] == 'reserved':
                go('POST', '/api/v1/resources/reservations/%s/activate' % res['id'], {})
            task = {'kind': 'terrain_tile', 'source': 'apollo11_32'}; t0 = time.perf_counter()
            run = subprocess.run(a.executor.split() + [json.dumps(task)], capture_output=True, text=True, timeout=600); cpu = int((time.perf_counter() - t0) * 1000)
            try:
                product = json.loads(run.stdout)
            except ValueError:
                product = {'error': run.stderr[-300:]}
            st, d = go('POST', '/api/v1/resources/reservations/%s/deliver' % res['id'], {'task': task, 'execution': {'cpu_ms': cpu, 'exit_code': run.returncode, 'product': product, 'executed_by': 'provider'}}); print(json.dumps({'reservation': res['id'], 'deliver': st, 'delivered_quantity': d.get('delivered_quantity'), 'verification': (d.get('measurement') or {}).get('verification')}), flush=True); done.add(res['id'])
        if a.once:
            return 0
        time.sleep(a.poll)


if __name__ == '__main__':
    sys.exit(main())
