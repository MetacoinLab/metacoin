"""Coordinator side of node federation: enrollment and credentials, request authentication, server-side claim and
leases, scoped input/checkpoint delivery, resumable bounded uploads, fenced checkpoint/result publication through
the same engine code the local worker uses, drain/disable/revoke, and observed-capability records."""
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path

from experiments.private_receipts import receipt as merkle
from .. import auth, history, scheduling
from ..compute import container, manifests as compute_manifests
from ..compute.engine import ComputeEngine
from ..db import now
from ..errors import ServiceError

NODE_KINDS = compute_manifests.KINDS
SIGNATURE_WINDOW_SECONDS = 120
CHUNK_MAX = 4 * 1024 * 1024


class NodePrincipal:
    """Authenticated node identity for the node routes (never a workspace principal)."""

    def __init__(self, row):
        self.row = row
        self.id, self.name = row['id'], row['name']
        self.capabilities = json.loads(row['capabilities_json'])
        self.workspaces = json.loads(row['workspaces_json'])
        self.lease_owner = 'node:' + row['id']


class CoordinatorProxy:
    """Stands in for the local Worker inside ComputeEngine when a node publishes: same fences, same store."""

    def __init__(self, database, store, settings, node):
        self.db, self.store, self.settings = database, store, settings
        self.worker_id = node.lease_owner
        self.name = 'node:' + node.name

    def _finish(self, job, result, error):
        from ..worker import Worker
        return Worker._finish(self, job, result, error)

    def _finish_in(self, db, job, result, error):
        from ..worker import Worker
        return Worker._finish_in(self, db, job, result, error)

    def _spec(self, db, job):
        from ..worker import Worker
        return Worker._spec(self, db, job)


