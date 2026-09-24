"""Versioned, bounded datasets for the temporal model and the interval model.

Formats: CSV (documented columns below) or the equivalent JSON {"rows": [...]}. Values are
integers only (a spreadsheet formula such as =SUM(...) is refused as `not_integer`; formulas
are never evaluated). Diagnostics carry row and column identifiers, never values.

  temporal_series:   duration_s, harvest_low_mW, harvest_high_mW, load_low_mW, load_high_mW
                     [, leakage_low_mW, leakage_high_mW] [, t_start_s monotonic]
  energy_intervals:  duration_s, power_low_mW, power_high_mW

Each version stores the raw bytes (age-encrypted artifact, digest of raw bytes) and a
deterministic normalized form (age-encrypted artifact) whose salted Merkle commitment is the
externally shareable identity. The raw digest and the normalized commitment are distinct.
Provenance labels (declared / imported / measured_by_named_source / synthetic) are owner
statements, not sensor verification. A declared license is information, not clearance.
"""
import csv
import io
import json
import re
import secrets
from experiments.private_receipts import receipt as merkle
from . import history
from .db import now
from .errors import ServiceError

KINDS = {
    'temporal_series': {'required': ['duration_s', 'harvest_low_mW', 'harvest_high_mW', 'load_low_mW', 'load_high_mW'],
                        'optional': ['leakage_low_mW', 'leakage_high_mW', 't_start_s'],
                        'pairs': [('harvest_low_mW', 'harvest_high_mW'), ('load_low_mW', 'load_high_mW'), ('leakage_low_mW', 'leakage_high_mW')]},
    'energy_intervals': {'required': ['duration_s', 'power_low_mW', 'power_high_mW'], 'optional': ['t_start_s'],
                         'pairs': [('power_low_mW', 'power_high_mW')]},
}
NORMALIZATION = 'dataset-normalize/v1'
INTEGER = re.compile(r'^-?(0|[1-9][0-9]{0,15})$')
PROVENANCE = ('synthetic', 'declared', 'imported', 'measured_by_named_source')
UNITS = {'duration': 's', 'power': 'mW'}


def _rows_from_csv(text, limits):
    lines = text.splitlines()
    for n, line in enumerate(lines, 1):
        if len(line) > limits['max_line_chars']:
            raise ServiceError('VALIDATION', {'row': n, 'code': 'line_too_long'})
    reader = csv.reader(io.StringIO(text), strict=True)
    try:
        rows = list(reader)
    except csv.Error:
        raise ServiceError('VALIDATION', {'code': 'csv_syntax'}) from None
    if not rows:
        raise ServiceError('VALIDATION', {'code': 'empty'})
    header = [h.strip() for h in rows[0]]
    return header, [[c.strip() for c in r] for r in rows[1:] if any(c.strip() for c in r)]


def _rows_from_json(text):
    data = merkle.parse(text.encode('utf-8'))
    if type(data) is not dict or set(data) != {'rows'} or type(data['rows']) is not list or not data['rows']:
        raise ServiceError('VALIDATION', {'code': 'json_shape', 'expected': '{"rows": [{column: integer, ...}]}'})
    header = list(data['rows'][0]) if type(data['rows'][0]) is dict else []
    body = []
    for r in data['rows']:
        if type(r) is not dict or list(r) != header:
            raise ServiceError('VALIDATION', {'code': 'inconsistent_columns'})
        body.append([str(r[h]) if type(r[h]) is int and type(r[h]) is not bool else '' for h in header])
    return header, body


