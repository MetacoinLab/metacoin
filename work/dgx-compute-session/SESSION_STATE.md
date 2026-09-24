# DGX compute engine session state
Start: 2026-09-23 23:41 MDT (America/Edmonton). Branch service/real-features, starting revision d18e490 (clean tree, schema 011).
Live: tmux metacoin-service (API 127.0.0.1:8402 + live-worker) at d18e490, provider mode test-http.
Host: DGX Spark, NVIDIA GB10 (cc 12.1), driver 580.126.09, aarch64, 20 CPUs, 119 GiB unified memory, /usr/local/cuda-13.0.
System python3 3.12.3 has torch 2.10.0+cu130, cupy-cuda12x 14.0.1, numba 0.61.2, numpy 2.2.6; .venv-service has none of these.
## Plan
A1 inventory + compute runtime (.venv-compute or system interpreter for the child) -> A2 compute substrate (manifests, compute_runs, chunked child protocol, progress, checkpoints, pause/cancel, reservations, lease renewal, telemetry)
B1 temporal_batch CPU (exact int64 numpy) + GPU (torch int64) + verification -> B2 monte_carlo_reliability -> B3 heat_diffusion (CPU numpy / GPU torch float64) + numerical acceptance
C catalog/quotes/usage units, workflows/campaigns nodes, CLI, console Compute pages, journeys (12), benchmarks, delivery ~/metacoin-dgx-compute-delivery-2026-09-24
## Done
- A1 inventory: GB10 cc12.1 driver 580.126.09; system python3 has torch 2.10.0+cu130 (device execution verified: int64 cumsum exact, float64 sum), numpy 2.2.6, pynvml (energy counter OK, memory unsupported), psutil; cupy broken; scipy absent (downloadable). Compute child interpreter = /usr/bin/python3 (probed by engine.compute_interpreter).
- A2/B: metacoin_service/compute/{npy,container,inputs,manifests,kernels,reference,verify,exec,engine,service}.py; migration 012 (compute_runs, compute_checkpoints, compute_reservations, compute_work_units, jobs.hold); contracts/catalog/jobs/worker/scheduling/metering/reviews/reuse/api integrated; client_cli compute-* commands
- tests: test_compute_science (9 OK under python3: CPU+CUDA exact vs reference, MC chunk invariance, heat vs scalar reference/eigenmode/refinement, npy codec); smoke: temporal batch 364 scenarios cpu verified exact_all through API + in-process worker
## Running processes created by this session
(none besides the pre-existing live tmux session; test children are spawned and reaped by the suites)
## Running processes created by this session
(none besides the pre-existing live tmux session)
- test_compute_engine 8/8 (cuda batch byte-identical to cpu, pause/resume, cancel, worker SIGKILL recovery from checkpoint under new generation, corrupted/foreign checkpoint refused, refusals, quotes/usage, capacity race); console Compute pages + job compute section; campaigns/workflows accept compute kinds
- commit 21c4a91 (compute engine); test_compute_integration 3/3; journeys_compute 12/12 after fixes (run1: 10/12, J5/J7 fixed); benchmarks: engine e2e (cpu 0.2-0.6 s small jobs; cuda +1.3 s child context; checkpoint overhead 5.37 vs 3.94 s on 885-unit heat), kernels pending; README compute section; live backup pre-migration-012-1790232347
- commit 1c56208: evidence-based auto selection (AUTO_CUDA_MIN_WORK), journeys 12/12 (journeys-compute-final.json), kernel + engine benchmarks, README; live migrated to 012 and restarted at 1c56208 (live-worker device:cuda; CLI heat job ran on cuda and verified); browser journey4 15/16 (console pause timing on the live instance)
- delivery dir populated: test-logs (101 OK + 9 science OK + inherited all OK), journeys, benchmarks, manifests, ledger
## Next action
fix/verify console pause in journey4; write IMPLEMENTATION_REPORT + verification-results + MIGRATIONS + patch/zip/scans/clean-export/manifest; then §53 extensions (6 second worker env routing journey, 1 planning) if time (lifecycle: cuda, pause/resume, cancel, worker kill recovery, corrupted checkpoint, capacity race); fix; commit; then C: workflows/campaign nodes for compute kinds, console Compute pages, journeys (12), benchmarks, live upgrade (backup+migrate 012+restart), delivery
