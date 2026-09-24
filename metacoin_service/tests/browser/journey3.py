"""Real-browser journey over the new console pages against the live loopback instance (order §36/§47).

    PLAYWRIGHT_BROWSERS_PATH=... PYTHONPATH=. <pwvenv>/bin/python metacoin_service/tests/browser/journey3.py SHOTS_DIR RESULTS_JSON

Logs in through the console form (tokens read from the live bootstrap file, never printed), creates the
objects the pages show through the public API, screenshots every new page for owner and viewer, drives
the drain/resume and grant-stop forms, and checks that private inputs never appear in page text.
"""
import json
import os
import re
import sys
import time
import urllib.request
from playwright.sync_api import sync_playwright

BASE = 'http://127.0.0.1:8402'; HOME = os.path.expanduser('~/.local/state/metacoin-service'); ROOT = os.path.expanduser('~/projects/metacoin')
SHOTS, OUT = sys.argv[1], sys.argv[2]
os.makedirs(SHOTS, exist_ok=True)
creds = json.load(open(HOME + '/credentials/bootstrap.json'))['principals']; tok = {r: e['token'] for r, e in creds.items()}; ids = {r: e['principal_id'] for r, e in creds.items()}
results = []
sys.path.insert(0, ROOT)
from metacoin_service.tests.test_service import own_inputs
from metacoin_service.tests.test_agents import TEMPORAL, policy


def check(name, ok, detail=''):
    results.append({'check': name, 'ok': bool(ok), 'detail': str(detail)[:300]}); print(('PASS ' if ok else 'FAIL ') + name, flush=True)


