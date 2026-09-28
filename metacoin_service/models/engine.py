"""Host side of the model runtime: task-owned runtime children per loaded revision, a memory policy that is actually
enforced (application level, from /proc/meminfo on this unified-memory host), bounded request execution with
durable output segments, cancellation, lease-fenced completion and unload/drain.

Two hosts exist: every worker owns one (executes generation/embedding jobs) and the API process owns one for the
synchronous query embeddings that retrieval needs. Both record their state in model_runtimes so readiness is a
fact per host, not a guess."""
import hashlib
import json
import os
import secrets
import select
import subprocess
import threading
import time
from pathlib import Path

from experiments.private_receipts import receipt as merkle
from .. import history
from ..compute.engine import compute_interpreter, exactable
from ..db import now
from ..errors import ServiceError
from . import registry as registry_mod

ROOT = Path(__file__).resolve().parents[2]
GENERATION_SCHEMA, EMBEDDING_SCHEMA = 'text-generation-input/v1', 'text-embedding-input/v1'
KINDS = ('text_generation', 'text_embedding')
KNOWLEDGE_KINDS = ('knowledge_index', 'knowledge_answer')
OPERATION_OF = {'text_generation': 'generate', 'text_embedding': 'embed', 'knowledge_index': 'embed', 'knowledge_answer': 'generate'}
MODEL_IDS = {'text_generation': 'local-text-generation/v1', 'text_embedding': 'local-text-embedding/v1', 'knowledge_index': 'private-knowledge-index/v1', 'knowledge_answer': 'retrieval-assisted-answer/v1'}
RESULT_SCHEMAS = {'text_generation': 'text-generation-result/v1', 'text_embedding': 'text-embedding-result/v1', 'knowledge_index': 'knowledge-index-result/v1', 'knowledge_answer': 'knowledge-answer-result/v1'}


def implementation_digest():
    h = hashlib.sha256()
    for name in ('runtime.py', 'engine.py', 'registry.py'):
        h.update((Path(__file__).parent / name).read_bytes())
    return h.hexdigest()


def mem_available_bytes():
    try:
        with open('/proc/meminfo') as f:
            for line in f:
                if line.startswith('MemAvailable:'):
                    return int(line.split()[1]) * 1024
    except OSError:
        pass
    return None


