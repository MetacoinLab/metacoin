"""Bounded economic actions (agent buys next-step compute) through the existing journal.
Provider mode is part of the persisted action binding and never changes on retry."""
import json
from experiments.private_receipts import receipt as merkle
from experiments.work_contracts import fixtures
from integrations.x402.legacy_adapter import LegacyAdapter
from . import history
from .db import now
from .errors import ServiceError

_process_faucet = None


def provider_for(mode, settings, capability, actor='agent-fixture'):
    """Adapters by mode. Capabilities are declared by the adapters themselves."""
    global _process_faucet
    if mode == 'simulation':
        if capability != 'legacy_simulation':
            raise ServiceError('ADAPTER_CAPABILITY', 'contract capability does not match simulation mode')
        if _process_faucet is None:
            _process_faucet = fixtures.funded_faucet(amount=1000)   # process-scoped zero-value fixture funding
        if _process_faucet.balance_of(actor) == 0:
            # Fixture funding for this actor from the separately labeled lunar-link task; not a reward.
            from demo.tasks import task_0001_lunar_link_budget as setup_task
            result = setup_task.compute()
            _process_faucet.dispense(actor, {'result': result, 'claimed_output_hash': setup_task.output_hash(result)}, 1000)
        return LegacyAdapter(_process_faucet), {'adapter_session': 'process-scoped', 'real_funds': False}
    if mode == 'test-http':
        if capability != 'x402_loopback_test':
            raise ServiceError('ADAPTER_CAPABILITY', 'contract capability does not match test-http mode')
        from integrations.x402 import loopback_harness as lb
        from integrations.x402.loopback_adapter import LoopbackAdapter
        if not lb.available():
            raise ServiceError('CAPABILITY_UNAVAILABLE', 'x402 SDK not installed')
        return LoopbackAdapter(_test_facilitator(lb)), {'adapter_session': 'in-process SDK objects with facilitator double', 'real_funds': False}
    if mode == 'production':
        missing = ['x402[evm] signer (eth-account) not installed in the service environment',
                   'METACOIN_BUYER_RESOURCE_URL (remote compute resource to purchase) not configured',
                   'funded wallet credential not configured']
        raise ServiceError('CAPABILITY_UNAVAILABLE', 'production buyer adapter incomplete: ' + '; '.join(missing))
    raise ServiceError('VALIDATION', 'provider_mode')


_facilitator = None


def _test_facilitator(lb):
    global _facilitator
    if _facilitator is None:
        _facilitator = lb.FacilitatorDouble(lb.load(), 'eip155:84532', balance=1000)
    return _facilitator


class Actions:
    def __init__(self, settings, jobs):
        self.settings, self.jobs = settings, jobs

    def _job_for_action(self, db, principal, job_id):
        job = self.jobs.get(db, principal, job_id)
        if job['kind'] != 'energy_audit':
            raise ServiceError('CONFLICT', 'only energy-audit contracts carry an action entitlement')
        if job['review_state'] != 'accepted':
            raise ServiceError('WORK_NOT_ACCEPTED', 'signed acceptance required before any action')
        contract = db.execute('SELECT * FROM contracts WHERE id=?', (job['contract_id'],)).fetchone()
        return job, contract, merkle.parse(contract['contract_json'])

    def create(self, db, principal, job_id, request_id, provider_mode, dry_run=False):
        principal.require('action:create')
        if provider_mode is None:
            provider_mode = self.settings.provider_mode
        job, contract, doc = self._job_for_action(db, principal, job_id)
        journal = self.jobs.journal(db, principal.workspace)
        existing = db.execute('SELECT * FROM payment_actions WHERE job_id=?', (job_id,)).fetchone()
        if existing is not None and (existing['request_id'] != request_id or existing['provider_mode'] != provider_mode):
            raise ServiceError('ENTITLEMENT_CONSUMED', 'an action with another request id or provider mode already exists')
        request = journal.request(contract['id'], request_id)
        adapter, session = provider_for(provider_mode, self.settings, doc['action']['capability'], doc['action']['actor'])
        if dry_run:
            return dict(journal.preview(request, doc['action']['actor'], adapter, now()), provider_mode=provider_mode, **session)
        if existing is None:
            db.execute('INSERT INTO payment_actions VALUES (?,?,?,?,?,?,?)',
                       (request_id, principal.workspace, job_id, provider_mode, merkle.canonical(request).decode(), principal.id, now()))
            history.record(db, principal.workspace, principal.id, 'payment.reserved', 'job', job_id,
                           {'request_id': request_id, 'provider_mode': provider_mode, 'amount': request['amount'],
                            'asset': request['asset'], 'network': request['network'], 'direction': 'agent-buys-next-compute'})
        status = journal.dispatch(request, doc['action']['actor'], adapter, now())
        history.record(db, principal.workspace, principal.id, 'payment.dispatched', 'job', job_id,
                       {'request_id': request_id, 'state': status['state'], 'reference': (status['result'] or {}).get('reference')})
        return dict(status, provider_mode=provider_mode, **session, direction='agent-buys-next-compute')

    def reconcile(self, db, principal, job_id):
        principal.require('action:reconcile')
        job, contract, doc = self._job_for_action(db, principal, job_id)
        act = db.execute('SELECT * FROM payment_actions WHERE job_id=?', (job_id,)).fetchone()
        if act is None:
            raise ServiceError('NOT_FOUND', 'action')
        adapter, session = provider_for(act['provider_mode'], self.settings, doc['action']['capability'], doc['action']['actor'])
        status = self.jobs.journal(db, principal.workspace).reconcile(act['request_id'], doc['action']['actor'], adapter, now())
        history.record(db, principal.workspace, principal.id, 'payment.reconciled', 'job', job_id,
                       {'request_id': act['request_id'], 'state': status['state'], 'reconciliation': status['reconciliation']})
        return dict(status, provider_mode=act['provider_mode'], **session)

    def budget(self, db, principal):
        principal.require('budget:read')
        camp = db.execute('SELECT * FROM campaigns WHERE workspace=?', (principal.workspace,)).fetchone()
        journal = self.jobs.journal(db, principal.workspace)
        listing = journal.inspect()
        return {'campaign': camp['campaign_id'], 'asset': camp['asset'], 'network': camp['network'], 'unit': camp['unit'],
                'cap': listing['limit'], 'exposure': listing['exposure'], 'available': listing['available'],
                'by_state': listing['exposure_by_state'], 'provider_mode': self.settings.provider_mode,
                'note': 'amounts are one asset in one unit; nothing here sums unlike assets'}
