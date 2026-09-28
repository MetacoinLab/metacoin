"""Collections, immutable document versions, deterministic chunks, index versions, revocation and answer records.

Authorization: every operation is scoped to the caller's workspace and requires knowledge:write (owner) for mutation
and knowledge:read for content (owner). Listings, counts and error messages never mention another workspace's
records. Normalized text lives in encrypted artifacts; chunk rows hold offsets and digests only."""
import hashlib
import json
import secrets

from experiments.private_receipts import receipt as merkle
from .. import history
from ..datasets import add_edge
from ..db import now
from ..errors import ServiceError
from . import text as text_mod

INDEX_SIMILARITY = 'cosine over L2-normalized mean-pooled vectors (dot product)'
INDEX_SCHEMA = 'knowledge-index/v1'


class Knowledge:
    def __init__(self, store, settings):
        self.store, self.settings = store, settings

    # ---- collections ------------------------------------------------------------------------------
    def create_collection(self, db, principal, name, description=''):
        principal.require('knowledge:write')
        if type(name) is not str or not 1 <= len(name) <= 96 or type(description) is not str or len(description) > 512:
            raise ServiceError('VALIDATION', 'name/description')
        n = db.execute('SELECT COUNT(*) FROM knowledge_collections WHERE workspace=? AND retired_at IS NULL', (principal.workspace,)).fetchone()[0]
        if n >= self.settings.limits['knowledge_max_collections']:
            raise ServiceError('RATE_LIMITED', 'collection quota')
        cid = 'kc_' + secrets.token_hex(6)
        db.execute('INSERT INTO knowledge_collections (id, workspace, owner_id, name, description, created_at) VALUES (?,?,?,?,?,?)', (cid, principal.workspace, principal.id, name, description, now()))
        history.record(db, principal.workspace, principal.id, 'knowledge.collection', 'collection', cid, {'created': True})
        return self.collection_view(db, self.collection(db, principal, cid))

    def collection(self, db, principal, cid):
        row = db.execute('SELECT * FROM knowledge_collections WHERE id=? AND workspace=?', (cid, principal.workspace)).fetchone()
        if row is None:
            raise ServiceError('NOT_FOUND', 'collection')
        return row

    def collection_view(self, db, row):
        docs = db.execute('SELECT COUNT(*) AS n, SUM(CASE WHEN revoked_at IS NOT NULL THEN 1 ELSE 0 END) AS revoked FROM knowledge_documents WHERE collection_id=?', (row['id'],)).fetchone()
        idx = db.execute('SELECT id, version, state, model_revision_id, chunk_count, built_at, stale_reason FROM knowledge_indexes WHERE collection_id=? ORDER BY version DESC LIMIT 5', (row['id'],)).fetchall()
        return {'id': row['id'], 'name': row['name'], 'description': row['description'], 'owner_id': row['owner_id'], 'created_at': row['created_at'], 'retired_at': row['retired_at'],
                'documents': docs['n'], 'revoked_documents': docs['revoked'] or 0, 'indexes': [dict(r) for r in idx]}

    def list_collections(self, db, principal):
        principal.require('knowledge:read')
        return [self.collection_view(db, r) for r in db.execute('SELECT * FROM knowledge_collections WHERE workspace=? ORDER BY created_at', (principal.workspace,)).fetchall()]

    # ---- documents ---------------------------------------------------------------------------------
    def add_document(self, db, principal, cid, *, name, fmt, content, provenance='declared', source='', license='', document_id=None):
        principal.require('knowledge:write')
        col = self.collection(db, principal, cid)
        if col['retired_at'] is not None:
            raise ServiceError('CONFLICT', 'collection retired')
        if type(name) is not str or not 1 <= len(name) <= 128 or type(content) is not bytes:
            raise ServiceError('VALIDATION', 'name/content')
        if provenance not in ('declared', 'synthetic', 'project_documentation') or type(source) is not str or len(source) > 256 or type(license) is not str or len(license) > 128:
            raise ServiceError('VALIDATION', 'provenance/source/license')
        n = db.execute('SELECT COUNT(*) FROM knowledge_documents WHERE collection_id=?', (cid,)).fetchone()[0]
        if document_id is None and n >= self.settings.limits['knowledge_max_documents_per_collection']:
            raise ServiceError('RATE_LIMITED', 'document quota for this collection')
        try:
            text, warnings = text_mod.normalize(fmt, content, self.settings.limits)
        except ValueError as exc:
            raise ServiceError('VALIDATION', {'code': 'document_unparseable', 'reason': str(exc)}) from None
        chunks = text_mod.chunk(text)
        if document_id is None:
            document_id = 'kd_' + secrets.token_hex(6)
            db.execute('INSERT INTO knowledge_documents (id, collection_id, workspace, name, created_at) VALUES (?,?,?,?,?)', (document_id, cid, principal.workspace, name, now()))
            version = 1
        else:
            doc = self.document(db, principal, document_id)
            if doc['collection_id'] != cid or doc['revoked_at'] is not None:
                raise ServiceError('CONFLICT', 'document not in this collection or revoked')
            version = db.execute('SELECT COALESCE(MAX(version),0)+1 FROM knowledge_versions WHERE document_id=?', (document_id,)).fetchone()[0]
        raw_id = self.store.store(db, workspace=principal.workspace, kind='knowledge_document', owner_id=principal.id, plaintext=content, recipients=[], intended_use='knowledge-raw-bytes;owner-only',
                                  limit_bytes=self.settings.limits['knowledge_max_document_bytes'])
        text_bytes = text.encode('utf-8')
        text_id = self.store.store(db, workspace=principal.workspace, kind='knowledge_document', owner_id=principal.id, plaintext=text_bytes, recipients=[], intended_use='knowledge-normalized-text;owner-worker',
                                   limit_bytes=self.settings.limits['knowledge_max_document_bytes'] * 2)
        vid = 'kv_' + secrets.token_hex(6)
        db.execute('INSERT INTO knowledge_versions (id, document_id, collection_id, workspace, version, format, raw_artifact_id, text_artifact_id, text_sha256, chars, parser_id, warnings_json, provenance, source, license, chunker_id, chunk_count, created_at) '
                   'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (vid, document_id, cid, principal.workspace, version, fmt, raw_id, text_id, hashlib.sha256(text_bytes).hexdigest(), len(text), text_mod.PARSER_ID,
                                                                    json.dumps(warnings), provenance, source, license, text_mod.CHUNKER_ID, len(chunks), now()))
        for c in chunks:
            db.execute('INSERT INTO knowledge_chunks (version_id, ordinal, start_byte, end_byte, heading, sha256, chars) VALUES (?,?,?,?,?,?,?)', (vid, c['ordinal'], c['start'], c['end'], c['heading'], c['sha256'], c['chars']))
        db.execute('UPDATE knowledge_documents SET current_version_id=? WHERE id=?', (vid, document_id))
        add_edge(db, principal.workspace, 'artifact', raw_id, 'knowledge_version', vid, 'normalized_from')
        add_edge(db, principal.workspace, 'artifact', text_id, 'knowledge_version', vid, 'chunked_from')
        # indexes built over an older version are now stale for this document (still queryable; results flagged)
        for idx in db.execute("SELECT id, document_versions_json FROM knowledge_indexes WHERE collection_id=? AND state='ready'", (cid,)).fetchall():
            if document_id in json.loads(idx['document_versions_json']):
                db.execute("UPDATE knowledge_indexes SET stale_reason=COALESCE(stale_reason, ?) WHERE id=?", ('document %s has a newer version' % document_id, idx['id']))
        history.record(db, principal.workspace, principal.id, 'knowledge.document', 'knowledge_version', vid, {'document_id': document_id, 'version': version, 'format': fmt, 'chunks': len(chunks), 'warnings': len(warnings)})
        return self.version_view(db, db.execute('SELECT * FROM knowledge_versions WHERE id=?', (vid,)).fetchone(), private=True)

    def publish_extraction(self, db, principal, cid, *, name, text, page_map, raw_artifact_id, text_artifact_id, parser, warnings, document_id=None, include_excluded=False, source_sha256=None):
        """A document version from an extraction revision: format pdf, page-aware chunks (page index + page-level region),
        the original bytes as the raw artifact and the normalized retrieval text as the text artifact."""
        principal.require('knowledge:write')
        col = self.collection(db, principal, cid)
        if col['retired_at'] is not None:
            raise ServiceError('CONFLICT', 'collection retired')
        excluded = {p['index'] for p in page_map if p.get('excluded')}
        if excluded and not include_excluded:
            # excluded pages contribute only their marker line; their (empty) text never becomes searchable content
            pass
        # page-aware chunking: every chunk lies inside one page (chunks never straddle a page boundary), offsets stay relative to the whole text
        data = text.encode('utf-8'); chunks = []
        for p in page_map:
            if p.get('excluded') and not include_excluded:
                continue
            page_text = data[p['start_byte']:p['end_byte']].decode('utf-8')
            for c in text_mod.chunk(page_text):
                if len(chunks) >= text_mod.MAX_CHUNKS_PER_DOCUMENT:
                    break
                c['start'] += p['start_byte']; c['end'] += p['start_byte']; c['ordinal'] = len(chunks)
                c['page_index'] = p['index']
                c['region'] = {'kind': 'page', 'page_index': p['index'], 'note': 'page-level location: span geometry is kept in the extraction revision, not per chunk'}
                chunks.append(c)
        if not chunks:
            raise ServiceError('VALIDATION', {'code': 'no_publishable_text', 'note': 'every page was excluded; nothing to index'})
        if document_id is None:
            n = db.execute('SELECT COUNT(*) FROM knowledge_documents WHERE collection_id=?', (cid,)).fetchone()[0]
            if n >= self.settings.limits['knowledge_max_documents_per_collection']:
                raise ServiceError('RATE_LIMITED', 'document quota for this collection')
            document_id = 'kd_' + secrets.token_hex(6)
            db.execute('INSERT INTO knowledge_documents (id, collection_id, workspace, name, created_at) VALUES (?,?,?,?,?)', (document_id, cid, principal.workspace, name, now()))
            version = 1
        else:
            doc = self.document(db, principal, document_id)
            if doc['collection_id'] != cid or doc['revoked_at'] is not None:
                raise ServiceError('CONFLICT', 'document not in this collection or revoked')
            version = db.execute('SELECT COALESCE(MAX(version),0)+1 FROM knowledge_versions WHERE document_id=?', (document_id,)).fetchone()[0]
        text_bytes = text.encode('utf-8')
        vid = 'kv_' + secrets.token_hex(6)
        db.execute('INSERT INTO knowledge_versions (id, document_id, collection_id, workspace, version, format, raw_artifact_id, text_artifact_id, text_sha256, chars, parser_id, warnings_json, provenance, source, license, chunker_id, chunk_count, created_at) '
                   'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (vid, document_id, cid, principal.workspace, version, 'pdf', raw_artifact_id, text_artifact_id, hashlib.sha256(text_bytes).hexdigest(), len(text), parser.get('id', 'metacoin-pdf-extractor/v1'),
                                                                    json.dumps(warnings), 'declared', 'document import sha256:' + (source_sha256 or ''), '', text_mod.CHUNKER_ID + '+pages', len(chunks), now()))
        for c in chunks:
            db.execute('INSERT INTO knowledge_chunks (version_id, ordinal, start_byte, end_byte, heading, sha256, chars, page_index, region_json) VALUES (?,?,?,?,?,?,?,?,?)', (vid, c['ordinal'], c['start'], c['end'], c['heading'], c['sha256'], c['chars'], c['page_index'], json.dumps(c['region'])))
        db.execute('UPDATE knowledge_documents SET current_version_id=? WHERE id=?', (vid, document_id))
        add_edge(db, principal.workspace, 'artifact', raw_artifact_id, 'knowledge_version', vid, 'normalized_from')
        add_edge(db, principal.workspace, 'artifact', text_artifact_id, 'knowledge_version', vid, 'chunked_from')
        for idx in db.execute("SELECT id, document_versions_json FROM knowledge_indexes WHERE collection_id=? AND state='ready'", (cid,)).fetchall():
            if document_id in json.loads(idx['document_versions_json']):
                db.execute("UPDATE knowledge_indexes SET stale_reason=COALESCE(stale_reason, ?) WHERE id=?", ('document %s has a newer version' % document_id, idx['id']))
        history.record(db, principal.workspace, principal.id, 'knowledge.document', 'knowledge_version', vid, {'document_id': document_id, 'version': version, 'format': 'pdf', 'chunks': len(chunks), 'pages': len(page_map), 'excluded_pages': len(excluded)})
        return self.version_view(db, db.execute('SELECT * FROM knowledge_versions WHERE id=?', (vid,)).fetchone(), private=True)

    def document(self, db, principal, did):
        row = db.execute('SELECT * FROM knowledge_documents WHERE id=? AND workspace=?', (did, principal.workspace)).fetchone()
        if row is None:
            raise ServiceError('NOT_FOUND', 'document')
        return row

    def version(self, db, principal, vid):
        row = db.execute('SELECT * FROM knowledge_versions WHERE id=? AND workspace=?', (vid, principal.workspace)).fetchone()
        if row is None:
            raise ServiceError('NOT_FOUND', 'document version')
        return row

    def version_view(self, db, v, private=False):
        doc = db.execute('SELECT name, revoked_at, current_version_id FROM knowledge_documents WHERE id=?', (v['document_id'],)).fetchone()
        out = {'id': v['id'], 'document_id': v['document_id'], 'collection_id': v['collection_id'], 'version': v['version'], 'format': v['format'], 'text_sha256': v['text_sha256'], 'chars': v['chars'],
               'parser_id': v['parser_id'], 'chunker_id': v['chunker_id'], 'chunk_count': v['chunk_count'], 'provenance': v['provenance'], 'created_at': v['created_at'], 'deleted_at': v['deleted_at'],
               'current': doc['current_version_id'] == v['id'], 'revoked': doc['revoked_at'] is not None}
        if private:
            out.update(name=doc['name'], warnings=json.loads(v['warnings_json'] or '[]'), source=v['source'], license=v['license'])
        return out

    def list_documents(self, db, principal, cid):
        principal.require('knowledge:read')
        self.collection(db, principal, cid)
        out = []
        for d in db.execute('SELECT * FROM knowledge_documents WHERE collection_id=? ORDER BY created_at', (cid,)).fetchall():
            vs = db.execute('SELECT * FROM knowledge_versions WHERE document_id=? ORDER BY version', (d['id'],)).fetchall()
            out.append({'id': d['id'], 'name': d['name'], 'revoked_at': d['revoked_at'], 'revocation_reason': d['revocation_reason'], 'current_version_id': d['current_version_id'],
                        'versions': [self.version_view(db, v) for v in vs]})
        return out

    def text_of(self, db, workspace, version_row):
        if version_row['deleted_at'] is not None:
            raise ServiceError('NOT_FOUND', 'normalized text payload deleted under retention policy')
        return self.store.load(db, version_row['text_artifact_id'], workspace)

    def preview(self, db, principal, vid, ordinal=None):
        """Authorized source preview (owner): the chunk text or the first 2000 characters."""
        principal.require('knowledge:read')
        v = self.version(db, principal, vid)
        doc = db.execute('SELECT * FROM knowledge_documents WHERE id=?', (v['document_id'],)).fetchone()
        if doc['revoked_at'] is not None:
            raise ServiceError('FORBIDDEN', {'code': 'source_revoked', 'reason': doc['revocation_reason']})
        data = self.text_of(db, principal.workspace, v)
        if ordinal is None:
            return {'version_id': vid, 'document_id': doc['id'], 'name': doc['name'], 'text': data[:2000].decode('utf-8', errors='replace'), 'truncated': len(data) > 2000}
        c = db.execute('SELECT * FROM knowledge_chunks WHERE version_id=? AND ordinal=?', (vid, ordinal)).fetchone()
        if c is None:
            raise ServiceError('NOT_FOUND', 'chunk')
        return {'version_id': vid, 'document_id': doc['id'], 'name': doc['name'], 'ordinal': ordinal, 'heading': c['heading'], 'text': data[c['start_byte']:c['end_byte']].decode('utf-8'), 'sha256': c['sha256'],
                'location': {'start_byte': c['start_byte'], 'end_byte': c['end_byte'], 'of_normalized_text_sha256': v['text_sha256']}}

    def revoke_document(self, db, principal, did, reason=''):
        """Revocation reaches retrieval (chunks excluded at selection), indexes (flagged stale) and answers (invalidated)."""
        principal.require('knowledge:write')
        doc = self.document(db, principal, did)
        if type(reason) is not str or len(reason) > 256:
            raise ServiceError('VALIDATION', 'reason')
        if doc['revoked_at'] is not None:
            return {'document_id': did, 'already_revoked': True}
        db.execute('UPDATE knowledge_documents SET revoked_at=?, revocation_reason=? WHERE id=?', (now(), reason, did))
        stale = 0
        for idx in db.execute("SELECT id, document_versions_json FROM knowledge_indexes WHERE collection_id=? AND state='ready'", (doc['collection_id'],)).fetchall():
            if did in json.loads(idx['document_versions_json']):
                db.execute("UPDATE knowledge_indexes SET stale_reason=COALESCE(stale_reason, ?) WHERE id=?", ('document %s revoked' % did, idx['id'])); stale += 1
        invalidated = []
        for a in db.execute("SELECT id, sources_json FROM knowledge_answers WHERE workspace=? AND invalidated_at IS NULL", (principal.workspace,)).fetchall():
            if any(s.get('document_id') == did for s in json.loads(a['sources_json'])):
                db.execute("UPDATE knowledge_answers SET invalidated_at=?, invalidation_reason=? WHERE id=?", (now(), 'source document revoked: ' + did, a['id'])); invalidated.append(a['id'])
        history.record(db, principal.workspace, principal.id, 'knowledge.revoked', 'document', did, {'reason': reason, 'indexes_flagged': stale, 'answers_invalidated': invalidated})
        return {'document_id': did, 'revoked_at': now(), 'indexes_flagged_stale': stale, 'answers_invalidated': invalidated,
                'limits': 'future retrieval, previews, cached answers and index selection exclude this document; copies already delivered to a client cannot be recalled; backups keep ciphertext until their own retention'}

    def delete_version_payload(self, db, principal, vid):
        principal.require('artifact:delete')
        v = self.version(db, principal, vid)
        if v['deleted_at'] is not None:
            return {'version_id': vid, 'already_deleted': True}
        self.store.delete_payload(db, v['text_artifact_id'], principal.workspace, 'knowledge retention')
        self.store.delete_payload(db, v['raw_artifact_id'], principal.workspace, 'knowledge retention')
        db.execute('UPDATE knowledge_versions SET deleted_at=? WHERE id=?', (now(), vid))
        invalidated = []
        for a in db.execute("SELECT id, sources_json FROM knowledge_answers WHERE workspace=? AND invalidated_at IS NULL", (principal.workspace,)).fetchall():
            if any(s.get('version_id') == vid for s in json.loads(a['sources_json'])):
                db.execute("UPDATE knowledge_answers SET invalidated_at=?, invalidation_reason=? WHERE id=?", (now(), 'source payload deleted: ' + vid, a['id'])); invalidated.append(a['id'])
        history.record(db, principal.workspace, principal.id, 'artifact.deleted', 'knowledge_version', vid, {'answers_invalidated': invalidated})
        return {'version_id': vid, 'deleted_at': now(), 'answers_invalidated': invalidated, 'note': 'chunk digests and offsets remain as a non-secret record; the text is gone from this host (not secure erasure)'}

    # ---- indexes -----------------------------------------------------------------------------------
    def create_index(self, db, principal, cid, model_row):
        """Snapshot the current non-revoked versions and record a building index; the worker job embeds and publishes."""
        principal.require('knowledge:write')
        col = self.collection(db, principal, cid)
        docs = db.execute('SELECT id, current_version_id FROM knowledge_documents WHERE collection_id=? AND revoked_at IS NULL AND current_version_id IS NOT NULL', (cid,)).fetchall()
        versions = {d['id']: d['current_version_id'] for d in docs}
        if not versions:
            raise ServiceError('CONFLICT', 'collection has no indexable documents')
        total = db.execute('SELECT COALESCE(SUM(chunk_count),0) FROM knowledge_versions WHERE id IN (%s)' % ','.join('?' * len(versions)), tuple(versions.values())).fetchone()[0]
        if total > self.settings.limits['knowledge_max_chunks_per_index']:
            raise ServiceError('VALIDATION', {'code': 'too_many_chunks', 'chunks': total, 'limit': self.settings.limits['knowledge_max_chunks_per_index']})
        version = db.execute('SELECT COALESCE(MAX(version),0)+1 FROM knowledge_indexes WHERE collection_id=?', (cid,)).fetchone()[0]
        iid = 'ki_' + secrets.token_hex(6)
        db.execute('INSERT INTO knowledge_indexes (id, collection_id, workspace, version, state, model_revision_id, chunker_id, embedding_dim, normalization, similarity, document_versions_json, chunk_count, created_at) '
                   'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)', (iid, cid, principal.workspace, version, 'building', model_row['id'], text_mod.CHUNKER_ID, model_row['embedding_dim'], 'l2-normalized ' + str(model_row['pooling']) + ' pooling',
                                                        INDEX_SIMILARITY, json.dumps(versions), total, now()))
        history.record(db, principal.workspace, principal.id, 'knowledge.index', 'index', iid, {'collection_id': cid, 'version': version, 'documents': len(versions), 'chunks': total, 'model_revision_id': model_row['id']})
        return iid, versions, total

    def index(self, db, principal, iid):
        row = db.execute('SELECT * FROM knowledge_indexes WHERE id=? AND workspace=?', (iid, principal.workspace)).fetchone()
        if row is None:
            raise ServiceError('NOT_FOUND', 'index')
        return row

    def index_view(self, db, row):
        return {'id': row['id'], 'collection_id': row['collection_id'], 'version': row['version'], 'state': row['state'], 'stale_reason': row['stale_reason'], 'model_revision_id': row['model_revision_id'],
                'chunker_id': row['chunker_id'], 'embedding_dim': row['embedding_dim'], 'normalization': row['normalization'], 'similarity': row['similarity'],
                'document_versions': json.loads(row['document_versions_json']), 'chunk_count': row['chunk_count'], 'job_id': row['index_job_id'], 'built_at': row['built_at'], 'error': row['error'], 'created_at': row['created_at'],
                'storage': 'vectors + chunk manifest in one age-encrypted artifact; decrypted only in the process that answers a query; never exported'}

    def latest_ready_index(self, db, cid):
        return db.execute("SELECT * FROM knowledge_indexes WHERE collection_id=? AND state='ready' ORDER BY version DESC LIMIT 1", (cid,)).fetchone()

    # ---- answers -----------------------------------------------------------------------------------
    def record_answer(self, db, workspace, principal_id, cid, iid, job_id, mode, question, status, sources, citations):
        aid = 'ka_' + secrets.token_hex(6)
        db.execute('INSERT INTO knowledge_answers (id, workspace, collection_id, index_id, job_id, principal_id, mode, question_sha256, status, sources_json, citations_json, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                   (aid, workspace, cid, iid, job_id, principal_id, mode, hashlib.sha256(question.encode()).hexdigest(), status, json.dumps(sources), json.dumps(citations), now()))
        return aid

    def answer(self, db, principal, aid, store):
        principal.require('knowledge:read')
        row = db.execute('SELECT * FROM knowledge_answers WHERE id=? AND workspace=?', (aid, principal.workspace)).fetchone()
        if row is None:
            raise ServiceError('NOT_FOUND', 'answer')
        if row['principal_id'] != principal.id and not principal.can('model:admin'):
            raise ServiceError('FORBIDDEN', 'answers are private to the principal who asked (and the workspace owner)')
        out = {'id': aid, 'collection_id': row['collection_id'], 'index_id': row['index_id'], 'job_id': row['job_id'], 'mode': row['mode'], 'status': row['status'], 'created_at': row['created_at'],
               'invalidated_at': row['invalidated_at'], 'invalidation_reason': row['invalidation_reason'], 'citations': json.loads(row['citations_json']), 'sources': json.loads(row['sources_json'])}
        if row['invalidated_at'] is not None:
            out['answer'] = None; out['note'] = 'answer payload withheld: ' + row['invalidation_reason']
            return out
        # the source documents must still be accessible now (revocation between generation and delivery)
        for s in out['sources']:
            d = db.execute('SELECT revoked_at FROM knowledge_documents WHERE id=?', (s['document_id'],)).fetchone()
            if d is None or d['revoked_at'] is not None:
                db.execute("UPDATE knowledge_answers SET invalidated_at=?, invalidation_reason=? WHERE id=?", (now(), 'source revoked before delivery', aid))
                out['answer'] = None; out['note'] = 'answer payload withheld: a cited source was revoked'; out['invalidated_at'] = now()
                return out
        job = db.execute('SELECT * FROM jobs WHERE id=?', (row['job_id'],)).fetchone()
        req = db.execute('SELECT output_artifact_id FROM model_requests WHERE job_id=?', (row['job_id'],)).fetchone()
        if job['state'] != 'succeeded' or not req or not req['output_artifact_id']:
            out['answer'] = None; out['note'] = 'no committed answer (job state %s)' % job['state']
            return out
        from ..compute import container
        files = container.unpack(store.load(db, req['output_artifact_id'], principal.workspace))
        payload = json.loads(files['output.json'])
        out['answer'] = payload.get('answer'); out['passages'] = payload.get('passages'); out['usage'] = payload.get('usage'); out['model'] = payload.get('model'); out['grounding'] = payload.get('grounding')
        return out
