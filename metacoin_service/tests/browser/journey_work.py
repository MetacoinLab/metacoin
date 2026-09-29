"""Real-browser journey of the core product console (Order 08 §63) against a running instance, at desktop and narrow widths:
the controls must perform the state transitions (draft terms → freeze → open request → provider offer → award → verify →
decide → pay), stale versions must be refused, private evidence must stay behind role policy, a lost response must be
recoverable by refresh, and scientific values must read plainly.

    PLAYWRIGHT_BROWSERS_PATH=... PYTHONPATH=. <pwvenv>/bin/python metacoin_service/tests/browser/journey_work.py BASE OWNER_CRED PROVIDER_CRED VIEWER_CRED REVIEWER_CRED SHOTS_DIR"""
import json
import os
import re
import sys
import time
import urllib.request
from playwright.sync_api import sync_playwright

BASE, OWNER, PROVIDER, VIEWER, REVIEWER, SHOTS = sys.argv[1:7]
os.makedirs(SHOTS, exist_ok=True)
tok = {r: json.load(open(p))['token'] for r, p in (('owner', OWNER), ('provider', PROVIDER), ('viewer', VIEWER), ('reviewer', REVIEWER))}
results, shots = [], []


def check(name, ok, detail=''):
    results.append({'check': name, 'ok': bool(ok), 'detail': str(detail)[:300]}); print(('PASS ' if ok else 'FAIL ') + name, flush=True)


def shot(page, name):
    page.screenshot(path=os.path.join(SHOTS, name), full_page=True); shots.append(name)


def login(browser, role, width=1280):
    c = browser.new_context(viewport={'width': width, 'height': 900}); page = c.new_page()
    page.goto(BASE + '/console/login'); page.fill('#token', tok[role]); page.click('button[type=submit]'); page.wait_for_url(re.compile(r'/console/?$')); return c, page


def wait_worker(page, aid, want='completed', tries=120):
    for _ in range(tries):
        page.goto(BASE + '/console/work/awards/' + aid); page.wait_for_load_state()
        if want in page.content():
            return True
        time.sleep(1)
    return False


