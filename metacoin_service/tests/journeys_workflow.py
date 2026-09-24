"""Order §41: eight end-to-end journeys through public entry points with separate client processes and isolated identities.

    PYTHONPATH=. .venv-service/bin/python -m metacoin_service.tests.journeys_workflow --out journeys.json

A fresh temporary home is bootstrapped in test-http provider mode; the API runs as its own process,
workers run as their own processes, and every client action is a subprocess (client_cli, agent_runner,
x402_invoke_client) holding only its own 0600 credential file. Synthetic inputs are labelled. The
facilitator double is local; nothing here is observed external settlement.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
import httpx
from metacoin_service.tests.test_service import Instance, free_port, ROOT, ENV, own_inputs
from metacoin_service.tests.test_agents import TEMPORAL, policy as agent_policy
from metacoin_service import workflows as wf_mod

PY = sys.executable
CSV = "duration_s,harvest_low_mW,harvest_high_mW,load_low_mW,load_high_mW\n10,600,800,500,500\n10,0,0,100,200\n"


class Journeys:
    def __init__(self):
        self.inst = Instance(provider_mode='test-http'); self.port = free_port(); self.base = 'http://127.0.0.1:%d' % self.port
        self.results = []; self.workers = []; self.stop_files = []
        self.creds = {}
        for role in ('owner', 'viewer', 'reviewer'):
            self.creds[role] = self.cred_file(role, self.inst.tok[role])
        self.http = httpx.Client(base_url=self.base, timeout=60)
        self.start_api()

    # ---- process management (task-owned only) ---------------------------------------
    def start_api(self):
        self.api = subprocess.Popen([PY, '-m', 'metacoin_service', '--home', str(self.inst.home), '--provider-mode', 'test-http', 'serve', '--port', str(self.port)],
                                    cwd=ROOT, env=ENV, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        for _ in range(200):
            try:
                if httpx.get(self.base + '/api/health', timeout=1).status_code == 200:
                    return
            except Exception:
                time.sleep(0.1)
        raise SystemExit('api did not start')

    def restart_api(self):
        self.api.terminate(); self.api.wait(timeout=20); self.start_api()

    def start_worker(self, name):
        stop = Path(self.inst.temp.name) / ('stop-' + name)
        p = subprocess.Popen([PY, '-m', 'metacoin_service', '--home', str(self.inst.home), '--provider-mode', 'test-http', 'worker', '--name', name, '--stop-file', str(stop)],
                             cwd=ROOT, env=ENV, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.workers.append(p); self.stop_files.append(stop); return p

    def stop_workers(self):
        for s in self.stop_files:
            s.write_text('stop')
        for p in self.workers:
            try:
                p.wait(timeout=30)
            except subprocess.TimeoutExpired:
                p.terminate(); p.wait(timeout=10)
        self.workers, self.stop_files = [], []

    def worker_once(self, name='once'):
        p = subprocess.run([PY, '-m', 'metacoin_service', '--home', str(self.inst.home), '--provider-mode', 'test-http', 'worker', '--once', '--name', name], cwd=ROOT, env=ENV, capture_output=True, text=True, timeout=120)
        return json.loads(p.stdout)['ran'] if p.returncode == 0 else None

    def close(self):
        self.stop_workers()
        self.api.terminate(); self.api.wait(timeout=20); self.inst.close()

    # ---- client processes ----------------------------------------------------------
    def cred_file(self, name, token):
        path = Path(self.inst.temp.name) / ('cred-' + name + '.json'); path.write_text(json.dumps({'token': token})); os.chmod(path, 0o600); return path

    def cli(self, role, *args, cred=None):
        p = subprocess.run([PY, '-m', 'metacoin_service.client_cli', '--base', self.base, '--credential-file', str(cred or self.creds[role]), *args],
                           cwd=ROOT, env=ENV, capture_output=True, text=True, timeout=300)
        try:
            return p.returncode, json.loads(p.stdout)
        except ValueError:
            return p.returncode, {'stdout': p.stdout[-500:], 'stderr': p.stderr[-500:]}

    def tmpjson(self, name, obj):
        path = Path(self.inst.temp.name) / name; path.write_text(json.dumps(obj)); return str(path)

    def record(self, n, title, ok, evidence, caveat=None):
        self.results.append({'journey': n, 'title': title, 'status': 'passed' if ok else 'FAILED', 'evidence': evidence, 'caveat': caveat})
        print('[%d] %s: %s' % (n, 'PASS' if ok else 'FAIL', title), flush=True)

    def wait_run(self, rid, states=('completed', 'blocked', 'partially_failed', 'failed', 'cancelled', 'waiting_review'), timeout=120):
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.http.post('/api/v1/runs/' + rid + '/advance', headers=self.inst.h('owner'))
            v = self.http.get('/api/v1/runs/' + rid, headers=self.inst.h('owner')).json()
            if v['state'] in states:
                return v
            time.sleep(0.5)
        return v

    # ---- journeys ----------------------------------------------------------------------
    def j1_dataset_workflow_review_export(self):
        rc, ds = self.cli('owner', 'dataset-create', '--name', 'synthetic-series', '--kind', 'temporal_series', '--file', self.tmpjson('series.csv', None) if False else self.write_text('series.csv', CSV), '--format', 'csv', '--provenance', 'declared')
        vid = ds['version_id']
        definition = {'schema': wf_mod.SCHEMA, 'name': 'dataset -> temporal -> review -> export', 'outputs': ['out'], 'nodes': [
            {'id': 'data', 'type': 'dataset', 'bind': 'series'},
            {'id': 'temporal', 'type': 'temporal_energy', 'depends_on': ['data'], 'input': 'data', 'parameters': {'capacity': 10000, 'initial_low': 6000, 'initial_high': 6000, 'reserve': 2000}},
            {'id': 'gate', 'type': 'review_gate', 'depends_on': ['temporal'], 'input': 'temporal'},
            {'id': 'out', 'type': 'export', 'depends_on': ['gate', {'node': 'temporal', 'require': 'accepted_review'}], 'input': 'temporal', 'fields': ['outcome', 'model_id', 'evidence_root', 'review_decision', 'envelope_digest']}]}
        rc, w = self.cli('owner', 'workflow-create', '--file', self.tmpjson('wf1.json', definition))
        rc, run = self.cli('owner', 'workflow-run', w['id'], '--bindings', json.dumps({'series': vid}))
        rid = run['run_id']
        self.start_worker('w-j1')
        v = self.wait_run(rid)
        temporal_job = [n for n in v['nodes'] if n['node_id'] == 'temporal'][0]['job_id']
        rc, dec = self.cli('reviewer', 'decide', temporal_job, 'accepted')
        v = self.wait_run(rid, states=('completed', 'blocked', 'failed'))
        self.stop_workers()
        out_node = [n for n in v['nodes'] if n['node_id'] == 'out'][0]
        rc, arts = self.cli('owner', 'artifacts', temporal_job)
        rc_res, vres = self.cli('viewer', 'result', temporal_job)
        rc, vjob = self.cli('viewer', 'status', temporal_job)
        # the export node's artifact is the authorized summary; the viewer gets the disclosed outcome only after review, never the private result
        export_ok = self.http.get('/api/v1/artifacts/' + out_node['artifact_id'] + '/export', headers=self.inst.h('owner')) if out_node.get('artifact_id') else None
        ok = v['state'] == 'completed' and dec.get('decision') == 'accepted' and rc_res == 2 and vres.get('code') == 'FORBIDDEN' and vjob.get('outcome') == 'FEASIBLE' and export_ok is not None and export_ok.status_code == 200
        self.record(1, 'dataset version -> workflow -> review -> authorized export', ok,
                    {'dataset_version': vid, 'run': rid, 'run_state': v['state'], 'export_node': out_node['state'], 'viewer_result_refusal': vres.get('code'), 'viewer_disclosed_outcome': vjob.get('outcome'),
                     'export_artifact_status': export_ok.status_code if export_ok is not None else None, 'summary_fields': sorted(json.loads(export_ok.text).get('fields', {}).keys()) if export_ok is not None and export_ok.status_code == 200 and export_ok.text.startswith('{') else None})

    def write_text(self, name, text):
        path = Path(self.inst.temp.name) / name; path.write_text(text); return str(path)

    def j2_campaign_restart(self):
        definition = {'name': 'capacity x load', 'kind': 'temporal_energy', 'base': TEMPORAL, 'axes': [{'path': 'capacity', 'values': [7000, 9000, 11000]}, {'path': 'load_scale_percent', 'values': [100, 200]}]}
        rc, pv = self.cli('owner', 'campaign-create', '--file', self.tmpjson('c2.json', definition), '--preview')
        rc, c = self.cli('owner', 'campaign-create', '--file', self.tmpjson('c2.json', definition))
        cid = c['campaign_id']
        self.cli('owner', 'campaign-control', cid, 'run')
        ran = [self.worker_once('w-j2') for _ in range(2)]                     # partial execution
        rc, mid = self.cli('owner', 'campaign-status', cid)
        self.restart_api()                                                       # service restart mid-campaign
        for _ in range(20):
            self.worker_once('w-j2')
            rc, st = self.cli('owner', 'campaign-status', cid)
            if st['state'] == 'completed':
                break
        rc, res = self.cli('owner', 'campaign-status', cid, '--results')
        rows = res['rows']
        jobs = self.http.get('/api/v1/jobs?limit=100', headers=self.inst.h('owner')).json()['items']
        campaign_jobs = [j for j in jobs if j['title'].startswith('capacity x load#')]
        valid = [r for r in rows if r['state'] != 'invalid']
        ok = st['state'] == 'completed' and len(campaign_jobs) == len(valid) and all(r['state'] == 'succeeded' for r in valid) and pv['total_candidates'] == 6
        self.record(2, 'campaign preview -> partial execution -> service restart -> completion without duplicates', ok,
                    {'campaign': cid, 'preview_total': pv.get('total_candidates'), 'preview_estimate': pv.get('estimate'), 'state_before_restart': mid.get('state'), 'done_before_restart': mid.get('done'),
                     'final_state': st['state'], 'jobs_for_valid_candidates': len(campaign_jobs), 'valid_candidates': len(valid), 'outcomes': {r['index']: r.get('outcome') or r.get('reason') for r in rows}})

    def j3_priced_invocation_x402(self):
        rc, svcs = self.cli('owner', 'services')
        sid = next(s['id'] for s in svcs['items'] if s['kind'] == 'temporal_energy')
        rc, q = self.cli('owner', 'quote', sid, '--inputs', self.tmpjson('t3.json', TEMPORAL), '--accept')
        body = json.dumps({'quote_id': q['quote_id'], 'inputs': TEMPORAL}, sort_keys=True)
        p = subprocess.run([PY, '-m', 'metacoin_service.tests.x402_invoke_client', self.base, sid, str(self.creds['owner']), body], cwd=ROOT, env=ENV, capture_output=True, text=True, timeout=120)
        out = json.loads(p.stdout.strip().splitlines()[-1]) if p.stdout.strip() else {'stderr': p.stderr[-400:]}
        jid = out.get('body', {}).get('job_id')
        self.worker_once('w-j3')
        rc, usage = self.cli('owner', 'usage')
        u = [x for x in usage.get('items', []) if x['job_id'] == jid]
        ok = out.get('first_status') == 402 and out.get('second_status') == 202 and out.get('settled', {}).get('success') and u and u[0]['signature_valid'] and u[0]['assessed_charge'] == 1 and u[0]['quantity'] == 1
        self.record(3, 'discover -> quote -> pay over local x402 (real SDK client/server) -> billable units -> bound usage statement', ok,
                    {'service': sid, 'quote': q.get('quote_id'), 'amount_max': q.get('amount_max'), 'statuses': [out.get('first_status'), out.get('second_status')], 'settlement': out.get('settled'),
                     'usage': {k: u[0][k] for k in ('usage_id', 'quantity', 'assessed_charge', 'signature_valid', 'unit')} if u else None},
                    caveat='facilitator is a local double; settlement is not externally observed')

    def j4_agent_under_grant(self):
        pol = agent_policy(allowed_operations=['services:read', 'quote', 'invoke', 'job:read', 'workflow:run'], ceilings={'total_amount': 2, 'per_action_amount': 1, 'max_jobs': 1, 'max_workflows': 1, 'concurrency': 2})
        out_cred = Path(self.inst.temp.name) / 'agent-cred.json'
        rc, g = self.cli('owner', 'grant-issue', '--policy', self.tmpjson('pol.json', pol), '--out', str(out_cred))
        pf = self.tmpjson('pol-agent.json', pol); os.chmod(pf, 0o600)
        cp = str(Path(self.inst.temp.name) / 'agent-cp.json')
        run = lambda *a: subprocess.run([PY, '-m', 'metacoin_service.agent_runner', '--base', self.base, '--credential-file', str(out_cred), '--policy-file', pf, '--checkpoint', cp, *a], cwd=ROOT, env=ENV, capture_output=True, text=True, timeout=120)
        plan = json.loads(run('plan', '--service', 'temporal_energy', '--inputs-file', self.tmpjson('t4.json', TEMPORAL)).stdout)
        # the agent (its own credential) starts a one-node workflow (counts one job + one workflow against the grant)
        definition = {'schema': wf_mod.SCHEMA, 'name': 'agent temporal', 'outputs': ['t'], 'nodes': [{'id': 't', 'type': 'temporal_energy', 'inputs': dict(TEMPORAL, private_label='AGENT_WF_SYNTHETIC')}]}
        rc, w = self.cli('owner', 'workflow-create', '--file', self.tmpjson('wf4.json', definition))
        rc, r1 = self.cli('agent', 'workflow-run', w['id'], cred=out_cred)
        rc, r2 = self.cli('agent', 'workflow-run', w['id'], cred=out_cred)                # exceeds max_workflows
        self.start_worker('w-j4'); v = self.wait_run(r1['run_id'], states=('completed', 'blocked', 'failed')); self.stop_workers()
        rc, grants = self.cli('owner', 'grants')
        view = self.http.get('/api/v1/agents/grants/' + g['grant_id'], headers=self.inst.h('owner')).json()
        # invoking under the plan is refused by the job ceiling (2 jobs: one used by the workflow, one more would fit; make the amount ceiling bite instead)
        ex = json.loads(run('execute', '--service', 'temporal_energy', '--inputs-file', self.tmpjson('t4.json', TEMPORAL)).stdout)
        rc, r3 = self.cli('agent', 'workflow-run', w['id'], cred=out_cred)
        refusal = (ex.get('refusal') or ex.get('plan', {}).get('refusal') or {})
        ok = plan.get('plan') and 'run_id' in r1 and r2.get('code') == 'RATE_LIMITED' and v['state'] == 'completed' and not ex.get('executed') and refusal.get('code') == 'RATE_LIMITED' and r3.get('code') in ('RATE_LIMITED', 'FORBIDDEN') and view['counters']['jobs_created'] == 1
        self.record(4, 'agent under a limited grant: discovers permitted services, completes a workflow, then cannot exceed its job/workflow ceiling', ok,
                    {'grant': g.get('grant_id'), 'plan_within_policy': plan.get('within_policy'), 'workflow_run': r1.get('run_id'), 'run_state': v['state'], 'second_workflow_refusal': r2.get('code'),
                     'invoke_after_ceiling': {'executed': ex.get('executed'), 'stage': ex.get('stage'), 'refusal': refusal.get('code'), 'detail': refusal.get('detail')}, 'third_refusal': r3.get('code'), 'counters': view['counters'], 'remaining': view['remaining']},
                    caveat='the agent runner invokes through the plain route, which prices only in simulation mode; in test-http the ceiling refusal arrives at quote acceptance, before any payment')

    def j5_shared_budget_multi_worker(self):
        already = self.http.get('/api/v1/budgets/tree', headers=self.inst.h('owner')).json()['tree']
        base_used = already['reserved'] + already['committed']                      # earlier journeys committed units against the same workspace root
        self.http.put('/api/v1/budgets/workspace', headers=self.inst.h('owner'), json={'ceiling': base_used + 3})
        def defn(name):
            return {'schema': wf_mod.SCHEMA, 'name': name, 'outputs': ['a', 'b'], 'nodes': [{'id': 'a', 'type': 'energy_audit', 'inputs': own_inputs('J5A_' + name)}, {'id': 'b', 'type': 'energy_audit', 'inputs': own_inputs('J5B_' + name)}]}
        rc, wa = self.cli('owner', 'workflow-create', '--file', self.tmpjson('wf5a.json', defn('shared-alpha')))
        rc, wb = self.cli('owner', 'workflow-create', '--file', self.tmpjson('wf5b.json', defn('shared-beta')))
        self.start_worker('w-j5-1'); self.start_worker('w-j5-2')
        rc, ra = self.cli('owner', 'workflow-run', wa['id'], '--budget-ceiling', '2')
        rc, rb = self.cli('owner', 'workflow-run', wb['id'], '--budget-ceiling', '2')
        va = self.wait_run(ra['run_id'], states=('completed', 'blocked', 'failed')); vb = self.wait_run(rb['run_id'], states=('completed', 'blocked', 'failed'))
        self.stop_workers()
        rc, tree = self.cli('owner', 'budget-tree')
        root = tree['tree']
        states = sorted([va['state'], vb['state']])
        succeeded = sum(n['state'] == 'succeeded' for v in (va, vb) for n in v['nodes'])
        blocked = [n for v in (va, vb) for n in v['nodes'] if n['state'] == 'blocked']
        ok = root['reserved'] + root['committed'] <= base_used + 3 and root['committed'] == base_used + 3 and root['reserved'] == 0 and succeeded == 3 and len(blocked) == 1 and 'never fit' in blocked[0]['blocked_reason']
        self.record(5, 'two workflows share a parent budget under two worker processes: no overspend, explained refusal', ok,
                    {'workspace_ceiling': root['ceiling'], 'committed_before_journey': base_used, 'reserved': root['reserved'], 'committed': root['committed'], 'run_states': states, 'succeeded_nodes': succeeded, 'blocked_nodes': len(blocked), 'blocked_reason': blocked[0]['blocked_reason'] if blocked else None,
                     'note': 'which run loses the fourth unit depends on worker interleaving; the invariant is committed <= ceiling and exactly one explained refusal',
                     'nodes': {v['run_id']: [(n['node_id'], n['state'], n['blocked_reason']) for n in v['nodes']] for v in (va, vb)},
                     'workers': [w['name'] for w in self.http.get('/api/v1/workers', headers=self.inst.h('owner')).json()['items'] if w['name'].startswith('w-j5')]},
                    caveat='stale-attempt fencing is exercised by the failure suite (test_failures_workflow) with a killed worker')
        self.http.put('/api/v1/budgets/workspace', headers=self.inst.h('owner'), json={'ceiling': 10})

    def j6_selective_share(self):
        rc, c = self.cli('owner', 'create', '--kind', 'energy_audit', '--title', 'share-source', '--inputs', self.tmpjson('in6.json', own_inputs('J6_PRIVATE_LABEL')), '--reviewer', self.inst.ids['reviewer'])
        self.cli('owner', 'freeze', c['id']); rc, j = self.cli('owner', 'submit', c['id'])
        rc, other = self.cli('owner', 'create', '--kind', 'energy_audit', '--title', 'unrelated', '--inputs', self.tmpjson('in6b.json', own_inputs('J6_OTHER')), '--reviewer', self.inst.ids['reviewer'])
        self.cli('owner', 'freeze', other['id']); rc, j2 = self.cli('owner', 'submit', other['id'])
        self.worker_once('w-j6'); self.worker_once('w-j6')
        rc, sh = self.cli('owner', 'share', j['id'], '--grantee', self.inst.ids['viewer'], '--fields', 'outcome,evidence_root,model_id')
        rc, proj = self.cli('viewer', 'projection', j['id'])
        verify = self.http.post('/api/v1/projections/verify', headers=self.inst.h('viewer'), json={'bundle': proj['bundle']}).json()
        rc_res, res = self.cli('viewer', 'result', j['id'])
        rc_art, arts = self.cli('viewer', 'artifacts', j['id'])
        priv = [a for a in arts.get('items', []) if not a.get('public')] if isinstance(arts, dict) else []
        rc_exp = self.http.get('/api/v1/artifacts/' + priv[0]['id'] + '/export', headers=self.inst.h('viewer')).status_code if priv else 'no-private-listed'
        rc_other, other_proj = self.cli('viewer', 'projection', j2['id'])
        text = json.dumps(proj)
        ok = sorted(proj.get('fields', {})) == ['evidence_root', 'model_id', 'outcome'] and verify['signature_valid'] and verify['issuer_is_this_service'] and res.get('code') == 'FORBIDDEN' and other_proj.get('code') == 'FORBIDDEN' and 'J6_PRIVATE_LABEL' not in text and rc_exp in (403, 'no-private-listed')
        self.record(6, 'share a selected projection with a viewer: verifiable statement, no private ancestors, no unrelated results', ok,
                    {'share': sh.get('share_id'), 'projection_fields': sorted(proj.get('fields', {})), 'verify': verify, 'viewer_private_result': res.get('code'), 'viewer_private_export': rc_exp, 'unrelated_projection': other_proj.get('code')})

    def j7_reuse_without_new_entitlements(self):
        rc, c1 = self.cli('owner', 'create', '--kind', 'energy_audit', '--title', 'reuse-source', '--inputs', self.tmpjson('in7.json', own_inputs('J7_SYNTHETIC')), '--reviewer', self.inst.ids['reviewer'])
        self.cli('owner', 'freeze', c1['id']); rc, j1 = self.cli('owner', 'submit', c1['id']); self.worker_once('w-j7')
        rc, c2 = self.cli('owner', 'create', '--kind', 'energy_audit', '--title', 'reuse-repeat', '--inputs', self.tmpjson('in7.json', own_inputs('J7_SYNTHETIC')), '--reviewer', self.inst.ids['reviewer'])
        self.cli('owner', 'freeze', c2['id'])
        rc, look = self.cli('owner', 'reuse-lookup', c2['id'])
        rc, j2 = self.cli('owner', 'submit', c2['id'], '--reuse')
        rc, act = self.cli('owner', 'action', j2['id'], '--request-id', 'req-reused-1', '--dry-run')          # no entitlement was registered for the reused job
        rc, rr = self.cli('owner', 'review-request', j2['id'])
        ok = look.get('hit', {}) and look['hit']['job_id'] == j1['id'] and j2.get('state') == 'succeeded' and j2.get('reused_from') == j1['id'] and j2['payment']['state'] == 'NOT_REQUESTED' and act.get('error') and j2['review_state'] == 'none'
        self.record(7, 'repeat an identical deterministic computation: reuse under policy, no implicit review or payment entitlement', ok,
                    {'original': j1['id'], 'reused': j2.get('id'), 'reused_from': j2.get('reused_from'), 'evidence_root_equal': j2.get('evidence_root') == self.http.get('/api/v1/jobs/' + j1['id'], headers=self.inst.h('owner')).json()['evidence_root'],
                     'payment_state': j2.get('payment', {}).get('state'), 'action_dry_run_refusal': act.get('code'), 'review_request_on_reused': rr.get('code') or rr.get('review_state'), 'metered': [u for u in self.cli('owner', 'usage')[1].get('items', []) if u['job_id'] == j2.get('id')]})

    def j8_cancel_with_unresolved_action(self):
        definition = {'schema': wf_mod.SCHEMA, 'name': 'cancel with action', 'outputs': ['a', 'b'], 'nodes': [
            {'id': 'a', 'type': 'energy_audit', 'inputs': own_inputs('J8A')}, {'id': 'b', 'type': 'energy_audit', 'inputs': own_inputs('J8B'), 'depends_on': ['a']}]}
        rc, w = self.cli('owner', 'workflow-create', '--file', self.tmpjson('wf8.json', definition))
        rc, run = self.cli('owner', 'workflow-run', w['id'], '--budget-ceiling', '2')
        rid = run['run_id']
        self.wait_run(rid, states=('running',), timeout=10); self.worker_once('w-j8')                           # node a completes
        v = self.wait_run(rid, states=('running',), timeout=10)
        ja = [n for n in v['nodes'] if n['node_id'] == 'a'][0]['job_id']
        self.cli('owner', 'review-request', ja); self.cli('reviewer', 'decide', ja, 'accepted')
        rc, act = self.cli('owner', 'action', ja, '--request-id', 'req-j8-1')                                     # provider action on the accepted node
        rc, cancel = self.cli('owner', 'run-cancel', rid)
        v = self.wait_run(rid, states=('cancelled', 'completed', 'blocked'), timeout=30)
        rc, job_a = self.cli('owner', 'status', ja)
        rc, tree = self.cli('owner', 'budget-tree')
        rc, ops = self.cli('owner', 'status-ops')
        rc, arts = self.cli('owner', 'artifacts', ja)
        evidence_ok = self.http.get('/api/v1/jobs/' + ja + '/result', headers=self.inst.h('owner')).status_code == 200
        ok = v['state'] == 'cancelled' and [n['state'] for n in v['nodes']] == ['succeeded', 'cancelled'] and evidence_ok and job_a['payment']['state'] in ('CONFIRMED', 'OUTCOME_UNKNOWN', 'SUBMISSION_PENDING') and ops['unresolved_payment_actions'] >= 1 and v['budget']['committed_total'] == 1 and v['budget']['reserved_total'] == 0
        self.record(8, 'cancel a partially completed workflow holding a provider action: evidence kept, economic state reported as it is', ok,
                    {'run': rid, 'run_state': v['state'], 'node_states': {n['node_id']: n['state'] for n in v['nodes']}, 'action_state': job_a['payment']['state'], 'action_reference': job_a['payment'].get('reference'),
                     'evidence_result_readable': evidence_ok, 'budget': v['budget'], 'unresolved_payment_actions_recorded': ops['unresolved_payment_actions']},
                    caveat='the local facilitator double answers synchronously, so the action settles as CONFIRMED here; an OUTCOME_UNKNOWN state is produced by the failure suite by making the provider unreachable')

    def run_all(self):
        for fn in (self.j1_dataset_workflow_review_export, self.j2_campaign_restart, self.j3_priced_invocation_x402, self.j4_agent_under_grant,
                   self.j5_shared_budget_multi_worker, self.j6_selective_share, self.j7_reuse_without_new_entitlements, self.j8_cancel_with_unresolved_action):
            try:
                fn()
            except Exception as exc:
                self.stop_workers()
                self.results.append({'journey': fn.__name__, 'status': 'ERROR', 'error': repr(exc)[:400]})
                print('[?] ERROR', fn.__name__, repr(exc)[:300], flush=True)
        return self.results


def main():
    p = argparse.ArgumentParser(); p.add_argument('--out'); a = p.parse_args()
    j = Journeys()
    try:
        results = j.run_all()
        health = j.http.get('/api/health').json()
    finally:
        j.close()
    out = {'provider_mode': 'test-http', 'revision': health.get('revision'), 'results': results, 'passed': sum(r['status'] == 'passed' for r in results), 'total': len(results),
           'note': 'separate client processes; isolated credential files; local facilitator double; synthetic labelled inputs'}
    text = json.dumps(out, indent=1)
    if a.out:
        Path(a.out).write_text(text)
    print(text)
    return 0 if out['passed'] == out['total'] else 1


if __name__ == '__main__':
    sys.exit(main())
