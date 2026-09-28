"""Application-side model operations: request validation (strict, integer-only sampling controls because the canonical
evidence format has no floats), contract binding, job views with usage, durable segment delivery with a cursor
(resumable delivery, never a second inference), output access, runtime facts and operator load/unload."""
import hashlib
import json
from experiments.private_receipts import receipt as merkle
from .. import history
from ..db import now
from ..errors import ServiceError
from . import registry as registry_mod
from .engine import GENERATION_SCHEMA, EMBEDDING_SCHEMA, KINDS, OPERATION_OF, MODEL_IDS, RESULT_SCHEMAS, implementation_digest

ROLES = ('system', 'user', 'assistant')


class ModelInvalid(ServiceError):
    def __init__(self, msg):
        super().__init__('VALIDATION', {'code': 'model_input', 'reason': msg})
        self.msg = msg

    def __str__(self):
        return self.msg


def _limits():
    from ..config import LIMITS
    return LIMITS


def validate_generation(data):
    L = _limits()
    if type(data) is not dict or data.get('schema') != GENERATION_SCHEMA:
        raise ModelInvalid('schema must be ' + GENERATION_SCHEMA)
    allowed = {'schema', 'messages', 'prompt', 'max_output_tokens', 'temperature_percent', 'top_p_percent', 'seed', 'stop', 'model_revision_id', 'private_label', 'purpose'}
    unknown = set(data) - allowed
    if unknown:
        raise ModelInvalid('unknown fields are refused: ' + ','.join(sorted(unknown)))
    msgs, prompt = data.get('messages'), data.get('prompt')
    if (msgs is None) == (prompt is None):
        raise ModelInvalid('exactly one of messages or prompt')
    total = 0
    if msgs is not None:
        if type(msgs) is not list or not 1 <= len(msgs) <= L['model_max_messages']:
            raise ModelInvalid('messages: 1..%d items' % L['model_max_messages'])
        for m in msgs:
            if type(m) is not dict or set(m) != {'role', 'content'} or m['role'] not in ROLES or type(m['content']) is not str or not m['content'].strip():
                raise ModelInvalid('message: {role: system|user|assistant, content: non-empty string}')
            total += len(m['content'])
        if msgs[-1]['role'] != 'user':
            raise ModelInvalid('the last message must be from the user')
    else:
        if type(prompt) is not str or not prompt.strip():
            raise ModelInvalid('prompt: non-empty string')
        total = len(prompt)
    if total > L['model_max_input_tokens'] * 8:
        raise ModelInvalid('input exceeds %d characters' % (L['model_max_input_tokens'] * 8))
    mo = data.get('max_output_tokens')
    if type(mo) is not int or not 1 <= mo <= L['model_max_output_tokens']:
        raise ModelInvalid('max_output_tokens: 1..%d' % L['model_max_output_tokens'])
    t = data.get('temperature_percent', 0)
    if type(t) is not int or not 0 <= t <= 200:
        raise ModelInvalid('temperature_percent: integer 0..200 (0 = greedy)')
    tp = data.get('top_p_percent', 100)
    if type(tp) is not int or not 1 <= tp <= 100:
        raise ModelInvalid('top_p_percent: integer 1..100')
    seed = data.get('seed')
    if seed is not None and (type(seed) is not int or not 0 <= seed < 2 ** 63):
        raise ModelInvalid('seed: integer or null')
    stop = data.get('stop', [])
    if type(stop) is not list or len(stop) > 4 or not all(type(s) is str and 1 <= len(s) <= 32 for s in stop):
        raise ModelInvalid('stop: up to 4 strings of 1..32 chars')
    rid = data.get('model_revision_id')
    if rid is not None and (type(rid) is not str or not rid.startswith('mr_') or len(rid) > 32):
        raise ModelInvalid('model_revision_id')
    for k in ('private_label', 'purpose'):
        v = data.get(k)
        if v is not None and (type(v) is not str or len(v) > 128):
            raise ModelInvalid(k)
    return data


def validate_embedding(data):
    L = _limits()
    if type(data) is not dict or data.get('schema') != EMBEDDING_SCHEMA:
        raise ModelInvalid('schema must be ' + EMBEDDING_SCHEMA)
    allowed = {'schema', 'texts', 'truncate', 'model_revision_id', 'private_label', 'purpose'}
    unknown = set(data) - allowed
    if unknown:
        raise ModelInvalid('unknown fields are refused: ' + ','.join(sorted(unknown)))
    texts = data.get('texts')
    if type(texts) is not list or not 1 <= len(texts) <= L['model_max_embed_items']:
        raise ModelInvalid('texts: 1..%d strings' % L['model_max_embed_items'])
    for t in texts:
        if type(t) is not str or not t.strip() or len(t) > L['model_max_text_chars']:
            raise ModelInvalid('each text: non-empty string of at most %d characters' % L['model_max_text_chars'])
    if type(data.get('truncate', False)) is not bool:
        raise ModelInvalid('truncate: boolean')
    rid = data.get('model_revision_id')
    if rid is not None and (type(rid) is not str or not rid.startswith('mr_') or len(rid) > 32):
        raise ModelInvalid('model_revision_id')
    for k in ('private_label', 'purpose'):
        v = data.get(k)
        if v is not None and (type(v) is not str or len(v) > 128):
            raise ModelInvalid(k)
    return data


