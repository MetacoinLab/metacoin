"""Result reuse (explicit, keyed by inputs digest + verifier digest) and selective sharing with canaries."""
import json
import unittest
from metacoin_service.tests.test_service import Instance, own_inputs
from metacoin_service import db as database


class ReuseTests(unittest.TestCase):
    def setUp(self):
        self.inst = Instance(); self.addCleanup(self.inst.close); self.c = self.inst.client; self.H = self.inst.h('owner')

    def test_explicit_reuse_hits_only_identical_inputs_under_the_same_verifier(self):
        a = self.inst.job()
        cb = self.inst.contract(title='same inputs, other title')
        self.assertIsNone(self.c.get('/api/v1/reuse/lookup?contract_id=' + cb, headers=self.H).json()['hit'])       # nothing succeeded yet
        self.inst.worker().run_once()
        hit = self.c.get('/api/v1/reuse/lookup?contract_id=' + cb, headers=self.H).json()
        self.assertEqual(hit['hit']['job_id'], a)
        # default submission recomputes; explicit reuse commits immediately, bound to the original evidence
        plain = self.c.post('/api/v1/jobs', headers=self.H, json={'contract_id': self.inst.contract(title='plain')}).json()
        self.assertEqual((plain['state'], plain['reused_from']), ('queued', None))
        reused = self.c.post('/api/v1/jobs', headers=self.H, json={'contract_id': cb, 'reuse': True}).json()
        self.assertEqual((reused['state'], reused['reused_from']), ('succeeded', a))
        original = self.c.get('/api/v1/jobs/' + a, headers=self.H).json()
        self.assertEqual((reused['evidence_root'], reused['outcome'], reused['summary']), (original['evidence_root'], original['outcome'], original['summary']))
        lineage = self.c.get('/api/v1/lineage/job/' + reused['id'], headers=self.H).json()
        self.assertIn('reused_result', json.dumps(lineage))
        events = self.c.get('/api/v1/jobs/' + reused['id'] + '/history', headers=self.H).json()
        self.assertIn('"recomputed": false', json.dumps(events).replace('"recomputed":false', '"recomputed": false'))
        # different inputs: no hit; a deleted original payload: no hit
        cc = self.inst.contract(inputs=own_inputs('OTHER'))
        self.assertIsNone(self.c.get('/api/v1/reuse/lookup?contract_id=' + cc, headers=self.H).json()['hit'])
        self.c.delete('/api/v1/artifacts/' + original['evidence_artifact_id'] if 'evidence_artifact_id' in original else '/api/v1/artifacts/none', headers=self.H)
        with database.Database(self.inst.settings.db_path).tx() as db:
            aid = db.execute('SELECT evidence_artifact_id FROM jobs WHERE id=?', (a,)).fetchone()[0]
        self.assertEqual(self.c.delete('/api/v1/artifacts/' + aid, headers=self.H).status_code, 200)
        cd = self.inst.contract(title='after deletion')
        self.assertIn('deleted', self.c.get('/api/v1/reuse/lookup?contract_id=' + cd, headers=self.H).json()['reason'])
        self.assertEqual(self.c.post('/api/v1/jobs', headers=self.H, json={'contract_id': cd, 'reuse': True}).json()['state'], 'queued')   # falls back to computing


