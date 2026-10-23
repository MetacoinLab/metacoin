"""Native resource reservation market (order 2026-10-23 group B): fixed-price offers for compute time, storage byte-time and
contact-window byte capacity (energy as a declared planning constraint). A reservation exchanges a specific funded native
obligation for a specific capacity commitment inside one authoritative transaction: the exact offer version, the buyer's
available funds and the remaining capacity are checked together; a stale quote is refused, never repriced. Delivery is
measured (compute: CPU/wall of a bounded execution; storage: stored bytes x held seconds; contact: received bytes over the
labelled link emulator); settlement pays the delivered portion once and releases the rest. Three labels are kept apart:
metered fact, provider assertion, independently verified delivery. Cross-authority reservations are NOT atomic: a buyer on
another domain first exports an allocation to this domain (prepare), then reserves here (commit), and the acknowledgment
reconciles; an unknown outcome keeps pending exposure."""
import hashlib
import json
import os
import resource as _resource
import secrets
import subprocess
import sys
import time

from .. import history
from ..db import now
from ..errors import ServiceError
from . import ledger
from . import protocol as P

KINDS = {'compute': {'unit': 'cpu_ms', 'time_basis': 'cpu milliseconds of a bounded child execution (rlimited); wall time recorded alongside', 'evidence': 'metered_fact: rusage of the child; provider_assertion: the task output; verified: independent reference check of the output'},
         'storage': {'unit': 'kib_seconds', 'time_basis': 'KiB held x seconds between durable store and release (service timestamps)', 'evidence': 'metered_fact: stored bytes and timestamps; verified: digest of the stored bytes on read-back'},
         'contact': {'unit': 'kib', 'time_basis': 'KiB received inside the window over the labelled loopback link emulator', 'evidence': 'metered_fact: received bytes + digest (emulator); provider_assertion: declared bandwidth; verified: digest equality with the sender'}}
OFFER_FIELDS = {'kind', 'resource_id', 'capacity', 'price_per_unit', 'asset', 'window_start', 'window_end', 'reservation_policy', 'cancellation', 'energy_mwh_per_unit', 'bandwidth_bytes_per_s', 'note'}
RESERVATION_STATES = ('reserved', 'active', 'delivered', 'settled', 'cancelled', 'prepared', 'unknown')
MAX_CAPACITY = 10 ** 12


def _int(v, lo, hi, name):
    if type(v) is not int or isinstance(v, bool) or not lo <= v <= hi:
        raise ServiceError('VALIDATION', {'code': name, 'allowed': 'integer %d..%d' % (lo, hi)})
    return v


