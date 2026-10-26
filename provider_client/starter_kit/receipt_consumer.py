"""Receipt consumer written against the export format only: verifies receipts under roots YOU pinned and re-derives pool overlap,
lot partition and obligation totals from the export. Trust roots come from --roots (a file you wrote at enrolment), never from the
export itself.

    python receipt_consumer.py EXPORT.json --roots roots.json [--allow-pure-python]"""
import argparse
import json
import sys

sys.path.insert(0, __import__('os').path.dirname(__file__))
import kitwire as W  # noqa: E402


def pool_overlap(pool, commits):
    pts = sorted({e['start'] for e in pool['schedule']} | {e['end'] for e in pool['schedule']} | {c['start'] for c in commits} | {c['end'] for c in commits}); bad = []
    for a, b in zip(pts, pts[1:]):
        used = sum(c['slots'] for c in commits if c['start'] < b and c['end'] > a and c.get('state') in ('reserved', 'active', 'delivered')); cap = next((e['capacity'] for e in pool['schedule'] if e['start'] <= a < e['end']), 0)
        if used > cap:
            bad.append({'start': a, 'end': b, 'used': used, 'capacity': cap})
    return bad


def lot_partition(alloc, lots):
    mine = [l for l in lots if l['origin_allocation'] == alloc['id'] and l['status'] != 'merged']
    return (sum(l['amount'] for l in mine) == alloc['amount']) if mine else None


def main(argv=None):
    ap = argparse.ArgumentParser(); ap.add_argument('export'); ap.add_argument('--roots', required=True); ap.add_argument('--allow-pure-python', action='store_true'); a = ap.parse_args(argv)
    ex = W.parse_strict(open(a.export, 'rb').read()); roots = json.load(open(a.roots)); keys = {k['key_id']: k for ks in roots.values() for k in ks}; out = {'receipts': [], 'pools': [], 'lots': [], 'obligations': None}
    verify = W.verify
    if a.allow_pure_python:
        try:
            sys.path.insert(0, __import__('os').path.join(__import__('os').path.dirname(__file__), '..'))
            from metacoin_provider import wire as PW
            verify = lambda pub, msg, sig: PW.verify(pub, msg, sig)
        except Exception:
            pass
    for r in ex.get('receipts', []):
        k = keys.get(r.get('key_id')); st = r.get('statement') or {}
        if k is None:
            out['receipts'].append({'id': r.get('id'), 'verdict': 'root_not_pinned'}); continue
        if k.get('state') == 'revoked':
            out['receipts'].append({'id': r.get('id'), 'verdict': 'key_revoked'}); continue
        v = verify(k['public_hex'], W.signing_bytes('receipt', st), r.get('signature_hex', '')); out['receipts'].append({'id': r.get('id'), 'verdict': 'ok' if v else ('unverified_no_crypto_library' if v is None else 'signature_invalid'), 'kind': st.get('kind')})
    by_pool = {}
    for c in ex.get('pool_commitments', []):
        by_pool.setdefault(c['pool_id'], []).append(c)
    for p in ex.get('resource_pools', []):
        bad = pool_overlap(p, by_pool.get(p['id'], [])); out['pools'].append({'pool': p['id'], 'ok': not bad, 'segments': bad[:3]})
    lots = ex.get('claim_lots', [])
    for al in ex.get('allocations', []):
        if al.get('direction') == 'import':
            r_ = lot_partition(al, lots); out['lots'].append({'allocation': al['id'], 'partition_ok': r_})
    obls = ex.get('obligations', []); out['obligations'] = {'open': sum(1 for o in obls if o.get('state') in ('open', 'partial')), 'outstanding_total': sum(o['amount'] - o['settled_amount'] for o in obls if o.get('state') in ('open', 'partial')), 'consistent': all(o['settled_amount'] <= o['amount'] for o in obls)}
    verdicts = [r['verdict'] for r in out['receipts']]; ok = all(v == 'ok' for v in verdicts) and all(p['ok'] for p in out['pools']) and all(l['partition_ok'] in (True, None) for l in out['lots']) and out['obligations']['consistent']
    print(json.dumps({'schema': 'starter-kit-receipt-verdict/v1', 'ok': ok, 'receipts': {'total': len(verdicts), 'ok': verdicts.count('ok'), 'unverified': verdicts.count('unverified_no_crypto_library'), 'other': sorted(set(v for v in verdicts if v not in ('ok', 'unverified_no_crypto_library')))}, 'pools': out['pools'], 'lots_checked': len(out['lots']), 'lots_failed': [l for l in out['lots'] if l['partition_ok'] is False], 'obligations': out['obligations'], 'roots_from': a.roots}, indent=1)); return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
