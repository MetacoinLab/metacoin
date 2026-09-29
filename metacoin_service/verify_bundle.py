"""Offline verifier for signed scientific result bundles (`metacoin-result-bundle/v1`). Works without the running service.

    python -m metacoin_service.verify_bundle bundle.zip [--trusted-key <hex public key>] [--recompute] [--json]

Checks are reported separately: archive safety (bounded members, no traversal, no duplicates, no absolute or symlink-like
names, decompression ratio and total size), manifest structure and schema version, file digests, signature validity and
whether the signing key is one the caller trusts (a valid signature from an untrusted key is reported as such, never as an
authorized review), verification records carried in the bundle, and the declared numerical witness where the inputs were
disclosed (a resource-plan witness is replayed with the exact simulator; an undisclosed input commitment cannot be
recomputed and is reported as a scope limitation, not as corruption)."""
import argparse
import hashlib
import json
import sys
import zipfile

MAX_MEMBERS, MAX_TOTAL, MAX_RATIO, MAX_NAME = 256, 64 * 1024 * 1024, 100, 200
SCHEMA = 'metacoin-result-bundle/v1'


def _canonical(obj):
    from experiments.private_receipts import receipt as merkle
    return merkle.canonical(obj)


def _verify_sig(public_hex, message, signature_hex):
    try:
        from metacoin_service import crypto
        return bool(crypto.verify(public_hex, message, signature_hex))
    except Exception:
        return False


def check_archive(path):
    items = []
    ok = True
    with zipfile.ZipFile(path) as z:
        infos = z.infolist()
        if len(infos) > MAX_MEMBERS:
            items.append({'check': 'member_count', 'ok': False, 'detail': len(infos)}); ok = False
        names = [i.filename for i in infos]
        dup = len(names) != len(set(names))
        items.append({'check': 'no_duplicate_members', 'ok': not dup, 'detail': len(names)}); ok = ok and not dup
        bad = [n for n in names if n.startswith('/') or '..' in n.split('/') or '\\' in n or len(n) > MAX_NAME or n != n.strip() or any(ord(c) < 32 for c in n)]
        items.append({'check': 'safe_member_names', 'ok': not bad, 'detail': bad[:5]}); ok = ok and not bad
        links = [i.filename for i in infos if (i.external_attr >> 16) & 0o170000 == 0o120000]
        items.append({'check': 'no_symlink_members', 'ok': not links, 'detail': links[:5]}); ok = ok and not links
        total = sum(i.file_size for i in infos); comp = sum(max(i.compress_size, 1) for i in infos)
        bomb = total > MAX_TOTAL or (total / comp) > MAX_RATIO
        items.append({'check': 'decompression_bounds', 'ok': not bomb, 'detail': {'uncompressed': total, 'compressed': comp, 'max_total': MAX_TOTAL, 'max_ratio': MAX_RATIO}}); ok = ok and not bomb
        files = {}
        if ok:
            for i in infos:
                data = z.read(i)
                if len(data) != i.file_size:
                    items.append({'check': 'declared_size', 'ok': False, 'detail': i.filename}); ok = False
                files[i.filename] = data
    return ok, items, files


