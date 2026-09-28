"""Document imports, extraction revisions, page inspection, review-then-publish, dependency-aware removal, table
annotations and validated table-to-dataset mappings (Order 07 §11-§18)."""
import base64
import hashlib
import json
import secrets
from decimal import Decimal

from experiments.private_receipts import receipt as merkle
from .. import history
from ..db import now
from ..errors import ServiceError
from . import units as units_mod

IMPORT_SCHEMA = 'document-import-input/v1'
MAPPING_SCHEMA = 'metacoin-table-mapping/v1'
STATES = ('received', 'validating', 'extracting', 'awaiting_review', 'ready', 'failed', 'cancelled', 'removed')
MODES = ('native', 'ocr_needed', 'ocr_forced')
FORMATS = ('pdf',)
ANNOTATION_KINDS = ('header_row', 'ignore_row', 'unit', 'cell_correction', 'locale', 'note')
MAPPING_TARGETS = ('energy_intervals', 'temporal_series', 'calibration_numeric')


def validate_import_input(data):
    if type(data) is not dict or data.get('schema') != IMPORT_SCHEMA or set(data) - {'schema', 'import_id', 'attempt'}:
        raise merkle.Invalid('document-import input: {schema, import_id, attempt}')
    if type(data.get('import_id')) is not str or not data['import_id'].startswith('di_') or type(data.get('attempt')) is not int:
        raise merkle.Invalid('import_id/attempt')


VALIDATORS = {'document_import': validate_import_input}


def implementation_digest():
    from pathlib import Path
    h = hashlib.sha256(b'metacoin/document-extractor/v1\0')
    here = Path(__file__).parent
    for name in ('extract_child.py', 'service.py', 'engine.py', 'units.py'):
        h.update(name.encode() + b'\0' + (here / name).read_bytes() + b'\0')
    return h.hexdigest()


