"""§65-7 per-workspace local service bundles and §65-8 export/import compatibility checks.

A bundle is a portable, data-only description of a finite set of services (kind, version, price), model revisions
(pinned hub repo + commit + digests), verification policy templates, the approval policy and the warmup policy, plus
the runtime identity it was produced under. It never carries credentials, grants, quotes, documents, vectors, weights
or keys. `check` validates a bundle against THIS instance (installed kinds, verifier digests, runtime versions, weights
present with matching digests) without changing anything; `import` applies only compatible items and never promotes a
model, loads a runtime, widens a grant or executes work."""
import hashlib
import json

from experiments.private_receipts import receipt as merkle
from . import history
from .compute import manifests as compute_manifests
from .compute.engine import compute_interpreter
from .db import now
from .errors import ServiceError

SCHEMA = 'metacoin-service-bundle/v1'
LIMITS = {'services': 32, 'models': 8, 'policies': 32}


def runtime_identity(settings):
    from .models import engine as model_engine
    from . import verification
    rt = compute_interpreter(settings) or {}
    return {'python': rt.get('python'), 'torch': rt.get('torch'), 'cuda': bool(rt.get('cuda')), 'compute_implementation': compute_manifests.implementation_digest(),
            'model_runtime_implementation': model_engine.implementation_digest(), 'verification_implementation': verification.implementation_digest()}


