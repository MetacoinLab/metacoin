"""Trusted compute task manifests (data, never code). The implementation digest binds the exact kernel
sources installed on this host; an accepted job records the manifest version and digest so a later
worker upgrade cannot silently reinterpret it."""
import hashlib
from pathlib import Path
from . import inputs

HERE = Path(__file__).parent
KERNEL_FILES = ('kernels.py', 'inputs.py', 'reference.py', 'npy.py', 'exec.py', 'calibration.py')


def implementation_digest():
    h = hashlib.sha256(b'metacoin/compute-implementation/v1\0')
    for name in KERNEL_FILES:
        h.update(name.encode() + b'\0' + (HERE / name).read_bytes() + b'\0')
    return h.hexdigest()


MANIFESTS = {
    'temporal_batch': {
        'manifest_id': 'temporal-batch/v1', 'version': 1, 'model_id': 'temporal-energy/v1', 'result_schema': 'temporal-batch-result/v1',
        'input_schema': inputs.TEMPORAL_BATCH_SCHEMA, 'devices': ['cpu', 'cuda'], 'precision': ['int64-exact'],
        'numerical_policy': 'exact signed 64-bit integer arithmetic on every backend; host-side overflow bound before conversion; classification identical to temporal-energy/v1',
        'limits': dict(inputs.BATCH_LIMITS, max_segments=512), 'work_unit': 'one verified scenario evaluation', 'price_basis': 'per scenario evaluation',
        'checkpoint_format': 'temporal-batch-checkpoint/v1', 'verification_modes': ['exact_all', 'exact_sampled'],
        'verification_policy': {'exact_all_threshold': 5000, 'sampled_count': 512, 'rule': 'every sampled scenario must match the reference exactly (no tolerance)'},
        'outputs': ['results.npy (int64, [scenarios, 12])', 'results.json (bounded), summary'],
        'resource_controls': {'chunk_scenarios': inputs.BATCH_LIMITS['chunk_scenarios'], 'threads': 'bounded per worker', 'device_slots': 1},
    },
    'monte_carlo_reliability': {
        'manifest_id': 'monte-carlo-reliability/v1', 'version': 1, 'model_id': 'monte-carlo-reliability/v1', 'result_schema': 'monte-carlo-reliability-result/v1',
        'input_schema': inputs.MONTE_CARLO_SCHEMA, 'devices': ['cpu', 'cuda'], 'precision': ['int64-exact trajectories; float64 statistics'],
        'numerical_policy': 'indexed Philox-4x64-10 stream (numpy.random.Philox, key = seed; word position p = sample_index * draws_per_sample + draw, counter block p // 4, offset p % 4); '
                            'modulo mapping of 64-bit words to finite/uniform integer distributions (bias below range/2^64); trajectories exact in int64; '
                            'Wilson score interval for the Bernoulli proportion at the declared confidence; fixed predeclared sample count (no adaptive stopping)',
        'limits': dict(inputs.MC_LIMITS), 'work_unit': 'one committed sample', 'price_basis': 'per committed sample',
        'checkpoint_format': 'monte-carlo-checkpoint/v1', 'verification_modes': ['sampled_audit', 'exact_all'],
        'verification_policy': {'exact_all_threshold': 5000, 'sampled_count': 512, 'rule': 'audited samples are regenerated from the stream definition and re-evaluated by the reference model; counts must match exactly'},
        'outputs': ['summary (probability, interval, counts), samples_audit.json'],
        'resource_controls': {'chunk_samples': inputs.MC_LIMITS['chunk_samples'], 'device_slots': 1},
    },
    'heat_diffusion': {
        'manifest_id': 'heat-diffusion-2d/v1', 'version': 1, 'model_id': 'heat-diffusion-2d-ftcs/v1', 'result_schema': 'heat-diffusion-result/v1',
        'input_schema': inputs.HEAT_SCHEMA, 'devices': ['cpu', 'cuda'], 'precision': ['float64'],
        'numerical_policy': 'forward-time central-space explicit scheme on a uniform rectangular grid with time-independent Dirichlet boundaries; '
                            'stability r_x + r_y <= 1/2 checked exactly on the decimal parameters; steps and dt are authoritative (horizon = steps * dt); '
                            'float64 on every backend; cross-backend agreement tolerance 1e-9 relative to the field scale',
        'limits': dict(inputs.HEAT_LIMITS), 'work_unit': 'one million interior cell updates (rounded up)', 'price_basis': 'per million cell updates',
        'checkpoint_format': 'heat-checkpoint/v1', 'verification_modes': ['exact_reference', 'invariants_and_last_step'],
        'verification_policy': {'exact_reference_max_cell_steps': 2_000_000, 'tolerance': {'abs': 1e-9, 'rel': 1e-9, 'norm': 'max'},
                                'invariants': ['finite', 'boundary fixed', 'discrete maximum principle', 'last step recomputed independently from the stored penultimate field']},
        'outputs': ['field.npy (float64, [ny, nx])', 'field_prev.npy', 'snapshots.npy (float64, [k, ny, nx])', 'summary'],
        'resource_controls': {'chunk_cell_steps': inputs.HEAT_LIMITS['chunk_cell_steps'], 'device_slots': 1},
    },
    'calibration_fit': {
        'manifest_id': 'calibration-linear/v1', 'version': 1, 'model_id': 'linear-least-squares-calibration/v1', 'result_schema': 'calibration-fit-result/v1',
        'input_schema': inputs.CALIBRATION_SCHEMA, 'devices': ['cpu'], 'precision': ['float64'],
        'numerical_policy': 'ordinary least squares or ridge (augmented rows, unpenalized intercept) solved by numpy.linalg.lstsq (LAPACK gelsd, SVD-based; no explicit inverse) '
                            'on features standardized with training-split statistics; rank and singular values reported; verification refits with an independent Householder QR in pure Python',
        'limits': dict(inputs.CALIBRATION_LIMITS), 'work_unit': 'one fit', 'price_basis': 'per fit',
        'checkpoint_format': 'none (single chunk)', 'verification_modes': ['reference_refit'],
        'verification_policy': {'tolerance': {'rel': 1e-6, 'abs': 1e-9}, 'rule': 'training predictions from the stored coefficients must match the Householder QR reference within tolerance; reported metrics must be reproducible from stored predictions'},
        'outputs': ['model.json (manifest: coefficients, scaling, split, metrics, domain, warnings)', 'predictions.json (train/eval rows: actual, predicted, residual)'],
        'resource_controls': {'threads': 'bounded per worker', 'device_slots': 1},
    },
}
KINDS = tuple(MANIFESTS)


def manifest(kind):
    m = dict(MANIFESTS[kind])
    m['implementation_digest'] = implementation_digest()
    return m
