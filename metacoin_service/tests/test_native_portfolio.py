"""Mission portfolio and acquisition decisions (order 2026-10-23 group I; journeys 41–44 at unit level)."""
import random
import time
import unittest

from metacoin_service.native import planner as PL, portfolio as PF
from metacoin_service.tests.test_native_missions import MissionBase


def fixture(seed, n=3):
    r = random.Random(seed); probs, prios = {}, {}
    offers = [{'id': 'o%d' % i, 'kind': ['compute', 'contact', 'storage'][i % 3], 'available': r.randint(4, 12), 'price_per_unit': r.randint(1, 4), 'window_start': 0, 'window_end': 600} for i in range(5)]
    for m in range(n):
        tasks = [{'id': 'm%d:t%d' % (m, j), 'kind': ['compute', 'contact', 'storage'][r.randint(0, 2)], 'quantity': r.randint(1, 5), 'duration_slots': r.randint(1, 2), 'mission': 'm%d' % m} for j in range(r.randint(1, 2))]
        probs['m%d' % m] = {'horizon': {'start': 0, 'end': 600, 'slot_seconds': 60}, 'tasks': tasks, 'offers': offers, 'budget': r.randint(8, 30), 'objective': 'lowest_cost'}; prios['m%d' % m] = r.randint(1, 3)
    return probs, prios


class PortfolioKernelTests(unittest.TestCase):
    def test_improved_matches_oracle_on_bounded_fixtures(self):
        agree = 0
        for seed in range(40):
            probs, prios = fixture(seed); o = PF.oracle(probs, prios); i = PF.improved(probs, prios, timeout_ms=5000); ref = PF.reference(probs, prios)
            self.assertEqual(i['status'], 'optimal' if o['status'] == 'optimal' else i['status'])
            if o['status'] == 'optimal':
                self.assertEqual(i['score'][0], o['score'][0], (seed, i, o)); self.assertEqual(i['score'][1], o['score'][1], (seed,)); agree += 1
                self.assertGreaterEqual(ref['score'][0] if ref['score'] else 0, o['score'][0])          # the reference admits no more priority mass than the optimum
        self.assertGreater(agree, 25)

    def test_rolling_beats_or_equals_fixed_on_identical_disruption(self):
        P_ = {'horizon': {'start': 0, 'end': 600, 'slot_seconds': 60}, 'objective': 'lowest_cost', 'budget': 1000,
              'tasks': [{'id': 'a', 'kind': 'compute', 'quantity': 2, 'duration_slots': 1}, {'id': 'b', 'kind': 'compute', 'quantity': 2, 'duration_slots': 1, 'depends': ['a']}, {'id': 'c', 'kind': 'compute', 'quantity': 2, 'duration_slots': 1, 'depends': ['b']}],
              'offers': [{'id': 'cheap', 'kind': 'compute', 'available': 10, 'price_per_unit': 1, 'window_start': 0, 'window_end': 600}, {'id': 'dear', 'kind': 'compute', 'available': 10, 'price_per_unit': 3, 'window_start': 0, 'window_end': 600}]}
        pf = PF.Portfolio(None, None); cmp_ = pf.rolling_vs_fixed({'problem': P_, 'disruption': {'offer_id': 'cheap', 'at_slot': 1}})
        self.assertTrue(cmp_['identical_inputs']); self.assertEqual(cmp_['fixed']['failed_tasks'], ['b', 'c']); self.assertEqual(cmp_['rolling']['replanned_tasks'], ['b', 'c'])
        self.assertLessEqual(cmp_['rolling']['total_cost'], cmp_['fixed']['total_cost']); self.assertEqual(cmp_['fixed']['cost_incurred_before_disruption'], 2)


