"""Interfaces of the native protocol (order §17; journey 37): every native CLI command parses and maps to one authenticated
request (offline commands run locally); MCP tools separate read / simulate / commit / spend by annotation and route to the
same API; the console shows the domain, machine account, settlement, treasury, mission, product and recovery views and
enforces the same authority as the API (a viewer reads, cannot spend)."""
import asyncio
import json
import os
import tempfile
import unittest

from metacoin_service import client_cli, mcp_server
from metacoin_service.tests.test_work_surfaces import ConsoleSession, _build_parser
from metacoin_service.tests.test_native_lunar import NativeLunarTests
from metacoin_service.lunar.__main__ import example


class NativeCliTests(unittest.TestCase):
    def test_every_native_command_parses_and_maps_to_one_request(self):
        parser = _build_parser(); calls = []
        def go(method, path, body=None, raw=False, idempotency_key=None, content_type=None):
            calls.append((method, path, body, idempotency_key, raw)); return 200, ({'id': 'x', 'credential': {'token': 't'}, 'domain': 'dom-a', 'receipts': [], 'intents': []} if not raw else b'zip')
        d = tempfile.mkdtemp(); jf = os.path.join(d, 'in.json'); open(jf, 'w').write(json.dumps({'domain_id': 'dom-b', 'authority_key_id': 'k', 'authority_public_hex': '00' * 32, 'epoch': 1}))
        cases = [(['native-domain'], ('GET', '/api/v1/native/domain', None)), (['native-domain-init', 'dom-a', '--idempotency-key', 'k1'], ('POST', '/api/v1/native/domain', 'k1')), (['native-accounts'], ('GET', '/api/v1/native/accounts', None)),
                 (['native-account', 'na_1'], ('GET', '/api/v1/native/accounts/na_1', None)), (['native-fund', 'na_1', '5', '--idempotency-key', 'k2'], ('POST', '/api/v1/native/fixtures/fund', 'k2')),
                 (['native-transfer', 'na_2', '3', '--request-id', 'r1', '--idempotency-key', 'k3'], ('POST', '/api/v1/native/transfers', 'k3')), (['native-simulate', 'na_2', '3', '--request-id', 'r1'], ('POST', '/api/v1/native/transfers/simulate', None)),
                 (['native-intents', '--account', 'na_1'], ('GET', '/api/v1/native/intents?account=na_1', None)), (['native-export', 'dom-b', 'na_9', '7', '--request-id', 'e1', '--idempotency-key', 'k4'], ('POST', '/api/v1/native/allocations/export', 'k4')),
                 (['native-allocation', 'nal_1'], ('GET', '/api/v1/native/allocations/nal_1', None)), (['native-allocation-return', 'nal_1', '--idempotency-key', 'k5'], ('POST', '/api/v1/native/allocations/nal_1/return', 'k5')),
                 (['native-peer-pin', '--file', jf, '--idempotency-key', 'k6'], ('POST', '/api/v1/native/peers', 'k6')), (['native-outbox', '--receiver', 'dom-b'], ('GET', '/api/v1/native/transport/outbox?receiver=dom-b&transport=online', None)),
                 (['native-inbox', '--file', jf, '--transport', 'bundle'], ('POST', '/api/v1/native/transport/inbox?transport=bundle', None)), (['native-observation', '--peer', 'dom-b'], ('POST', '/api/v1/native/transport/observation', None)),
                 (['native-rotate', '--grace-seconds', '10'], ('POST', '/api/v1/native/domain/rotate', None)), (['native-connectivity', 'cut'], ('POST', '/api/v1/native/domain/connectivity', None)), (['native-recovery'], ('GET', '/api/v1/native/recovery', None)),
                 (['native-reconcile', '--file', jf], ('POST', '/api/v1/native/recovery/reconcile', None)), (['native-audit', '--asset', 'a'], ('GET', '/api/v1/native/audit?asset=a', None)), (['native-spec'], ('GET', '/api/v1/native/spec', None)),
                 (['native-disputes'], ('GET', '/api/v1/native/disputes', None)), (['native-dispute-open', 'no_1', '--kind', 'delivery_mismatch', '--claim', 'c'], ('POST', '/api/v1/native/disputes', None)), (['native-dispute-resolve', 'nd_1', '--outcome', 'uphold'], ('POST', '/api/v1/native/disputes/nd_1/resolve', None)),
                 (['procurement-plan', '--file', jf], ('POST', '/api/v1/native/procurement/plan', None)), (['procurement-benchmark', '--missions', '5'], ('GET', '/api/v1/native/procurement/benchmark?missions=5&seed=1', None)),
                 (['lunar-sources'], ('GET', '/api/v1/lunar/sources', None)), (['lunar-product', 'j_1'], ('GET', '/api/v1/lunar/products/j_1', None)), (['lunar-example', 'route', '--source', 'tycho_48'], ('GET', '/api/v1/lunar/examples/route?source=tycho_48', None))]
        for argv, (method, path, key) in cases:
            calls.clear(); ns = parser.parse_args(['--credential-file', 'x'] + argv); ns.command = argv[0]
            status, out = client_cli.native(ns, go)
            self.assertEqual(len(calls), 1, argv); self.assertEqual((calls[0][0], calls[0][1], calls[0][3]), (method, path, key), argv)
        for c in client_cli.NATIVE_COMMANDS:
            self.assertTrue(any(c == a.dest or c in str(a.choices) for a in parser._subparsers._group_actions), c)
        # the actor credential goes to a private file, never to stdout
        out = os.path.join(d, 'actor.json'); ns = parser.parse_args(['--credential-file', 'x', 'native-actor-create', 'm', '--out', out]); ns.command = 'native-actor-create'
        st, res = client_cli.native(ns, go); self.assertNotIn('credential', res); self.assertEqual(oct(os.stat(out).st_mode & 0o777), '0o600'); self.assertEqual(json.load(open(out))['token'], 't')
        # offline commands need no API
        for argv in (['native-vectors'], ['native-model', '--depth', '3']):
            ns = parser.parse_args(['--credential-file', 'x'] + argv); ns.command = argv[0]; calls.clear()
            st, res = client_cli.native(ns, go); self.assertEqual(st, 200, res); self.assertEqual(calls, [])


