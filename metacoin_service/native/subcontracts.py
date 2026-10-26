"""Native machine subcontracting (order 2026-10-23 group D). A primary machine that holds an awarded parent milestone may
subcontract that specific deliverable to another provider under a bounded, funded sub-contract. The sub-contract is a *child
mission* owned by the primary machine (so every existing rule applies: frozen terms, signed settlement for external custody,
ledger projection, bundles) and a durable record that binds the parent mission revision, the parent milestone, the deliverable
(template, inputs digest, permitted inputs), the acceptance policy, the deadline semantics and the funded amount.

Funding modes: `machine_earnings` (the primary's already settled available funds; receivables never count) or
`requester_prefund` (a separately authorized transfer from the requester to the primary, bound to a prefund obligation, with the
unused remainder returned to the requester). A proposal alone reserves nothing; funds are reserved only when the child award is
made. The parent's expected future payout is never collateral: a funding mode naming it is refused and an insufficient settled
balance is refused with the receivables listed (dependency cycle). Depth ≤ 2, fan-out ≤ 3 per parent milestone, duplicates
(same parent milestone, provider and deliverable digest) refused. The primary remains liable for its own parent obligation.
Private inputs: the subcontractor's award carries exactly the inputs the primary passed; it has no role on the parent award, so
the parent's private compartments stay hidden; mission views redact inputs for non-parties."""
import json
import secrets

from .. import history
from ..db import now
from ..errors import ServiceError
from . import protocol as P

MAX_DEPTH = 2
MAX_FANOUT = 3
FUNDING_MODES = ('machine_earnings', 'requester_prefund')
STATES = ('proposed', 'awaiting_prefund', 'funded', 'running', 'completed', 'failed', 'cancelled', 'returned')


