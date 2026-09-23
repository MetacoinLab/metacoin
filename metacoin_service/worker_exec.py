"""Child process for one job attempt: reads a JSON spec on stdin, computes with the
allowlisted implementation for its kind, writes the result JSON to stdout.
Resource limits are applied here (CPU, address space, output size). This is a
process boundary for cancellation and limits, not a sandbox for hostile code:
only the three local implementations below can run."""
import json
import resource
import sys
from experiments.private_receipts import receipt as merkle
from experiments.work_contracts import acceptance, energy_analysis as energy, explanation
from metacoin_service import science


def run(spec):
    kind, inputs = spec['kind'], spec['inputs']
    if kind == 'energy_audit':
        doc = spec['contract']
        _, vault = acceptance.execute(doc, spec['contract_digest'], spec['input_vault'])
        values = acceptance.full_values(vault, vault['receipt']['root'])
        details = values['audit_details']
        summary = {'outcome': values['outcome'], 'reason': details['reason'],
                   'required_low': details['required_low'], 'required_high': details['required_high'],
                   'worst_margin': details['worst_margin'], 'best_margin': details['best_margin'],
                   'additional_usable_energy': details['additional_usable_energy'],
                   'dominant_uncertainty_source': values['dominant_uncertainty_source'],
                   'margin_width': values['margin_explanation']['margin_width'], 'units': details['units'],
                   'assumptions': details['assumptions']}
        return {'evidence_vault': vault, 'outcome': values['outcome'], 'summary': summary}
    if kind == 'safe_runtime':
        result = science.safe_runtime(inputs)
        outcome = result['status']
    elif kind == 'plan_comparison':
        result = science.compare_plans(inputs)
        outcome = 'SELECTED:' + result['selected_id'] if result['selected_id'] else 'NO_SELECTION'
    else:
        raise merkle.Invalid('unsupported adapter capability or destination')
    evidence = {'contract_digest': spec['contract_digest'], 'input_root': spec['input_root'],
                'verifier_id': 'service-science/v1', 'verifier_digest': science.bundle_digest(),
                'result_schema': result['result_schema'], 'model_id': result['model_id'], 'result': result,
                'scope': 'service-private-recomputation'}
    _, vault = merkle.commit(evidence)
    return {'evidence_vault': vault, 'outcome': outcome, 'summary': {k: v for k, v in result.items() if k != 'candidates'}
            | ({'candidates': [{k: c[k] for k in ('id', 'outcome', 'worst_margin', 'best_margin', 'duration', 'utility')} for c in result['candidates']]} if 'candidates' in result else {})}


def main():
    limits = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}
    resource.setrlimit(resource.RLIMIT_CPU, (limits.get('cpu', 20), limits.get('cpu', 20)))
    resource.setrlimit(resource.RLIMIT_AS, (limits.get('mem', 1 << 30), limits.get('mem', 1 << 30)))
    resource.setrlimit(resource.RLIMIT_FSIZE, (limits.get('out', 1 << 20), limits.get('out', 1 << 20)))
    raw = sys.stdin.buffer.read(limits.get('inbytes', 4 << 20) + 1)
    try:
        spec = merkle.parse(raw)
        out = run(spec)
        sys.stdout.write(json.dumps({'ok': True, **out}, separators=(',', ':')))
        return 0
    except merkle.Invalid as exc:
        sys.stdout.write(json.dumps({'ok': False, 'code': 'INPUT_INVALID', 'reason': str(exc)}))
        return 3
    except Exception as exc:                    # computation defect: retryable class, no details echoed
        sys.stdout.write(json.dumps({'ok': False, 'code': 'COMPUTATION_ERROR', 'reason': type(exc).__name__}))
        return 4


if __name__ == '__main__':
    sys.exit(main())
