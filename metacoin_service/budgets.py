"""Hierarchical budgets: workspace -> workflow run -> workflow node ceilings with atomic reservations.

Every reservation is checked and recorded at every ancestor inside the caller's BEGIN IMMEDIATE
transaction, so concurrent workers can never over-reserve a shared parent. Amounts are integer
units of the workspace's one action asset (the same unit as the economic journal cap). This tree
governs workflow-run jobs; the economic journal remains the independent hard cap for dispatched
actions, and both must pass. Reservations are made when a node's job is created, committed when
the job succeeds (its entitlement persists), and released when it fails or is cancelled. A refusal
names the level that refused and whether waiting could help (reserved amounts may be released)
or not (committed amounts never return).
"""
import json
import secrets
from . import history
from .db import now
from .errors import ServiceError

KINDS = ('workspace', 'workflow_run', 'workflow_node', 'campaign')


def root(db, workspace):
    """The workspace node: created on demand with the economic campaign cap as its ceiling."""
    row = db.execute("SELECT * FROM budget_nodes WHERE workspace=? AND kind='workspace'", (workspace,)).fetchone()
    if row is not None:
        return row
    camp = db.execute('SELECT cap FROM campaigns WHERE workspace=?', (workspace,)).fetchone()
    ceiling = camp['cap'] if camp else 0
    nid = 'b_' + secrets.token_hex(6)
    db.execute('INSERT INTO budget_nodes (id, workspace, parent_id, kind, ref_id, ceiling, reserved, committed, created_at) VALUES (?,?,NULL,?,?,?,0,0,?)',
               (nid, workspace, 'workspace', workspace, ceiling, now()))
    return db.execute('SELECT * FROM budget_nodes WHERE id=?', (nid,)).fetchone()


def set_workspace_ceiling(db, principal, ceiling):
    principal.require('admin:credentials')
    if type(ceiling) is not int or not 0 <= ceiling <= 10 ** 12:
        raise ServiceError('VALIDATION', 'ceiling')
    r = root(db, principal.workspace)
    if ceiling < r['reserved'] + r['committed']:
        raise ServiceError('CONFLICT', {'code': 'ceiling_below_use', 'reserved': r['reserved'], 'committed': r['committed']})
    db.execute('UPDATE budget_nodes SET ceiling=? WHERE id=?', (ceiling, r['id']))
    history.record(db, principal.workspace, principal.id, 'budget.ceiling_set', 'budget_node', r['id'], {'ceiling': ceiling, 'kind': 'workspace'})
    return view_node(db, db.execute('SELECT * FROM budget_nodes WHERE id=?', (r['id'],)).fetchone())


def create_child(db, workspace, parent_id, kind, ref_id, ceiling):
    if kind not in KINDS or kind == 'workspace':
        raise ServiceError('VALIDATION', 'budget kind')
    if type(ceiling) is not int or ceiling < 0:
        raise ServiceError('VALIDATION', 'budget ceiling must be a non-negative integer')
    parent = db.execute('SELECT * FROM budget_nodes WHERE id=? AND workspace=?', (parent_id, workspace)).fetchone()
    if parent is None:
        raise ServiceError('NOT_FOUND', 'budget parent')
    if ceiling > parent['ceiling']:
        raise ServiceError('VALIDATION', {'code': 'ceiling_exceeds_parent', 'parent_kind': parent['kind'], 'parent_ceiling': parent['ceiling'], 'requested': ceiling})
    existing = db.execute('SELECT id FROM budget_nodes WHERE kind=? AND ref_id=?', (kind, ref_id)).fetchone()
    if existing:
        return existing['id']
    nid = 'b_' + secrets.token_hex(6)
    db.execute('INSERT INTO budget_nodes (id, workspace, parent_id, kind, ref_id, ceiling, reserved, committed, created_at) VALUES (?,?,?,?,?,?,0,0,?)',
               (nid, workspace, parent_id, kind, ref_id, ceiling, now()))
    return nid


def node_for(db, kind, ref_id):
    return db.execute('SELECT * FROM budget_nodes WHERE kind=? AND ref_id=?', (kind, ref_id)).fetchone()


def chain(db, node_id):
    """The node and its ancestors, nearest first."""
    out = []
    cur = db.execute('SELECT * FROM budget_nodes WHERE id=?', (node_id,)).fetchone()
    while cur is not None:
        out.append(cur)
        cur = db.execute('SELECT * FROM budget_nodes WHERE id=?', (cur['parent_id'],)).fetchone() if cur['parent_id'] else None
    return out


def check(db, node_id, amount):
    """Returns None if the amount fits at every level, else a refusal detail naming the first level that refuses."""
    for level in chain(db, node_id):
        available = level['ceiling'] - level['reserved'] - level['committed']
        if amount > available:
            return {'code': 'budget_exhausted', 'level': level['kind'], 'ref_id': level['ref_id'], 'ceiling': level['ceiling'], 'reserved': level['reserved'],
                    'committed': level['committed'], 'available': available, 'requested': amount,
                    'retryable': level['ceiling'] - level['committed'] >= amount,      # could fit once reservations release
                    'explanation': ('waiting: %d unit(s) are reserved by in-flight work at the %s level' % (level['reserved'], level['kind'])) if level['ceiling'] - level['committed'] >= amount
                    else ('blocked: the %s ceiling %d minus committed %d can never fit %d' % (level['kind'], level['ceiling'], level['committed'], amount))}
    return None


