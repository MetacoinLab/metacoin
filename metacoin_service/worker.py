"""Background worker: atomic claim with a lease, bounded child execution, fenced publication."""
import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path
from experiments.private_receipts import receipt as merkle
from . import history
from .db import now
from .errors import ServiceError

ROOT = Path(__file__).resolve().parents[1]


class Worker:
    def __init__(self, database, store, settings, worker_id=None):
        self.db, self.store, self.settings = database, store, settings
        self.worker_id = worker_id or 'w_' + secrets.token_hex(6)
        self.lease = settings.limits['job_lease_seconds']

    def claim(self):
        """Atomically claim one queued job, or recover one whose lease expired."""
        with self.db.tx() as db:
            row = db.execute("SELECT id FROM jobs WHERE state='queued' AND cancel_requested=0 ORDER BY created_at LIMIT 1").fetchone()
            recovered = False
            if row is None:
                row = db.execute("SELECT id FROM jobs WHERE state='running' AND lease_expires < ? ORDER BY lease_expires LIMIT 1",
                                 (now(),)).fetchone()
                recovered = row is not None
            if row is None:
                return None
            generation = db.execute('SELECT lease_generation FROM jobs WHERE id=?', (row['id'],)).fetchone()[0] + 1
            changed = db.execute(
                "UPDATE jobs SET state='running', lease_owner=?, lease_expires=?, lease_generation=?, attempt=attempt+1, "
                "updated_at=? WHERE id=? AND lease_generation=? AND state IN ('queued','running')",
                (self.worker_id, now() + self.lease, generation, now(), row['id'], generation - 1)).rowcount
            if changed != 1:
                return None
            job = db.execute('SELECT * FROM jobs WHERE id=?', (row['id'],)).fetchone()
            db.execute('INSERT INTO attempts VALUES (?,?,?,?,?,NULL,NULL)',
                       ('at_' + secrets.token_hex(6), job['id'], generation, self.worker_id, now()))
            history.record(db, job['workspace'], self.worker_id, 'job.claimed', 'job', job['id'],
                           {'generation': generation, 'attempt': job['attempt'], 'recovered_expired_lease': recovered})
            return dict(job)

    def _spec(self, db, job):
        contract = db.execute('SELECT * FROM contracts WHERE id=?', (job['contract_id'],)).fetchone()
        doc = merkle.parse(contract['contract_json'])
        vault = self.store.load_json(db, contract['input_artifact_id'], job['workspace'])
        inputs = {f['name']: f['value'] for f in vault['fields']}['inputs']
        return contract, {'kind': job['kind'], 'contract': doc, 'contract_digest': contract['contract_digest'],
                          'input_root': contract['input_root'], 'input_vault': vault, 'inputs': inputs}

    def execute(self, job):
        """Run one attempt in a child process; publish only if the lease is still ours."""
        with self.db.read() as db:
            contract, spec = self._spec(db, job)
        limits = {'cpu': self.settings.limits['worker_cpu_seconds'], 'mem': self.settings.limits['worker_address_space_bytes'],
                  'out': self.settings.limits['job_output_bytes']}
        proc = subprocess.Popen([sys.executable, '-m', 'metacoin_service.worker_exec', json.dumps(limits)], cwd=ROOT,
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                env={'PATH': os.environ.get('PATH', ''), 'PYTHONPATH': str(ROOT), 'HOME': os.environ.get('HOME', '/')})
        try:
            out, _ = proc.communicate(merkle.canonical(spec), timeout=self.settings.limits['job_timeout_seconds'])
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            return self._finish(job, None, 'TIMEOUT')
        if self._cancelled(job):
            return self._finish(job, None, 'CANCELLED')
        try:
            result = json.loads(out[:self.settings.limits['job_output_bytes']]) if out else {'ok': False, 'code': 'COMPUTATION_ERROR'}
        except ValueError:
            result = {'ok': False, 'code': 'COMPUTATION_ERROR'}
        if not result.get('ok'):
            return self._finish(job, None, result.get('code', 'COMPUTATION_ERROR'))
        return self._finish(job, result, None)

    def _cancelled(self, job):
        with self.db.read() as db:
            return bool(db.execute('SELECT cancel_requested FROM jobs WHERE id=?', (job['id'],)).fetchone()[0])

    def _finish(self, job, result, error):
        with self.db.tx() as db:
            current = db.execute('SELECT * FROM jobs WHERE id=?', (job['id'],)).fetchone()
            fence = (current['state'] == 'running' and current['lease_owner'] == self.worker_id
                     and current['lease_generation'] == job['lease_generation'])
            if not fence:
                history.record(db, job['workspace'], self.worker_id, 'job.failed', 'job', job['id'],
                               {'fenced_out': True, 'generation': job['lease_generation']})
                return 'fenced'
            db.execute('UPDATE attempts SET finished_at=?, outcome=? WHERE job_id=? AND generation=?',
                       (now(), error or 'succeeded', job['id'], job['lease_generation']))
            if error is None:
                contract = db.execute('SELECT * FROM contracts WHERE id=?', (job['contract_id'],)).fetchone()
                reviewer_pub = db.execute('SELECT value FROM meta WHERE key=?', ('age_public:' + str(contract['reviewer_id']),)).fetchone()
                pol = json.loads(contract['policy_json'])
                aid = self.store.store(db, workspace=job['workspace'], kind='evidence_vault', owner_id=contract['owner_id'],
                                       plaintext=merkle.canonical(result['evidence_vault']),
                                       recipients=[reviewer_pub['value']] if reviewer_pub else [],
                                       intended_use='private-evidence;owner-and-designated-reviewer', job_id=job['id'],
                                       contract_id=job['contract_id'], retention_deadline=now() + pol['retention_seconds'])
                root = result['evidence_vault']['receipt']['root']
                db.execute("UPDATE jobs SET state='succeeded', evidence_artifact_id=?, evidence_root=?, outcome=?, summary_json=?, "
                           "lease_owner=NULL, lease_expires=NULL, updated_at=?, finished_at=? WHERE id=?",
                           (aid, root, result['outcome'], json.dumps(result['summary']), now(), now(), job['id']))
                history.record(db, job['workspace'], self.worker_id, 'job.result_committed', 'job', job['id'],
                               {'evidence_root': root, 'artifact_id': aid, 'generation': job['lease_generation']})
                from .datasets import add_edge
                add_edge(db, job['workspace'], 'job', job['id'], 'artifact', aid, 'produced')
                from . import metering
                metering.record_for_job(self.settings, db, db.execute('SELECT * FROM jobs WHERE id=?', (job['id'],)).fetchone())
                return 'succeeded'
            retryable = error in ('COMPUTATION_ERROR', 'TIMEOUT') and current['retries_left'] > 0
            if error == 'CANCELLED' or not retryable:
                state = 'cancelled' if error == 'CANCELLED' else 'failed'
                db.execute("UPDATE jobs SET state=?, error_code=?, lease_owner=NULL, lease_expires=NULL, updated_at=?, finished_at=? WHERE id=?",
                           (state, None if error == 'CANCELLED' else error, now(), now(), job['id']))
                history.record(db, job['workspace'], self.worker_id, 'job.cancelled' if state == 'cancelled' else 'job.failed',
                               'job', job['id'], {'error_code': error, 'generation': job['lease_generation']})
                return state
            db.execute("UPDATE jobs SET state='queued', retries_left=retries_left-1, error_code=?, lease_owner=NULL, "
                       "lease_expires=NULL, updated_at=? WHERE id=?", (error, now(), job['id']))
            history.record(db, job['workspace'], self.worker_id, 'job.retry_scheduled', 'job', job['id'],
                           {'error_code': error, 'retries_left': current['retries_left'] - 1})
            return 'retry'

    def run_once(self):
        job = self.claim()
        if job is None:
            return None
        return job['id'], self.execute(job)

    def tick_workflows(self):
        """Advance active workflow runs (scheduler tick); errors in one run do not stop the worker."""
        try:
            from .api import Services
            svc = getattr(self, '_svc', None) or Services(self.settings)
            self._svc = svc
            with self.db.tx() as db:
                return svc.workflows.advance_all(db) + svc.campaigns.tick_all(db)
        except Exception:
            return None

    def run_forever(self, poll_seconds=0.5, stop_file=None):
        while True:
            if stop_file and Path(stop_file).exists():
                return
            ran = self.run_once()
            advanced = self.tick_workflows()
            if ran is None and not advanced:
                time.sleep(poll_seconds)
