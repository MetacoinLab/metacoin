"""Evidence-based provider history without a magic score (Order 08 §38).

The view is built from the receipts, decisions, attempts, offers and disputes the VIEWER can already see; every dimension
keeps its sample size and the population is named. A valid negative accepted under contract is an accepted delivery, never a
failure. A provider-controlled list of selected receipts is a disclosed portfolio, not a complete audited history."""
import json
import statistics

from ..db import now
from ..errors import ServiceError
from .. import history

MIN_COHORT = 3
PORTFOLIO_MAX = 64


def _population(db, principal, prow):
    """Which contracts of this provider the viewer may aggregate over, and the honest label for that scope."""
    if principal.id == prow['principal_id']:
        rows = db.execute('SELECT * FROM work_awards WHERE provider_id=? ORDER BY awarded_at', (prow['id'],)).fetchall(); label = 'all contracts of this provider (its own view)'
    elif principal.can('work:award') and principal.role == 'owner':
        rows = db.execute('SELECT * FROM work_awards WHERE provider_id=? AND workspace=? ORDER BY awarded_at', (prow['id'], principal.workspace)).fetchall(); label = 'all contracts of this provider in this workspace (workspace owner view)'
    else:
        rows = db.execute('SELECT * FROM work_awards WHERE provider_id=? AND awarded_by=? ORDER BY awarded_at', (prow['id'], principal.id)).fetchall(); label = 'only contracts you awarded to this provider'
    return rows, label


def _band(n):
    return n if n >= MIN_COHORT or n == 0 else '1-%d' % (MIN_COHORT - 1)


def _summ(xs):
    xs = sorted(xs)
    return {'n': len(xs), 'min': xs[0], 'median': int(statistics.median(xs)), 'max': xs[-1]} if xs else {'n': 0}


def build(db, principal, providers, prid, disclosed_only=False):
    principal.require('work:read')
    prow = providers.row(db, principal, prid)
    ev = json.loads(prow['evidence_json'] or '{}')
    portfolio = ev.get('portfolio') or {'receipt_ids': [], 'updated_at': None, 'note': None}
    rows, label = ([], 'disclosed portfolio only') if disclosed_only else _population(db, principal, prow)
    board = getattr(getattr(providers.svc, 'economy', None), 'board', None)
    for a in rows:
        if board is not None:
            board.tick(db, a['id'])                                                       # attempt and milestone states follow the job records
    families, methods, outcomes, exec_states, disputes, latencies, prices = {}, {}, {'accepted_positive': 0, 'accepted_valid_negative': 0, 'accepted_diagnostic': 0, 'rejected': 0, 'pending': 0}, {'completed': 0, 'failed': 0, 'running': 0}, {}, [], {}
    completed = closed = 0
    for a in rows:
        t = db.execute('SELECT terms_json FROM work_terms WHERE id=?', (a['terms_id'],)).fetchone()
        terms = json.loads(t['terms_json']) if t else {}
        kind = (terms.get('operation') or {}).get('kind', 'unknown'); families[kind] = families.get(kind, 0) + 1
        if a['state'] == 'closed':
            closed += 1
        for at in db.execute('SELECT * FROM work_attempts WHERE award_id=? AND provider_id=?', (a['id'], prid)).fetchall():
            st = 'completed' if at['state'] == 'completed' else 'failed' if at['state'] == 'failed' else 'running'
            exec_states[st] += 1
            if at['state'] == 'completed' and at['finished_at'] and at['started_at']:
                latencies.append(at['finished_at'] - at['started_at']); completed += 1
        for r in db.execute("SELECT statement_json FROM work_receipts WHERE award_id=? AND kind='verification'", (a['id'],)).fetchall():
            cls = (json.loads(r['statement_json']).get('claims') or {}).get('class', 'unknown'); methods[cls] = methods.get(cls, 0) + 1
        for d in db.execute('SELECT * FROM work_decisions WHERE award_id=? AND superseded_by IS NULL', (a['id'],)).fetchall():
            e = json.loads(d['evaluation_json'])
            if d['decision'] == 'rejected':
                outcomes['rejected'] += 1
            elif d['payment_class'] == 'diagnostic':
                outcomes['accepted_diagnostic'] += 1
            elif e.get('science') in ('INFEASIBLE',):
                outcomes['accepted_valid_negative'] += 1
            else:
                outcomes['accepted_positive'] += 1
        for m in db.execute("SELECT state FROM work_milestones WHERE award_id=? AND state='delivered'", (a['id'],)).fetchall():
            outcomes['pending'] += 1
        for dp in db.execute('SELECT * FROM work_disputes WHERE award_id=?', (a['id'],)).fetchall():
            out = (json.loads(dp['decision_json']).get('outcome') if dp['decision_json'] else dp['state']); disputes[out] = disputes.get(out, 0) + 1
        o = db.execute('SELECT price_amount, asset FROM work_offers WHERE id=?', (a['offer_id'],)).fetchone()
        if o:
            prices.setdefault(o['asset'], []).append(o['price_amount'])
    n = len(rows)
    band = (lambda v: v) if (n >= MIN_COHORT or n == 0 or principal.id == prow['principal_id']) else _band   # bands only for a small cohort seen by someone else
    evidence = 'unknown: no contracts visible to you for this provider (a new or undisclosed provider is neither trusted nor suspected)' if n == 0 else ('below the minimum cohort of %d: counts shown as bands' % MIN_COHORT if n < MIN_COHORT else 'aggregated from %d visible contracts' % n)
    return {'schema': 'metacoin-provider-history/v1', 'provider_id': prid, 'name': prow['name'], 'population': {'label': label, 'contracts': band(n), 'exact_n_available_to_viewer': n >= MIN_COHORT or n == 0 or principal.id == prow['principal_id']},
            'evidence': evidence,
            'dimensions': {'task_families': {k: band(v) for k, v in families.items()}, 'completed_contracts': band(completed), 'closed_contracts': band(closed),
                           'verification_methods': {k: band(v) for k, v in methods.items()}, 'acceptance_outcomes': {k: band(v) for k, v in outcomes.items()},
                           'execution': {k: band(v) for k, v in exec_states.items()}, 'dispute_outcomes': {k: band(v) for k, v in disputes.items()},
                           'observed_latency_seconds': _summ(latencies) if len(latencies) >= MIN_COHORT else {'n': len(latencies), 'note': 'below the minimum cohort; not summarised'},
                           'price_behaviour': {asset: (_summ(xs) if len(xs) >= MIN_COHORT else {'n': len(xs), 'note': 'below the minimum cohort; not summarised'}) for asset, xs in prices.items()},
                           'operator_relationship': json.loads(prow['relationship_json']), 'execution_type': json.loads(prow['execution_json'])['type'], 'capabilities': json.loads(prow['capabilities_json'])},
            'disclosed_portfolio': {'count': len(portfolio.get('receipt_ids') or []), 'updated_at': portfolio.get('updated_at'), 'note': 'provider-selected receipts: a disclosed portfolio, not a complete audited history'},
            'reading': ['no single score: buyers weigh exact replay, custody, deadlines and price differently; a request\'s declared selection policy uses the relevant dimensions',
                        'an accepted valid negative is an accepted delivery under its contract, not a provider failure',
                        'the population is what you can see; cohort bands reduce but do not guarantee non-disclosure of private contract history'],
            'generated_at': now()}


