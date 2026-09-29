"""Fault campaign and independent invariant checks (Order 08 §55, §73): targeted injection around award commit,
reservation posting, evidence publication, verifier completion, acceptance decision, payment signing, submission,
observation, fee credit and refund observation on a disposable instance. For each boundary the expected durable facts
are stated BEFORE the injection; a restart (a fresh Services over the same database) must show no second entitlement,
no lost accepted evidence, no invented confirmed payment and no spendable balance released from unresolved exposure.
A small independent accounting model (not the production balance functions) checks the concurrent histories."""
import json
import unittest

from integrations.x402.local_chain import harness
from metacoin_service.tests.test_work_money import WorkMoneyTests
from metacoin_service import api as api_mod

HAVE_CHAIN = harness.ARTIFACTS.exists()


class Model:
    """Independent expected-balance model: obligations per entitlement and one-shot transfers; compares with the journal."""
    def __init__(self):
        self.payable, self.paid, self.exposure = {}, {}, {}

    def accept(self, eid, amount):
        self.payable[eid] = amount

    def submit(self, eid):
        self.exposure[eid] = self.payable.pop(eid)

    def settle(self, eid):
        self.paid[eid] = self.exposure.pop(eid)

    def unsubmit(self, eid):
        self.payable[eid] = self.exposure.pop(eid)          # reconciled: the submission never executed

    def totals(self):
        return {'obligations_payable': sum(self.payable.values()), 'exposure_pending': sum(self.exposure.values()), 'paid_out': sum(self.paid.values())}


