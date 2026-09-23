"""Owner-local journal; durable authorization state is separate from payment state.

This is not a network server or authentication system. Registration/audit/actor
parameters come from a trusted local operator, never an unauthenticated request.
Protect the DB, its parent directory, and backups. Restoring old state is unsafe.

Payment states of one action (one per job, one per request id):

  (no row)            NOT_REQUESTED   nothing reserved, nothing owed
  RESERVED            budget reserved; adapter never called; may expire -> FAILED_CONFIRMED
  SUBMISSION_PENDING  intent committed; the adapter call may be in flight or lost
  CONFIRMED           terminal; adapter answer bound to the stored request
  FAILED_CONFIRMED    terminal; conclusively no effect (adapter said so, or the
                      reservation expired before any dispatch attempt)
  OUTCOME_UNKNOWN     an attempt may have had an effect; exposure retained
                      until an authoritative adapter record resolves it

Exposure counted against the fixed campaign cap = every row that is not
FAILED_CONFIRMED. A terminal state is never replaced by a conflicting one.
"""
from contextlib import contextmanager
import os
import sqlite3
import stat
import time
from experiments.private_receipts import receipt as merkle
from integrations.x402.legacy_adapter import request_digest
from . import acceptance, contract as terms, energy_analysis as energy

STATES = ('RESERVED', 'SUBMISSION_PENDING', 'CONFIRMED', 'FAILED_CONFIRMED', 'OUTCOME_UNKNOWN')
TERMINAL = ('CONFIRMED', 'FAILED_CONFIRMED')


def clock(now):
    return energy.integer(int(time.time()) if now is None else now, 1)


