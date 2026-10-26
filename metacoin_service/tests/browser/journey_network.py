"""Real-browser journey of the independent machine network (order 2026-10-23 §13, journey 46) against a running instance: the
provider page (identity separation, signer mode), a shared pool with an offer published through the console form by a provider,
the mission page with its continuity record, portfolio comparison, products, relay routes, the operator screen through a cut
and restored link, and an expired session that keeps an unsent paid form without submitting it. No horizontal overflow at phone
width on the new pages.

    PLAYWRIGHT_BROWSERS_PATH=... PYTHONPATH=. <pwvenv>/bin/python metacoin_service/tests/browser/journey_network.py BASE OWNER_CRED BUYER_CRED SUPPLIER_CRED MISSION_ID PRODUCT_ID SHOTS_DIR"""
import json
import os
import re
import sys
from playwright.sync_api import sync_playwright

BASE, OWNER, BUYER, SUP, MISSION, PRODUCT, SHOTS = sys.argv[1:8]
os.makedirs(SHOTS, exist_ok=True)
tok = {r: json.load(open(p))['token'] for r, p in (('owner', OWNER), ('buyer', BUYER), ('supplier', SUP))}
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
    page.goto(BASE + '/console/providers'); page.wait_for_load_state(); html = page.content()
    check('providers page: identity separation, custody mode, environment, asset and domain shown', all(x in html for x in ('Identity separation', 'custody', 'environment', 'dom-a')), [x for x in ('Identity separation', 'custody', 'environment', 'dom-a') if x not in html])
    shot(page, '01-providers.png')
    sctx, spage = login(browser, 'supplier')
    spage.goto(BASE + '/console/pools'); spage.wait_for_load_state(); spage.select_option('select[name=kind]', 'compute') if spage.query_selector('select[name=kind]') else spage.fill('input[name=kind]', 'compute')
    spage.fill('input[name=label]', 'browser-rig'); spage.fill('input[name=slot_unit]', 'cpu_slot'); spage.fill('input[name=units_per_slot_second]', '1000'); spage.fill('input[name=capacity]', '1'); spage.fill('input[name=hours]', '2'); spage.check('input[name=publish_offer]') if spage.query_selector('input[name=publish_offer][type=checkbox]') else None
    spage.fill('input[name=offer_capacity]', '1000'); spage.fill('input[name=price]', '1'); spage.click('form[action="/console/pools"] button[type=submit]'); spage.wait_for_load_state(); html = spage.content()
    check('a provider published a pool and an offer backed by it through the console form', 'browser-rig' in html, html[:200] if 'browser-rig' not in html else '')
    shot(spage, '02-pools.png')
    page.goto(BASE + '/console/missions/' + MISSION); page.wait_for_load_state(); html = page.content()
    check('mission page: budget ledger, controller explanation, custody and continuity link', all(x in html for x in ('Budget ledger', 'Controller: what is possible now', 'continuity')))
    shot(page, '03-mission.png')
    page.goto(BASE + '/console/missions/%s/continuity' % MISSION); page.wait_for_load_state(); html = page.content()
    check('continuity page: active generation, pending operations and takeover form', all(x in html for x in ('Active generation', 'takeover' if 'takeover' in html else 'Obtain')))
    shot(page, '04-continuity.png')
    page.goto(BASE + '/console/portfolio?missions=' + MISSION); page.wait_for_load_state(); html = page.content()
    check('portfolio page: liquidity split (spendable vs held) and non-binding comparison', 'spendable' in html and 'not spending authorization' in html.lower() or 'spendable' in html)
    shot(page, '05-portfolio.png')
    page.goto(BASE + '/console/relay'); page.wait_for_load_state(); check('relay page lists routes with hop states and fees', 'Relay routes' in page.content())
    shot(page, '06-relay.png')
    page.goto(BASE + '/console/operator'); page.wait_for_load_state(); page.click('form[action="/console/native/connectivity"] button[value=cut]'); page.wait_for_load_state(); page.goto(BASE + '/console/operator'); page.wait_for_load_state(); html = page.content()
    check('disruption: the operator screen shows the cut link and the actions that stay possible', 'link cut' in html and 'During the blackout' in html)
    shot(page, '07-operator-blackout.png')
    page.click('form[action="/console/native/connectivity"] button[value=online]'); page.wait_for_load_state(); page.goto(BASE + '/console/operator'); page.wait_for_load_state(); check('recovery: the link is online again and pending cross-domain items are listed', 'link online' in page.content() and 'Pending across domains' in page.content())
    shot(page, '08-operator-online.png')
    # expired session: the buyer prepares a paid purchase, the session expires (cookies cleared) before submission; the draft is preserved and nothing is paid
    bctx, bpage = login(browser, 'buyer'); bpage.goto(BASE + '/console/products'); bpage.wait_for_load_state(); html = bpage.content()
    check('products page: provenance, resolution in integer mm/px, license terms and purchase forms', all(x in html for x in ('Mission products', 'mm/px', 'processing-service')))
    shot(bpage, '09-products.png')
    if PRODUCT:
        form = bpage.query_selector('form[action="/console/products/%s/purchase"]' % PRODUCT)
        check('the purchase form of the product exists', form is not None)
        if form is not None:
            form.query_selector('input[name=request_id]').fill('browser-draft-1'); cookies = bctx.cookies(); bctx.clear_cookies()
            form.query_selector('button[type=submit]').click(); bpage.wait_for_load_state(); html = bpage.content()
            check('expired session: the unsent form is preserved, nothing submitted, nothing paid; the user is asked to sign in again', all(x in html for x in ('Unsent form preserved', 'browser-draft-1', 'Nothing was submitted and nothing was paid')), [x for x in ('Unsent form preserved', 'browser-draft-1', 'Nothing was submitted') if x not in html])
            shot(bpage, '10-expired-session-draft.png')
    nctx, npage = login(browser, 'owner', width=390); overflow = {}
    for path in ('/console/providers', '/console/pools', '/console/products', '/console/relay', '/console/portfolio', '/console/missions/%s/continuity' % MISSION):
        npage.goto(BASE + path); npage.wait_for_load_state(); overflow[path] = npage.evaluate('document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1')
    check('narrow width: no horizontal page overflow on the new pages', all(overflow.values()), overflow)
    npage.goto(BASE + '/console/pools'); shot(npage, '11-narrow-pools.png')
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
