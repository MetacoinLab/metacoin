"""Versioned HTTP API. Every handler authenticates a server-side principal, parses the
body with the strict canonical parser (bounded size, no duplicate keys, no floats),
and calls the narrow services; state machines live in the services, not here."""
import asyncio
import hashlib
import json
import time
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool
from experiments.private_receipts import receipt as merkle
from experiments.work_contracts import contract as terms, energy_analysis as energy, explanation
from . import actions as actions_mod, artifacts as artifacts_mod, auth, contracts as contracts_mod, crypto, history
from .compute import service as compute_svc, inputs as compute_inputs
from .models import service as model_svc, registry as model_registry
from .knowledge import service as knowledge_mod, retrieval as retrieval_mod, engine as knowledge_engine
from .calibration import Calibration
from .verification import Verification
from .federation.service import Federation
from .approvals import Approvals, gate as approval_gate
from . import statements as statements_mod, tracing
from .evaluation import Evaluation
from .notebooks import Notebooks
from .planner import Planner
from .intent import Intents
from .bundles import Bundles
from .documents.service import Documents
from .disagreements import Disagreements
from . import agents as agents_mod, budgets, campaigns as campaigns_mod, observability, reuse as reuse_mod, schedules as schedules_mod, scheduling, search as search_mod, sharing, catalog as catalog_mod, datasets as datasets_mod, metering, jobs as jobs_mod, reviews as reviews_mod, science, templates_svc, workflows as workflows_mod, x402_http
from .db import Database, now
from .errors import ServiceError, from_exception

API = '/api/v1'
SENSITIVE_HEADERS = {'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
                     'Referrer-Policy': 'no-referrer', 'Content-Security-Policy': "default-src 'self'; frame-ancestors 'none'"}


class Services:
    def __init__(self, settings):
        self.settings = settings
        self.db = Database(settings.db_path)
        self.store = artifacts_mod.ArtifactStore(settings)
        self.contracts = contracts_mod.Contracts(self.store, settings)
        self.jobs = jobs_mod.Jobs(self.store, settings)
        self.reviews = reviews_mod.Reviews(self.store, settings, self.jobs)
        self.actions = actions_mod.Actions(settings, self.jobs)
        self.sales = x402_http.SaleService(settings, self.store, self.jobs)
        self.templates = templates_svc.Templates(self.contracts)
        self.datasets = datasets_mod.Datasets(self.store, settings)
        self.workflows = workflows_mod.Workflows(self.contracts, self.jobs, self.reviews, self.datasets, self.store, settings)
        self.campaigns = campaigns_mod.Campaigns(self.contracts, self.jobs, self.datasets, self.store, settings)
        self.catalog = catalog_mod.Catalog(settings, self.contracts, self.jobs)
        self.agents = agents_mod.Agents(settings)
        self.schedules = schedules_mod.Schedules(self.workflows, settings)
        self.models = model_registry.ModelRegistry(settings)
        self.knowledge = knowledge_mod.Knowledge(self.store, settings)
        self.calibration = Calibration(self.store, settings)
        self.verification = Verification(self.store, settings, self.contracts, self.jobs)
        self.federation = Federation(self.db, self.store, settings)
        self.approvals = Approvals(settings, self)
        self.evaluation = Evaluation(settings, self)
        self.notebooks = Notebooks(settings, self)
        from .analyses import Analyses
        self.analyses = Analyses(settings, self)
        self.planner = Planner(settings, self)
        self.intents = Intents(settings, self)
        self.bundles = Bundles(settings, self)
        self.documents = Documents(settings, self)
        self.disagreements = Disagreements(settings, self)
        from .packages import Packages
        self.packages = Packages(settings, self)
        from .reconciliation import Reconciliations
        self.reconciliations = Reconciliations(settings, self)
        from .economy.facade import Economy
        self.economy = Economy(settings, self)
        self.sales.delivery_gate = self.packages.gate_for_job
        self._model_host = None
        with self.db.tx() as db:                       # installed services are registered idempotently at start
            self.catalog.populate(db)
            metering.ensure_service_key(settings, db)


def model_host_of(svc):
    """The API process's own runtime host (used for synchronous query embeddings); created lazily."""
    if svc._model_host is None:
        from .models.engine import ModelHost
        svc._model_host = ModelHost(svc.settings, svc.db, 'api')
    return svc._model_host


def preloaded_generation_host(svc):
    """The API host with the promoted generation model already resident, or None. Loading writes runtime records in its own
    transaction, so callers do this BEFORE opening a write transaction that will run a synchronous model step."""
    host = model_host_of(svc)
    if not host.available():
        return None
    with svc.db.read() as db:
        d = db.execute("SELECT revision_id FROM model_defaults WHERE operation='generate'").fetchone()
        row = svc.models.row(db, d['revision_id']) if d else None
    if row is None:
        return None
    try:
        host.ensure(row)
    except ServiceError:
        return None
    return host


QUICK_POLICY_FIELDS = ('execution_locations', 'required_verification', 'verification_policy_id')


def quick_submit(svc, db, principal, kind, inputs, title, policy=None):
    """Create, freeze and submit a contract of `kind` for the caller (reviewer = first workspace reviewer). Same rules as the console.
    `policy` may carry only the execution-location and verification requirements; everything else keeps the defaults."""
    reviewer = db.execute("SELECT id FROM principals WHERE workspace=? AND role='reviewer' AND revoked_at IS NULL ORDER BY created_at LIMIT 1", (principal.workspace,)).fetchone()
    if policy is not None and (type(policy) is not dict or set(policy) - set(QUICK_POLICY_FIELDS)):
        raise ServiceError('VALIDATION', {'code': 'policy', 'allowed': list(QUICK_POLICY_FIELDS)})
    cid = svc.contracts.create_draft(db, principal, kind=kind, title=title, inputs=inputs, policy=dict(policy or {}, reviewer_id=reviewer['id'] if reviewer else None), datasets=svc.datasets)
    svc.contracts.freeze(db, principal, cid)
    jid = svc.jobs.submit(db, principal, cid)
    return {'job_id': jid, 'contract_id': cid, 'kind': kind, 'state': 'queued'}


def read_body(request, raw):
    limit = request.app.state.services.settings.limits['max_body_bytes']
    if len(raw) > limit:
        raise ServiceError('PAYLOAD_TOO_LARGE')
    if not raw:
        return {}
    value = merkle.parse(raw)
    if type(value) is not dict:
        raise ServiceError('VALIDATION', 'body must be an object')
    return value


def principal_of(request, db, mutating):
    header = request.headers.get('authorization', '')
    if header.startswith('Bearer '):
        return auth.authenticate_bearer(db, header[7:].strip())
    cookie = request.cookies.get('metacoin_session')
    if cookie:
        principal = auth.authenticate_session(db, cookie)
        if mutating:
            auth.check_csrf(principal, request.headers.get('x-csrf-token'))
        return principal
    raise ServiceError('UNAUTHENTICATED')


def idempotent(db, principal, operation, key, raw, fn):
    """Persisted binding among principal, operation, key and request digest."""
    if key is None:
        return fn()
    if type(key) is not str or not 1 <= len(key) <= 128:
        raise ServiceError('VALIDATION', 'Idempotency-Key')
    digest = hashlib.sha256(raw).hexdigest()
    row = db.execute('SELECT * FROM idempotency WHERE principal_id=? AND operation=? AND key=?', (principal.id, operation, key)).fetchone()
    if row is not None:
        if row['request_digest'] != digest:
            raise ServiceError('IDEMPOTENCY_CONFLICT')
        return json.loads(row['response_json']), row['status']
    result = fn()
    status = result[1] if isinstance(result, tuple) else 200
    body = result[0] if isinstance(result, tuple) else result
    db.execute('INSERT INTO idempotency VALUES (?,?,?,?,?,?,?)', (principal.id, operation, key, digest, status, json.dumps(body), now()))
    return body, status