class Documents:
    def __init__(self, settings, services):
        self.settings, self.svc = settings, services
        self.store = services.store

    # ---- imports --------------------------------------------------------------------------------------------
    def create_import(self, db, principal, *, name, fmt, content=None, artifact_id=None, collection_id=None, policy=None):
        principal.require('knowledge:write')
        L = self.settings.limits
        if type(name) is not str or not 1 <= len(name) <= 128:
            raise ServiceError('VALIDATION', 'name')
        if fmt not in FORMATS:
            raise ServiceError('VALIDATION', {'code': 'format', 'supported': list(FORMATS), 'note': 'text, markdown and csv go through POST /api/v1/knowledge/collections/{id}/documents'})
        policy = dict(policy or {})
        if set(policy) - {'mode', 'ocr_language', 'refuse_active_content'} or policy.get('mode', 'ocr_needed') not in MODES:
            raise ServiceError('VALIDATION', {'code': 'policy', 'modes': list(MODES)})
        policy.setdefault('mode', 'ocr_needed'); policy.setdefault('ocr_language', 'en'); policy.setdefault('refuse_active_content', True)
        if collection_id is not None:
            col = self.svc.knowledge.collection(db, principal, collection_id)
            if col['retired_at'] is not None:
                raise ServiceError('CONFLICT', 'collection retired')
        if (content is None) == (artifact_id is None):
            raise ServiceError('VALIDATION', 'exactly one of content (bytes) or artifact_id (a workspace artifact)')
        if artifact_id is not None:
            row = self.store.row(db, artifact_id, principal.workspace)
            if row['owner_id'] != principal.id and not principal.can('artifact:read_private'):
                raise ServiceError('FORBIDDEN', 'artifact not readable')
            content = self.store.load(db, artifact_id, principal.workspace)
        if type(content) is not bytes or not content:
            raise ServiceError('VALIDATION', 'content bytes')
        if len(content) > L['document_max_bytes']:
            raise ServiceError('PAYLOAD_TOO_LARGE', {'code': 'document_too_large', 'limit_bytes': L['document_max_bytes']})
        if fmt == 'pdf' and not content.startswith(b'%PDF-'):
            raise ServiceError('VALIDATION', {'code': 'format_mismatch', 'reason': 'declared pdf but the bytes do not start with %PDF-'})
        n = db.execute('SELECT COUNT(*) FROM document_imports WHERE workspace=?', (principal.workspace,)).fetchone()[0]
        if n >= L['document_max_imports_per_workspace']:
            raise ServiceError('RATE_LIMITED', 'import quota for this workspace')
        digest = hashlib.sha256(content).hexdigest()
        src = artifact_id or self.store.store(db, workspace=principal.workspace, kind='document_source', owner_id=principal.id, plaintext=content, recipients=[], intended_use='document-original-bytes;owner-worker', limit_bytes=L['document_max_bytes'])
        iid = 'di_' + secrets.token_hex(6)
        db.execute('INSERT INTO document_imports (id, workspace, owner_id, collection_id, name, declared_format, source_artifact_id, content_sha256, byte_count, policy_json, state, stage, progress_json, attempt, created_at, updated_at) '
                   'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (iid, principal.workspace, principal.id, collection_id, name, fmt, src, digest, len(content), json.dumps(policy), 'received', 'received', json.dumps({}), 0, now(), now()))
        history.record(db, principal.workspace, principal.id, 'document.import', 'document_import', iid, {'format': fmt, 'bytes': len(content), 'sha256': digest, 'mode': policy['mode']})
        self._submit(db, principal, iid)
        return self.view(db, principal, iid)

    def _submit(self, db, principal, iid):
        from ..api import quick_submit
        row = self._row(db, principal, iid)
        attempt = row['attempt'] + 1
        out = quick_submit(self.svc, db, principal, 'document_import', {'schema': IMPORT_SCHEMA, 'import_id': iid, 'attempt': attempt}, 'import ' + row['name'][:40])
        db.execute("UPDATE document_imports SET state='received', stage='queued', attempt=?, job_id=?, error_json=NULL, updated_at=? WHERE id=?", (attempt, out['job_id'], now(), iid))
        return out['job_id']

    def _row(self, db, principal, iid):
        r = db.execute('SELECT * FROM document_imports WHERE id=? AND workspace=?', (iid, principal.workspace)).fetchone()
        if r is None:
            raise ServiceError('NOT_FOUND', 'document import')
        return r

    def view(self, db, principal, iid):
        principal.require('knowledge:read')
        r = self._row(db, principal, iid)
        ex = db.execute('SELECT * FROM document_extractions WHERE id=?', (r['extraction_id'],)).fetchone() if r['extraction_id'] else None
        job = db.execute('SELECT state, error_code FROM jobs WHERE id=?', (r['job_id'],)).fetchone() if r['job_id'] else None
        out = {'id': iid, 'name': r['name'], 'format': r['declared_format'], 'collection_id': r['collection_id'], 'state': r['state'], 'stage': r['stage'], 'progress': json.loads(r['progress_json'] or '{}'), 'attempt': r['attempt'],
               'job_id': r['job_id'], 'job_state': job['state'] if job else None, 'content_sha256': r['content_sha256'], 'byte_count': r['byte_count'], 'policy': json.loads(r['policy_json']), 'page_count': r['page_count'],
               'error': json.loads(r['error_json']) if r['error_json'] else None, 'document_id': r['document_id'], 'version_id': r['version_id'], 'created_at': r['created_at'], 'updated_at': r['updated_at'], 'removed_at': r['removed_at'],
               'extraction': None, 'tables': []}
        if ex:
            out['extraction'] = {'id': ex['id'], 'attempt': ex['attempt'], 'parser': json.loads(ex['parser_json']), 'policy': json.loads(ex['policy_json']), 'page_count': ex['page_count'], 'excluded_pages': ex['excluded_pages'], 'ocr_pages': ex['ocr_pages'],
                                 'table_count': ex['table_count'], 'warnings': json.loads(ex['warnings_json']), 'active_content': json.loads(ex['active_content_json']), 'page_map': json.loads(ex['page_map_json']), 'created_at': ex['created_at']}
            out['tables'] = [self.table_view(t, brief=True) for t in db.execute('SELECT * FROM document_tables WHERE extraction_id=? ORDER BY ordinal', (ex['id'],)).fetchall()]
        out['next'] = {'received': 'queued for extraction', 'extracting': 'a worker is extracting; progress updates per page', 'awaiting_review': 'inspect excluded pages and tables, then POST /publish (or /retry with another policy)',
                       'ready': 'published to the collection; search, cite, map tables', 'failed': 'inspect error; POST /retry creates a new attempt', 'cancelled': 'POST /retry creates a new attempt', 'removed': 'removed; derived artifacts retired'}.get(r['state'])
        return out

    def list(self, db, principal, collection_id=None):
        principal.require('knowledge:read')
        sql, args = 'SELECT id FROM document_imports WHERE workspace=?', [principal.workspace]
        if collection_id:
            sql += ' AND collection_id=?'; args.append(collection_id)
        return [self.view(db, principal, r['id']) for r in db.execute(sql + ' ORDER BY created_at DESC LIMIT 100', args).fetchall()]

    def pages(self, db, principal, iid):
        """The stored extraction revision (private): per-page raw/normalized text, method, geometry, warnings, OCR diagnostics."""
        principal.require('knowledge:read')
        r = self._row(db, principal, iid)
        if not r['extraction_id']:
            raise ServiceError('CONFLICT', {'code': 'no_extraction', 'state': r['state']})
        ex = db.execute('SELECT * FROM document_extractions WHERE id=?', (r['extraction_id'],)).fetchone()
        return json.loads(self.store.load(db, ex['pages_artifact_id'], principal.workspace))

    def page(self, db, principal, iid, index):
        data = self.pages(db, principal, iid)
        if not 0 <= index < len(data['pages']):
            raise ServiceError('NOT_FOUND', 'page')
        pg = dict(data['pages'][index])
        pg['spans'] = pg['spans'][:500]
        return {'import_id': iid, 'page': pg, 'parser': data['parser'], 'indexing_convention': data['indexing_convention'], 'representations': {'raw_text': 'parser output, untouched', 'normalized_text': 'retrieval text (hyphenation joined, whitespace collapsed); quote verification names which one it binds to'}}

    def preview_png(self, db, principal, iid, index):
        principal.require('knowledge:read')
        r = self._row(db, principal, iid)
        if r['state'] == 'removed':
            raise ServiceError('FORBIDDEN', 'document removed')
        if r['document_id']:
            doc = db.execute('SELECT revoked_at FROM knowledge_documents WHERE id=?', (r['document_id'],)).fetchone()
            if doc and doc['revoked_at'] is not None:
                raise ServiceError('FORBIDDEN', 'source document revoked')
        ex = db.execute('SELECT previews_json FROM document_extractions WHERE id=?', (r['extraction_id'],)).fetchone() if r['extraction_id'] else None
        previews = json.loads(ex['previews_json']) if ex else []
        aid = next((p['artifact_id'] for p in previews if p['index'] == index), None)
        if aid is None:
            raise ServiceError('NOT_FOUND', 'no preview for this page')
        return self.store.load(db, aid, principal.workspace)

    def cancel(self, db, principal, iid):
        principal.require('knowledge:write')
        r = self._row(db, principal, iid)
        if r['state'] in ('ready', 'failed', 'cancelled', 'removed', 'awaiting_review'):
            raise ServiceError('CONFLICT', {'code': 'not_cancellable', 'state': r['state']})
        outcome = self.svc.jobs.cancel(db, principal, r['job_id']) if r['job_id'] else 'no_job'
        if outcome == 'cancelled':
            db.execute("UPDATE document_imports SET state='cancelled', stage='cancelled', updated_at=? WHERE id=?", (now(), iid))
        else:
            db.execute("UPDATE document_imports SET stage='cancel_requested', updated_at=? WHERE id=?", (now(), iid))
        return {'import_id': iid, 'job': outcome}

    def retry(self, db, principal, iid, policy=None):
        principal.require('knowledge:write')
        r = self._row(db, principal, iid)
        if r['state'] not in ('failed', 'cancelled', 'awaiting_review'):
            raise ServiceError('CONFLICT', {'code': 'not_retryable', 'state': r['state']})
        if policy:
            pol = dict(json.loads(r['policy_json']), **policy)
            if set(pol) - {'mode', 'ocr_language', 'refuse_active_content'} or pol.get('mode') not in MODES:
                raise ServiceError('VALIDATION', 'policy')
            db.execute('UPDATE document_imports SET policy_json=? WHERE id=?', (json.dumps(pol), iid))
        jid = self._submit(db, principal, iid)
        return {'import_id': iid, 'job_id': jid, 'attempt': r['attempt'] + 1, 'note': 'a new attempt identity; earlier extraction revisions stay readable'}

    def publish(self, db, principal, iid, include_excluded=False):
        """From awaiting_review: publish the extraction to the collection as a knowledge document version (page-aware chunks)."""
        principal.require('knowledge:write')
        r = self._row(db, principal, iid)
        if r['state'] != 'awaiting_review':
            raise ServiceError('CONFLICT', {'code': 'not_awaiting_review', 'state': r['state']})
        return self._publish(db, principal, r, include_excluded)

    def _publish(self, db, principal, r, include_excluded):
        ex = db.execute('SELECT * FROM document_extractions WHERE id=?', (r['extraction_id'],)).fetchone()
        if r['collection_id'] is None:
            db.execute("UPDATE document_imports SET state='ready', stage='ready (no collection: not indexed)', updated_at=? WHERE id=?", (now(), r['id']))
            return self.view(db, principal, r['id'])
        text = self.store.load(db, ex['text_artifact_id'], principal.workspace).decode('utf-8')
        page_map = json.loads(ex['page_map_json'])
        v = self.svc.knowledge.publish_extraction(db, principal, r['collection_id'], name=r['name'], text=text, page_map=page_map, raw_artifact_id=r['source_artifact_id'], text_artifact_id=ex['text_artifact_id'],
                                                  parser=json.loads(ex['parser_json']), warnings=json.loads(ex['warnings_json']), document_id=r['document_id'], include_excluded=include_excluded, source_sha256=r['content_sha256'])
        db.execute("UPDATE document_imports SET state='ready', stage='published', document_id=?, version_id=?, updated_at=? WHERE id=?", (v['document_id'], v['id'], now(), r['id']))
        history.record(db, principal.workspace, principal.id, 'document.published', 'document_import', r['id'], {'document_id': v['document_id'], 'version_id': v['id'], 'chunks': v['chunk_count'], 'excluded_pages_included': include_excluded})
        return self.view(db, principal, r['id'])

    def remove(self, db, principal, iid, confirm=False):
        """Dependency-aware removal: reports what is retired, what keeps references, what cannot be recalled; applies when confirm=true."""
        principal.require('knowledge:write')
        r = self._row(db, principal, iid)
        exs = db.execute('SELECT * FROM document_extractions WHERE import_id=?', (iid,)).fetchall()
        artifacts = [r['source_artifact_id']] + [a for ex in exs for a in (ex['pages_artifact_id'], ex['text_artifact_id'], ex['tables_artifact_id']) if a] + [p['artifact_id'] for ex in exs for p in json.loads(ex['previews_json'])]
        tables = [t['id'] for t in db.execute('SELECT id FROM document_tables WHERE import_id=?', (iid,)).fetchall()]
        mappings = db.execute('SELECT id, dataset_version_id, state FROM dataset_mappings WHERE table_id IN (%s)' % ','.join('?' * len(tables)) if tables else 'SELECT id, dataset_version_id, state FROM dataset_mappings WHERE 0', tables).fetchall()
        datasets = [m['dataset_version_id'] for m in mappings if m['dataset_version_id']]
        indexes = [i['id'] for i in db.execute("SELECT id, document_versions_json FROM knowledge_indexes WHERE collection_id=? AND state='ready'", (r['collection_id'],)).fetchall() if r['collection_id'] and r['document_id'] and r['document_id'] in json.loads(i['document_versions_json'])] if r['collection_id'] else []
        answers = [a['id'] for a in db.execute("SELECT id, sources_json FROM knowledge_answers WHERE workspace=?", (principal.workspace,)).fetchall() if r['document_id'] and any(s.get('document_id') == r['document_id'] for s in json.loads(a['sources_json']))]
        exports = db.execute("SELECT COUNT(*) FROM events WHERE workspace=? AND event_type='artifact.exported' AND object_id=?", (principal.workspace, r['document_id'] or '-')).fetchone()[0]
        report = {'import_id': iid, 'state': r['state'],
                  'will_retire': {'artifacts': artifacts, 'tables': tables, 'knowledge_document': r['document_id'], 'previews': sum(len(json.loads(ex['previews_json'])) for ex in exs)},
                  'keep_references': {'dataset_versions': datasets, 'mappings': [m['id'] for m in mappings], 'indexes_flagged_stale': indexes, 'answers_invalidated': answers, 'history_events': 'identifiers stay in the append-only history without content'},
                  'cannot_recall': {'exports_recorded': exports, 'note': "copies already delivered to a client, exported bundles and the operator's backups keep what they received; removal revokes new access and retires derivatives here"},
                  'confirm': confirm}
        if not confirm:
            return dict(report, applied=False)
        if r['document_id']:
            self.svc.knowledge.revoke_document(db, principal, r['document_id'], 'document import removed')
        t = now()
        for aid in artifacts:
            db.execute('UPDATE artifacts SET retention_deadline=? WHERE id=? AND workspace=?', (t, aid, principal.workspace))
        db.execute("UPDATE document_imports SET state='removed', stage='removed', removed_at=?, updated_at=? WHERE id=?", (t, t, iid))
        history.record(db, principal.workspace, principal.id, 'document.removed', 'document_import', iid, {'artifacts': len(artifacts), 'tables': len(tables), 'datasets_keep_references': len(datasets)})
        return dict(report, applied=True, note='new access revoked now; unreferenced ciphertext is unlinked by the bounded retention cleanup')

    # ---- tables, annotations, mappings ------------------------------------------------------------------------
    def _table(self, db, principal, tid):
        t = db.execute('SELECT * FROM document_tables WHERE id=? AND workspace=?', (tid, principal.workspace)).fetchone()
        if t is None:
            raise ServiceError('NOT_FOUND', 'table')
        imp = db.execute('SELECT state FROM document_imports WHERE id=?', (t['import_id'],)).fetchone()
        if imp['state'] == 'removed':
            raise ServiceError('FORBIDDEN', 'document removed')
        return t

    def table_view(self, t, brief=False, annotations=None):
        out = {'id': t['id'], 'import_id': t['import_id'], 'extraction_id': t['extraction_id'], 'ordinal': t['ordinal'], 'page_index': t['page_index'], 'page_number': t['page_index'] + 1, 'method': t['method'], 'n_rows': t['n_rows'], 'n_cols': t['n_cols'],
               'header_row': t['header_row'], 'region': json.loads(t['region_json']) if t['region_json'] else None, 'ambiguity': json.loads(t['ambiguity_json']), 'supported_form': t['supported_form'], 'continuation': json.loads(t['continuation_json']) if t['continuation_json'] else None,
               'table_sha256': t['table_sha256'], 'created_at': t['created_at']}
        if not brief:
            out['rows'] = json.loads(t['table_json'])
            out['annotations'] = annotations or []
        return out

    def table(self, db, principal, tid):
        principal.require('knowledge:read')
        t = self._table(db, principal, tid)
        ann = [self._ann_view(a) for a in db.execute('SELECT * FROM table_annotations WHERE table_id=? ORDER BY created_at', (tid,)).fetchall()]
        return self.table_view(t, annotations=ann)

    def _ann_view(self, a):
        return {'id': a['id'], 'kind': a['kind'], 'author_id': a['author_id'], 'created_at': a['created_at'], **json.loads(a['payload_json'])}

    def annotate(self, db, principal, tid, kind, payload):
        """Corrections never rewrite the extraction: an annotation carries author, time, previous value, new value and a reason."""
        principal.require('knowledge:write')
        t = self._table(db, principal, tid)
        if kind not in ANNOTATION_KINDS or type(payload) is not dict:
            raise ServiceError('VALIDATION', {'code': 'annotation', 'kinds': list(ANNOTATION_KINDS)})
        rows = json.loads(t['table_json'])
        reason = payload.get('reason', '')
        if type(reason) is not str or len(reason) > 500:
            raise ServiceError('VALIDATION', 'reason')
        rec = {'reason': reason}
        if kind in ('header_row', 'ignore_row'):
            row = payload.get('row')
            if type(row) is not int or not 0 <= row < len(rows):
                raise ServiceError('VALIDATION', 'row index')
            rec['row'] = row
        elif kind == 'unit':
            col, unit = payload.get('col'), payload.get('unit')
            if type(col) is not int or not 0 <= col < t['n_cols']:
                raise ServiceError('VALIDATION', 'col index')
            try:
                units_mod.canonical_unit(unit)
            except units_mod.UnitError as exc:
                raise ServiceError('VALIDATION', {'code': 'unit', 'reason': str(exc)}) from None
            rec.update(col=col, unit=unit)
        elif kind == 'cell_correction':
            row, col, new = payload.get('row'), payload.get('col'), payload.get('new_value')
            if type(row) is not int or type(col) is not int or not (0 <= row < len(rows) and 0 <= col < t['n_cols']) or type(new) is not str or len(new) > 200 or not reason:
                raise ServiceError('VALIDATION', 'cell correction needs row, col, new_value and a reason')
            rec.update(row=row, col=col, previous_value=rows[row][col], new_value=new)
        elif kind == 'locale':
            if payload.get('locale') not in ('point', 'comma'):
                raise ServiceError('VALIDATION', 'locale: point | comma')
            rec['locale'] = payload['locale']
        else:
            rec['text'] = str(payload.get('text', ''))[:500]
        aid = 'ta_' + secrets.token_hex(6)
        db.execute('INSERT INTO table_annotations (id, table_id, workspace, author_id, kind, payload_json, created_at) VALUES (?,?,?,?,?,?,?)', (aid, tid, principal.workspace, principal.id, kind, json.dumps(rec), now()))
        history.record(db, principal.workspace, principal.id, 'document.annotation', 'document_table', tid, {'kind': kind, 'annotation_id': aid})
        return self.table(db, principal, tid)

    def effective_table(self, db, principal, tid):
        """Rows after annotations: corrections applied as an interpretation layer; the stored extraction is untouched."""
        t = self._table(db, principal, tid)
        rows = [list(r) for r in json.loads(t['table_json'])]
        anns = db.execute('SELECT * FROM table_annotations WHERE table_id=? ORDER BY created_at', (tid,)).fetchall()
        header, ignored, units, locale = t['header_row'], set(), {}, None
        for a in anns:
            p = json.loads(a['payload_json'])
            if a['kind'] == 'header_row': header = p['row']
            elif a['kind'] == 'ignore_row': ignored.add(p['row'])
            elif a['kind'] == 'unit': units[p['col']] = p['unit']
            elif a['kind'] == 'cell_correction': rows[p['row']][p['col']] = p['new_value']
            elif a['kind'] == 'locale': locale = p['locale']
        digest = hashlib.sha256(merkle.canonical([{'id': a['id'], 'kind': a['kind'], 'payload': json.loads(a['payload_json'])} for a in anns])).hexdigest()
        return {'table': t, 'rows': rows, 'header_row': header, 'ignored_rows': sorted(ignored), 'units': units, 'locale': locale, 'annotations_digest': digest}

    def validate_mapping(self, db, principal, tid, mapping):
        """Deterministic conversion preview for a mapping over one immutable table revision plus its annotations."""
        eff = self.effective_table(db, principal, tid)
        if type(mapping) is not dict or mapping.get('schema') != MAPPING_SCHEMA or set(mapping) - {'schema', 'target', 'columns', 'locale', 'missing_policy', 'row_exclusions', 'rounding', 'name', 'units'}:
            raise ServiceError('VALIDATION', {'code': 'mapping', 'schema': MAPPING_SCHEMA, 'fields': ['target', 'columns', 'locale', 'missing_policy', 'row_exclusions', 'rounding', 'name']})
        target = mapping.get('target')
        if target not in MAPPING_TARGETS:
            raise ServiceError('VALIDATION', {'code': 'target', 'allowed': list(MAPPING_TARGETS)})
        locale = mapping.get('locale') or eff['locale'] or 'undeclared'
        if locale not in ('point', 'comma', 'undeclared'):
            raise ServiceError('VALIDATION', 'locale')
        missing = mapping.get('missing_policy', 'reject')
        if missing not in ('reject', 'exclude_row'):
            raise ServiceError('VALIDATION', 'missing_policy: reject | exclude_row')
        rounding = mapping.get('rounding', 'reject')
        if rounding not in ('reject', 'outward'):
            raise ServiceError('VALIDATION', 'rounding: reject | outward')
        cols = mapping.get('columns')
        if type(cols) is not list or not 1 <= len(cols) <= 16:
            raise ServiceError('VALIDATION', 'columns: 1..16 of {source_col, field, unit?, role?}')
        excl = set(mapping.get('row_exclusions') or [])
        if any(type(x) is not int for x in excl):
            raise ServiceError('VALIDATION', 'row_exclusions: row indexes')
        rows, header = eff['rows'], eff['header_row']
        data_rows = [(i, r) for i, r in enumerate(rows) if i != header and i not in eff['ignored_rows'] and i not in excl]
        errors, converted, excluded = [], [], []
        spec_cols = []
        from ..datasets import KINDS as DATASET_KINDS
        for c in cols:
            if type(c) is not dict or type(c.get('source_col')) is not int or not 0 <= c['source_col'] < eff['table']['n_cols'] or type(c.get('field')) is not str:
                raise ServiceError('VALIDATION', 'column mapping {source_col, field, unit?, role?}')
            unit = c.get('unit') or eff['units'].get(c['source_col'])
            role = c.get('role', 'value')
            if role not in ('value', 'low', 'high', 'label'):
                raise ServiceError('VALIDATION', 'role')
            spec_cols.append({'source_col': c['source_col'], 'field': c['field'], 'unit': unit, 'role': role})
        if target in DATASET_KINDS:
            required = DATASET_KINDS[target]['required']
            missing_fields = [f for f in required if f not in {c['field'] for c in spec_cols}]
            if missing_fields:
                errors.append({'code': 'missing_required_fields', 'fields': missing_fields, 'target': target})
        for (i, r) in data_rows:
            out, row_errors = {}, []
            for c in spec_cols:
                cell = r[c['source_col']] if c['source_col'] < len(r) else ''
                if c['role'] == 'label':
                    out[c['field']] = cell; continue
                blank = cell.strip() in ('', '—', '–', '-', 'n/a', 'N/A')
                if blank:
                    row_errors.append({'row': i, 'col': c['source_col'], 'code': 'missing_value', 'value_class': 'blank/dash (not zero, not a measurement)'}); continue
                try:
                    d = units_mod.parse_decimal(cell, locale)
                except units_mod.UnitError as exc:
                    row_errors.append({'row': i, 'col': c['source_col'], 'code': 'unparseable', 'reason': str(exc)}); continue
                if target in DATASET_KINDS:
                    field = c['field']
                    target_unit = 's' if field.endswith('_s') else ('mW' if field.endswith('_mW') else None)
                    if target_unit is None:
                        row_errors.append({'row': i, 'col': c['source_col'], 'code': 'unknown_target_field', 'field': field}); continue
                    try:
                        if c['unit'] is None:
                            row_errors.append({'row': i, 'col': c['source_col'], 'code': 'unit_required', 'field': field}); continue
                        if units_mod.dimension(c['unit']) != units_mod.dimension(target_unit):
                            row_errors.append({'row': i, 'col': c['source_col'], 'code': 'incompatible_unit', 'unit': c['unit'], 'target_unit': target_unit}); continue
                        role = 'low' if field.endswith('low_mW') else ('high' if field.endswith('high_mW') else c['role'])
                        conv = units_mod.to_base_integer(d, c['unit'], role if rounding == 'outward' else 'point', rounding)
                    except units_mod.UnitError as exc:
                        row_errors.append({'row': i, 'col': c['source_col'], 'code': 'conversion', 'reason': str(exc)}); continue
                    out[field] = conv['value']
                else:
                    if c['unit']:
                        try:
                            units_mod.canonical_unit(c['unit'])
                        except units_mod.UnitError as exc:
                            row_errors.append({'row': i, 'col': c['source_col'], 'code': 'unit', 'reason': str(exc)}); continue
                    out[c['field']] = format(d.normalize(), 'f')
            if row_errors:
                if missing == 'exclude_row' and all(e['code'] == 'missing_value' for e in row_errors):
                    excluded.append({'row': i, 'reason': 'missing value(s) under exclude_row policy'})
                else:
                    errors += row_errors
            else:
                converted.append({'row': i, 'values': out, 'source': {'table_id': tid, 'page_index': eff['table']['page_index'], 'row': i}})
        digest = hashlib.sha256(merkle.canonical({'table': eff['table']['table_sha256'], 'annotations': eff['annotations_digest'], 'mapping': {k: mapping.get(k) for k in sorted(mapping)}})).hexdigest()
        return {'table_id': tid, 'target': target, 'locale': locale, 'missing_policy': missing, 'rounding': rounding, 'columns': spec_cols, 'rows_considered': len(data_rows), 'rows_converted': len(converted), 'rows_excluded': excluded, 'errors': errors[:50],
                'valid': not errors and bool(converted), 'preview': converted[:20], 'converted_rows': converted, 'digest': digest, 'annotations_digest': eff['annotations_digest'],
                'labels': {'measurement_uncertainty': 'not represented by a single cell; declare interval columns (low/high) for bounds', 'parser_confidence': 'OCR scores are engine diagnostics, not correctness probabilities', 'operating_tolerance': 'not part of a mapping'}}

    def create_mapping(self, db, principal, tid, mapping):
        principal.require('knowledge:write')
        pv = self.validate_mapping(db, principal, tid, mapping)
        mid = 'dm_' + secrets.token_hex(6)
        db.execute('INSERT INTO dataset_mappings (id, table_id, workspace, author_id, mapping_json, annotations_digest, state, preview_json, digest, target, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                   (mid, tid, principal.workspace, principal.id, json.dumps(mapping), pv['annotations_digest'], 'validated' if pv['valid'] else 'invalid', json.dumps({k: pv[k] for k in ('rows_considered', 'rows_converted', 'rows_excluded', 'errors', 'valid', 'preview')}), pv['digest'], pv['target'], now(), now()))
        history.record(db, principal.workspace, principal.id, 'document.mapping', 'dataset_mapping', mid, {'table_id': tid, 'valid': pv['valid'], 'errors': len(pv['errors'])})
        return self.mapping(db, principal, mid)

    def mapping(self, db, principal, mid):
        principal.require('knowledge:read')
        m = db.execute('SELECT * FROM dataset_mappings WHERE id=? AND workspace=?', (mid, principal.workspace)).fetchone()
        if m is None:
            raise ServiceError('NOT_FOUND', 'mapping')
        return {'id': mid, 'table_id': m['table_id'], 'state': m['state'], 'target': m['target'], 'mapping': json.loads(m['mapping_json']), 'preview': json.loads(m['preview_json']) if m['preview_json'] else None, 'digest': m['digest'],
                'annotations_digest': m['annotations_digest'], 'dataset_version_id': m['dataset_version_id'], 'confirmed_by': m['confirmed_by'], 'confirmed_at': m['confirmed_at'], 'author_id': m['author_id'], 'created_at': m['created_at'],
                'note': 'a mapping binds one immutable table revision plus the annotations present at validation; confirmation creates an immutable dataset version with row-level provenance'}

    def confirm_mapping(self, db, principal, mid):
        """Confirmation is an explicit authorized action: revalidates against the current annotations, refuses if they changed."""
        principal.require('knowledge:write'); principal.require('contract:create')
        m = db.execute('SELECT * FROM dataset_mappings WHERE id=? AND workspace=?', (mid, principal.workspace)).fetchone()
        if m is None:
            raise ServiceError('NOT_FOUND', 'mapping')
        if m['state'] == 'confirmed':
            return self.mapping(db, principal, mid)
        mapping = json.loads(m['mapping_json'])
        pv = self.validate_mapping(db, principal, m['table_id'], mapping)
        if pv['annotations_digest'] != m['annotations_digest'] or pv['digest'] != m['digest']:
            db.execute("UPDATE dataset_mappings SET state='stale', updated_at=? WHERE id=?", (now(), mid))
            raise ServiceError('CONFLICT', {'code': 'mapping_stale', 'note': 'annotations changed since validation; create a new mapping'})
        if not pv['valid']:
            raise ServiceError('CONFLICT', {'code': 'mapping_invalid', 'errors': pv['errors'][:10]})
        t = self._table(db, principal, m['table_id'])
        imp = db.execute('SELECT * FROM document_imports WHERE id=?', (t['import_id'],)).fetchone()
        name = mapping.get('name') or ('%s table %d' % (imp['name'][:60], t['ordinal'] + 1))
        rows = [r['values'] for r in pv['converted_rows']]
        if pv['target'] in ('energy_intervals', 'temporal_series'):
            created = self.svc.datasets.create(db, principal, name=name, kind=pv['target'], fmt='json', content=json.dumps({'rows': rows}).encode(), provenance='imported', source='document import %s page %d table %d' % (imp['id'], t['page_index'] + 1, t['ordinal'] + 1))
            dvid = created['version_id']
        else:
            columns = [c['field'] for c in pv['columns']]
            body = {'name': name, 'columns': columns, 'target': columns[-1], 'units': {c['field']: c['unit'] for c in pv['columns'] if c['unit']}, 'rows': rows, 'provenance': 'imported'}
            created = self.svc.calibration.create_numeric_dataset(db, principal, body)
            dvid = created['id']
        prov = {'schema': 'metacoin-mapping-provenance/v1', 'mapping_id': mid, 'table_id': t['id'], 'table_sha256': t['table_sha256'], 'extraction_id': t['extraction_id'], 'import_id': imp['id'], 'source_sha256': imp['content_sha256'],
                'document_version_id': imp['version_id'], 'rows': [{'dataset_row': k, 'source_row': r['source']['row'], 'page_index': r['source']['page_index']} for k, r in enumerate(pv['converted_rows'])], 'excluded': pv['rows_excluded']}
        pid = self.store.store(db, workspace=principal.workspace, kind='document_tables', owner_id=principal.id, plaintext=merkle.canonical(prov), recipients=[], intended_use='mapping-provenance;owner-reviewer')
        db.execute("UPDATE dataset_mappings SET state='confirmed', dataset_version_id=?, confirmed_by=?, confirmed_at=?, updated_at=? WHERE id=?", (dvid, principal.id, now(), now(), mid))
        from ..datasets import add_edge
        add_edge(db, principal.workspace, 'document_table', t['id'], 'dataset_mapping', mid, 'interpreted_by')
        add_edge(db, principal.workspace, 'dataset_mapping', mid, 'dataset_version', dvid, 'produced')
        add_edge(db, principal.workspace, 'artifact', pid, 'dataset_version', dvid, 'provenance_of')
        if imp['version_id']:
            add_edge(db, principal.workspace, 'knowledge_version', imp['version_id'], 'dataset_version', dvid, 'derived_from')
        history.record(db, principal.workspace, principal.id, 'document.mapping', 'dataset_mapping', mid, {'confirmed': True, 'dataset_version_id': dvid, 'rows': len(rows), 'provenance_artifact': pid})
        return dict(self.mapping(db, principal, mid), created=created, provenance_artifact_id=pid)


# ---- Order 07 §66-1: document revision comparison ----------------------------------------------------------------------
def compare_imports(svc, db, principal, a_iid, b_iid, max_passages=200):
    """Compare two immutable extraction revisions (two imports, typically of one logical document) at page, passage and
    table-cell level and link content changes to the datasets, jobs and analyses that depend on the older revision.
    Layout movement (a table region or span geometry that moved while the interpreted content is identical) is reported
    as layout_only, never as a value change."""
    import difflib
    import hashlib
    principal.require('knowledge:read')
    docs = svc.documents
    ra, rb = docs._row(db, principal, a_iid), docs._row(db, principal, b_iid)
    if not ra['extraction_id'] or not rb['extraction_id']:
        raise ServiceError('CONFLICT', {'code': 'no_extraction', 'a': ra['state'], 'b': rb['state']})
    pa, pb = docs.pages(db, principal, a_iid), docs.pages(db, principal, b_iid)
    pages = []
    def h(t):
        return hashlib.sha256((t or '').encode()).hexdigest()
    n = max(len(pa['pages']), len(pb['pages']))
    passages = []
    for i in range(n):
        x = pa['pages'][i] if i < len(pa['pages']) else None; y = pb['pages'][i] if i < len(pb['pages']) else None
        if x is None or y is None:
            pages.append({'page_number': i + 1, 'change': 'added' if x is None else 'removed', 'a_method': x and x['method'], 'b_method': y and y['method']}); continue
        same_text = h(x.get('normalized_text')) == h(y.get('normalized_text'))
        geom_changed = (x.get('width'), x.get('height'), x.get('rotation')) != (y.get('width'), y.get('height'), y.get('rotation'))
        change = 'unchanged' if same_text and x['method'] == y['method'] else ('method_changed' if same_text else 'content_changed')
        if change == 'unchanged' and geom_changed:
            change = 'layout_only'
        entry = {'page_number': i + 1, 'change': change, 'a_method': x['method'], 'b_method': y['method'], 'a_text_sha256': h(x.get('normalized_text'))[:16], 'b_text_sha256': h(y.get('normalized_text'))[:16], 'geometry_changed': geom_changed}
        if change == 'content_changed':
            la, lb = (x.get('normalized_text') or '').split('\n'), (y.get('normalized_text') or '').split('\n')
            sm = difflib.SequenceMatcher(None, la, lb, autojunk=False)
            added = removed = 0
            for op, i1, i2, j1, j2 in sm.get_opcodes():
                if op == 'equal':
                    continue
                removed += i2 - i1; added += j2 - j1
                if len(passages) < max_passages:
                    passages.append({'page_number': i + 1, 'op': op, 'a_lines': la[i1:i2][:6], 'b_lines': lb[j1:j2][:6], 'a_range': [i1, i2], 'b_range': [j1, j2]})
            entry.update(lines_added=added, lines_removed=removed, similarity=round(sm.ratio(), 4))
        pages.append(entry)
    ta = [dict(r) for r in db.execute('SELECT * FROM document_tables WHERE extraction_id=? ORDER BY ordinal', (ra['extraction_id'],)).fetchall()]
    tb = [dict(r) for r in db.execute('SELECT * FROM document_tables WHERE extraction_id=? ORDER BY ordinal', (rb['extraction_id'],)).fetchall()]
    tables, changed_tables = [], []
    def match(t, pool):
        rows_t = json.loads(t['table_json'])
        for u in pool:
            if u.get('_used'):
                continue
            rows_u = json.loads(u['table_json'])
            if (u['page_index'] == t['page_index'] and u['ordinal'] == t['ordinal']) or (rows_t and rows_u and rows_t[0] == rows_u[0]):
                u['_used'] = True; return u, rows_t, rows_u
        return None, rows_t, None
    for t in ta:
        u, rows_t, rows_u = match(t, tb)
        if u is None:
            tables.append({'a_table': t['id'], 'b_table': None, 'page_number': t['page_index'] + 1, 'change': 'removed'}); changed_tables.append(t['id']); continue
        entry = {'a_table': t['id'], 'b_table': u['id'], 'page_number': t['page_index'] + 1, 'b_page_number': u['page_index'] + 1, 'a_shape': [t['n_rows'], t['n_cols']], 'b_shape': [u['n_rows'], u['n_cols']]}
        region_moved = (t['region_json'] or '') != (u['region_json'] or '') or t['page_index'] != u['page_index']
        if rows_t == rows_u:
            entry['change'] = 'layout_only' if region_moved else 'unchanged'; entry['cells_changed'] = []
        elif (t['n_rows'], t['n_cols']) != (u['n_rows'], u['n_cols']):
            entry['change'] = 'shape_changed'; entry['cells_changed'] = None; changed_tables.append(t['id'])
        else:
            cells = [{'row': r, 'col': c, 'a': rows_t[r][c], 'b': rows_u[r][c]} for r in range(len(rows_t)) for c in range(len(rows_t[r])) if rows_t[r][c] != rows_u[r][c]]
            entry['change'] = 'content_changed'; entry['cells_changed'] = cells[:500]; entry['cells_changed_count'] = len(cells); changed_tables.append(t['id'])
        entry['region_moved'] = region_moved
        tables.append(entry)
    for u in tb:
        if not u.get('_used'):
            tables.append({'a_table': None, 'b_table': u['id'], 'page_number': u['page_index'] + 1, 'change': 'added'})
    # what depends on the older revision's changed content: mappings of changed tables -> dataset versions -> downstream; analyses citing the older knowledge version
    affected = {'mappings': [], 'dataset_versions': [], 'downstream': [], 'analyses': []}
    for tid in changed_tables:
        for m in db.execute('SELECT id, state, dataset_version_id FROM dataset_mappings WHERE table_id=?', (tid,)).fetchall():
            affected['mappings'].append({'mapping': m['id'], 'table': tid, 'state': m['state'], 'dataset_version': m['dataset_version_id']})
            if m['dataset_version_id']:
                affected['dataset_versions'].append(m['dataset_version_id'])
    roots = [('dataset_version', v) for v in sorted(set(affected['dataset_versions']))]
    if ra['version_id']:
        roots.append(('knowledge_version', ra['version_id']))
    if roots:
        objs, edges, cycle, truncated = svc.analyses._forward(db, principal.workspace, roots)
        affected['downstream'] = [{'type': o['type'], 'id': o['id'], 'depth': o['depth'], 'via': o['via']} for o in objs if o['type'] != 'analysis'][:200]
        affected['analyses'] = sorted({o['id'] for o in objs if o['type'] == 'analysis'})
        affected['truncated'] = truncated
    content_pages = [p['page_number'] for p in pages if p['change'] == 'content_changed']
    summary = {'pages': len(pages), 'pages_unchanged': sum(p['change'] == 'unchanged' for p in pages), 'pages_content_changed': len(content_pages), 'pages_layout_only': sum(p['change'] == 'layout_only' for p in pages), 'pages_method_changed': sum(p['change'] == 'method_changed' for p in pages),
               'pages_added_or_removed': sum(p['change'] in ('added', 'removed') for p in pages), 'tables': len(tables), 'tables_content_changed': sum(t.get('change') in ('content_changed', 'shape_changed', 'added', 'removed') for t in tables), 'tables_layout_only': sum(t.get('change') == 'layout_only' for t in tables),
               'cells_changed': sum(t.get('cells_changed_count', 0) for t in tables), 'passages': len(passages)}
    return {'a': {'import_id': a_iid, 'extraction_id': ra['extraction_id'], 'source_sha256': ra['content_sha256'], 'version_id': ra['version_id'], 'name': ra['name']}, 'b': {'import_id': b_iid, 'extraction_id': rb['extraction_id'], 'source_sha256': rb['content_sha256'], 'version_id': rb['version_id'], 'name': rb['name']},
            'same_source_bytes': ra['content_sha256'] == rb['content_sha256'], 'summary': summary, 'pages': pages, 'passages': passages, 'tables': tables, 'affected_by_content_changes': affected,
            'interpretation': 'a scientific value changed only where interpreted content differs (content_changed / shape_changed); layout_only means geometry moved with identical interpreted content; method_changed means the same text came from another extraction method'}
