"""Versioned pricing experiments (Order 08 §76.7): compare fixed (exact) and metered (upto) payment policies on a SYNTHETIC
workload of delivery outcomes using the same frozen payment rules the acceptance path applies. An experiment is a stored,
versioned computation over declared assumptions; it awards nothing, touches no quote, no terms revision and no journal entry.
Historical quotes and accounting stay exact: the experiment reads the catalog's current price identities only to record them."""
import hashlib
import json
import secrets

from .. import catalog, history
from ..db import now
from ..errors import ServiceError
from experiments.private_receipts.receipt import canonical

SCHEMA = 'metacoin-pricing-experiment/v1'
OUTCOMES = ('FEASIBLE', 'INFEASIBLE', 'INDETERMINATE', 'execution_failure', 'rejected_evidence')
MAX_WORKLOAD = 10_000


def _policy_pay(policy, rule, outcome, metered_bps):
    """What one policy pays for one delivery under the frozen payment rule: fixed pays the class amount; metered pays the
    class amount capped by the ceiling and scaled by the declared metered basis points (never above the class amount)."""
    if outcome in ('FEASIBLE', 'INFEASIBLE'):
        base = rule['complete']
    elif outcome == 'INDETERMINATE':
        base = rule['diagnostic']
    else:
        base = 0
    base = min(base, policy['ceiling'])
    if policy['scheme'] == 'exact':
        return base
    return min(base, (policy['ceiling'] * metered_bps) // 10000)


class Pricing:
    def __init__(self, settings, services):
        self.settings, self.svc = settings, services

    def create(self, db, principal, body):
        principal.require('work:request')
        if type(body) is not dict or set(body) - {'name', 'payment_rule', 'policies', 'workload', 'metered_bps', 'notes'}:
            raise ServiceError('VALIDATION', {'code': 'fields', 'allowed': ['name', 'payment_rule', 'policies', 'workload', 'metered_bps', 'notes']})
        rule = body.get('payment_rule') or {'complete': 10, 'partial': 0, 'diagnostic': 5}
        pols = body.get('policies') or [{'name': 'fixed', 'scheme': 'exact', 'ceiling': rule['complete']}, {'name': 'metered', 'scheme': 'upto', 'ceiling': rule['complete']}]
        wl = body.get('workload')
        bps = body.get('metered_bps', 10000)
        if type(rule) is not dict or any(type(rule.get(k)) is not int or rule.get(k) < 0 for k in ('complete', 'partial', 'diagnostic')):
            raise ServiceError('VALIDATION', {'code': 'payment_rule', 'keys': ['complete', 'partial', 'diagnostic'], 'integers': True})
        if type(pols) is not list or not 1 <= len(pols) <= 8 or any(type(p) is not dict or p.get('scheme') not in ('exact', 'upto') or type(p.get('ceiling')) is not int or p['ceiling'] <= 0 or type(p.get('name')) is not str for p in pols):
            raise ServiceError('VALIDATION', {'code': 'policies', 'shape': [{'name': 'str', 'scheme': 'exact|upto', 'ceiling': 'int > 0'}]})
        if type(wl) is not list or not 1 <= len(wl) <= MAX_WORKLOAD or any(x not in OUTCOMES for x in wl):
            raise ServiceError('VALIDATION', {'code': 'workload', 'outcomes': list(OUTCOMES), 'max': MAX_WORKLOAD})
        if type(bps) is not int or not 0 <= bps <= 10000:
            raise ServiceError('VALIDATION', {'code': 'metered_bps', 'range': '0..10000 basis points of the ceiling actually metered under a metered policy (an assumption, not a measurement; integers only)'})
        counts = {o: wl.count(o) for o in OUTCOMES}
        results = []
        for p in pols:
            per = {o: _policy_pay(p, rule, o, bps) for o in OUTCOMES}
            total = sum(per[o] * counts[o] for o in OUTCOMES)
            paid_for_valid = sum(per[o] * counts[o] for o in ('FEASIBLE', 'INFEASIBLE'))
            results.append({'policy': p['name'], 'scheme': p['scheme'], 'ceiling': p['ceiling'], 'per_outcome': per, 'total': total, 'paid_for_valid_answers': paid_for_valid, 'paid_for_negatives': per['INFEASIBLE'] * counts['INFEASIBLE'],
                            'paid_for_failures': per['execution_failure'] * counts['execution_failure'] + per['rejected_evidence'] * counts['rejected_evidence'], 'max_requester_exposure_per_delivery': p['ceiling']})
        quotes = {k: hashlib.sha256(json.dumps(v.get('pricing', v.get('price_unit', v)) if isinstance(v, dict) else v, sort_keys=True, default=str).encode()).hexdigest()[:16] for k, v in sorted(catalog.INSTALLED.items())} if isinstance(catalog.INSTALLED, dict) else {}
        eid = 'wpx_' + secrets.token_hex(6)
        version = (db.execute('SELECT COALESCE(MAX(version),0) FROM pricing_experiments WHERE workspace=? AND name=?', (principal.workspace, body.get('name') or 'experiment')).fetchone()[0] or 0) + 1
        rec = {'schema': SCHEMA, 'id': eid, 'name': body.get('name') or 'experiment', 'version': version, 'assumptions': {'payment_rule': rule, 'policies': pols, 'workload_counts': counts, 'metered_bps': bps, 'note': 'synthetic outcome mix and metered fraction are declared assumptions, not observations'},
               'results': results, 'catalog_price_identities_at_run': quotes, 'effects': {'awards': 0, 'journal_entries': 0, 'quotes_changed': False, 'terms_changed': False}, 'created_by': principal.id, 'created_at': now(), 'notes': (body.get('notes') or '')[:400]}
        db.execute('INSERT INTO pricing_experiments VALUES (?,?,?,?,?,?)', (eid, principal.workspace, rec['name'], version, json.dumps(rec), now()))
        history.record(db, principal.workspace, principal.id, 'work.pricing_experiment', 'pricing_experiment', eid, {'name': rec['name'], 'version': version, 'policies': [p['name'] for p in pols], 'workload': len(wl)})
        return rec

    def view(self, db, principal, eid):
        principal.require('work:read')
        r = db.execute('SELECT * FROM pricing_experiments WHERE id=? AND workspace=?', (eid, principal.workspace)).fetchone()
        if r is None:
            raise ServiceError('NOT_FOUND', 'experiment')
        return json.loads(r['record_json'])

    def list(self, db, principal):
        principal.require('work:read')
        return [{'id': r['id'], 'name': r['name'], 'version': r['version'], 'created_at': r['created_at']} for r in db.execute('SELECT * FROM pricing_experiments WHERE workspace=? ORDER BY name, version', (principal.workspace,)).fetchall()]
