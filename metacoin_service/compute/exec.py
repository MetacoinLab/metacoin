"""Task-owned child process for one compute attempt.

    python -m metacoin_service.compute.exec WORKDIR

Reads WORKDIR/spec.json (written by the worker), executes the allowlisted kernel for the accepted kind in
bounded chunks, and speaks a line protocol on stdout/stdin:

    -> {"event":"started", backend, versions, pid, energy_counter_mJ|null}
    -> {"event":"progress", committed, total, chunk_id}
    -> {"event":"checkpoint", generation, committed, dir}      then waits for one stdin line: continue|pause|cancel
    -> {"event":"paused"|"cancelled", committed}                and exits 0
    -> {"event":"result", committed, out_dir, summary}         and exits 0
    -> {"event":"error", code, reason}                          and exits 2..5

Control lines ("pause"/"cancel") may also arrive between chunks; they take effect at the next chunk boundary,
where a checkpoint is written first. Checkpoints are plain files inside the private work directory; the
worker encrypts and publishes them. Nothing here is a sandbox for hostile code: only the kernels below run."""
import hashlib
import json
import os
import resource
import select
import sys
import time
from pathlib import Path

from experiments.private_receipts.receipt import canonical
from . import inputs, kernels, npy
from .kernels import Backend

CHECKPOINT_SCHEMA = 'compute-checkpoint/v1'
EXIT_INPUT, EXIT_DEVICE, EXIT_RESOURCE, EXIT_NUMERIC, EXIT_INTERNAL = 2, 3, 4, 5, 6


def emit(obj):
    sys.stdout.write(json.dumps(obj, separators=(',', ':')) + '\n'); sys.stdout.flush()


def read_control(block):
    """One control line from stdin, or None when nothing is pending (block=False)."""
    if not block:
        r, _, _ = select.select([sys.stdin], [], [], 0)
        if not r:
            return None
    line = sys.stdin.readline()
    return line.strip() or 'cancel'            # EOF on stdin means the worker is gone: stop safely


def energy_counter():
    try:
        import pynvml
        pynvml.nvmlInit(); h = pynvml.nvmlDeviceGetHandleByIndex(0)
        v = int(pynvml.nvmlDeviceGetTotalEnergyConsumption(h)); pynvml.nvmlShutdown(); return v
    except Exception:
        return None


class Task:
    """Chunked execution with resumable state; subclasses implement kind-specific pieces."""

    def __init__(self, spec, be, workdir):
        self.spec, self.be, self.workdir = spec, be, Path(workdir)
        self.data = spec['inputs']; self.job_id = spec['job_id']
        self.total = inputs.work_units(spec['kind'], self.data)
        self.committed = 0; self.generation = 0; self.chunk_id = 0
        self.state_extra = {}

    # -- checkpoint binding ---------------------------------------------------------------------
    def meta(self, boundary):
        return {'schema': CHECKPOINT_SCHEMA, 'kind': self.spec['kind'], 'manifest_id': self.spec['manifest']['manifest_id'], 'manifest_version': self.spec['manifest']['version'],
                'implementation_digest': self.spec['manifest']['implementation_digest'], 'job_id': self.job_id, 'attempt_generation': self.spec['attempt_generation'],
                'input_digest': self.spec['input_digest'], 'backend': self.be.name, 'precision': self.spec['precision'], 'generation': self.generation,
                'committed_units': self.committed, 'total_units': self.total, 'boundary': boundary, 'chunk_id': self.chunk_id, 'created_at': int(time.time())}

    def check_resume_meta(self, meta):
        for key in ('schema', 'kind', 'manifest_id', 'manifest_version', 'implementation_digest', 'job_id', 'input_digest', 'precision'):
            expected = {'schema': CHECKPOINT_SCHEMA, 'kind': self.spec['kind'], 'manifest_id': self.spec['manifest']['manifest_id'], 'manifest_version': self.spec['manifest']['version'],
                        'implementation_digest': self.spec['manifest']['implementation_digest'], 'job_id': self.job_id, 'input_digest': self.spec['input_digest'], 'precision': self.spec['precision']}[key]
            if meta.get(key) != expected:
                raise ValueError('checkpoint does not bind this job (' + key + ')')
        if meta['backend'] != self.be.name and not self.cross_backend_resume_ok():
            raise ValueError('checkpoint written by another backend under a policy that forbids cross-backend resume')

    def cross_backend_resume_ok(self):
        return True                            # exact-integer tasks are backend independent; heat declares its tolerance policy

    def write_checkpoint(self, reason):
        self.generation += 1
        d = self.workdir / ('ckpt-%d' % self.generation)
        d.mkdir(mode=0o700)
        arrays = self.save_state(d)
        meta = self.meta(self.boundary())
        meta['arrays'] = arrays; meta['reason'] = reason
        (d / 'meta.json').write_bytes(canonical(meta))
        return d

    # -- to implement ----------------------------------------------------------------------------
    def boundary(self): raise NotImplementedError
    def save_state(self, d): raise NotImplementedError
    def load_state(self, d, meta): raise NotImplementedError
    def run_chunk(self): raise NotImplementedError          # advances self.committed; returns units done
    def finish(self, out): raise NotImplementedError         # writes outputs into out dir; returns summary


