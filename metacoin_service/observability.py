"""Operational status and safe metrics with bounded labels (state, kind, category only; never ids or names)."""
import json
from . import scheduling
from .db import now


def status(db, workspace=None):
    where, args = ('WHERE workspace=?', (workspace,)) if workspace else ('', ())
    q = lambda sql, extra=(): db.execute(sql, extra if extra else args).fetchall()
    out = {'ts': now(), 'scope': workspace or 'all-workspaces'}
    out['queue_backlog'] = {r['kind']: r['n'] for r in q("SELECT kind, COUNT(*) AS n FROM jobs " + where + (" AND" if where else " WHERE") + " state='queued' GROUP BY kind")}
    out['jobs_by_state'] = {r['state']: r['n'] for r in q("SELECT state, COUNT(*) AS n FROM jobs " + where + " GROUP BY state")}
    workers = [dict(r) for r in db.execute('SELECT * FROM workers')]
    out['workers'] = {'registered': len(workers), 'live_active': sum(1 for w in workers if scheduling.live(w) and w['state'] == 'active'),
                      'draining': sum(1 for w in workers if w['state'] == 'draining'), 'stale_or_offline': sum(1 for w in workers if not scheduling.live(w))}
    out['waiting_review_gates'] = q("SELECT COUNT(*) AS n FROM workflow_nodes WHERE state='waiting_review'" + (" AND run_id IN (SELECT id FROM workflow_runs WHERE workspace=?)" if workspace else ""))[0]['n']
    out['review_requested_jobs'] = q("SELECT COUNT(*) AS n FROM jobs " + where + (" AND" if where else " WHERE") + " review_state='requested'")[0]['n']
    out['budget_waits'] = q("SELECT COUNT(*) AS n FROM workflow_nodes WHERE state='waiting_dependency' AND blocked_reason LIKE 'budget %'" + (" AND run_id IN (SELECT id FROM workflow_runs WHERE workspace=?)" if workspace else ""))[0]['n']
    out['budget_blocked'] = q("SELECT COUNT(*) AS n FROM workflow_nodes WHERE state='blocked' AND blocked_reason LIKE '%never fit%'" + (" AND run_id IN (SELECT id FROM workflow_runs WHERE workspace=?)" if workspace else ""))[0]['n']
    out['runs_by_state'] = {r['state']: r['n'] for r in q("SELECT state, COUNT(*) AS n FROM workflow_runs " + where + " GROUP BY state")}
    out['campaigns_by_state'] = {r['state']: r['n'] for r in q("SELECT state, COUNT(*) AS n FROM sci_campaigns " + where + " GROUP BY state")}
    out['unresolved_payment_actions'] = q("SELECT COUNT(*) AS n FROM payment_actions " + where)[0]['n']          # local records; provider state is per-journal
    out['sales_by_state'] = {r['state']: r['n'] for r in q("SELECT state, COUNT(*) AS n FROM sales " + where + " GROUP BY state")}
    out['invoke_sales_by_state'] = {r['state']: r['n'] for r in q("SELECT state, COUNT(*) AS n FROM invoke_sales " + where + " GROUP BY state")}
    out['agent_grants_by_state'] = {r['state']: r['n'] for r in q("SELECT state, COUNT(*) AS n FROM policy_grants " + where + " GROUP BY state")}
    out['events_latest_seq'] = q("SELECT COALESCE(MAX(seq),0) AS n FROM events " + where)[0]['n']
    out['usage_records'] = q("SELECT COUNT(*) AS n FROM usage_records " + where)[0]['n']
    # scientific workspace stages (Order 07 §56): document queue, batching occupancy, model residency, optimizer limits, verification backlog, reports, publication, package gates
    def safe(sql, extra=()):
        try:
            return q(sql, extra)
        except Exception:
            return []
    out['documents'] = {'by_stage': {(r['stage'] or r['state']): r['n'] for r in safe("SELECT state, stage, COUNT(*) AS n FROM document_imports " + where + " GROUP BY state, stage")},
                        'parser_failures': (safe("SELECT COUNT(*) AS n FROM document_imports " + where + (" AND" if where else " WHERE") + " state='failed'") or [{'n': 0}])[0]['n'],
                        'awaiting_review': (safe("SELECT COUNT(*) AS n FROM document_imports " + where + (" AND" if where else " WHERE") + " state='awaiting_review'") or [{'n': 0}])[0]['n']}
    batches = safe("SELECT members, cancelled_members, finished_at FROM model_batches ORDER BY started_at DESC LIMIT 20")
    out['generation_batching'] = {'recent_batches': len(batches), 'recent_members': sum(b['members'] for b in batches), 'recent_cancelled_members': sum(b['cancelled_members'] or 0 for b in batches), 'open_batches': sum(1 for b in batches if b['finished_at'] is None)}
    out['model_residency'] = {r['state']: r['n'] for r in safe("SELECT state, COUNT(*) AS n FROM model_runtimes GROUP BY state")}
    out['optimizer'] = {'queued_resource_plans': (safe("SELECT COUNT(*) AS n FROM jobs " + where + (" AND" if where else " WHERE") + " kind='resource_plan' AND state='queued'") or [{'n': 0}])[0]['n'],
                        'limits': 'per-solve time limit from the input (bounded by the manifest); no candidate on limit is reported as limit_no_candidate, never as infeasible'}
    out['verification_backlog'] = {r['state']: r['n'] for r in safe("SELECT state, COUNT(*) AS n FROM verification_jobs " + where + " GROUP BY state")}
    out['reports'] = {'built': (safe("SELECT COUNT(*) AS n FROM analysis_reports " + where) or [{'n': 0}])[0]['n'], 'projections': (safe("SELECT COUNT(*) AS n FROM analysis_projections " + where) or [{'n': 0}])[0]['n']}
    out['package_runs_by_state'] = {r['state']: r['n'] for r in safe("SELECT state, COUNT(*) AS n FROM package_runs " + where + " GROUP BY state")}
    stalled = safe("SELECT COUNT(*) AS n FROM jobs " + where + (" AND" if where else " WHERE") + " state='running' AND lease_expires_at < ?", args + (now(),))
    out['stalled_publication'] = (stalled or [{'n': 0}])[0]['n']
    out['waiting_reasons'] = {'model_loading': sum(v for k, v in out['model_residency'].items() if k in ('loading',)), 'insufficient_batch_capacity': out['generation_batching']['open_batches'],
                              'missing_review': out['waiting_review_gates'] + out['review_requested_jobs'], 'budget': out['budget_waits'] + out['budget_blocked'],
                              'awaiting_verification_delivery': out['package_runs_by_state'].get('awaiting_verification', 0), 'documents_awaiting_review': out['documents']['awaiting_review'],
                              'meaning': 'each count names why work waits; review, budget and document review need an action; loading, batching and verification resolve on their own'}
    out['note'] = 'counts of local records; not utilization, revenue, customers or environmental impact'
    return out


