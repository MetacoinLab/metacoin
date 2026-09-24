"""Scheduled local runs of explicitly enabled, bounded workflows (order §46 item 4).

A schedule names an immutable definition, its bindings and a per-run budget ceiling, local wall-clock
times in an IANA time zone, an overlap policy and a maximum number of starts. The worker's scheduler tick
starts due runs under the schedule's creator. DST: a local time that does not exist on a day (spring
forward) is skipped to the next day; an ambiguous time (fall back) fires at its first occurrence.
Definitions containing action-entitlement kinds (energy_audit) are refused: nothing recurring can ever
reserve or dispatch a payment. Ticks take an explicit clock for tests.
"""
import datetime as dt
import json
import secrets
import zoneinfo
from . import history
from .auth import Principal
from .db import now
from .errors import ServiceError

OVERLAP = ('skip', 'queue')
MAX_TIMES = 8
FUNDED_KINDS = ('energy_audit',)


def _tz(name):
    try:
        return zoneinfo.ZoneInfo(name)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError, TypeError):
        raise ServiceError('VALIDATION', {'code': 'timezone', 'detail': 'IANA zone name required'})


def next_occurrence(after_epoch, tz_name, times):
    """First local wall-clock occurrence strictly after `after_epoch`, DST-aware. Returns an epoch second."""
    tz = _tz(tz_name)
    after = dt.datetime.fromtimestamp(after_epoch, tz)
    day = after.date()
    for offset in range(0, 4):
        d = day + dt.timedelta(days=offset)
        for hhmm in sorted(times):
            h, m = (int(x) for x in hhmm.split(':'))
            local = dt.datetime(d.year, d.month, d.day, h, m, tzinfo=tz, fold=0)
            roundtrip = local.astimezone(dt.timezone.utc).astimezone(tz)
            if (roundtrip.hour, roundtrip.minute) != (h, m):
                continue                                              # non-existent local time (spring forward): skip
            if local > after:
                return int(local.timestamp())
    raise ServiceError('VALIDATION', {'code': 'no_occurrence', 'detail': 'no valid occurrence within four days'})


def validate(body):
    required = {'definition_id', 'timezone', 'times'}
    allowed = required | {'bindings', 'budget_ceiling', 'overlap', 'max_runs', 'name'}
    if type(body) is not dict or not required <= set(body) or not set(body) <= allowed:
        raise ServiceError('VALIDATION', {'code': 'schedule_fields', 'required': sorted(required), 'allowed': sorted(allowed)})
    times = body['times']
    if type(times) is not list or not 1 <= len(times) <= MAX_TIMES or not all(type(t) is str and len(t) == 5 and t[2] == ':' and t[:2].isdigit() and t[3:].isdigit()
                                                                              and 0 <= int(t[:2]) <= 23 and 0 <= int(t[3:]) <= 59 for t in times) or len(set(times)) != len(times):
        raise ServiceError('VALIDATION', {'code': 'times', 'expected': "up to %d distinct 'HH:MM' local times" % MAX_TIMES})
    _tz(body['timezone'])
    if body.get('overlap', 'skip') not in OVERLAP:
        raise ServiceError('VALIDATION', {'code': 'overlap', 'allowed': list(OVERLAP)})
    max_runs = body.get('max_runs', 30)
    if type(max_runs) is not int or not 1 <= max_runs <= 1000:
        raise ServiceError('VALIDATION', {'code': 'max_runs', 'range': [1, 1000]})
    if body.get('budget_ceiling') is not None and (type(body['budget_ceiling']) is not int or body['budget_ceiling'] < 0):
        raise ServiceError('VALIDATION', 'budget_ceiling')
    if body.get('bindings') is not None and (type(body['bindings']) is not dict or not all(type(k) is str and type(v) is str for k, v in body['bindings'].items())):
        raise ServiceError('VALIDATION', 'bindings')
    if 'name' in body and (type(body['name']) is not str or not 1 <= len(body['name']) <= 128):
        raise ServiceError('VALIDATION', 'name')
    return sorted(times), max_runs


