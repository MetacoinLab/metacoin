"""Server-enforced agent authority: policy grants bound to scoped credentials, counters, stop control.

A grant (`metacoin-agent-policy/v1`) is immutable once issued (its digest is recorded) and governs
exactly one scoped credential. Every server mutation performed with that credential passes through
`guard()`, which checks state, expiry, permitted services, allowed operations, and atomically
increments counters inside the caller's transaction. Counting is conservative: a job counts when
submitted (never decremented on failure or cancellation), amounts count when reserved (quote
accepted or action dispatched). Workflows and campaigns are charged their estimated job count
at start (the workflow estimate or the campaign candidate/evaluation total), so ceilings cannot be
evaded by nesting. Stop prevents new mutations and dispatches only; it never deletes evidence or
pretends a submitted payment is cancelled.
"""
import hashlib
import json
import secrets
from experiments.private_receipts import receipt as merkle
from . import auth, history
from .db import now
from .errors import ServiceError

POLICY_SCHEMA = 'metacoin-agent-policy/v1'
AGENT_OPERATIONS = ('services:read', 'quote', 'invoke', 'job:submit', 'job:read', 'workflow:run', 'campaign:run', 'dataset:read', 'usage:read', 'action:create', 'work:read', 'work:award')
OPERATION_TO_PERMISSION = {'services:read': ['contract:read'], 'quote': ['contract:create'], 'invoke': ['contract:create', 'contract:freeze', 'job:submit'], 'job:submit': ['job:submit'],
                           'job:read': ['job:read'], 'workflow:run': ['job:submit', 'contract:create', 'contract:freeze'], 'campaign:run': ['job:submit', 'contract:create', 'contract:freeze'],
                           'dataset:read': ['contract:read'], 'usage:read': ['budget:read'], 'action:create': ['action:create'],
                           'work:read': ['work:read'], 'work:award': ['work:read', 'work:award', 'budget:read']}      # Order 08 §65: an agent may award only under an explicit grant operation and its amount ceiling


def validate_policy(policy):
    merkle.canonical(policy)
    required = {'schema', 'permitted_services', 'allowed_operations', 'ceilings', 'validity_seconds', 'review_gate_mandatory', 'input_visibility'}
    if type(policy) is not dict or set(policy) != required or policy['schema'] != POLICY_SCHEMA:
        raise ServiceError('VALIDATION', {'code': 'policy_fields', 'required': sorted(required)})
    if type(policy['permitted_services']) is not list or not 1 <= len(policy['permitted_services']) <= 32 or not all(type(x) is str for x in policy['permitted_services']):
        raise ServiceError('VALIDATION', {'code': 'permitted_services'})
    ops = policy['allowed_operations']
    if type(ops) is not list or not ops or not set(ops) <= set(AGENT_OPERATIONS):
        raise ServiceError('VALIDATION', {'code': 'allowed_operations', 'allowed': list(AGENT_OPERATIONS)})
    c = policy['ceilings']
    if type(c) is not dict or set(c) != {'total_amount', 'per_action_amount', 'max_jobs', 'max_workflows', 'concurrency'} or not all(type(v) is int and 0 <= v <= 10 ** 9 for v in c.values()):
        raise ServiceError('VALIDATION', {'code': 'ceilings', 'required': ['total_amount', 'per_action_amount', 'max_jobs', 'max_workflows', 'concurrency']})
    if type(policy['validity_seconds']) is not int or not 60 <= policy['validity_seconds'] <= 30 * 86400:
        raise ServiceError('VALIDATION', {'code': 'validity_seconds'})
    if type(policy['review_gate_mandatory']) is not bool or policy['input_visibility'] not in ('own', 'workspace'):
        raise ServiceError('VALIDATION', {'code': 'review_gate_or_visibility'})
    return hashlib.sha256(b'metacoin/agent-policy/v1\0' + merkle.canonical(policy)).hexdigest()


