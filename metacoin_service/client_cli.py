"""Interoperable client CLI over the public API (same endpoints the console uses).

    python -m metacoin_service.client_cli --credential-file ~/.config/metacoin/credential.json \\
        [--base http://127.0.0.1:8402] create|freeze|submit|poll|status|review-request|decide|export|compare ...

The credential file (0600) holds {"token": "..."}; the token never appears in arguments,
environment listings, or output. Responses are printed as JSON.
"""
import argparse
import json
import os
import stat
import sys
import time
from pathlib import Path
import urllib.error
import urllib.request

DEFAULT_CREDENTIAL = os.path.join(os.environ.get('XDG_CONFIG_HOME', os.path.expanduser('~/.config')), 'metacoin', 'credential.json')


def load_token(path):
    info = os.stat(path)
    if info.st_mode & 0o077 or not stat.S_ISREG(info.st_mode):
        raise SystemExit('credential file must be a private regular file (chmod 600)')
    with open(path) as stream:
        token = json.load(stream).get('token')
    if not isinstance(token, str) or not token.startswith('mck_'):
        raise SystemExit('credential file must contain {"token": "mck_..."}')
    return token


def call(base, token, method, path, body=None, raw=False, idempotency_key=None, content_type=None):
    data = None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
    req = urllib.request.Request(base + path, data=data, method=method)
    req.add_header('Authorization', 'Bearer ' + token)
    if data is not None:
        req.add_header('Content-Type', content_type or ('application/octet-stream' if isinstance(body, bytes) else 'application/json'))
    if idempotency_key:
        req.add_header('Idempotency-Key', idempotency_key)
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            content = response.read()
            return response.status, (content if raw else json.loads(content))
    except urllib.error.HTTPError as exc:
        content = exc.read()
        try:
            return exc.code, json.loads(content)
        except ValueError:
            return exc.code, {'error': True, 'code': 'HTTP_' + str(exc.code)}


EXPANSION_COMMANDS = {'models', 'models-runtime', 'model-register', 'model-action', 'generate', 'embed', 'model-job', 'knowledge-collection-create', 'knowledge-collections', 'knowledge-add', 'knowledge-index',
                      'knowledge-search', 'knowledge-answer', 'knowledge-revoke', 'knowledge-validate-citations', 'calibration-dataset', 'calibration-fit', 'calibration-models', 'calibration-predict', 'calibration-action',
                      'calibration-plan', 'verification-preview', 'verification-request', 'verification-status', 'verification-statement', 'verifications', 'node-enroll', 'nodes', 'node', 'node-action',
                      'approval-propose', 'approval-decide', 'approvals', 'approval-policy', 'statement', 'mcp-connection', 'verification-policy-create', 'verification-policies',
                      'eval-suite-create', 'eval-suites', 'eval-run', 'eval-compare', 'eval-gate',
                      'calibration-design', 'model-warmup', 'model-batching', 'plan', 'plans', 'plan-accept', 'intent', 'intents', 'intent-continue', 'document-import', 'documents', 'document', 'document-compare', 'document-page', 'document-action', 'table', 'table-annotate', 'mapping-preview', 'mapping-create', 'mapping', 'mapping-confirm', 'resource-plan', 'plan-view', 'plan-freeze', 'bundle-export', 'bundle-check', 'bundle-import', 'disagreements', 'disagreement-decide', 'notebook-create', 'notebooks', 'notebook', 'notebook-version', 'notebook-compare', 'notebook-export', 'analysis-create', 'analyses', 'analysis', 'analysis-revise', 'analysis-freeze', 'analysis-impact', 'analysis-regenerate', 'analysis-report', 'report', 'report-projection', 'projection-verify', 'package-create', 'packages', 'package', 'package-compat', 'package-import', 'package-export', 'package-instantiate', 'package-quote', 'package-run', 'package-runs', 'package-run', 'package-retry', 'package-bundle', 'package-retire', 'reconcile', 'reconciliations', 'measurement-request', 'measurement-requests', 'package-upgrade-preview', 'review-queue'}


def wait_job(go, job_id, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        st, j = go('GET', '/api/v1/jobs/' + job_id)
        if st != 200 or j['state'] in ('succeeded', 'failed', 'cancelled'):
            return st, j
        time.sleep(0.5)
    return 408, {'error': True, 'code': 'CLIENT_TIMEOUT', 'job_id': job_id, 'note': 'the job continues server-side; poll again (no second request was created)'}


def expansion(args, go):
    c = args.command
    if c == 'models':
        return go('GET', '/api/v1/models')
    if c == 'models-runtime':
        return go('GET', '/api/v1/models/runtime')
    if c == 'model-batching':
        body = {}
        if args.enabled is not None: body['enabled'] = args.enabled == 'on'
        for k in ('max_sequences', 'max_tokens', 'wait_ms', 'kv_budget_bytes'):
            if getattr(args, k) is not None: body[k] = getattr(args, k)
        return go('POST', '/api/v1/models/batching', body)
    if c == 'model-warmup':
        body = {'enabled': not args.disable, 'revision_ids': [x for x in (args.revisions or '').split(',') if x]}
        if args.ceiling_bytes is not None:
            body['ceiling_bytes'] = args.ceiling_bytes
        return go('POST', '/api/v1/models/warmup', body)
    if c == 'model-register':
        return go('POST', '/api/v1/models', {'model_id': args.model_id, 'hub_repo': args.hub_repo, 'revision': args.revision, 'operations': args.operations.split(','), 'license': args.license})
    if c == 'model-action':
        body = {k: v for k, v in (('operation', args.operation), ('reason', args.reason)) if v}
        return go('POST', '/api/v1/models/' + args.revision_id + '/' + args.action, body)
    if c == 'generate':
        inputs = {'max_output_tokens': args.max_output_tokens, 'temperature_percent': args.temperature_percent}
        if args.messages:
            inputs['messages'] = json.load(open(args.messages))
        else:
            inputs['prompt'] = args.prompt
        if args.seed is not None:
            inputs['seed'] = args.seed
        if args.model_revision:
            inputs['model_revision_id'] = args.model_revision
        st, out = go('POST', '/api/v1/models/generate', {'inputs': inputs})
        if st != 202 or not args.watch:
            return st, out
        jid, after, text, deadline = out['job_id'], -1, '', time.time() + args.timeout
        while time.time() < deadline:
            st2, seg = go('GET', '/api/v1/models/jobs/%s/segments?after=%d&wait=10' % (jid, after))
            if st2 != 200:
                return st2, seg
            for s_ in seg['segments']:
                sys.stderr.write(s_['text']); sys.stderr.flush(); text += s_['text']        # streamed text on stderr; the JSON result stays on stdout
            after = seg['cursor']
            if seg['done']:
                sys.stderr.write('\n')
                st3, view = go('GET', '/api/v1/models/jobs/' + jid)
                return st3, {'job_id': jid, 'state': view.get('state'), 'usage': view.get('usage'), 'chars': len(text)}
        return 408, {'error': True, 'code': 'CLIENT_TIMEOUT', 'job_id': jid, 'note': 'delivery resumes with model-job --segments --after N; no second generation was requested'}
    if c == 'embed':
        return go('POST', '/api/v1/models/embed', {'inputs': {'texts': json.load(open(args.texts)), 'truncate': bool(args.truncate)}})
    if c == 'model-job':
        if args.segments:
            return go('GET', '/api/v1/models/jobs/%s/segments?after=%d' % (args.job_id, args.after))
        return go('GET', '/api/v1/models/jobs/' + args.job_id)
    if c == 'knowledge-collection-create':
        return go('POST', '/api/v1/knowledge/collections', {'name': args.name, 'description': args.description})
    if c == 'knowledge-collections':
        return go('GET', '/api/v1/knowledge/collections')
    if c == 'knowledge-add':
        body = {'name': args.name or os.path.basename(args.file), 'format': args.format, 'content': open(args.file, encoding='utf-8').read()}
        if args.document_id:
            body['document_id'] = args.document_id
        return go('POST', '/api/v1/knowledge/collections/' + args.collection_id + '/documents', body)
    if c == 'knowledge-index':
        st, out = go('POST', '/api/v1/knowledge/collections/' + args.collection_id + '/indexes', {})
        if st != 202 or not args.wait:
            return st, out
        st2, j = wait_job(go, out['job_id'], 600)
        st3, idx = go('GET', '/api/v1/knowledge/indexes/' + out['index_id'])
        return st3, idx
    if c == 'knowledge-search':
        return go('POST', '/api/v1/knowledge/collections/' + args.collection_id + '/search', {'query': args.query, 'mode': args.mode, 'k': args.k})
    if c == 'knowledge-answer':
        st, out = go('POST', '/api/v1/knowledge/collections/' + args.collection_id + '/answers', {'question': args.question, 'mode': args.mode, 'k': args.k, 'max_output_tokens': args.max_output_tokens})
        if st != 202 or not args.wait:
            return st, out
        st2, j = wait_job(go, out['job_id'], args.timeout)
        if j.get('state') != 'succeeded':
            return st2, j
        return go('GET', '/api/v1/knowledge/answers/by-job/' + out['job_id'])
    if c == 'knowledge-revoke':
        return go('POST', '/api/v1/knowledge/documents/' + args.document_id + '/revoke', {'reason': args.reason})
    if c == 'knowledge-validate-citations':
        return go('POST', '/api/v1/knowledge/citations/validate', {'citations': json.load(open(args.citations))})
    if c == 'calibration-dataset':
        if args.task_kind:
            return go('POST', '/api/v1/calibration/datasets', {'kind': 'performance', 'task_kind': args.task_kind})
        return go('POST', '/api/v1/calibration/datasets', json.load(open(args.file)))
    if c == 'calibration-fit':
        inputs = {'dataset_id': args.dataset_id, 'features': args.features.split(','), 'target': args.target, 'ridge_lambda': args.ridge, 'split': {'method': args.split, 'train_fraction_percent': 80}}
        if args.scope_kind:
            inputs['scope'] = {'task_kind': args.scope_kind, **({'backend': args.scope_backend} if args.scope_backend else {})}
        st, out = go('POST', '/api/v1/calibration/fits', {'inputs': inputs})
        if st != 202 or not args.wait:
            return st, out
        st2, j = wait_job(go, out['job_id'], 300)
        st3, models = go('GET', '/api/v1/calibration/models')
        return st3, {'job': j.get('state'), 'model': next((m for m in models.get('items', []) if m['job_id'] == out['job_id']), None)}
    if c == 'calibration-models':
        return go('GET', '/api/v1/calibration/models')
    if c == 'calibration-predict':
        return go('POST', '/api/v1/calibration/models/' + args.model_id + '/predict', {'features': json.loads(args.features)})
    if c == 'calibration-action':
        if args.action == 'comparison':
            return go('GET', '/api/v1/calibration/models/' + args.model_id + '/comparison')
        return go('POST', '/api/v1/calibration/models/' + args.model_id + '/' + args.action, {})
    if c == 'calibration-design':
        return go('POST', '/api/v1/calibration/models/' + args.model_id + '/design', json.load(open(args.file)))
    if c == 'calibration-plan':
        return go('POST', '/api/v1/calibration/plan', {'task_kind': args.kind, 'inputs': json.load(open(args.inputs))})
    if c in ('verification-preview', 'verification-request'):
        params = {'sample_count': args.sample_count} if args.sample_count else {}
        if c == 'verification-preview':
            return go('POST', '/api/v1/verification/preview', {'job_id': args.job_id, 'class': args.cls, 'params': params})
        st, out = go('POST', '/api/v1/verification', {'job_id': args.job_id, 'class': args.cls, 'params': params})
        if st != 202 or not args.wait:
            return st, out
        deadline = time.time() + args.timeout
        while time.time() < deadline:
            st2, v = go('GET', '/api/v1/verification/' + out['id'])
            if st2 != 200 or v['state'] not in ('queued', 'awaiting_replica'):
                return st2, v
            time.sleep(1)
        return 408, {'error': True, 'code': 'CLIENT_TIMEOUT', 'verification_id': out['id']}
    if c == 'verification-status':
        return go('GET', '/api/v1/verification/' + args.verification_id)
    if c == 'verification-statement':
        st, proj = go('GET', '/api/v1/verification/' + args.verification_id + '/statement')
        if st != 200:
            return st, proj
        if args.out:
            fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as f:
                json.dump(proj, f, indent=1)
        if args.verify:
            return go('POST', '/api/v1/verification/verify-statement', {'bundle': proj, 'expected': {'verification_id': args.verification_id}})
        return st, proj
    if c == 'verifications':
        return go('GET', '/api/v1/verification' + ('?job_id=' + args.job if args.job else ''))
    if c == 'verification-policy-create':
        body = {'name': args.name, 'class': args.cls, 'params': ({'sample_count': args.sample_count} if args.sample_count else {})}
        if args.max_work:
            body['max_work'] = args.max_work
        return go('POST', '/api/v1/verification/policies', body)
    if c == 'verification-policies':
        return go('GET', '/api/v1/verification/policies')
    if c == 'eval-suite-create':
        spec = json.load(open(args.file))
        return go('POST', '/api/v1/evaluation/suites', {'name': args.name or spec.get('name'), 'items': spec['items'], 'threshold_percent': args.threshold if args.threshold is not None else spec.get('threshold_percent', 100)})
    if c == 'eval-suites':
        return go('GET', '/api/v1/evaluation/suites')
    if c == 'eval-run':
        st, out = go('POST', '/api/v1/evaluation/suites/' + args.suite_id + '/runs', {'model_revision_id': args.revision} if args.revision else {})
        if st != 202 or not args.wait:
            return st, out
        deadline = time.time() + args.timeout
        while time.time() < deadline:
            st2, run = go('GET', '/api/v1/evaluation/runs/' + out['id'])
            if st2 != 200 or run['state'] == 'scored':
                return st2, run
            time.sleep(1)
        return st2, run
    if c == 'eval-compare':
        return go('GET', '/api/v1/evaluation/compare/%s/%s' % (args.run_a, args.run_b))
    if c == 'eval-gate':
        return go('POST', '/api/v1/evaluation/gate', {'suite_id': args.suite_id or None})
    if c == 'notebook-create':
        spec = json.load(open(args.file))
        return go('POST', '/api/v1/notebooks', {'name': args.name or spec.get('name'), 'blocks': spec['blocks'], 'note': spec.get('note', '')})
    if c == 'notebooks':
        return go('GET', '/api/v1/notebooks')
    if c == 'notebook':
        return go('GET', '/api/v1/notebooks/' + args.notebook_id + ('?version=%d' % args.version if args.version else ''))
    if c == 'notebook-version':
        spec = json.load(open(args.file))
        return go('POST', '/api/v1/notebooks/' + args.notebook_id + '/versions', {'blocks': spec['blocks'], 'note': args.note or spec.get('note', '')})
    if c == 'notebook-compare':
        return go('GET', '/api/v1/notebooks/%s/compare/%d/%d' % (args.notebook_id, args.a, args.b))
    if c == 'analysis-create':
        spec = json.load(open(args.file)) if args.file else {}
        return go('POST', '/api/v1/analyses', {'name': args.name or spec.get('name'), 'blocks': spec.get('blocks', []), 'from_document': args.from_document, 'from_workflow': args.from_workflow, 'note': spec.get('note', '')})
    if c == 'analyses':
        return go('GET', '/api/v1/analyses')
    if c == 'analysis':
        return go('GET', '/api/v1/analyses/' + args.analysis_id + ('?version=%d' % args.version if args.version else ''))
    if c == 'analysis-revise':
        spec = json.load(open(args.file))
        return go('POST', '/api/v1/analyses/' + args.analysis_id + '/revisions', {'blocks': spec['blocks'], 'expected_version': args.expected_version, 'note': args.note or spec.get('note', '')})
    if c == 'analysis-freeze':
        return go('POST', '/api/v1/analyses/' + args.analysis_id + '/freeze', {'version': args.version, 'reason': args.reason or ''})
    if c == 'analysis-impact':
        return go('POST', '/api/v1/analyses/' + args.analysis_id + '/impact', {'changed': json.loads(args.changed)})
    if c == 'analysis-regenerate':
        body = {'run_id': args.run_id, 'changes': json.loads(args.changes), 'budget_ceiling': args.budget_ceiling}
        return go('POST', '/api/v1/analyses/' + args.analysis_id + ('/regeneration-plan' if args.plan_only else '/regenerate'), body)
    if c == 'analysis-report':
        return go('POST', '/api/v1/analyses/' + args.analysis_id + '/reports', {'version': args.version, 'mode': args.mode})
    if c == 'report':
        if args.bundle:
            return go('GET', '/api/v1/reports/' + args.report_id + '/bundle')
        return go('GET', '/api/v1/reports/' + args.report_id)
    if c == 'report-projection':
        scope = json.loads(args.scope) if args.scope else {}
        if args.export:
            return go('POST', '/api/v1/reports/' + args.report_id + '/projection', {'scope': scope, 'acknowledge_warnings': args.acknowledge_warnings})
        return go('POST', '/api/v1/reports/' + args.report_id + '/projection/preview', {'scope': scope})
    if c == 'projection-verify':
        return go('POST', '/api/v1/reports/projection/verify', {'bundle': json.load(open(args.file))})
    if c == 'notebook-export':
        st, out = go('GET', '/api/v1/notebooks/' + args.notebook_id + '/export' + ('?version=%d' % args.version if args.version else ''))
        if st == 200 and args.out:
            Path(args.out).write_text(json.dumps(out, indent=1))
        return st, out
    if c == 'node-enroll':
        from cryptography.hazmat.primitives.asymmetric import ed25519
        from cryptography.hazmat.primitives import serialization
        key = ed25519.Ed25519PrivateKey.generate()
        pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
        body = {'name': args.name, 'public_key_hex': pub, 'devices': args.devices.split(',')}
        if args.capabilities:
            body['capabilities'] = args.capabilities.split(',')
        st, out = go('POST', '/api/v1/nodes', body)
        if st != 201:
            return st, out
        priv = key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()).hex()
        fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as f:
            json.dump({'node_id': out['node_id'], 'credential': out['credential'], 'private_key_hex': priv, 'devices': args.devices.split(',')}, f)
        return st, {'node_id': out['node_id'], 'identity_file': args.out, 'expires_at': out['expires_at'], 'note': 'credential and private key written to the identity file only'}
    if c == 'nodes':
        return go('GET', '/api/v1/nodes')
    if c == 'node':
        return go('GET', '/api/v1/nodes/' + args.node_id)
    if c == 'node-action':
        st, out = go('POST', '/api/v1/nodes/' + args.node_id + '/' + args.action, {'reason': args.reason})
        if args.action == 'rotate' and st == 200:
            out = {'node_id': out['node_id'], 'note': 'new credential returned by the API; not printed. Re-run node-enroll for a fresh identity file, or read the API response programmatically.'}
        return st, out
    if c == 'package-create':
        return go('POST', '/api/v1/packages', json.load(open(args.file)))
    if c == 'packages':
        return go('GET', '/api/v1/packages')
    if c == 'package':
        return go('GET', '/api/v1/packages/' + args.package_id)
    if c == 'package-compat':
        body = {'manifest': json.load(open(args.file))} if args.file else {'package_id': args.package_id}
        if args.device_policy:
            body['device_policy'] = args.device_policy
        return go('POST', '/api/v1/packages/compatibility', body)
    if c == 'package-import':
        return go('POST', '/api/v1/packages/import', {'manifest': json.load(open(args.file)), 'apply': args.apply})
    if c == 'package-export':
        st, out = go('GET', '/api/v1/packages/' + args.package_id + '/export' + ('' if args.example else '?example=0'))
        if st == 200 and args.out:
            Path(args.out).write_text(json.dumps(out, indent=1))
        return st, out
    if c == 'package-instantiate':
        spec = json.load(open(args.file)) if args.file else {}
        return go('POST', '/api/v1/packages/' + args.package_id + '/instantiate', {'values': spec.get('values'), 'inputs': spec.get('inputs'), 'name': args.name})
    if c == 'package-quote':
        return go('POST', '/api/v1/packages/' + args.package_id + '/quote', {'workflow_id': args.workflow_id, 'scheme': args.scheme})
    if c == 'package-run':
        if args.quote_id:
            return go('POST', '/api/v1/packages/' + args.package_id + '/runs', {'quote_id': args.quote_id, 'budget_ceiling': args.budget_ceiling})
        return go('GET', '/api/v1/packages/runs/' + args.package_id)
    if c == 'package-runs':
        return go('GET', '/api/v1/packages/runs')
    if c == 'package-retry':
        return go('POST', '/api/v1/packages/runs/' + args.run_id + '/retry', {})
    if c == 'package-bundle':
        st, out = go('POST', '/api/v1/packages/runs/' + args.run_id + '/bundle', {'scope': json.loads(args.scope) if args.scope else None})
        if st == 200 and args.out:
            import base64
            fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(base64.b64decode(out['zip_base64']))
            out = {'written': args.out, 'zip_sha256': out['zip_sha256'], 'bytes': out['bytes'], 'verify': out['verify']}
        return st, out
    if c == 'reconcile':
        return go('POST', '/api/v1/reconciliations', json.load(open(args.file)))
    if c == 'reconciliations':
        return go('GET', '/api/v1/reconciliations' + ('/' + args.id if args.id else ''))
    if c == 'measurement-request':
        st, out = go('POST', '/api/v1/measurement-requests', json.load(open(args.file)))
        if st == 201 and args.out:
            st2, exp = go('GET', '/api/v1/measurement-requests/' + out['id'] + '/export')
            Path(args.out).write_text(json.dumps(exp, indent=1)); out = dict(out, written=args.out)
        return st, out
    if c == 'measurement-requests':
        return go('GET', '/api/v1/measurement-requests' + ('/' + args.id if args.id else ''))
    if c == 'package-upgrade-preview':
        return go('GET', '/api/v1/packages/%s/upgrade-preview/%s' % (args.package_id, args.new_package_id))
    if c == 'review-queue':
        return go('GET', '/api/v1/review-queue')
    if c == 'package-retire':
        return go('POST', '/api/v1/packages/' + args.package_id + '/retire', {})
    if c == 'bundle-export':
        st, out = go('POST', '/api/v1/bundles/export', {'name': args.name, 'services': args.services.split(',') if args.services else None, 'models': args.models.split(',') if args.models else [], 'verification_policies': args.policies.split(',') if args.policies else []})
        if st == 200 and args.out:
            Path(args.out).write_text(json.dumps(out, indent=1))
        return st, out
    if c == 'bundle-check':
        return go('POST', '/api/v1/bundles/check', {'bundle': json.load(open(args.file))})
    if c == 'bundle-import':
        return go('POST', '/api/v1/bundles/import', {'bundle': json.load(open(args.file)), 'apply': bool(args.apply)})
    if c == 'disagreements':
        return go('GET', '/api/v1/verification/disagreements')
    if c == 'disagreement-decide':
        return go('POST', '/api/v1/verification/' + args.verification_id + '/decide', {'decision': args.decision, 'note': args.note, 'evidence': json.loads(args.evidence)})
    if c == 'document-import':
        data = open(args.file, 'rb').read()
        import urllib.parse
        q = {'name': args.name or os.path.basename(args.file)}
        if args.collection: q['collection_id'] = args.collection
        if args.mode: q['mode'] = args.mode
        st, out = go('POST', '/api/v1/documents/import?' + urllib.parse.urlencode(q), data, content_type='application/pdf')
        if st != 202 or not args.wait:
            return st, out
        deadline = time.time() + args.timeout
        while time.time() < deadline:
            st2, v = go('GET', '/api/v1/documents/' + out['id'])
            if st2 != 200 or v['state'] not in ('received', 'validating', 'extracting'):
                return st2, v
            time.sleep(1)
        return st2, v
    if c == 'documents':
        return go('GET', '/api/v1/documents')
    if c == 'document-compare':
        return go('GET', '/api/v1/documents/%s/compare/%s' % (args.import_a, args.import_b))
    if c == 'document':
        return go('GET', '/api/v1/documents/' + args.import_id)
    if c == 'document-page':
        return go('GET', '/api/v1/documents/%s/pages/%d' % (args.import_id, args.index))
    if c == 'document-action':
        body = {}
        if args.action == 'retry' and args.mode: body['policy'] = {'mode': args.mode}
        if args.action == 'remove': body['confirm'] = bool(args.confirm)
        if args.action == 'publish': body['include_excluded_pages'] = bool(args.include_excluded)
        return go('POST', '/api/v1/documents/%s/%s' % (args.import_id, args.action), body)
    if c == 'table':
        return go('GET', '/api/v1/documents/tables/' + args.table_id)
    if c == 'table-annotate':
        return go('POST', '/api/v1/documents/tables/%s/annotations' % args.table_id, {'kind': args.kind, 'payload': json.loads(args.payload)})
    if c in ('mapping-preview', 'mapping-create'):
        mapping = json.load(open(args.file))
        return go('POST', '/api/v1/documents/tables/%s/mappings%s' % (args.table_id, '/preview' if c == 'mapping-preview' else ''), {'mapping': mapping})
    if c == 'mapping':
        return go('GET', '/api/v1/documents/mappings/' + args.mapping_id)
    if c == 'mapping-confirm':
        return go('POST', '/api/v1/documents/mappings/%s/confirm' % args.mapping_id, {})
    if c == 'intent':
        req = {'text': args.text}
        if args.inputs: req['inputs'] = json.load(open(args.inputs))
        if args.kind: req['kind'] = args.kind
        if args.collection: req['collection_id'] = args.collection
        if args.verify: req['verify'] = args.verify
        return go('POST', '/api/v1/agents/intents', {'request': req})
    if c == 'intents':
        return go('GET', '/api/v1/agents/intents')
    if c == 'intent-continue':
        body = {'token': args.token}
        if args.answers: body['answers'] = json.loads(args.answers)
        if args.inputs: body['inputs'] = json.load(open(args.inputs))
        return go('POST', '/api/v1/agents/intents/' + args.intent_id + '/continue', body)
    if c == 'plan':
        body = {'goal': args.goal}
        if args.kind: body['kind'] = args.kind
        if args.inputs: body['inputs'] = json.load(open(args.inputs))
        if args.verify: body['verify'] = args.verify
        if args.assist: body['assist'] = 'model'
        if args.collection: body['collection_id'] = args.collection
        return go('POST', '/api/v1/agents/plans', body)
    if c == 'plans':
        return go('GET', '/api/v1/agents/plans')
    if c == 'plan-accept':
        return go('POST', '/api/v1/agents/plans/' + args.plan_id + '/accept', {})
    if c == 'approval-propose':
        return go('POST', '/api/v1/approvals', {'action': args.action, 'content': json.loads(args.content), 'note': args.note})
    if c == 'approval-decide':
        return go('POST', '/api/v1/approvals/' + args.approval_id + '/' + args.decision, {'note': args.note})
    if c == 'approvals':
        return go('GET', '/api/v1/approvals')
    if c == 'approval-policy':
        if args.required is None:
            return go('GET', '/api/v1/approvals/policy')
        return go('POST', '/api/v1/approvals/policy', {'required': [x for x in args.required.split(',') if x]})
    if c == 'statement':
        q = '?since=%d' % args.since + ('&until=%d' % args.until if args.until else '')
        if args.csv:
            st, content = go('GET', '/api/v1/statements.csv' + q, raw=True)
            if st == 200:
                fd = os.open(args.csv, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, 'wb') as f:
                    f.write(content)
                return st, {'written': args.csv, 'bytes': len(content)}
            return st, json.loads(content)
        return go('GET', '/api/v1/statements' + q)
    if c == 'mcp-connection':
        return 200, {'server': 'python -m metacoin_service.mcp_server', 'transport': 'stdio', 'env': {'METACOIN_MCP_CREDENTIAL_FILE': '<private 0600 JSON file with {"token": ...}; issue a scoped credential with POST /api/v1/credentials>', 'METACOIN_MCP_BASE_URL': '<this API base URL (loopback http or https)>'},
                     'client_config_example': {'mcpServers': {'metacoin': {'command': 'python', 'args': ['-m', 'metacoin_service.mcp_server'], 'env': {'METACOIN_MCP_CREDENTIAL_FILE': '/private/path/mcp-credential.json', 'METACOIN_MCP_BASE_URL': 'http://127.0.0.1:8402'}}}},
                     'trust_boundary': 'the OS process and the credential file; every tool call is authorized server-side under that principal', 'protocol': 'MCP (mcp SDK 1.26.0), stdio; no network MCP transport is exposed'}
    return 400, {'error': True, 'code': 'UNKNOWN_COMMAND'}

