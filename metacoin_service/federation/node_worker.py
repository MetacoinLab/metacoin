"""Remote worker node: a separate process with its own private home that talks to the coordinator only over the
authenticated node transport. It never opens the coordinator database or filesystem. It runs the same allowlisted
compute child (metacoin_service.compute.exec) and publishes progress, checkpoints and results through scoped
uploads; the coordinator verifies and publishes under its own fences.

    python -m metacoin_service node-worker --identity FILE --coordinator https://127.0.0.1:8443 --ca CA.pem [--once] [--home DIR]

Identity file (private, 0600): {"node_id", "credential", "private_key_hex"} written by `client_cli node-enroll`."""
import hashlib
import json
import os
import secrets
import select
import shutil
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from .. import crypto
from ..compute import container
from ..compute.engine import compute_interpreter
from .service import signing_message

ROOT = Path(__file__).resolve().parents[2]


class NodeClient:
    def __init__(self, coordinator, identity, ca_path=None, timeout=30, transport=None):
        self.base = coordinator.rstrip('/')
        self.identity = identity
        self.key = crypto._ed.Ed25519PrivateKey.from_private_bytes(bytes.fromhex(identity['private_key_hex']))
        self.timeout = timeout
        self.transport = transport                    # optional callable(method, path, headers, body) -> (status, body) for in-process tests
        self.ctx = None
        if self.base.startswith('https://'):
            self.ctx = ssl.create_default_context(cafile=ca_path)
            self.ctx.check_hostname = True
        elif not self.base.startswith('http://127.0.0.1') and not self.base.startswith('http://localhost'):
            raise ValueError('plain HTTP is allowed only for loopback development; use https with a pinned CA')

    def call(self, method, path, body=None, raw=None, headers=None):
        data = raw if raw is not None else (json.dumps(body, separators=(',', ':')).encode() if body is not None else b'')
        ts, nonce = str(int(time.time())), secrets.token_hex(8)
        sig = crypto.sign(self.key, signing_message(method, path, data, ts, nonce))
        h = {'X-Node-Credential': self.identity['credential'], 'X-Node-Signature': sig, 'X-Node-Timestamp': ts, 'X-Node-Nonce': nonce, 'Content-Type': 'application/octet-stream' if raw is not None else 'application/json'}
        h.update(headers or {})
        if self.transport:
            return self.transport(method, path, h, data)
        req = urllib.request.Request(self.base + path, data=data if method != 'GET' else None, method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self.ctx) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def json(self, method, path, body=None, raw=None, headers=None):
        status, out = self.call(method, path, body, raw, headers)
        try:
            parsed = json.loads(out) if out else {}
        except ValueError:
            parsed = {'raw': out[:200].decode(errors='replace')}
        return status, parsed


