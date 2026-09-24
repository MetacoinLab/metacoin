"""Server-rendered console: sign-in, work list, contract creation, job detail, reviewer
decision, artifacts, budget, and the two decision tools. Every page calls the same
services as the API; nothing is hidden with CSS or browser JavaScript."""
import json
from pathlib import Path
from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
import jinja2
from experiments.work_contracts import energy_analysis as energy, fixtures
from . import auth, history, metering, scheduling
from .errors import ServiceError, from_exception
from .api import SENSITIVE_HEADERS

templates = Jinja2Templates(env=jinja2.Environment(loader=jinja2.FileSystemLoader(str(Path(__file__).parent / 'templates')),
                                                   autoescape=True))
SAMPLE_ENERGY = dict(fixtures.inputs('INDETERMINATE'), private_label='SAMPLE_SYNTHETIC')
SAMPLE_RUNTIME = {'available_low': 1_000_000, 'available_high': 1_100_000, 'reserve': 100_000,
                  'fixed_segments': [{'duration': 600, 'power_low': 800, 'power_high': 1000}],
                  'variable_power_low': 100, 'variable_power_high': 250, 'duration_cap': 3600,
                  'units': dict(energy.UNITS), 'assumptions': list(energy.ASSUMPTIONS), 'provenance': 'synthetic',
                  'private_label': 'SAMPLE_SYNTHETIC'}
SAMPLE_SELECT = {'available_low': 1_000_000, 'available_high': 1_100_000, 'reserve': 100_000,
                 'fixed_segments': [{'duration': 600, 'power_low': 800, 'power_high': 1000}],
                 'optional_tasks': [{'id': 'imaging', 'duration': 300, 'power_high': 500, 'value': 8},
                                    {'id': 'downlink', 'duration': 200, 'power_high': 900, 'value': 6},
                                    {'id': 'calibration', 'duration': 120, 'power_high': 300, 'value': 3}],
                 'duration_cap': 900, 'units': dict(energy.UNITS), 'assumptions': list(energy.ASSUMPTIONS),
                 'provenance': 'synthetic', 'private_label': 'SAMPLE_SYNTHETIC'}
SAMPLE_COMPARE = {'candidates': [{'id': 'plan-a', 'inputs': dict(fixtures.inputs('FEASIBLE'), private_label='SAMPLE_SYNTHETIC'), 'utility': 5},
                                 {'id': 'plan-b', 'inputs': dict(fixtures.inputs('INDETERMINATE'), private_label='SAMPLE_SYNTHETIC'), 'utility': 9},
                                 {'id': 'plan-c', 'inputs': dict(fixtures.inputs('INFEASIBLE'), private_label='SAMPLE_SYNTHETIC'), 'utility': 1}],
                  'objective': 'max_utility', 'private_label': 'SAMPLE_SYNTHETIC'}


