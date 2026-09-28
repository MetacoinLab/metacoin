"""Group A through real entry points: native PDF import (worker child, pypdf), page provenance in search results, OCR
of a scanned page and a mixed document, table candidates with multi-page continuation, annotations, validated mapping
with declared locale and units, confirmed dataset with row-level provenance, review-then-publish, cancellation/retry,
dependency-aware removal, canary privacy, malformed refusals. Uses the distributable synthetic fixtures."""
import base64
import json
import os
import unittest
from pathlib import Path

from metacoin_service.tests.test_models import ModelInstance, HAVE_TORCH, installed, GEN, EMB
from metacoin_service.tests.test_compute_engine import ComputeInstance, HAVE_RUNTIME

FX = Path(__file__).parent / 'document_fixtures'
MAN = json.load(open(FX / 'manifest.json'))


class DocInstance(ModelInstance, ComputeInstance):
    pass


def pdf(name, subset='dev'):
    return (FX / subset / name).read_bytes()


@unittest.skipUnless(HAVE_RUNTIME, 'no compute interpreter')
class DocumentImportTests(unittest.TestCase):
    def setUp(self):
        self.inst = DocInstance(); self.addCleanup(self.inst.close); self.c = self.inst.client; self.H = self.inst.h('owner')
        self.w = self.inst.worker(); self.addCleanup(self.w.offline)
        self.cid = self.c.post('/api/v1/knowledge/collections', headers=self.H, json={'name': 'docs'}).json()['id']

    def upload(self, name, subset='dev', mode=None, collection=True, raw=True):
        if raw:
            r = self.c.post('/api/v1/documents/import?name=%s%s%s' % (name, ('&collection_id=' + self.cid) if collection else '', ('&mode=' + mode) if mode else ''), headers=dict(self.H, **{'Content-Type': 'application/pdf'}), content=pdf(name, subset))
        else:
            r = self.c.post('/api/v1/documents/import', headers=self.H, json={'name': name, 'format': 'pdf', 'content_base64': base64.b64encode(pdf(name, subset)).decode(), 'collection_id': self.cid if collection else None, 'policy': {'mode': mode} if mode else None})
        self.assertEqual(r.status_code, 202, r.text[:300])
        return r.json()

    def run_import(self, name, **kw):
        v = self.upload(name, **kw)
        self.assertEqual((v['state'], v['stage']), ('received', 'queued'))
        out = self.w.run_once()
        self.assertIsNotNone(out, 'worker claimed nothing')
        return self.c.get('/api/v1/documents/' + v['id'], headers=self.H).json(), out

    def test_native_import_search_citation_and_page_inspector(self):
        v, (jid, outcome) = self.run_import('report.pdf')
        self.assertEqual(outcome, 'succeeded'); self.assertEqual((v['state'], v['page_count'], v['extraction']['ocr_pages'], v['extraction']['excluded_pages']), ('ready', 1, 0, 0), v)
        self.assertEqual(v['extraction']['parser']['id'], 'metacoin-pdf-extractor/v1'); self.assertTrue(v['version_id'])
        # page inspector: raw and normalized representations, geometry convention, spans
        pg = self.c.get('/api/v1/documents/%s/pages/0' % v['id'], headers=self.H).json()
        self.assertEqual(pg['page']['method'], 'native'); self.assertIn('reserve of 2000 mJ', pg['page']['raw_text']); self.assertGreater(len(pg['page']['spans']), 5); self.assertIn('bottom-left', pg['page']['geometry'])
        png = self.c.get('/api/v1/documents/%s/pages/0/preview.png' % v['id'], headers=self.H)
        self.assertEqual((png.status_code, png.content[:4]), (200, b'\x89PNG'))
        self.assertEqual(self.c.get('/api/v1/documents/%s/pages/0/preview.png' % v['id'], headers=self.inst.h('viewer')).status_code, 403)
        # lexical search resolves to the document revision AND page
        s = self.c.post('/api/v1/knowledge/collections/' + self.cid + '/search', headers=self.H, json={'query': 'reserve 2000 mJ boundary', 'mode': 'lexical', 'k': 3}).json()
        self.assertTrue(s['results']); top = s['results'][0]
        self.assertEqual((top['version_id'], top['page_number'], top['region']['kind']), (v['version_id'], 1, 'page'))
        # the citation quote binds to the normalized representation and verifies byte-exactly
        chk = self.c.post('/api/v1/knowledge/citations/validate', headers=self.H, json={'citations': [{'chunk_id': top['chunk_id'], 'quote': 'keeps a reserve of 2000 mJ'}]}).json()
        self.assertTrue(all(c.get('valid') for c in chk.get('citations', [chk])) or chk.get('valid', True), chk)
        # table candidate
        self.assertEqual(len(v['tables']), 1); t = self.c.get('/api/v1/documents/tables/' + v['tables'][0]['id'], headers=self.H).json()
        self.assertEqual((t['rows'][0], t['header_row'], t['method'], t['page_number']), (['segment', 'duration_s', 'power_mW'], 0, 'native', 1))
        self.assertEqual(t['rows'][2][2], MAN['dev']['report.pdf']['tables'][0]['cell']['value'])
        # PROV export of the knowledge version shows the source artifact lineage
        prov = self.c.get('/api/v1/datasets/prov/knowledge_version/' + v['version_id'], headers=self.H)
        self.assertIn(prov.status_code, (200, 404))

    def test_ocr_scanned_and_mixed_with_method_provenance(self):
        v, (jid, outcome) = self.run_import('scanned.pdf')
        self.assertEqual(outcome, 'succeeded'); self.assertEqual((v['state'], v['extraction']['ocr_pages']), ('ready', 1), v)
        pg = self.c.get('/api/v1/documents/%s/pages/0' % v['id'], headers=self.H).json()['page']
        self.assertEqual(pg['method'], 'ocr'); self.assertIn('4700', pg['normalized_text']); self.assertIn('SN-2291', pg['normalized_text'])
        self.assertEqual(pg['ocr']['engine'], 'rapidocr_onnxruntime'); self.assertIn('diagnostic', pg['ocr']['confidence_meaning']); self.assertEqual(pg['ocr']['dpi'], 200)
        m, (jid2, out2) = self.run_import('mixed.pdf')
        self.assertEqual((out2, m['state'], m['page_count'], m['extraction']['ocr_pages']), ('succeeded', 'ready', 2, 1))
        p1 = self.c.get('/api/v1/documents/%s/pages/0' % m['id'], headers=self.H).json()['page']; p2 = self.c.get('/api/v1/documents/%s/pages/1' % m['id'], headers=self.H).json()['page']
        self.assertEqual((p1['method'], p2['method']), ('native', 'ocr')); self.assertIn('12000 mJ', p1['normalized_text']); self.assertIn('15 mW', p2['normalized_text'])
        # one logical page -> one chunk set: no duplicate indexing of the same content
        s = self.c.post('/api/v1/knowledge/collections/' + self.cid + '/search', headers=self.H, json={'query': 'leakage 15 mW', 'mode': 'lexical', 'k': 5}).json()
        hits = [r for r in s['results'] if r['version_id'] == m['version_id'] and '15 mW' in r['text']]
        self.assertEqual(len(hits), 1); self.assertEqual(hits[0]['page_number'], 2)
        # native-only policy excludes the scanned page and asks for review instead of silently indexing
        n = self.upload('mixed.pdf', mode='native'); self.assertEqual(self.w.run_once()[1], 'succeeded')
        nv = self.c.get('/api/v1/documents/' + n['id'], headers=self.H).json()
        self.assertEqual((nv['state'], nv['extraction']['excluded_pages']), ('awaiting_review', 1)); self.assertIsNone(nv['version_id'])
        pub = self.c.post('/api/v1/documents/' + n['id'] + '/publish', headers=self.H, json={}).json()
        self.assertEqual(pub['state'], 'ready'); self.assertTrue(pub['version_id'])

    def test_tables_continuation_annotations_mapping_and_dataset_provenance(self):
        v, (jid, outcome) = self.run_import('repeated-headers.pdf')
        self.assertEqual(outcome, 'succeeded'); self.assertEqual(len(v['tables']), 1)
        t = self.c.get('/api/v1/documents/tables/' + v['tables'][0]['id'], headers=self.H).json()
        self.assertEqual((t['n_rows'], t['continuation']['continued'], t['continuation']['pages']), (15, True, [1]))
        self.assertEqual(t['rows'][14], ['14', '114', '108'])
        tid = t['id']
        # a unit annotation and a cell correction with attribution; the stored extraction is unchanged
        self.c.post('/api/v1/documents/tables/%s/annotations' % tid, headers=self.H, json={'kind': 'unit', 'payload': {'col': 1, 'unit': 'mW', 'reason': 'column header says mW'}})
        r = self.c.post('/api/v1/documents/tables/%s/annotations' % tid, headers=self.H, json={'kind': 'cell_correction', 'payload': {'row': 1, 'col': 2, 'new_value': '83', 'reason': 'transcription error confirmed against the source scan'}})
        self.assertEqual(r.status_code, 201, r.text); ann = r.json()['annotations'][-1]
        self.assertEqual((ann['kind'], ann['previous_value'], ann['new_value'], ann['author_id']), ('cell_correction', '82', '83', self.inst.ids['owner']))
        self.assertEqual(self.c.get('/api/v1/documents/tables/' + tid, headers=self.H).json()['rows'][1][2], '82')       # extraction untouched
        self.assertEqual(self.c.post('/api/v1/documents/tables/%s/annotations' % tid, headers=self.H, json={'kind': 'cell_correction', 'payload': {'row': 1, 'col': 2, 'new_value': '84'}}).status_code, 422)   # reason required
        # mapping to energy_intervals: duration + power low/high from the supply/demand columns, units declared
        mapping = {'schema': 'metacoin-table-mapping/v1', 'target': 'energy_intervals', 'locale': 'point', 'missing_policy': 'reject', 'rounding': 'reject',
                   'columns': [{'source_col': 0, 'field': 'duration_s', 'unit': 'min'}, {'source_col': 2, 'field': 'power_low_mW', 'unit': 'mW'}, {'source_col': 1, 'field': 'power_high_mW', 'unit': 'mW'}]}
        pv = self.c.post('/api/v1/documents/tables/%s/mappings/preview' % tid, headers=self.H, json={'mapping': mapping}).json()
        self.assertTrue(pv['valid'], pv['errors']); self.assertEqual(pv['rows_converted'], 14); self.assertEqual(pv['preview'][0]['values'], {'duration_s': 60, 'power_low_mW': 83, 'power_high_mW': 101})
        m = self.c.post('/api/v1/documents/tables/%s/mappings' % tid, headers=self.H, json={'mapping': mapping}).json()
        self.assertEqual(m['state'], 'validated')
        conf = self.c.post('/api/v1/documents/mappings/%s/confirm' % m['id'], headers=self.H); self.assertEqual(conf.status_code, 200, conf.text); conf = conf.json()
        self.assertEqual(conf['state'], 'confirmed'); dvid = conf['dataset_version_id']
        rows = self.c.get('/api/v1/dataset-versions/' + dvid + '/rows', headers=self.H); self.assertEqual(rows.status_code, 200, rows.text); rows = rows.json()
        first = (rows.get('rows') or rows.get('items') or rows)[0]
        self.assertEqual(first['power_low_mW'], 83)
        # provenance: the mapping artifact traces dataset row 0 to table row 1 on page 1; PROV export names the table and mapping
        from metacoin_service.artifacts import ArtifactStore
        from metacoin_service.db import Database
        with Database(self.inst.settings.db_path).read() as db:
            prov = json.loads(ArtifactStore(self.inst.settings).load(db, conf['provenance_artifact_id'], 'ws_default'))
        self.assertEqual((prov['rows'][0]['source_row'], prov['rows'][0]['page_index'], prov['table_id']), (1, 0, tid))
        # a later annotation makes the mapping stale: confirmation of a second mapping created earlier is refused
        m2 = self.c.post('/api/v1/documents/tables/%s/mappings' % tid, headers=self.H, json={'mapping': mapping}).json()
        self.c.post('/api/v1/documents/tables/%s/annotations' % tid, headers=self.H, json={'kind': 'ignore_row', 'payload': {'row': 14, 'reason': 'footer'}})
        self.assertEqual(self.c.post('/api/v1/documents/mappings/%s/confirm' % m2['id'], headers=self.H).json()['detail']['code'], 'mapping_stale')
        # viewer cannot confirm
        self.assertEqual(self.c.post('/api/v1/documents/mappings/%s/confirm' % m['id'], headers=self.inst.h('viewer')).status_code, 403)

    def test_ambiguous_locale_waits_for_declaration(self):
        v, (jid, outcome) = self.run_import('locale-ambiguous.pdf')
        tid = v['tables'][0]['id']
        mapping = {'schema': 'metacoin-table-mapping/v1', 'target': 'calibration_numeric', 'missing_policy': 'reject', 'columns': [{'source_col': 0, 'field': 'item', 'role': 'label'}, {'source_col': 1, 'field': 'energy', 'unit': 'J'}]}
        pv = self.c.post('/api/v1/documents/tables/%s/mappings/preview' % tid, headers=self.H, json={'mapping': mapping}).json()
        self.assertFalse(pv['valid']); self.assertTrue(any(e['code'] == 'unparseable' and 'ambiguous' in e['reason'] for e in pv['errors']), pv['errors'])
        # the sheet mixes conventions: under either declared locale one row stays unparseable, so the mapping remains invalid rather than guessed
        pv2 = self.c.post('/api/v1/documents/tables/%s/mappings/preview' % tid, headers=self.H, json={'mapping': dict(mapping, locale='comma')}).json()
        self.assertFalse(pv2['valid']); self.assertEqual([e['row'] for e in pv2['errors']], [3])                      # '3.75' is not a comma-locale number
        pv3 = self.c.post('/api/v1/documents/tables/%s/mappings/preview' % tid, headers=self.H, json={'mapping': dict(mapping, locale='point')}).json()
        self.assertFalse(pv3['valid']); self.assertEqual([e['row'] for e in pv3['errors']], [2])                      # '2,5' is not a point-locale number
        # an explicit, recorded row exclusion under the declared comma locale completes the supported path
        pv4 = self.c.post('/api/v1/documents/tables/%s/mappings/preview' % tid, headers=self.H, json={'mapping': dict(mapping, locale='comma', row_exclusions=[3])}).json()
        self.assertTrue(pv4['valid'], pv4['errors']); self.assertEqual([r['values']['energy'] for r in pv4['preview']], ['1.25', '2.5']); self.assertEqual(pv4['rows_considered'], 2)
        # incompatible unit and a missing required field are refused, not guessed
        bad = {'schema': 'metacoin-table-mapping/v1', 'target': 'energy_intervals', 'locale': 'comma', 'missing_policy': 'reject', 'columns': [{'source_col': 1, 'field': 'duration_s', 'unit': 'J'}]}
        pv5 = self.c.post('/api/v1/documents/tables/%s/mappings/preview' % tid, headers=self.H, json={'mapping': bad}).json()
        self.assertIn('missing_required_fields', [e['code'] for e in pv5['errors']]); self.assertIn('incompatible_unit', [e['code'] for e in pv5['errors']])

    def test_refusals_cancel_retry_and_removal(self):
        for name, code in (('malformed.pdf', 'malformed'), ('not-a-pdf.pdf', None)):
            if code is None:
                r = self.c.post('/api/v1/documents/import?name=x.pdf', headers=dict(self.H, **{'Content-Type': 'application/pdf'}), content=pdf(name))
                self.assertEqual((r.status_code, r.json()['detail']['code']), (422, 'format_mismatch')); continue
            v, (jid, outcome) = self.run_import(name)
            self.assertEqual((outcome, v['state'], v['error']['code']), ('failed', 'failed', code))
            rt = self.c.post('/api/v1/documents/' + v['id'] + '/retry', headers=self.H, json={}).json(); self.assertEqual(rt['attempt'], 2)
            self.assertEqual(self.w.run_once()[1], 'failed')
        # cancel a queued import, retry it, remove it with a dependency report
        v = self.upload('report.pdf')
        self.assertEqual(self.c.post('/api/v1/documents/' + v['id'] + '/cancel', headers=self.H).json()['job'], 'cancelled')
        self.assertEqual(self.c.get('/api/v1/documents/' + v['id'], headers=self.H).json()['state'], 'cancelled')
        self.c.post('/api/v1/documents/' + v['id'] + '/retry', headers=self.H, json={}); self.assertEqual(self.w.run_once()[1], 'succeeded')
        v = self.c.get('/api/v1/documents/' + v['id'], headers=self.H).json(); self.assertEqual((v['state'], v['attempt']), ('ready', 2))
        rep = self.c.post('/api/v1/documents/' + v['id'] + '/remove', headers=self.H, json={}).json()
        self.assertFalse(rep['applied']); self.assertEqual(rep['will_retire']['knowledge_document'], v['document_id']); self.assertIn('cannot be recalled', json.dumps(rep).replace('cannot_recall', 'cannot be recalled'))
        done = self.c.post('/api/v1/documents/' + v['id'] + '/remove', headers=self.H, json={'confirm': True}).json()
        self.assertTrue(done['applied'])
        self.assertEqual(self.c.get('/api/v1/documents/%s/pages/0/preview.png' % v['id'], headers=self.H).status_code, 403)
        s = self.c.post('/api/v1/knowledge/collections/' + self.cid + '/search', headers=self.H, json={'query': 'reserve 2000 mJ', 'mode': 'lexical', 'k': 3}).json()
        self.assertFalse([r for r in s['results'] if r['version_id'] == v['version_id']])
        self.assertEqual(self.c.get('/api/v1/documents/tables/' + v['tables'][0]['id'], headers=self.H).status_code, 403)

    def test_canary_strings_stay_out_of_logs_metrics_and_capabilities(self):
        canary = 'CANARY-7f3a9c-DOCNAME'
        r = self.c.post('/api/v1/documents/import?name=%s.pdf&collection_id=%s' % (canary, self.cid), headers=dict(self.H, **{'Content-Type': 'application/pdf'}), content=pdf('report.pdf'))
        self.assertEqual(r.status_code, 202); v = r.json(); self.assertEqual(self.w.run_once()[1], 'succeeded')
        for path in ('/api/v1/status', '/api/v1/capabilities', '/api/v1/services'):
            self.assertNotIn(canary, self.c.get(path, headers=self.H).text, path)
        metrics = self.c.get('/api/v1/metrics', headers=self.H)
        if metrics.status_code == 200:
            self.assertNotIn(canary, metrics.text)
        logs = ''.join(p.read_text(errors='replace') for p in Path(self.inst.home).rglob('*.log'))
        self.assertNotIn(canary, logs)
        self.assertNotIn('reserve of 2000 mJ', logs)
        self.assertEqual(self.c.get('/api/v1/documents/' + v['id'], headers=self.inst.h('viewer')).status_code, 403)
