"""Evidence made actionable (Order 08 Group C, §27–§36): typed work receipts with bounded claims, verifier assignment
with honest independence labels, entitlements with duplicate-claim prevention, append-only acceptance decisions,
disputes as a bounded workflow with corrections that never rewrite history, provider reassignment and bounded
delegation, and portable bundles for the offline verifier.

Four signatures, four claims (§27): a provider receipt says what the provider claims to have executed; a verification
record says what a named checker examined; an acceptance decision says how the frozen policy treated the evidence; a
settlement receipt (Group D) says what the payment rail observed. All are signed by the service key on behalf of the
authenticated principal (service custody, labelled), composed into bundles with explicit trust roots."""
import hashlib
import io
import json
import re
import secrets
import zipfile

from experiments.private_receipts import receipt as merkle
from .. import auth, budgets, crypto, history, metering
from ..db import now
from ..errors import ServiceError
from . import acceptance as acceptance_mod, terms as terms_mod
from .board import _sign, _terms

RECEIPT_SCHEMA = 'metacoin-work-receipt/v1'
DECISION_SCHEMA = 'metacoin-acceptance-decision/v1'
BUNDLE_SCHEMA = 'metacoin-work-bundle/v1'
CUSTODY = 'service-custodied: Ed25519 by this instance\'s service key on behalf of the authenticated principal; two records signed by the same key do not establish separation of duties'
DISPUTE_OUTCOMES = ('uphold', 'supersede_acceptance', 'reverse_acceptance', 'unresolved')
BUNDLE_LIMITS = {'members': 64, 'bytes': 8 * 1024 * 1024}


