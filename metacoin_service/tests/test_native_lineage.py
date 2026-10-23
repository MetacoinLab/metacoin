"""Incremental cross-domain liquidity (order 2026-10-23 group F; journeys 26–28): pre-export subdivision into disjoint funded
allocations with exact backing conservation, per-allocation claim lineage at the destination, spending one allocation while
returning another unused one, and refusal of returns from spent or mixed provenance."""
import json
import unittest

from metacoin_service.tests.test_native_domains import NativeDomainTests, courier


class NativeLineageTests(NativeDomainTests):
    def test_subdivided_export_disjoint_and_conserved(self):
        A, B = self.A, self.B
        bad = A.c.post('/api/v1/native/allocations/export', headers=self.alice['h'], json={'destination_domain': 'dom-b', 'recipient_account': self.bob['account'], 'amount': 20, 'portions': [10, 5, 4], 'request_id': 'b-bad'})
        self.assertEqual(bad.status_code, 422); self.assertEqual(bad.json()['detail']['code'], 'portions_sum')
        r = A.c.post('/api/v1/native/allocations/export', headers=self.alice['h'], json={'destination_domain': 'dom-b', 'recipient_account': self.bob['account'], 'amount': 20, 'portions': [10, 6, 4], 'request_id': 'b1'}); self.assertEqual(r.status_code, 201, r.text); b = r.json()
        ids = [a['id'] for a in b['allocations']]; self.assertEqual((len(set(ids)), b['sum_check'], [a['state'] for a in b['allocations']], [a['amount'] for a in b['allocations']]), (3, True, ['certified'] * 3, [10, 6, 4]))
        self.assertTrue(all(a['batch']['portion_count'] == 3 for a in b['allocations']))
        au = A.audit(); self.assertEqual((A.bal(self.alice['account']), au['backing_locked_for_exports'], au['total_held'] + au['backing_locked_for_exports']), (30, 20, 50)); self.assertTrue(au['conserved'])
        again = A.c.post('/api/v1/native/allocations/export', headers=self.alice['h'], json={'destination_domain': 'dom-b', 'recipient_account': self.bob['account'], 'amount': 20, 'portions': [10, 6, 4], 'request_id': 'b1'}).json()
        self.assertTrue(again['replayed']); self.assertEqual([a['id'] for a in again['allocations']], ids); self.assertEqual(A.bal(self.alice['account']), 30)
        out = courier.push(A.ep, B.ep, 'dom-b'); self.assertEqual([x['state'] for x in out['results']], ['accepted'] * 3)
        lin = B.c.get('/api/v1/native/accounts/' + self.bob['account'] + '/lineage', headers=B.H).json()
        self.assertEqual(([c['amount'] for c in lin['claims']], lin['returnable_total']), ([10, 6, 4], 20)); self.assertEqual(B.bal(self.bob['account'], asset=A.asset), 20)
        acc = A.c.get('/api/v1/native/accounting', headers=A.H).json(); self.assertEqual(acc['source_backing_locked'], 20); self.assertTrue(acc['checks']['backing_matches_journal'])
        self.assertEqual(sorted(a['exposed_state'] for a in A.c.get('/api/v1/native/allocations', headers=A.H).json()['items']), ['reconciled'] * 3)

    def test_spend_one_allocation_return_another_refuse_spent_or_mixed(self):
        A, B = self.A, self.B
        b = A.c.post('/api/v1/native/allocations/export', headers=self.alice['h'], json={'destination_domain': 'dom-b', 'recipient_account': self.bob['account'], 'amount': 15, 'portions': [10, 5], 'request_id': 'b2'}).json(); a10, a5 = [x['id'] for x in b['allocations']]
        courier.push(A.ep, B.ep, 'dom-b')
        # bob spends 7: unlineaged units first (none), then the oldest claim (10): it becomes partially consumed, the 5 stays returnable
        t = B.c.post('/api/v1/native/transfers', headers=self.bob['h'], json={'to_account': self.carol['account'], 'amount': 7, 'asset': A.asset, 'request_id': 's7'}); self.assertEqual(t.status_code, 201, t.text)
        lin = {c['allocation_id']: c for c in B.c.get('/api/v1/native/accounts/' + self.bob['account'] + '/lineage', headers=B.H).json()['claims']}
        self.assertEqual((lin[a10]['state'], lin[a10]['consumed'], lin[a10]['returnable'], lin[a5]['state'], lin[a5]['returnable']), ('partially_consumed', 7, 0, 'active', 5))
        self.assertEqual((B.alloc(a10)['exposed_state'], B.alloc(a5)['exposed_state']), ('partially_consumed', 'returnable'))
        # journey 28: the spent/mixed allocation cannot be returned, not even for its untouched remainder
        r = B.c.post('/api/v1/native/allocations/' + a10 + '/return', headers=self.bob['h'], json={}); self.assertEqual(r.status_code, 409); self.assertEqual(r.json()['detail']['code'], 'return_partial_unsupported')
        # carol received 7 without lineage: she cannot return anything (no allocation of hers); her units are ordinary local funds
        self.assertEqual(B.c.get('/api/v1/native/accounts/' + self.carol['account'] + '/lineage', headers=B.H).json()['returnable_total'], 0)
        # journey 27: the unused 5 returns on its own: destination retirement first, then the source releases once
        r = B.c.post('/api/v1/native/allocations/' + a5 + '/return', headers=self.bob['h'], json={}); self.assertEqual(r.status_code, 200, r.text); self.assertEqual(r.json()['exposed_state'], 'retirement_pending')
        self.assertEqual((B.bal(self.bob['account'], asset=A.asset), B.bal(self.bob['account'], 'in_transit', asset=A.asset)), (3, 5))
        before = A.bal(self.alice['account']); out = courier.push(B.ep, A.ep, 'dom-a'); self.assertEqual(out['results'][0]['state'], 'accepted')
        self.assertEqual((A.bal(self.alice['account']), A.alloc(a5)['state'], B.alloc(a5)['exposed_state']), (before + 5, 'returned', 'reconciled'))
        # a replayed return request releases nothing again
        ob = B.c.get('/api/v1/native/transport/outbox', headers=B.H, params={'receiver': 'dom-a', 'all': '1'}).json(); dup = A.c.post('/api/v1/native/transport/inbox', headers=A.H, json=ob).json()
        self.assertTrue(all(x['state'] == 'duplicate' for x in dup['results'])); self.assertEqual(A.bal(self.alice['account']), before + 5)
        acc = B.c.get('/api/v1/native/accounting', headers=B.H, params={'asset': A.asset}).json(); self.assertEqual((acc['claims_consumed'], acc['claims_retired'], acc['returns_completed']), (7, 5, 5))
        self.assertEqual(A.audit()['total_held'] + B.audit(A.asset)['total_held'], 50)
        # mixed provenance: bob now holds 3 lineaged (from a10) ; a local receipt from carol adds unlineaged units; a new spend consumes the unlineaged first
        B.c.post('/api/v1/native/transfers', headers=self.carol['h'], json={'to_account': self.bob['account'], 'amount': 2, 'asset': A.asset, 'request_id': 'c2'})
        B.c.post('/api/v1/native/transfers', headers=self.bob['h'], json={'to_account': self.carol['account'], 'amount': 2, 'asset': A.asset, 'request_id': 's2'})
        lin = {c['allocation_id']: c for c in B.c.get('/api/v1/native/accounts/' + self.bob['account'] + '/lineage', headers=B.H).json()['claims']}; self.assertEqual(lin[a10]['consumed'], 7)


if __name__ == '__main__':
    unittest.main()
