"""Separate conforming MCP client process for the journeys: discovers a service, submits permitted work, queries
status, requests a verification, reads a resource; or, as a read-only principal, attempts mutations.

    python -m metacoin_service.tests.mcp_journey_client BASE CRED_FILE agent|readonly"""
import asyncio
import json
import os
import sys
from pathlib import Path

from metacoin_service.tests.test_compute_engine import batch_spec
from metacoin_service.tests.test_service import ROOT, ENV


def content(result):
    if result.structuredContent is not None and isinstance(result.structuredContent, dict):
        return result.structuredContent.get('result', result.structuredContent)
    for c in result.content:
        if getattr(c, 'type', None) == 'text':
            try:
                return json.loads(c.text)
            except ValueError:
                return c.text
    return None


async def run(base, cred, mode):
    from mcp.client.stdio import stdio_client, StdioServerParameters
    from mcp.client.session import ClientSession
    params = StdioServerParameters(command=sys.executable, args=['-m', 'metacoin_service.mcp_server'], cwd=str(ROOT), env=dict(ENV, METACOIN_MCP_CREDENTIAL_FILE=cred, METACOIN_MCP_BASE_URL=base))
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            init = await s.initialize()
            tools = await s.list_tools()
            out = {'protocol': init.protocolVersion, 'tool_count': len(tools.tools)}
            svc = content(await s.call_tool('list_services', {}))
            if mode == 'readonly':
                out['list_ok'] = bool(svc.get('items'))
                refusals = []
                for tool, args in (('submit_job', {'kind': 'temporal_batch', 'inputs': batch_spec(), 'title': 'ro'}), ('request_quote', {'service_id': svc['items'][0]['id'], 'inputs': batch_spec()}),
                                   ('cancel_job', {'job_id': 'j_nonexistent'}), ('request_verification', {'job_id': 'j_nonexistent', 'verification_class': 'analytical'})):
                    r = content(await s.call_tool(tool, args))
                    refusals.append({'tool': tool, 'status': r.get('status'), 'code': r.get('code')})
                out['refusals'] = refusals
                return out
            sid = next(x['id'] for x in svc['items'] if x['kind'] == 'temporal_batch')
            out['quote'] = content(await s.call_tool('request_quote', {'service_id': sid, 'inputs': batch_spec(private_label='MCP_J16')}))
            out['submitted'] = content(await s.call_tool('submit_job', {'kind': 'temporal_batch', 'inputs': batch_spec(private_label='MCP_J16'), 'title': 'mcp journey', 'idempotency_key': 'j16-1'}))
            out['status'] = content(await s.call_tool('job_status', {'job_id': out['submitted']['job_id']}))
            # the worker runs outside this client; the caller polls afterwards. Request the verification now (queued until the job completes -> preview refuses); so read a resource instead
            res = await s.read_resource('metacoin://jobs/%s/summary' % out['submitted']['job_id'])
            out['summary_read'] = bool(json.loads(res.contents[0].text).get('id'))
            return out


async def verify_after(base, cred, job_id):
    from mcp.client.stdio import stdio_client, StdioServerParameters
    from mcp.client.session import ClientSession
    params = StdioServerParameters(command=sys.executable, args=['-m', 'metacoin_service.mcp_server'], cwd=str(ROOT), env=dict(ENV, METACOIN_MCP_CREDENTIAL_FILE=cred, METACOIN_MCP_BASE_URL=base))
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            return content(await s.call_tool('request_verification', {'job_id': job_id, 'verification_class': 'analytical'}))


if __name__ == '__main__':
    base, cred, mode = sys.argv[1:4]
    out = asyncio.run(run(base, cred, mode))
    if mode == 'agent':
        # wait for a worker (started by the journey harness) to finish the job, then request the audit through MCP
        import time, urllib.request
        token = json.load(open(cred))['token']
        deadline = time.time() + 240
        while time.time() < deadline:
            req = urllib.request.Request(base + '/api/v1/jobs/' + out['submitted']['job_id'], headers={'Authorization': 'Bearer ' + token})
            state = json.loads(urllib.request.urlopen(req, timeout=10).read())['state']
            if state in ('succeeded', 'failed', 'cancelled'):
                break
            time.sleep(1)
        if state == 'succeeded':
            v = asyncio.run(verify_after(base, cred, out['submitted']['job_id']))
            out['verification_id'] = v.get('id'); out['verification_state'] = v.get('state')
    print(json.dumps(out, default=str))
