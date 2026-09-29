"""Synthetic measurement protocol; no speedup or real-world demand claim."""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
from demo.x402_spend_stub import buy_compute
from experiments.private_receipts import receipt as merkle
from integrations.x402.legacy_adapter import LegacyAdapter
from . import acceptance, contract, energy_analysis as energy, explanation, fixtures
from .execution_state import Journal
from .tests.durable_provider import DurableProvider


def summarize(values):
    return {'samples': len(values), 'median_ns': statistics.median(values),
            'p95_ns': sorted(values)[math.ceil(0.95 * len(values)) - 1],
            'min_ns': min(values), 'max_ns': max(values)}


def run(samples=30):
    if type(samples) is not int or not 30 <= samples <= 1000:
        raise ValueError('sample count must be 30..1000')
    warmup = 3
    observations = {}
    now = int(time.time())
    for outcome in energy.OUTCOMES:
        times = {key: [] for key in ('raw_energy_analysis', 'margin_explanation', 'commitment_creation',
                                     'private_audit_and_disclosure', 'public_verification',
                                     'journal_authorization_register_and_audit', 'legacy_compute_purchase_only',
                                     'contract_gated_purchase_only', 'contract_gated_purchase_durable_test_provider',
                                     'reconciliation_durable_test_provider')}
        data = fixtures.inputs(outcome)
        terms, input_vault = fixtures.agree('bench-' + outcome, outcome, expires_at=now + 86400)
        durable_terms, durable_input = fixtures.agree('bench-durable-' + outcome, outcome, expires_at=now + 86400,
                                                      capability='durable_test_simulation')
        _, evidence = acceptance.execute(terms, contract.digest(terms), input_vault)
        _, durable_evidence = acceptance.execute(durable_terms, contract.digest(durable_terms), durable_input)
        pin, durable_pin = contract.digest(terms), contract.digest(durable_terms)
        for index in range(samples + warmup):
            sample = {}
            start = time.perf_counter_ns()
            raw = energy.analyze(data)
            sample['raw_energy_analysis'] = time.perf_counter_ns() - start
            start = time.perf_counter_ns()
            explanation.explain(data)
            sample['margin_explanation'] = time.perf_counter_ns() - start
            start = time.perf_counter_ns()
            merkle.commit(acceptance.expected_evidence(terms, data))
            sample['commitment_creation'] = time.perf_counter_ns() - start
            start = time.perf_counter_ns()
            audited = acceptance.audit(terms, pin, input_vault, evidence)
            sample['private_audit_and_disclosure'] = time.perf_counter_ns() - start
            start = time.perf_counter_ns()
            acceptance.verify_public(terms, pin, audited['bundle'], audited['evidence_root'])
            sample['public_verification'] = time.perf_counter_ns() - start
            bare_faucet = fixtures.funded_faucet(amount=1)
            start = time.perf_counter_ns()
            buy_compute(bare_faucet, 'agent-fixture', 1)
            sample['legacy_compute_purchase_only'] = time.perf_counter_ns() - start
            with TemporaryDirectory(prefix='metacoin-benchmark-') as directory:
                state = Journal(Path(directory) / 'journal.sqlite', 'benchmark', 2)
                start = time.perf_counter_ns()
                state.register(terms, pin, 'local-owner', now)
                state.audit(terms['job_id'], input_vault, evidence, 'local-auditor', now)
                sample['journal_authorization_register_and_audit'] = time.perf_counter_ns() - start
                adapter = LegacyAdapter(fixtures.funded_faucet(amount=1))
                request = state.request(terms['job_id'], 'bench-request')
                start = time.perf_counter_ns()
                state.dispatch(request, 'agent-fixture', adapter, now)
                sample['contract_gated_purchase_only'] = time.perf_counter_ns() - start
                state.register(durable_terms, durable_pin, 'local-owner', now)
                state.audit(durable_terms['job_id'], durable_input, durable_evidence, 'local-auditor', now)
                provider = DurableProvider(Path(directory) / 'provider.json', initial_balance=1)
                durable_request = state.request(durable_terms['job_id'], 'bench-durable-request')
                start = time.perf_counter_ns()
                state.dispatch(durable_request, 'agent-fixture', provider, now)
                sample['contract_gated_purchase_durable_test_provider'] = time.perf_counter_ns() - start
                start = time.perf_counter_ns()
                state.reconcile('bench-durable-request', 'agent-fixture', DurableProvider(Path(directory) / 'provider.json'), now)
                sample['reconciliation_durable_test_provider'] = time.perf_counter_ns() - start
            if index >= warmup:
                for key, value in sample.items():
                    times[key].append(value)
        observations[outcome] = {
            'synthetic_input_sha256': hashlib.sha256(merkle.canonical(data)).hexdigest(),
            'input_bytes': len(merkle.canonical(data)), 'raw_result_bytes': len(merkle.canonical(raw)),
            'public_bundle_bytes': len(merkle.canonical(audited['bundle'])),
            'private_evidence_vault_bytes': len(merkle.canonical(evidence)),
            'raw_result_fields': sorted(raw), 'public_disclosed_fields': sorted(x['name'] for x in audited['bundle']['disclosures']),
            'private_evidence_fields': sorted(f['name'] for f in evidence['fields']),
            **{key: summarize(values) for key, values in times.items()}}
    try:
        head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=Path(__file__).parents[2], text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        head = 'unavailable-in-source-archive'
    return {'scope': 'synthetic-local-overhead;not-production-or-proof-performance',
            'python': sys.version.split()[0], 'platform': platform.platform(), 'machine': platform.machine(),
            'base_commit': head, 'verifier_digest': contract.verifier_digest(),
            'warmup_per_case': warmup, 'samples_per_case': samples,
            'quantile': 'nearest-rank: sorted[ceil(0.95*n)-1]',
            'randomized_test_seed': 73019, 'benchmark_inputs': 'three-fixed-analytical-cases',
            'clock': 'perf_counter_ns', 'memory_measured': False,
            'comparison_limits': [
                'Same energy inputs; raw result baseline discloses numerical margins, public bundle does not.',
                'Purchase timings exclude fixture funding, registration and private audit for both paths.',
                'Contract-gated purchase includes durable reservation and authorization checks absent from the legacy call.',
                'Schemes provide different guarantees; timings measure added overhead, not a like-for-like speedup.',
                'The durable test provider is a testing facility with file writes and fsync; it is not a payment rail.',
                'Reconciliation timing is a same-process lookup of a durable record, not a network query.',
                'Each operation is timed separately; no end-to-end figure hides setup cost.',
                'No ZK backend, real settlement, sensor validation or external participants measured.'],
            'cases': observations}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--samples', type=int, default=30)
    parser.add_argument('--out')
    args = parser.parse_args()
    result = run(args.samples)
    encoded = json.dumps(result, indent=2, sort_keys=True) + '\n'
    if args.out:
        # Timing summaries may be fractional; this is a measurement report,
        # deliberately separate from the integer-only contract wire format.
        fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as stream:
            stream.write(encoded)
    else:
        print(encoded, end='')


if __name__ == '__main__':
    main()
