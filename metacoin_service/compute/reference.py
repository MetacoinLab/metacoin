"""Pure-Python reference implementations used for verification and tests. They share no code with the
vectorized kernels: temporal scenarios go through the established temporal-energy/v1 verifier, the heat
reference is a scalar loop in Python floats, and the discrete eigenmode amplification is closed form."""
import math
from .. import temporal
from .kernels import TEMPORAL_COLUMNS, OUTCOME_CODES


def temporal_reference_row(scenario_input):
    """The 17-column row the batch kernel must reproduce exactly, from the reference verifier."""
    r = temporal.analyze(scenario_input)
    def viol(v):
        if v is None:
            return (-1, 0, 0, 1)
        return (v['segment'], v['t_start'], v['offset_seconds'][0], v['offset_seconds'][1])
    lo, hi = viol(r['first_uncertain_boundary']), viol(r['first_infeasible_boundary'])
    return [OUTCOME_CODES[r['outcome']], r['min_reserve_margin_pessimistic'], r['min_reserve_margin_pessimistic_t'], r['min_reserve_margin_optimistic'], r['min_reserve_margin_optimistic_t'],
            lo[0], lo[1], lo[2], lo[3], hi[0], hi[1], hi[2], hi[3], r['spill_bounds'][0], r['spill_bounds'][1], r['final_energy_bounds'][0], r['final_energy_bounds'][1]]


def row_view(row):
    return dict(zip(TEMPORAL_COLUMNS, row))


def heat_reference(field, rx, ry, steps):
    """Scalar FTCS in Python floats; field is a list of rows; boundaries stay fixed."""
    ny, nx = len(field), len(field[0])
    f = [list(r) for r in field]
    for _ in range(steps):
        g = [list(r) for r in f]
        for j in range(1, ny - 1):
            fj, fjm, fjp = f[j], f[j - 1], f[j + 1]
            gj = g[j]
            for i in range(1, nx - 1):
                c = fj[i]
                gj[i] = c + rx * (fj[i + 1] - 2 * c + fj[i - 1]) + ry * (fjp[i] - 2 * c + fjm[i])
        f = g
    return f


def heat_eigenmode_factor(m, n, nx, ny, rx, ry):
    """Exact per-step amplification of the discrete sine mode (m, n) under FTCS with zero Dirichlet boundaries."""
    return 1 - 4 * rx * math.sin(math.pi * m / (2 * (nx - 1))) ** 2 - 4 * ry * math.sin(math.pi * n / (2 * (ny - 1))) ** 2


def heat_max_abs_diff(a, b):
    return max(abs(x - y) for ra, rb in zip(a, b) for x, y in zip(ra, rb))
