"""Operational views of the work economy (Order 08 §65, §67, §68, §73): bounded counts and actionable waiting reasons,
in-application notifications deduplicated by business event and recipient, interpretable economic measurements, and
the fault-injection checkpoints used by the fault campaign on disposable instances."""
import json
import secrets

from .. import history
from ..db import now
from ..errors import ServiceError

FAULT_POINTS = ('award_commit', 'reservation_posting', 'evidence_publication', 'verifier_completion', 'acceptance_decision', 'payment_signing', 'payment_submission', 'payment_observation', 'fee_credit', 'refund_observation')


def fault(db, settings, point):
    """Raise at an armed fault point (disposable instances with limits.test_hooks only). Arming is a meta row written by the
    ops/faults route; the campaign disarms it explicitly so a rollback cannot silently re-arm or clear it."""
    if not settings.limits.get('test_hooks'):
        return
    if point not in FAULT_POINTS:
        raise ServiceError('INTERNAL_DEFECT', 'unknown fault point')
    row = db.execute("SELECT value FROM meta WHERE key=?", ('fault:' + point,)).fetchone()
    if row is not None:
        raise RuntimeError('FAULT INJECTED (test hook, disposable instance): ' + point)


def counts(db, workspace):
    q = lambda sql, *a: db.execute(sql, a).fetchall()
    def by(sql, *a):
        return {r[0]: r[1] for r in q(sql, *a)}
    out = {'requests_by_state': by('SELECT state, COUNT(*) FROM work_requests WHERE workspace=? GROUP BY state', workspace),
           'offers_by_state': by('SELECT state, COUNT(*) FROM work_offers WHERE workspace=? GROUP BY state', workspace),
           'awards_by_state': by('SELECT state, COUNT(*) FROM work_awards WHERE workspace=? GROUP BY state', workspace),
           'milestones_by_state': by('SELECT state, COUNT(*) FROM work_milestones WHERE workspace=? GROUP BY state', workspace),
           'entitlements_by_state': by('SELECT state, COUNT(*) FROM work_entitlements WHERE workspace=? GROUP BY state', workspace),
           'intents_by_state': by('SELECT state, COUNT(*) FROM payment_intents WHERE workspace=? GROUP BY state', workspace),
           'disputes_by_state': by('SELECT state, COUNT(*) FROM work_disputes WHERE workspace=? GROUP BY state', workspace),
           'evidence_awaiting_verification': db.execute("SELECT COUNT(*) FROM work_milestones m WHERE m.workspace=? AND m.state='delivered' AND m.job_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM verification_jobs v WHERE v.target_job_id=m.job_id AND v.state='passed')", (workspace,)).fetchone()[0],
           'evidence_awaiting_decision': db.execute("SELECT COUNT(*) FROM work_milestones WHERE workspace=? AND state='delivered'", (workspace,)).fetchone()[0],
           'treasury_allocations_by_state': by('SELECT state, COUNT(*) FROM treasury_allocations WHERE workspace=? GROUP BY state', workspace)}
    out['pending_settlements'] = sum(v for k, v in out['intents_by_state'].items() if k in ('authorized', 'submitted', 'unknown', 'expired'))
    out['unresolved_disputes'] = sum(v for k, v in out['disputes_by_state'].items() if k != 'closed')
    return out


def waiting_reasons(db, workspace):
    """Actionable, bounded reasons (no private text, amounts, addresses or user strings)."""
    reasons = []
    for m in db.execute("SELECT m.id, m.key, m.award_id, m.state, m.blocked_reason, m.deadline_at, m.job_id FROM work_milestones m WHERE m.workspace=? AND m.state IN ('pending','executing','delivered','disputed')", (workspace,)).fetchall():
        if m['state'] == 'pending' and m['blocked_reason']:
            reasons.append({'milestone_id': m['id'], 'reason': 'dependency_not_accepted' if 'required' in m['blocked_reason'] else 'blocked', 'kind': 'gating'})
        elif m['state'] == 'executing':
            job = db.execute('SELECT state, lease_expires FROM jobs WHERE id=?', (m['job_id'],)).fetchone() if m['job_id'] else None
            if job and job['state'] == 'queued':
                reasons.append({'milestone_id': m['id'], 'reason': 'compute_capacity_or_worker_unavailable', 'kind': 'execution'})
            elif job and job['state'] == 'running' and job['lease_expires'] and job['lease_expires'] < now():
                reasons.append({'milestone_id': m['id'], 'reason': 'lease_expired_awaiting_recovery', 'kind': 'execution'})
            if m['deadline_at'] < now():
                reasons.append({'milestone_id': m['id'], 'reason': 'delivery_deadline_passed', 'kind': 'deadline'})
        elif m['state'] == 'delivered':
            v = db.execute("SELECT state FROM verification_jobs WHERE target_job_id=? ORDER BY rowid DESC LIMIT 1", (m['job_id'],)).fetchone() if m['job_id'] else None
            reasons.append({'milestone_id': m['id'], 'reason': 'awaiting_verification' if v is None or v['state'] in ('queued', 'awaiting_replica') else 'awaiting_acceptance_decision', 'kind': 'acceptance'})
        elif m['state'] == 'disputed':
            reasons.append({'milestone_id': m['id'], 'reason': 'dispute_open', 'kind': 'dispute'})
    for i in db.execute("SELECT id, state FROM payment_intents WHERE workspace=? AND state IN ('submitted','unknown','expired')", (workspace,)).fetchall():
        reasons.append({'intent_id': i['id'], 'reason': 'payment_uncertain_reconcile' if i['state'] != 'expired' else 'authorization_expired_renewal_needed', 'kind': 'payment'})
    for g in db.execute("SELECT id FROM audit_grants WHERE workspace=? AND revoked_at IS NULL AND expires_at < ?", (workspace, now())).fetchall():
        reasons.append({'grant_id': g['id'], 'reason': 'grant_expired', 'kind': 'access'})
    for r in db.execute("SELECT id FROM work_requests WHERE workspace=? AND state='open'", (workspace,)).fetchall():
        n = db.execute("SELECT COUNT(*) FROM work_offers WHERE request_id=? AND state='offered'", (r['id'],)).fetchone()[0]
        reasons.append({'request_id': r['id'], 'reason': 'offers_awaiting_selection' if n else 'no_eligible_offer_yet', 'kind': 'board'})
    return reasons[:200]


