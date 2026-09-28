"""A private source-linked answer: collection -> document -> index build -> extractive answer with byte-checked citations.
Synthetic document; the answer cites exact byte spans of the stored version."""
import json
import sys
from _client import call, must, wait_job

col = must(*call('POST', '/api/v1/knowledge/collections', {'name': 'example notes', 'description': 'synthetic example'}))
doc = must(*call('POST', '/api/v1/knowledge/collections/' + col['id'] + '/documents', {'name': 'battery.md', 'format': 'markdown', 'provenance': 'synthetic',
                                                                                       'content': '# Battery notes\n\nThe demonstration reserve is 2000 mJ. The safe runtime is computed by the temporal energy service.\n'}))
idx = must(*call('POST', '/api/v1/knowledge/collections/' + col['id'] + '/indexes', {}))
job = wait_job(idx['job_id'])
if job['state'] != 'succeeded':
    sys.exit(json.dumps({'index_build': job['state'], 'error_code': job.get('error_code'), 'note': 'an embedding model must be registered and promoted, and a worker running'}))
ans = must(*call('POST', '/api/v1/knowledge/collections/' + col['id'] + '/answers', {'question': 'What is the demonstration reserve?', 'mode': 'extractive', 'k': 3}))
job = wait_job(ans['job_id'])
st, a = call('GET', '/api/v1/knowledge/answers/by-job/' + ans['job_id'])
must(st, a)
checked = must(*call('POST', '/api/v1/knowledge/citations/validate', {'citations': a.get('citations', [])}))
print(json.dumps({'collection_id': col['id'], 'document_id': doc['id'], 'index_id': idx['index_id'], 'answer_id': a['id'], 'status': a['status'],
                  'citations': [{'marker': c.get('marker'), 'chunk_id': c.get('chunk_id'), 'valid': c.get('valid')} for c in a.get('citations', [])], 'independent_validation': checked,
                  'passages': [p['text'][:120] for p in (a.get('passages') or [])], 'meaning': 'citations are byte-exact source spans; status insufficient_evidence means no source passed the thresholds'}, indent=1))
