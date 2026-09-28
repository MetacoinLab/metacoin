# EXPERIMENTAL — private test chain only — not the protocol's money layer, which remains [SPEC] and zero-value by MIP-0001/0002; chain-agnostic law unchanged.
"""Independent client process for a priced POST invocation over x402 (no server internals)."""
import hashlib, json, os, sys, httpx
from integrations.x402 import loopback_harness as lb
ns = lb.load()
base, sid, cred_path, body_text = sys.argv[1:5]
mutation = sys.argv[5] if len(sys.argv) > 5 else None
token = json.load(open(cred_path))['token']
url = base + '/api/v1/x402/services/' + sid + '/invoke'
headers = {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'}
client = lb.build_client(ns)
first = httpx.post(url, headers=headers, content=body_text)
out = {'first_status': first.status_code}
if first.status_code != 402:
    print(json.dumps(dict(out, body=first.text[:300]))); sys.exit(0)
required = client.http.get_payment_required_response(lambda n: first.headers.get(n), first.content)
extensions = dict(required.extensions or {})
identifier = mutation.split(':', 1)[1] if mutation and mutation.startswith('replay:') else ns.pi.generate_payment_id()
ns.pi.append_payment_identifier_to_extensions(extensions, identifier)
payload = client.core.create_payment_payload(required, extensions=extensions)
send_body = body_text
if mutation == 'body':
    send_body = json.dumps(dict(json.loads(body_text), inputs=dict(json.loads(body_text)['inputs'], reserve=1)), sort_keys=True)
elif mutation == 'amount':
    payload = payload.model_copy(update={'accepted': payload.accepted.model_copy(update={'amount': '999'})})
pay_headers = client.http.encode_payment_signature_header(payload)
second = httpx.post(url, headers=dict(headers, **pay_headers), content=send_body)
out.update(second_status=second.status_code, identifier=identifier)
if second.status_code in (200, 202):
    settle = client.http.get_payment_settle_response(lambda n: second.headers.get(n))
    out.update(settled=settle.model_dump(by_alias=True, exclude_none=True), body=json.loads(second.content))
else:
    hdr = second.headers.get(ns.http.PAYMENT_REQUIRED_HEADER)
    out['error'] = json.loads(ns.http.safe_base64_decode(hdr)).get('error') if hdr else second.text[:200]
print(json.dumps(out))
