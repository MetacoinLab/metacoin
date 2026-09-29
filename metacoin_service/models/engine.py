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
from .. import history, scheduling
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
        self.drain_after = None                         # set while busy when numerical compute needs the memory: released after the current request

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
        self.warm = set()                               # revisions the warmup policy keeps resident on this host (intent; actual state is in model_runtimes)
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
        """Unload runtimes idle longer than model_idle_unload_seconds (0 disables); warm (policy-resident) runtimes are exempt."""
        idle = self.limits.get('model_idle_unload_seconds') or 0
        if not idle:
            return 0
        n = 0
        for rid, c in list(self.children.items()):
            if rid not in self.warm and not c.lock.locked() and time.time() - c.last_used > idle:
                self.unload(rid, 'idle'); n += 1
        return n

    # ---- §65-5 warmup policy and drain ----------------------------------------------------------------
    def warmup_policy(self):
        with self.db.read() as db:
            row = db.execute("SELECT value FROM meta WHERE key='model_warmup'").fetchone()
        return json.loads(row['value']) if row else None

    def apply_warmup(self, registry):
        """Pre-load the explicitly listed revisions, in order, while their estimated bytes stay under the operator ceiling;
        release runtimes the policy no longer lists (idle ones only). Actual state is recorded per host, never inferred."""
        policy = self.warmup_policy() or {}
        wanted = list(policy.get('revision_ids') or []) if policy.get('enabled') else []
        ceiling = int(policy.get('ceiling_bytes') or 0)
        acted = {'loaded': [], 'refused': [], 'released': []}
        for rid in [r for r in self.warm if r not in wanted]:
            child = self.children.get(rid)
            if child is None or not child.lock.locked():
                if child:
                    self.unload(rid, 'warmup policy no longer lists this revision')
                self.warm.discard(rid); self._record(rid, warm=0); acted['released'].append(rid)
        for rid in wanted:
            with self.db.read() as db:
                row = db.execute('SELECT * FROM model_revisions WHERE id=?', (rid,)).fetchone()
            if row is None or row['status'] != 'registered' or not row['installed']:
                acted['refused'].append({'revision_id': rid, 'reason': 'not an installed registered revision'}); continue
            child = self.children.get(rid)
            if child and child.alive():
                if rid not in self.warm:
                    self.warm.add(rid); self._record(rid, warm=1)
                continue
            warm_bytes = sum((c.ready or {}).get('estimated_bytes', 0) for r2, c in self.children.items() if c.alive() and r2 in self.warm)
            est = row['resource_estimate_bytes'] or 0
            if warm_bytes + est > ceiling:
                reason = 'warmup refused: ceiling %d bytes (warm %d + estimate %d)' % (ceiling, warm_bytes, est)
                self._record(rid, warm=0, error=reason); self.warm.discard(rid); acted['refused'].append({'revision_id': rid, 'reason': reason}); continue
            try:
                self.ensure(row); self.warm.add(rid); self._record(rid, warm=1, error=None); acted['loaded'].append(rid)
            except ServiceError as exc:
                self.warm.discard(rid); acted['refused'].append({'revision_id': rid, 'reason': json.dumps(exc.body())[:200]})
        return acted

    def drain_for_compute(self, min_available_bytes, reason):
        """Release resident runtimes (least recently used first) until MemAvailable reaches the requested floor; a busy
        runtime is never killed under a request: it is flagged and released when its current request ends."""
        drained, flagged = [], []
        while self.children:
            avail = mem_available_bytes()
            if avail is None or avail >= min_available_bytes:
                break
            idle = [c for c in self.children.values() if c.alive() and not c.lock.locked()]
            if not idle:
                for c in self.children.values():
                    c.drain_after = reason; flagged.append(c.revision_id)
                break
            victim = min(idle, key=lambda c: c.last_used)
            self.unload(victim.revision_id, 'drained: ' + reason); drained.append(victim.revision_id)
            self._record(victim.revision_id, drain_reason=reason, drained_at=now())
        return {'drained': drained, 'flagged_busy': flagged}

    def _after_request(self, child):
        if child.drain_after and not child.lock.locked():
            reason = child.drain_after; child.drain_after = None
            self.unload(child.revision_id, 'drained: ' + reason); self._record(child.revision_id, drain_reason=reason, drained_at=now())

    # ---- requests --------------------------------------------------------------------------------
    def generate(self, row, request, on_segment=None, should_cancel=None, timeout=None):
        """Run one bounded generation. on_segment(seq, text) is called per streamed piece; should_cancel() polled."""
        child = self.ensure(row)
        try:
            return self._generate(child, row, request, on_segment, should_cancel, timeout)
        finally:
            self._after_request(child)

    def _generate(self, child, row, request, on_segment, should_cancel, timeout):
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

    def generate_batch(self, row, requests, on_segment=None, cancel_check=None, timeout=None):
        """One static batch. on_segment(request_id, seq, text); cancel_check() -> set of request ids to cancel (polled).
        Returns {'results': {request_id: done_event | {'error': ...}}, 'batch': batch_done_event}."""
        child = self.ensure(row)
        try:
            return self._generate_batch(child, row, requests, on_segment, cancel_check, timeout)
        finally:
            self._after_request(child)

    def _generate_batch(self, child, row, requests, on_segment, cancel_check, timeout):
        bid = 'mb_' + secrets.token_hex(6)
        results, cancelled = {}, set()
        with child.lock:
            child.requests += len(requests); child.last_used = time.time()
            child.send({'op': 'generate_batch', 'batch_id': bid, 'requests': requests})
            last_poll = time.time()
            for ev in child.events(timeout or self.limits['model_request_timeout_seconds']):
                if cancel_check and time.time() - last_poll > 0.5:
                    last_poll = time.time()
                    for rid in (cancel_check() or set()) - cancelled:
                        child.send({'op': 'cancel', 'request_id': rid}); cancelled.add(rid)
                if ev is None:
                    continue
                et = ev.get('event')
                if et == 'segment':
                    if on_segment:
                        on_segment(ev['request_id'], ev['seq'], ev['text'])
                elif et == 'done':
                    results[ev['request_id']] = ev
                elif et == 'error' and ev.get('request_id'):
                    results[ev['request_id']] = {'error': {'code': ev.get('code'), 'reason': ev.get('reason')}}
                elif et == 'batch_done':
                    child.last_used = time.time()
                    return {'results': results, 'batch': dict(ev, id=bid)}
                elif et in ('exited', 'timeout'):
                    self.children.pop(row['id'], None); self._record(row['id'], state='failed', error='runtime ' + et, pid=None)
                    raise ServiceError('CAPABILITY_UNAVAILABLE', {'code': 'model_runtime_' + et})
        raise ServiceError('CAPABILITY_UNAVAILABLE', 'model runtime produced no batch completion')

    def generate_continuous(self, row, initial, admit, on_segment=None, cancel_check=None, idle_seconds=1.0, timeout=None):
        """A continuous-batching session on the child: `initial` requests are admitted first; `admit()` is polled while the
        session runs and returns further requests to admit; `cancel_check()` returns request ids to cancel. The session ends
        when every admitted request finished and `admit()` returned nothing for `idle_seconds`. Returns per-request results,
        admission records and the session summary. Raises CAPABILITY_UNAVAILABLE when the runtime cannot run the session."""
        child = self.ensure(row)
        try:
            return self._generate_continuous(child, row, initial, admit, on_segment, cancel_check, idle_seconds, timeout)
        finally:
            self._after_request(child)

    def _generate_continuous(self, child, row, initial, admit, on_segment, cancel_check, idle_seconds, timeout):
        sid = 'mb_' + secrets.token_hex(6)
        results, cancelled, admissions = {}, set(), []
        with child.lock:
            child.requests += len(initial); child.last_used = time.time()
            child.send({'op': 'generate_continuous', 'session_id': sid})
            for r in initial:
                child.send(dict(r, op='cb_add'))
            outstanding = {r['request_id'] for r in initial}
            last_poll = time.time(); last_new = time.time(); ended = False
            for ev in child.events(timeout or self.limits['model_request_timeout_seconds']):
                if time.time() - last_poll > 0.25:
                    last_poll = time.time()
                    if cancel_check:
                        for rid in (cancel_check() or set()) - cancelled:
                            child.send({'op': 'cancel', 'request_id': rid}); cancelled.add(rid)
                    if not ended and admit:
                        for r in (admit() or []):
                            child.send(dict(r, op='cb_add')); outstanding.add(r['request_id']); child.requests += 1; last_new = time.time()
                    if not ended and not outstanding and time.time() - last_new >= idle_seconds:
                        child.send({'op': 'cb_end'}); ended = True
                if ev is None:
                    continue
                et = ev.get('event')
                if et == 'segment':
                    if on_segment:
                        on_segment(ev['request_id'], ev['seq'], ev['text'])
                elif et == 'admitted':
                    admissions.append({'request_id': ev['request_id'], 'position': ev['position'], 'at': now(), 'input_tokens': ev.get('input_tokens')})
                elif et == 'done':
                    results[ev['request_id']] = ev; outstanding.discard(ev['request_id']); last_new = time.time()
                elif et == 'error' and ev.get('request_id'):
                    results[ev['request_id']] = {'error': {'code': ev.get('code'), 'reason': ev.get('reason')}}; outstanding.discard(ev['request_id']); last_new = time.time()
                elif et == 'session_done':
                    child.last_used = time.time()
                    if ev.get('error'):
                        raise ServiceError('CAPABILITY_UNAVAILABLE', {'code': ev['error'].get('code', 'continuous_failed'), 'reason': ev['error'].get('reason')})
                    return {'results': results, 'admissions': admissions, 'batch': dict(ev, id=sid)}
                elif et in ('exited', 'timeout'):
                    self.children.pop(row['id'], None); self._record(row['id'], state='failed', error='runtime ' + et, pid=None)
                    raise ServiceError('CAPABILITY_UNAVAILABLE', {'code': 'model_runtime_' + et})
        raise ServiceError('CAPABILITY_UNAVAILABLE', 'model runtime produced no session completion')

    def embed(self, row, texts, truncate=False, timeout=None):
        child = self.ensure(row)
        try:
            return self._embed(child, row, texts, truncate, timeout)
        finally:
            self._after_request(child)

    def _embed(self, child, row, texts, truncate, timeout):
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
                'loaded': [{'revision_id': rid, 'pid': c.proc.pid, 'busy': c.lock.locked(), 'requests': c.requests, 'idle_seconds': round(time.time() - c.last_used, 1), 'estimated_bytes': (c.ready or {}).get('estimated_bytes'), 'warm': rid in self.warm, 'drain_pending': bool(c.drain_after)} for rid, c in self.children.items() if c.alive()],
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
                pol = self.batching_policy()
                if pol['enabled'] and 'continuous' in pol['modes'] and int(inputs.get('temperature_percent') or 0) == 0 and child.ready and (child.ready.get('versions') or {}).get('continuous_batching'):
                    res = self._generate_continuous(job, contract, spec, row, inputs, should_cancel, state, pol)
                    if res is not None:
                        return res                                                     # None: the session could not start; static fallback below
                members = self._claim_generation_companions(job, inputs, row)
                if members:
                    return self._generate_static_batch(job, contract, spec, row, inputs, should_cancel, state, members)
                return self._generate(job, contract, spec, row, child, inputs, should_cancel, state)
            return self._embed(job, contract, spec, row, inputs, should_cancel, state, members=self._claim_companions(job, inputs, row))
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

    # ---- §65-4 inference batching (embeddings): compatible queued requests of the same submitter share one forward pass --
    def _claim_companions(self, job, inputs, row):
        """Queued text_embedding jobs from the same workspace AND submitter, same revision and truncate flag, that fit under the
        batch item/char limits; each is claimed with the ordinary lease fencing. No waiting: only work already queued joins."""
        if not self.limits.get('model_batch_enabled', 1) or job['kind'] != 'text_embedding':
            return []
        max_items, max_chars = self.limits['model_batch_max_items'], self.limits['model_batch_max_chars']
        items, chars, members = len(inputs['texts']), sum(len(t) for t in inputs['texts']), []
        with self.worker.db.tx() as db:
            cands = db.execute("SELECT j.* FROM jobs j JOIN model_requests r ON r.job_id=j.id WHERE j.state='queued' AND j.cancel_requested=0 AND j.hold=0 AND j.kind='text_embedding' AND j.workspace=? AND j.submitted_by=? AND r.revision_id=? AND j.id!=? ORDER BY j.created_at LIMIT 16",
                               (job['workspace'], job['submitted_by'], row['id'], job['id'])).fetchall()
            for cand in cands:
                if not scheduling.local_allowed(cand['location_policy']):
                    continue
                contract, spec = self.worker._spec(db, cand)
                ci = spec['inputs']
                n, c = len(ci['texts']), sum(len(t) for t in ci['texts'])
                if bool(ci.get('truncate')) != bool(inputs.get('truncate')) or items + n > max_items or chars + c > max_chars:
                    continue
                generation = cand['lease_generation'] + 1
                changed = db.execute("UPDATE jobs SET state='running', lease_owner=?, lease_expires=?, lease_generation=?, attempt=attempt+1, updated_at=? WHERE id=? AND lease_generation=? AND state='queued'",
                                     (self.worker.worker_id, now() + self.worker.lease, generation, now(), cand['id'], cand['lease_generation'])).rowcount
                if changed != 1:
                    continue
                cj = dict(db.execute('SELECT * FROM jobs WHERE id=?', (cand['id'],)).fetchone())
                db.execute('INSERT INTO attempts VALUES (?,?,?,?,?,NULL,NULL)', ('at_' + secrets.token_hex(6), cj['id'], generation, self.worker.worker_id, now()))
                history.record(db, cj['workspace'], self.worker.worker_id, 'job.claimed', 'job', cj['id'], {'generation': generation, 'attempt': cj['attempt'], 'batched_with': job['id']})
                db.execute('UPDATE model_requests SET phase=?, host=?, attempt_generation=?, started_at=?, queue_seconds=?, updated_at=? WHERE job_id=?', ('running', self.host.host, generation, now(), now() - cj['created_at'], now(), cj['id']))
                members.append((cj, contract, spec)); items += n; chars += c
        return members

    # ---- §20-21 static generation batching: admission, per-member lifecycle -----------------------------------------
    def batching_policy(self):
        with self.worker.db.read() as db:
            row = db.execute("SELECT value FROM meta WHERE key='model_batching'").fetchone()
        pol = json.loads(row['value']) if row else {}
        L = self.limits
        return {'enabled': bool(pol.get('enabled', L['model_batch_generation_enabled'])), 'max_sequences': int(pol.get('max_sequences', L['model_batch_max_sequences'])), 'max_tokens': int(pol.get('max_tokens', L['model_batch_max_tokens'])),
                'wait_ms': int(pol.get('wait_ms', L['model_batch_wait_ms'])), 'modes': pol.get('modes', ['static']), 'kv_budget_bytes': int(pol.get('kv_budget_bytes', L['model_batch_kv_budget_bytes']))}

    @staticmethod
    def kv_bytes_per_token(row):
        cfg = json.loads(row['config_json'] or '{}')
        layers = int(cfg.get('num_hidden_layers') or 24); heads = int(cfg.get('num_attention_heads') or 16); kv = int(cfg.get('num_key_value_heads') or heads)
        head_dim = int(cfg.get('head_dim') or (int(cfg.get('hidden_size') or 1024) // max(heads, 1)))
        return 2 * layers * kv * head_dim * 2                   # K and V, bf16/fp16

    @staticmethod
    def prompt_estimate(inputs):
        text = ' '.join(m['content'] for m in inputs['messages']) if inputs.get('messages') else inputs.get('prompt', '')
        return len(text) // 3 + 16                                # conservative pre-tokenization estimate; the runtime counts real tokens

    def _claim_generation_companions(self, job, inputs, row):
        """Queued greedy text_generation jobs of the same workspace AND submitter on the same revision that fit under the
        sequence/token/KV envelope; claimed with the ordinary lease fencing. No mixing of unrelated workspaces; sampled
        requests never join (batch-global random state); a bounded wait window may be configured (default none)."""
        pol = self.batching_policy()
        if not pol['enabled'] or 'static' not in pol['modes'] or int(inputs.get('temperature_percent') or 0) > 0:
            return []
        kvb = self.kv_bytes_per_token(row)
        seqs, tokens = 1, self.prompt_estimate(inputs) + inputs['max_output_tokens']
        members = []
        def scan(db):
            nonlocal seqs, tokens
            cands = db.execute("SELECT j.* FROM jobs j JOIN model_requests r ON r.job_id=j.id WHERE j.state='queued' AND j.cancel_requested=0 AND j.hold=0 AND j.kind='text_generation' AND j.workspace=? AND j.submitted_by=? AND r.revision_id=? AND j.id!=? ORDER BY j.created_at LIMIT 16",
                               (job['workspace'], job['submitted_by'], row['id'], job['id'])).fetchall()
            for cand in cands:
                if seqs >= pol['max_sequences']:
                    break
                if not scheduling.local_allowed(cand['location_policy']):
                    continue
                contract, spec = self.worker._spec(db, cand); ci = spec['inputs']
                if int(ci.get('temperature_percent') or 0) > 0:
                    continue
                need = self.prompt_estimate(ci) + ci['max_output_tokens']
                if tokens + need > pol['max_tokens'] or (tokens + need) * kvb > pol['kv_budget_bytes']:
                    with self.worker.db.tx() as db2:
                        db2.execute('UPDATE model_requests SET phase=?, updated_at=? WHERE job_id=? AND phase=?', ('admitted', now(), cand['id'], 'admitted'))
                    continue                                       # too large for this batch: waits for its own turn (reason recorded in the batch outcome)
                generation = cand['lease_generation'] + 1
                changed = db.execute("UPDATE jobs SET state='running', lease_owner=?, lease_expires=?, lease_generation=?, attempt=attempt+1, updated_at=? WHERE id=? AND lease_generation=? AND state='queued'",
                                     (self.worker.worker_id, now() + self.worker.lease, generation, now(), cand['id'], cand['lease_generation'])).rowcount
                if changed != 1:
                    continue
                cj = dict(db.execute('SELECT * FROM jobs WHERE id=?', (cand['id'],)).fetchone())
                db.execute('INSERT INTO attempts VALUES (?,?,?,?,?,NULL,NULL)', ('at_' + secrets.token_hex(6), cj['id'], generation, self.worker.worker_id, now()))
                history.record(db, cj['workspace'], self.worker.worker_id, 'job.claimed', 'job', cj['id'], {'generation': generation, 'attempt': cj['attempt'], 'batched_with': job['id']})
                db.execute('UPDATE model_requests SET phase=?, host=?, attempt_generation=?, started_at=?, queue_seconds=?, updated_at=? WHERE job_id=?', ('loading', self.host.host, generation, now(), now() - cj['created_at'], now(), cj['id']))
                members.append((cj, contract, spec)); seqs += 1; tokens += need
        with self.worker.db.tx() as db:
            scan(db)
        if not members and pol['wait_ms'] > 0:
            time.sleep(min(pol['wait_ms'], 5000) / 1000.0)         # bounded waiting window (operator setting; default 0)
            with self.worker.db.tx() as db:
                scan(db)
        return members

    def _admit_generation(self, job, row, pol, budget, exclude):
        """Admission during a continuous session: the same cohort rule as static batching (one workspace and submitter, same
        revision, greedy only) under the sequence/token/KV envelope of what is still outstanding; each admitted job is claimed
        with the ordinary lease fencing. Returns [(job_row, contract, spec)]."""
        kvb = self.kv_bytes_per_token(row)
        members = []
        with self.worker.db.tx() as db:
            cands = db.execute("SELECT j.* FROM jobs j JOIN model_requests r ON r.job_id=j.id WHERE j.state='queued' AND j.cancel_requested=0 AND j.hold=0 AND j.kind='text_generation' AND j.workspace=? AND j.submitted_by=? AND r.revision_id=? ORDER BY j.created_at LIMIT 16",
                               (job['workspace'], job['submitted_by'], row['id'])).fetchall()
            for cand in cands:
                if cand['id'] in exclude or budget['seqs'] >= pol['max_sequences']:
                    continue
                if not scheduling.local_allowed(cand['location_policy']):
                    continue
                contract, spec = self.worker._spec(db, cand); ci = spec['inputs']
                if int(ci.get('temperature_percent') or 0) > 0:
                    continue
                need = self.prompt_estimate(ci) + ci['max_output_tokens']
                if budget['tokens'] + need > pol['max_tokens'] or (budget['tokens'] + need) * kvb > pol['kv_budget_bytes']:
                    continue
                generation = cand['lease_generation'] + 1
                changed = db.execute("UPDATE jobs SET state='running', lease_owner=?, lease_expires=?, lease_generation=?, attempt=attempt+1, updated_at=? WHERE id=? AND lease_generation=? AND state='queued'",
                                     (self.worker.worker_id, now() + self.worker.lease, generation, now(), cand['id'], cand['lease_generation'])).rowcount
                if changed != 1:
                    continue
                cj = dict(db.execute('SELECT * FROM jobs WHERE id=?', (cand['id'],)).fetchone())
                db.execute('INSERT INTO attempts VALUES (?,?,?,?,?,NULL,NULL)', ('at_' + secrets.token_hex(6), cj['id'], generation, self.worker.worker_id, now()))
                history.record(db, cj['workspace'], self.worker.worker_id, 'job.claimed', 'job', cj['id'], {'generation': generation, 'attempt': cj['attempt'], 'admitted_to_session_of': job['id']})
                db.execute('UPDATE model_requests SET phase=?, host=?, attempt_generation=?, started_at=?, queue_seconds=?, updated_at=? WHERE job_id=?', ('running', self.host.host, generation, now(), now() - cj['created_at'], now(), cj['id']))
                members.append((cj, contract, spec)); budget['seqs'] += 1; budget['tokens'] += need
        return members

    def _generate_continuous(self, job, contract, spec, row, inputs, should_cancel, state, pol):
        """Continuous admission (§66-2): the primary opens a session; compatible queued requests are admitted while it runs and
        removed as they finish or are cancelled; every member keeps its own lifecycle, segments, usage and accounting. Returns
        None when the runtime refuses the session so the caller falls back to static batching."""
        all_members = {}                                   # request id -> (job, contract, spec, inputs, sink, state)
        by_job = {}
        budget = {'seqs': 1, 'tokens': self.prompt_estimate(inputs) + inputs['max_output_tokens']}
        def register(mj, mc, ms, mi, st):
            rid = 'r' + secrets.token_hex(6)
            all_members[rid] = (mj, mc, ms, mi, SegmentSink(self, mj, st), st); by_job[mj['id']] = rid
            self._update(mj, phase='running')
            return {'request_id': rid, 'messages': mi.get('messages'), 'prompt': mi.get('prompt'), 'max_new_tokens': mi['max_output_tokens'], 'temperature_percent': 0, 'stop': mi.get('stop') or []}
        initial = [register(job, contract, spec, inputs, state)]
        last = {'renew': time.time()}
        def admit():
            for (mj, mc, ms) in self._admit_generation(job, row, pol, budget, set(by_job)):
                yield register(mj, mc, ms, ms['inputs'], {'fenced': False})
        def cancel_check():
            if time.time() - last['renew'] >= self.limits['compute_lease_renew_seconds']:
                self._renew_all([m[0] for m in all_members.values()]); last['renew'] = time.time()
            with self.worker.db.read() as db:
                ids = list(by_job)
                rows = db.execute('SELECT id FROM jobs WHERE cancel_requested=1 AND id IN (%s)' % ','.join('?' * len(ids)), ids).fetchall()
            return {by_job[r['id']] for r in rows}
        started = now(); kvb = self.kv_bytes_per_token(row)
        try:
            out = self.host.generate_continuous(row, initial, lambda: list(admit()), on_segment=lambda rid, seq, text: all_members[rid][4](seq, text), cancel_check=cancel_check, idle_seconds=max(0.2, pol['wait_ms'] / 1000.0 if pol['wait_ms'] else 1.0))
        except ServiceError as exc:
            body = exc.body()
            if len(all_members) == 1 and (body.get('detail') or {}).get('code') in ('CONTINUOUS_UNSUPPORTED', 'CONTINUOUS_START_FAILED'):
                return None                                                      # explicit static fallback for the primary
            for rid, (mj, mc, ms, mi, sink, st) in all_members.items():
                self._update(mj, phase='failed', error=json.dumps(body)[:300])
                res = self.worker._finish(mj, None, 'COMPUTATION_ERROR')
                if mj is job:
                    primary = res
            return primary
        batch = out['batch']; bid = batch['id']
        positions = {a['request_id']: a['position'] for a in out['admissions']}
        primary = None; cancelled = 0
        for rid, (mj, mc, ms, mi, sink, st) in all_members.items():
            sink.flush(force=True)
            done = out['results'].get(rid); k = positions.get(rid, 0)
            if st['fenced']:
                if mj is job:
                    primary = 'fenced'
                continue
            if done is None or 'error' in done:
                err = (done or {}).get('error') or {'code': 'no_result'}
                self._update(mj, phase='failed', error=json.dumps(err)[:300])
                res = self.worker._finish(mj, None, 'INPUT_INVALID' if err.get('code') == 'INPUT_INVALID' else 'COMPUTATION_ERROR')
            else:
                text, nsegs = sink.text(); usage = done['usage']
                self._update(mj, input_tokens=usage['input_tokens'], output_tokens=usage['output_tokens'], finish_reason=done['finish_reason'], inference_ms=done['ms'], usage_json=json.dumps(dict(usage, batch_id=bid, batch_position=k, batch_mode='continuous')), batch_id=bid, batch_position=k)
                if done['finish_reason'] == 'cancelled':
                    cancelled += 1; self._update(mj, phase='cancelled')
                    history.record_safe(self.worker.db, mj['workspace'], self.worker.worker_id, 'model.request', 'job', mj['id'], {'finish_reason': 'cancelled', 'output_tokens': usage['output_tokens'], 'partial_output_preserved': True, 'batch_id': bid, 'mode': 'continuous'})
                    res = self.worker._finish(mj, None, 'CANCELLED')
                elif hashlib.sha256(text.encode()).hexdigest() != done['text_sha256']:
                    self._update(mj, phase='failed', error='persisted segments do not reproduce the runtime output')
                    res = self.worker._finish(mj, None, 'COMPUTATION_ERROR')
                else:
                    output = {'schema': RESULT_SCHEMAS['text_generation'], 'text': text, 'finish_reason': done['finish_reason'], 'usage': usage, 'config': done['config'], 'segments': nsegs,
                              'model_revision_id': row['id'], 'model_id': row['model_id'], 'revision': row['revision'], 'weight_digest': row['weight_digest'], 'tokenizer_digest': row['tokenizer_digest'],
                              'versions': self.host.children[row['id']].ready['versions'] if row['id'] in self.host.children and self.host.children[row['id']].ready else None,
                              'request': {kk: mi.get(kk) for kk in ('messages', 'prompt', 'max_output_tokens', 'temperature_percent', 'top_p_percent', 'seed', 'stop')}}
                    res = self._complete(mj, mc, ms, row, output, 'GENERATED', {'output_tokens': usage['output_tokens'], 'input_tokens': usage['input_tokens'], 'finish_reason': done['finish_reason'], 'segments': nsegs, 'inference_ms': done['ms'],
                                                                                'tokens_per_second': done.get('tokens_per_second'), 'text_sha256': done['text_sha256'], 'output_chars': len(text), 'batch_id': bid, 'batch_members': len(all_members), 'batch_position': k, 'batch_mode': 'continuous'})
            if mj is job:
                primary = res
        with self.worker.db.tx() as db:
            db.execute('INSERT INTO model_batches (id, workspace, host, revision_id, mode, members, member_jobs_json, prompt_tokens, max_new_tokens, decode_steps, padded_prompt_length, ms, kv_estimate_bytes, cuda_peak_delta_bytes, cancelled_members, outcome_json, started_at, finished_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (bid, job['workspace'], self.host.host, row['id'], 'continuous', len(all_members), json.dumps([m[0]['id'] for m in all_members.values()]), sum(a.get('input_tokens') or 0 for a in out['admissions']), max(m[3]['max_output_tokens'] for m in all_members.values()), batch.get('steps'), None, batch.get('ms'),
                        kvb * budget['tokens'], (batch.get('memory') or {}).get('cuda_peak_delta_bytes'), cancelled,
                        json.dumps({'note': batch.get('note'), 'admissions': out['admissions'], 'admitted_during_session': max(0, len(all_members) - 1)}), started, now()))
        return primary

    def _renew_all(self, jobs):
        with self.worker.db.tx() as db:
            for j in jobs:
                db.execute("UPDATE jobs SET lease_expires=?, updated_at=? WHERE id=? AND state='running' AND lease_owner=? AND lease_generation=?", (now() + self.limits['job_lease_seconds'], now(), j['id'], self.worker.worker_id, j['lease_generation']))

    def _generate_static_batch(self, job, contract, spec, row, inputs, should_cancel, state, members):
        all_members = [(job, contract, spec, inputs)] + [(mj, mc, ms, ms['inputs']) for (mj, mc, ms) in members]
        sinks, requests, by_rid, states = {}, [], {}, {}
        for k, (mj, mc, ms, mi) in enumerate(all_members):
            rid = 'r' + secrets.token_hex(6); by_rid[rid] = k
            states[k] = {'fenced': False} if mj is not job else state
            sinks[k] = SegmentSink(self, mj, states[k])
            requests.append({'request_id': rid, 'messages': mi.get('messages'), 'prompt': mi.get('prompt'), 'max_new_tokens': mi['max_output_tokens'], 'temperature_percent': 0, 'stop': mi.get('stop') or []})
            self._update(mj, phase='running')
        last = {'renew': time.time()}
        def cancel_check():
            if time.time() - last['renew'] >= self.limits['compute_lease_renew_seconds']:
                self._renew_all([m[0] for m in all_members]); last['renew'] = time.time()
            with self.worker.db.read() as db:
                ids = [m[0]['id'] for m in all_members]
                rows = db.execute('SELECT id FROM jobs WHERE cancel_requested=1 AND id IN (%s)' % ','.join('?' * len(ids)), ids).fetchall()
            want = {r['id'] for r in rows}
            return {rid for rid, k in by_rid.items() if all_members[k][0]['id'] in want}
        started = now(); kvb = self.kv_bytes_per_token(row)
        try:
            out = self.host.generate_batch(row, requests, on_segment=lambda rid, seq, text: sinks[by_rid[rid]](seq, text), cancel_check=cancel_check)
        except ServiceError as exc:
            body = exc.body()
            for k, (mj, mc, ms, mi) in enumerate(all_members):
                self._update(mj, phase='failed', error=json.dumps(body)[:300])
                res = self.worker._finish(mj, None, 'COMPUTATION_ERROR')
                if mj is job:
                    primary = res
            return primary
        batch = out['batch']; bid = batch['id']
        primary = None; cancelled = 0
        for k, (mj, mc, ms, mi) in enumerate(all_members):
            rid = next(r for r, kk in by_rid.items() if kk == k)
            sinks[k].flush(force=True)
            done = out['results'].get(rid)
            if states[k]['fenced']:
                if mj is job:
                    primary = 'fenced'
                continue
            if done is None or 'error' in done:
                err = (done or {}).get('error') or {'code': 'no_result'}
                self._update(mj, phase='failed', error=json.dumps(err)[:300])
                res = self.worker._finish(mj, None, 'INPUT_INVALID' if err.get('code') == 'INPUT_INVALID' else 'COMPUTATION_ERROR')
            else:
                text, nsegs = sinks[k].text()
                usage = done['usage']
                self._update(mj, input_tokens=usage['input_tokens'], output_tokens=usage['output_tokens'], finish_reason=done['finish_reason'], inference_ms=done['ms'], usage_json=json.dumps(dict(usage, batch_id=bid, batch_position=k)), batch_id=bid, batch_position=k)
                if done['finish_reason'] == 'cancelled':
                    cancelled += 1
                    self._update(mj, phase='cancelled')
                    history.record_safe(self.worker.db, mj['workspace'], self.worker.worker_id, 'model.request', 'job', mj['id'], {'finish_reason': 'cancelled', 'output_tokens': usage['output_tokens'], 'partial_output_preserved': True, 'batch_id': bid})
                    res = self.worker._finish(mj, None, 'CANCELLED')
                elif hashlib.sha256(text.encode()).hexdigest() != done['text_sha256']:
                    self._update(mj, phase='failed', error='persisted segments do not reproduce the runtime output')
                    res = self.worker._finish(mj, None, 'COMPUTATION_ERROR')
                else:
                    output = {'schema': RESULT_SCHEMAS['text_generation'], 'text': text, 'finish_reason': done['finish_reason'], 'usage': usage, 'config': done['config'], 'segments': nsegs,
                              'model_revision_id': row['id'], 'model_id': row['model_id'], 'revision': row['revision'], 'weight_digest': row['weight_digest'], 'tokenizer_digest': row['tokenizer_digest'],
                              'versions': self.host.children[row['id']].ready['versions'] if row['id'] in self.host.children and self.host.children[row['id']].ready else None,
                              'request': {kk: mi.get(kk) for kk in ('messages', 'prompt', 'max_output_tokens', 'temperature_percent', 'top_p_percent', 'seed', 'stop')}}
                    res = self._complete(mj, mc, ms, row, output, 'GENERATED', {'output_tokens': usage['output_tokens'], 'input_tokens': usage['input_tokens'], 'finish_reason': done['finish_reason'], 'segments': nsegs, 'inference_ms': done['ms'],
                                                                                'tokens_per_second': done.get('tokens_per_second'), 'text_sha256': done['text_sha256'], 'output_chars': len(text), 'batch_id': bid, 'batch_members': len(all_members), 'batch_position': k})
            if mj is job:
                primary = res
        with self.worker.db.tx() as db:
            db.execute('INSERT INTO model_batches (id, workspace, host, revision_id, mode, members, member_jobs_json, prompt_tokens, max_new_tokens, decode_steps, padded_prompt_length, ms, kv_estimate_bytes, cuda_peak_delta_bytes, cancelled_members, outcome_json, started_at, finished_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (bid, job['workspace'], self.host.host, row['id'], 'static', len(all_members), json.dumps([m[0]['id'] for m in all_members]), batch.get('prompt_tokens'), max(m[3]['max_output_tokens'] for m in all_members), batch.get('steps'), batch.get('padded_prompt_length'), batch.get('ms'),
                        kvb * (int(batch.get('padded_prompt_length') or 0) + int(batch.get('steps') or 0)) * len(all_members), (batch.get('memory') or {}).get('cuda_peak_delta_bytes'), cancelled,
                        json.dumps({'note': batch.get('note'), 'error': batch.get('error')}), started, now()))
        return primary

    def _cancel_requested(self, job):
        with self.worker.db.read() as db:
            return bool(db.execute('SELECT cancel_requested FROM jobs WHERE id=?', (job['id'],)).fetchone()[0])

    def _embed(self, job, contract, spec, row, inputs, should_cancel, state, members=()):
        if should_cancel():
            for (mj, mc, ms) in members:                 # companions go back to the queue untouched (their lease expires) rather than being cancelled by proxy
                self._requeue(mj)
            return 'fenced' if state['fenced'] else self.worker._finish(job, None, 'CANCELLED')
        batch = [(job, contract, spec, inputs)]
        for (mj, mc, ms) in members:
            if self._cancel_requested(mj):
                self._update(mj, phase='cancelled'); self.worker._finish(mj, None, 'CANCELLED')
            else:
                batch.append((mj, mc, ms, ms['inputs']))
        texts = [t for (_, _, _, mi) in batch for t in mi['texts']]
        ev = self.host.embed(row, texts, truncate=bool(inputs.get('truncate')))
        from ..compute import npy
        batch_id = 'mb_' + secrets.token_hex(6) if len(batch) > 1 else None
        composition = None
        if batch_id:
            with self.worker.db.read() as db:
                digests = [db.execute('SELECT request_digest FROM model_requests WHERE job_id=?', (mj['id'],)).fetchone()[0] for (mj, _, _, _) in batch]
            composition = hashlib.sha256('\n'.join(digests).encode()).hexdigest()
        total_tokens = max(1, sum(ev['tokens']))
        result, offset = None, 0
        for position, (mj, mc, ms, mi) in enumerate(batch):
            n = len(mi['texts'])
            sl = slice(offset, offset + n); offset += n
            vectors, tokens, before, trunc = ev['vectors'][sl], ev['tokens'][sl], ev['tokens_before_truncation'][sl], ev['truncated'][sl]
            if mj is not job and self._cancel_requested(mj):
                self._update(mj, phase='cancelled'); self.worker._finish(mj, None, 'CANCELLED'); continue          # cancelled while the batch ran: its vectors are discarded, never stored
            share_ms = ev['ms'] if not batch_id else int(ev['ms'] * sum(tokens) / total_tokens)
            flat = [x for v in vectors for x in v]
            blob = npy.encode(flat, '<f8', (len(vectors), ev['dim']))
            meta = {'schema': RESULT_SCHEMAS['text_embedding'], 'items': len(vectors), 'dim': ev['dim'], 'pooling': ev['pooling'], 'normalized': ev['normalized'], 'max_seq_length': ev['max_seq_length'],
                    'tokens': tokens, 'tokens_before_truncation': before, 'truncated': trunc, 'model_revision_id': row['id'], 'model_id': row['model_id'], 'revision': row['revision'],
                    'weight_digest': row['weight_digest'], 'tokenizer_digest': row['tokenizer_digest'], 'dtype': '<f8 (float32 model output widened; not extra precision)', 'vector_sha256': hashlib.sha256(blob).hexdigest(),
                    'similarity_policy': 'cosine = dot product of the L2-normalized vectors; comparable only within the same revision/pooling/normalization', 'inference_ms': share_ms,
                    'batch': ({'id': batch_id, 'members': len(batch), 'position': position, 'composition_sha256': composition, 'batch_inference_ms': ev['ms'], 'inference_ms_basis': 'token-proportional share of the batch',
                               'isolation': 'members share workspace and submitter; each item is attended in isolation by its attention mask; padding to the batch shape may change the last floating-point bits versus solo execution'} if batch_id else None)}
            self._update(mj, items=len(vectors), inference_ms=share_ms, usage_json=json.dumps({'items': len(vectors), 'tokens': sum(tokens), 'batch_id': batch_id}), input_tokens=sum(tokens))
            out = self._complete(mj, mc, ms, row, meta, 'EMBEDDED', {'items': len(vectors), 'dim': ev['dim'], 'tokens': sum(tokens), 'truncated_items': sum(1 for t in trunc if t), 'inference_ms': share_ms, 'batch_id': batch_id, 'batch_members': len(batch)},
                                 extra_files={'vectors.npy': blob})
            if mj is job:
                result = out
        return result

    def _requeue(self, job):
        with self.worker.db.tx() as db:
            db.execute("UPDATE jobs SET state='queued', lease_owner=NULL, lease_expires=NULL, updated_at=? WHERE id=? AND state='running' AND lease_owner=? AND lease_generation=?", (now(), job['id'], self.worker.worker_id, job['lease_generation']))
            db.execute("UPDATE model_requests SET phase='admitted', updated_at=? WHERE job_id=?", (now(), job['id']))

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
