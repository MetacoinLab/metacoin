"""Encrypted private artifact storage. Ciphertext files live under <home>/artifacts with
internally generated names; every file is bound to a row (workspace, kind, owner, job,
contract, plaintext digest, format version, recipients, intended use). Decryption happens
in memory with the service identity; application authorization is checked by callers
before load() is reached. Deleting a row/unlinking a file is NOT cryptographic erasure."""
import hashlib
import os
import secrets
from pathlib import Path
from experiments.private_receipts import receipt as merkle
from . import crypto
from .db import now
from .errors import ServiceError

KINDS = ('draft_input', 'input_vault', 'evidence_vault', 'public_bundle', 'review_envelope', 'comparison_input', 'export',
         'dataset_raw', 'dataset_normalized')


class ArtifactStore:
    def __init__(self, settings):
        self.settings = settings
        self.dir = Path(settings.artifacts_dir)
        self._identity = None

    def service_public(self, db):
        row = db.execute("SELECT value FROM meta WHERE key='service_age_public'").fetchone()
        if row is None:
            raise ServiceError('CAPABILITY_UNAVAILABLE', 'service encryption identity not initialized')
        return row['value']

    def identity(self):
        if self._identity is None:
            self._identity = crypto.load_age_identity(self.settings.keys_dir / 'service.age')
        return self._identity

    def store(self, db, *, workspace, kind, owner_id, plaintext, recipients, intended_use,
              job_id=None, contract_id=None, public=False, retention_deadline=None):
        if kind not in KINDS:
            raise ServiceError('VALIDATION', 'artifact kind')
        if len(plaintext) > self.settings.limits['max_upload_bytes']:
            raise ServiceError('PAYLOAD_TOO_LARGE', 'artifact')
        aid = 'a_' + secrets.token_hex(12)
        digest = hashlib.sha256(plaintext).hexdigest()
        if public:
            data, encrypted, fmt, ct_digest = plaintext, 0, 'plain/v1', None
        else:
            crypto.require_age()
            recips = sorted(set([self.service_public(db)] + list(recipients)))
            data = crypto.encrypt_bytes(plaintext, recips)
            encrypted, fmt, ct_digest, recipients = 1, crypto.ARTIFACT_FORMAT, hashlib.sha256(data).hexdigest(), recips
        name = secrets.token_hex(16) + ('.age' if encrypted else '.json')
        self.dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self.dir / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        db.execute('''INSERT INTO artifacts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,?)''',
                   (aid, workspace, kind, contract_id, job_id, owner_id, name, encrypted, fmt, ct_digest, digest,
                    len(plaintext), merkle.canonical(list(recipients) if not public else []).decode(), intended_use,
                    int(public), retention_deadline, now()))
        return aid

    def row(self, db, artifact_id, workspace):
        row = db.execute('SELECT * FROM artifacts WHERE id=? AND workspace=?', (artifact_id, workspace)).fetchone()
        if row is None:
            raise ServiceError('NOT_FOUND', 'artifact')
        return row

    def load(self, db, artifact_id, workspace):
        """Plaintext bytes (in memory). Caller has already authorized the principal."""
        row = self.row(db, artifact_id, workspace)
        if row['deleted_at'] is not None:
            raise ServiceError('NOT_FOUND', 'artifact payload deleted under retention policy')
        path = self.dir / row['storage_name']
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            raise ServiceError('NOT_FOUND', 'artifact payload missing') from None
        if row['encrypted']:
            if hashlib.sha256(data).hexdigest() != row['sha256_ciphertext']:
                raise ServiceError('EVIDENCE_INVALID', 'ciphertext altered')
            data = crypto.decrypt_bytes(data, self.identity())
        if hashlib.sha256(data).hexdigest() != row['sha256_plaintext']:
            raise ServiceError('EVIDENCE_INVALID', 'plaintext digest mismatch')
        return data

    def load_json(self, db, artifact_id, workspace):
        return merkle.parse(self.load(db, artifact_id, workspace))

    def ciphertext(self, db, artifact_id, workspace):
        """Raw stored bytes for export to a recipient who holds their own identity."""
        row = self.row(db, artifact_id, workspace)
        if row['deleted_at'] is not None:
            raise ServiceError('NOT_FOUND', 'artifact payload deleted under retention policy')
        return row, (self.dir / row['storage_name']).read_bytes()

    def delete_payload(self, db, artifact_id, workspace, actor_note):
        """Unlink the ciphertext object and mark the row. Keeps the non-secret integrity
        record (digests, sizes, recipients). Not secure erasure on SSD/snapshots/backups."""
        row = self.row(db, artifact_id, workspace)
        if row['deleted_at'] is not None:
            return False
        running = db.execute("SELECT 1 FROM jobs WHERE state='running' AND (evidence_artifact_id=? OR contract_id IN "
                             "(SELECT id FROM contracts WHERE input_artifact_id=?))", (artifact_id, artifact_id)).fetchone()
        if running:
            raise ServiceError('CONFLICT', 'artifact required by a running job')
        try:
            os.unlink(self.dir / row['storage_name'])
        except FileNotFoundError:
            pass
        db.execute('UPDATE artifacts SET deleted_at=? WHERE id=?', (now(), artifact_id))
        return True