WORK_COMMANDS = ('work-terms-create', 'work-terms', 'work-term', 'work-terms-freeze', 'work-terms-amend', 'work-terms-inspect', 'work-terms-evaluate', 'work-providers', 'work-provider-register', 'work-request-create', 'work-requests', 'work-request',
                 'work-request-action', 'work-offer', 'work-compare', 'work-award', 'work-awards', 'work-award-view', 'work-ack', 'work-evaluate', 'work-decide', 'work-verify', 'work-receipts', 'work-receipt-verify', 'work-dispute-open', 'work-dispute',
                 'work-disputes', 'work-bundle', 'work-audit-grant', 'work-audit-use', 'work-audit-revoke', 'work-pay', 'work-intents', 'work-intent', 'work-reconcile', 'work-refund', 'work-journal', 'work-journal-replay', 'work-treasury', 'work-treasury-allocate',
                 'work-exposure', 'work-status', 'work-notifications', 'work-measurements', 'work-mission-import', 'work-missions', 'work-mission', 'work-mission-draft', 'work-observation', 'work-rails', 'work-close', 'work-provider-history', 'work-provider-portfolio', 'work-provider-portfolio-set', 'work-package-preview', 'work-program-create', 'work-programs', 'work-program', 'work-program-run', 'work-program-close', 'work-pricing-experiment', 'work-pricing-experiments', 'work-challenge', 'work-challenges', 'work-challenge-package', 'work-reconciliation', 'work-mission-scenario', 'work-mission-scenarios')

MISSION_COMMANDS = ('native-sign-pending', 'native-signing-requests', 'native-operator', 'mission-plan', 'mission-bundle', 'missions', 'mission-templates', 'mission-create', 'mission', 'mission-ledger', 'mission-explain', 'mission-action', 'mission-authorize')
MARKET_COMMANDS = ('resources', 'resource-offer-publish', 'resource-offers', 'resource-quote', 'resource-reserve', 'resource-reservations', 'resource-reservation', 'resource-action', 'resource-storage-put')
SIGNER_COMMANDS = ('signer-health', 'signer-enroll', 'signer-rotate', 'signer-revoke', 'signer-delegate', 'signer-keys', 'native-keys', 'native-key-revoke', 'native-transfer-signed')
NATIVE_COMMANDS = SIGNER_COMMANDS + MARKET_COMMANDS + MISSION_COMMANDS + ('native-domain', 'native-domain-init', 'native-accounts', 'native-account', 'native-actor-create', 'native-fund', 'native-transfer', 'native-simulate', 'native-intents', 'native-intent', 'native-obligations', 'native-export', 'native-allocations',
                   'native-allocation', 'native-allocation-return', 'native-recover', 'native-peer-pin', 'native-peer', 'native-outbox', 'native-inbox', 'native-bundle-write', 'native-bundle-import', 'native-courier', 'native-observation', 'native-rotate', 'native-connectivity',
                   'native-recovery', 'native-reconcile', 'native-audit', 'native-export-record', 'native-check', 'native-vectors', 'native-model', 'native-spec', 'native-disputes', 'native-dispute-open', 'native-dispute-resolve',
                   'procurement-plan', 'procurement-benchmark', 'lunar-sources', 'lunar-products', 'lunar-product', 'lunar-download', 'lunar-example')


def _signer(args):
    from .native.signer import SignerClient
    return SignerClient(args.signer, args.signer_token_file)