class SharingTests(unittest.TestCase):
    def setUp(self):
        self.inst = Instance(); self.addCleanup(self.inst.close); self.c = self.inst.client; self.H = self.inst.h('owner')
        self.jid = self.inst.job()
        self.inst.worker().run_once()
        self.viewer = self.inst.ids['viewer']

    def test_projection_contains_exactly_the_granted_fields_and_no_canary(self):
        V = self.inst.h('viewer')
        owner_view = self.c.get('/api/v1/jobs/' + self.jid, headers=self.H).json()
        summary_keys = sorted(owner_view['summary'])
        self.assertTrue(len(summary_keys) >= 2, summary_keys)
        shared_key, hidden_key = summary_keys[0], summary_keys[-1]
        # before any grant: the viewer sees a withheld outcome and no projection
        self.assertEqual(self.c.get('/api/v1/jobs/' + self.jid, headers=V).json()['outcome'], 'withheld-by-policy-or-not-yet-reviewed')
        self.assertEqual(self.c.get('/api/v1/jobs/' + self.jid + '/projection', headers=V).status_code, 403)
        # grants are validated: unknown fields, absent summary keys, viewers cannot grant
        self.assertEqual(self.c.post('/api/v1/jobs/' + self.jid + '/shares', headers=self.H, json={'grantee_id': self.viewer, 'fields': ['inputs']}).json()['detail']['code'], 'field_not_projectable')
        self.assertEqual(self.c.post('/api/v1/jobs/' + self.jid + '/shares', headers=self.H, json={'grantee_id': self.viewer, 'fields': ['summary:nope']}).json()['detail']['code'], 'summary_key_absent')
        self.assertEqual(self.c.post('/api/v1/jobs/' + self.jid + '/shares', headers=V, json={'grantee_id': self.viewer, 'fields': ['outcome']}).status_code, 403)
        g = self.c.post('/api/v1/jobs/' + self.jid + '/shares', headers=self.H, json={'grantee_id': self.viewer, 'fields': ['outcome', 'evidence_root', 'summary:' + shared_key]})
        self.assertEqual(g.status_code, 201, g.text)
        proj = self.c.get('/api/v1/jobs/' + self.jid + '/projection', headers=V).json()
        self.assertEqual(sorted(proj['fields']), sorted(['outcome', 'evidence_root', 'summary:' + shared_key]))
        self.assertEqual(proj['fields']['outcome'], owner_view['outcome'])
        text = json.dumps(proj)
        for canary in ('USER_PRIVATE_LABEL_4471', 'summary:' + hidden_key if hidden_key != shared_key else 'NO_SUCH', 'available_low', 'contract_digest', 'model_id'):
            self.assertNotIn(canary, text, canary)
        # the reviewer (not a grantee) gets nothing; the events carry field names, never values
        self.assertEqual(self.c.get('/api/v1/jobs/' + self.jid + '/projection', headers=self.inst.h('reviewer')).status_code, 403)
        hist = self.c.get('/api/v1/jobs/' + self.jid + '/history', headers=self.H).json()
        rows = hist if isinstance(hist, list) else next(v for v in hist.values() if isinstance(v, list))
        granted = [e for e in rows if e['event_type'] == 'sharing.granted'][0]
        self.assertEqual(set(json.loads(granted['ref_json']) if 'ref_json' in granted else granted['ref']), {'fields', 'grantee_id', 'share_id'})   # names only, never values
        # the signed bundle verifies against this service; tampering breaks it
        ok = self.c.post('/api/v1/projections/verify', headers=V, json={'bundle': proj['bundle']}).json()
        self.assertEqual((ok['signature_valid'], ok['issuer_is_this_service'], ok['fields']), (True, True, sorted(proj['fields'])))
        tampered = json.loads(json.dumps(proj['bundle'])); tampered['statement']['fields']['outcome'] = 'ROBUSTLY_FEASIBLE_FAKE'
        self.assertFalse(self.c.post('/api/v1/projections/verify', headers=V, json={'bundle': tampered}).json()['signature_valid'])
        # revocation removes access; the owner still sees everything
        sid = g.json()['share_id']
        self.assertEqual(self.c.delete('/api/v1/shares/' + sid, headers=V).status_code, 403)
        self.assertTrue(self.c.delete('/api/v1/shares/' + sid, headers=self.H).json()['revoked'])
        self.assertEqual(self.c.get('/api/v1/jobs/' + self.jid + '/projection', headers=V).status_code, 403)
        self.assertEqual(self.c.get('/api/v1/jobs/' + self.jid + '/shares', headers=self.H).json()['items'][0]['revoked_at'] is not None, True)
        self.assertIn('summary:' + shared_key, self.c.get('/api/v1/jobs/' + self.jid + '/projection', headers=self.H).json()['fields'])


if __name__ == '__main__':
    unittest.main()
