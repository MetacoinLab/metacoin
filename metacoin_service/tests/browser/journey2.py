"""Second real-browser pass: session expiry, service restart mid-session, a failed job, forbidden
actions with separate identities, and absence of private data in unauthorized responses."""
import json, os, re, sqlite3, subprocess, sys, time
from playwright.sync_api import sync_playwright
BASE = 'http://127.0.0.1:8402'; HOME = os.path.expanduser('~/.local/state/metacoin-service'); ROOT = os.path.expanduser('~/projects/metacoin')
SHOTS = 'work/real-features-session/browser/shots'
creds = json.load(open(HOME + '/credentials/bootstrap.json'))['principals']; tok = {r: e['token'] for r, e in creds.items()}
INPUTS = {"available_low": 900000, "available_high": 950000, "reserve": 100000, "segments": [{"duration": 600, "power_low": 800, "power_high": 1000}],
          "units": {"energy": "mJ", "power": "mW", "duration": "s"}, "assumptions": ["no_recharge", "usable_energy_at_load_boundary", "piecewise_constant_power_bounds", "no_unmodeled_loads"],
          "provenance": "declared_unverified", "private_label": "SECOND_PASS_PRIVATE_5566"}
findings = []
def check(name, ok, detail=''):
    findings.append({'check': name, 'ok': bool(ok), 'detail': detail}); print(('PASS ' if ok else 'FAIL ') + name + ('' if ok else ' — ' + detail))
def login(browser, role):
    ctx = browser.new_context(viewport={'width': 1280, 'height': 900}); page = ctx.new_page()
    page.goto(BASE + '/console/login'); page.fill('#token', tok[role]); page.click('button[type=submit]'); page.wait_for_url(re.compile(r'/console/?$')); return ctx, page
