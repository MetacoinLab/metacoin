"""Independent verification through real entry points: previews with honest cost/scope, full exact and sampled audits
with server-side challenges, analytical checks (row invariants; closed-form heat eigenmode), a full scalar heat
reference, Monte Carlo audits, replica agreement and a disputed replica, signed statements and their public
projection, corrupted results rejected, the review gate bound by contract policy, and reviewer authority limits."""
import hashlib
import json
import os
import unittest

from experiments.private_receipts import receipt as merkle
from metacoin_service import crypto
from metacoin_service.compute import container, npy
from metacoin_service.tests.test_compute_engine import ComputeInstance, HAVE_RUNTIME, HAVE_CUDA, batch_spec, mc_spec, heat_spec


def corrupt_output(inst, jid, mutate, rewrite_vault=False):
    """Simulate a dishonest producer: rewrite the stored compute_output container (and optionally the evidence vault's
    output commitments) so the artifact loads but the science is wrong."""
    store = inst.worker().store
    from metacoin_service.db import Database
    D = Database(inst.settings.db_path)
    with D.tx() as db:
        run = db.execute('SELECT * FROM compute_runs WHERE job_id=?', (jid,)).fetchone()
        job = db.execute('SELECT * FROM jobs WHERE id=?', (jid,)).fetchone()
        files = container.unpack(store.load(db, run['output_artifact_id'], job['workspace']))
        files = mutate(files)
        blob = container.pack(files)
        row = db.execute('SELECT * FROM artifacts WHERE id=?', (run['output_artifact_id'],)).fetchone()
        data = crypto.encrypt_bytes(blob, json.loads(row['recipients_json']))
        (store.dir / row['storage_name']).write_bytes(data)
        db.execute('UPDATE artifacts SET sha256_ciphertext=?, sha256_plaintext=?, size_plaintext=? WHERE id=?', (hashlib.sha256(data).hexdigest(), hashlib.sha256(blob).hexdigest(), len(blob), row['id']))
        if rewrite_vault:
            vault = store.load_json(db, job['evidence_artifact_id'], job['workspace'])
            from experiments.work_contracts import acceptance
            values = acceptance.full_values(vault, vault['receipt']['root'])
            values['output_commitments'] = {n: hashlib.sha256(b).hexdigest() for n, b in files.items()}
            _, new_vault = merkle.commit(values)
            vrow = db.execute('SELECT * FROM artifacts WHERE id=?', (job['evidence_artifact_id'],)).fetchone()
            vblob = merkle.canonical(new_vault)
            vdata = crypto.encrypt_bytes(vblob, json.loads(vrow['recipients_json']))
            (store.dir / vrow['storage_name']).write_bytes(vdata)
            db.execute('UPDATE artifacts SET sha256_ciphertext=?, sha256_plaintext=?, size_plaintext=? WHERE id=?', (hashlib.sha256(vdata).hexdigest(), hashlib.sha256(vblob).hexdigest(), len(vblob), vrow['id']))
            db.execute('UPDATE jobs SET evidence_root=? WHERE id=?', (new_vault['receipt']['root'], jid))


def flip_first_outcome(files):
    vals, dtype, shape = npy.decode(files['results.npy'])
    vals = list(vals); vals[0] = 1 if vals[0] != 1 else 0
    files['results.npy'] = npy.encode(vals, dtype, shape)
    return files


