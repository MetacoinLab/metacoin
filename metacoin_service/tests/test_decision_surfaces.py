"""Interfaces of the decision-evidence capability (Deliverable G): CLI commands map to single authenticated requests and keep
idempotency keys; MCP tools declare their authority and route correctly (analysis without purchase); the console pages
perform the connected journey through forms and never show private witnesses to an unauthorized role."""
import asyncio
import json
import unittest

from metacoin_service import client_cli, mcp_server
from metacoin_service.tests.test_work_surfaces import ConsoleSession, _build_parser
from metacoin_service.tests.test_decision_service import DecisionBase, analysis_inputs, menu, delivery, TOTAL


class DecisionCliTests(unittest.TestCase):
    def test_every_decision_command_parses_and_maps_to_one_request(self):
        parser = _build_parser(); calls = []
        def go(method, path, body=None, raw=False, idempotency_key=None, content_type=None):
            calls.append((method, path, body, idempotency_key, raw)); return 200, {'id': 'x'} if not raw else b'zip'
        import tempfile, os
        d = tempfile.mkdtemp(); jf = os.path.join(d, 'in.json'); open(jf, 'w').write(json.dumps({'hypotheses': []}))
        cases = [(['decision-analyze', '--file', jf, '--title', 't', '--idempotency-key', 'k1'], ('POST', '/api/v1/decisions/analyze', 'k1')),
                 (['decisions', '--kind', 'decision_observability'], ('GET', '/api/v1/decisions?kind=decision_observability', None)), (['decision', 'j_1'], ('GET', '/api/v1/decisions/j_1', None)),
                 (['decision-witnesses', 'j_1'], ('GET', '/api/v1/decisions/j_1/witnesses', None)), (['acquisition-preview', 'j_1', '--budget', '3', '--file', jf], ('POST', '/api/v1/acquisition-plans/preview', None)),
                 (['acquisition-plan', 'j_1', '--budget', '3', '--file', jf, '--idempotency-key', 'k2'], ('POST', '/api/v1/acquisition-plans', 'k2')), (['acquisition-materialize', 'ap_1', '--diagnostic', '1', '--idempotency-key', 'k3'], ('POST', '/api/v1/acquisition-plans/ap_1/materialize', 'k3')),
                 (['work-deliver', 'wa_1', '--file', jf, '--idempotency-key', 'k4'], ('POST', '/api/v1/work/awards/wa_1/milestones/m1/deliver', 'k4')), (['work-assurance', 'wa_1'], ('GET', '/api/v1/work/awards/wa_1/milestones/m1/assurance', None)),
                 (['sampling-plan', 'j_1', '--alpha', '1/100', '--max-samples', '500', '--minimum-defects', '10'], ('POST', '/api/v1/verification/sampling-plans', None)), (['sampling-audit', 'sp_1', '--idempotency-key', 'k5'], ('POST', '/api/v1/verification/sampling-plans/sp_1/audit', 'k5')),
                 (['assurance-evaluate', 'j_1', '--file', jf], ('POST', '/api/v1/verification/assurance/evaluate', None)), (['portfolio-select', 'es_1', '--families', 'a,b'], ('POST', '/api/v1/portfolios/select', None)),
                 (['campaign', 'es_1', '--portfolio-ids', 'p1,p2', '--split', 'held-out', '--idempotency-key', 'k6'], ('POST', '/api/v1/portfolios/campaigns', 'k6')), (['campaigns'], ('GET', '/api/v1/portfolios/campaigns', None))]
        for argv, (method, path, key) in cases:
            calls.clear(); ns = parser.parse_args(['--credential-file', 'x'] + argv); ns.command = argv[0]
            status, out = client_cli.decision(ns, go)
            self.assertEqual(len(calls), 1, argv); self.assertEqual((calls[0][0], calls[0][1], calls[0][3]), (method, path, key), argv)
        for c in client_cli.DECISION_COMMANDS:
            self.assertTrue(any(c == a.dest or c in str(a.choices) for a in parser._subparsers._group_actions), c)
        out = os.path.join(d, 'b.zip'); ns = parser.parse_args(['--credential-file', 'x', 'decision-bundle', 'j_1', '--out', out]); ns.command = 'decision-bundle'
        st, res = client_cli.decision(ns, go); self.assertEqual((st, res['bytes']), (200, 3)); self.assertTrue(calls[-1][4]); self.assertIn('metacoin_service.decision.replay', res['replay'])


class DecisionMcpTests(unittest.TestCase):
    def test_tools_declare_authority_and_route(self):
        calls = []
        class Fake:
            def call(self, method, path, body=None, idempotency_key=None):
                calls.append((method, path, body, idempotency_key)); return (202 if 'analyze' in path else 201 if method == 'POST' and 'preview' not in path and 'campaigns' not in path else 200), {'id': 'ap_1', 'state': 'planned', 'job_id': 'j_1'}
        m = mcp_server.build(Fake()); tools = {t.name: t for t in asyncio.run(m.list_tools())}
        for name in ('analyze_decision', 'decision_status', 'preview_acquisition', 'create_acquisition_contract', 'assurance_preview', 'sampling_plan_preview', 'compare_portfolios'):
            self.assertIn(name, tools)
        self.assertTrue(tools['decision_status'].annotations.readOnlyHint); self.assertTrue(tools['preview_acquisition'].annotations.readOnlyHint); self.assertTrue(tools['sampling_plan_preview'].annotations.readOnlyHint)
        self.assertFalse(tools['create_acquisition_contract'].annotations.readOnlyHint); self.assertTrue(tools['create_acquisition_contract'].annotations.destructiveHint); self.assertIn('CONSEQUENTIAL', tools['create_acquisition_contract'].description)
        self.assertIn('never spends', tools['analyze_decision'].description)
        asyncio.run(m.call_tool('preview_acquisition', {'analysis_job_id': 'j_1', 'menu': [], 'budget': 3})); self.assertEqual(calls[-1][:2], ('POST', '/api/v1/acquisition-plans/preview'))
        asyncio.run(m.call_tool('create_acquisition_contract', {'analysis_job_id': 'j_1', 'menu': [], 'budget': 3, 'idempotency_key': 'mcp-9'}))
        self.assertEqual([c[1] for c in calls[-2:]], ['/api/v1/acquisition-plans', '/api/v1/acquisition-plans/ap_1/materialize']); self.assertEqual([c[3] for c in calls[-2:]], ['mcp-9', 'mcp-9-mat'])
        asyncio.run(m.call_tool('sampling_plan_preview', {'job_id': 'j_1', 'alpha': '1/100', 'max_samples': 10, 'minimum_defects': 2})); self.assertTrue(calls[-1][2]['preview'])


