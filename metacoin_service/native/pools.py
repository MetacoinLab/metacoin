"""Canonical resource pools with interval-aware capacity accounting (order 2026-10-23 group C).

A pool is the authoritative capacity of one physical resource of a provider: a versioned schedule of half-open intervals
[start, end) with an integer capacity in *slots* (concurrent units: CPU slots, KiB of storage, contact channels), and a
declared conversion `units_per_slot_second` from one slot-second to the offer unit (compute: cpu_ms per slot-second, storage:
kib_seconds per KiB-slot-second = 1, contact: KiB per channel-second). Every offer references a pool; aliases and overlapping
offer windows draw from the same pool. A reservation commits `slots` over its own half-open interval [a, b) (aligned to the
pool resolution: a floored, b ceiled), where slots = ceil(quantity / (units_per_slot_second × (b − a))). Two reservations
overlap only if their intervals intersect as half-open sets: [0, 30) and [30, 60) touch and do not conflict.

The admission rule is an exact interval sweep: at every point of [a, b), committed slots + requested slots ≤ scheduled
capacity. Capacity reductions that would put any committed interval above the new capacity are refused (affected
commitments listed); delivered capacity is never released; releasing a future unused slot frees only the part after the
release point. `sweep()` is pure and is the function an independent oracle can recompute from exported commitments."""
import json
import secrets

from .. import history
from ..db import now
from ..errors import ServiceError

MAX_SCHEDULE = 256
MAX_COMMIT_SECONDS = 365 * 86400
STATES = ('open', 'draining', 'closed')
COMMITTED = ('reserved', 'active', 'delivered')         # commitment states that occupy capacity; 'released' does not


def _int(v, lo, hi, name):
    if type(v) is bool or type(v) is not int or not lo <= v <= hi:
        raise ServiceError('VALIDATION', {'code': 'bounded_integer', 'field': name, 'min': lo, 'max': hi})
    return v


