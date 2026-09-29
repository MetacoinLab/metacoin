"""Independent client example (Order 08 §62): the complete work loop over authenticated HTTP only — no private server
imports. Creates terms, opens a request, submits an offer (as the provider), awards, observes execution, requests the
verification, retrieves evidence, verifies the disclosed bundle offline, decides, pays and inspects reconciliation.
Idempotency keys are derived from a caller-chosen run id and reused on retries, so a retried award, decision or
payment never creates a second financial or execution identity.

    python -m metacoin_service.examples.work_client BASE OWNER_CRED_FILE PROVIDER_CRED_FILE [--run-id X] [--asset local-chain-token]

Credential files are private JSON files {"token": ...} (0600); tokens never appear in arguments, URLs or logs."""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request


def client(base, cred_file):
    st = os.stat(cred_file)
    if st.st_mode & 0o077:
        raise SystemExit('credential file must be private (0600)')
    token = json.load(open(cred_file))['token']

    def go(method, path, body=None, key=None, raw=False):
        data = json.dumps(body).encode() if body is not None else None
        headers = {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'}
        if key:
            headers['Idempotency-Key'] = key
        req = urllib.request.Request(base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                content = r.read()
                return r.status, (content if raw else json.loads(content or b'{}'))
        except urllib.error.HTTPError as e:
            content = e.read()
            return e.code, (content if raw else json.loads(content or b'{}'))
    return go


def main(argv=None):
    ap = argparse.ArgumentParser(); ap.add_argument('base'); ap.add_argument('owner_cred'); ap.add_argument('provider_cred'); ap.add_argument('--run-id', default='run-' + str(int(time.time())))
    ap.add_argument('--asset', default='action-units'); ap.add_argument('--outcome', default='INFEASIBLE'); ap.add_argument('--json', action='store_true')
    a = ap.parse_args(argv)
    owner, provider = client(a.base, a.owner_cred), client(a.base, a.provider_cred)
    K = lambda step: '%s-%s' % (a.run_id, step)
    out = {'run_id': a.run_id, 'steps': []}
    def step(name, status, body, want=(200, 201, 202)):
        out['steps'].append({'step': name, 'status': status, 'ok': status in want, 'id': body.get('id') if isinstance(body, dict) else None})
        if status not in want:
            out['failed'] = {'step': name, 'body': body}
            print(json.dumps(out, indent=1)); sys.exit(2)
        return body
    from experiments.work_contracts import fixtures
    t = step('terms', *owner('POST', '/api/v1/work/terms', {'template': 'determination', 'ceiling': 10, 'asset': a.asset}, key=K('terms')))
    f = step('freeze', *owner('POST', '/api/v1/work/terms/' + t['id'] + '/freeze', {'inputs': dict(fixtures.inputs(a.outcome), private_label='CLIENT_' + a.run_id)}, key=K('freeze')))
    r = step('request', *owner('POST', '/api/v1/work/requests', {'terms_id': t['id']}, key=K('request')))
    step('open', *owner('POST', '/api/v1/work/requests/' + r['id'] + '/open', {}, key=K('open')))
    o = step('offer', *provider('POST', '/api/v1/work/requests/' + r['id'] + '/offers', {'price_amount': 10, 'asset': a.asset, 'scheme': 'exact', 'window_seconds': 3600, 'verification': {'class': 'full_exact', 'distinct_verifier': False}}, key=K('offer')))
    cmp = step('compare', *owner('GET', '/api/v1/work/requests/' + r['id'] + '/compare'))
    aw = step('award', *owner('POST', '/api/v1/work/requests/' + r['id'] + '/award', {'offer_id': o['id']}, key=K('award')))
    again = step('award-retry', *owner('POST', '/api/v1/work/requests/' + r['id'] + '/award', {'offer_id': o['id']}, key=K('award')))
    out['award_retry_same'] = again['id'] == aw['id']
    step('ack', *provider('POST', '/api/v1/work/awards/' + aw['id'] + '/ack', {}, key=K('ack')))
    for _ in range(120):
        v = owner('GET', '/api/v1/work/awards/' + aw['id'])[1]
        if v['milestones'][0]['dimensions']['execution'] in ('completed', 'failed', 'cancelled'):
            break
        time.sleep(1)
    out['execution'] = v['milestones'][0]['dimensions']
    step('verify', *owner('POST', '/api/v1/work/awards/%s/milestones/m1/verify' % aw['id'], {}, key=K('verify')))
    for _ in range(120):
        ev = owner('POST', '/api/v1/work/awards/%s/milestones/m1/evaluate' % aw['id'], {})[1]
        if ev.get('decision_candidate') != 'pending':
            break
        time.sleep(1)
    out['evaluation'] = {k: ev.get(k) for k in ('decision_candidate', 'science', 'execution', 'payment_class', 'payable_amount')}
    receipts = step('receipts', *owner('GET', '/api/v1/work/awards/' + aw['id'] + '/receipts'))
    out['receipt_kinds'] = sorted(x['kind'] for x in receipts['items'])
    st, z = owner('GET', '/api/v1/work/awards/%s/milestones/m1/bundle?scope=restricted' % aw['id'], raw=True)
    pub = receipts['items'][0]['public_key_hex'] if receipts['items'] else None
    if st == 200 and pub:
        with tempfile.TemporaryDirectory() as tmp:
            open(os.path.join(tmp, 'bundle.zip'), 'wb').write(z)
            rep = subprocess.run([sys.executable, '-m', 'metacoin_service.economy.verify_work', 'bundle.zip', '--trust-root', pub, '--json'], cwd=tmp, capture_output=True, text=True, env=dict(os.environ, PYTHONPATH=os.getcwd()))
            try:
                rj = json.loads(rep.stdout); out['offline_verifier'] = {'integrity': rj['integrity']['ok'], 'trusted': rj['signer_trust']['trusted'], 'verdict': rj['verdict']}
            except ValueError:
                out['offline_verifier'] = {'error': rep.stderr[-300:]}
    d = step('decide', *owner('POST', '/api/v1/work/awards/%s/milestones/m1/decide' % aw['id'], {'decision': 'accept' if ev.get('decision_candidate') == 'accepted' else 'reject', 'reason': 'client example'}, key=K('decide')))
    d2 = owner('POST', '/api/v1/work/awards/%s/milestones/m1/decide' % aw['id'], {'decision': 'accept', 'reason': 'client example'}, key=K('decide'))[1]
    out['decision'] = {'decision': d['decision'], 'payable_amount': d['payable_amount'], 'retry_same': d2.get('id') == d['id']}
    if d.get('entitlement'):
        i = step('prepare', *owner('POST', '/api/v1/work/entitlements/' + d['entitlement']['id'] + '/prepare', {}, key=K('prepare')))
        step('authorize', *owner('POST', '/api/v1/work/intents/' + i['id'] + '/authorize', {}, key=K('authorize')))
        s = step('submit', *owner('POST', '/api/v1/work/intents/' + i['id'] + '/submit', {}, key=K('submit')))
        s2 = owner('POST', '/api/v1/work/intents/' + i['id'] + '/submit', {}, key=K('submit'))[1]
        out['payment'] = {'state': s['state'], 'final_amount': s['final_amount'], 'rail': s['rail'], 'transaction': s.get('transaction_ref'), 'retry_replayed': s2.get('id') == s['id']}
    rep = step('replay', *owner('POST', '/api/v1/work/journal/replay', {}))
    out['journal_consistent'] = rep['consistent']
    out['award_final'] = owner('GET', '/api/v1/work/awards/' + aw['id'])[1]['milestones'][0]['dimensions']
    out['ok'] = all(s['ok'] for s in out['steps'])
    print(json.dumps(out, indent=1))
    return 0 if out['ok'] else 2


if __name__ == '__main__':
    sys.exit(main())