class NativeMcpTests(unittest.TestCase):
    def test_tools_separate_read_simulate_commit_and_spend(self):
        calls = []
        class Fake:
            def call(self, method, path, body=None, idempotency_key=None):
                calls.append((method, path, body, idempotency_key)); return (201 if method == 'POST' and 'simulate' not in path and 'procurement' not in path and 'prepare' in path or (method == 'POST' and path.endswith(('/transfers', '/export', '/disputes'))) else 200), {'id': 'i_1', 'state': 'authorized'}
        m = mcp_server.build(Fake()); tools = {t.name: t for t in asyncio.run(m.list_tools())}
        for name in ('native_domain_status', 'native_account', 'native_audit', 'native_simulate_transfer', 'native_transfer', 'native_pay_entitlement', 'native_export_allocation', 'native_allocation_status', 'native_treasury', 'native_open_dispute', 'procurement_plan', 'lunar_product', 'lunar_sources'):
            self.assertIn(name, tools)
        for ro in ('native_domain_status', 'native_account', 'native_audit', 'native_simulate_transfer', 'procurement_plan', 'lunar_product', 'native_treasury'):
            self.assertTrue(tools[ro].annotations.readOnlyHint, ro); self.assertFalse(tools[ro].annotations.destructiveHint, ro)
        for sp in ('native_transfer', 'native_pay_entitlement'):
            self.assertFalse(tools[sp].annotations.readOnlyHint); self.assertTrue(tools[sp].annotations.destructiveHint); self.assertIn('SPEND', tools[sp].description)
        self.assertFalse(tools['native_export_allocation'].annotations.readOnlyHint); self.assertFalse(tools['native_export_allocation'].annotations.destructiveHint); self.assertIn('COMMIT', tools['native_export_allocation'].description)
        self.assertIn('SIMULATE', tools['native_simulate_transfer'].description); self.assertIn('Never funds', tools['native_domain_status'].description)
        asyncio.run(m.call_tool('native_simulate_transfer', {'to_account': 'na_2', 'amount': 3, 'request_id': 'r'})); self.assertEqual(calls[-1][:2], ('POST', '/api/v1/native/transfers/simulate'))
        asyncio.run(m.call_tool('native_transfer', {'to_account': 'na_2', 'amount': 3, 'request_id': 'r'})); self.assertEqual(calls[-1][:2], ('POST', '/api/v1/native/transfers'))
        asyncio.run(m.call_tool('native_pay_entitlement', {'entitlement_id': 'we_1', 'idempotency_key': 'p1'}))
        self.assertEqual([c[1] for c in calls[-3:]], ['/api/v1/work/entitlements/we_1/prepare', '/api/v1/work/intents/i_1/authorize', '/api/v1/work/intents/i_1/submit']); self.assertEqual([c[3] for c in calls[-3:]], ['p1', 'p1-auth', 'p1-submit'])
        self.assertFalse(any('fixtures/fund' in c[1] for c in calls))                                                     # no tool can fund an account


