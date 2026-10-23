"""Real-browser journey of the mission economy (order 2026-10-23 §12/§13 journey 35) against a running instance: the operator
screen online and during a blackout (disabled actions with reasons), the mission page (graph, budget ledger, controller
explanation, revisions, events), the resource market, settlement and recovery pages, and no horizontal overflow at phone width.

    PLAYWRIGHT_BROWSERS_PATH=... PYTHONPATH=. <pwvenv>/bin/python metacoin_service/tests/browser/journey_mission.py BASE OWNER_CRED REQUESTER_CRED MISSION_ID JOB_ID SHOTS_DIR"""
import json
import os
import re
import sys
from playwright.sync_api import sync_playwright

BASE, OWNER, REQ, MISSION, JOB, SHOTS = sys.argv[1:7]
os.makedirs(SHOTS, exist_ok=True)
tok = {r: json.load(open(p))['token'] for r, p in (('owner', OWNER), ('requester', REQ))}
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
    page.goto(BASE + '/console/operator'); page.wait_for_load_state(); html = page.content()
    check('operator screen: owns/owes, committed resources, missing evidence, possible/disabled actions, pending cross-domain, recovery', all(x in html for x in ('What each machine owns and owes', 'Evidence missing', 'possible and disabled actions', 'Pending across domains', 'Recovery', 'link online')), (page.title(), re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', html))[:400]))
    shot(page, '01-operator-online.png')
    page.click('form[action="/console/native/connectivity"] button[value=cut]'); page.wait_for_load_state(); page.goto(BASE + '/console/operator'); page.wait_for_load_state(); html = page.content()
    check('blackout: the operator screen shows the cut link and keeps local actions possible', 'link cut' in html and 'During the blackout' in html)
    shot(page, '02-operator-blackout.png')
    page.goto(BASE + '/console/missions/' + MISSION); page.wait_for_load_state(); html = page.content()
    check('mission page during blackout: controller context says connectivity cut; disabled actions carry reasons', 'connectivity cut' in html and 'Controller: what is possible now' in html)
    shot(page, '03-mission-blackout.png')
    page.goto(BASE + '/console/operator'); page.click('form[action="/console/native/connectivity"] button[value=online]'); page.wait_for_load_state(); page.goto(BASE + '/console/operator'); page.wait_for_load_state()
    check('reconnection: the operator screen shows the link online again', 'link online' in page.content())
    page.goto(BASE + '/console/missions/' + MISSION); page.wait_for_load_state(); html = page.content()
    check('mission page: graph/timeline, budget ledger projection, controller explanation, revisions and events', all(x in html for x in ('Graph / timeline', 'Budget ledger', 'Controller: what is possible now', 'Revisions', 'Events', 'frozen')))
    shot(page, '04-mission.png')
    page.goto(BASE + '/console/missions'); page.wait_for_load_state(); check('mission builder lists the three executable templates', all(x in page.content() for x in ('terrain_delivery', 'compute_and_relay', 'evidence_before_followon')))
    shot(page, '05-mission-builder.png')
    page.goto(BASE + '/console/resources'); page.wait_for_load_state(); html = page.content()
    check('resource market: offers with capacity, windows, price, evidence labels and refusal reasons', all(x in html for x in ('compute', 'available', 'window', 'stale_quote')))
    shot(page, '06-market.png')
    page.goto(BASE + '/console/native/settlement'); page.wait_for_load_state(); check('settlement page explains local finality, remote acknowledgment and unresolved exposure', all(x in page.content() for x in ('Local finality', 'Remote acknowledgment', 'Unresolved exposure')))
    shot(page, '07-settlement.png')
    page.goto(BASE + '/console/native/recovery'); page.wait_for_load_state(); check('recovery page names the rollback detection source and the safety limitation', 'rollback' in page.content().lower())
    shot(page, '08-recovery.png')
    if JOB:
        page.goto(BASE + '/console/lunar/' + JOB); page.wait_for_load_state(); check('product view states provenance, assumptions and resolution of the pinned public data', all(x in page.content() for x in ('Provenance', 'Assumptions', 'LDEM_4')))
        shot(page, '09-product.png')
    rctx, rpage = login(browser, 'requester')
    rpage.goto(BASE + '/console/missions/' + MISSION); rpage.wait_for_load_state(); check('the requester sees its own mission with the controller explanation', 'Controller: what is possible now' in rpage.content())
    nctx, npage = login(browser, 'owner', width=390); overflow = {}
    for path in ('/console/operator', '/console/missions/' + MISSION, '/console/resources', '/console/missions'):
        npage.goto(BASE + path); npage.wait_for_load_state(); overflow[path] = npage.evaluate('document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1')
    check('narrow width: no horizontal page overflow on operator, mission and market pages', all(overflow.values()), overflow)
    npage.goto(BASE + '/console/operator'); shot(npage, '10-narrow-operator.png')
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
