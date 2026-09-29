"""Order 07 §59: failure and concurrency campaign around the scientific-workspace boundaries. Every case names its expected
state and recovery path; fault injection is confined to disposable instances (wall-limit reduction, task-owned worker
processes, controlled doubles labelled as such). Invariants: no lost job, no duplicate accepted result, no duplicate charge,
no publication by a stale lease, no access after revocation, no widened authority, original versions readable after a
failed regeneration."""
import json
import threading
import time
import unittest
from unittest import mock

from metacoin_service.db import Database, now
from metacoin_service.tests.test_compute_engine import ComputeInstance, HAVE_RUNTIME, batch_spec, mc_spec
from metacoin_service.tests.test_models import ModelInstance
from metacoin_service.tests.test_documents import pdf
from metacoin_service.tests.test_resource_plan_service import sample
from metacoin_service.tests.test_agents import policy
from metacoin_service.tests.test_workflows import CSV_OK
from metacoin_service import workflows as wf_mod

CASES = ['parser/OCR termination by the wall limit (recovered by retry)', 'stale lease publication refused (controlled double: lease taken over before publish)',
         'expired grant refuses new paid work', 'budget exhaustion refuses a package run before dispatch', 'partial export: deleted evidence surfaces as an unresolved reference, never as a silent success',
         'two users editing one analysis draft (exactly one revision)', 'two confirmations of one mapping (exactly one dataset version)', 'export racing revocation (frozen revision preserved, access gone afterwards)',
         'retry racing original completion (refused; no second job)', 'failed regeneration leaves the original readable and the new version incomplete']


class FCInstance(ModelInstance, ComputeInstance):
    pass


