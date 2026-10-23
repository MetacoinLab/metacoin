"""Versioned conformance fixtures for the portable provider client (group H). Generated from fixed seeds with the service's own
wire implementation and Ed25519 primitive; the client's independent implementation must reproduce every expectation
(canonical digests, signature verdicts, bundle verdicts). Keys here are test-only (derived from a public seed) and never used
by a service.

    python -m metacoin_service.native.conformance_fixtures > provider_client/metacoin_provider/conformance/v1/fixtures.json
"""
import hashlib
import json

from .. import crypto
from . import protocol as P
from .vectors import test_key

VERSION = 'metacoin-provider-conformance/v1'


def _sig(key, mtype, body):
    return crypto.sign(key, P.signing_bytes(mtype, body))


def generate():
    k_auth, pub_auth, kid_auth = test_key('conformance-authority')
    k_actor, pub_actor, kid_actor = test_key('conformance-actor')
    k_other, pub_other, kid_other = test_key('conformance-other')
    canon = [{'id': 'nested_sorted', 'object': {'b': [1, 2, {'z': None, 'a': True}], 'a': 'x', 'n': -5}}, {'id': 'unicode_escaped', 'object': {'s': 'déjà ☃'}}, {'id': 'big_int', 'object': {'v': (1 << 62) - 1}}]
    for c in canon:
        c['canonical'] = P.canonical(c['object']).decode(); c['digest'] = P.digest(c['object'])
    invalid = [{'id': 'float', 'raw': '{"a": 1.5}', 'error': 'float_forbidden'}, {'id': 'duplicate_key', 'raw': '{"a": 1, "a": 2}', 'error': 'duplicate_key'}, {'id': 'nan', 'raw': '{"a": NaN}', 'error': 'constant_forbidden'}]
    st = {'schema': 'metacoin-native-receipt/v0', 'protocol': P.PROTOCOL, 'kind': 'settlement', 'ref_type': 'native_intent', 'ref_id': 'pi_fixture', 'claims': {'from': 'na_a', 'to': 'na_b', 'amount': 7}, 'issued_at': 1700000000, 'nonce': 'abcd'}
    sig = _sig(k_auth, 'receipt', st)
    sigs = [{'id': 'receipt_valid', 'message_type': 'receipt', 'body': st, 'public_hex': pub_auth, 'signature_hex': sig, 'expect': True},
            {'id': 'receipt_wrong_key', 'message_type': 'receipt', 'body': st, 'public_hex': pub_other, 'signature_hex': sig, 'expect': False},
            {'id': 'receipt_altered_claim', 'message_type': 'receipt', 'body': dict(st, claims=dict(st['claims'], amount=8)), 'public_hex': pub_auth, 'signature_hex': sig, 'expect': False},
            {'id': 'receipt_as_intent_type', 'message_type': 'intent', 'body': st, 'public_hex': pub_auth, 'signature_hex': sig, 'expect': False},
            {'id': 'signature_truncated', 'message_type': 'receipt', 'body': st, 'public_hex': pub_auth, 'signature_hex': sig[:-2], 'expect': False},
            {'id': 'public_key_not_on_curve', 'message_type': 'receipt', 'body': st, 'public_hex': 'ff' * 32, 'signature_hex': sig, 'expect': False}]
    intent = {'protocol': P.PROTOCOL, 'type': 'intent', 'environment': 'dev', 'actor_account': 'na_actor', 'authority': 'dom-fx', 'asset': 'dev:dom-fx:native:MU:0', 'amount_ceiling': 7, 'recipient': 'na_b', 'obligation_id': 'ob_1', 'nonce': '1', 'policy_version': P.POLICY_VERSION, 'request_id': 'r1', 'expires_at': 1700000600}
    isig = _sig(k_actor, 'intent', intent)
    sigs += [{'id': 'intent_valid', 'message_type': 'intent', 'body': intent, 'public_hex': pub_actor, 'signature_hex': isig, 'expect': True},
             {'id': 'intent_recipient_altered', 'message_type': 'intent', 'body': dict(intent, recipient='na_c'), 'public_hex': pub_actor, 'signature_hex': isig, 'expect': False}]
    # a minimal mission bundle: one settled intent paying obligation ob_1 (7 units), balanced journal entries, receipts, seal
    entries = [{'id': 'je1', 'event_key': 'commit:pi_1', 'kind': 'native_commit', 'asset': intent['asset'], 'network': 'dom-fx', 'environment': 'dev', 'ref_type': 'native_intent', 'ref_id': 'pi_1', 'memo': '', 'created_at': 1700000001, 'postings': [{'account': intent['asset'] + '@dom-fx/acct:na_actor:reserved', 'debit': 7, 'credit': 0}, {'account': intent['asset'] + '@dom-fx/acct:na_actor:committed_outgoing', 'debit': 0, 'credit': 7}]},
               {'id': 'je2', 'event_key': 'settle:pi_1', 'kind': 'native_settlement', 'asset': intent['asset'], 'network': 'dom-fx', 'environment': 'dev', 'ref_type': 'native_intent', 'ref_id': 'pi_1', 'memo': '', 'created_at': 1700000002, 'postings': [{'account': intent['asset'] + '@dom-fx/acct:na_actor:committed_outgoing', 'debit': 7, 'credit': 0}, {'account': intent['asset'] + '@dom-fx/acct:na_b:available', 'debit': 0, 'credit': 7}]}]
    fund = {'id': 'je0', 'event_key': 'fund', 'kind': 'fixture_funding', 'asset': intent['asset'], 'network': 'dom-fx', 'environment': 'dev', 'ref_type': 'fixture', 'ref_id': 'f', 'memo': '', 'created_at': 1700000000, 'postings': [{'account': intent['asset'] + '@dom-fx/equity:fixture_funding', 'debit': 7, 'credit': 0}, {'account': intent['asset'] + '@dom-fx/acct:na_actor:reserved', 'debit': 0, 'credit': 7}]}
    rec_st = dict(st, ref_id='pi_1', claims={'intent_id': 'pi_1', 'from': 'na_actor', 'to': 'na_b', 'amount': 7, 'obligation_id': 'ob_1'})
    receipts = [{'id': 'nr_1', 'kind': 'settlement', 'ref_type': 'native_intent', 'ref_id': 'pi_1', 'statement': rec_st, 'signature_hex': _sig(k_auth, 'receipt', rec_st), 'key_id': kid_auth}]
    content = {'domain': 'dom-fx', 'environment': 'dev', 'mission': {'id': 'mc_fx', 'frozen_revision': 1, 'owner_account': 'na_actor', 'receipt_ids': ['nr_1']},
               'ledger': {'milestones': [{'key': 'work', 'kind': 'work', 'state': 'settled', 'ceiling': 7, 'reserved': 0, 'spent': 7, 'released': 0, 'obligations': ['ob_1']}], 'totals': {'ceiling': 7, 'reserved': 0, 'spent': 7, 'released': 0}},
               'obligations': [{'id': 'ob_1', 'amount': 7, 'settled_amount': 7, 'state': 'settled'}],
               'intents': [{'id': 'pi_1', 'type': 'transfer', 'actor_account': 'na_actor', 'recipient': 'na_b', 'asset': intent['asset'], 'amount_final': 7, 'state': 'settled', 'commit_sequence': 1, 'obligation_id': 'ob_1', 'message': intent, 'message_digest': P.digest(intent), 'signature_hex': isig, 'signer_public_hex': pub_actor}],
               'receipts': receipts, 'journal_entries': [fund] + entries, 'accounts': [{'id': 'na_actor', 'kind': 'actor', 'label': 'actor', 'key_id': kid_actor, 'public_hex': pub_actor, 'custody': 'external-signer:fx', 'state': 'active'}], 'exported_at': 1700000003}

    def seal(c):
        cd = P.digest(c); s = {'schema': 'metacoin-native-receipt/v0', 'protocol': P.PROTOCOL, 'kind': 'mission_bundle_seal', 'ref_type': 'mission', 'ref_id': 'mc_fx', 'claims': {'content_digest': cd, 'mission_id': 'mc_fx', 'revision': 1}, 'issued_at': 1700000003, 'nonce': 'seal'}
        return {'content_digest': cd, 'statement': s, 'signature_hex': _sig(k_auth, 'receipt', s), 'key_id': kid_auth, 'domain': 'dom-fx'}
    valid = {'schema': 'metacoin-mission-bundle/v1', 'content': content, 'seal': seal(content)}
    roots = {'dom-fx': [{'key_id': kid_auth, 'public_hex': pub_auth, 'epoch': 1}]}

    def tampered(fn):
        b = json.loads(json.dumps(valid)); fn(b); return b
    def t_receipt(b): b['content']['receipts'][0]['statement']['claims']['amount'] = 70
    def t_ledger(b): b['content']['ledger']['milestones'][0]['spent'] = 70; b['content']['ledger']['totals']['spent'] = 70
    def t_journal(b): b['content']['journal_entries'][2]['postings'][1]['credit'] = 70
    def t_intent(b): b['content']['intents'][0]['amount_final'] = 70
    def t_seal_only(b): b['content']['exported_at'] = 1700000099
    bundles = [{'id': 'valid', 'bundle': valid, 'roots': roots, 'expect_ok': True, 'expect_failed_checks': []},
               {'id': 'receipt_claim_altered', 'bundle': tampered(t_receipt), 'roots': roots, 'expect_ok': False, 'expect_failed_checks': ['content_digest', 'receipt:nr_1']},
               {'id': 'ledger_spent_altered', 'bundle': tampered(t_ledger), 'roots': roots, 'expect_ok': False, 'expect_failed_checks': ['content_digest', 'ledger_spent_matches_settled_intents']},
               {'id': 'journal_posting_altered', 'bundle': tampered(t_journal), 'roots': roots, 'expect_ok': False, 'expect_failed_checks': ['content_digest', 'entries_balanced']},
               {'id': 'intent_amount_altered', 'bundle': tampered(t_intent), 'roots': roots, 'expect_ok': False, 'expect_failed_checks': ['content_digest', 'ledger_spent_matches_settled_intents']},
               {'id': 'content_altered_seal_stale', 'bundle': tampered(t_seal_only), 'roots': roots, 'expect_ok': False, 'expect_failed_checks': ['content_digest']},
               {'id': 'no_root_pinned', 'bundle': valid, 'roots': {}, 'expect_ok': False, 'expect_failed_checks': ['seal_root_pinned', 'receipt:nr_1']}]
    # v2 cases (order 2026-10-23 §17): monetary overflow, duplicate keys, unknown critical fields, stale authority epoch, revoked signer, consumed claim parent, malformed resource units
    v2 = []
    big = dict(intent, amount_ceiling=(1 << 63) + 5, request_id='r-big'); v2.append({'id': 'monetary_overflow', 'kind': 'intent', 'body': big, 'expect': 'invalid', 'error': 'integer_out_of_bounds'})
    v2.append({'id': 'duplicate_keys', 'kind': 'raw', 'raw': '{"protocol": "metacoin-native/v0", "protocol": "x"}', 'expect': 'invalid', 'error': 'duplicate_key'})
    v2.append({'id': 'unknown_critical_field', 'kind': 'intent', 'body': dict(intent, settlement_hint='pay-anyway'), 'expect': 'invalid', 'error': 'unknown_critical_field'})
    stale = dict(cert_base := {'protocol': P.PROTOCOL, 'type': 'allocation_certificate', 'allocation_id': 'nal_fx1', 'environment': 'dev', 'source_authority': 'dom-fx', 'source_epoch': 1, 'destination_authority': 'dom-fy', 'asset': intent['asset'], 'amount': 20, 'recipient_account': 'na_b', 'policy_version': P.POLICY_VERSION, 'commit_sequence': 7, 'commit_digest': 'ab' * 32, 'issued_at': 1700000000}, source_epoch=0)
    v2.append({'id': 'stale_authority_epoch', 'kind': 'certificate_epoch', 'body': stale, 'key_epoch': 1, 'expect': 'invalid', 'error': 'epoch_key_mismatch'})
    v2.append({'id': 'revoked_signer', 'kind': 'receipt_key', 'body': st, 'public_hex': pub_auth, 'signature_hex': sig, 'key': {'key_id': kid_auth, 'public_hex': pub_auth, 'epoch': 1, 'retired_at': 1600000000, 'grace_until': 1600000001, 'state': 'revoked'}, 'expect': 'invalid', 'error': 'key_not_valid'})
    lots = [{'lot_id': 'lot_p', 'origin_allocation': 'nal_fx1', 'parent_lot': None, 'asset': intent['asset'], 'amount': 13, 'owner_account': 'na_b', 'status': 'retired', 'transferred': 0, 'created_sequence': 1},
            {'lot_id': 'lot_c2', 'origin_allocation': 'nal_fx1', 'parent_lot': 'lot_p', 'asset': intent['asset'], 'amount': 7, 'owner_account': 'na_c', 'status': 'active', 'transferred': 1, 'created_sequence': 2}]
    bad_lots = [dict(lots[0], status='spent', amount=20), dict(lots[1]), {'lot_id': 'lot_bad', 'origin_allocation': 'nal_fx1', 'parent_lot': 'lot_p', 'asset': intent['asset'], 'amount': 13, 'owner_account': 'na_d', 'status': 'active', 'transferred': 1, 'created_sequence': 3}]
    v2.append({'id': 'consumed_claim_parent', 'kind': 'lots', 'allocation': {'id': 'nal_fx1', 'amount': 20, 'returned_amount': 0}, 'lots': bad_lots, 'expect': 'invalid', 'error': 'lot_partition', 'note': 'a child issued from an already spent 20-unit parent: the lots sum to 40 against an allocation of 20'})
    v2.append({'id': 'valid_lot_partition', 'kind': 'lots', 'allocation': {'id': 'nal_fx1', 'amount': 20, 'returned_amount': 13}, 'lots': lots, 'expect': 'valid', 'note': 'a split reduces the parent in place (13 retired) and issues the remainder (7, transferred)'})
    v2.append({'id': 'malformed_resource_units', 'kind': 'pool', 'pool': {'id': 'rp_x', 'kind': 'compute', 'slot_unit': 'cpu_slot', 'units_per_slot_second': 0, 'resolution_seconds': 1, 'schedule': [{'start': 0, 'end': 60, 'capacity': 1}]}, 'commitments': [], 'expect': 'invalid', 'error': 'units_per_slot_second'})
    v2.append({'id': 'pool_oversold', 'kind': 'pool', 'pool': {'id': 'rp_y', 'kind': 'contact', 'slot_unit': 'channel', 'units_per_slot_second': 1, 'resolution_seconds': 1, 'schedule': [{'start': 0, 'end': 60, 'capacity': 1}]}, 'commitments': [{'id': 'pc1', 'reservation_id': 'rr1', 'start': 0, 'end': 30, 'slots': 1, 'state': 'reserved'}, {'id': 'pc2', 'reservation_id': 'rr2', 'start': 20, 'end': 40, 'slots': 1, 'state': 'active'}], 'expect': 'invalid', 'error': 'pool_overlap'})
    v2.append({'id': 'pool_touching_ok', 'kind': 'pool', 'pool': {'id': 'rp_z', 'kind': 'contact', 'slot_unit': 'channel', 'units_per_slot_second': 1, 'resolution_seconds': 1, 'schedule': [{'start': 0, 'end': 60, 'capacity': 1}]}, 'commitments': [{'id': 'pc1', 'reservation_id': 'rr1', 'start': 0, 'end': 30, 'slots': 1, 'state': 'reserved'}, {'id': 'pc2', 'reservation_id': 'rr2', 'start': 30, 'end': 60, 'slots': 1, 'state': 'reserved'}], 'expect': 'valid'})
    return {'version': 'metacoin-provider-conformance/v2', 'note': 'test-only keys derived from public seeds; the client must reproduce every expectation with its own implementation', 'canonical': canon, 'invalid_json': invalid, 'signatures': sigs, 'bundles': bundles, 'v2': v2,
            'keys': {'authority': {'key_id': kid_auth, 'public_hex': pub_auth}, 'actor': {'key_id': kid_actor, 'public_hex': pub_actor}, 'key_id_rule': 'k_ + sha256(public_bytes)[:24]'}}


if __name__ == '__main__':
    print(json.dumps(generate(), indent=1, sort_keys=True))
