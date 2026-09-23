"""Local pilot command surface: owner, worker, auditor, actor and public verifier steps.

Every command has one trust context and one side-effect boundary (see README).
Actor names identify roles in a trusted local operator context; they are not
remote authentication. Refusals print a stable code and a constant reason;
no private value, path or upstream exception text is echoed.
"""
import argparse
import json
from pathlib import Path
import sys
import time
from experiments.private_receipts import receipt as merkle
from integrations.x402.legacy_adapter import LegacyAdapter
from . import acceptance, contract, demo, energy_analysis as energy, explanation, fixtures, packages, refusals, verifiers
from .execution_state import Journal

ADAPTERS = ('legacy-simulation', 'durable-test-simulation')


def capabilities():
    """Machine-readable capability table: what is enforced, what is only recorded."""
    fields = {
        'schema': 'enforced-exact-match', 'job_id': 'enforced-token;unique-per-journal',
        'owner': 'enforced-against-caller-supplied-role;not-authenticated',
        'auditor': 'enforced-against-caller-supplied-role;not-authenticated',
        'input_authority': 'descriptive', 'input_root': 'enforced-trust-anchor',
        'commitment_schema': 'enforced-exact-match', 'evidence_kind': 'enforced-exact-match',
        'verifier_id': 'enforced-allowlist', 'verifier_digest': 'enforced-installed-bundle-or-historical-read-only',
        'result_schema': 'enforced-exact-match', 'model_id': 'enforced-exact-match',
        'units': 'enforced-exact-match', 'assumptions': 'enforced-exact-match',
        'uncertainty': 'enforced-exact-match', 'domain': 'enforced-by-input-validation',
        'accepted_outcomes': 'enforced-completion-policy', 'allowed_disclosures': 'enforced-on-public-verify',
        'required_disclosures': 'enforced-on-audit-and-verify', 'action': 'enforced-binding-on-reserve',
        'expires_at': 'enforced-for-new-authorization-only;status-and-reconcile-remain',
        'dispute': 'descriptive;no-arbitration-implemented',
        'retention_seconds': 'descriptive;no-deletion-service-implemented',
        'access': 'descriptive;local-owner-process-filesystem-clock-trusted'}
    return {'contract_fields': fields, 'verifier': {'current_id': verifiers.CURRENT_ID,
                                                    'current_digest': contract.verifier_digest(),
                                                    'historical_read_only': list(verifiers.HISTORICAL)},
            'adapters': {'legacy-simulation': LegacyAdapter.CAPABILITIES,
                         'durable-test-simulation': _durable().CAPABILITIES,
                         'x402-loopback-test': _loopback_capabilities()},
            'transport': {'http_402': 'unavailable-over-a-socket;header-level-compatibility-tested-offline-with-x402-sdk-2.24.0',
                          'x402_sdk': 'optional;isolated-venv-only;see integrations/x402/README.md',
                          'network_settlement': 'unavailable', 'external_verification': 'not-performed'},
            'evidence': {'private_audit': 'full-recomputation-by-authorized-local-auditor',
                         'public_verification': 'salted-merkle-membership-and-bindings-only',
                         'zero_knowledge': False, 'encryption_at_rest': False, 'issuer_signature': False},
            'packages': {'public': packages.PUBLIC_SCHEMA, 'private_audit': packages.PRIVATE_SCHEMA,
                         'import_executes_code': False},
            'external_team_pilot': 'not-performed', 'refusal_codes': sorted(refusals.ACTIONS)}


def _durable():
    from .tests.durable_provider import DurableProvider  # testing facility, imported lazily
    return DurableProvider


def _loopback_capabilities():
    from integrations.x402 import loopback_harness
    if not loopback_harness.available():
        return {'capability': 'x402_loopback_test', 'status': 'sdk-not-installed-in-this-interpreter;tests-skip',
                'sdk': {'package': 'x402', 'version': loopback_harness.SDK_VERSION}}
    from integrations.x402.loopback_adapter import LoopbackAdapter
    return LoopbackAdapter.CAPABILITIES


