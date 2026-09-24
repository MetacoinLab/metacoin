"""Worker registry, capabilities, heartbeats, draining, deterministic fair scheduling, persisted quotas.

Scheduling rule (deterministic, explainable): among queued jobs a live worker can run (kind within
its capabilities, no cancel requested), pick the job whose submitter has the fewest running jobs;
ties break by creation time then id. The queue view predicts this order and gives every queued job
a waiting reason. Quotas are persisted per workspace and per principal and checked at admission.
"""
import json
import secrets
from . import history
from .db import now
from .errors import ServiceError

HEARTBEAT_SECONDS = 10
STALE_AFTER = 3 * HEARTBEAT_SECONDS
WORKER_STATES = ('active', 'draining', 'offline')


def register(db, worker_id, name, capabilities):
    db.execute('INSERT INTO workers (id, name, capabilities_json, state, registered_at, last_heartbeat) VALUES (?,?,?,?,?,?) '
               'ON CONFLICT(id) DO UPDATE SET name=excluded.name, capabilities_json=excluded.capabilities_json, last_heartbeat=excluded.last_heartbeat, '
               "state=CASE WHEN workers.state='draining' THEN 'draining' ELSE 'active' END",
               (worker_id, name, json.dumps(sorted(capabilities)), 'active', now(), now()))


def heartbeat(db, worker_id, current_job_id=None):
    db.execute('UPDATE workers SET last_heartbeat=?, current_job_id=? WHERE id=?', (now(), current_job_id, worker_id))
    row = db.execute('SELECT state FROM workers WHERE id=?', (worker_id,)).fetchone()
    return row['state'] if row else 'offline'


def go_offline(db, worker_id):
    db.execute("UPDATE workers SET state='offline', current_job_id=NULL, last_heartbeat=? WHERE id=?", (now(), worker_id))


def live(row):
    return row['state'] != 'offline' and row['last_heartbeat'] >= now() - STALE_AFTER


def worker_view(row):
    return {'id': row['id'], 'name': row['name'], 'capabilities': json.loads(row['capabilities_json']), 'state': row['state'], 'live': live(row),
            'last_heartbeat': row['last_heartbeat'], 'registered_at': row['registered_at'], 'current_job_id': row['current_job_id'],
            'drained_at': row['drained_at']}


def workers(db, principal):
    principal.require('job:read')
    return {'items': [worker_view(r) for r in db.execute('SELECT * FROM workers ORDER BY registered_at, id')], 'stale_after_seconds': STALE_AFTER}


def set_worker_state(db, principal, worker_id, state):
    principal.require('admin:credentials')
    if state not in ('draining', 'active'):
        raise ServiceError('VALIDATION', 'state: draining | active')
    row = db.execute('SELECT * FROM workers WHERE id=?', (worker_id,)).fetchone()
    if row is None:
        raise ServiceError('NOT_FOUND', 'worker')
    db.execute('UPDATE workers SET state=?, drained_at=? WHERE id=?', (state, now() if state == 'draining' else None, worker_id))
    history.record(db, principal.workspace, principal.id, 'worker.state_set', 'worker', worker_id, {'state': state})
    return worker_view(db.execute('SELECT * FROM workers WHERE id=?', (worker_id,)).fetchone())


def fair_order(db, capabilities=None, workspace=None):
    """Deterministic order of queued jobs for a worker with the given capabilities (None = all kinds)."""
    running = {r['submitted_by']: r['n'] for r in db.execute("SELECT submitted_by, COUNT(*) AS n FROM jobs WHERE state='running' GROUP BY submitted_by")}
    sql = "SELECT id, kind, submitted_by, created_at, workspace FROM jobs WHERE state='queued' AND cancel_requested=0 AND hold=0"
    args = []
    if workspace:
        sql += ' AND workspace=?'; args.append(workspace)
    rows = [dict(r) for r in db.execute(sql + ' ORDER BY created_at, id LIMIT 500', args)]
    if capabilities is not None:
        rows = [r for r in rows if r['kind'] in capabilities]
    rows.sort(key=lambda r: (running.get(r['submitted_by'], 0), r['created_at'], r['id']))
    return rows


def next_job_id(db, capabilities):
    order = fair_order(db, capabilities)
    return order[0]['id'] if order else None


