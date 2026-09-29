"""Bounded, paginated, workspace-scoped search over workflows, runs, datasets, services and campaigns, plus a safe CSV result table."""
import csv
import io
import json
from .errors import ServiceError

TYPES = ('workflow_definition', 'run', 'dataset', 'service', 'campaign')
MAX_LIMIT = 100


def _int(v, name):
    if v is None or v == '':
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        raise ServiceError('VALIDATION', name + ' must be an integer')


def search(db, principal, *, type=None, status=None, creator=None, since=None, until=None, tag=None, model=None, schema=None, limit=None, before=None):
    """Stable ordering: created_at DESC, id DESC; `before` is a created_at cursor. Only the caller's workspace is ever consulted."""
    principal.require('job:read')
    types = [type] if type else list(TYPES)
    if any(t not in TYPES for t in types):
        raise ServiceError('VALIDATION', {'code': 'type', 'allowed': list(TYPES)})
    limit = min(_int(limit, 'limit') or 25, MAX_LIMIT)
    since, until, before = _int(since, 'since'), _int(until, 'until'), _int(before, 'before')
    ws = principal.workspace
    items = []
    for t in types:
        if t == 'workflow_definition':
            sql, args = "SELECT id, name, digest, owner_id AS creator, created_at, 'workflow_definition' AS type, NULL AS status, definition_json FROM workflow_definitions WHERE workspace=?", [ws]
        elif t == 'run':
            sql, args = "SELECT r.id, d.name, d.digest, r.started_by AS creator, r.created_at, 'run' AS type, r.state AS status, NULL AS definition_json FROM workflow_runs r JOIN workflow_definitions d ON d.id=r.definition_id WHERE r.workspace=?", [ws]
        elif t == 'dataset':
            sql, args = "SELECT id, name, NULL AS digest, owner_id AS creator, created_at, 'dataset' AS type, CASE WHEN retired_at IS NULL THEN 'active' ELSE 'retired' END AS status, kind AS definition_json FROM datasets WHERE workspace=?", [ws]
        elif t == 'service':
            sql, args = "SELECT id, name, verifier_digest AS digest, NULL AS creator, created_at, 'service' AS type, status, kind AS definition_json FROM services WHERE (workspace='*' OR workspace=?)", [ws]
        else:
            sql, args = "SELECT id, name, digest, owner_id AS creator, created_at, 'campaign' AS type, state AS status, kind AS definition_json FROM sci_campaigns WHERE workspace=?", [ws]
        rows = db.execute(sql + ' ORDER BY 5 DESC, 1 DESC LIMIT 500', args).fetchall()
        for r in rows:
            item = {'type': r['type'], 'id': r['id'], 'name': r['name'], 'status': r['status'], 'creator': r['creator'], 'created_at': r['created_at'], 'digest': r['digest']}
            if t in ('dataset', 'service', 'campaign'):
                item['model'] = r['definition_json']            # the kind / model family
            elif t == 'workflow_definition':
                d = json.loads(r['definition_json'])
                item['model'] = sorted({n['type'] for n in d['nodes'] if n['type'] not in ('dataset', 'review_gate', 'export')})
                item['schema'] = d.get('schema')
            if t == 'dataset':
                drow = db.execute('SELECT tags FROM datasets WHERE id=?', (r['id'],)).fetchone()
                item['tags'] = json.loads(drow['tags']) if drow and drow['tags'] else []
            items.append(item)
    def keep(i):
        if status and i.get('status') != status: return False
        if creator and i.get('creator') != creator: return False
        if since is not None and i['created_at'] < since: return False
        if until is not None and i['created_at'] > until: return False
        if before is not None and i['created_at'] >= before: return False
        if tag and tag not in i.get('tags', []): return False
        if model and not (i.get('model') == model or (isinstance(i.get('model'), list) and model in i['model'])): return False
        if schema and i.get('schema') != schema: return False
        return True
    items = [i for i in items if keep(i)]
    items.sort(key=lambda i: (i['created_at'], i['id']), reverse=True)
    page = items[:limit]
    return {'items': page, 'more': len(items) > limit, 'next_before': page[-1]['created_at'] if page and len(items) > limit else None,
            'total_matching': len(items), 'ordering': 'created_at desc, id desc', 'limit': limit}


def _safe_cell(v):
    if v is None:
        return ''                                              # missing, distinct from 0
    s = str(v)
    return "'" + s if s[:1] in ('=', '+', '-', '@', '\t', '\r') else s


def results_csv(db, principal, jobs_service, *, state=None, kind=None, since=None, until=None, limit=None):
    """Authorized result table: outcome column is the caller's view (private, disclosed, withheld) and empty means no result."""
    principal.require('job:read')
    limit = min(_int(limit, 'limit') or 500, 2000)
    sql, args = 'SELECT * FROM jobs WHERE workspace=?', [principal.workspace]
    for col, val in (('state', state), ('kind', kind)):
        if val:
            sql += ' AND ' + col + '=?'; args.append(val)
    if _int(since, 'since') is not None:
        sql += ' AND created_at >= ?'; args.append(int(since))
    if _int(until, 'until') is not None:
        sql += ' AND created_at <= ?'; args.append(int(until))
    sql += ' ORDER BY created_at DESC, id DESC LIMIT ?'; args.append(limit)
    out = io.StringIO()
    w = csv.writer(out, lineterminator='\n')
    w.writerow(['job_id', 'kind', 'model_id', 'verifier_digest', 'state', 'review_state', 'outcome', 'evidence_root', 'reused_from', 'created_at', 'finished_at'])
    for r in db.execute(sql, args):
        v = jobs_service.view(db, principal, r)
        w.writerow([_safe_cell(v['id']), _safe_cell(v['kind']), _safe_cell(v['model_id']), _safe_cell(v['verifier_digest']), _safe_cell(v['state']), _safe_cell(v['review_state']),
                    _safe_cell(v['outcome']), _safe_cell(v['evidence_root']), _safe_cell(v.get('reused_from')), v['created_at'], _safe_cell(v['finished_at'])])
    return out.getvalue()
