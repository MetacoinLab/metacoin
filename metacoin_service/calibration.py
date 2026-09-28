"""Calibration datasets, fitted linear models, predictions with explicit domain status, approval for scheduling,
prediction-versus-actual comparison, a bounded replay of the scheduling policy, and counterfactual planning.

Datasets are private rows (encrypted artifact) with declared units, a measurement policy and censoring flags.
Performance datasets are collected from this workspace's own completed compute runs (never pooled across
workspaces). A fitted model is the output of a calibration_fit job (verified by an independent refit); it becomes a
scheduling signal only after explicit approval for a scope, and only while fresh and matching the current
implementation. The scheduler treats every prediction as advisory: hard limits, reservations and fairness are
unchanged and a missing/stale/extrapolating prediction falls back to the conservative evidence thresholds."""
import hashlib
import json
import secrets
from decimal import Decimal, InvalidOperation

from experiments.private_receipts import receipt as merkle
from . import history
from .compute import calibration as cal, manifests as compute_manifests
from .datasets import add_edge
from .db import now
from .errors import ServiceError

PERFORMANCE_COLUMNS = ['work_units', 'backend_cuda', 'duration_ms', 'compute_ms', 'censored', 'attempt', 'started_at']
PERFORMANCE_UNITS = {'work_units': 'service work units', 'backend_cuda': '1 if cuda else 0', 'duration_ms': 'ms (attempt end to end, worker clock)', 'compute_ms': 'ms (child started to result)',
                     'censored': '1 = failed/timed out (duration is a lower bound)', 'attempt': 'attempt number', 'started_at': 'unix seconds'}
MAX_AGE_SECONDS = 30 * 86400
SCHEDULING_KEY = 'calibrated_scheduling_enabled'


def _dec(v):
    try:
        d = Decimal(v) if not isinstance(v, bool) else None
    except (InvalidOperation, TypeError, ValueError):
        d = None
    if d is None or not d.is_finite():
        raise ServiceError('VALIDATION', 'numeric values must be integers or finite decimal strings')
    return str(d)


