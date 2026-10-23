"""Bounded planner (group E): improved planner matches the exhaustive oracle on bounded fixtures for both objectives; the baseline
is feasible-or-honest; witnesses explain infeasibility; competing missions never overcommit shared offers; timeouts expose status."""
import random
import unittest

from metacoin_service.errors import ServiceError
from metacoin_service.native import planner as PL


def fixture(seed, n_tasks=5, n_offers=8, missions=1):
    r = random.Random(seed); kinds = ['compute', 'storage', 'contact']; tasks = []
    for i in range(n_tasks):
        deps = [tasks[j]['id'] for j in range(i) if r.random() < 0.4]
        tasks.append({'id': 't%d' % i, 'kind': r.choice(kinds), 'quantity': r.randint(1, 8), 'duration_slots': r.randint(1, 3), 'depends': deps, 'mission': 'm%d' % (i % missions), 'deadline_slot': None if r.random() < 0.7 else r.randint(4, 10)})
    offers = []
    for i in range(n_offers):
        k = kinds[i % 3] if i < 3 else r.choice(kinds)
        ws = r.randint(0, 3) * 60; offers.append({'id': 'o%d' % i, 'kind': k, 'available': r.randint(6, 20), 'price_per_unit': r.randint(1, 5), 'energy_mwh_per_unit': r.randint(0, 2), 'window_start': ws, 'window_end': ws + r.randint(3, 7) * 60})
    return {'horizon': {'start': 0, 'end': 600, 'slot_seconds': 60}, 'tasks': tasks, 'offers': offers, 'budget': r.randint(40, 160), 'energy_budget_mwh': r.randint(30, 90), 'energy_reserve_mwh': 5}


