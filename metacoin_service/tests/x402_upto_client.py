"""Independent client process for a metered (upto) invocation over the HTTP route. It derives the local chain's synthetic
payer key from eth-tester's deterministic accounts and redirects the SDK's Permit2/proxy constants to the coordinator's
recorded local deployments (published under /api/v1/x402/settlements), so the EIP-712 domain matches. Test topology only."""
import json
import sys
import time
import httpx

base, sid, cred_path, body_text, identifier = sys.argv[1:6]
token = json.load(open(cred_path))['token']
H = {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'}
rec = httpx.get(base + '/api/v1/x402/settlements', headers=H, timeout=30).json()['local_chain']
from integrations.x402.local_chain import harness
for name, mod in harness.sdk_modules().items():
    for attr, value in (('PERMIT2_ADDRESS', rec['permit2']), ('X402_UPTO_PERMIT2_PROXY_ADDRESS', rec['proxy'])):
        if hasattr(mod, attr):
            setattr(mod, attr, value)
from eth_tester import PyEVMBackend
key = PyEVMBackend().account_keys[2]                       # the funded synthetic payer of the coordinator's chain (same deterministic key set)
import x402.client as client, x402.http as http
from x402.extensions import payment_identifier as pi
from x402.mechanisms.evm.signers import EthAccountSigner
from x402.mechanisms.evm.upto import UptoEvmClientScheme
from eth_account import Account
core = client.x402ClientSync().register('eip155:*', UptoEvmClientScheme(EthAccountSigner(Account.from_key(key.to_hex())))).set_spend_controls(False)
hclient = http.x402HTTPClientSync(core)
url = base + '/api/v1/x402/services/' + sid + '/invoke'
first = httpx.post(url, headers=H, content=body_text, timeout=60)
out = {'first_status': first.status_code}
if first.status_code != 402:
    print(json.dumps(dict(out, body=first.json()))); sys.exit(0)
required = hclient.get_payment_required_response(lambda n: first.headers.get(n), first.content)
extensions = dict(required.extensions or {})
pi.append_payment_identifier_to_extensions(extensions, identifier)
payload = core.create_payment_payload(required, extensions=extensions)
second = httpx.post(url, headers=dict(H, **hclient.encode_payment_signature_header(payload)), content=body_text, timeout=120)
out.update(scheme=required.accepts[0].scheme, authorized_max=payload.payload['permit2Authorization']['permitted']['amount'], second_status=second.status_code, body=second.json() if second.content else {})
if second.status_code == 402:
    hdr = second.headers.get(http.PAYMENT_REQUIRED_HEADER)
    out['error'] = json.loads(http.safe_base64_decode(hdr)).get('error') if hdr else None
    print(json.dumps(out)); sys.exit(0)
jid = out['body']['job_id']; pid = out['body']['settlement']['payment_id']
deadline = time.time() + 600
while time.time() < deadline:
    j = httpx.get(base + '/api/v1/jobs/' + jid, headers=H, timeout=30).json()
    if j['state'] in ('succeeded', 'failed', 'cancelled'):
        break
    time.sleep(1)
st = httpx.get(base + '/api/v1/x402/settlements/' + pid, headers=H, timeout=120).json()
out.update(job_state=j['state'], settlement={k: st.get(k) for k in ('state', 'authorized_max', 'final_amount', 'transaction', 'network', 'asset')})
print(json.dumps(out))
