"""Private audit access and portable proof packages (Order 08 Group E, §40–§44).

Compartments classify a contract's evidence by role; audit grants are explicit, read-only, purpose-bound, expiring
application grants (inspired by the viewing/spending separation, but MetaCoin application grants — not Zcash viewing
keys, no shielded proofs); encrypted offline packages reuse the existing age encryption with the recipient's own key;
retention holds protect evidence needed by an active dispute; projections choose what a private, collaborator or
public-ready record discloses and check for metadata leakage before signing. Publishing outside the application stays
outside this order's authority; a candidate anchoring record is a local export, never a ledger append."""
import hashlib
import json
import re
import secrets

from experiments.private_receipts import receipt as merkle
from .. import crypto, history, metering
from ..db import now
from ..errors import ServiceError
from .board import _terms

CATEGORIES = ('terms', 'offers', 'receipts', 'decisions', 'verification_statements', 'disclosed_evidence', 'dispute_records', 'private_inputs', 'private_evidence', 'payment_observations')
ROLE_CATEGORIES = {'requester': set(CATEGORIES), 'provider': {'terms', 'offers', 'receipts', 'decisions', 'verification_statements', 'disclosed_evidence', 'dispute_records', 'private_evidence', 'payment_observations'},
                   'verifier': {'terms', 'receipts', 'verification_statements', 'disclosed_evidence', 'private_inputs', 'private_evidence'}, 'resolver': {'terms', 'receipts', 'decisions', 'verification_statements', 'disclosed_evidence', 'dispute_records'},
                   'reader': {'terms', 'decisions', 'disclosed_evidence'}}
GRANT_SCHEMA = 'metacoin-audit-grant/v1'
PACKAGE_SCHEMA = 'metacoin-encrypted-audit-package/v1'
PROJECTION_SCHEMA = 'metacoin-contract-projection/v1'
AUDIENCES = ('private', 'collaborator', 'public_ready')


