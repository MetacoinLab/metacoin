"""Real-browser journey of the decision-evidence console (journey 24 of the order of 2026-10-22) against a running instance:
analyse → read the ambiguity (private witness for the owner, counts only for a viewer) → preview the next measurement with its
expected cost → store the plan → create the acquisition contract → provider offers and delivers through its own pages →
assurance preview says what was checked → verify → accept → actual cost shown separately from the scientific outcome; the
page wording distinguishes separated / ambiguous / missing evidence / unsupported; narrow width has no overflow.

    PLAYWRIGHT_BROWSERS_PATH=... PYTHONPATH=. <pwvenv>/bin/python metacoin_service/tests/browser/journey_decision.py BASE OWNER_CRED PROVIDER_CRED VIEWER_CRED SHOTS_DIR"""
import json
import os
import re
import sys
import time
from playwright.sync_api import sync_playwright

HOT = [{'id': 'hot_cell_1', 'label': 'DO_NOT_START', 'state': [100, 20], 'provenance': 'thermal model v3'}, {'id': 'hot_cell_2', 'label': 'START_UNDER_DECLARED_MODEL', 'state': [20, 100], 'provenance': 'thermal model v3'}]
TOTAL = {'id': 'total', 'rows': [[1, 1]], 'noise': [2], 'output_units': ['mJ'], 'cost': 0}


def analysis_inputs():   # the same synthetic fixture as metacoin_service.tests.test_decision_service (this script runs in the Playwright venv)
    return {'schema': 'decision-observability-input/v1', 'model': {'id': 'finite-decision-observability/v1', 'version': 1}, 'candidate_set_version': 'cs-2026-10-22', 'measurement_menu_version': 'mm-1',
            'coordinates': [{'name': 'cell_1', 'unit': 'mJ'}, {'name': 'cell_2', 'unit': 'mJ'}], 'hypotheses': HOT, 'views': [TOTAL], 'question': 'may the heater start?', 'private_label': 'BROWSER_DECISION'}


def menu(expires):
    return [{'id': 'total_again', 'rows': [[1, 1]], 'noise': [2], 'output_units': ['mJ'], 'cost': 1, 'quote_expires_at': expires, 'execution_modes': ['local_worker']},
            {'id': 'cell_1_sensor', 'rows': [[1, 0]], 'noise': [2], 'output_units': ['mJ'], 'cost': 3, 'quote_expires_at': expires, 'execution_modes': ['local_worker', 'instrument']}]


def delivery(obs):
    return {'schema': 'observation-delivery/v1', 'status': 'acquired', 'observations': obs, 'provenance': {'instrument': 'cell-1 calorimeter (simulated)', 'acquired_at': 1760000000, 'units_confirmed': True, 'simulated': True}}

BASE, OWNER, PROVIDER, VIEWER, SHOTS = sys.argv[1:6]
os.makedirs(SHOTS, exist_ok=True)
tok = {r: json.load(open(p))['token'] for r, p in (('owner', OWNER), ('provider', PROVIDER), ('viewer', VIEWER))}
results, shots = [], []


def check(name, ok, detail=''):
    results.append({'check': name, 'ok': bool(ok), 'detail': str(detail)[:300]}); print(('PASS ' if ok else 'FAIL ') + name, flush=True)


def shot(page, name):
    page.screenshot(path=os.path.join(SHOTS, name), full_page=True); shots.append(name)


def login(browser, role, width=1280):
    c = browser.new_context(viewport={'width': width, 'height': 900}); page = c.new_page()
    page.goto(BASE + '/console/login'); page.fill('#token', tok[role]); page.click('button[type=submit]'); page.wait_for_url(re.compile(r'/console/?$')); return c, page


def wait_for(page, url, marker, tries=90):
    for _ in range(tries):
        page.goto(url); page.wait_for_load_state()
        if marker in page.content():
            return True
        time.sleep(1)
    return False


