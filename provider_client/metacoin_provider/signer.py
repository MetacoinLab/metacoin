"""Client of the operator-controlled signer (metacoin-signer/v1): the provider never holds a private key. The signer refuses what
the service would refuse and records exposure durably before returning a signature."""
import json
import urllib.error
import urllib.request

from . import wire


class Signer:
    def __init__(self, base, token_file=None, token=None):
        self.base = base.rstrip('/')
        if token is None:
            with open(token_file) as f:
                token = json.load(f)['token']
        self.token = token

    def call(self, method, path, body=None, raw=None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(self.base + path, data=data, method=method, headers={'Authorization': 'Bearer ' + self.token, 'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read() or b'{}')
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read() or b'{}')
            except ValueError:
                return e.code, {'error': True, 'code': 'HTTP_%d' % e.code}
        except urllib.error.URLError:
            return 0, {'error': True, 'code': 'SIGNER_UNREACHABLE'}

    def health(self):
        return self.call('GET', '/health')

    def enroll(self, name, ceiling):
        return self.call('POST', '/enroll', {'name': name, 'ceiling': ceiling})

    def bind(self, actor, account):
        return self.call('POST', '/bind', {'actor': actor, 'account': account})

    def sign(self, actor, intent, delegate=None, context=None):
        body = {'actor': actor, 'intent': intent}
        if delegate:
            body['delegate'] = delegate
        if context is not None:
            body['context'] = context
        return self.call('POST', '/sign', raw=wire.canonical(body))

    def policy(self, actor):
        return self.call('GET', '/policy/' + actor)

    def set_policy(self, actor, policy):
        return self.call('POST', '/policy', {'actor': actor, 'policy': policy})