class Agents:
    def __init__(self, settings):
        self.settings = settings

    def issue(self, db, issuer, policy):
        """Owner issues a grant + scoped credential. The agent can never widen it (no admin:credentials)."""
        issuer.require('admin:credentials')
        digest = validate_policy(policy)
        needed = sorted({p for o in policy['allowed_operations'] for p in OPERATION_TO_PERMISSION[o]} | {'job:read', 'contract:read'})
        if not all(issuer.can(p) for p in needed):
            raise ServiceError('FORBIDDEN', 'grant would exceed the issuer')
        gid = 'g_' + secrets.token_hex(8)
        cid, token = auth.issue_credential(db, issuer.id, policy['validity_seconds'],
                                           scope={'operations': needed, 'workspace': issuer.workspace, 'grant_id': gid})
        db.execute('INSERT INTO policy_grants VALUES (?,?,?,?,?,?,?,?,?,?,NULL,NULL)',
                   (gid, issuer.workspace, issuer.id, cid, json.dumps(policy), digest, 'active',
                    json.dumps({'jobs_created': 0, 'workflows_started': 0, 'amount_reserved': 0, 'actions': 0, 'invocations': 0}), now(), now() + policy['validity_seconds']))
        history.record(db, issuer.workspace, issuer.id, 'credential.issued', 'agent_grant', gid, {'credential_id': cid, 'policy_digest': digest})
        return {'grant_id': gid, 'credential_id': cid, 'token': token, 'policy_digest': digest, 'expires_at': now() + policy['validity_seconds'],
                'note': 'token shown once; every mutation is checked server-side against this immutable policy'}

    def get(self, db, principal, gid):
        row = db.execute('SELECT * FROM policy_grants WHERE id=? AND workspace=?', (gid, principal.workspace)).fetchone()
        if row is None:
            raise ServiceError('NOT_FOUND', 'grant')
        return row

    def view(self, db, principal, gid):
        principal.require('budget:read')
        g = self.get(db, principal, gid)
        counters = json.loads(g['counters_json'])
        policy = json.loads(g['policy_json'])
        agent_jobs = [dict(r) for r in db.execute("SELECT j.id, j.state, j.review_state FROM jobs j JOIN events e ON e.object_id=j.id AND e.event_type='job.queued' "
                                                    "WHERE j.workspace=? AND e.ref_json LIKE ? ORDER BY j.created_at", (g['workspace'], '%' + gid + '%')).fetchall()]
        unresolved = [dict(r) for r in db.execute("SELECT request_id, job_id FROM payment_actions WHERE workspace=? AND request_json LIKE ?", (g['workspace'], '%' + gid + '%')).fetchall()]
        return {'grant_id': gid, 'state': g['state'], 'issuer_id': g['issuer_id'], 'credential_id': g['credential_id'], 'policy': policy, 'policy_digest': g['digest'],
                'counters': counters, 'remaining': {'jobs': max(0, policy['ceilings']['max_jobs'] - counters['jobs_created']),
                                                    'workflows': max(0, policy['ceilings']['max_workflows'] - counters['workflows_started']),
                                                    'amount': max(0, policy['ceilings']['total_amount'] - counters['amount_reserved'])},
                'expires_at': g['expires_at'], 'stopped_at': g['stopped_at'],
                'obligations': {'jobs_in_flight': [j for j in agent_jobs if j['state'] in ('queued', 'running')], 'awaiting_review': [j for j in agent_jobs if j['review_state'] == 'requested'],
                                'unresolved_actions': unresolved}}

    def stop(self, db, principal, gid, revoke=False):
        principal.require('admin:credentials')
        g = self.get(db, principal, gid)
        if g['issuer_id'] != principal.id:
            raise ServiceError('FORBIDDEN', 'grant')
        state = 'revoked' if revoke else 'stopped'
        db.execute('UPDATE policy_grants SET state=?, stopped_at=COALESCE(stopped_at, ?) WHERE id=?', (state, now(), gid))
        if revoke:
            auth.revoke_credential(db, g['credential_id'])
        history.record(db, principal.workspace, principal.id, 'credential.revoked', 'agent_grant', gid, {'state': state})
        return self.view(db, principal, gid)

    def guard(self, db, principal, operation, **kw):
        return guard(db, principal, operation, **kw)

    def list(self, db, principal):
        principal.require('budget:read')
        rows = db.execute('SELECT id, state, issuer_id, credential_id, digest, counters_json, created_at, expires_at, stopped_at FROM policy_grants WHERE workspace=? ORDER BY created_at DESC LIMIT 200', (principal.workspace,)).fetchall()
        return {'items': [dict(dict(r), counters=json.loads(r['counters_json'])) for r in rows]}


    def simulate(self, db, principal, gid, operations):
        """Evaluate example operations against a grant without creating anything."""
        principal.require('budget:read')
        g = self.get(db, principal, gid)
        policy = json.loads(g['policy_json']); counters = json.loads(g['counters_json'])
        out = []
        for op in operations[:50]:
            if type(op) is not dict:
                out.append({'operation': op, 'allowed': False, 'reason': 'malformed'}); continue
            name = op.get('operation'); reason = None
            if g['state'] != 'active': reason = 'grant ' + g['state']
            elif name not in policy['allowed_operations']: reason = 'operation not allowed'
            elif op.get('service') and op['service'] not in policy['permitted_services']: reason = 'service not permitted'
            elif op.get('amount', 0) > policy['ceilings']['per_action_amount']: reason = 'per-action amount ceiling'
            elif counters['amount_reserved'] + op.get('amount', 0) > policy['ceilings']['total_amount']: reason = 'total amount ceiling'
            elif counters['jobs_created'] + op.get('jobs', 0) > policy['ceilings']['max_jobs']: reason = 'job ceiling'
            out.append({'operation': op, 'allowed': reason is None, 'reason': reason})
        return {'grant_id': gid, 'decisions': out, 'note': 'simulation only; nothing created or signed'}


