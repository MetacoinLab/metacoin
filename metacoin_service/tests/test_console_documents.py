"""Console Documents pages through real forms: multipart upload, import detail with page list, page inspector with
preview image and both text representations, table review with an annotation and a mapping preview, confirmation."""
import json
import unittest

from metacoin_service.tests.test_documents import DocInstance, pdf, HAVE_RUNTIME
from metacoin_service.tests.test_console_expansion import login


@unittest.skipUnless(HAVE_RUNTIME, 'no compute interpreter')
class ConsoleDocumentTests(unittest.TestCase):
    def test_upload_inspect_review_and_map(self):
        inst = DocInstance(); self.addCleanup(inst.close); c = inst.client; w = inst.worker(); self.addCleanup(w.offline)
        csrf = login(inst, 'owner')
        cid = c.post('/api/v1/knowledge/collections', headers=inst.h('owner'), json={'name': 'console docs'}).json()['id']
        r = c.post('/console/documents/import', data={'csrf': csrf, 'collection_id': cid, 'mode': 'ocr_needed'}, files={'file': ('repeated-headers.pdf', pdf('repeated-headers.pdf'), 'application/pdf')}, follow_redirects=False)
        self.assertEqual(r.status_code, 303, r.text[-300:]); iid = r.headers['location'].rsplit('/', 1)[-1]
        self.assertEqual(w.run_once()[1], 'succeeded')
        page = c.get('/console/documents/' + iid).text
        self.assertIn('ready', page); self.assertIn('inspect', page); self.assertIn('review', page); self.assertIn('metacoin-pdf-extractor/v1', page)
        insp = c.get('/console/documents/%s/pages/1?q=Slots%%209%%20to%%2014' % iid).text
        self.assertIn('Normalized retrieval text', insp); self.assertIn('Raw parser text', insp); self.assertIn('present in the normalized text', insp); self.assertIn('/pages/1/preview.png', insp)
        self.assertEqual(c.get('/api/v1/documents/%s/pages/1/preview.png' % iid).status_code, 200)
        tid = c.get('/api/v1/documents/' + iid, headers=inst.h('owner')).json()['tables'][0]['id']
        tpage = c.get('/console/documents/tables/' + tid).text
        self.assertIn('header', tpage); self.assertIn('continued on page', tpage); self.assertIn('Map to a dataset', tpage)
        r = c.post('/console/documents/tables/%s/annotations' % tid, data={'csrf': csrf, 'kind': 'unit', 'col': '1', 'value': 'mW', 'reason': 'header'}, follow_redirects=False); self.assertEqual(r.status_code, 303)
        cols = json.dumps([{'source_col': 0, 'field': 'duration_s', 'unit': 'min'}, {'source_col': 2, 'field': 'power_low_mW', 'unit': 'mW'}, {'source_col': 1, 'field': 'power_high_mW'}])
        pv = c.post('/console/documents/tables/%s/mapping' % tid, data={'csrf': csrf, 'target': 'energy_intervals', 'columns': cols, 'locale': 'point', 'missing_policy': 'reject', 'rounding': 'reject', 'row_exclusions': '', 'action': 'preview'})
        self.assertEqual(pv.status_code, 200); self.assertIn('rows convert', pv.text); self.assertIn('valid', pv.text)
        r = c.post('/console/documents/tables/%s/mapping' % tid, data={'csrf': csrf, 'target': 'energy_intervals', 'columns': cols, 'locale': 'point', 'missing_policy': 'reject', 'rounding': 'reject', 'row_exclusions': '', 'action': 'create'}, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        mid = c.get('/console/documents/tables/' + tid).text.split('/console/documents/mappings/')[1].split('/')[0]
        r = c.post('/console/documents/mappings/%s/confirm' % mid, data={'csrf': csrf}, follow_redirects=False); self.assertEqual(r.status_code, 303)
        self.assertIn('confirmed', c.get('/console/documents/tables/' + tid).text)
        # a viewer sees no Documents page
        c.cookies.clear(); login(inst, 'viewer')
        self.assertEqual(c.get('/console/documents').status_code, 403)
