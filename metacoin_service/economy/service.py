"""WorkTerms service: draft / validate / compare / freeze / inspect / amend / counteroffer, and acceptance evaluation.

Drafts change; a frozen WorkTerms never does. Freezing binds the operation to a frozen job contract (input vault
committed, model and verifier identities pinned) and records the canonical digest that every offer and award must
reference. An amendment is a new immutable revision linked to the prior one with a structured, schema-classified
difference; a consequential amendment needs acceptance by the counterparties (recorded per revision) before new work
uses it. Supersession is an explicit event under optimistic concurrency: two amendments can never both be current."""
import hashlib
import json
import secrets

from experiments.private_receipts import receipt as merkle
from .. import history
from ..db import now
from ..errors import ServiceError
from . import acceptance as acceptance_mod, terms as terms_mod


class Terms:
    def __init__(self, settings, services):
        self.settings, self.svc = settings, services

    # ---- rows and views ----------------------------------------------------------------------------------------------
    def row(self, db, principal, tid):
        r = db.execute('SELECT * FROM work_terms WHERE id=? AND workspace=?', (tid, principal.workspace)).fetchone()
        if r is None:
            raise ServiceError('NOT_FOUND', 'work terms')
        return r

    def view(self, db, principal, r, full=True):
        principal.require('contract:read')
        t = json.loads(r['terms_json'])
        out = {'id': r['id'], 'state': r['state'], 'version': r['version'], 'lineage_id': r['lineage_id'], 'previous_id': r['previous_id'], 'digest': r['digest'], 'draft_digest': r['draft_digest'],
               'contract_id': r['contract_id'], 'requester_id': r['requester_id'], 'proposed_by': r['proposed_by'], 'expires_at': r['expires_at'], 'frozen_at': r['frozen_at'], 'superseded_by': r['superseded_by'],
               'created_at': r['created_at'], 'updated_at': r['updated_at'], 'title': t['title'], 'kind': t['operation']['kind']}
        if full:
            out['terms'] = t
            out['agreement'] = self._agreement(db, r)
        return out

    def _agreement(self, db, r):
        rows = db.execute("SELECT ref_json, actor_id, ts FROM events WHERE object_type='work_terms' AND object_id=? AND event_type='work.terms_amended' ORDER BY seq", (r['id'],)).fetchall()
        accepted = [json.loads(x['ref_json']) for x in rows if json.loads(x['ref_json']).get('accepted_by')]
        return {'consequential_since_previous': json.loads(r['terms_json']).get('_amendment', {}).get('consequential') if False else None, 'acceptances': [{'by': a['accepted_by'], 'role': a.get('role')} for a in accepted]}

    # ---- draft ---------------------------------------------------------------------------------------------------------
    def create(self, db, principal, body):
        principal.require('contract:create')
        if type(body) is not dict:
            raise ServiceError('VALIDATION', 'body')
        if 'template' in body:
            t = terms_mod.template(body['template'], principal.id, principal.workspace, ceiling=body.get('ceiling', 10), amount=body.get('amount'), kind=body.get('kind'), registered_hash=body.get('registered_hash'), asset=body.get('asset', 'action-units'), title=body.get('title'))
            for k in ('purpose', 'notes'):
                if k in body:
                    t[k] = body[k]
            if 'overrides' in body:
                if type(body['overrides']) is not dict or set(body['overrides']) - set(terms_mod.TOP_FIELDS):
                    raise ServiceError('VALIDATION', {'code': 'overrides', 'allowed': list(terms_mod.TOP_FIELDS)})
                t.update(body['overrides'])
        else:
            t = body.get('terms')
        if type(t) is not dict:
            raise ServiceError('VALIDATION', 'terms or template required')
        t = dict(t, requester={'principal_id': principal.id, 'workspace': principal.workspace})
        terms_mod.validate(t, 'draft')
        tid = 'wt_' + secrets.token_hex(8)
        db.execute('INSERT INTO work_terms (id, workspace, requester_id, state, version, lineage_id, previous_id, terms_json, digest, draft_digest, contract_id, proposed_by, created_at, updated_at) VALUES (?,?,?,?,1,?,NULL,?,NULL,?,NULL,?,?,?)',
                   (tid, principal.workspace, principal.id, 'draft', tid, merkle.canonical(t).decode(), terms_mod.draft_digest(t), principal.id, now(), now()))
        history.record(db, principal.workspace, principal.id, 'work.terms_created', 'work_terms', tid, {'kind': t['operation']['kind'], 'version': 1, 'template': body.get('template')})
        return self.view(db, principal, self.row(db, principal, tid))

    def validate_body(self, db, principal, body):
        """Validate terms without storing anything; returns the inspection or the structured refusal."""
        principal.require('contract:read')
        t = dict(body.get('terms') or {}, requester={'principal_id': principal.id, 'workspace': principal.workspace})
        try:
            terms_mod.validate(t, 'draft')
        except ServiceError as exc:
            return {'valid': False, 'refusal': exc.body()}
        return {'valid': True, 'inspection': terms_mod.inspect(t), 'draft_digest': terms_mod.draft_digest(t)}

    def update(self, db, principal, tid, body):
        r = self.row(db, principal, tid)
        if r['state'] != 'draft' or r['requester_id'] != principal.id:
            raise ServiceError('CONFLICT', 'only the requester may edit a draft; frozen terms never change (amend instead)')
        if body.get('expected_draft_digest') and body['expected_draft_digest'] != r['draft_digest']:
            raise ServiceError('STATE_CONFLICT', {'code': 'draft_changed', 'current_draft_digest': r['draft_digest']})
        t = json.loads(r['terms_json'])
        changes = body.get('terms')
        if type(changes) is not dict or set(changes) - set(terms_mod.TOP_FIELDS):
            raise ServiceError('VALIDATION', {'code': 'terms_fields', 'allowed': list(terms_mod.TOP_FIELDS)})
        t.update(changes); t['requester'] = {'principal_id': r['requester_id'], 'workspace': r['workspace']}
        terms_mod.validate(t, 'draft')
        db.execute('UPDATE work_terms SET terms_json=?, draft_digest=?, updated_at=? WHERE id=?', (merkle.canonical(t).decode(), terms_mod.draft_digest(t), now(), tid))
        return self.view(db, principal, self.row(db, principal, tid))

    def inspect(self, db, principal, tid):
        r = self.row(db, principal, tid)
        t = json.loads(r['terms_json'])
        out = terms_mod.inspect(t)
        out.update(id=tid, state=r['state'], digest=r['digest'], version=r['version'])
        return out

    def compare(self, db, principal, tid, other_id):
        a = self.row(db, principal, tid); b = self.row(db, principal, other_id)
        return dict(terms_mod.compare(json.loads(a['terms_json']), json.loads(b['terms_json'])), from_id=tid, to_id=other_id, from_version=a['version'], to_version=b['version'])

    # ---- freeze ----------------------------------------------------------------------------------------------------------
    def freeze(self, db, principal, tid, body=None):
        """Bind the operation: create (or adopt) and freeze the job contract for the operation kind with the provided inputs,
        then pin the WorkTerms digest. Nothing is dispatched. `contract_id` may name an already frozen contract of the kind."""
        principal.require('contract:freeze')
        body = body or {}
        r = self.row(db, principal, tid)
        if r['state'] != 'draft' or r['requester_id'] != principal.id:
            raise ServiceError('CONFLICT', 'not a draft owned by the caller')
        t = json.loads(r['terms_json'])
        kind = t['operation']['kind']
        if body.get('contract_id'):
            c = db.execute('SELECT * FROM contracts WHERE id=? AND workspace=?', (body['contract_id'], principal.workspace)).fetchone()
            if c is None or c['state'] != 'frozen' or c['kind'] != kind or c['owner_id'] != principal.id:
                raise ServiceError('CONFLICT', {'code': 'contract_not_usable', 'note': 'a frozen job contract of the same kind owned by the requester is required'})
            if db.execute('SELECT 1 FROM jobs WHERE contract_id=?', (c['id'],)).fetchone():
                raise ServiceError('CONFLICT', {'code': 'contract_already_executed', 'note': 'terms must be frozen before dispatch'})
        else:
            inputs = body.get('inputs')
            if inputs is None:
                raise ServiceError('VALIDATION', 'inputs (or contract_id) required to bind the operation')
            rrow = db.execute("SELECT id FROM principals WHERE workspace=? AND role='reviewer' AND revoked_at IS NULL ORDER BY created_at LIMIT 1", (principal.workspace,)).fetchone()
            reviewer = body.get('reviewer_id') or (rrow['id'] if rrow else None)
            pol = {'reviewer_id': reviewer, 'accepted_outcomes': list(t['acceptance']['outcomes'].keys() & set(('FEASIBLE', 'INFEASIBLE', 'INDETERMINATE'))) or ['FEASIBLE', 'INFEASIBLE', 'INDETERMINATE'],
                   'disclose_outcome': 'outcome' in t['privacy']['evidence_disclosure'], 'expires_in_seconds': min(t['deadlines']['offer_seconds'] + t['deadlines']['delivery_seconds'] + t['deadlines']['acknowledge_seconds'], 365 * 86400),
                   'execution_locations': body.get('execution_locations') or ['*']}
            req = t['acceptance']['required_verification']
            if req['class'] != 'none':
                pol['required_verification'] = req['class']
            cid = self.svc.contracts.create_draft(db, principal, kind=kind, title=t['title'][:128], inputs=inputs, policy=pol, datasets=self.svc.datasets)
            self.svc.contracts.freeze(db, principal, cid)
            c = db.execute('SELECT * FROM contracts WHERE id=?', (cid,)).fetchone()
        doc = json.loads(c['contract_json'])
        mi = body.get('milestone_inputs') or {}
        if type(mi) is not dict or set(mi) - {m['key'] for m in t['milestones']}:
            raise ServiceError('VALIDATION', {'code': 'milestone_inputs', 'allowed': [m['key'] for m in t['milestones']]})
        main_assigned = False
        for m in t['milestones']:
            if m['key'] in mi:
                mcid = self.svc.contracts.create_draft(db, principal, kind=kind, title=(t['title'] + ' / ' + m['key'])[:128], inputs=mi[m['key']], policy=dict(pol) if not body.get('contract_id') else {'reviewer_id': c['reviewer_id']}, datasets=self.svc.datasets)
                self.svc.contracts.freeze(db, principal, mcid)
                mc = db.execute('SELECT * FROM contracts WHERE id=?', (mcid,)).fetchone()
                m['operation'] = {'contract_id': mc['id'], 'contract_digest': mc['contract_digest'], 'input_root': mc['input_root'], 'kind': kind}
            elif not main_assigned:
                m['operation'] = {'contract_id': c['id'], 'contract_digest': c['contract_digest'], 'input_root': c['input_root'], 'kind': kind}; main_assigned = True
        t['operation'] = dict(t['operation'], contract_id=c['id'], contract_digest=c['contract_digest'], input_root=c['input_root'], inputs_digest=c['inputs_digest'] if 'inputs_digest' in c.keys() else None,
                              model_id=doc.get('model_id'), verifier_id=doc.get('verifier_id'), verifier_digest=doc.get('verifier_digest'))
        digest = terms_mod.digest(t)
        expires = now() + t['deadlines']['offer_seconds']
        db.execute("UPDATE work_terms SET state='frozen', terms_json=?, digest=?, contract_id=?, expires_at=?, frozen_at=?, updated_at=? WHERE id=? AND state='draft'",
                   (merkle.canonical(t).decode(), digest, c['id'], expires, now(), now(), tid))
        from ..datasets import add_edge
        add_edge(db, principal.workspace, 'contract', c['id'], 'work_terms', tid, 'bound_operation')
        history.record(db, principal.workspace, principal.id, 'work.terms_frozen', 'work_terms', tid, {'digest': digest, 'contract_id': c['id'], 'contract_digest': c['contract_digest'], 'kind': kind, 'version': r['version']})
        return self.view(db, principal, self.row(db, principal, tid))

    # ---- amendments and counteroffers (§17) -------------------------------------------------------------------------------
    def amend(self, db, principal, tid, body):
        """A new immutable revision linked to the prior one. Requester amendments keep the operation binding unless inputs
        change (then the operation must be re-bound at freeze). A provider counteroffer is a proposed revision with an expiry
        that only the requester can freeze; either way the prior revision is untouched."""
        principal.require('contract:read')
        r = self.row(db, principal, tid)
        if r['state'] not in ('frozen', 'draft'):
            raise ServiceError('CONFLICT', 'only a frozen or draft revision can be amended')
        if body.get('expected_version') is not None and body['expected_version'] != r['version']:
            raise ServiceError('STATE_CONFLICT', {'code': 'version_changed', 'current_version': r['version']})
        if db.execute("SELECT id FROM work_terms WHERE previous_id=? AND state IN ('draft','frozen')", (tid,)).fetchone():
            raise ServiceError('CONFLICT', {'code': 'amendment_pending', 'note': 'a proposed revision already exists for this version; withdraw or freeze it first (two amendments can never both be current)'})
        is_requester = r['requester_id'] == principal.id
        if not is_requester and not principal.can('work:offer'):
            raise ServiceError('FORBIDDEN', 'only the requester or a provider (counteroffer) may propose a revision')
        old = json.loads(r['terms_json'])
        changes = body.get('terms')
        if type(changes) is not dict or not changes or set(changes) - set(terms_mod.TOP_FIELDS) or 'requester' in changes:
            raise ServiceError('VALIDATION', {'code': 'terms_fields', 'allowed': [f for f in terms_mod.TOP_FIELDS if f != 'requester']})
        new = json.loads(json.dumps(old)); new.update(changes)
        if 'operation' in changes:
            new['operation'] = {'kind': changes['operation'].get('kind', old['operation']['kind']), 'compatibility': changes['operation'].get('compatibility', old['operation'].get('compatibility', 'same_verifier_digest'))}
        else:
            new['operation'] = dict(old['operation'])
        terms_mod.validate(new, 'draft')
        diff = terms_mod.compare(old, new)
        nid = 'wt_' + secrets.token_hex(8)
        expires = now() + body['expires_in_seconds'] if type(body.get('expires_in_seconds')) is int else None
        if expires is not None and not 60 <= body['expires_in_seconds'] <= 90 * 86400:
            raise ServiceError('VALIDATION', 'expires_in_seconds 60..7776000')
        db.execute('INSERT INTO work_terms (id, workspace, requester_id, state, version, lineage_id, previous_id, terms_json, digest, draft_digest, contract_id, proposed_by, expires_at, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,NULL,?,?,?,?,?,?)',
                   (nid, r['workspace'], r['requester_id'], 'draft', r['version'] + 1, r['lineage_id'], tid, merkle.canonical(new).decode(), terms_mod.draft_digest(new), None if 'operation' in changes else r['contract_id'], principal.id, expires, now(), now()))
        history.record(db, principal.workspace, principal.id, 'work.terms_amended', 'work_terms', nid, {'previous': tid, 'version': r['version'] + 1, 'consequential': diff['consequential'], 'changed_paths': [c['path'] for c in diff['changes']][:32],
                                                                                                        'counteroffer': not is_requester, 'proposed_by': principal.id})
        out = self.view(db, principal, self.row(db, principal, nid))
        out['difference'] = diff
        out['requires_new_agreement'] = diff['consequential']
        out['note'] = 'the previous revision is untouched; execution attempts stay bound to it until this revision is frozen and awarded'
        return out

    def freeze_amendment(self, db, principal, tid, body=None):
        """Freeze a proposed revision: the requester agrees (a counteroffer needs the requester; a requester amendment needs
        the awarded provider's acceptance when one exists — recorded by the board). Supersedes the previous revision explicitly."""
        body = body or {}
        r = self.row(db, principal, tid)
        if r['previous_id'] is None:
            return self.freeze(db, principal, tid, body)
        if r['expires_at'] is not None and r['expires_at'] < now():
            db.execute("UPDATE work_terms SET state='withdrawn', updated_at=? WHERE id=?", (now(), tid))
            history.record(db, principal.workspace, principal.id, 'work.terms_superseded', 'work_terms', tid, {'expired': True, 'state': 'withdrawn'})
            db.execute('COMMIT'); db.execute('BEGIN IMMEDIATE')          # the expiry is a durable fact even though this request is refused
            raise ServiceError('EXPIRED', {'code': 'proposed_revision_expired', 'note': 'preserved as an audit record; it cannot be awarded'})
        prev = db.execute('SELECT * FROM work_terms WHERE id=?', (r['previous_id'],)).fetchone()
        if prev['state'] == 'superseded':
            raise ServiceError('CONFLICT', {'code': 'previous_already_superseded', 'by': prev['superseded_by']})
        t = json.loads(r['terms_json'])
        if r['contract_id'] and not body.get('inputs') and not body.get('contract_id'):
            body = dict(body, contract_id=r['contract_id']) if not db.execute('SELECT 1 FROM jobs WHERE contract_id=?', (r['contract_id'],)).fetchone() else body
            if 'contract_id' not in body:
                # the bound contract already executed under the old revision: rebind by copying its frozen inputs into a new contract
                c = db.execute('SELECT * FROM contracts WHERE id=?', (r['contract_id'],)).fetchone()
                vault = self.svc.store.load_json(db, c['input_artifact_id'], r['workspace'])
                body = dict(body, inputs={f['name']: f['value'] for f in vault['fields']}['inputs'])
        out = self.freeze(db, principal, tid, body)
        changed = db.execute("UPDATE work_terms SET state='superseded', superseded_by=?, updated_at=? WHERE id=? AND state IN ('frozen','draft')", (tid, now(), prev['id'])).rowcount
        history.record(db, principal.workspace, principal.id, 'work.terms_superseded', 'work_terms', prev['id'], {'by': tid, 'version': r['version'], 'explicit': True, 'changed': changed})
        out['superseded'] = prev['id']
        return out

    def withdraw(self, db, principal, tid):
        r = self.row(db, principal, tid)
        if r['state'] != 'draft' or (r['requester_id'] != principal.id and r['proposed_by'] != principal.id):
            raise ServiceError('CONFLICT', 'only a draft or proposed revision can be withdrawn by its author')
        db.execute("UPDATE work_terms SET state='withdrawn', updated_at=? WHERE id=?", (now(), tid))
        return self.view(db, principal, self.row(db, principal, tid))

    def list(self, db, principal, state=None):
        principal.require('contract:read')
        sql, args = 'SELECT * FROM work_terms WHERE workspace=?', [principal.workspace]
        if state:
            sql += ' AND state=?'; args.append(state)
        return [self.view(db, principal, r, full=False) for r in db.execute(sql + ' ORDER BY created_at DESC LIMIT 200', args).fetchall()]

    def upgrade_preview(self, db, principal, contract_id):
        principal.require('contract:read')
        c = db.execute('SELECT * FROM contracts WHERE id=? AND workspace=?', (contract_id, principal.workspace)).fetchone()
        if c is None or not c['contract_json']:
            raise ServiceError('NOT_FOUND', 'frozen contract')
        doc = json.loads(c['contract_json'])
        if c['kind'] != 'energy_audit':
            return {'from': doc.get('schema'), 'to': terms_mod.SCHEMA, 'requires_new_agreement': True, 'note': 'service contracts of this kind carry no v0 work-contract semantics; WorkTerms v1 wraps them by freezing terms around a bound contract'}
        return terms_mod.upgrade_preview(doc)

    # ---- acceptance evaluation ----------------------------------------------------------------------------------------------
    def evaluate(self, db, principal, tid, body):
        principal.require('job:read')
        r = self.row(db, principal, tid)
        if r['state'] not in ('frozen', 'superseded'):
            raise ServiceError('CONFLICT', 'terms must be frozen before evaluation')
        t = json.loads(r['terms_json'])
        job = None
        jrow = db.execute('SELECT id FROM jobs WHERE contract_id=? ORDER BY created_at DESC LIMIT 1', (r['contract_id'],)).fetchone() if r['contract_id'] else None
        jid = body.get('job_id') or (jrow['id'] if jrow else None)
        if jid:
            job = db.execute('SELECT * FROM jobs WHERE id=? AND workspace=?', (jid, principal.workspace)).fetchone()
            if job is None:
                raise ServiceError('NOT_FOUND', 'job')
        ev = acceptance_mod.evaluate(db, t, body.get('milestone', t['milestones'][0]['key']), job, provider_identity=body.get('provider_identity'))
        if not principal.can('job:read_private'):
            ev = dict(ev, trace=[{k: v for k, v in x.items() if k != 'detail'} for x in ev['trace']], note=ev['note'] + '; private predicate details withheld for this role')
        eid = 'we_' + secrets.token_hex(6)
        db.execute('INSERT INTO work_evaluations VALUES (?,?,?,?,?,?,?,?,?,?,?,?)', (eid, principal.workspace, tid, ev['milestone'], ev['job_id'], json.dumps(ev), ev['decision_candidate'], ev['science'], ev['execution'], ev['payment_class'], principal.id, now()))
        history.record(db, principal.workspace, principal.id, 'work.acceptance_evaluated', 'work_terms', tid, {'evaluation_id': eid, 'job_id': ev['job_id'], 'decision_candidate': ev['decision_candidate'], 'science': ev['science'], 'execution': ev['execution'], 'payment_class': ev['payment_class']})
        return dict(ev, evaluation_id=eid, terms_id=tid, terms_digest=r['digest'])
