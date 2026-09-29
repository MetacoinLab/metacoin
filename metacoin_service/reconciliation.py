"""Backlog 3 and 4: cross-document measurement reconciliation and measurement-request packages.

A reconciliation records several sources for one declared quantity (table cells, dataset cells, assumption rows, manual
entries), keeps each source's own value, unit and interpretation (point, low or high bound, interval), converts them to the
exact base unit with directed rounding at the boundary, reports conflicts, and produces a result ONLY under an explicit,
documented rule (select one source, min, max, median, mean, interval hull). Nothing is averaged silently.

A measurement request is a local artifact describing the next useful measurement: required quantity and units, acceptable
format, decision relevance derived from recorded evidence (plan sensitivity rows, reconciliation conflicts, analysis
staleness) and the missing evidence. It contacts nobody; exporting it is the user's action."""
import json
import secrets
from fractions import Fraction

from experiments.private_receipts import receipt as merkle
from . import history
from .datasets import add_edge
from .db import now
from .documents import units
from .errors import ServiceError

SCHEMA = 'metacoin-reconciliation/v1'
REQUEST_SCHEMA = 'metacoin-measurement-request/v1'
SOURCE_KINDS = ('table_cell', 'dataset_cell', 'assumption', 'manual')
INTERPRETATIONS = ('point', 'low', 'high', 'interval')
RULES = ('select_source', 'min', 'max', 'median', 'mean', 'interval_hull')
FORMATS = ('csv', 'json', 'pdf_table')
LIMITS = {'max_sources': 16, 'max_requests': 500}


