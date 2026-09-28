"""Scientific campaigns: bounded parameter grids, adaptive bisection, Pareto sets, refinements.

A campaign evaluates a bounded set of parameter choices against one immutable model kind and one
immutable base input (a dataset version + parameters, or inline inputs). Candidates are enumerated
deterministically (Cartesian product in axis order, values ascending as declared) and persisted
with their index BEFORE any job exists, so a restart resumes exactly the missing evaluations.

Parameter axes (exact integer transforms only):
  temporal_energy : capacity, reserve, initial_low, initial_high, load_scale_percent, harvest_scale_percent
  energy_audit    : available_low, available_high, reserve, load_scale_percent
  safe_runtime    : available_low, reserve, variable_power_high, duration_cap
Scaling by s percent widens conservatively: low bounds use floor(x*s/100), high bounds use ceil(x*s/100).

Adaptive bisection (min/max boundary of robust FEASIBLE) is allowed only along axes whose effect on
the FEASIBLE verdict is monotone for the implemented model with fixed absolute initial energy:
  capacity (more never hurts: saturation only rises), reserve (more never helps), load_scale_percent
  (more never helps), harvest_scale_percent (more never hurts), available_low/high (more never hurts).
duration_cap and initial_* are refused for bisection (feasibility/validity is not monotone in them).
INDETERMINATE counts as "not robustly feasible" for the search objective. Budget exhaustion yields an
unresolved bracket, never a fabricated optimum.
"""
import hashlib
import itertools
import json
import math
import secrets
from experiments.private_receipts import receipt as merkle
from . import history
from .auth import Principal
from .datasets import add_edge
from .db import now
from .errors import ServiceError

KINDS = ('temporal_energy', 'energy_audit', 'safe_runtime', 'heat_diffusion', 'monte_carlo_reliability', 'resource_plan')
AXES = {'temporal_energy': ('capacity', 'reserve', 'initial_low', 'initial_high', 'load_scale_percent', 'harvest_scale_percent'),
        'energy_audit': ('available_low', 'available_high', 'reserve', 'load_scale_percent'),
        'safe_runtime': ('available_low', 'reserve', 'variable_power_high', 'duration_cap'),
        'heat_diffusion': ('nx', 'ny', 'steps', 'snapshots'),                  # grid/horizon refinement sweeps (dt, dx are decimal strings: not sweepable here)
        'monte_carlo_reliability': ('samples', 'seed'),
        'resource_plan': ('reserve', 'initial_low', 'capacity', 'supply_scale_percent', 'base_scale_percent', 'time_limit_s')}
MODEL_VERSIONS = {'temporal_energy': 'temporal-energy/v1', 'energy_audit': 'outage-energy-bounds/v0', 'safe_runtime': 'safe-runtime/v1',
                  'heat_diffusion': 'heat-diffusion-2d-ftcs/v1', 'monte_carlo_reliability': 'monte-carlo-reliability/v1', 'resource_plan': 'robust-resource-plan/v1'}
PLAN_FEASIBLE = ('optimal_within_tolerance', 'feasible_incumbent_no_optimality_claim')
CHANGE_SOURCES = ('user_edit', 'reviewed_table_correction', 'model_suggestion', 'measured_update')
MONOTONE = {  # axis -> 'up_better' (larger values never make FEASIBLE harder) | 'up_worse'
    'capacity': 'up_better', 'reserve': 'up_worse', 'load_scale_percent': 'up_worse', 'harvest_scale_percent': 'up_better',
    'available_low': 'up_better', 'available_high': 'up_better', 'initial_low': 'up_better', 'supply_scale_percent': 'up_better', 'base_scale_percent': 'up_worse'}
LIMITS = {'max_axes': 3, 'max_values_per_axis': 64, 'max_evaluations': 256, 'max_inflight': 8, 'max_total_segments': 65_536, 'max_adaptive_evaluations': 24, 'max_acquisition_candidates': 64, 'max_changes': 32}
STATES = ('created', 'running', 'paused', 'cancelled', 'completed', 'budget_exhausted')
CAND_STATES = ('unevaluated', 'invalid', 'queued', 'running', 'succeeded', 'failed', 'cancelled')
RESULT_FIELDS = {'heat_diffusion': ('final_min', 'final_max', 'steps', 'r_x', 'backend', 'work_units_committed'),
                 'monte_carlo_reliability': ('probability_estimate', 'events', 'samples', 'backend', 'work_units_committed'),
                 'temporal_energy': ('outcome', 'min_reserve_margin_pessimistic', 'min_reserve_margin_optimistic', 'spill_bounds', 'horizon_seconds'),
                 'energy_audit': ('outcome', 'worst_margin', 'best_margin', 'additional_usable_energy', 'required_high'),
                 'safe_runtime': ('status', 'safe_duration', 'margin_at_duration', 'residual_energy_worst_case'),
                 'resource_plan': ('status', 'objective', 'cost', 'selected', 'min_margin', 'spill', 'optimality', 'reason', 'first_violation')}


def robust_ok(outcome):
    return outcome in ('FEASIBLE', 'ROBUSTLY_FEASIBLE') or outcome in PLAN_FEASIBLE


def candidate_outcome(kind, job_outcome, summary_json):
    """The scientific outcome recorded per candidate: the job outcome, except resource plans whose verified job carries the plan status."""
    if kind == 'resource_plan' and summary_json:
        return json.loads(summary_json).get('status')
    return job_outcome


def apply_change(base, path, value):
    """Typed change on a base input: `field`, `list.index` or `tasks.<id>.<field>` (lists of {id: ...} records are addressed
    by id). Returns (previous, new_base). Structure is never created; the leaf type must be preserved."""
    out = json.loads(merkle.canonical(base))
    parts = path.split('.')
    if not parts or any(not p for p in parts):
        raise ServiceError('VALIDATION', {'code': 'unknown_base_field', 'path': path})
    def step(node, part):
        if isinstance(node, list):
            if part.isdigit() and int(part) < len(node):
                return int(part)
            if node and all(isinstance(t, dict) and 'id' in t for t in node):
                ids = [t['id'] for t in node]
                if part in ids:
                    return ids.index(part)
        elif isinstance(node, dict) and part in node:
            return part
        raise ServiceError('VALIDATION', {'code': 'unknown_base_field', 'path': path})
    node = out
    for part in parts[:-1]:
        node = node[step(node, part)]
    key = step(node, parts[-1])
    prev = node[key]
    if isinstance(prev, (dict, list)) or type(prev) is not type(value):
        raise ServiceError('VALIDATION', {'code': 'change_type', 'path': path, 'expected': type(prev).__name__})
    node[key] = value
    return prev, out


def deep_diff(a, b, prefix='', out=None, limit=200):
    """Changed leaf paths between two inputs (dotted; task lists keyed by id)."""
    out = {} if out is None else out
    if len(out) >= limit:
        return out
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            deep_diff(a.get(k), b.get(k), prefix + ('.' if prefix else '') + k, out, limit)
    elif isinstance(a, list) and isinstance(b, list):
        if a and b and all(isinstance(t, dict) and 'id' in t for t in a + b):
            da, dbb = {t['id']: t for t in a}, {t['id']: t for t in b}
            for k in sorted(set(da) | set(dbb)):
                deep_diff(da.get(k), dbb.get(k), prefix + '.' + str(k), out, limit)
        else:
            for i in range(max(len(a), len(b))):
                deep_diff(a[i] if i < len(a) else None, b[i] if i < len(b) else None, prefix + '.' + str(i), out, limit)
    elif a != b:
        out[prefix] = (a, b)
    return out


def _scale(value, percent, up):
    return math.ceil(value * percent / 100) if up else value * percent // 100


