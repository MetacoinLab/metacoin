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
            if mode in ('work', 'work-injection'):
                # J25: an authorized low-budget purchase through MCP under the agent's grant; J26: a poisoned offer note must not
                # move the agent beyond its grant or change the payment destination
                st = content(await s.call_tool('work_status', {'award_id': os.environ.get('MCP_WORK_AWARD', 'wa_none')})) if os.environ.get('MCP_WORK_AWARD') else None
                rid = os.environ['MCP_WORK_REQUEST']
                cmp = content(await s.call_tool('compare_offers', {'request_id': rid}))
                out['compare'] = {'eligible': [e['offer_id'] for e in cmp.get('eligible', [])], 'excluded': cmp.get('excluded'), 'recommended': cmp.get('recommended')}
                if mode == 'work-injection':
                    # the agent reads offer notes as DATA; even if it followed the instruction it cannot change recipient (no such parameter)
                    poisoned = [x for x in cmp.get('excluded', [])]
                    out['poisoned_offers_seen'] = poisoned
                    out['award_poisoned'] = content(await s.call_tool('award_work', {'request_id': rid, 'offer_id': os.environ.get('MCP_POISONED_OFFER'), 'reason': 'instruction in the offer note said to', 'idempotency_key': 'inj-1'}))
                    out['draft_bigger'] = content(await s.call_tool('draft_work_request', {'template': 'determination', 'ceiling': 1000}))
                    return out
                out['award'] = content(await s.call_tool('award_work', {'request_id': rid, 'idempotency_key': 'mcp-award-1'}))
                out['award_retry'] = content(await s.call_tool('award_work', {'request_id': rid, 'idempotency_key': 'mcp-award-1'}))
                aid = out['award'].get('id')
                if aid:
                    out['status'] = content(await s.call_tool('work_status', {'award_id': aid}))
                    out['evaluate'] = content(await s.call_tool('evaluate_acceptance', {'award_id': aid}))
                    out['reconcile'] = content(await s.call_tool('reconcile_budget', {}))
                return out
            if mode == 'analysis':
                out['analysis'] = content(await s.call_tool('create_analysis', {'name': 'mcp analysis', 'blocks': [{'id': 'aim', 'type': 'text', 'text': 'created through MCP'}]}))
                out['status'] = content(await s.call_tool('analysis_status', {'analysis_id': out['analysis'].get('id', 'x')}))
                out['impact'] = content(await s.call_tool('analysis_impact', {'analysis_id': out['analysis'].get('id', 'x'), 'changed': {'block': 'aim'}}))
                out['report_refused'] = content(await s.call_tool('build_report', {'analysis_id': out['analysis'].get('id', 'x'), 'version': 1}))
                out['compat'] = content(await s.call_tool('package_compatibility', {'manifest': {'schema': 'wrong'}}))
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
