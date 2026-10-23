"""Explicit provider substitution for unstarted milestones (order 2026-10-23 §21 extension 2): the requester (or the primary of a
sub-contract's child mission) proposes a new provider for a milestone that has not been awarded; the proposal is an ordinary
amendment revision, so every affected party (the previously named providers and the new one) must authorize before it activates;
awarded, executing or completed milestones are immutable, and the parent obligation of a sub-contract is untouched (nothing is
moved: the substitution only changes who may be awarded)."""
import json

from ..errors import ServiceError


class Substitution:
    def __init__(self, settings, services):
        self.settings, self.svc = settings, services

    def propose(self, db, principal, mid, body):
        M = self.svc.missions
        if type(body) is not dict or set(body) - {'milestone', 'provider_id', 'reason'} or type(body.get('milestone')) is not str or type(body.get('provider_id')) is not str:
            raise ServiceError('VALIDATION', {'code': 'fields', 'allowed': ['milestone', 'provider_id', 'reason?']})
        r = M._row(db, principal.workspace, mid)
        if principal.id != r['owner_principal'] and not principal.can('native:admin'):
            raise ServiceError('FORBIDDEN', 'only the mission owner proposes a substitution')
        ms = db.execute('SELECT * FROM mission_milestones WHERE mission_id=? AND key=?', (mid, body['milestone'])).fetchone()
        if ms is None or ms['kind'] != 'work':
            raise ServiceError('NOT_FOUND', 'work milestone')
        if ms['state'] not in ('pending', 'ready', 'waiting'):
            raise ServiceError('CONFLICT', {'code': 'milestone_committed', 'state': ms['state'], 'note': 'substitution applies to unstarted milestones only; awarded or executing work keeps its provider and its obligations'})
        prov = db.execute('SELECT * FROM providers WHERE id=? AND workspace=?', (body['provider_id'], principal.workspace)).fetchone()
        if prov is None:
            raise ServiceError('NOT_FOUND', 'provider')
        cur = json.loads(db.execute('SELECT graph_json FROM mission_revisions WHERE mission_id=? AND revision=?', (mid, r['frozen_revision'])).fetchone()['graph_json'])
        m0 = next(m for m in cur['milestones'] if m['key'] == ms['key']); before = (m0.get('spec') or {}).get('permitted_providers') or []
        new_m = dict(m0, spec=dict(m0['spec'], permitted_providers=[prov['id']]))
        out = M.amend(db, principal, mid, {'milestones': [new_m], 'reason': 'provider substitution: %s' % (body.get('reason') or '')[:200]})
        rev = out.get('revision') if isinstance(out, dict) else None
        # the previously named providers are affected parties too (they lose the eligibility): add them to the revision's affected set
        row = db.execute('SELECT * FROM mission_revisions WHERE mission_id=? AND revision=?', (mid, r['revision'] + 1)).fetchone()
        affected = set(json.loads(row['affected_json']))
        for pid in before:
            pr = db.execute('SELECT principal_id FROM providers WHERE id=? OR principal_id=?', (pid, pid)).fetchone()
            if pr:
                affected.add(pr['principal_id'])
        db.execute('UPDATE mission_revisions SET affected_json=? WHERE mission_id=? AND revision=?', (json.dumps(sorted(affected)), mid, r['revision'] + 1))
        M._event(db, mid, principal.id, 'substitution_proposed', {'milestone': ms['key'], 'from': before, 'to': prov['id'], 'revision': r['revision'] + 1, 'affected': sorted(affected), 'note': 'activates when every affected party authorizes; obligations of awarded work are untouched'})
        return {'mission': mid, 'milestone': ms['key'], 'revision': r['revision'] + 1, 'from_providers': before, 'to_provider': prov['id'], 'affected_parties': sorted(affected), 'state': 'proposed', 'authorize_with': 'POST /missions/%s/revisions/%d/authorize by each affected party' % (mid, r['revision'] + 1)}
