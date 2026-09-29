"""§38 provider history without a magic score (valid negatives, execution failures, incomplete disclosure, new provider) and
§61 cross-instance package import preview (structural completeness vs replayable evidence, trust requirements, method
availability, no accounting or trust effect)."""
import base64
import json
import unittest

from metacoin_service.tests.test_work_evidence import EvidenceBase
from metacoin_service.tests.test_work_terms import TermsInstance, energy_inputs


class ProviderHistoryTests(EvidenceBase):
    def test_history_dimensions_distinguish_negatives_failures_and_disclosure(self):
        # alpha: three accepted valid negatives (INFEASIBLE) + one positive; beta: an execution failure (crash) and nothing else; gamma: new
        for want in ('INFEASIBLE', 'INFEASIBLE', 'INFEASIBLE', 'FEASIBLE'):
            t, f, r, o, a = self.awarded(want); self.run_worker(); self.verify_ms(a['id']); self.decide(a['id'])
        import os
        os.environ['METACOIN_TEST_EXEC_DELAY_SECONDS'] = '5'; self.inst.settings.limits['job_timeout_seconds'] = 1; self.inst.settings.limits['job_max_retries'] = 0
        try:
            t, f, r, o, b = self.awarded('INFEASIBLE', provider='beta'); self.run_worker(2)                  # execution timeout: a failure, not a negative
        finally:
            del os.environ['METACOIN_TEST_EXEC_DELAY_SECONDS']; self.inst.settings.limits['job_timeout_seconds'] = 60; self.inst.settings.limits['job_max_retries'] = 1
        g = self.c.post('/api/v1/work/providers', headers=self.H, json={'name': 'gamma', 'capabilities': {'kinds': ['energy_audit'], 'verification_classes': ['full_exact'], 'payment_schemes': ['exact']}, 'pay_to': 'provider:gamma'}).json()
        ha = self.c.get('/api/v1/work/providers/%s/history' % self.pv['alpha']['id'], headers=self.H); self.assertEqual(ha.status_code, 200, ha.text); ha = ha.json()
        hb = self.c.get('/api/v1/work/providers/%s/history' % self.pv['beta']['id'], headers=self.H).json()
        hg = self.c.get('/api/v1/work/providers/%s/history' % g['id'], headers=self.H).json()
        self.assertEqual(ha['dimensions']['acceptance_outcomes']['accepted_valid_negative'], 3); self.assertEqual(ha['dimensions']['acceptance_outcomes']['accepted_positive'], 1); self.assertEqual(ha['dimensions']['acceptance_outcomes']['rejected'], 0)
        self.assertEqual(ha['dimensions']['execution']['failed'], 0); self.assertEqual(ha['dimensions']['task_families'], {'energy_audit': 4}); self.assertEqual(ha['dimensions']['verification_methods'], {'full_exact': 4})
        self.assertEqual(ha['dimensions']['observed_latency_seconds']['n'], 4); self.assertIn('n', ha['dimensions']['price_behaviour']['action-units'])
        self.assertNotIn('score', json.dumps(ha['dimensions']).lower()); self.assertIn('no single score', ha['reading'][0]); self.assertIn('not a provider failure', ' '.join(ha['reading']))
        # beta: below the cohort, counts are bands; the failure is an execution fact, not a scientific negative
        self.assertEqual(hb['population']['contracts'], '1-2'); self.assertEqual(hb['dimensions']['execution']['failed'], '1-2'); self.assertEqual(hb['dimensions']['acceptance_outcomes']['accepted_valid_negative'], 0)
        self.assertIn('below the minimum cohort', hb['evidence'])
        # gamma: unknown evidence, neither trusted nor suspected
        self.assertEqual(hg['population']['contracts'], 0); self.assertTrue(hg['evidence'].startswith('unknown')); self.assertEqual(hg['dimensions']['acceptance_outcomes']['accepted_positive'], 0)
        # population honesty: the provider's own view names all its contracts; a viewer-role principal sees no awarded contracts
        own = self.c.get('/api/v1/work/providers/%s/history' % self.pv['alpha']['id'], headers=self.pv['alpha']['h']).json(); self.assertIn('own view', own['population']['label'])
        vw = self.c.get('/api/v1/work/providers/%s/history' % self.pv['alpha']['id'], headers=self.inst.h('viewer')).json(); self.assertEqual(vw['population']['contracts'], 0); self.assertIn('only contracts you awarded', vw['population']['label'])
        # disclosed portfolio: provider-selected, labelled incomplete; foreign receipts refused; only the provider curates
        recs = [r for r in self.c.get('/api/v1/work/awards/%s/receipts' % a['id'], headers=self.H).json()['items']]
        alien = self.c.get('/api/v1/work/awards/%s/receipts' % b['id'], headers=self.H).json()['items']
        self.assertEqual(self.c.post('/api/v1/work/providers/%s/portfolio' % self.pv['alpha']['id'], headers=self.H, json={'receipt_ids': [recs[0]['id']]}).status_code, 403)
        bad = self.c.post('/api/v1/work/providers/%s/portfolio' % self.pv['alpha']['id'], headers=self.pv['alpha']['h'], json={'receipt_ids': [alien[0]['id']]}); self.assertEqual(bad.status_code, 403); self.assertEqual(bad.json()['detail']['code'], 'receipt_not_of_this_provider')
        pf = self.c.post('/api/v1/work/providers/%s/portfolio' % self.pv['alpha']['id'], headers=self.pv['alpha']['h'], json={'receipt_ids': [x['id'] for x in recs if x['kind'] in ('verification', 'acceptance')], 'note': 'selected'}); self.assertEqual(pf.status_code, 200, pf.text); pf = pf.json()
        self.assertEqual(pf['completeness']['label'], 'incomplete: the provider chose what to disclose'); self.assertIn('not an audited history', pf['completeness']['note'])
        self.assertTrue(any(i.get('decision') == 'accepted' for i in pf['items']))
        vpf = self.c.get('/api/v1/work/providers/%s/portfolio' % self.pv['alpha']['id'], headers=self.inst.h('viewer')).json(); self.assertEqual(vpf['completeness']['existing_receipts_known_to_this_service'], 'not disclosed to you')
        ha2 = self.c.get('/api/v1/work/providers/%s/history' % self.pv['alpha']['id'], headers=self.H).json(); self.assertEqual(ha2['disclosed_portfolio']['count'], len(pf['items']))


