"""Authenticated HTTP client over the public API only (urllib). Credentials come from a private file (0600) and never appear in
arguments, URLs or logs. Mutating calls carry an Idempotency-Key when given so retries never create a second identity."""
import json
import os
import urllib.error
import urllib.request


class ApiError(Exception):
    def __init__(self, status, body):
        super().__init__('HTTP %s: %s' % (status, body.get('code') if isinstance(body, dict) else body)); self.status = status; self.body = body


def load_token(cred_file):
    st = os.stat(cred_file)
    if st.st_mode & 0o077:
        raise SystemExit('credential file %s must be private (0600)' % cred_file)
    with open(cred_file) as f:
        return json.load(f)['token']


LOOPBACK = ('127.0.0.1', 'localhost', '::1', '[::1]')


def transport_check(base, ca_file=None):
    """Authenticated transport outside loopback: https (optionally with a pinned CA) is required; plain http is refused rather than
    tolerated, and certificate validation is never disabled to make a connection work."""
    from urllib.parse import urlparse
    u = urlparse(base)
    if u.scheme == 'https':
        import ssl
        ctx = ssl.create_default_context(cafile=ca_file) if ca_file else ssl.create_default_context(); ctx.check_hostname = True; ctx.verify_mode = ssl.CERT_REQUIRED; return ctx
    if u.hostname in LOOPBACK:
        return None
    raise SystemExit('authenticated transport required outside loopback: %s is plain http to %s; use https (pin the server CA with --ca); certificate validation is never disabled' % (base, u.hostname))


class Api:
    def __init__(self, base, cred_file=None, token=None, timeout=120, ca_file=None):
        self.base = base.rstrip('/'); self.token = token or load_token(cred_file); self.timeout = timeout; self.ssl = transport_check(self.base, ca_file)

    def call(self, method, path, body=None, key=None, raw=None, content_type='application/json', expect=None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        headers = {'Authorization': 'Bearer ' + self.token, 'Content-Type': content_type}
        if key:
            headers['Idempotency-Key'] = key
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self.ssl) as r:
                content = r.read(); status = r.status
        except urllib.error.HTTPError as e:
            content = e.read(); status = e.code
        try:
            out = json.loads(content or b'{}')
        except ValueError:
            out = {'raw': content[:200].decode('utf-8', 'replace')}
        if expect is not None and status not in expect:
            raise ApiError(status, out)
        return status, out

    def get(self, path, expect=(200,)):
        return self.call('GET', path, expect=expect)[1]

    def post(self, path, body=None, key=None, expect=(200, 201), raw=None, content_type='application/json'):
        return self.call('POST', path, body if raw is None else None, key=key, raw=raw, content_type=content_type, expect=expect)[1]
