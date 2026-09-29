"""Approval policies (bound content + revision, independent approver, expiry, stale revision, revoked approver,
duplicate decision, failed apply) and consolidated usage statements (grouping by asset/network/environment, no
double charge on repeated viewing, pagination, CSV export, synthetic labelling)."""
import json
import unittest

from metacoin_service.db import Database, now
from metacoin_service.tests.test_compute_engine import ComputeInstance, HAVE_RUNTIME, batch_spec
from metacoin_service.tests.test_models import HAVE_TORCH, installed, GEN, EMB, ModelInstance


class ApprovalTests(unittest.TestCase):
    def setUp(self):
        self.inst = ModelInstance(); self.addCleanup(self.inst.close); self.c = self.inst.client; self.H = self.inst.h('owner'); self.R = self.inst.h('reviewer')

    def test_proposal_binding_independence_expiry_stale_and_apply(self):
        # policy: node enrollment and scheduling toggles need approval
        pol = self.c.post('/api/v1/approvals/policy', headers=self.H, json={'required': ['node_enroll', 'scheduling_toggle']}).json()
        self.assertEqual(pol['required'], ['node_enroll', 'scheduling_toggle'])
        self.assertEqual(self.c.post('/api/v1/calibration/scheduling', headers=self.H, json={'enabled': False}).json()['detail']['code'], 'approval_required')
        content = {'enabled': False}
        pr = self.c.post('/api/v1/approvals', headers=self.H, json={'action': 'scheduling_toggle', 'content': content, 'note': 'disable learned ranking'})
        self.assertEqual(pr.status_code, 201, pr.text); pid = pr.json()['id']
        self.assertEqual((pr.json()['state'], pr.json()['stale']), ('proposed', False))
        # the proposer cannot approve; another credential of the same principal is still the same principal
        self.assertEqual(self.c.post('/api/v1/approvals/' + pid + '/approve', headers=self.H, json={}).json()['detail']['code'], 'same_principal')
        scoped = self.c.post('/api/v1/credentials', headers=self.H, json={'operations': ['job:read'], 'expires_in_seconds': 600}).json()
        self.assertEqual(self.c.post('/api/v1/approvals/' + pid + '/approve', headers={'Authorization': 'Bearer ' + scoped['token']}, json={}).status_code, 403)
        # a different principal (reviewer) approves; duplicate identical decision is idempotent
        ap = self.c.post('/api/v1/approvals/' + pid + '/approve', headers=self.R, json={'note': 'ok'}).json()
        self.assertEqual((ap['state'], ap['approved_by']), ('approved', self.inst.ids['reviewer']))
        self.assertEqual(self.c.post('/api/v1/approvals/' + pid + '/approve', headers=self.R, json={}).json()['state'], 'approved')
        self.assertEqual(self.c.post('/api/v1/approvals/' + pid + '/reject', headers=self.R, json={}).status_code, 409)
        # apply executes exactly once; a second apply is idempotent
        out = self.c.post('/api/v1/approvals/' + pid + '/apply', headers=self.H, json={}).json()
        self.assertEqual((out['state'], out['apply_result']['calibrated_scheduling_enabled']), ('applied', False))
        self.assertEqual(self.c.post('/api/v1/approvals/' + pid + '/apply', headers=self.H, json={}).json()['state'], 'applied')
        self.assertFalse(self.c.get('/api/v1/calibration/scheduling', headers=self.H).json()['calibrated_scheduling_enabled'])
        # changed content after approval: the object's revision moved -> stale, never applied
        pr2 = self.c.post('/api/v1/approvals', headers=self.H, json={'action': 'scheduling_toggle', 'content': {'enabled': True}}).json()
        self.c.post('/api/v1/approvals/' + pr2['id'] + '/approve', headers=self.R, json={})
        with Database(self.inst.settings.db_path).tx() as db:      # the toggled object changes underneath (operator edit outside the proposal)
            db.execute("UPDATE meta SET value='1' WHERE key='calibrated_scheduling_enabled'")
        r2 = self.c.post('/api/v1/approvals/' + pr2['id'] + '/apply', headers=self.H, json={})
        self.assertEqual((r2.status_code, r2.json()['refused']['code'], r2.json()['state']), (409, 'stale_revision', 'stale'))
        self.assertEqual(self.c.get('/api/v1/approvals/' + pr2['id'], headers=self.H).json()['state'], 'stale')
        # expiry: an expired proposal cannot be approved
        pr3 = self.c.post('/api/v1/approvals', headers=self.H, json={'action': 'scheduling_toggle', 'content': {'enabled': False}}).json()
        with Database(self.inst.settings.db_path).tx() as db:
            db.execute('UPDATE approvals SET expires_at=? WHERE id=?', (now() - 1, pr3['id']))
        self.assertEqual(self.c.post('/api/v1/approvals/' + pr3['id'] + '/approve', headers=self.R, json={}).json()['code'], 'EXPIRED')
        # node enrollment through approval: the applied operation returns the node credential once; direct enrollment stays gated
        key = __import__('metacoin_service.crypto', fromlist=['_ed'])._ed.Ed25519PrivateKey.generate()
        from cryptography.hazmat.primitives import serialization
        pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
        self.assertEqual(self.c.post('/api/v1/nodes', headers=self.H, json={'name': 'gated', 'public_key_hex': pub}).json()['detail']['code'], 'approval_required')
        pr4 = self.c.post('/api/v1/approvals', headers=self.H, json={'action': 'node_enroll', 'content': {'name': 'gated', 'public_key_hex': pub, 'capabilities': ['temporal_batch'], 'devices': ['cpu']}}).json()
        self.c.post('/api/v1/approvals/' + pr4['id'] + '/approve', headers=self.R, json={})
        applied = self.c.post('/api/v1/approvals/' + pr4['id'] + '/apply', headers=self.H, json={}).json()
        self.assertTrue(applied['apply_result']['node_id'].startswith('nd_'))
        self.assertEqual(self.c.get('/api/v1/nodes/' + applied['apply_result']['node_id'], headers=self.H).json()['capabilities'], ['temporal_batch'])
        # revoked approver: approval by a principal revoked before apply is refused
        pr5 = self.c.post('/api/v1/approvals', headers=self.H, json={'action': 'scheduling_toggle', 'content': {'enabled': False}}).json()
        self.c.post('/api/v1/approvals/' + pr5['id'] + '/approve', headers=self.R, json={})
        with Database(self.inst.settings.db_path).tx() as db:
            db.execute('UPDATE principals SET revoked_at=? WHERE id=?', (now(), self.inst.ids['reviewer']))
        r5 = self.c.post('/api/v1/approvals/' + pr5['id'] + '/apply', headers=self.H, json={})
        self.assertEqual((r5.status_code, r5.json()['refused']['code']), (403, 'approver_revoked'))
        # application failure after approval keeps the record with the error
        with Database(self.inst.settings.db_path).tx() as db:
            db.execute('UPDATE principals SET revoked_at=NULL WHERE id=?', (self.inst.ids['reviewer'],))
        pr6 = self.c.post('/api/v1/approvals', headers=self.H, json={'action': 'model_promote', 'content': {'revision_id': 'mr_missing000000', 'operation': 'generate'}}).json()
        self.c.post('/api/v1/approvals/' + pr6['id'] + '/approve', headers=self.R, json={})
        r6 = self.c.post('/api/v1/approvals/' + pr6['id'] + '/apply', headers=self.H, json={})
        self.assertEqual((r6.status_code, r6.json()['state']), (404, 'apply_failed'))
        self.assertEqual(self.c.get('/api/v1/approvals/' + pr6['id'], headers=self.H).json()['state'], 'apply_failed')
        items = self.c.get('/api/v1/approvals', headers=self.inst.h('viewer')).json()['items']
        self.assertGreaterEqual(len(items), 6)


