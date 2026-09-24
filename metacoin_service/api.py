"""Versioned HTTP API. Every handler authenticates a server-side principal, parses the
body with the strict canonical parser (bounded size, no duplicate keys, no floats),
and calls the narrow services; state machines live in the services, not here."""
import hashlib
import json
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from experiments.private_receipts import receipt as merkle
from experiments.work_contracts import contract as terms, energy_analysis as energy, explanation
from . import actions as actions_mod, artifacts as artifacts_mod, auth, contracts as contracts_mod, crypto, history
from . import campaigns as campaigns_mod, datasets as datasets_mod, jobs as jobs_mod, reviews as reviews_mod, science, templates_svc, workflows as workflows_mod, x402_http
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

    @app.get(API + '/lineage/{object_type}/{object_id}')
    async def lineage_query(request: Request, object_type: str, object_id: str):
        q = request.query_params
        def fn(db, p):
            p.require('history:read')
            if object_type not in ('dataset_version', 'contract', 'job', 'artifact', 'review', 'workflow_run', 'campaign', 'quote', 'usage'):
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
            jid = svc.jobs.submit(db, p, body.get('contract_id'))
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

    from . import console
    console.mount(app, svc)
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


def _revision():
    import subprocess
    from pathlib import Path
    try:
        return subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=Path(__file__).resolve().parents[1], text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:
        return 'unavailable'


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
