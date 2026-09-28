"""Model registry: immutable model revisions bound to a trusted loader.

A registration names a Hub repository and a pinned revision (or an operator-installed local artifact under the model
store); the service derives the directory itself, so a caller can never point the loader at an arbitrary path, URL or
Python import. Only the architectures listed in ARCHITECTURES load, only from safetensors, and never with remote
custom code. Registration is separate from installation (weights present and digested) and from readiness (a runtime
child loaded the weights and answered a bounded probe). Retiring or revoking a revision never redirects existing
contracts: jobs stay bound to the exact revision they accepted."""
import hashlib
import json
import os
import re
import secrets
from pathlib import Path

from .. import history
from ..db import now
from ..errors import ServiceError

# architecture -> trusted loader and the operations it supports. Adding an entry is a code change, never a request.
ARCHITECTURES = {
    'Qwen2ForCausalLM': {'loader': 'causal_lm', 'operations': ['generate'], 'family': 'qwen2', 'prompt_format': 'tokenizer chat_template'},
    'LlamaForCausalLM': {'loader': 'causal_lm', 'operations': ['generate'], 'family': 'llama', 'prompt_format': 'tokenizer chat_template'},
    'BertModel': {'loader': 'encoder', 'operations': ['embed'], 'family': 'bert', 'prompt_format': 'plain text'},
}
OPERATIONS = ('generate', 'embed')
STATUSES = ('registered', 'retired', 'revoked')
REVISION_RE = re.compile(r'^[0-9a-f]{40}$')
REPO_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,63}/[A-Za-z0-9][A-Za-z0-9._-]{0,95}$')
MODEL_ID_RE = re.compile(r'^[a-z0-9][a-z0-9.-]{1,63}$')
REQUIRED_FILES = {'causal_lm': ['config.json', 'model.safetensors', 'tokenizer.json', 'tokenizer_config.json'],
                  'encoder': ['config.json', 'model.safetensors', 'tokenizer.json', 'tokenizer_config.json']}
LICENSE_ALLOW = ('apache-2.0', 'mit', 'bsd-3-clause', 'cc-by-4.0', 'other-operator-reviewed')


def store_dir(settings):
    return Path(settings.model_store)


def local_dir(settings, hub_repo, revision):
    """The only way a directory is derived: store / repo-with-slash-replaced / revision."""
    return store_dir(settings) / hub_repo.replace('/', '__') / revision


def _digest_file(path, limit_bytes=None):
    h = hashlib.sha256()
    n = 0
    with open(path, 'rb') as f:
        while True:
            chunk = f.read(1 << 20)
            if not chunk:
                break
            h.update(chunk); n += len(chunk)
            if limit_bytes and n > limit_bytes:
                raise ServiceError('PAYLOAD_TOO_LARGE', 'model file exceeds the registry limit')
    return h.hexdigest(), n


