"""Catalog, quotes, usage, discovery, and the paid invocation over real TCP from a separate client process."""
import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from experiments.work_contracts import fixtures
from metacoin_service.tests.test_service import Instance, free_port, ROOT, ENV
from metacoin_service import catalog


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.inst = Instance(); self.addCleanup(self.inst.close); self.c = self.inst.client
        self.services = {s['kind']: s for s in self.c.get('/api/v1/services', headers=self.inst.h('viewer')).json()['items']}

    def test_catalog_lists_installed_services_with_separate_status_facts(self):
        self.assertEqual(sorted(self.services), ['calibration_fit', 'document_import', 'energy_audit', 'heat_diffusion', 'knowledge_answer', 'knowledge_index', 'legacy_task_replay', 'monte_carlo_reliability', 'plan_comparison', 'resource_plan', 'safe_runtime', 'task_selection', 'temporal_batch', 'temporal_energy', 'text_embedding', 'text_generation', 'verification_audit'])
        for s in self.services.values():
            self.assertEqual((s['state']['registered'], s['state']['installed'], s['state']['available'], s['state']['externally_validated']), (True, True, True, False))
            self.assertTrue(s['state']['verifier_matches_installed'])
        detail = self.c.get('/api/v1/services/' + self.services['temporal_energy']['id'], headers=self.inst.h('viewer')).json()
        self.assertEqual(detail['input_schema']['properties']['capacity']['unit'], 'mJ')
        self.assertEqual(detail['operations'], ['validate', 'quote', 'invoke'])
        self.assertIn('salted Merkle', detail['privacy']['public_verification'])
        filtered = self.c.get('/api/v1/services?input_type=temporal_series', headers=self.inst.h('viewer')).json()['items']
        self.assertEqual([s['kind'] for s in filtered], ['temporal_energy'])
        self.assertEqual(self.c.get('/api/v1/services?model=outage-energy-bounds/v0', headers=self.inst.h('viewer')).json()['items'][0]['kind'], 'energy_audit')
        # registration is an operator action bound to installed kinds only
        self.assertEqual(self.c.post('/api/v1/services', headers=self.inst.h('viewer'), json={'name': 'x', 'kind': 'energy_audit'}).status_code, 403)
        r = self.c.post('/api/v1/services', headers=self.inst.h('owner'), json={'name': 'evil', 'kind': 'shell', 'version': 1})
        self.assertEqual((r.status_code, r.json()['detail']['code']), (422, 'kind_not_installed'))
        r = self.c.post('/api/v1/services', headers=self.inst.h('owner'), json={'name': 'energy-audit-pro', 'kind': 'energy_audit', 'version': 2, 'price_per_unit': 3})
        self.assertEqual(r.status_code, 201)
        sid2 = r.json()['id']
        self.assertEqual(self.c.post('/api/v1/services/' + sid2 + '/retire', headers=self.inst.h('owner')).json()['retired'], sid2)
        self.assertNotIn(sid2, [s['id'] for s in self.c.get('/api/v1/services', headers=self.inst.h('viewer')).json()['items']])
        self.assertIn(sid2, [s['id'] for s in self.c.get('/api/v1/services?include_retired=1', headers=self.inst.h('viewer')).json()['items']])
        self.assertEqual(self.c.post('/api/v1/services/' + sid2 + '/quote', headers=self.inst.h('owner'), json={'inputs': fixtures.inputs()}).status_code, 409)

    def test_quote_bindings(self):
        sid = self.services['energy_audit']['id']
        inputs = fixtures.inputs()
        bad = self.c.post('/api/v1/services/' + sid + '/validate', headers=self.inst.h('viewer'), json={'inputs': dict(inputs, reserve=-1)})
        self.assertEqual(bad.status_code, 422)
        q = self.c.post('/api/v1/services/' + sid + '/quote', headers=self.inst.h('owner'), json={'inputs': inputs, 'quantity_max': 2}).json()
        self.assertEqual((q['amount_max'], q['unit'], q['state'], q['provider_mode']), (2, 'evaluation', 'offered', 'simulation'))
        qid = q['quote_id']
        # principal substitution: the viewer cannot accept the owner's quote; another owner-role credential cannot consume it
        self.assertEqual(self.c.post('/api/v1/quotes/' + qid + '/accept', headers=self.inst.h('reviewer')).status_code, 403)
        self.assertEqual(self.c.post('/api/v1/services/' + sid + '/invoke', headers=self.inst.h('owner'), json={'quote_id': qid, 'inputs': inputs}).status_code, 409)   # not accepted yet
        self.assertEqual(self.c.post('/api/v1/quotes/' + qid + '/accept', headers=self.inst.h('owner')).json()['state'], 'accepted')
        self.assertEqual(self.c.post('/api/v1/quotes/' + qid + '/accept', headers=self.inst.h('owner')).json()['state'], 'accepted')     # repeated acceptance idempotent
        # modified quantity / inputs: request digest differs
        r = self.c.post('/api/v1/services/' + sid + '/invoke', headers=self.inst.h('owner'), json={'quote_id': qid, 'inputs': dict(inputs, reserve=1)})
        self.assertEqual(r.json()['code'], 'BINDING_MISMATCH')
        # unsupported asset in production mode: quoting refuses
        self.assertEqual(self.c.post('/api/v1/services/' + sid + '/quote', headers=self.inst.h('owner'), json={'inputs': inputs, 'provider_mode': 'production'}).status_code, 501)
        # price change between discovery and execution: a new version does not alter the accepted quote; retiring the quoted service refuses consumption
        q2 = self.c.post('/api/v1/services/' + sid + '/quote', headers=self.inst.h('owner'), json={'inputs': inputs}).json()
        self.c.post('/api/v1/quotes/' + q2['quote_id'] + '/accept', headers=self.inst.h('owner'))
        self.c.post('/api/v1/services/' + sid + '/retire', headers=self.inst.h('owner'))
        self.assertEqual(self.c.post('/api/v1/services/' + sid + '/invoke', headers=self.inst.h('owner'), json={'quote_id': q2['quote_id'], 'inputs': inputs}).status_code, 409)
        # expiry race: an expired accepted quote cannot be consumed
        with __import__('metacoin_service.db', fromlist=['Database']).Database(self.inst.settings.db_path).tx() as db:
            db.execute("UPDATE services SET status='registered', retired_at=NULL WHERE id=?", (sid,))
            db.execute('UPDATE quotes SET expires_at=1 WHERE id=?', (qid,))
        self.assertEqual(self.c.post('/api/v1/services/' + sid + '/invoke', headers=self.inst.h('owner'), json={'quote_id': qid, 'inputs': inputs}).json()['code'], 'EXPIRED')
        q3 = self.c.post('/api/v1/services/' + sid + '/quote', headers=self.inst.h('owner'), json={'inputs': inputs}).json()
        with __import__('metacoin_service.db', fromlist=['Database']).Database(self.inst.settings.db_path).tx() as db:
            db.execute('UPDATE quotes SET expires_at=1 WHERE id=?', (q3['quote_id'],))
        self.assertEqual(self.c.post('/api/v1/quotes/' + q3['quote_id'] + '/accept', headers=self.inst.h('owner')).json()['code'], 'EXPIRED')
        # successful consumption is unique, and a usage record follows completion
        q4 = self.c.post('/api/v1/services/' + sid + '/quote', headers=self.inst.h('owner'), json={'inputs': inputs}).json()
        self.c.post('/api/v1/quotes/' + q4['quote_id'] + '/accept', headers=self.inst.h('owner'))
        out = self.c.post('/api/v1/services/' + sid + '/invoke', headers=self.inst.h('owner'), json={'quote_id': q4['quote_id'], 'inputs': inputs}).json()
        self.assertEqual(out['state'], 'queued')
        self.assertEqual(self.c.post('/api/v1/services/' + sid + '/invoke', headers=self.inst.h('owner'), json={'quote_id': q4['quote_id'], 'inputs': inputs}).status_code, 409)
        self.inst.worker().run_once()
        self.inst.worker().run_once()                                     # a second pass must not create a second usage record
        usage = self.c.get('/api/v1/usage', headers=self.inst.h('owner')).json()['items']
        self.assertEqual(len(usage), 1)
        u = usage[0]
        self.assertEqual((u['quantity'], u['assessed_charge'], u['signature_valid'], u['state']), (1, 1, True, 'assessed'))
        self.assertEqual(u['statement']['job_id'], out['job_id'])
        self.assertEqual(u['states']['provider_settlement'], 'none recorded')
        self.assertEqual(self.c.get('/api/v1/usage/' + u['usage_id'], headers=self.inst.h('viewer')).json()['assessed_charge'], 1)
        # a job that never completed has no usage record (failed work is never billed)
        self.assertEqual(self.c.get('/api/v1/quotes/' + q4['quote_id'], headers=self.inst.h('owner')).json()['state'], 'consumed')