class TemporalBatchTask(Task):
    def __init__(self, spec, be, workdir):
        super().__init__(spec, be, workdir)
        self.rows = []
        self.chunk = min(spec.get('chunk') or inputs.BATCH_LIMITS['chunk_scenarios'], inputs.BATCH_LIMITS['chunk_scenarios'])
        self.ids = []

    def boundary(self):
        return {'scenarios_committed': self.committed}

    def save_state(self, d):
        flat = [v for r in self.rows for v in r]
        npy.write(d / 'results.npy', flat, '<i8', (len(self.rows), len(kernels.TEMPORAL_COLUMNS)))
        (d / 'ids.json').write_bytes(json.dumps(self.ids).encode())
        return {'results.npy': {'dtype': '<i8', 'shape': [len(self.rows), len(kernels.TEMPORAL_COLUMNS)]}}

    def load_state(self, d, meta):
        vals, dtype, shape = npy.read(d / 'results.npy')
        if shape != [meta['committed_units'], len(kernels.TEMPORAL_COLUMNS)]:
            raise ValueError('checkpoint array shape does not match its boundary')
        w = shape[1]
        self.rows = [vals[i * w:(i + 1) * w] for i in range(shape[0])]
        self.ids = json.loads((d / 'ids.json').read_bytes())
        self.committed = meta['committed_units']; self.chunk_id = meta['chunk_id']

    def run_chunk(self):
        first, last = self.committed, min(self.total, self.committed + self.chunk)
        scenarios, ids = [], []
        for i in range(first, last):
            sid, sc, _ = inputs.scenario(self.data, i)
            scenarios.append(sc); ids.append(sid)
        rows = kernels.temporal_batch(self.be, kernels.temporal_chunk_from_scenarios(scenarios))
        self.rows.extend(rows); self.ids.extend(ids)
        self.committed = last; self.chunk_id += 1
        return last - first

    def finish(self, out):
        flat = [v for r in self.rows for v in r]
        npy.write(out / 'results.npy', flat, '<i8', (len(self.rows), len(kernels.TEMPORAL_COLUMNS)))
        names = list(kernels.OUTCOME_CODES)
        counts = {n: 0 for n in names}
        for r in self.rows:
            counts[names[r[0]]] += 1
        table = [{'id': sid, 'index': i, 'outcome': names[r[0]], 'min_margin_pessimistic': r[1], 'min_margin_optimistic': r[3],
                  'first_uncertain_boundary': None if r[5] < 0 else {'segment': r[5], 't_start': r[6], 'offset_seconds': [r[7], r[8]]},
                  'first_infeasible_boundary': None if r[9] < 0 else {'segment': r[9], 't_start': r[10], 'offset_seconds': [r[11], r[12]]},
                  'spill_bounds': [r[13], r[14]], 'final_energy_bounds': [r[15], r[16]]} for i, (sid, r) in enumerate(zip(self.ids, self.rows))]
        (out / 'results.json').write_bytes(json.dumps({'columns': list(kernels.TEMPORAL_COLUMNS), 'rows': table[:4096], 'truncated': len(table) > 4096}, separators=(',', ':')).encode())
        return {'scenarios': len(self.rows), 'outcomes': counts, 'first_infeasible_index': next((i for i, r in enumerate(self.rows) if r[0] == 1), None),
                'model_id': 'temporal-energy/v1', 'precision': 'int64-exact', 'columns': list(kernels.TEMPORAL_COLUMNS)}