def align(start, end, resolution):
    """[start, end) aligned outward to the pool resolution (start floored, end ceiled); end > start enforced."""
    s = (start // resolution) * resolution; e = -(-end // resolution) * resolution
    if e <= s:
        raise ServiceError('VALIDATION', {'code': 'interval', 'note': 'end must be after start at the pool resolution'})
    return s, e


def slots_for(quantity, units_per_slot_second, start, end):
    """Exact ceiling division: the smallest number of slots whose slot-seconds over [start, end) cover the quantity."""
    seconds = end - start; per_slot = units_per_slot_second * seconds
    if per_slot <= 0:
        raise ServiceError('VALIDATION', {'code': 'interval'})
    return -(-quantity // per_slot)


def validate_schedule(schedule):
    if type(schedule) is not list or not 1 <= len(schedule) <= MAX_SCHEDULE:
        raise ServiceError('VALIDATION', {'code': 'schedule', 'allowed': 'list of {start, end, capacity} (1..%d)' % MAX_SCHEDULE})
    out = []
    for i, e in enumerate(schedule):
        if type(e) is not dict or set(e) - {'start', 'end', 'capacity'}:
            raise ServiceError('VALIDATION', {'code': 'schedule_entry', 'index': i})
        s, en, c = _int(e.get('start'), 0, 10 ** 12, 'start'), _int(e.get('end'), 0, 10 ** 12, 'end'), _int(e.get('capacity'), 0, 10 ** 9, 'capacity')
        if en <= s:
            raise ServiceError('VALIDATION', {'code': 'schedule_entry', 'index': i, 'note': 'end must follow start'})
        out.append({'start': s, 'end': en, 'capacity': c})
    out.sort(key=lambda x: x['start'])
    for a, b in zip(out, out[1:]):
        if b['start'] < a['end']:
            raise ServiceError('VALIDATION', {'code': 'schedule_overlap', 'note': 'schedule intervals must not overlap; split them instead'})
    return out


def capacity_at(schedule, t):
    for e in schedule:
        if e['start'] <= t < e['end']:
            return e['capacity']
    return 0


def sweep(schedule, commitments, start=None, end=None):
    """Pure interval sweep. commitments: iterable of {start, end, slots} (half-open). Returns the list of elementary segments
    {start, end, used, capacity, ok} covering the union of all interval boundaries (optionally clipped to [start, end))."""
    points = set()
    for e in schedule:
        points.update((e['start'], e['end']))
    for c in commitments:
        points.update((c['start'], c['end']))
    if start is not None:
        points.add(start)
    if end is not None:
        points.add(end)
    pts = sorted(points); segs = []
    for a, b in zip(pts, pts[1:]):
        if start is not None and b <= start or end is not None and a >= end:
            continue
        used = sum(c['slots'] for c in commitments if c['start'] < b and c['end'] > a)
        cap = capacity_at(schedule, a); segs.append({'start': a, 'end': b, 'used': used, 'capacity': cap, 'ok': used <= cap})
    return segs


def admissible(schedule, commitments, start, end, slots):
    """True when adding `slots` over [start, end) keeps every point within capacity; otherwise the first violating segment."""
    for seg in sweep(schedule, list(commitments) + [{'start': start, 'end': end, 'slots': slots}], start, end):
        if not seg['ok']:
            return False, seg
    return True, None


class Pools:
    def __init__(self, settings, services):
        self.settings, self.svc = settings, services

    @property
    def native(self):
        return self.svc.native

    # ---- lifecycle ------------------------------------------------------------------------------------------------------------------
    def create(self, db, principal, body):
        principal.require('work:offer')
        if type(body) is not dict or set(body) - {'kind', 'label', 'slot_unit', 'units_per_slot_second', 'resolution_seconds', 'schedule', 'note'}:
            raise ServiceError('VALIDATION', {'code': 'fields', 'allowed': ['kind (compute|storage|contact)', 'label', 'slot_unit', 'units_per_slot_second', 'resolution_seconds?', 'schedule [{start,end,capacity}]', 'note?']})
        from .market import KINDS
        if body.get('kind') not in KINDS:
            raise ServiceError('VALIDATION', {'code': 'kind', 'allowed': list(KINDS)})
        acct = self.native.account_of(db, principal)
        label = body.get('label'); unit = body.get('slot_unit')
        if type(label) is not str or not 1 <= len(label) <= 64 or type(unit) is not str or not 1 <= len(unit) <= 32:
            raise ServiceError('VALIDATION', {'code': 'label_or_slot_unit'})
        u = _int(body.get('units_per_slot_second'), 1, 10 ** 9, 'units_per_slot_second'); res = _int(body.get('resolution_seconds', 1), 1, 86400, 'resolution_seconds')
        sched = validate_schedule(body.get('schedule')); pid = 'rp_' + secrets.token_hex(6); t = now()
        db.execute('INSERT INTO resource_pools VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (pid, principal.workspace, acct['id'], principal.id, body['kind'], label, unit, u, res, json.dumps(sched), 1, 'open', t, t))
        history.record(db, principal.workspace, principal.id, 'native.obligation', 'resource_pool', pid, {'kind': body['kind'], 'label': label, 'schedule': sched, 'units_per_slot_second': u})
        return self.view(db, principal, pid)

    def implicit_for_offer(self, db, principal, acct, kind, capacity, window_start, window_end, unit):
        """A pool for an offer published without one: one slot whose slot-seconds over the window cover the offer capacity exactly
        enough (ceil). The offer's own total-quantity bound stays authoritative for units; the pool bounds overlap in time."""
        # legacy semantics kept exactly: the offer's total quantity over its window is the authority; the implicit pool models the
        # offer as `capacity` unit-slots each yielding one unit per second, so any interval division of the window is admitted up to
        # the offer total (the offer check bounds the sum of units; the pool bounds concurrency in time to the same total)
        pid = 'rp_' + secrets.token_hex(6); t = now()
        db.execute('INSERT INTO resource_pools VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (pid, principal.workspace, acct['id'], principal.id, kind, 'implicit pool of one offer', unit + '_slot', 1, 1, json.dumps([{'start': window_start, 'end': window_end, 'capacity': capacity}]), 1, 'open', t, t))
        return pid

    def _row(self, db, workspace, pid):
        r = db.execute('SELECT * FROM resource_pools WHERE id=? AND workspace=?', (pid, workspace)).fetchone()
        if r is None:
            raise ServiceError('NOT_FOUND', 'resource pool')
        return r

    def commitments(self, db, pid, states=COMMITTED):
        q = 'SELECT * FROM pool_commitments WHERE pool_id=? AND state IN (%s) ORDER BY start' % ','.join('?' * len(states))
        return [dict(r) for r in db.execute(q, (pid, *states)).fetchall()]

    def amend_schedule(self, db, principal, pid, body):
        """A new schedule version. Reductions below any committed interval are refused with the affected commitments: capacity changes
        never retroactively invalidate delivered or reserved obligations (affected-contract policy: refuse)."""
        principal.require('work:offer'); p = self._row(db, principal.workspace, pid)
        if p['provider_principal'] != principal.id:
            raise ServiceError('FORBIDDEN', 'only the provider amends its pool')
        sched = validate_schedule((body or {}).get('schedule')); commits = self.commitments(db, pid)
        bad = [s for s in sweep(sched, commits) if not s['ok']]
        if bad:
            affected = sorted({c['reservation_id'] for c in commits for s in bad if c['start'] < s['end'] and c['end'] > s['start']})
            raise ServiceError('CONFLICT', {'code': 'capacity_below_commitments', 'segments': bad[:5], 'affected_reservations': affected, 'policy': 'refuse: committed and delivered capacity cannot be reduced; cancel or settle the affected reservations first (explicit affected-contract policy)'})
        db.execute('UPDATE resource_pools SET schedule_json=?, version=version+1, updated_at=? WHERE id=?', (json.dumps(sched), now(), pid))
        history.record(db, principal.workspace, principal.id, 'native.obligation', 'resource_pool', pid, {'amended_schedule': sched, 'version': p['version'] + 1})
        return self.view(db, principal, pid)

    def set_state(self, db, principal, pid, state):
        principal.require('work:offer'); p = self._row(db, principal.workspace, pid)
        if p['provider_principal'] != principal.id and not principal.can('native:admin'):
            raise ServiceError('FORBIDDEN', 'only the provider drains or closes its pool')
        if state not in ('draining', 'closed', 'open'):
            raise ServiceError('VALIDATION', {'code': 'state', 'allowed': ['open', 'draining', 'closed']})
        if state == 'closed' and any(c['state'] in ('reserved', 'active') for c in self.commitments(db, pid)):
            raise ServiceError('CONFLICT', {'code': 'pool_has_live_commitments', 'note': 'drain first: a pool closes only when no reservation is reserved or active; delivered work and accepted obligations are never erased'})
        db.execute('UPDATE resource_pools SET state=?, updated_at=? WHERE id=?', (state, now(), pid))
        history.record(db, principal.workspace, principal.id, 'native.obligation', 'resource_pool', pid, {'state': state})
        return self.view(db, principal, pid)

    # ---- commitments (called inside the market's reservation transaction) ---------------------------------------------------------
    def commit(self, db, workspace, pid, reservation_id, start, end, quantity):
        """Admit a reservation's interval into the pool or refuse with the first violating segment. Same transaction as the money."""
        p = self._row(db, workspace, pid)
        if p['state'] != 'open':
            raise ServiceError('CONFLICT', {'code': 'provider_draining' if p['state'] == 'draining' else 'pool_closed', 'pool': pid, 'note': 'no new reservations; existing commitments continue to delivery and settlement'})
        s, e = align(start, end, p['resolution_seconds'])
        if e - s > MAX_COMMIT_SECONDS:
            raise ServiceError('VALIDATION', {'code': 'interval_too_long'})
        slots = slots_for(quantity, p['units_per_slot_second'], s, e); sched = json.loads(p['schedule_json'])
        ok, seg = admissible(sched, self.commitments(db, pid), s, e, slots)
        if not ok:
            raise ServiceError('CONFLICT', {'code': 'pool_capacity_exceeded', 'pool': pid, 'interval': [s, e], 'slots_requested': slots, 'segment': seg, 'note': 'the shared pool cannot sell the same slot twice: another offer or reservation already commits it over this interval'})
        cid = 'pc_' + secrets.token_hex(6); t = now()
        db.execute('INSERT INTO pool_commitments VALUES (?,?,?,?,?,?,?,?,?,?)', (cid, workspace, pid, reservation_id, s, e, slots, 'reserved', t, t))
        return {'commitment_id': cid, 'pool_id': pid, 'interval': [s, e], 'slots': slots, 'units_per_slot_second': p['units_per_slot_second'], 'pool_version': p['version']}

    def set_commitment(self, db, reservation_id, state, truncate_at=None):
        """reserved -> active -> delivered | released. Releasing after activation keeps the delivered part: the interval is truncated
        at the release point (aligned up) and stays committed; only the future unused tail is freed."""
        c = db.execute('SELECT * FROM pool_commitments WHERE reservation_id=?', (reservation_id,)).fetchone()
        if c is None:
            return None
        if state == 'released' and truncate_at is not None and c['state'] in ('active', 'delivered') and truncate_at > c['start']:
            p = db.execute('SELECT resolution_seconds FROM resource_pools WHERE id=?', (c['pool_id'],)).fetchone(); res = p['resolution_seconds']
            cut = min(c['end'], -(-truncate_at // res) * res)
            db.execute("UPDATE pool_commitments SET end=?, state='delivered', updated_at=? WHERE id=?", (cut, now(), c['id'])); return {'truncated_to': cut, 'kept': 'delivered part'}
        db.execute('UPDATE pool_commitments SET state=?, updated_at=? WHERE id=?', (state, now(), c['id'])); return {'state': state}

    # ---- views ------------------------------------------------------------------------------------------------------------------------
    def view(self, db, principal, pid):
        principal.require('work:read'); p = self._row(db, principal.workspace, pid); sched = json.loads(p['schedule_json']); commits = self.commitments(db, pid)
        offers = [dict(o) for o in db.execute('SELECT id, version, window_start, window_end, capacity, price_per_unit, state FROM resource_offers WHERE pool_id=? ORDER BY window_start', (pid,)).fetchall()]
        segs = sweep(sched, commits); peak = max((s['used'] for s in segs), default=0)
        return {'schema': 'metacoin-resource-pool/v1', 'id': pid, 'provider_account': p['provider_account'], 'kind': p['kind'], 'label': p['label'], 'slot_unit': p['slot_unit'], 'units_per_slot_second': p['units_per_slot_second'], 'resolution_seconds': p['resolution_seconds'], 'schedule': sched, 'version': p['version'], 'state': p['state'],
                'offers': offers, 'commitments': [{k: c[k] for k in ('id', 'reservation_id', 'start', 'end', 'slots', 'state')} for c in commits], 'utilization': segs, 'peak_slots_used': peak, 'all_within_capacity': all(s['ok'] for s in segs),
                'semantics': {'intervals': 'half-open [start, end); touching intervals do not conflict', 'alignment': 'start floored, end ceiled to resolution_seconds', 'slots': 'ceil(quantity / (units_per_slot_second × seconds))', 'reductions': 'refused when below any committed interval', 'release': 'only the future unused tail of a commitment is freed; delivered capacity stays committed'}, 'created_at': p['created_at']}

    def list(self, db, principal, account=None):
        principal.require('work:read')
        rows = db.execute('SELECT id FROM resource_pools WHERE workspace=?' + (' AND provider_account=?' if account else '') + ' ORDER BY created_at', (principal.workspace,) + ((account,) if account else ())).fetchall()
        return [self.view(db, principal, r['id']) for r in rows]
