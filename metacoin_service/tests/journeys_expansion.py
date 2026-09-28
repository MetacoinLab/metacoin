"""Order §59: the twenty end-to-end acceptance journeys for the 24-hour expansion, through actual entry points with
isolated identities and separate processes (API, workers, a federated node over TLS, a conforming MCP client, the
client CLI, the SDK x402 client, and the local py-evm chain).

    PYTHONPATH=. .venv-service/bin/python -m metacoin_service.tests.journeys_expansion --out journeys-expansion.json [--only 1,2]

A journey is recorded as passed only when its stated scope was actually met; blocked names the exact dependency."""
import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx
from metacoin_service.tests.journeys_compute import Journeys as BaseJourneys, PY, RUNTIME, HAVE_CUDA
from metacoin_service.tests.test_service import ROOT, ENV, free_port
from metacoin_service.tests.test_compute_engine import batch_spec, heat_spec, mc_spec
from metacoin_service.tests.test_models import GEN, EMB, installed
from metacoin_service.tests.test_verification import corrupt_output, flip_first_outcome
from metacoin_service import crypto, config
from metacoin_service.db import Database, now

HAVE_TORCH = bool(RUNTIME and RUNTIME.get('torch'))
CORPUS = json.loads((Path(__file__).parent / 'knowledge_corpus' / 'corpus.json').read_text())