VALIDATORS = {'text_generation': validate_generation, 'text_embedding': validate_embedding}


def work_units(kind, data):
    """Quote quantity: generated-token allowance for generation, items for embedding (deterministic from the request)."""
    return data['max_output_tokens'] if kind == 'text_generation' else len(data['texts'])


WORK_UNIT = {'text_generation': 'generated token (allowance = max_output_tokens; billed = tokens actually generated, capped)', 'text_embedding': 'embedded item'}


def bind_params(db, settings, kind, inputs):
    """Called at draft creation: resolves the revision (explicit or promoted default) and binds it immutably."""
    reg = registry_mod.ModelRegistry(settings)
    row = reg.resolve(db, OPERATION_OF[kind], inputs.get('model_revision_id'))
    return {'model_revision_id': row['id'], 'model_id': row['model_id'], 'revision': row['revision'], 'weight_digest': row['weight_digest'], 'tokenizer_digest': row['tokenizer_digest'],
            'operation': OPERATION_OF[kind], 'implementation_digest': implementation_digest(), 'work_units': work_units(kind, inputs),
            'request_digest': hashlib.sha256(merkle.canonical(inputs)).hexdigest(), 'max_output_tokens': inputs.get('max_output_tokens'), 'max_items': len(inputs.get('texts', [])) or None}


def insert_request(db, jid, workspace, kind, params):
    db.execute('INSERT INTO model_requests (job_id, workspace, kind, revision_id, operation, phase, request_digest, max_output_tokens, max_items, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)',
               (jid, workspace, kind, params['model_revision_id'], params['operation'], 'admitted', params['request_digest'], params.get('max_output_tokens'), params.get('max_items'), now()))


def _req(db, principal, jobs, job_id):
    job = jobs.get(db, principal, job_id)
    req = db.execute('SELECT * FROM model_requests WHERE job_id=?', (job_id,)).fetchone()
    if req is None:
        raise ServiceError('NOT_FOUND', 'not a model job')
    return job, req


def view(db, principal, jobs, job_id):
    job, req = _req(db, principal, jobs, job_id)
    private = principal.can('job:read_private')
    rev = db.execute('SELECT model_id, revision, status, weight_digest FROM model_revisions WHERE id=?', (req['revision_id'],)).fetchone()
    out = {'job_id': job_id, 'kind': job['kind'], 'state': job['state'], 'review_state': job['review_state'], 'phase': req['phase'], 'operation': req['operation'],
           'model': {'revision_id': req['revision_id'], 'model_id': rev['model_id'], 'revision': rev['revision'], 'status_now': rev['status'], 'weight_digest': rev['weight_digest']},
           'usage': {'input_tokens': req['input_tokens'], 'output_tokens': req['output_tokens'], 'items': req['items'], 'finish_reason': req['finish_reason'],
                     'max_output_tokens': req['max_output_tokens'], 'max_items': req['max_items'], 'segments': req['segments'], 'output_chars': req['output_chars'],
                     'counting': 'measured by the runtime tokenizer after execution; the quote reserved max_output_tokens (or items); nothing is billed from an estimate'},
           'timing': {'queue_seconds': req['queue_seconds'], 'load_ms': req['load_ms'], 'inference_ms': req['inference_ms'], 'started_at': req['started_at'], 'updated_at': req['updated_at']},
           'host': req['host'], 'attempt_generation': req['attempt_generation'], 'cancel_requested': bool(job['cancel_requested']), 'error': req['error'] if private else None,
           'verification': 'none: generated output is not scientifically verified; it is data', 'outcome': job['outcome'] if private else None,
           'economic_state': jobs.payment_view(db, principal, job)['state'], 'allowed_actions': ['cancel'] if job['state'] in ('queued', 'running') and principal.can('job:cancel') else []}
    if private:
        out['versions'] = json.loads(req['versions_json']) if req['versions_json'] else None
        out['output_artifact_id'] = req['output_artifact_id']
    return out


