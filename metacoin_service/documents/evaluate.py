"""Document acceptance evaluation (§19): run every fixture through the REAL import path of a temporary instance and
compare with the independent known-answer manifest. Extraction and conversion are scored separately; OCR reports
field matches and a character error rate on the bounded fixture; development and held-out sets are reported apart.

    PYTHONPATH=. .venv-service/bin/python -m metacoin_service.documents.evaluate --out document-extraction-results.json"""
import argparse
import difflib
import json
import time
from pathlib import Path

FX = Path(__file__).resolve().parents[1] / 'tests' / 'document_fixtures'


def cer(expected, got):
    """Character error rate of the expected string against the best matching window of the recognized text (bounded fixture metric)."""
    if not expected:
        return None
    best = None
    for i in range(0, max(1, len(got) - len(expected) + 1)):
        window = got[i:i + len(expected)]
        sm = difflib.SequenceMatcher(None, expected, window)
        errs = sum(max(i2 - i1, j2 - j1) for tag, i1, i2, j1, j2 in sm.get_opcodes() if tag != 'equal')
        best = errs if best is None or errs < best else best
    return round(best / len(expected), 3)


def main():
    ap = argparse.ArgumentParser(); ap.add_argument('--out'); a = ap.parse_args()
    from metacoin_service.tests.test_documents import DocInstance
    man = json.load(open(FX / 'manifest.json'))
    inst = DocInstance(); c = inst.client; H = inst.h('owner'); w = inst.worker()
    cid = c.post('/api/v1/knowledge/collections', headers=H, json={'name': 'eval'}).json()['id']
    report = {'schema': 'metacoin-document-extraction-results/v1', 'fixtures': man['generator'], 'sets': {}}
    try:
        for subset in ('dev', 'heldout'):
            rows = []
            for name, exp in sorted(man[subset].items()):
                data = (FX / subset / name).read_bytes(); t0 = time.time()
                r = c.post('/api/v1/documents/import?name=%s&collection_id=%s' % (name, cid), headers=dict(H, **{'Content-Type': 'application/pdf'}), content=data)
                row = {'fixture': name, 'expected_import': exp.get('expected_import'), 'checks': []}
                if r.status_code != 202:
                    row.update(import_status='refused', http=r.status_code, reason=r.json().get('detail')); row['checks'].append({'check': 'refused_as_expected', 'ok': exp.get('expected_import') == 'failed'}); rows.append(row); continue
                v = r.json(); out = w.run_once(); v = c.get('/api/v1/documents/' + v['id'], headers=H).json(); row['seconds'] = round(time.time() - t0, 2)
                row.update(import_status=v['state'], error=v.get('error'))
                if exp.get('expected_import') == 'failed':
                    ok = v['state'] == 'failed' and (exp.get('reason_contains', '') in json.dumps(v.get('error') or {}).lower())
                    row['checks'].append({'check': 'failed_with_precise_reason', 'ok': ok, 'detail': v.get('error')}); rows.append(row); continue
                row['checks'].append({'check': 'import_state', 'ok': v['state'] == exp['expected_import'], 'detail': v['state']})
                pages = c.get('/api/v1/documents/%s/pages/0' % v['id'], headers=H).json() if v['extraction'] else None
                allpages = [c.get('/api/v1/documents/%s/pages/%d' % (v['id'], i), headers=H).json()['page'] for i in range(v['page_count'] or 0)]
                row['checks'].append({'check': 'page_count', 'ok': v['page_count'] == exp['pages'], 'detail': v['page_count']})
                if exp.get('method'):
                    row['checks'].append({'check': 'methods', 'ok': [p['method'] for p in allpages] == exp['method'], 'detail': [p['method'] for p in allpages]})
                if exp.get('rotation') is not None:
                    row['checks'].append({'check': 'rotation_recorded', 'ok': allpages[0]['rotation'] == exp['rotation'], 'detail': allpages[0]['rotation']})
                for ps in exp.get('passages', []):
                    pg = allpages[ps['page'] - 1]
                    row['checks'].append({'check': 'passage_on_page:' + ps['text'][:30], 'ok': ps['text'] in pg['normalized_text'] or ps['text'] in pg['raw_text'], 'detail': 'page %d' % ps['page']})
                for f in exp.get('fields', []):
                    pg = allpages[f['page'] - 1]; got = pg['normalized_text']
                    row['checks'].append({'check': 'ocr_field:' + f['name'], 'ok': f['value'] in got, 'value': f['value'], 'cer': cer(f['value'], got), 'method': pg['method'], 'mean_score': (pg.get('ocr') or {}).get('mean_score')})
                for tb in exp.get('tables', []):
                    cands = [t for t in v['tables'] if t['page_number'] == tb['page']]
                    ok = bool(cands)
                    detail = {}
                    if cands:
                        t = c.get('/api/v1/documents/tables/' + cands[0]['id'], headers=H).json()
                        detail = {'header': t['rows'][0], 'rows': t['n_rows'] - 1, 'continued': bool(t['continuation'])}
                        ok = t['rows'][0] == tb['header'] and (t['n_rows'] - 1) == tb['rows'] and (bool(t['continuation']) == bool(tb.get('continued')))
                        if tb.get('cell'):
                            cell = tb['cell']; ok = ok and t['rows'][cell['row']][cell['col']] == cell['value']; detail['cell'] = t['rows'][cell['row']][cell['col']] if cell['row'] < len(t['rows']) else None
                    row['checks'].append({'check': 'table_page_%d' % tb['page'], 'ok': ok, 'detail': detail})
                if exp.get('conversion'):
                    tid = v['tables'][0]['id'] if v['tables'] else None
                    if tid:
                        pv = c.post('/api/v1/documents/tables/%s/mappings/preview' % tid, headers=H, json={'mapping': {'schema': 'metacoin-table-mapping/v1', 'target': 'calibration_numeric', 'missing_policy': 'reject', 'columns': [{'source_col': 1, 'field': 'energy', 'unit': 'J'}]}}).json()
                        row['checks'].append({'check': 'conversion_waits_for_locale', 'ok': not pv['valid'] and any('ambiguous' in e.get('reason', '') for e in pv['errors']), 'detail': [e['code'] for e in pv['errors']][:3]})
                rows.append(row)
            passed = sum(1 for r in rows for ch in r['checks'] if ch['ok']); total = sum(len(r['checks']) for r in rows)
            report['sets'][subset] = {'fixtures': len(rows), 'checks_passed': passed, 'checks_total': total, 'rows': rows}
    finally:
        w.offline(); inst.close()
    report['method'] = 'known-answer manifest written by the fixture generator (independent of parser output); extraction checks (page count, method per page, rotation, passages on the expected page), OCR field presence with a bounded character error rate, table header/row/continuation/cell checks and a locale-refusal check; held-out fixtures were never used during development'
    text = json.dumps(report, indent=1)
    if a.out:
        Path(a.out).write_text(text)
    print(json.dumps({k: {kk: vv for kk, vv in v.items() if kk != 'rows'} for k, v in report['sets'].items()}, indent=1))
    for k, v in report['sets'].items():
        for r in v['rows']:
            for ch in r['checks']:
                if not ch['ok']:
                    print('MISS', k, r['fixture'], ch['check'], ch.get('detail'))


if __name__ == '__main__':
    main()