class Calibration:
    def __init__(self, store, settings):
        self.store, self.settings = store, settings

    # ---- datasets ------------------------------------------------------------------------------------
    def create_numeric_dataset(self, db, principal, body):
        principal.require('calibration:write')
        name, target, columns, units, rows = body.get('name'), body.get('target'), body.get('columns'), body.get('units') or {}, body.get('rows')
        if type(name) is not str or not 1 <= len(name) <= 96 or type(columns) is not list or not 2 <= len(columns) <= cal.MAX_FEATURES + 1 or len(set(columns)) != len(columns):
            raise ServiceError('VALIDATION', 'name/columns (2..%d distinct)' % (cal.MAX_FEATURES + 1))
        if type(target) is not str or target not in columns:
            raise ServiceError('VALIDATION', 'target must be one of the columns')
        if type(units) is not dict or not all(k in columns and type(v) is str and len(v) <= 32 for k, v in units.items()):
            raise ServiceError('VALIDATION', 'units: column -> unit string')
        if type(rows) is not list or not 3 <= len(rows) <= cal.MAX_ROWS:
            raise ServiceError('VALIDATION', 'rows: 3..%d' % cal.MAX_ROWS)
        norm, missing = [], 0
        for r in rows:
            if type(r) is not dict or set(r) - set(columns):
                raise ServiceError('VALIDATION', 'row keys must be columns')
            out = []
            for c in columns:
                if c not in r or r[c] is None:
                    missing += 1; out.append(None)
                else:
                    out.append(_dec(r[c]))
            norm.append(out)
        policy = {'missing_values': 'rows with a missing feature/target are excluded from fits (count reported); missing is never treated as zero', 'missing_cells': missing,
                  'provenance': body.get('provenance', 'declared'), 'censored_column': None}
        return self._store_dataset(db, principal, name, 'numeric', target, columns, units, norm, policy, len(norm))

    def create_performance_dataset(self, db, principal, body):
        """Rows from this workspace's completed (or failed = censored) compute runs, per task kind."""
        principal.require('calibration:write')
        kind = body.get('task_kind')
        if kind not in compute_manifests.KINDS:
            raise ServiceError('VALIDATION', {'code': 'task_kind', 'allowed': list(compute_manifests.KINDS)})
        since = body.get('since', 0)
        if type(since) is not int or since < 0:
            raise ServiceError('VALIDATION', 'since')
        name = body.get('name') or ('performance ' + kind)
        rows = db.execute("SELECT r.*, j.state, j.attempt, j.error_code FROM compute_runs r JOIN jobs j ON j.id=r.job_id WHERE r.workspace=? AND r.kind=? AND r.duration_ms IS NOT NULL AND r.started_at >= ? "
                          "AND j.state IN ('succeeded','failed') ORDER BY r.started_at, r.job_id LIMIT ?", (principal.workspace, kind, since, cal.MAX_ROWS)).fetchall()
        norm = []
        for r in rows:
            censored = 1 if r['state'] == 'failed' else 0
            norm.append([str(r['work_total']), '1' if r['selected_backend'] == 'cuda' else '0', str(r['duration_ms']), str(r['compute_ms']) if r['compute_ms'] is not None else None, str(censored), str(r['attempt']), str(r['started_at'])])
        if len(norm) < 3:
            raise ServiceError('CONFLICT', {'code': 'insufficient_measurements', 'rows': len(norm), 'note': 'run at least three completed jobs of this kind in this workspace first'})
        policy = {'source': 'compute_runs of this workspace only (never pooled)', 'target_meaning': {'duration_ms': 'end-to-end attempt duration incl. interpreter start and device context', 'compute_ms': 'child started to result (kernel + checkpoints)'},
                  'censoring': 'failed attempts keep their observed duration with censored=1 (a lower bound); exclude them for point prediction or model them separately', 'conditions': 'shared host; concurrency not controlled; versions recorded per row in compute_runs.versions_json',
                  'exclusions': 'paused/cancelled attempts, runs without duration', 'missing_values': 'compute_ms is missing when the child never reported start'}
        ds = self._store_dataset(db, principal, name, 'performance', 'duration_ms', PERFORMANCE_COLUMNS, PERFORMANCE_UNITS, norm, policy, len(norm))
        ds['task_kind'] = kind
        db.execute('UPDATE calibration_datasets SET scope_json=? WHERE id=?', (json.dumps({'task_kind': kind}), ds['id']))
        return ds

    def _store_dataset(self, db, principal, name, kind, target, columns, units, rows, policy, count):
        payload = merkle.canonical({'columns': columns, 'rows': rows})
        aid = self.store.store(db, workspace=principal.workspace, kind='dataset_normalized', owner_id=principal.id, plaintext=payload, recipients=[], intended_use='calibration-rows;owner-worker',
                               limit_bytes=self.settings.limits['compute_max_artifact_bytes'])
        did = 'cd_' + secrets.token_hex(6)
        db.execute('INSERT INTO calibration_datasets (id, workspace, owner_id, name, kind, target, columns_json, units_json, rows_artifact_id, row_count, digest, policy_json, scope_json, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   (did, principal.workspace, principal.id, name, kind, target, json.dumps(columns), json.dumps(units), aid, count, hashlib.sha256(payload).hexdigest(), json.dumps(policy), None, now()))
        add_edge(db, principal.workspace, 'artifact', aid, 'calibration_dataset', did, 'normalized_from')
        history.record(db, principal.workspace, principal.id, 'calibration.dataset', 'calibration_dataset', did, {'kind': kind, 'rows': count, 'target': target})
        return self.dataset_view(self.dataset(db, principal, did))

    def dataset(self, db, principal, did):
        row = db.execute('SELECT * FROM calibration_datasets WHERE id=? AND workspace=?', (did, principal.workspace)).fetchone()
        if row is None:
            raise ServiceError('NOT_FOUND', 'calibration dataset')
        return row

    @staticmethod
    def dataset_view(row):
        return {'id': row['id'], 'name': row['name'], 'kind': row['kind'], 'target': row['target'], 'columns': json.loads(row['columns_json']), 'units': json.loads(row['units_json']), 'rows': row['row_count'],
                'digest': row['digest'], 'policy': json.loads(row['policy_json']), 'scope': json.loads(row['scope_json']) if row['scope_json'] else None, 'created_at': row['created_at']}

    def list_datasets(self, db, principal):
        principal.require('job:read')
        return [self.dataset_view(r) for r in db.execute('SELECT * FROM calibration_datasets WHERE workspace=? ORDER BY created_at', (principal.workspace,)).fetchall()]

    def rows(self, db, principal, did):
        principal.require('artifact:read_private')
        row = self.dataset(db, principal, did)
        data = merkle.parse(self.store.load(db, row['rows_artifact_id'], principal.workspace))
        return {'id': did, 'columns': data['columns'], 'rows': data['rows'], 'units': json.loads(row['units_json'])}

    # ---- models --------------------------------------------------------------------------------------
    def register_from_job(self, db, job_row, store):
        """Called when a calibration_fit job succeeds: read model.json from the output container and record the model."""
        from .compute import container
        run = db.execute('SELECT * FROM compute_runs WHERE job_id=?', (job_row['id'],)).fetchone()
        if run is None or not run['output_artifact_id']:
            return None
        files = container.unpack(store.load(db, run['output_artifact_id'], job_row['workspace']))
        m = json.loads(files['model.json'])
        contract = db.execute('SELECT * FROM contracts WHERE id=?', (job_row['contract_id'],)).fetchone()
        inputs = store.load_json(db, contract['input_artifact_id'], job_row['workspace'])
        inputs = {f['name']: f['value'] for f in inputs['fields']}['inputs'] if 'fields' in inputs else inputs
        ds = db.execute('SELECT * FROM calibration_datasets WHERE id=?', (m['dataset_id'],)).fetchone()
        scope = inputs.get('scope') or (json.loads(ds['scope_json']) if ds and ds['scope_json'] else None)
        mid = 'cm_' + secrets.token_hex(6)
        ver = json.loads(run['verification_json'] or '{}')
        db.execute('INSERT INTO calibration_models (id, workspace, dataset_id, job_id, kind, state, target, features_json, scope_json, manifest_artifact_id, metrics_json, domain_json, warnings_json, '
                   'implementation_digest, verification_passed, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   (mid, job_row['workspace'], m['dataset_id'], job_row['id'], ds['kind'] if ds else 'numeric', 'fitted', m['target'], json.dumps(m['features']), json.dumps(scope) if scope else None,
                    run['output_artifact_id'], json.dumps(m['metrics']), json.dumps(m['domain']), json.dumps(m['warnings']), run['implementation_digest'], int(bool(ver.get('passed'))), now()))
        add_edge(db, job_row['workspace'], 'job', job_row['id'], 'calibration_model', mid, 'produced')
        history.record(db, job_row['workspace'], 'worker', 'calibration.fit', 'calibration_model', mid, {'job_id': job_row['id'], 'dataset_id': m['dataset_id'], 'verified': bool(ver.get('passed')), 'warnings': len(m['warnings'])})
        return mid

    def model(self, db, principal, mid):
        row = db.execute('SELECT * FROM calibration_models WHERE id=? AND workspace=?', (mid, principal.workspace)).fetchone()
        if row is None:
            raise ServiceError('NOT_FOUND', 'calibration model')
        return row

    def manifest_of(self, db, row):
        from .compute import container
        files = container.unpack(self.store.load(db, row['manifest_artifact_id'], row['workspace']))
        return json.loads(files['model.json']), json.loads(files['predictions.json'])

    def model_view(self, db, row, full=False):
        out = {'id': row['id'], 'dataset_id': row['dataset_id'], 'job_id': row['job_id'], 'kind': row['kind'], 'state': row['state'], 'target': row['target'], 'features': json.loads(row['features_json']),
               'scope': json.loads(row['scope_json']) if row['scope_json'] else None, 'metrics': json.loads(row['metrics_json']), 'domain': json.loads(row['domain_json']), 'warnings': json.loads(row['warnings_json']),
               'verification_passed': bool(row['verification_passed']), 'implementation_digest': row['implementation_digest'], 'approved_by': row['approved_by'], 'approved_at': row['approved_at'],
               'retired_at': row['retired_at'], 'created_at': row['created_at'], 'fresh': self.fresh(row), 'implementation_current': row['implementation_digest'] == compute_manifests.implementation_digest(),
               'default_for': [r['scope'] for r in db.execute('SELECT scope FROM calibration_defaults WHERE model_id=?', (row['id'],)).fetchall()]}
        if full:
            m, preds = self.manifest_of(db, row)
            out['manifest'] = m; out['predictions'] = preds
        return out

    def fresh(self, row):
        return now() - row['created_at'] <= self.settings.limits.get('calibration_max_age_seconds', MAX_AGE_SECONDS)

    def list_models(self, db, principal):
        principal.require('job:read')
        return [self.model_view(db, r) for r in db.execute('SELECT * FROM calibration_models WHERE workspace=? ORDER BY created_at', (principal.workspace,)).fetchall()]

    def predict(self, db, principal, mid, features):
        principal.require('job:read')
        row = self.model(db, principal, mid)
        m, _ = self.manifest_of(db, row)
        if type(features) is not dict or set(features) != set(m['features']):
            raise ServiceError('VALIDATION', {'code': 'features', 'expected': m['features']})
        vals = {}
        for k, v in features.items():
            try:
                vals[k] = cal.to_float(v)
            except ValueError as exc:
                raise ServiceError('VALIDATION', {'code': 'feature_value', 'feature': k, 'reason': str(exc)}) from None
        status, outside = cal.domain_status(vals, m['domain'])
        pred = cal.apply_model(m, vals)
        iv = m.get('prediction_interval')
        out = {'model_id': mid, 'prediction': repr(pred), 'unit': m.get('target_unit'), 'domain_status': status, 'outside_domain': {k: [repr(v), m['domain'][k]] for k, v in outside.items()},
               'interval': ({'low': repr(pred + float(iv['low_offset'])), 'high': repr(pred + float(iv['high_offset'])), 'level_percent': iv['level_percent'], 'basis': iv['basis'], 'assumption': iv['assumption']} if iv else None),
               'usable_for_scheduling': row['state'] == 'approved' and status == 'interpolation' and self.fresh(row) and row['implementation_digest'] == compute_manifests.implementation_digest(),
               'warnings': m['warnings'], 'meaning': 'point estimate of a fitted linear relationship; not a bound; extrapolation is labelled and refused for automatic use'}
        if pred != pred or pred in (float('inf'), float('-inf')):
            raise ServiceError('COMPUTATION', 'non-finite prediction')
        return out

    def design(self, db, principal, mid, body):
        """§65-3: rank candidate measurements by predicted utility under this model's design geometry (no execution)."""
        principal.require('calibration:write')
        from . import design as design_mod
        row = self.model(db, principal, mid)
        m, _ = self.manifest_of(db, row)
        data = rows_for_fit(db, self.store, principal.workspace, {'dataset_id': m['dataset_id'], 'features': m['features'], 'target': m['target']})
        idx = [data['columns'].index(f) for f in m['features']]
        train = [{f: cal.to_float(r[j]) for f, j in zip(m['features'], idx)} for k, r in enumerate(data['rows']) if k in set(m.get('train_indexes', range(len(data['rows']))))]
        out = design_mod.suggest(m, train, body if type(body) is dict else {})
        out['model_id'] = mid; out['model_state'] = row['state']
        history.record(db, principal.workspace, principal.id, 'calibration.dataset', 'calibration_model', mid, {'design_suggestion': True, 'candidates': len(body.get('candidates', [])), 'selected': len(out['selected'])})
        return out

    def approve(self, db, principal, mid, evidence=None):
        principal.require('calibration:write')
        row = self.model(db, principal, mid)
        if not row['verification_passed']:
            raise ServiceError('CONFLICT', 'only a verified fit can be approved')
        if row['state'] == 'retired':
            raise ServiceError('CONFLICT', 'model retired')
        scope = json.loads(row['scope_json']) if row['scope_json'] else None
        if not scope or 'task_kind' not in scope:
            raise ServiceError('CONFLICT', 'approval for scheduling needs a performance scope {task_kind, backend?}')
        key = scope['task_kind'] + ':' + scope.get('backend', 'any')
        prev = db.execute('SELECT model_id FROM calibration_defaults WHERE scope=?', (key,)).fetchone()
        db.execute("UPDATE calibration_models SET state='approved', approved_by=?, approved_at=? WHERE id=?", (principal.id, now(), mid))
        db.execute('INSERT INTO calibration_defaults (scope, workspace, model_id, set_by, previous_model_id, evidence_json, updated_at) VALUES (?,?,?,?,?,?,?) ON CONFLICT(scope) DO UPDATE SET model_id=excluded.model_id, set_by=excluded.set_by, previous_model_id=excluded.previous_model_id, evidence_json=excluded.evidence_json, updated_at=excluded.updated_at',
                   (key, principal.workspace, mid, principal.id, prev['model_id'] if prev else None, json.dumps(evidence or {}), now()))
        history.record(db, principal.workspace, principal.id, 'calibration.promoted', 'calibration_model', mid, {'scope': key, 'previous': prev['model_id'] if prev else None, 'evidence_keys': sorted(evidence or {})})
        return self.model_view(db, self.model(db, principal, mid))

    def retire(self, db, principal, mid):
        principal.require('calibration:write')
        row = self.model(db, principal, mid)
        db.execute("UPDATE calibration_models SET state='retired', retired_at=? WHERE id=?", (now(), mid))
        for d in db.execute('SELECT scope FROM calibration_defaults WHERE model_id=?', (mid,)).fetchall():
            db.execute('DELETE FROM calibration_defaults WHERE scope=?', (d['scope'],))
        history.record(db, principal.workspace, principal.id, 'calibration.promoted', 'calibration_model', mid, {'retired': True})
        return self.model_view(db, self.model(db, principal, mid))

    def comparison(self, db, principal, mid, limit=200):
        """Predictions versus actual outcomes for the caller's completed compute jobs in the model's scope."""
        principal.require('job:read_private')
        row = self.model(db, principal, mid)
        m, _ = self.manifest_of(db, row)
        scope = json.loads(row['scope_json']) if row['scope_json'] else None
        if not scope or row['kind'] != 'performance':
            raise ServiceError('CONFLICT', 'comparison is for performance models with a task scope')
        runs = db.execute("SELECT r.*, j.state FROM compute_runs r JOIN jobs j ON j.id=r.job_id WHERE r.workspace=? AND r.kind=? AND r.duration_ms IS NOT NULL AND j.state='succeeded' ORDER BY r.started_at DESC LIMIT ?",
                          (principal.workspace, scope['task_kind'], limit)).fetchall()
        rows = []
        for r in runs:
            feats = {'work_units': float(r['work_total']), 'backend_cuda': 1.0 if r['selected_backend'] == 'cuda' else 0.0, 'attempt': float(r['attempt'] if 'attempt' in r.keys() else 1), 'started_at': float(r['started_at'] or 0)}
            feats = {k: feats[k] for k in m['features'] if k in feats}
            if set(feats) != set(m['features']):
                rows.append({'job_id': r['job_id'], 'prediction': None, 'status': 'unavailable: model features not derivable from this run'}); continue
            status, _ = cal.domain_status(feats, m['domain'])
            pred = cal.apply_model(m, feats)
            actual = float(r[m['target']]) if m['target'] in r.keys() and r[m['target']] is not None else None
            rows.append({'job_id': r['job_id'], 'features': feats, 'prediction': repr(pred), 'actual': repr(actual) if actual is not None else None, 'residual': repr(actual - pred) if actual is not None else None, 'domain_status': status, 'backend': r['selected_backend']})
        return {'model_id': mid, 'target': m['target'], 'unit': m.get('target_unit'), 'rows': rows, 'note': 'residual = actual - prediction; only this workspace\'s runs; no claim about unseen jobs'}

    # ---- scheduling signal -----------------------------------------------------------------------------
    def scheduling_enabled(self, db):
        row = db.execute('SELECT value FROM meta WHERE key=?', (SCHEDULING_KEY,)).fetchone()
        return (row['value'] == '1') if row else True

    def set_scheduling(self, db, principal, enabled):
        principal.require('calibration:write')
        db.execute('INSERT INTO meta (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (SCHEDULING_KEY, '1' if enabled else '0'))
        history.record(db, principal.workspace, principal.id, 'calibration.promoted', 'service', 'scheduling', {'calibrated_scheduling_enabled': bool(enabled)})
        return {'calibrated_scheduling_enabled': bool(enabled), 'note': 'disabling keeps every model and its evidence; the scheduler falls back to the measured evidence thresholds'}

    def estimate(self, db, workspace, task_kind, work_units, backend):
        """Advisory estimate for the scheduler and planner: (duration_ms or None, reason, model_id)."""
        if not self.scheduling_enabled(db):
            return None, 'calibrated scheduling disabled by operator', None
        d = db.execute('SELECT model_id FROM calibration_defaults WHERE scope IN (?, ?) AND workspace=? ORDER BY scope DESC LIMIT 1', (task_kind + ':' + backend, task_kind + ':any', workspace)).fetchone()
        if d is None:
            return None, 'no approved calibration for %s on %s' % (task_kind, backend), None
        row = db.execute('SELECT * FROM calibration_models WHERE id=?', (d['model_id'],)).fetchone()
        if row is None or row['state'] != 'approved':
            return None, 'approved model missing or retired', None
        if not self.fresh(row):
            return None, 'calibration older than the freshness policy', row['id']
        if row['implementation_digest'] != compute_manifests.implementation_digest():
            return None, 'calibration was fitted for a different compute implementation', row['id']
        m, _ = self.manifest_of(db, row)
        feats = {'work_units': float(work_units), 'backend_cuda': 1.0 if backend == 'cuda' else 0.0}
        feats = {k: feats[k] for k in m['features'] if k in feats}
        if set(feats) != set(m['features']):
            return None, 'model features not derivable at scheduling time', row['id']
        status, _ = cal.domain_status(feats, m['domain'])
        if status != 'interpolation':
            return None, 'request outside the calibration domain (extrapolation refused for automatic selection)', row['id']
        pred = cal.apply_model(m, feats)
        if not (pred == pred) or pred <= 0 or pred in (float('inf'), float('-inf')):
            return None, 'prediction not finite/positive; ignored', row['id']
        return pred, 'approved calibration %s' % row['id'], row['id']

    def plan(self, db, principal, task_kind, inputs):
        """Counterfactual comparison of permitted backends for one validated request. Nothing is created."""
        principal.require('job:read')
        from .compute import inputs as compute_inputs, service as compute_svc
        if task_kind not in compute_manifests.KINDS:
            raise ServiceError('VALIDATION', 'task_kind')
        compute_inputs.VALIDATORS[task_kind](inputs)
        work = compute_inputs.work_units(task_kind, inputs)
        man = compute_manifests.MANIFESTS[task_kind]
        caps = compute_svc.capabilities(db, self.settings)
        live = caps['facts']['currently_available']['live_worker_devices']
        reserved = {r['device']: r['n'] for r in caps['facts']['currently_available']['reservations_in_use']}
        slots = {'cpu': self.settings.limits['compute_cpu_slots'], 'cuda': self.settings.limits['compute_gpu_slots']}
        policy = inputs.get('device_policy', 'auto')
        candidates = []
        for backend in ('cpu', 'cuda'):
            allowed = backend in man['devices'] and (policy == 'auto' or {'cpu': 'cpu', 'gpu': 'cuda'}[policy] == backend)
            est, reason, mid = self.estimate(db, principal.workspace, task_kind, work, backend)
            c = {'backend': backend, 'permitted_by_manifest': backend in man['devices'], 'permitted_by_policy': allowed, 'precision': man['precision'][0],
                 'live_workers_offer_it': backend in live, 'slots_free': max(0, slots[backend] - reserved.get(backend, 0)), 'predicted_duration_ms': repr(est) if est is not None else None,
                 'prediction_status': 'calibrated' if est is not None else 'no prediction (conservative fallback: evidence thresholds)', 'reason': reason, 'model_id': mid,
                 'hard_constraints': {'device_slots': slots[backend], 'timeout_seconds': self.settings.limits['compute_timeout_seconds'], 'cpu_seconds': self.settings.limits['compute_cpu_seconds']}}
            c['eligible'] = c['permitted_by_manifest'] and c['permitted_by_policy'] and c['live_workers_offer_it']
            candidates.append(c)
        eligible = [c for c in candidates if c['eligible']]
        with_pred = [c for c in eligible if c['predicted_duration_ms'] is not None]
        if with_pred:
            best = min(with_pred, key=lambda c: float(c['predicted_duration_ms']))
            basis = 'lowest calibrated prediction among eligible backends (advisory; admission rechecks capacity and authority)'
        elif eligible:
            from .compute.engine import AUTO_CUDA_MIN_WORK
            th = AUTO_CUDA_MIN_WORK.get(task_kind)
            pref = 'cuda' if (th is not None and work >= th and any(c['backend'] == 'cuda' for c in eligible)) else 'cpu'
            best = next((c for c in eligible if c['backend'] == pref), eligible[0])
            basis = 'no usable prediction: measured evidence thresholds (AUTO_CUDA_MIN_WORK) decide; conservative'
        else:
            best, basis = None, 'no eligible backend is live'
        return {'task_kind': task_kind, 'work_units': work, 'unit': man['work_unit'], 'candidates': candidates, 'recommended': best['backend'] if best else None, 'basis': basis,
                'scientific_semantics': 'unchanged: every backend runs the same accepted model and verification policy; the choice affects time only',
                'not_a_measurement': True, 'not_a_global_optimum': True, 'next': 'submit the request with device_policy set to the chosen backend under the ordinary quote/contract rules'}

    def replay(self, db, principal, task_kind, limit=200):
        """Bounded replay: for recorded runs of a kind, compare the backend the evidence policy would choose with the
        calibrated choice, using the recorded actual duration where the recorded backend matches."""
        principal.require('job:read_private')
        from .compute.engine import AUTO_CUDA_MIN_WORK
        runs = db.execute("SELECT r.*, j.state FROM compute_runs r JOIN jobs j ON j.id=r.job_id WHERE r.workspace=? AND r.kind=? AND r.duration_ms IS NOT NULL AND j.state='succeeded' ORDER BY r.started_at DESC LIMIT ?",
                          (principal.workspace, task_kind, limit)).fetchall()
        th = AUTO_CUDA_MIN_WORK.get(task_kind)
        rows, agree, changed = [], 0, 0
        for r in runs:
            evidence_choice = 'cuda' if (th is not None and r['work_total'] >= th) else 'cpu'
            preds = {b: self.estimate(db, principal.workspace, task_kind, r['work_total'], b) for b in ('cpu', 'cuda')}
            usable = {b: v[0] for b, v in preds.items() if v[0] is not None}
            cal_choice = min(usable, key=usable.get) if usable else evidence_choice
            agree += cal_choice == evidence_choice; changed += cal_choice != evidence_choice
            rows.append({'job_id': r['job_id'], 'work_units': r['work_total'], 'recorded_backend': r['selected_backend'], 'actual_ms': r['duration_ms'], 'evidence_choice': evidence_choice, 'calibrated_choice': cal_choice,
                         'predictions_ms': {b: repr(v) for b, v in usable.items()}, 'prediction_error_ms': repr(usable[r['selected_backend']] - r['duration_ms']) if r['selected_backend'] in usable else None})
        return {'task_kind': task_kind, 'runs': len(rows), 'agreements': agree, 'changed_choices': changed, 'rows': rows,
                'fairness': 'unchanged by construction: backend choice does not alter the fair order across submitters', 'note': 'replay over recorded runs; not a claim about future workloads'}


def rows_for_fit(db, store, workspace, inputs):
    """Rows the child fits (and the verifier refits): the bound dataset, rows with missing cells excluded, censored rows excluded for the point model."""
    ds = db.execute('SELECT * FROM calibration_datasets WHERE id=? AND workspace=?', (inputs['dataset_id'], workspace)).fetchone()
    if ds is None:
        raise ServiceError('NOT_FOUND', 'calibration dataset')
    data = merkle.parse(store.load(db, ds['rows_artifact_id'], workspace))
    columns = data['columns']
    needed = [columns.index(c) for c in inputs['features'] + [inputs['target']] if c in columns]
    cens = columns.index('censored') if 'censored' in columns else None
    rows = [r for r in data['rows'] if all(r[j] is not None for j in needed) and (cens is None or r[cens] == '0')]
    return {'columns': columns, 'rows': rows, 'units': json.loads(ds['units_json']), 'digest': ds['digest'], 'excluded_rows': len(data['rows']) - len(rows)}
