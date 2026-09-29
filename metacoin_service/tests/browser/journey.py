"""Real-browser journey (Playwright + headless Chromium) against the running console.
Credentials are read from the private bootstrap file and never printed."""
import json, os, re, sys, time
from playwright.sync_api import sync_playwright
BASE = sys.argv[1] if len(sys.argv) > 1 else 'http://127.0.0.1:8402'
HOME = os.path.expanduser(sys.argv[2] if len(sys.argv) > 2 else '~/.local/state/metacoin-service')
SHOTS = sys.argv[3] if len(sys.argv) > 3 else 'work/real-features-session/browser/shots'
os.makedirs(SHOTS, exist_ok=True)
creds = json.load(open(HOME + '/credentials/bootstrap.json'))['principals']
tok = {r: e['token'] for r, e in creds.items()}
NEW_INPUTS = {"available_low": 5000000, "available_high": 5400000, "reserve": 250000,
              "segments": [{"duration": 1800, "power_low": 1500, "power_high": 2200}, {"duration": 600, "power_low": 300, "power_high": 700}],
              "units": {"energy": "mJ", "power": "mW", "duration": "s"},
              "assumptions": ["no_recharge", "usable_energy_at_load_boundary", "piecewise_constant_power_bounds", "no_unmodeled_loads"],
              "provenance": "declared_unverified", "private_label": "BROWSER_PRIVATE_LABEL_9312"}
findings = []
def check(name, ok, detail=''):
    findings.append({'check': name, 'ok': bool(ok), 'detail': detail})
    print(('PASS ' if ok else 'FAIL ') + name + (' — ' + detail if detail and not ok else ''))

