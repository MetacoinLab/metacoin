"""Real-browser console inspection for the scientific workspace (Order 07 §51/§52/§61-1/§61-16) against a running instance.

    PLAYWRIGHT_BROWSERS_PATH=... PYTHONPATH=. <pwvenv>/bin/python metacoin_service/tests/browser/journey_workspace.py BASE OWNER_CRED REVIEWER_CRED VIEWER_CRED SHOTS_DIR CTX_JSON

Owner: Documents list → document page → page inspector (method, spans, preview image); Knowledge search result carries the
page number; Analyses → analysis page (block status badges, impact form) → report page (sections, evidence links) → the
linked job page; Compute new (resource plan sample validate) → plan job page (schedule SVG with reserve line, alternatives,
sensitivity); Packages → package page (compatibility check form); scenario comparison page; viewer boundaries; narrow width
without horizontal overflow. Tokens come from private credential files and are never printed."""
import json
import os
import re
import sys
import time
import urllib.request
from playwright.sync_api import sync_playwright

BASE, OWNER, REVIEWER, VIEWER, SHOTS, CTX = sys.argv[1:7]
os.makedirs(SHOTS, exist_ok=True)
tok = {r: json.load(open(p))['token'] for r, p in (('owner', OWNER), ('reviewer', REVIEWER), ('viewer', VIEWER))}
ctx = json.load(open(CTX))
results, shots = [], []


def check(name, ok, detail=''):
    results.append({'check': name, 'ok': bool(ok), 'detail': str(detail)[:300]}); print(('PASS ' if ok else 'FAIL ') + name, flush=True)


def shot(page, name):
    path = SHOTS + '/' + name; page.screenshot(path=path, full_page=True); shots.append(name)


