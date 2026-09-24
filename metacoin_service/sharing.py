"""Selective sharing: an owner grants a named principal a projection of a job's result.

A projection is an explicit allowlist of fields (top-level result facts, or `summary:<key>` for
one summary entry). The grantee sees exactly those fields and nothing else; inputs, private labels
and unlisted summary entries never enter the projection, the signed bundle, or the events. The
service signs the projection so it can be handed onward and verified against the service key.
"""
import json
import secrets
from experiments.private_receipts import receipt as merkle
from . import crypto, history
from .db import now
from .errors import ServiceError

TOP_LEVEL = ('kind', 'outcome', 'model_id', 'verifier_id', 'verifier_digest', 'evidence_root', 'contract_digest', 'finished_at', 'review_state')
SCHEMA = 'metacoin-projection/v1'


def _validate_fields(fields):
    if type(fields) is not list or not 1 <= len(fields) <= 32 or not all(type(f) is str for f in fields):
        raise ServiceError('VALIDATION', {'code': 'fields', 'allowed': list(TOP_LEVEL) + ['summary:<key>']})
    for f in fields:
        if f in TOP_LEVEL:
            continue
        if f.startswith('summary:') and 1 <= len(f) - 8 <= 64 and f[8:].replace('_', 'a').isalnum():
            continue
        raise ServiceError('VALIDATION', {'code': 'field_not_projectable', 'field': f, 'allowed': list(TOP_LEVEL) + ['summary:<key>']})
    return sorted(set(fields))


def grant(db, principal, job_id, grantee_id, fields):
    principal.require('job:read_private')
    job = db.execute('SELECT * FROM jobs WHERE id=? AND workspace=?', (job_id, principal.workspace)).fetchone()
    if job is None:
        raise ServiceError('NOT_FOUND', 'job')
    if job['state'] != 'succeeded':
        raise ServiceError('CONFLICT', 'only committed results can be projected')
    grantee = db.execute('SELECT id, role FROM principals WHERE id=? AND workspace=? AND revoked_at IS NULL', (grantee_id, principal.workspace)).fetchone()
    if grantee is None:
        raise ServiceError('NOT_FOUND', 'grantee principal')
    fields = _validate_fields(fields)
    summary = json.loads(job['summary_json'] or '{}')
    missing = [f[8:] for f in fields if f.startswith('summary:') and f[8:] not in summary]
    if missing:
        raise ServiceError('VALIDATION', {'code': 'summary_key_absent', 'keys': missing, 'available': sorted(summary)})
    sid = 'sh_' + secrets.token_hex(8)
    db.execute('INSERT INTO shares (id, workspace, job_id, granted_by, grantee_id, fields_json, created_at) VALUES (?,?,?,?,?,?,?)',
               (sid, principal.workspace, job_id, principal.id, grantee_id, json.dumps(fields), now()))
    history.record(db, principal.workspace, principal.id, 'sharing.granted', 'job', job_id, {'share_id': sid, 'grantee_id': grantee_id, 'fields': fields})
    return {'share_id': sid, 'job_id': job_id, 'grantee_id': grantee_id, 'fields': fields}


def revoke(db, principal, share_id):
    principal.require('job:read_private')
    row = db.execute('SELECT * FROM shares WHERE id=? AND workspace=?', (share_id, principal.workspace)).fetchone()
    if row is None:
        raise ServiceError('NOT_FOUND', 'share')
    db.execute('UPDATE shares SET revoked_at=COALESCE(revoked_at, ?) WHERE id=?', (now(), share_id))
    history.record(db, principal.workspace, principal.id, 'sharing.revoked', 'job', row['job_id'], {'share_id': share_id})
    return {'share_id': share_id, 'revoked': True}


def list_shares(db, principal, job_id):
    principal.require('job:read')
    rows = db.execute('SELECT * FROM shares WHERE job_id=? AND workspace=? ORDER BY created_at', (job_id, principal.workspace)).fetchall()
    if not principal.can('job:read_private'):
        rows = [r for r in rows if r['grantee_id'] == principal.id]
    return {'items': [{'share_id': r['id'], 'grantee_id': r['grantee_id'], 'fields': json.loads(r['fields_json']), 'created_at': r['created_at'], 'revoked_at': r['revoked_at']} for r in rows]}


def _fields_for(db, principal, job):
    if principal.can('job:read_private'):
        summary = json.loads(job['summary_json'] or '{}')
        return list(TOP_LEVEL) + ['summary:' + k for k in sorted(summary)], ['owner']
    rows = db.execute('SELECT id, fields_json FROM shares WHERE job_id=? AND grantee_id=? AND revoked_at IS NULL', (job['id'], principal.id)).fetchall()
    fields = sorted({f for r in rows for f in json.loads(r['fields_json'])})
    return fields, [r['id'] for r in rows]


def projection(db, principal, settings, job_id):
    principal.require('job:read')
    job = db.execute('SELECT * FROM jobs WHERE id=? AND workspace=?', (job_id, principal.workspace)).fetchone()
    if job is None:
        raise ServiceError('NOT_FOUND', 'job')
    fields, basis = _fields_for(db, principal, job)
    if not fields:
        raise ServiceError('FORBIDDEN', 'no projection shared with this principal')
    contract = db.execute('SELECT contract_digest, contract_json FROM contracts WHERE id=?', (job['contract_id'],)).fetchone()
    doc = json.loads(contract['contract_json']) if contract['contract_json'] else {}
    summary = json.loads(job['summary_json'] or '{}')
    source = {'kind': job['kind'], 'outcome': job['outcome'], 'model_id': doc.get('model_id'), 'verifier_id': doc.get('verifier_id'), 'verifier_digest': doc.get('verifier_digest'),
              'evidence_root': job['evidence_root'], 'contract_digest': contract['contract_digest'], 'finished_at': job['finished_at'], 'review_state': job['review_state']}
    out = {}
    for f in fields:
        if f in TOP_LEVEL:
            out[f] = source[f]
        elif f[8:] in summary:
            out[f] = summary[f[8:]]
    statement = {'schema': SCHEMA, 'job_id': job_id, 'workspace': principal.workspace, 'fields': out, 'issued_at': now(), 'issued_to': principal.id, 'shares': basis}
    from . import metering
    pub = metering.ensure_service_key(settings, db)
    statement['issuer_key_id'] = crypto.key_id_for(pub)
    message = merkle.canonical(statement)
    signature = crypto.sign(crypto.load_signing_key(settings.keys_dir / 'service.ed25519'), message)
    return {'job_id': job_id, 'fields': out, 'basis': basis, 'bundle': {'statement': statement, 'signature': signature, 'public_key': pub},
            'note': 'exactly the granted fields; the signature identifies the issuing service, not the truth of the result'}


def verify_bundle(db, bundle):
    if type(bundle) is not dict or set(bundle) != {'statement', 'signature', 'public_key'} or type(bundle['statement']) is not dict:
        raise ServiceError('VALIDATION', 'bundle: {statement, signature, public_key}')
    row = db.execute("SELECT value FROM meta WHERE key='service_signing_public'").fetchone()
    known = row is not None and row['value'] == bundle['public_key']
    try:
        ok = crypto.verify(bundle['public_key'], merkle.canonical(bundle['statement']), bundle['signature'])
    except Exception:
        ok = False
    return {'signature_valid': bool(ok), 'issuer_is_this_service': known, 'schema_ok': bundle['statement'].get('schema') == SCHEMA,
            'job_id': bundle['statement'].get('job_id'), 'fields': sorted(bundle['statement'].get('fields', {})) if ok else None}