def normalize(kind, content, fmt, limits):
    """Deterministic normalization to integer rows; returns (rows, columns, diagnostics-free). Raises VALIDATION with diagnostics."""
    if kind not in KINDS:
        raise ServiceError('VALIDATION', {'code': 'unknown_kind'})
    if len(content) > limits['max_dataset_bytes']:
        raise ServiceError('PAYLOAD_TOO_LARGE', 'dataset')
    try:
        text = content.decode('utf-8', errors='strict')
    except UnicodeDecodeError:
        raise ServiceError('VALIDATION', {'code': 'encoding_not_utf8'}) from None
    if text.startswith('﻿'):
        text = text[1:]
    header, body = _rows_from_csv(text, limits) if fmt == 'csv' else _rows_from_json(text)
    spec = KINDS[kind]
    diagnostics = []
    if len(header) != len(set(header)):
        diagnostics.append({'code': 'duplicate_column', 'columns': sorted({h for h in header if header.count(h) > 1})})
    missing = [c for c in spec['required'] if c not in header]
    unknown = [c for c in header if c not in spec['required'] + spec['optional']]
    if missing:
        diagnostics.append({'code': 'missing_columns', 'columns': missing})
    if unknown:
        diagnostics.append({'code': 'unknown_columns', 'columns': unknown})
    if diagnostics:
        raise ServiceError('VALIDATION', {'code': 'header', 'diagnostics': diagnostics})
    if not 1 <= len(body) <= limits['max_dataset_rows']:
        raise ServiceError('VALIDATION', {'code': 'row_limit', 'rows': len(body), 'max': limits['max_dataset_rows']})
    index = {h: i for i, h in enumerate(header)}
    rows, last_t = [], None
    for n, r in enumerate(body, 1):
        if len(r) != len(header):
            diagnostics.append({'row': n, 'code': 'column_count'}); continue
        row = {}
        for col in header:
            raw = r[index[col]]
            if raw == '':
                diagnostics.append({'row': n, 'column': col, 'code': 'missing'}); continue
            if not INTEGER.match(raw):
                diagnostics.append({'row': n, 'column': col, 'code': 'not_integer'}); continue
            row[col] = int(raw)
        if any(d.get('row') == n for d in diagnostics):
            continue
        if row['duration_s'] < 1:
            diagnostics.append({'row': n, 'column': 'duration_s', 'code': 'not_positive'})
        for col in header:
            if col != 't_start_s' and row[col] < 0:
                diagnostics.append({'row': n, 'column': col, 'code': 'negative'})
        for lo, hi in spec['pairs']:
            if lo in row and hi in row and row[lo] > row[hi]:
                diagnostics.append({'row': n, 'column': lo, 'code': 'reversed_bounds'})
        if 't_start_s' in row:
            if last_t is not None and row['t_start_s'] < last_t:
                diagnostics.append({'row': n, 'column': 't_start_s', 'code': 'non_monotonic_timestamp'})
            last_t = row['t_start_s']
        rows.append(row)
        if len(diagnostics) > 50:
            break
    if diagnostics:
        raise ServiceError('VALIDATION', {'code': 'rows', 'diagnostics': diagnostics[:50]})
    horizon = sum(r['duration_s'] for r in rows)
    if horizon > 10 ** 9:
        raise ServiceError('VALIDATION', {'code': 'horizon_limit'})
    columns = spec['required'] + [c for c in spec['optional'] if c in header]
    normalized = [{c: r.get(c, 0) for c in columns} for r in rows]
    return normalized, columns