class Evidence:
    def __init__(self, settings, services, board):
        self.settings, self.svc, self.board = settings, services, board

    # ---- lookups ----------------------------------------------------------------------------------------------------------
    def award(self, db, principal, aid):
        return self.board.award_row(db, principal, aid)

    def milestone(self, db, principal, aid, key):
        return self.board.milestone_row(db, principal, aid, key)

    def _party(self, db, principal, award):
        prow = db.execute('SELECT * FROM providers WHERE id=?', (award['provider_id'],)).fetchone()
        return {'requester': principal.id == award['awarded_by'], 'provider': principal.id == prow['principal_id'], 'reviewer': principal.role == 'reviewer', 'provider_row': prow}

    # ---- receipts (§27) ---------------------------------------------------------------------------------------------------
    def _receipt(self, db, kind, award, ms, attempt_id, subject_id, claims, signer_identity):
        rid = 'wrc_' + secrets.token_hex(8)
        st = {'schema': RECEIPT_SCHEMA, 'kind': kind, 'receipt_id': rid, 'workspace': award['workspace'], 'terms_digest': award['terms_digest'], 'award_id': award['id'], 'milestone': ms['key'] if ms else None, 'milestone_id': ms['id'] if ms else None,
              'attempt_id': attempt_id, 'subject_id': subject_id, 'signer_identity': signer_identity, 'signer_custody': CUSTODY, 'issued_at': now(), 'nonce': secrets.token_hex(8), 'claims': claims,
              'meaning': {'provider': 'what the provider claims to have executed and delivered; not a verification, not an acceptance, not a payment',
                          'verification': 'what a named checker actually examined, with its class, scope and independence facts; not an acceptance',
                          'acceptance': 'how the frozen acceptance policy treated the evidence; not a payment',
                          'settlement': 'what the payment rail observed for one entitlement; not a scientific claim'}[kind]}
        msg, sig, kid = _sign(self.settings, db, st)
        db.execute('INSERT INTO work_receipts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)', (rid, award['workspace'], kind, award['id'], ms['id'] if ms else None, attempt_id, subject_id, msg, sig, kid, CUSTODY, signer_identity, now()))
        history.record(db, award['workspace'], 'service', 'work.receipt', 'work_receipt', rid, {'kind': kind, 'award_id': award['id'], 'milestone': ms['key'] if ms else None, 'subject_id': subject_id})
        return rid

    def _resource_evidence(self, db, job):
        """Measurements the host actually provides, kept apart from estimates and provider attestations (§59)."""
        crun = db.execute('SELECT * FROM compute_runs WHERE job_id=?', (job['id'],)).fetchone()
        att = db.execute('SELECT started_at, finished_at FROM attempts WHERE job_id=? ORDER BY generation DESC LIMIT 1', (job['id'],)).fetchone()
        out = {'measured': {'wall_seconds': (att['finished_at'] - att['started_at']) if att and att['finished_at'] and att['started_at'] else None, 'source': 'coordinator clock between claim and publication (second resolution)'},
               'estimated': {}, 'attested_by_provider': {}, 'energy': {'available': False, 'note': 'no calibrated energy integration on this path; energy is reported unavailable rather than derived from a nominal rating'}}
        if crun:
            tel = json.loads(crun['telemetry_json']) if crun['telemetry_json'] else {}
            out['measured'].update({'duration_ms': crun['duration_ms'], 'compute_ms': crun['compute_ms'], 'backend': crun['selected_backend'], 'work_units_committed': crun['work_committed']})
            if tel.get('source', '').startswith('node-reported'):
                out['attested_by_provider'] = {'telemetry': tel, 'note': 'node-reported values are provider attestations, not coordinator measurements'}
            else:
                out['measured']['telemetry'] = {k: tel.get(k) for k in ('samples', 'source', 'interval_seconds') if k in tel}
            if crun['energy_delta_mJ'] is not None if 'energy_delta_mJ' in crun.keys() else False:
                out['energy'] = {'available': True, 'device_wide_delta_mJ': crun['energy_delta_mJ'], 'attribution': 'device-wide delta during the job; shared-device overhead not separated', 'source': 'runtime telemetry'}
        mreq = db.execute('SELECT input_tokens, output_tokens, items FROM model_requests WHERE job_id=?', (job['id'],)).fetchone()
        if mreq:
            out['measured']['tokens'] = {'input': mreq['input_tokens'], 'output': mreq['output_tokens'], 'items': mreq['items'], 'source': 'runtime tokenizer'}
        return out

    def provider_receipt(self, db, award, ms, attempt):
        if attempt['receipt_id']:
            return attempt['receipt_id']
        job = db.execute('SELECT * FROM jobs WHERE id=?', (attempt['job_id'],)).fetchone()
        contract = db.execute('SELECT * FROM contracts WHERE id=?', (job['contract_id'],)).fetchone()
        doc = json.loads(contract['contract_json'] or '{}')
        prow = db.execute('SELECT * FROM providers WHERE id=?', (attempt['provider_id'],)).fetchone()
        t, terms = _terms(db, award['terms_id'])
        claims = {'executed': {'job_id': job['id'], 'kind': job['kind'], 'lease_generation': job['lease_generation'], 'attempt_generation': attempt['generation'], 'execution_state': job['state'], 'finished_at': job['finished_at']},
                  'provider': {'provider_id': prow['id'], 'revision': attempt.get('provider_revision', prow['revision']) if isinstance(attempt, dict) else prow['revision'], 'execution': json.loads(prow['execution_json'])['type'], 'relationship': json.loads(prow['relationship_json'])['relationship']},
                  'inputs': {'commitment': contract['input_root'], 'contract_digest': contract['contract_digest'], 'privacy': terms['privacy']['inputs'], 'note': 'commitment only; opening material stays private'},
                  'operation': {'model_id': doc.get('model_id'), 'verifier_id': doc.get('verifier_id'), 'verifier_digest': doc.get('verifier_digest')},
                  'outputs': {'evidence_artifact_id': job['evidence_artifact_id'], 'evidence_root': job['evidence_root'], 'outcome_disclosed': job['outcome'] if 'outcome' in terms['privacy']['evidence_disclosure'] else 'withheld-by-policy'},
                  'resources': self._resource_evidence(db, job), 'timestamps': {'dispatched_at': attempt['started_at'], 'finished_at': attempt['finished_at'], 'source': 'server receipt times; provider-signed times would be recorded separately'}}
        rid = self._receipt(db, 'provider', award, ms, attempt['id'], job['id'], claims, prow['principal_id'])
        db.execute('UPDATE work_attempts SET receipt_id=? WHERE id=?', (rid, attempt['id']))
        return rid

    def tick(self, db, aid=None):
        """Provider receipts for completed attempts; verification receipts for finished verifications on milestone jobs."""
        self.board.tick(db, aid)
        n = 0
        for at in db.execute("SELECT * FROM work_attempts WHERE state IN ('completed','failed') AND receipt_id IS NULL" + (' AND award_id=?' if aid else ''), ((aid,) if aid else ())).fetchall():
            award = db.execute('SELECT * FROM work_awards WHERE id=?', (at['award_id'],)).fetchone()
            ms = db.execute('SELECT * FROM work_milestones WHERE id=?', (at['milestone_id'],)).fetchone()
            self.provider_receipt(db, award, ms, dict(at)); n += 1
        for ms in db.execute("SELECT * FROM work_milestones WHERE job_id IS NOT NULL" + (' AND award_id=?' if aid else ''), ((aid,) if aid else ())).fetchall():
            award = db.execute('SELECT * FROM work_awards WHERE id=?', (ms['award_id'],)).fetchone()
            for v in db.execute("SELECT * FROM verification_jobs WHERE target_job_id=? AND state IN ('passed','failed','incomplete','disputed','resolved') AND statement_json IS NOT NULL", (ms['job_id'],)).fetchall():
                if db.execute("SELECT 1 FROM work_receipts WHERE kind='verification' AND subject_id=?", (v['id'],)).fetchone():
                    continue
                st = json.loads(v['statement_json'])
                prow = db.execute('SELECT * FROM providers WHERE id=?', (award['provider_id'],)).fetchone()
                ex = json.loads(prow['execution_json'])
                indep = {'process': ex['type'] == 'node', 'implementation': st.get('shared_code') != ['same implementation'], 'device': None, 'operator': False, 'organization': False,
                         'label': ('separate process (node execution), same host, same operator' if ex['type'] == 'node' else 'same process family, same host, same operator') + '; not organizational independence',
                         'requested_by': v['requested_by'], 'self_verification': v['requested_by'] == prow['principal_id']}
                claims = {'verification_id': v['id'], 'class': v['class'], 'outcome': st.get('outcome'), 'scope': st.get('scope'), 'result_commitment': v['result_commitment'], 'auditor_id': st.get('auditor_id'), 'auditor_digest': st.get('auditor_digest'),
                          'claim': st.get('claim'), 'challenge_digest': st.get('challenge_digest'), 'sampling': 'sampled evidence is a distinct class; it never establishes full replay' if v['class'] == 'sampled_reference' else None,
                          'independence': indep, 'statement_signature': {'key_id': v['key_id'], 'signature_hex': v['signature_hex']}}
                self._receipt(db, 'verification', award, ms, None, v['id'], claims, st.get('auditor_id')); n += 1
                if getattr(self, 'money', None) is not None:
                    t, terms = _terms(db, award['terms_id'])
                    self.money.on_verification(db, award, ms, v['id'], terms)
        return n

    def receipt_view(self, db, principal, rid):
        principal.require('work:read')
        r = db.execute('SELECT * FROM work_receipts WHERE id=? AND workspace=?', (rid, principal.workspace)).fetchone()
        if r is None:
            raise ServiceError('NOT_FOUND', 'receipt')
        award = db.execute('SELECT * FROM work_awards WHERE id=?', (r['award_id'],)).fetchone()
        party = self._party(db, principal, award)
        st = json.loads(r['statement_json'])
        if not (party['requester'] or party['provider'] or party['reviewer'] or principal.can('work:award')):
            # opaque by default: scope-explaining fields only, no private payload
            st = {k: st[k] for k in ('schema', 'kind', 'receipt_id', 'award_id', 'milestone', 'signer_custody', 'issued_at', 'meaning')}
        pub = db.execute("SELECT value FROM meta WHERE key='service_signing_public'").fetchone()['value']
        return {'id': rid, 'kind': r['kind'], 'award_id': r['award_id'], 'milestone_id': r['milestone_id'], 'attempt_id': r['attempt_id'], 'subject_id': r['subject_id'], 'statement': st, 'signature_hex': r['signature_hex'], 'key_id': r['key_id'],
                'public_key_hex': pub, 'signer_custody': r['signer_custody'], 'signer_identity': r['signer_identity'], 'created_at': r['created_at'],
                'roles': 'a provider receipt, a verification record, an acceptance decision and a settlement receipt are four separate claims; none implies the others'}

    def list_receipts(self, db, principal, aid):
        award = self.award(db, principal, aid); self.tick(db, aid)
        return [self.receipt_view(db, principal, r['id']) for r in db.execute('SELECT id FROM work_receipts WHERE award_id=? ORDER BY rowid', (aid,)).fetchall()]

    def verify_receipt(self, db, principal, rid, body=None):
        """Structure, signature under a supplied or the instance trust root, and links — reported separately (§27)."""
        body = body or {}
        r = db.execute('SELECT * FROM work_receipts WHERE id=? AND workspace=?', (rid, principal.workspace)).fetchone()
        if r is None:
            raise ServiceError('NOT_FOUND', 'receipt')
        st = json.loads(r['statement_json'])
        checks = [{'check': 'structure', 'ok': st.get('schema') == RECEIPT_SCHEMA and st.get('receipt_id') == rid and st.get('kind') == r['kind'], 'detail': st.get('schema')}]
        trust = body.get('trust_root') or db.execute("SELECT value FROM meta WHERE key='service_signing_public'").fetchone()['value']
        checks.append({'check': 'signature_under_trust_root', 'ok': crypto.verify(trust, r['statement_json'].encode(), r['signature_hex']) and crypto.key_id_for(trust) == r['key_id'], 'detail': {'key_id': r['key_id'], 'trust_root_key_id': crypto.key_id_for(trust)}})
        award = db.execute('SELECT id, terms_digest FROM work_awards WHERE id=?', (r['award_id'],)).fetchone()
        checks.append({'check': 'links', 'ok': award is not None and award['terms_digest'] == st.get('terms_digest'), 'detail': {'award': r['award_id'], 'terms_digest': st.get('terms_digest')}})
        avail = {'provider': 'available' if r['kind'] == 'provider' else None}
        vrec = db.execute("SELECT id FROM work_receipts WHERE award_id=? AND kind='verification' AND milestone_id=?", (r['award_id'], r['milestone_id'])).fetchone()
        return {'receipt_id': rid, 'checks': checks, 'valid_structure': checks[0]['ok'], 'valid_signature': checks[1]['ok'], 'links_valid': checks[2]['ok'],
                'scope': {'verification_record_present': vrec is not None, 'note': 'a missing verification record stays missing; a provider sentence cannot supply it', 'private_payloads': 'not disclosed by receipt inspection'}}

    # ---- evaluation, decisions, entitlements (§13–§14, §29) --------------------------------------------------------------------
    def evaluate(self, db, principal, aid, key):
        award = self.award(db, principal, aid); self.tick(db, aid)
        ms = self.milestone(db, principal, aid, key)
        t, terms = _terms(db, award['terms_id'])
        party = self._party(db, principal, award)
        job = db.execute('SELECT * FROM jobs WHERE id=?', (ms['job_id'],)).fetchone() if ms['job_id'] else None
        ev = acceptance_mod.evaluate(db, terms, key, job, provider_identity=party['provider_row']['principal_id'], provider_execution=json.loads(party['provider_row']['execution_json'])['type'])
        if not (party['requester'] or party['reviewer'] or principal.can('work:award')):
            ev = dict(ev, trace=[{k: v for k, v in x.items() if k != 'detail'} for x in ev['trace']])
        return dict(ev, award_id=aid, milestone_state=ms['state'], current_decision_id=ms['decision_id'], entitlement_id=ms['entitlement_id'])

    def decide(self, db, principal, aid, key, body):
        """Explicit authorized acceptance transition. `accept` needs an accepted candidate; `reject` needs a reason and is
        recorded as the requester's decision (a dispute can supersede it). Decisions are append-only."""
        principal.require('work:accept')
        award = self.award(db, principal, aid)
        if principal.id != award['awarded_by']:
            raise ServiceError('FORBIDDEN', 'only the requester decides acceptance')
        ms = self.milestone(db, principal, aid, key)
        if ms['state'] not in ('delivered', 'executing'):
            raise ServiceError('CONFLICT', {'code': 'milestone_state', 'state': ms['state'], 'note': 'decisions apply to delivered milestones; later changes go through a dispute'})
        want = body.get('decision')
        if want not in ('accept', 'reject'):
            raise ServiceError('VALIDATION', {'code': 'decision', 'allowed': ['accept', 'reject']})
        ev = self.evaluate(db, principal, aid, key)
        if body.get('expected_evidence_root') and body['expected_evidence_root'] != ev['evidence_root']:
            raise ServiceError('STATE_CONFLICT', {'code': 'evidence_changed', 'current': ev['evidence_root']})
        if want == 'accept' and ev['decision_candidate'] != 'accepted':
            raise ServiceError('CONFLICT', {'code': 'candidate_not_accepted', 'candidate': ev['decision_candidate'], 'reason': ev['reason'], 'unresolved': [x['predicate'] for x in ev['trace'] if x['result'] in ('unknown', 'failed')]})
        if want == 'reject' and ev['decision_candidate'] == 'pending':
            raise ServiceError('CONFLICT', {'code': 'candidate_pending', 'reason': ev['reason']})
        reason = body.get('reason')
        if want == 'reject' and ev['decision_candidate'] == 'accepted' and (type(reason) is not str or not reason.strip()):
            raise ServiceError('VALIDATION', {'code': 'reason_required', 'note': 'rejecting an evidence-accepted candidate is recorded as the requester\'s decision with a reason; it can be disputed'})
        decision = 'accepted' if want == 'accept' else 'rejected'
        pay_class = ev['payment_class'] if decision == 'accepted' else ('diagnostic' if ev['payment_class'] == 'diagnostic' else 'none')
        amount = ev['payable_amount'] if pay_class != 'none' else 0
        return self._record_decision(db, principal, award, ms, ev, decision, pay_class, amount, 'requester', reason, supersedes=None, dispute_id=None)

    def _record_decision(self, db, principal, award, ms, ev, decision, pay_class, amount, authority, reason, supersedes, dispute_id):
        did = 'wd_' + secrets.token_hex(8)
        t, terms = _terms(db, award['terms_id'])
        st = {'schema': DECISION_SCHEMA, 'decision_id': did, 'award_id': award['id'], 'milestone': ms['key'], 'terms_digest': award['terms_digest'], 'evidence_root': ev['evidence_root'], 'job_id': ev['job_id'], 'decision': decision, 'candidate': ev['decision_candidate'],
              'execution': ev['execution'], 'science': ev['science'], 'payment_class': pay_class, 'payable_amount': amount, 'asset': terms['payment']['asset'], 'policy_digest': ev['policy_digest'], 'trace': [{k: x[k] for k in ('predicate', 'type', 'result')} for x in ev['trace']],
              'authority': authority, 'decided_by': principal.id, 'reason': reason, 'supersedes': supersedes, 'dispute_id': dispute_id, 'decided_at': now()}
        db.execute('INSERT INTO work_decisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (did, award['workspace'], award['id'], ms['id'], json.dumps(ev), decision, pay_class, amount, ev['evidence_root'], ev['policy_digest'], principal.id, authority, supersedes, None, dispute_id, now()))
        if supersedes:
            db.execute('UPDATE work_decisions SET superseded_by=? WHERE id=?', (did, supersedes))
        self._receipt(db, 'acceptance', award, ms, None, did, st, principal.id)
        new_state = 'accepted' if decision == 'accepted' else 'rejected'
        db.execute("UPDATE work_milestones SET state=?, decision_id=?, updated_at=? WHERE id=?", (new_state, did, now(), ms['id']))
        ent_id = ms['entitlement_id']
        if amount > 0:
            ent_id = self._entitlement(db, award, ms, did, amount, terms)
        elif ent_id:
            db.execute("UPDATE work_entitlements SET state='void', updated_at=? WHERE id=? AND state='payable'", (now(), ent_id))
        if getattr(self, 'money', None) is not None:
            self.money.on_decision(db, award, ms, did, amount, terms)
        if decision == 'accepted' and getattr(self, 'missions', None) is not None:
            self.missions.record_contribution(db, award, ms, did, ev, terms)
        history.record(db, award['workspace'], principal.id, 'work.decision', 'work_decision', did, {'award_id': award['id'], 'milestone': ms['key'], 'decision': decision, 'execution': ev['execution'], 'science': ev['science'], 'payment_class': pay_class, 'payable_amount': amount, 'authority': authority, 'supersedes': supersedes, 'dispute_id': dispute_id})
        self.board.dispatch_ready(db, award['id'])
        self._maybe_close(db, award['id'])
        return self.decision_view(db, principal, did)

    def _entitlement(self, db, award, ms, did, amount, terms):
        """Stable entitlement identity per milestone (UNIQUE): re-signing, a new salt, another archive, key rotation or a lost
        response cannot create a second payable claim. Amount is bound to the decision."""
        ex = db.execute("SELECT * FROM work_entitlements WHERE milestone_id=? AND kind='provider'", (ms['id'],)).fetchone()
        if ex is not None:
            if ex['state'] in ('paid', 'exposed', 'submitted', 'authorized'):
                db.execute("UPDATE work_entitlements SET decision_id=?, updated_at=? WHERE id=?", (did, now(), ex['id']))
                return ex['id']
            db.execute("UPDATE work_entitlements SET decision_id=?, amount=?, state='payable', updated_at=? WHERE id=?", (did, amount, now(), ex['id']))
            return ex['id']
        eid = 'wen_' + secrets.token_hex(8)
        db.execute("INSERT INTO work_entitlements VALUES (?,?,?,?,?,?,?,?,?,?,NULL,?,?,'provider')", (eid, award['workspace'], award['id'], ms['id'], did, award['pay_to'], amount, terms['payment']['asset'], terms['payment']['scale'], 'payable', now(), now()))
        db.execute('UPDATE work_milestones SET entitlement_id=? WHERE id=?', (eid, ms['id']))
        history.record(db, award['workspace'], 'service', 'work.entitlement', 'work_entitlement', eid, {'award_id': award['id'], 'milestone': ms['key'], 'amount': amount, 'asset': terms['payment']['asset'], 'recipient_bound': True})
        return eid

    def _maybe_close(self, db, aid):
        states = [m['state'] for m in db.execute('SELECT state FROM work_milestones WHERE award_id=?', (aid,)).fetchall()]
        if states and all(s in ('accepted', 'rejected', 'cancelled', 'superseded') for s in states):
            db.execute("UPDATE work_awards SET state='delivered', updated_at=? WHERE id=? AND state IN ('executing','acknowledged','awarded')", (now(), aid))

    def decision_view(self, db, principal, did):
        principal.require('work:read')
        d = db.execute('SELECT * FROM work_decisions WHERE id=? AND workspace=?', (did, principal.workspace)).fetchone()
        if d is None:
            raise ServiceError('NOT_FOUND', 'decision')
        ev = json.loads(d['evaluation_json'])
        award = db.execute('SELECT * FROM work_awards WHERE id=?', (d['award_id'],)).fetchone(); party = self._party(db, principal, award)
        if not (party['requester'] or party['provider'] or party['reviewer'] or principal.can('work:award')):
            ev = {'decision_candidate': ev['decision_candidate'], 'science': ev['science'], 'execution': ev['execution']}
        ent = db.execute("SELECT * FROM work_entitlements WHERE milestone_id=? AND kind='provider'", (d['milestone_id'],)).fetchone()
        return {'id': did, 'award_id': d['award_id'], 'milestone_id': d['milestone_id'], 'decision': d['decision'], 'payment_class': d['payment_class'], 'payable_amount': d['payable_amount'], 'evidence_root': d['evidence_root'], 'policy_digest': d['policy_digest'],
                'decided_by': d['decided_by'], 'authority': d['authority'], 'supersedes': d['supersedes'], 'superseded_by': d['superseded_by'], 'dispute_id': d['dispute_id'], 'created_at': d['created_at'], 'evaluation': ev,
                'entitlement': ({'id': ent['id'], 'state': ent['state'], 'amount': ent['amount'], 'asset': ent['asset']} if ent else None), 'current': d['superseded_by'] is None}

    def decisions(self, db, principal, aid, key):
        ms = self.milestone(db, principal, aid, key)
        return [self.decision_view(db, principal, d['id']) for d in db.execute('SELECT id FROM work_decisions WHERE milestone_id=? ORDER BY rowid', (ms['id'],)).fetchall()]

    def entitlement_view(self, db, principal, eid):
        principal.require('work:read')
        e = db.execute('SELECT * FROM work_entitlements WHERE id=? AND workspace=?', (eid, principal.workspace)).fetchone()
        if e is None:
            raise ServiceError('NOT_FOUND', 'entitlement')
        award = db.execute('SELECT * FROM work_awards WHERE id=?', (e['award_id'],)).fetchone(); party = self._party(db, principal, award)
        ms = db.execute('SELECT key FROM work_milestones WHERE id=?', (e['milestone_id'],)).fetchone()
        return {'id': eid, 'award_id': e['award_id'], 'milestone': ms['key'], 'milestone_id': e['milestone_id'], 'decision_id': e['decision_id'], 'recipient': e['recipient'] if (party['requester'] or party['provider'] or principal.can('work:award')) else None,
                'amount': e['amount'], 'asset': e['asset'], 'scale': e['scale'], 'state': e['state'], 'payment_intent_id': e['payment_intent_id'], 'created_at': e['created_at'], 'updated_at': e['updated_at'],
                'identity': 'one entitlement per payable milestone; distinct from evidence hashes, transport ids, attempt ids and payment identifiers'}

    # ---- verifier assignment (§30) and challenges (§31) ------------------------------------------------------------------
    def verify(self, db, principal, aid, key, body):
        """Request a verification of the milestone's evidence through the verification service. Requester, reviewer, or the
        provider itself (self-verification is recorded as such and satisfies only policies that do not require a distinct verifier)."""
        award = self.award(db, principal, aid); ms = self.milestone(db, principal, aid, key)
        party = self._party(db, principal, award)
        if not (party['requester'] or party['reviewer'] or party['provider'] or principal.can('verification:submit')):
            raise ServiceError('FORBIDDEN', 'verification request')
        if not ms['job_id']:
            raise ServiceError('CONFLICT', 'nothing delivered yet')
        t, terms = _terms(db, award['terms_id'])
        cls = body.get('class') or terms['acceptance']['required_verification']['class']
        if cls == 'none':
            raise ServiceError('VALIDATION', {'code': 'class', 'note': 'the policy requires no verification; choose a class explicitly to add one'})
        if terms['acceptance']['required_verification'].get('distinct_verifier') and party['provider'] and not party['requester']:
            return {'blocked': True, 'code': 'distinct_verifier_required', 'note': 'the policy requires a verifier identity distinct from the provider; a self-requested audit under service custody cannot satisfy it; ask the requester or reviewer', 'award_id': aid, 'milestone': key}
        from ..verification import _elevated
        req = _elevated(principal, {'verification:submit', 'contract:create', 'contract:freeze', 'job:submit'}) if not principal.can('verification:submit') else principal
        v = self.svc.verification.request(db, req, ms['job_id'], cls, body.get('params'))
        history.record(db, award['workspace'], principal.id, 'work.verification_requested', 'work_milestone', ms['id'], {'award_id': aid, 'verification_id': v['id'], 'class': cls, 'self_verification': party['provider'] and not party['requester']})
        return dict(v, award_id=aid, milestone=key, self_verification=party['provider'] and not party['requester'], independence='same host and operator; separate process only when the provider executed on an enrolled node')

    # ---- disputes and corrections (§35–§36) ---------------------------------------------------------------------------------
    def dispute_row(self, db, principal, did):
        d = db.execute('SELECT * FROM work_disputes WHERE id=? AND workspace=?', (did, principal.workspace)).fetchone()
        if d is None:
            raise ServiceError('NOT_FOUND', 'dispute')
        return d

    def _entry(self, db, d, kind, actor, body):
        eid = 'wde_' + secrets.token_hex(6)
        db.execute('INSERT INTO work_dispute_entries VALUES (?,?,?,?,?,?)', (eid, d['id'], kind, actor, json.dumps(body), now()))
        history.record(db, d['workspace'], actor, 'work.dispute', 'work_dispute', d['id'], {'entry': kind, 'state': d['state']})
        return eid

    def open_dispute(self, db, principal, aid, key, body):
        principal.require('work:dispute')
        award = self.award(db, principal, aid); ms = self.milestone(db, principal, aid, key); party = self._party(db, principal, award)
        if not (party['requester'] or party['provider']):
            raise ServiceError('FORBIDDEN', 'only a party to the award opens a dispute')
        t, terms = _terms(db, award['terms_id'])
        scope = body.get('scope', 'acceptance')
        if scope not in ('acceptance', 'evidence', 'charge'):
            raise ServiceError('VALIDATION', {'code': 'scope', 'allowed': ['acceptance', 'evidence', 'charge']})
        if ms['state'] not in ('accepted', 'rejected', 'delivered'):
            raise ServiceError('CONFLICT', {'code': 'milestone_state', 'state': ms['state']})
        if db.execute("SELECT id FROM work_disputes WHERE milestone_id=? AND state NOT IN ('closed')", (ms['id'],)).fetchone():
            raise ServiceError('CONFLICT', 'a dispute is already open for this milestone')
        if ms['decision_id']:
            dec = db.execute('SELECT created_at FROM work_decisions WHERE id=?', (ms['decision_id'],)).fetchone()
            if now() > dec['created_at'] + terms['dispute']['window_seconds']:
                raise ServiceError('EXPIRED', {'code': 'dispute_window_closed', 'window_seconds': terms['dispute']['window_seconds']})
        claim = body.get('claim')
        if type(claim) is not str or not 1 <= len(claim) <= 2000:
            raise ServiceError('VALIDATION', 'claim: 1..2000 characters')
        snapshot = {'evidence_root': ms['evidence_root'], 'decision_id': ms['decision_id'], 'job_id': ms['job_id'], 'verifications': [v['id'] for v in db.execute('SELECT id FROM verification_jobs WHERE target_job_id=?', (ms['job_id'],)).fetchall()] if ms['job_id'] else [],
                    'receipts': [r['id'] for r in db.execute('SELECT id FROM work_receipts WHERE milestone_id=?', (ms['id'],)).fetchall()], 'frozen_at': now(), 'note': 'the original submission stays as it was; supplements are separate records'}
        did = 'wdp_' + secrets.token_hex(8)
        resolver = terms['dispute']['resolver']
        db.execute('INSERT INTO work_disputes VALUES (?,?,?,?,?,?,?,?,?,?,?,NULL,NULL,NULL,NULL,?,?)', (did, award['workspace'], aid, ms['id'], scope, principal.id, 'open', json.dumps(snapshot), now() + terms['dispute']['window_seconds'], resolver, json.dumps({'appeal': terms['dispute']['appeal'], 'max_entries': terms['dispute']['max_entries'], 'at_deadline': terms['dispute'].get('at_deadline', 'close_unresolved')}), now(), now()))
        db.execute("UPDATE work_milestones SET state='disputed', updated_at=? WHERE id=?", (now(), ms['id']))
        if getattr(self, 'access', None) is not None:
            self.access.hold(db, aid, ms['id'], principal.id, 'dispute:' + did, 'evidence needed by an open dispute (relevant authorized evidence only)')
        if ms['entitlement_id']:
            db.execute("UPDATE work_entitlements SET state='held', updated_at=? WHERE id=? AND state='payable'", (now(), ms['entitlement_id']))
        d = self.dispute_row(db, principal, did)
        self._entry(db, d, 'open', principal.id, {'scope': scope, 'claim': claim, 'pauses': ['acceptance transitions', 'release of the reserved obligation', 'new downstream work'], 'does_not_pause': ['an already settled transfer', 'signatures held elsewhere']})
        history.record(db, award['workspace'], principal.id, 'work.dispute_opened', 'work_dispute', did, {'award_id': aid, 'milestone': key, 'scope': scope, 'resolver': resolver, 'deadline_at': now() + terms['dispute']['window_seconds']})
        return self.dispute_view(db, principal, did)

    def dispute_view(self, db, principal, did):
        principal.require('work:read')
        d = self.dispute_row(db, principal, did)
        award = db.execute('SELECT * FROM work_awards WHERE id=?', (d['award_id'],)).fetchone(); party = self._party(db, principal, award)
        entries = [dict(e, body=json.loads(e['body_json'])) for e in db.execute('SELECT id, kind, actor, body_json, created_at FROM work_dispute_entries WHERE dispute_id=? ORDER BY rowid', (did,)).fetchall()]
        for e in entries:
            e.pop('body_json', None)
        ms = db.execute('SELECT key, state FROM work_milestones WHERE id=?', (d['milestone_id'],)).fetchone()
        out = {'id': did, 'award_id': d['award_id'], 'milestone': ms['key'], 'milestone_state': ms['state'], 'scope': d['scope_type'], 'opened_by': d['opened_by'], 'state': d['state'], 'deadline_at': d['deadline_at'], 'resolver': d['resolver'], 'policy': json.loads(d['policy_json']),
               'decision': json.loads(d['decision_json']) if d['decision_json'] else None, 'decided_by': d['decided_by'], 'closed_at': d['closed_at'], 'close_reason': d['close_reason'], 'created_at': d['created_at'], 'updated_at': d['updated_at'],
               'timeline': entries if (party['requester'] or party['provider'] or party['reviewer'] or principal.can('work:award')) else [{'kind': e['kind'], 'created_at': e['created_at']} for e in entries], 'snapshot': json.loads(d['snapshot_json'])}
        return out

    def dispute_action(self, db, principal, did, action, body):
        d = self.dispute_row(db, principal, did)
        award = db.execute('SELECT * FROM work_awards WHERE id=?', (d['award_id'],)).fetchone(); party = self._party(db, principal, award)
        ms = db.execute('SELECT * FROM work_milestones WHERE id=?', (d['milestone_id'],)).fetchone()
        pol = json.loads(d['policy_json'])
        n_entries = db.execute('SELECT COUNT(*) FROM work_dispute_entries WHERE dispute_id=?', (did,)).fetchone()[0]
        if action in ('respond', 'evidence', 'recheck', 'appeal') and n_entries >= pol['max_entries']:
            raise ServiceError('CONFLICT', {'code': 'dispute_entry_limit', 'max_entries': pol['max_entries']})
        if d['state'] == 'closed' and action != 'close':
            raise ServiceError('CONFLICT', {'code': 'dispute_closed', 'note': 'a closed dispute never changes; a configured appeal opens a linked decision, not a rewrite'})
        if action == 'respond':
            if not (party['requester'] or party['provider']):
                raise ServiceError('FORBIDDEN', 'parties only')
            text = body.get('text')
            if type(text) is not str or not 1 <= len(text) <= 2000:
                raise ServiceError('VALIDATION', 'text')
            self._entry(db, d, 'response', principal.id, {'text': text})
            db.execute("UPDATE work_disputes SET state='responded', updated_at=? WHERE id=? AND state='open'", (now(), did))
        elif action == 'evidence':
            if not (party['requester'] or party['provider'] or party['reviewer']):
                raise ServiceError('FORBIDDEN', 'parties or reviewer only')
            ref = body.get('verification_id')
            v = db.execute('SELECT * FROM verification_jobs WHERE id=? AND workspace=?', (ref, principal.workspace)).fetchone() if ref else None
            if v is None or v['target_job_id'] != ms['job_id']:
                raise ServiceError('VALIDATION', {'code': 'supplement', 'note': 'supplemental evidence references a verification of the disputed job; the original artifact is never replaced'})
            self._entry(db, d, 'evidence', principal.id, {'verification_id': v['id'], 'class': v['class'], 'state': v['state'], 'result_commitment': v['result_commitment'], 'same_evidence': v['result_commitment'] == ms['evidence_root']})
        elif action == 'recheck':
            if not (party['requester'] or party['provider'] or party['reviewer']):
                raise ServiceError('FORBIDDEN', 'parties or reviewer only')
            # bounded diagnostic recheck (§34): compare inputs, method versions and canonical forms first; then rerun the required class
            t, terms = _terms(db, award['terms_id'])
            job = db.execute('SELECT * FROM jobs WHERE id=?', (ms['job_id'],)).fetchone(); contract = db.execute('SELECT * FROM contracts WHERE id=?', (job['contract_id'],)).fetchone()
            expected = next((m.get('operation') for m in terms['milestones'] if m['key'] == ms['key']), None) or terms['operation']
            diag = {'input_root_matches_terms': contract['input_root'] == expected['input_root'], 'contract_digest_matches_terms': contract['contract_digest'] == expected['contract_digest'],
                    'method_version': {'terms': terms['operation'].get('verifier_digest'), 'contract': json.loads(contract['contract_json']).get('verifier_digest')}, 'evidence_root_unchanged': job['evidence_root'] == ms['evidence_root']}
            diag['method_version_matches'] = diag['method_version']['terms'] == diag['method_version']['contract']
            cls = body.get('class') or terms['acceptance']['required_verification']['class']
            v = None
            if all(diag[k] for k in ('input_root_matches_terms', 'contract_digest_matches_terms', 'method_version_matches', 'evidence_root_unchanged')) and cls != 'none':
                from ..verification import _elevated
                req = _elevated(principal, {'verification:submit', 'contract:create', 'contract:freeze', 'job:submit'})
                v = self.svc.verification.request(db, req, ms['job_id'], cls, body.get('params'))
            self._entry(db, d, 'recheck', principal.id, {'diagnostic': diag, 'rerun_verification_id': v['id'] if v else None, 'class': cls, 'note': 'a mismatch of inputs or method versions is not a scientific disagreement'})
            db.execute("UPDATE work_disputes SET state='recheck', updated_at=? WHERE id=?", (now(), did))
        elif action == 'decide':
            self._decide_dispute(db, principal, d, award, ms, body, party)
        elif action == 'appeal':
            if not pol['appeal']:
                raise ServiceError('CONFLICT', {'code': 'appeal_not_configured'})
            if d['state'] != 'decided' or not (party['requester'] or party['provider']):
                raise ServiceError('CONFLICT', {'code': 'appeal_state', 'state': d['state']})
            self._entry(db, d, 'appeal', principal.id, {'text': str(body.get('text', ''))[:2000]})
            db.execute("UPDATE work_disputes SET state='appealed', updated_at=? WHERE id=?", (now(), did))
        elif action == 'close':
            if not (party['requester'] or party['reviewer'] or principal.can('work:resolve')):
                raise ServiceError('FORBIDDEN', 'close')
            if d['state'] not in ('decided', 'appealed', 'open', 'responded', 'recheck'):
                raise ServiceError('CONFLICT', {'code': 'dispute_state', 'state': d['state']})
            reason = str(body.get('reason', 'closed'))[:256]
            unresolved = d['state'] not in ('decided',)
            db.execute("UPDATE work_disputes SET state='closed', closed_at=?, close_reason=?, updated_at=? WHERE id=?", (now(), reason + (' (unresolved)' if unresolved else ''), now(), did))
            if getattr(self, 'access', None) is not None:
                self.access.release_holds(db, ms['id'], 'dispute closed')
            if unresolved:
                # deadline / unresolved: the milestone returns to its last decided state; a held entitlement stays held until reconciled
                prev = 'accepted' if ms['decision_id'] and db.execute('SELECT decision FROM work_decisions WHERE id=?', (ms['decision_id'],)).fetchone()['decision'] == 'accepted' else ('rejected' if ms['decision_id'] else 'delivered')
                db.execute("UPDATE work_milestones SET state=?, updated_at=? WHERE id=? AND state='disputed'", (prev, now(), ms['id']))
            self._entry(db, d, 'close', principal.id, {'reason': reason, 'unresolved': unresolved})
        else:
            raise ServiceError('VALIDATION', {'code': 'action', 'allowed': ['respond', 'evidence', 'recheck', 'decide', 'appeal', 'close']})
        return self.dispute_view(db, principal, did)

    def _decide_dispute(self, db, principal, d, award, ms, body, party):
        outcome = body.get('outcome')
        if outcome not in DISPUTE_OUTCOMES:
            raise ServiceError('VALIDATION', {'code': 'outcome', 'allowed': list(DISPUTE_OUTCOMES)})
        if d['resolver'] == 'designated_reviewer':
            if not (principal.role == 'reviewer' and principal.can('work:resolve')):
                raise ServiceError('FORBIDDEN', {'code': 'resolver_authority', 'required': 'the designated reviewer (role reviewer)', 'note': 'technical ability to change records is not contractual authority'})
        elif d['resolver'] == 'requester_reviewer_pair':
            if not (principal.role == 'reviewer' and principal.can('work:resolve')):
                raise ServiceError('FORBIDDEN', {'code': 'resolver_authority', 'required': 'reviewer decides; the requester opened or responded'})
        elif d['resolver'] == 'deterministic_replay':
            if not (party['requester'] or party['reviewer']):
                raise ServiceError('FORBIDDEN', 'replay resolution is applied by the requester or reviewer from a recorded recheck')
        if d['state'] in ('decided', 'closed'):
            raise ServiceError('CONFLICT', {'code': 'already_decided', 'note': 'a second decision after closure needs the configured appeal'})
        if body.get('award_id') and body['award_id'] != award['id']:
            raise ServiceError('VALIDATION', {'code': 'wrong_contract', 'note': 'the resolution references another award'})
        ev = self.evaluate(db, principal, award['id'], ms['key'])
        if d['resolver'] == 'deterministic_replay':
            outcome = {'accepted': 'supersede_acceptance', 'rejected': 'reverse_acceptance', 'pending': 'unresolved'}[ev['decision_candidate']]
        reason = str(body.get('reason', ''))[:2000]
        cur = db.execute('SELECT * FROM work_decisions WHERE id=?', (ms['decision_id'],)).fetchone() if ms['decision_id'] else None
        result = {'outcome': outcome, 'reason': reason, 'resolver': d['resolver'], 'decided_by': principal.id, 'evaluation_candidate': ev['decision_candidate'], 'evidence_root': ev['evidence_root'], 'decided_at': now(), 'monetary_consequence': None, 'follow_up': None}
        if outcome == 'supersede_acceptance':
            if ev['decision_candidate'] != 'accepted':
                raise ServiceError('CONFLICT', {'code': 'candidate_not_accepted', 'candidate': ev['decision_candidate'], 'note': 'a superseding acceptance needs evidence that the policy accepts (e.g. a passed replay added to the dispute)'})
            newd = self._record_decision(db, principal, award, ms, ev, 'accepted', ev['payment_class'], ev['payable_amount'], 'dispute_resolution', reason, supersedes=cur['id'] if cur else None, dispute_id=d['id'])
            result['monetary_consequence'] = {'entitlement_id': newd['entitlement']['id'] if newd.get('entitlement') else None, 'payable_amount': ev['payable_amount']}
            result['follow_up'] = 'settle the entitlement'
        elif outcome == 'reverse_acceptance':
            newd = self._record_decision(db, principal, award, ms, ev, 'rejected', 'none', 0, 'dispute_resolution', reason, supersedes=cur['id'] if cur else None, dispute_id=d['id'])
            ent = db.execute("SELECT * FROM work_entitlements WHERE milestone_id=? AND kind='provider'", (ms['id'],)).fetchone()
            if ent and ent['state'] in ('paid', 'exposed', 'submitted'):
                db.execute("UPDATE work_entitlements SET state='refund_pending', updated_at=? WHERE id=?", (now(), ent['id']))
                result['monetary_consequence'] = {'entitlement_id': ent['id'], 'refund_required': ent['amount'], 'state': 'refund_pending', 'note': 'an obligation until a reverse transfer is observed; never a fabricated completed refund'}
            elif ent:
                db.execute("UPDATE work_entitlements SET state='void', updated_at=? WHERE id=?", (now(), ent['id']))
                result['monetary_consequence'] = {'entitlement_id': ent['id'], 'state': 'void'}
        elif outcome == 'uphold':
            db.execute("UPDATE work_milestones SET state=?, updated_at=? WHERE id=?", (cur['decision'] if cur else 'delivered', now(), ms['id']))
            ent = db.execute("SELECT * FROM work_entitlements WHERE milestone_id=? AND kind='provider'", (ms['id'],)).fetchone()
            if ent and ent['state'] == 'held':
                db.execute("UPDATE work_entitlements SET state='payable', updated_at=? WHERE id=?", (now(), ent['id']))
        else:
            result['follow_up'] = 'unresolved: the milestone stays disputed until closed by its deadline rule'
        db.execute("UPDATE work_disputes SET state='decided', decision_json=?, decided_by=?, updated_at=? WHERE id=?", (json.dumps(result), principal.id, now(), d['id']))
        self._entry(db, d, 'decision', principal.id, result)
        history.record(db, award['workspace'], principal.id, 'work.dispute_decided', 'work_dispute', d['id'], {'outcome': outcome, 'resolver': d['resolver'], 'award_id': award['id']})

    def expire_disputes(self, db):
        n = 0
        for d in db.execute("SELECT * FROM work_disputes WHERE state NOT IN ('closed') AND deadline_at < ?", (now(),)).fetchall():
            pol = json.loads(d['policy_json'])
            ms = db.execute('SELECT * FROM work_milestones WHERE id=?', (d['milestone_id'],)).fetchone()
            row = db.execute('SELECT * FROM principals WHERE id=?', (d['opened_by'],)).fetchone(); p = auth.Principal(row); p.scope = None
            if d['state'] == 'decided' or pol['at_deadline'] == 'accept_last_decision' and d['decision_json']:
                db.execute("UPDATE work_disputes SET state='closed', closed_at=?, close_reason='deadline: last decision stands', updated_at=? WHERE id=?", (now(), now(), d['id']))
            else:
                db.execute("UPDATE work_disputes SET state='closed', closed_at=?, close_reason='deadline reached unresolved', updated_at=? WHERE id=?", (now(), now(), d['id']))
                prev = 'accepted' if ms['decision_id'] and db.execute('SELECT decision FROM work_decisions WHERE id=?', (ms['decision_id'],)).fetchone()['decision'] == 'accepted' else ('rejected' if ms['decision_id'] else 'delivered')
                db.execute("UPDATE work_milestones SET state=?, updated_at=? WHERE id=? AND state='disputed'", (prev, now(), ms['id']))
            self._entry(db, d, 'close', 'scheduler', {'deadline': True, 'rule': pol['at_deadline']})
            n += 1
        return n

    def list_disputes(self, db, principal, aid=None):
        principal.require('work:read')
        sql, args = 'SELECT id FROM work_disputes WHERE workspace=?', [principal.workspace]
        if aid:
            sql += ' AND award_id=?'; args.append(aid)
        return [self.dispute_view(db, principal, d['id']) for d in db.execute(sql + ' ORDER BY created_at DESC LIMIT 100', args).fetchall()]

    # ---- reassignment (§26) and delegation (§25) --------------------------------------------------------------------------
    def reassign(self, db, principal, aid, body):
        principal.require('work:award')
        award = self.award(db, principal, aid)
        if principal.id != award['awarded_by']:
            raise ServiceError('FORBIDDEN', 'requester only')
        t, terms = _terms(db, award['terms_id'])
        if not terms['reassignment']['allowed']:
            raise ServiceError('CONFLICT', {'code': 'reassignment_not_permitted'})
        self.tick(db, aid)
        cond = body.get('condition')
        if cond not in terms['reassignment']['conditions']:
            raise ServiceError('VALIDATION', {'code': 'condition', 'allowed': terms['reassignment']['conditions']})
        attempts = db.execute("SELECT * FROM work_attempts WHERE award_id=? ORDER BY started_at", (aid,)).fetchall()
        if cond == 'missed_acknowledgement' and (award['acknowledged_at'] is not None or now() <= award['ack_deadline']):
            raise ServiceError('CONFLICT', {'code': 'acknowledgement_not_missed', 'ack_deadline': award['ack_deadline'], 'acknowledged_at': award['acknowledged_at']})
        if cond == 'terminal_failure' and not any(a['state'] == 'failed' for a in attempts):
            raise ServiceError('CONFLICT', {'code': 'no_terminal_failure'})
        if terms['reassignment'].get('physical_effects', 'none_repeatable_computation') != 'none_repeatable_computation' and not body.get('physical_policy_confirmed'):
            raise ServiceError('CONFLICT', {'code': 'physical_effects_policy_required', 'note': 'automatic repetition may be unsafe; the declared policy must be confirmed'})
        # what the old provider may still do: nothing that becomes current. Running jobs are cancelled (fenced by lease), inputs stay readable only for its evidence retention
        ent_states = [e['state'] for e in db.execute('SELECT state FROM work_entitlements WHERE award_id=?', (aid,)).fetchall()]
        exposure = [s for s in ent_states if s in ('paid', 'submitted', 'exposed', 'authorized', 'held')]
        for m in db.execute('SELECT * FROM work_milestones WHERE award_id=? AND state IN (?,?,?)', (aid, 'pending', 'executing', 'delivered')).fetchall():
            if m['job_id']:
                job = db.execute('SELECT state FROM jobs WHERE id=?', (m['job_id'],)).fetchone()
                if job['state'] in ('queued', 'running'):
                    self.svc.jobs.cancel(db, principal, m['job_id'])
            db.execute("UPDATE work_milestones SET state='superseded', blocked_reason=?, updated_at=? WHERE id=?", ('award reassigned (%s)' % cond, now(), m['id']))
        db.execute("UPDATE work_awards SET state='reassigned', active=0, closed_at=?, close_reason=?, updated_at=? WHERE id=?", (now(), 'reassigned: ' + cond, now(), aid))
        if not exposure:
            budgets.settle(db, 'work_award', aid, 'release')
            db.execute("UPDATE work_awards SET reserved=0 WHERE id=?", (aid,))
        history.record(db, award['workspace'], principal.id, 'work.award_state', 'work_award', aid, {'state': 'reassigned', 'condition': cond, 'reserve_released': not exposure, 'exposure_retained': exposure})
        out = {'old_award': aid, 'condition': cond, 'reserve_released': not exposure, 'exposure_retained': exposure, 'old_evidence_preserved': True}
        oid = body.get('offer_id')
        if oid:
            o = db.execute('SELECT * FROM work_offers WHERE id=? AND request_id=?', (oid, award['request_id'])).fetchone()
            if o is None or o['expires_at'] < now() or o['state'] not in ('offered', 'superseded'):
                raise ServiceError('CONFLICT', {'code': 'replacement_offer_not_awardable', 'state': o['state'] if o else None})
            db.execute("UPDATE work_offers SET state='offered', updated_at=? WHERE id=?", (now(), oid))
            db.execute("UPDATE work_requests SET state='open', updated_at=? WHERE id=?", (now(), award['request_id']))
            new = self.board.award(db, principal, award['request_id'], {'offer_id': oid, 'reason': body.get('reason', 'reassignment after ' + cond)})
            db.execute("UPDATE work_awards SET replaced_by=? WHERE id=?", (new['id'], aid))
            out['new_award'] = new
        return out

    def delegate(self, db, principal, aid, key, body):
        principal.require('work:deliver')
        award = self.award(db, principal, aid); ms = self.milestone(db, principal, aid, key); party = self._party(db, principal, award)
        if not party['provider']:
            raise ServiceError('FORBIDDEN', 'the awarded provider delegates')
        t, terms = _terms(db, award['terms_id']); dg = terms['delegation']
        if not dg['allowed']:
            raise ServiceError('CONFLICT', {'code': 'delegation_not_permitted'})
        delegate = db.execute('SELECT * FROM providers WHERE id=? AND workspace=?', (body.get('provider_id'), principal.workspace)).fetchone()
        if delegate is None or delegate['state'] != 'active' or delegate['id'] == award['provider_id']:
            raise ServiceError('VALIDATION', {'code': 'delegate', 'note': 'a registered, active, different provider'})
        if dg.get('allowed_providers') and delegate['id'] not in dg['allowed_providers']:
            raise ServiceError('FORBIDDEN', {'code': 'delegate_not_allowed', 'allowed': dg['allowed_providers']})
        sub = body.get('sub_budget', 0)
        terms_mod._int(sub, 0, 10 ** 15, 'sub_budget')
        existing = db.execute("SELECT COALESCE(SUM(sub_budget),0), COUNT(*), COALESCE(MAX(depth),0) FROM work_delegations WHERE award_id=? AND state!='refused'", (aid,)).fetchone()
        if existing[0] + sub > dg.get('max_sub_budget', 0):
            raise ServiceError('BUDGET_EXHAUSTED', {'code': 'delegation_budget', 'max_sub_budget': dg.get('max_sub_budget', 0), 'already': existing[0], 'requested': sub, 'note': 'refused before signing or dispatch; the requester is not charged twice'})
        if existing[1] + 1 > dg.get('max_nodes', 1):
            raise ServiceError('CONFLICT', {'code': 'delegation_nodes', 'max_nodes': dg.get('max_nodes', 1)})
        depth = body.get('depth', 1)
        if type(depth) is not int or depth > dg.get('max_depth', 1):
            raise ServiceError('CONFLICT', {'code': 'delegation_depth', 'max_depth': dg.get('max_depth', 1)})
        scope = body.get('artifact_scope', dg.get('artifact_scope', 'derived_inputs_only'))
        if scope not in ('derived_inputs_only', 'declared_subtask_inputs') or (scope == 'declared_subtask_inputs' and dg.get('artifact_scope', 'derived_inputs_only') != 'declared_subtask_inputs'):
            raise ServiceError('FORBIDDEN', {'code': 'artifact_scope_exceeds_policy', 'policy': dg.get('artifact_scope', 'derived_inputs_only'), 'note': 'delegation never broadens the provider\'s authority or discloses beyond the requester\'s policy'})
        if any(x['code'] for x in self.board.providers.eligibility(db, delegate, terms)['reasons'] if x['code'] in ('unsupported_kind', 'node_cannot_execute_kind')):
            raise ServiceError('CONFLICT', {'code': 'delegate_cannot_execute_kind'})
        if ms['state'] not in ('executing', 'pending'):
            raise ServiceError('CONFLICT', {'code': 'milestone_state', 'state': ms['state']})
        did = 'wdl_' + secrets.token_hex(6)
        db.execute('INSERT INTO work_delegations VALUES (?,?,?,?,?,?,?,?,?,?,?,?)', (did, award['workspace'], aid, ms['id'], award['provider_id'], delegate['id'], sub, depth, scope, 'active', json.dumps({'verification_policy': terms['acceptance']['required_verification'], 'remaining_deadline': ms['deadline_at'], 'costs': 'retry and verification costs borne by the primary provider; no pass-through charge to the requester'}), now()))
        # the delegate executes a new attempt for the same milestone; the old queued job is cancelled; evidence returns through the same path
        if ms['job_id']:
            job = db.execute('SELECT state FROM jobs WHERE id=?', (ms['job_id'],)).fetchone()
            if job['state'] in ('queued', 'running'):
                req = self.board._requester_principal(db, award)
                self.svc.jobs.cancel(db, req, ms['job_id'])
                db.execute("UPDATE work_attempts SET state='superseded', finished_at=?, note='delegated' WHERE milestone_id=? AND job_id=?", (now(), ms['id'], ms['job_id']))
        req = self.board._requester_principal(db, award)
        jid = self.svc.jobs.submit(db, req, ms['contract_id'], supersede=ms['job_id'])
        ex = json.loads(delegate['execution_json'])
        db.execute('UPDATE jobs SET location_policy=? WHERE id=?', (json.dumps([ex['node_id']]) if ex['type'] == 'node' else json.dumps(['local']), jid))
        gen = (db.execute('SELECT COALESCE(MAX(generation),0) FROM work_attempts WHERE milestone_id=?', (ms['id'],)).fetchone()[0] or 0) + 1
        wat = 'wat_' + secrets.token_hex(6)
        db.execute('INSERT INTO work_attempts (id, workspace, award_id, milestone_id, provider_id, generation, job_id, state, started_at, note) VALUES (?,?,?,?,?,?,?,?,?,?)', (wat, award['workspace'], aid, ms['id'], delegate['id'], gen, jid, 'dispatched', now(), 'delegation ' + did))
        db.execute("UPDATE work_milestones SET state='executing', job_id=?, updated_at=? WHERE id=?", (jid, now(), ms['id']))
        history.record(db, award['workspace'], principal.id, 'work.delegated', 'work_delegation', did, {'award_id': aid, 'milestone': key, 'delegate': delegate['id'], 'sub_budget': sub, 'depth': depth, 'scope': scope, 'job_id': jid, 'primary_remains_bound': True})
        return {'id': did, 'award_id': aid, 'milestone': key, 'delegate_provider_id': delegate['id'], 'sub_budget': sub, 'depth': depth, 'artifact_scope': scope, 'attempt': wat, 'job_id': jid,
                'obligations': {'requester_to_primary': 'unchanged (entitlement recipient stays the awarded provider)', 'primary_to_delegate': {'amount': sub, 'recorded': 'memorandum in the journal; settled outside the requester\'s obligation'}},
                'responsibility': 'the primary provider remains bound to the deliverable; the delegate\'s result returns through the same acceptance and evidence path', 'independent_verification': 'not manufactured: same custody and operator'}

    # ---- portable bundles (§32, §61) ------------------------------------------------------------------------------------
    def bundle(self, db, principal, aid, key, scope='restricted'):
        principal.require('artifact:export')
        award = self.award(db, principal, aid); ms = self.milestone(db, principal, aid, key); party = self._party(db, principal, award)
        if not (party['requester'] or principal.can('work:award')):
            raise ServiceError('FORBIDDEN', 'the requester exports bundles')
        if scope not in ('restricted', 'full'):
            raise ServiceError('VALIDATION', {'code': 'scope', 'allowed': ['restricted', 'full']})
        self.tick(db, aid)
        t, terms = _terms(db, award['terms_id'])
        offer = db.execute('SELECT * FROM work_offers WHERE id=?', (award['offer_id'],)).fetchone()
        files = {'terms.json': merkle.canonical({'terms': terms, 'digest': t['digest'], 'id': t['id']}), 'offer.json': merkle.canonical({'statement': json.loads(offer['statement_json']), 'signature_hex': offer['signature_hex'], 'key_id': offer['key_id']}),
                 'award.json': merkle.canonical({k: award[k] for k in ('id', 'request_id', 'offer_id', 'terms_id', 'terms_digest', 'provider_id', 'provider_revision', 'state', 'ceiling', 'awarded_at')} | {'selection': json.loads(award['selection_json'])}),
                 'milestone.json': merkle.canonical({k: ms[k] for k in ('id', 'key', 'state', 'max_payment', 'contract_id', 'job_id', 'evidence_root', 'decision_id', 'entitlement_id')})}
        for r in db.execute('SELECT * FROM work_receipts WHERE milestone_id=? OR (award_id=? AND milestone_id IS NULL) ORDER BY rowid', (ms['id'], aid)).fetchall():
            files['receipts/%s.json' % r['id']] = merkle.canonical({'statement': json.loads(r['statement_json']), 'signature_hex': r['signature_hex'], 'key_id': r['key_id'], 'kind': r['kind']})
        for d in db.execute('SELECT * FROM work_decisions WHERE milestone_id=? ORDER BY rowid', (ms['id'],)).fetchall():
            files['decisions/%s.json' % d['id']] = merkle.canonical({'id': d['id'], 'decision': d['decision'], 'payment_class': d['payment_class'], 'payable_amount': d['payable_amount'], 'evidence_root': d['evidence_root'], 'policy_digest': d['policy_digest'], 'supersedes': d['supersedes'], 'superseded_by': d['superseded_by'], 'authority': d['authority'], 'created_at': d['created_at']})
        for v in db.execute('SELECT * FROM verification_jobs WHERE target_job_id=? AND statement_json IS NOT NULL ORDER BY created_at', (ms['job_id'],)).fetchall() if ms['job_id'] else []:
            files['verifications/%s.json' % v['id']] = merkle.canonical({'statement': json.loads(v['statement_json']), 'signature_hex': v['signature_hex'], 'key_id': v['key_id'], 'state': v['state']})
        undisclosed = []
        job = db.execute('SELECT * FROM jobs WHERE id=?', (ms['job_id'],)).fetchone() if ms['job_id'] else None
        if job and job['evidence_artifact_id']:
            arow = db.execute('SELECT deleted_at FROM artifacts WHERE id=?', (job['evidence_artifact_id'],)).fetchone()
            if arow is None or arow['deleted_at'] is not None:
                raise ServiceError('CONFLICT', {'code': 'evidence_deleted', 'evidence_root': ms['evidence_root'], 'note': 'the commitment and receipts remain; the private artifact is no longer available for replay or export'})
            vault = self.svc.store.load_json(db, job['evidence_artifact_id'], award['workspace'])
            names = [f['name'] for f in vault['fields']]
            wanted = [n for n in ('contract_digest', 'input_root', 'verifier_id', 'verifier_digest', 'result_schema', 'model_id', 'scope') if n in names]
            if 'outcome' in terms['privacy']['evidence_disclosure'] and 'outcome' in names:
                wanted.append('outcome')
            files['evidence/disclosed.json'] = merkle.canonical(merkle.disclose(vault, wanted))
            files['evidence/summary.json'] = merkle.canonical({'job_id': job['id'], 'kind': job['kind'], 'outcome': job['outcome'] if 'outcome' in terms['privacy']['evidence_disclosure'] else 'withheld-by-policy', 'evidence_root': job['evidence_root'], 'summary': ({k: v for k, v in json.loads(job['summary_json'] or '{}').items() if k in ('output_hash', 'registered_hash', 'matches_registered', 'task_id', 'status', 'outcome')} if 'summary' in terms['privacy']['evidence_disclosure'] or job['kind'] == 'legacy_task_replay' else 'withheld-by-policy')})
            if scope == 'full':
                contract = db.execute('SELECT * FROM contracts WHERE id=?', (job['contract_id'],)).fetchone()
                files['private/evidence-vault.json'] = merkle.canonical(vault)
                files['private/input-vault.json'] = merkle.canonical(self.svc.store.load_json(db, contract['input_artifact_id'], award['workspace']))
                files['private/contract.json'] = contract['contract_json'].encode()
                files['private/README.txt'] = b'PRIVATE AUDIT PACKAGE: contains the requester inputs and full evidence; do not publish or forward.'
            else:
                undisclosed = ['inputs (commitment only)', 'full evidence vault (disclosed fields only)'] + ([] if 'outcome' in terms['privacy']['evidence_disclosure'] else ['outcome'])
        else:
            undisclosed = ['no evidence committed']
        pub = metering.ensure_service_key(self.settings, db)
        manifest = {'schema': BUNDLE_SCHEMA, 'award_id': aid, 'milestone': key, 'terms_digest': award['terms_digest'], 'evidence_root': ms['evidence_root'], 'scope': scope, 'kind': terms['operation']['kind'],
                    'file_sha256': {k: hashlib.sha256(v).hexdigest() for k, v in files.items()}, 'issuer_key_id': crypto.key_id_for(pub), 'issuer_public_key': pub, 'undisclosed': undisclosed,
                    'trust': 'signatures identify this service key (service custody); the recipient pins the key independently; nothing here is a globally witnessed ledger',
                    'replay': {'legacy_task_replay': 'recomputable offline from the task id (registered implementation)', 'energy_audit': 'recomputable only with the private input vault (full scope)', 'resource_plan': 'witness replay needs the disclosed inputs and plan'}.get(terms['operation']['kind'], 'see acceptance class'),
                    'issued_at': now()}
        canon = merkle.canonical(manifest)
        files['manifest.json'] = canon
        files['statement.json'] = merkle.canonical({'schema': BUNDLE_SCHEMA + '-statement', 'manifest_sha256': hashlib.sha256(canon).hexdigest(), 'signature': crypto.sign(crypto.load_signing_key(self.settings.keys_dir / 'service.ed25519'), canon), 'public_key': pub, 'key_id': crypto.key_id_for(pub)})
        if len(files) > BUNDLE_LIMITS['members'] or sum(len(v) for v in files.values()) > BUNDLE_LIMITS['bytes']:
            raise ServiceError('VALIDATION', 'bundle too large')
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w', compression=zipfile.ZIP_DEFLATED) as z:
            for name in sorted(files):
                zi = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0)); zi.compress_type = zipfile.ZIP_DEFLATED; zi.external_attr = 0o644 << 16
                z.writestr(zi, files[name])
        history.record(db, award['workspace'], principal.id, 'artifact.exported', 'work_award', aid, {'milestone': key, 'scope': scope, 'bundle_sha256': hashlib.sha256(buf.getvalue()).hexdigest(), 'private_included': scope == 'full'})
        return buf.getvalue(), manifest
