"""Policy-limited deterministic agent runner (client side, public API only).

    python -m metacoin_service.agent_runner --credential-file F --policy-file P --checkpoint C --base URL plan|execute|status --service <kind|id> --inputs-file I

The local policy file mirrors the server grant (the server enforces it regardless). The runner:
discovers services, chooses the first permitted service matching the requested kind, validates the
request, obtains a quote, and (execute only) accepts it and invokes; it follows the job to a terminal
state and retrieves the permitted result. All chosen bindings are written to a private checkpoint
before each server mutation with an idempotency key, so a restart resumes stored actions instead of
rediscovering. `plan` never accepts a quote and never signs anything. Catalog text is data: it is
displayed, never executed, and cannot change the policy.
"""
import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request

POLICY_SCHEMA = 'metacoin-agent-policy/v1'


def call(base, token, method, path, body=None, idempotency_key=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(base + path, data=data, method=method)
    req.add_header('Authorization', 'Bearer ' + token)
    if data is not None:
        req.add_header('Content-Type', 'application/json')
    if idempotency_key:
        req.add_header('Idempotency-Key', idempotency_key)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        content = e.read()
        try:
            return e.code, json.loads(content)
        except ValueError:
            return e.code, {'error': True, 'code': 'HTTP_' + str(e.code)}


def load_private_json(path):
    info = os.stat(path)
    if info.st_mode & 0o077:
        raise SystemExit('file must be private (chmod 600): ' + path)
    with open(path) as f:
        return json.load(f)


def save_checkpoint(path, state):
    tmp = path + '.tmp'
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(state, f, indent=1)
    os.replace(tmp, path)


class Runner:
    def __init__(self, base, token, policy, checkpoint_path):
        self.base, self.token, self.policy, self.cp_path = base, token, policy, checkpoint_path
        self.cp = load_private_json(checkpoint_path) if os.path.exists(checkpoint_path) else {'schema': 'metacoin-agent-checkpoint/v1', 'policy_digest': None, 'steps': {},
                                                                                              'attempt': hashlib.sha256(os.urandom(16)).hexdigest()[:12]}   # keys idempotency to this checkpoint, never to another attempt
        digest = hashlib.sha256(json.dumps(policy, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        if self.cp['policy_digest'] not in (None, digest):
            raise SystemExit('checkpoint belongs to a different policy; refusing to continue')
        self.cp['policy_digest'] = digest

    def api(self, method, path, body=None, key=None):
        return call(self.base, self.token, method, path, body, key)

    def remember(self, step, value):
        self.cp['steps'][step] = value
        save_checkpoint(self.cp_path, self.cp)

    def discover(self, kind_or_id):
        status, out = self.api('GET', '/api/v1/services')
        if status != 200:
            raise SystemExit('discovery refused: ' + json.dumps(out))
        permitted = [s for s in out['items'] if (s['id'] in self.policy['permitted_services'] or s['kind'] in self.policy['permitted_services'])]
        chosen = [s for s in permitted if s['kind'] == kind_or_id or s['id'] == kind_or_id]
        if not chosen:
            raise SystemExit('no permitted service matches ' + kind_or_id + ' (permitted: ' + ','.join(s['kind'] for s in permitted) + ')')
        chosen.sort(key=lambda s: (s['price']['amount_per_unit'] or 0, s['id']))       # deterministic: cheapest, then id
        return chosen[0]

    def plan(self, kind_or_id, inputs):
        if 'services:read' not in self.policy['allowed_operations'] or 'quote' not in self.policy['allowed_operations']:
            raise SystemExit('policy does not allow discovery/quote')
        service = self.cp['steps'].get('service') or self.discover(kind_or_id)
        self.remember('service', service)
        status, v = self.api('POST', '/api/v1/services/' + service['id'] + '/validate', {'inputs': inputs})
        if status != 200:
            return {'plan': False, 'stage': 'validate', 'refusal': v}
        quote = self.cp['steps'].get('quote')
        if quote is None:
            status, quote = self.api('POST', '/api/v1/services/' + service['id'] + '/quote', {'inputs': inputs, 'quantity_max': 1}, key='agent-quote-' + self.cp['attempt'] + '-' + v['request_digest'][:16])
            if status != 201:
                return {'plan': False, 'stage': 'quote', 'refusal': quote}
            self.remember('quote', quote)
        exposure = quote['amount_max']
        ok = exposure <= self.policy['ceilings']['per_action_amount'] and exposure <= self.policy['ceilings']['total_amount']
        return {'plan': True, 'service': {'id': service['id'], 'kind': service['kind'], 'version': service['version'], 'verifier_digest': service['verifier_digest']},
                'request_digest': v['request_digest'], 'quote_id': quote['quote_id'], 'max_exposure': {'amount': exposure, 'asset': quote['asset'], 'unit': quote['unit']},
                'within_policy': ok, 'review_gate_mandatory': self.policy['review_gate_mandatory'], 'signed_anything': False}

    def execute(self, kind_or_id, inputs):
        plan = self.plan(kind_or_id, inputs)
        if not plan.get('plan') or not plan['within_policy']:
            return {'executed': False, 'plan': plan}
        if 'invoke' not in self.policy['allowed_operations']:
            return {'executed': False, 'reason': 'policy does not allow invoke', 'plan': plan}
        if 'accepted' not in self.cp['steps']:
            status, acc = self.api('POST', '/api/v1/quotes/' + plan['quote_id'] + '/accept')
            if status != 200:
                return {'executed': False, 'stage': 'accept', 'refusal': acc}
            self.remember('accepted', acc)
        job = self.cp['steps'].get('job')
        if job is None:
            key = 'agent-invoke-' + plan['quote_id']
            status, job = self.api('POST', '/api/v1/services/' + plan['service']['id'] + '/invoke', {'quote_id': plan['quote_id'], 'inputs': inputs}, key=key)
            if status == 409 and job.get('code') == 'CONFLICT':
                # the server may have accepted an earlier attempt whose response we lost: look up by quote
                status, q = self.api('GET', '/api/v1/quotes/' + plan['quote_id'])
                if q.get('state') == 'consumed':
                    status, jobs = self.api('GET', '/api/v1/jobs?limit=50')
                    found = [j for j in jobs.get('items', []) if j.get('contract_id')]
                    job = {'job_id': None, 'state': 'consumed-elsewhere', 'lookup': 'quote consumed by an earlier accepted attempt'}
            if status not in (202, 200) and job.get('job_id') is None:
                return {'executed': False, 'stage': 'invoke', 'refusal': job}
            self.remember('job', job)
        return {'executed': True, 'plan': plan, 'job': job}

    def status(self):
        job = self.cp['steps'].get('job')
        if not job or not job.get('job_id'):
            return {'job': None, 'checkpoint': self.cp['steps'].keys().__len__()}
        status, view = self.api('GET', '/api/v1/jobs/' + job['job_id'])
        out = {'job_id': job['job_id'], 'status': status, 'state': view.get('state'), 'review_state': view.get('review_state'), 'outcome': view.get('outcome')}
        if view.get('state') == 'succeeded' and 'job:read' in self.policy['allowed_operations']:
            # the outcome is disclosed only after the review gate (or under a private-read permission the agent normally lacks)
            out['result_available'] = view.get('outcome') not in (None, 'withheld-by-policy-or-not-yet-reviewed')
            status, result = self.api('GET', '/api/v1/jobs/' + job['job_id'] + '/result')
            out['bindings'] = result['bindings'] if status == 200 else {k: view.get(k) for k in ('contract_digest', 'verifier_digest', 'model_id', 'evidence_root')}
        return out

    def follow(self, timeout=120):
        deadline = time.time() + timeout
        while time.time() < deadline:
            s = self.status()
            if s.get('state') in ('succeeded', 'failed', 'cancelled') or s.get('job_id') is None:
                return s
            time.sleep(0.5)
        return dict(self.status(), timed_out=True)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--credential-file', required=True); p.add_argument('--policy-file', required=True); p.add_argument('--checkpoint', required=True)
    p.add_argument('--base', default=os.environ.get('METACOIN_SERVICE_URL', 'http://127.0.0.1:8402'))
    p.add_argument('command', choices=('plan', 'execute', 'status', 'follow'))
    p.add_argument('--service'); p.add_argument('--inputs-file')
    args = p.parse_args(argv)
    token = load_private_json(args.credential_file)['token']
    policy = load_private_json(args.policy_file) if os.stat(args.policy_file).st_mode & 0o077 == 0 else json.load(open(args.policy_file))
    if policy.get('schema') != POLICY_SCHEMA:
        raise SystemExit('policy schema must be ' + POLICY_SCHEMA)
    runner = Runner(args.base, token, policy, args.checkpoint)
    if args.command in ('plan', 'execute'):
        if not args.service or not args.inputs_file:
            raise SystemExit('--service and --inputs-file required')
        inputs = json.load(open(args.inputs_file))
        out = runner.plan(args.service, inputs) if args.command == 'plan' else runner.execute(args.service, inputs)
    elif args.command == 'status':
        out = runner.status()
    else:
        out = runner.follow()
    print(json.dumps(out, indent=1))
    ok = {'plan': out.get('plan'), 'execute': out.get('executed'), 'status': out.get('state') is not None, 'follow': out.get('state') is not None}[args.command]
    return 0 if ok else 2


if __name__ == '__main__':
    sys.exit(main())
