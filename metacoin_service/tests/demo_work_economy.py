"""Central scientific work-economy demonstration (Order 08 §66), runnable through normal application entry points against a
disposable instance (test-http mode, private local chain). Synthetic inputs and accounts only; nothing anchored, no real funds.

Sequence: a bounded mission question with two related determination instances (one FEASIBLE, one INFEASIBLE under the declared
exact model) → private requests → two eligible local offers each → selection by the visible policy → budget reservation →
execution through the provider transport (worker processes) → receipts → separate-process verification → acceptance of the
valid positive AND the valid negative → agreed synthetic payments on the local chain, reconciled. Then the adversarial half:
a fabricated positive (another contract's artifact), a solver timeout presented as infeasible, a duplicate claim with a new
salt — each refused or left unresolved without duplicate payment — and a dispute against a deliberately mistaken rejection
resolved from the preserved replay evidence. Finally a fee from an observed local payment funds a bounded treasury award and
the journal conserves every amount while base issuance stays untouched.

    PYTHONPATH=. .venv-service/bin/python -m metacoin_service.tests.demo_work_economy OUT_DIR"""
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from metacoin_service.tests.journeys_economy import Journeys, energy_inputs, PY, ROOT

FEE_BPS = 1000   # 10 % of every settled provider payment goes to the treasury address


