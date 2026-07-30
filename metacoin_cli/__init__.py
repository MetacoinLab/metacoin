"""metacoin_cli — the unified `metacoin` command over the MetaCoin protocol stack.

THIN ROUTING ONLY: every subcommand delegates to the existing, already-verified
protocol/demo modules; no protocol logic lives here. Packaging is ADDITIVE — the
repository's flat layout, every `python3 protocol/X.py` invocation, the selftest
runners, and CI all keep working unchanged.

VERSION SOURCE OF TRUTH: __version__ below is the single source; pyproject.toml
declares its version as dynamic and reads this attribute at build time.

Research-stage, ZERO-VALUE, no token. A passing verification proves
deterministic re-derivability of the anchored claims — not independence, not
usefulness, not value. Not consensus, not payment, not investment advice.
"""

__version__ = "0.1.0"

# The one-line honest banner every subcommand's --help carries.
BANNER = ("research-stage, zero-value, no token — verification proves "
          "deterministic re-derivability, not value")