class DecisionConsoleTests(DecisionBase):
    def test_console_pages_perform_the_connected_journey_and_police_witnesses(self):
        owner = ConsoleSession(self.c, self.inst.tok['owner']); viewer = ConsoleSession(self.c, self.inst.tok['viewer']); prov = ConsoleSession(self.c, self.pv['h']['Authorization'].split(' ')[1])
        ov = owner.get('/console/decisions'); self.assertEqual(ov.status_code, 200); self.assertIn('Decision evidence', ov.text); self.assertIn('Analyse the decision', ov.text)
        r = owner.post('/console/decisions/analyze', inputs=json.dumps(analysis_inputs()), title='heater'); self.assertEqual(r.status_code, 303, r.text); jid = r.headers['location'].split('/')[-1]
        self.run_worker(2)
        dp = owner.get('/console/decisions/' + jid).text; self.assertIn('ambiguous', dp); self.assertIn('hot_cell_1 vs hot_cell_2', dp); self.assertIn('Preview the plan', dp)
        vp = viewer.get('/console/decisions/' + jid); self.assertEqual(vp.status_code, 200); self.assertNotIn('hot_cell', vp.text); self.assertNotIn('DO_NOT_START', vp.text); self.assertIn('ambiguous', vp.text)
        # plan preview (expected cost before action), then store and materialise through the forms
        pv = owner.post('/console/decisions/%s/preview' % jid, budget='3', menu=json.dumps(menu(self.now() + 3600, self.pv['id']))); self.assertEqual(pv.status_code, 200, pv.text); self.assertIn('cell_1_sensor', pv.text); self.assertIn('nothing stored', pv.text)
        r = owner.post('/console/decisions/%s/plan' % jid, budget='3', menu=json.dumps(menu(self.now() + 3600, self.pv['id']))); self.assertEqual(r.status_code, 303, r.text); pid = r.headers['location'].split('/')[-1]
        pp = owner.get('/console/acquisition-plans/' + pid).text; self.assertIn('Expected cost before action: 3', pp); self.assertIn('Create the acquisition contract', pp)
        r = owner.post('/console/acquisition-plans/%s/materialize' % pid, diagnostic='1'); self.assertEqual(r.status_code, 303, r.text); rid = r.headers['location'].split('/')[-1]
        self.assertEqual(prov.post('/console/work/requests/' + rid + '/offer', price_amount='3', scheme='exact', verification_class='analytical', asset='action-units').status_code, 303)
        r = owner.post('/console/work/requests/' + rid + '/award'); self.assertEqual(r.status_code, 303, r.text); aid = r.headers['location'].split('/')[-1]
        ap = prov.get('/console/work/awards/' + aid).text; self.assertIn('Observation delivery m1', ap); self.assertIn('Deliver the observation', ap); self.assertNotIn('hot_cell', ap)
        self.assertEqual(prov.post('/console/work/awards/%s/deliver' % aid, milestone='m1', delivery=json.dumps(delivery({'cell_1_sensor': ['99']}))).status_code, 303)
        self.run_worker(2)
        ap = owner.get('/console/work/awards/' + aid).text; self.assertIn('Required assurance m1', ap); self.assertIn('missing evidence', ap)
        self.assertEqual(owner.post('/console/work/awards/%s/verify' % aid, milestone='m1', **{'class': 'analytical'}).status_code, 303); self.run_worker(2)
        ap = owner.get('/console/work/awards/' + aid).text; self.assertIn('satisfied', ap); self.assertIn('Checked: verification', ap)
        pp = owner.get('/console/acquisition-plans/' + pid).text; self.assertIn('Actual cost after action: reserved', pp)
        # sampling plan form (preview, bind) and the portfolios page render
        sp = owner.get('/console/decisions').text; self.assertIn('Sampling plans', sp); self.assertIn(pid, sp)
        pf = owner.get('/console/portfolios'); self.assertEqual(pf.status_code, 200); self.assertIn('Verification portfolios', pf.text); self.assertIn('shared_wrong_model', pf.text)
        self.assertEqual(owner.post('/console/portfolios/create', name='p', components='byte_integrity,contract_binding').status_code, 303)
        self.assertIn('<strong>p</strong>', owner.get('/console/portfolios').text)
        self.assertEqual(viewer.get('/console/acquisition-plans/' + pid).status_code, 200); self.assertNotIn('hot_cell', viewer.get('/console/acquisition-plans/' + pid).text)


if __name__ == '__main__':
    unittest.main()
