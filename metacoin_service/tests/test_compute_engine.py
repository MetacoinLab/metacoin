"""Compute engine lifecycle through normal entry points: CPU and CUDA execution, checkpoints, pause/resume, cancel,
worker termination with fenced recovery, corrupted and foreign checkpoints, access refusals, quotes and usage."""
import json
import os
import signal
import subprocess
import sys
import threading
import time
import unittest
from metacoin_service.tests.test_service import Instance, ROOT, ENV
from metacoin_service.tests.test_compute_science import base_temporal
from metacoin_service.compute import inputs, container, npy
from metacoin_service.compute.engine import compute_interpreter
from metacoin_service import db as database
from metacoin_service.db import now

PY = sys.executable


def heat_spec(nx=384, ny=384, steps=8000, **over):
    d = {'schema': inputs.HEAT_SCHEMA, 'nx': nx, 'ny': ny, 'dx': '0.01', 'dy': '0.01', 'dt': '0.00002', 'alpha': '1.0', 'steps': steps,
         'boundary': {'type': 'dirichlet', 'values': {'left': '0', 'right': '0', 'top': '0', 'bottom': '0'}},
         'initial': {'type': 'gaussian', 'center_x': '1.92', 'center_y': '1.92', 'sigma': '0.4', 'amplitude': '100', 'background': '0'}, 'snapshots': 2,
         'units': {'field': 'K', 'length': 'm', 'time': 's'}, 'device_policy': 'cpu', 'precision': 'float64', 'private_label': 'HEAT_ENGINE_SYNTHETIC'}
    d.update(over)
    return d


def batch_spec(**over):
    d = {'schema': inputs.TEMPORAL_BATCH_SCHEMA, 'base': base_temporal(), 'scenarios': None,
         'grid': [{'path': 'reserve', 'start': 0, 'stop': 9000, 'step': 100}, {'path': 'load_scale_percent', 'values': [50, 100, 150, 200]}],
         'device_policy': 'cpu', 'verification': 'auto', 'private_label': 'BATCH_ENGINE_SYNTHETIC'}
    d.update(over)
    return d


def mc_spec(**over):
    d = {'schema': inputs.MONTE_CARLO_SCHEMA, 'base': base_temporal(initial_low=6000, initial_high=6000, segments=[
             {'duration': 10, 'harvest_low': 600, 'harvest_high': 600, 'load_low': 500, 'load_high': 500, 'leakage_low': 0, 'leakage_high': 0},
             {'duration': 30, 'harvest_low': 0, 'harvest_high': 0, 'load_low': 150, 'load_high': 150, 'leakage_low': 0, 'leakage_high': 0}]),
         'distributions': {'initial_energy': {'type': 'finite', 'values': [4000, 6000, 8000], 'weights': [1, 2, 1]}, 'load_scale_percent': {'type': 'uniform_int', 'low': 50, 'high': 200}},
         'samples': 20000, 'seed': 11, 'confidence_percent': 95, 'event': 'reserve_maintained', 'device_policy': 'cpu', 'private_label': 'MC_ENGINE_SYNTHETIC'}
    d.update(over)
    return d


class ComputeInstance(Instance):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.settings.limits['compute_checkpoint_interval_seconds'] = 1
        self.reopen()

    def compute_job(self, kind, spec, title='compute'):
        r = self.client.post('/api/v1/contracts', headers=self.h('owner'), json={'kind': kind, 'title': title, 'inputs': spec, 'policy': {'reviewer_id': self.ids['reviewer']}})
        assert r.status_code == 201, r.text
        cid = r.json()['id']
        assert self.client.post('/api/v1/contracts/' + cid + '/freeze', headers=self.h('owner')).status_code == 200
        r = self.client.post('/api/v1/jobs', headers=self.h('owner'), json={'contract_id': cid})
        assert r.status_code == 202, r.text
        return r.json()['id']

    def view(self, jid, role='owner'):
        return self.client.get('/api/v1/compute/jobs/' + jid, headers=self.h(role)).json()

    def wait_phase(self, jid, pred, timeout=60):
        deadline = time.time() + timeout
        while time.time() < deadline:
            v = self.view(jid)
            if pred(v):
                return v
            time.sleep(0.2)
        return self.view(jid)