def metrics_text(db):
    """Prometheus exposition format; every label value comes from a closed enumeration."""
    lines = []
    def gauge(name, help_text, rows):
        lines.append('# HELP %s %s' % (name, help_text)); lines.append('# TYPE %s gauge' % name)
        for labels, value in rows:
            lbl = ','.join('%s="%s"' % (k, v) for k, v in labels.items())
            lines.append('%s{%s} %d' % (name, lbl, value) if lbl else '%s %d' % (name, value))
    s = status(db)
    try:
        from .economy import ops as economy_ops
        ws = db.execute('SELECT workspace FROM campaigns LIMIT 1').fetchone()
        w = economy_ops.counts(db, ws['workspace']) if ws else {}
    except Exception:
        w = {}
    for name, key in (('metacoin_work_requests', 'requests_by_state'), ('metacoin_work_awards', 'awards_by_state'), ('metacoin_work_milestones', 'milestones_by_state'), ('metacoin_work_entitlements', 'entitlements_by_state'), ('metacoin_work_intents', 'intents_by_state'), ('metacoin_work_disputes', 'disputes_by_state'), ('metacoin_treasury_allocations', 'treasury_allocations_by_state')):
        gauge(name, key.replace('_', ' '), [({'state': k}, v) for k, v in sorted((w.get(key) or {}).items())])
    gauge('metacoin_work_pending_settlements', 'payment intents awaiting settlement or reconciliation', [({}, w.get('pending_settlements', 0))])
    gauge('metacoin_work_evidence_awaiting_verification', 'delivered milestones without a passed verification', [({}, w.get('evidence_awaiting_verification', 0))])
    gauge('metacoin_jobs', 'jobs by state', [({'state': k}, v) for k, v in sorted(s['jobs_by_state'].items())])
    gauge('metacoin_queue_backlog', 'queued jobs by kind', [({'kind': k}, v) for k, v in sorted(s['queue_backlog'].items())])
    gauge('metacoin_workers', 'workers by liveness', [({'class': k}, v) for k, v in sorted(s['workers'].items())])
    gauge('metacoin_waiting_review_gates', 'workflow nodes waiting for a review decision', [({}, s['waiting_review_gates'])])
    gauge('metacoin_budget_waits', 'workflow nodes waiting for budget release', [({}, s['budget_waits'])])
    gauge('metacoin_budget_blocked', 'workflow nodes permanently blocked by budget', [({}, s['budget_blocked'])])
    gauge('metacoin_runs', 'workflow runs by state', [({'state': k}, v) for k, v in sorted(s['runs_by_state'].items())])
    gauge('metacoin_campaigns', 'campaigns by state', [({'state': k}, v) for k, v in sorted(s['campaigns_by_state'].items())])
    gauge('metacoin_payment_actions_unresolved', 'local payment action records', [({}, s['unresolved_payment_actions'])])
    gauge('metacoin_sales', 'sales by state', [({'state': k, 'route': 'bundle'}, v) for k, v in sorted(s['sales_by_state'].items())] +
          [({'state': k, 'route': 'invoke'}, v) for k, v in sorted(s['invoke_sales_by_state'].items())])
    gauge('metacoin_agent_grants', 'agent grants by state', [({'state': k}, v) for k, v in sorted(s['agent_grants_by_state'].items())])
    gauge('metacoin_events_latest_seq', 'latest event sequence', [({}, s['events_latest_seq'])])
    gauge('metacoin_documents', 'document imports by stage', [({'stage': str(k)}, v) for k, v in sorted(s['documents']['by_stage'].items())])
    gauge('metacoin_verification_backlog', 'verification records by state', [({'state': k}, v) for k, v in sorted(s['verification_backlog'].items())])
    gauge('metacoin_package_runs', 'package runs by delivery state', [({'state': k}, v) for k, v in sorted(s['package_runs_by_state'].items())])
    gauge('metacoin_model_residency', 'model runtimes by state', [({'state': k}, v) for k, v in sorted(s['model_residency'].items())])
    gauge('metacoin_stalled_publication', 'running jobs whose lease expired', [({}, s['stalled_publication'])])
    return '\n'.join(lines) + '\n'
