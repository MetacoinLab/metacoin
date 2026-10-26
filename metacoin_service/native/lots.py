"""Claim lots: explicit, non-fungible accounting of imported backing (order 2026-10-23 group G).

Every unit that enters a domain through an imported allocation is a *lot*: origin allocation, parent lot, asset, amount, current
owner, status. Lots are never destroyed: they split when part of a lot is committed, transferred or retired, so at all times the
lots of one origin allocation partition its amount (invariant published by `invariants()` and recomputed independently by the
checker from the export). Statuses: `active` (owned, spendable), `committed` (reserved / in flight inside the owner's account),
`retired` (irrevocably removed from spendability at this domain for a return), `spent` (left this domain by a re-export).
A lot transferred to another account keeps its origin and is marked `transferred`: it stays backed by the source but is not
retirable by the new owner (a return credits the exporter's source account, so only the original recipient may retire).
Spending consumes unlineaged units first, then lots oldest-first (conservative: a reservation commits lots even if later released;
releases restore the oldest committed lots). The hook runs inside every ledger move between account categories, in the same
transaction as the money. Growth is bounded: lots per account are capped and consolidated (same origin, owner, status) on demand."""
import json
import secrets

from ..db import now
from ..errors import ServiceError

MAX_LOTS_PER_ACCOUNT = 4000
CONSOLIDATE_ABOVE = 1000
SPENDING_CATEGORIES = ('reserved', 'committed_outgoing', 'in_transit')


def _parse(name):
    if name.startswith('acct:'):
        _, aid, cat = name.split(':', 2); return aid, cat
    return None, name


def _active(db, ws, account, asset):
    return db.execute("SELECT * FROM native_claim_lots WHERE workspace=? AND owner_account=? AND asset=? AND status='active' ORDER BY created_sequence, rowid", (ws, account, asset)).fetchall()


def _new(db, ws, origin, parent, asset, amount, owner, status, transferred, seq, ref=None):
    lid = 'lot_' + secrets.token_hex(6); t = now()
    db.execute('INSERT INTO native_claim_lots VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)', (lid, ws, origin, parent, asset, amount, owner, status, int(transferred), seq, json.dumps(ref) if ref else None, t, t))
    return lid


def _set(db, lid, status, **kw):
    db.execute('UPDATE native_claim_lots SET status=?, updated_at=? WHERE lot_id=?', (status, now(), lid))


def _take(db, ws, rows, amount, new_status, seq, ref, transferred_to=None):
    """Split/consume `amount` from the given ordered lots. Returns the list of (lot_id, amount) that changed status (new ids when split)."""
    out = []; remaining = amount
    for r in rows:
        if remaining <= 0:
            break
        take = min(remaining, r['amount'])
        if take == r['amount']:
            if transferred_to:                                   # the whole lot changes owner: same identity, new owner, nonretirable for it
                db.execute("UPDATE native_claim_lots SET owner_account=?, status='active', transferred=1, ref_json=?, updated_at=? WHERE lot_id=?", (transferred_to, json.dumps(dict(ref, from_owner=r['owner_account'])), now(), r['lot_id'])); out.append((r['lot_id'], take))
            else:
                _set(db, r['lot_id'], new_status); out.append((r['lot_id'], take))
        else:
            # split: the remainder stays as the original lot (amount reduced), the taken part becomes a child lot
            db.execute('UPDATE native_claim_lots SET amount=?, updated_at=? WHERE lot_id=?', (r['amount'] - take, now(), r['lot_id']))
            keep = _new(db, ws, r['origin_allocation'], r['lot_id'], r['asset'], take, transferred_to or r['owner_account'], 'active' if transferred_to else new_status, bool(transferred_to) or bool(r['transferred']), seq, ref)
            out.append((keep, take))
        remaining -= take
    return out, remaining


