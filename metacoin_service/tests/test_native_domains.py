"""Two separately configured settlement domains (separate homes, databases, authority keys and app instances) exchanging
destination-bound allocations through the courier adapters (order §7–§9, §16; journeys 8–20, 34, 35). The online adapter and
the file bundle drive one inbox rule set; every crash boundary is exercised with the armed fault points; returns are full or
refused; a backup restore enters quarantine until peers' signed observations reconcile it."""
import json
import os
import shutil
import unittest

from metacoin_service import crypto
from metacoin_service.native import courier, protocol as P
from metacoin_service.tests.test_work_terms import TermsInstance


class ClientEndpoint:
    """courier.Endpoint over a TestClient (same request shapes as the HTTP adapter)."""
    def __init__(self, inst, headers):
        self.c, self.h = inst.client, headers

    def call(self, method, path, body=None, raw=None):
        if raw is not None:
            r = self.c.request(method, path, headers=dict(self.h, **{'Content-Type': 'application/json'}), content=raw)
        else:
            r = self.c.request(method, path, headers=self.h, json=body) if body is not None else self.c.request(method, path, headers=self.h)
        return r.status_code, r.json()


class Domain:
    def __init__(self, did, fund=None):
        self.inst = TermsInstance(); self.inst.settings.x402_disabled = True; self.inst.settings.limits['test_hooks'] = 1; self.inst.reopen()
        self.c = self.inst.client; self.H = self.inst.h('owner'); self.id = did
        self.view = self.c.post('/api/v1/native/domain', headers=self.H, json={'domain_id': did, 'operator': 'same operator (test)', 'host': 'this host'}).json()
        self.asset = self.view['default_asset']; self.ep = ClientEndpoint(self.inst, self.H); self.actors = {}

    def actor(self, name, fund=0):
        a = self.c.post('/api/v1/native/actors', headers=self.H, json={'name': name}).json()
        self.actors[name] = {'account': a['account_id'], 'h': {'Authorization': 'Bearer ' + a['credential']['token']}}
        if fund:
            assert self.c.post('/api/v1/native/fixtures/fund', headers=self.H, json={'account_id': a['account_id'], 'amount': fund}).status_code == 201
        return self.actors[name]

    def pin(self, other):
        r = self.c.post('/api/v1/native/peers', headers=self.H, json={'domain_id': other.id, 'authority_key_id': other.domain()['authority']['key_id'], 'authority_public_hex': other.domain()['authority']['public_hex'], 'epoch': other.domain()['authority']['epoch']})
        assert r.status_code == 201, r.text

    def domain(self):
        return self.c.get('/api/v1/native/domain', headers=self.H).json()

    def bal(self, account, cat='available', asset=None):
        r = self.c.get('/api/v1/native/accounts/' + account, headers=self.H, params={'asset': asset} if asset else None); assert r.status_code == 200, r.text; return r.json()[cat]

    def audit(self, asset=None):
        return self.c.get('/api/v1/native/audit', headers=self.H, params={'asset': asset or self.asset}).json()

    def alloc(self, aid):
        return self.c.get('/api/v1/native/allocations/' + aid, headers=self.H).json()

    def fault(self, point, disarm=False):
        r = self.c.post('/api/v1/ops/faults', headers=self.H, json={'fault': point, 'disarm': disarm}); assert r.status_code == 200, r.text

    def export(self, who, to, recipient, amount, rid=None, expect=201):
        r = self.c.post('/api/v1/native/allocations/export', headers=self.actors[who]['h'], json={'destination_domain': to.id, 'recipient_account': recipient, 'amount': amount, 'request_id': rid or 'exp-%s-%d' % (who, amount)})
        assert r.status_code == expect, (r.status_code, r.text); return r.json()

    def close(self):
        self.inst.close()


