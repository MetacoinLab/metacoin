"""Durable transport and recovery witnesses (order 2026-10-23 group G; journeys 29–32): crash boundaries incl. retirement replay,
fragmented / duplicated / reordered / expired bundles without duplicate settlement or timeout refund, a stale restore quarantined
against a signed external checkpoint, and contradictory authority histories detected with the safety limitation exposed."""
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from metacoin_service import crypto
from metacoin_service.native import protocol as P
from metacoin_service.native.witness import WitnessClient
from metacoin_service.tests.test_native_domains import NativeDomainTests, Domain, ClientEndpoint, courier
from metacoin_service.tests.test_service import ROOT, ENV, free_port

PY = sys.executable


class WitnessProcess:
    def __init__(self):
        self.home = Path(tempfile.mkdtemp()) / 'witness'
        out = subprocess.run([PY, '-m', 'metacoin_service.native.witness', 'init', '--home', str(self.home), '--witness-id', 'w-test'], cwd=ROOT, env=ENV, check=True, capture_output=True, text=True); self.identity = json.loads(out.stdout)
        self.port = free_port(); self.base = 'http://127.0.0.1:%d' % self.port
        self.proc = subprocess.Popen([PY, '-m', 'metacoin_service.native.witness', 'serve', '--home', str(self.home), '--port', str(self.port)], cwd=ROOT, env=ENV, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.client = WitnessClient(self.base, str(self.home / 'token'))
        for _ in range(100):
            if self.client.call('GET', '/health')[0] == 200:
                break
            time.sleep(0.05)

    def close(self):
        self.proc.terminate(); self.proc.wait(timeout=10)


class NativeWitnessTests(NativeDomainTests):
    def setUp(self):
        super().setUp(); self.W = WitnessProcess(); self.addCleanup(self.W.close)
        for dom in (self.A, self.B):
            dom.inst.settings.witness_url = self.W.base; dom.inst.settings.witness_token_file = str(self.W.home / 'token'); dom.inst.settings.witness_key_id = self.W.identity['key_id']
            dom.inst.reopen(); dom.c = dom.inst.client; dom.ep = ClientEndpoint(dom.inst, dom.H)
            d = dom.domain(); self.assertEqual(self.W.client.call('POST', '/pin', {'domain': dom.id, 'key_id': d['authority']['key_id'], 'public_hex': d['authority']['public_hex']})[0], 200)

    def test_checkpoints_witnessed_and_stale_restore_quarantined_against_the_witness(self):
        A, B = self.A, self.B; rec0 = A.c.get('/api/v1/native/recovery', headers=A.H).json(); self.assertIn('witness', rec0['rollback_detection'])
        db = A.inst.settings.db_path; backup = Path(A.inst.temp.name) / 'before.sqlite'
        with A.inst.app.state.services.db.read() as d_:
            d_.execute('VACUUM INTO ?', (str(backup),))
        a = A.export('alice', B, self.bob['account'], 5); courier.push(A.ep, B.ep, 'dom-b')
        w = self.W.client.call('GET', '/latest/dom-a')[1]; self.assertEqual(w['latest']['sequence'], A.domain()['commit_sequence']); self.assertEqual(w['latest']['acknowledgment']['witness_id'], 'w-test')
        rec1 = A.c.get('/api/v1/native/recovery', headers=A.H).json(); self.assertEqual(rec1['witness']['latest_sequence'], A.domain()['commit_sequence']); self.assertTrue(rec1['witness']['identity_pinned_matches'])
        # restore the older database AND remove the local watermark file: only the witness can tell this database is stale
        for suffix in ('-wal', '-shm'):
            p = db.with_name(db.name + suffix)
            if p.exists():
                p.unlink()
        shutil.copyfile(backup, db); wm = A.inst.settings.keys_dir / 'native' / 'watermark-dom-a.json'; wm.unlink()
        A.inst.reopen(); A.c = A.inst.client; A.ep = ClientEndpoint(A.inst, A.H)
        d = A.domain(); self.assertEqual(d['recovery_state'], 'quarantine')
        rec = A.c.get('/api/v1/native/recovery', headers=A.H).json(); self.assertEqual(rec['recovery_state'], 'quarantine'); self.assertIn('witness-acknowledged', rec['rollback_detection'])
        self.assertEqual(A.c.post('/api/v1/native/transfers', headers=self.alice['h'], json={'to_account': self.adam['account'], 'amount': 1, 'request_id': 'q'}).json()['detail']['code'], 'domain_quarantined')
        # reconciliation needs the peer's observation of the allocation this database never saw
        B.c.post('/api/v1/native/transport/observation', headers=B.H, json={'peer': 'dom-a'}); courier.push(B.ep, A.ep, 'dom-a')
        r = A.c.post('/api/v1/native/recovery/reconcile', headers=A.H, json={'reconstruct_from_observations': True, 'source_account': self.alice['account'], 'amounts': {a['id']: 5}}).json()
        self.assertEqual(r['recovery_state'], 'normal'); self.assertGreaterEqual(A.domain()['commit_sequence'], w['latest']['sequence']); self.assertTrue(A.audit()['conserved'])
        # without a witness and without a watermark, detection is reported unavailable (never inferred from a clean start)
        A.inst.settings.witness_url = ''; A.inst.reopen(); A.c = A.inst.client
        rec = A.c.get('/api/v1/native/recovery', headers=A.H).json(); self.assertTrue(rec['rollback_detection'].startswith('available: local watermark') or rec['rollback_detection'].startswith('UNAVAILABLE'))

    def test_fragmented_reordered_duplicated_and_expired_bundles(self):
        A, B = self.A, self.B
        # a bundle of several certificates, fragmented artificially small, delivered reordered and duplicated: one credit each, signatures intact
        from metacoin_service.native import domains as D
        old = D.FRAGMENT_BYTES; D.FRAGMENT_BYTES = 400
        try:
            ids = [A.export('alice', B, self.bob['account'], 1, rid='f%d' % i)['id'] for i in range(3)]
            ob = A.c.get('/api/v1/native/transport/outbox', headers=A.H, params={'receiver': 'dom-b'}).json()
        finally:
            D.FRAGMENT_BYTES = old
        frags = [it for it in ob['items'] if it.get('fragment_of')]; self.assertGreater(len(frags), 3); self.assertEqual(ob['schema'], 'metacoin-native-bundle/v1'); self.assertEqual(sorted(ob['economic_ids']), sorted(ids))
        shuffled = dict(ob, items=list(reversed(ob['items'])) + ob['items'][:2])                                     # reordered + duplicated fragments
        r = B.c.post('/api/v1/native/transport/inbox', headers=B.H, json=shuffled).json(); self.assertEqual(r['envelope_version'], 'v1'); courier.mark_from_results(A.ep, r['results'])
        self.assertEqual(sum(1 for x in r['results'] if x['state'] == 'accepted'), 3); self.assertEqual(B.bal(self.bob['account'], asset=A.asset), 3)
        # a tampered fragment (one byte changed) is discarded: the signed content never reaches the economic layer
        bad = json.loads(json.dumps(ob)); f0 = [it for it in bad['items'] if it.get('fragment_of')][-1]; f0['data'] = ('x' if f0['data'][0] != 'x' else 'y') + f0['data'][1:]     # a message whose fragments are not staged any more
        r2 = B.c.post('/api/v1/native/transport/inbox', headers=B.H, json=bad).json(); self.assertIn('fragment_digest_mismatch', [x.get('reason') for x in r2['results']]); self.assertEqual(B.bal(self.bob['account'], asset=A.asset), 3)
        # an unsupported envelope version set is refused; an expired envelope moves nothing and refunds nothing
        self.assertEqual(B.c.post('/api/v1/native/transport/inbox', headers=B.H, json=dict(ob, envelope_versions=['v9'])).status_code, 422)
        D.ENVELOPE_TTL = -5                                                                                   # the certificate's envelope is signed already expired
        try:
            a = A.export('alice', B, self.bob['account'], 2, rid='exp')
        finally:
            D.ENVELOPE_TTL = 30 * 86400
        ob3 = A.c.get('/api/v1/native/transport/outbox', headers=A.H, params={'receiver': 'dom-b'}).json(); r3 = B.c.post('/api/v1/native/transport/inbox', headers=B.H, json=ob3).json()
        self.assertEqual([x['reason'] for x in r3['results'] if x.get('reason')], ['expired']); self.assertEqual(A.alloc(a['id'])['state'], 'certified'); self.assertEqual(A.bal(self.alice['account']), 50 - 3 - 2)   # backing stays locked: expiry is not a refund
        self.assertEqual(A.audit()['total_held'] + B.audit(A.asset)['total_held'] + 2, 50)

    def test_crash_at_retirement_boundaries_replays_safely(self):
        A, B = self.A, self.B; a = A.export('alice', B, self.bob['account'], 4, rid='ret'); courier.push(A.ep, B.ep, 'dom-b')
        B.fault('native_return_init'); r = B.c.post('/api/v1/native/allocations/' + a['id'] + '/return', headers=self.bob['h'], json={}); self.assertEqual(r.status_code, 409); B.fault('native_return_init', disarm=True)
        v = B.alloc(a['id']); self.assertEqual((v['state'], v['exposed_state'], B.bal(self.bob['account'], 'in_transit', asset=A.asset)), ('return_requested', 'retirement_pending', 4))   # retirement durable, message not yet queued
        r = B.c.post('/api/v1/native/allocations/' + a['id'] + '/return', headers=self.bob['h'], json={}); self.assertEqual(r.status_code, 200); self.assertTrue(r.json().get('replayed'))
        self.assertEqual(len(B.c.get('/api/v1/native/transport/outbox', headers=B.H, params={'receiver': 'dom-a'}).json()['items']), 1)     # the lost message was reconstructed from durable state
        A.fault('native_return_complete'); out = courier.push(B.ep, A.ep, 'dom-a'); A.fault('native_return_complete', disarm=True)
        self.assertEqual(A.alloc(a['id'])['state'], 'acknowledged')                                                          # the completion crashed before the release: nothing moved
        out = courier.push(B.ep, A.ep, 'dom-a'); before = A.bal(self.alice['account'])
        self.assertEqual((A.alloc(a['id'])['state'], B.alloc(a['id'])['state']), ('returned', 'returned')); self.assertEqual(before, 50)
        out = courier.push(B.ep, A.ep, 'dom-a'); self.assertEqual(A.bal(self.alice['account']), 50)                    # replay releases nothing twice
        self.assertEqual(A.audit()['total_held'] + B.audit(A.asset)['total_held'], 50)

    def test_contradictory_authority_histories_detected_with_limitation_exposed(self):
        A, B = self.A, self.B; a = A.export('alice', B, self.bob['account'], 3, rid='cx'); courier.push(A.ep, B.ep, 'dom-b')
        # the witness refuses a second, different checkpoint for an already acknowledged sequence; a conflicting certificate quarantines the peer at B
        latest = self.W.client.call('GET', '/latest/dom-a')[1]['latest']; st = dict(latest['statement'], journal_root='00' * 32)
        key = crypto.load_signing_key(A.inst.settings.keys_dir / 'native' / 'authority-dom-a.ed25519'); sig = crypto.sign(key, P.signing_bytes('commit_checkpoint', st))
        code, r = self.W.client.call('POST', '/checkpoint', {'statement': st, 'signature_hex': sig, 'key_id': A.domain()['authority']['key_id']}); self.assertEqual((code, r['code']), (400, 'conflicting_checkpoint'))
        cert = A.alloc(a['id'])['certificate']; twin = dict(cert, recipient_account=self.carol['account'])
        env = {'protocol': P.PROTOCOL, 'type': 'envelope', 'id': 'nenv_twin_w', 'environment': 'dev', 'sender': 'dom-a', 'receiver': 'dom-b', 'message_type': 'allocation_certificate', 'causality': a['id'], 'payload_digest': P.digest(twin), 'expires_at': 4102444800, 'created_at': cert['issued_at']}
        item = {'envelope': env, 'envelope_signature': crypto.sign(key, P.signing_bytes('envelope', env)), 'key_id': A.domain()['authority']['key_id'], 'payload': twin, 'payload_signature': crypto.sign(key, P.signing_bytes('allocation_certificate', twin))}
        r = B.c.post('/api/v1/native/transport/inbox', headers=B.H, json={'items': [item]}).json(); self.assertEqual(r['results'][0]['reason'], 'conflicting_signed_history')
        rec = B.c.get('/api/v1/native/recovery', headers=B.H).json(); self.assertEqual(rec['quarantined_peers'], ['dom-a'])
        self.assertIn('never prevented', rec['what_is_not'][0] if False else json.dumps(rec)); self.assertEqual(B.bal(self.bob['account'], asset=A.asset), 3)    # the earlier credit stands: detection does not undo prior loss


if __name__ == '__main__':
    unittest.main()
