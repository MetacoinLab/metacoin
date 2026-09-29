"""Portable work verification without the live service (Order 08 §32):

    python -m metacoin_service.economy.verify_work bundle.zip [--trust-root <hex ed25519 public key>] [--json]

Runs from an empty directory with only the package and the installed code. It never queries the application database
and never executes bundled scripts: only trusted installed task implementations run. The report separates parsing,
integrity, signer trust, method availability, scientific replay, acceptance evaluation and missing private evidence.
An unavailable implementation is reported as such, never as a signature-only "verified"."""
import argparse
import hashlib
import json
import sys
import zipfile

MAX_MEMBERS, MAX_TOTAL, MAX_RATIO, MAX_NAME = 128, 32 * 1024 * 1024, 100, 200
SCHEMA = 'metacoin-work-bundle/v1'


def _canonical(obj):
    from experiments.private_receipts import receipt as merkle
    return merkle.canonical(obj)


def _archive(path):
    checks, ok, files = [], True, {}
    try:
        z = zipfile.ZipFile(path)
    except Exception as exc:
        return False, [{'check': 'zip_parse', 'ok': False, 'detail': type(exc).__name__}], {}
    with z:
        infos = z.infolist(); names = [i.filename for i in infos]
        checks.append({'check': 'member_count', 'ok': len(infos) <= MAX_MEMBERS, 'detail': len(infos)})
        checks.append({'check': 'no_duplicate_members', 'ok': len(names) == len(set(names)), 'detail': len(names)})
        bad = [n for n in names if n.startswith('/') or '..' in n.split('/') or '\\' in n or len(n) > MAX_NAME or n != n.strip()]
        checks.append({'check': 'safe_member_names', 'ok': not bad, 'detail': bad[:5]})
        links = [i.filename for i in infos if (i.external_attr >> 16) & 0o170000 == 0o120000]
        checks.append({'check': 'no_symlink_members', 'ok': not links, 'detail': links[:5]})
        total = sum(i.file_size for i in infos); comp = sum(max(i.compress_size, 1) for i in infos)
        checks.append({'check': 'decompression_bounds', 'ok': total <= MAX_TOTAL and total / comp <= MAX_RATIO, 'detail': {'uncompressed': total, 'compressed': comp}})
        ok = all(c['ok'] for c in checks)
        if ok:
            for i in infos:
                files[i.filename] = z.read(i)
    return ok, checks, files


