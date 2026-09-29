"""Order 08 §76 extensions with a working user operation each: the reconciliation operator console (8) and mission learning
records (10)."""
import json
import unittest

from metacoin_service.tests.test_work_money import WorkMoneyTests, HAVE_CHAIN
from metacoin_service.tests import test_work_money as money_mod, test_work_access_missions as missions_mod
from metacoin_service.tests.test_work_access_missions import WorkAccessMissionTests
from metacoin_service.economy import legacy_bridge


@unittest.skipUnless(HAVE_CHAIN, 'needs the built local-chain artifacts')
class ReconciliationConsoleTests(WorkMoneyTests):
    locals().update({n: None for n in dir(WorkMoneyTests) if n.startswith('test_')})          # reuse the fixture, not the inherited tests

    def test_pending_observations_and_bounded_console_actions(self):
        # one lost response (submitted, unobserved) and one expired-unused authorization
        t, f, r, a = self.awarded('FEASIBLE', price=7); d = self.deliver_accept(a['id'])
        u = self.pay(d['entitlement']['id'], body={'_simulate_lost_response': True}, expect_state='unknown')
        t2, f2, r2, a2 = self.awarded('FEASIBLE', price=3); d2 = self.deliver_accept(a2['id']); eid2 = d2['entitlement']['id']
        i2 = self.c.post('/api/v1/work/entitlements/' + eid2 + '/prepare', headers=self.H, json={}).json(); self.c.post('/api/v1/work/intents/' + i2['id'] + '/authorize', headers=self.H, json={})
        with self.inst.app.state.services.db.tx() as db:
            db.execute('UPDATE payment_intents SET valid_until=1 WHERE id=?', (i2['id'],))
        v = self.c.get('/api/v1/work/reconciliation', headers=self.H); self.assertEqual(v.status_code, 200, v.text); v = v.json()
        by = {x['intent_id']: x for x in v['items']}
        self.assertEqual(by[u['id']]['suggested_action'], 'reconcile'); self.assertEqual(by[i2['id']]['suggested_action'], 'reconcile'); self.assertTrue(by[i2['id']]['expired'])
        # console: the page renders the pending rows; the reconcile action records the observation; the viewer role gets no action
        login = self.c.post('/console/login', data={'token': self.inst.tok['owner']}, follow_redirects=False); self.assertEqual(login.status_code, 303)
        page = self.c.get('/console/work/reconciliation'); self.assertEqual(page.status_code, 200); self.assertIn(u['id'], page.text); self.assertIn('Reconcile', page.text)
        csrf = page.text.split('name="csrf" value="')[1].split('"')[0]
        p0 = self.bal('requester_payer')
        act = self.c.post('/console/work/reconciliation/%s/reconcile' % u['id'], data={'csrf': csrf}, follow_redirects=False); self.assertEqual(act.status_code, 303, act.text)
        self.assertEqual(self.c.get('/api/v1/work/intents/' + u['id'], headers=self.H).json()['state'], 'settled'); self.assertEqual(self.bal('requester_payer'), p0)     # observed, not re-spent
        act2 = self.c.post('/console/work/reconciliation/%s/reconcile' % i2['id'], data={'csrf': csrf}, follow_redirects=False); self.assertEqual(act2.status_code, 303)
        v2 = {x['intent_id']: x for x in self.c.get('/api/v1/work/reconciliation', headers=self.H).json()['items']}
        self.assertNotIn(u['id'], v2); self.assertEqual(v2[i2['id']]['suggested_action'], 'renew')
        ren = self.c.post('/console/work/reconciliation/%s/renew' % i2['id'], data={'csrf': csrf}, follow_redirects=False); self.assertEqual(ren.status_code, 303)
        ints = [i for i in self.c.get('/api/v1/work/intents', headers=self.H).json()['items'] if i['entitlement_id'] == eid2]; self.assertEqual(len(ints), 2); self.assertEqual(sorted(i['state'] for i in ints), ['expired', 'prepared'])
        self.assertEqual(self.c.post('/console/work/reconciliation/%s/renew' % i2['id'], data={'csrf': csrf}, follow_redirects=False).status_code, 303)
        self.assertEqual(len([i for i in self.c.get('/api/v1/work/intents', headers=self.H).json()['items'] if i['entitlement_id'] == eid2]), 2)                       # a repeated renewal replays the prepared intent; no third identity
        self.assertEqual(self.c.post('/console/work/reconciliation/%s/void' % i2['id'], data={'csrf': csrf}, follow_redirects=False).status_code, 422)                 # no unbounded action exists
        rep = self.c.post('/api/v1/work/journal/replay', headers=self.H, json={}).json(); self.assertTrue(rep['consistent'], rep['differences'])
        self.c.post('/console/logout', data={'csrf': csrf}, follow_redirects=False)
        self.c.post('/console/login', data={'token': self.inst.tok['viewer']}, follow_redirects=False)
        vp = self.c.get('/console/work/reconciliation'); self.assertEqual(vp.status_code, 200); self.assertIn('no action for your role', vp.text); self.assertNotIn('>Reconcile<', vp.text)