def apply_params(kind, base, params):
    """Deterministic, exact application of axis values to a base input. Returns a new input dict."""
    out = json.loads(merkle.canonical(base))
    for path, value in params.items():
        if path not in AXES[kind]:
            raise ServiceError('VALIDATION', {'code': 'axis', 'path': path})
        if path == 'load_scale_percent':
            key_lo, key_hi = ('load_low', 'load_high') if kind == 'temporal_energy' else ('power_low', 'power_high')
            for seg in out['segments']:
                seg[key_lo], seg[key_hi] = _scale(seg[key_lo], value, False), _scale(seg[key_hi], value, True)
        elif path == 'harvest_scale_percent':
            for seg in out['segments']:
                seg['harvest_low'], seg['harvest_high'] = _scale(seg['harvest_low'], value, False), _scale(seg['harvest_high'], value, True)
        elif path == 'supply_scale_percent':
            out['supply_low'] = [_scale(x, value, False) for x in out['supply_low']]; out['supply_high'] = [_scale(x, value, True) for x in out['supply_high']]
        elif path == 'base_scale_percent':
            out['base_low'] = [_scale(x, value, False) for x in out['base_low']]; out['base_high'] = [_scale(x, value, True) for x in out['base_high']]
        else:
            out[path] = value
    return out


def expand_axes(kind, axes):
    if type(axes) is not list or not 1 <= len(axes) <= LIMITS['max_axes']:
        raise ServiceError('VALIDATION', {'code': 'axes_count', 'max': LIMITS['max_axes']})
    expanded, seen = [], set()
    for ax in axes:
        if type(ax) is not dict or ax.get('path') not in AXES[kind] or ax['path'] in seen:
            raise ServiceError('VALIDATION', {'code': 'axis', 'allowed': list(AXES[kind])})
        seen.add(ax['path'])
        if 'values' in ax and set(ax) == {'path', 'values'}:
            values = ax['values']
            if type(values) is not list or not values or not all(type(v) is int and type(v) is not bool for v in values) or len(set(values)) != len(values):
                raise ServiceError('VALIDATION', {'code': 'axis_values', 'path': ax['path']})
            values = sorted(values)
        elif set(ax) == {'path', 'start', 'stop', 'step'} and all(type(ax[k]) is int for k in ('start', 'stop', 'step')) and ax['step'] > 0 and ax['start'] <= ax['stop']:
            values = list(range(ax['start'], ax['stop'] + 1, ax['step']))
        else:
            raise ServiceError('VALIDATION', {'code': 'axis_shape', 'path': ax.get('path')})
        if len(values) > LIMITS['max_values_per_axis'] or any(v < 0 or v > 2 ** 53 - 1 for v in values):
            raise ServiceError('VALIDATION', {'code': 'axis_values', 'path': ax['path'], 'max': LIMITS['max_values_per_axis']})
        expanded.append((ax['path'], values))
    return expanded


def enumerate_candidates(expanded):
    paths = [p for p, _ in expanded]
    for combo in itertools.product(*[v for _, v in expanded]):
        yield dict(zip(paths, combo))


