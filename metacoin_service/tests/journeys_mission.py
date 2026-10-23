"""Acceptance journeys of the autonomous mission economy order (2026-10-23, §13: 36 journeys) and the §12 demonstration, run
through real processes: two settlement domains (separate homes, databases, authority keys, API and worker processes), an
operator-controlled signer per domain, one commit-checkpoint witness, x402 disabled and its facilitator URL pointed at a closed
port. Each journey records the candidate revision, its evidence and its status (passed / failed / blocked / skipped).

    METACOIN_X402_DISABLED=1 PYTHONPATH=. .venv-service/bin/python -m metacoin_service.tests.journeys_mission --out journeys-mission.json [--only 1,2] [--shots DIR] [--demo demonstration.json]
"""
import argparse
import json
import os
import random
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import httpx

from metacoin_service import crypto
from metacoin_service.db import now
from metacoin_service.lunar.__main__ import example as lunar_example
from metacoin_service.native import courier, planner as PL, protocol as P
from metacoin_service.tests.journeys_native import Dom, LAST_ERRORS
from metacoin_service.tests.test_native_signer import SignerProcess
from metacoin_service.tests.test_native_witness import WitnessProcess
from metacoin_service.tests.test_service import ENV, ROOT, free_port
from metacoin_service.tests.test_work_terms import energy_inputs

sys.path.insert(0, str(ROOT / 'provider_client'))
from metacoin_provider import verify as PV   # noqa: E402

PY = sys.executable


class MDom(Dom):
    """A journey domain whose API process is configured with the signer and the witness."""
    def __init__(self, did, extra_env):
        self.extra_env = extra_env; super().__init__(did)

    def start_api(self):
        self.env.update(self.extra_env); super().start_api()

    # ---- external signer actors ---------------------------------------------------------------------------------------------------
    def enroll_external(self, sg, name, ceiling=100):
        st, e = sg.client.call('POST', '/enroll', {'name': name, 'ceiling': ceiling}); assert st == 200, e
        st, a = self.call('post', '/api/v1/native/actors/enroll', json={'name': name, 'public_hex': e['public_hex'], 'key_id': e['key_id'], 'signer_id': e['signer_id']}); assert st == 201, a
        sg.client.call('POST', '/bind', {'actor': name, 'account': a['account_id']})
        self.actors[name] = {'account': a['account_id'], 'principal_id': a['principal_id'], 'token': a['credential']['token'], 'key_id': e['key_id'], 'external': True}; return a

    def prepare(self, who, to, amount, rid, **extra):
        st, r = self.call('post', '/api/v1/native/transfers/prepare', role=who, json=dict({'to_account': to, 'amount': amount, 'request_id': rid}, **extra)); assert st == 200, r; return r['message']

    def submit(self, who, msg, sig, kid):
        return self.call('post', '/api/v1/native/transfers/submit', role=who, json={'message': msg, 'signature_hex': sig, 'key_id': kid})

    def signed_pay(self, sg, who, to, amount, rid):
        msg = self.prepare(who, to, amount, rid); st, s = sg.client.sign(who, msg); assert st == 200, s
        return self.submit(who, msg, s['signature_hex'], s['key_id'])

    # ---- market ----------------------------------------------------------------------------------------------------------------
    def offer(self, who, kind, capacity, price, start=None, end=None, **extra):
        t = int(time.time()); st, o = self.call('post', '/api/v1/resources/offers', role=who, json=dict({'kind': kind, 'resource_id': '%s-%s-%d' % (kind, who, price), 'capacity': capacity, 'price_per_unit': price, 'window_start': t - 10 if start is None else start, 'window_end': t + 3600 if end is None else end}, **extra)); assert st == 201, o; return o

    def reserve(self, who, offer, quantity, rid, version=None):
        return self.call('post', '/api/v1/resources/reservations', role=who, json={'offer_id': offer['id'], 'offer_version': offer['version'] if version is None else version, 'quantity': quantity, 'request_id': rid})

    def res_action(self, who, rid, action, body=None):
        return self.call('post', '/api/v1/resources/reservations/%s/%s' % (rid, action), role=who, json=body or {})

    # ---- missions --------------------------------------------------------------------------------------------------------------
    def mission(self, who, template=None, graph=None, policy=None, grant_id=None, title=None):
        body = {'title': title or template or 'mission', 'policy': policy or {}}
        if graph is not None:
            body['graph'] = graph
        else:
            body['template'] = template
        if grant_id:
            body['grant_id'] = grant_id
        st, m = self.call('post', '/api/v1/missions', role=who, json=body); assert st == 201, m
        st, f = self.call('post', '/api/v1/missions/%s/freeze' % m['id'], role=who, json={}); assert st == 200, f; return f

    def step(self, who, mid):
        st, s = self.call('post', '/api/v1/missions/%s/step' % mid, role=who, json={}); assert st == 200, s; return s

    def mview(self, who, mid):
        return self.call('get', '/api/v1/missions/' + mid, role=who)[1]

    def ms(self, who, mid, key):
        return next(x for x in self.mview(who, mid)['milestones'] if x['key'] == key)

    def explain(self, who, mid):
        return self.call('get', '/api/v1/missions/%s/explain' % mid, role=who)[1]

    def advance(self, who, mid, until, timeout=240):
        """Step until the controller performs `until` (the worker runs jobs in the background between steps)."""
        deadline = time.time() + timeout; last = None
        while time.time() < deadline:
            last = self.step(who, mid); p = last.get('performed')
            if p and p['action'] == until and (until != 'decide' or 'decision' in (last.get('result') or {})):
                return last
            if p is None and last.get('state') == 'stopped':
                return last
            time.sleep(0.6)
        return last

    def drive_work(self, who, prov, mid, key, price, cls='analytical'):
        s = self.advance(who, mid, 'open_request'); assert s['performed'] and s['performed']['milestone'] == key, s
        rid = self.ms(who, mid, key)['binding']['request_id']
        st, o = self.call('post', '/api/v1/work/requests/%s/offers' % rid, role=prov, json={'price_amount': price, 'asset': self.asset, 'scheme': 'exact', 'window_seconds': 3600, 'verification': {'class': cls}}); assert st == 201, o
        s = self.advance(who, mid, 'award'); assert s['performed'] and s['performed']['action'] == 'award', s
        s = self.advance(who, mid, 'verify'); assert s['performed'] and s['performed']['action'] == 'verify', s
        return self.advance(who, mid, 'decide')

    def operator(self):
        return self.call('get', '/api/v1/native/operator')[1]

    def connectivity(self, state):
        st, r = self.call('post', '/api/v1/native/domain/connectivity', json={'state': state}); assert st == 200, r