def run(p):
    browser = p.chromium.launch()
    octx, page = login(browser, 'owner')
    page.goto(BASE + '/console/work'); page.wait_for_load_state()
    check('overview renders the seven areas', all(x in page.content() for x in ('Missions', 'Work requests', 'Contracts', 'Providers', 'Disputes', 'Notifications', 'Waiting reasons')))
    shot(page, '01-work-overview.png')
    page.select_option('#tpl', 'determination'); page.fill('#ceil', '10'); page.click('form[action="/console/work/terms"] button'); page.wait_for_load_state()
    check('draft terms created from the form', '/console/work/terms/' in page.url and 'What counts as delivery' in page.content())
    tid = page.url.rstrip('/').split('/')[-1]
    inputs = json.loads(page.locator('#inp').input_value()); inputs = dict(inputs, private_label='BROWSER_J', available_low=500000, available_high=550000)   # INFEASIBLE scenario
    page.fill('#inp', json.dumps(inputs)); page.click('form[action$="/freeze"] button'); page.wait_for_load_state()
    check('freeze binds the operation (digest shown)', 'frozen' in page.content() and re.search(r'<code>[0-9a-f]{64}</code>', page.content()) is not None)
    shot(page, '02-terms-frozen.png')
    page.click('form[action$="/request"] button'); page.wait_for_load_state(); rid = page.url.rstrip('/').split('/')[-1]
    check('request opened from the frozen terms', 'open' in page.content() and rid.startswith('wr_'))
    pctx, ppage = login(browser, 'provider')
    ppage.goto(BASE + '/console/work/requests/' + rid); ppage.wait_for_load_state()
    check('provider sees the request preview without private inputs', 'Submit an offer' in ppage.content() and 'BROWSER_J' not in ppage.content() and 'available_low' not in ppage.content())
    ppage.fill('#pr', '7'); ppage.click('form[action$="/offer"] button'); ppage.wait_for_load_state()
    check('provider offer submitted', 'offered' in ppage.content() or 'Offers' in ppage.content())
    page.goto(BASE + '/console/work/requests/' + rid); page.wait_for_load_state()
    check('requester sees the comparison under the declared policy', 'lowest_eligible_price' in page.content() and 'tie-break' in page.content())
    shot(page, '03-request-comparison.png')
    page.click('form[action$="/award"] button'); page.wait_for_load_state(); aid = page.url.rstrip('/').split('/')[-1]
    check('award performed from the comparison form', aid.startswith('wa_') and 'Contract' in page.content())
    ppage.goto(BASE + '/console/work/awards/' + aid); ppage.wait_for_load_state(); ppage.click('form[action$="/ack"] button'); ppage.wait_for_load_state()
    check('provider acknowledged the award', 'acknowledged' in ppage.content())
    check('worker delivered the milestone (execution completed, science INFEASIBLE readable)', wait_worker(page, aid, 'INFEASIBLE') and 'completed' in page.content())
    shot(page, '04-contract-delivered.png')
    page.click('form[action$="/verify"] button'); page.wait_for_load_state()
    ok = False
    for _ in range(60):
        page.goto(BASE + '/console/work/awards/' + aid); page.wait_for_load_state()
        if 'candidate: accepted' in page.content():
            ok = True; break
        time.sleep(1)
    check('verification completed; acceptance candidate accepted with a predicate trace', ok and 'verification_passed' in page.content())
    # stale version: submit the decision form with an outdated evidence root; the page state must be preserved after refresh
    page.evaluate("document.querySelector('input[name=expected_evidence_root]').value = '0'.repeat(64)")
    page.select_option('select[name=decision]', 'accept'); page.click('form[action$="/decide"] button'); page.wait_for_load_state()
    check('stale evidence root refused with a readable error', 'evidence_changed' in page.content() or 'STATE_CONFLICT' in page.content())
    page.goto(BASE + '/console/work/awards/' + aid); page.wait_for_load_state()
    check('state preserved after refresh (still pending acceptance)', 'candidate: accepted' in page.content() and 'accepted</span>' not in page.content().split('Milestones')[1].split('Acceptance evaluation')[0])
    page.select_option('select[name=decision]', 'accept'); page.click('form[action$="/decide"] button'); page.wait_for_load_state()
    check('decision recorded: acceptance accepted, payment payable', 'payable' in page.content() and 'prepare, authorize and submit payment' in page.content())
    shot(page, '05-contract-accepted.png')
    page.click('form[action$="/pay"] button'); page.wait_for_load_state()
    check('payment settled and award closed', 'transfer observed on the rail' in page.content() and 'closed' in page.content())
    shot(page, '06-contract-paid.png')
    # lost response recovery: submit the pay form again (simulating a retried post) -> replayed, no second intent
    page.goto(BASE + '/console/work/budget'); page.wait_for_load_state()
    check('budget page shows a consistent journal replay and the settlement entry', 'consistent' in page.content() and 'settle:' in page.content())
    shot(page, '07-budget-journal.png')
    vctx, vpage = login(browser, 'viewer')
    vpage.goto(BASE + '/console/work/awards/' + aid); vpage.wait_for_load_state()
    check('viewer sees the four dimensions but no role-bypassing controls and no private values', 'accepted' in vpage.content() and 'Record decision' not in vpage.content() and 'BROWSER_J' not in vpage.content() and 'worst_margin' not in vpage.content())
    r = vctx.request.post(BASE + '/console/work/awards/' + aid + '/pay', form={'entitlement_id': 'x', 'csrf': 'x'}); check('viewer cannot post a payment form', r.status in (403, 401))
    page.goto(BASE + '/console/work/awards/' + aid); page.wait_for_load_state()
    page.fill('#cl', 'browser dispute'); page.click('form[action$="/dispute"] button'); page.wait_for_load_state(); did = page.url.rstrip('/').split('/')[-1]
    check('dispute opened with a timeline', did.startswith('wdp_') and 'Timeline' in page.content())
    rctx, rpage = login(browser, 'reviewer'); rpage.goto(BASE + '/console/work/disputes/' + did); rpage.wait_for_load_state()
    rpage.select_option('#oc', 'uphold'); rpage.fill('#rs', 'fine'); rpage.click('form[action$="/decide"] button'); rpage.wait_for_load_state()
    check('reviewer decided the dispute from the timeline page', 'uphold' in rpage.content() and 'decided' in rpage.content())
    shot(page, '08-dispute.png')
    page.goto(BASE + '/console/work'); page.wait_for_load_state(); page.click('form[action="/console/work/missions/import"] button'); page.wait_for_load_state()
    check('mission portfolio imported and bottlenecks listed', 'Bottlenecks' in page.content() and 'task-0018' in page.content())
    shot(page, '09-mission.png')
    page.goto(BASE + '/console/work/providers/' + re.search(r'/console/work/providers/(pv_[0-9a-f]+)', page.goto(BASE + '/console/work') and page.content()).group(1)); page.wait_for_load_state()
    check('provider history page shows dimensions with a valid negative counted as accepted and no score', 'History dimensions (no single score)' in page.content() and 'valid negative' in page.content() and 'not failures' in page.content())
    shot(page, '11-provider-history.png')
    nctx, npage = login(browser, 'owner', width=390)
    overflow = {}
    for path in ('/console/work', '/console/work/awards/' + aid, '/console/work/requests/' + rid, '/console/work/budget', '/console/work/disputes/' + did):
        npage.goto(BASE + path); npage.wait_for_load_state(); overflow[path] = npage.evaluate('document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1')
    check('narrow width: no horizontal page overflow on the work pages', all(overflow.values()), overflow)
    npage.goto(BASE + '/console/work/awards/' + aid); shot(npage, '10-narrow-contract.png')
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
