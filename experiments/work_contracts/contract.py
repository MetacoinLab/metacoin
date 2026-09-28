"""Immutable local-owner contracts. Actor names are not remote authentication."""
import hashlib
from pathlib import Path
import re
from experiments.private_receipts import receipt as merkle
from . import energy_analysis as energy, verifiers

SCHEMA = 'metacoin-work-contract/v0-experimental'
VERIFIER = verifiers.CURRENT_ID
SCOPE = 'local-private-recomputation;public-membership-only'
BINDINGS = ('contract_digest', 'input_root', 'verifier_id', 'verifier_digest',
            'result_schema', 'model_id', 'scope')
# Fields a contract MAY permit in public openings. The two explanation fields
# are private unless the owner's disclosure policy names them explicitly.
PUBLIC_FIELDS = set(BINDINGS) | {'outcome', 'margin_explanation', 'dominant_uncertainty_source'}
TOKEN = re.compile(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}\Z')
ACTION_KEYS = ('actor', 'recipient', 'resource', 'amount', 'limit', 'asset', 'network', 'capability')


def token(value):
    if type(value) is not str or not TOKEN.fullmatch(value):
        raise merkle.Invalid('invalid identifier')
    return value


def verifier_digest():
    """Pin the installed evaluator and all local acceptance/schema dependencies."""
    here = Path(__file__).parent
    files = [('energy_analysis.py', here / 'energy_analysis.py'),
             ('explanation.py', here / 'explanation.py'),
             ('contract.py', here / 'contract.py'),
             ('acceptance.py', here / 'acceptance.py'),
             ('private_receipt.py', Path(merkle.__file__))]
    values = [[name, hashlib.sha256(path.read_bytes()).hexdigest()] for name, path in files]
    return hashlib.sha256(b'metacoin/verifier-bundle/v1\0' + merkle.canonical(values)).hexdigest()


def verifier_status(contract):
    """'current' | 'historical' | None for the verifier a contract names."""
    return verifiers.status(contract.get('verifier_id'), contract.get('verifier_digest'), verifier_digest())


def validate(contract, mode='current'):
    """Structural + semantic validation.

    mode='current'    the installed bundle must be the one the contract names
                      (required before any new registration, audit or spend).
    mode='historical' a superseded, allowlisted bundle is also accepted; the
                      caller may only read/verify, never execute or authorize.
    """
    if mode not in verifiers.MODES:
        raise ValueError('unknown validation mode')
    merkle.canonical(contract)
    energy.exact(contract, ('schema', 'job_id', 'owner', 'auditor', 'input_authority',
                           'input_root', 'commitment_schema', 'evidence_kind',
                           'verifier_id', 'verifier_digest', 'result_schema', 'model_id',
                           'units', 'assumptions', 'uncertainty', 'domain',
                           'accepted_outcomes', 'allowed_disclosures', 'required_disclosures',
                           'action', 'expires_at', 'dispute', 'retention_seconds', 'access'))
    for name in ('job_id', 'owner', 'auditor', 'input_authority'):
        token(contract[name])
    merkle._hex(contract['input_root'])
    merkle._hex(contract['verifier_digest'])
    status = verifier_status(contract)
    if status is None:
        raise merkle.Invalid('unknown verifier bundle')
    if status != 'current' and mode == 'current':
        raise merkle.Invalid('verifier bundle superseded; read-only verification only')
    if (contract['schema'] != SCHEMA or contract['commitment_schema'] != merkle.SCHEMA
            or contract['evidence_kind'] != merkle.KIND
            or contract['result_schema'] != energy.RESULT_SCHEMA
            or contract['model_id'] != energy.MODEL_ID
            or contract['units'] != energy.UNITS or contract['assumptions'] != energy.ASSUMPTIONS
            or contract['uncertainty'] != 'bounds-not-probabilities'
            or contract['domain'] != 'nonnegative-integers;1..128-segments;aggregate<=2^53-1'
            or contract['access'] != 'owner-controlled-local-audit'
            or contract['dispute'] != 'owner-auditor-review-no-automatic-refund'):
        raise merkle.Invalid('unsupported contract semantics or installed verifier')
    for name in ('accepted_outcomes', 'allowed_disclosures', 'required_disclosures'):
        value = contract[name]
        if (type(value) is not list or not value or not all(type(x) is str for x in value)
                or len(set(value)) != len(value)):
            raise merkle.Invalid('invalid policy list')
    if not set(contract['accepted_outcomes']) <= set(energy.OUTCOMES):
        raise merkle.Invalid('unsupported completion outcome')
    allowed, required = set(contract['allowed_disclosures']), set(contract['required_disclosures'])
    if not set(BINDINGS) <= required <= allowed <= PUBLIC_FIELDS:
        raise merkle.Invalid('incompatible disclosure policy')
    energy.integer(contract['expires_at'], 1)
    energy.integer(contract['retention_seconds'], 1)
    action = contract['action']
    energy.exact(action, ACTION_KEYS)
    for name in ('actor', 'recipient', 'resource', 'asset', 'network', 'capability'):
        token(action[name])
    energy.integer(action['amount'], 1)
    energy.integer(action['limit'], action['amount'])
    return contract