def signer_commands(args, go):
    """Operator-controlled signing (order 2026-10-23 group A): the signer is a separate process; the service stores public identities
    and signatures. Enrollment registers the public key with the service; rotation/delegation proofs are produced by the signer."""
    c = args.command; key = getattr(args, 'idempotency_key', None)
    if c == 'signer-health':
        return _signer(args).health()
    if c == 'signer-enroll':
        st, e = _signer(args).call('POST', '/enroll', {'name': args.name, 'ceiling': args.ceiling})
        if st != 200:
            return st, e
        st2, acct = go('POST', '/api/v1/native/actors/enroll', {'name': args.name, 'public_hex': e['public_hex'], 'key_id': e['key_id'], 'signer_id': e['signer_id']}, idempotency_key=key)
        if st2 < 400:
            _signer(args).call('POST', '/bind', {'actor': args.name, 'account': acct['account_id']})
            if args.out:
                fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, 'w') as f:
                    json.dump({'token': acct['credential']['token']}, f)
                acct = {k: v for k, v in acct.items() if k != 'credential'}; acct['credential_file'] = args.out
        return st2, acct
    if c in ('signer-rotate', 'signer-delegate'):
        body = {'actor': args.actor, 'grace_seconds': args.grace_seconds} if c == 'signer-rotate' else {'actor': args.actor, 'name': args.name, 'recipients': args.recipients.split(','), 'ceiling': args.ceiling}
        st, r = _signer(args).call('POST', '/rotate' if c == 'signer-rotate' else '/delegate', body)
        if st != 200 or not r.get('registration'):
            return st, r
        st2, reg = go('POST', '/api/v1/native/accounts/%s/keys' % args.account, r['registration'], idempotency_key=key)
        return st2, {'signer': {k: r[k] for k in r if k != 'registration'}, 'service_registration': reg}
    if c == 'signer-revoke':
        st, r = _signer(args).call('POST', '/revoke', {'actor': args.actor, 'key_id': args.key_id})
        st2, s2 = go('POST', '/api/v1/native/accounts/%s/keys/%s/revoke' % (args.account, args.key_id), {}, idempotency_key=key) if st == 200 else (st, r)
        return st2, {'signer': r, 'service': s2}
    if c == 'signer-keys':
        return _signer(args).call('GET', '/keys/' + args.actor)
    if c == 'native-keys':
        return go('GET', '/api/v1/native/accounts/%s/keys' % args.account)
    if c == 'native-key-revoke':
        return go('POST', '/api/v1/native/accounts/%s/keys/%s/revoke' % (args.account, args.key_id), {}, idempotency_key=key)
    if c == 'native-transfer-signed':
        body = {'to_account': args.to_account, 'amount': args.amount, 'request_id': args.request_id}
        for k in ('asset', 'from_account', 'obligation_id'):
            if getattr(args, k, None): body[k] = getattr(args, k)
        st, prep = go('POST', '/api/v1/native/transfers/prepare', body)
        if st >= 400:
            return st, prep
        st2, sig = _signer(args).sign(args.actor, prep['message'], getattr(args, 'delegate', None))
        if st2 != 200:
            return st2, {'prepared': prep['message_digest'], 'signer_refusal': sig}
        return go('POST', '/api/v1/native/transfers/submit', {'message': prep['message'], 'signature_hex': sig['signature_hex'], 'key_id': sig['key_id']}, idempotency_key=key)
    return 400, {'error': True, 'code': 'UNKNOWN_COMMAND'}


def mission_commands(args, go):
    c = args.command; key = getattr(args, 'idempotency_key', None); jf = (lambda: json.load(open(args.file))) if getattr(args, 'file', None) else (lambda: {})
    if c == 'native-signing-requests':
        return go('GET', '/api/v1/native/signing/pending' + ('?account=' + args.account if args.account else ''))
    if c == 'native-sign-pending':
        from .native.signer import SignerClient
        sg = SignerClient(args.signer, args.signer_token); st, pend = go('GET', '/api/v1/native/signing/pending' + ('?account=' + args.account if args.account else ''))
        if st != 200:
            return st, pend
        out = []
        for req in pend['items']:
            ss, sig = sg.sign(args.actor, req['message'], context=req['context'])
            if ss != 200:
                out.append({'request': req['id'], 'kind': req['kind'], 'signer_refusal': sig}); continue
            st2, r = go('POST', '/api/v1/native/signing/requests/%s/submit' % req['id'], {'signature_hex': sig['signature_hex'], 'key_id': sig['key_id']}, idempotency_key=req['id'])
            out.append({'request': req['id'], 'kind': req['kind'], 'submitted': st2, 'state': r.get('state'), 'result': {k: (r.get('result') or {}).get(k) for k in ('id', 'state', 'final_amount')}})
        return 200, {'account': pend.get('account'), 'processed': out, 'custody': pend.get('custody')}
    if c == 'native-operator':
        return go('GET', '/api/v1/native/operator')
    if c == 'mission-plan':
        return go('POST', '/api/v1/missions/%s/plan' % args.mission_id, {'objective': args.objective, 'planner': args.planner, 'apply': bool(args.apply)}, idempotency_key=key)
    if c == 'mission-bundle':
        st, b = go('GET', '/api/v1/missions/%s/bundle' % args.mission_id)
        if st < 400 and args.out:
            Path(args.out).write_text(json.dumps(b)); return st, {'written': args.out, 'seal': b.get('seal')}
        return st, b
    if c == 'missions':
        return go('GET', '/api/v1/missions')
    if c == 'mission-templates':
        return go('GET', '/api/v1/missions/templates')
    if c == 'mission-create':
        body = jf() if args.file else {'title': args.template, 'template': args.template, 'policy': json.loads(args.policy) if args.policy else {}}
        st, m = go('POST', '/api/v1/missions', body, idempotency_key=key)
        if st < 400 and args.freeze:
            return go('POST', '/api/v1/missions/' + m['id'] + '/freeze', {}, idempotency_key=(key + '-freeze') if key else None)
        return st, m
    if c == 'mission':
        return go('GET', '/api/v1/missions/' + args.mission_id)
    if c == 'mission-ledger':
        return go('GET', '/api/v1/missions/' + args.mission_id + '/ledger')
    if c == 'mission-explain':
        return go('GET', '/api/v1/missions/' + args.mission_id + '/explain')
    if c == 'mission-action':
        return go('POST', '/api/v1/missions/%s/%s' % (args.mission_id, args.action), jf(), idempotency_key=key)
    if c == 'mission-authorize':
        return go('POST', '/api/v1/missions/%s/revisions/%d/authorize' % (args.mission_id, args.revision), {}, idempotency_key=key)
    return 400, {'error': True, 'code': 'UNKNOWN_COMMAND'}


def market_commands(args, go):
    c = args.command; key = getattr(args, 'idempotency_key', None); jf = (lambda: json.load(open(args.file))) if getattr(args, 'file', None) else (lambda: {})
    if c == 'resources':
        return go('GET', '/api/v1/resources')
    if c == 'resource-offer-publish':
        return go('POST', '/api/v1/resources/offers', jf(), idempotency_key=key)
    if c == 'resource-offers':
        return go('GET', '/api/v1/resources/offers' + ('?kind=' + args.kind if args.kind else ''))
    if c == 'resource-quote':
        return go('POST', '/api/v1/resources/quote', {'offer_id': args.offer_id, 'quantity': args.quantity})
    if c == 'resource-reserve':
        return go('POST', '/api/v1/resources/reservations', {'offer_id': args.offer_id, 'offer_version': args.offer_version, 'quantity': args.quantity, 'request_id': args.request_id}, idempotency_key=key)
    if c == 'resource-reservations':
        return go('GET', '/api/v1/resources/reservations' + ('?account=' + args.account if args.account else ''))
    if c == 'resource-reservation':
        return go('GET', '/api/v1/resources/reservations/' + args.reservation_id)
    if c == 'resource-action':
        return go('POST', '/api/v1/resources/reservations/%s/%s' % (args.reservation_id, args.action), jf(), idempotency_key=key)
    if c == 'resource-storage-put':
        with open(args.path, 'rb') as f:
            data = f.read()
        return go('POST', '/api/v1/resources/reservations/%s/storage' % args.reservation_id, data, idempotency_key=key)
    return 400, {'error': True, 'code': 'UNKNOWN_COMMAND'}


def native(args, go):
    """Native protocol commands (order 2026-10-22): one authenticated HTTP request each unless marked offline. Reading never
    spends; native-transfer / native-export / native-fund are the consequential ones and are idempotent by request id."""
    c = args.command
    if c in SIGNER_COMMANDS:
        return signer_commands(args, go)
    if c in MARKET_COMMANDS:
        return market_commands(args, go)
    if c in MISSION_COMMANDS:
        return mission_commands(args, go)
    jf = (lambda: json.load(open(args.file))) if getattr(args, 'file', None) else (lambda: {})
    key = getattr(args, 'idempotency_key', None)
    if c == 'native-domain':
        return go('GET', '/api/v1/native/domain')
    if c == 'native-domain-init':
        return go('POST', '/api/v1/native/domain', {'domain_id': args.domain_id, 'operator': args.operator, 'host': args.host}, idempotency_key=key)
    if c == 'native-accounts':
        return go('GET', '/api/v1/native/accounts')
    if c == 'native-account':
        return go('GET', '/api/v1/native/accounts/' + args.account_id)
    if c == 'native-actor-create':
        st, out = go('POST', '/api/v1/native/actors', {'name': args.name}, idempotency_key=key)
        if st < 400 and args.out:
            fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as f:
                json.dump({'token': out['credential']['token']}, f)
            out = {k: v for k, v in out.items() if k != 'credential'}; out['credential_file'] = args.out
        return st, out
    if c == 'native-fund':
        return go('POST', '/api/v1/native/fixtures/fund', {'account_id': args.account_id, 'amount': args.amount, 'label': 'cli fixture'}, idempotency_key=key)
    if c in ('native-transfer', 'native-simulate'):
        body = {'to_account': args.to_account, 'amount': args.amount, 'request_id': args.request_id}
        if args.asset: body['asset'] = args.asset
        if getattr(args, 'from_account', None): body['from_account'] = args.from_account
        if getattr(args, 'obligation_id', None): body['obligation_id'] = args.obligation_id
        return go('POST', '/api/v1/native/transfers' + ('/simulate' if c == 'native-simulate' else ''), body, idempotency_key=key)
    if c == 'native-intents':
        return go('GET', '/api/v1/native/intents' + ('?account=' + args.account if args.account else ''))
    if c == 'native-intent':
        return go('GET', '/api/v1/native/intents/' + args.intent_id)
    if c == 'native-obligations':
        return go('GET', '/api/v1/native/obligations' + ('?account=' + args.account if args.account else ''))
    if c == 'native-export':
        body = {'destination_domain': args.destination, 'recipient_account': args.recipient, 'amount': args.amount, 'request_id': args.request_id}
        if args.asset: body['asset'] = args.asset
        return go('POST', '/api/v1/native/allocations/export', body, idempotency_key=key)
    if c == 'native-allocations':
        return go('GET', '/api/v1/native/allocations')
    if c == 'native-allocation':
        return go('GET', '/api/v1/native/allocations/' + args.allocation_id)
    if c == 'native-allocation-return':
        return go('POST', '/api/v1/native/allocations/' + args.allocation_id + '/return', {}, idempotency_key=key)
    if c == 'native-recover':
        return go('POST', '/api/v1/native/allocations/recover', {}, idempotency_key=key)
    if c == 'native-peer-pin':
        return go('POST', '/api/v1/native/peers', jf(), idempotency_key=key)
    if c == 'native-peer':
        return go('GET', '/api/v1/native/peers/' + args.domain_id)
    if c == 'native-outbox':
        return go('GET', '/api/v1/native/transport/outbox?receiver=%s&transport=%s' % (args.receiver, args.transport))
    if c == 'native-inbox':
        return go('POST', '/api/v1/native/transport/inbox?transport=' + args.transport, jf())
    if c in ('native-bundle-write', 'native-bundle-import', 'native-courier'):
        from .native import courier
        src = courier.Endpoint(args.base, args.credential_file)
        if c == 'native-bundle-write':
            return 200, courier.write_bundle(src, args.receiver, args.path)
        if c == 'native-bundle-import':
            return 200, courier.import_bundle(src, args.path, args.replies)
        dst = courier.Endpoint(args.dst_base, args.dst_credential_file)
        out = courier.push(src, dst, args.receiver); return (200 if out.get('ok') else 409), out
    if c == 'native-observation':
        return go('POST', '/api/v1/native/transport/observation', {'peer': args.peer}, idempotency_key=key)
    if c == 'native-rotate':
        return go('POST', '/api/v1/native/domain/rotate', {'grace_seconds': args.grace_seconds}, idempotency_key=key)
    if c == 'native-connectivity':
        return go('POST', '/api/v1/native/domain/connectivity', {'state': args.state}, idempotency_key=key)
    if c == 'native-recovery':
        return go('GET', '/api/v1/native/recovery')
    if c == 'native-reconcile':
        return go('POST', '/api/v1/native/recovery/reconcile', jf(), idempotency_key=key)
    if c == 'native-audit':
        return go('GET', '/api/v1/native/audit' + ('?asset=' + args.asset if args.asset else ''))
    if c == 'native-export-record':
        st, out = go('GET', '/api/v1/native/export')
        if st < 400 and args.out:
            with open(args.out, 'w') as f:
                json.dump(out, f)
            out = {'written': args.out, 'domain': out.get('domain'), 'receipts': len(out.get('receipts', [])), 'intents': len(out.get('intents', []))}
        return st, out
    if c == 'native-check':                                                          # offline
        from .native import checker
        rep = checker.check(json.load(open(args.export)), json.load(open(args.roots)), args.environment); return (200 if all(v in (True, None) for v in rep['summary'].values()) else 409), rep
    if c == 'native-vectors':                                                        # offline
        from .native import vectors
        out = vectors.run(); return (200 if out['all_as_expected'] else 409), out
    if c == 'native-model':                                                          # offline
        from .native import model
        out = model.explore(args.depth); return (200 if out['violation_count'] == 0 else 409), out
    if c == 'native-spec':
        return go('GET', '/api/v1/native/spec')
    if c == 'native-disputes':
        return go('GET', '/api/v1/native/disputes')
    if c == 'native-dispute-open':
        return go('POST', '/api/v1/native/disputes', {'obligation_id': args.obligation_id, 'kind': args.kind, 'claim': args.claim}, idempotency_key=key)
    if c == 'native-dispute-resolve':
        return go('POST', '/api/v1/native/disputes/' + args.dispute_id + '/resolve', {'outcome': args.outcome, 'reason': args.reason or ''}, idempotency_key=key)
    if c == 'procurement-plan':
        return go('POST', '/api/v1/native/procurement/plan', dict(jf(), policy=args.policy))
    if c == 'procurement-benchmark':
        return go('GET', '/api/v1/native/procurement/benchmark?missions=%d&seed=%d' % (args.missions, args.seed))
    if c == 'lunar-sources':
        return go('GET', '/api/v1/lunar/sources')
    if c == 'lunar-products':
        return go('GET', '/api/v1/lunar/products')
    if c == 'lunar-product':
        return go('GET', '/api/v1/lunar/products/' + args.job_id)
    if c == 'lunar-example':
        return go('GET', '/api/v1/lunar/examples/%s?source=%s' % (args.name, args.source))
    if c == 'lunar-download':
        st, out = go('GET', '/api/v1/lunar/products/' + args.job_id + '/download', raw=True)
        if st < 400:
            with open(args.out, 'wb') as f:
                f.write(out)
            return st, {'written': args.out, 'bytes': len(out)}
        return st, out
    return 400, {'error': True, 'code': 'UNKNOWN_COMMAND'}


DECISION_COMMANDS = ('decision-analyze', 'decisions', 'decision', 'decision-projection', 'decision-witnesses', 'decision-bundle', 'acquisition-preview', 'acquisition-plan', 'acquisition-plans', 'acquisition-materialize',
                     'work-deliver', 'work-delivery-spec', 'work-assurance', 'sampling-plan', 'sampling-plans', 'sampling-audit', 'sampling-verify', 'assurance-evaluate', 'assurance-claims', 'verification-assurance',
                     'portfolio-create', 'portfolios', 'portfolio', 'portfolio-select', 'fault-suite', 'campaign', 'campaigns')