def on_move(db, workspace, kind, asset, domain, amount, src, dst, ref_type, ref_id, seq_fn):
    """Ledger hook (same transaction). src/dst are journal account names."""
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='native_claim_lots'").fetchone():
        return
    sa, sc = _parse(src); da, dc = _parse(dst); ref = {'kind': kind, 'ref': [ref_type, ref_id]}
    if kind in ('native_return_initiation', 'native_claim_burn'):
        return                                                                      # retirement is explicit (retire / on_return_certificate)
    if sc.startswith('backing:claim:') and da and dc == 'available':                 # import: a new lot of exact provenance
        _new(db, workspace, sc.split(':', 2)[2], None, asset, amount, da, 'active', False, seq_fn(), ref); return
    if sa and da and sa == da:
        if sc == 'available' and dc in SPENDING_CATEGORIES:
            from . import ledger
            avail_after = ledger.balance(db, workspace, asset, domain, sa, 'available'); rows = _active(db, workspace, sa, asset)
            lineaged = sum(r['amount'] for r in rows); unlineaged = max(avail_after + amount - lineaged, 0); need = max(amount - unlineaged, 0)
            if need:
                _take(db, workspace, rows, need, 'committed', seq_fn(), ref)
            _bound(db, workspace, sa)
        elif sc in SPENDING_CATEGORIES and dc == 'available':                       # release: restore the oldest committed lots
            rows = db.execute("SELECT * FROM native_claim_lots WHERE workspace=? AND owner_account=? AND asset=? AND status='committed' ORDER BY created_sequence, rowid", (workspace, sa, asset)).fetchall()
            _take(db, workspace, rows, amount, 'active', seq_fn(), ref)
        return
    if sa and da and sa != da and dc == 'available' and sc in SPENDING_CATEGORIES:   # settlement / transfer: lots change owner, origin kept, nonretirable for the new owner
        rows = db.execute("SELECT * FROM native_claim_lots WHERE workspace=? AND owner_account=? AND asset=? AND status='committed' ORDER BY created_sequence, rowid", (workspace, sa, asset)).fetchall()
        _take(db, workspace, rows, amount, 'spent', seq_fn(), ref, transferred_to=da); _bound(db, workspace, da); return
    if sa and sc == 'in_transit' and dc.startswith('backing:exported:'):            # re-export from this domain: the lots leave
        rows = db.execute("SELECT * FROM native_claim_lots WHERE workspace=? AND owner_account=? AND asset=? AND status='committed' ORDER BY created_sequence, rowid", (workspace, sa, asset)).fetchall()
        _take(db, workspace, rows, amount, 'spent', seq_fn(), ref); return


def _bound(db, ws, account):
    n = db.execute("SELECT COUNT(*) FROM native_claim_lots WHERE workspace=? AND owner_account=? AND status IN ('active','committed')", (ws, account)).fetchone()[0]
    if n > CONSOLIDATE_ABOVE:
        consolidate(db, ws, account)
        n = db.execute("SELECT COUNT(*) FROM native_claim_lots WHERE workspace=? AND owner_account=? AND status IN ('active','committed')", (ws, account)).fetchone()[0]
    if n > MAX_LOTS_PER_ACCOUNT:
        raise ServiceError('BUDGET_EXHAUSTED', {'code': 'lineage_limit', 'lots': n, 'max': MAX_LOTS_PER_ACCOUNT, 'note': 'bounded lineage growth: consolidate or retire lots before more lineaged spending'})


def consolidate(db, ws, account):
    """Merge active lots of the same origin, owner, status and transfer flag into one (lineage recorded in merged_from)."""
    groups = {}
    for r in db.execute("SELECT * FROM native_claim_lots WHERE workspace=? AND owner_account=? AND status IN ('active','committed') ORDER BY created_sequence, rowid", (ws, account)).fetchall():
        groups.setdefault((r['origin_allocation'], r['asset'], r['status'], r['transferred']), []).append(r)
    merged = 0
    for (origin, asset, status, tr), rows in groups.items():
        if len(rows) < 2:
            continue
        keep = rows[0]; total = sum(r['amount'] for r in rows); ids = [r['lot_id'] for r in rows[1:]]
        db.execute('UPDATE native_claim_lots SET amount=?, ref_json=?, updated_at=? WHERE lot_id=?', (total, json.dumps({'merged_from': ids}), now(), keep['lot_id']))
        for r in rows[1:]:
            db.execute("UPDATE native_claim_lots SET status='merged', amount=0, updated_at=? WHERE lot_id=?", (now(), r['lot_id']))
        merged += len(ids)
    return merged


def retirable(db, ws, account, allocation_id):
    a = db.execute('SELECT recipient_account, asset, amount FROM native_allocations WHERE id=?', (allocation_id,)).fetchone()
    if a is None or a['recipient_account'] != account:
        return 0, []
    rows = db.execute("SELECT * FROM native_claim_lots WHERE workspace=? AND owner_account=? AND origin_allocation=? AND status='active' AND transferred=0 ORDER BY created_sequence, rowid", (ws, account, allocation_id)).fetchall()
    return sum(r['amount'] for r in rows), [r['lot_id'] for r in rows]