def create_app(settings):
    app = FastAPI(title='MetaCoin work service', version='0.1.0', docs_url='/api/docs', openapi_url='/api/openapi.json')
    app.state.services = Services(settings)
    svc = app.state.services

    @app.exception_handler(ServiceError)
    async def handle_service_error(request, exc):
        return JSONResponse(exc.body(), status_code=exc.status, headers=SENSITIVE_HEADERS)

    @app.middleware('http')
    async def headers(request, call_next):
        # Every other exception becomes a safe JSON refusal here (Starlette's generic
        # Exception handler would re-raise after responding, logging a trace and
        # closing the connection). No exception text reaches the client.
        try:
            response = await call_next(request)
        except ServiceError as exc:
            response = JSONResponse(exc.body(), status_code=exc.status)
        except Exception as exc:
            err = from_exception(exc)
            response = JSONResponse(err.body(), status_code=err.status)
        for key, value in SENSITIVE_HEADERS.items():
            response.headers.setdefault(key, value)
        return response

    def run_sync(request, mutating, fn, operation=None, raw=b''):
        with tracing.span('api.request', route=request.url.path.split('/api/v1/')[-1][:40], operation=operation or 'read'):
            with svc.db.tx() as db:
                principal = principal_of(request, db, mutating)
                key = request.headers.get('idempotency-key') if operation else None
                result = idempotent(db, principal, operation, key, raw, lambda: fn(db, principal)) if operation else fn(db, principal)
        if isinstance(result, tuple):
            return JSONResponse(result[0], status_code=result[1])
        return JSONResponse(result)

    async def run(request, mutating, fn, operation=None, raw=b''):
        # Service work is synchronous (SQLite, subprocesses, SDK); keep the event loop free.
        return await run_in_threadpool(run_sync, request, mutating, fn, operation, raw)

    # ---- health / capabilities ------------------------------------------------
    @app.get('/api/health')
    async def health():
        return await run_in_threadpool(health_sync)

    def health_sync():
        with svc.db.read() as db:
            queued = db.execute("SELECT COUNT(*) FROM jobs WHERE state='queued'").fetchone()[0]
            running = db.execute("SELECT COUNT(*) FROM jobs WHERE state='running'").fetchone()[0]
        return {'ok': True, 'revision': _revision(), 'provider_mode': settings.provider_mode, 'queued': queued, 'running': running}

    @app.get(API + '/capabilities')
    async def capabilities(request: Request):
        return await run(request, False, lambda db, p: capability_table(svc, db))

    # ---- session (browser) ---------------------------------------------------
    @app.post(API + '/session')
    async def login(request: Request):
        raw = await request.body()
        body = read_body(request, raw)
        def do():
            with svc.db.tx() as db:
                principal = auth.authenticate_bearer(db, body.get('token'))
                return principal, auth.create_session(db, principal.id, settings.limits['session_seconds'])
        principal, (sid, csrf) = await run_in_threadpool(do)
        response = JSONResponse({'principal_id': principal.id, 'role': principal.role, 'workspace': principal.workspace, 'csrf': csrf})
        response.set_cookie('metacoin_session', sid, httponly=True, samesite='strict', secure=not settings.dev_http_loopback,
                            max_age=settings.limits['session_seconds'], path='/')
        return response

    @app.delete(API + '/session')
    async def logout(request: Request):
        def do():
            with svc.db.tx() as db:
                principal = principal_of(request, db, True)
                if principal.session:
                    auth.end_session(db, principal.session['id'])
        await run_in_threadpool(do)
        response = JSONResponse({'ended': True})
        response.delete_cookie('metacoin_session', path='/')
        return response

    @app.get(API + '/me')
    async def me(request: Request):
        return await run(request, False, lambda db, p: {'principal_id': p.id, 'name': p.name, 'role': p.role, 'workspace': p.workspace,
                                                  'permissions': sorted(auth.PERMISSIONS[p.role])})

    # ---- contracts -----------------------------------------------------------
    @app.post(API + '/contracts', status_code=201)
    async def create_contract(request: Request):
        raw = await request.body()
        body = read_body(request, raw)
        def fn(db, p):
            cid = svc.contracts.create_draft(db, p, kind=body.get('kind'), title=body.get('title'), inputs=body.get('inputs'), policy=body.get('policy') or {},
                                             datasets=svc.datasets)
            return svc.contracts.public_view(svc.contracts.get(db, p, cid)), 201
        return await run(request, True, fn, 'contracts.create', raw)

    # ---- datasets ---------------------------------------------------------------
    @app.post(API + '/datasets', status_code=201)
    async def create_dataset(request: Request):
        raw = await request.body()
        if len(raw) > settings.limits['max_dataset_bytes'] + 4096:
            raise ServiceError('PAYLOAD_TOO_LARGE', 'dataset')
        body = merkle.parse(raw) if raw else {}
        if type(body) is not dict:
            raise ServiceError('VALIDATION', 'body must be an object')
        content = body.get('content')
        if type(content) is not str:
            raise ServiceError('VALIDATION', 'content must be a string (CSV text or JSON text)')
        def fn(db, p):
            return svc.datasets.create(db, p, name=body.get('name'), kind=body.get('kind'), fmt=body.get('format', 'csv'), content=content.encode('utf-8'),
                                       provenance=body.get('provenance', 'declared'), source=body.get('source', ''), license=body.get('license', ''),
                                       tags=body.get('tags'), dataset_id=body.get('dataset_id'), parent_version_id=body.get('parent_version_id')), 201
        return await run(request, True, fn, 'datasets.create', raw)

    @app.get(API + '/datasets')
    async def list_datasets(request: Request):
        q = request.query_params
        return await run(request, False, lambda db, p: {'items': svc.datasets.list(db, p, kind=q.get('kind'), tag=q.get('tag'), limit=q.get('limit', 50))})

    @app.get(API + '/datasets/{dataset_id}')
    async def dataset_detail(request: Request, dataset_id: str):
        return await run(request, False, lambda db, p: svc.datasets.detail(db, p, dataset_id))

    @app.post(API + '/datasets/{dataset_id}/retire')
    async def dataset_retire(request: Request, dataset_id: str):
        return await run(request, True, lambda db, p: svc.datasets.retire(db, p, dataset_id))

    @app.get(API + '/dataset-versions/{version_id}')
    async def dataset_version(request: Request, version_id: str):
        return await run(request, False, lambda db, p: svc.datasets.version_view(svc.datasets.version(db, p, version_id)))

    @app.get(API + '/dataset-versions/{version_id}/rows')
    async def dataset_rows(request: Request, version_id: str):
        def fn(db, p):
            rows, v = svc.datasets.rows(db, p, version_id)
            return {'version_id': version_id, 'private': True, 'columns': json.loads(v['columns_json']), 'rows': rows}
        return await run(request, False, fn)

    @app.delete(API + '/dataset-versions/{version_id}/payload')
    async def dataset_delete(request: Request, version_id: str):
        return await run(request, True, lambda db, p: svc.datasets.delete_payload(db, p, version_id))

    # ---- workflows ----------------------------------------------------------------
    @app.post(API + '/workflows/validate')
    async def workflow_validate(request: Request):
        raw = await request.body()
        body = read_body(request, raw)
        def fn(db, p):
            p.require('contract:read')
            digest, order = workflows_mod.validate_definition(body.get('definition'))
            return {'valid': True, 'digest': digest, 'order': order, 'estimate': workflows_mod.estimate(body['definition'], settings.limits)}
        return await run(request, False, fn)

    @app.post(API + '/workflows', status_code=201)
    async def workflow_create(request: Request):
        raw = await request.body()
        body = read_body(request, raw)
        def fn(db, p):
            wid, digest, created = svc.workflows.create(db, p, body.get('definition'))
            return svc.workflows.definition_view(svc.workflows.get_definition(db, p, wid)), (201 if created else 200)
        return await run(request, True, fn, 'workflows.create', raw)

    @app.get(API + '/workflows')
    async def workflow_list(request: Request):
        def fn(db, p):
            p.require('contract:read')
            rows = db.execute('SELECT id, name, version, digest, created_at FROM workflow_definitions WHERE workspace=? ORDER BY created_at DESC LIMIT 100', (p.workspace,)).fetchall()
            return {'items': [dict(r) for r in rows]}
        return await run(request, False, fn)

    @app.get(API + '/workflows/{wid}')
    async def workflow_get(request: Request, wid: str):
        return await run(request, False, lambda db, p: svc.workflows.definition_view(svc.workflows.get_definition(db, p, wid)))

    @app.post(API + '/workflows/{wid}/runs', status_code=202)
    async def workflow_run(request: Request, wid: str):
        raw = await request.body()
        body = read_body(request, raw)
        def fn(db, p):
            out = svc.workflows.start_run(db, p, wid, bindings=body.get('bindings'), budget_ceiling=body.get('budget_ceiling'), preview=bool(body.get('preview')))
            if out.get('preview'):
                return out, 200
            svc.workflows.advance(db, out['run_id'])
            return out, 202
        return await run(request, True, fn, 'workflows.run', raw)

    @app.post(API + '/workflows/{wid}/instantiate', status_code=201)
    async def workflow_instantiate(request: Request, wid: str):
        raw = await request.body()
        body = read_body(request, raw)
        def fn(db, p):
            out = svc.workflows.instantiate(db, p, wid, body.get('values'), name=body.get('name'))
            return out, (201 if out['created'] else 200)
        return await run(request, True, fn, 'workflows.instantiate', raw)

    # ---- scheduled local runs (§46 item 4) ---------------------------------------------------
    @app.post(API + '/schedules', status_code=201)
    async def schedule_create(request: Request):
        raw = await request.body()
        body = read_body(request, raw)
        return await run(request, True, lambda db, p: (svc.schedules.create(db, p, body), 201), 'schedules.create', raw)

    @app.get(API + '/schedules')
    async def schedule_list(request: Request):
        return await run(request, False, lambda db, p: svc.schedules.list(db, p))

    @app.get(API + '/schedules/{sid}')
    async def schedule_view(request: Request, sid: str):
        return await run(request, False, lambda db, p: svc.schedules.view(db, p, sid))

    @app.post(API + '/schedules/{sid}/{action}')
    async def schedule_control(request: Request, sid: str, action: str):
        return await run(request, True, lambda db, p: svc.schedules.control(db, p, sid, action))

    @app.get(API + '/runs')
    async def runs_list(request: Request):
        return await run(request, False, lambda db, p: {'items': svc.workflows.list(db, p, request.query_params.get('limit', 50))})

    @app.get(API + '/runs/{run_id}')
    async def run_get(request: Request, run_id: str):
        return await run(request, False, lambda db, p: svc.workflows.view(db, p, run_id))

    @app.post(API + '/runs/{run_id}/advance')
    async def run_advance(request: Request, run_id: str):
        def fn(db, p):
            p.require('job:read')
            svc.workflows._run(db, run_id, p.workspace)
            return svc.workflows.advance(db, run_id)
        return await run(request, True, fn)

    @app.post(API + '/runs/{run_id}/cancel')
    async def run_cancel(request: Request, run_id: str):
        return await run(request, True, lambda db, p: svc.workflows.cancel(db, p, run_id))

    # ---- campaigns ----------------------------------------------------------------
    @app.post(API + '/campaigns', status_code=201)
    async def campaign_create(request: Request):
        raw = await request.body()
        body = read_body(request, raw)
        def fn(db, p):
            if body.get('preview'):
                pv = svc.campaigns.preview(db, p, body.get('definition'))
                return {'preview': True, 'total_candidates': pv['total'], 'estimate': pv['estimate'], 'digest': pv['digest']}, 200
            return svc.campaigns.create(db, p, body.get('definition')), 201
        return await run(request, True, fn, 'campaigns.create', raw)

    @app.get(API + '/campaigns')
    async def campaign_list(request: Request):
        def fn(db, p):
            p.require('job:read')
            rows = db.execute('SELECT id, name, kind, state, total_candidates, created_at FROM sci_campaigns WHERE workspace=? ORDER BY created_at DESC LIMIT 100', (p.workspace,)).fetchall()
            return {'items': [dict(r) for r in rows]}
        return await run(request, False, fn)

    @app.get(API + '/campaigns/{campaign_id}')
    async def campaign_get(request: Request, campaign_id: str):
        return await run(request, False, lambda db, p: svc.campaigns.view(db, p, campaign_id))

    @app.post(API + '/campaigns/{campaign_id}/plan')
    async def campaign_plan(request: Request, campaign_id: str):
        raw = await request.body()
        body = read_body(request, raw)
        from .compute import planning
        return await run(request, False, lambda db, p: planning.plan(db, p, campaign_id, body.get('cost_cap_units')))

    @app.post(API + '/campaigns/{campaign_id}/branch', status_code=201)
    async def campaign_branch(request: Request, campaign_id: str):
        raw = await request.body()
        body = read_body(request, raw)
        return await run(request, True, lambda db, p: (svc.campaigns.branch(db, p, campaign_id, base_changes=body.get('base_changes'), axes=body.get('axes'),
                                                                            candidate_indexes=body.get('candidate_indexes'), name=body.get('name'), changes=body.get('changes'), expected_head=body.get('expected_head')), 201), 'campaigns.branch', raw)

    @app.get(API + '/campaigns/{campaign_id}/head')
    async def campaign_head(request: Request, campaign_id: str):
        def fn(db, p):
            p.require('job:read'); svc.campaigns._campaign(db, campaign_id, p.workspace)
            return {'campaign_id': campaign_id, 'head': svc.campaigns.lineage_head(db, campaign_id)}
        return await run(request, False, fn)

    @app.post(API + '/campaigns/{campaign_id}/{action}')
    async def campaign_control(request: Request, campaign_id: str, action: str):
        raw = await request.body()
        body = read_body(request, raw)
        def fn(db, p):
            if action in ('run', 'pause', 'resume', 'cancel'):
                return svc.campaigns.control(db, p, campaign_id, action)
            if action == 'tick':
                p.require('job:read'); svc.campaigns._campaign(db, campaign_id, p.workspace)
                return {'campaign_id': campaign_id, 'state': svc.campaigns.tick(db, campaign_id)}
            if action == 'pareto':
                return svc.campaigns.pareto(db, p, campaign_id, body.get('objectives'), tuple(body.get('require_outcomes', ['FEASIBLE'])))
            raise ServiceError('NOT_FOUND', 'action')
        return await run(request, True, fn)

    @app.get(API + '/campaigns/{campaign_a}/compare/{campaign_b}')
    async def campaign_compare(request: Request, campaign_a: str, campaign_b: str):
        return await run(request, False, lambda db, p: svc.campaigns.compare(db, p, campaign_a, campaign_b))

    @app.get(API + '/campaigns/{campaign_id}/results')
    async def campaign_results(request: Request, campaign_id: str):
        return await run(request, False, lambda db, p: svc.campaigns.results(db, p, campaign_id, request.query_params.get('limit', 500)))

    @app.get(API + '/campaigns/{campaign_id}/results.csv')
    async def campaign_results_csv(request: Request, campaign_id: str):
        def do():
            with svc.db.tx() as db:
                p = principal_of(request, db, False)
                return svc.campaigns.results_csv(db, p, campaign_id)
        text = await run_in_threadpool(do)
        return Response(text, media_type='text/csv', headers=dict(SENSITIVE_HEADERS, **{'Content-Disposition': 'attachment; filename="' + campaign_id + '.csv"'}))

    @app.get(API + '/campaigns/{campaign_id}/plot.svg')
    async def campaign_plot(request: Request, campaign_id: str):
        def do():
            with svc.db.tx() as db:
                p = principal_of(request, db, False)
                return svc.campaigns.plot_svg(db, p, campaign_id)
        return Response(await run_in_threadpool(do), media_type='image/svg+xml', headers=SENSITIVE_HEADERS)

    @app.post(API + '/jobs/{job_id}/refine')
    async def job_refine(request: Request, job_id: str):
        raw = await request.body()
        body = read_body(request, raw)
        return await run(request, True, lambda db, p: svc.campaigns.refine_job(db, p, job_id, body.get('refinements')))

    # ---- service catalog, quotes, usage ------------------------------------------------
    @app.get(API + '/services')
    async def services_list(request: Request):
        q = request.query_params
        return await run(request, False, lambda db, p: {'items': svc.catalog.list(db, p, model=q.get('model'), input_type=q.get('input_type'), price_unit=q.get('price_unit'),
                                                                                       privacy=q.get('privacy'), include_retired=q.get('include_retired') == '1')})

    @app.get(API + '/services/{sid}')
    async def service_get(request: Request, sid: str):
        return await run(request, False, lambda db, p: svc.catalog.view(svc.catalog._row(db, sid, p.workspace)))

    @app.get(API + '/services/{sid}/x402-discovery')
    async def service_discovery(request: Request, sid: str):
        base = str(request.base_url).rstrip('/')
        return await run(request, False, lambda db, p: svc.catalog.x402_discovery(svc.catalog._row(db, sid, p.workspace), base))

    @app.post(API + '/services', status_code=201)
    async def service_register(request: Request):
        raw = await request.body()
        body = read_body(request, raw)
        def fn(db, p):
            p.require('admin:keys')                        # trusted operator role only
            sid = svc.catalog.register(db, p.id, name=body.get('name'), kind=body.get('kind'), version=body.get('version', 1),
                                       price_per_unit=body.get('price_per_unit', 1), description=body.get('description', ''), workspace=p.workspace)
            return svc.catalog.view(svc.catalog._row(db, sid, p.workspace)), 201
        return await run(request, True, fn, 'services.register', raw)

    @app.post(API + '/services/{sid}/retire')
    async def service_retire(request: Request, sid: str):
        return await run(request, True, lambda db, p: svc.catalog.retire(db, p, sid))

    @app.post(API + '/services/{sid}/validate')
    async def service_validate(request: Request, sid: str):
        raw = await request.body()
        body = read_body(request, raw)
        def fn(db, p):
            row, digest = svc.catalog.validate_request(db, p, sid, body.get('inputs'))
            return {'valid': True, 'service_id': sid, 'revision': row['revision'], 'request_digest': digest}
        return await run(request, False, fn)

    @app.post(API + '/services/{sid}/quote', status_code=201)
    async def service_quote(request: Request, sid: str):
        raw = await request.body()
        body = read_body(request, raw)
        return await run(request, True, lambda db, p: (svc.catalog.quote(db, p, sid, body.get('inputs'), body.get('quantity_max'), body.get('provider_mode'), body.get('scheme', 'exact')), 201), 'services.quote', raw)

    @app.post(API + '/quotes/{qid}/accept')
    async def quote_accept(request: Request, qid: str):
        return await run(request, True, lambda db, p: svc.catalog.accept(db, p, qid))

    @app.get(API + '/quotes/{qid}')
    async def quote_get(request: Request, qid: str):
        return await run(request, False, lambda db, p: svc.catalog.quote_view(svc.catalog.get_quote(db, p, qid)))

    @app.post(API + '/services/{sid}/invoke', status_code=202)
    async def service_invoke(request: Request, sid: str):
        """Invocation under an accepted quote without x402 (simulation mode / zero-price); the x402 route binds payment."""
        raw = await request.body()
        body = read_body(request, raw)
        def fn(db, p):
            if settings.provider_mode != 'simulation':
                raise ServiceError('CAPABILITY_UNAVAILABLE', 'priced invocation requires the x402 invoke route in this provider mode')
            return invoke_under_quote(svc, db, p, sid, body.get('quote_id'), body.get('inputs')), 202
        return await run(request, True, fn, 'services.invoke', raw)

    # EXPERIMENTAL — private test chain only — not the protocol's money layer, which remains [SPEC] and zero-value by MIP-0001/0002; chain-agnostic law unchanged.
    @app.post(API + '/x402/services/{sid}/invoke')
    async def x402_invoke(request: Request, sid: str):
        raw = await request.body()
        base = str(request.base_url).rstrip('/')
        def do():
            with svc.db.tx() as db:
                p = principal_of(request, db, False)
                body = read_body(request, raw)
                quote = svc.catalog.get_quote(db, p, body.get('quote_id'))
                if quote['principal_id'] != p.id:
                    raise ServiceError('FORBIDDEN', 'quote belongs to another principal')
                metered = (quote['scheme'] if 'scheme' in quote.keys() else 'exact') == 'upto'
                if quote['state'] == 'consumed':
                    # re-delivery path: only a payment whose identifier already settled (or was authorized, for upto) can be answered
                    table = 'metered_settlements' if metered else "invoke_sales WHERE state='CONFIRMED' AND"
                    if db.execute('SELECT 1 FROM ' + ('metered_settlements WHERE' if metered else "invoke_sales WHERE state='CONFIRMED' AND") + ' quote_id=?', (quote['id'],)).fetchone() is None:
                        raise ServiceError('CONFLICT', 'quote consumed')
                elif quote['state'] != 'accepted':
                    raise ServiceError('CONFLICT', 'quote not accepted')
                handler = svc.sales.handle_invoke_upto if metered else svc.sales.handle_invoke
                status, headers, content = handler(db, request, raw, base, sid, quote, p, lambda: invoke_under_quote(svc, db, p, sid, quote['id'], body.get('inputs')))
                return status, headers, content
        status, headers, content = await run_in_threadpool(do)
        return Response(content=content, status_code=status, headers=dict(headers, **SENSITIVE_HEADERS), media_type='application/json')

    @app.get(API + '/x402/settlements')
    async def settlements_list(request: Request):
        def fn(db, p):
            p.require('budget:read')
            return {'items': [svc.sales.settlement_view(r) for r in db.execute('SELECT * FROM metered_settlements WHERE workspace=? ORDER BY created_at DESC LIMIT 100', (p.workspace,)).fetchall()],
                    'local_chain': svc.sales.local_chain().record if settings.provider_mode == 'test-http' and svc.sales.upto_available() else None}
        return await run(request, False, fn)

    @app.get(API + '/x402/settlements/{payment_id}')
    async def settlement_get(request: Request, payment_id: str):
        base = str(request.base_url).rstrip('/')
        def fn(db, p):
            p.require('budget:read')
            return svc.sales.settle_metered(db, p, payment_id, base)            # settles the measured amount once the job is terminal (idempotent); otherwise reports the state
        return await run(request, True, fn, 'x402.settle')

    @app.post(API + '/x402/settlements/{payment_id}/settle')
    async def settlement_post(request: Request, payment_id: str):
        base = str(request.base_url).rstrip('/')
        return await run(request, True, lambda db, p: svc.sales.settle_metered(db, p, payment_id, base), 'x402.settle')

    # ---- compute engine ------------------------------------------------------------------------
    @app.get(API + '/compute/capabilities')
    async def compute_capabilities(request: Request):
        def fn(db, p):
            p.require('contract:read')
            return compute_svc.capabilities(db, settings)
        return await run(request, False, fn)

    @app.get(API + '/compute/jobs/{job_id}')
    async def compute_job_view(request: Request, job_id: str):
        return await run(request, False, lambda db, p: compute_svc.view(db, p, svc.jobs, job_id))

    @app.post(API + '/compute/jobs/{job_id}/freeze-alternative', status_code=201)
    async def compute_freeze_alternative(request: Request, job_id: str):
        raw = await request.body()
        body = read_body(request, raw)
        def fn(db, p):
            if type(body.get('cost_ceiling')) is not int:
                raise ServiceError('VALIDATION', 'cost_ceiling: integer')
            return compute_svc.freeze_alternative(db, p, svc.jobs, svc.store, svc.workflows, job_id, body['cost_ceiling'], body.get('title')), 201
        return await run(request, True, fn, 'compute.freeze_alternative', raw)

    @app.post(API + '/compute/jobs/{job_id}/{action}')
    async def compute_job_control(request: Request, job_id: str, action: str):
        return await run(request, True, lambda db, p: compute_svc.control(db, p, svc.jobs, job_id, action), 'compute.control:' + action)

    @app.get(API + '/compute/jobs/{job_id}/outputs')
    async def compute_outputs(request: Request, job_id: str):
        return await run(request, False, lambda db, p: compute_svc.outputs(db, p, svc.jobs, svc.store, job_id))

    @app.get(API + '/compute/jobs/{job_id}/outputs/{name}')
    async def compute_output_file(request: Request, job_id: str, name: str):
        def do():
            with svc.db.tx() as db:
                p = principal_of(request, db, False)
                return compute_svc.outputs(db, p, svc.jobs, svc.store, job_id, name)
        data, media = await run_in_threadpool(do)
        return Response(content=data, media_type=media, headers=dict(SENSITIVE_HEADERS, **{'Content-Disposition': 'attachment; filename="' + name + '"', 'Cache-Control': 'private, no-store'}))

    @app.get(API + '/compute/jobs/{job_id}/checkpoints')
    async def compute_checkpoints(request: Request, job_id: str):
        return await run(request, False, lambda db, p: compute_svc.checkpoints(db, p, svc.jobs, job_id))

    @app.get(API + '/compute/jobs/{job_id}/log')
    async def compute_log(request: Request, job_id: str):
        return await run(request, False, lambda db, p: compute_svc.log_tail(db, p, svc.jobs, job_id))

    @app.get(API + '/compute/jobs/{job_id}/reproducibility')
    async def compute_repro(request: Request, job_id: str):
        return await run(request, False, lambda db, p: compute_svc.reproducibility(db, p, svc.jobs, job_id, settings))

    @app.get(API + '/compute/jobs/{job_id}/plot.svg')
    async def compute_plot(request: Request, job_id: str):
        def do():
            with svc.db.read() as db:
                p = principal_of(request, db, False)
                return compute_svc.heat_svg(db, p, svc.jobs, svc.store, job_id)
        text = await run_in_threadpool(do)
        return Response(content=text, media_type='image/svg+xml', headers=dict(SENSITIVE_HEADERS, **{'Cache-Control': 'private, no-store'}))

    @app.get(API + '/compute/jobs/{job_id}/plan')
    async def compute_plan(request: Request, job_id: str):
        def fn(db, p):
            job, plan = compute_svc.plan_json(db, p, svc.jobs, svc.store, job_id)
            return {'job_id': job_id, 'plan': plan}
        return await run(request, False, fn)

    @app.get(API + '/compute/jobs/{job_id}/plan.svg')
    async def compute_plan_svg(request: Request, job_id: str):
        alt = request.query_params.get('alternative')
        def do():
            with svc.db.read() as db:
                p = principal_of(request, db, False)
                return compute_svc.plan_svg(db, p, svc.jobs, svc.store, job_id, int(alt) if alt is not None and alt.lstrip('-').isdigit() else None)
        text = await run_in_threadpool(do)
        return Response(content=text, media_type='image/svg+xml', headers=dict(SENSITIVE_HEADERS, **{'Cache-Control': 'private, no-store'}))

    @app.post(API + '/compute/resource-plans', status_code=202)
    async def resource_plan_submit(request: Request):
        raw = await request.body()
        body = read_body(request, raw)
        def fn(db, p):
            p.require('contract:create')
            inputs = dict(body.get('inputs') or {}, schema=compute_inputs.RESOURCE_PLAN_SCHEMA)
            inputs.setdefault('device_policy', 'cpu')
            return quick_submit(svc, db, p, 'resource_plan', inputs, body.get('title') or 'robust resource plan', policy=body.get('policy')), 202
        return await run(request, True, fn, 'compute.resource_plan', raw)

    # ---- §46 extras: compatibility preview, PROV-JSON lineage export --------------------------
    @app.get(API + '/services/{sid}/compatibility')
    async def service_compatibility(request: Request, sid: str):
        q = request.query_params
        return await run(request, False, lambda db, p: catalog_mod.compatibility(db, p, svc.catalog, sid, dataset_version_id=q.get('dataset_version_id'), run_id=q.get('run_id'), node_id=q.get('node_id')))

    @app.get(API + '/lineage/{object_type}/{object_id}/prov.json')
    async def lineage_prov(request: Request, object_type: str, object_id: str):
        return await run(request, False, lambda db, p: datasets_mod.prov_export(db, p, object_type, object_id))

    # ---- observability, search, result table ---------------------------------------------
    @app.get(API + '/status')
    async def status_view(request: Request):
        def fn(db, p):
            p.require('history:read')
            out = observability.status(db, p.workspace)
            from .economy import ops as economy_ops
            out['work'] = economy_ops.counts(db, p.workspace); out['work_waiting_reasons'] = economy_ops.waiting_reasons(db, p.workspace)
            out['loaded_revision'] = _revision()
            return out
        return await run(request, False, fn)

    @app.get('/api/metrics')
    async def metrics(request: Request):
        def do():
            with svc.db.read() as db:
                p = principal_of(request, db, False)
                p.require('history:read')
                return observability.metrics_text(db)
        text = await run_in_threadpool(do)
        return Response(text, media_type='text/plain; version=0.0.4', headers=SENSITIVE_HEADERS)

    @app.get(API + '/search')
    async def search_view(request: Request):
        q = request.query_params
        return await run(request, False, lambda db, p: search_mod.search(db, p, type=q.get('type'), status=q.get('status'), creator=q.get('creator'), since=q.get('since'),
                                                                        until=q.get('until'), tag=q.get('tag'), model=q.get('model'), schema=q.get('schema'), limit=q.get('limit'), before=q.get('before')))

    @app.get(API + '/results.csv')
    async def results_csv(request: Request):
        q = request.query_params
        def do():
            with svc.db.read() as db:
                p = principal_of(request, db, False)
                return search_mod.results_csv(db, p, svc.jobs, state=q.get('state'), kind=q.get('kind'), since=q.get('since'), until=q.get('until'), limit=q.get('limit'))
        text = await run_in_threadpool(do)
        return Response(text, media_type='text/csv', headers=dict(SENSITIVE_HEADERS, **{'Content-Disposition': 'attachment; filename="results.csv"'}))

    # ---- result reuse and selective sharing ----------------------------------------------
    @app.get(API + '/reuse/lookup')
    async def reuse_lookup(request: Request):
        cid = request.query_params.get('contract_id')
        return await run(request, False, lambda db, p: reuse_mod.lookup(db, p, svc.contracts.get(db, p, cid)))

    @app.post(API + '/jobs/{job_id}/shares', status_code=201)
    async def share_grant(request: Request, job_id: str):
        raw = await request.body()
        body = read_body(request, raw)
        return await run(request, True, lambda db, p: (sharing.grant(db, p, job_id, body.get('grantee_id'), body.get('fields')), 201))

    @app.get(API + '/jobs/{job_id}/shares')
    async def share_list(request: Request, job_id: str):
        return await run(request, False, lambda db, p: sharing.list_shares(db, p, job_id))

    @app.delete(API + '/shares/{share_id}')
    async def share_revoke(request: Request, share_id: str):
        return await run(request, True, lambda db, p: sharing.revoke(db, p, share_id))

    @app.get(API + '/jobs/{job_id}/projection')
    async def share_projection(request: Request, job_id: str):
        return await run(request, False, lambda db, p: sharing.projection(db, p, settings, job_id))

    @app.post(API + '/projections/verify')
    async def share_verify(request: Request):
        raw = await request.body()
        body = read_body(request, raw)
        return await run(request, False, lambda db, p: sharing.verify_bundle(db, body.get('bundle')))

    # ---- events: cursor polling and server-sent events ---------------------------------
    def event_rows(workspace, cursor, limit=200):
        with svc.db.read() as db:
            rows = db.execute('SELECT seq, ts, actor_id, event_type, category, object_type, object_id, ref_json FROM events WHERE workspace=? AND seq>? ORDER BY seq LIMIT ?',
                              (workspace, cursor, limit)).fetchall()
        return [{'seq': r['seq'], 'ts': r['ts'], 'actor_id': r['actor_id'], 'event_type': r['event_type'], 'category': r['category'],
                 'object_type': r['object_type'], 'object_id': r['object_id'], 'ref': json.loads(r['ref_json'])} for r in rows]

    def parse_cursor(value):
        try:
            cur = int(value or 0)
        except ValueError:
            raise ServiceError('VALIDATION', 'cursor must be an integer event sequence number')
        if cur < 0:
            raise ServiceError('VALIDATION', 'cursor')
        return cur

    @app.get(API + '/events')
    async def events_poll(request: Request):
        q = request.query_params
        def fn(db, p):
            p.require('history:read')
            cur = parse_cursor(q.get('after'))
            types = {t for t in (q.get('types') or '').split(',') if t}
            rows = [r for r in event_rows(p.workspace, cur, min(int(q.get('limit') or 100), 500)) if not types or r['event_type'] in types]
            latest = db.execute('SELECT MAX(seq) FROM events WHERE workspace=?', (p.workspace,)).fetchone()[0] or 0
            return {'items': rows, 'cursor': rows[-1]['seq'] if rows else cur, 'latest': latest}
        return await run(request, False, fn)

    @app.get(API + '/events/stream')
    async def events_stream(request: Request):
        q = request.query_params
        cursor = parse_cursor(request.headers.get('last-event-id') or q.get('after'))
        types = {t for t in (q.get('types') or '').split(',') if t}
        max_seconds = max(1, min(int(q.get('max_seconds') or 300), 3600))
        def auth_sync():
            with svc.db.read() as db:
                p = principal_of(request, db, False)
                p.require('history:read')
                return p.workspace
        workspace = await run_in_threadpool(auth_sync)

        async def gen():
            cur, started, last_beat = cursor, time.time(), time.time()
            yield ': connected cursor=%d\n\n' % cur
            while time.time() - started < max_seconds:
                if await request.is_disconnected():
                    return
                rows = await run_in_threadpool(event_rows, workspace, cur)
                for r in rows:
                    cur = r['seq']
                    if types and r['event_type'] not in types:
                        continue
                    yield 'id: %d\nevent: %s\ndata: %s\n\n' % (r['seq'], r['event_type'], json.dumps(r, separators=(',', ':')))
                if not rows:
                    if time.time() - last_beat >= 15:
                        yield ': heartbeat\n\n'; last_beat = time.time()
                    await asyncio.sleep(0.25)
            yield 'event: end\ndata: %s\n\n' % json.dumps({'cursor': cur, 'reason': 'max_seconds reached; reconnect with Last-Event-ID'})
        return StreamingResponse(gen(), media_type='text/event-stream', headers=dict({'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'}, **SENSITIVE_HEADERS))

    # ---- scheduling: workers, queue, quotas ------------------------------------------
    @app.get(API + '/queue')
    async def queue_view(request: Request):
        return await run(request, False, lambda db, p: scheduling.queue(db, p))

    @app.get(API + '/workers')
    async def workers_list(request: Request):
        return await run(request, False, lambda db, p: scheduling.workers(db, p))

    @app.post(API + '/workers/{worker_id}/drain')
    async def worker_drain(request: Request, worker_id: str):
        return await run(request, True, lambda db, p: scheduling.set_worker_state(db, p, worker_id, 'draining'))

    @app.post(API + '/workers/{worker_id}/resume')
    async def worker_resume(request: Request, worker_id: str):
        return await run(request, True, lambda db, p: scheduling.set_worker_state(db, p, worker_id, 'active'))

    @app.get(API + '/quotas')
    async def quotas_list(request: Request):
        return await run(request, False, lambda db, p: scheduling.quotas(db, p))

    @app.put(API + '/quotas/{principal_id}')
    async def quotas_set(request: Request, principal_id: str):
        raw = await request.body()
        body = read_body(request, raw)
        return await run(request, True, lambda db, p: scheduling.set_quota(db, p, principal_id, body.get('max_queued'), body.get('max_per_minute')))

    # ---- hierarchical budgets ------------------------------------------------------
    @app.get(API + '/budgets/tree')
    async def budgets_tree(request: Request):
        return await run(request, False, lambda db, p: budgets.tree(db, p))

    @app.post(API + '/budgets/preview')
    async def budgets_preview(request: Request):
        raw = await request.body()
        body = read_body(request, raw)
        return await run(request, False, lambda db, p: budgets.preview(db, p, body.get('parent_run_id'), body.get('amounts')))

    @app.put(API + '/budgets/workspace')
    async def budgets_workspace(request: Request):
        raw = await request.body()
        body = read_body(request, raw)
        return await run(request, True, lambda db, p: budgets.set_workspace_ceiling(db, p, body.get('ceiling')))

    # ---- agent policy grants ------------------------------------------------------
    @app.post(API + '/agents/grants', status_code=201)
    async def agent_grant_issue(request: Request):
        raw = await request.body()
        body = read_body(request, raw)
        return await run(request, True, lambda db, p: (svc.agents.issue(db, p, body.get('policy')), 201))

    @app.get(API + '/agents/grants')
    async def agent_grant_list(request: Request):
        return await run(request, False, lambda db, p: svc.agents.list(db, p))

    @app.get(API + '/agents/grants/{gid}')
    async def agent_grant_view(request: Request, gid: str):
        return await run(request, False, lambda db, p: svc.agents.view(db, p, gid))

    @app.post(API + '/agents/grants/{gid}/stop')
    async def agent_grant_stop(request: Request, gid: str):
        return await run(request, True, lambda db, p: svc.agents.stop(db, p, gid, revoke=False))

    @app.post(API + '/agents/grants/{gid}/revoke')
    async def agent_grant_revoke(request: Request, gid: str):
        return await run(request, True, lambda db, p: svc.agents.stop(db, p, gid, revoke=True))

    @app.post(API + '/agents/grants/{gid}/simulate')
    async def agent_grant_simulate(request: Request, gid: str):
        raw = await request.body()
        body = read_body(request, raw)
        ops = body.get('operations')
        if type(ops) is not list:
            raise ServiceError('VALIDATION', 'operations list')
        return await run(request, False, lambda db, p: svc.agents.simulate(db, p, gid, ops))

    @app.get(API + '/usage')
    async def usage_list(request: Request):
        def fn(db, p):
            p.require('budget:read')
            rows = db.execute('SELECT * FROM usage_records WHERE workspace=? ORDER BY created_at DESC LIMIT 100', (p.workspace,)).fetchall()
            return {'items': [metering.view(db, r) for r in rows]}
        return await run(request, False, fn)

    @app.get(API + '/usage/{uid}')
    async def usage_get(request: Request, uid: str):
        def fn(db, p):
            p.require('budget:read')
            row = db.execute('SELECT * FROM usage_records WHERE id=? AND workspace=?', (uid, p.workspace)).fetchone()
            if row is None:
                raise ServiceError('NOT_FOUND', 'usage record')
            return metering.view(db, row)
        return await run(request, False, fn)

    @app.get(API + '/lineage/{object_type}/{object_id}')
    async def lineage_query(request: Request, object_type: str, object_id: str):
        q = request.query_params
        def fn(db, p):
            p.require('history:read')
            if object_type not in ('dataset_version', 'contract', 'job', 'artifact', 'review', 'workflow_run', 'workflow_definition', 'campaign', 'quote', 'usage', 'service'):
                raise ServiceError('VALIDATION', 'object_type')
            return datasets_mod.lineage(db, p, object_type, object_id, depth=int(q.get('depth', 4)), limit=min(int(q.get('limit', 200)), 500))
        return await run(request, False, fn)

    @app.get(API + '/contracts')
    async def list_contracts(request: Request):
        def fn(db, p):
            p.require('contract:read')
            rows = db.execute('SELECT * FROM contracts WHERE workspace=? ORDER BY created_at DESC LIMIT ?', (p.workspace, settings.limits['page_size'])).fetchall()
            return {'items': [svc.contracts.public_view(r) for r in rows]}
        return await run(request, False, fn)

    @app.get(API + '/contracts/{contract_id}')
    async def get_contract(request: Request, contract_id: str):
        return await run(request, False, lambda db, p: svc.contracts.public_view(svc.contracts.get(db, p, contract_id)))

    @app.get(API + '/contracts/{contract_id}/inputs')
    async def contract_inputs(request: Request, contract_id: str):
        def fn(db, p):
            p.require('job:read_private')
            row = svc.contracts.get(db, p, contract_id)
            data = svc.store.load_json(db, row['input_artifact_id'], p.workspace)
            return {'contract_id': contract_id, 'private': True, 'inputs': data if row['state'] == 'draft' else {f['name']: f['value'] for f in data['fields']}['inputs']}
        return await run(request, False, fn)

    @app.patch(API + '/contracts/{contract_id}')
    async def update_contract(request: Request, contract_id: str):
        raw = await request.body()
        body = read_body(request, raw)
        def fn(db, p):
            svc.contracts.update_draft(db, p, contract_id, inputs=body.get('inputs'), policy=body.get('policy'), title=body.get('title'))
            return svc.contracts.public_view(svc.contracts.get(db, p, contract_id))
        return await run(request, True, fn)

    @app.post(API + '/contracts/{contract_id}/freeze')
    async def freeze(request: Request, contract_id: str):
        raw = await request.body()
        def fn(db, p):
            svc.contracts.freeze(db, p, contract_id)
            return svc.contracts.public_view(svc.contracts.get(db, p, contract_id))
        return await run(request, True, fn, 'contracts.freeze', raw)

    @app.post(API + '/contracts/{contract_id}/amend', status_code=201)
    async def amend(request: Request, contract_id: str):
        raw = await request.body()
        body = read_body(request, raw)
        def fn(db, p):
            new_id = svc.contracts.amend(db, p, contract_id, inputs=body.get('inputs'), policy=body.get('policy'), title=body.get('title'))
            return svc.contracts.public_view(svc.contracts.get(db, p, new_id)), 201
        return await run(request, True, fn, 'contracts.amend', raw)

    # ---- scoped automation credentials -----------------------------------------
    @app.post(API + '/credentials', status_code=201)
    async def issue_credential(request: Request):
        raw = await request.body()
        body = read_body(request, raw)
        def fn(db, p):
            cid, token = auth.issue_scoped_credential(db, p, body.get('operations'), body.get('expires_in_seconds', 86400))
            history.record(db, p.workspace, p.id, 'credential.issued', 'principal', p.id, {'credential_id': cid, 'scoped': True})
            return {'credential_id': cid, 'token': token, 'scope': {'operations': sorted(set(body['operations'])), 'workspace': p.workspace},
                    'note': 'shown once; store privately; never place in URLs or logs'}, 201
        return await run(request, True, fn)

    @app.delete(API + '/credentials/{credential_id}')
    async def revoke_credential(request: Request, credential_id: str):
        def fn(db, p):
            p.require('admin:credentials')
            row = db.execute('SELECT principal_id FROM credentials WHERE id=?', (credential_id,)).fetchone()
            if row is None or row['principal_id'] != p.id:
                raise ServiceError('NOT_FOUND', 'credential')
            auth.revoke_credential(db, credential_id)
            history.record(db, p.workspace, p.id, 'credential.revoked', 'principal', p.id, {'credential_id': credential_id})
            return {'revoked': credential_id}
        return await run(request, True, fn)

    # ---- comparison of two completed runs -----------------------------------------
    @app.get(API + '/jobs/{job_a}/compare/{job_b}')
    async def compare_runs(request: Request, job_a: str, job_b: str):
        return await run(request, False, lambda db, p: compare_jobs(svc, db, p, job_a, job_b))

    # ---- templates (non-secret parameters) ------------------------------------
    @app.post(API + '/templates', status_code=201)
    async def create_template(request: Request):
        raw = await request.body()
        body = read_body(request, raw)
        def fn(db, p):
            tid = svc.templates.save(db, p, name=body.get('name'), kind=body.get('kind'), policy=body.get('policy') or {}, notes=body.get('notes') or '')
            return svc.templates.view(svc.templates.get(db, p, tid)), 201
        return await run(request, True, fn, 'templates.create', raw)

    @app.get(API + '/templates')
    async def list_templates(request: Request):
        return await run(request, False, lambda db, p: {'items': svc.templates.list(db, p)})

    @app.post(API + '/templates/{template_id}/instantiate', status_code=201)
    async def instantiate_template(request: Request, template_id: str):
        raw = await request.body()
        body = read_body(request, raw)
        def fn(db, p):
            cid = svc.templates.instantiate(db, p, template_id, inputs=body.get('inputs'), title=body.get('title'), policy_overrides=body.get('policy'))
            return svc.contracts.public_view(svc.contracts.get(db, p, cid)), 201
        return await run(request, True, fn, 'templates.instantiate', raw)

    @app.get(API + '/templates/{template_id}/runs')
    async def template_runs(request: Request, template_id: str):
        return await run(request, False, lambda db, p: svc.templates.runs(db, p, template_id))

    # ---- jobs ----------------------------------------------------------------
    @app.post(API + '/jobs', status_code=202)
    async def submit_job(request: Request):
        raw = await request.body()
        body = read_body(request, raw)
        def fn(db, p):
            jid = svc.jobs.submit(db, p, body.get('contract_id'), reuse=bool(body.get('reuse')))
            return svc.jobs.view(db, p, svc.jobs.get(db, p, jid)), 202
        return await run(request, True, fn, 'jobs.submit', raw)

    @app.post(API + '/jobs/batch', status_code=202)
    async def submit_batch(request: Request):
        raw = await request.body()
        body = read_body(request, raw)
        def fn(db, p):
            out = svc.jobs.submit_batch(db, p, body.get('contract_ids'))
            return out, (202 if out['submitted'] else 409)
        return await run(request, True, fn, 'jobs.batch', raw)

    @app.get(API + '/batches/{batch_id}')
    async def batch_progress(request: Request, batch_id: str):
        return await run(request, False, lambda db, p: svc.jobs.batch_progress(db, p, batch_id))

    @app.post(API + '/jobs/quick', status_code=202)
    async def quick_job(request: Request):
        """Create, freeze and submit in one authorized step (same rules as the three separate calls; reviewer = first workspace reviewer)."""
        raw = await request.body()
        body = read_body(request, raw)
        def fn(db, p):
            kind = body.get('kind')
            if kind not in contracts_mod.KINDS or kind in ('verification_audit', 'knowledge_index'):
                raise ServiceError('VALIDATION', {'code': 'kind', 'allowed': [k for k in contracts_mod.KINDS if k not in ('verification_audit', 'knowledge_index')]})
            title = body.get('title') or (kind + ' job')
            return quick_submit(svc, db, p, kind, body.get('inputs'), title, policy=body.get('policy')), 202
        return await run(request, True, fn, 'jobs.quick', raw)

    @app.get(API + '/jobs')
    async def list_jobs(request: Request):
        q = request.query_params
        def fn(db, p):
            rows, more = svc.jobs.list(db, p, state=q.get('state'), review_state=q.get('review_state'), limit=q.get('limit'), before=q.get('before'))
            return {'items': [svc.jobs.view(db, p, r) for r in rows], 'more': more,
                    'next_before': rows[-1]['created_at'] if rows and more else None}
        return await run(request, False, fn)

    @app.get(API + '/jobs/{job_id}')
    async def get_job(request: Request, job_id: str):
        return await run(request, False, lambda db, p: svc.jobs.view(db, p, svc.jobs.get(db, p, job_id)))

    @app.post(API + '/jobs/{job_id}/cancel')
    async def cancel_job(request: Request, job_id: str):
        return await run(request, True, lambda db, p: {'job_id': job_id, 'result': svc.jobs.cancel(db, p, job_id)})

    @app.get(API + '/jobs/{job_id}/result')
    async def job_result(request: Request, job_id: str):
        def fn(db, p):
            row = svc.jobs.get(db, p, job_id)
            contract = db.execute('SELECT * FROM contracts WHERE id=?', (row['contract_id'],)).fetchone()
            private_ok = p.can('job:read_private') or (p.role == 'reviewer' and contract['reviewer_id'] == p.id)
            if not private_ok:
                raise ServiceError('FORBIDDEN', 'private result')
            if row['state'] != 'succeeded':
                raise ServiceError('CONFLICT', 'no committed result')
            vault = svc.store.load_json(db, row['evidence_artifact_id'], p.workspace)
            values = {f['name']: f['value'] for f in vault['fields']}
            return {'job_id': job_id, 'private': True, 'evidence_root': row['evidence_root'], 'kind': row['kind'],
                    'result': values.get('audit_details') or values.get('result'),
                    'margin_explanation': values.get('margin_explanation'), 'outcome': values.get('outcome') or row['outcome'],
                    'bindings': {k: values.get(k) for k in ('contract_digest', 'input_root', 'verifier_id', 'verifier_digest', 'model_id')},
                    'limits': 'conditional on the declared interval model; bounds are assumptions, not calibrated observations'}
        return await run(request, False, fn)

    @app.get(API + '/jobs/{job_id}/history')
    async def job_history(request: Request, job_id: str):
        def fn(db, p):
            p.require('history:read')
            svc.jobs.get(db, p, job_id)
            return {'job_id': job_id, 'events': history.for_object(db, p.workspace, 'job', job_id)}
        return await run(request, False, fn)

    # ---- reviews -------------------------------------------------------------
    @app.post(API + '/jobs/{job_id}/review-request')
    async def review_request(request: Request, job_id: str):
        return await run(request, True, lambda db, p: {'job_id': job_id, 'review_state': 'requested', 'reviewer_id': svc.reviews.request(db, p, job_id)})

    @app.get(API + '/reviews/{job_id}/evidence')
    async def review_evidence(request: Request, job_id: str):
        return await run(request, False, lambda db, p: svc.reviews.evidence(db, p, job_id))

    @app.post(API + '/reviews/{job_id}/decision')
    async def review_decide(request: Request, job_id: str):
        raw = await request.body()
        body = read_body(request, raw)
        return await run(request, True, lambda db, p: svc.reviews.decide(db, p, job_id, body.get('decision')), 'reviews.decide', raw)

    @app.get(API + '/reviews/{job_id}')
    async def review_get(request: Request, job_id: str):
        def fn(db, p):
            svc.jobs.get(db, p, job_id)
            row = db.execute('SELECT * FROM reviews WHERE job_id=?', (job_id,)).fetchone()
            if row is None:
                raise ServiceError('NOT_FOUND', 'review')
            out = svc.reviews.view(row)
            out['verification'] = svc.reviews.verify(db, out['envelope'], out['signature_hex'], expected={'job_id': job_id})
            return out
        return await run(request, False, fn)

    @app.post(API + '/reviews/verify')
    async def review_verify(request: Request):
        raw = await request.body()
        body = read_body(request, raw)
        if len(raw) > settings.limits['max_envelope_bytes']:
            raise ServiceError('PAYLOAD_TOO_LARGE')
        return await run(request, False, lambda db, p: svc.reviews.verify(db, body.get('envelope'), body.get('signature_hex'), body.get('expected')))

    # ---- artifacts ---------------------------------------------------------------
    @app.get(API + '/jobs/{job_id}/artifacts')
    async def artifacts_list(request: Request, job_id: str):
        def fn(db, p):
            svc.jobs.get(db, p, job_id)
            rows = db.execute('SELECT * FROM artifacts WHERE job_id=? OR contract_id=(SELECT contract_id FROM jobs WHERE id=?) ORDER BY created_at', (job_id, job_id)).fetchall()
            return {'items': [artifact_view(r, p, db) for r in rows if r['public'] or p.can('artifact:read_private')
                              or (p.role == 'reviewer' and _assigned(db, r, p))]}
        return await run(request, False, fn)

    @app.get(API + '/artifacts/{artifact_id}/export')
    async def artifact_export(request: Request, artifact_id: str):
        def do():
          with svc.db.tx() as db:
            p = principal_of(request, db, False)
            row = svc.store.row(db, artifact_id, p.workspace)
            if row['public']:
                p.require('artifact:read_public') if p.role == 'viewer' else None
                data = svc.store.load(db, artifact_id, p.workspace)
                media = 'application/json'
            else:
                if not (p.can('artifact:export') or (p.role == 'reviewer' and _assigned(db, row, p))):
                    raise ServiceError('FORBIDDEN', 'private artifact')
                row, data = svc.store.ciphertext(db, artifact_id, p.workspace)   # ciphertext only; recipients decrypt with their own identity
                media = 'application/octet-stream'
            history.record(db, p.workspace, p.id, 'artifact.exported', 'artifact', artifact_id, {'public': bool(row['public']), 'kind': row['kind']})
            return data, media
        data, media = await run_in_threadpool(do)
        return Response(content=data, media_type=media, headers=dict(SENSITIVE_HEADERS, **{'Content-Disposition': 'attachment; filename="' + artifact_id + ('.json' if media == 'application/json' else '.age') + '"'}))

    @app.delete(API + '/artifacts/{artifact_id}')
    async def artifact_delete(request: Request, artifact_id: str):
        def fn(db, p):
            p.require('artifact:delete')
            removed = svc.store.delete_payload(db, artifact_id, p.workspace, p.id)
            history.record(db, p.workspace, p.id, 'artifact.deleted', 'artifact', artifact_id, {'payload_unlinked': removed})
            return {'artifact_id': artifact_id, 'payload_unlinked': removed,
                    'note': 'application reference and ciphertext object removed; not cryptographic erasure of every copy'}
        return await run(request, True, fn)

    # ---- actions / budget ------------------------------------------------------
    @app.post(API + '/actions')
    async def create_action(request: Request):
        raw = await request.body()
        body = read_body(request, raw)
        return await run(request, True, lambda db, p: svc.actions.create(db, p, body.get('job_id'), body.get('request_id'), body.get('provider_mode'),
                                                                     dry_run=bool(body.get('dry_run'))), 'actions.create', raw)

    @app.post(API + '/actions/{job_id}/reconcile')
    async def reconcile_action(request: Request, job_id: str):
        return await run(request, True, lambda db, p: svc.actions.reconcile(db, p, job_id))

    @app.get(API + '/budget')
    async def budget(request: Request):
        return await run(request, False, lambda db, p: svc.actions.budget(db, p))

    @app.post(API + '/sales/{job_id}/reconcile')
    async def reconcile_sale(request: Request, job_id: str):
        return await run(request, True, lambda db, p: {k: v for k, v in svc.sales.reconcile(db, p, job_id).items() if k != 'requirements_digest'})

    @app.get(API + '/history')
    async def workspace_history(request: Request):
        def fn(db, p):
            p.require('history:read')
            rows = db.execute('SELECT seq, ts, actor_id, event_type, category, object_type, object_id, ref_json FROM events WHERE workspace=? ORDER BY seq DESC LIMIT ?',
                              (p.workspace, settings.limits['page_size'])).fetchall()
            return {'events': [dict(r, ref=json.loads(r['ref_json'])) for r in rows], 'chain': history.verify_chain(db, p.workspace)}
        return await run(request, False, fn)

    # ---- x402 sale route (real HTTP 402) ------------------------------------------
    @app.get(API + '/x402/jobs/{job_id}/public-bundle')
    async def x402_public_bundle(request: Request, job_id: str):
        body = await request.body()
        base = str(request.base_url).rstrip('/')
        def do():
            with svc.db.tx() as db:
                return svc.sales.handle(db, request, body, base, job_id, _workspace_of_job(db, job_id))
        status, headers, content = await run_in_threadpool(do)
        return Response(content=content, status_code=status, headers=dict(headers, **SENSITIVE_HEADERS), media_type='application/json')

    if settings.provider_mode == 'test-http':
        @app.get('/facilitator-double/supported')
        async def fd_supported():
            return svc.sales.double.supported()

        @app.post('/facilitator-double/verify')
        async def fd_verify(request: Request):
            return svc.sales.double.verify(json.loads(await request.body()))

        @app.post('/facilitator-double/settle')
        async def fd_settle(request: Request):
            return svc.sales.double.settle(json.loads(await request.body()))

    # ---- local models (registry, runtime facts, generation/embedding jobs, durable segments) ---------------------
    @app.get(API + '/models')
    async def models_list(request: Request):
        return await run(request, False, lambda db, p: {'items': svc.models.list(db, p, include_retired=request.query_params.get('include_retired', '1') == '1')})

    @app.post(API + '/models', status_code=201)
    async def models_register(request: Request):
        raw = await request.body()
        body = read_body(request, raw)
        return await run(request, True, lambda db, p: (svc.models.register(db, p, body), 201), 'models.register', raw)

    @app.get(API + '/models/runtime')
    async def models_runtime(request: Request):
        def fn(db, p):
            p.require('job:read')
            return model_svc.runtime_facts(db, settings, api_host=svc._model_host)
        return await run(request, False, fn)

    @app.post(API + '/models/batching')
    async def models_batching(request: Request):
        raw = await request.body()
        body = read_body(request, raw)
        return await run(request, True, lambda db, p: model_svc.set_batching(db, p, settings, body), 'models.batching', raw)

    @app.post(API + '/models/warmup')
    async def models_warmup(request: Request):
        raw = await request.body()
        body = read_body(request, raw)
        return await run(request, True, lambda db, p: model_svc.set_warmup(db, p, settings, body), 'models.warmup', raw)

    @app.get(API + '/models/promotions')
    async def models_promotions(request: Request):
        return await run(request, False, lambda db, p: {'items': svc.models.promotions(db, p)})

    @app.post(API + '/models/generate', status_code=202)
    async def models_generate(request: Request):
        raw = await request.body()
        body = read_body(request, raw)
        def fn(db, p):
            p.require('model:use')
            inputs = dict(body.get('inputs') or {}, schema=model_svc.GENERATION_SCHEMA)
            decision = None
            if body.get('route'):
                from .models import routing
                rq = body['route'] if type(body['route']) is dict else {}
                decision = routing.route(db, p, svc.models, 'generate', rq.get('category'), rq.get('budget'))
                if decision['chosen'] is None:
                    raise ServiceError('CAPABILITY_UNAVAILABLE', {'code': 'no_model_within_budget', 'routing': decision['basis']})
                inputs['model_revision_id'] = decision['chosen']['revision_id']
            out = quick_submit(svc, db, p, 'text_generation', inputs, body.get('title') or 'text generation')
            if decision:
                out['routing'] = {'chosen': decision['chosen']['revision_id'], 'model_id': decision['chosen']['model_id'], 'basis': decision['basis'], 'category': decision['category']}
            return out, 202
        return await run(request, True, fn, 'models.generate', raw)

    @app.get(API + '/models/route')
    async def models_route(request: Request):
        q = request.query_params
        def fn(db, p):
            from .models import routing
            budget = {}
            if q.get('max_resource_bytes', '').isdigit():
                budget['max_resource_bytes'] = int(q['max_resource_bytes'])
            if q.get('max_ms_per_unit'):
                try:
                    budget['max_ms_per_unit'] = float(q['max_ms_per_unit'])
                except ValueError:
                    raise ServiceError('VALIDATION', 'max_ms_per_unit')
            return routing.route(db, p, svc.models, q.get('operation', 'generate'), q.get('category'), budget)
        return await run(request, False, fn)

    @app.post(API + '/models/embed', status_code=202)
    async def models_embed(request: Request):
        raw = await request.body()
        body = read_body(request, raw)
        def fn(db, p):
            p.require('model:use')
            inputs = dict(body.get('inputs') or {}, schema=model_svc.EMBEDDING_SCHEMA)
            return quick_submit(svc, db, p, 'text_embedding', inputs, body.get('title') or 'text embedding'), 202
        return await run(request, True, fn, 'models.embed', raw)

    @app.get(API + '/models/jobs/{job_id}')
    async def models_job(request: Request, job_id: str):
        return await run(request, False, lambda db, p: model_svc.view(db, p, svc.jobs, job_id))

    @app.get(API + '/models/jobs/{job_id}/segments')
    async def models_segments(request: Request, job_id: str):
        q = request.query_params
        try:
            after, wait = int(q.get('after', '-1')), min(int(q.get('wait', '0')), 20)
        except ValueError:
            raise ServiceError('VALIDATION', 'after/wait')
        def poll():
            deadline = time.time() + wait
            while True:
                with svc.db.tx() as db:
                    p = principal_of(request, db, False)
                    out = model_svc.segments(db, p, svc.jobs, job_id, after=after)
                if out['segments'] or out['done'] or time.time() >= deadline:
                    return out
                time.sleep(0.2)
        return JSONResponse(await run_in_threadpool(poll))

    @app.get(API + '/models/jobs/{job_id}/outputs')
    async def models_outputs(request: Request, job_id: str):
        return await run(request, False, lambda db, p: model_svc.output(db, p, svc.jobs, svc.store, job_id))

    @app.get(API + '/models/jobs/{job_id}/outputs/{name}')
    async def models_output_file(request: Request, job_id: str, name: str):
        def fn():
            with svc.db.tx() as db:
                p = principal_of(request, db, False)
                return model_svc.output(db, p, svc.jobs, svc.store, job_id, name)
        name, data = await run_in_threadpool(fn)
        return Response(content=data, media_type='application/json' if name.endswith('.json') else 'application/octet-stream', headers=SENSITIVE_HEADERS)

    @app.get(API + '/models/{rid}')
    async def models_detail(request: Request, rid: str):
        return await run(request, False, lambda db, p: svc.models.detail(db, p, rid))

    @app.post(API + '/models/{rid}/{action}')
    async def models_action(request: Request, rid: str, action: str):
        raw = await request.body()
        body = read_body(request, raw)
        def fn(db, p):
            if action == 'recheck':
                return svc.models.recheck_install(db, p, rid)
            if action == 'promote':
                approval_gate(svc.approvals, db, p, 'model_promote')
                eg = svc.evaluation.gate(db, p.workspace, rid)
                if eg:
                    raise ServiceError('CONFLICT', eg)
                return svc.models.promote(db, p, rid, body.get('operation'), body.get('evidence'))
            if action == 'rollback-default':
                return svc.models.rollback_default(db, p, body.get('operation'))
            if action in ('retire', 'revoke'):
                return svc.models.retire(db, p, rid, body.get('reason', ''), revoke=(action == 'revoke'))
            if action in ('load', 'unload'):
                return model_svc.request_load(db, p, settings, rid, 'loaded' if action == 'load' else 'unloaded')
            raise ServiceError('NOT_FOUND', 'action')
        return await run(request, True, fn, 'models.' + action, raw)

    # ---- private knowledge ------------------------------------------------------------------------------------
    def embed_fn_for(db, index_row):
        """Synchronous query embeddings with this process's own runtime host, bound to the index's revision."""
        if index_row is None:
            return None
        row = svc.models.row(db, index_row['model_revision_id'])
        host = model_host_of(svc)
        if not host.available():
            return None
        return lambda texts: host.embed(row, texts, truncate=True)['vectors']

    @app.post(API + '/knowledge/collections', status_code=201)
    async def kc_create(request: Request):
        raw = await request.body()
        body = read_body(request, raw)
        return await run(request, True, lambda db, p: (svc.knowledge.create_collection(db, p, body.get('name'), body.get('description', '')), 201), 'knowledge.collection', raw)

    @app.get(API + '/knowledge/collections')
    async def kc_list(request: Request):
        return await run(request, False, lambda db, p: {'items': svc.knowledge.list_collections(db, p)})

    @app.get(API + '/knowledge/collections/{cid}')
    async def kc_detail(request: Request, cid: str):
        def fn(db, p):
            p.require('knowledge:read')
            return dict(svc.knowledge.collection_view(db, svc.knowledge.collection(db, p, cid)), documents_list=svc.knowledge.list_documents(db, p, cid))
        return await run(request, False, fn)

    @app.post(API + '/knowledge/collections/{cid}/documents', status_code=201)
    async def kc_add_document(request: Request, cid: str):
        raw = await request.body()
        body = read_body(request, raw)
        content = body.get('content')
        if type(content) is not str:
            raise ServiceError('VALIDATION', 'content must be a string (text/markdown/csv)')
        def fn(db, p):
            return svc.knowledge.add_document(db, p, cid, name=body.get('name'), fmt=body.get('format', 'text'), content=content.encode('utf-8'), provenance=body.get('provenance', 'declared'),
                                              source=body.get('source', ''), license=body.get('license', ''), document_id=body.get('document_id')), 201
        return await run(request, True, fn, 'knowledge.document', raw)

    @app.post(API + '/knowledge/documents/{did}/revoke')
    async def kc_revoke(request: Request, did: str):
        raw = await request.body()
        body = read_body(request, raw)
        return await run(request, True, lambda db, p: svc.knowledge.revoke_document(db, p, did, body.get('reason', '')), 'knowledge.revoke', raw)

    @app.get(API + '/knowledge/versions/{vid}/preview')
    async def kc_preview(request: Request, vid: str):
        q = request.query_params
        ordinal = int(q['ordinal']) if q.get('ordinal', '').isdigit() else None
        return await run(request, False, lambda db, p: svc.knowledge.preview(db, p, vid, ordinal))

    @app.delete(API + '/knowledge/versions/{vid}/payload')
    async def kc_delete_payload(request: Request, vid: str):
        return await run(request, True, lambda db, p: svc.knowledge.delete_version_payload(db, p, vid))

    @app.post(API + '/knowledge/collections/{cid}/indexes', status_code=202)
    async def kc_index(request: Request, cid: str):
        raw = await request.body()
        body = read_body(request, raw)
        def fn(db, p):
            p.require('knowledge:write')
            model_row = svc.models.resolve(db, 'embed', body.get('model_revision_id'))
            iid, versions, total = svc.knowledge.create_index(db, p, cid, model_row)
            out = quick_submit(svc, db, p, 'knowledge_index', {'schema': knowledge_engine.INDEX_SCHEMA, 'collection_id': cid, 'index_id': iid}, 'index build ' + iid)
            db.execute('UPDATE knowledge_indexes SET index_job_id=? WHERE id=?', (out['job_id'], iid))
            return dict(out, index_id=iid, documents=len(versions), chunks=total), 202
        return await run(request, True, fn, 'knowledge.index', raw)

    @app.get(API + '/knowledge/indexes/{iid}')
    async def kc_index_view(request: Request, iid: str):
        def fn(db, p):
            p.require('knowledge:read')
            return svc.knowledge.index_view(db, svc.knowledge.index(db, p, iid))
        return await run(request, False, fn)

    @app.post(API + '/knowledge/collections/{cid}/search')
    async def kc_search(request: Request, cid: str):
        raw = await request.body()
        body = read_body(request, raw)
        def do():
            # authorization and index lookup in a read connection; the query embedding runs outside any transaction
            with svc.db.read() as db:
                p = principal_of(request, db, False)
                p.require('knowledge:read')
                index_row = svc.knowledge.index(db, p, body['index_id']) if body.get('index_id') else svc.knowledge.latest_ready_index(db, cid)
                mode = body.get('mode', 'hybrid')
                rev = svc.models.row(db, index_row['model_revision_id']) if (index_row is not None and mode != 'lexical') else None
            qv = None
            if rev is not None:
                host = model_host_of(svc)
                if not host.available():
                    raise ServiceError('CAPABILITY_UNAVAILABLE', 'no embedding runtime in the API process; use mode=lexical')
                q = body.get('query')
                if type(q) is not str or not q.strip() or len(q) > 2000:
                    raise ServiceError('VALIDATION', 'query: 1..2000 characters')
                qv = host.embed(rev, [q], truncate=True)['vectors'][0]
            with svc.db.read() as db:
                p = principal_of(request, db, False)
                out = retrieval_mod.search(db, svc.store, p, svc.knowledge, cid, body.get('query'), mode=mode, k=body.get('k', 8), index_row=index_row, document_ids=body.get('document_ids'),
                                           embed_fn=(lambda texts: [qv]) if qv is not None else None)
            with svc.db.tx() as db:
                history.record(db, p.workspace, p.id, 'knowledge.query', 'collection', cid, {'mode': out['mode'], 'results': len(out['results']), 'ms': out['timing_ms'].get('total_ms')})
            return out
        return JSONResponse(await run_in_threadpool(do))

    @app.post(API + '/knowledge/citations/validate')
    async def kc_citations(request: Request):
        raw = await request.body()
        body = read_body(request, raw)
        return await run(request, False, lambda db, p: retrieval_mod.validate_citations(db, svc.store, p, svc.knowledge, body.get('citations')))

    @app.post(API + '/knowledge/collections/{cid}/answers', status_code=202)
    async def kc_answer(request: Request, cid: str):
        raw = await request.body()
        body = read_body(request, raw)
        def fn(db, p):
            p.require('knowledge:read'); p.require('model:use')
            inputs = dict({k: v for k, v in body.items() if k in ('index_id', 'question', 'mode', 'k', 'max_output_tokens', 'generation_revision_id')}, schema=knowledge_engine.ANSWER_SCHEMA, collection_id=cid)
            inputs.setdefault('mode', 'extractive')
            return quick_submit(svc, db, p, 'knowledge_answer', inputs, 'answer'), 202
        return await run(request, True, fn, 'knowledge.answer', raw)

    @app.get(API + '/knowledge/answers/{aid}')
    async def kc_answer_view(request: Request, aid: str):
        return await run(request, True, lambda db, p: svc.knowledge.answer(db, p, aid, svc.store))

    @app.get(API + '/knowledge/answers/by-job/{job_id}')
    async def kc_answer_by_job(request: Request, job_id: str):
        def fn(db, p):
            row = db.execute('SELECT id FROM knowledge_answers WHERE job_id=? AND workspace=?', (job_id, p.workspace)).fetchone()
            if row is None:
                raise ServiceError('NOT_FOUND', 'no answer record for this job (not finished, or not an answer job)')
            return svc.knowledge.answer(db, p, row['id'], svc.store)
        return await run(request, True, fn)

    # ---- calibration ------------------------------------------------------------------------------------------
    @app.post(API + '/calibration/datasets', status_code=201)
    async def cal_dataset(request: Request):
        raw = await request.body()
        body = read_body(request, raw)
        def fn(db, p):
            if body.get('kind', 'numeric') == 'performance':
                return svc.calibration.create_performance_dataset(db, p, body), 201
            return svc.calibration.create_numeric_dataset(db, p, body), 201
        return await run(request, True, fn, 'calibration.dataset', raw)

    @app.get(API + '/calibration/datasets')
    async def cal_datasets(request: Request):
        return await run(request, False, lambda db, p: {'items': svc.calibration.list_datasets(db, p)})

    @app.get(API + '/calibration/datasets/{did}')
    async def cal_dataset_view(request: Request, did: str):
        def fn(db, p):
            p.require('job:read')
            return svc.calibration.dataset_view(svc.calibration.dataset(db, p, did))
        return await run(request, False, fn)

    @app.get(API + '/calibration/datasets/{did}/rows')
    async def cal_dataset_rows(request: Request, did: str):
        return await run(request, False, lambda db, p: svc.calibration.rows(db, p, did))

    @app.post(API + '/calibration/fits', status_code=202)
    async def cal_fit(request: Request):
        raw = await request.body()
        body = read_body(request, raw)
        def fn(db, p):
            p.require('calibration:write')
            inputs = dict(body.get('inputs') or {}, schema='calibration-fit-input/v1')
            inputs.setdefault('device_policy', 'cpu')
            return quick_submit(svc, db, p, 'calibration_fit', inputs, body.get('title') or 'calibration fit'), 202
        return await run(request, True, fn, 'calibration.fit', raw)

    @app.get(API + '/calibration/models')
    async def cal_models(request: Request):
        return await run(request, False, lambda db, p: {'items': svc.calibration.list_models(db, p)})

    @app.get(API + '/calibration/scheduling')
    async def cal_sched_get(request: Request):
        return await run(request, False, lambda db, p: {'calibrated_scheduling_enabled': svc.calibration.scheduling_enabled(db), 'defaults': [dict(r) for r in db.execute('SELECT scope, model_id, set_by, updated_at FROM calibration_defaults WHERE workspace=?', (p.workspace,)).fetchall()]})

    @app.post(API + '/calibration/scheduling')
    async def cal_sched_set(request: Request):
        raw = await request.body()
        body = read_body(request, raw)
        def fn(db, p):
            approval_gate(svc.approvals, db, p, 'scheduling_toggle')
            return svc.calibration.set_scheduling(db, p, bool(body.get('enabled', True)))
        return await run(request, True, fn, 'calibration.scheduling', raw)

    @app.post(API + '/calibration/models/{mid}/design')
    async def cal_design(request: Request, mid: str):
        raw = await request.body()
        body = read_body(request, raw)
        return await run(request, True, lambda db, p: svc.calibration.design(db, p, mid, body), 'calibration.design', raw)

    @app.post(API + '/calibration/plan')
    async def cal_plan(request: Request):
        raw = await request.body()
        body = read_body(request, raw)
        return await run(request, False, lambda db, p: svc.calibration.plan(db, p, body.get('task_kind'), body.get('inputs')))

    @app.get(API + '/calibration/replay/{task_kind}')
    async def cal_replay(request: Request, task_kind: str):
        return await run(request, False, lambda db, p: svc.calibration.replay(db, p, task_kind))

    @app.get(API + '/calibration/models/{mid}')
    async def cal_model_view(request: Request, mid: str):
        def fn(db, p):
            p.require('job:read')
            return svc.calibration.model_view(db, svc.calibration.model(db, p, mid), full=p.can('job:read_private'))
        return await run(request, False, fn)

    @app.get(API + '/calibration/models/{mid}/comparison')
    async def cal_model_comparison(request: Request, mid: str):
        return await run(request, False, lambda db, p: svc.calibration.comparison(db, p, mid))

    @app.post(API + '/calibration/models/{mid}/{action}')
    async def cal_model_action(request: Request, mid: str, action: str):
        raw = await request.body()
        body = read_body(request, raw)
        def fn(db, p):
            if action == 'predict':
                return svc.calibration.predict(db, p, mid, body.get('features'))
            if action == 'approve':
                approval_gate(svc.approvals, db, p, 'calibration_approve')
                return svc.calibration.approve(db, p, mid, body.get('evidence'))
            if action == 'retire':
                return svc.calibration.retire(db, p, mid)
            raise ServiceError('NOT_FOUND', 'action')
        return await run(request, action != 'predict', fn, 'calibration.' + action if action != 'predict' else None, raw)

    # ---- independent verification -------------------------------------------------------------------------------
    @app.post(API + '/verification/preview')
    async def vf_preview(request: Request):
        raw = await request.body()
        body = read_body(request, raw)
        return await run(request, False, lambda db, p: svc.verification.preview(db, p, body.get('job_id'), body.get('class'), body.get('params')))

    @app.post(API + '/verification', status_code=202)
    async def vf_request(request: Request):
        raw = await request.body()
        body = read_body(request, raw)
        return await run(request, True, lambda db, p: (svc.verification.request(db, p, body.get('job_id'), body.get('class'), body.get('params')), 202), 'verification.request', raw)

    @app.get(API + '/verification')
    async def vf_list(request: Request):
        return await run(request, False, lambda db, p: {'items': svc.verification.list(db, p, request.query_params.get('job_id'))})

    @app.post(API + '/verification/verify-statement')
    async def vf_verify(request: Request):
        raw = await request.body()
        body = read_body(request, raw)
        return await run(request, False, lambda db, p: Verification.verify_statement(db, body.get('bundle'), body.get('expected')))

    @app.post(API + '/verification/policies', status_code=201)
    async def vf_policy_create(request: Request):
        raw = await request.body()
        body = read_body(request, raw)
        return await run(request, True, lambda db, p: (svc.verification.create_policy(db, p, body), 201), 'verification.policy', raw)

    @app.get(API + '/verification/policies')
    async def vf_policies(request: Request):
        return await run(request, False, lambda db, p: {'items': svc.verification.list_policies(db, p)})

    @app.post(API + '/verification/policies/{pid}/retire')
    async def vf_policy_retire(request: Request, pid: str):
        return await run(request, True, lambda db, p: svc.verification.retire_policy(db, p, pid))

    # ---- disagreement review (§65-9) -------------------------------------------------------------------------------
    @app.get(API + '/verification/disagreements')
    async def disagreements(request: Request):
        return await run(request, False, lambda db, p: {'groups': svc.disagreements.groups(db, p)})

    @app.post(API + '/verification/{vid}/decide')
    async def disagreement_decide(request: Request, vid: str):
        raw = await request.body()
        body = read_body(request, raw)
        return await run(request, True, lambda db, p: svc.disagreements.decide(db, p, vid, body.get('decision'), body.get('note', ''), body.get('evidence')), 'verification.decide', raw)

    @app.get(API + '/verification/{vid}')
    async def vf_view(request: Request, vid: str):
        return await run(request, False, lambda db, p: svc.verification.view(db, p, vid))

    @app.get(API + '/verification/{vid}/statement')
    async def vf_statement(request: Request, vid: str):
        return await run(request, False, lambda db, p: svc.verification.projection(db, p, vid))

    @app.post(API + '/verification/{vid}/resolve')
    async def vf_resolve(request: Request, vid: str):
        raw = await request.body()
        body = read_body(request, raw)
        return await run(request, True, lambda db, p: svc.verification.resolve(db, p, vid, body.get('decision'), body.get('note', '')), 'verification.resolve', raw)

    # ---- federation: operator routes ---------------------------------------------------------------------------
    @app.post(API + '/nodes', status_code=201)
    async def nodes_enroll(request: Request):
        raw = await request.body()
        body = read_body(request, raw)
        def fn(db, p):
            approval_gate(svc.approvals, db, p, 'node_enroll')
            return svc.federation.enroll(db, p, body), 201
        return await run(request, True, fn, 'nodes.enroll', raw)

    @app.get(API + '/nodes')
    async def nodes_list(request: Request):
        return await run(request, False, lambda db, p: {'items': svc.federation.list(db, p), 'trust': 'authenticated coordination among enrolled nodes on this host\'s trust domain; not a permissionless network'})

    @app.get(API + '/nodes/{nid}')
    async def nodes_view(request: Request, nid: str):
        def fn(db, p):
            p.require('job:read')
            row = svc.federation.node(db, nid)
            if p.workspace not in json.loads(row['workspaces_json']):
                raise ServiceError('NOT_FOUND', 'node')
            out = svc.federation.view(db, row)
            out['transfers'] = [dict(r) for r in db.execute('SELECT id, job_id, attempt_generation, role, direction, bytes, sha256, state, created_at FROM node_transfers WHERE node_id=? ORDER BY created_at DESC LIMIT 50', (nid,)).fetchall()]
            out['uploads'] = [dict(r) for r in db.execute('SELECT id, job_id, role, total_bytes, received_bytes, state, created_at FROM node_uploads WHERE node_id=? ORDER BY created_at DESC LIMIT 20', (nid,)).fetchall()]
            return out
        return await run(request, False, fn)

    @app.post(API + '/nodes/{nid}/{action}')
    async def nodes_control(request: Request, nid: str, action: str):
        raw = await request.body()
        body = read_body(request, raw)
        return await run(request, True, lambda db, p: svc.federation.control(db, p, nid, action, body.get('reason', '')), 'nodes.' + action, raw)

    # ---- federation: node transport (node credential + Ed25519 request signature; serve over TLS) ----------------
    def node_call(request, raw, fn):
        with svc.db.tx() as db:
            node = svc.federation.authenticate(db, request, raw)
        return fn(node)

    def node_tx(request, raw, fn):
        with svc.db.tx() as db:
            node = svc.federation.authenticate(db, request, raw)
            return fn(db, node)

    def gen_of(request):
        g = request.query_params.get('generation', '')
        if not g.isdigit():
            raise ServiceError('VALIDATION', 'generation')
        return int(g)

    @app.post('/node/v1/register')
    async def node_register(request: Request):
        raw = await request.body()
        return JSONResponse(await run_in_threadpool(node_tx, request, raw, lambda db, node: svc.federation.register(db, node, json.loads(raw or b'{}'))))

    @app.post('/node/v1/heartbeat')
    async def node_heartbeat(request: Request):
        raw = await request.body()
        return JSONResponse(await run_in_threadpool(node_tx, request, raw, lambda db, node: svc.federation.heartbeat(db, node, json.loads(raw or b'{}'))))

    @app.post('/node/v1/claim')
    async def node_claim(request: Request):
        raw = await request.body()
        return JSONResponse(await run_in_threadpool(node_tx, request, raw, lambda db, node: svc.federation.claim(db, node, json.loads(raw or b'{}'))))

    @app.post('/node/v1/jobs/{job_id}/lease')
    async def node_lease(request: Request, job_id: str):
        raw = await request.body()
        return JSONResponse(await run_in_threadpool(node_tx, request, raw, lambda db, node: svc.federation.renew(db, node, job_id, gen_of(request))))

    @app.post('/node/v1/jobs/{job_id}/progress')
    async def node_progress(request: Request, job_id: str):
        raw = await request.body()
        return JSONResponse(await run_in_threadpool(node_tx, request, raw, lambda db, node: svc.federation.progress(db, node, job_id, gen_of(request), json.loads(raw or b'{}'))))

    @app.get('/node/v1/jobs/{job_id}/checkpoint')
    async def node_checkpoint_get(request: Request, job_id: str):
        def fn(db, node):
            return svc.federation.checkpoint_blob(db, node, job_id, gen_of(request))
        gen, blob = await run_in_threadpool(node_tx, request, b'', fn)
        return Response(content=blob, media_type='application/octet-stream', headers=dict(SENSITIVE_HEADERS, **{'X-Checkpoint-Generation': str(gen)}))

    @app.post('/node/v1/jobs/{job_id}/uploads')
    async def node_upload_start(request: Request, job_id: str):
        raw = await request.body()
        return JSONResponse(await run_in_threadpool(node_tx, request, raw, lambda db, node: svc.federation.upload_start(db, node, job_id, gen_of(request), json.loads(raw or b'{}'))))

    @app.put('/node/v1/uploads/{uid}')
    async def node_upload_chunk(request: Request, uid: str):
        raw = await request.body()
        off = request.query_params.get('offset', '')
        if not off.isdigit():
            raise ServiceError('VALIDATION', 'offset')
        return JSONResponse(await run_in_threadpool(node_tx, request, raw, lambda db, node: svc.federation.upload_chunk(db, node, uid, int(off), raw)))

    @app.post('/node/v1/uploads/{uid}/complete')
    async def node_upload_complete(request: Request, uid: str):
        raw = await request.body()
        return JSONResponse(await run_in_threadpool(node_tx, request, raw, lambda db, node: svc.federation.upload_complete(db, node, uid)))

    @app.post('/node/v1/jobs/{job_id}/checkpoints')
    async def node_checkpoint_publish(request: Request, job_id: str):
        raw = await request.body()
        return JSONResponse(await run_in_threadpool(node_call, request, raw, lambda node: svc.federation.publish_checkpoint(node, job_id, gen_of(request), json.loads(raw or b'{}'))))

    @app.post('/node/v1/jobs/{job_id}/result')
    async def node_result(request: Request, job_id: str):
        raw = await request.body()
        return JSONResponse(await run_in_threadpool(node_call, request, raw, lambda node: svc.federation.publish_result(node, job_id, gen_of(request), json.loads(raw or b'{}'))))

    @app.post('/node/v1/jobs/{job_id}/fail')
    async def node_fail(request: Request, job_id: str):
        raw = await request.body()
        return JSONResponse(await run_in_threadpool(node_call, request, raw, lambda node: svc.federation.fail(node, job_id, gen_of(request), json.loads(raw or b'{}'))))

    @app.post('/node/v1/jobs/{job_id}/paused')
    async def node_paused(request: Request, job_id: str):
        raw = await request.body()
        return JSONResponse(await run_in_threadpool(node_call, request, raw, lambda node: svc.federation.paused(node, job_id, gen_of(request), json.loads(raw or b'{}'))))

    # ---- approvals ------------------------------------------------------------------------------------------
    @app.get(API + '/approvals/policy')
    async def approvals_policy(request: Request):
        return await run(request, False, lambda db, p: dict(svc.approvals.policy(db, p.workspace), actions=list(__import__('metacoin_service.approvals', fromlist=['ACTIONS']).ACTIONS)))

    @app.post(API + '/approvals/policy')
    async def approvals_policy_set(request: Request):
        raw = await request.body()
        body = read_body(request, raw)
        return await run(request, True, lambda db, p: svc.approvals.set_policy(db, p, body.get('required')), 'approvals.policy', raw)

    @app.post(API + '/approvals', status_code=201)
    async def approvals_propose(request: Request):
        raw = await request.body()
        body = read_body(request, raw)
        return await run(request, True, lambda db, p: (svc.approvals.propose(db, p, body.get('action'), body.get('content'), body.get('note', '')), 201), 'approvals.propose', raw)

    @app.get(API + '/approvals')
    async def approvals_list(request: Request):
        return await run(request, True, lambda db, p: {'items': svc.approvals.list(db, p)})

    @app.get(API + '/approvals/{pid}')
    async def approvals_view(request: Request, pid: str):
        return await run(request, True, lambda db, p: svc.approvals.view(db, p, pid))

    @app.post(API + '/approvals/{pid}/{action}')
    async def approvals_action(request: Request, pid: str, action: str):
        raw = await request.body()
        body = read_body(request, raw)
        def fn(db, p):
            if action in ('approve', 'reject'):
                return svc.approvals.decide(db, p, pid, 'approved' if action == 'approve' else 'rejected', body.get('note', ''))
            if action == 'apply':
                return svc.approvals.apply(db, p, pid)
            raise ServiceError('NOT_FOUND', 'action')
        return await run(request, True, fn, 'approvals.' + action, raw)

    # ---- evaluation registry (§65-1) ---------------------------------------------------------------------------
    @app.post(API + '/evaluation/suites', status_code=201)
    async def ev_suite_create(request: Request):
        raw = await request.body()
        body = read_body(request, raw)
        return await run(request, True, lambda db, p: (svc.evaluation.create_suite(db, p, body.get('name'), body.get('items'), body.get('threshold_percent', 100)), 201), 'evaluation.suite', raw)

    @app.get(API + '/evaluation/suites')
    async def ev_suites(request: Request):
        return await run(request, False, lambda db, p: {'items': svc.evaluation.list_suites(db, p)})

    @app.post(API + '/evaluation/suites/{sid}/runs', status_code=202)
    async def ev_run_start(request: Request, sid: str):
        raw = await request.body()
        body = read_body(request, raw)
        host = await run_in_threadpool(preloaded_generation_host, svc)
        return await run(request, True, lambda db, p: (svc.evaluation.start_run(db, p, sid, body.get('model_revision_id'), host), 202), 'evaluation.run', raw)

    @app.get(API + '/evaluation/runs/{rid}')
    async def ev_run(request: Request, rid: str):
        return await run(request, True, lambda db, p: svc.evaluation.score(db, p, rid))

    @app.get(API + '/evaluation/compare/{run_a}/{run_b}')
    async def ev_compare(request: Request, run_a: str, run_b: str):
        return await run(request, True, lambda db, p: svc.evaluation.compare(db, p, run_a, run_b))

    @app.post(API + '/evaluation/gate')
    async def ev_gate(request: Request):
        raw = await request.body()
        body = read_body(request, raw)
        return await run(request, True, lambda db, p: svc.evaluation.set_gate(db, p, body.get('suite_id')), 'evaluation.gate', raw)

    # ---- documents (Order 07 group A) ---------------------------------------------------------------------------
    @app.post(API + '/documents/import', status_code=202)
    async def documents_import(request: Request):
        """Raw bytes (Content-Type application/pdf, query: name, collection_id, mode) or JSON {name, format, content_base64 | artifact_id, collection_id, policy}."""
        raw = await request.body()
        ctype = request.headers.get('content-type', '')
        q = request.query_params
        if ctype.startswith('application/pdf') or ctype.startswith('application/octet-stream'):
            if len(raw) > settings.limits['document_max_bytes']:
                raise ServiceError('PAYLOAD_TOO_LARGE', {'code': 'document_too_large', 'limit_bytes': settings.limits['document_max_bytes']})
            body = {'name': q.get('name', 'upload.pdf'), 'format': q.get('format', 'pdf'), 'content': raw, 'collection_id': q.get('collection_id'), 'policy': {'mode': q.get('mode')} if q.get('mode') else None}
            digest_for_log = b''
        else:
            if len(raw) > settings.limits['document_max_bytes'] * 2:
                raise ServiceError('PAYLOAD_TOO_LARGE', {'code': 'document_too_large'})
            body = merkle.parse(raw) if raw else {}
            if type(body) is not dict:
                raise ServiceError('VALIDATION', 'body must be an object')
            if body.get('content_base64') is not None:
                import base64
                try:
                    body['content'] = base64.b64decode(body.pop('content_base64'), validate=True)
                except Exception:
                    raise ServiceError('VALIDATION', 'content_base64') from None
            digest_for_log = raw[:0]
        def fn(db, p):
            return svc.documents.create_import(db, p, name=body.get('name'), fmt=body.get('format', 'pdf'), content=body.get('content'), artifact_id=body.get('artifact_id'), collection_id=body.get('collection_id'), policy=body.get('policy')), 202
        return await run(request, True, fn, 'documents.import', digest_for_log)

    @app.get(API + '/documents')
    async def documents_list(request: Request):
        return await run(request, False, lambda db, p: {'items': svc.documents.list(db, p, request.query_params.get('collection_id'))})

    @app.get(API + '/documents/{iid}/compare/{other}')
    async def document_compare(request: Request, iid: str, other: str):
        from .documents.service import compare_imports
        return await run(request, False, lambda db, p: compare_imports(svc, db, p, iid, other))

    @app.get(API + '/documents/{iid}')
    async def documents_view(request: Request, iid: str):
        return await run(request, False, lambda db, p: svc.documents.view(db, p, iid))

    @app.get(API + '/documents/{iid}/pages/{index}')
    async def documents_page(request: Request, iid: str, index: int):
        return await run(request, False, lambda db, p: svc.documents.page(db, p, iid, index))

    @app.get(API + '/documents/{iid}/pages/{index}/preview.png')
    async def documents_preview(request: Request, iid: str, index: int):
        def do():
            with svc.db.read() as db:
                p = principal_of(request, db, False)
                return svc.documents.preview_png(db, p, iid, index)
        png = await run_in_threadpool(do)
        return Response(content=png, media_type='image/png', headers=dict(SENSITIVE_HEADERS, **{'Cache-Control': 'private, no-store'}))

    @app.post(API + '/documents/{iid}/cancel')
    async def documents_cancel(request: Request, iid: str):
        return await run(request, True, lambda db, p: svc.documents.cancel(db, p, iid), 'documents.cancel')

    @app.post(API + '/documents/{iid}/retry', status_code=202)
    async def documents_retry(request: Request, iid: str):
        raw = await request.body()
        body = read_body(request, raw)
        return await run(request, True, lambda db, p: (svc.documents.retry(db, p, iid, body.get('policy')), 202), 'documents.retry', raw)

    @app.post(API + '/documents/{iid}/publish')
    async def documents_publish(request: Request, iid: str):
        raw = await request.body()
        body = read_body(request, raw)
        return await run(request, True, lambda db, p: svc.documents.publish(db, p, iid, bool(body.get('include_excluded_pages', False))), 'documents.publish', raw)

    @app.post(API + '/documents/{iid}/remove')
    async def documents_remove(request: Request, iid: str):
        raw = await request.body()
        body = read_body(request, raw)
        return await run(request, True, lambda db, p: svc.documents.remove(db, p, iid, bool(body.get('confirm', False))), 'documents.remove', raw)

    @app.get(API + '/documents/tables/{tid}')
    async def documents_table(request: Request, tid: str):
        return await run(request, False, lambda db, p: svc.documents.table(db, p, tid))

    @app.post(API + '/documents/tables/{tid}/annotations', status_code=201)
    async def documents_annotate(request: Request, tid: str):
        raw = await request.body()
        body = read_body(request, raw)
        return await run(request, True, lambda db, p: (svc.documents.annotate(db, p, tid, body.get('kind'), body.get('payload') or {}), 201), 'documents.annotate', raw)

    @app.post(API + '/documents/tables/{tid}/mappings', status_code=201)
    async def documents_mapping_create(request: Request, tid: str):
        raw = await request.body()
        body = read_body(request, raw)
        return await run(request, True, lambda db, p: (svc.documents.create_mapping(db, p, tid, body.get('mapping')), 201), 'documents.mapping', raw)

    @app.post(API + '/documents/tables/{tid}/mappings/preview')
    async def documents_mapping_preview(request: Request, tid: str):
        raw = await request.body()
        body = read_body(request, raw)
        return await run(request, False, lambda db, p: svc.documents.validate_mapping(db, p, tid, body.get('mapping')))

    @app.get(API + '/documents/mappings/{mid}')
    async def documents_mapping(request: Request, mid: str):
        return await run(request, False, lambda db, p: svc.documents.mapping(db, p, mid))

    @app.post(API + '/documents/mappings/{mid}/confirm')
    async def documents_mapping_confirm(request: Request, mid: str):
        raw = await request.body()
        return await run(request, True, lambda db, p: svc.documents.confirm_mapping(db, p, mid), 'documents.mapping_confirm', raw)

    # ---- service bundles (§65-7/8) ---------------------------------------------------------------------------------
    @app.post(API + '/bundles/export')
    async def bundle_export(request: Request):
        raw = await request.body()
        body = read_body(request, raw)
        return await run(request, True, lambda db, p: svc.bundles.export(db, p, body), 'bundles.export', raw)

    @app.post(API + '/bundles/check')
    async def bundle_check(request: Request):
        raw = await request.body()
        body = read_body(request, raw)
        return await run(request, False, lambda db, p: svc.bundles.check(db, p, body.get('bundle')))

    @app.post(API + '/bundles/import')
    async def bundle_import(request: Request):
        raw = await request.body()
        body = read_body(request, raw)
        return await run(request, True, lambda db, p: svc.bundles.import_bundle(db, p, body.get('bundle'), bool(body.get('apply', False))), 'bundles.import', raw)

    # ---- typed intent compilation (Order 07 group C) -------------------------------------------------------------
    @app.post(API + '/agents/intents', status_code=201)
    async def intent_create(request: Request):
        raw = await request.body()
        body = read_body(request, raw)
        def do():
            host = preloaded_generation_host(svc)                  # loaded (or None) before the write transaction below
            return run_sync(request, True, lambda db, p: (svc.intents.compile(db, p, body.get('request') or {}, host), 201), 'agents.intent', raw)
        return await run_in_threadpool(do)

    @app.get(API + '/agents/intents')
    async def intent_list(request: Request):
        return await run(request, False, lambda db, p: {'items': svc.intents.list(db, p)})

    @app.get(API + '/agents/intents/{iid}')
    async def intent_view(request: Request, iid: str):
        return await run(request, False, lambda db, p: svc.intents.view(db, p, iid))

    @app.post(API + '/agents/intents/{iid}/continue')
    async def intent_continue(request: Request, iid: str):
        raw = await request.body()
        body = read_body(request, raw)
        def do():
            host = preloaded_generation_host(svc)
            return run_sync(request, True, lambda db, p: svc.intents.continue_intent(db, p, iid, body.get('token'), body.get('answers'), body.get('inputs'), host), 'agents.intent_continue', raw)
        return await run_in_threadpool(do)

    # ---- structured planning (§51) --------------------------------------------------------------------------------
    @app.post(API + '/agents/plans', status_code=201)
    async def plan_create(request: Request):
        raw = await request.body()
        body = read_body(request, raw)
        def do():
            preselected = None
            if isinstance(body, dict) and body.get('assist') == 'model' and body.get('draft') is None:
                # read phase (authorization, catalog, retrieved context), then the model runs outside any transaction, then the write phase stores the draft
                with svc.db.read() as db:
                    p = principal_of(request, db, False)
                    goal = svc.planner.check_goal(body)
                    cat = svc.planner.catalog(db, p)
                    row, context = svc.planner.selection_context(db, p, goal, body.get('collection_id'))
                preselected = svc.planner.select_with_model(model_host_of(svc), row, cat, context, goal)
            return run_sync(request, True, lambda db, p: (svc.planner.create(db, p, body, None, preselected), 201), 'agents.plan', raw)
        return await run_in_threadpool(do)

    @app.get(API + '/agents/plans')
    async def plan_list(request: Request):
        return await run(request, False, lambda db, p: {'items': svc.planner.list(db, p)})

    @app.get(API + '/agents/plans/{pid}')
    async def plan_view(request: Request, pid: str):
        return await run(request, False, lambda db, p: svc.planner.view(db, p, pid))

    @app.post(API + '/agents/plans/{pid}/accept')
    async def plan_accept(request: Request, pid: str):
        raw = await request.body()
        return await run(request, True, lambda db, p: svc.planner.accept(db, p, pid), 'agents.plan_accept', raw)

    # ---- experiment notebooks (§65-2) ---------------------------------------------------------------------------
    @app.post(API + '/notebooks', status_code=201)
    async def nb_create(request: Request):
        raw = await request.body()
        body = read_body(request, raw)
        return await run(request, True, lambda db, p: (svc.notebooks.create(db, p, body.get('name'), body.get('blocks'), body.get('note', '')), 201), 'notebook.create', raw)

    @app.get(API + '/notebooks')
    async def nb_list(request: Request):
        return await run(request, False, lambda db, p: {'items': svc.notebooks.list(db, p)})

    @app.get(API + '/notebooks/{nid}')
    async def nb_view(request: Request, nid: str, version: int = None):
        return await run(request, False, lambda db, p: svc.notebooks.view(db, p, nid, version))

    @app.post(API + '/notebooks/{nid}/versions', status_code=201)
    async def nb_version(request: Request, nid: str):
        raw = await request.body()
        body = read_body(request, raw)
        return await run(request, True, lambda db, p: (svc.notebooks.add_version(db, p, nid, body.get('blocks'), body.get('note', '')), 201), 'notebook.version', raw)

    @app.get(API + '/notebooks/{nid}/compare/{va}/{vb}')
    async def nb_compare(request: Request, nid: str, va: int, vb: int):
        return await run(request, False, lambda db, p: svc.notebooks.compare(db, p, nid, va, vb))

    @app.get(API + '/notebooks/{nid}/export')
    async def nb_export(request: Request, nid: str, version: int = None):
        return await run(request, True, lambda db, p: svc.notebooks.export(db, p, nid, version))

    # ---- Group F: workflow packages, compatibility, composite quotes, gated runs, result bundles ------------------
    @app.post(API + '/packages', status_code=201)
    async def package_create(request: Request):
        raw = await request.body()
        body = read_body(request, raw)
        return await run(request, True, lambda db, p: (svc.packages.create(db, p, body), 201), 'package.create', raw)

    @app.get(API + '/packages')
    async def package_list(request: Request):
        return await run(request, False, lambda db, p: {'items': svc.packages.list(db, p)})

    @app.post(API + '/packages/compatibility')
    async def package_compat(request: Request):
        raw = await request.body()
        body = read_body(request, raw)
        def fn(db, p):
            manifest = body.get('manifest') or (svc.packages.view(db, p, body['package_id'])['manifest'] if body.get('package_id') else None)
            return svc.packages.compatibility(db, p, manifest, body.get('device_policy'))
        return await run(request, False, fn)

    @app.post(API + '/packages/import', status_code=201)
    async def package_import(request: Request):
        raw = await request.body()
        body = read_body(request, raw)
        return await run(request, True, lambda db, p: (svc.packages.import_manifest(db, p, (body.get('manifest') or {}).get('package') or body.get('manifest'), bool(body.get('apply'))), 201 if body.get('apply') else 200), 'package.import', raw)

    @app.get(API + '/packages/runs')
    async def package_runs_all(request: Request):
        return await run(request, True, lambda db, p: {'items': svc.packages.list_runs(db, p)})

    @app.get(API + '/packages/runs/{rid}')
    async def package_run_view(request: Request, rid: str):
        return await run(request, True, lambda db, p: svc.packages.run_view(db, p, rid))

    @app.post(API + '/packages/runs/{rid}/retry', status_code=202)
    async def package_run_retry(request: Request, rid: str):
        raw = await request.body()
        return await run(request, True, lambda db, p: (svc.packages.retry(db, p, rid), 202), 'package.retry', raw)

    @app.post(API + '/packages/runs/{rid}/bundle')
    async def package_run_bundle(request: Request, rid: str):
        raw = await request.body()
        body = read_body(request, raw)
        return await run(request, True, lambda db, p: svc.packages.result_bundle_json(db, p, rid, body.get('scope')), 'package.bundle', raw)

    @app.get(API + '/packages/runs/{rid}/bundle.zip')
    async def package_run_bundle_zip(request: Request, rid: str):
        def do():
            with svc.db.tx() as db:
                p = principal_of(request, db, True)
                data, manifest = svc.packages.result_bundle(db, p, rid, None)
                return data
        data = await run_in_threadpool(do)
        return Response(content=data, media_type='application/zip', headers=dict(SENSITIVE_HEADERS, **{'Content-Disposition': 'attachment; filename="result-bundle-' + rid + '.zip"', 'Cache-Control': 'private, no-store'}))

    @app.get(API + '/packages/{pid}')
    async def package_view(request: Request, pid: str):
        return await run(request, False, lambda db, p: svc.packages.view(db, p, pid))

    @app.post(API + '/packages/{pid}/retire')
    async def package_retire(request: Request, pid: str):
        raw = await request.body()
        return await run(request, True, lambda db, p: svc.packages.retire(db, p, pid), 'package.retire', raw)

    @app.get(API + '/packages/{pid}/export')
    async def package_export(request: Request, pid: str):
        return await run(request, True, lambda db, p: svc.packages.export(db, p, pid, request.query_params.get('example', '1') == '1'))

    @app.post(API + '/packages/{pid}/instantiate', status_code=201)
    async def package_instantiate(request: Request, pid: str):
        raw = await request.body()
        body = read_body(request, raw)
        return await run(request, True, lambda db, p: (svc.packages.instantiate(db, p, pid, body.get('values'), body.get('inputs'), body.get('name')), 201), 'package.instantiate', raw)

    @app.post(API + '/packages/{pid}/quote', status_code=201)
    async def package_quote(request: Request, pid: str):
        raw = await request.body()
        body = read_body(request, raw)
        return await run(request, True, lambda db, p: (svc.packages.quote(db, p, pid, body.get('workflow_id'), body.get('scheme', 'exact')), 201), 'package.quote', raw)

    @app.get(API + '/packages/quotes/{qid}')
    async def package_quote_view(request: Request, qid: str):
        return await run(request, False, lambda db, p: svc.packages.quote_view(db, p, qid))

    @app.post(API + '/packages/{pid}/runs', status_code=202)
    async def package_run_start(request: Request, pid: str):
        raw = await request.body()
        body = read_body(request, raw)
        return await run(request, True, lambda db, p: (svc.packages.start_run(db, p, pid, body.get('quote_id'), body.get('budget_ceiling')), 202), 'package.run', raw)

    @app.post(API + '/packages/{pid}/bind-job', status_code=201)
    async def package_bind_job(request: Request, pid: str):
        raw = await request.body()
        body = read_body(request, raw)
        return await run(request, True, lambda db, p: (svc.packages.bind_metered_job(db, p, pid, body.get('job_id')), 201), 'package.bind', raw)

    @app.post(API + '/ops/faults')
    async def ops_faults(request: Request):
        """Fault injection for disposable test instances only: refused unless the instance was started with limits.test_hooks and
        the caller holds admin:credentials. Never enabled by configuration files or environment in normal operation."""
        raw = await request.body()
        body = read_body(request, raw)
        def fn(db, p):
            p.require('admin:credentials')
            if not settings.limits.get('test_hooks'):
                raise ServiceError('CAPABILITY_UNAVAILABLE', {'code': 'test_hooks_disabled'})
            from .economy.ops import FAULT_POINTS
            if body.get('fault') in FAULT_POINTS:
                key = 'fault:' + body['fault']
                if body.get('disarm'):
                    db.execute('DELETE FROM meta WHERE key=?', (key,))
                    return {'fault': body['fault'], 'armed': False}
                db.execute('INSERT OR REPLACE INTO meta VALUES (?, ?)', (key, '1'))
                history.record(db, p.workspace, p.id, 'ops.fault', 'service', body['fault'], {'armed': True, 'point': body['fault'], 'disposable_instance_only': True})
                return {'fault': body['fault'], 'armed': True, 'note': 'raises at the named checkpoint until disarmed; disposable instances only'}
            if body.get('fault') != 'verification_fail' or type(body.get('job_id')) is not str:
                raise ServiceError('VALIDATION', {'code': 'fault', 'allowed': ['verification_fail'] + list(FAULT_POINTS), 'fields': ['job_id']})
            key = 'fault:verification_fail:' + body['job_id']
            if body.get('clear'):
                db.execute('DELETE FROM meta WHERE key=?', (key,))
            else:
                db.execute('INSERT OR REPLACE INTO meta VALUES (?, ?)', (key, json.dumps({'by': p.id, 'at': now()})))
            history.record(db, p.workspace, p.id, 'ops.fault', 'job', body['job_id'], {'fault': 'verification_fail', 'cleared': bool(body.get('clear'))})
            return {'fault': 'verification_fail', 'job_id': body['job_id'], 'active': not body.get('clear'), 'scope': 'this disposable instance only'}
        return await run(request, True, fn, 'ops.fault', raw)

    # ---- backlog 3/4: reconciliation and measurement requests; backlog 8/9: upgrade preview and review queue ----------
    @app.post(API + '/reconciliations', status_code=201)
    async def reconciliation_create(request: Request):
        raw = await request.body()
        body = read_body(request, raw)
        return await run(request, True, lambda db, p: (svc.reconciliations.create(db, p, body), 201), 'reconciliation.create', raw)

    @app.get(API + '/reconciliations')
    async def reconciliation_list(request: Request):
        return await run(request, False, lambda db, p: {'items': svc.reconciliations.list(db, p)})

    @app.get(API + '/reconciliations/{rid}')
    async def reconciliation_view(request: Request, rid: str):
        return await run(request, False, lambda db, p: svc.reconciliations.view(db, p, rid))

    @app.post(API + '/measurement-requests', status_code=201)
    async def measurement_request_create(request: Request):
        raw = await request.body()
        body = read_body(request, raw)
        return await run(request, True, lambda db, p: (svc.reconciliations.create_request(db, p, body), 201), 'measurement.request', raw)

    @app.get(API + '/measurement-requests')
    async def measurement_request_list(request: Request):
        return await run(request, False, lambda db, p: {'items': svc.reconciliations.list_requests(db, p)})

    @app.get(API + '/measurement-requests/{rid}')
    async def measurement_request_view(request: Request, rid: str):
        return await run(request, False, lambda db, p: svc.reconciliations.request_view(db, p, rid))

    @app.get(API + '/measurement-requests/{rid}/export')
    async def measurement_request_export(request: Request, rid: str):
        return await run(request, True, lambda db, p: svc.reconciliations.export_request(db, p, rid))

    @app.get(API + '/packages/{pid}/upgrade-preview/{new_pid}')
    async def package_upgrade_preview(request: Request, pid: str, new_pid: str):
        return await run(request, False, lambda db, p: svc.packages.upgrade_preview(db, p, pid, new_pid))

    @app.get(API + '/review-queue')
    async def review_queue(request: Request):
        return await run(request, False, lambda db, p: svc.analyses.review_queue(db, p))

    # ---- Group E: analysis sessions, impact, regeneration, reports, projections ----------------------------------
    @app.post(API + '/analyses', status_code=201)
    async def analysis_create(request: Request):
        raw = await request.body()
        body = read_body(request, raw)
        return await run(request, True, lambda db, p: (svc.analyses.create(db, p, body.get('name'), body.get('blocks'), body.get('from_document'), body.get('from_workflow'), body.get('note', '')), 201), 'analysis.create', raw)

    @app.get(API + '/analyses')
    async def analysis_list(request: Request):
        return await run(request, False, lambda db, p: {'items': svc.analyses.list(db, p)})

    @app.get(API + '/analyses/{aid}')
    async def analysis_view(request: Request, aid: str, version: int = None):
        return await run(request, False, lambda db, p: svc.analyses.view(db, p, aid, version))

    @app.post(API + '/analyses/{aid}/revisions', status_code=201)
    async def analysis_revise(request: Request, aid: str):
        raw = await request.body()
        body = read_body(request, raw)
        return await run(request, True, lambda db, p: (svc.analyses.revise(db, p, aid, body.get('blocks'), body.get('expected_version'), body.get('note', '')), 201), 'analysis.revise', raw)

    @app.post(API + '/analyses/{aid}/freeze')
    async def analysis_freeze(request: Request, aid: str):
        raw = await request.body()
        body = read_body(request, raw)
        return await run(request, True, lambda db, p: svc.analyses.freeze(db, p, aid, body.get('version'), body.get('reason', '')), 'analysis.freeze', raw)

    @app.post(API + '/analyses/{aid}/impact')
    async def analysis_impact(request: Request, aid: str):
        raw = await request.body()
        body = read_body(request, raw)
        return await run(request, False, lambda db, p: svc.analyses.impact(db, p, aid, body.get('changed'), body.get('version')))

    @app.post(API + '/analyses/{aid}/regeneration-plan')
    async def analysis_regeneration_plan(request: Request, aid: str):
        raw = await request.body()
        body = read_body(request, raw)
        return await run(request, False, lambda db, p: svc.analyses.regeneration_plan(db, p, aid, body.get('run_id'), body.get('changes'), body.get('budget_ceiling')))

    @app.post(API + '/analyses/{aid}/regenerate', status_code=202)
    async def analysis_regenerate(request: Request, aid: str):
        raw = await request.body()
        body = read_body(request, raw)
        return await run(request, True, lambda db, p: (svc.analyses.regenerate(db, p, aid, body.get('run_id'), body.get('changes'), body.get('budget_ceiling'), body.get('note', '')), 202), 'analysis.regenerate', raw)

    @app.post(API + '/analyses/{aid}/reports', status_code=201)
    async def analysis_report_build(request: Request, aid: str):
        raw = await request.body()
        body = read_body(request, raw)
        host = preloaded_generation_host(svc) if body.get('mode') == 'model' else None
        return await run(request, True, lambda db, p: (svc.analyses.build_report(db, p, aid, body.get('version'), body.get('mode', 'deterministic'), host), 201), 'analysis.report', raw)

    @app.get(API + '/reports/{rid}')
    async def report_view(request: Request, rid: str):
        return await run(request, False, lambda db, p: svc.analyses.report(db, p, rid))

    @app.get(API + '/reports/{rid}/html')
    async def report_html(request: Request, rid: str):
        def do():
            with svc.db.read() as db:
                p = principal_of(request, db, False)
                return svc.analyses.report_html(db, p, rid)
        text = await run_in_threadpool(do)
        return Response(content=text, media_type='text/html; charset=utf-8', headers=dict(SENSITIVE_HEADERS, **{'Cache-Control': 'private, no-store', 'Content-Security-Policy': "default-src 'none'; style-src 'unsafe-inline'"}))

    @app.get(API + '/reports/{rid}/pdf')
    async def report_pdf(request: Request, rid: str):
        def do():
            with svc.db.tx() as db:
                p = principal_of(request, db, True)
                return svc.analyses.report_pdf(db, p, rid, None)
        data = await run_in_threadpool(do)
        return Response(content=data, media_type='application/pdf', headers=dict(SENSITIVE_HEADERS, **{'Content-Disposition': 'attachment; filename="report-' + rid + '.pdf"', 'Cache-Control': 'private, no-store'}))

    @app.get(API + '/reports/{rid}/projections/{pid}/pdf')
    async def projection_pdf(request: Request, rid: str, pid: str):
        def do():
            with svc.db.tx() as db:
                p = principal_of(request, db, True)
                return svc.analyses.report_pdf(db, p, rid, pid)
        data = await run_in_threadpool(do)
        return Response(content=data, media_type='application/pdf', headers=dict(SENSITIVE_HEADERS, **{'Content-Disposition': 'attachment; filename="projection-' + pid + '.pdf"', 'Cache-Control': 'private, no-store'}))

    @app.get(API + '/reports/{rid}/bundle')
    async def report_bundle(request: Request, rid: str):
        return await run(request, True, lambda db, p: svc.analyses.report_bundle(db, p, rid))

    @app.post(API + '/reports/{rid}/projection/preview')
    async def report_projection_preview(request: Request, rid: str):
        raw = await request.body()
        body = read_body(request, raw)
        return await run(request, False, lambda db, p: svc.analyses.projection_preview(db, p, rid, body.get('scope')))

    @app.post(API + '/reports/{rid}/projection', status_code=201)
    async def report_projection_export(request: Request, rid: str):
        raw = await request.body()
        body = read_body(request, raw)
        return await run(request, True, lambda db, p: (svc.analyses.export_projection(db, p, rid, body.get('scope'), bool(body.get('acknowledge_warnings'))), 201), 'analysis.projection', raw)

    @app.post(API + '/reports/projection/verify')
    async def report_projection_verify(request: Request):
        raw = await request.body()
        body = read_body(request, raw)
        return await run(request, False, lambda db, p: svc.analyses.verify_projection(db, body.get('bundle')))

    # ---- usage statements -----------------------------------------------------------------------------------
    @app.get(API + '/statements')
    async def statement(request: Request):
        q = request.query_params
        def fn(db, p):
            try:
                since, until, page = int(q.get('since', '0')), (int(q['until']) if q.get('until') else None), int(q.get('page', '1'))
            except ValueError:
                raise ServiceError('VALIDATION', 'since/until/page')
            b = statements_mod.build(db, p, settings, since, until, page)
            history.record(db, p.workspace, p.id, 'artifact.exported', 'statement', b['digest'][:16], {'rows': b['statement']['total_rows'], 'page': page})
            return b
        return await run(request, True, fn)

    @app.get(API + '/statements.csv')
    async def statement_csv(request: Request):
        q = request.query_params
        def do():
            with svc.db.tx() as db:
                p = principal_of(request, db, False)
                b = statements_mod.build(db, p, settings, int(q.get('since', '0')), (int(q['until']) if q.get('until') else None), int(q.get('page', '1')))
                return statements_mod.export_csv(b)
        return Response(content=await run_in_threadpool(do), media_type='text/csv', headers=SENSITIVE_HEADERS)

    @app.get(API + '/tracing')
    async def tracing_status(request: Request):
        return await run(request, False, lambda db, p: tracing.status())

    from . import console
    console.mount(app, svc)
    from .economy import routes as economy_routes
    economy_routes.mount(app, svc, run, read_body, API)
    return app


