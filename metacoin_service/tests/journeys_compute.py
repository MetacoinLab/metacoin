"""Order §45: twelve operational acceptance journeys for the compute engine through normal API/CLI boundaries with
isolated identities, bounded synthetic inputs and real task-owned worker processes.

    PYTHONPATH=. .venv-service/bin/python -m metacoin_service.tests.journeys_compute --out journeys-compute.json

A fresh temporary home runs in test-http provider mode; the API and the workers are separate processes; every client
action is a client_cli / x402 client subprocess holding only its own 0600 credential file. Each journey records what
actually ran and which backend was used. GPU journeys are marked blocked (not passed) when no CUDA device is present."""
import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
import httpx
from metacoin_service.tests.test_service import Instance, free_port, ROOT, ENV
from metacoin_service.tests.test_compute_engine import batch_spec, mc_spec, heat_spec
from metacoin_service.compute import npy, container
from metacoin_service.compute.engine import compute_interpreter
from metacoin_service import db as database, auth, workflows as wf_mod
from metacoin_service.db import now

PY = sys.executable
RUNTIME = compute_interpreter(type('S', (), {'compute_python': ''})())
HAVE_CUDA = bool(RUNTIME and RUNTIME.get('cuda'))


class Journeys:
    def __init__(self):
        self.inst = Instance(provider_mode='test-http'); self.inst.settings.limits['compute_checkpoint_interval_seconds'] = 1
        self.port = free_port(); self.base = 'http://127.0.0.1:%d' % self.port
        self.results, self.workers, self.stop_files = [], [], []
        self.env = dict(ENV, METACOIN_LIMITS_JSON=json.dumps({'compute_checkpoint_interval_seconds': 1}))
        self.creds = {r: self.cred_file(r, self.inst.tok[r]) for r in ('owner', 'viewer', 'reviewer')}
        self.http = httpx.Client(base_url=self.base, timeout=120)
        self.start_api()

    # ---- processes (task-owned only) --------------------------------------------------------------
    def start_api(self):
        self.api = subprocess.Popen([PY, '-m', 'metacoin_service', '--home', str(self.inst.home), '--provider-mode', 'test-http', 'serve', '--port', str(self.port)],
                                    cwd=ROOT, env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
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
                             cwd=ROOT, env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
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

    def worker_once(self, name='once'):
        p = subprocess.run([PY, '-m', 'metacoin_service', '--home', str(self.inst.home), '--provider-mode', 'test-http', 'worker', '--once', '--name', name], cwd=ROOT, env=self.env, capture_output=True, text=True, timeout=600)
        return json.loads(p.stdout)['ran'] if p.returncode == 0 else None

    def close(self):
        self.stop_workers(); self.api.terminate(); self.api.wait(timeout=20); self.inst.close()

    # ---- clients ---------------------------------------------------------------------------------
    def cred_file(self, name, token):
        path = Path(self.inst.temp.name) / ('cred-' + name + '.json'); path.write_text(json.dumps({'token': token})); os.chmod(path, 0o600); return path

    def cli(self, role, *args, cred=None):
        p = subprocess.run([PY, '-m', 'metacoin_service.client_cli', '--base', self.base, '--credential-file', str(cred or self.creds[role]), *args], cwd=ROOT, env=self.env, capture_output=True, text=True, timeout=600)
        try:
            return p.returncode, json.loads(p.stdout)
        except ValueError:
            return p.returncode, {'stdout': p.stdout[-400:], 'stderr': p.stderr[-400:]}

    def tmpjson(self, name, obj):
        path = Path(self.inst.temp.name) / name; path.write_text(json.dumps(obj)); return str(path)

    def H(self, role='owner'):
        return self.inst.h(role)

    def submit(self, kind, spec, title='journey'):
        rc, out = self.cli('owner', 'compute-submit', '--kind', kind, '--inputs', self.tmpjson(title + '.json', spec), '--reviewer', self.inst.ids['reviewer'], '--title', title)
        assert rc == 0, out
        return out['job_id']

    def view(self, jid, role='owner'):
        return self.http.get('/api/v1/compute/jobs/' + jid, headers=self.H(role)).json()

    def wait(self, jid, pred, timeout=300):
        deadline = time.time() + timeout
        while time.time() < deadline:
            v = self.view(jid)
            if pred(v):
                return v
            time.sleep(0.5)
        return self.view(jid)

    def record(self, n, title, status, evidence, caveat=None):
        self.results.append({'journey': n, 'title': title, 'status': status, 'evidence': evidence, 'caveat': caveat})
        print('[%d] %s: %s' % (n, status.upper(), title), flush=True)

    # ---- journeys ----------------------------------------------------------------------------------
    def j1_temporal_batch_cpu(self):
        jid = self.submit('temporal_batch', batch_spec(device_policy='cpu', private_label='J1_SYNTHETIC'), 'j1')
        ran = self.worker_once('w-j1')
        v = self.view(jid)
        rc, verify = self.cli('owner', 'compute-verify', jid)
        out = Path(self.inst.temp.name) / 'j1-results.npy'
        rc2, exp = self.cli('owner', 'compute-export', jid, 'results.npy', '--out', str(out))
        vals, dtype, shape = npy.read(out)
        ok = ran and ran[1] == 'succeeded' and v['backend'] == 'cpu' and v['verification']['passed'] and v['verification']['mode'] == 'exact_all' and shape == [364, 17]
        self.record(1, 'temporal batch on the cpu backend, exact verification, authorized retrieval', 'passed' if ok else 'failed',
                    {'job': jid, 'backend': v['backend'], 'verification': {k: v['verification'][k] for k in ('mode', 'passed', 'checked')}, 'exported_shape': shape, 'reproducibility_keys': sorted(verify.get('reproducibility', {}))[:6]})

    def j2_gpu_batch_vs_cpu(self):
        if not HAVE_CUDA:
            self.record(2, 'bounded batch on the actual gpu compared with the cpu reference', 'blocked', {'reason': 'no CUDA device usable by the compute interpreter'}); return
        jg = self.submit('temporal_batch', batch_spec(device_policy='gpu', private_label='J2_SYNTHETIC'), 'j2g')
        jc = self.submit('temporal_batch', batch_spec(device_policy='cpu', private_label='J2_SYNTHETIC'), 'j2c')
        self.worker_once('w-j2'); self.worker_once('w-j2')
        vg, vc = self.view(jg), self.view(jc)
        g = self.http.get('/api/v1/compute/jobs/' + jg + '/outputs/results.npy', headers=self.H()).content
        c = self.http.get('/api/v1/compute/jobs/' + jc + '/outputs/results.npy', headers=self.H()).content
        ok = vg['backend'] == 'cuda' and vc['backend'] == 'cpu' and vg['verification']['passed'] and g == c
        self.record(2, 'bounded batch on the actual gpu compared with the cpu reference', 'passed' if ok else 'failed',
                    {'gpu_job': jg, 'gpu_backend': vg['backend'], 'device': (vg.get('versions') or {}).get('versions', {}).get('device'), 'byte_identical_to_cpu': g == c,
                     'gpu_verification': vg['verification']['mode'], 'energy_counter_present': (vg.get('versions') or {}).get('energy_counter_start_mJ') is not None,
                     'gpu_telemetry': (vg.get('telemetry') or {}).get('gpu')},
                    caveat='device-wide energy counter and nvidia-smi readings are not per-job attribution')

    def j3_long_job_progress_pause_resume(self):
        jid = self.submit('heat_diffusion', heat_spec(private_label='J3_SYNTHETIC'), 'j3')
        self.start_worker('w-j3')
        rc, watch = self.cli('owner', 'compute-watch', jid, '--pause-after-checkpoint', '--timeout', '240')
        v = self.view(jid)
        paused = v['phase'] == 'paused' and v['state'] == 'queued' and v['hold'] and v['checkpoint_generation'] >= 1 and 0 < v['work']['committed'] < v['work']['total']
        with database.Database(self.inst.settings.db_path).read() as db:
            slots_free = db.execute('SELECT COUNT(*) FROM compute_reservations').fetchone()[0] == 0
        rc, resumed = self.cli('owner', 'compute-resume', jid)
        v2 = self.wait(jid, lambda v: v['state'] in ('succeeded', 'failed', 'cancelled'), timeout=300)
        self.stop_workers()
        ok = paused and slots_free and v2['state'] == 'succeeded' and v2['verification']['passed'] and v2['work']['committed'] == v2['work']['total']
        self.record(3, 'long job: real progress, pause at a durable checkpoint, capacity released, resume to a verified result', 'passed' if ok else 'failed',
                    {'job': jid, 'paused_at_generation': v['checkpoint_generation'], 'committed_at_pause': v['work']['committed'], 'total': v['work']['total'], 'capacity_released': slots_free,
                     'final': {'state': v2['state'], 'phase': v2['phase'], 'verification': v2['verification']['mode'], 'checkpoints': len(v2['checkpoints'])}, 'backend': v2['backend']})

    def j4_kill_worker_recover_fenced(self):
        jid = self.submit('heat_diffusion', heat_spec(private_label='J4_SYNTHETIC'), 'j4')
        victim = self.start_worker('w-j4-victim')
        v = self.wait(jid, lambda v: v['checkpoint_generation'] >= 1 and v['state'] == 'running', timeout=120)
        gen, committed = v['checkpoint_generation'], v['work']['committed']
        os.kill(victim.pid, signal.SIGKILL); victim.wait(timeout=10); self.workers.remove(victim); self.stop_files.pop()
        with database.Database(self.inst.settings.db_path).tx() as db:
            db.execute('UPDATE jobs SET lease_expires=? WHERE id=?', (now() - 1, jid))
            old_gen = db.execute('SELECT lease_generation FROM jobs WHERE id=?', (jid,)).fetchone()[0]
        time.sleep(2)
        ran = self.worker_once('w-j4-rescuer')
        v2 = self.view(jid)
        units = self.http.get('/api/v1/compute/jobs/' + jid + '/checkpoints', headers=self.H()).json()['committed_work_units']
        contiguous = all(units[i]['unit_to'] == units[i + 1]['unit_from'] for i in range(len(units) - 1)) and units[-1]['unit_to'] == v2['work']['total']
        with database.Database(self.inst.settings.db_path).read() as db:
            new_gen = db.execute('SELECT lease_generation FROM jobs WHERE id=?', (jid,)).fetchone()[0]
        ok = ran and ran[1] == 'succeeded' and v2['verification']['passed'] and contiguous and new_gen == old_gen + 1
        self.record(4, 'worker terminated after a committed checkpoint; recovery under a new fenced attempt without missing or double-counting work', 'passed' if ok else 'failed',
                    {'job': jid, 'killed_after_generation': gen, 'committed_before_kill': committed, 'generation_before': old_gen, 'generation_after': new_gen, 'work_units_contiguous': contiguous, 'units': units, 'final_verification': v2['verification']['mode']})

    def j5_monte_carlo_chunk_and_resume_invariance(self):
        from metacoin_service.tests.test_compute_science import base_temporal
        long_base = base_temporal(initial_low=6000, initial_high=6000, segments=[{'duration': 5, 'harvest_low': 500 + (i % 7) * 40, 'harvest_high': 500 + (i % 7) * 40, 'load_low': 480 + (i % 5) * 30, 'load_high': 480 + (i % 5) * 30, 'leakage_low': 0, 'leakage_high': 0} for i in range(64)])
        base = mc_spec(base=long_base, samples=1_000_000, seed=2026, private_label='J5_SYNTHETIC')
        ja = self.submit('monte_carlo_reliability', base, 'j5a')
        self.worker_once('w-j5')
        va = self.view(ja)
        sa = self.http.get('/api/v1/jobs/' + ja, headers=self.H()).json()['summary']
        # second run of the identical model: pause + resume across a chunk boundary; counts must be identical
        jb = self.submit('monte_carlo_reliability', dict(base, private_label='J5_SYNTHETIC_B'), 'j5b')
        self.start_worker('w-j5b')
        rc, watch = self.cli('owner', 'compute-watch', jb, '--pause-after-checkpoint', '--timeout', '120')
        vb_p = self.view(jb)
        self.cli('owner', 'compute-resume', jb)
        vb = self.wait(jb, lambda v: v['state'] in ('succeeded', 'failed', 'cancelled'), timeout=300)
        self.stop_workers()
        sb = self.http.get('/api/v1/jobs/' + jb, headers=self.H()).json()['summary']
        ok = va['verification']['passed'] and vb['verification']['passed'] and sa['events'] == sb['events'] and sa['samples'] == sb['samples'] == base['samples'] and vb_p['phase'] == 'paused'
        self.record(5, 'monte carlo with a fixed seed and sample count: pause/resume across chunks reuses or skips no sample identifier', 'passed' if ok else 'failed',
                    {'uninterrupted': {'job': ja, 'events': sa['events'], 'estimate': sa['probability_estimate'], 'interval': sa['interval']},
                     'resumed': {'job': jb, 'paused_at_committed': vb_p['work']['committed'], 'events': sb['events'], 'checkpoints': len(vb['checkpoints'])}, 'audit_mode': vb['verification']['mode']})

    def j6_heat_reject_unstable_verify_export(self):
        rc, bad = self.cli('owner', 'compute-submit', '--kind', 'heat_diffusion', '--inputs', self.tmpjson('j6bad.json', heat_spec(dt='0.0001', private_label='J6_SYNTHETIC')), '--reviewer', self.inst.ids['reviewer'])
        jid = self.submit('heat_diffusion', heat_spec(nx=33, ny=33, steps=25, initial={'type': 'sine_mode', 'm': 1, 'n': 2, 'amplitude': '1'}, private_label='J6_SYNTHETIC'), 'j6')
        self.worker_once('w-j6')
        v = self.view(jid)
        from metacoin_service.compute import reference, inputs as cinputs
        spec = heat_spec(nx=33, ny=33, steps=25, initial={'type': 'sine_mode', 'm': 1, 'n': 2, 'amplitude': '1'}, private_label='J6_SYNTHETIC'); p = cinputs.validate_heat(spec)
        out = Path(self.inst.temp.name) / 'j6-field.npy'
        self.cli('owner', 'compute-export', jid, 'field.npy', '--out', str(out))
        vals, _, shape = npy.read(out)
        from metacoin_service.compute.kernels import heat_initial_field
        f0 = heat_initial_field(spec, p)
        g = reference.heat_eigenmode_factor(1, 2, 33, 33, float(p['rx']), float(p['ry'])) ** 25
        err = max(abs(vals[j * 33 + i] - g * f0[j][i]) for j in range(33) for i in range(33))
        rc_v, viewer = self.cli('viewer', 'compute-export', jid, 'field.npy', '--out', str(out) + '.viewer')
        ok = rc != 0 and 'unstable timestep' in json.dumps(bad) and v['verification']['passed'] and err < 1e-10 and rc_v != 0
        self.record(6, 'heat diffusion: unstable configuration rejected, analytical eigenmode benchmark verified, private field exported only to an authorized principal', 'passed' if ok else 'failed',
                    {'refusal': (bad.get('detail') or {}).get('reason') if isinstance(bad.get('detail'), dict) else bad.get('detail'), 'job': jid, 'verification': v['verification']['mode'], 'eigenmode_max_abs_error': err, 'viewer_export_refused': rc_v != 0})

    def j7_capacity_race(self):
        with database.Database(self.inst.settings.db_path).tx() as db:
            pass
        env = dict(self.env, METACOIN_LIMITS_JSON=json.dumps({'compute_checkpoint_interval_seconds': 1, 'compute_cpu_slots': 1}))
        ja = self.submit('heat_diffusion', heat_spec(private_label='J7_SYNTHETIC_A'), 'j7a')
        stop_a = Path(self.inst.temp.name) / 'stop-j7a'
        wa = subprocess.Popen([PY, '-m', 'metacoin_service', '--home', str(self.inst.home), '--provider-mode', 'test-http', 'worker', '--name', 'w-j7-a', '--stop-file', str(stop_a)], cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.wait(ja, lambda v: v['phase'] in ('running', 'initializing'), timeout=60)
        jb = self.submit('heat_diffusion', heat_spec(steps=200, private_label='J7_SYNTHETIC_B'), 'j7b')
        p = subprocess.run([PY, '-m', 'metacoin_service', '--home', str(self.inst.home), '--provider-mode', 'test-http', 'worker', '--once', '--name', 'w-j7-b'], cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
        ran_b = json.loads(p.stdout)['ran'] if p.returncode == 0 else 'error'
        q = self.http.get('/api/v1/queue', headers=self.H()).json()
        reason = next((x['waiting_reason'] for x in q['queued'] if x['job_id'] == jb), None)
        self.wait(ja, lambda v: v['state'] in ('succeeded', 'failed'), timeout=300)
        vb = self.wait(jb, lambda v: v['state'] in ('succeeded', 'failed'), timeout=120)          # once the slot is released, any live worker may take it
        stop_a.write_text('stop'); wa.wait(timeout=60)
        with database.Database(self.inst.settings.db_path).read() as db:
            who = db.execute("SELECT worker_id FROM attempts WHERE job_id=? ORDER BY generation DESC LIMIT 1", (jb,)).fetchone()['worker_id']
        ok = ran_b is None and reason == 'waiting for a free cpu compute slot' and vb['state'] == 'succeeded'
        self.record(7, 'two jobs race for one cpu compute slot: admission preserves the configured headroom and explains the wait', 'passed' if ok else 'failed',
                    {'first': ja, 'second': jb, 'second_claimed_while_first_running': ran_b is not None, 'waiting_reason': reason, 'second_state_after_release': vb['state'], 'second_run_by_worker': who})

    def j8_unauthorized_access(self):
        jid = self.submit('heat_diffusion', heat_spec(nx=64, ny=64, steps=200, private_label='J8_SYNTHETIC'), 'j8')
        self.worker_once('w-j8')
        with database.Database(self.inst.settings.db_path).tx() as db:
            db.execute("INSERT INTO principals (id, workspace, name, role, created_at) VALUES ('p_foreign_owner','ws_other','foreign','owner',?)", (now(),))
            db.execute("INSERT OR IGNORE INTO campaigns VALUES ('ws_other','campaign-ws_other',5,'Test-META','local-simulation','unit')")
            cid, token = auth.issue_credential(db, 'p_foreign_owner', 3600)
        foreign = self.cred_file('foreign', token)
        codes = {}
        for who, cred in (('viewer', self.creds['viewer']), ('other-workspace-owner', foreign)):
            for what, args in (('checkpoints', ('compute-inspect', jid)), ('outputs', ('compute-export', jid, 'field.npy', '--out', str(Path(self.inst.temp.name) / (who + '-x.npy')))),
                               ('plot', ('compute-export', jid, 'plot.svg', '--out', str(Path(self.inst.temp.name) / (who + '-p.svg')))), ('log', ('compute-log', jid))):
                rc, out = self.cli('owner', *args, cred=cred)
                codes[who + ':' + what] = out.get('code', 'OK' if rc == 0 else 'refused')
            r = self.http.get('/api/v1/compute/jobs/' + jid + '/checkpoints', headers={'Authorization': 'Bearer ' + json.load(open(cred))['token']}); codes[who + ':checkpoints_api'] = r.status_code
            r = self.http.get('/api/v1/usage', headers={'Authorization': 'Bearer ' + json.load(open(cred))['token']}); codes[who + ':usage_items'] = len(r.json().get('items', [])) if r.status_code == 200 else r.status_code
            r = self.http.get('/api/v1/compute/jobs/' + jid, headers={'Authorization': 'Bearer ' + json.load(open(cred))['token']}); codes[who + ':telemetry_exposed'] = 'telemetry' in r.json()
        ok = (codes['viewer:checkpoints_api'] == 403 and codes['viewer:outputs'] != 'OK' and codes['viewer:plot'] != 'OK' and codes['viewer:log'] != 'OK' and not codes['viewer:telemetry_exposed']
              and codes['other-workspace-owner:checkpoints_api'] == 404 and codes['other-workspace-owner:outputs'] != 'OK' and codes['other-workspace-owner:usage_items'] == 0)
        self.record(8, 'unauthorized checkpoint, output array, plot, telemetry and usage access from a viewer and from another workspace are refused', 'passed' if ok else 'failed', codes)

    def j9_purchase_over_x402(self):
        rc, svcs = self.cli('owner', 'services')
        sid = next(s['id'] for s in svcs['items'] if s['kind'] == 'temporal_batch')
        spec = batch_spec(private_label='J9_PAID_SYNTHETIC')
        rc, q = self.cli('owner', 'quote', sid, '--inputs', self.tmpjson('j9.json', spec), '--accept')
        body = json.dumps({'quote_id': q['quote_id'], 'inputs': spec}, sort_keys=True)
        p = subprocess.run([PY, '-m', 'metacoin_service.tests.x402_invoke_client', self.base, sid, str(self.creds['owner']), body], cwd=ROOT, env=self.env, capture_output=True, text=True, timeout=120)
        out = json.loads(p.stdout.strip().splitlines()[-1]) if p.stdout.strip() else {'stderr': p.stderr[-300:]}
        jid = out.get('body', {}).get('job_id')
        self.worker_once('w-j9')
        rc, usage = self.cli('owner', 'usage')
        u = [x for x in usage.get('items', []) if x['job_id'] == jid]
        v = self.view(jid) if jid else {}
        ok = out.get('first_status') == 402 and out.get('second_status') == 202 and out.get('settled', {}).get('success') and u and u[0]['quantity'] == 364 and u[0]['assessed_charge'] == 364 and u[0]['signature_valid'] and v.get('verification', {}).get('passed')
        self.record(9, 'purchase a bounded compute service over the local x402 protocol; entitlement and useful-work accounting inspected', 'passed' if ok else 'failed',
                    {'quote': {'quantity_max': q.get('quantity_max'), 'amount_max': q.get('amount_max'), 'unit': q.get('unit')}, 'statuses': [out.get('first_status'), out.get('second_status')], 'settled': out.get('settled'),
                     'usage': ({k: u[0][k] for k in ('quantity', 'assessed_charge', 'signature_valid', 'unit')} if u else None), 'work_committed': v.get('work', {}).get('committed')},
                    caveat='local facilitator double; settlement not externally observed; assessed charge is not settled money')

    def j10_lost_response_idempotent_recovery(self):
        spec = batch_spec(private_label='J10_SYNTHETIC')
        rc, c = self.cli('owner', 'create', '--kind', 'temporal_batch', '--title', 'j10', '--inputs', self.tmpjson('j10.json', spec), '--reviewer', self.inst.ids['reviewer'])
        self.cli('owner', 'freeze', c['id'])
        key = 'j10-submit-' + c['id']
        rc1, j1 = self.cli('owner', 'submit', c['id'], '--idempotency-key', key)
        self.restart_api()                                                          # the client "lost" the response: same key after an API restart
        rc2, j2 = self.cli('owner', 'submit', c['id'], '--idempotency-key', key)
        rc3, j3 = self.cli('owner', 'submit', c['id'])                              # without the key: refused (one job per contract), no duplicate
        self.worker_once('w-j10')
        jobs = [j for j in self.http.get('/api/v1/jobs?limit=100', headers=self.H()).json()['items'] if j['contract_id'] == c['id']]
        pause1 = self.cli('owner', 'compute-pause', j1['id'])[1]
        ok = j1['id'] == j2['id'] and rc3 != 0 and len(jobs) == 1 and jobs[0]['state'] == 'succeeded'
        self.record(10, 'client response lost after accepted submission: the same job and action recovered by idempotent identifiers, no duplicate computation or charge', 'passed' if ok else 'failed',
                    {'job': j1['id'], 'replay_same_job': j1['id'] == j2['id'], 'unkeyed_resubmit': j3.get('code'), 'jobs_for_contract': len(jobs), 'pause_on_terminal_refused': pause1.get('code')})

    def j11_cancel_with_unresolved_economic_state(self):
        rc, svcs = self.cli('owner', 'services')
        sid = next(s['id'] for s in svcs['items'] if s['kind'] == 'heat_diffusion')
        spec = heat_spec(private_label='J11_SYNTHETIC')
        rc, q = self.cli('owner', 'quote', sid, '--inputs', self.tmpjson('j11.json', spec), '--accept')
        body = json.dumps({'quote_id': q['quote_id'], 'inputs': spec}, sort_keys=True)
        p = subprocess.run([PY, '-m', 'metacoin_service.tests.x402_invoke_client', self.base, sid, str(self.creds['owner']), body], cwd=ROOT, env=self.env, capture_output=True, text=True, timeout=120)
        out = json.loads(p.stdout.strip().splitlines()[-1]) if p.stdout.strip() else {}
        jid = out.get('body', {}).get('job_id')
        with database.Database(self.inst.settings.db_path).tx() as db:               # persisted fault marker: the provider outcome for this sale is unknown
            db.execute("UPDATE invoke_sales SET state='OUTCOME_UNKNOWN' WHERE job_id=?", (jid,))
        self.start_worker('w-j11')
        self.wait(jid, lambda v: v['phase'] == 'running' and (v['progress'] or {}).get('computed', 0) > 0, timeout=120)
        rc, cancelled = self.cli('owner', 'compute-cancel', jid)
        v = self.wait(jid, lambda v: v['state'] in ('cancelled', 'succeeded', 'failed'), timeout=120)
        self.stop_workers()
        with database.Database(self.inst.settings.db_path).read() as db:
            sale = db.execute('SELECT state FROM invoke_sales WHERE job_id=?', (jid,)).fetchone()['state']
            usage = db.execute('SELECT COUNT(*) FROM usage_records WHERE job_id=?', (jid,)).fetchone()[0]
        ok = v['state'] == 'cancelled' and sale == 'OUTCOME_UNKNOWN' and usage == 0 and len(v['checkpoints']) >= 0
        self.record(11, 'cancel a running paid compute job whose provider outcome is unknown: compute stops, economic uncertainty preserved, nothing billed', 'passed' if ok else 'failed',
                    {'job': jid, 'final_state': v['state'], 'phase': v['phase'], 'sale_state_after_cancel': sale, 'usage_records': usage, 'checkpoints_kept': len(v['checkpoints'])},
                    caveat='the unknown provider outcome is a persisted fault marker on the local sale record (the double answers synchronously)')

    def j12_workflow_and_refinement_campaign(self):
        definition = {'schema': wf_mod.SCHEMA, 'name': 'j12 batch -> monte carlo -> review -> export', 'outputs': ['out'], 'nodes': [
            {'id': 'batch', 'type': 'temporal_batch', 'inputs': batch_spec(private_label='J12_BATCH')},
            {'id': 'mc', 'type': 'monte_carlo_reliability', 'depends_on': [{'node': 'batch', 'require': 'succeeded'}], 'inputs': mc_spec(samples=5000, private_label='J12_MC')},
            {'id': 'gate', 'type': 'review_gate', 'depends_on': ['mc'], 'input': 'mc'},
            {'id': 'out', 'type': 'export', 'depends_on': ['gate', {'node': 'mc', 'require': 'accepted_review'}], 'input': 'mc', 'fields': ['outcome', 'model_id', 'evidence_root', 'review_decision', 'envelope_digest']}]}
        rc, w = self.cli('owner', 'workflow-create', '--file', self.tmpjson('wf12.json', definition))
        rc, run = self.cli('owner', 'workflow-run', w['id'], '--budget-ceiling', '2')
        rid = run['run_id']
        self.start_worker('w-j12')
        deadline = time.time() + 300
        while time.time() < deadline:
            rc, st = self.cli('owner', 'run-status', rid)
            if st['state'] in ('waiting_review', 'completed', 'blocked', 'failed'):
                break
            time.sleep(1)
        mc_job = next(n['job_id'] for n in st['nodes'] if n['node_id'] == 'mc')
        rc, dec = self.cli('reviewer', 'decide', mc_job, 'accepted')
        rc, st2 = self.cli('owner', 'run-status', rid, '--follow', '--timeout', '120')
        campaign = {'name': 'j12 heat refinement', 'kind': 'heat_diffusion', 'base': heat_spec(nx=16, ny=16, steps=40, snapshots=0, initial={'type': 'sine_mode', 'm': 1, 'n': 1, 'amplitude': '1'}, private_label='J12_HEAT'), 'axes': [{'path': 'nx', 'values': [16, 32, 64]}]}
        rc, camp = self.cli('owner', 'campaign-create', '--file', self.tmpjson('c12.json', campaign))
        self.cli('owner', 'campaign-control', camp['campaign_id'], 'run')
        deadline = time.time() + 300
        while time.time() < deadline:
            rc, cs = self.cli('owner', 'campaign-status', camp['campaign_id'])
            if cs['state'] in ('completed', 'cancelled'):
                break
            time.sleep(1)
        self.stop_workers()
        rc, res = self.cli('owner', 'campaign-status', camp['campaign_id'], '--results')
        rows = res.get('rows', [])
        ok = st['state'] == 'waiting_review' and dec.get('decision') == 'accepted' and st2['state'] == 'completed' and cs['state'] == 'completed' and [r['state'] for r in rows] == ['succeeded'] * 3
        self.record(12, 'multi-step scientific workflow (batch -> monte carlo -> review gate -> export) and a heat refinement campaign through normal gates', 'passed' if ok else 'failed',
                    {'run': rid, 'state_before_review': st['state'], 'state_after_review': st2['state'], 'campaign': camp.get('campaign_id'), 'campaign_state': cs['state'], 'candidates': [(r['params'], r['state']) for r in rows]})

    def j13_two_worker_environments(self):
        """§53(6): a cpu-only worker (numpy-only interpreter, no torch) and the accelerator worker share the queue; gpu-required work
        is routed only to the cuda worker, cpu-required work to any worker, and both draw on the same workspace budget."""
        cpu_python = os.environ.get('METACOIN_CPU_ONLY_PYTHON')
        if not cpu_python or not os.path.exists(cpu_python):
            self.record(13, 'two worker environments (cpu-only interpreter + accelerator worker) with capability routing', 'blocked', {'reason': 'set METACOIN_CPU_ONLY_PYTHON to a numpy-only interpreter'}); return
        env_cpu = dict(self.env, METACOIN_COMPUTE_PYTHON=cpu_python)
        stop_c = Path(self.inst.temp.name) / 'stop-j13-cpu'
        wc = subprocess.Popen([PY, '-m', 'metacoin_service', '--home', str(self.inst.home), '--provider-mode', 'test-http', 'worker', '--name', 'w-j13-cpu-only', '--stop-file', str(stop_c)], cwd=ROOT, env=env_cpu, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(3)
        workers = {w['name']: w for w in self.http.get('/api/v1/workers', headers=self.H()).json()['items']}
        cpu_caps = [c for c in workers.get('w-j13-cpu-only', {}).get('capabilities', []) if c.startswith('device:')]
        jg = self.submit('temporal_batch', batch_spec(device_policy='gpu', private_label='J13_GPU'), 'j13g')
        jc = self.submit('heat_diffusion', heat_spec(nx=64, ny=64, steps=300, device_policy='cpu', private_label='J13_CPU'), 'j13c')
        time.sleep(4)                                                              # the cpu-only worker must take the cpu job and never the gpu job
        q = self.http.get('/api/v1/queue', headers=self.H()).json()
        reason_g = next((x['waiting_reason'] for x in q['queued'] if x['job_id'] == jg), None)
        vc_by = None
        vcj = self.wait(jc, lambda v: v['state'] in ('succeeded', 'failed'), timeout=60)
        with database.Database(self.inst.settings.db_path).read() as db:
            a = db.execute('SELECT worker_id FROM attempts WHERE job_id=? ORDER BY generation DESC LIMIT 1', (jc,)).fetchone()
            vc_by = workers_by_id = {w['id']: w['name'] for w in self.http.get('/api/v1/workers', headers=self.H()).json()['items']}.get(a['worker_id']) if a else None
        self.start_worker('w-j13-cuda')                                            # the accelerator worker arrives; the gpu job proceeds only now
        vg = self.wait(jg, lambda v: v['state'] in ('succeeded', 'failed'), timeout=120)
        with database.Database(self.inst.settings.db_path).read() as db:
            a = db.execute('SELECT worker_id FROM attempts WHERE job_id=? ORDER BY generation DESC LIMIT 1', (jg,)).fetchone()
            vg_by = {w['id']: w['name'] for w in self.http.get('/api/v1/workers', headers=self.H()).json()['items']}.get(a['worker_id']) if a else None
        stop_c.write_text('stop'); wc.wait(timeout=60); self.stop_workers()
        caps = self.http.get('/api/v1/compute/capabilities', headers=self.H()).json()['facts']['currently_available']
        ok = cpu_caps == ['device:cpu'] and reason_g == 'policy requires gpu; no live worker offers a cuda device' and vcj['state'] == 'succeeded' and vc_by == 'w-j13-cpu-only' and vg['state'] == 'succeeded' and vg['backend'] == 'cuda' and vg_by == 'w-j13-cuda'
        self.record(13, 'two worker environments (cpu-only interpreter + accelerator worker) with capability routing', 'passed' if ok else 'failed',
                    {'cpu_only_worker_devices': cpu_caps, 'gpu_job_waiting_reason_before_cuda_worker': reason_g, 'cpu_job': {'state': vcj['state'], 'backend': vcj['backend'], 'run_by': vc_by},
                     'gpu_job': {'state': vg['state'], 'backend': vg['backend'], 'run_by': vg_by}, 'cpu_only_interpreter': cpu_python},
                    caveat='same host, two processes: capability routing evidence, not multi-machine performance')

    def run_all(self, only=None):
        for fn in (self.j1_temporal_batch_cpu, self.j2_gpu_batch_vs_cpu, self.j3_long_job_progress_pause_resume, self.j4_kill_worker_recover_fenced, self.j5_monte_carlo_chunk_and_resume_invariance,
                   self.j6_heat_reject_unstable_verify_export, self.j7_capacity_race, self.j8_unauthorized_access, self.j9_purchase_over_x402, self.j10_lost_response_idempotent_recovery,
                   self.j11_cancel_with_unresolved_economic_state, self.j12_workflow_and_refinement_campaign, self.j13_two_worker_environments):
            if only and int(fn.__name__[1:].split('_')[0]) not in only:
                continue
            try:
                fn()
            except Exception as exc:
                self.stop_workers()
                self.results.append({'journey': fn.__name__, 'status': 'error', 'error': repr(exc)[:400]})
                print('[?] ERROR', fn.__name__, repr(exc)[:300], flush=True)
        return self.results


def main():
    p = argparse.ArgumentParser(); p.add_argument('--out'); p.add_argument('--only', help='comma-separated journey numbers'); a = p.parse_args()
    j = Journeys()
    try:
        results = j.run_all([int(x) for x in a.only.split(',')] if a.only else None); health = j.http.get('/api/health').json()
    finally:
        j.close()
    out = {'provider_mode': 'test-http', 'revision': health.get('revision'), 'compute_interpreter': RUNTIME, 'results': results,
           'passed': sum(r['status'] == 'passed' for r in results), 'blocked': sum(r['status'] == 'blocked' for r in results), 'total': len(results)}
    text = json.dumps(out, indent=1)
    if a.out:
        Path(a.out).write_text(text)
    print(text)
    return 0 if out['passed'] + out['blocked'] == out['total'] else 1


if __name__ == '__main__':
    sys.exit(main())
