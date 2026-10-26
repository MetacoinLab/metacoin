"""Operator-controlled signing (order 2026-10-23 group A; journeys 3–7): a separate signer process holds the keys; the service
holds public identities and signatures only. Enrollment, server-side substitution refused, crash/restart idempotency, rotation
with historical verification, delegation that cannot widen authority."""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from metacoin_service import crypto
from metacoin_service.native import protocol as P
from metacoin_service.native.signer import SignerClient
from metacoin_service.tests.test_native_protocol import NativeBase
from metacoin_service.tests.test_service import ROOT, ENV, free_port

PY = sys.executable


class SignerProcess:
    def __init__(self, domains=('dom-a',), fault=None):
        self.home = Path(tempfile.mkdtemp()) / 'signer'
        subprocess.run([PY, '-m', 'metacoin_service.native.signer', 'init', '--home', str(self.home), '--domains', ','.join(domains), '--signer-id', 'sig-test'], cwd=ROOT, env=ENV, check=True, capture_output=True)
        self.port = free_port(); self.base = 'http://127.0.0.1:%d' % self.port; self.proc = None; self.start(fault)
        self.client = SignerClient(self.base, str(self.home / 'token'))

    def start(self, fault=None):
        env = dict(ENV); env.pop('METACOIN_SIGNER_FAULT', None)
        if fault:
            env['METACOIN_SIGNER_FAULT'] = fault
        self.proc = subprocess.Popen([PY, '-m', 'metacoin_service.native.signer', 'serve', '--home', str(self.home), '--port', str(self.port)], cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        c = SignerClient(self.base, str(self.home / 'token'))
        for _ in range(100):
            if c.health()[0] == 200:
                return
            time.sleep(0.05)
        raise RuntimeError('signer did not start')

    def restart(self, fault=None):
        self.proc.terminate(); self.proc.wait(timeout=10); self.start(fault)

    def close(self):
        self.proc.terminate(); self.proc.wait(timeout=10)


class SignerTests(NativeBase):
    def setUp(self):
        super().setUp(); self.sg = SignerProcess(); self.addCleanup(self.sg.close)

    def enroll(self, name, ceiling=100):
        st, e = self.sg.client.call('POST', '/enroll', {'name': name, 'ceiling': ceiling}); self.assertEqual(st, 200, e)
        r = self.c.post('/api/v1/native/actors/enroll', headers=self.H, json={'name': name, 'public_hex': e['public_hex'], 'key_id': e['key_id'], 'signer_id': e['signer_id']}); self.assertEqual(r.status_code, 201, r.text); a = r.json()
        self.sg.client.call('POST', '/bind', {'actor': name, 'account': a['account_id']})
        self.actors[name] = {'account': a['account_id'], 'principal_id': a['principal_id'], 'h': {'Authorization': 'Bearer ' + a['credential']['token']}, 'key_id': e['key_id']}
        return a

    def prepare(self, who, to, amount, rid, **extra):
        r = self.c.post('/api/v1/native/transfers/prepare', headers=self.actors[who]['h'], json=dict({'to_account': to, 'amount': amount, 'request_id': rid}, **extra)); self.assertEqual(r.status_code, 200, r.text); return r.json()['message']

    def submit(self, who, msg, sig, kid, expect=201):
        r = self.c.post('/api/v1/native/transfers/submit', headers=self.actors[who]['h'], json={'message': msg, 'signature_hex': sig, 'key_id': kid}); self.assertEqual(r.status_code, expect, r.text); return r.json()

    def test_enroll_sign_outside_service_and_refuse_substitution(self):
        a = self.enroll('ext-alice'); self.assertFalse(a['private_key_held_by_service']); self.assertTrue(a['custody'].startswith('external-signer:'))
        self.assertFalse(list((self.inst.settings.keys_dir / 'native').glob('actor-' + a['account_id'] + '*')))               # journey 3: no actor key file on the server
        self.fund('ext-alice', 50)
        self.assertEqual(self.c.post('/api/v1/native/transfers', headers=self.actors['ext-alice']['h'], json={'to_account': self.actors['machine']['account'], 'amount': 1, 'request_id': 'direct'}).json()['detail']['code'], 'signature_required')
        msg = self.prepare('ext-alice', self.actors['machine']['account'], 10, 'r1'); st, sig = self.sg.client.sign('ext-alice', msg); self.assertEqual(st, 200, sig)
        out = self.submit('ext-alice', msg, sig['signature_hex'], sig['key_id']); self.assertEqual((out['state'], out['history'][1]['custody']), ('settled', 'external-signer'))
        self.assertEqual((self.bal('ext-alice'), self.bal('machine')), (40, 10))
        # journey 4: substitution after signing is refused before any value moves (recipient, amount, domain, obligation)
        msg2 = self.prepare('ext-alice', self.actors['machine']['account'], 5, 'r2'); st, sig2 = self.sg.client.sign('ext-alice', msg2)
        for field, value in (('recipient', self.actors['supplier']['account']), ('amount_ceiling', 6), ('authority', 'dom-z'), ('obligation_id', 'no_fake')):
            bad = dict(msg2, **{field: value}); r = self.submit('ext-alice', bad, sig2['signature_hex'], sig2['key_id'], expect=403 if field != 'authority' else 422)
            self.assertIn(r['detail']['code'] if isinstance(r.get('detail'), dict) else r['code'], ('signature_invalid', 'intent_authority', 'authority', 'authority_mismatch', 'VALIDATION'))
        self.assertEqual((self.bal('ext-alice'), self.bal('machine'), self.bal('supplier')), (40, 10, 0))
        ok = self.submit('ext-alice', msg2, sig2['signature_hex'], sig2['key_id']); self.assertEqual(ok['state'], 'settled'); self.assertEqual(self.bal('machine'), 15)
        # the signer refuses what the service would refuse: wrong domain, wrong policy, expired, mutated-after-prepare, unsupported field, float
        for bad, code in ((dict(msg2, authority='dom-z', request_id='x1'), 'intent'), (dict(msg2, policy_version='native-policy/v9', request_id='x2'), 'intent'), (dict(msg2, expires_at=1, request_id='x3'), 'expired'), (dict(msg2, extra_field=1, request_id='x4'), 'intent')):
            st, r = self.sg.client.sign('ext-alice', bad); self.assertEqual(st, 400, r); self.assertEqual(r['code'], code if code != 'intent' else r['code'])
        st, r = self.sg.client.call('POST', '/sign', raw=b'{"actor": "ext-alice", "intent": {"amount_ceiling": 1.5}}'); self.assertEqual(st, 400); self.assertEqual(r['code'], 'encoding')
        # exposure ceiling is the signer's own durable state, not an API balance: ceiling 100 with 15 outstanding -> another 90 refused
        msg3 = self.prepare('ext-alice', self.actors['machine']['account'], 90, 'r3'); st, r = self.sg.client.sign('ext-alice', msg3); self.assertEqual(st, 400); self.assertEqual(r['code'], 'signer_exposure_ceiling')
        cap = self.c.get('/api/v1/capabilities', headers=self.H).json()['signer']; self.assertEqual(cap['custody']['external_signer_accounts'], 1)

    def test_crash_between_signing_and_recording_adds_no_exposure(self):
        self.enroll('ext-bob', 100); self.fund('ext-bob', 30); to = self.actors['machine']['account']
        msg = self.prepare('ext-bob', to, 7, 'c1')
        self.sg.restart(fault='before_record'); st, r = self.sg.client.sign('ext-bob', msg); self.assertEqual(st, 500)
        self.sg.restart(); st, k = self.sg.client.call('GET', '/keys/ext-bob'); self.assertEqual(k['exposure_outstanding'], 0)           # nothing consumed by the crash
        st, s1 = self.sg.client.sign('ext-bob', msg); self.assertEqual(st, 200); self.assertFalse(s1['replayed'])
        st, again = self.sg.client.sign('ext-bob', msg); self.assertTrue(again['replayed']); self.assertEqual(again['signature_hex'], s1['signature_hex'])   # a lost response is answered from the durable record
        self.submit('ext-bob', msg, s1['signature_hex'], s1['key_id']); self.assertEqual(self.bal('ext-bob'), 23)
        msg2 = self.prepare('ext-bob', to, 3, 'c2'); self.sg.restart(fault='after_record'); st, r = self.sg.client.sign('ext-bob', msg2); self.assertEqual(st, 500)
        self.sg.restart(); st, s2 = self.sg.client.sign('ext-bob', msg2); self.assertEqual(st, 200); self.assertTrue(s2['replayed'])          # recorded before the crash: the same signature returns
        st, k = self.sg.client.call('GET', '/keys/ext-bob'); self.assertEqual(k['exposure_outstanding'], 10)                               # exactly the two authorizations, nothing extra
        self.submit('ext-bob', msg2, s2['signature_hex'], s2['key_id']); self.assertEqual(self.bal('ext-bob'), 20)
        dup = self.submit('ext-bob', msg2, s2['signature_hex'], s2['key_id']); self.assertTrue(dup.get('replayed'))                          # resubmission replays, never pays twice
        self.assertEqual(self.bal('ext-bob'), 20)

    def test_rotation_keeps_history_verifiable_and_blocks_old_key(self):
        a = self.enroll('ext-carol', 100); self.fund('ext-carol', 20); acct = a['account_id']; to = self.actors['machine']['account']
        msg = self.prepare('ext-carol', to, 4, 'h1'); st, s1 = self.sg.client.sign('ext-carol', msg); i1 = self.submit('ext-carol', msg, s1['signature_hex'], s1['key_id'])
        st, rot = self.sg.client.call('POST', '/rotate', {'actor': 'ext-carol', 'grace_seconds': 0}); self.assertEqual(st, 200, rot)
        r = self.c.post('/api/v1/native/accounts/%s/keys' % acct, headers=self.actors['ext-carol']['h'], json=rot['registration']); self.assertEqual(r.status_code, 201, r.text)
        keys = r.json()['keys']; self.assertEqual([k['state'] for k in keys], ['retired', 'active'])
        # the historical receipt still verifies against the key history
        old_pub = next(k['public_hex'] for k in keys if k['key_id'] == s1['key_id']); self.assertTrue(crypto.verify(old_pub, P.signing_bytes('intent', msg), s1['signature_hex']))
        # a new intent signed by the retired key (forged locally with the old key file) is refused by the service; the signer itself no longer signs with it
        msg2 = self.prepare('ext-carol', to, 2, 'h2'); old_file = next(f for f in (self.sg.home / 'keys').iterdir() if '-e1-' in f.name)
        forged = crypto.sign(crypto.load_signing_key(old_file), P.signing_bytes('intent', msg2)); r = self.submit('ext-carol', msg2, forged, s1['key_id'], expect=403); self.assertEqual(r['detail']['code'], 'key_not_authorized')
        st, s2 = self.sg.client.sign('ext-carol', msg2); self.assertEqual(s2['key_id'], rot['key_id']); self.submit('ext-carol', msg2, s2['signature_hex'], s2['key_id'])
        self.assertEqual(self.bal('ext-carol'), 14)
        # a forged registration (not signed by the current primary) is refused
        fake = dict(rot['registration']); fake['registration'] = dict(fake['registration'], key_id=crypto.key_id_for('aa' * 32), public_hex='aa' * 32, epoch=9)
        self.assertEqual(self.c.post('/api/v1/native/accounts/%s/keys' % acct, headers=self.actors['ext-carol']['h'], json=fake).status_code, 403)

    def test_delegation_cannot_widen_scope_or_authority(self):
        a = self.enroll('ext-dave', 50); self.fund('ext-dave', 50); acct = a['account_id']; m = self.actors['machine']['account']; s = self.actors['supplier']['account']
        st, big = self.sg.client.call('POST', '/delegate', {'actor': 'ext-dave', 'name': 'agent', 'recipients': [m], 'ceiling': 60}); self.assertEqual((st, big['code']), (400, 'delegation_exceeds_authority'))
        st, d = self.sg.client.call('POST', '/delegate', {'actor': 'ext-dave', 'name': 'agent', 'recipients': [m], 'ceiling': 20}); self.assertEqual(st, 200, d)
        r = self.c.post('/api/v1/native/accounts/%s/keys' % acct, headers=self.actors['ext-dave']['h'], json=d['registration']); self.assertEqual(r.status_code, 201, r.text)
        msg = self.prepare('ext-dave', s, 5, 'd1'); st, r = self.sg.client.sign('ext-dave', msg, delegate=d['key_id']); self.assertEqual(r['code'], 'delegate_recipient_out_of_scope')
        msg = self.prepare('ext-dave', m, 15, 'd2'); st, s1 = self.sg.client.sign('ext-dave', msg, delegate=d['key_id']); self.assertEqual(st, 200); self.submit('ext-dave', msg, s1['signature_hex'], s1['key_id'])
        msg = self.prepare('ext-dave', m, 10, 'd3'); st, r = self.sg.client.sign('ext-dave', msg, delegate=d['key_id']); self.assertEqual(r['code'], 'delegate_ceiling')
        # even a signature produced somehow with the delegate key is bounded server-side too
        dfile = next(f for f in (self.sg.home / 'keys').iterdir() if 'delegate-agent' in f.name); forged = crypto.sign(crypto.load_signing_key(dfile), P.signing_bytes('intent', msg))
        r = self.submit('ext-dave', msg, forged, d['key_id'], expect=400); self.assertEqual(r['detail']['code'], 'delegate_ceiling')
        msg = self.prepare('ext-dave', s, 1, 'd4'); forged = crypto.sign(crypto.load_signing_key(dfile), P.signing_bytes('intent', msg)); r = self.submit('ext-dave', msg, forged, d['key_id'], expect=403); self.assertEqual(r['detail']['code'], 'delegate_recipient_out_of_scope')
        self.assertEqual(self.bal('ext-dave'), 35)


if __name__ == '__main__':
    unittest.main()
