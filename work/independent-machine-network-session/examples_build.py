"""Developer examples with ACTUALLY executed commands and their structured responses (order 2026-10-23 §13): a fresh
custody-separated domain is started (API + worker + one signer), a provider enrols through the public CLI, publishes a pool and an
offer, a buyer reserves with a signed intent, the provider delivers and is settled, a mission is built and planned, a product is
checked for reuse, and the exported record is verified offline. Every command line is printed as run (credentials replaced by
placeholders) together with the exact response (tokens redacted)."""
import json, os, re, subprocess, sys, tempfile, time
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]; sys.path.insert(0, str(ROOT)); os.environ['METACOIN_X402_DISABLED'] = '1'
from metacoin_service.tests.journeys_mission import MDom, SignerProcess, WitnessProcess, PY
from metacoin_service.tests.journeys_network import NamedSigner
out_dir = Path(sys.argv[1]); out_dir.mkdir(parents=True, exist_ok=True); W = WitnessProcess(); sg = NamedSigner(('dom-ex',), 'sig-example')
wenv = {'METACOIN_WITNESS_URL': W.base, 'METACOIN_WITNESS_TOKEN_FILE': str(W.home / 'token'), 'METACOIN_WITNESS_KEY_ID': W.identity['key_id'], 'METACOIN_SIGNER_URL': sg.base, 'METACOIN_SIGNER_TOKEN_FILE': str(sg.home / 'token'), 'METACOIN_REQUIRE_EXTERNAL_CUSTODY': '1'}
D = MDom('dom-ex', wenv); steps = []; RED = re.compile(r'(mck_|mcn_)[A-Za-z0-9_-]{8,}')
def redact(x):
    return json.loads(RED.sub(r'\1<redacted>', json.dumps(x, default=str)))
def cli(args, **kw):
    cmd = [PY, '-m', 'metacoin_provider'] + args; p = subprocess.run(cmd, cwd=str(ROOT / 'provider_client'), env=dict(os.environ, PYTHONPATH=str(ROOT / 'provider_client')), capture_output=True, text=True, timeout=300)
    try:
        body = json.loads(p.stdout)
    except ValueError:
        body = {'stdout': p.stdout[-400:], 'stderr': p.stderr[-400:]}
    shown = ' '.join(('<admin-cred>' if a == str(admin) else '<cred>' if a == str(cred) else '<signer-token>' if a == str(sg.home / 'token') else a) for a in ['metacoin-provider'] + args)
    steps.append({'command': shown, 'exit_code': p.returncode, 'response': redact(body)}); return body
def api(method, path, role, body=None, **kw):
    st, r = D.call(method, path, role=role, json=body) if body is not None else D.call(method, path, role=role, **kw)
    steps.append({'command': '%s %s%s (as %s)' % (method.upper(), path, ' ' + json.dumps(body) if body is not None else '', role), 'status': st, 'response': redact(r)}); return st, r
