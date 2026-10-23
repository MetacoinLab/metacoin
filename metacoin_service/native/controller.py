"""Bounded mission controller (order 2026-10-23 group D): economic autonomy inside a funded envelope. For a frozen mission it
enumerates explicit permissible actions over immutable provider offers, validates each against recorded constraints
(spendable settled funds minus the minimum cash reserve, reserved exposure, per-action and aggregate ceilings, permitted
recipients, offer freshness, evidence requirements, declared energy reserve, contact windows, connectivity, grant state),
performs at most one action per step through the ordinary service entry points, and publishes the chosen action together
with every rejected alternative and its reason. A language model (or anyone) may PROPOSE an action; the same deterministic
validator decides. Earn-then-buy: a provider-actor milestone spends only the provider's settled balance. During a blackout
only locally available authority is used; with no safe action the mission enters a visible wait; a revoked grant stops new
authorized work while incurred obligations remain visible and payable under their terms."""
import json

from .. import history
from ..db import now
from ..errors import ServiceError
from . import ledger
from . import mission_contracts as MC

ACTIONS = ('open_request', 'award', 'await_execution', 'verify', 'decide', 'pay', 'reserve', 'await_signature', 'release_missed_reservation', 'activate_reservation', 'record_delivery', 'settle_reservation', 'analyze', 'buy_evidence', 'await_delivery', 'skip_gate', 'relay', 'wait', 'stop')


class Rejected(Exception):
    def __init__(self, code, **detail):
        super().__init__(code); self.code = code; self.detail = detail


