"""Scheduled relay service with native payment (order 2026-10-23 group F): communication delivery as a paid service on the
existing contact resource model. A route is a two-hop local store-and-forward path with separate persistent queues: sender →
relay 1 → relay 2 → recipient. Every hop is a funded contact reservation (an explicit obligation with a byte limit, a recipient,
a retention policy and an evidence policy); fees are bound before dispatch and never exceed the sender's declared ceiling; a
retry never charges again (idempotent by message id and hop). Four kinds of evidence are kept apart: custody/storage evidence
(a relay stored the bytes: hash, size, time), forwarding evidence (bytes moved to the next hop over the labelled loopback link
emulator), application delivery (the final recipient acknowledges the message id and content hash under its own credential),
and financial settlement (the hop reservation settles on measured delivery). An intermediate relay receipt is never proof that
the final mission succeeded. A dishonest receiver can withhold its acknowledgment: the declared dispute policy says what then
pays (`receiver_ack_required` pays the last hop only on the acknowledgment; `relay_evidence_after_timeout` pays on forwarding
evidence after the declared timeout); cryptography does not prove physical delivery and does not remove collusion. Transport is
file/HTTP on one host (labelled); it is not BPv7."""
import hashlib
import json
import secrets

from .. import history
from ..db import now
from ..errors import ServiceError
from . import protocol as P

MAX_BYTES = 4 * 1024 * 1024
MAX_RETENTION = 30 * 86400
DISPUTE_POLICIES = ('receiver_ack_required', 'relay_evidence_after_timeout')
TRANSPORT_LABEL = 'file/HTTP store-and-forward on one host over the labelled loopback link emulator; not BPv7, not a radio'


