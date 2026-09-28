# EXPERIMENTAL — private test chain only — not the protocol's money layer, which remains [SPEC] and zero-value by MIP-0001/0002; chain-agnostic law unchanged.
"""Group F application route: variable-price (upto) invocations. A quote chooses the scheme explicitly; the 402 asks for a
Permit2 authorization up to the ceiling; a verified authorization creates the job without moving funds; settlement after
the job transfers the measured amount (below the ceiling when usage is smaller); a failed job leaves the authorization
unused; a replayed authorization returns the same job; fixed-price (exact) invocations keep working as the fallback.
Local py-evm chain with the pinned contracts (test-http mode): local protocol validation, not production settlement."""
import json
import unittest

from metacoin_service.tests.test_compute_engine import ComputeInstance, HAVE_RUNTIME, batch_spec
from metacoin_service.tests.test_models import ModelInstance, HAVE_TORCH, installed, GEN, EMB
from integrations.x402.local_chain import harness

HAVE_CHAIN = harness.ARTIFACTS.exists()


class UptoInstance(ModelInstance, ComputeInstance):
    def __init__(self):
        super().__init__(provider_mode='test-http')


def upto_exchange(inst, sid, quote_id, inputs, identifier, mutate=None):
    """Client side over the HTTP route: 402 -> signed upto authorization (SDK client on the local chain's funded synthetic payer) -> job."""
    import x402.http as http
    from x402.extensions import payment_identifier as pi
    svc = inst.client.app.state.services
    chain = svc.sales.local_chain()
    core, hclient = harness.client_for(chain)
    H = inst.h('owner'); body = json.dumps({'quote_id': quote_id, 'inputs': inputs}, sort_keys=True)
    first = inst.client.post('/api/v1/x402/services/' + sid + '/invoke', headers=dict(H, **{'Content-Type': 'application/json'}), content=body)
    if first.status_code != 402:
        return {'first_status': first.status_code, 'body': first.json()}
    required = hclient.get_payment_required_response(lambda n: first.headers.get(n), first.content)
    accepts = required.accepts[0]
    extensions = dict(required.extensions or {})
    pi.append_payment_identifier_to_extensions(extensions, identifier)
    payload = core.create_payment_payload(required, extensions=extensions)
    if mutate:
        payload = mutate(payload)
    headers = hclient.encode_payment_signature_header(payload)
    second = inst.client.post('/api/v1/x402/services/' + sid + '/invoke', headers=dict(H, **{'Content-Type': 'application/json'}, **headers), content=body)
    err = None
    if second.status_code == 402:
        hdr = second.headers.get(http.PAYMENT_REQUIRED_HEADER)
        err = json.loads(http.safe_base64_decode(hdr)).get('error') if hdr else None
    return {'first_status': 402, 'scheme': accepts.scheme, 'max': accepts.amount, 'second_status': second.status_code, 'body': second.json() if second.content else {}, 'error': err,
            'authorized_max': payload.payload['permit2Authorization']['permitted']['amount'], 'chain': chain}


