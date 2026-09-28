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
from .compute import service as compute_svc, manifests as compute_manifests, inputs as compute_inputs
from .models import service as model_svc
from .knowledge import retrieval as retrieval_mod, engine as knowledge_engine
from . import statements as statements_mod, verification as verification_mod
from .errors import ServiceError, from_exception
from .api import SENSITIVE_HEADERS, model_host_of, preloaded_generation_host

_env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(Path(__file__).parent / 'templates')), autoescape=True)
_env.filters['zip'] = lambda a, b: list(zip(a, b))
templates = Jinja2Templates(env=_env)
SAMPLE_ENERGY = dict(fixtures.inputs('INDETERMINATE'), private_label='SAMPLE_SYNTHETIC')
SAMPLE_TEMPORAL_BASE = {'schema': 'temporal-energy-input/v1', 'capacity': 10_000, 'initial_low': 6_000, 'initial_high': 6_000, 'reserve': 2_000,
                        'segments': [{'duration': 10, 'harvest_low': 600, 'harvest_high': 800, 'load_low': 500, 'load_high': 500, 'leakage_low': 0, 'leakage_high': 0},
                                     {'duration': 30, 'harvest_low': 0, 'harvest_high': 0, 'load_low': 100, 'load_high': 200, 'leakage_low': 0, 'leakage_high': 5}],
                        'units': {'energy': 'mJ', 'power': 'mW', 'duration': 's'},
                        'assumptions': ['piecewise_constant_power_bounds', 'independent_interval_bounds', 'powers_at_usable_energy_boundary', 'saturation_at_capacity', 'constant_reserve',
                                        'virtual_energy_below_reserve_for_diagnostics', 'no_unmodeled_loads', 'no_recharge_physics'], 'provenance': 'synthetic', 'private_label': 'CONSOLE_SAMPLE'}
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
            compute = compute_svc.view(db, p, svc.jobs, job_id) if db.execute('SELECT 1 FROM compute_runs WHERE job_id=?', (job_id,)).fetchone() else None
            batch_rows = None; plan = None
            if compute and compute.get('output_artifact_id') and row['kind'] == 'resource_plan':
                try:
                    plan = compute_svc.plan_json(db, p, svc.jobs, svc.store, job_id)[1]
                except ServiceError:
                    plan = None
            if compute and compute.get('output_artifact_id') and row['kind'] == 'temporal_batch':
                try:
                    listing, _ = compute_svc.outputs(db, p, svc.jobs, svc.store, job_id, 'results.json')
                    batch_rows = json.loads(listing)['rows'][:50]
                except Exception:
                    batch_rows = None
            return render(request, 'job.html', principal=p, job=view, contract=contract, review=svc.reviews.view(review) if review else None,
                          artifacts=visible, events=history.for_object(db, p.workspace, 'job', job_id), compute=compute, batch_rows=batch_rows, plan=plan,
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
        return await page(request, lambda db, p: render(request, 'agents.html', principal=p, grants=svc.agents.list(db, p)['items'], plans=svc.planner.list(db, p), intents=svc.intents.list(db, p)))

    @app.post('/console/agents/intents', response_class=HTMLResponse)
    async def intent_form(request: Request):
        f = await form(request)
        def do():
            host = preloaded_generation_host(svc)
            def fn(db, p):
                req = {'text': f.get('text', '')}
                if f.get('kind'): req['kind'] = f['kind']
                if f.get('collection_id'): req['collection_id'] = f['collection_id']
                svc.intents.compile(db, p, req, host)
                return RedirectResponse('/console/agents', status_code=303)
            return page_sync(request, fn, True)
        return await run_in_threadpool(do)

    @app.post('/console/agents/intents/{iid}/continue', response_class=HTMLResponse)
    async def intent_continue_form(request: Request, iid: str):
        f = await form(request)
        def do():
            host = preloaded_generation_host(svc)
            def fn(db, p):
                answers = {k[2:]: v for k, v in f.items() if k.startswith('a_') and v}
                svc.intents.continue_intent(db, p, iid, f.get('token'), answers, None, host)
                return RedirectResponse('/console/agents', status_code=303)
            return page_sync(request, fn, True)
        return await run_in_threadpool(do)

    @app.post('/console/agents/plans/{pid}/accept', response_class=HTMLResponse)
    async def plan_accept_form(request: Request, pid: str):
        await form(request)
        def fn(db, p):
            svc.planner.accept(db, p, pid)
            return RedirectResponse('/console/agents', status_code=303)
        return await page(request, fn, mutating=True)

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

    # ---- compute -----------------------------------------------------------------------------
    SAMPLES_COMPUTE = {
        'temporal_batch': {'schema': compute_inputs.TEMPORAL_BATCH_SCHEMA, 'base': SAMPLE_TEMPORAL_BASE, 'scenarios': None,
                           'grid': [{'path': 'reserve', 'start': 0, 'stop': 9000, 'step': 500}, {'path': 'load_scale_percent', 'values': [50, 100, 150, 200]}],
                           'device_policy': 'auto', 'verification': 'auto', 'private_label': 'CONSOLE_SAMPLE_BATCH'},
        'monte_carlo_reliability': {'schema': compute_inputs.MONTE_CARLO_SCHEMA, 'base': dict(SAMPLE_TEMPORAL_BASE, segments=[dict(s, harvest_high=s['harvest_low'], load_high=s['load_low'], leakage_high=s['leakage_low']) for s in SAMPLE_TEMPORAL_BASE['segments']]),
                                    'distributions': {'initial_energy': {'type': 'finite', 'values': [4000, 6000, 8000], 'weights': [1, 2, 1]}, 'load_scale_percent': {'type': 'uniform_int', 'low': 50, 'high': 200}},
                                    'samples': 20000, 'seed': 11, 'confidence_percent': 95, 'event': 'reserve_maintained', 'device_policy': 'auto', 'private_label': 'CONSOLE_SAMPLE_MC'},
        'heat_diffusion': {'schema': compute_inputs.HEAT_SCHEMA, 'nx': 128, 'ny': 128, 'dx': '0.01', 'dy': '0.01', 'dt': '0.00002', 'alpha': '1.0', 'steps': 2000,
                           'boundary': {'type': 'dirichlet', 'values': {'left': '0', 'right': '0', 'top': '0', 'bottom': '0'}},
                           'initial': {'type': 'gaussian', 'center_x': '0.64', 'center_y': '0.64', 'sigma': '0.15', 'amplitude': '100', 'background': '0'}, 'snapshots': 2,
                           'units': {'field': 'K', 'length': 'm', 'time': 's'}, 'device_policy': 'auto', 'precision': 'float64', 'private_label': 'CONSOLE_SAMPLE_HEAT'},
        'calibration_fit': {'schema': compute_inputs.CALIBRATION_SCHEMA, 'dataset_id': 'cds_...', 'features': ['work_units'], 'target': 'duration_seconds', 'device_policy': 'cpu'},
        'resource_plan': {'schema': compute_inputs.RESOURCE_PLAN_SCHEMA, 'slot_seconds': 60, 'slots': 8, 'capacity': 120000, 'initial_low': 60000, 'reserve': 20000,
                          'supply_low': [500, 500, 400, 400, 300, 300, 500, 500], 'supply_high': [600, 600, 500, 500, 400, 400, 600, 600], 'base_low': [100] * 8, 'base_high': [150] * 8,
                          'uncertainty_interpretation': 'specification_bound', 'resources': {'radio': 1, 'cpu': 2},
                          'tasks': [{'id': 'downlink', 'utility': 6, 'duration': 2, 'power_high': 500, 'resources': {'radio': 1, 'cpu': 1}, 'cost': 4},
                                    {'id': 'science', 'utility': 5, 'duration': 3, 'power_high': 300, 'resources': {'cpu': 2}, 'cost': 2},
                                    {'id': 'compress', 'utility': 3, 'duration': 1, 'power_high': 200, 'resources': {'cpu': 1}, 'cost': 1, 'dependencies': ['science']},
                                    {'id': 'housekeeping', 'mandatory': True, 'utility': 0, 'duration': 1, 'power_high': 100, 'earliest_start': 6, 'latest_start': 7}],
                          'objectives': {'mode': 'cost_sweep', 'cost_ceilings': [0, 3, 5, 7]}, 'sensitivity': [{'parameter': 'reserve', 'value': 40000}, {'parameter': 'supply_scale_percent', 'value': 70}],
                          'time_limit_s': 10, 'device_policy': 'cpu', 'private_label': 'CONSOLE_SAMPLE_PLAN'},
    }

    @app.get('/console/compute', response_class=HTMLResponse)
    async def compute_page(request: Request):
        def fn(db, p):
            p.require('job:read')
            caps = compute_svc.capabilities(db, settings)
            rows = db.execute('SELECT job_id FROM compute_runs WHERE workspace=? ORDER BY updated_at DESC LIMIT 50', (p.workspace,)).fetchall()
            jobs = [compute_svc.view(db, p, svc.jobs, r['job_id']) for r in rows]
            return render(request, 'compute.html', principal=p, caps=caps, jobs=jobs)
        return await page(request, fn)

    @app.get('/console/compute/new', response_class=HTMLResponse)
    async def compute_new(request: Request):
        kind = request.query_params.get('kind', 'temporal_batch')
        def fn(db, p):
            p.require('contract:create')
            if kind not in compute_manifests.KINDS:
                raise ServiceError('VALIDATION', 'kind')
            caps = compute_svc.capabilities(db, settings)
            reviewers = db.execute("SELECT id, name FROM principals WHERE workspace=? AND role='reviewer' AND revoked_at IS NULL", (p.workspace,)).fetchall()
            srow = db.execute('SELECT * FROM services WHERE kind=? AND status=? ORDER BY version DESC LIMIT 1', (kind, 'registered')).fetchone()
            price = svc.catalog.view(srow)['price'] if srow else {'amount_per_unit': None, 'asset': None}
            return render(request, 'compute_new.html', principal=p, kind=kind, manifest=caps['manifests'][kind], devices=caps['facts']['currently_available']['live_worker_devices'],
                          reviewers=reviewers, price=price, sample=json.dumps(SAMPLES_COMPUTE[kind], indent=1), values={}, estimate=None, error=None)
        return await page(request, fn)

    @app.post('/console/compute', response_class=HTMLResponse)
    async def compute_create(request: Request):
        f = await form(request)
        def fn(db, p):
            kind = f.get('kind')
            if kind not in compute_manifests.KINDS:
                raise ServiceError('VALIDATION', 'kind')
            caps = compute_svc.capabilities(db, settings)
            reviewers = db.execute("SELECT id, name FROM principals WHERE workspace=? AND role='reviewer' AND revoked_at IS NULL", (p.workspace,)).fetchall()
            srow = db.execute('SELECT * FROM services WHERE kind=? AND status=? ORDER BY version DESC LIMIT 1', (kind, 'registered')).fetchone()
            price = svc.catalog.view(srow)['price'] if srow else {'amount_per_unit': None, 'asset': None}
            ctx = dict(principal=p, kind=kind, manifest=caps['manifests'][kind], devices=caps['facts']['currently_available']['live_worker_devices'], reviewers=reviewers, price=price,
                       sample=json.dumps(SAMPLES_COMPUTE[kind], indent=1), values=dict(f), estimate=None, error=None)
            try:
                inputs = json.loads(f.get('inputs', ''))
                compute_inputs.VALIDATORS[kind](inputs)
            except ValueError as exc:
                return render(request, 'compute_new.html', status=422, **dict(ctx, error='inputs are not valid JSON'))
            except Exception as exc:
                return render(request, 'compute_new.html', status=422, **dict(ctx, error='refused: ' + str(exc)[:300]))
            units = compute_inputs.work_units(kind, inputs)
            ctx['estimate'] = {'work_units': units, 'max_charge': (price['amount_per_unit'] or 0) * units, 'device_policy': inputs.get('device_policy', 'auto')}
            if f.get('submit'):
                cid = svc.contracts.create_draft(db, p, kind=kind, title=f.get('title') or kind, inputs=inputs, policy={'reviewer_id': f.get('reviewer_id') or None})
                svc.contracts.freeze(db, p, cid)
                jid = svc.jobs.submit(db, p, cid)
                return RedirectResponse('/console/jobs/' + jid, status_code=303)
            return render(request, 'compute_new.html', **ctx)
        return await page(request, fn, mutating=True)

    @app.post('/console/compute/{job_id}/{action}', response_class=HTMLResponse)
    async def compute_control(request: Request, job_id: str, action: str):
        f = await form(request)
        def fn(db, p):
            if action == 'freeze-alternative':
                out = compute_svc.freeze_alternative(db, p, svc.jobs, svc.store, svc.workflows, job_id, int(f.get('cost_ceiling', '0')), f.get('title') or None)
                return RedirectResponse('/console/workflows/' + out['workflow_id'], status_code=303)
            compute_svc.control(db, p, svc.jobs, job_id, action)
            return RedirectResponse('/console/jobs/' + job_id, status_code=303)
        return await page(request, fn, mutating=True)

    # ---- 24h expansion pages: Models, Knowledge, Calibration, Verification, Nodes, Approvals, Statement -----------
    @app.get('/console/models', response_class=HTMLResponse)
    async def models_page(request: Request):
        def fn(db, p):
            p.require('job:read')
            models = svc.models.list(db, p)
            rows = db.execute('SELECT job_id FROM model_requests WHERE workspace=? ORDER BY updated_at DESC LIMIT 30', (p.workspace,)).fetchall()
            jobs = [model_svc.view(db, p, svc.jobs, r['job_id']) for r in rows]
            facts = model_svc.runtime_facts(db, settings, api_host=svc._model_host)
            suites = svc.evaluation.list_suites(db, p)
            runs = [svc.evaluation.run_view(db, p, r['id']) for r in db.execute('SELECT id FROM evaluation_runs WHERE workspace=? ORDER BY created_at DESC LIMIT 20', (p.workspace,)).fetchall()]
            gate = db.execute('SELECT value FROM meta WHERE key=?', ('evaluation_gate:' + p.workspace,)).fetchone()
            return render(request, 'models.html', principal=p, models=models, jobs=jobs, facts=facts, generate_default='generate' in facts['defaults'], suites=suites, eval_runs=runs, eval_gate=gate['value'] if gate else None)
        return await page(request, fn)

    @app.post('/console/models/generate', response_class=HTMLResponse)
    async def models_generate_form(request: Request):
        f = await form(request)
        def fn(db, p):
            p.require('model:use')
            from .api import quick_submit
            try:
                mo = int(f.get('max_output_tokens', '48'))
            except ValueError:
                raise ServiceError('VALIDATION', 'max_output_tokens')
            out = quick_submit(svc, db, p, 'text_generation', {'schema': model_svc.GENERATION_SCHEMA, 'prompt': f.get('prompt', ''), 'max_output_tokens': mo}, 'console generation')
            return RedirectResponse('/console/jobs/' + out['job_id'], status_code=303)
        return await page(request, fn, mutating=True)

    @app.post('/console/models/{rid}/{action}', response_class=HTMLResponse)
    async def models_action_form(request: Request, rid: str, action: str):
        f = await form(request)
        def fn(db, p):
            if action == 'promote':
                from .approvals import gate as approval_gate
                approval_gate(svc.approvals, db, p, 'model_promote')
                svc.models.promote(db, p, rid, f.get('operation'), {'source': 'console'})
            elif action == 'retire':
                svc.models.retire(db, p, rid, 'console')
            elif action in ('load', 'unload'):
                model_svc.request_load(db, p, settings, rid, 'loaded' if action == 'load' else 'unloaded')
            else:
                raise ServiceError('NOT_FOUND', 'action')
            return RedirectResponse('/console/models', status_code=303)
        return await page(request, fn, mutating=True)

    def knowledge_ctx(db, p, results=None):
        cols = svc.knowledge.list_collections(db, p)
        for c in cols:
            c['documents_list'] = svc.knowledge.list_documents(db, p, c['id'])
        answers = [dict(r, citations=json.loads(r['citations_json'])) for r in db.execute('SELECT * FROM knowledge_answers WHERE workspace=? AND principal_id=? ORDER BY created_at DESC LIMIT 30', (p.workspace, p.id)).fetchall()]
        return dict(principal=p, collections=cols, answers=answers, results=results)

    # ---- documents (Order 07 group A) --------------------------------------------------------------------------
    @app.get('/console/documents', response_class=HTMLResponse)
    async def documents_page(request: Request):
        def fn(db, p):
            p.require('knowledge:read')
            return render(request, 'documents.html', principal=p, imports=svc.documents.list(db, p), collections=svc.knowledge.list_collections(db, p), limits=settings.limits)
        return await page(request, fn)

    @app.post('/console/documents/import', response_class=HTMLResponse)
    async def documents_import_form(request: Request):
        f = await form(request)
        upload = f.get('file')
        content = await upload.read() if hasattr(upload, 'read') else b''
        name = getattr(upload, 'filename', None) or 'upload.pdf'
        def fn(db, p):
            v = svc.documents.create_import(db, p, name=name[:120], fmt='pdf', content=content, collection_id=f.get('collection_id') or None, policy={'mode': f.get('mode', 'ocr_needed')})
            return RedirectResponse('/console/documents/' + v['id'], status_code=303)
        return await page(request, fn, mutating=True)

    @app.get('/console/documents/{iid}', response_class=HTMLResponse)
    async def document_page(request: Request, iid: str):
        def fn(db, p):
            d = svc.documents.view(db, p, iid)
            pages = svc.documents.pages(db, p, iid)['pages'] if d['extraction'] else []
            pages = [{k: pg[k] for k in ('index', 'display_number', 'method', 'excluded', 'warnings', 'ocr')} for pg in pages]
            return render(request, 'document.html', principal=p, d=d, pages=pages, removal=None)
        return await page(request, fn)

    @app.post('/console/documents/{iid}/{action}', response_class=HTMLResponse)
    async def document_action(request: Request, iid: str, action: str):
        f = await form(request)
        def fn(db, p):
            if action == 'cancel':
                svc.documents.cancel(db, p, iid)
            elif action == 'retry':
                svc.documents.retry(db, p, iid, {'mode': f.get('mode')} if f.get('mode') else None)
            elif action == 'publish':
                svc.documents.publish(db, p, iid, False)
            elif action == 'remove':
                rep = svc.documents.remove(db, p, iid, confirm=f.get('confirm') == '1')
                if not rep['applied']:
                    d = svc.documents.view(db, p, iid)
                    pages = [{k: pg[k] for k in ('index', 'display_number', 'method', 'excluded', 'warnings', 'ocr')} for pg in (svc.documents.pages(db, p, iid)['pages'] if d['extraction'] else [])]
                    return render(request, 'document.html', principal=p, d=d, pages=pages, removal=rep)
            else:
                raise ServiceError('NOT_FOUND', 'action')
            return RedirectResponse('/console/documents/' + iid, status_code=303)
        return await page(request, fn, mutating=True)

    @app.get('/console/documents/{iid}/pages/{index}', response_class=HTMLResponse)
    async def document_page_inspector(request: Request, iid: str, index: int):
        def fn(db, p):
            d = svc.documents.view(db, p, iid)
            pg = svc.documents.page(db, p, iid, index)['page']
            return render(request, 'document_page.html', principal=p, d=d, page=pg, highlight=request.query_params.get('q'))
        return await page(request, fn)

    @app.get('/console/documents/tables/{tid}', response_class=HTMLResponse)
    async def document_table_page(request: Request, tid: str):
        def fn(db, p):
            t = svc.documents.table(db, p, tid); d = svc.documents.view(db, p, t['import_id']); eff = svc.documents.effective_table(db, p, tid)
            maps = [svc.documents.mapping(db, p, r['id']) for r in db.execute('SELECT id FROM dataset_mappings WHERE table_id=? ORDER BY created_at', (tid,)).fetchall()]
            return render(request, 'document_table.html', principal=p, t=t, d=d, eff=eff, mappings=maps, preview=None, values={})
        return await page(request, fn)

    @app.post('/console/documents/tables/{tid}/annotations', response_class=HTMLResponse)
    async def document_table_annotate(request: Request, tid: str):
        f = await form(request)
        def fn(db, p):
            kind = f.get('kind'); payload = {'reason': f.get('reason', '')}
            if f.get('row'): payload['row'] = int(f['row'])
            if f.get('col'): payload['col'] = int(f['col'])
            if kind == 'unit': payload['unit'] = f.get('value')
            elif kind == 'cell_correction': payload['new_value'] = f.get('value', '')
            elif kind == 'locale': payload['locale'] = f.get('value')
            svc.documents.annotate(db, p, tid, kind, payload)
            return RedirectResponse('/console/documents/tables/' + tid, status_code=303)
        return await page(request, fn, mutating=True)

    @app.post('/console/documents/tables/{tid}/mapping', response_class=HTMLResponse)
    async def document_table_mapping(request: Request, tid: str):
        f = await form(request)
        def fn(db, p):
            try:
                columns = json.loads(f.get('columns') or '[]')
            except ValueError:
                raise ServiceError('VALIDATION', 'columns: JSON list')
            mapping = {'schema': 'metacoin-table-mapping/v1', 'target': f.get('target'), 'columns': columns, 'locale': f.get('locale', 'undeclared'), 'missing_policy': f.get('missing_policy', 'reject'), 'rounding': f.get('rounding', 'reject'),
                       'row_exclusions': [int(x) for x in (f.get('row_exclusions') or '').replace(' ', '').split(',') if x]}
            if f.get('action') == 'create':
                svc.documents.create_mapping(db, p, tid, mapping)
                return RedirectResponse('/console/documents/tables/' + tid, status_code=303)
            pv = svc.documents.validate_mapping(db, p, tid, mapping)
            t = svc.documents.table(db, p, tid); d = svc.documents.view(db, p, t['import_id']); eff = svc.documents.effective_table(db, p, tid)
            maps = [svc.documents.mapping(db, p, r['id']) for r in db.execute('SELECT id FROM dataset_mappings WHERE table_id=? ORDER BY created_at', (tid,)).fetchall()]
            return render(request, 'document_table.html', principal=p, t=t, d=d, eff=eff, mappings=maps, preview=pv, values=dict(f))
        return await page(request, fn, mutating=True)

    @app.post('/console/documents/mappings/{mid}/confirm', response_class=HTMLResponse)
    async def document_mapping_confirm(request: Request, mid: str):
        await form(request)
        def fn(db, p):
            m = svc.documents.confirm_mapping(db, p, mid)
            return RedirectResponse('/console/documents/tables/' + m['table_id'], status_code=303)
        return await page(request, fn, mutating=True)

    # ---- Group E: analyses, reports, projections ------------------------------------------------------------------
    @app.get('/console/analyses', response_class=HTMLResponse)
    async def analyses_page(request: Request):
        def fn(db, p):
            p.require('knowledge:read')
            return render(request, 'analyses.html', principal=p, analyses=svc.analyses.list(db, p))
        return await page(request, fn)

    @app.post('/console/analyses', response_class=HTMLResponse)
    async def analyses_create(request: Request):
        f = await form(request)
        def fn(db, p):
            blocks = [{'id': 'aim', 'type': 'text', 'heading': 'Aim', 'text': f.get('text') or 'New analysis.'}]
            a = svc.analyses.create(db, p, f.get('name', ''), blocks, f.get('from_document') or None, f.get('from_workflow') or None, 'console')
            return RedirectResponse('/console/analyses/' + a['id'], status_code=303)
        return await page(request, fn, mutating=True)

    @app.get('/console/analyses/{aid}', response_class=HTMLResponse)
    async def analysis_page(request: Request, aid: str):
        version = request.query_params.get('version')
        def fn(db, p):
            a = svc.analyses.view(db, p, aid, int(version) if version and version.isdigit() else None)
            return render(request, 'analysis.html', principal=p, a=a, impact=None, error=None)
        return await page(request, fn)

    def _strip(blocks):
        return [{k: v for k, v in b.items() if k not in ('status', 'stale', 'requires', 'reference', 'reference_drift')} for b in blocks]

    @app.post('/console/analyses/{aid}/blocks', response_class=HTMLResponse)
    async def analysis_add_block(request: Request, aid: str):
        f = await form(request)
        def fn(db, p):
            a = svc.analyses.view(db, p, aid)
            try:
                fields = json.loads(f.get('fields') or '{}')
            except ValueError:
                return render(request, 'analysis.html', status=422, principal=p, a=a, impact=None, error='typed fields are not valid JSON')
            if type(fields) is not dict:
                return render(request, 'analysis.html', status=422, principal=p, a=a, impact=None, error='typed fields must be a JSON object')
            b = dict(fields, id=f.get('id', ''), type=f.get('type', 'text'))
            if f.get('text'):
                b['text'] = f['text']
            ref = (f.get('ref') or '').strip()
            if ref:
                kind, _, rid = ref.partition(':'); b['ref_kind'] = kind; b['ref_id'] = rid
            deps = [d.strip() for d in (f.get('depends_on') or '').split(',') if d.strip()]
            if deps:
                b['depends_on'] = deps
            try:
                svc.analyses.revise(db, p, aid, _strip(a['blocks']) + [b], int(f.get('expected_version', '0')), 'console')
            except ServiceError as exc:
                return render(request, 'analysis.html', status=exc.status, principal=p, a=a, impact=None, error='refused: ' + json.dumps(exc.detail)[:400])
            return RedirectResponse('/console/analyses/' + aid, status_code=303)
        return await page(request, fn, mutating=True)

    @app.post('/console/analyses/{aid}/freeze', response_class=HTMLResponse)
    async def analysis_freeze_form(request: Request, aid: str):
        f = await form(request)
        def fn(db, p):
            svc.analyses.freeze(db, p, aid, int(f.get('version', '0')), f.get('reason', ''))
            return RedirectResponse('/console/analyses/' + aid, status_code=303)
        return await page(request, fn, mutating=True)

    @app.post('/console/analyses/{aid}/impact', response_class=HTMLResponse)
    async def analysis_impact_form(request: Request, aid: str):
        f = await form(request)
        def fn(db, p):
            a = svc.analyses.view(db, p, aid)
            imp = svc.analyses.impact(db, p, aid, {'block': f.get('block', '')})
            return render(request, 'analysis.html', principal=p, a=a, impact=imp, error=None)
        return await page(request, fn, mutating=True)

    @app.post('/console/analyses/{aid}/reports', response_class=HTMLResponse)
    async def analysis_report_form(request: Request, aid: str):
        f = await form(request)
        host = preloaded_generation_host(svc) if f.get('mode') == 'model' else None
        def fn(db, p):
            rep = svc.analyses.build_report(db, p, aid, int(f.get('version', '0')), f.get('mode', 'deterministic'), host)
            return RedirectResponse('/console/reports/' + rep['id'], status_code=303)
        return await page(request, fn, mutating=True)

    def _report_ctx(db, p, rid, **extra):
        r = svc.analyses.report(db, p, rid)
        body = svc.analyses.report_html(db, p, rid)
        body = body.split('<body>', 1)[-1].rsplit('</body>', 1)[0]
        return dict(principal=p, r=r, body=body, preview=None, exported=None, values={}, **extra)

    @app.get('/console/reports/{rid}', response_class=HTMLResponse)
    async def report_page(request: Request, rid: str):
        def fn(db, p):
            return render(request, 'report.html', **_report_ctx(db, p, rid))
        return await page(request, fn)

    @app.post('/console/reports/{rid}/projection', response_class=HTMLResponse)
    async def report_projection_form(request: Request, rid: str):
        rawf = await request.form()
        blocks = rawf.getlist('blocks')
        f = dict(rawf); request.state.form = f
        def fn(db, p):
            try:
                fields = json.loads(f.get('fields') or '{}')
            except ValueError:
                fields = {}
            scope = {'blocks': blocks, 'fields': fields, 'include_quotes': bool(f.get('include_quotes')), 'include_assumption_values': bool(f.get('include_assumption_values'))}
            pv = svc.analyses.projection_preview(db, p, rid, scope)
            exported = None
            if f.get('action') == 'export':
                exported = svc.analyses.export_projection(db, p, rid, scope, acknowledge_warnings=True)
            return render(request, 'report.html', **_report_ctx(db, p, rid, preview=pv, exported=exported, values=dict(f)))
        return await page(request, fn, mutating=True)

    @app.get('/console/notebooks', response_class=HTMLResponse)
    async def notebooks_page(request: Request):
        def fn(db, p):
            p.require('knowledge:read')
            nbs = [svc.notebooks.view(db, p, n['id']) for n in svc.notebooks.list(db, p)]
            return render(request, 'notebooks.html', principal=p, notebooks=nbs)
        return await page(request, fn)

    def _nb_blocks(f, existing=None):
        blocks = list(existing or [])
        n = len(blocks) + 1
        blocks.append({'id': 'b%d' % n, 'type': 'text', 'text': f.get('text', '')})
        link = (f.get('link') or '').strip()
        if link:
            kind, _, ref = link.partition(':')
            blocks.append({'id': 'l%d' % n, 'type': 'link', 'ref_kind': kind, 'ref_id': ref})
        return blocks

    @app.post('/console/notebooks', response_class=HTMLResponse)
    async def notebooks_create(request: Request):
        f = await form(request)
        def fn(db, p):
            svc.notebooks.create(db, p, f.get('name', ''), _nb_blocks(f), 'console')
            return RedirectResponse('/console/notebooks', status_code=303)
        return await page(request, fn, mutating=True)

    @app.post('/console/notebooks/{nid}/versions', response_class=HTMLResponse)
    async def notebooks_version(request: Request, nid: str):
        f = await form(request)
        def fn(db, p):
            cur = svc.notebooks.view(db, p, nid, check_links=False)
            svc.notebooks.add_version(db, p, nid, _nb_blocks(f, cur['blocks']), 'console')
            return RedirectResponse('/console/notebooks', status_code=303)
        return await page(request, fn, mutating=True)

    @app.get('/console/knowledge', response_class=HTMLResponse)
    async def knowledge_page(request: Request):
        def fn(db, p):
            p.require('knowledge:read')
            return render(request, 'knowledge.html', **knowledge_ctx(db, p))
        return await page(request, fn)

    @app.post('/console/knowledge/collections', response_class=HTMLResponse)
    async def knowledge_create(request: Request):
        f = await form(request)
        def fn(db, p):
            svc.knowledge.create_collection(db, p, f.get('name', ''), '')
            return RedirectResponse('/console/knowledge', status_code=303)
        return await page(request, fn, mutating=True)

    @app.post('/console/knowledge/collections/{cid}/documents', response_class=HTMLResponse)
    async def knowledge_add(request: Request, cid: str):
        f = await form(request)
        def fn(db, p):
            svc.knowledge.add_document(db, p, cid, name=f.get('name', ''), fmt=f.get('format', 'markdown'), content=(f.get('content') or '').encode('utf-8'), provenance='declared')
            return RedirectResponse('/console/knowledge', status_code=303)
        return await page(request, fn, mutating=True)

    @app.post('/console/knowledge/collections/{cid}/index', response_class=HTMLResponse)
    async def knowledge_index_form(request: Request, cid: str):
        await form(request)
        def fn(db, p):
            p.require('knowledge:write')
            from .api import quick_submit
            model_row = svc.models.resolve(db, 'embed', None)
            iid, versions, total = svc.knowledge.create_index(db, p, cid, model_row)
            out = quick_submit(svc, db, p, 'knowledge_index', {'schema': knowledge_engine.INDEX_SCHEMA, 'collection_id': cid, 'index_id': iid}, 'index build ' + iid)
            db.execute('UPDATE knowledge_indexes SET index_job_id=? WHERE id=?', (out['job_id'], iid))
            return RedirectResponse('/console/jobs/' + out['job_id'], status_code=303)
        return await page(request, fn, mutating=True)

    @app.post('/console/knowledge/collections/{cid}/search', response_class=HTMLResponse)
    async def knowledge_search_form(request: Request, cid: str):
        f = await form(request)
        def do():
            from .api import model_host_of
            with svc.db.tx() as db:
                p = session_principal(request, db)
                if p is None:
                    return RedirectResponse('/console/login?expired=1', status_code=303)
                try:
                    auth.check_csrf(p, f.get('csrf'))
                    p.require('knowledge:read')
                    index_row = svc.knowledge.latest_ready_index(db, cid)
                    mode = f.get('mode', 'hybrid')
                    rev = svc.models.row(db, index_row['model_revision_id']) if (index_row is not None and mode != 'lexical') else None
                except Exception as exc:
                    err = from_exception(exc); return render(request, 'error.html', status=err.status, principal=p, error=err.body())
            qv = None
            if rev is not None:
                host = model_host_of(svc)
                if host.available():
                    qv = host.embed(rev, [f.get('query', '')], truncate=True)['vectors'][0]
                else:
                    mode = 'lexical'
            with svc.db.tx() as db:
                p = session_principal(request, db)
                try:
                    results = retrieval_mod.search(db, svc.store, p, svc.knowledge, cid, f.get('query'), mode=mode, k=5, index_row=index_row, embed_fn=(lambda t: [qv]) if qv is not None else None)
                    history.record(db, p.workspace, p.id, 'knowledge.query', 'collection', cid, {'mode': results['mode'], 'results': len(results['results']), 'console': True})
                    return render(request, 'knowledge.html', **knowledge_ctx(db, p, results))
                except Exception as exc:
                    err = from_exception(exc); db.execute('ROLLBACK'); db.execute('BEGIN')
                    return render(request, 'error.html', status=err.status, principal=p, error=err.body())
        return await run_in_threadpool(do)

    @app.post('/console/knowledge/collections/{cid}/answer', response_class=HTMLResponse)
    async def knowledge_answer_form(request: Request, cid: str):
        f = await form(request)
        def fn(db, p):
            p.require('knowledge:read'); p.require('model:use')
            from .api import quick_submit
            inputs = {'schema': knowledge_engine.ANSWER_SCHEMA, 'collection_id': cid, 'question': f.get('query', ''), 'mode': f.get('mode_answer', 'extractive'), 'k': 4, 'max_output_tokens': 120}
            out = quick_submit(svc, db, p, 'knowledge_answer', inputs, 'answer')
            return RedirectResponse('/console/jobs/' + out['job_id'], status_code=303)
        return await page(request, fn, mutating=True)

    @app.post('/console/knowledge/documents/{did}/revoke', response_class=HTMLResponse)
    async def knowledge_revoke_form(request: Request, did: str):
        await form(request)
        def fn(db, p):
            svc.knowledge.revoke_document(db, p, did, 'console')
            return RedirectResponse('/console/knowledge', status_code=303)
        return await page(request, fn, mutating=True)

    @app.get('/console/knowledge/answers/{aid}', response_class=HTMLResponse)
    async def knowledge_answer_page(request: Request, aid: str):
        return await page(request, lambda db, p: render(request, 'knowledge_answer.html', principal=p, a=svc.knowledge.answer(db, p, aid, svc.store)))

    def calibration_ctx(db, p, **extra):
        ctx = dict(principal=p, datasets=svc.calibration.list_datasets(db, p), models=svc.calibration.list_models(db, p), scheduling={'calibrated_scheduling_enabled': svc.calibration.scheduling_enabled(db)},
                   kinds=[k for k in compute_manifests.KINDS if k != 'calibration_fit'], detail=None, prediction=None, comparison=None, design=None, values={})
        ctx.update(extra)
        return ctx

    @app.get('/console/calibration', response_class=HTMLResponse)
    async def calibration_page(request: Request):
        def fn(db, p):
            p.require('job:read')
            return render(request, 'calibration.html', **calibration_ctx(db, p))
        return await page(request, fn)

    @app.get('/console/calibration/models/{mid}', response_class=HTMLResponse)
    async def calibration_model_page(request: Request, mid: str):
        def fn(db, p):
            p.require('job:read')
            detail = svc.calibration.model_view(db, svc.calibration.model(db, p, mid), full=True)
            comparison = None
            if detail['scope'] and p.can('job:read_private'):
                try:
                    comparison = svc.calibration.comparison(db, p, mid)
                except ServiceError:
                    comparison = None
            return render(request, 'calibration.html', **calibration_ctx(db, p, detail=detail, comparison=comparison))
        return await page(request, fn)

    @app.post('/console/calibration/models/{mid}/{action}', response_class=HTMLResponse)
    async def calibration_action_form(request: Request, mid: str, action: str):
        f = await form(request)
        def fn(db, p):
            if action == 'predict':
                detail = svc.calibration.model_view(db, svc.calibration.model(db, p, mid), full=True)
                feats = {k: f.get('f_' + k, '') for k in detail['features']}
                pred = svc.calibration.predict(db, p, mid, feats)
                return render(request, 'calibration.html', **calibration_ctx(db, p, detail=detail, prediction=pred, values=dict(f)))
            if action == 'design':
                detail = svc.calibration.model_view(db, svc.calibration.model(db, p, mid), full=True)
                try:
                    cands = json.loads(f.get('candidates', ''))
                except ValueError:
                    raise ServiceError('VALIDATION', 'candidates: JSON list of {features, cost, label}')
                body = {'candidates': cands, 'objective': f.get('objective', 'reduce_overall_uncertainty'), 'cost_policy': {'rank_by': f.get('rank_by', 'utility_per_cost')}}
                if f.get('budget'):
                    body['cost_policy']['budget'] = f['budget']
                if f.get('targets'):
                    body['targets'] = json.loads(f['targets'])
                design = svc.calibration.design(db, p, mid, body)
                return render(request, 'calibration.html', **calibration_ctx(db, p, detail=detail, design=design, values=dict(f)))
            if action == 'approve':
                from .approvals import gate as approval_gate
                approval_gate(svc.approvals, db, p, 'calibration_approve')
                svc.calibration.approve(db, p, mid, {'source': 'console'})
            elif action == 'retire':
                svc.calibration.retire(db, p, mid)
            else:
                raise ServiceError('NOT_FOUND', 'action')
            return RedirectResponse('/console/calibration', status_code=303)
        return await page(request, fn, mutating=True)

    @app.post('/console/calibration/datasets', response_class=HTMLResponse)
    async def calibration_dataset_form(request: Request):
        f = await form(request)
        def fn(db, p):
            svc.calibration.create_performance_dataset(db, p, {'kind': 'performance', 'task_kind': f.get('task_kind')})
            return RedirectResponse('/console/calibration', status_code=303)
        return await page(request, fn, mutating=True)

    @app.post('/console/calibration/fits', response_class=HTMLResponse)
    async def calibration_fit_form(request: Request):
        f = await form(request)
        def fn(db, p):
            p.require('calibration:write')
            from .api import quick_submit
            inputs = {'schema': compute_inputs.CALIBRATION_SCHEMA, 'dataset_id': f.get('dataset_id'), 'features': ['work_units'], 'target': 'duration_ms', 'split': {'method': 'chronological', 'train_fraction_percent': 80}, 'device_policy': 'cpu'}
            if f.get('task_kind'):
                inputs['scope'] = {'task_kind': f['task_kind']}
            out = quick_submit(svc, db, p, 'calibration_fit', inputs, 'calibration fit')
            return RedirectResponse('/console/jobs/' + out['job_id'], status_code=303)
        return await page(request, fn, mutating=True)

    @app.get('/console/verification', response_class=HTMLResponse)
    async def verification_page(request: Request):
        def fn(db, p):
            p.require('job:read')
            return render(request, 'verification.html', principal=p, items=svc.verification.list(db, p), classes=list(verification_mod.CLASSES), preview=None, groups=[g for g in svc.disagreements.groups(db, p) if g['open']])
        return await page(request, fn)

    @app.post('/console/verification/{vid}/decide', response_class=HTMLResponse)
    async def verification_decide_form(request: Request, vid: str):
        f = await form(request)
        def fn(db, p):
            evidence = [{'kind': k, 'id': i} for k, i in (x.split(':', 1) for x in (f.get('evidence') or '').split() if ':' in x)]
            svc.disagreements.decide(db, p, vid, f.get('decision'), f.get('note', ''), evidence)
            return RedirectResponse('/console/verification', status_code=303)
        return await page(request, fn, mutating=True)

    @app.post('/console/verification', response_class=HTMLResponse)
    async def verification_form(request: Request):
        f = await form(request)
        def fn(db, p):
            params = {}
            if f.get('class') == 'sampled_reference' and (f.get('sample_count') or '').isdigit():
                params['sample_count'] = int(f['sample_count'])
            if f.get('preview'):
                pv = svc.verification.preview(db, p, f.get('job_id'), f.get('class'), params)
                return render(request, 'verification.html', principal=p, items=svc.verification.list(db, p), classes=list(verification_mod.CLASSES), preview=pv, groups=[])
            svc.verification.request(db, p, f.get('job_id'), f.get('class'), params)
            return RedirectResponse('/console/verification', status_code=303)
        return await page(request, fn, mutating=True)

    @app.post('/console/verification/{vid}/resolve', response_class=HTMLResponse)
    async def verification_resolve_form(request: Request, vid: str):
        f = await form(request)
        def fn(db, p):
            svc.verification.resolve(db, p, vid, f.get('decision'), f.get('note', ''))
            return RedirectResponse('/console/verification', status_code=303)
        return await page(request, fn, mutating=True)

    @app.get('/console/nodes', response_class=HTMLResponse)
    async def nodes_page(request: Request):
        def fn(db, p):
            p.require('job:read')
            return render(request, 'nodes.html', principal=p, nodes=svc.federation.list(db, p), workers=scheduling.workers(db, p)['items'], trust='authenticated coordination among enrolled nodes in this host\'s trust domain; not a permissionless network')
        return await page(request, fn)

    @app.post('/console/nodes/{nid}/{action}', response_class=HTMLResponse)
    async def nodes_action_form(request: Request, nid: str, action: str):
        await form(request)
        def fn(db, p):
            svc.federation.control(db, p, nid, action, 'console')
            return RedirectResponse('/console/nodes', status_code=303)
        return await page(request, fn, mutating=True)

    @app.get('/console/approvals', response_class=HTMLResponse)
    async def approvals_page(request: Request):
        def fn(db, p):
            p.require('job:read')
            from .approvals import ACTIONS
            return render(request, 'approvals.html', principal=p, items=svc.approvals.list(db, p), policy=dict(svc.approvals.policy(db, p.workspace), actions=list(ACTIONS)))
        return await page(request, fn)

    @app.post('/console/approvals/policy', response_class=HTMLResponse)
    async def approvals_policy_form(request: Request):
        f = await form(request)
        raw = await request.form()
        def fn(db, p):
            svc.approvals.set_policy(db, p, [v for v in raw.getlist('required')])
            return RedirectResponse('/console/approvals', status_code=303)
        return await page(request, fn, mutating=True)

    @app.post('/console/approvals/{pid}/{action}', response_class=HTMLResponse)
    async def approvals_action_form(request: Request, pid: str, action: str):
        await form(request)
        def fn(db, p):
            if action in ('approve', 'reject'):
                svc.approvals.decide(db, p, pid, 'approved' if action == 'approve' else 'rejected', 'console')
            elif action == 'apply':
                svc.approvals.apply(db, p, pid)
            else:
                raise ServiceError('NOT_FOUND', 'action')
            return RedirectResponse('/console/approvals', status_code=303)
        return await page(request, fn, mutating=True)

    @app.get('/console/statement', response_class=HTMLResponse)
    async def statement_page(request: Request):
        def fn(db, p):
            b = statements_mod.build(db, p, settings, 0, None, 1)
            return render(request, 'statement.html', principal=p, s=b['statement'])
        return await page(request, fn)

    @app.get('/console/history', response_class=HTMLResponse)
    async def history_page(request: Request):
        def fn(db, p):
            p.require('history:read')
            rows = db.execute('SELECT * FROM events WHERE workspace=? ORDER BY seq DESC LIMIT 100', (p.workspace,)).fetchall()
            return render(request, 'history.html', principal=p, events=[dict(r, ref=json.loads(r['ref_json'])) for r in rows],
                          chain=history.verify_chain(db, p.workspace))
        return await page(request, fn)
