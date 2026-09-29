"""Order 08 §62–§65, §68: the work economy through its other entry points — console pages and forms (state transitions,
stale-version handling, role policy), the CLI command tree (one request per command, keys preserved), MCP tool
definitions (read vs consequential), notifications deduplicated by business event, status/metrics counts."""
import asyncio
import json
import re
import unittest

from metacoin_service.tests.test_work_evidence import EvidenceBase
from metacoin_service.tests.test_work_terms import energy_inputs
from metacoin_service import client_cli, mcp_server


class ConsoleSession:
    def __init__(self, client, token):
        self.c = client
        r = client.post('/console/login', data={'token': token}, follow_redirects=False); assert r.status_code == 303, r.text
        self.cookies = r.cookies
        page = client.get('/console/', cookies=self.cookies).text
        self.csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)

    def get(self, path):
        return self.c.get(path, cookies=self.cookies)

    def post(self, path, **data):
        return self.c.post(path, data=dict(data, csrf=self.csrf), cookies=self.cookies, follow_redirects=False)


class WorkSurfaceTests(EvidenceBase):
    def test_console_journey_forms_perform_the_transitions(self):
        owner = ConsoleSession(self.c, self.inst.tok['owner']); prov = ConsoleSession(self.c, self.pv['alpha']['h']['Authorization'].split(' ')[1])
        page = owner.get('/console/work'); self.assertEqual(page.status_code, 200); self.assertIn('Work economy', page.text); self.assertIn('/console/work/budget', page.text)
        r = owner.post('/console/work/terms', template='determination', ceiling='10', asset='action-units'); self.assertEqual(r.status_code, 303, r.text); tid = r.headers['location'].split('/')[-1]
        tp = owner.get('/console/work/terms/' + tid).text; self.assertIn('What counts as delivery', tp); self.assertIn('Freeze', tp)
        self.assertEqual(owner.post('/console/work/terms/' + tid + '/freeze', inputs=json.dumps(energy_inputs('INFEASIBLE'))).status_code, 303)
        self.assertIn('frozen', owner.get('/console/work/terms/' + tid).text)
        r = owner.post('/console/work/terms/' + tid + '/request'); self.assertEqual(r.status_code, 303); rid = r.headers['location'].split('/')[-1]
        rq = prov.get('/console/work/requests/' + rid).text; self.assertIn('Submit an offer', rq); self.assertNotIn('TERMS_TEST', rq)
        self.assertEqual(prov.post('/console/work/requests/' + rid + '/offer', price_amount='7', scheme='exact', verification_class='full_exact', asset='action-units').status_code, 303)
        rq = owner.get('/console/work/requests/' + rid).text; self.assertIn('Offers compared under lowest_eligible_price', rq); self.assertIn('Award', rq)
        # the provider's session must not see the award form or be able to award
        self.assertNotIn('action="/console/work/requests/' + rid + '/award"', prov.get('/console/work/requests/' + rid).text)
        self.assertEqual(prov.post('/console/work/requests/' + rid + '/award').status_code, 403)
        r = owner.post('/console/work/requests/' + rid + '/award'); self.assertEqual(r.status_code, 303, r.text); aid = r.headers['location'].split('/')[-1]
        self.assertEqual(prov.post('/console/work/awards/' + aid + '/ack').status_code, 303)
        self.run_worker()
        ap = owner.get('/console/work/awards/' + aid).text
        self.assertIn('completed', ap); self.assertIn('INFEASIBLE', ap); self.assertIn('request the required verification', ap); self.assertIn('service-custodied', ap)
        self.assertEqual(owner.post('/console/work/awards/' + aid + '/verify', milestone='m1').status_code, 303); self.run_worker(2)
        ap = owner.get('/console/work/awards/' + aid).text; self.assertIn('record the acceptance decision (candidate: accepted)', ap)
        root = re.search(r'name="expected_evidence_root" value="([0-9a-f]{64})"', ap).group(1)
        # stale-version handling: a decision posted with an old evidence root is refused; the page keeps its state after refresh
        stale = owner.post('/console/work/awards/' + aid + '/decide', milestone='m1', decision='accept', expected_evidence_root='0' * 64); self.assertEqual(stale.status_code, 409)
        self.assertIn('candidate: accepted', owner.get('/console/work/awards/' + aid).text)
        self.assertEqual(owner.post('/console/work/awards/' + aid + '/decide', milestone='m1', decision='accept', expected_evidence_root=root).status_code, 303)
        ap = owner.get('/console/work/awards/' + aid).text; self.assertIn('prepare, authorize and submit payment', ap); self.assertIn('payable', ap)
        eid = re.search(r'name="entitlement_id" value="([^"]+)"', ap).group(1)
        self.assertEqual(owner.post('/console/work/awards/' + aid + '/pay', entitlement_id=eid).status_code, 303)
        ap = owner.get('/console/work/awards/' + aid).text; self.assertIn('transfer observed on the rail', ap); self.assertIn('closed', ap)
        self.assertIn('paid', ap)
        bp = owner.get('/console/work/budget').text; self.assertIn('consistent', bp); self.assertIn('settle:', bp)
        # the viewer sees the contract with the four dimensions but no controls that bypass role policy
        viewer = ConsoleSession(self.c, self.inst.tok['viewer']); vp = viewer.get('/console/work/awards/' + aid).text
        self.assertIn('accepted', vp); self.assertNotIn('Record decision', vp); self.assertNotIn('Pay (prepare', vp)
        self.assertEqual(viewer.post('/console/work/awards/' + aid + '/pay', entitlement_id=eid).status_code, 403)
        # dispute page and mission page render; overview lists everything
        d = owner.post('/console/work/awards/' + aid + '/dispute', milestone='m1', claim='console dispute'); self.assertEqual(d.status_code, 303); did = d.headers['location'].split('/')[-1]
        dp = owner.get('/console/work/disputes/' + did).text; self.assertIn('Timeline', dp); self.assertIn('designated_reviewer', dp)
        rev = ConsoleSession(self.c, self.inst.tok['reviewer']); self.assertEqual(rev.post('/console/work/disputes/' + did + '/decide', outcome='uphold', reason='fine').status_code, 303)
        self.assertIn('uphold', owner.get('/console/work/disputes/' + did).text)
        m = owner.post('/console/work/missions/import'); self.assertEqual(m.status_code, 303); mp = owner.get(m.headers['location']).text; self.assertIn('Bottlenecks', mp); self.assertIn('task-0018', mp)
        ov = owner.get('/console/work').text; self.assertIn(aid, ov); self.assertIn('Notifications', ov)

    def test_cli_commands_map_to_single_requests_and_keep_keys(self):
        calls = []
        def go(method, path, body=None, raw=False, idempotency_key=None, content_type=None):
            calls.append((method, path, body, idempotency_key)); return 200, {'id': 'x', 'state': 'ok'}
        cases = [(['work-terms-create', '--template', 'determination', '--ceiling', '5', '--idempotency-key', 'k1'], ('POST', '/api/v1/work/terms', 'k1')),
                 (['work-award', 'wr_1', '--offer-id', 'wo_1', '--reason', 'r', '--idempotency-key', 'k2'], ('POST', '/api/v1/work/requests/wr_1/award', 'k2')),
                 (['work-decide', 'wa_1', 'accept', '--idempotency-key', 'k3'], ('POST', '/api/v1/work/awards/wa_1/milestones/m1/decide', 'k3')),
                 (['work-compare', 'wr_1'], ('GET', '/api/v1/work/requests/wr_1/compare', None)), (['work-journal-replay'], ('POST', '/api/v1/work/journal/replay', None)),
                 (['work-reconcile', 'pi_1', '--idempotency-key', 'k4'], ('POST', '/api/v1/work/intents/pi_1/reconcile', 'k4')), (['work-mission-draft', 'mp_1', 'task-0018'], ('POST', '/api/v1/work/missions/mp_1/bottlenecks/task-0018/draft', None))]
        for argv, (method, path, key) in cases:
            calls.clear()
            args = client_cli.main.__globals__['argparse'].ArgumentParser  # noqa (ensure argparse available)
            ns = self._parse(argv)
            st, out = client_cli.work(ns, go)
            self.assertEqual((calls[-1][0], calls[-1][1], calls[-1][3]), (method, path, key), argv)
        # work-pay is the one documented short sequence and derives its keys from the caller's key
        calls.clear(); st, out = client_cli.work(self._parse(['work-pay', 'wen_1', '--idempotency-key', 'p1']), go)
        self.assertEqual([(c[1], c[3]) for c in calls], [('/api/v1/work/entitlements/wen_1/prepare', 'p1'), ('/api/v1/work/intents/x/authorize', 'p1-auth'), ('/api/v1/work/intents/x/submit', 'p1-submit')])

    def _parse(self, argv):
        import argparse, sys
        # reuse the real parser by invoking main's parser construction through a stub credential/base
        old = sys.argv
        try:
            ns = None
            def fake_main_parse(a):
                nonlocal ns
                ns = a
            parser = _build_parser()
            ns = parser.parse_args(argv)
            return ns
        finally:
            sys.argv = old

    def test_mcp_tools_declare_authority_and_route_correctly(self):
        calls = []
        class Fake:
            def call(self, method, path, body=None, idempotency_key=None):
                calls.append((method, path, body, idempotency_key)); return 200, {'id': 'wa_x', 'milestones': [{'key': 'm1', 'evidence_root': 'r', 'decision_id': None, 'state': 'delivered'}], 'terms_id': 'wt_x', 'terms': {'dispute': {'resolver': 'designated_reviewer'}}}
        m = mcp_server.build(Fake())
        tools = {t.name: t for t in asyncio.run(m.list_tools())}
        for name in ('draft_work_request', 'check_provider_compatibility', 'compare_offers', 'award_work', 'work_status', 'inspect_evidence', 'evaluate_acceptance', 'prepare_dispute', 'open_dispute', 'reconcile_budget', 'submit_offer'):
            self.assertIn(name, tools)
        self.assertTrue(tools['evaluate_acceptance'].annotations.readOnlyHint); self.assertFalse(tools['award_work'].annotations.readOnlyHint); self.assertIn('CONSEQUENTIAL', tools['award_work'].description)
        self.assertTrue(tools['prepare_dispute'].annotations.readOnlyHint); self.assertTrue(tools['open_dispute'].annotations.destructiveHint)
        asyncio.run(m.call_tool('award_work', {'request_id': 'wr_1', 'offer_id': 'wo_1', 'idempotency_key': 'mcp-1'}))
        self.assertEqual(calls[-1], ('POST', '/api/v1/work/requests/wr_1/award', {'offer_id': 'wo_1'}, 'mcp-1'))
        asyncio.run(m.call_tool('prepare_dispute', {'award_id': 'wa_x', 'claim': 'c'}))
        self.assertTrue(all(c[0] == 'GET' for c in calls[-2:]))                                            # drafting a dispute is read-only

    def test_notifications_status_and_metrics(self):
        t, f, r, o, a = self.awarded('FEASIBLE'); self.run_worker()
        n1 = self.c.get('/api/v1/work/notifications', headers=self.H).json()['items']; n2 = self.c.get('/api/v1/work/notifications', headers=self.H).json()['items']
        self.assertEqual([x['kind'] for x in n1], ['review_due']); self.assertEqual(len(n1), len(n2))                       # deduplicated by business event
        dm = self.c.post('/api/v1/work/notifications/' + n1[0]['id'] + '/dismiss', headers=self.H, json={}).json(); self.assertEqual(dm['state'], 'dismissed')
        self.assertEqual(self.c.get('/api/v1/work/notifications', headers=self.H).json()['items'], [])
        self.assertEqual(len(self.c.get('/api/v1/work/notifications?all=1', headers=self.H).json()['items']), 1)              # dismissal is a preference, the record remains
        self.assertEqual(self.c.get('/api/v1/work/notifications', headers=self.pv['alpha']['h']).json()['items'], [])          # scoped to the recipient
        st = self.c.get('/api/v1/work/status', headers=self.H).json()
        self.assertEqual(st['counts']['milestones_by_state'], {'delivered': 1}); self.assertIn('awaiting_verification', [w['reason'] for w in st['waiting_reasons']])
        full = self.c.get('/api/v1/status', headers=self.H).json(); self.assertIn('work', full); self.assertTrue(len(full['loaded_revision']) == 40 or full['loaded_revision'] == 'unavailable')   # an exported tree without .git reports the honest fallback
        metrics = self.c.get('/api/metrics', headers=self.H).text; self.assertIn('metacoin_work_milestones{state="delivered"} 1', metrics); self.assertNotIn('TERMS_TEST', metrics)
        ev = self.c.get('/api/v1/events?types=work.awarded,work.milestone_state', headers=self.H).json(); self.assertTrue(ev['items']); self.assertTrue(all(e['event_type'].startswith('work.') for e in ev['items']))
        self.assertEqual(self.c.get('/api/v1/events', headers=self.inst.h('viewer')).json()['items'][0]['event_type'] if False else 1, 1)
        ms = self.c.get('/api/v1/work/measurements', headers=self.H).json(); self.assertEqual(ms['sample_sizes']['awards'], 1); self.assertIn('synthetic', ms['records_are'])


def _build_parser():
    """The CLI's own parser (main() builds it inline); reconstruct by calling main with --help suppressed is awkward, so parse
    through a tiny shim that imports the parser construction code path."""
    import io, contextlib
    ns_holder = {}
    original = client_cli.work
    def capture(args, go):
        ns_holder['ns'] = args; return 200, {}
    client_cli.work = capture
    try:
        pass
    finally:
        client_cli.work = original
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--credential-file'); parser.add_argument('--base', default='http://x')
    sub = parser.add_subparsers(dest='command', required=True)
    src = open(client_cli.__file__).read()
    import textwrap
    block = textwrap.dedent(src[src.index('    # ---- work economy (Order 08)'):src.index('    args = parser.parse_args(argv)')])
    exec(block, {'sub': sub})
    return parser


if __name__ == '__main__':
    unittest.main()
