"""Background worker: atomic claim with a lease, bounded child execution, fenced publication."""
import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path
from experiments.private_receipts import receipt as merkle
from . import history, scheduling
from .compute import manifests as compute_manifests
from .compute.engine import ComputeEngine
from .models import engine as model_engine
from .db import now
from .errors import ServiceError

ROOT = Path(__file__).resolve().parents[1]


class Worker:
    def __init__(self, database, store, settings, worker_id=None, name=None, capabilities=None):
        self.db, self.store, self.settings = database, store, settings
        self.worker_id = worker_id or 'w_' + secrets.token_hex(6)
        self.name = name or self.worker_id
        from .contracts import KINDS
        self.compute = ComputeEngine(self)
        self.models = model_engine.ModelEngine(self)
        from .documents.engine import DocumentEngine
        self.documents = DocumentEngine(self)
        from .knowledge.engine import KnowledgeEngine
        self.knowledge = KnowledgeEngine(self)
        wanted = set(capabilities or KINDS)
        unknown = {c for c in wanted if c not in KINDS and not c.startswith('device:')}
        if unknown:
            raise ServiceError('VALIDATION', {'code': 'unknown_capabilities', 'unknown': sorted(unknown), 'installed': list(KINDS)})
        if not self.compute.runtime:                       # no numpy-capable interpreter: compute kinds are not offered
            wanted -= set(compute_manifests.KINDS) | {'document_import'}          # the extraction child runs under the same interpreter
        if not self.models.available():                    # no torch in the interpreter: model kinds are not offered
            wanted -= set(model_engine.KINDS) | set(model_engine.KNOWLEDGE_KINDS)
        wanted = {c for c in wanted if not c.startswith('device:')} | {'device:' + d for d in self.compute.devices}
        self.capabilities = sorted(wanted)
        self.lease = settings.limits['job_lease_seconds']
        from .db import MIGRATIONS
        with self.db.tx() as db:
            applied = [r[0] for r in db.execute('SELECT name FROM schema_migrations ORDER BY name')]
            expected = [name for name, _ in MIGRATIONS]
            if applied != expected:
                raise ServiceError('CONFLICT', {'code': 'schema_mismatch', 'applied': applied[-1] if applied else None, 'expected': expected[-1],
                                                'action': 'run `python -m metacoin_service migrate` (API and worker must run the same revision)'})
            scheduling.register(db, self.worker_id, self.name, self.capabilities)

    def heartbeat(self, current_job_id=None):
        with self.db.tx() as db:
            return scheduling.heartbeat(db, self.worker_id, current_job_id)

    def offline(self):
        with self.db.tx() as db:
            scheduling.go_offline(db, self.worker_id)
            db.execute('DELETE FROM compute_reservations WHERE worker_id=?', (self.worker_id,))
        self.models.host.drain_all('worker offline')

    def claim(self):
        """Atomically claim one queued job (fair order within our capabilities), or recover one whose lease expired."""
        with self.db.tx() as db:
            state = scheduling.heartbeat(db, self.worker_id)
            if state == 'draining':
                return None
            kinds = [c for c in self.capabilities if not c.startswith('device:')]
            row = None
            self.compute.resume_preempted(db)
            for cand in scheduling.fair_order(db, kinds):
                if cand['kind'] in compute_manifests.KINDS and self.compute.try_reserve(db, cand['id']) is None:
                    self.compute.preempt_if_fair(db, cand['id'])
                    continue                               # no compatible device slot for this job right now; try the next fair candidate
                row = db.execute("SELECT id FROM jobs WHERE id=? AND state='queued' AND cancel_requested=0 AND hold=0", (cand['id'],)).fetchone()
                if row:
                    break
                self.compute.release(db, cand['id'])
            recovered = False
            if row is None:
                placeholders = ','.join('?' * len(kinds))
                for cand in db.execute("SELECT id, kind, location_policy FROM jobs WHERE state='running' AND lease_expires < ? AND kind IN (" + placeholders + ") ORDER BY lease_expires LIMIT 10", (now(), *kinds)).fetchall():
                    if not scheduling.local_allowed(cand['location_policy']):
                        continue                                   # node-only work is recovered by another eligible node, never by a local worker
                    if cand['kind'] in compute_manifests.KINDS:
                        db.execute('DELETE FROM compute_reservations WHERE job_id=?', (cand['id'],))
                        if self.compute.try_reserve(db, cand['id']) is None:
                            continue
                    row = cand; break
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
            db.execute('UPDATE workers SET current_job_id=? WHERE id=?', (job['id'], self.worker_id))
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
        if job['kind'] in compute_manifests.KINDS:
            if self.models.available() and self.models.host.children:
                try:
                    self.models.host.drain_for_compute(self.settings.limits['compute_drain_min_available_bytes'], 'numerical compute job ' + job['id'])
                except Exception:
                    pass
            return self.compute.run(job)
        if job['kind'] in model_engine.KINDS:
            return self.models.run(job)
        if job['kind'] in model_engine.KNOWLEDGE_KINDS:
            return self.knowledge.run(job)
        if job['kind'] == 'verification_audit':
            return self._audit(job)
        if job['kind'] == 'document_import':
            return self.documents.run(job)
        try:
            with self.db.read() as db:
                contract, spec = self._spec(db, job)
        except ServiceError as exc:
            # the requester revoked or deleted a private input after dispatch: the attempt fails (INPUT_INVALID, not retried);
            # nothing is published from a source that is no longer available
            return self._finish(job, None, 'INPUT_INVALID')
        limits = {'cpu': self.settings.limits['worker_cpu_seconds'], 'mem': self.settings.limits['worker_address_space_bytes'],
                  'out': self.settings.limits['job_output_bytes']}
        proc = subprocess.Popen([sys.executable, '-m', 'metacoin_service.worker_exec', json.dumps(limits)], cwd=ROOT,
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                env=dict({'PATH': os.environ.get('PATH', ''), 'PYTHONPATH': str(ROOT), 'HOME': os.environ.get('HOME', '/')},
                                         **({'METACOIN_TEST_EXEC_DELAY_SECONDS': os.environ['METACOIN_TEST_EXEC_DELAY_SECONDS']} if 'METACOIN_TEST_EXEC_DELAY_SECONDS' in os.environ else {})))
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

    def _audit(self, job):
        """Independent audit executed in-process (pure Python); the verification record and the job result are
        published in ONE fenced transaction so a stale worker can never overwrite a newer statement."""
        from .api import Services
        svc = getattr(self, '_svc', None) or Services(self.settings)
        self._svc = svc
        try:
            with self.db.read() as db:
                contract, spec = self._spec(db, job)
                vrow, result, target, tcontract = svc.verification.compute_audit(db, self, job)
        except ServiceError as exc:
            with self.db.tx() as db:
                db.execute("UPDATE verification_jobs SET state='incomplete', result_json=? WHERE audit_job_id=? AND state='queued'", (json.dumps({'outcome': 'incomplete', 'error': exc.body()}), job['id']))
            return self._finish(job, None, 'COMPUTATION_ERROR' if exc.code in ('COMPUTATION', 'CAPABILITY_UNAVAILABLE') else 'INPUT_INVALID')
        with self.db.tx() as db:
            current = db.execute('SELECT * FROM jobs WHERE id=?', (job['id'],)).fetchone()
            if not (current['state'] == 'running' and current['lease_owner'] == self.worker_id and current['lease_generation'] == job['lease_generation']):
                return 'fenced'
            result, statement = svc.verification.finish_audit(db, vrow, result, target, tcontract)
            summary = {k: result.get(k) for k in ('outcome', 'checked', 'total', 'coverage', 'statement')}
            summary['verification_id'] = vrow['id']; summary['target_job_id'] = target['id']; summary['class'] = vrow['class']
            evidence = {'contract_digest': spec['contract_digest'], 'input_root': spec['input_root'], 'verifier_id': 'metacoin-verification/v1', 'verifier_digest': svc.verification.digest(),
                        'result_schema': 'verification-audit-result/v1', 'model_id': 'verification-audit/v1', 'result': summary, 'statement_digest': __import__('hashlib').sha256(merkle.canonical(statement)).hexdigest(),
                        'scope': 'verification-audit'}
            _, vault = merkle.commit(evidence)
            return self._finish_in(db, job, {'evidence_vault': vault, 'outcome': {'passed': 'AUDIT_PASSED', 'failed': 'AUDIT_FAILED', 'incomplete': 'AUDIT_INCOMPLETE'}[result['outcome']], 'summary': summary}, None)

    def _cancelled(self, job):
        with self.db.read() as db:
            return bool(db.execute('SELECT cancel_requested FROM jobs WHERE id=?', (job['id'],)).fetchone()[0])

    def _finish(self, job, result, error):
        with self.db.tx() as db:
            return self._finish_in(db, job, result, error)

    def _finish_in(self, db, job, result, error):
        if True:
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
                from . import metering, reuse
                done = db.execute('SELECT * FROM jobs WHERE id=?', (job['id'],)).fetchone()
                metering.record_for_job(self.settings, db, done)
                reuse.record(db, done, contract)
                if job['kind'] == 'calibration_fit':
                    from .calibration import Calibration
                    Calibration(self.store, self.settings).register_from_job(db, done, self.store)
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
        from . import tracing
        with tracing.span('worker.claim', service='metacoin-worker', worker=self.name):
            job = self.claim()
        if job is None:
            return None
        parent = None
        if tracing.ENABLED:
            with self.db.read() as db:
                ev = db.execute("SELECT ref_json FROM events WHERE object_id=? AND event_type='job.queued' ORDER BY seq LIMIT 1", (job['id'],)).fetchone()
            parent = (json.loads(ev['ref_json']).get('traceparent') if ev else None)
        try:
            with tracing.span('worker.execute', service='metacoin-worker', parent_traceparent=parent, job_id=job['id'], kind=job['kind'], worker=self.name):
                return job['id'], self.execute(job)
        finally:
            with self.db.tx() as db:
                db.execute('UPDATE workers SET current_job_id=NULL, last_heartbeat=? WHERE id=?', (now(), self.worker_id))

    def documents_service(self):
        from .api import Services
        svc = getattr(self, '_svc', None) or Services(self.settings)
        self._svc = svc
        return svc.documents

    def tick_workflows(self):
        """Advance active workflow runs (scheduler tick); errors in one run do not stop the worker."""
        try:
            from .api import Services
            svc = getattr(self, '_svc', None) or Services(self.settings)
            self._svc = svc
            with self.db.tx() as db:
                advanced = svc.workflows.advance_all(db) + svc.campaigns.tick_all(db) + svc.schedules.tick(db) + svc.packages.tick_all(db)
                ticked = svc.verification.tick(db)
                return advanced + ([{'verification_finalized': ticked}] if ticked else [])
        except Exception:
            return None

    def run_forever(self, poll_seconds=0.5, stop_file=None):
        last_beat = 0
        try:
            while True:
                if stop_file and Path(stop_file).exists():
                    return
                ran = self.run_once()
                advanced = self.tick_workflows()
                if time.time() - last_beat >= scheduling.HEARTBEAT_SECONDS:
                    self.heartbeat(); last_beat = time.time()
                    if self.models.available():
                        try:
                            self.models.host.apply_desired(self.models.registry); self.models.host.apply_warmup(self.models.registry); self.models.host.idle_cleanup()
                        except Exception:
                            pass
                if ran is None and not advanced:
                    time.sleep(poll_seconds)
        finally:
            self.offline()
