"""End-to-end acceptance matrix for the work service (order §25), through real entry points.

Run with the service environment:  .venv-service/bin/python -m unittest metacoin_service.tests.test_service -v
Uses temporary homes, ephemeral ports, isolated test identities and synthetic inputs.
"""
from fractions import Fraction
import json
import os
from pathlib import Path
import random
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

from fastapi.testclient import TestClient
from experiments.private_receipts import receipt as merkle
from experiments.work_contracts import energy_analysis as energy, fixtures
from metacoin_service import actions as actions_mod, api, artifacts, bootstrap, config, crypto, db as database
from metacoin_service import history, ops, science, worker as worker_mod
from metacoin_service.errors import ServiceError

ROOT = Path(__file__).resolve().parents[2]
ENV = dict(os.environ, PYTHONPATH=str(ROOT))


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def own_inputs(label='USER_PRIVATE_LABEL_4471'):
    """Arbitrary valid inputs that are NOT the fixture."""
    return {'available_low': 2_400_000, 'available_high': 2_650_000, 'reserve': 150_000,
            'segments': [{'duration': 900, 'power_low': 1200, 'power_high': 1500},
                         {'duration': 300, 'power_low': 400, 'power_high': 900}],
            'units': dict(energy.UNITS), 'assumptions': list(energy.ASSUMPTIONS), 'provenance': 'declared_unverified',
            'private_label': label}


class Instance:
    """One temporary service home with an in-process app (TestClient) and helpers."""

    def __init__(self, provider_mode='simulation'):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name) / 'home'
        self.settings = config.Settings(home=self.home, provider_mode=provider_mode)
        self.settings.validate()
        info = bootstrap.init(self.settings)
        self.creds = json.load(open(info['credential_file']))
        self.tok = {r: e['token'] for r, e in self.creds['principals'].items()}
        self.ids = {r: e['principal_id'] for r, e in self.creds['principals'].items()}
        self.reopen()

    def reopen(self):
        """A fresh app over the same home (service restart)."""
        self.app = api.create_app(self.settings)
        self.client = TestClient(self.app, base_url='http://testserver', raise_server_exceptions=False)

    def h(self, role, **extra):
        return dict({'Authorization': 'Bearer ' + self.tok[role]}, **extra)

    def worker(self):
        return worker_mod.Worker(database.Database(self.settings.db_path), artifacts.ArtifactStore(self.settings), self.settings)

    def worker_process(self):
        proc = subprocess.run([sys.executable, '-m', 'metacoin_service', '--home', str(self.home), 'worker', '--once'],
                              cwd=ROOT, env=ENV, capture_output=True, text=True, timeout=120)
        assert proc.returncode == 0, proc.stderr[-800:]
        return json.loads(proc.stdout)['ran']

    def contract(self, kind='energy_audit', inputs=None, policy=None, title='test', freeze=True):
        body = {'kind': kind, 'title': title, 'inputs': inputs or own_inputs(),
                'policy': dict({'reviewer_id': self.ids['reviewer']}, **(policy or {}))}
        r = self.client.post('/api/v1/contracts', headers=self.h('owner'), json=body)
        assert r.status_code == 201, r.text
        cid = r.json()['id']
        if freeze:
            r = self.client.post('/api/v1/contracts/' + cid + '/freeze', headers=self.h('owner'))
            assert r.status_code == 200, r.text
        return cid

    def job(self, cid=None, **kw):
        cid = cid or self.contract(**kw)
        r = self.client.post('/api/v1/jobs', headers=self.h('owner'), json={'contract_id': cid})
        assert r.status_code == 202, r.text
        return r.json()['id']

    def accepted_job(self, **kw):
        jid = self.job(**kw)
        self.worker().run_once()
        assert self.client.post('/api/v1/jobs/' + jid + '/review-request', headers=self.h('owner')).status_code == 200
        r = self.client.post('/api/v1/reviews/' + jid + '/decision', headers=self.h('reviewer'), json={'decision': 'accepted'})
        assert r.status_code == 200, r.text
        return jid

    def close(self):
        self.temp.cleanup()


