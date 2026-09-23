"""Append-only application history with a hash chain per workspace.

The chain detects accidental alteration when compared with an independently kept
checkpoint; stored beside the same database it does not defeat an administrator
or a rollback. Events carry identifiers and digests, never private values."""
import hashlib
import json
from experiments.private_receipts import receipt as merkle
from .db import now

CATEGORIES = {
    'contract.created': 'administrative', 'contract.frozen': 'scientific', 'contract.amended': 'administrative',
    'job.queued': 'scientific', 'job.claimed': 'scientific', 'job.result_committed': 'scientific',
    'job.failed': 'scientific', 'job.cancelled': 'administrative', 'job.retry_scheduled': 'scientific',
    'review.requested': 'scientific', 'review.accepted': 'scientific', 'review.rejected': 'scientific',
    'artifact.granted': 'administrative', 'artifact.deleted': 'administrative', 'artifact.exported': 'administrative',
    'payment.reserved': 'economic', 'payment.dispatched': 'economic', 'payment.reconciled': 'economic',
    'sale.requested': 'economic', 'sale.settled': 'economic', 'sale.failed': 'economic', 'sale.unknown': 'economic',
    'credential.issued': 'administrative', 'credential.revoked': 'administrative', 'key.rotated': 'administrative',
    'key.revoked': 'administrative', 'backup.created': 'administrative', 'restore.completed': 'administrative',
    'retention.cleanup': 'administrative',
}


def record(db, workspace, actor_id, event_type, object_type, object_id, ref=None):
    category = CATEGORIES[event_type]
    ref_bytes = merkle.canonical(ref or {})
    prev = db.execute('SELECT hash FROM events WHERE workspace=? ORDER BY seq DESC LIMIT 1', (workspace,)).fetchone()
    prev_hash = prev['hash'] if prev else '0' * 64
    ts = now()
    body = merkle.canonical([workspace, ts, actor_id, event_type, object_type, object_id]) + ref_bytes
    digest = hashlib.sha256(b'metacoin/service-event/v1\0' + bytes.fromhex(prev_hash) + body).hexdigest()
    db.execute('INSERT INTO events (workspace, ts, actor_id, event_type, category, object_type, object_id, ref_json, prev_hash, hash) '
               'VALUES (?,?,?,?,?,?,?,?,?,?)', (workspace, ts, actor_id, event_type, category, object_type, object_id,
                                               ref_bytes.decode(), prev_hash, digest))


def verify_chain(db, workspace):
    prev = '0' * 64
    count = 0
    for row in db.execute('SELECT * FROM events WHERE workspace=? ORDER BY seq', (workspace,)):
        body = merkle.canonical([workspace, row['ts'], row['actor_id'], row['event_type'], row['object_type'], row['object_id']]) \
            + row['ref_json'].encode()
        digest = hashlib.sha256(b'metacoin/service-event/v1\0' + bytes.fromhex(prev) + body).hexdigest()
        if row['prev_hash'] != prev or row['hash'] != digest:
            return {'valid': False, 'first_bad_seq': row['seq'], 'checked': count}
        prev, count = digest, count + 1
    return {'valid': True, 'checked': count, 'head': prev}


def for_object(db, workspace, object_type, object_id, limit=200):
    return [dict(r, ref=json.loads(r['ref_json'])) for r in db.execute(
        'SELECT seq, ts, actor_id, event_type, category, object_type, object_id, ref_json, hash FROM events '
        'WHERE workspace=? AND object_type=? AND object_id=? ORDER BY seq LIMIT ?', (workspace, object_type, object_id, limit))]