class NodeWorker:
    def __init__(self, client, home, compute_python=None, log=print):
        self.client, self.home, self.log = client, Path(home), log
        self.home.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.runtime = compute_interpreter(type('S', (), {'compute_python': compute_python or ''})())
        self.policy = None
        self.devices = ['cpu'] + (['cuda'] if self.runtime and self.runtime.get('cuda') else []) if self.runtime else []
        self.fail_after_checkpoint = os.environ.get('METACOIN_NODE_TEST_DIE_AFTER') or ''   # test hook: 'checkpoint' | 'result_upload'

    def register(self):
        """Report the devices the host actually has, restricted to what the enrollment allows (the identity file may
        carry 'devices' from enrollment; otherwise start with cpu and let the coordinator's policy answer)."""
        allowed = self.client.identity.get('devices')
        devices = [d for d in self.devices if allowed is None or d in allowed]
        status, out = self.client.json('POST', '/node/v1/register', {'versions': self.runtime, 'devices': devices})
        if status == 403 and isinstance(out, dict) and (out.get('detail') or {}).get('code') == 'devices_exceed_enrollment':
            devices = [d for d in self.devices if d in out['detail']['declared_at_enrollment']]
            status, out = self.client.json('POST', '/node/v1/register', {'versions': self.runtime, 'devices': devices})
        if status != 200:
            raise RuntimeError('register refused: %s %s' % (status, out))
        self.devices = devices
        self.policy = out
        return out

    def run_once(self):
        status, out = self.client.json('POST', '/node/v1/claim', {})
        if status != 200:
            raise RuntimeError('claim failed: %s %s' % (status, out))
        job = out.get('job')
        if not job:
            return None
        return job['id'], self.execute(job)

    def run_forever(self, poll_seconds=1.0, stop_file=None):
        last_beat = 0
        while True:
            if stop_file and Path(stop_file).exists():
                return
            if time.time() - last_beat >= self.policy['heartbeat_seconds']:
                st, out = self.client.json('POST', '/node/v1/heartbeat', {'current_job_id': None})
                last_beat = time.time()
                if st == 200 and out.get('state') == 'draining':
                    time.sleep(poll_seconds); continue
                if st == 403:
                    self.log('node disabled or revoked; exiting'); return
            try:
                ran = self.run_once()
            except RuntimeError as exc:
                self.log(str(exc)); time.sleep(poll_seconds); continue
            if ran is None:
                time.sleep(poll_seconds)

    # ---- one attempt ----------------------------------------------------------------------------
    def execute(self, job):
        jid, gen = job['id'], job['lease_generation']
        workdir = self.home / 'work' / (jid + '-' + str(gen) + '-' + secrets.token_hex(3))
        workdir.mkdir(mode=0o700, parents=True)
        try:
            resume_dir = None
            if job.get('resume_from_generation') is not None:
                st, blob = self.client.call('GET', '/node/v1/jobs/%s/checkpoint?generation=%d' % (jid, gen))
                if st == 200:
                    resume_dir = workdir / 'resume'; resume_dir.mkdir(mode=0o700)
                    for name, data in container.unpack(blob).items():
                        (resume_dir / name).write_bytes(data)
            spec = {'job_id': jid, 'kind': job['kind'], 'inputs': job['inputs'], 'manifest': job['manifest'], 'backend': job['backend'], 'precision': job['precision'], 'attempt_generation': gen,
                    'input_digest': job['input_digest'], 'chunk': None, 'checkpoint_interval_seconds': job['checkpoint_interval_seconds'], 'resume_dir': str(resume_dir) if resume_dir else None, 'limits': job['limits'], 'aux': None}
            (workdir / 'spec.json').write_bytes(json.dumps(spec).encode())
            return self._supervise(job, workdir)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    def _upload(self, jid, gen, role, files):
        blob = container.pack(files)
        digest = hashlib.sha256(blob).hexdigest()
        st, out = self.client.json('POST', '/node/v1/jobs/%s/uploads?generation=%d' % (jid, gen), {'role': role, 'total_bytes': len(blob), 'sha256': digest})
        if st != 200:
            raise RuntimeError('upload start refused: %s %s' % (st, out))
        uid, chunk = out['upload_id'], out['chunk_max_bytes']
        off = 0
        while off < len(blob):
            piece = blob[off:off + chunk]
            st, out = self.client.json('PUT', '/node/v1/uploads/%s?offset=%d' % (uid, off), raw=piece)
            if st != 200:
                raise RuntimeError('chunk refused: %s %s' % (st, out))
            off = out['received_bytes']
        st, out = self.client.json('POST', '/node/v1/uploads/%s/complete' % uid, {})
        if st != 200:
            raise RuntimeError('upload complete refused: %s %s' % (st, out))
        return uid

    def _supervise(self, job, workdir):
        jid, gen = job['id'], job['lease_generation']
        env = {'PATH': os.environ.get('PATH', ''), 'PYTHONPATH': str(ROOT), 'HOME': os.environ.get('HOME', '/'), 'PYTHONUNBUFFERED': '1'}
        log_file = open(workdir / 'stderr.log', 'wb')
        proc = subprocess.Popen([self.runtime['python'], '-m', 'metacoin_service.compute.exec', str(workdir)], cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log_file, env=env)
        started, child_started, last_lease, last_progress = time.time(), None, time.time(), 0.0
        buf, outcome, control_sent, versions = b'', None, None, None
        try:
            while True:
                if time.time() - last_lease >= max(5, self.policy['lease_seconds'] // 3):
                    st, out = self.client.json('POST', '/node/v1/jobs/%s/lease?generation=%d' % (jid, gen), {})
                    last_lease = time.time()
                    if st != 200:
                        proc.kill(); outcome = ('fenced', None, {}); break
                    if out.get('control') in ('pause', 'cancel') and control_sent is None:
                        control_sent = out['control']
                        try:
                            proc.stdin.write((control_sent + '\n').encode()); proc.stdin.flush()
                        except (BrokenPipeError, OSError):
                            pass
                r, _, _ = select.select([proc.stdout], [], [], 0.5)
                if not r:
                    if proc.poll() is not None:
                        outcome = ('error', 'COMPUTATION_ERROR', {'reason': 'child exited %s' % proc.returncode}); break
                    continue
                chunk = os.read(proc.stdout.fileno(), 65536)
                if not chunk:
                    outcome = ('error', 'COMPUTATION_ERROR', {'reason': 'child closed stdout'}); break
                buf += chunk
                while b'\n' in buf:
                    line, buf = buf.split(b'\n', 1)
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    et = ev.get('event')
                    if et == 'started':
                        child_started = time.time(); versions = {'backend': ev['backend'], 'versions': ev['versions'], 'threads': ev.get('threads'), 'node': True}
                        self.client.json('POST', '/node/v1/jobs/%s/progress?generation=%d' % (jid, gen), {'committed': 0, 'total': 0, 'chunk_id': 0, 'versions': versions})
                    elif et == 'progress' and time.time() - last_progress >= 1.0:
                        self.client.json('POST', '/node/v1/jobs/%s/progress?generation=%d' % (jid, gen), {'committed': ev['committed'], 'total': ev['total'], 'chunk_id': ev['chunk_id']}); last_progress = time.time()
                    elif et == 'checkpoint':
                        d = workdir / ev['dir']
                        files = {p.name: p.read_bytes() for p in d.iterdir() if p.is_file()}
                        try:
                            uid = self._upload(jid, gen, 'checkpoint', files)
                            st, out = self.client.json('POST', '/node/v1/jobs/%s/checkpoints?generation=%d' % (jid, gen), {'upload_id': uid, 'generation': ev['generation'], 'committed': ev['committed'], 'reason': ev.get('reason')})
                            ack = out.get('ack', 'cancel') if st == 200 else 'cancel'
                        except RuntimeError as exc:
                            self.log('checkpoint publication failed: %s' % exc); ack = 'cancel'
                        if self.fail_after_checkpoint == 'checkpoint':
                            self.log('test hook: dying after checkpoint'); proc.kill(); os._exit(3)
                        try:
                            proc.stdin.write((ack + '\n').encode()); proc.stdin.flush()
                        except (BrokenPipeError, OSError):
                            pass
                        if ack == 'cancel' and st != 200:
                            outcome = ('fenced', None, {}); break
                    elif et == 'paused':
                        outcome = ('paused', None, ev); break
                    elif et == 'cancelled':
                        outcome = ('cancelled', None, ev); break
                    elif et == 'result':
                        outcome = ('result', None, ev); break
                    elif et == 'error':
                        outcome = ('error', ev.get('code', 'INTERNAL_DEFECT'), ev); break
                if outcome:
                    break
        finally:
            try:
                proc.stdin.close()
            except Exception:
                pass
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill(); proc.wait(timeout=10)
            log_file.close()
        kind, code, ev = outcome
        if kind == 'fenced':
            return 'fenced'
        if kind == 'paused':
            st, out = self.client.json('POST', '/node/v1/jobs/%s/paused?generation=%d' % (jid, gen), {'generation': ev.get('generation'), 'committed': ev.get('committed')})
            return out.get('outcome', 'paused') if st == 200 else 'fenced'
        if kind == 'cancelled':
            st, out = self.client.json('POST', '/node/v1/jobs/%s/fail?generation=%d' % (jid, gen), {'code': 'CANCELLED', 'reason': 'cancelled at checkpoint'})
            return out.get('outcome', 'cancelled') if st == 200 else 'fenced'
        if kind == 'error':
            st, out = self.client.json('POST', '/node/v1/jobs/%s/fail?generation=%d' % (jid, gen), {'code': code, 'reason': str(ev.get('reason', ''))[:500]})
            return out.get('outcome', 'failed') if st == 200 else 'fenced'
        out_dir = workdir / ev['out_dir']
        files = {p.name: p.read_bytes() for p in out_dir.iterdir() if p.is_file()}
        uid = self._upload(jid, gen, 'result', files)
        if self.fail_after_checkpoint == 'result_upload':
            self.log('test hook: dying after result upload, before publication'); os._exit(4)
        st, out = self.client.json('POST', '/node/v1/jobs/%s/result?generation=%d' % (jid, gen), {'upload_id': uid, 'committed': ev['committed'], 'generation': ev.get('generation', 0), 'summary': ev['summary'],
                                                                                                   'energy_delta_mJ_device_wide': ev.get('energy_delta_mJ_device_wide'), 'peak_device_bytes': ev.get('peak_device_bytes'),
                                                                                                   'duration_ms': int((time.time() - started) * 1000), 'compute_ms': int((time.time() - child_started) * 1000) if child_started else None})
        if st != 200:
            self.log('result publication refused: %s %s' % (st, out)); return 'fenced'
        return out.get('outcome')


def load_identity(path):
    p = Path(path)
    if p.stat().st_mode & 0o077:
        raise SystemExit('identity file must be private (0600)')
    return json.loads(p.read_text())


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog='metacoin_service node-worker')
    ap.add_argument('--identity', required=True); ap.add_argument('--coordinator', required=True); ap.add_argument('--ca'); ap.add_argument('--home', required=True)
    ap.add_argument('--once', action='store_true'); ap.add_argument('--stop-file'); ap.add_argument('--compute-python')
    a = ap.parse_args(argv)
    client = NodeClient(a.coordinator, load_identity(a.identity), ca_path=a.ca)
    w = NodeWorker(client, a.home, compute_python=a.compute_python, log=lambda m: print(m, file=sys.stderr, flush=True))
    reg = w.register()
    if a.once:
        print(json.dumps({'registered': reg, 'ran': w.run_once()})); return 0
    print(json.dumps({'registered': reg}), flush=True)
    w.run_forever(stop_file=a.stop_file)
    return 0


if __name__ == '__main__':
    sys.exit(main())