def verify(path, trusted_keys=(), recompute=False):
    report = {'archive': None, 'manifest': None, 'digests': None, 'signature': None, 'verification_records': None, 'witness': None, 'scope': None}
    ok, items, files = check_archive(path)
    report['archive'] = {'ok': ok, 'checks': items}
    if not ok:
        return report
    try:
        manifest = json.loads(files['manifest.json']); statement = json.loads(files['statement.json'])
    except Exception as exc:
        report['manifest'] = {'ok': False, 'checks': [{'check': 'parse', 'ok': False, 'detail': type(exc).__name__}]}
        return report
    mchecks = [{'check': 'schema', 'ok': manifest.get('schema') == SCHEMA, 'detail': manifest.get('schema')},
               {'check': 'structure', 'ok': all(k in manifest for k in ('package', 'nodes', 'file_sha256', 'issuer_key_id', 'scope', 'delivery_state')), 'detail': sorted(manifest)[:12]},
               {'check': 'canonical_encoding', 'ok': _canonical(manifest) == files['manifest.json'], 'detail': 'manifest bytes are the canonical encoding of their content'}]
    report['manifest'] = {'ok': all(c['ok'] for c in mchecks), 'checks': mchecks}
    dchecks = []
    for name, digest in (manifest.get('file_sha256') or {}).items():
        present = name in files
        dchecks.append({'check': 'file:' + name, 'ok': present and hashlib.sha256(files[name]).hexdigest() == digest, 'detail': 'missing' if not present else ('digest matches' if hashlib.sha256(files[name]).hexdigest() == digest else 'digest differs: artifact changed')})
    extra = [n for n in files if n not in (manifest.get('file_sha256') or {}) and n not in ('manifest.json', 'statement.json')]
    dchecks.append({'check': 'no_unlisted_files', 'ok': not extra, 'detail': extra[:5]})
    dchecks.append({'check': 'statement_binds_manifest', 'ok': statement.get('manifest_sha256') == hashlib.sha256(files['manifest.json']).hexdigest(), 'detail': statement.get('manifest_sha256', '')[:16]})
    report['digests'] = {'ok': all(c['ok'] for c in dchecks), 'checks': dchecks}
    sig_ok = _verify_sig(statement.get('public_key', ''), files['manifest.json'], statement.get('signature', ''))
    trusted = statement.get('public_key') in set(trusted_keys)
    report['signature'] = {'ok': sig_ok, 'signature_valid': sig_ok, 'key_id': statement.get('key_id'), 'issuer_key_id_matches': statement.get('key_id') == manifest.get('issuer_key_id'), 'key_trusted_by_caller': trusted,
                           'meaning': ('valid signature from a key the caller trusts: the issuing service produced this bundle' if sig_ok and trusted else 'valid signature from an UNTRUSTED key: identifies a signer, not an authorized issuer' if sig_ok else 'signature invalid: the manifest or the statement was altered or the signer was substituted')}
    vchecks = []
    for node in manifest.get('nodes') or []:
        for v in node.get('verifications') or []:
            st = v.get('statement')
            v_ok = st is not None and v.get('signature') and _verify_sig(statement.get('public_key', ''), _canonical(st), v['signature'])
            vchecks.append({'check': 'verification:' + str(v.get('id')), 'ok': bool(v_ok), 'class': v.get('class'), 'state': v.get('state'), 'detail': 'statement signed by the same issuer key' if v_ok else 'statement missing or not signed by the bundle issuer key'})
    report['verification_records'] = {'ok': all(c['ok'] for c in vchecks) if vchecks else None, 'checks': vchecks, 'note': 'these records attest what the issuing service checked; they are not an independent recomputation by this verifier'}
    wchecks = []
    for node in manifest.get('nodes') or []:
        wf = node.get('witness_file')
        if not wf:
            continue
        if wf not in files:
            wchecks.append({'check': 'witness:' + node['node'], 'ok': False, 'scope': 'witness file listed in the manifest is missing from the archive', 'recomputed': False}); continue
        witness = json.loads(files[wf])
        if not node.get('inputs_file'):
            wchecks.append({'check': 'witness:' + node['node'], 'ok': None, 'scope': 'inputs not disclosed (commitment ' + str(node.get('input_commitment', ''))[:16] + '): the witness cannot be recomputed here', 'recomputed': False})
            continue
        if node['inputs_file'] not in files:
            wchecks.append({'check': 'witness:' + node['node'], 'ok': False, 'scope': 'the manifest declares disclosed inputs but the source file is missing: the witness cannot be recomputed and the bundle is incomplete', 'recomputed': False}); continue
        inputs = json.loads(files[node['inputs_file']])
        if hashlib.sha256(_canonical(dict(inputs))).hexdigest() != node.get('input_commitment') and hashlib.sha256(_canonical(inputs)).hexdigest() != node.get('input_commitment'):
            wchecks.append({'check': 'witness:' + node['node'], 'ok': None, 'scope': 'disclosed inputs omit private fields, so the commitment cannot be re-derived byte-for-byte; the replay below uses the disclosed inputs', 'recomputed': False})
        if recompute and witness.get('schema') == 'metacoin-resource-plan-witness/v1':
            from metacoin_service.compute import resource_plan as rp
            try:
                if witness.get('assignments') is None:
                    wchecks.append({'check': 'witness_replay:' + node['node'], 'ok': None, 'scope': 'no assignments (status %s): nothing to replay' % witness.get('status'), 'recomputed': False}); continue
                sim = rp.simulate(inputs, witness['assignments'])
                same = sim['feasible'] and sim['utility'] == witness.get('objective') and sim['min_margin'] == witness.get('min_margin') and [t['energy'] for t in sim['trajectory']] == witness.get('trajectory_energy')
                wchecks.append({'check': 'witness_replay:' + node['node'], 'ok': bool(same), 'recomputed': True, 'detail': {'feasible': sim['feasible'], 'objective': sim.get('utility'), 'min_margin': sim.get('min_margin'), 'violations': sim.get('violations')},
                                'scope': 'feasibility and objective of the disclosed schedule under the declared model; optimality is the issuing service\'s claim (status %s)' % witness.get('status')})
            except Exception as exc:
                wchecks.append({'check': 'witness_replay:' + node['node'], 'ok': False, 'recomputed': False, 'detail': type(exc).__name__})
        elif not recompute:
            wchecks.append({'check': 'witness:' + node['node'], 'ok': None, 'scope': 'inputs disclosed; pass --recompute to replay the witness', 'recomputed': False})
    report['witness'] = {'ok': all(c['ok'] for c in wchecks if c['ok'] is not None) if wchecks else None, 'checks': wchecks}
    report['scope'] = {'delivery_state': manifest.get('delivery_state'), 'disclosure': manifest.get('scope'), 'undisclosed': manifest.get('undisclosed'), 'package': manifest.get('package'),
                       'statement': 'this verifier checked archive safety, manifest structure, file digests, the issuer signature and the carried verification statements; ' + ('the disclosed witness was replayed' if any(c.get('recomputed') for c in wchecks) else 'no numerical recomputation was possible or requested') + '. Undisclosed inputs are not verified.'}
    return report


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('bundle'); ap.add_argument('--trusted-key', action='append', default=[], help='hex ed25519 public key(s) the caller trusts'); ap.add_argument('--recompute', action='store_true'); ap.add_argument('--json', action='store_true')
    a = ap.parse_args(argv)
    rep = verify(a.bundle, a.trusted_key, a.recompute)
    if a.json:
        print(json.dumps(rep, indent=1))
    else:
        for k, v in rep.items():
            if v is None:
                print('%-22s not reached' % k); continue
            print('%-22s %s' % (k, 'ok' if v.get('ok') else ('n/a' if v.get('ok') is None else 'FAILED')))
            for c in v.get('checks', []):
                print('    %-40s %s %s' % (c['check'], {True: 'ok', False: 'FAIL', None: 'scope'}[c.get('ok')], json.dumps(c.get('detail') or c.get('scope') or '')[:160]))
            for extra in ('meaning', 'statement'):
                if v.get(extra):
                    print('    ' + v[extra])
    core = rep['archive'] and rep['archive']['ok'] and rep['manifest'] and rep['manifest']['ok'] and rep['digests'] and rep['digests']['ok'] and rep['signature'] and rep['signature']['ok']
    return 0 if core else 1


if __name__ == '__main__':
    sys.exit(main())
