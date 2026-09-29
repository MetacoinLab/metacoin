"""HTTP routes of the work economy under /api/v1/work/…, mounted from api.create_app with the same run()/read_body helpers
(authentication, idempotency, JSON limits and error mapping are the API's)."""
from fastapi import Request


def mount(app, svc, run, read_body, API):
    T = svc.economy.terms

    # ---- work terms (Group A) ----------------------------------------------------------------------------------------
    @app.post(API + '/work/terms', status_code=201)
    async def work_terms_create(request: Request):
        raw = await request.body(); body = read_body(request, raw)
        return await run(request, True, lambda db, p: (T.create(db, p, body), 201), 'work.terms.create', raw)

    @app.post(API + '/work/terms/validate')
    async def work_terms_validate(request: Request):
        raw = await request.body(); body = read_body(request, raw)
        return await run(request, False, lambda db, p: T.validate_body(db, p, body))

    @app.get(API + '/work/terms')
    async def work_terms_list(request: Request, state: str = None):
        return await run(request, False, lambda db, p: {'items': T.list(db, p, state)})

    @app.get(API + '/work/terms/templates')
    async def work_terms_templates(request: Request):
        from . import terms as terms_mod
        return await run(request, False, lambda db, p: {'templates': terms_mod.TEMPLATES, 'deliverable_types': {k: {'kinds': list(v['kinds']), 'automatic': list(v['automatic']), 'review_required': v['review_required'], 'claim': v['claim']} for k, v in terms_mod.DELIVERABLE_TYPES.items()},
                                                          'predicate_types': list(terms_mod.PREDICATE_TYPES), 'assets': terms_mod.ASSETS, 'policy_schema': terms_mod.POLICY_SCHEMA, 'schema': terms_mod.SCHEMA})

    @app.get(API + '/work/terms/{tid}')
    async def work_terms_get(request: Request, tid: str):
        return await run(request, False, lambda db, p: T.view(db, p, T.row(db, p, tid)))

    @app.get(API + '/work/terms/{tid}/inspect')
    async def work_terms_inspect(request: Request, tid: str):
        return await run(request, False, lambda db, p: T.inspect(db, p, tid))

    @app.get(API + '/work/terms/{tid}/compare/{other}')
    async def work_terms_compare(request: Request, tid: str, other: str):
        return await run(request, False, lambda db, p: T.compare(db, p, tid, other))

    @app.post(API + '/work/terms/{tid}')
    async def work_terms_update(request: Request, tid: str):
        raw = await request.body(); body = read_body(request, raw)
        return await run(request, True, lambda db, p: T.update(db, p, tid, body), 'work.terms.update', raw)

    @app.post(API + '/work/terms/{tid}/freeze')
    async def work_terms_freeze(request: Request, tid: str):
        raw = await request.body(); body = read_body(request, raw)
        return await run(request, True, lambda db, p: T.freeze_amendment(db, p, tid, body), 'work.terms.freeze', raw)

    @app.post(API + '/work/terms/{tid}/amend', status_code=201)
    async def work_terms_amend(request: Request, tid: str):
        raw = await request.body(); body = read_body(request, raw)
        return await run(request, True, lambda db, p: (T.amend(db, p, tid, body), 201), 'work.terms.amend', raw)

    @app.post(API + '/work/terms/{tid}/withdraw')
    async def work_terms_withdraw(request: Request, tid: str):
        raw = await request.body()
        return await run(request, True, lambda db, p: T.withdraw(db, p, tid), 'work.terms.withdraw', raw)

    @app.post(API + '/work/terms/{tid}/evaluate')
    async def work_terms_evaluate(request: Request, tid: str):
        raw = await request.body(); body = read_body(request, raw)
        return await run(request, True, lambda db, p: T.evaluate(db, p, tid, body))

    @app.get(API + '/work/contracts/{contract_id}/upgrade-preview')
    async def work_upgrade_preview(request: Request, contract_id: str):
        return await run(request, False, lambda db, p: T.upgrade_preview(db, p, contract_id))
