"""An MCP-driven bounded job: a separate MCP client (stdio) discovers services, drafts and validates a plan, submits a
bounded temporal batch, follows it and reads the permitted summary resource. Needs the `mcp` package."""
import asyncio
import json
import os
import sys

try:
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client, StdioServerParameters
except ImportError:
    sys.exit(json.dumps({'missing_dependency': 'mcp', 'install': 'pip install mcp'}))

from _client import BATCH_SPEC
BASE = os.environ.get('METACOIN_BASE_URL', 'http://127.0.0.1:8402')
CRED = os.environ.get('METACOIN_CREDENTIAL_FILE')
SPEC = BATCH_SPEC


def content(result):
    return json.loads(result.content[0].text)


async def main():
    if not CRED:
        sys.exit(json.dumps({'error': 'METACOIN_CREDENTIAL_FILE not set'}))
    params = StdioServerParameters(command=sys.executable, args=['-m', 'metacoin_service.mcp_server'], env=dict(os.environ, METACOIN_MCP_CREDENTIAL_FILE=CRED, METACOIN_MCP_BASE_URL=BASE))
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            init = await s.initialize()
            tools = [t.name for t in (await s.list_tools()).tools]
            services = content(await s.call_tool('list_services', {}))
            plan = content(await s.call_tool('create_plan', {'goal': 'sweep the reserve of the example battery', 'service_kind': 'temporal_batch', 'inputs': SPEC}))
            submitted = content(await s.call_tool('submit_job', {'kind': 'temporal_batch', 'inputs': SPEC, 'title': 'mcp example', 'idempotency_key': 'example-mcp-1'}))
            status = content(await s.call_tool('job_status', {'job_id': submitted['job_id']}))
            summary = json.loads((await s.read_resource('metacoin://jobs/%s/summary' % submitted['job_id'])).contents[0].text)
            print(json.dumps({'protocol': init.protocolVersion, 'tools': len(tools), 'services': len(services.get('items', [])), 'plan': {'id': plan.get('id'), 'valid': plan.get('valid'), 'readable': plan.get('readable')},
                              'job_id': submitted['job_id'], 'state': status.get('state'), 'summary_keys': sorted(summary)[:8], 'meaning': 'the plan validated the request without executing; submit_job created one bounded job under the credential\'s own grants'}, indent=1))

asyncio.run(main())
