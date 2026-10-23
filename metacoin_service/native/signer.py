"""Operator-controlled actor signer (order 2026-10-23 group A): a separate executable that holds actor private keys and signs
canonical native intents. The API and workers store public identities and signed authorizations only; they never see a key.

    python -m metacoin_service.native.signer init  --home SIGNER_HOME --environment dev --domains dom-a,dom-b
    python -m metacoin_service.native.signer serve --home SIGNER_HOME --port 8501        # loopback HTTP, bearer token from SIGNER_HOME/token (0600)

Protocol (JSON over HTTP, loopback or an authorized host; token in Authorization: Bearer):
  GET  /health                         signer identity, custody, actors, policy, reachability facts
  POST /enroll     {name, ceiling}     create an actor key; returns public identity only
  POST /sign       {actor, intent, delegate?}   canonical intent -> Ed25519 signature; durable consumed-authorization state
  POST /release    {request_id}        the service reports that a signed authorization settled or lapsed (exposure released)
  POST /rotate     {actor, grace_seconds}       new key; old key retired (historical signatures stay verifiable; no new authorizations)
  POST /revoke     {actor, key_id}     immediate revocation of a key
  POST /delegate   {actor, name, recipients, ceiling}   bounded delegate key (recipient scope + aggregate authority <= parent remaining)
  GET  /keys/{actor}                   public key history (epochs, state, grace)
  POST /registration {actor, key_id}   a proof (signed by the current primary key) the service accepts to register a rotated or delegated key

Signing policy: the intent must be canonical (strict parse, exact field set, bounded integers, no floats), name this signer's
environment and one of its pinned domains, carry a sequence nonce one above the last signed for the account, an expiry in the
future, and fit the actor's exposure ceiling (sum of unreleased signed ceilings + this amount <= ceiling). An API-provided balance
is never consulted: the signer's own durable state and configured ceiling authorize. The signature is recorded BEFORE it is
returned, keyed by request id: a retry returns the same signature; a crash before the record leaves nothing consumed.

Cryptography: Ed25519 through metacoin_service.crypto (the cryptography library); nothing bespoke."""
import argparse
import hashlib
import json
import os
import secrets
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .. import crypto
from . import protocol as P

SIGNER_PROTOCOL = 'metacoin-signer/v1'
DEFAULT_GRACE = 3600
MAX_BODY = 64 * 1024
CUSTODY = 'external signer process (separate executable and key directory; the service holds public keys and signatures only)'


def now():
    return int(time.time())


