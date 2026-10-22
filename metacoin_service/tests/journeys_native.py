"""Order 2026-10-22 §19: the forty acceptance journeys of the MetaCoin-native space-work and settlement protocol, through real
entry points: two separately configured settlement domains (separate homes, databases, authority keys, API and worker
processes) on one authorized host, the courier adapters, the client CLI, a separate conforming MCP client process, the
console (and a real browser when METACOIN_PLAYWRIGHT_PYTHON is set), x402 disabled and the facilitator URL pointed at a
closed loopback port.

    METACOIN_X402_DISABLED=1 PYTHONPATH=. .venv-service/bin/python -m metacoin_service.tests.journeys_native --out journeys-native.json [--only 1,2] [--shots DIR] [--live-home PATH]

A journey passes only when its stated scope was actually met; blocked names the exact dependency."""
import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
from metacoin_service import crypto
from metacoin_service.db import now
from metacoin_service.lunar import evidence as lunar_evidence, reference as lunar_ref, scenario as lunar_scenario, sources as lunar_sources
from metacoin_service.lunar.__main__ import example as lunar_example
from metacoin_service.native import checker as native_checker, courier, model as native_model, procurement, protocol as P, vectors as native_vectors
from metacoin_service.tests.test_service import ENV, ROOT, Instance, free_port
from metacoin_service.tests.test_work_terms import energy_inputs

PY = sys.executable
LIMITS = {'test_hooks': 1, 'job_timeout_seconds': 180}
LAST_ERRORS = []


class Endpoint:
    def __init__(self, dom, headers):
        self.dom, self.h = dom, headers

    def call(self, method, path, body=None, raw=None):
        if raw is not None:
            r = self.dom.http.request(method, path, headers=dict(self.h, **{'Content-Type': 'application/json'}), content=raw)
        else:
            r = self.dom.http.request(method, path, headers=self.h, json=body) if body is not None else self.dom.http.request(method, path, headers=self.h)
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, {'text': r.text[:200]}


