"""§53(1): numerical quality-versus-cost planning over already validated campaign candidates.

Given a completed campaign of heat-diffusion resolutions or Monte Carlo sample counts and a declared cost cap in work
units, return the candidates that fit, ranked by their quality indicator, with the reasoning. The indicator is
labelled as either a proven quantity (a Wilson interval width at the declared confidence, under the declared model)
or a prediction (second-order discretization trend relative to the finest validated candidate). No global optimum
is claimed and nothing is scheduled: the caller chooses."""
import json
from ..errors import ServiceError


def plan(db, principal, campaign_id, cost_cap_units):
    principal.require('job:read')
    c = db.execute('SELECT * FROM sci_campaigns WHERE id=? AND workspace=?', (campaign_id, principal.workspace)).fetchone()
    if c is None:
        raise ServiceError('NOT_FOUND', 'campaign')
    if c['kind'] not in ('heat_diffusion', 'monte_carlo_reliability'):
        raise ServiceError('VALIDATION', {'code': 'plan_kind', 'allowed': ['heat_diffusion', 'monte_carlo_reliability']})
    if type(cost_cap_units) is not int or type(cost_cap_units) is bool or cost_cap_units < 0:
        raise ServiceError('VALIDATION', 'cost_cap_units must be a non-negative integer')
    rows = db.execute("SELECT idx, params_json, state, job_id FROM sci_campaign_candidates WHERE campaign_id=? ORDER BY idx", (campaign_id,)).fetchall()
    candidates, excluded = [], []
    for r in rows:
        params = json.loads(r['params_json'])
        if r['state'] != 'succeeded' or not r['job_id']:
            excluded.append({'index': r['idx'], 'params': params, 'reason': 'not a validated result (state %s)' % r['state']}); continue
        run = db.execute('SELECT work_committed, verification_json, selected_backend FROM compute_runs WHERE job_id=?', (r['job_id'],)).fetchone()
        job = db.execute('SELECT summary_json FROM jobs WHERE id=?', (r['job_id'],)).fetchone()
        ver = json.loads(run['verification_json']) if run and run['verification_json'] else {}
        if not run or not ver.get('passed'):
            excluded.append({'index': r['idx'], 'params': params, 'reason': 'verification not passed'}); continue
        summary = json.loads(job['summary_json'] or '{}')
        candidates.append({'index': r['idx'], 'job_id': r['job_id'], 'params': params, 'cost_work_units': run['work_committed'], 'backend': run['selected_backend'], 'summary': summary})
    if not candidates:
        return {'campaign_id': campaign_id, 'kind': c['kind'], 'cost_cap_units': cost_cap_units, 'candidates': [], 'excluded': excluded, 'recommended': None, 'reasoning': 'no validated candidates'}
    if c['kind'] == 'heat_diffusion':
        proxy = lambda s: 1.0 / s['nx'] ** 2 + 1.0 / s['ny'] ** 2          # O(dx^2 + dy^2) truncation-error proxy on the unit-scaled grid
        finest = min(candidates, key=lambda x: proxy(x['summary']))
        for x in candidates:
            x['quality'] = {'indicator': 'predicted_relative_discretization_error', 'value': proxy(x['summary']) / proxy(finest['summary']),
                            'basis': 'second-order FTCS truncation trend (dx^2 + dy^2) relative to the finest validated grid (predicted, not a proven bound)',
                            'resolution': [x['summary']['nx'], x['summary']['ny']], 'steps': x['summary']['steps'], 'verified': True}
        key = lambda x: (x['quality']['value'], x['cost_work_units'])
        proven = 'none: discretization error is predicted from the refinement trend; verification proves implementation invariants, not accuracy'
    else:
        for x in candidates:
            iv = x['summary'].get('interval') or {}
            width = (float(iv['high']) - float(iv['low'])) if iv else None
            x['quality'] = {'indicator': 'wilson_interval_width', 'value': width, 'basis': 'interval width at %s%% confidence under the declared model (proven for the declared sampling policy; says nothing about model error)' % iv.get('confidence_percent'),
                            'samples': x['summary'].get('samples'), 'verified': True}
        key = lambda x: (x['quality']['value'] if x['quality']['value'] is not None else float('inf'), x['cost_work_units'])
        proven = 'the interval width is a statement about sampling error under the declared distributions, not about the model'
    within = sorted([x for x in candidates if x['cost_work_units'] <= cost_cap_units], key=key)
    over = [x for x in candidates if x['cost_work_units'] > cost_cap_units]
    recommended = within[0] if within else None
    return {'campaign_id': campaign_id, 'kind': c['kind'], 'cost_cap_units': cost_cap_units, 'unit': 'billable work units of this service',
            'candidates_within_cap': [{k: x[k] for k in ('index', 'job_id', 'params', 'cost_work_units', 'backend', 'quality')} for x in within],
            'candidates_over_cap': [{k: x[k] for k in ('index', 'params', 'cost_work_units')} for x in over], 'excluded': excluded,
            'recommended': {k: recommended[k] for k in ('index', 'job_id', 'params', 'cost_work_units', 'quality')} if recommended else None,
            'reasoning': 'candidates are validated results of this campaign; the recommended one has the best quality indicator among those whose committed cost fits the cap; ties break by lower cost',
            'what_is_proven': proven, 'not_a_global_optimum': True}