def login(ctx, role, width=1280):
    page = ctx.new_page()
    page.goto(BASE + '/console/login')
    page.fill('#token', tok[role])
    page.click('button[type=submit]')
    page.wait_for_url(re.compile(r'/console/?$'))
    return page

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    owner_ctx = browser.new_context(viewport={'width': 1280, 'height': 900})
    page = login(owner_ctx, 'owner')
    check('owner login lands on work list', 'Work list' in page.content())
    page.screenshot(path=SHOTS + '/01-worklist.png', full_page=True)
    # create contract with NEW inputs (replace the sample), freeze and submit in one step
    page.click('a:has-text("New analysis")')
    page.wait_for_selector('#inputs')
    check('contract form shows units and policy', all(s in page.content() for s in ('millijoules', 'Acceptance policy', 'Freeze consequences')))
    page.fill('#title', 'browser instrument run')
    page.fill('#inputs', json.dumps(NEW_INPUTS))
    page.screenshot(path=SHOTS + '/02-contract-form.png', full_page=True)
    page.click('button[name=freeze_and_submit]')
    page.wait_for_url(re.compile(r'/console/jobs/'))
    job_url = page.url; job_id = job_url.rsplit('/', 1)[1]
    check('freeze+submit redirects to the job page', bool(job_id.startswith('j_')), job_url)
    # observe completion (page auto-refreshes every 3 s while queued/running)
    deadline = time.time() + 60
    while time.time() < deadline:
        html = page.content()
        if 'execution: succeeded' in html or 'execution: failed' in html:
            break
        time.sleep(1); page.reload()
    check('worker completed the job', 'execution: succeeded' in page.content())
    page.screenshot(path=SHOTS + '/03-job-done.png', full_page=True)
    html = page.content()
    # hand check: required_high = 250000 + 2200*1800 + 700*600 = 4630000; available_low 5000000 -> FEASIBLE
    check('owner sees verdict and private margins', 'FEASIBLE' in html and 'worst-case margin' in html and '370000' in html, 'expected worst margin 370000 mJ')
    check('interval chart present with accessible label', 'aria-label="margin interval' in html)
    check('private label absent from owner page (label is not rendered anywhere)', 'BROWSER_PRIVATE_LABEL_9312' not in html)
    page.click('button:has-text("Request review")')
    page.wait_for_load_state()
    check('review requested', 'review: requested' in page.content())
    # reviewer
    rev_ctx = browser.new_context(viewport={'width': 1280, 'height': 900})
    rpage = login(rev_ctx, 'reviewer')
    rpage.goto(BASE + '/console/reviews/' + job_id)
    rhtml = rpage.content()
    check('reviewer page shows pin, roots, verifier and policy', all(s in rhtml for s in ('contract pin', 'input root', 'evidence root', 'verifier', 'acceptance policy')))
    check('reviewer sees recomputation matches', 'Recomputation: <strong>matches</strong>' in rhtml)
    rpage.screenshot(path=SHOTS + '/04-review.png', full_page=True)
    rpage.click('button[value=accepted]')
    rpage.wait_for_load_state()
    check('decision recorded', 'Decision already recorded: <strong>accepted</strong>' in rpage.content())
    # verify signature through the API from the reviewer session (cookie auth) and via /reviews/verify
    api = rev_ctx.request.get(BASE + '/api/v1/reviews/' + job_id)
    rv = api.json()
    check('review signature verifies against the trust table', rv['verification']['valid'] and rv['verification']['key_status'] == 'current')
    tampered = dict(rv['envelope'], decision='rejected')
    bad = rev_ctx.request.post(BASE + '/api/v1/reviews/verify', data=json.dumps({'envelope': tampered, 'signature_hex': rv['signature_hex']}),
                               headers={'Content-Type': 'application/json', 'X-CSRF-Token': 'x'}).json()
    check('tampered envelope refused', bad.get('valid') is False)
    # owner: job page after review; export permitted evidence (public bundle) and a private vault (ciphertext)
    page.goto(job_url); html = page.content()
    check('owner job page shows signed review and payment states separately', 'Signed review' in html and 'payment: NOT_REQUESTED' in html)
    links = page.locator('a:has-text("export")')
    exports = [links.nth(i).get_attribute('href') for i in range(links.count())]
    pub = [h for h in exports if True]
    # find the public bundle artifact via API to export it
    arts = owner_ctx.request.get(BASE + '/api/v1/jobs/' + job_id + '/artifacts').json()['items']
    pub_id = [a['id'] for a in arts if a['public']][0]; priv_id = [a['id'] for a in arts if a['kind'] == 'evidence_vault'][0]
    pub_resp = owner_ctx.request.get(BASE + '/api/v1/artifacts/' + pub_id + '/export')
    check('public bundle export is JSON with receipt and disclosures', pub_resp.ok and sorted(json.loads(pub_resp.text())) == ['disclosures', 'receipt'])
    check('public bundle carries no private label or margins', 'BROWSER_PRIVATE_LABEL_9312' not in pub_resp.text() and 'worst_margin' not in pub_resp.text())
    priv_resp = owner_ctx.request.get(BASE + '/api/v1/artifacts/' + priv_id + '/export')
    check('private export is age ciphertext', priv_resp.ok and priv_resp.body()[:19] == b'age-encryption.org/')
    # dry run and dispatch a bounded action from the console
    page.fill('#rid', 'req-browser-' + job_id[-6:])
    page.click('button[name=dry_run]'); page.wait_for_load_state()
    check('dry run leaves payment NOT_REQUESTED', 'payment: NOT_REQUESTED' in page.content())
    page.click('form[action$="/action"] button:not([name=dry_run])'); page.wait_for_load_state()
    html = page.content()
    check('dispatch records a payment state on the job page', re.search(r'payment: (CONFIRMED|OUTCOME_UNKNOWN|SUBMISSION_PENDING|FAILED_CONFIRMED)', html) is not None, re.search(r'payment: [A-Z_]+', html).group(0) if re.search(r'payment: [A-Z_]+', html) else 'none')
    page.goto(BASE + '/console/budget'); bhtml = page.content()
    check('budget page separates states', all(s in bhtml for s in ('reserved', 'submission pending', 'confirmed', 'failed (released)', 'unresolved')))
    page.screenshot(path=SHOTS + '/05-budget.png', full_page=True)
    # viewer: forbidden actions and no private data
    v_ctx = browser.new_context(viewport={'width': 1280, 'height': 900})
    vpage = login(v_ctx, 'viewer')
    vpage.goto(job_url); vhtml = vpage.content()
    check('viewer sees outcome (policy discloses it) but no private margins', 'FEASIBLE' in vhtml and 'worst-case margin' not in vhtml and 'BROWSER_PRIVATE_LABEL_9312' not in vhtml)
    check('viewer has no operation buttons', 'Request review' not in vhtml and 'Dispatch bounded action' not in vhtml)
    vpage.goto(BASE + '/console/reviews/' + job_id); check('viewer review page refused', 'FORBIDDEN' in vpage.content())
    vpage.goto(BASE + '/console/contracts/new'); check('viewer cannot open contract form', 'FORBIDDEN' in vpage.content())
    for path, want in (('/api/v1/jobs/' + job_id + '/result', 403), ('/api/v1/reviews/' + job_id + '/evidence', 403), ('/api/v1/artifacts/' + priv_id + '/export', 403), ('/api/v1/contracts/' + job_id.replace('j_', 'ct_') + '/inputs', 403)):
        r = v_ctx.request.get(BASE + path)
        check('viewer API ' + path.split('/')[-1] + ' -> ' + str(want), r.status == want and 'BROWSER_PRIVATE_LABEL_9312' not in r.text(), str(r.status))
    forged = v_ctx.request.post(BASE + '/api/v1/reviews/' + job_id + '/decision', data=json.dumps({'decision': 'accepted', 'role': 'reviewer'}), headers={'Content-Type': 'application/json'})
    check('viewer cannot record a decision even with a role field', forged.status in (403,))
    vpage.screenshot(path=SHOTS + '/06-viewer-job.png', full_page=True)
    # narrow viewport
    n_ctx = browser.new_context(viewport={'width': 400, 'height': 800})
    npage = login(n_ctx, 'owner')
    npage.goto(job_url); npage.screenshot(path=SHOTS + '/07-narrow-job.png', full_page=True)
    check('narrow viewport has no horizontal overflow', npage.evaluate('document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1'))
    # sign out then access -> redirected to login with expiry note
    page.goto(BASE + '/console/'); page.click('button:has-text("Sign out")'); page.wait_for_load_state()
    page.goto(job_url)
    check('after sign-out the job page redirects to login', '/console/login' in page.url)
    # failed job: invalid domain input (reserve negative is refused at form time; a timeout cannot be provoked from the UI) -> form validation message
    page2 = login(owner_ctx, 'owner')
    page2.click('a:has-text("New analysis")'); page2.wait_for_selector('#inputs')
    page2.fill('#title', 'bad input'); page2.fill('#inputs', json.dumps(dict(NEW_INPUTS, available_low=9, available_high=1)))
    page2.click('button[name=freeze_and_submit]'); page2.wait_for_load_state()
    check('invalid input is refused with a safe code, not a trace', 'MODEL_DOMAIN' in page2.content() and 'Traceback' not in page2.content() and 'BROWSER_PRIVATE_LABEL_9312' not in page2.content())
    page2.screenshot(path=SHOTS + '/08-refusal.png', full_page=True)
    browser.close()
json.dump({'job_id': job_id, 'findings': findings, 'passed': sum(f['ok'] for f in findings), 'failed': sum(not f['ok'] for f in findings)},
          open(SHOTS + '/../journey-results.json', 'w'), indent=2)
print('passed', sum(f['ok'] for f in findings), 'failed', sum(not f['ok'] for f in findings))