class ServiceMatrix(unittest.TestCase):
    def setUp(self):
        self.inst = Instance()
        self.addCleanup(self.inst.close)
        self.c = self.inst.client

    # 1 ---------------------------------------------------------------------------
    def test_01_owner_creates_own_inputs_and_a_separate_worker_publishes(self):
        cid = self.inst.contract(inputs=own_inputs(), title='my instrument')
        contract = self.c.get('/api/v1/contracts/' + cid, headers=self.inst.h('owner')).json()
        self.assertEqual(contract['state'], 'frozen')
        self.assertEqual(len(contract['contract_digest']), 64)
        jid = self.inst.job(cid)
        self.assertEqual(self.c.get('/api/v1/jobs/' + jid, headers=self.inst.h('owner')).json()['state'], 'queued')
        self.assertEqual(self.inst.worker_process()[1], 'succeeded')       # a separate worker process
        view = self.c.get('/api/v1/jobs/' + jid, headers=self.inst.h('owner')).json()
        self.assertEqual((view['state'], view['outcome']), ('succeeded', 'FEASIBLE'))
        result = self.c.get('/api/v1/jobs/' + jid + '/result', headers=self.inst.h('owner')).json()
        # hand check: required_high = 150000 + 1500*900 + 900*300 = 1_770_000 <= available_low 2_400_000
        self.assertEqual(result['result']['required_high'], 1_770_000)
        self.assertEqual(result['bindings']['contract_digest'], contract['contract_digest'])
        self.assertNotIn('USER_PRIVATE_LABEL_4471', json.dumps(self.c.get('/api/v1/jobs/' + jid, headers=self.inst.h('viewer')).json()))

    # 2 ---------------------------------------------------------------------------
    def test_02_restart_preserves_job_result_pin_and_history(self):
        jid = self.inst.job()
        before = self.c.get('/api/v1/jobs/' + jid, headers=self.inst.h('owner')).json()
        self.inst.reopen()                                       # API restart with a queued job
        self.c = self.inst.client
        self.assertEqual(self.c.get('/api/v1/jobs/' + jid, headers=self.inst.h('owner')).json()['state'], 'queued')
        self.assertEqual(self.inst.worker_process()[1], 'succeeded')
        self.inst.reopen()                                       # restart again after the result
        self.c = self.inst.client
        after = self.c.get('/api/v1/jobs/' + jid, headers=self.inst.h('owner')).json()
        self.assertEqual(after['contract_digest'], before['contract_digest'])
        self.assertEqual(after['state'], 'succeeded')
        hist = self.c.get('/api/v1/jobs/' + jid + '/history', headers=self.inst.h('owner')).json()['events']
        self.assertEqual([e['event_type'] for e in hist], ['job.queued', 'job.claimed', 'job.result_committed'])
        self.assertTrue(self.c.get('/api/v1/history', headers=self.inst.h('owner')).json()['chain']['valid'])

    # 3 ---------------------------------------------------------------------------
    def test_03_two_workers_race_only_the_lease_holder_publishes(self):
        jid = self.inst.job()
        a, b = self.inst.worker(), self.inst.worker()
        claimed_a = a.claim()
        self.assertEqual(claimed_a['id'], jid)
        self.assertIsNone(b.claim())                             # nothing else queued; lease is A's
        with database.Database(self.inst.settings.db_path).tx() as db:   # A is terminated: its lease expires
            db.execute('UPDATE jobs SET lease_expires=? WHERE id=?', (0, jid))
        claimed_b = b.claim()                                    # recovery: B takes generation 2
        self.assertEqual((claimed_b['id'], claimed_b['lease_generation']), (jid, 2))
        self.assertEqual(b.execute(claimed_b), 'succeeded')
        root = self.c.get('/api/v1/jobs/' + jid, headers=self.inst.h('owner')).json()['evidence_root']
        self.assertEqual(a.execute(claimed_a), 'fenced')          # stale worker cannot overwrite
        self.assertEqual(self.c.get('/api/v1/jobs/' + jid, headers=self.inst.h('owner')).json()['evidence_root'], root)
        with database.Database(self.inst.settings.db_path).read() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM artifacts WHERE kind='evidence_vault' AND job_id=?", (jid,)).fetchone()[0], 1)
            events = [e['event_type'] for e in history.for_object(db, 'ws_default', 'job', jid)]
        self.assertEqual(events.count('job.result_committed'), 1)

    def test_03b_cancellation_keeps_evidence_and_failed_input_is_terminal(self):
        jid = self.inst.job()
        self.assertEqual(self.c.post('/api/v1/jobs/' + jid + '/cancel', headers=self.inst.h('owner')).json()['result'], 'cancelled')
        self.assertIsNone(self.inst.worker().run_once())
        view = self.c.get('/api/v1/jobs/' + jid, headers=self.inst.h('owner')).json()
        self.assertEqual(view['state'], 'cancelled')
        # a computation timeout is retried, then terminal
        jid2 = self.inst.job()
        self.inst.settings.limits['job_timeout_seconds'] = 0.001
        w = self.inst.worker()
        self.assertEqual(w.run_once()[1], 'retry')
        self.assertEqual(w.run_once()[1], 'retry')
        self.assertEqual(w.run_once()[1], 'failed')
        self.assertEqual(self.c.get('/api/v1/jobs/' + jid2, headers=self.inst.h('owner')).json()['error_code'], 'TIMEOUT')

    # 4 ---------------------------------------------------------------------------
    def test_04_viewer_and_spoofing_boundaries(self):
        jid = self.inst.accepted_job()
        cid = self.c.get('/api/v1/jobs/' + jid, headers=self.inst.h('owner')).json()['contract_id']
        v = self.inst.h('viewer')
        self.assertEqual(self.c.get('/api/v1/contracts/' + cid + '/inputs', headers=v).status_code, 403)
        self.assertEqual(self.c.get('/api/v1/jobs/' + jid + '/result', headers=v).status_code, 403)
        self.assertEqual(self.c.get('/api/v1/reviews/' + jid + '/evidence', headers=v).status_code, 403)
        self.assertEqual(self.c.post('/api/v1/reviews/' + jid + '/decision', headers=v, json={'decision': 'accepted'}).status_code, 403)
        self.assertEqual(self.c.post('/api/v1/actions', headers=v, json={'job_id': jid, 'request_id': 'r'}).status_code, 403)
        self.assertEqual(self.c.post('/api/v1/actions/' + jid + '/reconcile', headers=v).status_code, 403)
        priv = [a for a in self.c.get('/api/v1/jobs/' + jid + '/artifacts', headers=self.inst.h('owner')).json()['items'] if a['kind'] == 'evidence_vault'][0]['id']
        self.assertEqual(self.c.get('/api/v1/artifacts/' + priv + '/export', headers=v).status_code, 403)
        self.assertEqual(self.c.delete('/api/v1/artifacts/' + priv, headers=v).status_code, 403)
        # a submitted role/owner field is ignored: the worker cannot decide as reviewer, the viewer cannot create
        self.assertEqual(self.c.post('/api/v1/reviews/' + jid + '/decision', headers=self.inst.h('worker'), json={'decision': 'accepted', 'role': 'reviewer'}).status_code, 403)
        self.assertEqual(self.c.post('/api/v1/contracts', headers=v, json={'kind': 'energy_audit', 'title': 'x', 'inputs': own_inputs(), 'policy': {'reviewer_id': self.inst.ids['reviewer'], 'owner': self.inst.ids['owner']}}).status_code, 403)
        # guessed identifiers
        self.assertEqual(self.c.get('/api/v1/jobs/j_0000000000000000', headers=v).status_code, 404)
        self.assertEqual(self.c.get('/api/v1/artifacts/a_000000000000000000000000/export', headers=self.inst.h('owner')).status_code, 404)
        # revoked credential
        with database.Database(self.inst.settings.db_path).tx() as db:
            from metacoin_service import auth
            auth.revoke_credential(db, self.inst.creds['principals']['viewer']['credential_id'])
        self.assertEqual(self.c.get('/api/v1/jobs', headers=v).status_code, 401)
        # another workspace cannot see this job
        with database.Database(self.inst.settings.db_path).tx() as db:
            pid = auth.create_principal(db, 'other', 'owner', 'ws_other')
            _, other = auth.issue_credential(db, pid, 3600)
            db.execute('INSERT INTO campaigns VALUES (?,?,?,?,?,?)', ('ws_other', 'c-other', 3, 'Test-META', 'local-simulation', 'atomic'))
        self.assertEqual(self.c.get('/api/v1/jobs/' + jid, headers={'Authorization': 'Bearer ' + other}).status_code, 404)
        self.assertEqual(self.c.get('/api/v1/jobs', headers={'Authorization': 'Bearer ' + other}).json()['items'], [])
        # refused requests create no successful events
        events = self.c.get('/api/v1/jobs/' + jid + '/history', headers=self.inst.h('owner')).json()['events']
        self.assertNotIn('payment.reserved', [e['event_type'] for e in events])

    # 5 / 6 -----------------------------------------------------------------------
    def test_05_06_signed_decision_and_substitution(self):
        jid = self.inst.accepted_job()
        review = self.c.get('/api/v1/reviews/' + jid, headers=self.inst.h('viewer')).json()
        self.assertTrue(review['verification']['valid'])
        env, sig = review['envelope'], review['signature_hex']
        verify = lambda e, s, expected=None: self.c.post('/api/v1/reviews/verify', headers=self.inst.h('viewer'),
                                                          json={'envelope': e, 'signature_hex': s, 'expected': expected}).json()
        self.assertTrue(verify(env, sig, {'job_id': jid})['valid'])
        self.assertFalse(verify(dict(env, decision='rejected'), sig)['valid'])              # modified envelope
        self.assertFalse(verify(dict(env, evidence_root='0' * 64), sig)['valid'])          # changed root
        self.assertFalse(verify(env, sig, {'job_id': 'j_other'})['valid'])                 # replay across jobs
        self.assertFalse(verify(env, 'ab' * 64)['valid'])                                    # malformed signature
        self.assertFalse(verify(dict(env, schema='metacoin-review-envelope/v9'), sig)['valid'])
        # substituted key: a second key claiming the same reviewer id is not in the trust table under that key_id
        other_pub = crypto.generate_signing_key(self.inst.home / 'keys' / 'attacker.ed25519')
        attacker = crypto.load_signing_key(self.inst.home / 'keys' / 'attacker.ed25519')
        forged_env = dict(env, key_id=crypto.key_id_for(other_pub), decision='accepted')
        from metacoin_service import reviews
        forged_sig = crypto.sign(attacker, reviews.envelope_bytes(forged_env))
        self.assertEqual(verify(forged_env, forged_sig)['reason'], 'unknown reviewer key (trust table)')
        # revoked key: historical envelope no longer valid for authority
        ops.revoke_reviewer_key(self.inst.settings, env['key_id'])
        self.assertEqual(verify(env, sig)['key_status'], 'revoked')
        self.assertFalse(verify(env, sig)['valid'])
        # rotated key: old signature remains verifiable as historical
        new_key = ops.rotate_reviewer_key(self.inst.settings, self.inst.ids['reviewer'])
        jid2 = self.inst.accepted_job()
        self.assertEqual(self.c.get('/api/v1/reviews/' + jid2, headers=self.inst.h('viewer')).json()['key_id'], new_key)
        # oversized envelope
        big = dict(env, nonce='x' * 70000)
        self.assertEqual(self.c.post('/api/v1/reviews/verify', headers=self.inst.h('viewer'), json={'envelope': big, 'signature_hex': sig}).status_code, 413)
        # a second, different decision under the same job is refused, identical one idempotent
        self.assertEqual(self.c.post('/api/v1/reviews/' + jid2 + '/decision', headers=self.inst.h('reviewer'), json={'decision': 'rejected'}).status_code, 409)
        self.assertEqual(self.c.post('/api/v1/reviews/' + jid2 + '/decision', headers=self.inst.h('reviewer'), json={'decision': 'accepted'}).status_code, 200)

    def test_06b_acceptance_cannot_be_manufactured_when_recomputation_fails(self):
        jid = self.inst.job()
        self.inst.worker().run_once()
        self.c.post('/api/v1/jobs/' + jid + '/review-request', headers=self.inst.h('owner'))
        store = artifacts.ArtifactStore(self.inst.settings)
        with database.Database(self.inst.settings.db_path).tx() as db:      # tamper with the stored evidence root
            db.execute("UPDATE jobs SET evidence_root=? WHERE id=?", ('0' * 64, jid))
        ev = self.c.get('/api/v1/reviews/' + jid + '/evidence', headers=self.inst.h('reviewer')).json()
        self.assertEqual(ev['recomputation'], 'matches')  # vault itself is intact; the job row is not the evidence
        with database.Database(self.inst.settings.db_path).tx() as db:      # now corrupt the ciphertext bytes
            row = db.execute("SELECT storage_name FROM artifacts WHERE job_id=? AND kind='evidence_vault'", (jid,)).fetchone()
        path = self.inst.settings.artifacts_dir / row['storage_name']
        data = bytearray(path.read_bytes()); data[-1] ^= 0x01; path.write_bytes(bytes(data))
        r = self.c.post('/api/v1/reviews/' + jid + '/decision', headers=self.inst.h('reviewer'), json={'decision': 'accepted'})
        self.assertNotEqual(r.status_code, 200)
        self.assertEqual(self.c.get('/api/v1/jobs/' + jid, headers=self.inst.h('owner')).json()['review_state'], 'requested')

    # 7 ---------------------------------------------------------------------------
    def test_07_encrypted_artifacts_recipients_wrong_key_and_tamper(self):
        jid = self.inst.job()
        self.inst.worker().run_once()
        arts = self.c.get('/api/v1/jobs/' + jid + '/artifacts', headers=self.inst.h('reviewer')).json()['items']
        vault = [a for a in arts if a['kind'] == 'evidence_vault'][0]
        self.assertTrue(vault['encrypted'] and vault['format_version'] == crypto.ARTIFACT_FORMAT)
        self.assertEqual(len(vault['recipients']), 2)
        ct = self.c.get('/api/v1/artifacts/' + vault['id'] + '/export', headers=self.inst.h('reviewer')).content
        self.assertTrue(ct.startswith(b'age-encryption.org/'))
        self.assertNotIn(b'USER_PRIVATE_LABEL_4471', ct)
        reviewer_identity = crypto.load_age_identity(self.inst.home / 'keys' / ('reviewer-' + self.inst.ids['reviewer'] + '.age'))
        plaintext = crypto.decrypt_bytes(ct, reviewer_identity)
        self.assertEqual(merkle.parse(plaintext)['receipt']['root'], vault['sha256_plaintext'] and merkle.parse(plaintext)['receipt']['root'])
        wrong = crypto.load_age_identity.__globals__['_age'].Identity.generate()
        with self.assertRaises(ServiceError) as ctx:
            crypto.decrypt_bytes(ct, wrong)
        self.assertNotIn('USER_PRIVATE', str(ctx.exception.detail))
        tampered = bytearray(ct); tampered[len(ct) // 2] ^= 0xFF
        with self.assertRaises(ServiceError):
            crypto.decrypt_bytes(bytes(tampered), reviewer_identity)
        # the service refuses a modified ciphertext on disk before decrypting
        with database.Database(self.inst.settings.db_path).read() as db:
            name = db.execute('SELECT storage_name FROM artifacts WHERE id=?', (vault['id'],)).fetchone()['storage_name']
        (self.inst.settings.artifacts_dir / name).write_bytes(bytes(tampered))
        r = self.c.get('/api/v1/jobs/' + jid + '/result', headers=self.inst.h('owner'))
        self.assertEqual(r.json()['code'], 'EVIDENCE_INVALID')
        self.assertNotIn('USER_PRIVATE', r.text)
        (self.inst.settings.artifacts_dir / name).write_bytes(ct)      # restore the genuine ciphertext
        # the encrypted path fails closed when the identity is missing
        os.rename(self.inst.settings.keys_dir / 'service.age', self.inst.settings.keys_dir / 'service.age.moved')
        try:
            self.inst.reopen()
            r = self.inst.client.post('/api/v1/contracts', headers=self.inst.h('owner'), json={'kind': 'energy_audit', 'title': 'x', 'inputs': own_inputs(), 'policy': {}})
            self.assertEqual(r.status_code, 201)             # storing needs only the public key
            self.assertEqual(self.inst.client.get('/api/v1/jobs/' + jid + '/result', headers=self.inst.h('owner')).json()['code'], 'CAPABILITY_UNAVAILABLE')
        finally:
            os.rename(self.inst.settings.keys_dir / 'service.age.moved', self.inst.settings.keys_dir / 'service.age')

    # 8 ---------------------------------------------------------------------------
    def test_08_safe_runtime_boundary_cap_and_zero_power(self):
        def reference(d):
            """Exact rational reference in watt-hours, structured differently."""
            fixed = sum(Fraction(s['power_high'], 1000) * Fraction(s['duration'], 3600) for s in d['fixed_segments'])
            residual_wh = Fraction(d['available_low'] - d['reserve'], 3_600_000) - fixed
            if residual_wh < 0:
                return 'BASE_PLAN_INFEASIBLE', 0
            if d['variable_power_high'] == 0:
                return 'CAPPED_MODEL_UNBOUNDED', d['duration_cap']
            seconds = residual_wh / (Fraction(d['variable_power_high'], 1000) / 3600)
            best = int(seconds)                      # floor for nonnegative rationals
            return ('CAPPED', d['duration_cap']) if best >= d['duration_cap'] else ('ROBUSTLY_FEASIBLE', best)
        base = {'available_low': 1_000_000, 'available_high': 1_100_000, 'reserve': 100_000,
                'fixed_segments': [{'duration': 600, 'power_low': 800, 'power_high': 1000}], 'variable_power_low': 100,
                'variable_power_high': 250, 'duration_cap': 3600, 'units': dict(energy.UNITS),
                'assumptions': list(energy.ASSUMPTIONS), 'provenance': 'synthetic', 'private_label': 'x'}
        out = science.safe_runtime(base)
        self.assertEqual((out['status'], out['safe_duration'], out['margin_at_duration']), ('ROBUSTLY_FEASIBLE', 1200, 0))
        self.assertTrue(out['maximality']['violates_declared_bound'] and out['maximality']['one_more_second_margin'] == -250)
        cases = [dict(base, variable_power_low=0, variable_power_high=7),                                # non-divisible: floor and witness
                 dict(base, duration_cap=100),                                       # cap binds
                 dict(base, variable_power_high=0, variable_power_low=0),            # zero upper power
                 dict(base, reserve=950_000),                                        # negative residual
                 dict(base, available_low=700_000, available_high=700_000, fixed_segments=[{'duration': 600, 'power_low': 1000, 'power_high': 1000}]),  # residual exactly 0
                 dict(base, available_low=energy.LIMIT, available_high=energy.LIMIT, variable_power_low=0, variable_power_high=1, duration_cap=energy.LIMIT)]
        rng = random.Random(8_812)
        for _ in range(150):
            cases.append(dict(base, available_low=rng.randint(0, 5_000_000), available_high=5_000_000, reserve=rng.randint(0, 500_000),
                              fixed_segments=[{'duration': rng.randint(1, 900), 'power_low': 0, 'power_high': rng.randint(0, 2000)} for __ in range(rng.randint(0, 3))],
                              variable_power_high=rng.randint(0, 500), variable_power_low=0, duration_cap=rng.randint(1, 5000)))
        for d in cases:
            out = science.safe_runtime(d)
            status, duration = reference(d)
            self.assertEqual((out['status'], out['safe_duration']), (status, duration), d)
            if status == 'ROBUSTLY_FEASIBLE':
                self.assertGreaterEqual(out['margin_at_duration'], 0)
                self.assertLess(out['margin_at_duration'], d['variable_power_high'])
                self.assertLess(out['maximality']['one_more_second_margin'], 0)
            if status == 'CAPPED':
                self.assertIn('physical maximality not claimed', out['maximality'])
        for bad in (dict(base, variable_power_low=300), dict(base, duration_cap=0), dict(base, reserve=-1),
                    dict(base, available_low=2, available_high=1), dict(base, fixed_segments=[{'duration': energy.LIMIT, 'power_low': 2, 'power_high': 2}])):
            with self.assertRaises(merkle.Invalid):
                science.safe_runtime(bad)
        # through the API, worker and result
        jid = self.inst.job(kind='safe_runtime', inputs=base)
        self.assertEqual(self.inst.worker_process()[1], 'succeeded')
        view = self.c.get('/api/v1/jobs/' + jid, headers=self.inst.h('owner')).json()
        self.assertEqual((view['outcome'], view['summary']['safe_duration'], view['model_id']), ('ROBUSTLY_FEASIBLE', 1200, science.SAFE_RUNTIME_MODEL))

    # 9 ---------------------------------------------------------------------------
    def test_09_plan_comparison_selects_submitted_candidate_and_preserves_indeterminate(self):
        def reference(data):
            rows = {}
            for c in data['candidates']:
                r = energy.analyze(c['inputs'])
                rows[c['id']] = (r['outcome'], r['required_high'], sum(s['duration'] for s in c['inputs']['segments']), c['utility'])
            feasible = [(cid, v) for cid, v in rows.items() if v[0] == 'FEASIBLE' and v[3] is not None]
            if data['objective'] != 'max_utility' or not feasible:
                return None
            return min(feasible, key=lambda kv: (-kv[1][3], kv[1][1], kv[1][2], kv[0]))[0]
        f, i, n = fixtures.inputs('FEASIBLE'), fixtures.inputs('INDETERMINATE'), fixtures.inputs('INFEASIBLE')
        data = {'candidates': [{'id': 'a', 'inputs': f, 'utility': 5}, {'id': 'b', 'inputs': i, 'utility': 9},
                               {'id': 'c', 'inputs': n, 'utility': 1}, {'id': 'd', 'inputs': dict(f, reserve=90_000), 'utility': 5}],
                'objective': 'max_utility', 'private_label': 'x'}
        out = science.compare_plans(data)
        self.assertEqual(out['selected_id'], 'd')                 # tie on utility 5 broken by lower worst-case demand
        self.assertEqual((out['indeterminate_ids'], out['infeasible_ids']), (['b'], ['c']))
        self.assertEqual(out['selected_id'], reference(data))
        # permutation invariance and equal candidates -> identifier order
        rev = dict(data, candidates=list(reversed(data['candidates'])))
        self.assertEqual(science.compare_plans(rev)['selected_id'], 'd')
        eq = dict(data, candidates=[{'id': 'z', 'inputs': f, 'utility': 3}, {'id': 'y', 'inputs': f, 'utility': 3}])
        self.assertEqual(science.compare_plans(eq)['selected_id'], 'y')
        none = dict(data, candidates=[{'id': 'b', 'inputs': i, 'utility': 9}, {'id': 'c', 'inputs': n, 'utility': 1}])
        out = science.compare_plans(none)
        self.assertIsNone(out['selected_id'])
        self.assertIn('no robustly feasible candidate', out['selection_rationale'])
        self.assertEqual(out['indeterminate_ids'], ['b'])
        rng = random.Random(4_401)
        for _ in range(60):
            cands = [{'id': 'p%d' % k, 'inputs': dict(f, available_low=rng.randint(500_000, 800_000), available_high=rng.randint(800_000, 1_200_000)),
                      'utility': rng.choice([None, rng.randint(0, 3)])} for k in range(rng.randint(1, 6))]
            d = {'candidates': cands, 'objective': 'max_utility', 'private_label': 'x'}
            self.assertEqual(science.compare_plans(d)['selected_id'], reference(d))
        for bad in (dict(data, candidates=[]), dict(data, candidates=data['candidates'][:1] * 2), dict(data, objective='min'),
                    dict(data, candidates=[{'id': 'a', 'inputs': f, 'utility': -1}])):
            with self.assertRaises(merkle.Invalid):
                science.compare_plans(bad)
        jid = self.inst.job(kind='plan_comparison', inputs=data)
        self.assertEqual(self.inst.worker_process()[1], 'succeeded')
        view = self.c.get('/api/v1/jobs/' + jid, headers=self.inst.h('owner')).json()
        self.assertEqual((view['outcome'], view['summary']['selected_id']), ('SELECTED:d', 'd'))
        self.assertNotIn('available_low', json.dumps(self.c.get('/api/v1/jobs/' + jid, headers=self.inst.h('viewer')).json()))

    # 12 --------------------------------------------------------------------------
    def test_12_provider_loss_keeps_exposure_and_reconciliation_never_resubmits(self):
        from experiments.work_contracts import demo
        jid = self.inst.accepted_job()
        faucet = fixtures.funded_faucet(actor=self.inst.ids['owner'], amount=10)
        lost = demo.LostAcknowledgement(faucet)
        calls = {'submit': 0}
        real_submit = lost.submit
        def counting(request):
            calls['submit'] += 1
            return real_submit(request)
        lost.submit = counting
        original = actions_mod.provider_for
        actions_mod.provider_for = lambda mode, settings, capability, actor='x': (lost, {'adapter_session': 'test'})
        try:
            r = self.c.post('/api/v1/actions', headers=self.inst.h('owner'), json={'job_id': jid, 'request_id': 'r1'})
            self.assertEqual(r.json()['state'], 'OUTCOME_UNKNOWN')
            self.assertEqual(faucet.balance_of(self.inst.ids['owner']), 9)     # the effect happened
            budget = self.c.get('/api/v1/budget', headers=self.inst.h('owner')).json()
            self.assertEqual(budget['by_state']['OUTCOME_UNKNOWN'], 1)
            # identical resubmission does not dispatch again
            self.assertEqual(self.c.post('/api/v1/actions', headers=self.inst.h('owner'), json={'job_id': jid, 'request_id': 'r1'}).json()['state'], 'OUTCOME_UNKNOWN')
            self.assertEqual(calls['submit'], 1)
            # a provider without a record cannot resolve it
            from integrations.x402.legacy_adapter import LegacyAdapter
            actions_mod.provider_for = lambda mode, settings, capability, actor='x': (LegacyAdapter(faucet), {'adapter_session': 'fresh'})
            r = self.c.post('/api/v1/actions/' + jid + '/reconcile', headers=self.inst.h('owner')).json()
            self.assertEqual((r['state'], r['reconciliation']), ('OUTCOME_UNKNOWN', 'unavailable-exposure-retained'))
            # the instance that holds the record resolves it, without any new submission
            actions_mod.provider_for = lambda mode, settings, capability, actor='x': (lost, {'adapter_session': 'same'})
            r = self.c.post('/api/v1/actions/' + jid + '/reconcile', headers=self.inst.h('owner')).json()
            self.assertEqual((r['state'], r['reconciliation']), ('CONFIRMED', 'resolved-from-adapter-record'))
            self.assertEqual((calls['submit'], faucet.balance_of(self.inst.ids['owner'])), (1, 9))
        finally:
            actions_mod.provider_for = original
        events = [e['event_type'] for e in self.c.get('/api/v1/jobs/' + jid + '/history', headers=self.inst.h('owner')).json()['events']]
        self.assertEqual(events.count('payment.dispatched'), 2)   # both attempts recorded, one adapter call
        self.assertIn('payment.reconciled', events)

    def test_12b_concurrent_api_requests_keep_the_budget_invariant(self):
        from concurrent.futures import ThreadPoolExecutor
        jobs = [self.inst.accepted_job(policy={'amount': 4}) for _ in range(3)]   # cap is 10: only two fit
        def go(jid):
            return self.c.post('/api/v1/actions', headers=self.inst.h('owner'), json={'job_id': jid, 'request_id': 'r-' + jid}).json()
        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(go, jobs))
        states = sorted(r.get('state', r.get('code')) for r in results)
        self.assertEqual(states, ['BUDGET_EXHAUSTED', 'CONFIRMED', 'CONFIRMED'])
        self.assertEqual(self.c.get('/api/v1/budget', headers=self.inst.h('owner')).json()['exposure'], 8)

    # 13 --------------------------------------------------------------------------
    def test_13_backup_and_restore_without_economic_resubmission(self):
        from experiments.work_contracts import demo
        accepted = self.inst.accepted_job()
        queued = self.inst.job()
        faucet = fixtures.funded_faucet(actor=self.inst.ids['owner'], amount=10)
        original = actions_mod.provider_for
        actions_mod.provider_for = lambda mode, settings, capability, actor='x': (demo.LostAcknowledgement(faucet), {'adapter_session': 't'})
        try:
            self.assertEqual(self.c.post('/api/v1/actions', headers=self.inst.h('owner'), json={'job_id': accepted, 'request_id': 'ra'}).json()['state'], 'OUTCOME_UNKNOWN')
        finally:
            actions_mod.provider_for = original
        dest = Path(self.inst.temp.name) / 'backup'
        manifest = ops.backup(self.inst.settings, dest)
        self.assertIn('EXCLUDED', manifest['contains']['keys'])
        self.assertFalse((dest / 'keys').exists())
        # restore into a fresh home WITHOUT keys
        restored_home = Path(self.inst.temp.name) / 'restored'
        rs = config.Settings(home=restored_home)
        out = ops.restore(dest, rs)
        self.assertFalse(out['keys_restored'])
        st = ops.status(rs)
        self.assertEqual(st['reconciliation_gate'], '1')
        self.assertEqual(st['jobs_by_state'].get('queued'), 1)
        self.assertEqual(st['journal']['ws_default']['OUTCOME_UNKNOWN'], 1)      # unresolved exposure preserved
        rapp = TestClient(api.create_app(rs), raise_server_exceptions=False)
        self.assertEqual(rapp.get('/api/v1/jobs/' + accepted, headers=self.inst.h('owner')).json()['review_state'], 'accepted')
        self.assertEqual(rapp.get('/api/v1/jobs/' + accepted + '/result', headers=self.inst.h('owner')).json()['code'], 'CAPABILITY_UNAVAILABLE')  # key loss fails clearly
        self.assertEqual(rapp.get('/api/v1/jobs/' + accepted, headers=self.inst.h('owner')).json()['payment']['state'], 'OUTCOME_UNKNOWN')
        self.assertEqual(faucet.balance_of(self.inst.ids['owner']), 9)          # nothing was resubmitted by restoring
        # restore WITH keys into another fresh home: encrypted artifacts readable, review verifiable
        rs2 = config.Settings(home=Path(self.inst.temp.name) / 'restored2')
        out2 = ops.restore(dest, rs2, keys_dir=self.inst.settings.keys_dir)
        self.assertTrue(out2['keys_restored'])
        rapp2 = TestClient(api.create_app(rs2), raise_server_exceptions=False)
        self.assertEqual(rapp2.get('/api/v1/jobs/' + accepted + '/result', headers=self.inst.h('owner')).status_code, 200)
        self.assertTrue(rapp2.get('/api/v1/reviews/' + accepted, headers=self.inst.h('owner')).json()['verification']['valid'])
        with self.assertRaises(ServiceError):
            ops.restore(dest, rs2)                                    # never into a non-empty destination
        self.assertEqual(database.check_schema(rs2.db_path), database.schema_version(self.inst.settings.db_path))

    # 14 --------------------------------------------------------------------------
    def test_14_retention_cleanup_removes_payload_keeps_record(self):
        jid = self.inst.accepted_job(policy={'retention_seconds': 3600})
        running = self.inst.job()
        self.inst.worker().claim()                                   # a running job holds its input artifact
        with database.Database(self.inst.settings.db_path).tx() as db:
            db.execute('UPDATE artifacts SET retention_deadline=1 WHERE public=0')
        out = ops.cleanup(self.inst.settings)
        self.assertGreaterEqual(len(out['removed']), 2)
        self.assertTrue(any(s['code'] == 'CONFLICT' for s in out['skipped']))    # running job's input skipped
        r = self.c.get('/api/v1/jobs/' + jid + '/result', headers=self.inst.h('owner'))
        self.assertEqual(r.json()['code'], 'NOT_FOUND')
        arts = self.c.get('/api/v1/jobs/' + jid + '/artifacts', headers=self.inst.h('owner')).json()['items']
        vault = [a for a in arts if a['kind'] == 'evidence_vault'][0]
        self.assertIsNotNone(vault['deleted_at'])
        self.assertEqual(len(vault['sha256_plaintext']), 64)         # integrity record retained
        self.assertTrue(all(a['deleted_at'] is None for a in arts if a['public']))
        self.assertEqual(self.c.get('/api/v1/jobs/' + jid, headers=self.inst.h('owner')).json()['review_state'], 'accepted')
        # explicit owner deletion of a draft input
        cid = self.inst.contract(freeze=False)
        aid = [a for a in self.c.get('/api/v1/jobs/' + jid + '/artifacts', headers=self.inst.h('owner')).json()['items']][0]['id']
        self.assertEqual(self.c.delete('/api/v1/artifacts/' + aid, headers=self.inst.h('owner')).json()['payload_unlinked'], False)

    # misc boundaries ---------------------------------------------------------------
    def test_15_drafts_change_frozen_contracts_do_not_amendments_have_lineage(self):
        cid = self.inst.contract(freeze=False)
        r = self.c.patch('/api/v1/contracts/' + cid, headers=self.inst.h('owner'), json={'title': 'renamed', 'inputs': dict(own_inputs(), reserve=1)})
        self.assertEqual((r.status_code, r.json()['title']), (200, 'renamed'))
        self.assertEqual(self.c.post('/api/v1/contracts/' + cid + '/freeze', headers=self.inst.h('owner')).status_code, 200)
        self.assertEqual(self.c.patch('/api/v1/contracts/' + cid, headers=self.inst.h('owner'), json={'title': 'again'}).status_code, 409)
        self.assertEqual(self.c.post('/api/v1/contracts/' + cid + '/freeze', headers=self.inst.h('owner')).status_code, 409)
        jid = self.inst.job(cid)
        self.assertEqual(self.c.post('/api/v1/jobs', headers=self.inst.h('owner'), json={'contract_id': cid}).status_code, 409)
        r = self.c.post('/api/v1/contracts/' + cid + '/amend', headers=self.inst.h('owner'), json={'inputs': own_inputs(), 'policy': {'amount': 2}})
        self.assertEqual(r.status_code, 201)
        new = r.json()
        self.assertEqual((new['version'], new['previous_id'], new['state']), (2, cid, 'draft'))
        self.assertEqual(new['lineage_id'], self.c.get('/api/v1/contracts/' + cid, headers=self.inst.h('owner')).json()['lineage_id'])
        self.assertEqual(self.c.get('/api/v1/jobs/' + jid, headers=self.inst.h('owner')).json()['contract_id'], cid)
        for raw in (b'{"kind":"energy_audit","kind":"x"}', b'{"a":1.5}', b'{"a":NaN}', b'x' * 300_000):
            r = self.c.post('/api/v1/contracts', headers=self.inst.h('owner'), content=raw)
            self.assertIn(r.status_code, (413, 422), raw[:20])
            self.assertNotIn('Traceback', r.text)

    def test_16_sessions_csrf_and_capabilities(self):
        s = self.c.post('/api/v1/session', json={'token': self.inst.tok['owner']})
        self.assertEqual(s.status_code, 200)
        cookie = s.cookies['metacoin_session']
        self.assertEqual(self.c.get('/api/v1/me', cookies={'metacoin_session': cookie}).json()['role'], 'owner')
        r = self.c.post('/api/v1/contracts', cookies={'metacoin_session': cookie}, json={'kind': 'energy_audit', 'title': 'x', 'inputs': own_inputs(), 'policy': {}})
        self.assertEqual(r.json()['code'], 'CSRF')
        r = self.c.post('/api/v1/contracts', cookies={'metacoin_session': cookie}, headers={'X-CSRF-Token': s.json()['csrf']},
                        json={'kind': 'energy_audit', 'title': 'x', 'inputs': own_inputs(), 'policy': {}})
        self.assertEqual(r.status_code, 201)
        self.assertEqual(self.c.delete('/api/v1/session', cookies={'metacoin_session': cookie}, headers={'X-CSRF-Token': s.json()['csrf']}).status_code, 200)
        self.assertEqual(self.c.get('/api/v1/me', cookies={'metacoin_session': cookie}).status_code, 401)
        self.assertEqual(self.c.post('/api/v1/session', json={'token': 'mck_guess'}).status_code, 401)
        caps = self.c.get('/api/v1/capabilities', headers=self.inst.h('viewer')).json()
        self.assertFalse(caps['external_settlement_observed'])
        self.assertEqual(caps['x402_http_sale']['mode'], 'simulation')
        self.assertIn('not non-custodial', caps['signed_reviews']['custody'])
        for header in ('Cache-Control', 'Content-Security-Policy'):
            self.assertIn(header, self.c.get('/api/v1/me', headers=self.inst.h('viewer')).headers)

    def test_17_console_pages_render_for_each_role(self):
        jid = self.inst.accepted_job()
        cookies = {}
        for role in ('owner', 'reviewer', 'viewer'):
            r = self.c.post('/console/login', data={'token': self.inst.tok[role]}, follow_redirects=False)
            self.assertEqual(r.status_code, 303)
            cookies[role] = r.cookies['metacoin_session']
        for path, ok in (('/console/', 200), ('/console/jobs/' + jid, 200), ('/console/budget', 200), ('/console/history', 200),
                         ('/console/contracts/new?kind=energy_audit', 200), ('/console/reviews/' + jid, 403)):
            page = self.c.get(path, cookies={'metacoin_session': cookies['owner']})
            self.assertEqual(page.status_code, ok, path)
            self.assertNotIn('USER_PRIVATE_LABEL_4471', page.text)
        viewer_job = self.c.get('/console/jobs/' + jid, cookies={'metacoin_session': cookies['viewer']}).text
        self.assertNotIn('worst-case margin', viewer_job)
        self.assertEqual(self.c.get('/console/reviews/' + jid, cookies={'metacoin_session': cookies['reviewer']}).status_code, 200)
        self.assertEqual(self.c.get('/console/contracts/new', cookies={'metacoin_session': cookies['viewer']}).status_code, 403)
        # form path: create + freeze + submit from the console with CSRF
        me = self.c.get('/api/v1/me', cookies={'metacoin_session': cookies['owner']}).json()
        with database.Database(self.inst.settings.db_path).read() as db:
            csrf = db.execute('SELECT csrf FROM sessions WHERE id=?', (cookies['owner'],)).fetchone()['csrf']
        r = self.c.post('/console/contracts', cookies={'metacoin_session': cookies['owner']}, follow_redirects=False,
                        data={'csrf': csrf, 'kind': 'energy_audit', 'title': 'from form', 'inputs': json.dumps(own_inputs()),
                              'reviewer_id': self.inst.ids['reviewer'], 'accept_FEASIBLE': 'on', 'accept_INFEASIBLE': 'on',
                              'accept_INDETERMINATE': 'on', 'disclose_outcome': 'on', 'amount': '1', 'expires_in_seconds': '3600', 'freeze_and_submit': '1'})
        self.assertEqual(r.status_code, 303)
        self.assertTrue(r.headers['location'].startswith('/console/jobs/'))
        r = self.c.post('/console/contracts', cookies={'metacoin_session': cookies['owner']}, follow_redirects=False,
                        data={'kind': 'energy_audit', 'title': 'no csrf', 'inputs': '{}', 'reviewer_id': 'x'})
        self.assertEqual(r.status_code, 403)
        fresh = TestClient(self.inst.app, base_url='http://testserver', raise_server_exceptions=False)
        self.assertEqual(fresh.get('/console/', follow_redirects=False).status_code, 303)     # no session
        self.assertEqual(fresh.get('/console/', cookies={'metacoin_session': 'stale'}, follow_redirects=False).headers['location'], '/console/login?expired=1')