def main(out_dir):
    out = Path(out_dir); out.mkdir(parents=True, exist_ok=True)
    j = Journeys(); rec = {'schema': 'metacoin-work-demonstration/v1', 'started_at': int(time.time()), 'steps': []}
    def step(name, ok, **facts):
        rec['steps'].append({'step': name, 'ok': bool(ok), **facts}); print(('PASS ' if ok else 'FAIL ') + name + ('' if ok else ' :: ' + json.dumps(facts, default=str)[:600]), flush=True)
    try:
        j.start_api(); rec['revision'] = j.http.get('/api/health').json().get('revision')
        if not j.acct:
            rec['blocked'] = 'local chain unavailable'; return 2
        from metacoin_service.economy import legacy_bridge, missions as missions_mod
        base = [ROOT / p for p in ('README.md', 'WHITEPAPER.md', 'TOKENOMICS.md', 'protocol/ledger_anchor.json') if (ROOT / p).exists()] + [legacy_bridge.ledger_source(), missions_mod.verdict_source()]
        digest0 = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in base}
        supply0 = j.call('get', '/api/v1/work/rails')[1]['local-chain-token']
        payer0 = j.bal('requester_payer'); tre0 = j.bal('treasury'); prov0 = {k: j.bal(k) for k in ('provider_a', 'provider_b')}
        # ---- 1. two related determination instances, two eligible offers each, visible selection, reservation --------------------------
        awards = {}
        for label, outcome in (('positive', 'FEASIBLE'), ('negative', 'INFEASIBLE')):
            t = j.terms('determination', ceiling=12, amount=10, asset='local-chain-token', title='Mission question: is the %s energy scenario feasible under the declared exact model?' % label)
            pay = dict(t['terms']['payment'], fee_policy={'schema': 'metacoin-fee-policy/v1', 'treasury_bps': FEE_BPS, 'rounding': 'floor_fee_remainder_to_provider'})
            st, t2 = j.call('post', '/api/v1/work/terms/' + t['id'], json={'terms': {'payment': pay}}); assert st == 200, t2
            f = j.freeze(t['id'], energy_inputs(outcome, 'DEMO_' + label.upper())); r = j.request(t['id'])
            oa = j.offer('alpha', r['id'], price=10, asset='local-chain-token'); ob = j.offer('beta', r['id'], price=11, asset='local-chain-token')
            cmp = j.call('get', '/api/v1/work/requests/%s/compare' % r['id'])[1]
            st, a = j.award(r['id']); assert st == 201, a
            awards[label] = {'terms': t['id'], 'digest': f['digest'], 'request': r['id'], 'offers': [oa['id'], ob['id']], 'award': a['id'], 'selection': cmp['policy']['policy'], 'ranked': [o['offer_id'] for o in cmp['eligible']], 'excluded': cmp.get('excluded'), 'reserved': a['reserved']}
            step('%s: two eligible offers, lowest eligible price selected under the visible policy, budget reserved' % label, len(cmp['eligible']) >= 2 and a['offer_id'] == oa['id'] and a['reserved'] == 11, **awards[label])   # reserved = price + declared fee
        # ---- 2. execution through the provider transport, receipts, separate-process verification, acceptance of both ------------------
        j.run_workers(2, 'demo-exec')
        for label in ('positive', 'negative'):
            aid = awards[label]['award']; v = j.verify(aid); ev = j.evaluate(aid); st, d = j.decide(aid)
            recs = j.call('get', '/api/v1/work/awards/' + aid + '/receipts')[1]['items']
            awards[label].update({'science': ev['science'], 'candidate': ev['decision_candidate'], 'decision': d.get('decision'), 'payable': d.get('payable_amount'), 'entitlement': (d.get('entitlement') or {}).get('id'), 'verification': v['id'], 'receipts': [(x['kind'], x['id']) for x in recs]})
            want = 'FEASIBLE' if label == 'positive' else 'INFEASIBLE'
            step('%s: executed by the provider, verified in a separate worker process, accepted with the same complete amount' % label, ev['science'] == want and d.get('decision') == 'accepted' and d.get('payable_amount') == 10 and any(x['kind'] == 'verification' for x in recs), science=ev['science'], decision=d.get('decision'), payable=d.get('payable_amount'))
        # ---- 3. agreed synthetic payments observed on the local chain and reconciled ----------------------------------------------------
        for label in ('positive', 'negative'):
            st, s = j.pay(awards[label]['entitlement']); rc = j.call('post', '/api/v1/work/intents/' + s['id'] + '/reconcile', json={})[1]
            awards[label]['payment'] = {k: s.get(k) for k in ('id', 'state', 'final_amount', 'transaction', 'rail')}; awards[label]['reconciliation'] = rc.get('reconciliation')
            step('%s: exact payment observed on the private local chain and reconciled' % label, s['state'] == 'settled' and s['final_amount'] == 10 and rc['state'] == 'settled', **awards[label]['payment'])
        for label in ('positive', 'negative'):
            fees = [e for e in j.aview(awards[label]['award'])['entitlements'] if e['kind'] == 'fee']
            st, fs = j.pay(fees[0]['id']); awards[label]['fee_payment'] = {k: fs.get(k) for k in ('id', 'state', 'final_amount')}
            step('%s: the declared 10 %% fee is a separate entitlement paid by the requester to the treasury address and observed on the rail' % label, len(fees) == 1 and fs['state'] == 'settled' and fs['final_amount'] == 1, **awards[label]['fee_payment'])
        prov_after = j.bal('provider_a')
        step('provider received both agreed payments in full; the treasury address received the two fees; the payer funded both', prov_after - prov0['provider_a'] == 20 and j.bal('treasury') - tre0 == 2 and j.bal('requester_payer') == payer0 - 22, provider_delta=prov_after - prov0['provider_a'], treasury_delta=j.bal('treasury') - tre0, payer_delta=j.bal('requester_payer') - payer0)
        # ---- 4. adversarial: fabricated positive, mislabeled timeout, duplicate claim --------------------------------------------------
        neg = awards['negative']['award']; pos = awards['positive']['award']
        other_job = j.aview(pos)['milestones'][0]['job_id']
        fab = j.call('post', '/api/v1/work/terms/' + awards['negative']['terms'] + '/evaluate', json={'job_id': other_job})[1]
        step('fabricated positive: another contract\'s FEASIBLE artifact presented against the negative contract is rejected (contract digest / input root binding)', fab['decision_candidate'] == 'rejected' and any(x['type'] == 'source_revision' and x['result'] == 'failed' for x in fab['trace']), candidate=fab['decision_candidate'], failed=[x['predicate'] for x in fab['trace'] if x['result'] == 'failed'])
        t, f, r, o, a3 = j.flow('INFEASIBLE', 'DEMO_TIMEOUT', asset='local-chain-token', price=10, scheme='exact')
        for k in range(2):
            subprocess.run([PY, '-m', 'metacoin_service', '--home', str(j.inst.home), '--provider-mode', 'test-http', 'worker', '--once', '--name', 'demo-slow-%d' % k], cwd=ROOT, env=dict(j.env, METACOIN_TEST_EXEC_DELAY_SECONDS='12'), capture_output=True, text=True, timeout=120)
        ev3 = j.evaluate(a3['id']); st, d3 = j.decide(a3['id'], decision='reject')
        step('solver timeout presented as infeasible: execution failed, science no_valid_evidence, nothing payable', ev3['execution'] == 'failed' and ev3['science'] == 'no_valid_evidence' and d3.get('payable_amount') == 0, execution=ev3['execution'], science=ev3['science'], payable=d3.get('payable_amount'))
        st2, dup = j.decide(neg); st3, dup2 = j.decide(neg, idem='demo-dup-salt')
        ents = j.call('get', '/api/v1/work/entitlements/' + awards['negative']['entitlement'])[1]
        step('duplicate claim with a new request salt: refused; one entitlement, already paid, no second payment identity', st2 == 409 and st3 == 409 and ents['state'] == 'paid', statuses=[st2, st3], entitlement_state=ents['state'])
        # ---- 5. dispute against a deliberately mistaken rejection, resolved from the preserved replay evidence -------------------------
        t, f, r, o, a4 = j.flow('FEASIBLE', 'DEMO_DISPUTE', asset='local-chain-token', price=10); j.run_workers(2, 'demo-dispute'); v4 = j.verify(a4['id'])
        st, d4 = j.decide(a4['id'], decision='reject', reason='deliberately mistaken rejection for the demonstration')
        st, dp = j.call('post', '/api/v1/work/awards/%s/milestones/m1/dispute' % a4['id'], h=j.pv['alpha']['h'], json={'claim': 'the passed replay was ignored'})
        j.call('post', '/api/v1/work/disputes/' + dp['id'] + '/evidence', h=j.pv['alpha']['h'], json={'verification_id': v4['id']})
        dec = j.call('post', '/api/v1/work/disputes/' + dp['id'] + '/decide', role='reviewer', json={'outcome': 'supersede_acceptance', 'reason': 'replay passed on the identical commitment'})[1]
        hist = j.call('get', '/api/v1/work/awards/%s/milestones/m1/decisions' % a4['id'])[1]['items']
        step('dispute against a mistaken rejection: superseding acceptance from preserved replay evidence; the original decision retained', dec['decision']['outcome'] == 'supersede_acceptance' and [(h['decision'], h['current']) for h in hist] == [('rejected', False), ('accepted', True)], dispute=dp['id'], decisions=[(h['decision'], h['authority'], h['current']) for h in hist])
        # ---- 6. a fee from an observed payment funds a bounded treasury award; conservation; no base-supply change ---------------------
        tv = j.call('get', '/api/v1/work/treasury')[1]
        t5 = j.terms(ceiling=1, amount=1, asset='local-chain-token', title='Treasury-funded recheck: is the negative scenario still infeasible?'); pay5 = dict(t5['terms']['payment'], funding='treasury')
        j.call('post', '/api/v1/work/terms/' + t5['id'], json={'terms': {'payment': pay5}}); j.freeze(t5['id'], energy_inputs('INFEASIBLE', 'DEMO_TREASURY'))
        al = j.call('post', '/api/v1/work/treasury/allocate', json={'terms_id': t5['id']})[1]
        r5 = j.request(t5['id']); o5 = j.offer('beta', r5['id'], price=1, asset='local-chain-token'); st, a5 = j.award(r5['id'], o5['id']); assert st == 201, ('treasury award', st, a5, al); j.run_workers(2, 'demo-treasury'); j.verify(a5['id']); st, d5 = j.decide(a5['id'])
        st, s5 = j.pay(d5['entitlement']['id']); tv2 = j.call('get', '/api/v1/work/treasury')[1]
        step('fee-backed treasury award: confirmed fee revenue funds a bounded award that pays an accepted negative from the treasury account', tv['confirmed_revenue'] >= 2 and s5['payer_authority'] == 'treasury' and d5['evaluation']['science'] == 'INFEASIBLE' and tv2['settled_spending'] == 1, revenue_before=tv['confirmed_revenue'], settled_spending=tv2['settled_spending'], available_after=tv2['available'])
        rep = j.call('post', '/api/v1/work/journal/replay', json={})[1]
        digest1 = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in base}
        step('journal replay consistent, invariants hold, identity/protocol files and base issuance untouched', rep['consistent'] and all(c['ok'] for c in rep['invariants']) and digest0 == digest1, scopes=list(rep['scopes']), invariants_failed=[c['check'] for c in rep['invariants'] if not c['ok']], differences=rep.get('differences'), protocol_files_unchanged=digest0 == digest1)
        # ---- 7. artifacts: complete bundle of the negative contract, restricted bundle, projections, offline verifier -------------------
        recs = j.call('get', '/api/v1/work/awards/' + neg + '/receipts')[1]['items']; pub = recs[0]['public_key_hex']
        (out / 'contract-bundle-negative-full.zip').write_bytes(j.http.get('/api/v1/work/awards/%s/milestones/m1/bundle?scope=full' % neg, headers=j.H()).content)
        (out / 'contract-bundle-negative-restricted.zip').write_bytes(j.http.get('/api/v1/work/awards/%s/milestones/m1/bundle?scope=restricted' % neg, headers=j.H()).content)
        for aud in ('private', 'collaborator', 'public_ready'):
            (out / ('projection-negative-%s.json' % aud)).write_text(json.dumps(j.call('post', '/api/v1/work/awards/%s/milestones/m1/projection' % neg, json={'audience': aud, 'sign': aud == 'public_ready'})[1], indent=1))
        env = {'PATH': os.environ.get('PATH', ''), 'HOME': os.environ.get('HOME', '/'), 'PYTHONPATH': str(ROOT)}
        for name in ('full', 'restricted'):
            p = subprocess.run([sys.executable, '-m', 'metacoin_service.economy.verify_work', 'contract-bundle-negative-%s.zip' % name, '--trust-root', pub, '--json'], cwd=out, env=env, capture_output=True, text=True, timeout=180)
            (out / ('verifier-report-%s.json' % name)).write_text(p.stdout or json.dumps({'stderr': p.stderr[-800:]}))
            r_ = json.loads(p.stdout) if p.stdout else {}
            step('offline verifier on the %s bundle: %s' % (name, 'replays the INFEASIBLE determination' if name == 'full' else 'reports the missing private evidence honestly'), (r_.get('integrity') or {}).get('ok') and (r_.get('signer_trust') or {}).get('trusted') and ((r_.get('scientific_replay') or {}).get('scientific_outcome') == 'INFEASIBLE' if name == 'full' else bool(r_.get('missing_private_evidence'))), verdict=r_.get('verdict'))
        rec['awards'] = awards; rec['signing_public_key'] = pub; rec['console'] = ['/console/work', '/console/work/awards/' + neg, '/console/work/budget', '/console/work/reconciliation', '/console/work/disputes/' + dp['id']]
        rec['what_this_shows'] = ['a verified INFEASIBLE determination is accepted and paid exactly like the verified FEASIBLE one under the frozen outcome-neutral policy',
                                  'fabricated positives, mislabeled timeouts and duplicate claims are refused or left unresolved without a second payment',
                                  'a mistaken rejection is corrected through an authorized dispute using preserved replay evidence, never by editing the original decision',
                                  'fees observed on the local chain fund a bounded treasury award; the journal conserves every amount; base issuance and protocol files are untouched']
        rec['what_this_does_not_show'] = ['price discovery or independent organizations (one operator plays every role on one host)', 'public settlement (private in-memory py-evm chain, synthetic accounts)', 'anonymity of any party']
    finally:
        j.close()
    rec['finished_at'] = int(time.time()); rec['passed'] = sum(s['ok'] for s in rec['steps']); rec['failed'] = sum(not s['ok'] for s in rec['steps'])
    (out / 'demonstration.json').write_text(json.dumps(rec, indent=1, default=str))
    print(json.dumps({'passed': rec['passed'], 'failed': rec['failed']}))
    return 0 if rec['failed'] == 0 else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1]))