def retire(db, ws, account, allocation_id, amount, seq):
    total, _ = retirable(db, ws, account, allocation_id)
    if amount > total:
        raise ServiceError('CONFLICT', {'code': 'retire_exceeds_owned', 'retirable': total, 'requested': amount, 'note': 'only lots still owned by the original recipient of this allocation can be retired; transferred lots belong to their new owner and stay backed'})
    rows = db.execute("SELECT * FROM native_claim_lots WHERE workspace=? AND owner_account=? AND origin_allocation=? AND status='active' AND transferred=0 ORDER BY created_sequence, rowid", (ws, account, allocation_id)).fetchall()
    out, rem = _take(db, ws, rows, amount, 'retired', seq, {'kind': 'retirement', 'allocation': allocation_id})
    return [lid for lid, _ in out]


def summary(db, ws, account, asset=None):
    rows = [dict(r) for r in db.execute('SELECT * FROM native_claim_lots WHERE workspace=? AND owner_account=? ' + ('AND asset=? ' if asset else '') + 'ORDER BY created_sequence, rowid', (ws, account) + ((asset,) if asset else ())).fetchall()]
    by_origin = {}
    for r in rows:
        o = by_origin.setdefault(r['origin_allocation'], {'active': 0, 'committed': 0, 'retired': 0, 'spent': 0, 'transferred_in': 0, 'retirable': 0, 'lots': 0})
        if r['status'] in o:
            o[r['status']] += r['amount']
        if r['transferred'] and r['status'] == 'active':
            o['transferred_in'] += r['amount']
        if r['status'] == 'active' and not r['transferred']:
            o['retirable'] += r['amount']
        o['lots'] += 1
    for o in by_origin.values():
        a = db.execute('SELECT recipient_account FROM native_allocations WHERE id=?', (next(k for k, v in by_origin.items() if v is o),)).fetchone()
        o['original_recipient_here'] = bool(a and a['recipient_account'] == account)
        if not o['original_recipient_here']:
            o['retirable'] = 0
    return {'account': account, 'lots': [{k: r[k] for k in ('lot_id', 'origin_allocation', 'parent_lot', 'asset', 'amount', 'status', 'transferred', 'created_sequence')} for r in rows if r['status'] != 'merged'], 'by_origin': by_origin, 'retirable_total': sum(o['retirable'] for o in by_origin.values()),
            'rule': 'spending consumes unlineaged units first, then lots oldest-first; a lot transferred to another owner stays backed but is retirable only by the original recipient; a reservation commits lots conservatively (released lots return to active)'}


def invariants(db, ws, allocation_id):
    a = db.execute('SELECT * FROM native_allocations WHERE id=?', (allocation_id,)).fetchone()
    if a is None:
        return None
    rows = db.execute("SELECT status, SUM(amount) s FROM native_claim_lots WHERE workspace=? AND origin_allocation=? AND status<>'merged' GROUP BY status", (ws, allocation_id)).fetchall(); by = {r['status']: r['s'] for r in rows}
    partition = sum(by.values()); returned = a['returned_amount'] if 'returned_amount' in a.keys() else 0
    legacy = not partition and a['direction'] == 'import' and a['state'] in ('imported', 'acknowledged', 'return_requested', 'returned')   # imported before claim lineage existed and (partly) consumed then: no lots are invented for it
    if legacy:
        return {'allocation': allocation_id, 'amount': a['amount'], 'lots_by_status': {}, 'partition_sum': 0, 'partition_ok': None, 'legacy_without_lineage': True, 'returned_amount': returned, 'still_backed': a['amount'] - returned, 'backed_matches_lots': None,
                'rule': 'historical allocation consumed before claim lineage existed: no lots are invented for it; only a full return of its remaining backing is possible, never a partial retirement'}
    return {'allocation': allocation_id, 'amount': a['amount'], 'lots_by_status': by, 'partition_sum': partition, 'partition_ok': partition == a['amount'] if partition else a['direction'] == 'export' or a['state'] not in ('imported', 'acknowledged', 'return_requested', 'returned'),
            'returned_amount': returned, 'still_backed': a['amount'] - returned, 'backed_matches_lots': (by.get('active', 0) + by.get('committed', 0) + by.get('spent', 0)) == a['amount'] - returned if partition else None,
            'rule': 'lots of one origin partition its amount; retired lots correspond to returns (pending or completed); active + committed + spent = still backed'}
