"""task-0005-rover-path — deterministic lowest-energy rover path over synthetic terrain.

Research demo only. A lab-useful, fully deterministic robotic path-planning task: it
generates a fixed synthetic elevation grid from a documented closed-form height function,
then finds the minimum-ENERGY path from a fixed start cell to a fixed goal cell using
Dijkstra's algorithm (heapq). It maps to the NASA Technology Taxonomy TX04 (Robotic
Systems). The computation is deterministic and reproducible by machine, which is exactly
what MIP-0002 Gate 2 (independent re-run yields a byte-identical hash) checks.

WHAT GUARANTEES REPRODUCIBILITY (the critical part):
  * All move costs are INTEGERS (integer-centimeter elevations, integer base cost, integer
    uphill factor), so there are NO floating-point ties anywhere in the search.
  * When two partial paths have equal cost, the tie is broken by a FIXED TOTAL ORDER: heap
    entries are (cost, cell_index, cell) where cell_index = row * GRID_COLS + col is unique
    per cell. Equal-cost states are therefore always popped in ascending cell_index order,
    identically on every run. Neighbors are relaxed in a FIXED order (up, down, left, right)
    and a predecessor is recorded only on a STRICT cost improvement. Together these make the
    popped order, the predecessor tree, and hence the recovered path byte-for-byte identical
    across runs and machines.

HONEST SIMPLIFICATIONS (stated plainly, not hidden):
  * The terrain is SYNTHETIC (a closed-form function), not a real Digital Elevation Model.
  * The energy model is simplified: cost = horizontal base cost + an uphill penalty
    proportional to positive elevation gain only; descent costs only the base (no
    regenerative gain, no negative cost). It ignores rover dynamics, wheel slip, soil
    bearing, slope-stability limits, turning costs, and power/thermal constraints.
  * Space is discretized to a grid with 4-connectivity (axis-aligned moves only), so paths
    are Manhattan-like, not smooth arcs.

Test-META is a zero-value testnet placeholder and never mints base supply (MIP-0001
paragraph 3, MIP-0002 paragraph 8). Not financial, legal, or flight-engineering advice.
No NASA affiliation or endorsement.

Standard library only (math, json, hashlib, heapq).

Interface is identical to the other tasks so the verifier and agent loop can use them
interchangeably: compute() -> dict, canonical_json(result) -> str, output_hash(result) -> str.
"""

import heapq
import json
import math

# --- Fixed grid + endpoints (part of the reproducibility hash) --------------
GRID_ROWS = 32
GRID_COLS = 32
# Start and goal are on the SAME row, on opposite sides of the ridge wall. The direct route
# between them is a straight horizontal line straight into the wall; the only low crossing
# is the gap far below (GAP_ROW), so the lowest-energy path must take a long U-shaped detour.
START = (15, 2)       # (row, col) start cell
GOAL = (15, 29)       # (row, col) goal cell

# --- Fixed terrain generator (closed-form, deterministic) -------------------
# Elevation at (r, c), in integer centimeters, is the rounded sum of three fixed sinusoids
# (gentle rolling slopes, ridges, and a basin) PLUS a tall ridge WALL with a single low
# GAP. The wall is a Gaussian in column (spanning every row, so it cannot be skirted at the
# ends); the gap is a Gaussian dimple in row that drops the wall to ~5% height at GAP_ROW.
# Because the wall blocks every minimal start->goal path, the lowest-energy route MUST
# deviate toward the offset gap — a genuine "longer but cheaper" detour, not an equal-length
# corner. The sinusoids give general slope texture. Amplitudes are in centimeters.
#   rolling(r,c) = 120*sin(2*pi*c/13) + 90*cos(2*pi*r/17) + 70*sin(2*pi*(r+c)/19)
#   wall(r,c)    = WALL_AMP_CM * exp(-((c-WALL_COL)^2)/(2*WALL_SIGMA_COL^2))
#                              * (1 - GAP_DEPTH * exp(-((r-GAP_ROW)^2)/(2*GAP_SIGMA_ROW^2)))
#   elevation_cm(r,c) = round(rolling(r,c) + wall(r,c))
WALL_AMP_CM = 2500.0    # ridge-wall height, centimeters (25 m)
WALL_COL = 16           # wall column (between start and goal)
WALL_SIGMA_COL = 1.6    # wall thickness (cells)
GAP_ROW = 28            # row of the single low gap (far from the start/goal row -> long detour)
GAP_SIGMA_ROW = 2.0     # gap width (cells)
GAP_DEPTH = 0.95        # fraction the gap removes from wall height (0.95 -> ~5% remains)

# --- Fixed energy model (all integer; no float ties) ------------------------
CONNECTIVITY = 4       # 4-connectivity: axis-aligned moves only (up/down/left/right)
BASE_COST = 50         # integer horizontal cost per move (per cell)
UPHILL_FACTOR = 1      # integer uphill penalty per centimeter of POSITIVE elevation gain
# Fixed neighbor relaxation order (row, col deltas): up, down, left, right.
_NEIGHBOR_DELTAS = ((-1, 0), (1, 0), (0, -1), (0, 1))


