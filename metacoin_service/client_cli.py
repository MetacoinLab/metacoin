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
    s = sub.add_parser('submit'); s.add_argument('contract_id'); s.add_argument('--idempotency-key')
    p = sub.add_parser('poll', help='wait until the job is terminal'); p.add_argument('job_id'); p.add_argument('--timeout', type=int, default=120)
    st = sub.add_parser('status'); st.add_argument('job_id')
    r = sub.add_parser('result'); r.add_argument('job_id')
    rr = sub.add_parser('review-request'); rr.add_argument('job_id')
    d = sub.add_parser('decide'); d.add_argument('job_id'); d.add_argument('decision', choices=('accepted', 'rejected'))
    e = sub.add_parser('export', help='download an artifact (public JSON or private ciphertext) to a fresh file')
    e.add_argument('artifact_id'); e.add_argument('--out', required=True)
    cm = sub.add_parser('compare'); cm.add_argument('job_a'); cm.add_argument('job_b')
    sub.add_parser('me'); sub.add_parser('budget')
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
        status, out = go('POST', '/api/v1/jobs', {'contract_id': args.contract_id}, idempotency_key=args.idempotency_key)
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
    else:
        status, out = go('GET', '/api/v1/budget')
    print(json.dumps(out, indent=2))
    return 0 if status < 400 else 2


if __name__ == '__main__':
    sys.exit(main())