class Controller:
    def __init__(self, settings, services):
        self.settings, self.svc = settings, services

    @property
    def M(self):
        return self.svc.missions

    # ---- public entry points -----------------------------------------------------------------------------------------------------
    def step(self, db, principal, mid, proposal=None):
        """One controller step as `principal` (owner acts on requester milestones; the awarded provider acts on provider milestones)."""
        principal.require('work:read'); m = self.M._row(db, principal.workspace, mid)
        ctx = self._context(db, principal, m)
        candidates = self._candidates(db, principal, m, ctx)
        chosen, rejected = None, []
        if proposal is not None:
            cand = next((c for c in candidates if c['action'] == proposal.get('action') and c.get('milestone') == proposal.get('milestone') and (proposal.get('offer_id') is None or c.get('offer_id') == proposal.get('offer_id'))), None)
            if cand is None:
                verdict = {'proposal': proposal, 'accepted': False, 'reason': 'not_a_permissible_action', 'note': 'the proposal names no enumerated action for this mission in its current state', 'permissible': [{'action': c['action'], 'milestone': c.get('milestone'), 'offer_id': c.get('offer_id')} for c in candidates]}
                self.M._event(db, mid, principal.id, 'proposal_rejected', verdict); return {'mission_id': mid, 'performed': None, 'proposal': verdict, 'rejected': [], 'state': m['state']}
            ok, why = self._validate(db, principal, m, ctx, cand)
            if not ok:
                verdict = {'proposal': proposal, 'accepted': False, 'reason': why}; self.M._event(db, mid, principal.id, 'proposal_rejected', verdict); return {'mission_id': mid, 'performed': None, 'proposal': verdict, 'rejected': [], 'state': m['state']}
            chosen = cand
        else:
            for c in candidates:
                ok, why = self._validate(db, principal, m, ctx, c)
                if ok and chosen is None:
                    chosen = c
                elif not ok:
                    rejected.append({'action': c['action'], 'milestone': c.get('milestone'), 'offer_id': c.get('offer_id'), 'reason': why})
        if chosen is None:
            state = 'waiting' if ctx['stop_reason'] is None else 'stopped'
            if m['state'] in ('frozen', 'waiting', 'running') and ctx['stop_reason'] is None:
                db.execute("UPDATE mission_contracts SET state='waiting', updated_at=? WHERE id=?", (now(), mid))
            out = {'mission_id': mid, 'performed': None, 'state': state, 'wait_reason': ctx['stop_reason'] or self._wait_reason(rejected, ctx), 'rejected': rejected, 'context': ctx['public']}
            self.M._event(db, mid, principal.id, 'wait', {'reason': out['wait_reason'], 'rejected': rejected}); return out
        result = self._perform(db, principal, m, ctx, chosen)
        if m['state'] in ('frozen', 'waiting'):
            db.execute("UPDATE mission_contracts SET state='running', updated_at=? WHERE id=?", (now(), mid))
        self.M._event(db, mid, principal.id, 'action', {'chosen': {k: chosen.get(k) for k in ('action', 'milestone', 'offer_id', 'amount')}, 'result': result, 'rejected': rejected, 'context': ctx['public']})
        self._finish_if_done(db, mid)
        return {'mission_id': mid, 'performed': {k: chosen.get(k) for k in ('action', 'milestone', 'offer_id', 'amount', 'reason')}, 'result': result, 'rejected': rejected, 'state': self.M._row(db, principal.workspace, mid)['state'], 'context': ctx['public']}

    def run(self, db, principal, mid, max_steps=12):
        """Bounded loop: steps until nothing is performable (wait/stop/done)."""
        log = []
        for i in range(max_steps):
            r = self.step(db, principal, mid); log.append({k: r.get(k) for k in ('performed', 'state', 'wait_reason')})
            if r.get('performed') is None:
                break
        return {'mission_id': mid, 'steps': log, 'state': self.M._row(db, principal.workspace, mid)['state']}

    def propose(self, db, principal, mid, body):
        """A proposed action (for example from a language model) goes through the same validator; it can never authorize money by itself."""
        if type(body) is not dict or type(body.get('action')) is not str:
            raise ServiceError('VALIDATION', {'code': 'proposal', 'allowed': ['action', 'milestone?', 'offer_id?']})
        return self.step(db, principal, mid, proposal=body)

    def explain(self, db, principal, mid, observe=False):
        """What the controller sees: permissible actions and why each would be accepted or rejected now (no action performed).
        observe=True lets an administrator see the owner's view (read only; stepping still requires the owner or provider)."""
        principal.require('work:read'); m = self.M._row(db, principal.workspace, mid); ctx = self._context(db, principal, m, observe=observe)
        out = []
        for c in self._candidates(db, principal, m, ctx):
            ok, why = self._validate(db, principal, m, ctx, c); out.append({'action': c['action'], 'milestone': c.get('milestone'), 'offer_id': c.get('offer_id'), 'amount': c.get('amount'), 'permitted': ok, 'reason': why})
        return {'mission_id': mid, 'state': m['state'], 'context': ctx['public'], 'actions': out, 'stop_reason': ctx['stop_reason']}

    # ---- context: recorded constraints only --------------------------------------------------------------------------------------
    def _context(self, db, principal, m, observe=False):
        N = self.svc.native; d = N.domain(db, principal.workspace); pol = json.loads(m['policy_json'])
        owner_acct = N.account_row(db, principal.workspace, m['owner_account'])
        role = 'requester' if principal.id == m['owner_principal'] or (observe and principal.can('native:admin')) else 'provider'
        acting = owner_acct if role == 'requester' else N.account_of(db, principal, principal.workspace, create=False)
        if acting is None:
            raise ServiceError('FORBIDDEN', {'code': 'no_account', 'note': 'the caller has no native account on this domain'})
        cats = ledger.categories(db, principal.workspace, m['asset'], d['id'], acting['id'])
        earned = db.execute("SELECT COALESCE(SUM(amount - settled_amount),0) FROM native_obligations WHERE workspace=? AND payee_account=? AND asset=? AND state IN ('open','partial')", (principal.workspace, acting['id'], m['asset'])).fetchone()[0]
        spendable = max(cats['available'] - pol.get('min_cash_reserve', 0), 0)
        grant = None; stop = None
        gid = m['grant_id'] or (getattr(principal, 'scope', None) or {}).get('grant_id')
        if gid:
            g = db.execute('SELECT * FROM policy_grants WHERE id=?', (gid,)).fetchone()
            if g is None or g['state'] != 'active' or g['expires_at'] <= now():
                stop = 'grant_revoked' if (g is None or g['state'] in ('revoked', 'stopped')) else 'grant_expired'
            else:
                gp = json.loads(g['policy_json']); gc = json.loads(g['counters_json'])
                grant = {'id': gid, 'remaining_amount': gp['ceilings']['total_amount'] - gc['amount_reserved'], 'per_action': gp['ceilings']['per_action_amount']}
        if d['recovery_state'] != 'normal':
            stop = stop or 'domain_quarantined'
        if m['state'] == 'paused':
            stop = stop or 'paused: ' + (m['pause_reason'] or '')
        if m['state'] in ('completed', 'stopped', 'draft'):
            stop = stop or 'mission_' + m['state']
        spent = self.M.budget_ledger(db, principal, m['id'])['totals'] if principal.can('native:read') else {'reserved': 0, 'spent': 0}
        energy_used = sum(json.loads(x['spec_json']).get('quantity', 0) * (json.loads(x['result_json'] or '{}').get('energy_mwh_per_unit') or 0) for x in db.execute("SELECT spec_json, result_json FROM mission_milestones WHERE mission_id=? AND kind='resource' AND state NOT IN ('pending','ready','cancelled','skipped','failed')", (m['id'],)).fetchall())
        pub = {'role': role, 'acting_account': acting['id'], 'spendable_settled': spendable, 'available': cats['available'], 'reserved_exposure': cats['reserved'], 'earned_unsettled_not_spendable': earned, 'min_cash_reserve': pol.get('min_cash_reserve', 0), 'per_action_ceiling': pol.get('per_action_ceiling'), 'mission_ceiling': pol.get('mission_ceiling'),
               'mission_reserved': spent['reserved'], 'mission_spent': spent['spent'], 'grant': grant, 'custody': acting['custody'], 'connectivity': d['connectivity'], 'recovery_state': d['recovery_state'], 'energy': {'budget_mwh': pol.get('energy_budget_mwh'), 'reserve_mwh': pol.get('energy_reserve_mwh', 0), 'committed_mwh': energy_used}, 'now': now()}
        return {'domain': d, 'policy': pol, 'acting': acting, 'owner': owner_acct, 'role': role, 'cats': cats, 'spendable': spendable, 'grant': grant, 'stop_reason': stop, 'public': pub, 'energy_used': energy_used}

    def _wait_reason(self, rejected, ctx):
        if ctx['domain']['connectivity'] == 'cut' and any('link_cut' in (r['reason'] or '') for r in rejected):
            return 'blackout: safe local actions exhausted; waiting for reconnection within preallocated authority'
        if rejected:
            return 'no permitted action: ' + '; '.join(sorted({r['reason'] for r in rejected}))[:400]
        return 'nothing to do: waiting for providers, execution or dependencies'

    # ---- candidates -----------------------------------------------------------------------------------------------------------------
    PRIORITY = {'pay': 0, 'settle_reservation': 0, 'release_missed_reservation': 0, 'decide': 1, 'verify': 1, 'award': 2, 'skip_gate': 4, 'await_execution': 5, 'await_delivery': 5, 'await_signature': 5, 'wait': 6}

    def _candidates(self, db, principal, m, ctx):
        """Enumerated in graph order, then stably sorted so incurred obligations settle and decisions resolve before new spending."""
        return sorted(self._enumerate(db, principal, m, ctx), key=lambda c: self.PRIORITY.get(c['action'], 3))

    def _enumerate(self, db, principal, m, ctx):
        out = []
        for ms in db.execute("SELECT * FROM mission_milestones WHERE mission_id=? AND state NOT IN ('pending','settled','failed','skipped','cancelled','accepted') ORDER BY created_at", (m['id'],)).fetchall():
            if ms['actor'] != ctx['role']:
                continue
            sp = json.loads(ms['spec_json']); b = json.loads(ms['binding_json']); k = ms['kind']
            pend = self._pending_signatures(db, b)
            if pend is not None:
                out.append({'action': 'await_signature', 'milestone': ms['key'], 'signing_requests': pend, 'reason': 'awaiting the account holder\'s signer: %d request(s) pending; no fallback custody' % len(pend)}); continue
            if k == 'work':
                if ms['state'] in ('ready', 'waiting') and not b.get('request_id'):
                    out.append({'action': 'open_request', 'milestone': ms['key'], 'amount': MC.ceiling_of(sp, 'work'), 'spec': sp})
                elif ms['state'] == 'opened':
                    for o in self._eligible_offers(db, principal, b['request_id']):
                        if sp.get('permitted_providers') and o['provider_id'] not in sp['permitted_providers']:
                            continue
                        out.append({'action': 'award', 'milestone': ms['key'], 'offer_id': o['offer_id'], 'amount': o['price_amount'] + o['price_amount'] * sp.get('bps', 0) // 10000 + sp.get('verifier', 0), 'provider_id': o['provider_id'], 'request_id': b['request_id'], 'spec': sp})
                elif ms['state'] in ('executing', 'verifying') and b.get('award_id'):
                    aw = db.execute('SELECT * FROM work_awards WHERE id=?', (b['award_id'],)).fetchone(); v = self.svc.economy.board.award_view(db, principal, aw); dims = v['milestones'][0]['dimensions']
                    if dims['execution'] not in ('completed', 'failed'):
                        out.append({'action': 'await_execution', 'milestone': ms['key'], 'award_id': aw['id']})
                    elif dims['execution'] == 'failed':
                        out.append({'action': 'decide', 'milestone': ms['key'], 'award_id': aw['id'], 'decision': 'reject', 'spec': sp})
                    elif dims['acceptance'] != 'accepted' and not v['milestones'][0].get('verification_requested') and not b.get('verified'):
                        out.append({'action': 'verify', 'milestone': ms['key'], 'award_id': aw['id'], 'spec': sp})
                    elif dims['acceptance'] != 'accepted':
                        out.append({'action': 'decide', 'milestone': ms['key'], 'award_id': aw['id'], 'decision': 'accept', 'spec': sp})
            elif k == 'settle':
                tgt = db.execute('SELECT * FROM mission_milestones WHERE mission_id=? AND key=?', (m['id'], sp['of'])).fetchone(); tb = json.loads(tgt['binding_json']) if tgt else {}
                if ms['state'] in ('ready', 'waiting') and tb.get('award_id'):
                    aw = db.execute('SELECT * FROM work_awards WHERE id=?', (tb['award_id'],)).fetchone(); v = self.svc.economy.board.award_view(db, principal, aw)
                    payable = [e for e in v['entitlements'] if e['state'] == 'payable']
                    if payable:
                        out.append({'action': 'pay', 'milestone': ms['key'], 'award_id': aw['id'], 'entitlements': [e['id'] for e in payable], 'amount': sum(e['amount'] for e in payable)})
                    elif v['state'] == 'closed' or all(e['state'] == 'paid' for e in v['entitlements']) and v['entitlements']:
                        out.append({'action': 'pay', 'milestone': ms['key'], 'award_id': aw['id'], 'entitlements': [], 'amount': 0, 'already': True})
                    else:
                        out.append({'action': 'await_execution', 'milestone': ms['key'], 'award_id': aw['id']})
            elif k == 'resource':
                if ms['state'] in ('ready', 'waiting') and not b.get('reservation_id'):
                    planned = b.get('planned_offer_id'); cands = []
                    for o in self.svc.market.offers(db, principal, sp['offer_kind']):
                        if sp.get('offer_id') and o['id'] != sp['offer_id']:
                            continue
                        if sp.get('provider_account') and o['provider_account'] != sp['provider_account']:
                            continue
                        cands.append({'action': 'reserve', 'milestone': ms['key'], 'offer_id': o['id'], 'offer_version': o['version'], 'amount': sp['quantity'] * o['price_per_unit'], 'quantity': sp['quantity'], 'offer': o, 'spec': sp, 'planned': o['id'] == planned})
                    out.extend(sorted(cands, key=lambda c: (not c['planned'], c['amount'])))      # the applied plan's offer first, then cheapest; each still revalidated
                    if not any(c['milestone'] == ms['key'] for c in out):
                        out.append({'action': 'wait', 'milestone': ms['key'], 'reason': 'no_offer_for_' + sp['offer_kind']})
                elif b.get('reservation_id'):
                    r = self.svc.market.reservation_view(db, principal, b['reservation_id'])
                    if r['state'] == 'reserved' and r['window']['end'] <= now():
                        out.append({'action': 'release_missed_reservation', 'milestone': ms['key'], 'reservation_id': r['id'], 'reason': 'window_missed: the provider never activated before the window closed; cancel before activation returns funds and capacity in full, then replan'})
                    elif r['state'] == 'reserved':
                        out.append({'action': 'await_delivery', 'milestone': ms['key'], 'reservation_id': r['id'], 'note': 'the provider activates and records measured delivery'})
                    elif r['state'] == 'active':
                        out.append({'action': 'await_delivery', 'milestone': ms['key'], 'reservation_id': r['id']})
                    elif r['state'] == 'delivered':
                        out.append({'action': 'settle_reservation', 'milestone': ms['key'], 'reservation_id': r['id'], 'amount': r['delivered_quantity'] * r['price'] // max(r['quantity'], 1)})
            elif k == 'evidence_gate':
                if ms['state'] in ('ready', 'waiting') and not b.get('analysis_job_id'):
                    out.append({'action': 'analyze', 'milestone': ms['key'], 'spec': sp})
                elif b.get('analysis_job_id') and not b.get('plan_id'):
                    out.append({'action': 'buy_evidence', 'milestone': ms['key'], 'spec': sp, 'amount': sp['budget']})
                    out.append({'action': 'skip_gate', 'milestone': ms['key'], 'spec': sp})
                elif b.get('acquisition_award_id'):
                    out.append({'action': 'await_execution', 'milestone': ms['key'], 'award_id': b['acquisition_award_id'], 'gate': True})
            elif k == 'deliver':
                if ms['state'] in ('ready', 'waiting'):
                    out.append({'action': 'relay', 'milestone': ms['key'], 'spec': sp})
        return out

    def _eligible_offers(self, db, principal, rid):
        try:
            cmp = self.svc.economy.board.compare_offers(db, principal, rid)
        except ServiceError:
            return []
        return sorted(cmp.get('eligible', []), key=lambda o: (o['price_amount'], o['offer_id']))

    # ---- the deterministic validator ------------------------------------------------------------------------------------------------
    def _validate(self, db, principal, m, ctx, c):
        pol = ctx['policy']; a = c['action']
        if ctx['stop_reason'] and a not in ('pay',):                                               # incurred obligations stay payable; nothing new is authorized
            return False, ctx['stop_reason']
        if ctx['stop_reason'] and a == 'pay' and ctx['stop_reason'].startswith('domain_quarantined'):
            return False, ctx['stop_reason']
        if a == 'await_signature':
            return True, c['reason']
        amt = c.get('amount') or 0
        if a in ('open_request', 'award', 'reserve', 'buy_evidence') and amt:
            if ctx['role'] == 'provider' and ctx['acting']['id'] == ctx['owner']['id']:
                return False, 'actor_mismatch'
            if amt > ctx['spendable']:
                return False, 'insufficient_liquidity: spendable settled %d (available %d minus minimum reserve %d) < %d; receivables %d are not spendable' % (ctx['spendable'], ctx['cats']['available'], pol.get('min_cash_reserve', 0), amt, ctx['public']['earned_unsettled_not_spendable'])
            if pol.get('per_action_ceiling') is not None and amt > pol['per_action_ceiling']:
                return False, 'per_action_ceiling: %d > %d' % (amt, pol['per_action_ceiling'])
            if pol.get('mission_ceiling') is not None and ctx['public']['mission_reserved'] + ctx['public']['mission_spent'] + amt > pol['mission_ceiling']:
                return False, 'mission_ceiling: reserved %d + spent %d + %d > %d' % (ctx['public']['mission_reserved'], ctx['public']['mission_spent'], amt, pol['mission_ceiling'])
            if ctx['grant'] is not None and (amt > ctx['grant']['per_action'] or amt > ctx['grant']['remaining_amount']):
                return False, 'grant_envelope: %d exceeds per-action %d or remaining %d' % (amt, ctx['grant']['per_action'], ctx['grant']['remaining_amount'])
        if a == 'award':
            if pol.get('permitted_recipients') is not None and c.get('provider_id') not in pol['permitted_recipients']:
                return False, 'recipient_not_permitted: %s' % c.get('provider_id')
        if a == 'reserve':
            o = c['offer']
            if pol.get('permitted_recipients') is not None and o['provider_account'] not in pol['permitted_recipients']:
                return False, 'recipient_not_permitted: %s' % o['provider_account']
            if o['price_per_unit'] > c['spec']['max_price_per_unit']:
                return False, 'offer_price_above_milestone_max: %d > %d' % (o['price_per_unit'], c['spec']['max_price_per_unit'])
            if o['available'] < c['quantity']:
                return False, 'capacity_insufficient: %d available < %d' % (o['available'], c['quantity'])
            if o['window']['end'] <= now():
                return False, 'window_closed'
            horizon = pol.get('horizon_end')
            if horizon and o['window']['start'] > horizon:
                return False, 'contact_window_outside_horizon'
            need = c['quantity'] * (o.get('energy_mwh_per_unit') or 0); budget = pol.get('energy_budget_mwh')
            if budget is not None and ctx['energy_used'] + need > budget - pol.get('energy_reserve_mwh', 0):
                return False, 'insufficient_energy_reserve: committed %d + %d > budget %d - reserve %d (declared planning constraint)' % (ctx['energy_used'], need, budget, pol.get('energy_reserve_mwh', 0))
            if o.get('_stale'):
                return False, 'stale_offer'
        if a in ('verify',) and ctx['domain']['connectivity'] == 'cut' and c.get('spec', {}).get('remote_verifier'):
            return False, 'link_cut: remote verification unavailable during blackout'
        if a in ('relay',) and ctx['domain']['connectivity'] == 'cut':
            return False, 'link_cut: the contact link is down; delivery waits'
        if a == 'buy_evidence' and ctx['domain']['connectivity'] == 'cut':
            return False, 'link_cut: evidence purchase needs the supplier link'
        if a == 'await_execution' or a == 'await_delivery':
            return False, 'waiting_for_' + ('execution' if a == 'await_execution' else 'delivery')
        if a == 'wait':
            return False, c.get('reason', 'nothing_available')
        if a == 'skip_gate':
            return False, 'gate_requires_resolved_decision_or_explicit_skip'
        if a == 'pay' and c.get('already'):
            return True, 'already_paid'
        return True, 'ok'

    # ---- performing an action through the ordinary entry points ----------------------------------------------------------------------
    def _perform(self, db, principal, m, ctx, c):
        E = self.svc.economy; a = c['action']; ms = db.execute('SELECT * FROM mission_milestones WHERE mission_id=? AND key=?', (m['id'], c['milestone'])).fetchone(); sp = json.loads(ms['spec_json']); b = json.loads(ms['binding_json'])
        if a == 'open_request':
            inputs = sp.get('inputs')
            if inputs is None:
                src = db.execute('SELECT * FROM mission_milestones WHERE mission_id=? AND key=?', (m['id'], sp['inputs_from'])).fetchone(); inputs = (json.loads(src['result_json'] or '{}')).get('product_inputs') or {}
            ceiling = MC.ceiling_of(sp, 'work')
            t = E.terms.create(db, principal, {'template': sp['template'], 'ceiling': ceiling, 'amount': sp['price'], 'asset': m['asset']})
            pay = dict(t['terms']['payment'], scheme=sp.get('scheme', 'exact'), fee_policy={'schema': 'metacoin-fee-policy/v1', 'treasury_bps': sp.get('bps', 0), 'rounding': 'floor_fee_remainder_to_provider'}, funding='requester', verifier_compensation=sp.get('verifier', 0))
            if sp.get('verifier', 0):
                pay['verifier_pay_to'] = sp['verifier_pay_to']
            el = dict(t['terms']['eligibility'], payment_schemes=[sp.get('scheme', 'exact')])
            if sp.get('permitted_providers'):
                el['providers'] = sp['permitted_providers']
            upd = {'payment': pay, 'eligibility': el}
            if sp.get('resource_envelope'):
                upd['resource_envelope'] = sp['resource_envelope']
            E.terms.update(db, principal, t['id'], {'terms': upd}); f = E.terms.freeze(db, principal, t['id'], {'inputs': inputs})
            r = E.board.create_request(db, principal, {'terms_id': t['id']}); E.board.request_state(db, principal, r['id'], 'open', {})
            self.M.set_milestone(db, m['id'], ms['key'], 'opened', {'terms_id': t['id'], 'terms_digest': f.get('digest'), 'request_id': r['id'], 'input_root': (f.get('terms') or {}).get('operation', {}).get('input_root')})
            return {'terms_id': t['id'], 'request_id': r['id'], 'frozen_digest': f.get('digest')}
        if a == 'award':
            aw = E.board.award(db, principal, c['request_id'], {'offer_id': c['offer_id']})
            self.M.set_milestone(db, m['id'], ms['key'], 'executing', {'award_id': aw['id'], 'offer_id': c['offer_id'], 'provider_id': c['provider_id'], 'ceiling': aw['ceiling']})
            return {'award_id': aw['id'], 'ceiling': aw['ceiling'], 'provider_id': c['provider_id']}
        if a == 'verify':
            r = E.evidence.verify(db, principal, c['award_id'], 'm1', {}); self.M.set_milestone(db, m['id'], ms['key'], 'verifying', {'verified': True, 'verification_id': r.get('id')}); return {'verification': r.get('id'), 'state': r.get('state')}
        if a == 'decide':
            ev = E.evidence.evaluate(db, principal, c['award_id'], 'm1')
            if ev.get('decision_candidate') not in ('accepted', 'rejected'):
                return {'pending': ev.get('decision_candidate'), 'note': 'verification not complete yet'}
            d = E.evidence.decide(db, principal, c['award_id'], 'm1', {'decision': 'accept' if ev['decision_candidate'] == 'accepted' else 'reject'})
            aw = db.execute('SELECT * FROM work_awards WHERE id=?', (c['award_id'],)).fetchone(); v = E.board.award_view(db, principal, aw); jid = v['milestones'][0].get('job_id') or (v['milestones'][0].get('attempts') or [{}])[-1].get('job_id')
            product_inputs = sp.get('inputs')
            if d['decision'] == 'accepted':
                self.M.set_milestone(db, m['id'], ms['key'], 'accepted', {'decision_id': d.get('id'), 'job_id': jid, 'payable_amount': d.get('payable_amount')}, {'science': ev.get('science'), 'payment_class': d.get('payment_class'), 'payable_amount': d.get('payable_amount'), 'product_inputs': product_inputs, 'job_id': jid})
            else:
                self.M.set_milestone(db, m['id'], ms['key'], 'failed', {'decision_id': d.get('id'), 'job_id': jid}, {'science': ev.get('science'), 'reason': ev.get('reason')}, reason='rejected: ' + str(ev.get('reason'))[:200])
            return {'decision': d['decision'], 'science': ev.get('science'), 'payable_amount': d.get('payable_amount')}
        if a == 'pay':
            paid = list(b.get('paid') or []); requests = []
            for eid in c['entitlements']:
                i = E.money.prepare(db, principal, eid, {})
                if i['state'] == 'authorized' or i['state'] == 'reserved':
                    pass
                if i['state'] not in ('authorized', 'submitted', 'settled'):
                    E.money.authorize(db, principal, i['id'])
                try:
                    s_ = E.money.submit(db, principal, i['id'], {}); paid.append({'entitlement': eid, 'intent': i['id'], 'state': s_['state'], 'rail': s_.get('rail'), 'final': s_.get('final_amount')})
                except ServiceError as exc:
                    d_ = exc.detail if isinstance(exc.detail, dict) else {}
                    if d_.get('code') != 'native_signature_required':
                        raise
                    msg = dict(d_['message'], expires_at=now() + 900); aw = db.execute('SELECT * FROM work_awards WHERE id=?', (c['award_id'],)).fetchone()
                    base_seq = int(msg['nonce'].rsplit('-', 1)[1]); msg['nonce'] = '%s-%d' % (msg['actor_account'], base_seq + len(requests))     # several settlements of one milestone: consecutive sequence numbers, signed and submitted in order
                    ctx_ = {'kind': 'settlement', 'award_id': aw['id'], 'terms_digest': aw['terms_digest'], 'entitlement': eid, 'payment_intent': i['id'], 'obligation_id': msg.get('obligation_id'), 'recipient': msg['recipient'], 'amount': msg['amount_ceiling'], 'asset': msg['asset'], 'mission': m['id'], 'milestone': ms['key']}
                    sr = self.svc.signing.create(db, principal.workspace, msg['actor_account'], 'settlement', msg, ctx_, 'mission_milestone', '%s:%s:pay' % (m['id'], ms['key'])); requests.append(sr['id'])
            if requests:
                self.M.set_milestone(db, m['id'], ms['key'], ms['state'] if ms['state'] != 'pending' else 'ready', {'paid': paid, 'signing_requests': requests, 'custody': 'external-signer'})
                return {'awaiting_signature': requests, 'paid': paid, 'note': 'the payer account is held by an external signer: its agent must sign the settlement intents; nothing settles until then'}
            self.M.set_milestone(db, m['id'], ms['key'], 'settled', {'paid': paid}, {'paid': paid}); return {'paid': paid}
        if a == 'reserve':
            buyer = ctx['acting']['id']; attempt = len(b.get('previous_reservations') or []) + 1
            rbody = {'offer_id': c['offer_id'], 'offer_version': c['offer_version'], 'quantity': c['quantity'], 'request_id': 'mission-%s-%s-%d' % (m['id'], ms['key'], attempt), 'buyer_account': buyer, 'purpose': 'mission %s milestone %s attempt %d' % (m['id'], ms['key'], attempt)}
            if (ctx['acting']['custody'] or '').startswith('external-signer'):
                prep = self.svc.market.reserve_prepare(db, principal, rbody)
                sr = self.svc.signing.create(db, principal.workspace, buyer, 'reservation', prep['message'], prep['context'], 'mission_milestone', '%s:%s:reserve' % (m['id'], ms['key']), extra={'mission': m['id'], 'milestone': ms['key'], 'reserve_body': prep['reserve_body']})
                self.M.set_milestone(db, m['id'], ms['key'], ms['state'], {'signing_requests': [sr['id']], 'custody': 'external-signer', 'attempt': attempt, 'offer_id': c['offer_id'], 'offer_version': c['offer_version']})
                return {'awaiting_signature': [sr['id']], 'note': 'the buyer account is held by an external signer: its agent must sign the reservation intent; nothing reserved until then'}
            r = self.svc.market.reserve(db, principal, rbody)
            self.M.set_milestone(db, m['id'], ms['key'], 'executing', {'reservation_id': r['id'], 'offer_id': c['offer_id'], 'offer_version': c['offer_version'], 'price': r['price'], 'buyer_account': buyer, 'attempt': attempt}, {'energy_mwh_per_unit': c['offer'].get('energy_mwh_per_unit', 0)})
            return {'reservation_id': r['id'], 'price': r['price'], 'quantity': c['quantity'], 'attempt': attempt, 'planned': c.get('planned', False)}
        if a == 'release_missed_reservation':
            r = self.svc.market.cancel(db, principal, c['reservation_id'], {'reason': 'mission replan after a missed window'})
            prev = list(b.get('previous_reservations') or []) + [{'reservation_id': r['id'], 'offer_id': b.get('offer_id'), 'outcome': r['state'], 'released': r.get('price'), 'at': now()}]
            nb = {k: v for k, v in b.items() if k not in ('reservation_id', 'offer_id', 'offer_version', 'price', 'planned_offer_id', 'planned_start_at', 'planned_end_at', 'plan_digest', 'planned_cost')}
            db.execute('UPDATE mission_milestones SET binding_json=?, state=?, updated_at=? WHERE id=?', (json.dumps(dict(nb, previous_reservations=prev)), 'ready', now(), ms['id']))
            self.M._event(db, m['id'], principal.id, 'window_missed', {'milestone': ms['key'], 'reservation_id': r['id'], 'released': r.get('price'), 'note': 'no payment was made for the missed window; the milestone is ready for a replanned reservation'})
            return {'released_reservation': r['id'], 'state': r['state'], 'released_amount': r.get('price'), 'next': 'plan again (POST /missions/{id}/plan) or let the controller pick the next offer'}
        if a == 'settle_reservation':
            r = self.svc.market.settle(db, principal, c['reservation_id']); self.M.set_milestone(db, m['id'], ms['key'], 'settled', {'settled': True}, {'delivered_quantity': r['delivered_quantity'], 'owed': r['obligation']['settled_amount'] if r.get('obligation') else 0, 'measurement': r.get('measurement')})
            return {'reservation_id': r['id'], 'state': r['state'], 'delivered_quantity': r['delivered_quantity']}
        if a == 'analyze':
            from ..lunar import evidence as LE
            src = db.execute('SELECT * FROM mission_milestones WHERE mission_id=? AND key=?', (m['id'], sp['route_from'])).fetchone(); inputs = json.loads(src['spec_json']).get('inputs')
            cm = LE.candidate_model(inputs, sp['states'], label='MISSION_GATE'); r = self.svc.decisions.analyze(db, principal, {'inputs': cm, 'title': 'mission evidence gate'})
            self.M.set_milestone(db, m['id'], ms['key'], 'executing', {'analysis_job_id': r['job_id']}); return {'analysis_job_id': r['job_id']}
        if a == 'buy_evidence':
            from ..lunar import evidence as LE
            jid = b['analysis_job_id']; job = db.execute('SELECT state FROM jobs WHERE id=?', (jid,)).fetchone()
            if job['state'] != 'succeeded':
                return {'pending': 'analysis not finished'}
            exp = now() + 86400; sup = sp.get('supplier_provider')
            plan = self.svc.decisions.plan_create(db, principal, {'analysis_job_id': jid, 'menu': LE.menu(exp, sup), 'budget': sp['budget']})
            if plan['state'] == 'nothing_to_acquire':
                self.M.set_milestone(db, m['id'], ms['key'], 'accepted', {'plan_id': plan['id'], 'declined': True}, {'decision': 'no acquisition changes the decision; proceeding on the existing evidence', 'conclusion': 'redundant acquisition declined'}); return {'plan_id': plan['id'], 'declined': True}
            mat = self.svc.decisions.materialize(db, principal, plan['id'], {'asset': m['asset']})
            self.M.set_milestone(db, m['id'], ms['key'], 'executing', {'plan_id': plan['id'], 'acquisition_request_id': mat['request']['id'], 'acquisition_terms_id': mat['terms']['id'], 'selected_views': plan['selected_views']}); return {'plan_id': plan['id'], 'selected_views': plan['selected_views'], 'cost': plan['cost'], 'request_id': mat['request']['id']}
        if a == 'relay':
            src = db.execute('SELECT * FROM mission_milestones WHERE mission_id=? AND key=?', (m['id'], sp['artifact_from'])).fetchone(); res = db.execute('SELECT * FROM mission_milestones WHERE mission_id=? AND key=?', (m['id'], sp.get('reservation_from', ''))).fetchone()
            rb = json.loads(res['binding_json']) if res else {}
            if not rb.get('reservation_id'):
                return {'pending': 'no contact reservation'}
            r = self.svc.market.reservation_view(db, principal, rb['reservation_id'])
            if r['state'] != 'delivered' and r['state'] != 'settled':
                return {'pending': 'contact delivery not recorded by the provider', 'reservation': r['state']}
            self.M.set_milestone(db, m['id'], ms['key'], 'accepted', {'relayed_via': r['id']}, {'received_kib': r['delivered_quantity'], 'evidence': r.get('measurement')}); return {'relayed': r['delivered_quantity']}
        if a == 'await_signature':
            return {'pending_signing_requests': c['signing_requests'], 'note': 'waiting for the account holder\'s signer; the service holds no key for this account'}
        raise ServiceError('INTERNAL_DEFECT', 'unknown action')

    def _pending_signatures(self, db, b):
        ids = b.get('signing_requests') or []
        if not ids:
            return None
        rows = db.execute('SELECT id, state FROM native_signing_requests WHERE id IN (%s)' % ','.join('?' * len(ids)), tuple(ids)).fetchall()
        pend = [r['id'] for r in rows if r['state'] == 'pending']
        return pend or None

    def on_signed(self, db, principal, req, result):
        """A signing request bound to a mission milestone was executed: continue the milestone from durable records."""
        mid, key, what = req['ref_id'].split(':'); ms = db.execute('SELECT * FROM mission_milestones WHERE mission_id=? AND key=?', (mid, key)).fetchone(); b = json.loads(ms['binding_json'])
        if what == 'reserve':
            r = result; b.pop('signing_requests', None)
            self.M.set_milestone(db, mid, key, 'executing', dict(b, reservation_id=r['id'], price=r['price'], buyer_account=r['buyer_account']), {'energy_mwh_per_unit': 0})
        elif what == 'pay':
            paid = list(b.get('paid') or []); paid.append({'entitlement': ((json.loads(req['context_json']) or {}).get('bound') or {}).get('entitlement'), 'intent': result.get('id'), 'state': result.get('state'), 'rail': result.get('rail'), 'final': result.get('final_amount')})
            remaining = self._pending_signatures(db, b)
            if remaining:
                self.M.set_milestone(db, mid, key, ms['state'], dict(b, paid=paid))
            else:
                b.pop('signing_requests', None); self.M.set_milestone(db, mid, key, 'settled', dict(b, paid=paid), {'paid': paid}); self._finish_if_done(db, mid)
        self.M._event(db, mid, principal.id, 'signed', {'milestone': key, 'request': req['id'], 'kind': req['kind']})

    def _finish_if_done(self, db, mid):
        rows = db.execute('SELECT state, optional FROM mission_milestones WHERE mission_id=?', (mid,)).fetchall()
        if rows and all(r['state'] in ('settled', 'accepted', 'skipped', 'cancelled') or (r['optional'] and r['state'] == 'failed') for r in rows):
            db.execute("UPDATE mission_contracts SET state='completed', updated_at=? WHERE id=?", (now(), mid))

    def gate_progress(self, db, principal, mid, key):
        """Evidence gate follow-up: when the acquisition award is accepted and the decision resolved, the gate passes; ambiguous -> waiting; inconsistent -> failed."""
        ms = db.execute('SELECT * FROM mission_milestones WHERE mission_id=? AND key=?', (mid, key)).fetchone(); b = json.loads(ms['binding_json'])
        if not b.get('acquisition_award_id'):
            return None
        aw = db.execute('SELECT * FROM work_awards WHERE id=?', (b['acquisition_award_id'],)).fetchone(); v = self.svc.economy.board.award_view(db, principal, aw)
        jid = v['milestones'][0].get('job_id')
        if not jid:
            return None
        dv = self.svc.decisions.view(db, principal, jid)
        if dv['state'] != 'succeeded':
            return None
        concl = dv['projection'].get('conclusion')
        if concl == 'DECISION_RESOLVED':
            self.M.set_milestone(db, mid, key, 'accepted', {}, {'conclusion': concl, 'label': (dv.get('result') or {}).get('decision', {}).get('determined_label')})
        elif concl == 'DECISION_AMBIGUOUS':
            self.M.set_milestone(db, mid, key, 'waiting', {}, {'conclusion': concl}, reason='ambiguity remains: reported, not forced')
        else:
            self.M.set_milestone(db, mid, key, 'failed', {}, {'conclusion': concl}, reason='model inconsistent with observations')
        return concl