def _workspace_of_job(db, job_id):
    row = db.execute('SELECT workspace FROM jobs WHERE id=?', (job_id,)).fetchone()
    if row is None:
        raise ServiceError('NOT_FOUND', 'job')
    return row['workspace']


def _assigned(db, artifact_row, principal):
    cid = artifact_row['contract_id']
    if cid is None:
        return False
    row = db.execute('SELECT reviewer_id FROM contracts WHERE id=?', (cid,)).fetchone()
    return row is not None and row['reviewer_id'] == principal.id


def artifact_view(row, principal, db):
    return {'id': row['id'], 'kind': row['kind'], 'encrypted': bool(row['encrypted']), 'format_version': row['format_version'],
            'public': bool(row['public']), 'size_plaintext': row['size_plaintext'], 'sha256_plaintext': row['sha256_plaintext'],
            'recipients': json.loads(row['recipients_json']), 'intended_use': row['intended_use'],
            'retention_deadline': row['retention_deadline'], 'deleted_at': row['deleted_at'], 'created_at': row['created_at']}


_REVISION_AT_START = None


def _revision():
    """The source revision this process loaded: read once at first use (process start), never re-read, so a long-running
    service reports the code it runs rather than whatever the working tree's HEAD has moved to since."""
    global _REVISION_AT_START
    if _REVISION_AT_START is None:
        import subprocess
        from pathlib import Path
        try:
            _REVISION_AT_START = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=Path(__file__).resolve().parents[1], text=True, stderr=subprocess.DEVNULL).strip()
        except Exception:
            _REVISION_AT_START = 'unavailable'
    return _REVISION_AT_START


