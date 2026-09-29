"""Server-side principals, credentials, sessions and role permissions.

Bearer credentials are 32 random bytes shown once; only a salted SHA-256 of the
token is stored and compared with hmac.compare_digest. Tokens are high-entropy,
so a keyed hash (not a password KDF) is the right verification representation.
There is no password login and therefore no password hashing or recovery flow.
Browser sessions are server-side rows; the cookie carries only the session id.
A submitted owner/auditor/role field is never trusted; permissions derive from
the stored principal row. Workspace scoping applies to every resource query.
"""
import hashlib
import hmac
import json
import secrets
from .db import now
from .errors import ServiceError

ROLES = ('owner', 'worker', 'reviewer', 'viewer', 'provider')
# role -> permitted operations (the application authorization table)
PERMISSIONS = {
    'owner': {'contract:create', 'contract:read', 'contract:freeze', 'contract:amend', 'job:submit', 'job:read',
              'job:cancel', 'job:read_private', 'review:request', 'artifact:read_private', 'artifact:export',
              'artifact:delete', 'action:create', 'action:read', 'action:reconcile', 'budget:read', 'history:read',
              'x402:sell', 'admin:credentials', 'admin:keys', 'template:write',
              'model:admin', 'model:use', 'knowledge:write', 'knowledge:read', 'calibration:write', 'verification:submit', 'node:admin', 'approval:propose', 'approval:decide', 'statement:read',
              'work:read', 'work:request', 'work:award', 'work:accept', 'work:pay', 'work:dispute', 'work:audit_grant', 'work:treasury', 'work:provider_admin'},
    'worker': {'job:claim', 'job:read', 'job:publish', 'artifact:read_input', 'history:read'},
    'reviewer': {'job:read', 'review:read_evidence', 'review:decide', 'artifact:read_private_assigned',
                 'history:read', 'contract:read', 'action:read', 'budget:read', 'verification:submit', 'approval:decide', 'work:read', 'work:resolve', 'work:dispute'},
    'viewer': {'job:read', 'contract:read', 'artifact:read_public', 'history:read', 'budget:read', 'work:read'},
    'provider': {'job:read', 'contract:read', 'history:read', 'work:read', 'work:offer', 'work:ack', 'work:deliver', 'work:dispute', 'artifact:read_public'},
}
PEPPER_KEY = 'credential_pepper'


class Principal:
    def __init__(self, row, credential_id=None, session=None):
        self.id, self.name, self.role, self.workspace = row['id'], row['name'], row['role'], row['workspace']
        self.credential_id, self.session = credential_id, session

    scope = None      # {'operations': [...], 'workspace': ws} for scoped automation credentials

    def can(self, operation):
        if operation not in PERMISSIONS[self.role]:
            return False
        if self.scope is not None:
            return operation in self.scope.get('operations', ()) and self.scope.get('workspace') == self.workspace
        return True

    def require(self, operation):
        if not self.can(operation):
            raise ServiceError('FORBIDDEN', operation)
        return self


def _pepper(db):
    row = db.execute('SELECT value FROM meta WHERE key=?', (PEPPER_KEY,)).fetchone()
    if row is None:
        value = secrets.token_hex(32)
        db.execute('INSERT INTO meta VALUES (?, ?)', (PEPPER_KEY, value))
        return value
    return row['value']


def _hash(db, token):
    return hmac.new(bytes.fromhex(_pepper(db)), token.encode('ascii'), hashlib.sha256).hexdigest()


def new_id(prefix):
    return prefix + '_' + secrets.token_hex(8)


def create_principal(db, name, role, workspace):
    if role not in ROLES:
        raise ServiceError('VALIDATION', 'role')
    pid = new_id('p')
    db.execute('INSERT INTO principals VALUES (?, ?, ?, ?, ?, NULL)', (pid, name[:64], role, workspace, now()))
    return pid


AUTOMATION_OPERATIONS = {'contract:create', 'contract:read', 'contract:freeze', 'job:submit', 'job:read', 'job:read_private',
                         'review:request', 'artifact:export', 'history:read', 'budget:read', 'action:read',
                         'model:use', 'knowledge:read', 'knowledge:write', 'verification:submit', 'statement:read'}


