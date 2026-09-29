"""Group C (Order 08 §27–§36): receipts with separate claims, acceptance decisions and entitlements (duplicate-claim
prevention), verifier assignment with honest independence, conflicting verifications, disputes with append-only
corrections and resolver authority, reassignment, delegation, and the portable offline verifier from a clean process."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from metacoin_service.tests.test_work_terms import TermsInstance, energy_inputs
from metacoin_service.economy import legacy_bridge

ROOT = Path(__file__).resolve().parents[2]


class EvidenceBase(unittest.TestCase):
    def setUp(self):
        self.inst = TermsInstance(); self.addCleanup(self.inst.close); self.c = self.inst.client; self.H = self.inst.h('owner'); self.R = self.inst.h('reviewer')
        self.w = self.inst.worker(); self.addCleanup(self.w.offline)
        self.assertEqual(self.c.put('/api/v1/budgets/workspace', headers=self.H, json={'ceiling': 1000}).status_code, 200)
        self.pv = {}
        for name in ('alpha', 'beta'):
            p = self.c.post('/api/v1/work/providers', headers=self.H, json={'name': name, 'capabilities': {'kinds': ['energy_audit', 'legacy_task_replay'], 'verification_classes': ['full_exact'], 'payment_schemes': ['exact', 'upto']}, 'pay_to': 'provider:' + name}).json()
            self.pv[name] = {'id': p['id'], 'h': {'Authorization': 'Bearer ' + p['credential']['token']}, 'principal_id': p['principal_id']}

    def awarded(self, outcome='FEASIBLE', template='determination', provider='alpha', price=10, terms_body=None, inputs=None, milestone_inputs=None, distinct=False, **kw):
        body = terms_body or dict({'template': template, 'ceiling': 10}, **kw)
        t = self.c.post('/api/v1/work/terms', headers=self.H, json=body); self.assertEqual(t.status_code, 201, t.text); t = t.json()
        fb = {'inputs': inputs if inputs is not None else energy_inputs(outcome)}
        if milestone_inputs:
            fb['milestone_inputs'] = milestone_inputs
        f = self.c.post('/api/v1/work/terms/' + t['id'] + '/freeze', headers=self.H, json=fb); self.assertEqual(f.status_code, 200, f.text)
        r = self.c.post('/api/v1/work/requests', headers=self.H, json={'terms_id': t['id']}).json(); self.c.post('/api/v1/work/requests/' + r['id'] + '/open', headers=self.H, json={})
        o = self.c.post('/api/v1/work/requests/' + r['id'] + '/offers', headers=self.pv[provider]['h'], json={'price_amount': price, 'asset': 'action-units', 'scheme': 'exact', 'window_seconds': 3600, 'verification': {'class': 'full_exact', 'distinct_verifier': distinct}}); self.assertEqual(o.status_code, 201, o.text)
        a = self.c.post('/api/v1/work/requests/' + r['id'] + '/award', headers=self.H, json={'offer_id': o.json()['id']}); self.assertEqual(a.status_code, 201, a.text)
        return t, f.json(), r, o.json(), a.json()

    def run_worker(self, n=3):
        for _ in range(n):
            self.w.run_once()

    def verify_ms(self, aid, key='m1', h=None, cls=None):
        r = self.c.post('/api/v1/work/awards/%s/milestones/%s/verify' % (aid, key), headers=h or self.H, json={'class': cls} if cls else {}); self.assertEqual(r.status_code, 202, r.text)
        self.run_worker(2); return r.json()

    def evaluate(self, aid, key='m1', h=None):
        r = self.c.post('/api/v1/work/awards/%s/milestones/%s/evaluate' % (aid, key), headers=h or self.H, json={}); self.assertEqual(r.status_code, 200, r.text); return r.json()

    def decide(self, aid, key='m1', decision='accept', reason=None, h=None, expect=200):
        r = self.c.post('/api/v1/work/awards/%s/milestones/%s/decide' % (aid, key), headers=h or self.H, json={'decision': decision, 'reason': reason}); self.assertEqual(r.status_code, expect, r.text); return r.json()


class WorkEvidenceTests(EvidenceBase):
    # J8 + J9 + J10 --------------------------------------------------------------------------------------------------
    def test_feasible_and_infeasible_accepted_and_paid_equally_crash_refused(self):
        ents = {}
        for want in ('FEASIBLE', 'INFEASIBLE'):
            t, f, r, o, a = self.awarded(want)
            self.run_worker()
            ev = self.evaluate(a['id']); self.assertEqual((ev['decision_candidate'], ev['science']), ('pending', want))
            self.decide(a['id'], expect=409)                                          # cannot accept a pending candidate
            self.verify_ms(a['id'])
            d = self.decide(a['id'])
            self.assertEqual((d['decision'], d['payment_class'], d['payable_amount'], d['entitlement']['state'], d['entitlement']['amount']), ('accepted', 'complete', 10, 'payable', 10))
            ents[want] = d['entitlement']['id']
            receipts = self.c.get('/api/v1/work/awards/' + a['id'] + '/receipts', headers=self.H).json()['items']
            self.assertEqual(sorted(x['kind'] for x in receipts), ['acceptance', 'provider', 'verification'])
            prov = next(x for x in receipts if x['kind'] == 'provider')
            self.assertEqual(prov['statement']['claims']['outputs']['outcome_disclosed'], want); self.assertFalse(prov['statement']['claims']['resources']['energy']['available'])
            self.assertIn('service-custodied', prov['signer_custody'])
            vr = next(x for x in receipts if x['kind'] == 'verification'); self.assertEqual(vr['statement']['claims']['independence']['process'], False); self.assertIn('same operator', vr['statement']['claims']['independence']['label'])
            chk = self.c.post('/api/v1/work/receipts/' + prov['id'] + '/verify', headers=self.H, json={}).json(); self.assertTrue(chk['valid_signature'] and chk['links_valid'])
            bad = self.c.post('/api/v1/work/receipts/' + prov['id'] + '/verify', headers=self.H, json={'trust_root': 'ab' * 32}).json(); self.assertFalse(bad['valid_signature'])
            aw = self.c.get('/api/v1/work/awards/' + a['id'], headers=self.H).json()
            self.assertEqual(aw['milestones'][0]['dimensions'], {'execution': 'completed', 'science': want, 'acceptance': 'accepted', 'payment': 'payable'}); self.assertEqual(aw['state'], 'delivered')
        e1 = self.c.get('/api/v1/work/entitlements/' + ents['FEASIBLE'], headers=self.H).json(); e2 = self.c.get('/api/v1/work/entitlements/' + ents['INFEASIBLE'], headers=self.H).json()
        self.assertEqual(e1['amount'], e2['amount'])
        # crash traceback as a negative: execution failed, science no_valid_evidence, rejected, nothing payable, record preserved
        os.environ['METACOIN_TEST_EXEC_DELAY_SECONDS'] = '5'; self.inst.settings.limits['job_timeout_seconds'] = 1; self.inst.settings.limits['job_max_retries'] = 0
        try:
            t, f, r, o, a = self.awarded('INFEASIBLE')
            self.run_worker(2)
        finally:
            del os.environ['METACOIN_TEST_EXEC_DELAY_SECONDS']
        ev = self.evaluate(a['id']); self.assertEqual((ev['decision_candidate'], ev['execution'], ev['science']), ('rejected', 'failed', 'no_valid_evidence'))
        d = self.decide(a['id'], decision='reject'); self.assertEqual((d['decision'], d['payable_amount'], d['entitlement']), ('rejected', 0, None))
        aw = self.c.get('/api/v1/work/awards/' + a['id'], headers=self.H).json(); self.assertEqual(aw['milestones'][0]['attempts'][0]['state'], 'failed'); self.assertEqual(aw['milestones'][0]['dimensions']['payment'], 'reserved')

    # J11 + J15 ------------------------------------------------------------------------------------------------------------
    def test_legacy_replay_through_the_bridge_equals_protocol_and_duplicate_claims_are_refused(self):
        reg = legacy_bridge.registry()['task-0018']
        t, f, r, o, a = self.awarded(template='independent_replay', registered_hash=reg['registered_hash'], ceiling=3, price=3, inputs={'schema': legacy_bridge.INPUT_SCHEMA, 'task_id': 'task-0018'})
        self.run_worker()
        # the protocol path (module compute + output_hash) and the bridge give the identical canonical hash; historic artifacts untouched
        import importlib
        mod = importlib.import_module(reg['module']); direct = mod.output_hash(mod.compute())
        job = self.c.get('/api/v1/jobs/' + self.c.get('/api/v1/work/awards/' + a['id'], headers=self.H).json()['milestones'][0]['job_id'], headers=self.H).json()
        self.assertEqual((job['summary']['output_hash'], job['summary']['registered_hash'], job['outcome']), (direct, reg['registered_hash'], 'EXACT_MATCH'))
        self.assertEqual(legacy_bridge.registry()['task-0018']['source_sha256'], reg['source_sha256'])
        d = self.decide(a['id']); self.assertEqual((d['decision'], d['payable_amount']), ('accepted', 3)); eid = d['entitlement']['id']
        # duplicate claims: a second decision on an accepted milestone is refused; re-deciding cannot create a second entitlement;
        # a provider "re-delivery" (new receipt request) returns the same entitlement identity
        self.decide(a['id'], expect=409)
        self.assertEqual(len(self.c.get('/api/v1/work/awards/%s/milestones/m1/decisions' % a['id'], headers=self.H).json()['items']), 1)
        with self.inst.app.state.services.db.tx() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM work_entitlements WHERE award_id=?', (a['id'],)).fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM work_receipts WHERE award_id=? AND kind=?', (a['id'], 'provider')).fetchone()[0], 1)
        again = self.c.get('/api/v1/work/awards/' + a['id'] + '/receipts', headers=self.pv['alpha']['h']).json()['items']
        self.assertEqual(sum(x['kind'] == 'provider' for x in again), 1)
        aw2 = self.c.get('/api/v1/work/awards/' + a['id'], headers=self.pv['alpha']['h']).json(); self.assertEqual(aw2['milestones'][0]['entitlement_id'], eid)
        # a numerical witness template is a separate class and cannot be advertised as a legacy exact task (J12, terms level)
        tw = self.c.post('/api/v1/work/terms', headers=self.H, json={'template': 'infeasibility_witness'}).json()
        insp = self.c.get('/api/v1/work/terms/' + tw['id'] + '/inspect', headers=self.H).json()
        self.assertEqual(insp['deliverables'][0]['type'], 'numerical_witness'); self.assertNotIn('legacy', insp['deliverables'][0]['claim']); self.assertIn('global optimality', insp['deliverables'][0]['claim'])
        bad = dict(tw['terms']); bad['deliverables'] = [{'key': 'x', 'type': 'exact_task_output', 'required': True}]
        v = self.c.post('/api/v1/work/terms/validate', headers=self.H, json={'terms': bad}).json(); self.assertFalse(v['valid']); self.assertEqual(v['refusal']['detail']['code'], 'deliverable_type_kind_mismatch')

    # J13 + J14 + J20: bundles and the offline verifier from a clean directory --------------------------------------------------
    def test_bundles_verified_offline_transplant_rejected_restricted_scope_reported(self):
        t, f, r, o, a = self.awarded('INFEASIBLE'); self.run_worker(); self.verify_ms(a['id']); self.decide(a['id'])
        pub = self.c.get('/api/v1/work/awards/' + a['id'] + '/receipts', headers=self.H).json()['items'][0]['public_key_hex']
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup); clean = Path(tmp.name)
        for scope in ('full', 'restricted'):
            z = self.c.get('/api/v1/work/awards/%s/milestones/m1/bundle?scope=%s' % (a['id'], scope), headers=self.H); self.assertEqual(z.status_code, 200, z.text)
            (clean / (scope + '.zip')).write_bytes(z.content)
        env = {'PATH': os.environ.get('PATH', ''), 'HOME': os.environ.get('HOME', '/'), 'PYTHONPATH': str(ROOT)}
        full = subprocess.run([sys.executable, '-m', 'metacoin_service.economy.verify_work', 'full.zip', '--trust-root', pub, '--json'], cwd=clean, env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(full.returncode, 0, full.stderr[-800:]); rep = json.loads(full.stdout)
        self.assertTrue(rep['parsing']['ok'] and rep['integrity']['ok'] and rep['signer_trust']['trusted'])
        self.assertEqual((rep['scientific_replay']['performed'], rep['scientific_replay']['scientific_outcome'], rep['scientific_replay']['matches_manifest_root']), (True, 'INFEASIBLE', True))
        self.assertEqual(rep['acceptance_evaluation']['offline_candidate'], 'accepted'); self.assertEqual(rep['missing_private_evidence'], [])
        # restricted: structure and signatures valid, scientific scope limited, private evidence named as missing
        rest = subprocess.run([sys.executable, '-m', 'metacoin_service.economy.verify_work', 'restricted.zip', '--trust-root', pub, '--json'], cwd=clean, env=env, capture_output=True, text=True, timeout=120)
        rep2 = json.loads(rest.stdout)
        self.assertTrue(rep2['integrity']['ok'] and rep2['signer_trust']['trusted']); self.assertFalse(rep2['scientific_replay']['performed'])
        self.assertTrue(rep2['scientific_replay']['disclosed_fields']['membership_verified'] and rep2['scientific_replay']['disclosed_fields']['bindings_match_terms'])
        self.assertIn('inputs (commitment only)', rep2['missing_private_evidence']); self.assertIn('limited', rep2['verdict'])
        # key substitution: a different trust root fails the manifest signature and names the substitution
        sub = subprocess.run([sys.executable, '-m', 'metacoin_service.economy.verify_work', 'restricted.zip', '--trust-root', 'cd' * 32, '--json'], cwd=clean, env=env, capture_output=True, text=True, timeout=120)
        rep3 = json.loads(sub.stdout); self.assertFalse(rep3['signer_trust']['ok']); self.assertIn('key_substitution', [c['check'] for c in rep3['signer_trust']['checks']])
        # no trust root: consistent but untrusted
        nt = json.loads(subprocess.run([sys.executable, '-m', 'metacoin_service.economy.verify_work', 'restricted.zip', '--json'], cwd=clean, env=env, capture_output=True, text=True, timeout=120).stdout)
        self.assertFalse(nt['signer_trust']['trusted']); self.assertIn('untrusted', nt['verdict'])
        # malformed archive and unsupported schema
        (clean / 'bad.zip').write_bytes(b'not a zip'); m = json.loads(subprocess.run([sys.executable, '-m', 'metacoin_service.economy.verify_work', 'bad.zip', '--json'], cwd=clean, env=env, capture_output=True, text=True).stdout)
        self.assertFalse(m['parsing']['ok'])
        import zipfile
        with zipfile.ZipFile(clean / 'restricted.zip') as zin, zipfile.ZipFile(clean / 'schema.zip', 'w') as zout:
            for i in zin.infolist():
                data = zin.read(i)
                if i.filename == 'manifest.json':
                    d = json.loads(data); d['schema'] = 'metacoin-work-bundle/v9'
                    from experiments.private_receipts import receipt as merkle
                    data = merkle.canonical(d)
                zout.writestr(i, data)
        u = json.loads(subprocess.run([sys.executable, '-m', 'metacoin_service.economy.verify_work', 'schema.zip', '--json'], cwd=clean, env=env, capture_output=True, text=True).stdout)
        self.assertIn('unsupported schema', u['verdict'])
        # J14 transplant: disclosed evidence of ANOTHER award inside this bundle fails the binding check offline
        t2, f2, r2, o2, a2 = self.awarded('FEASIBLE'); self.run_worker(); self.verify_ms(a2['id']); self.decide(a2['id'])
        z2 = self.c.get('/api/v1/work/awards/%s/milestones/m1/bundle?scope=restricted' % a2['id'], headers=self.H).content
        with zipfile.ZipFile(clean / 'restricted.zip') as z1, zipfile.ZipFile(clean / 'transplant.zip', 'w') as zout:
            other = zipfile.ZipFile(__import__('io').BytesIO(z2))
            for i in z1.infolist():
                zout.writestr(i, other.read('evidence/disclosed.json') if i.filename == 'evidence/disclosed.json' else z1.read(i))
        tr = json.loads(subprocess.run([sys.executable, '-m', 'metacoin_service.economy.verify_work', 'transplant.zip', '--trust-root', pub, '--json'], cwd=clean, env=env, capture_output=True, text=True).stdout)
        self.assertFalse(tr['integrity']['ok'])                                                    # the digest manifest catches the swap first
        # server-side transplant: evaluating a milestone against another award's job fails source_revision
        ev = self.c.post('/api/v1/work/terms/' + t['id'] + '/evaluate', headers=self.H, json={'job_id': self.c.get('/api/v1/work/awards/' + a2['id'], headers=self.H).json()['milestones'][0]['job_id']}).json()
        self.assertEqual(ev['decision_candidate'], 'rejected'); self.assertIn('source', [x['predicate'] for x in ev['trace'] if x['result'] == 'failed'])

    # J17 + J18 + J19: conflicting verifications, disputes, resolver authority ------------------------------------------------
    def test_conflicting_verifications_dispute_supersedes_mistaken_rejection_unauthorized_resolver_refused(self):
        t, f, r, o, a = self.awarded('FEASIBLE'); self.run_worker()
        v1 = self.verify_ms(a['id']); self.assertEqual(self.c.get('/api/v1/verification/' + v1['id'], headers=self.H).json()['state'], 'passed')
        # a second verification forced to fail by the test hook: both records preserved, neither overwritten
        self.inst.settings.limits['test_hooks'] = 1
        jid = self.c.get('/api/v1/work/awards/' + a['id'], headers=self.H).json()['milestones'][0]['job_id']
        with self.inst.app.state.services.db.tx() as db:
            db.execute("INSERT INTO meta VALUES (?, '1')", ('fault:verification_fail:' + jid,))
        v2 = self.verify_ms(a['id']); self.assertEqual(self.c.get('/api/v1/verification/' + v2['id'], headers=self.H).json()['state'], 'failed')
        with self.inst.app.state.services.db.tx() as db:
            db.execute("DELETE FROM meta WHERE key=?", ('fault:verification_fail:' + jid,))
        recs = self.c.get('/api/v1/work/awards/' + a['id'] + '/receipts', headers=self.H).json()['items']
        self.assertEqual(sorted(x['statement']['claims']['outcome'] for x in recs if x['kind'] == 'verification'), ['failed', 'passed'])
        ev = self.evaluate(a['id']); self.assertEqual(ev['decision_candidate'], 'accepted')                       # a passed audit on the same commitment satisfies the policy
        # mistaken rejection by the requester (recorded with reason), then a dispute corrects it by supersession
        d1 = self.decide(a['id'], decision='reject', reason='reviewer believed the verification failed')
        self.assertEqual((d1['decision'], d1['authority']), ('rejected', 'requester'))
        self.assertEqual(self.c.post('/api/v1/work/awards/%s/milestones/m1/dispute' % a['id'], headers=self.inst.h('viewer'), json={'claim': 'x'}).status_code, 403)
        dp = self.c.post('/api/v1/work/awards/%s/milestones/m1/dispute' % a['id'], headers=self.pv['alpha']['h'], json={'claim': 'the passed full_exact verification on the same commitment was ignored'}); self.assertEqual(dp.status_code, 201, dp.text); dp = dp.json()
        self.assertEqual((dp['state'], dp['resolver'], dp['milestone_state']), ('open', 'designated_reviewer', 'disputed')); self.assertIn(v1['id'], dp['snapshot']['verifications'])
        self.assertEqual(self.c.post('/api/v1/work/disputes/' + dp['id'] + '/respond', headers=self.H, json={'text': 'we relied on the second record'}).json()['state'], 'responded')
        self.c.post('/api/v1/work/disputes/' + dp['id'] + '/evidence', headers=self.pv['alpha']['h'], json={'verification_id': v1['id']})
        rc = self.c.post('/api/v1/work/disputes/' + dp['id'] + '/recheck', headers=self.R, json={}).json(); self.run_worker(2)
        diag = rc['timeline'][-1]['body']['diagnostic']; self.assertTrue(diag['input_root_matches_terms'] and diag['method_version_matches'] and diag['evidence_root_unchanged'])
        # unauthorized resolver (the provider) and a resolution naming the wrong contract: refused, state unchanged
        self.assertEqual(self.c.post('/api/v1/work/disputes/' + dp['id'] + '/decide', headers=self.pv['alpha']['h'], json={'outcome': 'supersede_acceptance'}).status_code, 403)
        self.assertEqual(self.c.post('/api/v1/work/disputes/' + dp['id'] + '/decide', headers=self.H, json={'outcome': 'supersede_acceptance'}).status_code, 403)
        self.assertEqual(self.c.post('/api/v1/work/disputes/' + dp['id'] + '/decide', headers=self.R, json={'outcome': 'supersede_acceptance', 'award_id': 'wa_other'}).status_code, 422)
        self.assertEqual(self.c.get('/api/v1/work/disputes/' + dp['id'], headers=self.H).json()['state'], 'recheck')
        dec = self.c.post('/api/v1/work/disputes/' + dp['id'] + '/decide', headers=self.R, json={'outcome': 'supersede_acceptance', 'reason': 'the replay passed on the identical commitment'}); self.assertEqual(dec.status_code, 200, dec.text); dec = dec.json()
        self.assertEqual(dec['state'], 'decided'); self.assertEqual(dec['decision']['outcome'], 'supersede_acceptance'); self.assertIsNotNone(dec['decision']['monetary_consequence']['entitlement_id'])
        hist = self.c.get('/api/v1/work/awards/%s/milestones/m1/decisions' % a['id'], headers=self.H).json()['items']
        self.assertEqual([(h['decision'], h['authority'], h['current']) for h in hist], [('rejected', 'requester', False), ('accepted', 'dispute_resolution', True)])
        self.assertEqual(hist[0]['superseded_by'], hist[1]['id']); self.assertEqual(hist[1]['supersedes'], hist[0]['id'])
        aw = self.c.get('/api/v1/work/awards/' + a['id'], headers=self.H).json(); self.assertEqual(aw['milestones'][0]['dimensions']['acceptance'], 'accepted')
        # second decision after closure refused; timeline retained
        self.assertEqual(self.c.post('/api/v1/work/disputes/' + dp['id'] + '/close', headers=self.R, json={}).json()['state'], 'closed')
        self.assertEqual(self.c.post('/api/v1/work/disputes/' + dp['id'] + '/decide', headers=self.R, json={'outcome': 'uphold'}).status_code, 409)
        self.assertEqual([e['kind'] for e in self.c.get('/api/v1/work/disputes/' + dp['id'], headers=self.H).json()['timeline']], ['open', 'response', 'evidence', 'recheck', 'decision', 'close'])
        # deadline behaviour: an unresolved dispute reaching its deadline closes and restores the last decided state
        t3, f3, r3, o3, a3 = self.awarded('FEASIBLE'); self.run_worker(); self.verify_ms(a3['id']); self.decide(a3['id'])
        dp3 = self.c.post('/api/v1/work/awards/%s/milestones/m1/dispute' % a3['id'], headers=self.H, json={'claim': 'requester doubts', 'scope': 'evidence'}).json()
        with self.inst.app.state.services.db.tx() as db:
            db.execute('UPDATE work_disputes SET deadline_at=1 WHERE id=?', (dp3['id'],))
            self.inst.app.state.services.economy.tick(db)
        v = self.c.get('/api/v1/work/disputes/' + dp3['id'], headers=self.H).json(); self.assertEqual((v['state'], v['milestone_state']), ('closed', 'accepted')); self.assertIn('unresolved', v['close_reason'])

    # §30 distinct verifier and provider self-verification ------------------------------------------------------------------
    def test_self_verification_permitted_and_distinct_verifier_blocked_honestly(self):
        t, f, r, o, a = self.awarded('FEASIBLE'); self.run_worker()
        sv = self.verify_ms(a['id'], h=self.pv['alpha']['h']); self.assertTrue(sv['self_verification'])
        ev = self.evaluate(a['id']); self.assertEqual(ev['decision_candidate'], 'accepted')                       # policy permits same-custody verification
        base = self.c.post('/api/v1/work/terms', headers=self.H, json={'template': 'determination'}).json()['terms']
        acc = dict(base['acceptance'], required_verification={'class': 'full_exact', 'distinct_verifier': True}, predicates=[p if p['id'] != 'replay' else {'id': 'replay', 'type': 'verification_passed', 'params': {'class': 'full_exact', 'distinct_verifier': True}} for p in base['acceptance']['predicates']])
        t2, f2, r2, o2, a2 = self.awarded('FEASIBLE', terms_body={'terms': dict(base, acceptance=acc)}, distinct=True); self.run_worker()
        blocked = self.c.post('/api/v1/work/awards/%s/milestones/m1/verify' % a2['id'], headers=self.pv['alpha']['h'], json={}).json(); self.assertTrue(blocked.get('blocked')); self.assertEqual(blocked['code'], 'distinct_verifier_required')
        self.verify_ms(a2['id'])                                                                                    # requester-requested, same service custody
        ev2 = self.evaluate(a2['id']); self.assertEqual(ev2['decision_candidate'], 'pending')
        self.assertIn('distinct verifier', [x['reason'] for x in ev2['trace'] if x['type'] == 'verification_passed'][0])
        self.decide(a2['id'], expect=409)                                                                            # unavailable independence cannot be claimed fulfilled

    # §16 three milestones: accepted positive, accepted negative, downstream cancelled by policy -------------------------------------
    def test_three_milestones_negative_stops_downstream_by_policy(self):
        base = self.c.post('/api/v1/work/terms', headers=self.H, json={'template': 'determination', 'ceiling': 10}).json()['terms']
        ms = [{'key': 'pos', 'deliverables': ['determination'], 'max_payment': 3, 'depends_on': [], 'deadline_seconds': 3600, 'on_failure': 'stop_downstream'},
              {'key': 'neg', 'deliverables': ['determination'], 'max_payment': 3, 'depends_on': [], 'deadline_seconds': 3600, 'on_failure': 'stop_downstream'},
              {'key': 'down', 'deliverables': ['determination'], 'max_payment': 4, 'depends_on': ['pos', 'neg'], 'deadline_seconds': 3600, 'on_failure': 'stop_downstream', 'requires_acceptance_of': {'pos': 'accepted_positive', 'neg': 'accepted_positive'}}]
        acc = dict(base['acceptance'], payment_rule=dict(base['acceptance']['payment_rule'], complete=3, diagnostic=1))
        t, f, r, o, a = self.awarded(terms_body={'terms': dict(base, milestones=ms, acceptance=acc)}, milestone_inputs={'pos': energy_inputs('FEASIBLE'), 'neg': energy_inputs('INFEASIBLE')})
        self.run_worker(3)
        for key in ('pos', 'neg'):
            self.verify_ms(a['id'], key); d = self.decide(a['id'], key); self.assertEqual((d['decision'], d['payable_amount']), ('accepted', 3))
        aw = self.c.get('/api/v1/work/awards/' + a['id'], headers=self.H).json(); st = {m['key']: m for m in aw['milestones']}
        self.assertEqual((st['pos']['dimensions']['science'], st['neg']['dimensions']['science']), ('FEASIBLE', 'INFEASIBLE'))
        self.assertEqual(st['down']['state'], 'cancelled'); self.assertIn('stopped by policy', st['down']['blocked_reason'])
        self.assertEqual((st['pos']['dimensions']['payment'], st['neg']['dimensions']['payment'], st['down']['dimensions']['payment']), ('payable', 'payable', 'released'))
        with self.inst.app.state.services.db.tx() as db:
            self.assertEqual(db.execute('SELECT SUM(amount) FROM work_entitlements WHERE award_id=?', (a['id'],)).fetchone()[0], 6)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM work_receipts WHERE award_id=?', (a['id'],)).fetchone()[0], 6)     # 2 provider + 2 verification + 2 acceptance; evidence retained
        self.assertEqual(aw['state'], 'delivered')

    # §25 + §26: delegation bounds and reassignment with a late old worker --------------------------------------------------------
    def test_delegation_bounds_and_reassignment_after_failure(self):
        base = self.c.post('/api/v1/work/terms', headers=self.H, json={'template': 'determination', 'ceiling': 10}).json()['terms']
        dg = {'allowed': True, 'max_depth': 1, 'max_nodes': 1, 'max_sub_budget': 4, 'artifact_scope': 'derived_inputs_only'}
        t, f, r, o, a = self.awarded(terms_body={'terms': dict(base, delegation=dg)})
        # over-budget delegation refused before dispatch; unauthorized disclosure scope refused; then a legitimate delegation
        e = self.c.post('/api/v1/work/awards/%s/milestones/m1/delegate' % a['id'], headers=self.pv['alpha']['h'], json={'provider_id': self.pv['beta']['id'], 'sub_budget': 5}); self.assertEqual(e.json()['code'], 'BUDGET_EXHAUSTED', e.text)
        s = self.c.post('/api/v1/work/awards/%s/milestones/m1/delegate' % a['id'], headers=self.pv['alpha']['h'], json={'provider_id': self.pv['beta']['id'], 'sub_budget': 2, 'artifact_scope': 'declared_subtask_inputs'}); self.assertEqual(s.status_code, 403)
        self.assertEqual(self.c.post('/api/v1/work/awards/%s/milestones/m1/delegate' % a['id'], headers=self.pv['beta']['h'], json={'provider_id': self.pv['beta']['id'], 'sub_budget': 1}).status_code, 403)
        d = self.c.post('/api/v1/work/awards/%s/milestones/m1/delegate' % a['id'], headers=self.pv['alpha']['h'], json={'provider_id': self.pv['beta']['id'], 'sub_budget': 2}); self.assertEqual(d.status_code, 201, d.text); d = d.json()
        self.assertEqual(d['obligations']['requester_to_primary'], 'unchanged (entitlement recipient stays the awarded provider)')
        self.run_worker(3); self.verify_ms(a['id']); dec = self.decide(a['id'])
        aw = self.c.get('/api/v1/work/awards/' + a['id'], headers=self.H).json()
        self.assertEqual([x['state'] for x in aw['milestones'][0]['attempts']], ['superseded', 'completed']); self.assertEqual(aw['milestones'][0]['attempts'][1]['job_id'], d['job_id'])
        self.assertEqual((dec['entitlement']['amount'], self.c.get('/api/v1/work/entitlements/' + dec['entitlement']['id'], headers=self.H).json()['recipient']), (10, 'provider:alpha'))   # not charged twice; recipient unchanged
        # reassignment after a terminal failure: old award preserved and released, replacement awarded to beta, old worker fenced
        self.inst.settings.limits['job_max_retries'] = 0
        t2, f2, r2, o2, a2 = self.awarded('FEASIBLE')
        ob = self.c.post('/api/v1/work/requests/' + r2['id'] + '/offers', headers=self.pv['beta']['h'], json={'price_amount': 9, 'asset': 'action-units', 'scheme': 'exact', 'window_seconds': 3600, 'verification': {'class': 'full_exact'}})
        self.assertEqual(ob.status_code, 409)                                                                        # request already awarded: no new offers
        self.assertEqual(self.c.post('/api/v1/work/awards/' + a2['id'] + '/reassign', headers=self.H, json={'condition': 'terminal_failure'}).json()['detail']['code'], 'no_terminal_failure')
        os.environ['METACOIN_TEST_EXEC_DELAY_SECONDS'] = '5'; self.inst.settings.limits['job_timeout_seconds'] = 1
        try:
            self.run_worker(2)
        finally:
            del os.environ['METACOIN_TEST_EXEC_DELAY_SECONDS']; self.inst.settings.limits['job_timeout_seconds'] = 30
        # a standing (superseded) offer from beta on the same request can be the replacement
        with self.inst.app.state.services.db.tx() as db:
            db.execute("UPDATE work_requests SET state='open' WHERE id=?", (r2['id'],))
        ob = self.c.post('/api/v1/work/requests/' + r2['id'] + '/offers', headers=self.pv['beta']['h'], json={'price_amount': 9, 'asset': 'action-units', 'scheme': 'exact', 'window_seconds': 3600, 'verification': {'class': 'full_exact'}}).json()
        with self.inst.app.state.services.db.tx() as db:
            db.execute("UPDATE work_requests SET state='awarded' WHERE id=?", (r2['id'],))
        rs = self.c.post('/api/v1/work/awards/' + a2['id'] + '/reassign', headers=self.H, json={'condition': 'terminal_failure', 'offer_id': ob['id']}); self.assertEqual(rs.status_code, 200, rs.text); rs = rs.json()
        self.assertTrue(rs['reserve_released']); self.assertEqual(rs['new_award']['provider_id'], self.pv['beta']['id'])
        old = self.c.get('/api/v1/work/awards/' + a2['id'], headers=self.H).json(); self.assertEqual((old['state'], old['active'], old['replaced_by']), ('reassigned', False, rs['new_award']['id']))
        self.assertEqual(old['milestones'][0]['attempts'][0]['state'], 'failed')                                        # old evidence/state preserved
        self.run_worker(2); self.verify_ms(rs['new_award']['id']); dn = self.decide(rs['new_award']['id']); self.assertEqual(dn['decision'], 'accepted')
        tree = self.c.get('/api/v1/budgets/tree', headers=self.H).json()['tree']
        self.assertEqual(tree['reserved'], 10 + 9)                                                                    # first award (10) + replacement (9); the failed award's 10 released


if __name__ == '__main__':
    unittest.main()