@unittest.skipUnless(HAVE_RUNTIME, 'no numpy-capable compute interpreter on this host')
class StatementTests(unittest.TestCase):
    def test_statement_groups_rows_and_never_charges_on_view(self):
        inst = ComputeInstance(); self.addCleanup(inst.close); c = inst.client; H = inst.h('owner')
        sid = next(s['id'] for s in c.get('/api/v1/services', headers=H).json()['items'] if s['kind'] == 'temporal_batch')
        q = c.post('/api/v1/services/' + sid + '/quote', headers=H, json={'inputs': batch_spec(private_label='STMT')}).json()
        c.post('/api/v1/quotes/' + q['quote_id'] + '/accept', headers=H)
        inv = c.post('/api/v1/services/' + sid + '/invoke', headers=H, json={'quote_id': q['quote_id'], 'inputs': batch_spec(private_label='STMT')}).json()
        q2 = c.post('/api/v1/services/' + sid + '/quote', headers=H, json={'inputs': batch_spec(private_label='RESERVED')}).json()
        c.post('/api/v1/quotes/' + q2['quote_id'] + '/accept', headers=H)
        w = inst.worker(); self.addCleanup(w.offline); self.assertEqual(w.run_once()[1], 'succeeded')
        st = c.get('/api/v1/statements', headers=H).json()
        s = st['statement']
        self.assertEqual(s['environment'], 'synthetic-local'); self.assertEqual(s['total_rows'], 2)
        types = {r['type']: r for r in s['rows']}
        self.assertEqual(types['usage']['assessed_charge'], 364); self.assertEqual(types['usage']['reserved_max'], 364); self.assertEqual(types['reserved_ceiling']['assessed_charge'], 0)
        g = next(iter(s['groups'].values()))
        self.assertEqual((g['assessed_total'], g['reserved_total'], g['settled_confirmed_total']), (364, 728, 0))
        self.assertIn('never revenue', s['meaning'])
        again = c.get('/api/v1/statements', headers=H).json()['statement']
        self.assertEqual(again['total_rows'], 2)                                    # viewing twice adds nothing
        self.assertEqual(len(c.get('/api/v1/usage', headers=H).json()['items']), 1)
        csv = c.get('/api/v1/statements.csv', headers=H).text
        self.assertIn('assessed_charge', csv.splitlines()[0]); self.assertEqual(len(csv.strip().splitlines()), 3)
        self.assertEqual(c.get('/api/v1/statements?page=2', headers=H).json()['statement']['rows'], [])
        self.assertEqual(c.get('/api/v1/statements', headers=inst.h('viewer')).status_code, 403)
        self.assertEqual(c.get('/api/v1/statements?since=-1', headers=H).status_code, 422)
        self.assertNotIn('STMT', json.dumps(st))