class SignerStore:
    def __init__(self, home):
        self.home = Path(home); self.keys = self.home / 'keys'
        self.db_path = self.home / 'signer.sqlite'

    def init(self, environment, domains, signer_id=None):
        self.home.mkdir(mode=0o700, parents=True, exist_ok=True); os.chmod(self.home, 0o700); self.keys.mkdir(mode=0o700, exist_ok=True)
        cfg = {'signer_id': signer_id or ('signer-' + secrets.token_hex(4)), 'environment': environment, 'domains': domains, 'policy_version': P.POLICY_VERSION, 'created_at': now()}
        (self.home / 'config.json').write_text(json.dumps(cfg, indent=1)); os.chmod(self.home / 'config.json', 0o600)
        tok = self.home / 'token'
        if not tok.exists():
            fd = os.open(tok, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600); os.write(fd, json.dumps({'token': 'sgn_' + secrets.token_urlsafe(24)}).encode()); os.close(fd)
        with self.db() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS actors (name TEXT PRIMARY KEY, account TEXT, epoch INTEGER NOT NULL, ceiling INTEGER NOT NULL, state TEXT NOT NULL, created_at INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS keys (key_id TEXT PRIMARY KEY, actor TEXT NOT NULL, epoch INTEGER NOT NULL, kind TEXT NOT NULL, public_hex TEXT NOT NULL, key_file TEXT NOT NULL, scope_json TEXT, state TEXT NOT NULL, created_at INTEGER NOT NULL, retired_at INTEGER, grace_until INTEGER);
            CREATE TABLE IF NOT EXISTS signed (request_id TEXT PRIMARY KEY, actor TEXT NOT NULL, key_id TEXT NOT NULL, account TEXT NOT NULL, nonce TEXT NOT NULL, digest TEXT NOT NULL, signature TEXT NOT NULL, amount INTEGER NOT NULL, recipient TEXT NOT NULL, state TEXT NOT NULL, created_at INTEGER NOT NULL, released_at INTEGER, UNIQUE(account, nonce));
            CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY AUTOINCREMENT, at INTEGER NOT NULL, kind TEXT NOT NULL, detail TEXT NOT NULL);''')
        return cfg

    def config(self):
        return json.loads((self.home / 'config.json').read_text())

    def token(self):
        return json.loads((self.home / 'token').read_text())['token']

    def db(self):
        c = sqlite3.connect(self.db_path, isolation_level=None, timeout=30); c.row_factory = sqlite3.Row; c.execute('PRAGMA journal_mode=WAL'); c.execute('PRAGMA foreign_keys=ON')
        os.chmod(self.db_path, 0o600) if self.db_path.exists() else None
        return c

    def _event(self, db, kind, detail):
        db.execute('INSERT INTO events (at, kind, detail) VALUES (?,?,?)', (now(), kind, json.dumps(detail)))

    def _newkey(self, actor, epoch, kind):
        name = '%s-e%d-%s-%s.ed25519' % (actor, epoch, kind, secrets.token_hex(3)); path = self.keys / name
        pub = crypto.generate_signing_key(path); return name, pub, crypto.key_id_for(pub)

    # ---- operations ----------------------------------------------------------------------------------------------------------------
    def enroll(self, body):
        name = body.get('name'); ceiling = body.get('ceiling')
        if type(name) is not str or not 1 <= len(name) <= 64 or not name.replace('-', '').replace('_', '').isalnum():
            raise Refusal('name', 'alphanumeric, dash, underscore; 1..64')
        if type(ceiling) is not int or isinstance(ceiling, bool) or not 0 < ceiling <= P.MAX_AMOUNT:
            raise Refusal('ceiling', 'the signer\'s own exposure ceiling for this actor (integer base units) is required')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM actors WHERE name=?', (name,)).fetchone():
                raise Refusal('exists', 'actor already enrolled')
            kf, pub, kid = self._newkey(name, 1, 'primary')
            db.execute('INSERT INTO actors VALUES (?,?,?,?,?,?)', (name, body.get('account'), 1, ceiling, 'active', now()))
            db.execute('INSERT INTO keys VALUES (?,?,?,?,?,?,?,?,?,NULL,NULL)', (kid, name, 1, 'primary', pub, kf, None, 'active', now()))
            self._event(db, 'enroll', {'actor': name, 'key_id': kid, 'ceiling': ceiling}); db.execute('COMMIT')
        return {'actor': name, 'key_id': kid, 'public_hex': pub, 'epoch': 1, 'signer_id': self.config()['signer_id'], 'custody': CUSTODY, 'ceiling': ceiling}

    def bind_account(self, body):
        with self.db() as db:
            n = db.execute('UPDATE actors SET account=? WHERE name=?', (body.get('account'), body.get('actor'))).rowcount
        if not n:
            raise Refusal('unknown_actor')
        return {'actor': body['actor'], 'account': body['account']}

    def keys_of(self, actor):
        with self.db() as db:
            rows = [dict(r) for r in db.execute('SELECT key_id, epoch, kind, public_hex, scope_json, state, created_at, retired_at, grace_until FROM keys WHERE actor=? ORDER BY epoch, created_at', (actor,)).fetchall()]
            a = db.execute('SELECT * FROM actors WHERE name=?', (actor,)).fetchone()
        if a is None:
            raise Refusal('unknown_actor')
        for r in rows:
            r['scope'] = json.loads(r.pop('scope_json')) if r.get('scope_json') else None
        exposure = self.exposure(actor)
        return {'actor': actor, 'account': a['account'], 'epoch': a['epoch'], 'state': a['state'], 'ceiling': a['ceiling'], 'exposure_outstanding': exposure, 'keys': rows}

    def exposure(self, actor):
        with self.db() as db:
            return db.execute("SELECT COALESCE(SUM(amount),0) FROM signed WHERE actor=? AND state='signed'", (actor,)).fetchone()[0]

    def sign(self, raw):
        """raw: the request body bytes (parsed strictly here, never from a lenient parser)."""
        try:
            body = P.parse_strict(raw)
        except P.ProtocolError as exc:
            raise Refusal('encoding', exc.code)
        if type(body) is not dict or set(body) - {'actor', 'intent', 'delegate'}:
            raise Refusal('fields', 'actor, intent, delegate?')
        cfg = self.config(); intent = body.get('intent'); actor = body.get('actor')
        try:
            P.validate_intent(intent, cfg['environment'], intent.get('authority') if isinstance(intent, dict) else None)
        except P.ProtocolError as exc:
            raise Refusal('intent', {'code': exc.code, 'detail': exc.detail})
        if intent['authority'] not in cfg['domains']:
            raise Refusal('domain_not_pinned', {'authority': intent['authority'], 'pinned': cfg['domains']})
        if intent.get('policy_version') != cfg['policy_version']:
            raise Refusal('policy_version', {'expected': cfg['policy_version']})
        if 'expires_at' not in intent:
            raise Refusal('expiry_required', 'intents presented to the signer carry expires_at')
        if intent['expires_at'] <= now():
            raise Refusal('expired', {'expires_at': intent['expires_at']})
        amount = intent['amount_ceiling']; digest = P.digest(intent); msg = P.signing_bytes('intent', intent)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            a = db.execute('SELECT * FROM actors WHERE name=?', (actor,)).fetchone()
            if a is None or a['state'] != 'active':
                raise Refusal('unknown_or_revoked_actor')
            if a['account'] and intent['actor_account'] != a['account']:
                raise Refusal('account_mismatch', {'bound': a['account'], 'intent': intent['actor_account']})
            ex = db.execute('SELECT * FROM signed WHERE request_id=?', (intent['request_id'],)).fetchone()
            if ex is not None:
                if ex['digest'] != digest:
                    raise Refusal('request_id_reused', 'a different intent was already signed under this request id')
                db.execute('COMMIT'); return {'signature_hex': ex['signature'], 'key_id': ex['key_id'], 'digest': digest, 'replayed': True}
            # which key: a delegate (scoped) or the active primary
            if body.get('delegate'):
                k = db.execute("SELECT * FROM keys WHERE actor=? AND kind='delegate' AND key_id=? AND state='active'", (actor, body['delegate'])).fetchone()
                if k is None:
                    raise Refusal('delegate_unknown_or_revoked')
                scope = json.loads(k['scope_json'])
                if intent['recipient'] not in scope['recipients']:
                    raise Refusal('delegate_recipient_out_of_scope', {'recipient': intent['recipient'], 'permitted': scope['recipients']})
                used = db.execute("SELECT COALESCE(SUM(amount),0) FROM signed WHERE key_id=?", (k['key_id'],)).fetchone()[0]
                if used + amount > scope['ceiling']:
                    raise Refusal('delegate_ceiling', {'used': used, 'ceiling': scope['ceiling'], 'requested': amount})
            else:
                k = db.execute("SELECT * FROM keys WHERE actor=? AND kind='primary' AND state='active'", (actor,)).fetchone()
                if k is None:
                    raise Refusal('no_active_key')
            last = db.execute("SELECT nonce FROM signed WHERE account=? ORDER BY created_at DESC, rowid DESC LIMIT 1", (intent['actor_account'],)).fetchone()
            try:
                seq = int(intent['nonce'].rsplit('-', 1)[1]); prev = int(last['nonce'].rsplit('-', 1)[1]) if last else None
            except (ValueError, AttributeError):
                raise Refusal('nonce_format', 'nonce = <account>-<sequence>')
            if not intent['nonce'].startswith(intent['actor_account'] + '-') or (prev is not None and seq <= prev):
                raise Refusal('sequence', {'last_signed': prev, 'presented': seq, 'note': 'sequence must increase; a replayed or reordered intent is refused'})
            outstanding = db.execute("SELECT COALESCE(SUM(amount),0) FROM signed WHERE actor=? AND state='signed'", (actor,)).fetchone()[0]
            if outstanding + amount > a['ceiling']:
                raise Refusal('signer_exposure_ceiling', {'outstanding': outstanding, 'ceiling': a['ceiling'], 'requested': amount, 'note': 'the signer authorizes from its own durable exposure state; an API-provided balance is not authorization'})
            sig = crypto.sign(crypto.load_signing_key(self.keys / k['key_file']), msg)
            if os.environ.get('METACOIN_SIGNER_FAULT') == 'before_record':
                raise RuntimeError('FAULT INJECTED (signer test hook): crash after signing, before the durable record')
            db.execute('INSERT INTO signed VALUES (?,?,?,?,?,?,?,?,?,?,?,NULL)', (intent['request_id'], actor, k['key_id'], intent['actor_account'], intent['nonce'], digest, sig, amount, intent['recipient'], 'signed', now()))
            self._event(db, 'sign', {'actor': actor, 'request_id': intent['request_id'], 'amount': amount, 'recipient': intent['recipient'], 'key_id': k['key_id']})
            db.execute('COMMIT')
            if os.environ.get('METACOIN_SIGNER_FAULT') == 'after_record':
                raise RuntimeError('FAULT INJECTED (signer test hook): crash after the durable record, before returning')
        return {'signature_hex': sig, 'key_id': k['key_id'], 'digest': digest, 'replayed': False, 'exposure_outstanding_after': outstanding + amount}

    def release(self, body):
        with self.db() as db:
            n = db.execute("UPDATE signed SET state='released', released_at=? WHERE request_id=? AND state='signed'", (now(), body.get('request_id'))).rowcount
        return {'request_id': body.get('request_id'), 'released': bool(n)}

    def rotate(self, body):
        actor = body.get('actor'); grace = body.get('grace_seconds', DEFAULT_GRACE)
        if type(grace) is not int or not 0 <= grace <= 30 * 86400:
            raise Refusal('grace_seconds')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            a = db.execute('SELECT * FROM actors WHERE name=?', (actor,)).fetchone()
            if a is None:
                raise Refusal('unknown_actor')
            old = db.execute("SELECT * FROM keys WHERE actor=? AND kind='primary' AND state='active'", (actor,)).fetchone()
            new_epoch = a['epoch'] + 1; kf, pub, kid = self._newkey(actor, new_epoch, 'primary'); t = now()
            if old is not None:
                db.execute("UPDATE keys SET state='retired', retired_at=?, grace_until=? WHERE key_id=?", (t, t + grace, old['key_id']))
            db.execute('INSERT INTO keys VALUES (?,?,?,?,?,?,?,?,?,NULL,NULL)', (kid, actor, new_epoch, 'primary', pub, kf, None, 'active', t))
            db.execute('UPDATE actors SET epoch=? WHERE name=?', (new_epoch, actor))
            proof = None
            if old is not None:
                reg = {'type': 'key_registration', 'account': a['account'], 'actor': actor, 'key_id': kid, 'public_hex': pub, 'kind': 'rotation', 'epoch': new_epoch, 'scope': None, 'issued_at': t, 'grace_seconds': grace}
                proof = {'registration': reg, 'proof_signature': crypto.sign(crypto.load_signing_key(self.keys / old['key_file']), P.signing_bytes('rotation', reg)), 'proof_key_id': old['key_id']}
            self._event(db, 'rotate', {'actor': actor, 'old_key_id': old['key_id'] if old else None, 'new_key_id': kid, 'grace_seconds': grace}); db.execute('COMMIT')
        return {'actor': actor, 'key_id': kid, 'public_hex': pub, 'epoch': new_epoch, 'retired_key_id': old['key_id'] if old else None, 'grace_until': (t + grace) if old else None, 'registration': proof,
                'policy': 'historical signatures stay verifiable through the key history; the retired key cannot sign new authorizations after grace; a peer that has not received the rotation keeps honouring intents signed before it (disclosed; exposure bounded by previously funded authority)'}

    def revoke(self, body):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            k = db.execute('SELECT * FROM keys WHERE actor=? AND key_id=?', (body.get('actor'), body.get('key_id'))).fetchone()
            if k is None:
                raise Refusal('unknown_key')
            db.execute("UPDATE keys SET state='revoked', retired_at=COALESCE(retired_at, ?), grace_until=? WHERE key_id=?", (now(), now(), k['key_id']))
            if k['kind'] == 'primary' and not db.execute("SELECT 1 FROM keys WHERE actor=? AND kind='primary' AND state='active' AND key_id<>?", (k['actor'], k['key_id'])).fetchone():
                db.execute("UPDATE actors SET state='revoked' WHERE name=?", (k['actor'],))
            self._event(db, 'revoke', {'actor': k['actor'], 'key_id': k['key_id']}); db.execute('COMMIT')
        return {'actor': k['actor'], 'key_id': k['key_id'], 'state': 'revoked', 'note': 'a peer that has not learned the revocation cannot apply it; exposure stays bounded by what was funded before'}

    def delegate(self, body):
        actor = body.get('actor'); name = body.get('name'); recipients = body.get('recipients'); ceiling = body.get('ceiling')
        if type(name) is not str or not 1 <= len(name) <= 64 or type(recipients) is not list or not recipients or not all(type(r) is str for r in recipients) or type(ceiling) is not int or ceiling <= 0:
            raise Refusal('fields', 'name, recipients (non-empty list of accounts), ceiling (> 0)')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            a = db.execute('SELECT * FROM actors WHERE name=?', (actor,)).fetchone()
            if a is None or a['state'] != 'active':
                raise Refusal('unknown_or_revoked_actor')
            outstanding = db.execute("SELECT COALESCE(SUM(amount),0) FROM signed WHERE actor=? AND state='signed'", (actor,)).fetchone()[0]
            delegated = sum(json.loads(r['scope_json'])['ceiling'] for r in db.execute("SELECT scope_json FROM keys WHERE actor=? AND kind='delegate' AND state='active'", (actor,)).fetchall())
            if outstanding + delegated + ceiling > a['ceiling']:
                raise Refusal('delegation_exceeds_authority', {'actor_ceiling': a['ceiling'], 'outstanding': outstanding, 'already_delegated': delegated, 'requested': ceiling, 'note': 'a delegate can never widen the aggregate authority'})
            primary = db.execute("SELECT * FROM keys WHERE actor=? AND kind='primary' AND state='active'", (actor,)).fetchone()
            kf, pub, kid = self._newkey(actor, a['epoch'], 'delegate-' + name); scope = {'name': name, 'recipients': sorted(set(recipients)), 'ceiling': ceiling}; t = now()
            db.execute('INSERT INTO keys VALUES (?,?,?,?,?,?,?,?,?,NULL,NULL)', (kid, actor, a['epoch'], 'delegate', pub, kf, json.dumps(scope), 'active', t))
            reg = {'type': 'key_registration', 'account': a['account'], 'actor': actor, 'key_id': kid, 'public_hex': pub, 'kind': 'delegate', 'epoch': a['epoch'], 'scope': scope, 'issued_at': t, 'grace_seconds': 0}
            proof = {'registration': reg, 'proof_signature': crypto.sign(crypto.load_signing_key(self.keys / primary['key_file']), P.signing_bytes('rotation', reg)), 'proof_key_id': primary['key_id']}
            self._event(db, 'delegate', {'actor': actor, 'delegate': name, 'key_id': kid, 'scope': scope}); db.execute('COMMIT')
        return {'actor': actor, 'delegate': name, 'key_id': kid, 'public_hex': pub, 'scope': scope, 'registration': proof}

    def health(self):
        cfg = self.config()
        with self.db() as db:
            n = db.execute("SELECT COUNT(*) FROM actors WHERE state='active'").fetchone()[0]; signed = db.execute("SELECT COUNT(*) FROM signed").fetchone()[0]
        st = os.stat(self.keys)
        return {'protocol': SIGNER_PROTOCOL, 'signer_id': cfg['signer_id'], 'environment': cfg['environment'], 'domains': cfg['domains'], 'policy_version': cfg['policy_version'], 'custody': CUSTODY, 'actors_active': n, 'authorizations_recorded': signed,
                'key_dir_mode': oct(st.st_mode & 0o777), 'key_dir_owner_uid': st.st_uid, 'process_uid': os.getuid(), 'os_isolation': 'keys are readable only by uid %d (mode %s); the service process %s this uid' % (st.st_uid, oct(st.st_mode & 0o777), 'shares' if os.getuid() == st.st_uid else 'does not share'),
                'pid': os.getpid(), 'fault_hook': os.environ.get('METACOIN_SIGNER_FAULT') or None}


class Refusal(Exception):
    def __init__(self, code, detail=None):
        super().__init__(code); self.code = code; self.detail = detail


class Handler(BaseHTTPRequestHandler):
    store = None
    def log_message(self, *a):
        pass

    def _send(self, status, obj):
        data = json.dumps(obj).encode(); self.send_response(status); self.send_header('Content-Type', 'application/json'); self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data)

    def _auth(self):
        return self.headers.get('Authorization') == 'Bearer ' + self.store.token()

    def do_GET(self):
        if not self._auth():
            return self._send(401, {'error': 'unauthorized'})
        try:
            if self.path == '/health':
                return self._send(200, self.store.health())
            if self.path.startswith('/keys/'):
                return self._send(200, self.store.keys_of(self.path.split('/', 2)[2]))
            return self._send(404, {'error': 'not found'})
        except Refusal as exc:
            return self._send(400, {'error': True, 'code': exc.code, 'detail': exc.detail})

    def do_POST(self):
        if not self._auth():
            return self._send(401, {'error': 'unauthorized'})
        n = int(self.headers.get('Content-Length') or 0)
        if n > MAX_BODY:
            return self._send(413, {'error': True, 'code': 'body_too_large'})
        raw = self.rfile.read(n)
        try:
            if self.path == '/sign':
                return self._send(200, self.store.sign(raw))
            body = json.loads(raw or b'{}')
            fn = {'/enroll': self.store.enroll, '/release': self.store.release, '/rotate': self.store.rotate, '/revoke': self.store.revoke, '/delegate': self.store.delegate, '/bind': self.store.bind_account}.get(self.path)
            if fn is None:
                return self._send(404, {'error': 'not found'})
            return self._send(200, fn(body))
        except Refusal as exc:
            return self._send(400, {'error': True, 'code': exc.code, 'detail': exc.detail})
        except RuntimeError as exc:
            return self._send(500, {'error': True, 'code': 'signer_crash', 'detail': str(exc)[:200]})
        except ValueError as exc:
            return self._send(400, {'error': True, 'code': 'bad_request', 'detail': str(exc)[:100]})


class SignerClient:
    """Client used by the CLI, the provider client and tests. Never receives a private key."""
    def __init__(self, base, token_file=None, token=None):
        self.base = base.rstrip('/'); self.token = token or json.load(open(token_file))['token']

    def call(self, method, path, body=None, raw=None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(self.base + path, data=data, method=method, headers={'Authorization': 'Bearer ' + self.token, 'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read() or b'{}')
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read() or b'{}')
            except ValueError:
                return e.code, {'error': True, 'code': 'HTTP_%d' % e.code}
        except urllib.error.URLError:
            return 0, {'error': True, 'code': 'SIGNER_UNREACHABLE'}

    def sign(self, actor, intent, delegate=None):
        body = {'actor': actor, 'intent': intent}
        if delegate:
            body['delegate'] = delegate
        return self.call('POST', '/sign', raw=P.canonical(body))

    def health(self):
        return self.call('GET', '/health')


def serve(home, port, host='127.0.0.1'):
    Handler.store = SignerStore(home); Handler.store.config()
    srv = ThreadingHTTPServer((host, port), Handler); srv.daemon_threads = True
    print(json.dumps({'signer': 'serving', 'port': port, 'home': str(home), 'pid': os.getpid()}), flush=True)
    srv.serve_forever()


def main(argv=None):
    ap = argparse.ArgumentParser(prog='metacoin-signer'); sub = ap.add_subparsers(dest='cmd', required=True)
    i = sub.add_parser('init'); i.add_argument('--home', required=True); i.add_argument('--environment', default='dev'); i.add_argument('--domains', required=True); i.add_argument('--signer-id')
    s = sub.add_parser('serve'); s.add_argument('--home', required=True); s.add_argument('--port', type=int, required=True); s.add_argument('--host', default='127.0.0.1')
    h = sub.add_parser('health'); h.add_argument('--home', required=True)
    a = ap.parse_args(argv)
    if a.cmd == 'init':
        cfg = SignerStore(a.home).init(a.environment, a.domains.split(','), a.signer_id); print(json.dumps({'signer_id': cfg['signer_id'], 'home': a.home, 'token_file': str(Path(a.home) / 'token'), 'note': 'token is private (0600); never printed'})); return 0
    if a.cmd == 'serve':
        serve(a.home, a.port, a.host); return 0
    if a.cmd == 'health':
        print(json.dumps(SignerStore(a.home).health(), indent=1)); return 0


if __name__ == '__main__':
    sys.exit(main())
