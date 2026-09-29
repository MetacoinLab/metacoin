"""Numerical kernels for the compute services, written once over a small backend abstraction so the
CPU (numpy) and CUDA (torch) paths execute the same algorithm. Temporal and Monte Carlo trajectories
use exact int64 arithmetic (host-side overflow bounds are checked before conversion, see inputs.py);
the heat solver uses float64. Nothing here reads files, opens sockets or executes user code."""
import math

TEMPORAL_COLUMNS = ('outcome', 'min_margin_low', 'min_t_low', 'min_margin_high', 'min_t_high', 'viol_seg_low', 'viol_tstart_low', 'viol_num_low', 'viol_den_low',
                    'viol_seg_high', 'viol_tstart_high', 'viol_num_high', 'viol_den_high', 'spill_low', 'spill_high', 'final_low', 'final_high')
OUTCOME_CODES = {'FEASIBLE': 0, 'INFEASIBLE': 1, 'INDETERMINATE': 2}


class Backend:
    """Thin adapter: 'cpu' -> numpy, 'cuda' -> torch on the selected device."""

    def __init__(self, name, threads=None):
        self.name = name
        if name == 'cpu':
            import numpy as np
            self.np, self.torch, self.device = np, None, 'cpu'
            self.versions = {'numpy': np.__version__}
        elif name == 'cuda':
            import torch
            if not torch.cuda.is_available():
                raise RuntimeError('cuda unavailable')
            self.np, self.torch, self.device = None, torch, torch.device('cuda:0')
            if threads:
                torch.set_num_threads(threads)
            self.versions = {'torch': torch.__version__, 'cuda_build': torch.version.cuda, 'device': torch.cuda.get_device_name(0),
                             'capability': '%d.%d' % torch.cuda.get_device_capability(0)}
        else:
            raise RuntimeError('unsupported backend')

    # creation / conversion
    def ints(self, values, shape=None):
        if self.torch:
            t = self.torch.tensor(values, dtype=self.torch.int64, device=self.device)
            return t.reshape(shape) if shape else t
        a = self.np.asarray(values, dtype=self.np.int64)
        return a.reshape(shape) if shape else a

    def floats(self, values, shape=None):
        if self.torch:
            t = self.torch.tensor(values, dtype=self.torch.float64, device=self.device)
            return t.reshape(shape) if shape else t
        a = self.np.asarray(values, dtype=self.np.float64)
        return a.reshape(shape) if shape else a

    def full_int(self, n, value):
        return self.torch.full((n,), value, dtype=self.torch.int64, device=self.device) if self.torch else self.np.full(n, value, dtype=self.np.int64)

    def where(self, cond, a, b):
        return self.torch.where(cond, a, b) if self.torch else self.np.where(cond, a, b)

    def minimum(self, a, b):
        return self.torch.minimum(a, b) if self.torch else self.np.minimum(a, b)

    def clone(self, a):
        return a.clone() if self.torch else a.copy()

    def tolist(self, a):
        return a.tolist()

    def sync(self):
        if self.torch:
            self.torch.cuda.synchronize()

    def isfinite_all(self, a):
        return bool(self.torch.isfinite(a).all().item()) if self.torch else bool(self.np.isfinite(a).all())

    def stack_columns(self, cols):
        return self.torch.stack(cols, dim=1) if self.torch else self.np.stack(cols, axis=1)

    def peak_bytes(self):
        return int(self.torch.cuda.max_memory_allocated()) if self.torch else None


# ---- temporal batch ---------------------------------------------------------------------------------
def temporal_envelope(be, initial, nets, durations, capacity, reserve):
    """Vectorized exact envelope walk for a chunk of scenarios.
    initial, capacity, reserve: int64 [S]; nets: int64 [S, K]; durations: python ints [K]."""
    S = initial.shape[0]
    e = be.clone(initial)
    zero, one = be.full_int(S, 0), be.full_int(S, 1)
    min_margin = e - reserve
    min_t = be.full_int(S, 0)
    initial_bad = e < reserve
    viol_seg = be.where(initial_bad, zero, be.full_int(S, -1))
    viol_tstart, viol_num, viol_den = be.clone(zero), be.clone(zero), be.clone(one)   # initial violation: offset 0/1 at t=0
    spill = be.clone(zero)
    t = 0
    for i, d in enumerate(durations):
        n = nets[:, i]
        raw = e + n * d
        end = be.minimum(capacity, raw)
        spill = spill + be.where(raw > capacity, raw - capacity, zero)
        newly = (viol_seg < 0) & (end < reserve)
        viol_seg = be.where(newly, be.full_int(S, i), viol_seg)
        viol_tstart = be.where(newly, be.full_int(S, t), viol_tstart)
        viol_num = be.where(newly, e - reserve, viol_num)
        viol_den = be.where(newly, -n, viol_den)
        t += d
        e = end
        margin = e - reserve
        better = margin < min_margin
        min_margin = be.where(better, margin, min_margin)
        min_t = be.where(better, be.full_int(S, t), min_t)
    return {'min_margin': min_margin, 'min_t': min_t, 'viol_seg': viol_seg, 'viol_tstart': viol_tstart, 'viol_num': viol_num, 'viol_den': viol_den, 'spill': spill, 'final': e}


