"""Metered usage: one usage record per completed billable job (UNIQUE on job id), signed by the
service's own Ed25519 key. Billable unit = a completed evaluation; failed/partial work is never billed
(the policy is fixed in the quote before execution). States kept separate: estimated (quote),
reserved maximum (accepted quote), recorded usage (this record), assessed charge (this record's
amount), provider settlement (the sale row, if any). A usage record is not evidence that money moved.
"""
import hashlib
import json
import secrets
from experiments.private_receipts import receipt as merkle
from . import crypto, history
from .datasets import add_edge
from .db import now
from .errors import ServiceError


def ensure_service_key(settings, db):
    path = settings.keys_dir / 'service.ed25519'
    row = db.execute("SELECT value FROM meta WHERE key='service_signing_public'").fetchone()
    if row is None:
        if not path.exists():
            pub = crypto.generate_signing_key(path)
        else:
            from cryptography.hazmat.primitives import serialization
            pub = crypto.load_signing_key(path).public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
        db.execute("INSERT INTO meta VALUES ('service_signing_public', ?)", (pub,))
        return pub
    return row['value']


def record_for_job(settings, db, job_row):
    """Called when a job reaches `succeeded`. Idempotent: a second call finds the UNIQUE record."""
    if not job_row['quote_id']:
        return None
    if db.execute('SELECT id FROM usage_records WHERE job_id=?', (job_row['id'],)).fetchone():
        return None
    quote = db.execute('SELECT * FROM quotes WHERE id=?', (job_row['quote_id'],)).fetchone()
    reused = bool(job_row['reused_from']) if 'reused_from' in job_row.keys() else False
    quantity = 0 if reused else 1                          # one completed evaluation; a reused result computed nothing
    crun = db.execute('SELECT work_committed FROM compute_runs WHERE job_id=?', (job_row['id'],)).fetchone()
    if crun and not reused:
        quantity = min(crun['work_committed'], quote['quantity_max'])   # committed logical work units only; retries never add units
    mreq = db.execute('SELECT output_tokens, items, kind FROM model_requests WHERE job_id=?', (job_row['id'],)).fetchone()
    if mreq and not reused:
        measured = mreq['output_tokens'] if mreq['kind'] == 'text_generation' else mreq['items']
        quantity = min(measured or 0, quote['quantity_max'])            # measured by the runtime tokenizer; the quote only reserved the allowance
    charge = quantity * quote['amount_max'] // quote['quantity_max'] if quote['quantity_max'] else 0
    charge = min(charge, quote['amount_max'])
    pub = ensure_service_key(settings, db)
    key_id = crypto.key_id_for(pub)
    uid = 'u_' + secrets.token_hex(8)
    statement = {'schema': 'metacoin-usage-statement/v1', 'usage_id': uid, 'workspace': job_row['workspace'], 'job_id': job_row['id'],
                 'quote_id': quote['id'], 'service_id': quote['service_id'], 'service_revision': quote['service_revision'],
                 'pricing_revision': quote['pricing_revision'], 'unit': quote['unit'], 'quantity': quantity,
                 'amount_per_unit': quote['amount_max'] // quote['quantity_max'], 'assessed_charge': charge, 'asset': quote['asset'],
                 'network': quote['network'], 'evidence_root': job_row['evidence_root'], 'request_digest': quote['request_digest'],
                 'rounding': 'integer base units; floor', 'issued_at': now(), 'issuer_key_id': key_id,
                 'meaning': 'assessed charge for one completed evaluation; not settlement; the signature identifies the issuer, not resource truth'}
    message = merkle.canonical(statement)
    signature = crypto.sign(crypto.load_signing_key(settings.keys_dir / 'service.ed25519'), message)
    calc = {'quantity': quantity, 'amount_per_unit': statement['amount_per_unit'], 'cap': quote['amount_max'], 'formula': 'min(quantity*amount_per_unit, cap)',
            'reused_from': job_row['reused_from'] if reused else None}
    db.execute('INSERT INTO usage_records VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
               (uid, job_row['workspace'], job_row['id'], quote['id'], quote['service_id'], quote['pricing_revision'], quote['unit'], quantity,
                statement['amount_per_unit'], charge, quote['asset'], json.dumps(calc), 'assessed', message.decode(), signature, key_id, now()))
    history.record(db, job_row['workspace'], 'metering', 'sale.requested', 'usage', uid, {'job_id': job_row['id'], 'quote_id': quote['id'], 'assessed_charge': charge, 'unit': quote['unit']})
    add_edge(db, job_row['workspace'], 'job', job_row['id'], 'usage', uid, 'metered')
    return uid


def view(db, row):
    statement = merkle.parse(row['statement_json'])
    pub = db.execute("SELECT value FROM meta WHERE key='service_signing_public'").fetchone()
    valid = crypto.verify(pub['value'], row['statement_json'].encode(), row['signature_hex']) if pub else False
    sale = db.execute('SELECT state, transaction_ref FROM invoke_sales WHERE job_id=?', (row['job_id'],)).fetchone() or db.execute("SELECT state, transaction_ref, 'upto' AS scheme, final_amount, max_amount FROM metered_settlements WHERE job_id=?", (row['job_id'],)).fetchone()
    return {'usage_id': row['id'], 'job_id': row['job_id'], 'quote_id': row['quote_id'], 'service_id': row['service_id'], 'unit': row['unit'],
            'quantity': row['quantity'], 'amount_per_unit': row['amount_per_unit'], 'assessed_charge': row['assessed_charge'], 'asset': row['asset'],
            'state': row['state'], 'calculation': json.loads(row['calculation_json']), 'statement': statement, 'signature_hex': row['signature_hex'],
            'key_id': row['key_id'], 'signature_valid': valid,
            'states': {'estimated': 'quote', 'reserved_max': 'accepted quote amount_max', 'recorded_usage': 'this record', 'assessed_charge': row['assessed_charge'],
                       'provider_settlement': dict(sale) if sale else 'none recorded'},
            'independently_recomputable': 'quantity = completed evaluations (1), committed compute work units, or measured model tokens/items, capped by the quote; charge = min(quantity*amount_per_unit, cap)'}