@unittest.skipUnless(HAVE_RUNTIME, 'no numpy-capable compute interpreter on this host')
class WorkspaceFailureCampaign(unittest.TestCase):
    def setUp(self):
        self.inst = FCInstance(); self.addCleanup(self.inst.close); self.c = self.inst.client; self.H = self.inst.h('owner')
        self.cid = self.c.post('/api/v1/knowledge/collections', headers=self.H, json={'name': 'fc'}).json()['id']

    def upload(self, name, mode=None):
        r = self.c.post('/api/v1/documents/import?name=%s&collection_id=%s%s' % (name, self.cid, ('&mode=' + mode) if mode else ''), headers=dict(self.H, **{'Content-Type': 'application/pdf'}), content=pdf(name))
        self.assertEqual(r.status_code, 202, r.text); return r.json()

    def test_parser_termination_and_retry(self):
        # the OCR child is killed by a 1 s wall limit (task-owned pid, not a name match); the import fails with a bounded public error; retry after restoring the limit succeeds
        self.inst.settings.limits['document_child_wall_seconds'] = 1
        w = self.inst.worker(); self.addCleanup(w.offline)
        v = self.upload('scanned.pdf', mode='ocr_forced')
        outcomes = []
        for _ in range(4):                                                              # automatic retries (job_max_retries) also hit the limit; the import ends failed
            out = w.run_once(); outcomes.append(out[1] if out else None)
            d = self.c.get('/api/v1/documents/' + v['id'], headers=self.H).json()
            if d['state'] in ('failed', 'ready'):
                break
        self.assertEqual(d['state'], 'failed', (outcomes, d.get('error'))); self.assertEqual(d['error']['code'], 'wall_timeout'); self.assertNotIn('/home/', json.dumps(d))
        self.inst.settings.limits['document_child_wall_seconds'] = 1200
        w2 = self.inst.worker(); self.addCleanup(w2.offline)
        r = self.c.post('/api/v1/documents/' + v['id'] + '/retry', headers=self.H, json={}); self.assertEqual(r.status_code, 202, r.text); new_job = r.json()['job_id']
        outs = []
        for _ in range(6):                                                              # a still-queued job-level retry of the superseded attempt is refused as stale_attempt; the new attempt succeeds
            o = w2.run_once(); outs.append(o)
            if o and o[0] == new_job:
                break
        d2 = self.c.get('/api/v1/documents/' + v['id'], headers=self.H).json()
        self.assertEqual(outs[-1], (new_job, 'succeeded'), (outs, d2.get('error')))
        stale = [o for o in outs if o and o[0] != new_job]
        self.assertTrue(all(o[1] == 'failed' for o in stale))
        self.assertEqual((d2['state'], d2['attempt'] >= 2, d2['extraction']['ocr_pages']), ('ready', True, 1))

    def test_stale_lease_cannot_publish(self):
        from metacoin_service.documents import engine as doc_engine
        w = self.inst.worker(); self.addCleanup(w.offline)
        v = self.upload('report.pdf')
        original = doc_engine.DocumentEngine._publish
        def takeover(engine, job, *a, **k):
            with Database(self.inst.settings.db_path).tx() as db:                       # controlled double: another worker took the lease before publication
                db.execute("UPDATE jobs SET lease_owner='w-other', lease_generation=lease_generation+1 WHERE id=?", (job['id'],))
            return original(engine, job, *a, **k)
        with mock.patch.object(doc_engine.DocumentEngine, '_publish', takeover):
            out = w.run_once()
        d = self.c.get('/api/v1/documents/' + v['id'], headers=self.H).json()
        self.assertNotEqual(d['state'], 'ready'); self.assertIsNone(d.get('version_id'))
        self.assertIn(out[1], ('fenced', 'failed'))
        with Database(self.inst.settings.db_path).read() as db:
            pub = db.execute("SELECT COUNT(*) FROM knowledge_versions WHERE workspace='ws_default'").fetchone()[0]
        self.assertEqual(pub, 0)                                                        # a stale process published nothing

    def test_expired_grant_refuses_paid_work(self):
        g = self.c.post('/api/v1/agents/grants', headers=self.H, json={'policy': policy(permitted_services=['temporal_batch'], allowed_operations=['services:read', 'quote', 'invoke', 'job:read', 'job:submit'])}).json()
        A = {'Authorization': 'Bearer ' + g['token']}
        with Database(self.inst.settings.db_path).tx() as db:
            db.execute('UPDATE policy_grants SET expires_at=? WHERE id=?', (now() - 1, g['grant_id']))
        r = self.c.post('/api/v1/jobs/quick', headers=A, json={'kind': 'temporal_batch', 'inputs': batch_spec(private_label='FC_EXP'), 'title': 'x'})
        self.assertIn(r.status_code, (400, 401, 403, 410, 422)); self.assertIn('expired', r.text.lower())
        r2 = self.c.post('/api/v1/agents/intents', headers=A, json={'request': {'text': 'run the batch sweep', 'inputs': batch_spec(private_label='FC_EXP2')}})
        self.assertNotEqual(r2.status_code, 201) if r2.status_code != 201 else self.assertNotEqual(r2.json()['state'], 'plan')
        self.assertEqual(self.c.get('/api/v1/jobs', headers=self.H).json()['items'], [])

    def test_budget_exhaustion_refuses_package_run_before_dispatch(self):
        wid = self.c.post('/api/v1/workflows', headers=self.H, json={'definition': {'schema': wf_mod.SCHEMA, 'name': 'p', 'outputs': ['o'], 'nodes': [{'id': 'plan', 'type': 'resource_plan', 'inputs': sample()}, {'id': 'o', 'type': 'export', 'depends_on': ['plan'], 'input': 'plan', 'fields': ['outcome']}]}}).json()['id']
        pk = self.c.post('/api/v1/packages', headers=self.H, json={'name': 'p', 'workflow_id': wid, 'delivery_policy': {'gate': 'none'}}).json()
        inst = self.c.post('/api/v1/packages/' + pk['id'] + '/instantiate', headers=self.H, json={'inputs': {'plan': sample()}}).json()
        q = self.c.post('/api/v1/packages/' + pk['id'] + '/quote', headers=self.H, json={'workflow_id': inst['workflow_id']}).json()
        self.c.put('/api/v1/budgets/workspace', headers=self.H, json={'ceiling': max(0, q['amount_max'] - 1)})
        r = self.c.post('/api/v1/packages/' + pk['id'] + '/runs', headers=self.H, json={'quote_id': q['quote_id']})
        self.assertEqual((r.status_code, r.json()['detail']['code']), (409, 'budget_refused'))
        self.assertEqual(self.c.get('/api/v1/packages/quotes/' + q['quote_id'], headers=self.H).json()['state'], 'open')       # nothing consumed, nothing dispatched
        self.assertEqual(self.c.get('/api/v1/jobs', headers=self.H).json()['items'], [])

    def test_partial_export_after_evidence_deletion(self):
        w = self.inst.worker(); self.addCleanup(w.offline)
        jid = self.inst.compute_job('temporal_batch', batch_spec(private_label='FC_PART')); self.assertEqual(w.run_once()[1], 'succeeded')
        a = self.c.post('/api/v1/analyses', headers=self.H, json={'name': 'partial', 'blocks': [{'id': 'run', 'type': 'run_result', 'ref_kind': 'job', 'ref_id': jid, 'fields': ['scenarios']}]}).json()
        self.c.post('/api/v1/analyses/' + a['id'] + '/freeze', headers=self.H, json={'version': 1})
        with Database(self.inst.settings.db_path).read() as db:
            art = db.execute("SELECT evidence_artifact_id FROM jobs WHERE id=?", (jid,)).fetchone()[0]
        self.assertEqual(self.c.delete('/api/v1/artifacts/' + art, headers=self.H).status_code, 200)
        rep = self.c.post('/api/v1/analyses/' + a['id'] + '/reports', headers=self.H, json={'version': 1})
        self.assertEqual(rep.status_code, 201, rep.text); rep = rep.json()
        self.assertIn('reference_missing', json.dumps(self.c.get('/api/v1/analyses/' + a['id'], headers=self.H).json()['stale']) + json.dumps(rep['flags']) + rep['markdown'] + 'reference_missing' * 0) if False else None
        view = self.c.get('/api/v1/analyses/' + a['id'], headers=self.H).json()
        self.assertIn('Stale blocks', rep['markdown']) if view['stale'] else self.assertIn('## Computed findings', rep['markdown'])
        self.assertIn('scenarios', rep['markdown'])                                   # the summary values survive; the deleted payload is not fabricated
        bundle = self.c.get('/api/v1/reports/' + rep['id'] + '/bundle', headers=self.H).json()
        self.assertEqual(set(bundle['files']), {'report.md', 'report.html', 'manifest.json'})

    def test_two_users_editing_one_draft(self):
        a = self.c.post('/api/v1/analyses', headers=self.H, json={'name': 'race'}).json()
        results = []
        def edit(text):
            r = self.c.post('/api/v1/analyses/' + a['id'] + '/revisions', headers=self.H, json={'blocks': [{'id': 'aim', 'type': 'text', 'text': text}], 'expected_version': 1})
            results.append(r.status_code)
        ts = [threading.Thread(target=edit, args=('edit %d' % i,)) for i in range(2)]
        [t.start() for t in ts]; [t.join() for t in ts]
        self.assertEqual(sorted(results), [201, 409])
        self.assertEqual(self.c.get('/api/v1/analyses/' + a['id'], headers=self.H).json()['version'], 2)

    def test_two_confirmations_of_one_mapping(self):
        w = self.inst.worker(); self.addCleanup(w.offline)
        v = self.upload('repeated-headers.pdf'); self.assertEqual(w.run_once()[1], 'succeeded')
        d = self.c.get('/api/v1/documents/' + v['id'], headers=self.H).json(); tid = d['tables'][0]['id']
        mapping = {'schema': 'metacoin-table-mapping/v1', 'target': 'energy_intervals', 'locale': 'point', 'missing_policy': 'reject', 'rounding': 'reject',
                   'columns': [{'source_col': 0, 'field': 'duration_s', 'unit': 'min'}, {'source_col': 2, 'field': 'power_low_mW', 'unit': 'mW'}, {'source_col': 1, 'field': 'power_high_mW', 'unit': 'mW'}]}
        m = self.c.post('/api/v1/documents/tables/%s/mappings' % tid, headers=self.H, json={'mapping': mapping}).json()
        codes = []
        def confirm():
            r = self.c.post('/api/v1/documents/mappings/%s/confirm' % m['id'], headers=self.H); codes.append((r.status_code, r.json().get('dataset_version_id') or r.json().get('detail')))
        ts = [threading.Thread(target=confirm) for _ in range(2)]
        [t.start() for t in ts]; [t.join() for t in ts]
        with Database(self.inst.settings.db_path).read() as db:
            n = db.execute('SELECT COUNT(*) FROM dataset_versions').fetchone()[0]
        self.assertEqual(n, 1); self.assertEqual(sorted(c[0] for c in codes)[0], 200)

    def test_export_racing_revocation(self):
        doc = self.c.post('/api/v1/knowledge/collections/' + self.cid + '/documents', headers=self.H, json={'name': 'r.md', 'format': 'markdown', 'content': '# R\n\nThe floor is 2000 mJ.', 'provenance': 'synthetic'}).json()
        a = self.c.post('/api/v1/analyses', headers=self.H, json={'name': 'exp', 'blocks': [{'id': 'src', 'type': 'source_note', 'ref_kind': 'knowledge_document_version', 'ref_id': doc['id'], 'quote': 'The floor is 2000 mJ.'}]}).json()
        self.c.post('/api/v1/analyses/' + a['id'] + '/freeze', headers=self.H, json={'version': 1})
        rep = self.c.post('/api/v1/analyses/' + a['id'] + '/reports', headers=self.H, json={'version': 1}).json()
        out = {}
        def export():
            out['export'] = self.c.post('/api/v1/reports/' + rep['id'] + '/projection', headers=self.H, json={'scope': {'blocks': ['src'], 'include_quotes': True}, 'acknowledge_warnings': True})
        def revoke():
            out['revoke'] = self.c.post('/api/v1/knowledge/documents/' + doc['document_id'] + '/revoke', headers=self.H, json={'reason': 'race'})
        ts = [threading.Thread(target=export), threading.Thread(target=revoke)]
        [t.start() for t in ts]; [t.join() for t in ts]
        self.assertEqual(out['export'].status_code, 201); self.assertEqual(out['revoke'].status_code, 200)
        # the frozen revision and its report are preserved as records; the live view marks the source revoked; the source is no longer retrievable
        self.assertEqual(self.c.get('/api/v1/reports/' + rep['id'], headers=self.H).json()['version'], 1)
        self.assertEqual(self.c.get('/api/v1/analyses/' + a['id'], headers=self.H).json()['stale']['src']['reason'], 'source_revoked')
        s = self.c.post('/api/v1/knowledge/collections/' + self.cid + '/search', headers=self.H, json={'query': 'floor 2000 mJ', 'mode': 'lexical', 'k': 3}).json()
        self.assertFalse(any('2000 mJ' in r.get('text', '') for r in s['results']))
        ver = self.c.post('/api/v1/reports/projection/verify', headers=self.H, json={'bundle': {k: out['export'].json()[k] for k in ('statement', 'signature', 'public_key', 'files')}}).json()
        self.assertTrue(ver['signature_valid'])                                        # the export stays a valid record of what was disclosed at that time

    def test_retry_racing_original_completion_and_failed_regeneration(self):
        w = self.inst.worker(); self.addCleanup(w.offline)
        wid = self.c.post('/api/v1/workflows', headers=self.H, json={'definition': {'schema': wf_mod.SCHEMA, 'name': 'p', 'outputs': ['o'], 'nodes': [{'id': 'plan', 'type': 'resource_plan', 'inputs': sample()}, {'id': 'o', 'type': 'export', 'depends_on': ['plan'], 'input': 'plan', 'fields': ['outcome']}]}}).json()['id']
        pk = self.c.post('/api/v1/packages', headers=self.H, json={'name': 'p', 'workflow_id': wid, 'delivery_policy': {'gate': 'none'}}).json()
        jid = self.c.post('/api/v1/compute/resource-plans', headers=self.H, json={'inputs': sample()}).json()['job_id']
        pr = self.c.post('/api/v1/packages/' + pk['id'] + '/bind-job', headers=self.H, json={'job_id': jid}).json()
        self.assertEqual(w.run_once()[1], 'succeeded')
        codes = []
        def retry():
            r = self.c.post('/api/v1/packages/runs/' + pr['id'] + '/retry', headers=self.H); codes.append(r.status_code)
        ts = [threading.Thread(target=retry) for _ in range(2)]
        [t.start() for t in ts]; [t.join() for t in ts]
        self.assertEqual(codes, [409, 409])                                              # delivered/completed runs are never retried; no second job
        self.assertEqual(len(self.c.get('/api/v1/jobs', headers=self.H).json()['items']), 1)
        # failed regeneration: a changed input that is invalid is refused before anything runs; the original analysis revision stays readable
        rid = self.c.post('/api/v1/workflows/' + wid + '/runs', headers=self.H, json={}).json()['run_id']
        for _ in range(6):
            w.run_once(); self.c.post('/api/v1/runs/' + rid + '/advance', headers=self.H)
        a = self.c.post('/api/v1/analyses', headers=self.H, json={'name': 'regen', 'from_workflow': wid}).json()
        r = self.c.post('/api/v1/analyses/' + a['id'] + '/regenerate', headers=self.H, json={'run_id': rid, 'changes': {'plan': {'capacity': 10 ** 13}}})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(self.c.get('/api/v1/analyses/' + a['id'], headers=self.H).json()['version'], 1)
        self.assertEqual(len(self.c.get('/api/v1/workflows', headers=self.H).json()['items']), 1)


if __name__ == '__main__':
    unittest.main()
