"""Independent commit-checkpoint witness (order 2026-10-23 group G): a separate process with its own store that records signed
commit checkpoints of a domain and returns a witness-signed acknowledgment. A restored database older than an acknowledged
checkpoint is detected by comparing sequences; without a reachable witness (and without the local watermark file) rollback
detection is reported as unavailable, never inferred from a clean start.

    python -m metacoin_service.native.witness init  --home WITNESS_HOME --witness-id w1
    python -m metacoin_service.native.witness serve --home WITNESS_HOME --port 8601
  POST /pin        {domain, key_id, public_hex}        pin a domain authority (operator act)
  POST /checkpoint {statement, signature_hex, key_id}  statement signed by the domain authority -> witness acknowledgment
  GET  /latest/{domain}                                latest acknowledged checkpoint + freshness
  GET  /health"""
import argparse
import json
import os
import secrets
import sqlite3
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .. import crypto
from . import protocol as P

WITNESS_PROTOCOL = 'metacoin-witness/v1'
CHECKPOINT_FIELDS = {'type', 'domain', 'epoch', 'sequence', 'journal_root', 'environment', 'issued_at'}


def now():
    return int(time.time())


class WitnessStore:
    def __init__(self, home):
        self.home = Path(home); self.db_path = self.home / 'witness.sqlite'

    def init(self, witness_id=None):
        self.home.mkdir(mode=0o700, parents=True, exist_ok=True); os.chmod(self.home, 0o700)
        pub = crypto.generate_signing_key(self.home / 'witness.ed25519')
        cfg = {'witness_id': witness_id or ('witness-' + secrets.token_hex(3)), 'key_id': crypto.key_id_for(pub), 'public_hex': pub, 'created_at': now()}
        (self.home / 'config.json').write_text(json.dumps(cfg)); os.chmod(self.home / 'config.json', 0o600)
        tok = self.home / 'token'
        if not tok.exists():
            fd = os.open(tok, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600); os.write(fd, json.dumps({'token': 'wit_' + secrets.token_urlsafe(24)}).encode()); os.close(fd)
        with self.db() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS pins (domain TEXT PRIMARY KEY, key_id TEXT NOT NULL, public_hex TEXT NOT NULL, pinned_at INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS checkpoints (domain TEXT NOT NULL, sequence INTEGER NOT NULL, epoch INTEGER NOT NULL, statement_json TEXT NOT NULL, signature_hex TEXT NOT NULL, key_id TEXT NOT NULL, ack_json TEXT NOT NULL, ack_signature TEXT NOT NULL, received_at INTEGER NOT NULL, PRIMARY KEY (domain, sequence));''')
        return cfg

    def config(self):
        return json.loads((self.home / 'config.json').read_text())

    def token(self):
        return json.loads((self.home / 'token').read_text())['token']

    def db(self):
        c = sqlite3.connect(self.db_path, isolation_level=None, timeout=30); c.row_factory = sqlite3.Row; c.execute('PRAGMA journal_mode=WAL'); return c

    def pin(self, body):
        d, kid, pub = body.get('domain'), body.get('key_id'), body.get('public_hex')
        if type(d) is not str or type(pub) is not str or len(pub) != 64 or crypto.key_id_for(pub) != kid:
            raise Refusal('pin', 'domain, key_id and a 32-byte public key (hex) are required')
        with self.db() as db:
            db.execute('INSERT OR REPLACE INTO pins VALUES (?,?,?,?)', (d, kid, pub, now()))
        return {'domain': d, 'key_id': kid, 'pinned': True}

    def checkpoint(self, body):
        st = body.get('statement'); sig = body.get('signature_hex'); kid = body.get('key_id')
        if type(st) is not dict or set(st) != CHECKPOINT_FIELDS or st['type'] != 'commit_checkpoint' or type(st['sequence']) is not int or type(st['epoch']) is not int:
            raise Refusal('statement', sorted(CHECKPOINT_FIELDS))
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            pin = db.execute('SELECT * FROM pins WHERE domain=?', (st['domain'],)).fetchone()
            if pin is None or pin['key_id'] != kid:
                raise Refusal('domain_not_pinned', {'domain': st['domain'], 'key_id': kid})
            if not crypto.verify(pin['public_hex'], P.signing_bytes('commit_checkpoint', st), sig or ''):
                raise Refusal('bad_signature')
            latest = db.execute('SELECT sequence FROM checkpoints WHERE domain=? ORDER BY sequence DESC LIMIT 1', (st['domain'],)).fetchone()
            ex = db.execute('SELECT * FROM checkpoints WHERE domain=? AND sequence=?', (st['domain'], st['sequence'])).fetchone()
            if ex is not None:
                if json.loads(ex['statement_json']) != st:
                    db.execute('COMMIT'); raise Refusal('conflicting_checkpoint', {'sequence': st['sequence'], 'note': 'two different checkpoints for one sequence: contradictory authority history recorded'})
                db.execute('COMMIT'); return {'acknowledgment': json.loads(ex['ack_json']), 'witness_signature': ex['ack_signature'], 'replayed': True}
            if latest and st['sequence'] < latest['sequence']:
                db.execute('COMMIT'); raise Refusal('sequence_regression', {'latest_acknowledged': latest['sequence'], 'presented': st['sequence'], 'note': 'a checkpoint below the latest acknowledged one is not accepted: the presenting database may be rolled back'})
            cfg = self.config(); ack = {'type': 'witness_ack', 'witness_id': cfg['witness_id'], 'domain': st['domain'], 'sequence': st['sequence'], 'statement_digest': P.digest(st), 'acknowledged_at': now()}
            wsig = crypto.sign(crypto.load_signing_key(self.home / 'witness.ed25519'), P.signing_bytes('commit_checkpoint', ack))
            db.execute('INSERT INTO checkpoints VALUES (?,?,?,?,?,?,?,?,?)', (st['domain'], st['sequence'], st['epoch'], json.dumps(st), sig, kid, json.dumps(ack), wsig, now()))
            db.execute('COMMIT')
        return {'acknowledgment': ack, 'witness_signature': wsig, 'witness_key_id': cfg['key_id'], 'replayed': False}

    def latest(self, domain, max_age=None):
        with self.db() as db:
            r = db.execute('SELECT * FROM checkpoints WHERE domain=? ORDER BY sequence DESC LIMIT 1', (domain,)).fetchone(); n = db.execute('SELECT COUNT(*) FROM checkpoints WHERE domain=?', (domain,)).fetchone()[0]
        cfg = self.config()
        if r is None:
            return {'domain': domain, 'latest': None, 'count': 0, 'witness_id': cfg['witness_id'], 'witness_key_id': cfg['key_id'], 'witness_public_hex': cfg['public_hex']}
        age = now() - r['received_at']
        return {'domain': domain, 'latest': {'sequence': r['sequence'], 'epoch': r['epoch'], 'statement': json.loads(r['statement_json']), 'acknowledgment': json.loads(r['ack_json']), 'witness_signature': r['ack_signature'], 'received_at': r['received_at']}, 'count': n, 'age_seconds': age,
                'fresh': (age <= max_age) if max_age else None, 'witness_id': cfg['witness_id'], 'witness_key_id': cfg['key_id'], 'witness_public_hex': cfg['public_hex']}

    def health(self):
        cfg = self.config()
        with self.db() as db:
            pins = db.execute('SELECT COUNT(*) FROM pins').fetchone()[0]; cps = db.execute('SELECT COUNT(*) FROM checkpoints').fetchone()[0]
        return {'protocol': WITNESS_PROTOCOL, 'witness_id': cfg['witness_id'], 'key_id': cfg['key_id'], 'public_hex': cfg['public_hex'], 'pinned_domains': pins, 'checkpoints': cps, 'pid': os.getpid(), 'trust': 'an independently stored, witness-signed record of acknowledged sequences; it detects a database older than an acknowledged checkpoint; it is not consensus and does not prevent loss'}


class Refusal(Exception):
    def __init__(self, code, detail=None):
        super().__init__(code); self.code = code; self.detail = detail


class Handler(BaseHTTPRequestHandler):
    store = None
    def log_message(self, *a):
        pass

    def _send(self, status, obj):
        data = json.dumps(obj).encode(); self.send_response(status); self.send_header('Content-Type', 'application/json'); self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data)

    def do_GET(self):
        if self.headers.get('Authorization') != 'Bearer ' + self.store.token():
            return self._send(401, {'error': 'unauthorized'})
        if self.path == '/health':
            return self._send(200, self.store.health())
        if self.path.startswith('/latest/'):
            return self._send(200, self.store.latest(self.path.split('/', 2)[2].split('?')[0]))
        return self._send(404, {'error': 'not found'})

    def do_POST(self):
        if self.headers.get('Authorization') != 'Bearer ' + self.store.token():
            return self._send(401, {'error': 'unauthorized'})
        n = int(self.headers.get('Content-Length') or 0)
        if n > 64 * 1024:
            return self._send(413, {'error': 'too large'})
        try:
            body = json.loads(self.rfile.read(n) or b'{}')
            fn = {'/pin': self.store.pin, '/checkpoint': self.store.checkpoint}.get(self.path)
            if fn is None:
                return self._send(404, {'error': 'not found'})
            return self._send(200, fn(body))
        except Refusal as exc:
            return self._send(400, {'error': True, 'code': exc.code, 'detail': exc.detail})
        except ValueError as exc:
            return self._send(400, {'error': True, 'code': 'bad_request'})


class WitnessClient:
    def __init__(self, base, token_file=None, token=None):
        import json as _j
        self.base = base.rstrip('/'); self.token = token or _j.load(open(token_file))['token']

    def call(self, method, path, body=None):
        import urllib.error, urllib.request
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method, headers={'Authorization': 'Bearer ' + self.token, 'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.status, json.loads(r.read() or b'{}')
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read() or b'{}')
            except ValueError:
                return e.code, {'error': True}
        except urllib.error.URLError:
            return 0, {'error': True, 'code': 'WITNESS_UNREACHABLE'}


def main(argv=None):
    ap = argparse.ArgumentParser(prog='metacoin-witness'); sub = ap.add_subparsers(dest='cmd', required=True)
    i = sub.add_parser('init'); i.add_argument('--home', required=True); i.add_argument('--witness-id')
    s = sub.add_parser('serve'); s.add_argument('--home', required=True); s.add_argument('--port', type=int, required=True); s.add_argument('--host', default='127.0.0.1')
    a = ap.parse_args(argv)
    if a.cmd == 'init':
        cfg = WitnessStore(a.home).init(a.witness_id); print(json.dumps({'witness_id': cfg['witness_id'], 'key_id': cfg['key_id'], 'public_hex': cfg['public_hex'], 'token_file': str(Path(a.home) / 'token')})); return 0
    Handler.store = WitnessStore(a.home); srv = ThreadingHTTPServer((a.host, a.port), Handler); srv.daemon_threads = True
    print(json.dumps({'witness': 'serving', 'port': a.port, 'pid': os.getpid()}), flush=True); srv.serve_forever()


if __name__ == '__main__':
    sys.exit(main())