class PaidInvocationOverTcp(unittest.TestCase):
    """Journey 3: discover, quote, invoke through the real local x402 transport from a client process,
    then the usage statement bound to the completed job."""

    @classmethod
    def setUpClass(cls):
        cls.inst = Instance(provider_mode='test-http'); cls.port = free_port(); cls.base = 'http://127.0.0.1:%d' % cls.port
        cls.proc = subprocess.Popen([sys.executable, '-m', 'metacoin_service', '--home', str(cls.inst.home), '--provider-mode', 'test-http', 'serve', '--port', str(cls.port)],
                                    cwd=ROOT, env=ENV, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        import httpx
        for _ in range(100):
            try:
                if httpx.get(cls.base + '/api/health', timeout=1).status_code == 200:
                    break
            except Exception:
                time.sleep(0.1)
        cls.http = httpx.Client(base_url=cls.base, timeout=30)

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate(); cls.proc.wait(timeout=10); cls.inst.close()

    def test_discover_quote_pay_invoke_and_meter(self):
        H = self.inst.h('owner')
        services = self.http.get('/api/v1/services', headers=H).json()['items']
        sid = [s for s in services if s['kind'] == 'safe_runtime'][0]['id']
        disc = self.http.get('/api/v1/services/' + sid + '/x402-discovery', headers=H).json()
        self.assertTrue(disc['available'], disc)
        self.assertTrue(disc['parsed_ok'])
        self.assertTrue(disc['resource'].endswith('/api/v1/x402/services/' + sid + '/invoke'))
        inputs = {'available_low': 1_000_000, 'available_high': 1_100_000, 'reserve': 100_000, 'fixed_segments': [{'duration': 600, 'power_low': 800, 'power_high': 1000}],
                  'variable_power_low': 100, 'variable_power_high': 250, 'duration_cap': 3600, 'units': {'energy': 'mJ', 'power': 'mW', 'duration': 's'},
                  'assumptions': ['no_recharge', 'usable_energy_at_load_boundary', 'piecewise_constant_power_bounds', 'no_unmodeled_loads'], 'provenance': 'synthetic', 'private_label': 'PAID_PRIVATE_77'}
        quote = self.http.post('/api/v1/services/' + sid + '/quote', headers=H, json={'inputs': inputs, 'quantity_max': 1}).json()
        self.assertEqual(quote['provider_mode'], 'test-http')
        self.http.post('/api/v1/quotes/' + quote['quote_id'] + '/accept', headers=H)
        # a separate client process pays over the real socket
        cred = Path(self.inst.temp.name) / 'cred.json'; cred.write_text(json.dumps({'token': self.inst.tok['owner']})); os.chmod(cred, 0o600)
        body = json.dumps({'quote_id': quote['quote_id'], 'inputs': inputs}, sort_keys=True)
        run = lambda *extra: subprocess.run([sys.executable, '-m', 'metacoin_service.tests.x402_invoke_client', self.base, sid, str(cred), body, *extra], cwd=ROOT, env=ENV, capture_output=True, text=True, timeout=60)
        # before paying: a modified body with the same quote is refused (body digest bound); a wrong amount is refused by the SDK matcher
        p = run('body'); self.assertIn(json.loads(p.stdout.strip().splitlines()[-1])['error'], ('work_contract_binding_mismatch', 'No matching payment requirements'))
        p = run('amount'); self.assertEqual(json.loads(p.stdout.strip().splitlines()[-1])['error'], 'No matching payment requirements')
        p = run(); out = json.loads(p.stdout.strip().splitlines()[-1]) if p.stdout.strip() else {'stderr': p.stderr[-500:]}
        self.assertEqual((out.get('first_status'), out.get('second_status')), (402, 202), out)
        self.assertTrue(out['settled']['success'])
        jid = out['body']['job_id']
        self.assertEqual(out['body']['state'], 'queued')
        # replay of the same payment identifier re-delivers, no second job
        p = run('replay:' + out['identifier']); again = json.loads(p.stdout.strip().splitlines()[-1])
        self.assertEqual((again['second_status'], again['body'].get('replayed'), again['body']['job_id']), (200, True, jid))
        # a fresh payment against the consumed quote is refused before any verification
        p = run(); self.assertEqual(json.loads(p.stdout.strip().splitlines()[-1])['error'], 'work_contract_quote_consumed')
        subprocess.run([sys.executable, '-m', 'metacoin_service', '--home', str(self.inst.home), '--provider-mode', 'test-http', 'worker', '--once'], cwd=ROOT, env=ENV, check=True, capture_output=True, timeout=120)
        job = self.http.get('/api/v1/jobs/' + jid, headers=H).json()
        self.assertEqual((job['state'], job['outcome']), ('succeeded', 'ROBUSTLY_FEASIBLE'))
        usage = self.http.get('/api/v1/usage', headers=H).json()['items']
        self.assertEqual(len(usage), 1)
        self.assertEqual((usage[0]['job_id'], usage[0]['assessed_charge'], usage[0]['signature_valid']), (jid, 1, True))
        self.assertEqual(usage[0]['states']['provider_settlement']['state'], 'CONFIRMED')
        self.assertNotIn('PAID_PRIVATE_77', json.dumps(usage) + p.stdout)
        self.assertEqual(self.http.get('/api/v1/usage', headers=self.inst.h('viewer')).json()['items'][0]['assessed_charge'], 1)
