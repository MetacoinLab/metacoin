"""Operator screen (order 2026-10-23 §12): one view answering, from recorded state only, what each machine owns and owes,
which resources are committed, what evidence is missing, what remains possible during a blackout (and what is disabled, with
the recorded reason), what is pending across domains, and what the domain's recovery state is. Nothing here is a second
balance: every figure is a projection of the journal, obligations, reservations, allocations and mission records."""
import json

from ..errors import ServiceError


class OperatorScreen:
    def __init__(self, svc):
        self.svc = svc

    def view(self, db, principal, asset=None):
        principal.require('native:read'); principal.require('work:read')
        N, M, MS, CT = self.svc.native, self.svc.market, self.svc.missions, self.svc.controller
        d = N.domain_view(db, principal); asset = asset or d['default_asset']; ws = principal.workspace
        accounts = [a for a in N.accounts(db, principal) if a.get('kind') == 'actor']
        reservations = M.reservations(db, principal); open_res = [r for r in reservations if r['state'] in ('reserved', 'active', 'delivered')]
        machines = []
        for a in accounts:
            aid = a['id']
            owes = db.execute("SELECT COALESCE(SUM(amount - settled_amount),0), COUNT(*) FROM native_obligations WHERE workspace=? AND payer_account=? AND asset=? AND state IN ('open','partial')", (ws, aid, asset)).fetchone()
            owed = db.execute("SELECT COALESCE(SUM(amount - settled_amount),0), COUNT(*) FROM native_obligations WHERE workspace=? AND payee_account=? AND asset=? AND state IN ('open','partial')", (ws, aid, asset)).fetchone()
            bought = [r for r in open_res if r['buyer_account'] == aid]; sold = [r for r in open_res if r['provider_account'] == aid]
            machines.append({'account': aid, 'label': a.get('label'), 'custody': a.get('custody'), 'owns': {k: a.get(k, 0) for k in ('available', 'reserved', 'committed_outgoing', 'in_transit', 'disputed')}, 'pending_claims': a.get('pending_claims', 0),
                             'owes': {'outstanding': owes[0], 'obligations': owes[1]}, 'owed': {'outstanding': owed[0], 'obligations': owed[1], 'note': 'earned, unsettled: not spendable'},
                             'resources_committed': {'bought': [{'reservation': r['id'], 'kind': r['kind'], 'quantity': r['quantity'], 'state': r['state'], 'price': r['price']} for r in bought], 'sold': [{'reservation': r['id'], 'kind': r['kind'], 'quantity': r['quantity'], 'state': r['state']} for r in sold]}})
        missing, possible, disabled, missions_out = [], [], [], []
        for m in MS.list(db, principal):
            if m['state'] in ('completed', 'cancelled', 'draft'):
                continue
            v = MS.view(db, principal, m['id'])
            for x in v['milestones']:
                if x['state'] in ('executing', 'verifying') or (x['state'] == 'awarded'):
                    what = {'work': 'execution result and verification' if x['state'] != 'verifying' else 'verification record', 'resource': 'activation and measured delivery by the provider', 'evidence_gate': 'analysis / acquisition result', 'deliver': 'relay evidence'}.get(x['kind'], 'result')
                    missing.append({'mission': m['id'], 'milestone': x['key'], 'kind': x['kind'], 'state': x['state'], 'missing': what, 'binding': {k: x['binding'].get(k) for k in ('award_id', 'reservation_id', 'job_id') if x['binding'].get(k)}})
            try:
                ex = CT.explain(db, principal, m['id'], observe=True)
            except ServiceError as exc:
                ex = {'context': {}, 'actions': [], 'stop_reason': exc.code}
            for a in ex['actions']:
                row = {'mission': m['id'], 'action': a['action'], 'milestone': a['milestone'], 'amount': a.get('amount'), 'reason': a.get('reason')}
                (possible if a['permitted'] else disabled).append(row)
            missions_out.append({'id': m['id'], 'title': m['title'], 'state': m['state'], 'stop_reason': ex.get('stop_reason'), 'spendable_settled': ex['context'].get('spendable_settled'), 'reserved_exposure': ex['context'].get('reserved_exposure')})
        connectivity = d.get('connectivity'); blackout = connectivity == 'cut'
        allocs = N.allocations(db, principal)['items']; pending = [{'allocation': a['id'], 'direction': a['direction'], 'state': a['state'], 'exposed_state': a.get('exposed_state'), 'amount': a['amount'], 'peer': a['destination_domain'] if a['direction'] == 'export' else a['source_domain']} for a in allocs if a.get('exposed_state') not in ('reconciled',)]
        acc = N.accounting_view(db, principal, asset); rec = N.recovery_view(db, principal)
        outbox = db.execute("SELECT COUNT(*) FROM native_envelopes WHERE workspace=? AND direction='out' AND state IN ('queued')", (ws,)).fetchone()[0]
        return {'schema': 'metacoin-operator-screen/v1', 'domain': d['id'], 'asset': asset, 'connectivity': connectivity, 'recovery_state': d.get('recovery_state'),
                'machines': machines, 'missing_evidence': missing, 'missions': missions_out,
                'blackout': {'active': blackout, 'possible_now': possible, 'disabled_now': disabled, 'rule': 'during a cut link only local actions within already funded reservations and preallocated authority proceed; relay, cross-domain and witness-dependent actions are refused with link_cut'},
                'pending_cross_domain': {'allocations': pending, 'queued_outbox_messages': outbox, 'accounting': {k: acc[k] for k in ('source_backing_locked', 'claims_active', 'retirements_pending', 'returns_completed')}, 'checks': acc['checks']},
                'recovery': {'state': rec['recovery_state'], 'rollback_detection': rec['rollback_detection'], 'witness': rec.get('witness'), 'quarantined_peers': rec['quarantined_peers'], 'quarantined_allocations': rec['quarantined_allocations']},
                'audit': {k: v for k, v in N.audit(db, principal, asset).items() if k in ('total_held', 'fixture_funding', 'backing_locked_for_exports', 'claims_backed_elsewhere', 'conserved')},
                'source': 'projection of journal, obligations, reservations, allocations, missions and controller validation; no second balance'}
