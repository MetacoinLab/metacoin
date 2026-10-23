"""Inter-domain value conservation (order §7–§9, §16): pinned peer roots, destination-bound allocations exported in durable
steps (reserve -> export commit -> certificate), imported once at the destination under the pinned trust policy, acknowledged,
returned only in full through the authenticated reverse protocol; authenticated envelopes carried by an online courier or a
file bundle (both drive the same transition rules); replay tombstones; authority key rotation with epochs; rollback
quarantine from a durable watermark kept outside the database, reconciled against peers' signed observations.

Nothing here imports x402, performs hidden network calls, or executes imported content. Every inbound item is parsed
strictly, bounded, verified under an explicitly pinned root, deduplicated, then applied inside a savepoint."""
import hashlib
import json
import os
import secrets

from .. import crypto, history
from ..db import now
from ..errors import ServiceError
from . import ledger
from . import protocol as P

MAX_BUNDLE_BYTES = 1 << 20          # one file bundle
FRAGMENT_BYTES = int(os.environ.get('METACOIN_FRAGMENT_BYTES', 16 * 1024))   # link fragmentation threshold for the transport layer (signed content is untouched)
ENVELOPE_VERSIONS = ('v0', 'v1')    # supported envelope versions; a bundle announces the sender's set and the receiver applies the highest common
MAX_BUNDLE_ITEMS = 200
MAX_PAYLOAD_BYTES = 64 * 1024       # one control message (artifact chunks never travel in envelopes)
MAX_OUTBOX_QUEUED = 5000            # bounded queue: exports are refused, never silently dropped, when it is full
ENVELOPE_TTL = int(os.environ.get('METACOIN_ENVELOPE_TTL', 30 * 86400))      # transport expiry; expiry never moves value
DEFAULT_ROTATION_GRACE = 7 * 86400  # messages signed by a retired key are accepted while created_at <= retired_at + grace
GC_DELIVERED_AFTER = 30 * 86400     # delivered envelopes may be garbage-collected; tombstones are never deleted

ROLLBACK_NOTE = ('rollback detection: a durable watermark (keys_dir/native/watermark-<domain>.json) written outside the database at every '
                 'inter-domain commitment, plus peers\' signed observations. A restore that also restores the watermark file is detected only '
                 'through a peer observation; a rollback with no peer and no watermark is undetectable (no independent witness exists).')


def _fault(db, settings, point):
    from ..economy.ops import fault
    fault(db, settings, point)


def _checkpoint(db):
    """Make the preceding step durable on its own: the next step is a separate crash boundary (recovery resumes from state)."""
    db.execute('COMMIT'); db.execute('BEGIN IMMEDIATE')