def capability_table(svc, db):
    settings = svc.settings
    from integrations.x402 import loopback_harness as lb
    installed_x402 = lb.available()
    return {
        'service': {'version': '0.1.0', 'revision': _revision(), 'bind': settings.host + ':' + str(settings.port),
                    'dev_http_loopback': settings.dev_http_loopback},
        'authentication': {'installed': True, 'configured': True, 'available': True, 'externally_validated': False,
                           'model': 'server-side principals; bearer credentials (peppered hash); server-side sessions with CSRF; no passwords'},
        'encryption_at_rest': {'installed': crypto.AGE_AVAILABLE, 'configured': (settings.keys_dir / 'service.age').exists(),
                               'available': crypto.AGE_AVAILABLE and (settings.keys_dir / 'service.age').exists(),
                               'format': crypto.ARTIFACT_FORMAT, 'externally_validated': False},
        'signed_reviews': {'installed': crypto.ED25519_AVAILABLE, 'configured': True, 'available': crypto.ED25519_AVAILABLE,
                           'custody': 'server-managed reviewer keys (not non-custodial)', 'externally_validated': False},
        'job_queue': {'installed': True, 'available': True, 'execution': 'child process with rlimits; allowlisted kinds only; not a sandbox for hostile code'},
        'x402_http_sale': {'installed': installed_x402, 'configured': settings.provider_mode in ('test-http', 'production'),
                           'available': installed_x402 and settings.provider_mode in ('test-http', 'production'),
                           'mode': settings.provider_mode, 'externally_validated': False,
                           'settlement_observed': 'none (test double in test-http; production unexercised)'},
        'x402_variable_price_upto': {'installed': installed_x402 and svc.sales.upto_available(), 'configured': settings.provider_mode in ('test-http', 'production'), 'available': installed_x402 and svc.sales.upto_available(),
                                     'code_readiness': 'quote scheme=upto -> 402 upto requirements (Permit2 witness) -> verified authorization creates the job -> settlement of the measured amount after completion; failed jobs leave the authorization unused',
                                     'local_protocol_validation': 'private py-evm chain with the pinned Permit2 / x402UptoPermit2Proxy / mock token in test-http mode (integrations/x402/local_chain)',
                                     'external_production_settlement': 'unverified: no external facilitator or public network exercised', 'externally_validated': False},
        'payment_actions': {'modes': {'simulation': 'zero-value in-process stub', 'test-http': 'in-process SDK objects with facilitator double',
                                      'production': __import__('metacoin_service.buyer', fromlist=['status']).status(settings)},
                            'current_mode': settings.provider_mode, 'externally_validated': False},
        'retention': {'installed': True, 'available': True, 'semantics': 'deadline per private artifact; cleanup unlinks ciphertext; not secure erasure'},
        'science': {'models': [energy.MODEL_ID, science.SAFE_RUNTIME_MODEL, science.COMPARISON_MODEL],
                    'verifier_bundles': {'energy_audit': terms.verifier_digest(), 'service_science': science.bundle_digest()}},
        'external_settlement_observed': False, 'external_team_participation': False,
    }