class Journal:
    def __init__(self, path, campaign, limit):
        self.path = os.fspath(path)
        terms.token(campaign)
        energy.integer(limit, 1)
        parent = os.path.dirname(os.path.abspath(self.path)) or '.'
        # A directory others can write lets them swap the file between opens.
        # This is a cheap guard, not a substitute for owner-controlled storage.
        if os.stat(parent).st_mode & 0o002:
            raise merkle.Invalid('journal directory must not be writable by others')
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
                raise merkle.Invalid('journal must be a private regular file')
        finally:
            os.close(fd)
        with self._tx() as db:
            db.execute('CREATE TABLE IF NOT EXISTS campaign (id TEXT PRIMARY KEY, cap INTEGER NOT NULL)')
            db.execute('''CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, contract TEXT NOT NULL, digest TEXT UNIQUE NOT NULL,
                root TEXT, outcome TEXT, accepted INTEGER NOT NULL DEFAULT 0)''')
            db.execute('''CREATE TABLE IF NOT EXISTS actions (
                id TEXT PRIMARY KEY, job TEXT UNIQUE NOT NULL REFERENCES jobs(id),
                binding TEXT NOT NULL, request TEXT NOT NULL, amount INTEGER NOT NULL,
                state TEXT NOT NULL, result TEXT)''')
            rows = db.execute('SELECT id, cap FROM campaign').fetchall()
            if not rows:
                db.execute('INSERT INTO campaign VALUES (?, ?)', (campaign, limit))
            elif len(rows) != 1 or tuple(rows[0]) != (campaign, limit):
                raise merkle.Invalid('campaign configuration is immutable')
        self.campaign_id = campaign
        self.limit = limit

    # Test seams for deterministic process-interruption evidence. They do no
    # work here; a test-only subclass may terminate the process at either point.
    def _before_commit(self, db):
        pass

    def _after_commit(self, db):
        pass

    @contextmanager
    def _tx(self):
        db = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA foreign_keys=ON')
            db.execute('PRAGMA synchronous=FULL')
            db.execute('BEGIN IMMEDIATE')
            yield db
            self._before_commit(db)
            db.commit()
            self._after_commit(db)
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def register(self, contract, expected_digest, owner, now=None):
        terms.trusted(contract, expected_digest)  # current verifier bundle only
        if owner != contract['owner'] or clock(now) >= contract['expires_at']:
            raise merkle.Invalid('unauthorized or expired contract')
        encoded = merkle.canonical(contract).decode('ascii')
        with self._tx() as db:
            row = db.execute('SELECT contract FROM jobs WHERE id=?', (contract['job_id'],)).fetchone()
            if row is not None:
                if row['contract'] != encoded:
                    raise merkle.Invalid('job entitlement cannot be reissued')
                return
            db.execute('INSERT INTO jobs(id, contract, digest) VALUES (?, ?, ?)',
                       (contract['job_id'], encoded, expected_digest))

    @staticmethod
    def _job(db, job):
        """Load a job for a NEW authorization: the contract must name the current
        verifier bundle. Historical bundles are refused here by name; status()
        and reconcile() never call this, so in-flight actions stay resolvable."""
        row = db.execute('SELECT * FROM jobs WHERE id=?', (job,)).fetchone()
        if row is None:
            raise merkle.Invalid('unregistered job')
        contract = merkle.parse(row['contract'])
        terms.trusted(contract, row['digest'])
        return row, contract

    def audit(self, job, input_vault, evidence_vault, auditor, now=None):
        # Phase 1: snapshot the immutable registration (short transaction).
        with self._tx() as db:
            row, contract = self._job(db, job)
            snapshot = (row['contract'], row['digest'])
        if auditor != contract['auditor'] or clock(now) >= contract['expires_at']:
            raise merkle.Invalid('unauthorized or expired audit')
        # Phase 2: the full private recomputation runs OUTSIDE any write lock so a
        # slow audit cannot block unrelated reservations. It is bound to the
        # snapshot: the contract bytes and pin it was computed against.
        result = acceptance.audit(contract, snapshot[1], input_vault, evidence_vault)
        # Phase 3: recheck everything that could have changed, then record atomically.
        with self._tx() as db:
            row = db.execute('SELECT * FROM jobs WHERE id=?', (job,)).fetchone()
            if row is None or (row['contract'], row['digest']) != snapshot:
                raise merkle.Invalid('registered contract changed during audit')
            if clock(now) >= contract['expires_at']:
                raise merkle.Invalid('unauthorized or expired audit')
            if row['root'] is not None and row['root'] != result['evidence_root']:
                raise merkle.Invalid('accepted evidence root is immutable')
            db.execute('UPDATE jobs SET root=?, outcome=?, accepted=? WHERE id=?',
                       (result['evidence_root'], result['scientific_outcome'],
                        int(result['work_completed']), job))
            return result

    @staticmethod
    def _request(row, contract, request_id):
        terms.token(request_id)
        return {'request_id': request_id, 'job_id': contract['job_id'],
                'contract_digest': row['digest'], 'evidence_root': row['root'],
                'expires_at': contract['expires_at'],
                **{key: value for key, value in contract['action'].items() if key != 'limit'}}

    def request(self, job, request_id):
        with self._tx() as db:
            row, contract = self._job(db, job)
            if not row['accepted']:
                raise merkle.Invalid('work not accepted under the contract')
            return self._request(row, contract, request_id)

    def _reserve(self, request, actor, adapter, now):
        """Return True if a NEW reservation was recorded, False if the identical
        action already exists (a retry: no new right is created)."""
        adapter.validate(request)
        with self._tx() as db:
            row, contract = self._job(db, request.get('job_id'))
            if actor != contract['action']['actor'] or adapter.capability != contract['action']['capability']:
                raise merkle.Invalid('spend authorization refused')
            terms.token(request.get('request_id'))
            existing = db.execute('SELECT * FROM actions WHERE id=?', (request['request_id'],)).fetchone()
            if existing is not None:
                # Identical retry: return the recorded state. Expiry is not
                # rechecked because nothing new is authorized here.
                if existing['binding'] != request_digest(request):
                    raise merkle.Invalid('idempotency identifier rebound')
                return False
            # New authorization from here on: every condition is checked inside
            # the same write transaction as the budget comparison and insert.
            if not row['accepted']:
                raise merkle.Invalid('spend authorization refused')
            if clock(now) >= contract['expires_at']:
                raise merkle.Invalid('authorization expired')
            expected = self._request(row, contract, request['request_id'])
            if merkle.canonical(request) != merkle.canonical(expected):
                raise merkle.Invalid('action differs from the authorized binding')
            if db.execute('SELECT 1 FROM actions WHERE job=?', (request['job_id'],)).fetchone():
                raise merkle.Invalid('job action entitlement already used')
            exposure = self._exposure(db)
            if exposure + request['amount'] > self.limit:
                raise merkle.Invalid('campaign budget exhausted')
            db.execute('INSERT INTO actions VALUES (?, ?, ?, ?, ?, ?, NULL)',
                       (request['request_id'], request['job_id'], request_digest(request),
                        merkle.canonical(request).decode('ascii'), request['amount'], 'RESERVED'))
            return True

    @staticmethod
    def _expire_reserved(db, request_id, now):
        row = db.execute('SELECT request, state FROM actions WHERE id=?', (request_id,)).fetchone()
        if row is not None and row['state'] == 'RESERVED':
            request = merkle.parse(row['request'])
            if clock(now) >= request['expires_at']:
                result = {'state': 'FAILED_CONFIRMED', 'request_digest': request_digest(request),
                          'reference': 'local-expired-before-dispatch',
                          'capability': request['capability'], 'compute_units': 0}
                db.execute("UPDATE actions SET state='FAILED_CONFIRMED', result=? WHERE id=?",
                           (merkle.canonical(result).decode('ascii'), request_id))
                return True
        return False

    def _claim(self, request_id, now):
        with self._tx() as db:
            if self._expire_reserved(db, request_id, now):
                return False
            return db.execute("UPDATE actions SET state='SUBMISSION_PENDING' WHERE id=? AND state='RESERVED'",
                              (request_id,)).rowcount == 1

    def _unknown(self, request_id):
        with self._tx() as db:
            db.execute("UPDATE actions SET state='OUTCOME_UNKNOWN' WHERE id=? AND state='SUBMISSION_PENDING'",
                       (request_id,))

    def _finish(self, request_id, result, adapter):
        merkle.canonical(result)
        energy.exact(result, ('state', 'request_digest', 'reference', 'capability', 'compute_units'))
        if result['state'] not in TERMINAL:
            raise merkle.Invalid('invalid adapter conclusion')
        if type(result['reference']) is not str or not 1 <= len(result['reference']) <= 128:
            raise merkle.Invalid('invalid settlement reference')
        energy.integer(result['compute_units'])
        with self._tx() as db:
            row = db.execute('SELECT * FROM actions WHERE id=?', (request_id,)).fetchone()
            if row is None:
                raise merkle.Invalid('unknown action')
            request = merkle.parse(row['request'])
            if result['request_digest'] != row['binding'] or result['capability'] != request['capability']:
                raise merkle.Invalid('unbound adapter response')
            # Semantic consistency, not just key presence: a failure that grants
            # units, or a confirmation whose units do not match the bound amount,
            # is not evidence of anything and leaves the outcome unknown.
            expected_units = adapter.expected_units(request) if result['state'] == 'CONFIRMED' else 0
            if result['compute_units'] != expected_units:
                raise merkle.Invalid('inconsistent adapter response')
            encoded = merkle.canonical(result).decode('ascii')
            if row['state'] in TERMINAL:
                if row['result'] != encoded:
                    raise merkle.Invalid('conflicting terminal outcome')
                return
            if row['state'] == 'RESERVED':
                # No submission intent was ever recorded for this row; an
                # outcome cannot belong to it.
                raise merkle.Invalid('unbound adapter response')
            db.execute('UPDATE actions SET state=?, result=? WHERE id=?', (result['state'], encoded, request_id))

    def dispatch(self, request, actor, adapter, now=None):
        """Authorize (or resume) exactly one bounded action.

        New reservation -> submission intent -> adapter call -> bound confirmation.
        An identical retry returns the recorded state; a lost or mismatched answer
        leaves OUTCOME_UNKNOWN with exposure retained. Nothing here proves
        exactly-once external settlement; that depends on the adapter's rail.
        """
        # Detach caller-owned objects before authorization and asynchronous effects.
        request = merkle.parse(merkle.canonical(request))
        if type(request) is not dict:
            raise merkle.Invalid('action must be an object')
        self._reserve(request, actor, adapter, now)
        if self._claim(request['request_id'], now):
            try:
                self._finish(request['request_id'], adapter.submit(request), adapter)
            except Exception:
                # Do not echo adapter exceptions: they can contain private data.
                self._unknown(request['request_id'])
        return self.status(request['request_id'], actor)

    def reconcile(self, request_id, actor, adapter, now=None):
        """Read-only with respect to authorization: never calls submit(), may
        release an expired never-dispatched reservation, and may record an
        authoritative adapter outcome for a pending/unknown action."""
        with self._tx() as db:
            row = db.execute('SELECT * FROM actions WHERE id=?', (request_id,)).fetchone()
            if row is None:
                raise merkle.Invalid('unknown action')
            request = merkle.parse(row['request'])
            if request['actor'] != actor or adapter.capability != request['capability']:
                raise merkle.Invalid('unauthorized reconciliation')
            state = row['state']
            released = state == 'RESERVED' and self._expire_reserved(db, request_id, now)
        note = 'terminal-already' if state in TERMINAL else 'reserved-not-dispatched'
        if released:
            note = 'released-expired-reservation'
        if state in ('SUBMISSION_PENDING', 'OUTCOME_UNKNOWN'):
            self._unknown(request_id)
            note = 'unavailable-exposure-retained'
            try:
                result = adapter.reconcile(request)
                if result is not None:
                    self._finish(request_id, result, adapter)
                    note = 'resolved-from-adapter-record'
            except Exception:
                pass  # No authoritative outcome: retain the reservation.
        return dict(self.status(request_id, actor), reconciliation=note)

    def status(self, request_id, actor):
        with self._tx() as db:
            row = db.execute('SELECT * FROM actions WHERE id=?', (request_id,)).fetchone()
            if row is None:
                raise merkle.Invalid('unknown action')
            request = merkle.parse(row['request'])
            if actor != request['actor']:
                raise merkle.Invalid('unauthorized status access')
            return {'request_id': request_id, 'state': row['state'], 'amount': row['amount'],
                    'capability': request['capability'],
                    'result': None if row['result'] is None else merkle.parse(row['result'])}

    @staticmethod
    def _exposure(db):
        return db.execute("SELECT COALESCE(SUM(amount),0) FROM actions WHERE state != 'FAILED_CONFIRMED'").fetchone()[0]

    def exposure(self):
        with self._tx() as db:
            return self._exposure(db)

    def inspect(self):
        """Local-owner view of the campaign. Scientific outcomes are shown only
        when the job's own disclosure policy permits 'outcome' publicly, so
        this listing never widens a hide-outcome policy by accident."""
        with self._tx() as db:
            by_state = {state: 0 for state in STATES}
            for row in db.execute('SELECT state, SUM(amount) AS total FROM actions GROUP BY state'):
                by_state[row['state']] = row['total']
            jobs = []
            for row in db.execute('SELECT * FROM jobs ORDER BY id'):
                contract = merkle.parse(row['contract'])
                action = db.execute('SELECT id, state, amount FROM actions WHERE job=?', (row['id'],)).fetchone()
                jobs.append({'job_id': row['id'], 'contract_digest': row['digest'],
                             'verifier_status': terms.verifier_status(contract) or 'unknown',
                             'audited': row['root'] is not None, 'accepted': bool(row['accepted']),
                             'scientific_outcome': (row['outcome'] if 'outcome' in contract['allowed_disclosures']
                                                    else ('withheld-by-policy' if row['root'] is not None else None)),
                             'expires_at': contract['expires_at'],
                             'action': None if action is None else dict(action)})
            exposure = self._exposure(db)
        return {'campaign': self.campaign_id, 'limit': self.limit, 'exposure': exposure,
                'available': self.limit - exposure, 'exposure_by_state': by_state, 'jobs': jobs,
                'trust_scope': 'local-owner-journal;actor-strings-are-not-authentication'}