class Datasets:
    def __init__(self, store, settings):
        self.store, self.settings = store, settings

    def _dataset(self, db, principal, dataset_id):
        row = db.execute('SELECT * FROM datasets WHERE id=? AND workspace=?', (dataset_id, principal.workspace)).fetchone()
        if row is None:
            raise ServiceError('NOT_FOUND', 'dataset')
        return row

    def create(self, db, principal, *, name, kind, fmt, content, provenance, source='', license='', tags=None, dataset_id=None, parent_version_id=None):
        principal.require('contract:create')
        if type(name) is not str or not 1 <= len(name) <= 128 or provenance not in PROVENANCE or fmt not in ('csv', 'json'):
            raise ServiceError('VALIDATION', 'dataset fields')
        if type(source) is not str or len(source) > 256 or type(license) is not str or len(license) > 128:
            raise ServiceError('VALIDATION', 'dataset fields')
        tags = tags or []
        if type(tags) is not list or len(tags) > 10 or not all(type(t) is str and 1 <= len(t) <= 32 for t in tags):
            raise ServiceError('VALIDATION', 'tags')
        count = db.execute('SELECT COUNT(*) FROM dataset_versions v JOIN datasets d ON d.id=v.dataset_id WHERE d.workspace=?', (principal.workspace,)).fetchone()[0]
        if count >= self.settings.limits['max_dataset_versions_per_workspace']:
            raise ServiceError('RATE_LIMITED', 'dataset version quota')
        rows, columns = normalize(kind, content, fmt, self.settings.limits)
        if dataset_id is None:
            dataset_id = 'ds_' + secrets.token_hex(8)
            db.execute('INSERT INTO datasets VALUES (?,?,?,?,?,?,?,NULL)', (dataset_id, principal.workspace, principal.id, name, kind, json.dumps(tags), now()))
            version = 1
        else:
            ds = self._dataset(db, principal, dataset_id)
            if ds['owner_id'] != principal.id or ds['kind'] != kind or ds['retired_at'] is not None:
                raise ServiceError('CONFLICT', 'dataset owner/kind mismatch or retired')
            version = db.execute('SELECT COALESCE(MAX(version),0)+1 FROM dataset_versions WHERE dataset_id=?', (dataset_id,)).fetchone()[0]
        import hashlib
        raw_id = self.store.store(db, workspace=principal.workspace, kind='dataset_raw', owner_id=principal.id, plaintext=content,
                                  recipients=[], intended_use='dataset-raw-bytes;owner-only')
        receipt, vault = merkle.commit({'rows': rows})
        norm_id = self.store.store(db, workspace=principal.workspace, kind='dataset_normalized', owner_id=principal.id,
                                   plaintext=merkle.canonical(vault), recipients=[], intended_use='dataset-normalized-vault;owner-worker-reviewer')
        vid = 'dv_' + secrets.token_hex(8)
        db.execute('INSERT INTO dataset_versions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,?,?,?,NULL)',
                   (vid, dataset_id, version, raw_id, norm_id, hashlib.sha256(content).hexdigest(), receipt['root'], NORMALIZATION,
                    len(rows), len(content), json.dumps(columns), json.dumps(UNITS), provenance, source, license, 'private',
                    parent_version_id, 'upload' if parent_version_id is None else 'new-bytes-from-parent', now()))
        history.record(db, principal.workspace, principal.id, 'contract.created', 'dataset_version', vid,
                       {'dataset_id': dataset_id, 'version': version, 'rows': len(rows), 'commitment': receipt['root']})
        add_edge(db, principal.workspace, 'artifact', raw_id, 'dataset_version', vid, 'normalized_from')
        if parent_version_id:
            add_edge(db, principal.workspace, 'dataset_version', parent_version_id, 'dataset_version', vid, 'derived_from')
        return {'dataset_id': dataset_id, 'version_id': vid, 'version': version, 'rows': len(rows), 'columns': columns,
                'normalized_commitment': receipt['root'], 'raw_sha256': hashlib.sha256(content).hexdigest()}

    def list(self, db, principal, *, kind=None, tag=None, limit=50):
        principal.require('contract:read')
        sql, args = 'SELECT d.*, (SELECT COUNT(*) FROM dataset_versions v WHERE v.dataset_id=d.id) AS versions FROM datasets d WHERE d.workspace=?', [principal.workspace]
        if kind:
            sql += ' AND d.kind=?'; args.append(kind)
        rows = db.execute(sql + ' ORDER BY d.created_at DESC, d.id LIMIT ?', args + [min(int(limit), 100)]).fetchall()
        out = [dict(r, tags=json.loads(r['tags'])) for r in rows]
        return [r for r in out if tag is None or tag in r['tags']]

    def detail(self, db, principal, dataset_id):
        ds = self._dataset(db, principal, dataset_id)
        versions = db.execute('SELECT * FROM dataset_versions WHERE dataset_id=? ORDER BY version', (dataset_id,)).fetchall()
        return {'dataset': dict(ds, tags=json.loads(ds['tags'])), 'versions': [self.version_view(v) for v in versions]}

    def version(self, db, principal, version_id):
        row = db.execute('SELECT v.*, d.workspace, d.owner_id AS ds_owner, d.kind FROM dataset_versions v JOIN datasets d ON d.id=v.dataset_id WHERE v.id=?', (version_id,)).fetchone()
        if row is None or row['workspace'] != principal.workspace:
            raise ServiceError('NOT_FOUND', 'dataset version')
        return row

    @staticmethod
    def version_view(v):
        return {'id': v['id'], 'dataset_id': v['dataset_id'], 'version': v['version'], 'rows': v['row_count'], 'bytes': v['byte_count'],
                'columns': json.loads(v['columns_json']), 'units': json.loads(v['units_json']), 'normalization': v['normalization_id'],
                'raw_sha256': v['raw_sha256'], 'normalized_commitment': v['normalized_commitment'], 'provenance': v['provenance'],
                'provenance_source': v['provenance_source'], 'license': v['license'], 'privacy': v['privacy'],
                'parent_version_id': v['parent_version_id'], 'transformation': v['transformation'], 'created_at': v['created_at'],
                'payload_available': v['deleted_at'] is None}

    def rows(self, db, principal, version_id):
        """Normalized rows for an authorized principal (owner or worker in workspace)."""
        v = self.version(db, principal, version_id)
        if not (principal.can('job:read_private') or principal.role == 'worker'):
            raise ServiceError('FORBIDDEN', 'dataset rows')
        if v['deleted_at'] is not None:
            raise ServiceError('NOT_FOUND', 'dataset payload deleted')
        vault = self.store.load_json(db, v['normalized_artifact_id'], principal.workspace)
        return {f['name']: f['value'] for f in vault['fields']}['rows'], v

    def retire(self, db, principal, dataset_id):
        ds = self._dataset(db, principal, dataset_id)
        if ds['owner_id'] != principal.id:
            raise ServiceError('FORBIDDEN', 'dataset')
        db.execute('UPDATE datasets SET retired_at=? WHERE id=?', (now(), dataset_id))
        return {'retired': dataset_id, 'note': 'versions remain referable; new work under this dataset is refused'}

    def delete_payload(self, db, principal, version_id):
        v = self.version(db, principal, version_id)
        if v['ds_owner'] != principal.id:
            raise ServiceError('FORBIDDEN', 'dataset version')
        for aid in (v['raw_artifact_id'], v['normalized_artifact_id']):
            if aid:
                self.store.delete_payload(db, aid, principal.workspace, principal.id)
        db.execute('UPDATE dataset_versions SET deleted_at=? WHERE id=?', (now(), version_id))
        history.record(db, principal.workspace, principal.id, 'artifact.deleted', 'dataset_version', version_id, {'payload': 'unlinked'})
        return {'version_id': version_id, 'payload_deleted': True, 'note': 'commitment and metadata retained for historical results'}

    def temporal_input(self, rows, params, v):
        """Deterministic model input from normalized rows and explicit parameters."""
        from . import temporal
        allowed = {'capacity', 'initial_low', 'initial_high', 'reserve', 'private_label'}
        if type(params) is not dict or not set(params) <= allowed or not {'capacity', 'initial_low', 'initial_high', 'reserve'} <= set(params):
            raise ServiceError('VALIDATION', 'parameters: capacity, initial_low, initial_high, reserve[, private_label]')
        segments = [{'duration': r['duration_s'], 'harvest_low': r['harvest_low_mW'], 'harvest_high': r['harvest_high_mW'],
                     'load_low': r['load_low_mW'], 'load_high': r['load_high_mW'], 'leakage_low': r.get('leakage_low_mW', 0),
                     'leakage_high': r.get('leakage_high_mW', 0)} for r in rows]
        return {'schema': temporal.INPUT_SCHEMA, 'capacity': params['capacity'], 'initial_low': params['initial_low'],
                'initial_high': params['initial_high'], 'reserve': params['reserve'], 'segments': segments, 'units': dict(temporal.UNITS),
                'assumptions': list(temporal.ASSUMPTIONS), 'provenance': v['provenance'] if v['provenance'] != 'synthetic' else 'synthetic',
                'private_label': params.get('private_label', 'dataset:' + v['id'])}

    def interval_input(self, rows, params, v):
        from experiments.work_contracts import energy_analysis as energy
        allowed = {'available_low', 'available_high', 'reserve', 'private_label'}
        if type(params) is not dict or not set(params) <= allowed or not {'available_low', 'available_high', 'reserve'} <= set(params):
            raise ServiceError('VALIDATION', 'parameters: available_low, available_high, reserve[, private_label]')
        return {'available_low': params['available_low'], 'available_high': params['available_high'], 'reserve': params['reserve'],
                'segments': [{'duration': r['duration_s'], 'power_low': r['power_low_mW'], 'power_high': r['power_high_mW']} for r in rows],
                'units': dict(energy.UNITS), 'assumptions': list(energy.ASSUMPTIONS),
                'provenance': 'synthetic' if v['provenance'] == 'synthetic' else 'declared_unverified',
                'private_label': params.get('private_label', 'dataset:' + v['id'])}


