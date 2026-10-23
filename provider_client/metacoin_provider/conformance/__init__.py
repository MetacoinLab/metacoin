"""Run the versioned conformance fixtures against this implementation (wire format and offline verifier)."""
import json
import os

from .. import wire
from .. import verify as V


def load(version='v1'):
    with open(os.path.join(os.path.dirname(__file__), version, 'fixtures.json')) as f:
        return json.load(f)


def run_fixtures(version='v1'):
    fx = load(version); results = []
    for c in fx['canonical']:
        results.append({'case': 'canonical:' + c['id'], 'ok': wire.canonical(c['object']).decode() == c['canonical'] and wire.digest(c['object']) == c['digest']})
    for c in fx['invalid_json']:
        try:
            wire.parse_strict(c['raw']); results.append({'case': 'invalid_json:' + c['id'], 'ok': False, 'detail': 'accepted'})
        except wire.WireError as exc:
            results.append({'case': 'invalid_json:' + c['id'], 'ok': exc.code == c['error'], 'detail': exc.code})
    for s in fx['signatures']:
        try:
            got = wire.verify(s['public_hex'], wire.signing_bytes(s['message_type'], s['body']), s['signature_hex'])
        except wire.WireError:
            got = False
        results.append({'case': 'signature:' + s['id'], 'ok': got == s['expect'], 'detail': {'got': got, 'expect': s['expect']}})
    for b in fx['bundles']:
        rep = V.verify_bundle(b['bundle'], b['roots']); failed = [c['check'] for c in rep['checks'] if not c['ok']]
        ok = rep['ok'] == b['expect_ok'] and all(f in failed for f in b['expect_failed_checks'])
        results.append({'case': 'bundle:' + b['id'], 'ok': ok, 'detail': {'failed_checks': failed, 'expected': b['expect_failed_checks']}})
    k = fx['keys']['authority']; results.append({'case': 'key_id_rule', 'ok': wire.key_id_for(k['public_hex']) == k['key_id']})
    return {'version': fx['version'], 'cases': len(results), 'passed': sum(r['ok'] for r in results), 'ok': all(r['ok'] for r in results), 'results': results}