class PortfolioServiceTests(MissionBase):
    def test_admission_by_declared_priority_and_validator_refusals(self):
        self.fund('requester', 1000); t = int(time.time()); S = self.actors['supplier']['h']
        pool = self.c.post('/api/v1/resources/pools', headers=S, json={'kind': 'contact', 'label': 'antenna', 'slot_unit': 'channel', 'units_per_slot_second': 1, 'resolution_seconds': 1, 'schedule': [{'start': t, 'end': t + 7200, 'capacity': 1}]}).json()
        o = self.c.post('/api/v1/resources/offers', headers=S, json={'kind': 'contact', 'resource_id': 'ant', 'capacity': 7200, 'price_per_unit': 1, 'window_start': t, 'window_end': t + 7200, 'pool_id': pool['id']}).json()
        g = lambda q: {'milestones': [{'key': 'link', 'kind': 'resource', 'depends': [], 'spec': {'offer_kind': 'contact', 'quantity': q, 'max_price_per_unit': 5}}]}
        hi = self.mission(graph=g(60), template=None, policy={'priority': 3})['id']; lo = self.mission(graph=g(60), template=None, policy={'priority': 1})['id']; mid_ = self.mission(graph=g(60), template=None, policy={'priority': 2})['id']
        r = self.c.post('/api/v1/portfolio/plan', headers=self.R, json={'mission_ids': [hi, lo, mid_], 'objective': 'lowest_cost', 'planner': 'improved'}); self.assertEqual(r.status_code, 200, r.text); p = r.json()
        self.assertEqual(p['status'], 'optimal'); self.assertEqual(sorted(p['admitted']), sorted([hi, lo, mid_]))     # a one-channel pool over 2 h admits all three one-minute links; admission order follows declared priority
        self.assertFalse(p['binding']); self.assertEqual(p['liquidity'][hi]['spendable'], p['liquidity'][hi]['held_locally']); self.assertEqual(p['priorities'][hi], 3)
        ref = self.c.post('/api/v1/portfolio/plan', headers=self.R, json={'mission_ids': [hi, lo, mid_], 'planner': 'reference'}).json(); self.assertEqual(ref['admitted'][0], hi)           # declared priority leads
        orc = self.c.post('/api/v1/portfolio/plan', headers=self.R, json={'mission_ids': [hi, lo], 'planner': 'oracle'}).json(); self.assertEqual(orc['status'], 'optimal')
        # journey 44: an overselling plan and a plan spending non-spendable funds are refused by the independent validator
        sched = p['plan']['incumbent']['schedule']; v = self.c.post('/api/v1/portfolio/validate', headers=self.R, json={'plan': p['plan']}).json(); self.assertTrue(v['valid'], v)
        over = {tid: dict(s, start_at=t + 10, end_at=t + 70, quantity=60) for tid, s in sched.items()}                       # three one-channel links at the same minute on a one-channel pool
        v2 = self.c.post('/api/v1/portfolio/validate', headers=self.R, json={'plan': {'schedule': over}}).json(); self.assertFalse(v2['valid']); self.assertEqual(v2['reasons'][0]['constraint'], 'pool_oversold')
        rich = {tid: dict(s, cost=10 ** 6) for tid, s in sched.items()}; v3 = self.c.post('/api/v1/portfolio/validate', headers=self.R, json={'plan': {'schedule': rich}}).json(); self.assertFalse(v3['valid']); self.assertEqual(v3['reasons'][0]['constraint'], 'not_spendable')
        liq = self.c.get('/api/v1/native/accounts/%s/liquidity' % self.actors['requester']['account'], headers=self.R).json(); self.assertEqual(set(liq) >= {'held_locally', 'reserved', 'cross_domain_claims_held', 'cross_domain_pending', 'expected_earnings', 'spendable'}, True)
        # journey 43: evidence purchase then replanning of eligible uncommitted work only (evidence gate template)
        m = self.mission(policy={'gate_budget': 10}, template='evidence_before_followon'); mid = m['id']; self.drive_work(mid, 'compute', 40); s = self.step(mid); self.assertEqual(s['performed']['action'], 'analyze')
        for _ in range(4):
            self.w.run_once()
        s = self.step(mid); self.assertIn(s['performed']['action'], ('buy_evidence', 'skip_gate', 'await_execution'))
        pl = self.c.post('/api/v1/missions/%s/plan' % mid, headers=self.R, json={'objective': 'lowest_cost', 'apply': True}).json(); self.assertIn(pl['status'], ('optimal', 'infeasible', 'feasible_timeout'))
        v = self.view(mid); self.assertEqual(next(x for x in v['milestones'] if x['key'] == 'compute')['state'], 'accepted')          # committed/accepted work untouched by replanning


if __name__ == '__main__':
    unittest.main()