def digest(contract, mode='current'):
    validate(contract, mode)
    return hashlib.sha256(b'metacoin/work-contract/v0\0' + merkle.canonical(contract)).hexdigest()


def trusted(contract, expected_digest, mode='current'):
    merkle._hex(expected_digest)
    if digest(contract, mode) != expected_digest:
        raise merkle.Invalid('contract does not match owner pin')
    return contract


# Action destinations per adapter capability. Tokens only (the CAIP-2 network
# EXPERIMENTAL — private test chain only — not the protocol's money layer, which remains [SPEC] and zero-value by MIP-0001/0002; chain-agnostic law unchanged.
# id eip155:84532 is written eip155-84532; the loopback adapter maps it back).
ACTION_TEMPLATES = {
    'legacy_simulation': {'recipient': 'legacy-compute-provider', 'resource': 'next-compute',
                          'asset': 'Test-META', 'network': 'local-simulation'},
    'durable_test_simulation': {'recipient': 'legacy-compute-provider', 'resource': 'next-compute',
                                'asset': 'Test-META', 'network': 'local-simulation'},
    'x402_loopback_test': {'recipient': 'loopback-compute-provider', 'resource': 'next-compute',
                           'asset': 'usdc-test-identifier', 'network': 'eip155-84532'},
    'x402_http_buyer': {'recipient': 'remote-x402-resource', 'resource': 'next-compute',
                        'asset': 'usdc-test-identifier', 'network': 'eip155-84532'},
}
CAPABILITIES = tuple(ACTION_TEMPLATES)


def make(job_id, input_root, expires_at, actor='agent-fixture', amount=1,
         accepted_outcomes=energy.OUTCOMES, disclose_outcome=True,
         disclose_explanation=False, capability='legacy_simulation',
         owner='local-owner', auditor='local-auditor', retention_seconds=86400):
    if capability not in CAPABILITIES:
        raise merkle.Invalid('unsupported adapter capability or destination')
    destination = ACTION_TEMPLATES[capability]
    fields = list(BINDINGS) + (['outcome'] if disclose_outcome else [])
    if disclose_explanation:
        fields += ['margin_explanation', 'dominant_uncertainty_source']
    obj = {'schema': SCHEMA, 'job_id': job_id, 'owner': owner,
           'auditor': auditor, 'input_authority': owner,
           'input_root': input_root, 'commitment_schema': merkle.SCHEMA,
           'evidence_kind': merkle.KIND, 'verifier_id': VERIFIER,
           'verifier_digest': verifier_digest(), 'result_schema': energy.RESULT_SCHEMA,
           'model_id': energy.MODEL_ID, 'units': dict(energy.UNITS),
           'assumptions': list(energy.ASSUMPTIONS), 'uncertainty': 'bounds-not-probabilities',
           'domain': 'nonnegative-integers;1..128-segments;aggregate<=2^53-1',
           'accepted_outcomes': list(accepted_outcomes), 'allowed_disclosures': list(fields),
           'required_disclosures': list(fields),
           'action': {'actor': actor, 'amount': amount, 'limit': amount, 'capability': capability,
                      **destination},
           'expires_at': expires_at, 'dispute': 'owner-auditor-review-no-automatic-refund',
           'retention_seconds': retention_seconds, 'access': 'owner-controlled-local-audit'}
    validate(obj)
    return obj
