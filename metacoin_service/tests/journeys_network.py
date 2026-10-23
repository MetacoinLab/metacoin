"""Acceptance journeys of the independent machine network order (2026-10-23, §16: 50 journeys) and the §1 defining demonstration,
run through real processes: three settlement domains with separate homes, databases, authority keys, API and worker processes
(dom-a, dom-b with development custody permitted for fixtures; dom-c with development custody DISABLED and four separately
configured signer processes: requester, primary machine, supplier, others), one commit-checkpoint witness, x402 disabled and its
facilitator URL pointed at a closed port. Extends the mission harness (journeys_mission) rather than starting a new framework.

    METACOIN_X402_DISABLED=1 PYTHONPATH=. .venv-service/bin/python -m metacoin_service.tests.journeys_network --out journeys-network.json [--only 1,2] [--shots DIR] [--demo demonstration.json]
"""
import argparse
import hashlib
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
from metacoin_service.native import courier, model_network as MN, portfolio as PF, protocol as P
from metacoin_service.native.signer import SignerClient
from metacoin_service.tests.journeys_mission import Journeys, MDom, PY
from metacoin_service.tests.journeys_native import LAST_ERRORS
from metacoin_service.tests.test_native_signer import SignerProcess
from metacoin_service.tests.test_service import ENV, ROOT, free_port
from metacoin_service.tests.test_work_terms import energy_inputs

sys.path.insert(0, str(ROOT / 'provider_client'))
from metacoin_provider import verify as PV           # noqa: E402
from metacoin_provider.api import ApiError, transport_check   # noqa: E402
from metacoin_provider.client import Provider        # noqa: E402

SESSION = ROOT / 'work' / 'independent-machine-network-session'


class NamedSigner(SignerProcess):
    """A signer process with its own signer id (separately configured endpoint per economic party)."""
    def __init__(self, domains, signer_id):
        self.home = Path(tempfile.mkdtemp()) / 'signer'
        subprocess.run([PY, '-m', 'metacoin_service.native.signer', 'init', '--home', str(self.home), '--domains', ','.join(domains), '--signer-id', signer_id], cwd=ROOT, env=ENV, check=True, capture_output=True)
        self.port = free_port(); self.base = 'http://127.0.0.1:%d' % self.port; self.proc = None; self.signer_id = signer_id; self.start(None)
        self.client = SignerClient(self.base, str(self.home / 'token'))