def segments(db, principal, jobs, job_id, after=-1, limit=200):
    """Persisted output segments after a cursor (resumable delivery). Never triggers inference."""
    job, req = _req(db, principal, jobs, job_id)
    if not (principal.can('job:read_private') or (principal.role == 'reviewer' and db.execute('SELECT reviewer_id FROM contracts WHERE id=?', (job['contract_id'],)).fetchone()['reviewer_id'] == principal.id)):
        raise ServiceError('FORBIDDEN', 'generated text is private to the owner and the designated reviewer')
    gen = req['attempt_generation'] if req['attempt_generation'] is not None else job['lease_generation']
    rows = db.execute('SELECT seq, text, created_at FROM model_segments WHERE job_id=? AND attempt_generation=? AND seq>? ORDER BY seq LIMIT ?', (job_id, gen, after, limit)).fetchall()
    terminal = job['state'] in ('succeeded', 'failed', 'cancelled')
    return {'job_id': job_id, 'attempt_generation': gen, 'segments': [dict(r) for r in rows], 'cursor': rows[-1]['seq'] if rows else after, 'done': terminal and (len(rows) < limit),
            'state': job['state'], 'phase': req['phase'], 'finish_reason': req['finish_reason'],
            'note': 'delivery resumes from the cursor; a new attempt after a runtime failure has its own attempt_generation and is never spliced onto old segments'}


def output(db, principal, jobs, store, job_id, name=None):
    from ..compute import container
    job, req = _req(db, principal, jobs, job_id)
    contract = db.execute('SELECT reviewer_id FROM contracts WHERE id=?', (job['contract_id'],)).fetchone()
    if not (principal.can('artifact:read_private') or (principal.role == 'reviewer' and contract['reviewer_id'] == principal.id)):
        raise ServiceError('FORBIDDEN', 'model outputs are private to the owner and the designated reviewer')
    if req['output_artifact_id'] is None:
        raise ServiceError('NOT_FOUND', 'no output committed')
    blob = store.load(db, req['output_artifact_id'], principal.workspace)
    files = container.unpack(blob)
    if name is None:
        return {'job_id': job_id, 'files': [{'name': n, 'bytes': len(b), 'sha256': hashlib.sha256(b).hexdigest()} for n, b in sorted(files.items())], 'artifact_id': req['output_artifact_id']}
    if name not in files:
        raise ServiceError('NOT_FOUND', 'output file')
    return name, files[name]


def runtime_facts(db, settings, api_host=None):
    """Installed / configured / currently loaded / observed, per host."""
    from .engine import ModelHost, mem_available_bytes
    from ..compute.engine import compute_interpreter
    rt = compute_interpreter(settings)
    rows = [registry_mod.runtime_view(r) for r in db.execute('SELECT * FROM model_runtimes ORDER BY host, revision_id').fetchall()]
    completed = {r['revision_id']: r['n'] for r in db.execute("SELECT revision_id, COUNT(*) AS n FROM model_requests WHERE phase='completed' GROUP BY revision_id")}
    return {'installed': {'interpreter': (rt or {}).get('python'), 'torch': (rt or {}).get('torch'), 'cuda': bool((rt or {}).get('cuda')), 'device': (rt or {}).get('device'),
                          'registered_revisions': db.execute('SELECT COUNT(*) FROM model_revisions').fetchone()[0], 'installed_revisions': db.execute('SELECT COUNT(*) FROM model_revisions WHERE installed=1').fetchone()[0]},
            'configured': {k: settings.limits[k] for k in ('model_max_loaded', 'model_memory_budget_bytes', 'model_memory_headroom_bytes', 'model_max_input_tokens', 'model_max_output_tokens', 'model_max_embed_items', 'model_idle_unload_seconds')},
            'defaults': {r['operation']: r['revision_id'] for r in db.execute('SELECT operation, revision_id FROM model_defaults').fetchall()},
            'currently': {'runtimes': rows, 'mem_available_bytes': mem_available_bytes(), 'api_host': api_host.status() if api_host else None},
            'observed': {'completed_requests_by_revision': completed},
            'privacy': 'prompts, retrieved context, outputs and vectors stay on this host; the runtime child runs offline (HF_HUB_OFFLINE, TRANSFORMERS_OFFLINE, no proxy) and no hosted fallback exists',
            'note': 'a revision is callable only when a runtime host reports state=ready for it; readiness is observed, not inferred from installation'}


def request_load(db, principal, settings, rid, desired):
    """Operator intent: ask every live worker host that offers model kinds to load (or unload) a revision."""
    principal.require('model:admin')
    reg = registry_mod.ModelRegistry(settings)
    row = reg.row(db, rid)
    if desired == 'loaded' and (row['status'] != 'registered' or not row['installed']):
        raise ServiceError('CONFLICT', 'only an installed registered revision can be loaded')
    from ..scheduling import live
    hosts = ['worker:' + w['name'] for w in db.execute('SELECT * FROM workers').fetchall() if live(w) and 'text_generation' in json.loads(w['capabilities_json'])]
    for h in hosts:
        db.execute('INSERT INTO model_runtimes (host, revision_id, state, desired, updated_at) VALUES (?,?,?,?,?) ON CONFLICT(host, revision_id) DO UPDATE SET desired=excluded.desired, updated_at=excluded.updated_at',
                   (h, rid, 'unloaded', desired, now()))
    history.record(db, principal.workspace, principal.id, 'model.runtime', 'model', rid, {'desired': desired, 'hosts': hosts})
    return {'revision_id': rid, 'desired': desired, 'hosts': hosts, 'note': 'applied by each live worker on its next loop iteration; readiness is reported per host'}
