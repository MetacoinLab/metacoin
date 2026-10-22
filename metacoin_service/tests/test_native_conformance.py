"""Monetary separation and native independence conformance (order §15, §3): no path from work acceptance, evidence quality,
signatures, energy or subjective scoring reaches base issuance; the native package never imports x402 or the facilitator
adapters; identity, supply and monetary files are untouched; the treasury can only spend observed fee revenue; fixture
funding is the only source of development value and is refused outside dev fixtures; audit conservation holds."""
import ast
import hashlib
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
NATIVE = ROOT / 'metacoin_service' / 'native'
FORBIDDEN_IMPORTS = ('x402', 'integrations', 'web3', 'eth_account', 'facilitator', 'metacoin_service.x402_http', 'metacoin_service.buyer', 'metacoin_service.sales')
# the operator-owned identity and monetary surfaces (never edited by a session): digests recorded by the previous delivery
IDENTITY_FILES = ('README.md', 'WHITEPAPER.md', 'TOKENOMICS.md', 'MISSION.md', 'LICENSE.md', 'protocol/ledger_anchor.json', 'protocol/ledger_published.json', 'protocol/identity_text.py')
RECORDED = {'README.md': 'e2f019bfaf04b3718861337c9e532b96aa6f1a316b664727b0fc86170f7e9b0b', 'WHITEPAPER.md': '3ac73c38b2903ae2871adaa4eab31e72562d69dbb40a1c9a6c01775c76093d10', 'TOKENOMICS.md': '9e1fe1a3654de7d47417440b0d16a3af4d08870222b07461680cddaaf55654be',
            'MISSION.md': '34702f6cf2f5c4d00733cb7c132a09fa9a725254e083ca587b5b77edce855159', 'LICENSE.md': 'ff11cf165f5636db77376afea59590c045ab3fadb8e5136d0f9131c1e14624fc', 'protocol/ledger_anchor.json': '1f420ca3f7c103ad268d2b0e2a56ccdd759dc7190b88671469fad77d1ef45202',
            'protocol/ledger_published.json': '57011d96997911fee8988652054088c2dc93bf0edfa032df038398090f758d5b', 'protocol/identity_text.py': '00ec00d11d7cf7b6d0b4fabf9edccb7852227098d25cdb65a9c025ce4acfb660'}


def imports_of(path):
    tree = ast.parse(path.read_text())
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            out.append((('.' * node.level) + (node.module or '')))
    return out


class NativeIndependenceTests(unittest.TestCase):
    def test_native_package_never_imports_x402_or_adapters(self):
        for p in NATIVE.glob('*.py'):
            for imp in imports_of(p):
                for bad in FORBIDDEN_IMPORTS:
                    self.assertFalse(imp == bad or imp.startswith(bad + '.') or imp.endswith('.' + bad.split('.')[-1]) and 'x402' in imp, '%s imports %s' % (p.name, imp))
            text = p.read_text()
            self.assertNotIn('import x402', text); self.assertNotIn('facilitator_url', text)

    def test_core_runs_with_x402_disabled_in_a_fresh_process(self):
        env = dict(os.environ, METACOIN_X402_DISABLED='1', PYTHONPATH=str(ROOT))
        code = ("import sys\nfrom metacoin_service.native import protocol, service, domains, ledger, procurement, checker\n"
                "assert not any(m.startswith('x402') or m.startswith('integrations.x402') for m in sys.modules), [m for m in sys.modules if 'x402' in m]\nprint('ok', protocol.PROTOCOL)")
        r = subprocess.run([sys.executable, '-c', code], env=env, capture_output=True, text=True, cwd=ROOT)
        self.assertEqual(r.returncode, 0, r.stderr[-800:]); self.assertIn('ok metacoin-native/v0', r.stdout)

    def test_identity_and_monetary_files_unchanged(self):
        for f in IDENTITY_FILES:
            p = ROOT / f
            if not p.exists():
                continue
            self.assertEqual(hashlib.sha256(p.read_bytes()).hexdigest(), RECORDED[f], f + ' changed')

    def test_no_issuance_path_in_native_or_economy_sources(self):
        """No function in the native or economy packages mints, issues or credits base supply; the only value sources are the
        development fixture (equity:fixture_funding, dev only) and imported backing claims."""
        sources = list(NATIVE.glob('*.py')) + list((ROOT / 'metacoin_service' / 'economy').glob('*.py')) + list((ROOT / 'metacoin_service' / 'lunar').glob('*.py'))
        for p in sources:
            text = p.read_text()
            for word in ('def mint', 'issue_supply', 'base_issuance(', 'total_supply =', 'supply_schedule'):
                self.assertNotIn(word, text, '%s mentions %s' % (p.name, word))
            if p.name != 'legacy_bridge.py':                                     # the legacy bridge READS the published ledger snapshot (registered hashes); nothing writes it
                self.assertNotIn('ledger_anchor', text, p.name); self.assertNotIn('ledger_published', text, p.name)
            else:
                self.assertNotIn('write', text.split('def ledger_entries')[1].split('INPUT_SCHEMA')[0])
        ledger = (NATIVE / 'ledger.py').read_text()
        self.assertIn("equity:fixture_funding", ledger)
        svc = (NATIVE / 'service.py').read_text()
        self.assertIn("provider_mode == 'production'", svc)                       # fixture funding refuses production explicitly

    def test_fixture_funding_refused_outside_dev_and_by_non_admins_and_conservation_holds(self):
        from metacoin_service.tests.test_native_protocol import NativeBase
        class T(NativeBase):
            def runTest(self):
                pass
        t = T(); t.setUp()
        try:
            c, H = t.c, t.H
            # an actor (earn-and-pay identity) can never fund itself; a viewer cannot fund; a production-labelled asset is refused
            r = c.post('/api/v1/native/fixtures/fund', headers=t.actors['machine']['h'], json={'account_id': t.actors['machine']['account'], 'amount': 5}); self.assertEqual(r.status_code, 403)
            r = c.post('/api/v1/native/fixtures/fund', headers=t.inst.h('viewer'), json={'account_id': t.actors['machine']['account'], 'amount': 5}); self.assertEqual(r.status_code, 403)
            r = c.post('/api/v1/native/fixtures/fund', headers=H, json={'account_id': t.actors['machine']['account'], 'amount': 5, 'asset': 'production:dom-a:ledger-dom-a:META:0'}); self.assertIn(r.status_code, (400, 403, 422, 409))
            t.fund('requester', 30)
            # the treasury account cannot be funded by a fixture top-up into availability for awards beyond fee revenue
            tv = c.get('/api/v1/work/treasury', headers=H, params={'asset': t.asset}).json(); self.assertEqual(tv['available'], 0)
            au = c.get('/api/v1/native/audit', headers=H).json(); self.assertTrue(au['conserved']); self.assertEqual(au['fixture_funding'], 30); self.assertEqual(au['total_held'], 30)
            rep = c.post('/api/v1/work/journal/replay', headers=H, json={}).json(); self.assertTrue(rep['consistent'])
            spec = c.get('/api/v1/native/spec', headers=H).json(); self.assertIn('launched currency', json.dumps(spec['trust_model']))
        finally:
            t.doCleanups()


if __name__ == '__main__':
    unittest.main()