class X402SocketTests(unittest.TestCase):
    """Scenarios 10 and 11: a real client process against a real server process over TCP."""

    @classmethod
    def setUpClass(cls):
        cls.inst = Instance(provider_mode='test-http')
        cls.port = free_port()
        cls.base = 'http://127.0.0.1:' + str(cls.port)
        cls.proc = subprocess.Popen([sys.executable, '-m', 'metacoin_service', '--home', str(cls.inst.home), '--provider-mode', 'test-http',
                                     'serve', '--port', str(cls.port)], cwd=ROOT, env=ENV, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        import httpx
        for _ in range(100):
            try:
                if httpx.get(cls.base + '/api/health', timeout=1).status_code == 200:
                    break
            except Exception:
                time.sleep(0.1)
        else:
            raise RuntimeError('server did not start: ' + cls.proc.stderr.read().decode()[-500:])
        cls.http = httpx.Client(base_url=cls.base, timeout=30)
        cid = cls.http.post('/api/v1/contracts', headers=cls.inst.h('owner'),
                            json={'kind': 'energy_audit', 'title': 'sold', 'inputs': own_inputs(), 'policy': {'reviewer_id': cls.inst.ids['reviewer'], 'amount': 3}}).json()['id']
        cls.http.post('/api/v1/contracts/' + cid + '/freeze', headers=cls.inst.h('owner'))
        cls.jid = cls.http.post('/api/v1/jobs', headers=cls.inst.h('owner'), json={'contract_id': cid}).json()['id']
        subprocess.run([sys.executable, '-m', 'metacoin_service', '--home', str(cls.inst.home), '--provider-mode', 'test-http', 'worker', '--once'],
                       cwd=ROOT, env=ENV, check=True, capture_output=True, timeout=120)
        cls.http.post('/api/v1/jobs/' + cls.jid + '/review-request', headers=cls.inst.h('owner'))
        assert cls.http.post('/api/v1/reviews/' + cls.jid + '/decision', headers=cls.inst.h('reviewer'), json={'decision': 'accepted'}).status_code == 200

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(timeout=10)
        cls.inst.close()

    def client(self, mutation=None, identifier=None):
        proc = subprocess.run([sys.executable, '-m', 'metacoin_service.tests.x402_client', self.base, self.jid, mutation or '', identifier or ''][:7 if identifier else (6 if mutation else 5)],
                              cwd=ROOT, env=ENV, capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr[-800:])
        return json.loads(proc.stdout.strip().splitlines()[-1])

    def test_10_402_then_paid_retrieval_through_the_sdk_protocol(self):
        out = self.client()
        self.assertEqual((out['first_status'], out['has_payment_required'], out['second_status']), (402, True, 200))
        self.assertTrue(out['settled']['success'])
        self.assertEqual(out['settled']['amount'], '3')
        self.assertEqual(out['bundle_keys'], ['disclosures', 'receipt'])
        again = self.client(identifier=out['identifier'])         # same identifier: idempotent re-delivery, one settlement
        self.assertEqual(again['second_status'], 200)
        self.assertEqual(again['settled']['transaction'], out['settled']['transaction'])
        fresh = self.client()                                     # a new identifier is a new sale of the same bundle
        self.assertNotEqual(fresh['settled']['transaction'], out['settled']['transaction'])
        sales = [e for e in self.http.get('/api/v1/history', headers=self.inst.h('owner')).json()['events'] if e['event_type'].startswith('sale')]
        self.assertEqual(sorted(e['event_type'] for e in sales), ['sale.requested', 'sale.requested', 'sale.settled', 'sale.settled'])
        page = self.http.get('/api/v1/jobs/' + self.jid, headers=self.inst.h('owner')).json()
        self.assertEqual(page['payment']['state'], 'NOT_REQUESTED')   # selling a result is not the agent's purchase
        self.assertEqual(self.http.post('/api/v1/sales/' + self.jid + '/reconcile', headers=self.inst.h('owner')).json()['reconciliation'], 'terminal-already')

    def test_11_bindings_refused_at_the_right_layer(self):
        expected = {'amount': 'No matching payment requirements', 'pay_to': 'No matching payment requirements',
                    'network': 'No matching payment requirements', 'extra': 'No matching payment requirements',
                    'resource': 'work_contract_binding_mismatch', 'no_identifier': 'work_contract_payment_identifier_required',
                    'signature': 'invalid_signature'}
        for mutation, error in expected.items():
            with self.subTest(mutation=mutation):
                out = self.client(mutation)
                self.assertEqual((out['second_status'], out['error']), (402, error))
        # the route is not sold for a job without an accepted review
        other = self.http.post('/api/v1/contracts', headers=self.inst.h('owner'),
                               json={'kind': 'energy_audit', 'title': 'unsold', 'inputs': own_inputs(), 'policy': {'reviewer_id': self.inst.ids['reviewer']}}).json()['id']
        self.assertEqual(self.http.get('/api/v1/x402/jobs/' + other + '/public-bundle').status_code, 404)
        # the service in simulation mode refuses the sale route with a capability error, never a plaintext bundle
        sim = Instance()
        try:
            jid = sim.accepted_job()
            r = sim.client.get('/api/v1/x402/jobs/' + jid + '/public-bundle')
            self.assertEqual((r.status_code, r.json()['code']), (501, 'CAPABILITY_UNAVAILABLE'))
        finally:
            sim.close()
        # production mode fails closed on incomplete configuration
        with self.assertRaises(ValueError):
            config.Settings(home=self.inst.home, provider_mode='production').validate()


if __name__ == '__main__':
    unittest.main()


class BacklogTests(unittest.TestCase):
    def setUp(self):
        self.inst = Instance()
        self.addCleanup(self.inst.close)
        self.c = self.inst.client

    def test_batch_submission_is_all_or_nothing_with_aggregate_budget(self):
        good = [self.inst.contract(policy={'amount': 4}) for _ in range(2)]        # 8 of cap 10
        draft = self.inst.contract(freeze=False)
        r = self.c.post('/api/v1/jobs/batch', headers=self.inst.h('owner'), json={'contract_ids': good + [draft]})
        self.assertEqual(r.status_code, 409)
        self.assertEqual([d['code'] for d in r.json()['items']], [None, None, 'CONFLICT'])
        self.assertEqual(self.c.get('/api/v1/jobs', headers=self.inst.h('owner')).json()['items'], [])   # nothing queued
        third = self.inst.contract(policy={'amount': 4})                              # 12 > cap 10
        r = self.c.post('/api/v1/jobs/batch', headers=self.inst.h('owner'), json={'contract_ids': good + [third]})
        self.assertEqual((r.status_code, {d['code'] for d in r.json()['items']}), (409, {'BUDGET_EXHAUSTED'}))
        r = self.c.post('/api/v1/jobs/batch', headers=self.inst.h('owner', **{'Idempotency-Key': 'batch-1'}), json={'contract_ids': good})
        self.assertEqual(r.status_code, 202)
        bid = r.json()['batch_id']
        self.assertEqual(self.c.post('/api/v1/jobs/batch', headers=self.inst.h('owner', **{'Idempotency-Key': 'batch-1'}), json={'contract_ids': good}).json()['batch_id'], bid)
        progress = self.c.get('/api/v1/batches/' + bid, headers=self.inst.h('owner')).json()
        self.assertEqual((progress['size'], progress['by_state'], progress['done']), (2, {'queued': 2}, False))
        w = self.inst.worker(); w.run_once(); w.run_once()
        progress = self.c.get('/api/v1/batches/' + bid, headers=self.inst.h('viewer')).json()
        self.assertEqual((progress['by_state'], progress['done']), ({'succeeded': 2}, True))
        self.assertTrue(all(j['outcome'] is None for j in progress['jobs']))          # viewer projection
        self.assertEqual(self.c.post('/api/v1/jobs/batch', headers=self.inst.h('viewer'), json={'contract_ids': good}).status_code, 403)
        self.assertEqual(self.c.post('/api/v1/jobs/batch', headers=self.inst.h('owner'), json={'contract_ids': ['x'] * 21}).status_code, 422)

    def test_saved_templates_instantiate_with_explicit_inputs_and_compare_runs(self):
        r = self.c.post('/api/v1/templates', headers=self.inst.h('owner'),
                        json={'name': 'nightly adequacy', 'kind': 'energy_audit', 'policy': {'reviewer_id': self.inst.ids['reviewer'], 'amount': 2}, 'notes': 'no inputs stored'})
        self.assertEqual(r.status_code, 201)
        tid = r.json()['id']
        self.assertNotIn('inputs', r.json())
        self.assertEqual(self.c.post('/api/v1/templates/' + tid + '/instantiate', headers=self.inst.h('owner'), json={}).status_code, 422)
        ids = []
        for reserve in (150_000, 900_000):
            cid = self.c.post('/api/v1/templates/' + tid + '/instantiate', headers=self.inst.h('owner'),
                              json={'inputs': dict(own_inputs(), reserve=reserve), 'title': 'run reserve ' + str(reserve)}).json()['id']
            self.c.post('/api/v1/contracts/' + cid + '/freeze', headers=self.inst.h('owner'))
            ids.append(self.c.post('/api/v1/jobs', headers=self.inst.h('owner'), json={'contract_id': cid}).json()['id'])
        w = self.inst.worker(); w.run_once(); w.run_once()
        runs = self.c.get('/api/v1/templates/' + tid + '/runs', headers=self.inst.h('owner')).json()
        self.assertEqual((len(runs['runs']), runs['distinct_input_roots']), (2, 2))
        self.assertEqual([x['outcome'] for x in runs['runs']], ['FEASIBLE', 'INDETERMINATE'])   # larger reserve: bounds overlap
        self.assertLess(runs['runs'][1]['worst_margin'], runs['runs'][0]['worst_margin'])
        viewer_runs = self.c.get('/api/v1/templates/' + tid + '/runs', headers=self.inst.h('viewer')).json()
        self.assertNotIn('worst_margin', json.dumps(viewer_runs))
        self.assertEqual(self.c.post('/api/v1/templates', headers=self.inst.h('viewer'), json={'name': 'x', 'kind': 'energy_audit', 'policy': {}}).status_code, 403)
        self.assertEqual(self.c.get('/api/v1/templates', headers=self.inst.h('reviewer')).json()['items'][0]['id'], tid)

    def test_task_selection_optimizer_exhaustive_small_cases(self):
        def reference(d):
            """Independent recursive branch-and-bound-free search (include/exclude), same tie policy."""
            fixed = sum(s['power_high'] * s['duration'] for s in d['fixed_segments'])
            residual = d['available_low'] - d['reserve'] - fixed
            if residual < 0:
                return 'BASE_PLAN_INFEASIBLE', []
            tasks = d['optional_tasks']
            best = [None]
            def rec(i, chosen, e, dur, v):
                if e > residual or dur > d['duration_cap']:
                    return
                if i == len(tasks):
                    key = (-v, e, dur, sorted(chosen))
                    if best[0] is None or key < best[0]:
                        best[0] = key
                    return
                t = tasks[i]
                rec(i + 1, chosen + [t['id']], e + t['power_high'] * t['duration'], dur + t['duration'], v + t['value'])
                rec(i + 1, chosen, e, dur, v)
            rec(0, [], 0, 0, 0)
            return 'OPTIMAL_FOR_SUBMITTED_TASKS', best[0][3]
        base = {'available_low': 1_000_000, 'available_high': 1_100_000, 'reserve': 100_000,
                'fixed_segments': [{'duration': 600, 'power_low': 800, 'power_high': 1000}],
                'optional_tasks': [{'id': 'imaging', 'duration': 300, 'power_high': 500, 'value': 8},
                                   {'id': 'downlink', 'duration': 200, 'power_high': 900, 'value': 6},
                                   {'id': 'calibration', 'duration': 120, 'power_high': 300, 'value': 3}],
                'duration_cap': 900, 'units': dict(energy.UNITS), 'assumptions': list(energy.ASSUMPTIONS),
                'provenance': 'synthetic', 'private_label': 'x'}
        out = science.select_tasks(base)       # residual 300000 mJ: imaging 150000 + calibration 36000 fit; +downlink 180000 does not
        self.assertEqual((out['status'], out['selected_ids'], out['total_value'], out['energy_margin']), ('OPTIMAL_FOR_SUBMITTED_TASKS', ['calibration', 'imaging'], 11, 114000))
        self.assertEqual(out['considered'], 8)
        rng = random.Random(7_301)
        for _ in range(120):
            d = dict(base, available_low=rng.randint(700_000, 1_400_000), available_high=1_400_000, duration_cap=rng.randint(100, 1500),
                     optional_tasks=[{'id': 't%d' % k, 'duration': rng.randint(1, 400), 'power_high': rng.randint(0, 800), 'value': rng.randint(0, 9)}
                                     for k in range(rng.randint(1, 7))])
            out = science.select_tasks(d)
            status, ids = reference(d)
            self.assertEqual((out['status'], out['selected_ids']), (status, ids), d)
            if status != 'BASE_PLAN_INFEASIBLE':
                self.assertGreaterEqual(out['energy_margin'], 0)
                self.assertGreaterEqual(out['duration_margin'], 0)
        self.assertEqual(science.select_tasks(dict(base, reserve=2_000_000))['status'], 'BASE_PLAN_INFEASIBLE')
        with self.assertRaises(merkle.Invalid):
            science.select_tasks(dict(base, optional_tasks=[{'id': 't%d' % k, 'duration': 1, 'power_high': 1, 'value': 1} for k in range(13)]))
        jid = self.inst.job(kind='task_selection', inputs=base)
        self.assertEqual(self.inst.worker_process()[1], 'succeeded')
        view = self.c.get('/api/v1/jobs/' + jid, headers=self.inst.h('owner')).json()
        self.assertEqual((view['outcome'], view['summary']['selected_ids']), ('OPTIMAL_FOR_SUBMITTED_TASKS', ['calibration', 'imaging']))
        self.c.post('/api/v1/jobs/' + jid + '/review-request', headers=self.inst.h('owner'))
        self.assertEqual(self.c.get('/api/v1/reviews/' + jid + '/evidence', headers=self.inst.h('reviewer')).json()['recomputation'], 'matches')

    def test_scoped_credentials_cannot_be_widened(self):
        r = self.c.post('/api/v1/credentials', headers=self.inst.h('owner'), json={'operations': ['job:read', 'job:submit', 'contract:read'], 'expires_in_seconds': 3600})
        self.assertEqual(r.status_code, 201)
        scoped = {'Authorization': 'Bearer ' + r.json()['token']}
        cid = self.inst.contract()
        self.assertEqual(self.c.post('/api/v1/jobs', headers=scoped, json={'contract_id': cid}).status_code, 202)     # in scope
        self.assertEqual(self.c.post('/api/v1/contracts', headers=scoped, json={'kind': 'energy_audit', 'title': 'x', 'inputs': own_inputs(), 'policy': {}}).status_code, 403)
        self.assertEqual(self.c.post('/api/v1/actions', headers=scoped, json={'job_id': 'x', 'request_id': 'r'}).status_code, 403)
        self.assertEqual(self.c.post('/api/v1/credentials', headers=scoped, json={'operations': ['job:read']}).status_code, 403)   # cannot mint
        for widen in (['admin:credentials'], ['action:create'], ['job:read', 'review:decide']):
            self.assertEqual(self.c.post('/api/v1/credentials', headers=self.inst.h('owner'), json={'operations': widen}).status_code, 403, widen)
        self.assertEqual(self.c.post('/api/v1/credentials', headers=self.inst.h('viewer'), json={'operations': ['job:read']}).status_code, 403)
        self.assertEqual(self.c.post('/api/v1/credentials', headers=self.inst.h('owner'), json={'operations': ['job:read'], 'expires_in_seconds': 10}).status_code, 422)
        self.assertEqual(self.c.delete('/api/v1/credentials/' + r.json()['credential_id'], headers=self.inst.h('owner')).status_code, 200)
        self.assertEqual(self.c.get('/api/v1/jobs', headers=scoped).status_code, 401)

    def test_client_cli_and_run_comparison(self):
        from metacoin_service import client_cli
        import threading, uvicorn
        port = free_port()
        server = uvicorn.Server(uvicorn.Config(self.inst.app, host='127.0.0.1', port=port, log_level='error'))
        thread = threading.Thread(target=server.run, daemon=True); thread.start()
        import httpx
        for _ in range(100):
            try:
                if httpx.get('http://127.0.0.1:%d/api/health' % port, timeout=1).status_code == 200:
                    break
            except Exception:
                time.sleep(0.05)
        cred = Path(self.inst.temp.name) / 'cred.json'
        cred.write_text(json.dumps({'token': self.inst.tok['owner']})); os.chmod(cred, 0o600)
        inputs = Path(self.inst.temp.name) / 'inputs.json'
        inputs.write_text(json.dumps(own_inputs()))
        import io, contextlib
        def cli(*args):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = client_cli.main(['--credential-file', str(cred), '--base', 'http://127.0.0.1:%d' % port] + list(args))
            return code, json.loads(buf.getvalue())
        code, out = cli('create', '--title', 'cli run', '--inputs', str(inputs), '--reviewer', self.inst.ids['reviewer'])
        self.assertEqual(code, 0); cid = out['id']
        self.assertEqual(cli('freeze', cid)[1]['state'], 'frozen')
        jid = cli('submit', cid, '--idempotency-key', 'cli-1')[1]['id']
        self.assertEqual(cli('submit', cid, '--idempotency-key', 'cli-1')[1]['id'], jid)
        self.inst.worker().run_once()
        self.assertEqual(cli('poll', jid)[1]['state'], 'succeeded')
        self.assertEqual(cli('review-request', jid)[1]['review_state'], 'requested')
        os.chmod(cred, 0o644)
        with self.assertRaises(SystemExit):
            cli('me')
        os.chmod(cred, 0o600)
        # second run with a changed reserve, then compare
        second = self.inst.job(inputs=dict(own_inputs(), reserve=900_000))
        self.inst.worker().run_once()
        code, cmp = cli('compare', jid, second)
        self.assertEqual(code, 0)
        self.assertEqual((cmp['outcomes'], cmp['same_inputs'], cmp['projection']), (['FEASIBLE', 'INDETERMINATE'], False, 'private'))
        self.assertIn('worst_margin', cmp['changed_results'])
        self.assertEqual(cmp['margin_delta_worst_case_mJ'], -750_000)
        viewer = self.c.get('/api/v1/jobs/' + jid + '/compare/' + second, headers=self.inst.h('viewer')).json()
        self.assertIsNone(viewer['outcomes'])
        self.assertNotIn('changed_results', viewer)
        self.assertEqual(self.c.get('/api/v1/jobs/' + jid + '/compare/' + self.inst.job(kind='safe_runtime', inputs={'available_low': 1_000_000, 'available_high': 1_100_000, 'reserve': 100_000, 'fixed_segments': [], 'variable_power_low': 0, 'variable_power_high': 1, 'duration_cap': 10, 'units': dict(energy.UNITS), 'assumptions': list(energy.ASSUMPTIONS), 'provenance': 'synthetic', 'private_label': 'x'}), headers=self.inst.h('owner')).status_code, 409)
        pub = [a for a in self.c.get('/api/v1/jobs/' + jid + '/artifacts', headers=self.inst.h('owner')).json()['items'] if a['kind'] == 'input_vault'][0]['id']
        code, out = cli('export', pub, '--out', str(Path(self.inst.temp.name) / 'vault.age'))
        self.assertEqual((code, out['bytes'] > 0), (0, True))
        self.assertTrue((Path(self.inst.temp.name) / 'vault.age').read_bytes().startswith(b'age-encryption'))
        server.should_exit = True; thread.join(timeout=5)


class ProductionBuyerTests(unittest.TestCase):
    """The production buyer adapter (real SDK client, real EIP-3009 signing with a THROWAWAY
    UNFUNDED key, durable submission, re-presentation) against this service's own sale route
    served by a real server process whose facilitator double verifies the signature
    cryptographically offline. No chain, no funds, nothing broadcast."""

    @classmethod
    def setUpClass(cls):
        # EXPERIMENTAL — private test chain only — not the protocol's money layer, which remains [SPEC] and zero-value by MIP-0001/0002; chain-agnostic law unchanged.
        from eth_account import Account
        cls.seller = Instance(provider_mode='test-http')
        cls.port = free_port()
        cls.base = 'http://127.0.0.1:' + str(cls.port)
        cls.proc = subprocess.Popen([sys.executable, '-m', 'metacoin_service', '--home', str(cls.seller.home), '--provider-mode', 'test-http',
                                     'serve', '--port', str(cls.port)], cwd=ROOT, env=ENV, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        import httpx
        for _ in range(100):
            try:
                if httpx.get(cls.base + '/api/health', timeout=1).status_code == 200:
                    break
            except Exception:
                time.sleep(0.1)
        cls.http = httpx.Client(base_url=cls.base, timeout=30)
        cls.sold = cls._sold_job(amount=3)
        key = Account.create()                                        # throwaway; unfunded; never on any chain
        cls.key_file = Path(cls.seller.temp.name) / 'buyer.key'
        cls.key_file.write_text(key.key.hex()); os.chmod(cls.key_file, 0o600)
        cls.buyer_address = key.address

    @classmethod
    def _sold_job(cls, amount):
        cid = cls.http.post('/api/v1/contracts', headers=cls.seller.h('owner'),
                            json={'kind': 'energy_audit', 'title': 'sold', 'inputs': own_inputs(), 'policy': {'reviewer_id': cls.seller.ids['reviewer'], 'amount': amount}}).json()['id']
        cls.http.post('/api/v1/contracts/' + cid + '/freeze', headers=cls.seller.h('owner'))
        jid = cls.http.post('/api/v1/jobs', headers=cls.seller.h('owner'), json={'contract_id': cid}).json()['id']
        subprocess.run([sys.executable, '-m', 'metacoin_service', '--home', str(cls.seller.home), '--provider-mode', 'test-http', 'worker', '--once'],
                       cwd=ROOT, env=ENV, check=True, capture_output=True, timeout=120)
        cls.http.post('/api/v1/jobs/' + jid + '/review-request', headers=cls.seller.h('owner'))
        assert cls.http.post('/api/v1/reviews/' + jid + '/decision', headers=cls.seller.h('reviewer'), json={'decision': 'accepted'}).status_code == 200
        return jid

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate(); cls.proc.wait(timeout=10); cls.seller.close()

    def buyer_instance(self, **overrides):
        inst = Instance(provider_mode='production' if False else 'simulation')   # provider mode is per action; settings below select production
        s = inst.settings
        s.buyer_resource_url = self.base + '/api/v1/x402/jobs/' + self.sold + '/public-bundle'
        s.buyer_key_file = str(self.key_file); s.buyer_network = 'eip155:84532'
        s.buyer_asset = '0x036CbD53842c5426634e7929541eC2318f3dCF7e'; s.buyer_max_amount = 5
        for k, v in overrides.items():
            setattr(s, k, v)
        inst.reopen()
        return inst

    def test_status_distinguishes_missing_configuration_from_missing_code(self):
        from metacoin_service import buyer
        bare = config.Settings(home=Path(tempfile.mkdtemp()))
        st = buyer.status(bare)
        self.assertTrue(st['code_implemented'] and st['sdk_evm_extra_installed'])
        self.assertEqual(st['configuration_missing'], ['buyer_resource_url', 'buyer_key_file', 'buyer_network', 'buyer_asset', 'buyer_max_amount'])
        self.assertFalse(st['available'])
        r = TestClient(api.create_app(bare) if False else self.seller.app).get('/api/v1/capabilities', headers=self.seller.h('viewer')).json()
        self.assertEqual(r['payment_actions']['modes']['production']['available'], False)
        with self.assertRaises(ServiceError) as ctx:
            buyer.HttpBuyerAdapter(bare)
        self.assertEqual(ctx.exception.code, 'CAPABILITY_UNAVAILABLE')
        loose = self.buyer_instance().settings
        os.chmod(self.key_file, 0o644)
        try:
            self.assertFalse(buyer.status(loose)['credential_file_usable'])
        finally:
            os.chmod(self.key_file, 0o600)

    def test_buyer_pays_a_real_402_resource_with_a_real_signature(self):
        inst = self.buyer_instance()
        jid = inst.accepted_job(policy={'capability': 'x402_http_buyer', 'amount': 3})
        c = inst.client
        dry = c.post('/api/v1/actions', headers=inst.h('owner'), json={'job_id': jid, 'request_id': 'buy-1', 'provider_mode': 'production', 'dry_run': True}).json()
        self.assertEqual((dry['would_reserve'], dry['adapter']['capability']), (True, 'x402_http_buyer'))
        r = c.post('/api/v1/actions', headers=inst.h('owner'), json={'job_id': jid, 'request_id': 'buy-1', 'provider_mode': 'production'}).json()
        stored = sqlite3.connect(inst.home / 'buyer.sqlite').execute('SELECT state, response_json FROM submissions').fetchall()
        self.assertEqual(r['state'], 'CONFIRMED', (r, stored))
        self.assertTrue(r['result']['reference'].startswith('0x') and len(r['result']['reference']) == 66)
        # the seller recorded the sale, and its double verified the buyer's real signature (not the placeholder)
        sales = [e for e in self.http.get('/api/v1/history', headers=self.seller.h('owner')).json()['events'] if e['event_type'] == 'sale.settled']
        self.assertEqual(len(sales), 1)
        seller_page = self.http.get('/console/budget')  # route exists; content checked via API instead
        # identical retry: no second signature, no second exchange
        again = c.post('/api/v1/actions', headers=inst.h('owner'), json={'job_id': jid, 'request_id': 'buy-1', 'provider_mode': 'production'}).json()
        self.assertEqual(again['result']['reference'], r['result']['reference'])
        with sqlite3.connect(inst.home / 'buyer.sqlite') as db:
            rows = db.execute('SELECT state, identifier FROM submissions').fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], 'CONFIRMED')
        # reconcile from a fresh process view: durable record answers without re-sending
        r2 = c.post('/api/v1/actions/' + jid + '/reconcile', headers=inst.h('owner')).json()
        self.assertEqual(r2['reconciliation'], 'terminal-already')
        self.assertEqual(c.get('/api/v1/budget', headers=inst.h('owner')).json()['by_state']['CONFIRMED'], 3)

    def test_buyer_refuses_bindings_before_signing(self):
        # price offered (3) differs from the journal-authorized amount (2): refused before any signature
        inst = self.buyer_instance()
        jid = inst.accepted_job(policy={'capability': 'x402_http_buyer', 'amount': 2})
        r = inst.client.post('/api/v1/actions', headers=inst.h('owner'), json={'job_id': jid, 'request_id': 'buy-2', 'provider_mode': 'production'}).json()
        self.assertEqual(r['state'], 'OUTCOME_UNKNOWN')       # the journal saw an adapter exception; nothing was sent
        self.assertFalse((inst.home / 'buyer.sqlite').exists() and sqlite3.connect(inst.home / 'buyer.sqlite').execute('SELECT COUNT(*) FROM submissions').fetchone()[0])
        # configured ceiling below the contract amount: refused at validation
        inst2 = self.buyer_instance(buyer_max_amount=1)
        jid2 = inst2.accepted_job(policy={'capability': 'x402_http_buyer', 'amount': 3})
        r = inst2.client.post('/api/v1/actions', headers=inst2.h('owner'), json={'job_id': jid2, 'request_id': 'buy-3', 'provider_mode': 'production'})
        self.assertEqual(r.json()['code'], 'ADAPTER_CAPABILITY')
        # wrong network configuration: no matching requirement, nothing signed
        inst3 = self.buyer_instance(buyer_network='eip155:1')
        jid3 = inst3.accepted_job(policy={'capability': 'x402_http_buyer', 'amount': 3})
        r = inst3.client.post('/api/v1/actions', headers=inst3.h('owner'), json={'job_id': jid3, 'request_id': 'buy-4', 'provider_mode': 'production'})
        self.assertEqual(r.json()['code'], 'ADAPTER_CAPABILITY')
        # pinned recipient mismatch
        inst4 = self.buyer_instance(buyer_pay_to='0x' + '99' * 20)
        jid4 = inst4.accepted_job(policy={'capability': 'x402_http_buyer', 'amount': 3})
        r = inst4.client.post('/api/v1/actions', headers=inst4.h('owner'), json={'job_id': jid4, 'request_id': 'buy-5', 'provider_mode': 'production'}).json()
        self.assertEqual(r['state'], 'OUTCOME_UNKNOWN')
        # a plain-HTTP non-loopback resource URL is refused as configuration
        with self.assertRaises(ServiceError):
            from metacoin_service import buyer
            buyer.HttpBuyerAdapter(self.buyer_instance(buyer_resource_url='http://example.invalid/paid').settings)

    def test_lost_response_then_re_presentation_reconciles(self):
        from metacoin_service import buyer
        inst = self.buyer_instance()
        jid = inst.accepted_job(policy={'capability': 'x402_http_buyer', 'amount': 3})
        original = buyer.HttpBuyerAdapter._get
        calls = {'n': 0}
        def flaky(self_, headers=None):
            calls['n'] += 1
            if headers and calls['n'] == 2:                   # the paid request is sent, the response is lost
                import httpx
                original(self_, headers)
                raise httpx.ReadTimeout('lost')
            return original(self_, headers)
        buyer.HttpBuyerAdapter._get = flaky
        try:
            r = inst.client.post('/api/v1/actions', headers=inst.h('owner'), json={'job_id': jid, 'request_id': 'buy-6', 'provider_mode': 'production'}).json()
            self.assertEqual(r['state'], 'OUTCOME_UNKNOWN')
            self.assertEqual(inst.client.get('/api/v1/budget', headers=inst.h('owner')).json()['by_state']['OUTCOME_UNKNOWN'], 3)
        finally:
            buyer.HttpBuyerAdapter._get = original
        with sqlite3.connect(inst.home / 'buyer.sqlite') as db:
            self.assertEqual(db.execute('SELECT state FROM submissions').fetchone()[0], 'OUTCOME_UNKNOWN')
        settled_before = len([e for e in self.http.get('/api/v1/history', headers=self.seller.h('owner')).json()['events'] if e['event_type'] == 'sale.settled'])
        r = inst.client.post('/api/v1/actions/' + jid + '/reconcile', headers=inst.h('owner')).json()   # re-presents the SAME signed payload
        self.assertEqual((r['state'], r['reconciliation']), ('CONFIRMED', 'resolved-from-adapter-record'))
        settled_after = len([e for e in self.http.get('/api/v1/history', headers=self.seller.h('owner')).json()['events'] if e['event_type'] == 'sale.settled'])
        self.assertEqual(settled_after, settled_before)           # the seller did not settle twice
        self.assertEqual(inst.client.get('/api/v1/budget', headers=inst.h('owner')).json()['by_state']['CONFIRMED'], 3)
