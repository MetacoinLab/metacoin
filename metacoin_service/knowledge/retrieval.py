"""Authorized retrieval: exact lexical baseline (BM25 over chunk tokens), exact semantic search over an index's
vectors, and explicit reciprocal-rank fusion. Access is enforced before any chunk is scored: only chunks of the
caller's workspace, of non-revoked documents with a present payload, are candidates. Scores carry their meaning;
none is a probability of truth."""
import json
import math
import time
from collections import OrderedDict

from ..compute import container, npy
from ..errors import ServiceError
from . import text as text_mod

RRF_K = 60
MODES = ('lexical', 'semantic', 'hybrid')
_VECTOR_CACHE = OrderedDict()          # index_id -> (manifest, rows); process-local plaintext, bounded
_VECTOR_CACHE_MAX = 4


def bm25(query_tokens, docs_tokens, k1=1.2, b=0.75):
    n = len(docs_tokens)
    if n == 0:
        return []
    avgdl = sum(len(d) for d in docs_tokens) / n
    df = {}
    for d in docs_tokens:
        for t in set(d):
            df[t] = df.get(t, 0) + 1
    scores = []
    for d in docs_tokens:
        tf = {}
        for t in d:
            tf[t] = tf.get(t, 0) + 1
        s = 0.0
        for t in query_tokens:
            if t not in tf:
                continue
            idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
            s += idf * tf[t] * (k1 + 1) / (tf[t] + k1 * (1 - b + b * len(d) / avgdl))
        scores.append(s)
    return scores


def candidate_chunks(db, workspace, cid, version_ids=None, document_ids=None):
    """Authorized candidate set: (chunk rows, version rows) of non-revoked documents with a payload, in this workspace."""
    sql = ('SELECT c.*, v.document_id, v.version, v.text_artifact_id, v.text_sha256, d.name AS document_name, d.current_version_id FROM knowledge_chunks c JOIN knowledge_versions v ON v.id=c.version_id '
           'JOIN knowledge_documents d ON d.id=v.document_id WHERE v.workspace=? AND v.collection_id=? AND d.revoked_at IS NULL AND v.deleted_at IS NULL')
    args = [workspace, cid]
    if version_ids:
        sql += ' AND v.id IN (%s)' % ','.join('?' * len(version_ids)); args += list(version_ids)
    else:
        sql += ' AND v.id = d.current_version_id'
    if document_ids:
        sql += ' AND d.id IN (%s)' % ','.join('?' * len(document_ids)); args += list(document_ids)
    sql += ' ORDER BY v.document_id, v.version, c.ordinal'
    return db.execute(sql, args).fetchall()


def load_texts(db, store, workspace, chunks):
    texts, cache = [], {}
    for c in chunks:
        if c['version_id'] not in cache:
            cache[c['version_id']] = store.load(db, c['text_artifact_id'], workspace)
        texts.append(cache[c['version_id']][c['start_byte']:c['end_byte']].decode('utf-8'))
    return texts


def index_vectors(db, store, workspace, index_row):
    key = index_row['id']
    if key in _VECTOR_CACHE:
        _VECTOR_CACHE.move_to_end(key)
        return _VECTOR_CACHE[key]
    files = container.unpack(store.load(db, index_row['vectors_artifact_id'], workspace))
    manifest = json.loads(files['manifest.json'])
    vals, dtype, shape = npy.decode(files['vectors.npy'])
    dim = shape[1]
    rows = [vals[i * dim:(i + 1) * dim] for i in range(shape[0])]
    _VECTOR_CACHE[key] = (manifest, rows)
    while len(_VECTOR_CACHE) > _VECTOR_CACHE_MAX:
        _VECTOR_CACHE.popitem(last=False)
    return manifest, rows


def forget_index(index_id):
    _VECTOR_CACHE.pop(index_id, None)


