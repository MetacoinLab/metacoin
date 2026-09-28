"""Simple, explicit approval policies: a proposal binds the exact content of a high-impact change and the revision of
the object it touches; an independent principal approves that digest; apply executes the bound operation once.

This enforces CONFIGURED identities (the approver must be a different principal than the proposer; two credentials
of the same principal do not count as two people). It is not real-world organizational independence and it is not a
threshold-signature scheme. Actions covered: model promotion, calibration approval, node enrollment (the node's
identity, capabilities and workspace are fixed by the proposal) and the operator-marked high-impact action of
disabling calibrated scheduling. Routine development actions are not gated unless a policy row requires it."""
import hashlib
import json
import secrets

from experiments.private_receipts import receipt as merkle
from . import history
from .db import now
from .errors import ServiceError

ACTIONS = ('model_promote', 'calibration_approve', 'node_enroll', 'scheduling_toggle')
TTL_SECONDS = 24 * 3600


def _digest(obj):
    return hashlib.sha256(merkle.canonical(obj)).hexdigest()


class Approvals:
    def __init__(self, settings, services):
        self.settings, self.svc = settings, services

    # ---- policy -------------------------------------------------------------------------------------
    def policy(self, db, workspace):
        row = db.execute('SELECT value FROM meta WHERE key=?', ('approval_policy:' + workspace,)).fetchone()
        return json.loads(row['value']) if row else {'required': []}

    def set_policy(self, db, principal, required):
        principal.require('admin:credentials')
        if type(required) is not list or not set(required) <= set(ACTIONS):
            raise ServiceError('VALIDATION', {'code': 'required', 'allowed': list(ACTIONS)})
        db.execute('INSERT INTO meta (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', ('approval_policy:' + principal.workspace, json.dumps({'required': sorted(set(required))})))
        history.record(db, principal.workspace, principal.id, 'approval.proposed', 'policy', principal.workspace, {'required': sorted(set(required))})
        return self.policy(db, principal.workspace)

    def requires(self, db, workspace, action):
        return action in self.policy(db, workspace)['required']

    # ---- revision of the touched object (changes after proposal make the approval stale) ---------------
    def revision_of(self, db, action, content):
        if action == 'model_promote':
            row = db.execute('SELECT id, status, installed, weight_digest FROM model_revisions WHERE id=?', (content['revision_id'],)).fetchone()
            cur = db.execute('SELECT revision_id FROM model_defaults WHERE operation=?', (content['operation'],)).fetchone()
            return _digest({'target': dict(row) if row else None, 'current_default': cur['revision_id'] if cur else None})
        if action == 'calibration_approve':
            row = db.execute('SELECT id, state, verification_passed, implementation_digest FROM calibration_models WHERE id=?', (content['model_id'],)).fetchone()
            return _digest(dict(row) if row else None)
        if action == 'node_enroll':
            return _digest({'existing_with_key': bool(db.execute('SELECT 1 FROM nodes WHERE public_key_hex=? AND state!=?', (content['public_key_hex'], 'revoked')).fetchone())})
        if action == 'scheduling_toggle':
            row = db.execute('SELECT value FROM meta WHERE key=?', ('calibrated_scheduling_enabled',)).fetchone()
            return _digest({'enabled': row['value'] if row else '1'})
        raise ServiceError('VALIDATION', 'action')

    # ---- proposals ------------------------------------------------------------------------------------
    def propose(self, db, principal, action, content, note=''):
        principal.require('approval:propose')
        if action not in ACTIONS or type(content) is not dict or type(note) is not str or len(note) > 512:
            raise ServiceError('VALIDATION', {'code': 'proposal', 'actions': list(ACTIONS)})
        merkle.canonical(content)
        required = {'model_promote': {'revision_id', 'operation'}, 'calibration_approve': {'model_id'}, 'node_enroll': {'name', 'public_key_hex', 'capabilities', 'devices'}, 'scheduling_toggle': {'enabled'}}[action]
        if not required <= set(content):
            raise ServiceError('VALIDATION', {'code': 'content_fields', 'required': sorted(required)})
        pid = 'ap_' + secrets.token_hex(6)
        rev = self.revision_of(db, action, content)
        db.execute('INSERT INTO approvals (id, workspace, action, content_json, content_digest, revision_digest, state, proposed_by, note, created_at, expires_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                   (pid, principal.workspace, action, merkle.canonical(content).decode(), _digest(content), rev, 'proposed', principal.id, note, now(), now() + TTL_SECONDS))
        history.record(db, principal.workspace, principal.id, 'approval.proposed', 'approval', pid, {'action': action, 'content_digest': _digest(content)})
        return self.view(db, principal, pid)

    def row(self, db, principal, pid):
        r = db.execute('SELECT * FROM approvals WHERE id=? AND workspace=?', (pid, principal.workspace)).fetchone()
        if r is None:
            raise ServiceError('NOT_FOUND', 'approval')
        return r

    def view(self, db, principal, pid):
        principal.require('job:read')
        r = self.row(db, principal, pid)
        if r['state'] == 'proposed' and r['expires_at'] <= now():
            db.execute("UPDATE approvals SET state='expired' WHERE id=?", (pid,))
            history.record(db, principal.workspace, 'service', 'approval.expired', 'approval', pid, {})
            r = self.row(db, principal, pid)
        current_rev = None
        try:
            current_rev = self.revision_of(db, r['action'], json.loads(r['content_json']))
        except ServiceError:
            pass
        return {'id': r['id'], 'action': r['action'], 'content': json.loads(r['content_json']), 'content_digest': r['content_digest'], 'revision_digest': r['revision_digest'], 'state': r['state'],
                'proposed_by': r['proposed_by'], 'approved_by': r['approved_by'], 'decision_note': r['decision_note'], 'note': r['note'], 'created_at': r['created_at'], 'expires_at': r['expires_at'],
                'decided_at': r['decided_at'], 'applied_at': r['applied_at'], 'apply_result': json.loads(r['apply_result_json']) if r['apply_result_json'] else None,
                'stale': current_rev is not None and current_rev != r['revision_digest'],
                'independence': 'approver must be a different principal than the proposer (configured identities; not organizational independence; not a threshold signature)'}

    def list(self, db, principal):
        principal.require('job:read')
        return [self.view(db, principal, r['id']) for r in db.execute('SELECT id FROM approvals WHERE workspace=? ORDER BY created_at DESC LIMIT 100', (principal.workspace,)).fetchall()]

    def decide(self, db, principal, pid, decision, note=''):
        principal.require('approval:decide')
        if decision not in ('approved', 'rejected') or type(note) is not str or len(note) > 512:
            raise ServiceError('VALIDATION', 'decision/note')
        r = self.row(db, principal, pid)
        if r['state'] == 'proposed' and r['expires_at'] <= now():
            db.execute("UPDATE approvals SET state='expired' WHERE id=?", (pid,))
            raise ServiceError('EXPIRED', 'proposal expired')
        if r['state'] != 'proposed':
            if r['state'] in ('approved', 'rejected') and r['approved_by'] == principal.id and r['state'] == decision:
                return self.view(db, principal, pid)          # idempotent duplicate decision by the same approver
            raise ServiceError('CONFLICT', {'code': 'already_decided', 'state': r['state']})
        if r['proposed_by'] == principal.id:
            raise ServiceError('FORBIDDEN', {'code': 'same_principal', 'note': 'the approver must be a different principal; another credential of the proposer does not count'})
        current = self.revision_of(db, r['action'], json.loads(r['content_json']))
        if current != r['revision_digest']:
            db.execute("UPDATE approvals SET state='stale', decided_at=?, decision_note=? WHERE id=?", (now(), 'object changed since the proposal', pid))
            history.record(db, principal.workspace, principal.id, 'approval.decided', 'approval', pid, {'decision': 'stale'})
            return dict(self.view(db, principal, pid), refused={'code': 'stale_revision', 'note': 'the touched object changed after the proposal; propose again'}), 409
        db.execute('UPDATE approvals SET state=?, approved_by=?, decided_at=?, decision_note=? WHERE id=?', (decision, principal.id, now(), note, pid))
        history.record(db, principal.workspace, principal.id, 'approval.decided', 'approval', pid, {'decision': decision, 'content_digest': r['content_digest']})
        return self.view(db, principal, pid)

    def apply(self, db, principal, pid):
        """Execute the bound operation exactly once. Re-checks: state, expiry, approver still valid, revision unchanged."""
        principal.require('approval:propose')
        r = self.row(db, principal, pid)
        if r['state'] == 'applied':
            return self.view(db, principal, pid)               # idempotent
        if r['state'] != 'approved':
            raise ServiceError('CONFLICT', {'code': 'not_approved', 'state': r['state']})
        if r['expires_at'] <= now():
            db.execute("UPDATE approvals SET state='expired' WHERE id=?", (pid,))
            history.record(db, principal.workspace, 'service', 'approval.expired', 'approval', pid, {})
            return dict(self.view(db, principal, pid), refused={'code': 'approval_expired'}), 410
        approver = db.execute('SELECT revoked_at FROM principals WHERE id=?', (r['approved_by'],)).fetchone()
        if approver is None or approver['revoked_at'] is not None:
            db.execute("UPDATE approvals SET state='rejected', decision_note=? WHERE id=?", ('approver revoked before apply', pid))
            history.record(db, principal.workspace, principal.id, 'approval.decided', 'approval', pid, {'decision': 'rejected', 'reason': 'approver_revoked'})
            return dict(self.view(db, principal, pid), refused={'code': 'approver_revoked'}), 403
        content = json.loads(r['content_json'])
        if self.revision_of(db, r['action'], content) != r['revision_digest']:
            db.execute("UPDATE approvals SET state='stale' WHERE id=?", (pid,))
            history.record(db, principal.workspace, principal.id, 'approval.decided', 'approval', pid, {'decision': 'stale'})
            return dict(self.view(db, principal, pid), refused={'code': 'stale_revision'}), 409
        try:
            with _savepoint(db):
                result = self._execute(db, principal, r['action'], content)
        except ServiceError as exc:
            db.execute("UPDATE approvals SET state='apply_failed', apply_result_json=? WHERE id=?", (json.dumps({'error': exc.body()}), pid))
            history.record(db, principal.workspace, principal.id, 'approval.applied', 'approval', pid, {'failed': exc.code})
            return dict(self.view(db, principal, pid), refused={'code': 'apply_failed', 'error': exc.body()}), exc.status
        db.execute("UPDATE approvals SET state='applied', applied_at=?, apply_result_json=? WHERE id=?", (now(), json.dumps(result), pid))
        history.record(db, principal.workspace, principal.id, 'approval.applied', 'approval', pid, {'action': r['action']})
        return self.view(db, principal, pid)

    def _execute(self, db, principal, action, content):
        svc = self.svc
        p = _bypass(principal)
        if action == 'model_promote':
            return {'promoted': svc.models.promote(db, p, content['revision_id'], content['operation'], content.get('evidence'))['id']}
        if action == 'calibration_approve':
            return {'approved': svc.calibration.approve(db, p, content['model_id'], content.get('evidence'))['id']}
        if action == 'node_enroll':
            body = {k: content[k] for k in ('name', 'public_key_hex', 'capabilities', 'devices') if k in content}
            out = svc.federation.enroll(db, p, body)
            return {'node_id': out['node_id'], 'credential': out['credential'], 'note': 'credential shown once to the applier'}
        if action == 'scheduling_toggle':
            return svc.calibration.set_scheduling(db, p, bool(content['enabled']))
        raise ServiceError('VALIDATION', 'action')


class _savepoint:
    """Roll back only the failed application, keeping the approval record's own update."""

    def __init__(self, db):
        self.db = db

    def __enter__(self):
        self.db.execute('SAVEPOINT apply_op')

    def __exit__(self, et, ev, tb):
        if et is None:
            self.db.execute('RELEASE SAVEPOINT apply_op')
        else:
            self.db.execute('ROLLBACK TO SAVEPOINT apply_op'); self.db.execute('RELEASE SAVEPOINT apply_op')
        return False


def _bypass(principal):
    """Marks the principal so gated operations know an applied approval authorizes exactly this call."""
    principal.approval_applied = True
    return principal


def gate(approvals, db, principal, action):
    """Raise unless the workspace policy does not require approval for `action` or the call comes from apply()."""
    if getattr(principal, 'approval_applied', False):
        return
    if approvals.requires(db, principal.workspace, action):
        raise ServiceError('FORBIDDEN', {'code': 'approval_required', 'action': action, 'how': 'POST /api/v1/approvals {action, content}; an independent principal approves; then POST /api/v1/approvals/{id}/apply'})