def build_adapter(args):
    if args.adapter == 'legacy-simulation':
        adapter = LegacyAdapter(fixtures.funded_faucet(amount=args.legacy_funding))
        session = {'adapter_session': 'process-scoped',
                   'funding': 'fresh in-memory fixture faucet funded in THIS process by the separately '
                              'labeled lunar-link task; not a reward for the new analysis',
                   'persistence': 'balance and outcomes vanish when this process exits; a later '
                                  'process cannot reconcile them and the journal keeps exposure'}
    else:
        path = Path(args.provider_state)
        exists = path.exists()
        if not exists and args.provider_initial_balance is None:
            raise merkle.Invalid('provider state missing; initial balance required to create it')
        adapter = _durable()(path, initial_balance=None if exists else args.provider_initial_balance)
        session = {'adapter_session': 'file-backed testing facility (not a payment system)',
                   'persistence': 'request-bound outcomes survive process exit in the provider file'}
    return adapter, session


def emit(result):
    print(json.dumps(result, sort_keys=True, indent=2))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)

    fixture = sub.add_parser('fixture', help='write a synthetic input file')
    fixture.add_argument('--out', required=True)
    fixture.add_argument('--outcome', choices=energy.OUTCOMES, default='FEASIBLE')

    prepare = sub.add_parser('prepare', help='owner: commit inputs and fix the contract terms')
    prepare.add_argument('--input', required=True)
    prepare.add_argument('--out-dir', required=True)
    prepare.add_argument('--job', required=True)
    prepare.add_argument('--expires-at', required=True, type=int)
    prepare.add_argument('--amount', default=1, type=int)
    prepare.add_argument('--require-feasible', action='store_true')
    prepare.add_argument('--hide-outcome', action='store_true')
    prepare.add_argument('--disclose-explanation', action='store_true',
                         help='permit the margin explanation in PUBLIC openings (numbers become public)')
    prepare.add_argument('--capability', choices=contract.CAPABILITIES, default='legacy_simulation')

    for name in ('execute', 'audit', 'verify', 'explain', 'export-public', 'export-private'):
        part = sub.add_parser(name)
        part.add_argument('--contract', required=True)
        part.add_argument('--expected-contract-digest', required=name not in ('export-private',))
        if name in ('execute', 'audit', 'explain', 'export-private'):
            part.add_argument('--input-vault', required=True)
        if name in ('execute', 'audit'):
            part.add_argument('--out', required=True)
        if name == 'audit':
            part.add_argument('--evidence-vault', required=True)
        if name == 'export-private':
            part.add_argument('--evidence-vault', required=True)
            part.add_argument('--out', required=True)
        if name in ('verify', 'export-public'):
            part.add_argument('--bundle', required=True)
            part.add_argument('--expected-root', required=True)
        if name == 'export-public':
            part.add_argument('--out', required=True)
        if name == 'explain':
            part.add_argument('--added-usable-energy', type=int, default=None,
                              help='hypothetical mJ added to both usable-energy bounds')

    imp = sub.add_parser('import-public', help='verify a public package with the OPERATOR pins')
    imp.add_argument('--package', required=True)
    imp.add_argument('--expected-contract-digest', required=True)
    imp.add_argument('--expected-root', required=True)
    imp.add_argument('--out-dir')
    imp_private = sub.add_parser('import-private', help='auditor: extract a private audit package')
    imp_private.add_argument('--package', required=True)
    imp_private.add_argument('--out-dir', required=True)

    campaign = sub.add_parser('campaign', help='owner: create or inspect the local campaign journal')
    campaign.add_argument('action', choices=('init', 'show'))
    campaign.add_argument('--journal', required=True)
    campaign.add_argument('--campaign')
    campaign.add_argument('--limit', type=int)

    register = sub.add_parser('register', help='owner: pin a contract in the journal (one entitlement per job)')
    register.add_argument('--journal', required=True)
    register.add_argument('--contract', required=True)
    register.add_argument('--expected-contract-digest', required=True)
    register.add_argument('--owner', default='local-owner')

    record = sub.add_parser('record-audit', help='auditor: full private recomputation recorded in the journal')
    record.add_argument('--journal', required=True)
    record.add_argument('--job', required=True)
    record.add_argument('--input-vault', required=True)
    record.add_argument('--evidence-vault', required=True)
    record.add_argument('--auditor', default='local-auditor')
    record.add_argument('--out', required=True, help='public bundle output (fresh path)')

    request = sub.add_parser('request', help='actor: build the bound action request (read-only)')
    request.add_argument('--journal', required=True)
    request.add_argument('--job', required=True)
    request.add_argument('--request-id', required=True)
    request.add_argument('--out', required=True)

    for name in ('dispatch', 'reconcile', 'status'):
        part = sub.add_parser(name)
        part.add_argument('--journal', required=True)
        part.add_argument('--actor', default='agent-fixture')
        if name == 'dispatch':
            part.add_argument('--request', required=True)
            part.add_argument('--dry-run', action='store_true', help='all checks, no reservation, no dispatch')
        else:
            part.add_argument('--request-id', required=True)
        if name != 'status':
            part.add_argument('--adapter', choices=ADAPTERS, required=True)
            part.add_argument('--legacy-funding', type=int, default=10)
            part.add_argument('--provider-state')
            part.add_argument('--provider-initial-balance', type=int)

    sub.add_parser('capabilities', help='machine-readable capability table')
    show = sub.add_parser('demo')
    show.add_argument('--out')
    args = parser.parse_args(argv)
    try:
        emit(run(args))
        return 0
    except Exception as exc:
        code, reason = refusals.classify(exc)
        emit({'refused': True, 'code': code, 'reason': reason, 'action': refusals.ACTIONS[code]})
        print('REFUSED ' + code + ': ' + refusals.ACTIONS[code], file=sys.stderr)
        return 2


