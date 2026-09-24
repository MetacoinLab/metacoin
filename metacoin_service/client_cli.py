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


def call(base, token, method, path, body=None, raw=False, idempotency_key=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(base + path, data=data, method=method)
    req.add_header('Authorization', 'Bearer ' + token)
    if data is not None:
        req.add_header('Content-Type', 'application/json')
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
    rs = sub.add_parser('run-status'); rs.add_argument('run_id'); rs.add_argument('--follow', action='store_true'); rs.add_argument('--timeout', type=int, default=300)
    rc = sub.add_parser('run-cancel'); rc.add_argument('run_id')
    cc = sub.add_parser('campaign-create', help='create a scientific campaign from a JSON definition file'); cc.add_argument('--file', required=True); cc.add_argument('--preview', action='store_true')
    cs = sub.add_parser('campaign-status'); cs.add_argument('campaign_id'); cs.add_argument('--results', action='store_true'); cs.add_argument('--csv', help='write results CSV to this new file')
    cb = sub.add_parser('campaign-branch', help='fork a campaign with explicit changed assumptions'); cb.add_argument('campaign_id'); cb.add_argument('--base-changes', help='JSON object field->integer'); cb.add_argument('--candidates', help='comma-separated succeeded candidate indexes'); cb.add_argument('--name')
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
        status, out = go('POST', '/api/v1/campaigns/' + args.campaign_id + '/branch', body)
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
    elif args.command == 'share':
        status, out = go('POST', '/api/v1/jobs/' + args.job_id + '/shares', {'grantee_id': args.grantee, 'fields': args.fields.split(',')})
    else:
        status, out = go('GET', '/api/v1/jobs/' + args.job_id + '/projection')
    print(json.dumps(out, indent=2))
    return 0 if status < 400 else 2


if __name__ == '__main__':
    sys.exit(main())