def notify(db):
    """Create notifications for actionable states; deduplicated by (event_key, recipient). Dismissal is a UI preference."""
    n = 0
    for ws in [r['workspace'] for r in db.execute('SELECT workspace FROM campaigns').fetchall()]:
        for r in db.execute("SELECT id, requester_id FROM work_requests WHERE workspace=? AND state='open'", (ws,)).fetchall():
            if db.execute("SELECT 1 FROM work_offers WHERE request_id=? AND state='offered'", (r['id'],)).fetchone():
                n += _notify(db, ws, r['requester_id'], 'offer_awaiting_selection', 'work_request', r['id'], 'offers:' + r['id'])
        for m in db.execute("SELECT m.id, m.key, a.awarded_by, a.provider_id, m.state, m.deadline_at, m.job_id FROM work_milestones m JOIN work_awards a ON a.id=m.award_id WHERE m.workspace=? AND m.state IN ('executing','delivered')", (ws,)).fetchall():
            if m['state'] == 'delivered':
                n += _notify(db, ws, m['awarded_by'], 'review_due', 'work_milestone', m['id'], 'decide:' + m['id'])
            elif m['deadline_at'] < now():
                prov = db.execute('SELECT principal_id FROM providers WHERE id=?', (m['provider_id'],)).fetchone()
                n += _notify(db, ws, prov['principal_id'], 'missing_evidence_past_deadline', 'work_milestone', m['id'], 'deadline:' + m['id'])
        for i in db.execute("SELECT i.id, i.state, a.awarded_by FROM payment_intents i JOIN work_entitlements e ON e.id=i.entitlement_id JOIN work_awards a ON a.id=e.award_id WHERE i.workspace=? AND i.state IN ('submitted','unknown','expired')", (ws,)).fetchall():
            n += _notify(db, ws, i['awarded_by'], 'unresolved_payment_exposure', 'payment_intent', i['id'], 'exposure:' + i['id'] + ':' + i['state'])
        for d in db.execute("SELECT d.id, d.opened_by, d.deadline_at, a.awarded_by, a.provider_id FROM work_disputes d JOIN work_awards a ON a.id=d.award_id WHERE d.workspace=? AND d.state NOT IN ('closed')", (ws,)).fetchall():
            prov = db.execute('SELECT principal_id FROM providers WHERE id=?', (d['provider_id'],)).fetchone()
            other = prov['principal_id'] if d['opened_by'] == d['awarded_by'] else d['awarded_by']
            n += _notify(db, ws, other, 'dispute_response_due', 'work_dispute', d['id'], 'dispute:' + d['id'])
        for i in db.execute("SELECT i.id, a.awarded_by FROM payment_intents i JOIN work_entitlements e ON e.id=i.entitlement_id JOIN work_awards a ON a.id=e.award_id WHERE i.workspace=? AND i.state='settled'", (ws,)).fetchall():
            n += _notify(db, ws, i['awarded_by'], 'payment_confirmed', 'payment_intent', i['id'], 'settled:' + i['id'])
    return n


def _notify(db, ws, recipient, kind, ref_type, ref_id, event_key):
    if db.execute('SELECT 1 FROM notifications WHERE recipient_id=? AND event_key=?', (recipient, event_key)).fetchone():
        return 0
    db.execute('INSERT INTO notifications VALUES (?,?,?,?,?,?,?,?,?,NULL)', ('nt_' + secrets.token_hex(6), ws, recipient, kind, ref_type, ref_id, event_key, 'unread', now()))
    return 1


