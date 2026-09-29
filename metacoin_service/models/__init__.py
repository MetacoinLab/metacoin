"""Trusted local model services: registry of immutable model revisions, a task-owned runtime child (transformers +
torch under the compute interpreter), bounded generation and embedding jobs, durable output segments, and the
host-side lifecycle (load, readiness, drain). Model output is data: it never widens a grant, signs a review, or
changes a numerical result. Keep this package importable without torch (the child imports it lazily)."""
