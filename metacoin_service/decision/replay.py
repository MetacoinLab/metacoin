"""Offline replay of a decision bundle without the service (Deliverable G / journey 23):

    python -m metacoin_service.decision.replay bundle.zip [--trust-root HEX ...] [--json]

Standard library plus the installed `metacoin_service.decision` package (kernels + checker) and the Ed25519 verifier.
Reports integrity, signatures (under a supplied trust root; the bundle's own key is never a root), the scientific replay
(witnesses / conditioning re-decided by the independent checker from the raw inputs; the kernel recomputation compared
exactly) and the sampling arithmetic of every bound plan — each separately. Exit 0 only when integrity and the
scientific replay hold."""
import argparse
import hashlib
import json
import sys
import zipfile


def _read(path):
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        bad = [n for n in names if n.startswith('/') or '..' in n.split('/')]
        if bad or len(names) > 256:
            raise ValueError('unsafe archive')
        return {n: z.read(n) for n in names}


def replay(path, trust_roots=()):
    from metacoin_service import crypto
    from metacoin_service.decision import checker, kernels as K, operations as ops, sampling
    rep = {'integrity': None, 'signatures': None, 'scientific_replay': None, 'sampling': [], 'ok': False}
    try:
        files = _read(path)
        manifest = json.loads(files['manifest.json']); statement = json.loads(files['statement.json'])
    except Exception as exc:
        rep['integrity'] = {'ok': False, 'error': type(exc).__name__}; return rep
    checks = [{'check': 'schema', 'ok': manifest.get('schema') == 'metacoin-decision-bundle/v1'}]
    for n, d in manifest.get('file_sha256', {}).items():
        checks.append({'check': 'file:' + n, 'ok': n in files and hashlib.sha256(files[n]).hexdigest() == d})
    extra = [n for n in files if n not in manifest.get('file_sha256', {}) and n not in ('manifest.json', 'statement.json')]
    checks.append({'check': 'no_undeclared_members', 'ok': not extra, 'detail': extra[:5]})
    checks.append({'check': 'statement_manifest_digest', 'ok': statement.get('manifest_sha256') == hashlib.sha256(files['manifest.json']).hexdigest()})
    rep['integrity'] = {'ok': all(c['ok'] for c in checks), 'checks': checks}
    # signatures: manifest and every verification statement, under supplied roots only
    roots = {crypto.key_id_for(r): r for r in trust_roots}
    own = statement.get('public_key_hex')
    sig = []
    key = roots.get(statement.get('key_id')) or (own if not roots else None)
    sig.append({'check': 'manifest_signature', 'ok': bool(key and crypto.verify(key, files['manifest.json'], statement.get('signature_hex', ''))), 'under_supplied_root': statement.get('key_id') in roots})
    for n in sorted(files):
        if n.startswith('verifications/'):
            rec = json.loads(files[n]); k = roots.get(rec.get('key_id')) or (rec.get('public_key_hex') if not roots else None)
            from experiments.private_receipts.receipt import canonical
            sig.append({'check': n, 'ok': bool(k and crypto.verify(k, canonical(rec['statement']), rec.get('signature_hex', ''))), 'under_supplied_root': rec.get('key_id') in roots, 'class': rec['statement'].get('class'), 'outcome': rec['statement'].get('outcome')})
    rep['signatures'] = {'consistent': all(s['ok'] for s in sig), 'trusted': bool(roots) and all(s['ok'] and s['under_supplied_root'] for s in sig), 'checks': sig, 'note': 'trusted only under a trust root supplied by the recipient; the bundle\'s own key proves consistency, not provenance'}
    # science
    try:
        inputs = json.loads(files['inputs.json']); result = json.loads(files['result.json']); job = json.loads(files['job.json'])
        kind = job['kind']
        if kind == 'decision_observability':
            chk = checker.check_analysis(inputs, result)
            fresh, outcome, _ = ops.run_analysis(inputs)
            from experiments.private_receipts.receipt import canonical
            same = canonical(fresh) == canonical(result)
            rep['scientific_replay'] = {'kind': kind, 'independent_checker': chk, 'kernel_recomputation_identical': same, 'conclusion': result.get('conclusion'), 'ok': chk['ok'] and same}
        else:
            delivery = json.loads(files['delivery.json']) if 'delivery.json' in files else None
            fresh, outcome, _ = ops.run_acquisition(inputs, delivery)
            from experiments.private_receipts.receipt import canonical
            same = canonical(fresh) == canonical(result)
            cm = inputs['candidate_model']; views = list(cm.get('views', [])) + list(inputs['selected_views'])
            if delivery and delivery.get('status') == 'acquired' and ops.validate_delivery(delivery, inputs['selected_views']) is None:
                chk = checker.check_conditioning(cm['hypotheses'], views, cm['coordinates'], delivery['observations'], (result.get('decision') or {}).get('surviving') or [])
            else:
                chk = {'ok': result.get('outcome') in ('DELIVERY_INVALID', 'ACQUISITION_FAILED'), 'checks': [{'check': 'non_acquired_delivery_outcome', 'ok': result.get('outcome') in ('DELIVERY_INVALID', 'ACQUISITION_FAILED')}], 'checker': checker.CHECKER_ID}
            plan_ok = None
            if 'acquisition-plan.json' in files:
                plan = json.loads(files['acquisition-plan.json'])
                plan_ok = plan.get('plan_digest') == inputs.get('plan_digest')
            rep['scientific_replay'] = {'kind': kind, 'independent_checker': chk, 'kernel_recomputation_identical': same, 'outcome': result.get('outcome'), 'plan_digest_bound': plan_ok, 'ok': chk['ok'] and same and plan_ok is not False}
    except Exception as exc:
        rep['scientific_replay'] = {'ok': False, 'error': type(exc).__name__, 'detail': str(exc)[:200]}
    for n in sorted(files):
        if n.startswith('sampling-plans/'):
            p = json.loads(files[n])
            try:
                recomputed = K.sampling_plan(p['population'], p['minimum_defects'], p['alpha'], p['max_samples'])
                rep['sampling'].append({'plan': n, 'required_samples_recomputed': recomputed['required_samples'], 'stated': p['required_samples'], 'ok': recomputed['required_samples'] == p['required_samples'] and recomputed['feasible'] == p['feasible']})
            except Exception as exc:
                rep['sampling'].append({'plan': n, 'ok': False, 'error': type(exc).__name__})
    for n in sorted(files):
        if n.startswith('verifications/'):
            rec = json.loads(files[n])
            if (rec['statement'] or {}).get('sampling_plan'):
                rep['sampling'].append({'statement': n, 'offline': sampling.Sampling.verify_statement_offline(rec)})
    rep['ok'] = rep['integrity']['ok'] and (rep['scientific_replay'] or {}).get('ok', False) and all(s.get('ok', s.get('offline', {}).get('arithmetic', {}).get('ok', False)) for s in rep['sampling'])
    rep['verdict'] = 'integrity %s; signatures %s; scientific replay %s (independent checker + kernel recomputation); %d sampling record(s)' % ('ok' if rep['integrity']['ok'] else 'FAILED', 'trusted' if rep['signatures']['trusted'] else ('consistent but untrusted (no trust root)' if rep['signatures']['consistent'] else 'FAILED'), 'ok' if (rep['scientific_replay'] or {}).get('ok') else 'FAILED', len(rep['sampling']))
    return rep


def main(argv=None):
    ap = argparse.ArgumentParser(); ap.add_argument('bundle'); ap.add_argument('--trust-root', action='append', default=[]); ap.add_argument('--json', action='store_true')
    a = ap.parse_args(argv)
    rep = replay(a.bundle, a.trust_root)
    if a.json:
        print(json.dumps(rep, indent=1, default=str))
    else:
        print(rep['verdict'])
    return 0 if rep['ok'] else 2


if __name__ == '__main__':
    sys.exit(main())