def inspect_artifact(settings, hub_repo, revision):
    """Read-only inspection of the local artifact directory: architecture, format, digests, sizes, license file."""
    d = local_dir(settings, hub_repo, revision)
    out = {'local_dir_exists': d.is_dir(), 'files': {}, 'architecture': None, 'loader': None, 'weight_format': None, 'problems': []}
    if not d.is_dir():
        out['problems'].append('artifact directory absent (register records the identity; install the pinned files under the model store)')
        return out
    cfg_path = d / 'config.json'
    if not cfg_path.is_file():
        out['problems'].append('config.json missing'); return out
    try:
        cfg = json.loads(cfg_path.read_bytes())
    except ValueError:
        out['problems'].append('config.json unreadable'); return out
    archs = cfg.get('architectures') or []
    arch = archs[0] if len(archs) == 1 else None
    out['architecture'] = arch
    if arch not in ARCHITECTURES:
        out['problems'].append('architecture not in the trusted loader allowlist: ' + str(archs))
        return out
    if cfg.get('auto_map') or any(p.suffix == '.py' for p in d.iterdir()):
        out['problems'].append('custom model code present (auto_map or .py files); remote/custom code is never executed')
        return out
    loader = ARCHITECTURES[arch]['loader']
    out['loader'] = loader
    if (d / 'model.safetensors').is_file():
        out['weight_format'] = 'safetensors'
    elif any(p.name.startswith('model-') and p.suffix == '.safetensors' for p in d.iterdir()):
        out['weight_format'] = 'safetensors-sharded'
        out['problems'].append('sharded safetensors not supported by this registry version (single-file artifacts only)')
    else:
        out['problems'].append('no safetensors weights (pickle-based checkpoints are refused)')
    for name in REQUIRED_FILES[loader]:
        p = d / name
        if not p.is_file():
            out['problems'].append('required file missing: ' + name); continue
        digest, size = _digest_file(p, limit_bytes=settings.limits['model_max_weight_bytes'] if name.endswith('.safetensors') else 64 * 1024 * 1024)
        out['files'][name] = {'sha256': digest, 'bytes': size}
    for name in ('LICENSE', 'LICENSE.md', 'LICENSE.txt'):
        if (d / name).is_file():
            out['license_file'] = name; break
    out['config'] = {k: cfg.get(k) for k in ('model_type', 'hidden_size', 'num_hidden_layers', 'vocab_size', 'max_position_embeddings', 'torch_dtype', 'dtype', 'tie_word_embeddings') if k in cfg}
    pooling = d / '1_Pooling' / 'config.json'
    if loader == 'encoder':
        if pooling.is_file():
            try:
                pc = json.loads(pooling.read_bytes())
                out['pooling'] = 'mean' if pc.get('pooling_mode_mean_tokens') else ('cls' if pc.get('pooling_mode_cls_token') else 'unknown')
                out['embedding_dim'] = pc.get('word_embedding_dimension')
            except ValueError:
                out['pooling'] = 'unknown'
        else:
            out['pooling'] = 'mean (default; no pooling config shipped)'
            out['embedding_dim'] = cfg.get('hidden_size')
        sb = d / 'sentence_bert_config.json'
        if sb.is_file():
            try:
                out['max_seq_length'] = json.loads(sb.read_bytes()).get('max_seq_length')
            except ValueError:
                pass
    return out