RUNTIME = compute_interpreter(type('S', (), {'compute_python': ''})())
HAVE_RUNTIME = bool(RUNTIME)
HAVE_CUDA = bool(RUNTIME and RUNTIME.get('cuda'))


@unittest.skipUnless(HAVE_RUNTIME, 'no numpy-capable compute interpreter on this host')
class ComputeEngineTests(unittest.TestCase):
    def setUp(self):
        self.inst = ComputeInstance(); self.addCleanup(self.inst.close); self.c = self.inst.client; self.H = self.inst.h('owner')

    def test_cpu_batch_and_monte_carlo_complete_verified_with_private_outputs(self):
        jb = self.inst.compute_job('temporal_batch', batch_spec()); jm = self.inst.compute_job('monte_carlo_reliability', mc_spec())
        w = self.inst.worker()
        self.assertEqual(w.run_once()[1], 'succeeded'); self.assertEqual(w.run_once()[1], 'succeeded')
        vb, vm = self.inst.view(jb), self.inst.view(jm)
        self.assertEqual((vb['state'], vb['phase'], vb['backend'], vb['verification']['mode'], vb['verification']['passed'], vb['work']['committed']), ('succeeded', 'completed', 'cpu', 'exact_all', True, 364))
        self.assertEqual((vm['state'], vm['backend'], vm['verification']['mode'], vm['verification']['passed'], vm['work']['committed']), ('succeeded', 'cpu', 'sampled_audit', True, 20000))
        job = self.c.get('/api/v1/jobs/' + jm, headers=self.H).json()
        s = job['summary']
        self.assertEqual((job['outcome'], s['samples'], s['interval']['method'], s['stopping_policy'][:5]), ('VERIFIED', 20000, 'wilson_score', 'fixed'))
        p, lo, hi = float(s['probability_estimate']), float(s['interval']['low']), float(s['interval']['high'])
        self.assertTrue(0 < p < 1 and lo <= p <= hi, s['interval'])
        # outputs: owner and assigned reviewer only; npy readable by an ordinary client; viewer and other roles refused
        files = self.c.get('/api/v1/compute/jobs/' + jb + '/outputs', headers=self.H).json()['files']
        self.assertEqual(sorted(f['name'] for f in files), ['results.json', 'results.npy'])
        raw = self.c.get('/api/v1/compute/jobs/' + jb + '/outputs/results.npy', headers=self.H).content
        vals, dtype, shape = npy.decode(raw)
        self.assertEqual((dtype, shape), ('<i8', [364, 17]))
        for role, code in (('viewer', 403), ('reviewer', 200), ('worker', 403)):
            self.assertEqual(self.c.get('/api/v1/compute/jobs/' + jb + '/outputs/results.npy', headers=self.inst.h(role)).status_code, code, role)
            self.assertEqual(self.c.get('/api/v1/compute/jobs/' + jb + '/checkpoints', headers=self.inst.h(role)).status_code, code, role)
            self.assertEqual(self.c.get('/api/v1/compute/jobs/' + jb + '/log', headers=self.inst.h(role)).status_code, code, role)
        vv = self.inst.view(jb, 'viewer')
        self.assertNotIn('telemetry', vv); self.assertNotIn('output_artifact_id', vv); self.assertEqual(vv['verification'], {'mode': 'exact_all', 'passed': True})
        self.assertNotIn('BATCH_ENGINE_SYNTHETIC', json.dumps(self.c.get('/api/v1/compute/jobs/' + jb + '/reproducibility', headers=self.inst.h('viewer')).json()))
        # usage: quantity = committed work units under a quote (simulation invoke path)
        sid = next(s['id'] for s in self.c.get('/api/v1/services', headers=self.H).json()['items'] if s['kind'] == 'temporal_batch')
        q = self.c.post('/api/v1/services/' + sid + '/quote', headers=self.H, json={'inputs': batch_spec(private_label='QUOTED')})
        self.assertEqual((q.status_code, q.json()['quantity_max'], q.json()['amount_max']), (201, 364, 364), q.text)
        self.assertEqual(self.c.post('/api/v1/services/' + sid + '/quote', headers=self.H, json={'inputs': batch_spec(private_label='QUOTED'), 'quantity_max': 10}).json()['detail']['code'], 'quantity_below_work_estimate')
        self.c.post('/api/v1/quotes/' + q.json()['quote_id'] + '/accept', headers=self.H)
        inv = self.c.post('/api/v1/services/' + sid + '/invoke', headers=self.H, json={'quote_id': q.json()['quote_id'], 'inputs': batch_spec(private_label='QUOTED')}).json()
        self.assertEqual(w.run_once()[1], 'succeeded')
        usage = [u for u in self.c.get('/api/v1/usage', headers=self.H).json()['items'] if u['job_id'] == inv['job_id']]
        self.assertEqual((usage[0]['quantity'], usage[0]['assessed_charge'], usage[0]['signature_valid']), (364, 364, True))

    @unittest.skipUnless(HAVE_CUDA, 'no CUDA device in the compute interpreter')
    def test_gpu_required_batch_matches_cpu_reference_and_reports_the_device(self):
        jg = self.inst.compute_job('temporal_batch', batch_spec(device_policy='gpu'))
        w = self.inst.worker()
        self.assertIn('device:cuda', w.capabilities)
        self.assertEqual(w.run_once()[1], 'succeeded')
        v = self.inst.view(jg)
        self.assertEqual((v['backend'], v['verification']['passed'], v['verification']['mode']), ('cuda', True, 'exact_all'))
        self.assertIn('NVIDIA', v['versions']['versions'].get('device', ''))
        self.assertIsNotNone(v['versions'].get('energy_counter_start_mJ'))
        job = self.c.get('/api/v1/jobs/' + jg, headers=self.H).json()
        self.assertEqual(job['summary']['backend'], 'cuda')
        self.assertTrue(job['summary'].get('peak_device_bytes', 0) > 0)
        # the same batch on cpu gives byte-identical results (exact integer semantics)
        jc = self.inst.compute_job('temporal_batch', batch_spec(device_policy='cpu'))
        self.assertEqual(w.run_once()[1], 'succeeded')
        self.assertEqual(self.c.get('/api/v1/compute/jobs/' + jg + '/outputs/results.npy', headers=self.H).content,
                         self.c.get('/api/v1/compute/jobs/' + jc + '/outputs/results.npy', headers=self.H).content)
        caps = self.c.get('/api/v1/compute/capabilities', headers=self.H).json()
        self.assertTrue(caps['facts']['observed_running']['gpu_verified'])

    def test_gpu_required_waits_when_no_cuda_worker_and_cpu_only_worker_cannot_take_it(self):
        jg = self.inst.compute_job('temporal_batch', batch_spec(device_policy='gpu'))
        w = self.inst.worker()
        w.compute.devices = ['cpu']; w.capabilities = [c for c in w.capabilities if c != 'device:cuda']
        with database.Database(self.inst.settings.db_path).tx() as db:
            db.execute('UPDATE workers SET capabilities_json=? WHERE id=?', (json.dumps(w.capabilities), w.worker_id))
        self.assertIsNone(w.run_once())
        q = self.c.get('/api/v1/queue', headers=self.H).json()
        self.assertEqual(q['queued'][0]['waiting_reason'], 'policy requires gpu; no live worker offers a cuda device')
        self.assertEqual(self.inst.view(jg)['state'], 'queued')

    def test_heat_unstable_refused_and_stable_run_verified_with_checkpoints_pause_resume(self):
        r = self.c.post('/api/v1/contracts', headers=self.H, json={'kind': 'heat_diffusion', 'title': 'bad', 'inputs': heat_spec(dt='0.0001'), 'policy': {}})
        self.assertEqual(r.status_code, 422); self.assertIn('unstable timestep', r.text)
        jid = self.inst.compute_job('heat_diffusion', heat_spec())
        w = self.inst.worker()
        t = threading.Thread(target=w.run_once); t.start()
        v = self.inst.wait_phase(jid, lambda v: v['phase'] == 'running' and (v['progress'] or {}).get('computed', 0) > 0, timeout=90)
        self.assertEqual(v['phase'], 'running', v)
        self.assertIn('pause', v['allowed_actions'])
        self.assertEqual(self.c.post('/api/v1/compute/jobs/' + jid + '/pause', headers=self.inst.h('viewer')).status_code, 403)
        self.assertEqual(self.c.post('/api/v1/compute/jobs/' + jid + '/pause', headers=self.H).status_code, 200)
        t.join(timeout=120)
        v = self.inst.view(jid)
        self.assertEqual((v['state'], v['phase'], v['hold']), ('queued', 'paused', True), v)
        self.assertGreater(v['checkpoint_generation'], 0)
        self.assertGreater(v['work']['committed'], 0); self.assertLess(v['work']['committed'], v['work']['total'])
        self.assertEqual(self.c.get('/api/v1/queue', headers=self.H).json()['queued'][0]['waiting_reason'], 'paused at a durable checkpoint; resume to continue')
        with database.Database(self.inst.settings.db_path).read() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM compute_reservations').fetchone()[0], 0)      # operational capacity released
        self.assertIsNone(w.run_once())                                                                       # a held job is not claimed
        self.assertIn('resume', v['allowed_actions'])
        self.assertEqual(self.c.post('/api/v1/compute/jobs/' + jid + '/resume', headers=self.H).json()['phase'], 'admitted')
        res = w.run_once()
        self.assertEqual(res[1], 'succeeded', self.inst.view(jid))
        v = self.inst.view(jid)
        self.assertEqual((v['state'], v['phase'], v['verification']['passed'], v['work']['committed']), ('succeeded', 'completed', True, v['work']['total']))
        self.assertTrue(any(c['check'] == 'last_step_recomputed' and c['ok'] for c in v['verification']['checks']))
        gens = [c['generation'] for c in v['checkpoints']]
        self.assertEqual(gens, sorted(gens)); self.assertGreaterEqual(len(gens), 1)
        # resumed result equals an uninterrupted run under the declared tolerance
        jid2 = self.inst.compute_job('heat_diffusion', heat_spec())
        w.run_once()
        a = npy.decode(self.c.get('/api/v1/compute/jobs/' + jid + '/outputs/field.npy', headers=self.H).content)[0]
        b = npy.decode(self.c.get('/api/v1/compute/jobs/' + jid2 + '/outputs/field.npy', headers=self.H).content)[0]
        self.assertLess(max(abs(x - y) for x, y in zip(a, b)), 1e-9 * 100)
        # committed work units are contiguous and never overlap (no double counting across the pause)
        units = self.c.get('/api/v1/compute/jobs/' + jid + '/checkpoints', headers=self.H).json()['committed_work_units']
        self.assertTrue(all(units[i]['unit_to'] == units[i + 1]['unit_from'] for i in range(len(units) - 1)), units)
        self.assertEqual((units[0]['unit_from'], units[-1]['unit_to']), (0, v['work']['total']))
        svg = self.c.get('/api/v1/compute/jobs/' + jid + '/plot.svg', headers=self.H)
        self.assertEqual((svg.status_code, svg.headers['content-type'].split(';')[0]), (200, 'image/svg+xml'))
        self.assertEqual(self.c.get('/api/v1/compute/jobs/' + jid + '/plot.svg', headers=self.inst.h('viewer')).status_code, 403)

    def test_cancel_during_run_keeps_checkpoint_evidence_and_reports_cancelled(self):
        jid = self.inst.compute_job('heat_diffusion', heat_spec())
        w = self.inst.worker()
        t = threading.Thread(target=w.run_once); t.start()
        self.inst.wait_phase(jid, lambda v: v['phase'] == 'running' and (v['progress'] or {}).get('computed', 0) > 0, timeout=90)
        self.assertEqual(self.c.post('/api/v1/compute/jobs/' + jid + '/cancel', headers=self.H).status_code, 200)
        t.join(timeout=120)
        v = self.inst.view(jid)
        self.assertEqual((v['state'], v['phase']), ('cancelled', 'cancelled'), v)
        self.assertGreaterEqual(len(v['checkpoints']), 1)
        self.assertEqual(self.c.get('/api/v1/compute/jobs/' + jid + '/outputs', headers=self.H).status_code, 409)     # no committed outputs
        self.assertEqual(v['allowed_actions'], [])

    def test_worker_killed_mid_run_recovers_from_the_last_published_checkpoint_under_a_new_generation(self):
        jid = self.inst.compute_job('heat_diffusion', heat_spec())
        env = dict(ENV, METACOIN_LIMITS_JSON=json.dumps({'compute_checkpoint_interval_seconds': 1}))
        w1 = subprocess.Popen([PY, '-m', 'metacoin_service', '--home', str(self.inst.home), 'worker', '--once', '--name', 'victim'], cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        v = self.inst.wait_phase(jid, lambda v: v['checkpoint_generation'] >= 1 and v['state'] == 'running', timeout=90)
        self.assertGreaterEqual(v['checkpoint_generation'], 1, v)
        gen_before, committed_before = v['checkpoint_generation'], v['work']['committed']
        os.kill(w1.pid, signal.SIGKILL); w1.wait(timeout=10)
        with database.Database(self.inst.settings.db_path).tx() as db:
            db.execute('UPDATE jobs SET lease_expires=? WHERE id=?', (now() - 1, jid))                      # the dead worker's lease expires (persisted marker)
            old_gen = db.execute('SELECT lease_generation FROM jobs WHERE id=?', (jid,)).fetchone()[0]
        time.sleep(1.5)                                                                                       # the orphaned child loses its pipe and exits
        w2 = self.inst.worker()
        res = w2.run_once()
        self.assertEqual(res[1], 'succeeded', self.inst.view(jid))
        v = self.inst.view(jid)
        self.assertEqual((v['state'], v['verification']['passed'], v['work']['committed']), ('succeeded', True, v['work']['total']))
        with database.Database(self.inst.settings.db_path).read() as db:
            self.assertEqual(db.execute('SELECT lease_generation FROM jobs WHERE id=?', (jid,)).fetchone()[0], old_gen + 1)
            attempts = [dict(r) for r in db.execute('SELECT generation, outcome FROM attempts WHERE job_id=? ORDER BY generation', (jid,))]
        self.assertEqual([a['outcome'] for a in attempts][-1], 'succeeded')
        hist = self.c.get('/api/v1/jobs/' + jid + '/history', headers=self.H).json()
        rows = hist if isinstance(hist, list) else next(x for x in hist.values() if isinstance(x, list))
        resumed = [json.loads(e['ref_json']) if 'ref_json' in e else e['ref'] for e in rows if e['event_type'] == 'compute.control']
        self.assertTrue(any(r.get('resumed_from_generation') == gen_before for r in resumed), resumed)
        units = self.c.get('/api/v1/compute/jobs/' + jid + '/checkpoints', headers=self.H).json()['committed_work_units']
        self.assertTrue(all(units[i]['unit_to'] == units[i + 1]['unit_from'] for i in range(len(units) - 1)), units)
        self.assertEqual(units[-1]['unit_to'], v['work']['total'])
        self.assertTrue(any(u['unit_to'] == committed_before for u in units))                                # the replayed chunk was not billed twice

    def test_corrupted_or_foreign_checkpoint_is_refused_safely(self):
        jid = self.inst.compute_job('heat_diffusion', heat_spec())
        w = self.inst.worker()
        t = threading.Thread(target=w.run_once); t.start()
        self.inst.wait_phase(jid, lambda v: v['checkpoint_generation'] >= 1, timeout=90)
        self.c.post('/api/v1/compute/jobs/' + jid + '/pause', headers=self.H); t.join(timeout=120)
        v = self.inst.view(jid); self.assertEqual(v['phase'], 'paused')
        # corrupt the newest checkpoint object on disk
        with database.Database(self.inst.settings.db_path).tx() as db:
            ck = db.execute("SELECT artifact_id FROM compute_checkpoints WHERE job_id=? ORDER BY generation DESC LIMIT 1", (jid,)).fetchone()
            name = db.execute('SELECT storage_name FROM artifacts WHERE id=?', (ck['artifact_id'],)).fetchone()['storage_name']
        path = self.inst.settings.home / 'artifacts' / name
        data = bytearray(path.read_bytes()); data[len(data) // 2] ^= 0xFF; path.write_bytes(bytes(data))
        self.c.post('/api/v1/compute/jobs/' + jid + '/resume', headers=self.H)
        res = w.run_once()
        self.assertEqual(res[1], 'failed')
        j = self.c.get('/api/v1/jobs/' + jid, headers=self.H).json()
        self.assertEqual(j['error_code'], 'CHECKPOINT_INVALID')
        self.assertIn('CHECKPOINT_INVALID', json.dumps(self.inst.view(jid)['verification']))
        # a checkpoint that binds another job is refused inside the child too
        j2 = self.inst.compute_job('heat_diffusion', heat_spec())
        with database.Database(self.inst.settings.db_path).tx() as db:
            other = db.execute("SELECT artifact_id, digest, committed_units, boundary_json, backend FROM compute_checkpoints WHERE job_id=? ORDER BY generation LIMIT 1", (jid,)).fetchone()
            db.execute("INSERT INTO compute_checkpoints (id, job_id, generation, attempt_generation, artifact_id, committed_units, boundary_json, backend, digest, state, published_at) VALUES ('ck_foreign', ?, 1, 0, ?, ?, ?, ?, ?, 'published', ?)",
                       (j2, other['artifact_id'], other['committed_units'], other['boundary_json'], other['backend'], other['digest'], now()))
        res = w.run_once()
        self.assertEqual(res[1], 'failed')
        self.assertEqual(self.c.get('/api/v1/jobs/' + j2, headers=self.H).json()['error_code'], 'CHECKPOINT_INVALID')

    def test_capacity_race_respects_one_slot_and_headroom(self):
        self.inst.settings.limits['compute_cpu_slots'] = 1
        ja = self.inst.compute_job('heat_diffusion', heat_spec())
        wa, wb = self.inst.worker(), self.inst.worker()
        ta = threading.Thread(target=wa.run_once); ta.start()
        va = self.inst.wait_phase(ja, lambda v: v['phase'] in ('running', 'initializing'), timeout=60)
        jb = self.inst.compute_job('heat_diffusion', heat_spec(steps=200))                              # arrives while the only cpu slot is taken
        with database.Database(self.inst.settings.db_path).read() as db:
            snapshot = {'va_phase': va['phase'], 'reservations': [dict(r) for r in db.execute('SELECT * FROM compute_reservations')], 'jobs': [dict(r) for r in db.execute('SELECT id, state, lease_expires FROM jobs')], 'now': now()}
        self.assertIsNone(wb.run_once(), snapshot)                                                            # the second job waits: one cpu slot in use
        q = self.c.get('/api/v1/queue', headers=self.H).json()
        self.assertEqual([x['job_id'] for x in q['queued']], [jb])
        self.assertEqual(q['queued'][0]['waiting_reason'], 'waiting for a free cpu compute slot')
        ta.join(timeout=180)
        self.assertEqual(wb.run_once()[0], jb)


if __name__ == '__main__':
    unittest.main()
