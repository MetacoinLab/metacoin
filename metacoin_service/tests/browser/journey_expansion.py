"""Real-browser console journeys for the 24-hour expansion (order §57/§59-19) against a running instance with
isolated identities, at desktop and narrow widths.

    PLAYWRIGHT_BROWSERS_PATH=... PYTHONPATH=. <pwvenv>/bin/python metacoin_service/tests/browser/journey_expansion.py BASE OWNER_CRED REVIEWER_CRED VIEWER_CRED SHOTS_DIR

Owner: Models page (readiness facts), Knowledge (create collection, add document, lexical search, answer), Calibration,
Verification (preview + request), Nodes, Approvals (policy + proposal), Statement. Reviewer: approves a proposal and
sees verification. Viewer: no Knowledge/Statement, no mutation forms, no private fields in the HTML. Narrow width:
no horizontal overflow on the main pages. Tokens come from private credential files and are never printed."""
import json
import os
import re
import sys
import time
import urllib.request
from playwright.sync_api import sync_playwright

BASE, OWNER, REVIEWER, VIEWER, SHOTS = sys.argv[1:6]
os.makedirs(SHOTS, exist_ok=True)
tok = {r: json.load(open(p))['token'] for r, p in (('owner', OWNER), ('reviewer', REVIEWER), ('viewer', VIEWER))}
results = []


def check(name, ok, detail=''):
    results.append({'check': name, 'ok': bool(ok), 'detail': str(detail)[:300]}); print(('PASS ' if ok else 'FAIL ') + name, flush=True)


