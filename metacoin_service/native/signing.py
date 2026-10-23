"""Signing requests (order 2026-10-23 group A): the service never signs for an external-custody account. When an economic
action of such an account needs authorization (a mission payment, a resource reservation, a subcontract funding, a product
purchase, a relay fee), the service records a *signing request* carrying the canonical intent and the context it binds
(policy_hash = sha256 of the canonical context). The account holder's own agent (CLI, provider client, controller runner)
fetches pending requests, has its signer check and sign them, and submits the signature; the service then executes the bound
action exactly once. If no signer answers, the request stays pending and the dependent milestone waits: there is no fallback."""
import json
import secrets

from .. import history
from ..db import now
from ..errors import ServiceError
from . import protocol as P

KINDS = ('transfer', 'settlement', 'reservation', 'subcontract_funding', 'product_purchase', 'relay_fee')
TTL = 900


class Signing:
    def __init__(self, settings, services):
        self.settings, self.svc = settings, services

    def create(self, db, workspace, account_id, kind, message, context, ref_type=None, ref_id=None, extra=None):
        """Record a pending request for `account_id` (idempotent by request_id of the message). `context` is exactly what the signer
        is shown and hashes (policy_hash); `extra` is service-side continuation data that is not part of the binding."""
        if kind == 'settlement':
            message = dict(message, nonce=self.svc.native.allocate_nonce(db, account_id))        # one allocated sequence per request: several settlements of one milestone never collide
        if kind not in KINDS:
            raise ServiceError('INTERNAL_DEFECT', 'signing kind')
        ex = db.execute("SELECT * FROM native_signing_requests WHERE workspace=? AND account_id=? AND json_extract(message_json, '$.request_id')=? AND state IN ('pending','signed')", (workspace, account_id, message['request_id'])).fetchone()
        if ex:
            return self._view(ex)
        ph = P.digest(context); message = dict(message, policy_hash=ph); rid = 'sr_' + secrets.token_hex(6); t = now()
        db.execute('INSERT INTO native_signing_requests VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (rid, workspace, account_id, kind, json.dumps(message), json.dumps({'bound': context, 'extra': extra or {}}), ph, ref_type, ref_id, 'pending', None, None, None, message.get('expires_at') or t + TTL, t, t))
        history.record(db, workspace, account_id, 'native.signing', 'signing_request', rid, {'kind': kind, 'amount': message.get('amount_ceiling'), 'recipient': message.get('recipient'), 'ref': [ref_type, ref_id]})
        return self._view(db.execute('SELECT * FROM native_signing_requests WHERE id=?', (rid,)).fetchone())

    def _view(self, r):
        cj = json.loads(r['context_json'])
        return {'schema': 'metacoin-signing-request/v1', 'id': r['id'], 'account_id': r['account_id'], 'kind': r['kind'], 'message': json.loads(r['message_json']), 'context': cj.get('bound', cj), 'extra': cj.get('extra'), 'policy_hash': r['policy_hash'], 'ref': [r['ref_type'], r['ref_id']], 'state': r['state'], 'key_id': r['key_id'], 'result': json.loads(r['result_json']) if r['result_json'] else None, 'expires_at': r['expires_at'], 'created_at': r['created_at'],
                'how_to_sign': 'POST <signer>/sign {actor, intent: message, context} then POST /native/signing/requests/{id}/submit {signature_hex, key_id}; the signer refuses if context and message disagree'}

    def _row(self, db, workspace, rid):
        r = db.execute('SELECT * FROM native_signing_requests WHERE id=? AND workspace=?', (rid, workspace)).fetchone()
        if r is None:
            raise ServiceError('NOT_FOUND', 'signing request')
        return r

    def pending(self, db, principal, account=None):
        principal.require('native:read'); acct = self.svc.native.account_row(db, principal.workspace, account) if account else self.svc.native.account_of(db, principal, create=False)
        if acct is None:
            return {'items': [], 'account': None}
        if acct['principal_id'] != principal.id and not principal.can('native:admin'):
            raise ServiceError('FORBIDDEN', 'only the account holder lists its signing requests')
        self._expire(db, principal.workspace)
        rows = db.execute("SELECT * FROM native_signing_requests WHERE workspace=? AND account_id=? AND state='pending' ORDER BY created_at", (principal.workspace, acct['id'])).fetchall()
        return {'account': acct['id'], 'custody': acct['custody'], 'items': [self._view(r) for r in rows]}

    def view(self, db, principal, rid):
        principal.require('native:read'); return self._view(self._row(db, principal.workspace, rid))

    def _expire(self, db, workspace):
        db.execute("UPDATE native_signing_requests SET state='expired', updated_at=? WHERE workspace=? AND state='pending' AND expires_at <= ?", (now(), workspace, now()))

    def cancel(self, db, principal, rid):
        r = self._row(db, principal.workspace, rid); acct = self.svc.native.account_row(db, principal.workspace, r['account_id'])
        if acct['principal_id'] != principal.id and not principal.can('native:admin'):
            raise ServiceError('FORBIDDEN', 'only the account holder cancels')
        if r['state'] == 'pending':
            db.execute("UPDATE native_signing_requests SET state='cancelled', updated_at=? WHERE id=?", (now(), rid))
        return self._view(self._row(db, principal.workspace, rid))

    def submit(self, db, principal, rid, body):
        """Execute the bound action with the holder's signature, exactly once (a replay returns the recorded result)."""
        r = self._row(db, principal.workspace, rid); acct = self.svc.native.account_row(db, principal.workspace, r['account_id'])
        if acct['principal_id'] != principal.id and not principal.can('native:admin'):
            raise ServiceError('FORBIDDEN', 'only the account holder submits its signatures')
        if r['state'] in ('signed',):
            return dict(self._view(r), replayed=True)
        if r['state'] != 'pending':
            raise ServiceError('CONFLICT', {'code': 'signing_request_state', 'state': r['state']})
        if type(body) is not dict or set(body) - {'signature_hex', 'key_id'} or type(body.get('signature_hex')) is not str or type(body.get('key_id')) is not str:
            raise ServiceError('VALIDATION', {'code': 'fields', 'allowed': ['signature_hex', 'key_id']})
        msg = json.loads(r['message_json']); ns = {'message': msg, 'signature_hex': body['signature_hex'], 'key_id': body['key_id']}
        if msg.get('expires_at') and msg['expires_at'] <= now():
            db.execute("UPDATE native_signing_requests SET state='expired', updated_at=? WHERE id=?", (now(), rid)); raise ServiceError('EXPIRED', {'code': 'intent_expired', 'note': 'the dependent action will issue a fresh request'})
        cj = json.loads(r['context_json']); ctx = dict(cj.get('bound', cj), **(cj.get('extra') or {}))
        try:
            result = self._execute(db, principal, r, ns, ctx)
        except ServiceError as exc:
            code = (exc.detail or {}).get('code') if isinstance(exc.detail, dict) else None
            if code in ('nonce_stale', 'native_signature_required', 'intent_expired', 'stale_quote'):
                # the bound action moved on (another intent consumed the sequence, the offer changed, the message expired): this request
                # is void and the owning component issues a fresh one; recorded durably even though this call is refused
                db.execute("UPDATE native_signing_requests SET state='expired', result_json=?, updated_at=? WHERE id=?", (json.dumps({'void': code}), now(), rid)); db.execute('COMMIT'); db.execute('BEGIN IMMEDIATE')
            raise
        db.execute("UPDATE native_signing_requests SET state='signed', signature_hex=?, key_id=?, result_json=?, updated_at=? WHERE id=?", (body['signature_hex'], body['key_id'], json.dumps(result, default=str), now(), rid))
        history.record(db, principal.workspace, principal.id, 'native.signing', 'signing_request', rid, {'executed': r['kind'], 'ref': [r['ref_type'], r['ref_id']]})
        self._after(db, principal, r, result)
        return dict(self._view(self._row(db, principal.workspace, rid)), executed=True)

    def _execute(self, db, principal, r, ns, ctx):
        N = self.svc.native; kind = r['kind']
        if kind == 'transfer':
            return N.transfer_submit(db, principal, ns)
        if kind == 'settlement':
            E = self.svc.economy; iid = ctx['payment_intent']
            i = E.money.intent_row(db, principal, iid)
            if i['state'] == 'authorized':
                return E.money.submit(db, principal, iid, {'native_signature': ns})
            return E.money.intent_view(db, principal, iid)
        if kind == 'reservation':
            return self.svc.market.reserve(db, principal, dict(ctx['reserve_body'], native_signature=ns))
        if kind in ('subcontract_funding', 'product_purchase', 'relay_fee'):
            return N.transfer_submit(db, principal, ns)
        raise ServiceError('INTERNAL_DEFECT', 'signing kind')

    def _after(self, db, principal, r, result):
        """Let the owning component continue (mission milestone bindings, subcontract / product / relay states)."""
        if r['ref_type'] == 'mission_milestone':
            self.svc.controller.on_signed(db, principal, r, result)
        elif r['ref_type'] and hasattr(self.svc, 'on_signed_hooks'):
            for fn in self.svc.on_signed_hooks.get(r['ref_type'], []):
                fn(db, principal, r, result)
