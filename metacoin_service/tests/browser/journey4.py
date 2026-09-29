"""Real-browser journey over the compute console against the live loopback instance (order §43).

    PLAYWRIGHT_BROWSERS_PATH=... PYTHONPATH=. <pwvenv>/bin/python metacoin_service/tests/browser/journey4.py SHOTS_DIR RESULTS_JSON

Signs in through the console form, opens the Compute overview (backend facts), submits a heat-diffusion job through
the service form after a refused unstable configuration and a validated estimate, watches the job page while the live
worker runs it (phase, work, backend, verification), views the field plot, and checks that a viewer sees no controls,
telemetry or private arrays. Tokens are read from the live bootstrap file and never printed."""
import json
import os
import re
import sys
import time
import urllib.request
from playwright.sync_api import sync_playwright

BASE = 'http://127.0.0.1:8402'; HOME = os.path.expanduser('~/.local/state/metacoin-service')
SHOTS, OUT = sys.argv[1], sys.argv[2]
os.makedirs(SHOTS, exist_ok=True)
creds = json.load(open(HOME + '/credentials/bootstrap.json'))['principals']; tok = {r: e['token'] for r, e in creds.items()}; ids = {r: e['principal_id'] for r, e in creds.items()}
results = []


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


HEAT = {'schema': 'heat-diffusion-input/v1', 'nx': 256, 'ny': 256, 'dx': '0.01', 'dy': '0.01', 'dt': '0.00002', 'alpha': '1.0', 'steps': 3000,
        'boundary': {'type': 'dirichlet', 'values': {'left': '0', 'right': '0', 'top': '0', 'bottom': '0'}},
        'initial': {'type': 'gaussian', 'center_x': '1.28', 'center_y': '1.28', 'sigma': '0.3', 'amplitude': '100', 'background': '0'}, 'snapshots': 2,
        'units': {'field': 'K', 'length': 'm', 'time': 's'}, 'device_policy': 'auto', 'precision': 'float64', 'private_label': 'BROWSER_HEAT_SYNTHETIC'}