def add_edge(db, workspace, from_type, from_id, to_type, to_id, relation):
    db.execute('INSERT INTO lineage_edges (workspace, from_type, from_id, to_type, to_id, relation, created_at) VALUES (?,?,?,?,?,?,?)',
               (workspace, from_type, from_id, to_type, to_id, relation, now()))


def lineage(db, principal, object_type, object_id, depth=4, limit=200):
    """Bounded ancestor/descendant walk within the principal's workspace."""
    seen, edges, frontier = {(object_type, object_id)}, [], [(object_type, object_id)]
    for _ in range(max(1, min(depth, 8))):
        nxt = []
        for t, i in frontier:
            for r in db.execute('SELECT * FROM lineage_edges WHERE workspace=? AND ((from_type=? AND from_id=?) OR (to_type=? AND to_id=?)) LIMIT ?',
                                (principal.workspace, t, i, t, i, limit)):
                edge = {'from': [r['from_type'], r['from_id']], 'to': [r['to_type'], r['to_id']], 'relation': r['relation'], 'at': r['created_at']}
                if edge not in edges:
                    edges.append(edge)
                for key in ((r['from_type'], r['from_id']), (r['to_type'], r['to_id'])):
                    if key not in seen:
                        seen.add(key); nxt.append(key)
                if len(edges) >= limit:
                    return {'root': [object_type, object_id], 'edges': edges, 'truncated': True}
        frontier = nxt
    return {'root': [object_type, object_id], 'edges': edges, 'truncated': False,
            'prov_mapping': {'dataset_version': 'prov:Entity', 'artifact': 'prov:Entity', 'contract': 'prov:Entity', 'job': 'prov:Activity',
                             'review': 'prov:Activity', 'principal': 'prov:Agent', 'derived_from': 'prov:wasDerivedFrom',
                             'normalized_from': 'prov:wasDerivedFrom', 'used_input': 'prov:used', 'produced': 'prov:wasGeneratedBy'},
            'note': 'internal lineage with a documented PROV mapping; not a claim of full PROV conformance'}


