"""Durable job queue: draft -> (frozen contract) -> queued -> running -> succeeded | failed | cancelled.
Review and payment states live in separate columns/tables and are never merged."""
import json
import secrets
from experiments.private_receipts import receipt as merkle
from experiments.work_contracts import contract as terms
from experiments.work_contracts.execution_state import Journal
from . import history
from .db import now
from .errors import ServiceError


class Jobs:
    def __init__(self, store, settings):
        self.store, self.settings = store, settings

    def journal(self, db, workspace):
        row = db.execute('SELECT * FROM campaigns WHERE workspace=?', (workspace,)).fetchone()
        if row is None:
            raise ServiceError('CAPABILITY_UNAVAILABLE', 'campaign not initialized for workspace')
        return Journal(self.settings.journal_path, row['campaign_id'], row['cap'])

    def _check_submittable(self, db, principal, contract_id, queued_extra=0):
        row = db.execute('SELECT * FROM contracts WHERE id=? AND workspace=?', (contract_id, principal.workspace)).fetchone()
        if row is None:
            raise ServiceError('NOT_FOUND', 'contract')
        if row['state'] != 'frozen':
            raise ServiceError('CONFLICT', 'contract must be frozen before submission')
        if row['expires_at'] <= now():
            raise ServiceError('EXPIRED', 'contract expired')
        if db.execute('SELECT 1 FROM jobs WHERE contract_id=?', (contract_id,)).fetchone():
            raise ServiceError('CONFLICT', 'a job already exists for this contract version')
        queued = db.execute("SELECT COUNT(*) FROM jobs WHERE workspace=? AND state IN ('queued','running')", (principal.workspace,)).fetchone()[0]
        if queued + queued_extra >= self.settings.limits['max_queued_per_workspace']:
            raise ServiceError('RATE_LIMITED')
        from . import scheduling
        scheduling.check_admission(db, principal, queued_extra)
        return row

    def _insert(self, db, principal, row, batch_id=None):
        if row['kind'] == 'energy_audit':
            # One action entitlement per job, pinned in the economic journal at submission.
            doc = merkle.parse(row['contract_json'])
            self.journal(db, principal.workspace).register(doc, row['contract_digest'], principal.id, now())
        from . import agents
        grant = agents.grant_of(principal)
        if grant and not getattr(principal, 'agent_counted', False):
            agents.guard(db, principal, 'job:submit', service_kind=row['kind'], jobs=1)
        jid = 'j_' + secrets.token_hex(8)
        db.execute('INSERT INTO jobs (id, workspace, contract_id, kind, state, retries_left, submitted_by, created_at, updated_at, batch_id) '
                   'VALUES (?,?,?,?,?,?,?,?,?,?)', (jid, principal.workspace, row['id'], row['kind'], 'queued',
                                                  self.settings.limits['job_max_retries'], principal.id, now(), now(), batch_id))
        history.record(db, principal.workspace, principal.id, 'job.queued', 'job', jid,
                       {'contract_id': row['id'], 'contract_digest': row['contract_digest'], 'kind': row['kind'], 'batch_id': batch_id, 'grant_id': grant})
        from .compute import manifests as compute_manifests
        if row['kind'] in compute_manifests.KINDS:
            params = json.loads(row['params_json'] or '{}')
            db.execute('INSERT INTO compute_runs (job_id, workspace, kind, manifest_id, manifest_version, implementation_digest, input_digest, device_policy, precision, phase, work_total, updated_at) '
                       'VALUES (?,?,?,?,?,?,?,?,?,?,?,?)', (jid, principal.workspace, row['kind'], params['manifest_id'], params['manifest_version'], params['implementation_digest'],
                                                             params['input_digest'], params['device_policy'], params['precision'], 'admitted', params['work_units'], now()))
        from .models import engine as model_engine, service as model_svc
        if row['kind'] in model_engine.KINDS or row['kind'] in model_engine.KNOWLEDGE_KINDS:
            model_svc.insert_request(db, jid, principal.workspace, row['kind'], json.loads(row['params_json'] or '{}'))
        from .datasets import add_edge
        add_edge(db, principal.workspace, 'contract', row['id'], 'job', jid, 'used_input')
        return jid

    def submit(self, db, principal, contract_id, reuse=False):
        principal.require('job:submit')
        row = self._check_submittable(db, principal, contract_id)
        if reuse:
            from . import reuse as reuse_mod, agents
            found = reuse_mod.lookup(db, principal, row)
            if found['hit']:
                grant = agents.grant_of(principal)
                if grant and not getattr(principal, 'agent_counted', False):
                    agents.guard(db, principal, 'job:submit', service_kind=row['kind'], jobs=1)
                return reuse_mod.submit_reused(db, principal, row, found['hit'], self.settings)
        return self._insert(db, principal, row)

    def submit_batch(self, db, principal, contract_ids):
        """All-or-nothing: every item is checked first and per-item decisions are returned;
        the aggregate action budget of energy-audit items must fit the campaign cap."""
        principal.require('job:submit')
        if type(contract_ids) is not list or not 1 <= len(contract_ids) <= self.settings.limits['batch_max_items'] \
                or len(set(contract_ids)) != len(contract_ids) or not all(type(c) is str for c in contract_ids):
            raise ServiceError('VALIDATION', 'contract_ids')
        decisions, rows, total = [], [], 0
        for index, cid in enumerate(contract_ids):
            try:
                row = self._check_submittable(db, principal, cid, queued_extra=index)
                rows.append(row)
                if row['kind'] == 'energy_audit':
                    total += json.loads(row['policy_json'])['amount']
                decisions.append({'contract_id': cid, 'accepted': True, 'code': None})
            except ServiceError as exc:
                decisions.append({'contract_id': cid, 'accepted': False, 'code': exc.code})
        if all(d['accepted'] for d in decisions):
            journal = self.journal(db, principal.workspace)
            if total and journal.exposure() + total > journal.limit:
                for d in decisions:
                    d.update(accepted=False, code='BUDGET_EXHAUSTED')
        if not all(d['accepted'] for d in decisions):
            return {'batch_id': None, 'submitted': False, 'items': decisions, 'aggregate_amount': total,
                    'note': 'nothing was queued; fix the refused items and resubmit'}
        bid = 'b_' + secrets.token_hex(8)
        db.execute('INSERT INTO batches VALUES (?,?,?,?,?,?)', (bid, principal.workspace, principal.id, len(rows), total, now()))
        for row, d in zip(rows, decisions):
            d['job_id'] = self._insert(db, principal, row, batch_id=bid)
        return {'batch_id': bid, 'submitted': True, 'items': decisions, 'aggregate_amount': total}

    def batch_progress(self, db, principal, batch_id):
        principal.require('job:read')
        batch = db.execute('SELECT * FROM batches WHERE id=? AND workspace=?', (batch_id, principal.workspace)).fetchone()
        if batch is None:
            raise ServiceError('NOT_FOUND', 'batch')
        counts = {r['state']: r['n'] for r in db.execute('SELECT state, COUNT(*) AS n FROM jobs WHERE batch_id=? GROUP BY state', (batch_id,))}
        reviews = {r['review_state']: r['n'] for r in db.execute('SELECT review_state, COUNT(*) AS n FROM jobs WHERE batch_id=? GROUP BY review_state', (batch_id,))}
        jobs = [dict(r) for r in db.execute('SELECT id, contract_id, state, review_state, outcome FROM jobs WHERE batch_id=? ORDER BY created_at, id', (batch_id,))]
        private = principal.can('job:read_private')
        return {'batch_id': batch_id, 'size': batch['size'], 'aggregate_amount': batch['total_amount'], 'created_at': batch['created_at'],
                'by_state': counts, 'by_review_state': reviews,
                'done': sum(counts.get(s, 0) for s in ('succeeded', 'failed', 'cancelled')) == batch['size'],
                'jobs': [dict(j, outcome=j['outcome'] if private else None) for j in jobs]}

    def get(self, db, principal, job_id):
        principal.require('job:read')
        row = db.execute('SELECT * FROM jobs WHERE id=? AND workspace=?', (job_id, principal.workspace)).fetchone()
        if row is None:
            raise ServiceError('NOT_FOUND', 'job')
        return row

    def list(self, db, principal, *, state=None, review_state=None, limit=None, before=None):
        principal.require('job:read')
        limit = min(int(limit or self.settings.limits['page_size']), self.settings.limits['page_size'])
        sql, args = 'SELECT * FROM jobs WHERE workspace=?', [principal.workspace]
        if state:
            sql += ' AND state=?'; args.append(state)
        if review_state:
            sql += ' AND review_state=?'; args.append(review_state)
        if before:
            sql += ' AND created_at < ?'; args.append(int(before))
        sql += ' ORDER BY created_at DESC, id DESC LIMIT ?'; args.append(limit + 1)
        rows = db.execute(sql, args).fetchall()
        more = len(rows) > limit
        return rows[:limit], more

    def cancel(self, db, principal, job_id):
        principal.require('job:cancel')
        row = self.get(db, principal, job_id)
        if row['state'] == 'queued':
            db.execute("UPDATE jobs SET state='cancelled', cancel_requested=1, updated_at=?, finished_at=? WHERE id=? AND state='queued'",
                       (now(), now(), job_id))
            history.record(db, principal.workspace, principal.id, 'job.cancelled', 'job', job_id, {'from': 'queued'})
            return 'cancelled'
        if row['state'] == 'running':
            db.execute("UPDATE jobs SET cancel_requested=1, updated_at=? WHERE id=?", (now(), job_id))
            history.record(db, principal.workspace, principal.id, 'job.cancelled', 'job', job_id, {'from': 'running', 'cooperative': True})
            return 'cancel_requested'
        raise ServiceError('CONFLICT', 'job is terminal')

    def view(self, db, principal, row, contract_row=None):
        """Projection filtered by permission: viewers never see private summaries."""
        contract_row = contract_row or db.execute('SELECT * FROM contracts WHERE id=?', (row['contract_id'],)).fetchone()
        pol = json.loads(contract_row['policy_json'])
        out = {'id': row['id'], 'kind': row['kind'], 'contract_id': row['contract_id'], 'state': row['state'],
               'review_state': row['review_state'], 'attempt': row['attempt'], 'lease_generation': row['lease_generation'],
               'retries_left': row['retries_left'], 'cancel_requested': bool(row['cancel_requested']),
               'error_code': row['error_code'], 'created_at': row['created_at'], 'updated_at': row['updated_at'],
               'finished_at': row['finished_at'], 'evidence_root': row['evidence_root'],
               'contract_digest': contract_row['contract_digest'], 'title': contract_row['title'],
               'reused_from': row['reused_from'] if 'reused_from' in row.keys() else None,
               'expires_at': contract_row['expires_at'], 'model_id': json.loads(contract_row['contract_json'])['model_id'] if contract_row['contract_json'] else None,
               'verifier_id': json.loads(contract_row['contract_json'])['verifier_id'] if contract_row['contract_json'] else None,
               'verifier_digest': json.loads(contract_row['contract_json'])['verifier_digest'] if contract_row['contract_json'] else None}
        private_ok = principal.can('job:read_private') or (principal.role == 'reviewer' and contract_row['reviewer_id'] == principal.id)
        public_ok = pol['disclose_outcome'] and row['review_state'] == 'accepted'
        if private_ok:
            out['outcome'] = row['outcome']
            out['summary'] = json.loads(row['summary_json']) if row['summary_json'] else None
        elif public_ok:
            out['outcome'] = row['outcome']
            out['summary'] = None
        else:
            out['outcome'] = 'withheld-by-policy-or-not-yet-reviewed' if row['outcome'] else None
            out['summary'] = None
        crun = db.execute('SELECT * FROM compute_runs WHERE job_id=?', (row['id'],)).fetchone()
        if crun:
            ver = json.loads(crun['verification_json']) if crun['verification_json'] else None
            out['compute'] = {'phase': crun['phase'], 'device_policy': crun['device_policy'], 'backend': crun['selected_backend'], 'backend_reason': crun['backend_reason'],
                              'work_total': crun['work_total'], 'work_committed': crun['work_committed'], 'work_computed': crun['work_computed'],
                              'checkpoint_generation': crun['checkpoint_generation'], 'control': crun['control'], 'hold': bool(row['hold']) if 'hold' in row.keys() else False,
                              'verification': ({'mode': ver.get('mode'), 'passed': ver.get('passed')} if ver else None), 'manifest_id': crun['manifest_id']}
        mreq = db.execute('SELECT * FROM model_requests WHERE job_id=?', (row['id'],)).fetchone()
        if mreq:
            out['model'] = {'phase': mreq['phase'], 'operation': mreq['operation'], 'revision_id': mreq['revision_id'], 'finish_reason': mreq['finish_reason'],
                            'input_tokens': mreq['input_tokens'], 'output_tokens': mreq['output_tokens'], 'items': mreq['items'], 'segments': mreq['segments'],
                            'verification': 'none: model output is data, not a verified result'}
        out['payment'] = self.payment_view(db, principal, row)
        out['next_operation'] = self.next_operation(row, contract_row, principal)
        return out

    def payment_view(self, db, principal, row):
        act = db.execute('SELECT * FROM payment_actions WHERE job_id=?', (row['id'],)).fetchone()
        empty = {'request_id': None, 'amount': None, 'capability': None, 'provider_mode': None, 'reference': None}
        if act is None:
            return dict(empty, state='NOT_REQUESTED')
        try:
            status = self.journal(db, principal.workspace).status(act['request_id'], json.loads(act['request_json'])['actor'])
        except Exception:
            return dict(empty, state='UNKNOWN_LOCAL_RECORD', request_id=act['request_id'], provider_mode=act['provider_mode'])
        return {'state': status['state'], 'request_id': act['request_id'], 'amount': status['amount'],
                'capability': status['capability'], 'provider_mode': act['provider_mode'],
                'reference': (status['result'] or {}).get('reference')}

    @staticmethod
    def next_operation(row, contract_row, principal):
        if row['state'] in ('queued', 'running'):
            return 'wait for the worker (or cancel)'
        if row['state'] == 'failed':
            return 'inspect error; amend the contract to a new version if inputs were invalid'
        if row['state'] == 'cancelled':
            return 'amend the contract and submit a new job'
        if row['review_state'] == 'none':
            return 'owner: request review'
        if row['review_state'] == 'requested':
            return 'designated reviewer: recompute and record a signed decision'
        if row['review_state'] == 'accepted' and row['kind'] == 'energy_audit':
            return 'owner: create the bounded payment action (dry run first), or export the public bundle'
        return 'export the public bundle'