def decision(args, go):
    """Decision-evidence commands (order of 2026-10-22): analyses, acquisition plans, deliveries, assurance previews, sampling
    plans and audits, portfolios. One authenticated HTTP request each; analysis never implies spending authority."""
    c = args.command
    jf = (lambda: json.load(open(args.file))) if getattr(args, 'file', None) else (lambda: {})
    key = getattr(args, 'idempotency_key', None)
    if c == 'decision-analyze':
        return go('POST', '/api/v1/decisions/analyze', {'inputs': jf(), 'title': args.title or 'decision observability'}, idempotency_key=key)
    if c == 'decisions':
        return go('GET', '/api/v1/decisions' + ('?kind=' + args.kind if args.kind else ''))
    if c == 'decision':
        return go('GET', '/api/v1/decisions/' + args.job_id)
    if c == 'decision-projection':
        return go('GET', '/api/v1/decisions/' + args.job_id + '/projection')
    if c == 'decision-witnesses':
        return go('GET', '/api/v1/decisions/' + args.job_id + '/witnesses')
    if c == 'decision-bundle':
        status, content = go('GET', '/api/v1/decisions/' + args.job_id + '/bundle', raw=True)
        if status == 200:
            fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as fh:
                fh.write(content)
            return status, {'written': args.out, 'bytes': len(content), 'replay': 'python -m metacoin_service.decision.replay %s --trust-root <service public key hex> --json' % args.out}
        return status, json.loads(content)
    if c in ('acquisition-preview', 'acquisition-plan'):
        body = dict(jf(), analysis_job_id=args.job_id, budget=args.budget)
        return go('POST', '/api/v1/acquisition-plans' + ('/preview' if c == 'acquisition-preview' else ''), body, idempotency_key=key)
    if c == 'acquisition-plans':
        return go('GET', '/api/v1/acquisition-plans')
    if c == 'acquisition-materialize':
        body = {'diagnostic_amount': args.diagnostic}
        if args.title:
            body['title'] = args.title
        return go('POST', '/api/v1/acquisition-plans/' + args.plan_id + '/materialize', body, idempotency_key=key)
    if c == 'work-deliver':
        return go('POST', '/api/v1/work/awards/%s/milestones/%s/deliver' % (args.award_id, args.milestone), {'delivery': jf()}, idempotency_key=key)
    if c == 'work-delivery-spec':
        return go('GET', '/api/v1/work/awards/%s/milestones/%s/delivery-spec' % (args.award_id, args.milestone))
    if c == 'work-assurance':
        return go('GET', '/api/v1/work/awards/%s/milestones/%s/assurance' % (args.award_id, args.milestone))
    if c == 'sampling-plan':
        body = {'job_id': args.job_id, 'alpha': args.alpha, 'max_samples': args.max_samples}
        if args.defect_fraction:
            body['defect_fraction'] = args.defect_fraction
        else:
            body['minimum_defects'] = args.minimum_defects
        if args.preview:
            body['preview'] = True
        return go('POST', '/api/v1/verification/sampling-plans', body)
    if c == 'sampling-plans':
        return go('GET', '/api/v1/verification/sampling-plans' + ('/' + args.plan_id if args.plan_id else ('?job_id=' + args.job_id if args.job_id else '')))
    if c == 'sampling-audit':
        return go('POST', '/api/v1/verification/sampling-plans/' + args.plan_id + '/audit', {}, idempotency_key=key)
    if c == 'sampling-verify':
        return go('POST', '/api/v1/verification/sampling-plans/verify', {'bundle': jf()})
    if c == 'assurance-evaluate':
        return go('POST', '/api/v1/verification/assurance/evaluate', {'job_id': args.job_id, 'requirement': jf()})
    if c == 'assurance-claims':
        return go('GET', '/api/v1/verification/assurance/claims')
    if c == 'verification-assurance':
        return go('GET', '/api/v1/verification/' + args.verification_id + '/assurance')
    if c == 'portfolio-create':
        return go('POST', '/api/v1/portfolios', jf(), idempotency_key=key)
    if c == 'portfolios':
        return go('GET', '/api/v1/portfolios')
    if c == 'portfolio':
        return go('GET', '/api/v1/portfolios/' + args.portfolio_id)
    if c == 'portfolio-select':
        return go('POST', '/api/v1/portfolios/select', {'suite_id': args.suite_id, 'required_families': args.families.split(','), **({'max_cost': args.max_cost} if args.max_cost is not None else {})})
    if c == 'fault-suite':
        return go('POST', '/api/v1/portfolios/fault-suite', jf(), idempotency_key=key)
    if c == 'campaign':
        return go('POST', '/api/v1/portfolios/campaigns', {'suite_id': args.suite_id, 'portfolio_ids': args.portfolio_ids.split(','), 'split': args.split}, idempotency_key=key)
    if c == 'campaigns':
        return go('GET', '/api/v1/portfolios/campaigns' + ('/' + args.campaign_id if args.campaign_id else ''))
    return 2, {'error': 'unknown decision command'}


def work(args, go):
    """Work-economy commands: every call is one authenticated HTTP request (or a documented short sequence); idempotency keys
    are passed through unchanged so a retry never creates a second award, decision or payment identity."""
    c = args.command
    jf = (lambda: json.load(open(args.file))) if getattr(args, 'file', None) else (lambda: {})
    key = getattr(args, 'idempotency_key', None)
    if c == 'work-terms-create':
        body = jf() if args.file else {'template': args.template, 'ceiling': args.ceiling}
        if args.file and args.template:
            body['template'] = args.template
        return go('POST', '/api/v1/work/terms', body, idempotency_key=key)
    if c == 'work-terms':
        return go('GET', '/api/v1/work/terms' + ('?state=' + args.state if args.state else ''))
    if c == 'work-term':
        return go('GET', '/api/v1/work/terms/' + args.terms_id)
    if c == 'work-terms-inspect':
        return go('GET', '/api/v1/work/terms/' + args.terms_id + '/inspect')
    if c == 'work-terms-freeze':
        body = jf()
        if args.contract_id:
            body['contract_id'] = args.contract_id
        return go('POST', '/api/v1/work/terms/' + args.terms_id + '/freeze', body, idempotency_key=key)
    if c == 'work-terms-amend':
        body = {'terms': jf()}
        if args.expected_version is not None:
            body['expected_version'] = args.expected_version
        return go('POST', '/api/v1/work/terms/' + args.terms_id + '/amend', body, idempotency_key=key)
    if c == 'work-terms-evaluate':
        return go('POST', '/api/v1/work/terms/' + args.terms_id + '/evaluate', {'job_id': args.job_id} if args.job_id else {})
    if c == 'work-providers':
        return go('GET', '/api/v1/work/providers')
    if c == 'work-provider-register':
        return go('POST', '/api/v1/work/providers', jf(), idempotency_key=key)
    if c == 'work-request-create':
        return go('POST', '/api/v1/work/requests', {'terms_id': args.terms_id}, idempotency_key=key)
    if c == 'work-requests':
        return go('GET', '/api/v1/work/requests' + ('?state=' + args.state if args.state else ''))
    if c == 'work-request':
        return go('GET', '/api/v1/work/requests/' + args.request_id)
    if c == 'work-request-action':
        return go('POST', '/api/v1/work/requests/' + args.request_id + '/' + args.action, jf(), idempotency_key=key)
    if c == 'work-offer':
        body = jf() if args.file else {'price_amount': args.price, 'asset': args.asset, 'scheme': args.scheme, 'window_seconds': args.window, 'verification': {'class': args.verification_class, 'distinct_verifier': False}}
        return go('POST', '/api/v1/work/requests/' + args.request_id + '/offers', body, idempotency_key=key)
    if c == 'work-compare':
        return go('GET', '/api/v1/work/requests/' + args.request_id + '/compare')
    if c == 'work-award':
        body = {}
        if args.offer_id: body['offer_id'] = args.offer_id
        if args.reason: body['reason'] = args.reason
        if args.expected_price is not None: body['expected_price'] = args.expected_price
        return go('POST', '/api/v1/work/requests/' + args.request_id + '/award', body, idempotency_key=key)
    if c == 'work-awards':
        return go('GET', '/api/v1/work/awards' + ('?state=' + args.state if args.state else ''))
    if c == 'work-award-view':
        return go('GET', '/api/v1/work/awards/' + args.award_id)
    if c == 'work-ack':
        return go('POST', '/api/v1/work/awards/' + args.award_id + '/ack', {}, idempotency_key=key)
    if c == 'work-evaluate':
        return go('POST', '/api/v1/work/awards/%s/milestones/%s/evaluate' % (args.award_id, args.milestone), {})
    if c == 'work-decide':
        return go('POST', '/api/v1/work/awards/%s/milestones/%s/decide' % (args.award_id, args.milestone), {'decision': args.decision, 'reason': args.reason}, idempotency_key=key)
    if c == 'work-verify':
        return go('POST', '/api/v1/work/awards/%s/milestones/%s/verify' % (args.award_id, args.milestone), {'class': args.verification_class} if args.verification_class else {}, idempotency_key=key)
    if c == 'work-receipts':
        return go('GET', '/api/v1/work/awards/' + args.award_id + '/receipts')
    if c == 'work-program-create':
        return go('POST', '/api/v1/work/programs', jf(), idempotency_key=key)
    if c == 'work-programs':
        return go('GET', '/api/v1/work/programs')
    if c == 'work-program':
        return go('GET', '/api/v1/work/programs/' + args.program_id)
    if c == 'work-program-run':
        return go('POST', '/api/v1/work/programs/' + args.program_id + '/runs', dict(jf(), **({'ceiling': args.ceiling} if args.ceiling else {})), idempotency_key=key)
    if c == 'work-program-close':
        return go('POST', '/api/v1/work/programs/' + args.program_id + '/close', {})
    if c == 'work-pricing-experiment':
        return go('POST', '/api/v1/work/pricing-experiments', jf())
    if c == 'work-pricing-experiments':
        return go('GET', '/api/v1/work/pricing-experiments' + ('/' + args.experiment_id if args.experiment_id else ''))
    if c == 'work-challenge':
        return go('POST', '/api/v1/work/receipts/' + args.receipt_id + '/challenge', dict(jf(), claim=args.claim, asserted_outcome=args.asserted), idempotency_key=key)
    if c == 'work-challenges':
        return go('GET', '/api/v1/work/challenges' + ('/' + args.challenge_id if args.challenge_id else ''))
    if c == 'work-challenge-package':
        return go('GET', '/api/v1/work/challenges/' + args.challenge_id + '/package', raw_out=args.out) if 'raw_out' in go.__code__.co_varnames else go('GET', '/api/v1/work/challenges/' + args.challenge_id)
    if c == 'work-mission-scenario':
        return go('POST', '/api/v1/work/missions/' + args.portfolio_id + '/scenarios', jf())
    if c == 'work-mission-scenarios':
        return go('GET', '/api/v1/work/missions/' + args.portfolio_id + '/scenarios' + ('/' + args.scenario_id if args.scenario_id else ''))
    if c == 'work-reconciliation':
        return go('GET', '/api/v1/work/reconciliation')
    if c == 'work-provider-history':
        return go('GET', '/api/v1/work/providers/' + args.provider_id + '/history' + ('?scope=disclosed' if args.disclosed else ''))
    if c == 'work-provider-portfolio':
        return go('GET', '/api/v1/work/providers/' + args.provider_id + '/portfolio')
    if c == 'work-provider-portfolio-set':
        return go('POST', '/api/v1/work/providers/' + args.provider_id + '/portfolio', {'receipt_ids': args.receipt_ids.split(','), 'note': args.note or ''})
    if c == 'work-package-preview':
        import base64
        return go('POST', '/api/v1/work/packages/import-preview', {'package_b64': base64.b64encode(open(args.package, 'rb').read()).decode(), 'trust_roots': [x for x in (args.trust_roots or '').split(',') if x]})
    if c == 'work-receipt-verify':
        return go('POST', '/api/v1/work/receipts/' + args.receipt_id + '/verify', {'trust_root': args.trust_root} if args.trust_root else {})
    if c == 'work-dispute-open':
        return go('POST', '/api/v1/work/awards/%s/milestones/%s/dispute' % (args.award_id, args.milestone), {'claim': args.claim, 'scope': args.scope}, idempotency_key=key)
    if c == 'work-dispute':
        if args.action:
            return go('POST', '/api/v1/work/disputes/%s/%s' % (args.dispute_id, args.action), jf(), idempotency_key=key)
        return go('GET', '/api/v1/work/disputes/' + args.dispute_id)
    if c == 'work-disputes':
        return go('GET', '/api/v1/work/disputes')
    if c == 'work-bundle':
        status, content = go('GET', '/api/v1/work/awards/%s/milestones/%s/bundle?scope=%s' % (args.award_id, args.milestone, args.scope), raw=True)
        if status == 200:
            fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(content)
            return status, {'written': args.out, 'bytes': len(content)}
        return status, json.loads(content)
    if c == 'work-audit-grant':
        return go('POST', '/api/v1/work/awards/' + args.award_id + '/audit-grants', {'grantee_id': args.grantee, 'purpose': args.purpose, 'categories': args.categories.split(','), 'expires_in_seconds': args.expires}, idempotency_key=key)
    if c == 'work-audit-use':
        return go('POST', '/api/v1/work/audit-grants/' + args.grant_id + '/use', {})
    if c == 'work-audit-revoke':
        return go('POST', '/api/v1/work/audit-grants/' + args.grant_id + '/revoke', {}, idempotency_key=key)
    if c == 'work-pay':
        st, i = go('POST', '/api/v1/work/entitlements/' + args.entitlement_id + '/prepare', {}, idempotency_key=key)
        if st >= 400:
            return st, i
        st2, a = go('POST', '/api/v1/work/intents/' + i['id'] + '/authorize', {}, idempotency_key=(key + '-auth') if key else None)
        if st2 >= 400 or args.authorize_only:
            return st2, a
        return go('POST', '/api/v1/work/intents/' + i['id'] + '/submit', {}, idempotency_key=(key + '-submit') if key else None)
    if c == 'work-intents':
        return go('GET', '/api/v1/work/intents' + ('?state=' + args.state if args.state else ''))
    if c == 'work-intent':
        return go('GET', '/api/v1/work/intents/' + args.intent_id)
    if c == 'work-reconcile':
        return go('POST', '/api/v1/work/intents/' + args.intent_id + '/reconcile', {}, idempotency_key=key)
    if c == 'work-refund':
        return go('POST', '/api/v1/work/entitlements/' + args.entitlement_id + '/refund', {'amount': args.amount, 'provider_preauthorized': True, 'request_key': args.request_key}, idempotency_key=key)
    if c == 'work-journal':
        return go('GET', '/api/v1/work/journal')
    if c == 'work-journal-replay':
        return go('POST', '/api/v1/work/journal/replay', {})
    if c == 'work-treasury':
        return go('GET', '/api/v1/work/treasury?asset=' + args.asset)
    if c == 'work-treasury-allocate':
        return go('POST', '/api/v1/work/treasury/allocate', {'terms_id': args.terms_id}, idempotency_key=key)
    if c == 'work-exposure':
        return go('GET', '/api/v1/work/exposure')
    if c == 'work-status':
        return go('GET', '/api/v1/work/status')
    if c == 'work-notifications':
        return go('GET', '/api/v1/work/notifications')
    if c == 'work-measurements':
        return go('GET', '/api/v1/work/measurements')
    if c == 'work-mission-import':
        return go('POST', '/api/v1/work/missions/import', {}, idempotency_key=key)
    if c == 'work-missions':
        return go('GET', '/api/v1/work/missions')
    if c == 'work-mission':
        return go('GET', '/api/v1/work/missions/' + args.portfolio_id)
    if c == 'work-mission-draft':
        body = jf(); body.setdefault('ceiling', args.ceiling)
        return go('POST', '/api/v1/work/missions/%s/bottlenecks/%s/draft' % (args.portfolio_id, args.task), body, idempotency_key=key)
    if c == 'work-observation':
        return go('POST', '/api/v1/work/observations', {'package': jf()}, idempotency_key=key)
    if c == 'work-rails':
        return go('GET', '/api/v1/work/rails')
    if c == 'work-close':
        return go('POST', '/api/v1/work/awards/' + args.award_id + '/close', {}, idempotency_key=key)
    raise SystemExit('unknown work command')


