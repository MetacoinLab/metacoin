"""Synthetic fixtures, owner preparation, worker execution, audit and public checks."""
import argparse
import json
from pathlib import Path
import sys
import time
from experiments.private_receipts import receipt as merkle
from . import acceptance, contract, demo, energy_analysis as energy, fixtures


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    fixture = sub.add_parser('fixture')
    fixture.add_argument('--out', required=True)
    fixture.add_argument('--outcome', choices=energy.OUTCOMES, default='FEASIBLE')
    prepare = sub.add_parser('prepare')
    prepare.add_argument('--input', required=True)
    prepare.add_argument('--out-dir', required=True)
    prepare.add_argument('--job', required=True)
    prepare.add_argument('--expires-at', required=True, type=int)
    prepare.add_argument('--amount', default=1, type=int)
    prepare.add_argument('--require-feasible', action='store_true')
    prepare.add_argument('--hide-outcome', action='store_true')
    for name in ('execute', 'audit', 'verify'):
        part = sub.add_parser(name)
        part.add_argument('--contract', required=True)
        part.add_argument('--expected-contract-digest', required=True)
        if name != 'verify':
            part.add_argument('--input-vault', required=True)
            part.add_argument('--out', required=True)
        if name == 'audit':
            part.add_argument('--evidence-vault', required=True)
        if name == 'verify':
            part.add_argument('--bundle', required=True)
            part.add_argument('--expected-root', required=True)
    show = sub.add_parser('demo')
    show.add_argument('--out')
    args = parser.parse_args(argv)
    try:
        if args.command == 'fixture':
            merkle.write_new(args.out, fixtures.inputs(args.outcome))
            result = {'synthetic_fixture_created': True}
        elif args.command == 'prepare':
            data = merkle.read(args.input)
            energy.validate(data)
            if args.expires_at <= int(time.time()):
                raise merkle.Invalid('expiration must be in the future')
            root, vault = merkle.commit({'inputs': data})
            terms = contract.make(args.job, root['root'], args.expires_at, amount=args.amount,
                                  accepted_outcomes=('FEASIBLE',) if args.require_feasible else energy.OUTCOMES,
                                  disclose_outcome=not args.hide_outcome)
            directory = Path(args.out_dir)
            directory.mkdir(mode=0o700, parents=False, exist_ok=False)
            merkle.write_new(directory / 'contract.json', terms)
            merkle.write_new(directory / 'private-input-vault.json', vault)
            merkle.write_new(directory / 'owner-pin.json', {'contract_digest': contract.digest(terms)})
            result = {'prepared': True, 'contract_digest': contract.digest(terms),
                      'trust_scope': 'owner-local-pin;vault-is-private-plaintext'}
        elif args.command == 'demo':
            result = demo.run()
            if args.out:
                merkle.write_new(args.out, result)
                result = {key: value for key, value in result.items() if key != 'public_samples'}
        else:
            terms = merkle.read(args.contract)
            if args.command == 'verify':
                result = acceptance.verify_public(terms, args.expected_contract_digest,
                                                  merkle.read(args.bundle), args.expected_root)
            elif args.command == 'execute':
                _, vault = acceptance.execute(terms, args.expected_contract_digest, merkle.read(args.input_vault))
                merkle.write_new(args.out, vault)
                result = {'executed': True, 'output_is_private_plaintext': True}
            else:
                if int(time.time()) >= terms['expires_at']:
                    raise merkle.Invalid('contract expired')
                audited = acceptance.audit(terms, args.expected_contract_digest,
                                           merkle.read(args.input_vault), merkle.read(args.evidence_vault))
                merkle.write_new(args.out, audited['bundle'])
                result = {'audit_scope': audited['audit_scope'], 'work_completed': audited['work_completed'],
                          'expected_evidence_root': audited['evidence_root'], 'spend_permitted': False}
        print(json.dumps(result, sort_keys=True, indent=2))
        return 0
    except Exception:
        # No private values, paths, input fragments or upstream exception strings.
        print('REFUSED: invalid input, authorization, evidence, or local operation.', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