def _mixin(cls):
    # ---- durable watermark and quarantine -------------------------------------------------------------------------------
    def _watermark_path(self, did):
        return self._key_path('watermark-' + did).with_suffix('.json')

    def _watermark_write(self, db, workspace):
        d = db.execute('SELECT * FROM native_domains WHERE workspace=?', (workspace,)).fetchone()
        p = self._watermark_path(d['id'])
        p.write_text(json.dumps({'domain': d['id'], 'commit_sequence': d['commit_sequence'], 'export_watermark': d['export_watermark'], 'epoch': d['epoch'], 'updated_at': now()}))
        p.chmod(0o600)

    def _journal_root(self, db, workspace):
        r = db.execute('SELECT COUNT(*), COALESCE(MAX(rowid),0) FROM journal_entries WHERE workspace=?', (workspace,)).fetchone()
        last = db.execute('SELECT id, event_key FROM journal_entries WHERE workspace=? ORDER BY rowid DESC LIMIT 1', (workspace,)).fetchone()
        return P.digest({'entries': r[0], 'last_rowid': r[1], 'last': dict(last) if last else None})

    def checkpoint(self, db, workspace, send=True):
        """Sign a commit checkpoint of the current sequence (stored locally) and, when a witness is configured, obtain its acknowledgment.
        The acknowledgment is the external evidence restore detection relies on; without it the checkpoint is local only."""
        d = db.execute('SELECT * FROM native_domains WHERE workspace=?', (workspace,)).fetchone()
        st = {'type': 'commit_checkpoint', 'domain': d['id'], 'epoch': d['epoch'], 'sequence': d['commit_sequence'], 'journal_root': self._journal_root(db, workspace), 'environment': self.environment, 'issued_at': now()}
        sig, kid = self.authority_sign(db, workspace, 'commit_checkpoint', st)
        cid = 'ncp_' + secrets.token_hex(6)
        db.execute('INSERT INTO native_checkpoints VALUES (?,?,?,?,?,?,?,?,?,?,?,?)', (cid, workspace, d['id'], d['epoch'], d['commit_sequence'], json.dumps(st), sig, None, None, None, None, now()))
        out = {'checkpoint_id': cid, 'sequence': d['commit_sequence'], 'witness': None}
        if send and self.settings.witness_url and self.settings.witness_token_file:
            from .witness import WitnessClient
            try:
                code, ack = WitnessClient(self.settings.witness_url, self.settings.witness_token_file).call('POST', '/checkpoint', {'statement': st, 'signature_hex': sig, 'key_id': kid})
            except Exception as exc:
                code, ack = 0, {'error': type(exc).__name__}
            if code == 200:
                db.execute('UPDATE native_checkpoints SET witness_id=?, witness_ack_json=?, witness_signature=?, acknowledged_at=? WHERE id=?', (ack['acknowledgment']['witness_id'], json.dumps(ack['acknowledgment']), ack['witness_signature'], now(), cid))
                out['witness'] = {'witness_id': ack['acknowledgment']['witness_id'], 'acknowledged_sequence': ack['acknowledgment']['sequence']}
                wm = self._watermark_path(d['id']); data = json.loads(wm.read_text()) if wm.exists() else {}
                data.update({'witness_acknowledged_sequence': d['commit_sequence'], 'witness_id': ack['acknowledgment']['witness_id']}); wm.write_text(json.dumps(data)); wm.chmod(0o600)
            else:
                out['witness'] = {'unreachable_or_refused': True, 'code': ack.get('code'), 'detail': ack.get('detail')}
        return out

    def witness_latest(self, domain_id):
        """The witness's latest acknowledged sequence for this domain (None when no witness is configured or reachable)."""
        if not (self.settings.witness_url and self.settings.witness_token_file):
            return {'configured': False}
        from .witness import WitnessClient
        try:
            code, r = WitnessClient(self.settings.witness_url, self.settings.witness_token_file).call('GET', '/latest/' + domain_id)
        except Exception as exc:
            return {'configured': True, 'reachable': False, 'error': type(exc).__name__}
        if code != 200:
            return {'configured': True, 'reachable': False, 'code': r.get('code')}
        pinned = getattr(self.settings, 'witness_key_id', '') or ''
        ok_identity = (not pinned) or (r.get('witness_key_id') == pinned)
        return {'configured': True, 'reachable': True, 'identity_pinned_matches': ok_identity, 'latest_sequence': (r.get('latest') or {}).get('sequence'), 'age_seconds': r.get('age_seconds'), 'witness_id': r.get('witness_id'), 'fresh': (r.get('age_seconds') is not None and r['age_seconds'] <= (self.settings.witness_max_age_seconds or 86400))}

    def rollback_check(self, db, row):
        """Called by domain(): a committed sequence below the watermark means this database is older than commitments already published."""
        p = self._watermark_path(row['id']); wm = {}
        if p.exists():
            try:
                wm = json.loads(p.read_text())
            except (OSError, ValueError):
                wm = {}
        wseq = wm.get('witness_acknowledged_sequence')
        if wseq is None and row['recovery_state'] == 'normal' and self.settings.witness_url and not getattr(self, '_witness_checked', False):
            self._witness_checked = True
            w = self.witness_latest(row['id'])
            if w.get('reachable') and w.get('identity_pinned_matches', True) and w.get('latest_sequence') is not None:
                wseq = w['latest_sequence']
        if (wm.get('commit_sequence', 0) > row['commit_sequence'] or (wseq is not None and wseq > row['commit_sequence'])) and row['recovery_state'] == 'normal':
            try:
                db.execute("UPDATE native_domains SET recovery_state='quarantine', updated_at=? WHERE id=?", (now(), row['id']))
                history.record(db, row['workspace'], 'service', 'native.quarantine', 'native_domain', row['id'], {'reason': 'rollback_below_watermark_or_witness', 'database_sequence': row['commit_sequence'], 'watermark_sequence': wm.get('commit_sequence'), 'witness_acknowledged_sequence': wseq})
            except Exception:
                pass                                                                   # read-only connection: still report the state
            r = dict(row); r['recovery_state'] = 'quarantine'; return r
        return row

    def require_normal(self, d, what):
        if d['recovery_state'] != 'normal':
            raise ServiceError('CONFLICT', {'code': 'domain_quarantined', 'recovery_state': d['recovery_state'], 'refused': what, 'note': 'reconcile with peers\' observations (POST /native/recovery/reconcile) before outward commitments resume'})

    # ---- authority keys and peers ------------------------------------------------------------------------------------------
    def _record_local_key(self, db, d, key_file):
        if db.execute('SELECT 1 FROM native_authority_keys WHERE domain_id=? AND epoch=?', (d['id'], d['epoch'])).fetchone() is None:
            db.execute('INSERT INTO native_authority_keys VALUES (?,?,?,?,1,?,?,?,NULL,NULL,?)', (d['id'], d['epoch'], d['authority_key_id'], d['authority_public_hex'], key_file, 'active', d['created_at'], now()))

    def _current_key_path(self, db, d):
        self._record_local_key(db, d, 'authority-' + d['id'])
        r = db.execute("SELECT key_file FROM native_authority_keys WHERE domain_id=? AND epoch=? AND local=1", (d['id'], d['epoch'])).fetchone()
        return self._key_path(r['key_file'])

    def _sign_with(self, path, message_type, body):
        return crypto.sign(crypto.load_signing_key(path), P.signing_bytes(message_type, body))

    def pin_peer(self, db, principal, body):
        """Pin another domain's authority root explicitly (operator act). Nothing in a bundle can add or change a root."""
        principal.require('native:admin')
        if type(body) is not dict or set(body) - {'domain_id', 'authority_key_id', 'authority_public_hex', 'epoch', 'trust_policy'}:
            raise ServiceError('VALIDATION', {'code': 'fields', 'allowed': ['domain_id', 'authority_key_id', 'authority_public_hex', 'epoch', 'trust_policy']})
        d = self.domain(db, principal.workspace)
        pid = body.get('domain_id'); pub = body.get('authority_public_hex'); kid = body.get('authority_key_id'); epoch = body.get('epoch', 1)
        if type(pid) is not str or pid == d['id'] or not 1 <= len(pid) <= 48 or type(pub) is not str or len(pub) != 64 or crypto.key_id_for(pub) != kid or type(epoch) is not int or epoch < 1:
            raise ServiceError('VALIDATION', {'code': 'peer_identity', 'note': 'domain_id, the 32-byte Ed25519 public key (hex) and its key id (derived) are required; epoch >= 1'})
        policy = str(body.get('trust_policy') or 'pinned-by-operator: same operator and host, separate process and database')[:256]
        ex = db.execute('SELECT * FROM native_peers WHERE domain_id=? AND workspace=?', (pid, principal.workspace)).fetchone()
        if ex is not None:
            if ex['authority_public_hex'] != pub or ex['epoch'] != epoch:
                raise ServiceError('CONFLICT', {'code': 'peer_root_differs', 'note': 'a pinned root changes only through a rotation message signed by the retiring key, or by removing and re-pinning explicitly (not supported from a bundle)'})
            return self.peer_view(db, principal, pid)
        db.execute('INSERT INTO native_peers VALUES (?,?,?,?,?,?,?,?,?)', (pid, principal.workspace, kid, pub, epoch, policy, principal.id, now(), 'active'))
        db.execute('INSERT INTO native_authority_keys VALUES (?,?,?,?,0,NULL,?,?,NULL,NULL,?)', (pid, epoch, kid, pub, 'active', now(), now()))
        history.record(db, principal.workspace, principal.id, 'native.domain', 'native_peer', pid, {'pinned': kid, 'epoch': epoch})
        return self.peer_view(db, principal, pid)

    def peer_view(self, db, principal, pid):
        p = db.execute('SELECT * FROM native_peers WHERE domain_id=? AND workspace=?', (pid, principal.workspace)).fetchone()
        if p is None:
            raise ServiceError('NOT_FOUND', 'peer domain')
        keys = [dict(k) for k in db.execute('SELECT epoch, key_id, state, valid_from, retired_at, grace_until FROM native_authority_keys WHERE domain_id=? ORDER BY epoch', (pid,)).fetchall()]
        ex = db.execute("SELECT direction, state, COUNT(*) AS n, COALESCE(SUM(amount),0) AS total FROM native_allocations WHERE workspace=? AND (destination_domain=? OR source_domain=?) GROUP BY direction, state", (principal.workspace, pid, pid)).fetchall()
        return {'schema': 'metacoin-native-peer/v0', 'domain_id': pid, 'authority_key_id': p['authority_key_id'], 'authority_public_hex': p['authority_public_hex'], 'epoch': p['epoch'], 'trust_policy': p['trust_policy'], 'state': p['state'], 'pinned_at': p['pinned_at'], 'keys': keys,
                'allocations': [dict(r) for r in ex], 'queued_outbound': db.execute("SELECT COUNT(*) FROM native_envelopes WHERE workspace=? AND direction='out' AND receiver=? AND state='queued'", (principal.workspace, pid)).fetchone()[0]}

    def _peer(self, db, workspace, pid):
        p = db.execute('SELECT * FROM native_peers WHERE domain_id=? AND workspace=?', (pid, workspace)).fetchone()
        if p is None:
            raise ServiceError('CONFLICT', {'code': 'peer_not_pinned', 'domain': pid, 'note': 'pin the destination authority root first (POST /native/peers)'})
        return p

    def _verify_peer_signature(self, db, sender, key_id, message_type, body, sig_hex, signed_at):
        k = db.execute('SELECT * FROM native_authority_keys WHERE domain_id=? AND key_id=? AND local=0', (sender, key_id)).fetchone()
        if k is None:
            raise P.ProtocolError('unpinned_key', {'sender': sender, 'key_id': key_id, 'note': 'keys arriving with a bundle are never trusted; only operator-pinned roots and rotations signed by them'})
        if k['state'] == 'retired' and (k['grace_until'] is None or signed_at > k['grace_until']):
            raise P.ProtocolError('key_retired', {'key_id': key_id, 'retired_at': k['retired_at'], 'grace_until': k['grace_until']})
        if not crypto.verify(k['public_hex'], P.signing_bytes(message_type, body), sig_hex):
            raise P.ProtocolError('bad_signature', {'type': message_type})
        return k

    def rotate_authority(self, db, principal, body=None):
        """Next epoch: a new local key signs from now; the retiring key signs the rotation notice to every pinned peer and stays
        acceptable for messages created before retired_at + grace. A new key never signs for an earlier epoch."""
        principal.require('native:admin'); body = body or {}
        d = self.domain(db, principal.workspace); self.require_normal(d, 'authority rotation')
        grace = body.get('grace_seconds', DEFAULT_ROTATION_GRACE)
        if type(grace) is not int or not 0 <= grace <= 90 * 86400:
            raise ServiceError('VALIDATION', {'code': 'grace_seconds', 'allowed': '0..7776000'})
        old_path = self._current_key_path(db, d); new_epoch = d['epoch'] + 1
        name = 'authority-%s-e%d' % (d['id'], new_epoch); path, pub = self._ensure_key(name); kid = crypto.key_id_for(pub); t = now()
        db.execute("UPDATE native_authority_keys SET state='retired', retired_at=?, grace_until=? WHERE domain_id=? AND epoch=? AND local=1", (t, t + grace, d['id'], d['epoch']))
        db.execute('INSERT INTO native_authority_keys VALUES (?,?,?,?,1,?,?,?,NULL,NULL,?)', (d['id'], new_epoch, kid, pub, name, 'active', t, t))
        db.execute('UPDATE native_domains SET authority_key_id=?, authority_public_hex=?, epoch=?, updated_at=? WHERE id=?', (kid, pub, new_epoch, t, d['id']))
        notice = {'protocol': P.PROTOCOL, 'type': 'rotation', 'authority': d['id'], 'environment': self.environment, 'old_epoch': d['epoch'], 'new_epoch': new_epoch, 'old_key_id': d['authority_key_id'], 'new_key_id': kid, 'new_public_hex': pub, 'grace_seconds': grace, 'rotated_at': t}
        sig = self._sign_with(old_path, 'rotation', notice)
        for p in db.execute("SELECT domain_id FROM native_peers WHERE workspace=? AND state='active'", (principal.workspace,)).fetchall():
            self._outbox(db, principal.workspace, p['domain_id'], 'rotation', notice, None, payload_signature=sig, key_id=d['authority_key_id'], key_path=old_path)
        self._watermark_write(db, principal.workspace)
        history.record(db, principal.workspace, principal.id, 'native.domain', 'native_domain', d['id'], {'rotated_to_epoch': new_epoch, 'new_key_id': kid, 'grace_seconds': grace})
        return self.domain_view(db, principal)

    # ---- envelopes and outbox ---------------------------------------------------------------------------------------------
    def _outbox(self, db, workspace, receiver, message_type, payload, causality, payload_signature=None, key_id=None, key_path=None):
        d = self.domain(db, workspace)
        if db.execute("SELECT COUNT(*) FROM native_envelopes WHERE workspace=? AND direction='out' AND state='queued'", (workspace,)).fetchone()[0] >= MAX_OUTBOX_QUEUED:
            raise ServiceError('BUDGET_EXHAUSTED', {'code': 'outbox_full', 'limit': MAX_OUTBOX_QUEUED, 'note': 'deliver or garbage-collect before new inter-domain commitments'})
        key_path = key_path or self._current_key_path(db, d); key_id = key_id or d['authority_key_id']
        if payload_signature is None:
            payload_signature = self._sign_with(key_path, message_type, payload)
        eid = 'nenv_' + secrets.token_hex(8); t = now()
        if len(P.canonical(payload)) > MAX_PAYLOAD_BYTES:
            raise ServiceError('VALIDATION', {'code': 'payload_too_large', 'limit': MAX_PAYLOAD_BYTES, 'note': 'economic control messages are small by design; artifacts travel outside envelopes'})
        env = {'protocol': P.PROTOCOL, 'type': 'envelope', 'id': eid, 'environment': self.environment, 'sender': d['id'], 'receiver': receiver, 'message_type': message_type, 'causality': causality, 'payload_digest': P.digest(payload), 'expires_at': t + ENVELOPE_TTL, 'created_at': t}
        P.validate_envelope(env)
        esig = self._sign_with(key_path, 'envelope', env)
        item = {'envelope': env, 'envelope_signature': esig, 'key_id': key_id, 'payload': payload, 'payload_signature': payload_signature}
        db.execute('INSERT INTO native_envelopes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (eid, workspace, 'out', d['id'], receiver, message_type, causality, json.dumps(payload), env['payload_digest'], json.dumps(item), esig, 'queued', 0, None, env['expires_at'], t, t))
        return eid

    def outbox(self, db, principal, receiver=None, transport='online', include_delivered=False):
        principal.require('native:transport')
        d = self.domain(db, principal.workspace)
        if transport == 'online' and d['connectivity'] != 'online':
            raise ServiceError('CONFLICT', {'code': 'link_cut', 'connectivity': d['connectivity'], 'note': 'the online adapter is unavailable; export a file bundle instead'})
        q = "SELECT * FROM native_envelopes WHERE workspace=? AND direction='out'" + ("" if include_delivered else " AND state='queued'") + (" AND receiver=?" if receiver else "") + " ORDER BY created_at LIMIT ?"
        rows = db.execute(q, (principal.workspace,) + ((receiver,) if receiver else ()) + (MAX_BUNDLE_ITEMS,)).fetchall()
        items = []
        for r in rows:
            it = json.loads(r['envelope_json']); it['state'] = r['state']; it['attempts'] = r['attempts']; items.append(it)
        economic_ids = sorted({it['envelope']['causality'] for it in items if it['envelope'].get('causality')})
        items = self._fragment_items(items)
        return {'schema': 'metacoin-native-bundle/v1', 'protocol': P.PROTOCOL, 'envelope_versions': list(ENVELOPE_VERSIONS), 'sender': d['id'], 'sender_key_id': d['authority_key_id'], 'receiver': receiver, 'environment': self.environment, 'items': items, 'created_at': now(),
                'economic_ids': economic_ids,
                'note': 'the receiver verifies every item under its own pinned roots; nothing in this bundle establishes trust; fragments reassemble to the exact signed content or are discarded'}

    def _fragment_items(self, items):
        """Link fragmentation: an item larger than FRAGMENT_BYTES travels as numbered fragments of its canonical JSON; the receiver reassembles
        and then verifies the ORIGINAL signatures, so fragmentation can never change signed economic content."""
        out = []
        for it in items:
            raw = P.canonical({k: v for k, v in it.items() if k not in ('state', 'attempts')})
            if len(raw) <= FRAGMENT_BYTES:
                out.append(it); continue
            mid = it['envelope']['id']; chunks = [raw[i:i + FRAGMENT_BYTES] for i in range(0, len(raw), FRAGMENT_BYTES)]
            for i, c in enumerate(chunks):
                out.append({'fragment_of': mid, 'index': i, 'count': len(chunks), 'data': c.decode('ascii'), 'digest': hashlib.sha256(raw).hexdigest()})
        return out

    def outbox_mark(self, db, principal, body):
        """The courier reports the receiver's per-item outcome; delivered items stop being resent. Rejections are recorded with the reason."""
        principal.require('native:transport')
        results = body.get('results') if type(body) is dict else None
        if type(results) is not list or len(results) > MAX_BUNDLE_ITEMS:
            raise ServiceError('VALIDATION', {'code': 'results'})
        out = []
        for r in results:
            if type(r) is not dict or type(r.get('id')) is not str or r.get('state') not in ('accepted', 'duplicate', 'rejected'):
                raise ServiceError('VALIDATION', {'code': 'result_item', 'allowed_states': ['accepted', 'duplicate', 'rejected']})
            st = 'delivered' if r['state'] in ('accepted', 'duplicate') else 'rejected'
            n = db.execute("UPDATE native_envelopes SET state=?, reason=?, attempts=attempts+1, updated_at=? WHERE id=? AND workspace=? AND direction='out' AND state='queued'", (st, str(r.get('reason') or '')[:200] or None, now(), r['id'], principal.workspace)).rowcount
            out.append({'id': r['id'], 'state': st, 'updated': bool(n)})
        return {'marked': out}

    def transport_gc(self, db, principal):
        principal.require('native:transport')
        n = db.execute("DELETE FROM native_envelopes WHERE workspace=? AND direction='out' AND state='delivered' AND updated_at < ?", (principal.workspace, now() - GC_DELIVERED_AFTER)).rowcount
        return {'deleted_delivered_envelopes': n, 'tombstones_kept': db.execute('SELECT COUNT(*) FROM native_tombstones WHERE workspace=?', (principal.workspace,)).fetchone()[0], 'rule': 'tombstones (anti-replay evidence) are never deleted; only delivered outbound copies older than %d days' % (GC_DELIVERED_AFTER // 86400)}

    # ---- inbox: the single set of transition rules for both adapters -----------------------------------------------------------
    def inbox(self, db, principal, bundle, transport='online', raw_len=None):
        principal.require('native:transport')
        d = self.domain(db, principal.workspace)
        if transport == 'online' and d['connectivity'] != 'online':
            raise ServiceError('CONFLICT', {'code': 'link_cut', 'connectivity': d['connectivity']})
        if raw_len is not None and raw_len > MAX_BUNDLE_BYTES:
            raise ServiceError('VALIDATION', {'code': 'bundle_too_large', 'limit': MAX_BUNDLE_BYTES})
        if type(bundle) is not dict or type(bundle.get('items')) is not list:
            raise ServiceError('VALIDATION', {'code': 'bundle_shape'})
        if len(bundle['items']) > MAX_BUNDLE_ITEMS:
            raise ServiceError('VALIDATION', {'code': 'too_many_items', 'limit': MAX_BUNDLE_ITEMS})
        offered = bundle.get('envelope_versions') or ['v0']
        common = [v for v in ENVELOPE_VERSIONS if v in offered]
        if not common:
            raise ServiceError('VALIDATION', {'code': 'envelope_version_unsupported', 'offered': offered, 'supported': list(ENVELOPE_VERSIONS)})
        negotiated = common[-1]
        items, frag_results = self._reassemble(db, principal, bundle['items'])
        results, replies = list(frag_results), []
        for n, item in enumerate(items):
            db.execute('SAVEPOINT item')
            try:
                res = self._apply_item(db, principal, d, item)
                db.execute('RELEASE item')
            except (ServiceError, P.ProtocolError) as exc:
                db.execute('ROLLBACK TO item'); db.execute('RELEASE item')
                code = exc.code if isinstance(exc, P.ProtocolError) else (exc.detail.get('code') if isinstance(exc.detail, dict) else exc.code)
                res = {'id': (item.get('envelope') or {}).get('id') if isinstance(item, dict) and isinstance(item.get('envelope'), dict) else None, 'state': 'rejected', 'reason': code, 'detail': exc.detail if isinstance(exc.detail, dict) else None}
                self._record_rejection(db, principal.workspace, d['id'], item, code)
                q = exc.detail.get('quarantine') if isinstance(exc.detail, dict) else None
                if q:                                                                  # the safety state is written AFTER the rollback so it survives the rejected item
                    self._quarantine(db, principal, d, q['peer'], q['allocation_id'], code, q)
            res['index'] = n; results.append(res)
            replies.extend(res.pop('replies', []))
        return {'domain': d['id'], 'envelope_version': negotiated, 'results': results, 'replies': replies, 'accepted': sum(r['state'] == 'accepted' for r in results), 'duplicate': sum(r['state'] == 'duplicate' for r in results), 'rejected': sum(r['state'] == 'rejected' for r in results)}

    def _reassemble(self, db, principal, items):
        """Stage fragments durably (reordered and duplicated fragments are fine); emit the reassembled item only when every fragment is present
        and the digest matches; a mismatching reassembly is discarded, never applied."""
        out, results = [], []
        for it in items:
            if not (isinstance(it, dict) and it.get('fragment_of')):
                out.append(it); continue
            mid, idx, cnt, data, dg = it.get('fragment_of'), it.get('index'), it.get('count'), it.get('data'), it.get('digest')
            if type(mid) is not str or type(idx) is not int or type(cnt) is not int or not 0 <= idx < cnt <= 256 or type(data) is not str or len(data) > FRAGMENT_BYTES:
                results.append({'id': mid, 'state': 'rejected', 'reason': 'fragment_shape'}); continue
            db.execute('INSERT OR IGNORE INTO native_fragments VALUES (?,?,?,?,?,?,?)', (mid, principal.workspace, '?', idx, cnt, data, now()))
            have = db.execute('SELECT idx, data FROM native_fragments WHERE message_id=? AND workspace=? ORDER BY idx', (mid, principal.workspace)).fetchall()
            if len(have) < cnt:
                results.append({'id': mid, 'state': 'fragment_staged', 'have': len(have), 'count': cnt}); continue
            raw = ''.join(r['data'] for r in have).encode('ascii')
            if hashlib.sha256(raw).hexdigest() != dg:
                db.execute('DELETE FROM native_fragments WHERE message_id=? AND workspace=?', (mid, principal.workspace))
                results.append({'id': mid, 'state': 'rejected', 'reason': 'fragment_digest_mismatch'}); continue
            try:
                whole = P.parse_strict(raw)
            except P.ProtocolError as exc:
                results.append({'id': mid, 'state': 'rejected', 'reason': 'fragment_reassembly_' + exc.code}); continue
            db.execute('DELETE FROM native_fragments WHERE message_id=? AND workspace=?', (mid, principal.workspace))
            out.append(whole)
        return out, results

    def _record_rejection(self, db, workspace, did, item, code):
        try:
            env = item.get('envelope') if isinstance(item, dict) else None
            orig = env.get('id') if isinstance(env, dict) and type(env.get('id')) is str and len(env['id']) <= 64 else None
            eid = 'rej_' + secrets.token_hex(8)                                       # rejected copies never occupy the envelope id (a later genuine delivery may use it)
            db.execute('INSERT INTO native_envelopes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (eid, workspace, 'in', str(env.get('sender') if isinstance(env, dict) else '?')[:64], did, str(env.get('message_type') if isinstance(env, dict) else '?')[:32], orig, '{}', '', json.dumps(item)[:MAX_PAYLOAD_BYTES] if isinstance(item, dict) else '{}', '', 'rejected', 1, code, 0, now(), now()))
        except Exception:
            pass

    def _apply_item(self, db, principal, d, item):
        if type(item) is not dict or set(item) - {'envelope', 'envelope_signature', 'key_id', 'payload', 'payload_signature', 'state', 'attempts'}:
            raise P.ProtocolError('item_shape')
        env = item.get('envelope'); payload = item.get('payload')
        if len(P.canonical(payload)) > MAX_PAYLOAD_BYTES:
            raise P.ProtocolError('payload_too_large', {'limit': MAX_PAYLOAD_BYTES})
        P.validate_envelope(env)
        if env['receiver'] != d['id']:
            raise P.ProtocolError('wrong_receiver', {'addressed_to': env['receiver'], 'this_domain': d['id']})
        if env['environment'] != self.environment:
            raise P.ProtocolError('wrong_environment')
        if env['sender'] == d['id']:
            raise P.ProtocolError('self_addressed')
        if env['payload_digest'] != P.digest(payload):
            raise P.ProtocolError('payload_digest_mismatch')
        kid, esig, psig = item.get('key_id'), item.get('envelope_signature'), item.get('payload_signature')
        if type(kid) is not str or type(esig) is not str or type(psig) is not str:
            raise P.ProtocolError('signature_fields')
        peer = db.execute('SELECT * FROM native_peers WHERE domain_id=? AND workspace=?', (env['sender'], principal.workspace)).fetchone()
        if peer is None:
            raise P.ProtocolError('sender_not_pinned', {'sender': env['sender']})
        if peer['state'] == 'quarantined' and env['message_type'] not in ('observation',):
            raise P.ProtocolError('peer_quarantined', {'sender': env['sender']})
        key = self._verify_peer_signature(db, env['sender'], kid, 'envelope', env, esig, env['created_at'])
        self._verify_peer_signature(db, env['sender'], kid, env['message_type'], payload, psig, env['created_at'])
        if now() > env['expires_at']:
            raise P.ProtocolError('expired', {'expires_at': env['expires_at'], 'note': 'transport expiry only; no value moved or restored'})
        P.validate_message(env['message_type'], payload, self.environment)
        tomb = 'env:' + env['id']
        if db.execute('SELECT 1 FROM native_tombstones WHERE key=?', (tomb,)).fetchone():
            return {'id': env['id'], 'state': 'duplicate', 'reason': 'envelope_seen', 'replies': self._replay_replies(db, principal.workspace, env)}
        handler = {'allocation_certificate': self._on_certificate, 'import_ack': self._on_ack, 'return_request': self._on_return_request, 'return_certificate': self._on_return_certificate, 'rotation': self._on_rotation, 'observation': self._on_observation}[env['message_type']]
        out = handler(db, principal, d, env, payload, psig, key)
        db.execute('INSERT INTO native_tombstones VALUES (?,?,?,?,?)', (tomb, principal.workspace, env['message_type'], env['causality'] or env['id'], now()))
        db.execute('INSERT INTO native_envelopes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (env['id'], principal.workspace, 'in', env['sender'], d['id'], env['message_type'], env['causality'], json.dumps(payload), env['payload_digest'], json.dumps(item), esig, out.get('state', 'accepted'), 1, out.get('reason'), env['expires_at'], now(), now()))
        history.record(db, principal.workspace, principal.id, 'native.envelope', 'native_envelope', env['id'], {'from': env['sender'], 'type': env['message_type'], 'result': out.get('state', 'accepted'), 'causality': env['causality']})
        return dict({'id': env['id'], 'state': 'accepted'}, **out)

    def _replay_replies(self, db, workspace, env):
        """A duplicate certificate delivery gets its acknowledgment again (a lost ack is the sender's most likely reason to resend)."""
        if env['message_type'] != 'allocation_certificate':
            return []
        r = db.execute("SELECT envelope_json FROM native_envelopes WHERE workspace=? AND direction='out' AND message_type='import_ack' AND causality=?", (workspace, env['causality'])).fetchone()
        return [json.loads(r['envelope_json'])] if r else []

    # ---- claim lineage at a destination (order 2026-10-23 group F) --------------------------------------------------------------
    # Rule (documented): a unit imported under an allocation keeps that allocation's lineage while it sits unspent in the recipient
    # account. Spending draws first from the account's unlineaged units (locally earned or received), then from its claims oldest
    # first; a claim with any consumption is nonreturnable (partial return unsupported). A unit transferred to another local
    # account loses its lineage. Returnable funds are read from these records, never inferred from the fungible balance.
    def _consume_claims(self, db, workspace, account, asset, amount, ref_type, ref_id):
        rows = db.execute("SELECT * FROM native_claim_lineage WHERE workspace=? AND account_id=? AND asset=? AND state IN ('active','partially_consumed') ORDER BY imported_sequence", (workspace, account, asset)).fetchall()
        if not rows:
            return []
        d = self.domain(db, workspace)
        avail = ledger.balance(db, workspace, asset, d['id'], account, 'available')               # before this spend's reservation
        lineaged = sum(r['amount'] - r['consumed'] for r in rows)
        unlineaged = max(avail - lineaged, 0)
        remaining = max(amount - unlineaged, 0); consumed = []
        for r in rows:
            if remaining <= 0:
                break
            take = min(remaining, r['amount'] - r['consumed'])
            if take <= 0:
                continue
            new_c = r['consumed'] + take; state = 'consumed' if new_c == r['amount'] else 'partially_consumed'
            db.execute('UPDATE native_claim_lineage SET consumed=?, state=?, updated_at=? WHERE allocation_id=?', (new_c, state, now(), r['allocation_id']))
            db.execute('INSERT INTO native_claim_consumption (workspace, allocation_id, amount, ref_type, ref_id, created_at) VALUES (?,?,?,?,?,?)', (workspace, r['allocation_id'], take, ref_type, ref_id, now()))
            consumed.append({'allocation_id': r['allocation_id'], 'amount': take, 'state': state}); remaining -= take
        return consumed

    def lineage_view(self, db, principal, account):
        principal.require('native:read')
        rows = [dict(r) for r in db.execute('SELECT * FROM native_claim_lineage WHERE workspace=? AND account_id=? ORDER BY imported_sequence', (principal.workspace, account)).fetchall()]
        for r in rows:
            r['returnable'] = r['amount'] if r['consumed'] == 0 and r['state'] == 'active' else 0
            r['consumptions'] = [dict(c) for c in db.execute('SELECT amount, ref_type, ref_id, created_at FROM native_claim_consumption WHERE allocation_id=? ORDER BY id', (r['allocation_id'],)).fetchall()]
        return {'account_id': account, 'claims': rows, 'returnable_total': sum(r['returnable'] for r in rows),
                'rule': 'spending consumes unlineaged units first, then claims oldest first; any consumption makes a claim nonreturnable; transferred units lose lineage'}

    def accounting_view(self, db, principal, asset=None):
        """Independent accounting over source backing, active claims, retired claims and returns (counts and sums from the allocation and lineage records, cross-checked with the journal audit)."""
        principal.require('native:read'); ws = principal.workspace; asset = asset or self.default_asset(db, ws)
        q = lambda sql, *a: db.execute(sql, a).fetchone()[0]
        out = {'asset': asset, 'source_backing_locked': q("SELECT COALESCE(SUM(amount),0) FROM native_allocations WHERE workspace=? AND direction='export' AND asset=? AND state IN ('export_committed','certified','acknowledged','quarantined')", ws, asset),
               'source_backing_released_by_return': q("SELECT COALESCE(SUM(amount),0) FROM native_allocations WHERE workspace=? AND direction='export' AND asset=? AND state='returned'", ws, asset),
               'claims_active': q("SELECT COALESCE(SUM(amount - consumed),0) FROM native_claim_lineage WHERE workspace=? AND asset=? AND state IN ('active','partially_consumed')", ws, asset),
               'claims_consumed': q("SELECT COALESCE(SUM(consumed),0) FROM native_claim_lineage WHERE workspace=? AND asset=? AND state<>'retired'", ws, asset),
               'claims_retired': q("SELECT COALESCE(SUM(amount),0) FROM native_claim_lineage WHERE workspace=? AND asset=? AND state='retired'", ws, asset),
               'retirements_pending': q("SELECT COALESCE(SUM(amount),0) FROM native_allocations WHERE workspace=? AND direction='import' AND asset=? AND state='return_requested'", ws, asset),
               'returns_completed': q("SELECT COALESCE(SUM(amount),0) FROM native_allocations WHERE workspace=? AND direction='import' AND asset=? AND state='returned'", ws, asset)}
        au = self.audit(db, principal, asset); out['journal_audit'] = {k: au[k] for k in ('total_held', 'fixture_funding', 'backing_locked_for_exports', 'claims_backed_elsewhere', 'conserved')}
        out['checks'] = {'backing_matches_journal': out['source_backing_locked'] == au['backing_locked_for_exports'], 'claims_matches_journal': out['claims_active'] + out['claims_consumed'] + out['retirements_pending'] == au['claims_backed_elsewhere'] or au['claims_backed_elsewhere'] == 0}
        out['states_exposed'] = ['exported (certified, backing locked)', 'in_transit (certificate issued, import not acknowledged)', 'imported (claim active, returnable)', 'partially_consumed (nonreturnable)', 'consumed', 'retirement_pending (destination debit committed; certificate awaited)', 'reconciled (acknowledged / returned)']
        return out

    # ---- export (source) -----------------------------------------------------------------------------------------------------
    def export_create(self, db, principal, body):
        """Destination-bound allocation. Three durable steps, each its own crash boundary: reserve (available -> in_transit),
        export commit (in_transit -> backing:exported, sequence), certificate + outbox. Idempotent by (account, request_id)."""
        principal.require('native:export')
        if type(body) is not dict or set(body) - {'from_account', 'destination_domain', 'recipient_account', 'amount', 'asset', 'request_id', 'portions'}:
            raise ServiceError('VALIDATION', {'code': 'fields', 'allowed': ['from_account', 'destination_domain', 'recipient_account', 'amount', 'asset', 'request_id', 'portions?']})
        if body.get('portions') is not None:
            return self.export_batch(db, principal, body)
        d = self.domain(db, principal.workspace); self.require_normal(d, 'export')
        src = self.account_row(db, principal.workspace, body['from_account']) if body.get('from_account') else self.account_of(db, principal)
        if src['principal_id'] != principal.id and not principal.can('native:admin'):
            raise ServiceError('FORBIDDEN', 'only the account holder (or an administrator) exports from an account')
        rid = body.get('request_id')
        if type(rid) is not str or not 1 <= len(rid) <= 128:
            raise ServiceError('VALIDATION', {'code': 'request_id'})
        ex = db.execute("SELECT * FROM native_allocations WHERE workspace=? AND direction='export' AND source_account=? AND json_extract(evidence_json, '$.request_id')=?", (principal.workspace, src['id'], rid)).fetchone()
        if ex is not None:
            return dict(self.export_resume(db, principal, ex['id']), replayed=True)
        amt = P.amount(body.get('amount')); asset = body.get('asset') or self.default_asset(db, principal.workspace)
        if P.asset_parts(asset)['authority'] != d['id']:
            raise ServiceError('VALIDATION', {'code': 'asset_not_exportable_here', 'note': 'v0 exports only assets this domain issued (claims on another domain return through the reverse protocol, they are not re-exported)'})
        peer = self._peer(db, principal.workspace, body.get('destination_domain'))
        if peer['state'] != 'active':
            raise ServiceError('CONFLICT', {'code': 'peer_state', 'state': peer['state']})
        rcpt = body.get('recipient_account'); P.short_str(rcpt, 'recipient_account')
        ledger.require_available(db, principal.workspace, asset, d['id'], src['id'], amt)
        self._consume_claims(db, principal.workspace, src['id'], asset, amt, 'native_export', rid)
        aid = 'nal_' + secrets.token_hex(8)
        ev = {'request_id': rid, 'created_by': principal.id, 'steps': [{'state': 'reserved', 'at': now()}]}
        db.execute('INSERT INTO native_allocations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (aid, principal.workspace, 'export', d['id'], peer['domain_id'], asset, amt, src['id'], rcpt, 'reserved', d['epoch'], None, None, None, None, None, json.dumps(ev), now(), now()))
        ledger.move(db, principal.workspace, 'alloc-reserve:' + aid, 'native_export_reservation', asset, d['id'], self.environment, amt, ledger.account_name(src['id'], 'available'), ledger.account_name(src['id'], 'in_transit'), 'native_allocation', aid, 'reserved for export to ' + peer['domain_id'])
        history.record(db, principal.workspace, principal.id, 'native.allocation', 'native_allocation', aid, {'state': 'reserved', 'amount': amt, 'destination': peer['domain_id'], 'recipient': rcpt})
        _checkpoint(db); _fault(db, self.settings, 'native_export_reserve')
        return self.export_resume(db, principal, aid)

    def export_batch(self, db, principal, body):
        """Pre-export subdivision: one request locks the sum atomically and creates disjoint destination-bound allocations with
        unique identities (each with its own certificate); the exact sum is checked; each portion returns independently."""
        principal.require('native:export')
        d = self.domain(db, principal.workspace); self.require_normal(d, 'export')
        src = self.account_row(db, principal.workspace, body['from_account']) if body.get('from_account') else self.account_of(db, principal)
        if src['principal_id'] != principal.id and not principal.can('native:admin'):
            raise ServiceError('FORBIDDEN', 'only the account holder (or an administrator) exports from an account')
        rid = body.get('request_id'); portions = body.get('portions')
        if type(rid) is not str or not 1 <= len(rid) <= 128:
            raise ServiceError('VALIDATION', {'code': 'request_id'})
        if type(portions) is not list or not 1 <= len(portions) <= 32:
            raise ServiceError('VALIDATION', {'code': 'portions', 'allowed': '1..32 positive integers'})
        try:
            amts = [P.amount(x) for x in portions]; total = P.amount(body.get('amount'))
        except P.ProtocolError as exc:
            raise ServiceError('VALIDATION', {'code': exc.code, **exc.detail})
        if sum(amts) != total:
            raise ServiceError('VALIDATION', {'code': 'portions_sum', 'sum': sum(amts), 'amount': total, 'note': 'portions must sum exactly to the amount'})
        ex = db.execute("SELECT id FROM native_allocations WHERE workspace=? AND direction='export' AND source_account=? AND json_extract(evidence_json, '$.batch_request_id')=? ORDER BY json_extract(evidence_json, '$.portion_index')", (principal.workspace, src['id'], rid)).fetchall()
        if ex:
            return {'batch_request_id': rid, 'replayed': True, 'allocations': [self.export_resume(db, principal, r['id']) for r in ex]}
        asset = body.get('asset') or self.default_asset(db, principal.workspace)
        if P.asset_parts(asset)['authority'] != d['id']:
            raise ServiceError('VALIDATION', {'code': 'asset_not_exportable_here'})
        peer = self._peer(db, principal.workspace, body.get('destination_domain'))
        if peer['state'] != 'active':
            raise ServiceError('CONFLICT', {'code': 'peer_state', 'state': peer['state']})
        rcpt = body.get('recipient_account'); P.short_str(rcpt, 'recipient_account')
        ledger.require_available(db, principal.workspace, asset, d['id'], src['id'], total)      # the whole sum, atomically, before any portion is reserved
        self._consume_claims(db, principal.workspace, src['id'], asset, total, 'native_export_batch', rid)
        ids = []
        for i, amt in enumerate(amts):
            aid = 'nal_' + secrets.token_hex(8); ids.append(aid)
            ev = {'request_id': '%s#%d' % (rid, i), 'batch_request_id': rid, 'portion_index': i, 'portion_count': len(amts), 'batch_total': total, 'created_by': principal.id, 'steps': [{'state': 'reserved', 'at': now()}]}
            db.execute('INSERT INTO native_allocations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (aid, principal.workspace, 'export', d['id'], peer['domain_id'], asset, amt, src['id'], rcpt, 'reserved', d['epoch'], None, None, None, None, None, json.dumps(ev), now(), now()))
            ledger.move(db, principal.workspace, 'alloc-reserve:' + aid, 'native_export_reservation', asset, d['id'], self.environment, amt, ledger.account_name(src['id'], 'available'), ledger.account_name(src['id'], 'in_transit'), 'native_allocation', aid, 'portion %d/%d of batch %s reserved for export to %s' % (i + 1, len(amts), rid, peer['domain_id']))
        history.record(db, principal.workspace, principal.id, 'native.allocation', 'native_allocation', ids[0], {'batch': rid, 'portions': amts, 'total': total, 'destination': peer['domain_id'], 'allocation_ids': ids})
        _checkpoint(db); _fault(db, self.settings, 'native_export_reserve')
        return {'batch_request_id': rid, 'replayed': False, 'total': total, 'allocations': [self.export_resume(db, principal, a) for a in ids], 'disjoint': len(set(ids)) == len(ids), 'sum_check': sum(amts) == total}

    def export_resume(self, db, principal, aid):
        """Recovery: continue an export from its durable state (reconstructing the certificate and outbox message if they were lost)."""
        principal.require('native:export')
        a = db.execute("SELECT * FROM native_allocations WHERE id=? AND workspace=? AND direction='export'", (aid, principal.workspace)).fetchone()
        if a is None:
            raise ServiceError('NOT_FOUND', 'export allocation')
        d = self.domain(db, principal.workspace)
        if a['state'] == 'reserved':
            self.require_normal(d, 'export commit')
            seq = self.next_sequence(db, principal.workspace)
            ledger.move(db, principal.workspace, 'alloc-export:' + aid, 'native_export_commit', a['asset'], d['id'], self.environment, a['amount'], ledger.account_name(a['source_account'], 'in_transit'), 'backing:exported:' + aid, 'native_allocation', aid, 'export committed (sequence %d): nonspendable at the source' % seq)
            ev = json.loads(a['evidence_json']); ev['steps'].append({'state': 'export_committed', 'at': now(), 'sequence': seq}); ev['commit_digest'] = P.digest({'allocation_id': aid, 'sequence': seq, 'asset': a['asset'], 'amount': a['amount'], 'source_account': a['source_account'], 'recipient_account': a['recipient_account'], 'destination': a['destination_domain']})
            db.execute("UPDATE native_allocations SET state='export_committed', commit_sequence=?, evidence_json=?, updated_at=? WHERE id=?", (seq, json.dumps(ev), now(), aid))
            db.execute('UPDATE native_domains SET export_watermark=? WHERE id=?', (seq, d['id']))
            self._watermark_write(db, principal.workspace); self.checkpoint(db, principal.workspace)
            history.record(db, principal.workspace, principal.id, 'native.allocation', 'native_allocation', aid, {'state': 'export_committed', 'sequence': seq})
            _checkpoint(db); _fault(db, self.settings, 'native_export_commit')
            a = db.execute('SELECT * FROM native_allocations WHERE id=?', (aid,)).fetchone(); d = self.domain(db, principal.workspace)
        if a['state'] == 'export_committed':
            ev = json.loads(a['evidence_json'])
            cert = {'protocol': P.PROTOCOL, 'type': 'allocation_certificate', 'allocation_id': aid, 'environment': self.environment, 'source_authority': d['id'], 'source_epoch': a['epoch'], 'destination_authority': a['destination_domain'], 'asset': a['asset'], 'amount': a['amount'], 'recipient_account': a['recipient_account'], 'policy_version': P.POLICY_VERSION, 'commit_sequence': a['commit_sequence'], 'commit_digest': ev['commit_digest'], 'issued_at': now()}
            P.validate_certificate(cert, self.environment)
            kpath = self._key_path(db.execute('SELECT key_file FROM native_authority_keys WHERE domain_id=? AND epoch=? AND local=1', (d['id'], a['epoch'])).fetchone()['key_file']) if a['epoch'] != d['epoch'] else self._current_key_path(db, d)
            sig = self._sign_with(kpath, 'allocation_certificate', cert)
            kid = db.execute('SELECT key_id FROM native_authority_keys WHERE domain_id=? AND epoch=?', (d['id'], a['epoch'])).fetchone()['key_id']
            eid = self._outbox(db, principal.workspace, a['destination_domain'], 'allocation_certificate', cert, aid, payload_signature=sig, key_id=kid, key_path=kpath)
            ev['steps'].append({'state': 'certified', 'at': now(), 'envelope': eid})
            db.execute("UPDATE native_allocations SET state='certified', certificate_json=?, certificate_signature=?, evidence_json=?, updated_at=? WHERE id=?", (json.dumps(cert), sig, json.dumps(ev), now(), aid))
            self._receipt(db, principal.workspace, 'allocation_export', 'native_allocation', aid, {'allocation_id': aid, 'destination': a['destination_domain'], 'asset': a['asset'], 'amount': a['amount'], 'commit_sequence': a['commit_sequence'], 'finality': 'cross_domain_pending_claim', 'irrevocable': True})
            history.record(db, principal.workspace, principal.id, 'native.allocation', 'native_allocation', aid, {'state': 'certified', 'envelope': eid})
            _checkpoint(db); _fault(db, self.settings, 'native_certificate_publish')
        return self.allocation_view(db, principal, aid)

    def recover_exports(self, db, principal):
        """After a crash: every export left between boundaries is driven to its certificate; nothing is duplicated (state-driven)."""
        principal.require('native:export')
        out = []
        for a in db.execute("SELECT id, state FROM native_allocations WHERE workspace=? AND direction='export' AND state IN ('reserved','export_committed')", (principal.workspace,)).fetchall():
            out.append({'allocation_id': a['id'], 'from': a['state'], 'to': self.export_resume(db, principal, a['id'])['state']})
        return {'resumed': out}

    def allocation_view(self, db, principal, aid):
        principal.require('native:read')
        a = db.execute('SELECT * FROM native_allocations WHERE id=? AND workspace=?', (aid, principal.workspace)).fetchone()
        if a is None:
            raise ServiceError('NOT_FOUND', 'allocation')
        env = db.execute("SELECT id, state, attempts, reason FROM native_envelopes WHERE workspace=? AND direction='out' AND causality=? ORDER BY created_at", (principal.workspace, aid)).fetchall()
        fin = {'reserved': 'local_authorization', 'export_committed': 'durable_local_commit', 'certified': 'cross_domain_pending_claim', 'imported': 'domain_local_settlement', 'acknowledged': 'reconciled_inter_domain_settlement', 'return_requested': 'cross_domain_pending_claim', 'returned': 'reconciled_inter_domain_settlement', 'quarantined': 'cross_domain_pending_claim'}[a['state']]
        lin = db.execute('SELECT * FROM native_claim_lineage WHERE allocation_id=?', (aid,)).fetchone()
        exposed = {'reserved': 'reserved', 'export_committed': 'exported', 'certified': 'in_transit', 'acknowledged': 'reconciled', 'returned': 'reconciled', 'return_requested': 'retirement_pending', 'quarantined': 'quarantined', 'imported': ('returnable' if lin and lin['state'] == 'active' and lin['consumed'] == 0 else 'partially_consumed' if lin and lin['state'] == 'partially_consumed' else 'consumed' if lin and lin['state'] == 'consumed' else 'retirement_pending' if lin and lin['state'] == 'retired' else 'imported')}[a['state']]
        return {'schema': 'metacoin-native-allocation/v0', 'id': aid, 'exposed_state': exposed, 'returnable_amount': (a['amount'] if lin and lin['state'] == 'active' and lin['consumed'] == 0 else 0) if a['direction'] == 'import' else None, 'consumed_amount': lin['consumed'] if lin else None, 'batch': {'request_id': json.loads(a['evidence_json']).get('batch_request_id'), 'portion_index': json.loads(a['evidence_json']).get('portion_index'), 'portion_count': json.loads(a['evidence_json']).get('portion_count')} if json.loads(a['evidence_json']).get('batch_request_id') else None, 'direction': a['direction'], 'source_domain': a['source_domain'], 'destination_domain': a['destination_domain'], 'asset': a['asset'], 'amount': a['amount'], 'source_account': a['source_account'], 'recipient_account': a['recipient_account'],
                'state': a['state'], 'finality': fin, 'epoch': a['epoch'], 'commit_sequence': a['commit_sequence'], 'certificate': json.loads(a['certificate_json']) if a['certificate_json'] else None, 'certificate_signature': a['certificate_signature'],
                'acknowledgment': json.loads(a['ack_json']) if a['ack_json'] else None, 'return': json.loads(a['return_json']) if a['return_json'] else None, 'evidence': json.loads(a['evidence_json']), 'messages': [dict(e) for e in env],
                'explanation': {'reserved': 'the amount left the source account\'s spendable funds; nothing has been promised to the destination yet', 'export_committed': 'the source backing is locked and sequenced; a certificate can be issued', 'certified': 'irrevocable: the destination may import once; the source backing stays locked until a return certificate closes it',
                                'imported': 'credited at the destination under its pinned trust policy (spendable there); the source has not yet confirmed it knows', 'acknowledged': 'both domains hold durable, consistent state; one owner', 'return_requested': 'the destination made the full claim nonspendable and asked the source to unlock', 'returned': 'the source backing is spendable again; the destination claim is burned', 'quarantined': 'conflicting evidence; frozen until an administrator reconciles'}[a['state']], 'created_at': a['created_at'], 'updated_at': a['updated_at']}

    def allocations(self, db, principal, direction=None, state=None):
        principal.require('native:read')
        q = 'SELECT id FROM native_allocations WHERE workspace=?' + (' AND direction=?' if direction else '') + (' AND state=?' if state else '') + ' ORDER BY created_at'
        args = (principal.workspace,) + ((direction,) if direction else ()) + ((state,) if state else ())
        return {'items': [self.allocation_view(db, principal, r['id']) for r in db.execute(q, args).fetchall()]}

    # ---- import (destination) --------------------------------------------------------------------------------------------------
    def _on_certificate(self, db, principal, d, env, cert, sig, key):
        aid = cert['allocation_id']
        if cert['destination_authority'] != d['id']:
            raise P.ProtocolError('certificate_wrong_destination', {'addressed_to': cert['destination_authority'], 'this_domain': d['id']})
        if cert['source_authority'] != env['sender']:
            raise P.ProtocolError('certificate_sender_mismatch')
        if cert['source_epoch'] != key['epoch']:
            raise P.ProtocolError('epoch_key_mismatch', {'certificate_epoch': cert['source_epoch'], 'key_epoch': key['epoch'], 'note': 'a key signs only for its own epoch; a replacement key cannot authorize an earlier epoch\'s commitments'})
        if P.asset_parts(cert['asset'])['authority'] != env['sender']:
            raise P.ProtocolError('asset_origin_mismatch', {'asset': cert['asset'], 'sender': env['sender']})
        self.require_normal(d, 'import')
        ex = db.execute("SELECT * FROM native_allocations WHERE id=? AND workspace=?", (aid, principal.workspace)).fetchone()
        if ex is not None:
            if ex['direction'] == 'import' and ex['certificate_json'] and json.loads(ex['certificate_json']) == cert:
                return {'state': 'duplicate', 'reason': 'allocation_already_imported', 'replies': self._replay_replies(db, principal.workspace, dict(env, causality=aid))}
            raise P.ProtocolError('conflicting_signed_history', {'allocation_id': aid, 'note': 'two different certificates for one allocation id from the same authority: equivocation (quarantined)', 'quarantine': {'peer': env['sender'], 'allocation_id': aid, 'existing_digest': P.digest(json.loads(ex['certificate_json'] or '{}')), 'new_digest': P.digest(cert)}})
        clash = db.execute("SELECT id FROM native_allocations WHERE workspace=? AND direction='import' AND source_domain=? AND json_extract(certificate_json, '$.commit_sequence')=? AND id<>?", (principal.workspace, env['sender'], cert['commit_sequence'], aid)).fetchone()   # the SOURCE sequence (the local import sequence is unrelated)
        if clash is not None:
            raise P.ProtocolError('conflicting_sequence', {'sequence': cert['commit_sequence'], 'note': 'the source authority signed two allocations with one commit sequence (cloned authority or rollback); quarantined', 'quarantine': {'peer': env['sender'], 'allocation_id': clash['id'], 'sequence': cert['commit_sequence'], 'conflicting': aid}})
        rcpt = db.execute('SELECT * FROM native_accounts WHERE id=? AND workspace=?', (cert['recipient_account'], principal.workspace)).fetchone()
        if rcpt is None:
            raise P.ProtocolError('recipient_unknown', {'recipient_account': cert['recipient_account'], 'note': 'no value credited; the source backing stays locked; the source operator may return it through the reverse protocol only from a destination account that exists'})
        if db.execute('SELECT 1 FROM native_assets WHERE workspace=? AND asset_id=?', (principal.workspace, cert['asset'])).fetchone() is None:
            db.execute('INSERT INTO native_assets VALUES (?,?,?,?,?,?)', (cert['asset'], principal.workspace, 'claim on %s (imported allocations)' % env['sender'], 1, env['sender'], now()))
        seq = self.next_sequence(db, principal.workspace)
        ev = {'imported_from_envelope': env['id'], 'certificate_key_id': key['key_id'], 'steps': [{'state': 'imported', 'at': now(), 'sequence': seq}]}
        db.execute('INSERT INTO native_allocations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (aid, principal.workspace, 'import', env['sender'], d['id'], cert['asset'], cert['amount'], None, cert['recipient_account'], 'imported', cert['source_epoch'], seq, json.dumps(cert), sig, None, None, json.dumps(ev), now(), now()))
        ledger.move(db, principal.workspace, 'alloc-import:' + aid, 'native_import', cert['asset'], d['id'], self.environment, cert['amount'], 'backing:claim:' + aid, ledger.account_name(rcpt['id'], 'available'), 'native_allocation', aid, 'claim credited from %s certificate (sequence %d)' % (env['sender'], cert['commit_sequence']))
        db.execute('INSERT INTO native_claim_lineage VALUES (?,?,?,?,?,0,?,?,NULL,?,?)', (aid, principal.workspace, rcpt['id'], cert['asset'], cert['amount'], 'active', seq, now(), now()))
        _fault(db, self.settings, 'native_import_commit')
        ack = {'protocol': P.PROTOCOL, 'type': 'import_ack', 'allocation_id': aid, 'environment': self.environment, 'source_authority': env['sender'], 'destination_authority': d['id'], 'import_sequence': seq, 'imported_at': now()}
        eid = self._outbox(db, principal.workspace, env['sender'], 'import_ack', ack, aid)
        _fault(db, self.settings, 'native_ack')
        self._watermark_write(db, principal.workspace); self.checkpoint(db, principal.workspace)
        self._receipt(db, principal.workspace, 'allocation_import', 'native_allocation', aid, {'allocation_id': aid, 'source': env['sender'], 'asset': cert['asset'], 'amount': cert['amount'], 'recipient': rcpt['id'], 'import_sequence': seq, 'finality': 'domain_local_settlement', 'backing': 'locked at the source under its certificate; counted once'})
        history.record(db, principal.workspace, principal.id, 'native.allocation', 'native_allocation', aid, {'state': 'imported', 'from': env['sender'], 'amount': cert['amount'], 'recipient': rcpt['id']})
        reply = json.loads(db.execute('SELECT envelope_json FROM native_envelopes WHERE id=?', (eid,)).fetchone()['envelope_json'])
        return {'allocation_id': aid, 'credited': cert['amount'], 'recipient': rcpt['id'], 'replies': [reply]}

    def _quarantine(self, db, principal, d, peer, aid, reason, detail):
        db.execute("UPDATE native_peers SET state='quarantined' WHERE domain_id=? AND workspace=?", (peer, principal.workspace))
        db.execute("UPDATE native_allocations SET state='quarantined', updated_at=? WHERE id=? AND workspace=?", (now(), aid, principal.workspace))
        history.record(db, principal.workspace, principal.id, 'native.quarantine', 'native_peer', peer, dict(detail, reason=reason, allocation_id=aid))

    def _on_ack(self, db, principal, d, env, ack, sig, key):
        a = db.execute("SELECT * FROM native_allocations WHERE id=? AND workspace=? AND direction='export'", (ack['allocation_id'], principal.workspace)).fetchone()
        if a is None or a['destination_domain'] != env['sender'] or ack['source_authority'] != d['id']:
            raise P.ProtocolError('ack_unknown_allocation', {'allocation_id': ack['allocation_id']})
        if a['state'] == 'acknowledged':
            return {'state': 'duplicate', 'reason': 'already_acknowledged'}
        if a['state'] != 'certified':
            raise P.ProtocolError('ack_state', {'state': a['state']})
        ev = json.loads(a['evidence_json']); ev['steps'].append({'state': 'acknowledged', 'at': now(), 'envelope': env['id'], 'import_sequence': ack['import_sequence']})
        db.execute("UPDATE native_allocations SET state='acknowledged', ack_json=?, evidence_json=?, updated_at=? WHERE id=?", (json.dumps(ack), json.dumps(ev), now(), a['id']))
        history.record(db, principal.workspace, principal.id, 'native.allocation', 'native_allocation', a['id'], {'state': 'acknowledged', 'by': env['sender']})
        return {'allocation_id': a['id']}

    # ---- returns (full, unspent only) ------------------------------------------------------------------------------------------
    def return_request(self, db, principal, body):
        principal.require('native:export')
        aid = body.get('allocation_id') if type(body) is dict else None
        a = db.execute("SELECT * FROM native_allocations WHERE id=? AND workspace=? AND direction='import'", (aid, principal.workspace)).fetchone()
        if a is None:
            raise ServiceError('NOT_FOUND', 'imported allocation')
        d = self.domain(db, principal.workspace); self.require_normal(d, 'return')
        rcpt = self.account_row(db, principal.workspace, a['recipient_account'])
        if rcpt['principal_id'] != principal.id and not principal.can('native:admin'):
            raise ServiceError('FORBIDDEN', 'only the recipient (or an administrator) returns an allocation')
        if a['state'] == 'return_requested':
            if a['return_json'] and not db.execute("SELECT 1 FROM native_envelopes WHERE workspace=? AND direction='out' AND message_type='return_request' AND causality=?", (principal.workspace, aid)).fetchone():
                self._outbox(db, principal.workspace, a['source_domain'], 'return_request', json.loads(a['return_json']), aid)   # recovery: the retirement was durable, the message was not
            return dict(self.allocation_view(db, principal, aid), replayed=True)
        if a['state'] not in ('imported', 'acknowledged'):
            raise ServiceError('CONFLICT', {'code': 'return_state', 'state': a['state']})
        lin = db.execute('SELECT * FROM native_claim_lineage WHERE allocation_id=?', (aid,)).fetchone()
        avail = ledger.balance(db, principal.workspace, a['asset'], d['id'], rcpt['id'], 'available')
        if lin is None or lin['state'] != 'active' or lin['consumed'] != 0 or avail < a['amount']:
            raise ServiceError('CONFLICT', {'code': 'return_partial_unsupported', 'lineage_state': lin['state'] if lin else 'unknown', 'consumed': lin['consumed'] if lin else None, 'available': avail, 'amount': a['amount'], 'note': 'returns need an unconsumed claim of exact provenance; spent or mixed provenance is refused, not simulated'})
        db.execute("UPDATE native_claim_lineage SET state='retired', retired_at=?, updated_at=? WHERE allocation_id=?", (now(), now(), aid))   # irrevocable destination-side retirement, committed before any certificate
        ledger.move(db, principal.workspace, 'alloc-return-init:' + aid, 'native_return_initiation', a['asset'], d['id'], self.environment, a['amount'], ledger.account_name(rcpt['id'], 'available'), ledger.account_name(rcpt['id'], 'in_transit'), 'native_allocation', aid, 'retirement committed: claim debited irrevocably at the destination; the return certificate follows')
        seq = self.next_sequence(db, principal.workspace)
        req = {'protocol': P.PROTOCOL, 'type': 'return_request', 'allocation_id': aid, 'environment': self.environment, 'source_authority': a['source_domain'], 'destination_authority': d['id'], 'asset': a['asset'], 'amount': a['amount'], 'recipient_account': rcpt['id'], 'destination_sequence': seq, 'requested_at': now()}
        ev = json.loads(a['evidence_json']); ev['steps'].append({'state': 'return_requested', 'at': now(), 'sequence': seq})
        db.execute("UPDATE native_allocations SET state='return_requested', return_json=?, evidence_json=?, updated_at=? WHERE id=?", (json.dumps(req), json.dumps(ev), now(), aid))
        _checkpoint(db); _fault(db, self.settings, 'native_return_init')
        self._outbox(db, principal.workspace, a['source_domain'], 'return_request', req, aid)
        history.record(db, principal.workspace, principal.id, 'native.allocation', 'native_allocation', aid, {'state': 'return_requested'})
        return self.allocation_view(db, principal, aid)

    def _on_return_request(self, db, principal, d, env, req, sig, key):
        a = db.execute("SELECT * FROM native_allocations WHERE id=? AND workspace=? AND direction='export'", (req['allocation_id'], principal.workspace)).fetchone()
        if a is None or a['destination_domain'] != env['sender'] or req['source_authority'] != d['id'] or req['asset'] != a['asset'] or req['amount'] != a['amount'] or req['recipient_account'] != a['recipient_account']:
            raise P.ProtocolError('return_binding', {'allocation_id': req['allocation_id'], 'note': 'the return must name this source, the exact asset, amount and recipient of the allocation'})
        if a['state'] == 'returned':
            r = db.execute("SELECT envelope_json FROM native_envelopes WHERE workspace=? AND direction='out' AND message_type='return_certificate' AND causality=?", (principal.workspace, a['id'])).fetchone()
            return {'state': 'duplicate', 'reason': 'already_returned', 'replies': [json.loads(r['envelope_json'])] if r else []}
        if a['state'] not in ('certified', 'acknowledged'):
            raise P.ProtocolError('return_state', {'state': a['state']})
        self.require_normal(d, 'return completion')
        seq = self.next_sequence(db, principal.workspace)
        ledger.move(db, principal.workspace, 'alloc-return:' + a['id'], 'native_return_completion', a['asset'], d['id'], self.environment, a['amount'], 'backing:exported:' + a['id'], ledger.account_name(a['source_account'], 'available'), 'native_allocation', a['id'], 'returned by %s (destination sequence %d): source backing spendable again' % (env['sender'], req['destination_sequence']))
        _fault(db, self.settings, 'native_return_complete')
        cert = {'protocol': P.PROTOCOL, 'type': 'return_certificate', 'allocation_id': a['id'], 'environment': self.environment, 'source_authority': d['id'], 'destination_authority': env['sender'], 'asset': a['asset'], 'amount': a['amount'], 'source_sequence': seq, 'returned_at': now()}
        ev = json.loads(a['evidence_json']); ev['steps'].append({'state': 'returned', 'at': now(), 'sequence': seq, 'envelope': env['id']})
        db.execute("UPDATE native_allocations SET state='returned', return_json=?, evidence_json=?, updated_at=? WHERE id=?", (json.dumps(req), json.dumps(ev), now(), a['id']))
        eid = self._outbox(db, principal.workspace, env['sender'], 'return_certificate', cert, a['id'])
        self._watermark_write(db, principal.workspace); self.checkpoint(db, principal.workspace)
        history.record(db, principal.workspace, principal.id, 'native.allocation', 'native_allocation', a['id'], {'state': 'returned', 'sequence': seq})
        return {'allocation_id': a['id'], 'replies': [json.loads(db.execute('SELECT envelope_json FROM native_envelopes WHERE id=?', (eid,)).fetchone()['envelope_json'])]}

    def _on_return_certificate(self, db, principal, d, env, cert, sig, key):
        a = db.execute("SELECT * FROM native_allocations WHERE id=? AND workspace=? AND direction='import'", (cert['allocation_id'], principal.workspace)).fetchone()
        if a is None or a['source_domain'] != env['sender'] or cert['destination_authority'] != d['id'] or cert['amount'] != a['amount'] or cert['asset'] != a['asset']:
            raise P.ProtocolError('return_certificate_binding')
        if a['state'] == 'returned':
            return {'state': 'duplicate', 'reason': 'already_returned'}
        if a['state'] != 'return_requested':
            raise P.ProtocolError('return_certificate_state', {'state': a['state'], 'note': 'a return certificate can only close a return this domain requested (no timeout, no unilateral unlock)'})
        ledger.move(db, principal.workspace, 'alloc-burn:' + a['id'], 'native_claim_burn', a['asset'], d['id'], self.environment, a['amount'], ledger.account_name(a['recipient_account'], 'in_transit'), 'backing:claim:' + a['id'], 'native_allocation', a['id'], 'claim burned on the source\'s return certificate')
        ev = json.loads(a['evidence_json']); ev['steps'].append({'state': 'returned', 'at': now(), 'envelope': env['id']})
        db.execute("UPDATE native_allocations SET state='returned', evidence_json=?, updated_at=? WHERE id=?", (json.dumps(ev), now(), a['id']))
        history.record(db, principal.workspace, principal.id, 'native.allocation', 'native_allocation', a['id'], {'state': 'returned', 'claim': 'burned'})
        return {'allocation_id': a['id']}

    # ---- rotation and observations -------------------------------------------------------------------------------------------
    def _on_rotation(self, db, principal, d, env, msg, sig, key):
        if msg['authority'] != env['sender'] or msg['old_key_id'] != key['key_id'] or msg['old_epoch'] != key['epoch'] or msg['new_epoch'] != key['epoch'] + 1:
            raise P.ProtocolError('rotation_binding', {'note': 'a rotation is signed by the retiring key for exactly the next epoch'})
        if crypto.key_id_for(msg['new_public_hex']) != msg['new_key_id'] or len(msg['new_public_hex']) != 64:
            raise P.ProtocolError('rotation_key')
        if db.execute('SELECT 1 FROM native_authority_keys WHERE domain_id=? AND epoch=?', (env['sender'], msg['new_epoch'])).fetchone():
            return {'state': 'duplicate', 'reason': 'epoch_known'}
        t = now(); grace = min(max(msg['grace_seconds'], 0), 90 * 86400)
        db.execute("UPDATE native_authority_keys SET state='retired', retired_at=?, grace_until=? WHERE domain_id=? AND epoch=? AND local=0", (msg['rotated_at'], msg['rotated_at'] + grace, env['sender'], key['epoch']))
        db.execute('INSERT INTO native_authority_keys VALUES (?,?,?,?,0,NULL,?,?,NULL,NULL,?)', (env['sender'], msg['new_epoch'], msg['new_key_id'], msg['new_public_hex'], 'active', msg['rotated_at'], t))
        db.execute('UPDATE native_peers SET authority_key_id=?, authority_public_hex=?, epoch=? WHERE domain_id=? AND workspace=?', (msg['new_key_id'], msg['new_public_hex'], msg['new_epoch'], env['sender'], principal.workspace))
        history.record(db, principal.workspace, principal.id, 'native.domain', 'native_peer', env['sender'], {'rotated_to_epoch': msg['new_epoch'], 'old_key_grace_until': msg['rotated_at'] + grace})
        return {'peer': env['sender'], 'epoch': msg['new_epoch']}

    def observation(self, db, principal, peer):
        """This domain's signed statement of every allocation it imported from (or exported to) a peer: recovery evidence for the peer."""
        principal.require('native:transport')
        d = self.domain(db, principal.workspace); self._peer(db, principal.workspace, peer)
        rows = db.execute("SELECT * FROM native_allocations WHERE workspace=? AND ((direction='import' AND source_domain=?) OR (direction='export' AND destination_domain=?)) ORDER BY created_at", (principal.workspace, peer, peer)).fetchall()
        allocs = [{'allocation_id': r['id'], 'commit_sequence': (json.loads(r['certificate_json'])['commit_sequence'] if r['direction'] == 'import' and r['certificate_json'] else (r['commit_sequence'] or 0)), 'commit_digest': (json.loads(r['certificate_json']).get('commit_digest', '') if r['certificate_json'] else ''), 'state': r['direction'] + ':' + r['state']} for r in rows]
        body = {'protocol': P.PROTOCOL, 'type': 'observation', 'observer': d['id'], 'observed': peer, 'environment': self.environment, 'allocations': allocs, 'issued_at': now()}
        eid = self._outbox(db, principal.workspace, peer, 'observation', body, None)
        return {'envelope_id': eid, 'observation': body, 'count': len(allocs)}

    def _on_observation(self, db, principal, d, env, obs, sig, key):
        if obs['observer'] != env['sender'] or obs['observed'] != d['id']:
            raise P.ProtocolError('observation_binding')
        return {'peer': env['sender'], 'allocations_observed': len(obs['allocations'])}

    def recovery_reconcile(self, db, principal, body=None):
        """Leave quarantine only when every commitment a peer observed from this domain exists locally. Missing exported
        allocations are reconstructed from the peer's signed observation as locked backing (never as spendable funds)."""
        principal.require('native:admin'); body = body or {}
        d = self.domain(db, principal.workspace)
        peers = [p['domain_id'] for p in db.execute("SELECT domain_id FROM native_peers WHERE workspace=? AND state='active'", (principal.workspace,)).fetchall()]
        report = {'domain': d['id'], 'recovery_state': d['recovery_state'], 'peers': {}, 'reconstructed': [], 'missing_observations': [], 'rollback_note': ROLLBACK_NOTE}
        ok = True
        for p in peers:
            r = db.execute("SELECT payload_json FROM native_envelopes WHERE workspace=? AND direction='in' AND message_type='observation' AND sender=? AND state='accepted' ORDER BY created_at DESC LIMIT 1", (principal.workspace, p)).fetchone()
            if r is None:
                report['missing_observations'].append(p); ok = False; continue
            obs = json.loads(r['payload_json']); missing = []
            for a in obs['allocations']:
                mine = db.execute('SELECT * FROM native_allocations WHERE id=? AND workspace=?', (a['allocation_id'], principal.workspace)).fetchone()
                if mine is None and a['state'].startswith('import:'):
                    missing.append(a)
            report['peers'][p] = {'observed': len(obs['allocations']), 'missing_locally': [m['allocation_id'] for m in missing], 'observation_issued_at': obs['issued_at']}
            for m in missing:
                if not body.get('reconstruct_from_observations'):
                    ok = False; continue
                # the peer imported an allocation this database does not know: lock equivalent backing now (safe direction only)
                src = body.get('source_account') or self.system_account(db, principal.workspace, 'treasury')
                asset = self.default_asset(db, principal.workspace)
                avail = ledger.balance(db, principal.workspace, asset, d['id'], src, 'available')
                amt = body.get('amounts', {}).get(m['allocation_id'])
                if type(amt) is not int or amt <= 0 or avail < amt:
                    ok = False; report['reconstructed'].append({'allocation_id': m['allocation_id'], 'status': 'cannot_lock', 'note': 'supply amounts[allocation_id] from the peer\'s certificate copy; the source account must still hold the funds'}); continue
                ledger.move(db, principal.workspace, 'alloc-reconstruct:' + m['allocation_id'], 'native_export_reconstruction', asset, d['id'], self.environment, amt, ledger.account_name(src, 'available'), 'backing:exported:' + m['allocation_id'], 'native_allocation', m['allocation_id'], 'backing locked from the peer\'s signed observation after a rollback')
                db.execute('INSERT INTO native_allocations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (m['allocation_id'], principal.workspace, 'export', d['id'], p, asset, amt, src, '?', 'acknowledged', d['epoch'], m['commit_sequence'], None, None, None, None, json.dumps({'reconstructed_from_observation': p, 'steps': [{'state': 'reconstructed', 'at': now()}]}), now(), now()))
                report['reconstructed'].append({'allocation_id': m['allocation_id'], 'status': 'locked', 'amount': amt})
        if ok and d['recovery_state'] != 'normal':
            wm = self._watermark_path(d['id'])
            wmd = json.loads(wm.read_text()) if wm.exists() else {}; wl = self.witness_latest(d['id'])
            seq = max(d['commit_sequence'], wmd.get('commit_sequence', 0), wmd.get('witness_acknowledged_sequence') or 0, (wl.get('latest_sequence') or 0) if wl.get('reachable') and wl.get('identity_pinned_matches', True) else 0)
            db.execute("UPDATE native_domains SET recovery_state='normal', commit_sequence=?, updated_at=? WHERE id=?", (seq, now(), d['id']))
            self._watermark_write(db, principal.workspace)
            history.record(db, principal.workspace, principal.id, 'native.quarantine', 'native_domain', d['id'], {'recovery_state': 'normal', 'commit_sequence': seq, 'peers_reconciled': peers})
            report['recovery_state'] = 'normal'; report['commit_sequence'] = seq
        elif d['recovery_state'] != 'normal':
            report['recovery_state'] = d['recovery_state']; report['blocked_by'] = 'missing peer observations or unreconstructed commitments'
        return report

    def export_record(self, db, principal):
        """Everything the independent checker needs, as data: this domain's identity, receipts, intents (public fields + signer
        public keys as data), obligations, allocations, envelopes, native journal entries and the audit. Trust roots are NOT
        included as roots: the verifier pins them separately (they appear here only as this domain's self-description)."""
        principal.require('native:read')
        d = self.domain(db, principal.workspace); ws = principal.workspace
        from ..economy import journal
        accts = [dict(r) for r in db.execute('SELECT id, kind, label, key_id, public_hex, custody, state FROM native_accounts WHERE workspace=?', (ws,)).fetchall()]
        pub = {a['id']: a['public_hex'] for a in accts}
        intents = []
        for r in db.execute('SELECT * FROM native_intents WHERE workspace=? ORDER BY created_at', (ws,)).fetchall():
            intents.append({'id': r['id'], 'type': r['type'], 'actor_account': r['actor_account'], 'recipient': r['recipient'], 'asset': r['asset'], 'amount_final': r['amount_final'], 'state': r['state'], 'commit_sequence': r['commit_sequence'], 'obligation_id': r['obligation_ref'],
                            'message': json.loads(r['message_json']), 'message_digest': r['message_digest'], 'signature_hex': r['signature_hex'], 'signer_public_hex': pub.get(r['actor_account'])})
        receipts = [{'id': r['id'], 'kind': r['kind'], 'ref_type': r['ref_type'], 'ref_id': r['ref_id'], 'statement': json.loads(r['statement_json']), 'signature_hex': r['signature_hex'], 'key_id': r['key_id']} for r in db.execute('SELECT * FROM native_receipts WHERE workspace=? ORDER BY created_at', (ws,)).fetchall()]
        allocs = [self.allocation_view(db, principal, r['id']) for r in db.execute('SELECT id FROM native_allocations WHERE workspace=? ORDER BY created_at', (ws,)).fetchall()]
        envs = {'in': [], 'out': []}
        for r in db.execute("SELECT * FROM native_envelopes WHERE workspace=? AND state IN ('accepted','queued','delivered','duplicate') ORDER BY created_at", (ws,)).fetchall():
            try:
                envs[r['direction']].append(json.loads(r['envelope_json']))
            except ValueError:
                pass
        entries = [e for e in journal.entries(db, ws, limit=5000) if e['network'] == d['id']]
        obligations = [self.obligation_view(db, principal, r['id']) for r in db.execute('SELECT id FROM native_obligations WHERE workspace=? ORDER BY created_at', (ws,)).fetchall()]
        keys = [dict(k) for k in db.execute('SELECT domain_id, epoch, key_id, public_hex, local, state, valid_from, retired_at, grace_until FROM native_authority_keys ORDER BY domain_id, epoch').fetchall()]
        account_keys = [dict(k) for k in db.execute('SELECT account_id, key_id, public_hex, kind, epoch, state, retired_at, grace_until FROM native_account_keys WHERE workspace=? ORDER BY account_id, epoch', (ws,)).fetchall()]
        for i in intents:
            hist = json.loads(db.execute('SELECT history_json FROM native_intents WHERE id=?', (i['id'],)).fetchone()['history_json'] or '[]')
            i['signer_key_id'] = next((h.get('key_id') for h in reversed(hist) if isinstance(h, dict) and h.get('key_id')), None)
        return {'schema': 'metacoin-native-export/v0', 'domain': d['id'], 'environment': d['environment'], 'operator': d['operator'], 'host': d['host'], 'epoch': d['epoch'], 'commit_sequence': d['commit_sequence'], 'recovery_state': d['recovery_state'],
                'self_description_keys': keys, 'account_keys': account_keys, 'accounts': accts, 'intents': intents, 'receipts': receipts, 'obligations': obligations, 'allocations': allocs, 'envelopes_in': envs['in'], 'envelopes_out': envs['out'],
                'journal_entries': entries, 'audits': {a: self.audit(db, principal, a) for a in [r['asset_id'] for r in db.execute('SELECT asset_id FROM native_assets WHERE workspace=?', (ws,)).fetchall()]}, 'exported_at': now(),
                'note': 'keys listed here are data; a verifier pins roots from an out-of-band source (the domain view read over an authenticated channel, or the operator)'}

    def recovery_view(self, db, principal):
        principal.require('native:read')
        d = self.domain(db, principal.workspace); wm = self._watermark_path(d['id'])
        w = self.witness_latest(d['id']); last_cp = db.execute('SELECT sequence, acknowledged_at, witness_id FROM native_checkpoints WHERE workspace=? AND acknowledged_at IS NOT NULL ORDER BY sequence DESC LIMIT 1', (principal.workspace,)).fetchone()
        detection = 'available: witness-acknowledged checkpoint' if (w.get('reachable') and w.get('latest_sequence') is not None) else ('available: local watermark file only (same host; a restore that also restores the file is undetectable)' if wm.exists() else 'UNAVAILABLE: no witness reachable and no watermark file; a restored database cannot be told from the newest one')
        return {'domain': d['id'], 'recovery_state': d['recovery_state'], 'database_commit_sequence': d['commit_sequence'], 'watermark': json.loads(wm.read_text()) if wm.exists() else None, 'export_watermark': d['export_watermark'], 'witness': w, 'last_acknowledged_checkpoint': dict(last_cp) if last_cp else None, 'rollback_detection': detection,
                'quarantined_peers': [p['domain_id'] for p in db.execute("SELECT domain_id FROM native_peers WHERE workspace=? AND state='quarantined'", (principal.workspace,)).fetchall()],
                'quarantined_allocations': [a['id'] for a in db.execute("SELECT id FROM native_allocations WHERE workspace=? AND state='quarantined'", (principal.workspace,)).fetchall()],
                'what_is_detected': P.TRUST_MODEL['detected'], 'what_is_not': P.TRUST_MODEL['not_provided'], 'safety_limitation': P.TRUST_MODEL['boundary'], 'rollback_note': ROLLBACK_NOTE}

    for f in (export_record, _journal_root, checkpoint, witness_latest, _reassemble, _fragment_items, _consume_claims, lineage_view, accounting_view, export_batch, _watermark_path, _watermark_write, rollback_check, require_normal, _record_local_key, _current_key_path, _sign_with, pin_peer, peer_view, _peer, _verify_peer_signature, rotate_authority, _outbox, outbox, outbox_mark, transport_gc, inbox, _record_rejection, _apply_item, _replay_replies,
              export_create, export_resume, recover_exports, allocation_view, allocations, _on_certificate, _quarantine, _on_ack, return_request, _on_return_request, _on_return_certificate, _on_rotation, observation, _on_observation, recovery_reconcile, recovery_view):
        setattr(cls, f.__name__, f)
    return cls