def queue(db, principal):
    """Queue position, predicted order and a waiting reason for every queued job in the workspace."""
    principal.require('job:read')
    ws = [dict(r) for r in db.execute('SELECT * FROM workers')]
    live_workers = [w for w in ws if live(w) and w['state'] == 'active']
    draining = [w for w in ws if live(w) and w['state'] == 'draining']
    caps = set()
    for w in live_workers:
        caps |= set(json.loads(w['capabilities_json']))
    global_order = fair_order(db, None)                              # what the next worker with every capability would take
    position = {r['id']: i for i, r in enumerate(global_order)}
    items = []
    for r in db.execute("SELECT id, kind, submitted_by, created_at, cancel_requested, hold FROM jobs WHERE workspace=? AND state='queued' ORDER BY created_at, id", (principal.workspace,)):
        crun = db.execute('SELECT device_policy, phase FROM compute_runs WHERE job_id=?', (r['id'],)).fetchone()
        if r['cancel_requested']:
            reason = 'cancel requested; will not be claimed'
        elif r['hold'] and crun and db.execute('SELECT preempted_for FROM compute_runs WHERE job_id=?', (r['id'],)).fetchone()['preempted_for']:
            reason = 'preempted at a durable checkpoint for a much smaller job; resumes automatically when the slot is free'
        elif r['hold']:
            reason = 'paused at a durable checkpoint; resume to continue'
        elif crun and crun['device_policy'] == 'gpu' and not any('device:cuda' in json.loads(w['capabilities_json']) for w in live_workers):
            reason = 'policy requires gpu; no live worker offers a cuda device'
        elif crun and db.execute("SELECT COUNT(*) FROM compute_reservations WHERE device=?", ('cuda' if crun['device_policy'] == 'gpu' else 'cpu',)).fetchone()[0] > 0 and crun['device_policy'] != 'auto':
            reason = 'waiting for a free %s compute slot' % ('cuda' if crun['device_policy'] == 'gpu' else 'cpu')
        elif not live_workers and draining:
            reason = 'all live workers are draining'
        elif not live_workers:
            reason = 'no live worker registered'
        elif r['kind'] not in caps:
            reason = 'no live worker declares capability ' + r['kind']
        else:
            ahead = [o for o in global_order[:position.get(r['id'], 0)] if o['kind'] in caps]
            reason = 'next to run' if not ahead else 'behind %d job(s) under fair share (fewest running jobs per submitter first)' % len(ahead)
        items.append({'job_id': r['id'], 'kind': r['kind'], 'submitted_by': r['submitted_by'], 'created_at': r['created_at'],
                      'predicted_position': position.get(r['id']), 'waiting_reason': reason})
    items.sort(key=lambda i: (i['predicted_position'] is None, i['predicted_position'] or 0, i['created_at'], i['job_id']))   # predicted claim order
    running = [dict(r) for r in db.execute("SELECT id, kind, lease_owner, lease_expires FROM jobs WHERE workspace=? AND state='running' ORDER BY updated_at", (principal.workspace,))]
    return {'queued': items, 'running': running, 'workers': [worker_view(w) for w in ws], 'live_capabilities': sorted(caps),
            'rule': 'fewest running jobs per submitter first, then oldest, then id; only live active workers with the capability claim'}


# ---- quotas ------------------------------------------------------------------------------
def quota_for(db, workspace, principal_id):
    row = db.execute('SELECT * FROM quotas WHERE workspace=? AND principal_id=?', (workspace, principal_id)).fetchone()
    if row is None:
        row = db.execute("SELECT * FROM quotas WHERE workspace=? AND principal_id='*'", (workspace,)).fetchone()
    return dict(row) if row else None


def set_quota(db, principal, target_principal, max_queued, max_per_minute):
    principal.require('admin:credentials')
    for v in (max_queued, max_per_minute):
        if type(v) is not int or not 0 <= v <= 100000:
            raise ServiceError('VALIDATION', 'max_queued and max_per_minute: integers 0..100000')
    if target_principal != '*' and db.execute('SELECT 1 FROM principals WHERE id=? AND workspace=?', (target_principal, principal.workspace)).fetchone() is None:
        raise ServiceError('NOT_FOUND', 'principal')
    db.execute('INSERT INTO quotas (workspace, principal_id, max_queued, max_per_minute, updated_at) VALUES (?,?,?,?,?) '
               'ON CONFLICT(workspace, principal_id) DO UPDATE SET max_queued=excluded.max_queued, max_per_minute=excluded.max_per_minute, updated_at=excluded.updated_at',
               (principal.workspace, target_principal, max_queued, max_per_minute, now()))
    history.record(db, principal.workspace, principal.id, 'quota.set', 'principal', target_principal, {'max_queued': max_queued, 'max_per_minute': max_per_minute})
    return quotas(db, principal)


def quotas(db, principal):
    principal.require('budget:read')
    return {'items': [dict(r) for r in db.execute('SELECT * FROM quotas WHERE workspace=? ORDER BY principal_id', (principal.workspace,))],
            'workspace_max_queued': None}


def check_admission(db, principal, extra=0):
    """Per-principal persisted quota (queued count and submissions per minute); refusals name the quota."""
    q = quota_for(db, principal.workspace, principal.id)
    if q is None:
        return None
    queued = db.execute("SELECT COUNT(*) FROM jobs WHERE submitted_by=? AND state IN ('queued','running')", (principal.id,)).fetchone()[0]
    if queued + extra + 1 > q['max_queued']:
        raise ServiceError('RATE_LIMITED', {'code': 'quota_max_queued', 'max_queued': q['max_queued'], 'in_flight': queued, 'quota_for': q['principal_id']})
    recent = db.execute('SELECT COUNT(*) FROM jobs WHERE submitted_by=? AND created_at > ?', (principal.id, now() - 60)).fetchone()[0]
    if recent + extra + 1 > q['max_per_minute']:
        raise ServiceError('RATE_LIMITED', {'code': 'quota_max_per_minute', 'max_per_minute': q['max_per_minute'], 'last_minute': recent, 'quota_for': q['principal_id']})
    return q
