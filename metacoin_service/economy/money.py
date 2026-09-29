"""Money bound to accepted work (Order 08 Group D, §45–§48, §52–§55): payment intents, authorization and settlement on
the supported rails, settlement receipts, fee and verifier obligations, refunds where the rail supports a reverse
transfer, and reconciliation of uncertain observations.

Rails: `action-units` settle inside the application journal (no chain; the workspace action asset); `local-chain-token`
settles on the private py-evm chain through the pinned Permit2 / upto proxy (an authorization by the payer, a facilitator
transaction, an observed receipt). Both schemes use that mechanism there: `exact` fixes the final amount at the
authorized amount; `upto` authorizes the ceiling and settles the accepted amount (partial / diagnostic). The SDK exact
scheme against the facilitator double is NOT used for work payments (test-double evidence, not chain evidence).
Production external settlement remains unverified and is refused by the payer guard."""
import hashlib
import json
import secrets

from experiments.private_receipts import receipt as merkle
from .. import budgets, crypto, history, metering
from ..approvals import gate as approval_gate
from ..db import now
from ..errors import ServiceError
from . import journal, terms as terms_mod
from .board import _terms

ENV = {'simulation': 'synthetic-local', 'test-http': 'synthetic-local', 'production': 'production-configured'}
DEV_ACCOUNTS = {'requester_payer': 2, 'provider_a': 4, 'provider_b': 5, 'treasury': 6, 'verifier': 7, 'delegate': 8}
INTENT_STATES = ('prepared', 'authorized', 'submitted', 'settled', 'failed', 'unknown', 'expired', 'void')
AUTHORIZATION_SECONDS = 600