def mount(app, svc):
    settings = svc.settings

    def render(request, name, status=200, **ctx):
        response = templates.TemplateResponse(request, name, ctx, status_code=status)
        for k, v in SENSITIVE_HEADERS.items():
            response.headers.setdefault(k, v)
        return response

    def session_principal(request, db):
        cookie = request.cookies.get('metacoin_session')
        if not cookie:
            return None
        try:
            return auth.authenticate_session(db, cookie)
        except ServiceError:
            return None

    async def page(request, fn, mutating=False):
        return await run_in_threadpool(page_sync, request, fn, mutating)

    def page_sync(request, fn, mutating=False):
        """Run fn(db, principal) inside one transaction; render errors as pages."""
        with svc.db.tx() as db:
            principal = session_principal(request, db)
            if principal is None:
                return RedirectResponse('/console/login?expired=1', status_code=303)
            try:
                if mutating:
                    auth.check_csrf(principal, request.state.form.get('csrf'))
                return fn(db, principal)
            except Exception as exc:
                err = from_exception(exc)
                db.execute('ROLLBACK'); db.execute('BEGIN')
                return render(request, 'error.html', status=err.status, principal=principal, error=err.body())

    async def form(request):
        request.state.form = dict(await request.form())
        return request.state.form

    CSS = (Path(__file__).parent / 'templates' / 'console.css').read_text()

    @app.get('/console/static/console.css')
    async def stylesheet():
        # Served from the same origin so the CSP (default-src 'self', no inline) applies to the console too.
        return Response(CSS, media_type='text/css', headers={'Cache-Control': 'public, max-age=3600'})

    @app.get('/', response_class=HTMLResponse)
    async def root(request: Request):
        return RedirectResponse('/console/', status_code=303)

    @app.get('/console/login', response_class=HTMLResponse)
    async def login_page(request: Request):
        return render(request, 'login.html', expired=request.query_params.get('expired'), principal=None, error=None)

    @app.post('/console/login', response_class=HTMLResponse)
    async def login(request: Request):
        f = await form(request)
        def do():
            with svc.db.tx() as db:
                try:
                    principal = auth.authenticate_bearer(db, f.get('token', ''))
                except ServiceError:
                    return None
                return auth.create_session(db, principal.id, settings.limits['session_seconds'])[0]
        sid = await run_in_threadpool(do)
        if sid is None:
            return render(request, 'login.html', status=401, expired=None, principal=None, error='credential not accepted')
        response = RedirectResponse('/console/', status_code=303)
        response.set_cookie('metacoin_session', sid, httponly=True, samesite='strict', secure=not settings.dev_http_loopback,
                            max_age=settings.limits['session_seconds'], path='/')
        return response

    @app.post('/console/logout')
    async def logout(request: Request):
        f = await form(request)
        def do():
            with svc.db.tx() as db:
                principal = session_principal(request, db)
                if principal is not None:
                    auth.check_csrf(principal, f.get('csrf'))
                    auth.end_session(db, principal.session['id'])
        await run_in_threadpool(do)
        response = RedirectResponse('/console/login', status_code=303)
        response.delete_cookie('metacoin_session', path='/')
        return response

    @app.get('/console/', response_class=HTMLResponse)
    async def index(request: Request):
        q = request.query_params
        def fn(db, p):
            rows, more = svc.jobs.list(db, p, state=q.get('state') or None, review_state=q.get('review_state') or None,
                                       before=q.get('before') or None)
            items = [svc.jobs.view(db, p, r) for r in rows]
            drafts = db.execute("SELECT * FROM contracts WHERE workspace=? AND state='draft' ORDER BY created_at DESC LIMIT 20", (p.workspace,)).fetchall() if p.can('contract:create') else []
            return render(request, 'index.html', principal=p, jobs=items, more=more, filters=dict(q),
                          next_before=rows[-1]['created_at'] if rows and more else None,
                          drafts=[svc.contracts.public_view(r) for r in drafts], budget=svc.actions.budget(db, p) if p.can('budget:read') else None)
        return await page(request, fn)

    @app.get('/console/contracts/new', response_class=HTMLResponse)
    async def contract_new(request: Request):
        kind = request.query_params.get('kind', 'energy_audit')
        def fn(db, p):
            p.require('contract:create')
            reviewers = db.execute("SELECT id, name FROM principals WHERE workspace=? AND role='reviewer' AND revoked_at IS NULL", (p.workspace,)).fetchall()
            sample = {'energy_audit': SAMPLE_ENERGY, 'safe_runtime': SAMPLE_RUNTIME, 'plan_comparison': SAMPLE_COMPARE, 'task_selection': SAMPLE_SELECT}[kind]
            return render(request, 'contract_new.html', principal=p, kind=kind, reviewers=reviewers,
                          sample=json.dumps(sample, indent=1), outcomes=energy.OUTCOMES, error=None, values={},
                          capability=svc.contracts.default_capability, provider_mode=settings.provider_mode)
        return await page(request, fn)

    @app.post('/console/contracts', response_class=HTMLResponse)
    async def contract_create(request: Request):
        f = await form(request)
        def fn(db, p):
            try:
                inputs = json.loads(f.get('inputs', ''))
            except ValueError:
                raise ServiceError('VALIDATION', 'inputs is not valid JSON')
            policy = {'reviewer_id': f.get('reviewer_id') or None,
                      'accepted_outcomes': [o for o in energy.OUTCOMES if f.get('accept_' + o)],
                      'disclose_outcome': bool(f.get('disclose_outcome')), 'disclose_explanation': bool(f.get('disclose_explanation')),
                      'amount': int(f.get('amount') or 1), 'expires_in_seconds': int(f.get('expires_in_seconds') or 86400)}
            cid = svc.contracts.create_draft(db, p, kind=f.get('kind'), title=f.get('title'), inputs=inputs, policy=policy)
            if f.get('freeze_and_submit'):
                svc.contracts.freeze(db, p, cid)
                jid = svc.jobs.submit(db, p, cid)
                return RedirectResponse('/console/jobs/' + jid, status_code=303)
            return RedirectResponse('/console/contracts/' + cid, status_code=303)
        return await page(request, fn, mutating=True)

    @app.get('/console/contracts/{contract_id}', response_class=HTMLResponse)
    async def contract_page(request: Request, contract_id: str):
        def fn(db, p):
            row = svc.contracts.get(db, p, contract_id)
            job = db.execute('SELECT id FROM jobs WHERE contract_id=?', (contract_id,)).fetchone()
            return render(request, 'contract.html', principal=p, contract=svc.contracts.public_view(row), job_id=job['id'] if job else None,
                          events=history.for_object(db, p.workspace, 'contract', contract_id))
        return await page(request, fn)

    @app.post('/console/contracts/{contract_id}/freeze')
    async def contract_freeze(request: Request, contract_id: str):
        await form(request)
        def fn(db, p):
            svc.contracts.freeze(db, p, contract_id)
            return RedirectResponse('/console/contracts/' + contract_id, status_code=303)
        return await page(request, fn, mutating=True)

    @app.post('/console/contracts/{contract_id}/submit')
    async def contract_submit(request: Request, contract_id: str):
        await form(request)
        def fn(db, p):
            jid = svc.jobs.submit(db, p, contract_id)
            return RedirectResponse('/console/jobs/' + jid, status_code=303)
        return await page(request, fn, mutating=True)

    @app.get('/console/jobs/{job_id}', response_class=HTMLResponse)
    async def job_page(request: Request, job_id: str):
        def fn(db, p):
            row = svc.jobs.get(db, p, job_id)
            view = svc.jobs.view(db, p, row)
            contract = svc.contracts.public_view(db.execute('SELECT * FROM contracts WHERE id=?', (row['contract_id'],)).fetchone())
            review = db.execute('SELECT * FROM reviews WHERE job_id=?', (job_id,)).fetchone()
            arts = db.execute('SELECT * FROM artifacts WHERE job_id=? OR contract_id=? ORDER BY created_at', (job_id, row['contract_id'])).fetchall()
            visible = [a for a in arts if a['public'] or p.can('artifact:read_private') or (p.role == 'reviewer' and contract['reviewer_id'] == p.id)]
            return render(request, 'job.html', principal=p, job=view, contract=contract, review=svc.reviews.view(review) if review else None,
                          artifacts=visible, events=history.for_object(db, p.workspace, 'job', job_id),
                          sale=db.execute('SELECT * FROM sales WHERE job_id=?', (job_id,)).fetchone())
        return await page(request, fn)

    @app.post('/console/jobs/{job_id}/{op}')
    async def job_op(request: Request, job_id: str, op: str):
        f = await form(request)
        def fn(db, p):
            if op == 'cancel':
                svc.jobs.cancel(db, p, job_id)
            elif op == 'review-request':
                svc.reviews.request(db, p, job_id)
            elif op == 'action':
                svc.actions.create(db, p, job_id, f.get('request_id') or ('req-' + job_id), None, dry_run=bool(f.get('dry_run')))
            elif op == 'reconcile':
                svc.actions.reconcile(db, p, job_id)
            else:
                raise ServiceError('NOT_FOUND', 'operation')
            return RedirectResponse('/console/jobs/' + job_id, status_code=303)
        return await page(request, fn, mutating=True)

    @app.get('/console/reviews/{job_id}', response_class=HTMLResponse)
    async def review_page(request: Request, job_id: str):
        def fn(db, p):
            evidence = svc.reviews.evidence(db, p, job_id)
            existing = db.execute('SELECT * FROM reviews WHERE job_id=?', (job_id,)).fetchone()
            return render(request, 'review.html', principal=p, e=evidence, existing=svc.reviews.view(existing) if existing else None,
                          terms_json=json.dumps(evidence['contract_terms'], indent=1))
        return await page(request, fn)

    @app.post('/console/reviews/{job_id}/decision')
    async def review_decide(request: Request, job_id: str):
        f = await form(request)
        def fn(db, p):
            svc.reviews.decide(db, p, job_id, f.get('decision'))
            return RedirectResponse('/console/reviews/' + job_id, status_code=303)
        return await page(request, fn, mutating=True)

    @app.get('/console/budget', response_class=HTMLResponse)
    async def budget_page(request: Request):
        def fn(db, p):
            b = svc.actions.budget(db, p)
            acts = db.execute('SELECT * FROM payment_actions WHERE workspace=? ORDER BY created_at DESC LIMIT 50', (p.workspace,)).fetchall()
            rows = []
            for a in acts:
                j = db.execute('SELECT * FROM jobs WHERE id=?', (a['job_id'],)).fetchone()
                rows.append({'job_id': a['job_id'], 'request_id': a['request_id'], 'provider_mode': a['provider_mode'],
                             'payment': svc.jobs.payment_view(db, p, j)})
            sales = db.execute('SELECT job_id, payment_id, amount, asset, network, state, provider_mode FROM sales WHERE workspace=? ORDER BY created_at DESC LIMIT 50', (p.workspace,)).fetchall()
            return render(request, 'budget.html', principal=p, budget=b, actions=rows, sales=sales)
        return await page(request, fn)

    # ---- services, datasets, workflows, campaigns, agents, usage, queue --------------------
    @app.get('/console/services', response_class=HTMLResponse)
    async def services_page(request: Request):
        def fn(db, p):
            services = svc.catalog.list(db, p)
            services = services['items'] if isinstance(services, dict) else services
            quotes = db.execute('SELECT id, service_id, amount_max, asset, state, expires_at FROM quotes WHERE workspace=? ORDER BY created_at DESC LIMIT 30', (p.workspace,)).fetchall() if p.can('contract:read') else []
            return render(request, 'services.html', principal=p, services=services, quotes=quotes)
        return await page(request, fn)

    @app.get('/console/datasets', response_class=HTMLResponse)
    async def datasets_page(request: Request):
        def fn(db, p):
            items = svc.datasets.list(db, p)
            items = items['items'] if isinstance(items, dict) else items
            return render(request, 'datasets.html', principal=p, datasets=items)
        return await page(request, fn)

    @app.get('/console/workflows', response_class=HTMLResponse)
    async def workflows_page(request: Request):
        def fn(db, p):
            p.require('job:read')
            defs = db.execute('SELECT id, name, digest, definition_json, created_at FROM workflow_definitions WHERE workspace=? ORDER BY created_at DESC LIMIT 50', (p.workspace,)).fetchall()
            definitions = [{'id': d['id'], 'name': d['name'], 'digest': d['digest'], 'created_at': d['created_at'], 'node_count': len(json.loads(d['definition_json'])['nodes'])} for d in defs]
            return render(request, 'workflows.html', principal=p, definitions=definitions, runs=svc.workflows.list(db, p))
        return await page(request, fn)

    @app.get('/console/runs/{run_id}', response_class=HTMLResponse)
    async def run_page(request: Request, run_id: str):
        return await page(request, lambda db, p: render(request, 'run.html', principal=p, run=svc.workflows.view(db, p, run_id)))

    @app.post('/console/runs/{run_id}/cancel', response_class=HTMLResponse)
    async def run_cancel(request: Request, run_id: str):
        await form(request)
        def fn(db, p):
            svc.workflows.cancel(db, p, run_id)
            return RedirectResponse('/console/runs/' + run_id, status_code=303)
        return await page(request, fn, mutating=True)

    @app.get('/console/campaigns', response_class=HTMLResponse)
    async def campaigns_page(request: Request):
        def fn(db, p):
            p.require('job:read')
            rows = db.execute('SELECT id FROM sci_campaigns WHERE workspace=? ORDER BY created_at DESC LIMIT 50', (p.workspace,)).fetchall()
            items = []
            for r in rows:
                v = svc.campaigns.view(db, p, r['id'])
                items.append({'id': r['id'], 'name': v['name'], 'kind': v['kind'], 'state': v['state'], 'done': v['done'], 'total_candidates': v['total_candidates'], 'updated_at': v['updated_at']})
            return render(request, 'campaigns.html', principal=p, campaigns=items)
        return await page(request, fn)

    @app.get('/console/campaigns/{campaign_id}', response_class=HTMLResponse)
    async def campaign_page(request: Request, campaign_id: str):
        def fn(db, p):
            v = svc.campaigns.view(db, p, campaign_id)
            res = svc.campaigns.results(db, p, campaign_id)
            rows = res['rows'] if isinstance(res, dict) else res
            return render(request, 'campaign.html', principal=p, c=v, rows=rows)
        return await page(request, fn)

    @app.get('/console/agents', response_class=HTMLResponse)
    async def agents_page(request: Request):
        return await page(request, lambda db, p: render(request, 'agents.html', principal=p, grants=svc.agents.list(db, p)['items']))

    @app.post('/console/agents/{gid}/stop', response_class=HTMLResponse)
    async def agent_stop(request: Request, gid: str):
        await form(request)
        def fn(db, p):
            svc.agents.stop(db, p, gid, revoke=False)
            return RedirectResponse('/console/agents', status_code=303)
        return await page(request, fn, mutating=True)

    @app.get('/console/usage', response_class=HTMLResponse)
    async def usage_page(request: Request):
        def fn(db, p):
            p.require('budget:read')
            rows = db.execute('SELECT * FROM usage_records WHERE workspace=? ORDER BY created_at DESC LIMIT 100', (p.workspace,)).fetchall()
            return render(request, 'usage.html', principal=p, usage=[metering.view(db, r) for r in rows])
        return await page(request, fn)

    @app.get('/console/queue', response_class=HTMLResponse)
    async def queue_page(request: Request):
        return await page(request, lambda db, p: render(request, 'queue.html', principal=p, q=scheduling.queue(db, p)))

    @app.post('/console/workers/{worker_id}/{action}', response_class=HTMLResponse)
    async def worker_action(request: Request, worker_id: str, action: str):
        await form(request)
        def fn(db, p):
            scheduling.set_worker_state(db, p, worker_id, 'draining' if action == 'drain' else 'active')
            return RedirectResponse('/console/queue', status_code=303)
        return await page(request, fn, mutating=True)

    @app.get('/console/history', response_class=HTMLResponse)
    async def history_page(request: Request):
        def fn(db, p):
            p.require('history:read')
            rows = db.execute('SELECT * FROM events WHERE workspace=? ORDER BY seq DESC LIMIT 100', (p.workspace,)).fetchall()
            return render(request, 'history.html', principal=p, events=[dict(r, ref=json.loads(r['ref_json'])) for r in rows],
                          chain=history.verify_chain(db, p.workspace))
        return await page(request, fn)
