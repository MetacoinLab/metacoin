# Expansion examples (public API only)

Each script talks to a running MetaCoin service through its documented HTTP API with a private credential file
(`{"token": "..."}`, mode 0600). Nothing here imports the server or uses test-only flags. Synthetic inputs are
labelled; the payment mode is the instance's configured provider mode (`test-http` or `simulation` locally).

    export METACOIN_BASE_URL=http://127.0.0.1:8402
    export METACOIN_CREDENTIAL_FILE=~/.local/state/metacoin-service/credentials/owner.json   # never pass tokens on the command line

| Script | What it shows | Needs |
|---|---|---|
| `private_answer.py` | a collection, a document, an index build, a source-linked answer with byte-checked citations | a promoted embedding model (`model-register`/`promote`), a live worker |
| `calibrated_prediction.py` | a numeric dataset, a verified fit, a prediction with domain status, an out-of-domain refusal | a live worker with the compute interpreter |
| `audited_result.py` | a temporal batch, an independent analytical audit, the signed public statement and its verification | a live worker |
| `mcp_bounded_job.py` | an MCP client (stdio) discovering services, drafting a plan, submitting bounded work, reading a result | the `mcp` package in the client environment |
| `federated_execution.py` | enrol a node, run one job on it over TLS, inspect transfers and the fenced result | the API started with `serve --tls` (`node-tls` first); `METACOIN_TLS_BASE_URL` and `METACOIN_TLS_CA` |

Every script prints one JSON object with identifiers and outcomes; on a refusal it prints the server's machine-readable
error and exits non-zero. Missing optional dependencies are reported by name.