class PlannerTests(unittest.TestCase):
    def test_improved_matches_oracle_on_bounded_fixtures(self):
        agree = infeasible = baseline_worse = 0
        for seed in range(200):
            for obj in PL.OBJECTIVES:
                p = dict(fixture(seed, missions=2 if seed % 3 == 0 else 1), objective=obj)
                o, i, b = PL.solve(p, 'oracle'), PL.solve(p, 'improved'), PL.solve(p, 'baseline')
                if o['status'] == 'infeasible':
                    infeasible += 1; self.assertEqual(i['status'], 'infeasible', (seed, obj, i)); self.assertTrue(o['witness'] and i['witness']); self.assertIn(b['status'], ('infeasible', 'unknown'))
                else:
                    self.assertEqual(i['status'], 'optimal', (seed, obj, i)); self.assertEqual(i['incumbent']['objective_value'], o['incumbent']['objective_value'], (seed, obj)); agree += 1
                    ok, ev = PL.evaluate(PL.normalize(p), i['incumbent']['choice']); self.assertTrue(ok, ev)   # independent re-evaluation of the returned schedule
                    if b['status'] == 'feasible':
                        self.assertGreaterEqual(b['incumbent']['objective_value'], o['incumbent']['objective_value'])
                        baseline_worse += b['incumbent']['objective_value'] > o['incumbent']['objective_value']
                    else:
                        baseline_worse += 1
        self.assertGreater(agree, 150); self.assertGreater(infeasible, 20); self.assertGreater(baseline_worse, 5)
        self.stats = {'agree': agree, 'infeasible': infeasible, 'baseline_worse': baseline_worse}

    def test_witnesses_name_the_unsatisfied_constraint(self):
        base = {'horizon': {'start': 0, 'end': 600, 'slot_seconds': 60}, 'tasks': [{'id': 'a', 'kind': 'compute', 'quantity': 5, 'duration_slots': 2}, {'id': 'b', 'kind': 'contact', 'quantity': 4, 'duration_slots': 1, 'depends': ['a']}],
                'offers': [{'id': 'c1', 'kind': 'compute', 'available': 10, 'price_per_unit': 2, 'window_start': 0, 'window_end': 300}, {'id': 'k1', 'kind': 'contact', 'available': 10, 'price_per_unit': 3, 'window_start': 180, 'window_end': 420}]}
        ok = PL.solve(dict(base, objective='earliest_delivery'), 'improved'); self.assertEqual(ok['status'], 'optimal'); self.assertEqual(ok['incumbent']['makespan_slots'], 4); self.assertEqual(ok['incumbent']['cost'], 22)
        # reserve/deadline conflict (journey 25): budget after reserve below the cheapest feasible cost
        r = PL.solve(dict(base, objective='lowest_cost', budget=20), 'improved'); self.assertEqual((r['status'], r['witness']['constraint'], r['witness']['minimum_cost']), ('infeasible', 'budget', 22))
        r = PL.solve(dict(base, objective='lowest_cost', deadline_slot=3), 'improved'); self.assertEqual((r['status'], r['witness']['constraint']), ('infeasible', 'deadline'))
        r = PL.solve(dict(base, objective='lowest_cost', energy_budget_mwh=10, energy_reserve_mwh=8, offers=[dict(base['offers'][0], energy_mwh_per_unit=1), base['offers'][1]]), 'improved'); self.assertEqual((r['status'], r['witness']['constraint']), ('infeasible', 'energy'))
        r = PL.solve(dict(base, offers=[base['offers'][0]]), 'improved'); self.assertEqual((r['status'], r['witness']['constraint']), ('infeasible', 'no_offer_of_kind'))
        r = PL.solve(dict(base, offers=[base['offers'][0], dict(base['offers'][1], available=2)]), 'improved'); self.assertEqual((r['status'], r['witness']['constraint']), ('infeasible', 'capacity'))
        # joint conflict: two tasks need 6 units each from one 10-unit offer, every relaxation passes on its own
        r = PL.solve({'horizon': {'start': 0, 'end': 600, 'slot_seconds': 60}, 'tasks': [{'id': 'a', 'kind': 'compute', 'quantity': 6}, {'id': 'b', 'kind': 'compute', 'quantity': 6}], 'offers': [{'id': 'c1', 'kind': 'compute', 'available': 10, 'price_per_unit': 1}]}, 'oracle')
        self.assertEqual((r['status'], r['witness']['constraint']), ('infeasible', 'capacity_or_precedence_conflict'))

    def test_competing_missions_share_capacity_without_overcommit(self):
        p = {'horizon': {'start': 0, 'end': 600, 'slot_seconds': 60}, 'objective': 'lowest_cost',
             'tasks': [{'id': 'x', 'kind': 'contact', 'quantity': 6, 'mission': 'A'}, {'id': 'y', 'kind': 'contact', 'quantity': 6, 'mission': 'B'}],
             'offers': [{'id': 'cheap', 'kind': 'contact', 'available': 8, 'price_per_unit': 1}, {'id': 'dear', 'kind': 'contact', 'available': 8, 'price_per_unit': 4}], 'budgets': {'A': 50, 'B': 50}}
        r = PL.solve(p, 'improved'); self.assertEqual(r['status'], 'optimal'); ch = r['incumbent']['choice']; self.assertEqual(sorted(ch.values()), ['cheap', 'dear']); self.assertEqual(r['incumbent']['cost'], 30)
        o = PL.solve(p, 'oracle'); self.assertEqual(o['incumbent']['cost'], 30)
        ok, ev = PL.evaluate(PL.normalize(p), {'x': 'cheap', 'y': 'cheap'}); self.assertFalse(ok); self.assertEqual(ev['constraint'], 'capacity')
        self.assertFalse(r['binding'])

    def test_timeout_exposes_incumbent_bound_and_status(self):
        r = random.Random(1); tasks = [{'id': 't%d' % i, 'kind': 'compute', 'quantity': r.randint(1, 3), 'duration_slots': 1, 'depends': []} for i in range(14)]
        offers = [{'id': 'o%d' % i, 'kind': 'compute', 'available': 30, 'price_per_unit': r.randint(1, 9), 'window_start': 0, 'window_end': 600} for i in range(8)]
        p = {'horizon': {'start': 0, 'end': 600, 'slot_seconds': 60}, 'tasks': tasks, 'offers': offers, 'objective': 'lowest_cost'}
        with self.assertRaises(ServiceError):
            PL.solve(p, 'oracle')
        # 8^14 leaves without a bound would never finish; the bound makes the cheapest-first search exhaust almost immediately, but a 1 ms timeout still exposes an honest status
        fast = PL.solve(p, 'improved', timeout_ms=5000); self.assertEqual(fast['status'], 'optimal')
        slow = PL.solve(dict(p, objective='earliest_delivery', tasks=[dict(t, depends=['t%d' % (i - 1)] if i else []) for i, t in enumerate(tasks)]), 'improved', timeout_ms=1)
        self.assertIn(slow['status'], ('optimal', 'feasible_timeout', 'unknown_timeout')); self.assertIn('bound', slow)
        if slow['status'] == 'feasible_timeout':
            self.assertTrue(slow['incumbent']); self.assertIn('not established', slow['note'])


if __name__ == '__main__':
    unittest.main()
