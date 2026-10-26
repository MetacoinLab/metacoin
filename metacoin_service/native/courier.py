"""Transport adapters for native envelopes. Both drive the receiver's single inbox rule set:

  online : pull a domain's outbox over its authenticated HTTP API, post it to the peer's inbox, carry the replies back,
           and mark delivered items (retries are the sender's queue; the receiver deduplicates by envelope id).
  bundle : write the outbox to a file (carried by hand during an interrupted link) and import such a file at the peer.

No message content is executed; the courier only moves signed bytes. Credentials come from 0600 files, never arguments."""
import argparse
import json
import sys
import urllib.error
import urllib.request

MAX_BUNDLE_BYTES = 1 << 20


class Endpoint:
    def __init__(self, base, token_file):
        self.base = base.rstrip('/')
        self.token = json.load(open(token_file))['token']

    def call(self, method, path, body=None, raw=None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(self.base + path, data=data, method=method, headers={'Authorization': 'Bearer ' + self.token, 'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode() or '{}')


def push(src, dst, receiver, transport='online'):
    """One courier round: src outbox (for receiver) -> dst inbox; replies -> src inbox; delivered items marked on both sides."""
    st, ob = src.call('GET', '/api/v1/native/transport/outbox?receiver=%s&transport=%s' % (receiver, transport))
    if st != 200:
        return {'ok': False, 'stage': 'outbox', 'status': st, 'error': ob}
    if not ob['items']:
        return {'ok': True, 'delivered': 0, 'replies': 0}
    st, res = dst.call('POST', '/api/v1/native/transport/inbox?transport=' + transport, ob)
    if st != 200:
        return {'ok': False, 'stage': 'inbox', 'status': st, 'error': res}
    src.call('POST', '/api/v1/native/transport/outbox/mark', {'results': [{'id': r['id'], 'state': r['state'], 'reason': r.get('reason')} for r in res['results'] if r.get('id') and r['state'] in ('accepted', 'duplicate', 'rejected')]})
    back = None
    if res['replies']:
        st2, back = src.call('POST', '/api/v1/native/transport/inbox?transport=' + transport, {'schema': 'metacoin-native-bundle/v0', 'items': res['replies']})
        if st2 == 200:
            dst.call('POST', '/api/v1/native/transport/outbox/mark', {'results': [{'id': r['id'], 'state': r['state'], 'reason': r.get('reason')} for r in back['results'] if r.get('id') and r['state'] in ('accepted', 'duplicate', 'rejected')]})
    return {'ok': True, 'delivered': len(ob['items']), 'results': res['results'], 'replies': len(res['replies']), 'reply_results': (back or {}).get('results')}


def write_bundle(src, receiver, path):
    st, ob = src.call('GET', '/api/v1/native/transport/outbox?receiver=%s&transport=bundle' % receiver)
    if st != 200:
        return {'ok': False, 'status': st, 'error': ob}
    data = json.dumps(ob, sort_keys=True).encode()
    if len(data) > MAX_BUNDLE_BYTES:
        return {'ok': False, 'error': 'bundle_too_large'}
    with open(path, "wb") as f:
        f.write(data)
    return {'ok': True, 'items': len(ob['items']), 'bytes': len(data), 'path': path}


def import_bundle(dst, path, reply_path=None):
    with open(path, "rb") as f:
        raw = f.read()
    if len(raw) > MAX_BUNDLE_BYTES:
        return {'ok': False, 'error': 'bundle_too_large'}
    st, res = dst.call('POST', '/api/v1/native/transport/inbox?transport=bundle', raw=raw)
    if st != 200:
        return {'ok': False, 'status': st, 'error': res}
    if reply_path and res['replies']:
        with open(reply_path, "w") as f:
            f.write(json.dumps({"schema": "metacoin-native-bundle/v0", "items": res["replies"]}, sort_keys=True))
    return {'ok': True, 'results': res['results'], 'replies': len(res['replies']), 'reply_path': reply_path if res['replies'] else None}


def mark_from_results(src, results):
    return src.call('POST', '/api/v1/native/transport/outbox/mark', {'results': [{'id': r['id'], 'state': r['state'], 'reason': r.get('reason')} for r in results if r.get('id') and r['state'] in ('accepted', 'duplicate', 'rejected')]})[1]


def main(argv=None):
    ap = argparse.ArgumentParser(prog='metacoin-native-courier', description=__doc__.split('\n')[0])
    sub = ap.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('push', help='online round trip between two domains'); p.add_argument('--src'); p.add_argument('--src-token-file'); p.add_argument('--dst'); p.add_argument('--dst-token-file'); p.add_argument('--receiver')
    w = sub.add_parser('write-bundle', help='write a domain outbox for a receiver to a file'); w.add_argument('--src'); w.add_argument('--src-token-file'); w.add_argument('--receiver'); w.add_argument('path')
    i = sub.add_parser('import-bundle', help='import a bundle file at a domain'); i.add_argument('--dst'); i.add_argument('--dst-token-file'); i.add_argument('path'); i.add_argument('--replies')
    a = ap.parse_args(argv)
    if a.cmd == 'push':
        out = push(Endpoint(a.src, a.src_token_file), Endpoint(a.dst, a.dst_token_file), a.receiver)
    elif a.cmd == 'write-bundle':
        out = write_bundle(Endpoint(a.src, a.src_token_file), a.receiver, a.path)
    else:
        out = import_bundle(Endpoint(a.dst, a.dst_token_file), a.path, a.replies)
    print(json.dumps(out, indent=1)); return 0 if out.get('ok') else 1


if __name__ == '__main__':
    sys.exit(main())