def search(db, store, principal, knowledge, cid, query, *, mode='hybrid', k=8, index_row=None, document_ids=None, embed_fn=None, deadline_seconds=10):
    """Returns ranked chunks with scores and their meaning. embed_fn(texts) -> list of vectors (semantic modes)."""
    principal.require('knowledge:read')
    col = knowledge.collection(db, principal, cid)
    if type(query) is not str or not query.strip() or len(query) > 2000:
        raise ServiceError('VALIDATION', 'query: 1..2000 characters')
    if mode not in MODES or type(k) is not int or not 1 <= k <= 20:
        raise ServiceError('VALIDATION', 'mode/k')
    if document_ids is not None and (type(document_ids) is not list or len(document_ids) > 50 or not all(type(d) is str and d.startswith('kd_') for d in document_ids)):
        raise ServiceError('VALIDATION', 'document_ids')
    t0 = time.time()
    timing = {}
    notes = []
    if mode in ('semantic', 'hybrid'):
        index_row = index_row or knowledge.latest_ready_index(db, cid)
        if index_row is None:
            if mode == 'semantic':
                raise ServiceError('CAPABILITY_UNAVAILABLE', {'code': 'no_ready_index', 'collection_id': cid})
            notes.append('no ready index: hybrid fell back to lexical only'); mode = 'lexical'
        elif index_row['collection_id'] != cid:
            raise ServiceError('VALIDATION', 'index belongs to another collection')
    if mode in ('semantic', 'hybrid'):
        versions = json.loads(index_row['document_versions_json'])
        chunks = candidate_chunks(db, principal.workspace, cid, version_ids=list(versions.values()), document_ids=document_ids)
        stale = [c['document_id'] for c in chunks if c['version_id'] != c['current_version_id']]
        if index_row['stale_reason']:
            notes.append('index stale: ' + index_row['stale_reason'] + '; results from superseded versions are flagged')
    else:
        chunks = candidate_chunks(db, principal.workspace, cid, document_ids=document_ids)
        stale = []
    if not chunks:
        return {'collection_id': cid, 'mode': mode, 'results': [], 'index_id': index_row['id'] if index_row else None, 'notes': notes + ['no authorized chunks (empty collection, all revoked, or payloads deleted)'], 'timing_ms': {}}
    if len(chunks) > knowledge.settings.limits['knowledge_max_chunks_per_index']:
        raise ServiceError('VALIDATION', 'collection too large for exact search')
    texts = load_texts(db, store, principal.workspace, chunks)
    timing['load_ms'] = int((time.time() - t0) * 1000)
    ranked = {}
    lexical_scores = None
    if mode in ('lexical', 'hybrid'):
        t1 = time.time()
        q = text_mod.query_tokens(query)
        lexical_scores = bm25(q, [text_mod.tokens(t) for t in texts])
        order = sorted(range(len(chunks)), key=lambda i: (-lexical_scores[i], i))
        order = [i for i in order if lexical_scores[i] > 0]
        for rank, i in enumerate(order):
            ranked.setdefault(i, {})['lexical_rank'] = rank
        timing['lexical_ms'] = int((time.time() - t1) * 1000)
    semantic_scores = None
    if mode in ('semantic', 'hybrid'):
        t1 = time.time()
        if embed_fn is None:
            raise ServiceError('CAPABILITY_UNAVAILABLE', 'no embedding runtime available in this process')
        manifest, rows = index_vectors(db, store, principal.workspace, index_row)
        pos = {(m['version_id'], m['ordinal']): j for j, m in enumerate(manifest['chunks'])}
        qv = embed_fn([query])[0]
        semantic_scores = []
        for c in chunks:
            j = pos.get((c['version_id'], c['ordinal']))
            if j is None:
                semantic_scores.append(None); continue
            r = rows[j]
            semantic_scores.append(sum(a * b for a, b in zip(qv, r)))
        order = sorted([i for i in range(len(chunks)) if semantic_scores[i] is not None], key=lambda i: (-semantic_scores[i], i))
        for rank, i in enumerate(order):
            ranked.setdefault(i, {})['semantic_rank'] = rank
        timing['semantic_ms'] = int((time.time() - t1) * 1000)
    fused = []
    for i, r in ranked.items():
        s = 0.0
        if mode == 'hybrid':
            s = sum(1.0 / (RRF_K + r[key]) for key in ('lexical_rank', 'semantic_rank') if key in r)
        elif mode == 'lexical':
            s = lexical_scores[i]
        else:
            s = semantic_scores[i]
        fused.append((s, i))
    fused.sort(key=lambda x: (-x[0], x[1]))
    results = []
    for s, i in fused[:k]:
        c = chunks[i]
        results.append({'chunk_id': c['version_id'] + ':' + str(c['ordinal']), 'version_id': c['version_id'], 'document_id': c['document_id'], 'document_name': c['document_name'], 'version': c['version'],
                        'ordinal': c['ordinal'], 'heading': c['heading'], 'score': s, 'lexical_score': lexical_scores[i] if lexical_scores else None, 'cosine': semantic_scores[i] if semantic_scores else None,
                        'superseded_version': c['version_id'] != c['current_version_id'], 'text': texts[i], 'sha256': c['sha256'], 'location': {'start_byte': c['start_byte'], 'end_byte': c['end_byte']}})
    timing['total_ms'] = int((time.time() - t0) * 1000)
    return {'collection_id': cid, 'mode': mode, 'index_id': index_row['id'] if index_row else None, 'candidates': len(chunks), 'results': results, 'notes': notes, 'timing_ms': timing,
            'score_meaning': {'lexical': 'BM25 (k1=1.2, b=0.75) over lowercase alphanumeric tokens, query stopwords removed; 0 = no informative query term present', 'semantic': 'cosine similarity in the index model space; not a probability',
                              'hybrid': 'reciprocal rank fusion, k=60, over the two ranked lists; ordering only', 'stale': 'superseded_version marks chunks of a version that is no longer the document head'}}