def run(args):
    now = int(time.time())
    if args.command == 'fixture':
        merkle.write_new(args.out, fixtures.inputs(args.outcome))
        return {'synthetic_fixture_created': True}
    if args.command == 'prepare':
        data = merkle.read(args.input)
        energy.validate(data)
        if args.expires_at <= now:
            raise merkle.Invalid('expiration must be in the future')
        root, vault = merkle.commit({'inputs': data})
        terms = contract.make(args.job, root['root'], args.expires_at, amount=args.amount,
                              accepted_outcomes=('FEASIBLE',) if args.require_feasible else energy.OUTCOMES,
                              disclose_outcome=not args.hide_outcome, disclose_explanation=args.disclose_explanation,
                              capability=args.capability)
        directory = Path(args.out_dir)
        directory.mkdir(mode=0o700, parents=False, exist_ok=False)
        merkle.write_new(directory / 'contract.json', terms)
        merkle.write_new(directory / 'private-input-vault.json', vault)
        merkle.write_new(directory / 'owner-pin.json', {'contract_digest': contract.digest(terms)})
        return {'prepared': True, 'contract_digest': contract.digest(terms), 'verifier_id': terms['verifier_id'],
                'trust_scope': 'owner-local-pin;vault-is-private-plaintext'}
    if args.command == 'demo':
        result = demo.run()
        if args.out:
            merkle.write_new(args.out, result)
            result = {key: value for key, value in result.items() if key != 'public_samples'}
        return result
    if args.command == 'capabilities':
        return capabilities()
    if args.command == 'import-public':
        return packages.import_public(args.package, args.expected_contract_digest, args.expected_root, args.out_dir)
    if args.command == 'import-private':
        return packages.import_private(args.package, args.out_dir)
    if args.command == 'campaign':
        if args.action == 'init':
            if args.campaign is None or args.limit is None:
                raise merkle.Invalid('campaign identifier and limit required')
            if Path(args.journal).exists():
                raise merkle.Invalid('campaign configuration is immutable')
            return dict(Journal(args.journal, args.campaign, args.limit).inspect(), created=True)
        return Journal.open(args.journal).inspect()
    if args.command == 'register':
        journal = Journal.open(args.journal)
        terms = merkle.read(args.contract)
        journal.register(terms, args.expected_contract_digest, args.owner, now)
        return {'registered': True, 'job_id': terms['job_id'], 'contract_digest': args.expected_contract_digest,
                'entitlement': 'one-action-per-job;terms-immutable'}
    if args.command == 'record-audit':
        journal = Journal.open(args.journal)
        audited = journal.audit(args.job, merkle.read(args.input_vault), merkle.read(args.evidence_vault), args.auditor, now)
        merkle.write_new(args.out, audited['bundle'])
        return {'recorded': True, 'audit_scope': audited['audit_scope'], 'work_completed': audited['work_completed'],
                'expected_evidence_root': audited['evidence_root'], 'spend_permitted': False,
                'next': 'request -> dispatch (actor) if work_completed'}
    if args.command == 'request':
        journal = Journal.open(args.journal)
        request = journal.request(args.job, args.request_id)
        merkle.write_new(args.out, request)
        return {'request_written': True, 'binding': request, 'side_effects': 'none'}
    if args.command in ('dispatch', 'reconcile', 'status'):
        journal = Journal.open(args.journal)
        if args.command == 'status':
            return journal.status(args.request_id, args.actor)
        adapter, session = build_adapter(args)
        if args.command == 'dispatch':
            request = merkle.read(args.request)
            if args.dry_run:
                return dict(journal.preview(request, args.actor, adapter, now), **session)
            return dict(journal.dispatch(request, args.actor, adapter, now), **session,
                        adapter_capabilities=getattr(adapter, 'CAPABILITIES', {}))
        return dict(journal.reconcile(args.request_id, args.actor, adapter, now), **session)
    # contract-file commands
    terms = merkle.read(args.contract)
    if args.command == 'verify':
        return acceptance.verify_public(terms, args.expected_contract_digest, merkle.read(args.bundle), args.expected_root)
    if args.command == 'export-public':
        return packages.export_public(terms, args.expected_contract_digest, merkle.read(args.bundle),
                                      args.expected_root, args.out)
    if args.command == 'export-private':
        return packages.export_private(terms, merkle.read(args.input_vault), merkle.read(args.evidence_vault), args.out)
    if args.command == 'execute':
        _, vault = acceptance.execute(terms, args.expected_contract_digest, merkle.read(args.input_vault))
        merkle.write_new(args.out, vault)
        return {'executed': True, 'output_is_private_plaintext': True}
    if args.command == 'explain':
        contract.trusted(terms, args.expected_contract_digest)
        inputs = acceptance.inputs_for(terms, merkle.read(args.input_vault))
        result = {'privacy': 'PRIVATE-audit-only-output;numerical-widths-are-private-unless-the-contract-discloses-them',
                  'contract_digest': args.expected_contract_digest, 'input_root': terms['input_root'],
                  'explanation': explanation.explain(inputs), 'threshold_check': explanation.threshold_check(inputs)}
        if args.added_usable_energy is not None:
            result['counterfactual'] = explanation.conditional_outcome(inputs, args.added_usable_energy)
        return result
    # audit (stateless)
    if now >= terms['expires_at']:
        raise merkle.Invalid('contract expired')
    audited = acceptance.audit(terms, args.expected_contract_digest, merkle.read(args.input_vault), merkle.read(args.evidence_vault))
    merkle.write_new(args.out, audited['bundle'])
    return {'audit_scope': audited['audit_scope'], 'work_completed': audited['work_completed'],
            'expected_evidence_root': audited['evidence_root'], 'spend_permitted': False,
            'recorded_in_journal': False}


if __name__ == '__main__':
    raise SystemExit(main())
