# EXPERIMENTAL — private test chain only — not the protocol's money layer, which remains [SPEC] and zero-value by MIP-0001/0002; chain-agnostic law unchanged.
"""Local-chain validation of the x402 `upto` scheme: real pinned SDK client/server/facilitator code and real contract
execution on a private py-evm chain. Evidence of LOCAL contract behaviour only; never public-network settlement.

    PYTHONPATH=. .venv-service/bin/python -m unittest integrations.x402.local_chain.test_local_chain -v
    (METACOIN_LOCAL_CHAIN_OUT=path writes the full observed record)"""
import json
import os
import unittest
from pathlib import Path

ART = Path(__file__).parent / 'artifacts.json'


@unittest.skipUnless(ART.exists(), 'local-chain artifacts not built (integrations.x402.local_chain.build)')
class LocalChainUptoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from integrations.x402.local_chain import harness
        cls.r = harness.scenarios(out_path=os.environ.get('METACOIN_LOCAL_CHAIN_OUT'))
        cls.s = cls.r['scenarios']

    def test_below_maximum_settles_exactly_the_final_amount(self):
        s = self.s['below_maximum']
        self.assertEqual(s['stage'], 'settlement-attempted'); self.assertTrue(s['success'], s)
        self.assertEqual((s['authorized_max'], s['final_amount'], s['moved']), ('1000', 640, 640))
        self.assertEqual(s['receipt']['status'], 1); self.assertEqual(s['settle_response']['amount'], '640')
        self.assertEqual(s['balances_after']['recipient'] - s['balances_before']['recipient'], 640)

    def test_over_maximum_is_refused_without_a_transaction(self):
        s = self.s['over_maximum']
        self.assertEqual(s['stage'], 'settlement-attempted'); self.assertFalse(s['success'])
        self.assertEqual(s['error_reason'], 'invalid_upto_evm_payload_settlement_exceeds_amount'); self.assertEqual(s['moved'], 0)

    def test_wrong_recipient_spender_expiry_and_domain_are_refused_before_settlement(self):
        for name, reason in (('wrong_recipient', 'invalid_permit2_recipient_mismatch'), ('wrong_spender', 'invalid_permit2_spender'), ('expired', 'permit2_deadline_expired'), ('wrong_domain', 'invalid_permit2_signature')):
            s = self.s[name]
            self.assertEqual(s['stage'], 'refused-before-settlement', (name, s))
            self.assertEqual(s['verify_error'], reason, (name, s['verify_error']))

    def test_replay_of_a_consumed_nonce_moves_no_funds(self):
        first, replayed = self.s['replay_first'], self.s['replay_same_nonce']
        self.assertTrue(first['success']); self.assertEqual(first['moved'], 500)
        self.assertEqual(replayed['stage'], 'refused-before-settlement'); self.assertEqual(replayed['verify_error'], 'invalid_upto_evm_transaction_failed')   # consumed nonce fails the settle simulation

    def test_lost_response_reconciles_to_the_same_transaction(self):
        s = self.s['response_lost_then_retry']
        self.assertEqual(s['receipt_wait_attempts_during_first'], 2, s)              # injected timeout, then the SDK-level retry reconciled
        self.assertTrue(s['first']['success']); self.assertTrue(s['first']['transaction'].startswith('0x'))
        self.assertGreaterEqual(s['facilitator_settle_calls'], 2)
        self.assertFalse(s['second']['success']); self.assertEqual(s['total_moved'], 700); self.assertEqual(s['moved_after_second'], 0)

    def test_topology_is_labelled_local(self):
        t = self.r['topology']
        self.assertNotIn(t['network'], ('eip155:8453', 'eip155:84532', 'eip155:1')); self.assertIn('not a public network', t['note'])
        self.assertEqual(set(t['contracts']), {'Permit2', 'x402UptoPermit2Proxy', 'MockGenericERC20'})
