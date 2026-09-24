"""Application-side compute operations: capability facts, run views, controls, authorized output access,
checkpoint listings, bounded logs, telemetry, reproducibility bundles and a heat-field SVG."""
import json
import html
from . import container, manifests, npy
from .. import history
from ..db import now
from ..errors import ServiceError

PLOT_MAX_CELLS = 64 * 64


def capabilities(db, settings):
    from .engine import compute_interpreter
    rt = compute_interpreter(settings)
    workers = [dict(w) for w in db.execute('SELECT * FROM workers')]
    from ..scheduling import live
    live_devices = sorted({c[7:] for w in workers if live(w) and w['state'] == 'active' for c in json.loads(w['capabilities_json']) if c.startswith('device:')})
    reservations = [dict(r) for r in db.execute('SELECT device, COUNT(*) AS n FROM compute_reservations WHERE expires_at > ? GROUP BY device', (now(),))]
    observed = {r['selected_backend']: r['n'] for r in db.execute("SELECT selected_backend, COUNT(*) AS n FROM compute_runs WHERE phase='completed' AND selected_backend IS NOT NULL GROUP BY selected_backend")}
    return {'manifests': {k: manifests.manifest(k) for k in manifests.KINDS},
            'interpreter': ({k: v for k, v in rt.items()} if rt else None),
            'facts': {'installed': {'numpy': bool(rt), 'torch': bool(rt and rt.get('torch')), 'cuda_available_in_interpreter': bool(rt and rt.get('cuda'))},
                      'configured': {'gpu_slots': settings.limits['compute_gpu_slots'], 'cpu_slots': settings.limits['compute_cpu_slots'], 'threads': settings.limits['compute_threads']},
                      'currently_available': {'live_worker_devices': live_devices, 'reservations_in_use': reservations},
                      'observed_running': {'completed_runs_by_backend': observed, 'gpu_verified': observed.get('cuda', 0) > 0}},
            'note': 'gpu_verified means at least one job completed and verified on the cuda backend on this instance; it is not set by device detection alone'}


def _run(db, principal, jobs, job_id):
    job = jobs.get(db, principal, job_id)
    run = db.execute('SELECT * FROM compute_runs WHERE job_id=?', (job_id,)).fetchone()
    if run is None:
        raise ServiceError('NOT_FOUND', 'not a compute job')
    return job, run