@unittest.skipUnless(HAVE_RUNTIME, 'no numpy-capable compute interpreter on this host')
class VerificationTests(unittest.TestCase):
    def setUp(self):
        self.inst = ComputeInstance(); self.addCleanup(self.inst.close); self.c = self.inst.client; self.H = self.inst.h('owner'); self.R = self.inst.h('reviewer')
        self.w = self.inst.worker(); self.addCleanup(self.w.offline)

    def request(self, jid, cls, params=None, headers=None, run=True):
        r = self.c.post('/api/v1/verification', headers=headers or self.H, json={'job_id': jid, 'class': cls, 'params': params or {}})
        self.assertEqual(r.status_code, 202, r.text)
        v = r.json()
        if run and v['audit_job_id']:
            self.assertEqual(self.w.run_once()[1], 'succeeded')
        return self.c.get('/api/v1/verification/' + v['id'], headers=headers or self.H).json()

    def test_audits_statements_corruption_gate_and_replica(self):
        jb = self.inst.compute_job('temporal_batch', batch_spec()); self.assertEqual(self.w.run_once()[1], 'succeeded')
        # previews state the claim, shared code, cost and affordability; unsupported classes are refused precisely
        pv = self.c.post('/api/v1/verification/preview', headers=self.H, json={'job_id': jb, 'class': 'full_exact'}).json()
        self.assertEqual((pv['estimated_work'], pv['affordable']), (364, True)); self.assertIn('scenario', pv['claim']); self.assertIn('independence', pv)
        self.assertEqual(self.c.post('/api/v1/verification/preview', headers=self.H, json={'job_id': jb, 'class': 'full_reference'}).json()['detail']['code'], 'class_unsupported_for_kind')
        # full exact, sampled (server-side challenge) and analytical audits pass on an honest result
        full = self.request(jb, 'full_exact')
        self.assertEqual((full['state'], full['result']['checked'], full['result']['total']), ('passed', 364, 364), full)
        samp = self.request(jb, 'sampled_reference', {'sample_count': 32})
        self.assertEqual((samp['state'], samp['result']['checked'], len(samp['result']['scope_items'])), ('passed', 32, 32))
        self.assertEqual(samp['challenge']['result_commitment'], full['result_commitment']); self.assertEqual(len(samp['challenge']['seed']), 32); self.assertIn('not guaranteed', samp['result']['statement'])
        ana = self.request(jb, 'analytical')
        self.assertEqual((ana['state'], ana['result']['coverage']), ('passed', 'invariants only'))
        # the audit job itself is an ordinary succeeded job with outcome AUDIT_PASSED; the reviewer sees the same statement
        aj = self.c.get('/api/v1/jobs/' + full['audit_job_id'], headers=self.H).json()
        self.assertEqual((aj['state'], aj['outcome'], aj['kind']), ('succeeded', 'AUDIT_PASSED', 'verification_audit'))
        # signed statement: public projection verifies; tampering or wrong bindings fail; the viewer cannot read private diagnostics
        proj = self.c.get('/api/v1/verification/' + full['id'] + '/statement', headers=self.H).json()
        st = proj['statement']
        self.assertEqual((st['class'], st['outcome'], st['scope']['checked'], st['target_job_id']), ('full_exact', 'passed', 364, jb)); self.assertNotIn('mismatches', json.dumps(st))
        ok = self.c.post('/api/v1/verification/verify-statement', headers=self.inst.h('viewer'), json={'bundle': proj, 'expected': {'target_job_id': jb, 'result_commitment': full['result_commitment']}}).json()
        self.assertEqual((ok['signature_valid'], ok['issuer_trusted_by_this_service'], ok['bindings_match'], ok['sufficient_for_current_policy']), (True, True, True, True))
        bad = dict(proj, statement=dict(st, outcome='passed', **{'class': 'analytical'}))
        self.assertFalse(self.c.post('/api/v1/verification/verify-statement', headers=self.H, json={'bundle': bad}).json()['signature_valid'])
        wrong = self.c.post('/api/v1/verification/verify-statement', headers=self.H, json={'bundle': proj, 'expected': {'target_job_id': 'j_other'}}).json()
        self.assertFalse(wrong['bindings_match'])
        vv = self.c.get('/api/v1/verification/' + samp['id'], headers=self.inst.h('viewer')).json()
        self.assertNotIn('challenge', vv); self.assertNotIn('checks', json.dumps(vv['result']))
        # a reviewer may request an audit but cannot create ordinary contracts
        rv = self.request(jb, 'analytical', headers=self.R)
        self.assertEqual(rv['state'], 'passed')
        self.assertEqual(self.c.post('/api/v1/contracts', headers=self.R, json={'kind': 'temporal_batch', 'title': 'x', 'inputs': batch_spec(), 'policy': {}}).status_code, 403)
        # verification gate bound in the contract policy: review is refused until a passing audit of that class exists
        r = self.c.post('/api/v1/contracts', headers=self.H, json={'kind': 'temporal_batch', 'title': 'gated', 'inputs': batch_spec(private_label='GATED'), 'policy': {'reviewer_id': self.inst.ids['reviewer'], 'required_verification': 'full_exact'}})
        self.assertEqual(r.status_code, 201, r.text); gcid = r.json()['id']
        self.c.post('/api/v1/contracts/' + gcid + '/freeze', headers=self.H)
        gj = self.c.post('/api/v1/jobs', headers=self.H, json={'contract_id': gcid}).json()['id']
        self.assertEqual(self.w.run_once()[1], 'succeeded')
        refused = self.c.post('/api/v1/jobs/' + gj + '/review-request', headers=self.H)
        self.assertEqual((refused.status_code, refused.json()['detail']['code']), (409, 'awaiting_verification'))
        self.request(gj, 'sampled_reference', {'sample_count': 8})
        self.assertEqual(self.c.post('/api/v1/jobs/' + gj + '/review-request', headers=self.H).json()['detail']['code'], 'awaiting_verification')   # a sampled audit cannot substitute
        g_full = self.request(gj, 'full_exact'); self.assertEqual(g_full['state'], 'passed')
        self.assertEqual(self.c.post('/api/v1/jobs/' + gj + '/review-request', headers=self.H).status_code, 200)
        self.assertEqual(self.c.post('/api/v1/reviews/' + gj + '/decision', headers=self.R, json={'decision': 'accepted'}).status_code, 200)
        # corrupted outputs: (a) altered after commitment -> commitment check fails; (b) commitments rewritten too -> the reference catches the wrong row
        jc = self.inst.compute_job('temporal_batch', batch_spec(private_label='CORRUPT_A')); self.assertEqual(self.w.run_once()[1], 'succeeded')
        corrupt_output(self.inst, jc, flip_first_outcome)
        va = self.request(jc, 'full_exact')
        self.assertEqual((va['state'], va['result']['checks'][0]['check']), ('failed', 'output_commitment'))
        self.assertEqual(self.c.get('/api/v1/jobs/' + va['audit_job_id'], headers=self.H).json()['outcome'], 'AUDIT_FAILED')
        jd = self.inst.compute_job('temporal_batch', batch_spec(private_label='CORRUPT_B')); self.assertEqual(self.w.run_once()[1], 'succeeded')
        corrupt_output(self.inst, jd, flip_first_outcome, rewrite_vault=True)
        vb = self.request(jd, 'full_exact')
        self.assertEqual(vb['state'], 'failed'); self.assertEqual(vb['result']['checks'][0]['detail']['mismatches'][0]['index'], 0)
        vs = self.request(jd, 'sampled_reference', {'sample_count': 364})
        self.assertEqual(vs['state'], 'failed')
        van = self.request(jd, 'analytical')
        self.assertEqual(van['state'], 'failed', van['result'])     # a flipped outcome breaks outcome/margin consistency
        # gated contract with a corrupted result: the gate never opens
        r = self.c.post('/api/v1/contracts', headers=self.H, json={'kind': 'temporal_batch', 'title': 'gated2', 'inputs': batch_spec(private_label='GATED2'), 'policy': {'reviewer_id': self.inst.ids['reviewer'], 'required_verification': 'full_exact'}})
        gcid2 = r.json()['id']; self.c.post('/api/v1/contracts/' + gcid2 + '/freeze', headers=self.H)
        gj2 = self.c.post('/api/v1/jobs', headers=self.H, json={'contract_id': gcid2}).json()['id']; self.assertEqual(self.w.run_once()[1], 'succeeded')
        corrupt_output(self.inst, gj2, flip_first_outcome, rewrite_vault=True)
        self.assertEqual(self.request(gj2, 'full_exact')['state'], 'failed')
        self.assertEqual(self.c.post('/api/v1/jobs/' + gj2 + '/review-request', headers=self.H).json()['detail']['code'], 'awaiting_verification')
        # heat: analytical closed-form eigenmode, full scalar reference, sampled last step
        jh = self.inst.compute_job('heat_diffusion', heat_spec(nx=24, ny=24, steps=200, dt='0.00001', initial={'type': 'sine_mode', 'm': 1, 'n': 2, 'amplitude': '10'}, snapshots=1))
        self.assertEqual(self.w.run_once()[1], 'succeeded')
        ha = self.request(jh, 'analytical')
        self.assertEqual(ha['state'], 'passed'); self.assertIn('closed_form_eigenmode', [c['check'] for c in ha['result']['checks']])
        hf = self.request(jh, 'full_reference'); self.assertEqual(hf['state'], 'passed'); self.assertIn('whole trajectory', hf['result']['coverage'])
        hs = self.request(jh, 'sampled_reference'); self.assertEqual(hs['state'], 'passed'); self.assertIn('one transition', hs['result']['coverage'])
        big = self.inst.compute_job('heat_diffusion', heat_spec(nx=256, ny=256, steps=400)); self.assertEqual(self.w.run_once()[1], 'succeeded')
        self.assertEqual(self.c.post('/api/v1/verification', headers=self.H, json={'job_id': big, 'class': 'full_reference'}).json()['detail']['code'], 'verification_budget_exceeded')
        # Monte Carlo: audited samples re-evaluated; the statement names what was not regenerated
        jm = self.inst.compute_job('monte_carlo_reliability', mc_spec()); self.assertEqual(self.w.run_once()[1], 'succeeded')
        vm = self.request(jm, 'full_reference')
        self.assertEqual(vm['state'], 'passed'); self.assertIn('not independently regenerated', vm['result']['statement'])
        # replica on the other backend (cuda when available): byte-identical results pass; a tampered replica is disputed and resolved administratively
        if HAVE_CUDA:
            rep = self.c.post('/api/v1/verification', headers=self.H, json={'job_id': jb, 'class': 'replica'})
            self.assertEqual(rep.status_code, 202, rep.text); rid = rep.json()['id']
            self.assertEqual(rep.json()['state'], 'awaiting_replica')
            self.assertEqual(self.w.run_once()[1], 'succeeded'); self.w.tick_workflows()
            rv = self.c.get('/api/v1/verification/' + rid, headers=self.H).json()
            self.assertEqual(rv['state'], 'passed', rv); self.assertEqual(rv['result']['replica']['backend'], 'cuda'); self.assertEqual(rv['result']['original']['backend'], 'cpu')
            rep2 = self.c.post('/api/v1/verification', headers=self.H, json={'job_id': jb, 'class': 'replica'}).json()
            self.assertEqual(self.w.run_once()[1], 'succeeded')
            corrupt_output(self.inst, rep2['replica_job_id'], flip_first_outcome, rewrite_vault=True)
            self.w.tick_workflows()
            dv = self.c.get('/api/v1/verification/' + rep2['id'], headers=self.H).json()
            self.assertEqual(dv['state'], 'disputed'); self.assertIsNotNone(dv['result']['discrepancy'])
            self.assertEqual(self.c.post('/api/v1/verification/' + rep2['id'] + '/resolve', headers=self.H, json={'decision': 'original_accepted'}).status_code, 403)
            res = self.c.post('/api/v1/verification/' + rep2['id'] + '/resolve', headers=self.R, json={'decision': 'original_accepted', 'note': 'replica output altered in test'}).json()
            self.assertEqual((res['state'], res['resolution']['decision']), ('resolved', 'original_accepted'))
        items = self.c.get('/api/v1/verification?job_id=' + jb, headers=self.H).json()['items']
        self.assertGreaterEqual(len(items), 4)