def notifications(db, principal, include_dismissed=False):
    principal.require('work:read')
    rows = db.execute('SELECT * FROM notifications WHERE workspace=? AND recipient_id=?' + ('' if include_dismissed else " AND state='unread'") + ' ORDER BY rowid DESC LIMIT 200', (principal.workspace, principal.id)).fetchall()
    return [{'id': r['id'], 'kind': r['kind'], 'ref_type': r['ref_type'], 'ref_id': r['ref_id'], 'state': r['state'], 'created_at': r['created_at'], 'dismissed_at': r['dismissed_at']} for r in rows]


def dismiss(db, principal, nid):
    r = db.execute('SELECT * FROM notifications WHERE id=? AND recipient_id=?', (nid, principal.id)).fetchone()
    if r is None:
        raise ServiceError('NOT_FOUND', 'notification')
    db.execute("UPDATE notifications SET state='dismissed', dismissed_at=COALESCE(dismissed_at, ?) WHERE id=?", (now(), nid))
    return {'id': nid, 'state': 'dismissed', 'note': 'a preference: the underlying audit record is untouched'}


def measurements(db, principal):
    """Interpretable economic facts with sample sizes (§67); synthetic vs actual usage is stated by the instance mode."""
    principal.require('work:read')
    ws = principal.workspace
    reqs = db.execute("SELECT id FROM work_requests WHERE workspace=?", (ws,)).fetchall()
    per_req = [db.execute("SELECT COUNT(*) FROM work_offers WHERE request_id=? AND state IN ('offered','awarded','superseded')", (r['id'],)).fetchone()[0] for r in reqs]
    excl = db.execute("SELECT COUNT(*) FROM work_offers WHERE workspace=? AND state='excluded'", (ws,)).fetchone()[0]
    sel = {}
    for a in db.execute('SELECT selection_json FROM work_awards WHERE workspace=?', (ws,)).fetchall():
        s = json.loads(a['selection_json']); k = ('manual' if s.get('manual') else s['policy']['policy']); sel[k] = sel.get(k, 0) + 1
    deliveries = db.execute("SELECT a.awarded_at, m.delivered_at, d.created_at AS decided_at FROM work_milestones m JOIN work_awards a ON a.id=m.award_id LEFT JOIN work_decisions d ON d.id=m.decision_id WHERE m.workspace=? AND m.delivered_at IS NOT NULL", (ws,)).fetchall()
    tta = [d['decided_at'] - d['awarded_at'] for d in deliveries if d['decided_at']]
    dec = db.execute("SELECT decision, evaluation_json FROM work_decisions WHERE workspace=? AND superseded_by IS NULL", (ws,)).fetchall()
    accepted = [json.loads(d['evaluation_json'])['science'] for d in dec if d['decision'] == 'accepted']
    neg = sum(1 for s in accepted if s == 'INFEASIBLE')
    vjobs = db.execute("SELECT COUNT(*) FROM verification_jobs v JOIN work_milestones m ON m.job_id=v.target_job_id WHERE m.workspace=?", (ws,)).fetchone()[0]
    return {'sample_sizes': {'requests': len(reqs), 'awards': sum(sel.values()), 'decisions_current': len(dec), 'deliveries': len(deliveries)},
            'eligible_offers_per_request': {'mean': (sum(per_req) / len(per_req)) if per_req else None, 'min': min(per_req) if per_req else None, 'max': max(per_req) if per_req else None},
            'offers_excluded_with_reasons': excl, 'selection_reasons': sel, 'time_to_accepted_delivery_seconds': {'n': len(tta), 'median': sorted(tta)[len(tta) // 2] if tta else None},
            'verification_records_over_deliveries': {'verifications': vjobs, 'deliveries': len(deliveries)}, 'disputed_obligations': db.execute("SELECT COUNT(*) FROM work_disputes WHERE workspace=?", (ws,)).fetchone()[0],
            'duplicate_claims_refused': 'enforced structurally (one entitlement per milestone and kind); refusals are recorded as 409 responses, not counted here',
            'unreconciled_exposure_intents': db.execute("SELECT COUNT(*) FROM payment_intents WHERE workspace=? AND state IN ('submitted','unknown','expired')", (ws,)).fetchone()[0],
            'accepted_findings_negative_fraction': {'negative': neg, 'accepted': len(accepted), 'fraction': (neg / len(accepted)) if accepted else None, 'note': 'a correct negative prevents wasted downstream work; this is not a rate to minimise'},
            'records_are': 'synthetic fixtures on a same-operator local market: software mechanics, not competitive price discovery',
            'uncertainty': {'operator_relationships': {k: v for k, v in ((r[0], r[1]) for r in db.execute("SELECT json_extract(relationship_json,'$.relationship'), COUNT(*) FROM providers WHERE workspace=? GROUP BY 1", (ws,)).fetchall())},
                            'independent_verifiers_available': False, 'external_rails_tested': False, 'resource_metrics': 'estimates and device-wide samples; energy counters unavailable'}}
