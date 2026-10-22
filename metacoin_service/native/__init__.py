"""MetaCoin native space-work and settlement protocol v0 (order of 2026-10-22).

Dependency direction: protocol types and transition rules (protocol.py) -> accounts, grants, obligations, domains and
the authoritative journal (ledger.py, service.py, domains.py) -> work acceptance and MetaStar circulation (hooks in the
economy package) -> API / CLI / console / MCP / offline checker (routes.py, checker.py). Nothing here imports x402; the
optional rails stay behind the economy's payment-intent boundary."""