@unittest.skipUnless(HAVE_CHAIN, 'needs the built local-chain artifacts')
class WorkFaultTests(WorkMoneyTests):
    def arm(self, point):
        r = self.c.post('/api/v1/ops/faults', headers=self.H, json={'fault': point}); self.assertEqual(r.status_code, 200, r.text)

    def disarm(self, point):
        self.c.post('/api/v1/ops/faults', headers=self.H, json={'fault': point, 'disarm': True})

    def restart(self):
        """A fresh Services over the same database (the chain object is shared so settlement facts stay observable)."""
        old = self.inst.app.state.services
        chain, fac = old.sales._chain, old.sales._chain_facilitator
        self.inst.reopen(); self.c = self.inst.client
        new = self.inst.app.state.services; new.sales._chain, new.sales._chain_facilitator = chain, fac; new.sales._chain.patch_sdk()
        self.w = self.inst.worker()

    def counts(self, aid):
        with self.inst.app.state.services.db.read() as db:
            return {'awards': db.execute("SELECT COUNT(*) FROM work_awards WHERE request_id=(SELECT request_id FROM work_awards WHERE id=?) ", (aid,)).fetchone()[0] if aid else None,
                    'reservations': db.execute("SELECT COUNT(*) FROM budget_reservations WHERE ref_type='work_award'").fetchone()[0],
                    'entitlements': db.execute("SELECT COUNT(*) FROM work_entitlements").fetchone()[0], 'decisions': db.execute("SELECT COUNT(*) FROM work_decisions").fetchone()[0],
                    'intents': {r[0]: r[1] for r in db.execute('SELECT state, COUNT(*) FROM payment_intents GROUP BY state').fetchall()}, 'journal_entries': db.execute('SELECT COUNT(*) FROM journal_entries').fetchone()[0]}

    def test_award_and_reservation_faults_leave_no_partial_award(self):
        t = self.terms(); self.c.post('/api/v1/work/terms/' + t['id'] + '/freeze', headers=self.H, json={'inputs': __import__('metacoin_service.tests.test_work_terms', fromlist=['energy_inputs']).energy_inputs('FEASIBLE')})
        r = self.c.post('/api/v1/work/requests', headers=self.H, json={'terms_id': t['id']}).json(); self.c.post('/api/v1/work/requests/' + r['id'] + '/open', headers=self.H, json={})
        o = self.c.post('/api/v1/work/requests/' + r['id'] + '/offers', headers=self.pv['alpha']['h'], json={'price_amount': 5, 'asset': 'local-chain-token', 'scheme': 'exact', 'window_seconds': 3600, 'verification': {'class': 'full_exact'}}).json()
        for point in ('reservation_posting', 'award_commit'):
            before = self.counts(None); self.arm(point)
            resp = self.c.post('/api/v1/work/requests/' + r['id'] + '/award', headers=self.H, json={'offer_id': o['id']}); self.disarm(point)
            self.assertEqual(resp.status_code, 409, resp.text); self.assertEqual(resp.json()['code'], 'INTERNAL_DEFECT')
            self.restart(); after = self.counts(None)
            # expected durable facts: no award row, no reservation, the offer still offered, the request still open
            self.assertEqual((after['reservations'], after['awards']), (before['reservations'], before['awards']))
            self.assertEqual(self.c.get('/api/v1/work/requests/' + r['id'], headers=self.H).json()['state'], 'open')
            self.assertEqual({x['id']: x['state'] for x in self.c.get('/api/v1/work/requests/' + r['id'], headers=self.H).json()['offers']}[o['id']], 'offered')
        a = self.c.post('/api/v1/work/requests/' + r['id'] + '/award', headers=self.H, json={'offer_id': o['id']}); self.assertEqual(a.status_code, 201)     # recovery: the same offer awards cleanly once
        self.assertEqual(self.counts(a.json()['id'])['awards'], 1)

    def test_evidence_verifier_and_decision_faults(self):
        t, f, r, a = self.awarded('FEASIBLE', price=6)
        # evidence publication: the worker's publication transaction fails after computing; the lease expires and the job is recovered; evidence is not lost or duplicated
        self.arm('evidence_publication')
        try:
            self.w.run_once()
        except Exception:
            pass
        self.disarm('evidence_publication')
        with self.inst.app.state.services.db.tx() as db:
            j = db.execute('SELECT id, state FROM jobs WHERE id=(SELECT job_id FROM work_milestones WHERE award_id=?)', (a['id'],)).fetchone()
            self.assertEqual(j['state'], 'running')                                                                       # nothing published; lease held
            db.execute('UPDATE jobs SET lease_expires=1 WHERE id=?', (j['id'],))
        self.restart(); self.w.run_once()
        aw = self.c.get('/api/v1/work/awards/' + a['id'], headers=self.H).json(); self.assertEqual(aw['milestones'][0]['dimensions']['execution'], 'completed')
        with self.inst.app.state.services.db.read() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM artifacts WHERE job_id=? AND deleted_at IS NULL', (j['id'],)).fetchone()[0], 1)          # one evidence artifact recorded
        # verifier completion: the audit's finish is interrupted; the verification stays queued and completes on retry without a duplicate statement
        self.c.post('/api/v1/work/awards/%s/milestones/m1/verify' % a['id'], headers=self.H, json={}); self.arm('verifier_completion')
        try:
            self.w.run_once()
        except Exception:
            pass
        self.disarm('verifier_completion'); self.restart()
        with self.inst.app.state.services.db.tx() as db:
            v = db.execute("SELECT id, state, audit_job_id FROM verification_jobs WHERE target_job_id=?", (j['id'],)).fetchone(); self.assertIn(v['state'], ('queued',))
            db.execute("UPDATE jobs SET lease_expires=1 WHERE id=?", (v['audit_job_id'],))
        self.w.run_once(); self.w.run_once()
        vs = self.c.get('/api/v1/verification/' + v['id'], headers=self.H).json(); self.assertEqual(vs['state'], 'passed')
        with self.inst.app.state.services.db.read() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM verification_jobs WHERE target_job_id=?', (j['id'],)).fetchone()[0], 1)
        # acceptance decision: interrupted after the decision row is written -> rolled back; no decision, no entitlement; then it succeeds once
        self.arm('acceptance_decision'); d = self.c.post('/api/v1/work/awards/%s/milestones/m1/decide' % a['id'], headers=self.H, json={'decision': 'accept'}); self.disarm('acceptance_decision')
        self.assertEqual(d.json()['code'], 'INTERNAL_DEFECT'); self.restart()
        c = self.counts(a['id']); self.assertEqual((c['decisions'], c['entitlements']), (0, 0))
        self.assertEqual(self.c.get('/api/v1/work/awards/' + a['id'], headers=self.H).json()['milestones'][0]['dimensions']['acceptance'], 'pending')
        d = self.c.post('/api/v1/work/awards/%s/milestones/m1/decide' % a['id'], headers=self.H, json={'decision': 'accept'}).json(); self.assertEqual(d['decision'], 'accepted')
        self.assertEqual(self.counts(a['id'])['entitlements'], 1)

    def test_payment_faults_never_invent_a_settlement_or_release_exposure(self):
        model = Model()
        t, f, r, a = self.awarded('FEASIBLE', price=8); d = self.deliver_accept(a['id']); eid = d['entitlement']['id']; model.accept(eid, 8)
        p0 = self.bal('requester_payer')
        # signing: the authorization is discarded; the intent stays prepared; nothing spendable persisted
        i = self.c.post('/api/v1/work/entitlements/' + eid + '/prepare', headers=self.H, json={}).json()
        self.arm('payment_signing'); resp = self.c.post('/api/v1/work/intents/' + i['id'] + '/authorize', headers=self.H, json={}); self.disarm('payment_signing')
        self.assertEqual(resp.json()['code'], 'INTERNAL_DEFECT'); self.restart()
        iv = self.c.get('/api/v1/work/intents/' + i['id'], headers=self.H).json(); self.assertEqual(iv['state'], 'prepared')
        with self.inst.app.state.services.db.read() as db:
            self.assertIsNone(db.execute('SELECT authorization_json FROM payment_intents WHERE id=?', (i['id'],)).fetchone()['authorization_json'])
        self.c.post('/api/v1/work/intents/' + i['id'] + '/authorize', headers=self.H, json={})
        # submission: durable 'submitted' before the rail; the fault fires before the rail is touched -> exposure retained, nothing moved
        self.arm('payment_submission'); resp = self.c.post('/api/v1/work/intents/' + i['id'] + '/submit', headers=self.H, json={}); self.disarm('payment_submission')
        self.assertEqual(resp.json()['code'], 'INTERNAL_DEFECT'); self.restart(); model.submit(eid)
        iv = self.c.get('/api/v1/work/intents/' + i['id'], headers=self.H).json(); self.assertEqual(iv['state'], 'submitted'); self.assertEqual(self.bal('requester_payer'), p0)
        self.assertEqual(self.c.post('/api/v1/work/intents/' + i['id'] + '/submit', headers=self.H, json={}).status_code, 409)                 # no blind retry
        rep = self.c.post('/api/v1/work/journal/replay', headers=self.H, json={}).json(); sc = next(k for k in rep['scopes'] if k.startswith('local-chain-token'))
        self.assertEqual({k: rep['scopes'][sc][k] for k in ('obligations_payable', 'exposure_pending', 'paid_out')}, model.totals())
        rc = self.c.post('/api/v1/work/intents/' + i['id'] + '/reconcile', headers=self.H, json={}).json(); self.assertEqual(rc['state'], 'authorized'); model.unsubmit(eid)      # nonce unused: retry allowed
        # observation: the rail executes, the response is dropped -> unknown; restart; reconciliation finds the nonce consumed -> settled once
        self.arm('payment_observation'); resp = self.c.post('/api/v1/work/intents/' + i['id'] + '/submit', headers=self.H, json={}).json(); self.disarm('payment_observation')
        self.assertEqual(resp['state'], 'unknown'); self.assertEqual(self.bal('requester_payer'), p0 - 8); model.submit(eid); self.restart()
        self.assertEqual(self.c.post('/api/v1/work/intents/' + i['id'] + '/submit', headers=self.H, json={}).status_code, 409)
        rc = self.c.post('/api/v1/work/intents/' + i['id'] + '/reconcile', headers=self.H, json={}).json(); self.assertEqual(rc['state'], 'settled'); model.settle(eid)
        self.assertEqual(self.bal('requester_payer'), p0 - 8)
        rep = self.c.post('/api/v1/work/journal/replay', headers=self.H, json={}).json()
        self.assertEqual({k: rep['scopes'][sc][k] for k in ('obligations_payable', 'exposure_pending', 'paid_out')}, model.totals()); self.assertTrue(rep['consistent'])
        with self.inst.app.state.services.db.read() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM payment_intents WHERE entitlement_id=?", (eid,)).fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM work_receipts WHERE kind='settlement' AND subject_id=?", (i['id'],)).fetchone()[0], 1)

    def test_fee_credit_and_refund_observation_faults(self):
        t, f, r, a = self.awarded('FEASIBLE', price=10, ceiling=11, bps=1000); d = self.deliver_accept(a['id'])
        with self.inst.app.state.services.db.read() as db:
            fee = db.execute("SELECT id FROM work_entitlements WHERE award_id=? AND kind='fee'", (a['id'],)).fetchone()['id']
        self.pay(d['entitlement']['id'])
        i = self.c.post('/api/v1/work/entitlements/' + fee + '/prepare', headers=self.H, json={}).json(); self.c.post('/api/v1/work/intents/' + i['id'] + '/authorize', headers=self.H, json={})
        tr0 = self.bal('treasury'); self.arm('fee_credit'); resp = self.c.post('/api/v1/work/intents/' + i['id'] + '/submit', headers=self.H, json={}); self.disarm('fee_credit')
        # the rail settled (the chain moved) but the recording transaction rolled back: the intent shows submitted, revenue not yet credited
        self.assertEqual(resp.json()['code'], 'INTERNAL_DEFECT'); self.assertEqual(self.bal('treasury'), tr0 + 1); self.restart()
        self.assertEqual(self.c.get('/api/v1/work/treasury', headers=self.H).json()['confirmed_revenue'], 0)
        self.assertEqual(self.c.get('/api/v1/work/intents/' + i['id'], headers=self.H).json()['state'], 'submitted')
        rc = self.c.post('/api/v1/work/intents/' + i['id'] + '/reconcile', headers=self.H, json={}).json(); self.assertEqual(rc['state'], 'settled')
        self.c.post('/api/v1/work/intents/' + i['id'] + '/reconcile', headers=self.H, json={})
        tv = self.c.get('/api/v1/work/treasury', headers=self.H).json(); self.assertEqual((tv['confirmed_revenue'], tv['available']), (1, 1))                # credited exactly once
        # refund observation: the reverse transfer executes, its observation is interrupted; reconcile by nonce; no second refund
        a0 = self.bal('provider_a'); self.arm('refund_observation')
        resp = self.c.post('/api/v1/work/entitlements/' + d['entitlement']['id'] + '/refund', headers=self.H, json={'amount': 3, 'provider_preauthorized': True, 'request_key': 'f-1'}); self.disarm('refund_observation')
        self.assertEqual(resp.json()['code'], 'INTERNAL_DEFECT'); self.assertEqual(self.bal('provider_a'), a0 - 3); self.restart()
        with self.inst.app.state.services.db.read() as db:
            rid = db.execute("SELECT id, state FROM payment_intents WHERE kind='refund' ORDER BY rowid DESC LIMIT 1").fetchone()
        self.assertEqual(rid['state'], 'submitted')
        rc = self.c.post('/api/v1/work/intents/' + rid['id'] + '/reconcile', headers=self.H, json={}).json(); self.assertEqual(rc['state'], 'settled'); self.assertEqual(self.bal('provider_a'), a0 - 3)
        again = self.c.post('/api/v1/work/entitlements/' + d['entitlement']['id'] + '/refund', headers=self.H, json={'amount': 3, 'provider_preauthorized': True, 'request_key': 'f-1'}).json(); self.assertTrue(again.get('replayed'))
        rep = self.c.post('/api/v1/work/journal/replay', headers=self.H, json={}).json(); self.assertTrue(rep['consistent'], rep['differences']); self.assertTrue(all(c['ok'] for c in rep['invariants']))

    def test_adversarial_accounting_cases(self):
        # one award submitted twice (same offer): one award; two milestones competing for one remaining ceiling: the second dispatch waits
        t, f, r, a = self.awarded('FEASIBLE', price=6)
        again = self.c.post('/api/v1/work/requests/' + r['id'] + '/award', headers=self.H, json={'offer_id': self.c.get('/api/v1/work/requests/' + r['id'], headers=self.H).json()['awards'][0]['offer_id']}).json(); self.assertTrue(again.get('replayed'))
        self.assertEqual(self.c.put('/api/v1/budgets/workspace', headers=self.H, json={'ceiling': 6}).status_code, 200)
        t2 = self.terms(); self.c.post('/api/v1/work/terms/' + t2['id'] + '/freeze', headers=self.H, json={'inputs': __import__('metacoin_service.tests.test_work_terms', fromlist=['energy_inputs']).energy_inputs('FEASIBLE')})
        r2 = self.c.post('/api/v1/work/requests', headers=self.H, json={'terms_id': t2['id']}).json(); self.c.post('/api/v1/work/requests/' + r2['id'] + '/open', headers=self.H, json={})
        o2 = self.c.post('/api/v1/work/requests/' + r2['id'] + '/offers', headers=self.pv['beta']['h'], json={'price_amount': 3, 'asset': 'local-chain-token', 'scheme': 'exact', 'window_seconds': 3600, 'verification': {'class': 'full_exact'}}).json()
        blocked = self.c.post('/api/v1/work/requests/' + r2['id'] + '/award', headers=self.H, json={'offer_id': o2['id']}).json(); self.assertEqual(blocked['code'], 'BUDGET_EXHAUSTED'); self.assertTrue(blocked['detail']['retryable'])
        self.c.put('/api/v1/budgets/workspace', headers=self.H, json={'ceiling': 10000})
        # inflated usage statement: the provider's telemetry is attested, the settled amount follows the decision, not the meter
        d = self.deliver_accept(a['id']); s = self.pay(d['entitlement']['id']); self.assertEqual(s['final_amount'], 6)
        recs = self.c.get('/api/v1/work/awards/' + a['id'] + '/receipts', headers=self.H).json()['items']; prov = next(x for x in recs if x['kind'] == 'provider')
        self.assertIn('attested_by_provider', prov['statement']['claims']['resources']); self.assertEqual(prov['statement']['claims']['resources']['energy']['available'], False)
        # dispute while settlement pending and refund before reconciliation: refused / held
        t3, f3, r3, a3 = self.awarded('FEASIBLE', price=4); d3 = self.deliver_accept(a3['id'])
        u = self.pay(d3['entitlement']['id'], body={'_simulate_lost_response': True}, expect_state='unknown')
        dp = self.c.post('/api/v1/work/awards/%s/milestones/m1/dispute' % a3['id'], headers=self.pv['alpha']['h'], json={'claim': 'x'}); self.assertEqual(dp.status_code, 201)
        rf = self.c.post('/api/v1/work/entitlements/' + d3['entitlement']['id'] + '/refund', headers=self.H, json={'amount': 1, 'provider_preauthorized': True}); self.assertEqual(rf.json()['detail']['code'], 'nothing_settled')
        ex = self.c.get('/api/v1/work/exposure', headers=self.H).json(); self.assertIn(u['id'], [x['id'] for x in ex['unresolved']])
        rep = self.c.post('/api/v1/work/journal/replay', headers=self.H, json={}).json(); self.assertTrue(rep['consistent'], rep['differences'])


if __name__ == '__main__':
    unittest.main()
