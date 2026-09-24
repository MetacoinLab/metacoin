# Real-browser checks (Playwright + headless Chromium)

Isolated tooling, not part of the service extra:

```sh
python3 -m venv /path/pwvenv && /path/pwvenv/bin/pip install playwright==1.63.0
PLAYWRIGHT_BROWSERS_PATH=/path/pw-browsers /path/pwvenv/bin/playwright install chromium
cd <repo>; PLAYWRIGHT_BROWSERS_PATH=/path/pw-browsers /path/pwvenv/bin/python metacoin_service/tests/browser/journey.py  [BASE] [HOME] [SHOTS]
PLAYWRIGHT_BROWSERS_PATH=/path/pw-browsers /path/pwvenv/bin/python metacoin_service/tests/browser/journey2.py
```

`journey.py` (32 checks): sign in, create a contract with new inputs, freeze and submit, watch the
worker finish, inspect the verdict/margins/interval chart, request review, reviewer decision with
bindings and recomputation, signature verification and tamper refusal, public and private exports,
dry run and dispatch, budget states, viewer boundaries, 400 px layout, sign-out.
`journey2.py` (23 checks): restart of API and worker during a browser session, a job that fails
with a safe error, expired session, reviewer/viewer/worker refusals, private export refused on
every HTTP method, anonymous ranged request refused. Credentials come from the private bootstrap
file and are never printed. Both scripts run against the live loopback instance and write
screenshots and a JSON result next to the working directory they are given.

Defect these checks found and that is now fixed: the console's inline stylesheet and inline
`style=` attributes were blocked by the service's own Content-Security-Policy, so every page
rendered unstyled; the stylesheet is now served from `/console/static/console.css` and the
interval chart is inline SVG.
