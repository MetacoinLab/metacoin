"""HTTP routes of the native protocol under /api/v1/native/* (authentication, idempotency, strict JSON and error mapping are
the API's). Every route is a thin call into the service; public inspection (native:read) is separate from authority to spend
(native:transfer) and administration (native:admin)."""
from fastapi import Request


def mount(app, svc, run, read_body, API):
    N = svc.native

    @app.post(API + '/native/domain', status_code=201)
    async def native_domain_init(request: Request):
        raw = await request.body(); body = read_body(request, raw)
        return await run(request, True, lambda db, p: (N.init_domain(db, p, body), 201), 'native.domain.init', raw)

    @app.get(API + '/native/domain')
    async def native_domain(request: Request):
        return await run(request, False, lambda db, p: N.domain_view(db, p))

    @app.post(API + '/native/domain/connectivity')
    async def native_connectivity(request: Request):
        raw = await request.body(); body = read_body(request, raw)
        return await run(request, True, lambda db, p: N.set_connectivity(db, p, body.get('state')), 'native.domain.connectivity', raw)

    @app.post(API + '/native/actors', status_code=201)
    async def native_actor_create(request: Request):
        raw = await request.body(); body = read_body(request, raw)
        return await run(request, True, lambda db, p: (N.create_actor(db, p, body), 201), 'native.actor.create', raw)

    @app.get(API + '/native/accounts')
    async def native_accounts(request: Request):
        return await run(request, False, lambda db, p: {'items': N.accounts(db, p)})

    @app.get(API + '/native/accounts/me')
    async def native_account_me(request: Request):
        return await run(request, True, lambda db, p: N.account_view(db, p, N.account_of(db, p)['id'], request.query_params.get('asset')))

    @app.get(API + '/native/accounts/{aid}')
    async def native_account(request: Request, aid: str):
        return await run(request, False, lambda db, p: N.account_view(db, p, aid, request.query_params.get('asset')))

    @app.post(API + '/native/fixtures/fund', status_code=201)
    async def native_fixture_fund(request: Request):
        raw = await request.body(); body = read_body(request, raw)
        return await run(request, True, lambda db, p: (N.fixture_fund(db, p, body), 201), 'native.fixture.fund', raw)

    @app.post(API + '/native/transfers', status_code=201)
    async def native_transfer(request: Request):
        raw = await request.body(); body = read_body(request, raw)
        return await run(request, True, lambda db, p: (N.transfer(db, p, body), 201), 'native.transfer', raw)

    @app.post(API + '/native/refunds', status_code=201)
    async def native_refund(request: Request):
        raw = await request.body(); body = read_body(request, raw)
        return await run(request, True, lambda db, p: (N.refund(db, p, body), 201), 'native.refund', raw)

    @app.get(API + '/native/intents')
    async def native_intents(request: Request):
        return await run(request, False, lambda db, p: {'items': N.intents(db, p, request.query_params.get('account'))})

    @app.get(API + '/native/intents/{iid}')
    async def native_intent(request: Request, iid: str):
        return await run(request, False, lambda db, p: N.intent_view(db, p, iid))

    @app.get(API + '/native/obligations')
    async def native_obligations(request: Request):
        return await run(request, False, lambda db, p: {'items': N.obligations(db, p, request.query_params.get('account'))})

    @app.get(API + '/native/obligations/{oid}')
    async def native_obligation(request: Request, oid: str):
        return await run(request, False, lambda db, p: N.obligation_view(db, p, oid))

    @app.get(API + '/native/audit')
    async def native_audit(request: Request):
        return await run(request, False, lambda db, p: N.audit(db, p, request.query_params.get('asset')))

    @app.get(API + '/native/spec')
    async def native_spec(request: Request):
        from . import protocol
        return await run(request, False, lambda db, p: protocol.spec())