def view(db, principal, jobs, job_id):
    job, run = _run(db, principal, jobs, job_id)
    private = principal.can('job:read_private')
    ckpts = [dict(r) for r in db.execute('SELECT generation, attempt_generation, committed_units, backend, state, published_at FROM compute_checkpoints WHERE job_id=? ORDER BY generation', (job_id,))]
    out = {'job_id': job_id, 'kind': job['kind'], 'state': job['state'], 'review_state': job['review_state'], 'phase': run['phase'], 'device_policy': run['device_policy'],
           'backend': run['selected_backend'], 'backend_reason': run['backend_reason'], 'precision': run['precision'], 'manifest_id': run['manifest_id'], 'manifest_version': run['manifest_version'],
           'implementation_digest': run['implementation_digest'], 'work': {'total': run['work_total'], 'committed': run['work_committed'], 'computed': run['work_computed'],
                                                                           'unit': manifests.MANIFESTS[job['kind']]['work_unit'],
                                                                           'percent_committed': (100 * run['work_committed'] // run['work_total']) if run['work_total'] else None},
           'checkpoint_generation': run['checkpoint_generation'], 'checkpoints': ckpts, 'control': run['control'], 'hold': bool(job['hold']),
           'preempted_for': run['preempted_for'] if 'preempted_for' in run.keys() else None,
           'progress': json.loads(run['progress_json']) if run['progress_json'] else None, 'versions': json.loads(run['versions_json']) if run['versions_json'] else None,
           'verification': json.loads(run['verification_json']) if run['verification_json'] else None, 'started_at': run['started_at'], 'updated_at': run['updated_at'],
           'allowed_actions': allowed_actions(job, run, principal), 'economic_state': jobs.payment_view(db, principal, job)['state'], 'outcome': job['outcome'] if private else None}
    if private:
        out['telemetry'] = json.loads(run['telemetry_json']) if run['telemetry_json'] else None
        out['output_artifact_id'] = run['output_artifact_id']
    else:
        out['verification'] = {'mode': out['verification'].get('mode'), 'passed': out['verification'].get('passed')} if out['verification'] else None
    return out


def allowed_actions(job, run, principal):
    acts = []
    if not principal.can('job:cancel'):
        return acts
    if job['state'] == 'running' and run['phase'] in ('running', 'checkpointing', 'initializing') and not run['control']:
        acts += ['pause', 'cancel']
    if job['state'] == 'queued' and job['hold']:
        acts += ['resume', 'cancel']
    elif job['state'] == 'queued':
        acts += ['cancel']
    return acts


def control(db, principal, jobs, job_id, action):
    principal.require('job:cancel')
    job, run = _run(db, principal, jobs, job_id)
    if action == 'pause':
        if job['state'] != 'running':
            raise ServiceError('CONFLICT', 'only a running compute job can be paused')
        db.execute("UPDATE compute_runs SET control='pause', controlled_at=?, updated_at=? WHERE job_id=?", (now(), now(), job_id))
    elif action == 'resume':
        if not (job['state'] == 'queued' and job['hold']):
            raise ServiceError('CONFLICT', 'only a paused job can be resumed')
        db.execute("UPDATE jobs SET hold=0, updated_at=? WHERE id=?", (now(), job_id))
        db.execute("UPDATE compute_runs SET phase='admitted', control=NULL, updated_at=? WHERE job_id=?", (now(), job_id))
    elif action == 'cancel':
        if job['state'] == 'queued':
            jobs.cancel(db, principal, job_id)
            db.execute("UPDATE compute_runs SET phase='cancelled', updated_at=? WHERE job_id=?", (now(), job_id))
        elif job['state'] == 'running':
            db.execute("UPDATE jobs SET cancel_requested=1, updated_at=? WHERE id=?", (now(), job_id))
            db.execute("UPDATE compute_runs SET control='cancel', controlled_at=?, updated_at=? WHERE job_id=?", (now(), now(), job_id))
        else:
            raise ServiceError('CONFLICT', 'job is terminal')
    else:
        raise ServiceError('VALIDATION', {'code': 'action', 'allowed': ['pause', 'resume', 'cancel']})
    history.record(db, principal.workspace, principal.id, 'compute.control', 'job', job_id, {'action': action})
    return view(db, principal, jobs, job_id)


def _private_ok(db, principal, job):
    if principal.can('job:read_private'):
        return True
    c = db.execute('SELECT reviewer_id FROM contracts WHERE id=?', (job['contract_id'],)).fetchone()
    return principal.role == 'reviewer' and c is not None and c['reviewer_id'] == principal.id


def outputs(db, principal, jobs, store, job_id, name=None):
    job, run = _run(db, principal, jobs, job_id)
    if not _private_ok(db, principal, job):
        raise ServiceError('FORBIDDEN', 'private compute outputs')
    if not run['output_artifact_id']:
        raise ServiceError('CONFLICT', 'no committed outputs')
    blob = store.load(db, run['output_artifact_id'], principal.workspace)
    if name is None:
        return {'job_id': job_id, 'artifact_id': run['output_artifact_id'], 'files': container.listing(blob), 'format': 'npy files are NumPy format 1.0 (int64/float64, C order, no pickles)'}
    files = container.unpack(blob)
    if name not in files:
        raise ServiceError('NOT_FOUND', 'output')
    history.record(db, principal.workspace, principal.id, 'artifact.exported', 'artifact', run['output_artifact_id'], {'compute_output': name})
    return files[name], ('application/octet-stream' if name.endswith('.npy') else 'application/json')


def checkpoints(db, principal, jobs, job_id):
    job, run = _run(db, principal, jobs, job_id)
    if not _private_ok(db, principal, job):
        raise ServiceError('FORBIDDEN', 'checkpoints are private')
    rows = [dict(r) for r in db.execute('SELECT * FROM compute_checkpoints WHERE job_id=? ORDER BY generation', (job_id,))]
    for r in rows:
        r['boundary'] = json.loads(r.pop('boundary_json'))
    units = [dict(r) for r in db.execute('SELECT unit_from, unit_to, generation, attempt_generation, committed_at FROM compute_work_units WHERE job_id=? ORDER BY unit_from', (job_id,))]
    return {'job_id': job_id, 'checkpoints': rows, 'committed_work_units': units, 'retained_generations': None}


def log_tail(db, principal, jobs, job_id):
    job, run = _run(db, principal, jobs, job_id)
    if not _private_ok(db, principal, job):
        raise ServiceError('FORBIDDEN', 'logs are private')
    return {'job_id': job_id, 'log_tail': run['log_tail'] or '', 'bounded_bytes': True}


def reproducibility(db, principal, jobs, job_id, settings):
    job, run = _run(db, principal, jobs, job_id)
    contract = db.execute('SELECT * FROM contracts WHERE id=?', (job['contract_id'],)).fetchone()
    man = manifests.manifest(job['kind'])
    vault_root = job['evidence_root']
    out = {'schema': 'metacoin-compute-reproducibility/v1', 'job_id': job_id, 'kind': job['kind'], 'manifest': {k: man[k] for k in ('manifest_id', 'version', 'model_id', 'numerical_policy', 'precision', 'checkpoint_format', 'verification_policy')},
           'accepted': {'implementation_digest': run['implementation_digest'], 'manifest_version': run['manifest_version'], 'input_digest_private': 'internal; not exported', 'input_root': contract['input_root'],
                        'contract_digest': contract['contract_digest'], 'device_policy': run['device_policy'], 'precision': run['precision']},
           'execution': {'backend': run['selected_backend'], 'versions': json.loads(run['versions_json']) if run['versions_json'] else None, 'checkpoint_generations': run['checkpoint_generation'], 'work_committed': run['work_committed']},
           'evidence_root': vault_root, 'verification': json.loads(run['verification_json']) if run['verification_json'] else None,
           'equality_policy': {'temporal_batch': 'exact integer equality of every result column', 'monte_carlo_reliability': 'exact equality of per-sample outcomes and counts for the same seed, sample count and implementation',
                               'heat_diffusion': 'max-norm difference <= 1e-9 (abs) + 1e-9 * field scale across backends and library versions; bitwise identity is not promised'}[job['kind']],
           'how_to_verify': 'export the outputs (npy/json) and re-run the reference implementation in metacoin_service.compute.reference / temporal.analyze in a CPU-only environment; the reference needs no GPU',
           'limits': 'the bundle identifies bytes and implementations; it does not reconstruct private inputs from commitments and is not an attestation of hardware'}
    if not principal.can('job:read_private'):
        out.pop('execution')
    return out


def heat_svg(db, principal, jobs, store, job_id):
    job, run = _run(db, principal, jobs, job_id)
    if job['kind'] != 'heat_diffusion':
        raise ServiceError('VALIDATION', 'not a heat job')
    if not _private_ok(db, principal, job):
        raise ServiceError('FORBIDDEN', 'private field')
    if not run['output_artifact_id']:
        raise ServiceError('CONFLICT', 'no committed field')
    files = container.unpack(store.load(db, run['output_artifact_id'], principal.workspace))
    vals, dtype, shape = npy.decode(files['field.npy'])
    ny, nx = shape
    summary = json.loads(store.load_json(db, job['evidence_artifact_id'], principal.workspace)['fields'][0]['value'] if False else '{}') if False else None
    lo, hi = min(vals), max(vals)
    stride = max(1, int((nx * ny / PLOT_MAX_CELLS) ** 0.5) + (1 if nx * ny > PLOT_MAX_CELLS else 0))
    cols = list(range(0, nx, stride)); rows = list(range(0, ny, stride))
    cell = max(2, 480 // max(len(cols), len(rows)))
    w, h = cell * len(cols), cell * len(rows)
    parts = ['<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" viewBox="0 0 %d %d">' % (w + 160, h + 60, w + 160, h + 60),
             '<title>heat field, job %s: %d x %d float64, value range [%.6g, %.6g], shared scale (min=blue, max=red)</title>' % (html.escape(job_id), nx, ny, lo, hi)]
    span = (hi - lo) or 1.0
    for rj, j in enumerate(rows):
        for ci, i in enumerate(cols):
            v = (vals[j * nx + i] - lo) / span
            r, g, b = int(255 * v), int(80 * (1 - abs(2 * v - 1))), int(255 * (1 - v))
            parts.append('<rect x="%d" y="%d" width="%d" height="%d" fill="rgb(%d,%d,%d)"><title>x=%d y=%d value=%.6g</title></rect>' % (ci * cell, (len(rows) - 1 - rj) * cell, cell, cell, r, g, b, i, j, vals[j * nx + i]))
    parts.append('<text x="%d" y="16" font-size="11" font-family="sans-serif">field[y][x], y up; %dx%d; stride %d</text>' % (w + 8, nx, ny, stride))
    parts.append('<text x="%d" y="32" font-size="11" font-family="sans-serif">min %.6g (blue)</text><text x="%d" y="48" font-size="11" font-family="sans-serif">max %.6g (red)</text>' % (w + 8, lo, w + 8, hi))
    parts.append('<text x="4" y="%d" font-size="11" font-family="sans-serif">model output (FTCS), not a measurement; values labelled per cell</text></svg>' % (h + 40))
    return ''.join(parts)
