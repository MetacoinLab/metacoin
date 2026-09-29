"""§66-1 document revision comparison: page, passage and table-cell differences between two extraction revisions, layout
movement distinguished from content change, and the datasets/analyses affected by content changes."""
import json
import unittest
from metacoin_service.tests.test_documents import DocInstance, pdf, HAVE_RUNTIME


@unittest.skipUnless(HAVE_RUNTIME, 'no compute interpreter')
class DocumentCompareTests(unittest.TestCase):
    def setUp(self):
        self.inst = DocInstance(); self.addCleanup(self.inst.close); self.c = self.inst.client; self.H = self.inst.h('owner')
        self.w = self.inst.worker(); self.addCleanup(self.w.offline)
        self.cid = self.c.post('/api/v1/knowledge/collections', headers=self.H, json={'name': 'cmp'}).json()['id']

    def imp(self, name, subset='dev'):
        r = self.c.post('/api/v1/documents/import?name=%s&collection_id=%s' % (name, self.cid), headers=dict(self.H, **{'Content-Type': 'application/pdf'}), content=pdf(name, subset))
        self.assertEqual(r.status_code, 202, r.text); v = r.json(); self.assertEqual(self.w.run_once()[1], 'succeeded')
        return self.c.get('/api/v1/documents/' + v['id'], headers=self.H).json()

    def test_same_bytes_are_unchanged_and_different_revisions_diff_at_cell_level_with_affected_objects(self):
        a = self.imp('repeated-headers.pdf'); a2 = self.imp('repeated-headers.pdf')
        cmp0 = self.c.get('/api/v1/documents/%s/compare/%s' % (a['id'], a2['id']), headers=self.H).json()
        self.assertTrue(cmp0['same_source_bytes']); self.assertEqual(cmp0['summary']['pages_content_changed'], 0); self.assertEqual(cmp0['summary']['cells_changed'], 0)
        self.assertEqual([p['change'] for p in cmp0['pages']], ['unchanged'] * cmp0['summary']['pages']); self.assertTrue(all(t['change'] == 'unchanged' for t in cmp0['tables']))
        # a mapping on A's table makes A's dataset a dependency; a different revision (the heldout continuation fixture) changes cells
        tid = a['tables'][0]['id']
        mapping = {'schema': 'metacoin-table-mapping/v1', 'target': 'energy_intervals', 'locale': 'point', 'missing_policy': 'reject', 'rounding': 'reject',
                   'columns': [{'source_col': 0, 'field': 'duration_s', 'unit': 'min'}, {'source_col': 2, 'field': 'power_low_mW', 'unit': 'mW'}, {'source_col': 1, 'field': 'power_high_mW', 'unit': 'mW'}]}
        m = self.c.post('/api/v1/documents/tables/%s/mappings' % tid, headers=self.H, json={'mapping': mapping}).json()
        conf = self.c.post('/api/v1/documents/mappings/%s/confirm' % m['id'], headers=self.H).json(); dvid = conf['dataset_version_id']
        an = self.c.post('/api/v1/analyses', headers=self.H, json={'name': 'cmp study', 'blocks': [{'id': 'src', 'type': 'source_note', 'ref_kind': 'knowledge_document_version', 'ref_id': a['version_id']}, {'id': 'data', 'type': 'dataset_ref', 'ref_kind': 'dataset_version', 'ref_id': dvid, 'depends_on': ['src']}]}).json()
        b = self.imp('h3-continued.pdf', subset='heldout')
        cmp1 = self.c.get('/api/v1/documents/%s/compare/%s' % (a['id'], b['id']), headers=self.H).json()
        self.assertFalse(cmp1['same_source_bytes'])
        self.assertGreater(cmp1['summary']['pages_content_changed'] + cmp1['summary']['pages_added_or_removed'], 0)
        self.assertGreater(cmp1['summary']['tables_content_changed'], 0)
        self.assertTrue(cmp1['passages']); self.assertIn('op', cmp1['passages'][0])
        aff = cmp1['affected_by_content_changes']
        self.assertIn(dvid, aff['dataset_versions']); self.assertIn(an['id'], aff['analyses']); self.assertEqual(aff['mappings'][0]['mapping'], m['id'])
        self.assertIn('layout_only', cmp1['interpretation'])
        # console page renders; viewer cannot compare
        s = self.c.post('/api/v1/session', json={'token': self.inst.tok['owner']}); cookies = {'metacoin_session': s.cookies['metacoin_session']}
        page = self.c.get('/console/documents/%s/compare/%s' % (a['id'], b['id']), cookies=cookies)
        self.assertEqual(page.status_code, 200, page.text[:200]); self.assertIn('Revision comparison', page.text); self.assertIn('cells changed', page.text.lower())
        self.assertEqual(self.c.get('/api/v1/documents/%s/compare/%s' % (a['id'], b['id']), headers=self.inst.h('viewer')).status_code, 403)


if __name__ == '__main__':
    unittest.main()