class Relay:
    def __init__(self, settings, services):
        self.settings, self.svc = settings, services

    # ---- route creation: bind every hop before any byte is accepted ------------------------------------------------------------------
    def create(self, db, principal, body):
        """hops: [{offer_id, offer_version, relay_account}] in order; the sender funds each hop's contact reservation (bytes → KiB)."""
        principal.require('native:transfer')
        if type(body) is not dict or set(body) - {'message_id', 'recipient_account', 'bytes', 'hops', 'fee_ceiling', 'retention_seconds', 'dispute_policy', 'ack_timeout_seconds', 'native_signatures'}:
            raise ServiceError('VALIDATION', {'code': 'fields', 'allowed': ['message_id', 'recipient_account', 'bytes', 'hops [{offer_id, offer_version, relay_account}]', 'fee_ceiling', 'retention_seconds?', 'dispute_policy (%s)' % '|'.join(DISPUTE_POLICIES), 'ack_timeout_seconds?']})
        mid = body.get('message_id'); nbytes = body.get('bytes'); hops = body.get('hops'); ceiling = body.get('fee_ceiling')
        if type(mid) is not str or not 1 <= len(mid) <= 128:
            raise ServiceError('VALIDATION', {'code': 'message_id'})
        if type(nbytes) is not int or isinstance(nbytes, bool) or not 1 <= nbytes <= MAX_BYTES:
            raise ServiceError('VALIDATION', {'code': 'payload_limit', 'max_bytes': MAX_BYTES, 'note': 'relay admission refuses payloads above the declared limit before any reservation'})
        if type(hops) is not list or not 1 <= len(hops) <= 4:
            raise ServiceError('VALIDATION', {'code': 'hops', 'allowed': '1..4 hops'})
        try:
            P.amount(ceiling, allow_zero=True)
        except P.ProtocolError as exc:
            raise ServiceError('VALIDATION', {'code': 'fee_ceiling'})
        ret = body.get('retention_seconds', 3600)
        if type(ret) is not int or not 60 <= ret <= MAX_RETENTION:
            raise ServiceError('VALIDATION', {'code': 'retention_limit', 'allowed': '60..%d seconds' % MAX_RETENTION})
        pol = body.get('dispute_policy', 'receiver_ack_required')
        if pol not in DISPUTE_POLICIES:
            raise ServiceError('VALIDATION', {'code': 'dispute_policy', 'allowed': list(DISPUTE_POLICIES)})
        N, M = self.svc.native, self.svc.market; sender = N.account_of(db, principal); rcpt = N.account_row(db, principal.workspace, body.get('recipient_account'))
        if rcpt['id'] == sender['id']:
            raise ServiceError('VALIDATION', {'code': 'recipient_is_sender'})
        ex = db.execute('SELECT id FROM relay_routes WHERE sender_account=? AND message_id=?', (sender['id'], mid)).fetchone()
        if ex:
            return dict(self.view(db, principal, ex['id']), replayed=True)
        kib = -(-nbytes // 1024); priced = []; total = 0
        for i, h in enumerate(hops):
            if type(h) is not dict or set(h) - {'offer_id', 'offer_version', 'relay_account'}:
                raise ServiceError('VALIDATION', {'code': 'hop', 'index': i})
            o = M._offer(db, principal.workspace, h.get('offer_id'))
            if o['kind'] != 'contact' or o['provider_account'] != h.get('relay_account'):
                raise ServiceError('VALIDATION', {'code': 'hop_offer', 'index': i, 'note': 'each hop names a contact offer of the relay account that will carry the bytes'})
            if h.get('offer_version') != o['version']:
                raise ServiceError('CONFLICT', {'code': 'stale_quote', 'hop': i, 'current_version': o['version']})
            price = kib * o['price_per_unit']; total += price; priced.append({'index': i, 'relay_account': o['provider_account'], 'offer_id': o['id'], 'offer_version': o['version'], 'kib': kib, 'price': price, 'byte_limit': nbytes, 'reservation_id': None, 'state': 'bound'})
        if total > ceiling:
            raise ServiceError('BUDGET_EXHAUSTED', {'code': 'fee_ceiling_exceeded', 'fees_bound': total, 'fee_ceiling': ceiling, 'note': 'every hop fee is bound before dispatch; the route is refused rather than overcharged'})
        rid = 'rl_' + secrets.token_hex(6); t = now()
        # fund the hops: one contact reservation per hop from the sender (the relay cannot debit the sender; the sender's reservation is the payment authority)
        sigs = body.get('native_signatures') or {}
        for h in priced:
            rb = {'offer_id': h['offer_id'], 'offer_version': h['offer_version'], 'quantity': kib, 'request_id': 'relay-%s-%d' % (mid, h['index']), 'buyer_account': sender['id'], 'purpose': 'relay hop %d of message %s' % (h['index'], mid)}
            if (sender['custody'] or '').startswith('external-signer'):
                if str(h['index']) not in sigs:
                    prep = M.reserve_prepare(db, principal, rb)
                    raise ServiceError('CONFLICT', {'code': 'native_signature_required', 'hop': h['index'], 'message': prep['message'], 'context': prep['context'], 'note': 'the sender account is held by an external signer: sign each hop reservation (native_signatures {hop_index: {message, signature_hex, key_id}}) and resubmit'})
                rb['native_signature'] = sigs[str(h['index'])]
            res = M.reserve(db, principal, rb); h['reservation_id'] = res['id']; h['state'] = 'reserved'
        pj = {'retention_seconds': ret, 'dispute_policy': pol, 'ack_timeout_seconds': body.get('ack_timeout_seconds', 600), 'evidence': {'custody': 'relay stores the bytes: sha256, size, stored_at (relay statement, service-recorded)', 'forwarding': 'bytes moved to the next hop over the labelled loopback link emulator: received bytes + digest (metered_fact, emulator)', 'application_delivery': 'the recipient acknowledges message id + content hash under its own credential (receiver statement)', 'settlement': 'each hop reservation settles on measured delivery; bounded by its reservation'}, 'transport': TRANSPORT_LABEL}
        db.execute('INSERT INTO relay_routes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (rid, principal.workspace, sender['id'], principal.id, rcpt['id'], mid, None, nbytes, json.dumps(priced), json.dumps(pj), ceiling, total, 'funded', json.dumps({'steps': [{'state': 'funded', 'at': t, 'fees_bound': total}]}), t, t))
        history.record(db, principal.workspace, principal.id, 'native.obligation', 'relay_route', rid, {'message_id': mid, 'hops': len(priced), 'fees_bound': total, 'bytes': nbytes})
        return self.view(db, principal, rid)

    def _row(self, db, ws, rid):
        r = db.execute('SELECT * FROM relay_routes WHERE id=? AND workspace=?', (rid, ws)).fetchone()
        if r is None:
            raise ServiceError('NOT_FOUND', 'relay route')
        return r

    def _ev(self, db, r, state, **kw):
        ev = json.loads(r['evidence_json']); ev['steps'].append(dict({'state': state, 'at': now()}, **kw)); db.execute('UPDATE relay_routes SET state=?, evidence_json=?, updated_at=? WHERE id=?', (state if state in ('funded', 'in_transit', 'delivered', 'acknowledged', 'failed', 'expired') else r['state'], json.dumps(ev), now(), r['id']))

    # ---- sender submits the payload to hop 0 (custody evidence) ----------------------------------------------------------------------
    def submit(self, db, principal, rid, data):
        r = self._row(db, principal.workspace, rid)
        if r['sender_principal'] != principal.id:
            raise ServiceError('FORBIDDEN', 'only the sender submits its message')
        if not data or len(data) > r['bytes']:
            raise ServiceError('VALIDATION', {'code': 'payload_limit', 'declared_bytes': r['bytes'], 'got': len(data or b''), 'note': 'the payload cannot exceed the declared byte limit the hops were funded for'})
        q = db.execute('SELECT * FROM relay_queue WHERE route_id=? AND hop_index=0', (rid,)).fetchone()
        if q:
            return dict(self.view(db, principal, rid), replayed=True)
        hops = json.loads(r['hops_json']); h0 = hops[0]; chash = hashlib.sha256(data).hexdigest(); pol = json.loads(r['policy_json'])
        art = self.svc.store.store(db, workspace=principal.workspace, kind='export', owner_id=principal.id, plaintext=data, recipients=[], intended_use='relay-message-' + r['message_id'], public=False)
        self.svc.market.activate(db, self._as(db, principal, h0['relay_account']), h0['reservation_id'])
        db.execute('INSERT INTO relay_queue VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', ('rq_' + secrets.token_hex(6), principal.workspace, rid, 0, h0['relay_account'], art, len(data), chash, 'stored', now() + pol['retention_seconds'], now(), None, 0, json.dumps({'custody': {'relay': h0['relay_account'], 'sha256': chash, 'bytes': len(data), 'stored_at': now(), 'kind': 'relay statement (service-recorded)'}}), now(), now()))
        db.execute('UPDATE relay_routes SET content_hash=?, updated_at=? WHERE id=?', (chash, now(), rid)); self._ev(db, self._row(db, principal.workspace, rid), 'in_transit', hop=0, custody_hash=chash)
        return self.view(db, principal, rid)

    def _as(self, db, principal, account_id):
        """The provider-side operations (activate, deliver) are performed on behalf of the relay account by the authority inside the
        route's declared contract: recorded as such (an administrator-level act bounded to the funded reservation)."""
        acct = self.svc.native.account_row(db, principal.workspace, account_id)
        class _P:
            pass
        p = _P(); p.id = acct['principal_id'] or principal.id; p.workspace = principal.workspace; p.role = 'actor'; p.can = lambda perm: perm in ('native:read', 'work:read', 'native:admin'); p.require = lambda perm: None; p.scope = None; return p

    # ---- a relay forwards to the next hop (forwarding evidence) or to the recipient --------------------------------------------------
    def forward(self, db, principal, rid, hop_index):
        r = self._row(db, principal.workspace, rid); hops = json.loads(r['hops_json']); pol = json.loads(r['policy_json'])
        if hop_index < 0 or hop_index >= len(hops):
            raise ServiceError('VALIDATION', {'code': 'hop_index'})
        h = hops[hop_index]; relay = self.svc.native.account_row(db, principal.workspace, h['relay_account'])
        if relay['principal_id'] != principal.id and not principal.can('native:admin'):
            raise ServiceError('FORBIDDEN', 'only the relay of this hop forwards')
        q = db.execute('SELECT * FROM relay_queue WHERE route_id=? AND hop_index=?', (rid, hop_index)).fetchone()
        if q is None or q['state'] not in ('stored',):
            if q is not None and q['state'] in ('forwarded', 'delivered'):
                return dict(self.view(db, principal, rid), replayed=True, note='already forwarded: no second charge')
            raise ServiceError('CONFLICT', {'code': 'nothing_stored_for_hop', 'hop': hop_index})
        if q['retention_until'] and now() > q['retention_until']:
            db.execute("UPDATE relay_queue SET state='expired', updated_at=? WHERE id=?", (now(), q['id'])); self._ev(db, r, 'expired', hop=hop_index)
            raise ServiceError('CONFLICT', {'code': 'retention_expired', 'hop': hop_index, 'note': 'an expired bundle does not prove that the hop obligation never committed: the reservation stays as recorded until settled or cancelled'})
        o = self.svc.market._offer(db, principal.workspace, h['offer_id']); res = self.svc.market._res(db, principal.workspace, h['reservation_id'])
        if res['state'] not in ('active', 'reserved'):
            raise ServiceError('CONFLICT', {'code': 'hop_reservation_state', 'state': res['state']})
        if res['state'] == 'reserved':
            self.svc.market.activate(db, self._as(db, principal, h['relay_account']), h['reservation_id'])
        data = self.svc.store.load(db, q['artifact_id'], principal.workspace)
        if hashlib.sha256(data).hexdigest() != q['content_hash']:
            raise ServiceError('INTERNAL_DEFECT', 'relay custody hash mismatch')
        from . import link
        from ..economy.ops import fault
        fault(db, self.settings, 'relay_forward')
        meta = json.loads(o['meta_json']); fw = link.transfer(data, meta.get('bandwidth_bytes_per_s'))
        if not fw['match']:
            db.execute('UPDATE relay_queue SET attempts=attempts+1, updated_at=? WHERE id=?', (now(), q['id'])); raise ServiceError('CONFLICT', {'code': 'hop_failed', 'hop': hop_index, 'evidence': fw})
        ev = json.loads(q['evidence_json']); ev['forwarding'] = dict(fw, hop=hop_index, to=hops[hop_index + 1]['relay_account'] if hop_index + 1 < len(hops) else r['recipient_account'])
        db.execute("UPDATE relay_queue SET state='forwarded', forwarded_at=?, attempts=attempts+1, evidence_json=?, updated_at=? WHERE id=?", (now(), json.dumps(ev), now(), q['id']))
        # measured delivery of this hop's reservation: the forwarded KiB
        d = self.svc.market.deliver(db, self._as(db, principal, h['relay_account']), h['reservation_id'], {'bytes': len(data)})
        if hop_index + 1 < len(hops):
            nh = hops[hop_index + 1]; db.execute('INSERT INTO relay_queue VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', ('rq_' + secrets.token_hex(6), principal.workspace, rid, hop_index + 1, nh['relay_account'], q['artifact_id'], len(data), q['content_hash'], 'stored', now() + pol['retention_seconds'], now(), None, 0, json.dumps({'custody': {'relay': nh['relay_account'], 'sha256': q['content_hash'], 'bytes': len(data), 'stored_at': now(), 'kind': 'relay statement (service-recorded)'}}), now(), now()))
            self._ev(db, self._row(db, principal.workspace, rid), 'in_transit', hop=hop_index + 1, forwarded_from=hop_index)
        else:
            self._ev(db, self._row(db, principal.workspace, rid), 'delivered', hop=hop_index, note='application delivery to the recipient queue: the recipient must acknowledge; this is not mission success')
        return dict(self.view(db, principal, rid), hop_delivery=d.get('delivered_quantity'))

    # ---- recipient acknowledges application content ------------------------------------------------------------------------------------
    def acknowledge(self, db, principal, rid, body):
        r = self._row(db, principal.workspace, rid); rcpt = self.svc.native.account_row(db, principal.workspace, r['recipient_account'])
        if rcpt['principal_id'] != principal.id:
            raise ServiceError('FORBIDDEN', 'only the recipient acknowledges')
        if r['state'] == 'acknowledged':
            return dict(self.view(db, principal, rid), replayed=True)
        if r['state'] != 'delivered':
            raise ServiceError('CONFLICT', {'code': 'not_delivered', 'state': r['state']})
        if type(body) is not dict or body.get('message_id') != r['message_id'] or body.get('content_hash') != r['content_hash']:
            raise ServiceError('VALIDATION', {'code': 'ack_binding', 'note': 'the acknowledgment names the message id and the content hash it received'})
        from ..economy.ops import fault
        fault(db, self.settings, 'relay_receipt')
        self._ev(db, r, 'acknowledged', recipient=rcpt['id'], content_hash=r['content_hash'], kind='receiver statement under its own credential (not a cryptographic proof of physical delivery)')
        return self.view(db, principal, rid)

    def fetch(self, db, principal, rid):
        """The recipient reads the delivered bytes (application delivery)."""
        r = self._row(db, principal.workspace, rid); rcpt = self.svc.native.account_row(db, principal.workspace, r['recipient_account'])
        if rcpt['principal_id'] != principal.id and r['sender_principal'] != principal.id:
            raise ServiceError('FORBIDDEN', 'only the recipient (or sender) reads the message')
        if r['state'] not in ('delivered', 'acknowledged'):
            raise ServiceError('CONFLICT', {'code': 'not_delivered', 'state': r['state']})
        last = db.execute('SELECT * FROM relay_queue WHERE route_id=? ORDER BY hop_index DESC LIMIT 1', (rid,)).fetchone()
        return self.svc.store.load(db, last['artifact_id'], principal.workspace), r['content_hash']

    # ---- settlement of hop fees under the declared policy -----------------------------------------------------------------------------
    def settle(self, db, principal, rid):
        """Settle every delivered hop reservation (once). Under receiver_ack_required the last hop settles only after the
        acknowledgment or the declared dispute path; under relay_evidence_after_timeout the forwarding evidence suffices after the timeout."""
        r = self._row(db, principal.workspace, rid); hops = json.loads(r['hops_json']); pol = json.loads(r['policy_json']); out = []
        if r['sender_principal'] != principal.id and not principal.can('native:admin'):
            raise ServiceError('FORBIDDEN', 'the sender settles the hops it funded')
        last_ok = r['state'] == 'acknowledged' or (pol['dispute_policy'] == 'relay_evidence_after_timeout' and r['state'] == 'delivered' and now() >= json.loads(r['evidence_json'])['steps'][-1]['at'] + pol['ack_timeout_seconds'])
        for i, h in enumerate(hops):
            q = db.execute('SELECT state FROM relay_queue WHERE route_id=? AND hop_index=?', (rid, i)).fetchone(); res = self.svc.market._res(db, principal.workspace, h['reservation_id'])
            if res['state'] == 'settled':
                out.append({'hop': i, 'state': 'settled', 'already': True}); continue
            if q is None or q['state'] not in ('forwarded', 'delivered'):
                out.append({'hop': i, 'state': 'not_delivered', 'reservation': res['state']}); continue
            if i == len(hops) - 1 and not last_ok:
                out.append({'hop': i, 'state': 'awaiting_acknowledgment', 'policy': pol['dispute_policy'], 'note': 'a dishonest receiver can withhold the acknowledgment; the declared dispute policy resolves it (timeout evidence or dispute), never cryptography alone'}); continue
            s = self.svc.market.settle(db, principal, h['reservation_id']); out.append({'hop': i, 'state': s['state'], 'owed': (s.get('obligation') or {}).get('settled_amount')})
        return {'route': rid, 'hops': out, 'fees_bound': r['fees_bound'], 'fee_ceiling': r['fee_ceiling']}

    def cancel_unused(self, db, principal, rid):
        """Release hops never activated (missed window / replan): capacity and funds return in full; delivered hops stay owed."""
        r = self._row(db, principal.workspace, rid); hops = json.loads(r['hops_json']); out = []
        if r['sender_principal'] != principal.id:
            raise ServiceError('FORBIDDEN', 'the sender cancels its unused hops')
        for h in hops:
            res = self.svc.market._res(db, principal.workspace, h['reservation_id'])
            if res['state'] == 'reserved':
                c = self.svc.market.cancel(db, principal, h['reservation_id'], {'reason': 'relay replan'}); out.append({'hop': h['index'], 'state': c['state'], 'released': c.get('price')})
            else:
                out.append({'hop': h['index'], 'state': res['state'], 'note': 'activated or delivered capacity is never released: the incurred charge stands'})
        if all(o['state'] == 'cancelled' for o in out):
            self._ev(db, r, 'failed', reason='all hops released before activation (replan)')
        return {'route': rid, 'hops': out}

    # ---- views ------------------------------------------------------------------------------------------------------------------------
    def view(self, db, principal, rid):
        principal.require('work:read'); r = self._row(db, principal.workspace, rid); hops = json.loads(r['hops_json'])
        queue = [dict(q, evidence=json.loads(q['evidence_json'])) for q in db.execute('SELECT * FROM relay_queue WHERE route_id=? ORDER BY hop_index', (rid,)).fetchall()]
        for h in hops:
            res = db.execute('SELECT state, delivered_quantity FROM resource_reservations WHERE id=?', (h['reservation_id'],)).fetchone() if h.get('reservation_id') else None
            h['reservation_state'] = res['state'] if res else None; h['delivered_kib'] = res['delivered_quantity'] if res else None
        ev = json.loads(r['evidence_json'])
        return {'schema': 'metacoin-relay-route/v1', 'id': rid, 'message_id': r['message_id'], 'sender_account': r['sender_account'], 'recipient_account': r['recipient_account'], 'bytes': r['bytes'], 'content_hash': r['content_hash'], 'hops': hops, 'policy': json.loads(r['policy_json']), 'fee_ceiling': r['fee_ceiling'], 'fees_bound': r['fees_bound'], 'state': r['state'],
                'queues': [{k: q[k] for k in ('id', 'hop_index', 'relay_account', 'bytes', 'content_hash', 'state', 'retention_until', 'stored_at', 'forwarded_at', 'attempts', 'evidence')} for q in queue],
                'evidence': ev, 'meaning': {'custody': 'a relay holds the bytes (relay statement)', 'forwarding': 'bytes moved to the next hop (emulator-metered)', 'delivered': 'the recipient queue holds the bytes; not an acknowledgment, not mission success', 'acknowledged': 'the recipient confirmed message id + content hash under its credential', 'settlement': 'per-hop reservation settlement, bounded by the funded reservation'},
                'transport': TRANSPORT_LABEL, 'created_at': r['created_at']}

    def list(self, db, principal):
        principal.require('work:read'); acct = self.svc.native.account_of(db, principal, create=False)
        rows = db.execute('SELECT id FROM relay_routes WHERE workspace=? ORDER BY created_at', (principal.workspace,)).fetchall()
        return [self.view(db, principal, r['id']) for r in rows if acct is None or principal.can('native:admin') or acct['id'] in (self._row(db, principal.workspace, r['id'])['sender_account'], self._row(db, principal.workspace, r['id'])['recipient_account']) or any(h['relay_account'] == acct['id'] for h in json.loads(self._row(db, principal.workspace, r['id'])['hops_json']))]