@unittest.skipUnless(HAVE_RUNTIME and HAVE_CHAIN, 'needs the compute interpreter and the built local-chain artifacts')
class UptoRouteTests(unittest.TestCase):
    def setUp(self):
        self.inst = UptoInstance(); self.addCleanup(self.inst.close); self.c = self.inst.client; self.H = self.inst.h('owner')
        self.w = self.inst.worker(); self.addCleanup(self.w.offline)
        self.sid = next(s['id'] for s in self.c.get('/api/v1/services', headers=self.H).json()['items'] if s['kind'] == 'temporal_batch')

    def quote(self, inputs, scheme, sid=None):
        q = self.c.post('/api/v1/services/' + (sid or self.sid) + '/quote', headers=self.H, json={'inputs': inputs, 'scheme': scheme})
        self.assertEqual(q.status_code, 201, q.text); q = q.json()
        self.assertEqual(self.c.post('/api/v1/quotes/' + q['quote_id'] + '/accept', headers=self.H).status_code, 200)
        return q

    def test_metered_authorization_settlement_unused_and_replay(self):
        self.assertEqual(self.c.post('/api/v1/services/' + self.sid + '/quote', headers=self.H, json={'inputs': batch_spec(), 'scheme': 'streaming'}).status_code, 422)
        caps = self.c.get('/api/v1/capabilities', headers=self.H).json()['x402_variable_price_upto']
        self.assertTrue(caps['available']); self.assertFalse(caps['externally_validated'])
        inputs = dict(batch_spec(private_label='UPTO'), schema=batch_spec()['schema'])
        q = self.quote(inputs, 'upto')
        self.assertEqual((q['scheme'], q['network'], q['pay_to']), ('upto', 'local-chain', 'local-chain-provider')); self.assertIn('authorized up to', q['settlement_rule'])
        ex = upto_exchange(self.inst, self.sid, q['quote_id'], inputs, 'upto-metered-1-' + 'a' * 20)
        self.assertEqual((ex['first_status'], ex['scheme'], ex['second_status']), (402, 'upto', 202), ex.get('error') or ex['body'])
        self.assertEqual(ex['max'], str(q['amount_max'])); self.assertEqual(ex['authorized_max'], str(q['amount_max']))
        jid = ex['body']['job_id']; pid = ex['body']['settlement']['payment_id']
        self.assertEqual(ex['body']['settlement']['state'], 'AUTHORIZED')
        chain = ex['chain']; before = chain.balances()
        st = self.c.get('/api/v1/x402/settlements/' + pid, headers=self.H).json()
        self.assertEqual((st['state'], st['job_state']), ('AUTHORIZED', 'queued')); self.assertEqual(chain.balances(), before)          # nothing moved before the job
        self.assertEqual(self.w.run_once(), (jid, 'succeeded'))
        st = self.c.get('/api/v1/x402/settlements/' + pid, headers=self.H).json()
        self.assertEqual(st['state'], 'SETTLED', st); self.assertEqual(int(st['final_amount']), int(st['authorized_max'])); self.assertEqual(len(st['transaction']), 66)
        after = chain.balances(); self.assertEqual(before['payer'] - after['payer'], int(st['final_amount'])); self.assertEqual(after['recipient'] - before['recipient'], int(st['final_amount']))
        self.assertEqual(self.c.get('/api/v1/x402/settlements/' + pid, headers=self.H).json()['transaction'], st['transaction'])       # idempotent
        self.assertEqual(chain.balances(), after)
        usage = [u for u in self.c.get('/api/v1/usage', headers=self.H).json()['items'] if u['job_id'] == jid][0]
        self.assertEqual(usage['assessed_charge'], int(st['final_amount']))
        rows = [r for r in self.c.get('/api/v1/statements', headers=self.H).json()['statement']['rows'] if r.get('job_id') == jid]
        self.assertEqual((rows[0]['settlement']['scheme'], rows[0]['settlement']['state']), ('upto', 'SETTLED'))
        # replay of the same authorization returns the same job, no second job, no second settlement
        again = upto_exchange(self.inst, self.sid, q['quote_id'], inputs, 'upto-metered-1-' + 'a' * 20)
        self.assertEqual((again['second_status'], again['body'].get('replayed'), again['body'].get('job_id')), (200, True, jid))
        self.assertEqual(chain.balances(), after)
        # a cancelled job leaves the authorization unused: no transfer, no fixed-price substitution
        q2 = self.quote(dict(inputs, private_label='UPTO2'), 'upto')
        ex2 = upto_exchange(self.inst, self.sid, q2['quote_id'], dict(inputs, private_label='UPTO2'), 'upto-metered-2-' + 'b' * 20)
        self.assertEqual(ex2['second_status'], 202, ex2)
        self.assertEqual(self.c.post('/api/v1/jobs/' + ex2['body']['job_id'] + '/cancel', headers=self.H).status_code, 200)
        st2 = self.c.get('/api/v1/x402/settlements/' + ex2['body']['settlement']['payment_id'], headers=self.H).json()
        self.assertEqual(st2['state'], 'AUTHORIZATION_UNUSED'); self.assertIsNone(st2['transaction']); self.assertEqual(chain.balances(), after)
        # a tampered authorization (recipient changed) is refused before any job exists
        def wrong_recipient(payload):
            p = payload.payload; p['permit2Authorization']['witness']['to'] = chain.deployer if 'witness' in p['permit2Authorization'] else p['permit2Authorization'].get('to'); return payload
        q3 = self.quote(dict(inputs, private_label='UPTO3'), 'upto')
        ex3 = upto_exchange(self.inst, self.sid, q3['quote_id'], dict(inputs, private_label='UPTO3'), 'upto-metered-3-' + 'c' * 20, mutate=wrong_recipient)
        self.assertEqual(ex3['second_status'], 402, ex3); self.assertIsNotNone(ex3['error'])
        self.assertEqual(self.c.get('/api/v1/quotes/' + q3['quote_id'], headers=self.H).json()['state'], 'accepted')
        # fixed-price fallback still works, selected explicitly
        q4 = self.quote(dict(inputs, private_label='EXACT'), 'exact')
        self.assertEqual(q4['scheme'], 'exact')
        items = self.c.get('/api/v1/x402/settlements', headers=self.H).json()
        self.assertEqual(len(items['items']), 2); self.assertIn('permit2', items['local_chain'])

    @unittest.skipUnless(HAVE_TORCH, 'no torch')
    def test_generation_settles_below_the_ceiling(self):
        if not installed(self.inst.settings, GEN) or not installed(self.inst.settings, EMB):
            self.skipTest('models absent')
        self.inst.register_defaults()
        gsid = next(s['id'] for s in self.c.get('/api/v1/services', headers=self.H).json()['items'] if s['kind'] == 'text_generation')
        inputs = {'schema': 'text-generation-input/v1', 'messages': [{'role': 'user', 'content': 'Reply with exactly one word: hello'}], 'max_output_tokens': 200}
        q = self.quote(inputs, 'upto', sid=gsid)
        ex = upto_exchange(self.inst, gsid, q['quote_id'], inputs, 'upto-gen-1-' + 'd' * 20)
        self.assertEqual(ex['second_status'], 202, ex)
        jid, pid = ex['body']['job_id'], ex['body']['settlement']['payment_id']
        chain = ex['chain']; before = chain.balances()
        self.assertEqual(self.w.run_once(), (jid, 'succeeded'))
        st = self.c.get('/api/v1/x402/settlements/' + pid, headers=self.H).json()
        view = self.c.get('/api/v1/models/jobs/' + jid, headers=self.H).json()
        self.assertEqual(st['state'], 'SETTLED', st); self.assertLess(int(st['final_amount']), int(st['authorized_max']))
        self.assertEqual(int(st['final_amount']), view['usage']['output_tokens'] * (q['amount_max'] // q['quantity_max']))
        self.assertEqual(before['payer'] - chain.balances()['payer'], int(st['final_amount']))
