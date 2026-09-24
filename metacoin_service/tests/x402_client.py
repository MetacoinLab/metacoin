"""Real x402 client process: obtains 402 requirements over TCP, builds the SDK payload with the
payment-identifier extension, retries with PAYMENT-SIGNATURE, decodes PAYMENT-RESPONSE."""
import hashlib, json, sys, httpx
from experiments.private_receipts import receipt as merkle
from integrations.x402 import loopback_harness as lb
ns = lb.load()
base, job_id = sys.argv[1], sys.argv[2]
mutation = (sys.argv[3] or None) if len(sys.argv) > 3 else None
url = base + '/api/v1/x402/jobs/' + job_id + '/public-bundle'
client = lb.build_client(ns)
first = httpx.get(url)
out = {'first_status': first.status_code, 'has_payment_required': ns.http.PAYMENT_REQUIRED_HEADER in first.headers}
if first.status_code != 402:
    print(json.dumps(dict(out, body=first.text[:200]))); sys.exit(0)
required = client.http.get_payment_required_response(lambda n: first.headers.get(n), first.content)
extensions = dict(required.extensions or {})
acc = required.accepts[0]
expected = {k: acc.extra[k] for k in ('job_id', 'contract_digest', 'evidence_root', 'resource_version', 'route')}
identifier = sys.argv[4] if len(sys.argv) > 4 else ns.pi.generate_payment_id()     # the client's idempotency key
if mutation == 'no_identifier':
    identifier = None
else:
    ns.pi.append_payment_identifier_to_extensions(extensions, identifier)
payload = client.core.create_payment_payload(required, extensions=extensions)
upd = lambda **kw: payload.model_copy(update={'accepted': payload.accepted.model_copy(update=kw)})
if mutation == 'amount': payload = upd(amount='999')
elif mutation == 'pay_to': payload = upd(pay_to='0x' + '33' * 20)
elif mutation == 'network': payload = upd(network='eip155:1')
elif mutation == 'extra': payload = upd(extra=dict(acc.extra, contract_digest='0' * 64))
elif mutation == 'resource': payload = payload.model_copy(update={'resource': ns.schemas.ResourceInfo(url=base + '/api/v1/x402/jobs/other/public-bundle')})
elif mutation == 'signature': payload = payload.model_copy(update={'payload': dict(payload.payload, signature='forged')})
headers = client.http.encode_payment_signature_header(payload)
second = httpx.get(url, headers=headers)
out.update(second_status=second.status_code, identifier=identifier)
if second.status_code == 200:
    settle = client.http.get_payment_settle_response(lambda n: second.headers.get(n))
    out.update(settled=settle.model_dump(by_alias=True, exclude_none=True), bundle_keys=sorted(json.loads(second.content)))
else:
    hdr = second.headers.get(ns.http.PAYMENT_REQUIRED_HEADER)
    out['error'] = json.loads(ns.http.safe_base64_decode(hdr)).get('error') if hdr else second.text[:200]
print(json.dumps(out))
