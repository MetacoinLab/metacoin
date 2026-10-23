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

    # ---- inter-domain: peers, allocations, transport, rotation, recovery (order §7–§9, §16) ----------------------------------
    @app.post(API + '/native/peers', status_code=201)
    async def native_peer_pin(request: Request):
        raw = await request.body(); body = read_body(request, raw)
        return await run(request, True, lambda db, p: (N.pin_peer(db, p, body), 201), 'native.peer.pin', raw)

    @app.get(API + '/native/peers/{pid}')
    async def native_peer(request: Request, pid: str):
        return await run(request, False, lambda db, p: N.peer_view(db, p, pid))

    @app.post(API + '/native/allocations/export', status_code=201)
    async def native_export(request: Request):
        raw = await request.body(); body = read_body(request, raw)
        return await run(request, True, lambda db, p: (N.export_create(db, p, body), 201), 'native.allocation.export', raw)

    @app.post(API + '/native/allocations/recover')
    async def native_recover_exports(request: Request):
        raw = await request.body(); read_body(request, raw)
        return await run(request, True, lambda db, p: N.recover_exports(db, p), 'native.allocation.recover', raw)

    @app.post(API + '/native/allocations/{aid}/resume')
    async def native_export_resume(request: Request, aid: str):
        raw = await request.body(); read_body(request, raw)
        return await run(request, True, lambda db, p: N.export_resume(db, p, aid), 'native.allocation.resume', raw)

    @app.post(API + '/native/allocations/{aid}/return')
    async def native_return(request: Request, aid: str):
        raw = await request.body(); read_body(request, raw)
        return await run(request, True, lambda db, p: N.return_request(db, p, {'allocation_id': aid}), 'native.allocation.return', raw)

    @app.get(API + '/native/allocations')
    async def native_allocations(request: Request):
        return await run(request, False, lambda db, p: N.allocations(db, p, request.query_params.get('direction'), request.query_params.get('state')))

    @app.get(API + '/native/allocations/{aid}')
    async def native_allocation(request: Request, aid: str):
        return await run(request, False, lambda db, p: N.allocation_view(db, p, aid))

    @app.get(API + '/native/transport/outbox')
    async def native_outbox(request: Request):
        q = request.query_params
        return await run(request, False, lambda db, p: N.outbox(db, p, q.get('receiver'), q.get('transport', 'online'), q.get('all') == '1'))

    @app.post(API + '/native/transport/outbox/mark')
    async def native_outbox_mark(request: Request):
        raw = await request.body(); body = read_body(request, raw)
        return await run(request, True, lambda db, p: N.outbox_mark(db, p, body), 'native.transport.mark', raw)

    @app.post(API + '/native/transport/inbox')
    async def native_inbox(request: Request):
        raw = await request.body(); body = read_body(request, raw)
        return await run(request, True, lambda db, p: N.inbox(db, p, body, request.query_params.get('transport', 'online'), len(raw)), 'native.transport.inbox', raw)

    @app.post(API + '/native/transport/gc')
    async def native_transport_gc(request: Request):
        raw = await request.body(); read_body(request, raw)
        return await run(request, True, lambda db, p: N.transport_gc(db, p), 'native.transport.gc', raw)

    @app.post(API + '/native/transport/observation', status_code=201)
    async def native_observation(request: Request):
        raw = await request.body(); body = read_body(request, raw)
        return await run(request, True, lambda db, p: (N.observation(db, p, body.get('peer')), 201), 'native.transport.observation', raw)

    @app.post(API + '/native/domain/rotate')
    async def native_rotate(request: Request):
        raw = await request.body(); body = read_body(request, raw)
        return await run(request, True, lambda db, p: N.rotate_authority(db, p, body), 'native.domain.rotate', raw)

    @app.get(API + '/native/export')
    async def native_export(request: Request):
        return await run(request, False, lambda db, p: N.export_record(db, p))

    @app.get(API + '/native/recovery')
    async def native_recovery(request: Request):
        return await run(request, False, lambda db, p: N.recovery_view(db, p))

    @app.post(API + '/native/recovery/reconcile')
    async def native_reconcile(request: Request):
        raw = await request.body(); body = read_body(request, raw)
        return await run(request, True, lambda db, p: N.recovery_reconcile(db, p, body), 'native.recovery.reconcile', raw)

    # ---- disputes (§16) ---------------------------------------------------------------------------------------------------------
    @app.post(API + '/native/disputes', status_code=201)
    async def native_dispute_open(request: Request):
        raw = await request.body(); body = read_body(request, raw)
        return await run(request, True, lambda db, p: (N.dispute_open(db, p, body), 201), 'native.dispute.open', raw)

    @app.get(API + '/native/disputes')
    async def native_disputes(request: Request):
        return await run(request, False, lambda db, p: {'items': N.disputes(db, p)})

    @app.get(API + '/native/disputes/{did}')
    async def native_dispute(request: Request, did: str):
        return await run(request, False, lambda db, p: N.dispute_view(db, p, did))

    @app.post(API + '/native/disputes/{did}/resolve')
    async def native_dispute_resolve(request: Request, did: str):
        raw = await request.body(); body = read_body(request, raw)
        return await run(request, True, lambda db, p: N.dispute_resolve(db, p, did, body), 'native.dispute.resolve', raw)

    @app.post(API + '/native/transfers/simulate')
    async def native_transfer_simulate(request: Request):
        raw = await request.body(); body = read_body(request, raw)
        return await run(request, False, lambda db, p: N.transfer_simulate(db, p, body))

    # ---- procurement (§12) ---------------------------------------------------------------------------------------------------
    @app.post(API + '/native/procurement/plan')
    async def native_procurement_plan(request: Request):
        from . import procurement
        from ..db import now as _now
        raw = await request.body(); body = read_body(request, raw)
        def fn(db, p):
            p.require('work:read')
            return procurement.plan(body.get('envelope'), body.get('plans'), body.get('policy', 'cheapest_eligible'), body.get('now', _now()), bool(body.get('allow_reduced')), body.get('arrival_order'))
        return await run(request, False, fn)

    @app.get(API + '/native/procurement/benchmark')
    async def native_procurement_benchmark(request: Request):
        from . import procurement
        q = request.query_params
        return await run(request, False, lambda db, p: procurement.benchmark(int(q.get('seed', '1')), min(int(q.get('missions', '200')), 2000)))

    # ---- external signer custody (order 2026-10-23 group A) ------------------------------------------------------------------
    @app.post(API + '/native/actors/enroll', status_code=201)
    async def native_enroll_external(request: Request):
        raw = await request.body(); body = read_body(request, raw)
        return await run(request, True, lambda db, p: (N.enroll_external(db, p, body), 201), 'native.actor.enroll', raw)

    @app.get(API + '/native/accounts/{aid}/keys')
    async def native_keys(request: Request, aid: str):
        return await run(request, False, lambda db, p: N.keys_view(db, p, aid))

    @app.post(API + '/native/accounts/{aid}/keys', status_code=201)
    async def native_register_key(request: Request, aid: str):
        raw = await request.body(); body = read_body(request, raw)
        return await run(request, True, lambda db, p: (N.register_key(db, p, aid, body), 201), 'native.key.register', raw)

    @app.post(API + '/native/accounts/{aid}/keys/{kid}/revoke')
    async def native_revoke_key(request: Request, aid: str, kid: str):
        raw = await request.body(); read_body(request, raw)
        return await run(request, True, lambda db, p: N.revoke_key(db, p, aid, kid), 'native.key.revoke', raw)

    @app.post(API + '/native/transfers/prepare')
    async def native_transfer_prepare(request: Request):
        raw = await request.body(); body = read_body(request, raw)
        return await run(request, False, lambda db, p: N.transfer_prepare(db, p, body))

    @app.post(API + '/native/transfers/submit', status_code=201)
    async def native_transfer_submit(request: Request):
        raw = await request.body(); body = read_body(request, raw)
        return await run(request, True, lambda db, p: (N.transfer_submit(db, p, body), 201), 'native.transfer.submit', raw)

    # ---- claim lineage and accounting (group F) --------------------------------------------------------------------------------
    @app.get(API + '/native/accounts/{aid}/lineage')
    async def native_lineage(request: Request, aid: str):
        return await run(request, False, lambda db, p: N.lineage_view(db, p, aid))

    @app.get(API + '/native/accounting')
    async def native_accounting(request: Request):
        return await run(request, False, lambda db, p: N.accounting_view(db, p, request.query_params.get('asset')))

    @app.post(API + '/native/checkpoint')
    async def native_checkpoint(request: Request):
        raw = await request.body(); read_body(request, raw)
        def fn(db, p):
            p.require('native:admin'); return N.checkpoint(db, p.workspace)
        return await run(request, True, fn, 'native.checkpoint', raw)

    # ---- resource reservation market (order 2026-10-23 group B) -----------------------------------------------------------------
    M = svc.market

    @app.get(API + '/resources')
    async def resources_market(request: Request):
        return await run(request, False, lambda db, p: M.market_view(db, p))

    @app.post(API + '/resources/offers', status_code=201)
    async def resources_publish(request: Request):
        raw = await request.body(); body = read_body(request, raw)
        return await run(request, True, lambda db, p: (M.publish(db, p, body), 201), 'resources.offer', raw)

    @app.get(API + '/resources/offers')
    async def resources_offers(request: Request):
        return await run(request, False, lambda db, p: {'items': M.offers(db, p, request.query_params.get('kind'))})

    @app.get(API + '/resources/offers/{oid}')
    async def resources_offer(request: Request, oid: str):
        return await run(request, False, lambda db, p: M.offer_view(db, p, oid))

    @app.post(API + '/resources/offers/{oid}/amend')
    async def resources_amend(request: Request, oid: str):
        raw = await request.body(); body = read_body(request, raw)
        return await run(request, True, lambda db, p: M.amend_offer(db, p, oid, body), 'resources.offer.amend', raw)

    @app.post(API + '/resources/quote')
    async def resources_quote(request: Request):
        raw = await request.body(); body = read_body(request, raw)
        return await run(request, False, lambda db, p: M.quote(db, p, body))

    @app.post(API + '/resources/reservations', status_code=201)
    async def resources_reserve(request: Request):
        raw = await request.body(); body = read_body(request, raw)
        return await run(request, True, lambda db, p: (M.reserve(db, p, body), 201), 'resources.reserve', raw)

    @app.get(API + '/resources/reservations')
    async def resources_reservations(request: Request):
        return await run(request, False, lambda db, p: {'items': M.reservations(db, p, request.query_params.get('account'))})

    @app.get(API + '/resources/reservations/{rid}')
    async def resources_reservation(request: Request, rid: str):
        return await run(request, False, lambda db, p: M.reservation_view(db, p, rid))

    @app.post(API + '/resources/reservations/{rid}/{action}')
    async def resources_reservation_action(request: Request, rid: str, action: str):
        raw = await request.body()
        if action == 'storage':
            data = raw
            return await run(request, True, lambda db, p: M.storage_put(db, p, rid, data), 'resources.storage', raw)
        body = read_body(request, raw)
        fn = {'activate': lambda db, p: M.activate(db, p, rid), 'deliver': lambda db, p: M.deliver(db, p, rid, body), 'settle': lambda db, p: M.settle(db, p, rid), 'cancel': lambda db, p: M.cancel(db, p, rid, body)}.get(action)
        if fn is None:
            from ..errors import ServiceError
            raise ServiceError('NOT_FOUND', 'reservation action')
        return await run(request, True, fn, 'resources.' + action, raw)
