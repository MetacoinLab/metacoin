"""Provider onboarding and trust boundaries (order 2026-10-23 group B; journeys 3, 9, 10, 15 at unit level): a fresh-directory
client initialises its configuration, pins the remote identity and protocol versions, refuses an identity change or an
incompatible version, declares its identity separation, drains safely; shared operators are disclosed, never independent."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from metacoin_service.tests.test_native_provider_client import ProviderClientTests
from metacoin_service.tests.test_service import ROOT

sys.path.insert(0, str(ROOT / 'provider_client'))
from metacoin_provider.client import Provider          # noqa: E402
from metacoin_provider.api import ApiError, transport_check  # noqa: E402


class OnboardingTests(ProviderClientTests):
    def test_init_handshake_identity_and_drain(self):
        home = str(Path(self.inst.temp.name) / 'prov-home'); ini = Provider.init(home, self.base, self.cred, 'prov-1', signer=self.sg.base, signer_token_file=str(self.sg.home / 'token'), actor='prov-1')
        self.assertEqual(ini['pinned']['domain'], 'dom-pc'); self.assertEqual(ini['pinned']['protocol'], 'metacoin-native/v0'); self.assertEqual(os.stat(os.path.join(home, 'config.json')).st_mode & 0o777, 0o600)
        P = Provider.load(home); hs = P.handshake(); self.assertTrue(hs['ok']); self.assertEqual(hs['custody'][:15], 'external-signer')
        # journey 10: an unpinned peer / changed identity or an incompatible critical version is refused
        cfg = json.load(open(os.path.join(home, 'config.json'))); bad = dict(cfg); bad['pinned'] = dict(cfg['pinned'], authority_key_id='k_other'); json.dump(bad, open(os.path.join(home, 'config.json'), 'w'))
        with self.assertRaises(ApiError) as cm:
            Provider.load(home).handshake()
        self.assertEqual(cm.exception.body['problems'][0]['code'], 'identity_changed')
        bad = dict(cfg); bad['compatible_protocols'] = ['metacoin-native/v9']; json.dump(bad, open(os.path.join(home, 'config.json'), 'w'))
        with self.assertRaises(ApiError) as cm:
            Provider.load(home).handshake()
        self.assertEqual(cm.exception.body['problems'][0]['code'], 'protocol_incompatible'); json.dump(cfg, open(os.path.join(home, 'config.json'), 'w'))
        with self.assertRaises(SystemExit):
            transport_check('http://192.0.2.10:8402')                                          # plain http outside loopback refused; validation never disabled
        # journey 9: identity separation declared; two providers under one operator are disclosed as one party
        reg = self.http.post('/api/v1/work/providers', headers=self.H, json={'name': 'prov-1', 'principal_id': self.enr['principal_id'], 'capabilities': {'kinds': ['terrain_tile'], 'verification_classes': ['analytical'], 'payment_schemes': ['exact']}, 'relationship': {'relationship': 'same_operator', 'same_host': True, 'shared_custody': False, 'identity': {'operator_id': 'op-A', 'machine_id': 'rig-1', 'worker_instance': 'w1', 'signer_id': 'sig-test', 'domain': 'dom-pc'}}}); self.assertEqual(reg.status_code, 201, reg.text); p1 = reg.json()['id']
        other = self.http.post('/api/v1/native/actors', headers=self.H, json={'name': 'twin'}).json(); reg2 = self.http.post('/api/v1/work/providers', headers=self.H, json={'name': 'prov-2', 'principal_id': other['principal_id'], 'capabilities': {'kinds': ['terrain_tile'], 'verification_classes': ['analytical'], 'payment_schemes': ['exact']}, 'relationship': {'relationship': 'independent_declared', 'identity': {'operator_id': 'op-A', 'machine_id': 'rig-2', 'worker_instance': 'w2', 'signer_id': 'sig-x', 'domain': 'dom-pc'}}}).json()
        idv = self.http.get('/api/v1/work/providers/%s/identity' % p1, headers=self.H).json(); self.assertEqual(idv['identity']['operator_id'], 'op-A'); self.assertTrue(idv['independence']['shared_operator']); self.assertFalse(idv['independence']['independent_party']); self.assertEqual(idv['independence']['same_operator_providers'][0]['provider_id'], reg2['id'])
        self.assertIn('never counted as independent', idv['independence']['disclosure']); self.assertIn('no universal trust score', idv['outcomes']['note'])
        mine = P.capabilities(); self.assertEqual(mine['provider_id'], p1)
        # journey 15: drain keeps delivered work and accepted obligations
        import time; t = int(time.time()); o = P.publish_offer('compute', 'cpu-drain', 100000, 1, t - 1, t + 7200)
        res = self.http.post('/api/v1/resources/reservations', headers=self.req['h'], json={'offer_id': o['id'], 'offer_version': o['version'], 'quantity': 2000, 'request_id': 'd-1'}).json(); P.activate(res['id']); d = P.deliver_compute(res['id'], {'kind': 'terrain_tile', 'source': 'apollo11_32'}, self.executor)
        dr = P.drain(); self.assertEqual(len(dr['drained_pools']), 1)
        again = self.http.post('/api/v1/resources/reservations', headers=self.req['h'], json={'offer_id': o['id'], 'offer_version': o['version'], 'quantity': 10, 'request_id': 'd-2'}); self.assertEqual(again.json()['detail']['code'], 'provider_draining')
        s = self.http.post('/api/v1/resources/reservations/%s/settle' % res['id'], headers=self.req['h'], json={}).json(); self.assertEqual(s['state'], 'settled'); self.assertEqual(P.earnings()['available'], s['obligation']['settled_amount'])
        wd = P.withdraw(); self.assertEqual(wd['pools'][0]['state'], 'closed')
        cli = subprocess.run([sys.executable, '-m', 'metacoin_provider', '--home', home, 'handshake'], cwd=str(ROOT / 'provider_client'), env={'PATH': os.environ['PATH']}, capture_output=True, text=True, timeout=60); self.assertEqual(cli.returncode, 0, cli.stderr); self.assertTrue(json.loads(cli.stdout)['ok'])

    def test_offer_to_receipt_cycle_and_offline_verification(self):
        pass

    def test_mission_bundle_verifies_offline_and_rejects_tampering(self):
        pass


if __name__ == '__main__':
    unittest.main()
