"""The twenty-six acceptance journeys of the order of 2026-10-22 against a RUNNING API process (test-http instance, private
local chain, separate worker processes). Journeys 1-23 and 25 run here through the real HTTP entry points and persisted
state; 24 is the Playwright console journey (--shots); 26 (clean exported tree) is recorded by clean_export.sh.

    METACOIN_PLAYWRIGHT_PYTHON=... PYTHONPATH=. .venv-service/bin/python -m metacoin_service.tests.journeys_decision --out journeys-decision.json [--only 1,2] [--shots DIR]"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import traceback
import zipfile
from fractions import Fraction
from pathlib import Path

from metacoin_service.tests.journeys_economy import Journeys as EconomyJourneys, PY, ROOT, RUNTIME, HAVE_TORCH
from metacoin_service.tests.test_decision_service import analysis_inputs, menu, delivery, HOT, HOT3, TOTAL
from metacoin_service.tests.test_compute_engine import batch_spec, heat_spec
from metacoin_service.decision import kernels as K, checker
from metacoin_service.db import now


class Journeys(EconomyJourneys):
    def __init__(self):
        super().__init__()
        p = self.http.post('/api/v1/work/providers', headers=self.H(), json={'name': 'lab', 'capabilities': {'kinds': ['evidence_acquisition', 'decision_observability', 'energy_audit'], 'verification_classes': ['analytical', 'full_exact'], 'payment_schemes': ['exact', 'upto'], 'privacy_modes': ['requester_private']},
                                                                            'pay_to': self.acct.get('provider_b', 'provider:lab')}).json()
        self.lab = {'id': p['id'], 'h': {'Authorization': 'Bearer ' + p['credential']['token']}, 'principal_id': p['principal_id'], 'cred': self.cred_file('provider-lab', p['credential']['token'])}

    # ---- helpers ----------------------------------------------------------------------------------------------------------------------
    def analyze(self, inputs, h=None):
        st, r = self.call('post', '/api/v1/decisions/analyze', h=h, json={'inputs': inputs, 'title': 'heater start'}); assert st == 202, r
        self.run_workers(2, 'an'); return self.call('get', '/api/v1/decisions/' + r['decision_job_id'], h=h)[1]

    def plan(self, jid, budget, expires=None, create=True, h=None):
        body = {'analysis_job_id': jid, 'menu': menu(expires or (now() + 3600), self.lab['id']), 'budget': budget}
        st, r = self.call('post', '/api/v1/acquisition-plans' + ('' if create else '/preview'), h=h, json=body); assert st == (201 if create else 200), r
        return r

    def acquire(self, budget=3, diagnostic=0, inputs=None, price=None):
        an = self.analyze(inputs or analysis_inputs()); pl = self.plan(an['job']['id'], budget)
        st, m = self.call('post', '/api/v1/acquisition-plans/' + pl['id'] + '/materialize', json={'diagnostic_amount': diagnostic}); assert st == 201, m
        st, o = self.call('post', '/api/v1/work/requests/' + m['request']['id'] + '/offers', h=self.lab['h'], json={'price_amount': price if price is not None else pl['cost'], 'asset': 'action-units', 'scheme': 'exact', 'window_seconds': 3600, 'verification': {'class': 'analytical', 'distinct_verifier': False}}); assert st == 201, o
        st, a = self.award(m['request']['id'], o['id']); assert st == 201, a
        return an, pl, m, a

    def deliver(self, aid, d, h=None):
        return self.call('post', '/api/v1/work/awards/%s/milestones/m1/deliver' % aid, h=h or self.lab['h'], json={'delivery': d})

    def dview(self, jid, h=None):
        return self.call('get', '/api/v1/decisions/' + jid, h=h)[1]

    def complete(self, aid):
        """deliver -> execute -> verify (analytical) -> evaluate; returns (decision view, evaluation)."""
        self.run_workers(2, 'acq'); self.verify(aid); ev = self.evaluate(aid)
        jid = self.aview(aid)['milestones'][0]['job_id']; return self.dview(jid), ev

    def temporal_batch_job(self, **over):
        st, c = self.call('post', '/api/v1/contracts', json={'kind': 'temporal_batch', 'title': 'batch', 'inputs': batch_spec(**over), 'policy': {'reviewer_id': self.inst.ids['reviewer']}}); assert st == 201, c
        assert self.call('post', '/api/v1/contracts/' + c['id'] + '/freeze')[0] == 200
        st, j = self.call('post', '/api/v1/jobs', json={'contract_id': c['id']}); assert st == 202, j
        self.run_workers(2, 'tb'); return j['id']

    def rec(self, n, title, ok, evidence, caveat=None, t0=None, blocked=None):
        evidence = dict(evidence or {}, seconds=round(time.time() - t0, 3) if t0 else None)
        self.record(n, title, 'blocked' if blocked else ('passed' if ok else 'failed'), dict(evidence, blocked=blocked) if blocked else evidence, caveat)

    def guard(self, n, title, fn):
        t0 = time.time()
        try:
            fn(t0)
        except Exception as exc:
            self.record(n, title, 'failed', {'exception': type(exc).__name__, 'detail': str(exc)[:600], 'trace': traceback.format_exc()[-1200:], 'seconds': round(time.time() - t0, 3)})

    # ---- journeys 1-10: observability --------------------------------------------------------------------------------------------------
    def j1(self, t0):
        an = self.analyze(analysis_inputs(views=[dict(TOTAL, id='total_%d' % i) for i in range(4)]))
        w = self.call('get', '/api/v1/decisions/%s/witnesses' % an['job']['id'])[1]
        ok = an['projection']['conclusion'] == 'AMBIGUOUS' and an['readings']['label'] == 'ambiguous' and w['ambiguity_witnesses'][0]['shared_observation']['total_0'] == ['120']
        self.ctx['j1'] = an['job']['id']
        self.rec(1, 'repeated identical total measurements remain ambiguous (witness: both states predict 120)', ok, {'job': an['job']['id'], 'conclusion': an['projection']['conclusion'], 'reading': an['readings']['label']}, t0=t0)

    def j2(self, t0):
        an = self.analyze(analysis_inputs(views=[TOTAL, {'id': 'cell_1_sensor', 'rows': [[1, 0]], 'noise': [2], 'output_units': ['mJ'], 'cost': 3}]))
        self.rec(2, 'a complementary measurement resolves the declared two-state decision', an['projection']['conclusion'] == 'SEPARATED_ON_DECLARED_SET' and an['readings']['label'] == 'separated within these assumptions', {'conclusion': an['projection']['conclusion'], 'reading': an['readings']['label']}, t0=t0)

    def j3(self, t0):
        an = self.analyze(analysis_inputs(views=[TOTAL, {'id': 'near_parallel', 'rows': [[1, '1001/1000']], 'noise': [1], 'output_units': ['mJ'], 'cost': 1}]))
        self.rec(3, 'near-parallel measurements fail to separate states when noise masks their difference', an['projection']['conclusion'] == 'AMBIGUOUS', {'conclusion': an['projection']['conclusion']}, t0=t0)

    def j4(self, t0):
        an = self.analyze(analysis_inputs(views=[{'id': 'c1', 'rows': [[1, 0]], 'noise': [40], 'output_units': ['mJ'], 'cost': 1}]))
        w = self.call('get', '/api/v1/decisions/%s/witnesses' % an['job']['id'])[1]
        self.rec(4, 'touching closed error intervals are not treated as separated (gap 80 = 2*40)', an['projection']['conclusion'] == 'AMBIGUOUS' and w['ambiguity_witnesses'][0]['shared_observation']['c1'] == ['60'], {'witness': w['ambiguity_witnesses'][0]['shared_observation']}, t0=t0)

    def j5(self, t0):
        jid = self.ctx['j1']
        st, v = self.call('post', '/api/v1/verification', json={'job_id': jid, 'class': 'analytical'}); assert st == 202, v
        self.run_workers(2, 'chk'); vs = self.call('get', '/api/v1/verification/' + v['id'])[1]; a = self.call('get', '/api/v1/verification/' + v['id'] + '/assurance')[1]
        ok = vs['state'] == 'passed' and a['claim_type'] == 'witness_check' and a['independence']['implementation'] is True and a['outcome'] == 'passed'
        self.rec(5, 'the generated ambiguity witness passes a separate exact checker (verification class analytical, independent implementation)', ok, {'verification': v['id'], 'state': vs['state'], 'claim': a['claim_type']}, t0=t0)

    def j6(self, t0):
        an = self.dview(self.ctx['j1']); fake = json.loads(json.dumps(an['result'])); fake['ambiguity_witnesses'][0]['shared_observation']['total_0'] = ['300']
        same_label = checker.check_analysis(an['inputs'], fake)
        ok = not same_label['ok'] and checker.check_analysis(an['inputs'], an['result'])['ok']
        self.rec(6, 'a fabricated witness (observation outside one noise box) fails that checker; the genuine one passes', ok, {'fabricated': same_label['ok'], 'checker': checker.CHECKER_ID}, t0=t0)

    def j7(self, t0):
        pl = self.plan(self.ctx['j1'], 2)
        st, m = self.call('post', '/api/v1/acquisition-plans/' + pl['id'] + '/materialize', json={})
        before = self.call('get', '/api/v1/work/journal')[1]
        ok = pl['selected_views'] == [] and pl['state'] == 'nothing_to_acquire' and st == 409 and m['detail']['code'] == 'nothing_to_acquire'
        self.rec(7, 'a budget that buys no useful information causes no useless acquisition charge (no terms, no reservation, no journal entry)', ok, {'plan': pl['id'], 'state': pl['state'], 'journal_entries': len(before.get('entries', []))}, t0=t0)

    def j8(self, t0):
        pl = self.plan(self.ctx['j1'], 3, create=False); an = self.dview(self.ctx['j1'])
        oracle = K.plan_oracle(an['inputs'], menu(now() + 3600), 3)
        ok = (pl['selected_views'], pl['cost'], pl['optimization_status']) == (oracle['selected_views'], oracle['cost'], 'exact')
        self.rec(8, 'exact finite planning agrees with an independent brute-force oracle on the small menu', ok, {'selected': pl['selected_views'], 'cost': pl['cost'], 'oracle': oracle['selected_views']}, t0=t0)

    def j9(self, t0):
        base = self.analyze(analysis_inputs(views=[TOTAL, {'id': 's', 'rows': [[1, 0]], 'noise': [2], 'output_units': ['mJ'], 'cost': 3}]))
        scaled = self.analyze(analysis_inputs(views=[TOTAL, {'id': 's', 'rows': [['7', 0]], 'noise': ['14'], 'output_units': ['mJ'], 'cost': 3}]))
        half = self.analyze(analysis_inputs(views=[TOTAL, {'id': 's', 'rows': [['1/3', 0]], 'noise': ['2/3'], 'output_units': ['mJ'], 'cost': 3}]))
        ok = base['projection']['conclusion'] == scaled['projection']['conclusion'] == half['projection']['conclusion'] == 'SEPARATED_ON_DECLARED_SET'
        self.rec(9, 'scaling an observation row and its noise consistently preserves the separation decision', ok, {'conclusions': [x['projection']['conclusion'] for x in (base, scaled, half)]}, t0=t0)

    def j10(self, t0):
        an = self.analyze(analysis_inputs(observations={'total': ['500']}))
        ok = an['projection']['conclusion'] == 'MODEL_INCONSISTENT_WITH_OBSERVATIONS' and an['result']['conditioning']['surviving'] == [] and an['readings']['label'] == 'unsupported'
        self.rec(10, 'actual observations outside all hypotheses return model inconsistency (no nearest hypothesis is chosen)', ok, {'conclusion': an['projection']['conclusion'], 'surviving': an['result']['conditioning']['surviving']}, t0=t0)

    # ---- 11-16: sampling plans and assurance --------------------------------------------------------------------------------------------
    def j11(self, t0):
        if not RUNTIME:
            self.rec(11, 'sampling plan bound and minimal', False, {}, blocked='compute interpreter unavailable', t0=t0); return
        jid = self.temporal_batch_job(); self.ctx['batch'] = jid
        st, sp = self.call('post', '/api/v1/verification/sampling-plans', json={'job_id': jid, 'minimum_defects': 10, 'alpha': '1/100', 'max_samples': 500}); assert st == 201, sp
        N, k = sp['population'], sp['required_samples']
        ok = sp['state'] == 'bound' and K.miss_at_most(N, 10, k, Fraction(1, 100)) and not K.miss_at_most(N, 10, k - 1, Fraction(1, 100)) and K.sampling_plan(N, 10, '1/100', N)['required_samples'] == k
        st, au = self.call('post', '/api/v1/verification/sampling-plans/' + sp['id'] + '/audit', json={}); assert st == 202, au
        self.run_workers(2, 'aud'); v = self.call('get', '/api/v1/verification/' + au['id'])[1]; stt = self.call('get', '/api/v1/verification/' + au['id'] + '/statement')[1]
        off = self.call('post', '/api/v1/verification/sampling-plans/verify', json={'bundle': stt, 'expected_plan': {'plan_digest': sp['plan_digest']}})[1]
        ok = ok and v['state'] == 'passed' and v['challenge']['sample_count'] == k and off['ok'] and off['signature']['valid'] and off['arithmetic']['ok']
        self.ctx['sp'] = sp['id']; self.ctx['sp_v'] = au['id']
        self.rec(11, 'a sample plan meets its declared finite-population bound, is minimal for its target, is bound before the challenge, and its signed statement recomputes offline (signature and arithmetic reported separately)', ok,
                 {'population': N, 'required_samples': k, 'alpha': '1/100', 'm_assumed': 10, 'verification': au['id'], 'offline': {'signature': off['signature']['valid'], 'arithmetic': off['arithmetic']['ok']}}, t0=t0)

    def j12(self, t0):
        if not RUNTIME:
            self.rec(12, 'single-defect refusal', False, {}, blocked='compute interpreter unavailable', t0=t0); return
        jid = self.ctx['batch']
        st, sp = self.call('post', '/api/v1/verification/sampling-plans', json={'job_id': jid, 'minimum_defects': 1, 'alpha': '1/100', 'max_samples': 40})
        st2, au = self.call('post', '/api/v1/verification/sampling-plans/' + sp['id'] + '/audit', json={})
        ok = st == 201 and sp['state'] == 'infeasible' and sp['required_samples'] > sp['max_samples'] and sp['alpha'] == '1/100' and st2 == 409 and au['detail']['code'] == 'sampling_plan_infeasible'
        self.rec(12, 'a single-defect target refuses an insufficient sample budget: achieved bound and required budget reported, alpha and m never relaxed', ok, {'required': sp.get('required_samples'), 'budget': sp.get('max_samples'), 'achieved': (sp.get('achieved_bound') or {}).get('exact')}, t0=t0)

    def j13(self, t0):
        if not RUNTIME:
            self.rec(13, 'changed binding invalidates', False, {}, blocked='compute interpreter unavailable', t0=t0); return
        other = self.temporal_batch_job(grid=[{'path': 'reserve', 'start': 0, 'stop': 4000, 'step': 100}, {'path': 'load_scale_percent', 'values': [50, 100]}])
        st, bad = self.call('post', '/api/v1/verification', json={'job_id': other, 'class': 'sampled_reference', 'params': {'sampling_plan_id': self.ctx['sp']}})
        st2, sp2 = self.call('post', '/api/v1/verification/sampling-plans', json={'job_id': other, 'minimum_defects': 3, 'alpha': '1/20', 'max_samples': 300})
        from metacoin_service.db import Database
        with Database(self.inst.settings.db_path).tx() as db:
            db.execute("UPDATE jobs SET evidence_root=? WHERE id=?", ('1' * 64, other))
        st3, inv = self.call('post', '/api/v1/verification/sampling-plans/' + sp2['id'] + '/audit', json={})
        state = self.call('get', '/api/v1/verification/sampling-plans/' + sp2['id'])[1]['state']
        ok = st == 409 and bad['detail']['code'] == 'sampling_plan_binding' and st3 == 409 and inv['detail']['code'] == 'sampling_plan_invalidated' and state == 'invalidated'
        self.rec(13, 'a changed frame digest / scope / model / result root invalidates the attached sampling claim (another job refused; changed root invalidates the plan durably)', ok, {'other_job': bad['detail']['code'], 'changed_root': inv['detail']['code'], 'state': state}, t0=t0)

    def j14(self, t0):
        an = self.dview(self.ctx['j1']); jid = an['job']['id']
        req = {'schema': 'metacoin-assurance-requirement/v1', 'claim_type': 'witness_check', 'coverage': 'whole_result', 'comparison': {'semantics': 'enumerated_predicate'}, 'independence': {'implementation': True}}
        pv = self.call('post', '/api/v1/verification/assurance/evaluate', json={'job_id': jid, 'requirement': req})[1]
        st, v = self.call('post', '/api/v1/verification', json={'job_id': jid, 'class': 'full_exact'}); self.run_workers(2, 'fe')
        reg = {'schema': 'metacoin-assurance-requirement/v1', 'claim_type': 'same_implementation_regression', 'coverage': 'whole_result', 'comparison': {'semantics': 'exact'}, 'independence': {'implementation': True}}
        pv2 = self.call('post', '/api/v1/verification/assurance/evaluate', json={'job_id': jid, 'requirement': reg})[1]
        claims = self.call('get', '/api/v1/verification/assurance/claims')[1]
        ok = pv['satisfied'] is True and pv2['satisfied'] is False and 'independence:implementation' in pv2['missing_clauses'] and 'every_item->sampled_items' in claims['implications']
        self.rec(14, 'a same-implementation check cannot satisfy a distinct independent requirement merely through its class or rank (and the independent witness check satisfies only its own claim)', ok, {'witness_check': pv['status'], 'regression_claims_independence': pv2['status'], 'missing': pv2['missing_clauses']}, t0=t0)

    def j15(self, t0):
        if not RUNTIME:
            self.rec(15, 'final transition is not the trajectory', False, {}, blocked='compute interpreter unavailable', t0=t0); return
        st, c = self.call('post', '/api/v1/contracts', json={'kind': 'heat_diffusion', 'title': 'heat', 'inputs': heat_spec(nx=48, ny=48, steps=200), 'policy': {'reviewer_id': self.inst.ids['reviewer']}}); assert st == 201, c
        self.call('post', '/api/v1/contracts/' + c['id'] + '/freeze'); st, j = self.call('post', '/api/v1/jobs', json={'contract_id': c['id']}); self.run_workers(2, 'heat')
        st, v = self.call('post', '/api/v1/verification', json={'job_id': j['id'], 'class': 'sampled_reference', 'params': {}}); self.run_workers(2, 'hs')
        whole = {'schema': 'metacoin-assurance-requirement/v1', 'claim_type': 'whole_result_reference', 'coverage': 'whole_result', 'comparison': {'semantics': 'tolerance'}, 'independence': {'implementation': True}}
        pv = self.call('post', '/api/v1/verification/assurance/evaluate', json={'job_id': j['id'], 'requirement': whole})[1]
        fin = self.call('post', '/api/v1/verification/assurance/evaluate', json={'job_id': j['id'], 'requirement': dict(whole, claim_type='final_transition_reference', coverage='final_transition')})[1]
        vs = self.call('get', '/api/v1/verification/' + v['id'])[1]
        ok = vs['state'] == 'passed' and pv['satisfied'] is False and fin['satisfied'] is True
        self.rec(15, 'a final-transition heat audit (passed) cannot establish an entire-trajectory claim; it satisfies only its own claim', ok, {'audit': vs['state'], 'whole_trajectory': pv['status'], 'final_transition': fin['status']}, t0=t0)

    def j16(self, t0):
        if not RUNTIME:
            self.rec(16, 'no retry until pass', False, {}, blocked='compute interpreter unavailable', t0=t0); return
        from metacoin_service.tests.test_verification import corrupt_output, flip_first_outcome
        jid = self.temporal_batch_job(grid=[{'path': 'reserve', 'start': 0, 'stop': 2000, 'step': 100}, {'path': 'load_scale_percent', 'values': [50, 100]}])
        st, sp = self.call('post', '/api/v1/verification/sampling-plans', json={'job_id': jid, 'defect_fraction': '1/2', 'alpha': '1/1000', 'max_samples': 500}); assert st == 201, sp
        corrupt_output(self.inst, jid, flip_first_outcome)
        st, au = self.call('post', '/api/v1/verification/sampling-plans/' + sp['id'] + '/audit', json={}); self.run_workers(2, 'au2')
        vs = self.call('get', '/api/v1/verification/' + au['id'])[1]
        st2, again = self.call('post', '/api/v1/verification/sampling-plans/' + sp['id'] + '/audit', json={})
        st3, again2 = self.call('post', '/api/v1/verification', json={'job_id': jid, 'class': 'sampled_reference', 'params': {'sampling_plan_id': sp['id']}})
        ok = st2 == 409 and st3 == 409 and again['detail']['code'] == 'sampling_plan_attempt_policy'
        self.rec(16, 'an audit outcome cannot be hidden by retrying until a passing challenge appears: one bound attempt per plan, every attempt recorded', ok, {'first_attempt': vs['state'], 'retry': again['detail']['code'], 'attempts': len(self.call('get', '/api/v1/verification/sampling-plans/' + sp['id'])[1]['attempts'])},
                 caveat='the corrupted item is detected only when sampled; the attempt policy holds either way', t0=t0)

    # ---- 17-23: acquisition contracts, accounting, access, bundle ---------------------------------------------------------------------------
    def j17(self, t0):
        amounts = {}
        an, pl, m, a = self.acquire(diagnostic=2); self.deliver(a['id'], delivery(None, status='could_not_acquire', note='instrument offline')); dv, ev = self.complete(a['id'])
        st, d = self.decide(a['id']); amounts['ACQUISITION_FAILED'] = (ev['payment_class'], d.get('entitlement', {}) and d['entitlement']['amount'])
        an, pl, m, a = self.acquire(inputs=analysis_inputs(hypotheses=HOT3)); self.deliver(a['id'], delivery({'cell_1_sensor': ['99']})); dv, ev = self.complete(a['id']); st, d = self.decide(a['id']); amounts['DECISION_AMBIGUOUS'] = (ev['payment_class'], d['entitlement']['amount'])
        an, pl, m, a = self.acquire(); self.deliver(a['id'], delivery({'cell_1_sensor': ['500']})); dv, ev = self.complete(a['id']); st, d = self.decide(a['id']); amounts['MODEL_INCONSISTENT'] = (ev['payment_class'], d['entitlement']['amount'])
        an, pl, m, a = self.acquire(); self.deliver(a['id'], delivery({'cell_1_sensor': ['99']})); dv, ev = self.complete(a['id']); st, d = self.decide(a['id']); amounts['DECISION_RESOLVED'] = (ev['payment_class'], d['entitlement']['amount'])
        self.ctx['j17'] = (a['id'], d['entitlement']['id'], dv['job']['id'])
        an, pl, m, a = self.acquire(); self.deliver(a['id'], delivery({'cell_1_sensor': ['abc']})); dv, ev = self.complete(a['id']); st, d = self.decide(a['id'], decision='reject', reason='malformed'); amounts['DELIVERY_INVALID'] = (ev['payment_class'], None if not d.get('entitlement') else d['entitlement']['amount'])
        ok = amounts['ACQUISITION_FAILED'] == ('diagnostic', 2) and amounts['DECISION_AMBIGUOUS'] == ('complete', 3) and amounts['MODEL_INCONSISTENT'] == ('complete', 3) and amounts['DECISION_RESOLVED'] == ('complete', 3) and amounts['DELIVERY_INVALID'] == ('none', None)
        self.rec(17, 'valid negative / ambiguous / inconsistent / failed-acquisition deliveries receive exactly the payment the frozen terms define; an invalid delivery earns nothing', ok, {'amounts': {k: list(v) for k, v in amounts.items()}}, t0=t0)

    def j18(self, t0):
        aid, eid, jid = self.ctx['j17']
        i1 = self.call('post', '/api/v1/work/entitlements/%s/prepare' % eid, json={})[1]; i2 = self.call('post', '/api/v1/work/entitlements/%s/prepare' % eid, json={})[1]
        self.call('post', '/api/v1/work/intents/%s/authorize' % i1['id'], json={})
        s1 = self.call('post', '/api/v1/work/intents/%s/submit' % i1['id'], json={})[1]; st2, s2 = self.call('post', '/api/v1/work/intents/%s/submit' % i1['id'], json={})
        rep = self.call('post', '/api/v1/work/journal/replay', json={})[1]
        ents = [e for e in self.aview(aid)['entitlements'] if e['kind'] == 'provider']
        ok = i1['id'] == i2['id'] and s1['state'] == 'settled' and st2 in (200, 409) and rep['consistent'] and len(ents) == 1 and ents[0]['state'] == 'paid'
        self.rec(18, 'duplicate prepare/submit requests reconcile without another charge: one entitlement, one intent, journal consistent', ok, {'intent': i1['id'], 'second_submit': st2, 'consistent': rep['consistent'], 'provider_entitlements': len(ents)}, t0=t0)

    def j19(self, t0):
        st, r = self.call('post', '/api/v1/credentials', json={'operations': ['contract:create', 'contract:freeze', 'job:submit', 'job:read', 'job:read_private', 'contract:read'], 'expires_in_seconds': 3600}); assert st == 201, r
        S = {'Authorization': 'Bearer ' + r['token']}
        an = self.analyze(analysis_inputs(), h=S); pl = self.plan(an['job']['id'], 3, h=S)
        st_m, mat = self.call('post', '/api/v1/acquisition-plans/' + pl['id'] + '/materialize', h=S, json={})
        st_w, w = self.call('get', '/api/v1/decisions/%s/witnesses' % an['job']['id'], h=self.lab['h'])
        st_v, err = self.call('get', '/api/v1/decisions/j_doesnotexist/witnesses', role='viewer')
        ok = st_m == 403 and st_w == 403 and 'hot_cell' not in json.dumps(w) and 'hot_cell' not in json.dumps(err)
        self.rec(19, 'an analysis-only credential cannot purchase; another principal and unknown ids never leak private witnesses', ok, {'materialize': st_m, 'witnesses_other_principal': st_w}, t0=t0)

    def j20(self, t0):
        an, pl, m, a = self.acquire(); self.deliver(a['id'], delivery({'cell_1_sensor': ['99']}))
        self.call('post', '/api/v1/ops/faults', json={'fault': 'evidence_publication'})
        subprocess.run([PY, '-m', 'metacoin_service', '--home', str(self.inst.home), '--provider-mode', 'test-http', 'worker', '--once', '--name', 'w-j20-crash'], cwd=ROOT, env=self.env, capture_output=True, text=True, timeout=120)
        self.call('post', '/api/v1/ops/faults', json={'fault': 'evidence_publication', 'disarm': True})
        jid = self.aview(a['id'])['milestones'][0]['job_id']
        from metacoin_service.db import Database
        with Database(self.inst.settings.db_path).tx() as db:
            db.execute('UPDATE jobs SET lease_expires=1 WHERE id=?', (jid,))
        self.run_workers(2, 'j20r')
        with Database(self.inst.settings.db_path).read() as db:
            arts = db.execute("SELECT COUNT(*) FROM artifacts WHERE job_id=? AND kind='evidence_vault' AND deleted_at IS NULL", (jid,)).fetchone()[0]
            dels = db.execute('SELECT COUNT(*) FROM job_deliveries WHERE job_id=?', (jid,)).fetchone()[0]
            res = db.execute("SELECT COUNT(*) FROM budget_reservations WHERE ref_type='work_award' AND ref_id=?", (a['id'],)).fetchone()[0]
        dv = self.dview(jid)
        ok = dv['state'] == 'succeeded' and dv['projection']['conclusion'] == 'DECISION_RESOLVED' and arts == 1 and dels == 1 and res == 1
        self.rec(20, 'a crash at evidence publication during acquisition resumes without duplicate publication or a second budget reservation', ok, {'state': dv['state'], 'evidence_artifacts': arts, 'deliveries': dels, 'reservations': res}, t0=t0)

    def j21(self, t0):
        mk = lambda name, cs: self.call('post', '/api/v1/portfolios', json={'name': name, 'components': [{'component': c} for c in cs]})[1]
        rep_only = mk('replicas', ['byte_integrity', 'contract_binding', 'replica_same_code'])
        full = mk('full', ['byte_integrity', 'contract_binding', 'schema_units', 'invariants', 'itemwise_exact_full', 'heat_whole_trajectory', 'sampler_regeneration', 'quote_validity', 'witness_check'])
        st, suite = self.call('post', '/api/v1/portfolios/fault-suite', json={'name': 'faults', 'seed': 7, 'cases_per_family': 2, 'holdout_per_family': 1}); assert st == 201, suite
        st, camp = self.call('post', '/api/v1/portfolios/campaigns', json={'suite_id': suite['id'], 'portfolio_ids': [rep_only['id'], full['id']], 'split': 'dev'}); assert st == 201, camp
        rep = camp['report']; F = rep['portfolios'][full['id']]
        ok = 'shared_wrong_model' in rep['common_mode']['families_no_portfolio_caught'] and F['by_family']['shared_wrong_model']['false_acceptance'] > 0 and 'measurement' in rep['common_mode']['reading']
        self.ctx['suite'] = suite['id']; self.ctx['full'] = full['id']
        self.rec(21, 'a common-model defect survives identical replicas and the full portfolio alike, and is reported as residual risk with a recommendation for a different evidence source', ok, {'uncaught_families': rep['common_mode']['families_no_portfolio_caught'], 'full_false_acceptance': F['totals']['false_acceptance']}, t0=t0)

    def j22(self, t0):
        st, rs = self.call('post', '/api/v1/portfolios/fault-suite', json={'name': 'rep', 'seed': 3, 'cases_per_family': 3, 'holdout_per_family': 0, 'families': ['single_item_corrupt', 'burst_corrupt', 'clean'], 'representative_draws': True}); assert st == 201, rs
        c2 = self.call('post', '/api/v1/portfolios/campaigns', json={'suite_id': rs['id'], 'portfolio_ids': [self.ctx['full']], 'split': 'dev'})[1]['report']['portfolios'][self.ctx['full']]
        ok = c2['totals']['false_acceptance'] == 0 and c2['statistical']['applicable'] and c2['statistical']['false_acceptance_upper_95'] > 0 and 'not a zero-error claim' in c2['statistical']['zero_failure_note']
        self.rec(22, 'a zero-failure campaign on a representative suite retains a nonzero exact one-sided bound', ok, {'false_acceptance': c2['totals']['false_acceptance'], 'upper_95': c2['statistical']['false_acceptance_upper_95']}, t0=t0)

    def j23(self, t0):
        aid, eid, jid = self.ctx['j17']
        pub = self.call('get', '/api/v1/verification/' + self.call('get', '/api/v1/verification?job_id=' + jid)[1]['items'][0]['id'] + '/statement')[1]['public_key_hex']
        st_v, _ = self.call('get', '/api/v1/decisions/%s/bundle' % jid, role='viewer')
        z = self.http.get('/api/v1/decisions/%s/bundle' % jid, headers=self.H())
        tmp = tempfile.mkdtemp(); p = Path(tmp) / 'bundle.zip'; p.write_bytes(z.content)
        env = {'PATH': os.environ.get('PATH', ''), 'PYTHONPATH': str(ROOT), 'HOME': tmp}
        out = subprocess.run([sys.executable, '-m', 'metacoin_service.decision.replay', str(p), '--trust-root', pub, '--json'], cwd=tmp, env=env, capture_output=True, text=True, timeout=180)
        rep = json.loads(out.stdout) if out.stdout else {}
        names = zipfile.ZipFile(p).namelist()
        ok = st_v == 403 and out.returncode == 0 and rep['integrity']['ok'] and rep['signatures']['trusted'] and rep['scientific_replay']['ok'] and not any('ed25519' in n or 'credential' in n for n in names)
        self.ctx['bundle'] = str(p); self.ctx['pub'] = pub
        self.rec(23, 'an authorized bundle replays decision witnesses and sampling calculations in a clean process without service secrets', ok, {'members': len(names), 'integrity': rep.get('integrity', {}).get('ok'), 'trusted': rep.get('signatures', {}).get('trusted'), 'science': rep.get('scientific_replay', {}).get('ok')}, t0=t0)

    def j24(self, t0):
        self.rec(24, 'browser journey', False, {'note': 'recorded by the --shots run (Playwright) as "browser" in this file'}, blocked='run with --shots', t0=t0)

    def j25(self, t0):
        t, f, r, o, a = self.flow('INFEASIBLE', 'J25L'); self.run_workers(2, 'leg'); self.verify(a['id']); ev = self.evaluate(a['id']); st, d = self.decide(a['id'])
        insp = self.call('get', '/api/v1/work/terms/' + t['id'] + '/inspect')[1]
        recs = self.call('get', '/api/v1/work/awards/' + a['id'] + '/receipts')[1]['items']
        ver = self.call('post', '/api/v1/work/receipts/' + recs[0]['id'] + '/verify', json={})[1]
        ok = d['decision'] == 'accepted' and ev['policy_schema'] in ('metacoin-acceptance-policy/v1', 'metacoin-acceptance-policy/v2') and ev['policy_semantics'] and ver.get('signature_valid', ver.get('valid', True)) is not False
        self.rec(25, 'existing legacy work contracts (determination template) and signed evidence remain readable, with their policy semantics named explicitly', ok, {'policy_schema': ev['policy_schema'], 'semantics': ev['policy_semantics'][:60], 'receipts': len(recs)}, t0=t0)

    def j26(self, t0):
        self.rec(26, 'clean exported tree reproduces the journey', False, {'note': 'recorded by clean_export.sh after the archive exists'}, blocked='runs after packaging', t0=t0)

    def browser(self, outdir):
        script = ROOT / 'metacoin_service' / 'tests' / 'browser' / 'journey_decision.py'
        pw = os.environ.get('METACOIN_PLAYWRIGHT_PYTHON')
        if not pw or not script.exists():
            return {'status': 'blocked', 'reason': 'METACOIN_PLAYWRIGHT_PYTHON not set or browser script missing'}
        self.run_workers(0, 'browser-drain'); bg = self.worker_bg('w-browser')
        p = subprocess.run([pw, str(script), self.base, str(self.creds['owner']), str(self.lab['cred']), str(self.creds['viewer']), outdir], cwd=ROOT, env=dict(self.env, PLAYWRIGHT_BROWSERS_PATH=os.environ.get('PLAYWRIGHT_BROWSERS_PATH', '')), capture_output=True, text=True, timeout=900)
        try:
            out = json.loads(p.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            out = {'stderr': p.stderr[-600:], 'stdout': p.stdout[-300:]}
        self.stop_workers()
        return dict(out, rc=p.returncode, status='passed' if p.returncode == 0 and out.get('failed', 1) == 0 else 'failed')

    def run_all(self, only=None):
        for n in range(1, 27):
            if only and n not in only:
                continue
            fn = getattr(self, 'j%d' % n)
            self.guard(n, 'journey %d' % n, fn)
        return self.results


def main():
    p = argparse.ArgumentParser(); p.add_argument('--out'); p.add_argument('--only'); p.add_argument('--shots'); a = p.parse_args()
    j = Journeys(); browser = None
    try:
        results = j.run_all([int(x) for x in a.only.split(',')] if a.only else None); health = j.http.get('/api/health').json()
        if a.shots:
            browser = j.browser(a.shots)
            for r in results:
                if r['journey'] == 24:
                    r.update(status=browser.get('status', 'failed'), evidence={'checks': browser.get('passed'), 'failed': browser.get('failed'), 'screenshots': browser.get('screenshots')}, caveat=None)
    finally:
        j.close()
    out = {'schema': 'metacoin-journeys-decision/v1', 'provider_mode': 'test-http', 'revision': health.get('revision'), 'dependencies': {'compute_interpreter': bool(RUNTIME), 'torch': HAVE_TORCH, 'python': sys.version.split()[0], 'local_chain': bool(j.acct)},
           'started_at': j.started_at, 'finished_at': now(), 'results': results, 'browser': browser, 'passed': sum(r['status'] == 'passed' for r in results), 'failed': sum(r['status'] in ('failed', 'error') for r in results),
           'blocked': sum(r['status'] == 'blocked' for r in results), 'total': len(results)}
    text = json.dumps(out, indent=1, default=str)
    if a.out:
        Path(a.out).write_text(text)
    print(text[-1200:])
    return 0 if out['failed'] == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
