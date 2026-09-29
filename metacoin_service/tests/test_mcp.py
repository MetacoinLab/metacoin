"""MCP interface with a separate conforming client process: protocol negotiation over stdio, tool and resource
discovery, bounded job submission and status, a verification request, invalid requests, and a read-only principal
whose mutation attempts are refused server-side (never by the tool description). The API runs as its own process;
the MCP server is a third process holding only a private credential file."""
import asyncio
import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

import httpx
from metacoin_service.tests.test_service import Instance, free_port, ROOT, ENV
from metacoin_service.tests.test_compute_engine import batch_spec, HAVE_RUNTIME

PY = sys.executable


def write_private(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(data, f)


class McpClient:
    """Runs one conforming MCP client session against a stdio server subprocess (mcp SDK client)."""

    def __init__(self, cred_path, base):
        from mcp.client.stdio import StdioServerParameters
        self.params = StdioServerParameters(command=PY, args=['-m', 'metacoin_service.mcp_server'], cwd=str(ROOT),
                                            env=dict(ENV, METACOIN_MCP_CREDENTIAL_FILE=str(cred_path), METACOIN_MCP_BASE_URL=base))

    async def _session(self, fn):
        from mcp.client.stdio import stdio_client
        from mcp.client.session import ClientSession
        async with stdio_client(self.params) as (r, w):
            async with ClientSession(r, w) as s:
                init = await s.initialize()
                return await fn(s, init)

    def run(self, fn):
        return asyncio.run(self._session(fn))


def content_json(result):
    out = []
    for c in result.content:
        if getattr(c, 'type', None) == 'text':
            try:
                out.append(json.loads(c.text))
            except ValueError:
                out.append(c.text)
    if result.structuredContent is not None:
        return result.structuredContent.get('result', result.structuredContent) if isinstance(result.structuredContent, dict) else result.structuredContent
    return out[0] if len(out) == 1 else out


@unittest.skipUnless(HAVE_RUNTIME, 'no numpy-capable compute interpreter on this host')
class McpTests(unittest.TestCase):
    def setUp(self):
        self.inst = Instance(); self.addCleanup(self.inst.close)
        self.port = free_port(); self.base = 'http://127.0.0.1:%d' % self.port
        self.api = subprocess.Popen([PY, '-m', 'metacoin_service', '--home', str(self.inst.home), 'serve', '--port', str(self.port)], cwd=ROOT, env=ENV, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(self._stop)
        for _ in range(200):
            try:
                if httpx.get(self.base + '/api/health', timeout=1).status_code == 200:
                    break
            except Exception:
                time.sleep(0.1)
        self.http = httpx.Client(base_url=self.base, timeout=60)
        H = {'Authorization': 'Bearer ' + self.inst.tok['owner']}
        scoped = self.http.post('/api/v1/credentials', headers=H, json={'operations': ['contract:create', 'contract:read', 'contract:freeze', 'job:submit', 'job:read', 'job:read_private', 'verification:submit'], 'expires_in_seconds': 3600}).json()
        self.agent_cred = Path(self.inst.temp.name) / 'mcp-agent.json'; write_private(self.agent_cred, {'token': scoped['token']})
        self.viewer_cred = Path(self.inst.temp.name) / 'mcp-viewer.json'; write_private(self.viewer_cred, {'token': self.inst.tok['viewer']})

    def _stop(self):
        self.api.terminate()
        try:
            self.api.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.api.kill()

    def test_separate_client_discovers_submits_verifies_and_read_only_is_refused(self):
        agent = McpClient(self.agent_cred, self.base)

        async def flow(s, init):
            out = {'protocol': init.protocolVersion, 'server': init.serverInfo.name}
            tools = await s.list_tools(); out['tools'] = {t.name: (t.annotations.readOnlyHint if t.annotations else None) for t in tools.tools}
            res = await s.list_resources(); out['resources'] = [str(r.uri) for r in res.resources]
            tmpl = await s.list_resource_templates(); out['templates'] = [t.uriTemplate for t in tmpl.resourceTemplates]
            svc = content_json(await s.call_tool('list_services', {}))
            sid = next(x['id'] for x in svc['items'] if x['kind'] == 'temporal_batch'); out['service_id'] = sid
            out['validate'] = content_json(await s.call_tool('validate_request', {'service_id': sid, 'inputs': batch_spec(private_label='MCP')}))
            out['quote'] = content_json(await s.call_tool('request_quote', {'service_id': sid, 'inputs': batch_spec(private_label='MCP')}))
            out['submit'] = content_json(await s.call_tool('submit_job', {'kind': 'temporal_batch', 'inputs': batch_spec(private_label='MCP'), 'title': 'mcp batch', 'idempotency_key': 'mcp-1'}))
            out['submit_again'] = content_json(await s.call_tool('submit_job', {'kind': 'temporal_batch', 'inputs': batch_spec(private_label='MCP'), 'title': 'mcp batch', 'idempotency_key': 'mcp-1'}))
            out['status'] = content_json(await s.call_tool('job_status', {'job_id': out['submit']['job_id']}))
            bad = await s.call_tool('submit_job', {'kind': 'shell_command', 'inputs': {'cmd': 'ls'}})
            out['bad_kind'] = content_json(bad)
            out['bad_inputs'] = content_json(await s.call_tool('submit_job', {'kind': 'temporal_batch', 'inputs': {'schema': 'x'}}))
            out['schema'] = json.loads((await s.read_resource('metacoin://schemas/temporal_batch')).contents[0].text)
            try:
                r = await s.call_tool('no_such_tool', {})
                out['unknown_tool'] = 'error_result' if r.isError else 'accepted'
            except Exception as exc:
                out['unknown_tool'] = type(exc).__name__
            return out
        out = agent.run(flow)
        self.assertEqual(out['server'], 'metacoin'); self.assertIn(out['protocol'], ('2025-11-25', '2025-06-18', '2025-03-26'))
        self.assertTrue(out['tools']['list_services']); self.assertFalse(out['tools']['submit_job']); self.assertIn('metacoin://services', out['resources'])
        self.assertTrue(any('verification' in t for t in out['templates']))
        self.assertTrue(out['validate'].get('valid', True) is not False, out['validate']); self.assertEqual(out['quote']['quantity_max'], 364)
        self.assertEqual(out['submit']['state'], 'queued'); self.assertEqual(out['submit_again']['job_id'], out['submit']['job_id'])   # idempotent, no second job
        self.assertEqual(out['status']['kind'], 'temporal_batch')
        self.assertEqual((out['bad_kind']['ok'], out['bad_kind']['status']), (False, 422)); self.assertEqual(out['bad_inputs']['status'], 422)
        self.assertNotIn('Traceback', json.dumps(out)); self.assertEqual(out['schema']['schema'], 'temporal-batch-input/v1')
        self.assertNotEqual(out['unknown_tool'], 'accepted')
        # run the job with a real worker process, then request an audit and read the signed statement resource through MCP
        proc = subprocess.run([PY, '-m', 'metacoin_service', '--home', str(self.inst.home), 'worker', '--once'], cwd=ROOT, env=ENV, capture_output=True, text=True, timeout=180)
        self.assertEqual(proc.returncode, 0, proc.stderr[-500:])
        jid = out['submit']['job_id']

        async def audit(s, init):
            st = content_json(await s.call_tool('job_status', {'job_id': jid}))
            pv = content_json(await s.call_tool('verification_preview', {'job_id': jid, 'verification_class': 'analytical'}))
            rq = content_json(await s.call_tool('request_verification', {'job_id': jid, 'verification_class': 'analytical'}))
            return st, pv, rq
        st, pv, rq = agent.run(audit)
        self.assertEqual(st['state'], 'succeeded'); self.assertTrue(pv['affordable']); self.assertEqual(rq['state'], 'queued')
        subprocess.run([PY, '-m', 'metacoin_service', '--home', str(self.inst.home), 'worker', '--once'], cwd=ROOT, env=ENV, capture_output=True, text=True, timeout=180)

        async def statement(s, init):
            v = content_json(await s.call_tool('verification_status', {'verification_id': rq['id']}))
            r = await s.read_resource('metacoin://verification/%s/statement' % rq['id'])
            return v, json.loads(r.contents[0].text)
        v, proj = agent.run(statement)
        self.assertEqual(v['state'], 'passed'); self.assertEqual(proj['statement']['outcome'], 'passed'); self.assertNotIn('mck_', json.dumps(proj))
        # a read-only principal (viewer credential) can discover but every mutation is refused by the server
        viewer = McpClient(self.viewer_cred, self.base)

        async def ro(s, init):
            svc = content_json(await s.call_tool('list_services', {}))
            sub = content_json(await s.call_tool('submit_job', {'kind': 'temporal_batch', 'inputs': batch_spec(), 'title': 'x'}))
            q = content_json(await s.call_tool('request_quote', {'service_id': svc['items'][0]['id'], 'inputs': batch_spec()}))
            rv = content_json(await s.call_tool('request_verification', {'job_id': jid, 'verification_class': 'analytical'}))
            res = content_json(await s.call_tool('job_result', {'job_id': jid}))
            can = content_json(await s.call_tool('cancel_job', {'job_id': jid}))
            return svc, sub, q, rv, res, can
        svc, sub, q, rv, res, can = viewer.run(ro)
        self.assertTrue(svc['items'])
        for r in (sub, q, rv, res, can):
            self.assertEqual((r['ok'], r['status']), (False, 403), r)
        # revoking the agent credential ends its access on the next call
        H = {'Authorization': 'Bearer ' + self.inst.tok['owner']}
        cid = [c for c in self.http.get('/api/v1/credentials', headers=H).json()['items'] if c.get('scope')][0]['id'] if self.http.get('/api/v1/credentials', headers=H).status_code == 200 else None
        if cid:
            self.http.delete('/api/v1/credentials/' + cid, headers=H)
            async def after(s, init):
                return content_json(await s.call_tool('list_jobs', {}))
            self.assertEqual(agent.run(after)['status'], 401)
