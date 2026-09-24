"""Worker-side compute orchestration: reservation, child process lifecycle, lease renewal, bounded progress
and telemetry, atomic encrypted checkpoint publication, pause/cancel, resume under a new fencing generation,
verification, and durable result publication through the ordinary job finish path."""
import hashlib
import json
import os
import secrets
import select
import shutil
import subprocess
import sys
import time
from pathlib import Path

from experiments.private_receipts import receipt as merkle
from . import container, manifests, npy, verify
from .. import history
from ..db import now

ROOT = Path(__file__).resolve().parents[2]
PROBE_CACHE = {}
# Evidence-based automatic backend selection (benchmark_compute on this DGX, 2026-09-24): the cuda path wins only for
# large heat grids (512^2: 9x, 1024^2: 10x warm), while temporal batches and Monte Carlo chunks are transfer/host-bound
# and run 1.3-4x faster on numpy. 'auto' therefore prefers cuda only above these work thresholds; 'gpu' always uses it.
AUTO_CUDA_MIN_WORK = {'heat_diffusion': 100, 'temporal_batch': None, 'monte_carlo_reliability': None}   # work units (heat: millions of cell updates)


def compute_interpreter(settings):
    """The trusted interpreter that carries numpy (and torch when present). Operator-configurable; probed once."""
    key = settings.compute_python or 'auto'
    if key in PROBE_CACHE:
        return PROBE_CACHE[key]
    candidates = [settings.compute_python] if settings.compute_python else ['/usr/bin/python3', sys.executable]
    found = None
    for cand in candidates:
        if not cand or not os.path.exists(cand):
            continue
        try:
            out = subprocess.run([cand, '-c', 'import json,numpy,sys\nd={"numpy":numpy.__version__,"python_version":sys.version.split()[0],"cuda":False,"torch":None,"device":None}\n'
                                  'try:\n import torch\n d["torch"]=torch.__version__\n d["cuda"]=bool(torch.cuda.is_available())\n d["device"]=torch.cuda.get_device_name(0) if d["cuda"] else None\n'
                                  ' d["capability"]="%d.%d"%torch.cuda.get_device_capability(0) if d["cuda"] else None\nexcept Exception as e:\n d["torch_error"]=type(e).__name__\nprint(json.dumps(d))'],
                                 capture_output=True, text=True, timeout=120, env={'PATH': os.environ.get('PATH', ''), 'HOME': os.environ.get('HOME', '/')})
            line = out.stdout.strip().splitlines()[-1] if out.stdout.strip() else ''
            info = json.loads(line)
            found = {'python': cand, **info}
            break
        except Exception:
            continue
    PROBE_CACHE[key] = found
    return found


def input_digest(inputs):
    return hashlib.sha256(merkle.canonical(inputs)).hexdigest()