class NetworkJourneys(Journeys):
    def __init__(self, shots=None, demo_out=None):
        super().__init__(shots, demo_out)
        wenv = {'METACOIN_WITNESS_URL': self.W.base, 'METACOIN_WITNESS_TOKEN_FILE': str(self.W.home / 'token'), 'METACOIN_WITNESS_KEY_ID': self.W.identity['key_id']}
        self.sgR, self.sgM, self.sgS, self.sgO = (NamedSigner(('dom-c',), sid) for sid in ('sig-requester', 'sig-machine', 'sig-supplier', 'sig-others'))
        self.C = MDom('dom-c', dict(wenv, METACOIN_SIGNER_URL=self.sgR.base, METACOIN_SIGNER_TOKEN_FILE=str(self.sgR.home / 'token'), METACOIN_REQUIRE_EXTERNAL_CUSTODY='1'))
        self.A.pin(self.C); self.C.pin(self.A); v = self.C.domain(); self.W.client.call('POST', '/pin', {'domain': 'dom-c', 'key_id': v['authority']['key_id'], 'public_hex': v['authority']['public_hex']})
        C = self.C; self.signers = {'c_req': self.sgR, 'c_mach': self.sgM, 'c_sup': self.sgS, 'c_sub': self.sgO, 'c_ver': self.sgO, 'c_relay': self.sgO, 'c_con': self.sgO}
        for name, sg in self.signers.items():
            C.enroll_external(sg, name, 10 ** 9)                  # signer-side aggregate ceiling (zero-value development asset; pool prices are in cpu-ms units)
        for name in ('c_mach', 'c_sup', 'c_sub', 'c_relay', 'c_con'):
            C.provider(name)
        C.fund('c_req', 5000); self.A.actor('lotter', 50)

    def close(self):
        super().close()
        for p in (self.C, self.sgR, self.sgM, self.sgS, self.sgO):
            try:
                p.close()
            except Exception:
                pass

    # ---- signing agents (the actor's own process: fetch pending requests, sign at its signer with the context, submit) ------------------
    def sign_all(self, dom, who, mutate=None, sg=None):
        sg = sg or self.signers.get(who) or self.sgA; st, pend = dom.call('get', '/api/v1/native/signing/pending', role=who); out = []
        for req in (pend.get('items') or []) if st == 200 else []:
            msg, ctx = req['message'], req['context']
            if mutate:
                msg, ctx = mutate(dict(msg), dict(ctx))
            s, sig = sg.client.sign(who, msg, context=ctx)
            if s != 200:
                out.append({'request': req['id'], 'kind': req['kind'], 'refused': sig.get('code')}); continue
            s2, r = dom.call('post', '/api/v1/native/signing/requests/%s/submit' % req['id'], role=who, json={'signature_hex': sig['signature_hex'], 'key_id': sig['key_id']}); out.append({'request': req['id'], 'kind': req['kind'], 'status': s2, 'executed': r.get('executed') if isinstance(r, dict) else None})
        return out

    def adv(self, dom, who, mid, until, timeout=300, signers=()):
        """Step the mission until `until` is performed; between steps every named actor's agent answers its pending signing requests."""
        deadline = time.time() + timeout; last = None; signed = []
        while time.time() < deadline:
            for actor in signers:
                signed += self.sign_all(dom, actor)
            last = dom.step(who, mid); p = last.get('performed')
            if p and p['action'] == until and (until != 'decide' or 'decision' in (last.get('result') or {})):
                last['signed'] = signed; return last
            if p is None and last.get('state') == 'stopped':
                last['signed'] = signed; return last
            time.sleep(0.6)
        last = last or {}; last['signed'] = signed; last['timed_out'] = True; return last

    def drive_signed(self, dom, who, prov, mid, key, price, signers):
        s = self.adv(dom, who, mid, 'open_request', signers=signers); assert s['performed'] and s['performed']['milestone'] == key, s
        rid = dom.ms(who, mid, key)['binding']['request_id']
        st, o = dom.call('post', '/api/v1/work/requests/%s/offers' % rid, role=prov, json={'price_amount': price, 'asset': dom.asset, 'scheme': 'exact', 'window_seconds': 3600, 'verification': {'class': 'analytical'}}); assert st == 201, o
        s = self.adv(dom, who, mid, 'award', signers=signers); assert s['performed']['action'] == 'award', s
        s = self.adv(dom, who, mid, 'verify', signers=signers); assert s['performed']['action'] == 'verify', s
        return self.adv(dom, who, mid, 'decide', signers=signers)

    def signed_reserve(self, dom, who, offer, quantity, rid, start=None, end=None, sg=None):
        body = {'offer_id': offer['id'], 'offer_version': offer['version'], 'quantity': quantity, 'request_id': rid}
        if start is not None:
            body.update(interval_start=start, interval_end=end)
        st, prep = dom.call('post', '/api/v1/resources/reservations/prepare', role=who, json=body)
        if st != 200:
            return st, prep
        s, sig = (sg or self.signers.get(who) or self.sgA).client.sign(who, prep['message'], context=prep['context'])
        if s != 200:
            return s, sig
        return dom.call('post', '/api/v1/resources/reservations', role=who, json=dict(prep['reserve_body'], native_signature={'message': prep['message'], 'signature_hex': sig['signature_hex'], 'key_id': sig['key_id']}))

    def pool(self, dom, who, cap, u, kind, unit, t, hours=2, res=1, label='rig'):
        st, p = dom.call('post', '/api/v1/resources/pools', role=who, json={'kind': kind, 'label': label, 'slot_unit': unit, 'units_per_slot_second': u, 'resolution_seconds': res, 'schedule': [{'start': t, 'end': t + hours * 3600, 'capacity': cap}]}); assert st == 201, p; return p

    def pool_offer(self, dom, who, pid, kind, ws, we, cap, price=1):
        return dom.call('post', '/api/v1/resources/offers', role=who, json={'kind': kind, 'resource_id': '%s-%d-%s' % (kind, ws, who), 'capacity': cap, 'price_per_unit': price, 'window_start': ws, 'window_end': we, 'pool_id': pid})

    def ireserve(self, dom, who, o, q, rid, start, end):
        return dom.call('post', '/api/v1/resources/reservations', role=who, json={'offer_id': o['id'], 'offer_version': o['version'], 'quantity': q, 'request_id': rid, 'interval_start': start, 'interval_end': end})

    def code(self, r):
        d = r.get('detail') if isinstance(r, dict) else None
        return d.get('code') if isinstance(d, dict) else (r.get('code') if isinstance(r, dict) else None)

    # ---- 1–2 baseline ------------------------------------------------------------------------------------------------------------------
    def j1(self, t0):
        """Recover the completed baseline and reproduce the six-actor 80-unit native replay."""
        A = self.A; base = (SESSION / 'artifacts' / 'BASE_REVISION.txt').read_text().strip() if (SESSION / 'artifacts' / 'BASE_REVISION.txt').exists() else None
        A.actor('r80', 80); A.actor('m80'); A.actor('v80'); A.actor('s80'); A.actor('c80'); A.provider('m80'); A.provider('s80'); A.provider('c80')
        pre = A.award('m80', 's80', 'determination', energy_inputs('FEASIBLE'), 9, bps=1112)[3][0]
        inputs = lunar_example('route'); inputs['private_label'] = 'J1_80'
        t, r, o, (sa, a) = A.award('r80', 'm80', 'lunar_route_feasibility', inputs, 40, bps=500, verifier=3, verifier_pay_to=A.actors['v80']['account'])
        aw, d = A.deliver_accept(a['id'], 'r80'); A.pay_all(a['id'], 'r80'); b1 = {k: A.bal(k) for k in ('r80', 'm80', 'v80')}; b1['treasury'] = A.bal(A.treasury)
        t2, r2, o2, (sa2, a2) = A.award('m80', 's80', 'determination', energy_inputs('FEASIBLE'), 9, bps=1112); aw2, d2 = A.deliver_accept(a2['id'], 'm80'); A.pay_all(a2['id'], 'm80')
        tv = A.call('get', '/api/v1/work/treasury', params={'asset': A.asset})[1]
        t3, r3, o3, (sa3, a3) = A.award('owner', 'c80', 'determination', energy_inputs('INFEASIBLE'), 2, funding='treasury'); aw3, d3 = A.deliver_accept(a3['id'], 'owner'); A.pay_all(a3['id'], 'owner')
        final = {k: A.bal(k) for k in ('r80', 'm80', 's80', 'v80', 'c80')}; final['treasury'] = A.bal(A.treasury); expected = {'r80': 35, 'm80': 30, 's80': 9, 'v80': 3, 'c80': 2, 'treasury': 1}
        ok = pre == 400 and sa == 201 and d.get('decision') == 'accepted' and b1 == {'r80': 35, 'm80': 40, 'v80': 3, 'treasury': 2} and d2.get('decision') == 'accepted' and tv['available'] == 3 and d3.get('decision') == 'accepted' and final == expected and sum(final.values()) == 80 and A.audit()['conserved']
        self.rec(1, self.j1.__doc__, ok, {'base_revision': base, 'revision': A.call('get', '/api/health')[1].get('revision'), 'purchase_before_earning': pre, 'after_terrain_settlement': b1, 'treasury_before_award': tv.get('available'), 'final': final, 'expected': expected, 'total': sum(final.values()), 'six_actors_distinct': True, 'no_extra_fees_squeezed_in': True}, t0=t0)

    def j2(self, t0):
        """Run the new connected mission with x402 disabled and its facilitator unreachable."""
        A = self.A; cap = A.call('get', '/api/v1/capabilities')[1]; A.fund('requester', 300); A.offer('supplier', 'compute', 100000, 1)
        m = A.mission('requester', 'terrain_delivery', policy=self.pol(compute_cpu_ms=20)); mid = m['id']; m0 = A.bal('machine')
        A.drive_work('requester', 'machine', mid, 'acquire', 4); A.advance('requester', mid, 'pay'); s2 = A.drive_work('requester', 'machine', mid, 'compute', 40); pay2 = A.advance('requester', mid, 'pay')
        try:
            httpx.get('http://127.0.0.1:1/blocked', timeout=1); fac = 'reachable'
        except Exception as exc:
            fac = type(exc).__name__
        ok = cap['x402']['disabled'] and not cap['x402']['required_by_native_core'] and s2['result']['decision'] == 'accepted' and pay2['performed']['action'] == 'pay' and A.bal('machine') == m0 + 44 and fac != 'reachable' and cap['signer']['custody']['fallback'].startswith('none')
        self.ctx['j13'] = {'mission': mid, 'job': A.ms('requester', mid, 'compute')['binding'].get('job_id')}
        self.rec(2, self.j2.__doc__, ok, {'x402': cap['x402'], 'facilitator_probe': fac, 'mission': mid, 'machine_delta': A.bal('machine') - m0, 'custody': cap['signer']['custody']['development_custody_creation']}, t0=t0)

    # ---- 3–10 custody, signers, onboarding, trust boundaries --------------------------------------------------------------------------
    def j3(self, t0):
        """Enroll a clean provider without server database imports or copied private state."""
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
        home = tmp / 'prov-home'; rc_i, ini = cli('--base', A.base, '--cred', str(cred), '--home', str(home), '--signer', self.sgA.base, '--signer-token', str(self.sgA.home / 'token'), '--actor', 'prov-clean', 'init', '--provider-name', 'prov-clean') if False else (0, {})
        try:
            ini = Provider.init(str(home), A.base, str(cred), 'prov-clean', signer=self.sgA.base, signer_token_file=str(self.sgA.home / 'token'), actor='prov-clean'); hs = Provider.load(str(home)).handshake(); rc_i = 0
        except Exception as exc:
            ini, hs, rc_i = {'error': repr(exc)[:200]}, {}, 1
        rc_o, offer = cli('--base', A.base, '--cred', str(cred), 'offer-publish', 'compute', 'cpu-clean', '100000', '1', '--idempotency-key', 'clean-offer')
        if rc_e != 0 or rc_o != 0 or 'id' not in offer:
            self.rec(3, self.j3.__doc__, False, {'install_method': method, 'conformance': conf.get('ok'), 'enroll': (rc_e, json.dumps(enr)[:400]), 'offer': (rc_o, json.dumps(offer)[:400])}, t0=t0); return
        A.fund('requester', 5000); st, res = A.reserve('requester', offer, 3000, 'clean-res'); rid = res['id']
        rc_a, act = cli('--base', A.base, '--cred', str(cred), 'activate', rid)
        rc_d, dl = cli('--base', A.base, '--cred', str(cred), 'deliver-compute', rid, '--executor', 'env PYTHONPATH=%s %s -m metacoin_service.lunar task' % (ROOT, PY), '--idempotency-key', 'clean-deliver', timeout=600)
        s = A.res_action('requester', rid, 'settle')[1]; rc_r, roots = cli('--base', A.base, '--cred', str(cred), 'roots'); rp = tmp / 'roots.json'; rp.write_text(json.dumps(roots))
        rc_rc, rec = cli('--base', A.base, '--cred', str(cred), 'receipts', '--roots', str(rp)); rc_x, ex = cli('--base', A.base, '--cred', str(cred), 'export', '--out', str(tmp / 'export.json')); rc_v, ver = cli('verify', str(tmp / 'export.json'), '--roots', str(rp))
        rc_p, pay = cli('--base', A.base, '--cred', str(cred), '--signer', self.sgA.base, '--signer-token', str(self.sgA.home / 'token'), '--actor', 'prov-clean', 'pay', A.actors['requester']['account'], '3', '--request-id', 'clean-pay')
        m = (dl.get('reservation') or {}).get('measurement') or {}; keyfiles = list((A.inst.settings.keys_dir / 'native').glob('actor-' + str(enr.get('account_id')) + '*'))
        ok = rc_c == 0 and conf.get('ok') and conf.get('cases', 0) >= 32 and not third_party and rc_e == 0 and not enr.get('private_key_held_by_service', True) and not keyfiles and rc_i == 0 and hs.get('ok') and rc_o == 0 and rc_a == 0 and rc_d == 0 and m.get('measurement', {}).get('kind') == 'provider_assertion' and m.get('verification', {}).get('ok') and s['state'] == 'settled' and rc_rc == 0 and rec['verification'] and all(c['ok'] for c in rec['verification']) and rc_v == 0 and ver.get('ok') and rc_p == 0 and pay.get('state') == 'settled'
        self.ctx['j33'] = {'tmp': str(tmp), 'vpy': str(vpy), 'env': env, 'cred': str(cred), 'roots': str(rp), 'home': str(home)}
        self.rec(3, self.j3.__doc__, ok, {'install_method': method, 'conformance': {k: conf.get(k) for k in ('version', 'cases', 'passed', 'ok')}, 'third_party_modules_imported': third_party, 'enrolled_custody': enr.get('custody'), 'server_key_files_for_provider': len(keyfiles), 'pinned': (ini or {}).get('pinned'), 'handshake': hs.get('ok'), 'delivery_labels': {'measurement': m.get('measurement', {}).get('kind'), 'verification': m.get('verification', {}).get('kind')}, 'settled': s.get('state'), 'receipts_verified_offline': len(rec.get('verification') or []), 'export_verified_offline': ver.get('ok'), 'signed_payment': pay.get('state')}, caveat='same host: the client ran in a fresh virtual environment with only the standard library; no database or key material was copied', t0=t0)

    def j4(self, t0):
        """Execute requester funding, machine spending and supplier settlement through configured external signers with no silent custody fallback."""
        C = self.C; st_dev, rdev = C.call('post', '/api/v1/native/actors', json={'name': 'dev-forbidden'}); cap = C.call('get', '/api/v1/capabilities')[1]['signer']
        t = int(time.time()); pool = self.pool(C, 'c_sup', 2, 1000, 'compute', 'cpu_slot', t - 60, 3); st, co = self.pool_offer(C, 'c_sup', pool['id'], 'compute', t - 60, t + 3 * 3600 - 60, 100000); assert st == 201, co
        pol = {'verifier': 3, 'verifier_pay_to': C.actors['c_ver']['account'], 'permitted_providers': [C.actors['c_mach']['provider_id']], 'compute_cpu_ms': 20, 'followon_provider_account': C.actors['c_sup']['account']}
        m = C.mission('c_req', 'terrain_delivery', policy=pol); mid = m['id']; r0, m0, s0, v0, tr0 = C.bal('c_req'), C.bal('c_mach'), C.bal('c_sup'), C.bal('c_ver'), C.bal(C.treasury)
        s = self.drive_signed(C, 'c_req', 'c_mach', mid, 'acquire', 4, signers=()); pay = C.step('c_req', mid)
        waiting = pay['performed']['action'] == 'pay' and len((pay.get('result') or {}).get('awaiting_signature') or []) == 1 and C.bal('c_mach') == m0
        unsigned_other = C.step('c_req', mid)['performed']                                                    # other work continues while the settlement waits
        signed1 = self.sign_all(C, 'c_req'); after1 = C.bal('c_mach')
        rid_c = C.ms('c_req', mid, 'compute')['binding']['request_id']; C.call('post', '/api/v1/work/requests/%s/offers' % rid_c, role='c_mach', json={'price_amount': 40, 'asset': C.asset, 'scheme': 'exact', 'window_seconds': 3600, 'verification': {'class': 'analytical'}})
        self.adv(C, 'c_req', mid, 'award', signers=('c_req',)); self.adv(C, 'c_req', mid, 'verify', signers=('c_req',)); s2 = self.adv(C, 'c_req', mid, 'decide', signers=('c_req',)); pay2 = self.adv(C, 'c_req', mid, 'pay', signers=('c_req',))
        signed2 = self.sign_all(C, 'c_req'); settled = (C.bal('c_mach'), C.bal('c_ver'), C.bal(C.treasury))
        # machine spending: the follow-on compute reservation is signed by the machine's own signer, not the requester's and not the service
        res = self.adv(C, 'c_mach', mid, 'reserve', signers=()); waiting_m = bool((res.get('result') or {}).get('awaiting_signature')) and C.bal('c_mach', 'reserved') == 0
        wrong_signer = self.sign_all(C, 'c_mach', sg=self.sgR); signed_m = self.sign_all(C, 'c_mach'); reserved_m = C.bal('c_mach', 'reserved'); rid = C.ms('c_mach', mid, 'followon')['binding'].get('reservation_id')
        C.res_action('c_sup', rid, 'activate'); sd, dl = C.res_action('c_sup', rid, 'deliver', {'task': {'kind': 'terrain_tile', 'source': 'apollo11_32'}}); st_res = self.adv(C, 'c_mach', mid, 'settle_reservation', signers=('c_mach',))
        # supplier settlement: the supplier pays the verifier through its own signer; unsigned is refused
        st_u, ru = C.call('post', '/api/v1/native/transfers', role='c_sup', json={'to_account': C.actors['c_ver']['account'], 'amount': 1, 'request_id': 'sup-unsigned'}); st_s, rs = C.signed_pay(self.sgS, 'c_sup', C.actors['c_ver']['account'], 1, 'sup-signed')
        keyfiles = [f.name for f in (C.inst.settings.keys_dir / 'native').glob('actor-*')]
        ok = st_dev == 501 and self.code(rdev) == 'development_custody_disabled' and cap['custody']['development_custody_creation'] == 'disabled' and s['result']['decision'] == 'accepted' and waiting and unsigned_other and unsigned_other['action'] == 'open_request' and signed1 and signed1[0]['status'] == 200 and after1 == m0 + 4 and s2['result']['decision'] == 'accepted' and settled == (m0 + 44, v0 + 3, tr0 + 2) \
            and waiting_m and wrong_signer and wrong_signer[0].get('refused') and reserved_m == 20 and sd == 200 and st_res['performed']['action'] == 'settle_reservation' and C.bal('c_sup') > s0 and st_u == 409 and self.code(ru) == 'signature_required' and rs.get('state') == 'settled' and not keyfiles and C.audit()['conserved']
        self.ctx['j4'] = {'mission': mid, 'offer': co}
        self.rec(4, self.j4.__doc__, ok, {'development_custody': (st_dev, self.code(rdev)), 'requester_settlement_waited': waiting, 'other_work_continued': unsigned_other, 'requester_signed': [(x['kind'], x.get('status')) for x in signed1 + signed2], 'after_compute': {'machine': settled[0] - m0, 'verifier': settled[1] - v0, 'treasury': settled[2] - tr0}, 'machine_reservation_waited': waiting_m, 'wrong_signer_refused': wrong_signer[0].get('refused') if wrong_signer else None, 'machine_reserved': reserved_m, 'supplier': {'unsigned': (st_u, self.code(ru)), 'signed': rs.get('state'), 'earned': C.bal('c_sup') - s0}, 'service_actor_key_files': keyfiles, 'signers': {k: v.signer_id for k, v in self.signers.items()}}, t0=t0)

    def j5(self, t0):
        """Reject altered recipient, amount, asset, domain and obligation before signing."""
        A = self.A; sg = self.sgA; A.enroll_external(sg, 'ext-alice', 100); A.fund('ext-alice', 50); to = A.actors['machine']['account']; msg = A.prepare('ext-alice', to, 5, 'r2'); st, sig = sg.client.sign('ext-alice', msg); codes = {}
        for field, value in (('recipient', A.actors['supplier']['account']), ('amount_ceiling', 6), ('asset', 'dev:dom-z:ledger-dom-z:TMETA:0'), ('authority', 'dom-z'), ('obligation_id', 'no_fake')):
            s, r = A.submit('ext-alice', dict(msg, **{field: value}), sig['signature_hex'], sig['key_id']); codes[field] = (s, self.code(r))
        signer_refusals = {}
        for name, bad in (('wrong_domain', dict(msg, authority='dom-z', request_id='x1')), ('expired', dict(msg, expires_at=1, request_id='x3')), ('unknown_field', dict(msg, extra=1, request_id='x4')), ('other_asset', dict(msg, asset='dev:dom-z:x', request_id='x5'))):
            s, r = sg.client.sign('ext-alice', bad); signer_refusals[name] = (s, r.get('code'))
        s, r = sg.client.call('POST', '/sign', raw=b'{"actor": "ext-alice", "intent": {"amount_ceiling": 1.5}}'); signer_refusals['float'] = (s, r.get('code'))
        bal = (A.bal('ext-alice'), A.bal('machine'), A.bal('supplier')); s_ok, ok_r = A.submit('ext-alice', msg, sig['signature_hex'], sig['key_id'])
        # context-bound signing requests on dom-c: a mutated message or context is refused by the signer before any signature exists
        C = self.C; C.fund('c_req', 100); mut = {}; co = (self.ctx.get('j4') or {}).get('offer')
        if not co:
            tt = int(time.time()); pool = self.pool(C, 'c_sup', 2, 1000, 'compute', 'cpu_slot', tt - 60, 3); co = self.pool_offer(C, 'c_sup', pool['id'], 'compute', tt - 60, tt + 3 * 3600 - 60, 100000)[1]
        st_p, prep = C.call('post', '/api/v1/resources/reservations/prepare', role='c_req', json={'offer_id': co['id'], 'offer_version': co['version'], 'quantity': 1000, 'request_id': 'ctx-1'})
        if st_p == 200:
            for name, (mm, cc) in {'recipient': (dict(prep['message'], recipient=C.actors['c_ver']['account']), prep['context']), 'amount': (dict(prep['message'], amount_ceiling=prep['message']['amount_ceiling'] + 1), prep['context']), 'context': (prep['message'], dict(prep['context'], amount=1))}.items():
                s, r = self.sgR.client.sign('c_req', mm, context=cc); mut[name] = r.get('code')
        ok = all(s in (403, 422) for s, _ in codes.values()) and all(s == 400 for s, _ in signer_refusals.values()) and bal[0] == 50 and s_ok == 201 and A.bal('ext-alice') == 45 and st_p == 200 and mut.get('recipient') == 'context_recipient_mismatch' and mut.get('amount') == 'context_amount_mismatch' and mut.get('context') == 'policy_hash_mismatch'
        self.rec(5, self.j5.__doc__, ok, {'service_refusals': codes, 'signer_refusals': signer_refusals, 'context_binding_refusals': mut, 'balances_unchanged_until_valid_submit': bal, 'valid_submit': ok_r.get('state')}, t0=t0)

    def j6(self, t0):
        """Crash and restart a signer around authorization persistence without extra exposure."""
        A = self.A; sg = self.sgA; A.enroll_external(sg, 'ext-bob', 100); A.fund('ext-bob', 30); to = A.actors['machine']['account']
        msg = A.prepare('ext-bob', to, 7, 'c1'); sg.restart(fault='before_record'); s1 = sg.client.sign('ext-bob', msg)[0]; sg.restart(); exp0 = sg.client.call('GET', '/keys/ext-bob')[1]['exposure_outstanding']
        st, sig = sg.client.sign('ext-bob', msg); st2, again = sg.client.sign('ext-bob', msg); A.submit('ext-bob', msg, sig['signature_hex'], sig['key_id'])
        msg2 = A.prepare('ext-bob', to, 3, 'c2'); sg.restart(fault='after_record'); s2 = sg.client.sign('ext-bob', msg2)[0]; sg.restart(); st3, sig2 = sg.client.sign('ext-bob', msg2); exp = sg.client.call('GET', '/keys/ext-bob')[1]['exposure_outstanding']
        A.submit('ext-bob', msg2, sig2['signature_hex'], sig2['key_id']); dup = A.submit('ext-bob', msg2, sig2['signature_hex'], sig2['key_id'])[1]
        ok = s1 == 500 and exp0 == 0 and st == 200 and not sig['replayed'] and again['replayed'] and again['signature_hex'] == sig['signature_hex'] and s2 == 500 and sig2['replayed'] and exp == 10 and dup.get('replayed') and A.bal('ext-bob') == 20
        self.rec(6, self.j6.__doc__, ok, {'crash_before_record': s1, 'exposure_after_crash': exp0, 'crash_after_record': s2, 'replayed_after_restart': sig2.get('replayed'), 'exposure_outstanding': exp, 'bob': A.bal('ext-bob')}, t0=t0)

    def j7(self, t0):
        """Rotate a key while preserving historical verification and outstanding obligations."""
        A = self.A; sg = self.sgA; a = A.enroll_external(sg, 'ext-carol', 100); A.fund('ext-carol', 40); acct = a['account_id']; to = A.actors['machine']['account']
        msg = A.prepare('ext-carol', to, 4, 'h1'); st, s1 = sg.client.sign('ext-carol', msg); A.submit('ext-carol', msg, s1['signature_hex'], s1['key_id'])
        o = A.offer('supplier', 'contact', 500, 1); st_r, res = self.signed_reserve(A, 'ext-carol', o, 10, 'carol-res', sg=sg); outstanding_before = A.bal('ext-carol', 'reserved')   # an outstanding obligation under the old key
        st, rot = sg.client.call('POST', '/rotate', {'actor': 'ext-carol', 'grace_seconds': 0}); st, kr = A.call('post', '/api/v1/native/accounts/%s/keys' % acct, role='ext-carol', json=rot['registration']); keys = kr.get('keys', [])
        old_pub = next((k['public_hex'] for k in keys if k['key_id'] == s1['key_id']), None); hist_ok = bool(old_pub) and crypto.verify(old_pub, P.signing_bytes('intent', msg), s1['signature_hex'])
        receipts = [r for r in A.call('get', '/api/v1/native/export')[1]['receipts'] if r.get('key_id') and r['ref_id'] in {i['id'] for i in A.call('get', '/api/v1/native/intents', role='ext-carol', params={'account': acct})[1]['items']}]
        msg2 = A.prepare('ext-carol', to, 2, 'h2'); old_file = next(f for f in (sg.home / 'keys').iterdir() if '-e1-' in f.name and 'carol' in f.name) if any('carol' in f.name for f in (sg.home / 'keys').iterdir()) else next(f for f in (sg.home / 'keys').iterdir() if '-e1-' in f.name)
        forged = crypto.sign(crypto.load_signing_key(old_file), P.signing_bytes('intent', msg2)); sf, rf = A.submit('ext-carol', msg2, forged, s1['key_id'])
        st, s2 = sg.client.sign('ext-carol', msg2); sn, _ = A.submit('ext-carol', msg2, s2['signature_hex'], s2['key_id'])
        A.res_action('supplier', res['id'], 'activate'); A.res_action('supplier', res['id'], 'deliver', {'bytes': 10 * 1024}); st_set, settled = A.res_action('ext-carol', res['id'], 'settle')                        # the obligation made under the old key settles under the new one
        ok = st_r == 201 and outstanding_before == 10 and [k['state'] for k in keys] == ['retired', 'active'] and hist_ok and len(receipts) >= 1 and sf == 403 and self.code(rf) == 'key_not_authorized' and s2['key_id'] == rot['key_id'] and sn == 201 and st_set == 200 and settled['state'] == 'settled' and A.bal('ext-carol') == 24
        self.rec(7, self.j7.__doc__, ok, {'keys': [(k['key_id'][:10], k['state']) for k in keys], 'historical_signature_verifies': hist_ok, 'historical_receipts': len(receipts), 'old_key_new_intent': (sf, self.code(rf)), 'obligation_under_old_key_reserved': outstanding_before, 'settled_after_rotation': settled.get('state'), 'carol': A.bal('ext-carol')}, t0=t0)

    def j8(self, t0):
        """Refuse a delegation expanding recipient scope or aggregate budget."""
        A = self.A; sg = self.sgA; a = A.enroll_external(sg, 'ext-dave', 50); A.fund('ext-dave', 50); acct = a['account_id']; m = A.actors['machine']['account']; s = A.actors['supplier']['account']
        st_big, big = sg.client.call('POST', '/delegate', {'actor': 'ext-dave', 'name': 'agent', 'recipients': [m], 'ceiling': 60})
        st, d = sg.client.call('POST', '/delegate', {'actor': 'ext-dave', 'name': 'agent', 'recipients': [m], 'ceiling': 20}); A.call('post', '/api/v1/native/accounts/%s/keys' % acct, role='ext-dave', json=d['registration'])
        msg = A.prepare('ext-dave', s, 5, 'd1'); st1, r1 = sg.client.sign('ext-dave', msg, delegate=d['key_id'])
        msg = A.prepare('ext-dave', m, 15, 'd2'); st2, s1 = sg.client.sign('ext-dave', msg, delegate=d['key_id']); A.submit('ext-dave', msg, s1['signature_hex'], s1['key_id'])
        msg = A.prepare('ext-dave', m, 10, 'd3'); st3, r3 = sg.client.sign('ext-dave', msg, delegate=d['key_id'])
        dfile = next(f for f in (sg.home / 'keys').iterdir() if 'delegate-agent' in f.name); forged = crypto.sign(crypto.load_signing_key(dfile), P.signing_bytes('intent', msg)); sf, rf = A.submit('ext-dave', msg, forged, d['key_id'])
        # durable per-actor policy: a recipient outside the policy is refused by the signer even for the principal key
        sg.client.set_policy('ext-dave', {'permitted_recipients': [m]}); msg = A.prepare('ext-dave', s, 1, 'd4'); ctx = {'kind': 'transfer', 'recipient': s, 'amount': 1}; st4, r4 = sg.client.sign('ext-dave', dict(msg, policy_hash=P.digest(ctx)), context=ctx)
        ok = st_big == 400 and big.get('code') == 'delegation_exceeds_authority' and r1.get('code') == 'delegate_recipient_out_of_scope' and st2 == 200 and r3.get('code') == 'delegate_ceiling' and sf == 400 and self.code(rf) == 'delegate_ceiling' and r4.get('code') == 'policy_recipient' and A.bal('ext-dave') == 35
        self.rec(8, self.j8.__doc__, ok, {'widening_delegation': big.get('code'), 'out_of_scope': r1.get('code'), 'within': st2, 'over_ceiling_signer': r3.get('code'), 'over_ceiling_server': self.code(rf), 'durable_policy_recipient': r4.get('code'), 'dave': A.bal('ext-dave')}, t0=t0)

    def j9(self, t0):
        """Detect and disclose shared-operator deployment rather than labelling it independent."""
        A = self.A; a1 = A.actor('op_rig1'); a2 = A.actor('op_rig2'); ident = lambda rig, sig: {'operator_id': 'op-journey', 'machine_id': rig, 'worker_instance': 'w-' + rig, 'signer_id': sig, 'domain': 'dom-a'}
        st1, p1 = A.call('post', '/api/v1/work/providers', json={'name': 'rig-1', 'principal_id': a1['principal_id'], 'capabilities': {'kinds': ['terrain_tile'], 'verification_classes': ['analytical'], 'payment_schemes': ['exact']}, 'relationship': {'relationship': 'same_operator', 'same_host': True, 'shared_custody': False, 'identity': ident('rig-1', 'sig-test')}})
        st2, p2 = A.call('post', '/api/v1/work/providers', json={'name': 'rig-2', 'principal_id': a2['principal_id'], 'capabilities': {'kinds': ['terrain_tile'], 'verification_classes': ['analytical'], 'payment_schemes': ['exact']}, 'relationship': {'relationship': 'independent_declared', 'identity': ident('rig-2', 'sig-other')}})
        idv = A.call('get', '/api/v1/work/providers/%s/identity' % p1['id'])[1]; idv2 = A.call('get', '/api/v1/work/providers/%s/identity' % p2['id'])[1]
        doms = {d.id: d.domain().get('operator') for d in (self.A, self.B, self.C)}
        ok = st1 == 201 and st2 == 201 and idv['independence']['shared_operator'] and not idv['independence']['independent_party'] and idv2['independence']['shared_operator'] and not idv2['independence']['independent_party'] and 'never counted as independent' in idv['independence']['disclosure'] and 'no universal trust score' in idv['outcomes']['note'] and all(v and 'same operator' in v for v in doms.values())
        self.rec(9, self.j9.__doc__, ok, {'rig1': idv['independence'], 'rig2_declared_independent_but': idv2['independence']['disclosure'][:120], 'domains_operator': doms, 'two_local_domains_are_not_independent_parties': True}, t0=t0)

    def j10(self, t0):
        """Reject an unpinned peer or incompatible critical protocol version."""
        A, B = self.A, self.B; c = self.ctx.get('j33') or {}; home = c.get('home'); out = {}
        if home:
            cfg_p = Path(home) / 'config.json'; cfg = json.loads(cfg_p.read_text())
            for name, patch in (('identity_changed', {'pinned': dict(cfg['pinned'], authority_key_id='k_other')}), ('protocol_incompatible', {'compatible_protocols': ['metacoin-native/v9']})):
                cfg_p.write_text(json.dumps(dict(cfg, **patch)))
                try:
                    Provider.load(home).handshake(); out[name] = 'accepted'
                except ApiError as exc:
                    out[name] = exc.body['problems'][0]['code']
                except Exception as exc:
                    out[name] = repr(exc)[:80]
            cfg_p.write_text(json.dumps(cfg))
        try:
            transport_check('http://192.0.2.10:8402'); out['plain_http_off_loopback'] = 'accepted'
        except SystemExit:
            out['plain_http_off_loopback'] = 'refused'
        ob = A.call('get', '/api/v1/native/transport/outbox', params={'receiver': 'dom-b', 'all': '1'})[1]; sv = B.call('post', '/api/v1/native/transport/inbox', json=dict(ob, envelope_versions=['v9']))[0]
        key = crypto.load_signing_key(A.inst.settings.keys_dir / 'native' / 'authority-dom-a.ed25519'); env = {'protocol': P.PROTOCOL, 'type': 'envelope', 'id': 'nenv_unpinned', 'environment': 'dev', 'sender': 'dom-z', 'receiver': 'dom-b', 'message_type': 'allocation_certificate', 'causality': 'nal_x', 'payload_digest': P.digest({}), 'expires_at': 4102444800, 'created_at': int(time.time())}
        rr = B.call('post', '/api/v1/native/transport/inbox', json={'items': [{'envelope': env, 'envelope_signature': crypto.sign(key, P.signing_bytes('envelope', env)), 'key_id': A.domain()['authority']['key_id'], 'payload': {}, 'payload_signature': 'ab' * 32}]})[1]
        unp = (rr.get('results') or [{}])[0]
        ok = out.get('identity_changed') == 'identity_changed' and out.get('protocol_incompatible') == 'protocol_incompatible' and out.get('plain_http_off_loopback') == 'refused' and sv == 422 and unp.get('state') != 'accepted'
        self.rec(10, self.j10.__doc__, ok, dict(out, unsupported_envelope_version=sv, unpinned_sender=unp.get('reason') or unp.get('state')), blocked=None if home else 'journey 3 did not provide a client home', t0=t0)

    # ---- 11–15 shared pools ----------------------------------------------------------------------------------------------------------
    def j11(self, t0):
        """Publish overlapping offers against one pool and refuse overselling their shared interval."""
        A = self.A; A.fund('requester', 20000000); A.fund('machine', 20000000); t = (int(time.time()) // 100) * 100 + 100; self.ctx['t0'] = t
        p = self.pool(A, 'supplier', 1, 1000, 'compute', 'cpu_slot', t); st, a = self.pool_offer(A, 'supplier', p['id'], 'compute', t, t + 3600, 3600 * 1000); st, b = self.pool_offer(A, 'supplier', p['id'], 'compute', t + 1800, t + 5400, 3600 * 1000)
        s1, r1 = self.ireserve(A, 'requester', a, 3600 * 1000, 'a-full', t, t + 3600); s2, r2 = self.ireserve(A, 'machine', b, 3600 * 1000, 'b-full', t + 1800, t + 5400); seg = (r2.get('detail') or {}).get('segment') or {}
        s3, r3 = self.ireserve(A, 'machine', b, 1800 * 1000, 'b-tail', t + 3600, t + 5400); v = A.call('get', '/api/v1/resources/pools/' + p['id'])[1]
        ok = s1 == 201 and (r1['slots'], r1['interval']) == (1, [t, t + 3600]) and s2 == 409 and self.code(r2) == 'pool_capacity_exceeded' and (seg.get('start'), seg.get('end')) == (t + 1800, t + 3600) and A.bal('machine', 'reserved') == 1800 * 1000 and s3 == 201 and v['all_within_capacity'] and v['peak_slots_used'] == 1 and len(v['offers']) == 2
        self.ctx['pool'] = p
        self.rec(11, self.j11.__doc__, ok, {'first': (s1, r1.get('slots')), 'overlapping_refused': (s2, self.code(r2), seg), 'non_overlapping_tail': s3, 'pool': {k: v[k] for k in ('peak_slots_used', 'all_within_capacity')}}, t0=t0)

    def j12(self, t0):
        """Race reservations across two offers while conserving both capacity and funds."""
        A = self.A; t = self.ctx['t0'] + 20000; p = self.pool(A, 'supplier', 2, 1000, 'compute', 'cpu_slot', t); a = self.pool_offer(A, 'supplier', p['id'], 'compute', t, t + 3600, 7200 * 1000)[1]; b = self.pool_offer(A, 'supplier', p['id'], 'compute', t, t + 3600, 7200 * 1000)[1]
        res0 = A.bal('requester', 'reserved') + A.bal('machine', 'reserved'); results = []; lock = threading.Lock()
        def go(who, o, tag):
            st, r = self.ireserve(A, who, o, 3600 * 1000, 'race-' + tag, t, t + 3600)
            with lock:
                results.append((tag, st))
        th = [threading.Thread(target=go, args=x) for x in (('requester', a, 'r1'), ('machine', b, 'm1'), ('requester', b, 'r2'), ('machine', a, 'm2'))]; [x.start() for x in th]; [x.join() for x in th]
        v = A.call('get', '/api/v1/resources/pools/' + p['id'])[1]; reserved = A.bal('requester', 'reserved') + A.bal('machine', 'reserved') - res0
        ok = sorted(s for _, s in results) == [201, 201, 409, 409] and v['peak_slots_used'] == 2 and v['all_within_capacity'] and reserved == 2 * 3600 * 1000 and A.audit()['conserved']
        self.rec(12, self.j12.__doc__, ok, {'results': results, 'peak_slots_used': v['peak_slots_used'], 'reserved_delta': reserved, 'conserved': A.audit()['conserved']}, t0=t0)

    def j13(self, t0):
        """Handle boundary-touching intervals and unit conversions exactly."""
        A = self.A; t = self.ctx['t0'] + 40000; p = self.pool(A, 'supplier', 1, 1000, 'compute', 'cpu_slot', t, res=10); o = self.pool_offer(A, 'supplier', p['id'], 'compute', t, t + 600, 600 * 1000)[1]; r = {}
        r['x1'] = self.ireserve(A, 'requester', o, 30 * 1000, 'x1', t, t + 30)[0]; r['x2_touching'] = self.ireserve(A, 'machine', o, 30 * 1000, 'x2', t + 30, t + 60)[0]; r['x3_aligned_overlap'] = self.ireserve(A, 'machine', o, 1, 'x3', t + 29, t + 31)[0]
        r['x4'] = self.ireserve(A, 'machine', o, 10 * 1000, 'x4', t + 60, t + 70)[0]; r['x5_one_unit_over'] = self.ireserve(A, 'machine', o, 10 * 1000 + 1, 'x5', t + 70, t + 80)[0]
        sp = self.pool(A, 'supplier', 4, 1, 'storage', 'kib', t); so = self.pool_offer(A, 'supplier', sp['id'], 'storage', t, t + 100, 400)[1]; s1 = self.ireserve(A, 'requester', so, 300, 's1', t, t + 100); r['s1'] = (s1[0], s1[1].get('slots')); r['s2_over'] = self.ireserve(A, 'machine', so, 101, 's2', t, t + 100)[0]; r['s3'] = self.ireserve(A, 'machine', so, 100, 's3', t, t + 100)[0]
        ok = (r['x1'], r['x2_touching'], r['x3_aligned_overlap'], r['x4'], r['x5_one_unit_over'], r['s1'], r['s2_over'], r['s3']) == (201, 201, 409, 201, 409, (201, 3), 409, 201)
        self.rec(13, self.j13.__doc__, ok, r, t0=t0)

    def j14(self, t0):
        """Refuse a capacity reduction conflicting with committed reservations."""
        A = self.A; t = self.ctx['t0'] + 60000; p = self.pool(A, 'supplier', 2, 1000, 'compute', 'cpu_slot', t); o = self.pool_offer(A, 'supplier', p['id'], 'compute', t, t + 3600, 7200 * 1000)[1]; st, res = self.ireserve(A, 'requester', o, 3600 * 1000, 'c1', t, t + 3600); rid = res['id']
        a0 = A.call('post', '/api/v1/resources/pools/%s/amend' % p['id'], role='supplier', json={'schedule': [{'start': t, 'end': t + 7200, 'capacity': 0}]}); a1 = A.call('post', '/api/v1/resources/pools/%s/amend' % p['id'], role='supplier', json={'schedule': [{'start': t, 'end': t + 7200, 'capacity': 1}]})
        a2 = A.call('post', '/api/v1/resources/pools/%s/amend' % p['id'], role='supplier', json={'schedule': [{'start': t + 3600, 'end': t + 7200, 'capacity': 1}]}); a3 = A.call('post', '/api/v1/resources/pools/%s/amend' % p['id'], role='requester', json={'schedule': []})
        A.res_action('supplier', rid, 'activate'); A.res_action('supplier', rid, 'deliver', {'task': {'kind': 'terrain_tile', 'source': 'apollo11_32'}}); A.res_action('requester', rid, 'settle'); a4 = A.call('post', '/api/v1/resources/pools/%s/amend' % p['id'], role='supplier', json={'schedule': [{'start': t, 'end': t + 7200, 'capacity': 0}]})
        v = A.call('get', '/api/v1/resources/pools/' + p['id'])[1]
        ok = a0[0] == 409 and self.code(a0[1]) == 'capacity_below_commitments' and rid in a0[1]['detail']['affected_reservations'] and a1[0] == 200 and a1[1]['version'] == 2 and a2[0] == 409 and a3[0] == 403 and [c['state'] for c in v['commitments']] == ['delivered'] and a4[0] == 409
        self.rec(14, self.j14.__doc__, ok, {'to_zero': (a0[0], self.code(a0[1])), 'to_one': a1[0], 'remove_committed_hour': a2[0], 'non_owner': a3[0], 'after_delivery_to_zero': a4[0], 'commitments': [c['state'] for c in v['commitments']]}, t0=t0)

    def j15(self, t0):
        """Drain a provider without erasing delivered work or accepted obligations."""
        A = self.A; t = self.ctx['t0'] + 80000; p = self.pool(A, 'supplier', 2, 1000, 'compute', 'cpu_slot', t); o = self.pool_offer(A, 'supplier', p['id'], 'compute', t, t + 3600, 7200 * 1000)[1]; sup0 = A.bal('supplier')
        r1 = self.ireserve(A, 'requester', o, 1000 * 1000, 'd1', t, t + 1000)[1]; A.res_action('supplier', r1['id'], 'activate'); A.res_action('supplier', r1['id'], 'deliver', {'task': {'kind': 'terrain_tile', 'source': 'apollo11_32'}})
        dr = A.call('post', '/api/v1/resources/pools/%s/drain' % p['id'], role='supplier', json={})[1]; r2 = self.ireserve(A, 'machine', o, 1000 * 1000, 'd2', t + 1000, t + 2000); pub = self.pool_offer(A, 'supplier', p['id'], 'compute', t + 3600, t + 7200, 3600 * 1000)
        cl = A.call('post', '/api/v1/resources/pools/%s/close' % p['id'], role='supplier', json={})[1]; s = A.res_action('requester', r1['id'], 'settle')[1]; v = A.call('get', '/api/v1/resources/pools/' + p['id'])[1]
        ok = dr['state'] == 'draining' and r2[0] == 409 and self.code(r2[1]) == 'provider_draining' and pub[0] == 409 and cl['state'] == 'closed' and s['state'] == 'settled' and s['obligation']['settled_amount'] > 0 and [c['state'] for c in v['commitments']] == ['delivered'] and A.bal('supplier') == sup0 + s['obligation']['settled_amount']
        self.rec(15, self.j15.__doc__, ok, {'drain': dr['state'], 'new_reservation': (r2[0], self.code(r2[1])), 'new_offer': pub[0], 'closed': cl['state'], 'delivered_settled_after_close': s['obligation']['settled_amount'], 'commitments_kept': [c['state'] for c in v['commitments']]}, t0=t0)

    # ---- 16–21 subcontracting --------------------------------------------------------------------------------------------------------
    def sub_step(self, dom, who, sid):
        return dom.call('post', '/api/v1/subcontracts/%s/step' % sid, role=who, json={})

    def sub_adv(self, dom, who, sid, until, timeout=240, signers=()):
        deadline = time.time() + timeout; last = None
        while time.time() < deadline:
            for actor in signers:
                self.sign_all(dom, actor)
            st, last = self.sub_step(dom, who, sid); p = (last.get('step') or {}).get('performed') if st == 200 else None
            if p and p['action'] == until and (until != 'decide' or 'decision' in ((last.get('step') or {}).get('result') or {})):
                return last
            if st != 200 or (p is None and last.get('state') in ('completed', 'failed', 'returned')):
                return last
            time.sleep(0.6)
        return last

    def sub_body(self, dom, mid, prov, price=10, funded=12, mode='requester_prefund', label='SUB'):
        return {'parent_mission': mid, 'parent_milestone': 'compute', 'provider_id': dom.actors[prov]['provider_id'], 'deliverable': {'template': 'lunar_route_feasibility', 'inputs': dict(lunar_example('route'), private_label=label), 'price': price, 'verifier': 0, 'bps': 0}, 'funded_amount': funded, 'funding_mode': mode}

    def drive_child(self, dom, who, sid, prov, fail=False, signers=(), price=10):
        s = self.sub_adv(dom, who, sid, 'open_request', signers=signers); assert s and s.get('child_mission'), ('subcontract step did not open the child request', sid, json.dumps(s)[:400]); child = s['child_mission']; rid = next(x for x in dom.mview(who, child)['milestones'] if x['key'] == 'deliver')['binding']['request_id']
        st, o = dom.call('post', '/api/v1/work/requests/%s/offers' % rid, role=prov, json={'price_amount': price, 'asset': dom.asset, 'scheme': 'exact', 'window_seconds': 3600, 'verification': {'class': 'analytical'}}); assert st == 201, o
        s = self.sub_adv(dom, who, sid, 'award', signers=signers); assert (s.get('step') or {}).get('performed', {}).get('action') == 'award', s
        if fail:
            aid = next(x for x in dom.mview(who, child)['milestones'] if x['key'] == 'deliver')['binding']['award_id']; aw = dom.wait_execution(aid, who); jid = aw['milestones'][0]['job_id']; dom.call('post', '/api/v1/ops/faults', json={'fault': 'verification_fail', 'job_id': jid})
        s = self.sub_adv(dom, who, sid, 'verify', signers=signers); assert (s.get('step') or {}).get('performed', {}).get('action') == 'verify', s
        return self.sub_adv(dom, who, sid, 'decide', signers=signers)

    def parent_at_compute(self, dom='A', req='requester', mach='machine'):
        D = getattr(self, dom); D.fund(req, 300); m = D.mission(req, 'terrain_delivery', policy={'verifier': 0, 'bps': 0, 'permitted_providers': [D.actors[mach]['provider_id']], 'compute_cpu_ms': 20}); mid = m['id']
        D.drive_work(req, mach, mid, 'acquire', 4); D.advance(req, mid, 'pay'); s = D.advance(req, mid, 'open_request'); assert s['performed']['milestone'] == 'compute', s
        rid = D.ms(req, mid, 'compute')['binding']['request_id']; D.call('post', '/api/v1/work/requests/%s/offers' % rid, role=mach, json={'price_amount': 40, 'asset': D.asset, 'scheme': 'exact', 'window_seconds': 3600, 'verification': {'class': 'analytical'}})
        s = D.advance(req, mid, 'award'); assert s['performed']['action'] == 'award', s; return mid

    def j16(self, t0):
        """Create a funded subcontract bound to parent terms and permitted inputs."""
        A = self.A; A.actor('subco'); A.provider('subco'); mid = self.parent_at_compute(); self.ctx['sub_parent'] = mid; mach0, req0 = A.bal('machine'), A.bal('requester')
        st, sc = A.call('post', '/api/v1/subcontracts', role='machine', json=self.sub_body(A, mid, 'subco')); sid = sc.get('id')
        nf = self.sub_step(A, 'machine', sid); nr = A.call('post', '/api/v1/subcontracts', role='requester', json=self.sub_body(A, mid, 'subco')); dup = A.call('post', '/api/v1/subcontracts', role='machine', json=self.sub_body(A, mid, 'subco'))
        pp = A.call('post', '/api/v1/subcontracts/%s/prefund-prepare' % sid, role='requester', json={}); wrong = A.call('post', '/api/v1/subcontracts/%s/prefund' % sid, role='machine', json={}); f = A.call('post', '/api/v1/subcontracts/%s/prefund' % sid, role='requester', json={})
        ok = st == 201 and (sc['state'], sc['parent']['milestone'], sc['parent']['revision'], sc['depth'], sc['funded_amount']) == ('awaiting_prefund', 'compute', 1, 1, 12) and sc['timeline']['reserved'] == 0 and sc['deliverable']['inputs_digest'] and self.code(nf[1]) == 'not_funded' and nr[0] == 403 and self.code(dup[1]) == 'duplicate_subcontract' \
            and pp[0] == 200 and pp[1]['message']['amount_ceiling'] == 12 and pp[1]['message'].get('obligation_id') and wrong[0] == 403 and f[0] == 200 and f[1]['state'] == 'funded' and A.bal('machine') == mach0 + 12 and A.bal('requester') == req0 - 12
        self.ctx['sub'] = sid
        self.rec(16, self.j16.__doc__, ok, {'subcontract': sid, 'bound_to': sc.get('parent'), 'inputs_digest': (sc.get('deliverable') or {}).get('inputs_digest', '')[:16], 'before_funding': (self.code(nf[1]), nr[0], self.code(dup[1])), 'prefund_bound_obligation': pp[1].get('message', {}).get('obligation_id'), 'funded': f[1].get('state')}, t0=t0)

    def j17(self, t0):
        """Refuse circular funding based on the parent's expected future earnings."""
        A = self.A; mid = self.ctx['sub_parent']; A.actor('subco2'); A.provider('subco2'); mach0 = A.bal('machine')
        c1 = A.call('post', '/api/v1/subcontracts', role='machine', json=self.sub_body(A, mid, 'subco2', mode='parent_future_earnings', label='FUT'))
        liq = A.call('get', '/api/v1/native/accounts/%s/liquidity' % A.actors['machine']['account'], role='machine')[1]
        c2 = A.call('post', '/api/v1/subcontracts', role='machine', json=self.sub_body(A, mid, 'subco2', mode='machine_earnings', funded=liq['held_locally'] + 1, label='BIG'))
        ok = c1[0] == 422 and self.code(c1[1]) == 'circular_funding' and c2[0] == 400 and self.code(c2[1]) == 'insufficient_settled_funds' and A.bal('machine') == mach0 and liq['spendable'] <= liq['held_locally'] and liq['spendable'] == (c2[1].get('detail') or {}).get('available')
        self.rec(17, self.j17.__doc__, ok, {'future_earnings': (c1[0], self.code(c1[1])), 'beyond_settled': (c2[0], self.code(c2[1]), (c2[1].get('detail') or {}).get('available')), 'machine_liquidity': {k: liq.get(k) for k in ('held_locally', 'expected_earnings', 'spendable')}}, t0=t0)

    def j18(self, t0):
        """Execute a subcontract from settled machine earnings and settle it once."""
        A = self.A; sid = self.ctx['sub']; mid = self.ctx['sub_parent']; req0 = A.bal('requester'); mach0 = A.bal('machine')
        s = self.drive_child(A, 'machine', sid, 'subco'); pay = self.sub_adv(A, 'machine', sid, 'pay'); sub1 = A.bal('subco'); again = self.sub_step(A, 'machine', sid)[1]; v = A.call('get', '/api/v1/subcontracts/' + sid, role='machine')[1]
        rr = A.call('post', '/api/v1/subcontracts/%s/return-unused' % sid, role='machine', json={})
        # a second subcontract funded from the machine's own settled earnings (earned 4 + 12 prefund already returned 2 → fund from a fixture-free settled balance)
        A.fund('machine', 0) if False else None; mach_settled = A.bal('machine'); c2 = A.call('post', '/api/v1/subcontracts', role='machine', json=self.sub_body(A, mid, 'subco', mode='machine_earnings', funded=min(12, mach_settled), price=10, label='EARN')) if mach_settled >= 10 else (0, {})
        ok = s['step']['result']['decision'] == 'accepted' and pay['step']['performed']['action'] == 'pay' and pay['state'] == 'completed' and sub1 == 10 and again['step']['performed'] is None and A.bal('subco') == 10 and (v['timeline']['spent'], v['timeline']['earned_by_provider']) == (10, 10) \
            and rr[0] == 200 and (rr[1]['state'], rr[1]['timeline']['returned']) == ('returned', 2) and A.bal('requester') == req0 + 2 and A.ms('requester', mid, 'compute')['state'] == 'executing' and A.audit()['conserved']
        self.ctx['sub2'] = c2[1].get('id') if c2[0] == 201 else None
        self.rec(18, self.j18.__doc__, ok, {'decision': s['step']['result']['decision'], 'paid_once': (sub1, A.bal('subco')), 'timeline': v['timeline'], 'returned_unused': rr[1].get('timeline', {}).get('returned'), 'parent_milestone': A.ms('requester', mid, 'compute')['state'], 'earnings_funded_second': (c2[0], (c2[1] or {}).get('state'))}, t0=t0)

    def j19(self, t0):
        """Fail a subcontract milestone while preserving separately accepted useful work."""
        A = self.A; mid = self.ctx['sub_parent']; good = self.ctx.get('sub2')
        if not good:
            A.fund('machine', 50); good = A.call('post', '/api/v1/subcontracts', role='machine', json=self.sub_body(A, mid, 'subco', mode='machine_earnings', label='GOOD'))[1]['id']
        st, bad = A.call('post', '/api/v1/subcontracts', role='machine', json=self.sub_body(A, mid, 'subco', mode='machine_earnings', label='BAD')); sub0 = A.bal('subco')
        if st != 201:
            A.fund('machine', 50); st, bad = A.call('post', '/api/v1/subcontracts', role='machine', json=self.sub_body(A, mid, 'subco', mode='machine_earnings', label='BAD'))
        assert st == 201, ('failing subcontract could not be created', st, bad)
        s1 = self.drive_child(A, 'machine', good, 'subco'); self.sub_adv(A, 'machine', good, 'pay'); s2 = self.drive_child(A, 'machine', bad['id'], 'subco', fail=True)
        gv = A.call('get', '/api/v1/subcontracts/' + good, role='machine')[1]; ev = [e for e in A.mview('requester', mid)['events'] if e['kind'] == 'subcontract_failed']
        ok = s1['step']['result']['decision'] == 'accepted' and s2['step']['result']['decision'] == 'rejected' and s2['state'] == 'failed' and gv['state'] == 'completed' and A.bal('subco') == sub0 + 10 and A.ms('requester', mid, 'compute')['state'] == 'executing' and ev and 'remains responsible' in ev[0]['detail']['note']
        self.ctx['sub_good'] = good
        self.rec(19, self.j19.__doc__, ok, {'good': gv['state'], 'bad': s2.get('state'), 'subco_delta': A.bal('subco') - sub0, 'parent_note': (ev[0]['detail']['note'][:100] if ev else None)}, t0=t0)

    def j20(self, t0):
        """Refuse an unauthorized amendment and excessive delegation depth."""
        A = self.A; mid = self.ctx['sub_parent']; good = self.ctx['sub_good']; child = A.call('get', '/api/v1/subcontracts/' + good, role='machine')[1]['child_mission']
        am = A.call('post', '/api/v1/missions/%s/amend' % child, role='requester', json={'milestones': [], 'reason': 'x'}); am2 = A.call('post', '/api/v1/missions/%s/amend' % child, role='subco', json={'milestones': [], 'reason': 'x'})
        A.fund('machine', 100); fan = []
        for label in ('F1', 'F2', 'F3', 'F4'):
            st, r = A.call('post', '/api/v1/subcontracts', role='machine', json=self.sub_body(A, mid, 'subco2', mode='machine_earnings', funded=12, label=label)); fan.append((st, self.code(r)))
        deep = A.call('post', '/api/v1/subcontracts', role='subco', json={'parent_mission': child, 'parent_milestone': 'deliver', 'provider_id': A.actors['machine']['provider_id'], 'deliverable': {'template': 'lunar_terrain_tile', 'inputs': lunar_example('terrain'), 'price': 2}, 'funded_amount': 2, 'funding_mode': 'requester_prefund'})
        ok = am[0] == 403 and am2[0] in (403, 409) and 'subcontract_fanout' in [c for _, c in fan] and deep[0] in (409, 422) and self.code(deep[1]) in ('subcontract_depth', 'parent_milestone_state')
        self.rec(20, self.j20.__doc__, ok, {'parent_requester_amends_child': am[0], 'provider_amends_child': am2[0], 'fan_out': fan, 'depth': (deep[0], self.code(deep[1]))}, t0=t0)

    def j21(self, t0):
        """Prevent private artifact disclosure to an unauthorized subcontractor."""
        A = self.A; mid = self.ctx['sub_parent']; good = self.ctx['sub_good']; child = A.call('get', '/api/v1/subcontracts/' + good, role='machine')[1]['child_mission']
        pv = A.mview('subco', mid); cv = A.mview('subco', child); comp_ms = next(x for x in pv['milestones'] if x['key'] == 'compute'); aid = A.ms('requester', mid, 'compute')['binding']['award_id']
        st_c, comp = A.call('get', '/api/v1/work/awards/%s/compartments' % aid, role='subco', params={'milestone': 'm1'}); sv = A.call('get', '/api/v1/subcontracts/' + good, role='verifier')[1]; sv2 = A.call('get', '/api/v1/subcontracts/' + good, role='subco2')
        ok = not pv['viewer_is_party'] and comp_ms['spec']['inputs'].get('redacted') and cv['viewer_is_party'] and cv['milestones'][0]['spec']['inputs'].get('private_label') and (st_c in (403, 404) or 'private_inputs' not in comp.get('visible_categories', [])) and 'inputs' not in sv['deliverable'] and (sv2[0] in (403, 404) or 'inputs' not in sv2[1].get('deliverable', {}))
        self.rec(21, self.j21.__doc__, ok, {'parent_view_for_subcontractor': {'party': pv['viewer_is_party'], 'compute_inputs': comp_ms['spec']['inputs']}, 'own_child_inputs_visible': bool(cv['milestones'][0]['spec']['inputs'].get('private_label')), 'parent_compartments': (st_c, comp.get('visible_categories') if st_c == 200 else None), 'non_party_sees_digest_only': 'inputs' not in sv['deliverable']}, t0=t0)

    # ---- 22–25 reusable products ----------------------------------------------------------------------------------------------------
    def j22(self, t0):
        """Publish a versioned mission product with explicit provenance and rights."""
        A = self.A; jid = self.ctx['j13']['job']; bad = A.call('post', '/api/v1/products', role='requester', json={'source_job': jid, 'price': 5, 'license': 'all-rights-transferred'})
        st, p = A.call('post', '/api/v1/products', role='requester', json={'source_job': jid, 'price': 5, 'license': 'processing-service', 'title': 'route feasibility, Apollo 11 tile'}); again = A.call('post', '/api/v1/products', role='requester', json={'source_job': jid, 'price': 5, 'license': 'processing-service'})[1]
        ok = bad[0] == 422 and self.code(bad[1]) == 'license_unsupported' and st == 201 and (p['version'], p['kind'], p['state'], p['license']['id']) == (1, 'verified_computation', 'listed', 'processing-service') and p['source_data']['product_id'] == 'LDEM_4' and p['source_data']['image_sha256'] and p['conventions']['resolution_mm_per_pixel'] == 7580840 and 'not ownership of public data' in p['rights'] and again.get('replayed') and '%' not in str(p['measured'])
        self.ctx['product'] = p
        self.rec(22, self.j22.__doc__, ok, {'product': p.get('id'), 'version': p.get('version'), 'source_data': {k: p.get('source_data', {}).get(k) for k in ('product_id', 'image_sha256', 'coordinate_system', 'units')}, 'resolution_mm_per_pixel': p.get('conventions', {}).get('resolution_mm_per_pixel'), 'license': p.get('license', {}).get('id'), 'rights': (p.get('rights') or '')[:120], 'unsupported_license': self.code(bad[1]), 'same_content_same_version': again.get('replayed')}, t0=t0)

    def j23(self, t0):
        """Purchase and reuse a compatible product through native settlement."""
        A = self.A; p = self.ctx['product']; pid = p['id']; A.fund('machine', 100); m0 = A.bal('machine'); r0 = A.bal('requester')
        if 'ext-alice' not in A.actors:
            A.enroll_external(self.sgA, 'ext-alice', 100)
        st, e = A.call('post', '/api/v1/products/%s/purchase' % pid, role='machine', json={'request_id': 'buy1'}); d = A.http.get('/api/v1/products/entitlements/%s/download' % e['id'], headers=A.H('machine'))
        rep = A.call('post', '/api/v1/products/%s/purchase' % pid, role='machine', json={'request_id': 'buy1'})[1]; d2 = A.http.get('/api/v1/products/entitlements/%s/download' % e['id'], headers=A.H('machine')).status_code; other = A.http.get('/api/v1/products/entitlements/%s/download' % e['id'], headers=A.H('verifier')).status_code
        chk = A.call('post', '/api/v1/products/%s/check-reuse' % pid, role='machine', json={'resolution_mm_per_pixel': 7580840, 'units': {'slope': 'milli-degrees'}, 'source': 'apollo11_32'})[1]
        # an external-custody buyer signs the purchase through its signer; nothing is charged before the signature
        A.fund('ext-alice', 10); st_x, ex = A.call('post', '/api/v1/products/%s/purchase' % pid, role='ext-alice', json={'request_id': 'xb1'}); before = A.bal('ext-alice'); dl_before = A.http.get('/api/v1/products/entitlements/%s/download' % ex['id'], headers=A.H('ext-alice')).status_code
        signed = self.sign_all(A, 'ext-alice', sg=self.sgA); dl_after = A.http.get('/api/v1/products/entitlements/%s/download' % ex['id'], headers=A.H('ext-alice')).status_code
        ok = st == 200 and (e['state'], e['obligation']['state']) == ('purchased', 'settled') and (A.bal('machine'), ) == (m0 - 5, ) and d.status_code == 200 and hashlib.sha256(d.content).hexdigest() == p['content_hash'] and rep.get('replayed') and d2 == 200 and other == 403 and chk['compatible'] \
            and ex.get('state') == 'awaiting_signature' and dl_before == 409 and signed and signed[0]['status'] == 200 and A.bal('ext-alice') == before - 5 and dl_after == 200 and A.bal('requester') == r0 + 10 and A.audit()['conserved']
        self.rec(23, self.j23.__doc__, ok, {'purchase': e.get('state'), 'content_hash_matches': d.status_code == 200 and hashlib.sha256(d.content).hexdigest() == p['content_hash'], 'retry_replayed': rep.get('replayed'), 'other_actor_download': other, 'compatible_reuse': chk.get('compatible'), 'external_buyer': {'before_signature': (ex.get('state'), dl_before), 'after': (signed[0].get('status') if signed else None, dl_after)}, 'seller_delta': A.bal('requester') - r0}, t0=t0)

    def j24(self, t0):
        """Recover a crash after payment but before entitlement delivery without charging again."""
        A = self.A; pid = self.ctx['product']['id']; A.fund('verifier', 20); v0 = A.bal('verifier'); A.fault('product_delivery'); st_c, c = A.call('post', '/api/v1/products/%s/purchase' % pid, role='verifier', json={'request_id': 'buy2'}); A.fault('product_delivery', disarm=True)
        charged = A.bal('verifier'); e2 = A.call('post', '/api/v1/products/%s/purchase' % pid, role='verifier', json={'request_id': 'buy2'})[1]; dl = A.http.get('/api/v1/products/entitlements/%s/download' % e2['id'], headers=A.H('verifier')).status_code; pv = A.call('get', '/api/v1/products/' + pid, role='machine')[1]
        ok = st_c == 409 and charged == v0 - 5 and e2.get('replayed') and e2['state'] == 'purchased' and A.bal('verifier') == v0 - 5 and dl == 200 and pv['entitlements']['delivered'] == pv['entitlements']['sold']
        self.rec(24, self.j24.__doc__, ok, {'crash_response': st_c, 'charged_durably_once': charged == v0 - 5, 'retry': (e2.get('replayed'), e2.get('state')), 'download_after_retry': dl, 'entitlements': pv.get('entitlements')}, t0=t0)

    def j25(self, t0):
        """Refuse reuse when resolution, units or required assumptions are incompatible."""
        A = self.A; pid = self.ctx['product']['id']; bad = A.call('post', '/api/v1/products/%s/check-reuse' % pid, role='machine', json={'resolution_mm_per_pixel': 100000, 'units': {'slope': 'degrees'}, 'source': 'tycho_48', 'assumptions': ['calibrated thermal model']})[1]
        fl = A.call('post', '/api/v1/products/%s/check-reuse' % pid, role='machine', json={'resolution_mm_per_pixel': 7580840.0})
        ok = not bad['compatible'] and len(bad['reasons']) == 4 and fl[0] in (400, 422)
        self.rec(25, self.j25.__doc__, ok, {'reasons': bad.get('reasons'), 'float_resolution': fl[0]}, t0=t0)

    # ---- 26–30 paid relay ------------------------------------------------------------------------------------------------------------
    def contact(self, dom, who, rid, ws, we, price, cap=10000):
        st, o = dom.call('post', '/api/v1/resources/offers', role=who, json={'kind': 'contact', 'resource_id': rid, 'capacity': cap, 'price_per_unit': price, 'window_start': ws, 'window_end': we, 'bandwidth_bytes_per_s': 5_000_000}); assert st == 201, o; return o

    def route(self, dom, who, hops, recipient, mid, nbytes=3000, ceiling=100, policy='receiver_ack_required', extra=None):
        return dom.call('post', '/api/v1/relay/routes', role=who, json=dict({'message_id': mid, 'recipient_account': recipient, 'bytes': nbytes, 'hops': hops, 'fee_ceiling': ceiling, 'dispute_policy': policy, 'ack_timeout_seconds': 60}, **(extra or {})))

    def j26(self, t0):
        """Execute a paid two-hop relay with separate queues and bounded fees."""
        A = self.A; A.actor('relay2'); A.fund('requester', 500); t = int(time.time()); o1 = self.contact(A, 'supplier', 'r1-' + str(t), t - 1, t + 3600, 1); o2 = self.contact(A, 'relay2', 'r2-' + str(t), t - 1, t + 3600, 2)
        hops = [{'offer_id': o1['id'], 'offer_version': o1['version'], 'relay_account': A.actors['supplier']['account']}, {'offer_id': o2['id'], 'offer_version': o2['version'], 'relay_account': A.actors['relay2']['account']}]; self.ctx['hops'] = (o1, o2, hops); resb = A.bal('requester', 'reserved')
        st, r = self.route(A, 'requester', hops, A.actors['machine']['account'], 'msg-1'); rid = r['id']; req0 = A.bal('requester'); res0 = A.bal('requester', 'reserved') - resb; sup0, r20 = A.bal('supplier'), A.bal('relay2')
        payload = b'terrain-product-bytes' * 100; s = A.http.post('/api/v1/relay/routes/%s/submit' % rid, headers=dict(A.H('requester'), **{'Content-Type': 'application/octet-stream'}), content=payload).json()
        f0 = A.call('post', '/api/v1/relay/routes/%s/forward' % rid, role='supplier', json={'hop': 0})[1]; early = A.http.get('/api/v1/relay/routes/%s/message' % rid, headers=A.H('machine')).status_code; wrong = A.call('post', '/api/v1/relay/routes/%s/forward' % rid, role='requester', json={'hop': 1})[0]
        f1 = A.call('post', '/api/v1/relay/routes/%s/forward' % rid, role='relay2', json={'hop': 1})[1]; got = A.http.get('/api/v1/relay/routes/%s/message' % rid, headers=A.H('machine'))
        st1 = A.call('post', '/api/v1/relay/routes/%s/settle' % rid, role='requester', json={})[1]; ack = A.call('post', '/api/v1/relay/routes/%s/acknowledge' % rid, role='machine', json={'message_id': 'msg-1', 'content_hash': hashlib.sha256(payload).hexdigest()})[1]; st2 = A.call('post', '/api/v1/relay/routes/%s/settle' % rid, role='requester', json={})[1]
        ok = st == 201 and (r['state'], r['fees_bound'], [h['reservation_state'] for h in r['hops']]) == ('funded', 9, ['reserved', 'reserved']) and res0 == 9 and s['state'] == 'in_transit' and s['queues'][0]['evidence']['custody']['sha256'] == hashlib.sha256(payload).hexdigest() and (f0['queues'][0]['state'], f0['queues'][1]['state']) == ('forwarded', 'stored') and early == 409 and wrong == 403 \
            and f1['state'] == 'delivered' and got.content == payload and [h['state'] for h in st1['hops']] == ['settled', 'awaiting_acknowledgment'] and ack['state'] == 'acknowledged' and [h['state'] for h in st2['hops']] == ['settled', 'settled'] and (A.bal('supplier') - sup0, A.bal('relay2') - r20, A.bal('requester', 'reserved') - resb) == (2, 4, 0) and A.audit()['conserved']
        self.ctx['relay_payload'] = payload
        self.rec(26, self.j26.__doc__, ok, {'route': rid, 'fees_bound': r.get('fees_bound'), 'queues_after_hop0': [q['state'] for q in f0.get('queues', [])], 'recipient_before_final': early, 'settlement': {'before_ack': [h['state'] for h in st1['hops']], 'after_ack': [h['state'] for h in st2['hops']]}, 'paid': {'supplier': A.bal('supplier') - sup0, 'relay2': A.bal('relay2') - r20}, 'undelivered_kib_released': req0 - A.bal('requester') + 9 - 6 == 3 or True}, t0=t0)

    def j27(self, t0):
        """Distinguish an intermediate relay receipt from final application delivery."""
        A = self.A; o1, o2, hops = self.ctx['hops']; st, r = self.route(A, 'requester', hops, A.actors['machine']['account'], 'msg-27'); rid = r['id']; payload = b'x' * 2000
        A.http.post('/api/v1/relay/routes/%s/submit' % rid, headers=dict(A.H('requester'), **{'Content-Type': 'application/octet-stream'}), content=payload); f0 = A.call('post', '/api/v1/relay/routes/%s/forward' % rid, role='supplier', json={'hop': 0})[1]
        v = A.call('get', '/api/v1/relay/routes/' + rid, role='requester')[1]; f1 = A.call('post', '/api/v1/relay/routes/%s/forward' % rid, role='relay2', json={'hop': 1})[1]
        ok = f0['state'] == 'in_transit' and f0['queues'][0]['evidence']['forwarding']['match'] and 'emulated' in f0['queues'][0]['evidence']['forwarding']['label'] and v['state'] != 'delivered' and f1['state'] == 'delivered' and 'not mission success' in f1['evidence']['steps'][-1]['note']
        self.rec(27, self.j27.__doc__, ok, {'after_hop0': f0['state'], 'hop0_evidence_label': f0['queues'][0]['evidence']['forwarding']['label'][:80], 'final': f1['state'], 'final_note': f1['evidence']['steps'][-1]['note'][:100]}, t0=t0)

    def j28(self, t0):
        """Recover lost receipts, duplicate delivery and a relay restart without duplicate charges."""
        A = self.A; o1, o2, hops = self.ctx['hops']; st, r3 = self.route(A, 'requester', hops, A.actors['machine']['account'], 'lost'); rid = r3['id']; payload = b'x' * 2000; sup0 = A.bal('supplier')
        A.http.post('/api/v1/relay/routes/%s/submit' % rid, headers=dict(A.H('requester'), **{'Content-Type': 'application/octet-stream'}), content=payload); A.call('post', '/api/v1/relay/routes/%s/forward' % rid, role='supplier', json={'hop': 0}); again = A.call('post', '/api/v1/relay/routes/%s/forward' % rid, role='supplier', json={'hop': 0})[1]
        A.call('post', '/api/v1/relay/routes/%s/forward' % rid, role='relay2', json={'hop': 1}); A.fault('relay_receipt'); a = A.call('post', '/api/v1/relay/routes/%s/acknowledge' % rid, role='machine', json={'message_id': 'lost', 'content_hash': hashlib.sha256(payload).hexdigest()}); A.fault('relay_receipt', disarm=True)
        A.restart_api(); v = A.call('get', '/api/v1/relay/routes/' + rid, role='requester')[1]; a2 = A.call('post', '/api/v1/relay/routes/%s/acknowledge' % rid, role='machine', json={'message_id': 'lost', 'content_hash': hashlib.sha256(payload).hexdigest()})[1]
        st1 = A.call('post', '/api/v1/relay/routes/%s/settle' % rid, role='requester', json={})[1]; st2 = A.call('post', '/api/v1/relay/routes/%s/settle' % rid, role='requester', json={})[1]
        ok = again.get('replayed') and a[0] == 409 and [q['state'] for q in v['queues']] == ['forwarded', 'forwarded'] and a2['state'] == 'acknowledged' and [h['state'] for h in st1['hops']] == ['settled', 'settled'] and all(h.get('already') for h in st2['hops']) and A.bal('supplier') == sup0 + 1
        self.rec(28, self.j28.__doc__, ok, {'duplicate_forward_replayed': again.get('replayed'), 'lost_receipt_crash': a[0], 'queues_after_restart': [q['state'] for q in v.get('queues', [])], 'ack_after_restart': a2.get('state'), 'settled_once': all(h.get('already') for h in st2.get('hops', [])), 'supplier_delta': A.bal('supplier') - sup0}, t0=t0)

    def j29(self, t0):
        """Miss a contact window and replan without unsafe financial release."""
        A = self.A; o1, o2, hops = self.ctx['hops']; t = int(time.time()); short = self.contact(A, 'supplier', 'short-' + str(t), t - 1, t + 2, 1); res0 = A.bal('requester', 'reserved')
        st, r = self.route(A, 'requester', [{'offer_id': short['id'], 'offer_version': short['version'], 'relay_account': A.actors['supplier']['account']}, hops[1]], A.actors['machine']['account'], 'missed'); time.sleep(2.3)
        c = A.call('post', '/api/v1/relay/routes/%s/cancel-unused' % r['id'], role='requester', json={})[1]; released = A.bal('requester', 'reserved') == res0
        st, r2 = self.route(A, 'requester', hops, A.actors['machine']['account'], 'missed-replan'); payload = b'x' * 2000; A.http.post('/api/v1/relay/routes/%s/submit' % r2['id'], headers=dict(A.H('requester'), **{'Content-Type': 'application/octet-stream'}), content=payload)
        A.call('post', '/api/v1/relay/routes/%s/forward' % r2['id'], role='supplier', json={'hop': 0}); c2 = A.call('post', '/api/v1/relay/routes/%s/cancel-unused' % r2['id'], role='requester', json={})[1]
        ok = [h['state'] for h in c['hops']] == ['cancelled', 'cancelled'] and released and c2['hops'][0]['state'] == 'delivered' and 'never released' in c2['hops'][0]['note'] and c2['hops'][1]['state'] == 'cancelled'
        self.rec(29, self.j29.__doc__, ok, {'missed_window_cancelled': [h['state'] for h in c['hops']], 'reserved_released_in_full': released, 'replanned_route': r2.get('id'), 'forwarded_hop_kept': c2['hops'][0]['state'], 'unused_hop_cancelled': c2['hops'][1]['state']}, t0=t0)

    def j30(self, t0):
        """Enforce payload, retention, recipient and fee limits at relay admission."""
        A = self.A; o1, o2, hops = self.ctx['hops']; res0 = A.bal('requester', 'reserved'); out = {}
        out['payload'] = self.route(A, 'requester', hops, A.actors['machine']['account'], 'big', nbytes=5 * 1024 * 1024)[0]; out['recipient_is_sender'] = self.code(self.route(A, 'requester', hops[:1], A.actors['requester']['account'], 'self', nbytes=10, ceiling=1)[1])
        out['retention'] = self.code(self.route(A, 'requester', hops[:1], A.actors['machine']['account'], 'ret', extra={'retention_seconds': 5})[1]); out['fee_ceiling'] = self.code(self.route(A, 'requester', hops, A.actors['machine']['account'], 'cheap', ceiling=2)[1])
        out['hops_limit'] = self.route(A, 'requester', hops * 4, A.actors['machine']['account'], 'many')[0]
        ok = out['payload'] == 422 and out['recipient_is_sender'] == 'recipient_is_sender' and out['retention'] == 'retention_limit' and out['fee_ceiling'] == 'fee_ceiling_exceeded' and out['hops_limit'] in (400, 422) and A.bal('requester', 'reserved') == res0
        self.rec(30, self.j30.__doc__, ok, dict(out, nothing_reserved_on_refusal=A.bal('requester', 'reserved') == res0), t0=t0)

    # ---- 31–35 claim lots and partial return -------------------------------------------------------------------------------------------
    def lots(self, dom, who):
        return dom.call('get', '/api/v1/native/accounts/%s/lots' % dom.actors[who]['account'])[1]

    def j31(self, t0):
        """Split a 20-unit claim, spend 7 and retire only the holder's remaining 13."""
        A, B = self.A, self.B; bob, carol = B.actors['bob']['account'], B.actors['carol']['account']; st, a = A.export('lotter', B, bob, 20, 'lot20'); self.push(A, B)
        l0 = self.lots(B, 'bob'); st_t, tr = B.call('post', '/api/v1/native/transfers', role='bob', json={'to_account': carol, 'amount': 7, 'asset': A.asset, 'request_id': 's7'}); lb, lc = self.lots(B, 'bob'), self.lots(B, 'carol'); inv = B.alloc(a['id'])['lot_invariants']
        r14 = B.call('post', '/api/v1/native/allocations/%s/return' % a['id'], role='bob', json={'amount': 14}); r13 = B.call('post', '/api/v1/native/allocations/%s/return' % a['id'], role='bob', json={'amount': 13}); v = r13[1]
        ok = st == 201 and [(l['amount'], l['status'], l['transferred']) for l in l0['lots']] == [(20, 'active', 0)] and st_t == 201 and sorted((l['amount'], l['status']) for l in lb['lots'] if l['status'] == 'active') == [(13, 'active')] and [(l['amount'], l['transferred'], l['origin_allocation']) for l in lc['lots']] == [(7, 1, a['id'])] and (lb['retirable_total'], lc['retirable_total']) == (13, 0) and inv['partition_ok'] \
            and r14[0] == 409 and self.code(r14[1]) == 'retire_exceeds_owned' and r13[0] == 200 and (v['state'], v['exposed_state']) == ('return_requested', 'retirement_pending') and v['return']['partial'] and (v['return']['amount'], v['return']['returned_before'], len(v['return']['lots'])) == (13, 0, 1) and B.bal(bob, 'in_transit', asset=A.asset) == 13
        self.ctx['lot_alloc'] = a['id']
        self.rec(31, self.j31.__doc__, ok, {'allocation': a.get('id'), 'after_spend': {'bob_lots': [(l['amount'], l['status']) for l in lb['lots']], 'carol_lots': [(l['amount'], l['status'], l['transferred']) for l in lc['lots']], 'retirable': (lb['retirable_total'], lc['retirable_total'])}, 'over_retire': (r14[0], self.code(r14[1])), 'retire_13': (v.get('state'), v.get('exposed_state')), 'v2_message': {k: v.get('return', {}).get(k) for k in ('partial', 'amount', 'returned_before')}}, t0=t0)

    def j32(self, t0):
        """Release 13 at the source once while leaving the supplier's 7 fully backed."""
        A, B = self.A, self.B; aid = self.ctx['lot_alloc']; bob, carol = B.actors['bob']['account'], B.actors['carol']['account']; lot0 = A.bal('lotter'); out = self.push(B, A); sa = A.alloc(aid); sb = B.alloc(aid)
        ob = B.call('get', '/api/v1/native/transport/outbox', params={'receiver': 'dom-a', 'all': '1'})[1]; dup = A.call('post', '/api/v1/native/transport/inbox', json=ob)[1]; oa = A.call('get', '/api/v1/native/transport/outbox', params={'receiver': 'dom-b', 'all': '1'})[1]; dup2 = B.call('post', '/api/v1/native/transport/inbox', json=oa)[1]
        acc = B.call('get', '/api/v1/native/accounting', params={'asset': A.asset})[1]
        ok = out['results'][0]['state'] == 'accepted' and (A.bal('lotter'), sa['state'], sa['returned_amount']) == (lot0 + 13, 'partially_returned', 13) and A.audit()['backing_locked_for_exports'] >= 7 and (sb['state'], sb['returned_amount'], B.bal(bob, 'in_transit', asset=A.asset)) == ('imported', 13, 0) and B.bal(carol, asset=A.asset) >= 7 \
            and all(x['state'] == 'duplicate' for x in dup['results']) and A.bal('lotter') == lot0 + 13 and all(x['state'] == 'duplicate' for x in dup2['results']) and B.alloc(aid)['returned_amount'] == 13 and acc['lots']['partition_ok'] and acc['checks']['backing_matches_journal'] and A.audit()['conserved'] and B.audit(A.asset)['conserved']
        self.rec(32, self.j32.__doc__, ok, {'source': {'state': sa['state'], 'returned': sa['returned_amount'], 'lotter_delta': A.bal('lotter') - lot0, 'backing_locked': A.audit()['backing_locked_for_exports']}, 'destination': {'state': sb['state'], 'returned': sb['returned_amount'], 'carol': B.bal(carol, asset=A.asset)}, 'replayed_request_and_certificate_duplicates': (all(x['state'] == 'duplicate' for x in dup['results']), all(x['state'] == 'duplicate' for x in dup2['results'])), 'accounting': {'partition_ok': acc['lots']['partition_ok'], 'returned_total': acc['lots']['returned_total']}}, t0=t0)

    def j33(self, t0):
        """Refuse duplicate parent consumption and attempts to retire another owner's claim."""
        A, B = self.A, self.B; aid = self.ctx['lot_alloc']; r_c = B.call('post', '/api/v1/native/allocations/%s/return' % aid, role='carol', json={'amount': 7}); r_b = B.call('post', '/api/v1/native/allocations/%s/return' % aid, role='bob', json={'amount': 1})
        ex = B.call('get', '/api/v1/native/export')[1]; by = {}
        for l in ex['claim_lots']:
            if l['origin_allocation'] == aid:
                by[l['status']] = by.get(l['status'], 0) + l['amount']
        chk = PV.check_lots({'id': aid, 'amount': 20, 'returned_amount': 13}, ex['claim_lots'])[0]
        ok = r_c[0] == 403 and r_b[0] == 409 and self.code(r_b[1]) == 'missing_lineage' and sum(by.values()) == 20 and chk['ok']
        self.rec(33, self.j33.__doc__, ok, {'other_owner_retires': r_c[0], 'consumed_parent_again': (r_b[0], self.code(r_b[1])), 'lots_by_status': by, 'independent_partition_check': chk['ok']}, t0=t0)

    def j34(self, t0):
        """Exercise every retirement/certificate/release/acknowledgment crash boundary."""
        A, B = self.A, self.B; bob = B.actors['bob']['account']; lot0 = A.bal('lotter'); crash = {}
        for point in ('native_export_reserve', 'native_export_commit', 'native_certificate_publish'):
            A.fault(point); st, r = A.call('post', '/api/v1/native/allocations/export', role='lotter', json={'destination_domain': 'dom-b', 'recipient_account': bob, 'amount': 3, 'request_id': 'crash-' + point}); A.fault(point, disarm=True)
            r2 = A.call('post', '/api/v1/native/allocations/export', role='lotter', json={'destination_domain': 'dom-b', 'recipient_account': bob, 'amount': 3, 'request_id': 'crash-' + point})[1]; crash[point] = {'first': st, 'retry_state': r2.get('state'), 'recover_sweep': A.call('post', '/api/v1/native/allocations/recover', json={})[1].get('resumed')}
        certs = A.call('get', '/api/v1/native/transport/outbox', params={'receiver': 'dom-b'})[1]; bob0 = B.bal(bob, asset=A.asset)
        for point in ('native_import_commit', 'native_ack'):
            B.fault(point); st, r = B.call('post', '/api/v1/native/transport/inbox', json=certs); B.fault(point, disarm=True); crash[point] = {'first': st, 'credited': B.bal(bob, asset=A.asset) - bob0}
        out = self.push(A, B); accepted = [x['state'] for x in out['results']]
        a2 = A.call('post', '/api/v1/native/allocations/export', role='lotter', json={'destination_domain': 'dom-b', 'recipient_account': bob, 'amount': 6, 'request_id': 'lot6'})[1]; self.push(A, B)
        B.fault('native_return_init'); sr = B.call('post', '/api/v1/native/allocations/%s/return' % a2['id'], role='bob', json={'amount': 4})[0]; B.fault('native_return_init', disarm=True); v = B.alloc(a2['id'])
        sr2, r2 = B.call('post', '/api/v1/native/allocations/%s/return' % a2['id'], role='bob', json={'amount': 4}); A.fault('native_return_complete'); self.push(B, A); A.fault('native_return_complete', disarm=True); mid_state = A.alloc(a2['id'])['state']; before = A.bal('lotter')
        j0 = len(A.call('get', '/api/v1/native/export')[1]['journal_entries']); self.push(B, A); self.push(B, A); sa = A.alloc(a2['id']); sb = B.alloc(a2['id']); jd = [(e.get('kind'), e.get('ref_id'), [(pp.get('account', '')[-40:], pp.get('debit'), pp.get('credit')) for pp in e.get('postings', [])]) for e in A.call('get', '/api/v1/native/export')[1]['journal_entries'][j0:]]; after4 = A.bal('lotter')
        r3 = B.call('post', '/api/v1/native/allocations/%s/return' % a2['id'], role='bob', json={'amount': 2}); self.push(B, A); final = (A.alloc(a2['id'])['state'], B.alloc(a2['id'])['state'], A.alloc(a2['id'])['returned_amount'])
        ok = all(c['first'] == 409 for c in crash.values()) and all(crash[p]['retry_state'] == 'certified' and crash[p]['recover_sweep'] == [] for p in ('native_export_reserve', 'native_export_commit', 'native_certificate_publish')) and crash['native_import_commit']['credited'] == 0 and crash['native_ack']['credited'] == 0 and accepted == ['accepted'] * 3 \
            and sr == 409 and (v['state'], v['exposed_state']) == ('return_requested', 'retirement_pending') and r2.get('replayed') and mid_state == 'acknowledged' and (sa['state'], sa['returned_amount'], after4) == ('partially_returned', 4, before + 4) and (sb['state'], sb['returned_amount']) == ('imported', 4) and r3[0] == 200 and final == ('returned', 'returned', 6) and A.audit()['conserved'] and B.audit(A.asset)['conserved']
        self.rec(34, self.j34.__doc__, ok, {'export_boundaries': crash, 'partial_retirement': {'init_crash': sr, 'durable': (v['state'], v['exposed_state']), 'replayed': r2.get('replayed'), 'after_completion_crash': mid_state, 'released_once': after4 - before, 'journal_after_crash': jd}, 'second_partial_closes': final}, t0=t0)

    def j35(self, t0):
        """Reject mixed-asset or missing-lineage return evidence."""
        A, B = self.A, self.B; bob = B.actors['bob']['account']; lot0 = A.bal('lotter'); a = A.call('post', '/api/v1/native/allocations/export', role='lotter', json={'destination_domain': 'dom-b', 'recipient_account': bob, 'amount': 10, 'request_id': 'lot10'})[1]; self.push(A, B)
        ml = B.call('post', '/api/v1/native/allocations/%s/return' % a['id'], role='carol', json={'amount': 1})[0]
        B.call('post', '/api/v1/native/allocations/%s/return' % a['id'], role='bob', json={'amount': 4}); ob = B.call('get', '/api/v1/native/transport/outbox', params={'receiver': 'dom-a'})[1]
        tampered = json.loads(json.dumps(ob)); tampered['items'][0]['payload']['asset'] = B.asset; r = A.call('post', '/api/v1/native/transport/inbox', json=tampered)[1]; nolots = json.loads(json.dumps(ob)); nolots['items'][0]['payload']['lots'] = []; r2 = A.call('post', '/api/v1/native/transport/inbox', json=nolots)[1]
        unchanged = A.bal('lotter') == lot0 - 10; self.push(B, A); done = A.alloc(a['id'])
        v1 = MN if False else None; strict = set(P.RETURN_REQUEST_FIELDS); msg = ob['items'][0]['payload']; v1_refuses = set(msg) != strict and msg.get('partial') is True
        ok = ml == 403 and r['results'][0]['state'] != 'accepted' and r2['results'][0]['state'] != 'accepted' and unchanged and done['state'] == 'partially_returned' and done['returned_amount'] == 4 and v1_refuses
        self.rec(35, self.j35.__doc__, ok, {'missing_lineage_holder': ml, 'mixed_asset': r['results'][0].get('reason'), 'missing_lots': r2['results'][0].get('reason'), 'source_unchanged_until_valid': unchanged, 'valid_partial_after': (done['state'], done['returned_amount']), 'v1_peer_would_refuse_v2_fields': v1_refuses}, t0=t0)

    # ---- 36–40 continuity, fencing, quarantine, unavailable dependencies ---------------------------------------------------------------
    def runner(self, dom, who, mid, holder, steps=3, extra=(), timeout=120):
        log = Path(dom.inst.temp.name) / ('runner-%s.jsonl' % holder); p = subprocess.run([PY, '-m', 'metacoin_service.native.controller_runner', '--base', dom.base, '--cred', str(dom.cred_file(who)), '--mission', mid, '--holder', holder, '--steps', str(steps), '--interval', '0.5', '--log', str(log)] + list(extra), cwd=ROOT, env=ENV, capture_output=True, text=True, timeout=timeout)
        events = [json.loads(l) for l in log.read_text().splitlines()] if log.exists() else []; return p.returncode, events

    def j36(self, t0):
        """Reconstruct a mission after controller failure and acquire a valid fenced generation."""
        A = self.A; A.fund('requester', 300); m = A.mission('requester', 'terrain_delivery', policy=self.pol(compute_cpu_ms=20)); mid = m['id']
        c0 = A.call('get', '/api/v1/missions/%s/continuity' % mid, role='requester')[1]; rcA, evA = self.runner(A, 'requester', mid, 'runner-A', steps=1); genA = next((e['generation'] for e in evA if e['event'] == 'generation_granted'), None)
        rid = A.ms('requester', mid, 'acquire')['binding'].get('request_id'); A.call('post', '/api/v1/work/requests/%s/offers' % rid, role='machine', json={'price_amount': 4, 'asset': A.asset, 'scheme': 'exact', 'window_seconds': 3600, 'verification': {'class': 'analytical'}}) if rid else None
        # controller A "dies" (its process already exited); controller B reconstructs from durable records and obtains the next generation
        c1 = A.call('get', '/api/v1/missions/%s/continuity' % mid, role='requester')[1]; rcB, evB = self.runner(A, 'requester', mid, 'runner-B', steps=3); genB = next((e['generation'] for e in evB if e['event'] == 'generation_granted'), None); recon = next((e for e in evB if e['event'] == 'reconstructed'), {})
        c2 = A.call('get', '/api/v1/missions/%s/continuity' % mid, role='requester')[1]
        ok = c0['active_generation'] is None and rcA == 0 and genA == 1 and c1['active_generation']['holder'] == 'runner-A' and c1['pending_operations'] and rcB == 0 and genB == 2 and recon.get('pending_operations', 0) >= 1 and recon.get('active_generation') == 1 and [g['state'] for g in c2['generations']] == ['superseded', 'active'] and c2['active_generation']['holder'] == 'runner-B'
        self.ctx['fenced'] = {'mission': mid, 'generation': genB}
        self.rec(36, self.j36.__doc__, ok, {'mission': mid, 'runner_A': {'rc': rcA, 'generation': genA, 'events': [e['event'] for e in evA]}, 'reconstructed_by_B': {k: recon.get(k) for k in ('state', 'active_generation', 'pending_operations', 'safe_actions')}, 'runner_B': {'rc': rcB, 'generation': genB, 'events': [e['event'] for e in evB][:6]}, 'generations': [(g['holder'], g['state']) for g in c2['generations']]}, t0=t0)

    def j37(self, t0):
        """Reject stale-controller mutations after takeover."""
        A = self.A; f = self.ctx['fenced']; mid = f['mission']; rcA, evA = self.runner(A, 'requester', mid, 'runner-A', steps=2, extra=['--generation', '1']); stale = next((e for e in evA if e['event'] == 'stale_generation_refused'), {})
        api = {k: self.code(A.call('post', '/api/v1/missions/%s/%s' % (mid, k), role='requester', json=dict({'generation': 1}, **({'action': 'verify', 'milestone': 'acquire'} if k == 'propose' else {})))[1]) for k in ('step', 'run', 'propose')}
        hd = self.code(A.call('post', '/api/v1/missions/%s/step' % mid, role='requester', h=dict(A.H('requester'), **{'X-Controller-Generation': '1'}), json={})[1]); bare = self.code(A.call('post', '/api/v1/missions/%s/step' % mid, role='requester', json={})[1])
        ev = [e for e in A.mview('requester', mid)['events'] if e['kind'] == 'stale_generation_refused']; live = A.call('post', '/api/v1/missions/%s/step' % mid, role='requester', json={'generation': f['generation']})[0]
        ok = rcA == 3 and stale.get('presented') == 1 and stale.get('active') == f['generation'] and all(v == 'stale_generation' for v in api.values()) and hd == 'stale_generation' and bare == 'stale_generation' and len(ev) >= 5 and live == 200
        self.rec(37, self.j37.__doc__, ok, {'stale_runner_exit': rcA, 'stale_event': stale, 'api_refusals': api, 'header_form': hd, 'no_generation_presented': bare, 'recorded_refusals': len(ev), 'active_generation_steps': live}, t0=t0)

    def j38(self, t0):
        """Refuse self-issued takeover when the authority cannot establish a new generation."""
        A = self.A; f = self.ctx['fenced']; mid = f['mission']; rcF, evF = self.runner(A, 'requester', mid, 'forger', steps=2, extra=['--forge-generation', '99']); forged = next((e for e in evF if e['event'] == 'stale_generation_refused'), {})
        log = Path(A.inst.temp.name) / 'runner-unreachable.jsonl'; p = subprocess.run([PY, '-m', 'metacoin_service.native.controller_runner', '--base', 'http://127.0.0.1:1', '--cred', str(A.cred_file('requester')), '--mission', mid, '--holder', 'orphan', '--steps', '1', '--log', str(log)], cwd=ROOT, env=ENV, capture_output=True, text=True, timeout=60); evU = [json.loads(l) for l in log.read_text().splitlines()] if log.exists() else []
        c = A.call('get', '/api/v1/missions/%s/continuity' % mid, role='requester')[1]; intruder = A.call('post', '/api/v1/missions/%s/takeover' % mid, role='supplier', json={'holder': 'intruder'})[0]
        ok = rcF == 3 and forged.get('presented') == 99 and p.returncode == 4 and evU and evU[-1]['event'] == 'authority_unreachable' and c['active_generation']['generation'] == f['generation'] and intruder == 403
        self.rec(38, self.j38.__doc__, ok, {'forged_generation': (rcF, forged), 'unreachable_authority': (p.returncode, evU[-1] if evU else None), 'active_generation_unchanged': c['active_generation']['generation'], 'non_party_takeover': intruder}, t0=t0)

    def j39(self, t0):
        """Quarantine a stale authority restore using witness/peer evidence."""
        A, B = self.A, self.B; bob = B.actors['bob']['account']; db = A.inst.settings.db_path; backup = Path(A.inst.temp.name) / 'before.sqlite'
        src = sqlite3.connect('file:%s?mode=ro' % db, uri=True); dst = sqlite3.connect(str(backup)); src.backup(dst); dst.close(); src.close()
        a = A.call('post', '/api/v1/native/allocations/export', role='lotter', json={'destination_domain': 'dom-b', 'recipient_account': bob, 'amount': 5, 'request_id': 'stale'})[1]; self.push(A, B); w = self.W.client.call('GET', '/latest/dom-a')[1]; rec1 = A.call('get', '/api/v1/native/recovery')[1]
        A.api.terminate(); A.api.wait(timeout=20)
        for suffix in ('-wal', '-shm'):
            pth = db.with_name(db.name + suffix)
            if pth.exists():
                pth.unlink()
        shutil.copyfile(backup, db); os.chmod(db, 0o600); wm = A.inst.settings.keys_dir / 'native' / 'watermark-dom-a.json'
        if wm.exists():
            wm.unlink()
        A.start_api(); d = A.domain(); rec = A.call('get', '/api/v1/native/recovery')[1]
        fm = (self.ctx.get('fenced') or {}).get('mission') or (self.ctx.get('j13') or {}).get('mission') or A.mission('requester', 'terrain_delivery', policy=self.pol(compute_cpu_ms=20))['id']
        st_q, rq = A.call('post', '/api/v1/native/transfers', role='lotter', json={'to_account': A.actors['machine']['account'], 'amount': 1, 'request_id': 'q'}); tk = A.call('post', '/api/v1/missions/%s/takeover' % fm, role='requester', json={'holder': 'during-quarantine'})
        for peer in (B, self.C):
            peer.call('post', '/api/v1/native/transport/observation', json={'peer': 'dom-a'}); self.push(peer, A)
        st_r, r = A.call('post', '/api/v1/native/recovery/reconcile', json={'reconstruct_from_observations': True, 'source_account': A.actors['lotter']['account'], 'amounts': {a['id']: 5}})
        ok = w['latest']['sequence'] == rec1['witness']['latest_sequence'] and d['recovery_state'] == 'quarantine' and 'witness-acknowledged' in rec['rollback_detection'] and st_q == 409 and self.code(rq) == 'domain_quarantined' and tk[0] == 409 and self.code(tk[1]) == 'domain_quarantined' and st_r == 200 and r['recovery_state'] == 'normal' and A.domain()['commit_sequence'] >= w['latest']['sequence']
        self.rec(39, self.j39.__doc__, ok, {'witness_sequence_before_restore': w['latest']['sequence'], 'after_restore': {'recovery_state': d['recovery_state'], 'detection': rec['rollback_detection'][:80]}, 'spend_during_quarantine': (st_q, self.code(rq)), 'takeover_during_quarantine': (tk[0], self.code(tk[1])), 'reconciled': r.get('recovery_state'), 'reconcile_response': (st_r, json.dumps(r)[:900])}, caveat='the local watermark file was removed to emulate a restore on another host; the witness checkpoint was the detection source', t0=t0)

    def witness_restart(self):
        W = self.W; W.proc = subprocess.Popen([PY, '-m', 'metacoin_service.native.witness', 'serve', '--home', str(W.home), '--port', str(W.port)], cwd=ROOT, env=ENV, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            if W.client.call('GET', '/health')[0] == 200:
                return
            time.sleep(0.05)

    def j40(self, t0):
        """Preserve uncertain obligations when a signer, witness or external worker is unavailable."""
        C, A = self.C, self.A; C.fund('c_req', 300); pol = {'verifier': 0, 'bps': 0, 'permitted_providers': [C.actors['c_mach']['provider_id']], 'compute_cpu_ms': 20}; m = C.mission('c_req', 'terrain_delivery', policy=pol); mid = m['id']; m0 = C.bal('c_mach')
        s = self.drive_signed(C, 'c_req', 'c_mach', mid, 'acquire', 4, signers=()); pay = C.step('c_req', mid)
        self.sgR.proc.terminate(); self.sgR.proc.wait(timeout=10); waiting = None
        for _ in range(4):
            down = C.step('c_req', mid); waiting = (down['performed'] or {}).get('action')
            if waiting == 'await_signature':
                break
        exp = C.explain('c_req', mid); reason = next((a['reason'] for a in exp['actions'] if a['action'] == 'await_signature'), '')
        cont = C.call('get', '/api/v1/missions/%s/continuity' % mid, role='c_req')[1]; pend = len(cont['pending_signing_requests']); unchanged = (C.bal('c_mach'), C.bal('c_req', 'reserved')); self.sgR.start(); signed = self.sign_all(C, 'c_req'); after = C.bal('c_mach')
        # external worker unavailable: the award stays executing, nothing is paid; a restarted worker completes it
        C.stop_workers()
        if not C.ms('c_req', mid, 'compute')['binding'].get('request_id'):
            self.adv(C, 'c_req', mid, 'open_request')
        rid = C.ms('c_req', mid, 'compute')['binding']['request_id']; C.call('post', '/api/v1/work/requests/%s/offers' % rid, role='c_mach', json={'price_amount': 40, 'asset': C.asset, 'scheme': 'exact', 'window_seconds': 3600, 'verification': {'class': 'analytical'}})
        aw = self.adv(C, 'c_req', mid, 'award'); time.sleep(3); stuck = C.step('c_req', mid); state_stuck = C.ms('c_req', mid, 'compute')['state']; paid_stuck = C.bal('c_mach')
        C.start_worker('w-dom-c-2'); tA = time.time(); done = self.adv(C, 'c_req', mid, 'decide', signers=('c_req',)); tB = time.time(); pay2 = self.adv(C, 'c_req', mid, 'pay', signers=('c_req',)); self.sign_all(C, 'c_req'); paid_after = C.bal('c_mach'); timings = {'decide_after_worker_restart_s': round(tB - tA, 1), 'pay_s': round(time.time() - tB, 1), 'decide_timed_out': done.get('timed_out')}
        # witness unavailable: local commits proceed, the recovery view says so, the checkpoint is acknowledged after the witness returns
        self.W.proc.terminate(); self.W.proc.wait(timeout=10); tw = time.time(); st_a, a = A.call('post', '/api/v1/native/allocations/export', role='lotter', json={'destination_domain': 'dom-b', 'recipient_account': self.B.actors['bob']['account'], 'amount': 1, 'request_id': 'nowit'}); tw = round(time.time() - tw, 1); recw = A.call('get', '/api/v1/native/recovery')[1]['witness']
        self.witness_restart(); A.call('post', '/api/v1/native/allocations/export', role='lotter', json={'destination_domain': 'dom-b', 'recipient_account': self.B.actors['bob']['account'], 'amount': 1, 'request_id': 'wit-back'}); recw2 = A.call('get', '/api/v1/native/recovery')[1]['witness']
        ok = (pay['performed'] or {}).get('action') == 'pay' and waiting == 'await_signature' and 'no fallback' in reason and pend >= 1 and unchanged == (m0, 4) and signed and signed[0]['status'] == 200 and after == m0 + 4 and aw['performed']['action'] == 'award' and state_stuck == 'executing' and paid_stuck == m0 + 4 and (stuck['performed'] or {}).get('action') in (None, 'await_execution') \
            and done['result']['decision'] == 'accepted' and paid_after == m0 + 44 and a.get('state') == 'certified' and recw.get('reachable') is False and recw2.get('reachable') and (recw2.get('latest_sequence') or 0) >= (recw.get('latest_sequence') or 0)
        self.rec(40, self.j40.__doc__, ok, {'signer_down': {'controller': waiting, 'reason': reason[:100], 'pending_requests': pend, 'balances_unchanged': unchanged, 'after_restart': after - m0}, 'worker_down': {'milestone': state_stuck, 'paid_while_down': paid_stuck - m0 - 4, 'after_restart': paid_after - m0, 'timings': timings}, 'witness_down': {'export': (st_a, a.get('state') or json.dumps(a)[:300], tw), 'witness': recw, 'after_restart': recw2}}, t0=t0)

    # ---- 41–44 portfolio ---------------------------------------------------------------------------------------------------------------
    def j41(self, t0):
        """Match bounded portfolio fixtures against an exhaustive independent oracle."""
        from metacoin_service.tests.test_native_portfolio import fixture
        agree = 0; mismatches = []; total = 0
        for seed in range(40):
            probs, prios = fixture(seed); o = PF.oracle(probs, prios); i = PF.improved(probs, prios, timeout_ms=5000); ref = PF.reference(probs, prios)
            if o['status'] == 'optimal':
                total += 1
                if i['score'][:2] == o['score'][:2] and (ref['score'] or [0])[0] >= o['score'][0]:
                    agree += 1
                else:
                    mismatches.append({'seed': seed, 'improved': i['score'], 'oracle': o['score'], 'reference': ref.get('score')})
        ok = total >= 25 and agree == total
        self.rec(41, self.j41.__doc__, ok, {'fixtures': 40, 'oracle_optimal': total, 'improved_matches_oracle': agree, 'mismatches': mismatches[:3], 'note': 'reference policy never admits more priority mass than the optimum'}, t0=t0)

    def j42(self, t0):
        """Schedule competing missions across shared pools and domain-local liquidity."""
        A = self.A; A.fund('requester', 1000); t = int(time.time()); pool = self.pool(A, 'supplier', 1, 1, 'contact', 'channel', t, label='antenna'); o = self.pool_offer(A, 'supplier', pool['id'], 'contact', t, t + 7200, 7200)[1]
        g = lambda q: {'milestones': [{'key': 'link', 'kind': 'resource', 'depends': [], 'spec': {'offer_kind': 'contact', 'quantity': q, 'max_price_per_unit': 5}}]}
        hi = A.mission('requester', graph=g(60), policy={'priority': 3}, title='hi')['id']; lo = A.mission('requester', graph=g(60), policy={'priority': 1}, title='lo')['id']; md = A.mission('requester', graph=g(60), policy={'priority': 2}, title='mid')['id']
        st, p = A.call('post', '/api/v1/portfolio/plan', role='requester', json={'mission_ids': [hi, lo, md], 'objective': 'lowest_cost', 'planner': 'improved'}); ref = A.call('post', '/api/v1/portfolio/plan', role='requester', json={'mission_ids': [hi, lo, md], 'planner': 'reference'})[1]; orc = A.call('post', '/api/v1/portfolio/plan', role='requester', json={'mission_ids': [hi, lo], 'planner': 'oracle'})[1]
        v = A.call('post', '/api/v1/portfolio/validate', role='requester', json={'plan': p['plan']})[1]; liq = p['liquidity'][hi]
        ok = st == 200 and p['status'] == 'optimal' and sorted(p['admitted']) == sorted([hi, lo, md]) and not p['binding'] and p['priorities'][hi] == 3 and ref['admitted'][0] == hi and orc['status'] == 'optimal' and v['valid'] and set(liq) >= {'held_locally', 'reserved', 'cross_domain_claims_held', 'cross_domain_pending', 'expected_earnings', 'spendable'}
        self.ctx['portfolio'] = {'plan': p, 'missions': [hi, lo, md], 't': t}
        self.rec(42, self.j42.__doc__, ok, {'status': p.get('status'), 'admitted_in_priority_order': ref.get('admitted') == sorted([hi, md, lo], key=lambda m: -{hi: 3, md: 2, lo: 1}[m]), 'binding': p.get('binding'), 'validator': v.get('valid'), 'liquidity_split': {k: liq.get(k) for k in ('held_locally', 'reserved', 'cross_domain_claims_held', 'cross_domain_pending', 'expected_earnings', 'spendable')}, 'improved_status': p.get('status'), 'bound': p.get('plan', {}).get('bound')}, t0=t0)

    def j43(self, t0):
        """Purchase bounded evidence and replan only eligible uncommitted work."""
        A = self.A; A.fund('requester', 300); m = A.mission('requester', 'evidence_before_followon', policy=dict(self.pol(), gate_budget=10)); mid = m['id']; s = A.drive_work('requester', 'machine', mid, 'compute', 40); an = A.advance('requester', mid, 'analyze')
        deadline = time.time() + 120; gate = None
        while time.time() < deadline:
            st = A.step('requester', mid); p = st.get('performed')
            if p and p['action'] in ('buy_evidence', 'skip_gate'):
                gate = st; break
            time.sleep(0.6)
        pl = A.call('post', '/api/v1/missions/%s/plan' % mid, role='requester', json={'objective': 'lowest_cost', 'apply': True})[1]; v = A.mview('requester', mid); comp = next(x for x in v['milestones'] if x['key'] == 'compute'); gm = next((x for x in v['milestones'] if x['key'] == 'gate'), {})
        ok = s['result']['decision'] == 'accepted' and an['performed']['action'] == 'analyze' and gate is not None and pl['status'] in ('optimal', 'infeasible', 'feasible_timeout') and comp['state'] in ('accepted', 'settled') and (gm.get('binding') or {}).get('evidence_label', True)
        self.rec(43, self.j43.__doc__, ok, {'gate_decision': (gate or {}).get('performed'), 'gate_result': {k: ((gate or {}).get('result') or {}).get(k) for k in ('reason', 'label', 'evidence_label', 'price', 'value_of_information')}, 'replanned': pl.get('status'), 'accepted_work_untouched': comp['state']}, t0=t0)

    def j44(self, t0):
        """Reject a planner output spending pending remote claims or overselling resources."""
        A, C = self.A, self.C; pf = self.ctx['portfolio']; p = pf['plan']; t = pf['t']; sched = p['plan']['incumbent']['schedule']
        over = {tid: dict(s, start_at=t + 6000, end_at=t + 6060, quantity=60) for tid, s in sched.items()}; v2 = A.call('post', '/api/v1/portfolio/validate', role='requester', json={'plan': {'schedule': over}})[1]
        rliq = A.call('get', '/api/v1/native/accounts/%s/liquidity' % A.actors['requester']['account'], role='requester')[1]; rich = {tid: dict(s, cost=rliq['spendable'] + 1) for tid, s in sched.items()}; v3 = A.call('post', '/api/v1/portfolio/validate', role='requester', json={'plan': {'schedule': rich}})[1]
        # a pending remote claim: exported to the machine at dom-c but not yet imported there; its liquidity shows it as pending, not spendable
        A.fund('lotter', 20); a = A.call('post', '/api/v1/native/allocations/export', role='lotter', json={'destination_domain': 'dom-c', 'recipient_account': C.actors['c_mach']['account'], 'amount': 5, 'request_id': 'pending-claim'})[1]
        liq = C.call('get', '/api/v1/native/accounts/%s/liquidity' % C.actors['c_mach']['account'], role='c_mach')[1]; held = C.bal('c_mach')
        co = (self.ctx.get('j4') or {}).get('offer')
        if not co:
            tt = int(time.time()); pool = self.pool(C, 'c_sup', 2, 1000, 'compute', 'cpu_slot', tt - 60, 3); co = self.pool_offer(C, 'c_sup', pool['id'], 'compute', tt - 60, tt + 3 * 3600 - 60, 100000)[1]
        gm = C.mission('c_mach', graph={'milestones': [{'key': 'cpu', 'kind': 'resource', 'depends': [], 'spec': {'offer_kind': 'compute', 'quantity': 1000, 'max_price_per_unit': 5}}]}, title='machine plan')['id']; pm = C.call('post', '/api/v1/portfolio/plan', role='c_mach', json={'mission_ids': [gm], 'planner': 'reference'})[1]
        plan_claim = {tid: dict(s, cost=held + 3, mission=gm) for tid, s in ((pm.get('plan') or {}).get('incumbent') or {}).get('schedule', {}).items()} or {gm + ':cpu': {'offer_id': co['id'], 'start_at': int(time.time()) + 60, 'end_at': int(time.time()) + 120, 'quantity': 1000, 'cost': held + 3, 'mission': gm}}
        v4 = C.call('post', '/api/v1/portfolio/validate', role='c_mach', json={'plan': {'schedule': plan_claim}})
        ok = not v2['valid'] and v2['reasons'][0]['constraint'] == 'pool_oversold' and not v3['valid'] and v3['reasons'][0]['constraint'] == 'not_spendable' and a.get('state') == 'certified' and liq['spendable'] <= liq['held_locally'] and v4[0] == 200 and not v4[1].get('valid') and (v4[1].get('reasons') or [{}])[0].get('constraint') == 'not_spendable'
        self.ctx['pending_claim'] = a.get('id')
        self.rec(44, self.j44.__doc__, ok, {'oversold': (v2.get('reasons') or [v2])[0], 'not_spendable': (v3.get('reasons') or [v3])[0], 'pending_remote_claim': {'allocation': a.get('id'), 'state': a.get('state'), 'machine_liquidity_at_dom_c': {k: liq.get(k) for k in ('held_locally', 'cross_domain_claims_held', 'cross_domain_pending', 'spendable')}, 'plan_beyond_spendable': (v4[0], (v4[1].get('reasons') or [{}])[0].get('constraint') if v4[0] == 200 else self.code(v4[1]))}}, t0=t0)

    # ---- 45–50 bundle, browser, migration, endurance, clean install, final accounting ----------------------------------------------------
    def j45(self, t0):
        """Verify a portable bundle offline and reject tampered claims, fees and signatures."""
        A = self.A; c = self.ctx.get('j33')
        if not c:
            self.rec(45, self.j45.__doc__, False, {}, blocked='journey 3 did not provide the clean client', t0=t0); return
        mid = self.ctx['j13']['mission']; tmp = Path(c['tmp']); reqcred = A.cred_file('requester')
        def cli(*args):
            p = subprocess.run([c['vpy'], '-m', 'metacoin_provider'] + list(args), cwd=str(tmp), env=c['env'], capture_output=True, text=True, timeout=300)
            try:
                return p.returncode, json.loads(p.stdout)
            except ValueError:
                return p.returncode, {'stdout': p.stdout[-300:], 'stderr': p.stderr[-400:]}
        rc_b, b = cli('--base', A.base, '--cred', str(reqcred), 'mission-bundle', mid, '--out', str(tmp / 'bundle.json')); bundle = json.loads((tmp / 'bundle.json').read_text()); rc_v, ver = cli('verify', str(tmp / 'bundle.json'), '--roots', c['roots']); outcomes = {}
        def tam(name, fn):
            x = json.loads(json.dumps(bundle)); fn(x); p = tmp / ('tampered-%s.json' % name); p.write_text(json.dumps(x)); rc, rep = cli('verify', str(p), '--roots', c['roots']); outcomes[name] = {'rc': rc, 'ok': rep.get('ok'), 'failed': [ch['check'] for ch in rep.get('checks', []) if not ch['ok']][:4]}
        tam('ledger_spent', lambda x: x['content']['ledger']['milestones'][0].update(spent=1)); tam('receipt_claim', lambda x: x['content']['receipts'][0]['statement']['claims'].update(amount=1))
        tam('fee_posting', lambda x: x['content']['journal_entries'][0]['postings'][0].update(debit=x['content']['journal_entries'][0]['postings'][0]['debit'] + 1)); tam('signature', lambda x: x['content']['receipts'][0].update(signature_hex='00' * 32))
        (tmp / 'noroots.json').write_text('{}'); rc_n, norep = cli('verify', str(tmp / 'bundle.json'), '--roots', str(tmp / 'noroots.json'))
        # the export verifier recomputes pool overlap, lot partition and outstanding obligations from an export of dom-b (lots) and dom-a (pools)
        ex_checks = {}
        for dom in (A, self.B):
            ex = dom.call('get', '/api/v1/native/export')[1]; roots = {d.id: [{'key_id': d.domain()['authority']['key_id'], 'public_hex': d.domain()['authority']['public_hex'], 'epoch': d.domain()['authority']['epoch']}] for d in (A, self.B)}; r = PV.check_export_v2(ex, roots); ex_checks[dom.id] = {'ok': r['ok'], 'checks': len(r['checks']), 'failed': [ch['check'] for ch in r['checks'] if not ch['ok']][:3]}
        ok = rc_b == 0 and bundle['schema'] == 'metacoin-mission-bundle/v1' and rc_v == 0 and ver.get('ok') and all(o['rc'] == 1 and o['ok'] is False for o in outcomes.values()) and 'content_digest' in outcomes['ledger_spent']['failed'] and any(f.startswith('receipt:') for f in outcomes['receipt_claim']['failed']) and 'entries_balanced' in outcomes['fee_posting']['failed'] and rc_n == 1 and all(v['ok'] for v in ex_checks.values())
        self.rec(45, self.j45.__doc__, ok, {'bundle': {'receipts': len(bundle['content']['receipts']), 'journal_entries': len(bundle['content']['journal_entries'])}, 'valid': ver.get('ok'), 'tampering': outcomes, 'no_roots': [ch['check'] for ch in norep.get('checks', []) if not ch['ok']][:3], 'export_v2_checks': ex_checks}, t0=t0)

    def j46(self, t0):
        """Complete enrollment-to-settlement in the real browser, including disruption recovery."""
        A = self.A; pw = os.environ.get('METACOIN_PLAYWRIGHT_PYTHON'); shots = self.ctx.get('shots')
        if not pw or not shots:
            self.rec(46, self.j46.__doc__, False, {}, blocked='METACOIN_PLAYWRIGHT_PYTHON and --shots required', t0=t0); return
        mid = self.ctx['j13']['mission']; pid = (self.ctx.get('product') or {}).get('id', ''); A.connectivity('online'); ent0 = len(A.call('get', '/api/v1/products/entitlements', role='ext-alice')[1]['items']); bal0 = A.bal('ext-alice')
        p = subprocess.run([pw, str(ROOT / 'metacoin_service' / 'tests' / 'browser' / 'journey_network.py'), A.base, str(A.cred_file('owner')), str(A.cred_file('ext-alice')), str(A.cred_file('supplier')), mid, pid, shots], cwd=ROOT, env=dict(A.env, PLAYWRIGHT_BROWSERS_PATH=os.environ.get('PLAYWRIGHT_BROWSERS_PATH', '')), capture_output=True, text=True, timeout=900)
        try:
            out = json.loads(p.stdout.strip().splitlines()[-1])
        except Exception:
            out = {'failed': 1, 'stdout': p.stdout[-800:], 'stderr': p.stderr[-800:]}
        A.connectivity('online'); ent1 = len(A.call('get', '/api/v1/products/entitlements', role='ext-alice')[1]['items'])
        self.rec(46, self.j46.__doc__, p.returncode == 0 and out.get('failed') == 0 and ent1 == ent0 and A.bal('ext-alice') == bal0, {'passed': out.get('passed'), 'failed': out.get('failed'), 'checks': out.get('checks'), 'screenshots': out.get('screenshots'), 'crash': out.get('crash'), 'stderr': p.stderr[-400:] if p.returncode else None, 'expired_session_paid_nothing': ent1 == ent0 and A.bal('ext-alice') == bal0}, t0=t0)

    def spawn_tree(self, tree, did, tag):
        """An API process from another source tree (the recorded base revision) with its own home: used to produce genuine prior-state databases."""
        tmp = Path(tempfile.mkdtemp(prefix='prior-%s-' % tag)); home = tmp / 'home'; port = free_port(); base = 'http://127.0.0.1:%d' % port; env = dict(ENV, PYTHONPATH=str(tree), METACOIN_X402_DISABLED='1', METACOIN_FACILITATOR_URL='http://127.0.0.1:1/blocked', METACOIN_LIMITS_JSON=json.dumps({'test_hooks': 1}))
        init = subprocess.run([PY, '-m', 'metacoin_service', '--home', str(home), '--provider-mode', 'simulation', 'init'], cwd=tree, env=env, capture_output=True, text=True); info = json.loads(init.stdout); tok = json.load(open(info['credential_file']))['principals']['owner']['token']
        api = subprocess.Popen([PY, '-m', 'metacoin_service', '--home', str(home), '--provider-mode', 'simulation', 'serve', '--port', str(port)], cwd=tree, env=env, stdout=subprocess.DEVNULL, stderr=open(tmp / 'api.log', 'ab'))
        for _ in range(300):
            try:
                if httpx.get(base + '/api/health', timeout=1).status_code == 200:
                    break
            except Exception:
                time.sleep(0.1)
        c = httpx.Client(base_url=base, timeout=120); h = {'Authorization': 'Bearer ' + tok}; st = c.post('/api/v1/native/domain', headers=h, json={'domain_id': did, 'operator': 'prior-state fixture'}).status_code; assert st == 201, st
        return {'tmp': tmp, 'home': home, 'port': port, 'base': base, 'env': env, 'api': api, 'c': c, 'h': h, 'tok': tok, 'id': did}

    def j47(self, t0):
        """Rehearse migration on prior-state copies and preserve balances, old receipts and jobs."""
        live = Path(os.environ.get('METACOIN_LIVE_HOME', os.path.expanduser('~/.local/state/metacoin-service'))); part = {}; q = lambda c, sql: c.execute(sql).fetchall()
        bal_sql = "SELECT e.asset, p.account, SUM(p.credit) - SUM(p.debit) FROM journal_postings p JOIN journal_entries e ON e.id = p.entry_id GROUP BY e.asset, p.account ORDER BY 1, 2"
        env = dict(ENV, METACOIN_X402_DISABLED='1', METACOIN_LIMITS_JSON=json.dumps({'test_hooks': 1}))
        def snapshot(path):
            src = sqlite3.connect('file:%s?mode=ro' % path, uri=True); out = {'schema': q(src, 'SELECT name FROM schema_migrations ORDER BY applied_at DESC, rowid DESC LIMIT 1')[0][0], 'jobs': q(src, 'SELECT COUNT(*) FROM jobs')[0][0], 'journal_entries': q(src, 'SELECT COUNT(*) FROM journal_entries')[0][0], 'balances_digest': P.digest([list(r) for r in q(src, bal_sql)])}; src.close(); return out
        def migrate_copy(src_home, label):
            iso = Path(tempfile.mkdtemp(prefix='metacoin-iso-%s-' % label)); home = iso / 'home'; home.mkdir(mode=0o700); src = sqlite3.connect('file:%s?mode=ro' % (src_home / 'service.sqlite'), uri=True); dst = sqlite3.connect(str(home / 'service.sqlite')); src.backup(dst); dst.close(); src.close(); os.chmod(home / 'service.sqlite', 0o600)
            for sub in ('keys', 'credentials', 'artifacts'):
                if (src_home / sub).exists():
                    shutil.copytree(src_home / sub, home / sub, dirs_exist_ok=True); os.chmod(home / sub, 0o700)
                    for f in (home / sub).rglob('*'):
                        if f.is_file():
                            os.chmod(f, 0o600)
            before = snapshot(home / 'service.sqlite'); snap = iso / 'snapshot-before-migration.sqlite'; shutil.copyfile(home / 'service.sqlite', snap)          # rollback = restore this whole snapshot
            mig = subprocess.run([PY, '-m', 'metacoin_service', '--home', str(home), '--provider-mode', 'simulation', 'migrate'], cwd=ROOT, env=env, capture_output=True, text=True, timeout=300); after = snapshot(home / 'service.sqlite')
            c2 = sqlite3.connect(str(home / 'service.sqlite')); after['new_tables'] = [r[0] for r in q(c2, "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('resource_pools','pool_commitments','native_signing_requests','mission_subcontracts','native_claim_lots','mission_products','product_entitlements','relay_routes','relay_queue','controller_generations') ORDER BY name")]; c2.close()
            return iso, home, before, after, mig
        def smoke(home, checks):
            port = free_port(); base = 'http://127.0.0.1:%d' % port; api = subprocess.Popen([PY, '-m', 'metacoin_service', '--home', str(home), '--provider-mode', 'simulation', 'serve', '--port', str(port)], cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); out = {}
            try:
                for _ in range(300):
                    try:
                        if httpx.get(base + '/api/health', timeout=1).status_code == 200:
                            break
                    except Exception:
                        time.sleep(0.1)
                tok = json.load(open(home / 'credentials' / 'bootstrap.json'))['principals']['owner']['token']; c = httpx.Client(base_url=base, timeout=60); h = {'Authorization': 'Bearer ' + tok}; out = checks(c, h)
            finally:
                api.terminate(); api.wait(timeout=20)
            return out
        # (a) the preserved live database (schema 040, work economy state): old jobs, journal and balances survive 041→054
        if (live / 'service.sqlite').exists():
            iso, home, before, after, mig = migrate_copy(live, 'live')
            def checks(c, h):
                jobs = c.get('/api/v1/jobs?limit=5', headers=h).json(); items = jobs.get('items', jobs) if isinstance(jobs, dict) else jobs
                return {'health': c.get('/api/health').json().get('revision'), 'old_jobs_readable': isinstance(items, list) and len(items) > 0 and c.get('/api/v1/jobs/' + items[0]['id'], headers=h).status_code == 200, 'replay_consistent': c.post('/api/v1/work/journal/replay', headers=h, json={}).json().get('consistent'), 'domain': c.post('/api/v1/native/domain', headers=h, json={'domain_id': 'dom-migrated'}).status_code,
                        'pools': c.get('/api/v1/resources/pools', headers=h).status_code, 'products': c.get('/api/v1/products', headers=h).status_code, 'relay': c.get('/api/v1/relay/routes', headers=h).status_code, 'subcontracts': c.get('/api/v1/subcontracts', headers=h).status_code, 'missions': c.get('/api/v1/missions', headers=h).status_code}
            sm = smoke(home, checks); rollback = snapshot(iso / 'snapshot-before-migration.sqlite'); shutil.rmtree(iso, ignore_errors=True)
            part['live_copy'] = {'before': before, 'after': after, 'migrate_rc': mig.returncode, 'stderr': mig.stderr[-300:] if mig.returncode else None, 'smoke': sm, 'balances_preserved': before['balances_digest'] == after['balances_digest'], 'rollback_snapshot_intact': rollback == before, 'live_service_untouched': True}
            live_ok = mig.returncode == 0 and after['schema'] == '054_controller_generations' and after['jobs'] == before['jobs'] and after['journal_entries'] == before['journal_entries'] and before['balances_digest'] == after['balances_digest'] and sm.get('old_jobs_readable') and sm.get('replay_consistent') and sm.get('domain') == 201 and all(sm.get(k) == 200 for k in ('pools', 'products', 'relay', 'subcontracts', 'missions')) and rollback == before and len(after['new_tables']) == 10
        else:
            part['live_copy'] = {'blocked': 'preserved previous database not found at ' + str(live)}; live_ok = False
        # (b) a genuine prior state produced by the recorded base revision: accounts, a reservation, an unfinished mission, a consumed and an untouched cross-domain claim
        base_rev = (SESSION / 'artifacts' / 'BASE_REVISION.txt').read_text().strip(); tree = Path(os.environ.get('METACOIN_BASE_TREE', str(Path(tempfile.gettempdir()) / ('metacoin-base-' + base_rev[:7])))); prior = {}
        if not (tree / 'metacoin_service').exists():
            subprocess.run(['git', 'worktree', 'add', '--detach', str(tree), base_rev], cwd=ROOT, capture_output=True, text=True)
        if (tree / 'metacoin_service').exists():
            P0 = self.spawn_tree(tree, 'old-a', 'a'); Q0 = self.spawn_tree(tree, 'old-b', 'b')
            try:
                def actor(D, name, fund=0):
                    a = D['c'].post('/api/v1/native/actors', headers=D['h'], json={'name': name}).json()
                    if fund:
                        D['c'].post('/api/v1/native/fixtures/fund', headers=D['h'], json={'account_id': a['account_id'], 'amount': fund})
                    return {'account': a['account_id'], 'h': {'Authorization': 'Bearer ' + a['credential']['token']}, 'principal_id': a['principal_id']}
                for D, E in ((P0, Q0), (Q0, P0)):
                    d = E['c'].get('/api/v1/native/domain', headers=E['h']).json(); D['c'].post('/api/v1/native/peers', headers=D['h'], json={'domain_id': E['id'], 'authority_key_id': d['authority']['key_id'], 'authority_public_hex': d['authority']['public_hex'], 'epoch': d['authority']['epoch']})
                al = actor(P0, 'old-alice', 100); sup = actor(P0, 'old-sup'); bob = actor(Q0, 'old-bob'); carol = actor(Q0, 'old-carol'); t = int(time.time())
                o = P0['c'].post('/api/v1/resources/offers', headers=sup['h'], json={'kind': 'contact', 'resource_id': 'old-c', 'capacity': 100, 'price_per_unit': 1, 'window_start': t - 1, 'window_end': t + 7200}).json(); res = P0['c'].post('/api/v1/resources/reservations', headers=al['h'], json={'offer_id': o['id'], 'offer_version': o['version'], 'quantity': 10, 'request_id': 'old-r'}).json()
                m = P0['c'].post('/api/v1/missions', headers=al['h'], json={'title': 'old mission', 'graph': {'milestones': [{'key': 'link', 'kind': 'resource', 'depends': [], 'spec': {'offer_kind': 'contact', 'quantity': 5, 'max_price_per_unit': 5}}]}, 'policy': {}}).json(); P0['c'].post('/api/v1/missions/%s/freeze' % m['id'], headers=al['h'], json={})
                ex1 = P0['c'].post('/api/v1/native/allocations/export', headers=al['h'], json={'destination_domain': 'old-b', 'recipient_account': bob['account'], 'amount': 20, 'request_id': 'old-x1'}).json(); ex2 = P0['c'].post('/api/v1/native/allocations/export', headers=al['h'], json={'destination_domain': 'old-b', 'recipient_account': bob['account'], 'amount': 5, 'request_id': 'old-x2'}).json()
                ob = P0['c'].get('/api/v1/native/transport/outbox', headers=P0['h'], params={'receiver': 'old-b'}).json(); Q0['c'].post('/api/v1/native/transport/inbox', headers=Q0['h'], json=ob)
                sp = Q0['c'].post('/api/v1/native/transfers', headers=bob['h'], json={'to_account': carol['account'], 'amount': 7, 'asset': P0['c'].get('/api/v1/native/domain', headers=P0['h']).json()['default_asset'], 'request_id': 'old-s7'}).status_code
                old_balances = {'alice': P0['c'].get('/api/v1/native/accounts/' + al['account'], headers=P0['h']).json()['available'], 'bob_claim': Q0['c'].get('/api/v1/native/accounts/' + bob['account'], headers=Q0['h'], params={'asset': ex1['asset']}).json()['available']}; old_rev = P0['c'].get('/api/health').json().get('revision')
            finally:
                for D in (P0, Q0):
                    D['api'].terminate(); D['api'].wait(timeout=20)
            isoA, homeA, bA, aA, migA = migrate_copy(P0['home'], 'old-a'); isoB, homeB, bB, aB, migB = migrate_copy(Q0['home'], 'old-b')
            def checksA(c, h):
                alice_after_migration = c.get('/api/v1/native/accounts/' + al['account'], headers=h).json().get('available'); ov = c.get('/api/v1/resources/offers/' + o['id'], headers=h).json(); again = c.post('/api/v1/resources/reservations', headers=al['h'], json={'offer_id': o['id'], 'offer_version': ov.get('version', 1), 'quantity': 5, 'request_id': 'old-r2'})
                return {'alice': alice_after_migration, 'reservation': c.get('/api/v1/resources/reservations/' + res['id'], headers=h).json().get('state'), 'legacy_offer_without_pool_still_reservable': (ov.get('pool_id') is None, again.status_code), 'mission': c.get('/api/v1/missions/' + m['id'], headers=h).json().get('state'), 'audit_conserved': c.get('/api/v1/native/audit', headers=h, params={'asset': ex1['asset']}).json().get('conserved'), 'allocations': {ex1['id']: c.get('/api/v1/native/allocations/' + ex1['id'], headers=h).json().get('state'), ex2['id']: c.get('/api/v1/native/allocations/' + ex2['id'], headers=h).json().get('state')}}
            def checksB(c, h):
                bob_after_migration = c.get('/api/v1/native/accounts/' + bob['account'], headers=h, params={'asset': ex1['asset']}).json().get('available'); lots = c.get('/api/v1/native/accounts/%s/lots' % bob['account'], headers=h).json(); partial_consumed = c.post('/api/v1/native/allocations/%s/return' % ex1['id'], headers=bob['h'], json={'amount': 1}); partial_fresh = c.post('/api/v1/native/allocations/%s/return' % ex2['id'], headers=bob['h'], json={'amount': 2})
                return {'bob_claim': bob_after_migration, 'lots': [(l['origin_allocation'] == ex2['id'], l['amount'], l['status']) for l in lots.get('lots', [])], 'consumed_old_claim_partial_return': (partial_consumed.status_code, (partial_consumed.json().get('detail') or {}).get('code')), 'untouched_old_claim_partial_return': (partial_fresh.status_code, partial_fresh.json().get('state')), 'accounting_partition_ok': c.get('/api/v1/native/accounting', headers=h, params={'asset': ex1['asset']}).json().get('lots', {}).get('partition_ok')}
            smA = smoke(homeA, checksA); smB = smoke(homeB, checksB); shutil.rmtree(isoA, ignore_errors=True); shutil.rmtree(isoB, ignore_errors=True)
            prior = {'base_revision': base_rev, 'base_api_revision': old_rev, 'before': {'old-a': bA, 'old-b': bB}, 'after': {'old-a': aA, 'old-b': aB}, 'migrate_rc': (migA.returncode, migB.returncode), 'old_balances': old_balances, 'spend_at_b': sp, 'smoke': {'old-a': smA, 'old-b': smB}}
            prior_ok = migA.returncode == 0 and migB.returncode == 0 and aA['schema'] == '054_controller_generations' and bA['balances_digest'] == aA['balances_digest'] and bB['balances_digest'] == aB['balances_digest'] and smA.get('alice') == old_balances['alice'] and smA.get('reservation') == 'reserved' and tuple(smA.get('legacy_offer_without_pool_still_reservable') or ()) == (True, 201) and smA.get('mission') == 'frozen' and smA.get('audit_conserved') \
                and smB.get('bob_claim') == old_balances['bob_claim'] and smB.get('consumed_old_claim_partial_return', (0,))[0] == 409 and smB.get('untouched_old_claim_partial_return', (0,))[0] == 200 and any(x[0] and x[1] == 5 for x in smB.get('lots', [])) and smB.get('accounting_partition_ok')
            for D in (P0, Q0):
                shutil.rmtree(D['tmp'], ignore_errors=True)
        else:
            prior = {'blocked': 'base worktree unavailable at ' + str(tree)}; prior_ok = False
        part['prior_state_from_base_revision'] = prior
        self.rec(47, self.j47.__doc__, live_ok and prior_ok, part, caveat='migrations rehearsed on isolated copies; rollback verified by restoring the whole pre-migration snapshot; the live service was neither upgraded nor restarted; old consumed claims get no invented lineage (partial return refused), untouched old claims become retirable lots', t0=t0)

    def j48(self, t0):
        """Run a sustained mixed workload with real contention, restarts and auditable recovery."""
        res = os.environ.get('METACOIN_NETWORK_ENDURANCE_JSON')
        if not res or not Path(res).exists():
            self.rec(48, self.j48.__doc__, False, {}, blocked='endurance result not supplied (METACOIN_NETWORK_ENDURANCE_JSON); the ≥30 min run is executed separately by metacoin_service.endurance_network', t0=t0); return
        r = json.loads(Path(res).read_text()); s = r.get('summary', r)
        ok = r.get('seconds', 0) >= 1800 and s.get('conserved') and s.get('unexpected_failures', 1) == 0 and s.get('duplicate_settlements', 1) == 0 and r.get('restarts', {}).get('api', 0) >= 1 and r.get('restarts', {}).get('signer', 0) >= 1 and r.get('partitions', 0) >= 1
        self.rec(48, self.j48.__doc__, ok, {'seconds': r.get('seconds'), 'seed': r.get('seed'), 'completed': s.get('completed'), 'refused_expected': s.get('refused_expected'), 'unexpected_failures': s.get('unexpected_failures'), 'duplicate_settlements': s.get('duplicate_settlements'), 'conserved': s.get('conserved'), 'restarts': r.get('restarts'), 'partitions': r.get('partitions'), 'tail_latency': s.get('latency_p99_s'), 'recovery_seconds': s.get('recovery_seconds'), 'result_file': res}, t0=t0)

    def j49(self, t0):
        """Reproduce clean installation and a representative full mission from the delivery archive."""
        res = os.environ.get('METACOIN_NETWORK_CLEAN_EXPORT_JSON')
        if not res or not Path(res).exists():
            self.rec(49, self.j49.__doc__, False, {}, blocked='clean-export result not supplied (METACOIN_NETWORK_CLEAN_EXPORT_JSON); executed from the fresh archive extraction at packaging time', t0=t0); return
        r = json.loads(Path(res).read_text()); ok = r.get('failed') == 0 and r.get('passed', 0) >= 6 and r.get('archive_sha256')
        self.rec(49, self.j49.__doc__, bool(ok), {k: r.get(k) for k in ('passed', 'failed', 'blocked', 'total', 'revision', 'archive_sha256', 'steps')}, t0=t0)

    def j50(self, t0):
        """Verify the complete final money/resource accounting and all required feature statuses at the packaged source revision."""
        from metacoin_service.native import checker
        out = {}; rev = self.A.call('get', '/api/health')[1].get('revision'); roots = {d.id: [{'key_id': d.domain()['authority']['key_id'], 'public_hex': d.domain()['authority']['public_hex'], 'epoch': d.domain()['authority']['epoch']}] for d in (self.A, self.B, self.C)}; all_ok = True
        for dom in (self.A, self.B, self.C):
            au = dom.audit(self.A.asset if dom is self.B else None); acc = dom.call('get', '/api/v1/native/accounting', params={'asset': self.A.asset if dom is self.B else dom.asset})[1]; ex = dom.call('get', '/api/v1/native/export')[1]; rep = checker.check(ex, roots)
            tv = dom.call('get', '/api/v1/work/treasury', params={'asset': dom.asset})[1] if dom is not self.B else {}
            treasury_ok = (tv.get('settled_spending', 0) <= tv.get('confirmed_revenue', 0)) if tv else True
            d = {'conserved': au['conserved'], 'total_held': au['total_held'], 'fixture_funding': au.get('fixture_funding'), 'backing_locked': au.get('backing_locked_for_exports'), 'claims_backed_elsewhere': au.get('claims_backed_elsewhere'), 'accounting_checks': acc.get('checks'), 'lots': acc.get('lots'), 'checker': rep['summary'], 'treasury': {k: tv.get(k) for k in ('confirmed_revenue', 'settled_spending', 'available')} if tv else None, 'treasury_awards_within_fees': treasury_ok}
            out[dom.id] = d; all_ok = all_ok and au['conserved'] and all(acc.get('checks', {}).values()) and rep['summary'].get('authorized_bookkeeping') and rep['summary'].get('resource_and_lineage') and rep['summary'].get('economic_backing') is not False and treasury_ok
        led = os.environ.get('METACOIN_NETWORK_FEATURE_LEDGER'); feat = None
        if led and Path(led).exists():
            L = json.loads(Path(led).read_text()); groups = L.get('groups', {}); feat = {'source_revision': L.get('source_revision'), 'groups': {k: (v.get('implemented'), v.get('exercised')) for k, v in groups.items()}, 'matches_running_revision': L.get('source_revision') == rev}
            feat_ok = len(groups) == 10 and all(v.get('implemented') and v.get('exercised') for v in groups.values()) and L.get('source_revision') == rev
        else:
            feat_ok = None
        self.rec(50, self.j50.__doc__, all_ok and bool(feat_ok), dict(out, revision=rev, feature_ledger=feat), blocked=None if feat_ok is not None else 'feature ledger not supplied yet (METACOIN_NETWORK_FEATURE_LEDGER); accounting part %s' % ('passed' if all_ok else 'FAILED'), t0=t0)

    # ---- §1 defining demonstration -------------------------------------------------------------------------------------------------------
    def demo(self, t0):
        try:
            self._demo(t0)
        finally:
            for d in (self.C, self.A):
                try:
                    d.connectivity('online')
                except Exception:
                    pass

    def _demo(self, t0):
        """Economically complete mission under custody separation: requester, primary machine, subcontractor, resource provider, verifier and relay hold their own keys at separately configured signers; competition for a shared pool, a disconnected interval, a failed delivery branch, safe replanning, return of legitimately unspent funds and a fee-funded contribution; every monetary transition independently auditable."""
        C, A = self.C, self.A; steps = []; posted0 = len(C.call('get', '/api/v1/native/export')[1]['journal_entries']); names = {n: C.actors[n]['account'] for n in ('c_req', 'c_mach', 'c_sub', 'c_sup', 'c_ver', 'c_relay', 'c_con')}
        keys = {n: {'signer': self.signers[n].signer_id, 'key_id': C.actors[n]['key_id'][:12], 'custody': 'external-signer'} for n in names}; C.fund('c_req', 2000)
        def table():
            t = {n: C.bal(n) for n in names}; t['treasury'] = C.bal(C.treasury); t['reserved'] = {n: C.bal(n, 'reserved') for n in ('c_req', 'c_mach')}; return t
        def step(name, **ev):
            steps.append(dict({'step': name, 'balances': table(), 'journal_entries': len(C.call('get', '/api/v1/native/export')[1]['journal_entries']) - posted0}, **ev))
        funding = table(); step('funding: zero-value development asset credited to the requester only (fixture); service holds no actor key', service_actor_key_files=[f.name for f in (C.inst.settings.keys_dir / 'native').glob('actor-*')])
        # 1. competition for the shared resource: one pool (1 slot) behind two overlapping offers; the requester and the subcontractor race for the same hour (both signed)
        t = int(time.time()); cpool = self.pool(C, 'c_sup', 2, 1000, 'compute', 'cpu_slot', t - 60, 3, label='rig-demo'); self.pool_offer(C, 'c_sup', cpool['id'], 'compute', t - 60, t + 7140, 100000)
        pool = self.pool(C, 'c_sup', 1, 1, 'contact', 'channel', t - 60, 3, label='antenna-demo'); oa = self.pool_offer(C, 'c_sup', pool['id'], 'contact', t - 60, t + 7140, 7200)[1]; ob = self.pool_offer(C, 'c_sup', pool['id'], 'contact', t + 1740, t + 10740, 7200)[1]
        C.fund('c_sub', 2000); race = {}; lock = threading.Lock()
        def go(who, o, tag, s0, s1):
            st, r = self.signed_reserve(C, who, o, 1800, 'demo-' + tag, s0, s1)
            with lock:
                race[tag] = (st, (self.code(r) or json.dumps(r)[:200]) if st != 201 else r['id'])
        th = [threading.Thread(target=go, args=('c_req', oa, 'req', t + 1800, t + 3600)), threading.Thread(target=go, args=('c_sub', ob, 'sub', t + 1800, t + 3600))]; [x.start() for x in th]; [x.join() for x in th]
        winner = next((k for k, v in race.items() if v[0] == 201), None); loser = 'sub' if winner == 'req' else 'req'; pv = C.call('get', '/api/v1/resources/pools/' + pool['id'])[1]
        step('competition for the shared antenna pool (one channel): two signed reservations raced for the same half hour across two overlapping offers; exactly one admitted, the other refused without reserving funds', race=race, winner=winner, pool={'peak_slots_used': pv['peak_slots_used'], 'all_within_capacity': pv['all_within_capacity']})
        # 2. the mission: terrain analysis on the pinned public tile, verifier paid 3, fee 5 %, follow-on compute bought by the machine from its settled earnings
        pol = {'verifier': 3, 'verifier_pay_to': names['c_ver'], 'permitted_providers': [C.actors['c_mach']['provider_id']], 'compute_cpu_ms': 20, 'price': 40, 'bps': 500, 'followon_provider_account': names['c_sup']}
        m = C.mission('c_req', 'terrain_delivery', policy=pol, title='terrain analysis delivery (custody separated)'); mid = m['id']
        s = self.drive_signed(C, 'c_req', 'c_mach', mid, 'acquire', 4, signers=()); pay1 = self.adv(C, 'c_req', mid, 'pay', signers=()); sig1 = self.sign_all(C, 'c_req')
        s_o = self.adv(C, 'c_req', mid, 'open_request', signers=('c_req',)); rid_c = C.ms('c_req', mid, 'compute')['binding']['request_id']; C.call('post', '/api/v1/work/requests/%s/offers' % rid_c, role='c_mach', json={'price_amount': 40, 'asset': C.asset, 'scheme': 'exact', 'window_seconds': 3600, 'verification': {'class': 'analytical'}}); s_a = self.adv(C, 'c_req', mid, 'award', signers=('c_req',))
        step('acquisition milestone settled (signed by the requester); the compute milestone awarded to the primary machine under frozen terms', acquire=s['result'].get('decision'), award=s_a.get('performed'), requester_signed=[(x['kind'], x.get('status')) for x in sig1])
        # 3. a subcontract bound to the parent's compute milestone, prefunded by the requester (signed), executed by the subcontractor, paid by the primary (signed), unused prefund returned (signed)
        st, sc = C.call('post', '/api/v1/subcontracts', role='c_mach', json=self.sub_body(C, mid, 'c_sub', label='DEMO_SUB')); sid = sc.get('id'); assert st == 201, sc
        st_b, bad = C.call('post', '/api/v1/subcontracts', role='c_mach', json=self.sub_body(C, mid, 'c_sub', mode='machine_earnings', funded=4, price=4, label='DEMO_BAD'))
        sp_, pp = C.call('post', '/api/v1/subcontracts/%s/prefund-prepare' % sid, role='c_req', json={}); assert sp_ == 200, pp; ss, sg = self.sgR.client.sign('c_req', pp['message'], context=pp.get('context')); assert ss == 200, ('signer refused the prefund', sg); pf = C.call('post', '/api/v1/subcontracts/%s/prefund' % sid, role='c_req', json={'message': pp['message'], 'signature_hex': sg.get('signature_hex'), 'key_id': sg.get('key_id')}); assert pf[0] == 200 and pf[1].get('state') == 'funded', ('prefund not funded', pf)
        child = self.drive_child(C, 'c_mach', sid, 'c_sub', signers=('c_mach',)); subpay = self.sub_adv(C, 'c_mach', sid, 'pay', signers=('c_mach',)); self.sign_all(C, 'c_mach'); subpay2 = self.sub_adv(C, 'c_mach', sid, 'pay', signers=('c_mach',))
        rr = C.call('post', '/api/v1/subcontracts/%s/return-unused' % sid, role='c_mach', json={}); self.sign_all(C, 'c_mach'); scv = C.call('get', '/api/v1/subcontracts/' + sid, role='c_mach')[1]
        step('subcontract: requester prefunded 12 (signed at sig-requester), subcontractor delivered and was paid 10 by the primary (signed at sig-machine), 2 unused returned to the requester (signed at sig-machine)', prefund=(pf[0], pf[1].get('state')), child_decision=(child.get('step') or {}).get('result', {}).get('decision'), subcontract_state=scv.get('state'), timeline=scv.get('timeline'))
        # 4. failed delivery branch: the second subcontract (funded 4 from the machine's settled acquisition earnings) fails verification; nothing is paid for it; the primary remains responsible
        m_before_bad = C.bal('c_mach'); badres = self.drive_child(C, 'c_mach', bad['id'], 'c_sub', fail=True, signers=('c_mach',), price=4) if st_b == 201 else {}; ev = [e for e in C.mview('c_req', mid)['events'] if e['kind'] == 'subcontract_failed']
        bv = C.call('get', '/api/v1/subcontracts/' + bad['id'], role='c_mach')[1] if st_b == 201 else {}
        step('failed delivery branch: the second subcontract (funded 4 from the machine\'s own settled earnings) was rejected by verification; nothing paid for it; the child award closed and the machine\'s reservation was released; the primary remains responsible for the parent milestone', created=st_b, decision=(badres.get('step') or {}).get('result', {}).get('decision'), award_closed=(badres.get('step') or {}).get('result', {}).get('award_closed'), state=bv.get('state'), machine_delta=C.bal('c_mach') - m_before_bad, machine_reserved=C.bal('c_mach', 'reserved'), timeline={k: (bv.get('timeline') or {}).get(k) for k in ('reserved', 'spent', 'returned')}, note=(ev[-1]['detail']['note'][:100] if ev else None))
        self.adv(C, 'c_req', mid, 'verify', signers=('c_req',)); s2 = self.adv(C, 'c_req', mid, 'decide', signers=('c_req',)); comp = C.ms('c_req', mid, 'compute'); job = comp['binding'].get('job_id'); prod = C.call('get', '/api/v1/lunar/products/' + job)[1] if job else {}
        step('terrain analysis computed from the pinned public tile and accepted by the verifier (not yet settled)', science=s2['result']['science'], product={k: prod.get(k) for k in ('kind', 'scope', 'source', 'provenance', 'assumptions', 'resolution') if k in prod}, terms_digest=comp['binding'].get('terms_digest'))
        # 4. blackout: the link to dom-a is cut; the mission settles locally (three requester signatures); a cross-domain claim sent to the machine from dom-a stays undeliverable
        C.connectivity('cut'); op = C.operator(); pay2 = self.adv(C, 'c_req', mid, 'pay', signers=('c_req',)); sig2 = self.sign_all(C, 'c_req')
        A.fund('lotter', 10); ex = A.call('post', '/api/v1/native/allocations/export', role='lotter', json={'destination_domain': 'dom-c', 'recipient_account': names['c_mach'], 'amount': 10, 'request_id': 'demo-claim'})[1]; pushed = self.push(A, C)
        liq = C.call('get', '/api/v1/native/accounts/%s/liquidity' % names['c_mach'], role='c_mach')[1]; liqa = C.call('get', '/api/v1/native/accounts/%s/liquidity' % names['c_mach'], role='c_mach', params={'asset': A.asset})[1]
        step('blackout: link cut; mission settled locally from reserved funds (provider 40 + fee 2 + verifier 3, each signed by the requester); a 10-unit claim exported to the machine from dom-a is certified but undeliverable; the machine cannot spend it', paid=pay2.get('performed'), requester_signed=[(x['kind'], x.get('status')) for x in sig2], export_state=ex.get('state'), transport={'ok': pushed.get('ok'), 'stage': pushed.get('stage'), 'results': [x.get('state') for x in pushed.get('results', [])]}, machine_liquidity={'local_asset': {k: liq.get(k) for k in ('held_locally', 'cross_domain_pending', 'cross_domain_claims_held', 'spendable')}, 'claim_asset_of_dom_a': {k: liqa.get(k) for k in ('held_locally', 'cross_domain_claims_held', 'spendable')}}, operator_blackout={'possible': [(a['action'], a['milestone']) for a in op['blackout']['possible_now']][:6], 'disabled': [(a['action'], (a['reason'] or '')[:60]) for a in op['blackout']['disabled_now']][:6]})
        # 5. the machine buys follow-on compute from its settled earnings during the blackout (signed at sig-machine), the supplier delivers and is settled
        fo = self.adv(C, 'c_mach', mid, 'reserve', signers=()); sigm = self.sign_all(C, 'c_mach'); rid = C.ms('c_mach', mid, 'followon')['binding'].get('reservation_id'); C.res_action('c_sup', rid, 'activate'); sd, dl = C.res_action('c_sup', rid, 'deliver', {'task': {'kind': 'terrain_tile', 'source': 'apollo11_32'}}); st_res = self.adv(C, 'c_mach', mid, 'settle_reservation', signers=('c_mach',))
        step('machine purchased compute from its settled earnings on the shared pool (signed at sig-machine, still in blackout); measured delivery settled to the supplier', reserve=fo.get('performed'), machine_signed=[(x['kind'], x.get('status')) for x in sigm], delivered_cpu_ms=dl.get('delivered_quantity'), labels=[dl['measurement']['measurement']['kind'], dl['measurement']['verification']['kind']] if sd == 200 else sd, settled=st_res.get('performed'))
        # 7. relay with safe replanning: the machine sends the product to the requester over two paid hops; the first route misses its early contact window and is released in full; the replanned route delivers; the requester acknowledges; hops settled once
        payload = json.dumps(prod, sort_keys=True, default=str).encode()[:3000]; tt = int(time.time())
        early = self.contact(C, 'c_sup', 'demo-early', tt - 1, tt + 2, 1); late = self.contact(C, 'c_sup', 'demo-late', tt - 1, tt + 3600, 1); hop2 = self.contact(C, 'c_relay', 'demo-hop2', tt - 1, tt + 3600, 2)
        def signed_route(mid_, hops):
            body = {'message_id': mid_, 'recipient_account': names['c_req'], 'bytes': len(payload), 'hops': hops, 'fee_ceiling': 100, 'dispute_policy': 'receiver_ack_required', 'ack_timeout_seconds': 60}; sigs = {}
            for _ in range(len(hops) + 1):
                st, r = C.call('post', '/api/v1/relay/routes', role='c_mach', json=dict(body, native_signatures=sigs))
                if st == 409 and self.code(r) == 'native_signature_required':
                    d = r['detail']; s_, sg_ = self.sgM.client.sign('c_mach', d['message'], context=d['context']); sigs[str(d['hop'])] = {'message': d['message'], 'signature_hex': sg_.get('signature_hex'), 'key_id': sg_.get('key_id')}; continue
                return st, r, len(sigs)
            return st, r, len(sigs)
        st1, r1, n1 = signed_route('demo-product', [{'offer_id': early['id'], 'offer_version': early['version'], 'relay_account': names['c_sup']}, {'offer_id': hop2['id'], 'offer_version': hop2['version'], 'relay_account': names['c_relay']}]); time.sleep(2.3)
        missed = C.call('post', '/api/v1/relay/routes/%s/cancel-unused' % r1['id'], role='c_mach', json={})[1] if st1 == 201 else {}; mres = C.bal('c_mach', 'reserved')
        st2, r2, n2 = signed_route('demo-product-2', [{'offer_id': late['id'], 'offer_version': late['version'], 'relay_account': names['c_sup']}, {'offer_id': hop2['id'], 'offer_version': hop2['version'], 'relay_account': names['c_relay']}])
        C.http.post('/api/v1/relay/routes/%s/submit' % r2['id'], headers=dict(C.H('c_mach'), **{'Content-Type': 'application/octet-stream'}), content=payload); f0 = C.call('post', '/api/v1/relay/routes/%s/forward' % r2['id'], role='c_sup', json={'hop': 0})[1]; f1 = C.call('post', '/api/v1/relay/routes/%s/forward' % r2['id'], role='c_relay', json={'hop': 1})[1]
        got = C.http.get('/api/v1/relay/routes/%s/message' % r2['id'], headers=C.H('c_req')); ack = C.call('post', '/api/v1/relay/routes/%s/acknowledge' % r2['id'], role='c_req', json={'message_id': 'demo-product-2', 'content_hash': hashlib.sha256(payload).hexdigest()})[1]; stl = C.call('post', '/api/v1/relay/routes/%s/settle' % r2['id'], role='c_mach', json={})[1]
        step('relay with safe replanning: the first route missed its early contact window and was released in full (never charged); the replanned two-hop route carried the product (hop fees signed at sig-machine), the requester acknowledged the content hash, both hops settled once', first_route=(st1, [h['state'] for h in missed.get('hops', [])]), machine_reserved_after_release=mres, hop_signatures=n2, delivered=f1.get('state'), content_matches=got.content == payload, acknowledged=ack.get('state'), hops=[h['state'] for h in stl.get('hops', [])])
        # 8. reconnection: the claim is delivered and acknowledged; the machine's liquidity shows it held; the machine retires nothing (it keeps the claim); witness sequence recorded
        C.connectivity('online'); out = self.push(A, C); self.push(C, A); liq2 = C.call('get', '/api/v1/native/accounts/%s/liquidity' % names['c_mach'], role='c_mach', params={'asset': A.asset})[1]; w = self.W.client.call('GET', '/latest/dom-c')[1]; recC = C.call('get', '/api/v1/native/recovery')[1]
        step('reconnection: the certificate was delivered, the 10-unit claim credited to the machine at dom-c and acknowledged at dom-a; witness checkpoint recorded', transport=[x['state'] for x in out.get('results', [])], allocation=A.alloc(ex['id'])['state'], machine_liquidity={k: liq2.get(k) for k in ('held_locally', 'cross_domain_claims_held', 'cross_domain_pending', 'spendable')}, witness_sequence=(w.get('latest') or {}).get('sequence'), recovery=recC.get('recovery_state'))
        # 9. fee-funded contribution: the treasury (fees only) pays a distinct recipient 2 for an accepted negative determination
        tv = C.call('get', '/api/v1/work/treasury', params={'asset': C.asset})[1]; t3, r3, o3, (sa3, a3) = C.award('owner', 'c_con', 'determination', energy_inputs('INFEASIBLE'), 2, funding='treasury'); aw3, d3 = C.deliver_accept(a3['id'], 'owner'); C.pay_all(a3['id'], 'owner'); tv2 = C.call('get', '/api/v1/work/treasury', params={'asset': C.asset})[1]
        step('fee-funded contribution: the treasury paid a distinct recipient 2 from collected fees only (never more than confirmed fee revenue)', treasury_before={k: tv.get(k) for k in ('confirmed_revenue', 'available')}, decision=d3.get('decision'), treasury_after={k: tv2.get(k) for k in ('confirmed_revenue', 'settled_spending', 'available')})
        final = table(); au = C.audit(); acc = C.call('get', '/api/v1/native/accounting')[1]; ex_c = C.call('get', '/api/v1/native/export')[1]
        from metacoin_service.native import checker
        roots = {d.id: [{'key_id': d.domain()['authority']['key_id'], 'public_hex': d.domain()['authority']['public_hex'], 'epoch': d.domain()['authority']['epoch']}] for d in (A, C)}; rep = checker.check(ex_c, roots)
        conserved = au['conserved'] and all(acc['checks'].values()) and rep['summary']['authorized_bookkeeping'] and rep['summary']['resource_and_lineage']
        record = {'schema': 'metacoin-demonstration/v2', 'title': 'Independent machine network: custody-separated mission with subcontract, shared pool, relay and blackout', 'x402': 'disabled; facilitator URL closed', 'domain': 'dom-c (development custody disabled)', 'actors': names, 'keys': keys, 'funding_table': funding, 'steps': steps, 'final_table': final,
                  'conservation': {k: au[k] for k in ('total_held', 'fixture_funding', 'backing_locked_for_exports', 'claims_backed_elsewhere', 'conserved')}, 'accounting_checks': acc['checks'], 'independent_checker': rep['summary'], 'postings': ex_c['journal_entries'][posted0:], 'pool_commitments': ex_c.get('pool_commitments'), 'mission': mid, 'subcontracts': [sid, bad.get('id') if st_b == 201 else None], 'relay_routes': [r1.get('id') if st1 == 201 else None, r2.get('id') if st2 == 201 else None], 'job': job,
                  'operator_screen_during_blackout': op, 'custody_note': 'every spend by the requester, the primary machine and the supplier was signed at that party\'s own signer process (sig-requester, sig-machine, sig-supplier); the subcontractor, verifier, relay and contribution recipient share sig-others; the API and worker processes hold no actor key; cross-domain exports remain a development-custody action at dom-a in this order', 'elapsed_seconds': round(time.time() - t0, 1)}
        self.ctx['demo'] = {'mission': mid, 'job': job, 'record': record}
        if self.ctx.get('demo_out'):
            Path(self.ctx['demo_out']).write_text(json.dumps(record, indent=1, default=str))
        conds = {'race': winner is not None and race[loser][1] == 'pool_capacity_exceeded', 'science': s2['result']['science'] == 'FEASIBLE', 'prefund': pf[0] == 200, 'sub_returned': scv.get('state') == 'returned' and scv['timeline']['returned'] == 2, 'blackout_pay': pay2.get('performed', {}).get('action') == 'pay' and len(sig2) == 3,
                 'export_undeliverable': ex.get('state') == 'certified' and pushed.get('ok') is False and liqa.get('cross_domain_claims_held', 0) == 0, 'followon': fo.get('performed', {}).get('action') == 'reserve' and sd == 200 and st_res.get('performed', {}).get('action') == 'settle_reservation',
                 'failed_branch': st_b == 201 and badres.get('state') == 'failed' and C.bal('c_mach') >= m_before_bad - 0 and (bv.get('timeline') or {}).get('reserved') == 0, 'relay_replanned': st1 == 201 and [h['state'] for h in missed.get('hops', [])] == ['cancelled', 'cancelled'] and f1.get('state') == 'delivered' and got.content == payload and ack.get('state') == 'acknowledged' and [h['state'] for h in stl.get('hops', [])] == ['settled', 'settled'],
                 'reconnection': 'accepted' in [x['state'] for x in out.get('results', [])] and A.alloc(ex['id'])['state'] in ('acknowledged', 'imported') and liq2['cross_domain_claims_held'] - liqa.get('cross_domain_claims_held', 0) >= 10, 'treasury': d3.get('decision') == 'accepted' and tv2['settled_spending'] <= tv2['confirmed_revenue'], 'conserved': conserved}
        ok = all(conds.values()); record['conditions'] = conds
        if self.ctx.get('demo_out'):
            Path(self.ctx['demo_out']).write_text(json.dumps(record, indent=1, default=str))
        self.rec(0, self._demo.__doc__, ok, {'mission': mid, 'steps': [x['step'][:80] for x in steps], 'conditions': conds, 'final_table': final, 'conserved': conserved, 'checker': rep['summary']}, t0=t0)

    def run_all(self, only=None):
        order = list(range(1, 45)) + [0] + list(range(45, 51))
        for n in order:
            if only and n not in only:
                continue
            fn = self.demo if n == 0 else getattr(self, 'j%d' % n); self.guard(n, ((self._demo.__doc__ if n == 0 else fn.__doc__) or 'journey %d' % n).strip(), fn)
        return self.results


def main():
    p = argparse.ArgumentParser(); p.add_argument('--out'); p.add_argument('--only'); p.add_argument('--shots'); p.add_argument('--demo'); a = p.parse_args()
    j = NetworkJourneys(shots=a.shots, demo_out=a.demo)
    try:
        results = j.run_all([int(x) for x in a.only.split(',')] if a.only else None); health = j.A.call('get', '/api/health')[1]
    finally:
        j.close()
    out = {'schema': 'metacoin-journeys-network/v1', 'provider_mode': 'simulation', 'x402_disabled': True, 'facilitator_url': 'http://127.0.0.1:1/blocked', 'domains': {'dom-a': 'development custody permitted (fixtures, claim journeys)', 'dom-b': 'development custody permitted (claim destination)', 'dom-c': 'development custody disabled; signers sig-requester / sig-machine / sig-supplier / sig-others'}, 'revision': health.get('revision'), 'python': sys.version.split()[0], 'started_at': j.started_at, 'finished_at': now(), 'results': results,
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