class Access:
    def __init__(self, settings, services, board, evidence):
        self.settings, self.svc, self.board, self.evidence = settings, services, board, evidence

    # ---- compartments (§40) ------------------------------------------------------------------------------------------------
    def role_of(self, db, principal, award):
        prow = db.execute('SELECT principal_id FROM providers WHERE id=?', (award['provider_id'],)).fetchone()
        if principal.id == award['awarded_by']:
            return 'requester'
        if prow and principal.id == prow['principal_id']:
            return 'provider'
        if principal.role == 'reviewer':
            return 'resolver'
        return 'reader'

    def compartments(self, db, principal, aid, key=None):
        award = self.board.award_row(db, principal, aid)
        t, terms = _terms(db, award['terms_id'])
        role = self.role_of(db, principal, award)
        allowed = set(ROLE_CATEGORIES[role])
        grant = self._active_grant(db, principal, aid)
        if grant:
            allowed |= set(json.loads(grant['fields_json']))
        if role == 'provider' and terms['privacy']['inputs'] == 'shared_with_provider_after_award' and award['state'] not in ('awarded',):
            allowed.add('private_inputs')
        if role == 'resolver' and terms['privacy']['verifier_access'] == 'full_private':
            allowed |= {'private_inputs', 'private_evidence'}
        out = {'award_id': aid, 'role': role, 'grant_id': grant['id'] if grant else None, 'visible_categories': sorted(allowed), 'hidden_categories': sorted(set(CATEGORIES) - allowed),
               'compartments': {'requester_inputs': 'private_inputs', 'provider_working_artifacts': 'not stored by the coordinator', 'submitted_deliverables': 'private_evidence (vault) / disclosed_evidence (openings)', 'verifier_diagnostics': 'verification_statements',
                                'dispute_supplements': 'dispute_records', 'public_projections': 'projection records'},
               'rule': 'a participant receives only the categories its role and purpose need; a signed artifact reference is not a right to read its contents; hiding a link is not authorization'}
        if key:
            ms = self.board.milestone_row(db, principal, aid, key)
            out['milestone'] = {'key': key, 'artifacts': self._artifact_refs(db, award, ms, allowed)}
        return out

    def _artifact_refs(self, db, award, ms, allowed):
        refs = []
        if ms['job_id']:
            job = db.execute('SELECT * FROM jobs WHERE id=?', (ms['job_id'],)).fetchone(); contract = db.execute('SELECT * FROM contracts WHERE id=?', (job['contract_id'],)).fetchone()
            if job['evidence_artifact_id']:
                refs.append({'category': 'private_evidence', 'artifact_id': job['evidence_artifact_id'], 'readable': 'private_evidence' in allowed, 'schema': 'merkle vault of the evidence (commitment root %s)' % job['evidence_root']})
            refs.append({'category': 'private_inputs', 'artifact_id': contract['input_artifact_id'], 'readable': 'private_inputs' in allowed, 'schema': 'merkle input vault (commitment root %s)' % contract['input_root']})
        return refs

    def read_artifact(self, db, principal, aid, artifact_id):
        """Read a compartment artifact under role or grant authority; every attempt is recorded (without the payload)."""
        award = self.board.award_row(db, principal, aid)
        comp = self.compartments(db, principal, aid)
        row = db.execute('SELECT * FROM artifacts WHERE id=? AND workspace=?', (artifact_id, principal.workspace)).fetchone()
        grant = self._active_grant(db, principal, aid)
        cat = None
        if row is not None:
            if row['kind'] == 'evidence_vault' and self._belongs(db, award, row):
                cat = 'private_evidence'
            elif row['kind'] in ('input_vault', 'draft_input') and self._belongs(db, award, row):
                cat = 'private_inputs'
        allowed = row is not None and cat is not None and cat in comp['visible_categories']
        if grant:
            self._access_event(db, grant, principal, 'read_artifact', artifact_id, allowed, cat)
        if not allowed:
            raise ServiceError('FORBIDDEN', {'code': 'out_of_scope', 'category': cat, 'note': 'the artifact is outside this principal\'s compartments or the grant\'s permitted categories'})
        if grant and json.loads(grant['policy_json']).get('download') == 'inspect_only':
            data = self.svc.store.load_json(db, artifact_id, principal.workspace)
            return {'artifact_id': artifact_id, 'category': cat, 'inspection': {'fields': sorted(f['name'] for f in data.get('fields', [])) if isinstance(data, dict) and 'fields' in data else 'structured', 'root': (data.get('receipt') or {}).get('root') if isinstance(data, dict) else None}, 'download': 'not permitted by the grant (inspect_only)'}
        return {'artifact_id': artifact_id, 'category': cat, 'content': self.svc.store.load_json(db, artifact_id, principal.workspace), 'access': 'role' if not grant else 'audit grant ' + grant['id']}

    def _belongs(self, db, award, row):
        ms = db.execute('SELECT job_id, contract_id FROM work_milestones WHERE award_id=?', (award['id'],)).fetchall()
        return any((row['job_id'] and row['job_id'] == m['job_id']) or (row['contract_id'] and row['contract_id'] == m['contract_id']) for m in ms)

    # ---- audit grants (§41) ---------------------------------------------------------------------------------------------------
    def create_grant(self, db, principal, aid, body):
        principal.require('work:audit_grant')
        award = self.board.award_row(db, principal, aid)
        if principal.id != award['awarded_by']:
            raise ServiceError('FORBIDDEN', 'the requester grants audit access to its contract')
        grantee = db.execute('SELECT id, role FROM principals WHERE id=? AND workspace=? AND revoked_at IS NULL', (body.get('grantee_id'),  principal.workspace)).fetchone()
        if grantee is None:
            raise ServiceError('NOT_FOUND', 'grantee principal')
        fields = body.get('categories')
        if type(fields) is not list or not fields or not set(fields) <= set(CATEGORIES):
            raise ServiceError('VALIDATION', {'code': 'categories', 'allowed': list(CATEGORIES)})
        purpose = body.get('purpose')
        if type(purpose) is not str or not 1 <= len(purpose) <= 256:
            raise ServiceError('VALIDATION', 'purpose')
        ttl = body.get('expires_in_seconds', 3600)
        if type(ttl) is not int or not 60 <= ttl <= 90 * 86400:
            raise ServiceError('VALIDATION', 'expires_in_seconds 60..7776000')
        download = body.get('download', 'inspect_only')
        if download not in ('inspect_only', 'download_allowed'):
            raise ServiceError('VALIDATION', {'code': 'download', 'allowed': ['inspect_only', 'download_allowed']})
        gid = 'ag_' + secrets.token_hex(8)
        st = {'schema': GRANT_SCHEMA, 'grant_id': gid, 'award_id': aid, 'grantee_id': grantee['id'], 'grantee_role': grantee['role'], 'purpose': purpose, 'categories': sorted(set(fields)), 'expires_at': now() + ttl, 'download': download,
              'authority': 'read-only audit access; separate from execution, acceptance, amendment and spending authority (which follow the grantee\'s own role and never this grant)',
              'not': 'not a Zcash viewing key, not a shielded proof: an application grant checked on every read', 'granted_by': principal.id, 'issued_at': now()}
        db.execute('INSERT INTO audit_grants VALUES (?,?,?,?,?,?,?,?,?,?,NULL,?)', (gid, principal.workspace, 'award', aid, grantee['id'], purpose, json.dumps(sorted(set(fields))), st['expires_at'], json.dumps({'download': download, 'statement': st}), principal.id, now()))
        history.record(db, principal.workspace, principal.id, 'work.audit_grant', 'audit_grant', gid, {'award_id': aid, 'grantee_id': grantee['id'], 'categories': sorted(set(fields)), 'expires_at': st['expires_at'], 'download': download})
        return self.grant_view(db, principal, gid)

    def grant_view(self, db, principal, gid):
        principal.require('work:read')
        g = db.execute('SELECT * FROM audit_grants WHERE id=? AND workspace=?', (gid, principal.workspace)).fetchone()
        if g is None:
            raise ServiceError('NOT_FOUND', 'audit grant')
        pol = json.loads(g['policy_json'])
        state = 'revoked' if g['revoked_at'] else ('expired' if g['expires_at'] < now() else 'active')
        out = {'id': gid, 'award_id': g['scope_id'], 'grantee_id': g['grantee_id'], 'purpose': g['purpose'], 'categories': json.loads(g['fields_json']), 'expires_at': g['expires_at'], 'download': pol['download'], 'state': state, 'granted_by': g['granted_by'], 'revoked_at': g['revoked_at'], 'created_at': g['created_at'],
               'statement': pol['statement'], 'limits': 'revocation and expiry stop FUTURE service access; evidence already disclosed or downloaded cannot be recalled'}
        if principal.id in (g['granted_by'], g['grantee_id']) or principal.can('work:audit_grant'):
            out['access_history'] = [dict(e) for e in db.execute('SELECT actor, action, object, allowed, category, at FROM audit_access_events WHERE grant_id=? ORDER BY rowid', (gid,)).fetchall()]
        return out

    def _active_grant(self, db, principal, aid):
        g = db.execute("SELECT * FROM audit_grants WHERE scope_id=? AND grantee_id=? AND revoked_at IS NULL ORDER BY created_at DESC LIMIT 1", (aid, principal.id)).fetchone()
        if g is None:
            return None
        if g['expires_at'] < now():
            self._access_event(db, g, principal, 'use', aid, False, None, note='expired')
            return None
        return g

    def _access_event(self, db, g, principal, action, obj, allowed, category, note=None):
        db.execute('INSERT INTO audit_access_events VALUES (?,?,?,?,?,?,?,?,?)', ('ae_' + secrets.token_hex(6), g['id'], principal.id, action, obj, int(bool(allowed)), category, note, now()))
        if not allowed:
            db.execute('COMMIT'); db.execute('BEGIN IMMEDIATE')          # a refused access is recorded even though the request is refused

    def use_grant(self, db, principal, gid):
        """The grantee's audit view: the permitted categories of the award; authority rechecked on every use."""
        principal.require('work:read')
        g = db.execute('SELECT * FROM audit_grants WHERE id=? AND workspace=?', (gid, principal.workspace)).fetchone()
        if g is None or g['grantee_id'] != principal.id:
            raise ServiceError('FORBIDDEN', 'not the grant holder')
        if g['revoked_at']:
            self._access_event(db, g, principal, 'use', gid, False, None, note='revoked')
            raise ServiceError('FORBIDDEN', {'code': 'grant_revoked', 'note': 'future access stops; already disclosed data cannot be recalled'})
        if g['expires_at'] < now():
            self._access_event(db, g, principal, 'use', gid, False, None, note='expired')
            raise ServiceError('EXPIRED', {'code': 'grant_expired'})
        award = db.execute('SELECT * FROM work_awards WHERE id=?', (g['scope_id'],)).fetchone()
        cats = set(json.loads(g['fields_json']))
        out = {'grant_id': gid, 'award_id': award['id'], 'categories': sorted(cats), 'purpose': g['purpose'], 'expires_at': g['expires_at']}
        t, terms = _terms(db, award['terms_id'])
        if 'terms' in cats:
            out['terms'] = terms
        if 'receipts' in cats:
            out['receipts'] = [{'id': r['id'], 'kind': r['kind'], 'statement': json.loads(r['statement_json']), 'signature_hex': r['signature_hex'], 'key_id': r['key_id']} for r in db.execute('SELECT * FROM work_receipts WHERE award_id=? ORDER BY rowid', (award['id'],)).fetchall()]
        if 'decisions' in cats:
            out['decisions'] = [{k: d[k] for k in ('id', 'decision', 'payment_class', 'payable_amount', 'evidence_root', 'authority', 'supersedes', 'superseded_by', 'created_at')} for d in db.execute('SELECT * FROM work_decisions WHERE award_id=? ORDER BY rowid', (award['id'],)).fetchall()]
        if 'verification_statements' in cats:
            out['verification_statements'] = [{'id': v['id'], 'class': v['class'], 'state': v['state'], 'statement': json.loads(v['statement_json']) if v['statement_json'] else None} for v in db.execute('SELECT v.* FROM verification_jobs v JOIN work_milestones m ON m.job_id=v.target_job_id WHERE m.award_id=? ORDER BY v.rowid', (award['id'],)).fetchall()]
        if 'dispute_records' in cats:
            out['dispute_records'] = [self.evidence.dispute_view(db, principal, d['id']) if False else {'id': d['id'], 'state': d['state'], 'scope': d['scope_type'], 'decision': json.loads(d['decision_json']) if d['decision_json'] else None} for d in db.execute('SELECT * FROM work_disputes WHERE award_id=?', (award['id'],)).fetchall()]
        if 'payment_observations' in cats:
            out['payment_observations'] = [{'id': i['id'], 'state': i['state'], 'final_amount': i['final_amount'], 'transaction_ref': i['transaction_ref'], 'observations': json.loads(i['observations_json'])} for i in db.execute('SELECT * FROM payment_intents WHERE entitlement_id IN (SELECT id FROM work_entitlements WHERE award_id=?)', (award['id'],)).fetchall()]
        for cat, art in (('private_inputs', 'input'), ('private_evidence', 'evidence')):
            if cat in cats:
                out[cat] = self._artifact_refs(db, award, db.execute('SELECT * FROM work_milestones WHERE award_id=? ORDER BY rowid LIMIT 1', (award['id'],)).fetchone(), cats)
        if 'disclosed_evidence' in cats:
            ms = db.execute('SELECT * FROM work_milestones WHERE award_id=? ORDER BY rowid LIMIT 1', (award['id'],)).fetchone()
            out['disclosed_evidence'] = {'evidence_root': ms['evidence_root'], 'disclosure': terms['privacy']['evidence_disclosure']}
        self._access_event(db, g, principal, 'use', gid, True, ','.join(sorted(cats)))
        return out

    def revoke_grant(self, db, principal, gid):
        principal.require('work:audit_grant')
        g = db.execute('SELECT * FROM audit_grants WHERE id=? AND workspace=?', (gid, principal.workspace)).fetchone()
        if g is None or g['granted_by'] != principal.id:
            raise ServiceError('NOT_FOUND', 'audit grant')
        db.execute('UPDATE audit_grants SET revoked_at=COALESCE(revoked_at, ?) WHERE id=?', (now(), gid))
        history.record(db, principal.workspace, principal.id, 'work.audit_grant', 'audit_grant', gid, {'revoked': True, 'note': 'future access stops; previously disclosed evidence cannot be remotely erased'})
        return self.grant_view(db, principal, gid)

    def list_grants(self, db, principal, aid=None):
        principal.require('work:read')
        sql, args = 'SELECT id FROM audit_grants WHERE workspace=?', [principal.workspace]
        if aid:
            sql += ' AND scope_id=?'; args.append(aid)
        rows = db.execute(sql + ' ORDER BY rowid', args).fetchall()
        return [self.grant_view(db, principal, r['id']) for r in rows if principal.can('work:audit_grant') or db.execute('SELECT 1 FROM audit_grants WHERE id=? AND grantee_id=?', (r['id'], principal.id)).fetchone()]

    # ---- encrypted offline packages (§42) -----------------------------------------------------------------------------------
    def encrypted_package(self, db, principal, aid, key, body):
        principal.require('artifact:export')
        award = self.board.award_row(db, principal, aid)
        if principal.id != award['awarded_by']:
            raise ServiceError('FORBIDDEN', 'the requester exports audit packages')
        recipient = body.get('recipient_age_public')
        if type(recipient) is not str or not recipient.startswith('age1') or len(recipient) > 128:
            raise ServiceError('VALIDATION', {'code': 'recipient_age_public', 'note': 'an age public key of the audit recipient (its private key never travels here)'})
        scope = body.get('scope', 'restricted')
        data, manifest = self.evidence.bundle(db, principal, aid, key, scope)
        crypto.require_age()
        ct = crypto.encrypt_bytes(data, [recipient])
        pub = metering.ensure_service_key(self.settings, db)
        outer = {'schema': PACKAGE_SCHEMA, 'award_id': aid, 'milestone': key, 'terms_digest': award['terms_digest'], 'evidence_root': manifest['evidence_root'], 'disclosure_scope': scope, 'recipient_key_fingerprint': hashlib.sha256(recipient.encode()).hexdigest()[:16],
                 'inner_bundle_sha256': hashlib.sha256(data).hexdigest(), 'ciphertext_sha256': hashlib.sha256(ct).hexdigest(), 'included_file_sha256': manifest['file_sha256'], 'encryption': 'age (X25519) to the recipient public key; signing and encryption roles are distinct',
                 'issuer_key_id': crypto.key_id_for(pub), 'issued_at': now(),
                 'limits': ['server-side revocation cannot revoke an already decrypted package', 'expiry labels are agreed policy, not cryptographic deletion', 'the signature proves the sender\'s bound disclosure, not the recipient\'s retention behaviour']}
        canon = merkle.canonical(outer)
        sig = crypto.sign(crypto.load_signing_key(self.settings.keys_dir / 'service.ed25519'), canon)
        history.record(db, principal.workspace, principal.id, 'artifact.exported', 'work_award', aid, {'milestone': key, 'encrypted_package': True, 'recipient_fingerprint': outer['recipient_key_fingerprint'], 'scope': scope})
        return {'manifest': outer, 'manifest_signature': sig, 'issuer_public_key': pub, 'ciphertext_b64': __import__('base64').b64encode(ct).decode(), 'decrypt_with': 'the recipient\'s own age identity; then run metacoin_service.economy.verify_work on the plaintext zip with --trust-root ' + pub}

    # ---- retention holds and deletion (§43) ---------------------------------------------------------------------------------
    def hold(self, db, award_id, milestone_id, imposed_by, policy, reason):
        hid = 'hold_' + secrets.token_hex(6)
        db.execute('INSERT INTO evidence_holds VALUES (?,?,?,?,?,?,?,1,?,NULL)', (hid, db.execute('SELECT workspace FROM work_awards WHERE id=?', (award_id,)).fetchone()['workspace'], award_id, milestone_id, imposed_by, policy, reason, now()))
        return hid

    def release_holds(self, db, milestone_id, reason):
        db.execute("UPDATE evidence_holds SET active=0, released_at=?, reason=reason || ' / released: ' || ? WHERE milestone_id=? AND active=1", (now(), reason, milestone_id))

    @staticmethod
    def held_artifact_ids(db):
        ids = set()
        for m in db.execute("SELECT m.job_id, m.contract_id FROM evidence_holds h JOIN work_milestones m ON m.id=h.milestone_id WHERE h.active=1").fetchall():
            for r in db.execute('SELECT id FROM artifacts WHERE (job_id=? AND job_id IS NOT NULL) OR (contract_id=? AND contract_id IS NOT NULL)', (m['job_id'], m['contract_id'])).fetchall():
                ids.add(r['id'])
        return ids

    def retention(self, db, principal, aid, key):
        award = self.board.award_row(db, principal, aid); ms = self.board.milestone_row(db, principal, aid, key)
        t, terms = _terms(db, award['terms_id'])
        holds = [dict(h) for h in db.execute('SELECT * FROM evidence_holds WHERE milestone_id=? ORDER BY rowid', (ms['id'],)).fetchall()]
        arts = []
        if ms['job_id']:
            for a in db.execute('SELECT id, kind, retention_deadline, deleted_at, intended_use FROM artifacts WHERE job_id=? OR contract_id=?', (ms['job_id'], ms['contract_id'])).fetchall():
                arts.append(dict(a))
        return {'award_id': aid, 'milestone': key, 'rules': {'requester_inputs': 'retained for the contract retention period; deletable on request unless held', 'submitted_deliverables': 'retained while an obligation or dispute is open; then per contract retention', 'dispute_supplements': 'retained with the dispute record',
                                                             'working_intermediates': 'not stored by the coordinator', 'indexes_caches_exports': 'derived copies are removed by the bounded cleanup; offline exports and backups cannot be recalled'},
                'holds': holds, 'artifacts': arts, 'cannot_recall': ['offline packages already exported', 'backups', 'copies held by providers or recipients'], 'what_remains_after_deletion': ['commitments (roots, digests)', 'receipts and decisions', 'this retention record']}

    def delete_evidence(self, db, principal, aid, key, body):
        """Immediate revocation of new disclosure; bounded payload cleanup unless a hold protects the evidence."""
        principal.require('artifact:delete')
        award = self.board.award_row(db, principal, aid); ms = self.board.milestone_row(db, principal, aid, key)
        if principal.id != award['awarded_by']:
            raise ServiceError('FORBIDDEN', 'requester only')
        holds = db.execute('SELECT * FROM evidence_holds WHERE milestone_id=? AND active=1', (ms['id'],)).fetchall()
        if holds and not body.get('resolve_holds_first'):
            raise ServiceError('CONFLICT', {'code': 'evidence_held', 'holds': [{'id': h['id'], 'policy': h['policy'], 'reason': h['reason'], 'imposed_by': h['imposed_by']} for h in holds], 'note': 'evidence required by an active contractual hold is not deleted; resolve the policy conflict first'})
        removed, retained = [], []
        db.execute("UPDATE work_milestones SET blocked_reason=?, updated_at=? WHERE id=?", ('evidence access revoked by the requester at %d; commitments retained' % now(), now(), ms['id']))
        targets = db.execute('SELECT id, kind FROM artifacts WHERE (job_id=? AND job_id IS NOT NULL) OR (contract_id=? AND contract_id IS NOT NULL)', (ms['job_id'], ms['contract_id'])).fetchall() if ms['job_id'] else []
        for a in targets:
            try:
                if self.svc.store.delete_payload(db, a['id'], principal.workspace, 'requester deletion request'):
                    removed.append(a['id'])
                else:
                    retained.append(a['id'])
            except ServiceError as exc:
                retained.append({'id': a['id'], 'code': exc.code})
        history.record(db, principal.workspace, principal.id, 'artifact.deleted', 'work_milestone', ms['id'], {'award_id': aid, 'removed': removed, 'retained': retained, 'commitments_retained': True})
        return {'award_id': aid, 'milestone': key, 'removed_payloads': removed, 'retained': retained, 'remains': {'evidence_root': ms['evidence_root'], 'receipts': 'retained', 'decisions': 'retained'}, 'not_recalled': ['exports already made', 'backups', 'copies at providers/recipients'], 'late_publication': 'a stale lease cannot republish: job leases are fenced'}

    # ---- projections and publication-ready records (§44) ----------------------------------------------------------------------
    def projection(self, db, principal, aid, key, body, sign=False):
        principal.require('work:read')
        award = self.board.award_row(db, principal, aid); ms = self.board.milestone_row(db, principal, aid, key)
        if principal.id != award['awarded_by'] and not principal.can('work:award'):
            raise ServiceError('FORBIDDEN', 'the requester builds projections')
        audience = body.get('audience', 'collaborator')
        if audience not in AUDIENCES:
            raise ServiceError('VALIDATION', {'code': 'audience', 'allowed': list(AUDIENCES)})
        t, terms = _terms(db, award['terms_id'])
        job = db.execute('SELECT * FROM jobs WHERE id=?', (ms['job_id'],)).fetchone() if ms['job_id'] else None
        dec = db.execute('SELECT * FROM work_decisions WHERE id=?', (ms['decision_id'],)).fetchone() if ms['decision_id'] else None
        verifs = db.execute("SELECT id, class, state FROM verification_jobs WHERE target_job_id=? AND state IN ('passed','failed')", (ms['job_id'],)).fetchall() if ms['job_id'] else []
        prow = db.execute('SELECT * FROM providers WHERE id=?', (award['provider_id'],)).fetchone()
        full = {'award_id': aid, 'milestone': key, 'terms_digest': award['terms_digest'], 'kind': terms['operation']['kind'], 'title': terms['title'], 'operation': {k: terms['operation'].get(k) for k in ('model_id', 'verifier_id', 'verifier_digest', 'input_root', 'contract_digest')},
                'execution': {'state': job['state'] if job else None, 'finished_at': job['finished_at'] if job else None}, 'science': {'outcome': job['outcome'] if job else None, 'summary': json.loads(job['summary_json']) if job and job['summary_json'] else None},
                'acceptance': {'decision': dec['decision'] if dec else None, 'payment_class': dec['payment_class'] if dec else None, 'decided_at': dec['created_at'] if dec else None, 'authority': dec['authority'] if dec else None},
                'verification': [{'id': v['id'], 'class': v['class'], 'state': v['state']} for v in verifs], 'payment': {'amount': dec['payable_amount'] if dec else None, 'recipient': award['pay_to'], 'asset': terms['payment']['asset']},
                'provider': {'id': prow['id'], 'name': prow['name'], 'relationship': json.loads(prow['relationship_json'])['relationship']}, 'requester': award['awarded_by'], 'evidence_root': ms['evidence_root']}
        omissions = []
        rec = json.loads(json.dumps(full))
        if audience in ('collaborator', 'public_ready'):
            rec['payment'] = {'amount': None if audience == 'public_ready' else rec['payment']['amount'], 'recipient': None, 'asset': rec['payment']['asset']}; omissions.append('payment recipient address')
            rec['science']['summary'] = None; omissions.append('private summary values')
            rec['requester'] = None; omissions.append('requester identity')
            if audience == 'public_ready':
                rec['payment']['amount'] = None; omissions.append('payment amount')
                rec['execution']['finished_at'] = None; rec['acceptance']['decided_at'] = None; omissions.append('exact timestamps (day granularity would be a separate choice)')
                rec['provider'] = {'id': None, 'name': None, 'relationship': rec['provider']['relationship']}; omissions.append('provider identity (relationship label kept)')
                rec['title'] = None; omissions.append('title (may name a rare task)')
                if 'outcome' not in terms['privacy']['evidence_disclosure']:
                    rec['science']['outcome'] = 'withheld-by-policy'; omissions.append('outcome (contract policy)')
        leak = []
        text = json.dumps(rec)
        if re.search(r'0x[0-9a-fA-F]{40}', text):
            leak.append({'check': 'payment_address_present', 'note': 'a chain address identifies a party across records'})
        if audience == 'public_ready' and any(rec[k] for k in ('title',)):
            leak.append({'check': 'rare_task_name', 'note': 'a rare title can identify the work'})
        if audience == 'public_ready' and (rec['execution']['finished_at'] or rec['acceptance']['decided_at']):
            leak.append({'check': 'exact_execution_time'})
        stable_ids = [x for x in (aid, ms['id'], award['terms_digest']) if x in text]
        leak.append({'check': 'stable_identifiers_present', 'note': 'award/milestone ids and the terms digest link records over time (kept on purpose: they are what a recipient binds to)', 'ids': len(stable_ids)})
        cohort = db.execute('SELECT COUNT(*) FROM work_awards WHERE workspace=? AND provider_id=?', (principal.workspace, award['provider_id'])).fetchone()[0]
        if cohort < 5 and audience == 'public_ready':
            leak.append({'check': 'small_cohort', 'note': 'fewer than 5 awards for this provider in the workspace: aggregates would identify it'})
        rec['verification_claim'] = ('independently replayed: %s' % ', '.join(v['class'] for v in verifs if v['state'] == 'passed')) if any(v['state'] == 'passed' for v in verifs) else 'no passed verification recorded: this projection does not claim scientific verification'
        if audience != 'private' and not any(v['state'] == 'passed' for v in verifs):
            rec['verification_claim'] = 'not independently verified in this projection (inputs withheld; no passed verification statement)'
        out = {'schema': PROJECTION_SCHEMA, 'audience': audience, 'record': rec, 'declared_omissions': omissions, 'leakage_checks': leak, 'consistency': 'every disclosed fact equals the private record; omissions are declared, never altered',
               'publication': 'outside this order\'s authority: the record is local until the operator publishes it deliberately', 'issued_at': now()}
        if sign:
            pub = metering.ensure_service_key(self.settings, db)
            canon = merkle.canonical(out['record'])
            out['signature'] = {'signature_hex': crypto.sign(crypto.load_signing_key(self.settings.keys_dir / 'service.ed25519'), canon), 'key_id': crypto.key_id_for(pub), 'public_key': pub, 'over': 'canonical record bytes'}
            history.record(db, principal.workspace, principal.id, 'analysis.projection', 'work_award', aid, {'milestone': key, 'audience': audience, 'omissions': len(omissions), 'signed': True})
        return out

    def anchor_candidate(self, db, principal, aid, key, body):
        """A proposed anchoring record in the protocol's append-only conventions, written as a LOCAL export: never appended to the
        real ledger, no production keys, no confirmation flag."""
        principal.require('work:read')
        pj = self.projection(db, principal, aid, key, dict(body, audience='public_ready'), sign=True)
        rec = {'event': 'work_contract_outcome_candidate', 'schema': 'metacoin-anchor-candidate/v1', 'status': 'candidate-not-anchored', 'anchored': False, 'no_token': True, 'zero_value': True,
               'record_sha256': hashlib.sha256(merkle.canonical(pj['record'])).hexdigest(), 'projection_signature_key_id': pj['signature']['key_id'], 'terms_digest': pj['record']['terms_digest'], 'evidence_root': pj['record']['evidence_root'],
               'limitation_note': 'application-layer candidate produced by the requester\'s own service; not a public ledger entry, not consensus, not independent verification; anchoring needs governance and is outside this order',
               'produced_at': now()}
        return {'candidate': rec, 'projection': pj, 'where': 'returned to the caller and recorded privately as a candidate artifact; not appended to protocol/ledger_data.jsonl'}