class Bundles:
    def __init__(self, settings, services):
        self.settings, self.svc = settings, services

    def export(self, db, principal, body):
        principal.require('model:admin'); principal.require('admin:credentials')
        if type(body) is not dict or type(body.get('name')) is not str or not 1 <= len(body['name']) <= 64:
            raise ServiceError('VALIDATION', 'name')
        cat = {s['id']: s for s in self.svc.catalog.list(db, principal)}
        wanted = body.get('services')
        if wanted is None:
            wanted = list(cat)
        if type(wanted) is not list or len(wanted) > LIMITS['services'] or not all(type(x) is str for x in wanted):
            raise ServiceError('VALIDATION', 'services: list of ids or kinds')
        services = []
        for w in wanted:
            s = cat.get(w) or next((x for x in cat.values() if x['kind'] == w), None)
            if s is None:
                raise ServiceError('NOT_FOUND', {'service': w})
            services.append({'name': s['name'], 'kind': s['kind'], 'version': s['version'], 'price_per_unit': s['price']['amount_per_unit'], 'description': s.get('description', ''), 'verifier_digest': s['verifier_digest']})
        models = []
        for rid in body.get('models') or []:
            r = self.svc.models.row(db, rid)
            models.append({'model_id': r['model_id'], 'hub_repo': r['hub_repo'], 'revision': r['revision'], 'operations': json.loads(r['operations_json']), 'license': r['license'], 'precision': r['precision'],
                           'weight_digest': r['weight_digest'], 'tokenizer_digest': r['tokenizer_digest'], 'resource_estimate_bytes': r['resource_estimate_bytes'], 'description': r['description'] or ''})
        if len(models) > LIMITS['models']:
            raise ServiceError('VALIDATION', 'models: at most %d' % LIMITS['models'])
        policies = []
        for pid in body.get('verification_policies') or []:
            r = self.svc.verification.policy(db, principal, pid)
            policies.append({'name': r['name'], 'class': r['class'], 'params': json.loads(r['params_json']), 'max_work': r['max_work'], 'scope': r['scope'], 'verifier_digest': r['verifier_digest']})
        bundle = {'schema': SCHEMA, 'name': body['name'], 'services': services, 'models': models, 'verification_policies': policies,
                  'approval_policy': self.svc.approvals.policy(db, principal.workspace) if body.get('include_approval_policy', True) else None,
                  'warmup': None, 'runtime_identity': runtime_identity(self.settings), 'produced_at': now(), 'produced_by_workspace': principal.workspace,
                  'contents': 'services, pinned model identities, verification templates, approval and warmup policies; no credentials, grants, quotes, documents, vectors, weights or keys'}
        if body.get('include_warmup', True):
            row = db.execute("SELECT value FROM meta WHERE key='model_warmup'").fetchone()
            if row:
                w = json.loads(row['value'])
                ids = {r['id']: r['model_id'] for r in db.execute('SELECT id, model_id FROM model_revisions').fetchall()}
                bundle['warmup'] = {'enabled': w.get('enabled'), 'model_ids': [ids.get(x) for x in w.get('revision_ids', []) if ids.get(x)], 'ceiling_bytes': w.get('ceiling_bytes')}
        bundle['digest'] = hashlib.sha256(merkle.canonical({k: v for k, v in bundle.items() if k != 'digest'})).hexdigest()
        history.record(db, principal.workspace, principal.id, 'artifact.exported', 'bundle', bundle['digest'][:16], {'name': body['name'], 'services': len(services), 'models': len(models), 'policies': len(policies)})
        return bundle

    # ---- §65-8 compatibility checks (data-only) -------------------------------------------------------------
    def check(self, db, principal, bundle):
        principal.require('model:admin')
        if type(bundle) is not dict or bundle.get('schema') != SCHEMA or type(bundle.get('digest')) is not str:
            raise ServiceError('VALIDATION', 'bundle: %s with digest' % SCHEMA)
        body = {k: v for k, v in bundle.items() if k != 'digest'}
        items = []
        digest_ok = hashlib.sha256(merkle.canonical(body)).hexdigest() == bundle['digest']
        items.append({'item': 'bundle_digest', 'ok': digest_ok, 'detail': 'digest matches content' if digest_ok else 'content altered since export'})
        ident = runtime_identity(self.settings)
        bident = bundle.get('runtime_identity') or {}
        for k in ('torch', 'compute_implementation', 'model_runtime_implementation', 'verification_implementation'):
            items.append({'item': 'runtime.' + k, 'ok': bident.get(k) == ident.get(k), 'detail': {'bundle': bident.get(k), 'installed': ident.get(k)}, 'blocking': k != 'torch'})
        from .catalog import INSTALLED, verifier_digest
        cat = self.svc.catalog.list(db, principal, include_retired=True)
        for s in bundle.get('services') or []:
            if type(s) is not dict or s.get('kind') not in INSTALLED:
                items.append({'item': 'service.' + str((s or {}).get('name')), 'ok': False, 'detail': 'kind not installed here', 'blocking': True}); continue
            same = [x for x in cat if x['name'] == s['name'] and x['version'] == s['version']]
            if same:
                x = same[0]
                identical = x['kind'] == s['kind'] and x['price']['amount_per_unit'] == s.get('price_per_unit')
                items.append({'item': 'service.' + s['name'] + '.v%d' % s['version'], 'ok': identical, 'detail': 'exists with identical terms' if identical else 'exists with different terms (register a new version instead)', 'blocking': not identical, 'action': 'none'})
            else:
                vd = verifier_digest(s['kind'])
                items.append({'item': 'service.' + s['name'] + '.v%d' % s['version'], 'ok': vd == s.get('verifier_digest'), 'detail': {'verifier_digest_bundle': s.get('verifier_digest'), 'installed': vd}, 'blocking': vd != s.get('verifier_digest'), 'action': 'register'})
        from .models import registry as registry_mod
        for m in bundle.get('models') or []:
            insp = registry_mod.inspect_artifact(self.settings, m['hub_repo'], m['revision'])
            present = insp['local_dir_exists'] and not insp['problems']
            wd = insp['files'].get('model.safetensors', {}).get('sha256')
            digest_match = (wd == m.get('weight_digest')) if (present and m.get('weight_digest')) else None
            existing = db.execute('SELECT id, status FROM model_revisions WHERE model_id=? AND revision=?', (m['model_id'], m['revision'])).fetchone()
            ok = (existing is not None) or (present and digest_match is not False)
            items.append({'item': 'model.' + m['model_id'] + '@' + m['revision'][:8], 'ok': ok, 'blocking': not ok,
                          'detail': {'registered_here': existing['id'] if existing else None, 'weights_present': present, 'weight_digest_matches': digest_match, 'problems': insp['problems'][:3],
                                     'architecture': insp.get('architecture'), 'acquisition': 'install the pinned artifact under the model store (download record with digests), then import again' if not present else None},
                          'action': 'none' if existing else ('register (not promoted, not loaded)' if ok else 'blocked')})
        from .verification import CLASSES, implementation_digest as vdigest
        for p in bundle.get('verification_policies') or []:
            ok = p.get('class') in CLASSES
            items.append({'item': 'verification_policy.' + str(p.get('name')), 'ok': ok, 'blocking': not ok, 'detail': {'class': p.get('class'), 'verifier_current_here': p.get('verifier_digest') == vdigest()}, 'action': 'create a new version here (verifier digest of this instance)' if ok else 'unknown class'})
        from .approvals import ACTIONS
        ap = bundle.get('approval_policy')
        if ap is not None:
            ok = type(ap) is dict and type(ap.get('required')) is list and set(ap['required']) <= set(ACTIONS)
            items.append({'item': 'approval_policy', 'ok': ok, 'blocking': not ok, 'detail': ap, 'action': 'set'})
        wu = bundle.get('warmup')
        if wu:
            items.append({'item': 'warmup', 'ok': True, 'blocking': False, 'detail': wu, 'action': 'set for the model ids registered here (only installed revisions are kept resident)'})
        blocking = [i['item'] for i in items if not i['ok'] and i.get('blocking', True)]
        return {'compatible': not blocking, 'blocking': blocking, 'items': items, 'nothing_executed': True, 'note': 'data-only check: no registration, load, promotion or execution happened'}

    def import_bundle(self, db, principal, bundle, apply=False):
        principal.require('model:admin'); principal.require('admin:credentials')
        report = self.check(db, principal, bundle)
        if not apply:
            return dict(report, applied=False)
        if not report['compatible']:
            raise ServiceError('CONFLICT', {'code': 'bundle_incompatible', 'blocking': report['blocking']})
        applied = {'services': [], 'models': [], 'verification_policies': [], 'approval_policy': None, 'warmup': None}
        by_item = {i['item']: i for i in report['items']}
        cat = self.svc.catalog.list(db, principal, include_retired=True)
        for s in bundle.get('services') or []:
            if by_item['service.' + s['name'] + '.v%d' % s['version']].get('action') == 'register':
                sid = self.svc.catalog.register(db, principal.id, name=s['name'], kind=s['kind'], version=s['version'], price_per_unit=s['price_per_unit'], description=s.get('description', ''), workspace=principal.workspace)
                applied['services'].append(sid)
        id_by_model = {}
        for m in bundle.get('models') or []:
            existing = db.execute('SELECT id FROM model_revisions WHERE model_id=? AND revision=?', (m['model_id'], m['revision'])).fetchone()
            if existing:
                id_by_model[m['model_id']] = existing['id']; continue
            body = {k: m[k] for k in ('model_id', 'hub_repo', 'revision', 'operations', 'license', 'precision', 'description') if m.get(k) is not None}
            if m.get('resource_estimate_bytes'):
                body['resource_estimate_bytes'] = m['resource_estimate_bytes']
            v = self.svc.models.register(db, principal, body)
            id_by_model[m['model_id']] = v['id']; applied['models'].append(v['id'])
        for p in bundle.get('verification_policies') or []:
            v = self.svc.verification.create_policy(db, principal, {'name': p['name'], 'class': p['class'], 'params': p.get('params') or {}, 'max_work': p['max_work'], 'scope': p.get('scope', 'all-results')})
            applied['verification_policies'].append(v['id'])
        if bundle.get('approval_policy') is not None:
            applied['approval_policy'] = self.svc.approvals.set_policy(db, principal, bundle['approval_policy']['required'])
        wu = bundle.get('warmup')
        if wu and wu.get('model_ids'):
            from .models import service as model_svc
            ids = [id_by_model[m] for m in wu['model_ids'] if m in id_by_model and db.execute('SELECT installed FROM model_revisions WHERE id=?', (id_by_model[m],)).fetchone()['installed']]
            if ids:
                applied['warmup'] = model_svc.set_warmup(db, principal, self.settings, {'enabled': bool(wu.get('enabled', True)), 'revision_ids': ids, 'ceiling_bytes': min(int(wu.get('ceiling_bytes') or self.settings.limits['model_memory_budget_bytes']), self.settings.limits['model_memory_budget_bytes'])})['policy']
        history.record(db, principal.workspace, principal.id, 'model.registered', 'bundle', bundle['digest'][:16], {'imported': {k: (len(v) if isinstance(v, list) else bool(v)) for k, v in applied.items()}})
        return dict(report, applied=True, results=applied, not_done='no model promoted, no runtime loaded, no grant issued, no work executed')
