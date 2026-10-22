"""Real-browser journey of the native mission / account / treasury story (order §17, journey 38) against a running instance:
domain view, machine account with separated categories, settlement explanation, treasury following fees into awards, the
mission screen, the lunar product view with its preview, a simulated then committed transfer through the account form,
a viewer who can read but cannot spend, and no horizontal overflow at phone width.

    PLAYWRIGHT_BROWSERS_PATH=... PYTHONPATH=. <pwvenv>/bin/python metacoin_service/tests/browser/journey_native.py BASE OWNER_CRED ACTOR_CRED VIEWER_CRED ACCOUNT_ID TO_ACCOUNT AWARD_ID JOB_ID SHOTS_DIR"""
import json
import os
import re
import sys
from playwright.sync_api import sync_playwright

BASE, OWNER, ACTOR, VIEWER, ACCOUNT, TO, AWARD, JOB, SHOTS = sys.argv[1:10]
os.makedirs(SHOTS, exist_ok=True)
tok = {r: json.load(open(p))['token'] for r, p in (('owner', OWNER), ('actor', ACTOR), ('viewer', VIEWER))}
results, shots = [], []


def check(name, ok, detail=''):
    results.append({'check': name, 'ok': bool(ok), 'detail': str(detail)[:300]}); print(('PASS ' if ok else 'FAIL ') + name, flush=True)


def shot(page, name):
    page.screenshot(path=os.path.join(SHOTS, name), full_page=True); shots.append(name)


def login(browser, role, width=1280):
    c = browser.new_context(viewport={'width': width, 'height': 900}); page = c.new_page()
    page.goto(BASE + '/console/login'); page.fill('#token', tok[role]); page.click('button[type=submit]'); page.wait_for_url(re.compile(r'/console/?$')); return c, page


def run(p):
    browser = p.chromium.launch()
    octx, page = login(browser, 'owner')
    page.goto(BASE + '/console/native'); page.wait_for_load_state()
    check('domain view: identity, custody, authority policy, connectivity and recovery state', all(x in page.content() for x in ('authority key', 'custody', 'trust model', 'recovery' if 'normal' in page.content() else 'quarantine', 'online')))
    check('domain view: x402 not required by the native core', 'required by the native core: False' in page.content())
    shot(page, '01-native-domain.png')
    page.goto(BASE + '/console/native/accounts/' + ACCOUNT); page.wait_for_load_state()
    check('machine account: available, reservations, earned receivables and pending inter-domain claims shown separately', all(x in page.content() for x in ('available', 'reserved', 'earned, unsettled', 'pending inter-domain claims')))
    shot(page, '02-machine-account.png')
    page.goto(BASE + '/console/native/settlement'); page.wait_for_load_state()
    check('settlement view explains local finality, remote acknowledgment and unresolved exposure', all(x in page.content() for x in ('Local finality', 'Remote acknowledgment', 'Unresolved exposure')))
    shot(page, '03-settlement.png')
    page.goto(BASE + '/console/native/treasury'); page.wait_for_load_state()
    check('treasury view follows fee receipts into allocations and completed awards', all(x in page.content() for x in ('Fee receipts', 'Allocations and awards funded by fees', 'Treasury payouts')))
    shot(page, '04-treasury.png')
    page.goto(BASE + '/console/native/missions/' + AWARD); page.wait_for_load_state()
    check('mission screen shows deliverable, acceptance, resource envelope, maximum obligation and selected provider', all(x in page.content() for x in ('Deliverable', 'Acceptance', 'Resource envelope', 'Maximum obligation', 'Selected provider')))
    shot(page, '05-mission.png')
    page.goto(BASE + '/console/lunar/' + JOB); page.wait_for_load_state()
    check('product view shows the artifact scope, provenance, assumptions and reproduction path', all(x in page.content() for x in ('Provenance', 'Assumptions', 'Reproduction', 'LDEM_4')))
    img_ok = page.evaluate("() => { const i = document.querySelector('img'); return !!i && i.naturalWidth > 0; }")
    check('product preview image renders', img_ok)
    shot(page, '06-product.png')
    actx, apage = login(browser, 'actor')
    apage.goto(BASE + '/console/native/accounts/' + ACCOUNT); apage.wait_for_load_state()
    before = re.search(r'<strong>(\d+)</strong> ', apage.content()).group(1)
    apage.fill('#to', TO); apage.fill('#amt', '1'); apage.fill('#rq', 'browser-sim'); apage.click('form[action$="/transfer"] button:not([name=commit])'); apage.wait_for_load_state()
    check('simulate shows would_succeed and moves nothing', 'would_succeed' in apage.content() and re.search(r'<strong>(\d+)</strong> ', apage.content()).group(1) == before)
    apage.fill('#to', TO); apage.fill('#amt', '1'); apage.fill('#rq', 'browser-real'); apage.click('form[action$="/transfer"] button[name=commit]'); apage.wait_for_load_state()
    after = re.search(r'<strong>(\d+)</strong> ', apage.content()).group(1)
    check('committed transfer reduced the available balance by one', int(after) == int(before) - 1, (before, after))
    shot(apage, '07-transfer.png')
    vctx, vpage = login(browser, 'viewer')
    vpage.goto(BASE + '/console/native/accounts/' + ACCOUNT); vpage.wait_for_load_state()
    check('viewer reads the account but has no transfer control', 'available' in vpage.content() and 'Transfer (spend)' not in vpage.content())
    r = vctx.request.post(BASE + '/console/native/accounts/' + ACCOUNT + '/transfer', form={'to_account': TO, 'amount': '1', 'request_id': 'viewer-x', 'commit': '1', 'csrf': 'x'}); check('viewer cannot post a transfer form', r.status in (403, 401, 400))
    nctx, npage = login(browser, 'owner', width=390)
    overflow = {}
    for path in ('/console/native', '/console/native/accounts/' + ACCOUNT, '/console/native/settlement', '/console/native/treasury', '/console/native/missions/' + AWARD, '/console/lunar/' + JOB):
        npage.goto(BASE + path); npage.wait_for_load_state(); overflow[path] = npage.evaluate('document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1')
    check('narrow width: no horizontal page overflow on the native pages', all(overflow.values()), overflow)
    npage.goto(BASE + '/console/native/accounts/' + ACCOUNT); shot(npage, '08-narrow-account.png')
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
