"""Private full audit and public membership verification have different claims."""
from experiments.private_receipts import receipt as merkle
from . import contract as terms, energy_analysis as energy, explanation


def full_values(vault, expected_root):
    merkle.canonical(vault)
    # Full validation enforces unique names/slots and covers every hidden field.
    merkle._vault_tree(vault)
    if vault['receipt']['root'] != expected_root:
        raise merkle.Invalid('private evidence root does not match trusted pin')
    return {field['name']: field['value'] for field in vault['fields']}


def expected_evidence(contract, inputs):
    result = energy.analyze(inputs)
    explained = explanation.explain(inputs)
    return {'contract_digest': terms.digest(contract), 'input_root': contract['input_root'],
            'verifier_id': contract['verifier_id'], 'verifier_digest': contract['verifier_digest'],
            'result_schema': energy.RESULT_SCHEMA, 'model_id': energy.MODEL_ID,
            'scope': terms.SCOPE, 'outcome': result['outcome'], 'audit_details': result,
            # Audit-only by default; public only under an explicit disclosure policy.
            'margin_explanation': explained,
            'dominant_uncertainty_source': explained['dominant_uncertainty_source']}


def inputs_for(contract, input_vault):
    data = full_values(input_vault, contract['input_root'])
    energy.exact(data, ('inputs',))
    energy.validate(data['inputs'])
    return data['inputs']


def execute(contract, expected_contract_digest, input_vault):
    terms.trusted(contract, expected_contract_digest)
    return merkle.commit(expected_evidence(contract, inputs_for(contract, input_vault)))


def audit(contract, expected_contract_digest, input_vault, evidence_vault):
    terms.trusted(contract, expected_contract_digest)
    data = inputs_for(contract, input_vault)
    merkle.canonical(evidence_vault)
    merkle._vault_tree(evidence_vault)
    root = evidence_vault['receipt']['root']
    actual = full_values(evidence_vault, root)
    expected = expected_evidence(contract, data)
    if merkle.canonical(actual) != merkle.canonical(expected):
        raise merkle.Invalid('evidence differs from the complete recomputed result')
    accepted = expected['outcome'] in contract['accepted_outcomes']
    bundle = merkle.disclose(evidence_vault, contract['required_disclosures'])
    verify_public(contract, expected_contract_digest, bundle, root)
    return {'work_completed': accepted, 'scientific_outcome': expected['outcome'],
            'evidence_root': root, 'bundle': bundle, 'audit_scope': 'local-private-recomputation',
            'spend_permitted': False}  # only the stateful executor may reserve spending


def verify_public(contract, expected_contract_digest, bundle, expected_root):
    """Membership + binding check only. Historical (superseded) verifier bundles
    are accepted here because nothing is executed or authorized; the result
    says which. Never pass a root taken from the bundle itself as expected_root."""
    terms.trusted(contract, expected_contract_digest, mode='historical')
    status = terms.verifier_status(contract)
    values = merkle.verify(bundle, expected_root, contract['required_disclosures'])
    if not set(values) <= set(contract['allowed_disclosures']):
        raise merkle.Invalid('prohibited public disclosure')
    bindings = {'contract_digest': expected_contract_digest, 'input_root': contract['input_root'],
                'verifier_id': contract['verifier_id'], 'verifier_digest': contract['verifier_digest'],
                'result_schema': contract['result_schema'], 'model_id': contract['model_id'],
                'scope': terms.SCOPE}
    if any(values.get(key) != value for key, value in bindings.items()):
        raise merkle.Invalid('wrong evidence binding')
    if 'outcome' in values and values['outcome'] not in energy.OUTCOMES:
        raise merkle.Invalid('unsupported outcome')
    if 'margin_explanation' in values:
        shown = values['margin_explanation']
        if (type(shown) is not dict or shown.get('explanation_schema') != explanation.EXPLANATION_SCHEMA
                or shown.get('model_id') != contract['model_id']):
            raise merkle.Invalid('malformed disclosed explanation')
    if 'dominant_uncertainty_source' in values and type(values['dominant_uncertainty_source']) is not str:
        raise merkle.Invalid('malformed disclosed explanation')
    return {'membership_verified': True, 'disclosed': values, 'verifier_status': status,
            'task_correctness_proven': False, 'issuer_authenticated': False,
            'spend_permitted': False, 'kind': merkle.KIND}