class Dom:
    """One settlement domain: its own home, database, keys, API process and background worker."""
    def __init__(self, did):
        self.id = did; self.inst = Instance(provider_mode='simulation'); self.port = free_port(); self.base = 'http://127.0.0.1:%d' % self.port
        self.env = dict(ENV, METACOIN_LIMITS_JSON=json.dumps(LIMITS), METACOIN_X402_DISABLED='1', METACOIN_FACILITATOR_URL='http://127.0.0.1:1/blocked')
        self.http = httpx.Client(base_url=self.base, timeout=180); self.workers = []; self.stop_files = []; self.actors = {}
        self.start_api(); self.start_worker('w-' + did)
        st, d = self.call('post', '/api/v1/native/domain', json={'domain_id': did, 'operator': 'same operator (journey host)', 'host': 'this host'}); assert st == 201, d
        self.asset = d['default_asset']; self.treasury = d['accounts']['treasury']

    def start_api(self):
        self.api_log = open(Path(self.inst.temp.name) / 'api.log', 'ab')
        self.api = subprocess.Popen([PY, '-m', 'metacoin_service', '--home', str(self.inst.home), '--provider-mode', 'simulation', 'serve', '--port', str(self.port)], cwd=ROOT, env=self.env, stdout=subprocess.DEVNULL, stderr=self.api_log)
        for _ in range(300):
            try:
                if httpx.get(self.base + '/api/health', timeout=1).status_code == 200:
                    return
            except Exception:
                time.sleep(0.1)
        raise SystemExit('api %s did not start' % self.id)

    def restart_api(self):
        self.api.terminate(); self.api.wait(timeout=20); self.start_api()

    def start_worker(self, name):
        stop = Path(self.inst.temp.name) / ('stop-' + name)
        p = subprocess.Popen([PY, '-m', 'metacoin_service', '--home', str(self.inst.home), '--provider-mode', 'simulation', 'worker', '--name', name, '--stop-file', str(stop)], cwd=ROOT, env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.workers.append(p); self.stop_files.append(stop); return p

    def stop_workers(self):
        for s in self.stop_files:
            s.write_text('stop')
        for p in self.workers:
            try:
                p.wait(timeout=60)
            except subprocess.TimeoutExpired:
                p.terminate(); p.wait(timeout=10)
        self.workers, self.stop_files = [], []

    def close(self):
        self.stop_workers(); self.api.terminate(); self.api.wait(timeout=20); self.api_log.close(); self.inst.close()

    def H(self, role='owner'):
        return {'Authorization': 'Bearer ' + (self.inst.tok[role] if role in self.inst.tok else self.actors[role]['token'])}

    def call(self, method, path, role='owner', h=None, **kw):
        r = getattr(self.http, method)(path, headers=h or self.H(role), **kw)
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, {'text': r.text[:300]}

    def ep(self, role='owner'):
        return Endpoint(self, self.H(role))

    def cred_file(self, role):
        p = Path(self.inst.temp.name) / ('cred-%s-%s.json' % (self.id, role)); p.write_text(json.dumps({'token': self.inst.tok[role] if role in self.inst.tok else self.actors[role]['token']})); os.chmod(p, 0o600); return p

    def actor(self, name, fund=0):
        if name in self.actors:
            return self.actors[name]
        st, a = self.call('post', '/api/v1/native/actors', json={'name': name}); assert st == 201, a
        self.actors[name] = {'account': a['account_id'], 'principal_id': a['principal_id'], 'token': a['credential']['token']}
        if fund:
            self.fund(name, fund)
        return self.actors[name]

    def fund(self, name, amount):
        st, r = self.call('post', '/api/v1/native/fixtures/fund', json={'account_id': self.actors[name]['account'], 'amount': amount}); assert st == 201, r; return r

    def provider(self, name, kinds=('energy_audit', 'terrain_tile', 'route_feasibility', 'scenario_pack', 'evidence_acquisition')):
        st, p = self.call('post', '/api/v1/work/providers', json={'name': name + '-provider', 'principal_id': self.actors[name]['principal_id'], 'capabilities': {'kinds': list(kinds), 'verification_classes': ['analytical', 'full_exact'], 'payment_schemes': ['exact', 'upto']}})
        assert st in (201, 409), p; self.actors[name]['provider_id'] = p.get('id'); return p

    def bal(self, account, cat='available', asset=None):
        st, a = self.call('get', '/api/v1/native/accounts/' + (self.actors[account]['account'] if account in self.actors else account), params={'asset': asset} if asset else None); assert st == 200, a; return a[cat]

    def audit(self, asset=None):
        return self.call('get', '/api/v1/native/audit', params={'asset': asset or self.asset})[1]

    def alloc(self, aid):
        return self.call('get', '/api/v1/native/allocations/' + aid)[1]

    def domain(self):
        return self.call('get', '/api/v1/native/domain')[1]

    def fault(self, point, disarm=False):
        st, r = self.call('post', '/api/v1/ops/faults', json={'fault': point, 'disarm': disarm}); assert st == 200, r

    def export(self, who, to, recipient, amount, rid):
        return self.call('post', '/api/v1/native/allocations/export', role=who, json={'destination_domain': to.id, 'recipient_account': recipient, 'amount': amount, 'request_id': rid})

    def pin(self, other):
        d = other.domain(); st, r = self.call('post', '/api/v1/native/peers', json={'domain_id': other.id, 'authority_key_id': d['authority']['key_id'], 'authority_public_hex': d['authority']['public_hex'], 'epoch': d['authority']['epoch']}); assert st == 201, r

    # ---- work path through real HTTP with the background worker ------------------------------------------------------------------
    def terms(self, who, template, price, bps=0, verifier=0, verifier_pay_to=None, funding='requester', scheme='exact', extra=None):
        ceiling = price + price * bps // 10000 + verifier
        st, t = self.call('post', '/api/v1/work/terms', role=who, json={'template': template, 'ceiling': ceiling, 'amount': price, 'asset': self.asset}); assert st == 201, t
        pay = dict(t['terms']['payment'], scheme=scheme, fee_policy={'schema': 'metacoin-fee-policy/v1', 'treasury_bps': bps, 'rounding': 'floor_fee_remainder_to_provider'}, funding=funding, verifier_compensation=verifier)
        if verifier:
            pay['verifier_pay_to'] = verifier_pay_to
        body = {'terms': dict({'payment': pay, 'eligibility': dict(t['terms']['eligibility'], payment_schemes=[scheme])}, **(extra or {}))}
        st, t2 = self.call('post', '/api/v1/work/terms/' + t['id'], role=who, json=body); assert st == 200, t2; return t2

    def award(self, who, prov, template, inputs, price, offer_extra=None, **kw):
        t = self.terms(who, template, price, **kw)
        st, f = self.call('post', '/api/v1/work/terms/' + t['id'] + '/freeze', role=who, json={'inputs': inputs}); assert st == 200, f
        if kw.get('funding') == 'treasury':
            st, al = self.call('post', '/api/v1/work/treasury/allocate', json={'terms_id': t['id']}); assert st == 201, al
        st, r = self.call('post', '/api/v1/work/requests', role=who, json={'terms_id': t['id']}); assert st == 201, r
        self.call('post', '/api/v1/work/requests/' + r['id'] + '/open', role=who, json={})
        cls = 'analytical' if template.startswith('lunar') else 'full_exact'
        st, o = self.call('post', '/api/v1/work/requests/' + r['id'] + '/offers', role=prov, json=dict({'price_amount': price, 'asset': self.asset, 'scheme': kw.get('scheme', 'exact'), 'window_seconds': 3600, 'verification': {'class': cls}}, **(offer_extra or {}))); assert st == 201, o
        st, a = self.call('post', '/api/v1/work/requests/' + r['id'] + '/award', role=who, json={'offer_id': o['id']})
        if st != 201:
            LAST_ERRORS.append({'award': a, 'offer': o.get('state'), 'eligibility': o.get('eligibility'), 'who': who, 'template': template})
        return t, r, o, (st, a)

    def wait_execution(self, aid, who='owner', timeout=240):
        deadline = time.time() + timeout
        while time.time() < deadline:
            st, v = self.call('get', '/api/v1/work/awards/' + aid, role=who)
            ex = v['milestones'][0]['dimensions']['execution']
            if ex in ('completed', 'failed'):
                return v
            time.sleep(0.5)
        return v

    def deliver_accept(self, aid, who, decision='accept', timeout=240):
        v = self.wait_execution(aid, who, timeout)
        if v['milestones'][0]['dimensions']['execution'] != 'completed':
            return v, None
        st, vr = self.call('post', '/api/v1/work/awards/%s/milestones/m1/verify' % aid, role=who, json={}); assert st == 202, vr
        deadline = time.time() + timeout
        while time.time() < deadline:
            st, ev = self.call('post', '/api/v1/work/awards/%s/milestones/m1/evaluate' % aid, role=who, json={})
            if ev.get('decision_candidate') in ('accepted', 'rejected'):
                break
            time.sleep(0.5)
        st, d = self.call('post', '/api/v1/work/awards/%s/milestones/m1/decide' % aid, role=who, json={'decision': decision})
        return self.call('get', '/api/v1/work/awards/' + aid, role=who)[1], d

    def pay_all(self, aid, who):
        st, aw = self.call('get', '/api/v1/work/awards/' + aid, role=who); out = {}
        for e in aw['entitlements']:
            if e['state'] != 'payable':
                continue
            st, i = self.call('post', '/api/v1/work/entitlements/' + e['id'] + '/prepare', role=who, json={}); assert st == 201, i
            st, a = self.call('post', '/api/v1/work/intents/' + i['id'] + '/authorize', role=who, json={}); assert st == 200, a
            st, s = self.call('post', '/api/v1/work/intents/' + i['id'] + '/submit', role=who, json={}); assert st == 200, s
            out[e['kind']] = s
        return out


class Journeys:
    def __init__(self):
        self.started_at = now(); self.results = []; self.ctx = {}
        self.A = Dom('dom-a'); self.B = Dom('dom-b'); self.A.pin(self.B); self.B.pin(self.A)

    def close(self):
        for d in (self.A, self.B) + tuple(self.ctx.get('extra_domains', [])):
            try:
                d.close()
            except Exception:
                pass

    def rec(self, n, title, ok, evidence, blocked=None, caveat=None, t0=None):
        ev = dict(evidence, seconds=round(time.time() - t0, 3) if t0 else None)
        self.results.append({'journey': n, 'title': title, 'status': 'blocked' if blocked else ('passed' if ok else 'failed'), 'evidence': dict(ev, blocked=blocked) if blocked else ev, 'caveat': caveat})

    def guard(self, n, title, fn):
        t0 = time.time()
        try:
            fn(t0)
        except Exception as exc:
            import traceback
            self.rec(n, title, False, {'exception': type(exc).__name__, 'detail': str(exc)[:300], 'trace': traceback.format_exc()[-700:], 'last_award_errors': LAST_ERRORS[-2:]}, t0=t0)

    def push(self, src, dst):
        return courier.push(src.ep(), dst.ep(), dst.id)

    # ---- native independence and authority (1–7) ---------------------------------------------------------------------------------
    def j1(self, t0):
        """x402 disabled and the facilitator blocked: accounts created, work accepted and settled natively."""
        A = self.A; st, cap = A.call('get', '/api/v1/capabilities'); st2, h = A.call('get', '/api/health')
        req = A.actor('requester', 80); mach = A.actor('machine'); A.actor('verifier'); A.actor('supplier'); A.provider('machine'); A.provider('supplier')
        t, r, o, (sa, a) = A.award('requester', 'machine', 'determination', energy_inputs('FEASIBLE'), 5)
        aw, d = A.deliver_accept(a['id'], 'requester'); paid = A.pay_all(a['id'], 'requester')
        ok = cap['x402']['disabled'] and not cap['x402']['required_by_native_core'] and cap['native']['domain'] == 'dom-a' and sa == 201 and d and d.get('decision') == 'accepted' and paid.get('provider', {}).get('rail') == 'native' and A.bal('machine') == 5 and A.bal('requester') == 75
        self.rec(1, self.j1.__doc__, ok, {'x402': cap['x402'], 'native': cap['native']['domain'], 'facilitator_url': 'http://127.0.0.1:1/blocked', 'award': a.get('id'), 'rail': paid.get('provider', {}).get('rail'), 'machine': A.bal('machine'), 'requester': A.bal('requester'), 'revision': h.get('revision')}, t0=t0)

    def j2(self, t0):
        """A provider becomes a buyer with the same earned balance: no hidden requester account."""
        A = self.A; before = A.bal('machine')
        t, r, o, (sa, a) = A.award('machine', 'supplier', 'determination', energy_inputs('INFEASIBLE'), 3)
        aw, d = A.deliver_accept(a['id'], 'machine'); paid = A.pay_all(a['id'], 'machine')
        st, acct = A.call('get', '/api/v1/native/accounts/me', role='machine')
        ok = sa == 201 and d.get('decision') == 'accepted' and A.bal('machine') == before - 3 and A.bal('supplier') == 3 and acct['principal_id'] == A.actors['machine']['principal_id'] and paid['provider']['rail'] == 'native'
        self.rec(2, self.j2.__doc__, ok, {'machine_before': before, 'machine_after': A.bal('machine'), 'supplier': A.bal('supplier'), 'same_identity': acct['id'] == A.actors['machine']['account']}, t0=t0)

    def j3(self, t0):
        """Read-only credentials cannot authorize or spend; a delegated actor cannot expand its limits."""
        A = self.A; mach = A.actors['machine']['account']
        sv, rv = A.call('post', '/api/v1/native/transfers', role='viewer', json={'to_account': mach, 'amount': 1, 'request_id': 'v1'})
        st, c = A.call('post', '/api/v1/credentials', json={'operations': ['native:read'], 'lifetime_seconds': 600})
        sro = A.call('post', '/api/v1/native/transfers', h={'Authorization': 'Bearer ' + c.get('token', 'x')}, json={'to_account': mach, 'amount': 1, 'request_id': 'ro1'})[0] if st == 201 else None
        pol = {'schema': 'metacoin-agent-policy/v1', 'permitted_services': ['energy_audit'], 'allowed_operations': ['native:read', 'native:transfer'], 'ceilings': {'total_amount': 5, 'per_action_amount': 3, 'max_jobs': 1, 'max_workflows': 0, 'concurrency': 1}, 'validity_seconds': 3600, 'review_gate_mandatory': False, 'input_visibility': 'own'}
        sg, g = A.call('post', '/api/v1/agents/grants', json={'policy': pol})
        owner_acct = A.call('get', '/api/v1/native/accounts/me')[1]['id']; A.call('post', '/api/v1/native/fixtures/fund', json={'account_id': owner_acct, 'amount': 20})
        ah = {'Authorization': 'Bearer ' + g['token']}
        s1 = A.call('post', '/api/v1/native/transfers', h=ah, json={'to_account': mach, 'amount': 3, 'request_id': 'ag1'})[0]
        s2 = A.call('post', '/api/v1/native/transfers', h=ah, json={'to_account': mach, 'amount': 3, 'request_id': 'ag2'})[0]
        s3 = A.call('post', '/api/v1/native/transfers', h=ah, json={'to_account': mach, 'amount': 4, 'request_id': 'ag3'})[0]
        s4 = A.call('post', '/api/v1/agents/grants', h=ah, json={'policy': dict(pol, ceilings=dict(pol['ceilings'], total_amount=1000))})[0]
        ok = sv == 403 and sro == 403 and sg == 201 and s1 == 201 and s2 == 400 and s3 == 400 and s4 == 403
        self.rec(3, self.j3.__doc__, ok, {'viewer': sv, 'scoped_readonly': sro, 'agent_within': s1, 'agent_over_total': s2, 'agent_over_per_action': s3, 'agent_widening_grant': s4, 'owner_after': A.bal(owner_acct)}, t0=t0)

    def j4(self, t0):
        """Concurrent spending cannot exceed the account envelope."""
        A = self.A; a = A.actor('racer', 50); b = A.actor('racee'); results = []; lock = threading.Lock()
        def go(i):
            st, r = A.call('post', '/api/v1/native/transfers', role='racer', json={'to_account': b['account'], 'amount': 10, 'request_id': 'race-%d' % i})
            with lock:
                results.append(st)
        th = [threading.Thread(target=go, args=(i,)) for i in range(12)]; [t.start() for t in th]; [t.join() for t in th]
        ok = results.count(201) == 5 and A.bal('racer') == 0 and A.bal('racee') == 50 and A.audit()['conserved']
        self.rec(4, self.j4.__doc__, ok, {'statuses': sorted(results), 'racer': A.bal('racer'), 'racee': A.bal('racee')}, t0=t0)

    def j5(self, t0):
        """Cross-environment, cross-asset, wrong-scale and wrong-recipient messages fail before value changes."""
        A, B = self.A, self.B; before = A.audit()['total_held']; mach = A.actors['machine']['account']
        s_asset = A.call('post', '/api/v1/native/transfers', role='requester', json={'to_account': mach, 'amount': 1, 'request_id': 'x-asset', 'asset': 'dev:dom-a:ledger-dom-a:OTHER:0'})[0]
        s_scale = A.call('post', '/api/v1/native/transfers', role='requester', json={'to_account': mach, 'amount': 1, 'request_id': 'x-scale', 'asset': A.asset[:-1] + '2'})[0]
        s_rcpt = A.call('post', '/api/v1/native/transfers', role='requester', json={'to_account': 'na_nobody', 'amount': 1, 'request_id': 'x-rcpt'})[0]
        s_env = A.call('post', '/api/v1/native/transfers', role='requester', json={'to_account': mach, 'amount': 1, 'request_id': 'x-env', 'asset': 'production:dom-a:ledger-dom-a:TMETA:0'})[0]
        # an inter-domain envelope from another environment / to another receiver / with another asset origin
        a = A.actor('alice', 300); st, al = A.export('alice', B, B.actor('bob')['account'], 3, 'j5'); ob = A.call('get', '/api/v1/native/transport/outbox', params={'receiver': 'dom-b'})[1]
        bad = []
        for field, value in (('environment', 'test'), ('receiver', 'dom-c')):
            t = json.loads(json.dumps(ob)); t['items'][0]['envelope'][field] = value; res = B.call('post', '/api/v1/native/transport/inbox', json=t)[1]; bad.append(res['results'][0]['reason'])
        t = json.loads(json.dumps(ob)); t['items'][0]['payload']['asset'] = 'dev:dom-b:ledger-dom-b:TMETA:0'; res = B.call('post', '/api/v1/native/transport/inbox', json=t)[1]; bad.append(res['results'][0]['reason'])
        ok = s_asset in (400, 422) and s_scale in (400, 422) and s_rcpt in (404, 422) and s_env in (400, 422) and A.audit()['total_held'] == before + 300 - 3 and B.bal('bob', asset=A.asset) == 0 and all(r in ('envelope_signature', 'bad_signature', 'wrong_receiver', 'wrong_environment', 'payload_digest_mismatch', 'envelope_binding') for r in bad)
        self.rec(5, self.j5.__doc__, ok, {'wrong_asset': s_asset, 'wrong_scale': s_scale, 'wrong_recipient': s_rcpt, 'wrong_environment': s_env, 'envelope_rejections': bad, 'bob_claim': B.bal('bob', asset=A.asset)}, t0=t0)
        self.ctx['j5_alloc'] = al.get('id')

    def j6(self, t0):
        """A replayed payment or an alternate request id for the same settled obligation cannot pay twice."""
        A = self.A; st, aws = A.call('get', '/api/v1/work/awards', role='requester'); aid = next(a['id'] for a in aws['items'] if a['awarded_by'] == A.actors['requester']['principal_id']) if isinstance(aws, dict) else None
        st, aw = A.call('get', '/api/v1/work/awards/' + aid, role='requester'); e = [x for x in aw['entitlements'] if x['kind'] == 'provider'][0]
        s1, i1 = A.call('post', '/api/v1/work/entitlements/' + e['id'] + '/prepare', role='requester', json={})
        s2, sub = A.call('post', '/api/v1/work/intents/' + e['payment_intent_id'] + '/submit', role='requester', json={})
        st, obs = A.call('get', '/api/v1/native/obligations', role='requester'); ob = next((o for o in obs['items'] if o['ref_type'] == 'work_milestone' and o['state'] == 'settled' and o['kind'] == 'provider'), None)
        s3, r3 = A.call('post', '/api/v1/native/transfers', role='requester', json={'to_account': ob['payee_account'], 'amount': ob['amount'], 'request_id': 'alt-' + ob['id'], 'obligation_id': ob['id']})
        mach_before = A.bal('machine')
        ok = s1 in (409, 201) and (s1 != 201 or i1.get('state') in ('settled', 'authorized')) and s2 == 200 and sub.get('replayed') and s3 == 409 and A.bal('machine') == mach_before
        self.rec(6, self.j6.__doc__, ok, {'prepare_again': s1, 'submit_again': {'status': s2, 'replayed': sub.get('replayed')}, 'alternate_request_id': {'status': s3, 'code': (r3.get('detail') or {}).get('code') if isinstance(r3.get('detail'), dict) else r3.get('detail')}}, t0=t0)

    def j7(self, t0):
        """Partial payment and refund behaviour is bounded or explicitly refused."""
        A = self.A; a = A.actor('alice'); c = A.actor('carla')
        st, tr = A.call('post', '/api/v1/native/transfers', role='alice', json={'to_account': c['account'], 'amount': 10, 'request_id': 'ref-src'})
        s_over, r_over = A.call('post', '/api/v1/native/refunds', role='carla', json={'intent_id': tr['id'], 'amount': 11, 'request_id': 'ref-over'})
        s_part, r_part = A.call('post', '/api/v1/native/refunds', role='carla', json={'intent_id': tr['id'], 'amount': 4, 'request_id': 'ref-part'})
        s_rest, r_rest = A.call('post', '/api/v1/native/refunds', role='carla', json={'intent_id': tr['id'], 'amount': 7, 'request_id': 'ref-rest'})
        # partial payment of an exact obligation is refused
        st, obs = A.call('get', '/api/v1/native/obligations'); ob = next((o for o in obs['items'] if o['state'] == 'settled'), None)
        s_pp = A.call('post', '/api/v1/native/transfers', role='requester', json={'to_account': ob['payee_account'], 'amount': 1, 'request_id': 'pp-1', 'obligation_id': ob['id']})[0] if ob else None
        ok = s_over == 409 and s_part == 201 and r_part['state'] == 'settled' and s_rest == 409 and A.bal('carla') == 6
        self.rec(7, self.j7.__doc__, ok, {'refund_over': s_over, 'refund_partial': s_part, 'refund_beyond_remaining': s_rest, 'partial_payment_of_exact_obligation': s_pp, 'carla': A.bal('carla'), 'alice': A.bal('alice')}, t0=t0)

    # ---- domains and disconnected operation (8–20) --------------------------------------------------------------------------------
    def j8(self, t0):
        """A source export makes its allocated value unavailable before an importable certificate is released."""
        A = self.A; a = A.actor('alice'); before = A.bal('alice'); A.fault('native_export_reserve')
        st, r = A.export('alice', self.B, self.B.actor('bob')['account'], 5, 'j8')
        A.fault('native_export_reserve', disarm=True)
        mid = A.bal('alice'); it = A.bal('alice', 'in_transit'); st2, res = A.call('post', '/api/v1/native/allocations/recover', json={})
        allocs = A.call('get', '/api/v1/native/allocations')[1]['items']; al = next(x for x in allocs if x['evidence'].get('request_id') == 'j8')
        ok = st == 409 and mid == before - 5 and it == 5 and al['state'] == 'certified' and A.bal('alice', 'in_transit') == 0 and A.audit()['backing_locked_for_exports'] >= 5
        self.rec(8, self.j8.__doc__, ok, {'crash_at_reserve': st, 'available_after_reserve': mid, 'in_transit': it, 'after_recovery': al['state'], 'resumed': res.get('resumed')}, t0=t0); self.ctx['j8_alloc'] = al['id']

    def j9(self, t0):
        """Destination import consumes an allocation once; duplicate delivery is idempotent."""
        A, B = self.A, self.B; before = B.bal('bob', asset=A.asset); out = self.push(A, B)
        ob = A.call('get', '/api/v1/native/transport/outbox', params={'receiver': 'dom-b', 'all': '1'})[1]; again = B.call('post', '/api/v1/native/transport/inbox', json=ob)[1]
        after = B.bal('bob', asset=A.asset); credited = sum(x['amount'] for x in A.call('get', '/api/v1/native/allocations')[1]['items'] if x['state'] in ('acknowledged', 'certified') and x['direction'] == 'export')
        ok = out['ok'] and all(r['state'] == 'accepted' for r in out['results']) and all(r['state'] == 'duplicate' for r in again['results']) and after == before + credited and len(again['replies']) >= 1
        self.rec(9, self.j9.__doc__, ok, {'first': [r['state'] for r in out['results']], 'duplicate': [r['state'] for r in again['results']], 'bob_before': before, 'bob_after': after, 'acks_replayed': len(again['replies'])}, t0=t0)

    def j10(self, t0):
        """A certificate addressed to domain B cannot credit domain C."""
        A, B = self.A, self.B; C = Dom('dom-c'); self.ctx.setdefault('extra_domains', []).append(C); C.pin(A); C.actor('cbob')
        st, al = A.export('alice', B, B.actors['bob']['account'], 2, 'j10'); ob = A.call('get', '/api/v1/native/transport/outbox', params={'receiver': 'dom-b'})[1]
        rc = C.call('post', '/api/v1/native/transport/inbox', json=ob)[1]
        forged = json.loads(json.dumps(ob)); forged['items'][0]['envelope']['receiver'] = 'dom-c'; rf = C.call('post', '/api/v1/native/transport/inbox', json=forged)[1]
        ok = rc['results'][0]['reason'] == 'wrong_receiver' and rf['results'][0]['reason'] == 'bad_signature' and C.audit(A.asset)['total_held'] == 0 if C.call('get', '/api/v1/native/audit', params={'asset': A.asset})[0] == 200 else rc['results'][0]['reason'] == 'wrong_receiver' and rf['results'][0]['reason'] == 'bad_signature'
        self.rec(10, self.j10.__doc__, ok, {'as_is': rc['results'][0]['reason'], 'readdressed': rf['results'][0]['reason']}, t0=t0)

    def j11(self, t0):
        """A lost import acknowledgment cannot trigger an unsafe source refund or re-export."""
        A, B = self.A, self.B; ob = A.call('get', '/api/v1/native/transport/outbox', params={'receiver': 'dom-b'})[1]
        r = B.call('post', '/api/v1/native/transport/inbox', json=ob)[1]                                            # ack reply dropped
        al = next(x for x in A.call('get', '/api/v1/native/allocations')[1]['items'] if x['evidence'].get('request_id') == 'j10')
        avail = A.bal('alice'); s_over = A.call('post', '/api/v1/native/transfers', role='alice', json={'to_account': A.actors['machine']['account'], 'amount': avail + 1, 'request_id': 'j11-over'})[0]
        s_ret = A.call('post', '/api/v1/native/allocations/' + al['id'] + '/return', json={})[0]
        s_re = A.export('alice', B, B.actors['bob']['account'], 2, 'j10')[1]
        ok = r['results'][0]['state'] == 'accepted' and al['state'] == 'certified' and s_over == 400 and s_ret == 404 and s_re.get('replayed') and A.audit()['backing_locked_for_exports'] >= 2
        self.rec(11, self.j11.__doc__, ok, {'state_without_ack': al['state'], 'spend_beyond_available': s_over, 'return_from_source': s_ret, 'reexport_same_request': s_re.get('replayed')}, t0=t0)

    def j12(self, t0):
        """Crash at each export/import persistence boundary recovers without duplicated spendable ownership."""
        A, B = self.A, self.B; fix = A.audit()['fixture_funding']; ev = {}
        for point, durable in (('native_export_reserve', 'reserved'), ('native_export_commit', 'export_committed'), ('native_certificate_publish', 'certified')):
            A.fault(point); st, r = A.export('alice', B, B.actors['bob']['account'], 1, 'crash-' + point); A.fault(point, disarm=True)
            al = next(x for x in A.call('get', '/api/v1/native/allocations')[1]['items'] if x['evidence'].get('request_id') == 'crash-' + point)
            st2, r2 = A.export('alice', B, B.actors['bob']['account'], 1, 'crash-' + point); ev[point] = {'crash': st, 'durable': al['state'], 'retry': r2.get('state')}
        certs = A.call('get', '/api/v1/native/transport/outbox', params={'receiver': 'dom-b'})[1]
        for point in ('native_import_commit', 'native_ack'):
            B.fault(point); st, r = B.call('post', '/api/v1/native/transport/inbox', json=certs); B.fault(point, disarm=True); ev[point] = {'delivery_status': st, 'bob_after': B.bal('bob', asset=A.asset)}
        out = self.push(A, B)
        held = A.audit()['total_held'] + B.audit(A.asset)['total_held']
        ok = all(v['crash'] == 409 and v['retry'] == 'certified' for k, v in ev.items() if k.startswith('native_export') or k == 'native_certificate_publish') and all(ev[k]['delivery_status'] == 409 for k in ('native_import_commit', 'native_ack')) and out['ok'] and held == fix and A.audit()['conserved'] and B.audit(A.asset)['conserved']
        self.rec(12, self.j12.__doc__, ok, dict(ev, held_both=held, fixture=fix), t0=t0)

    def j13(self, t0):
        """Disjoint preallocations support bounded work while the inter-domain link is cut."""
        A, B = self.A, self.B; bob = B.actors['bob']; carol = B.actor('carol'); B.provider('carol', kinds=('energy_audit',)) if 'provider_id' not in carol else None
        st, al = A.export('alice', B, bob['account'], 6, 'prealloc'); B.call('post', '/api/v1/native/domain/connectivity', json={'state': 'cut'})
        cut = self.push(A, B); path = Path(B.inst.temp.name) / 'a-to-b.json'; w = courier.write_bundle(A.ep(), 'dom-b', str(path)); imp = courier.import_bundle(B.ep(), str(path), str(Path(B.inst.temp.name) / 'replies.json'))
        pending = B.call('get', '/api/v1/native/accounts/' + bob['account'], params={'asset': A.asset})[1]['pending_inter_domain_claims']
        # bounded local work while cut: bob awards carol a determination paid in the claim asset
        t, r, o, (sa, a) = B.award('bob', 'carol', 'determination', energy_inputs('FEASIBLE'), 4) if False else (None, None, None, (None, None))
        avail = B.bal('bob', asset=A.asset); carol_before = B.bal('carol', asset=A.asset)
        s_ok = B.call('post', '/api/v1/native/transfers', role='bob', json={'to_account': carol['account'], 'amount': avail, 'asset': A.asset, 'request_id': 'cut-all'})[0]
        ok = cut.get('error', {}).get('detail', {}).get('code') == 'link_cut' and w['ok'] and imp['ok'] and imp['results'][0]['state'] == 'accepted' and pending >= 6 and s_ok == 201 and B.bal('carol', asset=A.asset) == carol_before + avail and B.bal('bob', asset=A.asset) == 0
        self.rec(13, self.j13.__doc__, ok, {'online_refused': cut.get('error', {}).get('detail', {}).get('code'), 'bundle_items': w.get('items'), 'import': imp['results'][0]['state'], 'pending_claims_before_ack': pending, 'local_spend': s_ok}, t0=t0)
        self.ctx['j13'] = {'alloc': al['id'], 'replies': str(Path(B.inst.temp.name) / 'replies.json'), 'imp': imp}

    def j14(self, t0):
        """Exhausted authority refuses further commitments without destroying completed work."""
        A, B = self.A, self.B; s_no, r_no = B.call('post', '/api/v1/native/transfers', role='bob', json={'to_account': B.actors['carol']['account'], 'amount': 1, 'asset': A.asset, 'request_id': 'cut-7'})
        st, ints = B.call('get', '/api/v1/native/intents', role='bob'); settled = [i for i in ints['items'] if i['state'] == 'settled' and i['actor_account'] == B.actors['bob']['account']]
        # link back: replies carried by hand, source marks acknowledged
        j13 = self.ctx['j13']; i2 = courier.import_bundle(A.ep(), j13['replies']); courier.mark_from_results(A.ep(), j13['imp']['results']); courier.mark_from_results(B.ep(), i2['results'])
        B.call('post', '/api/v1/native/domain/connectivity', json={'state': 'online'})
        ok = s_no == 400 and (r_no.get('detail') or {}).get('code') == 'insufficient_available' and len(settled) >= 1 and B.bal('bob', asset=A.asset) == 0 and A.alloc(j13['alloc'])['state'] == 'acknowledged'
        self.rec(14, self.j14.__doc__, ok, {'refused': (r_no.get('detail') or {}).get('code'), 'completed_transfers_kept': len(settled), 'carol': B.bal('carol', asset=A.asset), 'alloc_after_ack': A.alloc(j13['alloc'])['state']}, t0=t0)

    def j15(self, t0):
        """A conflicting signed history (cloned authority) is detected and quarantined under the documented trust model."""
        A, B = self.A, self.B; carol0 = B.bal('carol', asset=A.asset)
        st, al = A.export('alice', B, B.actors['bob']['account'], 1, 'twin-src'); self.push(A, B); cert = A.alloc(al['id'])['certificate']
        twin = dict(cert, recipient_account=B.actors['carol']['account']); key = crypto.load_signing_key(A.inst.settings.keys_dir / 'native' / 'authority-dom-a.ed25519')
        env = {'protocol': P.PROTOCOL, 'type': 'envelope', 'id': 'nenv_twin_j15', 'environment': 'dev', 'sender': 'dom-a', 'receiver': 'dom-b', 'message_type': 'allocation_certificate', 'causality': al['id'], 'payload_digest': P.digest(twin), 'expires_at': 4102444800, 'created_at': cert['issued_at']}
        item = {'envelope': env, 'envelope_signature': crypto.sign(key, P.signing_bytes('envelope', env)), 'key_id': A.domain()['authority']['key_id'], 'payload': twin, 'payload_signature': crypto.sign(key, P.signing_bytes('allocation_certificate', twin))}
        r = B.call('post', '/api/v1/native/transport/inbox', json={'items': [item]})[1]; rec = B.call('get', '/api/v1/native/recovery')[1]
        st2, al2 = A.export('alice', B, B.actors['bob']['account'], 1, 'after-twin'); out = self.push(A, B)
        ok = r['results'][0]['reason'] == 'conflicting_signed_history' and rec['quarantined_peers'] == ['dom-a'] and out['results'][0]['reason'] == 'peer_quarantined' and B.bal('carol', asset=A.asset) == carol0
        self.rec(15, self.j15.__doc__, ok, {'rejection': r['results'][0]['reason'], 'quarantined_peers': rec['quarantined_peers'], 'next_certificate': out['results'][0].get('reason'), 'carol_unchanged': B.bal('carol', asset=A.asset)}, caveat='equivocation is detected where two signed histories meet; it is not prevented (documented trust model)', t0=t0)
        self.ctx['j15_alloc'] = al2.get('id')
        # lift the quarantine for the remaining journeys: an explicit administrator act on B (recorded), the twin allocation stays quarantined
        with sqlite3.connect(B.inst.settings.db_path) as db:
            db.execute("UPDATE native_peers SET state='active' WHERE domain_id='dom-a'"); db.commit()

    def j16(self, t0):
        """Reordered, delayed and duplicated messages converge where the protocol promises convergence."""
        A, B = self.A, self.B; st, a1 = A.export('alice', B, B.actors['bob']['account'], 1, 'ord-1'); st, a2 = A.export('alice', B, B.actors['bob']['account'], 1, 'ord-2')
        ob = A.call('get', '/api/v1/native/transport/outbox', params={'receiver': 'dom-b'})[1]; items = ob['items']; rev = dict(ob, items=list(reversed(items)))
        r1 = B.call('post', '/api/v1/native/transport/inbox', json=rev)[1]; r2 = B.call('post', '/api/v1/native/transport/inbox', json=rev)[1]
        acks = r1['replies'] + r2['replies']; back = A.call('post', '/api/v1/native/transport/inbox', json={'items': list(reversed(acks)) + acks})[1]
        courier.mark_from_results(A.ep(), r1['results']); out = self.push(A, B)
        states = {x: A.alloc(x)['state'] for x in (a1['id'], a2['id'])}
        ok = all(s == 'acknowledged' for s in states.values()) and all(r['state'] == 'duplicate' for r in r2['results']) and B.bal('bob', asset=A.asset) >= 2 and A.audit()['conserved'] and B.audit(A.asset)['conserved']
        self.rec(16, self.j16.__doc__, ok, {'reversed_first': [r['state'] for r in r1['results']], 'reversed_again': [r['state'] for r in r2['results']], 'acks_back': [r['state'] for r in back['results']], 'final': states}, t0=t0)

    def j17(self, t0):
        """Reverse return follows the supported closure protocol; timeout alone cannot restore funds."""
        A, B = self.A, self.B; bob = B.actors['bob']; st, al = A.export('alice', B, bob['account'], 4, 'ret-full'); self.push(A, B); before = A.bal('alice')
        sr, rr = B.call('post', '/api/v1/native/allocations/' + al['id'] + '/return', role='bob', json={}); mid = A.bal('alice'); out = self.push(B, A)
        after_return = A.bal('alice')
        st2, al2 = A.export('alice', B, bob['account'], 2, 'ret-part'); self.push(A, B); B.call('post', '/api/v1/native/transfers', role='bob', json={'to_account': B.actors['carol']['account'], 'amount': 1, 'asset': A.asset, 'request_id': 'ret-spend'})
        sp, rp = B.call('post', '/api/v1/native/allocations/' + al2['id'] + '/return', role='bob', json={})
        ok = sr == 200 and rr['state'] == 'return_requested' and mid == before and out['ok'] and A.alloc(al['id'])['state'] == 'returned' and after_return == before + 4 and B.alloc(al['id'])['state'] == 'returned' and sp == 409 and (rp.get('detail') or {}).get('code') == 'return_partial_unsupported'
        self.rec(17, self.j17.__doc__, ok, {'request': rr.get('state'), 'source_before_closure': mid == before, 'after_closure': A.alloc(al['id'])['state'], 'alice_delta_after_closure': after_return - before, 'partial': (rp.get('detail') or {}).get('code')}, t0=t0)

    def j18(self, t0):
        """Global audit distinguishes backing from claims and proves conservation for the exercised sequence."""
        A, B = self.A, self.B; au = A.audit(); bu = B.audit(A.asset)
        held = au['total_held'] + bu['total_held']; pending_backing = au['backing_locked_for_exports'] - bu['claims_backed_elsewhere']
        ok = au['conserved'] and bu['conserved'] and held + pending_backing == au['fixture_funding'] and pending_backing >= 0 and au['negative_balances'] == [] and bu['negative_balances'] == []
        self.rec(18, self.j18.__doc__, ok, {'A': {k: au[k] for k in ('total_held', 'fixture_funding', 'backing_locked_for_exports', 'claims_backed_elsewhere', 'conserved')}, 'B': {k: bu[k] for k in ('total_held', 'fixture_funding', 'backing_locked_for_exports', 'claims_backed_elsewhere', 'conserved')}, 'held_both': held, 'pending_backing_not_yet_imported': pending_backing, 'trust': au.get('operator_trust')}, t0=t0)

    def j19(self, t0):
        """Old backup restoration cannot silently resume outward spending as if no external commitments existed."""
        A, B = self.A, self.B; db = A.inst.settings.db_path; backup = Path(A.inst.temp.name) / 'before.sqlite'
        src = sqlite3.connect(db); dst = sqlite3.connect(backup); src.backup(dst); dst.close(); src.close()
        st, al = A.export('alice', B, B.actors['bob']['account'], 3, 'pre-backup'); self.push(A, B); seq_before = A.domain()['commit_sequence']
        A.stop_workers(); A.api.terminate(); A.api.wait(timeout=20)
        for suffix in ('-wal', '-shm'):
            p = db.with_name(db.name + suffix)
            if p.exists():
                p.unlink()
        shutil.copyfile(backup, db); A.start_api(); A.start_worker('w-a-restored')
        try:
            d = A.domain(); s_exp = A.export('alice', B, B.actors['bob']['account'], 1, 'post-restore')[0]; s_tr = A.call('post', '/api/v1/native/transfers', role='alice', json={'to_account': A.actors['machine']['account'], 'amount': 1, 'request_id': 'post-restore'})[0]
            rec0 = A.call('post', '/api/v1/native/recovery/reconcile', json={})[1]
        finally:
            B.call('post', '/api/v1/native/transport/observation', json={'peer': 'dom-a'}); obs = self.push(B, A)
            rec1 = A.call('post', '/api/v1/native/recovery/reconcile', json={'reconstruct_from_observations': True, 'source_account': A.actors['alice']['account'], 'amounts': {al.get('id', 'none'): 3}})[1]
        ok = d['recovery_state'] == 'quarantine' and s_exp == 409 and s_tr == 409 and rec0['recovery_state'] == 'quarantine' and obs['ok'] and rec1['recovery_state'] == 'normal' and rec1['reconstructed'][0]['status'] == 'locked' and A.audit()['conserved'] and A.domain()['commit_sequence'] >= seq_before
        self.rec(19, self.j19.__doc__, ok, {'after_restore': d['recovery_state'], 'export': s_exp, 'transfer': s_tr, 'before_observation': rec0['recovery_state'], 'after': rec1['recovery_state'], 'reconstructed': rec1.get('reconstructed'), 'sequence': (seq_before, A.domain()['commit_sequence'])}, caveat='rollback is detected from the durable watermark file and the peer\'s signed observation; a restore that also restores the watermark is detected only through the peer', t0=t0)

    def j20(self, t0):
        """Local finality, pending inter-domain claim and reconciled settlement appear correctly in the API and the UI."""
        A, B = self.A, self.B; st, al = A.export('alice', B, B.actors['bob']['account'], 1, 'ui-pending'); api_pending = A.alloc(al['id'])
        sess = A.http.post('/console/login', data={'token': A.inst.tok['owner']}, follow_redirects=False); cookies = sess.cookies
        page1 = A.http.get('/console/native/settlement?allocation=' + al['id'], cookies=cookies).text
        self.push(A, B); api_done = A.alloc(al['id']); page2 = A.http.get('/console/native/settlement?allocation=' + al['id'], cookies=cookies).text
        st, ints = A.call('get', '/api/v1/native/intents'); settled = next((i for i in ints['items'] if i['state'] == 'settled'), None)
        ok = api_pending['finality'] == 'cross_domain_pending_claim' and 'certified' in page1 and 'irrevocable' in page1 and api_done['finality'] == 'reconciled_inter_domain_settlement' and 'acknowledged' in page2 and settled and settled['finality'] == 'domain_local_settlement' and 'Local finality' in page2
        self.rec(20, self.j20.__doc__, ok, {'api_pending': api_pending['finality'], 'api_reconciled': api_done['finality'], 'ui_pending_shown': 'certified' in page1, 'ui_reconciled_shown': 'acknowledged' in page2, 'local': settled['finality'] if settled else None}, t0=t0)

    # ---- work and resources (21–30) ------------------------------------------------------------------------------------------------
    def j21(self, t0):
        """The complete 80-unit example: real terrain work, native settlement, a real second purchase and a treasury award."""
        A = self.A; req = A.actor('r80', 80); m = A.actor('m80'); v = A.actor('v80'); s = A.actor('s80'); A.provider('m80'); A.provider('s80')
        pre = A.award('m80', 's80', 'determination', energy_inputs('FEASIBLE'), 9, bps=1112)[3][0]
        inputs = lunar_example('route'); inputs['private_label'] = 'J21'
        t, r, o, (sa, a) = A.award('r80', 'm80', 'lunar_route_feasibility', inputs, 40, bps=500, verifier=3, verifier_pay_to=v['account'])
        aw, d = A.deliver_accept(a['id'], 'r80'); paid = A.pay_all(a['id'], 'r80'); b1 = {k: A.bal(k) for k in ('r80', 'm80', 'v80')}; b1['treasury'] = A.bal(A.treasury)
        t2, r2, o2, (sa2, a2) = A.award('m80', 's80', 'determination', energy_inputs('FEASIBLE'), 9, bps=1112); aw2, d2 = A.deliver_accept(a2['id'], 'm80'); paid2 = A.pay_all(a2['id'], 'm80')
        tv = A.call('get', '/api/v1/work/treasury', params={'asset': A.asset})[1]
        t3, r3, o3, (sa3, a3) = A.award('owner', 'm80', 'determination', energy_inputs('INFEASIBLE'), 2, funding='treasury'); aw3, d3 = A.deliver_accept(a3['id'], 'owner'); paid3 = A.pay_all(a3['id'], 'owner')
        final = {k: A.bal(k) for k in ('r80', 'm80', 's80', 'v80')}; final['treasury'] = A.bal(A.treasury)
        ok = pre == 400 and sa == 201 and a['ceiling'] == 45 and d.get('decision') == 'accepted' and b1 == {'r80': 35, 'm80': 40, 'v80': 3, 'treasury': 2} and sa2 == 201 and d2.get('decision') == 'accepted' and tv['available'] == 3 and sa3 == 201 and d3.get('decision') == 'accepted' and final == {'r80': 35, 'm80': 32, 's80': 9, 'v80': 3, 'treasury': 1} and sum(final.values()) == 80
        self.rec(21, self.j21.__doc__, ok, {'purchase_before_earning': pre, 'after_step_2': b1, 'treasury_available_before_award': tv.get('available'), 'final': final, 'total': sum(final.values()), 'fees_additional_to_provider': True, 'terrain_outcome': aw['milestones'][0]['dimensions']['science']}, t0=t0)
        self.ctx['j21'] = {'award': a['id'], 'job': aw['milestones'][0]['attempts'][-1]['job_id'], 'machine': m['account'], 'supplier': s['account']}

    def j22(self, t0):
        """An unfunded or merely receivable-funded actor cannot buy; a settled actor can."""
        A = self.A; u = A.actor('unfunded'); A.provider('unfunded', kinds=('energy_audit',)); e = A.actor('earner'); A.provider('earner', kinds=('energy_audit',))
        s_unfunded = A.award('unfunded', 's80', 'determination', energy_inputs('FEASIBLE'), 2)[3][0]
        t, r, o, (sa, a) = A.award('r80', 'earner', 'determination', energy_inputs('FEASIBLE'), 6); aw, d = A.deliver_accept(a['id'], 'r80')
        recv = A.call('get', '/api/v1/native/accounts/me', role='earner')[1]
        s_recv = A.award('earner', 's80', 'determination', energy_inputs('FEASIBLE'), 2)[3][0]
        A.pay_all(a['id'], 'r80'); s_settled = A.award('earner', 's80', 'determination', energy_inputs('FEASIBLE'), 2)[3][0]
        ok = s_unfunded == 400 and d.get('decision') == 'accepted' and recv['earned_unsettled'] == 6 and recv['available'] == 0 and s_recv == 400 and s_settled == 201
        self.rec(22, self.j22.__doc__, ok, {'unfunded': s_unfunded, 'receivable_only': {'status': s_recv, 'earned_unsettled': recv['earned_unsettled'], 'available': recv['available']}, 'after_settlement': s_settled}, t0=t0)

    def j23(self, t0):
        """Terrain processing on a verified public-data input agrees with the independent bounded reference."""
        A = self.A; st, src = A.call('get', '/api/v1/lunar/sources'); tycho = lunar_sources.bundled('tycho_48'); dn_digest = hashlib.sha256(json.dumps(tycho['dn'], separators=(',', ':')).encode()).hexdigest()
        inputs = lunar_example('terrain', 'tycho_48'); inputs['private_label'] = 'J23'
        t, r, o, (sa, a) = A.award('r80', 'm80', 'lunar_terrain_tile', inputs, 2); aw, d = A.deliver_accept(a['id'], 'r80'); jid = aw['milestones'][0]['attempts'][-1]['job_id']
        pv = A.call('get', '/api/v1/lunar/products/' + jid)[1]; chk = lunar_ref.check_terrain(inputs, pv['result'])
        cli = subprocess.run([PY, '-m', 'metacoin_service.client_cli', '--credential-file', str(A.cred_file('owner')), '--base', A.base, 'lunar-download', jid, '--out', str(Path(A.inst.temp.name) / 'tile.json')], cwd=ROOT, env=A.env, capture_output=True, text=True, timeout=120)
        file_ok = cli.returncode == 0 and json.loads((Path(A.inst.temp.name) / 'tile.json').read_text())['result']['provenance']['image_sha256'] == lunar_sources.LDEM_4['image_sha256']
        ver = [v for v in pv['verifications'] if v['state'] == 'passed' and v['class'] == 'analytical']
        ok = sa == 201 and d.get('decision') == 'accepted' and chk['ok'] and ver and pv['provenance']['product_id'] == 'LDEM_4' and next(s for s in src['sources'] if s['name'] == 'tycho_48')['extract_sha256'] == dn_digest and file_ok
        self.rec(23, self.j23.__doc__, ok, {'reference_checks': [c['check'] for c in chk['checks']], 'verification': [v['class'] for v in ver], 'provenance': {k: pv['provenance'][k] for k in ('product_id', 'data_set_id', 'image_sha256', 'acquired_at')}, 'download_cli': cli.returncode}, caveat='LDEM_4 is a 7.58 km/pixel public shape grid: grid-scale slopes, not traversability', t0=t0)

    def j24(self, t0):
        """Missing data, units, orientation, projection scale, edges and near-threshold decisions are handled explicitly."""
        A = self.A; syn = lunar_sources.synthetic(seed=9, lines=16, samples=16); syn['nodata_dn'] = -32768; syn['dn'][5][5] = -32768
        inputs = lunar_example('terrain'); inputs['source'] = syn; inputs['private_label'] = 'J24'
        t, r, o, (sa, a) = A.award('r80', 'm80', 'lunar_terrain_tile', inputs, 1); aw, d = A.deliver_accept(a['id'], 'r80'); jid = aw['milestones'][0]['attempts'][-1]['job_id']; res = A.call('get', '/api/v1/lunar/products/' + jid)[1]['result']
        refusals = {}
        for name, bad in (('polar', dict(inputs, source=dict(syn, first_line_latitude_milli_deg=89800))), ('units', dict(inputs, units={'height': 'feet', 'slope': 'milli-degree', 'spacing': 'millimeter'})), ('orientation_projection', dict(inputs, source=dict(syn, projection='polar_stereographic')))):
            tt = A.terms('r80', 'lunar_terrain_tile', 1); st, f = A.call('post', '/api/v1/work/terms/' + tt['id'] + '/freeze', role='r80', json={'inputs': bad}); refusals[name] = st
        rr = lunar_example('route', 'tycho_48'); rr['rover']['max_slope_milli_deg'] = 5000; near = None
        for lim in (4000, 4500, 5000, 5500, 6000):
            rr['rover']['max_slope_milli_deg'] = lim; outc = __import__('metacoin_service.lunar.route', fromlist=['compute']).compute(rr)['outcome']
            if outc == 'INDETERMINATE':
                near = lim; break
        ok = sa == 201 and d.get('decision') == 'accepted' and res['outcome'] == 'TILE_PARTIAL' and res['stats']['nodata_cells'] >= 5 and res['flags'][5][5] == 2 and res['flags'][0][0] == 1 and all(v in (409, 422) for v in refusals.values()) and res['row_spacing_mm'][0][1] <= res['row_spacing_mm'][0][0]
        self.rec(24, self.j24.__doc__, ok, {'nodata_cells': res['stats']['nodata_cells'], 'edge_flag_corner': res['flags'][0][0], 'nodata_flag': res['flags'][5][5], 'refusals': refusals, 'row_spacing_ns_ew_mm': res['row_spacing_mm'][0], 'near_threshold_indeterminate_at_limit': near, 'uncertain_cells_reported': res['stats']['cells_uncertain_at_threshold']}, t0=t0)

    def j25(self, t0):
        """An honestly verified negative result earns the contracted payment; an unsupported positive result is refused."""
        A = self.A; inputs = lunar_example('route', 'tycho_48'); inputs['rover']['max_slope_milli_deg'] = 4000; inputs['private_label'] = 'J25'
        t, r, o, (sa, a) = A.award('r80', 'm80', 'lunar_route_feasibility', inputs, 3); aw, d = A.deliver_accept(a['id'], 'r80'); paid = A.pay_all(a['id'], 'r80')
        neg_ok = aw['milestones'][0]['dimensions']['science'] == 'INFEASIBLE' and d.get('decision') == 'accepted' and paid.get('provider', {}).get('state') == 'settled'
        # an unsupported positive: the verification is forced to fail (test hook) -> acceptance refused, nothing payable
        inputs2 = lunar_example('route'); inputs2['private_label'] = 'J25B'; t2, r2, o2, (sa2, a2) = A.award('r80', 'm80', 'lunar_route_feasibility', inputs2, 3); v2 = A.wait_execution(a2['id'], 'r80'); jid2 = v2['milestones'][0]['attempts'][-1]['job_id']
        A.call('post', '/api/v1/ops/faults', json={'fault': 'verification_fail', 'job_id': jid2}); st, vr = A.call('post', '/api/v1/work/awards/%s/milestones/m1/verify' % a2['id'], role='r80', json={})
        deadline = time.time() + 120; ev = {}
        while time.time() < deadline:
            st, ev = A.call('post', '/api/v1/work/awards/%s/milestones/m1/evaluate' % a2['id'], role='r80', json={})
            if ev.get('decision_candidate') in ('accepted', 'rejected'):
                break
            time.sleep(0.5)
        sd, dd = A.call('post', '/api/v1/work/awards/%s/milestones/m1/decide' % a2['id'], role='r80', json={'decision': 'accept'})
        ok = neg_ok and ev.get('decision_candidate') == 'rejected' and sd == 409 and A.bal('m80') == 32 + 3
        self.rec(25, self.j25.__doc__, ok, {'negative': {'science': aw['milestones'][0]['dimensions']['science'], 'decision': d.get('decision'), 'paid': paid.get('provider', {}).get('state')}, 'unsupported_positive': {'candidate': ev.get('decision_candidate'), 'accept_attempt': sd}, 'machine': A.bal('m80')}, t0=t0)

    def j26(self, t0):
        """A cheap offer that violates energy reserve, deadline, permission or contact policy is ineligible."""
        A = self.A; tnow = int(time.time()); env = {'energy_budget_mwh': 1000, 'energy_reserve_mwh': 400, 'memory_bytes': 1 << 30, 'runtime_seconds': 600, 'thermal_permission': 'nominal', 'storage_bytes': 1 << 20, 'max_exposure': 12, 'asset': A.asset, 'contact_windows': [{'id': 'pass-1', 'start': tnow + 100, 'end': tnow + 200000, 'capacity_bytes': 1 << 20}], 'deadline': {'at': 'delivery', 'time': tnow + 300000}}
        inputs = lunar_example('route'); inputs['private_label'] = 'J26'
        t = A.terms('r80', 'lunar_route_feasibility', 10, extra={'resource_envelope': env}); A.call('post', '/api/v1/work/terms/' + t['id'] + '/freeze', role='r80', json={'inputs': inputs}); st, r = A.call('post', '/api/v1/work/requests', role='r80', json={'terms_id': t['id']}); A.call('post', '/api/v1/work/requests/' + r['id'] + '/open', role='r80', json={})
        cheap = A.call('post', '/api/v1/work/requests/' + r['id'] + '/offers', role='s80', json={'price_amount': 3, 'asset': A.asset, 'scheme': 'exact', 'window_seconds': 3600, 'verification': {'class': 'analytical'}, 'resource_claims': {'energy_mwh': 700, 'energy_basis': 'estimated_tdp_runtime', 'thermal_class': 'peak', 'delivery_contact': 'pass-1', 'runtime_seconds': 500}})[1]
        good = A.call('post', '/api/v1/work/requests/' + r['id'] + '/offers', role='m80', json={'price_amount': 9, 'asset': A.asset, 'scheme': 'exact', 'window_seconds': 3600, 'verification': {'class': 'analytical'}, 'resource_claims': {'energy_mwh': 500, 'energy_basis': 'measured', 'thermal_class': 'nominal', 'delivery_contact': 'pass-1', 'runtime_seconds': 500}})[1]
        cmp = A.call('get', '/api/v1/work/requests/' + r['id'] + '/compare', role='r80')[1]; sa, a = A.call('post', '/api/v1/work/requests/' + r['id'] + '/award', role='r80', json={})
        codes = [x['code'] for x in cheap['eligibility']['reasons']]
        ok = cheap['state'] == 'excluded' and 'resource_violation:energy_reserve_violation' in codes and 'resource_violation:thermal_permission_missing' in codes and good['state'] == 'offered' and [x['offer_id'] for x in cmp['eligible']] == [good['id']] and sa == 201 and a['provider_id'] == good['provider_id']
        self.rec(26, self.j26.__doc__, ok, {'cheap_excluded': codes, 'eligible': [x['offer_id'] for x in cmp['eligible']], 'awarded_provider': a.get('provider_id')}, t0=t0); self.ctx['j26_award'] = a.get('id')
        A.deliver_accept(a['id'], 'r80'); A.pay_all(a['id'], 'r80')

    def j27(self, t0):
        """No feasible plan produces an informative no-spend outcome."""
        A = self.A; tnow = int(time.time()); env = {'energy_budget_mwh': 500, 'energy_reserve_mwh': 400, 'max_exposure': 5, 'asset': A.asset, 'contact_windows': [{'id': 'w', 'start': tnow, 'end': tnow + 100, 'capacity_bytes': 1000}], 'deadline': {'at': 'delivery', 'time': tnow + 50}}
        st, res = A.call('post', '/api/v1/native/procurement/plan', json={'envelope': env, 'plans': [{'id': 'local', 'kind': 'local_compute', 'energy_mwh': 300, 'price': 0}, {'id': 'remote', 'kind': 'remote_compute', 'energy_mwh': 10, 'price': 30, 'delivery_contact': 'w', 'runtime_seconds': 1000, 'storage_bytes': 10}, {'id': 'wait', 'kind': 'deferred', 'ready_at': tnow + 5000, 'price': 1}, {'id': 'decline', 'kind': 'refusal'}], 'policy': 'cheapest_eligible', 'now': tnow})
        before = A.audit()['total_held']; bench = A.call('get', '/api/v1/native/procurement/benchmark', params={'missions': 150})[1]
        ok = st == 200 and res['outcome'] == 'NO_FEASIBLE_PLAN' and res['spend'] == 0 and res['selected'] is None and len([c for c in res['candidates'] if c['violations']]) == 3 and A.audit()['total_held'] == before and bench['results']['cheapest_eligible']['resource_violations'] == 0 and bench['results']['fifo_ignoring_envelope']['resource_violations'] > 0
        self.rec(27, self.j27.__doc__, ok, {'outcome': res.get('outcome'), 'violations': {c['plan']['id']: [v['code'] for v in c['violations']] for c in res.get('candidates', [])}, 'benchmark': {k: {kk: v[kk] for kk in ('accepted_products', 'total_spend', 'resource_violations', 'pending_exposure')} for k, v in bench['results'].items()}}, t0=t0)

    def j28(self, t0):
        """A decision-changing acquisition is purchased and updates the decision; a redundant acquisition is declined."""
        A = self.A; inputs = lunar_example('route'); inputs['private_label'] = 'J28'; cm = lunar_evidence.candidate_model(inputs, lunar_evidence.DEFAULT_STATES)
        st, an = A.call('post', '/api/v1/decisions/analyze', role='m80', json={'inputs': cm, 'title': 'route observability'}); jid = an['job_id']
        deadline = time.time() + 120
        while time.time() < deadline and A.call('get', '/api/v1/decisions/' + jid, role='m80')[1]['state'] not in ('succeeded', 'failed'):
            time.sleep(0.5)
        dv = A.call('get', '/api/v1/decisions/' + jid, role='m80')[1]; exp = int(time.time()) + 86400; sup = A.actors['s80']['provider_id']
        red = A.call('post', '/api/v1/acquisition-plans', role='m80', json={'analysis_job_id': jid, 'menu': [lunar_evidence.menu(exp, sup)[2]], 'budget': 10})[1]; s_red = A.call('post', '/api/v1/acquisition-plans/' + red['id'] + '/materialize', role='m80', json={})[0]
        plan = A.call('post', '/api/v1/acquisition-plans', role='m80', json={'analysis_job_id': jid, 'menu': lunar_evidence.menu(exp, sup), 'budget': 10})[1]
        sm, m = A.call('post', '/api/v1/acquisition-plans/' + plan['id'] + '/materialize', role='m80', json={'asset': A.asset})
        so, o = A.call('post', '/api/v1/work/requests/' + m['request']['id'] + '/offers', role='s80', json={'price_amount': plan['cost'], 'asset': A.asset, 'scheme': 'exact', 'window_seconds': 3600, 'verification': {'class': 'analytical'}})
        sa, aw = A.call('post', '/api/v1/work/requests/' + m['request']['id'] + '/award', role='m80', json={'offer_id': o['id']}); before_m = A.bal('m80')
        obs = {v: (['14500'] if v == 'slope_calibration_pass' else ['0']) for v in plan['selected_views']}
        sd, d = A.call('post', '/api/v1/work/awards/%s/milestones/m1/deliver' % aw['id'], role='s80', json={'delivery': {'schema': 'observation-delivery/v1', 'status': 'acquired', 'provenance': {'instrument': 'finer-vertical-error terrain pass (simulated)', 'acquired_at': 1760000000, 'units_confirmed': True, 'simulated': True}, 'observations': obs}})
        awv, dec = A.deliver_accept(aw['id'], 'm80'); after = A.call('get', '/api/v1/decisions/' + awv['milestones'][0]['job_id'], role='m80')[1]; paid = A.pay_all(aw['id'], 'm80')
        ok = dv['projection']['conclusion'] == 'AMBIGUOUS' and red['state'] == 'nothing_to_acquire' and s_red == 409 and sm == 201 and sa == 201 and sd == 201 and after['projection']['conclusion'] == 'DECISION_RESOLVED' and after['result']['decision']['determined_label'] == 'ROUTE_INFEASIBLE' and dec.get('decision') == 'accepted' and paid['provider']['rail'] == 'native' and after['evidence_root'] != dv['evidence_root']
        self.rec(28, self.j28.__doc__, ok, {'before': dv['projection']['conclusion'], 'redundant': {'state': red['state'], 'materialize': s_red}, 'selected': plan['selected_views'], 'cost': plan['cost'], 'after': after['projection']['conclusion'], 'label': after['result']['decision']['determined_label'] if after.get('result') else None, 'paid_rail': paid.get('provider', {}).get('rail')}, caveat='declared finite states; a model-level decision, not physical truth', t0=t0)

    def j29(self, t0):
        """Compatible paid artifacts can be reused; stale or incompatible ones are rejected."""
        A = self.A; inputs = lunar_example('terrain'); inputs['private_label'] = 'J29'
        t, r, o, (sa, a) = A.award('r80', 'm80', 'lunar_terrain_tile', inputs, 1); aw, d = A.deliver_accept(a['id'], 'r80'); A.pay_all(a['id'], 'r80'); jid = aw['milestones'][0]['attempts'][-1]['job_id']
        st, c = A.call('post', '/api/v1/contracts', json={'kind': 'terrain_tile', 'title': 'reuse', 'inputs': inputs, 'policy': {'reviewer_id': A.inst.ids['reviewer'], 'accepted_outcomes': ['FEASIBLE', 'INFEASIBLE', 'INDETERMINATE'], 'disclose_outcome': True, 'expires_in_seconds': 3600, 'retention_seconds': 3600, 'execution_locations': ['*']}})
        reused = None; incompatible = None
        if st == 201:
            A.call('post', '/api/v1/contracts/' + c['id'] + '/freeze', json={}); sj, j = A.call('post', '/api/v1/jobs', json={'contract_id': c['id'], 'reuse': True}); reused = j.get('reused_from') or (A.call('get', '/api/v1/jobs/' + j['id'])[1].get('reused_from') if sj in (201, 202) else None)
            bad = dict(inputs, parameters=dict(inputs['parameters'], vertical_error_mm=2000)); st2, c2 = A.call('post', '/api/v1/contracts', json={'kind': 'terrain_tile', 'title': 'reuse-incompatible', 'inputs': bad, 'policy': {'reviewer_id': A.inst.ids['reviewer'], 'accepted_outcomes': ['FEASIBLE', 'INFEASIBLE', 'INDETERMINATE'], 'disclose_outcome': True, 'expires_in_seconds': 3600, 'retention_seconds': 3600, 'execution_locations': ['*']}})
            A.call('post', '/api/v1/contracts/' + c2['id'] + '/freeze', json={}); sj2, j2 = A.call('post', '/api/v1/jobs', json={'contract_id': c2['id'], 'reuse': True}); incompatible = j2.get('reused_from') if sj2 in (201, 202) else 'submit:%d' % sj2
        ok = reused == jid and not incompatible
        self.rec(29, self.j29.__doc__, ok, {'original_job': jid, 'reused_from': reused, 'incompatible_reused_from': incompatible, 'contract_status': st}, caveat='reuse is keyed by kind, canonical inputs and verifier digest; a changed parameter is a different key', t0=t0)

    def j30(self, t0):
        """Partial delivery and later changed conditions obey the frozen payment terms."""
        A = self.A; t, r, o, (sa, a) = A.award('r80', 'm80', 'diagnostic_delivery', energy_inputs('INDETERMINATE'), 6, scheme='upto'); aw, d = A.deliver_accept(a['id'], 'r80'); paid = A.pay_all(a['id'], 'r80')
        s_am, am = A.call('post', '/api/v1/work/terms/' + t['id'] + '/amend', role='r80', json={'terms': {'payment': dict(t['terms']['payment'], ceiling=1)}})
        aw2 = A.call('get', '/api/v1/work/awards/' + a['id'], role='r80')[1]
        ok = sa == 201 and d.get('payment_class') == 'diagnostic' and d.get('payable_amount') == 3 and paid['provider']['final_amount'] == 3 and paid['provider']['max_amount'] == 6 and aw2['state'] == 'closed' and aw2['terms_digest'] == a['terms_digest'] and s_am in (201, 409, 422)
        self.rec(30, self.j30.__doc__, ok, {'decision': d.get('payment_class'), 'payable': d.get('payable_amount'), 'settled': paid['provider']['final_amount'], 'authorized_max': paid['provider']['max_amount'], 'amend_after_close': s_am, 'award_terms_unchanged': aw2['terms_digest'] == a['terms_digest']}, t0=t0)

    # ---- treasury, security and delivery (31–40) -----------------------------------------------------------------------------------
    def j31(self, t0):
        """Treasury cannot award against pending receipts, duplicate fees or unrelated assets."""
        A = self.A; A.fund('r80', 50); tv = A.call('get', '/api/v1/work/treasury', params={'asset': A.asset})[1]
        t = A.terms('owner', 'determination', tv['available'] + 5, funding='treasury'); A.call('post', '/api/v1/work/terms/' + t['id'] + '/freeze', json={'inputs': energy_inputs('FEASIBLE')}); s_over, r_over = A.call('post', '/api/v1/work/treasury/allocate', json={'terms_id': t['id']})
        t2 = A.terms('owner', 'determination', 1, funding='treasury'); A.call('post', '/api/v1/work/terms/' + t2['id'] + '/freeze', json={'inputs': energy_inputs('FEASIBLE')}); s1 = A.call('post', '/api/v1/work/treasury/allocate', json={'terms_id': t2['id']})[0]; s_dup = A.call('post', '/api/v1/work/treasury/allocate', json={'terms_id': t2['id']})[0]
        # a pending (accepted, unpaid) fee is not revenue: award a fee-bearing task, accept, do not pay, then check availability unchanged
        tb = tv['available'] - 1
        t3, r3, o3, (sa3, a3) = A.award('r80', 'm80', 'determination', energy_inputs('FEASIBLE'), 10, bps=1000); aw3, d3 = A.deliver_accept(a3['id'], 'r80'); tv2 = A.call('get', '/api/v1/work/treasury', params={'asset': A.asset})[1]
        other = A.call('get', '/api/v1/work/treasury', params={'asset': 'action-units'})[1]
        n_alloc = sum(1 for x in A.call('get', '/api/v1/work/treasury', params={'asset': A.asset})[1]['allocations'] if x['terms_id'] == t2['id'])
        ok = s_over == 400 and 'insufficient' in (r_over.get('detail') or {}).get('code', '') and s1 == 201 and s_dup in (400, 409) and n_alloc == 1 and tv2['available'] == tb and other['available'] == 0 and other['asset'] == 'action-units'
        self.rec(31, self.j31.__doc__, ok, {'over_allocation': s_over, 'first': s1, 'duplicate': s_dup, 'allocations_for_terms': n_alloc, 'available_with_pending_fee': tv2['available'], 'expected': tb, 'unrelated_asset_available': other['available']}, t0=t0)
        A.pay_all(a3['id'], 'r80')

    def j32(self, t0):
        """Fees and awards preserve conservation and the objective/subjective issuance separation."""
        A = self.A; au = A.audit(); rep = A.call('post', '/api/v1/work/journal/replay', json={})[1]; spec = A.call('get', '/api/v1/native/spec')[1]
        ids = {f: hashlib.sha256((ROOT / f).read_bytes()).hexdigest() for f in ('WHITEPAPER.md', 'TOKENOMICS.md', 'protocol/ledger_anchor.json', 'protocol/identity_text.py') if (ROOT / f).exists()}
        conf = subprocess.run([PY, '-m', 'unittest', 'metacoin_service.tests.test_native_conformance'], cwd=ROOT, env=A.env, capture_output=True, text=True, timeout=600)
        ok = au['conserved'] and rep['consistent'] and conf.returncode == 0 and 'launched currency' in json.dumps(spec['trust_model'])
        self.rec(32, self.j32.__doc__, ok, {'conserved': au['conserved'], 'fixture_funding': au['fixture_funding'], 'held': au['total_held'], 'journal_consistent': rep['consistent'], 'replay_differences': rep.get('differences'), 'invariants_failed': [c for c in rep.get('invariants', []) if not c.get('ok')], 'conformance_suite_rc': conf.returncode, 'identity_digests': ids}, t0=t0)

    def j33(self, t0):
        """Unauthorized disputes, substituted signatures and changed acceptance policies cannot alter unrelated obligations."""
        A = self.A; st, obs = A.call('get', '/api/v1/native/obligations'); ob = next(o for o in obs['items'] if o['state'] == 'settled' and o['kind'] == 'provider' and o['payee_account'] == A.actors['m80']['account'])
        stranger = A.actor('stranger', 2); s_str = A.call('post', '/api/v1/native/disputes', role='stranger', json={'obligation_id': ob['id'], 'kind': 'delivery_mismatch', 'claim': 'x'})[0]
        before = {k: A.bal(k) for k in ('m80', 's80', 'r80', 'stranger')}
        sd, d = A.call('post', '/api/v1/native/disputes', role='r80', json={'obligation_id': ob['id'], 'kind': 'delivery_mismatch', 'claim': 'digest differs'})
        # a substituted signature on a transport item is rejected; a changed acceptance policy after award leaves the award's frozen digest
        st2, al = A.export('alice', self.B, self.B.actors['bob']['account'], 1, 'j33'); ob2 = A.call('get', '/api/v1/native/transport/outbox', params={'receiver': 'dom-b'})[1]; t = json.loads(json.dumps(ob2)); t['items'][0]['payload_signature'] = 'ab' * 32; sub = self.B.call('post', '/api/v1/native/transport/inbox', json=t)[1]
        res = A.call('post', '/api/v1/native/disputes/' + d['id'] + '/resolve', role='reviewer', json={'outcome': 'dismiss', 'reason': 'digest matches'})[1]
        after = {k: A.bal(k) for k in ('m80', 's80', 'r80', 'stranger')}
        ok = s_str == 403 and sd == 201 and d['held_amount'] <= ob['amount'] and sub['results'][0]['reason'] == 'bad_signature' and res['state'] == 'resolved' and after == before and all(after[k] == before[k] for k in ('s80', 'r80', 'stranger'))
        self.rec(33, self.j33.__doc__, ok, {'stranger_dispute': s_str, 'party_dispute': sd, 'hold_bounded': d.get('held_amount'), 'substituted_signature': sub['results'][0]['reason'], 'balances_unchanged_after_dismiss': after == before}, t0=t0)
        self.push(A, self.B)

    def j34(self, t0):
        """Rotation/revocation handles delayed valid messages under the declared policy."""
        A, B = self.A, self.B; st, old = A.export('alice', B, B.actors['bob']['account'], 1, 'pre-rot'); rot = A.call('post', '/api/v1/native/domain/rotate', json={'grace_seconds': 3600})[1]; st, new = A.export('alice', B, B.actors['bob']['account'], 1, 'post-rot')
        out = self.push(A, B); peer = B.call('get', '/api/v1/native/peers/dom-a')[1]
        ok = rot['authority']['epoch'] >= 2 and all(r['state'] == 'accepted' for r in out['results']) and peer['epoch'] == rot['authority']['epoch'] and A.alloc(old['id'])['state'] == 'acknowledged' and A.alloc(new['id'])['state'] == 'acknowledged' and A.alloc(new['id'])['certificate']['source_epoch'] == rot['authority']['epoch']
        self.rec(34, self.j34.__doc__, ok, {'epoch': rot['authority']['epoch'], 'deliveries': [r['state'] for r in out['results']], 'peer_epoch': peer['epoch'], 'old_key_grace_seconds': 3600, 'keys': [(k['epoch'], k['state']) for k in peer['keys']]}, caveat='an offline counterparty learns a rotation only when the notice arrives; the retiring key stays valid for messages created before retired_at + grace', t0=t0)

    def j35(self, t0):
        """Offline bundle import enforces size and trust boundaries and does not execute arbitrary content."""
        B = self.B; big = {'items': [{'envelope': {'id': 'x'}} for _ in range(201)]}; s_many = B.call('post', '/api/v1/native/transport/inbox', json=big)[0]
        huge = {'items': [{'envelope': {'id': 'y', 'blob': 'a' * (1100 * 1024)}}]}; s_huge = B.call('post', '/api/v1/native/transport/inbox', json=huge)[0]
        weird = B.call('post', '/api/v1/native/transport/inbox', json={'items': ['__import__("os")', {'envelope': {'id': 'z', 'sender': 'dom-a', 'receiver': 'dom-b', 'message_type': 'rotation', 'protocol': 'metacoin-native/v0', 'type': 'envelope', 'environment': 'dev', 'causality': None, 'payload_digest': '0' * 64, 'expires_at': 4102444800, 'created_at': 1}, 'envelope_signature': 'aa', 'key_id': 'k', 'payload': {'new_public_hex': 'ff' * 32}, 'payload_signature': 'aa'}]})[1]
        peer = B.call('get', '/api/v1/native/peers/dom-a')[1]
        ok = s_many == 422 and s_huge in (413, 422) and all(r['state'] == 'rejected' for r in weird['results']) and 'ff' * 32 not in json.dumps(peer)
        self.rec(35, self.j35.__doc__, ok, {'too_many_items': s_many, 'too_large': s_huge, 'rejections': [r.get('reason') for r in weird['results']], 'roots_unchanged': 'ff' * 32 not in json.dumps(peer)}, t0=t0)

    def j36(self, t0):
        """The independent checker accepts supported evidence and rejects all targeted tampering vectors."""
        A, B = self.A, self.B; roots = {d.id: [{'key_id': k['key_id'], 'public_hex': k['public_hex'], 'epoch': k['epoch'], 'retired_at': k.get('retired_at'), 'grace_until': k.get('grace_until')} for k in d.call('get', '/api/v1/native/export')[1]['self_description_keys'] if k['domain_id'] == d.id] for d in (A, B)}
        reps = {}
        for d in (A, B):
            exp = d.call('get', '/api/v1/native/export')[1]; rep = native_checker.check(exp, roots); reps[d.id] = rep['summary']
            path = Path(d.inst.temp.name) / 'export.json'; path.write_text(json.dumps(exp)); rp = Path(d.inst.temp.name) / 'roots.json'; rp.write_text(json.dumps(roots))
            cli = subprocess.run([PY, '-m', 'metacoin_service.native.checker', str(path), '--roots', str(rp)], cwd=ROOT, env=A.env, capture_output=True, text=True, timeout=300); reps[d.id + '_cli_rc'] = cli.returncode
        wrong = native_checker.check(A.call('get', '/api/v1/native/export')[1], {k: [dict(v[0], public_hex='00' * 32)] for k, v in roots.items()})
        vr = native_vectors.run(); model = native_model.explore(5)
        ok = all(reps[d]['authorized_bookkeeping'] and reps[d]['cryptographic_authenticity'] and reps[d]['economic_backing'] for d in ('dom-a', 'dom-b')) and reps['dom-a_cli_rc'] == 0 and not wrong['summary']['cryptographic_authenticity'] and wrong['summary']['authorized_bookkeeping'] and vr['all_as_expected'] and model['violation_count'] == 0
        self.rec(36, self.j36.__doc__, ok, {'reports': reps, 'wrong_root_separates_properties': {'authenticity': wrong['summary']['cryptographic_authenticity'], 'bookkeeping': wrong['summary']['authorized_bookkeeping']}, 'vectors': {'count': len(vr['results']), 'all_as_expected': vr['all_as_expected']}, 'model': {'states': model['reachable_states'], 'violations': model['violation_count'], 'bounds': model['bounds']}}, t0=t0)

    def j37(self, t0):
        """API, CLI, console and MCP show consistent balances and enforce the same authority."""
        A = self.A; acct = A.actors['m80']['account']; api = A.call('get', '/api/v1/native/accounts/' + acct, role='m80')[1]
        cli = subprocess.run([PY, '-m', 'metacoin_service.client_cli', '--credential-file', str(A.cred_file('m80')), '--base', A.base, 'native-account', acct], cwd=ROOT, env=A.env, capture_output=True, text=True, timeout=120); cli_out = json.loads(cli.stdout) if cli.returncode == 0 else {}
        sess = A.http.post('/console/login', data={'token': A.actors['m80']['token']}, follow_redirects=False); page = A.http.get('/console/native/accounts/' + acct, cookies=sess.cookies).text
        mcp = subprocess.run([PY, '-m', 'metacoin_service.tests.mcp_journey_client', A.base, str(A.cred_file('m80')), 'native'], cwd=ROOT, env=dict(A.env, MCP_NATIVE_TO=A.actors['s80']['account'], MCP_NATIVE_AMOUNT='1'), capture_output=True, text=True, timeout=300)
        m = json.loads(mcp.stdout) if mcp.returncode == 0 else {}
        vcli = subprocess.run([PY, '-m', 'metacoin_service.client_cli', '--credential-file', str(A.cred_file('viewer')), '--base', A.base, 'native-transfer', acct, '1', '--request-id', 'viewer-cli'], cwd=ROOT, env=A.env, capture_output=True, text=True, timeout=120)
        ok = cli.returncode == 0 and cli_out.get('available') == api['available'] and ('<strong>%d</strong>' % api['available']) in page and m.get('account', {}).get('available') == api['available'] and m.get('account_after_simulate', {}).get('available') == api['available'] and m.get('transfer', {}).get('state') == 'settled' and m.get('transfer_retry', {}).get('replayed') and m.get('account_after', {}).get('available') == api['available'] - 1 and vcli.returncode != 0
        self.rec(37, self.j37.__doc__, ok, {'api': api['available'], 'cli': cli_out.get('available'), 'console_shows': ('<strong>%d</strong>' % api['available']) in page, 'mcp': {'tools': m.get('native_tools'), 'before': m.get('account', {}).get('available'), 'after_simulate': m.get('account_after_simulate', {}).get('available'), 'after_transfer': m.get('account_after', {}).get('available'), 'retry_replayed': m.get('transfer_retry', {}).get('replayed')}, 'viewer_cli_transfer_rc': vcli.returncode, 'mcp_stderr': mcp.stderr[-200:] if mcp.returncode else None}, t0=t0)

    def j38(self, t0):
        """A real browser completes the native mission / account / treasury journey."""
        A = self.A; pw = os.environ.get('METACOIN_PLAYWRIGHT_PYTHON'); shots = self.ctx.get('shots')
        if not pw or not shots:
            self.rec(38, self.j38.__doc__, False, {}, blocked='METACOIN_PLAYWRIGHT_PYTHON and --shots required', t0=t0); return
        j = self.ctx['j21']; p = subprocess.run([pw, str(ROOT / 'metacoin_service' / 'tests' / 'browser' / 'journey_native.py'), A.base, str(A.cred_file('owner')), str(A.cred_file('m80')), str(A.cred_file('viewer')), j['machine'], j['supplier'], j['award'], j['job'], shots], cwd=ROOT, env=dict(A.env, PLAYWRIGHT_BROWSERS_PATH=os.environ.get('PLAYWRIGHT_BROWSERS_PATH', '')), capture_output=True, text=True, timeout=600)
        try:
            out = json.loads(p.stdout.strip().splitlines()[-1])
        except Exception:
            out = {'passed': 0, 'failed': 1, 'checks': [], 'stderr': p.stderr[-500:], 'stdout': p.stdout[-500:]}
        self.rec(38, self.j38.__doc__, p.returncode == 0 and out.get('failed') == 0, {'passed': out.get('passed'), 'failed': out.get('failed'), 'checks': out.get('checks'), 'screenshots': out.get('screenshots'), 'crash': out.get('crash')}, t0=t0)

    def j39(self, t0):
        """A clean export reproduces native operation without x402, remote facilitators, private local files or hidden data."""
        res = os.environ.get('METACOIN_NATIVE_CLEAN_EXPORT_JSON')
        if not res or not Path(res).exists():
            self.rec(39, self.j39.__doc__, False, {}, blocked='run by work/native-space-protocol-session/clean_export.sh after packaging; result file not supplied', t0=t0); return
        r = json.loads(Path(res).read_text()); ok = r.get('failed') == 0 and r.get('passed', 0) >= 5
        self.rec(39, self.j39.__doc__, ok, {'from_clean_export': {k: r.get(k) for k in ('passed', 'failed', 'blocked', 'total', 'revision')}, 'archive_sha256': r.get('archive_sha256')}, t0=t0)

    def j40(self, t0):
        """Migration from the preserved previous database and safe recovery are verified on an isolated copy."""
        live = Path(os.environ.get('METACOIN_LIVE_HOME', os.path.expanduser('~/.local/state/metacoin-service')))
        if not (live / 'service.sqlite').exists():
            self.rec(40, self.j40.__doc__, False, {}, blocked='preserved previous database not found at ' + str(live), t0=t0); return
        import tempfile
        iso = Path(tempfile.mkdtemp(prefix='metacoin-iso-')); home = iso / 'home'; home.mkdir(mode=0o700)
        src = sqlite3.connect('file:%s?mode=ro' % (live / 'service.sqlite'), uri=True); before = {'schema': src.execute('SELECT name FROM schema_migrations ORDER BY applied_at DESC, rowid DESC LIMIT 1').fetchone()[0], 'jobs': src.execute('SELECT COUNT(*) FROM jobs').fetchone()[0], 'artifacts': src.execute('SELECT COUNT(*) FROM artifacts').fetchone()[0], 'receipts': src.execute("SELECT COUNT(*) FROM work_receipts").fetchone()[0] if src.execute("SELECT 1 FROM sqlite_master WHERE name='work_receipts'").fetchone() else None, 'native_tables_before': src.execute("SELECT COUNT(*) FROM sqlite_master WHERE name LIKE 'native_%'").fetchone()[0]}
        dst = sqlite3.connect(home / 'service.sqlite'); src.backup(dst); dst.close(); src.close(); os.chmod(home / 'service.sqlite', 0o600)
        for sub in ('keys', 'credentials', 'artifacts'):
            if (live / sub).exists():
                shutil.copytree(live / sub, home / sub, dirs_exist_ok=True); os.chmod(home / sub, 0o700)
                for f in (home / sub).rglob('*'):
                    if f.is_file():
                        os.chmod(f, 0o600)
        env = dict(ENV, METACOIN_X402_DISABLED='1', METACOIN_LIMITS_JSON=json.dumps({'test_hooks': 1}))
        mig = subprocess.run([PY, '-m', 'metacoin_service', '--home', str(home), '--provider-mode', 'simulation', 'migrate'], cwd=ROOT, env=env, capture_output=True, text=True, timeout=300)
        if mig.returncode != 0:
            self.rec(40, self.j40.__doc__, False, {'before': before, 'migrate_rc': mig.returncode, 'stderr': mig.stderr[-600:], 'stdout': mig.stdout[-300:]}, t0=t0); shutil.rmtree(iso, ignore_errors=True); return
        after_db = sqlite3.connect(home / 'service.sqlite'); after = {'schema': after_db.execute('SELECT name FROM schema_migrations ORDER BY applied_at DESC, rowid DESC LIMIT 1').fetchone()[0], 'jobs': after_db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0], 'artifacts': after_db.execute('SELECT COUNT(*) FROM artifacts').fetchone()[0], 'native_domains': after_db.execute('SELECT COUNT(*) FROM native_domains').fetchone()[0]}; after_db.close()
        port = free_port(); base = 'http://127.0.0.1:%d' % port; api = subprocess.Popen([PY, '-m', 'metacoin_service', '--home', str(home), '--provider-mode', 'simulation', 'serve', '--port', str(port)], cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        smoke = {}
        try:
            for _ in range(300):
                try:
                    if httpx.get(base + '/api/health', timeout=1).status_code == 200:
                        break
                except Exception:
                    time.sleep(0.1)
            creds = json.load(open(home / 'credentials' / 'bootstrap.json')); tok = creds['principals']['owner']['token']; h = {'Authorization': 'Bearer ' + tok}; c = httpx.Client(base_url=base, timeout=60)
            smoke['health'] = c.get('/api/health').json(); jobs = c.get('/api/v1/jobs?limit=5', headers=h).json(); items = jobs.get('items', jobs) if isinstance(jobs, dict) else jobs
            smoke['old_jobs_readable'] = isinstance(items, list) and len(items) > 0 and c.get('/api/v1/jobs/' + items[0]['id'], headers=h).status_code == 200
            smoke['domain'] = c.post('/api/v1/native/domain', headers=h, json={'domain_id': 'dom-migrated'}).status_code
            act = c.post('/api/v1/native/actors', headers=h, json={'name': 'mig-actor'}).json(); smoke['fund'] = c.post('/api/v1/native/fixtures/fund', headers=h, json={'account_id': act['account_id'], 'amount': 7}).status_code
            act2 = c.post('/api/v1/native/actors', headers=h, json={'name': 'mig-actor-2'}).json(); smoke['transfer'] = c.post('/api/v1/native/transfers', headers={'Authorization': 'Bearer ' + act['credential']['token']}, json={'to_account': act2['account_id'], 'amount': 3, 'request_id': 'mig-1'}).status_code
            smoke['audit'] = c.get('/api/v1/native/audit', headers=h).json().get('conserved'); smoke['replay'] = c.post('/api/v1/work/journal/replay', headers=h, json={}).json().get('consistent')
            smoke['recovery'] = c.get('/api/v1/native/recovery', headers=h).json().get('recovery_state')
        finally:
            api.terminate(); api.wait(timeout=20); shutil.rmtree(iso, ignore_errors=True)
        ok = mig.returncode == 0 and after['schema'] == '043_native_authority_keys' and after['jobs'] == before['jobs'] and after['artifacts'] == before['artifacts'] and smoke.get('old_jobs_readable') and smoke.get('domain') == 201 and smoke.get('fund') == 201 and smoke.get('transfer') == 201 and smoke.get('audit') and smoke.get('replay') and smoke.get('recovery') == 'normal'
        self.rec(40, self.j40.__doc__, ok, {'before': before, 'after': after, 'migrate_rc': mig.returncode, 'smoke': smoke, 'live_service_untouched': True}, caveat='isolated copy only; the live service was neither upgraded nor restarted', t0=t0)

    def run_all(self, only=None):
        for n in range(1, 41):
            if only and n not in only:
                continue
            fn = getattr(self, 'j%d' % n); self.guard(n, fn.__doc__ or 'journey %d' % n, fn)
        return self.results


def main():
    p = argparse.ArgumentParser(); p.add_argument('--out'); p.add_argument('--only'); p.add_argument('--shots'); a = p.parse_args()
    j = Journeys(); j.ctx['shots'] = a.shots
    try:
        results = j.run_all([int(x) for x in a.only.split(',')] if a.only else None); health = j.A.call('get', '/api/health')[1]
    finally:
        j.close()
    out = {'schema': 'metacoin-journeys-native/v1', 'provider_mode': 'simulation', 'x402_disabled': True, 'facilitator_url': 'http://127.0.0.1:1/blocked', 'revision': health.get('revision'), 'python': sys.version.split()[0], 'started_at': j.started_at, 'finished_at': now(), 'results': results,
           'passed': sum(r['status'] == 'passed' for r in results), 'failed': sum(r['status'] in ('failed', 'error') for r in results), 'blocked': sum(r['status'] == 'blocked' for r in results), 'total': len(results)}
    text = json.dumps(out, indent=1, default=str)
    if a.out:
        Path(a.out).write_text(text)
    print(text[-1500:])
    return 0 if out['failed'] == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