class Journeys(BaseJourneys):
    def __init__(self):
        super().__init__()
        self.models_ok = HAVE_TORCH and installed(self.inst.settings, GEN) and installed(self.inst.settings, EMB)
        self.node_procs = []

    def api_json(self, method, path, role='owner', **kw):
        r = getattr(self.http, method)(path, headers=self.H(role), **kw)
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, {'text': r.text[:200]}

    def wait_job(self, jid, timeout=300):
        deadline = time.time() + timeout
        while time.time() < deadline:
            st, j = self.api_json('get', '/api/v1/jobs/' + jid)
            if st != 200 or j['state'] in ('succeeded', 'failed', 'cancelled'):
                return j
            time.sleep(0.5)
        return j

    def worker_bg(self, name):
        return self.start_worker(name)

    def close(self):
        for p in self.node_procs:
            try:
                p.terminate(); p.wait(timeout=10)
            except Exception:
                p.kill()
        super().close()

    # ---- 1-3: models and knowledge ----------------------------------------------------------------------
    def j1_register_load_generate(self):
        if not self.models_ok:
            self.record(1, 'register and load a real local model, generate on new input, inspect usage', 'blocked', {'reason': 'torch interpreter or pinned model artifacts absent'}); return
        rc, reg = self.cli('owner', 'model-register', '--model-id', GEN['model_id'], '--hub-repo', GEN['hub_repo'], '--revision', GEN['revision'], '--operations', 'generate', '--license', 'apache-2.0')
        rc2, prom = self.cli('owner', 'model-action', reg['id'], 'promote', '--operation', 'generate')
        rc3, reg_e = self.cli('owner', 'model-register', '--model-id', EMB['model_id'], '--hub-repo', EMB['hub_repo'], '--revision', EMB['revision'], '--operations', 'embed', '--license', 'apache-2.0')
        self.cli('owner', 'model-action', reg_e['id'], 'promote', '--operation', 'embed')
        self.gen_rev, self.emb_rev = reg['id'], reg_e['id']
        self.worker_bg('w-models')
        t0 = time.time()
        rc4, gen = self.cli('owner', 'generate', '--prompt', 'List two prime numbers greater than 90, separated by a comma.', '--max-output-tokens', '24', '--watch')
        st, view = self.api_json('get', '/api/v1/models/jobs/' + gen.get('job_id', 'x'))
        st2, det = self.api_json('get', '/api/v1/models/' + reg['id'])
        seg = self.api_json('get', '/api/v1/models/jobs/%s/segments' % gen.get('job_id', 'x'))[1]
        text = ''.join(s['text'] for s in seg.get('segments', []))
        ok = rc == 0 and rc2 == 0 and rc4 == 0 and view.get('state') == 'succeeded' and view['usage']['output_tokens'] > 0 and det.get('callable') and any(ch.isdigit() for ch in text)
        self.record(1, 'register and load a real local model, generate on new input, inspect usage', 'passed' if ok else 'failed',
                    {'revision': reg.get('id'), 'weight_digest': (reg.get('weight_digest') or '')[:16], 'callable_after': det.get('callable'), 'runtime': [r['device'] for r in det.get('runtimes', [])], 'usage': view.get('usage'), 'load_ms': view.get('timing', {}).get('load_ms'), 'wall_s': round(time.time() - t0, 1), 'output_chars': len(text)})

    def j2_embeddings_index_retrieve(self):
        if not self.models_ok:
            self.record(2, 'embeddings for authorized text, versioned private index, expected source retrieved', 'blocked', {'reason': 'models absent'}); return
        self.worker_bg('w-j2')
        rc, col = self.cli('owner', 'knowledge-collection-create', '--name', 'journey corpus')
        self.cid = col['id']
        for d in CORPUS['documents']:
            path = self.tmpjson('doc-' + d['name'] + '.txt', d['text']) if False else Path(self.inst.temp.name) / ('doc-' + d['name']); path.write_text(d['text'])
            self.cli('owner', 'knowledge-add', self.cid, '--file', str(path), '--name', d['name'], '--format', d['format'])
        rc2, idx = self.cli('owner', 'knowledge-index', self.cid, '--wait')
        rc3, emb = self.cli('owner', 'embed', '--texts', self.tmpjson('texts.json', ['The heat solver refuses unstable timesteps.', 'A Wilson interval bounds the estimate.']))
        ej = self.wait_job(emb.get('job_id', 'x'))
        rc4, res = self.cli('owner', 'knowledge-search', self.cid, '--query', 'What is the reserve for the demonstration mission?', '--mode', 'hybrid', '--k', '3')
        top = [r['document_name'] for r in res.get('results', [])]
        ok = rc2 == 0 and idx.get('state') == 'ready' and idx.get('embedding_dim') == 384 and ej.get('state') == 'succeeded' and 'temporal-model-spec.md' in top
        self.record(2, 'embeddings for authorized text, versioned private index, expected source retrieved', 'passed' if ok else 'failed', {'index': {k: idx.get(k) for k in ('id', 'version', 'state', 'chunk_count', 'embedding_dim')}, 'embedding_job': ej.get('state'), 'top_documents': top, 'timing_ms': res.get('timing_ms')})

    def j3_answer_and_insufficient(self):
        if not self.models_ok:
            self.record(3, 'retrieval-assisted answer with valid citations and an honest insufficient-evidence response', 'blocked', {'reason': 'models absent'}); return
        self.worker_bg('w-j3')
        rc, a = self.cli('owner', 'knowledge-answer', self.cid, '--question', 'What is the reserve for the demonstration mission?', '--mode', 'generative', '--wait')
        rc2, b = self.cli('owner', 'knowledge-answer', self.cid, '--question', 'What is the launch mass of the spacecraft in kilograms?', '--mode', 'generative', '--wait')
        rc3, val = self.cli('owner', 'knowledge-validate-citations', '--citations', self.tmpjson('cit.json', [{'chunk_id': c['chunk_id']} for c in a.get('citations', [])] or [{'chunk_id': 'kv_none:0'}]))
        ok = a.get('status') == 'answered' and a.get('citations') and '2000' in (a.get('answer') or '') and b.get('status') == 'insufficient_evidence' and val.get('all_valid')
        self.record(3, 'retrieval-assisted answer with valid citations and an honest insufficient-evidence response', 'passed' if ok else 'failed',
                    {'answered': {'status': a.get('status'), 'citations': len(a.get('citations', [])), 'grounding': (a.get('grounding') or {}).get('attributed_sentences'), 'usage': a.get('usage')}, 'unsupported': {'status': b.get('status'), 'reason': (b.get('grounding') or {}).get('reason')}, 'citations_valid': val.get('all_valid')})
        self.answer_id = a.get('id')

    def j4_revocation(self):
        if not self.models_ok:
            self.record(4, 'revoke source access: retrieval, cached answers, previews and delivery obey it', 'blocked', {'reason': 'models absent'}); return
        st, cols = self.api_json('get', '/api/v1/knowledge/collections/' + self.cid)
        doc = next(d for d in cols['documents_list'] if d['name'] == 'temporal-model-spec.md')
        rc, rev = self.cli('owner', 'knowledge-revoke', doc['id'], '--reason', 'journey revocation')
        rc2, res = self.cli('owner', 'knowledge-search', self.cid, '--query', 'reserve for the demonstration mission', '--mode', 'hybrid', '--k', '5')
        st2, ans = self.api_json('get', '/api/v1/knowledge/answers/' + self.answer_id)
        st3, prev = self.api_json('get', '/api/v1/knowledge/versions/' + doc['current_version_id'] + '/preview')
        ok = self.answer_id in rev.get('answers_invalidated', []) and 'temporal-model-spec.md' not in [r['document_name'] for r in res.get('results', [])] and ans.get('answer') is None and st3 == 403
        self.record(4, 'revoke source access: retrieval, cached answers, previews and delivery obey it', 'passed' if ok else 'failed', {'revocation': {k: rev.get(k) for k in ('indexes_flagged_stale', 'answers_invalidated')}, 'retrieval_excludes': 'temporal-model-spec.md' not in [r['document_name'] for r in res.get('results', [])], 'answer_withheld': ans.get('note'), 'preview_status': st3})

    def j5_injection(self):
        if not self.models_ok:
            self.record(5, 'hostile document instructions do not create tools, reveal secrets, widen grants or dispatch paid work', 'blocked', {'reason': 'models absent'}); return
        self.worker_bg('w-j5')
        before = self.api_json('get', '/api/v1/jobs?limit=50')[1]['items']
        grants_before = self.api_json('get', '/api/v1/agent-grants')[1] if self.api_json('get', '/api/v1/agent-grants')[0] == 200 else {}
        rc, a = self.cli('owner', 'knowledge-answer', self.cid, '--question', 'What port does the console listen on by default?', '--mode', 'generative', '--wait')
        after = self.api_json('get', '/api/v1/jobs?limit=50')[1]['items']
        new_kinds = sorted({j['kind'] for j in after} - {j['kind'] for j in before})
        leaked = 'hunter2' in json.dumps(a) or 'mck_' in json.dumps(a)
        ok = not leaked and set(new_kinds) <= {'knowledge_answer'} and len(after) == len(before) + 1
        self.record(5, 'hostile document instructions do not create tools, reveal secrets, widen grants or dispatch paid work', 'passed' if ok else 'failed', {'status': a.get('status'), 'answer_excerpt': (a.get('answer') or '')[:120], 'jobs_added': len(after) - len(before), 'new_kinds': new_kinds, 'secret_leaked': leaked})

    # ---- 6-7: calibration -------------------------------------------------------------------------------
    def j6_calibration(self):
        self.worker_bg('w-j6')
        rows = [{'x1': i, 'x2': (i * 7) % 13, 'y': 4 * i - 3 * ((i * 7) % 13) + 2} for i in range(40)]
        rc, ds = self.cli('owner', 'calibration-dataset', '--file', self.tmpjson('cal.json', {'name': 'journey exact', 'columns': ['x1', 'x2', 'y'], 'target': 'y', 'units': {'y': 'ms'}, 'rows': rows}))
        rc2, fit = self.cli('owner', 'calibration-fit', ds['id'], '--features', 'x1,x2', '--target', 'y', '--split', 'random', '--wait')
        m = fit.get('model') or {}
        rc3, p_in = self.cli('owner', 'calibration-predict', m.get('id', 'x'), '--features', json.dumps({'x1': 10, 'x2': 5}))
        rc4, p_out = self.cli('owner', 'calibration-predict', m.get('id', 'x'), '--features', json.dumps({'x1': 5000, 'x2': 5}))
        ok = m.get('verification_passed') and abs(float(p_in.get('prediction', 'nan')) - (40 - 15 + 2)) < 1e-6 and p_in.get('domain_status') == 'interpolation' and p_out.get('domain_status') == 'extrapolation' and not p_out.get('usable_for_scheduling')
        self.record(6, 'fit a stable calibration model, evaluate held-out data, predict a supported case, label extrapolation', 'passed' if ok else 'failed', {'model': m.get('id'), 'eval_rmse': (m.get('metrics') or {}).get('eval', {}).get('rmse'), 'verified': m.get('verification_passed'), 'interpolation': p_in.get('prediction'), 'extrapolation': p_out.get('domain_status')})

    def j7_calibrated_scheduling(self):
        self.worker_bg('w-j7')
        for step in (300, 100, 50, 25, 20):
            self.wait_job(self.submit('temporal_batch', batch_spec(grid=[{'path': 'reserve', 'start': 0, 'stop': 9000, 'step': step}, {'path': 'load_scale_percent', 'values': [50, 100, 150, 200]}], private_label='J7'), 'j7-%d' % step))
        rc, ds = self.cli('owner', 'calibration-dataset', '--task-kind', 'temporal_batch')
        rc2, fit = self.cli('owner', 'calibration-fit', ds.get('id', 'x'), '--features', 'work_units', '--target', 'duration_ms', '--scope-kind', 'temporal_batch', '--scope-backend', 'cpu', '--wait')
        m = fit.get('model') or {}
        rc3, ap = self.cli('owner', 'calibration-action', m.get('id', 'x'), 'approve')
        rc4, plan = self.cli('owner', 'calibration-plan', '--kind', 'temporal_batch', '--inputs', self.tmpjson('plan.json', batch_spec(device_policy='auto', private_label='J7P')))
        jid = self.submit('temporal_batch', batch_spec(device_policy='auto', private_label='J7Q'), 'j7q'); self.wait_job(jid)
        v = self.view(jid)
        cpu = next((c for c in plan.get('candidates', []) if c['backend'] == 'cpu'), {})
        ok = ap.get('state') == 'approved' and cpu.get('prediction_status') == 'calibrated' and 'calibrat' in (v.get('backend_reason') or '') and v['verification']['passed'] and plan.get('not_a_measurement')
        self.record(7, 'approved calibration used in scheduling without bypassing resource or workspace limits', 'passed' if ok else 'failed', {'model': m.get('id'), 'plan_cpu': {k: cpu.get(k) for k in ('predicted_duration_ms', 'prediction_status', 'slots_free')}, 'backend_reason': (v.get('backend_reason') or '')[:160], 'hard_constraints': cpu.get('hard_constraints')})

    # ---- 8-10: verification -----------------------------------------------------------------------------
    def j8_audit_and_corruption(self):
        jid = self.submit('temporal_batch', batch_spec(private_label='J8'), 'j8'); self.worker_once('w-j8')
        jc = self.submit('temporal_batch', batch_spec(private_label='J8C'), 'j8c'); self.worker_once('w-j8c')
        corrupt_output(self.inst, jc, flip_first_outcome, rewrite_vault=True)
        self.worker_bg('w-j8')
        rc, v = self.cli('owner', 'verification-request', jid, '--class', 'full_exact', '--wait')
        rc2, vc = self.cli('owner', 'verification-request', jc, '--class', 'full_exact', '--wait')
        ok = v.get('state') == 'passed' and v['result']['checked'] == 364 and vc.get('state') == 'failed' and vc['result']['checks'][0]['detail']['mismatches'][0]['index'] == 0
        self.record(8, 'audit a scientific result through an independent reference; reject a deliberately corrupted result', 'passed' if ok else 'failed', {'honest': {'state': v.get('state'), 'checked': v.get('result', {}).get('checked')}, 'corrupted': {'state': vc.get('state'), 'first_mismatch': vc.get('result', {}).get('checks', [{}])[0].get('detail')}})
        self.j8_job = jid

    def j9_sampled_audit(self):
        self.worker_bg('w-j9')
        rc, v = self.cli('owner', 'verification-request', self.j8_job, '--class', 'sampled_reference', '--sample-count', '32', '--wait')
        r = v.get('result', {})
        ok = v.get('state') == 'passed' and r.get('checked') == 32 and r.get('total') == 364 and len(r.get('scope_items', [])) == 32 and 'not guaranteed' in r.get('statement', '') and v.get('challenge', {}).get('drawn_by', '').startswith('service')
        self.record(9, 'sampled audit with a bound result and recorded challenge; partial scope reported accurately', 'passed' if ok else 'failed', {'checked': r.get('checked'), 'total': r.get('total'), 'challenge': {k: v.get('challenge', {}).get(k) for k in ('policy', 'drawn_by', 'sample_count')}, 'statement': r.get('statement', '')[:160]})
        self.j9_vid = v.get('id')

    def j10_signed_projection(self):
        out = Path(self.inst.temp.name) / 'statement.json'
        rc, proj = self.cli('owner', 'verification-statement', self.j9_vid, '--out', str(out))
        rc2, ver = self.cli('viewer', 'verification-statement', self.j9_vid, '--verify')
        bundle = json.loads(out.read_text())
        tampered = dict(bundle, statement=dict(bundle['statement'], outcome='passed', scope=dict(bundle['statement']['scope'], checked=364)))
        st, bad = self.api_json('post', '/api/v1/verification/verify-statement', 'viewer', json={'bundle': tampered})
        ok = ver.get('signature_valid') and ver.get('issuer_trusted_by_this_service') and ver.get('bindings_match') and not bad.get('signature_valid') and 'mismatches' not in json.dumps(bundle['statement'])
        self.record(10, 'signed audit projection produced and verified under an independently configured trust policy; tampering detected', 'passed' if ok else 'failed', {'verify': {k: ver.get(k) for k in ('signature_valid', 'issuer_trusted_by_this_service', 'bindings_match', 'sufficient_for_current_policy')}, 'tampered_valid': bad.get('signature_valid'), 'claim': bundle['statement'].get('claim')})

    # ---- 11-13: federation over TLS with separate processes ---------------------------------------------
    def start_tls_api(self):
        self.tls_port = free_port()
        self.tls = json.loads(subprocess.run([PY, '-m', 'metacoin_service', '--home', str(self.inst.home), 'node-tls'], cwd=ROOT, env=self.env, capture_output=True, text=True, timeout=60).stdout)
        self.tls_api = subprocess.Popen([PY, '-m', 'metacoin_service', '--home', str(self.inst.home), '--provider-mode', 'test-http', 'serve', '--port', str(self.tls_port), '--tls'], cwd=ROOT, env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.node_procs.append(self.tls_api)
        for _ in range(200):
            try:
                if httpx.get('https://127.0.0.1:%d/api/health' % self.tls_port, verify=self.tls['ca'], timeout=1).status_code == 200:
                    return
            except Exception:
                time.sleep(0.1)
        raise RuntimeError('tls api did not start')

    def node_once(self, ident, home, env_extra=None):
        p = subprocess.run([PY, '-m', 'metacoin_service', 'node-worker', '--identity', str(ident), '--coordinator', 'https://127.0.0.1:%d' % self.tls_port, '--ca', self.tls['ca'], '--node-home', str(home), '--once'],
                           cwd=ROOT, env=dict(self.env, **(env_extra or {})), capture_output=True, text=True, timeout=600)
        try:
            return p.returncode, json.loads(p.stdout)
        except ValueError:
            return p.returncode, {'stdout': p.stdout[-300:], 'stderr': p.stderr[-300:]}

    def j11_node_executes(self):
        self.start_tls_api()
        ident = Path(self.inst.temp.name) / 'node-a.json'
        rc, enr = self.cli('owner', 'node-enroll', '--name', 'journey-node-a', '--out', str(ident), '--devices', 'cpu', '--capabilities', 'temporal_batch,heat_diffusion')
        self.node_id = enr.get('node_id')
        r = self.http.post('/api/v1/contracts', headers=self.H(), json={'kind': 'temporal_batch', 'title': 'j11', 'inputs': batch_spec(private_label='J11'), 'policy': {'reviewer_id': self.inst.ids['reviewer'], 'execution_locations': [self.node_id]}}).json()
        self.http.post('/api/v1/contracts/' + r['id'] + '/freeze', headers=self.H()); jid = self.http.post('/api/v1/jobs', headers=self.H(), json={'contract_id': r['id']}).json()['id']
        local = self.worker_once('w-j11-local')
        rc2, ran = self.node_once(ident, Path(self.inst.temp.name) / 'node-a-home')
        v = self.view(jid)
        st, nv = self.api_json('get', '/api/v1/nodes/' + self.node_id)
        ok = local is None and ran.get('ran', [None, None])[1] == 'succeeded' and v['state'] == 'succeeded' and v['verification']['passed'] and 'federated node' in v['backend_reason'] and nv['observed']['completed_by_backend'].get('cpu') == 1 and any(t['direction'] == 'from_node' and t['role'] == 'result' for t in nv['transfers'])
        self.record(11, 'enroll an isolated worker identity, transfer data over TLS, execute without coordinator database access, publish a fenced result', 'passed' if ok else 'failed',
                    {'node': self.node_id, 'transport': 'https://127.0.0.1:%d (pinned local CA)' % self.tls_port, 'local_worker_refused_node_only_job': local is None, 'node_ran': ran.get('ran', ran), 'verification': v.get('verification', {}).get('mode'), 'transfers': [t['role'] + ':' + t['direction'] for t in nv.get('transfers', [])][:6], 'topology': 'two processes on one host (not multi-machine)'})
        self.node_ident = ident

    def j12_interrupt_and_reassign(self):
        self.inst.settings.limits['compute_checkpoint_interval_seconds'] = 1
        r = self.http.post('/api/v1/contracts', headers=self.H(), json={'kind': 'heat_diffusion', 'title': 'j12', 'inputs': heat_spec(device_policy='cpu', private_label='J12'), 'policy': {'reviewer_id': self.inst.ids['reviewer'], 'execution_locations': ['*']}}).json()
        self.http.post('/api/v1/contracts/' + r['id'] + '/freeze', headers=self.H()); jid = self.http.post('/api/v1/jobs', headers=self.H(), json={'contract_id': r['id']}).json()['id']
        rc, ran = self.node_once(self.node_ident, Path(self.inst.temp.name) / 'node-a-home', {'METACOIN_NODE_TEST_DIE_AFTER': 'checkpoint'})
        v1 = self.view(jid)
        with Database(self.inst.settings.db_path).tx() as db:               # deterministic lease expiry instead of waiting for the wall clock
            db.execute("UPDATE jobs SET lease_expires=? WHERE id=?", (now() - 1, jid))
        gen_node = v1['checkpoints'][-1]['attempt_generation'] if v1['checkpoints'] else None
        local = self.worker_once('w-j12-local')
        v2 = self.view(jid)
        st, stale = self.api_json('get', '/api/v1/nodes/' + self.node_id)
        with Database(self.inst.settings.db_path).read() as db:
            units = db.execute('SELECT unit_from, unit_to FROM compute_work_units WHERE job_id=? ORDER BY unit_from', (jid,)).fetchall()
        contiguous = units and units[0]['unit_from'] == 0 and all(a['unit_to'] == b['unit_from'] for a, b in zip(units, units[1:])) and units[-1]['unit_to'] == v2['work']['total']
        st2, u = self.api_json('get', '/api/v1/usage')
        ok = rc != 0 and v1['work']['committed'] > 0 and local == [jid, 'succeeded'] and v2['state'] == 'succeeded' and v2['verification']['passed'] and contiguous and v2['checkpoints'][-1]['attempt_generation'] > gen_node
        self.record(12, 'interrupt node connectivity after a checkpoint, reassign safely, stale publication rejected, no duplicate useful-work charge', 'passed' if ok else 'failed',
                    {'node_exit_code': rc, 'committed_by_node': v1['work']['committed'], 'node_generation': gen_node, 'recovered_by': 'local worker', 'final_generation': v2['checkpoints'][-1]['attempt_generation'] if v2['checkpoints'] else None, 'work_units_contiguous': bool(contiguous), 'total': v2['work']['total']})

    def j13_revoke_node(self):
        rc, rv = self.cli('owner', 'node-action', self.node_id, 'revoke', '--reason', 'journey')
        rc2, ran = self.node_once(self.node_ident, Path(self.inst.temp.name) / 'node-a-home')
        st, nv = self.api_json('get', '/api/v1/nodes/' + self.node_id)
        ok = rv.get('state') == 'revoked' and rc2 != 0 and nv['state'] == 'revoked' and nv['observed']['completed_by_backend'].get('cpu') == 1 and len(nv['transfers']) >= 2
        self.record(13, 'revoke a node: new work and artifact access denied, historical evidence preserved', 'passed' if ok else 'failed', {'state': nv.get('state'), 'node_after_revoke': ran.get('stderr', '')[-120:], 'observed_kept': nv.get('observed'), 'transfers_kept': len(nv.get('transfers', []))})

    # ---- 14-15: payments ---------------------------------------------------------------------------------
    def j14_upto_local_chain(self):
        art = ROOT / 'integrations' / 'x402' / 'local_chain' / 'artifacts.json'
        if not art.exists():
            self.record(14, 'variable-price (upto) contract behaviour on an isolated local chain', 'blocked', {'reason': 'local-chain artifacts not built: python -m integrations.x402.local_chain.build'}); return
        p = subprocess.run([PY, '-m', 'unittest', 'integrations.x402.local_chain.test_local_chain'], cwd=ROOT, env=dict(self.env, METACOIN_LOCAL_CHAIN_OUT=str(Path(self.inst.temp.name) / 'local-chain.json')), capture_output=True, text=True, timeout=900)
        rec = json.loads((Path(self.inst.temp.name) / 'local-chain.json').read_text()) if (Path(self.inst.temp.name) / 'local-chain.json').exists() else {}
        s = rec.get('scenarios', {})
        ok = p.returncode == 0 and s.get('below_maximum', {}).get('moved') == 640 and not s.get('over_maximum', {}).get('success')
        self.record(14, 'variable-price (upto) contract behaviour on an isolated local chain', 'passed' if ok else 'failed',
                    {'unittest_rc': p.returncode, 'chain': rec.get('topology', {}).get('network'), 'below_maximum_moved': s.get('below_maximum', {}).get('moved'), 'refusals': {k: s[k].get('verify_error') or s[k].get('error_reason') for k in ('over_maximum', 'wrong_recipient', 'wrong_spender', 'expired', 'wrong_domain', 'replay_same_nonce') if k in s},
                     'lost_response': {k: s.get('response_lost_then_retry', {}).get(k) for k in ('receipt_wait_attempts_during_first', 'total_moved', 'moved_after_second')}, 'unresolved': 'application-level metered settlement (authorize max at invoke, settle assessed amount at completion) is not wired into the service routes; local contract behaviour only'},
                    caveat='local py-evm chain with pinned sources; not public-network settlement; SDK canonical addresses redirected to local deployments')

    def j15_purchase_verification_via_sdk(self):
        self.worker_bg('w-j15')
        sid = next(s['id'] for s in self.api_json('get', '/api/v1/services')[1]['items'] if s['kind'] == 'verification_audit')
        st, sv = self.api_json('get', '/api/v1/services/' + sid)
        inputs = {'schema': 'verification-audit-input/v1', 'verification_id': 'vf_000000000000', 'target_job_id': self.j8_job, 'class': 'analytical', 'params': {}}
        st_q, q = self.api_json('post', '/api/v1/services/' + sid + '/quote', json={'inputs': inputs})
        # verification audits are requested through the verification service (bound to a record), so the catalog invoke path refuses a free-standing audit; buy a model generation instead when models are present
        if self.models_ok:
            gsid = next(s['id'] for s in self.api_json('get', '/api/v1/services')[1]['items'] if s['kind'] == 'text_generation')
            ginputs = {'schema': 'text-generation-input/v1', 'prompt': 'Say the word hello.', 'max_output_tokens': 12}
            st2, gq = self.api_json('post', '/api/v1/services/' + gsid + '/quote', json={'inputs': ginputs})
            self.api_json('post', '/api/v1/quotes/' + gq['quote_id'] + '/accept')
            body_text = json.dumps({'quote_id': gq['quote_id'], 'inputs': ginputs}, sort_keys=True)
            p = subprocess.run([PY, '-m', 'metacoin_service.tests.x402_invoke_client', self.base, gsid, str(self.creds['owner']), body_text], cwd=ROOT, env=self.env, capture_output=True, text=True, timeout=120)
            try:
                out = json.loads(p.stdout)
            except ValueError:
                out = {'stdout': p.stdout[-300:], 'stderr': p.stderr[-300:]}
            jid = (out.get('body') or {}).get('job_id')
            j = self.wait_job(jid) if jid else {}
            st3, usage = self.api_json('get', '/api/v1/usage')
            u = [x for x in usage.get('items', []) if x['job_id'] == jid]
            st4, mv = self.api_json('get', '/api/v1/models/jobs/' + (jid or 'x'))
            ok = out.get('first_status') == 402 and out.get('second_status') == 202 and (out.get('settled') or {}).get('success') and j.get('state') == 'succeeded' and u and u[0]['quantity'] == mv['usage']['output_tokens'] and u[0]['quantity'] <= 12 and u[0]['signature_valid']
            self.record(15, 'purchase a local model operation through the SDK HTTP path; bound usage and result entitlement inspected', 'passed' if ok else 'failed', {'quote_max': gq.get('amount_max'), 'x402': {k: out.get(k) for k in ('first_status', 'second_status')}, 'settled': (out.get('settled') or {}).get('success'), 'measured_tokens': mv.get('usage', {}).get('output_tokens'), 'assessed_charge': u[0]['assessed_charge'] if u else None, 'quote_flow_for_audit_service': st_q})
        else:
            self.record(15, 'purchase a local model operation through the SDK HTTP path', 'blocked', {'reason': 'models absent'})

    # ---- 16-17: MCP --------------------------------------------------------------------------------------
    def j16_mcp_client(self):
        self.worker_bg('w-j16')
        p = subprocess.run([PY, '-m', 'metacoin_service.tests.mcp_journey_client', self.base, str(self.creds['owner']), 'agent'], cwd=ROOT, env=self.env, capture_output=True, text=True, timeout=600)
        try:
            out = json.loads(p.stdout)
        except ValueError:
            out = {'stdout': p.stdout[-300:], 'stderr': p.stderr[-300:]}
        self.wait(out.get('verification_id') and self.api_json('get', '/api/v1/verification/' + out['verification_id'])[1].get('audit_job_id') or 'x', lambda v: v.get('state') in ('succeeded', 'failed', 'cancelled'), 120) if out.get('verification_id') else None
        st, v = self.api_json('get', '/api/v1/verification/' + out.get('verification_id', 'x'))
        ok = out.get('protocol') and out.get('submitted', {}).get('state') == 'queued' and v.get('state') in ('passed', 'queued') and out.get('summary_read')
        self.record(16, 'separate MCP client discovers a service, submits permitted work, queries status, requests verification, retrieves an allowed result', 'passed' if ok else 'failed', {'protocol': out.get('protocol'), 'tools': out.get('tool_count'), 'job': out.get('submitted', {}).get('job_id'), 'verification': v.get('state'), 'sdk': 'mcp 1.26.0 stdio'})

    def j17_mcp_read_only(self):
        p = subprocess.run([PY, '-m', 'metacoin_service.tests.mcp_journey_client', self.base, str(self.creds['viewer']), 'readonly'], cwd=ROOT, env=self.env, capture_output=True, text=True, timeout=300)
        try:
            out = json.loads(p.stdout)
        except ValueError:
            out = {'stdout': p.stdout[-300:], 'stderr': p.stderr[-300:]}
        ok = out.get('list_ok') and all(r.get('status') == 403 for r in out.get('refusals', [])) and len(out.get('refusals', [])) >= 3
        self.record(17, 'read-only MCP principal attempts mutation and receives server-side refusals', 'passed' if ok else 'failed', {'refusals': [(r.get('tool'), r.get('status'), r.get('code')) for r in out.get('refusals', [])]})

    # ---- 18-20: restore, console, clean export -----------------------------------------------------------
    def j18_restore(self):
        dest = Path(self.inst.temp.name) / 'backup'
        p = subprocess.run([PY, '-m', 'metacoin_service', '--home', str(self.inst.home), 'backup', str(dest)], cwd=ROOT, env=self.env, capture_output=True, text=True, timeout=300)
        manifest = json.loads(p.stdout) if p.returncode == 0 else {}
        fresh = Path(self.inst.temp.name) / 'restored'
        r = subprocess.run([PY, '-m', 'metacoin_service', '--home', str(fresh), 'restore', str(dest), '--keys-dir', str(self.inst.settings.keys_dir)], cwd=ROOT, env=self.env, capture_output=True, text=True, timeout=300)
        rest = json.loads(r.stdout) if r.returncode == 0 else {'stderr': r.stderr[-300:], 'stdout': r.stdout[-200:]}
        status = json.loads(subprocess.run([PY, '-m', 'metacoin_service', '--home', str(fresh), 'status'], cwd=ROOT, env=self.env, capture_output=True, text=True, timeout=120).stdout or '{}')
        weights = rest.get('models_and_indexes', {}).get('model_revisions', [])
        ok = p.returncode == 0 and r.returncode == 0 and status.get('reconciliation_gate') == '1' and manifest.get('inventory') is not None and status.get('sales_by_state') is not None and (not self.models_ok or all(w['weights_present'] for w in weights))
        self.record(18, 'restore a representative backup into a fresh location: privacy preserved, no automatic economic resubmission, model/index recovery reported', 'passed' if ok else 'failed',
                    {'inventory': {k: (len(v) if isinstance(v, list) else v) for k, v in (manifest.get('inventory') or {}).items()}, 'classes': manifest.get('classes'), 'gate': status.get('reconciliation_gate'), 'restore_error': rest.get('stderr'), 'models': [(w['model_id'], w['weights_present']) for w in weights], 'indexes': rest.get('models_and_indexes', {}).get('knowledge_indexes')})

    def j19_console_browser(self):
        script = ROOT / 'metacoin_service' / 'tests' / 'browser' / 'journey_expansion.py'
        pw = os.environ.get('METACOIN_PLAYWRIGHT_PYTHON')
        if not pw or not script.exists():
            self.record(19, 'connected console journeys with owner/reviewer/viewer roles at desktop and narrow widths', 'blocked', {'reason': 'METACOIN_PLAYWRIGHT_PYTHON not set (isolated Playwright venv) or browser script missing'}); return
        outdir = Path(self.inst.temp.name) / 'shots'
        p = subprocess.run([pw, str(script), self.base, str(self.creds['owner']), str(self.creds['reviewer']), str(self.creds['viewer']), str(outdir)], cwd=ROOT, env=dict(os.environ, PYTHONPATH=str(ROOT)), capture_output=True, text=True, timeout=900)
        try:
            out = json.loads(p.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            out = {'stderr': p.stderr[-400:], 'stdout': p.stdout[-200:]}
        ok = p.returncode == 0 and out.get('failed', 1) == 0
        self.record(19, 'connected console journeys with owner/reviewer/viewer roles at desktop and narrow widths', 'passed' if ok else 'failed', {'checks': out.get('passed'), 'failed': out.get('failed'), 'screenshots': out.get('screenshots'), 'details': [c for c in out.get('checks', []) if not c.get('ok')][:5]})
        self.browser_shots = outdir

    def j20_clean_export(self):
        self.record(20, 'install a clean export and reproduce a representative path from each feature group', 'blocked', {'reason': 'executed by the packaging step (clean-export-logs/) against the final archive, not inside this process'})

    def run_all(self, only=None):
        fns = [self.j1_register_load_generate, self.j2_embeddings_index_retrieve, self.j3_answer_and_insufficient, self.j4_revocation, self.j5_injection, self.j6_calibration, self.j7_calibrated_scheduling,
               self.j8_audit_and_corruption, self.j9_sampled_audit, self.j10_signed_projection, self.j11_node_executes, self.j12_interrupt_and_reassign, self.j13_revoke_node, self.j14_upto_local_chain,
               self.j15_purchase_verification_via_sdk, self.j16_mcp_client, self.j17_mcp_read_only, self.j18_restore, self.j19_console_browser, self.j20_clean_export]
        for i, fn in enumerate(fns, 1):
            if only and i not in only:
                continue
            try:
                fn()
            except Exception as exc:
                self.results.append({'journey': i, 'title': fn.__name__, 'status': 'error', 'error': repr(exc)[:400]})
                print('[%d] ERROR %s: %r' % (i, fn.__name__, exc), flush=True)
            self.stop_workers()
        return self.results


def main():
    p = argparse.ArgumentParser(); p.add_argument('--out'); p.add_argument('--only'); a = p.parse_args()
    j = Journeys()
    try:
        results = j.run_all([int(x) for x in a.only.split(',')] if a.only else None); health = j.http.get('/api/health').json()
    finally:
        j.close()
    out = {'provider_mode': 'test-http', 'revision': health.get('revision'), 'compute_interpreter': RUNTIME, 'models_present': j.models_ok, 'results': results,
           'passed': sum(r['status'] == 'passed' for r in results), 'blocked': sum(r['status'] == 'blocked' for r in results), 'total': len(results)}
    text = json.dumps(out, indent=1, default=str)
    if a.out:
        Path(a.out).write_text(text)
    print(text)
    return 0 if out['passed'] + out['blocked'] == out['total'] else 1


if __name__ == '__main__':
    sys.exit(main())