class Reconciliations:
    def __init__(self, settings, services):
        self.settings, self.svc = settings, services

    def _resolve(self, db, principal, s):
        """Read the source object to record whether the declared value matches what the source holds."""
        kind, ref = s['kind'], s.get('ref') or {}
        if kind == 'table_cell':
            t = self.svc.documents.table(db, principal, ref.get('table_id', ''))
            rows = t['rows']
            r, c = ref.get('row'), ref.get('col')
            if type(r) is not int or type(c) is not int or not (0 <= r < len(rows)) or not (0 <= c < len(rows[r])):
                raise ServiceError('VALIDATION', {'code': 'table_cell_ref', 'table': ref.get('table_id')})
            return {'source_value': rows[r][c], 'page_number': t['page_number'], 'import_id': t['import_id']}
        if kind == 'dataset_cell':
            rows, v = self.svc.datasets.rows(db, principal, ref.get('dataset_version_id', ''))
            r, f = ref.get('row'), ref.get('field')
            if type(r) is not int or not (0 <= r < len(rows)) or f not in rows[r]:
                raise ServiceError('VALIDATION', {'code': 'dataset_cell_ref'})
            return {'source_value': rows[r][f], 'dataset_kind': v['kind']}
        if kind == 'assumption':
            a = self.svc.analyses.view(db, principal, ref.get('analysis_id', ''))
            b = next((x for x in a['blocks'] if x['id'] == ref.get('block') and x['type'] == 'assumption_table'), None)
            row = next((x for x in (b or {}).get('rows', []) if x['name'] == ref.get('name')), None)
            if row is None:
                raise ServiceError('VALIDATION', {'code': 'assumption_ref'})
            return {'source_value': row['value'], 'source_unit': row.get('unit'), 'analysis_version': a['version']}
        return {'source_value': None}

    def create(self, db, principal, body):
        principal.require('knowledge:write')
        if type(body) is not dict or set(body) - {'quantity', 'unit', 'sources', 'rule', 'note'}:
            raise ServiceError('VALIDATION', {'code': 'fields', 'allowed': ['quantity', 'unit', 'sources', 'rule', 'note']})
        q = body.get('quantity')
        if type(q) is not dict or type(q.get('name')) is not str or not 1 <= len(q['name']) <= 64:
            raise ServiceError('VALIDATION', 'quantity: {name}')
        unit = body.get('unit')
        try:
            dim = units.dimension(unit)
        except Exception:
            raise ServiceError('VALIDATION', {'code': 'unit', 'supported': sorted(units.TABLE)})
        srcs = body.get('sources')
        if type(srcs) is not list or not 1 <= len(srcs) <= LIMITS['max_sources']:
            raise ServiceError('VALIDATION', {'code': 'sources', 'max': LIMITS['max_sources']})
        rule = body.get('rule')
        if type(rule) is not dict or rule.get('method') not in RULES or type(rule.get('justification')) is not str or not rule['justification'].strip():
            raise ServiceError('VALIDATION', {'code': 'rule', 'methods': list(RULES), 'requires': 'justification'})
        resolved = []
        for i, s in enumerate(srcs):
            if type(s) is not dict or s.get('kind') not in SOURCE_KINDS or s.get('interpretation', 'point') not in INTERPRETATIONS or type(s.get('unit')) is not str:
                raise ServiceError('VALIDATION', {'code': 'source', 'index': i, 'kinds': list(SOURCE_KINDS), 'interpretations': list(INTERPRETATIONS)})
            try:
                if units.dimension(s['unit']) != dim:
                    raise ServiceError('VALIDATION', {'code': 'source_dimension', 'index': i, 'unit': s['unit'], 'expected_dimension': dim})
            except ServiceError:
                raise
            except Exception:
                raise ServiceError('VALIDATION', {'code': 'source_unit', 'index': i})
            info = self._resolve(db, principal, s)
            interp = s.get('interpretation', 'point')
            vals = s.get('value')
            try:
                if interp == 'interval':
                    if type(vals) is not list or len(vals) != 2:
                        raise ServiceError('VALIDATION', {'code': 'interval_value', 'index': i})
                    lo = units.to_base_integer(str(vals[0]), s['unit'], role='low', rounding='outward'); hi = units.to_base_integer(str(vals[1]), s['unit'], role='high', rounding='outward')
                    base = {'low': lo['value'], 'high': hi['value'], 'directed': [lo['directed'], hi['directed']]}
                else:
                    role = {'point': 'point', 'low': 'low', 'high': 'high'}[interp]
                    conv = units.to_base_integer(str(vals), s['unit'], role=role, rounding='outward' if role != 'point' else 'reject')
                    base = {'value': conv['value'], 'exact': conv['exact'], 'directed': conv['directed']}
            except units.UnitError as exc:
                raise ServiceError('VALIDATION', {'code': 'unconvertible', 'index': i, 'reason': str(exc)[:120]})
            declared = str(vals) if interp != 'interval' else None
            src_val = info.get('source_value')
            matches = None if src_val is None or declared is None else (str(src_val).strip() == declared.strip() or (str(src_val).replace(',', '.').strip() == declared.strip()))
            resolved.append({'index': i, 'kind': s['kind'], 'ref': s.get('ref'), 'label': (s.get('label') or '')[:80], 'value': vals, 'unit': s['unit'], 'interpretation': interp, 'note': (s.get('note') or '')[:300], 'base': base, 'base_unit': units.BASE[dim],
                             'source_value': src_val, 'declared_matches_source': matches, 'source_context': {k: v for k, v in info.items() if k != 'source_value'}})
        points = [r['base']['value'] for r in resolved if r['interpretation'] == 'point']
        lows = [r['base']['value'] for r in resolved if r['interpretation'] == 'low'] + [r['base']['low'] for r in resolved if r['interpretation'] == 'interval'] + points
        highs = [r['base']['value'] for r in resolved if r['interpretation'] == 'high'] + [r['base']['high'] for r in resolved if r['interpretation'] == 'interval'] + points
        conflict = {'conflicting': len(set(points)) > 1 or (min(lows) < max(highs) and len(resolved) > 1 and (len(set(points)) > 1 or any(r['interpretation'] != 'point' for r in resolved))), 'point_values': sorted(set(points)),
                    'hull': [min(lows), max(highs)] if lows and highs else None, 'spread': (max(points) - min(points)) if points else None, 'mismatching_declarations': [r['index'] for r in resolved if r['declared_matches_source'] is False]}
        m = rule['method']
        if m == 'select_source':
            sel = rule.get('selected')
            if type(sel) is not int or not 0 <= sel < len(resolved):
                raise ServiceError('VALIDATION', {'code': 'selected', 'range': [0, len(resolved) - 1]})
            r = resolved[sel]
            result = {'value': r['base'].get('value'), 'interval': [r['base']['low'], r['base']['high']] if r['interpretation'] == 'interval' else None, 'from_source': sel}
        elif m in ('min', 'max', 'median', 'mean'):
            if not points:
                raise ServiceError('VALIDATION', {'code': 'rule_needs_point_values', 'method': m})
            if m == 'min':
                result = {'value': min(points)}
            elif m == 'max':
                result = {'value': max(points)}
            elif m == 'median':
                sp = sorted(points); n = len(sp)
                med = Fraction(sp[n // 2]) if n % 2 else Fraction(sp[n // 2 - 1] + sp[n // 2], 2)
                result = {'value': int(med) if med.denominator == 1 else None, 'exact': str(med), 'interval': [med.__floor__(), -((-med).__floor__())]}
            else:
                mean = Fraction(sum(points), len(points))
                result = {'value': int(mean) if mean.denominator == 1 else None, 'exact': str(mean), 'interval': [mean.__floor__(), -((-mean).__floor__())]}
        else:
            result = {'value': None, 'interval': conflict['hull']}
        rid = 'rc_' + secrets.token_hex(6)
        record = {'schema': SCHEMA, 'id': rid, 'quantity': {'name': q['name'], 'dimension': dim}, 'unit': unit, 'base_unit': units.BASE[dim], 'sources': resolved, 'conflict': conflict, 'rule': {'method': m, 'selected': rule.get('selected'), 'justification': rule['justification'][:500]},
                  'result': result, 'note': (body.get('note') or '')[:500], 'created_by': principal.id, 'created_at': now(),
                  'statement': 'each source keeps its own value, unit and interpretation; the result exists only under the recorded rule; a mean or median of point values is a convention, not a measurement'}
        record['digest'] = merkle.canonical({k: v for k, v in record.items() if k != 'digest'}).hex()[:0] or __import__('hashlib').sha256(merkle.canonical({k: v for k, v in record.items() if k not in ('digest',)})).hexdigest()
        db.execute('INSERT INTO reconciliations (id, workspace, quantity, unit, record_json, digest, created_by, created_at) VALUES (?,?,?,?,?,?,?,?)', (rid, principal.workspace, q['name'], unit, json.dumps(record), record['digest'], principal.id, now()))
        for r in resolved:
            if r['kind'] == 'table_cell':
                add_edge(db, principal.workspace, 'document_table', r['ref']['table_id'], 'reconciliation', rid, 'used_input')
            elif r['kind'] == 'dataset_cell':
                add_edge(db, principal.workspace, 'dataset_version', r['ref']['dataset_version_id'], 'reconciliation', rid, 'used_input')
        history.record(db, principal.workspace, principal.id, 'reconciliation.created', 'reconciliation', rid, {'quantity': q['name'], 'sources': len(resolved), 'conflicting': conflict['conflicting'], 'rule': m})
        return record

    def view(self, db, principal, rid):
        principal.require('contract:read')
        r = db.execute('SELECT * FROM reconciliations WHERE id=? AND workspace=?', (rid, principal.workspace)).fetchone()
        if r is None:
            raise ServiceError('NOT_FOUND', 'reconciliation')
        rec = json.loads(r['record_json'])
        if not principal.can('job:read_private'):
            rec = {k: v for k, v in rec.items() if k not in ('sources', 'result', 'conflict', 'note')}; rec['withheld'] = 'source values and result are private'
        return rec

    def list(self, db, principal):
        principal.require('contract:read')
        return [{'id': r['id'], 'quantity': r['quantity'], 'unit': r['unit'], 'digest': r['digest'], 'created_at': r['created_at']} for r in db.execute('SELECT * FROM reconciliations WHERE workspace=? ORDER BY created_at DESC LIMIT 200', (principal.workspace,)).fetchall()]

    # ---- measurement requests ------------------------------------------------------------------------------------------
    def create_request(self, db, principal, body):
        principal.require('knowledge:write')
        if type(body) is not dict or set(body) - {'quantity', 'unit', 'required_precision', 'acceptable_format', 'from', 'note', 'title'}:
            raise ServiceError('VALIDATION', {'code': 'fields', 'allowed': ['quantity', 'unit', 'required_precision', 'acceptable_format', 'from', 'note', 'title']})
        q = body.get('quantity'); unit = body.get('unit')
        if type(q) is not str or not 1 <= len(q) <= 64:
            raise ServiceError('VALIDATION', 'quantity: name')
        try:
            dim = units.dimension(unit)
        except Exception:
            raise ServiceError('VALIDATION', {'code': 'unit', 'supported': sorted(units.TABLE)})
        fmt = body.get('acceptable_format') or {'kind': 'csv', 'columns': [q, 'unit', 'timestamp', 'method']}
        if type(fmt) is not dict or fmt.get('kind') not in FORMATS:
            raise ServiceError('VALIDATION', {'code': 'acceptable_format', 'kinds': list(FORMATS)})
        prec = body.get('required_precision')
        if prec is not None and (type(prec) is not dict or type(prec.get('abs')) not in (int, str) or type(prec.get('unit')) is not str):
            raise ServiceError('VALIDATION', 'required_precision: {abs, unit}')
        src = body.get('from') or {}
        relevance, missing, evidence = [], [], []
        if src.get('plan_job_id'):
            from .compute import service as compute_svc
            job, plan = compute_svc.plan_json(db, principal, self.svc.jobs, self.svc.store, src['plan_job_id'])
            rows = (plan.get('sensitivity') or {}).get('rows') or []
            for r in rows:
                relevance.append({'source': 'plan_sensitivity', 'job_id': src['plan_job_id'], 'parameter': r['change']['parameter'], 'value': r['change']['value'], 'decision_changed': r.get('decision_changed'), 'status': r.get('status'), 'objective': r.get('objective')})
            evidence.append({'kind': 'resource_plan', 'job_id': src['plan_job_id'], 'status': plan.get('status'), 'objective': plan.get('objective'), 'min_margin': plan.get('min_margin'), 'voi_proxy': (plan.get('sensitivity') or {}).get('value_of_information_proxy')})
            if not any(r.get('decision_changed') for r in rows):
                missing.append('no tested change of this plan altered the decision: a measurement of %s is not shown to matter within the tested range' % q)
        if src.get('reconciliation_id'):
            rec = self.view(db, principal, src['reconciliation_id'])
            evidence.append({'kind': 'reconciliation', 'id': rec['id'], 'conflicting': rec.get('conflict', {}).get('conflicting'), 'hull': rec.get('conflict', {}).get('hull'), 'rule': rec.get('rule', {}).get('method')})
            if rec.get('conflict', {}).get('conflicting'):
                relevance.append({'source': 'reconciliation_conflict', 'id': rec['id'], 'spread_base_units': rec['conflict'].get('spread'), 'hull': rec['conflict'].get('hull'), 'base_unit': rec.get('base_unit')})
                missing.append('sources disagree on %s (hull %s %s); a direct measurement would resolve which interpretation applies' % (q, rec['conflict'].get('hull'), rec.get('base_unit')))
        if src.get('analysis_id'):
            a = self.svc.analyses.view(db, principal, src['analysis_id'])
            stale = [b['id'] for b in a['blocks'] if b['status'] == 'stale']
            evidence.append({'kind': 'analysis', 'id': a['id'], 'version': a['version'], 'stale_blocks': stale})
            for b in a['blocks']:
                if b['type'] == 'assumption_table':
                    for r in b['rows']:
                        if r['name'] == q and r.get('source') in ('user_edit', 'model_suggestion', 'document'):
                            missing.append('assumption %s=%s %s in %s comes from %s, not from a measurement' % (r['name'], r['value'], r.get('unit', ''), b['id'], r.get('source')))
        rid = 'mq_' + secrets.token_hex(6)
        record = {'schema': REQUEST_SCHEMA, 'id': rid, 'title': (body.get('title') or ('measurement request: ' + q))[:120], 'quantity': {'name': q, 'dimension': dim}, 'unit': unit, 'base_unit': units.BASE[dim], 'required_precision': prec,
                  'acceptable_format': fmt, 'decision_relevance': relevance, 'missing_evidence': missing or ['no recorded evidence names this quantity as decision-relevant; the request is a user judgement'], 'evidence_considered': evidence,
                  'note': (body.get('note') or '')[:500], 'created_by': principal.id, 'created_at': now(),
                  'delivery': 'local artifact only: nothing was sent to anyone; export it and hand it over yourself', 'not_a_measurement': 'a request is not an observation; no uncertainty interval shrinks until new evidence is imported and reconciled'}
        db.execute('INSERT INTO measurement_requests (id, workspace, quantity, unit, record_json, created_by, created_at) VALUES (?,?,?,?,?,?,?)', (rid, principal.workspace, q, unit, json.dumps(record), principal.id, now()))
        for e in evidence:
            if e['kind'] == 'resource_plan':
                add_edge(db, principal.workspace, 'job', e['job_id'], 'measurement_request', rid, 'derived_from')
            elif e['kind'] == 'analysis':
                add_edge(db, principal.workspace, 'analysis', e['id'], 'measurement_request', rid, 'derived_from')
        history.record(db, principal.workspace, principal.id, 'measurement.requested', 'measurement_request', rid, {'quantity': q, 'relevance': len(relevance)})
        return record

    def request_view(self, db, principal, rid):
        principal.require('contract:read')
        r = db.execute('SELECT * FROM measurement_requests WHERE id=? AND workspace=?', (rid, principal.workspace)).fetchone()
        if r is None:
            raise ServiceError('NOT_FOUND', 'measurement request')
        return json.loads(r['record_json'])

    def list_requests(self, db, principal):
        principal.require('contract:read')
        return [{'id': r['id'], 'quantity': r['quantity'], 'unit': r['unit'], 'created_at': r['created_at']} for r in db.execute('SELECT * FROM measurement_requests WHERE workspace=? ORDER BY created_at DESC LIMIT 200', (principal.workspace,)).fetchall()]

    def export_request(self, db, principal, rid):
        principal.require('artifact:export')
        rec = self.request_view(db, principal, rid)
        history.record(db, principal.workspace, principal.id, 'artifact.exported', 'measurement_request', rid, {})
        return {'schema': REQUEST_SCHEMA + '-export', 'request': rec, 'exported_at': now(), 'exported_by': principal.id, 'contents': 'the request document only; no private results, documents or credentials'}