with sync_playwright() as p:
    browser = p.chromium.launch()
    octx, page = login(browser, 'owner')
    page.goto(BASE + '/console/compute'); page.wait_for_load_state()
    html = page.content()
    check('compute overview shows backend facts and services', 'gpu verified' in html and 'heat-diffusion-2d/v1' in html and 'Traceback' not in html)
    page.screenshot(path=SHOTS + '/01-compute-overview.png', full_page=True)
    page.goto(BASE + '/console/compute/new?kind=heat_diffusion'); page.wait_for_load_state()
    page.screenshot(path=SHOTS + '/02-heat-form.png', full_page=True)
    page.fill('#inputs', json.dumps(dict(HEAT, dt='0.0001')))
    page.click('button[name=preview]'); page.wait_for_load_state()
    check('unstable timestep is refused in the form with the exact reason', 'unstable timestep' in page.content())
    page.screenshot(path=SHOTS + '/03-heat-refused.png', full_page=True)
    page.fill('#inputs', json.dumps(HEAT)); page.click('button[name=preview]'); page.wait_for_load_state()
    check('validated estimate shows work units and maximum charge', 'work units' in page.content() and 'maximum charge' in page.content())
    page.screenshot(path=SHOTS + '/04-heat-estimate.png', full_page=True)
    page.fill('#inputs', json.dumps(HEAT)); page.click('button[name=submit]'); page.wait_for_url(re.compile(r'/console/jobs/j_'))
    jid = page.url.rsplit('/', 1)[-1]
    check('job submitted from the form', jid.startswith('j_'), jid)
    seen_running = False
    for _ in range(90):
        page.goto(BASE + '/console/jobs/' + jid); page.wait_for_load_state(); html = page.content()
        st, v = api('owner', 'GET', '/api/v1/compute/jobs/' + jid)
        if v.get('phase') in ('running', 'checkpointing') and not seen_running:
            seen_running = True; page.screenshot(path=SHOTS + '/05-heat-running.png', full_page=True)
        if v.get('state') in ('succeeded', 'failed', 'cancelled'):
            break
        time.sleep(1)
    st, v = api('owner', 'GET', '/api/v1/compute/jobs/' + jid)
    check('live worker completed and verified the heat job', v.get('state') == 'succeeded' and v['verification']['passed'], {'backend': v.get('backend'), 'phase': v.get('phase'), 'mode': (v.get('verification') or {}).get('mode')})
    page.goto(BASE + '/console/jobs/' + jid); page.wait_for_load_state(); html = page.content()
    check('job page shows phase, backend, work, checkpoint generation and verification distinctly', all(k in html for k in ('Compute execution', 'backend', 'checkpoint generation', 'verification', 'economic state')))
    check('job page does not leak the private label', 'BROWSER_HEAT_SYNTHETIC' not in html)
    page.screenshot(path=SHOTS + '/06-heat-completed.png', full_page=True)
    r = octx.request.get(BASE + '/api/v1/compute/jobs/' + jid + '/plot.svg')
    check('field plot SVG served to the owner session', r.ok and '<svg' in r.text()[:200] and 'shared scale' in r.text()[:400])
    (open(SHOTS + '/07-heat-plot.svg', 'w')).write(r.text())                       # the served SVG itself, saved as evidence
    r = octx.request.get(BASE + '/api/v1/compute/jobs/' + jid + '/outputs')
    check('owner output listing names npy/json files', r.ok and any(f['name'] == 'field.npy' for f in r.json()['files']))
    # a second job: pause from the console while it runs, then resume
    st, c = api('owner', 'POST', '/api/v1/contracts', {'kind': 'heat_diffusion', 'title': 'browser pause', 'inputs': dict(HEAT, nx=384, ny=384, steps=40000, device_policy='cpu', private_label='BROWSER_PAUSE'), 'policy': {'reviewer_id': ids['reviewer']}})
    api('owner', 'POST', '/api/v1/contracts/' + c['id'] + '/freeze'); st, j2 = api('owner', 'POST', '/api/v1/jobs', {'contract_id': c['id']})
    paused = False
    for _ in range(120):
        st, v2 = api('owner', 'GET', '/api/v1/compute/jobs/' + j2['id'])
        if 'pause' in v2.get('allowed_actions', []) and v2.get('checkpoint_generation', 0) >= 1:
            page.goto(BASE + '/console/jobs/' + j2['id']); page.wait_for_load_state()
            if page.locator('form[action$="/pause"] button').count():
                page.click('form[action$="/pause"] button'); page.wait_for_load_state(); paused = True; break
        if v2.get('state') in ('succeeded', 'failed'):
            break
        time.sleep(0.5)
    for _ in range(60):
        st, v2 = api('owner', 'GET', '/api/v1/compute/jobs/' + j2['id'])
        if v2.get('phase') == 'paused' or v2.get('state') in ('succeeded', 'failed'):
            break
        time.sleep(0.5)
    check('pause from the console lands at a durable checkpoint', paused and v2.get('phase') == 'paused' and v2.get('hold'), {'phase': v2.get('phase'), 'gen': v2.get('checkpoint_generation')})
    page.goto(BASE + '/console/jobs/' + j2['id']); page.wait_for_load_state(); page.screenshot(path=SHOTS + '/08-heat-paused.png', full_page=True)
    if page.locator('form[action$="/resume"] button').count():
        page.click('form[action$="/resume"] button'); page.wait_for_load_state()
    for _ in range(180):
        st, v2 = api('owner', 'GET', '/api/v1/compute/jobs/' + j2['id'])
        if v2.get('state') in ('succeeded', 'failed', 'cancelled'):
            break
        time.sleep(1)
    check('resume from the console completes the job with verification', v2.get('state') == 'succeeded' and v2['verification']['passed'], {'checkpoints': len(v2.get('checkpoints', []))})
    # viewer boundaries
    vctx, vpage = login(browser, 'viewer')
    vpage.goto(BASE + '/console/jobs/' + jid); vpage.wait_for_load_state(); vh = vpage.content()
    check('viewer job page has no controls, telemetry or plot', 'resource observations' not in vh and 'plot.svg' not in vh and '/pause' not in vh and 'Traceback' not in vh)
    vpage.screenshot(path=SHOTS + '/09-viewer-heat.png', full_page=True)
    r = vctx.request.get(BASE + '/api/v1/compute/jobs/' + jid + '/outputs/field.npy'); check('viewer cannot download the field array', r.status == 403)
    r = vctx.request.get(BASE + '/api/v1/compute/jobs/' + jid + '/plot.svg'); check('viewer cannot fetch the plot', r.status == 403)
    r = vctx.request.get(BASE + '/api/v1/compute/jobs/' + jid + '/checkpoints'); check('viewer cannot list checkpoints', r.status == 403)
    vpage.goto(BASE + '/console/compute'); vpage.wait_for_load_state(); check('viewer sees the compute overview read-only', vpage.locator('form').count() <= 1)
    browser.close()

json.dump({'base': BASE, 'checks': results, 'passed': sum(r['ok'] for r in results), 'total': len(results), 'jobs': [jid, j2['id']]}, open(OUT, 'w'), indent=1)
print(json.dumps({'passed': sum(r['ok'] for r in results), 'total': len(results)}))
