"""Group F: versioned workflow packages, compatibility negotiation, composite quotes, verification-gated delivery and
signed result bundles with the offline verifier. Verification gating with the x402 upto path is in test_packages_upto."""
import base64
import io
import json
import tempfile
import unittest
import zipfile
from unittest import mock
from pathlib import Path
from metacoin_service.tests.test_compute_engine import ComputeInstance, HAVE_RUNTIME, batch_spec
from metacoin_service.tests.test_resource_plan_service import sample
from metacoin_service import workflows as wf_mod, verify_bundle, crypto
from metacoin_service.db import Database
from experiments.private_receipts import receipt as merkle


def plan_definition(name='plan package source', **over):
    return {'schema': wf_mod.SCHEMA, 'name': name, 'outputs': ['out'], 'nodes': [
        {'id': 'plan', 'type': 'resource_plan', 'inputs': dict(sample(), **over)},
        {'id': 'out', 'type': 'export', 'depends_on': ['plan'], 'input': 'plan', 'fields': ['outcome', 'model_id', 'evidence_root', 'status']}]}


@unittest.skipUnless(HAVE_RUNTIME, 'no numpy-capable compute interpreter on this host')
class PackageTests(unittest.TestCase):
    def setUp(self):
        self.inst = ComputeInstance(); self.addCleanup(self.inst.close); self.c = self.inst.client; self.H = self.inst.h('owner')

    def post(self, path, body, status=201, headers=None):
        r = self.c.post(path, headers=headers or self.H, json=body)
        self.assertEqual(r.status_code, status, r.text)
        return r.json()

    def drive(self, rid, ticks=20):
        w = self.inst.worker()
        for _ in range(ticks):
            w.run_once(); w.tick_workflows()
            v = self.c.get('/api/v1/packages/runs/' + rid, headers=self.H).json()
            if v['state'] in ('delivered', 'unaccepted', 'failed', 'cancelled'):
                return v
        return v

    def test_versions_compatibility_quotes_gated_delivery_and_bundles(self):
        wid = self.post('/api/v1/workflows', {'definition': plan_definition()})['id']
        body = {'name': 'robust-plan', 'workflow_id': wid, 'description': 'schedule under declared bounds (example is synthetic)', 'delivery_policy': {'gate': 'required_verification', 'required_class': 'full_reference'},
                'example': {'plan': sample()}, 'disclosure_defaults': {'disclose_inputs': False, 'disclose_witness': True, 'summary_fields': None}}
        pk = self.post('/api/v1/packages', body)
        m = pk['manifest']
        self.assertEqual((pk['version'], pk['created'], m['schema'], m['stripped_inline_inputs']), (1, True, 'metacoin-workflow-package/v1', ['plan']))
        self.assertNotIn('RP_TEST', json.dumps(m['workflow'])); self.assertIn('resource_plan', m['operations']); self.assertEqual(m['operations']['resource_plan']['manifest_id'], 'robust-resource-plan/v1')
        self.assertEqual(self.post('/api/v1/packages', body)['created'], False)                         # same content, same package
        v2 = self.post('/api/v1/packages', dict(body, description='revised text'))
        self.assertEqual((v2['version'], v2['name'], v2['created']), (2, 'robust-plan', True)); self.assertNotEqual(v2['digest'], pk['digest'])
        # export is manifest-only; import checks refuse altered schema, digest, executable fields and unknown operations
        exp = self.c.get('/api/v1/packages/' + pk['id'] + '/export', headers=self.H).json()
        self.assertEqual(exp['package']['digest'], pk['digest']); self.assertNotIn('RP_TEST', json.dumps(exp))
        chk = self.post('/api/v1/packages/import', {'manifest': exp}, 200); self.assertEqual(chk['blocking'], ['operation.resource_plan.device']); self.assertFalse(chk['installed'])   # no worker live yet
        for mutate, code in ((lambda x: x.update(schema='metacoin-workflow-package/v9'), 'schema'), (lambda x: x.update(description='tampered'), 'digest_mismatch'),
                             (lambda x: x['workflow']['nodes'][0].update(script='rm -rf /'), 'digest_mismatch')):
            bad = json.loads(json.dumps(exp['package'])); mutate(bad)
            r = self.post('/api/v1/packages/import', {'manifest': bad}, 422); self.assertEqual(r['detail']['code'] if isinstance(r['detail'], dict) else r['detail'], code)
        evil = json.loads(json.dumps(exp['package'])); evil['workflow']['nodes'][0]['script'] = 'x'; evil['digest'] = merkle.canonical({k: v for k, v in evil.items() if k != 'digest'}).hex()[:0] or __import__('hashlib').sha256(merkle.canonical({k: v for k, v in evil.items() if k != 'digest'})).hexdigest()
        self.assertEqual(self.post('/api/v1/packages/import', {'manifest': evil}, 422)['detail']['code'], 'executable_field_refused')
        foreign = json.loads(json.dumps(exp['package'])); foreign['operations']['alien_kind'] = {'verifier_digest': 'x' * 64}; foreign['required_models'] = [{'model_id': 'nope/model', 'revision': 'f' * 40}]
        foreign['digest'] = __import__('hashlib').sha256(merkle.canonical({k: v for k, v in foreign.items() if k != 'digest'})).hexdigest()
        rep = self.post('/api/v1/packages/compatibility', {'manifest': foreign}, 200)
        self.assertFalse(rep['compatible']); self.assertIn('operation.alien_kind', rep['blocking']); self.assertTrue(any(r.startswith('model.nope/model') for r in rep['blocking']))
        self.assertTrue(rep['nothing_reserved'] and rep['nothing_started'] and rep['nothing_downloaded'])
        self.assertEqual(self.post('/api/v1/packages/import', {'manifest': foreign, 'apply': True}, 409)['detail']['code'], 'package_incompatible')
        # implementation digest mismatch is blocked (a different implementation is not automatically equivalent)
        other = json.loads(json.dumps(exp['package'])); other['operations']['resource_plan']['verifier_digest'] = 'a' * 64
        other['digest'] = __import__('hashlib').sha256(merkle.canonical({k: v for k, v in other.items() if k != 'digest'})).hexdigest()
        rep2 = self.post('/api/v1/packages/compatibility', {'manifest': other}, 200)
        self.assertIn('operation.resource_plan.implementation', rep2['blocking'])
        # compatibility before any worker is live: compute operations are blocked (no devices); afterwards supported
        rep0 = self.post('/api/v1/packages/compatibility', {'package_id': pk['id']}, 200)
        self.assertIn('operation.resource_plan.device', rep0['blocking'])
        w = self.inst.worker(); w.run_once()
        rep1 = self.post('/api/v1/packages/compatibility', {'package_id': pk['id']}, 200)
        self.assertTrue(rep1['compatible'], rep1['blocking'])
        # CPU-only environment for a gpu request on an exact-integer operation: declared equivalent (controlled double for the device facts)
        bwid = self.post('/api/v1/workflows', {'definition': {'schema': wf_mod.SCHEMA, 'name': 'batch pkg', 'outputs': ['o'], 'nodes': [{'id': 'b', 'type': 'temporal_batch', 'inputs': batch_spec(private_label='PKG_B')}, {'id': 'o', 'type': 'export', 'depends_on': ['b'], 'input': 'b', 'fields': ['outcome']}]}})['id']
        bpk = self.post('/api/v1/packages', {'name': 'batch-pkg', 'workflow_id': bwid, 'delivery_policy': {'gate': 'required_verification', 'required_class': 'full_exact'}})
        svc = self.c.app.state.services
        from metacoin_service.compute import service as compute_svc
        real = compute_svc.capabilities
        def cpu_only(db, settings):
            caps = real(db, settings); caps['facts']['currently_available']['live_worker_devices'] = ['cpu']; return caps
        with mock.patch('metacoin_service.compute.service.capabilities', cpu_only):
            with Database(self.inst.settings.db_path).read() as db:
                from metacoin_service.auth import Principal
                p = Principal(db.execute('SELECT * FROM principals WHERE id=?', (self.inst.ids['owner'],)).fetchone())
                rep_cpu = svc.packages.compatibility(db, p, bpk['manifest'], 'gpu')
                rep_plan = svc.packages.compatibility(db, p, pk['manifest'], 'gpu')
        st = {i['requirement']: i['status'] for i in rep_cpu['items']}
        self.assertEqual(st['operation.temporal_batch.device'], 'supported_via_declared_equivalent'); self.assertTrue(rep_cpu['compatible'])
        self.assertEqual({i['requirement']: i['status'] for i in rep_plan['items']}['operation.resource_plan.device'], 'supported_as_requested')
        # instantiate with explicit inputs; quote binds digests; a changed input is a different quote; expiry, budget refusal and tampering refuse the run
        self.assertEqual(self.post('/api/v1/packages/' + pk['id'] + '/instantiate', {'inputs': {}}, 422)['detail']['code'], 'node_inputs_required')
        inst = self.post('/api/v1/packages/' + pk['id'] + '/instantiate', {'inputs': {'plan': sample()}})
        q = self.post('/api/v1/packages/' + pk['id'] + '/quote', {'workflow_id': inst['workflow_id'], 'scheme': 'exact'})
        self.assertEqual((q['state'], q['scheme'], q['metered_amount'], len([c for c in q['components'] if c.get('quote_id')])), ('open', 'exact', 0, 1)); self.assertGreater(q['amount_max'], 0)
        self.assertEqual(q['binding']['package_digest'], pk['digest']); self.assertEqual(q['delivery_policy']['required_class'], 'full_reference')
        inst2 = self.post('/api/v1/packages/' + pk['id'] + '/instantiate', {'inputs': {'plan': dict(sample(), reserve=25000)}})
        q2 = self.post('/api/v1/packages/' + pk['id'] + '/quote', {'workflow_id': inst2['workflow_id']})
        self.assertNotEqual(q2['digest'], q['digest']); self.assertNotEqual(q2['binding']['input_digests']['plan'], q['binding']['input_digests']['plan'])
        with Database(self.inst.settings.db_path).tx() as db:
            db.execute('UPDATE package_quotes SET expires_at=1 WHERE id=?', (q2['quote_id'],))
        self.assertEqual(self.post('/api/v1/packages/' + pk['id'] + '/runs', {'quote_id': q2['quote_id']}, 409)['detail']['code'], 'quote_expired')
        q3 = self.post('/api/v1/packages/' + pk['id'] + '/quote', {'workflow_id': inst2['workflow_id']})
        with Database(self.inst.settings.db_path).tx() as db:
            b = json.loads(db.execute('SELECT binding_json FROM package_quotes WHERE id=?', (q3['quote_id'],)).fetchone()[0]); b['definition_digest'] = 'x' * 64
            db.execute('UPDATE package_quotes SET binding_json=? WHERE id=?', (json.dumps(b), q3['quote_id']))
        self.assertEqual(self.post('/api/v1/packages/' + pk['id'] + '/runs', {'quote_id': q3['quote_id']}, 409)['detail']['code'], 'quote_input_mismatch')
        ceiling_before = self.c.get('/api/v1/budget', headers=self.H).json()
        self.assertEqual(self.c.put('/api/v1/budgets/workspace', headers=self.H, json={'ceiling': 0}).status_code, 200)
        self.assertEqual(self.post('/api/v1/packages/' + pk['id'] + '/runs', {'quote_id': q['quote_id']}, 409)['detail']['code'], 'budget_refused')
        self.assertEqual(self.c.put('/api/v1/budgets/workspace', headers=self.H, json={'ceiling': 10 ** 9}).status_code, 200)
        # run under the quote: delivery gated on full_reference verification; bundle only after delivery
        run = self.post('/api/v1/packages/' + pk['id'] + '/runs', {'quote_id': q['quote_id']}, 202)
        self.assertEqual((run['state'], run['kind']), ('running', 'workflow'))
        self.assertEqual(self.c.get('/api/v1/packages/quotes/' + q['quote_id'], headers=self.H).json()['state'], 'consumed')
        self.assertEqual(self.post('/api/v1/packages/' + pk['id'] + '/runs', {'quote_id': q['quote_id']}, 409)['detail']['code'], 'quote_consumed')
        bz = self.c.post('/api/v1/packages/runs/' + run['id'] + '/bundle', headers=self.H, json={}); self.assertEqual((bz.status_code, bz.json()['detail']['code']), (409, 'not_delivered'))
        v = self.drive(run['id'])
        self.assertEqual(v['state'], 'delivered', v); self.assertEqual(v['delivery']['required_class'], 'full_reference'); self.assertEqual(list(v['verification'].values())[0]['state'], 'passed')
        self.assertIn('withheld until the required verification', v['meaning'] or '') if v['state'] == 'awaiting_verification' else None
        # the run keeps its package identity when a newer version exists; retiring blocks new instantiation only
        self.assertEqual(v['package_id'], pk['id'])
        self.post('/api/v1/packages/' + v2['id'] + '/retire', {}, 200)
        self.assertEqual(self.post('/api/v1/packages/' + v2['id'] + '/instantiate', {'inputs': {'plan': sample()}}, 409)['detail']['code'], 'package_retired')
        # a cancelled run is a cancelled package run (partial completion never delivers)
        q4 = self.post('/api/v1/packages/' + pk['id'] + '/quote', {'workflow_id': inst['workflow_id']})
        run2 = self.post('/api/v1/packages/' + pk['id'] + '/runs', {'quote_id': q4['quote_id']}, 202)
        self.assertEqual(self.c.post('/api/v1/runs/' + run2['run_id'] + '/cancel', headers=self.H).status_code, 200)
        self.assertIn(self.drive(run2['id'], 5)['state'], ('cancelled', 'failed'))
        # signed result bundle: valid; changed artifact; substituted signer; missing source; restricted projection
        full = self.post('/api/v1/packages/runs/' + run['id'] + '/bundle', {'scope': {'disclose_inputs': True, 'disclose_witness': True}}, 200)
        data = base64.b64decode(full['zip_base64'])
        pub = self.c.get('/api/v1/capabilities', headers=self.H).json().get('service_signing_public') or full['manifest'] and None
        with Database(self.inst.settings.db_path).read() as db:
            pub = db.execute("SELECT value FROM meta WHERE key='service_signing_public'").fetchone()[0]
        tmp = tempfile.mkdtemp(); path = Path(tmp) / 'bundle.zip'; path.write_bytes(data)
        rep = verify_bundle.verify(str(path), [pub], recompute=True)
        self.assertTrue(rep['archive']['ok'] and rep['manifest']['ok'] and rep['digests']['ok'] and rep['signature']['ok'], rep)
        self.assertTrue(rep['signature']['key_trusted_by_caller']); self.assertTrue(rep['verification_records']['ok'])
        wit = [c for c in rep['witness']['checks'] if c['check'].startswith('witness_replay')]; self.assertEqual((len(wit), wit[0]['ok'], wit[0]['recomputed']), (1, True, True))
        self.assertNotIn('RP_TEST', data.decode('latin-1')); self.assertNotIn('/home/', data.decode('latin-1'))
        self.assertEqual(verify_bundle.main([str(path), '--trusted-key', pub, '--recompute']), 0)
        def rezip(mutate):
            src = zipfile.ZipFile(io.BytesIO(data)); out = io.BytesIO()
            with zipfile.ZipFile(out, 'w') as z:
                for i in src.infolist():
                    content = src.read(i)
                    content, keep = mutate(i.filename, content)
                    if keep:
                        z.writestr(i, content)
            p2 = Path(tmp) / ('m%d.zip' % len(list(Path(tmp).iterdir()))); p2.write_bytes(out.getvalue()); return str(p2)
        changed = verify_bundle.verify(rezip(lambda n, c: (c.replace(b'"objective":12', b'"objective":99') if n.endswith('summary.json') else c, True)), [pub])
        self.assertFalse(changed['digests']['ok']); self.assertTrue(changed['signature']['ok'])
        self.assertIn('digest differs', json.dumps(changed['digests']['checks']))
        other_key = crypto.generate_signing_key(Path(tmp) / 'other.ed25519')
        manifest_bytes = zipfile.ZipFile(io.BytesIO(data)).read('manifest.json')
        forged_statement = merkle.canonical({'schema': 'metacoin-result-bundle/v1-statement', 'manifest_sha256': __import__('hashlib').sha256(manifest_bytes).hexdigest(), 'signature': crypto.sign(crypto.load_signing_key(Path(tmp) / 'other.ed25519'), manifest_bytes), 'public_key': other_key, 'key_id': crypto.key_id_for(other_key)})
        subst = verify_bundle.verify(rezip(lambda n, c: (forged_statement if n == 'statement.json' else c, True)), [pub])
        self.assertTrue(subst['signature']['signature_valid']); self.assertFalse(subst['signature']['key_trusted_by_caller']); self.assertIn('UNTRUSTED', subst['signature']['meaning']); self.assertFalse(subst['signature']['issuer_key_id_matches'])
        missing = verify_bundle.verify(rezip(lambda n, c: (c, not n.endswith('inputs.json'))), [pub], recompute=True)
        self.assertFalse(missing['digests']['ok']); self.assertIn('missing', json.dumps(missing['digests']['checks']))
        restricted = self.post('/api/v1/packages/runs/' + run['id'] + '/bundle', {'scope': {'disclose_inputs': False}}, 200)
        rpath = Path(tmp) / 'restricted.zip'; rpath.write_bytes(base64.b64decode(restricted['zip_base64']))
        rrep = verify_bundle.verify(str(rpath), [pub], recompute=True)
        self.assertTrue(rrep['archive']['ok'] and rrep['digests']['ok'] and rrep['signature']['ok'])
        self.assertIsNone(rrep['witness']['checks'][0]['ok']); self.assertIn('cannot be recomputed', rrep['witness']['checks'][0]['scope']); self.assertIn('Undisclosed inputs are not verified', rrep['scope']['statement'])
        self.assertNotIn('supply_low', base64.b64decode(restricted['zip_base64']).decode('latin-1'))
        # a decompression bomb / traversal member is refused before anything is parsed
        bomb = io.BytesIO()
        with zipfile.ZipFile(bomb, 'w', compression=zipfile.ZIP_DEFLATED) as z:
            z.writestr('../escape.json', b'{}'); z.writestr('manifest.json', b'0' * (2 * 1024 * 1024))
        bpath = Path(tmp) / 'bomb.zip'; bpath.write_bytes(bomb.getvalue())
        brep = verify_bundle.verify(str(bpath), [pub])
        self.assertFalse(brep['archive']['ok']); self.assertIsNone(brep['manifest'])
        # console pages and the viewer boundary
        s = self.c.post('/api/v1/session', json={'token': self.inst.tok['owner']}); cookies = {'metacoin_session': s.cookies['metacoin_session']}
        self.assertEqual(self.c.get('/console/packages', cookies=cookies).status_code, 200)
        page = self.c.get('/console/packages/' + pk['id'], cookies=cookies); self.assertEqual(page.status_code, 200); self.assertIn('signed result bundle', page.text); self.assertIn('delivered', page.text)
        csrf = s.json()['csrf']
        cf = self.c.post('/console/packages/' + pk['id'] + '/compatibility', cookies=cookies, data={'csrf': csrf, 'device_policy': ''})
        self.assertEqual(cf.status_code, 200, cf.text[:300]); self.assertIn('supported_as_requested', cf.text); self.assertIn('nothing reserved', cf.text)
        qf = self.c.post('/console/packages/' + pk['id'] + '/instantiate', cookies=cookies, data={'csrf': csrf, 'inputs': json.dumps({'plan': sample()}), 'scheme': 'exact', 'action': 'quote'})
        self.assertEqual(qf.status_code, 200, qf.text[:300]); self.assertIn('Composite quote', qf.text)
        self.assertEqual(self.c.post('/api/v1/packages/runs/' + run['id'] + '/bundle', headers=self.inst.h('viewer'), json={}).status_code, 403)
        self.assertEqual(self.c.post('/api/v1/packages', headers=self.inst.h('viewer'), json=body).status_code, 403)


if __name__ == '__main__':
    unittest.main()
