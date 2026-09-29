"""§65-9 advanced disagreement review: group failed, incomplete and disputed audits, lay the producer's and the
auditor's implementation/environment facts side by side, and record evidence-linked reviewer decisions. No automatic
truth adjudication: a decision is an administrative record naming the evidence it rests on; gating still requires a
passing verification of the required class."""
import json
import secrets

from . import history
from .db import now
from .errors import ServiceError

DECISIONS = ('producer_upheld', 'auditor_upheld', 'environment_difference', 'inconclusive')
FIELDS = ('implementation_digest', 'selected_backend', 'precision', 'versions')


def _env(db, job_id):
    run = db.execute('SELECT implementation_digest, selected_backend, precision, versions_json, manifest_id, manifest_version FROM compute_runs WHERE job_id=?', (job_id,)).fetchone()
    if run is None:
        req = db.execute('SELECT host, versions_json FROM model_requests WHERE job_id=?', (job_id,)).fetchone()
        return {'kind': 'model', 'host': req['host'] if req else None, 'versions': json.loads(req['versions_json']) if req and req['versions_json'] else None} if req else None
    return {'kind': 'compute', 'implementation_digest': run['implementation_digest'], 'selected_backend': run['selected_backend'], 'precision': run['precision'], 'versions': json.loads(run['versions_json'] or '{}'),
            'manifest': '%s v%s' % (run['manifest_id'], run['manifest_version'])}


def _diff(a, b):
    out = []
    for f in FIELDS:
        va, vb = (a or {}).get(f), (b or {}).get(f)
        if f == 'versions' and isinstance(va, dict) and isinstance(vb, dict):
            for k in sorted(set(va) | set(vb)):
                if va.get(k) != vb.get(k):
                    out.append({'field': 'versions.' + k, 'producer': va.get(k), 'auditor': vb.get(k)})
        elif va != vb:
            out.append({'field': f, 'producer': va, 'auditor': vb})
    return out


class Disagreements:
    def __init__(self, settings, services):
        self.settings, self.svc = settings, services

    def groups(self, db, principal):
        principal.require('job:read')
        rows = db.execute("SELECT * FROM verification_jobs WHERE workspace=? AND state IN ('failed','incomplete','disputed','resolved') ORDER BY created_at", (principal.workspace,)).fetchall()
        groups = {}
        for r in rows:
            g = groups.setdefault(r['target_job_id'], {'target_job_id': r['target_job_id'], 'records': [], 'producer': _env(db, r['target_job_id']), 'auditors': [], 'differences': [], 'classes': set()})
            res = json.loads(r['result_json']) if r['result_json'] else {}
            checks = res.get('checks') or []
            first = next((c for c in checks if not c.get('ok')), None)
            mism = (first or {}).get('detail', {}) if isinstance((first or {}).get('detail'), dict) else {}
            aud = _env(db, r['audit_job_id']) if r['audit_job_id'] else None
            rep = _env(db, r['replica_job_id']) if r['replica_job_id'] else None
            rec = {'verification_id': r['id'], 'class': r['class'], 'state': r['state'], 'outcome': res.get('outcome'), 'checked': res.get('checked'), 'total': res.get('total'), 'first_failed_check': (first or {}).get('check'),
                   'mismatches': mism.get('mismatches', [])[:3] if isinstance(mism.get('mismatches'), list) else None, 'mismatch_count': len(mism.get('mismatches', [])) if isinstance(mism.get('mismatches'), list) else None,
                   'statement': res.get('statement'), 'auditor_digest': (res.get('auditor_digest') or (r['statement_json'] and json.loads(r['statement_json']).get('auditor_digest'))), 'replica': rep, 'resolution': json.loads(r['resolution_json']) if r['resolution_json'] else None}
            g['records'].append(rec); g['classes'].add(r['class'])
            other = rep or aud
            if other:
                g['auditors'].append({'verification_id': r['id'], 'environment': other})
                for d in _diff(g['producer'], other):
                    if d not in g['differences']:
                        g['differences'].append(d)
        out = []
        for g in groups.values():
            g['classes'] = sorted(g['classes'])
            g['open'] = any(r['state'] in ('failed', 'incomplete', 'disputed') and not r['resolution'] for r in g['records'])
            g['reading'] = ('environment differences recorded between producer and auditor: a numerical disagreement may stem from them, or not; decide with evidence' if g['differences']
                            else 'same recorded implementation/backend/precision: the disagreement is not explained by the recorded environment')
            out.append(g)
        return out

    def decide(self, db, principal, vid, decision, note, evidence):
        principal.require('review:decide')
        r = db.execute('SELECT * FROM verification_jobs WHERE id=? AND workspace=?', (vid, principal.workspace)).fetchone()
        if r is None:
            raise ServiceError('NOT_FOUND', 'verification')
        if r['state'] not in ('failed', 'incomplete', 'disputed'):
            raise ServiceError('CONFLICT', {'code': 'not_open', 'state': r['state']})
        if decision not in DECISIONS or type(note) is not str or not 1 <= len(note) <= 2000:
            raise ServiceError('VALIDATION', {'code': 'decision/note', 'decisions': list(DECISIONS)})
        if type(evidence) is not list or not 1 <= len(evidence) <= 16:
            raise ServiceError('VALIDATION', 'evidence: 1..16 references {kind: job|verification|artifact, id}')
        contract = db.execute('SELECT reviewer_id FROM contracts WHERE id=(SELECT contract_id FROM jobs WHERE id=?)', (r['target_job_id'],)).fetchone()
        if contract['reviewer_id'] != principal.id:
            raise ServiceError('FORBIDDEN', 'not the designated reviewer for the target contract')
        bound = []
        for e in evidence:
            if type(e) is not dict or e.get('kind') not in ('job', 'verification', 'artifact') or type(e.get('id')) is not str:
                raise ServiceError('VALIDATION', 'evidence reference')
            table, col = {'job': ('jobs', 'evidence_root'), 'verification': ('verification_jobs', 'result_commitment'), 'artifact': ('artifacts', 'sha256_plaintext')}[e['kind']]
            row = db.execute('SELECT %s AS c FROM %s WHERE id=? AND workspace=?' % (col, table), (e['id'], principal.workspace)).fetchone()
            if row is None:
                raise ServiceError('NOT_FOUND', {'evidence': e})
            bound.append({'kind': e['kind'], 'id': e['id'], 'commitment': row['c']})
        res = {'decision': decision, 'note': note, 'evidence': bound, 'decided_at': now(), 'decided_by': principal.id, 'differences_at_decision': next((g['differences'] for g in self.groups(db, principal) if g['target_job_id'] == r['target_job_id']), []),
               'adjudication': 'reviewer decision bound to the listed evidence commitments; not an automatic truth determination',
               'effect': 'administrative record only: the verification keeps its outcome for gating; scientific acceptance still requires a passing verification of the required class'}
        db.execute("UPDATE verification_jobs SET state='resolved', resolution_json=?, resolved_by=?, resolved_at=? WHERE id=?", (json.dumps(res), principal.id, now(), vid))
        history.record(db, principal.workspace, principal.id, 'verification.resolved', 'verification', vid, {'decision': decision, 'evidence': [b['id'] for b in bound], 'review': 'disagreement'})
        return self.svc.verification.view(db, principal, vid)
