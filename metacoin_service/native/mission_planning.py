"""Mission-level planning (group E): builds a bounded planning problem from unstarted milestones of one or several missions, the
current market offers, the owner's spendable settled funds (after the minimum reserve and mission ceiling) and the declared energy
budget; solves it with the chosen planner; records the result as a mission event; optionally writes planned offer hints into the
unstarted resource milestones. A plan locks nothing: the controller's reserve action revalidates through the market."""
import hashlib
import json
import time

from ..errors import ServiceError
from . import planner as PL
from . import mission_contracts as MC

DEFAULT_SLOT = 60
DEFAULT_HORIZON = 7200


def now():
    return int(time.time())


class MissionPlanning:
    def __init__(self, svc):
        self.svc = svc

    def problem(self, db, principal, mission_ids, body):
        body = body or {}
        if set(body) - {'objective', 'planner', 'timeout_ms', 'horizon', 'deadline_at', 'apply', 'budget_override', 'mission_ids', 'label'}:
            raise ServiceError('VALIDATION', {'code': 'fields', 'allowed': ['objective', 'planner', 'timeout_ms', 'horizon{start,end,slot_seconds}', 'deadline_at', 'apply', 'mission_ids (joint)']})
        t = now(); MS, CT = self.svc.missions, self.svc.controller
        offers = [o for o in self.svc.market.offers(db, principal) if o['state'] == 'open' and o['available'] > 0 and o['window']['end'] > t]
        h = body.get('horizon') or {}
        start = h.get('start', t); end = h.get('end', max([o['window']['end'] for o in offers] + [t + DEFAULT_HORIZON])); slot = h.get('slot_seconds', DEFAULT_SLOT)
        if type(end) is int and type(start) is int and type(slot) is int and (end - start) // slot > PL.MAX_SLOTS:
            slot = -(-(end - start) // PL.MAX_SLOTS)
        tasks, budgets, energy_budget, energy_reserve, labels = [], {}, None, 0, {}
        for mid in mission_ids:
            m = MS._row(db, principal.workspace, mid); ctx = CT._context(db, principal, m); pol = ctx['policy']
            labels[mid] = m['title']
            budget = ctx['spendable'] if ctx['role'] == 'requester' else ctx['spendable']
            if pol.get('mission_ceiling') is not None:
                budget = min(budget, max(pol['mission_ceiling'] - ctx['public']['mission_reserved'] - ctx['public']['mission_spent'], 0))
            if ctx['grant']:
                budget = min(budget, max(ctx['grant']['remaining_amount'], 0))
            budgets[mid] = budget
            if pol.get('energy_budget_mwh') is not None:
                energy_budget = (energy_budget or 0) + pol['energy_budget_mwh'] - ctx['energy_used']; energy_reserve = max(energy_reserve, pol.get('energy_reserve_mwh', 0))
            rows = db.execute('SELECT * FROM mission_milestones WHERE mission_id=? ORDER BY created_at', (mid,)).fetchall(); states = {r['key']: r['state'] for r in rows}
            for r in rows:
                if r['state'] not in ('pending', 'ready', 'waiting') or r['actor'] != ctx['role']:
                    continue
                sp = json.loads(r['spec_json']); deps = [mid + ':' + d for d in json.loads(r['depends_json']) if states.get(d) in ('pending', 'ready', 'waiting')]
                base = {'id': mid + ':' + r['key'], 'depends': deps, 'mission': mid, 'label': r['key'], 'duration_slots': int(sp.get('duration_slots', 1))}
                if r['kind'] == 'resource':
                    dl = sp.get('deadline_at')
                    tasks.append(dict(base, kind=sp['offer_kind'], quantity=int(sp['quantity']), deadline_slot=None if dl is None else max(1, min(-(-(int(dl) - start) // slot), PL.MAX_SLOTS))))
                elif r['kind'] == 'work':
                    tasks.append(dict(base, kind='work', quantity=0, local=True, fixed_cost=MC.ceiling_of(sp, 'work')))
                elif r['kind'] == 'evidence_gate':
                    tasks.append(dict(base, kind='gate', quantity=0, local=True, fixed_cost=int(sp.get('budget', 0))))
                else:
                    tasks.append(dict(base, kind=r['kind'], quantity=0, local=True))
        problem = {'horizon': {'start': start, 'end': end, 'slot_seconds': slot}, 'tasks': tasks, 'budgets': budgets, 'objective': body.get('objective', 'earliest_delivery'),
                   'offers': [{'id': o['id'], 'kind': o['kind'], 'available': o['available'], 'price_per_unit': o['price_per_unit'], 'energy_mwh_per_unit': o['energy_mwh_per_unit'] or 0, 'window_start': o['window']['start'], 'window_end': o['window']['end'], 'provider': o['provider_account']} for o in offers],
                   'energy_budget_mwh': energy_budget, 'energy_reserve_mwh': energy_reserve, 'label': labels}
        if body.get('deadline_at') is not None:
            problem['deadline_slot'] = max(1, min(-(-(int(body['deadline_at']) - start) // slot), PL.MAX_SLOTS))
        return problem

    def plan(self, db, principal, mission_ids, body):
        body = body or {}; problem = self.problem(db, principal, mission_ids, body)
        out = PL.solve(problem, body.get('planner', 'improved'), int(body.get('timeout_ms', 2000)))
        digest = hashlib.sha256(json.dumps(out, sort_keys=True, default=str).encode()).hexdigest()
        out.update({'mission_ids': list(mission_ids), 'plan_digest': digest, 'problem': {'tasks': len(problem['tasks']), 'offers': len(problem['offers']), 'budgets': problem['budgets'], 'energy_budget_mwh': problem['energy_budget_mwh'], 'energy_reserve_mwh': problem['energy_reserve_mwh'], 'horizon': problem['horizon']}, 'applied': []})
        summary = {'status': out['status'], 'objective': out['objective'], 'planner': out['planner'], 'digest': digest, 'witness': out.get('witness'), 'cost': (out.get('incumbent') or {}).get('cost'), 'makespan_slots': (out.get('incumbent') or {}).get('makespan_slots'), 'bound': out.get('bound'), 'runtime_ms': out.get('runtime_ms'), 'joint_with': [x for x in mission_ids]}
        for mid in mission_ids:
            self.svc.missions._event(db, mid, principal.id, 'plan', summary)
        if body.get('apply') and out.get('incumbent'):
            for tid, s in out['incumbent']['schedule'].items():
                mid, key = tid.split(':', 1)
                if s['offer_id'] is None:
                    continue
                row = db.execute('SELECT binding_json, state FROM mission_milestones WHERE mission_id=? AND key=?', (mid, key)).fetchone()
                if row is None or row['state'] not in ('pending', 'ready', 'waiting'):
                    continue
                b = json.loads(row['binding_json']); b.update({'planned_offer_id': s['offer_id'], 'planned_start_at': s['start_at'], 'planned_end_at': s['end_at'], 'plan_digest': digest, 'planned_cost': s['cost']})
                db.execute('UPDATE mission_milestones SET binding_json=?, updated_at=? WHERE mission_id=? AND key=?', (json.dumps(b), now(), mid, key)); out['applied'].append(tid)
            for mid in mission_ids:
                self.svc.missions._event(db, mid, principal.id, 'plan_applied', {'digest': digest, 'milestones': [x for x in out['applied'] if x.startswith(mid + ':')], 'note': 'hints only: the controller still revalidates offer version, capacity and funds at reservation'})
        return out
