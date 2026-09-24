"""Saved scientific templates: NON-SECRET parameters (kind, policy, notes) an authorized user
reuses to instantiate a new versioned contract. Inputs are never stored in a template and
must be supplied explicitly on instantiation, so private input is never silently reused.
Runs instantiated from one template can be compared side by side (owner projection)."""
import json
import secrets
from . import history
from .contracts import KINDS, validate_policy
from .db import now
from .errors import ServiceError


class Templates:
    def __init__(self, contracts):
        self.contracts = contracts

    def save(self, db, principal, *, name, kind, policy, notes=''):
        principal.require('template:write')
        if kind not in KINDS or type(name) is not str or not 1 <= len(name) <= 64 or type(notes) is not str or len(notes) > 1000:
            raise ServiceError('VALIDATION', 'template fields')
        pol = validate_policy(kind, policy, self.contracts.default_capability)
        tid = 't_' + secrets.token_hex(8)
        db.execute('INSERT INTO templates VALUES (?,?,?,?,?,?,?,?,?)',
                   (tid, principal.workspace, principal.id, name, kind, json.dumps(pol), notes, now(), now()))
        return tid

    def list(self, db, principal):
        principal.require('contract:read')
        return [self.view(r) for r in db.execute('SELECT * FROM templates WHERE workspace=? ORDER BY created_at DESC LIMIT 100', (principal.workspace,))]

    def get(self, db, principal, template_id):
        principal.require('contract:read')
        row = db.execute('SELECT * FROM templates WHERE id=? AND workspace=?', (template_id, principal.workspace)).fetchone()
        if row is None:
            raise ServiceError('NOT_FOUND', 'template')
        return row

    def instantiate(self, db, principal, template_id, *, inputs, title=None, policy_overrides=None):
        """A new draft contract from the template's non-secret policy plus EXPLICIT inputs."""
        row = self.get(db, principal, template_id)
        if inputs is None:
            raise ServiceError('VALIDATION', 'inputs must be supplied explicitly; templates never carry inputs')
        pol = json.loads(row['policy_json'])
        if policy_overrides:
            pol.update(policy_overrides)
        cid = self.contracts.create_draft(db, principal, kind=row['kind'], title=title or row['name'], inputs=inputs, policy=pol)
        db.execute('UPDATE contracts SET template_id=? WHERE id=?', (template_id, cid))
        return cid

    def runs(self, db, principal, template_id):
        """Side-by-side results of runs instantiated from one template (private fields only for the owner)."""
        self.get(db, principal, template_id)
        private = principal.can('job:read_private')
        rows = db.execute('SELECT j.id AS job_id, j.state, j.review_state, j.outcome, j.summary_json, j.evidence_root, c.id AS contract_id, '
                          'c.version, c.contract_digest, c.input_root, c.title FROM jobs j JOIN contracts c ON c.id=j.contract_id '
                          'WHERE c.template_id=? AND c.workspace=? ORDER BY j.created_at', (template_id, principal.workspace)).fetchall()
        out = []
        for r in rows:
            item = {'job_id': r['job_id'], 'contract_id': r['contract_id'], 'version': r['version'], 'title': r['title'],
                    'contract_digest': r['contract_digest'], 'input_root': r['input_root'], 'state': r['state'],
                    'review_state': r['review_state'], 'evidence_root': r['evidence_root']}
            if private:
                summary = json.loads(r['summary_json']) if r['summary_json'] else {}
                item.update(outcome=r['outcome'], worst_margin=summary.get('worst_margin'), best_margin=summary.get('best_margin'),
                            safe_duration=summary.get('safe_duration'), selected_id=summary.get('selected_id'))
            out.append(item)
        return {'template_id': template_id, 'runs': out, 'distinct_input_roots': len({r['input_root'] for r in rows}),
                'note': 'each run has its own committed input root; the template supplies policy only'}

    @staticmethod
    def view(row):
        return {'id': row['id'], 'name': row['name'], 'kind': row['kind'], 'policy': json.loads(row['policy_json']),
                'notes': row['notes'], 'owner_id': row['owner_id'], 'created_at': row['created_at']}