def reserve(db, workspace, node_id, amount, ref_type, ref_id):
    """Atomic reservation along the whole chain; idempotent per (ref_type, ref_id)."""
    if type(amount) is not int or amount < 0:
        raise ServiceError('VALIDATION', 'amount')
    existing = db.execute('SELECT * FROM budget_reservations WHERE ref_type=? AND ref_id=?', (ref_type, ref_id)).fetchone()
    if existing is not None:
        return existing['id']
    refusal = check(db, node_id, amount)
    if refusal is not None:
        raise ServiceError('BUDGET_EXHAUSTED', refusal)
    for level in chain(db, node_id):
        db.execute('UPDATE budget_nodes SET reserved=reserved+? WHERE id=?', (amount, level['id']))
    rid = 'br_' + secrets.token_hex(6)
    db.execute('INSERT INTO budget_reservations (id, workspace, node_id, amount, state, ref_type, ref_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)',
               (rid, workspace, node_id, amount, 'reserved', ref_type, ref_id, now(), now()))
    return rid


def settle(db, ref_type, ref_id, outcome):
    """outcome 'commit' (job succeeded; entitlement persists) or 'release' (failed/cancelled). Idempotent."""
    r = db.execute('SELECT * FROM budget_reservations WHERE ref_type=? AND ref_id=?', (ref_type, ref_id)).fetchone()
    if r is None or r['state'] != 'reserved':
        return r['state'] if r else None
    for level in chain(db, r['node_id']):
        if outcome == 'commit':
            db.execute('UPDATE budget_nodes SET reserved=reserved-?, committed=committed+? WHERE id=?', (r['amount'], r['amount'], level['id']))
        else:
            db.execute('UPDATE budget_nodes SET reserved=reserved-? WHERE id=?', (r['amount'], level['id']))
    state = 'committed' if outcome == 'commit' else 'released'
    db.execute('UPDATE budget_reservations SET state=?, updated_at=? WHERE id=?', (state, now(), r['id']))
    return state


def release_partial(db, ref_type, ref_id, keep):
    """Commit `keep` of a reservation and release the rest (award close: only paid/payable amounts stay committed). Idempotent."""
    r = db.execute('SELECT * FROM budget_reservations WHERE ref_type=? AND ref_id=?', (ref_type, ref_id)).fetchone()
    if r is None or r['state'] != 'reserved':
        return r['state'] if r else None
    keep = max(0, min(int(keep), r['amount']))
    for level in chain(db, r['node_id']):
        db.execute('UPDATE budget_nodes SET reserved=reserved-?, committed=committed+? WHERE id=?', (r['amount'], keep, level['id']))
    db.execute("UPDATE budget_reservations SET state='committed', amount=?, updated_at=? WHERE id=?", (keep, now(), r['id']))
    return 'committed'


def view_node(db, row, depth=0):
    children = db.execute('SELECT * FROM budget_nodes WHERE parent_id=? ORDER BY created_at, id', (row['id'],)).fetchall()
    reservations = db.execute("SELECT ref_type, ref_id, amount, state FROM budget_reservations WHERE node_id=? ORDER BY created_at", (row['id'],)).fetchall()
    return {'id': row['id'], 'kind': row['kind'], 'ref_id': row['ref_id'], 'ceiling': row['ceiling'], 'reserved': row['reserved'], 'committed': row['committed'],
            'available': row['ceiling'] - row['reserved'] - row['committed'], 'reservations': [dict(r) for r in reservations],
            'children': [view_node(db, c, depth + 1) for c in children] if depth < 6 else []}


def tree(db, principal):
    principal.require('budget:read')
    return {'unit': 'action units (one asset per workspace)', 'basis': 'reservations at job creation; committed on success; released on failure or cancellation',
            'tree': view_node(db, root(db, principal.workspace))}


def preview(db, principal, parent_ref, amounts):
    """Would these amounts (taken in order) fit under the parent (a run id, or the workspace)? Nothing is reserved."""
    principal.require('budget:read')
    if type(amounts) is not list or not 1 <= len(amounts) <= 200 or not all(type(a) is int and 0 <= a <= 10 ** 12 for a in amounts):
        raise ServiceError('VALIDATION', 'amounts: list of non-negative integers')
    node = node_for(db, 'workflow_run', parent_ref) if parent_ref else root(db, principal.workspace)
    if node is None or node['workspace'] != principal.workspace:
        raise ServiceError('NOT_FOUND', 'budget parent')
    levels = [dict(l) for l in chain(db, node['id'])]
    decisions = []
    for i, a in enumerate(amounts):
        refusal = None
        for level in levels:
            if a > level['ceiling'] - level['reserved'] - level['committed']:
                refusal = {'level': level['kind'], 'ref_id': level['ref_id'], 'available': level['ceiling'] - level['reserved'] - level['committed']}
                break
        if refusal is None:
            for level in levels:
                level['reserved'] += a
        decisions.append({'index': i, 'amount': a, 'fits': refusal is None, 'refused_at': refusal})
    return {'parent': {'kind': node['kind'], 'ref_id': node['ref_id']}, 'decisions': decisions, 'all_fit': all(d['fits'] for d in decisions), 'note': 'preview only; nothing reserved'}