def _elevation_cm(r: int, c: int) -> int:
    """Closed-form synthetic elevation at cell (r, c), in integer centimeters."""
    rolling = (
        120.0 * math.sin(2.0 * math.pi * c / 13.0)
        + 90.0 * math.cos(2.0 * math.pi * r / 17.0)
        + 70.0 * math.sin(2.0 * math.pi * (r + c) / 19.0)
    )
    gap_factor = 1.0 - GAP_DEPTH * math.exp(
        -((r - GAP_ROW) ** 2) / (2.0 * GAP_SIGMA_ROW ** 2)
    )
    wall = (
        WALL_AMP_CM
        * math.exp(-((c - WALL_COL) ** 2) / (2.0 * WALL_SIGMA_COL ** 2))
        * gap_factor
    )
    return int(round(rolling + wall))


def _build_grid() -> list:
    """Materialize the fixed elevation grid as a list of lists of integer centimeters."""
    return [[_elevation_cm(r, c) for c in range(GRID_COLS)] for r in range(GRID_ROWS)]


def _step_cost(elev_from: int, elev_to: int) -> int:
    """Integer move cost: base horizontal cost + uphill penalty on positive gain only."""
    gain = elev_to - elev_from
    uphill = UPHILL_FACTOR * gain if gain > 0 else 0
    return BASE_COST + uphill


def _dijkstra(grid: list):
    """Lowest-energy Dijkstra search from START to GOAL over the integer-cost grid.

    Heap entries are (cost, cell_index, cell). cell_index = row*GRID_COLS+col is a unique,
    fixed total order, so equal-cost states pop deterministically. Predecessors are recorded
    only on a strict improvement, and neighbors are relaxed in _NEIGHBOR_DELTAS order — the
    combination makes the predecessor tree (and the recovered path) fully reproducible.
    """
    def index(cell):
        return cell[0] * GRID_COLS + cell[1]

    dist = {START: 0}
    pred = {START: None}
    heap = [(0, index(START), START)]

    while heap:
        cost, _, cell = heapq.heappop(heap)
        if cost > dist.get(cell, math.inf):
            continue  # stale entry
        if cell == GOAL:
            break
        r, c = cell
        elev_here = grid[r][c]
        for dr, dc in _NEIGHBOR_DELTAS:
            nr, nc = r + dr, c + dc
            if 0 <= nr < GRID_ROWS and 0 <= nc < GRID_COLS:
                neighbor = (nr, nc)
                new_cost = cost + _step_cost(elev_here, grid[nr][nc])
                if new_cost < dist.get(neighbor, math.inf):
                    dist[neighbor] = new_cost
                    pred[neighbor] = cell
                    heapq.heappush(heap, (new_cost, index(neighbor), neighbor))

    # Recover the path from GOAL back to START via predecessors.
    path = []
    node = GOAL
    while node is not None:
        path.append(node)
        node = pred.get(node)
    path.reverse()
    return path, dist[GOAL]


def compute() -> dict:
    """Plan the lowest-energy rover path and return the structured result."""
    grid = _build_grid()
    path, total_cost = _dijkstra(grid)

    # Per-step statistics (all integer).
    max_step_uphill = 0
    for (r0, c0), (r1, c1) in zip(path, path[1:]):
        gain = grid[r1][c1] - grid[r0][c0]
        uphill = UPHILL_FACTOR * gain if gain > 0 else 0
        if uphill > max_step_uphill:
            max_step_uphill = uphill

    elev_start = grid[START[0]][START[1]]
    elev_goal = grid[GOAL[0]][GOAL[1]]

    return {
        "task_id": "task-0005-rover-path",
        "inputs": {
            "grid_rows": GRID_ROWS,
            "grid_cols": GRID_COLS,
            "start": list(START),
            "goal": list(GOAL),
            "connectivity": CONNECTIVITY,
            "base_cost": BASE_COST,
            "uphill_factor": UPHILL_FACTOR,
            "generator": (
                "elevation_cm(r,c) = round("
                "120*sin(2*pi*c/13) + 90*cos(2*pi*r/17) + 70*sin(2*pi*(r+c)/19)"
                " + 2500*exp(-((c-16)^2)/(2*1.6^2))"
                "*(1 - 0.95*exp(-((r-28)^2)/(2*2.0^2))))"
            ),
            "tie_break": "ascending cell_index = row*grid_cols+col (fixed total order)",
        },
        "results": [list(cell) for cell in path],
        "summary": {
            "path_length_cells": len(path),
            "total_energy_cost": total_cost,
            "total_horizontal_distance": len(path) - 1,
            "net_elevation_change": elev_goal - elev_start,
            "max_single_step_uphill_cost": max_step_uphill,
        },
    }


def canonical_json(result: dict) -> str:
    """Serialize the result deterministically.

    sort_keys=True, fixed compact separators, and ensure_ascii=True make the output
    byte-stable across runs and platforms (all emitted values are integers/strings).
    """
    return json.dumps(
        result,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


def output_hash(result: dict) -> str:
    """Return the SHA-256 hex digest of the canonical JSON (the Gate-2 reproducibility hash)."""
    import hashlib

    return hashlib.sha256(canonical_json(result).encode("utf-8")).hexdigest()


if __name__ == "__main__":
    _result = compute()
    print(canonical_json(_result))
    print("sha256:" + output_hash(_result))