def temporal_batch(be, chunk):
    """chunk: dict with lists initial_low, initial_high, capacity, reserve (S), net_low, net_high (S x K flat), durations (K).
    Returns [S, 17] int64 rows in TEMPORAL_COLUMNS order (fractions reduced on the host)."""
    S, K = len(chunk['capacity']), len(chunk['durations'])
    cap, res = be.ints(chunk['capacity']), be.ints(chunk['reserve'])
    low = temporal_envelope(be, be.ints(chunk['initial_low']), be.ints(chunk['net_low'], (S, K)), chunk['durations'], cap, res)
    high = temporal_envelope(be, be.ints(chunk['initial_high']), be.ints(chunk['net_high'], (S, K)), chunk['durations'], cap, res)
    outcome = be.where(low['min_margin'] >= 0, be.full_int(S, 0), be.where(high['min_margin'] < 0, be.full_int(S, 1), be.full_int(S, 2)))
    cols = [outcome, low['min_margin'], low['min_t'], high['min_margin'], high['min_t'],
            low['viol_seg'], low['viol_tstart'], low['viol_num'], low['viol_den'], high['viol_seg'], high['viol_tstart'], high['viol_num'], high['viol_den'],
            low['spill'], high['spill'], low['final'], high['final']]
    be.sync()
    rows = be.tolist(be.stack_columns(cols))
    for r in rows:                             # reduce the exact crossing fractions like the reference (Fraction normalizes)
        for seg_i, num_i, den_i in ((5, 7, 8), (9, 11, 12)):
            if r[seg_i] >= 0:
                g = math.gcd(r[num_i], r[den_i]) or 1
                r[num_i], r[den_i] = r[num_i] // g, r[den_i] // g
            else:
                r[num_i], r[den_i], r[seg_i + 1] = 0, 1, 0
    return rows


def temporal_chunk_from_scenarios(scenarios):
    """scenarios: list of full temporal inputs sharing one duration schedule -> flat chunk arrays."""
    durations = [s['duration'] for s in scenarios[0]['segments']]
    chunk = {'initial_low': [], 'initial_high': [], 'capacity': [], 'reserve': [], 'net_low': [], 'net_high': [], 'durations': durations}
    for sc in scenarios:
        if [s['duration'] for s in sc['segments']] != durations:
            raise ValueError('batch scenarios must share the duration schedule')
        chunk['initial_low'].append(sc['initial_low']); chunk['initial_high'].append(sc['initial_high'])
        chunk['capacity'].append(sc['capacity']); chunk['reserve'].append(sc['reserve'])
        for s in sc['segments']:
            chunk['net_low'].append(s['harvest_low'] - s['load_high'] - s['leakage_high'])
            chunk['net_high'].append(s['harvest_high'] - s['load_low'] - s['leakage_low'])
    return chunk


# ---- Monte Carlo sampling ----------------------------------------------------------------------------
def philox_words(seed, first_sample, count, draws_per_sample):
    """64-bit words for samples [first_sample, first_sample+count): deterministic in the logical sample index,
    independent of chunking (numpy.random.Philox counter advanced to first_sample * draws_per_sample)."""
    import numpy as np
    p0, p1 = first_sample * draws_per_sample, (first_sample + count) * draws_per_sample   # absolute 64-bit word positions
    b0, o0 = p0 // 4, p0 % 4                                                           # Philox emits 4 words per counter block
    nblocks = (p1 - b0 * 4 + 3) // 4
    words = np.random.Philox(key=seed, counter=b0).random_raw(nblocks * 4)
    return words[o0:o0 + count * draws_per_sample].reshape(count, draws_per_sample)


def map_words(words, normalized):
    """Map raw words (numpy uint64 [S, D]) to parameter values in MC_PARAMS order. Modulo mapping (documented bias)."""
    import numpy as np
    out = {}
    for j, (name, dist) in enumerate(normalized.items()):
        w = words[:, j]
        if dist['type'] == 'finite':
            total = dist['total_weight']
            r = (w % np.uint64(total)).astype(np.int64)
            cum = np.cumsum(np.asarray(dist['weights'], dtype=np.int64))
            idx = np.searchsorted(cum, r, side='right')
            out[name] = np.asarray(dist['values'], dtype=np.int64)[idx]
        else:
            out[name] = dist['low'] + (w % np.uint64(dist['count'])).astype(np.int64)
    return out


