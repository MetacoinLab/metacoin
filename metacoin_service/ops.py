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
        for f in Path(settings.keys_dir).glob('*'):
            shutil.copy2(f, dest / 'keys' / f.name); os.chmod(dest / 'keys' / f.name, 0o600)
    manifest = {'schema': BACKUP_SCHEMA, 'created_at': now(), 'contains': {
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
    database.check_schema(settings.db_path)      # structural + version check before opening for writes
    if (backup_dir / 'journal.sqlite').exists():
        shutil.copy2(backup_dir / 'journal.sqlite', settings.journal_path); os.chmod(settings.journal_path, 0o600)
    for f in (backup_dir / 'artifacts').glob('*'):
        shutil.copy2(f, settings.artifacts_dir / f.name); os.chmod(settings.artifacts_dir / f.name, 0o600)
    source_keys = Path(keys_dir) if keys_dir else (backup_dir / 'keys' if (backup_dir / 'keys').exists() else None)
    keys_restored = False
    if source_keys is not None and source_keys.exists():
        for f in source_keys.glob('*'):
            shutil.copy2(f, settings.keys_dir / f.name); os.chmod(settings.keys_dir / f.name, 0o600)
        keys_restored = True
    D = database.Database(settings.db_path)
    with D.tx() as db:
        db.execute("UPDATE meta SET value='1' WHERE key='reconciliation_gate'")
        for w in db.execute('SELECT workspace FROM campaigns'):
            history.record(db, w['workspace'], 'operator', 'restore.completed', 'service', 'restore',
                           {'keys_restored': keys_restored, 'reconciliation_gate': True})
    return {'restored_to': str(home), 'keys_restored': keys_restored,
            'note': 'key material absent: encrypted artifacts unreadable until the identity is restored' if not keys_restored else 'ok',
            'reconciliation_gate': 'set; run reconcile on unresolved actions before any new dispatch'}


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
        for r in rows:
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