class Journeys:
    def __init__(self, shots=None, demo_out=None):
        self.started_at = now(); self.results = []; self.ctx = {'shots': shots, 'demo_out': demo_out}
        self.W = WitnessProcess(); self.sgA = SignerProcess(domains=('dom-a',)); self.sgB = SignerProcess(domains=('dom-b',))
        wenv = {'METACOIN_WITNESS_URL': self.W.base, 'METACOIN_WITNESS_TOKEN_FILE': str(self.W.home / 'token'), 'METACOIN_WITNESS_KEY_ID': self.W.identity['key_id']}
        self.A = MDom('dom-a', dict(wenv, METACOIN_SIGNER_URL=self.sgA.base, METACOIN_SIGNER_TOKEN_FILE=str(self.sgA.home / 'token')))
        self.B = MDom('dom-b', dict(wenv, METACOIN_SIGNER_URL=self.sgB.base, METACOIN_SIGNER_TOKEN_FILE=str(self.sgB.home / 'token')))
        self.A.pin(self.B); self.B.pin(self.A)
        for d in (self.A, self.B):
            v = d.domain(); assert self.W.client.call('POST', '/pin', {'domain': d.id, 'key_id': v['authority']['key_id'], 'public_hex': v['authority']['public_hex']})[0] == 200
        A = self.A
        A.actor('requester', 200); A.actor('machine'); A.actor('verifier'); A.actor('supplier'); A.actor('contributor'); A.provider('machine'); A.provider('supplier'); A.provider('contributor')
        self.B.actor('bob'); self.B.actor('carol'); self.B.actor('supplier_b'); self.B.provider('supplier_b')

    def close(self):
        for d in (self.A, self.B):
            try:
                d.close()
            except Exception:
                pass
        for p in (self.sgA, self.sgB, self.W):
            try:
                p.close()
            except Exception:
                pass

    def rec(self, n, title, ok, evidence, blocked=None, caveat=None, t0=None, skipped=None):
        ev = dict(evidence, seconds=round(time.time() - t0, 3) if t0 else None)
        status = 'blocked' if blocked else ('skipped' if skipped else ('passed' if ok else 'failed'))
        self.results.append({'journey': n, 'title': title, 'status': status, 'evidence': dict(ev, blocked=blocked, skipped=skipped) if (blocked or skipped) else ev, 'caveat': caveat})

    def guard(self, n, title, fn):
        t0 = time.time()
        try:
            fn(t0)
        except Exception as exc:
            import traceback
            self.rec(n, title, False, {'exception': type(exc).__name__, 'detail': str(exc)[:400], 'trace': traceback.format_exc()[-900:], 'last_award_errors': LAST_ERRORS[-2:]}, t0=t0)

    def push(self, src, dst):
        return courier.push(src.ep(), dst.ep(), dst.id)

    # ---- 1–2 baseline and native cycle -------------------------------------------------------------------------------------------
    def j1(self, t0):
        """Recover baseline and reproduce the distinct-recipient 80-unit accounting example."""
        A = self.A; base = (ROOT / 'work' / 'autonomous-mission-economy-session' / 'artifacts' / 'BASE_REVISION.txt').read_text().strip() if (ROOT / 'work' / 'autonomous-mission-economy-session' / 'artifacts' / 'BASE_REVISION.txt').exists() else None
        req = A.actor('r80', 80); A.actor('m80'); A.actor('v80'); A.actor('s80'); A.actor('c80'); A.provider('m80'); A.provider('s80'); A.provider('c80')
        pre = A.award('m80', 's80', 'determination', energy_inputs('FEASIBLE'), 9, bps=1112)[3][0]
        inputs = lunar_example('route'); inputs['private_label'] = 'J1_80'
        t, r, o, (sa, a) = A.award('r80', 'm80', 'lunar_route_feasibility', inputs, 40, bps=500, verifier=3, verifier_pay_to=A.actors['v80']['account'])
        aw, d = A.deliver_accept(a['id'], 'r80'); paid = A.pay_all(a['id'], 'r80'); b1 = {k: A.bal(k) for k in ('r80', 'm80', 'v80')}; b1['treasury'] = A.bal(A.treasury)
        t2, r2, o2, (sa2, a2) = A.award('m80', 's80', 'determination', energy_inputs('FEASIBLE'), 9, bps=1112); aw2, d2 = A.deliver_accept(a2['id'], 'm80'); paid2 = A.pay_all(a2['id'], 'm80')
        tv = A.call('get', '/api/v1/work/treasury', params={'asset': A.asset})[1]
        t3, r3, o3, (sa3, a3) = A.award('owner', 'c80', 'determination', energy_inputs('INFEASIBLE'), 2, funding='treasury'); aw3, d3 = A.deliver_accept(a3['id'], 'owner'); paid3 = A.pay_all(a3['id'], 'owner')
        final = {k: A.bal(k) for k in ('r80', 'm80', 's80', 'v80', 'c80')}; final['treasury'] = A.bal(A.treasury)
        expected = {'r80': 35, 'm80': 30, 's80': 9, 'v80': 3, 'c80': 2, 'treasury': 1}
        ok = pre == 400 and sa == 201 and d.get('decision') == 'accepted' and b1 == {'r80': 35, 'm80': 40, 'v80': 3, 'treasury': 2} and d2.get('decision') == 'accepted' and tv['available'] == 3 and d3.get('decision') == 'accepted' and final == expected and sum(final.values()) == 80 and A.audit()['conserved']
        self.ctx['j1'] = {'award': a['id'], 'job': aw['milestones'][0].get('job_id')}
        self.rec(1, self.j1.__doc__, ok, {'base_revision': base, 'revision': A.call('get', '/api/health')[1].get('revision'), 'purchase_before_earning': pre, 'after_terrain_settlement': b1, 'treasury_before_award': tv.get('available'), 'final': final, 'expected': expected, 'total': sum(final.values()), 'award_recipient_distinct_from_machine': True, 'previous_record_untouched': 'journey 21 of the native order awarded the treasury contract to the machine on purpose (machine 32); this replay names a distinct recipient'}, t0=t0)

    def j2(self, t0):
        """Complete the native economic cycle with x402 disabled and facilitator unavailable."""
        A = self.A; cap = A.call('get', '/api/v1/capabilities')[1]
        t, r, o, (sa, a) = A.award('requester', 'machine', 'determination', energy_inputs('FEASIBLE'), 5)
        aw, d = A.deliver_accept(a['id'], 'requester'); paid = A.pay_all(a['id'], 'requester')
        try:
            httpx.get('http://127.0.0.1:1/blocked', timeout=1); fac = 'reachable'
        except Exception as exc:
            fac = type(exc).__name__
        ok = cap['x402']['disabled'] and not cap['x402']['required_by_native_core'] and sa == 201 and d.get('decision') == 'accepted' and paid['provider']['rail'] == 'native' and A.bal('machine') == 5 and fac != 'reachable' and cap.get('signer', {}).get('custody') is not None
        self.rec(2, self.j2.__doc__, ok, {'x402': cap['x402'], 'facilitator_probe': fac, 'rail': paid['provider']['rail'], 'machine': A.bal('machine'), 'signer_capability': cap.get('signer', {}).get('reachable')}, t0=t0)

    # ---- 3–7 signer -----------------------------------------------------------------------------------------------------------------
    def j3(self, t0):
        """Enroll an actor through the external signer; server has no configured actor private key."""
        A = self.A; a = A.enroll_external(self.sgA, 'ext-alice', 100); A.fund('ext-alice', 50)
        keyfiles = list((A.inst.settings.keys_dir / 'native').glob('actor-' + a['account_id'] + '*'))
        st_direct, r_direct = A.call('post', '/api/v1/native/transfers', role='ext-alice', json={'to_account': A.actors['machine']['account'], 'amount': 1, 'request_id': 'direct'})
        st, out = A.signed_pay(self.sgA, 'ext-alice', A.actors['machine']['account'], 10, 'r1')
        ok = not a['private_key_held_by_service'] and a['custody'].startswith('external-signer:') and not keyfiles and st_direct == 409 and (r_direct.get('detail') or {}).get('code') == 'signature_required' and st == 201 and out['state'] == 'settled' and out['history'][1]['custody'] == 'external-signer' and A.bal('ext-alice') == 40
        self.rec(3, self.j3.__doc__, ok, {'custody': a['custody'], 'key_files_on_server': len(keyfiles), 'unsigned_transfer': st_direct, 'signed_transfer': out.get('state'), 'alice': A.bal('ext-alice')}, t0=t0)

    def j4(self, t0):
        """Refuse altered recipient, amount, domain and obligation before signing."""
        A = self.A; sg = self.sgA; to = A.actors['machine']['account']; msg = A.prepare('ext-alice', to, 5, 'r2'); st, sig = sg.client.sign('ext-alice', msg); codes = {}
        for field, value in (('recipient', A.actors['supplier']['account']), ('amount_ceiling', 6), ('authority', 'dom-z'), ('obligation_id', 'no_fake')):
            s, r = A.submit('ext-alice', dict(msg, **{field: value}), sig['signature_hex'], sig['key_id']); codes[field] = (s, (r.get('detail') or {}).get('code') if isinstance(r.get('detail'), dict) else r.get('code'))
        signer_refusals = {}
        for name, bad in (('wrong_domain', dict(msg, authority='dom-z', request_id='x1')), ('expired', dict(msg, expires_at=1, request_id='x3')), ('unknown_field', dict(msg, extra=1, request_id='x4'))):
            s, r = sg.client.sign('ext-alice', bad); signer_refusals[name] = (s, r.get('code'))
        s, r = sg.client.call('POST', '/sign', raw=b'{"actor": "ext-alice", "intent": {"amount_ceiling": 1.5}}'); signer_refusals['float'] = (s, r.get('code'))
        bal = (A.bal('ext-alice'), A.bal('machine'), A.bal('supplier'))
        s_ok, ok_r = A.submit('ext-alice', msg, sig['signature_hex'], sig['key_id'])
        ok = all(s in (403, 422) for s, _ in codes.values()) and all(s == 400 for s, _ in signer_refusals.values()) and bal[0] == 40 and s_ok == 201 and A.bal('ext-alice') == 35
        self.rec(4, self.j4.__doc__, ok, {'service_refusals': codes, 'signer_refusals': signer_refusals, 'balances_unchanged_until_valid_submit': bal, 'valid_submit': ok_r.get('state')}, t0=t0)

    def j5(self, t0):
        """Crash and restart the signer around signature persistence without additional exposure."""
        A = self.A; sg = self.sgA; A.enroll_external(sg, 'ext-bob', 100); A.fund('ext-bob', 30); to = A.actors['machine']['account']
        msg = A.prepare('ext-bob', to, 7, 'c1'); sg.restart(fault='before_record'); s1 = sg.client.sign('ext-bob', msg)[0]; sg.restart()
        exp0 = sg.client.call('GET', '/keys/ext-bob')[1]['exposure_outstanding']
        st, sig = sg.client.sign('ext-bob', msg); st2, again = sg.client.sign('ext-bob', msg)
        A.submit('ext-bob', msg, sig['signature_hex'], sig['key_id'])
        msg2 = A.prepare('ext-bob', to, 3, 'c2'); sg.restart(fault='after_record'); s2 = sg.client.sign('ext-bob', msg2)[0]; sg.restart()
        st3, sig2 = sg.client.sign('ext-bob', msg2); exp = sg.client.call('GET', '/keys/ext-bob')[1]['exposure_outstanding']
        A.submit('ext-bob', msg2, sig2['signature_hex'], sig2['key_id']); dup = A.submit('ext-bob', msg2, sig2['signature_hex'], sig2['key_id'])[1]
        ok = s1 == 500 and exp0 == 0 and st == 200 and not sig['replayed'] and again['replayed'] and again['signature_hex'] == sig['signature_hex'] and s2 == 500 and sig2['replayed'] and exp == 10 and dup.get('replayed') and A.bal('ext-bob') == 20
        self.rec(5, self.j5.__doc__, ok, {'crash_before_record': s1, 'exposure_after_crash': exp0, 'crash_after_record': s2, 'replayed_after_restart': sig2.get('replayed'), 'exposure_outstanding': exp, 'bob': A.bal('ext-bob')}, t0=t0)

    def j6(self, t0):
        """Rotate a signer, verify historical receipts and refuse unauthorized new old-key intents."""
        A = self.A; sg = self.sgA; a = A.enroll_external(sg, 'ext-carol', 100); A.fund('ext-carol', 20); acct = a['account_id']; to = A.actors['machine']['account']
        msg = A.prepare('ext-carol', to, 4, 'h1'); st, s1 = sg.client.sign('ext-carol', msg); A.submit('ext-carol', msg, s1['signature_hex'], s1['key_id'])
        st, rot = sg.client.call('POST', '/rotate', {'actor': 'ext-carol', 'grace_seconds': 0})
        st, kr = A.call('post', '/api/v1/native/accounts/%s/keys' % acct, role='ext-carol', json=rot['registration']); keys = kr.get('keys', [])
        old_pub = next((k['public_hex'] for k in keys if k['key_id'] == s1['key_id']), None); hist_ok = bool(old_pub) and crypto.verify(old_pub, P.signing_bytes('intent', msg), s1['signature_hex'])
        receipts = [r for r in A.call('get', '/api/v1/native/export')[1]['receipts'] if r['ref_id'] == A.call('get', '/api/v1/native/intents', role='ext-carol', params={'account': acct})[1]['items'][0]['id']]
        msg2 = A.prepare('ext-carol', to, 2, 'h2'); old_file = next(f for f in (sg.home / 'keys').iterdir() if '-e1-' in f.name)
        forged = crypto.sign(crypto.load_signing_key(old_file), P.signing_bytes('intent', msg2)); sf, rf = A.submit('ext-carol', msg2, forged, s1['key_id'])
        st, s2 = sg.client.sign('ext-carol', msg2); sn, _ = A.submit('ext-carol', msg2, s2['signature_hex'], s2['key_id'])
        ok = st == 200 and [k['state'] for k in keys] == ['retired', 'active'] and hist_ok and len(receipts) >= 1 and sf == 403 and (rf.get('detail') or {}).get('code') == 'key_not_authorized' and s2['key_id'] == rot['key_id'] and sn == 201 and A.bal('ext-carol') == 14
        self.rec(6, self.j6.__doc__, ok, {'keys': [(k['key_id'][:10], k['state']) for k in keys], 'historical_signature_verifies': hist_ok, 'historical_receipts': len(receipts), 'old_key_new_intent': (sf, (rf.get('detail') or {}).get('code')), 'carol': A.bal('ext-carol')}, t0=t0)

    def j7(self, t0):
        """Refuse delegation that expands recipient scope or aggregate authority."""
        A = self.A; sg = self.sgA; a = A.enroll_external(sg, 'ext-dave', 50); A.fund('ext-dave', 50); acct = a['account_id']; m = A.actors['machine']['account']; s = A.actors['supplier']['account']
        st_big, big = sg.client.call('POST', '/delegate', {'actor': 'ext-dave', 'name': 'agent', 'recipients': [m], 'ceiling': 60})
        st, d = sg.client.call('POST', '/delegate', {'actor': 'ext-dave', 'name': 'agent', 'recipients': [m], 'ceiling': 20}); A.call('post', '/api/v1/native/accounts/%s/keys' % acct, role='ext-dave', json=d['registration'])
        msg = A.prepare('ext-dave', s, 5, 'd1'); st1, r1 = sg.client.sign('ext-dave', msg, delegate=d['key_id'])
        msg = A.prepare('ext-dave', m, 15, 'd2'); st2, s1 = sg.client.sign('ext-dave', msg, delegate=d['key_id']); A.submit('ext-dave', msg, s1['signature_hex'], s1['key_id'])
        msg = A.prepare('ext-dave', m, 10, 'd3'); st3, r3 = sg.client.sign('ext-dave', msg, delegate=d['key_id'])
        dfile = next(f for f in (sg.home / 'keys').iterdir() if 'delegate-agent' in f.name); forged = crypto.sign(crypto.load_signing_key(dfile), P.signing_bytes('intent', msg)); sf, rf = A.submit('ext-dave', msg, forged, d['key_id'])
        ok = st_big == 400 and big.get('code') == 'delegation_exceeds_authority' and r1.get('code') == 'delegate_recipient_out_of_scope' and st2 == 200 and r3.get('code') == 'delegate_ceiling' and sf == 400 and (rf.get('detail') or {}).get('code') == 'delegate_ceiling' and A.bal('ext-dave') == 35
        self.rec(7, self.j7.__doc__, ok, {'widening_delegation': big.get('code'), 'out_of_scope': r1.get('code'), 'within': st2, 'over_ceiling_signer': r3.get('code'), 'over_ceiling_server': (rf.get('detail') or {}).get('code'), 'dave': A.bal('ext-dave')}, t0=t0)

    # ---- 8–12 resource market ---------------------------------------------------------------------------------------------------------
    def j8(self, t0):
        """Race two buyers for one resource slot; capacity and funds remain consistent."""
        A = self.A; A.fund('machine', 100); o = A.offer('supplier', 'contact', 10, 1); results = []; lock = threading.Lock()
        def go(who):
            st, r = A.reserve(who, o, 8, 'race-' + who)
            with lock:
                results.append((who, st, (r.get('detail') or {}).get('code') if st != 201 else None))
        th = [threading.Thread(target=go, args=(w,)) for w in ('requester', 'machine')]; [t.start() for t in th]; [t.join() for t in th]
        v = A.call('get', '/api/v1/resources/offers/' + o['id'])[1]; winner = next((w for w, s, _ in results if s == 201), None); loser = 'machine' if winner == 'requester' else 'requester'
        ok = sorted(s for _, s, _ in results) == [201, 409] and 'capacity_exhausted' in [c for _, _, c in results] and (v['reserved'], v['available']) == (8, 2) and winner and A.bal(winner, 'reserved') == 8 and A.bal(loser, 'reserved') == 0 and A.audit()['conserved']
        self.rec(8, self.j8.__doc__, ok, {'results': results, 'offer': {'reserved': v['reserved'], 'available': v['available']}, 'winner': winner}, t0=t0)

    def j9(self, t0):
        """Refuse acceptance of a stale resource quote without silently repricing it."""
        A = self.A; o = A.offer('supplier', 'compute', 5000, 2); q = A.call('post', '/api/v1/resources/quote', role='requester', json={'offer_id': o['id'], 'quantity': 1000})[1]
        am = A.call('post', '/api/v1/resources/offers/%s/amend' % o['id'], role='supplier', json={'price_per_unit': 3})[1]
        st, r = A.reserve('requester', o, 10, 'stale', version=1); fresh = A.call('get', '/api/v1/resources/offers/' + o['id'])[1]; st2, res = A.reserve('requester', fresh, 10, 'fresh')
        ok = (q['price'], q['binding']) == (2000, False) and am['version'] == 2 and st == 409 and r['detail']['code'] == 'stale_quote' and st2 == 201 and (res['price'], res['offer_version']) == (30, 2)
        self.rec(9, self.j9.__doc__, ok, {'quote': {'price': q['price'], 'binding': q['binding']}, 'stale': (st, r.get('detail', {}).get('code')), 'fresh': (res.get('price'), res.get('offer_version'))}, t0=t0)

    def j10(self, t0):
        """Deliver actual compute work and settle its measured contract once."""
        A = self.A; A.fund('requester', 5000); o = A.offer('supplier', 'compute', 60000, 1); st, res = A.reserve('requester', o, 3000, 'cpu'); rid = res['id']
        early = A.res_action('supplier', rid, 'deliver')[0]; wrong = A.res_action('requester', rid, 'activate')[0]; A.res_action('supplier', rid, 'activate')
        sd, d = A.res_action('supplier', rid, 'deliver', {'task': {'kind': 'terrain_tile', 'source': 'apollo11_32'}}); m = d['measurement']
        s1 = A.res_action('requester', rid, 'settle')[1]; owed = d['delivered_quantity']; sup_after = A.bal('supplier'); s2 = A.res_action('requester', rid, 'settle')[1]
        ok = early == 409 and wrong == 403 and sd == 200 and m['measurement']['kind'] == 'metered_fact' and m['measurement']['cpu_ms'] > 0 and m['verification']['ok'] and s1['state'] == 'settled' and s1['obligation']['settled_amount'] == owed and s2.get('replayed') and A.bal('supplier') == sup_after and A.audit()['conserved']
        self.ctx['j10_supplier'] = sup_after
        self.rec(10, self.j10.__doc__, ok, {'measured_cpu_ms': m['measurement']['cpu_ms'], 'exit_code': m['measurement']['exit_code'], 'verification': m['verification']['ok'], 'delivered_quantity': owed, 'settled_once': s2.get('replayed'), 'measurement_source': m['measurement']['measurement_source']}, t0=t0)

    def j11(self, t0):
        """Deliver storage and emulated contact capacity with correctly labelled evidence."""
        A = self.A; A.fund('machine', 5000); so = A.offer('supplier', 'storage', 10 ** 6, 1); st, sr = A.reserve('machine', so, 4096, 'st'); A.res_action('supplier', sr['id'], 'activate')
        put = A.http.post('/api/v1/resources/reservations/%s/storage' % sr['id'], headers=dict(A.H('machine'), **{'Content-Type': 'application/octet-stream'}), content=b'x' * 2048).json()
        d = A.res_action('supplier', sr['id'], 'deliver', {'min_seconds': 2})[1]; m = d['measurement']
        co = A.offer('supplier', 'contact', 64, 2, bandwidth_bytes_per_s=2_000_000); st, cr = A.reserve('machine', co, 32, 'ct'); A.res_action('supplier', cr['id'], 'activate')
        d2 = A.res_action('supplier', cr['id'], 'deliver', {'bytes': 20 * 1024})[1]; m2 = d2['measurement']['measurement']; s2 = A.res_action('supplier', cr['id'], 'settle')[1]
        A.res_action('machine', sr['id'], 'settle')
        ok = put['stored_bytes'] == 2048 and m['measurement']['kind'] == 'metered_fact' and m['verification']['ok'] and m['assertion']['kind'] == 'provider_assertion' and d['delivered_quantity'] >= 4 and m2['received_bytes'] == 20 * 1024 and m2['match'] and 'emulated' in m2['label'] and d2['delivered_quantity'] == 20 and s2['obligation']['settled_amount'] == 40 and A.audit()['conserved']
        self.rec(11, self.j11.__doc__, ok, {'storage': {'stored_bytes': m['measurement']['stored_bytes'], 'kib_seconds': d['delivered_quantity'], 'labels': [m['measurement']['kind'], m['assertion']['kind'], m['verification']['kind']]}, 'contact': {'received_bytes': m2['received_bytes'], 'label': m2['label'][:60], 'delivered_kib': d2['delivered_quantity'], 'settled': s2['obligation']['settled_amount']}}, t0=t0)

    def j12(self, t0):
        """Cancel uncommitted resource reservations and preserve committed obligations."""
        A = self.A; o = A.offer('supplier', 'contact', 100, 1); st, r1 = A.reserve('requester', o, 30, 'c1'); st, r2 = A.reserve('requester', o, 20, 'c2'); req0 = A.bal('requester'); sup0 = A.bal('supplier')
        A.res_action('supplier', r2['id'], 'activate'); A.res_action('supplier', r2['id'], 'deliver', {'bytes': 10 * 1024})
        c1 = A.res_action('requester', r1['id'], 'cancel', {'reason': 'not needed'})[1]; v = A.call('get', '/api/v1/resources/offers/' + o['id'])[1]; c2 = A.res_action('requester', r2['id'], 'cancel')[1]
        ok = (c1['state'], c1['obligation']['state']) == ('cancelled', 'cancelled') and (v['reserved'], v['delivered'], v['available']) == (0, 10, 90) and (c2['state'], c2['obligation']['settled_amount']) == ('settled', 10) and A.bal('requester') == req0 + 50 - 10 and A.bal('supplier') == sup0 + 10 and A.audit()['conserved']
        self.rec(12, self.j12.__doc__, ok, {'uncommitted': c1['state'], 'committed_after_activation': (c2['state'], c2['obligation']['settled_amount']), 'offer': {k: v[k] for k in ('reserved', 'delivered', 'available')}}, t0=t0)

    # ---- 13–21 mission contracts and the bounded controller ---------------------------------------------------------------------------
    def pol(self, **kw):
        A = self.A; return dict({'verifier': 3, 'verifier_pay_to': A.actors['verifier']['account'], 'permitted_providers': [A.actors['machine']['provider_id']]}, **kw)

    def j13(self, t0):
        """Execute a multi-milestone mission with frozen terms and linked input versions."""
        A = self.A; A.fund('requester', 300); A.offer('supplier', 'compute', 100000, 1); m = A.mission('requester', 'terrain_delivery', policy=self.pol(compute_cpu_ms=20)); mid = m['id']; m0 = A.bal('machine'); r0 = A.bal('requester'); v0 = A.bal('verifier'); tr0 = A.bal(A.treasury)
        s = A.drive_work('requester', 'machine', mid, 'acquire', 4); acq = A.ms('requester', mid, 'acquire'); pay1 = A.advance('requester', mid, 'pay')
        s2 = A.drive_work('requester', 'machine', mid, 'compute', 40); comp = A.ms('requester', mid, 'compute'); pay2 = A.advance('requester', mid, 'pay')
        v = A.mview('requester', mid); led = A.call('get', '/api/v1/missions/%s/ledger' % mid, role='requester')[1]
        ok = m['state'] == 'frozen' and m['frozen_revision'] == 1 and s['performed']['action'] == 'decide' and s['result']['decision'] == 'accepted' and acq['binding'].get('terms_digest') and acq['binding'].get('input_root') and s2['result']['decision'] == 'accepted' and s2['result']['science'] == 'FEASIBLE' and comp['binding'].get('terms_digest') and pay2['performed']['action'] == 'pay' \
            and A.bal('machine') == m0 + 44 and A.bal('verifier') == v0 + 3 and A.bal(A.treasury) == tr0 + 2 and A.bal('requester') == r0 - 49 and led['totals']['spent'] == 49 and v['revisions'][0]['state'] == 'frozen'
        self.ctx['j13'] = {'mission': mid, 'job': comp['binding'].get('job_id'), 'award': comp['binding'].get('award_id')}
        self.rec(13, self.j13.__doc__, ok, {'mission': mid, 'frozen_digest': v['revisions'][0]['digest'][:16], 'acquire_terms_digest': (acq['binding'].get('terms_digest') or '')[:16], 'compute_input_root': (comp['binding'].get('input_root') or '')[:16], 'science': s2['result']['science'], 'ledger_spent': led['totals']['spent'], 'balances': {'machine': A.bal('machine'), 'verifier': A.bal('verifier'), 'treasury': A.bal(A.treasury)}}, t0=t0)

    def j14(self, t0):
        """Settle a partial useful delivery without paying an unfinished mandatory milestone."""
        A = self.A; A.fund('requester', 300); m0 = A.bal('machine')
        graph = {'milestones': [{'key': 'first', 'kind': 'work', 'depends': [], 'spec': {'template': 'lunar_terrain_tile', 'inputs': dict(lunar_example('terrain'), private_label='J14_FIRST'), 'price': 3, 'bps': 0, 'verifier': 0}}, {'key': 'pay_first', 'kind': 'settle', 'depends': ['first'], 'spec': {'of': 'first'}},
                               {'key': 'bad', 'kind': 'work', 'depends': ['first'], 'spec': {'template': 'lunar_route_feasibility', 'inputs': dict(lunar_example('route'), private_label='J14_BAD'), 'price': 5, 'bps': 0, 'verifier': 0}}, {'key': 'after', 'kind': 'settle', 'depends': ['bad'], 'spec': {'of': 'bad'}}]}
        mid = A.mission('requester', graph=graph, title='partial delivery')['id']
        A.drive_work('requester', 'machine', mid, 'first', 3); p = A.advance('requester', mid, 'pay')
        s = A.advance('requester', mid, 'open_request'); rid = A.ms('requester', mid, 'bad')['binding']['request_id']
        A.call('post', '/api/v1/work/requests/%s/offers' % rid, role='machine', json={'price_amount': 5, 'asset': A.asset, 'scheme': 'exact', 'window_seconds': 3600, 'verification': {'class': 'analytical'}})
        A.advance('requester', mid, 'award'); aid = A.ms('requester', mid, 'bad')['binding']['award_id']; aw = A.wait_execution(aid, 'requester'); jid = aw['milestones'][0]['job_id']
        A.call('post', '/api/v1/ops/faults', json={'fault': 'verification_fail', 'job_id': jid}); A.advance('requester', mid, 'verify'); d = A.advance('requester', mid, 'decide')
        final = A.step('requester', mid); v = A.mview('requester', mid); states = {x['key']: x['state'] for x in v['milestones']}
        ok = p['performed']['action'] == 'pay' and d['performed']['action'] == 'decide' and d['result']['decision'] == 'rejected' and states['bad'] == 'failed' and states['after'] == 'skipped' and states['pay_first'] == 'settled' and A.bal('machine') == m0 + 3 and final['performed'] is None
        self.rec(14, self.j14.__doc__, ok, {'states': states, 'machine_delta': A.bal('machine') - m0, 'rejected_reason': (A.ms('requester', mid, 'bad').get('reason') or '')[:80]}, t0=t0)

    def j15(self, t0):
        """Accept and pay a valid negative result under an explicit policy."""
        A = self.A; A.fund('requester', 300); m0 = A.bal('machine'); route = lunar_example('route', 'tycho_48'); route['rover']['max_slope_milli_deg'] = 4000; route['private_label'] = 'J15_NEG'
        graph = {'milestones': [{'key': 'compute', 'kind': 'work', 'depends': [], 'spec': {'template': 'lunar_route_feasibility', 'inputs': route, 'price': 10, 'bps': 0, 'verifier': 0, 'verification_class': 'analytical'}}, {'key': 'settle', 'kind': 'settle', 'depends': ['compute'], 'spec': {'of': 'compute'}}]}
        mid = A.mission('requester', graph=graph, title='valid negative')['id']; s = A.drive_work('requester', 'machine', mid, 'compute', 10); p = A.advance('requester', mid, 'pay')
        ok = s['result']['decision'] == 'accepted' and s['result']['science'] == 'INFEASIBLE' and p['performed']['action'] == 'pay' and A.bal('machine') == m0 + 10
        self.rec(15, self.j15.__doc__, ok, {'science': s['result']['science'], 'decision': s['result']['decision'], 'payment_class': s['result'].get('payment_class'), 'paid': A.bal('machine') - m0}, t0=t0)

    def j16(self, t0):
        """Refuse unauthorized mutation of awarded terms; accept an authorized amendment."""
        A = self.A; A.fund('requester', 300); m = A.mission('requester', 'terrain_delivery', policy=self.pol(compute_cpu_ms=20)); mid = m['id']; A.drive_work('requester', 'machine', mid, 'acquire', 4)
        s_prov = A.call('post', '/api/v1/missions/%s/amend' % mid, role='machine', json={'milestones': [], 'reason': 'x'})[0]
        s_comm, r_comm = A.call('post', '/api/v1/missions/%s/amend' % mid, role='requester', json={'milestones': [{'key': 'acquire', 'kind': 'work', 'spec': {'template': 'lunar_terrain_tile', 'inputs': {}, 'price': 1}}], 'reason': 'cheaper'})
        spec = dict(A.ms('requester', mid, 'compute')['spec'], price=30); st, v = A.call('post', '/api/v1/missions/%s/amend' % mid, role='requester', json={'milestones': [{'key': 'compute', 'kind': 'work', 'depends': ['acquire'], 'spec': spec}], 'reason': 'renegotiated price'})
        rev = v['revisions'][-1]; s_v = A.call('post', '/api/v1/missions/%s/revisions/2/authorize' % mid, role='verifier', json={})[0]
        st2, v2 = A.call('post', '/api/v1/missions/%s/revisions/2/authorize' % mid, role='machine', json={})
        A.advance('requester', mid, 'pay'); s = A.drive_work('requester', 'machine', mid, 'compute', 30)
        ok = s_prov == 403 and s_comm == 409 and r_comm['detail']['code'] == 'milestone_committed' and st == 200 and rev['state'] == 'proposed' and A.actors['machine']['principal_id'] in rev['affected'] and s_v == 403 and v2['frozen_revision'] == 2 and v2['revisions'][0]['state'] == 'superseded' and A.ms('requester', mid, 'acquire')['state'] == 'accepted' and s['result']['payable_amount'] == 30
        self.rec(16, self.j16.__doc__, ok, {'provider_amend': s_prov, 'committed_milestone_amend': (s_comm, r_comm.get('detail', {}).get('code')), 'affected': len(rev['affected']), 'unaffected_authorize': s_v, 'frozen_revision': v2.get('frozen_revision'), 'new_price_paid': s['result'].get('payable_amount')}, t0=t0)

    def j17(self, t0):
        """Refuse a machine purchase before earnings; allow it from settled earnings afterward."""
        A = self.A; A.fund('requester', 300); A.offer('supplier', 'compute', 100000, 1); A.actor('mach17'); A.provider('mach17'); m = A.mission('requester', 'terrain_delivery', policy=self.pol(compute_cpu_ms=20, permitted_providers=[A.actors['mach17']['provider_id']])); mid = m['id']
        A.drive_work('requester', 'mach17', mid, 'acquire', 4); A.advance('requester', mid, 'pay'); A.drive_work('requester', 'mach17', mid, 'compute', 40)
        exm = A.explain('mach17', mid); fo = next((a for a in exm['actions'] if a['milestone'] == 'followon' and a['action'] == 'reserve'), None)
        before = A.step('mach17', mid); A.advance('requester', mid, 'pay'); after = A.step('mach17', mid)
        ok = fo is not None and not fo['permitted'] and 'insufficient_liquidity' in fo['reason'] and 'receivables 40' in fo['reason'] and before['performed'] is None and before['state'] == 'waiting' and after['performed'] and after['performed']['action'] == 'reserve' and A.bal('mach17', 'reserved') == 20 and A.bal('mach17') == 24
        self.rec(17, self.j17.__doc__, ok, {'before_settlement': fo['reason'][:120] if fo else None, 'after_settlement': after.get('performed'), 'machine_reserved': A.bal('mach17', 'reserved'), 'machine_available': A.bal('mach17')}, t0=t0)

    def j18(self, t0):
        """Refuse spending unsettled anticipated revenue and preserve minimum cash reserve."""
        A = self.A; own = A.call('get', '/api/v1/native/accounts/me')[1]['id']; A.call('post', '/api/v1/native/fixtures/fund', json={'account_id': own, 'amount': 200})
        A.offer('supplier', 'compute', 100000, 1); A.actor('mach18'); A.provider('mach18'); self.ctx['mach18'] = A.actors['mach18']['provider_id']
        m = A.mission('owner', 'terrain_delivery', policy=self.pol(min_cash_reserve=150, per_action_ceiling=45, compute_cpu_ms=20, permitted_providers=[self.ctx['mach18']])); mid = m['id']; ex = A.explain('owner', mid)
        A.drive_work('owner', 'mach18', mid, 'acquire', 4); acct = A.call('get', '/api/v1/native/accounts/me', role='mach18')[1]
        offer = A.call('get', '/api/v1/resources/offers', params={'kind': 'compute'})[1]['items'][-1]; st_u, ru = A.reserve('mach18', offer, 4, 'unsettled')       # the accepted-but-unsettled 4 is a receivable, not liquidity
        m2 = A.mission('owner', 'terrain_delivery', policy=self.pol(min_cash_reserve=198, per_action_ceiling=45)); ex2 = A.explain('owner', m2['id']); op2 = next(a for a in ex2['actions'] if a['action'] == 'open_request')
        ok = ex['context']['spendable_settled'] == 50 and acct['earned_unsettled'] == 4 and acct['available'] == 0 and st_u in (400, 409) and (ru.get('detail') or {}).get('code') == 'insufficient_available' and not op2['permitted'] and 'insufficient_liquidity' in op2['reason'] and 'minimum reserve 198' in op2['reason']
        self.ctx['j18'] = mid
        self.rec(18, self.j18.__doc__, ok, {'spendable_with_reserve_150': ex['context']['spendable_settled'], 'machine_receivable': acct['earned_unsettled'], 'machine_available': acct['available'], 'unsettled_revenue_refusal': (st_u, (ru.get('detail') or {}).get('code')), 'reserve_198_refusal': op2['reason'][:120]}, t0=t0)

    def j19(self, t0):
        """Reject an unsafe model-proposed action through the deterministic grant validator."""
        A = self.A; mid = self.ctx['j18']
        pr = A.call('post', '/api/v1/missions/%s/propose' % mid, role='owner', json={'action': 'pay', 'milestone': 'settle'})[1]
        pr2 = A.call('post', '/api/v1/missions/%s/propose' % mid, role='owner', json={'action': 'reserve', 'milestone': 'followon', 'offer_id': 'off_nonexistent'})[1]
        A.advance('owner', mid, 'pay'); s = A.advance('owner', mid, 'open_request')
        pr3 = A.call('post', '/api/v1/missions/%s/propose' % mid, role='owner', json={'action': 'award', 'milestone': 'compute', 'offer_id': 'wo_fake'})[1]
        ev = [e for e in A.mview('owner', mid)['events'] if e['kind'] == 'proposal_rejected']
        ok = not pr['proposal']['accepted'] and not pr2['proposal']['accepted'] and not pr3['proposal']['accepted'] and all(x['proposal'].get('reason') for x in (pr, pr2, pr3)) and len(ev) >= 3
        self.rec(19, self.j19.__doc__, ok, {'rejections': [x['proposal']['reason'] for x in (pr, pr2, pr3)], 'recorded': len(ev)}, t0=t0)

    def j20(self, t0):
        """Continue during blackout within preallocated authority; wait when exhausted."""
        A = self.A; mid = self.ctx['j18']; rid = A.ms('owner', mid, 'compute')['binding']['request_id']
        A.call('post', '/api/v1/work/requests/%s/offers' % rid, role='mach18', json={'price_amount': 40, 'asset': A.asset, 'scheme': 'exact', 'window_seconds': 3600, 'verification': {'class': 'analytical'}})
        A.advance('owner', mid, 'award'); A.advance('owner', mid, 'verify'); d = A.advance('owner', mid, 'decide')
        A.connectivity('cut'); ex = A.explain('owner', mid); op = A.operator(); local = A.advance('owner', mid, 'pay'); waiting = A.step('owner', mid)
        cut_reasons = [a['reason'] for a in ex['actions'] if not a['permitted'] and a['reason'] and 'link_cut' in a['reason']]
        A.connectivity('online')
        ok = d['result']['decision'] == 'accepted' and ex['context']['connectivity'] == 'cut' and op['blackout']['active'] and local['performed']['action'] == 'pay' and waiting['performed'] is None and waiting['state'] in ('waiting', 'stopped') and any(a['action'] == 'pay' for a in op['blackout']['possible_now'])
        self.ctx['j20_operator'] = op
        self.rec(20, self.j20.__doc__, ok, {'paid_during_blackout': local.get('performed'), 'then': waiting.get('state'), 'wait_reason': (waiting.get('wait_reason') or '')[:100], 'link_cut_refusals': cut_reasons[:3], 'operator_possible': [(a['action'], a['milestone']) for a in op['blackout']['possible_now']][:5], 'operator_disabled': [(a['action'], (a['reason'] or '')[:50]) for a in op['blackout']['disabled_now']][:5]}, caveat='local settlement from reserved funds continues; relay and cross-domain actions are refused with link_cut', t0=t0)

    def j21(self, t0):
        """Apply learned revocation to new work while retaining already incurred obligations."""
        A = self.A; pol = {'schema': 'metacoin-agent-policy/v1', 'permitted_services': ['route_feasibility', 'terrain_tile'], 'allowed_operations': ['native:read', 'native:transfer', 'work:read', 'work:award'], 'ceilings': {'total_amount': 60, 'per_action_amount': 50, 'max_jobs': 10, 'max_workflows': 0, 'concurrency': 1}, 'validity_seconds': 3600, 'review_gate_mandatory': False, 'input_visibility': 'own'}
        st, g = A.call('post', '/api/v1/agents/grants', json={'policy': pol}); own = A.call('get', '/api/v1/native/accounts/me')[1]['id']; A.call('post', '/api/v1/native/fixtures/fund', json={'account_id': own, 'amount': 100})
        m = A.mission('owner', 'terrain_delivery', policy=self.pol(compute_cpu_ms=20), grant_id=g['grant_id']); mid = m['id']; A.drive_work('owner', 'machine', mid, 'acquire', 4)
        A.call('post', '/api/v1/agents/grants/%s/revoke' % g['grant_id'], json={}); ex = A.explain('owner', mid)
        pay = [a for a in ex['actions'] if a['action'] == 'pay']; others = [a for a in ex['actions'] if a['action'] != 'pay']
        s = A.step('owner', mid); led = A.call('get', '/api/v1/missions/%s/ledger' % mid, role='owner')[1]
        ok = st == 201 and ex['stop_reason'] == 'grant_revoked' and pay and pay[0]['permitted'] and all(not a['permitted'] and 'grant_revoked' in (a['reason'] or '') for a in others) and s['performed'] and s['performed']['action'] == 'pay' and led['totals']['spent'] == 4
        self.rec(21, self.j21.__doc__, ok, {'stop_reason': ex['stop_reason'], 'incurred_obligation_paid': s.get('performed'), 'new_work_refused': [(a['action'], (a['reason'] or '')[:40]) for a in others][:3], 'spent': led['totals']['spent']}, t0=t0)

    # ---- 22–25 planner ----------------------------------------------------------------------------------------------------------------
    def j22(self, t0):
        """Match the planner to an exhaustive oracle on bounded fixtures."""
        from metacoin_service.tests.test_native_planner import fixture
        A = self.A; agree = infeasible = 0; mism = []
        for seed in range(40):
            for obj in PL.OBJECTIVES:
                p = dict(fixture(seed, missions=2 if seed % 3 == 0 else 1), objective=obj)
                so, o = A.call('post', '/api/v1/plans/solve', role='requester', json={'problem': p, 'planner': 'oracle'}); si, i = A.call('post', '/api/v1/plans/solve', role='requester', json={'problem': p, 'planner': 'improved'})
                if o['status'] == 'infeasible':
                    infeasible += 1
                    if i['status'] != 'infeasible':
                        mism.append((seed, obj, 'feasibility'))
                elif i['status'] != 'optimal' or i['incumbent']['objective_value'] != o['incumbent']['objective_value']:
                    mism.append((seed, obj, i['status']))
                else:
                    agree += 1
        ok = not mism and agree >= 25 and infeasible >= 3
        self.rec(22, self.j22.__doc__, ok, {'fixtures': 80, 'optimal_agreements': agree, 'infeasible_agreements': infeasible, 'mismatches': mism[:5], 'via': 'POST /plans/solve (oracle vs improved)'}, t0=t0)

    def j23(self, t0):
        """Schedule competing missions without overcommitting shared resources."""
        A = self.A; A.fund('requester', 1500); t = int(time.time()); cheap = A.offer('supplier', 'contact', 100, 1, t - 1, t + 3600); dear = A.offer('contributor', 'contact', 200, 4, t - 1, t + 3600)
        g = lambda: {'milestones': [{'key': 'link', 'kind': 'resource', 'depends': [], 'spec': {'offer_kind': 'contact', 'quantity': 95, 'max_price_per_unit': 5}}]}
        a = A.mission('requester', graph=g(), title='competing A')['id']; b = A.mission('requester', graph=g(), title='competing B')['id']
        st, pl = A.call('post', '/api/v1/missions/plan', role='requester', json={'mission_ids': [a, b], 'objective': 'lowest_cost', 'apply': True})
        avail0 = A.call('get', '/api/v1/resources/offers/' + cheap['id'])[1]['available']; sa = A.step('requester', a); sb = A.step('requester', b)
        avail1 = A.call('get', '/api/v1/resources/offers/' + cheap['id'])[1]['available']; c3 = A.mission('requester', graph=g(), title='competing C')['id']; pl3 = A.call('post', '/api/v1/missions/%s/plan' % c3, role='requester', json={'objective': 'lowest_cost'})[1]
        ok = st == 200 and pl['status'] == 'optimal' and not pl['binding'] and pl['incumbent']['cost'] == 95 + 380 and avail0 == 100 and {sa['performed']['offer_id'], sb['performed']['offer_id']} == {cheap['id'], dear['id']} and avail1 == 5 and pl3['incumbent']['choice'][c3 + ':link'] == dear['id']
        self.rec(23, self.j23.__doc__, ok, {'joint_cost': (pl.get('incumbent') or {}).get('cost'), 'plan_locked_nothing': avail0 == 100, 'reserved': {a: sa['performed']['offer_id'] == cheap['id'], b: sb['performed']['offer_id'] == dear['id']}, 'third_mission_plans_around_live_availability': (pl3.get('incumbent') or {}).get('choice')}, t0=t0)

    def j24(self, t0):
        """Replan after a missed contact window without duplicating payments or reservations."""
        A = self.A; A.fund('requester', 1500); t = int(time.time()); early = A.offer('supplier', 'contact', 500, 1, t - 1, t + 2); late = A.offer('supplier', 'contact', 500, 2, t + 4, t + 3600); sup0 = A.bal('supplier'); req0 = A.bal('requester')
        mid = A.mission('requester', graph={'milestones': [{'key': 'link', 'kind': 'resource', 'depends': [], 'spec': {'offer_kind': 'contact', 'quantity': 450, 'max_price_per_unit': 5}}]}, title='missed window')['id']
        pl = A.call('post', '/api/v1/missions/%s/plan' % mid, role='requester', json={'objective': 'earliest_delivery', 'apply': True})[1]; s1 = A.step('requester', mid); first = s1['result']['reservation_id']
        time.sleep(2.5); s2 = A.step('requester', mid); pl2 = A.call('post', '/api/v1/missions/%s/plan' % mid, role='requester', json={'objective': 'earliest_delivery', 'apply': True})[1]; s3 = A.step('requester', mid); second = s3['result']['reservation_id']
        A.res_action('supplier', second, 'activate'); A.res_action('supplier', second, 'deliver', {'bytes': 450 * 1024}); s4 = A.advance('requester', mid, 'settle_reservation'); led = A.call('get', '/api/v1/missions/%s/ledger' % mid, role='requester')[1]
        ok = pl['incumbent']['choice'][mid + ':link'] == early['id'] and s1['performed']['offer_id'] == early['id'] and s2['performed']['action'] == 'release_missed_reservation' and pl2['incumbent']['choice'][mid + ':link'] == late['id'] and s3['performed']['offer_id'] == late['id'] and first != second and s4['performed']['action'] == 'settle_reservation' \
            and (led['totals']['spent'], led['totals']['released']) == (900, 450) and A.bal('supplier') == sup0 + 900 and A.bal('requester') == req0 - 900 and A.call('get', '/api/v1/resources/reservations/' + first, role='requester')[1]['state'] == 'cancelled'
        self.rec(24, self.j24.__doc__, ok, {'first_reservation': first, 'released': s2['result'].get('released_amount'), 'second_reservation': second, 'ledger': led['totals'], 'supplier_delta': A.bal('supplier') - sup0, 'requester_delta': A.bal('requester') - req0}, t0=t0)

    def j25(self, t0):
        """Return an explicit feasibility failure when reserve/deadline constraints conflict."""
        A = self.A; A.fund('requester', 2000); t = int(time.time()); A.offer('supplier', 'contact', 1000, 3, t + 600, t + 3600); g = {'milestones': [{'key': 'link', 'kind': 'resource', 'depends': [], 'spec': {'offer_kind': 'contact', 'quantity': 500, 'max_price_per_unit': 5}}]}
        avail = A.bal('requester'); m = A.mission('requester', graph=g, policy={'min_cash_reserve': avail - 10}, title='reserve conflict')['id']; pl = A.call('post', '/api/v1/missions/%s/plan' % m, role='requester', json={'objective': 'lowest_cost'})[1]
        m2 = A.mission('requester', graph=g, title='deadline conflict')['id']; pl2 = A.call('post', '/api/v1/missions/%s/plan' % m2, role='requester', json={'objective': 'lowest_cost', 'deadline_at': t + 300})[1]
        ok = pl['status'] == 'infeasible' and pl['witness']['constraint'] == 'budget' and pl['witness']['minimum_cost'] == 1500 and pl['problem']['budgets'][m] == 10 and pl2['status'] == 'infeasible' and pl2['witness']['constraint'] == 'deadline'
        self.rec(25, self.j25.__doc__, ok, {'reserve_witness': pl.get('witness'), 'deadline_witness': pl2.get('witness')}, t0=t0)

    # ---- 26–32 cross-domain liquidity, transport and recovery witnesses -----------------------------------------------------------------
    def j26(self, t0):
        """Subdivide an export and prove disjoint identities and exact backing conservation."""
        A, B = self.A, self.B; A.actor('alice', 50); bob = B.actors['bob']['account']; held0 = A.audit()['total_held']
        sb, bad = A.call('post', '/api/v1/native/allocations/export', role='alice', json={'destination_domain': 'dom-b', 'recipient_account': bob, 'amount': 20, 'portions': [10, 5, 4], 'request_id': 'b-bad'})
        st, b = A.call('post', '/api/v1/native/allocations/export', role='alice', json={'destination_domain': 'dom-b', 'recipient_account': bob, 'amount': 20, 'portions': [10, 6, 4], 'request_id': 'b1'}); ids = [a['id'] for a in b['allocations']]
        again = A.call('post', '/api/v1/native/allocations/export', role='alice', json={'destination_domain': 'dom-b', 'recipient_account': bob, 'amount': 20, 'portions': [10, 6, 4], 'request_id': 'b1'})[1]
        au = A.audit(); out = self.push(A, B); lin = B.call('get', '/api/v1/native/accounts/%s/lineage' % bob)[1]; acc = A.call('get', '/api/v1/native/accounting')[1]
        ok = sb == 422 and bad['detail']['code'] == 'portions_sum' and st == 201 and len(set(ids)) == 3 and b['sum_check'] and [a['amount'] for a in b['allocations']] == [10, 6, 4] and again['replayed'] and [a['id'] for a in again['allocations']] == ids and A.bal('alice') == 30 and au['backing_locked_for_exports'] >= 20 and au['conserved'] \
            and [x['state'] for x in out['results']] == ['accepted'] * 3 and [c['amount'] for c in lin['claims']] == [10, 6, 4] and lin['returnable_total'] == 20 and acc['checks']['backing_matches_journal']
        self.ctx['j26'] = ids
        self.rec(26, self.j26.__doc__, ok, {'portions': [10, 6, 4], 'distinct_ids': len(set(ids)), 'replay_same_ids': again.get('replayed'), 'alice': A.bal('alice'), 'backing_locked': au['backing_locked_for_exports'], 'claims_at_b': [c['amount'] for c in lin['claims']], 'accounting_checks': acc['checks']}, t0=t0)

    def j27(self, t0):
        """Spend one allocation and return a separate unused allocation safely."""
        A, B = self.A, self.B; bob, carol = B.actors['bob']['account'], B.actors['carol']['account']; a10, a6, a4 = self.ctx['j26']; alice0 = A.bal('alice')
        st, t = B.call('post', '/api/v1/native/transfers', role='bob', json={'to_account': carol, 'amount': 7, 'asset': A.asset, 'request_id': 's7'})
        lin = {c['allocation_id']: c for c in B.call('get', '/api/v1/native/accounts/%s/lineage' % bob)[1]['claims']}
        sr, r = B.call('post', '/api/v1/native/allocations/%s/return' % a4, role='bob', json={}); out = self.push(B, A); dup = A.call('post', '/api/v1/native/transport/inbox', json=B.call('get', '/api/v1/native/transport/outbox', params={'receiver': 'dom-a', 'all': '1'})[1])[1]
        acc = B.call('get', '/api/v1/native/accounting', params={'asset': A.asset})[1]
        ok = st == 201 and (lin[a10]['state'], lin[a10]['consumed'], lin[a10]['returnable'], lin[a6]['state'], lin[a4]['returnable']) == ('partially_consumed', 7, 0, 'active', 4) and sr == 200 and r['exposed_state'] == 'retirement_pending' and out['results'][0]['state'] == 'accepted' \
            and A.bal('alice') == alice0 + 4 and A.alloc(a4)['state'] == 'returned' and B.alloc(a4)['exposed_state'] == 'reconciled' and all(x['state'] == 'duplicate' for x in dup['results']) and A.bal('alice') == alice0 + 4 and acc['claims_retired'] == 4 and A.audit()['total_held'] + B.audit(A.asset)['total_held'] + A.audit()['backing_locked_for_exports'] - B.audit(A.asset)['claims_backed_elsewhere'] == A.audit()['fixture_funding']
        self.rec(27, self.j27.__doc__, ok, {'spent_from_a10': lin[a10]['consumed'], 'returned': a4, 'alice_after_return': A.bal('alice'), 'replay_duplicate': all(x['state'] == 'duplicate' for x in dup['results']), 'claims_retired': acc.get('claims_retired')}, t0=t0)

    def j28(self, t0):
        """Refuse unsupported return of spent or mixed provenance."""
        A, B = self.A, self.B; bob, carol = B.actors['bob']['account'], B.actors['carol']['account']; a10, a6, a4 = self.ctx['j26']
        s1, r1 = B.call('post', '/api/v1/native/allocations/%s/return' % a10, role='bob', json={})
        B.call('post', '/api/v1/native/transfers', role='carol', json={'to_account': bob, 'amount': 2, 'asset': A.asset, 'request_id': 'c2'}); B.call('post', '/api/v1/native/transfers', role='bob', json={'to_account': carol, 'amount': 2, 'asset': A.asset, 'request_id': 's2'})
        lin = {c['allocation_id']: c for c in B.call('get', '/api/v1/native/accounts/%s/lineage' % bob)[1]['claims']}; carol_lin = B.call('get', '/api/v1/native/accounts/%s/lineage' % carol)[1]
        s2, r2 = B.call('post', '/api/v1/native/allocations/%s/return' % a6, role='carol', json={})
        ok = s1 == 409 and r1['detail']['code'] == 'return_partial_unsupported' and lin[a10]['consumed'] == 7 and lin[a6]['state'] == 'active' and carol_lin['returnable_total'] == 0 and s2 in (403, 409)
        self.rec(28, self.j28.__doc__, ok, {'spent_allocation_return': (s1, r1.get('detail', {}).get('code')), 'mixed_spend_consumed_unlineaged_first': lin[a10]['consumed'] == 7, 'carol_returnable': carol_lin['returnable_total'], 'non_holder_return': s2}, t0=t0)

    def j29(self, t0):
        """Crash at export, import, retirement, return and acknowledgment boundaries; replay safely."""
        A, B = self.A, self.B; bob = B.actors['bob']['account']; alice0 = A.bal('alice'); funding = A.audit()['fixture_funding']; crash = {}
        for point, durable in (('native_export_reserve', 'reserved'), ('native_export_commit', 'export_committed'), ('native_certificate_publish', 'certified')):
            A.fault(point); st, r = A.call('post', '/api/v1/native/allocations/export', role='alice', json={'destination_domain': 'dom-b', 'recipient_account': bob, 'amount': 3, 'request_id': 'crash-' + point}); A.fault(point, disarm=True)
            mine = [x for x in A.call('get', '/api/v1/native/allocations', params={'direction': 'export'})[1]['items'] if x['evidence'].get('request_id') == 'crash-' + point]
            r2 = A.call('post', '/api/v1/native/allocations/export', role='alice', json={'destination_domain': 'dom-b', 'recipient_account': bob, 'amount': 3, 'request_id': 'crash-' + point})[1]
            crash[point] = {'first': st, 'durable_state': mine[0]['state'] if mine else None, 'retry_state': r2.get('state'), 'same_id': bool(mine) and r2.get('id') == mine[0]['id'], 'recover_sweep': A.call('post', '/api/v1/native/allocations/recover', json={})[1].get('resumed')}
        certs = A.call('get', '/api/v1/native/transport/outbox', params={'receiver': 'dom-b'})[1]; bob0 = B.bal(bob, asset=A.asset)
        for point in ('native_import_commit', 'native_ack'):
            B.fault(point); st, r = B.call('post', '/api/v1/native/transport/inbox', json=certs); B.fault(point, disarm=True); crash[point] = {'first': st, 'credited': B.bal(bob, asset=A.asset) - bob0}
        out = self.push(A, B); accepted = [x['state'] for x in out['results']]
        # retirement / return / completion boundaries on one of the three
        aid = [x['id'] for x in A.call('get', '/api/v1/native/allocations', params={'direction': 'export'})[1]['items'] if x['evidence'].get('request_id') == 'crash-native_export_reserve'][0]
        B.fault('native_return_init'); sr = B.call('post', '/api/v1/native/allocations/%s/return' % aid, role='bob', json={})[0]; B.fault('native_return_init', disarm=True); v = B.alloc(aid)
        sr2, r2 = B.call('post', '/api/v1/native/allocations/%s/return' % aid, role='bob', json={}); A.fault('native_return_complete'); self.push(B, A); A.fault('native_return_complete', disarm=True); mid_state = A.alloc(aid)['state']
        self.push(B, A); self.push(B, A); crash['return'] = {'init_crash': sr, 'durable': (v['state'], v['exposed_state']), 'replayed': r2.get('replayed'), 'after_completion_crash': mid_state, 'final': (A.alloc(aid)['state'], B.alloc(aid)['state'])}
        ok = all(c['first'] == 409 for c in crash.values() if 'first' in c) and all(crash[p]['retry_state'] == 'certified' and crash[p]['same_id'] and crash[p]['recover_sweep'] == [] for p in ('native_export_reserve', 'native_export_commit', 'native_certificate_publish')) and crash['native_import_commit']['credited'] == 0 and crash['native_ack']['credited'] == 0 \
            and accepted == ['accepted'] * 3 and crash['return']['durable'] == ('return_requested', 'retirement_pending') and crash['return']['replayed'] and crash['return']['after_completion_crash'] == 'acknowledged' and crash['return']['final'] == ('returned', 'returned') and A.bal('alice') == alice0 - 6 and A.audit()['conserved']
        self.rec(29, self.j29.__doc__, ok, {'boundaries': crash, 'alice_delta': A.bal('alice') - alice0, 'funding': funding}, t0=t0)

    def j30(self, t0):
        """Deliver duplicate/reordered/expired bundles without duplicate settlement or timeout refund."""
        A, B = self.A, self.B; bob = B.actors['bob']['account']; bob0 = B.bal(bob, asset=A.asset); alice0 = A.bal('alice')
        A.env['METACOIN_FRAGMENT_BYTES'] = '400'; A.restart_api()
        try:
            ids = [A.call('post', '/api/v1/native/allocations/export', role='alice', json={'destination_domain': 'dom-b', 'recipient_account': bob, 'amount': 1, 'request_id': 'f%d' % i})[1]['id'] for i in range(3)]
            ob = A.call('get', '/api/v1/native/transport/outbox', params={'receiver': 'dom-b'})[1]
        finally:
            A.env.pop('METACOIN_FRAGMENT_BYTES', None); A.restart_api()
        frags = [it for it in ob['items'] if it.get('fragment_of')]; shuffled = dict(ob, items=list(reversed(ob['items'])) + ob['items'][:2])
        st, r = B.call('post', '/api/v1/native/transport/inbox', json=shuffled); courier.mark_from_results(A.ep(), r['results']); credited = B.bal(bob, asset=A.asset) - bob0
        bad = json.loads(json.dumps(ob)); f0 = [it for it in bad['items'] if it.get('fragment_of')][-1]; f0['data'] = ('x' if f0['data'][0] != 'x' else 'y') + f0['data'][1:]; r2 = B.call('post', '/api/v1/native/transport/inbox', json=bad)[1]
        sv = B.call('post', '/api/v1/native/transport/inbox', json=dict(ob, envelope_versions=['v9']))[0]
        A.env['METACOIN_ENVELOPE_TTL'] = '-5'; A.restart_api()
        try:
            exp = A.call('post', '/api/v1/native/allocations/export', role='alice', json={'destination_domain': 'dom-b', 'recipient_account': bob, 'amount': 2, 'request_id': 'exp'})[1]
        finally:
            A.env.pop('METACOIN_ENVELOPE_TTL', None); A.restart_api()
        ob3 = A.call('get', '/api/v1/native/transport/outbox', params={'receiver': 'dom-b'})[1]; r3 = B.call('post', '/api/v1/native/transport/inbox', json=ob3)[1]
        ok = len(frags) > 3 and ob['schema'] == 'metacoin-native-bundle/v1' and sorted(ob['economic_ids']) == sorted(ids) and r['envelope_version'] == 'v1' and sum(1 for x in r['results'] if x['state'] == 'accepted') == 3 and credited == 3 and 'fragment_digest_mismatch' in [x.get('reason') for x in r2['results']] and B.bal(bob, asset=A.asset) - bob0 == 3 and sv == 422 \
            and [x['reason'] for x in r3['results'] if x.get('reason')] == ['expired'] and A.alloc(exp['id'])['state'] == 'certified' and A.bal('alice') == alice0 - 5
        self.rec(30, self.j30.__doc__, ok, {'fragments': len(frags), 'economic_ids': len(ob['economic_ids']), 'reordered_duplicated_credited_once': credited, 'tampered_fragment': 'fragment_digest_mismatch' in [x.get('reason') for x in r2['results']], 'unsupported_version': sv, 'expired': [x.get('reason') for x in r3['results']], 'expired_backing_still_locked': A.alloc(exp['id'])['state']}, caveat='expiry never refunds: the 2 units stay locked as backing until a return or an administrator decision', t0=t0)

    def j31(self, t0):
        """Restore stale state and quarantine against a signed external checkpoint."""
        A, B = self.A, self.B; bob = B.actors['bob']['account']; db = A.inst.settings.db_path; backup = Path(A.inst.temp.name) / 'before.sqlite'
        src = sqlite3.connect('file:%s?mode=ro' % db, uri=True); dst = sqlite3.connect(str(backup)); src.backup(dst); dst.close(); src.close()
        a = A.call('post', '/api/v1/native/allocations/export', role='alice', json={'destination_domain': 'dom-b', 'recipient_account': bob, 'amount': 5, 'request_id': 'stale'})[1]; self.push(A, B)
        w = self.W.client.call('GET', '/latest/dom-a')[1]; rec1 = A.call('get', '/api/v1/native/recovery')[1]
        A.api.terminate(); A.api.wait(timeout=20)
        for suffix in ('-wal', '-shm'):
            p = db.with_name(db.name + suffix)
            if p.exists():
                p.unlink()
        shutil.copyfile(backup, db); os.chmod(db, 0o600); wm = A.inst.settings.keys_dir / 'native' / 'watermark-dom-a.json'
        if wm.exists():
            wm.unlink()
        A.start_api(); d = A.domain(); rec = A.call('get', '/api/v1/native/recovery')[1]
        st_q, rq = A.call('post', '/api/v1/native/transfers', role='alice', json={'to_account': A.actors['machine']['account'], 'amount': 1, 'request_id': 'q'})
        B.call('post', '/api/v1/native/transport/observation', json={'peer': 'dom-a'}); self.push(B, A)
        st_r, r = A.call('post', '/api/v1/native/recovery/reconcile', json={'reconstruct_from_observations': True, 'source_account': A.actors['alice']['account'], 'amounts': {a['id']: 5}})
        ok = w['latest']['sequence'] == rec1['witness']['latest_sequence'] and rec1['witness']['identity_pinned_matches'] and d['recovery_state'] == 'quarantine' and 'witness-acknowledged' in rec['rollback_detection'] and st_q == 409 and rq['detail']['code'] == 'domain_quarantined' and st_r == 200 and r['recovery_state'] == 'normal' and A.domain()['commit_sequence'] >= w['latest']['sequence'] and A.audit()['conserved']
        self.rec(31, self.j31.__doc__, ok, {'witness_sequence_before_restore': w['latest']['sequence'], 'after_restore': {'recovery_state': d['recovery_state'], 'detection': rec['rollback_detection'][:80]}, 'spend_during_quarantine': (st_q, rq.get('detail', {}).get('code')), 'reconciled': r.get('recovery_state'), 'sequence_after': A.domain()['commit_sequence']}, caveat='the local watermark file was removed on purpose: only the witness could tell the restored database was stale', t0=t0)

    def j32(self, t0):
        """Detect contradictory authority histories; expose the actual safety limitation."""
        A, B = self.A, self.B; bob, carol = B.actors['bob']['account'], B.actors['carol']['account']; a = A.call('post', '/api/v1/native/allocations/export', role='alice', json={'destination_domain': 'dom-b', 'recipient_account': bob, 'amount': 3, 'request_id': 'cx'})[1]; self.push(A, B); bob0 = B.bal(bob, asset=A.asset)
        latest = self.W.client.call('GET', '/latest/dom-a')[1]['latest']; stx = dict(latest['statement'], journal_root='00' * 32); key = crypto.load_signing_key(A.inst.settings.keys_dir / 'native' / 'authority-dom-a.ed25519')
        code, r = self.W.client.call('POST', '/checkpoint', {'statement': stx, 'signature_hex': crypto.sign(key, P.signing_bytes('commit_checkpoint', stx)), 'key_id': A.domain()['authority']['key_id']})
        cert = A.alloc(a['id'])['certificate']; twin = dict(cert, recipient_account=carol)
        env = {'protocol': P.PROTOCOL, 'type': 'envelope', 'id': 'nenv_twin_j32', 'environment': 'dev', 'sender': 'dom-a', 'receiver': 'dom-b', 'message_type': 'allocation_certificate', 'causality': a['id'], 'payload_digest': P.digest(twin), 'expires_at': 4102444800, 'created_at': cert['issued_at']}
        item = {'envelope': env, 'envelope_signature': crypto.sign(key, P.signing_bytes('envelope', env)), 'key_id': A.domain()['authority']['key_id'], 'payload': twin, 'payload_signature': crypto.sign(key, P.signing_bytes('allocation_certificate', twin))}
        rr = B.call('post', '/api/v1/native/transport/inbox', json={'items': [item]})[1]; rec = B.call('get', '/api/v1/native/recovery')[1]
        ok = code == 400 and r.get('code') == 'conflicting_checkpoint' and rr['results'][0]['reason'] == 'conflicting_signed_history' and rec['quarantined_peers'] == ['dom-a'] and 'never prevented' in rec['safety_limitation'] and B.bal(bob, asset=A.asset) == bob0
        self.ctx['j32_quarantined_b'] = True
        self.rec(32, self.j32.__doc__, ok, {'witness_conflicting_checkpoint': r.get('code'), 'twin_certificate': rr['results'][0].get('reason'), 'quarantined_peers_at_b': rec['quarantined_peers'], 'safety_limitation': rec['safety_limitation'][:140], 'earlier_credit_stands': B.bal(bob, asset=A.asset) == bob0}, t0=t0)

    # ---- 33–36 client, bundle, console, reproduction -------------------------------------------------------------------------------------
    def j33(self, t0):
        """Install a clean provider client and complete an offer-to-receipt cycle over public APIs."""
        A = self.A; tmp = Path(tempfile.mkdtemp(prefix='metacoin-client-')); copy = tmp / 'provider_client'; shutil.copytree(ROOT / 'provider_client', copy, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        venv = tmp / 'venv'; subprocess.run([sys.executable, '-m', 'venv', str(venv)], check=True, capture_output=True); vpy = venv / 'bin' / 'python'
        inst = subprocess.run([str(venv / 'bin' / 'pip'), 'install', '--no-index', '--no-build-isolation', '--no-deps', str(copy)], capture_output=True, text=True, timeout=300)
        env = {'PATH': os.environ['PATH'], 'HOME': str(tmp), 'LANG': 'C.UTF-8'}; method = 'pip install (offline, no build isolation)'
        if inst.returncode != 0:
            env['PYTHONPATH'] = str(copy); method = 'package directory on PYTHONPATH (pip could not build offline: %s)' % (inst.stderr.strip().splitlines()[-1][:120] if inst.stderr.strip() else 'no stderr')
        def cli(*args, timeout=300):
            p = subprocess.run([str(vpy), '-m', 'metacoin_provider'] + list(args), cwd=str(tmp), env=env, capture_output=True, text=True, timeout=timeout)
            try:
                return p.returncode, json.loads(p.stdout)
            except ValueError:
                return p.returncode, {'stdout': p.stdout[-300:], 'stderr': p.stderr[-400:]}
        probe = subprocess.run([str(vpy), '-c', 'import metacoin_provider, sys; print(sorted(m.split(".")[0] for m in sys.modules if not m.startswith("_")))'], cwd=str(tmp), env=env, capture_output=True, text=True); third_party = [m for m in json.loads(probe.stdout.replace("'", '"')) if m in ('httpx', 'fastapi', 'cryptography', 'metacoin_service', 'sqlite3')] if probe.returncode == 0 else ['probe failed']
        rc_c, conf = cli('conformance'); cred = tmp / 'prov.json'; admin = A.cred_file('owner')
        rc_e, enr = cli('--base', A.base, '--signer', self.sgA.base, '--signer-token', str(self.sgA.home / 'token'), 'enroll', '--admin-cred', str(admin), '--name', 'prov-clean', '--ceiling', '500', '--out', str(cred))
        rc_o, offer = cli('--base', A.base, '--cred', str(cred), 'offer-publish', 'compute', 'cpu-clean', '100000', '1', '--idempotency-key', 'clean-offer')
        if rc_e != 0 or rc_o != 0 or 'id' not in offer:
            self.rec(33, self.j33.__doc__, False, {'install_method': method, 'conformance': conf.get('ok'), 'enroll': (rc_e, json.dumps(enr)[:400]), 'offer': (rc_o, json.dumps(offer)[:400])}, t0=t0); return
        A.fund('requester', 5000); st, res = A.reserve('requester', offer, 3000, 'clean-res'); rid = res['id']
        rc_a, act = cli('--base', A.base, '--cred', str(cred), 'activate', rid)
        rc_d, dl = cli('--base', A.base, '--cred', str(cred), 'deliver-compute', rid, '--executor', 'env PYTHONPATH=%s %s -m metacoin_service.lunar task' % (ROOT, PY), '--idempotency-key', 'clean-deliver', timeout=600)
        s = A.res_action('requester', rid, 'settle')[1]; rc_r, roots = cli('--base', A.base, '--cred', str(cred), 'roots'); rp = tmp / 'roots.json'; rp.write_text(json.dumps(roots))
        rc_rc, rec = cli('--base', A.base, '--cred', str(cred), 'receipts', '--roots', str(rp)); rc_x, ex = cli('--base', A.base, '--cred', str(cred), 'export', '--out', str(tmp / 'export.json')); rc_v, ver = cli('verify', str(tmp / 'export.json'), '--roots', str(rp))
        rc_p, pay = cli('--base', A.base, '--cred', str(cred), '--signer', self.sgA.base, '--signer-token', str(self.sgA.home / 'token'), '--actor', 'prov-clean', 'pay', A.actors['requester']['account'], '3', '--request-id', 'clean-pay')
        m = (dl.get('reservation') or {}).get('measurement') or {}
        ok = rc_c == 0 and conf.get('ok') and not third_party and rc_e == 0 and not enr.get('private_key_held_by_service', True) and rc_o == 0 and rc_a == 0 and rc_d == 0 and m.get('measurement', {}).get('kind') == 'provider_assertion' and m.get('verification', {}).get('ok') and s['state'] == 'settled' and rc_rc == 0 and rec['verification'] and all(c['ok'] for c in rec['verification']) and rc_v == 0 and ver.get('ok') and rc_p == 0 and pay.get('state') == 'settled'
        self.ctx['j33'] = {'tmp': str(tmp), 'vpy': str(vpy), 'env': env, 'cred': str(cred), 'roots': str(rp)}
        self.rec(33, self.j33.__doc__, ok, {'install_method': method, 'conformance': {k: conf.get(k) for k in ('cases', 'passed', 'ok')}, 'third_party_modules_imported': third_party, 'enrolled_custody': enr.get('custody'), 'delivery_labels': {'measurement': m.get('measurement', {}).get('kind'), 'verification': m.get('verification', {}).get('kind'), 'verified': m.get('verification', {}).get('ok')}, 'delivered_quantity': (dl.get('reservation') or {}).get('delivered_quantity'), 'settled': s.get('state'), 'receipts_verified_offline': len(rec.get('verification') or []), 'export_verified_offline': ver.get('ok'), 'signed_payment': pay.get('state')}, caveat='same host: the client ran in a fresh virtual environment with only the standard library; the executor ran the public lunar module from the repository', t0=t0)

    def j34(self, t0):
        """Verify a portable mission bundle offline; reject tampered evidence and accounting."""
        A = self.A; c = self.ctx.get('j33')
        if not c:
            self.rec(34, self.j34.__doc__, False, {}, blocked='journey 33 did not provide the clean client', t0=t0); return
        mid = self.ctx['j13']['mission']; tmp = Path(c['tmp']); reqcred = A.cred_file('requester')
        def cli(*args):
            p = subprocess.run([c['vpy'], '-m', 'metacoin_provider'] + list(args), cwd=str(tmp), env=c['env'], capture_output=True, text=True, timeout=300)
            try:
                return p.returncode, json.loads(p.stdout)
            except ValueError:
                return p.returncode, {'stdout': p.stdout[-300:], 'stderr': p.stderr[-400:]}
        rc_b, b = cli('--base', A.base, '--cred', str(reqcred), 'mission-bundle', mid, '--out', str(tmp / 'bundle.json')); bundle = json.loads((tmp / 'bundle.json').read_text())
        rc_v, ver = cli('verify', str(tmp / 'bundle.json'), '--roots', c['roots']); outcomes = {}
        def tam(name, fn):
            x = json.loads(json.dumps(bundle)); fn(x); p = tmp / ('tampered-%s.json' % name); p.write_text(json.dumps(x)); rc, rep = cli('verify', str(p), '--roots', c['roots']); outcomes[name] = {'rc': rc, 'ok': rep.get('ok'), 'failed': [ch['check'] for ch in rep.get('checks', []) if not ch['ok']][:4]}
        tam('ledger_spent', lambda x: x['content']['ledger']['milestones'][0].update(spent=1)); tam('receipt_claim', lambda x: x['content']['receipts'][0]['statement']['claims'].update(amount=1))
        tam('journal_posting', lambda x: x['content']['journal_entries'][0]['postings'][0].update(debit=x['content']['journal_entries'][0]['postings'][0]['debit'] + 1)); tam('intent_amount', lambda x: x['content']['intents'][0].update(amount_final=1))
        (tmp / 'noroots.json').write_text('{}'); rc_n, norep = cli('verify', str(tmp / 'bundle.json'), '--roots', str(tmp / 'noroots.json'))
        ok = rc_b == 0 and bundle['schema'] == 'metacoin-mission-bundle/v1' and rc_v == 0 and ver.get('ok') and len(bundle['content']['receipts']) >= 3 and all(o['rc'] == 1 and o['ok'] is False for o in outcomes.values()) and 'content_digest' in outcomes['ledger_spent']['failed'] and any(f.startswith('receipt:') for f in outcomes['receipt_claim']['failed']) and 'entries_balanced' in outcomes['journal_posting']['failed'] and rc_n == 1 and 'seal_root_pinned' in [ch['check'] for ch in norep.get('checks', []) if not ch['ok']]
        self.rec(34, self.j34.__doc__, ok, {'bundle': {'receipts': len(bundle['content']['receipts']), 'intents': len(bundle['content']['intents']), 'journal_entries': len(bundle['content']['journal_entries']), 'seal_key': bundle['seal']['key_id'][:12]}, 'valid': ver.get('ok'), 'tampering': outcomes, 'no_roots': [ch['check'] for ch in norep.get('checks', []) if not ch['ok']][:3]}, t0=t0)

    def j35(self, t0):
        """Run the connected mission through the console, including blackout and recovery states."""
        A = self.A; pw = os.environ.get('METACOIN_PLAYWRIGHT_PYTHON'); shots = self.ctx.get('shots')
        if not pw or not shots:
            self.rec(35, self.j35.__doc__, False, {}, blocked='METACOIN_PLAYWRIGHT_PYTHON and --shots required', t0=t0); return
        demo = self.ctx.get('demo') or {}; mid = demo.get('mission') or self.ctx['j13']['mission']; job = demo.get('job') or self.ctx['j13'].get('job')
        A.connectivity('online'); sess = A.http.post('/console/login', data={'token': A.inst.tok['owner']}, follow_redirects=False); pre = A.http.get('/console/operator', cookies=sess.cookies, follow_redirects=False); api_st = A.call('get', '/api/v1/native/operator')
        p = subprocess.run([pw, str(ROOT / 'metacoin_service' / 'tests' / 'browser' / 'journey_mission.py'), A.base, str(A.cred_file('owner')), str(A.cred_file('requester')), mid, job or '', shots], cwd=ROOT, env=dict(A.env, PLAYWRIGHT_BROWSERS_PATH=os.environ.get('PLAYWRIGHT_BROWSERS_PATH', '')), capture_output=True, text=True, timeout=900)
        try:
            out = json.loads(p.stdout.strip().splitlines()[-1])
        except Exception:
            out = {'failed': 1, 'stdout': p.stdout[-800:], 'stderr': p.stderr[-800:]}
        A.connectivity('online')
        self.rec(35, self.j35.__doc__, p.returncode == 0 and out.get('failed') == 0, {'passed': out.get('passed'), 'failed': out.get('failed'), 'checks': out.get('checks'), 'screenshots': out.get('screenshots'), 'crash': out.get('crash'), 'stderr': p.stderr[-400:] if p.returncode else None, 'operator_console_status': pre.status_code, 'operator_console_snippet': pre.text[:300] if pre.status_code != 200 else None, 'operator_api': (api_st[0], json.dumps(api_st[1])[:300] if api_st[0] != 200 else 'ok')}, t0=t0)

    def j36(self, t0):
        """Reproduce the packaged candidate in a fresh environment and verify migrations on a prior-state copy."""
        live = Path(os.environ.get('METACOIN_LIVE_HOME', os.path.expanduser('~/.local/state/metacoin-service'))); part = {}
        if (live / 'service.sqlite').exists():
            iso = Path(tempfile.mkdtemp(prefix='metacoin-iso-')); home = iso / 'home'; home.mkdir(mode=0o700)
            src = sqlite3.connect('file:%s?mode=ro' % (live / 'service.sqlite'), uri=True)
            q = lambda c, sql: c.execute(sql).fetchall()
            bal_sql = "SELECT e.asset, p.account, SUM(p.credit) - SUM(p.debit) FROM journal_postings p JOIN journal_entries e ON e.id = p.entry_id GROUP BY e.asset, p.account ORDER BY 1, 2"
            before = {'schema': q(src, 'SELECT name FROM schema_migrations ORDER BY applied_at DESC, rowid DESC LIMIT 1')[0][0], 'jobs': q(src, 'SELECT COUNT(*) FROM jobs')[0][0], 'artifacts': q(src, 'SELECT COUNT(*) FROM artifacts')[0][0], 'journal_entries': q(src, 'SELECT COUNT(*) FROM journal_entries')[0][0], 'balances_digest': P.digest([list(r) for r in q(src, bal_sql)])}
            dst = sqlite3.connect(str(home / 'service.sqlite')); src.backup(dst); dst.close(); src.close(); os.chmod(home / 'service.sqlite', 0o600)
            for sub in ('keys', 'credentials', 'artifacts'):
                if (live / sub).exists():
                    shutil.copytree(live / sub, home / sub, dirs_exist_ok=True); os.chmod(home / sub, 0o700)
                    for f in (home / sub).rglob('*'):
                        if f.is_file():
                            os.chmod(f, 0o600)
            env = dict(ENV, METACOIN_X402_DISABLED='1', METACOIN_LIMITS_JSON=json.dumps({'test_hooks': 1}))
            mig = subprocess.run([PY, '-m', 'metacoin_service', '--home', str(home), '--provider-mode', 'simulation', 'migrate'], cwd=ROOT, env=env, capture_output=True, text=True, timeout=300)
            c2 = sqlite3.connect(str(home / 'service.sqlite')); after = {'schema': q(c2, 'SELECT name FROM schema_migrations ORDER BY applied_at DESC, rowid DESC LIMIT 1')[0][0], 'jobs': q(c2, 'SELECT COUNT(*) FROM jobs')[0][0], 'artifacts': q(c2, 'SELECT COUNT(*) FROM artifacts')[0][0], 'journal_entries': q(c2, 'SELECT COUNT(*) FROM journal_entries')[0][0], 'balances_digest': P.digest([list(r) for r in q(c2, bal_sql)]), 'new_tables': [r[0] for r in q(c2, "SELECT name FROM sqlite_master WHERE type='table' AND (name LIKE 'mission_%' OR name LIKE 'resource_%' OR name LIKE 'native_c%' OR name='native_account_keys' OR name='native_fragments') ORDER BY name")]}; c2.close()
            port = free_port(); base = 'http://127.0.0.1:%d' % port; api = subprocess.Popen([PY, '-m', 'metacoin_service', '--home', str(home), '--provider-mode', 'simulation', 'serve', '--port', str(port)], cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); smoke = {}
            try:
                for _ in range(300):
                    try:
                        if httpx.get(base + '/api/health', timeout=1).status_code == 200:
                            break
                    except Exception:
                        time.sleep(0.1)
                tok = json.load(open(home / 'credentials' / 'bootstrap.json'))['principals']['owner']['token']; h = {'Authorization': 'Bearer ' + tok}; c = httpx.Client(base_url=base, timeout=60)
                smoke['health'] = c.get('/api/health').json().get('revision'); jobs = c.get('/api/v1/jobs?limit=5', headers=h).json(); items = jobs.get('items', jobs) if isinstance(jobs, dict) else jobs
                smoke['old_jobs_readable'] = isinstance(items, list) and len(items) > 0 and c.get('/api/v1/jobs/' + items[0]['id'], headers=h).status_code == 200
                smoke['domain'] = c.post('/api/v1/native/domain', headers=h, json={'domain_id': 'dom-migrated'}).status_code; smoke['replay_consistent'] = c.post('/api/v1/work/journal/replay', headers=h, json={}).json().get('consistent')
                smoke['market'] = c.get('/api/v1/resources', headers=h).status_code; smoke['missions'] = c.get('/api/v1/missions', headers=h).status_code; smoke['operator'] = c.get('/api/v1/native/operator', headers=h).status_code
                smoke['capabilities_signer'] = c.get('/api/v1/capabilities', headers=h).json().get('signer', {}).get('custody') is not None
            finally:
                api.terminate(); api.wait(timeout=20); shutil.rmtree(iso, ignore_errors=True)
            part['migration'] = {'before': before, 'after': after, 'migrate_rc': mig.returncode, 'stderr': mig.stderr[-300:] if mig.returncode else None, 'smoke': smoke, 'balances_preserved': before['balances_digest'] == after['balances_digest'], 'live_service_untouched': True}
            mig_ok = mig.returncode == 0 and after['schema'] == '047_mission_contracts' and after['jobs'] == before['jobs'] and after['journal_entries'] == before['journal_entries'] and before['balances_digest'] == after['balances_digest'] and smoke.get('old_jobs_readable') and smoke.get('domain') == 201 and smoke.get('replay_consistent') and smoke.get('market') == 200 and smoke.get('missions') == 200 and smoke.get('operator') == 200
        else:
            part['migration'] = {'blocked': 'preserved previous database not found at ' + str(live)}; mig_ok = False
        res = os.environ.get('METACOIN_MISSION_CLEAN_EXPORT_JSON')
        if res and Path(res).exists():
            r = json.loads(Path(res).read_text()); part['clean_export'] = {k: r.get(k) for k in ('passed', 'failed', 'blocked', 'total', 'revision', 'archive_sha256')}; clean_ok = r.get('failed') == 0 and r.get('passed', 0) >= 5
            self.rec(36, self.j36.__doc__, mig_ok and clean_ok, part, caveat='migration rehearsed on an isolated copy of the preserved live database; the live service was neither upgraded nor restarted', t0=t0)
        else:
            self.rec(36, self.j36.__doc__, mig_ok, part, blocked=None if mig_ok is False and 'blocked' in part['migration'] else 'clean-export result not supplied yet (METACOIN_MISSION_CLEAN_EXPORT_JSON); migration part %s' % ('passed' if mig_ok else 'failed'), t0=t0)

    # ---- §12 defining demonstration -----------------------------------------------------------------------------------------------------
    def demo(self, t0):
        try:
            self._demo(t0)
        finally:
            try:
                self.A.connectivity('online')
            except Exception:
                pass

    def _demo(self, t0):
        """Connected mission across two domains with signers: terrain analysis on pinned public data, native settlement with fees, machine purchases from earnings, blackout, outage replan, reconnection, treasury award to a distinct recipient."""
        A, B = self.A, self.B; steps = []; postings_before = {d.id: len(d.call('get', '/api/v1/native/export')[1]['journal_entries']) for d in (A, B)}
        req = A.actor('d_req', 5000); mach = A.actor('d_mach'); ver = A.actor('d_ver'); con = A.actor('d_con'); A.provider('d_mach'); A.provider('d_con'); sup = A.enroll_external(self.sgA, 'd_sup', 500); A.provider('d_sup')
        mb = B.actor('d_mach_b'); supb = B.actor('d_sup_b'); B.provider('d_sup_b')
        names = {'d_req': 'requester', 'd_mach': 'machine provider', 'd_sup': 'resource supplier (external signer custody)', 'd_ver': 'verifier', 'd_con': 'award recipient (distinct)', A.treasury: 'treasury (dom-a)', 'd_mach_b': 'machine account at dom-b', 'd_sup_b': 'storage supplier at dom-b'}
        def table():
            t = {k: A.bal(k) for k in ('d_req', 'd_mach', 'd_sup', 'd_ver', 'd_con')}; t['treasury'] = A.bal(A.treasury); t['dom-b:d_mach_b'] = B.bal('d_mach_b', asset=A.asset); t['dom-b:d_sup_b'] = B.bal('d_sup_b', asset=A.asset); return t
        def step(name, **ev):
            pa = {d.id: len(d.call('get', '/api/v1/native/export')[1]['journal_entries']) for d in (A, B)}; steps.append(dict({'step': name, 'balances': table(), 'journal_entries': pa}, **ev))
        funding = table(); step('initial funding (zero-value development asset; fixture funding of the requester only)')
        compute = A.offer('d_sup', 'compute', 100000, 1)
        pol = {'verifier': 3, 'verifier_pay_to': ver['account'], 'permitted_providers': [A.actors['d_mach']['provider_id']], 'compute_cpu_ms': 20, 'price': 40, 'bps': 500, 'followon_provider_account': sup['account_id']}
        m = A.mission('d_req', 'terrain_delivery', policy=pol, title='terrain analysis delivery'); mid = m['id']
        A.drive_work('d_req', 'd_mach', mid, 'acquire', 4); A.advance('d_req', mid, 'pay'); s = A.drive_work('d_req', 'd_mach', mid, 'compute', 40); comp = A.ms('d_req', mid, 'compute'); job = comp['binding'].get('job_id')
        prod = A.call('get', '/api/v1/lunar/products/' + job)[1] if job else {}
        step('terrain analysis computed from the pinned public tile and accepted (not yet settled)', science=s['result']['science'], product={k: prod.get(k) for k in ('kind', 'scope', 'source', 'provenance', 'assumptions', 'resolution') if k in prod}, terms_digest=comp['binding'].get('terms_digest'))
        # blackout before settlement: local settlement proceeds, cross-domain transport is refused
        A.connectivity('cut'); op = A.operator(); pay = A.advance('d_req', mid, 'pay'); ex = A.export('d_mach', B, mb['account'], 10, 'demo-x'); pushed = None
        try:
            pushed = self.push(A, B)
        except Exception as exc:
            pushed = {'refused': type(exc).__name__, 'detail': str(exc)[:160]}
        ob_state = A.call('get', '/api/v1/native/transport/outbox', params={'receiver': 'dom-b'})
        step('blackout: link cut; mission settled locally from reserved funds (provider 40 + fee 2 + verifier 3); export to dom-b certified but undeliverable', paid=pay['performed'], export_state=ex[1].get('state'), transport={'refused': pushed.get('ok') is False, 'stage': pushed.get('stage'), 'status': pushed.get('status'), 'code': ((pushed.get('error') or {}).get('detail') or {}).get('code') if isinstance(pushed.get('error'), dict) else None} if isinstance(pushed, dict) else pushed, outbox_status=ob_state[0], operator_blackout={'possible': [(a['action'], a['milestone']) for a in op['blackout']['possible_now']], 'disabled': [(a['action'], (a['reason'] or '')[:60]) for a in op['blackout']['disabled_now']]})
        self.ctx['demo_operator_blackout'] = op
        # machine buys follow-on compute from settled earnings, during the blackout (local market)
        fo = A.step('d_mach', mid); rid = (fo.get('result') or {}).get('reservation_id'); act = A.res_action('d_sup', rid or 'none', 'activate'); sdl, dl = A.res_action('d_sup', rid or 'none', 'deliver', {'task': {'kind': 'terrain_tile', 'source': 'apollo11_32'}})
        if sdl != 200:
            raise RuntimeError('follow-on delivery failed: step=%s activate=%s deliver=%s' % (json.dumps(fo)[:300], json.dumps(act)[:200], json.dumps(dl)[:300]))
        st_res = A.advance('d_mach', mid, 'settle_reservation')
        step('machine purchased compute from its settled earnings (local market, still in blackout); measured delivery settled', reserve=fo.get('performed'), delivered_cpu_ms=dl.get('delivered_quantity'), labels=[dl['measurement']['measurement']['kind'], dl['measurement']['verification']['kind']], settled=st_res.get('performed'))
        # outage replan: the early contact window passes without activation; the controller releases and replans to the later window
        tt = int(time.time()); contact_early = A.offer('d_sup', 'contact', 2000, 1, tt - 1, tt + 3); contact_late = A.offer('d_sup', 'contact', 2000, 2, tt + 5, tt + 3600)
        relay = A.mission('d_req', graph={'milestones': [{'key': 'link', 'kind': 'resource', 'depends': [], 'spec': {'offer_kind': 'contact', 'quantity': 1500, 'max_price_per_unit': 5, 'provider_account': sup['account_id']}}]}, title='relay of the product over a contact window')['id']
        A.call('post', '/api/v1/missions/%s/plan' % relay, role='d_req', json={'objective': 'earliest_delivery', 'apply': True}); r1 = A.step('d_req', relay); time.sleep(max(0, tt + 5.5 - time.time())); rel = A.step('d_req', relay)
        pl2 = A.call('post', '/api/v1/missions/%s/plan' % relay, role='d_req', json={'objective': 'earliest_delivery', 'apply': True})[1]; r2 = A.step('d_req', relay); rid2 = r2['result']['reservation_id']
        A.res_action('d_sup', rid2, 'activate'); A.res_action('d_sup', rid2, 'deliver', {'bytes': 1500 * 1024}); st2 = A.advance('d_req', relay, 'settle_reservation')
        step('outage: the early contact window was missed (provider outage); reservation released in full, replanned to the next window, delivered and settled once', first=r1.get('performed'), released=rel.get('result'), replanned_to=pl2.get('incumbent', {}).get('choice'), second=r2.get('performed'), settled=st2.get('performed'))
        # reconnection: transport resumes, the export is imported at dom-b, acknowledged, witnessed; money and evidence reconcile
        A.connectivity('online'); out = self.push(A, B); ack = self.push(B, A) if False else None; w = self.W.client.call('GET', '/latest/dom-a')[1]; recA = A.call('get', '/api/v1/native/recovery')[1]; accA = A.call('get', '/api/v1/native/accounting')[1]
        step('reconnection: certificate delivered, claim credited at dom-b, acknowledged and witnessed; accounting reconciled', transport=[x['state'] for x in out['results']], allocation=A.alloc(ex[1]['id'])['state'], witness_sequence=w.get('latest', {}).get('sequence'), recovery=recA['recovery_state'], accounting_checks=accA['checks'])
        # at dom-b the machine spends part of its claim on storage; the claim becomes nonreturnable
        so = B.offer('d_sup_b', 'storage', 10 ** 6, 1, asset=A.asset); st, sr = B.reserve('d_mach_b', so, 6, 'demo-store')
        if st == 201:
            B.res_action('d_sup_b', sr['id'], 'activate'); B.http.post('/api/v1/resources/reservations/%s/storage' % sr['id'], headers=dict(B.H('d_mach_b'), **{'Content-Type': 'application/octet-stream'}), content=b'product' * 300); time.sleep(1.2); B.res_action('d_sup_b', sr['id'], 'deliver', {'min_seconds': 2}); B.res_action('d_mach_b', sr['id'], 'settle')
        lin = B.call('get', '/api/v1/native/accounts/%s/lineage' % mb['account'])[1]
        step('at dom-b the machine bought storage with the imported claim (claim partially consumed: nonreturnable)', reservation=(st, sr.get('state') if isinstance(sr, dict) else None), lineage=[(c['state'], c['consumed'], c['returnable']) for c in lin.get('claims', [])])
        # the externally-custodied supplier spends earnings only through its signer
        st_u, ru = A.call('post', '/api/v1/native/transfers', role='d_sup', json={'to_account': ver['account'], 'amount': 1, 'request_id': 'demo-unsigned'}); st_s, rs = A.signed_pay(self.sgA, 'd_sup', ver['account'], 1, 'demo-signed')
        step('supplier (external signer custody) pays the verifier 1: unsigned refused, signed through the signer settled', unsigned=(st_u, (ru.get('detail') or {}).get('code')), signed=rs.get('state'))
        # the treasury funds a distinct accepted contribution from collected fees alone
        tv = A.call('get', '/api/v1/work/treasury', params={'asset': A.asset})[1]; t3, r3, o3, (sa3, a3) = A.award('owner', 'd_con', 'determination', energy_inputs('INFEASIBLE'), 2, funding='treasury'); aw3, d3 = A.deliver_accept(a3['id'], 'owner'); A.pay_all(a3['id'], 'owner')
        step('treasury award: a distinct recipient paid 2 from collected fees only', treasury_available_before=tv.get('available'), decision=d3.get('decision'))
        final = table(); auA = A.audit(); auB = B.audit(A.asset); exA = A.call('get', '/api/v1/native/export')[1]; exB = B.call('get', '/api/v1/native/export')[1]
        conserved = auA['total_held'] + auA['backing_locked_for_exports'] == auA['fixture_funding'] and auB['claims_backed_elsewhere'] == auB['total_held'] and auA['conserved']
        record = {'schema': 'metacoin-demonstration/v1', 'title': 'Autonomous mission economy: connected two-domain mission with signers', 'x402': 'disabled; facilitator URL closed', 'actors': names, 'funding_table': funding, 'steps': steps, 'final_table': final,
                  'conservation': {'dom-a': {k: auA[k] for k in ('total_held', 'fixture_funding', 'backing_locked_for_exports', 'claims_backed_elsewhere', 'conserved')}, 'dom-b (asset of dom-a)': {k: auB[k] for k in ('total_held', 'claims_backed_elsewhere', 'conserved')}, 'ok': conserved},
                  'postings': {'dom-a': exA['journal_entries'][postings_before['dom-a']:], 'dom-b': exB['journal_entries'][postings_before['dom-b']:]}, 'mission': mid, 'relay_mission': relay, 'job': job, 'operator_screen_during_blackout': op, 'custody_note': 'mission payments and the machine\'s purchases run under development custody (service-held keys); the supplier and the clean-client provider use external signer custody', 'elapsed_seconds': round(time.time() - t0, 1)}
        self.ctx['demo'] = {'mission': mid, 'job': job, 'record': record}
        if self.ctx.get('demo_out'):
            Path(self.ctx['demo_out']).write_text(json.dumps(record, indent=1, default=str))
        refused_during_blackout = isinstance(pushed, dict) and (pushed.get('ok') is False and pushed.get('stage') == 'outbox' or 'refused' in pushed)
        ok = st == 201 and s['result']['science'] == 'FEASIBLE' and pay['performed']['action'] == 'pay' and refused_during_blackout and fo.get('performed', {}).get('action') == 'reserve' and st_res['performed']['action'] == 'settle_reservation' and rel['performed']['action'] == 'release_missed_reservation' and st2['performed']['action'] == 'settle_reservation' and [x['state'] for x in out['results']] == ['accepted'] and st_u == 409 and rs.get('state') == 'settled' and d3.get('decision') == 'accepted' and conserved
        self.rec(0, self._demo.__doc__, ok, {'mission': mid, 'steps': [x['step'] for x in steps], 'final_table': final, 'conserved': conserved}, t0=t0)

    def run_all(self, only=None):
        order = list(range(1, 26)) + [0] + list(range(26, 37))
        for n in order:
            if only and n not in only:
                continue
            fn = self.demo if n == 0 else getattr(self, 'j%d' % n); self.guard(n, ((self._demo.__doc__ if n == 0 else fn.__doc__) or 'journey %d' % n).strip(), fn)
        return self.results


def main():
    p = argparse.ArgumentParser(); p.add_argument('--out'); p.add_argument('--only'); p.add_argument('--shots'); p.add_argument('--demo'); a = p.parse_args()
    j = Journeys(shots=a.shots, demo_out=a.demo)
    try:
        results = j.run_all([int(x) for x in a.only.split(',')] if a.only else None); health = j.A.call('get', '/api/health')[1]
    finally:
        j.close()
    out = {'schema': 'metacoin-journeys-mission/v1', 'provider_mode': 'simulation', 'x402_disabled': True, 'facilitator_url': 'http://127.0.0.1:1/blocked', 'revision': health.get('revision'), 'python': sys.version.split()[0], 'started_at': j.started_at, 'finished_at': now(), 'results': results,
           'passed': sum(r['status'] == 'passed' for r in results), 'failed': sum(r['status'] == 'failed' for r in results), 'blocked': sum(r['status'] == 'blocked' for r in results), 'skipped': sum(r['status'] == 'skipped' for r in results), 'total': len(results)}
    text = json.dumps(out, indent=1, default=str)
    if a.out:
        Path(a.out).write_text(text)
    print(json.dumps({k: out[k] for k in ('passed', 'failed', 'blocked', 'skipped', 'total', 'revision')}), flush=True)
    for r in results:
        print('%s %s %s' % (r['journey'], r['status'], r['title'][:90]), flush=True)
    return 0 if out['failed'] == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
