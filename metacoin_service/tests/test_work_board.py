"""Group B (Order 08 §20–§26): providers with signed capability revisions, the private request board, eligibility with
structured reasons, binding offers, transparent comparison under the declared selection policy, the atomic award with
budget reservation, lost-response retry, single-award races, provider revision after offer, and milestone gating."""
import json
import threading
import unittest

from metacoin_service.tests.test_work_terms import TermsInstance, energy_inputs


class WorkBoardTests(unittest.TestCase):
    def setUp(self):
        self.inst = TermsInstance(); self.addCleanup(self.inst.close); self.c = self.inst.client; self.H = self.inst.h('owner')
        self.w = self.inst.worker(); self.addCleanup(self.w.offline)
        self.pv = {}
        for name, caps in (('alpha', {'kinds': ['energy_audit', 'legacy_task_replay'], 'verification_classes': ['full_exact'], 'payment_schemes': ['exact', 'upto']}),
                           ('beta', {'kinds': ['energy_audit'], 'verification_classes': ['full_exact', 'analytical'], 'payment_schemes': ['exact']}),
                           ('gamma', {'kinds': ['resource_plan'], 'verification_classes': ['analytical'], 'payment_schemes': ['exact']})):
            r = self.c.post('/api/v1/work/providers', headers=self.H, json={'name': name, 'capabilities': caps, 'pay_to': 'provider:' + name})
            self.assertEqual(r.status_code, 201, r.text); p = r.json()
            self.pv[name] = {'id': p['id'], 'h': {'Authorization': 'Bearer ' + p['credential']['token']}, 'principal_id': p['principal_id']}
            self.assertEqual(p['signature']['custody'], 'service-custodied'); self.assertEqual(p['relationship']['relationship'], 'same_operator')

    def terms(self, **kw):
        t = self.c.post('/api/v1/work/terms', headers=self.H, json=dict({'template': 'determination', 'ceiling': 10}, **kw)).json()
        return t

    def frozen_request(self, outcome='FEASIBLE', open_it=True, **kw):
        t = self.terms(**kw)
        f = self.c.post('/api/v1/work/terms/' + t['id'] + '/freeze', headers=self.H, json={'inputs': energy_inputs(outcome)}); self.assertEqual(f.status_code, 200, f.text)
        r = self.c.post('/api/v1/work/requests', headers=self.H, json={'terms_id': t['id']}); self.assertEqual(r.status_code, 201, r.text); r = r.json()
        if open_it:
            r = self.c.post('/api/v1/work/requests/' + r['id'] + '/open', headers=self.H, json={}).json(); self.assertEqual(r['state'], 'open')
        return t, f.json(), r

    def offer(self, name, rid, price=5, **kw):
        body = dict({'price_amount': price, 'asset': 'action-units', 'scheme': 'exact', 'window_seconds': 3600, 'verification': {'class': 'full_exact', 'distinct_verifier': False}}, **kw)
        r = self.c.post('/api/v1/work/requests/' + rid + '/offers', headers=self.pv[name]['h'], json=body); self.assertEqual(r.status_code, 201, r.text); return r.json()

    # J1 + J2 ---------------------------------------------------------------------------------------------------------
    def test_request_from_real_operation_offers_eligibility_and_award(self):
        t, f, r = self.frozen_request()
        self.assertEqual(r['preview']['operation']['model_id'], 'outage-energy-bounds/v0'); self.assertNotIn('TERMS_TEST', json.dumps(r['preview'])); self.assertNotIn('available_low', json.dumps(r['preview']))
        self.assertTrue(self.c.post('/api/v1/work/requests/' + r['id'] + '/validate', headers=self.H, json={}).json()['valid'])
        # a provider sees the board metadata (two-stage access) but not the requester's audience list or private inputs
        seen = self.c.get('/api/v1/work/requests/' + r['id'], headers=self.pv['alpha']['h']).json()
        self.assertEqual(seen['preview']['payment']['ceiling'], 10); self.assertNotIn('principals', seen['audience'])
        # eligibility check before bidding: gamma cannot deliver (wrong kind, no exact validator)
        el = self.c.post('/api/v1/work/requests/' + r['id'] + '/eligibility', headers=self.pv['gamma']['h'], json={}).json()
        self.assertFalse(el['eligible']); self.assertEqual({x['code'] for x in el['reasons']}, {'unsupported_kind', 'unsupported_exact_validator'})
        bad = self.offer('gamma', r['id'], price=1); self.assertEqual(bad['state'], 'excluded'); self.assertIn('unsupported_kind', [x['code'] for x in bad['eligibility']['reasons']])
        too_expensive = self.offer('beta', r['id'], price=50); self.assertEqual(too_expensive['state'], 'excluded'); self.assertIn('price_above_ceiling', [x['code'] for x in too_expensive['eligibility']['reasons']])
        good = self.offer('alpha', r['id'], price=6); self.assertEqual(good['state'], 'offered'); self.assertTrue(good['eligibility']['eligible'])
        cmp = self.c.get('/api/v1/work/requests/' + r['id'] + '/compare', headers=self.H).json()
        self.assertEqual([e['offer_id'] for e in cmp['eligible']], [good['id']]); self.assertEqual({x['offer_id']: x['reasons'] for x in cmp['excluded']}[bad['id']], ['unsupported_kind', 'unsupported_exact_validator'])
        self.assertEqual(cmp['recommended'], good['id'])
        # awarding the excluded offer explicitly is refused; the recommended one is awarded with an atomic reservation
        self.assertEqual(self.c.post('/api/v1/work/requests/' + r['id'] + '/award', headers=self.H, json={'offer_id': bad['id']}).status_code, 409)
        a = self.c.post('/api/v1/work/requests/' + r['id'] + '/award', headers=self.H, json={}); self.assertEqual(a.status_code, 201, a.text); a = a.json()
        self.assertEqual((a['offer_id'], a['provider_id'], a['ceiling'], a['reserved'], a['state']), (good['id'], self.pv['alpha']['id'], 6, 6, 'executing'))
        self.assertEqual(a['milestones'][0]['dimensions'], {'execution': 'queued', 'science': 'unknown', 'acceptance': 'pending', 'payment': 'reserved'})
        tree = self.c.get('/api/v1/budgets/tree', headers=self.H).json()['tree']
        node = next(n for n in tree['children'] if n['ref_id'] == 'award:' + a['id']); self.assertEqual((node['reserved'], node['ceiling']), (6, 6))
        # the request is awarded; the offer is bound; the provider acknowledges; the worker executes; the milestone is delivered
        self.assertEqual(self.c.get('/api/v1/work/requests/' + r['id'], headers=self.H).json()['state'], 'awarded')
        ack = self.c.post('/api/v1/work/awards/' + a['id'] + '/ack', headers=self.pv['alpha']['h']).json(); self.assertIsNotNone(ack['acknowledged_at'])
        self.assertEqual(self.c.post('/api/v1/work/awards/' + a['id'] + '/ack', headers=self.pv['beta']['h']).status_code, 403)
        self.w.run_once()
        v = self.c.get('/api/v1/work/awards/' + a['id'], headers=self.H).json(); m = v['milestones'][0]
        self.assertEqual((m['state'], m['dimensions']['execution'], m['dimensions']['science'], m['dimensions']['acceptance'], m['dimensions']['payment']), ('delivered', 'completed', 'FEASIBLE', 'pending', 'reserved'))
        self.assertEqual(len(m['evidence_root']), 64); self.assertEqual(m['attempts'][0]['state'], 'completed')
        # provider view of its award carries the payment recipient; a viewer sees the award without recipient or selection
        pv = self.c.get('/api/v1/work/awards/' + a['id'], headers=self.pv['alpha']['h']).json(); self.assertEqual(pv['pay_to'], 'provider:alpha')
        vv = self.c.get('/api/v1/work/awards/' + a['id'], headers=self.inst.h('viewer')).json(); self.assertIsNone(vv['pay_to']); self.assertIsNone(vv['selection'])

    # J3 ----------------------------------------------------------------------------------------------------------------
    def test_equal_prices_follow_the_declared_tie_break_and_manual_choice_needs_a_reason(self):
        t, f, r = self.frozen_request()
        oa = self.offer('alpha', r['id'], price=5); ob = self.offer('beta', r['id'], price=5)
        cmp = self.c.get('/api/v1/work/requests/' + r['id'] + '/compare', headers=self.H).json()
        self.assertEqual(cmp['policy']['tie_break'], ['earliest_offer', 'provider_id']); self.assertEqual([e['offer_id'] for e in cmp['eligible']], [oa['id'], ob['id']])
        self.assertEqual(cmp['side_by_side']['differing_fields'], ['pay_to', 'provider_revision'] if oa['provider_revision'] != ob['provider_revision'] else ['pay_to'])
        # same prices under a provider_id tie-break: deterministic by id
        t2 = self.terms(overrides={'selection': {'policy': 'lowest_eligible_price', 'tie_break': ['provider_id']}})
        self.c.post('/api/v1/work/terms/' + t2['id'] + '/freeze', headers=self.H, json={'inputs': energy_inputs('FEASIBLE')})
        r2 = self.c.post('/api/v1/work/requests', headers=self.H, json={'terms_id': t2['id']}).json(); self.c.post('/api/v1/work/requests/' + r2['id'] + '/open', headers=self.H, json={})
        ob2 = self.offer('beta', r2['id'], price=5); oa2 = self.offer('alpha', r2['id'], price=5)
        cmp2 = self.c.get('/api/v1/work/requests/' + r2['id'] + '/compare', headers=self.H).json()
        self.assertEqual([e['provider_id'] for e in cmp2['eligible']], sorted([self.pv['alpha']['id'], self.pv['beta']['id']]))
        # manual selection of the non-recommended offer needs a recorded reason
        self.assertEqual(self.c.post('/api/v1/work/requests/' + r['id'] + '/award', headers=self.H, json={'offer_id': ob['id']}).status_code, 422)
        a = self.c.post('/api/v1/work/requests/' + r['id'] + '/award', headers=self.H, json={'offer_id': ob['id'], 'reason': 'beta also offers analytical checks'}).json()
        self.assertEqual((a['selection']['manual'], a['selection']['reason'], a['selection']['recommended']), (True, 'beta also offers analytical checks', oa['id']))
        # the losing offer is superseded, not deleted
        self.assertEqual(self.c.get('/api/v1/work/requests/' + r['id'], headers=self.H).json()['offers_count']['total'], 2)
        st = {o['id']: o['state'] for o in self.c.get('/api/v1/work/requests/' + r['id'], headers=self.H).json()['offers']}; self.assertEqual(st[oa['id']], 'superseded')

    # J4 + J5 + stale quote race ---------------------------------------------------------------------------------------------
    def test_retry_returns_same_award_race_yields_one_and_stale_offers_are_refused(self):
        t, f, r = self.frozen_request()
        oa = self.offer('alpha', r['id'], price=4); ob = self.offer('beta', r['id'], price=4)
        results = []
        def go(oid):
            results.append(self.c.post('/api/v1/work/requests/' + r['id'] + '/award', headers=self.H, json={'offer_id': oid, 'reason': 'race'}))
        th = [threading.Thread(target=go, args=(oa['id'],)), threading.Thread(target=go, args=(ob['id'],))]
        [x.start() for x in th]; [x.join() for x in th]
        codes = sorted(x.status_code for x in results); self.assertEqual(codes, [201, 409], [x.text for x in results])
        won = next(x.json() for x in results if x.status_code == 201)
        lost = next(x.json() for x in results if x.status_code == 409); self.assertIn(lost['detail']['code'], ('award_limit_reached', 'offer_not_awardable'))
        # retry after a lost response: same award, no second reservation
        again = self.c.post('/api/v1/work/requests/' + r['id'] + '/award', headers=self.H, json={'offer_id': won['offer_id'], 'reason': 'race'}).json()
        self.assertEqual((again['id'], again.get('replayed')), (won['id'], True))
        tree = self.c.get('/api/v1/budgets/tree', headers=self.H).json()['tree']
        self.assertEqual(sum(1 for n in tree['children'] if n['ref_id'].startswith('award:')), 1); self.assertEqual(tree['reserved'], 4)
        # Idempotency-Key replay through the API layer also returns the recorded response
        k = {'Idempotency-Key': 'award-once-1'}
        first = self.c.post('/api/v1/work/requests/' + r['id'] + '/award', headers=dict(self.H, **k), json={'offer_id': won['offer_id'], 'reason': 'race'})
        second = self.c.post('/api/v1/work/requests/' + r['id'] + '/award', headers=dict(self.H, **k), json={'offer_id': won['offer_id'], 'reason': 'race'})
        self.assertEqual(first.json()['id'], second.json()['id'])
        # stale price expectation and a provider revision after the offer are refused before commit
        t2, f2, r2 = self.frozen_request()
        o2 = self.offer('alpha', r2['id'], price=3)
        self.assertEqual(self.c.post('/api/v1/work/requests/' + r2['id'] + '/award', headers=self.H, json={'offer_id': o2['id'], 'expected_price': 2}).status_code, 409)
        rev = self.c.post('/api/v1/work/providers/' + self.pv['alpha']['id'] + '/revise', headers=self.H, json={'pay_to': 'provider:alpha-new'}).json(); self.assertEqual(rev['revision'], 2)
        refused = self.c.post('/api/v1/work/requests/' + r2['id'] + '/award', headers=self.H, json={'offer_id': o2['id']}); self.assertEqual(refused.status_code, 409); self.assertEqual(refused.json()['detail']['code'], 'provider_revised_since_offer')
        # an offer naming a recipient other than the registered one is excluded (recipient substitution)
        sub = self.offer('beta', r2['id'], price=3, pay_to='attacker:wallet'); self.assertEqual(sub['state'], 'excluded'); self.assertIn('recipient_differs_from_registered', [x['code'] for x in sub['eligibility']['reasons']])
        # expired offer cannot be awarded
        fresh = self.offer('beta', r2['id'], price=3, expires_in_seconds=60)
        self.inst.app.state.services.db  # noqa
        with self.inst.app.state.services.db.tx() as db:
            db.execute('UPDATE work_offers SET expires_at=? WHERE id=?', (1, fresh['id']))
        e = self.c.post('/api/v1/work/requests/' + r2['id'] + '/award', headers=self.H, json={'offer_id': fresh['id']}); self.assertEqual(e.status_code, 409); self.assertEqual(e.json()['detail']['code'], 'offer_not_awardable')
        # nearly exhausted parent budget: ceiling 5 left -> an award needing 6 fails cleanly with nothing reserved
        self.assertEqual(self.c.put('/api/v1/budgets/workspace', headers=self.H, json={'ceiling': 4 + 5}).status_code, 200)
        t3, f3, r3 = self.frozen_request(); o3 = self.offer('alpha', r3['id'], price=6)
        with self.inst.app.state.services.db.tx() as db:
            db.execute('UPDATE work_offers SET provider_revision=2 WHERE id=?', (o3['id'],))
        b = self.c.post('/api/v1/work/requests/' + r3['id'] + '/award', headers=self.H, json={'offer_id': o3['id']}); self.assertEqual(b.json()['code'], 'BUDGET_EXHAUSTED', b.text); self.assertEqual(b.json()['detail']['note'], 'nothing awarded; nothing reserved')
        tree = self.c.get('/api/v1/budgets/tree', headers=self.H).json()['tree']; self.assertEqual(tree['reserved'], 4); self.assertTrue(tree['available'] >= 0)
        self.assertEqual(self.c.get('/api/v1/work/requests/' + r3['id'], headers=self.H).json()['awards'], [])

    # J6: change after freeze needs a new agreement (board side) --------------------------------------------------------------
    def test_terms_change_after_opening_supersedes_the_request_binding(self):
        t, f, r = self.frozen_request()
        o = self.offer('alpha', r['id'], price=5)
        am = self.c.post('/api/v1/work/terms/' + t['id'] + '/amend', headers=self.H, json={'terms': {'payment': dict(f['terms']['payment'], ceiling=20)}}).json(); self.assertTrue(am['requires_new_agreement'])
        fr = self.c.post('/api/v1/work/terms/' + am['id'] + '/freeze', headers=self.H, json={}).json(); self.assertEqual(fr['state'], 'frozen')
        # the old offer bound the superseded digest: it cannot be awarded; a new request on the new revision is needed
        e = self.c.post('/api/v1/work/requests/' + r['id'] + '/award', headers=self.H, json={'offer_id': o['id']}); self.assertEqual(e.status_code, 409); self.assertEqual(e.json()['detail']['code'], 'terms_superseded')
        self.assertEqual(self.c.post('/api/v1/work/requests/' + r['id'] + '/offers', headers=self.pv['beta']['h'], json={'price_amount': 5, 'asset': 'action-units', 'scheme': 'exact', 'window_seconds': 60, 'verification': {'class': 'full_exact'}}).status_code, 409)
        r2 = self.c.post('/api/v1/work/requests', headers=self.H, json={'terms_id': am['id']}).json(); self.assertEqual(r2['terms_digest'], fr['digest'])
        # a provider counteroffer is a proposed revision that only the requester can freeze; it expires
        co = self.c.post('/api/v1/work/terms/' + am['id'] + '/amend', headers=self.pv['alpha']['h'], json={'terms': {'deadlines': dict(fr['terms']['deadlines'], delivery_seconds=7200)}, 'expires_in_seconds': 60}); self.assertEqual(co.status_code, 201, co.text); co = co.json()
        self.assertEqual((co['proposed_by'], co['state']), (self.pv['alpha']['principal_id'], 'draft'))
        self.assertEqual(self.c.post('/api/v1/work/terms/' + co['id'] + '/freeze', headers=self.pv['alpha']['h'], json={}).status_code, 403)
        with self.inst.app.state.services.db.tx() as db:
            db.execute('UPDATE work_terms SET expires_at=1 WHERE id=?', (co['id'],))
        ex = self.c.post('/api/v1/work/terms/' + co['id'] + '/freeze', headers=self.H, json={}); self.assertEqual(ex.json()['code'], 'EXPIRED'); self.assertEqual(ex.json()['detail']['code'], 'proposed_revision_expired')
        self.assertEqual(self.c.get('/api/v1/work/terms/' + co['id'], headers=self.H).json()['state'], 'withdrawn')     # preserved as an audit record

    # J7: multi-milestone gating on ACCEPTANCE (delivery here; decisions in the evidence group) -----------------------------
    def test_multi_milestone_dispatch_gates_on_dependencies(self):
        base = self.terms()['terms']
        ms = [{'key': 'pos', 'deliverables': ['determination'], 'max_payment': 3, 'depends_on': [], 'deadline_seconds': 3600, 'on_failure': 'stop_downstream'},
              {'key': 'neg', 'deliverables': ['determination'], 'max_payment': 3, 'depends_on': [], 'deadline_seconds': 3600, 'on_failure': 'stop_downstream'},
              {'key': 'down', 'deliverables': ['determination'], 'max_payment': 4, 'depends_on': ['pos', 'neg'], 'deadline_seconds': 3600, 'on_failure': 'stop_downstream', 'requires_acceptance_of': {'pos': 'accepted', 'neg': 'accepted_or_valid_negative'}}]
        t = self.c.post('/api/v1/work/terms', headers=self.H, json={'terms': dict(base, milestones=ms)}).json()
        f = self.c.post('/api/v1/work/terms/' + t['id'] + '/freeze', headers=self.H, json={'inputs': energy_inputs('FEASIBLE'), 'milestone_inputs': {'pos': energy_inputs('FEASIBLE'), 'neg': energy_inputs('INFEASIBLE')}}); self.assertEqual(f.status_code, 200, f.text); f = f.json()
        ops = {m['key']: m['operation']['contract_id'] for m in f['terms']['milestones']}
        self.assertEqual(len(set(ops.values())), 3); self.assertEqual(ops['down'], f['contract_id'])
        r = self.c.post('/api/v1/work/requests', headers=self.H, json={'terms_id': t['id']}).json(); self.c.post('/api/v1/work/requests/' + r['id'] + '/open', headers=self.H, json={})
        o = self.offer('alpha', r['id'], price=10)
        a = self.c.post('/api/v1/work/requests/' + r['id'] + '/award', headers=self.H, json={'offer_id': o['id']}).json()
        st = {m['key']: m for m in a['milestones']}
        self.assertEqual((st['pos']['state'], st['neg']['state'], st['down']['state']), ('executing', 'executing', 'pending'))
        self.assertIn('required', st['down']['blocked_reason'])
        self.w.run_once(); self.w.run_once()
        v = self.c.get('/api/v1/work/awards/' + a['id'], headers=self.H).json(); st = {m['key']: m for m in v['milestones']}
        self.assertEqual((st['pos']['dimensions']['science'], st['neg']['dimensions']['science'], st['down']['state']), ('FEASIBLE', 'INFEASIBLE', 'pending'))
        self.assertEqual(sum(m['max_payment'] for m in v['milestones']), 10); self.assertEqual(v['reserved'], 10)


if __name__ == '__main__':
    unittest.main()
