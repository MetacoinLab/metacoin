"""Operator functions: status, backup, restore, migrations, retention cleanup, credentials, keys."""
import json
import os
import shutil
import sqlite3
from pathlib import Path
from . import auth, crypto, db as database, history
from .db import now
from .errors import ServiceError

BACKUP_SCHEMA = 'metacoin-service-backup/v1'


def status(settings):
    out = {'home': str(settings.home), 'db_exists': settings.db_path.exists(), 'provider_mode': settings.provider_mode}
    if not settings.db_path.exists():
        return dict(out, initialized=False)
    D = database.Database(settings.db_path)
    with D.read() as db:
        out['schema'] = database.schema_version(settings.db_path)
        out['jobs_by_state'] = {r['state']: r['n'] for r in db.execute('SELECT state, COUNT(*) AS n FROM jobs GROUP BY state')}
        out['review_by_state'] = {r['review_state']: r['n'] for r in db.execute('SELECT review_state, COUNT(*) AS n FROM jobs GROUP BY review_state')}
        out['sales_by_state'] = {r['state']: r['n'] for r in db.execute('SELECT state, COUNT(*) AS n FROM sales GROUP BY state')}
        out['artifacts'] = {'total': db.execute('SELECT COUNT(*) FROM artifacts').fetchone()[0],
                            'payload_deleted': db.execute('SELECT COUNT(*) FROM artifacts WHERE deleted_at IS NOT NULL').fetchone()[0]}
        out['reconciliation_gate'] = db.execute("SELECT value FROM meta WHERE key='reconciliation_gate'").fetchone()[0]
        out['workspaces'] = [dict(r) for r in db.execute('SELECT * FROM campaigns')]
        out['history_chains'] = {w['workspace']: history.verify_chain(db, w['workspace']) for w in out['workspaces']}
    if settings.journal_path.exists():
        from experiments.work_contracts.execution_state import Journal
        out['journal'] = {w['workspace']: Journal(settings.journal_path, w['campaign_id'], w['cap']).inspect()['exposure_by_state'] for w in out['workspaces']}
    pid = settings.run_dir / 'api.pid'
    out['api_pid_file'] = str(pid) if pid.exists() else None
    return out