def compare_jobs(svc, db, principal, job_a, job_b):
    """Explain what changed between two completed runs of the same kind: policy, assumptions,
    inputs (by root only), verdict and margins. Private fields for the owner/designated reviewer;
    a viewer receives only the differences both contracts permit publicly after review."""
    rows = [svc.jobs.get(db, principal, j) for j in (job_a, job_b)]
    if rows[0]['kind'] != rows[1]['kind']:
        raise ServiceError('CONFLICT', 'runs of different kinds are not comparable')
    if any(r['state'] != 'succeeded' for r in rows):
        raise ServiceError('CONFLICT', 'both runs must have a committed result')
    contracts = [db.execute('SELECT * FROM contracts WHERE id=?', (r['contract_id'],)).fetchone() for r in rows]
    pols = [json.loads(c['policy_json']) for c in contracts]
    docs = [merkle.parse(c['contract_json']) for c in contracts]
    private = principal.can('job:read_private') or (principal.role == 'reviewer' and all(c['reviewer_id'] == principal.id for c in contracts))
    public_ok = all(p['disclose_outcome'] and r['review_state'] == 'accepted' for p, r in zip(pols, rows))
    out = {'kind': rows[0]['kind'], 'jobs': [job_a, job_b],
           'contract_digests': [c['contract_digest'] for c in contracts], 'input_roots': [c['input_root'] for c in contracts],
           'same_inputs': contracts[0]['input_root'] == contracts[1]['input_root'],
           'same_lineage': contracts[0]['lineage_id'] == contracts[1]['lineage_id'],
           'verifier_digests': [d['verifier_digest'] for d in docs],
           'changed_policy': {k: [pols[0].get(k), pols[1].get(k)] for k in pols[0] if pols[0].get(k) != pols[1].get(k)},
           'changed_assumptions': sorted(set(docs[0].get('assumptions', [])) ^ set(docs[1].get('assumptions', []))),
           'scope': 'differences between two submitted runs; no claim about runs not compared'}
    if private:
        sums = [json.loads(r['summary_json']) if r['summary_json'] else {} for r in rows]
        keys = sorted(set(sums[0]) & set(sums[1]) & {'outcome', 'worst_margin', 'best_margin', 'required_low', 'required_high',
                                                         'additional_usable_energy', 'margin_width', 'dominant_uncertainty_source',
                                                         'safe_duration', 'status', 'selected_id', 'selected_ids', 'total_value'})
        out['outcomes'] = [r['outcome'] for r in rows]
        out['changed_results'] = {k: [sums[0].get(k), sums[1].get(k)] for k in keys if sums[0].get(k) != sums[1].get(k)}
        out['margin_delta_worst_case_mJ'] = (sums[1].get('worst_margin') - sums[0].get('worst_margin')) if all('worst_margin' in x for x in sums) else None
        out['projection'] = 'private'
    elif public_ok:
        out['outcomes'] = [r['outcome'] for r in rows]
        out['projection'] = 'public: outcomes and bindings only (both contracts disclose the outcome and are accepted)'
    else:
        out['outcomes'] = None
        out['projection'] = 'public: bindings only'
    return out


