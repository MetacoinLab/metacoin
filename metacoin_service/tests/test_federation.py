"""Worker-node federation through real entry points: deliberate enrollment with operator-approved limits, node
request authentication (credential + Ed25519 signature, replay and widening refused), execution-location policy
(local-only work never reaches a node; node-only work never runs on a local worker), a node executing a real
compute job through the transport (input transfer, chunked uploads, fenced publication, coordinator-side
verification), lease loss with safe reassignment and stale publication rejected, checkpoint resume across a node
and a local worker, drain/disable/revoke with evidence preserved."""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from metacoin_service import crypto
from metacoin_service.db import Database, now
from metacoin_service.federation.node_worker import NodeClient, NodeWorker
from metacoin_service.tests.test_compute_engine import ComputeInstance, HAVE_RUNTIME, batch_spec, heat_spec


def make_identity(inst, name='node-a', devices=('cpu',), caps=None, headers=None):
    key = crypto._ed.Ed25519PrivateKey.generate()
    pub = key.public_key().public_bytes(crypto._ser.Encoding.Raw, crypto._ser.PublicFormat.Raw).hex()
    r = inst.client.post('/api/v1/nodes', headers=headers or inst.h('owner'), json={'name': name, 'public_key_hex': pub, 'devices': list(devices), 'capabilities': caps or ['temporal_batch', 'heat_diffusion']})
    assert r.status_code == 201, r.text
    priv = key.private_bytes(crypto._ser.Encoding.Raw, crypto._ser.PrivateFormat.Raw, crypto._ser.NoEncryption()).hex()
    return {'node_id': r.json()['node_id'], 'credential': r.json()['credential'], 'private_key_hex': priv}


def in_process_client(inst, identity):
    def transport(method, path, headers, body):
        r = inst.client.request(method, path, headers=headers, content=body)
        return r.status_code, r.content
    return NodeClient('http://127.0.0.1:1', identity, transport=transport)


