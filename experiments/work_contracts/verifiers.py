"""Allowlisted verifier bundles: the current bundle may authorize; historical ones are read-only.

The verifier digest pins the installed analysis/acceptance source bytes
(see contract.verifier_digest). Any legitimate upgrade of those files changes
the digest, so contracts and receipts produced under the previous bundle
would otherwise become unverifiable. This module is the local trust policy
that says which superseded bundles may still be *read*:

  current    -> new registration, audit, and spend authorization permitted
  historical -> public membership verification and journal inspection only;
                registration, audit, request and dispatch are refused by name

This file is deliberately NOT part of the digested bundle: it is policy about
bundles, not computation. Only explicitly listed digests are accepted; a digest
named by a submitter, a contract, or a receipt is never added here at runtime.
"""

CURRENT_ID = 'local-energy-audit/v1'

# digest -> what it was. Never remove a row; never edit a historical record to
# match new code. Rows are added by a human when a bundle is superseded.
HISTORICAL = {
    '5e5b7cd66796359eaa54d7fe05069588ffee65fc5ac3f9930a77bbee32f53204': {
        'verifier_id': 'local-energy-audit/v0',
        'superseded': '2026-09-23',
        'reason': 'v1 adds the audit-only margin decomposition field, response '
                  'consistency checks and refusal codes; the energy model, units, '
                  'domain and outcome rules are unchanged',
        'evidence_fields': ['contract_digest', 'input_root', 'verifier_id', 'verifier_digest',
                            'result_schema', 'model_id', 'scope', 'outcome', 'audit_details'],
    },
}

MODES = ('current', 'historical')


def status(verifier_id, verifier_digest, current_digest):
    """Return 'current', 'historical', or None (unknown / not allowlisted)."""
    if verifier_id == CURRENT_ID and verifier_digest == current_digest:
        return 'current'
    row = HISTORICAL.get(verifier_digest)
    if row is not None and row['verifier_id'] == verifier_id:
        return 'historical'
    return None