class NativeConsoleTests(NativeLunarTests):
    def test_console_views_show_the_native_story_and_enforce_authority(self):
        R, M = self.actors['requester']['h'], self.actors['machine']['h']
        self.provider_lunar('machine'); self.fund('requester', 80)
        inputs = example('route'); inputs['private_label'] = 'CONSOLE_NATIVE'
        t, r, a = self.lunar_award(R, M, 'lunar_route_feasibility', inputs, 40, bps=500, verifier=3, verifier_pay_to=self.actors['verifier']['account'])
        self.deliver_accept(a['id'], R); self.pay_all(a['id'], R)
        owner = ConsoleSession(self.c, self.inst.tok['owner']); viewer = ConsoleSession(self.c, self.inst.tok['viewer']); req = ConsoleSession(self.c, self.actors['requester']['h']['Authorization'].split(' ')[1])
        p = owner.get('/console/native'); self.assertEqual(p.status_code, 200, p.text[-500:]); self.assertIn('dom-a', p.text); self.assertIn('conserved', p.text); self.assertIn('required by the native core: False', p.text); self.assertIn('Byzantine', p.text)
        acct = self.actors['machine']['account']
        p = owner.get('/console/native/accounts/' + acct); self.assertEqual(p.status_code, 200, p.text[-500:]); self.assertIn('earned, unsettled', p.text); self.assertIn('pending inter-domain claims', p.text); self.assertIn('<strong>40</strong>', p.text)
        p = owner.get('/console/native/settlement'); self.assertEqual(p.status_code, 200, p.text[-500:]); self.assertIn('Local finality', p.text); self.assertIn('Remote acknowledgment', p.text); self.assertIn('Unresolved exposure', p.text)
        p = owner.get('/console/native/treasury'); self.assertEqual(p.status_code, 200, p.text[-500:]); self.assertIn('available 2', p.text); self.assertIn('never', p.text.lower())
        p = owner.get('/console/native/missions/' + a['id']); self.assertEqual(p.status_code, 200, p.text[-800:]); self.assertIn('Deliverable', p.text); self.assertIn('Acceptance', p.text); self.assertIn('Resource envelope', p.text); self.assertIn('Maximum obligation', p.text); self.assertIn('Selected provider', p.text); self.assertIn('<strong>45</strong>', p.text)
        jid = self.c.get('/api/v1/work/awards/' + a['id'], headers=self.H).json()['milestones'][0]['attempts'][-1]['job_id']
        p = owner.get('/console/lunar/' + jid); self.assertEqual(p.status_code, 200, p.text[-500:]); self.assertIn('LDEM_4', p.text); self.assertIn('Assumptions', p.text); self.assertIn('Reproduction', p.text); self.assertIn('preview.png', p.text)
        p = owner.get('/console/native/recovery'); self.assertEqual(p.status_code, 200); self.assertIn('normal', p.text)
        # the viewer reads inspection views but holds no authority to spend; the requester's own account page offers the transfer form
        p = viewer.get('/console/native'); self.assertEqual(p.status_code, 200); self.assertNotIn('Transfer (spend)', p.text)
        p = viewer.get('/console/native/accounts/' + acct); self.assertEqual(p.status_code, 200); self.assertNotIn('Transfer (spend)', p.text)
        p = req.get('/console/native/accounts/' + self.actors['requester']['account']); self.assertEqual(p.status_code, 200); self.assertIn('Transfer (spend)', p.text)
        sim = req.post('/console/native/accounts/' + self.actors['requester']['account'] + '/transfer', to_account=acct, amount='5', request_id='console-sim'); self.assertEqual(sim.status_code, 200, sim.text[-300:]); self.assertIn('would_succeed', sim.text)
        self.assertEqual(self.bal('requester'), 35)                                                                        # simulate moved nothing
        done = req.post('/console/native/accounts/' + self.actors['requester']['account'] + '/transfer', to_account=acct, amount='5', request_id='console-real', commit='1'); self.assertEqual(done.status_code, 303, done.text[-300:])
        self.assertEqual((self.bal('requester'), self.bal('machine')), (30, 45))
        rv = viewer.post('/console/native/accounts/' + self.actors['requester']['account'] + '/transfer', to_account=acct, amount='5', request_id='console-viewer', commit='1'); self.assertIn(rv.status_code, (403, 400))
        self.assertEqual(self.bal('requester'), 30)


if __name__ == '__main__':
    unittest.main()
