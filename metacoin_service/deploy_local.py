"""One-command local deployment (order 2026-10-23 group H): service home, API, background worker, operator-controlled signer and
commit-checkpoint witness on loopback, domain initialised and witness pinned, x402 disabled and its facilitator unreachable.

    python -m metacoin_service.deploy_local up --home DIR [--port 8410] [--domain-id dom-local]
    python -m metacoin_service.deploy_local status --home DIR
    python -m metacoin_service.deploy_local down --home DIR

`up` prints where the administrator credential file is (it never prints the token), the URLs and the pid file. Everything lives
under DIR (service home, signer home, witness home, logs); nothing touches another deployment."""
import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

PY = sys.executable
ROOT = Path(__file__).resolve().parents[1]


def _free_port():
    import socket
    s = socket.socket(); s.bind(('127.0.0.1', 0)); p = s.getsockname()[1]; s.close(); return p


def _wait_http(url, headers=None, tries=300):
    for _ in range(tries):
        try:
            req = urllib.request.Request(url, headers=headers or {})
            with urllib.request.urlopen(req, timeout=1) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.1)
    return False


def up(a):
    home = Path(a.home).resolve(); home.mkdir(parents=True, exist_ok=True); os.chmod(home, 0o700)
    svc, sg, wt, logs = home / 'service', home / 'signer', home / 'witness', home / 'logs'; logs.mkdir(exist_ok=True)
    state_path = home / 'deploy.json'
    if state_path.exists():
        return {'error': 'already deployed (deploy.json exists); run down first', 'state': json.loads(state_path.read_text())}
    env = dict(os.environ, PYTHONPATH=str(ROOT), METACOIN_X402_DISABLED='1', METACOIN_FACILITATOR_URL='http://127.0.0.1:1/blocked')
    fresh = not (svc / 'service.sqlite').exists()
    if fresh:
        init = subprocess.run([PY, '-m', 'metacoin_service', '--home', str(svc), '--provider-mode', 'simulation', 'init'], cwd=ROOT, env=env, capture_output=True, text=True)
        if init.returncode != 0:
            return {'error': 'service init failed', 'stderr': init.stderr[-2000:]}
        info = json.loads(init.stdout); cred_file = info['credential_file']
    else:
        cred_file = str(next((svc / 'credentials').glob('*.json'), ''))
    port = a.port or _free_port(); sport = a.signer_port or _free_port(); wport = a.witness_port or _free_port(); did = a.domain_id
    if not (sg / 'token').exists():
        subprocess.run([PY, '-m', 'metacoin_service.native.signer', 'init', '--home', str(sg), '--domains', did, '--signer-id', 'signer-' + did], cwd=ROOT, env=env, check=True, capture_output=True)
    if not (wt / 'token').exists():
        w = subprocess.run([PY, '-m', 'metacoin_service.native.witness', 'init', '--home', str(wt), '--witness-id', 'witness-' + did], cwd=ROOT, env=env, check=True, capture_output=True, text=True); (wt / 'identity.json').write_text(w.stdout)
    identity = json.loads((wt / 'identity.json').read_text())
    procs = {}
    procs['signer'] = subprocess.Popen([PY, '-m', 'metacoin_service.native.signer', 'serve', '--home', str(sg), '--port', str(sport)], cwd=ROOT, env=env, stdout=open(logs / 'signer.log', 'ab'), stderr=subprocess.STDOUT)
    procs['witness'] = subprocess.Popen([PY, '-m', 'metacoin_service.native.witness', 'serve', '--home', str(wt), '--port', str(wport)], cwd=ROOT, env=env, stdout=open(logs / 'witness.log', 'ab'), stderr=subprocess.STDOUT)
    env_api = dict(env, METACOIN_SIGNER_URL='http://127.0.0.1:%d' % sport, METACOIN_SIGNER_TOKEN_FILE=str(sg / 'token'), METACOIN_WITNESS_URL='http://127.0.0.1:%d' % wport, METACOIN_WITNESS_TOKEN_FILE=str(wt / 'token'), METACOIN_WITNESS_KEY_ID=identity['key_id'])
    procs['api'] = subprocess.Popen([PY, '-m', 'metacoin_service', '--home', str(svc), '--provider-mode', 'simulation', 'serve', '--port', str(port)], cwd=ROOT, env=env_api, stdout=open(logs / 'api.log', 'ab'), stderr=subprocess.STDOUT)
    stop = home / 'worker.stop'
    if stop.exists():
        stop.unlink()
    procs['worker'] = subprocess.Popen([PY, '-m', 'metacoin_service', '--home', str(svc), '--provider-mode', 'simulation', 'worker', '--name', 'w-local', '--stop-file', str(stop)], cwd=ROOT, env=env_api, stdout=open(logs / 'worker.log', 'ab'), stderr=subprocess.STDOUT)
    base = 'http://127.0.0.1:%d' % port
    ok = _wait_http(base + '/api/health')
    state = {'home': str(home), 'service_home': str(svc), 'base': base, 'signer': {'url': 'http://127.0.0.1:%d' % sport, 'token_file': str(sg / 'token')}, 'witness': {'url': 'http://127.0.0.1:%d' % wport, 'token_file': str(wt / 'token'), 'key_id': identity['key_id']},
             'admin_credential_file': cred_file, 'domain_id': did, 'pids': {k: p.pid for k, p in procs.items()}, 'worker_stop_file': str(stop), 'x402': 'disabled (METACOIN_X402_DISABLED=1; facilitator URL closed)', 'started_at': int(time.time())}
    state_path.write_text(json.dumps(state, indent=1)); os.chmod(state_path, 0o600)
    if not ok:
        return dict(state, error='API did not become healthy; see logs/api.log')
    token = json.load(open(cred_file))['principals']['owner']['token'] if cred_file else None
    if token and fresh:
        req = urllib.request.Request(base + '/api/v1/native/domain', data=json.dumps({'domain_id': did, 'operator': 'local deployment (one host)', 'host': 'this host'}).encode(), method='POST', headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                dom = json.loads(r.read())
        except urllib.error.HTTPError as e:
            dom = {'error': e.code, 'body': e.read()[:300].decode('utf-8', 'replace')}
        if 'authority' in dom:
            wreq = urllib.request.Request(state['witness']['url'] + '/pin', data=json.dumps({'domain': did, 'key_id': dom['authority']['key_id'], 'public_hex': dom['authority']['public_hex']}).encode(), method='POST', headers={'Authorization': 'Bearer ' + json.load(open(wt / 'token'))['token'], 'Content-Type': 'application/json'})
            try:
                urllib.request.urlopen(wreq, timeout=10).read(); state['witness']['pinned'] = did
            except Exception as exc:
                state['witness']['pinned'] = 'failed: %s' % exc
        state['domain'] = {k: dom.get(k) for k in ('id', 'default_asset', 'accounts')} if 'authority' in dom else dom
        state_path.write_text(json.dumps(state, indent=1))
    state['next'] = ['console: %s/console (log in with the administrator credential file; the token is never printed)' % base,
                     'provider client: pip install ./provider_client; metacoin-provider --base %s --signer %s --signer-token %s enroll --admin-cred <admin file> --name prov-1 --ceiling 1000 --out prov.json' % (base, state['signer']['url'], state['signer']['token_file']),
                     'stop: python -m metacoin_service.deploy_local down --home %s' % home]
    return state


def status(a):
    home = Path(a.home).resolve(); sp = home / 'deploy.json'
    if not sp.exists():
        return {'deployed': False}
    st = json.loads(sp.read_text()); alive = {}
    for k, pid in st['pids'].items():
        try:
            os.kill(pid, 0); alive[k] = True
        except OSError:
            alive[k] = False
    st['alive'] = alive; st['api_healthy'] = _wait_http(st['base'] + '/api/health', tries=3); return st


def down(a):
    home = Path(a.home).resolve(); sp = home / 'deploy.json'
    if not sp.exists():
        return {'deployed': False}
    st = json.loads(sp.read_text()); Path(st['worker_stop_file']).write_text('stop'); stopped = {}
    for k in ('worker', 'api', 'signer', 'witness'):
        pid = st['pids'].get(k)
        try:
            os.kill(pid, signal.SIGTERM); stopped[k] = 'terminated'
        except OSError:
            stopped[k] = 'not running'
    for _ in range(100):
        if all(_dead(p) for p in st['pids'].values()):
            break
        time.sleep(0.1)
    for pid in st['pids'].values():
        if not _dead(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
    sp.unlink(); return {'stopped': stopped, 'home': str(home), 'note': 'state kept under the home; up again resumes the same service home, signer and witness'}


def _dead(pid):
    try:
        os.kill(pid, 0); return False
    except OSError:
        return True


def main(argv=None):
    ap = argparse.ArgumentParser(prog='metacoin-deploy-local'); sub = ap.add_subparsers(dest='cmd', required=True)
    u = sub.add_parser('up'); u.add_argument('--home', required=True); u.add_argument('--port', type=int); u.add_argument('--signer-port', type=int); u.add_argument('--witness-port', type=int); u.add_argument('--domain-id', default='dom-local')
    s = sub.add_parser('status'); s.add_argument('--home', required=True); d = sub.add_parser('down'); d.add_argument('--home', required=True)
    a = ap.parse_args(argv); out = {'up': up, 'status': status, 'down': down}[a.cmd](a); print(json.dumps(out, indent=1)); return 1 if out.get('error') else 0


if __name__ == '__main__':
    sys.exit(main())
