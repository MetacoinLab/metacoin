"""Portable provider client (group H; journeys 33–34 at unit level): a provider enrolled through the external signer publishes an
offer, activates and delivers a compute reservation executed on its own host, is paid, pays through the signer, fetches its
receipts and verifies them offline under pinned roots; a mission bundle verifies offline and every tampering is caught.
The client talks to a separate API process over HTTP only (no server imports, no database)."""
import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

import httpx

from metacoin_service.tests.test_native_signer import SignerProcess
from metacoin_service.tests.test_service import ENV, ROOT, Instance, free_port

sys.path.insert(0, str(ROOT / 'provider_client'))
from metacoin_provider.client import Provider          # noqa: E402
from metacoin_provider import verify as V              # noqa: E402

PY = sys.executable


class ProviderClientTests(unittest.TestCase):
    def setUp(self):
        self.inst = Instance(provider_mode='simulation'); self.addCleanup(self.inst.close); self.port = free_port(); self.base = 'http://127.0.0.1:%d' % self.port
        self.env = dict(ENV, METACOIN_X402_DISABLED='1', METACOIN_FACILITATOR_URL='http://127.0.0.1:1/blocked', METACOIN_LIMITS_JSON=json.dumps({'test_hooks': 1}))
        self.api = subprocess.Popen([PY, '-m', 'metacoin_service', '--home', str(self.inst.home), '--provider-mode', 'simulation', 'serve', '--port', str(self.port)], cwd=ROOT, env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: (self.api.terminate(), self.api.wait(timeout=20)))
        for _ in range(300):
            try:
                if httpx.get(self.base + '/api/health', timeout=1).status_code == 200:
                    break
            except Exception:
                time.sleep(0.1)
        self.http = httpx.Client(base_url=self.base, timeout=120); self.H = {'Authorization': 'Bearer ' + self.inst.tok['owner']}
        self.admin_cred = Path(self.inst.temp.name) / 'admin.json'; self.admin_cred.write_text(json.dumps({'token': self.inst.tok['owner']})); os.chmod(self.admin_cred, 0o600)
        d = self.http.post('/api/v1/native/domain', headers=self.H, json={'domain_id': 'dom-pc', 'operator': 'test', 'host': 'this host'}); self.assertEqual(d.status_code, 201, d.text); self.dom = d.json(); self.asset = self.dom['default_asset']
        self.sg = SignerProcess(domains=('dom-pc',)); self.addCleanup(self.sg.close)
        r = self.http.post('/api/v1/native/actors', headers=self.H, json={'name': 'requester'}).json(); self.req = {'account': r['account_id'], 'h': {'Authorization': 'Bearer ' + r['credential']['token']}}
        self.http.post('/api/v1/native/fixtures/fund', headers=self.H, json={'account_id': self.req['account'], 'amount': 5000})
        self.cred = str(Path(self.inst.temp.name) / 'prov.json')
        self.enr = Provider.enroll(self.base, str(self.admin_cred), self.sg.base, str(self.sg.home / 'token'), 'prov-1', 500, self.cred)
        self.P = Provider(self.base, self.cred, signer=self.sg.base, signer_token_file=str(self.sg.home / 'token'), actor='prov-1')
        self.executor = [PY, '-m', 'metacoin_service.lunar', 'task']; os.environ['PYTHONPATH'] = str(ROOT)

    def bal(self, acct, cat='available'):
        return self.http.get('/api/v1/native/accounts/' + acct, headers=self.H).json()[cat]

    def test_offer_to_receipt_cycle_and_offline_verification(self):
        self.assertFalse(self.enr['private_key_held_by_service']); self.assertEqual(os.stat(self.cred).st_mode & 0o777, 0o600)
        t = int(time.time()); o = self.P.publish_offer('compute', 'cpu-prov', 100000, 1, t - 1, t + 7200, key='offer-1'); self.assertEqual(o['provider_account'], self.enr['account_id'])
        res = self.http.post('/api/v1/resources/reservations', headers=self.req['h'], json={'offer_id': o['id'], 'offer_version': o['version'], 'quantity': 3000, 'request_id': 'r-1'}); self.assertEqual(res.status_code, 201, res.text); rid = res.json()['id']
        self.assertEqual([x['id'] for x in self.P.reservations()['items']], [rid])
        self.P.activate(rid, key='act-1'); d = self.P.deliver_compute(rid, {'kind': 'terrain_tile', 'source': 'apollo11_32'}, self.executor, key='del-1')
        m = d['reservation']['measurement']; self.assertEqual((m['measurement']['kind'], m['measurement']['executed_by'], m['verification']['kind'], m['verification']['ok']), ('provider_assertion', 'provider', 'verified', True))
        self.assertIn('not verified', m['verification']['method']); self.assertEqual(d['reservation']['delivered_quantity'], min(d['execution']['cpu_ms'], 3000)); owed = d['reservation']['delivered_quantity']
        s = self.http.post('/api/v1/resources/reservations/' + rid + '/settle', headers=self.req['h'], json={}).json(); self.assertEqual(s['state'], 'settled'); self.assertEqual(self.P.account()['available'], owed)
        # signed payment through the signer: the provider pays a small amount back; the service holds no key for it
        p = self.P.pay(self.req['account'], 5, 'pay-1'); self.assertEqual((p['state'], p['history'][1]['custody']), ('settled', 'external-signer')); self.assertEqual(self.P.account()['available'], owed - 5)
        self.assertFalse(list((self.inst.settings.keys_dir / 'native').glob('actor-' + self.enr['account_id'] + '*')))
        # receipts fetched over the public API, verified offline with the pure-Python verifier under roots pinned at enrolment time
        roots = self.P.roots_from_domain(); mine = self.P.my_receipts(); self.assertGreaterEqual(len(mine), 2)
        rep = V.verify_receipts(mine, roots, self.dom['id']); self.assertTrue(all(c['ok'] for c in rep), rep)
        bad = json.loads(json.dumps(mine)); bad[0]['statement']['claims']['amount'] = 999999; self.assertFalse(V.verify_receipts(bad, roots, self.dom['id'])[0]['ok'])
        self.assertFalse(V.verify_receipts(mine, {}, self.dom['id'])[0]['ok'])
        ex = self.P.export(); rep = V.verify_export(ex, roots); self.assertTrue(rep['ok'], [c for c in rep['checks'] if not c['ok']])
        # the CLI entry point of the client works as a separate process with only the standard library
        cli = subprocess.run([sys.executable, '-m', 'metacoin_provider', '--base', self.base, '--cred', self.cred, 'account'], cwd=str(ROOT / 'provider_client'), env={'PATH': os.environ['PATH']}, capture_output=True, text=True, timeout=60)
        self.assertEqual(cli.returncode, 0, cli.stderr); self.assertEqual(json.loads(cli.stdout)['id'], self.enr['account_id'])

    def test_mission_bundle_verifies_offline_and_rejects_tampering(self):
        t = int(time.time()); o = self.P.publish_offer('compute', 'cpu-prov', 100000, 1, t - 1, t + 7200)
        g = {'milestones': [{'key': 'cpu', 'kind': 'resource', 'depends': [], 'spec': {'offer_kind': 'compute', 'quantity': 2000, 'max_price_per_unit': 1}}]}
        m = self.http.post('/api/v1/missions', headers=self.req['h'], json={'title': 'bundle', 'graph': g, 'policy': {}}); self.assertEqual(m.status_code, 201, m.text); mid = m.json()['id']
        self.http.post('/api/v1/missions/' + mid + '/freeze', headers=self.req['h'], json={}); s = self.http.post('/api/v1/missions/' + mid + '/step', headers=self.req['h'], json={}).json(); self.assertEqual(s['performed']['action'], 'reserve', s); rid = s['result']['reservation_id']
        self.P.activate(rid); self.P.deliver_compute(rid, {'kind': 'terrain_tile', 'source': 'apollo11_32'}, self.executor)
        s = self.http.post('/api/v1/missions/' + mid + '/step', headers=self.req['h'], json={}).json(); self.assertEqual(s['performed']['action'], 'settle_reservation', s)
        b = self.P.mission_bundle(mid); roots = self.P.roots_from_domain(); self.assertEqual(b['schema'], 'metacoin-mission-bundle/v1')
        rep = V.verify_bundle(b, roots); self.assertTrue(rep['ok'], [c for c in rep['checks'] if not c['ok']]); self.assertGreaterEqual(len(b['content']['receipts']), 1); self.assertGreaterEqual(len(b['content']['intents']), 1)
        led = b['content']['ledger']; self.assertEqual(led['totals']['spent'], s['result']['delivered_quantity'])
        def tam(fn):
            x = json.loads(json.dumps(b)); fn(x); r = V.verify_bundle(x, roots); return [c['check'] for c in r['checks'] if not c['ok']]
        self.assertIn('content_digest', tam(lambda x: x['content']['ledger']['milestones'].__setitem__(0, dict(x['content']['ledger']['milestones'][0], spent=1))))
        self.assertIn('ledger_spent_matches_settled_intents', tam(lambda x: x['content']['ledger']['milestones'][0].update(spent=1)))
        self.assertIn('receipt:' + b['content']['receipts'][0]['id'], tam(lambda x: x['content']['receipts'][0]['statement']['claims'].update(amount=1)))
        self.assertIn('entries_balanced', tam(lambda x: x['content']['journal_entries'][0]['postings'][0].update(debit=x['content']['journal_entries'][0]['postings'][0]['debit'] + 1)))
        self.assertIn('seal_root_pinned', tam.__call__(lambda x: None) if False else [c['check'] for c in V.verify_bundle(b, {})['checks'] if not c['ok']])
        # the CLI verifies the saved bundle without base or credential
        bp = Path(self.inst.temp.name) / 'bundle.json'; rp = Path(self.inst.temp.name) / 'roots.json'; bp.write_text(json.dumps(b)); rp.write_text(json.dumps(roots))
        cli = subprocess.run([sys.executable, '-m', 'metacoin_provider', 'verify', str(bp), '--roots', str(rp)], cwd=str(ROOT / 'provider_client'), env={'PATH': os.environ['PATH']}, capture_output=True, text=True, timeout=60)
        self.assertEqual(cli.returncode, 0, cli.stderr); self.assertTrue(json.loads(cli.stdout)['ok'])


if __name__ == '__main__':
    unittest.main()