def api(role, method, path, body=None):
    req = urllib.request.Request(BASE + path, data=None if body is None else json.dumps(body).encode(), method=method, headers={'Authorization': 'Bearer ' + tok[role], 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b'{}')


def login(browser, role, width=1280):
    ctx = browser.new_context(viewport={'width': width, 'height': 900}); page = ctx.new_page()
    page.goto(BASE + '/console/login'); page.fill('#token', tok[role]); page.click('button[type=submit]'); page.wait_for_url(re.compile(r'/console/?$')); return ctx, page


def no_overflow(page):
    return page.evaluate('document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1')


with sync_playwright() as p:
    browser = p.chromium.launch()
    octx, page = login(browser, 'owner')
    page.goto(BASE + '/console/models'); page.wait_for_load_state(); html = page.content()
    check('models page shows runtime facts, readiness and defaults', 'Runtime facts' in html and 'readiness' in html and 'Traceback' not in html)
    page.screenshot(path=SHOTS + '/01-models.png', full_page=True)
    page.goto(BASE + '/console/knowledge'); page.wait_for_load_state()
    page.fill('#cname', 'browser notes'); page.click('button:has-text("Create")'); page.wait_for_load_state()
    cid = [c for c in api('owner', 'GET', '/api/v1/knowledge/collections')[1]['items'] if c['name'] == 'browser notes'][0]['id']
    page.fill('#dn-' + cid, 'notes.md'); page.select_option('#df-' + cid, 'markdown'); page.fill('#dc-' + cid, '# Browser note\n\nThe demonstration reserve is 2000 mJ and the console listens on port 8402.\n')
    page.click('form[action$="/collections/' + cid + '/documents"] button.secondary'); page.wait_for_load_state()
    check('document added through the console form', 'notes.md' in page.content())
    page.fill('#q-' + cid, 'demonstration reserve'); page.select_option('#m-' + cid, 'lexical'); page.click('form[action$="/collections/' + cid + '/search"] button.secondary'); page.wait_for_load_state()
    html = page.content()
    check('lexical search renders authorized chunks with score meaning', '2000 mJ' in html and 'BM25' in html)
    page.screenshot(path=SHOTS + '/02-knowledge-search.png', full_page=True)
    st, ready = api('owner', 'GET', '/api/v1/models/runtime')
    if 'generate' in ready.get('defaults', {}):
        page.fill('#q-' + cid, 'What is the demonstration reserve?'); page.click('button[name=mode_answer][value=extractive]'); page.wait_for_url(re.compile(r'/console/jobs/j_'))
        jid = page.url.rsplit('/', 1)[-1]
        for _ in range(120):
            st, j = api('owner', 'GET', '/api/v1/jobs/' + jid)
            if j.get('state') in ('succeeded', 'failed', 'cancelled'):
                break
            time.sleep(1)
        st, a = api('owner', 'GET', '/api/v1/knowledge/answers/by-job/' + jid)
        page.goto(BASE + '/console/knowledge/answers/' + a.get('id', 'x')); page.wait_for_load_state(); html = page.content()
        check('answer page shows status, passages and citations with source previews', a.get('status') == 'answered' and 'source preview' in html and '2000' in html, a.get('status'))
        page.screenshot(path=SHOTS + '/03-answer.png', full_page=True)
    else:
        check('answer page (skipped: no promoted generation model on this instance)', True, 'skipped')
    page.goto(BASE + '/console/calibration'); page.wait_for_load_state(); html = page.content()
    check('calibration page shows scheduling state, datasets and models', 'Calibrated scheduling' in html and 'Models' in html)
    page.screenshot(path=SHOTS + '/04-calibration.png', full_page=True)
    st, jobs = api('owner', 'GET', '/api/v1/jobs?state=succeeded&limit=50')
    target = next((j for j in jobs.get('items', []) if j['kind'] == 'temporal_batch'), None)
    page.goto(BASE + '/console/verification'); page.wait_for_load_state()
    if target:
        page.fill('#vj', target['id']); page.select_option('#vc', 'analytical'); page.click('button[name=preview]'); page.wait_for_load_state(); html = page.content()
        check('verification preview shows claim, cost and independence', 'claim' in html and 'independence' in html and 'affordable' in html)
        page.fill('#vj', target['id']); page.select_option('#vc', 'analytical'); page.click('form[action="/console/verification"] button:not([name=preview])'); page.wait_for_load_state()
        check('verification requested from the console', 'analytical' in page.content())
    else:
        check('verification form (skipped: no succeeded temporal_batch job)', True, 'skipped')
    page.screenshot(path=SHOTS + '/05-verification.png', full_page=True)
    page.goto(BASE + '/console/nodes'); page.wait_for_load_state(); html = page.content()
    check('nodes page states the trust model and separates local workers', 'not a permissionless network' in html and 'Local workers' in html)
    page.screenshot(path=SHOTS + '/06-nodes.png', full_page=True)
    page.goto(BASE + '/console/approvals'); page.wait_for_load_state()
    page.check('input[name=required][value=scheduling_toggle]'); page.click('button:has-text("Save policy")'); page.wait_for_load_state()
    st, pr = api('owner', 'POST', '/api/v1/approvals', {'action': 'scheduling_toggle', 'content': {'enabled': False}, 'note': 'browser proposal'})
    page.goto(BASE + '/console/approvals'); page.wait_for_load_state(); html = page.content()
    check('approvals page lists the proposal and hides approve for the proposer', pr['id'] in html and ('/approvals/' + pr['id'] + '/approve') not in html)
    page.screenshot(path=SHOTS + '/07-approvals-owner.png', full_page=True)
    page.goto(BASE + '/console/statement'); page.wait_for_load_state(); html = page.content()
    check('statement page groups by asset and labels the environment', 'synthetic-local' in html and 'never summed' in html)
    page.screenshot(path=SHOTS + '/08-statement.png', full_page=True)
    # reviewer approves the proposal and sees verification
    rctx, rpage = login(browser, 'reviewer')
    rpage.goto(BASE + '/console/approvals'); rpage.wait_for_load_state()
    if rpage.locator('form[action$="/' + pr['id'] + '/approve"] button').count():
        rpage.click('form[action$="/' + pr['id'] + '/approve"] button'); rpage.wait_for_load_state()
    st, pv = api('owner', 'GET', '/api/v1/approvals/' + pr['id'])
    check('reviewer approved the proposal through the console', pv.get('state') == 'approved', pv.get('state'))
    rpage.screenshot(path=SHOTS + '/09-approvals-reviewer.png', full_page=True)
    rpage.goto(BASE + '/console/verification'); rpage.wait_for_load_state(); check('reviewer sees verification records', rpage.locator('table').count() >= 1)
    # viewer boundaries (HTML bodies, not just rendering)
    vctx, vpage = login(browser, 'viewer')
    r = vctx.request.get(BASE + '/console/knowledge'); check('viewer cannot open Knowledge', r.status == 403)
    r = vctx.request.get(BASE + '/console/statement'); check('viewer cannot open Statement', r.status == 403)
    vpage.goto(BASE + '/console/models'); vpage.wait_for_load_state(); vh = vpage.content()
    check('viewer models page has no promote/load/generate forms', '>promote ' not in vh and 'Bounded generation' not in vh and 'mck_' not in vh)
    vpage.goto(BASE + '/console/approvals'); vpage.wait_for_load_state(); check('viewer approvals page has no decision buttons', vpage.locator('form').count() <= 1)
    vpage.screenshot(path=SHOTS + '/10-viewer-models.png', full_page=True)
    # narrow width
    nctx, npage = login(browser, 'owner', width=390)
    overflow = {}
    for path in ('/console/models', '/console/knowledge', '/console/calibration', '/console/verification', '/console/nodes', '/console/approvals', '/console/statement'):
        npage.goto(BASE + path); npage.wait_for_load_state(); overflow[path] = no_overflow(npage)
    check('narrow width: no horizontal page overflow on the new pages', all(overflow.values()), overflow)
    npage.goto(BASE + '/console/models'); npage.screenshot(path=SHOTS + '/11-narrow-models.png', full_page=True)
    npage.goto(BASE + '/console/knowledge'); npage.screenshot(path=SHOTS + '/12-narrow-knowledge.png', full_page=True)
    # keyboard: tab reaches the first form control on the knowledge page
    npage.keyboard.press('Tab'); focused = npage.evaluate('document.activeElement && document.activeElement.tagName')
    check('keyboard focus moves to a control', focused in ('A', 'INPUT', 'BUTTON', 'SELECT', 'TEXTAREA'), focused)
    browser.close()

out = {'base': BASE, 'checks': results, 'passed': sum(r['ok'] for r in results), 'failed': sum(not r['ok'] for r in results), 'total': len(results), 'screenshots': sorted(os.listdir(SHOTS))}
print(json.dumps(out))