class Subcontracts:
    def __init__(self, settings, services):
        self.settings, self.svc = settings, services

    # ---- creation ---------------------------------------------------------------------------------------------------------------------
    def create(self, db, principal, body):
        principal.require('work:request')
        if type(body) is not dict or set(body) - {'parent_mission', 'parent_milestone', 'provider_id', 'deliverable', 'acceptance', 'deadline_at', 'funded_amount', 'funding_mode', 'return_destination', 'title'}:
            raise ServiceError('VALIDATION', {'code': 'fields', 'allowed': ['parent_mission', 'parent_milestone', 'provider_id', 'deliverable {template, inputs, price, bps?, verifier?, verifier_pay_to?, verification_class?}', 'acceptance?', 'deadline_at?', 'funded_amount', 'funding_mode (machine_earnings|requester_prefund)', 'return_destination?', 'title?']})
        MS = self.svc.missions; N = self.svc.native
        parent = MS._row(db, principal.workspace, body.get('parent_mission')); pm = db.execute('SELECT * FROM mission_milestones WHERE mission_id=? AND key=?', (parent['id'], body.get('parent_milestone'))).fetchone()
        if pm is None or pm['kind'] != 'work':
            raise ServiceError('NOT_FOUND', 'parent work milestone')
        pb = json.loads(pm['binding_json'])
        if pm['state'] not in ('executing', 'verifying', 'awarded') or not pb.get('award_id'):
            raise ServiceError('CONFLICT', {'code': 'parent_milestone_state', 'state': pm['state'], 'note': 'only an awarded, unfinished parent milestone can be subcontracted'})
        aw = db.execute('SELECT * FROM work_awards WHERE id=?', (pb['award_id'],)).fetchone(); prow = db.execute('SELECT * FROM providers WHERE id=?', (aw['provider_id'],)).fetchone()
        if prow is None or prow['principal_id'] != principal.id:
            raise ServiceError('FORBIDDEN', {'code': 'not_the_primary', 'note': 'only the provider awarded the parent milestone may subcontract it'})
        primary = N.account_of(db, principal)
        sub = db.execute('SELECT * FROM providers WHERE id=?', (body.get('provider_id'),)).fetchone()
        if sub is None or sub['state'] != 'active':
            raise ServiceError('NOT_FOUND', 'subcontractor provider')
        if sub['principal_id'] == principal.id:
            raise ServiceError('VALIDATION', {'code': 'self_subcontract'})
        mode = body.get('funding_mode')
        if mode not in FUNDING_MODES:
            raise ServiceError('VALIDATION', {'code': 'circular_funding' if mode in ('parent_future_earnings', 'parent_expected_earnings', 'expected_earnings') else 'funding_mode', 'allowed': list(FUNDING_MODES), 'note': 'the parent\'s unpaid earnings are not collateral and not spendable: a sub-contract is funded from settled machine earnings or a separately authorized requester prefund'})
        amount = body.get('funded_amount')
        try:
            P.amount(amount)
        except P.ProtocolError as exc:
            raise ServiceError('VALIDATION', {'code': exc.code})
        d = body.get('deliverable') or {}
        if type(d) is not dict or type(d.get('template')) is not str or type(d.get('inputs')) is not dict or type(d.get('price')) is not int:
            raise ServiceError('VALIDATION', {'code': 'deliverable', 'required': ['template', 'inputs', 'price']})
        ceiling = d['price'] + d['price'] * d.get('bps', 0) // 10000 + d.get('verifier', 0)
        if ceiling > amount:
            raise ServiceError('VALIDATION', {'code': 'deliverable_exceeds_funding', 'ceiling': ceiling, 'funded_amount': amount})
        # depth, fan-out, duplicates
        depth = 1 + self._depth_of(db, parent['id'])
        if depth > MAX_DEPTH:
            raise ServiceError('CONFLICT', {'code': 'subcontract_depth', 'depth': depth, 'max': MAX_DEPTH})
        siblings = db.execute("SELECT * FROM mission_subcontracts WHERE parent_mission=? AND parent_milestone=? AND state NOT IN ('cancelled')", (parent['id'], pm['key'])).fetchall()
        if len(siblings) >= MAX_FANOUT:
            raise ServiceError('CONFLICT', {'code': 'subcontract_fanout', 'max': MAX_FANOUT})
        ddigest = P.digest({'template': d['template'], 'inputs': d['inputs'], 'price': d['price']})
        if any(s['provider_id'] == sub['id'] and s['deliverable_digest'] == ddigest for s in siblings):
            raise ServiceError('CONFLICT', {'code': 'duplicate_subcontract', 'note': 'the same deliverable is already subcontracted to this provider for this milestone (duplicate obligation refused)'})
        # funding: settled funds only
        from . import ledger
        dom = N.domain(db, principal.workspace)
        if mode == 'machine_earnings':
            avail = ledger.balance(db, principal.workspace, parent['asset'], dom['id'], primary['id'], 'available')
            receivable = db.execute("SELECT COALESCE(SUM(amount - settled_amount),0) FROM native_obligations WHERE workspace=? AND payee_account=? AND asset=? AND state IN ('open','partial')", (principal.workspace, primary['id'], parent['asset'])).fetchone()[0]
            if avail < amount:
                raise ServiceError('BUDGET_EXHAUSTED', {'code': 'insufficient_settled_funds', 'available': avail, 'funded_amount': amount, 'receivables_not_spendable': receivable, 'note': 'a sub-contract funded from machine earnings needs settled available funds; the parent\'s unpaid earnings (receivables) are not money — funding this sub-contract from them would be a cycle'})
        rd = body.get('return_destination') or (parent['owner_account'] if mode == 'requester_prefund' else primary['id'])
        # the child mission: one work milestone + its settlement, owned by the primary, ceiling-bounded, provider pinned
        spec = {'template': d['template'], 'inputs': d['inputs'], 'price': d['price'], 'bps': d.get('bps', 0), 'verifier': d.get('verifier', 0), 'verifier_pay_to': d.get('verifier_pay_to'), 'permitted_providers': [sub['id']], 'verification_class': d.get('verification_class', 'analytical')}
        graph = {'milestones': [{'key': 'deliver', 'kind': 'work', 'depends': [], 'spec': spec}, {'key': 'settle', 'kind': 'settle', 'depends': ['deliver'], 'spec': {'of': 'deliver'}}], 'notes': 'sub-contract of %s/%s (revision %d)' % (parent['id'], pm['key'], parent['frozen_revision'] or parent['revision'])}
        child = MS.create(db, principal, {'title': body.get('title') or ('subcontract of %s/%s' % (parent['id'], pm['key'])), 'graph': graph, 'asset': parent['asset'], 'policy': {'mission_ceiling': amount, 'permitted_providers': [sub['id']], 'permitted_recipients': None}})
        sid = 'sc_' + secrets.token_hex(6); t = now(); state = 'funded' if mode == 'machine_earnings' else 'awaiting_prefund'; prefund = None
        if mode == 'requester_prefund':
            ob = N.obligation_create(db, principal.workspace, 'subcontract_prefund', parent['owner_account'], primary['id'], parent['asset'], amount, 'subcontract', sid, {'partial_payments': False, 'return_destination': rd, 'note': 'separately authorized requester prefunding of a sub-contract; the unused remainder returns to the requester'})
            prefund = ob['id'] if isinstance(ob, dict) else ob
        acceptance = body.get('acceptance') or {'policy': 'frozen child terms; accepted by the primary on the independent verification verdict; partial completion only through separately priced milestones', 'verifier_paid_regardless_of_verdict': True}
        tl = [{'at': t, 'event': 'proposed', 'funded_amount': amount, 'mode': mode, 'state': state}]
        db.execute('INSERT INTO mission_subcontracts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (sid, principal.workspace, parent['id'], parent['frozen_revision'] or parent['revision'], pm['key'], principal.id, primary['id'], sub['id'], sub['principal_id'], child['id'], json.dumps({'template': d['template'], 'inputs_digest': P.digest(d['inputs']), 'permitted_inputs': sorted(d['inputs'].keys()), 'price': d['price'], 'ceiling': ceiling}), ddigest, json.dumps(acceptance), body.get('deadline_at'), amount, mode, prefund, rd, depth, state, json.dumps(tl), t, t))
        MS._event(db, parent['id'], principal.id, 'subcontract_proposed', {'subcontract': sid, 'milestone': pm['key'], 'provider': sub['id'], 'funded_amount': amount, 'mode': mode, 'child_mission': child['id']})
        history.record(db, principal.workspace, principal.id, 'native.obligation', 'subcontract', sid, {'parent': parent['id'], 'milestone': pm['key'], 'provider': sub['id'], 'amount': amount, 'mode': mode})
        MS.freeze(db, principal, child['id'])                      # the child revision is frozen by its owner (the primary) at proposal: frozen terms reserve nothing
        return self.view(db, principal, sid)

    def _depth_of(self, db, mid):
        row = db.execute('SELECT parent_mission FROM mission_subcontracts WHERE child_mission=?', (mid,)).fetchone()
        return 0 if row is None else 1 + self._depth_of(db, row['parent_mission'])

    def _row(self, db, workspace, sid):
        r = db.execute('SELECT * FROM mission_subcontracts WHERE id=? AND workspace=?', (sid, workspace)).fetchone()
        if r is None:
            raise ServiceError('NOT_FOUND', 'subcontract')
        return r

    def _tl(self, db, r, event, **kw):
        tl = json.loads(r['timeline_json']); tl.append(dict({'at': now(), 'event': event}, **kw)); db.execute('UPDATE mission_subcontracts SET timeline_json=?, updated_at=? WHERE id=?', (json.dumps(tl), now(), r['id']))

    # ---- prefund (requester) ------------------------------------------------------------------------------------------------------
    def prefund_prepare(self, db, principal, sid):
        """The requester's authorization: a transfer to the primary bound to the prefund obligation (signed when external)."""
        r = self._row(db, principal.workspace, sid); parent = self.svc.missions._row(db, principal.workspace, r['parent_mission'])
        if principal.id != parent['owner_principal']:
            raise ServiceError('FORBIDDEN', 'only the parent requester prefunds')
        if r['state'] != 'awaiting_prefund':
            raise ServiceError('CONFLICT', {'code': 'state', 'state': r['state']})
        return self.svc.native.transfer_prepare(db, principal, {'from_account': parent['owner_account'], 'to_account': r['primary_account'], 'amount': r['funded_amount'], 'asset': parent['asset'], 'obligation_id': r['prefund_obligation'], 'request_id': 'prefund-' + sid, 'purpose': 'sub-contract prefund ' + sid})

    def prefund_submit(self, db, principal, sid, body):
        """Either a signed message (external custody) or an ordinary transfer by a development-custody requester."""
        r = self._row(db, principal.workspace, sid); parent = self.svc.missions._row(db, principal.workspace, r['parent_mission'])
        if principal.id != parent['owner_principal']:
            raise ServiceError('FORBIDDEN', 'only the parent requester prefunds')
        if r['state'] != 'awaiting_prefund':
            return dict(self.view(db, principal, sid), replayed=True)
        if body and body.get('message'):
            it = self.svc.native.transfer_submit(db, principal, {'message': body['message'], 'signature_hex': body.get('signature_hex'), 'key_id': body.get('key_id')})
        else:
            it = self.svc.native.transfer(db, principal, {'from_account': parent['owner_account'], 'to_account': r['primary_account'], 'amount': r['funded_amount'], 'asset': parent['asset'], 'obligation_id': r['prefund_obligation'], 'request_id': 'prefund-' + sid, 'purpose': 'sub-contract prefund ' + sid})
        db.execute("UPDATE mission_subcontracts SET state='funded', updated_at=? WHERE id=?", (now(), sid)); self._tl(db, self._row(db, principal.workspace, sid), 'prefunded', intent=it.get('id'), amount=r['funded_amount'])
        return self.view(db, principal, sid)

    # ---- execution: the child mission's controller does the work ------------------------------------------------------------------
    def step(self, db, principal, sid):
        r = self._row(db, principal.workspace, sid)
        if r['state'] in ('awaiting_prefund', 'proposed'):
            raise ServiceError('CONFLICT', {'code': 'not_funded', 'state': r['state'], 'note': 'a proposal reserves nothing and authorizes no work; fund it first'})
        if r['deadline_at'] and now() > r['deadline_at'] and r['state'] in ('funded', 'running'):
            child = self.svc.missions._row(db, principal.workspace, r['child_mission'])
            if child['state'] in ('frozen', 'waiting') and not db.execute("SELECT 1 FROM mission_milestones WHERE mission_id=? AND state NOT IN ('pending','ready','waiting')", (child['id'],)).fetchone():
                db.execute("UPDATE mission_subcontracts SET state='failed', updated_at=? WHERE id=?", (now(), sid)); self._tl(db, r, 'deadline_passed_unstarted'); return self.view(db, principal, sid)
        out = self.svc.controller.step(db, principal, r['child_mission']); self._sync(db, principal, r); return dict(self.view(db, principal, sid), step=out)

    def _sync(self, db, principal, r):
        child = self.svc.missions._row(db, principal.workspace, r['child_mission']); states = {m['key']: m['state'] for m in db.execute('SELECT key, state FROM mission_milestones WHERE mission_id=?', (child['id'],)).fetchall()}
        new = r['state']
        if child['state'] == 'completed':
            new = 'completed'
        elif states.get('deliver') == 'failed':
            new = 'failed'
        elif child['state'] == 'running' and r['state'] == 'funded':
            new = 'running'
        if new != r['state']:
            db.execute('UPDATE mission_subcontracts SET state=?, updated_at=? WHERE id=?', (new, now(), r['id'])); self._tl(db, self._row(db, principal.workspace, r['id']), new)
            self.svc.missions._event(db, r['parent_mission'], principal.id, 'subcontract_' + new, {'subcontract': r['id'], 'milestone': r['parent_milestone'], 'note': 'the primary remains responsible for its own parent obligation; a supplier failure does not erase it'})

    def return_unused(self, db, principal, sid):
        """After completion or failure of a prefunded sub-contract: the primary returns prefund minus what the child spent."""
        r = self._row(db, principal.workspace, sid)
        if principal.id != r['primary_principal']:
            raise ServiceError('FORBIDDEN', 'the primary returns unused prefunding')
        if r['funding_mode'] != 'requester_prefund' or r['state'] not in ('completed', 'failed', 'cancelled'):
            raise ServiceError('CONFLICT', {'code': 'return_state', 'state': r['state'], 'mode': r['funding_mode']})
        led = self.svc.missions.budget_ledger(db, principal, r['child_mission']); unused = r['funded_amount'] - led['totals']['spent'] - led['totals']['reserved']
        if unused <= 0:
            db.execute("UPDATE mission_subcontracts SET state='returned', updated_at=? WHERE id=?", (now(), sid)); self._tl(db, r, 'nothing_to_return', spent=led['totals']['spent']); return self.view(db, principal, sid)
        parent = self.svc.missions._row(db, principal.workspace, r['parent_mission']); body = {'from_account': r['primary_account'], 'to_account': r['return_destination'], 'amount': unused, 'asset': parent['asset'], 'request_id': 'return-' + sid, 'purpose': 'return of unused sub-contract prefund ' + sid}
        prim = self.svc.native.account_row(db, principal.workspace, r['primary_account'])
        if (prim['custody'] or '').startswith('external-signer'):
            prep = self.svc.native.transfer_prepare(db, principal, body); ctx = {'kind': 'transfer', 'recipient': r['return_destination'], 'amount': unused, 'asset': parent['asset'], 'subcontract': sid, 'purpose': 'return_unused_prefund'}
            sr = self.svc.signing.create(db, principal.workspace, r['primary_account'], 'transfer', prep['message'], ctx, 'subcontract', sid + ':return')
            self._tl(db, r, 'return_awaiting_signature', amount=unused, request=sr['id']); return dict(self.view(db, principal, sid), awaiting_signature=sr['id'])
        it = self.svc.native.transfer(db, principal, body); db.execute("UPDATE mission_subcontracts SET state='returned', updated_at=? WHERE id=?", (now(), sid)); self._tl(db, self._row(db, principal.workspace, sid), 'returned', amount=unused, intent=it['id'])
        return self.view(db, principal, sid)

    def on_signed(self, db, principal, req, result):
        sid = req['ref_id'].split(':')[0]; r = self._row(db, principal.workspace, sid)
        db.execute("UPDATE mission_subcontracts SET state='returned', updated_at=? WHERE id=?", (now(), sid)); self._tl(db, r, 'returned', amount=result.get('amount_final'), intent=result.get('id'))

    def cancel(self, db, principal, sid):
        r = self._row(db, principal.workspace, sid)
        if principal.id != r['primary_principal']:
            raise ServiceError('FORBIDDEN', 'the primary cancels its proposal')
        if r['state'] not in ('proposed', 'awaiting_prefund', 'funded'):
            raise ServiceError('CONFLICT', {'code': 'cancel_state', 'state': r['state'], 'note': 'started work is settled or failed through the child mission, never erased'})
        if db.execute("SELECT 1 FROM mission_milestones WHERE mission_id=? AND state NOT IN ('pending','ready','waiting')", (r['child_mission'],)).fetchone():
            raise ServiceError('CONFLICT', {'code': 'child_started'})
        db.execute("UPDATE mission_subcontracts SET state='cancelled', updated_at=? WHERE id=?", (now(), sid)); db.execute("UPDATE mission_contracts SET state='cancelled', updated_at=? WHERE id=?", (now(), r['child_mission'])); self._tl(db, r, 'cancelled')
        return self.view(db, principal, sid)

    # ---- views ------------------------------------------------------------------------------------------------------------------------
    def view(self, db, principal, sid):
        principal.require('work:read'); r = self._row(db, principal.workspace, sid); led = self.svc.missions.budget_ledger(db, principal, r['child_mission'])
        child = self.svc.missions._row(db, principal.workspace, r['child_mission']); ob = db.execute('SELECT * FROM native_obligations WHERE id=?', (r['prefund_obligation'],)).fetchone() if r['prefund_obligation'] else None
        earned = db.execute("SELECT COALESCE(SUM(settled_amount),0) FROM native_obligations WHERE workspace=? AND kind='provider' AND ref_type='work_milestone' AND ref_id IN (SELECT id FROM work_milestones WHERE award_id IN (SELECT json_extract(binding_json,'$.award_id') FROM mission_milestones WHERE mission_id=?))", (principal.workspace, r['child_mission'])).fetchone()[0]
        disputed = db.execute("SELECT COUNT(*) FROM native_disputes WHERE workspace=? AND ref_id IN (SELECT id FROM native_intents WHERE obligation_ref IN (SELECT id FROM native_obligations WHERE ref_type='work_milestone' AND ref_id IN (SELECT id FROM work_milestones WHERE award_id IN (SELECT json_extract(binding_json,'$.award_id') FROM mission_milestones WHERE mission_id=?))))", (principal.workspace, r['child_mission'])).fetchone()[0] if db.execute("SELECT 1 FROM sqlite_master WHERE name='native_disputes'").fetchone() else 0
        tl = json.loads(r['timeline_json']); returned = sum(e.get('amount', 0) for e in tl if e['event'] == 'returned')
        party = principal.id in (r['primary_principal'], r['provider_principal']) or principal.can('native:admin') or principal.id == self.svc.missions._row(db, principal.workspace, r['parent_mission'])['owner_principal']
        dl = json.loads(r['deliverable_json'])
        return {'schema': 'metacoin-subcontract/v1', 'id': sid, 'parent': {'mission': r['parent_mission'], 'revision': r['parent_revision'], 'milestone': r['parent_milestone']}, 'primary': {'principal': r['primary_principal'], 'account': r['primary_account'], 'liability': 'the primary stays responsible for its parent obligation unless an authorized parent amendment says otherwise'},
                'provider_id': r['provider_id'], 'child_mission': r['child_mission'], 'child_state': child['state'], 'deliverable': dl if party else {'template': dl['template'], 'inputs_digest': dl['inputs_digest'], 'ceiling': dl['ceiling']}, 'acceptance': json.loads(r['acceptance_json']), 'deadline_at': r['deadline_at'], 'funded_amount': r['funded_amount'], 'funding_mode': r['funding_mode'],
                'prefund': {'obligation': ob['id'], 'state': ob['state'], 'settled': ob['settled_amount']} if ob else None, 'return_destination': r['return_destination'], 'depth': r['depth'], 'limits': {'max_depth': MAX_DEPTH, 'max_fanout': MAX_FANOUT}, 'state': r['state'],
                'timeline': {'reserved': led['totals']['reserved'], 'spent': led['totals']['spent'], 'earned_by_provider': earned, 'disputed': disputed, 'returned': returned, 'events': tl}, 'created_at': r['created_at']}

    def list(self, db, principal, parent=None):
        principal.require('work:read')
        rows = db.execute('SELECT id FROM mission_subcontracts WHERE workspace=?' + (' AND parent_mission=?' if parent else '') + ' ORDER BY created_at', (principal.workspace,) + ((parent,) if parent else ())).fetchall()
        return [self.view(db, principal, r['id']) for r in rows]
