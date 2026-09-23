"""SQLite persistence with explicit migrations, WAL, foreign keys, busy handling and
BEGIN IMMEDIATE transactions. One connection per transaction; never shared across threads."""
from contextlib import contextmanager
import os
import sqlite3
import stat
import time

MIGRATIONS = [
    ('001_initial', """
    CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE principals (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, role TEXT NOT NULL CHECK (role IN ('owner','worker','reviewer','viewer')),
        workspace TEXT NOT NULL, created_at INTEGER NOT NULL, revoked_at INTEGER);
    CREATE INDEX principals_ws ON principals(workspace);
    CREATE TABLE credentials (
        id TEXT PRIMARY KEY, principal_id TEXT NOT NULL REFERENCES principals(id),
        kind TEXT NOT NULL CHECK (kind IN ('api','session')), secret_hash TEXT UNIQUE NOT NULL,
        scope TEXT, created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL, revoked_at INTEGER);
    CREATE INDEX credentials_principal ON credentials(principal_id);
    CREATE TABLE sessions (
        id TEXT PRIMARY KEY, principal_id TEXT NOT NULL REFERENCES principals(id), csrf TEXT NOT NULL,
        created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL, revoked_at INTEGER);
    CREATE TABLE contracts (
        id TEXT PRIMARY KEY, workspace TEXT NOT NULL, owner_id TEXT NOT NULL REFERENCES principals(id),
        kind TEXT NOT NULL, state TEXT NOT NULL CHECK (state IN ('draft','frozen')),
        version INTEGER NOT NULL DEFAULT 1, lineage_id TEXT NOT NULL, previous_id TEXT REFERENCES contracts(id),
        title TEXT NOT NULL, policy_json TEXT NOT NULL, params_json TEXT NOT NULL,
        input_artifact_id TEXT, contract_json TEXT, contract_digest TEXT UNIQUE, input_root TEXT,
        reviewer_id TEXT REFERENCES principals(id), expires_at INTEGER, created_at INTEGER NOT NULL, frozen_at INTEGER);
    CREATE INDEX contracts_ws ON contracts(workspace, created_at);
    CREATE TABLE jobs (
        id TEXT PRIMARY KEY, workspace TEXT NOT NULL, contract_id TEXT NOT NULL REFERENCES contracts(id),
        kind TEXT NOT NULL, state TEXT NOT NULL CHECK (state IN ('queued','running','succeeded','failed','cancelled')),
        attempt INTEGER NOT NULL DEFAULT 0, lease_owner TEXT, lease_expires INTEGER, lease_generation INTEGER NOT NULL DEFAULT 0,
        retries_left INTEGER NOT NULL, cancel_requested INTEGER NOT NULL DEFAULT 0,
        evidence_artifact_id TEXT, evidence_root TEXT, outcome TEXT, summary_json TEXT, error_code TEXT,
        review_state TEXT NOT NULL DEFAULT 'none' CHECK (review_state IN ('none','requested','accepted','rejected')),
        submitted_by TEXT NOT NULL REFERENCES principals(id), created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
        finished_at INTEGER);
    CREATE INDEX jobs_ws ON jobs(workspace, created_at);
    CREATE INDEX jobs_queue ON jobs(state, created_at);
    CREATE TABLE attempts (
        id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id), generation INTEGER NOT NULL,
        worker_id TEXT NOT NULL, started_at INTEGER NOT NULL, finished_at INTEGER, outcome TEXT);
    CREATE TABLE artifacts (
        id TEXT PRIMARY KEY, workspace TEXT NOT NULL, kind TEXT NOT NULL, contract_id TEXT REFERENCES contracts(id),
        job_id TEXT REFERENCES jobs(id), owner_id TEXT NOT NULL REFERENCES principals(id),
        storage_name TEXT UNIQUE, encrypted INTEGER NOT NULL, format_version TEXT NOT NULL,
        sha256_ciphertext TEXT, sha256_plaintext TEXT NOT NULL, size_plaintext INTEGER NOT NULL,
        recipients_json TEXT NOT NULL, intended_use TEXT NOT NULL, public INTEGER NOT NULL DEFAULT 0,
        retention_deadline INTEGER, deleted_at INTEGER, created_at INTEGER NOT NULL);
    CREATE INDEX artifacts_job ON artifacts(job_id);
    CREATE TABLE reviewer_keys (
        key_id TEXT PRIMARY KEY, principal_id TEXT NOT NULL REFERENCES principals(id), public_key_hex TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('active','rotated','revoked')), created_at INTEGER NOT NULL,
        status_changed_at INTEGER);
    CREATE TABLE reviews (
        id TEXT PRIMARY KEY, workspace TEXT NOT NULL, job_id TEXT NOT NULL REFERENCES jobs(id),
        reviewer_id TEXT NOT NULL REFERENCES principals(id), key_id TEXT NOT NULL REFERENCES reviewer_keys(key_id),
        decision TEXT NOT NULL CHECK (decision IN ('accepted','rejected')), envelope_json TEXT NOT NULL,
        envelope_digest TEXT UNIQUE NOT NULL, signature_hex TEXT NOT NULL, public_bundle_artifact_id TEXT,
        created_at INTEGER NOT NULL);
    CREATE TABLE events (
        seq INTEGER PRIMARY KEY AUTOINCREMENT, workspace TEXT NOT NULL, ts INTEGER NOT NULL, actor_id TEXT NOT NULL,
        event_type TEXT NOT NULL, category TEXT NOT NULL CHECK (category IN ('scientific','administrative','economic')),
        object_type TEXT NOT NULL, object_id TEXT NOT NULL, ref_json TEXT NOT NULL, prev_hash TEXT NOT NULL, hash TEXT NOT NULL);
    CREATE INDEX events_ws ON events(workspace, seq);
    CREATE TABLE idempotency (
        principal_id TEXT NOT NULL, operation TEXT NOT NULL, key TEXT NOT NULL, request_digest TEXT NOT NULL,
        status INTEGER NOT NULL, response_json TEXT NOT NULL, created_at INTEGER NOT NULL,
        PRIMARY KEY (principal_id, operation, key));
    CREATE TABLE campaigns (
        workspace TEXT PRIMARY KEY, campaign_id TEXT NOT NULL, cap INTEGER NOT NULL, asset TEXT NOT NULL,
        network TEXT NOT NULL, unit TEXT NOT NULL);
    CREATE TABLE payment_actions (
        request_id TEXT PRIMARY KEY, workspace TEXT NOT NULL, job_id TEXT NOT NULL REFERENCES jobs(id),
        provider_mode TEXT NOT NULL, request_json TEXT NOT NULL, created_by TEXT NOT NULL, created_at INTEGER NOT NULL);
    CREATE TABLE sales (
        payment_id TEXT PRIMARY KEY, workspace TEXT NOT NULL, job_id TEXT NOT NULL REFERENCES jobs(id),
        resource TEXT NOT NULL, amount TEXT NOT NULL, asset TEXT NOT NULL, network TEXT NOT NULL, pay_to TEXT NOT NULL,
        provider_mode TEXT NOT NULL, state TEXT NOT NULL, transaction_ref TEXT, payer TEXT, requirements_digest TEXT NOT NULL,
        created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
    """),
]


