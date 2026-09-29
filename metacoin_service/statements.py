"""Consolidated usage statements: work performed, reserved ceilings, assessed charges, unresolved actions and observed
settlement references for one workspace and interval, grouped by asset and network. Amounts of unlike denominations
are never summed. Synthetic (simulation / test-http) rows are labelled and kept apart from production configuration.
A statement is an application record with stable identifiers and reproducible rules; it is not proof of money
received unless a settlement reference is present and independently confirmed."""
import hashlib
import json

from experiments.private_receipts import receipt as merkle
from . import crypto, metering
from .db import now
from .errors import ServiceError

STATEMENT_SCHEMA = 'metacoin-usage-statement/v2'


def build(db, principal, settings, since=0, until=None, page=1, page_size=200):
    principal.require('statement:read')
    until = until or now()
    if type(since) is not int or type(until) is not int or since < 0 or until < since or type(page) is not int or page < 1:
        raise ServiceError('VALIDATION', 'since/until/page')
    ws = principal.workspace
    mode = settings.provider_mode
    env = 'synthetic-local' if mode in ('simulation', 'test-http') else 'production-configured'
    rows = []
    # 1. usage (assessed charges) per completed job under a quote
    for u in db.execute('SELECT * FROM usage_records WHERE workspace=? AND created_at>=? AND created_at<=? ORDER BY created_at, id', (ws, since, until)).fetchall():
        q = db.execute('SELECT * FROM quotes WHERE id=?', (u['quote_id'],)).fetchone()
        job = db.execute('SELECT kind, state, outcome, finished_at FROM jobs WHERE id=?', (u['job_id'],)).fetchone()
        sale = db.execute('SELECT state, transaction_ref, network FROM invoke_sales WHERE job_id=?', (u['job_id'],)).fetchone() or db.execute('SELECT state, transaction_ref, network, final_amount, max_amount FROM metered_settlements WHERE job_id=?', (u['job_id'],)).fetchone()
        rows.append({'row_id': u['id'], 'type': 'usage', 'at': u['created_at'], 'job_id': u['job_id'], 'kind': job['kind'] if job else None, 'service_id': u['service_id'], 'quote_id': u['quote_id'],
                     'unit': u['unit'], 'quantity': u['quantity'], 'amount_per_unit': u['amount_per_unit'], 'assessed_charge': u['assessed_charge'], 'asset': u['asset'],
                     'network': q['network'] if q else None, 'reserved_max': q['amount_max'] if q else None, 'provider_mode': q['provider_mode'] if q else mode,
                     'settlement': ({'state': sale['state'], 'reference': sale['transaction_ref'], **({'scheme': 'upto', 'settled_amount': sale['final_amount'], 'authorized_max': sale['max_amount']} if 'final_amount' in sale.keys() else {'scheme': 'exact'})} if sale else {'state': 'none', 'reference': None}), 'environment': env,
                     'rule': 'charge = min(quantity * amount_per_unit, reserved_max); quantity measured (tokens/items/work units) or 1 evaluation'})
    # 2. accepted quotes without usage yet (reserved ceilings)
    for q in db.execute("SELECT * FROM quotes WHERE workspace=? AND state IN ('accepted','consumed') AND created_at>=? AND created_at<=? ORDER BY created_at", (ws, since, until)).fetchall():
        if db.execute('SELECT 1 FROM usage_records WHERE quote_id=?', (q['id'],)).fetchone():
            continue
        job = db.execute('SELECT id, state FROM jobs WHERE quote_id=?', (q['id'],)).fetchone()
        rows.append({'row_id': q['id'], 'type': 'reserved_ceiling', 'at': q['created_at'], 'job_id': job['id'] if job else None, 'job_state': job['state'] if job else None, 'service_id': q['service_id'], 'quote_id': q['id'],
                     'unit': q['unit'], 'quantity': None, 'amount_per_unit': None, 'assessed_charge': 0, 'reserved_max': q['amount_max'], 'asset': q['asset'], 'network': q['network'], 'provider_mode': q['provider_mode'],
                     'settlement': {'state': 'not_applicable', 'reference': None}, 'environment': env, 'rule': 'ceiling reserved by an accepted quote; nothing assessed until the job completes'})
    # 3. sales of public bundles (customer buys result) and unresolved payment actions (agent buys)
    for s in db.execute('SELECT * FROM sales WHERE workspace=? AND created_at>=? AND created_at<=? ORDER BY created_at', (ws, since, until)).fetchall():
        rows.append({'row_id': s['payment_id'], 'type': 'bundle_sale', 'at': s['created_at'], 'job_id': s['job_id'], 'service_id': None, 'quote_id': None, 'unit': 'bundle', 'quantity': 1, 'amount_per_unit': int(s['amount']),
                     'assessed_charge': int(s['amount']), 'reserved_max': int(s['amount']), 'asset': s['asset'], 'network': s['network'], 'provider_mode': s['provider_mode'],
                     'settlement': {'state': s['state'], 'reference': s['transaction_ref']}, 'environment': env, 'rule': 'fixed price from the contract policy; settled through the x402 sale route'})
    for a in db.execute('SELECT * FROM payment_actions WHERE workspace=? AND created_at>=? AND created_at<=? ORDER BY created_at', (ws, since, until)).fetchall():
        req = json.loads(a['request_json'])
        rows.append({'row_id': a['request_id'], 'type': 'payment_action', 'at': a['created_at'], 'job_id': a['job_id'], 'service_id': None, 'quote_id': None, 'unit': 'action', 'quantity': 1, 'amount_per_unit': int(req.get('amount', 0)),
                     'assessed_charge': int(req.get('amount', 0)), 'reserved_max': int(req.get('amount', 0)), 'asset': req.get('asset'), 'network': req.get('network'), 'provider_mode': a['provider_mode'],
                     'settlement': {'state': 'see journal', 'reference': None}, 'environment': env, 'rule': 'agent-buys-compute action bound in the economic journal; provider state is per-journal'})
    rows.sort(key=lambda r: (r['at'], r['row_id']))
    total_rows = len(rows)
    start = (page - 1) * page_size
    page_rows = rows[start:start + page_size]
    groups = {}
    for r in rows:
        key = '%s @ %s [%s]' % (r['asset'], r['network'], r['environment'])
        g = groups.setdefault(key, {'asset': r['asset'], 'network': r['network'], 'environment': r['environment'], 'assessed_total': 0, 'reserved_total': 0, 'settled_confirmed_total': 0, 'rows': 0})
        g['rows'] += 1
        g['assessed_total'] += r['assessed_charge'] or 0
        g['reserved_total'] += r['reserved_max'] or 0
        if r['settlement']['state'] == 'CONFIRMED':
            g['settled_confirmed_total'] += r['assessed_charge'] or 0
    statement = {'schema': STATEMENT_SCHEMA, 'workspace': ws, 'issued_to': principal.id, 'interval': {'since': since, 'until': until}, 'page': page, 'page_size': page_size, 'total_rows': total_rows,
                 'provider_mode': mode, 'environment': env, 'groups': groups, 'rows': page_rows, 'issued_at': now(),
                 'meaning': 'application record of work, ceilings and assessed charges; settlement references are the only evidence of movement of funds; synthetic environments are never revenue',
                 'rules': {'usage': 'one record per completed job under a quote (UNIQUE on job); repeated viewing never creates a charge', 'grouping': 'by asset, network and environment; unlike denominations are never summed'}}
    pub = metering.ensure_service_key(settings, db)
    statement['issuer_key_id'] = crypto.key_id_for(pub)
    msg = merkle.canonical(statement)
    return {'statement': statement, 'signature_hex': crypto.sign(crypto.load_signing_key(settings.keys_dir / 'service.ed25519'), msg), 'public_key_hex': pub, 'digest': hashlib.sha256(msg).hexdigest()}


def export_csv(bundle):
    import csv, io
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(['row_id', 'type', 'at', 'job_id', 'kind', 'service_id', 'quote_id', 'unit', 'quantity', 'amount_per_unit', 'assessed_charge', 'reserved_max', 'asset', 'network', 'environment', 'settlement_state', 'settlement_reference'])
    for r in bundle['statement']['rows']:
        w.writerow([r['row_id'], r['type'], r['at'], r['job_id'], r.get('kind'), r['service_id'], r['quote_id'], r['unit'], r['quantity'], r['amount_per_unit'], r['assessed_charge'], r['reserved_max'], r['asset'], r['network'], r['environment'],
                    r['settlement']['state'], r['settlement']['reference']])
    return out.getvalue()
