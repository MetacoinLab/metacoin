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
    return '\n'.join(lines) + '\n'
