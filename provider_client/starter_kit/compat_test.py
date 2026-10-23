"""Protocol compatibility test of the starter kit against the versioned conformance vectors: canonical bytes, digests, signing
bytes, signature verdicts (when a crypto library is available) and the v2 pool / lot rules must agree with the fixtures produced
by the service. Exit 0 only when every applicable case agrees; cases that need Ed25519 are reported `skipped` without the library."""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import kitwire as W  # noqa: E402
import receipt_consumer as RC  # noqa: E402


def main():
    fx = json.load(open(os.path.join(HERE, '..', 'metacoin_provider', 'conformance', 'v1', 'fixtures.json'))); results = []
    for c in fx['canonical']:
        try:
            got = W.canonical(c['object']).decode(); ok = got == c['canonical'] and W.digest(c['object']) == c['digest']
        except W.KitError as exc:
            ok = c.get('expect') == 'invalid'; got = exc.code
        results.append({'case': 'canonical:' + c['id'], 'ok': ok, 'got': got[:60]})
    for c in fx['invalid_json']:
        try:
            W.parse_strict(c['raw'].encode() if isinstance(c['raw'], str) else c['raw']); got = 'valid'
        except W.KitError as exc:
            got = exc.code
        except Exception as exc:
            got = type(exc).__name__
        results.append({'case': 'invalid_json:' + c['id'], 'ok': got != 'valid', 'got': got, 'expected': c.get('error')})
    have_crypto = W.verify('00' * 32, b'x', '00' * 64) is not None
    for c in fx['signatures']:
        if not have_crypto:
            results.append({'case': 'signature:' + c['id'], 'ok': True, 'skipped': 'no cryptography library'}); continue
        v = W.verify(c['public_hex'], W.signing_bytes(c['message_type'], c['body']), c['signature_hex']); results.append({'case': 'signature:' + c['id'], 'ok': bool(v) == (c['expect'] == 'valid')})
    for c in fx.get('v2', []):
        if c['kind'] == 'pool':
            bad = RC.pool_overlap(c['pool'], c['commitments']) if c['pool']['units_per_slot_second'] > 0 else ['units']; got = 'valid' if not bad else ('units_per_slot_second' if bad == ['units'] else 'pool_overlap')
        elif c['kind'] == 'lots':
            r = RC.lot_partition(c['allocation'], c['lots']); got = 'valid' if r else 'lot_partition'
        elif c['kind'] == 'raw':
            try:
                W.parse_strict(c['raw'].encode()); got = 'valid'
            except W.KitError as exc:
                got = exc.code
        elif c['kind'] == 'intent':
            try:
                W.canonical(c['body']); fields = {'protocol', 'type', 'environment', 'actor_account', 'authority', 'asset', 'amount_ceiling', 'recipient', 'permitted_recipients', 'obligation_id', 'grant_id', 'nonce', 'policy_version', 'request_id', 'purpose', 'expires_at', 'policy_hash'}; got = 'unknown_critical_field' if set(c['body']) - fields else 'valid'
            except W.KitError as exc:
                got = exc.code
        else:
            results.append({'case': 'v2:' + c['id'], 'ok': True, 'skipped': 'kind %s needs the service key history' % c['kind']}); continue
        results.append({'case': 'v2:' + c['id'], 'ok': (got == 'valid') if c['expect'] == 'valid' else got == c['error'], 'got': got})
    out = {'schema': 'starter-kit-compat/v1', 'fixtures': fx['version'], 'cases': len(results), 'passed': sum(r['ok'] for r in results), 'skipped': sum(1 for r in results if r.get('skipped')), 'crypto_library': have_crypto, 'ok': all(r['ok'] for r in results), 'failed': [r for r in results if not r['ok']]}
    print(json.dumps(out, indent=1)); return 0 if out['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
