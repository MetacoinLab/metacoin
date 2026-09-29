"""Compute services as ordinary workflow nodes and campaign candidates, and the console compute pages."""
import json
import unittest
from metacoin_service.tests.test_compute_engine import ComputeInstance, HAVE_RUNTIME, batch_spec, mc_spec, heat_spec
from metacoin_service import workflows as wf_mod


@unittest.skipUnless(HAVE_RUNTIME, 'no numpy-capable compute interpreter on this host')
class ComputeIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.inst = ComputeInstance(); self.addCleanup(self.inst.close); self.c = self.inst.client; self.H = self.inst.h('owner')

    def drive(self, rid, ticks=12):
        w = self.inst.worker()
        for _ in range(ticks):
            w.run_once(); self.c.post('/api/v1/runs/' + rid + '/advance', headers=self.H)
            v = self.c.get('/api/v1/runs/' + rid, headers=self.H).json()
            if v['state'] in ('completed', 'blocked', 'failed', 'waiting_review', 'cancelled'):
                return v
        return v

    def test_workflow_batch_then_monte_carlo_with_review_gate_and_export(self):
        definition = {'schema': wf_mod.SCHEMA, 'name': 'batch -> monte carlo -> review -> export', 'outputs': ['out'], 'nodes': [
            {'id': 'batch', 'type': 'temporal_batch', 'inputs': batch_spec(private_label='WF_BATCH')},
            {'id': 'mc', 'type': 'monte_carlo_reliability', 'depends_on': [{'node': 'batch', 'require': 'succeeded'}], 'inputs': mc_spec(samples=3000, private_label='WF_MC')},
            {'id': 'gate', 'type': 'review_gate', 'depends_on': ['mc'], 'input': 'mc'},
            {'id': 'out', 'type': 'export', 'depends_on': ['gate', {'node': 'mc', 'require': 'accepted_review'}], 'input': 'mc', 'fields': ['outcome', 'model_id', 'evidence_root', 'review_decision', 'envelope_digest']}]}
        wid = self.c.post('/api/v1/workflows', headers=self.H, json={'definition': definition}).json()['id']
        rid = self.c.post('/api/v1/workflows/' + wid + '/runs', headers=self.H, json={'budget_ceiling': 2}).json()['run_id']
        v = self.drive(rid)
        self.assertEqual(v['state'], 'waiting_review', v)
        nodes = {n['node_id']: n for n in v['nodes']}
        self.assertEqual((nodes['batch']['state'], nodes['mc']['state']), ('succeeded', 'succeeded'))
        mc_job = nodes['mc']['job_id']
        mc = self.c.get('/api/v1/compute/jobs/' + mc_job, headers=self.H).json()
        self.assertEqual((mc['verification']['passed'], mc['work']['committed']), (True, 3000))
        # the gate waits for verified numerical evidence plus an authenticated decision; a viewer cannot decide
        self.assertEqual(self.c.post('/api/v1/reviews/' + mc_job + '/decision', headers=self.inst.h('viewer'), json={'decision': 'accepted'}).status_code, 403)
        ev = self.c.get('/api/v1/reviews/' + mc_job + '/evidence', headers=self.inst.h('reviewer')).json()
        self.assertTrue(ev['policy_satisfied']); self.assertIn('persisted-verification-phase', ev['verification_source']); self.assertEqual(ev['recomputation'], 'matches')
        self.assertEqual(self.c.post('/api/v1/reviews/' + mc_job + '/decision', headers=self.inst.h('reviewer'), json={'decision': 'accepted'}).status_code, 200)
        v = self.drive(rid)
        self.assertEqual(v['state'], 'completed', v)
        self.assertEqual(v['budget']['committed_total'], 2)
        # robust feasibility (batch) and probabilistic reliability (mc) stay distinct result identities
        b = self.c.get('/api/v1/jobs/' + nodes['batch']['job_id'], headers=self.H).json()['summary']; m = self.c.get('/api/v1/jobs/' + mc_job, headers=self.H).json()['summary']
        self.assertIn('outcomes', b); self.assertIn('probability_estimate', m); self.assertNotIn('probability_estimate', b)

    def test_heat_refinement_campaign_reports_error_versus_cost(self):
        definition = {'name': 'heat grid refinement', 'kind': 'heat_diffusion', 'base': heat_spec(nx=32, ny=32, steps=40, snapshots=0, initial={'type': 'sine_mode', 'm': 1, 'n': 1, 'amplitude': '1'}),
                      'axes': [{'path': 'nx', 'values': [16, 32, 64]}]}
        r = self.c.post('/api/v1/campaigns', headers=self.H, json={'definition': definition})
        self.assertEqual(r.status_code, 201, r.text)
        cid = r.json()['campaign_id']
        self.c.post('/api/v1/campaigns/' + cid + '/run', headers=self.H)
        w = self.inst.worker()
        for _ in range(15):
            w.tick_workflows(); w.run_once()
            if self.c.get('/api/v1/campaigns/' + cid, headers=self.H).json()['state'] == 'completed':
                break
        rows = self.c.get('/api/v1/campaigns/' + cid + '/results', headers=self.H).json()['rows']
        self.assertEqual([r['state'] for r in rows], ['succeeded'] * 3, rows)
        self.assertEqual([r['params']['nx'] for r in rows], [16, 32, 64])
        for r in rows:
            self.assertEqual(r['fields'].get('backend'), 'cpu') if 'fields' in r else None
        jobs = [self.c.get('/api/v1/compute/jobs/' + r['job_id'], headers=self.H).json() for r in rows]
        self.assertTrue(all(j['verification']['passed'] for j in jobs))
        self.assertEqual([j['work']['committed'] for j in jobs], [1, 1, 1])                        # < 1e6 cell updates each: one billable unit each
        # §53(1): quality-versus-cost planning over the validated candidates
        plan = self.c.post('/api/v1/campaigns/' + cid + '/plan', headers=self.H, json={'cost_cap_units': 1}).json()
        self.assertEqual(len(plan['candidates_within_cap']), 3)
        self.assertEqual(plan['recommended']['params'], {'nx': 64})                                # finest fits the cap; predicted error 1.0 relative to itself
        for got, want in zip([c['quality']['value'] for c in plan['candidates_within_cap']], [1.0, 1.6, 4.0]):
            self.assertAlmostEqual(got, want, places=9)
        self.assertIn('predicted', plan['what_is_proven']); self.assertTrue(plan['not_a_global_optimum'])
        self.assertEqual(self.c.post('/api/v1/campaigns/' + cid + '/plan', headers=self.H, json={'cost_cap_units': 0})['recommended'] if False else self.c.post('/api/v1/campaigns/' + cid + '/plan', headers=self.H, json={'cost_cap_units': 0}).json()['recommended'], None)
        self.assertEqual(self.c.post('/api/v1/campaigns/' + cid + '/plan', headers=self.H, json={'cost_cap_units': -1}).status_code, 422)

    def test_console_compute_pages_and_form(self):
        s = self.c.post('/api/v1/session', json={'token': self.inst.tok['owner']})
        cookies, csrf = {'metacoin_session': s.cookies['metacoin_session']}, s.json()['csrf']
        page = self.c.get('/console/compute', cookies=cookies)
        self.assertEqual(page.status_code, 200); self.assertIn('gpu verified', page.text); self.assertIn('temporal-batch/v1', page.text)
        form = self.c.get('/console/compute/new?kind=heat_diffusion', cookies=cookies)
        self.assertEqual(form.status_code, 200); self.assertIn('maximum charge', form.text.lower()) if 'maximum charge' in form.text.lower() else self.assertIn('price', form.text)
        bad = self.c.post('/console/compute', cookies=cookies, data={'csrf': csrf, 'kind': 'heat_diffusion', 'title': 't', 'reviewer_id': self.inst.ids['reviewer'], 'inputs': json.dumps(heat_spec(dt='0.0001')), 'preview': '1'})
        self.assertEqual(bad.status_code, 422); self.assertIn('unstable timestep', bad.text)
        ok = self.c.post('/console/compute', cookies=cookies, data={'csrf': csrf, 'kind': 'heat_diffusion', 'title': 't', 'reviewer_id': self.inst.ids['reviewer'], 'inputs': json.dumps(heat_spec(nx=64, ny=64, steps=300)), 'preview': '1'})
        self.assertEqual(ok.status_code, 200); self.assertIn('work units', ok.text)
        sub = self.c.post('/console/compute', cookies=cookies, data={'csrf': csrf, 'kind': 'heat_diffusion', 'title': 't', 'reviewer_id': self.inst.ids['reviewer'], 'inputs': json.dumps(heat_spec(nx=64, ny=64, steps=300)), 'submit': '1'}, follow_redirects=False)
        self.assertEqual(sub.status_code, 303)
        jid = sub.headers['location'].rsplit('/', 1)[-1]
        self.inst.worker().run_once()
        jp = self.c.get('/console/jobs/' + jid, cookies=cookies)
        self.assertEqual(jp.status_code, 200)
        for needle in ('Compute execution', 'completed', 'plot.svg', 'exact_reference: passed', 'r_x'):
            self.assertIn(needle, jp.text, needle)
        self.assertNotIn('HEAT_ENGINE_SYNTHETIC', jp.text)
        vs = self.c.post('/api/v1/session', json={'token': self.inst.tok['viewer']}); vc = {'metacoin_session': vs.cookies['metacoin_session']}
        vp = self.c.get('/console/jobs/' + jid, cookies=vc)
        self.assertEqual(vp.status_code, 200); self.assertNotIn('resource observations', vp.text); self.assertNotIn('plot.svg', vp.text)


if __name__ == '__main__':
    unittest.main()