def mc_chunk(be, base, normalized, seed, first, count):
    """Evaluate logical samples [first, first+count). Returns (event_count, per-sample outcome list of 0/1 ints, params dict)."""
    import numpy as np
    from .inputs import scale_low, scale_high
    words = philox_words(seed, first, count, len(normalized))
    params = map_words(words, normalized)
    K = len(base['segments'])
    initial = np.full(count, base['initial_low'], dtype=np.int64)
    reserve = np.full(count, base['reserve'], dtype=np.int64)
    if 'initial_energy' in params:
        initial = params['initial_energy']
    if 'reserve' in params:
        reserve = params['reserve']
    h = np.asarray([s['harvest_low'] for s in base['segments']], dtype=np.int64)[None, :].repeat(count, axis=0)
    l = np.asarray([s['load_low'] for s in base['segments']], dtype=np.int64)[None, :].repeat(count, axis=0)
    k = np.asarray([s['leakage_low'] for s in base['segments']], dtype=np.int64)[None, :].repeat(count, axis=0)
    def scaled(arr, pct):                      # point values: low == high, so floor scaling applies (matches scale_low for non-negative values)
        return np.floor_divide(arr * pct[:, None], 100)
    if 'harvest_scale_percent' in params:
        h = scaled(h, params['harvest_scale_percent'])
    if 'load_scale_percent' in params:
        l = scaled(l, params['load_scale_percent'])
    if 'leakage_scale_percent' in params:
        k = scaled(k, params['leakage_scale_percent'])
    nets = (h - l - k)
    durations = [s['duration'] for s in base['segments']]
    cap = np.full(count, base['capacity'], dtype=np.int64)
    env = temporal_envelope(be, be.ints(initial.tolist()), be.ints(nets.reshape(-1).tolist(), (count, K)), durations, be.ints(cap.tolist()), be.ints(reserve.tolist()))
    be.sync()
    ok = be.tolist((env['min_margin'] >= 0))
    ok = [1 if x else 0 for x in ok]
    return sum(ok), ok, {n: v.tolist() for n, v in params.items()}


def wilson_interval(successes, n, confidence_percent):
    """Wilson score interval for a binomial proportion (normal quantile from the standard library)."""
    from statistics import NormalDist
    if n <= 0:
        return None
    z = NormalDist().inv_cdf(1 - (1 - confidence_percent / 100) / 2)
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return {'method': 'wilson_score', 'confidence_percent': confidence_percent, 'z': z, 'low': max(0.0, centre - half), 'high': min(1.0, centre + half)}


# ---- 2-D heat diffusion (FTCS) -------------------------------------------------------------------------
def heat_initial_field(spec, params):
    """Deterministic initial field rows (ny x nx python floats) from an allowlisted parameterized condition."""
    nx, ny = params['nx'], params['ny']
    init = spec['initial']
    dv = lambda s: float(__import__('decimal').Decimal(s))
    if init['type'] == 'uniform':
        v = dv(init['value']); field = [[v] * nx for _ in range(ny)]
    elif init['type'] == 'gaussian':
        cx, cy, sig, amp, bg = (dv(init[k]) for k in ('center_x', 'center_y', 'sigma', 'amplitude', 'background'))
        dx, dy = float(params['dx']), float(params['dy'])
        field = [[bg + amp * math.exp(-(((i * dx - cx) ** 2 + (j * dy - cy) ** 2) / (2 * sig * sig))) for i in range(nx)] for j in range(ny)]
    elif init['type'] == 'sine_mode':
        m, n, amp = init['m'], init['n'], dv(init['amplitude'])
        field = [[amp * math.sin(math.pi * m * i / (nx - 1)) * math.sin(math.pi * n * j / (ny - 1)) for i in range(nx)] for j in range(ny)]
    elif init['type'] == 'hot_rectangle':
        inside, outside = dv(init['inside']), dv(init['outside'])
        field = [[inside if init['x0'] <= i <= init['x1'] and init['y0'] <= j <= init['y1'] else outside for i in range(nx)] for j in range(ny)]
    else:
        field = [[dv(v) for v in row] for row in init['rows']]
    bv = {side: dv(spec['boundary']['values'][side]) for side in ('left', 'right', 'top', 'bottom')}
    for j in range(ny):
        field[j][0], field[j][nx - 1] = bv['left'], bv['right']
    for i in range(nx):
        field[0][i], field[ny - 1][i] = bv['bottom'], bv['top']
    return field


def heat_steps(be, field, rx, ry, steps):
    """Advance a float64 [ny, nx] array `steps` FTCS steps with fixed boundaries. Returns (field, previous_field)."""
    prev = None
    for _ in range(steps):
        prev = field
        nxt = be.clone(field)
        c = field[1:-1, 1:-1]
        nxt[1:-1, 1:-1] = c + rx * (field[1:-1, 2:] - 2 * c + field[1:-1, :-2]) + ry * (field[2:, 1:-1] - 2 * c + field[:-2, 1:-1])
        field = nxt
    be.sync()
    return field, prev