class Money:
    def __init__(self, settings, services, board, evidence):
        self.settings, self.svc, self.board, self.evidence = settings, services, board, evidence
        self._approved = set()

    # ---- rails -----------------------------------------------------------------------------------------------------
    def chain(self):
        if self.settings.provider_mode != 'test-http':
            return None
        try:
            return self.svc.sales.local_chain()
        except Exception:
            return None

    def rails(self, db, principal):
        principal.require('work:read')
        chain = self.chain()
        out = {'assets': terms_mod.ASSETS, 'action-units': {'rail': 'application journal', 'settlement': 'internal ledger transfer between the requester budget scope and the provider receivable; no chain, no signature'},
               'local-chain-token': {'available': chain is not None, 'rail': 'private py-evm chain (pinned Permit2 / x402 upto proxy / mock token)', 'mechanism': 'Permit2 witness authorization by the payer, settled by the local facilitator; exact = final fixed at the authorized amount, upto = final <= ceiling',
                                     'production': 'unverified: external facilitators and public networks are refused by the payer guard', 'facilitator': 'in-process local facilitator (not a public one)'}}
        if chain is not None:
            accounts = chain.w3.eth.accounts
            out['local-chain-token'].update({'network': chain.network, 'token': chain.token, 'permit2': chain.permit2, 'proxy': chain.proxy,
                                             'synthetic_accounts': {k: accounts[i] for k, i in DEV_ACCOUNTS.items()}, 'provenance': 'test setup: the requester payer was minted 10^12 units by the chain fixture; nothing here is earned protocol fees or real funds',
                                             'balances': {k: chain.token_c.functions.balanceOf(accounts[i]).call() for k, i in DEV_ACCOUNTS.items()}})
        return out

    def _key_for(self, chain, address):
        for i, acct in enumerate(chain.w3.eth.accounts):
            if acct.lower() == address.lower():
                return chain.backend.account_keys[i], i
        return None, None

    def _ensure_approval(self, chain, address):
        if address in self._approved:
            return
        chain.token_c.functions.approve(chain.permit2, 2 ** 256 - 1).transact({'from': address})
        self._approved.add(address)

    # ---- entitlements of other kinds (fee, verifier) ---------------------------------------------------------------------------
    def on_decision(self, db, award, ms, decision_id, amount, terms):
        """Called by the evidence layer after an acceptance decision: creates the fee obligation and posts the journal."""
        env = ENV[self.settings.provider_mode]; asset = terms['payment']['asset']; net = self._network(asset)
        if amount > 0:
            journal.post(db, award['workspace'], 'accept:%s:%s' % (ms['id'], decision_id), 'acceptance', asset, net, env, [('expense:work_accepted', amount, 0), ('liability:payable', 0, amount)], 'work_milestone', ms['id'], 'accepted obligation to the provider (entitlement of the milestone)')
            fee = terms_mod.fee_amount(terms['payment'], amount)
            if fee > 0:
                tr = self._treasury_address(db, award['workspace'], asset)
                ex = db.execute("SELECT * FROM work_entitlements WHERE milestone_id=? AND kind='fee'", (ms['id'],)).fetchone()
                if ex is None:
                    fid = 'wen_' + secrets.token_hex(8)
                    db.execute("INSERT INTO work_entitlements VALUES (?,?,?,?,?,?,?,?,?,?,NULL,?,?,'fee')", (fid, award['workspace'], award['id'], ms['id'], decision_id, tr, fee, asset, terms['payment']['scale'], 'payable', now(), now()))
                    history.record(db, award['workspace'], 'service', 'work.entitlement', 'work_entitlement', fid, {'kind': 'fee', 'award_id': award['id'], 'milestone': ms['key'], 'amount': fee, 'bps': terms['payment']['fee_policy']['treasury_bps'], 'rounding': 'floor; remainder to provider'})
                    journal.post(db, award['workspace'], 'fee:%s:%s' % (ms['id'], decision_id), 'fee_obligation', asset, net, env, [('expense:platform_fee', fee, 0), ('liability:payable', 0, fee)], 'work_entitlement', fid, 'treasury fee obligation bound to the frozen price policy')

    def on_verification(self, db, award, ms, verification_id, terms):
        """Verifier compensation is bound to COMPLETION of the required verification with a valid statement, never to its verdict."""
        comp = terms['payment'].get('verifier_compensation', 0)
        if comp <= 0 or not terms['payment'].get('verifier_pay_to'):
            return None
        v = db.execute('SELECT * FROM verification_jobs WHERE id=?', (verification_id,)).fetchone()
        if v is None or v['state'] not in ('passed', 'failed') or v['class'] != terms['acceptance']['required_verification']['class']:
            return None
        ex = db.execute("SELECT * FROM work_entitlements WHERE milestone_id=? AND kind='verifier'", (ms['id'],)).fetchone()
        if ex is not None:
            return ex['id']
        asset = terms['payment']['asset']; env = ENV[self.settings.provider_mode]
        vid = 'wen_' + secrets.token_hex(8)
        db.execute("INSERT INTO work_entitlements VALUES (?,?,?,?,?,?,?,?,?,?,NULL,?,?,'verifier')", (vid, award['workspace'], award['id'], ms['id'], 'verification:' + verification_id, terms['payment']['verifier_pay_to'], comp, asset, terms['payment']['scale'], 'payable', now(), now()))
        journal.post(db, award['workspace'], 'verifier:%s:%s' % (ms['id'], verification_id), 'verifier_obligation', asset, self._network(asset), env, [('expense:verifier', comp, 0), ('liability:payable', 0, comp)], 'work_entitlement', vid, 'verifier compensation for a completed verification (outcome %s); not verdict-dependent' % v['state'])
        history.record(db, award['workspace'], 'service', 'work.entitlement', 'work_entitlement', vid, {'kind': 'verifier', 'award_id': award['id'], 'milestone': ms['key'], 'amount': comp, 'verification_id': verification_id, 'verdict_independent': True})
        return vid

    def _network(self, asset):
        if asset == 'local-chain-token':
            chain = self.chain(); return chain.network if chain else 'local-chain'
        return 'application'

    def _treasury_address(self, db, workspace, asset):
        if asset == 'local-chain-token' and self.chain() is not None:
            return self.chain().w3.eth.accounts[DEV_ACCOUNTS['treasury']]
        return 'treasury:' + workspace

    # ---- payment intents (§45–§47) ---------------------------------------------------------------------------------------------
    def _ent(self, db, principal, eid):
        e = db.execute('SELECT * FROM work_entitlements WHERE id=? AND workspace=?', (eid, principal.workspace)).fetchone()
        if e is None:
            raise ServiceError('NOT_FOUND', 'entitlement')
        return e

    def _bindings(self, db, e, award, terms, body):
        """Revalidate every binding before any authorization (§45); refusals happen before signing."""
        if e['state'] not in ('payable', 'authorized', 'unknown'):
            raise ServiceError('CONFLICT', {'code': 'entitlement_not_payable', 'state': e['state']})
        ms = db.execute('SELECT * FROM work_milestones WHERE id=?', (e['milestone_id'],)).fetchone()
        if e['kind'] == 'provider':
            if ms['state'] != 'accepted' or ms['decision_id'] != e['decision_id']:
                raise ServiceError('CONFLICT', {'code': 'missing_or_stale_acceptance', 'milestone_state': ms['state'], 'note': 'payment is conditional on the current acceptance decision'})
            cur = db.execute('SELECT superseded_by, decision FROM work_decisions WHERE id=?', (e['decision_id'],)).fetchone()
            if cur is None or cur['superseded_by'] is not None or cur['decision'] != 'accepted':
                raise ServiceError('CONFLICT', {'code': 'stale_decision'})
        t = db.execute('SELECT digest, state FROM work_terms WHERE id=?', (award['terms_id'],)).fetchone()
        if t['digest'] != award['terms_digest'] or t['state'] != 'frozen':
            raise ServiceError('CONFLICT', {'code': 'stale_contract_revision', 'terms_state': t['state'], 'note': 'the award is bound to a revision that is no longer current; payment needs the current agreement'})
        if body.get('recipient') and body['recipient'] != e['recipient']:
            raise ServiceError('FORBIDDEN', {'code': 'recipient_substitution', 'note': 'the recipient is bound by the award and the entitlement; a description or plan cannot replace it'})
        if body.get('asset') and body['asset'] != e['asset']:
            raise ServiceError('CONFLICT', {'code': 'asset_mismatch', 'bound': e['asset']})
        net = self._network(e['asset'])
        if body.get('network') and body['network'] != net:
            raise ServiceError('CONFLICT', {'code': 'wrong_network', 'bound': net})
        if body.get('amount') is not None and body['amount'] > e['amount']:
            raise ServiceError('CONFLICT', {'code': 'amount_above_entitlement', 'entitlement': e['amount']})
        if e['amount'] > terms['payment']['ceiling']:
            raise ServiceError('INTERNAL_DEFECT', 'entitlement above the contract ceiling')
        prow = db.execute('SELECT pay_to FROM providers WHERE id=?', (award['provider_id'],)).fetchone()
        if e['kind'] == 'provider' and e['recipient'] != award['pay_to']:
            raise ServiceError('CONFLICT', {'code': 'recipient_not_bound_to_award'})
        return ms, net

    def prepare(self, db, principal, eid, body=None):
        principal.require('work:pay')
        body = body or {}
        e = self._ent(db, principal, eid)
        award = db.execute('SELECT * FROM work_awards WHERE id=?', (e['award_id'],)).fetchone()
        if principal.id != award['awarded_by']:
            raise ServiceError('FORBIDDEN', 'the requester (payer authority) prepares payment')
        t, terms = _terms(db, award['terms_id'])
        ms, net = self._bindings(db, e, award, terms, body)
        existing = db.execute("SELECT * FROM payment_intents WHERE entitlement_id=? AND kind='payment' AND state NOT IN ('failed','void','expired')", (eid,)).fetchone()
        if existing is not None:
            return dict(self.intent_view(db, principal, existing['id']), replayed=True)
        approval_gate(self.svc.approvals, db, principal, 'work_pay')
        from ..agents import grant_of, guard
        gid = grant_of(principal)
        if gid:
            guard(db, principal, 'action:create', amount=e['amount'])
        payer_authority = 'requester' if terms['payment'].get('funding', 'requester') == 'requester' else 'treasury'
        payer = self._payer_address(db, principal.workspace, e['asset'], payer_authority)
        scheme = terms['payment']['scheme']
        # exact: the authorized amount is the fixed accepted amount; upto: the authorization covers the milestone ceiling and
        # settlement names the accepted (possibly partial / diagnostic) amount; the difference was never owed
        max_amount = e['amount'] if (scheme == 'exact' or e['kind'] != 'provider') else max(e['amount'], ms['max_payment'])
        prior = db.execute("SELECT COUNT(*) FROM payment_intents WHERE entitlement_id=? AND kind='payment'", (eid,)).fetchone()[0]
        identifier = ('work-' + eid.replace('wen_', '') + ('' if prior == 0 else '-r%d' % prior))[:64]   # stable per intent: retries and restarts reuse it; a renewal after a RESOLVED expiry/failure is a new identity
        iid = 'pi_' + secrets.token_hex(8)
        db.execute("INSERT INTO payment_intents (id, workspace, kind, entitlement_id, original_intent_id, payer, payer_authority, recipient, asset, network, scheme, rail, identifier, max_amount, final_amount, terms_digest, decision_id, state, valid_until, grant_id, created_by, created_at, updated_at) VALUES (?,?,?,?,NULL,?,?,?,?,?,?,?,?,?,NULL,?,?,?,NULL,?,?,?,?)",
                   (iid, principal.workspace, 'payment', eid, payer, payer_authority, e['recipient'], e['asset'], net, scheme, 'local-chain' if e['asset'] == 'local-chain-token' else 'application-journal', identifier, max_amount, award['terms_digest'], e['decision_id'], 'prepared', gid, principal.id, now(), now()))
        db.execute("UPDATE work_entitlements SET payment_intent_id=?, updated_at=? WHERE id=?", (iid, now(), eid))
        history.record(db, principal.workspace, principal.id, 'work.intent', 'payment_intent', iid, {'entitlement_id': eid, 'kind': e['kind'], 'scheme': scheme, 'max_amount': max_amount, 'asset': e['asset'], 'network': net, 'payer_authority': payer_authority, 'grant_id': gid, 'state': 'prepared'})
        return self.intent_view(db, principal, iid)

    def _payer_address(self, db, workspace, asset, authority):
        chain = self.chain()
        if asset == 'local-chain-token':
            if chain is None:
                raise ServiceError('CAPABILITY_UNAVAILABLE', {'code': 'payer_not_configured', 'note': 'local-chain settlement needs test-http mode with the built chain artifacts; production payers are never configured here'})
            return chain.w3.eth.accounts[DEV_ACCOUNTS['treasury'] if authority == 'treasury' else DEV_ACCOUNTS['requester_payer']]
        return ('treasury:' if authority == 'treasury' else 'requester:') + workspace

    def intent_row(self, db, principal, iid):
        i = db.execute('SELECT * FROM payment_intents WHERE id=? AND workspace=?', (iid, principal.workspace)).fetchone()
        if i is None:
            raise ServiceError('NOT_FOUND', 'payment intent')
        return i

    def intent_view(self, db, principal, iid):
        principal.require('work:read')
        i = self.intent_row(db, principal, iid)
        award = db.execute('SELECT awarded_by, provider_id FROM work_awards WHERE id=(SELECT award_id FROM work_entitlements WHERE id=?)', (i['entitlement_id'],)).fetchone()
        prow = db.execute('SELECT principal_id FROM providers WHERE id=?', (award['provider_id'],)).fetchone()
        party = principal.id in (award['awarded_by'], prow['principal_id']) or principal.can('work:pay')
        out = {'id': iid, 'kind': i['kind'], 'entitlement_id': i['entitlement_id'], 'original_intent_id': i['original_intent_id'], 'payer_authority': i['payer_authority'], 'asset': i['asset'], 'network': i['network'], 'scheme': i['scheme'], 'rail': i['rail'],
               'identifier': i['identifier'], 'max_amount': i['max_amount'], 'final_amount': i['final_amount'], 'terms_digest': i['terms_digest'], 'decision_id': i['decision_id'], 'state': i['state'], 'valid_until': i['valid_until'],
               'transaction_ref': i['transaction_ref'], 'submissions': i['submissions'], 'error': i['error'], 'grant_id': i['grant_id'], 'created_at': i['created_at'], 'updated_at': i['updated_at'],
               'observations': json.loads(i['observations_json']) if party else len(json.loads(i['observations_json'])),
               'payer': i['payer'] if party else None, 'recipient': i['recipient'] if party else None,
               'meaning': {'prepared': 'bindings validated; nothing signed', 'authorized': 'a signed authorization exists (private); nothing has moved', 'submitted': 'settlement sent; receipt not yet observed (exposure)', 'settled': 'transfer observed on the rail', 'failed': 'settlement refused; nothing moved',
                           'unknown': 'response lost after submission; reconcile before any retry', 'expired': 'authorization validity passed before settlement; may still have been exercised — reconcile', 'void': 'entitlement voided before payment'}[i['state']],
               'enforcement': 'conditional on acceptance inside the application; the rail itself is not trustless escrow'}
        return out

    def authorize(self, db, principal, iid):
        """Produce the signed authorization from the intent's bindings (server-side payer for the synthetic rails); production is refused."""
        principal.require('work:pay')
        i = self.intent_row(db, principal, iid)
        if i['state'] == 'authorized':
            return dict(self.intent_view(db, principal, iid), replayed=True)
        if i['state'] != 'prepared':
            raise ServiceError('CONFLICT', {'code': 'intent_state', 'state': i['state']})
        e = self._ent(db, principal, i['entitlement_id']); award = db.execute('SELECT * FROM work_awards WHERE id=?', (e['award_id'],)).fetchone(); t, terms = _terms(db, award['terms_id'])
        if i['kind'] == 'payment':
            self._bindings(db, e, award, terms, {})                     # revalidated at signing time (§45)
        if i['rail'] == 'application-journal':
            db.execute("UPDATE payment_intents SET state='authorized', valid_until=?, authorization_json=?, updated_at=? WHERE id=?", (now() + AUTHORIZATION_SECONDS, json.dumps({'rail': 'application-journal', 'note': 'no signature: internal ledger authorization bound to the intent id'}), now(), iid))
            db.execute("UPDATE work_entitlements SET state='authorized', updated_at=? WHERE id=?", (now(), e['id']))
            return self.intent_view(db, principal, iid)
        if self.settings.provider_mode == 'production':
            raise ServiceError('CAPABILITY_UNAVAILABLE', {'code': 'production_guard', 'note': 'live spendable authorizations are refused by this development order; production settlement is unverified'})
        chain = self.chain()
        key, idx = self._key_for(chain, i['payer'])
        if key is None:
            raise ServiceError('CAPABILITY_UNAVAILABLE', {'code': 'payer_key_unavailable', 'payer': i['payer']})
        self._ensure_approval(chain, i['payer'])
        from integrations.x402.local_chain import harness
        from x402.extensions import payment_identifier as pi
        binding = {'entitlement_id': e['id'], 'terms_digest': i['terms_digest'], 'decision_id': i['decision_id'] or '', 'intent_id': iid, 'scheme': i['scheme'], 'resource_version': 'work-entitlement/v1'}
        server = harness.UptoServer(chain, self.svc.sales._chain_facilitator, i['max_amount'], binding, path='/work/entitlements/' + e['id'], pay_to=i['recipient'])
        core, hclient = harness.client_for(chain, key)
        ctx, first = server.handle()
        required = hclient.get_payment_required_response(lambda name: first.response.headers.get(name), json.dumps(first.response.body).encode())
        extensions = dict(required.extensions or {}); pi.append_payment_identifier_to_extensions(extensions, i['identifier'])
        payload = core.create_payment_payload(required, extensions=extensions)
        headers = hclient.encode_payment_signature_header(payload)
        ctx2, second = server.handle(headers)
        if second.type != 'payment-verified':
            db.execute("UPDATE payment_intents SET state='failed', error=?, updated_at=? WHERE id=?", ('authorization refused by the resource server: ' + str(second.type), now(), iid))
            raise ServiceError('CONFLICT', {'code': 'authorization_refused', 'stage': second.type})
        from .ops import fault
        fault(db, self.settings, 'payment_signing')
        genesis = chain.w3.eth.get_block(0)['hash'].hex()
        auth_rec = {'chain_genesis': genesis, 'headers': headers, 'binding': binding, 'path': '/work/entitlements/' + e['id'], 'nonce': payload.payload['permit2Authorization']['nonce'], 'permitted_amount': payload.payload['permit2Authorization']['permitted']['amount'],
                    'deadline': payload.payload['permit2Authorization'].get('deadline'), 'payload_sha256': hashlib.sha256(payload.model_dump_json(by_alias=True).encode()).hexdigest(), 'signer': i['payer'], 'mechanism': 'permit2 witness (x402 upto proxy)'}
        db.execute("UPDATE payment_intents SET state='authorized', authorization_json=?, requirements_json=?, requirements_digest=?, valid_until=?, updated_at=? WHERE id=?",
                   (json.dumps(auth_rec), second.payment_requirements.model_dump_json(by_alias=True), hashlib.sha256(second.payment_requirements.model_dump_json(by_alias=True).encode()).hexdigest(), int(auth_rec['deadline']) if auth_rec.get('deadline') else now() + 300, now(), iid))
        db.execute("UPDATE work_entitlements SET state='authorized', updated_at=? WHERE id=?", (now(), e['id']))
        history.record(db, principal.workspace, principal.id, 'work.intent', 'payment_intent', iid, {'state': 'authorized', 'nonce': auth_rec['nonce'], 'permitted_amount': auth_rec['permitted_amount'], 'valid_until': auth_rec.get('deadline')})
        return self.intent_view(db, principal, iid)

    def submit(self, db, principal, iid, body=None):
        """Settle: the final amount (<= authorized) is presented through requirements.amount; the outcome is observed and journaled.
        The intent is marked SUBMITTED before the rail is touched so a lost response leaves exposure, never a second spend."""
        principal.require('work:pay')
        body = body or {}
        i = self.intent_row(db, principal, iid)
        if i['state'] in ('settled',):
            return dict(self.intent_view(db, principal, iid), replayed=True)
        if i['state'] in ('submitted', 'unknown'):
            raise ServiceError('CONFLICT', {'code': 'reconcile_first', 'state': i['state'], 'note': 'a submission is outstanding; reconcile by identifier before any retry'})
        if i['state'] != 'authorized':
            raise ServiceError('CONFLICT', {'code': 'intent_state', 'state': i['state']})
        e = self._ent(db, principal, i['entitlement_id']); award = db.execute('SELECT * FROM work_awards WHERE id=?', (e['award_id'],)).fetchone(); t, terms = _terms(db, award['terms_id'])
        if i['kind'] == 'payment':
            self._bindings(db, e, award, terms, {})
        final = i['max_amount'] if i['scheme'] == 'exact' else min(e['amount'], i['max_amount'])
        if body.get('final_amount') is not None:
            if i['scheme'] == 'exact' and body['final_amount'] != i['max_amount']:
                raise ServiceError('CONFLICT', {'code': 'exact_amount_fixed', 'amount': i['max_amount']})
            if body['final_amount'] > i['max_amount'] or body['final_amount'] > e['amount']:
                raise ServiceError('CONFLICT', {'code': 'final_above_authorized', 'authorized': i['max_amount'], 'entitlement': e['amount']})
            final = body['final_amount']
        if i['valid_until'] and now() > i['valid_until'] and i['rail'] == 'local-chain':
            db.execute("UPDATE payment_intents SET state='expired', updated_at=? WHERE id=?", (now(), iid))
            history.record(db, principal.workspace, principal.id, 'work.intent', 'payment_intent', iid, {'state': 'expired', 'submitted_before': i['submissions']})
            db.execute('COMMIT'); db.execute('BEGIN IMMEDIATE')                                  # the expiry is a durable fact even though this request is refused
            raise ServiceError('EXPIRED', {'code': 'authorization_expired', 'note': 'expiry alone does not prove nothing was paid; reconcile, then reauthorize only after the old state is resolved'})
        env = ENV[self.settings.provider_mode]
        journal.post(db, principal.workspace, 'submit:%s:%d' % (iid, i['submissions'] + 1), 'submission', i['asset'], i['network'], env, [('liability:payable', final, 0), ('exposure:pending', 0, final)], 'payment_intent', iid, 'settlement submitted (attempt %d); exposure until observed' % (i['submissions'] + 1))
        db.execute("UPDATE payment_intents SET state='submitted', final_amount=?, submissions=submissions+1, updated_at=? WHERE id=?", (final, now(), iid))
        db.execute("UPDATE work_entitlements SET state='submitted', updated_at=? WHERE id=?", (now(), e['id']))
        db.execute('COMMIT'); db.execute('BEGIN IMMEDIATE')                                  # durable before the rail is touched
        from .ops import fault
        fault(db, self.settings, 'payment_submission')
        if i['rail'] == 'application-journal':
            obs = {'source': 'application journal', 'at': now(), 'amount': final, 'reference': 'journal:' + iid, 'confirmation': 'internal ledger transfer; final by construction'}
            return self._observe_settled(db, principal, iid, e, award, terms, final, 'journal:' + iid, obs)
        chain = self.chain(); auth = json.loads(i['authorization_json'])
        from integrations.x402.local_chain import harness
        server = harness.UptoServer(chain, self.svc.sales._chain_facilitator, i['max_amount'], auth['binding'], path=auth['path'], pay_to=i['recipient'])
        try:
            ctx2, second = server.handle(auth['headers'])
            if second.type != 'payment-verified':
                db.execute("UPDATE payment_intents SET state='failed', error=?, updated_at=? WHERE id=?", ('re-verification refused before settlement: ' + str(second.type), now(), iid))
                journal.post(db, principal.workspace, 'fail:%s:%d' % (iid, i['submissions'] + 1), 'settlement_failed', i['asset'], i['network'], env, [('exposure:pending', final, 0), ('liability:payable', 0, final)], 'payment_intent', iid, 'settlement refused before the transfer; obligation returns to payable')
                db.execute("UPDATE work_entitlements SET state='payable', updated_at=? WHERE id=?", (now(), e['id']))
                return self.intent_view(db, principal, iid)
            settled = server.settle(ctx2, second, final)
            if body.get('_simulate_lost_response') and self.settings.limits.get('test_hooks'):
                raise TimeoutError('FAULT INJECTED (test hook, disposable instance): settlement executed on the rail, response dropped before observation')
        except Exception as exc:
            db.execute("UPDATE payment_intents SET state='unknown', error=?, updated_at=? WHERE id=?", ('settlement outcome unknown: ' + type(exc).__name__, now(), iid))
            db.execute("UPDATE work_entitlements SET state='exposed', updated_at=? WHERE id=?", (now(), e['id']))
            history.record(db, principal.workspace, principal.id, 'work.payment', 'payment_intent', iid, {'state': 'unknown', 'reason': type(exc).__name__})
            return dict(self.intent_view(db, principal, iid), note='exposure retained; reconcile by identifier/nonce before any retry')
        try:
            fault(db, self.settings, 'payment_observation')
        except RuntimeError as exc:
            db.execute("UPDATE payment_intents SET state='unknown', error=?, updated_at=? WHERE id=?", ('settlement outcome unknown: ' + str(exc)[:80], now(), iid))
            db.execute("UPDATE work_entitlements SET state='exposed', updated_at=? WHERE id=?", (now(), e['id']))
            return dict(self.intent_view(db, principal, iid), note='exposure retained; reconcile by identifier/nonce before any retry')
        resp = settled.settle_response.model_dump(by_alias=True, exclude_none=True) if settled.settle_response else {}
        if not settled.success:
            state = 'unknown' if settled.error_reason == 'settlement_pending' else 'failed'
            db.execute("UPDATE payment_intents SET state=?, error=?, updated_at=? WHERE id=?", (state, settled.error_reason, now(), iid))
            if state == 'failed':
                journal.post(db, principal.workspace, 'fail:%s:%d' % (iid, i['submissions'] + 1), 'settlement_failed', i['asset'], i['network'], env, [('exposure:pending', final, 0), ('liability:payable', 0, final)], 'payment_intent', iid, 'settlement refused: ' + str(settled.error_reason))
                db.execute("UPDATE work_entitlements SET state='payable', updated_at=? WHERE id=?", (now(), e['id']))
            else:
                db.execute("UPDATE work_entitlements SET state='exposed', updated_at=? WHERE id=?", (now(), e['id']))
            return self.intent_view(db, principal, iid)
        obs = {'source': 'local facilitator settle response', 'at': now(), 'transaction': settled.transaction, 'amount': resp.get('amount'), 'network': resp.get('network'), 'payer': resp.get('payer')}
        try:
            rc = chain.w3.eth.get_transaction_receipt(settled.transaction)
            obs['chain_receipt'] = {'status': rc['status'], 'block': rc['blockNumber'], 'gas_used': rc['gasUsed'], 'source': 'private py-evm chain receipt (local validation; no public finality)'}
        except Exception as exc:
            obs['chain_receipt'] = {'error': type(exc).__name__}
        if resp.get('amount') not in (None, str(final)):
            db.execute("UPDATE payment_intents SET state='unknown', error=?, updated_at=? WHERE id=?", ('settlement answer names a different amount: ' + str(resp.get('amount')), now(), iid))
            return self.intent_view(db, principal, iid)
        return self._observe_settled(db, principal, iid, e, award, terms, final, settled.transaction, obs)

    def _observe_settled(self, db, principal, iid, e, award, terms, final, txref, obs):
        i = self.intent_row(db, principal, iid)
        env = ENV[self.settings.provider_mode]
        observations = json.loads(i['observations_json']); observations.append(obs)
        db.execute("UPDATE payment_intents SET state='settled', transaction_ref=?, final_amount=?, observations_json=?, updated_at=? WHERE id=?", (txref, final, json.dumps(observations), now(), iid))
        db.execute("UPDATE work_entitlements SET state='paid', updated_at=? WHERE id=?", (now(), e['id']))
        # discharge: exposure -> payer cash; unused authorization headroom returns to payable then is released with the award
        posts = [('exposure:pending', final, 0), ('asset:payer_cash', 0, final)]
        journal.post(db, principal.workspace, 'settle:%s' % iid, 'settlement', i['asset'], i['network'], env, posts, 'payment_intent', iid, 'transfer observed on the rail (%s)' % i['rail'])
        if e['kind'] == 'fee':
            from .ops import fault
            fault(db, self.settings, 'fee_credit')
            journal.post(db, principal.workspace, 'fee-credit:%s' % iid, 'fee_credit', i['asset'], i['network'], env, [('treasury:cash', final, 0), ('treasury:revenue', 0, final)], 'payment_intent', iid, 'platform fee observed as settled to the treasury address; credited once per settlement event')
        if e['amount'] > final:
            journal.post(db, principal.workspace, 'headroom:%s' % iid, 'unused_authorization', i['asset'], i['network'], env, [('liability:payable', e['amount'] - final, 0), ('expense:work_accepted', 0, e['amount'] - final)], 'work_entitlement', e['id'], 'metered: the accepted amount below the ceiling was settled; the difference was never owed')
        ms = db.execute('SELECT * FROM work_milestones WHERE id=?', (e['milestone_id'],)).fetchone()
        claims = {'entitlement_id': e['id'], 'kind': e['kind'], 'intent_id': iid, 'identifier': i['identifier'], 'rail': i['rail'], 'scheme': i['scheme'], 'asset': i['asset'], 'network': i['network'], 'authorized_max': i['max_amount'], 'final_amount': final, 'transaction': txref,
                  'observation': obs, 'recipient': i['recipient'], 'payer_authority': i['payer_authority'], 'finality': 'private local chain: observed receipt; no public confirmation policy applies' if i['rail'] == 'local-chain' else 'application ledger'}
        self.evidence._receipt(db, 'settlement', award, ms, None, iid, claims, 'rail:' + i['rail'])
        history.record(db, principal.workspace, principal.id, 'work.payment', 'payment_intent', iid, {'state': 'settled', 'final_amount': final, 'transaction': txref, 'entitlement_id': e['id'], 'kind': e['kind']})
        if i['payer_authority'] == 'treasury':
            journal.post(db, principal.workspace, 'treasury-spend:%s' % iid, 'treasury_spend', i['asset'], i['network'], env, [('treasury:spent', final, 0), ('treasury:cash', 0, final)], 'payment_intent', iid, 'treasury-funded award settled')
        self.close_award_if_done(db, award['id'])
        return self.intent_view(db, principal, iid)

    def reconcile(self, db, principal, iid):
        """Query the configured rail by the intent's exact identity (transaction hash, else the Permit2 nonce) and record the observation."""
        principal.require('work:pay')
        i = self.intent_row(db, principal, iid)
        if i['state'] in ('settled', 'failed', 'void', 'prepared'):
            return dict(self.intent_view(db, principal, iid), reconciliation='terminal-or-unsubmitted; nothing to query')
        if i['state'] == 'authorized' and i['submissions'] == 0 and not (i['valid_until'] and now() > i['valid_until']):
            return dict(self.intent_view(db, principal, iid), reconciliation='authorized, never submitted, still valid; nothing to query')
        e = self._ent(db, principal, i['entitlement_id']); award = db.execute('SELECT * FROM work_awards WHERE id=?', (e['award_id'],)).fetchone(); t, terms = _terms(db, award['terms_id'])
        if i['rail'] != 'local-chain':
            return dict(self.intent_view(db, principal, iid), reconciliation='application journal intents settle synchronously; state retained')
        chain = self.chain(); auth = json.loads(i['authorization_json'] or '{}'); env = ENV[self.settings.provider_mode]
        obs = {'source': 'chain query', 'at': now(), 'method': None}
        genesis = chain.w3.eth.get_block(0)['hash'].hex()
        if auth.get('chain_genesis') and auth['chain_genesis'] != genesis:
            observations = json.loads(i['observations_json']); observations.append(dict(obs, method='chain_identity', authorized_on=auth['chain_genesis'], current=genesis, result='rail state unavailable'))
            db.execute('UPDATE payment_intents SET observations_json=?, error=?, updated_at=? WHERE id=?', (json.dumps(observations), 'private in-memory chain restarted since authorization: the observation cannot be made; exposure retained', now(), iid))
            return dict(self.intent_view(db, principal, iid), reconciliation='rail identity changed (in-memory private chain restarted with the API process): no observation possible; exposure retained as ' + i['state'] + '; a persistent rail would answer this query')
        final = i['final_amount'] or i['max_amount']
        if i['transaction_ref']:
            try:
                rc = chain.w3.eth.get_transaction_receipt(i['transaction_ref']); obs.update(method='receipt_by_hash', status=rc['status'], block=rc['blockNumber'])
                if rc['status'] == 1:
                    return self._observe_settled(db, principal, iid, e, award, terms, final, i['transaction_ref'], obs)
            except Exception as exc:
                obs.update(error=type(exc).__name__)
        nonce = auth.get('nonce')
        if nonce is not None:
            p2 = chain.w3.eth.contract(address=chain.permit2, abi=chain.art['contracts']['Permit2']['abi'])
            word, bit = int(nonce) >> 8, int(nonce) & 0xff
            bitmap = p2.functions.nonceBitmap(chain.w3.to_checksum_address(i['payer']), word).call()
            used = bool((bitmap >> bit) & 1)
            obs.update(method='permit2_nonce_bitmap', nonce=str(nonce), used=used)
            if used:
                # the authorization was exercised: locate the transfer by scanning recent blocks for the token transfer to the recipient
                txref = None
                latest = chain.w3.eth.block_number
                for bn in range(latest, max(-1, latest - 200), -1):
                    blk = chain.w3.eth.get_block(bn, full_transactions=True)
                    for tx in blk['transactions']:
                        if tx['to'] and tx['to'].lower() == chain.proxy.lower():
                            txref = tx['hash'].hex() if hasattr(tx['hash'], 'hex') else str(tx['hash']); break
                    if txref:
                        break
                obs['transaction_located'] = txref
                return self._observe_settled(db, principal, iid, e, award, terms, final, txref or ('nonce:' + str(nonce)), obs)
            observations = json.loads(i['observations_json']); observations.append(obs)
            exposed = i['state'] in ('submitted', 'unknown')            # only a SUBMITTED intent carries exposure in the journal
            if i['valid_until'] and now() > i['valid_until'] or i['state'] == 'expired':
                db.execute("UPDATE payment_intents SET state='expired', observations_json=?, updated_at=? WHERE id=?", (json.dumps(observations), now(), iid))
                if exposed:
                    journal.post(db, principal.workspace, 'expire:%s:%d' % (iid, i['submissions']), 'authorization_expired_unused', i['asset'], i['network'], env, [('exposure:pending', final, 0), ('liability:payable', 0, final)], 'payment_intent', iid, 'authorization expired without being exercised (nonce unused); obligation returns to payable')
                db.execute("UPDATE work_entitlements SET state='payable', updated_at=? WHERE id=?", (now(), e['id']))
                return dict(self.intent_view(db, principal, iid), reconciliation='nonce unused and authorization expired: needs renewal (prepare a new intent)')
            # not exercised, still valid: the submission can be retried under the SAME authorization and identifier
            db.execute("UPDATE payment_intents SET state='authorized', observations_json=?, updated_at=? WHERE id=?", (json.dumps(observations), now(), iid))
            if exposed:
                journal.post(db, principal.workspace, 'unsubmitted:%s:%d' % (iid, i['submissions']), 'submission_not_exercised', i['asset'], i['network'], env, [('exposure:pending', final, 0), ('liability:payable', 0, final)], 'payment_intent', iid, 'nonce unused: the lost submission never executed; same authorization may be retried')
            db.execute("UPDATE work_entitlements SET state='authorized', updated_at=? WHERE id=?", (now(), e['id']))
            history.record(db, principal.workspace, principal.id, 'work.reconciled', 'payment_intent', iid, {'nonce_used': False, 'state': 'authorized'})
            return dict(self.intent_view(db, principal, iid), reconciliation='nonce unused: retry allowed under the same authorization')
        return dict(self.intent_view(db, principal, iid), reconciliation='no identity to query; exposure retained')

    def close_award_if_done(self, db, aid):
        """When every milestone is terminal and every entitlement paid/void, commit the paid amounts and release the rest of the reservation."""
        ms = db.execute('SELECT state FROM work_milestones WHERE award_id=?', (aid,)).fetchall()
        if not ms or not all(m['state'] in ('accepted', 'rejected', 'cancelled', 'superseded') for m in ms):
            return False
        ents = db.execute('SELECT * FROM work_entitlements WHERE award_id=?', (aid,)).fetchall()
        if any(e['state'] in ('payable', 'authorized', 'submitted', 'exposed', 'held', 'unknown', 'refund_pending') for e in ents):
            return False
        award = db.execute('SELECT * FROM work_awards WHERE id=?', (aid,)).fetchone()
        if award['state'] == 'closed':
            return True
        paid = sum(e['amount'] for e in ents if e['state'] in ('paid', 'refunded'))
        if award['budget_node_id']:
            budgets.release_partial(db, 'work_award', aid, paid)
        else:
            self.svc.economy.treasury.release(db, award['workspace'], aid, paid)
        db.execute("UPDATE work_awards SET state='closed', closed_at=?, close_reason=?, reserved=?, updated_at=? WHERE id=?", (now(), 'all milestones decided and obligations settled', paid, now(), aid))
        history.record(db, award['workspace'], 'service', 'work.award_closed', 'work_award', aid, {'paid': paid, 'released': award['ceiling'] - paid})
        return True

    def close_award(self, db, principal, aid):
        principal.require('work:award')
        award = self.board.award_row(db, principal, aid)
        if not self.close_award_if_done(db, aid):
            raise ServiceError('CONFLICT', {'code': 'award_not_closable', 'note': 'open milestones or unsettled obligations remain'})
        return self.board.award_view(db, principal, self.board.award_row(db, principal, aid))

    # ---- refunds (§53) ----------------------------------------------------------------------------------------------------------
    def refund(self, db, principal, eid, body):
        """Reverse transfer on the local chain under a preauthorized local recovery arrangement (the synthetic provider account
        signs); bound to the original settled intent; total refunds never exceed the refundable amount; duplicates refused."""
        principal.require('work:pay')
        body = body or {}
        e = self._ent(db, principal, eid)
        orig = db.execute("SELECT * FROM payment_intents WHERE entitlement_id=? AND kind='payment' AND state='settled'", (eid,)).fetchone()
        if orig is None:
            raise ServiceError('CONFLICT', {'code': 'nothing_settled', 'note': 'a refund reverses an observed settlement; a released reservation is not a refund'})
        amount = body.get('amount')
        terms_mod._int(amount, 1, 10 ** 15, 'refund_amount')
        prior = db.execute("SELECT COALESCE(SUM(final_amount),0) FROM payment_intents WHERE original_intent_id=? AND kind='refund' AND state IN ('settled','submitted','unknown')", (orig['id'],)).fetchone()[0] or 0
        if prior + amount > orig['final_amount']:
            raise ServiceError('CONFLICT', {'code': 'refund_exceeds_refundable', 'refundable': orig['final_amount'] - prior, 'requested': amount})
        if body.get('request_key'):
            dup = db.execute("SELECT id FROM payment_intents WHERE original_intent_id=? AND kind='refund' AND identifier=?", (orig['id'], 'refund-' + hashlib.sha256((orig['id'] + ':' + body['request_key']).encode()).hexdigest()[:32])).fetchone()
            if dup:
                return dict(self.intent_view(db, principal, dup['id']), replayed=True)
        if orig['rail'] != 'local-chain':
            raise ServiceError('CAPABILITY_UNAVAILABLE', {'code': 'refund_unsupported_on_rail', 'rail': orig['rail'], 'note': 'no reverse transfer on this rail; represent the claim as an application credit or a pending obligation instead'})
        if not body.get('provider_preauthorized'):
            raise ServiceError('FORBIDDEN', {'code': 'reverse_payment_authority', 'note': 'a requester cannot withdraw provider funds; the provider\'s preauthorized local recovery arrangement must be invoked explicitly (custody assumption recorded)'})
        chain = self.chain()
        key, idx = self._key_for(chain, orig['recipient'])
        if key is None:
            raise ServiceError('CAPABILITY_UNAVAILABLE', {'code': 'recovery_key_unavailable', 'note': 'the recipient is not a synthetic account with a preauthorized recovery key'})
        rid = 'pi_' + secrets.token_hex(8); ident = 'refund-' + hashlib.sha256((orig['id'] + ':' + (body.get('request_key') or rid)).encode()).hexdigest()[:32]   # stable per request key (SDK ids: 16-128 chars)
        env = ENV[self.settings.provider_mode]
        db.execute("INSERT INTO payment_intents (id, workspace, kind, entitlement_id, original_intent_id, payer, payer_authority, recipient, asset, network, scheme, rail, identifier, max_amount, final_amount, terms_digest, decision_id, state, created_by, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (rid, principal.workspace, 'refund', eid, orig['id'], orig['recipient'], 'provider_preauthorized_recovery', orig['payer'], orig['asset'], orig['network'], 'exact', 'local-chain', ident, amount, amount, orig['terms_digest'], orig['decision_id'], 'prepared', principal.id, now(), now()))
        journal.post(db, principal.workspace, 'refund-claim:%s' % rid, 'refund_claim', orig['asset'], orig['network'], env, [('asset:refund_receivable', amount, 0), ('liability:refund_claim', 0, amount)], 'payment_intent', rid, 'repayment obligation until the reverse transfer is observed')
        self._ensure_approval(chain, orig['recipient'])
        from integrations.x402.local_chain import harness
        from x402.extensions import payment_identifier as pi
        binding = {'refund_of': orig['id'], 'entitlement_id': eid, 'intent_id': rid, 'resource_version': 'work-refund/v1'}
        server = harness.UptoServer(chain, self.svc.sales._chain_facilitator, amount, binding, path='/work/refunds/' + rid, pay_to=orig['payer'])
        core, hclient = harness.client_for(chain, key)
        ctx, first = server.handle()
        required = hclient.get_payment_required_response(lambda name: first.response.headers.get(name), json.dumps(first.response.body).encode())
        extensions = dict(required.extensions or {}); pi.append_payment_identifier_to_extensions(extensions, ident)
        payload = core.create_payment_payload(required, extensions=extensions)
        ctx2, second = server.handle(hclient.encode_payment_signature_header(payload))
        db.execute("UPDATE payment_intents SET state='submitted', submissions=1, authorization_json=?, updated_at=? WHERE id=?", (json.dumps({'nonce': payload.payload['permit2Authorization']['nonce'], 'signer': orig['recipient'], 'custody': 'synthetic provider account key held by the test fixture (preauthorized local recovery arrangement)'}), now(), rid))
        db.execute('COMMIT'); db.execute('BEGIN IMMEDIATE')
        if body.get('_simulate_lost_response') and self.settings.limits.get('test_hooks'):
            db.execute("UPDATE payment_intents SET state='unknown', error='simulated lost response during refund submission', updated_at=? WHERE id=?", (now(), rid))
            server.settle(ctx2, second, amount)                                   # executed on the rail, response lost
            return dict(self.intent_view(db, principal, rid), note='refund submitted; response lost; reconcile')
        settled = server.settle(ctx2, second, amount)
        if not settled.success:
            db.execute("UPDATE payment_intents SET state='failed', error=?, updated_at=? WHERE id=?", (settled.error_reason, now(), rid))
            return self.intent_view(db, principal, rid)
        return self._observe_refund(db, principal, rid, e, orig, amount, settled.transaction)

    def _observe_refund(self, db, principal, rid, e, orig, amount, txref):
        env = ENV[self.settings.provider_mode]
        from .ops import fault
        fault(db, self.settings, 'refund_observation')
        r = self.intent_row(db, principal, rid)
        obs = json.loads(r['observations_json']); obs.append({'source': 'local facilitator settle response', 'at': now(), 'transaction': txref, 'amount': amount})
        db.execute("UPDATE payment_intents SET state='settled', transaction_ref=?, observations_json=?, updated_at=? WHERE id=?", (txref, json.dumps(obs), now(), rid))
        journal.post(db, principal.workspace, 'refund-settle:%s' % rid, 'refund_settled', orig['asset'], orig['network'], env, [('liability:refund_claim', amount, 0), ('asset:refund_receivable', 0, amount), ('asset:payer_cash', amount, 0), ('asset:provider_returned', 0, amount)], 'payment_intent', rid, 'reverse transfer observed: returned assets (not a service credit)')
        total = db.execute("SELECT COALESCE(SUM(final_amount),0) FROM payment_intents WHERE original_intent_id=? AND kind='refund' AND state='settled'", (orig['id'],)).fetchone()[0]
        db.execute("UPDATE work_entitlements SET state=?, updated_at=? WHERE id=?", ('refunded' if total >= orig['final_amount'] else 'paid', now(), e['id']))
        award = db.execute('SELECT * FROM work_awards WHERE id=?', (e['award_id'],)).fetchone(); ms = db.execute('SELECT * FROM work_milestones WHERE id=?', (e['milestone_id'],)).fetchone()
        self.evidence._receipt(db, 'settlement', award, ms, None, rid, {'refund_of': orig['id'], 'amount': amount, 'transaction': txref, 'returned_assets': True, 'service_credit': False, 'rail': 'local-chain'}, 'rail:local-chain')
        history.record(db, principal.workspace, principal.id, 'work.refund', 'payment_intent', rid, {'original': orig['id'], 'amount': amount, 'transaction': txref, 'observed': True})
        return self.intent_view(db, principal, rid)

    def reconcile_refund(self, db, principal, rid):
        principal.require('work:pay')
        r = self.intent_row(db, principal, rid)
        if r['kind'] != 'refund' or r['state'] not in ('unknown', 'submitted'):
            return dict(self.intent_view(db, principal, rid), reconciliation='nothing to query')
        chain = self.chain(); auth = json.loads(r['authorization_json'] or '{}')
        p2 = chain.w3.eth.contract(address=chain.permit2, abi=chain.art['contracts']['Permit2']['abi'])
        nonce = int(auth['nonce']); bitmap = p2.functions.nonceBitmap(chain.w3.to_checksum_address(r['payer']), nonce >> 8).call()
        if (bitmap >> (nonce & 0xff)) & 1:
            e = self._ent(db, principal, r['entitlement_id']); orig = db.execute('SELECT * FROM payment_intents WHERE id=?', (r['original_intent_id'],)).fetchone()
            return self._observe_refund(db, principal, rid, e, orig, r['final_amount'], 'nonce:' + str(nonce))
        db.execute("UPDATE payment_intents SET state='failed', error='nonce unused: refund never executed', updated_at=? WHERE id=?", (now(), rid))
        return self.intent_view(db, principal, rid)

    def credit(self, db, principal, eid, body):
        """An application credit: a future service entitlement / accounting liability, explicitly NOT returned currency."""
        principal.require('work:pay')
        e = self._ent(db, principal, eid)
        amount = body.get('amount'); terms_mod._int(amount, 1, 10 ** 15, 'credit_amount')
        cid = 'wcr_' + secrets.token_hex(6)
        env = ENV[self.settings.provider_mode]
        journal.post(db, principal.workspace, 'credit:%s' % cid, 'application_credit', e['asset'], self._network(e['asset']), env, [('expense:credit_granted', amount, 0), ('liability:application_credit', 0, amount)], 'work_entitlement', eid, 'application credit (future service entitlement); no currency moved')
        history.record(db, principal.workspace, principal.id, 'work.refund', 'work_entitlement', eid, {'credit_id': cid, 'amount': amount, 'currency_moved': False})
        return {'credit_id': cid, 'entitlement_id': eid, 'amount': amount, 'asset': e['asset'], 'label': 'application credit: a liability of this service, not returned currency'}

    # ---- views ------------------------------------------------------------------------------------------------------------------
    def list_intents(self, db, principal, state=None):
        principal.require('work:read')
        sql, args = 'SELECT id FROM payment_intents WHERE workspace=?', [principal.workspace]
        if state:
            sql += ' AND state=?'; args.append(state)
        return [self.intent_view(db, principal, r['id']) for r in db.execute(sql + ' ORDER BY created_at DESC LIMIT 200', args).fetchall()]

    def exposure(self, db, principal):
        principal.require('work:read')
        rows = db.execute("SELECT id, state, max_amount, final_amount, asset, network FROM payment_intents WHERE workspace=? AND state IN ('submitted','unknown','expired','authorized')", (principal.workspace,)).fetchall()
        return {'unresolved': [dict(r) for r in rows], 'total_by_scope': {}, 'note': 'authorized = signed but unsent (may still be exercised only by this service); submitted/unknown = sent, unobserved; expired = may have been exercised before expiry; reconcile each by identifier'}