def set_portfolio(db, principal, providers, prid, body):
    """The provider selects receipts of its own contracts to disclose; the list is stored as a labelled portfolio."""
    prow = providers.row(db, principal, prid)
    if principal.id != prow['principal_id']:
        raise ServiceError('FORBIDDEN', 'only the provider curates its disclosed portfolio')
    ids = body.get('receipt_ids')
    if type(ids) is not list or len(ids) > PORTFOLIO_MAX or any(type(x) is not str for x in ids):
        raise ServiceError('VALIDATION', {'code': 'receipt_ids', 'max': PORTFOLIO_MAX})
    own = {r['id'] for r in db.execute('SELECT r.id FROM work_receipts r JOIN work_awards a ON a.id=r.award_id WHERE a.provider_id=?', (prid,)).fetchall()}
    foreign = [x for x in ids if x not in own]
    if foreign:
        raise ServiceError('FORBIDDEN', {'code': 'receipt_not_of_this_provider', 'ids': foreign[:5]})
    ev = json.loads(prow['evidence_json'] or '{}'); ev['portfolio'] = {'receipt_ids': sorted(set(ids)), 'updated_at': now(), 'note': (body.get('note') or '')[:400]}
    db.execute('UPDATE providers SET evidence_json=?, updated_at=? WHERE id=?', (json.dumps(ev), now(), prid))
    history.record(db, prow['workspace'], principal.id, 'work.provider_portfolio', 'provider', prid, {'count': len(ev['portfolio']['receipt_ids'])})
    return portfolio_view(db, principal, providers, prid)


def portfolio_view(db, principal, providers, prid):
    principal.require('work:read')
    prow = providers.row(db, principal, prid)
    ev = json.loads(prow['evidence_json'] or '{}'); pf = ev.get('portfolio') or {'receipt_ids': [], 'updated_at': None, 'note': ''}
    items = []
    for rid in pf['receipt_ids']:
        r = db.execute('SELECT * FROM work_receipts WHERE id=?', (rid,)).fetchone()
        if r is None:
            items.append({'id': rid, 'status': 'receipt no longer exists'}); continue
        st = json.loads(r['statement_json']); claims = st.get('claims') or {}
        disclosed = {'id': rid, 'kind': r['kind'], 'issued_at': st.get('issued_at'), 'terms_digest': st.get('terms_digest'), 'key_id': r['key_id']}
        if r['kind'] == 'verification':
            disclosed['class'] = claims.get('class'); disclosed['outcome'] = claims.get('outcome'); disclosed['independence'] = (claims.get('independence') or {}).get('label')
        elif r['kind'] == 'acceptance':
            disclosed['decision'] = claims.get('decision'); disclosed['science'] = claims.get('science'); disclosed['payment_class'] = claims.get('payment_class')
        elif r['kind'] == 'provider':
            disclosed['kind_executed'] = (claims.get('executed') or {}).get('kind'); disclosed['execution_state'] = (claims.get('executed') or {}).get('execution_state')
        items.append(disclosed)
    total = db.execute('SELECT COUNT(*) FROM work_receipts r JOIN work_awards a ON a.id=r.award_id WHERE a.provider_id=?', (prid,)).fetchone()[0]
    return {'schema': 'metacoin-provider-portfolio/v1', 'provider_id': prid, 'items': items, 'note': pf.get('note'), 'updated_at': pf.get('updated_at'),
            'completeness': {'disclosed': len(items), 'existing_receipts_known_to_this_service': total if (principal.id == prow['principal_id'] or principal.role == 'owner') else 'not disclosed to you',
                             'label': 'complete' if total == len(items) and total else ('incomplete: the provider chose what to disclose' if items else 'nothing disclosed'), 'note': 'a provider-selected list is not an audited history; verify each receipt against the trust history'},
            'operator_relationship': json.loads(prow['relationship_json'])}
