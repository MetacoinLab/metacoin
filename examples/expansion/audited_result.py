"""An audited scientific result: a temporal batch job -> an independent analytical audit -> the signed public statement
verified against the service key. Synthetic battery model."""
import json
import sys
from _client import call, must, wait_job, BATCH_SPEC

spec = BATCH_SPEC
q = must(*call('POST', '/api/v1/jobs/quick', {'kind': 'temporal_batch', 'inputs': spec, 'title': 'example audited batch'}))
job = wait_job(q['job_id'])
if job['state'] != 'succeeded':
    sys.exit(json.dumps({'job': job['state'], 'error_code': job.get('error_code')}))
v = must(*call('POST', '/api/v1/verification', {'job_id': q['job_id'], 'class': 'analytical', 'params': {}}))
import time
for _ in range(300):
    st, vv = call('GET', '/api/v1/verification/' + v['id'])
    if vv.get('state') not in ('queued', 'awaiting_replica'):
        break
    time.sleep(1)
proj = must(*call('GET', '/api/v1/verification/' + v['id'] + '/statement'))
check = must(*call('POST', '/api/v1/verification/verify-statement', {'bundle': proj}))
print(json.dumps({'job_id': q['job_id'], 'verification_id': v['id'], 'state': vv.get('state'), 'checked': (vv.get('result') or {}).get('checked'), 'total': (vv.get('result') or {}).get('total'),
                  'statement_claim': proj['statement'].get('claim'), 'signature_valid': check.get('signature_valid'), 'issuer_trusted_by_this_service': check.get('issuer_trusted_by_this_service'),
                  'meaning': 'the audit re-derives the result independently of the worker; the statement is a signed public projection, not a scientific guarantee beyond its scope'}, indent=1))