@unittest.skipUnless(HAVE_RUNTIME, 'no numpy-capable compute interpreter on this host')
class FederationTests(unittest.TestCase):
    def setUp(self):
        self.inst = ComputeInstance(); self.addCleanup(self.inst.close); self.c = self.inst.client; self.H = self.inst.h('owner')
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)

    def node(self, identity):
        w = NodeWorker(in_process_client(self.inst, identity), Path(self.tmp.name) / identity['node_id'], log=lambda m: None)
        w.register()
        return w

    def job(self, spec, locations, kind='temporal_batch'):
        r = self.c.post('/api/v1/contracts', headers=self.H, json={'kind': kind, 'title': 'fed', 'inputs': spec, 'policy': {'reviewer_id': self.inst.ids['reviewer'], 'execution_locations': locations}})
        self.assertEqual(r.status_code, 201, r.text)
        cid = r.json()['id']; self.c.post('/api/v1/contracts/' + cid + '/freeze', headers=self.H)
        return self.c.post('/api/v1/jobs', headers=self.H, json={'contract_id': cid}).json()['id']

    def test_enrollment_authentication_policy_execution_recovery_and_revocation(self):
        # enrollment is deliberate: only node:admin; scope cannot be widened through registration fields
        key = crypto._ed.Ed25519PrivateKey.generate()
        pub = key.public_key().public_bytes(crypto._ser.Encoding.Raw, crypto._ser.PublicFormat.Raw).hex()
        self.assertEqual(self.c.post('/api/v1/nodes', headers=self.inst.h('viewer'), json={'name': 'x', 'public_key_hex': pub}).status_code, 403)
        self.assertEqual(self.c.post('/api/v1/nodes', headers=self.H, json={'name': 'x', 'public_key_hex': pub, 'workspaces': ['ws_other']}).status_code, 403)
        self.assertEqual(self.c.post('/api/v1/nodes', headers=self.H, json={'name': 'x', 'public_key_hex': pub, 'capabilities': ['text_generation']}).status_code, 422)
        ident = make_identity(self.inst)
        nid = ident['node_id']
        # authentication: unknown credential, wrong key, replayed request and an unsigned request are refused
        unknown = in_process_client(self.inst, dict(ident, credential='mcn_' + 'a' * 43))
        self.assertEqual(unknown.call('POST', '/node/v1/register', {'devices': ['cpu']})[0], 401)
        wrongkey = in_process_client(self.inst, dict(ident, private_key_hex=crypto._ed.Ed25519PrivateKey.generate().private_bytes(crypto._ser.Encoding.Raw, crypto._ser.PrivateFormat.Raw, crypto._ser.NoEncryption()).hex()))
        self.assertEqual(wrongkey.call('POST', '/node/v1/register', {'devices': ['cpu']})[0], 401)
        good = in_process_client(self.inst, ident)
        captured = {}
        orig = good.transport
        def capture(method, path, headers, body):
            captured['headers'], captured['body'] = headers, body
            return orig(method, path, headers, body)
        good.transport = capture
        st, out = good.json('POST', '/node/v1/register', {'devices': ['cpu'], 'versions': {'python': '3.12'}})
        self.assertEqual(st, 200, out); self.assertEqual(out['capabilities'], ['heat_diffusion', 'temporal_batch'])
        replay = self.c.request('POST', '/node/v1/register', headers=captured['headers'], content=captured['body'])
        self.assertEqual(replay.status_code, 401)
        self.assertEqual(self.c.post('/node/v1/register', headers={'X-Node-Credential': ident['credential']}, json={'devices': ['cpu']}).status_code, 401)
        good.transport = orig
        # a node cannot report devices beyond its enrollment
        self.assertEqual(good.json('POST', '/node/v1/register', {'devices': ['cpu', 'cuda']})[0], 403)
        # execution-location policy: local-only work is invisible to the node; node-only work is invisible to local workers
        local_job = self.job(batch_spec(private_label='LOCAL_ONLY'), ['local'])
        node_job = self.job(batch_spec(private_label='NODE_ONLY'), [nid])
        w = self.node(ident)
        st, out = good.json('POST', '/node/v1/claim', {})
        self.assertEqual((st, out['job']['id']), (200, node_job), out)
        self.assertEqual(out['job']['inputs']['private_label'], 'NODE_ONLY'); self.assertNotIn('LOCAL_ONLY', json.dumps(out))
        # the node already holds node_job; release it back for the worker path by expiring the lease deterministically
        with Database(self.inst.settings.db_path).tx() as db:
            db.execute("UPDATE jobs SET lease_expires=? WHERE id=?", (now() - 1, node_job))
        lw = self.inst.worker(); self.addCleanup(lw.offline)
        ran = lw.run_once()
        self.assertEqual(ran[0], local_job)                     # the local worker never touches node-only work even with an expired lease
        self.assertEqual(ran[1], 'succeeded')
        self.assertIsNone(lw.run_once())
        q = self.c.get('/api/v1/queue', headers=self.H).json()
        # the node reclaims its job (expired lease -> a new generation), executes the real compute child and publishes through uploads
        res = w.run_once()
        self.assertIsNotNone(res, 'node should reclaim the expired node-only job')
        self.assertEqual(res, (node_job, 'succeeded'), res)
        v = self.inst.view(node_job)
        self.assertEqual((v['state'], v['phase'], v['verification']['passed'], v['work']['committed']), ('succeeded', 'completed', True, 364))
        self.assertIn('federated node', v['backend_reason'])
        nv = self.c.get('/api/v1/nodes/' + nid, headers=self.H).json()
        self.assertEqual(nv['observed']['completed_by_backend'], {'cpu': 1}); self.assertTrue(any(t['role'] == 'result' and t['direction'] == 'from_node' for t in nv['transfers']))
        self.assertTrue(any(t['role'] == 'input' and t['direction'] == 'to_node' for t in nv['transfers']))
        # stale publication: after the lease is lost and the job reassigned, the old generation's uploads and results are refused
        long_job = self.job(heat_spec(nx=128, ny=128, steps=3000, device_policy='cpu'), ['*'], kind='heat_diffusion')
        st, out = good.json('POST', '/node/v1/claim', {})
        self.assertEqual(out['job']['id'], long_job); gen = out['job']['lease_generation']
        with Database(self.inst.settings.db_path).tx() as db:
            db.execute("UPDATE jobs SET lease_expires=? WHERE id=?", (now() - 1, long_job))
        self.assertEqual(lw.run_once(), (long_job, 'succeeded'))            # '*' policy: a local worker may recover it
        st, out = good.json('POST', '/node/v1/jobs/%s/uploads?generation=%d' % (long_job, gen), {'role': 'result', 'total_bytes': 10, 'sha256': '0' * 64})
        self.assertEqual((st, out['detail']['code']), (409, 'stale_lease'))
        st, out = good.json('POST', '/node/v1/jobs/%s/lease?generation=%d' % (long_job, gen), {})
        self.assertEqual(st, 409)
        # chunked uploads: offset gaps refused, duplicate chunk idempotent, digest mismatch aborts
        j3 = self.job(batch_spec(private_label='UPLOADS'), ['nodes'])
        st, out = good.json('POST', '/node/v1/claim', {}); g3 = out['job']['lease_generation']
        blob = b'x' * 10000
        import hashlib
        st, up = good.json('POST', '/node/v1/jobs/%s/uploads?generation=%d' % (j3, g3), {'role': 'result', 'total_bytes': len(blob), 'sha256': hashlib.sha256(blob).hexdigest()})
        self.assertEqual(st, 200)
        uid = up['upload_id']
        self.assertEqual(good.json('PUT', '/node/v1/uploads/%s?offset=5000' % uid, raw=blob[5000:])[1]['detail']['code'], 'chunk_offset_gap')
        self.assertEqual(good.json('PUT', '/node/v1/uploads/%s?offset=0' % uid, raw=blob[:5000])[1]['received_bytes'], 5000)
        self.assertEqual(good.json('PUT', '/node/v1/uploads/%s?offset=0' % uid, raw=blob[:5000])[1]['received_bytes'], 5000)
        self.assertEqual(good.json('POST', '/node/v1/uploads/%s/complete' % uid, {})[1]['detail']['code'], 'upload_incomplete')
        self.assertEqual(good.json('PUT', '/node/v1/uploads/%s?offset=5000' % uid, raw=b'y' * 5000)[1]['received_bytes'], 10000)
        self.assertEqual(good.json('POST', '/node/v1/uploads/%s/complete' % uid, {})[1]['detail']['code'], 'upload_digest_mismatch')
        good.json('POST', '/node/v1/jobs/%s/fail?generation=%d' % (j3, g3), {'code': 'COMPUTATION_ERROR', 'reason': 'test'})
        # drain: no new claims; revoke: transport refused, historical evidence kept, artifact access denied
        self.assertEqual(self.c.post('/api/v1/nodes/' + nid + '/drain', headers=self.H, json={}).json()['state'], 'draining')
        st, out = good.json('POST', '/node/v1/claim', {}); self.assertIsNone(out['job']); self.assertIn('draining', out['reason'])
        self.c.post('/api/v1/nodes/' + nid + '/enable', headers=self.H, json={})
        rv = self.c.post('/api/v1/nodes/' + nid + '/revoke', headers=self.H, json={'reason': 'test revocation'}).json()
        self.assertEqual(rv['state'], 'revoked')
        self.assertEqual(good.json('POST', '/node/v1/claim', {})[0], 403)
        self.assertEqual(good.call('GET', '/node/v1/jobs/%s/checkpoint?generation=%d' % (node_job, 2))[0], 403)
        self.assertEqual(self.c.get('/api/v1/compute/jobs/' + node_job, headers=self.H).json()['state'], 'succeeded')
        self.assertEqual(self.c.post('/api/v1/nodes/' + nid + '/enable', headers=self.H, json={}).status_code, 409)
        ev = self.c.get('/api/v1/events?types=node.revoked,node.claimed', headers=self.H).json()
        self.assertTrue(any(e['event_type'] == 'node.revoked' for e in ev['items']))

    def test_checkpoint_resume_across_node_and_local_worker(self):
        ident = make_identity(self.inst, name='node-b')
        w = self.node(ident)
        jid = self.job(heat_spec(device_policy='cpu'), ['*'], kind='heat_diffusion')
        client = in_process_client(self.inst, ident)
        st, out = client.json('POST', '/node/v1/claim', {}); gen = out['job']['lease_generation']
        # run the child ourselves up to the first checkpoint, publish it through the node upload path, then abandon the attempt
        from metacoin_service.compute import container
        import subprocess, sys
        home = Path(self.tmp.name) / 'resume'; home.mkdir(exist_ok=True)
        spec = {'job_id': jid, 'kind': 'heat_diffusion', 'inputs': out['job']['inputs'], 'manifest': out['job']['manifest'], 'backend': 'cpu', 'precision': out['job']['precision'], 'attempt_generation': gen,
                'input_digest': out['job']['input_digest'], 'chunk': None, 'checkpoint_interval_seconds': 1, 'resume_dir': None, 'limits': out['job']['limits'], 'aux': None}
        (home / 'spec.json').write_bytes(json.dumps(spec).encode())
        proc = subprocess.Popen([w.runtime['python'], '-m', 'metacoin_service.compute.exec', str(home)], cwd=str(Path(__file__).resolve().parents[2]), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                env={'PATH': os.environ['PATH'], 'PYTHONPATH': str(Path(__file__).resolve().parents[2]), 'HOME': os.environ.get('HOME', '/')})
        published = None
        for line in proc.stdout:
            ev = json.loads(line)
            if ev.get('event') == 'checkpoint':
                files = {p.name: p.read_bytes() for p in (home / ev['dir']).iterdir()}
                uid = w._upload(jid, gen, 'checkpoint', files)
                st, res = client.json('POST', '/node/v1/jobs/%s/checkpoints?generation=%d' % (jid, gen), {'upload_id': uid, 'generation': ev['generation'], 'committed': ev['committed'], 'reason': 'interval'})
                self.assertEqual((st, res['published']), (200, True), res); published = ev['committed']
                proc.stdin.write(b'cancel\n'); proc.stdin.flush(); break
        proc.kill(); proc.wait()
        self.assertIsNotNone(published)
        v = self.inst.view(jid)
        self.assertEqual((v['work']['committed'], v['checkpoint_generation']), (published, 1))
        # the node disappears: lease expires; a local worker resumes from the node's published checkpoint under a new generation
        with Database(self.inst.settings.db_path).tx() as db:
            db.execute("UPDATE jobs SET lease_expires=? WHERE id=?", (now() - 1, jid))
        lw = self.inst.worker(); self.addCleanup(lw.offline)
        self.assertEqual(lw.run_once(), (jid, 'succeeded'))
        v = self.inst.view(jid)
        self.assertEqual((v['state'], v['verification']['passed']), ('succeeded', True))
        units = self.c.get('/api/v1/compute/jobs/' + jid + '/checkpoints', headers=self.H).json()
        ranges = units['work_units'] if 'work_units' in units else None
        self.assertGreaterEqual(len(units['checkpoints']), 1)
        self.assertTrue(any(c['attempt_generation'] == gen for c in units['checkpoints']))
        # committed unit ranges are contiguous and never double count across the node attempt and the local attempt
        with Database(self.inst.settings.db_path).read() as db:
            rows = db.execute('SELECT unit_from, unit_to FROM compute_work_units WHERE job_id=? ORDER BY unit_from', (jid,)).fetchall()
        self.assertEqual(rows[0]['unit_from'], 0)
        for a, b in zip(rows, rows[1:]):
            self.assertEqual(a['unit_to'], b['unit_from'])
        self.assertEqual(rows[-1]['unit_to'], v['work']['total'])
