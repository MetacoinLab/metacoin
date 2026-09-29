"""§65-2 private experiment notebooks: immutable versions, checked links with snapshotted commitments, drift
detection, version comparison, authorized export without payloads, and no executable blocks."""
import json
import unittest

from metacoin_service.db import Database
from metacoin_service.tests.test_compute_engine import ComputeInstance, HAVE_RUNTIME, batch_spec


@unittest.skipUnless(HAVE_RUNTIME, 'no numpy-capable compute interpreter on this host')
class NotebookTests(unittest.TestCase):
    def setUp(self):
        self.inst = ComputeInstance(); self.addCleanup(self.inst.close); self.c = self.inst.client; self.H = self.inst.h('owner')
        self.w = self.inst.worker(); self.addCleanup(self.w.offline)

    def test_versions_links_drift_compare_export(self):
        jid = self.inst.compute_job('temporal_batch', batch_spec(private_label='NB')); self.assertEqual(self.w.run_once()[1], 'succeeded')
        v = self.c.post('/api/v1/verification', headers=self.H, json={'job_id': jid, 'class': 'analytical', 'params': {}}).json(); self.assertEqual(self.w.run_once()[1], 'succeeded')
        with Database(self.inst.settings.db_path).read() as db:
            art = db.execute("SELECT id FROM artifacts WHERE job_id=? AND kind='evidence_vault'", (jid,)).fetchone()['id']
        blocks = [{'id': 'intro', 'type': 'text', 'heading': 'Aim', 'text': 'Reserve sweep on the temporal batch; see the linked run and its analytical audit.'},
                  {'id': 'run', 'type': 'link', 'ref_kind': 'job', 'ref_id': jid, 'label': 'batch run'},
                  {'id': 'audit', 'type': 'link', 'ref_kind': 'verification', 'ref_id': v['id'], 'label': 'analytical audit'},
                  {'id': 'vault', 'type': 'link', 'ref_kind': 'artifact', 'ref_id': art}]
        nb = self.c.post('/api/v1/notebooks', headers=self.H, json={'name': 'sweep notes', 'blocks': blocks, 'note': 'first'})
        self.assertEqual(nb.status_code, 201, nb.text); nb = nb.json()
        self.assertEqual((nb['version'], len(nb['links']), nb['link_drift']), (1, 3, {'run': 'unchanged', 'audit': 'unchanged', 'vault': 'unchanged'}))
        self.assertEqual(nb['links']['run']['commitment_field'], 'evidence_root'); self.assertEqual(len(nb['links']['vault']['commitment']), 64)
        # refusals: executable block types, unknown fields, links outside the workspace / non-existent objects
        for bad in ([{'id': 'x', 'type': 'code', 'text': 'import os'}], [{'id': 'x', 'type': 'text', 'text': 'ok', 'script': 'rm'}], [{'id': 'x', 'type': 'link', 'ref_kind': 'job', 'ref_id': 'job_nope'}]):
            r = self.c.post('/api/v1/notebooks/' + nb['id'] + '/versions', headers=self.H, json={'blocks': bad})
            self.assertIn(r.status_code, (404, 422), r.text)
        self.assertEqual(self.c.post('/api/v1/notebooks/' + nb['id'] + '/versions', headers=self.inst.h('viewer'), json={'blocks': blocks}).status_code, 403)
        # a second version: text changed, one link removed, one added; compare reports block-level differences
        blocks2 = [dict(blocks[0], text=blocks[0]['text'] + ' Revised after the audit passed.'), blocks[1], blocks[2], {'id': 'next', 'type': 'text', 'text': 'Next: sampled audit at 64.'}]
        v2 = self.c.post('/api/v1/notebooks/' + nb['id'] + '/versions', headers=self.H, json={'blocks': blocks2, 'note': 'second'}).json()
        self.assertEqual((v2['version'], len(v2['versions'])), (2, 2))
        cmp = self.c.get('/api/v1/notebooks/%s/compare/1/2' % nb['id'], headers=self.H).json()
        self.assertEqual((cmp['added'], cmp['removed'], cmp['changed'], cmp['identical']), (['next'], ['vault'], ['intro'], False))
        # version 1 is still readable unchanged; drift is reported when a referenced object's commitment changes
        with Database(self.inst.settings.db_path).tx() as db:
            db.execute("UPDATE jobs SET evidence_root=? WHERE id=?", ('f' * 64, jid))
        v1 = self.c.get('/api/v1/notebooks/' + nb['id'] + '?version=1', headers=self.H).json()
        self.assertEqual((v1['digest'], v1['link_drift']['run']), (nb['digest'], 'changed'))
        # export needs the export permission and carries no payloads
        self.assertEqual(self.c.get('/api/v1/notebooks/' + nb['id'] + '/export', headers=self.inst.h('viewer')).status_code, 403)
        ex = self.c.get('/api/v1/notebooks/' + nb['id'] + '/export?version=2', headers=self.H).json()
        self.assertEqual((ex['schema'], ex['version'], ex['digest']), ('metacoin-notebook-export/v1', 2, v2['digest']))
        text = json.dumps(ex)
        self.assertNotIn('NB', text.replace('metacoin-notebook', '')); self.assertNotIn('mck_', text); self.assertNotIn('outcome', text)
        self.assertEqual([b['id'] for b in ex['blocks']], ['intro', 'run', 'audit', 'next'])
        self.assertEqual(self.c.get('/api/v1/notebooks', headers=self.inst.h('viewer')).status_code, 403)          # notebooks are private knowledge
        self.assertEqual(len(self.c.get('/api/v1/notebooks', headers=self.H).json()['items']), 1)
