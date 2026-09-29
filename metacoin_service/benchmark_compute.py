"""Fair, bounded measurements for the compute services on this machine (order §47).

    PYTHONPATH=. python3 -m metacoin_service.benchmark_compute --out FILE          (kernel measurements: needs numpy/torch)
    PYTHONPATH=. .venv-service/bin/python -m metacoin_service.benchmark_compute --engine --out FILE   (end-to-end through the engine)

Kernel timings synchronize the device before stopping the clock; cold (first call, includes CUDA context and
first-kernel compilation) and warm medians are reported separately with sample counts and dispersion. The CPU
baseline is the same vectorized numpy algorithm, not a deliberately slow loop. Nothing here is a throughput claim
for other inputs or machines."""
import argparse
import json
import platform
import statistics
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def summary(xs):
    s = sorted(xs)
    return {'samples': len(s), 'median_s': round(statistics.median(s), 5), 'min_s': round(s[0], 5), 'max_s': round(s[-1], 5), 'stdev_s': round(statistics.pstdev(s), 5) if len(s) > 1 else 0.0}


def kernel_benchmarks(samples):
    from metacoin_service.compute import inputs, kernels
    from metacoin_service.compute.kernels import Backend
    from metacoin_service.tests.test_compute_science import base_temporal
    def heat_spec(**over):
        d = {'schema': inputs.HEAT_SCHEMA, 'nx': 384, 'ny': 384, 'dx': '0.01', 'dy': '0.01', 'dt': '0.00002', 'alpha': '1.0', 'steps': 8000,
             'boundary': {'type': 'dirichlet', 'values': {'left': '0', 'right': '0', 'top': '0', 'bottom': '0'}},
             'initial': {'type': 'gaussian', 'center_x': '1.92', 'center_y': '1.92', 'sigma': '0.4', 'amplitude': '100', 'background': '0'}, 'snapshots': 2,
             'units': {'field': 'K', 'length': 'm', 'time': 's'}, 'device_policy': 'cpu', 'precision': 'float64', 'private_label': 'BENCH'}
        d.update(over); return d
    def mc_spec(**over):
        d = {'schema': inputs.MONTE_CARLO_SCHEMA, 'base': None, 'distributions': {'initial_energy': {'type': 'finite', 'values': [4000, 6000, 8000], 'weights': [1, 2, 1]}, 'load_scale_percent': {'type': 'uniform_int', 'low': 50, 'high': 200}},
             'samples': 20000, 'seed': 11, 'confidence_percent': 95, 'event': 'reserve_maintained', 'device_policy': 'cpu', 'private_label': 'BENCH'}
        d.update(over); return d
    out = {'machine': platform.platform(), 'python': sys.version.split()[0], 'backends': {}}
    backends = ['cpu']
    try:
        import torch
        if torch.cuda.is_available():
            backends.append('cuda')
    except Exception:
        pass
    for name in backends:
        t0 = time.perf_counter(); be = Backend(name); init = time.perf_counter() - t0
        out['backends'][name] = {'versions': be.versions, 'init_s': round(init, 4), 'threads': 4}
    results = []
    # temporal batch: S scenarios x K segments, int64 exact
    for S, K in ((1000, 16), (8192, 64), (8192, 512)):
        base = base_temporal(segments=[{'duration': 3 + (i % 5), 'harvest_low': 400 + (i % 9) * 20, 'harvest_high': 450 + (i % 9) * 20, 'load_low': 380, 'load_high': 420 + (i % 4) * 10, 'leakage_low': 0, 'leakage_high': 2} for i in range(K)])
        nr = S // 16
        spec = {'schema': inputs.TEMPORAL_BATCH_SCHEMA, 'base': base, 'scenarios': None, 'grid': [{'path': 'reserve', 'values': [i * (9000 // nr) for i in range(nr)]}, {'path': 'load_scale_percent', 'values': [80 + 2 * i for i in range(16)]}],
                'device_policy': 'auto', 'verification': 'auto', 'private_label': 'BENCH'}
        inputs.validate_temporal_batch(spec)
        n = inputs.batch_total(spec)
        scenarios = [inputs.scenario(spec, i)[1] for i in range(n)]
        chunk = kernels.temporal_chunk_from_scenarios(scenarios)
        for name in backends:
            be = Backend(name); times = []
            for i in range(samples + 1):
                t0 = time.perf_counter(); kernels.temporal_batch(be, chunk); be.sync(); times.append(time.perf_counter() - t0)
            results.append({'service': 'temporal_batch', 'backend': name, 'scenarios': n, 'segments': K, 'dtype': 'int64', 'cold_s': round(times[0], 5), 'warm': summary(times[1:]),
                            'scenarios_per_s_warm': round(n / statistics.median(times[1:]), 1), 'includes': 'host->device transfer, kernel, device->host, fraction reduction; excludes checkpoint/verification'})
    # monte carlo: samples x segments
    for N, K in ((65536, 2), (65536, 64), (262144, 64)):
        base = base_temporal(initial_low=6000, initial_high=6000, segments=[{'duration': 5, 'harvest_low': 500 + (i % 7) * 40, 'harvest_high': 500 + (i % 7) * 40, 'load_low': 480 + (i % 5) * 30, 'load_high': 480 + (i % 5) * 30, 'leakage_low': 0, 'leakage_high': 0} for i in range(K)])
        spec = mc_spec(base=base, samples=N); normalized = inputs.validate_monte_carlo(spec)
        for name in backends:
            be = Backend(name); times = []
            for i in range(samples + 1):
                t0 = time.perf_counter(); kernels.mc_chunk(be, base, normalized, spec['seed'], 0, N); times.append(time.perf_counter() - t0)
            results.append({'service': 'monte_carlo_reliability', 'backend': name, 'samples_per_chunk': N, 'segments': K, 'dtype': 'int64 (uint64 sampling on host)', 'cold_s': round(times[0], 5), 'warm': summary(times[1:]),
                            'samples_per_s_warm': round(N / statistics.median(times[1:]), 1), 'includes': 'host Philox sampling + mapping, transfer, trajectory kernel; excludes checkpoint/verification'})
    # heat: grid x steps, float64
    for n, steps in ((128, 500), (512, 200), (1024, 50)):
        spec = heat_spec(nx=n, ny=n, steps=steps, dx='0.01', dy='0.01', dt='0.00002', initial={'type': 'gaussian', 'center_x': str(round(n * 0.005, 4)), 'center_y': str(round(n * 0.005, 4)), 'sigma': '0.3', 'amplitude': '100', 'background': '0'})
        p = inputs.validate_heat(spec)
        f0 = kernels.heat_initial_field(spec, p)
        for name in backends:
            be = Backend(name); times = []
            for i in range(samples + 1):
                f = be.floats([v for r in f0 for v in r], (n, n))
                t0 = time.perf_counter(); kernels.heat_steps(be, f, float(p['rx']), float(p['ry']), steps); be.sync(); times.append(time.perf_counter() - t0)
            cell_steps = n * n * steps
            results.append({'service': 'heat_diffusion', 'backend': name, 'grid': [n, n], 'steps': steps, 'dtype': 'float64', 'cold_s': round(times[0], 5), 'warm': summary(times[1:]),
                            'cell_updates_per_s_warm': round(cell_steps / statistics.median(times[1:]), 0), 'includes': 'stencil steps only (field already on device); excludes transfer/checkpoint/verification'})
    out['kernels'] = results
    return out


def engine_benchmarks(samples):
    """End-to-end through the API + engine: submit -> claim -> child -> checkpoints -> verification -> publish."""
    from metacoin_service.tests.test_compute_engine import ComputeInstance, heat_spec, batch_spec, mc_spec, HAVE_CUDA
    from metacoin_service import db as database
    out = {'cases': []}
    inst = ComputeInstance()
    try:
        H = inst.h('owner'); c = inst.client; w = inst.worker()
        cases = [('temporal_batch', batch_spec(device_policy='cpu')), ('monte_carlo_reliability', mc_spec(samples=200000, device_policy='cpu')), ('heat_diffusion', heat_spec(nx=256, ny=256, steps=1000, device_policy='cpu'))]
        if HAVE_CUDA:
            cases += [('temporal_batch', batch_spec(device_policy='gpu')), ('heat_diffusion', heat_spec(nx=256, ny=256, steps=1000, device_policy='gpu'))]
        for kind, spec in cases:
            times, verify_times, ckpts = [], [], []
            for i in range(samples):
                jid = inst.compute_job(kind, dict(spec, private_label='BENCH_%d' % i))
                t0 = time.perf_counter(); res = w.run_once(); total = time.perf_counter() - t0
                v = inst.view(jid)
                with database.Database(inst.settings.db_path).read() as db:
                    ev = [dict(r) for r in db.execute("SELECT event_type, ts FROM events WHERE object_id=? ORDER BY seq", (jid,))]
                times.append(total); ckpts.append(len(v['checkpoints']))
                assert res and res[1] == 'succeeded', (kind, v)
            out['cases'].append({'service': kind, 'device_policy': spec['device_policy'], 'backend_used': v['backend'], 'work_units': v['work']['total'], 'end_to_end_job_s': summary(times),
                                 'checkpoints_written': ckpts, 'verification_mode': v['verification']['mode'], 'includes': 'claim, child start, compute, checkpoint encryption, verification phase, output encryption, publication; excludes queue wait'})
        # checkpoint overhead and recovered work: one heat run with 1 s checkpoint interval vs the same run with a very long interval
        for interval, label in ((1, 'checkpoint_every_1s'), (3600, 'no_checkpoints')):
            inst.settings.limits['compute_checkpoint_interval_seconds'] = interval
            jid = inst.compute_job('heat_diffusion', heat_spec(nx=384, ny=384, steps=6000, private_label='BENCH_CK'))
            t0 = time.perf_counter(); w.run_once(); dt = time.perf_counter() - t0
            v = inst.view(jid)
            out.setdefault('checkpoint_overhead', {})[label] = {'end_to_end_s': round(dt, 3), 'checkpoints': len(v['checkpoints']), 'work_units': v['work']['total']}
    finally:
        inst.close()
    return out


def main():
    p = argparse.ArgumentParser(); p.add_argument('--samples', type=int, default=5); p.add_argument('--engine', action='store_true'); p.add_argument('--out'); a = p.parse_args()
    try:
        rev = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    except Exception:
        rev = 'unavailable'
    result = {'revision': rev, 'samples': a.samples, 'scope': 'bounded measurements on this DGX (one host, one process, shared GPU); medians with dispersion; not throughput claims for other inputs',
              'measurement': 'time.perf_counter around the call with device synchronization before stopping the clock'}
    result.update(engine_benchmarks(a.samples) if a.engine else kernel_benchmarks(a.samples))
    text = json.dumps(result, indent=1)
    if a.out:
        Path(a.out).write_text(text)
    print(text)


if __name__ == '__main__':
    main()