class Child:
    def __init__(self, revision_id, proc, log_path, log_file=None):
        self.revision_id, self.proc, self.log_path, self.log_file = revision_id, proc, log_path, log_file
        self.lock = threading.Lock()
        self.buf = b''
        self.ready = None
        self.last_used = time.time()
        self.requests = 0

    def send(self, cmd):
        try:
            self.proc.stdin.write((json.dumps(cmd, separators=(',', ':')) + '\n').encode()); self.proc.stdin.flush()
        except (BrokenPipeError, OSError):
            raise ServiceError('CAPABILITY_UNAVAILABLE', 'model runtime child is gone')

    def events(self, timeout):
        """Yield events until `timeout` seconds pass without any line (yields None on idle ticks of 0.25 s)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            while b'\n' in self.buf:
                line, self.buf = self.buf.split(b'\n', 1)
                try:
                    yield json.loads(line)
                except ValueError:
                    continue
                deadline = time.time() + timeout
            r, _, _ = select.select([self.proc.stdout], [], [], 0.25)
            if not r:
                if self.proc.poll() is not None:
                    yield {'event': 'exited', 'code': self.proc.returncode}; return
                yield None; continue
            chunk = os.read(self.proc.stdout.fileno(), 1 << 16)
            if not chunk:
                yield {'event': 'exited', 'code': self.proc.poll()}; return
            self.buf += chunk
        yield {'event': 'timeout'}

    def alive(self):
        return self.proc.poll() is None

    def stop(self, grace=10):
        try:
            self.send({'op': 'unload'})
        except ServiceError:
            pass
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            self.proc.kill(); self.proc.wait(timeout=5)
        for f in (self.proc.stdout, self.log_file):
            try:
                f.close()
            except Exception:
                pass


class ModelHost:
    """Loads at most `model_max_loaded` revisions; refuses a load that would leave less than the configured headroom
    of available memory, unloading idle runtimes first (least recently used)."""

    def __init__(self, settings, database, host_name):
        self.settings, self.db, self.host = settings, database, host_name
        self.limits = settings.limits
        self.runtime = compute_interpreter(settings)
        self.children = {}
        self.lock = threading.RLock()
        self.log_dir = settings.home / 'models'

    def available(self):
        return bool(self.runtime and self.runtime.get('torch'))

    def device_for(self, row):
        return 'cuda' if self.runtime and self.runtime.get('cuda') and row['loader'] == 'causal_lm' else ('cuda' if self.runtime and self.runtime.get('cuda') and self.limits.get('model_embed_on_cuda') else 'cpu')

    def _record(self, revision_id, **cols):
        cols['updated_at'] = now()
        with self.db.tx() as db:
            db.execute('INSERT INTO model_runtimes (host, revision_id, state, updated_at) VALUES (?,?,?,?) ON CONFLICT(host, revision_id) DO NOTHING', (self.host, revision_id, 'unloaded', now()))
            db.execute('UPDATE model_runtimes SET ' + ', '.join(k + '=?' for k in cols) + ' WHERE host=? AND revision_id=?', (*cols.values(), self.host, revision_id))

    def ensure(self, row, wait_seconds=None):
        """Return a ready child for the revision row, loading it if needed. Raises ServiceError with a precise reason."""
        if not self.available():
            raise ServiceError('CAPABILITY_UNAVAILABLE', {'code': 'no_model_runtime', 'note': 'the compute interpreter has no torch'})
        with self.lock:
            child = self.children.get(row['id'])
            if child and child.alive():
                child.last_used = time.time()
                return child
            if child:
                self.children.pop(row['id'], None)
            self._make_room(row)
            return self._load(row, wait_seconds or self.limits['model_load_timeout_seconds'])

    def _make_room(self, row):
        estimate = row['resource_estimate_bytes'] or 0
        headroom = self.limits['model_memory_headroom_bytes']
        budget = self.limits['model_memory_budget_bytes']
        def loaded_bytes():
            return sum((c.ready or {}).get('estimated_bytes', 0) for c in self.children.values() if c.alive())
        while (len(self.children) >= self.limits['model_max_loaded'] or loaded_bytes() + estimate > budget
               or (mem_available_bytes() is not None and mem_available_bytes() - estimate < headroom)) and self.children:
            victim = min(self.children.values(), key=lambda c: c.last_used)
            if victim.lock.locked():
                break                                   # busy runtime: never killed under a request
            self.unload(victim.revision_id, reason='memory policy: make room for ' + row['id'])
        avail = mem_available_bytes()
        if avail is not None and avail - estimate < headroom:
            raise ServiceError('CAPABILITY_UNAVAILABLE', {'code': 'model_memory_unavailable', 'available_bytes': avail, 'estimate_bytes': estimate, 'headroom_bytes': headroom,
                                                          'enforced': 'application policy from /proc/meminfo MemAvailable; not a hardware limit'})
        if loaded_bytes() + estimate > budget:
            raise ServiceError('CAPABILITY_UNAVAILABLE', {'code': 'model_memory_budget', 'loaded_bytes': loaded_bytes(), 'estimate_bytes': estimate, 'budget_bytes': budget})

    def _load(self, row, wait_seconds):
        self.log_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        workdir = self.log_dir / ('runtime-' + row['id'] + '-' + secrets.token_hex(3))
        workdir.mkdir(mode=0o700)
        device = self.device_for(row)
        insp = json.loads(row['install_json'] or '{}')
        spec = {'revision_id': row['id'], 'local_dir': row['local_dir'], 'loader': row['loader'], 'device': device, 'precision': row['precision'], 'pooling': row['pooling'],
                'normalize': True, 'max_seq_length': insp.get('max_seq_length') or (min(row['context_limit'] or 512, 512) if row['loader'] == 'encoder' else None),
                'limits': {'max_input_tokens': min(self.limits['model_max_input_tokens'], row['context_limit'] or self.limits['model_max_input_tokens']),
                           'max_output_tokens': self.limits['model_max_output_tokens'], 'max_items': self.limits['model_max_embed_items'], 'threads': self.limits['compute_threads'],
                           'token_timeout_seconds': self.limits['model_token_timeout_seconds'], 'batch_size': 32}}
        (workdir / 'spec.json').write_bytes(json.dumps(spec).encode())
        env = {'PATH': os.environ.get('PATH', ''), 'PYTHONPATH': str(ROOT), 'HOME': os.environ.get('HOME', '/'), 'PYTHONUNBUFFERED': '1', 'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1',
               'HF_HUB_DISABLE_TELEMETRY': '1', 'TOKENIZERS_PARALLELISM': 'false', 'no_proxy': '*', 'NO_PROXY': '*'}
        log_path = workdir / 'stderr.log'
        log_file = open(log_path, 'wb')
        t0 = time.time()
        proc = subprocess.Popen([self.runtime['python'], '-m', 'metacoin_service.models.runtime', str(workdir)], cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log_file, env=env)
        child = Child(row['id'], proc, log_path, log_file)
        self._record(row['id'], state='loading', pid=proc.pid, device=device, dtype=None, desired='loaded', error=None, loaded_at=None)
        for ev in child.events(wait_seconds):
            if ev is None:
                continue
            if ev.get('event') == 'ready':
                child.ready = {'estimated_bytes': ev['versions']['param_bytes'], 'versions': ev['versions'], 'load_ms': ev['load_ms'], 'memory': ev.get('memory')}
                self.children[row['id']] = child
                self._record(row['id'], state='ready', dtype=ev['versions']['dtype'], estimated_bytes=ev['versions']['param_bytes'], versions_json=json.dumps(ev['versions']),
                             loaded_at=now(), load_ms=ev['load_ms'], error=None)
                return child
            if ev.get('event') in ('error', 'exited', 'timeout'):
                reason = ev.get('reason') or ev.get('event')
                child.stop(grace=2)
                self._record(row['id'], state='failed', error=str(reason)[:300], pid=None)
                raise ServiceError('CAPABILITY_UNAVAILABLE', {'code': 'model_load_failed', 'revision_id': row['id'], 'reason': str(reason)[:200], 'load_seconds': round(time.time() - t0, 1)})
        raise ServiceError('CAPABILITY_UNAVAILABLE', {'code': 'model_load_timeout', 'revision_id': row['id']})

    def unload(self, revision_id, reason=''):
        with self.lock:
            child = self.children.pop(revision_id, None)
        if child:
            child.stop()
        self._record(revision_id, state='unloaded', pid=None, error=None, desired='unloaded' if 'operator' in reason else None)
        return child is not None

    def drain_all(self, reason='drain'):
        for rid in list(self.children):
            self.unload(rid, reason)

    def apply_desired(self, registry):
        """Operator load/unload requests recorded for this host (polled by the worker loop)."""
        with self.db.read() as db:
            rows = db.execute("SELECT r.*, m.* FROM model_runtimes r JOIN model_revisions m ON m.id=r.revision_id WHERE r.host=? AND ((r.desired='loaded' AND r.state IN ('unloaded','failed')) OR (r.desired='unloaded' AND r.state='ready'))", (self.host,)).fetchall()
        acted = 0
        for r in rows:
            try:
                if r['desired'] == 'loaded' and r['status'] == 'registered' and r['installed']:
                    self.ensure(r); acted += 1
                elif r['desired'] == 'unloaded':
                    child = self.children.get(r['revision_id'])
                    if child is None or not child.lock.locked():
                        self.unload(r['revision_id'], 'operator unload'); acted += 1
            except ServiceError:
                continue
        return acted

    def idle_cleanup(self):
        """Unload runtimes idle longer than model_idle_unload_seconds (0 disables)."""
        idle = self.limits.get('model_idle_unload_seconds') or 0
        if not idle:
            return 0
        n = 0
        for rid, c in list(self.children.items()):
            if not c.lock.locked() and time.time() - c.last_used > idle:
                self.unload(rid, 'idle'); n += 1
        return n

    # ---- requests --------------------------------------------------------------------------------
    def generate(self, row, request, on_segment=None, should_cancel=None, timeout=None):
        """Run one bounded generation. on_segment(seq, text) is called per streamed piece; should_cancel() polled."""
        child = self.ensure(row)
        rid = 'r' + secrets.token_hex(6)
        with child.lock:
            child.requests += 1; child.last_used = time.time()
            child.send(dict(request, op='generate', request_id=rid))
            last_poll = time.time()
            for ev in child.events(timeout or self.limits['model_request_timeout_seconds']):
                if ev is None or ev.get('request_id') not in (None, rid):
                    if should_cancel and time.time() - last_poll > 0.5:
                        last_poll = time.time()
                        if should_cancel():
                            child.send({'op': 'cancel', 'request_id': rid})
                    continue
                et = ev.get('event')
                if et == 'segment':
                    if on_segment:
                        on_segment(ev['seq'], ev['text'])
                    if should_cancel and time.time() - last_poll > 0.5:
                        last_poll = time.time()
                        if should_cancel():
                            child.send({'op': 'cancel', 'request_id': rid})
                elif et == 'done':
                    child.last_used = time.time()
                    return ev
                elif et == 'error':
                    raise ServiceError('COMPUTATION' if ev.get('code') == 'RUNTIME_ERROR' else 'VALIDATION', {'code': ev.get('code'), 'reason': ev.get('reason')})
                elif et in ('exited', 'timeout'):
                    self.children.pop(row['id'], None); self._record(row['id'], state='failed', error='runtime ' + et, pid=None)
                    raise ServiceError('CAPABILITY_UNAVAILABLE', {'code': 'model_runtime_' + et})
        raise ServiceError('CAPABILITY_UNAVAILABLE', 'model runtime produced no completion')

    def embed(self, row, texts, truncate=False, timeout=None):
        child = self.ensure(row)
        rid = 'r' + secrets.token_hex(6)
        with child.lock:
            child.requests += 1; child.last_used = time.time()
            child.send({'op': 'embed', 'request_id': rid, 'texts': texts, 'truncate': truncate})
            for ev in child.events(timeout or self.limits['model_request_timeout_seconds']):
                if ev is None or ev.get('request_id') not in (None, rid):
                    continue
                et = ev.get('event')
                if et == 'embedding':
                    child.last_used = time.time()
                    return ev
                if et == 'error':
                    raise ServiceError('VALIDATION', {'code': ev.get('code'), 'reason': ev.get('reason')})
                if et in ('exited', 'timeout'):
                    self.children.pop(row['id'], None); self._record(row['id'], state='failed', error='runtime ' + et, pid=None)
                    raise ServiceError('CAPABILITY_UNAVAILABLE', {'code': 'model_runtime_' + et})
        raise ServiceError('CAPABILITY_UNAVAILABLE', 'model runtime produced no embedding')

    def status(self):
        return {'host': self.host, 'runtime_available': self.available(), 'interpreter': (self.runtime or {}).get('python'), 'cuda': bool((self.runtime or {}).get('cuda')),
                'loaded': [{'revision_id': rid, 'pid': c.proc.pid, 'busy': c.lock.locked(), 'requests': c.requests, 'idle_seconds': round(time.time() - c.last_used, 1), 'estimated_bytes': (c.ready or {}).get('estimated_bytes')} for rid, c in self.children.items() if c.alive()],
                'policy': {'max_loaded': self.limits['model_max_loaded'], 'memory_budget_bytes': self.limits['model_memory_budget_bytes'], 'headroom_bytes': self.limits['model_memory_headroom_bytes'],
                           'mem_available_bytes': mem_available_bytes(), 'enforced': 'application-level (load refused / idle runtime unloaded); no hardware memory limit on unified memory'}}


class SegmentSink:
    """Persists streamed output pieces as durable, fenced segments (coalesced to at most ~4 rows per second)."""

    def __init__(self, engine, job, state):
        self.engine, self.job, self.state = engine, job, state
        self.seq, self.buf, self.chars, self.last_flush = 0, '', 0, time.time()

    def __call__(self, seq, text):
        self.buf += text; self.chars += len(text)
        self.flush()

    def flush(self, force=False):
        if not self.buf or (not force and time.time() - self.last_flush < 0.25):
            return
        with self.engine.worker.db.tx() as db:
            if self.engine._fenced(db, self.job):
                self.state['fenced'] = True; return
            db.execute('INSERT OR IGNORE INTO model_segments (job_id, attempt_generation, seq, text, chars, created_at) VALUES (?,?,?,?,?,?)',
                       (self.job['id'], self.job['lease_generation'], self.seq, self.buf, len(self.buf), now()))
            db.execute('UPDATE model_requests SET segments=?, output_chars=?, updated_at=? WHERE job_id=?', (self.seq + 1, self.chars, now(), self.job['id']))
        self.seq += 1; self.buf = ''; self.last_flush = time.time()

    def text(self):
        with self.engine.worker.db.read() as db:
            segs = db.execute('SELECT text FROM model_segments WHERE job_id=? AND attempt_generation=? ORDER BY seq', (self.job['id'], self.job['lease_generation'])).fetchall()
        return ''.join(s['text'] for s in segs), len(segs)


class ModelEngine:
    """Worker-side execution of text_generation / text_embedding jobs through the ordinary job queue and finish path."""

    def __init__(self, worker):
        self.worker = worker
        self.settings = worker.settings
        self.limits = worker.settings.limits
        self.host = ModelHost(worker.settings, worker.db, 'worker:' + worker.name)
        self.registry = registry_mod.ModelRegistry(worker.settings)

    def available(self):
        return self.host.available()

    def _update(self, job, **cols):
        cols['updated_at'] = now()
        with self.worker.db.tx() as db:
            db.execute('UPDATE model_requests SET ' + ', '.join(k + '=?' for k in cols) + ' WHERE job_id=?', (*cols.values(), job['id']))

    def _fenced(self, db, job):
        return db.execute("SELECT 1 FROM jobs WHERE id=? AND state='running' AND lease_owner=? AND lease_generation=?", (job['id'], self.worker.worker_id, job['lease_generation'])).fetchone() is None

    def run(self, job):
        with self.worker.db.read() as db:
            contract, spec = self.worker._spec(db, job)
            req = dict(db.execute('SELECT * FROM model_requests WHERE job_id=?', (job['id'],)).fetchone())
            row = self.registry.row(db, req['revision_id'])
        if row['status'] == 'revoked':
            self._update(job, phase='failed', error='model revision revoked: ' + str(row['revocation_reason']))
            return self.worker._finish(job, None, 'MODEL_REVOKED')
        if not row['installed']:
            self._update(job, phase='failed', error='model not installed on this host')
            return self.worker._finish(job, None, 'MODEL_UNAVAILABLE')
        digest = implementation_digest()
        params = json.loads(contract['params_json'] or '{}')
        if params.get('implementation_digest') != digest:
            self._update(job, phase='failed', error='runtime implementation differs from the accepted one')
            return self.worker._finish(job, None, 'MANIFEST_MISMATCH')
        t_queue = now() - job['created_at']
        self._update(job, phase='loading', host=self.host.host, attempt_generation=job['lease_generation'], started_at=now(), queue_seconds=t_queue)
        should_cancel, state = self.poller(job)
        t0 = time.time()
        try:
            child = self.host.ensure(row)
        except ServiceError as exc:
            self._update(job, phase='failed', error=json.dumps(exc.body())[:300])
            return self.worker._finish(job, None, 'COMPUTATION_ERROR' if 'memory' in json.dumps(exc.body()) else 'MODEL_UNAVAILABLE')
        load_ms = int((time.time() - t0) * 1000) if child.ready and child.requests == 0 else 0
        self._update(job, phase='running', load_ms=load_ms, versions_json=json.dumps(child.ready['versions']) if child.ready else None)
        inputs = spec['inputs']
        try:
            if job['kind'] == 'text_generation':
                return self._generate(job, contract, spec, row, child, inputs, should_cancel, state)
            return self._embed(job, contract, spec, row, inputs, should_cancel, state)
        except ServiceError as exc:
            body = exc.body()
            self._update(job, phase='failed', error=json.dumps(body)[:300])
            code = 'COMPUTATION_ERROR' if exc.code in ('COMPUTATION', 'CAPABILITY_UNAVAILABLE') else 'INPUT_INVALID'
            return self.worker._finish(job, None, code)

    def poller(self, job):
        """Returns (should_cancel, state): renews the lease periodically and reports cancel requests; sets state['fenced']."""
        last = {'lease': time.time()}
        state = {'fenced': False}

        def should_cancel():
            if time.time() - last['lease'] >= self.limits['compute_lease_renew_seconds']:
                with self.worker.db.tx() as db:
                    ok = db.execute("UPDATE jobs SET lease_expires=?, updated_at=? WHERE id=? AND state='running' AND lease_owner=? AND lease_generation=?",
                                    (now() + self.limits['job_lease_seconds'], now(), job['id'], self.worker.worker_id, job['lease_generation'])).rowcount == 1
                last['lease'] = time.time()
                if not ok:
                    state['fenced'] = True; return True
            with self.worker.db.read() as db:
                return bool(db.execute('SELECT cancel_requested FROM jobs WHERE id=?', (job['id'],)).fetchone()[0])
        return should_cancel, state

    def _generate(self, job, contract, spec, row, child, inputs, should_cancel, state):
        sink = SegmentSink(self, job, state)
        request = {'messages': inputs.get('messages'), 'prompt': inputs.get('prompt'), 'max_new_tokens': inputs['max_output_tokens'], 'temperature_percent': inputs.get('temperature_percent', 0),
                   'top_p_percent': inputs.get('top_p_percent', 100), 'seed': inputs.get('seed'), 'stop': inputs.get('stop') or []}
        done = self.host.generate(row, request, on_segment=sink, should_cancel=should_cancel)
        sink.flush(force=True)
        if state['fenced']:
            return 'fenced'
        text, nsegs = sink.text()
        if hashlib.sha256(text.encode()).hexdigest() != done['text_sha256']:
            self._update(job, phase='failed', error='persisted segments do not reproduce the runtime output')
            return self.worker._finish(job, None, 'COMPUTATION_ERROR')
        usage = done['usage']
        self._update(job, input_tokens=usage['input_tokens'], output_tokens=usage['output_tokens'], finish_reason=done['finish_reason'], inference_ms=done['ms'], usage_json=json.dumps(usage))
        if done['finish_reason'] == 'cancelled':
            self._update(job, phase='cancelled')
            history.record_safe(self.worker.db, job['workspace'], self.worker.worker_id, 'model.request', 'job', job['id'], {'finish_reason': 'cancelled', 'output_tokens': usage['output_tokens'], 'partial_output_preserved': True})
            return self.worker._finish(job, None, 'CANCELLED')
        output = {'schema': RESULT_SCHEMAS['text_generation'], 'text': text, 'finish_reason': done['finish_reason'], 'usage': usage, 'config': done['config'], 'segments': nsegs,
                  'model_revision_id': row['id'], 'model_id': row['model_id'], 'revision': row['revision'], 'weight_digest': row['weight_digest'], 'tokenizer_digest': row['tokenizer_digest'],
                  'versions': child.ready['versions'] if child.ready else None, 'request': {k: inputs.get(k) for k in ('messages', 'prompt', 'max_output_tokens', 'temperature_percent', 'top_p_percent', 'seed', 'stop')}}
        return self._complete(job, contract, spec, row, output, 'GENERATED', {'output_tokens': usage['output_tokens'], 'input_tokens': usage['input_tokens'], 'finish_reason': done['finish_reason'],
                                                                             'segments': nsegs, 'inference_ms': done['ms'], 'tokens_per_second': done.get('tokens_per_second'), 'text_sha256': done['text_sha256'],
                                                                             'output_chars': len(text)})

    def _embed(self, job, contract, spec, row, inputs, should_cancel, state):
        if should_cancel():
            return 'fenced' if state['fenced'] else self.worker._finish(job, None, 'CANCELLED')
        ev = self.host.embed(row, inputs['texts'], truncate=bool(inputs.get('truncate')))
        from ..compute import npy
        flat = [x for v in ev['vectors'] for x in v]
        blob = npy.encode(flat, '<f8', (len(ev['vectors']), ev['dim']))
        meta = {'schema': RESULT_SCHEMAS['text_embedding'], 'items': len(ev['vectors']), 'dim': ev['dim'], 'pooling': ev['pooling'], 'normalized': ev['normalized'], 'max_seq_length': ev['max_seq_length'],
                'tokens': ev['tokens'], 'tokens_before_truncation': ev['tokens_before_truncation'], 'truncated': ev['truncated'], 'model_revision_id': row['id'], 'model_id': row['model_id'], 'revision': row['revision'],
                'weight_digest': row['weight_digest'], 'tokenizer_digest': row['tokenizer_digest'], 'dtype': '<f8 (float32 model output widened; not extra precision)', 'vector_sha256': hashlib.sha256(blob).hexdigest(),
                'similarity_policy': 'cosine = dot product of the L2-normalized vectors; comparable only within the same revision/pooling/normalization', 'inference_ms': ev['ms']}
        self._update(job, items=len(ev['vectors']), inference_ms=ev['ms'], usage_json=json.dumps({'items': len(ev['vectors']), 'tokens': sum(ev['tokens'])}), input_tokens=sum(ev['tokens']))
        return self._complete(job, contract, spec, row, meta, 'EMBEDDED', {'items': len(ev['vectors']), 'dim': ev['dim'], 'tokens': sum(ev['tokens']), 'truncated_items': sum(1 for t in ev['truncated'] if t), 'inference_ms': ev['ms']},
                              extra_files={'vectors.npy': blob})

    def _complete(self, job, contract, spec, row, output, outcome, summary_extra, extra_files=None, on_commit=None, scope='local-model-inference'):
        from ..compute import container
        files = {'output.json': merkle.canonical(exactable(output))}
        files.update(extra_files or {})
        blob = container.pack(files)
        with self.worker.db.tx() as db:
            if self._fenced(db, job):
                return 'fenced'
            if on_commit:
                on_commit(db)
            crow = db.execute('SELECT owner_id, reviewer_id FROM contracts WHERE id=?', (job['contract_id'],)).fetchone()
            reviewer_pub = db.execute('SELECT value FROM meta WHERE key=?', ('age_public:' + str(crow['reviewer_id']),)).fetchone()
            aid = self.worker.store.store(db, workspace=job['workspace'], kind='model_output', owner_id=crow['owner_id'], plaintext=blob, recipients=[reviewer_pub['value']] if reviewer_pub else [],
                                          intended_use='model-output;owner-and-designated-reviewer', job_id=job['id'], contract_id=job['contract_id'], limit_bytes=self.limits['compute_max_artifact_bytes'])
            db.execute('UPDATE model_requests SET phase=?, output_artifact_id=?, updated_at=? WHERE job_id=?', ('completed', aid, now(), job['id']))
            history.record(db, job['workspace'], self.worker.worker_id, 'model.request', 'job', job['id'], exactable(dict(summary_extra, revision_id=row['id'], outcome=outcome)))
        summary = exactable(dict(summary_extra, model_revision_id=row['id'], model_id=row['model_id'], revision=row['revision'], weight_digest=row['weight_digest'], output_artifact_id=aid,
                                 output_files=sorted(files), implementation_digest=implementation_digest(), verification='none: model output is generated text/vectors under the recorded configuration; it is not scientifically verified',
                                 float_encoding='floats are shortest-repr decimal strings in evidence and summaries'))
        evidence = {'contract_digest': spec['contract_digest'], 'input_root': spec['input_root'], 'verifier_id': 'model-runtime/v1', 'verifier_digest': implementation_digest(),
                    'result_schema': RESULT_SCHEMAS[job['kind']], 'model_id': MODEL_IDS[job['kind']], 'result': summary, 'output_commitments': {n: hashlib.sha256(files[n]).hexdigest() for n in files},
                    'scope': scope}
        _, vault = merkle.commit(evidence)
        return self.worker._finish(job, {'evidence_vault': vault, 'outcome': outcome, 'summary': summary}, None)
