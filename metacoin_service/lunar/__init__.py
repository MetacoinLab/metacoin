"""Three connected lunar products (order §11) on the ordinary worker, evidence and acceptance path:

  terrain_tile       elevation + slope with explicit missing-data, edge, projection-scale and uncertainty treatment, PNG preview
  route_feasibility  bounded exact search on the tile under a declared rover/energy model: FEASIBLE / INFEASIBLE / INDETERMINATE
  scenario_pack      reproducible, downloadable pack: pinned inputs, deterministic seeds, variations, reference outputs

Pure Python integer/float arithmetic with an independent CPU reference (reference.py); GPU acceleration is an optional
benchmark, never a verification path. A coarse DEM is not traversability truth: every product states its assumptions."""
