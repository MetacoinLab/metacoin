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

    # ---- providers, requests, offers, awards (Group B) ----------------------------------------------------------------
    P, B = svc.economy.providers, svc.economy.board

    @app.post(API + '/work/providers', status_code=201)
    async def work_provider_register(request: Request):
        raw = await request.body(); body = read_body(request, raw)
        return await run(request, True, lambda db, p: (P.register(db, p, body), 201), 'work.provider.register', raw)

    @app.get(API + '/work/providers')
    async def work_providers(request: Request):
        return await run(request, False, lambda db, p: {'items': P.list(db, p)})

    @app.get(API + '/work/providers/{prid}')
    async def work_provider(request: Request, prid: str):
        return await run(request, False, lambda db, p: P.view(db, p, P.row(db, p, prid)))

    @app.post(API + '/work/providers/{prid}/revise')
    async def work_provider_revise(request: Request, prid: str):
        raw = await request.body(); body = read_body(request, raw)
        return await run(request, True, lambda db, p: P.revise(db, p, prid, body), 'work.provider.revise', raw)

    @app.post(API + '/work/requests', status_code=201)
    async def work_request_create(request: Request):
        raw = await request.body(); body = read_body(request, raw)
        return await run(request, True, lambda db, p: (B.create_request(db, p, body), 201), 'work.request.create', raw)

    @app.get(API + '/work/requests')
    async def work_requests(request: Request, state: str = None):
        return await run(request, False, lambda db, p: {'items': B.list_requests(db, p, state)})

    @app.get(API + '/work/requests/{rid}')
    async def work_request(request: Request, rid: str):
        return await run(request, False, lambda db, p: B.request_view(db, p, B.request_row(db, p, rid)))

    @app.post(API + '/work/requests/{rid}/{action}')
    async def work_request_action(request: Request, rid: str, action: str):
        raw = await request.body(); body = read_body(request, raw)
        if action == 'offers':
            return await run(request, True, lambda db, p: (B.submit_offer(db, p, rid, body), 201), 'work.offer.submit', raw)
        if action == 'award':
            return await run(request, True, lambda db, p: (B.award(db, p, rid, body), 201), 'work.award', raw)
        if action == 'eligibility':
            return await run(request, False, lambda db, p: B.check_eligibility(db, p, rid, body))
        return await run(request, True, lambda db, p: B.request_state(db, p, rid, action, body), 'work.request.' + action, raw)

    @app.get(API + '/work/requests/{rid}/compare')
    async def work_request_compare(request: Request, rid: str):
        return await run(request, False, lambda db, p: B.compare_offers(db, p, rid))

    @app.post(API + '/work/offers/{oid}/{action}')
    async def work_offer_action(request: Request, oid: str, action: str):
        raw = await request.body(); body = read_body(request, raw)
        return await run(request, True, lambda db, p: B.offer_state(db, p, oid, action, body), 'work.offer.' + action, raw)

    @app.get(API + '/work/awards')
    async def work_awards(request: Request, state: str = None):
        return await run(request, False, lambda db, p: {'items': B.list_awards(db, p, state)})

    @app.get(API + '/work/awards/{aid}')
    async def work_award(request: Request, aid: str):
        return await run(request, False, lambda db, p: B.award_view(db, p, B.award_row(db, p, aid)))

    @app.post(API + '/work/awards/{aid}/ack')
    async def work_award_ack(request: Request, aid: str):
        raw = await request.body()
        return await run(request, True, lambda db, p: B.acknowledge(db, p, aid), 'work.award.ack', raw)

    # ---- evidence, decisions, entitlements, disputes, bundles (Group C) -----------------------------------------------------
    E = svc.economy.evidence

    @app.get(API + '/work/awards/{aid}/receipts')
    async def work_receipts(request: Request, aid: str):
        return await run(request, False, lambda db, p: {'items': E.list_receipts(db, p, aid)})

    @app.get(API + '/work/receipts/{rid}')
    async def work_receipt(request: Request, rid: str):
        return await run(request, False, lambda db, p: E.receipt_view(db, p, rid))

    @app.post(API + '/work/receipts/{rid}/verify')
    async def work_receipt_verify(request: Request, rid: str):
        raw = await request.body(); body = read_body(request, raw)
        return await run(request, False, lambda db, p: E.verify_receipt(db, p, rid, body))

    @app.get(API + '/work/awards/{aid}/milestones/{key}/decisions')
    async def work_decisions(request: Request, aid: str, key: str):
        return await run(request, False, lambda db, p: {'items': E.decisions(db, p, aid, key)})

    @app.get(API + '/work/awards/{aid}/milestones/{key}/bundle')
    async def work_bundle(request: Request, aid: str, key: str, scope: str = 'restricted'):
        from fastapi.responses import Response as _R
        def fn(db, p):
            data, manifest = E.bundle(db, p, aid, key, scope)
            return data
        from starlette.concurrency import run_in_threadpool
        def sync():
            with svc.db.tx() as db:
                from ..api import principal_of
                p = principal_of(request, db, False)
                return fn(db, p)
        data = await run_in_threadpool(sync)
        return _R(content=data, media_type='application/zip', headers={'Content-Disposition': 'attachment; filename="work-bundle-%s-%s.zip"' % (aid, key)})

    @app.post(API + '/work/awards/{aid}/milestones/{key}/{action}')
    async def work_milestone_action(request: Request, aid: str, key: str, action: str):
        raw = await request.body(); body = read_body(request, raw)
        if action == 'evaluate':
            return await run(request, True, lambda db, p: E.evaluate(db, p, aid, key))
        if action == 'decide':
            return await run(request, True, lambda db, p: E.decide(db, p, aid, key, body), 'work.decide', raw)
        if action == 'verify':
            return await run(request, True, lambda db, p: (E.verify(db, p, aid, key, body), 202), 'work.verify', raw)
        if action == 'dispute':
            return await run(request, True, lambda db, p: (E.open_dispute(db, p, aid, key, body), 201), 'work.dispute.open', raw)
        if action == 'delegate':
            return await run(request, True, lambda db, p: (E.delegate(db, p, aid, key, body), 201), 'work.delegate', raw)
        AC = svc.economy.access
        if action == 'package':
            return await run(request, True, lambda db, p: (AC.encrypted_package(db, p, aid, key, body), 201), 'work.package', raw)
        if action == 'projection':
            return await run(request, True, lambda db, p: AC.projection(db, p, aid, key, body, sign=bool(body.get('sign'))))
        if action == 'anchor-candidate':
            return await run(request, True, lambda db, p: AC.anchor_candidate(db, p, aid, key, body))
        from ..errors import ServiceError
        raise ServiceError('NOT_FOUND', 'milestone action')

    @app.post(API + '/work/awards/{aid}/reassign')
    async def work_reassign(request: Request, aid: str):
        raw = await request.body(); body = read_body(request, raw)
        return await run(request, True, lambda db, p: E.reassign(db, p, aid, body), 'work.reassign', raw)

    @app.get(API + '/work/entitlements/{eid}')
    async def work_entitlement(request: Request, eid: str):
        return await run(request, False, lambda db, p: E.entitlement_view(db, p, eid))

    @app.get(API + '/work/decisions/{did}')
    async def work_decision(request: Request, did: str):
        return await run(request, False, lambda db, p: E.decision_view(db, p, did))

    @app.get(API + '/work/disputes')
    async def work_disputes(request: Request, award_id: str = None):
        return await run(request, False, lambda db, p: {'items': E.list_disputes(db, p, award_id)})

    @app.get(API + '/work/disputes/{did}')
    async def work_dispute(request: Request, did: str):
        return await run(request, False, lambda db, p: E.dispute_view(db, p, did))

    @app.post(API + '/work/disputes/{did}/{action}')
    async def work_dispute_action(request: Request, did: str, action: str):
        raw = await request.body(); body = read_body(request, raw)
        return await run(request, True, lambda db, p: E.dispute_action(db, p, did, action, body), 'work.dispute.' + action, raw)

    # ---- money: intents, settlement, refunds, journal, treasury (Group D) ----------------------------------------------------
    M, TR = svc.economy.money, svc.economy.treasury

    @app.get(API + '/work/rails')
    async def work_rails(request: Request):
        return await run(request, False, lambda db, p: M.rails(db, p))

    @app.post(API + '/work/entitlements/{eid}/{action}')
    async def work_entitlement_action(request: Request, eid: str, action: str):
        raw = await request.body(); body = read_body(request, raw)
        if action == 'prepare':
            return await run(request, True, lambda db, p: (M.prepare(db, p, eid, body), 201), 'work.pay.prepare', raw)
        if action == 'refund':
            return await run(request, True, lambda db, p: (M.refund(db, p, eid, body), 201), 'work.refund', raw)
        if action == 'credit':
            return await run(request, True, lambda db, p: (M.credit(db, p, eid, body), 201), 'work.credit', raw)
        from ..errors import ServiceError
        raise ServiceError('NOT_FOUND', 'entitlement action')

    @app.get(API + '/work/intents')
    async def work_intents(request: Request, state: str = None):
        return await run(request, False, lambda db, p: {'items': M.list_intents(db, p, state)})

    @app.get(API + '/work/intents/{iid}')
    async def work_intent(request: Request, iid: str):
        return await run(request, False, lambda db, p: M.intent_view(db, p, iid))

    @app.post(API + '/work/intents/{iid}/{action}')
    async def work_intent_action(request: Request, iid: str, action: str):
        raw = await request.body(); body = read_body(request, raw)
        if action == 'authorize':
            return await run(request, True, lambda db, p: M.authorize(db, p, iid), 'work.pay.authorize', raw)
        if action == 'submit':
            return await run(request, True, lambda db, p: M.submit(db, p, iid, body), 'work.pay.submit', raw)
        if action == 'reconcile':
            return await run(request, True, lambda db, p: M.reconcile_refund(db, p, iid) if M.intent_row(db, p, iid)['kind'] == 'refund' else M.reconcile(db, p, iid), 'work.pay.reconcile', raw)
        from ..errors import ServiceError
        raise ServiceError('NOT_FOUND', 'intent action')

    @app.post(API + '/work/awards/{aid}/close')
    async def work_award_close(request: Request, aid: str):
        raw = await request.body()
        return await run(request, True, lambda db, p: M.close_award(db, p, aid), 'work.award.close', raw)

    @app.get(API + '/work/journal')
    async def work_journal(request: Request):
        from . import journal as journal_mod
        def fn(db, p):
            p.require('budget:read')
            return {'entries': journal_mod.entries(db, p.workspace), 'balances': journal_mod.scope_balances(db, p.workspace)}
        return await run(request, False, fn)

    @app.post(API + '/work/journal/replay')
    async def work_journal_replay(request: Request):
        from . import journal as journal_mod
        def fn(db, p):
            p.require('budget:read')
            return journal_mod.replay(db, p.workspace)
        return await run(request, False, fn)

    @app.get(API + '/work/exposure')
    async def work_exposure(request: Request):
        return await run(request, False, lambda db, p: M.exposure(db, p))

    @app.get(API + '/work/treasury')
    async def work_treasury(request: Request, asset: str = 'local-chain-token'):
        return await run(request, False, lambda db, p: TR.view(db, p, asset))

    @app.post(API + '/work/treasury/allocate', status_code=201)
    async def work_treasury_allocate(request: Request):
        raw = await request.body(); body = read_body(request, raw)
        return await run(request, True, lambda db, p: (TR.allocate(db, p, body), 201), 'work.treasury.allocate', raw)

    # ---- audit access, compartments, packages, retention, projections (Group E) ---------------------------------------------
    AC, MS = svc.economy.access, svc.economy.missions

    @app.get(API + '/work/awards/{aid}/compartments')
    async def work_compartments(request: Request, aid: str, milestone: str = None):
        return await run(request, False, lambda db, p: AC.compartments(db, p, aid, milestone))

    @app.get(API + '/work/awards/{aid}/artifacts/{artifact_id}')
    async def work_read_artifact(request: Request, aid: str, artifact_id: str):
        return await run(request, True, lambda db, p: AC.read_artifact(db, p, aid, artifact_id))

    @app.post(API + '/work/awards/{aid}/audit-grants', status_code=201)
    async def work_grant_create(request: Request, aid: str):
        raw = await request.body(); body = read_body(request, raw)
        return await run(request, True, lambda db, p: (AC.create_grant(db, p, aid, body), 201), 'work.audit_grant', raw)

    @app.get(API + '/work/audit-grants')
    async def work_grants(request: Request, award_id: str = None):
        return await run(request, False, lambda db, p: {'items': AC.list_grants(db, p, award_id)})

    @app.get(API + '/work/audit-grants/{gid}')
    async def work_grant(request: Request, gid: str):
        return await run(request, False, lambda db, p: AC.grant_view(db, p, gid))

    @app.post(API + '/work/audit-grants/{gid}/{action}')
    async def work_grant_action(request: Request, gid: str, action: str):
        raw = await request.body()
        if action == 'use':
            return await run(request, True, lambda db, p: AC.use_grant(db, p, gid))
        if action == 'revoke':
            return await run(request, True, lambda db, p: AC.revoke_grant(db, p, gid), 'work.audit_grant.revoke', raw)
        from ..errors import ServiceError
        raise ServiceError('NOT_FOUND', 'grant action')

    @app.get(API + '/work/awards/{aid}/milestones/{key}/retention')
    async def work_retention(request: Request, aid: str, key: str):
        return await run(request, False, lambda db, p: AC.retention(db, p, aid, key))

    @app.post(API + '/work/awards/{aid}/milestones/{key}/evidence/delete')
    async def work_delete_evidence(request: Request, aid: str, key: str):
        raw = await request.body(); body = read_body(request, raw)
        return await run(request, True, lambda db, p: AC.delete_evidence(db, p, aid, key, body), 'work.evidence.delete', raw)

    # ---- missions, contributions, resource probe, observations (Group F) ---------------------------------------------------------
    @app.post(API + '/work/missions/import', status_code=201)
    async def work_mission_import(request: Request):
        raw = await request.body(); body = read_body(request, raw)
        return await run(request, True, lambda db, p: (MS.import_mission(db, p, body), 201), 'work.mission.import', raw)

    @app.get(API + '/work/missions')
    async def work_missions(request: Request):
        return await run(request, False, lambda db, p: {'items': MS.list_portfolios(db, p)})

    @app.get(API + '/work/missions/{pid}')
    async def work_mission(request: Request, pid: str):
        return await run(request, False, lambda db, p: MS.portfolio_view(db, p, pid))

    @app.post(API + '/work/missions/{pid}/bottlenecks/{task}/draft', status_code=201)
    async def work_mission_draft(request: Request, pid: str, task: str):
        raw = await request.body(); body = read_body(request, raw)
        return await run(request, True, lambda db, p: (MS.draft_from_bottleneck(db, p, pid, task, body), 201), 'work.mission.draft', raw)

    @app.post(API + '/work/missions/{pid}/link')
    async def work_mission_link(request: Request, pid: str):
        raw = await request.body(); body = read_body(request, raw)
        return await run(request, True, lambda db, p: MS.link_request(db, p, pid, body), 'work.mission.link', raw)

    @app.get(API + '/work/resource-evidence/probe')
    async def work_resource_probe(request: Request):
        return await run(request, False, lambda db, p: MS.resource_probe(db, p))

    @app.post(API + '/work/observations', status_code=201)
    async def work_observation(request: Request):
        raw = await request.body(); body = read_body(request, raw)
        return await run(request, True, lambda db, p: (MS.ingest_observation(db, p, body), 201), 'work.observation', raw)

    @app.get(API + '/work/observations/{oid}')
    async def work_observation_view(request: Request, oid: str):
        return await run(request, False, lambda db, p: MS.observation_view(db, p, oid))

    # ---- notifications, measurements (§65, §67) --------------------------------------------------------------------------------
    from . import ops as economy_ops

    @app.get(API + '/work/notifications')
    async def work_notifications(request: Request, all: str = None):
        def fn(db, p):
            svc.economy.tick(db); economy_ops.notify(db)
            return {'items': economy_ops.notifications(db, p, include_dismissed=bool(all))}
        return await run(request, True, fn)

    @app.post(API + '/work/notifications/{nid}/dismiss')
    async def work_notification_dismiss(request: Request, nid: str):
        raw = await request.body()
        return await run(request, True, lambda db, p: economy_ops.dismiss(db, p, nid), 'work.notification.dismiss', raw)

    @app.get(API + '/work/measurements')
    async def work_measurements(request: Request):
        return await run(request, False, lambda db, p: economy_ops.measurements(db, p))

    @app.get(API + '/work/status')
    async def work_status(request: Request):
        return await run(request, False, lambda db, p: (p.require('work:read') and None) or {'counts': economy_ops.counts(db, p.workspace), 'waiting_reasons': economy_ops.waiting_reasons(db, p.workspace)})

    @app.get(API + '/work/keys')
    async def work_keys(request: Request):
        return await run(request, True, lambda db, p: (p.require('work:read') and None) or AC.trust_history(db, p))

    @app.post(API + '/work/keys/rotate')
    async def work_keys_rotate(request: Request):
        raw = await request.body(); body = read_body(request, raw)
        return await run(request, True, lambda db, p: AC.rotate_key(db, p, body), 'work.keys.rotate', raw)

    @app.get(API + '/work/contracts/{contract_id}/upgrade-preview')
    async def work_upgrade_preview(request: Request, contract_id: str):
        return await run(request, False, lambda db, p: T.upgrade_preview(db, p, contract_id))