def exactable(obj):
    """The evidence format is strict JSON without floats: floating-point values are carried as their shortest
    round-trip decimal strings (Python repr), which an ordinary client parses back exactly."""
    if isinstance(obj, float):
        return repr(obj)
    if isinstance(obj, dict):
        return {k: exactable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [exactable(v) for v in obj]
    return obj


class ComputeEngine:
    def __init__(self, worker):
        self.worker = worker
        self.settings = worker.settings
        self.limits = worker.settings.limits
        self.runtime = compute_interpreter(worker.settings)
        self.devices = ['cpu'] + (['cuda'] if self.runtime and self.runtime.get('cuda') else []) if self.runtime else []

    # ---- reservation (called inside the worker's claim transaction) ----------------------------------
    def try_reserve(self, db, job_id):
        run = db.execute('SELECT * FROM compute_runs WHERE job_id=?', (job_id,)).fetchone()
        if run is None or not self.runtime:
            return None
        policy = run['device_policy']
        wanted = {'cpu': ['cpu'], 'gpu': ['cuda'], 'auto': ['cuda', 'cpu']}[policy]
        auto_note = ''
        if policy == 'auto':
            threshold = AUTO_CUDA_MIN_WORK.get(run['kind'])
            if threshold is None or run['work_total'] < threshold:
                wanted = ['cpu', 'cuda']
                auto_note = '; measured evidence prefers cpu for this service at %d work units' % run['work_total']
            else:
                auto_note = '; measured evidence prefers cuda above %d work units (job has %d)' % (threshold, run['work_total'])
        db.execute('DELETE FROM compute_reservations WHERE expires_at < ?', (now(),))
        for device in wanted:
            if device not in self.devices:
                continue
            slots = self.limits['compute_gpu_slots'] if device == 'cuda' else self.limits['compute_cpu_slots']
            used = db.execute('SELECT COALESCE(SUM(slots),0) FROM compute_reservations WHERE device=?', (device,)).fetchone()[0]
            if used + 1 > slots:
                continue
            db.execute('INSERT OR REPLACE INTO compute_reservations (job_id, worker_id, device, slots, expires_at, created_at) VALUES (?,?,?,?,?,?)',
                       (job_id, self.worker.worker_id, device, 1, now() + self.limits['job_lease_seconds'], now()))
            reason = {'cpu': 'policy requires cpu', 'gpu': 'policy requires gpu', 'auto': 'automatic: first free device in the evidence-based preference order ' + ','.join(wanted)}[policy]
            db.execute("UPDATE compute_runs SET selected_backend=?, backend_reason=?, updated_at=? WHERE job_id=?", (device, reason + auto_note + ' (worker devices: %s)' % ','.join(self.devices), now(), job_id))
            return device
        return None

    def preempt_if_fair(self, db, waiting_job_id):
        """§53(4) chunk-aware fair preemption: a small job that cannot get a device slot may ask one much larger, already
        checkpointed job to pause at its next checkpoint. Accounting is untouched (the paused job keeps its committed units)
        and the preempted job is resumed automatically once the slot is free again. Returns the preempted job id or None."""
        run = db.execute('SELECT * FROM compute_runs WHERE job_id=?', (waiting_job_id,)).fetchone()
        if run is None:
            return None
        devices = {'cpu': ['cpu'], 'gpu': ['cuda'], 'auto': ['cpu', 'cuda']}[run['device_policy']]
        ratio = self.limits['compute_preempt_max_ratio_percent']
        for device in devices:
            holders = db.execute("SELECT r.job_id, c.work_total, c.checkpoint_generation, c.started_at, c.control, c.preempted_for FROM compute_reservations r JOIN compute_runs c ON c.job_id=r.job_id "
                                 "JOIN jobs j ON j.id=r.job_id WHERE r.device=? AND j.state='running' ORDER BY c.work_total DESC", (device,)).fetchall()
            for h in holders:
                if h['control'] or h['preempted_for'] or h['checkpoint_generation'] < 1 or not h['started_at']:
                    continue
                if h['work_total'] * ratio < run['work_total'] * 100 or now() - h['started_at'] < self.limits['compute_preempt_after_seconds']:
                    continue
                db.execute("UPDATE compute_runs SET control='pause', controlled_at=?, preempted_for=?, preempted_at=?, updated_at=? WHERE job_id=?", (now(), waiting_job_id, now(), now(), h['job_id']))
                history.record(db, run['workspace'], self.worker.worker_id, 'compute.control', 'job', h['job_id'], {'preempted_for': waiting_job_id, 'at_next_checkpoint': True})
                return h['job_id']
        return None

    def resume_preempted(self, db):
        """Release the hold of preempted jobs whose device slot is free again (called from the claim loop)."""
        for r in db.execute("SELECT c.job_id, c.device_policy, c.selected_backend, c.preempted_for FROM compute_runs c JOIN jobs j ON j.id=c.job_id WHERE c.preempted_for IS NOT NULL AND j.hold=1 AND j.state='queued'").fetchall():
            waiter = db.execute("SELECT state, hold FROM jobs WHERE id=?", (r['preempted_for'],)).fetchone()
            if waiter and waiter['state'] == 'queued' and not waiter['hold']:
                continue                                   # the job that asked for the slot has not had its turn yet
            device = r['selected_backend'] or 'cpu'
            slots = self.limits['compute_gpu_slots'] if device == 'cuda' else self.limits['compute_cpu_slots']
            used = db.execute('SELECT COALESCE(SUM(slots),0) FROM compute_reservations WHERE device=? AND expires_at > ?', (device, now())).fetchone()[0]
            if used < slots:
                db.execute("UPDATE jobs SET hold=0, updated_at=? WHERE id=?", (now(), r['job_id']))
                db.execute("UPDATE compute_runs SET phase='admitted', preempted_for=NULL, updated_at=? WHERE job_id=?", (now(), r['job_id']))
                history.record(db, db.execute('SELECT workspace FROM jobs WHERE id=?', (r['job_id'],)).fetchone()[0], self.worker.worker_id, 'compute.control', 'job', r['job_id'], {'auto_resumed_after_preemption': True})

    def waiting_reason(self, db, job_id):
        run = db.execute('SELECT device_policy FROM compute_runs WHERE job_id=?', (job_id,)).fetchone()
        if run is None:
            return None
        if not self.runtime:
            return 'no compute interpreter on this worker'
        if run['device_policy'] == 'gpu' and 'cuda' not in self.devices:
            return 'policy requires gpu; this worker has no cuda device'
        return 'waiting for a free compute slot'

    def release(self, db, job_id):
        db.execute('DELETE FROM compute_reservations WHERE job_id=?', (job_id,))

    # ---- execution -----------------------------------------------------------------------------------
    def run(self, job):
        """Executes one attempt end to end; returns the worker finish outcome string."""
        with self.worker.db.read() as db:
            contract, spec = self.worker._spec(db, job)
            run = dict(db.execute('SELECT * FROM compute_runs WHERE job_id=?', (job['id'],)).fetchone())
            latest = db.execute("SELECT * FROM compute_checkpoints WHERE job_id=? AND state='published' ORDER BY generation DESC LIMIT 1", (job['id'],)).fetchone()
        kind = job['kind']
        man = manifests.manifest(kind)
        if man['implementation_digest'] != run['implementation_digest'] or man['version'] != run['manifest_version']:
            return self._fail(job, 'MANIFEST_MISMATCH', {'accepted': run['implementation_digest'][:16], 'installed': man['implementation_digest'][:16]})
        backend = run['selected_backend'] or 'cpu'
        if backend not in self.devices:
            return self._fail(job, 'DEVICE_UNAVAILABLE', {'backend': backend, 'devices': self.devices})
        workdir = self.settings.home / 'compute' / job['id'] / ('attempt-%d-%s' % (job['lease_generation'], secrets.token_hex(3)))
        workdir.mkdir(mode=0o700, parents=True)
        try:
            resume_dir = None
            if latest is not None:
                resume_dir = workdir / 'resume'
                resume_dir.mkdir(mode=0o700)
                try:
                    with self.worker.db.read() as db:
                        blob = self.worker.store.load(db, latest['artifact_id'], job['workspace'])
                    if hashlib.sha256(blob).hexdigest() != latest['digest']:
                        raise ValueError('checkpoint digest mismatch')
                    for name, data in container.unpack(blob).items():
                        (resume_dir / name).write_bytes(data)
                except Exception as exc:
                    return self._fail(job, 'CHECKPOINT_INVALID', {'generation': latest['generation'], 'reason': type(exc).__name__ + ': ' + str(exc)[:80]})
            child_spec = {'job_id': job['id'], 'kind': kind, 'inputs': spec['inputs'], 'manifest': man, 'backend': backend, 'precision': run['precision'],
                          'attempt_generation': job['lease_generation'], 'input_digest': run['input_digest'], 'chunk': None,
                          'checkpoint_interval_seconds': self.limits['compute_checkpoint_interval_seconds'], 'resume_dir': str(resume_dir) if resume_dir else None,
                          'limits': {'cpu_seconds': self.limits['compute_cpu_seconds'], 'fsize_bytes': self.limits['compute_max_artifact_bytes'], 'threads': self.limits['compute_threads']}}
            (workdir / 'spec.json').write_bytes(json.dumps(child_spec).encode())
            return self._supervise(job, contract, spec, run, man, workdir, backend, latest)
        finally:
            with self.worker.db.tx() as db:
                self.release(db, job['id'])
            shutil.rmtree(workdir, ignore_errors=True)

    def _supervise(self, job, contract, spec, run, man, workdir, backend, latest):
        env = {'PATH': os.environ.get('PATH', ''), 'PYTHONPATH': str(ROOT), 'HOME': os.environ.get('HOME', '/'), 'PYTHONUNBUFFERED': '1'}
        if 'METACOIN_TEST_EXEC_DELAY_SECONDS' in os.environ:
            env['METACOIN_TEST_EXEC_DELAY_SECONDS'] = os.environ['METACOIN_TEST_EXEC_DELAY_SECONDS']
        log_path = workdir / 'stderr.log'
        log_file = open(log_path, 'wb')
        proc = subprocess.Popen([self.runtime['python'], '-m', 'metacoin_service.compute.exec', str(workdir)], cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log_file, env=env)
        started = time.time()
        self._update(job, phase='initializing', child_pid=proc.pid, started_at=now(), selected_backend=backend)
        history.record_safe(self.worker.db, job['workspace'], self.worker.worker_id, 'compute.control', 'job', job['id'], {'attempt': job['lease_generation'], 'backend': backend, 'resumed_from_generation': latest['generation'] if latest else None})
        last_lease, last_tele, last_progress_event = time.time(), 0.0, 0.0
        samples, control_sent, control_pending = [], None, None
        outcome = None
        buf = b''
        try:
            while True:
                if time.time() - started > self.limits['compute_timeout_seconds']:
                    proc.kill(); outcome = ('error', 'TIMEOUT', {}); break
                if time.time() - last_lease >= self.limits['compute_lease_renew_seconds']:
                    if not self._renew_lease(job):
                        proc.kill(); outcome = ('fenced', None, {}); break
                    last_lease = time.time()
                    with self.worker.db.tx() as db:
                        db.execute('UPDATE compute_reservations SET expires_at=? WHERE job_id=?', (now() + self.limits['job_lease_seconds'], job['id']))
                if time.time() - last_tele >= self.limits['compute_telemetry_interval_seconds']:
                    samples.append(self._sample(proc.pid, backend)); last_tele = time.time()
                    if len(samples) > 600:
                        samples = samples[-600:]
                if control_sent is None:
                    ctl = self._control(job)
                    if ctl in ('pause', 'cancel'):
                        try:
                            proc.stdin.write((ctl + '\n').encode()); proc.stdin.flush()
                        except (BrokenPipeError, OSError):
                            pass
                        control_sent = ctl
                        self._update(job, phase='cancelling' if ctl == 'cancel' else 'pausing')
                r, _, _ = select.select([proc.stdout], [], [], 0.5)
                if not r:
                    if proc.poll() is not None:
                        outcome = ('error', 'CHILD_EXITED', {'code': proc.returncode}); break
                    continue
                chunk = proc.stdout.read1(65536) if hasattr(proc.stdout, 'read1') else os.read(proc.stdout.fileno(), 65536)
                if not chunk:
                    outcome = ('error', 'CHILD_EXITED', {'code': proc.poll()}); break
                buf += chunk
                while b'\n' in buf:
                    line, buf = buf.split(b'\n', 1)
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    et = ev.get('event')
                    if et == 'started':
                        self._update(job, phase='running', versions_json=json.dumps({'backend': ev['backend'], 'versions': ev['versions'], 'threads': ev.get('threads'), 'energy_counter_start_mJ': ev.get('energy_counter_mJ')}))
                    elif et == 'resumed':
                        self._update(job, phase='running', work_committed=ev['committed'], work_computed=ev['committed'], checkpoint_generation=ev['generation'])
                    elif et == 'progress':
                        self._update(job, work_computed=ev['committed'], chunk_id=ev['chunk_id'], progress_json=json.dumps({'computed': ev['committed'], 'total': ev['total'], 'chunk_seconds': ev.get('chunk_seconds')}))
                        if time.time() - last_progress_event >= 2:
                            history.record_safe(self.worker.db, job['workspace'], self.worker.worker_id, 'compute.progress', 'job', job['id'], {'computed': ev['committed'], 'total': ev['total'], 'chunk_id': ev['chunk_id']})
                            last_progress_event = time.time()
                    elif et == 'checkpoint':
                        ok = self._publish_checkpoint(job, run, workdir / ev['dir'], ev, backend)
                        ack = control_sent if control_sent in ('pause', 'cancel') else ('continue' if ok else 'cancel')
                        try:
                            proc.stdin.write((ack + '\n').encode()); proc.stdin.flush()
                        except (BrokenPipeError, OSError):
                            pass
                    elif et == 'paused':
                        outcome = ('paused', None, ev); break
                    elif et == 'cancelled':
                        outcome = ('cancelled', None, ev); break
                    elif et == 'result':
                        outcome = ('result', None, ev); break
                    elif et == 'error':
                        outcome = ('error', ev.get('code', 'INTERNAL_DEFECT'), ev); break
                if outcome:
                    break
        finally:
            try:
                proc.stdin.close()
            except Exception:
                pass
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill(); proc.wait(timeout=10)
            log_file.close()
            tail = log_path.read_bytes()[-self.limits['compute_log_tail_bytes']:].decode(errors='replace') if log_path.exists() else ''
            self._update(job, telemetry_json=json.dumps(self._telemetry_summary(samples, backend)), log_tail=tail, child_pid=None)
        kind, code, ev = outcome
        if kind == 'fenced':
            return 'fenced'
        if kind == 'paused':
            return self._pause(job, ev)
        if kind == 'cancelled':
            self._update(job, phase='cancelled')
            return self.worker._finish(job, None, 'CANCELLED')
        if kind == 'error':
            if code == 'CHILD_EXITED':
                self._update(job, phase='interrupted')
                return self.worker._finish(job, None, 'COMPUTATION_ERROR')          # retryable: the next attempt resumes from the last published checkpoint
            return self._fail(job, code, {k: v for k, v in ev.items() if k in ('reason', 'code')})
        return self._complete(job, contract, spec, run, man, workdir / ev['out_dir'], ev, backend)

    # ---- helpers -------------------------------------------------------------------------------------
    def _update(self, job, **cols):
        cols['updated_at'] = now()
        sets = ', '.join(k + '=?' for k in cols)
        with self.worker.db.tx() as db:
            db.execute('UPDATE compute_runs SET ' + sets + ' WHERE job_id=?', (*cols.values(), job['id']))

    def _control(self, job):
        with self.worker.db.read() as db:
            row = db.execute('SELECT control FROM compute_runs WHERE job_id=?', (job['id'],)).fetchone()
            cancel = db.execute('SELECT cancel_requested FROM jobs WHERE id=?', (job['id'],)).fetchone()[0]
        if cancel:
            return 'cancel'
        return row['control'] if row else None

    def _renew_lease(self, job):
        with self.worker.db.tx() as db:
            changed = db.execute("UPDATE jobs SET lease_expires=?, updated_at=? WHERE id=? AND state='running' AND lease_owner=? AND lease_generation=?",
                                 (now() + self.limits['job_lease_seconds'], now(), job['id'], self.worker.worker_id, job['lease_generation'])).rowcount
        return changed == 1

    def _sample(self, pid, backend):
        s = {'t': now(), 'source': {'cpu_seconds': '/proc/<pid>/stat (child process, utime+stime)', 'rss_bytes': '/proc/<pid>/status VmRSS (child process)'}}
        try:
            fields = open('/proc/%d/stat' % pid).read().split(')')[-1].split()
            s['cpu_seconds'] = (int(fields[11]) + int(fields[12])) / os.sysconf('SC_CLK_TCK')
            for line in open('/proc/%d/status' % pid):
                if line.startswith('VmRSS:'):
                    s['rss_bytes'] = int(line.split()[1]) * 1024
        except (OSError, ValueError, IndexError):
            s['cpu_seconds'] = None; s['rss_bytes'] = None
        if backend == 'cuda':
            try:
                q = subprocess.run(['nvidia-smi', '--query-gpu=utilization.gpu,temperature.gpu,power.draw', '--format=csv,noheader,nounits'], capture_output=True, text=True, timeout=5)
                u, t, p = [x.strip() for x in q.stdout.strip().split(',')]
                s['gpu'] = {'utilization_percent': int(u) if u.isdigit() else None, 'temperature_c': int(t) if t.isdigit() else None, 'power_w': float(p) if p.replace('.', '', 1).isdigit() else None,
                            'scope': 'device-wide (nvidia-smi); not per process'}
            except Exception:
                s['gpu'] = {'utilization_percent': None, 'temperature_c': None, 'power_w': None, 'scope': 'unavailable'}
        return s

    def _telemetry_summary(self, samples, backend):
        cpu = [s['cpu_seconds'] for s in samples if s.get('cpu_seconds') is not None]
        rss = [s['rss_bytes'] for s in samples if s.get('rss_bytes') is not None]
        out = {'samples': len(samples), 'interval_seconds': self.limits['compute_telemetry_interval_seconds'], 'backend': backend,
               'child_cpu_seconds_max': max(cpu) if cpu else None, 'child_rss_bytes_max': max(rss) if rss else None,
               'scope': 'child process CPU/RSS from /proc; device-wide GPU readings from nvidia-smi; missing readings are reported as null, never zero'}
        if backend == 'cuda':
            g = [s['gpu'] for s in samples if s.get('gpu')]
            util = [x['utilization_percent'] for x in g if x['utilization_percent'] is not None]
            power = [x['power_w'] for x in g if x['power_w'] is not None]
            temp = [x['temperature_c'] for x in g if x['temperature_c'] is not None]
            out['gpu'] = {'utilization_percent_max': max(util) if util else None, 'utilization_percent_mean': sum(util) / len(util) if util else None,
                          'power_w_max': max(power) if power else None, 'temperature_c_max': max(temp) if temp else None, 'readings': len(g),
                          'power_integral_estimate_j': (sum(power) / len(power)) * (len(power) - 1) * self.limits['compute_telemetry_interval_seconds'] if len(power) > 1 else None,
                          'power_integral_method': 'mean sampled power x observed interval (estimate, device-wide, gaps ignored); not a hardware energy counter',
                          'unavailable': ['memory (not supported on this device)']}
        return out

    def _publish_checkpoint(self, job, run, ckpt_dir, ev, backend):
        """Pack, encrypt, store and record in one transaction; nothing references the checkpoint before the object is complete."""
        try:
            files = {p.name: p.read_bytes() for p in ckpt_dir.iterdir() if p.is_file()}
            meta = json.loads(files['meta.json'])
            if meta['job_id'] != job['id'] or meta['generation'] != ev['generation']:
                raise ValueError('checkpoint metadata does not bind this job')
            blob = container.pack(files)
            digest = hashlib.sha256(blob).hexdigest()
            with self.worker.db.tx() as db:
                fence = db.execute("SELECT 1 FROM jobs WHERE id=? AND state='running' AND lease_owner=? AND lease_generation=?", (job['id'], self.worker.worker_id, job['lease_generation'])).fetchone()
                if not fence:
                    return False
                contract = db.execute('SELECT owner_id, reviewer_id FROM contracts WHERE id=?', (job['contract_id'],)).fetchone()
                aid = self.worker.store.store(db, workspace=job['workspace'], kind='compute_checkpoint', owner_id=contract['owner_id'], plaintext=blob, recipients=[],
                                              intended_use='compute-checkpoint;worker-resume', job_id=job['id'], contract_id=job['contract_id'], limit_bytes=self.limits['compute_max_artifact_bytes'])
                prev = db.execute('SELECT COALESCE(MAX(committed_units),0) FROM compute_checkpoints WHERE job_id=?', (job['id'],)).fetchone()[0]
                db.execute('INSERT INTO compute_checkpoints (id, job_id, generation, attempt_generation, artifact_id, committed_units, boundary_json, backend, digest, state, published_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                           ('ck_' + secrets.token_hex(6), job['id'], ev['generation'], job['lease_generation'], aid, ev['committed'], json.dumps(meta['boundary']), backend, digest, 'published', now()))
                if ev['committed'] > prev:
                    db.execute('INSERT OR IGNORE INTO compute_work_units (job_id, unit_from, unit_to, generation, attempt_generation, committed_at) VALUES (?,?,?,?,?,?)',
                               (job['id'], prev, ev['committed'], ev['generation'], job['lease_generation'], now()))
                db.execute('UPDATE compute_runs SET work_committed=MAX(work_committed, ?), checkpoint_generation=?, phase=?, updated_at=? WHERE job_id=?',
                           (ev['committed'], ev['generation'], 'checkpointing', now(), job['id']))
                history.record(db, job['workspace'], self.worker.worker_id, 'compute.checkpoint', 'job', job['id'], {'generation': ev['generation'], 'committed': ev['committed'], 'artifact_id': aid, 'reason': ev.get('reason')})
                # retention: keep the newest N generations; older payloads are unlinked (never the one just written or its predecessor)
                keep = self.limits['compute_checkpoints_retained']
                old = db.execute("SELECT id, artifact_id FROM compute_checkpoints WHERE job_id=? AND state='published' ORDER BY generation DESC LIMIT -1 OFFSET ?", (job['id'], keep)).fetchall()
                for o in old:
                    try:
                        self.worker.store.delete_payload(db, o['artifact_id'], job['workspace'], 'checkpoint retention')
                        db.execute("UPDATE compute_checkpoints SET state='retired' WHERE id=?", (o['id'],))
                    except Exception:
                        pass
            self._update(job, phase='running')
            return True
        except Exception as exc:
            self._update(job, phase='running', log_tail='checkpoint publication failed: ' + type(exc).__name__)
            return False

    def _pause(self, job, ev):
        with self.worker.db.tx() as db:
            fence = db.execute("SELECT 1 FROM jobs WHERE id=? AND state='running' AND lease_owner=? AND lease_generation=?", (job['id'], self.worker.worker_id, job['lease_generation'])).fetchone()
            if not fence:
                return 'fenced'
            db.execute("UPDATE jobs SET state='queued', hold=1, lease_owner=NULL, lease_expires=NULL, updated_at=? WHERE id=?", (now(), job['id']))
            db.execute("UPDATE attempts SET finished_at=?, outcome='paused' WHERE job_id=? AND generation=?", (now(), job['id'], job['lease_generation']))
            db.execute("UPDATE compute_runs SET phase='paused', control=NULL, updated_at=? WHERE job_id=?", (now(), job['id']))
            self.release(db, job['id'])
            history.record(db, job['workspace'], self.worker.worker_id, 'compute.control', 'job', job['id'], {'paused_at_generation': ev.get('generation'), 'committed': ev.get('committed')})
        return 'paused'

    def _fail(self, job, code, detail):
        self._update(job, phase='failed', verification_json=json.dumps({'failure': code, 'detail': detail}))
        return self.worker._finish(job, None, code)

    def _complete(self, job, contract, spec, run, man, out_dir, ev, backend):
        files = {p.name: p.read_bytes() for p in out_dir.iterdir() if p.is_file()}
        self._update(job, phase='verifying', work_computed=ev['committed'])
        try:
            verification = verify.run(job['kind'], spec['inputs'], files, man, ev['summary'])
        except Exception as exc:
            verification = {'mode': 'error', 'passed': False, 'mismatches': [{'reason': 'verifier raised ' + type(exc).__name__}]}
        blob = container.pack({k: v for k, v in files.items()})
        with self.worker.db.tx() as db:
            fence = db.execute("SELECT 1 FROM jobs WHERE id=? AND state='running' AND lease_owner=? AND lease_generation=?", (job['id'], self.worker.worker_id, job['lease_generation'])).fetchone()
            if not fence:
                return 'fenced'
            crow = db.execute('SELECT owner_id, reviewer_id FROM contracts WHERE id=?', (job['contract_id'],)).fetchone()
            reviewer_pub = db.execute('SELECT value FROM meta WHERE key=?', ('age_public:' + str(crow['reviewer_id']),)).fetchone()
            aid = self.worker.store.store(db, workspace=job['workspace'], kind='compute_output', owner_id=crow['owner_id'], plaintext=blob, recipients=[reviewer_pub['value']] if reviewer_pub else [],
                                          intended_use='compute-output;owner-and-designated-reviewer', job_id=job['id'], contract_id=job['contract_id'], limit_bytes=self.limits['compute_max_artifact_bytes'])
            prev = db.execute('SELECT COALESCE(MAX(committed_units),0) FROM compute_checkpoints WHERE job_id=?', (job['id'],)).fetchone()[0]
            if ev['committed'] > prev:
                db.execute('INSERT OR IGNORE INTO compute_work_units (job_id, unit_from, unit_to, generation, attempt_generation, committed_at) VALUES (?,?,?,?,?,?)',
                           (job['id'], prev, ev['committed'], ev.get('generation', 0) + 1, job['lease_generation'], now()))
            db.execute('UPDATE compute_runs SET work_committed=?, work_computed=?, output_artifact_id=?, verification_json=?, phase=?, updated_at=? WHERE job_id=?',
                       (ev['committed'], ev['committed'], aid, json.dumps(verification), 'completed' if verification['passed'] else 'verification_failed', now(), job['id']))
            history.record(db, job['workspace'], self.worker.worker_id, 'compute.verified', 'job', job['id'], {'mode': verification.get('mode'), 'passed': verification['passed'], 'checked': verification.get('checked')})
        if not verification['passed']:
            return self.worker._finish(job, None, 'VERIFICATION_FAILED')
        summary = exactable(dict(ev['summary'], verification={k: verification[k] for k in ('mode', 'passed', 'statement') if k in verification}, backend=backend,
                       float_encoding='floats are shortest-repr decimal strings in evidence and summaries',
                       work_units_committed=ev['committed'], output_artifact_id=aid, output_files=sorted(files), manifest_id=man['manifest_id'],
                       implementation_digest=man['implementation_digest'], energy_delta_mJ_device_wide=ev.get('energy_delta_mJ_device_wide'), peak_device_bytes=ev.get('peak_device_bytes')))
        evidence = {'contract_digest': spec['contract_digest'], 'input_root': spec['input_root'], 'verifier_id': man['manifest_id'] + '-verifier', 'verifier_digest': man['implementation_digest'],
                    'result_schema': man['result_schema'], 'model_id': man['model_id'], 'result': summary, 'output_commitments': {n: hashlib.sha256(files[n]).hexdigest() for n in files},
                    'scope': 'service-compute-execution'}
        _, vault = merkle.commit(evidence)
        return self.worker._finish(job, {'evidence_vault': vault, 'outcome': 'VERIFIED', 'summary': summary}, None)
