"""Mission contracts with funded milestones (order 2026-10-23 group C). A mission is a versioned dependency graph of obligations
(acquire input, compute, verify/deliver, settle, follow-on resource purchase, evidence gate). Every milestone binds input
versions, resource requirements, permitted providers, a budget ceiling, the acceptance policy (through the existing frozen work
terms) and compensation rules. An awarded mission revision is frozen; amendments create a new revision that affected parties
authorize; replanning may change only unstarted, uncommitted work within the grant; incurred obligations are never erased.

The mission binds to existing primitives: work milestones become frozen WorkTerms -> requests -> awards (reservation of the
owner's native funds), resource milestones become market reservations, evidence gates use the decision package, settlement
pays existing entitlements natively. One authoritative monetary state (the journal); the mission ledger is a projection."""
import hashlib
import json
import secrets

from .. import history
from ..db import now
from ..errors import ServiceError
from . import protocol as P

KINDS = ('work', 'resource', 'evidence_gate', 'settle', 'deliver')
ACTORS = ('requester', 'provider')
STATES_MS = ('pending', 'ready', 'opened', 'awarded', 'executing', 'verifying', 'accepted', 'settled', 'failed', 'skipped', 'cancelled', 'waiting')
TERMINAL_OK = ('accepted', 'settled')
MAX_MILESTONES = 24


def digest(obj):
    return hashlib.sha256(b'metacoin/mission-graph/v1\0' + P.canonical(obj)).hexdigest()


def _int(v, lo, hi, name):
    if type(v) is not int or isinstance(v, bool) or not lo <= v <= hi:
        raise ServiceError('VALIDATION', {'code': name, 'allowed': 'integer %d..%d' % (lo, hi)})
    return v


def validate_graph(graph):
    """graph: {'milestones': [{key, kind, actor?, optional?, depends: [...], spec: {...}}]}"""
    if type(graph) is not dict or set(graph) - {'milestones', 'horizon', 'energy_budget_mwh', 'energy_reserve_mwh', 'notes'}:
        raise ServiceError('VALIDATION', {'code': 'graph_fields', 'allowed': ['milestones', 'horizon?', 'energy_budget_mwh?', 'energy_reserve_mwh?', 'notes?']})
    ms = graph.get('milestones')
    if type(ms) is not list or not 1 <= len(ms) <= MAX_MILESTONES:
        raise ServiceError('VALIDATION', {'code': 'milestones', 'allowed': '1..%d' % MAX_MILESTONES})
    keys = set()
    for m in ms:
        if type(m) is not dict or set(m) - {'key', 'kind', 'actor', 'optional', 'depends', 'spec'} or type(m.get('key')) is not str or not 1 <= len(m['key']) <= 32 or m.get('kind') not in KINDS:
            raise ServiceError('VALIDATION', {'code': 'milestone', 'allowed': ['key', 'kind (%s)' % '|'.join(KINDS), 'actor?', 'optional?', 'depends', 'spec']})
        if m['key'] in keys:
            raise ServiceError('VALIDATION', {'code': 'duplicate_key', 'key': m['key']})
        keys.add(m['key'])
        if m.get('actor', 'requester') not in ACTORS or type(m.get('optional', False)) is not bool or type(m.get('depends', [])) is not list or type(m.get('spec')) is not dict:
            raise ServiceError('VALIDATION', {'code': 'milestone_shape', 'key': m['key']})
        sp = m['spec']
        if m['kind'] == 'work':
            if type(sp.get('template')) is not str or type(sp.get('inputs')) not in (dict, type(None)) or (sp.get('inputs') is None and type(sp.get('inputs_from')) is not str):
                raise ServiceError('VALIDATION', {'code': 'work_spec', 'key': m['key'], 'allowed': ['template', 'inputs | inputs_from', 'price', 'bps?', 'verifier_pay_to?', 'verifier?', 'permitted_providers?', 'verification_class?', 'diagnostic?', 'scheme?']})
            _int(sp.get('price'), 1, P.MAX_AMOUNT, 'price'); _int(sp.get('bps', 0), 0, 10000, 'bps'); _int(sp.get('verifier', 0), 0, P.MAX_AMOUNT, 'verifier')
        elif m['kind'] == 'resource':
            if sp.get('offer_kind') not in ('compute', 'storage', 'contact'):
                raise ServiceError('VALIDATION', {'code': 'resource_spec', 'key': m['key'], 'allowed': ['offer_kind', 'quantity', 'max_price_per_unit', 'provider_account?', 'offer_id?', 'buyer (requester|provider_of:<key>)?']})
            _int(sp.get('quantity'), 1, 10 ** 12, 'quantity'); _int(sp.get('max_price_per_unit'), 0, P.MAX_AMOUNT, 'max_price_per_unit')
        elif m['kind'] == 'evidence_gate':
            if type(sp.get('route_from')) is not str or type(sp.get('states')) is not list or type(sp.get('budget')) is not int:
                raise ServiceError('VALIDATION', {'code': 'gate_spec', 'key': m['key'], 'allowed': ['route_from (work milestone key)', 'states', 'budget', 'supplier_provider?']})
        elif m['kind'] == 'settle':
            if type(sp.get('of')) is not str:
                raise ServiceError('VALIDATION', {'code': 'settle_spec', 'key': m['key'], 'allowed': ['of (work milestone key)']})
        elif m['kind'] == 'deliver':
            if type(sp.get('artifact_from')) is not str or sp.get('via') not in ('contact',):
                raise ServiceError('VALIDATION', {'code': 'deliver_spec', 'key': m['key'], 'allowed': ['artifact_from (work milestone key)', 'via: contact', 'reservation_from (resource milestone key)']})
    for m in ms:
        for d in m.get('depends', []):
            if d not in keys or d == m['key']:
                raise ServiceError('VALIDATION', {'code': 'dependency_unknown', 'key': m['key'], 'depends': d})
    # acyclic
    order = []; seen = {}
    def visit(k):
        if seen.get(k) == 1:
            raise ServiceError('VALIDATION', {'code': 'cycle', 'at': k})
        if seen.get(k) == 2:
            return
        seen[k] = 1; m = next(x for x in ms if x['key'] == k)
        for d in m.get('depends', []):
            visit(d)
        seen[k] = 2; order.append(k)
    for m in ms:
        visit(m['key'])
    return order


