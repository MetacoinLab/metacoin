"""Repeated procurement policies (Order 08 §76.1): a requester defines a bounded recurring CLASS of work — a terms template
with a per-run ceiling, an aggregate ceiling and a maximum number of runs. Every run is a fresh agreement: new frozen terms
bound to new inputs (an input commitment already used by an earlier run is refused), a new request and fresh offers; nothing
is pre-awarded and no provider is pre-selected. The aggregate is enforced at award time inside the award transaction."""
import json
import secrets

from .. import history
from ..db import now
from ..errors import ServiceError
from . import terms as terms_mod

SCHEMA = 'metacoin-work-program/v1'
MAX_RUNS = 64


class Programs:
    def __init__(self, settings, services):
        self.settings, self.svc = settings, services

    def row(self, db, principal, pid):
        r = db.execute('SELECT * FROM work_programs WHERE id=? AND workspace=?', (pid, principal.workspace)).fetchone()
        if r is None:
            raise ServiceError('NOT_FOUND', 'program')
        return r

    def create(self, db, principal, body):
        principal.require('work:request')
        if type(body) is not dict or set(body) - {'name', 'class_terms', 'per_run_ceiling', 'aggregate_ceiling', 'max_runs', 'notes'}:
            raise ServiceError('VALIDATION', {'code': 'fields', 'allowed': ['name', 'class_terms', 'per_run_ceiling', 'aggregate_ceiling', 'max_runs', 'notes']})
        name = body.get('name'); cls = body.get('class_terms'); per = body.get('per_run_ceiling'); agg = body.get('aggregate_ceiling'); runs = body.get('max_runs', MAX_RUNS)
        if type(name) is not str or not 1 <= len(name) <= 120 or type(cls) is not dict or type(per) is not int or type(agg) is not int or type(runs) is not int or per <= 0 or agg < per or not 1 <= runs <= MAX_RUNS:
            raise ServiceError('VALIDATION', {'code': 'program', 'rule': '1 <= per_run_ceiling <= aggregate_ceiling; 1 <= max_runs <= %d; class_terms is a draft WorkTerms document' % MAX_RUNS})
        if cls.get('payment', {}).get('ceiling', per) > per:
            raise ServiceError('VALIDATION', {'code': 'class_ceiling_above_per_run', 'per_run_ceiling': per})
        cls = dict(cls, requester={'principal_id': principal.id, 'workspace': principal.workspace}) if 'requester' in cls else cls
        terms_mod.validate(dict(cls, payment=dict(cls.get('payment', {}), ceiling=min(cls.get('payment', {}).get('ceiling', per), per))), 'draft')
        pid = 'wp_' + secrets.token_hex(8)
        db.execute('INSERT INTO work_programs VALUES (?,?,?,?,?,?,?,?,?,?,?)', (pid, principal.workspace, principal.id, name, json.dumps(cls), per, agg, runs, 'active', (body.get('notes') or '')[:400], now()))
        history.record(db, principal.workspace, principal.id, 'work.program', 'work_program', pid, {'per_run_ceiling': per, 'aggregate_ceiling': agg, 'max_runs': runs})
        return self.view(db, principal, pid)

    def _runs(self, db, pid):
        return db.execute('SELECT * FROM work_program_runs WHERE program_id=? ORDER BY rowid', (pid,)).fetchall()

    def exposure(self, db, pid):
        """Aggregate of the ceilings of every award made under the program (closed awards count what they committed)."""
        total = 0
        for run in self._runs(db, pid):
            for a in db.execute('SELECT * FROM work_awards WHERE request_id=?', (run['request_id'],)).fetchall() if run['request_id'] else []:
                total += a['reserved'] if a['state'] == 'closed' else a['ceiling']
        return total

    def view(self, db, principal, pid):
        principal.require('work:read')
        r = self.row(db, principal, pid); runs = self._runs(db, pid)
        items = []
        for run in runs:
            aw = db.execute('SELECT id, state, ceiling, reserved, provider_id FROM work_awards WHERE request_id=?', (run['request_id'],)).fetchall() if run['request_id'] else []
            items.append({'run': run['run_no'], 'terms_id': run['terms_id'], 'request_id': run['request_id'], 'input_root': run['input_root'], 'awards': [dict(a) for a in aw], 'created_at': run['created_at']})
        return {'schema': SCHEMA, 'id': r['id'], 'name': r['name'], 'state': r['state'], 'per_run_ceiling': r['per_run_ceiling'], 'aggregate_ceiling': r['aggregate_ceiling'], 'max_runs': r['max_runs'], 'runs_used': len(runs),
                'aggregate_exposure': self.exposure(db, pid), 'aggregate_available': r['aggregate_ceiling'] - self.exposure(db, pid), 'runs': items, 'class_terms': json.loads(r['class_json']), 'notes': r['notes'],
                'rule': 'each run is a new frozen agreement with fresh inputs and fresh offers; the aggregate ceiling is enforced when an award is made; no provider is pre-selected'}

    def list(self, db, principal):
        principal.require('work:read')
        return [self.view(db, principal, r['id']) for r in db.execute('SELECT id FROM work_programs WHERE workspace=? ORDER BY created_at', (principal.workspace,)).fetchall()]

    def run(self, db, principal, pid, body):
        """Instantiate one run: class terms → new draft → frozen with the run's own inputs → request opened. Fresh offers follow."""
        principal.require('work:request')
        r = self.row(db, principal, pid)
        if r['requester_id'] != principal.id:
            raise ServiceError('FORBIDDEN', 'the program owner starts runs')
        if r['state'] != 'active':
            raise ServiceError('CONFLICT', {'code': 'program_state', 'state': r['state']})
        runs = self._runs(db, pid)
        if len(runs) >= r['max_runs']:
            raise ServiceError('BUDGET_EXHAUSTED', {'code': 'max_runs_reached', 'max_runs': r['max_runs']})
        if type(body) is not dict or type(body.get('inputs')) is not dict:
            raise ServiceError('VALIDATION', {'code': 'inputs', 'note': 'every run binds its own inputs'})
        ceiling = body.get('ceiling', r['per_run_ceiling'])
        if type(ceiling) is not int or not 0 < ceiling <= r['per_run_ceiling']:
            raise ServiceError('VALIDATION', {'code': 'run_ceiling', 'per_run_ceiling': r['per_run_ceiling']})
        if self.exposure(db, pid) + ceiling > r['aggregate_ceiling']:
            raise ServiceError('BUDGET_EXHAUSTED', {'code': 'aggregate_ceiling', 'exposure': self.exposure(db, pid), 'requested': ceiling, 'aggregate_ceiling': r['aggregate_ceiling']})
        cls = json.loads(r['class_json'])
        rule = {k: min(v, ceiling) if type(v) is int else v for k, v in cls['acceptance']['payment_rule'].items()}          # a smaller run ceiling bounds every payment class
        doc = dict(cls, payment=dict(cls['payment'], ceiling=ceiling), acceptance=dict(cls['acceptance'], payment_rule=rule),
                   milestones=[dict(m, max_payment=min(m['max_payment'], ceiling)) for m in cls['milestones']], title=(cls.get('title') or r['name']) + ' (run %d)' % (len(runs) + 1))
        T = self.svc.economy.terms
        t = T.create(db, principal, {'terms': doc})
        f = T.freeze(db, principal, t['id'], {'inputs': body['inputs']})
        op = (f.get('terms') or {}).get('operation') or {}
        root = op.get('inputs_digest') or op.get('input_root')                      # inputs_digest is deterministic for equal inputs; the vault root carries a per-freeze salt
        if root and any(x['input_root'] == root for x in runs):
            raise ServiceError('CONFLICT', {'code': 'fresh_inputs_required', 'note': 'an input commitment already bound by an earlier run of this program cannot be reused; each run must bring new inputs'})
        req = self.svc.economy.board.create_request(db, principal, {'terms_id': t['id']}); req = self.svc.economy.board.request_state(db, principal, req['id'], 'open')
        db.execute('INSERT INTO work_program_runs VALUES (?,?,?,?,?,?,?)', ('wpr_' + secrets.token_hex(6), pid, len(runs) + 1, t['id'], req['id'], root, now()))
        history.record(db, principal.workspace, principal.id, 'work.program_run', 'work_program', pid, {'run': len(runs) + 1, 'terms_id': t['id'], 'request_id': req['id'], 'ceiling': ceiling})
        return {'program_id': pid, 'run': len(runs) + 1, 'terms': f, 'request': req, 'note': 'fresh offers are required: providers offer on this request; the award enforces the aggregate ceiling'}

    def award_guard(self, db, rid, ceiling):
        """Called inside the award transaction: refuse an award that would exceed the program aggregate."""
        run = db.execute('SELECT * FROM work_program_runs WHERE request_id=?', (rid,)).fetchone()
        if run is None:
            return None
        r = db.execute('SELECT * FROM work_programs WHERE id=?', (run['program_id'],)).fetchone()
        if r['state'] != 'active':
            raise ServiceError('CONFLICT', {'code': 'program_state', 'state': r['state']})
        exposure = self.exposure(db, r['id'])
        if exposure + ceiling > r['aggregate_ceiling']:
            raise ServiceError('BUDGET_EXHAUSTED', {'code': 'program_aggregate_ceiling', 'program_id': r['id'], 'exposure': exposure, 'requested': ceiling, 'aggregate_ceiling': r['aggregate_ceiling']})
        return r['id']

    def close(self, db, principal, pid):
        principal.require('work:request')
        r = self.row(db, principal, pid)
        if r['requester_id'] != principal.id:
            raise ServiceError('FORBIDDEN', 'the program owner closes it')
        db.execute("UPDATE work_programs SET state='closed' WHERE id=?", (pid,))
        history.record(db, principal.workspace, principal.id, 'work.program', 'work_program', pid, {'state': 'closed'})
        return self.view(db, principal, pid)