def open_db(path, create=False):
    path = os.fspath(path)
    if not create and not os.path.exists(path):
        raise FileNotFoundError('database missing')
    if create:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        os.close(fd)
    info = os.stat(path)
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
        raise PermissionError('database must be a private regular file')
    db = sqlite3.connect(path, timeout=15, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('PRAGMA synchronous=FULL')
    db.execute('PRAGMA busy_timeout=15000')
    return db


def migrate(path):
    """Apply pending migrations in order; returns the applied list."""
    db = open_db(path, create=True)
    applied = []
    try:
        db.execute('BEGIN IMMEDIATE')
        db.execute('CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY, applied_at INTEGER NOT NULL)')
        done = {r[0] for r in db.execute('SELECT name FROM schema_migrations')}
        for name, sql in MIGRATIONS:
            if name in done:
                continue
            db.executescript(sql) if False else [db.execute(stmt) for stmt in _statements(sql)]
            db.execute('INSERT INTO schema_migrations VALUES (?, ?)', (name, int(time.time())))
            applied.append(name)
        db.execute('COMMIT')
    except BaseException:
        db.execute('ROLLBACK')
        raise
    finally:
        db.close()
    return applied


def _statements(sql):
    return [s.strip() for s in sql.split(';') if s.strip()]


def schema_version(path):
    db = open_db(path)
    try:
        rows = [r[0] for r in db.execute('SELECT name FROM schema_migrations ORDER BY name')]
    finally:
        db.close()
    return rows


def check_schema(path):
    """Structural check before opening restored or foreign state for writes."""
    expected = [name for name, _ in MIGRATIONS]
    actual = schema_version(path)
    if actual != expected:
        raise RuntimeError('schema version mismatch: ' + ','.join(actual) + ' vs ' + ','.join(expected))
    db = open_db(path)
    try:
        if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise RuntimeError('integrity check failed')
        if db.execute('PRAGMA foreign_key_check').fetchall():
            raise RuntimeError('foreign key check failed')
    finally:
        db.close()
    return actual


class Database:
    def __init__(self, path):
        self.path = os.fspath(path)

    @contextmanager
    def tx(self):
        db = open_db(self.path)
        try:
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.execute('COMMIT')
        except BaseException:
            db.execute('ROLLBACK')
            raise
        finally:
            db.close()

    @contextmanager
    def read(self):
        db = open_db(self.path)
        try:
            yield db
        finally:
            db.close()


def now():
    return int(time.time())
