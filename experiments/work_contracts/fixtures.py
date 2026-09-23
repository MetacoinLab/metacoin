"""Explicitly synthetic examples and isolated zero-value fixture funding."""
from demo.test_meta_faucet import _Faucet
from demo.tasks import task_0001_lunar_link_budget as setup_task
from experiments.private_receipts import receipt as merkle
from . import acceptance, contract, energy_analysis as energy


def inputs(outcome='FEASIBLE'):
    bounds = {'FEASIBLE': [1_000_000, 1_100_000], 'INFEASIBLE': [500_000, 550_000],
              'INDETERMINATE': [650_000, 680_000]}[outcome]
    return {'available_low': bounds[0], 'available_high': bounds[1], 'reserve': 100_000,
            'segments': [{'duration': 600, 'power_low': 800, 'power_high': 1_000}],
            'units': dict(energy.UNITS), 'assumptions': list(energy.ASSUMPTIONS),
            'provenance': 'synthetic', 'private_label': 'SYNTHETIC_PRIVATE_CANARY_73'}


def agree(job, outcome='FEASIBLE', expires_at=2_000_000_000, **kwargs):
    data = inputs(outcome)
    public, vault = merkle.commit({'inputs': data})
    terms = contract.make(job, public['root'], expires_at, **kwargs)
    return terms, vault


def prepare(job, outcome='FEASIBLE', expires_at=2_000_000_000, **kwargs):
    """Test convenience; end-to-end demos register terms before execution."""
    terms, vault = agree(job, outcome, expires_at, **kwargs)
    _, evidence = acceptance.execute(terms, contract.digest(terms), vault)
    return terms, vault, evidence


def funded_faucet(actor='agent-fixture', amount=1000):
    # Fixture setup ONLY. The existing verified lunar-link task seeds isolated
    # Test-META. This is NOT a reward for the new energy analysis.
    faucet = _Faucet()
    result = setup_task.compute()
    funded = faucet.dispense(actor, {'result': result, 'claimed_output_hash': setup_task.output_hash(result)}, amount)
    if funded['dispensed'] is not True:
        raise RuntimeError('synthetic fixture setup failed')
    return faucet
