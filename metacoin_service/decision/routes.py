"""HTTP routes of the decision-evidence capability: /api/v1/decisions, /api/v1/acquisition-plans, sampling plans and
assurance under /api/v1/verification, portfolios under /api/v1/portfolios. Mounted from api.create_app with the API's own
run()/read_body helpers (authentication, idempotency, strict JSON, error mapping)."""
from fastapi import Request
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool


def mount(app, svc, run, read_body, API):
    D, S = svc.decisions, svc.sampling

    # ---- decisions ----------------------------------------------------------------------------------------------------------------
    @app.post(API + '/decisions/analyze', status_code=202)
    async def decision_analyze(request: Request):
        raw = await request.body(); body = read_body(request, raw)
        return await run(request, True, lambda db, p: (D.analyze(db, p, body), 202), 'decision.analyze', raw)

    @app.get(API + '/decisions')
    async def decisions_list(request: Request, kind: str = None):
        return await run(request, False, lambda db, p: {'items': D.list(db, p, kind)})

    @app.get(API + '/decisions/{jid}')
    async def decision_view(request: Request, jid: str):
        return await run(request, False, lambda db, p: D.view(db, p, jid))

    @app.get(API + '/decisions/{jid}/projection')
    async def decision_projection(request: Request, jid: str):
        return await run(request, False, lambda db, p: D.projection(db, p, jid))

    @app.get(API + '/decisions/{jid}/witnesses')
    async def decision_witnesses(request: Request, jid: str):
        return await run(request, False, lambda db, p: D.witnesses(db, p, jid))

    @app.get(API + '/decisions/{jid}/bundle')
    async def decision_bundle(request: Request, jid: str):
        def sync():
            with svc.db.tx() as db:
                from ..api import principal_of
                p = principal_of(request, db, True)
                return D.bundle(db, p, jid)
        data = await run_in_threadpool(sync)
        return Response(content=data, media_type='application/zip', headers={'Content-Disposition': 'attachment; filename="decision-bundle-%s.zip"' % jid})

    # ---- acquisition plans -----------------------------------------------------------------------------------------------------------
    @app.post(API + '/acquisition-plans/preview')
    async def plan_preview(request: Request):
        raw = await request.body(); body = read_body(request, raw)
        return await run(request, False, lambda db, p: D.plan_preview(db, p, body))

    @app.post(API + '/acquisition-plans', status_code=201)
    async def plan_create(request: Request):
        raw = await request.body(); body = read_body(request, raw)
        return await run(request, True, lambda db, p: (D.plan_create(db, p, body), 201), 'decision.plan', raw)

    @app.get(API + '/acquisition-plans')
    async def plans_list(request: Request):
        return await run(request, False, lambda db, p: {'items': D.plan_list(db, p)})

    @app.get(API + '/acquisition-plans/{pid}')
    async def plan_view(request: Request, pid: str):
        return await run(request, False, lambda db, p: D.plan_view(db, p, pid))

    @app.post(API + '/acquisition-plans/{pid}/materialize', status_code=201)
    async def plan_materialize(request: Request, pid: str):
        raw = await request.body(); body = read_body(request, raw)
        return await run(request, True, lambda db, p: (D.materialize(db, p, pid, body), 201), 'decision.materialize', raw)

    # ---- sampling plans and assurance (verification extension) ---------------------------------------------------------------------------
    @app.post(API + '/verification/sampling-plans', status_code=201)
    async def sampling_create(request: Request):
        raw = await request.body(); body = read_body(request, raw)
        if body.get('preview'):
            return await run(request, False, lambda db, p: (S.create(db, p, body, preview=True), 200))
        return await run(request, True, lambda db, p: (S.create(db, p, body), 201), 'verification.sampling_plan', raw)

    @app.get(API + '/verification/sampling-plans')
    async def sampling_list(request: Request, job_id: str = None):
        return await run(request, False, lambda db, p: {'items': S.list(db, p, job_id)})

    @app.post(API + '/verification/sampling-plans/verify')
    async def sampling_verify(request: Request):
        raw = await request.body(); body = read_body(request, raw)
        return await run(request, False, lambda db, p: S.verify_statement_offline(body.get('bundle'), body.get('expected_plan')))

    @app.get(API + '/verification/sampling-plans/{pid}')
    async def sampling_view(request: Request, pid: str):
        return await run(request, False, lambda db, p: S.view(db, p, pid))

    @app.post(API + '/verification/sampling-plans/{pid}/audit', status_code=202)
    async def sampling_audit(request: Request, pid: str):
        raw = await request.body()
        return await run(request, True, lambda db, p: (S.audit(db, p, pid), 202), 'verification.sampling_audit', raw)

    @app.post(API + '/verification/assurance/evaluate')
    async def assurance_evaluate(request: Request):
        raw = await request.body(); body = read_body(request, raw)
        def fn(db, p):
            from . import assurance, records
            from ..errors import ServiceError
            p.require('job:read')
            job = db.execute('SELECT * FROM jobs WHERE id=? AND workspace=?', (body.get('job_id'), p.workspace)).fetchone()
            if job is None:
                raise ServiceError('NOT_FOUND', 'job')
            contract = db.execute('SELECT * FROM contracts WHERE id=?', (job['contract_id'],)).fetchone()
            try:
                assurance.validate_requirement(body.get('requirement'), job['kind'])
            except assurance.AssuranceInvalid as exc:
                raise ServiceError('VALIDATION', {'code': exc.code, **exc.detail})
            sts = records.statements_for_job(db, job, contract, body.get('provider_execution', 'local_worker'))
            return assurance.preview(body['requirement'], sts, records.binding_for(job, contract))
        return await run(request, False, fn)

    @app.get(API + '/verification/assurance/claims')
    async def assurance_claims(request: Request):
        from . import assurance
        return await run(request, False, lambda db, p: {'claims': {k: {c: {'claim_type': v[0], 'coverage': v[1], 'comparison': v[2], 'implementation_independent': v[3]} for c, v in d.items()} for k, d in assurance.CLAIMS.items()},
                                                        'implications': {'%s->%s' % k: v for k, v in assurance.IMPLICATIONS.items()}, 'independence_dimensions': list(assurance.INDEPENDENCE_DIMENSIONS), 'requirement_schema': assurance.REQUIREMENT_SCHEMA, 'statement_schema': assurance.STATEMENT_SCHEMA})

    @app.get(API + '/verification/{vid}/assurance')
    async def verification_assurance(request: Request, vid: str):
        def fn(db, p):
            from . import records
            r = svc.verification.row(db, p, vid)
            job = db.execute('SELECT * FROM jobs WHERE id=?', (r['target_job_id'],)).fetchone(); contract = db.execute('SELECT * FROM contracts WHERE id=?', (job['contract_id'],)).fetchone()
            p.require('job:read')
            return next(s for s in records.statements_for_job(db, job, contract) if s['verification_id'] == vid)
        return await run(request, False, fn)

    # ---- portfolios ----------------------------------------------------------------------------------------------------------------------
    P = getattr(svc, 'portfolios', None)
    if P is not None:
        @app.post(API + '/portfolios', status_code=201)
        async def portfolio_create(request: Request):
            raw = await request.body(); body = read_body(request, raw)
            return await run(request, True, lambda db, p: (P.create(db, p, body), 201), 'portfolio.create', raw)

        @app.get(API + '/portfolios')
        async def portfolios(request: Request):
            return await run(request, False, lambda db, p: {'items': P.list(db, p), 'families': P.families(), 'components': P.components()})

        @app.post(API + '/portfolios/select')
        async def portfolio_select(request: Request):
            raw = await request.body(); body = read_body(request, raw)
            return await run(request, False, lambda db, p: P.select(db, p, body))

        @app.post(API + '/portfolios/campaigns', status_code=201)
        async def portfolio_campaign(request: Request):
            raw = await request.body(); body = read_body(request, raw)
            return await run(request, True, lambda db, p: (P.run_campaign(db, p, body), 201), 'portfolio.campaign', raw)

        @app.get(API + '/portfolios/campaigns')
        async def portfolio_campaigns(request: Request):
            return await run(request, False, lambda db, p: {'items': P.list_campaigns(db, p)})

        @app.get(API + '/portfolios/campaigns/{cid}')
        async def portfolio_campaign_view(request: Request, cid: str):
            return await run(request, False, lambda db, p: P.campaign_view(db, p, cid))

        @app.post(API + '/portfolios/fault-suite', status_code=201)
        async def portfolio_fault_suite(request: Request):
            raw = await request.body(); body = read_body(request, raw)
            return await run(request, True, lambda db, p: (P.create_fault_suite(db, p, body), 201), 'portfolio.suite', raw)

        @app.get(API + '/portfolios/{pid}')
        async def portfolio_view(request: Request, pid: str):
            return await run(request, False, lambda db, p: P.view(db, p, pid))