def backup(settings, dest, include_keys=False):
    """Consistent SQLite backup (backup API), artifacts copy, manifest declaring contents.
    Keys are excluded unless explicitly requested; encrypted artifacts need them to restore."""
    dest = Path(dest)
    if dest.exists():
        raise ServiceError('CONFLICT', 'backup destination exists')
    dest.mkdir(mode=0o700, parents=True)
    for name in ('service.sqlite', 'journal.sqlite'):
        src = settings.home / name
        if src.exists():
            source = sqlite3.connect(src)
            target = sqlite3.connect(dest / name)
            with target:
                source.backup(target)
            target.close(); source.close()
            os.chmod(dest / name, 0o600)
    (dest / 'artifacts').mkdir(mode=0o700)
    count = 0
    for f in Path(settings.artifacts_dir).glob('*'):
        if f.is_file():
            shutil.copy2(f, dest / 'artifacts' / f.name); os.chmod(dest / 'artifacts' / f.name, 0o600); count += 1
    if include_keys:
        (dest / 'keys').mkdir(mode=0o700)
        copy_key_tree(Path(settings.keys_dir), dest / 'keys')
    D0 = database.Database(settings.db_path)
    with D0.read() as db:
        def _n(sql):
            try:
                return db.execute(sql).fetchone()[0]
            except Exception:
                return None
        inventory = {'model_revisions': [dict(r) for r in db.execute('SELECT id, model_id, revision, hub_repo, weight_digest, status, installed FROM model_revisions').fetchall()] if _n('SELECT COUNT(*) FROM model_revisions') is not None else [],
                     'knowledge_indexes': _n('SELECT COUNT(*) FROM knowledge_indexes'), 'knowledge_versions': _n('SELECT COUNT(*) FROM knowledge_versions'), 'calibration_models': _n('SELECT COUNT(*) FROM calibration_models'),
                     'verification_records': _n('SELECT COUNT(*) FROM verification_jobs'), 'nodes': [dict(r) for r in db.execute('SELECT id, name, state, public_key_hex FROM nodes').fetchall()] if _n('SELECT COUNT(*) FROM nodes') is not None else [],
                     'approvals': _n('SELECT COUNT(*) FROM approvals')}
    manifest = {'schema': BACKUP_SCHEMA, 'created_at': now(), 'inventory': inventory,
                'classes': {'portable_source': 'not in this backup (git export)', 'operational_state': 'service.sqlite + journal.sqlite', 'encrypted_payloads': 'artifacts/ (age ciphertext: documents, indexes, checkpoints, outputs, calibration rows)',
                            'secret_material': 'keys/ only with --include-keys; node credentials are hashes only (the nodes keep their private keys); model weights are NOT included (pinned manifests only)'},
                'contains': {
        'service_database': 'sensitive metadata (principal names, credential hashes, contract terms, digests, events)',
        'journal_database': 'economic authorization state (a restored copy must be reconciled, not resumed)',
        'artifacts': str(count) + ' ciphertext objects (age) and public bundles',
        'keys': 'INCLUDED: decryption identity and signing keys' if include_keys else 'EXCLUDED: encrypted artifacts cannot be read without the matching key material'},
        'schema_migrations': database.schema_version(settings.db_path)}
    (dest / 'BACKUP_MANIFEST.json').write_text(json.dumps(manifest, indent=2))
    D = database.Database(settings.db_path)
    with D.tx() as db:
        for w in db.execute('SELECT workspace FROM campaigns'):
            history.record(db, w['workspace'], 'operator', 'backup.created', 'service', 'backup', {'keys_included': include_keys, 'artifacts': count})
    return manifest


def restore(backup_dir, settings, keys_dir=None):
    """Restore into a FRESH home. Structural checks run before any write; the
    reconciliation gate is set so no payment action resumes automatically."""
    backup_dir, home = Path(backup_dir), Path(settings.home)
    if home.exists() and any(home.iterdir()):
        raise ServiceError('CONFLICT', 'restore destination must be a fresh empty directory')
    manifest = json.loads((backup_dir / 'BACKUP_MANIFEST.json').read_text())
    if manifest.get('schema') != BACKUP_SCHEMA:
        raise ServiceError('PACKAGE_VERSION', 'backup manifest schema')
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    for d in (settings.keys_dir, settings.artifacts_dir, settings.credentials_dir, settings.logs_dir, settings.run_dir):
        Path(d).mkdir(mode=0o700, exist_ok=True)
    shutil.copy2(backup_dir / 'service.sqlite', settings.db_path); os.chmod(settings.db_path, 0o600)
    applied = database.check_schema(settings.db_path, allow_older=True)      # structural + version check before opening for writes; an older (pre-upgrade) backup is accepted and reported as pending migrations
    pending = [name for name, _ in database.MIGRATIONS if name not in set(applied)]
    if (backup_dir / 'journal.sqlite').exists():
        shutil.copy2(backup_dir / 'journal.sqlite', settings.journal_path); os.chmod(settings.journal_path, 0o600)
    for f in (backup_dir / 'artifacts').glob('*'):
        shutil.copy2(f, settings.artifacts_dir / f.name); os.chmod(settings.artifacts_dir / f.name, 0o600)
    source_keys = Path(keys_dir) if keys_dir else (backup_dir / 'keys' if (backup_dir / 'keys').exists() else None)
    keys_restored = False
    if source_keys is not None and source_keys.exists():
        copy_key_tree(source_keys, Path(settings.keys_dir))
        keys_restored = True
    D = database.Database(settings.db_path)
    with D.tx() as db:
        db.execute("UPDATE meta SET value='1' WHERE key='reconciliation_gate'")
        for w in db.execute('SELECT workspace FROM campaigns'):
            history.record(db, w['workspace'], 'operator', 'restore.completed', 'service', 'restore',
                           {'keys_restored': keys_restored, 'reconciliation_gate': True})
    recovery = model_index_recovery(settings)
    return {'restored_to': str(home), 'keys_restored': keys_restored, 'schema': applied[-1] if applied else None, 'pending_migrations': pending,
            'note': 'key material absent: encrypted artifacts unreadable until the identity is restored' if not keys_restored else 'ok',
            'reconciliation_gate': 'set; run reconcile on unresolved actions before any new dispatch', 'models_and_indexes': recovery}