class ModelRegistry:
    def __init__(self, settings):
        self.settings = settings

    # ---- registration ---------------------------------------------------------------------------
    def register(self, db, principal, body):
        principal.require('model:admin')
        if type(body) is not dict:
            raise ServiceError('VALIDATION', 'body')
        allowed = {'model_id', 'hub_repo', 'revision', 'operations', 'license', 'description', 'precision', 'resource_estimate_bytes', 'max_context_tokens'}
        if not set(body) <= allowed:
            raise ServiceError('VALIDATION', {'code': 'unknown_fields', 'unknown': sorted(set(body) - allowed)})
        model_id, repo, rev = body.get('model_id'), body.get('hub_repo'), body.get('revision')
        if type(model_id) is not str or not MODEL_ID_RE.match(model_id):
            raise ServiceError('VALIDATION', 'model_id: lowercase letters, digits, dots and dashes')
        if type(repo) is not str or not REPO_RE.match(repo) or '..' in repo:
            raise ServiceError('VALIDATION', 'hub_repo: owner/name')
        if type(rev) is not str or not REVISION_RE.match(rev):
            raise ServiceError('VALIDATION', 'revision: 40-hex pinned commit')
        ops = body.get('operations')
        if type(ops) is not list or not ops or not set(ops) <= set(OPERATIONS) or len(set(ops)) != len(ops):
            raise ServiceError('VALIDATION', 'operations: subset of ' + ','.join(OPERATIONS))
        lic = body.get('license')
        if type(lic) is not str or lic not in LICENSE_ALLOW:
            raise ServiceError('VALIDATION', {'code': 'license', 'allowed': list(LICENSE_ALLOW), 'note': 'operator states the license of the pinned artifact; not verified against the hub'})
        desc = body.get('description', '')
        if type(desc) is not str or len(desc) > 512:
            raise ServiceError('VALIDATION', 'description')
        precision = body.get('precision', 'auto')
        if precision not in ('auto', 'float32', 'bfloat16', 'float16'):
            raise ServiceError('VALIDATION', 'precision')
        est = body.get('resource_estimate_bytes')
        if est is not None and (type(est) is not int or not 0 < est <= self.settings.limits['model_max_weight_bytes'] * 4):
            raise ServiceError('VALIDATION', 'resource_estimate_bytes')
        ctx = body.get('max_context_tokens')
        if ctx is not None and (type(ctx) is not int or not 64 <= ctx <= 1 << 20):
            raise ServiceError('VALIDATION', 'max_context_tokens')
        if db.execute('SELECT 1 FROM model_revisions WHERE model_id=? AND revision=?', (model_id, rev)).fetchone():
            raise ServiceError('CONFLICT', 'this model revision is already registered (revisions are immutable)')
        insp = inspect_artifact(self.settings, repo, rev)
        loader = insp['loader']
        if loader and not all(op in ARCHITECTURES[insp['architecture']]['operations'] for op in ops):
            raise ServiceError('VALIDATION', {'code': 'operation_unsupported_by_architecture', 'architecture': insp['architecture'], 'supported': ARCHITECTURES[insp['architecture']]['operations']})
        installed = insp['local_dir_exists'] and not insp['problems']
        rid = 'mr_' + secrets.token_hex(6)
        weight = insp['files'].get('model.safetensors', {})
        tok = insp['files'].get('tokenizer.json', {})
        cfg = insp.get('config') or {}
        estimate = est or (weight.get('bytes') * (1 if precision in ('bfloat16', 'float16') or cfg.get('dtype') in ('bfloat16', 'float16') else 2) if weight.get('bytes') else None)
        db.execute('INSERT INTO model_revisions (id, model_id, revision, hub_repo, local_dir, architecture, loader, weight_format, weight_digest, tokenizer_digest, config_json, license, '
                   'operations_json, context_limit, embedding_dim, pooling, precision, resource_estimate_bytes, status, installed, install_json, description, registered_by, created_at) '
                   'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   (rid, model_id, rev, repo, str(local_dir(self.settings, repo, rev)), insp['architecture'] or 'unknown', loader or 'none', insp['weight_format'] or 'none',
                    weight.get('sha256'), tok.get('sha256'), json.dumps(cfg), lic, json.dumps(ops), ctx or cfg.get('max_position_embeddings') or insp.get('max_seq_length'),
                    insp.get('embedding_dim'), insp.get('pooling'), precision, estimate, 'registered', int(installed), json.dumps(insp), desc, principal.id, now()))
        history.record(db, principal.workspace, principal.id, 'model.registered', 'model', rid, {'model_id': model_id, 'revision': rev, 'hub_repo': repo, 'installed': installed, 'problems': insp['problems']})
        return self.view(self.row(db, rid), db)

    def recheck_install(self, db, principal, rid):
        """Re-inspect the artifact directory (after the operator installed the pinned files)."""
        principal.require('model:admin')
        row = self.row(db, rid)
        insp = inspect_artifact(self.settings, row['hub_repo'], row['revision'])
        installed = insp['local_dir_exists'] and not insp['problems']
        weight = insp['files'].get('model.safetensors', {})
        if row['weight_digest'] and weight.get('sha256') and weight['sha256'] != row['weight_digest']:
            insp['problems'].append('weight digest changed since registration; the revision identity is immutable and this artifact is refused')
            installed = False
        db.execute('UPDATE model_revisions SET installed=?, install_json=?, weight_digest=COALESCE(weight_digest, ?), tokenizer_digest=COALESCE(tokenizer_digest, ?), '
                   'architecture=CASE WHEN architecture=\'unknown\' THEN ? ELSE architecture END, loader=CASE WHEN loader=\'none\' THEN ? ELSE loader END WHERE id=?',
                   (int(installed), json.dumps(insp), weight.get('sha256'), insp['files'].get('tokenizer.json', {}).get('sha256'), insp['architecture'] or 'unknown', insp['loader'] or 'none', rid))
        history.record(db, principal.workspace, principal.id, 'model.install_checked', 'model', rid, {'installed': installed, 'problems': insp['problems']})
        return self.view(self.row(db, rid), db)

    def row(self, db, rid):
        row = db.execute('SELECT * FROM model_revisions WHERE id=?', (rid,)).fetchone()
        if row is None:
            raise ServiceError('NOT_FOUND', 'model revision')
        return row

    def resolve(self, db, operation, revision_id=None):
        """The revision a new request binds: an explicit registered revision or the promoted default for the operation.
        Retired/revoked revisions refuse new execution (history stays readable)."""
        if revision_id is not None:
            if type(revision_id) is not str:
                raise ServiceError('VALIDATION', 'model_revision_id')
            row = self.row(db, revision_id)
        else:
            d = db.execute('SELECT revision_id FROM model_defaults WHERE operation=?', (operation,)).fetchone()
            if d is None:
                raise ServiceError('CAPABILITY_UNAVAILABLE', {'code': 'no_default_model', 'operation': operation, 'action': 'register and promote a model revision for this operation'})
            row = self.row(db, d['revision_id'])
        if row['status'] != 'registered':
            raise ServiceError('CONFLICT', {'code': 'model_' + row['status'], 'revision_id': row['id'], 'note': 'no new execution; historical results keep their metadata'})
        if operation not in json.loads(row['operations_json']):
            raise ServiceError('VALIDATION', {'code': 'operation_not_supported', 'revision_id': row['id'], 'operations': json.loads(row['operations_json'])})
        if not row['installed']:
            raise ServiceError('CAPABILITY_UNAVAILABLE', {'code': 'model_not_installed', 'revision_id': row['id'], 'problems': json.loads(row['install_json'] or '{}').get('problems')})
        return row

    # ---- lifecycle -------------------------------------------------------------------------------
    def promote(self, db, principal, rid, operation, evidence=None):
        principal.require('model:admin')
        row = self.row(db, rid)
        if operation not in OPERATIONS or operation not in json.loads(row['operations_json']):
            raise ServiceError('VALIDATION', 'operation')
        if row['status'] != 'registered' or not row['installed']:
            raise ServiceError('CONFLICT', 'only an installed, registered revision can become the default')
        if evidence is not None and (type(evidence) is not dict or len(json.dumps(evidence)) > 8192):
            raise ServiceError('VALIDATION', 'evidence')
        prev = db.execute('SELECT revision_id FROM model_defaults WHERE operation=?', (operation,)).fetchone()
        prev_id = prev['revision_id'] if prev else None
        if prev_id == rid:
            return self.view(row, db)
        db.execute('INSERT INTO model_defaults (operation, revision_id, set_by, evidence_json, previous_revision_id, updated_at) VALUES (?,?,?,?,?,?) '
                   'ON CONFLICT(operation) DO UPDATE SET revision_id=excluded.revision_id, set_by=excluded.set_by, evidence_json=excluded.evidence_json, previous_revision_id=excluded.previous_revision_id, updated_at=excluded.updated_at',
                   (operation, rid, principal.id, json.dumps(evidence or {}), prev_id, now()))
        pid = 'mp_' + secrets.token_hex(6)
        db.execute('INSERT INTO model_promotions (id, operation, from_revision_id, to_revision_id, principal_id, evidence_json, action, created_at) VALUES (?,?,?,?,?,?,?,?)',
                   (pid, operation, prev_id, rid, principal.id, json.dumps(evidence or {}), 'promote', now()))
        history.record(db, principal.workspace, principal.id, 'model.promoted', 'model', rid, {'operation': operation, 'previous': prev_id, 'promotion_id': pid, 'evidence_keys': sorted(evidence or {})})
        return self.view(self.row(db, rid), db)

    def rollback_default(self, db, principal, operation):
        """Move the default pointer back to the previous revision (if still executable). Not a database rollback."""
        principal.require('model:admin')
        d = db.execute('SELECT * FROM model_defaults WHERE operation=?', (operation,)).fetchone()
        if d is None or d['previous_revision_id'] is None:
            raise ServiceError('CONFLICT', 'no previous default recorded for this operation')
        prev = self.row(db, d['previous_revision_id'])
        if prev['status'] != 'registered' or not prev['installed']:
            raise ServiceError('CONFLICT', {'code': 'previous_default_not_executable', 'status': prev['status'], 'installed': bool(prev['installed'])})
        pid = 'mp_' + secrets.token_hex(6)
        db.execute('UPDATE model_defaults SET revision_id=?, set_by=?, previous_revision_id=?, updated_at=? WHERE operation=?', (prev['id'], principal.id, d['revision_id'], now(), operation))
        db.execute('INSERT INTO model_promotions (id, operation, from_revision_id, to_revision_id, principal_id, evidence_json, action, created_at) VALUES (?,?,?,?,?,?,?,?)',
                   (pid, operation, d['revision_id'], prev['id'], principal.id, '{}', 'rollback_default', now()))
        history.record(db, principal.workspace, principal.id, 'model.promoted', 'model', prev['id'], {'operation': operation, 'rollback_from': d['revision_id'], 'promotion_id': pid})
        return self.view(prev, db)

    def retire(self, db, principal, rid, reason='', revoke=False):
        principal.require('model:admin')
        row = self.row(db, rid)
        if type(reason) is not str or len(reason) > 256:
            raise ServiceError('VALIDATION', 'reason')
        status = 'revoked' if revoke else 'retired'
        if row['status'] == 'revoked' or (row['status'] == 'retired' and not revoke):
            return self.view(row, db)
        db.execute('UPDATE model_revisions SET status=?, retired_at=COALESCE(retired_at, ?), revoked_at=?, revocation_reason=? WHERE id=?',
                   (status, now(), now() if revoke else None, reason if revoke else None, rid))
        for d in db.execute('SELECT operation FROM model_defaults WHERE revision_id=?', (rid,)).fetchall():
            db.execute('DELETE FROM model_defaults WHERE operation=?', (d['operation'],))      # no silent redirect: the operation has no default until promoted again
        history.record(db, principal.workspace, principal.id, 'model.retired', 'model', rid, {'status': status, 'reason': reason, 'defaults_cleared': True})
        return self.view(self.row(db, rid), db)

    # ---- views ----------------------------------------------------------------------------------
    def view(self, row, db=None, private=True):
        insp = json.loads(row['install_json'] or '{}')
        arch = ARCHITECTURES.get(row['architecture'], {})
        out = {'id': row['id'], 'model_id': row['model_id'], 'revision': row['revision'], 'hub_repo': row['hub_repo'], 'architecture': row['architecture'], 'loader': row['loader'],
               'weight_format': row['weight_format'], 'weight_digest': row['weight_digest'], 'tokenizer_digest': row['tokenizer_digest'], 'license': row['license'],
               'operations': json.loads(row['operations_json']), 'context_limit': row['context_limit'], 'embedding_dim': row['embedding_dim'], 'pooling': row['pooling'],
               'precision': row['precision'], 'resource_estimate_bytes': row['resource_estimate_bytes'], 'status': row['status'], 'installed': bool(row['installed']),
               'install_problems': insp.get('problems'), 'prompt_format': arch.get('prompt_format'), 'family': arch.get('family'), 'description': row['description'],
               'created_at': row['created_at'], 'retired_at': row['retired_at'], 'revoked_at': row['revoked_at'], 'revocation_reason': row['revocation_reason'],
               'trust': 'operator-registered pinned artifact loaded by the trusted in-tree loader; no remote code; identity = hub repo + commit + weight digest'}
        if private:
            out['config'] = json.loads(row['config_json'] or '{}')
        if db is not None:
            out['default_for'] = [r['operation'] for r in db.execute('SELECT operation FROM model_defaults WHERE revision_id=?', (row['id'],)).fetchall()]
            out['runtimes'] = [runtime_view(r) for r in db.execute('SELECT * FROM model_runtimes WHERE revision_id=? ORDER BY host', (row['id'],)).fetchall()]
            out['callable'] = row['status'] == 'registered' and bool(row['installed']) and any(r['state'] == 'ready' for r in out['runtimes'])
            out['readiness'] = ('ready on ' + ','.join(r['host'] for r in out['runtimes'] if r['state'] == 'ready')) if out['callable'] else (
                'installed; no runtime has loaded it yet (a worker loads it on first request or on operator load)' if row['installed'] and row['status'] == 'registered' else 'not callable')
        return out

    def list(self, db, principal, include_retired=True):
        principal.require('job:read')
        rows = db.execute('SELECT * FROM model_revisions ORDER BY model_id, created_at').fetchall()
        return [self.view(r, db, private=principal.can('model:admin')) for r in rows if include_retired or r['status'] == 'registered']

    def detail(self, db, principal, rid):
        principal.require('job:read')
        return self.view(self.row(db, rid), db, private=principal.can('model:admin'))

    def promotions(self, db, principal):
        principal.require('job:read')
        return [dict(r, evidence=json.loads(r['evidence_json'] or '{}')) for r in db.execute('SELECT id, operation, from_revision_id, to_revision_id, principal_id, action, created_at, evidence_json FROM model_promotions ORDER BY created_at DESC LIMIT 100').fetchall()]


def runtime_view(r):
    return {'host': r['host'], 'revision_id': r['revision_id'], 'state': r['state'], 'device': r['device'], 'dtype': r['dtype'], 'estimated_bytes': r['estimated_bytes'],
            'loaded_at': r['loaded_at'], 'load_ms': r['load_ms'], 'last_used_at': r['last_used_at'], 'requests': r['requests'], 'desired': r['desired'], 'error': r['error'],
            'versions': json.loads(r['versions_json']) if r['versions_json'] else None, 'updated_at': r['updated_at']}