class Schedules:
    def __init__(self, workflows, settings):
        self.workflows, self.settings = workflows, settings

    def create(self, db, principal, body, clock=None):
        principal.require('job:submit')
        times, max_runs = validate(body)
        drow = self.workflows.get_definition(db, principal, body['definition_id'])
        definition = json.loads(drow['definition_json'])
        funded = sorted({n['type'] for n in definition['nodes'] if n['type'] in FUNDED_KINDS})
        if funded:
            raise ServiceError('VALIDATION', {'code': 'funded_kind_refused', 'kinds': funded, 'detail': 'schedules never carry action entitlements; run such workflows explicitly'})
        from .workflows import slot_references
        if slot_references(definition):
            raise ServiceError('VALIDATION', {'code': 'template_not_schedulable', 'detail': 'instantiate the template first'})
        # the run must be startable now (bindings, budget) before anything is scheduled
        self.workflows.start_run(db, principal, body['definition_id'], bindings=body.get('bindings'), budget_ceiling=body.get('budget_ceiling'), preview=True)
        sid = 'sch_' + secrets.token_hex(8)
        t = clock if clock is not None else now()
        nxt = next_occurrence(t, body['timezone'], times)
        db.execute('INSERT INTO schedules (id, workspace, definition_id, name, bindings_json, budget_ceiling, timezone, times_json, overlap, max_runs, runs_started, runs_skipped, '
                   'enabled, disabled_reason, last_run_at, next_run_at, created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,0,0,1,NULL,NULL,?,?,?)',
                   (sid, principal.workspace, body['definition_id'], body.get('name') or drow['name'] + ' (scheduled)', json.dumps(body.get('bindings') or {}), body.get('budget_ceiling'),
                    body['timezone'], json.dumps(times), body.get('overlap', 'skip'), max_runs, nxt, principal.id, t))
        history.record(db, principal.workspace, principal.id, 'job.queued', 'schedule', sid, {'definition_id': body['definition_id'], 'times': times, 'timezone': body['timezone'], 'max_runs': max_runs})
        return self.view(db, principal, sid)

    def _row(self, db, principal, sid):
        row = db.execute('SELECT * FROM schedules WHERE id=? AND workspace=?', (sid, principal.workspace)).fetchone()
        if row is None:
            raise ServiceError('NOT_FOUND', 'schedule')
        return row

    def view(self, db, principal, sid):
        principal.require('job:read')
        r = self._row(db, principal, sid)
        tz = _tz(r['timezone'])
        runs = [dict(x) for x in db.execute("SELECT r.id, r.state, r.created_at FROM workflow_runs r JOIN events e ON e.object_id=r.id AND e.event_type='job.queued' AND e.object_type='workflow_run' "
                                            "WHERE e.ref_json LIKE ? ORDER BY r.created_at DESC LIMIT 20", ('%' + sid + '%',))]
        return {'schedule_id': sid, 'name': r['name'], 'definition_id': r['definition_id'], 'bindings': json.loads(r['bindings_json']), 'budget_ceiling': r['budget_ceiling'],
                'timezone': r['timezone'], 'times': json.loads(r['times_json']), 'overlap': r['overlap'], 'max_runs': r['max_runs'], 'runs_started': r['runs_started'], 'runs_skipped': r['runs_skipped'],
                'enabled': bool(r['enabled']), 'disabled_reason': r['disabled_reason'], 'last_run_at': r['last_run_at'], 'next_run_at': r['next_run_at'],
                'next_run_local': dt.datetime.fromtimestamp(r['next_run_at'], tz).isoformat() if r['next_run_at'] else None, 'created_by': r['created_by'], 'runs': runs,
                'note': 'no recurring action entitlements; each run is admitted like a manual run (budgets, quotas, grants)'}

    def list(self, db, principal):
        principal.require('job:read')
        return {'items': [self.view(db, principal, r['id']) for r in db.execute('SELECT id FROM schedules WHERE workspace=? ORDER BY created_at DESC LIMIT 100', (principal.workspace,))]}

    def control(self, db, principal, sid, action, clock=None):
        principal.require('job:submit' if action in ('enable', 'run-now') else 'job:cancel')
        r = self._row(db, principal, sid)
        t = clock if clock is not None else now()
        if action == 'disable':
            db.execute("UPDATE schedules SET enabled=0, disabled_reason='operator' WHERE id=?", (sid,))
        elif action == 'enable':
            if r['runs_started'] >= r['max_runs']:
                raise ServiceError('CONFLICT', {'code': 'max_runs_reached', 'max_runs': r['max_runs']})
            db.execute('UPDATE schedules SET enabled=1, disabled_reason=NULL, next_run_at=? WHERE id=?', (next_occurrence(t, r['timezone'], json.loads(r['times_json'])), sid))
        elif action == 'run-now':
            self._start(db, r, t, manual=True)
        elif action == 'delete':
            db.execute('DELETE FROM schedules WHERE id=?', (sid,))
            history.record(db, principal.workspace, principal.id, 'job.cancelled', 'schedule', sid, {'deleted': True})
            return {'schedule_id': sid, 'deleted': True}
        else:
            raise ServiceError('VALIDATION', {'code': 'action', 'allowed': ['enable', 'disable', 'run-now', 'delete']})
        history.record(db, principal.workspace, principal.id, 'job.cancelled' if action == 'disable' else 'job.queued', 'schedule', sid, {'action': action})
        return self.view(db, principal, sid)

    def _active_run_exists(self, db, sid):
        return db.execute("SELECT 1 FROM workflow_runs r JOIN events e ON e.object_id=r.id AND e.event_type='job.queued' AND e.object_type='workflow_run' "
                          "WHERE e.ref_json LIKE ? AND r.state IN ('created','running','waiting_review') LIMIT 1", ('%' + sid + '%',)).fetchone() is not None

    def _start(self, db, r, t, manual=False):
        owner_row = db.execute('SELECT * FROM principals WHERE id=?', (r['created_by'],)).fetchone()
        if owner_row is None or owner_row['revoked_at'] is not None:
            db.execute("UPDATE schedules SET enabled=0, disabled_reason='creator revoked' WHERE id=?", (r['id'],))
            return None
        owner = Principal(owner_row)
        try:
            out = self.workflows.start_run(db, owner, r['definition_id'], bindings=json.loads(r['bindings_json']), budget_ceiling=r['budget_ceiling'])
        except ServiceError as exc:
            db.execute("UPDATE schedules SET enabled=0, disabled_reason=? WHERE id=?", ('start refused: ' + exc.code, r['id']))
            history.record(db, r['workspace'], 'scheduler', 'job.cancelled', 'schedule', r['id'], {'disabled': exc.code})
            return None
        # bind the run to the schedule through the durable event log (the run's queued event carries the schedule id), then dispatch
        history.record(db, r['workspace'], 'scheduler', 'job.queued', 'workflow_run', out['run_id'], {'schedule_id': r['id'], 'manual': manual, 'due_at': r['next_run_at']})
        self.workflows.advance(db, out['run_id'])
        started = r['runs_started'] + 1
        disable = started >= r['max_runs']
        db.execute('UPDATE schedules SET runs_started=?, last_run_at=?, enabled=?, disabled_reason=? WHERE id=?',
                   (started, t, 0 if disable else 1, 'max_runs reached' if disable else None, r['id']))
        return out['run_id']

    def tick(self, db, clock=None, limit=20):
        """Start every due, enabled schedule (at most one start per schedule per tick). Returns the run ids started."""
        t = clock if clock is not None else now()
        started = []
        for r in db.execute('SELECT * FROM schedules WHERE enabled=1 AND next_run_at<=? ORDER BY next_run_at LIMIT ?', (t, limit)).fetchall():
            if r['overlap'] == 'skip' and self._active_run_exists(db, r['id']):
                db.execute('UPDATE schedules SET runs_skipped=runs_skipped+1, next_run_at=? WHERE id=?', (next_occurrence(r['next_run_at'], r['timezone'], json.loads(r['times_json'])), r['id']))
                history.record(db, r['workspace'], 'scheduler', 'job.retry_scheduled', 'schedule', r['id'], {'skipped': 'overlap', 'due_at': r['next_run_at']})
                continue
            rid = self._start(db, r, t)
            if rid:
                started.append(rid)
            db.execute('UPDATE schedules SET next_run_at=? WHERE id=?', (next_occurrence(max(t, r['next_run_at']), r['timezone'], json.loads(r['times_json'])), r['id']))
        return started
