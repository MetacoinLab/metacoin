"""Native recurring service subscriptions as bounded prepaid milestones (order 2026-10-23 §21 extension 4): a subscription is an
ordinary mission with N identical work milestones, each gated by `not_before` (period start) and settled by its own settle
milestone, funded from the subscriber's own money under the mission ceiling (N × price + fees); there is NO automatic renewal:
when the last period settles the subscription is complete, and a new one needs a new explicit creation. `stop` (subscriber) and
`revoke` (the named provider) cancel every unstarted period through an amendment that both parties authorize by construction of
the call, releasing their ceilings; delivered periods stay paid."""
import json
import secrets

from ..db import now
from ..errors import ServiceError

MAX_PERIODS = 52


class _OwnerProxy:
    """The mission owner's identity for a revision proposed on its behalf by a provider revocation; same workspace and scope checks."""
    def __init__(self, principal, owner_id):
        self._p = principal; self.id = owner_id; self.workspace = principal.workspace

    def __getattr__(self, name):
        return getattr(self._p, name)


class Subscriptions:
    def __init__(self, settings, services):
        self.settings, self.svc = settings, services

    def create(self, db, principal, body):
        M = self.svc.missions
        if type(body) is not dict or set(body) - {'title', 'provider_id', 'template', 'inputs', 'price', 'periods', 'period_seconds', 'start_at', 'verifier', 'verifier_pay_to', 'bps'}:
            raise ServiceError('VALIDATION', {'code': 'fields', 'allowed': ['title?', 'provider_id', 'template', 'inputs', 'price', 'periods (1..%d)' % MAX_PERIODS, 'period_seconds (>=1)', 'start_at?', 'verifier?', 'verifier_pay_to?', 'bps?']})
        n = body.get('periods'); ps = body.get('period_seconds')
        if type(n) is not int or isinstance(n, bool) or not 1 <= n <= MAX_PERIODS or type(ps) is not int or isinstance(ps, bool) or ps < 1:
            raise ServiceError('VALIDATION', {'code': 'periods', 'allowed': 'periods 1..%d, period_seconds >= 1' % MAX_PERIODS})
        prov = db.execute('SELECT * FROM providers WHERE id=? AND workspace=?', (body.get('provider_id'), principal.workspace)).fetchone()
        if prov is None:
            raise ServiceError('NOT_FOUND', 'provider')
        start = body.get('start_at') or now(); ms = []
        for i in range(n):
            key = 'period_%d' % (i + 1); spec = {'template': body['template'], 'inputs': dict(body.get('inputs') or {}), 'price': body['price'], 'bps': body.get('bps', 0), 'verifier': body.get('verifier', 0), 'verifier_pay_to': body.get('verifier_pay_to'), 'permitted_providers': [prov['id']], 'not_before': start + i * ps}
            ms.append({'key': key, 'kind': 'work', 'depends': [], 'spec': spec}); ms.append({'key': 'settle_%d' % (i + 1), 'kind': 'settle', 'depends': [key], 'spec': {'of': key}})
        m = M.create(db, principal, {'title': body.get('title') or ('subscription: %s x%d' % (body['template'], n)), 'graph': {'milestones': ms, 'notes': 'subscription of %d prepaid periods of %d s; no automatic renewal' % (n, ps)}, 'policy': {}})
        f = M.freeze(db, principal, m['id']); sid = 'sub_' + secrets.token_hex(6)
        M._event(db, m['id'], principal.id, 'subscription_created', {'subscription': sid, 'provider': prov['id'], 'periods': n, 'period_seconds': ps, 'start_at': start, 'renewal': 'none: a new subscription needs a new explicit creation'})
        return dict(self.view(db, principal, m['id']), subscription=sid, frozen=f.get('state'))

    def view(self, db, principal, mid):
        v = self.svc.missions.view(db, principal, mid); periods = [m for m in v['milestones'] if m['key'].startswith('period_')]
        return {'mission': mid, 'title': v.get('title'), 'state': v['state'], 'periods': [{'key': m['key'], 'state': m['state'], 'not_before': (m.get('spec') or {}).get('not_before'), 'provider': ((m.get('spec') or {}).get('permitted_providers') or [None])[0]} for m in periods],
                'delivered': sum(1 for m in periods if m['state'] in ('accepted', 'settled')), 'cancelled': sum(1 for m in periods if m['state'] == 'cancelled'), 'remaining': sum(1 for m in periods if m['state'] in ('pending', 'ready', 'waiting')), 'renewal': 'none', 'ledger': self.svc.missions.budget_ledger(db, principal, mid)['totals']}

    def stop(self, db, principal, mid, body, by_provider=False):
        """Cancel every unstarted period: the subscriber stops, or the named provider revokes. Both are affected parties of the
        resulting revision; the caller's authorization is recorded with the proposal and the other party's is recorded here too,
        because cancelling unstarted periods takes nothing from either side (ceilings are released, delivered periods stay paid)."""
        M = self.svc.missions; r = M._row(db, principal.workspace, mid)
        rows = db.execute("SELECT * FROM mission_milestones WHERE mission_id=? AND key LIKE 'period_%' AND state IN ('pending','ready','waiting') ORDER BY created_at", (mid,)).fetchall()
        if by_provider:
            named = {json.loads(m['spec_json'])['permitted_providers'][0] for m in rows} if rows else set()
            mine = {p['id'] for p in db.execute('SELECT id FROM providers WHERE principal_id=?', (principal.id,)).fetchall()}
            if not named & mine and not principal.can('native:admin'):
                raise ServiceError('FORBIDDEN', 'only the named provider revokes')
        elif principal.id != r['owner_principal'] and not principal.can('native:admin'):
            raise ServiceError('FORBIDDEN', 'only the subscriber stops')
        if not rows:
            return dict(self.view(db, principal, mid), stopped=0, note='no unstarted period remained')
        keys = [m['key'] for m in rows] + ['settle_' + m['key'].split('_')[1] for m in rows]
        live = db.execute("SELECT COUNT(*) FROM mission_milestones WHERE mission_id=? AND state NOT IN ('cancelled','skipped')", (mid,)).fetchone()[0]
        if live == len(keys):                                                                 # nothing started at all: the whole subscription is withdrawn (no amendment to an empty graph)
            t = now()
            for k in keys:
                db.execute("UPDATE mission_milestones SET state='cancelled', reason=?, updated_at=? WHERE mission_id=? AND key=?", ('subscription %s before any period started' % ('revoked' if by_provider else 'stopped'), t, mid, k))
            db.execute("UPDATE mission_contracts SET state='cancelled', updated_at=? WHERE id=?", (t, mid))
            M._event(db, mid, principal.id, 'subscription_revoked' if by_provider else 'subscription_stopped', {'cancelled_periods': [m['key'] for m in rows], 'mission_cancelled': True, 'note': 'no period had started; nothing was owed on either side; no renewal'})
            return dict(self.view(db, principal, mid), stopped=len(rows), mission_cancelled=True)
        # a provider revocation proposes the cancelling revision on the owner's behalf (the owner loses nothing: unstarted periods only); both parties are affected and both authorizations are recorded
        proposer = principal if not by_provider else _OwnerProxy(principal, r['owner_principal'])
        out = M.amend(db, proposer, mid, {'milestones': [{'key': k, 'cancel': True} for k in keys], 'reason': ('provider revocation' if by_provider else 'subscriber stop') + ': ' + str((body or {}).get('reason') or '')[:200]})
        rev = r['revision'] + 1; row = db.execute('SELECT * FROM mission_revisions WHERE mission_id=? AND revision=?', (mid, rev)).fetchone(); affected = set(json.loads(row['affected_json']))
        for m in rows:
            pr = db.execute('SELECT principal_id FROM providers WHERE id=?', (json.loads(m['spec_json'])['permitted_providers'][0],)).fetchone()
            if pr:
                affected.add(pr['principal_id'])
        auths = set(json.loads(row['authorizations_json'])) | {principal.id, r['owner_principal']} | affected        # unstarted prepaid periods carry no obligation on either side: cancelling them needs no further consent
        db.execute('UPDATE mission_revisions SET affected_json=?, authorizations_json=? WHERE mission_id=? AND revision=?', (json.dumps(sorted(affected)), json.dumps(sorted(auths)), mid, rev))
        M._maybe_activate(db, principal, mid, rev)
        M._event(db, mid, principal.id, 'subscription_revoked' if by_provider else 'subscription_stopped', {'cancelled_periods': [m['key'] for m in rows], 'revision': rev, 'note': 'unstarted periods cancelled and their ceilings released; delivered periods stay paid; no renewal'})
        return dict(self.view(db, principal, mid), stopped=len(rows), revision=rev)
