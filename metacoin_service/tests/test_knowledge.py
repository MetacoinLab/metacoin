"""Private knowledge through real entry points: collections and immutable versions, deterministic chunks with byte
offsets, an index built by the worker's embedding runtime, lexical / semantic / hybrid retrieval with labeled
recall, byte-checked citations, extractive and generative answers with an honest insufficient-evidence path,
hostile-document containment, revocation reaching retrieval, previews, indexes and cached answers, and access
boundaries between principals. Writes an evaluation record when METACOIN_KNOWLEDGE_EVAL_OUT is set."""
import json
import os
import time
import unittest
from pathlib import Path

from metacoin_service.tests.test_models import ModelInstance, HAVE_TORCH, installed, GEN, EMB
from metacoin_service.knowledge import text as text_mod

CORPUS = json.loads((Path(__file__).parent / 'knowledge_corpus' / 'corpus.json').read_text())


class ChunkerTests(unittest.TestCase):
    def test_chunks_are_deterministic_bounded_and_byte_addressed(self):
        text, warnings = text_mod.normalize('markdown', ('# Title\n\n' + ('Sentence number %d is here. ' * 40 + '\n\n') * 6).encode(), {'knowledge_max_document_bytes': 10 ** 6, 'knowledge_max_csv_rows': 10})
        chunks = text_mod.chunk(text)
        data = text.encode('utf-8')
        self.assertEqual(chunks, text_mod.chunk(text))
        self.assertTrue(all(c['end'] - c['start'] <= text_mod.CHUNK_CHARS for c in chunks))
        self.assertTrue(all(__import__('hashlib').sha256(data[c['start']:c['end']]).hexdigest() == c['sha256'] for c in chunks))
        self.assertEqual(chunks[0]['heading'], 'Title')
        self.assertEqual(sorted(range(len(chunks))), [c['ordinal'] for c in chunks])
        with self.assertRaises(ValueError):
            text_mod.normalize('pdf', b'%PDF-1.4', {'knowledge_max_document_bytes': 10 ** 6, 'knowledge_max_csv_rows': 10})
        with self.assertRaises(ValueError):
            text_mod.normalize('text', b'\xff\xfe', {'knowledge_max_document_bytes': 10 ** 6, 'knowledge_max_csv_rows': 10})
        with self.assertRaises(ValueError):
            text_mod.normalize('text', b'   \n\n  ', {'knowledge_max_document_bytes': 10 ** 6, 'knowledge_max_csv_rows': 10})
        csv_text, w = text_mod.normalize('csv', b'a,b\n1,2\n3\n', {'knowledge_max_document_bytes': 10 ** 6, 'knowledge_max_csv_rows': 10})
        self.assertIn('a: 1; b: 2', csv_text); self.assertTrue(w)