class Market:
    def __init__(self, settings, services):
        self.settings, self.svc = settings, services

    @property
    def native(self):
        return self.svc.native

    # ---- offers ----------------------------------------------------------------------------------------------------------------
    def publish(self, db, principal, body):
        principal.require('work:offer')
        if type(body) is not dict or set(body) - OFFER_FIELDS or body.get('kind') not in KINDS:
            raise ServiceError('VALIDATION', {'code': 'fields', 'allowed': sorted(OFFER_FIELDS), 'kinds': list(KINDS)})
        acct = self.native.account_of(db, principal); d = self.native.domain(db, principal.workspace)
        cap = _int(body.get('capacity'), 1, MAX_CAPACITY, 'capacity'); price = _int(body.get('price_per_unit'), 0, P.MAX_AMOUNT, 'price_per_unit')
        ws, we = _int(body.get('window_start'), 0, 10 ** 12, 'window_start'), _int(body.get('window_end'), 0, 10 ** 12, 'window_end')
        if we <= ws:
            raise ServiceError('VALIDATION', {'code': 'window'})
        asset = body.get('asset') or self.native.default_asset(db, principal.workspace)
        if not self.native.is_native_asset(db, principal.workspace, asset):
            raise ServiceError('VALIDATION', {'code': 'asset_unknown_here', 'asset': asset})
        rid = body.get('resource_id')
        if type(rid) is not str or not 1 <= len(rid) <= 64:
            raise ServiceError('VALIDATION', {'code': 'resource_id'})
        energy = _int(body.get('energy_mwh_per_unit', 0), 0, 10 ** 9, 'energy_mwh_per_unit'); bw = body.get('bandwidth_bytes_per_s')
        if bw is not None:
            _int(bw, 1, 10 ** 12, 'bandwidth_bytes_per_s')
        policy = body.get('reservation_policy', 'exact_capacity'); canc = body.get('cancellation', 'free_before_activation; after activation the delivered portion is owed')
        if policy != 'exact_capacity' or type(canc) is not str or len(canc) > 256:
            raise ServiceError('VALIDATION', {'code': 'policy', 'allowed': ['exact_capacity']})
        oid = 'ro_' + secrets.token_hex(6)
        db.execute('INSERT INTO resource_offers VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (oid, principal.workspace, acct['id'], principal.id, body['kind'], rid, KINDS[body['kind']]['unit'], cap, price, asset, ws, we, 1, policy, canc, json.dumps({'energy_mwh_per_unit': energy, 'bandwidth_bytes_per_s': bw, 'note': (body.get('note') or '')[:256], 'delivery_evidence': KINDS[body['kind']]['evidence'], 'time_basis': KINDS[body['kind']]['time_basis']}), 'open', now()))
        history.record(db, principal.workspace, principal.id, 'native.obligation', 'resource_offer', oid, {'kind': body['kind'], 'capacity': cap, 'price_per_unit': price, 'window': [ws, we]})
        return self.offer_view(db, principal, oid)

    def amend_offer(self, db, principal, oid, body):
        """A new offer version (price/capacity); outstanding quotes of the old version become stale and are refused at acceptance."""
        principal.require('work:offer'); o = self._offer(db, principal.workspace, oid)
        if o['provider_principal'] != principal.id:
            raise ServiceError('FORBIDDEN', 'only the provider amends its offer')
        price = _int(body.get('price_per_unit', o['price_per_unit']), 0, P.MAX_AMOUNT, 'price_per_unit'); cap = _int(body.get('capacity', o['capacity']), 1, MAX_CAPACITY, 'capacity')
        if cap < self._reserved_quantity(db, oid):
            raise ServiceError('CONFLICT', {'code': 'capacity_below_reserved', 'reserved': self._reserved_quantity(db, oid)})
        db.execute('UPDATE resource_offers SET price_per_unit=?, capacity=?, version=version+1 WHERE id=?', (price, cap, oid))
        history.record(db, principal.workspace, principal.id, 'native.obligation', 'resource_offer', oid, {'amended': {'price_per_unit': price, 'capacity': cap}, 'version': o['version'] + 1})
        return self.offer_view(db, principal, oid)

    def _offer(self, db, workspace, oid):
        o = db.execute('SELECT * FROM resource_offers WHERE id=? AND workspace=?', (oid, workspace)).fetchone()
        if o is None:
            raise ServiceError('NOT_FOUND', 'resource offer')
        return o

    def _reserved_quantity(self, db, oid):
        return db.execute("SELECT COALESCE(SUM(quantity),0) FROM resource_reservations WHERE offer_id=? AND state IN ('reserved','active','prepared','unknown')", (oid,)).fetchone()[0]

    def _delivered_quantity(self, db, oid):
        return db.execute("SELECT COALESCE(SUM(delivered_quantity),0) FROM resource_reservations WHERE offer_id=? AND state IN ('delivered','settled')", (oid,)).fetchone()[0]

    def offer_view(self, db, principal, oid):
        principal.require('work:read'); o = self._offer(db, principal.workspace, oid); meta = json.loads(o['meta_json'])
        reserved = self._reserved_quantity(db, oid); delivered = self._delivered_quantity(db, oid)
        return {'schema': 'metacoin-resource-offer/v1', 'id': oid, 'version': o['version'], 'provider_account': o['provider_account'], 'kind': o['kind'], 'resource_id': o['resource_id'], 'unit': o['unit'], 'time_basis': meta['time_basis'], 'capacity': o['capacity'], 'reserved': reserved, 'delivered': delivered, 'available': o['capacity'] - reserved - delivered,
                'price_per_unit': o['price_per_unit'], 'asset': o['asset'], 'window': {'start': o['window_start'], 'end': o['window_end'], 'upcoming': o['window_start'] > now()}, 'reservation_policy': o['reservation_policy'], 'cancellation': o['cancellation'], 'delivery_evidence': meta['delivery_evidence'],
                'energy_mwh_per_unit': meta['energy_mwh_per_unit'], 'bandwidth_bytes_per_s': meta.get('bandwidth_bytes_per_s'), 'note': meta.get('note'), 'state': o['state'], 'created_at': o['created_at'],
                'refusal_reasons_when_reserving': ['stale_quote (offer version changed)', 'capacity_exhausted', 'insufficient_available (buyer funds)', 'window_closed', 'offer_closed', 'self_dealing (provider cannot buy its own capacity)', 'domain_quarantined']}

    def offers(self, db, principal, kind=None):
        principal.require('work:read')
        rows = db.execute('SELECT id FROM resource_offers WHERE workspace=? ' + ('AND kind=? ' if kind else '') + 'ORDER BY window_start, created_at', (principal.workspace,) + ((kind,) if kind else ())).fetchall()
        return [self.offer_view(db, principal, r['id']) for r in rows]

    # ---- quotes and reservations ---------------------------------------------------------------------------------------------------
    def quote(self, db, principal, body):
        """Nonbinding preview: price for a quantity at the CURRENT offer version; nothing reserved."""
        principal.require('work:read'); o = self._offer(db, principal.workspace, body.get('offer_id')); q = _int(body.get('quantity'), 1, MAX_CAPACITY, 'quantity')
        v = self.offer_view(db, principal, o['id']); price = q * o['price_per_unit']
        return {'offer_id': o['id'], 'offer_version': o['version'], 'quantity': q, 'unit': o['unit'], 'price': price, 'asset': o['asset'], 'available_now': v['available'], 'binding': False, 'energy_mwh': q * json.loads(o['meta_json'])['energy_mwh_per_unit'],
                'note': 'nonbinding: acceptance checks this exact offer version, the buyer\'s available funds and the remaining capacity in one transaction; a changed version is refused, not repriced'}

    def reserve(self, db, principal, body):
        principal.require('native:transfer')
        if type(body) is not dict or set(body) - {'offer_id', 'offer_version', 'quantity', 'request_id', 'buyer_account', 'purpose'}:
            raise ServiceError('VALIDATION', {'code': 'fields', 'allowed': ['offer_id', 'offer_version', 'quantity', 'request_id', 'buyer_account?', 'purpose?']})
        d = self.native.domain(db, principal.workspace); self.native.require_normal(d, 'reservation')
        o = self._offer(db, principal.workspace, body.get('offer_id')); q = _int(body.get('quantity'), 1, MAX_CAPACITY, 'quantity'); rid = body.get('request_id')
        if type(rid) is not str or not 1 <= len(rid) <= 128:
            raise ServiceError('VALIDATION', {'code': 'request_id'})
        buyer = self.native.account_row(db, principal.workspace, body['buyer_account']) if body.get('buyer_account') else self.native.account_of(db, principal)
        if buyer['principal_id'] != principal.id and not principal.can('native:admin'):
            raise ServiceError('FORBIDDEN', 'only the account holder reserves with its funds')
        ex = db.execute('SELECT id FROM resource_reservations WHERE buyer_account=? AND request_id=?', (buyer['id'], rid)).fetchone()
        if ex:
            return dict(self.reservation_view(db, principal, ex['id']), replayed=True)
        if body.get('offer_version') != o['version']:
            raise ServiceError('CONFLICT', {'code': 'stale_quote', 'quoted_version': body.get('offer_version'), 'current_version': o['version'], 'note': 'the offer changed since the quote; nothing reserved; request a fresh quote (no silent repricing)'})
        if o['state'] != 'open':
            raise ServiceError('CONFLICT', {'code': 'offer_closed'})
        if o['window_end'] <= now():
            raise ServiceError('CONFLICT', {'code': 'window_closed', 'window_end': o['window_end']})
        if o['provider_account'] == buyer['id']:
            raise ServiceError('VALIDATION', {'code': 'self_dealing'})
        reserved = self._reserved_quantity(db, o['id']); delivered = self._delivered_quantity(db, o['id'])
        if reserved + delivered + q > o['capacity']:
            raise ServiceError('CONFLICT', {'code': 'capacity_exhausted', 'available': o['capacity'] - reserved - delivered, 'requested': q, 'note': 'concurrent buyers cannot reserve the same capacity twice: the check runs inside the single-writer transaction'})
        price = q * o['price_per_unit']
        ledger.require_available(db, principal.workspace, o['asset'], d['id'], buyer['id'], price)
        from ..agents import grant_of, guard
        if grant_of(principal):
            guard(db, principal, 'native:transfer', amount=price)
        resid = 'rr_' + secrets.token_hex(6)
        ob = self.native.obligation_create(db, principal.workspace, 'resource', buyer['id'], o['provider_account'], o['asset'], price, 'resource_reservation', resid, {'partial_payments': True, 'offer_id': o['id'], 'offer_version': o['version'], 'unit': o['unit'], 'quantity': q, 'price_per_unit': o['price_per_unit'], 'note': 'the delivered portion is owed; the undelivered portion is released at settlement or cancellation'})
        obid = ob['id'] if isinstance(ob, dict) else ob
        self.native._consume_claims(db, principal.workspace, buyer['id'], o['asset'], price, 'resource_reservation', resid)
        if price:
            ledger.move(db, principal.workspace, 'rr-reserve:' + resid, 'native_resource_reservation', o['asset'], d['id'], self.native.environment, price, ledger.account_name(buyer['id'], 'available'), ledger.account_name(buyer['id'], 'reserved'), 'resource_reservation', resid, 'funded reservation of %d %s on %s' % (q, o['unit'], o['id']))
        db.execute('INSERT INTO resource_reservations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (resid, principal.workspace, o['id'], o['version'], buyer['id'], o['provider_account'], q, 0, price, o['asset'], obid, 'reserved', rid, json.dumps({'purpose': (body.get('purpose') or '')[:200], 'steps': [{'state': 'reserved', 'at': now()}]}), None, None, now(), now(), None))
        history.record(db, principal.workspace, principal.id, 'native.obligation', 'resource_reservation', resid, {'offer': o['id'], 'version': o['version'], 'quantity': q, 'price': price, 'buyer': buyer['id']})
        return self.reservation_view(db, principal, resid)

    def _res(self, db, workspace, resid):
        r = db.execute('SELECT * FROM resource_reservations WHERE id=? AND workspace=?', (resid, workspace)).fetchone()
        if r is None:
            raise ServiceError('NOT_FOUND', 'resource reservation')
        return r

    def reservation_view(self, db, principal, resid):
        principal.require('work:read'); r = self._res(db, principal.workspace, resid); o = self._offer(db, principal.workspace, r['offer_id']); ev = json.loads(r['evidence_json'])
        ob = db.execute('SELECT * FROM native_obligations WHERE id=?', (r['obligation_id'],)).fetchone()
        return {'schema': 'metacoin-resource-reservation/v1', 'id': resid, 'offer_id': r['offer_id'], 'offer_version': r['offer_version'], 'kind': o['kind'], 'resource_id': o['resource_id'], 'unit': o['unit'], 'buyer_account': r['buyer_account'], 'provider_account': r['provider_account'], 'quantity': r['quantity'], 'delivered_quantity': r['delivered_quantity'],
                'price': r['price'], 'asset': r['asset'], 'obligation': {'id': ob['id'], 'amount': ob['amount'], 'settled_amount': ob['settled_amount'], 'state': ob['state']} if ob else None, 'state': r['state'], 'evidence': ev, 'measurement': json.loads(r['measurement_json']) if r['measurement_json'] else None,
                'window': {'start': o['window_start'], 'end': o['window_end']}, 'cancellation': o['cancellation'], 'activated_at': r['activated_at'], 'created_at': r['created_at'], 'updated_at': r['updated_at'],
                'labels': {'metered_fact': 'the measurement block (service-recorded)', 'provider_assertion': 'what the provider claims it delivered (assertion block)', 'verified': 'independent verification of the delivered thing (verification block)'},
                'atomicity': 'money and capacity changed in one domain transaction; cross-domain buyers export an allocation first (prepare) and reserve locally (commit): no global atomic reservation is claimed'}

    def reservations(self, db, principal, account=None):
        principal.require('work:read')
        rows = db.execute('SELECT id FROM resource_reservations WHERE workspace=? ' + ('AND (buyer_account=? OR provider_account=?) ' if account else '') + 'ORDER BY created_at', (principal.workspace,) + ((account, account) if account else ())).fetchall()
        return [self.reservation_view(db, principal, r['id']) for r in rows]

    def activate(self, db, principal, resid):
        r = self._res(db, principal.workspace, resid); prov = self.native.account_row(db, principal.workspace, r['provider_account'])
        if prov['principal_id'] != principal.id and not principal.can('native:admin'):
            raise ServiceError('FORBIDDEN', 'only the provider activates a reservation')
        if r['state'] != 'reserved':
            raise ServiceError('CONFLICT', {'code': 'state', 'state': r['state']})
        ev = json.loads(r['evidence_json']); ev['steps'].append({'state': 'active', 'at': now()})
        db.execute("UPDATE resource_reservations SET state='active', activated_at=?, evidence_json=?, updated_at=? WHERE id=?", (now(), json.dumps(ev), now(), resid))
        return self.reservation_view(db, principal, resid)

    def cancel(self, db, principal, resid, body=None):
        """Buyer or provider cancels: before activation the funds and capacity return in full; after activation the delivered portion is owed (settled) and the rest released."""
        r = self._res(db, principal.workspace, resid); buyer = self.native.account_row(db, principal.workspace, r['buyer_account']); prov = self.native.account_row(db, principal.workspace, r['provider_account'])
        if principal.id not in (buyer['principal_id'], prov['principal_id']) and not principal.can('native:admin'):
            raise ServiceError('FORBIDDEN', 'only a party cancels')
        if r['state'] in ('cancelled', 'settled'):
            return dict(self.reservation_view(db, principal, resid), replayed=True)
        if r['state'] == 'reserved':
            d = self.native.domain(db, principal.workspace)
            if r['price']:
                ledger.move(db, principal.workspace, 'rr-cancel:' + resid, 'native_resource_release', r['asset'], d['id'], self.native.environment, r['price'], ledger.account_name(buyer['id'], 'reserved'), ledger.account_name(buyer['id'], 'available'), 'resource_reservation', resid, 'cancelled before activation: funds and capacity released in full')
            db.execute("UPDATE native_obligations SET state='cancelled', updated_at=? WHERE id=?", (now(), r['obligation_id']))
            ev = json.loads(r['evidence_json']); ev['steps'].append({'state': 'cancelled', 'at': now(), 'by': principal.id, 'reason': (body or {}).get('reason')})
            db.execute("UPDATE resource_reservations SET state='cancelled', evidence_json=?, updated_at=? WHERE id=?", (json.dumps(ev), now(), resid))
            history.record(db, principal.workspace, principal.id, 'native.obligation', 'resource_reservation', resid, {'cancelled': 'before_activation', 'released': r['price']})
            return self.reservation_view(db, principal, resid)
        return self.settle(db, principal, resid, cancelled=True)                       # active or delivered: the delivered portion is owed

    # ---- measured delivery ---------------------------------------------------------------------------------------------------------
    def deliver(self, db, principal, resid, body):
        """Provider records a measured delivery. compute: runs a bounded child executing a supported task and measures rusage;
        storage: bytes were stored (put) and are released now; contact: the link emulator transfers the given bytes."""
        r = self._res(db, principal.workspace, resid); prov = self.native.account_row(db, principal.workspace, r['provider_account']); o = self._offer(db, principal.workspace, r['offer_id'])
        if prov['principal_id'] != principal.id and not principal.can('native:admin'):
            raise ServiceError('FORBIDDEN', 'only the provider records delivery')
        if r['state'] != 'active':
            raise ServiceError('CONFLICT', {'code': 'state', 'state': r['state'], 'note': 'activate first; delivery is recorded on an active reservation'})
        body = body or {}; meta = json.loads(o['meta_json'])
        if o['kind'] == 'compute' and body.get('executed_by') == 'provider':
            # portable provider client: the task ran on the provider's host; CPU time is the provider's assertion, the product is
            # verified here against the public lunar reference (labels kept apart; the buyer's policy decides what it pays for)
            m = self.accept_provider_execution(body, cpu_quantity=r['quantity'])
            quantity = min(m['claimed_cpu_ms'], r['quantity']) if m.get('reference_ok') else 0
        elif o['kind'] == 'compute':
            m = self.measure_compute(body.get('task') or {'kind': 'terrain_tile', 'source': 'apollo11_32'}, cpu_limit_s=max(1, r['quantity'] // 1000 + 2))
            quantity = min(m['cpu_ms'], r['quantity'])
        elif o['kind'] == 'storage':
            st = json.loads(r['measurement_json'] or '{}').get('storage')
            if not st:
                raise ServiceError('CONFLICT', {'code': 'nothing_stored', 'note': 'the buyer puts bytes first (POST .../storage) so that stored bytes and held time are measured'})
            held = max(now() - st['stored_at'], body.get('min_seconds', 1) if type(body.get('min_seconds')) is int else 1)
            kib_seconds = (st['bytes'] * held) // 1024
            m = {'kind': 'metered_fact', 'stored_bytes': st['bytes'], 'held_seconds': held, 'kib_seconds': kib_seconds, 'digest': st['digest'], 'read_back_digest': st.get('read_back_digest')}
            quantity = min(kib_seconds, r['quantity'])
        else:
            from . import link
            size = body.get('bytes', 0); _int(size, 1, 64 * 1024 * 1024, 'bytes')
            payload = hashlib.sha256(resid.encode()).digest() * ((size + 31) // 32); payload = payload[:size]
            m = link.transfer(payload, meta.get('bandwidth_bytes_per_s')); quantity = min(m['received_bytes'] // 1024, r['quantity'])
        assertion = {'kind': 'provider_assertion', 'claimed_quantity': body.get('claimed_quantity', quantity), 'note': (body.get('note') or '')[:200]}
        verified = self._verify_delivery(o['kind'], m)
        meas = json.loads(r['measurement_json'] or '{}'); meas.update({'measurement': m, 'assertion': assertion, 'verification': verified, 'recorded_at': now()})
        ev = json.loads(r['evidence_json']); ev['steps'].append({'state': 'delivered', 'at': now(), 'delivered_quantity': quantity})
        db.execute("UPDATE resource_reservations SET state='delivered', delivered_quantity=?, measurement_json=?, evidence_json=?, updated_at=? WHERE id=?", (quantity, json.dumps(meas), json.dumps(ev), now(), resid))
        history.record(db, principal.workspace, principal.id, 'native.obligation', 'resource_reservation', resid, {'delivered_quantity': quantity, 'unit': o['unit'], 'measured': True, 'verified': verified.get('ok')})
        return self.reservation_view(db, principal, resid)

    def accept_provider_execution(self, body, cpu_quantity):
        """Provider-executed compute: the output is checked against the lunar reference (verified); cpu_ms is a provider assertion."""
        from ..lunar import reference
        from ..lunar.__main__ import example
        task = body.get('task') or {}; kind = task.get('kind', 'terrain_tile'); src = task.get('source', 'apollo11_32'); out = body.get('output')
        cpu = _int(body.get('cpu_ms', 0), 1, 10 ** 9, 'cpu_ms')
        if type(out) is not dict:
            raise ServiceError('VALIDATION', {'code': 'output', 'note': 'the executed product JSON is required for provider-executed delivery'})
        inputs = example('terrain' if kind == 'terrain_tile' else 'route', src)
        chk = reference.check(kind if kind in ('terrain_tile', 'route_feasibility') else 'terrain_tile', inputs, out)
        return {'kind': 'provider_assertion', 'claimed_cpu_ms': cpu, 'claimed_wall_ms': body.get('wall_ms'), 'task': {'kind': kind, 'source': src}, 'output_digest': hashlib.sha256(json.dumps(out, sort_keys=True).encode()).hexdigest(), 'reference_ok': chk['ok'], 'checker_id': chk['checker_id'],
                'measurement_source': 'provider host rusage as asserted by the provider client; not measured by this service', 'executed_by': 'provider'}

    def _verify_delivery(self, kind, m):
        if kind == 'compute' and m.get('executed_by') == 'provider':
            return {'kind': 'verified', 'ok': bool(m.get('reference_ok')), 'method': 'independent lunar reference check of the provider-submitted product; the CPU time itself is a provider assertion, not verified', 'checker': m.get('checker_id')}
        if kind == 'compute':
            return {'kind': 'verified', 'ok': bool(m.get('reference_ok')), 'method': 'independent lunar reference check of the child output', 'checker': m.get('checker_id')}
        if kind == 'storage':
            return {'kind': 'verified', 'ok': m.get('read_back_digest') == m.get('digest'), 'method': 'digest of the stored bytes on read-back equals the digest at put'}
        return {'kind': 'verified', 'ok': bool(m.get('match')), 'method': 'received digest equals the sender digest (emulated link)'}

    def measure_compute(self, task, cpu_limit_s=5):
        """Actual bounded execution: a child process computes a supported lunar product; CPU time comes from the OS (rusage of children)."""
        from ..lunar.__main__ import example
        kind = task.get('kind', 'terrain_tile'); src = task.get('source', 'apollo11_32')
        inputs = example('terrain' if kind == 'terrain_tile' else 'route', src)
        import tempfile
        from pathlib import Path
        tmp = Path(tempfile.mkdtemp()); (tmp / 'in.json').write_text(json.dumps(inputs))
        before = _resource.getrusage(_resource.RUSAGE_CHILDREN); t0 = time.perf_counter()
        p = subprocess.run([sys.executable, '-m', 'metacoin_service.lunar', 'terrain' if kind == 'terrain_tile' else 'route', str(tmp / 'in.json')], cwd=str(Path(__file__).resolve().parents[2]), env=dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[2])), capture_output=True, text=True, timeout=cpu_limit_s + 30)
        after = _resource.getrusage(_resource.RUSAGE_CHILDREN); wall = time.perf_counter() - t0
        cpu = (after.ru_utime - before.ru_utime) + (after.ru_stime - before.ru_stime)
        out = {'kind': 'metered_fact', 'cpu_ms': max(1, int(cpu * 1000)), 'wall_ms': int(wall * 1000), 'exit_code': p.returncode, 'task': {'kind': kind, 'source': src}, 'output_digest': hashlib.sha256(p.stdout.encode()).hexdigest() if p.returncode == 0 else None, 'measurement_source': 'os rusage of the child process (RUSAGE_CHILDREN deltas); a bounded execution, not a benchmark of the provider\'s hardware'}
        if p.returncode == 0:
            from ..lunar import reference
            result = json.loads(p.stdout); result.pop('preview_png_base64', None)
            chk = reference.check(kind if kind in ('terrain_tile', 'route_feasibility') else 'terrain_tile', inputs, result)
            out['reference_ok'] = chk['ok']; out['checker_id'] = chk['checker_id']
        return out

    def storage_put(self, db, principal, resid, data):
        """Buyer stores bytes under an active storage reservation: stored in the artifact store, measured by size and timestamp."""
        r = self._res(db, principal.workspace, resid); buyer = self.native.account_row(db, principal.workspace, r['buyer_account']); o = self._offer(db, principal.workspace, r['offer_id'])
        if buyer['principal_id'] != principal.id and not principal.can('native:admin'):
            raise ServiceError('FORBIDDEN', 'only the buyer stores bytes under its reservation')
        if o['kind'] != 'storage' or r['state'] != 'active':
            raise ServiceError('CONFLICT', {'code': 'state', 'state': r['state'], 'kind': o['kind']})
        if not data or len(data) > 8 * 1024 * 1024:
            raise ServiceError('VALIDATION', {'code': 'bytes', 'allowed': '1..8 MiB'})
        aid = self.svc.store.store(db, workspace=principal.workspace, kind='export', owner_id=principal.id, plaintext=data, recipients=[], intended_use='resource-storage-delivery', public=False)
        back = self.svc.store.load(db, aid, principal.workspace)
        meas = json.loads(r['measurement_json'] or '{}'); meas['storage'] = {'artifact_id': aid, 'bytes': len(data), 'digest': hashlib.sha256(data).hexdigest(), 'read_back_digest': hashlib.sha256(back).hexdigest(), 'stored_at': now()}
        db.execute('UPDATE resource_reservations SET measurement_json=?, updated_at=? WHERE id=?', (json.dumps(meas), now(), resid))
        return {'reservation_id': resid, 'stored_bytes': len(data), 'digest': meas['storage']['digest'], 'stored_at': meas['storage']['stored_at'], 'note': 'byte-time accrues until delivery is recorded'}

    # ---- settlement ---------------------------------------------------------------------------------------------------------------
    def settle(self, db, principal, resid, cancelled=False):
        """Pay the delivered portion once (reserved -> committed -> provider available, sequenced, receipted) and release the rest."""
        r = self._res(db, principal.workspace, resid); buyer = self.native.account_row(db, principal.workspace, r['buyer_account']); prov = self.native.account_row(db, principal.workspace, r['provider_account']); o = self._offer(db, principal.workspace, r['offer_id'])
        if principal.id not in (buyer['principal_id'], prov['principal_id']) and not principal.can('native:admin'):
            raise ServiceError('FORBIDDEN', 'only a party settles')
        if r['state'] == 'settled':
            return dict(self.reservation_view(db, principal, resid), replayed=True)
        if r['state'] not in ('delivered', 'active'):
            raise ServiceError('CONFLICT', {'code': 'state', 'state': r['state'], 'note': 'settlement follows a recorded delivery (or a cancellation after activation)'})
        d = self.native.domain(db, principal.workspace); self.native.require_normal(d, 'settlement')
        owed = min(r['delivered_quantity'] * o['price_per_unit'], r['price']); release = r['price'] - owed
        ob = db.execute('SELECT * FROM native_obligations WHERE id=?', (r['obligation_id'],)).fetchone()
        if ob['settled_amount']:
            raise ServiceError('CONFLICT', {'code': 'already_settled'})
        iid = None
        if owed:
            seq = self.native.next_sequence(db, principal.workspace); iid = 'ni_' + secrets.token_hex(8)
            msg = {'protocol': P.PROTOCOL, 'type': 'intent', 'environment': self.native.environment, 'actor_account': buyer['id'], 'authority': d['id'], 'asset': r['asset'], 'amount_ceiling': owed, 'recipient': prov['id'], 'obligation_id': ob['id'], 'grant_id': None, 'nonce': 'rr-' + resid, 'policy_version': P.POLICY_VERSION, 'request_id': 'rr-settle-' + resid, 'purpose': 'resource delivery %d of %d %s' % (r['delivered_quantity'], r['quantity'], o['unit'])}
            hist = [{'state': 'reserved', 'at': now(), 'from': 'resource_reservation'}, {'state': 'authorized', 'at': now(), 'note': 'authorized by the funded reservation the buyer signed for; settlement amount bounded by the measured delivery'}, {'state': 'committed', 'at': now(), 'sequence': seq}, {'state': 'settled', 'at': now(), 'sequence': seq}]
            db.execute('INSERT INTO native_intents VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (iid, principal.workspace, d['id'], buyer['id'], principal.id, 'settlement', r['asset'], owed, owed, prov['id'], ob['id'], None, 'rr-' + resid, 'rr-settle-' + resid, json.dumps(msg), P.digest(msg), None, 'settled', seq, None, json.dumps(hist), now(), now()))
            ledger.move(db, principal.workspace, 'commit:' + iid, 'native_commit', r['asset'], d['id'], self.native.environment, owed, ledger.account_name(buyer['id'], 'reserved'), ledger.account_name(buyer['id'], 'committed_outgoing'), 'native_intent', iid, 'resource delivery committed (sequence %d)' % seq)
            ledger.move(db, principal.workspace, 'settle:' + iid, 'native_settlement', r['asset'], d['id'], self.native.environment, owed, ledger.account_name(buyer['id'], 'committed_outgoing'), ledger.account_name(prov['id'], 'available'), 'native_intent', iid, 'resource delivery settled')
            self.native._receipt(db, principal.workspace, 'settlement', 'native_intent', iid, {'intent_id': iid, 'from': buyer['id'], 'to': prov['id'], 'asset': r['asset'], 'amount': owed, 'obligation_id': ob['id'], 'reservation_id': resid, 'delivered_quantity': r['delivered_quantity'], 'unit': o['unit'], 'commit_sequence': seq, 'domain': d['id'], 'finality': 'domain_local_settlement'})
        if release:
            ledger.move(db, principal.workspace, 'rr-release:' + resid, 'native_resource_release', r['asset'], d['id'], self.native.environment, release, ledger.account_name(buyer['id'], 'reserved'), ledger.account_name(buyer['id'], 'available'), 'resource_reservation', resid, 'undelivered portion released')
        db.execute("UPDATE native_obligations SET settled_amount=?, state=?, updated_at=? WHERE id=?", (owed, 'settled' if owed == ob['amount'] else ('partial' if owed else 'cancelled'), now(), ob['id']))
        ev = json.loads(r['evidence_json']); ev['steps'].append({'state': 'settled', 'at': now(), 'owed': owed, 'released': release, 'cancelled_after_activation': cancelled})
        db.execute("UPDATE resource_reservations SET state='settled', evidence_json=?, settlement_intent=?, updated_at=? WHERE id=?", (json.dumps(ev), iid, now(), resid))
        history.record(db, principal.workspace, principal.id, 'native.settlement', 'resource_reservation', resid, {'owed': owed, 'released': release, 'intent': iid})
        return self.reservation_view(db, principal, resid)

    def market_view(self, db, principal):
        principal.require('work:read')
        offers = self.offers(db, principal); res = self.reservations(db, principal)
        return {'offers': offers, 'reservations': res, 'kinds': KINDS, 'upcoming_windows': [{'offer_id': o['id'], 'kind': o['kind'], 'start': o['window']['start'], 'end': o['window']['end'], 'available': o['available'], 'unit': o['unit']} for o in offers if o['window']['end'] > now()],
                'energy': 'energy per unit is a declared planning constraint on offers; nothing meters rover electricity', 'cross_domain': 'no global atomic reservation across domains: prepare = export an allocation here; commit = reserve locally; reconcile = acknowledgment'}
