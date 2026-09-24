"""Hierarchical budgets: workspace -> run -> node ceilings, atomic reservations under concurrent workers, explained refusals."""
import threading
import unittest
from metacoin_service.tests.test_service import Instance, own_inputs
from metacoin_service import workflows


def two_node_definition(name, budgets=None):
    nodes = [{'id': 'a', 'type': 'energy_audit', 'inputs': own_inputs('A_' + name)}, {'id': 'b', 'type': 'energy_audit', 'inputs': own_inputs('B_' + name)}]
    for n in nodes:
        if budgets and n['id'] in budgets:
            n['budget'] = budgets[n['id']]
    return {'schema': workflows.SCHEMA, 'name': name, 'nodes': nodes, 'outputs': ['a', 'b']}


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.inst = Instance(); self.addCleanup(self.inst.close); self.c = self.inst.client; self.H = self.inst.h('owner')
        self.assertEqual(self.c.put('/api/v1/budgets/workspace', headers=self.H, json={'ceiling': 3}).json()['ceiling'], 3)

    def root(self):
        return self.c.get('/api/v1/budgets/tree', headers=self.H).json()['tree']

    def start(self, definition, ceiling=None):
        wid = self.c.post('/api/v1/workflows', headers=self.H, json={'definition': definition}).json()['id']
        body = {} if ceiling is None else {'budget_ceiling': ceiling}
        r = self.c.post('/api/v1/workflows/' + wid + '/runs', headers=self.H, json=body)
        return r

    def advance(self, rid):
        self.c.post('/api/v1/runs/' + rid + '/advance', headers=self.H)
        return self.c.get('/api/v1/runs/' + rid, headers=self.H).json()

    def test_two_runs_share_the_workspace_ceiling_under_concurrent_workers(self):
        ra = self.start(two_node_definition('alpha'), ceiling=2).json()['run_id']
        rb = self.start(two_node_definition('beta'), ceiling=2).json()['run_id']
        # dispatch both runs concurrently: reservations are atomic, so the workspace ceiling (3) admits exactly three nodes
        threads = [threading.Thread(target=self.advance, args=(r,)) for r in (ra, rb)]
        for t in threads: t.start()
        for t in threads: t.join()
        root = self.root()
        self.assertEqual((root['ceiling'], root['reserved'], root['committed'], root['available']), (3, 3, 0, 0))
        va, vb = self.advance(ra), self.advance(rb)
        states = sorted(n['state'] for n in va['nodes'] + vb['nodes'])
        self.assertEqual(states, ['queued', 'queued', 'queued', 'waiting_dependency'])
        waiting = [n for n in va['nodes'] + vb['nodes'] if n['state'] == 'waiting_dependency'][0]
        self.assertIn('reserved by in-flight work at the workspace level', waiting['blocked_reason'])
        self.assertEqual(self.c.post('/api/v1/budgets/preview', headers=self.H, json={'parent_run_id': None, 'amounts': [1]}).json()['decisions'][0]['refused_at']['level'], 'workspace')
        # several workers complete the jobs concurrently; committed amounts never exceed the ceiling
        workers = [threading.Thread(target=lambda: self.inst.worker().run_once()) for _ in range(3)]
        for t in workers: t.start()
        for t in workers: t.join()
        self.inst.worker().run_once()
        va, vb = self.advance(ra), self.advance(rb)
        root = self.root()
        self.assertEqual(root['reserved'] + root['committed'], 3)
        self.assertLessEqual(root['committed'], 3)
        # the fourth node can never fit: committed units do not return, so it is blocked with an explanation rather than waiting forever
        for _ in range(3):
            va, vb = self.advance(ra), self.advance(rb)
        blocked = [n for n in va['nodes'] + vb['nodes'] if n['state'] == 'blocked']
        self.assertEqual(len(blocked), 1)
        self.assertIn('can never fit', blocked[0]['blocked_reason'])
        self.assertEqual(sorted(v['state'] for v in (va, vb)), ['blocked', 'completed'])
        self.assertEqual((self.root()['reserved'], self.root()['committed']), (0, 3))
        done = va if va['state'] == 'completed' else vb
        self.assertEqual((done['budget']['run_ceiling'], done['budget']['committed_total'], done['budget']['reserved_total']), (2, 2, 0))

    def test_run_and_node_ceilings_and_release_on_cancel(self):
        # a run ceiling above the workspace ceiling is refused at start
        r = self.start(two_node_definition('gamma'), ceiling=5)
        self.assertEqual((r.status_code, r.json()['detail']['code']), (422, 'ceiling_exceeds_parent'))
        # a node ceiling of zero blocks that node immediately and permanently, the other proceeds
        rid = self.start(two_node_definition('delta', budgets={'b': 0}), ceiling=2).json()['run_id']
        v = self.advance(rid)
        by = {n['node_id']: n for n in v['nodes']}
        self.assertEqual((by['a']['state'], by['b']['state']), ('queued', 'blocked'))
        self.assertIn('workflow_node ceiling 0', by['b']['blocked_reason'])
        self.assertEqual(self.root()['reserved'], 1)
        # cancelling the run releases the reservation of the queued node
        self.assertEqual(self.c.post('/api/v1/runs/' + rid + '/cancel', headers=self.H).status_code, 200)
        self.inst.worker().run_once()
        v = self.advance(rid)
        self.assertEqual(v['state'], 'cancelled')
        self.assertEqual((self.root()['reserved'], self.root()['committed']), (0, 0))
        self.assertEqual([r['state'] for r in v['budget']['reservations']], ['released'])
        # invalid node budgets are named at validation
        bad = two_node_definition('eps'); bad['nodes'][0]['budget'] = -1
        r = self.c.post('/api/v1/workflows', headers=self.H, json={'definition': bad})
        self.assertEqual((r.status_code, r.json()['detail']['errors'][0]['code']), (422, 'budget_type'))
        # the workspace ceiling cannot be lowered below current use, and viewers cannot set it
        rid2 = self.start(two_node_definition('zeta'), ceiling=2).json()['run_id']; self.advance(rid2)
        self.assertEqual(self.c.put('/api/v1/budgets/workspace', headers=self.H, json={'ceiling': 1}).json()['detail']['code'], 'ceiling_below_use')
        self.assertEqual(self.c.put('/api/v1/budgets/workspace', headers=self.inst.h('viewer'), json={'ceiling': 9}).status_code, 403)
        self.assertEqual(self.c.get('/api/v1/budgets/tree', headers=self.inst.h('viewer')).json()['tree']['reserved'], 2)


if __name__ == '__main__':
    unittest.main()