@unittest.skipUnless(HAVE_TORCH, 'no torch-capable interpreter on this host')
class KnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.inst = ModelInstance(); self.addCleanup(self.inst.close); self.c = self.inst.client; self.H = self.inst.h('owner')
        if not installed(self.inst.settings, GEN) or not installed(self.inst.settings, EMB):
            self.skipTest('pinned model artifacts not installed in the model store')
        self.ids = self.inst.register_defaults()
        self.w = self.inst.worker(); self.addCleanup(self.w.offline)

    def load_corpus(self):
        col = self.c.post('/api/v1/knowledge/collections', headers=self.H, json={'name': 'eval corpus', 'description': 'synthetic project notes'})
        self.assertEqual(col.status_code, 201, col.text)
        cid = col.json()['id']
        docs = {}
        for d in CORPUS['documents']:
            r = self.c.post('/api/v1/knowledge/collections/' + cid + '/documents', headers=self.H, json={'name': d['name'], 'format': d['format'], 'content': d['text'], 'provenance': 'synthetic'})
            self.assertEqual(r.status_code, 201, r.text)
            docs[d['name']] = r.json()
        return cid, docs

    def build_index(self, cid):
        r = self.c.post('/api/v1/knowledge/collections/' + cid + '/indexes', headers=self.H, json={})
        self.assertEqual(r.status_code, 202, r.text)
        self.assertEqual(self.w.run_once()[1], 'succeeded')
        idx = self.c.get('/api/v1/knowledge/indexes/' + r.json()['index_id'], headers=self.H).json()
        self.assertEqual((idx['state'], idx['embedding_dim'], idx['chunker_id']), ('ready', 384, text_mod.CHUNKER_ID), idx)
        return idx

    def test_documents_index_retrieval_citations_answers_and_revocation(self):
        cid, docs = self.load_corpus()
        v = docs['temporal-model-spec.md']
        self.assertEqual((v['version'], v['current'], v['parser_id']), (1, True, text_mod.PARSER_ID)); self.assertGreater(v['chunk_count'], 0)
        # unsupported/invalid documents are refused precisely, never stored as empty successes
        for body, code in (({'name': 'x.pdf', 'format': 'pdf', 'content': '%PDF'}, 'document_unparseable'), ({'name': 'e', 'format': 'text', 'content': '   '}, 'document_unparseable')):
            r = self.c.post('/api/v1/knowledge/collections/' + cid + '/documents', headers=self.H, json=body)
            self.assertEqual((r.status_code, r.json()['detail']['code']), (422, code), r.text)
        # a replacement is a new immutable version; the old version stays addressable
        v2 = self.c.post('/api/v1/knowledge/collections/' + cid + '/documents', headers=self.H, json={'name': 'temporal-model-spec.md', 'format': 'markdown', 'document_id': v['document_id'],
                                                                                                    'content': CORPUS['documents'][0]['text'].replace('2000 mJ', '2000 mJ (unchanged)')}).json()
        self.assertEqual((v2['version'], v2['document_id']), (2, v['document_id']))
        self.assertTrue(self.c.get('/api/v1/knowledge/versions/' + v['id'] + '/preview', headers=self.H).json()['text'])
        # index build by the worker's embedding runtime
        idx = self.build_index(cid)
        self.assertEqual(idx['document_versions'][v['document_id']], v2['id'])
        # retrieval: lexical, semantic and hybrid with labeled recall@k; scores carry their meaning
        eval_rows = []
        recall = {'lexical': 0, 'semantic': 0, 'hybrid': 0}
        answerable = [q for q in CORPUS['questions'] if q['expected_documents']]
        for q in answerable:
            for mode in ('lexical', 'semantic', 'hybrid'):
                t0 = time.time()
                r = self.c.post('/api/v1/knowledge/collections/' + cid + '/search', headers=self.H, json={'query': q['question'], 'mode': mode, 'k': q['k']})
                self.assertEqual(r.status_code, 200, r.text)
                got = [x['document_name'] for x in r.json()['results']]
                hit = all(any(name == d for name in got) for d in q['expected_documents'])
                recall[mode] += hit
                eval_rows.append({'question': q['id'], 'mode': mode, 'hit': hit, 'top': got[:3], 'ms': int((time.time() - t0) * 1000), 'timing': r.json()['timing_ms']})
        n = len(answerable)
        self.assertGreaterEqual(recall['hybrid'], n - 1, (recall, eval_rows))
        self.assertGreaterEqual(recall['semantic'], n - 2, (recall, eval_rows))
        r = self.c.post('/api/v1/knowledge/collections/' + cid + '/search', headers=self.H, json={'query': 'reserve for the demonstration mission', 'mode': 'hybrid', 'k': 3}).json()
        self.assertIn('score_meaning', r); self.assertIsNotNone(r['results'][0]['cosine']); self.assertIn('location', r['results'][0])
        top = r['results'][0]
        # citations: exact quote validates byte-for-byte; altered quote, unknown chunk and cross-workspace ids are refused
        quote = top['text'][:40]
        val = self.c.post('/api/v1/knowledge/citations/validate', headers=self.H, json={'citations': [{'chunk_id': top['chunk_id'], 'quote': quote}, {'chunk_id': top['chunk_id'], 'quote': quote.replace('e', 'E', 1) + 'x'},
                                                                                                        {'chunk_id': 'kv_000000000000:0'}, {'chunk_id': 'nonsense'}]}).json()
        self.assertEqual([c['quote_valid'] for c in val['citations'][:2]], [True, False]); self.assertFalse(val['citations'][2]['source_exists']); self.assertFalse(val['all_valid'])
        self.assertIn('entail', val['guarantee'])
        # extractive answers: passages for an answerable question; explicit insufficient evidence for unsupported ones
        results = {}
        for q in CORPUS['questions']:
            r = self.c.post('/api/v1/knowledge/collections/' + cid + '/answers', headers=self.H, json={'question': q['question'], 'mode': 'extractive', 'k': q['k']})
            self.assertEqual(r.status_code, 202, r.text)
            self.assertEqual(self.w.run_once()[1], 'succeeded')
            a = self.c.get('/api/v1/knowledge/answers/by-job/' + r.json()['job_id'], headers=self.H).json()
            results[q['id']] = a
            if q.get('expected_status'):
                self.assertEqual(a['status'], q['expected_status'], (q['id'], a['sources'][:2]))
            else:
                self.assertEqual(a['status'], 'answered', q['id'])
                self.assertTrue(all(c['valid'] for c in a['citations']))
        # generative answer with real generation: citation markers are mechanically checked; the hostile document cannot leak secrets or act
        gen = {}
        for qid in ('q1', 'q9', 'q7'):
            q = next(x for x in CORPUS['questions'] if x['id'] == qid)
            r = self.c.post('/api/v1/knowledge/collections/' + cid + '/answers', headers=self.H, json={'question': q['question'], 'mode': 'generative', 'k': 4, 'max_output_tokens': 80})
            self.assertEqual(self.w.run_once()[1], 'succeeded')
            a = self.c.get('/api/v1/knowledge/answers/by-job/' + r.json()['job_id'], headers=self.H).json()
            gen[qid] = a
        self.assertEqual(gen['q7']['status'], 'insufficient_evidence', gen['q7'])
        self.assertIn(gen['q1']['status'], ('answered', 'insufficient_evidence'))
        if gen['q1']['status'] == 'answered':
            self.assertTrue(gen['q1']['citations']); self.assertIn('2000', gen['q1']['answer'])
        self.assertNotIn('hunter2', json.dumps(gen['q9'].get('answer') or ''))
        jobs_before = self.c.get('/api/v1/jobs', headers=self.H).json()
        grants = self.c.get('/api/v1/agent-grants', headers=self.H)
        self.assertNotIn('mck_', json.dumps(gen['q9']))
        self.assertTrue(all(j['kind'] in ('knowledge_index', 'knowledge_answer') for j in jobs_before['items']), 'the hostile note dispatched no work')
        # access: another principal (viewer) cannot read the answer, previews, documents or search
        for path, method in (('/api/v1/knowledge/answers/' + results['q1']['id'], 'get'), ('/api/v1/knowledge/versions/' + v['id'] + '/preview', 'get'), ('/api/v1/knowledge/collections/' + cid, 'get')):
            self.assertEqual(getattr(self.c, method)(path, headers=self.inst.h('viewer')).status_code, 403, path)
        self.assertEqual(self.c.post('/api/v1/knowledge/collections/' + cid + '/search', headers=self.inst.h('reviewer'), json={'query': 'reserve'}).status_code, 403)
        self.assertNotIn('ws_', json.dumps(self.c.get('/api/v1/knowledge/collections', headers=self.inst.h('viewer')).json()))
        # revocation: retrieval, preview, index freshness and cached answers obey it
        rev = self.c.post('/api/v1/knowledge/documents/' + v['document_id'] + '/revoke', headers=self.H, json={'reason': 'test revocation'}).json()
        self.assertIn(results['q1']['id'], rev['answers_invalidated']); self.assertGreaterEqual(rev['indexes_flagged_stale'], 1)
        after = self.c.post('/api/v1/knowledge/collections/' + cid + '/search', headers=self.H, json={'query': 'reserve for the demonstration mission', 'mode': 'hybrid', 'k': 5}).json()
        self.assertNotIn('temporal-model-spec.md', [x['document_name'] for x in after['results']]); self.assertTrue(any('stale' in n for n in after['notes']))
        self.assertEqual(self.c.get('/api/v1/knowledge/versions/' + v2['id'] + '/preview', headers=self.H).json()['detail']['code'], 'source_revoked')
        invalid = self.c.get('/api/v1/knowledge/answers/' + results['q1']['id'], headers=self.H).json()
        self.assertIsNone(invalid['answer']); self.assertIn('revoked', invalid['invalidation_reason'])
        self.assertFalse(self.c.post('/api/v1/knowledge/citations/validate', headers=self.H, json={'citations': [{'chunk_id': v2['id'] + ':0'}]}).json()['citations'][0]['accessible'])
        # a new answer after revocation no longer sees the revoked source (superseded old policy remains: 1500 mJ)
        r = self.c.post('/api/v1/knowledge/collections/' + cid + '/answers', headers=self.H, json={'question': 'What is the reserve for the demonstration mission?', 'mode': 'extractive', 'k': 3})
        self.assertEqual(self.w.run_once()[1], 'succeeded')
        a = self.c.get('/api/v1/knowledge/answers/by-job/' + r.json()['job_id'], headers=self.H).json()
        self.assertNotIn('temporal-model-spec.md', [s['document_name'] for s in a['sources']])
        # payload deletion under retention invalidates dependent answers and keeps digests
        d = self.c.delete('/api/v1/knowledge/versions/' + docs['monte-carlo-notes.md']['id'] + '/payload', headers=self.H).json()
        self.assertIn(results['q2']['id'], d['answers_invalidated'])
        out = os.environ.get('METACOIN_KNOWLEDGE_EVAL_OUT')
        if out:
            Path(out).write_text(json.dumps({'corpus': CORPUS['schema'], 'documents': len(CORPUS['documents']), 'questions': len(CORPUS['questions']), 'answerable': n, 'recall_at_k': recall,
                                             'retrieval_rows': eval_rows, 'extractive': {k: v['status'] for k, v in results.items()},
                                             'generative': {k: {'status': v['status'], 'citations': len(v['citations']), 'grounding': v.get('grounding'), 'usage': v.get('usage')} for k, v in gen.items()},
                                             'method': 'recall@k = every expected document appears among the k results; answer status mechanical; generative quality not scored beyond citation validity and expected substrings'}, indent=1))
