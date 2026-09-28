# EXPERIMENTAL — private test chain only — not the protocol's money layer, which remains [SPEC] and zero-value by MIP-0001/0002; chain-agnostic law unchanged.
"""§49: verification-gated delivery on the existing x402 upto path. A single-operation optimization package (resource_plan)
paid by a metered authorization: settlement of the measured amount is withheld until the package's required verification
passes; a deliberately invalid candidate (fault-injected verification failure on a disposable instance) is unaccepted, its
authorization stays unused (nothing moves on the private chain), and a retry under the SAME authorization delivers and settles
once. The application journal and the local chain state are compared."""
import json
import unittest
from metacoin_service.tests.test_upto_route import UptoInstance, upto_exchange, HAVE_CHAIN
from metacoin_service.tests.test_compute_engine import HAVE_RUNTIME
from metacoin_service.tests.test_resource_plan_service import sample
from metacoin_service.tests.test_packages import plan_definition


@unittest.skipUnless(HAVE_RUNTIME and HAVE_CHAIN, 'needs the compute interpreter and the built local-chain artifacts')
class PackageUptoGatingTests(unittest.TestCase):
    def setUp(self):
        self.inst = UptoInstance(); self.addCleanup(self.inst.close)
        self.inst.settings.limits['test_hooks'] = True; self.inst.reopen()          # disposable instance with fault injection enabled explicitly
        self.c = self.inst.client; self.H = self.inst.h('owner'); self.w = self.inst.worker(); self.addCleanup(self.w.offline)
        self.sid = next(s['id'] for s in self.c.get('/api/v1/services', headers=self.H).json()['items'] if s['kind'] == 'resource_plan')
        wid = self.c.post('/api/v1/workflows', headers=self.H, json={'definition': plan_definition()}).json()['id']
        self.pk = self.c.post('/api/v1/packages', headers=self.H, json={'name': 'metered-plan', 'workflow_id': wid, 'delivery_policy': {'gate': 'required_verification', 'required_class': 'full_reference', 'metered_failure_charge': 'none'}}).json()

    def quote(self, inputs):
        q = self.c.post('/api/v1/services/' + self.sid + '/quote', headers=self.H, json={'inputs': inputs, 'scheme': 'upto'}); self.assertEqual(q.status_code, 201, q.text); q = q.json()
        self.assertEqual(self.c.post('/api/v1/quotes/' + q['quote_id'] + '/accept', headers=self.H).status_code, 200)
        return q

    def settle(self, pid):
        return self.c.get('/api/v1/x402/settlements/' + pid, headers=self.H).json()

    def run_view(self, rid):
        return self.c.get('/api/v1/packages/runs/' + rid, headers=self.H).json()

    def test_gated_settlement_invalid_candidate_and_retry_under_same_authorization(self):
        inputs = dict(sample(), private_label='PKG_UPTO_OK')
        q = self.quote(inputs)
        ex = upto_exchange(self.inst, self.sid, q['quote_id'], inputs, 'pkg-upto-ok-' + 'a' * 20)
        self.assertEqual(ex['second_status'], 202, ex); jid, pid = ex['body']['job_id'], ex['body']['settlement']['payment_id']
        chain = ex['chain']; before = chain.balances()
        pr = self.c.post('/api/v1/packages/' + self.pk['id'] + '/bind-job', headers=self.H, json={'job_id': jid}); self.assertEqual(pr.status_code, 201, pr.text); rid = pr.json()['id']
        self.assertEqual(self.w.run_once(), (jid, 'succeeded'))
        # computed but not delivered: the measured amount is NOT settled while verification is pending
        st = self.settle(pid)
        self.assertEqual(st['state'], 'AUTHORIZED'); self.assertEqual(st['delivery_gate']['state'], 'awaiting_verification'); self.assertIn('withheld', st['note']); self.assertEqual(chain.balances(), before)
        v = self.run_view(rid); self.assertEqual(v['state'], 'awaiting_verification'); self.assertEqual(v['delivery'].get('withheld'), None)
        self.assertEqual(self.w.run_once()[1], 'succeeded')                     # the full_reference audit requested by the gate
        v = self.run_view(rid); self.assertEqual(v['state'], 'delivered', v)
        st = self.settle(pid)
        self.assertEqual(st['state'], 'SETTLED', st); after = chain.balances()
        self.assertEqual(before['payer'] - after['payer'], int(st['final_amount'])); self.assertEqual(after['recipient'] - before['recipient'], int(st['final_amount']))
        self.assertLessEqual(int(st['final_amount']), int(st['authorized_max']))
        usage = [u for u in self.c.get('/api/v1/usage', headers=self.H).json()['items'] if u['job_id'] == jid][0]
        self.assertEqual(usage['assessed_charge'], int(st['final_amount']))          # journal and chain agree
        bundle = self.c.post('/api/v1/packages/runs/' + rid + '/bundle', headers=self.H, json={}); self.assertEqual(bundle.status_code, 200, bundle.text)
        self.assertEqual(bundle.json()['manifest']['delivery_state'], 'delivered')
        # a deliberately invalid candidate: verification forced to fail (fault hook, this disposable instance only)
        inputs2 = dict(sample(), private_label='PKG_UPTO_BAD')
        q2 = self.quote(inputs2)
        ex2 = upto_exchange(self.inst, self.sid, q2['quote_id'], inputs2, 'pkg-upto-bad-' + 'b' * 20)
        self.assertEqual(ex2['second_status'], 202, ex2); jid2, pid2 = ex2['body']['job_id'], ex2['body']['settlement']['payment_id']
        rid2 = self.c.post('/api/v1/packages/' + self.pk['id'] + '/bind-job', headers=self.H, json={'job_id': jid2}).json()['id']
        self.assertEqual(self.c.post('/api/v1/ops/faults', headers=self.inst.h('viewer'), json={'fault': 'verification_fail', 'job_id': jid2}).status_code, 403)
        self.assertEqual(self.c.post('/api/v1/ops/faults', headers=self.H, json={'fault': 'verification_fail', 'job_id': jid2}).status_code, 200)
        self.assertEqual(self.w.run_once(), (jid2, 'succeeded'))
        self.assertEqual(self.settle(pid2)['state'], 'AUTHORIZED')
        self.assertEqual(self.w.run_once()[1], 'succeeded')                     # audit job runs; its outcome is a forced failure
        v2 = self.run_view(rid2)
        self.assertEqual(v2['state'], 'unaccepted', v2); self.assertIn('FAULT INJECTED', json.dumps(self.c.get('/api/v1/verification/' + list(v2['verification'].values())[0]['verification_id'], headers=self.H).json()))
        self.assertEqual(v2['delivery']['charge_policy'], 'none'); self.assertIn('kept privately', v2['delivery']['evidence'])
        st2 = self.settle(pid2)
        self.assertEqual(st2['state'], 'AUTHORIZATION_UNUSED', st2); self.assertIn('verification did not pass', st2['error']); self.assertEqual(chain.balances(), after)     # nothing moved
        self.assertEqual(self.c.post('/api/v1/packages/runs/' + rid2 + '/bundle', headers=self.H, json={}).json()['detail']['code'], 'not_delivered')
        # the failed attempt is preserved as evidence; a retry needs an AUTHORIZED authorization: this one was already released as unused
        r = self.c.post('/api/v1/packages/runs/' + rid2 + '/retry', headers=self.H)
        self.assertEqual((r.status_code, r.json()['detail']['code']), (409, 'authorization_not_reusable'))
        # retry before settlement is consulted: a third run fails verification, is retried under the SAME authorization, then delivers and settles exactly once
        inputs3 = dict(sample(), private_label='PKG_UPTO_RETRY')
        q3 = self.quote(inputs3)
        ex3 = upto_exchange(self.inst, self.sid, q3['quote_id'], inputs3, 'pkg-upto-retry-' + 'c' * 18)
        jid3, pid3 = ex3['body']['job_id'], ex3['body']['settlement']['payment_id']
        rid3 = self.c.post('/api/v1/packages/' + self.pk['id'] + '/bind-job', headers=self.H, json={'job_id': jid3}).json()['id']
        self.c.post('/api/v1/ops/faults', headers=self.H, json={'fault': 'verification_fail', 'job_id': jid3})
        self.assertEqual(self.w.run_once(), (jid3, 'succeeded')); self.assertEqual(self.run_view(rid3)['state'], 'awaiting_verification'); self.assertEqual(self.w.run_once()[1], 'succeeded')
        self.assertEqual(self.run_view(rid3)['state'], 'unaccepted')
        rt = self.c.post('/api/v1/packages/runs/' + rid3 + '/retry', headers=self.H); self.assertEqual(rt.status_code, 202, rt.text)
        rv = rt.json(); jid3b = rv['job_id']
        self.assertNotEqual(jid3b, jid3); self.assertEqual((rv['attempt'], rv['state'], rv['settlement']['payment_id'], rv['settlement']['state']), (2, 'running', pid3, 'AUTHORIZED'))
        self.assertEqual(rv['delivery']['previous_attempt_job'], jid3)
        self.assertEqual(self.w.run_once(), (jid3b, 'succeeded')); self.assertEqual(self.run_view(rid3)['state'], 'awaiting_verification'); self.assertEqual(self.w.run_once()[1], 'succeeded')
        self.assertEqual(self.run_view(rid3)['state'], 'delivered')
        st3 = self.settle(pid3); self.assertEqual(st3['state'], 'SETTLED'); self.assertEqual(st3['job_id'], jid3b)
        final = chain.balances(); self.assertEqual(after['payer'] - final['payer'], int(st3['final_amount']))
        self.assertEqual(self.settle(pid3)['transaction'], st3['transaction']); self.assertEqual(chain.balances(), final)      # settled once
        self.assertEqual(self.c.get('/api/v1/jobs/' + jid3, headers=self.H).json()['state'], 'succeeded')                     # failure evidence preserved
        # journal vs chain: every SETTLED record's final amount is reflected on the chain; unused authorizations moved nothing
        items = self.c.get('/api/v1/x402/settlements', headers=self.H).json()['items']
        settled = sum(int(i['final_amount']) for i in items if i['state'] == 'SETTLED')
        self.assertEqual(before['payer'] - final['payer'], settled); self.assertEqual(sum(1 for i in items if i['state'] == 'AUTHORIZATION_UNUSED'), 1)
        # the fault route is unavailable on an instance without test hooks
        plain = UptoInstance(); self.addCleanup(plain.close)
        self.assertEqual(plain.client.post('/api/v1/ops/faults', headers=plain.h('owner'), json={'fault': 'verification_fail', 'job_id': 'j_x'}).status_code, 501)


if __name__ == '__main__':
    unittest.main()