def model_index_recovery(settings):
    """After a restore: which model revisions lack weights on this host (nothing is downloaded; the operator installs the
    pinned artifact and runs recheck), which indexes have their encrypted artifact, and that no node enrolls itself."""
    from .models import registry as registry_mod
    D = database.Database(settings.db_path)
    out = {'model_revisions': [], 'knowledge_indexes': [], 'nodes': 'enrolled node rows restored; none re-registered automatically; credentials unchanged (hashes)', 'policy': 'no automatic download; no index rebuild without an authorized index request'}
    with D.tx() as db:
        try:
            rows = db.execute('SELECT * FROM model_revisions').fetchall()
        except Exception:
            return out
        for r in rows:
            insp = registry_mod.inspect_artifact(settings, r['hub_repo'], r['revision'])
            present = insp['local_dir_exists'] and not insp['problems'] and (insp['files'].get('model.safetensors', {}).get('sha256') == r['weight_digest'] or r['weight_digest'] is None)
            db.execute('UPDATE model_revisions SET installed=? WHERE id=?', (int(present), r['id']))
            db.execute("UPDATE model_runtimes SET state='unloaded', pid=NULL WHERE revision_id=?", (r['id'],))
            out['model_revisions'].append({'id': r['id'], 'model_id': r['model_id'], 'weights_present': present, 'problems': insp['problems'][:2], 'action': None if present else 'install the pinned artifact under the model store, then POST /api/v1/models/{id}/recheck'})
        for i in db.execute('SELECT id, state, vectors_artifact_id FROM knowledge_indexes').fetchall():
            art = db.execute('SELECT deleted_at, storage_name FROM artifacts WHERE id=?', (i['vectors_artifact_id'],)).fetchone() if i['vectors_artifact_id'] else None
            payload = bool(art) and art['deleted_at'] is None and (Path(settings.artifacts_dir) / art['storage_name']).exists()
            out['knowledge_indexes'].append({'id': i['id'], 'state': i['state'], 'payload_present': payload, 'action': None if payload or i['state'] != 'ready' else 'build a new index version (a rebuilt index gets a new identity)'})
    return out


