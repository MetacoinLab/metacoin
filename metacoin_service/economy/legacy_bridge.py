"""Read-only bridge from the frozen deterministic task registry (demo/tasks + the public ledger's registered output
hashes) to the work service (Order 08 §33). Nothing here edits a task, regenerates an expected result or relaxes the
historical exact-hash rule: a replay recomputes the task with its registered implementation and compares the canonical
output hash exactly. Newer numerical/model services are separate acceptance classes and cannot be advertised as legacy
exact tasks through this bridge."""
import hashlib
import importlib
import json
import re
from pathlib import Path

from experiments.private_receipts import receipt as merkle

ROOT = Path(__file__).resolve().parents[2]
TASK_DIR = ROOT / 'demo' / 'tasks'
LEDGER = ROOT / 'protocol' / 'ledger_data.jsonl'
INPUT_SCHEMA = 'legacy-task-replay/v1'
RESULT_SCHEMA = 'legacy-task-canonical-json'
MODEL_ID = 'legacy-deterministic-task-registry/read-only'
VERIFIER_ID = 'legacy-exact-hash/v1'
_ID = re.compile(r'task-\d{4}\Z')
_cache = {}


def registry():
    """task_id -> {module, file, registered_hash, mission_node} from the task files and the ledger (first recorded hash,
    the agent_verifier convention). Cached per process; the sources are frozen files."""
    if 'registry' in _cache:
        return _cache['registry']
    tasks = {}
    for f in sorted(TASK_DIR.glob('task_*.py')):
        num = f.name[5:9]
        tasks['task-' + num] = {'module': 'demo.tasks.' + f.stem, 'file': str(f.relative_to(ROOT)), 'source_sha256': hashlib.sha256(f.read_bytes()).hexdigest(), 'registered_hash': None, 'registered_at_index': None}
    if LEDGER.exists():
        for line in LEDGER.read_text().splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            p = e.get('payload') or {}
            tid = p.get('task_id')
            if tid in tasks and tasks[tid]['registered_hash'] is None:
                for key in ('local_output_hash', 'output_hash', 'submitted_output_hash'):
                    if isinstance(p.get(key), str):
                        tasks[tid]['registered_hash'] = p[key]; tasks[tid]['registered_at_index'] = e.get('index'); break
    _cache['registry'] = tasks
    return tasks


def implementation_digest():
    h = hashlib.sha256(b'metacoin/legacy-bridge/v1\0')
    for tid, t in sorted(registry().items()):
        h.update((tid + ':' + t['source_sha256'] + ':' + str(t['registered_hash'])).encode())
    return h.hexdigest()


def validate(data):
    if type(data) is not dict or set(data) - {'schema', 'task_id', 'private_label'} or data.get('schema') != INPUT_SCHEMA:
        raise merkle.Invalid('legacy replay input: {schema: %s, task_id, private_label?}' % INPUT_SCHEMA)
    tid = data.get('task_id')
    if type(tid) is not str or not _ID.fullmatch(tid) or tid not in registry():
        raise merkle.Invalid('unknown frozen task id')
    if 'private_label' in data and (type(data['private_label']) is not str or len(data['private_label']) > 128):
        raise merkle.Invalid('private_label')
    return data


def replay(task_id):
    """Recompute the frozen task with its registered module; returns the canonical result, its hash and the comparison."""
    t = registry()[task_id]
    mod = importlib.import_module(t['module'])
    result = mod.compute()
    canonical = mod.canonical_json(result)
    output_hash = mod.output_hash(result)
    return {'result_schema': RESULT_SCHEMA, 'model_id': MODEL_ID, 'task_id': task_id, 'output_hash': output_hash, 'canonical_sha256': hashlib.sha256(canonical.encode()).hexdigest(),
            'registered_hash': t['registered_hash'], 'matches_registered': (t['registered_hash'] == output_hash) if t['registered_hash'] else None, 'source_sha256': t['source_sha256'],
            'canonical_result_json': canonical, 'rule': 'legacy exact rule: the canonical output hash must equal the registered hash byte for byte; no tolerance', 'verifier_id': VERIFIER_ID}


def run(inputs):
    """Worker child entry: evidence for the job. Never mutates the registry."""
    r = replay(inputs['task_id'])
    outcome = 'EXACT_MATCH' if r['matches_registered'] else ('UNREGISTERED' if r['registered_hash'] is None else 'HASH_MISMATCH')
    summary = {k: r[k] for k in ('task_id', 'output_hash', 'registered_hash', 'matches_registered', 'source_sha256', 'rule', 'model_id', 'result_schema', 'verifier_id')}
    summary['outcome'] = outcome
    return r, outcome, summary


def audit(inputs, result):
    """Verification audit (class full_exact): recompute and compare with BOTH the producer's result and the registered hash."""
    fresh = replay(inputs['task_id'])
    same_as_producer = fresh['output_hash'] == result.get('output_hash') and fresh['canonical_sha256'] == result.get('canonical_sha256')
    registered_ok = fresh['matches_registered'] is True
    checks = [{'check': 'recomputed_hash_equals_producer_hash', 'ok': same_as_producer, 'detail': {'fresh': fresh['output_hash'], 'producer': result.get('output_hash')}},
              {'check': 'recomputed_hash_equals_registered_hash', 'ok': registered_ok, 'detail': {'registered': fresh['registered_hash'], 'index': registry()[inputs['task_id']]['registered_at_index']}}]
    return {'outcome': 'passed' if same_as_producer and registered_ok else 'failed', 'checked': 2, 'total': 2, 'checks': checks, 'coverage': 'whole canonical output, exact',
            'statement': 'frozen task recomputed by its registered implementation in a separate process; exact hash comparison against the producer and the public ledger registration (legacy exact rule unchanged)'}
