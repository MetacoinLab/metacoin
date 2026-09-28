"""Backlog 6: model routing by evaluated task capability.

Chooses among ALREADY INSTALLED, registered local revisions that offer an operation, using measured evidence recorded in
this workspace: scored evaluation runs (per task category when the suite items carry one), observed latency from completed
requests (milliseconds per output token / per item), and a caller budget (resource bytes, latency). Ranking is deterministic
and explainable; the promoted default is the explicit fallback when no candidate carries evidence or meets the budget. No
hosted model is ever a candidate."""
import json

from ..errors import ServiceError

POLICY = 'metacoin-model-routing/v1'


def evidence_for(db, workspace, revision_id, category=None):
    """Evaluation and latency evidence for one revision: category accuracy from scored runs (items with a matching category,
    or all items when no category is requested), and observed latency from succeeded requests."""
    runs = db.execute("SELECT id, suite_id, results_json, percent, scored_at FROM evaluation_runs WHERE workspace=? AND model_revision_id=? AND state='scored' ORDER BY scored_at DESC LIMIT 10", (workspace, revision_id)).fetchall()
    ok = n = 0; used = []
    for r in runs:
        results = json.loads(r['results_json']) if r['results_json'] else []
        if not isinstance(results, list):
            continue
        items = [x for x in results if (category is None or x.get('category') == category)]
        if not items:
            continue
        ok += sum(1 for x in items if x.get('ok')); n += len(items); used.append(r['id'])
    lat = db.execute("SELECT SUM(inference_ms) AS ms, SUM(COALESCE(output_tokens, 0)) AS toks, SUM(COALESCE(items, 0)) AS items, COUNT(*) AS n FROM model_requests WHERE workspace=? AND revision_id=? AND phase='completed' AND inference_ms IS NOT NULL", (workspace, revision_id)).fetchone()
    units = (lat['toks'] or 0) + (lat['items'] or 0)
    return {'evaluated_items': n, 'accuracy': round(ok / n, 3) if n else None, 'runs': used, 'requests_observed': lat['n'] or 0, 'ms_per_unit': round((lat['ms'] or 0) / units, 3) if units else None}


def route(db, principal, registry, operation, category=None, budget=None):
    principal.require('model:use')
    if operation not in ('generate', 'embed'):
        raise ServiceError('VALIDATION', {'code': 'operation', 'allowed': ['generate', 'embed']})
    budget = budget or {}
    if type(budget) is not dict or set(budget) - {'max_resource_bytes', 'max_ms_per_unit'}:
        raise ServiceError('VALIDATION', {'code': 'budget', 'allowed': ['max_resource_bytes', 'max_ms_per_unit']})
    rows = db.execute("SELECT * FROM model_revisions WHERE status='registered' AND installed=1").fetchall()
    cands = []
    for r in rows:
        ops = json.loads(r['operations_json'] or '[]')
        if operation not in ops:
            continue
        ev = evidence_for(db, principal.workspace, r['id'], category)
        reasons = []
        if budget.get('max_resource_bytes') is not None and (r['resource_estimate_bytes'] or 0) > budget['max_resource_bytes']:
            reasons.append('resource estimate %d exceeds budget %d' % (r['resource_estimate_bytes'] or 0, budget['max_resource_bytes']))
        if budget.get('max_ms_per_unit') is not None and ev['ms_per_unit'] is not None and ev['ms_per_unit'] > budget['max_ms_per_unit']:
            reasons.append('observed %.1f ms/unit exceeds budget %s' % (ev['ms_per_unit'], budget['max_ms_per_unit']))
        cands.append({'revision_id': r['id'], 'model_id': r['model_id'], 'revision': r['revision'], 'resource_estimate_bytes': r['resource_estimate_bytes'], 'evidence': ev, 'eligible': not reasons, 'excluded_because': reasons})
    d = db.execute('SELECT revision_id FROM model_defaults WHERE operation=?', (operation,)).fetchone()
    default_id = d['revision_id'] if d else None
    eligible = [c for c in cands if c['eligible']]
    with_evidence = [c for c in eligible if c['evidence']['accuracy'] is not None]
    if with_evidence:
        ranked = sorted(with_evidence, key=lambda c: (-c['evidence']['accuracy'], c['evidence']['ms_per_unit'] if c['evidence']['ms_per_unit'] is not None else float('inf'), c['model_id']))
        chosen, basis = ranked[0], 'highest evaluated accuracy for the category, then lowest observed latency, among installed revisions within budget'
    elif eligible and default_id in {c['revision_id'] for c in eligible}:
        chosen, basis = next(c for c in eligible if c['revision_id'] == default_id), 'fallback: the promoted default (no evaluation evidence for this category on any installed revision)'
    elif eligible:
        chosen, basis = sorted(eligible, key=lambda c: (c['resource_estimate_bytes'] or 0, c['model_id']))[0], 'fallback: smallest installed revision within budget (no promoted default within budget, no evaluation evidence)'
    else:
        chosen, basis = None, 'no installed revision offers this operation within the budget; nothing chosen'
    return {'policy': POLICY, 'operation': operation, 'category': category, 'budget': budget, 'chosen': chosen, 'basis': basis, 'candidates': sorted(cands, key=lambda c: c['model_id']), 'default_revision_id': default_id,
            'hosted_models': 'never candidates: only installed local revisions', 'fallback': 'explicit: the promoted default is used when evidence is absent; the caller may pin model_revision_id to override'}