class Federation:
    def __init__(self, database, store, settings):
        self.database, self.store, self.settings = database, store, settings
        self.upload_dir = Path(settings.home) / 'node_uploads'

    # ---- enrollment (operator) ------------------------------------------------------------------
    def enroll(self, db, principal, body):
        principal.require('node:admin')
        if type(body) is not dict or set(body) - {'name', 'public_key_hex', 'capabilities', 'devices', 'workspaces', 'expires_in_seconds', 'description'}:
            raise ServiceError('VALIDATION', 'fields: name, public_key_hex, capabilities, devices, workspaces, expires_in_seconds, description')
        name, pub = body.get('name'), body.get('public_key_hex')
        if type(name) is not str or not 1 <= len(name) <= 64 or not name.replace('-', '').replace('_', '').isalnum():
            raise ServiceError('VALIDATION', 'name')
        if type(pub) is not str or len(pub) != 64 or any(c not in '0123456789abcdef' for c in pub):
            raise ServiceError('VALIDATION', 'public_key_hex: 32-byte Ed25519 public key, hex')
        caps = body.get('capabilities') or list(NODE_KINDS)
        if type(caps) is not list or not caps or not set(caps) <= set(NODE_KINDS):
            raise ServiceError('VALIDATION', {'code': 'capabilities', 'allowed': list(NODE_KINDS), 'note': 'nodes execute allowlisted compute manifests only'})
        devices = body.get('devices') or ['cpu']
        if type(devices) is not list or not set(devices) <= {'cpu', 'cuda'}:
            raise ServiceError('VALIDATION', 'devices: subset of cpu, cuda')
        workspaces = body.get('workspaces') or [principal.workspace]
        if type(workspaces) is not list or not workspaces or any(type(w) is not str for w in workspaces) or not set(workspaces) <= {principal.workspace}:
            raise ServiceError('FORBIDDEN', 'a node may be granted only the enrolling operator\'s workspace')
        ttl = body.get('expires_in_seconds', 30 * 86400)
        if type(ttl) is not int or not 300 <= ttl <= 365 * 86400:
            raise ServiceError('VALIDATION', 'expires_in_seconds: 300..31536000')
        if db.execute('SELECT 1 FROM nodes WHERE public_key_hex=? AND state!=?', (pub, 'revoked')).fetchone():
            raise ServiceError('CONFLICT', 'public key already enrolled')
        nid = 'nd_' + secrets.token_hex(6)
        token = 'mcn_' + secrets.token_urlsafe(32)
        db.execute('INSERT INTO nodes (id, name, description, public_key_hex, secret_hash, capabilities_json, devices_json, workspaces_json, artifact_scope, state, enrolled_by, enrolled_at, expires_at, observed_json, trust_json) '
                   'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (nid, name, body.get('description', '')[:256], pub, auth._hash(db, token), json.dumps(sorted(set(caps))), json.dumps(sorted(set(devices))), json.dumps(workspaces),
                                                              'assigned-job-only', 'enrolled', principal.id, now(), now() + ttl, json.dumps({'completed_by_backend': {}}),
                                                              json.dumps({'transport': 'TLS with pinned local CA + node bearer credential + Ed25519 request signature (no mutual TLS)', 'plaintext': 'the node receives plaintext inputs for its assigned job and is part of the trust boundary',
                                                                          'attestation': 'none: declared devices are claims until observed through completed verified jobs'})))
        history.record(db, principal.workspace, principal.id, 'node.enrolled', 'node', nid, {'name': name, 'capabilities': sorted(set(caps)), 'devices': sorted(set(devices)), 'expires_at': now() + ttl})
        return {'node_id': nid, 'credential': token, 'note': 'shown once; store privately on the node together with its Ed25519 private key', 'expires_at': now() + ttl}

    def node(self, db, nid):
        row = db.execute('SELECT * FROM nodes WHERE id=?', (nid,)).fetchone()
        if row is None:
            raise ServiceError('NOT_FOUND', 'node')
        return row

    def view(self, db, row, private=False):
        out = {'id': row['id'], 'name': row['name'], 'description': row['description'], 'state': row['state'], 'capabilities': json.loads(row['capabilities_json']), 'devices_declared': json.loads(row['devices_json']),
               'workspaces': json.loads(row['workspaces_json']), 'artifact_scope': row['artifact_scope'], 'enrolled_at': row['enrolled_at'], 'expires_at': row['expires_at'], 'revoked_at': row['revoked_at'],
               'revocation_reason': row['revocation_reason'], 'last_seen': row['last_seen'], 'live': bool(row['last_seen']) and row['last_seen'] >= now() - scheduling.STALE_AFTER and row['state'] in ('enrolled', 'draining'),
               'current_job_id': row['current_job_id'], 'observed': json.loads(row['observed_json'] or '{}'), 'versions': json.loads(row['versions_json']) if row['versions_json'] else None,
               'trust': json.loads(row['trust_json'] or '{}'), 'public_key_hex': row['public_key_hex'], 'topology': 'same host as the coordinator unless the operator states otherwise; not multi-machine evidence'}
        return out

    def list(self, db, principal):
        principal.require('job:read')
        return [self.view(db, r) for r in db.execute('SELECT * FROM nodes ORDER BY enrolled_at').fetchall() if principal.workspace in json.loads(r['workspaces_json'])]

    def control(self, db, principal, nid, action, reason=''):
        principal.require('node:admin')
        row = self.node(db, nid)
        if principal.workspace not in json.loads(row['workspaces_json']):
            raise ServiceError('NOT_FOUND', 'node')
        if type(reason) is not str or len(reason) > 256:
            raise ServiceError('VALIDATION', 'reason')
        if action == 'drain':
            db.execute("UPDATE nodes SET state='draining' WHERE id=? AND state='enrolled'", (nid,))
            history.record(db, principal.workspace, principal.id, 'node.drained', 'node', nid, {'reason': reason})
        elif action == 'enable':
            if row['state'] in ('revoked',):
                raise ServiceError('CONFLICT', 'revoked nodes cannot be re-enabled; enroll a new identity')
            db.execute("UPDATE nodes SET state='enrolled' WHERE id=?", (nid,))
            history.record(db, principal.workspace, principal.id, 'node.enrolled', 'node', nid, {'re_enabled': True})
        elif action == 'disable':
            db.execute("UPDATE nodes SET state='disabled' WHERE id=? AND state!='revoked'", (nid,))
            history.record(db, principal.workspace, principal.id, 'node.drained', 'node', nid, {'disabled': True, 'reason': reason})
        elif action == 'revoke':
            db.execute("UPDATE nodes SET state='revoked', revoked_at=?, revocation_reason=? WHERE id=?", (now(), reason, nid))
            db.execute("UPDATE node_uploads SET state='aborted' WHERE node_id=? AND state IN ('open','complete')", (nid,))
            history.record(db, principal.workspace, principal.id, 'node.revoked', 'node', nid, {'reason': reason, 'note': 'historical evidence retained; plaintext already delivered cannot be recalled'})
        elif action == 'rotate':
            token = 'mcn_' + secrets.token_urlsafe(32)
            db.execute('UPDATE nodes SET secret_hash=?, credential_rotated_at=? WHERE id=?', (auth._hash(db, token), now(), nid))
            history.record(db, principal.workspace, principal.id, 'node.enrolled', 'node', nid, {'credential_rotated': True})
            return {'node_id': nid, 'credential': token, 'note': 'previous credential invalid immediately'}
        else:
            raise ServiceError('NOT_FOUND', 'action')
        return self.view(db, self.node(db, nid))

    # ---- node authentication --------------------------------------------------------------------
    def authenticate(self, db, request, body):
        token = request.headers.get('x-node-credential', '')
        sig = request.headers.get('x-node-signature', '')
        ts = request.headers.get('x-node-timestamp', '')
        nonce = request.headers.get('x-node-nonce', '')
        if not token.startswith('mcn_') or len(token) > 128 or not ts.isdigit() or not nonce or len(nonce) > 64:
            raise ServiceError('UNAUTHENTICATED')
        row = db.execute('SELECT * FROM nodes WHERE secret_hash=?', (auth._hash(db, token),)).fetchone()
        if row is None or not hmac.compare_digest(row['secret_hash'], auth._hash(db, token)):
            raise ServiceError('UNAUTHENTICATED')
        if row['state'] in ('revoked', 'disabled'):
            raise ServiceError('FORBIDDEN', {'code': 'node_' + row['state']})
        if row['expires_at'] <= now():
            raise ServiceError('UNAUTHENTICATED')
        if abs(int(ts) - now()) > SIGNATURE_WINDOW_SECONDS:
            raise ServiceError('UNAUTHENTICATED')
        if row['last_request_ts'] is not None:
            seen = json.loads(row['last_request_nonce'] or '[]')
            if int(ts) < row['last_request_ts'] or (int(ts) == row['last_request_ts'] and nonce in seen):
                raise ServiceError('UNAUTHENTICATED')      # replayed or older-than-latest request
        message = signing_message(request.method, request.url.path + ('?' + request.url.query if request.url.query else ''), body, ts, nonce)
        from .. import crypto
        if not crypto.verify(row['public_key_hex'], message, sig):
            raise ServiceError('UNAUTHENTICATED')
        seen = json.loads(row['last_request_nonce'] or '[]') if row['last_request_ts'] == int(ts) else []
        db.execute('UPDATE nodes SET last_seen=?, last_request_ts=?, last_request_nonce=? WHERE id=?', (now(), int(ts), json.dumps((seen + [nonce])[-64:]), row['id']))
        return NodePrincipal(row)

    # ---- node operations ---------------------------------------------------------------------------
    def register(self, db, node, body):
        versions = body.get('versions') if type(body) is dict else None
        devices = body.get('devices') if type(body) is dict else None
        if type(devices) is not list or not set(devices) <= set(json.loads(node.row['devices_json'])):
            raise ServiceError('FORBIDDEN', {'code': 'devices_exceed_enrollment', 'declared_at_enrollment': json.loads(node.row['devices_json'])})
        db.execute('UPDATE nodes SET versions_json=?, devices_reported_json=?, last_seen=? WHERE id=?', (json.dumps(versions)[:4000] if versions else None, json.dumps(devices), now(), node.id))
        return {'node_id': node.id, 'state': node.row['state'], 'capabilities': node.capabilities, 'devices': devices, 'lease_seconds': self.settings.limits['job_lease_seconds'],
                'heartbeat_seconds': scheduling.HEARTBEAT_SECONDS, 'chunk_max_bytes': CHUNK_MAX, 'max_artifact_bytes': self.settings.limits['compute_max_artifact_bytes'],
                'checkpoint_interval_seconds': self.settings.limits['compute_checkpoint_interval_seconds']}

    def heartbeat(self, db, node, body):
        cur = body.get('current_job_id') if type(body) is dict else None
        db.execute('UPDATE nodes SET last_seen=?, current_job_id=? WHERE id=?', (now(), cur, node.id))
        return {'state': db.execute('SELECT state FROM nodes WHERE id=?', (node.id,)).fetchone()['state']}

    def claim(self, db, node, body):
        """Server-side claim under the same fair order, restricted to the node's kinds, workspaces, devices and the
        contract's execution-location policy. Returns the assignment with the plaintext spec (input transfer)."""
        if node.row['state'] != 'enrolled':
            return {'job': None, 'reason': 'node is ' + node.row['state']}
        devices = json.loads(node.row['devices_reported_json'] or node.row['devices_json'])
        for cand in scheduling.fair_order(db, node.capabilities, location=None):
            if cand['workspace'] not in node.workspaces:
                continue
            job = db.execute("SELECT * FROM jobs WHERE id=? AND state='queued' AND cancel_requested=0 AND hold=0", (cand['id'],)).fetchone()
            if job is None:
                continue
            loc = json.loads(job['location_policy'] or '["local"]')
            if not ('*' in loc or node.id in loc or 'nodes' in loc):
                continue
            run = db.execute('SELECT * FROM compute_runs WHERE job_id=?', (job['id'],)).fetchone()
            if run is None:
                continue
            wanted = {'cpu': ['cpu'], 'gpu': ['cuda'], 'auto': ['cuda', 'cpu']}[run['device_policy']]
            backend = next((d for d in wanted if d in devices), None)
            if backend is None:
                continue
            generation = job['lease_generation'] + 1
            changed = db.execute("UPDATE jobs SET state='running', lease_owner=?, lease_expires=?, lease_generation=?, attempt=attempt+1, updated_at=? WHERE id=? AND lease_generation=? AND state='queued'",
                                 (node.lease_owner, now() + self.settings.limits['job_lease_seconds'], generation, now(), job['id'], job['lease_generation'])).rowcount
            if changed != 1:
                continue
            db.execute('INSERT INTO attempts VALUES (?,?,?,?,?,NULL,NULL)', ('at_' + secrets.token_hex(6), job['id'], generation, node.lease_owner, now()))
            db.execute("UPDATE compute_runs SET selected_backend=?, backend_reason=?, phase='admitted', updated_at=? WHERE job_id=?", (backend, 'federated node %s (%s): first device in policy order %s among the node\'s reported devices %s' % (node.id, node.name, wanted, devices), now(), job['id']))
            db.execute('UPDATE nodes SET current_job_id=? WHERE id=?', (job['id'], node.id))
            history.record(db, job['workspace'], node.lease_owner, 'node.claimed', 'job', job['id'], {'generation': generation, 'node_id': node.id, 'backend': backend})
            job = dict(db.execute('SELECT * FROM jobs WHERE id=?', (job['id'],)).fetchone())
            proxy = CoordinatorProxy(self.database, self.store, self.settings, node)
            contract, spec = proxy._spec(db, job)
            run = dict(db.execute('SELECT * FROM compute_runs WHERE job_id=?', (job['id'],)).fetchone())
            latest = db.execute("SELECT generation, digest FROM compute_checkpoints WHERE job_id=? AND state='published' ORDER BY generation DESC LIMIT 1", (job['id'],)).fetchone()
            tid = self._transfer(db, node, job, generation, 'input', len(merkle.canonical(spec['inputs'])), hashlib.sha256(merkle.canonical(spec['inputs'])).hexdigest())
            return {'job': {'id': job['id'], 'kind': job['kind'], 'workspace': job['workspace'], 'lease_generation': generation, 'lease_expires': job['lease_expires'], 'backend': backend,
                            'inputs': spec['inputs'], 'input_digest': run['input_digest'], 'manifest': compute_manifests.manifest(job['kind']), 'precision': run['precision'], 'attempt_generation': generation,
                            'resume_from_generation': latest['generation'] if latest else None, 'checkpoint_interval_seconds': self.settings.limits['compute_checkpoint_interval_seconds'], 'transfer_id': tid,
                            'limits': {'cpu_seconds': self.settings.limits['compute_cpu_seconds'], 'fsize_bytes': self.settings.limits['compute_max_artifact_bytes'], 'threads': self.settings.limits['compute_threads']}}}
        # expired leases on node-eligible work (a node that vanished, or this node after a crash) are recovered here
        for cand in db.execute("SELECT * FROM jobs WHERE state='running' AND lease_expires < ? ORDER BY lease_expires LIMIT 20", (now(),)).fetchall():
            if cand['kind'] not in node.capabilities or cand['workspace'] not in node.workspaces:
                continue
            loc = json.loads(cand['location_policy'] or '["local"]')
            if not ('*' in loc or node.id in loc or 'nodes' in loc):
                continue
            run = db.execute('SELECT * FROM compute_runs WHERE job_id=?', (cand['id'],)).fetchone()
            if run is None:
                continue
            wanted = {'cpu': ['cpu'], 'gpu': ['cuda'], 'auto': ['cuda', 'cpu']}[run['device_policy']]
            backend = next((d for d in wanted if d in devices), None)
            if backend is None:
                continue
            generation = cand['lease_generation'] + 1
            changed = db.execute("UPDATE jobs SET lease_owner=?, lease_expires=?, lease_generation=?, attempt=attempt+1, updated_at=? WHERE id=? AND lease_generation=? AND state='running' AND lease_expires < ?",
                                 (node.lease_owner, now() + self.settings.limits['job_lease_seconds'], generation, now(), cand['id'], cand['lease_generation'], now())).rowcount
            if changed != 1:
                continue
            db.execute('DELETE FROM compute_reservations WHERE job_id=?', (cand['id'],))
            db.execute('INSERT INTO attempts VALUES (?,?,?,?,?,NULL,NULL)', ('at_' + secrets.token_hex(6), cand['id'], generation, node.lease_owner, now()))
            db.execute("UPDATE compute_runs SET selected_backend=?, backend_reason=?, phase='admitted', updated_at=? WHERE job_id=?", (backend, 'federated node %s recovered an expired lease (generation %d)' % (node.id, generation), now(), cand['id']))
            db.execute('UPDATE nodes SET current_job_id=? WHERE id=?', (cand['id'], node.id))
            history.record(db, cand['workspace'], node.lease_owner, 'node.claimed', 'job', cand['id'], {'generation': generation, 'node_id': node.id, 'backend': backend, 'recovered_expired_lease': True})
            job = dict(db.execute('SELECT * FROM jobs WHERE id=?', (cand['id'],)).fetchone())
            proxy = CoordinatorProxy(self.database, self.store, self.settings, node)
            contract, spec = proxy._spec(db, job)
            latest = db.execute("SELECT generation FROM compute_checkpoints WHERE job_id=? AND state='published' ORDER BY generation DESC LIMIT 1", (job['id'],)).fetchone()
            tid = self._transfer(db, node, job, generation, 'input', len(merkle.canonical(spec['inputs'])), hashlib.sha256(merkle.canonical(spec['inputs'])).hexdigest())
            return {'job': {'id': job['id'], 'kind': job['kind'], 'workspace': job['workspace'], 'lease_generation': generation, 'lease_expires': job['lease_expires'], 'backend': backend,
                            'inputs': spec['inputs'], 'input_digest': run['input_digest'], 'manifest': compute_manifests.manifest(job['kind']), 'precision': run['precision'], 'attempt_generation': generation,
                            'resume_from_generation': latest['generation'] if latest else None, 'checkpoint_interval_seconds': self.settings.limits['compute_checkpoint_interval_seconds'], 'transfer_id': tid,
                            'limits': {'cpu_seconds': self.settings.limits['compute_cpu_seconds'], 'fsize_bytes': self.settings.limits['compute_max_artifact_bytes'], 'threads': self.settings.limits['compute_threads']}}}
        return {'job': None, 'reason': 'no eligible queued job for this node (kinds %s, workspaces %s, location policy)' % (node.capabilities, node.workspaces)}

    def _transfer(self, db, node, job, generation, role, size, digest, direction='to_node'):
        tid = 'tx_' + secrets.token_hex(6)
        db.execute('INSERT INTO node_transfers (id, node_id, job_id, attempt_generation, role, direction, bytes, sha256, state, created_at, completed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                   (tid, node.id, job['id'], generation, role, direction, size, digest, 'complete', now(), now()))
        history.record(db, job['workspace'], node.lease_owner, 'node.transfer', 'job', job['id'], {'transfer_id': tid, 'role': role, 'direction': direction, 'bytes': size})
        return tid

    def _assigned(self, db, node, job_id, generation):
        job = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if job is None or job['workspace'] not in node.workspaces:
            raise ServiceError('NOT_FOUND', 'job')
        if not (job['state'] == 'running' and job['lease_owner'] == node.lease_owner and job['lease_generation'] == generation):
            raise ServiceError('CONFLICT', {'code': 'stale_lease', 'note': 'this node no longer holds the lease for this generation; discard local output'})
        return dict(job)

    def renew(self, db, node, job_id, generation):
        changed = db.execute("UPDATE jobs SET lease_expires=?, updated_at=? WHERE id=? AND state='running' AND lease_owner=? AND lease_generation=?", (now() + self.settings.limits['job_lease_seconds'], now(), job_id, node.lease_owner, generation)).rowcount
        if changed != 1:
            raise ServiceError('CONFLICT', {'code': 'stale_lease'})
        ctl = db.execute('SELECT control FROM compute_runs WHERE job_id=?', (job_id,)).fetchone()
        cancel = db.execute('SELECT cancel_requested FROM jobs WHERE id=?', (job_id,)).fetchone()[0]
        return {'lease_expires': now() + self.settings.limits['job_lease_seconds'], 'control': 'cancel' if cancel else (ctl['control'] if ctl else None)}

    def progress(self, db, node, job_id, generation, body):
        self._assigned(db, node, job_id, generation)
        committed, total, chunk_id = int(body.get('committed', 0)), int(body.get('total', 0)), int(body.get('chunk_id', 0))
        db.execute("UPDATE compute_runs SET phase='running', work_computed=?, chunk_id=?, progress_json=?, versions_json=COALESCE(versions_json, ?), updated_at=? WHERE job_id=?",
                   (committed, chunk_id, json.dumps({'computed': committed, 'total': total, 'node_id': node.id}), json.dumps(body.get('versions')) if body.get('versions') else None, now(), job_id))
        return {'ok': True}

    def checkpoint_blob(self, db, node, job_id, generation):
        job = self._assigned(db, node, job_id, generation)
        latest = db.execute("SELECT * FROM compute_checkpoints WHERE job_id=? AND state='published' ORDER BY generation DESC LIMIT 1", (job_id,)).fetchone()
        if latest is None:
            raise ServiceError('NOT_FOUND', 'no published checkpoint')
        blob = self.store.load(db, latest['artifact_id'], job['workspace'])
        self._transfer(db, node, job, generation, 'checkpoint', len(blob), hashlib.sha256(blob).hexdigest())
        return latest['generation'], blob

    # ---- uploads ---------------------------------------------------------------------------------
    def upload_start(self, db, node, job_id, generation, body):
        self._assigned(db, node, job_id, generation)
        role, total, digest = body.get('role'), body.get('total_bytes'), body.get('sha256')
        if role not in ('checkpoint', 'result') or type(total) is not int or not 1 <= total <= self.settings.limits['compute_max_artifact_bytes'] or type(digest) is not str or len(digest) != 64:
            raise ServiceError('VALIDATION', 'role checkpoint|result, total_bytes within the artifact limit, sha256')
        open_n = db.execute("SELECT COUNT(*) FROM node_uploads WHERE node_id=? AND state='open'", (node.id,)).fetchone()[0]
        if open_n >= 4:
            raise ServiceError('RATE_LIMITED', 'too many open uploads for this node')
        uid = 'up_' + secrets.token_hex(8)
        self.upload_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = self.upload_dir / uid
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600); os.close(fd)
        db.execute('INSERT INTO node_uploads (id, node_id, job_id, attempt_generation, role, expected_sha256, total_bytes, received_bytes, path, state, created_at, updated_at, expires_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   (uid, node.id, job_id, generation, role, digest, total, 0, str(path), 'open', now(), now(), now() + 3600))
        return {'upload_id': uid, 'chunk_max_bytes': CHUNK_MAX, 'received_bytes': 0}

    def _upload(self, db, node, uid):
        up = db.execute('SELECT * FROM node_uploads WHERE id=? AND node_id=?', (uid, node.id)).fetchone()
        if up is None:
            raise ServiceError('NOT_FOUND', 'upload')
        if up['expires_at'] <= now() or up['state'] == 'aborted':
            raise ServiceError('CONFLICT', {'code': 'upload_' + ('expired' if up['state'] != 'aborted' else 'aborted')})
        return up

    def upload_chunk(self, db, node, uid, offset, data):
        up = self._upload(db, node, uid)
        self._assigned(db, node, up['job_id'], up['attempt_generation'])
        if up['state'] != 'open':
            raise ServiceError('CONFLICT', {'code': 'upload_not_open', 'state': up['state']})
        if len(data) == 0 or len(data) > CHUNK_MAX:
            raise ServiceError('PAYLOAD_TOO_LARGE', 'chunk')
        if offset < up['received_bytes']:
            return {'received_bytes': up['received_bytes'], 'note': 'chunk already received (idempotent)'}
        if offset != up['received_bytes']:
            raise ServiceError('CONFLICT', {'code': 'chunk_offset_gap', 'received_bytes': up['received_bytes']})
        if offset + len(data) > up['total_bytes']:
            raise ServiceError('VALIDATION', 'chunk exceeds the declared total')
        with open(up['path'], 'r+b') as f:
            f.seek(offset); f.write(data); f.flush(); os.fsync(f.fileno())
        db.execute('UPDATE node_uploads SET received_bytes=?, updated_at=? WHERE id=?', (offset + len(data), now(), uid))
        return {'received_bytes': offset + len(data)}

    def upload_complete(self, db, node, uid):
        up = self._upload(db, node, uid)
        self._assigned(db, node, up['job_id'], up['attempt_generation'])
        if up['state'] == 'complete':
            return {'upload_id': uid, 'state': 'complete'}
        if up['received_bytes'] != up['total_bytes']:
            raise ServiceError('CONFLICT', {'code': 'upload_incomplete', 'received_bytes': up['received_bytes'], 'total_bytes': up['total_bytes']})
        digest = hashlib.sha256(Path(up['path']).read_bytes()).hexdigest()
        if digest != up['expected_sha256']:
            db.execute("UPDATE node_uploads SET state='aborted', updated_at=? WHERE id=?", (now(), uid))
            try:
                os.unlink(up['path'])
            except FileNotFoundError:
                pass
            raise ServiceError('EVIDENCE_INVALID', {'code': 'upload_digest_mismatch'})
        db.execute("UPDATE node_uploads SET state='complete', updated_at=? WHERE id=?", (now(), uid))
        return {'upload_id': uid, 'state': 'complete', 'sha256': digest}

    def _take_upload(self, db, node, uid, job_id, generation, role):
        up = self._upload(db, node, uid)
        if up['state'] != 'complete' or up['job_id'] != job_id or up['attempt_generation'] != generation or up['role'] != role:
            raise ServiceError('CONFLICT', {'code': 'upload_not_usable', 'state': up['state'], 'role': up['role']})
        data = Path(up['path']).read_bytes()
        try:
            files = container.unpack(data)
        except Exception as exc:
            raise ServiceError('EVIDENCE_INVALID', {'code': 'container_invalid', 'reason': type(exc).__name__}) from None
        db.execute("UPDATE node_uploads SET state='consumed', updated_at=? WHERE id=?", (now(), uid))
        try:
            os.unlink(up['path'])
        except FileNotFoundError:
            pass
        self._transfer(db, node, {'id': job_id, 'workspace': db.execute('SELECT workspace FROM jobs WHERE id=?', (job_id,)).fetchone()[0]}, generation, role, len(data), hashlib.sha256(data).hexdigest(), direction='from_node')
        return files

    # ---- publication (fenced, same engine code as the local worker) ------------------------------------
    def publish_checkpoint(self, node, job_id, generation, body):
        with self.database.tx() as db:
            job = self._assigned(db, node, job_id, generation)
            files = self._take_upload(db, node, body.get('upload_id'), job_id, generation, 'checkpoint')
            run = dict(db.execute('SELECT * FROM compute_runs WHERE job_id=?', (job_id,)).fetchone())
        proxy = CoordinatorProxy(self.database, self.store, self.settings, node)
        engine = ComputeEngine(proxy)
        ev = {'generation': int(body['generation']), 'committed': int(body['committed']), 'reason': body.get('reason', 'interval')}
        ok = engine.publish_checkpoint_files(job, run, files, ev, run['selected_backend'] or 'cpu')
        if not ok:
            raise ServiceError('CONFLICT', {'code': 'checkpoint_rejected', 'note': 'fence or metadata binding failed; the node must not continue from unpublished state'})
        with self.database.read() as db:
            ctl = db.execute('SELECT control FROM compute_runs WHERE job_id=?', (job_id,)).fetchone()['control']
            cancel = db.execute('SELECT cancel_requested FROM jobs WHERE id=?', (job_id,)).fetchone()[0]
        return {'published': True, 'generation': ev['generation'], 'ack': 'cancel' if cancel else (ctl if ctl in ('pause', 'cancel') else 'continue')}

    def publish_result(self, node, job_id, generation, body):
        with self.database.tx() as db:
            job = self._assigned(db, node, job_id, generation)
            files = self._take_upload(db, node, body.get('upload_id'), job_id, generation, 'result')
            run = dict(db.execute('SELECT * FROM compute_runs WHERE job_id=?', (job_id,)).fetchone())
            proxy = CoordinatorProxy(self.database, self.store, self.settings, node)
            contract, spec = proxy._spec(db, job)
            db.execute('UPDATE compute_runs SET duration_ms=?, compute_ms=?, telemetry_json=?, updated_at=? WHERE job_id=?',
                       (body.get('duration_ms'), body.get('compute_ms'), json.dumps({'source': 'node-reported (not measured by the coordinator)', 'node_id': node.id, 'samples': 0}), now(), job_id))
        engine = ComputeEngine(proxy)
        man = compute_manifests.manifest(job['kind'])
        ev = {'committed': int(body['committed']), 'generation': int(body.get('generation', 0)), 'summary': body.get('summary') or {}, 'energy_delta_mJ_device_wide': body.get('energy_delta_mJ_device_wide'), 'peak_device_bytes': body.get('peak_device_bytes')}
        outcome = engine.complete_files(job, contract, spec, run, man, files, ev, run['selected_backend'] or 'cpu')
        with self.database.tx() as db:
            if outcome == 'succeeded':
                obs = json.loads(db.execute('SELECT observed_json FROM nodes WHERE id=?', (node.id,)).fetchone()['observed_json'] or '{}')
                obs.setdefault('completed_by_backend', {})
                obs['completed_by_backend'][run['selected_backend'] or 'cpu'] = obs['completed_by_backend'].get(run['selected_backend'] or 'cpu', 0) + 1
                obs['last_verified_job'] = job_id
                db.execute('UPDATE nodes SET observed_json=?, current_job_id=NULL WHERE id=?', (json.dumps(obs), node.id))
            else:
                db.execute('UPDATE nodes SET current_job_id=NULL WHERE id=?', (node.id,))
        return {'outcome': outcome, 'note': {'succeeded': 'verified and published by the coordinator', 'fenced': 'lease lost before publication; result discarded', 'failed': 'verification failed; evidence retained'}.get(outcome, outcome)}

    def fail(self, node, job_id, generation, body):
        code = body.get('code', 'COMPUTATION_ERROR')
        if code not in ('COMPUTATION_ERROR', 'TIMEOUT', 'RESOURCE_REJECTED', 'DEVICE_UNAVAILABLE', 'NUMERICAL_FAILURE', 'INPUT_INVALID', 'INTERNAL_DEFECT', 'CANCELLED'):
            code = 'COMPUTATION_ERROR'
        with self.database.tx() as db:
            job = self._assigned(db, node, job_id, generation)
            db.execute("UPDATE compute_runs SET phase=?, log_tail=?, updated_at=? WHERE job_id=?", ('interrupted' if code in ('COMPUTATION_ERROR', 'TIMEOUT') else 'failed', str(body.get('reason', ''))[:2000], now(), job_id))
            db.execute('UPDATE nodes SET current_job_id=NULL WHERE id=?', (node.id,))
        proxy = CoordinatorProxy(self.database, self.store, self.settings, node)
        return {'outcome': proxy._finish(job, None, code)}

    def paused(self, node, job_id, generation, body):
        proxy = CoordinatorProxy(self.database, self.store, self.settings, node)
        engine = ComputeEngine(proxy)
        with self.database.read() as db:
            job = self._assigned(db, node, job_id, generation)
        out = engine._pause(job, {'generation': body.get('generation'), 'committed': body.get('committed')})
        with self.database.tx() as db:
            db.execute('UPDATE nodes SET current_job_id=NULL WHERE id=?', (node.id,))
        return {'outcome': out}

    def cleanup_uploads(self, db):
        n = 0
        for up in db.execute("SELECT * FROM node_uploads WHERE state IN ('open','complete') AND expires_at <= ?", (now(),)).fetchall():
            try:
                os.unlink(up['path'])
            except FileNotFoundError:
                pass
            db.execute("UPDATE node_uploads SET state='aborted', updated_at=? WHERE id=?", (now(), up['id'])); n += 1
        return n


def signing_message(method, path, body, ts, nonce):
    return b'metacoin/node-request/v1\0' + method.upper().encode() + b'\n' + path.encode() + b'\n' + hashlib.sha256(body or b'').hexdigest().encode() + b'\n' + str(ts).encode() + b'\n' + str(nonce).encode()
