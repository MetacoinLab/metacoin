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
                      'calibration-design', 'model-warmup', 'model-batching', 'plan', 'plans', 'plan-accept', 'intent', 'intents', 'intent-continue', 'document-import', 'documents', 'document', 'document-compare', 'document-page', 'document-action', 'table', 'table-annotate', 'mapping-preview', 'mapping-create', 'mapping', 'mapping-confirm', 'resource-plan', 'plan-view', 'plan-freeze', 'bundle-export', 'bundle-check', 'bundle-import', 'disagreements', 'disagreement-decide', 'notebook-create', 'notebooks', 'notebook', 'notebook-version', 'notebook-compare', 'notebook-export', 'analysis-create', 'analyses', 'analysis', 'analysis-revise', 'analysis-freeze', 'analysis-impact', 'analysis-regenerate', 'analysis-report', 'report', 'report-projection', 'projection-verify', 'package-create', 'packages', 'package', 'package-compat', 'package-import', 'package-export', 'package-instantiate', 'package-quote', 'package-run', 'package-runs', 'package-run', 'package-retry', 'package-bundle', 'package-retire'}


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
