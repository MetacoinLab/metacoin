"""MetaCoin work service: authenticated API, durable job queue, encrypted artifacts,
signed reviews, x402 over HTTP, and a server-rendered console.

Optional dependencies (extra `service`, see pyproject): fastapi, uvicorn, jinja2,
python-multipart, pyrage (age), cryptography (Ed25519), x402 (transport). The
protocol core and the experiments stay standard-library only; this package is
never imported by them.
"""
__version__ = '0.1.0'