def verify(path, trust_root=None, trust_roots=()):
    report = {'parsing': None, 'integrity': None, 'signer_trust': None, 'method_availability': None, 'scientific_replay': None, 'acceptance_evaluation': None, 'missing_private_evidence': None, 'verdict': None}
    ok, checks, files = _archive(path)
    report['parsing'] = {'ok': ok, 'checks': checks}
    if not ok:
        report['verdict'] = 'unverifiable: archive rejected'; return report
    try:
        manifest = json.loads(files['manifest.json']); statement = json.loads(files['statement.json']); terms = json.loads(files['terms.json'])['terms']
    except Exception as exc:
        report['parsing'] = {'ok': False, 'checks': checks + [{'check': 'json', 'ok': False, 'detail': type(exc).__name__}]}; report['verdict'] = 'unverifiable: malformed members'; return report
    pchecks = [{'check': 'schema', 'ok': manifest.get('schema') == SCHEMA, 'detail': manifest.get('schema')}, {'check': 'canonical_manifest', 'ok': _canonical(manifest) == files['manifest.json'], 'detail': None}]
    if manifest.get('schema') != SCHEMA:
        report['parsing']['checks'] += pchecks; report['parsing']['ok'] = False; report['verdict'] = 'unverifiable: unsupported schema version ' + str(manifest.get('schema')); return report
    report['parsing']['checks'] += pchecks
    # integrity: every declared file digest, no undeclared members
    ichecks = []
    for name, digest in manifest['file_sha256'].items():
        ichecks.append({'check': 'file:' + name, 'ok': name in files and hashlib.sha256(files[name]).hexdigest() == digest, 'detail': 'missing' if name not in files else None})
    extra = [n for n in files if n not in manifest['file_sha256'] and n not in ('manifest.json', 'statement.json')]
    ichecks.append({'check': 'no_undeclared_members', 'ok': not extra, 'detail': extra[:5]})
    ichecks.append({'check': 'statement_manifest_digest', 'ok': statement.get('manifest_sha256') == hashlib.sha256(files['manifest.json']).hexdigest(), 'detail': None})
    report['integrity'] = {'ok': all(c['ok'] for c in ichecks), 'checks': ichecks}
    # signer trust: the manifest signature and every receipt/verification statement against the supplied trust root (never a key from the bundle)
    from metacoin_service import crypto
    root = trust_root or None
    roots = {crypto.key_id_for(r): r for r in ([root] if root else []) + list(trust_roots or [])}
    schecks = []
    if root is None:
        schecks.append({'check': 'trust_root_supplied', 'ok': False, 'detail': 'no --trust-root: signatures can be checked for consistency only; the bundle\'s own key is not a trust root'})
        root_for_math = statement.get('public_key')
    else:
        root_for_math = root
        schecks.append({'check': 'trust_root_supplied', 'ok': True, 'detail': crypto.key_id_for(root)})
    sig_ok = crypto.verify(root_for_math, files['manifest.json'], statement.get('signature', '')) if root_for_math else False
    schecks.append({'check': 'manifest_signature', 'ok': bool(sig_ok), 'detail': {'key_id': statement.get('key_id'), 'under_supplied_root': root is not None}})
    if root is not None and statement.get('key_id') != crypto.key_id_for(root):
        schecks.append({'check': 'key_substitution', 'ok': False, 'detail': 'the bundle names a different key than the trust root; refused even if its own signature is mathematically valid'})
    history = {}
    if 'trust-history.json' in files:
        try:
            history = {h['key_id']: h for h in json.loads(files['trust-history.json'])}
        except Exception:
            history = {}
    for name in sorted(files):
        if name.startswith('receipts/') or name.startswith('verifications/'):
            rec = json.loads(files[name])
            msg = _canonical(rec['statement'])
            kid = rec.get('key_id')
            key = roots.get(kid) if roots else (statement.get('public_key') if kid == statement.get('key_id') else None)
            if key is None and roots and kid in history and root is not None:
                # an OLD key named by the bundle's own history: acceptable only when the recipient supplied it as a trust root
                key = None
            issued = rec['statement'].get('issued_at')
            h = history.get(kid)
            in_interval = h is None or (h['valid_from'] <= (issued or 0) and (h['valid_until'] is None or (issued or 0) <= h['valid_until']))
            ok = bool(key and crypto.verify(key, msg, rec['signature_hex']) and in_interval)
            schecks.append({'check': name, 'ok': ok, 'detail': {'kind': rec.get('kind') or rec.get('state'), 'key_id': kid, 'key_supplied_as_root': key is not None, 'issued_within_validity': in_interval,
                                                                'note': None if ok else ('key not among the supplied trust roots (rotation: pass the historical key too)' if key is None else 'signature or validity interval failed')}})
    report['signer_trust'] = {'ok': all(c['ok'] for c in schecks), 'trusted': root is not None and all(c['ok'] for c in schecks), 'checks': schecks}
    # method availability and scientific replay
    kind = terms['operation']['kind']
    avail, replay = {'kind': kind}, {'performed': False}
    summary = json.loads(files['evidence/summary.json']) if 'evidence/summary.json' in files else None
    if kind == 'legacy_task_replay':
        try:
            from metacoin_service.economy import legacy_bridge
            tid = (summary or {}).get('summary', {}).get('task_id') if isinstance((summary or {}).get('summary'), dict) else None
            avail.update(available=tid in legacy_bridge.registry() if tid else False, implementation='demo/tasks registered module', task_id=tid)
            if avail['available']:
                r = legacy_bridge.replay(tid)
                replay = {'performed': True, 'output_hash': r['output_hash'], 'registered_hash': r['registered_hash'], 'matches_registered': r['matches_registered'], 'matches_bundle': r['output_hash'] == summary['summary'].get('output_hash'), 'rule': 'exact canonical hash'}
        except Exception as exc:
            avail.update(available=False, error=type(exc).__name__)
    elif kind == 'energy_audit':
        from experiments.work_contracts import acceptance as v0_acceptance, contract as v0_terms
        avail.update(available=True, implementation='experiments.work_contracts (installed)', verifier_status='current' if v0_terms.verifier_digest() == terms['operation'].get('verifier_digest') else 'different installed bundle')
        if 'private/evidence-vault.json' in files and 'private/input-vault.json' in files and 'private/contract.json' in files:
            try:
                doc = json.loads(files['private/contract.json']); ev = json.loads(files['private/evidence-vault.json']); iv = json.loads(files['private/input-vault.json'])
                audited = v0_acceptance.audit(doc, terms['operation']['contract_digest'], iv, ev)
                replay = {'performed': True, 'scientific_outcome': audited['scientific_outcome'], 'evidence_root': audited['evidence_root'], 'matches_manifest_root': audited['evidence_root'] == manifest.get('evidence_root'), 'scope': 'full private recomputation'}
            except Exception as exc:
                replay = {'performed': True, 'failed': type(exc).__name__, 'detail': str(exc)[:200]}
        else:
            replay = {'performed': False, 'reason': 'private input and evidence vaults not in this package (restricted scope): disclosed fields are membership-checked only'}
    else:
        avail.update(available=False, note='no offline replay implementation for this kind in the portable verifier; acceptance class scope is narrower')
    report['method_availability'] = avail; report['scientific_replay'] = replay
    # disclosed evidence membership against the manifest root (pinned by the recipient through the signed manifest)
    if 'evidence/disclosed.json' in files and manifest.get('evidence_root'):
        from experiments.private_receipts import receipt as merkle
        try:
            opened = merkle.verify(json.loads(files['evidence/disclosed.json']), manifest['evidence_root'])
            replay['disclosed_fields'] = {'membership_verified': True, 'fields': sorted(opened), 'bindings_match_terms': opened.get('contract_digest') == terms['operation'].get('contract_digest') and opened.get('input_root') == terms['operation'].get('input_root')}
        except Exception as exc:
            replay['disclosed_fields'] = {'membership_verified': False, 'error': type(exc).__name__}
    # acceptance evaluation offline: predicates decidable from the package
    pol = terms['acceptance']; trace = []
    decisions = [json.loads(files[n]) for n in sorted(files) if n.startswith('decisions/')]
    current = next((d for d in decisions if d.get('superseded_by') is None), None)
    vrecs = [json.loads(files[n]) for n in sorted(files) if n.startswith('verifications/')]
    for p in pol['predicates']:
        t = p['type']
        if t == 'source_revision':
            r = replay.get('disclosed_fields', {}).get('bindings_match_terms')
            trace.append({'predicate': p['id'], 'result': 'passed' if r else ('failed' if r is False else 'unknown')})
        elif t == 'exact_output_hash':
            trace.append({'predicate': p['id'], 'result': 'passed' if replay.get('output_hash') == p['params']['registered_hash'] else ('failed' if replay.get('performed') else 'unknown')})
        elif t == 'verification_passed':
            good = [v for v in vrecs if v['state'] == 'passed' and v['statement'].get('result_commitment') == manifest.get('evidence_root') and v['statement'].get('class') == p['params']['class']]
            trace.append({'predicate': p['id'], 'result': 'passed' if good and report['signer_trust']['trusted'] else ('unknown' if good else 'unknown'), 'note': None if good and report['signer_trust']['trusted'] else 'verification statements are only evidence under a supplied trust root'})
        elif t == 'outcome_in':
            oc = (summary or {}).get('outcome') if summary else None
            trace.append({'predicate': p['id'], 'result': 'passed' if oc in p['params']['outcomes'] else ('unknown' if oc in (None, 'withheld-by-policy') else 'failed')})
        elif t == 'artifact_complete':
            trace.append({'predicate': p['id'], 'result': 'passed' if manifest.get('evidence_root') else 'failed'})
        elif t == 'schema_valid':
            opened = replay.get('disclosed_fields', {}).get('fields', [])
            if 'model_id' in opened:
                from experiments.private_receipts import receipt as merkle
                vals = merkle.verify(json.loads(files['evidence/disclosed.json']), manifest['evidence_root'])
                trace.append({'predicate': p['id'], 'result': 'passed' if vals.get('model_id') == terms['operation'].get('model_id') else 'failed'})
            else:
                trace.append({'predicate': p['id'], 'result': 'unknown', 'note': 'model identity not disclosed'})
        else:
            trace.append({'predicate': p['id'], 'result': 'unknown', 'note': 'not decidable offline from this package'})
    report['acceptance_evaluation'] = {'trace': trace, 'offline_candidate': 'accepted' if all(x['result'] == 'passed' for x in trace) else ('rejected' if any(x['result'] == 'failed' for x in trace) else 'limited_scope'),
                                       'recorded_decision': current, 'note': 'the recorded decision is the requester\'s; the offline candidate is what THIS package lets a recipient conclude'}
    report['missing_private_evidence'] = manifest.get('undisclosed', [])
    scope_limited = bool(report['missing_private_evidence']) or not replay.get('performed')
    report['verdict'] = ('structurally valid, signatures %s, scientific scope %s' % ('trusted' if report['signer_trust']['trusted'] else 'consistent but untrusted (no trust root)', 'replayed' if replay.get('performed') and not replay.get('failed') else 'limited'))
    report['ok'] = report['parsing']['ok'] and report['integrity']['ok']
    return report


def main(argv=None):
    ap = argparse.ArgumentParser(); ap.add_argument('bundle'); ap.add_argument('--trust-root', action='append', default=[], help='hex ed25519 public key; repeat for historical (rotated) keys'); ap.add_argument('--json', action='store_true')
    a = ap.parse_args(argv)
    rep = verify(a.bundle, a.trust_root[0] if a.trust_root else None, a.trust_root[1:])
    if a.json:
        print(json.dumps(rep, indent=1, default=str))
    else:
        for k in ('parsing', 'integrity', 'signer_trust'):
            print('%-22s %s' % (k, 'ok' if (rep[k] or {}).get('ok') else 'FAILED'))
        print('%-22s %s' % ('method', json.dumps(rep['method_availability'])))
        print('%-22s %s' % ('replay', json.dumps(rep['scientific_replay'])[:300]))
        print('%-22s %s' % ('acceptance', rep['acceptance_evaluation']['offline_candidate'] if rep['acceptance_evaluation'] else None))
        print('%-22s %s' % ('missing private', rep['missing_private_evidence']))
        print('verdict: ' + str(rep['verdict']))
    return 0 if rep.get('ok') else 2


if __name__ == '__main__':
    sys.exit(main())
