"""Deterministic conformance vectors (order §18): valid messages signed with fixed test keys (derived from a seed; never
service keys) and targeted invalid variants: changed recipient, amount, asset, domain, epoch, acceptance policy, duplicate
identifier, mismatched output digest, conflicting allocation. `generate()` returns the vector set; `run()` checks each
through the independent checker and reports the expected refusal codes."""
import hashlib
import json

from .. import crypto
from . import checker, protocol as P

ENV = 'dev'
SEED = b'metacoin-native-vectors/v0'


def test_key(name):
    """Ed25519 key from a seed (test vectors only). Returns (private_key, public_hex, key_id)."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519
    raw = hashlib.sha256(SEED + b'\0' + name.encode()).digest()
    k = ed25519.Ed25519PrivateKey.from_private_bytes(raw)
    pub = k.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
    return k, pub, crypto.key_id_for(pub)


def _item(sender, receiver, mtype, payload, key, kid, eid, created=1_800_000_000, causality=None, expires=4_102_444_800):
    env = {'protocol': P.PROTOCOL, 'type': 'envelope', 'id': eid, 'environment': ENV, 'sender': sender, 'receiver': receiver, 'message_type': mtype, 'causality': causality, 'payload_digest': P.digest(payload), 'expires_at': expires, 'created_at': created}
    return {'envelope': env, 'envelope_signature': crypto.sign(key, P.signing_bytes('envelope', env)), 'key_id': kid, 'payload': payload, 'payload_signature': crypto.sign(key, P.signing_bytes(mtype, payload))}


def generate():
    ka, puba, kida = test_key('dom-a'); kb, pubb, kidb = test_key('dom-b')
    roots = {'dom-a': [{'key_id': kida, 'public_hex': puba, 'epoch': 1}], 'dom-b': [{'key_id': kidb, 'public_hex': pubb, 'epoch': 1}]}
    asset = 'dev:dom-a:ledger-dom-a:TMETA:0'
    cert = {'protocol': P.PROTOCOL, 'type': 'allocation_certificate', 'allocation_id': 'nal_vector0001', 'environment': ENV, 'source_authority': 'dom-a', 'source_epoch': 1, 'destination_authority': 'dom-b', 'asset': asset, 'amount': 20, 'recipient_account': 'na_bob', 'policy_version': P.POLICY_VERSION, 'commit_sequence': 7, 'commit_digest': P.digest({'v': 1}), 'issued_at': 1_800_000_000}
    valid = _item('dom-a', 'dom-b', 'allocation_certificate', cert, ka, kida, 'nenv_vector0001', causality=cert['allocation_id'])
    ack = {'protocol': P.PROTOCOL, 'type': 'import_ack', 'allocation_id': cert['allocation_id'], 'environment': ENV, 'source_authority': 'dom-a', 'destination_authority': 'dom-b', 'import_sequence': 3, 'imported_at': 1_800_000_100}
    valid_ack = _item('dom-b', 'dom-a', 'import_ack', ack, kb, kidb, 'nenv_vector0002', causality=cert['allocation_id'])
    vectors = [{'id': 'valid_certificate', 'expect': 'valid', 'item': valid}, {'id': 'valid_ack', 'expect': 'valid', 'item': valid_ack}]
    def tamper(vid, expect_fail, **changes):
        it = json.loads(json.dumps(valid)); it['payload'].update(changes); vectors.append({'id': vid, 'expect': 'invalid', 'expected_failed_checks': expect_fail, 'item': it})
    tamper('changed_recipient', ['payload_digest', 'payload_signature'], recipient_account='na_mallory')
    tamper('changed_amount', ['payload_digest', 'payload_signature'], amount=21)
    tamper('changed_asset', ['payload_digest', 'payload_signature'], asset='dev:dom-a:ledger-dom-a:OTHER:0')
    tamper('changed_domain', ['payload_digest', 'payload_signature'], destination_authority='dom-c')
    tamper('changed_epoch', ['payload_digest', 'payload_signature'], source_epoch=2)
    tamper('changed_policy', ['payload_digest', 'payload_signature'], policy_version='native-policy/v9')
    # re-signed variants (attacker holds no root key: signs with a fresh key) -> root not pinned
    kx, pubx, kidx = test_key('mallory')
    resigned = _item('dom-a', 'dom-b', 'allocation_certificate', dict(cert, recipient_account='na_mallory'), kx, kidx, 'nenv_vector0003', causality=cert['allocation_id'])
    vectors.append({'id': 'resigned_by_unpinned_key', 'expect': 'invalid', 'expected_failed_checks': ['sender_root_pinned'], 'item': resigned})
    # correctly signed but wrong epoch for the key (new epoch claimed with the epoch-1 key)
    wrong_epoch = _item('dom-a', 'dom-b', 'allocation_certificate', dict(cert, source_epoch=2), ka, kida, 'nenv_vector0004', causality=cert['allocation_id'])
    vectors.append({'id': 'epoch_key_mismatch_signed', 'expect': 'invalid', 'expected_failed_checks': ['certificate_sender_binding'], 'item': wrong_epoch})
    # certificate addressed to dom-c but carried in an envelope to dom-b (binding mismatch, both signed)
    wrong_dest = _item('dom-a', 'dom-b', 'allocation_certificate', dict(cert, destination_authority='dom-c'), ka, kida, 'nenv_vector0005', causality=cert['allocation_id'])
    vectors.append({'id': 'destination_binding_mismatch_signed', 'expect': 'invalid', 'expected_failed_checks': ['certificate_destination_binding'], 'item': wrong_dest})
    # mismatched digest (payload digest of a different output)
    bad_digest = json.loads(json.dumps(valid)); bad_digest['envelope']['payload_digest'] = P.digest({'other': 1}); vectors.append({'id': 'mismatched_output_digest', 'expect': 'invalid', 'expected_failed_checks': ['envelope_signature', 'payload_digest'], 'item': bad_digest})
    # duplicate identifier: the same envelope id twice in one bundle (the checker flags the duplicate; a receiver would tombstone)
    vectors.append({'id': 'duplicate_identifier', 'expect': 'invalid', 'expected_failed_checks': ['duplicate_envelope_id'], 'item': json.loads(json.dumps(valid)), 'duplicate_of': 'valid_certificate'})
    # conflicting allocation: same allocation id, different content, both validly signed by dom-a
    twin = _item('dom-a', 'dom-b', 'allocation_certificate', dict(cert, recipient_account='na_carol'), ka, kida, 'nenv_vector0006', causality=cert['allocation_id'])
    vectors.append({'id': 'conflicting_allocation', 'expect': 'invalid', 'expected_failed_checks': ['conflicting_allocation_history'], 'item': twin, 'conflicts_with': 'valid_certificate'})
    # acceptance policy changed inside a receipt-like statement: signature must fail
    st = {'schema': 'metacoin-native-receipt/v0', 'protocol': P.PROTOCOL, 'kind': 'settlement', 'ref_type': 'native_intent', 'ref_id': 'ni_vector', 'claims': {'acceptance_policy': 'metacoin-acceptance-policy/v2', 'amount': 5}, 'issued_at': 1_800_000_000, 'nonce': 'ab'}
    rc = {'id': 'nr_vector', 'statement': st, 'signature_hex': crypto.sign(ka, P.signing_bytes('receipt', st)), 'key_id': kida}
    rc_bad = json.loads(json.dumps(rc)); rc_bad['statement']['claims']['acceptance_policy'] = 'metacoin-acceptance-policy/v1'
    return {'schema': 'metacoin-native-vectors/v0', 'environment': ENV, 'roots': roots, 'vectors': vectors, 'receipts': [{'id': 'valid_receipt', 'expect': 'valid', 'receipt': rc}, {'id': 'changed_acceptance_policy_receipt', 'expect': 'invalid', 'receipt': rc_bad}],
            'note': 'test keys derived from a public seed; never service or operator keys'}


def run(vs=None):
    vs = vs or generate(); roots = vs['roots']; results = []; seen = {}; by_alloc = {}
    for v in vs['vectors']:
        checks = checker.verify_item(v['item'], roots, vs['environment'])
        eid = v['item']['envelope'].get('id'); aid = v['item']['payload'].get('allocation_id')
        if eid in seen:
            checks.append({'check': 'duplicate_envelope_id', 'ok': False, 'detail': {'first': seen[eid]}})
        else:
            seen[eid] = v['id']
        if v['item']['envelope'].get('message_type') == 'allocation_certificate' and all(c['ok'] for c in checks):
            d = P.digest(v['item']['payload'])
            if aid in by_alloc and by_alloc[aid] != d:
                checks.append({'check': 'conflicting_allocation_history', 'ok': False, 'detail': {'allocation_id': aid}})
            by_alloc.setdefault(aid, d)
        failed = [c['check'] for c in checks if not c['ok']]
        ok = (not failed) if v['expect'] == 'valid' else bool(failed) and all(f in failed for f in v.get('expected_failed_checks', []))
        results.append({'id': v['id'], 'expect': v['expect'], 'failed_checks': failed, 'as_expected': ok})
    for r in vs['receipts']:
        chk = checker.verify_receipts([r['receipt']], roots, vs['environment'], 'dom-a')[0]
        results.append({'id': r['id'], 'expect': r['expect'], 'failed_checks': [] if chk['ok'] else [chk['check']], 'as_expected': chk['ok'] == (r['expect'] == 'valid')})
    return {'schema': 'metacoin-native-vector-results/v0', 'all_as_expected': all(r['as_expected'] for r in results), 'results': results}


if __name__ == '__main__':
    import sys
    out = run(); print(json.dumps(out, indent=1)); sys.exit(0 if out['all_as_expected'] else 1)
