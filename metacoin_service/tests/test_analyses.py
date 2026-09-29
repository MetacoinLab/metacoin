"""Group E: analysis sessions (typed blocks, optimistic concurrency, freezes, stale marking), dependency/impact analysis,
selective regeneration with exact reuse, deterministic evidence-linked reports, and signed disclosure projections."""
import json
import unittest
from metacoin_service.tests.test_compute_engine import ComputeInstance, HAVE_RUNTIME, batch_spec, mc_spec
from metacoin_service.tests.test_workflows import CSV_OK
from metacoin_service import workflows as wf_mod


@unittest.skipUnless(HAVE_RUNTIME, 'no numpy-capable compute interpreter on this host')
class AnalysisTests(unittest.TestCase):
    def setUp(self):
        self.inst = ComputeInstance(); self.addCleanup(self.inst.close); self.c = self.inst.client; self.H = self.inst.h('owner'); self.w = self.inst.worker()

    def post(self, path, body, status=201, headers=None):
        r = self.c.post(path, headers=headers or self.H, json=body)
        self.assertEqual(r.status_code, status, r.text)
        return r.json()

    def test_sessions_concurrency_stale_impact_reports_and_projections(self):
        col = self.post('/api/v1/knowledge/collections', {'name': 'notes', 'description': 'synthetic'})['id']
        doc = self.post('/api/v1/knowledge/collections/' + col + '/documents', {'name': 'bench-notes.md', 'format': 'markdown', 'content': '# Bench\nThe reserve floor measured on the bench was 2000 mJ.', 'provenance': 'synthetic'})
        vid, did = doc['id'], doc['document_id']
        ds = self.post('/api/v1/datasets', {'name': 'series', 'kind': 'temporal_series', 'format': 'csv', 'content': CSV_OK, 'provenance': 'declared'})['version_id']
        jid = self.inst.compute_job('temporal_batch', batch_spec(private_label='ANALYSIS_PRIVATE_LABEL')); self.assertEqual(self.w.run_once()[1], 'succeeded')
        ver = self.post('/api/v1/verification', {'job_id': jid, 'class': 'analytical', 'params': {}}, 202); self.assertEqual(self.w.run_once()[1], 'succeeded')
        scenarios = self.c.get('/api/v1/jobs/' + jid, headers=self.H).json()['summary']['scenarios']
        blocks = [{'id': 'source', 'type': 'source_note', 'ref_kind': 'knowledge_document_version', 'ref_id': vid, 'label': 'bench-notes.md', 'quote': 'The reserve floor measured on the bench was 2000 mJ.', 'page_number': 1},
                  {'id': 'data', 'type': 'dataset_ref', 'ref_kind': 'dataset_version', 'ref_id': ds, 'depends_on': ['source']},
                  {'id': 'assumptions', 'type': 'assumption_table', 'rows': [{'name': 'reserve', 'value': 2000, 'unit': 'mJ', 'source': 'document'}, {'name': 'capacity', 'value': 10000, 'unit': 'mJ', 'source': 'user_edit'}], 'depends_on': ['source']},
                  {'id': 'run', 'type': 'run_result', 'ref_kind': 'job', 'ref_id': jid, 'fields': ['scenarios', 'first_infeasible_index'], 'depends_on': ['assumptions', 'data']},
                  {'id': 'audit', 'type': 'verification', 'ref_kind': 'verification', 'ref_id': ver['id'], 'depends_on': ['run']},
                  {'id': 'concl', 'type': 'conclusion', 'text': 'The sweep covers every scenario of the grid.', 'claims': [{'text': 'Scenarios evaluated: %d.' % scenarios, 'values': {'run.scenarios': scenarios}, 'refs': ['run']}], 'depends_on': ['run', 'audit']}]
        a = self.post('/api/v1/analyses', {'name': 'reserve study', 'blocks': blocks})
        aid = a['id']
        self.assertEqual((a['version'], a['frozen'], a['stale']), (1, False, {}))
        self.assertTrue(all(b['status'] == 'current' for b in a['blocks'])); self.assertEqual(a['blocks'][0]['schema'], 'metacoin-analysis-block/v1'); self.assertEqual(a['blocks'][0]['author'], self.inst.ids['owner'])
        # executable content is refused; unknown dependency and cycles are refused
        self.assertEqual(self.c.post('/api/v1/analyses', headers=self.H, json={'name': 'x', 'blocks': [{'id': 'c', 'type': 'code', 'text': 'import os'}]}).status_code, 422)
        self.assertEqual(self.post('/api/v1/analyses/' + aid + '/revisions', {'blocks': blocks + [{'id': 'z', 'type': 'text', 'text': 'x', 'depends_on': ['nope']}], 'expected_version': 1}, 422)['detail']['code'], 'unknown_dependency')
        cyc = [dict(b) for b in blocks]; cyc[0] = dict(cyc[0], depends_on=['concl'])
        self.assertEqual(self.post('/api/v1/analyses/' + aid + '/revisions', {'blocks': cyc, 'expected_version': 1}, 422)['detail']['code'], 'dependency_cycle')
        # optimistic concurrency: a stale expected version is a conflict; the right one creates revision 2
        self.assertEqual(self.post('/api/v1/analyses/' + aid + '/revisions', {'blocks': blocks, 'expected_version': 0}, 409)['detail']['code'], 'version_conflict')
        v2 = self.post('/api/v1/analyses/' + aid + '/revisions', {'blocks': blocks + [{'id': 'aim', 'type': 'text', 'text': 'Aim: bound the reserve.'}], 'expected_version': 1, 'note': 'aim added'})
        self.assertEqual((v2['version'], v2['changed_in_this_revision'], v2['stale']), (2, ['aim'], {}))
        # a changed assumption creates revision 3 and marks dependents stale with reasons and required actions; revision 2 is untouched
        changed = [dict(b) for b in v2['blocks']]
        for b in changed:
            for k in ('status', 'stale', 'requires', 'reference', 'reference_drift'):
                b.pop(k, None)
        idx = [b['id'] for b in changed].index('assumptions'); changed[idx] = dict(changed[idx], rows=[{'name': 'reserve', 'value': 2500, 'unit': 'mJ', 'source': 'measured_update', 'note': 're-measured'}, changed[idx]['rows'][1]])
        v3 = self.post('/api/v1/analyses/' + aid + '/revisions', {'blocks': changed, 'expected_version': 2, 'note': 'reserve re-measured'})
        st = {b['id']: (b['status'], b.get('stale', {}).get('reason'), b.get('requires')) for b in v3['blocks']}
        self.assertEqual(st['run'], ('stale', 'dependency_changed', 'regeneration')); self.assertEqual(st['audit'], ('stale', 'dependency_stale', 're-verification')); self.assertEqual(st['concl'], ('stale', 'dependency_stale', 're-review'))
        self.assertEqual((st['source'][0], st['data'][0], st['assumptions'][0], st['aim'][0]), ('current', 'current', 'current', 'current'))
        self.assertEqual(self.c.get('/api/v1/analyses/' + aid + '?version=2', headers=self.H).json()['stale'], {})
        # impact query: the changed assumption affects run directly, audit and conclusion transitively; source/data/aim unaffected
        imp = self.post('/api/v1/analyses/' + aid + '/impact', {'changed': {'block': 'assumptions'}}, 200)
        self.assertEqual([x['block'] for x in imp['directly_affected']], ['run']); self.assertEqual(sorted(x['block'] for x in imp['transitively_affected']), ['audit', 'concl'])
        self.assertEqual(sorted(imp['unaffected']), ['aim', 'data', 'source']); self.assertEqual(imp['unknown'], []); self.assertEqual(imp['cycles'], [])
        # changing the source: assumptions and data depend on it declaredly; a block with no declared dependency is 'unknown', never 'unaffected'
        undeclared = changed + [{'id': 'loose', 'type': 'assumption_table', 'rows': [{'name': 'slot', 'value': 60, 'unit': 's'}]}]
        v4 = self.post('/api/v1/analyses/' + aid + '/revisions', {'blocks': undeclared, 'expected_version': 3})
        imp2 = self.post('/api/v1/analyses/' + aid + '/impact', {'changed': {'object_type': 'knowledge_document_version', 'object_id': vid}}, 200)
        self.assertEqual(imp2['root_blocks'], ['source']); self.assertEqual(sorted(x['block'] for x in imp2['directly_affected']), ['assumptions', 'data'])
        self.assertEqual([u['block'] for u in imp2['unknown']], ['loose']); self.assertIn('not established', imp2['unknown'][0]['reason'])
        self.assertTrue(any(o['type'] == 'analysis' for o in imp2['affected_objects']))
        # revoking the source marks the source block stale (source_revoked) and, through declared dependencies, everything downstream; old revisions keep their record
        self.post('/api/v1/knowledge/documents/' + did + '/revoke', {'reason': 'superseded'}, 200)
        live = self.c.get('/api/v1/analyses/' + aid, headers=self.H).json()
        self.assertEqual(live['stale']['source']['reason'], 'source_revoked'); self.assertEqual(live['stale']['aim'] if 'aim' in live['stale'] else None, None); self.assertIn('data', live['stale'])
        # reports bind to frozen revisions only; the deterministic report checks claims against structured values
        self.assertEqual(self.post('/api/v1/analyses/' + aid + '/reports', {'version': 4}, 409)['detail']['code'], 'revision_not_frozen')
        fr = self.post('/api/v1/analyses/' + aid + '/freeze', {'version': 4, 'reason': 'for report'}, 200); self.assertTrue(fr['frozen'])
        rep = self.post('/api/v1/analyses/' + aid + '/reports', {'version': 4})
        md = rep['markdown']
        for section in ('## Source facts', '## Declared assumptions', '## Computed findings', '## Verification scope', '## Conclusions', '## Limitations', '## Methods'):
            self.assertIn(section, md)
        self.assertIn('Scenarios evaluated: %d.' % scenarios, md); self.assertNotIn('FLAGGED', md); self.assertIn('[revoked source]', md); self.assertIn('Stale blocks', md)
        self.assertNotIn('ANALYSIS_PRIVATE_LABEL', md); self.assertNotIn('/home/', md); self.assertNotIn('ANALYSIS_PRIVATE_LABEL', json.dumps(rep['manifest']))
        self.assertTrue(all(c['ok'] for c in rep['manifest']['claim_checks'])); self.assertEqual(rep['manifest']['analysis_version'], 4)
        self.assertIn(jid, [x['ref_id'] for x in rep['manifest']['artifacts']])
        html = self.c.get('/api/v1/reports/' + rep['id'] + '/html', headers=self.H)
        self.assertEqual(html.status_code, 200); self.assertIn('<h2>Computed findings</h2>', html.text); self.assertIn('<table>', html.text)
        bundle = self.c.get('/api/v1/reports/' + rep['id'] + '/bundle', headers=self.H).json()
        self.assertEqual(set(bundle['files']), {'report.md', 'report.html', 'manifest.json'}); self.assertEqual(len(bundle['file_sha256']['report.md']), 64)
        # a contradicting claim and an unsupported reference are flagged in the report, never rewritten
        wrong = [dict(b) for b in undeclared]
        ci = [b['id'] for b in wrong].index('concl'); wrong[ci] = dict(wrong[ci], claims=[{'text': 'Scenarios evaluated: 999999.', 'values': {'run.scenarios': 999999}, 'refs': ['run', 'ghost']}])
        v5 = self.post('/api/v1/analyses/' + aid + '/revisions', {'blocks': wrong, 'expected_version': 4}); self.post('/api/v1/analyses/' + aid + '/freeze', {'version': 5}, 200)
        rep2 = self.post('/api/v1/analyses/' + aid + '/reports', {'version': 5})
        codes = sorted({f['code'] for f in rep2['flags']})
        self.assertIn('claim_contradicts_structured_result', codes); self.assertIn('unsupported_reference', codes); self.assertIn('FLAGGED', rep2['markdown']); self.assertIn('999999', rep2['markdown'])
        self.assertFalse(all(c['ok'] for c in rep2['manifest']['claim_checks']))
        # projections: exact preview, indirect-disclosure warnings, signed export, honest verification scope, omitted fields absent from the bytes
        scope = {'blocks': ['run', 'concl'], 'fields': {'run': ['scenarios']}, 'include_assumption_values': False}
        pv = self.post('/api/v1/reports/' + rep['id'] + '/projection/preview', {'scope': scope}, 200)
        self.assertEqual(sorted(pv['omitted_blocks']), ['aim', 'assumptions', 'audit', 'data', 'loose', 'source'])
        self.assertIn('scenarios', pv['projected_markdown']); self.assertNotIn('first_infeasible_index: ', pv['projected_markdown']); self.assertNotIn('bench', pv['projected_markdown'].lower())
        self.assertNotIn('ANALYSIS_PRIVATE_LABEL', pv['projected_markdown']); self.assertIn('fields withheld: first_infeasible_index', pv['projected_markdown'])
        leaky = {'blocks': ['concl', 'audit'], 'fields': {}}
        pv2 = self.post('/api/v1/reports/' + rep['id'] + '/projection/preview', {'scope': leaky}, 200)
        self.assertIn('verification_of_hidden_result', [w['code'] for w in pv2['warnings']])
        self.assertEqual(self.post('/api/v1/reports/' + rep['id'] + '/projection', {'scope': leaky}, 409)['detail']['code'], 'disclosure_warnings')
        exp = self.post('/api/v1/reports/' + rep['id'] + '/projection', {'scope': scope})
        self.assertEqual(exp['files']['projection.md'], pv['projected_markdown']); self.assertEqual(exp['statement']['omitted_block_count'], 6)
        ok = self.post('/api/v1/reports/projection/verify', {'bundle': {k: exp[k] for k in ('statement', 'signature', 'public_key', 'files')}}, 200)
        self.assertEqual((ok['signature_valid'], ok['issuer_is_this_service'], ok['projected_text_matches_statement'], ok['report_known_here']), (True, True, True, True))
        self.assertIn('not independently verified', ok['evidence_scope']['not_verified']); self.assertEqual(ok['evidence_scope']['omitted_blocks'], 6)
        tampered = {k: exp[k] for k in ('statement', 'signature', 'public_key', 'files')}; tampered['files'] = {'projection.md': exp['files']['projection.md'] + ' (edited)'}
        self.assertFalse(self.post('/api/v1/reports/projection/verify', {'bundle': tampered}, 200)['projected_text_matches_statement'])
        forged = json.loads(json.dumps({k: exp[k] for k in ('statement', 'signature', 'public_key', 'files')})); forged['statement']['omitted_block_count'] = 0
        self.assertFalse(self.post('/api/v1/reports/projection/verify', {'bundle': forged}, 200)['signature_valid'])
        # a viewer cannot read private plans, build reports or export; MCP/CLI-facing listing works
        self.assertEqual(self.c.post('/api/v1/reports/' + rep['id'] + '/projection', headers=self.inst.h('viewer'), json={'scope': scope}).status_code, 403)
        self.assertEqual(self.c.get('/api/v1/analyses', headers=self.H).json()['items'][0]['id'], aid)
        # console pages render
        s = self.c.post('/api/v1/session', json={'token': self.inst.tok['owner']}); cookies = {'metacoin_session': s.cookies['metacoin_session']}
        page = self.c.get('/console/analyses/' + aid, cookies=cookies)
        self.assertEqual(page.status_code, 200, page.text[:300]); self.assertIn('stale', page.text); self.assertIn('Reports', page.text)
        self.assertEqual(self.c.get('/console/analyses', cookies=cookies).status_code, 200)
        rpage = self.c.get('/console/reports/' + rep['id'], cookies=cookies); self.assertEqual(rpage.status_code, 200); self.assertIn('Computed findings', rpage.text)
        pf = self.c.post('/console/reports/' + rep['id'] + '/projection', cookies=cookies, data={'csrf': s.json()['csrf'], 'blocks': ['run', 'concl'], 'fields': json.dumps({'run': ['scenarios']}), 'action': 'preview'})   # repeated keys as a browser sends them
        self.assertEqual(pf.status_code, 200, pf.text[:300]); self.assertIn('Preview', pf.text); self.assertIn('scenarios', pf.text); self.assertNotIn('first_infeasible_index: ', pf.text)

    def test_regeneration_reruns_changed_branch_and_reuses_the_other(self):
        definition = {'schema': wf_mod.SCHEMA, 'name': 'two branches', 'outputs': ['out'], 'nodes': [
            {'id': 'a', 'type': 'temporal_batch', 'inputs': batch_spec(private_label='REGEN_A')},
            {'id': 'b', 'type': 'monte_carlo_reliability', 'depends_on': [{'node': 'a', 'require': 'succeeded'}], 'inputs': mc_spec(samples=2000, private_label='REGEN_B')},
            {'id': 'c', 'type': 'temporal_batch', 'inputs': batch_spec(private_label='REGEN_C', grid=[{'path': 'reserve', 'start': 0, 'stop': 4000, 'step': 500}])},
            {'id': 'out', 'type': 'export', 'depends_on': ['b', 'c'], 'input': 'b', 'fields': ['outcome', 'model_id', 'evidence_root']}]}
        wid = self.post('/api/v1/workflows', {'definition': definition})['id']
        rid = self.post('/api/v1/workflows/' + wid + '/runs', {}, 202)['run_id']
        def drive(run_id, ticks=15):
            for _ in range(ticks):
                self.w.run_once(); self.c.post('/api/v1/runs/' + run_id + '/advance', headers=self.H)
                v = self.c.get('/api/v1/runs/' + run_id, headers=self.H).json()
                if v['state'] in ('completed', 'blocked', 'failed', 'cancelled'):
                    return v
            return v
        v = drive(rid); self.assertEqual(v['state'], 'completed', v)
        jobs = {n['node_id']: n['job_id'] for n in v['nodes'] if n.get('job_id')}
        a = self.post('/api/v1/analyses', {'name': 'branch study', 'from_workflow': wid})
        self.assertEqual(a['blocks'][0]['type'], 'operation_draft')
        # plan: changing node a's grid reruns a and b (downstream) and reuses c under the exact cache contract
        change = {'a': {'grid': [{'path': 'reserve', 'start': 0, 'stop': 2000, 'step': 500}, {'path': 'load_scale_percent', 'values': [100]}]}}
        plan = self.post('/api/v1/analyses/' + a['id'] + '/regeneration-plan', {'run_id': rid, 'changes': change}, 200)
        actions = {p['node']: (p['action'], p['reason']) for p in plan['plan']}
        self.assertEqual(actions['a'], ('rerun', 'inputs_changed')); self.assertEqual(actions['b'], ('rerun', 'downstream_of_change')); self.assertEqual(actions['c'], ('reuse', 'identical_inputs_and_verifier'))
        self.assertEqual(actions['out'][0], 'keep'); self.assertEqual(plan['quote']['service_nodes_reused'], 1); self.assertEqual(plan['quote']['service_nodes_to_execute'], 2)
        self.assertEqual(self.post('/api/v1/analyses/' + a['id'] + '/regeneration-plan', {'run_id': rid, 'changes': {'a': {'nope': 1}}}, 422)['detail']['code'], 'unknown_input_field')
        # regenerate: a new run; c's job is a zero-charge reuse of the original; the original run and revision stay readable
        usage_before = len(self.c.get('/api/v1/usage', headers=self.H).json().get('items', []))
        rg = self.post('/api/v1/analyses/' + a['id'] + '/regenerate', {'run_id': rid, 'changes': change, 'budget_ceiling': 10}, 202)
        self.assertEqual((rg['reruns'], rg['reuses'], rg['analysis_version']), (['a', 'b'], ['c'], 2))
        v2 = drive(rg['run_id'], 20); self.assertEqual(v2['state'], 'completed', v2)
        jobs2 = {n['node_id']: n['job_id'] for n in v2['nodes'] if n.get('job_id')}
        cj = self.c.get('/api/v1/jobs/' + jobs2['c'], headers=self.H).json()
        self.assertEqual(cj['reused_from'], jobs['c']); self.assertNotEqual(jobs2['a'], jobs['a'])
        usage = self.c.get('/api/v1/usage', headers=self.H).json().get('items', [])
        charged = [u for u in usage if u.get('job_id') == jobs2['c']]
        self.assertTrue(all(u.get('assessed_charge', 0) == 0 for u in charged))
        self.assertEqual(self.c.get('/api/v1/runs/' + rid, headers=self.H).json()['state'], 'completed')
        view = self.c.get('/api/v1/analyses/' + a['id'], headers=self.H).json()
        self.assertEqual(view['version'], 2); self.assertTrue(any(b['type'] == 'comparison' and b['ref_id'] == rg['run_id'] for b in view['blocks']))
        self.assertEqual(self.c.get('/api/v1/analyses/' + a['id'] + '?version=1', headers=self.H).json()['version'], 1)


if __name__ == '__main__':
    unittest.main()