def invoke_under_quote(svc, db, principal, sid, quote_id, inputs):
    """Consume the quote atomically, then create the bound contract and job (reviewer = first workspace reviewer)."""
    quote, service = svc.catalog.consume(db, principal, quote_id, inputs, provider_mode=svc.settings.provider_mode)
    agents_mod.guard(db, principal, 'invoke', service_id=sid, service_kind=service['kind'], jobs=1)
    principal.agent_counted = True                     # the job below is already counted against the grant
    reviewer = db.execute("SELECT id FROM principals WHERE workspace=? AND role='reviewer' AND revoked_at IS NULL ORDER BY created_at LIMIT 1", (principal.workspace,)).fetchone()
    policy = {'reviewer_id': reviewer['id'] if reviewer else None}
    cid = svc.contracts.create_draft(db, principal, kind=service['kind'], title=service['name'] + ' invocation', inputs=inputs, policy=policy, datasets=svc.datasets)
    svc.contracts.freeze(db, principal, cid)
    jid = svc.jobs.submit(db, principal, cid)
    db.execute('UPDATE jobs SET quote_id=? WHERE id=?', (quote['id'], jid))
    db.execute('UPDATE contracts SET quote_id=? WHERE id=?', (quote['id'], cid))
    from .datasets import add_edge
    add_edge(db, principal.workspace, 'quote', quote['id'], 'job', jid, 'used_input')
    return {'job_id': jid, 'contract_id': cid, 'quote_id': quote['id'], 'state': 'queued', 'service_id': sid}