class MonteCarloTask(Task):
    def __init__(self, spec, be, workdir):
        super().__init__(spec, be, workdir)
        self.normalized = inputs.validate_monte_carlo(self.data)
        self.events = 0
        self.chunk = min(spec.get('chunk') or inputs.MC_LIMITS['chunk_samples'], inputs.MC_LIMITS['chunk_samples'])
        step = max(1, self.total // 512)
        self.audit_indices = set(range(0, self.total, step))
        self.audit = []                       # [{index, params, outcome}] regenerated deterministically for verification

    def boundary(self):
        return {'samples_committed': self.committed, 'events': self.events}

    def save_state(self, d):
        (d / 'state.json').write_bytes(json.dumps({'events': self.events, 'audit': self.audit}, separators=(',', ':')).encode())
        return {}

    def load_state(self, d, meta):
        st = json.loads((d / 'state.json').read_bytes())
        if meta['boundary'].get('events') != st['events']:
            raise ValueError('checkpoint state/boundary mismatch')
        self.events, self.audit = st['events'], st['audit']
        self.committed = meta['committed_units']; self.chunk_id = meta['chunk_id']

    def run_chunk(self):
        first, count = self.committed, min(self.chunk, self.total - self.committed)
        n_events, ok, params = kernels.mc_chunk(self.be, self.data['base'], self.normalized, self.data['seed'], first, count)
        self.events += n_events
        for i in range(first, first + count):
            if i in self.audit_indices:
                self.audit.append({'index': i, 'params': {k: v[i - first] for k, v in params.items()}, 'outcome': ok[i - first]})
        self.committed = first + count; self.chunk_id += 1
        return count

    def finish(self, out):
        n = self.committed
        ci = kernels.wilson_interval(self.events, n, self.data['confidence_percent'])
        summary = {'model_id': 'monte-carlo-reliability/v1', 'event': self.data['event'], 'samples': n, 'events': self.events, 'probability_estimate': self.events / n if n else None,
                   'interval': ci, 'seed': self.data['seed'], 'stream': 'philox-4x64-10 indexed by sample (numpy.random.Philox)', 'draws_per_sample': len(self.normalized),
                   'distributions': self.normalized, 'rejected_samples': 0, 'rejected_sample_policy': 'none: every drawn sample maps to a valid parameter set by construction',
                   'stopping_policy': 'fixed predeclared sample count; no data-dependent stopping', 'precision': 'int64-exact trajectories; float64 statistics',
                   'interpretation': 'probability under the declared distributions and the point model; not calibrated real-world reliability and not a proof that every allowed trajectory succeeds'}
        (out / 'samples_audit.json').write_bytes(json.dumps(self.audit, separators=(',', ':')).encode())
        return summary


class HeatTask(Task):
    def __init__(self, spec, be, workdir):
        super().__init__(spec, be, workdir)
        self.p = inputs.validate_heat(self.data)
        self.rx, self.ry = float(self.p['rx']), float(self.p['ry'])
        self.nx, self.ny, self.steps = self.p['nx'], self.p['ny'], self.p['steps']
        cells = self.nx * self.ny
        self.chunk_steps = max(1, min(self.steps, inputs.HEAT_LIMITS['chunk_cell_steps'] // cells))
        self.step = 0
        self.field0 = kernels.heat_initial_field(self.data, self.p)
        self.field = be.floats([v for r in self.field0 for v in r], (self.ny, self.nx))
        self.prev = None
        self.snap_steps = sorted({(self.steps * (k + 1)) // (self.data['snapshots'] + 1) for k in range(self.data['snapshots'])}) if self.data['snapshots'] else []
        self.snapshots = {}
        self.total = inputs.heat_work_units(self.data)

    def cross_backend_resume_ok(self):
        return True                            # declared policy: float64 on every backend, 1e-9 cross-backend tolerance, verification still applies

    def boundary(self):
        return {'step': self.step, 'snapshots_taken': sorted(self.snapshots)}

    def save_state(self, d):
        rows = self.be.tolist(self.field)
        npy.write(d / 'field.npy', [v for r in rows for v in r], '<f8', (self.ny, self.nx))
        if self.prev is not None:
            prow = self.be.tolist(self.prev)
            npy.write(d / 'field_prev.npy', [v for r in prow for v in r], '<f8', (self.ny, self.nx))
        if self.snapshots:
            ks = sorted(self.snapshots)
            npy.write(d / 'snapshots.npy', [v for k in ks for r in self.snapshots[k] for v in r], '<f8', (len(ks), self.ny, self.nx))
        return {'field.npy': {'dtype': '<f8', 'shape': [self.ny, self.nx]}}

    def load_state(self, d, meta):
        vals, dtype, shape = npy.read(d / 'field.npy')
        if shape != [self.ny, self.nx]:
            raise ValueError('checkpoint field shape mismatch')
        self.field = self.be.floats(vals, (self.ny, self.nx))
        if (d / 'field_prev.npy').exists():
            pv, _, _ = npy.read(d / 'field_prev.npy'); self.prev = self.be.floats(pv, (self.ny, self.nx))
        if (d / 'snapshots.npy').exists():
            sv, _, sshape = npy.read(d / 'snapshots.npy')
            ks = meta['boundary']['snapshots_taken']
            per = self.ny * self.nx
            for idx, k in enumerate(ks):
                block = sv[idx * per:(idx + 1) * per]
                self.snapshots[k] = [block[j * self.nx:(j + 1) * self.nx] for j in range(self.ny)]
        self.step = meta['boundary']['step']
        self.committed = meta['committed_units']; self.chunk_id = meta['chunk_id']

    def run_chunk(self):
        target = min(self.steps, self.step + self.chunk_steps)
        upcoming = [s for s in self.snap_steps if self.step < s <= target]
        cur = self.step
        for s in upcoming + [target]:
            n = s - cur
            if n > 0:
                self.field, self.prev = kernels.heat_steps(self.be, self.field, self.rx, self.ry, n)
                cur = s
            if s in self.snap_steps and s != target or (s == target and s in self.snap_steps):
                self.snapshots[s] = self.be.tolist(self.field)
        if not self.be.isfinite_all(self.field):
            raise ArithmeticError('non-finite field')
        self.step = target
        self.committed = -(-(self.nx * self.ny * self.step) // 1_000_000)
        self.chunk_id += 1
        return target

    def finish(self, out):
        rows = self.be.tolist(self.field)
        npy.write(out / 'field.npy', [v for r in rows for v in r], '<f8', (self.ny, self.nx))
        if self.prev is not None:
            prow = self.be.tolist(self.prev)
            npy.write(out / 'field_prev.npy', [v for r in prow for v in r], '<f8', (self.ny, self.nx))
        ks = sorted(self.snapshots)
        if ks:
            npy.write(out / 'snapshots.npy', [v for k in ks for r in self.snapshots[k] for v in r], '<f8', (len(ks), self.ny, self.nx))
        flat = [v for r in rows for v in r]
        if self.nx * self.ny <= 16384:
            (out / 'field.json').write_bytes(json.dumps({'rows': rows, 'orientation': 'field[y][x]; row index is y'}, separators=(',', ':')).encode())
        return {'model_id': 'heat-diffusion-2d-ftcs/v1', 'nx': self.nx, 'ny': self.ny, 'steps': self.steps, 'dt': str(self.p['dt']), 'dx': str(self.p['dx']), 'dy': str(self.p['dy']),
                'alpha': str(self.p['alpha']), 'r_x': float(self.p['rx']), 'r_y': float(self.p['ry']), 'horizon': str(self.p['dt'] * self.steps), 'units': self.data['units'],
                'boundary': self.data['boundary'], 'initial_type': self.data['initial']['type'], 'final_min': min(flat), 'final_max': max(flat), 'final_mean': sum(flat) / len(flat),
                'initial_min': min(min(r) for r in self.field0), 'initial_max': max(max(r) for r in self.field0), 'snapshot_steps': ks, 'precision': 'float64',
                'orientation': 'field[y][x]', 'timestep_policy': 'steps and dt are authoritative; horizon = steps * dt',
                'interpretation': 'explicit FTCS solution of the constant-diffusivity heat equation with fixed Dirichlet boundaries; a numerical model, not a validated thermal model of any device'}


class CalibrationFitTask(Task):
    """One-chunk fit: numpy.linalg.lstsq on the standardized training design (ridge by augmentation); metrics on both
    splits; empirical evaluation-residual interval; training domain; warnings. Rows come from spec['aux']."""

    def __init__(self, spec, be, workdir):
        super().__init__(spec, be, workdir)
        self.aux = spec.get('aux') or {}
        self.result = None

    def boundary(self):
        return {'fits_committed': self.committed}

    def save_state(self, d):
        return {}

    def load_state(self, d, meta):
        self.committed = meta['committed_units']; self.chunk_id = meta['chunk_id']

    def run_chunk(self):
        import numpy as np
        from fractions import Fraction
        from . import calibration as cal
        d = self.data
        columns, rows = self.aux['columns'], self.aux['rows']
        if not rows or len(rows) < inputs.CALIBRATION_LIMITS['min_rows']:
            raise ValueError('dataset has fewer than %d usable rows' % inputs.CALIBRATION_LIMITS['min_rows'])
        feats, target = d['features'], d['target']
        missing = [f for f in feats + [target] if f not in columns]
        if missing:
            raise ValueError('columns absent from the dataset: ' + ','.join(missing))
        fi = [columns.index(f) for f in feats]; ti = columns.index(target)
        X = [[cal.to_float(r[j]) for j in fi] for r in rows]
        y = [cal.to_float(r[ti]) for r in rows]
        train_idx, eval_idx, split = cal.split_rows(len(rows), d.get('split', {'method': 'chronological', 'train_fraction_percent': 80}))
        if max(train_idx + eval_idx) >= len(rows):
            raise ValueError('split indexes exceed the dataset')
        Xtr, ytr = [X[i] for i in train_idx], [y[i] for i in train_idx]
        scaling = d.get('scaling', 'standardize')
        kept, stats, dropped = cal.standardize_stats(Xtr, feats)
        if scaling == 'none':
            kept, stats = list(range(len(feats))), [(0.0, 1.0)] * len(feats)
        intercept = d.get('intercept', True)
        Ztr = cal.design(Xtr, kept, stats, intercept, scaling)
        lam = float(Fraction(d.get('ridge_lambda', '0')))
        A = np.array(Ztr, dtype=np.float64); b = np.array(ytr, dtype=np.float64)
        n_pen = len(kept)
        if lam > 0:
            aug = np.zeros((n_pen, A.shape[1])); aug[np.arange(n_pen), np.arange(n_pen)] = np.sqrt(lam)
            A = np.vstack([A, aug]); b = np.concatenate([b, np.zeros(n_pen)])
        coef, residuals, rank, sv = np.linalg.lstsq(A, b, rcond=None)
        coef = [float(c) for c in coef]
        cond = float(sv[0] / sv[-1]) if len(sv) and sv[-1] > 0 else None
        warnings = []
        if dropped:
            warnings.append('zero-variance features dropped: ' + ','.join(dropped))
        if rank < A.shape[1]:
            warnings.append('rank-deficient design (rank %d < %d columns): coefficients are the minimum-norm solution and are not individually identifiable' % (rank, A.shape[1]))
        if cond is not None and cond > 1e8:
            warnings.append('ill-conditioned design (condition number %.3g): coefficients are sensitive to small data changes' % cond)
        if len(train_idx) < 5 * max(1, A.shape[1]):
            warnings.append('few training rows relative to parameters (%d rows, %d parameters)' % (len(train_idx), A.shape[1]))
        yhat_tr = cal.predict_rows(Ztr, coef)
        Xev, yev = [X[i] for i in eval_idx], [y[i] for i in eval_idx]
        Zev = cal.design(Xev, kept, stats, intercept, scaling)
        yhat_ev = cal.predict_rows(Zev, coef)
        mtr, mev = cal.metrics(ytr, yhat_tr), cal.metrics(yev, yhat_ev)
        interval = cal.empirical_interval([a - b for a, b in zip(yev, yhat_ev)], d.get('interval_percent', 90))
        if interval is None:
            warnings.append('fewer than 5 evaluation rows: no prediction interval is claimed')
        domain = cal.domain_of(Xtr, feats)
        unit = self.aux.get('units', {}).get(target)
        self.result = {'schema': 'calibration-model/v1', 'model_id': 'linear-least-squares-calibration/v1', 'dataset_id': d['dataset_id'], 'dataset_digest': self.aux.get('digest'), 'rows': len(rows),
                       'features': feats, 'target': target, 'target_unit': unit, 'feature_units': {f: self.aux.get('units', {}).get(f) for f in feats}, 'intercept': intercept, 'ridge_lambda': d.get('ridge_lambda', '0'),
                       'scaling': scaling, 'kept_columns': [feats[j] for j in kept], 'scaling_stats': [[repr(m), repr(s)] for (m, s) in stats], 'dropped_columns': dropped,
                       'coefficients': [repr(c) for c in coef], 'coefficient_names': [feats[j] for j in kept] + (['intercept'] if intercept else []),
                       'rank': int(rank), 'singular_values': [repr(float(v)) for v in sv], 'condition_number': repr(cond) if cond is not None else None,
                       'split': split, 'train_indexes': train_idx, 'eval_indexes': eval_idx, 'metrics': {'train': mtr, 'eval': mev}, 'prediction_interval': interval, 'domain': domain,
                       'warnings': warnings, 'solver': 'numpy.linalg.lstsq (gelsd) ' + np.__version__,
                       'meaning': 'a fitted linear relationship on the training rows; not causation, not a guaranteed bound; predictions outside the domain are extrapolation'}
        self.predictions = {'train': [{'row': i, 'actual': repr(a), 'predicted': repr(p), 'residual': repr(a - p)} for i, a, p in zip(train_idx, ytr, yhat_tr)],
                            'eval': [{'row': i, 'actual': repr(a), 'predicted': repr(p), 'residual': repr(a - p)} for i, a, p in zip(eval_idx, yev, yhat_ev)]}
        self.committed = 1; self.chunk_id += 1
        return 1

    def finish(self, out):
        def exactable(o):
            if isinstance(o, float): return repr(o)
            if isinstance(o, dict): return {k: exactable(v) for k, v in o.items()}
            if isinstance(o, list): return [exactable(v) for v in o]
            return o
        (out / 'model.json').write_bytes(canonical(exactable(self.result)))
        (out / 'predictions.json').write_bytes(canonical(self.predictions))
        m = self.result
        return exactable({'model_id': m['model_id'], 'rows': m['rows'], 'features': m['features'], 'target': m['target'], 'rank': m['rank'], 'warnings': m['warnings'],
                          'metrics_eval_rmse': m['metrics']['eval'].get('rmse'), 'metrics_train_rmse': m['metrics']['train'].get('rmse'), 'eval_rows': m['metrics']['eval'].get('n'), 'train_rows': m['metrics']['train'].get('n'),
                          'split': m['split'], 'precision': 'float64'})


TASKS = {'temporal_batch': TemporalBatchTask, 'monte_carlo_reliability': MonteCarloTask, 'heat_diffusion': HeatTask, 'calibration_fit': CalibrationFitTask}


def main():
    workdir = Path(sys.argv[1])
    spec = json.loads((workdir / 'spec.json').read_bytes())
    lim = spec.get('limits', {})
    if lim.get('cpu_seconds'):
        resource.setrlimit(resource.RLIMIT_CPU, (lim['cpu_seconds'], lim['cpu_seconds']))
    if lim.get('fsize_bytes'):
        resource.setrlimit(resource.RLIMIT_FSIZE, (lim['fsize_bytes'], lim['fsize_bytes']))
    threads = lim.get('threads') or 4
    for var in ('OMP_NUM_THREADS', 'MKL_NUM_THREADS', 'OPENBLAS_NUM_THREADS'):
        os.environ.setdefault(var, str(threads))
    kind = spec['kind']
    if kind not in TASKS or kind != spec['manifest'].get('manifest_id', '').split('/')[0].replace('-', '_').replace('monte_carlo_reliability', 'monte_carlo_reliability') and False:
        emit({'event': 'error', 'code': 'INPUT_INVALID', 'reason': 'unsupported kind'}); return EXIT_INPUT
    try:
        inputs.VALIDATORS[kind](spec['inputs'])              # validated again inside the process boundary
    except Exception as exc:
        emit({'event': 'error', 'code': 'INPUT_INVALID', 'reason': str(exc)[:200]}); return EXIT_INPUT
    try:
        be = Backend(spec['backend'], threads=threads)
    except Exception as exc:
        emit({'event': 'error', 'code': 'DEVICE_UNAVAILABLE', 'reason': type(exc).__name__ + ': ' + str(exc)[:120]}); return EXIT_DEVICE
    e0 = energy_counter() if be.name == 'cuda' else None
    emit({'event': 'started', 'backend': be.name, 'versions': be.versions, 'pid': os.getpid(), 'energy_counter_mJ': e0, 'threads': threads})
    try:
        task = TASKS[kind](spec, be, workdir)
        if spec.get('resume_dir'):
            rd = Path(spec['resume_dir'])
            meta = json.loads((rd / 'meta.json').read_bytes())
            task.check_resume_meta(meta)
            task.generation = meta['generation']
            task.load_state(rd, meta)
            emit({'event': 'resumed', 'generation': task.generation, 'committed': task.committed, 'total': task.total, 'from_backend': meta['backend']})
    except (ValueError, KeyError) as exc:
        emit({'event': 'error', 'code': 'CHECKPOINT_INVALID', 'reason': str(exc)[:200]}); return EXIT_INPUT
    except Exception as exc:
        emit({'event': 'error', 'code': 'INTERNAL_DEFECT', 'reason': type(exc).__name__}); return EXIT_INTERNAL
    interval = spec.get('checkpoint_interval_seconds', 5)
    last_ckpt = time.time()
    pending = None
    try:
        while task.committed < task.total:
            ctl = read_control(block=False)
            if ctl in ('pause', 'cancel'):
                pending = ctl
            if pending or (time.time() - last_ckpt >= interval and task.committed > 0):
                d = task.write_checkpoint(pending or 'interval')
                emit({'event': 'checkpoint', 'generation': task.generation, 'committed': task.committed, 'total': task.total, 'dir': d.name, 'reason': pending or 'interval'})
                ack = read_control(block=True)
                last_ckpt = time.time()
                if ack in ('pause', 'cancel') or pending:
                    final = ack if ack in ('pause', 'cancel') else pending
                    emit({'event': 'paused' if final == 'pause' else 'cancelled', 'committed': task.committed, 'generation': task.generation}); return 0
            t0 = time.time()
            task.run_chunk()
            emit({'event': 'progress', 'committed': task.committed, 'total': task.total, 'chunk_id': task.chunk_id, 'chunk_seconds': round(time.time() - t0, 4)})
        out = workdir / 'out'
        out.mkdir(mode=0o700)
        summary = task.finish(out)
        e1 = energy_counter() if be.name == 'cuda' else None
        emit({'event': 'result', 'committed': task.committed, 'total': task.total, 'out_dir': out.name, 'summary': summary, 'generation': task.generation,
              'energy_counter_mJ': e1, 'energy_delta_mJ_device_wide': (e1 - e0) if (e0 is not None and e1 is not None) else None, 'peak_device_bytes': be.peak_bytes()})
        return 0
    except MemoryError:
        emit({'event': 'error', 'code': 'RESOURCE_REJECTED', 'reason': 'allocation failed'}); return EXIT_RESOURCE
    except ArithmeticError as exc:
        emit({'event': 'error', 'code': 'NUMERICAL_FAILURE', 'reason': str(exc)[:120]}); return EXIT_NUMERIC
    except Exception as exc:
        if 'out of memory' in str(exc).lower():
            emit({'event': 'error', 'code': 'RESOURCE_REJECTED', 'reason': 'device allocation failed'}); return EXIT_RESOURCE
        emit({'event': 'error', 'code': 'INTERNAL_DEFECT', 'reason': type(exc).__name__}); return EXIT_INTERNAL


if __name__ == '__main__':
    sys.exit(main())