def grant_of(principal):
    scope = getattr(principal, 'scope', None)
    return scope.get('grant_id') if scope else None


def guard(db, principal, operation, *, service_id=None, service_kind=None, amount=0, jobs=0, workflows=0, precheck_jobs=0):
    """No-op for non-agent principals. For agents: enforce and count atomically in the caller's transaction."""
    if True:
        scope = getattr(principal, 'scope', None)
        if not scope or 'grant_id' not in scope:
            return None
        g = db.execute('SELECT * FROM policy_grants WHERE id=?', (scope['grant_id'],)).fetchone()
        if g is None or g['state'] != 'active':
            raise ServiceError('FORBIDDEN', 'agent grant ' + (g['state'] if g else 'missing'))
        if g['expires_at'] <= now():
            raise ServiceError('EXPIRED', 'agent grant expired')
        policy = json.loads(g['policy_json'])
        if operation not in policy['allowed_operations']:
            raise ServiceError('FORBIDDEN', 'operation not in agent policy: ' + operation)
        if service_id is not None or service_kind is not None:
            allowed = policy['permitted_services']
            if not (service_id in allowed or service_kind in allowed):
                raise ServiceError('FORBIDDEN', 'service not permitted by agent policy')
        counters = json.loads(g['counters_json'])
        c = policy['ceilings']
        if amount and amount > c['per_action_amount']:
            raise ServiceError('BUDGET_EXHAUSTED', 'per-action amount exceeds the agent ceiling')
        if counters['amount_reserved'] + amount > c['total_amount']:
            raise ServiceError('BUDGET_EXHAUSTED', 'agent total amount ceiling')
        if counters['jobs_created'] + max(jobs, precheck_jobs) > c['max_jobs']:
            raise ServiceError('RATE_LIMITED', 'agent job ceiling reached')
        if counters['workflows_started'] + workflows > c['max_workflows']:
            raise ServiceError('RATE_LIMITED', 'agent workflow ceiling reached')
        if jobs:
            inflight = db.execute("SELECT COUNT(*) FROM jobs j JOIN events e ON e.object_id=j.id AND e.event_type='job.queued' WHERE j.workspace=? AND j.state IN ('queued','running') AND e.ref_json LIKE ?",
                                  (g['workspace'], '%' + g['id'] + '%')).fetchone()[0]
            if inflight + jobs > c['concurrency']:
                raise ServiceError('RATE_LIMITED', 'agent concurrency ceiling')
        counters['amount_reserved'] += amount; counters['jobs_created'] += jobs; counters['workflows_started'] += workflows
        counters['invocations'] += 1 if operation in ('invoke', 'quote') else 0
        db.execute('UPDATE policy_grants SET counters_json=? WHERE id=?', (json.dumps(counters), g['id']))
        return g['id']