def tmux_restart():
    subprocess.run(['tmux', 'kill-session', '-t', 'metacoin-service'], check=False)
    for line in subprocess.run(['pgrep', '-f', r'^\.venv-service/bin/python -m metacoin_service --home /home/zhangd2/.local'], capture_output=True, text=True).stdout.split():
        subprocess.run(['kill', line])
    time.sleep(1)
    cmd = 'PYTHONPATH=. .venv-service/bin/python -m metacoin_service --home %s --provider-mode test-http ' % HOME
    subprocess.run(['tmux', 'new-session', '-d', '-s', 'metacoin-service', '-c', ROOT, cmd + 'serve --port 8402 >> %s/logs/api.log 2>&1' % HOME], check=True)
    subprocess.run(['tmux', 'new-window', '-t', 'metacoin-service', '-c', ROOT, cmd + 'worker >> %s/logs/worker.log 2>&1' % HOME], check=True)
    import urllib.request
    for _ in range(50):
        try:
            urllib.request.urlopen(BASE + '/api/health', timeout=1); return
        except Exception:
            time.sleep(0.2)
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    octx, page = login(browser, 'owner')
    # 1. job list before restart, create a job, restart API+worker while the browser session is open
    page.click('a:has-text("New analysis")'); page.wait_for_selector('#inputs'); page.fill('#title', 'restart survivor'); page.fill('#inputs', json.dumps(INPUTS))
    page.click('button[name=freeze_and_submit]'); page.wait_for_url(re.compile(r'/console/jobs/')); job_url = page.url; jid = job_url.rsplit('/', 1)[1]
    tmux_restart()
    page.goto(job_url)
    check('browser session survives a service restart (server-side session)', '/console/login' not in page.url and 'restart survivor' in page.content())
    deadline = time.time() + 60
    while time.time() < deadline and 'execution: succeeded' not in page.content():
        time.sleep(1); page.reload()
    check('job queued before the restart completes after it', 'execution: succeeded' in page.content())
    check('history shows queued/claimed/committed in order', re.search(r'job\.queued.*job\.claimed.*job\.result_committed', page.content(), re.S) is not None)
    # 2. failed job: a one-off worker process with a tiny timeout fails a fresh job (three attempts: two retries then terminal)
    page.click('a:has-text("New analysis")'); page.wait_for_selector('#inputs'); page.fill('#title', 'will time out'); page.fill('#inputs', json.dumps(INPUTS))
    subprocess.run(['tmux', 'kill-window', '-t', 'metacoin-service:1'], check=False)   # stop the live worker so the tiny-timeout worker claims it
    page.click('button[name=freeze_and_submit]'); page.wait_for_url(re.compile(r'/console/jobs/')); fail_url = page.url
    env = dict(os.environ, PYTHONPATH=ROOT, METACOIN_LIMITS_JSON='{"job_timeout_seconds": 0.001}')
    outcomes = []
    for _ in range(3):
        out = subprocess.run([ROOT + '/.venv-service/bin/python', '-m', 'metacoin_service', '--home', HOME, '--provider-mode', 'test-http', 'worker', '--once'], cwd=ROOT, env=env, capture_output=True, text=True)
        outcomes.append(json.loads(out.stdout)['ran'][1] if out.stdout.strip() else out.stderr[-200:])
    page.goto(fail_url); html = page.content()
    check('failed job shows a safe error and retries (' + ','.join(map(str, outcomes)) + ')', 'execution: failed' in html and 'TIMEOUT' in html and 'Traceback' not in html and 'SECOND_PASS_PRIVATE_5566' not in html)
    page.screenshot(path=SHOTS + '/09-failed-job.png', full_page=True)
    subprocess.run(['tmux', 'new-window', '-t', 'metacoin-service', '-c', ROOT, 'PYTHONPATH=. .venv-service/bin/python -m metacoin_service --home %s --provider-mode test-http worker >> %s/logs/worker.log 2>&1' % (HOME, HOME)], check=True)
    page.goto(BASE + '/console/?state=failed'); check('work-list filter shows the failed job', 'will time out' in page.content() and 'restart survivor' not in page.content())
    # 3. session expiry: expire the owner's session server-side, then navigate
    with sqlite3.connect(HOME + '/service.sqlite') as db:
        db.execute("UPDATE sessions SET expires_at=1 WHERE revoked_at IS NULL AND principal_id=?", (creds['owner']['principal_id'],)); db.commit()
    page.goto(job_url)
    check('expired session redirects to login with the expiry notice', page.url.endswith('/console/login?expired=1') and 'session expired' in page.content())
    r = octx.request.get(BASE + '/api/v1/jobs/' + jid + '/result')
    check('expired session API call is 401 without private data', r.status == 401 and 'SECOND_PASS_PRIVATE_5566' not in r.text())
    page.screenshot(path=SHOTS + '/10-expired.png', full_page=True)
    # 4. separate identities: reviewer cannot submit/cancel/act; viewer sees nothing private; worker credential cannot use the console for owner actions
    rctx, rpage = login(browser, 'reviewer')
    rpage.goto(job_url); rh = rpage.content()
    check('reviewer sees no owner-only operations', 'Request review' not in rh and 'Dispatch bounded action' not in rh and 'Cancel' not in rh)
    for path, want in (('/api/v1/jobs', 'POST'), ('/api/v1/actions', 'POST'), ('/api/v1/jobs/' + jid + '/cancel', 'POST'), ('/api/v1/credentials', 'POST')):
        r = rctx.request.post(BASE + path, data='{}', headers={'Content-Type': 'application/json', 'X-CSRF-Token': 'wrong'})
        check('reviewer ' + path + ' refused (' + str(r.status) + ')', r.status in (403,))
    vctx, vpage = login(browser, 'viewer')
    r = vctx.request.get(BASE + '/api/v1/jobs/' + jid)
    body = r.json()
    check('viewer job view withholds outcome before review and carries no private values', body.get('outcome') in (None, 'withheld-by-policy-or-not-yet-reviewed') and body.get('summary') is None and 'SECOND_PASS_PRIVATE_5566' not in r.text())
    vpage.goto(job_url); check('viewer job page has no private numbers', 'worst-case margin' not in vpage.content() and 'SECOND_PASS_PRIVATE_5566' not in vpage.content())
    r = vctx.request.get(BASE + '/api/v1/history'); check('viewer history carries no private values', r.ok and 'SECOND_PASS_PRIVATE_5566' not in r.text() and 'available_low' not in r.text())
    wctx = browser.new_context(); wpage = wctx.new_page(); wpage.goto(BASE + '/console/login'); wpage.fill('#token', tok['worker']); wpage.click('button[type=submit]'); wpage.wait_for_load_state()
    wpage.goto(BASE + '/console/contracts/new'); check('worker identity cannot open the owner contract form', 'FORBIDDEN' in wpage.content())
    r = wctx.request.get(BASE + '/api/v1/jobs/' + jid + '/result'); check('worker identity cannot read private results over the API', r.status == 403)
    # 5. stale/guessed URLs and alternate methods on a private export
    arts = login(browser, 'owner')[0].request.get(BASE + '/api/v1/jobs/' + jid + '/artifacts').json()['items']
    priv = [a['id'] for a in arts if a['kind'] == 'evidence_vault'][0]
    for method in ('get', 'post', 'head', 'put', 'delete'):
        r = getattr(vctx.request, method)(BASE + '/api/v1/artifacts/' + priv + '/export')
        check('viewer %s private export refused (%d)' % (method.upper(), r.status), r.status in (403, 405) and 'age-encryption' not in r.text())
    r = browser.new_context().request.get(BASE + '/api/v1/artifacts/' + priv + '/export', headers={'Range': 'bytes=0-10'})
    check('anonymous ranged request refused', r.status == 401)
    browser.close()
json.dump({'findings': findings, 'passed': sum(f['ok'] for f in findings), 'failed': sum(not f['ok'] for f in findings)}, open('work/real-features-session/browser/journey2-results.json', 'w'), indent=2)
print('passed', sum(f['ok'] for f in findings), 'failed', sum(not f['ok'] for f in findings))