class PackagePreviewTests(EvidenceBase):
    def test_import_preview_in_a_clean_second_instance(self):
        t, f, r, o, a = self.awarded('INFEASIBLE'); self.run_worker(); self.verify_ms(a['id']); self.decide(a['id'])
        full = self.c.get('/api/v1/work/awards/%s/milestones/m1/bundle?scope=full' % a['id'], headers=self.H).content
        restricted = self.c.get('/api/v1/work/awards/%s/milestones/m1/bundle?scope=restricted' % a['id'], headers=self.H).content
        pub = self.c.get('/api/v1/work/awards/%s/receipts' % a['id'], headers=self.H).json()['items'][0]['public_key_hex']
        other = TermsInstance(); self.addCleanup(other.close); oc, oh = other.client, other.h('owner')
        j0 = oc.get('/api/v1/work/journal', headers=oh).json()
        # foreign instance, no trust root: structurally complete, signer unknown, methods installed, replay possible only from the full package
        pv = oc.post('/api/v1/work/packages/import-preview', headers=dict(oh, **{'Content-Type': 'application/zip'}), content=full); self.assertEqual(pv.status_code, 200, pv.text); pv = pv.json()
        self.assertTrue(pv['structural']['complete']); self.assertTrue(pv['structural']['private_disclosures']); self.assertFalse(pv['local_knowledge']['award_known_here'])
        self.assertTrue(all(not x['trusted'] for x in pv['trust_requirements'])); self.assertIn('unknown signer', pv['trust_requirements'][0]['source'])
        self.assertTrue(pv['method_availability']['installed_here']); self.assertTrue(pv['method_availability']['replay_possible_from_package']); self.assertEqual(pv['method_availability']['operation_kind'], 'energy_audit')
        self.assertEqual(pv['effects']['journal_entries_added'], 0); self.assertEqual(pv['effects']['awards_created'], 0)
        # with the sender's key supplied as a trust root the signatures verify; still nothing changes
        pv2 = oc.post('/api/v1/work/packages/import-preview', headers=oh, json={'package_b64': base64.b64encode(restricted).decode(), 'trust_roots': [pub]}).json()
        self.assertTrue(all(x['trusted'] for x in pv2['trust_requirements'])); self.assertTrue(pv2['verifier_report']['signer_trust']['trusted'])
        self.assertFalse(pv2['structural']['private_disclosures']); self.assertFalse(pv2['method_availability']['replay_possible_from_package']); self.assertTrue(pv2['verifier_report']['missing_private_evidence'])
        self.assertEqual(oc.get('/api/v1/work/journal', headers=oh).json(), j0); self.assertEqual(oc.get('/api/v1/work/awards', headers=oh).json()['items'], [])
        self.assertEqual(oc.get('/api/v1/work/keys', headers=oh).json()['keys'].__len__(), 1)                        # the foreign key was not added to the trust history
        # the originating instance recognises its own award; a corrupted archive is rejected before parsing
        home = self.c.post('/api/v1/work/packages/import-preview', headers=dict(self.H, **{'Content-Type': 'application/zip'}), content=full).json(); self.assertTrue(home['local_knowledge']['award_known_here']); self.assertTrue(home['trust_requirements'][0]['trusted'])
        bad = oc.post('/api/v1/work/packages/import-preview', headers=dict(oh, **{'Content-Type': 'application/zip'}), content=full[:-40] + b'x' * 40).json(); self.assertFalse(bad['structural']['ok'])


if __name__ == '__main__':
    unittest.main()
