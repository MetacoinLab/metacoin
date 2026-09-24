"""Compute engine: trusted task manifests, bounded chunked execution in a task-owned child process,
encrypted checkpoints with atomic publication, resume under new fencing, verification phases, and
three scientific services (temporal batches, Monte Carlo reliability, 2-D heat diffusion).

The orchestration side (engine.py) runs inside the ordinary worker and needs only the service
dependencies. The numerical side (exec.py + kernels) runs in a separate interpreter that carries
numpy and, when present, a CUDA-capable torch; nothing here executes user-supplied code."""
