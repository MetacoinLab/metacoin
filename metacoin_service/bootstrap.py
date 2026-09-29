"""Initialize a service home: database, keys, campaign, principals and one-time credentials."""
import json
import os
from pathlib import Path
from . import auth, crypto, db as database, history
from .db import now
from .errors import ServiceError


def init(settings, workspace='ws_default', names=None):
    home = Path(settings.home)
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(home, 0o700)
    for d in (settings.keys_dir, settings.artifacts_dir, settings.credentials_dir, settings.logs_dir, settings.run_dir):
        Path(d).mkdir(mode=0o700, exist_ok=True)
    if settings.db_path.exists():
        raise ServiceError('CONFLICT', 'service home already initialized')
    database.migrate(settings.db_path)
    service_pub = crypto.generate_age_identity(settings.keys_dir / 'service.age')
    service_sign_pub = crypto.generate_signing_key(settings.keys_dir / 'service.ed25519')
    D = database.Database(settings.db_path)
    out = {'workspace': workspace, 'principals': {}}
    with D.tx() as db:
        db.execute("INSERT INTO meta VALUES ('service_age_public', ?)", (service_pub,))
        db.execute("INSERT INTO meta VALUES ('service_signing_public', ?)", (service_sign_pub,))
        db.execute("INSERT INTO meta VALUES ('initialized_at', ?)", (str(now()),))
        db.execute("INSERT INTO meta VALUES ('reconciliation_gate', '0')")
        db.execute('INSERT INTO campaigns VALUES (?,?,?,?,?,?)', (workspace, 'campaign-' + workspace, settings.campaign_cap,
                                                                 'Test-META', 'local-simulation', 'atomic'))
        for role in ('owner', 'worker', 'reviewer', 'viewer'):
            pid = auth.create_principal(db, (names or {}).get(role, role), role, workspace)
            cid, token = auth.issue_credential(db, pid, settings.limits['credential_seconds'])
            entry = {'principal_id': pid, 'credential_id': cid, 'token': token, 'role': role}
            if role == 'reviewer':
                pub = crypto.generate_age_identity(settings.keys_dir / ('reviewer-' + pid + '.age'))
                db.execute('INSERT INTO meta VALUES (?, ?)', ('age_public:' + pid, pub))
                sign_pub = crypto.generate_signing_key(settings.keys_dir / ('reviewer-' + pid + '.ed25519'))
                key_id = crypto.key_id_for(sign_pub)
                db.execute('INSERT INTO reviewer_keys VALUES (?,?,?,?,?,NULL)', (key_id, pid, sign_pub, 'active', now()))
                entry['signing_key_id'] = key_id
            history.record(db, workspace, 'system', 'credential.issued', 'principal', pid, {'role': role, 'credential_id': cid})
            out['principals'][role] = entry
    path = settings.credentials_dir / 'bootstrap.json'
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump({'note': 'PRIVATE bearer credentials, shown once. Retrieve with: metacoin_service credentials path',
                   **out}, stream, indent=2)
    return {'home': str(home), 'workspace': workspace, 'credential_file': str(path),
            'principals': {r: {k: v for k, v in e.items() if k != 'token'} for r, e in out['principals'].items()}}