class MissionLearningTests(WorkAccessMissionTests):
    locals().update({n: None for n in dir(WorkAccessMissionTests) if n.startswith('test_')})

    def test_learning_records_classify_findings_and_decision_change_is_explicit(self):
        pf = self.c.post('/api/v1/work/missions/import', headers=self.H, json={}).json()
        dr = self.c.post('/api/v1/work/missions/%s/bottlenecks/task-0018/draft' % pf['id'], headers=self.H, json={'ceiling': 3}).json()
        self.c.post('/api/v1/work/terms/' + dr['terms']['id'] + '/freeze', headers=self.H, json={'inputs': dr['suggested_inputs_for_freeze']})
        r = self.c.post('/api/v1/work/requests', headers=self.H, json={'terms_id': dr['terms']['id']}).json(); self.c.post('/api/v1/work/requests/' + r['id'] + '/open', headers=self.H, json={})
        self.c.post('/api/v1/work/missions/%s/link' % pf['id'], headers=self.H, json={'link_id': dr['link_id'], 'request_id': r['id']})
        o = self.c.post('/api/v1/work/requests/' + r['id'] + '/offers', headers=self.pv['alpha']['h'], json={'price_amount': 3, 'asset': 'action-units', 'scheme': 'exact', 'window_seconds': 3600, 'verification': {'class': 'none'}}).json()
        a = self.c.post('/api/v1/work/requests/' + r['id'] + '/award', headers=self.H, json={'offer_id': o['id']}).json(); self.run_worker(); self.decide(a['id'])
        view = self.c.get('/api/v1/work/missions/' + pf['id'], headers=self.H).json()
        c = view['contributions'][0]; self.assertEqual(c['learning']['class'], 'confirmed_declared_verdict'); self.assertFalse(c['learning']['decision_changed']); self.assertTrue(c['learning']['no_impact_score'])
        self.assertEqual(view['learning']['by_class']['confirmed_declared_verdict'], 1); self.assertEqual(view['learning']['decisions_changed'], []); self.assertNotIn('impact_score', json.dumps(view['learning']['by_class']))
        # an explicit requester record of a decision change; a viewer may not record one; malformed bodies are refused
        bad = self.c.post('/api/v1/work/missions/%s/contributions/%s/learning' % (pf['id'], c['id']), headers=self.H, json={'decision_changed': 'yes'}); self.assertIn(bad.status_code, (400, 422))
        self.assertEqual(self.c.post('/api/v1/work/missions/%s/contributions/%s/learning' % (pf['id'], c['id']), headers=self.inst.h('viewer'), json={'decision_changed': True}).status_code, 403)
        rec = self.c.post('/api/v1/work/missions/%s/contributions/%s/learning' % (pf['id'], c['id']), headers=self.H, json={'decision_changed': True, 'note': 'replication confirmed; the plan keeps task-0018 as a constraining node'}); self.assertEqual(rec.status_code, 200, rec.text)
        view2 = self.c.get('/api/v1/work/missions/' + pf['id'], headers=self.H).json(); self.assertEqual(view2['learning']['decisions_changed'], [c['id']]); self.assertEqual(view2['contributions'][0]['learning']['recorded_by'], self.inst.ids['owner'])
        self.assertEqual(self.c.post('/api/v1/work/missions/%s/contributions/wc_nope/learning' % pf['id'], headers=self.H, json={'decision_changed': False}).status_code, 404)


if __name__ == '__main__':
    unittest.main()
