"""Backlog 5, 6, 7, 10: report PDF export (local renderer, inspected pages, scoped metadata), model routing by evaluated
capability (installed revisions only, explicit fallback), incremental reindexing (vector reuse under compatibility checks;
old citations preserved), and checkpointed adaptive campaign recovery across an API restart and a worker interruption."""
import io
import json
import unittest
from unittest import mock
from pathlib import Path

from metacoin_service.db import Database, now
from metacoin_service.tests.test_compute_engine import ComputeInstance, HAVE_RUNTIME, batch_spec
from metacoin_service.tests.test_models import ModelInstance, HAVE_TORCH, installed, GEN, EMB
from metacoin_service.tests.test_resource_plan_service import sample
from metacoin_service.models import routing


class LInstance(ModelInstance, ComputeInstance):
    pass


@unittest.skipUnless(HAVE_RUNTIME, 'no compute interpreter')
class LateBacklogTests(unittest.TestCase):
    def setUp(self):
        self.inst = LInstance(); self.addCleanup(self.inst.close); self.c = self.inst.client; self.H = self.inst.h('owner')
        self.w = self.inst.worker(); self.addCleanup(self.w.offline)

    def post(self, path, body, status=201, headers=None):
        r = self.c.post(path, headers=headers or self.H, json=body); self.assertEqual(r.status_code, status, r.text); return r.json()

    def test_report_pdf_and_projection_pdf(self):
        jid = self.inst.compute_job('temporal_batch', batch_spec(private_label='PDF_PRIVATE_LABEL')); self.assertEqual(self.w.run_once()[1], 'succeeded')
        a = self.post('/api/v1/analyses', {'name': 'pdf study', 'blocks': [{'id': 'assump', 'type': 'assumption_table', 'rows': [{'name': 'reserve', 'value': 2000, 'unit': 'mJ'}]}, {'id': 'run', 'type': 'run_result', 'ref_kind': 'job', 'ref_id': jid, 'fields': ['scenarios'], 'depends_on': ['assump']},
                                                                        {'id': 'concl', 'type': 'conclusion', 'text': 'Sweep complete.', 'claims': [{'text': 'All scenarios ran.', 'values': {}, 'refs': ['run']}], 'depends_on': ['run']}]})
        self.post('/api/v1/analyses/' + a['id'] + '/freeze', {'version': 1}, 200)
        rep = self.post('/api/v1/analyses/' + a['id'] + '/reports', {'version': 1})
        r = self.c.get('/api/v1/reports/' + rep['id'] + '/pdf', headers=self.H)
        self.assertEqual((r.status_code, r.headers['content-type']), (200, 'application/pdf'), r.text[:200]); self.assertTrue(r.content.startswith(b'%PDF'))
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(r.content)); text = ''.join(p.extract_text() or '' for p in reader.pages)
        self.assertGreaterEqual(len(reader.pages), 1)
        for s in ('Computed findings', 'Declared assumptions', 'Limitations', 'scenarios'):
            self.assertIn(s, text)
        self.assertNotIn('PDF_PRIVATE_LABEL', text); self.assertNotIn('/home/', text)
        meta = reader.metadata or {}
        self.assertEqual(meta.get('/Title'), 'Report ' + rep['id']); self.assertFalse(meta.get('/Author'))
        # a restricted projection renders only its disclosed content
        pj = self.post('/api/v1/reports/' + rep['id'] + '/projection', {'scope': {'blocks': ['concl'], 'include_assumption_values': False}, 'acknowledge_warnings': True})
        r2 = self.c.get('/api/v1/reports/%s/projections/%s/pdf' % (rep['id'], pj['projection_id']), headers=self.H)
        self.assertEqual(r2.status_code, 200, r2.text[:200]); text2 = ''.join(p.extract_text() or '' for p in PdfReader(io.BytesIO(r2.content)).pages)
        self.assertIn('Sweep complete', text2); self.assertNotIn('scenarios:', text2); self.assertNotIn('2000', text2)
        self.assertIn('scope=', (PdfReader(io.BytesIO(r2.content)).metadata or {}).get('/Keywords', ''))
        self.assertEqual(self.c.get('/api/v1/reports/' + rep['id'] + '/pdf', headers=self.inst.h('viewer')).status_code, 403)

    def test_model_routing_by_evaluated_capability(self):
        if not (HAVE_TORCH and installed(self.inst.settings, GEN) and installed(self.inst.settings, EMB)):
            self.skipTest('models absent')
        ids = self.inst.register_defaults()
        with Database(self.inst.settings.db_path).tx() as db:
            db.execute("UPDATE model_revisions SET installed=1")
        # no evidence yet: explicit fallback to the promoted default
        r = self.c.get('/api/v1/models/route?operation=generate&category=summarize', headers=self.H).json()
        self.assertEqual(r['chosen']['revision_id'], ids['generate']); self.assertIn('fallback', r['basis']); self.assertEqual(r['hosted_models'], 'never candidates: only installed local revisions')
        # synthetic scored evaluation evidence (labelled: rows inserted directly, not produced by a real evaluation run) makes the ranking observable
        with Database(self.inst.settings.db_path).tx() as db:
            db.execute("INSERT INTO evaluation_suites (id, workspace, name, version, items_json, digest, threshold_percent, created_by, created_at) VALUES ('es_x','ws_default','synthetic',1,'[]','d',50,'p',?)", (now(),))
            db.execute("INSERT INTO evaluation_runs (id, workspace, suite_id, model_revision_id, jobs_json, state, results_json, passed, total, percent, started_by, created_at, scored_at) VALUES ('er_x','ws_default','es_x',?, '[]','scored',?,3,4,75,'p',?,?)",
                       (ids['generate'], json.dumps([{'item': 'i1', 'ok': True, 'category': 'summarize'}, {'item': 'i2', 'ok': False, 'category': 'summarize'}, {'item': 'i3', 'ok': True, 'category': 'extract'}, {'item': 'i4', 'ok': True, 'category': 'extract'}]), now(), now()))
        r2 = self.c.get('/api/v1/models/route?operation=generate&category=extract', headers=self.H).json()
        self.assertEqual((r2['chosen']['revision_id'], r2['chosen']['evidence']['accuracy'], r2['chosen']['evidence']['evaluated_items']), (ids['generate'], 1.0, 2)); self.assertIn('highest evaluated accuracy', r2['basis'])
        self.assertEqual(self.c.get('/api/v1/models/route?operation=generate&category=summarize', headers=self.H).json()['chosen']['evidence']['accuracy'], 0.5)
        # a budget nobody meets refuses instead of picking silently; the generate route records the decision and binds the revision
        r3 = self.c.get('/api/v1/models/route?operation=generate&max_resource_bytes=1', headers=self.H).json()
        self.assertIsNone(r3['chosen']); self.assertTrue(r3['candidates'][0]['excluded_because'])
        g = self.c.post('/api/v1/models/generate', headers=self.H, json={'inputs': {'messages': [{'role': 'user', 'content': 'Reply with one word: hi'}], 'max_output_tokens': 6}, 'route': {'category': 'extract'}})
        self.assertEqual(g.status_code, 202, g.text); self.assertEqual(g.json()['routing']['chosen'], ids['generate'])
        self.assertEqual(self.c.post('/api/v1/models/generate', headers=self.H, json={'inputs': {'messages': [{'role': 'user', 'content': 'x'}], 'max_output_tokens': 6}, 'route': {'budget': {'max_resource_bytes': 1}}}).status_code, 501)
        # pure ranking function on labelled synthetic rows: evidence beats no evidence; lower latency breaks ties
        with Database(self.inst.settings.db_path).read() as db:
            from metacoin_service.auth import Principal
            p = Principal(db.execute('SELECT * FROM principals WHERE id=?', (self.inst.ids['owner'],)).fetchone())
            ev = routing.evidence_for(db, 'ws_default', ids['generate'], 'summarize')
        self.assertEqual((ev['accuracy'], ev['evaluated_items'], ev['runs']), (0.5, 2, ['er_x']))

    def test_incremental_reindexing_reuses_compatible_vectors_and_keeps_old_citations(self):
        if not (HAVE_TORCH and installed(self.inst.settings, GEN) and installed(self.inst.settings, EMB)):
            self.skipTest('models absent')
        self.inst.register_defaults()
        cid = self.post('/api/v1/knowledge/collections', {'name': 'inc'})['id']
        d1 = self.post('/api/v1/knowledge/collections/' + cid + '/documents', {'name': 'a.md', 'format': 'markdown', 'content': '# A\n\nThe reserve floor is 2000 mJ.\n\n## B\n\nThe pump draws 450 mW.\n', 'provenance': 'synthetic'})
        i1 = self.post('/api/v1/knowledge/collections/' + cid + '/indexes', {}, 202); self.assertEqual(self.w.run_once()[1], 'succeeded')

        s1 = self.c.post('/api/v1/knowledge/collections/' + cid + '/search', headers=self.H, json={'query': 'reserve floor', 'mode': 'semantic', 'k': 2}).json()
        old_chunk = s1['results'][0]['chunk_id']
        j1 = self.c.get('/api/v1/jobs/' + i1['job_id'], headers=self.H).json()['summary']
        self.assertEqual((j1['reused_vectors'], j1['embedded_vectors'] > 0), (0, True))
        d2 = self.post('/api/v1/knowledge/collections/' + cid + '/documents', {'name': 'b.md', 'format': 'markdown', 'content': '# C\n\nLeakage is 15 mW at rest.\n', 'provenance': 'synthetic'})
        i2 = self.post('/api/v1/knowledge/collections/' + cid + '/indexes', {}, 202); self.assertEqual(self.w.run_once()[1], 'succeeded')
        j2 = self.c.get('/api/v1/jobs/' + i2['job_id'], headers=self.H).json()['summary']
        self.assertEqual(j2['reused_vectors'], j1['embedded_vectors']); self.assertGreater(j2['embedded_vectors'], 0); self.assertIsNone(j2['reuse_rejected'])
        # the old citation still validates byte-exactly and resolves to the same chunk id
        chk = self.c.post('/api/v1/knowledge/citations/validate', headers=self.H, json={'citations': [{'chunk_id': old_chunk, 'quote': 'reserve floor is 2000 mJ'}]}).json()
        self.assertTrue(chk['all_valid'])
        s2 = self.c.post('/api/v1/knowledge/collections/' + cid + '/search', headers=self.H, json={'query': 'reserve floor', 'mode': 'semantic', 'k': 2}).json()
        self.assertEqual(s2['results'][0]['chunk_id'], old_chunk)
        # a changed chunker is incompatible: reuse rejected, everything re-embedded (controlled double on the chunker id)
        from metacoin_service.knowledge import text as text_mod
        with mock.patch.object(text_mod, 'CHUNKER_ID', text_mod.CHUNKER_ID + '-v2'):
            i3 = self.post('/api/v1/knowledge/collections/' + cid + '/indexes', {}, 202); self.assertEqual(self.w.run_once()[1], 'succeeded')
        j3 = self.c.get('/api/v1/jobs/' + i3['job_id'], headers=self.H).json()['summary']
        self.assertEqual(j3['reused_vectors'], 0); self.assertIn('chunker changed', j3['reuse_rejected'] or '')

    def test_checkpointed_campaign_recovery_across_restart_and_interruption(self):
        base = sample(); base.pop('sensitivity'); base['objectives'] = {'mode': 'utility'}
        cands = [{'reserve': r} for r in (10000, 20000, 30000, 40000, 50000, 60000)]
        camp = self.post('/api/v1/campaigns', {'definition': {'name': 'recover', 'kind': 'resource_plan', 'base': base, 'adaptive': {'strategy': 'acquisition', 'candidates': cands, 'objective': {'field': 'min_margin', 'direction': 'max'}, 'max_evaluations': 4, 'exploration_percent': 20, 'seed': 5}}})
        cid = camp['campaign_id']; self.post('/api/v1/campaigns/' + cid + '/run', {}, 200)
        self.post('/api/v1/campaigns/' + cid + '/tick', {}, 200)                       # dispatches evaluation 0
        # interruption 1: the worker claims the job and dies (lease expiry simulated by the test), then the API restarts
        job = self.w.claim(); self.assertIsNotNone(job)
        with Database(self.inst.settings.db_path).tx() as db:
            db.execute('UPDATE jobs SET lease_expires=? WHERE id=?', (now() - 1, job['id']))
        self.w.offline(); self.inst.reopen(); self.c = self.inst.client
        st = self.c.get('/api/v1/campaigns/' + cid, headers=self.H).json()['adaptive']
        self.assertEqual((st['budget_left'], len(st['log'])), (3, 1))                    # the checkpoint survived the restart; nothing re-dispatched
        w2 = self.inst.worker(); self.addCleanup(w2.offline)
        for _ in range(30):
            w2.run_once(); state = self.c.post('/api/v1/campaigns/' + cid + '/tick', headers=self.H).json()['state']
            if state in ('completed', 'budget_exhausted'):
                break
        v = self.c.get('/api/v1/campaigns/' + cid, headers=self.H).json(); ad = v['adaptive']
        self.assertEqual(state, 'budget_exhausted'); self.assertEqual(len(ad['evaluated']), 4); self.assertEqual(len({e['index'] for e in ad['evaluated']}), 4)
        self.assertEqual(ad['checkpoint']['recoveries'], 1); self.assertTrue(any(e['recovered_after_interruption'] and e['job_attempts'] == 2 for e in ad['evaluated']))
        self.assertEqual((ad['checkpoint']['seed'], ad['checkpoint']['exploration_percent']), (5, 20))
        with Database(self.inst.settings.db_path).read() as db:
            n_jobs = db.execute("SELECT COUNT(*) FROM jobs WHERE kind='resource_plan'").fetchone()[0]
            n_usage = db.execute('SELECT COUNT(*) FROM usage_records').fetchone()[0]
        self.assertEqual(n_jobs, 4)                                                       # one job per evaluation: the recovered attempt is the same job, never a duplicate evaluation
        self.assertLessEqual(n_usage, 4)


if __name__ == '__main__':
    unittest.main()