try:
    admin = D.cred_file('owner'); cred = Path(D.inst.temp.name) / 'prov-cred.json'; home = Path(D.inst.temp.name) / 'prov-home'
    enr = cli(['--base', D.base, '--signer', sg.base, '--signer-token', str(sg.home / 'token'), 'enroll', '--admin-cred', str(admin), '--name', 'prov-ex', '--ceiling', '100000', '--out', str(cred)])
    cli(['--base', D.base, '--cred', str(cred), '--home', str(home), '--signer', sg.base, '--signer-token', str(sg.home / 'token'), '--actor', 'prov-ex', 'init', '--name', 'prov-ex'])
    cli(['--home', str(home), 'handshake'])
    D.actors['prov-ex'] = {'account': enr['account_id'], 'principal_id': enr['principal_id'], 'token': json.load(open(cred))['token']}
    st, prov = api('post', '/api/v1/work/providers', 'owner', {'name': 'prov-ex', 'principal_id': enr['principal_id'], 'capabilities': {'kinds': ['terrain_tile', 'route_feasibility'], 'verification_classes': ['analytical'], 'payment_schemes': ['exact']}, 'relationship': {'relationship': 'same_operator', 'same_host': True, 'shared_custody': False, 'identity': {'operator_id': 'op-example', 'machine_id': 'rig-example', 'worker_instance': 'w1', 'signer_id': 'sig-example', 'domain': 'dom-ex'}}})
    cli(['--home', str(home), 'capabilities'])
    t = int(time.time()); st, pool = api('post', '/api/v1/resources/pools', 'prov-ex', {'kind': 'compute', 'label': 'rig-example', 'slot_unit': 'cpu_slot', 'units_per_slot_second': 1000, 'resolution_seconds': 1, 'schedule': [{'start': t - 60, 'end': t + 7200, 'capacity': 1}]})
    st, offer = api('post', '/api/v1/resources/offers', 'prov-ex', {'kind': 'compute', 'resource_id': 'cpu-example', 'capacity': 100000, 'price_per_unit': 1, 'window_start': t - 60, 'window_end': t + 7200, 'pool_id': pool['id']})
    # a buyer under external custody: enrol through the same signer (its own key), fund with the zero-value fixture, reserve with a signed intent
    bst, b = sg.client.call('POST', '/enroll', {'name': 'buyer-ex', 'ceiling': 100000}); st, buyer = api('post', '/api/v1/native/actors/enroll', 'owner', {'name': 'buyer-ex', 'public_hex': b['public_hex'], 'key_id': b['key_id'], 'signer_id': b['signer_id']}); sg.client.call('POST', '/bind', {'actor': 'buyer-ex', 'account': buyer['account_id']})
    D.actors['buyer-ex'] = {'account': buyer['account_id'], 'principal_id': buyer['principal_id'], 'token': buyer['credential']['token'], 'external': True}; api('post', '/api/v1/native/fixtures/fund', 'owner', {'account_id': buyer['account_id'], 'amount': 5000})
    st, prep = api('post', '/api/v1/resources/reservations/prepare', 'buyer-ex', {'offer_id': offer['id'], 'offer_version': offer['version'], 'quantity': 3000, 'request_id': 'ex-res-1', 'interval_start': t, 'interval_end': t + 60})
    s_, sig = sg.client.sign('buyer-ex', prep['message'], context=prep['context']); steps.append({'command': 'POST <signer>/sign {actor: buyer-ex, intent: <message>, context: <context>}', 'status': s_, 'response': {k: v for k, v in sig.items() if k != 'signature_hex'} | {'signature_hex': '<64 bytes hex>'}})
    st, res = api('post', '/api/v1/resources/reservations', 'buyer-ex', dict(prep['reserve_body'], native_signature={'message': prep['message'], 'signature_hex': sig['signature_hex'], 'key_id': sig['key_id']}))
    cli(['--base', D.base, '--cred', str(cred), 'activate', res['id']]); dl = cli(['--base', D.base, '--cred', str(cred), 'deliver-compute', res['id'], '--executor', 'env PYTHONPATH=%s %s -m metacoin_service.lunar task' % (ROOT, PY)])
    api('post', '/api/v1/resources/reservations/%s/settle' % res['id'], 'buyer-ex', {}); api('get', '/api/v1/resources/pools/' + pool['id'], 'owner'); cli(['--base', D.base, '--cred', str(cred), 'earnings'])
    api('get', '/api/v1/native/accounts/%s/liquidity' % buyer['account_id'], 'buyer-ex'); api('get', '/api/v1/work/providers/%s/identity' % prov['id'], 'owner')
    st, m = api('post', '/api/v1/missions', 'buyer-ex', {'title': 'example mission', 'graph': {'milestones': [{'key': 'cpu', 'kind': 'resource', 'depends': [], 'spec': {'offer_kind': 'compute', 'quantity': 2000, 'max_price_per_unit': 2}}]}, 'policy': {'priority': 2}})
    api('post', '/api/v1/missions/%s/freeze' % m['id'], 'buyer-ex', {}); api('post', '/api/v1/portfolio/plan', 'buyer-ex', {'mission_ids': [m['id']], 'planner': 'reference'}); api('get', '/api/v1/missions/%s/continuity' % m['id'], 'buyer-ex')
    api('post', '/api/v1/missions/%s/takeover' % m['id'], 'buyer-ex', {'holder': 'runner-example'}); api('post', '/api/v1/missions/%s/step' % m['id'], 'buyer-ex', {'generation': 1})
    cli(['--base', D.base, '--cred', str(cred), 'roots']); rp = Path(D.inst.temp.name) / 'roots.json'; rp.write_text(json.dumps(steps[-1]['response'])); ex = Path(D.inst.temp.name) / 'export.json'
    cli(['--base', D.base, '--cred', str(cred), 'export', '--out', str(ex)]); cli(['verify', str(ex), '--roots', str(rp)]); cli(['conformance'])
    rec = {'schema': 'metacoin-developer-examples/v2', 'revision': D.call('get', '/api/health')[1].get('revision'), 'environment': 'fresh custody-separated domain dom-ex (METACOIN_REQUIRE_EXTERNAL_CUSTODY=1), one signer process (sig-example), witness, x402 disabled; commands executed as shown, responses verbatim except redacted tokens', 'steps': steps}
    (out_dir / 'developer-examples.json').write_text(json.dumps(rec, indent=1)); md = ['# Developer examples (executed %s)' % time.strftime('%Y-%m-%d'), '', rec['environment'], '']
    for s in steps:
        md += ['```', s['command'], '```', '→ ' + ('exit %s' % s['exit_code'] if 'exit_code' in s else 'HTTP %s' % s['status']), '```json', json.dumps(s['response'], indent=1)[:1800], '```', '']
    (out_dir / 'DEVELOPER_EXAMPLES.md').write_text('\n'.join(md)); print('examples', len(steps), 'written to', out_dir)
finally:
    D.close(); sg.close(); W.close()