class Campaigns:
    def __init__(self, contracts, jobs, datasets, store, settings):
        self.contracts, self.jobs, self.datasets, self.store, self.settings = contracts, jobs, datasets, store, settings

    # ---- definition and admission ----------------------------------------------------
    def _base_input(self, db, principal, kind, base):
        """Materialize the immutable base input (dataset version + parameters, or inline)."""
        if type(base) is dict and 'dataset_version_id' in base:
            rows, v = self.datasets.rows(db, principal, base['dataset_version_id'])
            if kind == 'temporal_energy' and v['kind'] == 'temporal_series':
                return self.datasets.temporal_input(rows, base.get('parameters', {}), v), v['id']
            if kind == 'energy_audit' and v['kind'] == 'energy_intervals':
                return self.datasets.interval_input(rows, base.get('parameters', {}), v), v['id']
            raise ServiceError('VALIDATION', 'dataset kind incompatible with campaign kind')
        from .contracts import validate_inputs
        validate_inputs(kind, base)
        return base, None

    def preview(self, db, principal, definition):
        principal.require('contract:create')
        merkle.canonical(definition)
        allowed = {'name', 'kind', 'base', 'axes', 'max_evaluations', 'policy', 'adaptive', 'tags', 'changes'}
        if type(definition) is not dict or not {'name', 'kind', 'base'} <= set(definition) or not set(definition) <= allowed:
            raise ServiceError('VALIDATION', {'code': 'definition_fields', 'allowed': sorted(allowed)})
        kind = definition['kind']
        if kind not in KINDS or type(definition['name']) is not str or not 1 <= len(definition['name']) <= 128:
            raise ServiceError('VALIDATION', {'code': 'kind_or_name', 'kinds': list(KINDS)})
        base, dataset_version_id = self._base_input(db, principal, kind, definition['base'])
        adaptive = definition.get('adaptive')
        if adaptive is not None:
            self._validate_adaptive(kind, adaptive)
            total = min(adaptive['max_evaluations'], LIMITS['max_adaptive_evaluations'])
            expanded = []
        else:
            expanded = expand_axes(kind, definition.get('axes'))
            total = 1
            for _, values in expanded:
                total *= len(values)
        max_eval = definition.get('max_evaluations', LIMITS['max_evaluations'])
        if type(max_eval) is not int or not 1 <= max_eval <= LIMITS['max_evaluations']:
            raise ServiceError('VALIDATION', {'code': 'max_evaluations', 'max': LIMITS['max_evaluations']})
        segments = len(base.get('segments') or base.get('fixed_segments') or [])
        if total > max_eval:
            raise ServiceError('VALIDATION', {'code': 'too_many_evaluations', 'requested': total, 'max': max_eval,
                                              'suggestion': 'reduce axis values so the product is at most ' + str(max_eval)})
        if total * max(segments, 1) > LIMITS['max_total_segments']:
            raise ServiceError('VALIDATION', {'code': 'too_many_segments', 'total_segments': total * segments, 'max': LIMITS['max_total_segments']})
        amount = (definition.get('policy') or {}).get('amount', 1) if kind == 'energy_audit' else 0
        attempts = 1 + self.settings.limits['job_max_retries']
        estimate = {'basis': 'exact counts from the accepted definition and hard application limits; no measured runtimes',
                    'evaluations': total, 'segments_per_evaluation': segments, 'total_segments': total * segments,
                    'max_job_attempts': total * attempts, 'machine_time_upper_bound_seconds': total * attempts * self.settings.limits['job_timeout_seconds'],
                    'monetary_exposure_max': {'amount': amount * total, 'unit': 'campaign asset base units',
                                              'basis': 'energy-audit candidates carry one action entitlement each; campaigns never dispatch actions'},
                    'modeled_scientific_energy': 'per-candidate mJ results; not money, not measured electricity'}
        digest = hashlib.sha256(b'metacoin/campaign/v1\0' + merkle.canonical({'definition': definition, 'base_input': base})).hexdigest()
        return {'kind': kind, 'base': base, 'dataset_version_id': dataset_version_id, 'expanded': expanded, 'total': total,
                'estimate': estimate, 'digest': digest, 'definition': definition}

    @staticmethod
    def _validate_adaptive(kind, adaptive):
        if type(adaptive) is dict and adaptive.get('strategy') == 'acquisition':
            return Campaigns._validate_acquisition(kind, adaptive)
        allowed = {'objective', 'axis', 'lo', 'hi', 'max_evaluations'}
        if type(adaptive) is not dict or set(adaptive) != allowed or adaptive['objective'] not in ('min_feasible', 'max_feasible'):
            raise ServiceError('VALIDATION', {'code': 'adaptive_fields', 'allowed': sorted(allowed), 'objectives': ['min_feasible', 'max_feasible']})
        if adaptive['axis'] not in AXES[kind]:
            raise ServiceError('VALIDATION', {'code': 'axis', 'allowed': list(AXES[kind])})
        if adaptive['axis'] not in MONOTONE:
            raise ServiceError('VALIDATION', {'code': 'not_monotone', 'axis': adaptive['axis'],
                                              'reason': 'feasibility is not monotone in this axis for the implemented model; use a bounded grid'})
        want = 'up_better' if adaptive['objective'] == 'min_feasible' else 'up_worse'
        if MONOTONE[adaptive['axis']] != want:
            raise ServiceError('VALIDATION', {'code': 'objective_direction', 'axis': adaptive['axis'], 'monotone': MONOTONE[adaptive['axis']],
                                              'reason': 'min_feasible needs an axis where larger is never worse; max_feasible needs larger never better'})
        if not all(type(adaptive[k]) is int for k in ('lo', 'hi', 'max_evaluations')) or adaptive['lo'] > adaptive['hi'] or adaptive['lo'] < 0 \
                or not 3 <= adaptive['max_evaluations'] <= LIMITS['max_adaptive_evaluations']:
            raise ServiceError('VALIDATION', {'code': 'adaptive_bounds', 'max_evaluations': LIMITS['max_adaptive_evaluations']})

    @staticmethod
    def _validate_acquisition(kind, adaptive):
        """Budgeted acquisition over a finite authorized candidate set (§39): score = predicted improvement over the best
        observed value (inverse-distance prediction over evaluated candidates, normalized axes) + exploration term
        (exploration_percent/100 x observed value range x distance to the nearest evaluated candidate)."""
        allowed = {'strategy', 'candidates', 'objective', 'max_evaluations', 'exploration_percent', 'seed', 'stop_below'}
        if set(adaptive) - allowed or not {'candidates', 'objective', 'max_evaluations'} <= set(adaptive):
            raise ServiceError('VALIDATION', {'code': 'adaptive_fields', 'allowed': sorted(allowed), 'required': ['strategy', 'candidates', 'objective', 'max_evaluations']})
        cands = adaptive['candidates']
        if type(cands) is not list or not 1 <= len(cands) <= LIMITS['max_acquisition_candidates']:
            raise ServiceError('VALIDATION', {'code': 'candidates', 'max': LIMITS['max_acquisition_candidates']})
        seen = set()
        for cd in cands:
            if type(cd) is not dict or not cd or set(cd) - set(AXES[kind]) or not all(type(v) is int and type(v) is not bool and 0 <= v <= 2 ** 53 - 1 for v in cd.values()):
                raise ServiceError('VALIDATION', {'code': 'candidate_shape', 'allowed_axes': list(AXES[kind])})
            key = json.dumps(cd, sort_keys=True)
            if key in seen:
                raise ServiceError('VALIDATION', {'code': 'duplicate_candidate', 'params': cd})
            seen.add(key)
        obj = adaptive['objective']
        if type(obj) is not dict or set(obj) != {'field', 'direction'} or obj['direction'] not in ('min', 'max') or type(obj['field']) is not str or (obj['field'] != 'feasible' and obj['field'] not in RESULT_FIELDS[kind]):
            raise ServiceError('VALIDATION', {'code': 'objective_shape', 'fields': ['feasible'] + list(RESULT_FIELDS[kind]), 'directions': ['min', 'max']})
        if type(adaptive['max_evaluations']) is not int or not 1 <= adaptive['max_evaluations'] <= LIMITS['max_adaptive_evaluations']:
            raise ServiceError('VALIDATION', {'code': 'max_evaluations', 'max': LIMITS['max_adaptive_evaluations']})
        ex = adaptive.get('exploration_percent', 10)
        if type(ex) is not int or not 0 <= ex <= 100:
            raise ServiceError('VALIDATION', {'code': 'exploration_percent', 'range': [0, 100]})
        if type(adaptive.get('seed', 0)) is not int or type(adaptive.get('stop_below', 0)) is not int or adaptive.get('stop_below', 0) < 0:
            raise ServiceError('VALIDATION', {'code': 'seed_or_stop_below'})
        return adaptive

    def create(self, db, principal, definition):
        pv = self.preview(db, principal, definition)
        from .agents import guard
        adaptive_def = definition.get('adaptive')
        guard(db, principal, 'campaign:run', workflows=1, jobs=pv['total'] if adaptive_def is None else min(adaptive_def['max_evaluations'], LIMITS['max_adaptive_evaluations']))
        cid = 'cmp_' + secrets.token_hex(8)
        base_id = self.store.store(db, workspace=principal.workspace, kind='draft_input', owner_id=principal.id, plaintext=merkle.canonical(pv['base']),
                                   recipients=[], intended_use='campaign-base-input;owner-worker')
        adaptive = definition.get('adaptive')
        db.execute('INSERT INTO sci_campaigns VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)',
                   (cid, principal.workspace, principal.id, definition['name'], pv['kind'], pv['dataset_version_id'], base_id,
                    merkle.canonical(definition).decode(), pv['digest'], 'created', pv['total'],
                    json.dumps(self._initial_adaptive_state(adaptive)) if adaptive else None,
                    json.dumps(pv['estimate']), now(), now()))
        if adaptive is not None and adaptive.get('strategy') == 'acquisition':
            for index, params in enumerate(adaptive['candidates']):
                db.execute('INSERT INTO sci_campaign_candidates (campaign_id, idx, params_json, state, updated_at) VALUES (?,?,?,?,?)',
                           (cid, index, json.dumps(params, sort_keys=True), 'unevaluated', now()))
        if adaptive is None:
            for index, params in enumerate(enumerate_candidates(pv['expanded'])):
                db.execute('INSERT INTO sci_campaign_candidates (campaign_id, idx, params_json, state, updated_at) VALUES (?,?,?,?,?)',
                           (cid, index, json.dumps(params, sort_keys=True), 'unevaluated', now()))
        history.record(db, principal.workspace, principal.id, 'job.queued', 'campaign', cid, {'digest': pv['digest'], 'total': pv['total'], 'kind': pv['kind']})
        if pv['dataset_version_id']:
            add_edge(db, principal.workspace, 'dataset_version', pv['dataset_version_id'], 'campaign', cid, 'used_input')
        return {'campaign_id': cid, 'digest': pv['digest'], 'total_candidates': pv['total'], 'estimate': pv['estimate']}

    @staticmethod
    def _initial_adaptive_state(adaptive):
        budget = min(adaptive['max_evaluations'], LIMITS['max_adaptive_evaluations'])
        if adaptive.get('strategy') == 'acquisition':
            return {'strategy': 'acquisition', 'candidates': adaptive['candidates'], 'evaluated': [], 'stopping_reason': None, 'best': None, 'budget_left': budget, 'seed': adaptive.get('seed', 0),
                    'acquisition': {'objective': adaptive['objective'], 'exploration_percent': adaptive.get('exploration_percent', 10), 'stop_below': adaptive.get('stop_below', 0),
                                    'score': 'predicted improvement over best observed (inverse-distance prediction on normalized axes) + exploration_percent/100 x observed range x nearest-evaluated distance',
                                    'uncertainty_interpretation': 'the prediction is an interpolation of observed values, not a calibrated posterior; the exploration term is a declared bonus, not a probability'},
                    'log': [], 'phase': 'select'}
        return {'lo': adaptive['lo'], 'hi': adaptive['hi'], 'evaluated': [], 'stopping_reason': None, 'boundary': None, 'phase': 'check_first', 'budget_left': budget}

    # ---- state and control -----------------------------------------------------------------
    def _campaign(self, db, campaign_id, workspace=None):
        row = db.execute('SELECT * FROM sci_campaigns WHERE id=?' + (' AND workspace=?' if workspace else ''), (campaign_id,) + ((workspace,) if workspace else ())).fetchone()
        if row is None:
            raise ServiceError('NOT_FOUND', 'campaign')
        return row

    def control(self, db, principal, campaign_id, action):
        principal.require('job:submit' if action in ('run', 'resume') else 'job:cancel')
        c = self._campaign(db, campaign_id, principal.workspace)
        if c['owner_id'] != principal.id:
            raise ServiceError('FORBIDDEN', 'campaign')
        transitions = {'run': ('created', 'running'), 'pause': ('running', 'paused'), 'resume': ('paused', 'running'), 'cancel': (None, 'cancelled')}
        frm, to = transitions[action]
        if c['state'] in ('completed', 'cancelled', 'budget_exhausted'):
            raise ServiceError('CONFLICT', 'campaign is terminal')
        if frm is not None and c['state'] != frm:
            raise ServiceError('CONFLICT', 'campaign is ' + c['state'] + ', cannot ' + action)
        db.execute('UPDATE sci_campaigns SET state=?, updated_at=? WHERE id=?', (to, now(), campaign_id))
        if to == 'cancelled':
            for cand in db.execute("SELECT idx, job_id, state FROM sci_campaign_candidates WHERE campaign_id=? AND state IN ('unevaluated','queued','running')", (campaign_id,)).fetchall():
                if cand['job_id']:
                    try:
                        self.jobs.cancel(db, principal, cand['job_id'])
                    except ServiceError:
                        pass
                db.execute("UPDATE sci_campaign_candidates SET state='cancelled', updated_at=? WHERE campaign_id=? AND idx=?", (now(), campaign_id, cand['idx']))
            db.execute('UPDATE sci_campaigns SET finished_at=? WHERE id=?', (now(), campaign_id))
        history.record(db, principal.workspace, principal.id, 'job.cancelled' if to == 'cancelled' else 'job.retry_scheduled', 'campaign', campaign_id, {'action': action, 'state': to})
        if to == 'running':
            self.tick(db, campaign_id)
        return self.view(db, principal, campaign_id)

    def view(self, db, principal, campaign_id):
        principal.require('job:read')
        c = self._campaign(db, campaign_id, principal.workspace)
        counts = {r['state']: r['n'] for r in db.execute('SELECT state, COUNT(*) AS n FROM sci_campaign_candidates WHERE campaign_id=? GROUP BY state', (campaign_id,))}
        outcomes = {r['outcome']: r['n'] for r in db.execute("SELECT outcome, COUNT(*) AS n FROM sci_campaign_candidates WHERE campaign_id=? AND state='succeeded' GROUP BY outcome", (campaign_id,))}
        done = sum(counts.get(s, 0) for s in ('succeeded', 'failed', 'invalid', 'cancelled'))
        out = {'campaign_id': campaign_id, 'name': c['name'], 'kind': c['kind'], 'state': c['state'], 'digest': c['digest'],
               'dataset_version_id': c['dataset_version_id'], 'total_candidates': c['total_candidates'], 'done': done,
               'progress': {'denominator': c['total_candidates'], 'numerator': done}, 'by_state': counts, 'by_outcome': outcomes,
               'estimate': json.loads(c['estimate_json']), 'created_at': c['created_at'], 'updated_at': c['updated_at'], 'finished_at': c['finished_at'],
               'definition': merkle.parse(c['definition_json']) if principal.can('job:read_private') else None}
        if c['adaptive_json']:
            out['adaptive'] = json.loads(c['adaptive_json'])
        return out

    def results(self, db, principal, campaign_id, limit=500):
        principal.require('job:read')
        c = self._campaign(db, campaign_id, principal.workspace)
        private = principal.can('job:read_private')
        rows = db.execute('SELECT * FROM sci_campaign_candidates WHERE campaign_id=? ORDER BY idx LIMIT ?', (campaign_id, min(int(limit), 1000))).fetchall()
        table = []
        for r in rows:
            item = {'index': r['idx'], 'params': json.loads(r['params_json']), 'state': r['state'], 'job_id': r['job_id']}
            if private and r['summary_json']:
                summary = json.loads(r['summary_json'])
                item['result'] = {k: summary.get(k) for k in RESULT_FIELDS[c['kind']]}
                item['outcome'] = r['outcome']
            elif r['state'] == 'succeeded':
                item['outcome'] = 'withheld' if not private else r['outcome']
            if r['state'] == 'invalid':
                item['reason'] = r['outcome']            # refusal code, never a value
            table.append(item)
        return {'campaign_id': campaign_id, 'kind': c['kind'], 'model_version': MODEL_VERSIONS[c['kind']],
                'rows': table, 'legend': {'unevaluated': 'not yet run', 'invalid': 'outside the model domain', 'failed': 'execution failed', 'cancelled': 'cancelled',
                                          'succeeded': 'result available; see outcome'}, 'note': 'missing values are absent, never zero'}

    def results_csv(self, db, principal, campaign_id):
        """Safe CSV: text cells beginning with = + - @ are prefixed with an apostrophe (export policy); numbers unchanged."""
        data = self.results(db, principal, campaign_id)
        axes = sorted({k for r in data['rows'] for k in r['params']})
        fields = list(RESULT_FIELDS[data['kind']])
        def safe(v):
            s = '' if v is None else str(v)
            return ("'" + s) if s[:1] in ('=', '+', '-', '@') and not s.lstrip('-').isdigit() else s
        lines = [','.join(['index'] + axes + ['state', 'outcome'] + fields + ['model_version'])]
        for r in data['rows']:
            res = r.get('result') or {}
            lines.append(','.join([str(r['index'])] + [str(r['params'].get(a, '')) for a in axes] + [r['state'], safe(r.get('outcome'))]
                                  + [safe(json.dumps(res[f]) if isinstance(res.get(f), list) else res.get(f)) for f in fields] + [data['model_version']]))
        return '\n'.join(lines) + '\n'

    # ---- execution tick ----------------------------------------------------------------------
    def _owner(self, db, c):
        row = db.execute('SELECT * FROM principals WHERE id=?', (c['owner_id'],)).fetchone()
        if row is None or row['revoked_at'] is not None:
            raise ServiceError('FORBIDDEN', 'campaign owner revoked')
        return Principal(row)

    def _dispatch(self, db, c, owner, base, params, index):
        definition = merkle.parse(c['definition_json'])
        try:
            inputs = apply_params(c['kind'], base, params)
            if c['kind'] == 'temporal_energy' and (inputs['initial_high'] > inputs['capacity'] or inputs['reserve'] > inputs['capacity']):
                raise ServiceError('MODEL_DOMAIN', 'initial energy or reserve exceeds capacity')
            from .contracts import validate_inputs
            validate_inputs(c['kind'], inputs)
        except Exception as exc:
            code = getattr(exc, 'code', 'MODEL_DOMAIN')
            db.execute("UPDATE sci_campaign_candidates SET state='invalid', outcome=?, updated_at=? WHERE campaign_id=? AND idx=?", (code, now(), c['id'], index))
            return 'invalid'
        policy = dict(definition.get('policy') or {})
        policy.setdefault('reviewer_id', db.execute("SELECT id FROM principals WHERE workspace=? AND role='reviewer' AND revoked_at IS NULL ORDER BY created_at LIMIT 1", (c['workspace'],)).fetchone()['id'])
        contract_id = self.contracts.create_draft(db, owner, kind=c['kind'], title=c['name'] + '#' + str(index), inputs=inputs, policy=policy)
        self.contracts.freeze(db, owner, contract_id)
        job_id = self.jobs.submit(db, owner, contract_id)
        db.execute("UPDATE sci_campaign_candidates SET state='queued', contract_id=?, job_id=?, updated_at=? WHERE campaign_id=? AND idx=?", (contract_id, job_id, now(), c['id'], index))
        add_edge(db, c['workspace'], 'campaign', c['id'], 'job', job_id, 'produced')
        return 'queued'

    def tick(self, db, campaign_id):
        """Poll in-flight candidates, dispatch new ones up to the concurrency limit, advance adaptive search."""
        c = self._campaign(db, campaign_id)
        if c['state'] not in ('running',):
            return c['state']
        owner = self._owner(db, c)
        base = self.store.load_json(db, c['base_artifact_id'], c['workspace'])
        # 1. poll
        for cand in db.execute("SELECT idx, job_id FROM sci_campaign_candidates WHERE campaign_id=? AND state IN ('queued','running')", (campaign_id,)).fetchall():
            job = db.execute('SELECT state, outcome, summary_json FROM jobs WHERE id=?', (cand['job_id'],)).fetchone()
            if job['state'] == 'succeeded':
                db.execute("UPDATE sci_campaign_candidates SET state='succeeded', outcome=?, summary_json=?, updated_at=? WHERE campaign_id=? AND idx=?",
                           (candidate_outcome(c['kind'], job['outcome'], job['summary_json']), job['summary_json'], now(), campaign_id, cand['idx']))
            elif job['state'] in ('failed', 'cancelled'):
                db.execute("UPDATE sci_campaign_candidates SET state=?, updated_at=? WHERE campaign_id=? AND idx=?", (job['state'], now(), campaign_id, cand['idx']))
            elif job['state'] == 'running':
                db.execute("UPDATE sci_campaign_candidates SET state='running', updated_at=? WHERE campaign_id=? AND idx=?", (now(), campaign_id, cand['idx']))
        # 2. adaptive step or grid dispatch
        if c['adaptive_json']:
            self._adaptive_step(db, c, owner, base)
        else:
            inflight = db.execute("SELECT COUNT(*) FROM sci_campaign_candidates WHERE campaign_id=? AND state IN ('queued','running')", (campaign_id,)).fetchone()[0]
            room = LIMITS['max_inflight'] - inflight
            if room > 0:
                for cand in db.execute("SELECT idx, params_json FROM sci_campaign_candidates WHERE campaign_id=? AND state='unevaluated' ORDER BY idx LIMIT ?", (campaign_id, room)).fetchall():
                    self._dispatch(db, c, owner, base, json.loads(cand['params_json']), cand['idx'])
        # 3. completion
        remaining = db.execute("SELECT COUNT(*) FROM sci_campaign_candidates WHERE campaign_id=? AND state IN ('unevaluated','queued','running')", (campaign_id,)).fetchone()[0]
        c = self._campaign(db, campaign_id)
        adaptive = json.loads(c['adaptive_json']) if c['adaptive_json'] else None
        if remaining == 0 and (adaptive is None or adaptive['stopping_reason']):
            final = 'budget_exhausted' if adaptive and adaptive['stopping_reason'] == 'evaluation_budget_exhausted' else 'completed'
            db.execute('UPDATE sci_campaigns SET state=?, updated_at=?, finished_at=? WHERE id=?', (final, now(), now(), campaign_id))
            history.record(db, c['workspace'], 'scheduler', 'job.result_committed', 'campaign', campaign_id, {'state': final})
            return final
        db.execute('UPDATE sci_campaigns SET updated_at=? WHERE id=?', (now(), campaign_id))
        return 'running'

    def _adaptive_step(self, db, c, owner, base):
        state = json.loads(c['adaptive_json'])
        if state.get('strategy') == 'acquisition':
            return self._acquisition_step(db, c, owner, base, state)
        return self._bisection_step(db, c, owner, base)

    def _acquisition_step(self, db, c, owner, base, state):
        import random
        if state['stopping_reason']:
            return
        if db.execute("SELECT 1 FROM sci_campaign_candidates WHERE campaign_id=? AND state IN ('queued','running')", (c['id'],)).fetchone():
            return                                            # exactly one evaluation in flight; a restart never re-dispatches it
        obj = state['acquisition']['objective']; maxim = obj['direction'] == 'max'
        done_idx = {e['index'] for e in state['evaluated']}
        for row in db.execute("SELECT idx, state, outcome, params_json, summary_json FROM sci_campaign_candidates WHERE campaign_id=? AND state IN ('succeeded','failed','cancelled','invalid') ORDER BY idx", (c['id'],)).fetchall():
            if row['idx'] in done_idx:
                continue
            value = None
            if row['state'] == 'succeeded':
                summary = json.loads(row['summary_json'] or '{}')
                value = (1 if robust_ok(row['outcome']) else 0) if obj['field'] == 'feasible' else summary.get(obj['field'])
                if type(value) not in (int, float) or isinstance(value, bool):
                    value = None
            state['evaluated'].append({'index': row['idx'], 'params': json.loads(row['params_json']), 'state': row['state'], 'outcome': row['outcome'] if row['state'] == 'succeeded' else row['state'], 'observed': value})
            if value is not None and (state['best'] is None or (value > state['best']['observed'] if maxim else value < state['best']['observed'])):
                state['best'] = {'index': row['idx'], 'params': json.loads(row['params_json']), 'observed': value}
        evaluated = {e['index'] for e in state['evaluated']}
        pending = [(i, cd) for i, cd in enumerate(state['candidates']) if i not in evaluated]
        if not pending:
            state['stopping_reason'] = 'candidate_set_exhausted'
        elif state['budget_left'] <= 0:
            state['stopping_reason'] = 'evaluation_budget_exhausted'
        else:
            observed = [e for e in state['evaluated'] if e['observed'] is not None]
            axes = sorted({k for cd in state['candidates'] for k in cd})
            span = {a: max(1, max(cd.get(a, 0) for cd in state['candidates']) - min(cd.get(a, 0) for cd in state['candidates'])) for a in axes}
            def dist(p, q):
                return math.sqrt(sum(((p.get(a, 0) - q.get(a, 0)) / span[a]) ** 2 for a in axes))
            if not observed:
                rng = random.Random(state['seed'] + len(state['evaluated']))
                idx, cd = pending[rng.randrange(len(pending))]
                choice = {'index': idx, 'score': None, 'predicted': None, 'improvement': None, 'exploration': None, 'reason': 'seeded first evaluation (no observation yet)'}
            else:
                vals = [e['observed'] for e in observed]; vrange = max(vals) - min(vals); best = state['best']['observed']
                scored = []
                for idx, cd in pending:
                    ws = [(1.0 / (dist(cd, e['params']) + 1e-9), e['observed']) for e in observed]
                    predicted = sum(w * v for w, v in ws) / sum(w for w, _ in ws)
                    improvement = max(0.0, predicted - best) if maxim else max(0.0, best - predicted)
                    nearest = min(dist(cd, e['params']) for e in state['evaluated'])
                    exploration = state['acquisition']['exploration_percent'] / 100.0 * max(vrange, 1) * nearest
                    scored.append((-(improvement + exploration), idx, cd, predicted, improvement, exploration))
                scored.sort(key=lambda t: (t[0], t[1]))
                neg, idx, cd, predicted, improvement, exploration = scored[0]
                if -neg < state['acquisition']['stop_below']:
                    state['stopping_reason'] = 'acquisition_below_threshold'
                    state['log'].append({'index': None, 'score': -neg, 'reason': 'best acquisition score below stop_below; no further evaluation dispatched'})
                choice = {'index': idx, 'score': -neg, 'predicted': predicted, 'improvement': improvement, 'exploration': exploration, 'reason': 'highest acquisition score (ties by lowest index)'}
            if state['stopping_reason'] is None:
                state['log'].append(choice)
                self._dispatch(db, c, owner, base, cd, idx)
                state['budget_left'] -= 1
        state['remaining_candidates'] = len([i for i in range(len(state['candidates'])) if i not in evaluated]) - (0 if state['stopping_reason'] else 1)
        db.execute('UPDATE sci_campaigns SET adaptive_json=?, updated_at=? WHERE id=?', (json.dumps(state), now(), c['id']))
        if state['stopping_reason']:
            db.execute("UPDATE sci_campaign_candidates SET state='cancelled', outcome='not_selected_by_acquisition', updated_at=? WHERE campaign_id=? AND state='unevaluated'", (now(), c['id']))

    def _bisection_step(self, db, c, owner, base):
        """Bisection with an explicit bracket. For max_feasible (axis up_worse) the feasible set is a prefix
        [domain_lo, boundary]; for min_feasible (axis up_better) it is a suffix [boundary, domain_hi].
        State: lo/hi = current bracket, phase check_first -> check_second -> bisect, evaluated = decisions."""
        state = json.loads(c['adaptive_json'])
        definition = merkle.parse(c['definition_json'])
        axis, objective = definition['adaptive']['axis'], definition['adaptive']['objective']
        if state['stopping_reason']:
            return
        if db.execute("SELECT 1 FROM sci_campaign_candidates WHERE campaign_id=? AND state IN ('unevaluated','queued','running')", (c['id'],)).fetchone():
            return                                            # exactly one evaluation in flight
        maxf = objective == 'max_feasible'
        last = db.execute("SELECT idx, state, outcome, params_json FROM sci_campaign_candidates WHERE campaign_id=? ORDER BY idx DESC LIMIT 1", (c['id'],)).fetchone()
        if last is not None and len(state['evaluated']) < last['idx'] + 1:
            value = json.loads(last['params_json'])[axis]
            ok = last['state'] == 'succeeded' and robust_ok(last['outcome'])
            state['evaluated'].append({'index': last['idx'], 'value': value, 'outcome': last['outcome'] if last['state'] == 'succeeded' else last['state'], 'robust_feasible': ok})
            if last['state'] != 'succeeded':
                state['stopping_reason'] = 'evaluation_failed_or_invalid'
            elif state['phase'] == 'check_first':          # max: lo must be feasible; min: hi must be feasible
                if ok:
                    state['phase'] = 'check_second'
                else:
                    state['stopping_reason'] = 'no_feasible_point_in_domain'
            elif state['phase'] == 'check_second':         # max: hi; min: lo
                if ok:
                    state['boundary'] = state['hi'] if maxf else state['lo']
                    state['stopping_reason'] = 'boundary_at_domain_end' if maxf else 'boundary_at_domain_start'
                else:
                    state['phase'] = 'bisect'              # invariant: max -> lo feasible, hi infeasible; min -> lo infeasible, hi feasible
            else:
                if maxf:
                    if ok: state['lo'] = value
                    else: state['hi'] = value
                else:
                    if ok: state['hi'] = value
                    else: state['lo'] = value
        if state['stopping_reason'] is None:
            if state['phase'] == 'bisect' and state['hi'] - state['lo'] <= 1:
                state['boundary'] = state['lo'] if maxf else state['hi']
                state['stopping_reason'] = 'bracket_closed'
            elif state['budget_left'] <= 0:
                state['stopping_reason'] = 'evaluation_budget_exhausted'
            else:
                if state['phase'] == 'check_first':
                    value = state['lo'] if maxf else state['hi']
                elif state['phase'] == 'check_second':
                    value = state['hi'] if maxf else state['lo']
                else:
                    value = (state['lo'] + state['hi']) // 2
                index = db.execute('SELECT COALESCE(MAX(idx),-1)+1 FROM sci_campaign_candidates WHERE campaign_id=?', (c['id'],)).fetchone()[0]
                db.execute('INSERT INTO sci_campaign_candidates (campaign_id, idx, params_json, state, updated_at) VALUES (?,?,?,?,?)',
                           (c['id'], index, json.dumps({axis: value}), 'unevaluated', now()))
                self._dispatch(db, c, owner, base, {axis: value}, index)
                state['budget_left'] -= 1
        state['remaining_bracket'] = [state['lo'], state['hi']]
        db.execute('UPDATE sci_campaigns SET adaptive_json=?, total_candidates=(SELECT COUNT(*) FROM sci_campaign_candidates WHERE campaign_id=?), updated_at=? WHERE id=?',
                   (json.dumps(state), c['id'], now(), c['id']))

    def tick_all(self, db, limit=20):
        return [self.tick(db, r['id']) for r in db.execute("SELECT id FROM sci_campaigns WHERE state='running' ORDER BY updated_at LIMIT ?", (limit,)).fetchall()]

    # ---- Pareto ---------------------------------------------------------------------------------
    def pareto(self, db, principal, campaign_id, objectives, require_outcomes=('FEASIBLE',) + PLAN_FEASIBLE):
        """Conservative interval dominance over succeeded candidates. objectives: [{field, direction}]
        where field is an axis value or a result field; interval fields ([lo, hi]) compare by worst/best ends."""
        principal.require('job:read_private')
        c = self._campaign(db, campaign_id, principal.workspace)
        if type(objectives) is not list or not 1 <= len(objectives) <= 4:
            raise ServiceError('VALIDATION', 'objectives (1-4)')
        for o in objectives:
            if type(o) is not dict or set(o) != {'field', 'direction'} or o['direction'] not in ('min', 'max') or type(o['field']) is not str:
                raise ServiceError('VALIDATION', 'objective shape {field, direction}')
        rows = db.execute("SELECT * FROM sci_campaign_candidates WHERE campaign_id=? AND state='succeeded' ORDER BY idx", (campaign_id,)).fetchall()
        cands, excluded = [], []
        for r in rows:
            if r['outcome'] not in require_outcomes:
                excluded.append({'index': r['idx'], 'reason': 'outcome ' + str(r['outcome']) + ' not in required ' + ','.join(require_outcomes)}); continue
            params, summary = json.loads(r['params_json']), json.loads(r['summary_json'] or '{}')
            vals, missing = [], []
            for o in objectives:
                v = params.get(o['field'], summary.get(o['field']))
                if v is None:
                    missing.append(o['field']); continue
                if type(v) is list and len(v) == 2 and all(type(x) is int for x in v):
                    lo, hi = v
                elif type(v) is int:
                    lo = hi = v
                else:
                    missing.append(o['field']); continue
                vals.append((lo, hi))
            if missing:
                excluded.append({'index': r['idx'], 'reason': 'missing objective(s): ' + ','.join(missing)}); continue
            cands.append({'index': r['idx'], 'params': params, 'values': vals})
        def dominates(a, b):
            better_somewhere = False
            for (alo, ahi), (blo, bhi), o in zip(a['values'], b['values'], objectives):
                if o['direction'] == 'min':
                    if ahi > blo: return False           # a's worst must be <= b's best
                    if ahi < blo: better_somewhere = True
                else:
                    if alo < bhi: return False
                    if alo > bhi: better_somewhere = True
            return better_somewhere
        front, dominated = [], []
        for a in cands:
            witness = next((b for b in cands if b is not a and dominates(b, a)), None)
            if witness is None:
                front.append(a)
            else:
                dominated.append({'index': a['index'], 'reason': 'dominated', 'dominated_by': witness['index']})
        return {'campaign_id': campaign_id, 'objectives': objectives, 'required_outcomes': list(require_outcomes),
                'non_dominated': [{'index': a['index'], 'params': a['params'], 'values': a['values']} for a in front],
                'excluded': excluded + dominated, 'rule': 'conservative interval dominance: for min, dominator worst <= other best on every objective, strictly better on one; '
                'for max, dominator best-worst >= other best; scalars are zero-width intervals',
                'note': 'a Pareto set does not identify a uniquely best choice; no cross-asset valuation is performed'}

    def plot_svg(self, db, principal, campaign_id):
        """Outcome grid over the first two axes from persisted results (accessible table is the primary view)."""
        data = self.results(db, principal, campaign_id)
        axes = sorted({k for r in data['rows'] for k in r['params']})[:2]
        if len(axes) < 1:
            return '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"></svg>'
        xs = sorted({r['params'][axes[0]] for r in data['rows']})
        ys = sorted({r['params'][axes[1]] for r in data['rows']}) if len(axes) > 1 else [0]
        cell, pad = 22, 60
        w, h = pad + cell * len(xs) + 10, pad + cell * len(ys) + 10
        colors = {'FEASIBLE': '#1c6b3a', 'INDETERMINATE': '#8a5a00', 'INFEASIBLE': '#9b1c1c', 'ROBUSTLY_FEASIBLE': '#1c6b3a', 'CAPPED': '#8a5a00',
                  'CAPPED_MODEL_UNBOUNDED': '#8a5a00', 'BASE_PLAN_INFEASIBLE': '#9b1c1c'}
        parts = ['<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" role="img" aria-label="campaign outcome grid over %s">' % (w, h, ' and '.join(axes))]
        for r in data['rows']:
            xi = xs.index(r['params'][axes[0]]); yi = ys.index(r['params'][axes[1]]) if len(axes) > 1 else 0
            state = r['state']; outcome = r.get('outcome')
            fill = colors.get(outcome, '#ffffff') if state == 'succeeded' else {'invalid': '#cccccc', 'failed': '#333333', 'cancelled': '#999999'}.get(state, '#f4f4f4')
            label = outcome if state == 'succeeded' else state
            parts.append('<rect x="%d" y="%d" width="%d" height="%d" fill="%s" stroke="#d8dcd6"><title>%s=%s %s: %s</title></rect>' %
                         (pad + xi * cell, pad + yi * cell, cell - 1, cell - 1, fill, axes[0], r['params'][axes[0]], (axes[1] + '=' + str(r['params'][axes[1]])) if len(axes) > 1 else '', label))
        parts.append('<text x="%d" y="%d" font-size="11">%s (%d values)</text>' % (pad, 20, axes[0], len(xs)))
        if len(axes) > 1:
            parts.append('<text x="4" y="%d" font-size="11">%s (%d values)</text>' % (40, axes[1], len(ys)))
        parts.append('<text x="%d" y="%d" font-size="10">green feasible, amber indeterminate/capped, red infeasible, grey invalid/unevaluated, black failed</text>' % (4, h - 2))
        parts.append('</svg>')
        return ''.join(parts)

    # ---- refinement what-if as an immutable derived result ----------------------------------------
    def refine_job(self, db, principal, job_id, refinements):
        principal.require('job:read_private')
        job = self.jobs.get(db, principal, job_id)
        if job['kind'] != 'temporal_energy' or job['state'] != 'succeeded':
            raise ServiceError('CONFLICT', 'refinement applies to a succeeded temporal_energy job')
        contract = db.execute('SELECT * FROM contracts WHERE id=?', (job['contract_id'],)).fetchone()
        vault = self.store.load_json(db, contract['input_artifact_id'], principal.workspace)
        inputs = {f['name']: f['value'] for f in vault['fields']}['inputs']
        from . import temporal
        result = temporal.refine(inputs, refinements)
        payload = {'schema': 'temporal-refinement/v1', 'source_job': job_id, 'source_evidence_root': job['evidence_root'], 'refinements': refinements, 'result': result}
        aid = self.store.store(db, workspace=principal.workspace, kind='evidence_vault', owner_id=principal.id, plaintext=merkle.canonical(payload),
                               recipients=[], intended_use='refinement-what-if;private', job_id=job_id, contract_id=contract['id'])
        add_edge(db, principal.workspace, 'job', job_id, 'artifact', aid, 'derived_from')
        history.record(db, principal.workspace, principal.id, 'job.result_committed', 'artifact', aid, {'refinement_of': job_id, 'hypothetical': True})
        return dict(result, artifact_id=aid, source_job=job_id)

    # ---- §46(3) experiment branching -------------------------------------------------------
    OUTCOME_RANK = {'FEASIBLE': 2, 'ROBUSTLY_FEASIBLE': 2, 'INDETERMINATE': 1, 'INFEASIBLE': 0, 'ROBUSTLY_INFEASIBLE': 0,
                    'optimal_within_tolerance': 2, 'feasible_incumbent_no_optimality_claim': 2, 'limit_no_candidate': 1, 'numerical_failure': 1, 'candidate_rejected_by_checker': 1,
                    'infeasible_by_solver': 0, 'infeasible_established_by_enumeration': 0}

    def lineage_head(self, db, campaign_id):
        """The newest branch derived from a campaign (or the campaign itself): the version an agent must build on."""
        row = db.execute("SELECT to_id FROM lineage_edges WHERE from_type='campaign' AND from_id=? AND to_type='campaign' AND relation='derived_from' ORDER BY created_at DESC, to_id DESC LIMIT 1", (campaign_id,)).fetchone()
        return row['to_id'] if row else campaign_id

    def branch(self, db, principal, campaign_id, *, base_changes=None, axes=None, candidate_indexes=None, name=None, changes=None, expected_head=None):
        """Fork a campaign from its authorized (succeeded) results with explicit changed assumptions. The branch is a
        new campaign whose definition is the original's with the stated base overrides (and optionally new axes or the
        smallest product grid containing the selected candidates). Nothing from the original is re-used as evidence."""
        principal.require('job:read_private')                      # the base input is private to the workspace owner
        c = self._campaign(db, campaign_id, principal.workspace)
        definition = merkle.parse(c['definition_json'])
        if definition.get('adaptive') is not None:
            raise ServiceError('VALIDATION', {'code': 'branch_adaptive', 'detail': 'branch a grid campaign; adaptive searches are re-run, not forked'})
        head = self.lineage_head(db, campaign_id)
        if expected_head is not None and expected_head != head:
            raise ServiceError('CONFLICT', {'code': 'stale_branch', 'expected_head': expected_head, 'head': head, 'detail': 'a newer branch of this campaign exists; rebase your change on the head or pass expected_head=head explicitly'})
        base_changes = base_changes or {}
        if type(base_changes) is not dict or not all(type(k) is str and type(v) is int and type(v) is not bool for k, v in base_changes.items()):
            raise ServiceError('VALIDATION', {'code': 'base_changes', 'expected': '{field: integer}'})
        base = definition['base']
        recorded = []
        if changes:
            if type(changes) is not list or len(changes) > LIMITS['max_changes']:
                raise ServiceError('VALIDATION', {'code': 'changes', 'max': LIMITS['max_changes']})
            if 'dataset_version_id' in base:
                raise ServiceError('VALIDATION', {'code': 'changes_on_dataset_base', 'detail': 'typed path changes apply to inline bases; use base_changes for dataset parameters'})
            for ch in changes:
                if type(ch) is not dict or set(ch) - {'path', 'value', 'source', 'note'} or type(ch.get('path')) is not str or ch.get('source') not in CHANGE_SOURCES or isinstance(ch.get('value'), bool) or type(ch.get('value')) not in (int, str):
                    raise ServiceError('VALIDATION', {'code': 'change_shape', 'expected': {'path': 'field | list.index | tasks.<id>.<field>', 'value': 'int | str', 'source': list(CHANGE_SOURCES)}})
                if ch['source'] == 'reviewed_table_correction' and not principal.can('review:decide'):
                    raise ServiceError('FORBIDDEN', {'code': 'change_source_requires_reviewer', 'source': ch['source']})
                prev, base = apply_change(base, ch['path'], ch['value'])
                recorded.append({'path': ch['path'], 'previous': prev, 'value': ch['value'], 'source': ch['source'], 'note': (ch.get('note') or '')[:200], 'by': principal.id})
        if 'dataset_version_id' in base:
            params = dict(base.get('parameters', {}))
            unknown = [k for k in base_changes if k not in params]
            if unknown:
                raise ServiceError('VALIDATION', {'code': 'unknown_base_field', 'fields': unknown, 'known': sorted(params)})
            new_base = dict(base, parameters=dict(params, **base_changes))
        else:
            unknown = [k for k in base_changes if k not in base or type(base[k]) is not int]
            if unknown:
                raise ServiceError('VALIDATION', {'code': 'unknown_base_field', 'fields': unknown, 'known': sorted(k for k, v in base.items() if type(v) is int)})
            new_base = dict(base, **base_changes)
        new_axes = axes if axes is not None else definition.get('axes')
        if candidate_indexes is not None:
            if type(candidate_indexes) is not list or not candidate_indexes or not all(type(i) is int for i in candidate_indexes):
                raise ServiceError('VALIDATION', 'candidate_indexes: non-empty integer list')
            rows = {r['idx']: r for r in db.execute('SELECT idx, state, params_json FROM sci_campaign_candidates WHERE campaign_id=?', (campaign_id,))}
            bad = [i for i in candidate_indexes if i not in rows or rows[i]['state'] != 'succeeded']
            if bad:
                raise ServiceError('VALIDATION', {'code': 'candidate_not_authorized', 'indexes': bad, 'detail': 'only succeeded candidates can seed a branch'})
            selected = [json.loads(rows[i]['params_json']) for i in candidate_indexes]
            new_axes = [{'path': a['path'], 'values': sorted({p[a['path']] for p in selected if a['path'] in p})} for a in (definition.get('axes') or [])]
            new_axes = [a for a in new_axes if a['values']]
        new_def = {k: v for k, v in definition.items() if k not in ('axes', 'base', 'name', 'tags', 'changes')}
        new_def.update({'name': name or (definition['name'] + ' / branch'), 'base': new_base, 'axes': new_axes,
                        'tags': sorted(set(definition.get('tags', [])) | {'branch-of:' + campaign_id})[:10]})
        if recorded:
            new_def['changes'] = recorded
        if not base_changes and axes is None and candidate_indexes is None and not recorded:
            raise ServiceError('VALIDATION', {'code': 'no_change', 'detail': 'a branch must state at least one changed assumption, new axes or a candidate selection'})
        out = self.create(db, principal, new_def)
        add_edge(db, principal.workspace, 'campaign', campaign_id, 'campaign', out['campaign_id'], 'derived_from')
        history.record(db, principal.workspace, principal.id, 'contract.created', 'campaign', out['campaign_id'],
                       {'branched_from': campaign_id, 'base_changes': sorted(base_changes), 'candidate_indexes': candidate_indexes, 'digest': out['digest']})
        return dict(out, branched_from=campaign_id, base_changes=base_changes, changes=recorded, axes=new_axes, name=new_def['name'], previous_head=head,
                    note='the branch re-evaluates every candidate; original evidence is never re-used')

    def compare(self, db, principal, a_id, b_id):
        """Candidate-by-candidate comparison of two campaigns joined on parameter values, plus the base-input differences
        (fields only for non-private principals)."""
        principal.require('job:read')
        ca, cb = self._campaign(db, a_id, principal.workspace), self._campaign(db, b_id, principal.workspace)
        if ca['kind'] != cb['kind']:
            raise ServiceError('VALIDATION', {'code': 'kind_mismatch', 'a': ca['kind'], 'b': cb['kind']})
        da, dbf = merkle.parse(ca['definition_json']), merkle.parse(cb['definition_json'])
        private = principal.can('job:read_private')
        ba, bb = da['base'], dbf['base']
        changed_fields = deep_diff(ba, bb)
        base_changes = {k: {'a': v[0], 'b': v[1]} for k, v in changed_fields.items()} if private else {k: 'changed' for k in changed_fields}
        def table(cid):
            return {r['params_json']: dict(r) for r in db.execute('SELECT idx, params_json, state, outcome, summary_json FROM sci_campaign_candidates WHERE campaign_id=?', (cid,))}
        ta, tb = table(a_id), table(b_id)
        matched, changes = [], []
        for key in sorted(set(ta) & set(tb)):
            ra, rb = ta[key], tb[key]
            item = {'params': json.loads(key), 'a': {'index': ra['idx'], 'state': ra['state'], 'outcome': ra['outcome']}, 'b': {'index': rb['idx'], 'state': rb['state'], 'outcome': rb['outcome']}}
            matched.append(item)
            if ca['kind'] == 'resource_plan' and private:
                for side, row in (('a', ra), ('b', rb)):
                    s = json.loads(row['summary_json'] or '{}')
                    item[side].update({k: s.get(k) for k in ('objective', 'selected', 'min_margin', 'first_violation', 'reason')})
                if ra['state'] == 'succeeded' and rb['state'] == 'succeeded':
                    sa, sb = json.loads(ra['summary_json'] or '{}'), json.loads(rb['summary_json'] or '{}')
                    item['plan_changes'] = {'selected_added': sorted(set(sb.get('selected') or []) - set(sa.get('selected') or [])), 'selected_removed': sorted(set(sa.get('selected') or []) - set(sb.get('selected') or [])),
                                            'objective_delta': (sb['objective'] - sa['objective']) if sa.get('objective') is not None and sb.get('objective') is not None else None,
                                            'min_margin_delta': (sb['min_margin'] - sa['min_margin']) if sa.get('min_margin') is not None and sb.get('min_margin') is not None else None}
            if ra['outcome'] != rb['outcome'] or ra['state'] != rb['state']:
                ranka, rankb = self.OUTCOME_RANK.get(ra['outcome']), self.OUTCOME_RANK.get(rb['outcome'])
                item['direction'] = 'unknown' if ranka is None or rankb is None else ('improved' if rankb > ranka else ('worsened' if rankb < ranka else 'changed'))
                changes.append(item)
        summary = {'matched': len(matched), 'changed': len(changes), 'improved': sum(c.get('direction') == 'improved' for c in changes),
                   'worsened': sum(c.get('direction') == 'worsened' for c in changes), 'unknown': sum(c.get('direction') == 'unknown' for c in changes),
                   'only_in_a': len(set(ta) - set(tb)), 'only_in_b': len(set(tb) - set(ta))}
        return {'a': {'campaign_id': a_id, 'name': ca['name'], 'state': ca['state'], 'digest': ca['digest']}, 'b': {'campaign_id': b_id, 'name': cb['name'], 'state': cb['state'], 'digest': cb['digest']},
                'base_changes': base_changes, 'recorded_changes': (dbf.get('changes') if private else [{'path': c['path'], 'source': c['source']} for c in dbf.get('changes') or []]), 'summary': summary, 'changes': changes[:500], 'matched': matched[:500] if ca['kind'] == 'resource_plan' else None,
                'note': 'candidates joined on identical parameter values; outcomes of unfinished candidates are null, never guessed; ranking FEASIBLE > INDETERMINATE > INFEASIBLE'}

