"""Groups E and F (Order 08 §40–§44, §56–§60): compartments per role, read-only audit grants (refusals for spending,
out-of-scope reads, expiry, revocation), encrypted offline packages to a recipient key, dispute holds and deletion,
projections with leakage checks and a non-anchored candidate record, the mission portfolio with a bottleneck-derived
contract completed end to end, contributions, the resource probe and the simulated physical-observation boundary."""
import base64
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from metacoin_service import crypto
from metacoin_service.economy import legacy_bridge, missions as missions_mod
from metacoin_service.tests.test_work_evidence import EvidenceBase, ROOT
from metacoin_service.tests.test_work_terms import energy_inputs


class WorkAccessMissionTests(EvidenceBase):
    def accepted_award(self, outcome='FEASIBLE'):
        t, f, r, o, a = self.awarded(outcome); self.run_worker(); self.verify_ms(a['id']); d = self.decide(a['id'])
        return t, a, d

    # J21 + J22 + compartments (§40, §41) --------------------------------------------------------------------------------
    def test_compartments_and_audit_grants(self):
        t, a, d = self.accepted_award()
        comp = {role: self.c.get('/api/v1/work/awards/' + a['id'] + '/compartments?milestone=m1', headers=h).json() for role, h in (('requester', self.H), ('provider', self.pv['alpha']['h']), ('resolver', self.R), ('reader', self.inst.h('viewer')))}
        self.assertEqual({k: v['role'] for k, v in comp.items()}, {'requester': 'requester', 'provider': 'provider', 'resolver': 'resolver', 'reader': 'reader'})
        self.assertIn('private_inputs', comp['requester']['visible_categories']); self.assertNotIn('private_inputs', comp['provider']['visible_categories']); self.assertNotIn('private_inputs', comp['reader']['visible_categories'])
        inputs_art = next(x['artifact_id'] for x in comp['requester']['milestone']['artifacts'] if x['category'] == 'private_inputs')
        evidence_art = next(x['artifact_id'] for x in comp['requester']['milestone']['artifacts'] if x['category'] == 'private_evidence')
        self.assertEqual(self.c.get('/api/v1/work/awards/%s/artifacts/%s' % (a['id'], inputs_art), headers=self.inst.h('viewer')).status_code, 403)
        self.assertEqual(self.c.get('/api/v1/work/awards/%s/artifacts/%s' % (a['id'], inputs_art), headers=self.H).status_code, 200)
        # audit grant: read-only, purpose-bound, expiring; the grantee (viewer) reads permitted categories, cannot pay or amend
        g = self.c.post('/api/v1/work/awards/' + a['id'] + '/audit-grants', headers=self.H, json={'grantee_id': self.inst.ids['viewer'], 'purpose': 'external audit of the determination', 'categories': ['terms', 'receipts', 'verification_statements', 'private_evidence'], 'expires_in_seconds': 120, 'download': 'download_allowed'})
        self.assertEqual(g.status_code, 201, g.text); g = g.json(); self.assertEqual(g['state'], 'active'); self.assertIn('not a Zcash viewing key', g['statement']['not'])
        V = self.inst.h('viewer')
        use = self.c.post('/api/v1/work/audit-grants/' + g['id'] + '/use', headers=V, json={}).json()
        self.assertEqual(sorted(use), sorted(['grant_id', 'award_id', 'categories', 'purpose', 'expires_at', 'terms', 'receipts', 'verification_statements', 'private_evidence']))
        self.assertEqual(self.c.get('/api/v1/work/awards/%s/artifacts/%s' % (a['id'], evidence_art), headers=V).status_code, 200)     # in scope through the grant
        self.assertEqual(self.c.get('/api/v1/work/awards/%s/artifacts/%s' % (a['id'], inputs_art), headers=V).status_code, 403)       # out of scope: private inputs not granted
        self.assertEqual(self.c.post('/api/v1/work/entitlements/' + d['entitlement']['id'] + '/prepare', headers=V, json={}).status_code, 403)     # audit authority cannot spend
        self.assertEqual(self.c.post('/api/v1/work/terms/' + t['id'] + '/amend', headers=V, json={'terms': {'title': 'x'}}).status_code, 403)          # nor amend
        hist = self.c.get('/api/v1/work/audit-grants/' + g['id'], headers=self.H).json()['access_history']
        self.assertEqual([(e['action'], bool(e['allowed'])) for e in hist], [('use', True), ('read_artifact', True), ('read_artifact', False)])
        # expiry during the audit
        with self.inst.app.state.services.db.tx() as db:
            db.execute('UPDATE audit_grants SET expires_at=1 WHERE id=?', (g['id'],))
        self.assertEqual(self.c.post('/api/v1/work/audit-grants/' + g['id'] + '/use', headers=V, json={}).json()['code'], 'EXPIRED')
        self.assertEqual(self.c.get('/api/v1/work/awards/%s/artifacts/%s' % (a['id'], evidence_art), headers=V).status_code, 403)
        # a fresh grant, then revocation while the session is open
        g2 = self.c.post('/api/v1/work/awards/' + a['id'] + '/audit-grants', headers=self.H, json={'grantee_id': self.inst.ids['viewer'], 'purpose': 'second look', 'categories': ['receipts'], 'expires_in_seconds': 600}).json()
        self.assertEqual(self.c.post('/api/v1/work/audit-grants/' + g2['id'] + '/use', headers=V, json={}).status_code, 200)
        rv = self.c.post('/api/v1/work/audit-grants/' + g2['id'] + '/revoke', headers=self.H, json={}).json(); self.assertEqual(rv['state'], 'revoked'); self.assertIn('cannot be recalled', rv['limits'])
        r = self.c.post('/api/v1/work/audit-grants/' + g2['id'] + '/use', headers=V, json={}); self.assertEqual(r.status_code, 403); self.assertEqual(r.json()['detail']['code'], 'grant_revoked')
        self.assertEqual(self.c.post('/api/v1/work/audit-grants/' + g2['id'] + '/use', headers=self.pv['alpha']['h'], json={}).status_code, 403)     # not the holder

    # J23 (§42) ----------------------------------------------------------------------------------------------------------------------
    def test_encrypted_offline_package_for_one_recipient(self):
        t, a, d = self.accepted_award('INFEASIBLE')
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup); p = Path(tmp.name)
        pub = crypto.generate_age_identity(p / 'recipient.key'); other = crypto.generate_age_identity(p / 'other.key')
        pk = self.c.post('/api/v1/work/awards/%s/milestones/m1/package' % a['id'], headers=self.H, json={'recipient_age_public': pub, 'scope': 'full'}); self.assertEqual(pk.status_code, 201, pk.text); pk = pk.json()
        ct = base64.b64decode(pk['ciphertext_b64'])
        self.assertEqual(hashlib.sha256(ct).hexdigest(), pk['manifest']['ciphertext_sha256']); self.assertNotIn('AGE-SECRET-KEY', json.dumps(pk))
        self.assertTrue(crypto.verify(pk['issuer_public_key'], __import__('experiments.private_receipts.receipt', fromlist=['canonical']).canonical(pk['manifest']), pk['manifest_signature']))
        plain = crypto.decrypt_bytes(ct, crypto.load_age_identity(p / 'recipient.key'))
        self.assertEqual(hashlib.sha256(plain).hexdigest(), pk['manifest']['inner_bundle_sha256'])
        (p / 'bundle.zip').write_bytes(plain)
        env = {'PATH': os.environ.get('PATH', ''), 'HOME': os.environ.get('HOME', '/'), 'PYTHONPATH': str(ROOT)}
        rep = json.loads(subprocess.run([sys.executable, '-m', 'metacoin_service.economy.verify_work', 'bundle.zip', '--trust-root', pk['issuer_public_key'], '--json'], cwd=p, env=env, capture_output=True, text=True, timeout=120).stdout)
        self.assertTrue(rep['integrity']['ok'] and rep['signer_trust']['trusted']); self.assertEqual(rep['scientific_replay']['scientific_outcome'], 'INFEASIBLE')
        with self.assertRaises(Exception):
            crypto.decrypt_bytes(ct, crypto.load_age_identity(p / 'other.key'))                                            # wrong recipient
        with self.assertRaises(Exception):
            crypto.decrypt_bytes(ct[:-7] + b'\\x00' * 7, crypto.load_age_identity(p / 'recipient.key'))                   # changed ciphertext
        tampered = dict(pk['manifest'], disclosure_scope='restricted')
        self.assertFalse(crypto.verify(pk['issuer_public_key'], __import__('experiments.private_receipts.receipt', fromlist=['canonical']).canonical(tampered), pk['manifest_signature']))   # changed manifest
        self.assertIn('cannot revoke an already decrypted package', pk['manifest']['limits'][0])
        self.assertEqual(self.c.post('/api/v1/work/awards/%s/milestones/m1/package' % a['id'], headers=self.H, json={'recipient_age_public': 'not-a-key'}).status_code, 422)

    # J24 + §43 --------------------------------------------------------------------------------------------------------------------
    def test_retention_holds_deletion_and_late_worker(self):
        t, a, d = self.accepted_award()
        dp = self.c.post('/api/v1/work/awards/%s/milestones/m1/dispute' % a['id'], headers=self.pv['alpha']['h'], json={'claim': 'hold test'}).json()
        ret = self.c.get('/api/v1/work/awards/%s/milestones/m1/retention' % a['id'], headers=self.H).json(); self.assertEqual(len([h for h in ret['holds'] if h['active']]), 1)
        refused = self.c.post('/api/v1/work/awards/%s/milestones/m1/evidence/delete' % a['id'], headers=self.H, json={}); self.assertEqual(refused.json()['detail']['code'], 'evidence_held')
        from metacoin_service import ops
        with self.inst.app.state.services.db.tx() as db:
            db.execute('UPDATE artifacts SET retention_deadline=1 WHERE job_id=?', (ret['artifacts'][0]['id'],)) if False else None
            evidence_ids = [x['id'] for x in ret['artifacts']]
            db.execute('UPDATE artifacts SET retention_deadline=1 WHERE id IN (%s)' % ','.join('?' * len(evidence_ids)), evidence_ids)
        out = ops.cleanup(self.inst.settings)
        self.assertTrue(all(s['code'] == 'HELD_BY_DISPUTE' for s in out['skipped'] if s['id'] in evidence_ids)); self.assertFalse(set(out['removed']) & set(evidence_ids))
        self.c.post('/api/v1/work/disputes/' + dp['id'] + '/close', headers=self.R, json={'reason': 'withdrawn'})
        ret2 = self.c.get('/api/v1/work/awards/%s/milestones/m1/retention' % a['id'], headers=self.H).json(); self.assertEqual([h['active'] for h in ret2['holds']], [0])
        dl = self.c.post('/api/v1/work/awards/%s/milestones/m1/evidence/delete' % a['id'], headers=self.H, json={}).json()
        self.assertTrue(dl['removed_payloads']); self.assertEqual(dl['remains']['evidence_root'], ret['artifacts'] and self.c.get('/api/v1/work/awards/' + a['id'], headers=self.H).json()['milestones'][0]['evidence_root'])
        self.assertEqual(self.c.get('/api/v1/work/awards/%s/milestones/m1/bundle' % a['id'], headers=self.H).status_code, 409)              # deleted evidence cannot be exported
        # the requester revokes its inputs while the worker is late: the attempt cannot publish evidence from a deleted source
        t2, f2, r2, o2, a2 = self.awarded('FEASIBLE')
        with self.inst.app.state.services.db.tx() as db:
            cid = db.execute('SELECT contract_id FROM work_milestones WHERE award_id=?', (a2['id'],)).fetchone()['contract_id']
            art = db.execute('SELECT input_artifact_id FROM contracts WHERE id=?', (cid,)).fetchone()['input_artifact_id']
        self.assertEqual(self.c.delete('/api/v1/artifacts/' + art, headers=self.H).status_code, 200)
        self.run_worker(2)
        aw = self.c.get('/api/v1/work/awards/' + a2['id'], headers=self.H).json(); m = aw['milestones'][0]
        self.assertEqual(m['dimensions']['execution'], 'failed'); self.assertIsNone(m['evidence_root'])
        self.assertEqual(self.evaluate(a2['id'])['decision_candidate'], 'rejected')

    # §44 projections and anchor candidate ------------------------------------------------------------------------------------------
    def test_projections_declare_omissions_and_candidate_is_not_anchored(self):
        t, a, d = self.accepted_award('INFEASIBLE')
        ledger = ROOT / 'protocol' / 'ledger_data.jsonl'; before = hashlib.sha256(ledger.read_bytes()).hexdigest()
        priv = self.c.post('/api/v1/work/awards/%s/milestones/m1/projection' % a['id'], headers=self.H, json={'audience': 'private'}).json()
        col = self.c.post('/api/v1/work/awards/%s/milestones/m1/projection' % a['id'], headers=self.H, json={'audience': 'collaborator'}).json()
        pub = self.c.post('/api/v1/work/awards/%s/milestones/m1/projection' % a['id'], headers=self.H, json={'audience': 'public_ready', 'sign': True}).json()
        self.assertEqual(priv['record']['payment']['recipient'], 'provider:alpha'); self.assertIsNone(col['record']['payment']['recipient']); self.assertIn('payment recipient address', col['declared_omissions'])
        self.assertEqual((pub['record']['science']['outcome'], pub['record']['acceptance']['decision'], pub['record']['payment']['amount'], pub['record']['provider']['name']), ('INFEASIBLE', 'accepted', None, None))
        self.assertIn('small_cohort', [c['check'] for c in pub['leakage_checks']]); self.assertIn('independently replayed', pub['record']['verification_claim'])
        self.assertTrue(crypto.verify(pub['signature']['public_key'], __import__('experiments.private_receipts.receipt', fromlist=['canonical']).canonical(pub['record']), pub['signature']['signature_hex']))
        for k in ('terms_digest', 'evidence_root'):
            self.assertEqual((priv['record'][k], col['record'][k], pub['record'][k]), (priv['record'][k],) * 3)                    # disclosed facts agree
        cand = self.c.post('/api/v1/work/awards/%s/milestones/m1/anchor-candidate' % a['id'], headers=self.H, json={}).json()
        self.assertEqual((cand['candidate']['anchored'], cand['candidate']['status'], cand['candidate']['no_token']), (False, 'candidate-not-anchored', True))
        self.assertEqual(hashlib.sha256(ledger.read_bytes()).hexdigest(), before)                                                # the real ledger is untouched
        self.assertEqual(self.c.post('/api/v1/work/awards/%s/milestones/m1/projection' % a['id'], headers=self.inst.h('viewer'), json={'audience': 'public_ready'}).status_code, 403)

    # J39 + §56–§58 ----------------------------------------------------------------------------------------------------------------
    def test_mission_bottleneck_to_contract_updates_only_the_portfolio(self):
        verdict = json.loads((ROOT / 'mission_verdict.json').read_text()); before = hashlib.sha256((ROOT / 'mission_verdict.json').read_bytes()).hexdigest()
        pf = self.c.post('/api/v1/work/missions/import', headers=self.H, json={}); self.assertEqual(pf.status_code, 201, pf.text); pf = pf.json()
        self.assertEqual((pf['mission_id'], pf['imported']['verdict_hash'], pf['imported']['mission_feasible']), (verdict['mission_id'], verdict['verdict_hash'], False))
        self.assertIn('task-0018', pf['unresolved_bottlenecks'])
        self.assertEqual(self.c.post('/api/v1/work/missions/import', headers=self.H, json={}).json()['id'], pf['id'])                    # idempotent
        dr = self.c.post('/api/v1/work/missions/%s/bottlenecks/task-0018/draft' % pf['id'], headers=self.H, json={'ceiling': 3}); self.assertEqual(dr.status_code, 201, dr.text); dr = dr.json()
        terms = dr['terms']['terms']
        self.assertEqual((terms['operation']['kind'], terms['purpose']['node'], terms['purpose']['mission_id']), ('legacy_task_replay', 'task-0018', verdict['mission_id']))
        self.assertEqual(terms['acceptance']['predicates'][-1]['params']['registered_hash'], legacy_bridge.registry()['task-0018']['registered_hash'])
        self.assertIn('task-0021', terms['purpose']['affects']); self.assertEqual(self.c.get('/api/v1/work/journal', headers=self.H).json()['entries'], [])       # nothing spent
        self.assertEqual(self.c.post('/api/v1/work/missions/%s/bottlenecks/task-9999/draft' % pf['id'], headers=self.H, json={}).status_code, 404)
        # the requester reviews, freezes with the suggested inputs, opens the request, links it, awards, executes, accepts
        f = self.c.post('/api/v1/work/terms/' + dr['terms']['id'] + '/freeze', headers=self.H, json={'inputs': dr['suggested_inputs_for_freeze']}); self.assertEqual(f.status_code, 200, f.text)
        r = self.c.post('/api/v1/work/requests', headers=self.H, json={'terms_id': dr['terms']['id']}).json(); self.c.post('/api/v1/work/requests/' + r['id'] + '/open', headers=self.H, json={})
        self.c.post('/api/v1/work/missions/%s/link' % pf['id'], headers=self.H, json={'link_id': dr['link_id'], 'request_id': r['id']})
        o = self.c.post('/api/v1/work/requests/' + r['id'] + '/offers', headers=self.pv['alpha']['h'], json={'price_amount': 3, 'asset': 'action-units', 'scheme': 'exact', 'window_seconds': 3600, 'verification': {'class': 'none'}}).json()
        a = self.c.post('/api/v1/work/requests/' + r['id'] + '/award', headers=self.H, json={'offer_id': o['id']}).json()
        self.run_worker(); d = self.decide(a['id']); self.assertEqual((d['decision'], d['evaluation']['science']), ('accepted', 'not_applicable'))
        view = self.c.get('/api/v1/work/missions/' + pf['id'], headers=self.H).json()
        c = view['contributions'][0]
        self.assertEqual((c['affected_node'], c['contribution_type'], c['contribution_kind'], c['evidence_outcome']), ('task-0018', 'verified_computation', 'commissioned_replication', 'EXACT_MATCH'))
        self.assertNotIn('task-0018', view['unresolved_bottlenecks'])
        self.assertEqual(view['objectives_and_constraints']['node_verdicts']['task-0018']['verdict'], False)                    # the anchored negative is unchanged
        self.assertEqual(view['imported']['verdict_hash'], verdict['verdict_hash']); self.assertEqual(hashlib.sha256((ROOT / 'mission_verdict.json').read_bytes()).hexdigest(), before)
        self.assertEqual(view['commissioned_work'][0]['awards'][0]['findings'][0]['acceptance'], 'accepted'); self.assertEqual(view['budget']['ceilings'], 3)
        # an accepted negative on a linked determination closes a branch; a second identical replay is flagged as a duplicate, not a second entitlement
        dr2 = self.c.post('/api/v1/work/missions/%s/bottlenecks/task-0018/draft' % pf['id'], headers=self.H, json={'ceiling': 3}).json()
        self.c.post('/api/v1/work/terms/' + dr2['terms']['id'] + '/freeze', headers=self.H, json={'inputs': dr2['suggested_inputs_for_freeze']})
        r2 = self.c.post('/api/v1/work/requests', headers=self.H, json={'terms_id': dr2['terms']['id']}).json(); self.c.post('/api/v1/work/requests/' + r2['id'] + '/open', headers=self.H, json={})
        self.c.post('/api/v1/work/missions/%s/link' % pf['id'], headers=self.H, json={'link_id': dr2['link_id'], 'request_id': r2['id']})
        o2 = self.c.post('/api/v1/work/requests/' + r2['id'] + '/offers', headers=self.pv['beta']['h'], json={'price_amount': 3, 'asset': 'action-units', 'scheme': 'exact', 'window_seconds': 3600, 'verification': {'class': 'none'}}).json()
        a2 = self.c.post('/api/v1/work/requests/' + r2['id'] + '/award', headers=self.H, json={'offer_id': o2['id']}).json(); self.run_worker(); self.decide(a2['id'])
        c2 = self.c.get('/api/v1/work/missions/' + pf['id'], headers=self.H).json()['contributions'][1]
        self.assertEqual(c2['contribution_kind'], 'commissioned_replication'); self.assertIsNotNone(json.loads(c2['dedup_json']))

    # §59 + §60 --------------------------------------------------------------------------------------------------------------------
    def test_resource_probe_and_simulated_observation_boundary(self):
        probe = self.c.get('/api/v1/work/resource-evidence/probe', headers=self.H).json()
        self.assertIn('calibrated hardware energy counter', probe['not_available'][-1]); self.assertIn('gpu_power_w_sample', probe['measurements_available'])
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        pub = crypto.generate_signing_key(Path(tmp.name) / 'device.key'); key = crypto.load_signing_key(Path(tmp.name) / 'device.key')
        base = {'schema': missions_mod.OBSERVATION_SCHEMA, 'simulated': True, 'device': {'device_id': 'sim-plate-reader-01', 'public_key_hex': pub, 'kind': 'simulated-plate-reader-with-power-meter'},
                'calibration': {'calibrated_at': 1000, 'reference': 'synthetic calibration fixture v1', 'valid_until': 9000}, 'samples': [{'t': 2000, 'value': 500}, {'t': 2001, 'value': 600}, {'t': 2002, 'value': 700}],
                'uncertainty': {'absorbance_milli_au': 10}, 'declared_bounds': {'unit': 'absorbance_au', 'scale': 1000, 'min': 0, 'max': 2000, 'max_rate_per_s': 500}}
        pkg = dict(base, signature_hex=missions_mod.sign_observation(key, base))
        ok = self.c.post('/api/v1/work/observations', headers=self.H, json={'package': pkg}); self.assertEqual(ok.status_code, 201, ok.text); ok = ok.json()
        self.assertTrue(all(c['ok'] for c in ok['checks'])); self.assertTrue(ok['no_actuation']); self.assertIn('SIMULATED', ok['label'])
        self.assertEqual(self.c.get('/api/v1/work/observations/' + ok['id'], headers=self.H).json()['label'], 'simulated')
        nocal = dict(base); nocal['calibration'] = {'calibrated_at': 1000}; nocal['signature_hex'] = missions_mod.sign_observation(key, nocal)
        self.assertEqual(self.c.post('/api/v1/work/observations', headers=self.H, json={'package': nocal}).json()['detail']['code'], 'calibration_missing')
        changed = json.loads(json.dumps(pkg)); changed['samples'][1]['value'] = 601
        self.assertEqual(self.c.post('/api/v1/work/observations', headers=self.H, json={'package': changed}).json()['detail']['code'], 'signature_invalid')
        implaus = dict(base, samples=[{'t': 2000, 'value': 500}, {'t': 2001, 'value': 1900}, {'t': 2000, 'value': 700}]); implaus['signature_hex'] = missions_mod.sign_observation(key, implaus)
        r = self.c.post('/api/v1/work/observations', headers=self.H, json={'package': implaus}).json(); self.assertEqual(r['detail']['code'], 'implausible_sequence')
        self.assertEqual({c['check']: c['ok'] for c in r['detail']['checks']}['timestamps_monotonic'], False)
        real = dict(base, simulated=False); real['signature_hex'] = missions_mod.sign_observation(key, real)
        self.assertEqual(self.c.post('/api/v1/work/observations', headers=self.H, json={'package': real}).json()['detail']['code'], 'real_hardware_not_authorized')


if __name__ == '__main__':
    unittest.main()