PROV_TYPES = {'dataset_version': 'entity', 'artifact': 'entity', 'contract': 'entity', 'quote': 'entity', 'usage': 'entity', 'service': 'entity',
              'workflow_definition': 'entity', 'job': 'activity', 'review': 'activity', 'workflow_run': 'activity', 'campaign': 'activity', 'principal': 'agent'}
PROV_RELATIONS = {'used_input': 'used', 'produced': 'wasGeneratedBy', 'derived_from': 'wasDerivedFrom', 'normalized_from': 'wasDerivedFrom',
                  'reviewed': 'wasInformedBy', 'reused_result': 'wasInformedBy', 'quoted': 'wasDerivedFrom', 'metered': 'wasGeneratedBy'}


def prov_export(db, principal, object_type, object_id, depth=4):
    """PROV-JSON (W3C PROV-JSON serialization shape) of the bounded lineage around one object.
    Identifiers and public digests only: never inputs, summaries or private labels."""
    principal.require('job:read')
    graph = lineage(db, principal, object_type, object_id, depth=depth)
    doc = {'prefix': {'metacoin': 'urn:metacoin:', 'prov': 'http://www.w3.org/ns/prov#'}, 'entity': {}, 'activity': {}, 'agent': {}, 'used': {}, 'wasGeneratedBy': {}, 'wasDerivedFrom': {}, 'wasInformedBy': {}, 'wasAssociatedWith': {}}
    def qid(t, i):
        return 'metacoin:' + t + '/' + i
    def declare(t, i):
        cls = PROV_TYPES.get(t)
        if cls is None:
            return None
        key = qid(t, i)
        if key not in doc[cls]:
            attrs = {'prov:type': 'metacoin:' + t}
            if t == 'job':
                j = db.execute('SELECT kind, state, evidence_root, submitted_by FROM jobs WHERE id=? AND workspace=?', (i, principal.workspace)).fetchone()
                if j:
                    attrs.update({'metacoin:kind': j['kind'], 'metacoin:state': j['state'], 'metacoin:evidence_root': j['evidence_root']})
                    doc['agent'].setdefault(qid('principal', j['submitted_by']), {'prov:type': 'metacoin:principal'})
                    doc['wasAssociatedWith']['_:assoc_' + i] = {'prov:activity': key, 'prov:agent': qid('principal', j['submitted_by'])}
            elif t == 'dataset_version':
                v = db.execute('SELECT normalized_commitment, version FROM dataset_versions WHERE id=?', (i,)).fetchone()
                if v:
                    attrs.update({'metacoin:commitment': v['normalized_commitment'], 'metacoin:version': v['version']})
            elif t == 'contract':
                c = db.execute('SELECT contract_digest, kind FROM contracts WHERE id=? AND workspace=?', (i, principal.workspace)).fetchone()
                if c:
                    attrs.update({'metacoin:contract_digest': c['contract_digest'], 'metacoin:kind': c['kind']})
            doc[cls][key] = attrs
        return key, cls
    n = 0
    for e in graph['edges']:
        a = declare(*e['from']); b = declare(*e['to'])
        if a is None or b is None:
            continue
        rel = PROV_RELATIONS.get(e['relation'])
        if rel is None:
            continue
        n += 1
        (ka, ca), (kb, cb) = a, b
        if rel == 'used':                                     # activity used entity
            act, ent = (kb, ka) if cb == 'activity' else (ka, kb)
            doc['used']['_:u%d' % n] = {'prov:activity': act, 'prov:entity': ent}
        elif rel == 'wasGeneratedBy':                         # entity generated by activity
            act, ent = (ka, kb) if ca == 'activity' else (kb, ka)
            doc['wasGeneratedBy']['_:g%d' % n] = {'prov:entity': ent, 'prov:activity': act}
        elif rel == 'wasDerivedFrom':
            doc['wasDerivedFrom']['_:d%d' % n] = {'prov:generatedEntity': kb, 'prov:usedEntity': ka}
        elif rel == 'wasInformedBy':
            doc['wasInformedBy']['_:i%d' % n] = {'prov:informed': kb, 'prov:informant': ka}
    doc['metacoin:root'] = qid(*graph['root']); doc['metacoin:truncated'] = graph['truncated']
    return doc