def main(argv=None):
    parser = argparse.ArgumentParser(prog='metacoin-client', description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--credential-file', default=DEFAULT_CREDENTIAL)
    parser.add_argument('--base', default=os.environ.get('METACOIN_SERVICE_URL', 'http://127.0.0.1:8402'))
    sub = parser.add_subparsers(dest='command', required=True)
    c = sub.add_parser('create', help='create a draft contract from an inputs JSON file'); c.add_argument('--kind', default='energy_audit')
    c.add_argument('--title', required=True); c.add_argument('--inputs', required=True, help='path to inputs JSON (read locally, sent over the API)')
    c.add_argument('--reviewer', required=True); c.add_argument('--policy', help='JSON object of policy overrides')
    f = sub.add_parser('freeze'); f.add_argument('contract_id')
    s = sub.add_parser('submit'); s.add_argument('contract_id'); s.add_argument('--idempotency-key'); s.add_argument('--reuse', action='store_true', help='reuse an identical committed result if the policy allows')
    p = sub.add_parser('poll', help='wait until the job is terminal'); p.add_argument('job_id'); p.add_argument('--timeout', type=int, default=120)
    st = sub.add_parser('status'); st.add_argument('job_id')
    r = sub.add_parser('result'); r.add_argument('job_id')
    rr = sub.add_parser('review-request'); rr.add_argument('job_id')
    d = sub.add_parser('decide'); d.add_argument('job_id'); d.add_argument('decision', choices=('accepted', 'rejected'))
    e = sub.add_parser('export', help='download an artifact (public JSON or private ciphertext) to a fresh file')
    e.add_argument('artifact_id'); e.add_argument('--out', required=True)
    cm = sub.add_parser('compare'); cm.add_argument('job_a'); cm.add_argument('job_b')
    sub.add_parser('me'); sub.add_parser('budget')
    # workflows, datasets, campaigns, services, agents, usage, queue, events
    ds = sub.add_parser('dataset-create', help='upload a bounded CSV/JSON dataset version'); ds.add_argument('--name', required=True); ds.add_argument('--kind', required=True)
    ds.add_argument('--file', required=True); ds.add_argument('--format', default='csv'); ds.add_argument('--provenance', default='declared')
    sub.add_parser('datasets')
    wf = sub.add_parser('workflow-create', help='register a workflow definition from a JSON file'); wf.add_argument('--file', required=True)
    wr = sub.add_parser('workflow-run'); wr.add_argument('definition_id'); wr.add_argument('--bindings', help='JSON object slot->dataset version id'); wr.add_argument('--budget-ceiling', type=int); wr.add_argument('--preview', action='store_true')
    wi = sub.add_parser('workflow-instantiate', help='fill a template\'s parameter slots into a new immutable definition'); wi.add_argument('definition_id'); wi.add_argument('--values', required=True, help='JSON object slot->integer'); wi.add_argument('--name')
    sc = sub.add_parser('schedule-create', help='schedule a bounded workflow at local times in an IANA zone'); sc.add_argument('definition_id'); sc.add_argument('--timezone', required=True); sc.add_argument('--times', required=True, help='comma-separated HH:MM'); sc.add_argument('--bindings'); sc.add_argument('--budget-ceiling', type=int); sc.add_argument('--overlap', choices=('skip', 'queue'), default='skip'); sc.add_argument('--max-runs', type=int, default=30)
    sub.add_parser('schedules'); sct = sub.add_parser('schedule-control'); sct.add_argument('schedule_id'); sct.add_argument('action', choices=('enable', 'disable', 'run-now', 'delete'))
    rs = sub.add_parser('run-status'); rs.add_argument('run_id'); rs.add_argument('--follow', action='store_true'); rs.add_argument('--timeout', type=int, default=300)
    rc = sub.add_parser('run-cancel'); rc.add_argument('run_id')
    cc = sub.add_parser('campaign-create', help='create a scientific campaign from a JSON definition file'); cc.add_argument('--file', required=True); cc.add_argument('--preview', action='store_true')
    cs = sub.add_parser('campaign-status'); cs.add_argument('campaign_id'); cs.add_argument('--results', action='store_true'); cs.add_argument('--csv', help='write results CSV to this new file')
    cb = sub.add_parser('campaign-branch', help='fork a campaign with explicit changed assumptions'); cb.add_argument('campaign_id'); cb.add_argument('--base-changes', help='JSON object field->integer'); cb.add_argument('--changes', help='JSON list of typed changes [{path, value, source, note?}]'); cb.add_argument('--expected-head', help='refuse when a newer branch of the campaign exists'); cb.add_argument('--candidates', help='comma-separated succeeded candidate indexes'); cb.add_argument('--name')
    cpl = sub.add_parser('campaign-plan', help='rank validated campaign candidates under a cost cap (quality vs cost)'); cpl.add_argument('campaign_id'); cpl.add_argument('--cost-cap', type=int, required=True)
    cc2 = sub.add_parser('campaign-compare'); cc2.add_argument('campaign_a'); cc2.add_argument('campaign_b')
    cp = sub.add_parser('campaign-control'); cp.add_argument('campaign_id'); cp.add_argument('action', choices=('run', 'pause', 'resume', 'cancel'))
    sub.add_parser('services'); sq = sub.add_parser('quote'); sq.add_argument('service_id'); sq.add_argument('--inputs', required=True); sq.add_argument('--accept', action='store_true')
    iv = sub.add_parser('invoke'); iv.add_argument('service_id'); iv.add_argument('--quote', required=True); iv.add_argument('--inputs', required=True)
    sub.add_parser('usage'); sub.add_parser('queue'); sub.add_parser('workers'); sub.add_parser('grants')
    gi = sub.add_parser('grant-issue', help='issue an agent policy grant from a JSON policy file; the token is written to --out (0600)'); gi.add_argument('--policy', required=True); gi.add_argument('--out', required=True)
    gs = sub.add_parser('grant-stop'); gs.add_argument('grant_id'); gs.add_argument('--revoke', action='store_true')
    ev = sub.add_parser('events', help='poll events after a cursor'); ev.add_argument('--after', type=int, default=0); ev.add_argument('--types')
    lg = sub.add_parser('lineage'); lg.add_argument('object_type'); lg.add_argument('object_id')
    sh = sub.add_parser('share'); sh.add_argument('job_id'); sh.add_argument('--grantee', required=True); sh.add_argument('--fields', required=True, help='comma-separated projectable fields')
    pj = sub.add_parser('projection'); pj.add_argument('job_id')
    # compute engine
    # ---- 24h expansion: models, knowledge, calibration, verification, nodes, approvals, statements, mcp ----
    sub.add_parser('models', help='registered local model revisions with readiness'); sub.add_parser('models-runtime', help='model runtime facts per host')
    mr = sub.add_parser('model-register', help='register a pinned model artifact (hub repo + 40-hex commit) already installed under the model store'); mr.add_argument('--model-id', required=True); mr.add_argument('--hub-repo', required=True); mr.add_argument('--revision', required=True); mr.add_argument('--operations', required=True, help='comma-separated: generate,embed'); mr.add_argument('--license', required=True)
    ma = sub.add_parser('model-action'); ma.add_argument('revision_id'); ma.add_argument('action', choices=('promote', 'retire', 'revoke', 'recheck', 'load', 'unload', 'rollback-default')); ma.add_argument('--operation'); ma.add_argument('--reason', default='')
    gen = sub.add_parser('generate', help='bounded local text generation (job); --watch streams persisted segments'); gen.add_argument('--prompt'); gen.add_argument('--messages', help='JSON file with [{role, content}]'); gen.add_argument('--max-output-tokens', type=int, default=64); gen.add_argument('--seed', type=int); gen.add_argument('--temperature-percent', type=int, default=0); gen.add_argument('--model-revision'); gen.add_argument('--watch', action='store_true'); gen.add_argument('--timeout', type=int, default=300)
    emb = sub.add_parser('embed', help='embedding job for a JSON list of texts'); emb.add_argument('--texts', required=True, help='JSON file with a list of strings'); emb.add_argument('--truncate', action='store_true')
    mj = sub.add_parser('model-job'); mj.add_argument('job_id'); mj.add_argument('--segments', action='store_true'); mj.add_argument('--after', type=int, default=-1)
    kc = sub.add_parser('knowledge-collection-create'); kc.add_argument('--name', required=True); kc.add_argument('--description', default='')
    sub.add_parser('knowledge-collections')
    kd = sub.add_parser('knowledge-add', help='add a text/markdown/csv document version to a collection'); kd.add_argument('collection_id'); kd.add_argument('--file', required=True); kd.add_argument('--name'); kd.add_argument('--format', default='markdown'); kd.add_argument('--document-id')
    ki = sub.add_parser('knowledge-index', help='build (or wait for) an embedding index version'); ki.add_argument('collection_id'); ki.add_argument('--wait', action='store_true')
    ks = sub.add_parser('knowledge-search'); ks.add_argument('collection_id'); ks.add_argument('--query', required=True); ks.add_argument('--mode', default='hybrid', choices=('lexical', 'semantic', 'hybrid')); ks.add_argument('--k', type=int, default=5)
    ka = sub.add_parser('knowledge-answer', help='retrieval-assisted answer job (extractive or generative)'); ka.add_argument('collection_id'); ka.add_argument('--question', required=True); ka.add_argument('--mode', default='extractive', choices=('extractive', 'generative')); ka.add_argument('--k', type=int, default=4); ka.add_argument('--max-output-tokens', type=int, default=120); ka.add_argument('--wait', action='store_true'); ka.add_argument('--timeout', type=int, default=300)
    kr = sub.add_parser('knowledge-revoke'); kr.add_argument('document_id'); kr.add_argument('--reason', default='')
    kv = sub.add_parser('knowledge-validate-citations'); kv.add_argument('--citations', required=True, help='JSON file [{chunk_id, quote}]')
    cd = sub.add_parser('calibration-dataset', help='create a numeric dataset from a JSON file {name, columns, target, units, rows} or a performance dataset with --task-kind'); cd.add_argument('--file'); cd.add_argument('--task-kind')
    cf = sub.add_parser('calibration-fit'); cf.add_argument('dataset_id'); cf.add_argument('--features', required=True); cf.add_argument('--target', required=True); cf.add_argument('--ridge', default='0'); cf.add_argument('--split', default='chronological'); cf.add_argument('--scope-kind'); cf.add_argument('--scope-backend'); cf.add_argument('--wait', action='store_true')
    sub.add_parser('calibration-models'); cpd = sub.add_parser('calibration-predict'); cpd.add_argument('model_id'); cpd.add_argument('--features', required=True, help='JSON object')
    cma = sub.add_parser('calibration-action'); cma.add_argument('model_id'); cma.add_argument('action', choices=('approve', 'retire', 'comparison'))
    cpn = sub.add_parser('calibration-plan'); cpn.add_argument('--kind', required=True); cpn.add_argument('--inputs', required=True)
    vp = sub.add_parser('verification-preview'); vp.add_argument('job_id'); vp.add_argument('--class', dest='cls', required=True); vp.add_argument('--sample-count', type=int)
    vr = sub.add_parser('verification-request'); vr.add_argument('job_id'); vr.add_argument('--class', dest='cls', required=True); vr.add_argument('--sample-count', type=int); vr.add_argument('--wait', action='store_true'); vr.add_argument('--timeout', type=int, default=600)
    vs = sub.add_parser('verification-status'); vs.add_argument('verification_id'); vst = sub.add_parser('verification-statement', help='public signed projection; --verify checks it against this service'); vst.add_argument('verification_id'); vst.add_argument('--verify', action='store_true'); vst.add_argument('--out')
    vl = sub.add_parser('verifications'); vl.add_argument('--job')
    vpc = sub.add_parser('verification-policy-create', help='reusable immutable audit requirement: class, params, max work'); vpc.add_argument('--name', required=True); vpc.add_argument('--class', dest='cls', required=True); vpc.add_argument('--sample-count', type=int); vpc.add_argument('--max-work', type=int)
    sub.add_parser('verification-policies')
    esc = sub.add_parser('eval-suite-create', help='immutable evaluation suite from a JSON file {name, threshold_percent, items:[...]}'); esc.add_argument('--file', required=True); esc.add_argument('--name'); esc.add_argument('--threshold', type=int)
    sub.add_parser('eval-suites'); er = sub.add_parser('eval-run', help='run a suite under a generation revision (default: the promoted one) through ordinary jobs'); er.add_argument('suite_id'); er.add_argument('--revision'); er.add_argument('--wait', action='store_true'); er.add_argument('--timeout', type=int, default=600)
    mbt = sub.add_parser('model-batching', help='operator settings for static generation batching (applied at the workers\' next claim)'); mbt.add_argument('--enabled', choices=('on', 'off')); mbt.add_argument('--max-sequences', dest='max_sequences', type=int); mbt.add_argument('--max-tokens', dest='max_tokens', type=int); mbt.add_argument('--wait-ms', dest='wait_ms', type=int); mbt.add_argument('--kv-budget-bytes', dest='kv_budget_bytes', type=int)
    mw = sub.add_parser('model-warmup', help='operator warmup policy: ordered revisions kept resident under a byte ceiling; --disable releases them'); mw.add_argument('--revisions'); mw.add_argument('--ceiling-bytes', type=int); mw.add_argument('--disable', action='store_true')
    cdz = sub.add_parser('calibration-design', help='rank candidate measurements (JSON file {candidates:[{features,cost,label}], objective, cost_policy, targets}) by predicted utility; nothing is executed'); cdz.add_argument('model_id'); cdz.add_argument('--file', required=True)
    ac = sub.add_parser('analysis-create', help='structured analysis session; optional JSON file {name, blocks}; --from-document / --from-workflow seed blocks'); ac.add_argument('--file'); ac.add_argument('--name'); ac.add_argument('--from-document'); ac.add_argument('--from-workflow')
    sub.add_parser('analyses')
    av = sub.add_parser('analysis'); av.add_argument('analysis_id'); av.add_argument('--version', type=int)
    ar = sub.add_parser('analysis-revise', help='new revision from a JSON file {blocks}; refused unless --expected-version is current'); ar.add_argument('analysis_id'); ar.add_argument('--file', required=True); ar.add_argument('--expected-version', type=int, required=True); ar.add_argument('--note')
    af = sub.add_parser('analysis-freeze'); af.add_argument('analysis_id'); af.add_argument('--version', type=int, required=True); af.add_argument('--reason')
    ai = sub.add_parser('analysis-impact', help='impact of a change: --changed \'{"block": "assumptions"}\' or \'{"object_type": "knowledge_document_version", "object_id": "..."}\''); ai.add_argument('analysis_id'); ai.add_argument('--changed', required=True)
    ag = sub.add_parser('analysis-regenerate', help='plan (or start) a selective regeneration of a workflow run: --changes \'{"node": {"field": value}}\''); ag.add_argument('analysis_id'); ag.add_argument('--run-id', required=True); ag.add_argument('--changes', required=True); ag.add_argument('--budget-ceiling', type=int); ag.add_argument('--plan-only', action='store_true')
    arp = sub.add_parser('analysis-report', help='build an evidence-linked report from a frozen revision'); arp.add_argument('analysis_id'); arp.add_argument('--version', type=int, required=True); arp.add_argument('--mode', choices=('deterministic', 'model'), default='deterministic')
    rp2 = sub.add_parser('report'); rp2.add_argument('report_id'); rp2.add_argument('--bundle', action='store_true', help='Markdown + HTML + manifest bundle with per-file digests')
    rpj = sub.add_parser('report-projection', help='preview (default) or --export a signed disclosure projection; --scope JSON {blocks, fields, include_quotes, include_assumption_values}'); rpj.add_argument('report_id'); rpj.add_argument('--scope'); rpj.add_argument('--export', action='store_true'); rpj.add_argument('--acknowledge-warnings', action='store_true')
    pvf = sub.add_parser('projection-verify'); pvf.add_argument('--file', required=True)
    nc = sub.add_parser('notebook-create', help='private experiment notebook from a JSON file {name, note, blocks:[{id,type:text|link,...}]}; nothing is executed'); nc.add_argument('--file', required=True); nc.add_argument('--name')
    sub.add_parser('notebooks'); nv_ = sub.add_parser('notebook'); nv_.add_argument('notebook_id'); nv_.add_argument('--version', type=int)
    nvv = sub.add_parser('notebook-version', help='append an immutable version'); nvv.add_argument('notebook_id'); nvv.add_argument('--file', required=True); nvv.add_argument('--note')
    ncp = sub.add_parser('notebook-compare'); ncp.add_argument('notebook_id'); ncp.add_argument('a', type=int); ncp.add_argument('b', type=int)
    nex = sub.add_parser('notebook-export', help='authorized export: text + object references with commitments, no payloads'); nex.add_argument('notebook_id'); nex.add_argument('--version', type=int); nex.add_argument('--out')
    ec = sub.add_parser('eval-compare'); ec.add_argument('run_a'); ec.add_argument('run_b'); eg = sub.add_parser('eval-gate', help='require a passing scored run of this suite before promotion (empty clears)'); eg.add_argument('--suite-id', default='')
    ne = sub.add_parser('node-enroll', help='generate a node keypair, enroll it, and write a private identity file'); ne.add_argument('--name', required=True); ne.add_argument('--out', required=True); ne.add_argument('--devices', default='cpu'); ne.add_argument('--capabilities')
    sub.add_parser('nodes'); nv = sub.add_parser('node'); nv.add_argument('node_id'); na = sub.add_parser('node-action'); na.add_argument('node_id'); na.add_argument('action', choices=('drain', 'enable', 'disable', 'revoke', 'rotate')); na.add_argument('--reason', default='')
    pc = sub.add_parser('package-create', help='versioned workflow package from a JSON body {name, workflow_id, description, required_models, delivery_policy, example, disclosure_defaults}'); pc.add_argument('--file', required=True)
    sub.add_parser('packages'); pv2 = sub.add_parser('package'); pv2.add_argument('package_id')
    pcm = sub.add_parser('package-compat', help='structured compatibility report (nothing reserved/started/downloaded)'); pcm.add_argument('--package-id'); pcm.add_argument('--file'); pcm.add_argument('--device-policy')
    pim = sub.add_parser('package-import', help='check (default) or --apply install a package manifest file'); pim.add_argument('--file', required=True); pim.add_argument('--apply', action='store_true')
    pex = sub.add_parser('package-export'); pex.add_argument('package_id'); pex.add_argument('--out'); pex.add_argument('--example', action='store_true', help='include the synthetic example')
    pin = sub.add_parser('package-instantiate', help='concrete workflow from a package: JSON file {values, inputs:{node: inputs}}'); pin.add_argument('package_id'); pin.add_argument('--file'); pin.add_argument('--name')
    pq = sub.add_parser('package-quote', help='composite quote over an instantiated definition'); pq.add_argument('package_id'); pq.add_argument('--workflow-id', required=True); pq.add_argument('--scheme', choices=('exact', 'upto'), default='exact')
    prn = sub.add_parser('package-run', help='start a run under a composite quote (--quote-id) or view a package run by id'); prn.add_argument('package_id'); prn.add_argument('--quote-id'); prn.add_argument('--budget-ceiling', type=int)
    sub.add_parser('package-runs'); prt = sub.add_parser('package-retry'); prt.add_argument('run_id')
    pb = sub.add_parser('package-bundle', help='signed result bundle of a delivered package run'); pb.add_argument('run_id'); pb.add_argument('--scope'); pb.add_argument('--out')
    pre = sub.add_parser('package-retire'); pre.add_argument('package_id')
    rc1 = sub.add_parser('reconcile', help='reconcile conflicting sources for one declared quantity under an explicit rule (JSON file {quantity, unit, sources, rule})'); rc1.add_argument('--file', required=True)
    rc2 = sub.add_parser('reconciliations'); rc2.add_argument('id', nargs='?')
    mq1 = sub.add_parser('measurement-request', help='local measurement-request artifact (JSON file {quantity, unit, acceptable_format, from:{plan_job_id|reconciliation_id|analysis_id}}); --out writes the export'); mq1.add_argument('--file', required=True); mq1.add_argument('--out')
    mq2 = sub.add_parser('measurement-requests'); mq2.add_argument('id', nargs='?')
    pup = sub.add_parser('package-upgrade-preview', help='what a newer package changes (operations, models, verification, bounds, costs) and which runs/analyses are affected'); pup.add_argument('package_id'); pup.add_argument('new_package_id')
    sub.add_parser('review-queue', help='analyses whose current evidence is stale, contradictory or insufficient for their declared policy')
    be = sub.add_parser('bundle-export', help='portable data-only bundle: services, pinned model identities, verification templates, approval/warmup policies'); be.add_argument('--name', required=True); be.add_argument('--services'); be.add_argument('--models'); be.add_argument('--policies'); be.add_argument('--out')
    bc = sub.add_parser('bundle-check', help='compatibility check of a bundle against this instance (nothing changes)'); bc.add_argument('--file', required=True)
    bi = sub.add_parser('bundle-import', help='import compatible items (no promotion, load, grant or execution); --apply performs it, otherwise a dry run'); bi.add_argument('--file', required=True); bi.add_argument('--apply', action='store_true')
    sub.add_parser('disagreements', help='failed/incomplete/disputed audits grouped per target with producer/auditor environment differences'); dd = sub.add_parser('disagreement-decide'); dd.add_argument('verification_id'); dd.add_argument('decision', choices=('producer_upheld', 'auditor_upheld', 'environment_difference', 'inconclusive')); dd.add_argument('--note', required=True); dd.add_argument('--evidence', required=True, help='JSON list of {kind: job|verification|artifact, id}')
    di = sub.add_parser('document-import', help='import a PDF (bytes uploaded privately; native text + bounded local OCR in a task-owned child)'); di.add_argument('--file', required=True); di.add_argument('--name'); di.add_argument('--collection'); di.add_argument('--mode', choices=('native', 'ocr_needed', 'ocr_forced')); di.add_argument('--wait', action='store_true'); di.add_argument('--timeout', type=int, default=900)
    sub.add_parser('documents'); dv = sub.add_parser('document'); dv.add_argument('import_id'); dc = sub.add_parser('document-compare', help='page / passage / table-cell comparison of two extraction revisions with the datasets and analyses affected by content changes'); dc.add_argument('import_a'); dc.add_argument('import_b'); dp = sub.add_parser('document-page'); dp.add_argument('import_id'); dp.add_argument('index', type=int)
    da = sub.add_parser('document-action'); da.add_argument('import_id'); da.add_argument('action', choices=('cancel', 'retry', 'publish', 'remove')); da.add_argument('--mode'); da.add_argument('--confirm', action='store_true'); da.add_argument('--include-excluded', action='store_true')
    tb = sub.add_parser('table'); tb.add_argument('table_id'); ta = sub.add_parser('table-annotate'); ta.add_argument('table_id'); ta.add_argument('--kind', required=True, choices=('header_row', 'ignore_row', 'unit', 'cell_correction', 'locale', 'note')); ta.add_argument('--payload', required=True, help='JSON object')
    mp = sub.add_parser('mapping-preview'); mp.add_argument('table_id'); mp.add_argument('--file', required=True, help='JSON mapping (metacoin-table-mapping/v1)'); mc2 = sub.add_parser('mapping-create'); mc2.add_argument('table_id'); mc2.add_argument('--file', required=True)
    mg = sub.add_parser('mapping'); mg.add_argument('mapping_id'); mcf = sub.add_parser('mapping-confirm', help='explicit confirmation creates an immutable dataset version with row-level provenance'); mcf.add_argument('mapping_id')
    it = sub.add_parser('intent', help='compile a request into a typed intent (plan | clarification | abstention); nothing executes'); it.add_argument('--text', required=True); it.add_argument('--inputs', help='JSON file'); it.add_argument('--kind'); it.add_argument('--collection'); it.add_argument('--verify')
    sub.add_parser('intents'); ic = sub.add_parser('intent-continue', help='answer named unresolved fields of a clarification'); ic.add_argument('intent_id'); ic.add_argument('--token', required=True); ic.add_argument('--answers', help='JSON object'); ic.add_argument('--inputs', help='JSON file')
    pl = sub.add_parser('plan', help='typed plan draft for a goal (validated, stored, not executed); --assist lets the local model pick the service kind'); pl.add_argument('--goal', required=True); pl.add_argument('--kind'); pl.add_argument('--inputs', help='JSON file'); pl.add_argument('--verify'); pl.add_argument('--assist', action='store_true'); pl.add_argument('--collection')
    sub.add_parser('plans'); pa = sub.add_parser('plan-accept', help='execute a valid plan once (idempotent)'); pa.add_argument('plan_id')
    apr = sub.add_parser('approval-propose'); apr.add_argument('--action', required=True); apr.add_argument('--content', required=True, help='JSON object'); apr.add_argument('--note', default='')
    apd = sub.add_parser('approval-decide'); apd.add_argument('approval_id'); apd.add_argument('decision', choices=('approve', 'reject', 'apply')); apd.add_argument('--note', default='')
    sub.add_parser('approvals'); app_ = sub.add_parser('approval-policy'); app_.add_argument('--required', help='comma-separated actions (empty string clears)')
    stm = sub.add_parser('statement', help='consolidated usage statement for an interval'); stm.add_argument('--since', type=int, default=0); stm.add_argument('--until', type=int); stm.add_argument('--csv', help='write CSV to this new file')
    sub.add_parser('mcp-connection', help='print how to connect an MCP client to this service (no secrets printed)')
    sub.add_parser('compute-capabilities', help='installed / configured / available / observed compute facts')
    cs = sub.add_parser('compute-submit', help='create, freeze and submit a compute job from a bounded JSON input file'); cs.add_argument('--kind', required=True, choices=('temporal_batch', 'monte_carlo_reliability', 'heat_diffusion', 'resource_plan'))
    cs.add_argument('--inputs', required=True); cs.add_argument('--title', default='compute job'); cs.add_argument('--reviewer', required=True); cs.add_argument('--idempotency-key')
    ci = sub.add_parser('compute-inspect'); ci.add_argument('job_id')
    cw = sub.add_parser('compute-watch', help='poll progress until the job is paused, cancelled or terminal'); cw.add_argument('job_id'); cw.add_argument('--timeout', type=int, default=600); cw.add_argument('--pause-after-checkpoint', action='store_true')
    for name in ('compute-pause', 'compute-resume', 'compute-cancel'):
        x = sub.add_parser(name); x.add_argument('job_id')
    cv = sub.add_parser('compute-verify', help='fetch the persisted verification record and reproducibility bundle'); cv.add_argument('job_id')
    ce = sub.add_parser('compute-export', help='download one output file (npy/json) or the heat plot'); ce.add_argument('job_id'); ce.add_argument('name'); ce.add_argument('--out', required=True)
    cl = sub.add_parser('compute-log'); cl.add_argument('job_id')
    rp = sub.add_parser('resource-plan', help='submit a robust resource-planning instance (JSON file) and optionally wait for the verified plan'); rp.add_argument('--inputs', required=True); rp.add_argument('--title', default='robust resource plan'); rp.add_argument('--wait', action='store_true'); rp.add_argument('--timeout', type=int, default=600)
    pv = sub.add_parser('plan-view', help='the committed plan of a resource-plan job (assignments, trajectory, alternatives, sensitivity) or its SVG'); pv.add_argument('job_id'); pv.add_argument('--svg', help='write the schedule/energy SVG to this new file'); pv.add_argument('--alternative', type=int)
    pf = sub.add_parser('plan-freeze', help='freeze a sweep alternative (cost ceiling) into a workflow draft'); pf.add_argument('job_id'); pf.add_argument('--cost-ceiling', type=int, required=True); pf.add_argument('--title')
    ac = sub.add_parser('action', help='create (or dry-run) the bounded next-step payment action of an accepted job'); ac.add_argument('job_id'); ac.add_argument('--request-id', required=True); ac.add_argument('--dry-run', action='store_true')
    sub.add_parser('budget-tree'); sub.add_parser('status-ops', help='operational status counters')
    se = sub.add_parser('search'); se.add_argument('--type'); se.add_argument('--status'); se.add_argument('--model'); se.add_argument('--limit', type=int)
    rl = sub.add_parser('reuse-lookup'); rl.add_argument('contract_id')
    ar = sub.add_parser('artifacts'); ar.add_argument('job_id')

    # ---- work economy (Order 08) ------------------------------------------------------------------------------------------
    w = sub.add_parser('work-terms-create', help='draft WorkTerms from a template (--template) or a JSON file (--file {terms:{...}})'); w.add_argument('--template'); w.add_argument('--ceiling', type=int, default=10); w.add_argument('--file'); w.add_argument('--idempotency-key')
    w = sub.add_parser('work-terms'); w.add_argument('--state')
    w = sub.add_parser('work-term'); w.add_argument('terms_id')
    w = sub.add_parser('work-terms-inspect', help='what counts as delivery, before reserving anything'); w.add_argument('terms_id')
    w = sub.add_parser('work-terms-freeze', help='bind the operation: --file {inputs:{...}, milestone_inputs?} or --contract-id'); w.add_argument('terms_id'); w.add_argument('--file'); w.add_argument('--contract-id'); w.add_argument('--idempotency-key')
    w = sub.add_parser('work-terms-amend', help='new revision from a JSON file of changed top-level fields'); w.add_argument('terms_id'); w.add_argument('--file', required=True); w.add_argument('--expected-version', type=int); w.add_argument('--idempotency-key')
    w = sub.add_parser('work-terms-evaluate'); w.add_argument('terms_id'); w.add_argument('--job-id')
    sub.add_parser('work-providers'); w = sub.add_parser('work-provider-register', help='operator registers a provider from a JSON file {name, capabilities, execution?, relationship?, pay_to?}'); w.add_argument('--file', required=True); w.add_argument('--idempotency-key')
    w = sub.add_parser('work-request-create'); w.add_argument('terms_id'); w.add_argument('--idempotency-key')
    w = sub.add_parser('work-requests'); w.add_argument('--state')
    w = sub.add_parser('work-request'); w.add_argument('request_id')
    w = sub.add_parser('work-request-action', help='open | pause | close | validate | eligibility'); w.add_argument('request_id'); w.add_argument('action'); w.add_argument('--file'); w.add_argument('--idempotency-key')
    w = sub.add_parser('work-offer', help='provider submits a binding offer'); w.add_argument('request_id'); w.add_argument('--price', type=int); w.add_argument('--asset', default='action-units'); w.add_argument('--scheme', default='exact'); w.add_argument('--window', type=int, default=3600); w.add_argument('--verification-class', default='full_exact'); w.add_argument('--file'); w.add_argument('--idempotency-key')
    w = sub.add_parser('work-compare', help='eligible offers ranked under the declared policy; excluded with reasons'); w.add_argument('request_id')
    w = sub.add_parser('work-award', help='award (recommended offer, or --offer-id with --reason for a manual choice); one guarded transaction'); w.add_argument('request_id'); w.add_argument('--offer-id'); w.add_argument('--reason'); w.add_argument('--expected-price', type=int); w.add_argument('--idempotency-key')
    w = sub.add_parser('work-awards'); w.add_argument('--state')
    w = sub.add_parser('work-award-view'); w.add_argument('award_id')
    w = sub.add_parser('work-ack'); w.add_argument('award_id'); w.add_argument('--idempotency-key')
    w = sub.add_parser('work-evaluate', help='acceptance candidate with predicate trace (read-only)'); w.add_argument('award_id'); w.add_argument('--milestone', default='m1')
    w = sub.add_parser('work-decide'); w.add_argument('award_id'); w.add_argument('decision', choices=('accept', 'reject')); w.add_argument('--milestone', default='m1'); w.add_argument('--reason'); w.add_argument('--idempotency-key')
    w = sub.add_parser('work-verify'); w.add_argument('award_id'); w.add_argument('--milestone', default='m1'); w.add_argument('--verification-class'); w.add_argument('--idempotency-key')
    w = sub.add_parser('work-receipts'); w.add_argument('award_id')
    w = sub.add_parser('work-receipt-verify'); w.add_argument('receipt_id'); w.add_argument('--trust-root')
    w = sub.add_parser('work-program-create', help='define a bounded recurring class of work (JSON file: name, class_terms, per_run_ceiling, aggregate_ceiling, max_runs)'); w.add_argument('--file', required=True); w.add_argument('--idempotency-key')
    w = sub.add_parser('work-programs')
    w = sub.add_parser('work-program'); w.add_argument('program_id')
    w = sub.add_parser('work-program-run', help='start one run with fresh inputs (JSON file: inputs)'); w.add_argument('program_id'); w.add_argument('--file', required=True); w.add_argument('--ceiling', type=int); w.add_argument('--idempotency-key')
    w = sub.add_parser('work-program-close'); w.add_argument('program_id')
    w = sub.add_parser('work-pricing-experiment', help='compare fixed and metered policies on a synthetic workload (JSON file)'); w.add_argument('--file', required=True)
    w = sub.add_parser('work-pricing-experiments'); w.add_argument('experiment_id', nargs='?')
    w = sub.add_parser('work-challenge', help='propose a bounded counterexample against a receipt (JSON file: counterexample_inputs)'); w.add_argument('receipt_id'); w.add_argument('--claim', required=True); w.add_argument('--asserted', required=True); w.add_argument('--file', required=True); w.add_argument('--idempotency-key')
    w = sub.add_parser('work-challenges'); w.add_argument('challenge_id', nargs='?')
    w = sub.add_parser('work-challenge-package'); w.add_argument('challenge_id'); w.add_argument('--out')
    w = sub.add_parser('work-mission-scenario', help='portfolio budget scenario under declared utilities (JSON file: name, budget_ceilings, utilities, costs)'); w.add_argument('portfolio_id'); w.add_argument('--file', required=True)
    w = sub.add_parser('work-mission-scenarios'); w.add_argument('portfolio_id'); w.add_argument('scenario_id', nargs='?')
    # ---- missions ----
    n = sub.add_parser('native-signing-requests', help='pending signing requests of my account (external custody)'); n.add_argument('--account')
    n = sub.add_parser('native-sign-pending', help='actor-side signing agent: fetch pending requests, have MY signer check and sign them, submit'); n.add_argument('--actor', required=True); n.add_argument('--signer', required=True); n.add_argument('--signer-token', required=True); n.add_argument('--account')
    sub.add_parser('native-operator', help='operator screen: owns/owes, committed resources, missing evidence, blackout possibilities, pending cross-domain')
    n = sub.add_parser('mission-plan'); n.add_argument('mission_id'); n.add_argument('--objective', default='earliest_delivery', choices=('earliest_delivery', 'lowest_cost')); n.add_argument('--planner', default='improved', choices=('baseline', 'improved', 'oracle')); n.add_argument('--apply', action='store_true'); n.add_argument('--idempotency-key')
    n = sub.add_parser('mission-bundle', help='portable, sealed mission bundle for offline verification'); n.add_argument('mission_id'); n.add_argument('--out')
    sub.add_parser('missions'); sub.add_parser('mission-templates')
    n = sub.add_parser('mission-create', help='create (and optionally freeze) a mission from a template or a JSON graph file'); n.add_argument('--template'); n.add_argument('--policy', help='JSON policy (price, verifier_pay_to, min_cash_reserve, ...)'); n.add_argument('--file'); n.add_argument('--freeze', action='store_true'); n.add_argument('--idempotency-key')
    n = sub.add_parser('mission'); n.add_argument('mission_id'); n = sub.add_parser('mission-ledger'); n.add_argument('mission_id'); n = sub.add_parser('mission-explain'); n.add_argument('mission_id')
    n = sub.add_parser('mission-action'); n.add_argument('mission_id'); n.add_argument('action', choices=('freeze', 'amend', 'pause', 'resume', 'step', 'run', 'propose', 'gate')); n.add_argument('--file'); n.add_argument('--idempotency-key')
    n = sub.add_parser('mission-authorize'); n.add_argument('mission_id'); n.add_argument('revision', type=int); n.add_argument('--idempotency-key')
    # ---- resource market ----
    sub.add_parser('resources'); n = sub.add_parser('resource-offer-publish', help='publish a fixed-price resource offer (JSON file: kind, resource_id, capacity, price_per_unit, window_start, window_end, ...)'); n.add_argument('--file', required=True); n.add_argument('--idempotency-key')
    n = sub.add_parser('resource-offers'); n.add_argument('--kind'); n = sub.add_parser('resource-quote'); n.add_argument('offer_id'); n.add_argument('quantity', type=int)
    n = sub.add_parser('resource-reserve', help='binding: checks the exact offer version, funds and capacity in one transaction'); n.add_argument('offer_id'); n.add_argument('offer_version', type=int); n.add_argument('quantity', type=int); n.add_argument('--request-id', required=True); n.add_argument('--idempotency-key')
    n = sub.add_parser('resource-reservations'); n.add_argument('--account'); n = sub.add_parser('resource-reservation'); n.add_argument('reservation_id')
    n = sub.add_parser('resource-action'); n.add_argument('reservation_id'); n.add_argument('action', choices=('activate', 'deliver', 'settle', 'cancel')); n.add_argument('--file'); n.add_argument('--idempotency-key')
    n = sub.add_parser('resource-storage-put'); n.add_argument('reservation_id'); n.add_argument('path'); n.add_argument('--idempotency-key')
    # ---- external signer ----
    def sg(p):
        p.add_argument('--signer', required=True, help='signer base URL (loopback or an authorized host)'); p.add_argument('--signer-token-file', required=True); return p
    sg(sub.add_parser('signer-health'))
    n = sg(sub.add_parser('signer-enroll', help='create the actor key in the signer and register the public identity with the service; credential to --out (0600)')); n.add_argument('name'); n.add_argument('--ceiling', type=int, required=True); n.add_argument('--out'); n.add_argument('--idempotency-key')
    n = sg(sub.add_parser('signer-rotate')); n.add_argument('actor'); n.add_argument('--account', required=True); n.add_argument('--grace-seconds', type=int, default=3600); n.add_argument('--idempotency-key')
    n = sg(sub.add_parser('signer-revoke')); n.add_argument('actor'); n.add_argument('--account', required=True); n.add_argument('--key-id', required=True); n.add_argument('--idempotency-key')
    n = sg(sub.add_parser('signer-delegate', help='bounded delegate key: recipient scope and aggregate ceiling never widen the actor\'s authority')); n.add_argument('actor'); n.add_argument('--account', required=True); n.add_argument('--name', required=True); n.add_argument('--recipients', required=True); n.add_argument('--ceiling', type=int, required=True); n.add_argument('--idempotency-key')
    n = sg(sub.add_parser('signer-keys')); n.add_argument('actor')
    n = sub.add_parser('native-keys'); n.add_argument('account'); n = sub.add_parser('native-key-revoke'); n.add_argument('account'); n.add_argument('key_id'); n.add_argument('--idempotency-key')
    n = sg(sub.add_parser('native-transfer-signed', help='prepare -> sign outside the service -> submit')); n.add_argument('actor'); n.add_argument('to_account'); n.add_argument('amount', type=int); n.add_argument('--request-id', required=True); n.add_argument('--asset'); n.add_argument('--from-account'); n.add_argument('--obligation-id'); n.add_argument('--delegate'); n.add_argument('--idempotency-key')
    # ---- native protocol ----
    sub.add_parser('native-domain'); n = sub.add_parser('native-domain-init'); n.add_argument('domain_id'); n.add_argument('--operator', default='same operator (local development)'); n.add_argument('--host', default='this host'); n.add_argument('--idempotency-key')
    sub.add_parser('native-accounts'); n = sub.add_parser('native-account'); n.add_argument('account_id', nargs='?', default='me')
    n = sub.add_parser('native-actor-create', help='create an earn-and-pay actor identity; the credential goes to --out (0600), never to stdout'); n.add_argument('name'); n.add_argument('--out'); n.add_argument('--idempotency-key')
    n = sub.add_parser('native-fund', help='development fixture funding (native:admin; dev only; zero-value asset)'); n.add_argument('account_id'); n.add_argument('amount', type=int); n.add_argument('--idempotency-key')
    for cmd in ('native-transfer', 'native-simulate'):
        n = sub.add_parser(cmd); n.add_argument('to_account'); n.add_argument('amount', type=int); n.add_argument('--request-id', required=True); n.add_argument('--asset'); n.add_argument('--from-account'); n.add_argument('--obligation-id'); n.add_argument('--idempotency-key')
    n = sub.add_parser('native-intents'); n.add_argument('--account'); n = sub.add_parser('native-intent'); n.add_argument('intent_id'); n = sub.add_parser('native-obligations'); n.add_argument('--account')
    n = sub.add_parser('native-export', help='export a destination-bound allocation to a pinned peer'); n.add_argument('destination'); n.add_argument('recipient'); n.add_argument('amount', type=int); n.add_argument('--request-id', required=True); n.add_argument('--asset'); n.add_argument('--idempotency-key')
    sub.add_parser('native-allocations'); n = sub.add_parser('native-allocation'); n.add_argument('allocation_id'); n = sub.add_parser('native-allocation-return'); n.add_argument('allocation_id'); n.add_argument('--idempotency-key'); n = sub.add_parser('native-recover'); n.add_argument('--idempotency-key')
    n = sub.add_parser('native-peer-pin', help='pin a peer authority root (JSON file: domain_id, authority_key_id, authority_public_hex, epoch)'); n.add_argument('--file', required=True); n.add_argument('--idempotency-key'); n = sub.add_parser('native-peer'); n.add_argument('domain_id')
    n = sub.add_parser('native-outbox'); n.add_argument('--receiver', required=True); n.add_argument('--transport', default='online'); n = sub.add_parser('native-inbox'); n.add_argument('--file', required=True); n.add_argument('--transport', default='online')
    n = sub.add_parser('native-bundle-write', help='file-bundle adapter: write this domain\'s outbox for a receiver'); n.add_argument('--receiver', required=True); n.add_argument('path')
    n = sub.add_parser('native-bundle-import', help='file-bundle adapter: import a bundle file at this domain; replies (acks) to --replies'); n.add_argument('path'); n.add_argument('--replies')
    n = sub.add_parser('native-courier', help='online adapter: one round trip from this domain to a peer API'); n.add_argument('--receiver', required=True); n.add_argument('--dst-base', required=True); n.add_argument('--dst-credential-file', required=True)
    n = sub.add_parser('native-observation'); n.add_argument('--peer', required=True); n.add_argument('--idempotency-key'); n = sub.add_parser('native-rotate'); n.add_argument('--grace-seconds', type=int, default=604800); n.add_argument('--idempotency-key')
    n = sub.add_parser('native-connectivity'); n.add_argument('state', choices=('online', 'cut')); n.add_argument('--idempotency-key'); sub.add_parser('native-recovery'); n = sub.add_parser('native-reconcile'); n.add_argument('--file'); n.add_argument('--idempotency-key')
    n = sub.add_parser('native-audit'); n.add_argument('--asset'); n = sub.add_parser('native-export-record'); n.add_argument('--out')
    n = sub.add_parser('native-check', help='OFFLINE independent conformance check of an exported record under explicitly pinned roots'); n.add_argument('export'); n.add_argument('--roots', required=True); n.add_argument('--environment', default='dev')
    sub.add_parser('native-vectors', help='OFFLINE deterministic valid/invalid vectors through the checker'); n = sub.add_parser('native-model', help='OFFLINE bounded state-space exploration'); n.add_argument('--depth', type=int, default=5)
    sub.add_parser('native-spec'); sub.add_parser('native-disputes'); n = sub.add_parser('native-dispute-open'); n.add_argument('obligation_id'); n.add_argument('--kind', required=True); n.add_argument('--claim', required=True); n.add_argument('--idempotency-key')
    n = sub.add_parser('native-dispute-resolve'); n.add_argument('dispute_id'); n.add_argument('--outcome', required=True, choices=('uphold', 'dismiss')); n.add_argument('--reason'); n.add_argument('--idempotency-key')
    n = sub.add_parser('procurement-plan', help='resource/contact-aware plan selection (JSON file: envelope, plans)'); n.add_argument('--file', required=True); n.add_argument('--policy', default='cheapest_eligible')
    n = sub.add_parser('procurement-benchmark'); n.add_argument('--missions', type=int, default=200); n.add_argument('--seed', type=int, default=1)
    sub.add_parser('lunar-sources'); sub.add_parser('lunar-products'); n = sub.add_parser('lunar-product'); n.add_argument('job_id'); n = sub.add_parser('lunar-download'); n.add_argument('job_id'); n.add_argument('--out', required=True)
    n = sub.add_parser('lunar-example'); n.add_argument('name', choices=('terrain', 'route', 'pack')); n.add_argument('--source', default='apollo11_32')
    d = sub.add_parser('decision-analyze', help='analyse a declared finite candidate model (JSON file: hypotheses, views, coordinates, optional observations, disclosure)'); d.add_argument('--file', required=True); d.add_argument('--title'); d.add_argument('--idempotency-key')
    d = sub.add_parser('decisions'); d.add_argument('--kind')
    d = sub.add_parser('decision'); d.add_argument('job_id')
    d = sub.add_parser('decision-projection', help='the disclosure-policed projection (counts, conclusion, digests)'); d.add_argument('job_id')
    d = sub.add_parser('decision-witnesses', help='private ambiguity witnesses (owner / designated reviewer)'); d.add_argument('job_id')
    d = sub.add_parser('decision-bundle', help='authorized decision bundle for offline replay (python -m metacoin_service.decision.replay)'); d.add_argument('job_id'); d.add_argument('--out', required=True)
    d = sub.add_parser('acquisition-preview', help='the next useful measurement set under a budget (nothing stored)'); d.add_argument('job_id'); d.add_argument('--budget', type=int, required=True); d.add_argument('--file', required=True, help='JSON {menu: [...]}')
    d = sub.add_parser('acquisition-plan', help='store a bound acquisition plan'); d.add_argument('job_id'); d.add_argument('--budget', type=int, required=True); d.add_argument('--file', required=True); d.add_argument('--idempotency-key')
    d = sub.add_parser('acquisition-plans')
    d = sub.add_parser('acquisition-materialize', help='freeze decision-evidence-acquisition terms from a plan and open the request (nothing reserved until award)'); d.add_argument('plan_id'); d.add_argument('--diagnostic', type=int, default=0); d.add_argument('--title'); d.add_argument('--idempotency-key')
    d = sub.add_parser('work-deliver', help='provider: deliver the observation (JSON file: delivery)'); d.add_argument('award_id'); d.add_argument('--milestone', default='m1'); d.add_argument('--file', required=True); d.add_argument('--idempotency-key')
    d = sub.add_parser('work-delivery-spec'); d.add_argument('award_id'); d.add_argument('--milestone', default='m1')
    d = sub.add_parser('work-assurance', help='required assurance, available evidence, satisfied / missing clauses, next checks'); d.add_argument('award_id'); d.add_argument('--milestone', default='m1')
    d = sub.add_parser('sampling-plan', help='exact finite-population sampling plan for an itemwise job'); d.add_argument('job_id'); d.add_argument('--alpha', required=True, help='rational string, e.g. 1/100'); d.add_argument('--max-samples', type=int, required=True); d.add_argument('--minimum-defects', type=int); d.add_argument('--defect-fraction', help='rational string; ceiling rule'); d.add_argument('--preview', action='store_true')
    d = sub.add_parser('sampling-plans'); d.add_argument('plan_id', nargs='?'); d.add_argument('--job-id')
    d = sub.add_parser('sampling-audit', help='issue the bound challenge (single fixed-size attempt)'); d.add_argument('plan_id'); d.add_argument('--idempotency-key')
    d = sub.add_parser('sampling-verify', help='recompute the sampling arithmetic of a signed statement bundle (JSON file)'); d.add_argument('--file', required=True)
    d = sub.add_parser('assurance-evaluate', help='evaluate a typed assurance requirement (JSON file) against a job\'s evidence'); d.add_argument('job_id'); d.add_argument('--file', required=True)
    d = sub.add_parser('assurance-claims')
    d = sub.add_parser('verification-assurance'); d.add_argument('verification_id')
    d = sub.add_parser('portfolio-create', help='versioned verification portfolio (JSON file: name, components)'); d.add_argument('--file', required=True); d.add_argument('--idempotency-key')
    d = sub.add_parser('portfolios')
    d = sub.add_parser('portfolio'); d.add_argument('portfolio_id')
    d = sub.add_parser('portfolio-select'); d.add_argument('suite_id'); d.add_argument('--families', required=True); d.add_argument('--max-cost', type=int)
    d = sub.add_parser('fault-suite', help='preregistered fault campaign suite (JSON file: name, seed, cases_per_family, holdout_per_family, families?)'); d.add_argument('--file', required=True); d.add_argument('--idempotency-key')
    d = sub.add_parser('campaign'); d.add_argument('suite_id'); d.add_argument('--portfolio-ids', required=True); d.add_argument('--split', default='dev', choices=('dev', 'held-out')); d.add_argument('--idempotency-key')
    d = sub.add_parser('campaigns'); d.add_argument('campaign_id', nargs='?')
    w = sub.add_parser('work-reconciliation', help='pending payment observations with the one bounded action that applies to each')
    w = sub.add_parser('work-provider-history', help='evidence-based provider history (dimensions with sample sizes; no score)'); w.add_argument('provider_id'); w.add_argument('--disclosed', action='store_true')
    w = sub.add_parser('work-provider-portfolio'); w.add_argument('provider_id')
    w = sub.add_parser('work-provider-portfolio-set', help='provider: select receipts of your own contracts to disclose'); w.add_argument('provider_id'); w.add_argument('--receipt-ids', required=True); w.add_argument('--note')
    w = sub.add_parser('work-package-preview', help='validate a contract/evidence bundle from another instance without awarding, paying or trusting anything'); w.add_argument('package'); w.add_argument('--trust-roots')
    w = sub.add_parser('work-dispute-open'); w.add_argument('award_id'); w.add_argument('--milestone', default='m1'); w.add_argument('--claim', required=True); w.add_argument('--scope', default='acceptance'); w.add_argument('--idempotency-key')
    w = sub.add_parser('work-dispute', help='view, or act: --action respond|evidence|recheck|decide|appeal|close with --file body'); w.add_argument('dispute_id'); w.add_argument('--action'); w.add_argument('--file'); w.add_argument('--idempotency-key')
    sub.add_parser('work-disputes')
    w = sub.add_parser('work-bundle', help='portable work bundle (zip) for the offline verifier'); w.add_argument('award_id'); w.add_argument('--milestone', default='m1'); w.add_argument('--scope', default='restricted'); w.add_argument('--out', required=True)
    w = sub.add_parser('work-audit-grant'); w.add_argument('award_id'); w.add_argument('--grantee', required=True); w.add_argument('--purpose', required=True); w.add_argument('--categories', default='terms,receipts'); w.add_argument('--expires', type=int, default=3600); w.add_argument('--idempotency-key')
    w = sub.add_parser('work-audit-use'); w.add_argument('grant_id')
    w = sub.add_parser('work-audit-revoke'); w.add_argument('grant_id'); w.add_argument('--idempotency-key')
    w = sub.add_parser('work-pay', help='prepare -> authorize -> submit for one entitlement (stable identity; safe to retry with the same key)'); w.add_argument('entitlement_id'); w.add_argument('--authorize-only', action='store_true'); w.add_argument('--idempotency-key')
    w = sub.add_parser('work-intents'); w.add_argument('--state')
    w = sub.add_parser('work-intent'); w.add_argument('intent_id')
    w = sub.add_parser('work-reconcile'); w.add_argument('intent_id'); w.add_argument('--idempotency-key')
    w = sub.add_parser('work-refund'); w.add_argument('entitlement_id'); w.add_argument('--amount', type=int, required=True); w.add_argument('--request-key', required=True); w.add_argument('--idempotency-key')
    sub.add_parser('work-journal'); sub.add_parser('work-journal-replay'); w = sub.add_parser('work-treasury'); w.add_argument('--asset', default='local-chain-token')
    w = sub.add_parser('work-treasury-allocate'); w.add_argument('terms_id'); w.add_argument('--idempotency-key')
    sub.add_parser('work-exposure'); sub.add_parser('work-status'); sub.add_parser('work-notifications'); sub.add_parser('work-measurements'); sub.add_parser('work-rails')
    w = sub.add_parser('work-mission-import'); w.add_argument('--idempotency-key'); sub.add_parser('work-missions'); w = sub.add_parser('work-mission'); w.add_argument('portfolio_id')
    w = sub.add_parser('work-mission-draft', help='deterministic draft from a named bottleneck'); w.add_argument('portfolio_id'); w.add_argument('task'); w.add_argument('--ceiling', type=int, default=3); w.add_argument('--file'); w.add_argument('--idempotency-key')
    w = sub.add_parser('work-observation', help='ingest a signed simulated observation package (JSON file)'); w.add_argument('--file', required=True); w.add_argument('--idempotency-key')
    w = sub.add_parser('work-close'); w.add_argument('award_id'); w.add_argument('--idempotency-key')
    args = parser.parse_args(argv)
    token = load_token(args.credential_file)
    go = lambda *a, **k: call(args.base, token, *a, **k)
    if args.command == 'create':
        with open(args.inputs) as stream:
            inputs = json.load(stream)
        policy = dict(json.loads(args.policy) if args.policy else {}, reviewer_id=args.reviewer)
        status, out = go('POST', '/api/v1/contracts', {'kind': args.kind, 'title': args.title, 'inputs': inputs, 'policy': policy})
    elif args.command == 'freeze':
        status, out = go('POST', '/api/v1/contracts/' + args.contract_id + '/freeze', {})
    elif args.command == 'submit':
        status, out = go('POST', '/api/v1/jobs', {'contract_id': args.contract_id, 'reuse': bool(args.reuse)}, idempotency_key=args.idempotency_key)
    elif args.command == 'poll':
        deadline = time.time() + args.timeout
        while True:
            status, out = go('GET', '/api/v1/jobs/' + args.job_id)
            if status != 200 or out['state'] in ('succeeded', 'failed', 'cancelled') or time.time() > deadline:
                break
            time.sleep(0.5)
    elif args.command == 'status':
        status, out = go('GET', '/api/v1/jobs/' + args.job_id)
    elif args.command == 'result':
        status, out = go('GET', '/api/v1/jobs/' + args.job_id + '/result')
    elif args.command == 'review-request':
        status, out = go('POST', '/api/v1/jobs/' + args.job_id + '/review-request', {})
    elif args.command == 'decide':
        status, out = go('POST', '/api/v1/reviews/' + args.job_id + '/decision', {'decision': args.decision})
    elif args.command == 'export':
        status, content = go('GET', '/api/v1/artifacts/' + args.artifact_id + '/export', raw=True)
        if status == 200:
            fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(content)
            out = {'written': args.out, 'bytes': len(content)}
        else:
            out = json.loads(content)
    elif args.command == 'compare':
        status, out = go('GET', '/api/v1/jobs/' + args.job_a + '/compare/' + args.job_b)
    elif args.command == 'me':
        status, out = go('GET', '/api/v1/me')
    elif args.command == 'budget':
        status, out = go('GET', '/api/v1/budget')
    elif args.command == 'dataset-create':
        with open(args.file) as stream:
            content = stream.read()
        status, out = go('POST', '/api/v1/datasets', {'name': args.name, 'kind': args.kind, 'format': args.format, 'content': content, 'provenance': args.provenance})
    elif args.command == 'datasets':
        status, out = go('GET', '/api/v1/datasets')
    elif args.command == 'workflow-create':
        with open(args.file) as stream:
            status, out = go('POST', '/api/v1/workflows', {'definition': json.load(stream)})
    elif args.command == 'workflow-run':
        body = {'bindings': json.loads(args.bindings) if args.bindings else {}, 'preview': args.preview}
        if args.budget_ceiling is not None:
            body['budget_ceiling'] = args.budget_ceiling
        status, out = go('POST', '/api/v1/workflows/' + args.definition_id + '/runs', body)
    elif args.command == 'workflow-instantiate':
        body = {'values': json.loads(args.values)}
        if args.name:
            body['name'] = args.name
        status, out = go('POST', '/api/v1/workflows/' + args.definition_id + '/instantiate', body)
    elif args.command == 'schedule-create':
        body = {'definition_id': args.definition_id, 'timezone': args.timezone, 'times': args.times.split(','), 'overlap': args.overlap, 'max_runs': args.max_runs}
        if args.bindings:
            body['bindings'] = json.loads(args.bindings)
        if args.budget_ceiling is not None:
            body['budget_ceiling'] = args.budget_ceiling
        status, out = go('POST', '/api/v1/schedules', body)
    elif args.command == 'schedules':
        status, out = go('GET', '/api/v1/schedules')
    elif args.command == 'schedule-control':
        status, out = go('POST', '/api/v1/schedules/' + args.schedule_id + '/' + args.action, {})
    elif args.command == 'run-status':
        deadline = time.time() + args.timeout
        while True:
            status, out = go('GET', '/api/v1/runs/' + args.run_id)
            if not args.follow or status != 200 or out['state'] in ('completed', 'blocked', 'partially_failed', 'failed', 'cancelled', 'waiting_review') or time.time() > deadline:
                break
            time.sleep(1)
    elif args.command == 'run-cancel':
        status, out = go('POST', '/api/v1/runs/' + args.run_id + '/cancel', {})
    elif args.command == 'campaign-create':
        with open(args.file) as stream:
            definition = json.load(stream)
        status, out = go('POST', '/api/v1/campaigns', {'definition': definition, 'preview': args.preview})
    elif args.command == 'campaign-status':
        if args.csv:
            status, content = go('GET', '/api/v1/campaigns/' + args.campaign_id + '/results.csv', raw=True)
            if status == 200:
                fd = os.open(args.csv, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(content)
                out = {'written': args.csv, 'bytes': len(content)}
            else:
                out = json.loads(content)
        else:
            status, out = go('GET', '/api/v1/campaigns/' + args.campaign_id + ('/results' if args.results else ''))
    elif args.command == 'campaign-branch':
        body = {'base_changes': json.loads(args.base_changes) if args.base_changes else {}}
        if args.candidates:
            body['candidate_indexes'] = [int(x) for x in args.candidates.split(',')]
        if args.name:
            body['name'] = args.name
        if args.changes:
            body['changes'] = json.loads(args.changes)
        if args.expected_head:
            body['expected_head'] = args.expected_head
        status, out = go('POST', '/api/v1/campaigns/' + args.campaign_id + '/branch', body)
    elif args.command == 'campaign-plan':
        status, out = go('POST', '/api/v1/campaigns/' + args.campaign_id + '/plan', {'cost_cap_units': args.cost_cap})
    elif args.command == 'campaign-compare':
        status, out = go('GET', '/api/v1/campaigns/' + args.campaign_a + '/compare/' + args.campaign_b)
    elif args.command == 'campaign-control':
        status, out = go('POST', '/api/v1/campaigns/' + args.campaign_id + '/' + args.action, {})
    elif args.command == 'services':
        status, out = go('GET', '/api/v1/services')
    elif args.command == 'quote':
        with open(args.inputs) as stream:
            inputs = json.load(stream)
        status, out = go('POST', '/api/v1/services/' + args.service_id + '/quote', {'inputs': inputs})
        if status == 201 and args.accept:
            status, out = go('POST', '/api/v1/quotes/' + out['quote_id'] + '/accept', {})
    elif args.command == 'invoke':
        with open(args.inputs) as stream:
            inputs = json.load(stream)
        status, out = go('POST', '/api/v1/services/' + args.service_id + '/invoke', {'quote_id': args.quote, 'inputs': inputs}, idempotency_key='cli-invoke-' + args.quote)
    elif args.command == 'usage':
        status, out = go('GET', '/api/v1/usage')
    elif args.command == 'queue':
        status, out = go('GET', '/api/v1/queue')
    elif args.command == 'workers':
        status, out = go('GET', '/api/v1/workers')
    elif args.command == 'grants':
        status, out = go('GET', '/api/v1/agents/grants')
    elif args.command == 'grant-issue':
        with open(args.policy) as stream:
            policy = json.load(stream)
        status, out = go('POST', '/api/v1/agents/grants', {'policy': policy})
        if status == 201:
            fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as stream:
                json.dump({'token': out.pop('token')}, stream)
            out['credential_written_to'] = args.out
    elif args.command == 'grant-stop':
        status, out = go('POST', '/api/v1/agents/grants/' + args.grant_id + ('/revoke' if args.revoke else '/stop'), {})
    elif args.command == 'events':
        status, out = go('GET', '/api/v1/events?after=%d%s' % (args.after, '&types=' + args.types if args.types else ''))
    elif args.command == 'lineage':
        status, out = go('GET', '/api/v1/lineage/' + args.object_type + '/' + args.object_id)
    elif args.command == 'action':
        status, out = go('POST', '/api/v1/actions', {'job_id': args.job_id, 'request_id': args.request_id, 'dry_run': args.dry_run}, idempotency_key='cli-action-' + args.request_id)
    elif args.command == 'budget-tree':
        status, out = go('GET', '/api/v1/budgets/tree')
    elif args.command == 'status-ops':
        status, out = go('GET', '/api/v1/status')
    elif args.command == 'search':
        qs = '&'.join(k + '=' + str(v) for k, v in (('type', args.type), ('status', args.status), ('model', args.model), ('limit', args.limit)) if v)
        status, out = go('GET', '/api/v1/search' + ('?' + qs if qs else ''))
    elif args.command == 'reuse-lookup':
        status, out = go('GET', '/api/v1/reuse/lookup?contract_id=' + args.contract_id)
    elif args.command == 'artifacts':
        status, out = go('GET', '/api/v1/jobs/' + args.job_id + '/artifacts')
    elif args.command == 'compute-capabilities':
        status, out = go('GET', '/api/v1/compute/capabilities')
    elif args.command == 'compute-submit':
        with open(args.inputs) as stream:
            inputs = json.load(stream)
        if inputs.get('schema', '').split('-input/')[0].replace('-', '_') not in (args.kind, args.kind.replace('_reliability', ''), 'temporal_batch', 'monte_carlo_reliability', 'heat_diffusion'):
            pass                                                     # the server validates; the local check only catches an obviously wrong file
        status, out = go('POST', '/api/v1/contracts', {'kind': args.kind, 'title': args.title, 'inputs': inputs, 'policy': {'reviewer_id': args.reviewer}})
        if status == 201:
            cid = out['id']
            status, out = go('POST', '/api/v1/contracts/' + cid + '/freeze', {})
            if status == 200:
                status, out = go('POST', '/api/v1/jobs', {'contract_id': cid}, idempotency_key=args.idempotency_key or ('cli-compute-' + cid))
                if status == 202:
                    out = {'job_id': out['id'], 'contract_id': cid, 'state': out['state'], 'compute': out.get('compute')}
    elif args.command == 'compute-inspect':
        status, out = go('GET', '/api/v1/compute/jobs/' + args.job_id)
    elif args.command == 'compute-watch':
        deadline = time.time() + args.timeout; last = None; paused_sent = False
        while True:
            status, out = go('GET', '/api/v1/compute/jobs/' + args.job_id)
            if status != 200:
                break
            line = (out['phase'], out['work']['committed'], out['work']['computed'], out['checkpoint_generation'])
            if line != last:
                print(json.dumps({'phase': out['phase'], 'committed': out['work']['committed'], 'computed': out['work']['computed'], 'total': out['work']['total'], 'checkpoint_generation': out['checkpoint_generation'], 'backend': out['backend']}), file=sys.stderr)
                last = line
            if args.pause_after_checkpoint and not paused_sent and out['checkpoint_generation'] >= 1 and 'pause' in out['allowed_actions']:
                go('POST', '/api/v1/compute/jobs/' + args.job_id + '/pause', {}); paused_sent = True
            if out['state'] in ('succeeded', 'failed', 'cancelled') or out['phase'] == 'paused' or time.time() > deadline:
                break
            time.sleep(1)
    elif args.command in ('compute-pause', 'compute-resume', 'compute-cancel'):
        status, out = go('POST', '/api/v1/compute/jobs/' + args.job_id + '/' + args.command.split('-')[1], {}, idempotency_key='cli-' + args.command + '-' + args.job_id + '-' + str(int(time.time())))
    elif args.command == 'compute-verify':
        status, view = go('GET', '/api/v1/compute/jobs/' + args.job_id)
        status2, repro = go('GET', '/api/v1/compute/jobs/' + args.job_id + '/reproducibility')
        out = {'verification': view.get('verification') if status == 200 else view, 'reproducibility': repro if status2 == 200 else repro}
        status = max(status, status2)
    elif args.command == 'resource-plan':
        with open(args.inputs) as stream:
            inputs = json.load(stream)
        status, out = go('POST', '/api/v1/compute/resource-plans', {'inputs': inputs, 'title': args.title})
        if status == 202 and args.wait:
            deadline = time.time() + args.timeout
            while time.time() < deadline:
                st, job = go('GET', '/api/v1/jobs/' + out['job_id'])
                if st == 200 and job['state'] in ('succeeded', 'failed', 'cancelled'):
                    out = {'submitted': out, 'job': job}; break
                time.sleep(1)
    elif args.command == 'plan-view':
        if args.svg:
            status, content = go('GET', '/api/v1/compute/jobs/' + args.job_id + '/plan.svg' + ('?alternative=%d' % args.alternative if args.alternative is not None else ''), raw=True)
            if status == 200:
                fd = os.open(args.svg, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(content)
                out = {'written': args.svg, 'bytes': len(content)}
            else:
                out = json.loads(content or b'{}')
        else:
            status, out = go('GET', '/api/v1/compute/jobs/' + args.job_id + '/plan')
    elif args.command == 'plan-freeze':
        status, out = go('POST', '/api/v1/compute/jobs/' + args.job_id + '/freeze-alternative', {'cost_ceiling': args.cost_ceiling, 'title': args.title})
    elif args.command == 'compute-export':
        path = '/api/v1/compute/jobs/' + args.job_id + ('/plot.svg' if args.name == 'plot.svg' else '/outputs/' + args.name)
        status, content = go('GET', path, raw=True)
        if status == 200:
            fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(content)
            out = {'written': args.out, 'bytes': len(content)}
        else:
            out = json.loads(content)
    elif args.command == 'compute-log':
        status, out = go('GET', '/api/v1/compute/jobs/' + args.job_id + '/log')
    elif args.command in WORK_COMMANDS:
        status, out = work(args, go)
    elif args.command in DECISION_COMMANDS:
        status, out = decision(args, go)
    elif args.command in NATIVE_COMMANDS:
        status, out = native(args, go)
    elif args.command in EXPANSION_COMMANDS:
        status, out = expansion(args, go)
    elif args.command == 'share':
        status, out = go('POST', '/api/v1/jobs/' + args.job_id + '/shares', {'grantee_id': args.grantee, 'fields': args.fields.split(',')})
    else:
        status, out = go('GET', '/api/v1/jobs/' + args.job_id + '/projection')
    print(json.dumps(out, indent=2))
    return 0 if status < 400 else 2


if __name__ == '__main__':
    sys.exit(main())