def rehearse_recovery(settings, dest):
    """§65-10: bounded backup -> restore -> read-only verification rehearsal in an isolated directory. The running
    service is not touched (backup uses the SQLite backup API; nothing is started, resubmitted or enrolled)."""
    from . import config as config_mod
    dest = Path(dest)
    if dest.exists() and any(dest.iterdir()):
        raise ServiceError('CONFLICT', 'rehearsal directory must be empty')
    dest.mkdir(mode=0o700, parents=True, exist_ok=True)
    t0 = now()
    manifest = backup(settings, dest / 'backup', include_keys=False)
    restored = config_mod.Settings(home=dest / 'restored-home', provider_mode=settings.provider_mode, compute_python=settings.compute_python, model_store=settings.model_store)
    result = restore(dest / 'backup', restored, keys_dir=str(settings.keys_dir))
    D = database.Database(restored.db_path)
    with D.read() as db:
        checks = {'schema': database.schema_version(restored.db_path)[-1], 'integrity': db.execute('PRAGMA integrity_check').fetchone()[0], 'foreign_keys': len(db.execute('PRAGMA foreign_key_check').fetchall()),
                  'jobs': db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0], 'artifacts_present': 0, 'artifacts_missing': [], 'unresolved_actions': db.execute("SELECT COUNT(*) FROM sales WHERE state IN ('SUBMISSION_PENDING','OUTCOME_UNKNOWN')").fetchone()[0],
                  'reconciliation_gate': db.execute("SELECT value FROM meta WHERE key='reconciliation_gate'").fetchone()[0], 'history_chains': {w['workspace']: history.verify_chain(db, w['workspace']) for w in db.execute('SELECT workspace FROM campaigns')}}
        for a in db.execute('SELECT id, storage_name, deleted_at FROM artifacts').fetchall():
            if a['deleted_at'] is None:
                if (Path(restored.artifacts_dir) / a['storage_name']).exists():
                    checks['artifacts_present'] += 1
                else:
                    checks['artifacts_missing'].append(a['id'])
        checks['artifacts_missing'] = checks['artifacts_missing'][:20]
        # decryptability of one private artifact with the restored keys (read-only)
        from .artifacts import ArtifactStore
        store = ArtifactStore(restored)
        row = db.execute("SELECT id, workspace FROM artifacts WHERE encrypted=1 AND deleted_at IS NULL ORDER BY created_at LIMIT 1").fetchone()
        try:
            checks['sample_artifact_decrypts'] = bool(store.load(db, row['id'], row['workspace'])) if row else None
        except Exception as exc:
            checks['sample_artifact_decrypts'] = False; checks['decrypt_error'] = type(exc).__name__
    missing_keys = [k for k in ('service.age', 'service.ed25519') if not (Path(restored.keys_dir) / k).exists()]
    out = {'rehearsal_dir': str(dest), 'seconds': now() - t0, 'backup_inventory': manifest.get('inventory'), 'restore': {k: result[k] for k in ('keys_restored', 'reconciliation_gate')}, 'checks': checks,
           'missing_keys': missing_keys, 'models_and_indexes': result.get('models_and_indexes'), 'running_service': 'untouched (no process started, no payment resubmitted, no node enrolled)',
           'scope': 'one local rehearsal on this host; not evidence of disaster recovery across machines, keys and external settlement history'}
    (dest / 'REHEARSAL.json').write_text(json.dumps(out, indent=1, default=str))
    return out


def copy_key_tree(src, dst):
    """Copy key material recursively (the node TLS CA lives in a subdirectory); every copied file is 0600, directories 0700."""
    dst.mkdir(parents=True, exist_ok=True); os.chmod(dst, 0o700)
    for f in Path(src).iterdir():
        if f.is_dir():
            copy_key_tree(f, dst / f.name)
        else:
            shutil.copy2(f, dst / f.name); os.chmod(dst / f.name, 0o600)


def clear_gate(settings):
    D = database.Database(settings.db_path)
    with D.tx() as db:
        db.execute("UPDATE meta SET value='0' WHERE key='reconciliation_gate'")


def cleanup(settings, at=None):
    """Bounded retention cleanup: unlink ciphertext of private artifacts past their deadline,
    keeping the integrity record. Skips artifacts required by running jobs."""
    from .artifacts import ArtifactStore
    store = ArtifactStore(settings)
    D = database.Database(settings.db_path)
    at = at or now()
    removed, skipped = [], []
    with D.tx() as db:
        rows = db.execute('SELECT id, workspace FROM artifacts WHERE public=0 AND deleted_at IS NULL AND retention_deadline IS NOT NULL '
                          'AND retention_deadline <= ? LIMIT 500', (at,)).fetchall()
        held = set()
        try:
            from .economy.access import Access
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='evidence_holds'").fetchone():
                held = Access.held_artifact_ids(db)
        except Exception:
            held = set()
        for r in rows:
            if r['id'] in held:
                skipped.append({'id': r['id'], 'code': 'HELD_BY_DISPUTE'}); continue
            try:
                if store.delete_payload(db, r['id'], r['workspace'], 'retention'):
                    removed.append(r['id'])
                    history.record(db, r['workspace'], 'retention', 'artifact.deleted', 'artifact', r['id'], {'retention': True})
            except ServiceError as exc:
                skipped.append({'id': r['id'], 'code': exc.code})
        for w in db.execute('SELECT workspace FROM campaigns'):
            history.record(db, w['workspace'], 'retention', 'retention.cleanup', 'service', 'cleanup', {'removed': len(removed), 'skipped': len(skipped)})
    return {'removed': removed, 'skipped': skipped}