def api(role, method, path, body=None):
    req = urllib.request.Request(BASE + path, data=None if body is None else json.dumps(body).encode(), method=method, headers={'Authorization': 'Bearer ' + tok[role], 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b'{}')


def login(browser, role):
    ctx = browser.new_context(viewport={'width': 1280, 'height': 900}); page = ctx.new_page()
    page.goto(BASE + '/console/login'); page.fill('#token', tok[role]); page.click('button[type=submit]'); page.wait_for_url(re.compile(r'/console/?$')); return ctx, page


# ---- seed objects through the public API (synthetic, labelled) -----------------------------
csv = "duration_s,harvest_low_mW,harvest_high_mW,load_low_mW,load_high_mW\n10,600,800,500,500\n10,0,0,100,200\n"
st, ds = api('owner', 'POST', '/api/v1/datasets', {'name': 'browser-series', 'kind': 'temporal_series', 'format': 'csv', 'content': csv, 'provenance': 'declared'})
check('dataset version created via API', st == 201, ds.get('version_id'))
definition = {'schema': 'metacoin-workflow/v1', 'name': 'browser temporal pipeline', 'outputs': ['temporal'], 'nodes': [
    {'id': 'data', 'type': 'dataset', 'bind': 'series'},
    {'id': 'temporal', 'type': 'temporal_energy', 'depends_on': ['data'], 'input': 'data', 'parameters': {'capacity': 10000, 'initial_low': 6000, 'initial_high': 6000, 'reserve': 2000}}]}
st, w = api('owner', 'POST', '/api/v1/workflows', {'definition': definition})
st, run = api('owner', 'POST', '/api/v1/workflows/' + w['id'] + '/runs', {'bindings': {'series': ds['version_id']}, 'budget_ceiling': 2})
check('workflow run started via API', st == 202, run.get('run_id'))
st, camp = api('owner', 'POST', '/api/v1/campaigns', {'definition': {'name': 'browser cap sweep', 'kind': 'temporal_energy', 'base': TEMPORAL, 'axes': [{'path': 'capacity', 'values': [5000, 7000, 12000]}]}})
api('owner', 'POST', '/api/v1/campaigns/' + camp['campaign_id'] + '/run', {})
st, grant = api('owner', 'POST', '/api/v1/agents/grants', {'policy': policy()})
check('agent grant issued via API', st == 201, grant.get('grant_id'))
sid = next(s['id'] for s in api('owner', 'GET', '/api/v1/services')[1]['items'] if s['kind'] == 'temporal_energy')
st, q = api('owner', 'POST', '/api/v1/services/' + sid + '/quote', {'inputs': TEMPORAL})
api('owner', 'POST', '/api/v1/quotes/' + q['quote_id'] + '/accept', {})
deadline = time.time() + 90
while time.time() < deadline:                      # the live worker completes the run and the campaign
    st, v = api('owner', 'GET', '/api/v1/runs/' + run['run_id'])
    st, cv = api('owner', 'GET', '/api/v1/campaigns/' + camp['campaign_id'])
    if v['state'] in ('completed', 'blocked', 'failed') and cv['state'] in ('completed', 'cancelled'):
        break
    time.sleep(1)
check('live worker completed the workflow run', v['state'] == 'completed', v['state'])
check('live worker completed the campaign', cv['state'] == 'completed', cv['state'])

with sync_playwright() as p:
    browser = p.chromium.launch()
    octx, page = login(browser, 'owner')
    pages = [('services', '/console/services', 'temporal-energy'), ('datasets', '/console/datasets', 'browser-series'), ('workflows', '/console/workflows', run['run_id']),
             ('run', '/console/runs/' + run['run_id'], 'temporal'), ('campaigns', '/console/campaigns', camp['campaign_id']), ('campaign', '/console/campaigns/' + camp['campaign_id'], 'Candidates'),
             ('agents', '/console/agents', grant['grant_id']), ('usage', '/console/usage', 'Usage and metering'), ('queue', '/console/queue', 'Workers')]
    for i, (name, path, expect) in enumerate(pages, 1):
        page.goto(BASE + path); page.wait_for_load_state()
        html = page.content()
        page.screenshot(path='%s/%02d-owner-%s.png' % (SHOTS, i, name), full_page=True)
        check('owner page ' + name + ' renders expected content', expect in html and 'Traceback' not in html and 'AGENT_PRIVATE' not in html and 'USER_PRIVATE' not in html, path)
    # the campaign plot is served to the session as an SVG image
    r = octx.request.get(BASE + '/api/v1/campaigns/' + camp['campaign_id'] + '/plot.svg')
    check('campaign plot SVG served to the browser session', r.ok and '<svg' in r.text()[:200], r.status)
    # drain and resume the live worker from the queue page (CSRF-protected form), then verify through the API
    page.goto(BASE + '/console/queue'); page.wait_for_load_state()
    before = api('owner', 'GET', '/api/v1/workers')[1]['items']
    live = [w for w in before if w['live'] and w['state'] == 'active']
    if live:
        page.click('form[action$="/drain"] button'); page.wait_for_load_state()
        after = api('owner', 'GET', '/api/v1/workers')[1]['items']
        check('drain from the console changes the live worker state', any(w['state'] == 'draining' for w in after), [w['state'] for w in after])
        page.screenshot(path='%s/10-owner-queue-draining.png' % SHOTS, full_page=True)
        page.click('form[action$="/resume"] button'); page.wait_for_load_state()
        after = api('owner', 'GET', '/api/v1/workers')[1]['items']
        check('resume from the console restores the worker', all(w['state'] != 'draining' for w in after), [w['state'] for w in after])
    else:
        check('drain/resume from the console', False, 'no live active worker registered on the live instance')
    # stop the grant from the agents page
    page.goto(BASE + '/console/agents'); page.wait_for_load_state()
    page.click('form[action$="/' + grant['grant_id'] + '/stop"] button'); page.wait_for_load_state()
    check('grant stopped from the console', api('owner', 'GET', '/api/v1/agents/grants/' + grant['grant_id'])[1]['state'] == 'stopped')
    page.screenshot(path='%s/11-owner-agents-stopped.png' % SHOTS, full_page=True)
    # a viewer sees read-only pages, no controls, no private values
    vctx, vpage = login(browser, 'viewer')
    for i, (name, path, expect) in enumerate(pages, 12):
        vpage.goto(BASE + path); vpage.wait_for_load_state(); html = vpage.content()
        vpage.screenshot(path='%s/%02d-viewer-%s.png' % (SHOTS, i, name), full_page=True)
        check('viewer page ' + name + ' read-only', 'Traceback' not in html and 'Drain' not in html and '>Stop<' not in html and 'AGENT_PRIVATE' not in html and 'USER_PRIVATE' not in html, path)
    # events reach the browser: the SSE stream delivers typed events with ids to the session
    r = vctx.request.get(BASE + '/api/v1/events?after=0&limit=5')
    check('viewer can poll events with a cursor', r.ok and r.json()['items'] and 'seq' in r.json()['items'][0])
    browser.close()

json.dump({'base': BASE, 'checks': results, 'passed': sum(r['ok'] for r in results), 'total': len(results), 'run_id': run['run_id'], 'campaign_id': camp['campaign_id']}, open(OUT, 'w'), indent=1)
print(json.dumps({'passed': sum(r['ok'] for r in results), 'total': len(results)}))
