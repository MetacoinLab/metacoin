"""A federated worker execution over the local TLS topology: enrol a node identity, freeze a contract that may run only
on that node, run the node worker once against the coordinator over HTTPS (pinned local CA), inspect transfers and
the fenced result. Needs an API started with `serve --tls` (run `node-tls` first) and:
    METACOIN_TLS_BASE_URL=https://127.0.0.1:<port>   METACOIN_TLS_CA=<path to the local CA certificate>"""
import json
import os
import subprocess
import sys
import tempfile
from _client import call, must, wait_job, BATCH_SPEC

TLS_BASE, CA = os.environ.get('METACOIN_TLS_BASE_URL'), os.environ.get('METACOIN_TLS_CA')
if not TLS_BASE or not CA:
    sys.exit(json.dumps({'error': 'METACOIN_TLS_BASE_URL and METACOIN_TLS_CA are required (API started with serve --tls)'}))
work = tempfile.mkdtemp(prefix='metacoin-node-example-')
ident = os.path.join(work, 'node.json')
enroll = subprocess.run([sys.executable, '-m', 'metacoin_service.client_cli', '--credential-file', os.environ['METACOIN_CREDENTIAL_FILE'], '--base', os.environ.get('METACOIN_BASE_URL', 'http://127.0.0.1:8402'),
                         'node-enroll', '--name', 'example-node', '--out', ident, '--devices', 'cpu', '--capabilities', 'temporal_batch'], capture_output=True, text=True)
if enroll.returncode != 0:
    sys.exit(json.dumps({'enroll_failed': enroll.stdout[-300:] or enroll.stderr[-300:]}))
node_id = json.loads(enroll.stdout)['node_id']
spec = BATCH_SPEC
job = must(*call('POST', '/api/v1/jobs/quick', {'kind': 'temporal_batch', 'title': 'example node job', 'inputs': spec, 'policy': {'execution_locations': [node_id]}}))
job['id'] = job['job_id']
ran = subprocess.run([sys.executable, '-m', 'metacoin_service', 'node-worker', '--identity', ident, '--coordinator', TLS_BASE, '--ca', CA, '--node-home', os.path.join(work, 'home'), '--once'], capture_output=True, text=True, timeout=600)
try:
    node_out = json.loads(ran.stdout)
except ValueError:
    node_out = {'stdout': ran.stdout[-300:], 'stderr': ran.stderr[-300:]}
j = wait_job(job['id'], timeout=120)
node = must(*call('GET', '/api/v1/nodes/' + node_id))
print(json.dumps({'node_id': node_id, 'job_id': job['id'], 'state': j['state'], 'node_ran': node_out.get('ran'), 'transfers': [t['role'] + ':' + t['direction'] for t in node.get('transfers', [])],
                  'observed': node.get('observed'), 'meaning': 'two processes on one host over loopback TLS; the node never opens the coordinator database'}, indent=1))