def issue_scoped_credential(db, issuer, operations, lifetime_seconds):
    """Automation credential limited to the issuer's workspace and a subset of the issuer's
    own permissions within AUTOMATION_OPERATIONS. Request fields cannot widen it."""
    issuer.require('admin:credentials')
    if type(operations) is not list or not operations or len(operations) > 20 or not all(type(o) is str for o in operations):
        raise ServiceError('VALIDATION', 'operations')
    widened = [o for o in operations if o not in AUTOMATION_OPERATIONS or not issuer.can(o)]
    if widened:
        raise ServiceError('FORBIDDEN', 'scope exceeds the issuer or the automation set')
    if type(lifetime_seconds) is not int or not 60 <= lifetime_seconds <= 30 * 86400:
        raise ServiceError('VALIDATION', 'expires_in_seconds')
    return issue_credential(db, issuer.id, lifetime_seconds, scope={'operations': sorted(set(operations)), 'workspace': issuer.workspace})


def issue_credential(db, principal_id, lifetime_seconds, scope=None):
    """Returns the raw bearer token exactly once."""
    token = 'mck_' + secrets.token_urlsafe(32)
    cid = new_id('c')
    db.execute('INSERT INTO credentials VALUES (?, ?, ?, ?, ?, ?, ?, NULL)',
               (cid, principal_id, 'api', _hash(db, token), json.dumps(scope) if scope else None, now(),
                now() + lifetime_seconds))
    return cid, token


def revoke_credential(db, credential_id):
    db.execute('UPDATE credentials SET revoked_at=? WHERE id=? AND revoked_at IS NULL', (now(), credential_id))


def rotate_credential(db, credential_id, lifetime_seconds):
    row = db.execute('SELECT principal_id, scope FROM credentials WHERE id=?', (credential_id,)).fetchone()
    if row is None:
        raise ServiceError('NOT_FOUND', 'credential')
    revoke_credential(db, credential_id)
    return issue_credential(db, row['principal_id'], lifetime_seconds, json.loads(row['scope']) if row['scope'] else None)


def authenticate_bearer(db, token):
    if not isinstance(token, str) or not token.startswith('mck_') or len(token) > 128:
        raise ServiceError('UNAUTHENTICATED')
    digest = _hash(db, token)
    # constant-time compare against the single matching row (hash is unique)
    row = db.execute('''SELECT c.id AS cid, c.secret_hash, c.expires_at, c.revoked_at, c.scope, p.* FROM credentials c
                        JOIN principals p ON p.id = c.principal_id WHERE c.secret_hash=?''', (digest,)).fetchone()
    if row is None or not hmac.compare_digest(row['secret_hash'], digest):
        raise ServiceError('UNAUTHENTICATED')
    if row['revoked_at'] is not None or row['expires_at'] <= now() or row['revoked_at'] is not None:
        raise ServiceError('UNAUTHENTICATED')
    if db.execute('SELECT revoked_at FROM principals WHERE id=?', (row['id'],)).fetchone()['revoked_at'] is not None:
        raise ServiceError('UNAUTHENTICATED')
    principal = Principal(row, credential_id=row['cid'])
    principal.scope = json.loads(row['scope']) if row['scope'] else None
    return principal


def create_session(db, principal_id, lifetime_seconds):
    sid, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
    db.execute('INSERT INTO sessions VALUES (?, ?, ?, ?, ?, NULL)', (sid, principal_id, csrf, now(), now() + lifetime_seconds))
    return sid, csrf


def authenticate_session(db, session_id):
    if not isinstance(session_id, str) or len(session_id) > 128:
        raise ServiceError('UNAUTHENTICATED')
    row = db.execute('''SELECT s.id AS sid, s.csrf, s.expires_at, s.revoked_at AS s_revoked, p.* FROM sessions s
                        JOIN principals p ON p.id = s.principal_id WHERE s.id=?''', (session_id,)).fetchone()
    if row is None or row['s_revoked'] is not None or row['expires_at'] <= now() or row['revoked_at'] is not None:
        raise ServiceError('UNAUTHENTICATED')
    principal = Principal(row, session={'id': row['sid'], 'csrf': row['csrf']})
    principal.scope = None
    return principal


def end_session(db, session_id):
    db.execute('UPDATE sessions SET revoked_at=? WHERE id=?', (now(), session_id))


def check_csrf(principal, token):
    if principal.session is None or not isinstance(token, str) \
            or not hmac.compare_digest(principal.session['csrf'], token):
        raise ServiceError('CSRF')


def revoke_principal(db, principal_id):
    db.execute('UPDATE principals SET revoked_at=? WHERE id=? AND revoked_at IS NULL', (now(), principal_id))
    db.execute('UPDATE credentials SET revoked_at=? WHERE principal_id=? AND revoked_at IS NULL', (now(), principal_id))
    db.execute('UPDATE sessions SET revoked_at=? WHERE principal_id=? AND revoked_at IS NULL', (now(), principal_id))