def validate_citations(db, store, principal, knowledge, citations):
    """Mechanical checks per citation: source exists in this workspace, is accessible now, and the quote (if any) is a
    byte-exact substring of the chunk's normalized text (after trimming outer whitespace). No entailment claim."""
    principal.require('knowledge:read')
    if type(citations) is not list or not 1 <= len(citations) <= 32:
        raise ServiceError('VALIDATION', 'citations: 1..32')
    out = []
    for c in citations:
        item = {'chunk_id': c.get('chunk_id') if type(c) is dict else None, 'source_exists': False, 'accessible': False, 'quote_valid': None, 'reason': None}
        if type(c) is not dict or type(c.get('chunk_id')) is not str or ':' not in c['chunk_id']:
            item['reason'] = 'malformed'; out.append(item); continue
        vid, _, ordinal = c['chunk_id'].rpartition(':')
        if not ordinal.isdigit():
            item['reason'] = 'malformed ordinal'; out.append(item); continue
        row = db.execute('SELECT c.*, v.text_artifact_id, v.deleted_at, d.revoked_at FROM knowledge_chunks c JOIN knowledge_versions v ON v.id=c.version_id JOIN knowledge_documents d ON d.id=v.document_id '
                         'WHERE c.version_id=? AND c.ordinal=? AND v.workspace=?', (vid, int(ordinal), principal.workspace)).fetchone()
        if row is None:
            item['reason'] = 'no such chunk in this workspace'; out.append(item); continue
        item['source_exists'] = True
        if row['revoked_at'] is not None or row['deleted_at'] is not None:
            item['reason'] = 'source revoked' if row['revoked_at'] else 'source payload deleted'; out.append(item); continue
        item['accessible'] = True
        quote = c.get('quote')
        if quote is not None:
            if type(quote) is not str or not 1 <= len(quote) <= 2000:
                item['quote_valid'] = False; item['reason'] = 'quote must be 1..2000 characters'
            else:
                data = store.load(db, row['text_artifact_id'], principal.workspace)[row['start_byte']:row['end_byte']]
                item['quote_valid'] = quote.strip().encode('utf-8') in data
                if not item['quote_valid']:
                    item['reason'] = 'quote is not a byte-exact span of the cited chunk'
        out.append(item)
    return {'citations': out, 'all_valid': all(i['source_exists'] and i['accessible'] and i['quote_valid'] is not False for i in out),
            'guarantee': 'source-link validity and exact-quote validity only; a valid citation does not establish that the citing sentence is entailed by the source'}