def rotate_reviewer_key(settings, principal_id):
    D = database.Database(settings.db_path)
    with D.tx() as db:
        old = db.execute("SELECT key_id FROM reviewer_keys WHERE principal_id=? AND status='active'", (principal_id,)).fetchone()
        if old:
            db.execute("UPDATE reviewer_keys SET status='rotated', status_changed_at=? WHERE key_id=?", (now(), old['key_id']))
        current = settings.keys_dir / ('reviewer-' + principal_id + '.ed25519')
        if current.exists():
            # keep the superseded private key under its key id (historical verification needs only the public key)
            latest = db.execute('SELECT key_id FROM reviewer_keys WHERE principal_id=? ORDER BY created_at DESC LIMIT 1', (principal_id,)).fetchone()
            os.replace(current, settings.keys_dir / ('reviewer-' + principal_id + '.ed25519.' + (latest['key_id'] if latest else 'old')))
        pub = crypto.generate_signing_key(settings.keys_dir / ('reviewer-' + principal_id + '.ed25519'))
        key_id = crypto.key_id_for(pub)
        db.execute('INSERT INTO reviewer_keys VALUES (?,?,?,?,?,NULL)', (key_id, principal_id, pub, 'active', now()))
        ws = db.execute('SELECT workspace FROM principals WHERE id=?', (principal_id,)).fetchone()['workspace']
        history.record(db, ws, 'operator', 'key.rotated', 'principal', principal_id, {'new_key_id': key_id, 'old_key_id': old['key_id'] if old else None})
    return key_id


def revoke_reviewer_key(settings, key_id):
    D = database.Database(settings.db_path)
    with D.tx() as db:
        row = db.execute('SELECT * FROM reviewer_keys WHERE key_id=?', (key_id,)).fetchone()
        if row is None:
            raise ServiceError('NOT_FOUND', 'key')
        db.execute("UPDATE reviewer_keys SET status='revoked', status_changed_at=? WHERE key_id=?", (now(), key_id))
        ws = db.execute('SELECT workspace FROM principals WHERE id=?', (row['principal_id'],)).fetchone()['workspace']
        history.record(db, ws, 'operator', 'key.revoked', 'principal', row['principal_id'], {'key_id': key_id})


def rotate_credential(settings, credential_id):
    D = database.Database(settings.db_path)
    with D.tx() as db:
        cid, token = auth.rotate_credential(db, credential_id, settings.limits['credential_seconds'])
        pid = db.execute('SELECT principal_id FROM credentials WHERE id=?', (cid,)).fetchone()['principal_id']
        ws = db.execute('SELECT workspace FROM principals WHERE id=?', (pid,)).fetchone()['workspace']
        history.record(db, ws, 'operator', 'credential.issued', 'principal', pid, {'credential_id': cid, 'rotated_from': credential_id})
        history.record(db, ws, 'operator', 'credential.revoked', 'principal', pid, {'credential_id': credential_id})
    path = settings.credentials_dir / ('rotated-' + cid + '.json')
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump({'credential_id': cid, 'principal_id': pid, 'token': token}, stream)
    return {'credential_id': cid, 'written_to': str(path)}


def revoke_credential(settings, credential_id):
    D = database.Database(settings.db_path)
    with D.tx() as db:
        auth.revoke_credential(db, credential_id)