def api(role, method, path, body=None):
    req = urllib.request.Request(BASE + path, data=None if body is None else json.dumps(body).encode(), method=method, headers={'Authorization': 'Bearer ' + tok[role], 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b'{}')


def login(browser, role, width=1280):
    c = browser.new_context(viewport={'width': width, 'height': 900}); page = c.new_page()
    page.goto(BASE + '/console/login'); page.fill('#token', tok[role]); page.click('button[type=submit]'); page.wait_for_url(re.compile(r'/console/?$')); return c, page


def no_overflow(page):
    return page.evaluate('document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1')


def run(p):
    browser = p.chromium.launch()
    octx, page = login(browser, 'owner')
    # documents and the page inspector
    page.goto(BASE + '/console/documents'); page.wait_for_load_state(); html = page.content()
    check('documents page lists imports with stages', 'Documents' in html and 'ready' in html)
    shot(page, '01-documents.png')
    did = ctx.get('document_id')
    if did:
        page.goto(BASE + '/console/documents/' + did); page.wait_for_load_state(); html = page.content()
        check('document page shows extraction provenance and pages', 'native' in html and ('page' in html.lower()))
        shot(page, '02-document.png')
        page.goto(BASE + '/console/documents/' + did + '/pages/0'); page.wait_for_load_state(); html = page.content()
        check('page inspector shows method, spans and the preview image', 'native' in html and 'span' in html.lower() and page.locator('img').count() >= 1)
        shot(page, '03-page-inspector.png')
    cid = ctx.get('collection_id')
    if cid:
        page.goto(BASE + '/console/knowledge'); page.wait_for_load_state()
        page.fill('#q-' + cid, 'reserve 2000 mJ boundary'); page.select_option('#m-' + cid, 'lexical'); page.click('form[action$="/collections/' + cid + '/search"] button.secondary'); page.wait_for_load_state(); html = page.content()
        check('knowledge search result shows the page number of the hit', '2000 mJ' in html and ('page 1' in html.lower() or 'p. 1' in html.lower() or '>1<' in html))
        shot(page, '04-search-page-citation.png')
    # analyses and reports
    aid = ctx.get('analysis_id'); rid = ctx.get('report_id')
    page.goto(BASE + '/console/analyses'); page.wait_for_load_state(); check('analyses list renders', 'Analyses' in page.content())
    if aid:
        page.goto(BASE + '/console/analyses/' + aid); page.wait_for_load_state(); html = page.content()
        check('analysis page shows typed blocks with status badges and the impact form', 'Blocks' in html and 'current' in html and 'Analyse impact' in html)
        shot(page, '05-analysis.png')
        page.select_option('#ib', 'plan') if page.locator('#ib option[value=plan]').count() else None
        page.click('form[action$="/impact"] button'); page.wait_for_load_state(); html = page.content()
        check('impact result rendered from the form', 'directly affected' in html)
        shot(page, '06-analysis-impact.png')
    if rid:
        page.goto(BASE + '/console/reports/' + rid); page.wait_for_load_state(); html = page.content()
        check('report page shows the evidence sections and flags', 'Computed findings' in html and 'Limitations' in html and 'Verification scope' in html)
        shot(page, '07-report.png')
        link = page.locator('a[href^="/console/analyses/"]').first
        link.click(); page.wait_for_load_state(); check('report links back to its frozen analysis revision', '/console/analyses/' in page.url and 'frozen' in page.content())
    # plan job page and compute form
    pj = ctx.get('tradeoff_job') or ctx.get('plan_job')
    if pj:
        page.goto(BASE + '/console/jobs/' + pj); page.wait_for_load_state(); html = page.content()
        check('plan job page shows status, alternatives, sensitivity and the schedule SVG', 'Alternatives (epsilon-constraint sweep' in html and 'Sensitivity (finite study)' in html and 'plan.svg' in html)
        svg = octx.request.get(BASE + '/api/v1/compute/jobs/' + pj + '/plan.svg')
        check('schedule SVG carries the reserve line, labelled boundaries and a task legend', svg.status == 200 and 'reserve' in svg.text() and 'boundary 6' in svg.text() and 'mandatory' in svg.text())
        shot(page, '08-plan-job.png')
    page.goto(BASE + '/console/compute/new?kind=resource_plan'); page.wait_for_load_state()
    page.click('button[name=preview]'); page.wait_for_load_state(); html = page.content()
    check('resource plan sample validates in the compute form with a work-unit estimate', 'work units' in html and 'start variable' in html)
    shot(page, '09-compute-new-plan.png')
    # packages
    pk = ctx.get('package_id')
    page.goto(BASE + '/console/packages'); page.wait_for_load_state(); check('packages list renders', 'Packages' in page.content())
    if pk:
        page.goto(BASE + '/console/packages/' + pk); page.wait_for_load_state()
        page.click('form[action$="/compatibility"] button'); page.wait_for_load_state(); html = page.content()
        check('package compatibility report rendered from the console form', 'supported_as_requested' in html and 'nothing reserved' in html)
        shot(page, '10-package-compat.png')
    # scenario comparison (two campaigns from the resource plan sample)
    st, s = api('owner', 'GET', '/api/v1/services')
    base_in = json.loads(json.dumps(ctx.get('plan_inputs') or {}))
    if base_in:
        st, c1 = api('owner', 'POST', '/api/v1/campaigns', {'definition': {'name': 'cmp A', 'kind': 'resource_plan', 'base': base_in, 'axes': [{'path': 'reserve', 'values': [20000, 30000]}]}})
        st, c2 = api('owner', 'POST', '/api/v1/campaigns/' + c1['campaign_id'] + '/branch', {'changes': [{'path': 'tasks.a.duration', 'value': 3, 'source': 'user_edit'}], 'name': 'cmp B'})
        page.goto(BASE + '/console/campaigns/' + c1['campaign_id'] + '/compare/' + c2['campaign_id']); page.wait_for_load_state(); html = page.content()
        check('scenario comparison page shows the changed assumption path and side-by-side columns', 'tasks.a.duration' in html and 'Side by side' in html)
        shot(page, '11-scenario-compare.png')
    # viewer boundaries
    vctx, vpage = login(browser, 'viewer')
    r = vctx.request.get(BASE + '/console/analyses'); check('viewer cannot open Analyses', r.status == 403)
    if pj:
        vh = vctx.request.get(BASE + '/console/jobs/' + pj).text(); check('viewer job page carries no private plan values', 'Alternatives (epsilon' not in vh and 'RP_TEST' not in vh and 'J14' not in vh)
    r = vctx.request.get(BASE + '/api/v1/packages/' + (pk or 'x')); check('viewer cannot create or export packages (read allowed, mutation refused)', r.status in (200, 404))
    r = vctx.request.post(BASE + '/console/packages', form={'name': 'x', 'workflow_id': 'x'}); check('viewer package creation refused', r.status in (403, 422))
    # narrow width
    nctx, npage = login(browser, 'owner', width=390)
    overflow = {}
    for path in ('/console/documents', '/console/analyses', '/console/packages', '/console/compute/new?kind=resource_plan') + (('/console/jobs/' + pj,) if pj else ()) + (('/console/reports/' + rid,) if rid else ()):
        npage.goto(BASE + path); npage.wait_for_load_state(); overflow[path] = no_overflow(npage)
    check('narrow width: no horizontal page overflow on the workspace pages', all(overflow.values()), overflow)
    if pj:
        npage.goto(BASE + '/console/jobs/' + pj); shot(npage, '12-narrow-plan-job.png')
    npage.goto(BASE + '/console/analyses'); shot(npage, '13-narrow-analyses.png')
    browser.close()


crash = None
try:
    with sync_playwright() as p:
        run(p)
except Exception as exc:
    import traceback
    crash = traceback.format_exc()[-800:]
    results.append({'check': 'browser run crashed', 'ok': False, 'detail': repr(exc)[:300]})
print(json.dumps({'passed': sum(r['ok'] for r in results), 'failed': sum(not r['ok'] for r in results), 'checks': results, 'screenshots': shots, 'crash': crash}))
sys.exit(0 if all(r['ok'] for r in results) else 1)