class NativeDomainTests(unittest.TestCase):
    def setUp(self):
        os.environ['METACOIN_X402_DISABLED'] = '1'; self.addCleanup(lambda: os.environ.pop('METACOIN_X402_DISABLED', None))
        self.A = Domain('dom-a'); self.B = Domain('dom-b'); self.addCleanup(self.A.close); self.addCleanup(self.B.close)
        self.A.pin(self.B); self.B.pin(self.A)
        self.alice = self.A.actor('alice', fund=50); self.adam = self.A.actor('adam'); self.bob = self.B.actor('bob'); self.carol = self.B.actor('carol')

    def push(self, src, dst):
        return courier.push(src.ep, dst.ep, dst.id)

    # J8 J9 J18 J20 -----------------------------------------------------------------------------------------------------------
    def test_export_import_ack_conservation_and_duplicates(self):
        a = self.A.export('alice', self.B, self.bob['account'], 20)
        self.assertEqual((a['state'], a['finality'], self.A.bal(self.alice['account']), self.A.bal(self.alice['account'], 'in_transit')), ('certified', 'cross_domain_pending_claim', 30, 0))
        au = self.A.audit(); self.assertEqual((au['backing_locked_for_exports'], au['total_held'], au['fixture_funding']), (20, 30, 50)); self.assertTrue(au['conserved'])
        # before any import the destination has nothing; after the courier round trip the claim is spendable at B and acknowledged at A
        self.assertEqual(self.B.c.get('/api/v1/native/accounts/' + self.bob['account'], headers=self.B.H, params={'asset': self.A.asset}).json()['available'], 0)
        out = self.push(self.A, self.B); self.assertTrue(out['ok'], out); self.assertEqual(out['results'][0]['state'], 'accepted'); self.assertEqual(out['reply_results'][0]['state'], 'accepted')
        self.assertEqual(self.B.bal(self.bob['account'], asset=self.A.asset), 20)
        self.assertEqual(self.A.alloc(a['id'])['state'], 'acknowledged'); self.assertEqual(self.A.alloc(a['id'])['finality'], 'reconciled_inter_domain_settlement')
        bu = self.B.audit(self.A.asset); self.assertEqual((bu['claims_backed_elsewhere'], bu['total_held']), (20, 20)); self.assertTrue(bu['conserved'])
        self.assertEqual(self.A.audit()['total_held'] + self.B.audit(self.A.asset)['total_held'], 50)                      # one owner per unit across both domains
        # duplicate delivery (the whole bundle again, including the already-delivered certificate): idempotent, no second credit
        ob = self.A.c.get('/api/v1/native/transport/outbox', headers=self.A.H, params={'receiver': 'dom-b', 'all': '1'}).json()
        again = self.B.c.post('/api/v1/native/transport/inbox', headers=self.B.H, json=ob).json()
        self.assertEqual([r['state'] for r in again['results']], ['duplicate']); self.assertEqual(self.B.bal(self.bob['account'], asset=self.A.asset), 20)
        self.assertEqual(len(again['replies']), 1)                                                                            # the ack is replayed for a resent certificate
        # the imported claim is spendable at B under B's rules: bob pays carol
        t = self.B.c.post('/api/v1/native/transfers', headers=self.bob['h'], json={'to_account': self.carol['account'], 'amount': 5, 'asset': self.A.asset, 'request_id': 'bc1'}); self.assertEqual(t.status_code, 201, t.text)
        self.assertEqual((self.B.bal(self.bob['account'], asset=self.A.asset), self.B.bal(self.carol['account'], asset=self.A.asset)), (15, 5))
        # same request id replays the export instead of exporting twice
        a2 = self.A.export('alice', self.B, self.bob['account'], 20); self.assertEqual((a2['id'], a2.get('replayed')), (a['id'], True)); self.assertEqual(self.A.bal(self.alice['account']), 30)
        # the machine-account view at B separates claims pending acknowledgment from settled ones
        v = self.B.c.get('/api/v1/native/accounts/' + self.bob['account'], headers=self.B.H, params={'asset': self.A.asset}).json(); self.assertEqual(v['pending_inter_domain_claims'], 0)

    # J10 J35 J5 -----------------------------------------------------------------------------------------------------------------
    def test_wrong_destination_tampering_and_unpinned_sender_fail_before_value_changes(self):
        C = Domain('dom-c'); self.addCleanup(C.close); C.pin(self.A); cbob = C.actor('cbob')
        a = self.A.export('alice', self.B, self.bob['account'], 10)
        ob = self.A.c.get('/api/v1/native/transport/outbox', headers=self.A.H, params={'receiver': 'dom-b'}).json()
        r = C.c.post('/api/v1/native/transport/inbox', headers=C.H, json=ob).json(); self.assertEqual((r['results'][0]['state'], r['results'][0]['reason']), ('rejected', 'wrong_receiver'))
        # re-addressed envelope with the original certificate: the envelope signature no longer verifies; the certificate itself names dom-b
        forged = json.loads(json.dumps(ob)); forged['items'][0]['envelope']['receiver'] = 'dom-c'
        r = C.c.post('/api/v1/native/transport/inbox', headers=C.H, json=forged).json(); self.assertEqual(r['results'][0]['reason'], 'bad_signature')
        # tampered amount / recipient / asset inside the certificate -> payload digest or signature failure, nothing credited
        for field, value in (('amount', 11), ('recipient_account', self.carol['account']), ('asset', self.A.asset.replace('TMETA', 'OTHER'))):
            t = json.loads(json.dumps(ob)); t['items'][0]['payload'][field] = value
            r = self.B.c.post('/api/v1/native/transport/inbox', headers=self.B.H, json=t).json(); self.assertEqual(r['results'][0]['state'], 'rejected', r); self.assertIn(r['results'][0]['reason'], ('payload_digest_mismatch', 'bad_signature'))
            t['items'][0]['envelope']['payload_digest'] = P.digest(t['items'][0]['payload'])
            r = self.B.c.post('/api/v1/native/transport/inbox', headers=self.B.H, json=t).json(); self.assertEqual(r['results'][0]['reason'], 'bad_signature')
        self.assertEqual(self.B.bal(self.bob['account'], asset=self.A.asset), 0); self.assertEqual(self.B.bal(self.carol['account'], asset=self.A.asset), 0)
        # an unpinned sender (C never pinned at B) is refused even with valid signatures; a bundle cannot add a root
        cexp = C.c.post('/api/v1/native/fixtures/fund', headers=C.H, json={'account_id': cbob['account'], 'amount': 5}); C.pin(self.B)
        C.export('cbob', self.B, self.bob['account'], 5)
        cob = C.c.get('/api/v1/native/transport/outbox', headers=C.H, params={'receiver': 'dom-b'}).json()
        r = self.B.c.post('/api/v1/native/transport/inbox', headers=self.B.H, json=cob).json(); self.assertEqual(r['results'][0]['reason'], 'sender_not_pinned')
        # oversized / too many items / non-object items are refused by the limits, never executed
        big = dict(ob, items=ob['items'] * 201); r = self.B.c.post('/api/v1/native/transport/inbox', headers=self.B.H, json=big); self.assertEqual(r.status_code, 422)
        r = self.B.c.post('/api/v1/native/transport/inbox', headers=self.B.H, json={'items': ['<script>', {'envelope': {'id': 'x'}}]}).json(); self.assertEqual([x['state'] for x in r['results']], ['rejected', 'rejected'])
        # the genuine certificate still imports exactly once afterwards
        out = self.push(self.A, self.B); self.assertEqual(out['results'][0]['state'], 'accepted', out); self.assertEqual(self.B.bal(self.bob['account'], asset=self.A.asset), 10)

    # J11 J16 ---------------------------------------------------------------------------------------------------------------------
    def test_lost_ack_cannot_refund_or_reexport_and_redelivery_converges(self):
        a = self.A.export('alice', self.B, self.bob['account'], 20)
        ob = self.A.c.get('/api/v1/native/transport/outbox', headers=self.A.H, params={'receiver': 'dom-b'}).json()
        r = self.B.c.post('/api/v1/native/transport/inbox', headers=self.B.H, json=ob).json(); self.assertEqual(r['results'][0]['state'], 'accepted')   # the ack (reply) is lost here
        self.assertEqual(self.A.alloc(a['id'])['state'], 'certified'); self.assertEqual(self.A.bal(self.alice['account']), 30)
        self.assertEqual(self.A.alloc(a['id'])['messages'][0]['state'], 'queued')                                                                  # still retried
        # no source path makes the backing spendable again: not time, not an administrator transfer
        t = self.A.c.post('/api/v1/native/transfers', headers=self.alice['h'], json={'to_account': self.adam['account'], 'amount': 31, 'request_id': 'over'}); self.assertEqual(t.status_code, 400, t.text)
        rr = self.A.c.post('/api/v1/native/allocations/' + a['id'] + '/return', headers=self.A.H, json={}); self.assertEqual(rr.status_code, 404)      # returns start only at the destination
        # out-of-order and delayed: the ack arrives at A after a second export's certificate was issued; everything converges
        a2 = self.A.export('alice', self.B, self.carol['account'], 5)
        out = self.push(self.A, self.B)                                      # resends cert 1 (duplicate -> ack replayed) + cert 2 (accepted -> ack)
        self.assertEqual([x['state'] for x in out['results']], ['duplicate', 'accepted']); self.assertEqual([x['state'] for x in out['reply_results']], ['accepted', 'accepted'])
        self.assertEqual((self.A.alloc(a['id'])['state'], self.A.alloc(a2['id'])['state']), ('acknowledged', 'acknowledged'))
        self.assertEqual((self.B.bal(self.bob['account'], asset=self.A.asset), self.B.bal(self.carol['account'], asset=self.A.asset)), (20, 5))
        self.assertEqual(self.A.audit()['total_held'] + self.B.audit(self.A.asset)['total_held'], 50)

    # J12 -----------------------------------------------------------------------------------------------------------------------
    def test_every_crash_boundary_recovers_without_duplicated_ownership(self):
        # source boundaries: reserve / export commit / certificate publication
        for point, durable_state in (('native_export_reserve', 'reserved'), ('native_export_commit', 'export_committed'), ('native_certificate_publish', 'certified')):
            self.A.fault(point)
            r = self.A.c.post('/api/v1/native/allocations/export', headers=self.alice['h'], json={'destination_domain': 'dom-b', 'recipient_account': self.bob['account'], 'amount': 3, 'request_id': 'crash-' + point})
            self.assertEqual(r.status_code, 409, r.text); self.A.fault(point, disarm=True)
            allocs = self.A.c.get('/api/v1/native/allocations', headers=self.A.H, params={'direction': 'export'}).json()['items']
            mine = [x for x in allocs if x['evidence'].get('request_id') == 'crash-' + point]
            self.assertEqual(len(mine), 1); self.assertEqual(mine[0]['state'], durable_state)
            self.assertEqual(self.A.bal(self.alice['account']) + self.A.bal(self.alice['account'], 'in_transit') + self.A.audit()['backing_locked_for_exports'], 50)
            # the retried request (same request id) resumes from the durable state; the recovery sweep finds nothing left behind
            r2 = self.A.c.post('/api/v1/native/allocations/export', headers=self.alice['h'], json={'destination_domain': 'dom-b', 'recipient_account': self.bob['account'], 'amount': 3, 'request_id': 'crash-' + point}).json()
            self.assertEqual((r2['id'], r2['state']), (mine[0]['id'], 'certified'))
            self.assertEqual(self.A.c.post('/api/v1/native/allocations/recover', headers=self.A.H, json={}).json()['resumed'], [])
        self.assertEqual(self.A.audit()['backing_locked_for_exports'], 9); self.assertEqual(self.A.bal(self.alice['account']), 41)
        certs = self.A.c.get('/api/v1/native/transport/outbox', headers=self.A.H, params={'receiver': 'dom-b'}).json(); self.assertEqual(len(certs['items']), 3)
        # destination boundaries: import commit (nothing credited, no tombstone, retry succeeds) / ack publication (credited once, ack reconstructed on redelivery)
        # a crash inside the import (before or after the credit, before the ack is queued) aborts the whole delivery transaction:
        # the credit and its acknowledgment share one boundary, so nothing is credited without its ack record and no tombstone is left
        for point in ('native_import_commit', 'native_ack'):
            self.B.fault(point)
            r = self.B.c.post('/api/v1/native/transport/inbox', headers=self.B.H, json=certs); self.assertEqual(r.status_code, 409, r.text); self.B.fault(point, disarm=True)
            self.assertEqual(self.B.bal(self.bob['account'], asset=self.A.asset), 0)
            self.assertEqual(self.B.c.get('/api/v1/native/allocations', headers=self.B.H).json()['items'], [])
        out = self.push(self.A, self.B); self.assertEqual([x['state'] for x in out['results']], ['accepted'] * 3); self.assertEqual(self.B.bal(self.bob['account'], asset=self.A.asset), 9)
        self.assertEqual(self.A.audit()['total_held'] + self.B.audit(self.A.asset)['total_held'], 50)
        self.assertEqual(len([x for x in self.A.c.get('/api/v1/native/allocations', headers=self.A.H).json()['items'] if x['state'] == 'acknowledged']), 3)

    # J17 -----------------------------------------------------------------------------------------------------------------------
    def test_full_return_closes_and_partial_is_refused(self):
        a = self.A.export('alice', self.B, self.bob['account'], 20); self.push(self.A, self.B)
        self.assertEqual(self.B.bal(self.bob['account'], asset=self.A.asset), 20)
        rr = self.B.c.post('/api/v1/native/allocations/' + a['id'] + '/return', headers=self.bob['h'], json={}); self.assertEqual(rr.status_code, 200, rr.text)
        self.assertEqual((rr.json()['state'], self.B.bal(self.bob['account'], asset=self.A.asset), self.B.bal(self.bob['account'], 'in_transit', asset=self.A.asset)), ('return_requested', 0, 20))
        self.assertEqual(self.A.bal(self.alice['account']), 30)                                                                 # nothing at the source until the request arrives
        out = self.push(self.B, self.A); self.assertEqual(out['results'][0]['state'], 'accepted'); self.assertEqual(out['reply_results'][0]['state'], 'accepted')
        self.assertEqual((self.A.alloc(a['id'])['state'], self.A.bal(self.alice['account']), self.A.audit()['backing_locked_for_exports']), ('returned', 50, 0))
        self.assertEqual((self.B.alloc(a['id'])['state'], self.B.bal(self.bob['account'], 'in_transit', asset=self.A.asset), self.B.audit(self.A.asset)['claims_backed_elsewhere']), ('returned', 0, 0))
        # partial: spend part of a second allocation, then try to return it
        a2 = self.A.export('alice', self.B, self.bob['account'], 10, rid='second'); self.push(self.A, self.B)
        self.B.c.post('/api/v1/native/transfers', headers=self.bob['h'], json={'to_account': self.carol['account'], 'amount': 1, 'asset': self.A.asset, 'request_id': 'spend1'})
        rr = self.B.c.post('/api/v1/native/allocations/' + a2['id'] + '/return', headers=self.bob['h'], json={}); self.assertEqual(rr.status_code, 409); self.assertEqual(rr.json()['detail']['code'], 'return_partial_unsupported')
        self.assertEqual(self.A.audit()['total_held'] + self.B.audit(self.A.asset)['total_held'], 50)

    # J34 -----------------------------------------------------------------------------------------------------------------------
    def test_rotation_policy_old_key_grace_and_no_retroactive_authority(self):
        a_old = self.A.export('alice', self.B, self.bob['account'], 4, rid='pre-rotation')                                # certified under epoch 1, delivered later
        old_key_path = self.A.inst.settings.keys_dir / 'native' / 'authority-dom-a.ed25519'
        rot = self.A.c.post('/api/v1/native/domain/rotate', headers=self.A.H, json={'grace_seconds': 3600}).json(); self.assertEqual(rot['authority']['epoch'], 2)
        a_new = self.A.export('alice', self.B, self.bob['account'], 6, rid='post-rotation')
        self.assertEqual(self.A.alloc(a_new['id'])['certificate']['source_epoch'], 2)
        out = self.push(self.A, self.B)                                                                                   # rotation notice first, then both certificates
        self.assertEqual([x['state'] for x in out['results']], ['accepted'] * 3, out)
        self.assertEqual(self.B.c.get('/api/v1/native/peers/dom-a', headers=self.B.H).json()['epoch'], 2); self.assertEqual(self.B.bal(self.bob['account'], asset=self.A.asset), 10)
        # a message signed by the retired key AFTER its grace (created_at in the future of grace) is refused; a new-epoch key cannot sign an epoch-1 certificate
        cert = dict(self.A.alloc(a_old['id'])['certificate'], allocation_id='nal_forged1', issued_at=P.canonical and 1)
        env = {'protocol': P.PROTOCOL, 'type': 'envelope', 'id': 'nenv_forged1', 'environment': 'dev', 'sender': 'dom-a', 'receiver': 'dom-b', 'message_type': 'allocation_certificate', 'causality': 'nal_forged1', 'payload_digest': P.digest(cert), 'expires_at': 4102444800, 'created_at': 4102444700}
        key = crypto.load_signing_key(old_key_path)
        item = {'envelope': env, 'envelope_signature': crypto.sign(key, P.signing_bytes('envelope', env)), 'key_id': rot['authority']['key_id'] and self.B.c.get('/api/v1/native/peers/dom-a', headers=self.B.H).json()['keys'][0]['key_id'], 'payload': cert, 'payload_signature': crypto.sign(key, P.signing_bytes('allocation_certificate', cert))}
        r = self.B.c.post('/api/v1/native/transport/inbox', headers=self.B.H, json={'items': [item]}).json(); self.assertEqual(r['results'][0]['reason'], 'key_retired')
        new_key = crypto.load_signing_key(self.A.inst.settings.keys_dir / 'native' / 'authority-dom-a-e2.ed25519')
        env2 = dict(env, id='nenv_forged2', created_at=env['created_at'] - 1000)
        item2 = {'envelope': env2, 'envelope_signature': crypto.sign(new_key, P.signing_bytes('envelope', env2)), 'key_id': rot['authority']['key_id'], 'payload': cert, 'payload_signature': crypto.sign(new_key, P.signing_bytes('allocation_certificate', cert))}
        r = self.B.c.post('/api/v1/native/transport/inbox', headers=self.B.H, json={'items': [item2]}).json(); self.assertEqual(r['results'][0]['reason'], 'epoch_key_mismatch')
        self.assertEqual(self.B.bal(self.bob['account'], asset=self.A.asset), 10)

    # J15 -----------------------------------------------------------------------------------------------------------------------
    def test_conflicting_signed_history_is_quarantined(self):
        a = self.A.export('alice', self.B, self.bob['account'], 7); self.push(self.A, self.B)
        cert = self.A.alloc(a['id'])['certificate']; twin = dict(cert, recipient_account=self.carol['account'])            # the same allocation id, a different signed content
        key = crypto.load_signing_key(self.A.inst.settings.keys_dir / 'native' / 'authority-dom-a.ed25519')
        env = {'protocol': P.PROTOCOL, 'type': 'envelope', 'id': 'nenv_twin', 'environment': 'dev', 'sender': 'dom-a', 'receiver': 'dom-b', 'message_type': 'allocation_certificate', 'causality': a['id'], 'payload_digest': P.digest(twin), 'expires_at': 4102444800, 'created_at': cert['issued_at']}
        item = {'envelope': env, 'envelope_signature': crypto.sign(key, P.signing_bytes('envelope', env)), 'key_id': self.A.domain()['authority']['key_id'], 'payload': twin, 'payload_signature': crypto.sign(key, P.signing_bytes('allocation_certificate', twin))}
        r = self.B.c.post('/api/v1/native/transport/inbox', headers=self.B.H, json={'items': [item]}).json(); self.assertEqual(r['results'][0]['reason'], 'conflicting_signed_history')
        rec = self.B.c.get('/api/v1/native/recovery', headers=self.B.H).json(); self.assertEqual(rec['quarantined_peers'], ['dom-a']); self.assertEqual(rec['quarantined_allocations'], [a['id']])
        self.assertEqual(self.B.bal(self.carol['account'], asset=self.A.asset), 0)
        # further certificates from the quarantined authority are refused until an administrator acts
        self.A.export('alice', self.B, self.bob['account'], 1, rid='after-twin'); out = self.push(self.A, self.B); self.assertEqual(out['results'][0]['reason'], 'peer_quarantined')

    # J13 J14 J35 ----------------------------------------------------------------------------------------------------------------
    def test_cut_link_bounded_spending_with_file_bundles(self):
        a = self.A.export('alice', self.B, self.bob['account'], 12, rid='prealloc')                                         # disjoint preallocation before the cut
        self.B.c.post('/api/v1/native/domain/connectivity', headers=self.B.H, json={'state': 'cut'})
        self.assertEqual(self.push(self.A, self.B)['error']['detail']['code'], 'link_cut')
        tmp = self.B.inst.home / 'bundle-a-to-b.json'; w = courier.write_bundle(self.A.ep, 'dom-b', str(tmp)); self.assertTrue(w['ok'])
        i = courier.import_bundle(self.B.ep, str(tmp), str(self.B.inst.home / 'replies.json')); self.assertTrue(i['ok'], i); self.assertEqual(i['results'][0]['state'], 'accepted')
        self.assertEqual(self.B.bal(self.bob['account'], asset=self.A.asset), 12)
        v = self.B.c.get('/api/v1/native/accounts/' + self.bob['account'], headers=self.B.H, params={'asset': self.A.asset}).json(); self.assertEqual(v['pending_inter_domain_claims'], 12)   # ack not yet carried back
        # bounded local spending while cut: bob commits 12, the 13th unit is refused, completed transfers stay
        ok = self.B.c.post('/api/v1/native/transfers', headers=self.bob['h'], json={'to_account': self.carol['account'], 'amount': 12, 'asset': self.A.asset, 'request_id': 'cut-1'}); self.assertEqual(ok.status_code, 201)
        no = self.B.c.post('/api/v1/native/transfers', headers=self.bob['h'], json={'to_account': self.carol['account'], 'amount': 1, 'asset': self.A.asset, 'request_id': 'cut-2'}); self.assertEqual(no.status_code, 400); self.assertEqual(no.json()['detail']['code'], 'insufficient_available')
        self.assertEqual(self.B.bal(self.carol['account'], asset=self.A.asset), 12)
        # the reply bundle (ack) carried back by hand; the source marks it acknowledged; link restored
        rb = self.B.inst.home / 'replies.json'; self.assertTrue(rb.exists())
        i2 = courier.import_bundle(self.A.ep, str(rb)); self.assertTrue(i2['ok']); courier.mark_from_results(self.A.ep, i['results']); courier.mark_from_results(self.B.ep, i2['results'])
        self.assertEqual(self.A.alloc(a['id'])['state'], 'acknowledged')
        self.B.c.post('/api/v1/native/domain/connectivity', headers=self.B.H, json={'state': 'online'})
        self.assertEqual(self.B.c.get('/api/v1/native/accounts/' + self.bob['account'], headers=self.B.H, params={'asset': self.A.asset}).json()['pending_inter_domain_claims'], 0)
        self.assertEqual(self.A.audit()['total_held'] + self.B.audit(self.A.asset)['total_held'], 50)

    # J19 -----------------------------------------------------------------------------------------------------------------------
    def test_backup_restore_enters_quarantine_until_peer_observation_reconciles(self):
        db = self.A.inst.settings.db_path; backup = self.A.inst.home / 'service.before.sqlite'
        # a checkpoint of A before the export; B then imports the export; A is restored from the old backup (restoring old balance data)
        with self.A.inst.app.state.services.db.read() as d:
            d.execute("VACUUM INTO ?", (str(backup),))
        a = self.A.export('alice', self.B, self.bob['account'], 20, rid='pre-backup'); self.push(self.A, self.B); self.assertEqual(self.B.bal(self.bob['account'], asset=self.A.asset), 20)
        for suffix in ('', '-wal', '-shm'):
            p = db.with_name(db.name + suffix)
            if p.exists() and suffix:
                p.unlink()
        shutil.copyfile(backup, db); self.A.inst.reopen(); self.A.c = self.A.inst.client; self.A.ep = ClientEndpoint(self.A.inst, self.A.H)
        dv = self.A.domain(); self.assertEqual(dv['recovery_state'], 'quarantine'); self.assertEqual(self.A.bal(self.alice['account']), 50)                     # stale balance visible, but...
        for call in (lambda: self.A.c.post('/api/v1/native/allocations/export', headers=self.alice['h'], json={'destination_domain': 'dom-b', 'recipient_account': self.bob['account'], 'amount': 1, 'request_id': 'q1'}),
                     lambda: self.A.c.post('/api/v1/native/transfers', headers=self.alice['h'], json={'to_account': self.adam['account'], 'amount': 1, 'request_id': 'q2'})):
            r = call(); self.assertEqual(r.status_code, 409, r.text); self.assertEqual(r.json()['detail']['code'], 'domain_quarantined')            # ...no outward spending resumes
        rec = self.A.c.post('/api/v1/native/recovery/reconcile', headers=self.A.H, json={}).json(); self.assertEqual(rec['recovery_state'], 'quarantine'); self.assertEqual(rec['missing_observations'], ['dom-b'])
        # B's signed observation names the imported allocation A no longer knows; A locks the backing from it, then leaves quarantine
        self.B.c.post('/api/v1/native/transport/observation', headers=self.B.H, json={'peer': 'dom-a'})
        out = self.push(self.B, self.A); self.assertEqual(out['results'][0]['state'], 'accepted', out)
        rec = self.A.c.post('/api/v1/native/recovery/reconcile', headers=self.A.H, json={}).json(); self.assertEqual(rec['peers']['dom-b']['missing_locally'], [a['id']]); self.assertEqual(rec['recovery_state'], 'quarantine')
        rec = self.A.c.post('/api/v1/native/recovery/reconcile', headers=self.A.H, json={'reconstruct_from_observations': True, 'source_account': self.alice['account'], 'amounts': {a['id']: 20}}).json()
        self.assertEqual(rec['recovery_state'], 'normal'); self.assertEqual(rec['reconstructed'][0]['status'], 'locked')
        self.assertEqual((self.A.bal(self.alice['account']), self.A.audit()['backing_locked_for_exports']), (30, 20)); self.assertTrue(self.A.audit()['conserved'])
        self.assertEqual(self.A.audit()['total_held'] + self.B.audit(self.A.asset)['total_held'], 50)
        self.assertEqual(self.A.c.get('/api/v1/native/recovery', headers=self.A.H).json()['recovery_state'], 'normal')


if __name__ == '__main__':
    unittest.main()
