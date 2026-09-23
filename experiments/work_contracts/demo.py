"""Local end-to-end demonstration, using synthetic inputs and the legacy stub."""
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import time
from experiments.private_receipts import receipt as merkle
from integrations.x402.legacy_adapter import LegacyAdapter
from . import acceptance, contract, fixtures
from .execution_state import Journal


class LostAcknowledgement(LegacyAdapter):
    """Fault injection: the in-memory spend happens, then the response is lost."""
    def submit(self, request):
        super().submit(request)
        raise ConnectionError('simulated acknowledgement loss')


def run():
    now = int(time.time())
    reports, public_samples = [], []
    with TemporaryDirectory(prefix='metacoin-work-contract-') as directory:
        state = Journal(Path(directory) / 'state.sqlite', 'synthetic-campaign', 4)
        faucet = fixtures.funded_faucet()
        adapter = LegacyAdapter(faucet)
        for index, outcome in enumerate(('FEASIBLE', 'INFEASIBLE', 'INDETERMINATE')):
            terms, inputs = fixtures.agree('job-' + str(index), outcome, expires_at=now + 3600)
            pin = contract.digest(terms)
            state.register(terms, pin, 'local-owner', now)
            _, evidence = acceptance.execute(terms, pin, inputs)
            audited = state.audit(terms['job_id'], inputs, evidence, 'local-auditor', now)
            public = acceptance.verify_public(terms, pin, audited['bundle'], audited['evidence_root'])
            request = state.request(terms['job_id'], 'request-' + str(index))
            first = state.dispatch(request, 'agent-fixture', adapter, now)
            before = faucet.balance_of('agent-fixture')
            retry = state.dispatch(request, 'agent-fixture', adapter, now)
            assert first == retry and before == faucet.balance_of('agent-fixture')
            reports.append({'outcome': public['disclosed']['outcome'], 'work_completed': audited['work_completed'],
                            'payment_state': first['state'], 'capability': adapter.capability,
                            'identical_retry_without_additional_spend': True})
            public_samples.append({'contract': terms, 'expected_contract_digest': pin,
                                   'expected_evidence_root': audited['evidence_root'], 'bundle': audited['bundle'],
                                   'trust_source': 'local-synthetic-demo-audit;not-remote-authentication'})
        forged = deepcopy(public_samples[-1]['bundle'])
        next(x for x in forged['disclosures'] if x['name'] == 'outcome')['value'] = 'FEASIBLE'
        try:
            acceptance.verify_public(terms, pin, forged, audited['evidence_root'])
        except merkle.Invalid:
            tampered_refused = True
        else:
            raise AssertionError('tampered outcome accepted')
        terms, inputs = fixtures.agree('unknown-job', expires_at=now + 3600)
        state.register(terms, contract.digest(terms), 'local-owner', now)
        _, evidence = acceptance.execute(terms, contract.digest(terms), inputs)
        state.audit('unknown-job', inputs, evidence, 'local-auditor', now)
        lost = LostAcknowledgement(faucet)
        request = state.request('unknown-job', 'unknown-request')
        unknown = state.dispatch(request, 'agent-fixture', lost, now)
        before = faucet.balance_of('agent-fixture')
        state.dispatch(request, 'agent-fixture', lost, now)
        assert before == faucet.balance_of('agent-fixture')
        # Fresh adapter lacks the previous instance's outcome cache.
        unknown = state.reconcile('unknown-request', 'agent-fixture', LegacyAdapter(faucet))
        assert unknown['state'] == 'OUTCOME_UNKNOWN' and state.exposure() == 4
        return {'mode': 'synthetic-local-demonstration', 'payment_capability': adapter.capability,
                'funding': 'separate-existing-task-fixture-not-new-analysis-reward',
                'cases': reports, 'tampered_disclosure_refused': tampered_refused,
                'lost_response': {'state': unknown['state'], 'blind_retry_prevented': True,
                                  'retained_campaign_exposure': state.exposure()},
                'external_team_pilot': 'not_performed', 'public_samples': public_samples}


if __name__ == '__main__':
    import json
    print(json.dumps(run(), sort_keys=True, indent=2))
