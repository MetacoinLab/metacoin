"""Provider-side operations over the public API: identity through the external signer, resource offers, reservations and
measured deliveries, work offers and awards, signed payments, receipts and offline verification. The compute execution is a
bounded local subprocess chosen by the operator (`executor`), so the provider computes on its own hardware and submits the
result with its CPU time as a provider assertion; the service verifies the result against the public reference."""
import hashlib
import json
import os
import resource
import subprocess
import sys
import time

from .api import Api, ApiError
from .signer import Signer
from . import verify as V


def write_private(path, obj):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(obj, f)


class Provider:
    def __init__(self, base, cred_file=None, token=None, signer=None, signer_token_file=None, actor=None):
        self.api = Api(base, cred_file, token) if (cred_file or token) else None
        self.signer = Signer(signer, signer_token_file) if signer else None
        self.actor = actor

    # ---- identity (operator step: needs an administrator credential on the service) ---------------------------------------------
    @staticmethod
    def enroll(base, admin_cred, signer_base, signer_token_file, name, ceiling, out_cred):
        """Signer generates the key; the service stores the public identity only; the credential lands in a private file."""
        sg = Signer(signer_base, signer_token_file); st, e = sg.enroll(name, ceiling)
        if st != 200:
            raise ApiError(st, e)
        admin = Api(base, admin_cred)
        a = admin.post('/api/v1/native/actors/enroll', {'name': name, 'public_hex': e['public_hex'], 'key_id': e['key_id'], 'signer_id': e['signer_id']}, expect=(201,))
        sg.bind(name, a['account_id'])
        write_private(out_cred, {'token': a['credential']['token'], 'account_id': a['account_id'], 'principal_id': a['principal_id'], 'actor': name, 'key_id': e['key_id']})
        return {'account_id': a['account_id'], 'principal_id': a['principal_id'], 'key_id': e['key_id'], 'custody': a['custody'], 'private_key_held_by_service': a['private_key_held_by_service'], 'credential_file': out_cred}

    @staticmethod
    def register_work_provider(base, admin_cred, principal_id, name, kinds, classes=('analytical', 'full_exact')):
        return Api(base, admin_cred).post('/api/v1/work/providers', {'name': name, 'principal_id': principal_id, 'capabilities': {'kinds': list(kinds), 'verification_classes': list(classes), 'payment_schemes': ['exact']}}, expect=(201,))

    # ---- views ------------------------------------------------------------------------------------------------------------------------
    def account(self):
        return self.api.get('/api/v1/native/accounts/me')

    def market(self):
        return self.api.get('/api/v1/resources')

    def offers(self, kind=None):
        return self.api.get('/api/v1/resources/offers' + ('?kind=' + kind if kind else ''))

    def reservations(self):
        return self.api.get('/api/v1/resources/reservations')

    def reservation(self, rid):
        return self.api.get('/api/v1/resources/reservations/' + rid)

    # ---- offers and deliveries ---------------------------------------------------------------------------------------------------------
    def publish_offer(self, kind, resource_id, capacity, price_per_unit, window_start, window_end, key=None, **extra):
        return self.api.post('/api/v1/resources/offers', dict({'kind': kind, 'resource_id': resource_id, 'capacity': capacity, 'price_per_unit': price_per_unit, 'window_start': window_start, 'window_end': window_end}, **extra), key=key, expect=(201,))

    def activate(self, rid, key=None):
        return self.api.post('/api/v1/resources/reservations/%s/activate' % rid, {}, key=key)

    def execute(self, task, executor, cpu_limit_s=30):
        """Bounded local execution: `executor` is an operator-chosen command that reads the task JSON file path and prints the
        result JSON (e.g. "<python> -m metacoin_service.lunar terrain"). CPU time is the OS rusage delta of the child."""
        import tempfile
        tmp = tempfile.mkdtemp(); path = os.path.join(tmp, 'task.json')
        with open(path, 'w') as f:
            json.dump(task, f)
        before = resource.getrusage(resource.RUSAGE_CHILDREN); t0 = time.perf_counter()
        p = subprocess.run(executor + [path], capture_output=True, text=True, timeout=cpu_limit_s + 60)
        after = resource.getrusage(resource.RUSAGE_CHILDREN)
        cpu = (after.ru_utime - before.ru_utime) + (after.ru_stime - before.ru_stime)
        out = {'cpu_ms': max(1, int(cpu * 1000)), 'wall_ms': int((time.perf_counter() - t0) * 1000), 'exit_code': p.returncode, 'measurement_source': 'os rusage of the executor child on the provider host (provider assertion when reported to the service)'}
        if p.returncode == 0:
            result = json.loads(p.stdout); result.pop('preview_png_base64', None); out['output'] = result; out['output_digest'] = hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()
        else:
            out['stderr'] = p.stderr[-2000:]
        return out

    def deliver_compute(self, rid, task, executor, key=None):
        ex = self.execute(task, executor)
        if ex['exit_code'] != 0:
            raise RuntimeError('executor failed: ' + ex.get('stderr', ''))
        r = self.api.post('/api/v1/resources/reservations/%s/deliver' % rid, {'executed_by': 'provider', 'task': task, 'cpu_ms': ex['cpu_ms'], 'wall_ms': ex['wall_ms'], 'output': ex['output'], 'output_digest': ex['output_digest']}, key=key)
        return {'execution': {k: ex[k] for k in ('cpu_ms', 'wall_ms', 'output_digest', 'measurement_source')}, 'reservation': r}

    def deliver_contact(self, rid, nbytes, key=None):
        return self.api.post('/api/v1/resources/reservations/%s/deliver' % rid, {'bytes': nbytes}, key=key)

    def deliver_storage(self, rid, key=None):
        return self.api.post('/api/v1/resources/reservations/%s/deliver' % rid, {}, key=key)

    def settle(self, rid, key=None):
        return self.api.post('/api/v1/resources/reservations/%s/settle' % rid, {}, key=key)

    # ---- work (offer -> award -> receipt) ---------------------------------------------------------------------------------------
    def open_requests(self):
        return self.api.get('/api/v1/work/requests?state=open')

    def work_offer(self, request_id, price, asset, cls='analytical', window=3600, key=None):
        return self.api.post('/api/v1/work/requests/%s/offers' % request_id, {'price_amount': price, 'asset': asset, 'scheme': 'exact', 'window_seconds': window, 'verification': {'class': cls}}, key=key, expect=(201,))

    def awards(self):
        return self.api.get('/api/v1/work/awards')

    def award(self, aid):
        return self.api.get('/api/v1/work/awards/' + aid)

    def award_receipts(self, aid):
        return self.api.get('/api/v1/work/awards/%s/receipts' % aid)

    # ---- signed payments through the signer ------------------------------------------------------------------------------------
    def pay(self, to_account, amount, request_id, obligation_id=None, asset=None, key=None):
        if self.signer is None or not self.actor:
            raise RuntimeError('a signer and an actor name are required for signed payments')
        body = {'to_account': to_account, 'amount': amount, 'request_id': request_id}
        if obligation_id:
            body['obligation_id'] = obligation_id
        if asset:
            body['asset'] = asset
        msg = self.api.post('/api/v1/native/transfers/prepare', body)['message']
        st, sig = self.signer.sign(self.actor, msg)
        if st != 200:
            raise ApiError(st, sig)
        return self.api.post('/api/v1/native/transfers/submit', {'message': msg, 'signature_hex': sig['signature_hex'], 'key_id': sig['key_id']}, key=key or request_id, expect=(201,))

    # ---- actor-side signing agent (the service never signs for this account) ------------------------------------------------------
    def signing_pending(self):
        return self.api.get('/api/v1/native/signing/pending')

    def sign_pending(self):
        """Fetch pending signing requests of this account, let the signer check context and intent, submit each signature."""
        if self.signer is None or not self.actor:
            raise RuntimeError('a signer and an actor name are required')
        pend = self.signing_pending(); out = []
        for req in pend['items']:
            st, sig = self.signer.sign(self.actor, req['message'], context=req['context'])
            if st != 200:
                out.append({'request': req['id'], 'kind': req['kind'], 'signer_refusal': sig}); continue
            r = self.api.post('/api/v1/native/signing/requests/%s/submit' % req['id'], {'signature_hex': sig['signature_hex'], 'key_id': sig['key_id']}, key=req['id'], expect=(200, 201, 409, 410))
            out.append({'request': req['id'], 'kind': req['kind'], 'state': r.get('state'), 'result_state': (r.get('result') or {}).get('state')})
        return {'account': pend.get('account'), 'processed': out}

    # ---- receipts and offline verification ---------------------------------------------------------------------------------------
    def export(self):
        return self.api.get('/api/v1/native/export')

    def roots_from_domain(self):
        """Roots read over the authenticated channel at enrolment time; pin them to a file and reuse them, do not refetch per verification."""
        d = self.api.get('/api/v1/native/domain'); a = d['authority']
        return {d['id']: [{'key_id': a['key_id'], 'public_hex': a['public_hex'], 'epoch': a['epoch']}]}

    def my_receipts(self, export=None):
        ex = export or self.export(); me = self.account()['id']
        mine = {i['id'] for i in ex['intents'] if me in (i.get('actor_account'), i.get('recipient'))}
        return [r for r in ex['receipts'] if r.get('ref_id') in mine or (r.get('statement') or {}).get('claims', {}).get('to') == me or (r.get('statement') or {}).get('claims', {}).get('from') == me]

    def mission_bundle(self, mid):
        return self.api.get('/api/v1/missions/%s/bundle' % mid)

    @staticmethod
    def verify_offline(record, roots):
        if isinstance(record, dict) and record.get('schema') == 'metacoin-mission-bundle/v1':
            return V.verify_bundle(record, roots)
        return V.verify_export(record, roots)