def ceiling_of(milestone_spec, kind):
    if kind == 'work':
        sp = milestone_spec; return sp['price'] + sp['price'] * sp.get('bps', 0) // 10000 + sp.get('verifier', 0)
    if kind == 'resource':
        return milestone_spec['quantity'] * milestone_spec['max_price_per_unit']
    if kind == 'evidence_gate':
        return milestone_spec['budget']
    return 0


class Missions:
    def __init__(self, settings, services):
        self.settings, self.svc = settings, services

    # ---- creation, freezing, amendments ------------------------------------------------------------------------------------------
    def create(self, db, principal, body):
        principal.require('work:request')
        if type(body) is not dict or set(body) - {'title', 'graph', 'template', 'asset', 'policy', 'grant_id'}:
            raise ServiceError('VALIDATION', {'code': 'fields', 'allowed': ['title', 'graph', 'template?', 'asset?', 'policy?', 'grant_id?']})
        from . import mission_templates
        graph = body.get('graph') if body.get('graph') is not None else mission_templates.build(body.get('template'), body.get('policy') or {})
        order = validate_graph(graph)
        title = body.get('title') or (body.get('template') or 'mission')
        if type(title) is not str or not 1 <= len(title) <= 128:
            raise ServiceError('VALIDATION', 'title')
        acct = self.svc.native.account_of(db, principal); asset = body.get('asset') or self.svc.native.default_asset(db, principal.workspace)
        policy = dict({'min_cash_reserve': 0, 'per_action_ceiling': None, 'mission_ceiling': None, 'permitted_recipients': None, 'energy_reserve_mwh': graph.get('energy_reserve_mwh', 0), 'energy_budget_mwh': graph.get('energy_budget_mwh'), 'stop_on': ['grant_revoked', 'domain_quarantined']}, **(body.get('policy') or {}))
        mid = 'mc_' + secrets.token_hex(6); t = now()
        db.execute('INSERT INTO mission_contracts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (mid, principal.workspace, principal.id, acct['id'], title, body.get('template'), asset, body.get('grant_id'), json.dumps(policy), 'draft', 1, None, None, t, t))
        db.execute('INSERT INTO mission_revisions VALUES (?,?,?,?,?,?,?,?,?,?)', (mid, 1, json.dumps(graph), digest(graph), principal.id, 'draft', '[]', '[]', 'initial', t))
        for m in graph['milestones']:
            db.execute('INSERT INTO mission_milestones VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', ('mm_' + secrets.token_hex(5), mid, m['key'], m['kind'], m.get('actor', 'requester'), int(bool(m.get('optional', False))), json.dumps(m.get('depends', [])), json.dumps(m['spec']), digest(m['spec']), 'pending', '{}', None, None, t, t))
        self._event(db, mid, principal.id, 'created', {'milestones': order, 'ceiling': self.ceiling(db, mid)})
        return self.view(db, principal, mid)

    def _row(self, db, workspace, mid):
        r = db.execute('SELECT * FROM mission_contracts WHERE id=? AND workspace=?', (mid, workspace)).fetchone()
        if r is None:
            raise ServiceError('NOT_FOUND', 'mission')
        return r

    def _event(self, db, mid, actor, kind, detail):
        db.execute('INSERT INTO mission_events (mission_id, at, actor, kind, detail_json) VALUES (?,?,?,?,?)', (mid, now(), actor, kind, json.dumps(detail)))

    def ceiling(self, db, mid):
        return sum(ceiling_of(json.loads(m['spec_json']), m['kind']) for m in db.execute('SELECT kind, spec_json FROM mission_milestones WHERE mission_id=? AND state<>?', (mid, 'cancelled')).fetchall())

    def freeze(self, db, principal, mid):
        """Freeze the current revision: milestone specs are digested and bound; nothing is awarded yet."""
        r = self._row(db, principal.workspace, mid)
        if r['owner_principal'] != principal.id:
            raise ServiceError('FORBIDDEN', 'only the mission owner freezes it')
        if r['state'] not in ('draft',):
            raise ServiceError('CONFLICT', {'code': 'state', 'state': r['state']})
        db.execute("UPDATE mission_revisions SET state='frozen' WHERE mission_id=? AND revision=?", (mid, r['revision']))
        db.execute("UPDATE mission_contracts SET state='frozen', frozen_revision=?, updated_at=? WHERE id=?", (r['revision'], now(), mid))
        db.execute("UPDATE mission_milestones SET state='ready', updated_at=? WHERE mission_id=? AND state='pending' AND depends_json='[]'", (now(), mid))
        self._event(db, mid, principal.id, 'frozen', {'revision': r['revision'], 'digest': db.execute('SELECT digest FROM mission_revisions WHERE mission_id=? AND revision=?', (mid, r['revision'])).fetchone()[0]})
        return self.view(db, principal, mid)

    def amend(self, db, principal, mid, body):
        """A new revision touching only unstarted milestones (pending/ready/waiting): specs may change or milestones be added/cancelled.
        Affected parties: the owner and any named provider principal of a changed milestone; the revision activates once all authorize.
        Completed, awarded or executing milestones are immutable; incurred obligations stay."""
        r = self._row(db, principal.workspace, mid)
        if r['owner_principal'] != principal.id:
            raise ServiceError('FORBIDDEN', 'only the mission owner proposes an amendment')
        if r['state'] not in ('frozen', 'running', 'paused', 'waiting'):
            raise ServiceError('CONFLICT', {'code': 'state', 'state': r['state']})
        if type(body) is not dict or set(body) - {'milestones', 'reason'}:
            raise ServiceError('VALIDATION', {'code': 'fields', 'allowed': ['milestones (full new specs for changed/added keys; cancel: true to cancel)', 'reason']})
        cur = json.loads(db.execute('SELECT graph_json FROM mission_revisions WHERE mission_id=? AND revision=?', (mid, r['frozen_revision'])).fetchone()['graph_json'])
        rows = {m['key']: m for m in db.execute('SELECT * FROM mission_milestones WHERE mission_id=?', (mid,)).fetchall()}
        new_ms = {m['key']: dict(m) for m in cur['milestones']}; affected = {principal.id}; changed = []
        for m in body.get('milestones') or []:
            key = m.get('key'); ex = rows.get(key)
            if ex is not None and ex['state'] not in ('pending', 'ready', 'waiting'):
                raise ServiceError('CONFLICT', {'code': 'milestone_committed', 'key': key, 'state': ex['state'], 'note': 'awarded, executing or completed milestones are immutable; a replan touches only unstarted work'})
            if m.get('cancel'):
                new_ms.pop(key, None); changed.append(key); continue
            spec_only = {k: v for k, v in m.items() if k != 'cancel'}; new_ms[key] = spec_only; changed.append(key)
            for prov in (spec_only.get('spec') or {}).get('permitted_providers') or []:
                pr = db.execute('SELECT principal_id FROM providers WHERE id=? OR principal_id=?', (prov, prov)).fetchone()
                if pr:
                    affected.add(pr['principal_id'])
        graph = dict(cur, milestones=list(new_ms.values())); validate_graph(graph)
        new_rev = r['revision'] + 1
        db.execute('INSERT INTO mission_revisions VALUES (?,?,?,?,?,?,?,?,?,?)', (mid, new_rev, json.dumps(graph), digest(graph), principal.id, 'proposed', json.dumps(sorted(affected)), json.dumps([principal.id]), str(body.get('reason', ''))[:256], now()))
        db.execute('UPDATE mission_contracts SET revision=?, updated_at=? WHERE id=?', (new_rev, now(), mid))
        self._event(db, mid, principal.id, 'amendment_proposed', {'revision': new_rev, 'changed': changed, 'affected': sorted(affected)})
        return self._maybe_activate(db, principal, mid, new_rev)

    def authorize(self, db, principal, mid, rev):
        r = self._row(db, principal.workspace, mid); row = db.execute('SELECT * FROM mission_revisions WHERE mission_id=? AND revision=?', (mid, rev)).fetchone()
        if row is None or row['state'] != 'proposed':
            raise ServiceError('CONFLICT', {'code': 'revision_state'})
        affected = json.loads(row['affected_json']); auths = json.loads(row['authorizations_json'])
        if principal.id not in affected:
            raise ServiceError('FORBIDDEN', {'code': 'not_affected', 'affected': affected})
        if principal.id not in auths:
            auths.append(principal.id); db.execute('UPDATE mission_revisions SET authorizations_json=? WHERE mission_id=? AND revision=?', (json.dumps(auths), mid, rev))
        self._event(db, mid, principal.id, 'amendment_authorized', {'revision': rev, 'by': principal.id})
        return self._maybe_activate(db, principal, mid, rev)

    def _maybe_activate(self, db, principal, mid, rev):
        row = db.execute('SELECT * FROM mission_revisions WHERE mission_id=? AND revision=?', (mid, rev)).fetchone(); affected = json.loads(row['affected_json']); auths = json.loads(row['authorizations_json'])
        if row['state'] == 'proposed' and set(affected) <= set(auths):
            r = self._row(db, principal.workspace, mid); graph = json.loads(row['graph_json']); t = now()
            db.execute("UPDATE mission_revisions SET state='superseded' WHERE mission_id=? AND revision=?", (mid, r['frozen_revision']))
            db.execute("UPDATE mission_revisions SET state='frozen' WHERE mission_id=? AND revision=?", (mid, rev))
            db.execute("UPDATE mission_contracts SET frozen_revision=?, state=CASE WHEN state='paused' THEN 'paused' ELSE 'frozen' END, updated_at=? WHERE id=?", (rev, t, mid))
            keys = {m['key'] for m in graph['milestones']}
            for m in db.execute('SELECT * FROM mission_milestones WHERE mission_id=?', (mid,)).fetchall():
                if m['key'] not in keys and m['state'] in ('pending', 'ready', 'waiting'):
                    db.execute("UPDATE mission_milestones SET state='cancelled', reason='removed by revision %d', updated_at=? WHERE id=?" % rev, (t, m['id']))
            for m in graph['milestones']:
                ex = db.execute('SELECT * FROM mission_milestones WHERE mission_id=? AND key=?', (mid, m['key'])).fetchone()
                if ex is None:
                    db.execute('INSERT INTO mission_milestones VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', ('mm_' + secrets.token_hex(5), mid, m['key'], m['kind'], m.get('actor', 'requester'), int(bool(m.get('optional', False))), json.dumps(m.get('depends', [])), json.dumps(m['spec']), digest(m['spec']), 'pending', '{}', None, None, t, t))
                elif ex['state'] in ('pending', 'ready', 'waiting'):
                    db.execute("UPDATE mission_milestones SET kind=?, actor=?, optional=?, depends_json=?, spec_json=?, spec_digest=?, state='pending', updated_at=? WHERE id=?", (m['kind'], m.get('actor', 'requester'), int(bool(m.get('optional', False))), json.dumps(m.get('depends', [])), json.dumps(m['spec']), digest(m['spec']), t, ex['id']))
            self._refresh_ready(db, mid)
            self._event(db, mid, principal.id, 'revision_activated', {'revision': rev, 'digest': row['digest']})
        return self.view(db, principal, mid)

    def pause(self, db, principal, mid, body):
        r = self._row(db, principal.workspace, mid)
        if r['owner_principal'] != principal.id and not principal.can('native:admin'):
            raise ServiceError('FORBIDDEN', 'only the owner pauses')
        reason = str((body or {}).get('reason', ''))[:256]
        if not reason:
            raise ServiceError('VALIDATION', {'code': 'reason_required'})
        db.execute("UPDATE mission_contracts SET state='paused', pause_reason=?, updated_at=? WHERE id=?", (reason, now(), mid)); self._event(db, mid, principal.id, 'paused', {'reason': reason})
        return self.view(db, principal, mid)

    def resume(self, db, principal, mid):
        r = self._row(db, principal.workspace, mid)
        if r['owner_principal'] != principal.id and not principal.can('native:admin'):
            raise ServiceError('FORBIDDEN', 'only the owner resumes')
        db.execute("UPDATE mission_contracts SET state='frozen', pause_reason=NULL, updated_at=? WHERE id=?", (now(), mid)); self._event(db, mid, principal.id, 'resumed', {})
        return self.view(db, principal, mid)

    # ---- dependency state ------------------------------------------------------------------------------------------------------
    def _refresh_ready(self, db, mid):
        rows = {m['key']: m for m in db.execute('SELECT * FROM mission_milestones WHERE mission_id=?', (mid,)).fetchall()}
        for m in rows.values():
            if m['state'] != 'pending':
                continue
            deps = json.loads(m['depends_json']); st = [rows[d]['state'] for d in deps if d in rows]
            if all(x in TERMINAL_OK for x in st):
                db.execute("UPDATE mission_milestones SET state='ready', updated_at=? WHERE id=?", (now(), m['id']))
            elif any(x in ('failed', 'cancelled') and not rows[d]['optional'] for d, x in zip(deps, st)):
                db.execute("UPDATE mission_milestones SET state='skipped', reason='mandatory dependency failed', updated_at=? WHERE id=?", (now(), m['id']))
            elif any(x in ('failed', 'skipped', 'cancelled') and rows[d]['optional'] for d, x in zip(deps, st)) and all(x in TERMINAL_OK or rows[d]['optional'] for d, x in zip(deps, st)):
                db.execute("UPDATE mission_milestones SET state='ready', updated_at=? WHERE id=?", (now(), m['id']))     # an optional branch failed: dependents continue without it

    def set_milestone(self, db, mid, key, state, binding=None, result=None, reason=None):
        m = db.execute('SELECT * FROM mission_milestones WHERE mission_id=? AND key=?', (mid, key)).fetchone()
        b = json.loads(m['binding_json']); b.update(binding or {})
        db.execute('UPDATE mission_milestones SET state=?, binding_json=?, result_json=COALESCE(?, result_json), reason=COALESCE(?, reason), updated_at=? WHERE id=?', (state, json.dumps(b), json.dumps(result) if result is not None else None, reason, now(), m['id']))
        self._refresh_ready(db, mid)

    # ---- views ---------------------------------------------------------------------------------------------------------------------
    def budget_ledger(self, db, principal, mid):
        """Projection of the authoritative journal and obligations onto this mission's milestones: ceiling, reserved, spent, released."""
        r = self._row(db, principal.workspace, mid); rows = db.execute('SELECT * FROM mission_milestones WHERE mission_id=? ORDER BY created_at', (mid,)).fetchall(); out = []; tot = {'ceiling': 0, 'reserved': 0, 'spent': 0, 'released': 0}
        for m in rows:
            b = json.loads(m['binding_json']); sp = json.loads(m['spec_json']); row = {'key': m['key'], 'kind': m['kind'], 'state': m['state'], 'ceiling': ceiling_of(sp, m['kind']), 'reserved': 0, 'spent': 0, 'released': 0, 'binding': b}
            if b.get('award_id'):
                aw = db.execute('SELECT * FROM work_awards WHERE id=?', (b['award_id'],)).fetchone()
                if aw:
                    paid = db.execute("SELECT COALESCE(SUM(amount_final),0) FROM native_intents WHERE type='settlement' AND state='settled' AND obligation_ref IN (SELECT id FROM native_obligations WHERE json_extract(policy_json, '$.award_id')=?)", (aw['id'],)).fetchone()[0]
                    row['spent'] = paid; row['reserved'] = aw['ceiling'] - paid if aw['state'] != 'closed' else 0; row['released'] = (aw['ceiling'] - paid) if aw['state'] == 'closed' else 0
            for resid in [x.get('reservation_id') for x in (b.get('previous_reservations') or [])] + ([b['reservation_id']] if b.get('reservation_id') else []):
                rr = db.execute('SELECT * FROM resource_reservations WHERE id=?', (resid,)).fetchone()
                if rr:
                    ob = db.execute('SELECT settled_amount FROM native_obligations WHERE id=?', (rr['obligation_id'],)).fetchone(); paid = ob['settled_amount'] if ob else 0
                    row['spent'] += paid; row['reserved'] += rr['price'] - paid if rr['state'] in ('reserved', 'active', 'delivered') else 0; row['released'] += (rr['price'] - paid) if rr['state'] in ('settled', 'cancelled') else 0
            if b.get('acquisition_award_id'):
                aw = db.execute('SELECT * FROM work_awards WHERE id=?', (b['acquisition_award_id'],)).fetchone()
                if aw:
                    paid = db.execute("SELECT COALESCE(SUM(amount_final),0) FROM native_intents WHERE type='settlement' AND state='settled' AND obligation_ref IN (SELECT id FROM native_obligations WHERE json_extract(policy_json, '$.award_id')=?)", (aw['id'],)).fetchone()[0]
                    row['spent'] = paid; row['reserved'] = aw['ceiling'] - paid if aw['state'] != 'closed' else 0
            for k in ('ceiling', 'reserved', 'spent', 'released'):
                tot[k] += row[k]
            out.append(row)
        acct = self.svc.native.account_view(db, principal, r['owner_account'], None) if principal.can('native:read') else None
        return {'mission_id': mid, 'asset': r['asset'], 'milestones': out, 'totals': tot, 'owner_account': {'id': r['owner_account'], 'available': acct['available'], 'reserved': acct['reserved'], 'earned_unsettled': acct['earned_unsettled']} if acct else None, 'source': 'projection of the journal, work awards and reservations; not a second balance'}

    def view(self, db, principal, mid):
        principal.require('work:read'); r = self._row(db, principal.workspace, mid)
        ms = []
        for m in db.execute('SELECT * FROM mission_milestones WHERE mission_id=? ORDER BY created_at', (mid,)).fetchall():
            ms.append({'id': m['id'], 'key': m['key'], 'kind': m['kind'], 'actor': m['actor'], 'optional': bool(m['optional']), 'depends': json.loads(m['depends_json']), 'spec': json.loads(m['spec_json']), 'spec_digest': m['spec_digest'], 'state': m['state'], 'binding': json.loads(m['binding_json']), 'result': json.loads(m['result_json']) if m['result_json'] else None, 'reason': m['reason'], 'ceiling': ceiling_of(json.loads(m['spec_json']), m['kind'])})
        revs = [{'revision': x['revision'], 'state': x['state'], 'digest': x['digest'], 'affected': json.loads(x['affected_json']), 'authorizations': json.loads(x['authorizations_json']), 'reason': x['reason'], 'created_at': x['created_at']} for x in db.execute('SELECT * FROM mission_revisions WHERE mission_id=? ORDER BY revision', (mid,)).fetchall()]
        events = [{'seq': e['seq'], 'at': e['at'], 'actor': e['actor'], 'kind': e['kind'], 'detail': json.loads(e['detail_json'])} for e in db.execute('SELECT * FROM mission_events WHERE mission_id=? ORDER BY seq DESC LIMIT 200', (mid,)).fetchall()]
        return {'schema': 'metacoin-mission-contract/v1', 'id': mid, 'title': r['title'], 'template': r['template'], 'state': r['state'], 'pause_reason': r['pause_reason'], 'owner_principal': r['owner_principal'], 'owner_account': r['owner_account'], 'asset': r['asset'], 'grant_id': r['grant_id'], 'policy': json.loads(r['policy_json']),
                'revision': r['revision'], 'frozen_revision': r['frozen_revision'], 'revisions': revs, 'milestones': ms, 'ceiling': self.ceiling(db, mid), 'events': events, 'created_at': r['created_at'], 'updated_at': r['updated_at']}

    def list(self, db, principal):
        principal.require('work:read')
        return [{'id': r['id'], 'title': r['title'], 'state': r['state'], 'template': r['template'], 'revision': r['revision'], 'owner_account': r['owner_account'], 'created_at': r['created_at']} for r in db.execute('SELECT * FROM mission_contracts WHERE workspace=? ORDER BY created_at DESC', (principal.workspace,)).fetchall()]