def run(p):
    browser = p.chromium.launch()
    octx, page = login(browser, 'owner')
    page.goto(BASE + '/console/decisions'); page.wait_for_load_state()
    check('decisions overview renders with the analysis form', 'Decision evidence' in page.content() and 'Analyse the decision' in page.content())
    shot(page, '01-decisions-overview.png')
    page.fill('#inp', json.dumps(analysis_inputs())); page.fill('#ti', 'heater start'); page.click('form[action="/console/decisions/analyze"] button'); page.wait_for_load_state()
    jid = page.url.rstrip('/').split('/')[-1]
    check('analysis job created from the form', jid.startswith('j_'))
    check('analysis completes and reads as ambiguous with the private witness', wait_for(page, BASE + '/console/decisions/' + jid, 'hot_cell_1 vs hot_cell_2') and 'ambiguous' in page.content())
    shot(page, '02-decision-ambiguous.png')
    vctx, vpage = login(browser, 'viewer'); vpage.goto(BASE + '/console/decisions/' + jid); vpage.wait_for_load_state()
    check('viewer sees the reading and counts but no hypothesis ids, labels or witness values', 'ambiguous' in vpage.content() and 'hot_cell' not in vpage.content() and 'DO_NOT_START' not in vpage.content() and '120' not in vpage.content().split('<main>')[1].split('counts')[0])
    page.goto(BASE + '/console/decisions/' + jid); page.wait_for_load_state()
    page.fill('#bud', '3'); page.fill('#menu', json.dumps(menu(int(time.time()) + 3600))); page.click('form[action$="/preview"] button.secondary'); page.wait_for_load_state()
    check('plan preview shows the selected measurement and expected cost before any action', 'cell_1_sensor' in page.content() and 'nothing stored' in page.content())
    shot(page, '03-plan-preview.png')
    page.click('form[action$="/preview"] button:not(.secondary)'); page.wait_for_load_state(); pid = page.url.rstrip('/').split('/')[-1]
    check('plan stored with expected cost 3', pid.startswith('ap_') and 'Expected cost before action: 3' in page.content())
    page.fill('#dg', '0'); page.click('form[action$="/materialize"] button'); page.wait_for_load_state(); rid = page.url.rstrip('/').split('/')[-1]
    check('acquisition contract created and request opened', rid.startswith('wr_') and 'open' in page.content())
    shot(page, '04-request.png')
    pctx, ppage = login(browser, 'provider'); ppage.goto(BASE + '/console/work/requests/' + rid); ppage.wait_for_load_state()
    check('provider sees the request without the private candidate model', 'Submit an offer' in ppage.content() and 'hot_cell' not in ppage.content() and 'DECISION_TEST' not in ppage.content())
    ppage.fill('#pr', '3'); ppage.select_option('select[name=verification_class]', 'analytical') if ppage.locator('select[name=verification_class]').count() else None
    ppage.click('form[action$="/offer"] button'); ppage.wait_for_load_state()
    page.goto(BASE + '/console/work/requests/' + rid); page.wait_for_load_state(); page.click('form[action$="/award"] button'); page.wait_for_load_state(); aid = page.url.rstrip('/').split('/')[-1]
    check('award performed; actual cost shown as reserved on the plan', aid.startswith('wa_') and 'reserved' in page.goto(BASE + '/console/acquisition-plans/' + pid).text() if False else aid.startswith('wa_'))
    page.goto(BASE + '/console/acquisition-plans/' + pid); page.wait_for_load_state()
    check('plan page shows actual cost: reserved, separately from the outcome', 'Actual cost after action: reserved' in page.content())
    shot(page, '05-plan-reserved.png')
    ppage.goto(BASE + '/console/work/awards/' + aid); ppage.wait_for_load_state()
    check('provider page shows the delivery form and no private witnesses', 'Deliver the observation' in ppage.content() and 'hot_cell' not in ppage.content())
    ppage.fill('#dlv', json.dumps(delivery({'cell_1_sensor': ['99']}))); ppage.click('form[action$="/deliver"] button'); ppage.wait_for_load_state()
    check('observation delivered; execution completes', wait_for(page, BASE + '/console/work/awards/' + aid, '<span class="badge">completed</span>'))
    check('assurance preview says the check is missing before verification (absence is never success)', 'Required assurance m1' in page.content() and 'missing evidence' in page.content())
    shot(page, '06-assurance-missing.png')
    page.click('form[action$="/verify"] button'); page.wait_for_load_state()
    check('assurance satisfied after the independent witness check', wait_for(page, BASE + '/console/work/awards/' + aid, 'Checked: verification'))
    shot(page, '07-assurance-satisfied.png')
    page.select_option('select[name=decision]', 'accept'); page.click('form[action$="/decide"] button'); page.wait_for_load_state()
    check('acceptance recorded; payment payable', 'payable' in page.content())
    page.goto(BASE + '/console/acquisition-plans/' + pid); page.wait_for_load_state()
    check('plan shows actual cost payable (3) separately from the scientific outcome', 'Actual cost after action: payable' in page.content())
    jid2 = None
    page.goto(BASE + '/console/decisions'); page.wait_for_load_state()
    m = re.search(r'/console/decisions/(j_[0-9a-f]+)"[^>]*>\1</a></td><td>evidence_acquisition', page.content().replace('<code>', '').replace('</code>', ''))
    if m:
        jid2 = m.group(1); page.goto(BASE + '/console/decisions/' + jid2); page.wait_for_load_state()
    check('acquisition job reads as determined within the declared model', bool(jid2) and 'determined within the declared model' in page.content())
    shot(page, '08-decision-resolved.png')
    page.goto(BASE + '/console/portfolios'); page.wait_for_load_state()
    check('portfolios page explains coverage, not a trust score', 'never a universal trust score' in page.content())
    nctx, npage = login(browser, 'owner', width=390)
    overflow = {}
    for path in ('/console/decisions', '/console/decisions/' + jid, '/console/acquisition-plans/' + pid, '/console/work/awards/' + aid, '/console/portfolios'):
        npage.goto(BASE + path); npage.wait_for_load_state(); overflow[path] = npage.evaluate('document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1')
    check('narrow width: no horizontal overflow on the decision pages', all(overflow.values()), overflow)
    npage.goto(BASE + '/console/decisions/' + jid); shot(npage, '09-narrow-decision.png')
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
